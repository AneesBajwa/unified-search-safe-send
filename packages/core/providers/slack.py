"""Slack Web API client: search, post, and the reconciliation probe (R4, R5).

🔴 **Token routing is per call, not per connection.** ``search.messages``
requires the **user** token (`xoxp`) and rejects a bot token with
``not_allowed_token_type``; everything else takes the **bot** token (`xoxb`).
That is why ``search:read`` goes in `user_scope` and the rest in `scope`, and
why one install yields two tokens (R4). A test asserts search never reaches for
the bot token, because the failure is silent at config time and only appears at
runtime.

🔴 **Almost every failure is HTTP 200.** Slack signals errors with
``{"ok": false, "error": "..."}``; only rate limiting is a 429. Anything reading
``response.status_code`` to decide success will conclude that every failure
succeeded.

🔴 **`include_all_metadata=true` on `conversations.history`,** or the idempotency
payload silently vanishes from the response leaving only ``event_type`` — and a
probe that cannot see its own fingerprint reports ABSENT, which double-sends
(R5).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.adapters.types import TokenKind
from core.errors import ProviderError
from core.http import json_body, provider_client

logger = logging.getLogger("core.providers.slack")

BASE = "https://slack.com/api"

#: The `metadata.event_type` every message we send carries. Slack requires a
#: type alongside the payload; ours names the product so a human reading raw
#: message JSON in Slack knows where it came from.
EVENT_TYPE = "unified_search_safe_send"

TokenGetter = Callable[[TokenKind], Awaitable[str]]


@dataclass(frozen=True)
class SlackMessage:
    channel_id: str
    channel_name: str
    ts: str
    text: str
    username: str
    permalink: str
    #: Only populated by history reads with `include_all_metadata=true`.
    idempotency_key: str | None = None


class SlackClient:
    def __init__(self, *, get_token: TokenGetter) -> None:
        self._get_token = get_token

    # -- search --------------------------------------------------------------

    async def search_messages(self, query: str, *, limit: int = 10) -> list[SlackMessage]:
        """The primary search path, on the **user** token.

        Each match carries its own ``permalink``, so there is no follow-up
        ``chat.getPermalink`` call — which matters at a 30-second per-run
        deadline with a Tier 2 rate limit.
        """
        payload = await self._call(
            "search.messages", TokenKind.USER, {"query": query, "count": str(limit)}
        )
        matches = ((payload.get("messages") or {}).get("matches")) or []
        return [_from_search_match(match) for match in matches]

    async def list_channels(self, *, limit: int = 100) -> list[dict[str, Any]]:
        payload = await self._call(
            "conversations.list",
            TokenKind.BOT,
            {"types": "public_channel", "limit": str(limit), "exclude_archived": "true"},
        )
        return list(payload.get("channels") or [])

    async def channel_history(
        self,
        channel: str,
        *,
        oldest: float | None = None,
        limit: int = 100,
        include_metadata: bool = False,
        join_if_needed: bool = False,
    ) -> list[SlackMessage]:
        """Read a channel's recent messages.

        🔴 ``join_if_needed`` exists because of a finding from spike 2 that the
        design did not anticipate: **`chat:write.public` lets the bot post to a
        public channel without joining, but `conversations.history` still
        returns `not_in_channel`.** Posting and reading have different
        membership rules.

        That asymmetry is dangerous specifically for the reconciliation probe.
        The send path can post to a channel the bot never joined; the probe then
        cannot read it back, returns INCONCLUSIVE, and a perfectly resolvable
        send parks in ``uncertain``. So the probe passes ``join_if_needed=True``
        and recovers exactly the way ``post_message`` already does.

        The **degraded search scan** deliberately passes False: silently joining
        every public channel in a workspace to search it is intrusive, visible
        to everyone in the channel, and not something a search should do. It
        skips what it cannot read instead.
        """
        params: dict[str, str] = {"channel": channel, "limit": str(limit)}
        if oldest is not None:
            params["oldest"] = f"{oldest:.6f}"
        if include_metadata:
            # 🔴 Load-bearing — confirmed empirically by spike 2 (2026-08-01):
            # without it the round-tripped `idempotency_key` reads back as None.
            params["include_all_metadata"] = "true"

        try:
            payload = await self._call("conversations.history", TokenKind.BOT, params)
        except ProviderError as exc:
            if not (join_if_needed and exc.code == "not_in_channel"):
                raise
            logger.info("not in %s; joining so history can be read, then retrying", channel)
            await self._call("conversations.join", TokenKind.BOT, {"channel": channel})
            payload = await self._call("conversations.history", TokenKind.BOT, params)

        return [
            _from_history_message(message, channel)
            for message in (payload.get("messages") or [])
        ]

    async def channel_name(self, channel: str) -> str | None:
        """Resolve an id to ``#name``.

        Called at **draft-write time only**, never on the send path: the
        confirmation digest is computed from the persisted display name, so a
        Slack outage must not be able to make a confirmed message unsendable.
        """
        try:
            payload = await self._call(
                "conversations.info", TokenKind.BOT, {"channel": channel}
            )
        except ProviderError as exc:
            logger.info("could not resolve channel %s: %s", channel, exc.code)
            return None
        name = ((payload.get("channel") or {}).get("name"))
        return f"#{name}" if name else None

    # -- send ----------------------------------------------------------------

    async def post_message(
        self, *, channel: str, text: str, idempotency_key: str
    ) -> tuple[str, str]:
        """Post, stamping the fingerprint. Returns ``(ts, channel_id)``.

        ``chat.postMessage`` has no idempotency parameter of any kind — verified
        against its full argument list — so the key rides in
        ``metadata.event_payload``, which is the only field that survives a
        round trip through ``conversations.history``.

        Payload constraints are real: snake_case keys starting with a letter, no
        nested objects, no arrays of objects. A flat string value fits trivially.
        """
        try:
            return await self._post_message_once(
                channel=channel, text=text, idempotency_key=idempotency_key
            )
        except ProviderError as exc:
            if exc.code != "not_in_channel":
                raise
            # `chat:write.public` prevents most of these. When it does not
            # apply, join and retry **once** — recovery is an action taken
            # before classification, not a fifth error class.
            logger.info("not in %s; joining and retrying once", channel)
            await self._call("conversations.join", TokenKind.BOT, {"channel": channel})
            return await self._post_message_once(
                channel=channel, text=text, idempotency_key=idempotency_key
            )

    async def _post_message_once(
        self, *, channel: str, text: str, idempotency_key: str
    ) -> tuple[str, str]:
        payload = await self._call(
            "chat.postMessage",
            TokenKind.BOT,
            {
                "channel": channel,
                "text": text,
                "metadata": json.dumps(
                    {
                        "event_type": EVENT_TYPE,
                        "event_payload": {"idempotency_key": idempotency_key},
                    }
                ),
            },
        )
        return str(payload.get("ts", "")), str(payload.get("channel", channel))

    async def permalink(self, *, channel: str, ts: str) -> str | None:
        try:
            payload = await self._call(
                "chat.getPermalink", TokenKind.BOT, {"channel": channel, "message_ts": ts}
            )
        except ProviderError:
            return None
        link = payload.get("permalink")
        return str(link) if link else None

    # -- the one call path ---------------------------------------------------

    async def _call(
        self, method: str, kind: TokenKind, params: dict[str, str]
    ) -> dict[str, Any]:
        token = await self._get_token(kind)
        async with provider_client() as client:
            response = await client.post(
                f"{BASE}/{method}",
                data=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        payload = json_body(response)

        if not payload.get("ok", False):
            # `Retry-After` threads through to `full_jitter`, where a provider's
            # own number always wins over our computed backoff. Ignoring it is
            # how a rate limit becomes a harder rate limit.
            raise ProviderError.from_slack(
                payload, status=response.status_code, headers=dict(response.headers)
            )
        return payload


def _from_search_match(match: dict[str, Any]) -> SlackMessage:
    channel = match.get("channel") or {}
    name = str(channel.get("name", ""))
    return SlackMessage(
        channel_id=str(channel.get("id", "")),
        channel_name=f"#{name}" if name else "",
        ts=str(match.get("ts", "")),
        text=str(match.get("text", "")),
        username=str(match.get("username") or match.get("user") or ""),
        permalink=str(match.get("permalink", "")),
    )


def _from_history_message(message: dict[str, Any], channel: str) -> SlackMessage:
    metadata = message.get("metadata") or {}
    payload = metadata.get("event_payload") or {}
    key = payload.get("idempotency_key")
    return SlackMessage(
        channel_id=channel,
        channel_name="",
        ts=str(message.get("ts", "")),
        text=str(message.get("text", "")),
        username=str(message.get("username") or message.get("user") or ""),
        permalink="",
        idempotency_key=str(key) if key else None,
    )
