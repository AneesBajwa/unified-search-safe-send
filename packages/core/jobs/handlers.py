"""Job handlers.

Two kinds, and the shape of each is dictated by what a crash mid-flight costs.

``adapter_run`` is read-only, so its bookkeeping half — claim, status
transitions, close out the parent search — is the interesting part. Per-source
status honesty is a graded behaviour: a failed source and an empty source are
different facts and the UI renders them differently, so the transitions here
matter as much as the results do. Dispatch itself is one call into
``core.adapters.orchestrator``.

``send`` lives in ``core.send.handler``, imported here purely so that importing
this module still populates the whole registry — the invariant every app and the
test suite already rely on.

The phase-1 ``fault:`` seam is gone from this file. It existed because
``adapter_runs.source`` is free text and there was no registry to consult; now
the fault modes are ordinary registered adapters and an unknown source fails
with ``UnknownSource`` instead of being dispatched hopefully.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for its registration side effects: `core.send.handler` registers both
# the `send` handler and the `send` reconciler. Kept here so that "import
# core.jobs.handlers and the registry is complete" stays true — every app and
# the test suite already rely on that, and a second bootstrap module would be a
# second thing to forget.
import core.send.handler  # noqa: F401
from core.adapters import live
from core.adapters.orchestrator import close_search_if_complete, execute_adapter_run
from core.connections import service as connections
from core.db import session_scope
from core.enums import ErrorClass, JobKind
from core.errors import classify
from core.jobs.queue import ClaimedJob
from core.jobs.runtime import register_handler

logger = logging.getLogger("core.jobs.handlers")

# Same reason, one layer down: this is where the adapter registry gets
# populated. This is the one line phase 3 changed — `fakes` became `live`, and
# nothing else in the dispatch path moved. `live` keeps the `fault:` adapters
# and falls back to a fixture source for any provider without credentials,
# labelling it `mock` rather than pretending.
live.register_defaults()


@register_handler(JobKind.ADAPTER_RUN)
async def run_adapter(session: AsyncSession, job: ClaimedJob) -> None:
    run = (
        await session.execute(
            text(
                """
                SELECT r.id, r.search_id, r.source, r.connection_id, s.query
                  FROM adapter_runs r JOIN searches s ON s.id = r.search_id
                 WHERE r.id = :id
                """
            ),
            {"id": job.ref_id},
        )
    ).first()
    if run is None:
        # The run was deleted under us — nothing to do, and nothing to retry.
        logger.warning("adapter_run %s no longer exists; job %s is a no-op", job.ref_id, job.id)
        return

    # Committed in its own transaction, for two reasons that happen to coincide.
    # The visible one: "running" is a progress signal, and a status written
    # inside a transaction that only commits when the job ends is a status
    # nobody ever sees. The one that bites: holding a row lock on `adapter_runs`
    # here and then updating the same row from the failure path's own session
    # deadlocks until `transaction_timeout` fires — 60 seconds of a job looking
    # merely slow.
    await _mark_run_started(run.id)

    try:
        # Commits its own results and terminal status together. The job's
        # session is deliberately left untouched by any of it — see the
        # docstring on `execute_adapter_run`.
        await execute_adapter_run(
            run_id=run.id,
            search_id=run.search_id,
            source=run.source,
            connection_id=run.connection_id,
            query=run.query,
        )
    except Exception as exc:
        # Recorded in its own transaction, because `runtime._execute` rolls this
        # session back before it records the job failure — and per-source
        # honesty is exactly the thing that must not be lost in that rollback.
        await _record_run_failure(run.id, run.search_id, exc, job)
        raise

    # The provider accepted this connection's credential, so the connection is
    # demonstrably healthy right now. This is the only routine signal of that —
    # `mark_needs_reconnect` had no counterpart, so a connection that failed
    # once carried its `last_error_detail` forever and `last_success_at` never
    # moved off NULL. `WHERE status <> 'needs_reconnect'` inside `mark_success`
    # is what stops a revoked grant being cleared by anything but a real
    # reconnect. Sources with no connection (web) skip it.
    if run.connection_id is not None:
        await connections.record_success(run.connection_id)

    await close_search_if_complete(run.search_id)


async def _mark_run_started(run_id: object) -> None:
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE adapter_runs
                   SET status = 'running', started_at = COALESCE(started_at, now()),
                       error_class = NULL, error_detail = NULL
                 WHERE id = :id
                """
            ),
            {"id": run_id},
        )
        await session.commit()


async def _record_run_failure(
    run_id: uuid.UUID, search_id: uuid.UUID, exc: Exception, job: ClaimedJob
) -> None:
    error_class = classify(exc)
    detail = str(exc)
    # `needs_reconnect` is a run status of its own, not a flavour of failure:
    # the UI renders it as an inline reconnect action rather than an error.
    status = "needs_reconnect" if error_class.value == "needs_reconnect" else "failed"

    # Mirrors `queue.fail`'s ladder, because the job's fate is decided after
    # this returns and the search cannot be told "everyone is done" while a
    # retry is still coming. Getting this wrong in the other direction is worse
    # than it sounds: `finished_at` is what stops the UI polling, so a search
    # closed too early goes permanently quiet with a source still to report.
    will_retry = error_class is ErrorClass.TRANSIENT and job.attempts < job.max_attempts

    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE adapter_runs
                   SET status = CAST(:status AS run_status), finished_at = now(),
                       error_class = CAST(:error_class AS error_class),
                       error_detail = :detail
                 WHERE id = :id
                """
            ),
            {
                "id": run_id,
                "status": status,
                "error_class": error_class.value,
                "detail": detail,
            },
        )
        await session.commit()

    if not will_retry:
        # A search where one adapter died for good is finished, not permanently
        # in progress. Only the success path used to say so, which left exactly
        # the searches a customer most needs an answer about spinning forever.
        await close_search_if_complete(search_id)
