"""The job runtime against a real Postgres 17.

Every test here needs a real database. The claim's correctness is entirely a
property of how PostgreSQL executes it — ``SKIP LOCKED``, the materialized CTE,
the partial unique indexes — and none of that survives being mocked. A fake
queue would pass all of these and prove nothing.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator

import pytest
from conftest import make_adapter_run, make_user
from core.config import get_settings
from core.db import dispose_engine, session_scope
from core.enums import ErrorClass, JobKind, JobState
from core.jobs import queue, runtime
from core.jobs.queue import ClaimedJob
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("clean_db")

#: Generous on purpose. These bound a *hang*, not the behaviour under test —
#: the threads reach the barrier in milliseconds when the machine is idle. They
#: were 10 s and 20 s, which a laptop running a container build alongside the
#: suite could exceed, turning a timeout into what looked like a lost claim.
#: Both stay well inside pytest's own 30 s per-test timeout.
BARRIER_TIMEOUT = 20.0
JOIN_TIMEOUT = 25.0


async def _enqueue_adapter_job(source: str = "web", **kwargs: object) -> tuple[int, uuid.UUID]:
    user_id = await make_user()
    run_id = await make_adapter_run(user_id, source=source)
    async with session_scope() as session:
        job_id = await queue.enqueue(
            session,
            kind=JobKind.ADAPTER_RUN,
            ref_id=run_id,
            **kwargs,  # type: ignore[arg-type]
        )
        await session.commit()
    assert job_id is not None
    return job_id, run_id


async def _job(job_id: int) -> dict[str, object]:
    async with session_scope() as session:
        row = (
            await session.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id})
        ).one()
    return dict(row._mapping)


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Swap the global handler/reconciler registries for the duration of a test."""
    handlers = dict(runtime.HANDLERS)
    reconcilers = dict(runtime.RECONCILERS)
    try:
        yield
    finally:
        runtime.HANDLERS.clear()
        runtime.HANDLERS.update(handlers)
        runtime.RECONCILERS.clear()
        runtime.RECONCILERS.update(reconcilers)


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


async def test_claim_never_over_claims(_: None = None) -> None:
    """Regression for risks.md R26 — the over-claiming form took 99 rows for 1.

    The broken shape is ``WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED LIMIT n)``:
    the planner puts ``Limit -> LockRows`` in the inner loop of a Nested Loop
    Semi Join, so the subquery runs once per outer row and the LIMIT applies
    per-iteration. Everything about it looks right, and it silently drains the
    queue into one worker.
    """
    user_id = await make_user()
    run_ids = [await make_adapter_run(user_id) for _ in range(20)]
    async with session_scope() as session:
        for run_id in run_ids:
            await queue.enqueue(session, kind=JobKind.ADAPTER_RUN, ref_id=run_id)
        await session.commit()

    async with session_scope() as session:
        claimed = await queue.claim(session, worker_id="w1", limit=1, lease_seconds=30)
        await session.commit()
    assert len(claimed) == 1

    async with session_scope() as session:
        claimed_five = await queue.claim(session, worker_id="w2", limit=5, lease_seconds=30)
        await session.commit()
    assert len(claimed_five) == 5

    async with session_scope() as session:
        still_ready = await session.scalar(text("SELECT count(*) FROM jobs WHERE state = 'ready'"))
    assert still_ready == 14


async def test_two_concurrent_workers_claim_one_job_exactly_once() -> None:
    """openspec task 3.10, the asyncio half.

    Each worker gets its own session — and therefore its own connection — so
    ``SKIP LOCKED`` is actually being exercised. Sharing one session would
    serialize them and the test would pass without proving anything.
    """
    job_id, _ = await _enqueue_adapter_job()

    async def worker(name: str) -> list[int]:
        async with session_scope() as session:
            claimed = await queue.claim(session, worker_id=name, limit=5, lease_seconds=30)
            await session.commit()
        return [job.id for job in claimed]

    a, b = await asyncio.gather(worker("worker-a"), worker("worker-b"))
    assert sorted(a + b) == [job_id], "the job was claimed by both workers"

    row = await _job(job_id)
    assert row["state"] == JobState.RUNNING
    assert row["attempts"] == 1, "one claim, one attempt increment"


