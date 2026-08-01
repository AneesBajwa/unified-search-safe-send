"""Connections, OAuth, and silent refresh (openspec tasks 6.3-6.13).

Every provider call is mocked with ``respx``, so this asserts at the socket:
what we *sent* to Google and Slack, and how many times. Two of these are
regression tests for bugs that are famous precisely because they are invisible
until a user's connection is already broken — the nulled refresh token (R21) and
the refresh stampede (R11).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest
import respx
from conftest import make_user
from core.adapters.types import TokenKind
from core.connections import oauth, service, state, store, tokens
from core.db import session_scope
from core.enums import ErrorClass
from core.errors import NeedsReconnect, ProviderError, classify

pytestmark = pytest.mark.usefixtures("clean_db")

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
SLACK_TOKEN = "https://slack.com/api/oauth.v2.access"

# `sub` = 1234567890, email = dana@acme.test. Unsigned — see `_id_token_claims`.
ID_TOKEN = (
    "eyJhbGciOiJSUzI1NiJ9."
    "eyJzdWIiOiAiMTIzNDU2Nzg5MCIsICJlbWFpbCI6ICJkYW5hQGFjbWUudGVzdCJ9."
    "signature-not-verified-for-a-token-from-the-token-endpoint"
)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from core.config import get_settings

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-google-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-test-secret")
    monkeypatch.setenv("SLACK_CLIENT_ID", "test-slack-client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "slack-test-secret")
    monkeypatch.setenv("OAUTH_TUNNEL_URL", "https://tunnel.test")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def _connect_google(user_id: int, *, refresh: str | None = "1//0-original") -> int:
    grant = oauth.Grant(
        external_account_id="1234567890",
        display_name="dana@acme.test",
        granted_scopes=("openid", "https://www.googleapis.com/auth/gmail.readonly"),
        access_token="ya29.original-access",
        refresh_token=refresh,
        access_expires_at=None,
    )
    async with session_scope() as session:
        row = await store.upsert(
            session, user_id=user_id, provider="gmail", grant=grant
        )
        await session.commit()
    return row.id


# ---------------------------------------------------------------------------
# Authorize URLs — task 6.3, 6.4, R18, R21
# ---------------------------------------------------------------------------


def test_google_requests_offline_access_and_the_two_gmail_scopes(configured: None) -> None:
    url = oauth.authorize_url(oauth.GOOGLE, state="signed", force_consent=False)
    assert "access_type=offline" in url  # without it there is no refresh token at all
    assert "gmail.readonly" in url
    # 🔴 `compose`, not `send`: draft-then-send is the idempotency mechanism (R3).
    assert "gmail.compose" in url
    assert "gmail.send" not in url
    assert "mail.google.com" not in url  # never the full-access scope


def test_consent_is_not_forced_on_an_ordinary_login(configured: None) -> None:
    """🔴 R21. Google mints a *new* refresh token every time consent is granted
    and silently evicts the oldest past 100 per account — so re-prompting on
    every login eventually breaks our own oldest connections, with no error
    surfaced anywhere."""
    assert "prompt=consent" not in oauth.authorize_url(
        oauth.GOOGLE, state="s", force_consent=False
    )
    assert "prompt=consent" in oauth.authorize_url(
        oauth.GOOGLE, state="s", force_consent=True
    )


def test_slack_puts_search_read_in_user_scope(configured: None) -> None:
    """🔴 R4. In `scope` it lands on the bot token and `search.messages` returns
    `not_allowed_token_type` — at runtime, never at config time."""
    url = oauth.authorize_url(oauth.SLACK, state="s", force_consent=False)
    assert "user_scope=search%3Aread" in url
    bot_scopes = url.split("scope=")[1].split("&")[0]
    assert "search%3Aread" not in bot_scopes
    assert "chat%3Awrite.public" in url  # kills most not_in_channel


def test_slack_refuses_an_http_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """R18 — caught with a sentence here rather than as `bad_redirect_uri` in a
    browser tab thirty seconds later."""
    from core.config import get_settings

    monkeypatch.setenv("SLACK_CLIENT_ID", "id")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OAUTH_TUNNEL_URL", "")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8080")
    get_settings.cache_clear()
    try:
        with pytest.raises(oauth.OAuthConfigError, match="HTTPS tunnel"):
            oauth.authorize_url(oauth.SLACK, state="s", force_consent=False)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# State signing — task 6.5
# ---------------------------------------------------------------------------


def test_state_round_trips_and_carries_the_user() -> None:
    signed = state.sign(state.new_state(user_id=42, provider="gmail"))
    assert state.verify(signed).user_id == 42


def test_a_forged_state_is_refused() -> None:
    """The callback is unauthenticated by necessity — a provider redirects a
    browser to it — so the user id has to come out of the signature. Read from a
    query parameter it would mean "connect this account to any user you like"."""
    signed = state.sign(state.new_state(user_id=42, provider="gmail"))
    body, _, signature = signed.partition(".")
    forged = state.sign(state.new_state(user_id=99, provider="gmail")).split(".")[0]
    with pytest.raises(state.StateInvalid):
        state.verify(f"{forged}.{signature}")
    with pytest.raises(state.StateInvalid):
        state.verify(f"{body}.{'A' * len(signature)}")


def test_an_expired_state_is_refused() -> None:
    from datetime import UTC, datetime, timedelta

    signed = state.sign(state.new_state(user_id=1, provider="gmail"))
    later = datetime.now(UTC) + timedelta(seconds=state.STATE_TTL_SECONDS + 60)
    with pytest.raises(state.StateInvalid, match="too long"):
        state.verify(signed, now=later)


# ---------------------------------------------------------------------------
# Storage — tasks 6.6, 6.10, 6.11
# ---------------------------------------------------------------------------


async def test_tokens_are_encrypted_at_rest() -> None:
    user_id = await make_user()
    connection_id = await _connect_google(user_id)

    async with session_scope() as session:
        from sqlalchemy import text

        raw = (
            await session.execute(
                text("SELECT refresh_token_ct FROM connections WHERE id = :id"),
                {"id": connection_id},
            )
        ).scalar_one()

    assert b"1//0-original" not in raw
    async with session_scope() as session:
        row = await store.by_id(session, connection_id)
    assert row is not None
    assert row.token("refresh_token_ct") == "1//0-original"


async def test_reconnect_updates_the_same_row() -> None:
    """Design D8 — identity survives, because every draft and send references
    ``connection.id`` and those references must keep meaning what they meant."""
    user_id = await make_user()
    first = await _connect_google(user_id)
    again = await _connect_google(user_id, refresh="1//0-re-granted")
    assert again == first

    async with session_scope() as session:
        row = await store.by_id(session, first)
    assert row is not None
    assert row.token("refresh_token_ct") == "1//0-re-granted"


async def test_a_reconnect_to_a_different_account_is_refused() -> None:
    """Never silently rebound: repointing the row at a different mailbox would
    rewrite the meaning of every historical row that references it."""
    user_id = await make_user()
    connection_id = await _connect_google(user_id)

    other = oauth.Grant(
        external_account_id="9999999999",
        display_name="someone.else@acme.test",
        access_token="ya29.other",
        refresh_token="1//0-other",
    )
    async with session_scope() as session:
        with pytest.raises(store.ReconnectMismatch):
            await store.upsert(
                session,
                user_id=user_id,
                provider="gmail",
                grant=other,
                reconnecting_id=connection_id,
            )


async def test_disconnect_drops_tokens_and_keeps_the_row() -> None:
    user_id = await make_user()
    connection_id = await _connect_google(user_id)
    assert await service.disconnect(user_id=user_id, connection_id=connection_id)

    async with session_scope() as session:
        row = await store.by_id(session, connection_id)
    assert row is not None  # history still references it
    assert row.refresh_token_ct is None
    assert row.status == "needs_reconnect"  # no longer participates in search


# ---------------------------------------------------------------------------
# Refresh — tasks 6.7-6.8, R11, R21
# ---------------------------------------------------------------------------


@respx.mock
async def test_a_refresh_response_without_a_refresh_token_keeps_the_stored_one(
    configured: None,
) -> None:
    """🔴 R21, task 6.7d. The single most common Google OAuth bug.

    Google returns a refresh token **only on first authorization** and omits it
    on every ordinary refresh. ``token = body.get("refresh_token")`` therefore
    nulls a working credential and permanently breaks the connection.
    """
    user_id = await make_user()
    connection_id = await _connect_google(user_id)

    respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(
            200,
            # Note: no `refresh_token` key at all. This is the ordinary shape.
            json={"access_token": "ya29.refreshed", "expires_in": 3599},
        )
    )

    token = await tokens.get_token(connection_id=connection_id, kind=TokenKind.OAUTH)
    assert token == "ya29.refreshed"

    async with session_scope() as session:
        row = await store.by_id(session, connection_id)
    assert row is not None
    assert row.token("refresh_token_ct") == "1//0-original", (
        "the stored refresh token was destroyed by a response that simply "
        "omitted it — this permanently breaks the connection (risks.md R21)"
    )


@respx.mock
async def test_a_concurrent_fan_out_issues_exactly_one_refresh(configured: None) -> None:
    """🔴 R11, task 6.8. The advisory lock alone only *serializes* a stampede.

    What eliminates it is re-reading the connection after acquiring the lock, so
    workers queued behind the winner observe the already-refreshed token. With a
    rotating provider a stampede is not merely wasteful: RFC 9700 §4.14 says an
    authorization server detecting refresh-token replay should revoke the whole
    token family, so our own concurrency can get a user's grant killed.
    """
    user_id = await make_user()
    connection_id = await _connect_google(user_id)

    route = respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ya29.refreshed-once", "expires_in": 3599}
        )
    )

    results = await asyncio.gather(
        *(
            tokens.get_token(connection_id=connection_id, kind=TokenKind.OAUTH)
            for _ in range(6)
        )
    )

    assert results == ["ya29.refreshed-once"] * 6
    assert route.call_count == 1, (
        f"{route.call_count} refreshes left the process for one connection; the "
        "lock is serializing the stampede rather than eliminating it — the "
        "re-read after acquiring it is missing (risks.md R11)"
    )


@respx.mock
async def test_a_revoked_grant_becomes_needs_reconnect(configured: None) -> None:
    """`invalid_grant` is the only reliable revocation signal Google emits, and
    it collapses eight distinct causes. Never retried."""
    user_id = await make_user()
    connection_id = await _connect_google(user_id)

    respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
        )
    )

    with pytest.raises(NeedsReconnect):
        await tokens.get_token(connection_id=connection_id, kind=TokenKind.OAUTH)

    async with session_scope() as session:
        row = await store.by_id(session, connection_id)
    assert row is not None
    assert row.status == "needs_reconnect"


@respx.mock
async def test_a_config_error_does_not_tell_the_user_to_reconnect(
    configured: None,
) -> None:
    """🔴 R24. `invalid_client` is terminal but it is **our** bug — a rotated
    client secret, say. Rendering "reconnect your account" sends the user round
    in circles fixing a grant that was never broken."""
    user_id = await make_user()
    connection_id = await _connect_google(user_id)

    respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(400, json={"error": "invalid_client"})
    )

    with pytest.raises(ProviderError) as caught:
        await tokens.get_token(connection_id=connection_id, kind=TokenKind.OAUTH)
    assert classify(caught.value, provider="google") is ErrorClass.CONFIG

    async with session_scope() as session:
        row = await store.by_id(session, connection_id)
    assert row is not None
    assert row.status == "active", "a config error must not mark the grant broken"


async def test_the_invalidation_helper_makes_revocation_reproducible(
    configured: None,
) -> None:
    """Task 6.12 — so the revoked-grant round trip can be demonstrated twice in
    a row without a trip through Google's account settings."""
    user_id = await make_user()
    connection_id = await _connect_google(user_id)
    await service.invalidate_stored_token(connection_id)

    with respx.mock:
        respx.post(GOOGLE_TOKEN).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        with pytest.raises(NeedsReconnect):
            await tokens.get_token(connection_id=connection_id, kind=TokenKind.OAUTH)


