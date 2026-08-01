"""The real send providers (openspec tasks 8.1-8.6).

The headline test is :func:`test_the_draft_id_is_durable_before_the_send`. It
asserts the ordering the whole Gmail crash story rests on:

    drafts.create  ->  **commit the draft id**  ->  drafts.send

Writing the id after delivery — which is where it used to be written — cannot
help a crash that happens *before* delivery. So the assertion is not "the id is
stored" but "the id is stored **and committed** at the moment the crash seam
fires", which is what recovery actually depends on.

Everything is ``respx``: the assertions are at the socket, which is the only
level at which a client-library retry is visible (R23).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
import respx
from conftest import make_user
from core.connections import oauth, store
from core.db import session_scope
from core.enums import ProviderKind
from core.send.gmail import GmailSendProvider
from core.send.providers import ProbeVerdict, SendRequest
from core.send.slack import SlackSendProvider
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("clean_db")

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SLACK_BASE = "https://slack.com/api"
KEY = "e3b0c44298fc1c149afbf4c8996fb924"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from core.config import get_settings

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-google-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-test")
    monkeypatch.setenv("SLACK_CLIENT_ID", "test-slack-client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "slack-test")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def _connection(provider: str) -> int:
    """A connection with live-looking tokens and an expiry far in the future, so
    nothing here accidentally exercises the refresh path."""
    from datetime import UTC, datetime, timedelta

    user_id = await make_user()
    grant = oauth.Grant(
        external_account_id="acct-1",
        display_name="dana@acme.test" if provider == "gmail" else "Acme HQ",
        access_token="ya29.live" if provider == "gmail" else "xoxp-live",
        refresh_token="1//0-live" if provider == "gmail" else None,
        bot_token=None if provider == "gmail" else "xoxb-live",
        access_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    async with session_scope() as session:
        row = await store.upsert(session, user_id=user_id, provider=provider, grant=grant)
        await session.commit()
    return row.id


def _request(connection_id: int, *, channel: ProviderKind, draft_id: str | None = None):
    return SendRequest(
        fingerprint=KEY,
        channel=channel,
        recipient="dana@acme.test" if channel is ProviderKind.GMAIL else "C024BE91L",
        recipient_display="dana@acme.test" if channel is ProviderKind.GMAIL else "#acme-renewal",
        subject="Re: renewal" if channel is ProviderKind.GMAIL else None,
        body="Confirming for Thursday.",
        provider_draft_id=draft_id,
        connection_id=connection_id,
    )


# ---------------------------------------------------------------------------
# Gmail — tasks 8.1, 8.2, 8.2b
# ---------------------------------------------------------------------------


@respx.mock
async def test_gmail_sends_via_a_draft_never_a_bare_message(configured: None) -> None:
    """🔴 Task 8.1, R3. `messages.send` has no idempotency token of any kind, so
    a crash after it leaves an unanswerable question. The draft id answers it."""
    connection_id = await _connection("gmail")
    create = respx.post(f"{GMAIL_BASE}/drafts").mock(
        return_value=httpx.Response(200, json={"id": "r-8891", "message": {"id": "m1"}})
    )
    send = respx.post(f"{GMAIL_BASE}/drafts/send").mock(
        return_value=httpx.Response(200, json={"id": "18f2b", "threadId": "t9"})
    )
    messages_send = respx.post(f"{GMAIL_BASE}/messages/send")

    provider = GmailSendProvider()
    request = _request(connection_id, channel=ProviderKind.GMAIL)
    prepared = await provider.prepare(request)
    receipt = await provider.send(
        _request(connection_id, channel=ProviderKind.GMAIL, draft_id=prepared.provider_draft_id)
    )

    assert prepared.provider_draft_id == "r-8891"
    assert receipt.provider_message_id == "18f2b"
    assert create.call_count == 1
    assert send.call_count == 1
    assert not messages_send.called, "a bare messages.send bypasses the idempotency mechanism"


@respx.mock
async def test_preparing_twice_reuses_the_draft(configured: None) -> None:
    """A retry after a crash must not create a second draft: it would litter the
    user's Drafts folder and — worse — move the id recovery asks about."""
    connection_id = await _connection("gmail")
    create = respx.post(f"{GMAIL_BASE}/drafts").mock(
        return_value=httpx.Response(200, json={"id": "r-new"})
    )
    prepared = await GmailSendProvider().prepare(
        _request(connection_id, channel=ProviderKind.GMAIL, draft_id="r-existing")
    )
    assert prepared.provider_draft_id == "r-existing"
    assert not create.called