async def test_two_workers_in_real_os_threads_claim_one_job_exactly_once() -> None:
    """The same guarantee under real threads released by a barrier.

    ``asyncio.gather`` interleaves at await points on a single thread, which is
    weaker than genuine parallelism. Each thread here runs its own event loop
    and therefore its own engine and pool — which is also an incidental check
    that the per-loop engine caching does the right thing.
    """
    job_id, _ = await _enqueue_adapter_job()

    barrier = threading.Barrier(2)
    results: list[list[int]] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def run_worker(name: str) -> None:
        async def go() -> None:
            try:
                async with session_scope() as session:
                    # Warm the connection first so the barrier releases into the
                    # claim itself rather than into connection setup.
                    await session.execute(text("SELECT 1"))
                    barrier.wait(timeout=BARRIER_TIMEOUT)
                    claimed = await queue.claim(session, worker_id=name, limit=5, lease_seconds=30)
                    await session.commit()
                with lock:
                    results.append([job.id for job in claimed])
            finally:
                await dispose_engine()

        # 🔴 A thread that dies here used to vanish: the traceback went to
        # stderr, `results` was simply short, and the assertion below reported
        # "expected exactly one claim" — which reads as a *concurrency* failure
        # when the real cause was a barrier timeout on a loaded machine. The
        # cause has to survive the thread boundary, or the next person debugs
        # the claim query instead of their laptop.
        try:
            asyncio.run(go())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=run_worker, args=(f"thread-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)

    still_running = [t.name for t in threads if t.is_alive()]
    assert not still_running, f"workers did not finish within {JOIN_TIMEOUT}s: {still_running}"
    assert not failures, f"a worker thread raised rather than claiming: {failures!r}"

    claimed_ids = sorted(job_id for batch in results for job_id in batch)
    assert claimed_ids == [job_id], f"expected exactly one claim, got {results}"


async def test_a_partition_key_admits_one_running_job_at_a_time() -> None:
    """Sends are gated to one in flight per connection; adapter runs are not.

    Both halves matter. Serializing sends is what keeps ordering sane per
    mailbox; leaving adapter runs ungated is what keeps fan-out parallel, which
    is graded behaviour.
    """
    user_id = await make_user()
    runs = [await make_adapter_run(user_id) for _ in range(3)]
    async with session_scope() as session:
        for run_id in runs:
            await queue.enqueue(
                session,
                kind=JobKind.ADAPTER_RUN,
                ref_id=run_id,
                partition_key="gmail:1",
            )
        await session.commit()

    async with session_scope() as session:
        first = await queue.claim(session, worker_id="w1", limit=10, lease_seconds=30)
        await session.commit()
    assert len(first) == 1, "only one job per partition may run at a time"

    async with session_scope() as session:
        second = await queue.claim(session, worker_id="w2", limit=10, lease_seconds=30)
        await session.commit()
    assert second == [], "the partition is still occupied"

    async with session_scope() as session:
        await queue.succeed(session, first[0].id, worker_id="w1")
        await session.commit()

    async with session_scope() as session:
        third = await queue.claim(session, worker_id="w3", limit=10, lease_seconds=30)
        await session.commit()
    assert len(third) == 1, "the partition frees up once the first job finishes"


async def test_a_dedupe_key_blocks_a_duplicate_live_job_but_not_a_later_one() -> None:
    user_id = await make_user()
    run_id = await make_adapter_run(user_id)
    async with session_scope() as session:
        first = await queue.enqueue(
            session, kind=JobKind.ADAPTER_RUN, ref_id=run_id, dedupe_key="search:abc"
        )
        duplicate = await queue.enqueue(
            session, kind=JobKind.ADAPTER_RUN, ref_id=run_id, dedupe_key="search:abc"
        )
        await session.commit()
    assert first is not None
    assert duplicate is None, "a live job with this key already exists"

    # Once it is finished the key is free again — that is what makes "re-run
    # this search" work without inventing a second key.
    async with session_scope() as session:
        await session.execute(
            text("UPDATE jobs SET state='succeeded', lease_expires_at=NULL WHERE id=:id"),
            {"id": first},
        )
        again = await queue.enqueue(
            session, kind=JobKind.ADAPTER_RUN, ref_id=run_id, dedupe_key="search:abc"
        )
        await session.commit()
    assert again is not None


# ---------------------------------------------------------------------------
# Execution, retry and parking
# ---------------------------------------------------------------------------


async def test_a_job_runs_to_success_and_records_the_worker() -> None:
    job_id, run_id = await _enqueue_adapter_job()

    report = await runtime.run_once(worker_id="host-1:42", limit=5)
    assert report.claimed == 1
    assert report.succeeded == 1

    row = await _job(job_id)
    assert row["state"] == JobState.SUCCEEDED
    assert row["attempts"] == 1
    assert row["claimed_by"] == "host-1:42"
    assert row["finished_at"] is not None
    # jobs_lease_consistent: a finished job cannot still hold a lease.
    assert row["lease_expires_at"] is None

    async with session_scope() as session:
        run_status = await session.scalar(
            text("SELECT status FROM adapter_runs WHERE id = :id"), {"id": run_id}
        )
        finished = await session.scalar(
            text(
                "SELECT finished_at FROM searches WHERE id = "
                "(SELECT search_id FROM adapter_runs WHERE id = :id)"
            ),
            {"id": run_id},
        )
    assert run_status == "done"
    assert finished is not None, "the search closes once no source is outstanding"


@pytest.mark.headline
async def test_transient_failure_reschedules_with_jitter_and_keeps_the_attempt() -> None:
    """A retry moves `run_at` into the future and records a countdown.

    `backoff_seconds` is persisted purely so the UI can render a real countdown
    instead of an indeterminate spinner — the schedule itself is only ever
    `attempts` plus `run_at`.
    """
    job_id, run_id = await _enqueue_adapter_job(source="fault:transient")

    report = await runtime.run_once(worker_id="host-1:42", limit=5)
    assert report.retried == 1

    row = await _job(job_id)
    assert row["state"] == JobState.READY
    assert row["attempts"] == 1
    assert row["last_error_class"] == ErrorClass.TRANSIENT
    assert row["backoff_seconds"] is not None and row["backoff_seconds"] >= 0
    assert row["run_at"] > row["created_at"], "the retry is scheduled into the future"
    assert row["last_error_detail"] and "injected transient failure" in row["last_error_detail"]
    # started_at survives the retry, so end-to-end latency stays measurable.
    assert row["started_at"] is not None

    async with session_scope() as session:
        run_status = await session.scalar(
            text("SELECT status FROM adapter_runs WHERE id = :id"), {"id": run_id}
        )
    assert run_status == "failed", "per-source status survives the job rollback"


@pytest.mark.headline
async def test_permanent_failure_schedules_no_retry() -> None:
    """No backoff, no future `run_at` — the reason is surfaced immediately."""
    job_id, _ = await _enqueue_adapter_job(source="fault:permanent")

    report = await runtime.run_once(worker_id="host-1:42", limit=5)
    assert report.failed == 1
    assert report.retried == 0

    row = await _job(job_id)
    assert row["state"] == JobState.FAILED
    assert row["last_error_class"] == ErrorClass.PERMANENT
    assert row["backoff_seconds"] is None
    assert row["finished_at"] is not None


async def test_transient_failure_at_the_ceiling_parks_rather_than_fails() -> None:
    """`parked` is a terminal *transient* state, deliberately distinct from `failed`.

    "We gave up retrying" and "the provider told us no" are different facts, and
    only one of them is worth an operator retry.
    """
    job_id, _ = await _enqueue_adapter_job(source="fault:transient")

    # MAX_ATTEMPTS is 2 under test. Drive both attempts, clearing the backoff
    # delay between them so the test does not wait out real jitter.
    for _ in range(2):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE jobs SET run_at = now() WHERE id = :id"), {"id": job_id}
            )
            await session.commit()
        await runtime.run_once(worker_id="host-1:42", limit=5)

    row = await _job(job_id)
    assert row["attempts"] == 2
    assert row["state"] == JobState.PARKED
    assert row["last_error_class"] == ErrorClass.TRANSIENT


