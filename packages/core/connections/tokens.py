"""Silent refresh, and the callable an adapter is handed (tasks 6.7-6.8, R11).

This is the module ``orchestrator``'s ``get_token=`` points at, and the reason
that parameter is a *callable* rather than a value: the refresh has to happen at
the moment of use, inside the advisory lock — never eagerly in the fan-out,
which would refresh every connection on every search.

🔴 **Re-read the connection after acquiring the lock.** The lock alone does not
collapse a stampede, it only serializes it: without the re-read, every worker
queued behind the winner proceeds to refresh again against a token that is
already fresh. With a rotating provider that is worse than wasteful — RFC 9700
§4.14 says an authorization server detecting refresh-token replay *should revoke
the entire token family*, so our own concurrency can look exactly like a stolen
token and get the user's whole grant killed (R11).

``pg_advisory_xact_lock`` rather than the session-scoped variant: a preempted
container aborts its transaction and the lock evaporates, whereas a
session-scoped lock leaks until the pooled connection is reaped.

**Accepted cost:** the lock holds a Postgres connection across an outbound HTTP
call. That is why :func:`core.http.provider_client` has a short timeout and the
pool has ``max_overflow=0`` — starvation fails loudly rather than silently.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from core.adapters.types import TokenKind, TokenUnavailable
from core.connections import oauth, store
from core.db import session_scope
from core.enums import ErrorClass
from core.errors import NeedsReconnect, ProviderError, classify

logger = logging.getLogger("core.connections.tokens")

#: Refresh when the access token is within this of expiry. Wide enough that a
#: token cannot expire *during* a call we already started.
REFRESH_MARGIN_SECONDS = 120

#: Which encrypted column answers which kind of request, per provider. Data
#: rather than branches because getting it wrong is silent: Slack's
#: `search.messages` sent a bot token returns `not_allowed_token_type`, which
#: reads as a scope problem rather than a routing one (R4).
_COLUMN_FOR: dict[tuple[str, TokenKind], str] = {
    ("gmail", TokenKind.OAUTH): "access_token_ct",
    ("gmail", TokenKind.USER): "access_token_ct",
    ("slack", TokenKind.USER): "access_token_ct",
    ("slack", TokenKind.BOT): "bot_token_ct",
    ("slack", TokenKind.OAUTH): "bot_token_ct",
}


#: Re-exported so callers reach for one name. Defined in ``core.errors`` because
#: :func:`classify` — the single classification boundary — has to recognise it,
#: and ``errors`` cannot import this module without a cycle. Distinct from a
#: ``config`` failure, which is *our* bug: rendering a reconnect prompt for a
#: rotated client secret sends the user in circles fixing a grant that was never
#: broken (R24).
__all__ = ["REFRESH_MARGIN_SECONDS", "NeedsReconnect", "get_token", "token_getter"]


async def get_token(*, connection_id: int, kind: TokenKind) -> str:
    """The one path to a live credential.

    Everything happens inside a single transaction that holds
    ``pg_advisory_xact_lock(connection_id)``: read, decide, refresh, persist,
    commit. The lock is released by the commit, so a caller cannot forget it.
    """
    async with session_scope() as session:
        # Transaction-scoped. Taken *before* the read, so the read below is the
        # authoritative one rather than a racing snapshot.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:id)"), {"id": connection_id}
        )

        # 🔴 The re-read. Workers queued behind the winner observe the
        # already-refreshed token here and return it, which is what turns the
        # lock from a queue into a stampede eliminator.
        connection = await store.by_id(session, connection_id)
        if connection is None:
            raise TokenUnavailable(f"connection {connection_id} no longer exists")

        column = _COLUMN_FOR.get((connection.provider, kind))
        if column is None:
            raise TokenUnavailable(
                f"{connection.provider} has no {kind.value} token; "
                "this is a routing bug rather than a missing grant"
            )

        stored = connection.token(column)

        # Slack tokens do not expire and we deliberately left rotation off, so
        # a stored token is simply the answer. Google's access token is a cache
        # in front of the refresh token and may be absent or stale.
        if column != "access_token_ct" or connection.provider != "gmail":
            if stored:
                return stored
            raise _reconnect(
                connection_id,
                f"no {kind.value} token stored for connection {connection_id}",
            )

        if stored and not _expiring(connection.access_expires_at):
            return stored

        return await _refresh_google(session, connection)


async def expire_access_token(connection_id: int) -> None:
    """Mark the cached access token stale so the next read refreshes it.

    The bottom rung of the Gmail 401 ladder (task 6.3c). A ``401`` says only
    that the *access token* is no longer accepted — usually because it expired
    early or was invalidated server-side — and the correct response is to
    refresh and retry once, escalating only if the **refresh** comes back
    ``invalid_grant``. Treating a raw 401 as a dead grant disconnects healthy
    users constantly (R24).

    Expressed as "expire the cache" rather than "force a refresh" so it composes
    with the advisory lock: the next :func:`get_token` still takes the lock,
    re-reads, and may well find that a concurrent worker has already refreshed —
    in which case no second refresh happens at all.
    """
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE connections
                   SET access_expires_at = now() - interval '1 second',
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": connection_id},
        )
        await session.commit()


def token_getter(connection_id: int | None) -> Callable[[TokenKind], Awaitable[str]]:
    """Bind a connection id into the callable ``AdapterContext`` carries.

    A source with no connection gets a callable that raises rather than one that
    returns ``None`` — an adapter reaching for a credential it was never given
    is a bug, and it should say so where it happens rather than three frames
    later as a 401.
    """

    async def getter(kind: TokenKind) -> str:
        if connection_id is None:
            raise TokenUnavailable(
                f"this source has no connection, so it has no {kind.value} token"
            )
        return await get_token(connection_id=connection_id, kind=kind)

    return getter


async def _refresh_google(session, connection: store.ConnectionRow) -> str:  # type: ignore[no-untyped-def]
    refresh_token = connection.token("refresh_token_ct")
    if not refresh_token:
        raise _reconnect(
            connection.id,
            "no refresh token is stored for this connection, so the access "
            "token cannot be renewed without the account owner",
        )

    try:
        grant = await oauth.refresh_access_token(oauth.GOOGLE, refresh_token)
    except ProviderError as exc:
        error_class = classify(exc, provider="google")
        if error_class is ErrorClass.NEEDS_RECONNECT:
            # `invalid_grant` — the only reliable revocation signal Google
            # emits, collapsing eight distinct causes. Never retried.
            await store.mark_needs_reconnect(session, connection.id, str(exc))
            await session.commit()
            raise NeedsReconnect(connection.id, str(exc)) from exc
        # `config` (invalid_client, deleted_client, …) and transient failures
        # both propagate untouched: neither is the user's to fix, and marking
        # the connection would tell them it was.
        raise

    # 🔴 Persisted immediately on receipt, before any other work and even if
    # that work then fails. The window between a provider issuing a credential
    # and us committing it cannot be made atomic, only narrowed (R11, 6.7b).
    #
    # `grant.refresh_token` is None on an ordinary Google refresh, and
    # `persist_tokens` treats None as "keep what you have" — the single line
    # that stops a working grant being nulled (R21).
    await store.persist_tokens(
        session, connection_id=connection.id, provider=connection.provider, grant=grant
    )
    await session.commit()

    if not grant.access_token:
        raise _reconnect(connection.id, "the refresh response carried no access token")
    logger.info("refreshed the access token for connection %s", connection.id)
    return grant.access_token


def _expiring(expires_at: datetime | None) -> bool:
    if expires_at is None:
        # No recorded expiry means we cannot prove it is live, so treat it as
        # due. A needless refresh is cheap; using a dead token is a 401 storm.
        return True
    return expires_at <= datetime.now(UTC) + timedelta(seconds=REFRESH_MARGIN_SECONDS)


def _reconnect(connection_id: int, detail: str) -> NeedsReconnect:
    return NeedsReconnect(connection_id, detail)
