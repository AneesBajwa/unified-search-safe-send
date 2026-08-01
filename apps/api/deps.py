"""``X-API-Key`` authentication (openspec task 9.1 — the dependency half).

Built now rather than in group 9 because the send gate's central claim is
**user-scoped by construction**: ``sends_user_key_uniq`` is
``(user_id, idempotency_key)``. Testing exactly-once against a hardcoded dev
user would test a weaker property than the one that ships.

Three properties worth stating:

- **The checksum gates the database.** ``api_keys.parse`` rejects a malformed or
  mistyped key on arithmetic alone, so garbage traffic never reaches the pool.
- **Every auth failure is the same 401.** Wrong key, revoked key, expired key,
  unknown key — identical code and identical message. Distinguishing them is a
  free oracle for anyone probing.
- **``last_used_at`` is throttled to at most one write an hour.** Unthrottled, it
  turns every authenticated request into a write, which on a queue-heavy free
  tier is a real cost and a pointless one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from core.db import session_scope
from core.security import api_keys
from fastapi import Depends, Header
from sqlalchemy import text

from api.errors import ApiError

#: At most one `last_used_at` write per key per hour. Unthrottled, every
#: authenticated request becomes a write.
LAST_USED_THROTTLE_SECONDS = 3600


def _unauthorized() -> ApiError:
    """Identical for every auth failure — wrong, revoked, expired, unknown.

    A fresh instance per call rather than a module-level singleton: a raised
    exception keeps a reference to its traceback, and a shared one would pin
    request frames for the life of the process.
    """
    return ApiError("unauthorized", "a valid X-API-Key header is required")


@dataclass(frozen=True)
class Caller:
    user_id: int
    key_id: str


async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Caller:
    if not x_api_key:
        raise _unauthorized()
    key_id = api_keys.parse(x_api_key)
    if key_id is None:
        raise _unauthorized()

    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT user_id, key_hash, revoked_at, expires_at
                      FROM api_keys WHERE key_id = :key_id
                    """
                ),
                {"key_id": key_id},
            )
        ).first()
        if row is None or not api_keys.verify(x_api_key, row.key_hash):
            raise _unauthorized()
        if row.revoked_at is not None:
            raise _unauthorized()

        await session.execute(
            text(
                """
                UPDATE api_keys
                   SET last_used_at = now()
                 WHERE key_id = :key_id
                   AND (last_used_at IS NULL
                        OR last_used_at < now() - make_interval(secs => :throttle))
                """
            ),
            {"key_id": key_id, "throttle": LAST_USED_THROTTLE_SECONDS},
        )
        # Expiry is checked in application code, after the read, so an expired
        # key is indistinguishable from a wrong one to the caller while still
        # being visible to us in the row.
        expired = row.expires_at is not None and await session.scalar(
            text("SELECT :expires_at < now()"), {"expires_at": row.expires_at}
        )
        await session.commit()

    if expired:
        raise _unauthorized()
    return Caller(user_id=int(row.user_id), key_id=key_id)


CallerDep = Annotated[Caller, Depends(require_api_key)]
