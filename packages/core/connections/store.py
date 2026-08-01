"""Reading and writing ``connections``, with the token columns encrypted.

The natural key ``(user_id, provider, external_account_id)`` is what makes
reconnect an **UPDATE of the matched row** rather than an INSERT (design D8).
That is the whole of what "reconnect preserves identity" means: every draft,
send and adapter run references ``connection.id``, and they stay intact because
the row they point at is the same row.

🔴 **A reconnect that authorizes a *different* provider account is rejected**,
never silently rebound. Silently rebinding would leave every historical row
pointing at a connection that is now somebody else's mailbox.

🔴 **A missing token in a provider response means "keep what you have."** There
is no code path in this module that clears a refresh token from a response, only
from an explicit disconnect. Google returns a refresh token only on first
authorization and omits it on every ordinary refresh, so the naive
``refresh_token_ct = EXCLUDED.refresh_token_ct`` permanently breaks the
connection (R21).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.connections.oauth import Grant
from core.enums import ConnStatus, ErrorClass
from core.security import crypto

#: Column name -> the ``field`` component of the AAD. Kept as data so the AAD
#: for a column can never be computed two different ways in two places.
TOKEN_FIELDS = {
    "access_token_ct": "access",
    "refresh_token_ct": "refresh",
    "bot_token_ct": "bot",
}


#: The ``external_account_id`` the PoC sign-in writes for a provider that had no
#: OAuth client configured when the user signed in. It is a **sentinel, never a
#: provider account id** — the row it names has no tokens and can never reach
#: anything.
#:
#: It matters here because :class:`ReconnectMismatch` exists to stop a re-grant
#: silently repointing a *real* mailbox at a different one, and a placeholder is
#: not a mailbox. See :func:`upsert`.
PLACEHOLDER_PREFIX = "fake:"


def is_placeholder(external_account_id: str) -> bool:
    """True for a row that was never bound to a provider account."""
    return external_account_id.startswith(PLACEHOLDER_PREFIX)


class ReconnectMismatch(Exception):
    """The re-grant landed on a different provider account than the one being
    repaired. Carries both ids so the message can name them."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            "this reconnect authorized a different account "
            f"({actual!r}) than the connection being repaired ({expected!r}); "
            "connect it as a new connection instead"
        )
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class ConnectionRow:
    id: int
    user_id: int
    provider: str
    external_account_id: str
    display_name: str
    status: str
    granted_scopes: tuple[str, ...]
    access_expires_at: datetime | None
    access_token_ct: bytes | None
    refresh_token_ct: bytes | None
    bot_token_ct: bytes | None

    def token(self, column: str) -> str | None:
        """Decrypt one column, or ``None`` if it was never stored.

        An access token that fails to decrypt is a **cache miss** and returns
        ``None`` so the caller refreshes; a *refresh* token that fails to
        decrypt is a hard error, because there is nothing to fall back to and
        pretending otherwise would present a broken keyring as a revoked grant.
        """
        blob = getattr(self, column)
        if blob is None:
            return None
        aad = crypto.aad_for(
            connection_id=self.id, provider=self.provider, field=TOKEN_FIELDS[column]
        )
        if column == "access_token_ct":
            try:
                return crypto.decrypt(blob, aad=aad)
            except (crypto.EnvelopeError, crypto.KeyNotFound):
                return None
        return crypto.decrypt(blob, aad=aad)


_SELECT = """
SELECT id, user_id, provider::text AS provider, external_account_id, display_name,
       status::text AS status, granted_scopes, access_expires_at,
       access_token_ct, refresh_token_ct, bot_token_ct
  FROM connections
"""


def _row(mapping: Any) -> ConnectionRow:
    return ConnectionRow(
        id=int(mapping["id"]),
        user_id=int(mapping["user_id"]),
        provider=str(mapping["provider"]),
        external_account_id=str(mapping["external_account_id"]),
        display_name=str(mapping["display_name"]),
        status=str(mapping["status"]),
        granted_scopes=tuple(mapping["granted_scopes"] or ()),
        access_expires_at=mapping["access_expires_at"],
        access_token_ct=mapping["access_token_ct"],
        refresh_token_ct=mapping["refresh_token_ct"],
        bot_token_ct=mapping["bot_token_ct"],
    )


async def by_id(session: AsyncSession, connection_id: int) -> ConnectionRow | None:
    row = (
        await session.execute(text(_SELECT + " WHERE id = :id"), {"id": connection_id})
    ).mappings().first()
    return _row(row) if row is not None else None


