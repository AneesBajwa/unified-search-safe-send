"""Walking skeleton API.

Phase 0's entire feature set is ``/health``, and it earns its keep by doing a
real database round trip: if this returns ``ok`` from the deployed SPA, then
Cloud Run, the Neon pooled endpoint, TLS, the linux/amd64 image and the
migration have all been proven together.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from core import __version__
from core.config import get_settings
from core.db import dispose_engine, session_scope
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    # Note we do NOT create the engine here beyond what a request needs — it is
    # built lazily inside the running loop on first use (core.db.get_engine).
    yield
    await dispose_engine()


app = FastAPI(
    title="Unified Search & Safe Send",
    version=__version__,
    lifespan=lifespan,
)

# The SPA is served from a different origin in every environment (Vite on 5173
# locally, Firebase Hosting in production), so it is always cross-origin.
_origins = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,  # design D10: X-API-Key only, never a cookie path
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness plus a real database round trip.

    Reports degraded rather than raising, so a deployed SPA can render *why*
    the backend is unhappy instead of showing an opaque 500.
    """
    payload: dict[str, Any] = {
        "status": "ok",
        "service": "api",
        "version": __version__,
        "database": {"connected": False},
    }
    try:
        async with session_scope() as session:
            server_version = (await session.execute(text("SHOW server_version"))).scalar_one()
            migration = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            user_count = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()
        payload["database"] = {
            "connected": True,
            "server_version": server_version,
            "migration": migration,
            "users": user_count,
        }
    except Exception as exc:  # noqa: BLE001 - health must never raise
        logger.warning("health check database probe failed: %s", exc)
        payload["status"] = "degraded"
        payload["database"] = {"connected": False, "error": f"{type(exc).__name__}: {exc}"}
        return JSONResponse(payload, status_code=503)

    return JSONResponse(payload)
