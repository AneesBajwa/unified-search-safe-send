"""The Gmail search adapter (task 7.1).

One file, one class, one registry line. Nothing in the orchestrator, the merge
layer or the UI knows this exists — which is the claim phase 2's
source-agnosticism test defends and this file is the first real test of.

Normalization is **total**: every field of the closed ``Result`` is populated or
the row is dropped with a warning by ``assert_normalized``. A card with a blank
title reads as a bug in our product rather than a gap in Gmail's data, so a
message with no subject gets an explicit placeholder rather than an empty
string.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from core.adapters.types import AdapterContext, Result
from core.connections import tokens
from core.providers.gmail import MESSAGE_PERMALINK, GmailClient, GmailMessage

SOURCE = "gmail"

#: What a subject-less message is called. Named rather than blank, and phrased
#: as a fact about the message rather than as an error about our processing.
NO_SUBJECT = "(no subject)"

#: A per-run ceiling. Each hit costs a second HTTP call for its headers, and the
#: run has a 30-second deadline that has to cover all of them.
DEFAULT_LIMIT = 10


class GmailAdapter:
    source = SOURCE

    def __init__(self, *, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = limit

    async def search(self, query: str, ctx: AdapterContext) -> list[Result]:
        client = GmailClient(
            get_token=ctx.get_token,
            # The 401 ladder's bottom rung, bound to this connection. Without
            # it a 401 has nowhere to go but up, and escalating a routine
            # expired access token to `needs_reconnect` disconnects healthy
            # users constantly (R24).
            expire_access_token=_expire_for(ctx),
        )
        messages = await client.search(query, limit=self._limit)
        return [_to_result(message) for message in messages]


def _expire_for(ctx: AdapterContext):  # type: ignore[no-untyped-def]
    connection_id = ctx.connection_id
    if connection_id is None:
        return None

    async def expire() -> None:
        await tokens.expire_access_token(connection_id)

    return expire


def _to_result(message: GmailMessage) -> Result:
    return Result(
        source=SOURCE,
        id=message.id,
        title=message.subject.strip() or NO_SUBJECT,
        # Gmail's own snippet — already the "relevant excerpt" the brief asks
        # for, and already truncated by Google, so we neither re-derive it nor
        # pull the full body to build one.
        snippet=message.snippet.strip() or NO_SUBJECT,
        author=message.sender or None,
        timestamp=_iso(message.date),
        url=MESSAGE_PERMALINK.format(message_id=message.id),
    )


def _iso(raw: str | None) -> str | None:
    """RFC 2822 ``Date:`` -> ISO 8601.

    ``assert_normalized`` parses timestamps with ``datetime.fromisoformat``, so
    a raw ``Mon, 21 Jul 2025 09:12:33 -0700`` would fail the boundary check and
    drop an otherwise perfectly good result. Converted here rather than loosened
    there: the common shape is ISO 8601, and every adapter meets it.
    """
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        # Already ISO (the `internalDate` fallback path), or unparseable.
        try:
            return datetime.fromisoformat(raw).isoformat()
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # A naive `Date:` header is legal and does happen. UTC rather than local
        # time, so a result's age does not depend on where the worker runs.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()
