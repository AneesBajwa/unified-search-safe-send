"""The queue itself: enqueue, claim, finish, reclaim (tasks 3.3, 3.5-3.8, 3.9c).

Four things in this file look like style choices and are not. Each one fails
*only* under real concurrency, and passes every single-threaded test:

1. **The claim is a materialized CTE with ``FOR NO KEY UPDATE SKIP LOCKED``.**
   The idiomatic-looking ``WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED LIMIT n)``
   lets the planner run the subquery once per outer row, so the limit applies
   per-iteration — demonstrated claiming 99 rows when it was asked for 1 (R26).
2. **Claims are leased, never transaction-held.** Transactional locking rolls a
   crashed job back to ``ready`` with ``attempts`` un-incremented and the
   dispatch evidence erased, so the next worker sees a pristine job and
   re-dispatches: a silent double-send with nothing recording that it happened
   (R28). The claim is committed; a lease plus a sweeper handles the crash.
3. **Every terminal transition is a compare-and-swap on ``claimed_by``, with the
   rowcount checked.** A worker whose lease expired mid-job must not be able to
   overwrite the state of the worker that legitimately reclaimed it.
4. **The ``jobs`` table is never partitioned by state.** A claim ``UPDATE``
   crossing partitions raises ``40001`` and ``SKIP LOCKED`` does not suppress it.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Result, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.enums import ErrorClass, JobKind, JobState
from core.jobs.backoff import full_jitter

#: The spec forbids reducing a provider error to a generic message, but a
#: provider HTML error page per row would TOAST-churn the hottest table in the
#: system. 16 KiB keeps a real stack trace or JSON body and drops the essay.
MAX_ERROR_DETAIL_BYTES = 16 * 1024

#: Transaction-scoped guards for worker sessions (task 3.9c). All three sit well
#: under the 90 s lease, so a wedged worker frees its row before the sweeper
#: comes looking — and an abandoned transaction can never pin the xmin horizon
#: long enough to bloat the queue table.
STATEMENT_TIMEOUT = "15s"
IDLE_IN_TRANSACTION_TIMEOUT = "15s"
TRANSACTION_TIMEOUT = "60s"


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: JobKind
    ref_id: uuid.UUID
    attempts: int
    max_attempts: int
    partition_key: str | None
    dedupe_key: str | None
    claimed_by: str | None
    lease_expires_at: datetime | None
    started_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> ClaimedJob:
        return cls(
            id=row.id,
            kind=JobKind(row.kind),
            ref_id=row.ref_id,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            partition_key=row.partition_key,
            dedupe_key=row.dedupe_key,
            claimed_by=row.claimed_by,
            lease_expires_at=row.lease_expires_at,
            started_at=row.started_at,
        )


def worker_identity() -> str:
    """``host:pid`` — what lands in ``jobs.claimed_by``.

    Deliberately the *process*, not a random uuid: when a job is stuck, this is
    the string you grep the logs for.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


async def apply_worker_timeouts(session: AsyncSession) -> None:
    """Bound the current transaction (task 3.9c).

    ``SET LOCAL`` rather than ``SET``: session-level settings do not survive
    Neon's transaction-mode pooler, which hands the next transaction whichever
    backend is free. Transaction-scoped is the only variant that means anything
    here — the same reason the sweeper uses ``pg_try_advisory_xact_lock``.
    """
    await session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    await session.execute(
        text(
            "SET LOCAL idle_in_transaction_session_timeout = "
            f"'{IDLE_IN_TRANSACTION_TIMEOUT}'"
        )
    )
    await session.execute(text(f"SET LOCAL transaction_timeout = '{TRANSACTION_TIMEOUT}'"))


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------

_ENQUEUE = text(
    """
    INSERT INTO jobs (kind, ref_id, dedupe_key, partition_key, priority, run_at,
                      max_attempts)
    VALUES (:kind, :ref_id, :dedupe_key, :partition_key, :priority,
            COALESCE(:run_at, now()), :max_attempts)
    ON CONFLICT (dedupe_key)
        WHERE dedupe_key IS NOT NULL AND state IN ('ready','running')
        DO NOTHING
    RETURNING id
    """
)