async def test_operator_retry_resumes_the_same_record() -> None:
    """openspec task 3.7 — one record, continuing attempt count, no duplicate work."""
    job_id, _ = await _enqueue_adapter_job(source="fault:transient")
    for _ in range(2):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE jobs SET run_at = now() WHERE id = :id"), {"id": job_id}
            )
            await session.commit()
        await runtime.run_once(worker_id="host-1:42", limit=5)

    parked = await _job(job_id)
    assert parked["state"] == JobState.PARKED
    attempts_before = parked["attempts"]

    async with session_scope() as session:
        assert await queue.operator_retry(session, job_id)
        await session.commit()

    resumed = await _job(job_id)
    assert resumed["state"] == JobState.READY
    assert resumed["attempts"] == attempts_before, "the count continues; it does not reset"
    assert resumed["finished_at"] is None

    async with session_scope() as session:
        job_count = await session.scalar(text("SELECT count(*) FROM jobs"))
    assert job_count == 1, "operator retry reuses the record rather than enqueueing a new one"


async def test_operator_retry_refuses_a_job_that_is_not_terminal() -> None:
    job_id, _ = await _enqueue_adapter_job()
    async with session_scope() as session:
        assert not await queue.operator_retry(session, job_id)
        await session.commit()


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


async def test_a_queued_job_survives_a_worker_restart() -> None:
    """openspec task 3.11 — durability, not liveness.

    The engine is disposed between enqueue and execution, which is as close to
    "the process went away" as a test can get without forking. The work is a row
    in the database, so nothing about it was bound to the process that created
    it.
    """
    job_id, _ = await _enqueue_adapter_job()

    # The "restart": drop the pool the enqueue ran on.
    await dispose_engine()

    row = await _job(job_id)
    assert row["state"] == JobState.READY, "still queued after the restart"

    report = await runtime.run_once(worker_id="restarted-worker:1", limit=5)
    assert report.succeeded == 1
    assert (await _job(job_id))["state"] == JobState.SUCCEEDED


