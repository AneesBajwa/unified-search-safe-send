"""The send gate (openspec tasks 5.9, 5.10, 5.10b, 5.13, 5.13b, and the decision table).

**The headline assertion is always ``provider.delivery_count == 1``**, never a
database row count. Our own table saying ``delivered`` exactly once proves
nothing about how many messages the provider received — and the number the
provider received is the only one the customer experiences.

Real Postgres throughout, via testcontainers. SQLite cannot test this: no
``ON CONFLICT`` speculative-insertion semantics and no real MVCC, so the claim
would "pass" while proving nothing about the statement that actually ships.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest
from conftest import make_api_key
from core.db import dispose_engine, session_scope
from core.enums import ProviderKind, SendState
from core.jobs import runtime
from core.send import service
from core.send.claim import claim_send
from core.send.digest import confirmation_digest
from core.send.providers import FakeSendProvider, get_provider
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("clean_db")

CHANNEL = "C024BE91L"  # resolves to #acme-renewal
BODY = "Confirming for Thursday."


def _provider() -> FakeSendProvider:
    impl = get_provider(ProviderKind.SLACK)
    assert isinstance(impl, FakeSendProvider)
    return impl


async def _make_draft(
    client: Any, headers: dict[str, str], *, body: str = BODY
) -> dict[str, Any]:
    response = await client.post(
        "/v1/drafts",
        json={"channel": "slack", "to": CHANNEL, "body": body},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _drain_jobs() -> None:
    while (await runtime.run_once(limit=10)).claimed:
        pass


# ---------------------------------------------------------------------------
# Drafts are inert
# ---------------------------------------------------------------------------


async def test_creating_a_draft_contacts_no_provider(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """Task 5.1. The confirmation step is only meaningful if the thing being
    confirmed has not already happened."""
    client, headers = api_client
    created = await _make_draft(client, headers)
    assert _provider().delivery_count == 0
    assert created["confirmation"]["confirm_sha256"]
    assert created["confirmation"]["recipient_display"] == "#acme-renewal"


async def test_the_confirmation_resolves_a_channel_id_to_a_name(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """A confirmation showing `C024BE91L` is a confirmation nobody reads."""
    client, headers = api_client
    created = await _make_draft(client, headers)
    confirmation = created["confirmation"]
    assert confirmation["warning"].startswith("This will post to #acme-renewal in ")


def test_the_digest_covers_the_recipient_not_only_the_body() -> None:
    """risks.md R6 — sending the right words to the wrong person is the worst
    failure this gate exists to prevent."""
    base = {
        "channel": ProviderKind.SLACK,
        "recipient": CHANNEL,
        "recipient_display": "#acme-renewal",
        "subject": None,
        "body": BODY,
    }
    assert confirmation_digest(**base) != confirmation_digest(
        **{**base, "recipient": "C7X2QF3AA", "recipient_display": "#sales"}
    )
    assert confirmation_digest(**base) != confirmation_digest(
        **{**base, "channel": ProviderKind.GMAIL}
    )


def test_the_digest_encoding_is_unambiguous() -> None:
    """Length-prefixed, so no rearrangement of the parts collides.

    With a plain delimiter, a body that happens to contain the delimiter could
    be split differently into the same hash — which would quietly undo the
    entire point of hashing more than the body.
    """
    a = confirmation_digest(
        channel=ProviderKind.GMAIL,
        recipient="a@b.test",
        recipient_display="a@b.test",
        subject="x",
        body="yz",
    )
    b = confirmation_digest(
        channel=ProviderKind.GMAIL,
        recipient="a@b.test",
        recipient_display="a@b.test",
        subject="xy",
        body="z",
    )
    assert a != b


# ---------------------------------------------------------------------------
# The decision table
# ---------------------------------------------------------------------------


async def test_a_send_without_a_digest_is_refused(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    client, headers = api_client
    created = await _make_draft(client, headers)
    response = await client.post(
        f"/v1/drafts/{created['draft']['id']}/send", json={}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "confirmation_required"
    assert _provider().delivery_count == 0


async def test_a_draft_edited_after_confirmation_is_refused(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """The confirm screen rendered, then the draft moved on. 422, no delivery."""
    client, headers = api_client
    created = await _make_draft(client, headers)
    stale_digest = created["confirmation"]["confirm_sha256"]

    patched = await client.patch(
        f"/v1/drafts/{created['draft']['id']}",
        json={"body": "Actually, make it Friday."},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["confirmation"]["confirm_sha256"] != stale_digest

    response = await client.post(
        f"/v1/drafts/{created['draft']['id']}/send",
        json={"confirmed_sha256": stale_digest},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "body_changed_since_confirmation"
    assert _provider().delivery_count == 0


async def test_a_reused_key_with_different_content_is_a_mismatch_not_a_replay(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """Replaying the first send's result for different content would tell the
    caller something false about what was delivered."""
    client, headers = api_client
    created = await _make_draft(client, headers)
    draft_id = created["draft"]["id"]
    first = await client.post(
        f"/v1/drafts/{draft_id}/send",
        json={"confirmed_sha256": created["confirmation"]["confirm_sha256"]},
        headers=headers,
    )
    assert first.status_code == 201

    patched = await client.patch(
        f"/v1/drafts/{draft_id}", json={"body": "different"}, headers=headers
    )
    response = await client.post(
        f"/v1/drafts/{draft_id}/send",
        json={"confirmed_sha256": patched.json()["confirmation"]["confirm_sha256"]},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_body_mismatch"


async def test_two_sequential_sends_with_one_key_produce_exactly_one_delivery(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """openspec task 5.9, and check 3 of the manual list.

    Identical responses, `idempotent_replay: true` on the second, and — the
    assertion that matters — one message at the provider.
    """
    client, headers = api_client
    created = await _make_draft(client, headers)
    draft_id = created["draft"]["id"]
    digest = created["confirmation"]["confirm_sha256"]

    first = await client.post(
        f"/v1/drafts/{draft_id}/send", json={"confirmed_sha256": digest}, headers=headers
    )
    assert first.status_code == 201
    assert first.json()["idempotent_replay"] is False
    assert first.headers["Idempotent-Replayed"] == "false"

    await _drain_jobs()

    second = await client.post(
        f"/v1/drafts/{draft_id}/send", json={"confirmed_sha256": digest}, headers=headers
    )
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.headers["Idempotent-Replayed"] == "true"

    assert first.json()["send_id"] == second.json()["send_id"]
    assert second.json()["state"] == SendState.DELIVERED.value
    assert second.json()["provider_message_id"]
    assert _provider().delivery_count == 1

    # And a third call after delivery still does not dispatch.
    await _drain_jobs()
    assert _provider().delivery_count == 1


async def test_a_duplicate_while_still_in_flight_is_a_200_not_a_409(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """The deliberate deviation from Stripe (design D5).

    Our caller includes a human double-tapping a button on a phone, for whom a
    409 is an error toast for a send that is proceeding perfectly.
    """
    client, headers = api_client
    created = await _make_draft(client, headers)
    draft_id = created["draft"]["id"]
    digest = created["confirmation"]["confirm_sha256"]

    await client.post(
        f"/v1/drafts/{draft_id}/send", json={"confirmed_sha256": digest}, headers=headers
    )
    # No worker has run, so the send is still in_flight.
    duplicate = await client.post(
        f"/v1/drafts/{draft_id}/send", json={"confirmed_sha256": digest}, headers=headers
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["state"] == SendState.IN_FLIGHT.value
    assert duplicate.json()["idempotent_replay"] is True


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.headline
async def test_n_concurrent_sends_with_one_key_produce_exactly_one_delivery() -> None:
    """openspec task 5.10, and check 2 of the manual list — the double-tap.

    🔴 **Real OS threads released by a barrier**, not only ``asyncio.gather``,
    which interleaves at await points on a single thread and is strictly weaker
    than genuine parallelism. Each thread runs its own event loop and therefore
    its own engine and pool — without that they serialize on one connection and
    the test passes vacuously.
    """
    key = await make_api_key()
    headers = {"X-API-Key": str(key["key"])}

    from api.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(run_worker_inline=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await _make_draft(client, headers)
    draft_id = created["draft"]["id"]
    digest = created["confirmation"]["confirm_sha256"]

    callers = 8
    barrier = threading.Barrier(callers)
    statuses: list[int] = []
    send_ids: list[str] = []
    lock = threading.Lock()

    def caller() -> None:
        async def go() -> None:
            try:
                thread_app = create_app(run_worker_inline=False)
                async with AsyncClient(
                    transport=ASGITransport(app=thread_app), base_url="http://test"
                ) as thread_client:
                    # Warm the pool first, so the barrier releases into the
                    # claim itself rather than into connection setup.
                    async with session_scope() as session:
                        await session.execute(text("SELECT 1"))
                    barrier.wait(timeout=20)
                    response = await thread_client.post(
                        f"/v1/drafts/{draft_id}/send",
                        json={"confirmed_sha256": digest},
                        headers=headers,
                    )
                with lock:
                    statuses.append(response.status_code)
                    send_ids.append(response.json()["send_id"])
            finally:
                await dispose_engine()

        asyncio.run(go())

    threads = [threading.Thread(target=caller, name=f"caller-{i}") for i in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(statuses) == callers, f"a caller never returned: {statuses}"
    assert sorted(statuses) == [200] * (callers - 1) + [201], (
        f"exactly one caller must own the send, got {sorted(statuses)}"
    )
    assert len(set(send_ids)) == 1, "every caller must be told about the same send"

    await _drain_jobs()
    assert _provider().delivery_count == 1

    async with session_scope() as session:
        rows = await session.scalar(text("SELECT count(*) FROM sends"))
    assert rows == 1


async def test_thirty_concurrent_claimants_yield_one_winner_and_one_row() -> None:
    """openspec task 5.10b — the claim in isolation, without the API around it.

    Worth having separately: if the end-to-end test above ever fails, this says
    immediately whether the statement or the plumbing is at fault.
    """
    key = await make_api_key()
    user_id = int(str(key["user_id"]))

    async with session_scope() as session:
        connection_id = await session.scalar(
            text(
                "SELECT id FROM connections WHERE user_id = :u AND provider = 'slack'"
            ),
            {"u": user_id},
        )
        view = await service.create_draft(
            session,
            user_id=user_id,
            channel=ProviderKind.SLACK,
            recipient=CHANNEL,
            body=BODY,
        )
        await session.commit()
    draft_id = uuid.UUID(view.draft["id"])
    idempotency_key = view.draft["idempotency_key"]
    digest = view.confirmation["confirm_sha256"]

    claimants = 30
    barrier = threading.Barrier(claimants)
    wins: list[bool] = []
    lock = threading.Lock()

    def claimant() -> None:
        async def go() -> None:
            try:
                async with session_scope() as session:
                    await session.execute(text("SELECT 1"))
                    barrier.wait(timeout=30)
                    result = await claim_send(
                        session,
                        user_id=user_id,
                        draft_id=draft_id,
                        connection_id=int(connection_id or 0),
                        provider=ProviderKind.SLACK,
                        idempotency_key=idempotency_key,
                        confirmed_sha256=digest,
                    )
                    await session.commit()
                with lock:
                    wins.append(result.won)
            finally:
                await dispose_engine()

        asyncio.run(go())

    threads = [threading.Thread(target=claimant) for _ in range(claimants)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(wins) == claimants
    assert sum(wins) == 1, f"expected exactly one winner, got {sum(wins)}"

    async with session_scope() as session:
        rows = await session.scalar(text("SELECT count(*) FROM sends"))
    assert rows == 1


# ---------------------------------------------------------------------------
# The absence that is the feature
# ---------------------------------------------------------------------------


def test_no_route_accepts_a_recipient_and_a_body_and_delivers() -> None:
    """openspec task 5.13 — the absence is the feature.

    Every path to a provider goes draft -> confirm -> send. This asserts it
    structurally, over the registered route table, so a future convenience
    endpoint cannot quietly reintroduce one.
    """
    from api.main import create_app

    app = create_app(run_worker_inline=False)
    paths = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set()) or set()
        if hasattr(route, "path")
    }
    for method, path in paths:
        bypasses = (
            method == "POST"
            and path.rstrip("/").endswith("/send")
            and "drafts" not in path
        )
        assert not bypasses, f"{method} {path} looks like a send that bypasses the draft gate"

    # And the one send route there is refuses without a digest, which the
    # decision-table tests above exercise end to end.
    openapi = app.openapi()
    send_paths = [path for path in openapi["paths"] if path.endswith("/send")]
    assert send_paths == ["/v1/drafts/{draft_id}/send"], send_paths


async def test_an_uncertain_send_is_never_auto_retried(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """openspec task 5.13b.

    An in-doubt send needs a decision, not a repeat. Offering a retry invites
    exactly the wrong action — re-sending a message that may already have
    arrived.
    """
    client, headers = api_client
    created = await _make_draft(client, headers)
    draft_id = created["draft"]["id"]
    sent = await client.post(
        f"/v1/drafts/{draft_id}/send",
        json={"confirmed_sha256": created["confirmation"]["confirm_sha256"]},
        headers=headers,
    )
    send_id = sent.json()["send_id"]

    # Drive it into `uncertain` the way the world does: dispatched, then
    # nothing conclusive ever came back.
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE sends SET state = 'uncertain', dispatched_at = now(),
                                 reconcile_attempts = 3
                 WHERE id = :id
                """
            ),
            {"id": uuid.UUID(send_id)},
        )
        await session.commit()

    # Every automatic path: the worker, and the sweeper.
    await _drain_jobs()
    await runtime.sweep()
    assert _provider().delivery_count == 0

    # And the explicit operator path refuses too, pointing at resolution.
    retry = await client.post(f"/v1/sends/{send_id}/retry", headers=headers)
    assert retry.status_code == 409
    assert "resolve" in retry.json()["error"]["message"]
    assert _provider().delivery_count == 0

    detail = await client.get(f"/v1/sends/{send_id}", headers=headers)
    body = detail.json()
    assert body["state"] == "uncertain"
    assert body["retryable_by_operator"] is False
    # The evidence a person needs to settle it themselves, in seconds.
    assert body["uncertainty"]["resolutions"] == ["marked_delivered", "forced_resend"]
    assert body["uncertainty"]["verify_url"]


