"""Walking skeleton API.

Phase 0's entire feature set is ``/health``, and it earns its keep by doing a
real database round trip: if this returns ``ok`` from the deployed SPA, then
Cloud Run, the Neon pooled endpoint, TLS, the linux/amd64 image and the
migration have all been proven together.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

# Importing the handlers module registers them. The inline worker below claims
# jobs in this process, so the registry has to be populated here too.
import core.jobs.handlers  # noqa: F401
from core import __version__
from core.config import get_settings
from core.db import dispose_engine, session_scope
from core.jobs.runtime import inline_loop
from core.security.redaction import install_redaction
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api import errors as api_errors
from api.routes_dev import router as dev_router
from api.routes_v1 import router as v1_router

logger = logging.getLogger("api")


def _lifespan_for(run_worker_inline: bool) -> Any:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=get_settings().log_level)
        # 🔴 Immediately after basicConfig, because that is what creates the
        # handlers this attaches to — and the filter must be on **handlers, not
        # loggers**: a logger's own filters are not applied to records
        # propagated up from child loggers, which is exactly where a token would
        # be logged (task 6.2). Installed before the worker starts, so no job
        # can log anything before the filter exists.
        install_redaction()
        # Note we do NOT create the engine here beyond what a request needs — it
        # is built lazily inside the running loop on first use
        # (core.db.get_engine).

        stop = asyncio.Event()
        worker: asyncio.Task[None] | None = None
        if run_worker_inline:
            # Local dev only. On Cloud Run this is 0 and work arrives by Cloud
            # Tasks push — an always-allocated poller there costs $44.71/month
            # (design D4). This is also precisely the configuration that breaks
            # if the engine is created at import time, since the loop running
            # here is not the loop that imported the module.
            worker = asyncio.create_task(inline_loop(stop), name="inline-worker")

        try:
            yield
        finally:
            if worker is not None:
                stop.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await dispose_engine()

    return lifespan


def create_app(*, run_worker_inline: bool | None = None) -> FastAPI:
    """Build the app. One factory, so every caller gets the same wiring.

    ``run_worker_inline`` defaults to the setting and is overridable because the
    two real deployments already disagree about it — Cloud Run runs the worker
    as its own service, local dev runs it in-process — so it was never a
    property of *the app*, only of how the app is being run. The test suite
    takes the Cloud Run shape and drives the runtime explicitly: a background
    loop claiming jobs underneath a test that asserts on claims is a flake
    factory.
    """
    inline = (
        get_settings().run_worker_inline if run_worker_inline is None else run_worker_inline
    )
    app = FastAPI(
        title="Unified Search & Safe Send",
        version=__version__,
        lifespan=_lifespan_for(inline),
    )

    # The SPA is served from a different origin in every environment (Vite on
    # 5173 locally, Firebase Hosting in production), so it is always cross-origin.
    origins = [
        o.strip()
        for o in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,  # design D10: X-API-Key only, never a cookie path
        allow_methods=["*"],
        allow_headers=["*"],
        # Without this the SPA cannot read `Idempotent-Replayed`, which is the
        # one header the send gate's contract actually puts on the wire.
        expose_headers=["Idempotent-Replayed"],
    )

    api_errors.install(app)

    # The real, API-key-authenticated surface at its documented paths. Everything
    # the UI and `make smoke` drive goes through here — there is no
    # browser-session path and no privileged route, which is what makes "the UI
    # is a pure consumer" structurally true rather than an assertion to audit
    # (design D10).
    app.include_router(v1_router)

    if get_settings().dev_routes:
        # What is left of `/dev` is only the two worker triggers, so a single
        # `docker compose` service can be driven without a second one being
        # reachable. `/dev/api-keys`, `/dev/searches` and `/dev/jobs/*` were
        # retired the moment their real equivalents landed: two ways in is one
        # too many.
        app.include_router(dev_router)

    app.add_api_route("/health", health, methods=["GET"])
    return app


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


app = create_app()