async def test_a_crashed_claim_leaves_evidence_and_is_reclaimed() -> None:
    """risks.md R28 — the reason claims are leased rather than transaction-held.

    Under transactional locking a crash rolls the row back to `ready` with
    `attempts` un-incremented, so the next worker sees a pristine job and simply
    runs it. For an adapter run that is harmless; for a send it is a silent
    double delivery with nothing recording that it happened.
    """
    job_id, _ = await _enqueue_adapter_job()

    async with session_scope() as session:
        claimed = await queue.claim(session, worker_id="doomed:99", limit=1, lease_seconds=1)
        await session.commit()
    assert len(claimed) == 1

    crashed = await _job(job_id)
    assert crashed["state"] == JobState.RUNNING
    assert crashed["attempts"] == 1, "the attempt is committed, not rolled back"
    assert crashed["claimed_by"] == "doomed:99", "the crash names its worker"

    await asyncio.sleep(1.2)  # the lease expires

    report = await runtime.sweep()
    assert report.stale == 1
    assert report.requeued == 1, "adapter runs are idempotent, so RETRY is correct"

    reclaimed = await _job(job_id)
    assert reclaimed["state"] == JobState.READY
    assert reclaimed["attempts"] == 1, "the crashed attempt is not erased"


async def test_an_orphaned_side_effecting_job_is_parked_not_re_executed(
    isolated_registry: None,
) -> None:
    """The fail-safe default, exercised on the kind that actually has side effects.

    `send` now *has* a reconciler (task 5.7), so the registration is removed for
    the duration of this test and restored by `isolated_registry`. That is not a
    weaker test than it was — it is the same one, aimed at what it was always
    about: the behaviour a **future** side-effecting kind inherits when someone
    adds it and forgets. The sweeper must refuse to re-dispatch rather than
    assume the message probably did not land.
    """
    runtime.RECONCILERS.pop(JobKind.SEND, None)
    user_id = await make_user()
    run_id = await make_adapter_run(user_id)
    async with session_scope() as session:
        job_id = await queue.enqueue(session, kind=JobKind.SEND, ref_id=run_id)
        await session.commit()
    assert job_id is not None

    async with session_scope() as session:
        await queue.claim(session, worker_id="doomed:99", limit=1, lease_seconds=1)
        await session.commit()

    await asyncio.sleep(1.2)
    report = await runtime.sweep()

    assert report.parked == 1
    assert report.requeued == 0, "a side-effecting job must never be blindly re-run"

    row = await _job(job_id)
    assert row["state"] == JobState.PARKED
    assert "reconciler" in str(row["last_error_detail"])


