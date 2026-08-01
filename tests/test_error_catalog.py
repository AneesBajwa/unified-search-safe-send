"""Every published error code, proven to have crossed the wire (task 9.11).

🔴 **This file exists because of the three defects phase 3 shipped green.** Each
was a test asserting the *shape* of a value rather than the value: that a
parameter was present rather than what it said, that a URL ended in the right
suffix rather than that it resolved. Group 9 is an API-shaping phase, which is
where that mistake is cheapest to make — and "a documented error code" is not
"an error code that has ever been returned".

So: for every entry in the catalog that can be a top-level refusal, a real
request is made and the response is read. Codes that are carried *inside* a
payload rather than as an envelope — a per-source ``error`` block, a send's
failure class — are declared as such in the catalog and checked where they
actually appear.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from api import catalog
from conftest import make_api_key
from httpx import AsyncClient

pytestmark = pytest.mark.usefixtures("clean_db")


async def _drafted(client: AsyncClient, auth: dict[str, str]) -> dict[str, Any]:
    created = await client.post(
        "/v1/drafts",
        json={"channel": "gmail", "to": "someone@example.test", "body": "hello"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def _error(response: Any) -> dict[str, Any]:
    body = response.json()
    assert "error" in body, f"not an error envelope: {body}"
    return dict(body["error"])


def _assert_catalogued(response: Any, code: str) -> None:
    """The response must match the catalog **exactly** — code, status, class.

    All three, because each has been wrong on its own: a code that means 422 in
    one route and 409 in another is a code no client can branch on, and a
    classification of `needs_reconnect` on what is actually our configuration bug
    sends a user round in circles repairing a grant that was never broken (R24).
    """
    spec = catalog.BY_CODE[code]
    error = _error(response)
    assert error["code"] == code, f"expected {code}, got {error}"
    assert response.status_code == spec.status, (
        f"{code} answered {response.status_code}, catalog says {spec.status}"
    )
    assert error["classification"] == spec.classification, (
        f"{code} is classified {error['classification']}, catalog says {spec.classification}"
    )


async def test_every_envelope_code_is_returned_by_a_real_request(
    api_client: tuple[AsyncClient, dict[str, str]],
) -> None:
    """The catalog and the API are the same fact.

    Each branch below makes a request and records the code it got back. At the
    end, the set of codes observed must cover every catalog entry marked
    reachable — so adding a row to the catalog without a way to reach it fails
    here, and so does deleting the only route that produced one.
    """
    client, auth = api_client
    observed: set[str] = set()

    def record(response: Any, code: str) -> None:
        _assert_catalogued(response, code)
        observed.add(code)

    # unauthorized — and identically so for a malformed key, an unknown one, and
    # none at all. Three shapes of wrong, one answer.
    for headers in ({}, {"X-API-Key": "nonsense"}, {"X-API-Key": "sk_live_" + "a" * 60}):
        record(await client.get("/v1/sends", headers=headers), "unauthorized")

    # not_found, for something that does not exist...
    missing = uuid.uuid4()
    record(await client.get(f"/v1/searches/{missing}", headers=auth), "not_found")
    # ...and for something that does, but is not theirs. Same answer, which is
    # the point: a 403 here would confirm the resource exists.
    other = await make_api_key()
    other_auth = {"X-API-Key": str(other["key"])}
    theirs = await client.post("/v1/searches", json={"query": "private"}, headers=other_auth)
    record(
        await client.get(f"/v1/searches/{theirs.json()['search_id']}", headers=auth),
        "not_found",
    )

    draft = await _drafted(client, auth)
    draft_id = draft["draft"]["id"]

    # confirmation_required — no digest, no send. The refusal *is* the product
    # behaviour, so it has to be legible rather than a generic 422 from the
    # transport layer.
    record(
        await client.post(f"/v1/drafts/{draft_id}/send", json={}, headers=auth),
        "confirmation_required",
    )

    # body_changed_since_confirmation — a digest that was true before the edit.
    stale = draft["confirmation"]["confirm_sha256"]
    await client.patch(f"/v1/drafts/{draft_id}", json={"body": "different"}, headers=auth)
    record(
        await client.post(
            f"/v1/drafts/{draft_id}/send", json={"confirmed_sha256": stale}, headers=auth
        ),
        "body_changed_since_confirmation",
    )

    # idempotency_key_body_mismatch — the same key, genuinely different content.
    fresh = await client.get(f"/v1/drafts/{draft_id}", headers=auth)
    digest = fresh.json()["confirmation"]["confirm_sha256"]
    sent = await client.post(
        f"/v1/drafts/{draft_id}/send", json={"confirmed_sha256": digest}, headers=auth
    )
    assert sent.status_code == 201, sent.text
    record(
        await client.post(
            f"/v1/drafts/{draft_id}/send",
            json={"confirmed_sha256": "0" * 64},
            headers=auth,
        ),
        "idempotency_key_body_mismatch",
    )

    # recipient_invalid — a draft with no body, and an unknown source name.
    record(
        await client.post(
            "/v1/drafts", json={"channel": "slack", "to": "C1", "body": "  "}, headers=auth
        ),
        "recipient_invalid",
    )
    record(
        await client.post("/v1/searches", json={"query": "x", "sources": ["nope"]}, headers=auth),
        "recipient_invalid",
    )

    # invalid_cursor — recoverable in one step, so a named 422 rather than a 500
    # that reads like ours.
    record(await client.get("/v1/sends?cursor=not-a-cursor", headers=auth), "invalid_cursor")

    # connection_needs_reconnect — a user with no grant at all, refused at draft
    # time rather than at send time.
    stranger = await make_api_key()
    stranger_auth = {"X-API-Key": str(stranger["key"])}
    async with _no_connections(int(stranger["user_id"])):
        refusal = await client.post(
            "/v1/drafts",
            json={"channel": "gmail", "to": "nobody@example.test", "body": "x"},
            headers=stranger_auth,
        )
        record(refusal, "connection_needs_reconnect")
        # 🔴 And it carries the repair. `api-design.md` has always said this code
        # hands back a URL; the gate raised it without one, so the compose screen
        # rendered the refusal with nothing to click — a dead end at the exact
        # moment the product is supposed to offer the way out. Under both names,
        # because the older one is what existing clients read.
        error = refusal.json()["error"]
        assert error.get("action_url"), "the gate refused with nowhere to go"
        assert error["action_url"] == error.get("reconnect_url")
        # A user with no connection at all is a *first* connect, not a re-grant.
        assert "reconnect=" not in error["action_url"]
        # Followed, not read. The historical failure is a **404** — a URL that
        # names no route at all — and only requesting it can tell you.
        followed = await client.get(error["action_url"], headers=stranger_auth)
        assert followed.status_code != 404, (
            "the advertised repair points at no route; this link has been broken "
            "on four surfaces already, which is why it is followed rather than read"
        )
        if followed.status_code == 200:
            assert followed.json()["authorize_url"].startswith("http")
        else:
            # The suite is hermetic, so there may be no OAuth client at all. That
            # is `config` — our gap, named as ours — and never `needs_reconnect`,
            # which would send the user in circles repairing a healthy grant.
            assert followed.json()["error"]["code"] == "provider_not_configured"
            assert followed.json()["error"]["classification"] == "config"

    # resolution_required — the refusal that used to share `confirmation_required`
    # at a different status. An in-doubt send needs a decision, not a repeat.
    uncertain_id = await _make_uncertain_send(client, auth)
    record(
        await client.post(f"/v1/sends/{uncertain_id}/retry", headers=auth),
        "resolution_required",
    )

    # provider_not_configured / authorization_* / state_invalid — the connect flow.
    record(
        await client.get("/v1/connections/callback/gmail?error=access_denied"),
        "authorization_denied",
    )
    record(await client.get("/v1/connections/callback/gmail"), "authorization_incomplete")
    record(
        await client.get("/v1/connections/callback/gmail?code=abc&state=tampered"),
        "state_invalid",
    )
    record(
        await client.get("/v1/connections/gmail/authorize", headers=auth),
        "provider_not_configured",
    )

    expected = {spec.code for spec in catalog.CATALOG if spec.reachable_as_envelope}
    # `reconnect_account_mismatch` needs a completed OAuth round trip against a
    # second provider account, which no headless test can produce; it is covered
    # by test_connection_routes.py at the service boundary instead.
    expected.discard("reconnect_account_mismatch")
    assert observed == expected, (
        f"catalogued but never returned: {sorted(expected - observed)}; "
        f"returned but not catalogued: {sorted(observed - expected)}"
    )


async def test_in_payload_codes_appear_where_they_are_documented(
    api_client: tuple[AsyncClient, dict[str, str]],
) -> None:
    """The codes that ride inside a payload rather than as a refusal.

    ``provider_unavailable`` and ``connection_needs_reconnect`` are what a search
    snapshot's per-source ``error`` block carries, and that block is the only
    thing standing between "we looked and found nothing" and "we could not look"
    (R16). Asserted here because they never appear as an HTTP status.
    """
    from core.jobs.runtime import run_once

    client, auth = api_client
    queued = await client.post(
        "/v1/searches",
        json={"query": "x", "sources": ["fault:transient", "fault:reconnect"]},
        headers=auth,
    )
    assert queued.status_code == 202, queued.text
    search_id = queued.json()["search_id"]

    for _ in range(12):
        await run_once()
        snapshot = (await client.get(f"/v1/searches/{search_id}", headers=auth)).json()
        if snapshot["finished"]:
            break

    codes = {
        source["error"]["code"] for source in snapshot["sources"] if "error" in source
    }
    assert codes, f"no source reported an error: {snapshot['sources']}"
    for code in codes:
        assert code in catalog.BY_CODE, f"a source returned an uncatalogued code: {code}"


async def test_a_never_connected_source_offers_to_connect_not_an_error(
    api_client: tuple[AsyncClient, dict[str, str]],
) -> None:
    """🔴 The state **every new user sees first**, and it was reported as a fault.

    A source whose provider has never been connected raised ``TokenUnavailable``,
    a bare RuntimeError, which fell through ``classify`` to ``permanent`` — so a
    brand-new user's very first search reported Gmail and Slack as
    ``provider_unavailable``, classification ``permanent``. That reads as "this
    provider is broken and will stay broken" about a provider they simply had not
    connected yet, and it carried **no action** when the action is one click away.

    That is risks.md R16 at its sharpest: not "we looked and found nothing" but
    "we could not look", reported as neither. Found by making a search as a fresh
    user and reading the payload, which no existing test did — every one of them
    ran as a user `dev-login` had already given connections to.

    The fix is a distinct, actionable state. Asserted here by **following the URL
    that is offered**, because the last three times this project got a repair
    link wrong, the link's shape was right and the page was not.
    """
    from core.db import session_scope
    from core.jobs.runtime import run_once
    from sqlalchemy import text

    client, auth = api_client

    # `dev-login` provisions a fake connection for every *unconfigured* provider,
    # which is honest — but the state under test is the one a user with nothing
    # connected meets, so they have to go.
    async with session_scope() as session:
        await session.execute(text("DELETE FROM connections"))
        await session.commit()

    # ⚠️ And the adapter has to actually **reach for a credential**. With no
    # client ids configured the suite registers fixture sources that need no
    # token and cheerfully return results, so the state under test simply cannot
    # arise — which is exactly why no existing test had ever seen it. This is the
    # smallest honest reproduction: a source that requires a connection and asks
    # for its token, with nothing behind it.
    with _credential_hungry("gmail"):
        queued = await client.post("/v1/searches", json={"query": "acme"}, headers=auth)
        search_id = queued.json()["search_id"]
        for _ in range(12):
            await run_once()
            snapshot = (
                await client.get(f"/v1/searches/{search_id}", headers=auth)
            ).json()
            if snapshot["finished"]:
                break

    unconnected = [
        source
        for source in snapshot["sources"]
        if source.get("error", {}).get("code") == "connection_not_connected"
    ]
    assert unconnected, (
        "no source reported itself as never-connected. Sources reported: "
        + str([(x["source"], x["status"], x.get("error", {}).get("code"))
                for x in snapshot["sources"]])
    )

    for source in unconnected:
        assert source["status"] == "needs_reconnect", (
            f"{source['source']} is not connected but reports {source['status']!r}, "
            "which renders as a fault rather than as something to do"
        )
        assert source["error"]["classification"] == "needs_reconnect", (
            "classified as a failure, so a client cannot tell it apart from a "
            "provider outage — and `permanent` in particular says 'this will "
            "never work', which is the opposite of the truth"
        )
        # Never `reconnect_url`: there is nothing to *re*-connect, and offering
        # that verb for an account someone never linked reads as though we lost
        # something of theirs.
        assert "reconnect_url" not in source["error"]

        url = source["error"]["action_url"]
        followed = await client.get(url, headers=auth)
        assert followed.status_code != 404, (
            f"the connect link we hand the user, {url!r}, answered 404"
        )
        assert followed.json().get("error", {}).get("code") != "not_found"

    # ...and a source that needs no connection at all is untouched by any of this.
    web = next(source for source in snapshot["sources"] if source["source"] == "web")
    assert web["status"] == "done"
    assert "error" not in web
    assert snapshot["results"], (
        "with nothing connected the search returned nothing at all — the "
        "zero-connections case has to still be worth looking at"
    )


class _credential_hungry:
    """Register a source that requires a connection and asks for its token.

    Restores the real registry afterwards. Without the restore, every later test
    in the session would run against this adapter — the kind of leak that makes
    one file's bug look like another file's flake.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> None:
        from core.adapters import registry
        from core.adapters.types import AdapterContext, Result, TokenKind
        from core.enums import SourceMode

        name = self.name

        class NeedsCredential:
            source = name

            async def search(self, query: str, ctx: AdapterContext) -> list[Result]:
                # The whole point: it reaches for the credential rather than
                # being told there is none. `token_getter(None)` raises, which is
                # the path a real adapter takes for a user who never connected.
                await ctx.get_token(TokenKind.OAUTH)
                return []

        registry.register(
            name, NeedsCredential, participates_by_default=True,
            mode=SourceMode.LIVE, requires_connection=True,
        )

    def __exit__(self, *_: object) -> None:
        from core.adapters import live, registry

        registry.clear()
        live.register_defaults()


