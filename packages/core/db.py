"""Async engine and session factory.

Every setting here is load-bearing against the deployment target (Neon free
tier behind its transaction-mode pooler, Cloud Run scaling to zero). See
openspec tasks 1.4b to 1.4d.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings

# Keyed by event loop, never a module-level singleton — see get_engine().
_engines: dict[asyncio.AbstractEventLoop, AsyncEngine] = {}
_sessionmakers: dict[asyncio.AbstractEventLoop, async_sessionmaker[AsyncSession]] = {}


def get_engine() -> AsyncEngine:
    """Return the engine for the *running* loop, creating it if needed.

    Task 1.4c: the engine must be created inside the event loop and never at
    import time. An engine built at import binds its pool to whichever loop
    happened to import the module; reusing it from a different loop raises
    "attached to a different loop" errors that present as data corruption
    rather than as a configuration mistake. This breaks specifically under
    ``RUN_WORKER_INLINE=1`` — the local-dev configuration we built for, where
    the API and the inline worker can end up on separate loops.

    ``asyncio.get_running_loop()`` raising at import time is the point: it
    turns a subtle runtime failure into an immediate, obvious one.
    """
    loop = asyncio.get_running_loop()
    engine = _engines.get(loop)
    if engine is None:
        settings = get_settings()
        engine = create_async_engine(
            settings.sqlalchemy_url,
            # Neon suspends an idle compute at 300s. Without pre-ping the first
            # job after a suspend dies on a stale pooled connection.
            pool_pre_ping=True,
            # Recycle under that 300s window so we retire connections before
            # Neon does it for us.
            pool_recycle=240,
            # 🔴 Sized from the batch, not a constant, and the factor of two is
            # the whole point.
            #
            # A claimed job holds its own `AsyncSession` for its entire
            # execution (task 1.4d) and then opens a **second** one for
            # bookkeeping — `handlers._mark_run_started` writes `status =
            # 'running'` in its own transaction so that progress is visible
            # before the job ends, and so that the failure path is not fighting
            # the job's own row lock. So a batch of N jobs needs 2N connections,
            # not N.
            #
            # With `pool_size=5` and `job_batch_size=5` that was a **guaranteed
            # deadlock** the moment a full batch was claimed: five jobs hold all
            # five connections and every one of them waits for a sixth that can
            # only come from a peer finishing. Nobody finishes. The lease
            # expires, the batch is reclaimed, and it does it again — which
            # presents as "a search never finished, is a worker running?" rather
            # than as a pool problem. Found in phase 5: `make smoke` took 32-62s
            # at the default batch and 1.8s at `JOB_BATCH_SIZE=2`, same code,
            # same data.
            pool_size=max(2 * get_settings().job_batch_size, 5),
            # Still zero. Starvation should fail loudly rather than silently
            # growing the pool until the database refuses connections — the fix
            # above is to stop *manufacturing* starvation, not to hide it.
            max_overflow=0,
            # NB: prepared_statement_cache_size=0 — mandatory behind Neon's
            # transaction-mode pooler — is carried on the URL by
            # Settings.sqlalchemy_url. SQLAlchemy rejects it as a kwarg here.
        )
        _engines[loop] = engine
    return engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    loop = asyncio.get_running_loop()
    maker = _sessionmakers.get(loop)
    if maker is None:
        maker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
        _sessionmakers[loop] = maker
    return maker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One session, one caller.

    Task 1.4d: the search fan-out must use one ``AsyncSession`` per gathered
    task. Sharing a single session across ``asyncio.gather`` interleaves
    statements on one connection and produces failures that look like data
    corruption. This context manager exists so "get a session" is always a
    fresh one rather than a shared handle passed down.
    """
    maker = get_sessionmaker()
    async with maker() as session:
        yield session


async def dispose_engine() -> None:
    """Release the running loop's pool. Called from the app lifespan."""
    loop = asyncio.get_running_loop()
    engine = _engines.pop(loop, None)
    _sessionmakers.pop(loop, None)
    if engine is not None:
        await engine.dispose()