async def test_a_registered_reconciler_decides_whether_to_re_dispatch(
    isolated_registry: None,
) -> None:
    """When reconciliation says "the side effect did not happen", the job runs again."""
    user_id = await make_user()
    run_id = await make_adapter_run(user_id)
    async with session_scope() as session:
        job_id = await queue.enqueue(session, kind=JobKind.SEND, ref_id=run_id)
        await session.commit()

    calls: list[int] = []

    async def reconciler(_session: object, job: ClaimedJob) -> bool:
        calls.append(job.id)
        return True  # the provider has no record of it; safe to dispatch

    runtime.RECONCILERS[JobKind.SEND] = reconciler  # type: ignore[assignment]

    async with session_scope() as session:
        await queue.claim(session, worker_id="doomed:99", limit=1, lease_seconds=1)
        await session.commit()
    await asyncio.sleep(1.2)

    report = await runtime.sweep()
    assert calls == [job_id]
    assert report.reconciled == 1
    assert report.requeued == 1
    assert (await _job(job_id or 0))["state"] == JobState.READY


async def test_an_inconclusive_reconciliation_parks_rather_than_guesses(
    isolated_registry: None,
) -> None:
    user_id = await make_user()
    run_id = await make_adapter_run(user_id)
    async with session_scope() as session:
        job_id = await queue.enqueue(session, kind=JobKind.SEND, ref_id=run_id)
        await session.commit()

    async def reconciler(_session: object, _job: ClaimedJob) -> bool:
        return False  # we could not establish what happened

    runtime.RECONCILERS[JobKind.SEND] = reconciler  # type: ignore[assignment]

    async with session_scope() as session:
        await queue.claim(session, worker_id="doomed:99", limit=1, lease_seconds=1)
        await session.commit()
    await asyncio.sleep(1.2)

    report = await runtime.sweep()
    assert report.parked == 1
    assert (await _job(job_id or 0))["state"] == JobState.PARKED


async def test_a_live_lease_is_not_reclaimed() -> None:
    """Reclaiming a live job is strictly worse than reclaiming a dead one slowly.

    The lease is 3x the per-job deadline for exactly this reason.
    """
    await _enqueue_adapter_job()
    async with session_scope() as session:
        await queue.claim(session, worker_id="busy:1", limit=1, lease_seconds=60)
        await session.commit()

    report = await runtime.sweep()
    assert report.stale == 0


async def test_the_sweeper_is_singleton_gated() -> None:
    """openspec task 3.8c — `pg_try_advisory_xact_lock`, transaction-scoped.

    Session-scoped advisory locks do not survive a transaction-mode pooler, and
    a blocking acquire would just queue the second sweeper up to redo the first
    one's work. The second caller reports that it did nothing rather than
    silently succeeding.
    """
    holder_has_lock = asyncio.Event()
    release = asyncio.Event()

    async def hold_the_lock() -> None:
        async with session_scope() as session:
            got = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": runtime.SWEEP_ADVISORY_KEY},
            )
            assert got is True
            holder_has_lock.set()
            await release.wait()
            await session.rollback()  # transaction ends, lock evaporates

    holder = asyncio.create_task(hold_the_lock())
    await holder_has_lock.wait()

    report = await runtime.sweep()
    assert report.held_lock is False, "a second sweeper must back off, not queue"

    release.set()
    await holder
    assert (await runtime.sweep()).held_lock is True


