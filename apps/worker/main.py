"""Walking-skeleton worker.

Endpoints exist and are addressable; they claim nothing yet. The job runtime
lands in phase 1 (openspec task group 3).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from core import __version__
from core.config import get_settings
from core.db import dispose_engine, session_scope
from fastapi import FastAPI
from sqlalchemy import text

logger = logging.getLogger("worker")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=get_settings().log_level)
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker health probe failed: %s", exc)
    return {"status": "ok", "service": "worker", "database": {"connected": connected}}


@app.post("/work")
async def work() -> dict[str, Any]:
    """Cloud Tasks push target. Claims and processes ready jobs.

    Stub until phase 1: the claim is a materialized CTE with
    FOR NO KEY UPDATE SKIP LOCKED (design D3 / risks R26).
    """
    return {"claimed": 0, "processed": 0, "note": "phase-0 stub"}


@app.post("/sweep")
async def sweep() -> dict[str, Any]:
    """Cloud Scheduler target: retries, stale leases, reconciliation.

    Stub until phase 1.
    """
    return {"reclaimed": 0, "reconciled": 0, "note": "phase-0 stub"}