async def test_a_forced_resend_leaves_runnable_work_even_though_its_job_succeeded(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """The mechanism behind "It is not there — send it again".

    🔴 This is the shape of a bug that shipped. A send reaches ``uncertain``
    because ``reconcile_send`` parks it and ``run_send`` then **returns
    normally** — so the job is ``succeeded``. ``operator_retry`` only resumes
    ``parked``/``failed``, so it matched nothing, and its result was taken on
    trust: the send went to ``in_flight`` with no job behind it and no lease for
    the sweeper to reclaim. Stranded, silently, for ever.

    Asserting the state the endpoint returns would not have caught it — the
    endpoint said ``in_flight`` and was telling the truth about the row. What
    catches it is asking whether anything is actually scheduled.
    """
    client, headers = api_client
    created = await _make_draft(client, headers)
    sent = await client.post(
        f"/v1/drafts/{created['draft']['id']}/send",
        json={"confirmed_sha256": created["confirmation"]["confirm_sha256"]},
        headers=headers,
    )
    send_id = uuid.UUID(sent.json()["send_id"])

    await _drain_jobs()  # the job runs and, having done its work, succeeds

    async with session_scope() as session:
        job_state = await session.scalar(
            text("SELECT state::text FROM jobs WHERE kind = 'send' AND ref_id = :ref"),
            {"ref": send_id},
        )
        # The precondition that made the bug reachable. If this ever becomes
        # `parked`, the fix below is still correct but this test is no longer
        # exercising the case it was written for.
        assert job_state == "succeeded", f"expected a finished job, got {job_state}"

        await session.execute(
            text(
                "UPDATE sends SET state = 'uncertain', dispatched_at = now(), "
                "reconcile_attempts = 3, provider_message_id = NULL, delivered_at = NULL "
                "WHERE id = :id"
            ),
            {"id": send_id},
        )
        await session.commit()

    resolved = await client.post(
        f"/v1/sends/{send_id}/resolve",
        json={"resolution": "forced_resend"},
        headers=headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["state"] == "in_flight"

    async with session_scope() as session:
        runnable = await session.scalar(
            text(
                "SELECT count(*) FROM jobs WHERE kind = 'send' AND ref_id = :ref "
                "AND state IN ('ready','running')"
            ),
            {"ref": send_id},
        )
    assert runnable == 1, (
        "a forced resend must leave exactly one runnable job — the send is "
        f"in_flight with {runnable} scheduled, which is how it strands"
    )


async def test_retrying_a_delivered_send_is_a_no_op_returning_the_original_result(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """openspec task 5.8."""
    client, headers = api_client
    created = await _make_draft(client, headers)
    sent = await client.post(
        f"/v1/drafts/{created['draft']['id']}/send",
        json={"confirmed_sha256": created["confirmation"]["confirm_sha256"]},
        headers=headers,
    )
    send_id = sent.json()["send_id"]
    await _drain_jobs()

    delivered = (await client.get(f"/v1/sends/{send_id}", headers=headers)).json()
    assert delivered["state"] == SendState.DELIVERED.value

    retried = await client.post(f"/v1/sends/{send_id}/retry", headers=headers)
    assert retried.status_code == 200
    assert retried.json()["provider_message_id"] == delivered["provider_message_id"]

    await _drain_jobs()
    assert _provider().delivery_count == 1


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


async def test_another_users_send_is_a_404_not_a_403(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """A 403 discloses that the row exists (api-design.md, Conventions)."""
    client, headers = api_client
    created = await _make_draft(client, headers)
    sent = await client.post(
        f"/v1/drafts/{created['draft']['id']}/send",
        json={"confirmed_sha256": created["confirmation"]["confirm_sha256"]},
        headers=headers,
    )
    other = await make_api_key()
    response = await client.get(
        f"/v1/sends/{sent.json()['send_id']}",
        headers={"X-API-Key": str(other["key"])},
    )
    assert response.status_code == 404


async def test_an_unauthenticated_call_is_refused_identically_for_every_reason(
    api_client: tuple[Any, dict[str, str]],
) -> None:
    from core.security import api_keys

    client, _ = api_client
    missing = await client.get("/v1/sends")
    malformed = await client.get("/v1/sends", headers={"X-API-Key": "sk_live_nope"})
    # Well-formed, correct checksum, simply never stored — the case a caller
    # could otherwise use to distinguish "wrong key" from "revoked key".
    unknown = await client.get(
        "/v1/sends", headers={"X-API-Key": api_keys.mint().plaintext}
    )
    assert {missing.status_code, malformed.status_code, unknown.status_code} == {401}
    assert missing.json() == malformed.json() == unknown.json()
