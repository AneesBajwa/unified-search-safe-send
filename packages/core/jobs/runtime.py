"""Job execution: the handler registry, one claim-and-run pass, and the sweeper.

Design D4 — the worker is an **HTTP handler**, not a resident poll loop. There
is no configuration of a continuously-running process on the deployment target
that costs $0, so work is *dispatched* rather than discovered: ``/work`` runs
:func:`run_once`, ``/sweep`` runs :func:`sweep`. :func:`inline_loop` exists
only so ``docker compose up`` stays a single command locally.

**No LISTEN/NOTIFY anywhere.** ``NOTIFY`` takes a global lock at commit that
serializes every commit on the instance, and it does not survive a
transaction-mode pooler regardless. Poll-only is forced, not preferred.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.db import session_scope
from core.enums import ErrorClass, JobKind
from core.errors import ProviderError, classify
from core.jobs import queue
from core.jobs.queue import ClaimedJob
from core.jobs.recovery import RecoveryPolicy, policy_for

logger = logging.getLogger("core.jobs")

#: One arbitrary-but-fixed bigint. Advisory locks share a single global
#: namespace across the database, so the value only has to be ours and stable.
SWEEP_ADVISORY_KEY = 5_357_455_000_000_001

class ReconcileOutcome(StrEnum):
    """What a reconciler concluded, and therefore what happens to the job.

    Three answers rather than two, because "the side effect did not happen" and
    "the side effect happened and I have recorded it" both mean *do not park*,
    yet they mean opposite things about whether to run the job again. Collapsing
    them would either re-dispatch a delivered message or file a resolved send
    under "awaiting an operator", and both read as bugs to whoever finds them.
    """

    #: The provider has no record. Hand the job back to `ready`.
    SAFE_TO_DISPATCH = "safe_to_dispatch"
    #: Settled — a delivery was adopted, or the row reached a terminal state.
    #: Finish the job; there is nothing left to run.
    RESOLVED = "resolved"
    #: We could not establish what happened. Park it for a human.
    INCONCLUSIVE = "inconclusive"


Handler = Callable[[AsyncSession, ClaimedJob], Awaitable[None]]
#: Returns a :class:`ReconcileOutcome`. ``bool`` is still accepted and means what
#: it used to — True is "safe to dispatch", False is "inconclusive" — so a
#: reconciler with nothing to say about the third case need not learn about it.
Reconciler = Callable[[AsyncSession, ClaimedJob], Awaitable["ReconcileOutcome | bool"]]

HANDLERS: dict[JobKind, Handler] = {}
RECONCILERS: dict[JobKind, Reconciler] = {}


def register_handler(kind: JobKind) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return decorate


def register_reconciler(kind: JobKind) -> Callable[[Reconciler], Reconciler]:
    def decorate(fn: Reconciler) -> Reconciler:
        RECONCILERS[kind] = fn
        return fn

    return decorate


@dataclass
class RunReport:
    claimed: int = 0
    succeeded: int = 0
    retried: int = 0
    failed: int = 0
    parked: int = 0
    job_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "retried": self.retried,
            "failed": self.failed,
            "parked": self.parked,
            "job_ids": self.job_ids,
        }


@dataclass
class SweepReport:
    held_lock: bool = True
    stale: int = 0
    requeued: int = 0
    reconciled: int = 0
    parked: int = 0
    resolved: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "held_lock": self.held_lock,
            "stale": self.stale,
            "requeued": self.requeued,
            "reconciled": self.reconciled,
            "parked": self.parked,
            "resolved": self.resolved,
        }


def _as_outcome(value: ReconcileOutcome | bool) -> ReconcileOutcome:
    if isinstance(value, ReconcileOutcome):
        return value
    return (
        ReconcileOutcome.SAFE_TO_DISPATCH if value else ReconcileOutcome.INCONCLUSIVE
    )


async def run_once(
    *,
    worker_id: str | None = None,
    limit: int | None = None,
    lease_seconds: int | None = None,
) -> RunReport:
    """Claim a batch and run it. One pass; the caller decides about the next.

    The claim is committed **before** any handler runs. That ordering is the
    entire basis of crash recovery: a worker that dies mid-job leaves a row that
    says "attempt 3, claimed by host:pid, lease expires at T", and the sweeper
    can act on it. Holding the claim in an open transaction would roll all of
    that away and present the next worker with a job that looks untouched.
    """
    settings = get_settings()
    worker = worker_id or queue.worker_identity()
    batch = limit if limit is not None else settings.job_batch_size
    lease = lease_seconds if lease_seconds is not None else settings.job_lease_seconds

    async with session_scope() as session:
        await queue.apply_worker_timeouts(session)
        jobs = await queue.claim(
            session,
            worker_id=worker,
            limit=batch,
            lease_seconds=lease,
            # A batch mixes kinds, so the lease has to be decided per row rather
            # than per call. Letting a send inherit the 90 s adapter lease is how
            # the sweeper ends up reconciling a send that is still in flight.
            send_lease_seconds=settings.send_lease_seconds,
        )
        await session.commit()

    report = RunReport(claimed=len(jobs), job_ids=[job.id for job in jobs])
    if not jobs:
        return report

    # One AsyncSession per gathered task (task 1.4d). Sharing one across
    # `gather` interleaves statements on a single connection and produces
    # failures that read like data corruption rather than like a bug in this
    # line. `_execute` opens its own session precisely so that cannot happen.
    outcomes = await asyncio.gather(
        *(_execute(job, worker=worker) for job in jobs), return_exceptions=True
    )
    for job, outcome in zip(jobs, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            # Only reachable if the bookkeeping itself failed — the handler's
            # own errors are caught inside `_execute`. The lease will expire and
            # the sweeper will route it.
            logger.exception("job %s bookkeeping failed", job.id, exc_info=outcome)
            report.failed += 1
        elif outcome == "succeeded":
            report.succeeded += 1
        elif outcome == "retried":
            report.retried += 1
        elif outcome == "parked":
            report.parked += 1
        else:
            report.failed += 1
    return report


async def _execute(job: ClaimedJob, *, worker: str) -> str:
    handler = HANDLERS.get(job.kind)
    if handler is None:
        async with session_scope() as session:
            await queue.fail(
                session,
                job,
                worker_id=worker,
                error_class=ErrorClass.CONFIG,
                detail=f"no handler registered for job kind {job.kind.value!r}",
            )
            await session.commit()
        return "failed"

    deadline = _deadline_for(job.kind)
    async with session_scope() as session:
        try:
            await queue.apply_worker_timeouts(session)
            # The per-job wall clock (task 3.5d). Without it the lease is the
            # only bound on a wedged handler, so a hung provider call holds its
            # row for the full 90 seconds and the "lease is 3x the deadline"
            # relationship means nothing. A TimeoutError classifies transient,
            # so the job backs off and tries again rather than dying.
            await asyncio.wait_for(handler(session, job), timeout=deadline)
            # The handler's writes and the state transition commit together, so
            # "the job succeeded" and "what the job did" can never disagree.
            applied = await queue.succeed(session, job.id, worker_id=worker)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - this IS the classification boundary
            await session.rollback()
            error_class = classify(exc, provider=_provider_hint(exc))
            retry_after = exc.retry_after if isinstance(exc, ProviderError) else None
            outcome = await queue.fail(
                session,
                job,
                worker_id=worker,
                error_class=error_class,
                detail=_detail(exc),
                retry_after=retry_after,
            )
            await session.commit()
            logger.warning(
                "job %s failed (%s): next=%s delay=%s",
                job.id,
                error_class.value,
                outcome.state.value,
                outcome.delay_seconds,
            )
            if outcome.state.value == "ready":
                return "retried"
            if outcome.state.value == "parked":
                return "parked"
            return "failed"

    if not applied:
        # We lost the row between claiming it and finishing it — its lease
        # expired and someone else legitimately took over. Say so rather than
        # reporting a success nobody recorded.
        logger.warning("job %s completed but the claim had already moved on", job.id)
        return "lost"
    return "succeeded"


async def sweep(*, limit: int = 20) -> SweepReport:
    """Recover jobs orphaned by a crashed worker (tasks 3.8, 3.8c).

    Singleton-gated by ``pg_try_advisory_xact_lock`` — **transaction-scoped**,
    the only advisory-lock variant that survives a transaction-mode pooler, and
    the only one that releases on its own when a container is preempted
    mid-sweep. ``try`` rather than a blocking acquire: a second sweeper should
    return immediately, not queue up behind the first and then redo its work.
    """
    report = SweepReport()
    async with session_scope() as session:
        await queue.apply_worker_timeouts(session)
        got = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": SWEEP_ADVISORY_KEY}
        )
        if not got:
            report.held_lock = False
            await session.rollback()
            return report

        stale = await queue.select_stale(session, limit=limit)
        report.stale = len(stale)

        for job in stale:
            policy = policy_for(job.kind)
            if policy is RecoveryPolicy.RETRY:
                if job.attempts >= job.max_attempts:
                    await queue.park(
                        session,
                        job.id,
                        detail=(
                            "lease expired after the final attempt; "
                            f"worker {job.claimed_by} did not report back"
                        ),
                    )
                    report.parked += 1
                else:
                    await queue.requeue(session, job.id)
                    report.requeued += 1
                continue

            # RECONCILE — including every unregistered kind, by design.
            reconciler = RECONCILERS.get(job.kind)
            if reconciler is None:
                # The fail-safe. A side-effecting job with no way to ask the
                # provider what happened is parked for a human, never re-run on
                # the assumption that it probably did not land.
                await queue.park(
                    session,
                    job.id,
                    detail=(
                        f"orphaned lease on job kind {job.kind.value!r} with no "
                        "reconciler registered; not re-executed because the "
                        "side effect cannot be ruled out"
                    ),
                )
                report.parked += 1
                continue

            outcome = _as_outcome(await reconciler(session, job))
            report.reconciled += 1
            if outcome is ReconcileOutcome.SAFE_TO_DISPATCH:
                await queue.requeue(session, job.id)
                report.requeued += 1
            elif outcome is ReconcileOutcome.RESOLVED:
                # The reconciler settled it — typically by adopting a delivery
                # that had already happened. Finishing the job says so; parking
                # it would file a resolved send under "awaiting an operator".
                await queue.resolve(session, job.id)
                report.resolved += 1
            else:
                await queue.park(
                    session,
                    job.id,
                    detail="reconciliation was inconclusive; awaiting an operator",
                )
                report.parked += 1

        await session.commit()
    return report


async def inline_loop(stop: asyncio.Event) -> None:
    """``RUN_WORKER_INLINE=1`` — local dev only (task 3.9).

    Cloud Run runs the worker as a service and Cloud Tasks pushes to it; this
    loop exists so a developer gets the same behaviour from ``docker compose
    up`` without standing up a task queue.
    """
    settings = get_settings()
    last_sweep = time.monotonic()
    logger.info("inline worker loop started (poll=%ss)", settings.worker_poll_seconds)
    while not stop.is_set():
        try:
            report = await run_once()
            now = time.monotonic()
            if now - last_sweep >= settings.sweep_interval_seconds:
                last_sweep = now
                await sweep()
            if report.claimed == 0:
                await _sleep_or_stop(stop, settings.worker_poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The loop has to outlive one bad pass. A worker that dies on a
            # single transient database blip and never comes back is a worse
            # failure than the blip.
            logger.exception("inline worker pass failed; continuing")
            await _sleep_or_stop(stop, settings.worker_poll_seconds)
    logger.info("inline worker loop stopped")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately on shutdown.

    A plain ``asyncio.sleep`` here makes container shutdown wait out the poll
    interval, which shows up as a slow ``docker compose down`` and, on Cloud
    Run, as a SIGKILL during the grace period.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _deadline_for(kind: JobKind) -> int:
    """The per-job wall clock, per kind.

    A send may have to *dispatch and then reconcile* inside one execution, which
    does not fit an adapter's budget. Set deliberately rather than discovered as
    an intermittent transient failure whose text points at the provider — and
    each provider client's own timeout stays comfortably inside whichever value
    applies.
    """
    settings = get_settings()
    if kind is JobKind.SEND:
        return settings.send_deadline_seconds
    return settings.job_deadline_seconds


def _provider_hint(exc: BaseException) -> str | None:
    return exc.provider if isinstance(exc, ProviderError) else None


def _detail(exc: BaseException) -> str:
    if isinstance(exc, ProviderError):
        return str(exc)
    # Full text, not a generic message — the spec is explicit that a failed job
    # must expose what the provider actually said. Truncation to 16 KiB happens
    # once, at persist time.
    import traceback

    return "".join(traceback.format_exception(exc)).strip()