# ---------------------------------------------------------------------------
# Token routing — task 6.4b, R4
# ---------------------------------------------------------------------------


async def test_slack_search_and_posting_use_different_tokens() -> None:
    """🔴 R4. Search needs the user token; everything else needs the bot token.
    Getting it wrong is silent at config time and only fails at runtime."""
    user_id = await make_user()
    grant = oauth.Grant(
        external_account_id="T123:U456",
        display_name="Acme HQ",
        granted_scopes=("chat:write", "search:read"),
        access_token="xoxp-user-token-value",
        bot_token="xoxb-bot-token-value",
    )
    async with session_scope() as session:
        row = await store.upsert(session, user_id=user_id, provider="slack", grant=grant)
        await session.commit()

    assert (
        await tokens.get_token(connection_id=row.id, kind=TokenKind.USER)
    ) == "xoxp-user-token-value"
    assert (
        await tokens.get_token(connection_id=row.id, kind=TokenKind.BOT)
    ) == "xoxb-bot-token-value"


# ---------------------------------------------------------------------------
# Exchange — task 6.3e
# ---------------------------------------------------------------------------


@respx.mock
async def test_the_google_exchange_reads_identity_from_the_id_token(
    configured: None,
) -> None:
    respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ya29.new",
                "refresh_token": "1//0-new",
                "expires_in": 3599,
                "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
                "id_token": ID_TOKEN,
            },
        )
    )
    grant = await oauth.exchange_code(oauth.GOOGLE, "auth-code")
    # `sub`, not email: email can change and is not unique over time.
    assert grant.external_account_id == "1234567890"
    assert grant.display_name == "dana@acme.test"
    assert grant.access_expires_at is not None