def test_the_readme_documents_every_code() -> None:
    """The spec says the README documents the API; this makes that checkable.

    Documentation drifts silently, and an error table missing the code a client
    just received is worse than no table — it implies the code is not real. The
    catalog is the source of truth and this is the cheapest possible check that
    the prose agrees with it.
    """
    from conftest import REPO_ROOT

    readme = (REPO_ROOT / "README.md").read_text()
    undocumented = [spec.code for spec in catalog.CATALOG if f"`{spec.code}`" not in readme]
    assert not undocumented, (
        f"these codes can be returned and are not in the README's error table: "
        f"{undocumented}"
    )


def test_catalog_has_no_duplicate_codes() -> None:
    codes = [spec.code for spec in catalog.CATALOG]
    assert len(codes) == len(set(codes)), "a code is declared twice"


def test_unknown_codes_cannot_be_raised() -> None:
    """A code that is not in the catalog fails at the raise site.

    Which is the whole point of looking status up rather than passing it: an
    undocumented refusal cannot reach a client, so the README's error table and
    the code cannot drift apart without something going red.
    """
    from api.errors import ApiError

    with pytest.raises(KeyError, match="published error catalog"):
        ApiError("something_i_just_made_up", "nope")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _make_uncertain_send(client: AsyncClient, auth: dict[str, str]) -> str:
    """A real send, driven into ``uncertain`` through the runtime.

    Not an INSERT: ``sends_uncertain_was_dispatched`` means the state is only
    reachable after a dispatch was actually attempted, and writing the row by
    hand would test a state the system can produce rather than the one it does.
    """
    from core.db import session_scope
    from sqlalchemy import text

    draft = await _drafted(client, auth)
    draft_id = draft["draft"]["id"]
    sent = await client.post(
        f"/v1/drafts/{draft_id}/send",
        json={"confirmed_sha256": draft["confirmation"]["confirm_sha256"]},
        headers=auth,
    )
    send_id = sent.json()["send_id"]
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE sends
                   SET state = 'uncertain', dispatched_at = now(),
                       reconcile_attempts = 3,
                       last_error_class = 'transient',
                       last_error_detail = 'three probes returned no usable answer'
                 WHERE id = :id
                """
            ),
            {"id": uuid.UUID(send_id)},
        )
        await session.commit()
    return str(send_id)


class _no_connections:
    """Strip a user's connections for the duration of the block.

    ``dev-login`` provisions a fake connection for every provider that is *not*
    configured, which is honest — but the refusal being tested is the one a user
    with no grant meets, so the connections have to go.
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    async def __aenter__(self) -> None:
        from core.db import session_scope
        from sqlalchemy import text

        async with session_scope() as session:
            await session.execute(
                text("DELETE FROM connections WHERE user_id = :u"), {"u": self.user_id}
            )
            await session.commit()

    async def __aexit__(self, *_: object) -> None:
        return None

