"""The worker, as an HTTP service (design D4, openspec tasks 3.4 and 3.9).

Not a resident poll loop, and not for elegance: there is no configuration of a
continuously-running process on the deployment target that costs $0 — every
always-on variant lands between $25 and $45 a month, and Cloud Run *Jobs* bill a
one-minute minimum per execution, so a three-second job on a one-minute schedule
costs exactly as much as running around the clock. A Cloud Run *service* rounds
to 100 ms, so hitting an endpoint on a schedule is roughly 20x cheaper.

    ``POST /work``   Cloud Tasks push target — claim a batch and run it.
    ``POST /sweep``  Cloud Scheduler target — reclaim orphaned leases.

Both are idempotent and safe to call concurrently: ``/work`` claims with
``SKIP LOCKED`` and ``/sweep`` is singleton-gated by a transaction-scoped
advisory lock.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

# Importing the handlers module is what populates the handler registry. Without
# it every claim finds a job and immediately fails it for want of a handler.
import core.jobs.handlers  # noqa: F401
from core import __version__
from core.config import get_settings
from core.db import dispose_engine, session_scope
from core.jobs.runtime import run_once, sweep
from core.security.redaction import install_redaction
from fastapi import FastAPI
from sqlalchemy import text

logger = logging.getLogger("worker")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=get_settings().log_level)
    # The worker is where provider calls actually happen, so it is the process
    # most likely to log a token. Same rule as the API: handlers, not loggers.
    install_redaction()
    yield
    await dispose_engine()


app = FastAPI(title="Unified Search Worker", version=__version__, lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    connected = False
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        connected = True
    except Exception as exc:  # noqa: BLE001 - health must never raise
        logger.warning("worker health probe failed: %s", exc)
    return {"status": "ok", "service": "worker", "database": {"connected": connected}}


@app.post("/work")
async def work() -> dict[str, Any]:
    """Claim and process one batch.

    Returns immediately when there is nothing ready — Cloud Tasks delivery is
    at-least-once, so a duplicate push must be cheap rather than merely correct.
    """
    return (await run_once()).as_dict()


@app.post("/sweep")
async def sweep_endpoint() -> dict[str, Any]:
    """Reclaim leases orphaned by a crashed worker.

    ``held_lock: false`` means another sweeper was already running and this call
    did nothing — reported rather than hidden, so a scheduler misconfiguration
    that fires two sweeps a second is visible instead of merely wasteful.
    """
    return (await sweep()).as_dict()