@respx.mock
async def test_the_slack_exchange_captures_both_tokens(configured: None) -> None:
    respx.post(SLACK_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-bot",
                "scope": "chat:write,channels:read",
                "team": {"id": "T0001", "name": "Acme HQ"},
                "authed_user": {
                    "id": "U0001",
                    "scope": "search:read",
                    "access_token": "xoxp-user",
                },
            },
        )
    )
    grant = await oauth.exchange_code(oauth.SLACK, "auth-code")
    assert grant.external_account_id == "T0001:U0001"
    assert grant.access_token == "xoxp-user"  # search uses this one
    assert grant.bot_token == "xoxb-bot"
    assert "search:read" in grant.granted_scopes  # stored for the pre-flight check


# ---------------------------------------------------------------------------
# The revoked-grant round trip — task 6.13, success criterion 5
# ---------------------------------------------------------------------------


@respx.mock
async def test_a_revoked_grant_surfaces_on_search_and_survives_a_reconnect(
    configured: None,
) -> None:
    """🔴 Task 6.13 — the whole round trip, which is the demo's best moment.

    Revoke → the *search run* reports ``needs_reconnect`` rather than a generic
    failure → it is **not** auto-retried → reconnect → the connection id is
    unchanged, so every draft, send and historical run still points at the right
    row → the same operation now succeeds.

    Driven through the real job runtime rather than by calling the adapter, so
    what is asserted is the status a customer would actually see.
    """
    from core.adapters import orchestrator
    from core.jobs import runtime

    user_id = await make_user()
    connection_id = await _connect_google(user_id)

    # 1. The grant dies. Every refresh from here answers invalid_grant.
    revoked = respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    await service.invalidate_stored_token(connection_id)

    async with session_scope() as session:
        plan = await orchestrator.plan_search(
            session, user_id=user_id, query="invoice 4417", sources=["fault:reconnect"]
        )
        await session.commit()

    while (await runtime.run_once(limit=5)).claimed:
        pass

    snapshot = await orchestrator.load_snapshot(search_id=plan.search_id, user_id=user_id)
    assert snapshot is not None
    run = snapshot.sources[0]
    assert run["status"] == "needs_reconnect", (
        f"a revoked grant surfaced as {run['status']!r}; it must be distinct from "
        "a generic failure or the UI cannot offer the one action that fixes it"
    )
    assert run["error_class"] == "needs_reconnect"
    # Terminal: the search is finished rather than spinning on a retry that can
    # only ever fail the same way.
    assert snapshot.finished

    # 2. A send from the same connection refuses for the same reason, and never
    #    auto-retries — re-sending against a dead grant just burns attempts.
    with pytest.raises(NeedsReconnect):
        await tokens.get_token(connection_id=connection_id, kind=TokenKind.OAUTH)
    assert revoked.called

    # 3. Reconnect. The natural key matches, so this is an UPDATE.
    respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ya29.after-reconnect",
                "refresh_token": "1//0-re-granted",
                "expires_in": 3599,
                "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
                "id_token": ID_TOKEN,
            },
        )
    )
    signed = state.sign(
        state.new_state(user_id=user_id, provider="gmail", reconnect=True)
    )
    reconnected = await service.complete(code="fresh-code", raw_state=signed)

    assert reconnected.id == connection_id, (
        "the reconnect created a new connection; every draft, send and adapter "
        "run referencing the old id would now point at a dead row"
    )
    assert reconnected.status == "active"

    # 4. And the operation that failed now works.
    assert (
        await tokens.get_token(connection_id=connection_id, kind=TokenKind.OAUTH)
    ) == "ya29.after-reconnect"