@respx.mock
async def test_the_gmail_probe_reads_a_200_as_not_yet_sent(configured: None) -> None:
    """Task 8.2 — `200` means the draft still exists, so nothing went out and
    dispatching is safe."""
    connection_id = await _connection("gmail")
    respx.get(f"{GMAIL_BASE}/drafts/r-8891").mock(
        return_value=httpx.Response(200, json={"id": "r-8891"})
    )
    probe = await GmailSendProvider().probe_by_fingerprint(
        _request(connection_id, channel=ProviderKind.GMAIL, draft_id="r-8891")
    )
    assert probe.verdict is ProbeVerdict.ABSENT
    assert "never sent" in probe.evidence


@respx.mock
async def test_the_gmail_probe_reads_a_404_as_already_sent(configured: None) -> None:
    """`404` means Gmail deleted the draft when it sent it. FOUND, so the send
    is never dispatched a second time."""
    connection_id = await _connection("gmail")
    respx.get(f"{GMAIL_BASE}/drafts/r-8891").mock(
        return_value=httpx.Response(404, json={"error": {"code": 404}})
    )
    probe = await GmailSendProvider().probe_by_fingerprint(
        _request(connection_id, channel=ProviderKind.GMAIL, draft_id="r-8891")
    )
    assert probe.verdict is ProbeVerdict.FOUND
    # No message id recoverable from a draft id alone, so the send parks in
    # `uncertain` with a link to the user's own mailbox rather than claiming a
    # delivery it cannot evidence (task 8.2c).
    assert probe.provider_message_id is None
    assert "mail.google.com" in probe.evidence


@respx.mock
async def test_a_failed_gmail_probe_is_inconclusive_never_absent(
    configured: None,
) -> None:
    """🔴 The asymmetry that matters. ABSENT means "safe to dispatch", so
    reporting it without evidence is what double-sends."""
    connection_id = await _connection("gmail")
    respx.get(f"{GMAIL_BASE}/drafts/r-8891").mock(
        return_value=httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
    )
    probe = await GmailSendProvider().probe_by_fingerprint(
        _request(connection_id, channel=ProviderKind.GMAIL, draft_id="r-8891")
    )
    assert probe.verdict is ProbeVerdict.INCONCLUSIVE


@respx.mock
async def test_a_permanently_failed_send_deletes_its_draft(configured: None) -> None:
    """Task 8.2b — the user's Drafts folder is never littered with our failures.
    Only ever an id we stored; drafts the user wrote are never touched."""
    connection_id = await _connection("gmail")
    delete = respx.delete(f"{GMAIL_BASE}/drafts/r-8891").mock(
        return_value=httpx.Response(204)
    )
    await GmailSendProvider().discard(
        _request(connection_id, channel=ProviderKind.GMAIL, draft_id="r-8891")
    )
    assert delete.call_count == 1


# ---------------------------------------------------------------------------
# Slack — tasks 8.3, 8.4
# ---------------------------------------------------------------------------


@respx.mock
async def test_slack_stamps_the_idempotency_key_in_metadata(configured: None) -> None:
    """Task 8.3. `chat.postMessage` has no idempotency parameter of any kind, so
    `metadata.event_payload` is the only carrier that survives a round trip."""
    connection_id = await _connection("slack")
    posted = respx.post(f"{SLACK_BASE}/chat.postMessage").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "ts": "1721580753.000100", "channel": "C024BE91L"}
        )
    )
    receipt = await SlackSendProvider().send(
        _request(connection_id, channel=ProviderKind.SLACK)
    )
    assert receipt.provider_message_id == "C024BE91L:1721580753.000100"

    body = posted.calls[0].request.content.decode()
    assert "metadata" in body
    assert KEY in body


@respx.mock
async def test_not_in_channel_joins_and_retries_once(configured: None) -> None:
    """`chat:write.public` prevents most of these; when it does not apply,
    recovery is an action taken *before* classification, not a fifth class."""
    connection_id = await _connection("slack")
    post = respx.post(f"{SLACK_BASE}/chat.postMessage")
    post.side_effect = [
        httpx.Response(200, json={"ok": False, "error": "not_in_channel"}),
        httpx.Response(200, json={"ok": True, "ts": "1.1", "channel": "C024BE91L"}),
    ]
    join = respx.post(f"{SLACK_BASE}/conversations.join").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    receipt = await SlackSendProvider().send(
        _request(connection_id, channel=ProviderKind.SLACK)
    )
    assert receipt.provider_message_id == "C024BE91L:1.1"
    assert join.call_count == 1
    assert post.call_count == 2  # exactly one retry, never a loop