async def enqueue(
    session: AsyncSession,
    *,
    kind: JobKind,
    ref_id: uuid.UUID,
    dedupe_key: str | None = None,
    partition_key: str | None = None,
    priority: int = 0,
    run_at: datetime | None = None,
    max_attempts: int | None = None,
) -> int | None:
    """Insert a job. Returns ``None`` when a live duplicate already exists.

    Deduplication is the partial unique index ``jobs_dedupe_idx``, scoped to
    ``ready`` and ``running`` — a *finished* job with the same key does not
    block a new one, which is what makes "re-run this search" work without
    inventing a second key.

    ``partition_key`` is ``'{provider}:{connection_id}'`` on sends, which the
    claim predicate turns into "at most one send in flight per connection", and
    NULL on adapter runs so fan-out stays fully parallel (task 2.7f).
    """
    result = await session.execute(
        _ENQUEUE,
        {
            "kind": kind.value,
            "ref_id": ref_id,
            "dedupe_key": dedupe_key,
            "partition_key": partition_key,
            "priority": priority,
            "run_at": run_at,
            # MAX_ATTEMPTS is 2 under test and 6 everywhere else (contracts.md
            # §3), so the ceiling has to come from settings rather than be
            # baked in — otherwise the parking test waits out six real retries.
            "max_attempts": max_attempts
            if max_attempts is not None
            else get_settings().max_attempts,
        },
    )
    row = result.first()
    return int(row.id) if row is not None else None


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

_CLAIM = text(
    """
    WITH candidate AS MATERIALIZED (
        SELECT j.id, j.partition_key, j.priority, j.run_at
          FROM jobs j
         WHERE j.state = 'ready' AND j.run_at <= now()
           AND j.attempts < j.max_attempts
           AND (j.partition_key IS NULL
                OR NOT EXISTS (SELECT 1 FROM jobs r
                                WHERE r.state = 'running'
                                  AND r.partition_key = j.partition_key))
         ORDER BY j.priority DESC, j.run_at, j.id
         LIMIT :limit
         FOR NO KEY UPDATE SKIP LOCKED
    ),
    picked AS (
        SELECT DISTINCT ON (COALESCE(c.partition_key, 'job:' || c.id::text)) c.id
          FROM candidate c
         ORDER BY COALESCE(c.partition_key, 'job:' || c.id::text),
                  c.priority DESC, c.run_at, c.id
    )
    UPDATE jobs j
       SET state = 'running', attempts = j.attempts + 1,
           claimed_at = now(), claimed_by = :worker,
           -- The lease is per *kind*, decided inside the one statement that
           -- sets it. A batch claim mixes kinds, so a single `lease_seconds`
           -- applied to the whole batch would quietly give sends the 90 s
           -- adapter lease — and the sweeper would then start reconciling
           -- sends that are still in flight, which is the one thing the
           -- 5-minute lease exists to prevent (contracts.md §3, design D5).
           -- The casts are required, not tidiness: inside a CASE the planner has
           -- no function signature to infer the parameter type from, so both
           -- arrive as `text` and `make_interval(secs => text)` does not exist.
           lease_expires_at = now() + make_interval(
               secs => CASE j.kind
                         WHEN 'send' THEN CAST(:send_lease AS double precision)
                         ELSE CAST(:lease AS double precision)
                       END),
           started_at = COALESCE(j.started_at, now()),
           updated_at = now()
      FROM picked p WHERE j.id = p.id
    RETURNING j.*
    """
)


async def claim(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 1,
    lease_seconds: int = 90,
    send_lease_seconds: int | None = None,
) -> list[ClaimedJob]:
    """Claim up to ``limit`` ready jobs, leasing them to ``worker_id``.

    ``send_lease_seconds`` defaults to ``lease_seconds`` so a caller that has no
    opinion gets the old behaviour, but every real caller has one: a send needs
    5 minutes because reconciliation is slower than a re-read.

    ``attempts < max_attempts`` is not redundant with the parking logic in
    :func:`finish`. Recovery can hand a job back to ``ready`` with its attempt
    count already at the ceiling, and the increment in this statement would then
    violate ``jobs_attempts_bounded`` — turning an exhausted job into a hard
    error on the claim path, which is where it would do the most damage.

    ``FOR NO KEY UPDATE`` rather than ``FOR UPDATE`` because the subsequent
    UPDATE does not touch the key, and the weaker lock avoids blocking inserts
    on tables whose foreign keys reference this one.

    **The ``picked`` stage is a correction to the claim query in
    ``data-model.md``, not decoration.** The ``NOT EXISTS`` guard reads rows
    that were already ``running`` when the statement began; it cannot see rows
    this same statement is about to set running. So a batch claim over two
    queued sends for one connection passes the guard for both, sets both
    running, and violates ``jobs_running_partition_uniq`` — the whole claim
    fails with an IntegrityError instead of claiming one. Entirely reachable:
    two sends to the same Gmail connection with the default batch size of 5.
    ``DISTINCT ON`` keeps one candidate per partition; the ``COALESCE`` onto the
    id keeps NULL-partition rows (every adapter run) distinct from each other,
    so fan-out is unaffected. Rows locked but not picked are released at commit
    and claimed on the next pass.

    The caller commits. The claim must be **committed before the handler runs**
    so that a crash leaves evidence behind (R28).
    """
    result = await session.execute(
        _CLAIM,
        {
            "worker": worker_id,
            "limit": limit,
            "lease": lease_seconds,
            "send_lease": send_lease_seconds
            if send_lease_seconds is not None
            else lease_seconds,
        },
    )
    return [ClaimedJob.from_row(row) for row in result.all()]