# ---------------------------------------------------------------------------
# Connection health — the other half of `mark_needs_reconnect`
# ---------------------------------------------------------------------------


@pytest.fixture
def source_needing_a_connection() -> Iterator[str]:
    """A source named for a provider, so ``plan_search`` attaches that
    provider's connection to the run — which is the condition under test.

    Deliberately does not touch the token: what is being asserted is the
    bookkeeping that follows a successful run, not the credential path, and a
    fake that refreshed would put a second write on the row and blur which one
    did the work.
    """
    from datetime import UTC, datetime

    from core.adapters import registry
    from core.adapters.types import AdapterContext, Result
    from core.enums import SourceMode

    class HealthyGmail:
        source = "gmail"

        async def search(self, query: str, ctx: AdapterContext) -> list[Result]:
            return [
                Result(
                    source="gmail",
                    id="msg-1",
                    title=f"{query} — re: renewal",
                    snippet="The provider answered, so the credential works.",
                    url="https://mail.google.com/mail/u/0/#all/msg-1",
                    timestamp=datetime.now(UTC).isoformat(),
                )
            ]

    registry.register(
        "gmail", HealthyGmail, participates_by_default=False,
        mode=SourceMode.LIVE, requires_connection=True,
    )
    try:
        yield "gmail"
    finally:
        from core.adapters import live

        registry.clear()
        live.register_defaults()


