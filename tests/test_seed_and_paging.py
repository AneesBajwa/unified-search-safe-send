"""The seed dataset and the listing conventions (task group 10, task 9.9).

The seed carries the demo. Its job is one sentence from the plan — *the app is
fully explorable with zero connections* — and the way that claim fails is not
loudly: it fails by a status being missing, or by a row being seeded twice, or by
history looking identical whether or not the reviewer has done anything
themselves. Each of those is asserted here.

🔴 **Every assertion reads the API, not the database.** Seeding rows that the
listings then filter out, or that the detail route cannot render, would leave a
green suite and an empty history page — which is precisely the class of defect
phase 3 shipped three of.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import make_api_key
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.usefixtures("clean_db")

#: contracts.md §4 — the states history has to be able to show from cold.
REQUIRED_SEND_STATES = {
    "delivered",
    "failed_transient",
    "failed_permanent",
    "uncertain",
    "in_flight",
}
REQUIRED_SOURCE_STATUSES = {"done", "failed", "needs_reconnect", "running"}


async def _seed() -> None:
    from scripts.seed import seed

    await seed()


async def _seeded_client() -> tuple[AsyncClient, dict[str, str]]:
    """Seed, then authenticate **as the seed user**.

    The seeder mints a key and prints it once; the plaintext is unrecoverable
    afterwards, by design, so there is no way to read it back. A second key is
    minted here for the same user instead — which is also the honest shape of
    what a reviewer does: run `make seed`, then sign in.

    Its ``name`` is deliberately not ``'seed'``: re-seeding deletes the seeder's
    own key by that name, and a helper whose credential vanishes halfway through
    the idempotency test would fail for a reason that has nothing to do with
    idempotency.
    """
    from api.main import create_app
    from core.db import session_scope
    from core.security import api_keys
    from sqlalchemy import text

    from scripts.seed import SEED_EMAIL

    minted = api_keys.mint()
    async with session_scope() as session:
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE email = :email"), {"email": SEED_EMAIL}
        )
        assert user_id is not None, "the seeder did not create its user"
        await session.execute(
            text(
                """
                INSERT INTO api_keys (user_id, key_id, key_hash, prefix_display, name)
                VALUES (:u, :key_id, :key_hash, :prefix, 'test-harness')
                """
            ),
            {
                "u": user_id,
                "key_id": minted.key_id,
                "key_hash": minted.key_hash,
                "prefix": minted.prefix_display,
            },
        )
        await session.commit()

    client = AsyncClient(
        transport=ASGITransport(app=create_app(run_worker_inline=False)),
        base_url="http://test",
    )
    return client, {"X-API-Key": minted.plaintext}


async def _seeded() -> tuple[AsyncClient, dict[str, str]]:
    await _seed()
    return await _seeded_client()


async def test_the_seeder_mints_exactly_one_key() -> None:
    """And stores it hashed. The printed plaintext is the only copy that exists."""
    from core.db import session_scope
    from sqlalchemy import text

    await _seed()
    await _seed()
    async with session_scope() as session:
        rows = (
            await session.execute(
                text("SELECT key_hash, prefix_display FROM api_keys WHERE name = 'seed'")
            )
        ).all()
    assert len(rows) == 1, f"re-seeding left {len(rows)} keys behind"
    assert len(rows[0].key_hash) == 64, "the key is not stored as a SHA-256 hex digest"


# ---------------------------------------------------------------------------
# the dataset
# ---------------------------------------------------------------------------


async def test_seed_covers_every_send_state_and_every_source_status() -> None:
    """Task 10.1 and verification step 6: every status represented, and browsable.

    Read through ``GET /v1/sends`` and ``GET /v1/searches/{id}`` rather than off
    the tables, because a state that exists in the database and never reaches a
    listing is a state the demo does not have.
    """
    client, auth = await _seeded()
    async with client:
        sends = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]
        states = {send["state"] for send in sends}
        assert REQUIRED_SEND_STATES <= states, f"missing: {REQUIRED_SEND_STATES - states}"

        searches = (await client.get("/v1/searches?limit=100", headers=auth)).json()["searches"]
        statuses: set[str] = set()
        for row in searches:
            snapshot = (
                await client.get(f"/v1/searches/{row['search_id']}", headers=auth)
            ).json()
            statuses |= {source["status"] for source in snapshot["sources"]}
        assert REQUIRED_SOURCE_STATUSES <= statuses, (
            f"missing: {REQUIRED_SOURCE_STATUSES - statuses}"
        )


async def test_every_seeded_row_is_badged() -> None:
    """Task 10.3. A seed row that does not say so is worse than no seed row.

    It is indistinguishable from something the reviewer did, which makes the
    whole history untrustworthy — and the trustworthiness of history is the
    product.
    """
    client, auth = await _seeded()
    async with client:
        sends = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]
        assert sends and all(send["is_seed"] for send in sends)
        searches = (await client.get("/v1/searches?limit=100", headers=auth)).json()["searches"]
        assert searches and all(search["is_seed"] for search in searches)


async def test_include_seed_false_excludes_them_from_every_listing() -> None:
    """The switch that makes "show me only what I actually did" answerable."""
    client, auth = await _seeded()
    async with client:
        assert (
            await client.get("/v1/sends?include_seed=false", headers=auth)
        ).json()["sends"] == []
        assert (
            await client.get("/v1/searches?include_seed=false", headers=auth)
        ).json()["searches"] == []

        # ...and a real row is still visible through the same filter, so the
        # test cannot pass by the filter simply returning nothing.
        real = await client.post("/v1/searches", json={"query": "mine"}, headers=auth)
        assert real.status_code == 202
        visible = (
            await client.get("/v1/searches?include_seed=false", headers=auth)
        ).json()["searches"]
        assert [row["query"] for row in visible] == ["mine"]


async def test_seeding_twice_does_not_duplicate() -> None:
    """Task 10.2, and verification step 7."""
    client, auth = await _seeded()
    async with client:
        first_sends = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]
        first_searches = (
            await client.get("/v1/searches?limit=100", headers=auth)
        ).json()["searches"]

        # Re-seed under the same user. The harness key survives because it is
        # not named 'seed', so the second read is made by the same caller as the
        # first — which is what makes the two counts comparable at all.
        await _seed()

        second_sends = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]
        second_searches = (
            await client.get("/v1/searches?limit=100", headers=auth)
        ).json()["searches"]

    assert len(second_sends) == len(first_sends), "re-seeding duplicated sends"
    assert len(second_searches) == len(first_searches), "re-seeding duplicated searches"


async def test_seeded_searches_have_results_behind_their_counts() -> None:
    """A source reporting `done, 4` with no rows is the status lying.

    Which is the one thing the per-source payload exists to get right — and a
    seed that reported counts with nothing behind them would demo a search
    result page that renders empty.
    """
    client, auth = await _seeded()
    async with client:
        searches = (await client.get("/v1/searches?limit=100", headers=auth)).json()["searches"]
        total_seen = 0
        for row in searches:
            snapshot = (
                await client.get(f"/v1/searches/{row['search_id']}", headers=auth)
            ).json()
            claimed = sum(source["result_count"] for source in snapshot["sources"])
            assert len(snapshot["results"]) == claimed, (
                f"{row['query']}: sources claim {claimed}, "
                f"{len(snapshot['results'])} rows returned"
            )
            total_seen += claimed
        assert total_seen >= 11, f"only {total_seen} seeded results across every search"


async def test_the_seeded_reconnect_source_offers_a_way_back() -> None:
    """🔴 A `needs_reconnect` chip with no action behind it is a dead end.

    This caught a real defect. The seeder wrote its adapter runs with a NULL
    ``connection_id``, and ``_source_view`` builds the reconnect link *from* that
    id — so the seeded `invoice 4417` search rendered the chip and advertised
    **nothing**. On the dataset a reviewer with zero connections actually
    explores, the one action that repairs a revoked grant was absent.

    It is the same shape as the defect phase 3 shipped green, and it survived
    every existing assertion for the same reason: they were about the *live*
    path, and this one is about the seed. Found by reading the value rather than
    trusting that a `needs_reconnect` source implies a link.

    The URL is then **requested**, not pattern-matched. With no OAuth client
    configured under test it answers `provider_not_configured` — our bug, said
    honestly — and what matters is that it is not a 404: it resolves to a route.
    """
    client, auth = await _seeded()
    async with client:
        searches = (await client.get("/v1/searches?limit=100", headers=auth)).json()["searches"]
        advertised: list[str] = []
        for row in searches:
            snapshot = (
                await client.get(f"/v1/searches/{row['search_id']}", headers=auth)
            ).json()
            for source in snapshot["sources"]:
                if source["status"] == "needs_reconnect":
                    url = source.get("error", {}).get("reconnect_url")
                    assert url, (
                        f"{row['query']}: source {source['source']} is "
                        "needs_reconnect and offers no way back"
                    )
                    advertised.append(url)

        assert advertised, "the seed contains no needs_reconnect source at all"

        for url in advertised:
            response = await client.get(url, headers=auth)
            assert response.status_code != 404, (
                f"the reconnect URL we hand the user, {url!r}, answered 404. "
                "The one action that repairs a revoked grant must lead somewhere."
            )
            assert response.json()["error"]["code"] != "not_found"


async def test_the_uncertain_row_carries_what_a_person_needs_to_settle_it() -> None:
    """The most important row in the dataset (contracts.md §4).

    ``dispatched_at``, three reconcile attempts, a link to check at the provider,
    and both resolutions. Without those it is just an amber badge, and an amber
    badge with no way to act on it is worse than a red one.
    """
    client, auth = await _seeded()
    async with client:
        sends = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]
        uncertain = next(send for send in sends if send["state"] == "uncertain")
        detail = (await client.get(f"/v1/sends/{uncertain['send_id']}", headers=auth)).json()

    evidence = detail["uncertainty"]
    assert evidence["dispatched_at"] is not None
    assert evidence["reconcile_attempts"] == 3
    assert evidence["reason"]
    assert evidence["verify_url"].startswith("https://")
    assert set(evidence["resolutions"]) == {"marked_delivered", "forced_resend"}
    # Never offered a retry: that is the action that re-sends a message which may
    # already have arrived.
    assert detail["retryable_by_operator"] is False


async def test_the_retrying_row_renders_a_countdown_not_a_spinner() -> None:
    """Task 11.5c needs ``backoff_seconds`` and ``next_attempt_at`` to be real.

    A client cannot compute either — the backoff has full jitter — so if the API
    does not carry them the console has no honest option but a spinner, and a
    spinner is what a user reads as "nobody knows what is happening".
    """
    client, auth = await _seeded()
    async with client:
        sends = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]
        retrying = next(send for send in sends if send["state"] == "in_flight")
        detail = (await client.get(f"/v1/sends/{retrying['send_id']}", headers=auth)).json()

    assert detail["backoff_seconds"], "no backoff to count down from"
    assert detail["next_attempt_at"], "no time to count down to"
    assert detail["attempts"] == 2
    assert detail["max_attempts"] == 2  # contracts.md §3, tests column


async def test_seeded_sends_never_dispatch() -> None:
    """🔴 With live credentials configured, a claimable seed job sends real email.

    The retrying row carries a job so its countdown is real; that job must be
    unclaimable. Asserted by running the worker rather than by reading the row —
    "no worker will take it" is a claim about the claim predicate, and the only
    honest way to check a predicate is to run it.
    """
    from core.jobs.runtime import run_once

    client, auth = await _seeded()
    async with client:
        before = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]
        outcome = await run_once()
        after = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]

    assert outcome.claimed == 0, f"the worker claimed a seeded job: {outcome.as_dict()}"
    assert {row["send_id"]: row["state"] for row in before} == {
        row["send_id"]: row["state"] for row in after
    }, "a seeded send changed state when the worker ran"


# ---------------------------------------------------------------------------
# listing conventions (task 9.9)
# ---------------------------------------------------------------------------


async def test_cursor_paging_walks_every_row_exactly_once() -> None:
    """Keyset paging, checked by walking it.

    The failure this prevents is a row seen twice or skipped across a page
    boundary — invisible in a test that only checks page one's length, and the
    reason the cursor carries the id as well as the timestamp: seeded rows share
    ``now()`` to the microsecond, so a cursor on the timestamp alone would either
    repeat or lose whichever the plan happened to return first.
    """
    client, auth = await _seeded()
    async with client:
        everything = (await client.get("/v1/sends?limit=100", headers=auth)).json()["sends"]
        assert len(everything) >= 7

        walked: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            url = "/v1/sends?limit=2" + (f"&cursor={cursor}" if cursor else "")
            page = (await client.get(url, headers=auth)).json()
            walked.extend(page["sends"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        else:  # pragma: no cover - a cursor that never exhausts is an infinite loop
            pytest.fail("the cursor never returned null")

    ids = [row["send_id"] for row in walked]
    assert ids == [row["send_id"] for row in everything], "paging changed the order"
    assert len(ids) == len(set(ids)), "a row appeared on two pages"


async def test_the_last_page_offers_no_cursor() -> None:
    """``next_cursor: null`` rather than one that resolves to nothing.

    A UI that shows Load-more leading to an empty page looks broken in a way
    that is hard to tell from a real failure.
    """
    client, auth = await _seeded()
    async with client:
        page = (await client.get("/v1/sends?limit=100", headers=auth)).json()
        assert page["next_cursor"] is None

        first = (await client.get("/v1/sends?limit=2", headers=auth)).json()
        assert first["next_cursor"] is not None


async def test_a_cursor_is_scoped_to_its_owner() -> None:
    """A forged or borrowed cursor cannot reach another user's rows.

    The cursor is opaque rather than signed, and this is why that is safe: it
    names a position in an ordering that is already filtered by the caller's own
    user id, so the worst it can do is address the forger's own data.
    """
    client, auth = await _seeded()
    other = await make_api_key()
    async with client:
        page = (await client.get("/v1/sends?limit=2", headers=auth)).json()
        borrowed = page["next_cursor"]
        theirs = (
            await client.get(
                f"/v1/sends?cursor={borrowed}", headers={"X-API-Key": str(other["key"])}
            )
        ).json()
    assert theirs["sends"] == []
