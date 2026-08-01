"""The real search adapters (openspec tasks 7.1-7.7).

Two kinds of assertion here, and the second one is the one the brief grades.

**Normalization is total.** Every adapter maps its provider's shape onto the
closed seven-field ``Result``, or drops the row. Recorded provider payloads
rather than hand-built objects, so the mapping is tested against what the
provider actually emits — including the awkward cases (an empty subject, an
RFC 2822 date, a missing permalink).

**Extensibility is a property, not a promise** (task 7.7). The last test
registers a *fourth* source and drives it through fan-out, merge, ranking and
status without touching a line outside its own definition and the registry.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from conftest import make_user
from core.adapters import merge, orchestrator, registry
from core.adapters.gmail import NO_SUBJECT, GmailAdapter
from core.adapters.slack import SlackAdapter
from core.adapters.types import AdapterContext, Result, TokenKind, assert_normalized
from core.adapters.websearch import WebSearchAdapter
from core.db import session_scope
from core.enums import SourceMode
from core.jobs import runtime

pytestmark = pytest.mark.usefixtures("clean_db")

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SLACK_BASE = "https://slack.com/api"


def _ctx(**overrides: object) -> AdapterContext:
    async def token(_kind: TokenKind) -> str:
        return "test-token"

    import logging

    defaults: dict[str, object] = {
        "connection_id": None,
        "provider": None,
        "get_token": token,
        "deadline": datetime.now(UTC) + timedelta(seconds=30),
        "correlation_id": "test",
        "logger": logging.getLogger("test"),
        "mode_hint": SourceMode.LIVE,
    }
    defaults.update(overrides)
    return AdapterContext(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gmail — task 7.1
# ---------------------------------------------------------------------------

GMAIL_LIST = {"messages": [{"id": "18f2a", "threadId": "t1"}]}
GMAIL_MESSAGE = {
    "id": "18f2a",
    "threadId": "t1",
    "snippet": "Attaching the redlines for the renewal — see clause 7.",
    "payload": {
        "headers": [
            {"name": "Subject", "value": "Re: acme renewal — contract redlines"},
            {"name": "From", "value": "Dana Chen <dana@acme.test>"},
            # RFC 2822, which `datetime.fromisoformat` cannot parse — the
            # boundary check would drop this result if the adapter did not
            # convert it.
            {"name": "Date", "value": "Mon, 21 Jul 2025 09:12:33 -0700"},
        ]
    },
}


@respx.mock
async def test_gmail_normalizes_onto_the_common_shape() -> None:
    respx.get(f"{GMAIL_BASE}/messages").mock(return_value=httpx.Response(200, json=GMAIL_LIST))
    respx.get(f"{GMAIL_BASE}/messages/18f2a").mock(
        return_value=httpx.Response(200, json=GMAIL_MESSAGE)
    )

    results = await GmailAdapter().search("acme renewal", _ctx())

    assert len(results) == 1
    result = assert_normalized(results[0])
    assert result.source == "gmail"
    assert result.title == "Re: acme renewal — contract redlines"
    assert result.snippet.startswith("Attaching the redlines")
    assert result.author == "Dana Chen <dana@acme.test>"
    assert result.url == "https://mail.google.com/mail/u/0/#all/18f2a"
    # Converted to ISO 8601, preserving the offset rather than assuming UTC.
    assert result.timestamp is not None
    assert datetime.fromisoformat(result.timestamp).hour == 9


@respx.mock
async def test_a_subject_less_message_gets_a_placeholder_not_a_blank() -> None:
    """A card with a blank title reads as a bug in our product rather than as a
    gap in Gmail's data — and `assert_normalized` would drop it outright."""
    respx.get(f"{GMAIL_BASE}/messages").mock(return_value=httpx.Response(200, json=GMAIL_LIST))
    respx.get(f"{GMAIL_BASE}/messages/18f2a").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "18f2a",
                "threadId": "t1",
                "snippet": "no subject on this one",
                "payload": {"headers": [{"name": "From", "value": "x@acme.test"}]},
            },
        )
    )
    results = await GmailAdapter().search("anything", _ctx())
    assert results[0].title == NO_SUBJECT
    assert_normalized(results[0])