async def _connection_health(connection_id: int) -> dict[str, object]:
    from sqlalchemy import text

    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT status::text AS status, last_success_at,
                           last_error_class::text AS last_error_class, last_error_detail
                      FROM connections WHERE id = :id
                    """
                ),
                {"id": connection_id},
            )
        ).mappings().first()
    assert row is not None
    return dict(row)


async def _record_past_failure(connection_id: int, status: str = "active") -> None:
    from sqlalchemy import text

    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE connections
                   SET status = CAST(:status AS conn_status),
                       last_error_class = 'transient',
                       last_error_detail = 'Gmail returned 503 at 04:11',
                       last_success_at = NULL
                 WHERE id = :id
                """
            ),
            {"id": connection_id, "status": status},
        )
        await session.commit()


async def _run_search_against(user_id: int, source: str) -> dict[str, object]:
    from core.adapters import orchestrator
    from core.jobs import runtime

    async with session_scope() as session:
        plan = await orchestrator.plan_search(
            session, user_id=user_id, query="renewal", sources=[source]
        )
        await session.commit()

    while (await runtime.run_once(limit=5)).claimed:
        pass

    snapshot = await orchestrator.load_snapshot(search_id=plan.search_id, user_id=user_id)
    assert snapshot is not None
    return dict(snapshot.sources[0])


