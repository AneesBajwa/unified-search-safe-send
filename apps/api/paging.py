"""Cursor pagination for the history listings (task 9.9).

``api-design.md`` §Conventions: ``?limit`` (≤100, default 25), ``?cursor``,
newest first.

**Keyset, not OFFSET.** Both listings are ordered by ``created_at DESC`` and both
grow at the head — a search or a send lands while someone is paging. With OFFSET
that shifts every later page by one and the reader sees a row twice or not at
all; with a keyset the cursor names a position in the ordering rather than a
count of rows before it, so concurrent inserts are invisible to a page already
in progress.

The tiebreak on ``id`` is not decoration: ``created_at`` is ``now()``, two rows
written in one transaction share it exactly, and a cursor on a non-unique key
either repeats or skips whichever of them the plan happens to return first.

The cursor is opaque on purpose — base64 of a value pair, not a signed token.
There is nothing in it to protect: it names a position in a listing that is
already scoped to the caller's own rows, so a forged cursor can only ever
address the forger's own data.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from api.errors import ApiError

#: api-design.md §Conventions.
DEFAULT_LIMIT = 25
MAX_LIMIT = 100


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    row_id: uuid.UUID


def encode(created_at: datetime, row_id: uuid.UUID | str) -> str:
    raw = f"{created_at.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(cursor: str) -> Cursor:
    """Parse a cursor, or refuse with a code the client can act on.

    A malformed cursor is the caller's bug and recoverable in one step — drop it
    and re-read the first page — so it is a named 422 rather than a 500 that
    reads like ours.
    """
    try:
        padding = "=" * (-len(cursor) % 4)
        stamp, _, row_id = base64.urlsafe_b64decode(cursor + padding).decode().partition("|")
        return Cursor(created_at=datetime.fromisoformat(stamp), row_id=uuid.UUID(row_id))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ApiError("invalid_cursor", "this cursor is not one we issued") from exc


def page(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    time_key: str = "created_at",
    id_key: str = "id",
) -> tuple[list[dict[str, Any]], str | None]:
    """Trim an over-fetched result set and mint the next cursor.

    Callers ask the database for ``limit + 1`` rows. The extra row is never
    returned — it is only evidence that another page exists, which is how
    ``next_cursor`` can be ``null`` on the last page instead of handing the
    client a cursor that resolves to nothing. "There is more" is a fact worth
    getting right: a UI that shows a Load-more button leading to an empty page
    looks broken in a way that is hard to distinguish from a real failure.
    """
    if len(rows) <= limit:
        return rows, None
    kept = rows[:limit]
    last = kept[-1]
    return kept, encode(last[time_key], last[id_key])