# ---------------------------------------------------------------------------
# Session discipline and bookkeeping
# ---------------------------------------------------------------------------


async def test_each_gathered_job_gets_its_own_session(isolated_registry: None) -> None:
    """openspec task 1.4d.

    Sharing one `AsyncSession` across `asyncio.gather` interleaves statements on
    a single connection, and the resulting failures look like data corruption
    rather than like a session-scoping mistake. Asserting on the *connection*
    rather than the session object is what makes this meaningful — two sessions
    over one connection would be just as broken.
    """
    user_id = await make_user()
    for _ in range(3):
        run_id = await make_adapter_run(user_id)
        async with session_scope() as session:
            await queue.enqueue(session, kind=JobKind.ADAPTER_RUN, ref_id=run_id)
            await session.commit()

    seen: list[tuple[int, int]] = []
    ready = asyncio.Event()
    proceed = asyncio.Event()

    async def handler(session: object, _job: ClaimedJob) -> None:
        conn = await session.connection()  # type: ignore[attr-defined]
        seen.append((id(session), id(conn.sync_connection)))
        if len(seen) == 3:
            ready.set()
        # Hold all three open at once, so they must genuinely be concurrent
        # rather than sequential reuse of one pooled connection.
        await asyncio.wait_for(proceed.wait(), timeout=10)

    runtime.HANDLERS[JobKind.ADAPTER_RUN] = handler  # type: ignore[assignment]

    task = asyncio.create_task(runtime.run_once(worker_id="w:1", limit=3))
    await asyncio.wait_for(ready.wait(), timeout=10)
    proceed.set()
    report = await task

    assert report.claimed == 3
    assert len({session_id for session_id, _ in seen}) == 3, "sessions were shared"
    assert len({conn_id for _, conn_id in seen}) == 3, "connections were shared"


async def test_a_full_batch_can_open_a_second_session_without_deadlocking(
    isolated_registry: None,
) -> None:
    """🔴 The pool must fit a whole batch **twice over**.

    Every real handler holds its own session for the length of the job (task
    1.4d) and then opens a *second* one for bookkeeping — ``run_adapter`` marks
    the run ``running`` in its own transaction so progress is visible before the
    job ends, and ``_record_run_failure`` writes the failure in another. So a
    claimed batch of N needs 2N connections, not N.

    Phase 5 found the version where it did not: ``pool_size=5`` against
    ``job_batch_size=5`` deadlocked the instant a full batch was claimed — five
    jobs holding five connections, every one waiting on a sixth that could only
    come from a peer finishing. It presented as *"a search never finished, is a
    worker running?"*, the lease expired, the batch was reclaimed, and it did it
    again. ``make smoke`` took 32-62s at the default batch and 1.8s at
    ``JOB_BATCH_SIZE=2`` — same code, same data.

    This test reproduces the shape rather than asserting a pool number, because
    the number is a consequence and the shape is the rule.
    """
    batch = get_settings().job_batch_size
    user_id = await make_user()
    for _ in range(batch):
        run_id = await make_adapter_run(user_id)
        async with session_scope() as session:
            await queue.enqueue(session, kind=JobKind.ADAPTER_RUN, ref_id=run_id)
            await session.commit()

    bookkept = 0

    async def handler(session: object, _job: ClaimedJob) -> None:
        nonlocal bookkept
        # Hold the job's own connection, exactly as a real handler does...
        await session.connection()  # type: ignore[attr-defined]
        # ...and then ask for a second one while still holding the first.
        async with session_scope() as second:
            await second.execute(text("SELECT 1"))
        bookkept += 1

    runtime.HANDLERS[JobKind.ADAPTER_RUN] = handler  # type: ignore[assignment]

    # Generous, because the failure mode is a hang rather than an error: with an
    # undersized pool this never returns and the timeout is the only signal.
    report = await asyncio.wait_for(runtime.run_once(worker_id="w:1", limit=batch), timeout=30)

    assert report.claimed == batch
    assert bookkept == batch, (
        f"only {bookkept} of {batch} jobs got a second connection — the pool "
        "cannot fit a full batch twice over, so a full batch deadlocks"
    )