# ---------------------------------------------------------------------------
# finish
# ---------------------------------------------------------------------------

# NB on every UPDATE below: `lease_expires_at = NULL` is required, not tidiness.
# `jobs_lease_consistent` says a row is running exactly when it holds a lease,
# so leaving the lease set on a finished job is a constraint violation. We keep
# `claimed_at` and `claimed_by` deliberately — they are the forensic record of
# which worker ran the last attempt and when.

_SUCCEED = text(
    """
    UPDATE jobs
       SET state = 'succeeded', finished_at = now(), lease_expires_at = NULL,
           backoff_seconds = NULL, updated_at = now()
     WHERE id = :id AND state = 'running' AND claimed_by = :worker
    """
)

_RETRY = text(
    """
    UPDATE jobs
       SET state = 'ready', run_at = now() + make_interval(secs => :delay),
           backoff_seconds = :delay, lease_expires_at = NULL,
           last_error_class = CAST(:error_class AS error_class),
           last_error_detail = :detail, last_error_at = now(), updated_at = now()
     WHERE id = :id AND state = 'running' AND claimed_by = :worker
    """
)

_TERMINAL = text(
    """
    UPDATE jobs
       SET state = CAST(:state AS job_state), finished_at = now(),
           lease_expires_at = NULL, backoff_seconds = NULL,
           last_error_class = CAST(:error_class AS error_class),
           last_error_detail = :detail, last_error_at = now(), updated_at = now()
     WHERE id = :id AND state = 'running' AND claimed_by = :worker
    """
)


async def succeed(session: AsyncSession, job_id: int, *, worker_id: str) -> bool:
    """Compare-and-swap to ``succeeded``. False means we no longer owned it."""
    result = await session.execute(_SUCCEED, {"id": job_id, "worker": worker_id})
    return _affected(result) == 1


@dataclass(frozen=True)
class FailOutcome:
    state: JobState
    delay_seconds: float | None
    applied: bool


async def fail(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    worker_id: str,
    error_class: ErrorClass,
    detail: str,
    retry_after: float | None = None,
) -> FailOutcome:
    """Record a failure and decide what happens next.

    Only ``transient`` is ever retried. ``permanent``, ``needs_reconnect`` and
    ``config`` are recorded and surfaced immediately with no retry scheduled
    (task 3.6) — retrying a revoked grant just burns attempts on its way to the
    same answer, and hides the reconnect prompt behind five minutes of backoff.

    At the ceiling a transient failure lands in ``parked``, not ``failed``: a
    terminal *transient* state, distinct because only one of the two is worth an
    operator retry.
    """
    capped = _cap(detail)

    if error_class is not ErrorClass.TRANSIENT:
        state = JobState.FAILED
        result = await session.execute(
            _TERMINAL,
            {
                "id": job.id,
                "worker": worker_id,
                "state": state.value,
                "error_class": error_class.value,
                "detail": capped,
            },
        )
        return FailOutcome(state, None, _affected(result) == 1)

    if job.attempts >= job.max_attempts:
        state = JobState.PARKED
        result = await session.execute(
            _TERMINAL,
            {
                "id": job.id,
                "worker": worker_id,
                "state": state.value,
                "error_class": error_class.value,
                "detail": capped,
            },
        )
        return FailOutcome(state, None, _affected(result) == 1)

    # Only `attempts` and `run_at` are stored — never a materialized schedule.
    # `backoff_seconds` is persisted purely so the UI can render a countdown
    # rather than an indeterminate spinner (task 3.5c).
    delay = full_jitter(job.attempts, retry_after=retry_after)
    result = await session.execute(
        _RETRY,
        {
            "id": job.id,
            "worker": worker_id,
            "delay": delay,
            "error_class": error_class.value,
            "detail": capped,
        },
    )
    return FailOutcome(JobState.READY, delay, _affected(result) == 1)


# ---------------------------------------------------------------------------
# recovery / operator actions
# ---------------------------------------------------------------------------

