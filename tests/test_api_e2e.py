"""The whole product loop, driven over a real socket, with no UI (task 9.13).

    "We should be able to run a search and send a message entirely through the
    API with no UI present."

``test_route_surface.py`` proves the structural half — one credential type, no
session path, so there is no route the SPA could reach differently.
This file proves the behavioural half by doing it: mint a key, search, poll,
read results, draft, refuse without a digest, send, send again, list history,
open a failure, retry it, resolve an in-doubt send, and page through the lot.

🔴 **Over a real socket, not ``ASGITransport``.** Both obvious in-process
transports buffer the whole response body before returning (contracts.md §5), so
the SSE assertions below cannot be written against one — a stream and a single
delivery at the end are indistinguishable to a buffering client. Everything here
shares ``live_server`` for that reason, and because "it works through the API"
should be checked the way a reviewer will check it.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("clean_db", "worker_pump")

TERMINAL_SOURCE_STATUSES = {"done", "failed", "needs_reconnect"}
TERMINAL_SEND_STATES = {"delivered", "failed_permanent", "failed_transient", "uncertain"}


@pytest.fixture
def worker_pump() -> Iterator[None]:
    """A background worker, in its own thread, for the duration of a test.

    ``live_server`` runs the **Cloud Run shape** — no inline worker — so that
    tests asserting on job claims are not racing a loop claiming underneath
    them. This file needs the opposite: the whole point is that a search
    finishes and a send delivers without anyone poking anything, which is the
    durable-background-work requirement demonstrated rather than described.

    So the worker is real and runs concurrently, and nothing below ever nudges
    it — no ``/dev/work``, no direct handler call. If the jobs complete, a
    background loop claimed and ran them.

    Its own thread with its own event loop, and the engine disposed inside that
    loop: ``core.db`` caches an engine per loop, and one left behind on a dead
    loop is a pool that never returns its connections.
    """
    import asyncio
    import threading

    stop = threading.Event()

    def pump() -> None:
        from core.db import dispose_engine
        from core.jobs.runtime import run_once

        async def loop() -> None:
            try:
                while not stop.is_set():
                    await run_once()
                    await asyncio.sleep(0.05)
            finally:
                await dispose_engine()

        asyncio.run(loop())

    thread = threading.Thread(target=pump, name="test-worker", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)


def _key(client: httpx.Client, email: str | None = None) -> dict[str, str]:
    created = client.post(
        "/v1/auth/dev-login",
        json={"email": email or f"{uuid.uuid4().hex[:8]}@example.test"},
    )
    assert created.status_code == 201, created.text
    return {"X-API-Key": created.json()["key"]}


def _await_search(client: httpx.Client, auth: dict[str, str], search_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        snapshot = client.get(f"/v1/searches/{search_id}", headers=auth).json()
        if snapshot.get("finished"):
            return dict(snapshot)
        time.sleep(0.2)
    raise AssertionError(f"search {search_id} never finished")


def _await_send(client: httpx.Client, auth: dict[str, str], send_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        send = client.get(f"/v1/sends/{send_id}", headers=auth).json()
        if send.get("state") in TERMINAL_SEND_STATES:
            return dict(send)
        time.sleep(0.2)
    raise AssertionError(f"send {send_id} never reached a terminal state")


def test_the_whole_loop_runs_through_the_api_alone(live_server: str) -> None:
    """Verification step 1, as a test rather than as a thing somebody did once.

    ⚠️ Nothing here nudges the worker — see the ``worker_pump`` fixture. If the
    jobs complete, a background loop claimed and ran them, which is the durable
    background work requirement demonstrated rather than described.
    """
    with httpx.Client(base_url=live_server, timeout=20.0) as client:
        auth = _key(client)

        # There is no unauthenticated read. Checked first, because every
        # assertion after it is worthless if the key is decorative.
        assert client.get("/v1/sends").status_code == 401

        # --- search -------------------------------------------------------
        queued = client.post("/v1/searches", json={"query": "acme renewal"}, headers=auth)
        assert queued.status_code == 202, queued.text
        search_id = queued.json()["search_id"]
        assert {source["status"] for source in queued.json()["sources"]} == {"pending"}, (
            "a source was past `pending` before any worker ran, so the response "
            "did not predate the work and partial results are a rendering trick"
        )

        snapshot = _await_search(client, auth, search_id)
        assert all(
            source["status"] in TERMINAL_SOURCE_STATUSES for source in snapshot["sources"]
        )
        assert snapshot["results"]

        # --- results, read on their own ------------------------------------
        results = client.get(f"/v1/searches/{search_id}/results", headers=auth).json()
        assert results["finished"] is True
        assert [row["id"] for row in results["results"]] == [
            row["id"] for row in snapshot["results"]
        ], "the results route and the snapshot disagree about the same search"

        # --- rerun ---------------------------------------------------------
        again = client.post(f"/v1/searches/{search_id}/rerun", headers=auth)
        assert again.status_code == 202, again.text
        assert again.json()["query"] == "acme renewal"
        assert again.json()["search_id"] != search_id, (
            "a rerun overwrote the original search, destroying the record of "
            "what the sources said the first time"
        )

        # --- draft ---------------------------------------------------------
        drafted = client.post(
            "/v1/drafts",
            json={"channel": "gmail", "to": "qa@example.test", "body": "Confirming."},
            headers=auth,
        )
        assert drafted.status_code == 201, drafted.text
        draft = drafted.json()
        draft_id = draft["draft"]["id"]
        digest = draft["confirmation"]["confirm_sha256"]

        # --- the gate ------------------------------------------------------
        refused = client.post(f"/v1/drafts/{draft_id}/send", json={}, headers=auth)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "confirmation_required"

        first = client.post(
            f"/v1/drafts/{draft_id}/send", json={"confirmed_sha256": digest}, headers=auth
        )
        assert first.status_code == 201, first.text
        assert first.headers["Idempotent-Replayed"] == "false"
        send_id = first.json()["send_id"]
        delivered = _await_send(client, auth, send_id)
        assert delivered["state"] == "delivered", delivered

        # The same key again. One message, same evidence, and it says so.
        second = client.post(
            f"/v1/drafts/{draft_id}/send", json={"confirmed_sha256": digest}, headers=auth
        )
        assert second.status_code == 200
        assert second.headers["Idempotent-Replayed"] == "true"
        assert second.json()["send_id"] == send_id
        assert second.json()["provider_message_id"] == delivered["provider_message_id"], (
            "the duplicate returned a different provider_message_id — that is a "
            "second message"
        )

        # --- history -------------------------------------------------------
        history = client.get("/v1/sends", headers=auth).json()
        assert [row["send_id"] for row in history["sends"]].count(send_id) == 1
        assert history["next_cursor"] is None

        detail = client.get(f"/v1/sends/{send_id}", headers=auth).json()
        assert detail["body"] == "Confirming.", "history does not show what was transmitted"
        assert detail["is_seed"] is False


def test_a_failed_send_can_be_retried_from_history(live_server: str) -> None:
    """Verification step 1's last leg, and task 13.3.

    A transient failure offers an operator retry and a permanent one does not —
    because a permanent failure retried arrives at the same answer more slowly,
    and offering the button teaches a user that the button does nothing.
    """
    with httpx.Client(base_url=live_server, timeout=20.0) as client:
        auth = _key(client)
        transient = _stranded(client, auth, "failed_transient", "transient")
        permanent = _stranded(client, auth, "failed_permanent", "permanent")

        assert client.get(f"/v1/sends/{transient}", headers=auth).json()[
            "retryable_by_operator"
        ] is True
        assert client.get(f"/v1/sends/{permanent}", headers=auth).json()[
            "retryable_by_operator"
        ] is False

        retried = client.post(f"/v1/sends/{transient}/retry", headers=auth)
        assert retried.status_code == 200, retried.text
        assert retried.json()["state"] == "in_flight"

        # The full error text survives — untruncated, because the record a
        # customer checks is the one that has to be complete.
        detail = client.get(f"/v1/sends/{permanent}", headers=auth).json()
        assert "invalidArgument" in detail["error"]["detail"]


def test_an_uncertain_send_is_resolved_not_retried(live_server: str) -> None:
    """The two resolutions, and the refusal that sits between them.

    Retrying an in-doubt send is exactly the wrong action — it re-sends a message
    that may already have arrived — so the API refuses it by name and offers
    ``resolve`` instead. Both resolutions are driven here because the console
    offers both, and an action that 500s when a user takes it is worse than one
    that was never offered.
    """
    with httpx.Client(base_url=live_server, timeout=20.0) as client:
        auth = _key(client)

        marked = _stranded(client, auth, "uncertain", "transient")
        refused = client.post(f"/v1/sends/{marked}/retry", headers=auth)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "resolution_required"

        detail = client.get(f"/v1/sends/{marked}", headers=auth).json()
        assert set(detail["uncertainty"]["resolutions"]) == {
            "marked_delivered",
            "forced_resend",
        }

        settled = client.post(
            f"/v1/sends/{marked}/resolve",
            json={"resolution": "marked_delivered", "note": "found it in Sent"},
            headers=auth,
        )
        assert settled.status_code == 200, settled.text
        assert settled.json()["state"] == "delivered"

        after = client.get(f"/v1/sends/{marked}", headers=auth).json()
        assert after["state"] == "delivered"
        assert after["resolution"]["resolution"] == "marked_delivered"
        assert after["resolution"]["note"] == "found it in Sent"
        # 🔴 Attested by a person, not by the provider. The two are different
        # claims and this is the state where the difference is the whole content.
        assert after["provider_message_id"] == "operator-attested"
        assert "uncertainty" not in after

        # ...and the other way. A forced resend is the only path that clears
        # `dispatched_at`, because the user has answered the question the probe
        # could not: it did not arrive.
        resend = _stranded(client, auth, "uncertain", "transient")
        forced = client.post(
            f"/v1/sends/{resend}/resolve",
            json={"resolution": "forced_resend"},
            headers=auth,
        )
        assert forced.status_code == 200, forced.text
        assert forced.json()["state"] == "in_flight"

        # An already-settled send cannot be resolved twice.
        again = client.post(
            f"/v1/sends/{marked}/resolve",
            json={"resolution": "forced_resend"},
            headers=auth,
        )
        assert again.status_code == 422
        assert again.json()["error"]["code"] == "recipient_invalid"


def test_api_keys_can_be_listed_created_and_revoked(live_server: str) -> None:
    """Task 9.2, and the property that makes revocation worth having.

    The last assertion is the one that matters: a revoked key stops working
    **and fails identically to a key that never existed**, so revocation is not
    a free oracle telling a prober which keys are real.
    """
    with httpx.Client(base_url=live_server, timeout=20.0) as client:
        auth = _key(client)

        created = client.post("/v1/api-keys", json={"name": "ci"}, headers=auth)
        assert created.status_code == 201, created.text
        minted = created.json()
        assert minted["key"].startswith("sk_live_")

        # The new key works.
        second = {"X-API-Key": minted["key"]}
        assert client.get("/v1/sends", headers=second).status_code == 200

        listed = client.get("/v1/api-keys", headers=auth).json()["api_keys"]
        assert len(listed) == 2
        # 🔴 The plaintext is never in a listing. Checked against the whole
        # response body rather than field by field, because the way this breaks
        # is a field nobody thought to exclude.
        assert minted["key"] not in json.dumps(listed)
        assert all("key_hash" not in row for row in listed)
        assert sum(row["current"] for row in listed) == 1

        revoked = client.delete(f"/v1/api-keys/{minted['key_id']}", headers=auth)
        assert revoked.status_code == 200

        after = client.get("/v1/sends", headers=second)
        assert after.status_code == 401
        unknown = client.get("/v1/sends", headers={"X-API-Key": "sk_live_" + "a" * 60})
        assert after.json() == unknown.json(), (
            "a revoked key answers differently from an unknown one, which tells a "
            "prober which keys are real"
        )

        # Another user's key is not theirs to revoke, and saying `403` would
        # confirm it exists.
        stranger = _key(client)
        assert (
            client.delete(f"/v1/api-keys/{minted['key_id']}", headers=stranger).status_code
            == 404
        )


def test_sse_delivers_progress_before_the_search_finishes(live_server: str) -> None:
    """The stream, read incrementally off a real socket.

    ⚠️ This assertion is impossible over ``ASGITransport``: it buffers the body
    and hands it back whole, so "arrived early" and "arrived at the end" look
    identical (contracts.md §5). Reading frames as they land is the only version
    of this test that means anything.

    SSE is an optional enhancement carrying nothing the snapshot lacks (R8), so
    what is asserted is modest and specific: the headers that stop a proxy
    buffering it, a per-source update, and a terminating completion event.
    """
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        auth = _key(client)
        search_id = client.post(
            "/v1/searches", json={"query": "acme"}, headers=auth
        ).json()["search_id"]

        events: list[tuple[str, dict[str, Any]]] = []
        with client.stream(
            "GET", f"/v1/searches/{search_id}/events", headers=auth
        ) as stream:
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            # Without this an nginx-shaped proxy holds the response until its
            # buffer fills, turning a progress stream into one delivery at the end.
            assert stream.headers["x-accel-buffering"] == "no"
            assert "content-encoding" not in stream.headers

            name = ""
            for line in stream.iter_lines():
                if line.startswith("event: "):
                    name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    events.append((name, json.loads(line.removeprefix("data: "))))
                    if name == "search_complete":
                        break

    kinds = [name for name, _ in events]
    assert "source_update" in kinds, f"no per-source progress was streamed: {kinds}"
    assert kinds[-1] == "search_complete"
    assert events[-1][1]["search_id"] == search_id
    # Every update names a source and its status — the same facts the snapshot
    # carries, which is exactly the claim: sooner, never different.
    for name, payload in events:
        if name == "source_update":
            assert {"source", "status", "mode", "result_count"} <= set(payload)


def test_sse_requires_the_key_in_a_header_not_a_query_string(live_server: str) -> None:
    """🔴 A query-string key is written to Cloud Logging with the request URL.

    Which is how a credential ends up in a log retained far longer than the
    credential is. ``EventSource`` cannot set headers, which is precisely why
    this endpoint is so often built the insecure way — so the absence of that
    affordance is asserted rather than assumed.
    """
    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        auth = _key(client)
        search_id = client.post(
            "/v1/searches", json={"query": "acme"}, headers=auth
        ).json()["search_id"]

        key = auth["X-API-Key"]
        as_query = client.get(f"/v1/searches/{search_id}/events?api_key={key}")
        assert as_query.status_code == 401
        assert client.get(f"/v1/searches/{search_id}/events?key={key}").status_code == 401


def test_another_users_resources_are_not_found(live_server: str) -> None:
    """Verification step 5. ``404``, never ``403`` — existence is not disclosed."""
    with httpx.Client(base_url=live_server, timeout=20.0) as client:
        mine = _key(client)
        theirs = _key(client)

        search_id = client.post(
            "/v1/searches", json={"query": "private"}, headers=mine
        ).json()["search_id"]
        draft_id = client.post(
            "/v1/drafts",
            json={"channel": "gmail", "to": "qa@example.test", "body": "private"},
            headers=mine,
        ).json()["draft"]["id"]

        for path in (
            f"/v1/searches/{search_id}",
            f"/v1/searches/{search_id}/results",
            f"/v1/drafts/{draft_id}",
        ):
            response = client.get(path, headers=theirs)
            assert response.status_code == 404, f"{path} answered {response.status_code}"
            assert response.json()["error"]["code"] == "not_found"

        assert (
            client.post(f"/v1/searches/{search_id}/rerun", headers=theirs).status_code == 404
        )
        # ...and their listings never contain it.
        assert client.get("/v1/searches", headers=theirs).json()["searches"] == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stranded(
    client: httpx.Client, auth: dict[str, str], state: str, error_class: str
) -> str:
    """A real send, moved into a terminal state the runtime can genuinely reach.

    Through the gate, then a targeted UPDATE — rather than an INSERT — because
    the ``sends_*`` CHECK constraints mean a hand-written row can express states
    the system cannot produce, and a test built on one proves nothing about the
    system.
    """
    import asyncio

    drafted = client.post(
        "/v1/drafts",
        json={"channel": "gmail", "to": "qa@example.test", "body": f"stranded {state}"},
        headers=auth,
    ).json()
    sent = client.post(
        f"/v1/drafts/{drafted['draft']['id']}/send",
        json={"confirmed_sha256": drafted["confirmation"]["confirm_sha256"]},
        headers=auth,
    )
    send_id = sent.json()["send_id"]
    _await_send(client, auth, send_id)

    detail = (
        '{"error": {"code": 400, "message": "Invalid to header", '
        '"errors": [{"reason": "invalidArgument"}]}}'
        if error_class == "permanent"
        else '{"error": {"code": 503, "message": "Backend Error"}}'
    )
    asyncio.run(_force_state(send_id, state, error_class, detail))
    return str(send_id)


async def _force_state(send_id: str, state: str, error_class: str, detail: str) -> None:
    from core.db import dispose_engine, session_scope
    from sqlalchemy import text

    try:
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    UPDATE sends
                       SET state = CAST(:state AS send_state),
                           provider_message_id = NULL,
                           delivered_at = NULL,
                           dispatched_at = CASE WHEN :dispatched THEN COALESCE(dispatched_at, now())
                                                ELSE NULL END,
                           reconcile_attempts = :reconciles,
                           attempts = 2,
                           last_error_class = CAST(:ec AS error_class),
                           last_error_detail = :ed
                     WHERE id = :id
                    """
                ),
                {
                    "id": uuid.UUID(send_id),
                    "state": state,
                    # `sends_uncertain_was_dispatched` — the database refuses an
                    # uncertain row that never reached a provider, which is the
                    # constraint keeping this state honest.
                    "dispatched": state == "uncertain",
                    "reconciles": 3 if state == "uncertain" else 0,
                    "ec": error_class,
                    "ed": detail,
                },
            )
            await session.commit()
    finally:
        await dispose_engine()