async def test_error_detail_is_capped_by_bytes_not_characters(
    isolated_registry: None,
) -> None:
    """openspec task 2.7e.

    The spec forbids reducing a provider error to a generic message, but a
    provider HTML error page per row would TOAST-churn the hottest table in the
    system. Multi-byte on purpose: a character-based cap is a byte cap that is
    wrong by up to 4x on any provider that returns non-ASCII, which is all of
    them.
    """
    job_id, _ = await _enqueue_adapter_job()

    async def handler(_session: object, _job: ClaimedJob) -> None:
        raise RuntimeError("💥" * 20_000)

    runtime.HANDLERS[JobKind.ADAPTER_RUN] = handler  # type: ignore[assignment]
    await runtime.run_once(worker_id="w:1", limit=1)

    detail = str((await _job(job_id))["last_error_detail"])
    assert len(detail.encode("utf-8")) <= queue.MAX_ERROR_DETAIL_BYTES
    assert detail, "capped, not discarded — the operator still gets the evidence"


async def test_an_unregistered_job_kind_fails_as_config_not_silently(
    isolated_registry: None,
) -> None:
    """A job nobody can run is our bug, and `config` says so without blaming the user."""
    job_id, _ = await _enqueue_adapter_job()
    runtime.HANDLERS.pop(JobKind.ADAPTER_RUN, None)

    report = await runtime.run_once(worker_id="w:1", limit=1)
    assert report.failed == 1

    row = await _job(job_id)
    assert row["state"] == JobState.FAILED
    assert row["last_error_class"] == ErrorClass.CONFIG


async def test_finishing_a_job_we_no_longer_own_is_refused() -> None:
    """The compare-and-swap, exercised directly.

    A worker whose lease expired mid-job must not be able to stamp `succeeded`
    over the state of the worker that legitimately took over.
    """
    job_id, _ = await _enqueue_adapter_job()
    async with session_scope() as session:
        await queue.claim(session, worker_id="owner:1", limit=1, lease_seconds=30)
        await session.commit()

    async with session_scope() as session:
        assert not await queue.succeed(session, job_id, worker_id="impostor:2")
        assert await queue.succeed(session, job_id, worker_id="owner:1")
        await session.commit()


async def test_the_lease_consistency_constraint_is_real() -> None:
    """`jobs_lease_consistent` makes an inconsistent lease unrepresentable.

    Asserted against the database rather than trusted, because the value of the
    constraint is precisely that it holds for code that has not been written yet.
    """
    import sqlalchemy.exc

    job_id, _ = await _enqueue_adapter_job()
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE jobs SET state = 'running' WHERE id = :id"), {"id": job_id}
            )
            await session.commit()


@pytest.mark.parametrize("deadline", [1])
async def test_a_wedged_handler_hits_the_job_deadline_before_the_lease(
    isolated_registry: None, deadline: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """openspec task 3.5d — the deadline is what makes the 3x lease meaningful.

    Without it a hung provider call holds its row for the whole 90-second lease
    and only the sweeper ever frees it. With it the job fails transiently on its
    own clock and backs off, and the lease goes back to being what it is for:
    the safety net for a worker that died rather than one that is merely stuck.
    """
    job_id, _ = await _enqueue_adapter_job()

    async def wedged(_session: object, _job: ClaimedJob) -> None:
        await asyncio.sleep(3600)

    runtime.HANDLERS[JobKind.ADAPTER_RUN] = wedged  # type: ignore[assignment]
    settings = get_settings()
    monkeypatch.setattr(settings, "job_deadline_seconds", deadline)

    report = await runtime.run_once(worker_id="w:1", limit=1, lease_seconds=90)
    assert report.retried == 1, "a timeout is transient, so it backs off rather than dying"

    row = await _job(job_id)
    assert row["state"] == JobState.READY
    assert row["last_error_class"] == ErrorClass.TRANSIENT
    assert row["lease_expires_at"] is None, "the row was freed without waiting for the lease"
