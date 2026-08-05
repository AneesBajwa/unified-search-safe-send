"""What is left of the dev surface: the two worker triggers.

Phase 1 mounted unauthenticated ``/dev`` routes for keys, searches and job
inspection purely so ``make smoke`` could drive the runtime through HTTP. Every
one of them now has a real, authenticated equivalent under ``/v1`` and has been
**deleted rather than deprecated** — two ways to do the same thing is how a
security property quietly stops holding, because the weaker path is the one
nobody re-reads.

These two remain because they have no ``/v1`` equivalent by design: on Cloud Run
the worker is a separate service that Cloud Tasks and Cloud Scheduler push to,
and locally the API runs the worker inline. They exist so a developer can drive
a pass by hand against a single compose service. Mounted only when
``DEV_ROUTES=1``, which is ``0`` on Cloud Run.
"""

from __future__ import annotations

from typing import Any

from core.jobs.runtime import run_once, sweep
from fastapi import APIRouter

router = APIRouter(prefix="/dev", tags=["dev"])


@router.post("/work")
async def trigger_work() -> dict[str, Any]:
    """The same pass the worker service's ``/work`` runs."""
    return (await run_once()).as_dict()


@router.post("/sweep")
async def trigger_sweep() -> dict[str, Any]:
    """The same pass the worker service's ``/sweep`` runs: recover, then drain."""
    return {"sweep": (await sweep()).as_dict(), "work": (await run_once()).as_dict()}