_SELECT_STALE = text(
    """
    WITH stale AS MATERIALIZED (
        SELECT j.id
          FROM jobs j
         WHERE j.state = 'running' AND j.lease_expires_at < now()
         ORDER BY j.lease_expires_at
         LIMIT :limit
         FOR NO KEY UPDATE SKIP LOCKED
    )
    SELECT j.* FROM jobs j JOIN stale s ON j.id = s.id
    """
)


async def select_stale(session: AsyncSession, *, limit: int = 20) -> list[ClaimedJob]:
    """Rows whose lease has expired — a worker died holding them.

    The ``LIMIT`` lives inside the locking CTE so it can never be applied
    *after* the lock, which is the shape that quietly locks far more rows than
    it returns.
    """
    result = await session.execute(_SELECT_STALE, {"limit": limit})
    return [ClaimedJob.from_row(row) for row in result.all()]


_REQUEUE = text(
    """
    UPDATE jobs
       SET state = 'ready', run_at = now(), lease_expires_at = NULL,
           updated_at = now()
     WHERE id = :id AND state = 'running'
    """
)

_PARK = text(
    """
    UPDATE jobs
       SET state = 'parked', finished_at = now(), lease_expires_at = NULL,
           last_error_class = 'transient', last_error_detail = :detail,
           last_error_at = now(), updated_at = now()
     WHERE id = :id AND state = 'running'
    """
)


async def requeue(session: AsyncSession, job_id: int) -> bool:
    """Hand an orphaned job straight back to ``ready``.

    ``attempts`` is deliberately left where the crashed claim put it. The
    increment happened before the crash and losing it would erase the evidence
    that an attempt was made at all.
    """
    result = await session.execute(_REQUEUE, {"id": job_id})
    return _affected(result) == 1


async def park(session: AsyncSession, job_id: int, *, detail: str) -> bool:
    result = await session.execute(_PARK, {"id": job_id, "detail": _cap(detail)})
    return _affected(result) == 1


_RESOLVE = text(
    """
    UPDATE jobs
       SET state = 'succeeded', finished_at = now(), lease_expires_at = NULL,
           backoff_seconds = NULL, updated_at = now()
     WHERE id = :id AND state = 'running'
    """
)


async def resolve(session: AsyncSession, job_id: int) -> bool:
    """Finish an orphaned job the sweeper's reconciler settled.

    No ``claimed_by`` predicate, unlike :func:`succeed`: by construction the
    worker that held this claim is gone, and the sweeper is acting on its behalf
    under the advisory lock. ``state = 'running'`` is still checked, so a job
    that was reclaimed between the reconcile and this write is left alone.
    """
    result = await session.execute(_RESOLVE, {"id": job_id})
    return _affected(result) == 1


_OPERATOR_RETRY = text(
    """
    UPDATE jobs
       SET state = 'ready', run_at = now(), finished_at = NULL,
           lease_expires_at = NULL, backoff_seconds = NULL,
           max_attempts = GREATEST(max_attempts, attempts + :grant),
           updated_at = now()
     WHERE id = :id AND state IN ('parked','failed')
    RETURNING id
    """
)


async def operator_retry(
    session: AsyncSession, job_id: int, *, extra_attempts: int = 1
) -> bool:
    """Resume a terminal job under the same record (task 3.7).

    No new row and no reset of ``attempts`` — the attempt count continues from
    the previous total, so history shows "6 automatic attempts, then an operator
    tried again" rather than a job that mysteriously restarts at one. Because
    ``attempts`` is already at ``max_attempts``, the ceiling is raised by the
    granted amount; otherwise the claim predicate would skip the row it was
    just asked to run.

    For a send this is the entry point that reuses the original idempotency key
    (task 5.8) — the key lives on the ``sends`` row, which this job still
    references, so reuse is automatic rather than something to remember.
    """
    result = await session.execute(
        _OPERATOR_RETRY, {"id": job_id, "grant": extra_attempts}
    )
    return result.first() is not None


def _affected(result: Result[Any]) -> int:
    """``rowcount`` off an ``execute()``.

    ``AsyncSession.execute`` is typed as returning ``Result``, which has no
    ``rowcount``; every DML statement actually returns a ``CursorResult``, which
    does. The cast is the honest way to say that — and the rowcount matters:
    it is how a compare-and-swap reports that it lost.
    """
    return cast("CursorResult[Any]", result).rowcount


def _cap(detail: str) -> str:
    """Truncate to 16 KiB **by bytes**, not characters.

    A character-based cap is a byte-based cap that is wrong by up to 4x on any
    provider that returns non-ASCII, which is all of them.
    """
    raw = detail.encode("utf-8")
    if len(raw) <= MAX_ERROR_DETAIL_BYTES:
        return detail
    return raw[:MAX_ERROR_DETAIL_BYTES].decode("utf-8", errors="ignore")
