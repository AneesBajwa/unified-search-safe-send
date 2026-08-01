"""Gmail REST client: search, and the draft-then-send mechanism (R3, R24).

Two things live here that are easy to get wrong and expensive to get wrong.

🔴 **The 401 ladder.** A Gmail ``401 authError`` says the *access token* was not
accepted. It is **not** evidence the grant is revoked. The correct response is
refresh → retry **once** → escalate to ``needs_reconnect`` only if the *refresh*
returns ``invalid_grant``. Treating a raw 401 as terminal is described in the
research as the most common implementation error in this area, and it
disconnects healthy users constantly (R24). The ladder is implemented once, in
:meth:`GmailClient._call`, so neither the adapter nor the send provider can
forget it.

🔴 **Zero retries at the transport.** ``core.http.provider_client`` pins
``retries=0``. The equivalent when using Google's own client library is
``execute(num_retries=0)`` — its default is not zero, and a retried *send*
duplicates a message underneath the gate entirely (R23).

The draft flow itself is documented on :class:`GmailClient.create_draft`.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import httpx

from core.adapters.types import TokenKind
from core.errors import ProviderError
from core.http import json_body, provider_client

logger = logging.getLogger("core.providers.gmail")

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

#: Gmail's own web UI. `#all/` rather than `#inbox/` because a sent message is
#: not in the inbox, and a permalink that 404s for the sender is worse than none.
MESSAGE_PERMALINK = "https://mail.google.com/mail/u/0/#all/{message_id}"
DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"

TokenGetter = Callable[[TokenKind], Awaitable[str]]


@dataclass(frozen=True)
class GmailMessage:
    id: str
    thread_id: str
    subject: str
    snippet: str
    sender: str
    date: str | None


class GmailClient:
    """One connection's worth of Gmail.

    ``expire_access_token`` is the hook that makes the 401 ladder possible
    without widening ``AdapterContext.get_token``, whose signature is fixed by
    ``contracts.md`` §1. It marks the cached access token stale; the *next*
    ``get_token`` then refreshes under the advisory lock — so a concurrent
    worker that already refreshed is observed rather than duplicated.
    """

    def __init__(
        self,
        *,
        get_token: TokenGetter,
        expire_access_token: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._get_token = get_token
        self._expire = expire_access_token

    # -- search --------------------------------------------------------------

    async def search(self, query: str, *, limit: int = 10) -> list[GmailMessage]:
        listing = await self._call(
            "GET", f"{BASE}/messages", params={"q": query, "maxResults": str(limit)}
        )
        ids = [item.get("id") for item in listing.get("messages") or []]

        messages: list[GmailMessage] = []
        for message_id in ids:
            if not message_id:
                continue
            # `format=metadata` with a header allow-list: the full body is not
            # needed for a search result and asking for it would pull megabytes
            # per hit through a 30-second per-run deadline.
            detail = await self._call(
                "GET",
                f"{BASE}/messages/{message_id}",
                params=[
                    ("format", "metadata"),
                    ("metadataHeaders", "Subject"),
                    ("metadataHeaders", "From"),
                    ("metadataHeaders", "Date"),
                ],
            )
            messages.append(_message_from(detail))
        return messages

    # -- draft-then-send -----------------------------------------------------

    async def create_draft(
        self, *, to: str, subject: str | None, body: str, thread_id: str | None = None
    ) -> str:
        """Create the draft and return its **stable** id.

        Gmail's own drafts guide is explicit that the drafts resource "provides
        a stable ID because the underlying message IDs change every time the
        message is replaced", and that on send "the draft is automatically
        deleted". Those two sentences together are what make the draft id a
        server-side idempotency token whose *existence* is the state machine —
        and why this is `drafts.create`/`drafts.send` rather than the simpler
        `messages.send` (R3).
        """
        payload: dict[str, Any] = {"message": {"raw": _mime(to=to, subject=subject, body=body)}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        created = await self._call("POST", f"{BASE}/drafts", json=payload)
        draft_id = created.get("id")
        if not draft_id:
            raise ProviderError(
                provider="google",
                code="draft_without_id",
                detail="drafts.create returned no id, so the send has no idempotency token",
                status=502,
            )
        return str(draft_id)

    async def send_draft(self, draft_id: str) -> tuple[str, str]:
        """Send it. Returns ``(message_id, thread_id)``."""
        sent = await self._call("POST", f"{BASE}/drafts/send", json={"id": draft_id})
        return str(sent.get("id", "")), str(sent.get("threadId", ""))

    async def draft_exists(self, draft_id: str) -> bool:
        """🔴 The probe. ``200`` = not yet sent, ``404`` = already sent.

        A direct existence check rather than a search, and that is the whole
        point: Gmail's search index is eventually consistent, so an
        ``rfc822msgid:`` probe running immediately after a crash can return a
        false negative — and a false negative double-sends.
        """
        try:
            await self._call("GET", f"{BASE}/drafts/{draft_id}", params={"format": "minimal"})
        except ProviderError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def delete_draft(self, draft_id: str) -> None:
        """Tidy up a draft whose send failed permanently (task 8.2b).

        Only ever called with an id **we** stored. Never enumerates the user's
        drafts and never touches one it did not create — the scope permits it,
        which is exactly why the restraint has to be in the code.
        """
        try:
            await self._call("DELETE", f"{BASE}/drafts/{draft_id}")
        except ProviderError as exc:
            if exc.status == 404:
                return  # already gone; nothing to tidy
            logger.warning("could not delete draft %s: %s", draft_id, exc)

    # -- the one call path ---------------------------------------------------

    async def _call(
        self, method: str, url: str, *, params: Any = None, json: Any = None
    ) -> dict[str, Any]:
        response = await self._request(method, url, params=params, json=json)

        if response.status_code == 401 and self._expire is not None:
            # Rung one: the access token was not accepted. Refresh and retry
            # exactly once. If the refresh itself fails with `invalid_grant`,
            # `tokens.get_token` raises NeedsReconnect and we never get here —
            # which is the escalation, and the only one.
            logger.info("gmail returned 401; refreshing the access token and retrying once")
            await self._expire()
            response = await self._request(method, url, params=params, json=json)

        if response.status_code == 204 or not response.content:
            return {}

        payload = json_body(response)
        if response.status_code >= 400:
            raise ProviderError.from_google(
                payload, status=response.status_code, headers=dict(response.headers)
            )
        return payload

    async def _request(
        self, method: str, url: str, *, params: Any, json: Any
    ) -> httpx.Response:
        token = await self._get_token(TokenKind.OAUTH)
        async with provider_client() as client:
            return await client.request(
                method,
                url,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )


def _message_from(detail: dict[str, Any]) -> GmailMessage:
    headers = {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in (detail.get("payload") or {}).get("headers") or []
    }
    return GmailMessage(
        id=str(detail.get("id", "")),
        thread_id=str(detail.get("threadId", "")),
        subject=headers.get("subject", ""),
        snippet=str(detail.get("snippet", "")),
        sender=headers.get("from", ""),
        date=headers.get("date") or _from_internal_date(detail.get("internalDate")),
    )


def _from_internal_date(raw: Any) -> str | None:
    """`internalDate` is epoch **milliseconds** as a string.

    Used only when the `Date` header is absent, which happens more often than
    expected on drafts and on mail from misbehaving senders.
    """
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError):
        return None


def _mime(*, to: str, subject: str | None, body: str) -> str:
    """RFC 2822, base64url — the encoding `drafts.create` wants.

    No `Message-ID` is set. Google documents no guarantee that a client-supplied
    one survives, and the strongest field evidence says it does not; the draft
    id is the idempotency token instead, which is documented and deterministic
    (R3).
    """
    message = EmailMessage()
    message["To"] = to
    if subject:
        message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