@respx.mock
async def test_the_slack_probe_asks_history_and_passes_include_all_metadata(
    configured: None,
) -> None:
    """🔴 R5, two rules at once.

    `include_all_metadata=true` or the payload silently vanishes leaving only
    `event_type` — so the probe would not see its own fingerprint. And the read
    is `conversations.history` on the single target channel, **never**
    `search.messages`, whose index is eventually consistent.
    """
    connection_id = await _connection("slack")
    history = respx.post(f"{SLACK_BASE}/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {
                        "ts": "1721580753.000100",
                        "text": "Confirming for Thursday.",
                        "metadata": {
                            "event_type": "unified_search_safe_send",
                            "event_payload": {"idempotency_key": KEY},
                        },
                    }
                ],
            },
        )
    )
    search = respx.post(f"{SLACK_BASE}/search.messages")

    probe = await SlackSendProvider().probe_by_fingerprint(
        _request(connection_id, channel=ProviderKind.SLACK)
    )

    assert probe.verdict is ProbeVerdict.FOUND
    assert probe.provider_message_id == "C024BE91L:1721580753.000100"
    body = history.calls[0].request.content.decode()
    assert "include_all_metadata=true" in body
    assert "oldest=" in body  # bounded window, not a channel scan
    assert not search.called, (
        "reconciliation used search.messages, whose index is eventually "
        "consistent — a false ABSENT there double-sends (risks.md R5)"
    )


@respx.mock
async def test_the_probe_joins_a_channel_it_can_post_to_but_not_read(
    configured: None,
) -> None:
    """🔴 Regression for a gap **spike 2 found live** (2026-08-01).

    `chat:write.public` lets the bot post to a public channel it never joined —
    but `conversations.history` on that same channel returns `not_in_channel`.
    Posting and reading have different membership rules, which the design did
    not anticipate.

    Left unhandled, every send to an unjoined channel probes INCONCLUSIVE and
    parks a perfectly recoverable send in `uncertain`. So the probe joins and
    retries, exactly as `post_message` already did.
    """
    connection_id = await _connection("slack")
    history = respx.post(f"{SLACK_BASE}/conversations.history")
    history.side_effect = [
        httpx.Response(200, json={"ok": False, "error": "not_in_channel"}),
        httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {
                        "ts": "1.5",
                        "text": "posted without joining",
                        "metadata": {
                            "event_type": "unified_search_safe_send",
                            "event_payload": {"idempotency_key": KEY},
                        },
                    }
                ],
            },
        ),
    ]
    join = respx.post(f"{SLACK_BASE}/conversations.join").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    probe = await SlackSendProvider().probe_by_fingerprint(
        _request(connection_id, channel=ProviderKind.SLACK)
    )

    assert probe.verdict is ProbeVerdict.FOUND, (
        "the probe gave up on not_in_channel instead of joining; a recoverable "
        "send would park in `uncertain`"
    )
    assert join.call_count == 1
    assert history.call_count == 2  # one retry, never a loop


@respx.mock
async def test_the_degraded_search_scan_does_not_auto_join(configured: None) -> None:
    """The other half of the same decision, and it goes the other way.

    Silently joining every public channel in a workspace so a *search* can read
    it is intrusive and visible to everyone in the channel. The scan skips what
    it cannot read; only the send probe joins.
    """
    import logging
    from datetime import UTC, datetime, timedelta

    from core.adapters.slack import SlackAdapter
    from core.adapters.types import AdapterContext, TokenKind

    respx.post(f"{SLACK_BASE}/search.messages").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "missing_scope"})
    )
    respx.post(f"{SLACK_BASE}/conversations.list").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "channels": [{"id": "C1", "name": "private-ish"}]}
        )
    )
    respx.post(f"{SLACK_BASE}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
    )
    join = respx.post(f"{SLACK_BASE}/conversations.join")

    async def token(_kind: TokenKind) -> str:
        return "xoxb-test"

    ctx = AdapterContext(
        connection_id=1,
        provider="slack",
        get_token=token,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        correlation_id="t",
        logger=logging.getLogger("t"),
    )
    results = await SlackAdapter().search("anything", ctx)

    assert results == []  # skipped, not joined
    assert not join.called, "the degraded search scan joined a channel to read it"


@respx.mock
async def test_a_window_with_no_metadata_is_inconclusive(configured: None) -> None:
    """If nothing in the window carries metadata we cannot distinguish "not
    sent" from "the payload did not round-trip" — which is exactly what spike 2
    is unrun about. INCONCLUSIVE, so the send parks rather than repeating."""
    connection_id = await _connection("slack")
    respx.post(f"{SLACK_BASE}/conversations.history").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "messages": [{"ts": "1.0", "text": "hello"}]}
        )
    )
    probe = await SlackSendProvider().probe_by_fingerprint(
        _request(connection_id, channel=ProviderKind.SLACK)
    )
    assert probe.verdict is ProbeVerdict.INCONCLUSIVE
    assert "no carrier" in probe.evidence


