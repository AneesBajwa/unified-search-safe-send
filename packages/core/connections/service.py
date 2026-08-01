"""Begin and complete an authorization (tasks 6.3, 6.5, 6.6, 6.10-6.12).

The API layer owns HTTP; this owns the decisions. Two of those decisions are
subtle enough to be worth naming up front.

**When to send `prompt=consent`.** Only when we hold no refresh token for the
account, or when we are repairing a broken grant. Never on an ordinary login:
Google mints a *new* refresh token each time consent is granted and silently
evicts the oldest once an account passes 100 for a client id — so re-prompting
every time eventually breaks our own oldest connections with no error surfaced
anywhere (R21).

**What a reconnect is allowed to change.** The display name and the tokens.
Never the identity: a re-grant landing on a different provider account is
rejected with a specific error rather than silently rebound, because every draft
and send references ``connection.id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.connections import oauth, state, store
from core.db import session_scope

logger = logging.getLogger("core.connections")

#: Shown on the connect screen, not only in the README (task 8.2d). Draft access
#: is the single most surprising thing on Google's consent screen, and an
#: unexplained scope is the most likely reason a reviewer abandons the flow.
CONSENT_RATIONALE: dict[str, str] = {
    "gmail": (
        "Draft access is the mechanism that guarantees we never send twice. "
        "Every message is created as a draft first, its id is recorded, and only "
        "then is it sent — so if this app crashes mid-send we can ask Gmail "
        "whether the draft still exists instead of guessing and sending again. "
        "Read access is what makes your mail searchable here. We never delete or "
        "modify mail you wrote."
    ),
    "slack": (
        "Search runs on your user token so it can see the channels you can see. "
        "Posting runs on a separate bot token, so a message from this app is "
        "always attributable to the app rather than to you."
    ),
}


@dataclass(frozen=True)
class Authorization:
    url: str
    provider: str
    rationale: str


async def begin(
    *,
    user_id: int,
    provider_name: str,
    return_to: str = "/connections",
    reconnect_connection_id: int | None = None,
) -> Authorization:
    provider = oauth.provider_for(provider_name)

    force_consent = await _should_force_consent(
        user_id=user_id, provider_name=provider_name, reconnecting=reconnect_connection_id
    )
    signed = state.sign(
        state.new_state(
            user_id=user_id,
            provider=provider_name,
            return_to=return_to,
            reconnect=reconnect_connection_id is not None,
        )
    )
    return Authorization(
        url=oauth.authorize_url(provider, state=signed, force_consent=force_consent),
        provider=provider_name,
        rationale=CONSENT_RATIONALE.get(provider_name, ""),
    )


async def complete(*, code: str, raw_state: str) -> store.ConnectionRow:
    """Exchange the code and land the connection. Raises on a bad state.

    The user id comes from the **signature**, never from a query parameter —
    otherwise the callback would read "connect this account to any user id you
    like" (task 6.5).
    """
    verified = state.verify(raw_state)
    provider = oauth.provider_for(verified.provider)
    grant = await oauth.exchange_code(provider, code)

    if not grant.external_account_id.strip(":"):
        raise oauth.OAuthConfigError(
            f"{provider.name} returned no account identifier, so this "
            "authorization cannot be bound to an account"
        )

    async with session_scope() as session:
        existing = await store.by_natural_key(
            session,
            user_id=verified.user_id,
            provider=provider.name,
            external_account_id=grant.external_account_id,
        )
        row = await store.upsert(
            session,
            user_id=verified.user_id,
            provider=provider.name,
            grant=grant,
            # Identity-preserving by construction: matched on the natural key,
            # so the id that comes back is the id that was already there.
            reconnecting_id=existing.id if existing is not None else None,
        )
        await session.commit()

    logger.info(
        "connection %s (%s) authorized with %d scopes",
        row.id,
        provider.name,
        len(row.granted_scopes),
    )
    return row


async def disconnect(*, user_id: int, connection_id: int) -> bool:
    async with session_scope() as session:
        removed = await store.disconnect(
            session, connection_id=connection_id, user_id=user_id
        )
        await session.commit()
    return removed


def reconnect_url(connection_id: int, provider: str) -> str:
    """What a ``needs_reconnect`` error hands back, so the fix is one click.

    Deliberately a path rather than a full authorize URL: minting the real URL
    signs a state bound to a user, and an error payload is rendered in contexts
    where that user is not necessarily the caller.
    """
    return f"/v1/connections/{provider}/authorize?reconnect={connection_id}"


async def _should_force_consent(
    *, user_id: int, provider_name: str, reconnecting: int | None
) -> bool:
    """`prompt=consent` is a repair tool, not a default (R21).

    True when repairing, and true when we hold no refresh token for this
    provider — which is the case on a genuine first connect and the case after
    a grant was revoked. False otherwise, so an ordinary re-login reuses the
    refresh token we already have rather than minting one more against the
    100-per-account cap.
    """
    if provider_name != oauth.GOOGLE.name:
        # Slack issues no refresh token (rotation is deliberately off), so the
        # parameter has nothing to buy there.
        return False
    if reconnecting is not None:
        return True
    async with session_scope() as session:
        current = await store.active_for(session, user_id=user_id, provider=provider_name)
    return current is None or current.refresh_token_ct is None


# ---------------------------------------------------------------------------
# Test / demo helper
# ---------------------------------------------------------------------------


#: A syntactically valid credential that no provider will ever honour. Shaped
#: like the real thing so it travels the real code path.
DEAD_TOKEN = {"gmail": "1//0-revoked-by-test-helper", "slack": "xoxp-revoked-by-test-helper"}


async def invalidate_stored_token(connection_id: int) -> None:
    """Break a connection's credential by hand (task 6.12).

    Makes the revoked-grant round trip **reproducible on demand** — which is
    what turns "watch it fail, then reconnect" from something requiring a trip
    through Google's account settings into something a demo can do twice in a
    row.

    It substitutes a *well-formed but dead* token, deliberately **not** a
    corrupted ciphertext. The distinction is the whole point: a corrupt envelope
    is a broken keyring, which is our bug and classifies ``permanent``, whereas
    a dead token is a revoked grant, which classifies ``needs_reconnect`` and is
    the state under test. Simulating the wrong one would exercise the wrong
    branch and pass anyway.

    The token is encrypted with the row's genuine AAD, so everything downstream
    — decrypt, refresh, ``invalid_grant``, ``mark_needs_reconnect`` — is the
    real code doing the real thing.
    """
    from sqlalchemy import text

    from core.security import crypto

    async with session_scope() as session:
        connection = await store.by_id(session, connection_id)
        if connection is None:
            raise LookupError(f"no connection {connection_id}")

        dead = DEAD_TOKEN.get(connection.provider, "revoked-by-test-helper")

        def poison(field: str) -> bytes:
            return crypto.encrypt(
                dead,
                aad=crypto.aad_for(
                    connection_id=connection_id, provider=connection.provider, field=field
                ),
            )

        params: dict[str, object] = {"id": connection_id}
        if connection.provider == "gmail":
            # The refresh token is the thing that has to be dead: the access
            # token is only a cache, so it is cleared rather than poisoned and
            # the next call is forced down the refresh path where Google
            # answers `invalid_grant`.
            statement = """
                UPDATE connections
                   SET refresh_token_ct = :blob,
                       access_token_ct = NULL,
                       access_expires_at = NULL,
                       updated_at = now()
                 WHERE id = :id
            """
            params["blob"] = poison("refresh")
        else:
            # Slack has no refresh token, so the stored tokens themselves are
            # the credentials; a dead one answers `invalid_auth` on first use.
            # Each column is encrypted under **its own** AAD field — reusing one
            # blob for both would fail authentication on decrypt instead, which
            # is a broken-keyring error rather than the revoked grant under test.
            statement = """
                UPDATE connections
                   SET access_token_ct = :user_blob,
                       bot_token_ct = :bot_blob,
                       access_expires_at = NULL,
                       updated_at = now()
                 WHERE id = :id
            """
            params["user_blob"] = poison("access")
            params["bot_blob"] = poison("bot")

        await session.execute(text(statement), params)
        await session.commit()