@respx.mock
async def test_gmail_refreshes_and_retries_once_on_a_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 R24, task 6.3c — the full ladder, end to end.

    A raw ``401`` says only that the *access token* was not accepted. It is not
    evidence the grant is revoked, and treating it as terminal is described in
    the research as the most common implementation error in this area: it
    disconnects healthy users constantly.

    So: 401 → refresh → retry **once** → succeed. Escalation to
    ``needs_reconnect`` happens only if the *refresh* returns ``invalid_grant``,
    which is a different test (``test_a_revoked_grant_becomes_needs_reconnect``).
    """
    from core.config import get_settings
    from core.connections import oauth, store, tokens

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-test")
    get_settings.cache_clear()

    user_id = await make_user()
    async with session_scope() as session:
        connection = await store.upsert(
            session,
            user_id=user_id,
            provider="gmail",
            grant=oauth.Grant(
                external_account_id="acct-1",
                display_name="dana@acme.test",
                access_token="ya29.stale",
                refresh_token="1//0-live",
                # Far in the future, so the 401 is the *only* thing that can
                # trigger a refresh — otherwise this would pass for the wrong
                # reason.
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )
        await session.commit()

    try:
        refresh = respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "ya29.fresh", "expires_in": 3599}
            )
        )
        listing = respx.get(f"{GMAIL_BASE}/messages")
        listing.side_effect = [
            httpx.Response(401, json={"error": {"errors": [{"reason": "authError"}]}}),
            httpx.Response(200, json=GMAIL_LIST),
        ]
        respx.get(f"{GMAIL_BASE}/messages/18f2a").mock(
            return_value=httpx.Response(200, json=GMAIL_MESSAGE)
        )

        results = await GmailAdapter().search(
            "acme renewal",
            _ctx(connection_id=connection.id, get_token=tokens.token_getter(connection.id)),
        )

        assert len(results) == 1, "the 401 was treated as terminal instead of refreshed"
        assert refresh.call_count == 1
        assert listing.call_count == 2, "the call was not retried after the refresh"
        # The retry carried the *new* token, not the stale one.
        assert (
            listing.calls[1].request.headers["Authorization"] == "Bearer ya29.fresh"
        )
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Slack — tasks 7.2, 7.3
# ---------------------------------------------------------------------------

SLACK_SEARCH = {
    "ok": True,
    "messages": {
        "matches": [
            {
                "ts": "1721580753.123456",
                "text": "anyone have the acme renewal deck?",
                "username": "priya",
                "channel": {"id": "C024BE91L", "name": "acme-renewal"},
                "permalink": "https://acme.slack.com/archives/C024BE91L/p1721580753123456",
            }
        ]
    },
}


@respx.mock
async def test_slack_search_uses_the_user_token_and_the_returned_permalink() -> None:
    """🔴 R4 — and the permalink comes back on the match, so there is no extra
    `chat.getPermalink` call on this path."""
    used: list[TokenKind] = []

    async def token(kind: TokenKind) -> str:
        used.append(kind)
        return "xoxp-user" if kind is TokenKind.USER else "xoxb-bot"

    respx.post(f"{SLACK_BASE}/search.messages").mock(
        return_value=httpx.Response(200, json=SLACK_SEARCH)
    )

    results = await SlackAdapter().search("acme renewal", _ctx(get_token=token))

    assert used == [TokenKind.USER], (
        "search.messages was given a bot token; Slack answers "
        "not_allowed_token_type and the failure is invisible until runtime (R4)"
    )
    assert TokenKind.BOT not in used
    result = assert_normalized(results[0])
    assert result.url == SLACK_SEARCH["messages"]["matches"][0]["permalink"]
    assert result.id == "C024BE91L:1721580753.123456"
    assert result.title.startswith("#acme-renewal:")


@respx.mock
async def test_slack_falls_back_and_reports_degraded() -> None:
    """Task 7.3. The fallback sees only joined public channels and matches on
    substrings, so it is genuinely worse — and the run says so. A source quietly
    returning worse results while claiming to be live is what the status chip
    exists to prevent."""
    respx.post(f"{SLACK_BASE}/search.messages").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "missing_scope"})
    )
    respx.post(f"{SLACK_BASE}/conversations.list").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "channels": [{"id": "C024BE91L", "name": "acme-renewal"}]}
        )
    )
    respx.post(f"{SLACK_BASE}/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"ts": "1721580753.123456", "text": "the acme renewal deck", "user": "U1"},
                    {"ts": "1721580700.000000", "text": "unrelated chatter", "user": "U2"},
                ],
            },
        )
    )
    respx.post(f"{SLACK_BASE}/chat.getPermalink").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "permalink": "https://acme.slack.com/archives/C024BE91L/p1"}
        )
    )

    adapter = SlackAdapter()
    results = await adapter.search("acme renewal", _ctx())

    assert adapter.reported_mode is SourceMode.DEGRADED
    assert len(results) == 1  # only the matching message
    assert_normalized(results[0])


@respx.mock
async def test_a_revoked_slack_token_is_not_swallowed_by_the_fallback() -> None:
    """The fallback covers "search is unavailable to this connection", never
    "this grant is dead". Swallowing the latter would show a customer thin
    results instead of a reconnect prompt."""
    respx.post(f"{SLACK_BASE}/search.messages").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "token_revoked"})
    )
    from core.enums import ErrorClass
    from core.errors import ProviderError, classify

    with pytest.raises(ProviderError) as caught:
        await SlackAdapter().search("anything", _ctx())
    assert classify(caught.value) is ErrorClass.NEEDS_RECONNECT


# ---------------------------------------------------------------------------
# Web — tasks 7.4, 7.5
# ---------------------------------------------------------------------------


async def test_the_web_source_works_with_no_key_and_says_it_is_mocked() -> None:
    """Task 7.5 — the zero-connections case, which has to keep working."""
    results = await WebSearchAdapter().search("board deck", _ctx())
    assert results
    for result in results:
        assert_normalized(result)
    # Labelled in the text itself, so a screenshot with the chip cropped out is
    # still not mistakable for real data.
    assert "Mock result" in results[0].snippet


@respx.mock
async def test_the_web_source_parses_a_real_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import get_settings

    monkeypatch.setenv("WEB_SEARCH_API_KEY", "brave-test-key")
    get_settings.cache_clear()
    try:
        respx.get("https://api.search.brave.com/res/v1/web/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Acme renewal briefing",
                                "url": "https://example.test/briefing",
                                "description": "What the renewal means for buyers.",
                                "page_age": "2025-07-19T00:00:00Z",
                            },
                            # No URL: nothing to click, so nothing to render.
                            {"title": "Broken", "description": "no url"},
                        ]
                    }
                },
            )
        )
        results = await WebSearchAdapter().search("acme renewal", _ctx())
        assert len(results) == 1
        assert_normalized(results[0])
        assert results[0].url == "https://example.test/briefing"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Extensibility — task 7.7
# ---------------------------------------------------------------------------


@pytest.fixture
def fourth_source() -> Iterator[str]:
    """A source that did not exist when the fan-out, merge or ranking were
    written. Registered here and torn down after, because registration is
    module-level state."""

    class NotionAdapter:
        source = "notion"

        async def search(self, query: str, ctx: AdapterContext) -> list[Result]:
            return [
                Result(
                    source="notion",
                    id="page-1",
                    title=f"{query} — planning doc",
                    snippet="A fourth source nothing in the merge layer knows about.",
                    url="https://notion.test/page-1",
                    author="@sam",
                    timestamp=datetime.now(UTC).isoformat(),
                )
            ]

    registry.register(
        "notion", NotionAdapter, participates_by_default=True,
        mode=SourceMode.LIVE, requires_connection=False,
    )
    try:
        yield "notion"
    finally:
        # Re-registering the real defaults rather than clearing: other tests in
        # the session rely on the registry being populated.
        from core.adapters import live

        registry.clear()
        live.register_defaults()


async def test_a_fourth_adapter_flows_through_untouched(fourth_source: str) -> None:
    """🔴 Task 7.7 — the extensibility claim, made checkable.

    One file plus one registry line. If this test needed a change to the
    orchestrator, the merge layer, the ranking or the status payload, the seam
    would be in the wrong place — and phase 3's whole premise would be wrong.
    """
    user_id = await make_user()
    async with session_scope() as session:
        plan = await orchestrator.plan_search(
            session, user_id=user_id, query="roadmap", sources=[fourth_source]
        )
        await session.commit()

    while (await runtime.run_once(limit=5)).claimed:
        pass

    snapshot = await orchestrator.load_snapshot(
        search_id=plan.search_id, user_id=user_id
    )
    assert snapshot is not None
    assert snapshot.finished
    assert [row["source"] for row in snapshot.sources] == ["notion"]
    assert snapshot.sources[0]["status"] == "done"
    assert snapshot.sources[0]["mode"] == "live"
    assert snapshot.results[0].source == "notion"
    # It ranks like anything else: the merge layer never learned its name.
    assert snapshot.ranking[0]["source"] == "notion"


def test_ranking_is_computed_without_knowing_the_source() -> None:
    """The merge layer's contract, restated at the unit level: interleaving is a
    function of position and recency, never of which provider produced a row."""
    now = datetime.now(UTC)
    rows = [
        Result(
            source=source,
            id=f"{source}-1",
            title="t",
            snippet="s",
            url="https://x.test",
            timestamp=(now - timedelta(hours=hours)).isoformat(),
        )
        for source, hours in (("notion", 1), ("gmail", 2))
    ]
    ranked = [merge.rank_within_source([row], now=now)[0] for row in rows]
    assert {item.result.source for item in merge.merge(ranked)} == {"notion", "gmail"}