@respx.mock
async def test_an_empty_window_is_inconclusive_not_absent(configured: None) -> None:
    connection_id = await _connection("slack")
    respx.post(f"{SLACK_BASE}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": []})
    )
    probe = await SlackSendProvider().probe_by_fingerprint(
        _request(connection_id, channel=ProviderKind.SLACK)
    )
    assert probe.verdict is ProbeVerdict.INCONCLUSIVE


@respx.mock
async def test_a_stamped_message_absent_from_the_window_is_absent(
    configured: None,
) -> None:
    """The one case that legitimately concludes ABSENT: messages are visible,
    they carry metadata, and none of them is ours."""
    connection_id = await _connection("slack")
    respx.post(f"{SLACK_BASE}/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {
                        "ts": "1.0",
                        "text": "somebody else's message",
                        "metadata": {
                            "event_type": "unified_search_safe_send",
                            "event_payload": {"idempotency_key": "a-different-key"},
                        },
                    }
                ],
            },
        )
    )
    probe = await SlackSendProvider().probe_by_fingerprint(
        _request(connection_id, channel=ProviderKind.SLACK)
    )
    assert probe.verdict is ProbeVerdict.ABSENT


# ---------------------------------------------------------------------------
# The ordering the crash story rests on — task 8.1
# ---------------------------------------------------------------------------


@respx.mock
async def test_the_draft_id_is_durable_before_the_send(configured: None) -> None:
    """🔴 The gap group 8 closes, asserted where it actually matters.

    ``drafts.send`` is mocked to observe the database **at the moment the send
    call is made** — which is the instant a crash would strike. If the draft id
    were still being written in ``_mark_delivered``, this read would see NULL
    and a crash here would leave a draft at Google that nothing points at: the
    send would park in ``uncertain`` when it was in fact fully recoverable.
    """
    from core.send import handler, providers

    connection_id = await _connection("gmail")
    user_id = (
        await _scalar("SELECT user_id FROM connections WHERE id = :id", {"id": connection_id})
    )

    draft_id = uuid.uuid4()
    send_id = uuid.uuid4()
    async with session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO drafts (id, user_id, connection_id, channel, recipient,
                                    recipient_display, subject, body, idempotency_key)
                VALUES (:id, :user_id, :connection_id, 'gmail', 'dana@acme.test',
                        'dana@acme.test', 'Re: renewal', 'Confirming.', :key)
                """
            ),
            {
                "id": draft_id,
                "user_id": user_id,
                "connection_id": connection_id,
                "key": KEY,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO sends (id, user_id, draft_id, connection_id, provider,
                                   idempotency_key, state, confirmed_sha256)
                VALUES (:id, :user_id, :draft_id, :connection_id, 'gmail', :key,
                        'in_flight', :digest)
                """
            ),
            {
                "id": send_id,
                "user_id": user_id,
                "draft_id": draft_id,
                "connection_id": connection_id,
                "key": KEY,
                "digest": "0" * 64,
            },
        )
        await session.commit()

    respx.post(f"{GMAIL_BASE}/drafts").mock(
        return_value=httpx.Response(200, json={"id": "r-8891"})
    )

    observed: dict[str, object] = {}

    async def observe_then_send(_request: httpx.Request) -> httpx.Response:
        # Read in a *separate* session, so only committed data is visible. An
        # uncommitted write would be invisible here — which is the point.
        observed["draft_id"] = await _scalar(
            "SELECT provider_draft_id FROM sends WHERE id = :id", {"id": send_id}
        )
        observed["dispatched_at"] = await _scalar(
            "SELECT dispatched_at FROM sends WHERE id = :id", {"id": send_id}
        )
        return httpx.Response(200, json={"id": "18f2b", "threadId": "t9"})

    respx.post(f"{GMAIL_BASE}/drafts/send").mock(side_effect=observe_then_send)

    providers.set_provider(ProviderKind.GMAIL, GmailSendProvider())

    class Job:
        id = 1
        ref_id = send_id
        attempts = 1
        max_attempts = 6

    await handler.run_send(None, Job())  # type: ignore[arg-type]

    assert observed["draft_id"] == "r-8891", (
        "the draft id was not committed before drafts.send; a crash here would "
        "leave a draft at Google that reconciliation cannot ask about"
    )
    assert observed["dispatched_at"] is not None, (
        "dispatched_at was not committed before the provider call, so the "
        "sweeper could not tell 'never dispatched' from 'crashed mid-dispatch'"
    )

    final = await _scalar("SELECT state::text FROM sends WHERE id = :id", {"id": send_id})
    assert final == "delivered"


async def _scalar(sql: str, params: dict[str, object]) -> object:
    async with session_scope() as session:
        return (await session.execute(text(sql), params)).scalar()
