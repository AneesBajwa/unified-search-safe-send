"""The Slack search adapter, with a degraded fallback (tasks 7.2-7.3).

Two paths, and which one ran is reported honestly rather than hidden.

**Primary** — ``search.messages`` on the **user** token. Each match already
carries its own ``permalink``, so there is no follow-up ``chat.getPermalink``
call on this path.

**Degraded** — when `search:read` was never granted, or the workspace refuses
search, the adapter scans ``conversations.history`` across the channels the bot
can see and filters in-process. That is a genuinely worse search: it sees only
joined public channels and matches on substrings rather than Slack's index. So
the run reports ``mode: degraded`` and the UI badges it. A source that quietly
returns worse results while claiming to be live is the exact dishonesty the
status chip exists to prevent.

🔴 The fallback is **never** used for reconciliation. That is a different
question with a different failure mode: ``search.messages``' index is eventually
consistent, and a probe that cannot find a just-posted message reports ABSENT,
which double-sends (R5). Reconciliation lives in ``core.send.slack`` and uses
``conversations.history`` on the single target channel.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from core.adapters.types import AdapterContext, Result
from core.enums import SourceMode
from core.errors import ProviderError
from core.providers.slack import SlackClient, SlackMessage

logger = logging.getLogger("core.adapters.slack")

SOURCE = "slack"
DEFAULT_LIMIT = 10

#: How many channels the degraded scan will look at, and how deep. Bounded
#: because this path runs inside the same 30-second per-run deadline as the
#: single API call it is standing in for.
FALLBACK_CHANNELS = 10
FALLBACK_DEPTH = 50

#: The Slack errors that mean "search is not available to this connection"
#: rather than "this search failed". Only these fall back; everything else is a
#: real failure and is allowed to fail, because a fallback that swallows a
#: revoked token would report thin results instead of a reconnect prompt.
FALLBACK_TRIGGERS = frozenset({"missing_scope", "not_allowed_token_type", "no_permission"})


class SlackAdapter:
    """Reports its own mode when it degrades.

    ``reported_mode`` starts as ``None`` — meaning "no opinion, keep what the
    registry said" — and is set only when this run actually took the fallback.
    The adapter is constructed per run, so the value can never leak from one
    search into the next.
    """

    source = SOURCE

    def __init__(self, *, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = limit
        self.reported_mode: SourceMode | None = None

    async def search(self, query: str, ctx: AdapterContext) -> list[Result]:
        client = SlackClient(get_token=ctx.get_token)
        try:
            matches = await client.search_messages(query, limit=self._limit)
        except ProviderError as exc:
            if exc.code not in FALLBACK_TRIGGERS:
                raise
            logger.warning(
                "slack search unavailable (%s); falling back to a channel scan", exc.code
            )
            self.reported_mode = SourceMode.DEGRADED
            return await self._scan(client, query, ctx)
        return [_to_result(match) for match in matches if match.permalink]

    async def _scan(
        self, client: SlackClient, query: str, ctx: AdapterContext
    ) -> list[Result]:
        needle = query.casefold()
        results: list[Result] = []
        channels = await client.list_channels(limit=FALLBACK_CHANNELS)

        for channel in channels:
            channel_id = str(channel.get("id", ""))
            name = str(channel.get("name", ""))
            if not channel_id:
                continue
            if datetime.now(UTC) >= ctx.deadline:
                # The per-run deadline is a hard stop, and partial results from
                # a degraded scan are still results. Stopping here reports what
                # was found; running on would fail the whole source.
                logger.info("degraded scan stopped at the run deadline")
                break
            try:
                history = await client.channel_history(channel_id, limit=FALLBACK_DEPTH)
            except ProviderError as exc:
                logger.info("skipping channel %s in degraded scan: %s", name, exc.code)
                continue

            for message in history:
                if needle not in message.text.casefold():
                    continue
                permalink = await client.permalink(channel=channel_id, ts=message.ts)
                if not permalink:
                    # No permalink means no working link on the card, and the
                    # brief grades clickable results. Dropped rather than shown.
                    continue
                results.append(
                    _to_result(
                        SlackMessage(
                            channel_id=channel_id,
                            channel_name=f"#{name}" if name else channel_id,
                            ts=message.ts,
                            text=message.text,
                            username=message.username,
                            permalink=permalink,
                        )
                    )
                )
                if len(results) >= self._limit:
                    return results
        return results


def _to_result(message: SlackMessage) -> Result:
    channel = message.channel_name or message.channel_id
    text = message.text.strip()
    return Result(
        source=SOURCE,
        # `channel:ts` rather than `ts` alone: `ts` is unique only within a
        # channel, and the same id appearing under two channels would collide
        # in the merged list.
        id=f"{message.channel_id}:{message.ts}",
        # Slack messages have no subject, so the channel is the most useful
        # thing a scanning eye can anchor on.
        title=f"{channel}: {_first_line(text)}" if channel else _first_line(text),
        snippet=text or _first_line(text),
        author=message.username or None,
        timestamp=_iso(message.ts),
        url=message.permalink,
    )


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "(no text)"
    return line if len(line) <= 120 else line[:117] + "…"


def _iso(ts: str) -> str | None:
    """Slack's ``ts`` is epoch seconds with microseconds, as a string."""
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (TypeError, ValueError):
        return None
