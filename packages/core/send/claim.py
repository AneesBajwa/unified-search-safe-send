"""The send claim (openspec task 5.3, design D5(a), risks.md R22).

One row per ``(user, idempotency_key)``, and **the database picks the winner**.
Exactly one caller gets a row back; every other caller — concurrent or minutes
later — gets none, re-reads the existing send and returns *its* current state.
There is no check-then-act window because there is no check.

🔴 **The isolation level is load-bearing.** Verified against PostgreSQL 17.9:
under REPEATABLE READ *and* SERIALIZABLE this insert raises
``40001: could not serialize access due to concurrent update`` whenever the
conflicting row committed after the transaction's snapshot. It fails *only*
under real concurrency and passes every single-threaded test — which is the
worst possible failure profile for the one statement the whole gate rests on.
READ COMMITTED, deliberately, and asserted below rather than assumed.

🔴 **Two statements, never the single-statement CTE.** The popular
"insert-or-select" CTE was demonstrated returning zero rows for a row that
existed: the INSERT branch is suppressed by the conflict while the SELECT branch
runs against the statement-start snapshot, which predates the concurrent commit.
``SELECT … FOR UPDATE`` then insert is worse still — there is no row to lock.

The two-statement form is safe because the losing insert *blocks* until the
winner commits or rolls back; the follow-up ``SELECT`` then takes a fresh
snapshot and is guaranteed to see the committed row. The only gap is the winner
rolling back between our two statements, which :data:`CLAIM_RETRIES` covers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import ProviderKind

#: Bounded, and small. Each iteration only happens when a competing claimant
#: rolled back, which is rare and not self-sustaining — an unbounded loop here
#: would turn a rare race into a hang.
CLAIM_RETRIES = 3

_INSERT = text(
    """
    INSERT INTO sends (user_id, draft_id, connection_id, provider, idempotency_key,
                       confirmed_sha256)
    VALUES (:user_id, :draft_id, :connection_id, CAST(:provider AS provider_kind),
            :idempotency_key, :confirmed_sha256)
    ON CONFLICT ON CONSTRAINT sends_user_key_uniq DO NOTHING
    RETURNING *
    """
)

_SELECT = text(
    "SELECT * FROM sends WHERE user_id = :user_id AND idempotency_key = :idempotency_key"
)


@dataclass(frozen=True)
class ClaimResult:
    row: dict[str, Any]
    #: True when *this* caller created the row and therefore owns dispatching it.
    won: bool


class ClaimContention(RuntimeError):
    """Every retry saw a conflict and then no row. Vanishingly unlikely, and a
    loud error rather than a silent second insert attempt."""


async def assert_read_committed(session: AsyncSession) -> None:
    """Fail fast if someone raises the isolation level.

    A comment saying "must be READ COMMITTED" is a comment. This is a check —
    and it is worth its two milliseconds because the failure it guards against
    appears only under concurrent load, in production, as a random 500.
    """
    level = await session.scalar(text("SELECT current_setting('transaction_isolation')"))
    if level != "read committed":
        raise RuntimeError(
            f"the send claim requires READ COMMITTED, got {level!r}: "
            "ON CONFLICT DO NOTHING raises 40001 above it (risks.md R22)"
        )


async def claim_send(
    session: AsyncSession,
    *,
    user_id: int,
    draft_id: uuid.UUID,
    connection_id: int,
    provider: ProviderKind,
    idempotency_key: str,
    confirmed_sha256: str,
) -> ClaimResult:
    """Conditionally insert, then read back on conflict.

    The caller commits. That ordering matters as much as the statement does:
    the ``in_flight`` row is committed **before** the provider is contacted, so
    a duplicate arriving mid-flight has something truthful to be told rather
    than a conflict to be reported (design D5, and why we return 200 where
    Stripe returns 409).
    """
    await assert_read_committed(session)
    params = {
        "user_id": user_id,
        "draft_id": draft_id,
        "connection_id": connection_id,
        "provider": provider.value,
        "idempotency_key": idempotency_key,
        "confirmed_sha256": confirmed_sha256,
    }

    for _ in range(CLAIM_RETRIES):
        inserted = (await session.execute(_INSERT, params)).mappings().first()
        if inserted is not None:
            return ClaimResult(row=dict(inserted), won=True)

        existing = (
            await session.execute(
                _SELECT, {"user_id": user_id, "idempotency_key": idempotency_key}
            )
        ).mappings().first()
        if existing is not None:
            return ClaimResult(row=dict(existing), won=False)

        # The winner rolled back between our two statements, so the conflict is
        # gone and there is nothing to read. Try to claim it ourselves.

    raise ClaimContention(
        f"could not claim or read the send for key {idempotency_key!r} "
        f"after {CLAIM_RETRIES} attempts"
    )


async def load_send_by_key(
    session: AsyncSession, *, user_id: int, idempotency_key: str
) -> dict[str, Any] | None:
    """The cheap pre-check before claiming.

    Not a substitute for the conditional insert — it is racy on its own and is
    never treated as authoritative. It exists so a duplicate that arrives long
    after the fact is answered with a read instead of an insert that would have
    conflicted anyway.
    """
    row = (
        await session.execute(
            _SELECT, {"user_id": user_id, "idempotency_key": idempotency_key}
        )
    ).mappings().first()
    return dict(row) if row is not None else None


async def load_send(
    session: AsyncSession, send_id: uuid.UUID, *, user_id: int | None = None
) -> dict[str, Any] | None:
    sql = "SELECT * FROM sends WHERE id = :id"
    params: dict[str, Any] = {"id": send_id}
    if user_id is not None:
        # Cross-user access is a 404, not a 403: a 403 discloses that the row
        # exists (api-design.md, Conventions).
        sql += " AND user_id = :user_id"
        params["user_id"] = user_id
    row = (await session.execute(text(sql), params)).mappings().first()
    return dict(row) if row is not None else None