async def by_natural_key(
    session: AsyncSession, *, user_id: int, provider: str, external_account_id: str
) -> ConnectionRow | None:
    row = (
        await session.execute(
            text(
                _SELECT
                + """ WHERE user_id = :user_id
                        AND provider = CAST(:provider AS provider_kind)
                        AND external_account_id = :external_account_id"""
            ),
            {
                "user_id": user_id,
                "provider": provider,
                "external_account_id": external_account_id,
            },
        )
    ).mappings().first()
    return _row(row) if row is not None else None


async def upsert(
    session: AsyncSession,
    *,
    user_id: int,
    provider: str,
    grant: Grant,
    reconnecting_id: int | None = None,
) -> ConnectionRow:
    """Land a completed authorization on the natural key (tasks 6.6, 6.10).

    Two statements rather than one, for a reason that is structural rather than
    stylistic: **the AAD binds each ciphertext to the row id**, and the row id
    does not exist until the row does. So the identity is upserted first, then
    the tokens are encrypted against the id it came back with. Both statements
    are in the caller's transaction, so no other session ever observes the
    intermediate state.
    """
    if reconnecting_id is not None:
        existing = await by_id(session, reconnecting_id)
        if existing is not None and existing.external_account_id != grant.external_account_id:
            if is_placeholder(existing.external_account_id):
                # 🔴 Adopted, not refused. The guard below protects a *real*
                # mailbox from being silently repointed; a placeholder has never
                # been bound to one, holds no tokens, and could never have
                # delivered anything — so there is no meaning to rewrite.
                #
                # Refusing here is a dead end, and it was on the default
                # sign-in path: the PoC identity carries placeholder rows from
                # before the OAuth clients existed, the console offers the
                # reconnect those rows advertise, and the callback answered
                # `reconnect_account_mismatch`. The one repair the product
                # promises always exists did not.
                await _adopt_placeholder(session, existing, grant.external_account_id)
            else:
                # Never silently rebound: every draft and send references
                # `connection.id`, and repointing it at a different mailbox would
                # rewrite the meaning of all of them.
                raise ReconnectMismatch(
                    existing.external_account_id, grant.external_account_id
                )

    row = (
        await session.execute(
            text(
                """
                INSERT INTO connections
                    (user_id, provider, external_account_id, display_name,
                     status, granted_scopes)
                VALUES (:user_id, CAST(:provider AS provider_kind), :external_account_id,
                        :display_name, 'active', CAST(:scopes AS text[]))
                ON CONFLICT ON CONSTRAINT connections_natural_key DO UPDATE
                   SET display_name = EXCLUDED.display_name,
                       status = 'active',
                       -- Stored so a scope added after consent is caught as a
                       -- pre-flight check rather than a runtime 403
                       -- insufficientPermissions (task 6.3b).
                       granted_scopes = EXCLUDED.granted_scopes,
                       last_error_class = NULL,
                       last_error_detail = NULL,
                       updated_at = now()
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "provider": provider,
                "external_account_id": grant.external_account_id,
                "display_name": grant.display_name or grant.external_account_id,
                "scopes": list(grant.granted_scopes),
            },
        )
    ).first()
    assert row is not None
    connection_id = int(row[0])

    await persist_tokens(session, connection_id=connection_id, provider=provider, grant=grant)
    refreshed = await by_id(session, connection_id)
    assert refreshed is not None
    return refreshed


async def _adopt_placeholder(
    session: AsyncSession, existing: ConnectionRow, external_account_id: str
) -> None:
    """Rebind a placeholder row onto the account that just authorized.

    Done as its own statement *before* the upsert so the natural-key conflict
    below lands on this row rather than inserting a second one — which is the
    point: `connection.id` stays stable, so the drafts and sends that reference
    it keep referring to something that now works.

    Skipped when the user already has a real connection for that account. Two
    rows may not share the natural key, and the honest outcome there is that the
    working row wins and the placeholder is left to be disconnected.
    """
    clash = (
        await session.execute(
            text(
                """
                SELECT id FROM connections
                 WHERE user_id = :user_id
                   AND provider = CAST(:provider AS provider_kind)
                   AND external_account_id = :external_account_id
                """
            ),
            {
                "user_id": existing.user_id,
                "provider": existing.provider,
                "external_account_id": external_account_id,
            },
        )
    ).first()
    if clash is not None:
        return

    await session.execute(
        text(
            """
            UPDATE connections
               SET external_account_id = :external_account_id, updated_at = now()
             WHERE id = :id
            """
        ),
        {"id": existing.id, "external_account_id": external_account_id},
    )


async def persist_tokens(
    session: AsyncSession, *, connection_id: int, provider: str, grant: Grant
) -> None:
    """Encrypt and store whatever the provider actually gave us.

    🔴 Every column is written with ``COALESCE(:new, existing)`` semantics: a
    ``None`` means the response omitted it, which means **keep the stored
    value**. There is deliberately no branch anywhere in this function that can
    write ``NULL`` over a credential (R21, task 6.7c).

    Called immediately on receipt and before any downstream work — the window
    between a provider issuing a rotated credential and us committing it cannot
    be made atomic, only narrowed (R11, task 6.7b).
    """
    updates: dict[str, Any] = {"id": connection_id}
    assignments: list[str] = []

    for column, value in (
        ("access_token_ct", grant.access_token),
        ("refresh_token_ct", grant.refresh_token),
        ("bot_token_ct", grant.bot_token),
    ):
        if value is None:
            continue
        aad = crypto.aad_for(
            connection_id=connection_id, provider=provider, field=TOKEN_FIELDS[column]
        )
        updates[column] = crypto.encrypt(value, aad=aad)
        assignments.append(f"{column} = :{column}")

    if grant.access_expires_at is not None:
        assignments.append("access_expires_at = :access_expires_at")
        updates["access_expires_at"] = grant.access_expires_at

    # The keyring version is recorded so rotation can select rows to re-encrypt
    # with a plain WHERE rather than trial-decrypting the table (task 6.1d).
    assignments.append("key_version = :key_version")
    updates["key_version"] = crypto.get_keyring().current
    assignments.append("updated_at = now()")

    # The only interpolated part is the SET list, and every fragment in it comes
    # from the literal column names above — never from a caller, never from a
    # provider response. Values ride as bound parameters, which is where the
    # untrusted data actually is.
    columns = ", ".join(assignments)
    statement = f"UPDATE connections SET {columns} WHERE id = :id"  # noqa: S608
    await session.execute(text(statement), updates)


async def mark_success(session: AsyncSession, connection_id: int) -> None:
    await session.execute(
        text(
            """
            UPDATE connections
               SET last_success_at = now(), status = 'active',
                   last_error_class = NULL, last_error_detail = NULL, updated_at = now()
             WHERE id = :id AND status <> 'needs_reconnect'
            """
        ),
        {"id": connection_id},
    )


async def mark_needs_reconnect(
    session: AsyncSession, connection_id: int, detail: str
) -> None:
    """The revocation landing (task 6.9).

    Set from the one classification boundary rather than from a status code, so
    ``invalid_grant`` / ``token_revoked`` / ``invalid_auth`` / ``account_inactive``
    all arrive here and nothing else does. A ``config`` error must **never**
    reach this function: it would tell the user to reconnect a grant that was
    never broken (R24).
    """
    await session.execute(
        text(
            """
            UPDATE connections
               SET status = 'needs_reconnect',
                   last_error_class = CAST(:error_class AS error_class),
                   last_error_detail = :detail,
                   updated_at = now()
             WHERE id = :id
            """
        ),
        {
            "id": connection_id,
            "error_class": ErrorClass.NEEDS_RECONNECT.value,
            "detail": detail[:16384],
        },
    )


async def disconnect(session: AsyncSession, *, connection_id: int, user_id: int) -> bool:
    """Drop the credentials, keep the history (task 6.11).

    The row survives because drafts, sends and adapter runs reference it — this
    is a *disconnect*, not a delete, and erasing the row would erase the record
    of everything that was sent through it.

    Status becomes ``needs_reconnect`` rather than a status of its own: the
    ``conn_status`` enum has no ``disconnected`` member, and adding one is a
    migration that buys a distinction nobody acts on differently — a
    disconnected account genuinely does need reconnecting before it can be used,
    and ``last_error_detail`` records that a person did it deliberately.
    """
    result = await session.execute(
        text(
            """
            UPDATE connections
               SET access_token_ct = NULL, refresh_token_ct = NULL, bot_token_ct = NULL,
                   access_expires_at = NULL,
                   status = 'needs_reconnect',
                   last_error_class = NULL,
                   last_error_detail = 'disconnected at the account owner''s request',
                   updated_at = now()
             WHERE id = :id AND user_id = :user_id
            """
        ),
        {"id": connection_id, "user_id": user_id},
    )
    # The rowcount is what distinguishes "disconnected" from "that connection
    # is not yours" — the UPDATE is scoped by user_id, so a mismatch simply
    # matches nothing rather than raising.
    return int(result.rowcount) == 1  # type: ignore[attr-defined]


async def active_for(
    session: AsyncSession, *, user_id: int, provider: str
) -> ConnectionRow | None:
    row = (
        await session.execute(
            text(
                _SELECT
                + """ WHERE user_id = :user_id
                        AND provider = CAST(:provider AS provider_kind)
                        AND status = CAST(:status AS conn_status)
                      ORDER BY id LIMIT 1"""
            ),
            {"user_id": user_id, "provider": provider, "status": ConnStatus.ACTIVE.value},
        )
    ).mappings().first()
    return _row(row) if row is not None else None
