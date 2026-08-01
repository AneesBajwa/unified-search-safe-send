"""The adapter layer (openspec tasks 4.2, 4.8, 4.9).

Two of these are headline behaviours from ``contracts.md`` §6 — every result
conforms to the closed shape, and the merge layer does not know which sources
exist. Both are properties the suite has to *defend*, because both are the kind
of thing that stays true right up until someone adds a convenient field.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_api_key, make_user
from core.adapters import merge, registry
from core.adapters.orchestrator import load_snapshot, plan_search
from core.adapters.types import (
    RESULT_FIELDS,
    NormalizationError,
    Result,
    assert_normalized,
)
from core.db import session_scope
from core.jobs import runtime
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("clean_db")

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The modules that must never name a provider, and the names they must not
#: contain. Whole-file, comments included: a comment naming a source is a strong
#: hint the code below it is about to.
AGNOSTIC_MODULES = (
    REPO_ROOT / "packages" / "core" / "adapters" / "orchestrator.py",
    REPO_ROOT / "packages" / "core" / "adapters" / "merge.py",
)
FORBIDDEN_SOURCE_LITERALS = ("gmail", "slack", "web")


def test_the_orchestrator_and_merge_layer_never_name_a_source() -> None:
    """openspec task 4.9 — extensibility as a property the suite defends.

    Written now rather than afterwards on purpose: a test like this written
    after the fact gets written *around* whatever the code already does, and
    then it only asserts that nobody made it worse.
    """
    offences: list[str] = []
    for module in AGNOSTIC_MODULES:
        for lineno, line in enumerate(module.read_text().splitlines(), start=1):
            for literal in FORBIDDEN_SOURCE_LITERALS:
                if re.search(literal, line, flags=re.IGNORECASE):
                    offences.append(f"{module.name}:{lineno}: {line.strip()}")
    assert not offences, (
        "the fan-out and merge layers must not know which sources exist — "
        "adding a fourth adapter is one file plus one registry line:\n"
        + "\n".join(offences)
    )


# ---------------------------------------------------------------------------
# The closed shape
# ---------------------------------------------------------------------------


def test_a_result_serializes_to_exactly_the_documented_keys() -> None:
    result = Result(
        source="anything",
        id="1",
        title="t",
        snippet="s",
        url="https://example.test/1",
        author="a",
        timestamp="2026-07-31T12:00:00+00:00",
    )
    assert tuple(result.as_public_dict()) == RESULT_FIELDS


def test_absent_optionals_are_omitted_rather_than_nulled() -> None:
    """`author?: string` in the brief means absent, not `null`."""
    payload = Result(
        source="anything", id="1", title="t", snippet="s", url="https://example.test/1"
    ).as_public_dict()
    assert set(payload) == {"source", "id", "title", "snippet", "url"}


@pytest.mark.parametrize("field", ["source", "id", "title", "snippet", "url"])
def test_normalization_rejects_an_empty_required_field(field: str) -> None:
    values = {
        "source": "anything",
        "id": "1",
        "title": "t",
        "snippet": "s",
        "url": "https://example.test/1",
    }
    values[field] = "   "
    with pytest.raises(NormalizationError, match=field):
        assert_normalized(Result(**values))


def test_normalization_rejects_a_timestamp_that_is_not_iso8601() -> None:
    with pytest.raises(NormalizationError, match="ISO 8601"):
        assert_normalized(
            Result(
                source="anything",
                id="1",
                title="t",
                snippet="s",
                url="https://example.test/1",
                timestamp="last Tuesday",
            )
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_an_unknown_source_is_rejected_rather_than_dispatched_hopefully() -> None:
    with pytest.raises(registry.UnknownSource, match="no adapter registered"):
        registry.registration("carrier-pigeon")


def test_the_fault_adapters_are_reachable_but_never_part_of_a_plain_search() -> None:
    """The phase-1 `fault:` seam, folded into the registry.

    Registered, so `make smoke` keeps its failure paths and the dispatch path
    has exactly one way to resolve a source — but never in the default fan-out,
    so an ordinary search cannot wander into a deliberate failure.
    """
    assert "fault:transient" in registry.known_sources()
    assert "fault:transient" not in registry.default_sources()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _ranked(source: str, rank: int, score: float) -> merge.Ranked:
    return merge.Ranked(
        result=Result(
            source=source,
            id=f"{source}-{rank}",
            title="t",
            snippet="s",
            url=f"https://example.test/{source}/{rank}",
        ),
        source_rank=rank,
        recency_weight=0.0,
        blended_score=score,
    )


def test_recency_decays_by_half_over_the_half_life() -> None:
    now = datetime.now(UTC)
    half_life = timedelta(hours=merge.RECENCY_HALF_LIFE_HOURS)
    assert merge.recency_weight(now, now=now) == pytest.approx(1.0)
    assert merge.recency_weight(now - half_life, now=now) == pytest.approx(0.5)


def test_an_unknown_timestamp_scores_zero_not_one() -> None:
    """"We do not know when this happened" must never outrank "five minutes ago"."""
    now = datetime.now(UTC)
    assert merge.recency_weight(None, now=now) == 0.0


def test_the_merge_interleaves_so_one_source_cannot_own_the_page() -> None:
    ranked = [
        _ranked("a", 1, 0.90),
        _ranked("a", 2, 0.89),
        _ranked("a", 3, 0.88),
        _ranked("b", 1, 0.50),
    ]
    order = [item.result.source for item in merge.merge(ranked)]
    # The strongest single match still leads; the second slot goes to the other
    # source rather than to the same one three times.
    assert order == ["a", "b", "a", "a"]


def test_a_chatty_source_is_capped() -> None:
    ranked = [_ranked("a", i, 1.0 - i / 100) for i in range(1, 30)]
    merged = merge.merge(ranked, per_source_cap=3)
    assert len(merged) == 3


# ---------------------------------------------------------------------------
# Fan-out end to end
# ---------------------------------------------------------------------------


async def test_a_second_grant_on_one_provider_gets_its_own_adapter_run() -> None:
    """`provider-connections`: one independent adapter run **per connection**.

    🔴 The fan-out used to build ``{provider: connection_id}`` and so kept only
    the *last* grant. A user with two accounts on one provider got a single run
    against one of them and the other was never searched — and nothing surfaced
    it, because the snapshot showed one healthy source. Half the mailbox the
    user had deliberately connected simply did not appear in the results.

    Asserted on the runs rather than on the dict, because the dict was the bug.
    """
    # A source whose name matches a provider and which needs a grant. In the
    # hermetic environment the real ones register as connectionless fakes, so
    # the attach — the thing under test — would never happen.
    source = "gmail"
    previous = registry.registration(source)
    registry.register(
        source,
        previous.factory,
        participates_by_default=False,
        mode=previous.mode,
        requires_connection=True,
    )

    user_id = await make_user()
    connection_ids: list[int] = []
    async with session_scope() as session:
        for index in (1, 2):
            connection_id = await session.scalar(
                text(
                    """
                    INSERT INTO connections (user_id, provider, external_account_id,
                                             display_name, status)
                    VALUES (:user_id, CAST(:provider AS provider_kind), :external_id,
                            :display, 'active')
                    RETURNING id
                    """
                ),
                {
                    "user_id": user_id,
                    "provider": source,
                    "external_id": f"account-{index}",
                    "display": f"person{index}@example.test",
                },
            )
            connection_ids.append(int(connection_id or 0))
        plan = await plan_search(
            session, user_id=user_id, query="acme renewal", sources=[source]
        )
        await session.commit()

    assert [run.connection_id for run in plan.runs] == connection_ids, (
        "a search must dispatch one independent adapter run per connection"
    )
    assert len({run.job_id for run in plan.runs}) == 2, "the two runs share a job"

    snapshot = await load_snapshot(search_id=plan.search_id, user_id=user_id)
    assert snapshot is not None
    # Two entries for one source name, told apart by the account they went
    # through — `source` alone is not a key.
    assert [row["source"] for row in snapshot.sources] == [source, source]
    assert [row["display_name"] for row in snapshot.sources] == [
        "person1@example.test",
        "person2@example.test",
    ]
    registry.register(
        source,
        previous.factory,
        participates_by_default=previous.participates_by_default,
        mode=previous.mode,
        requires_connection=previous.requires_connection,
    )


async def test_a_search_fans_out_to_every_registered_source_and_stores_results() -> None:
    user_id = await make_user()
    async with session_scope() as session:
        plan = await plan_search(session, user_id=user_id, query="acme renewal")
        await session.commit()

    assert {run.source for run in plan.runs} == set(registry.default_sources())
    assert all(run.job_id is not None for run in plan.runs), "every source got a durable job"

    while (await runtime.run_once(limit=10)).claimed:
        pass

    snapshot = await load_snapshot(search_id=plan.search_id, user_id=user_id)
    assert snapshot is not None
    assert snapshot.finished
    assert {row["status"] for row in snapshot.sources} == {"done"}
    assert snapshot.results, "the fan-out produced no results at all"
    # Ranking metadata rides the envelope, parallel to the results — never on
    # them (design D7).
    assert len(snapshot.ranking) == len(snapshot.results)
    assert all("blended_score" in entry for entry in snapshot.ranking)


@pytest.mark.headline
async def test_every_result_the_api_serves_conforms_to_the_closed_shape(
    api_client: tuple[object, dict[str, str]],
) -> None:
    """contracts.md §6, headline behaviour 3 — asserted at the wire, not in a
    dataclass, because the wire is where a stray key would actually appear."""
    client, headers = api_client
    created = await client.post(  # type: ignore[attr-defined]
        "/v1/searches", json={"query": "acme renewal"}, headers=headers
    )
    assert created.status_code == 202
    search_id = created.json()["search_id"]

    while (await runtime.run_once(limit=10)).claimed:
        pass

    snapshot = await client.get(f"/v1/searches/{search_id}", headers=headers)  # type: ignore[attr-defined]
    body = snapshot.json()
    assert body["results"], "no results to check the shape of"
    for result in body["results"]:
        extra = set(result) - set(RESULT_FIELDS)
        assert not extra, f"result carries keys outside the closed shape: {extra}"
    assert "ranking" not in body, "ranking metadata must not ride along by default"

    debug = await client.get(  # type: ignore[attr-defined]
        f"/v1/searches/{search_id}?debug=1", headers=headers
    )
    assert "ranking" in debug.json(), "?debug=1 exposes ranking, outside the results"


async def test_a_revoked_grant_surfaces_as_reconnect_not_a_generic_failure(
    api_client: tuple[object, dict[str, str]],
) -> None:
    """contracts.md §6, headline behaviour 4.

    ⏸️ The revocation is *synthetic* until group 6 gives us a grant that can
    actually be revoked — but nothing else here is: the fake adapter raises the
    same `invalid_grant` Google emits, it flows through the real `classify`
    boundary, and it lands on the real `needs_reconnect` run status. What phase
    3 replaces is where the error comes from, not what happens to it.

    The distinction being defended: a revoked grant is an **action**, not an
    error. Rendering it as a generic failure leaves the customer with a dead
    integration and nothing to click.
    """
    client, headers = api_client
    created = await client.post(  # type: ignore[attr-defined]
        "/v1/searches",
        json={"query": "invoice 4417", "sources": ["web", "fault:reconnect"]},
        headers=headers,
    )
    search_id = created.json()["search_id"]

    while (await runtime.run_once(limit=10)).claimed:
        pass

    snapshot = (
        await client.get(f"/v1/searches/{search_id}", headers=headers)  # type: ignore[attr-defined]
    ).json()
    revoked = next(row for row in snapshot["sources"] if row["source"] == "fault:reconnect")

    assert revoked["status"] == "needs_reconnect", (
        "a revoked grant must be its own status, not a flavour of `failed`"
    )
    assert revoked["error"]["classification"] == "needs_reconnect"
    # One of the two **actionable** codes. Which one depends on whether a
    # connection row exists to re-grant, and `fault:reconnect` is a synthetic
    # source that never had one — `plan_search` attaches a connection by matching
    # `connections.provider` to the source name, and no provider is called
    # `fault:reconnect`. So it reports the never-connected variant, honestly.
    # The connection-carrying variant is pinned by
    # `test_connection_routes.py::test_every_advertised_reconnect_url_resolves`,
    # which registers its source as `gmail` and does get a connection attached.
    #
    # What this test grades is the property both share and that headline 4 is
    # about: **an action, not an error**.
    assert revoked["error"]["code"] in {
        "connection_needs_reconnect",
        "connection_not_connected",
    }
    # There must be somewhere to go. An error with no action is a dead end —
    # so the URL is followed rather than pattern-matched, which is the only way
    # the 404 this project shipped twice would ever have shown up.
    action = revoked["error"]["action_url"]
    followed = await client.get(action, headers=headers)  # type: ignore[attr-defined]
    assert followed.status_code != 404, (
        f"the repair we offered, {action!r}, answered 404 — the one action that "
        "fixes a revoked grant led nowhere"
    )
    assert "invalid_grant" in revoked["error"]["message"]

    # And the healthy source is unaffected — one revoked connection does not
    # take the search down with it.
    healthy = next(row for row in snapshot["sources"] if row["source"] == "web")
    assert healthy["status"] == "done"
    assert snapshot["results"], "results from the healthy source were lost"


async def test_a_failing_source_is_a_failed_source_not_an_empty_search() -> None:
    """risks.md R16 — a throttled source must never look like an empty one."""
    key = await make_api_key()
    user_id = int(str(key["user_id"]))
    async with session_scope() as session:
        plan = await plan_search(
            session,
            user_id=user_id,
            query="anything",
            sources=["web", "fault:permanent"],
        )
        await session.commit()

    while (await runtime.run_once(limit=10)).claimed:
        pass

    snapshot = await load_snapshot(search_id=plan.search_id, user_id=user_id)
    assert snapshot is not None
    statuses = {row["source"]: row["status"] for row in snapshot.sources}
    assert statuses == {"web": "done", "fault:permanent": "failed"}
    failed = next(row for row in snapshot.sources if row["status"] == "failed")
    assert failed["error_detail"], (
        "a failed source with no reason is indistinguishable from an empty one"
    )
    # The search is *finished* even though a source died — `finished_at` means
    # "no source will change again", including the ones that failed.
    assert snapshot.finished