async def test_a_successful_run_records_the_connection_as_healthy(
    source_needing_a_connection: str,
) -> None:
    """🔴 ``mark_needs_reconnect`` had no counterpart, and nothing called
    ``mark_success``.

    Found by hand, not by the suite: two live searches against a real Gmail
    mailbox and a real Slack workspace both returned results, and both
    connection rows still read ``last_success_at = NULL``. The whole suite was
    green while that was true, because every existing test asserts the *run*
    status and none of them looked at the row the connections page renders.

    Two separate lies came out of the same missing call. ``last_success_at``
    never moved, so a working connection displayed as never used; and nothing
    ever cleared ``last_error_detail``, so one transient 503 sat next to an
    `active` badge for the life of the connection.
    """
    user_id = await make_user()
    connection_id = await _connect_google(user_id)
    await _record_past_failure(connection_id)

    run = await _run_search_against(user_id, source_needing_a_connection)
    assert run["status"] == "done"
    assert run["connection_id"] == connection_id, (
        "the run carried no connection, so this asserts nothing — "
        "`requires_connection` or the source name is wrong"
    )

    health = await _connection_health(connection_id)
    assert health["last_success_at"] is not None, (
        "a provider accepted this credential and the connection still reports "
        "it has never succeeded"
    )
    assert health["last_error_detail"] is None, (
        "a stale error is still displayed next to a connection that just worked"
    )
    assert health["last_error_class"] is None
    assert health["status"] == "active"


async def test_a_revoked_connection_is_not_quietly_cleared_by_a_success() -> None:
    """The guard inside ``mark_success``: ``WHERE status <> 'needs_reconnect'``.

    Only a real reconnect clears a revoked grant — otherwise a success arriving
    from anywhere would flip the connection back to `active` and take away the
    one action that fixes it.

    Called **directly** rather than driven through a search, and the reason is
    worth stating: ``plan_search`` attaches only `active` connections, so a
    revoked one never reaches a run in the first place. A test that went the
    long way round would pass without the guard existing at all — it would be
    asserting the filter in ``_active_connections``, not this. Two independent
    defences, and this one covers the day a caller arrives from somewhere else.
    """
    user_id = await make_user()
    connection_id = await _connect_google(user_id)
    async with session_scope() as session:
        await store.mark_needs_reconnect(session, connection_id, "invalid_grant")
        await session.commit()

    await service.record_success(connection_id)

    health = await _connection_health(connection_id)
    assert health["status"] == "needs_reconnect", (
        "a success cleared a revoked grant; the user would lose the reconnect "
        "action while the grant is still dead"
    )
    assert health["last_success_at"] is None
    assert health["last_error_detail"] == "invalid_grant"


def test_a_grant_never_reprs_its_tokens() -> None:
    """Task 6.2b. This object lives for about four lines between a provider
    response and an encrypted column — and those four lines are exactly where a
    stray log line or a pydantic traceback would leak every token at once."""
    grant = oauth.Grant(
        external_account_id="1",
        display_name="d",
        access_token="ya29.super-secret",
        refresh_token="1//0-super-secret",
        bot_token="xoxb-super-secret",
    )
    assert "super-secret" not in repr(grant)
    assert "super-secret" not in str(grant)
    assert "«redacted»" in repr(grant)
