"""Test harness.

**Postgres 17, not 16.** Neon serves 17.x and the isolation-level findings this
design rests on were verified on 17.9. Testing the queue on 16 while deploying
onto 17 is how a concurrency bug gets in that will not reproduce locally.

The lease and attempt settings are deliberately tiny here (contracts.md §3
"Tests" column) so that lease expiry is a two-second wait rather than ninety.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# contracts.md §3, Tests column. Set before anything imports core.config, so the
# lru_cached Settings are built from these rather than from a developer's .env.
TEST_ENV = {
    "JOB_LEASE_SECONDS": "2",
    "SEND_LEASE_SECONDS": "2",
    "ADAPTER_DEADLINE_SECONDS": "1",
    "MAX_ATTEMPTS": "2",
    # Not tightened: tests that hold a handler open to prove session isolation
    # would otherwise race the deadline rather than the thing under test.
    "JOB_DEADLINE_SECONDS": "30",
    "SEND_DEADLINE_SECONDS": "30",
    "RUN_WORKER_INLINE": "1",
    "SLOW_ADAPTER_DELAY_MS": "0",
    "WEB_SEARCH_API_KEY": "",
    "DEV_ROUTES": "1",
    "LOG_LEVEL": "WARNING",
    # contracts.md §3: **generated per run**, never a checked-in constant. The
    # suite is the one environment where the key is disposable, and generating
    # it here means a leaked test fixture cannot decrypt anything real.
    "TOKEN_KEYRING": base64.b64encode(os.urandom(32)).decode("ascii"),
    # Point the volume-mount lookup at a path that will not exist, so tests take
    # the documented env fallback rather than silently reading a developer's
    # real mounted keyring if one is ever present.
    "TOKEN_KEYRING_PATH": "/nonexistent/test-keyring",
    # No OAuth client ids: `core.adapters.live` then registers the fixture
    # sources badged `mock` and the fake send providers, which is what the
    # phase-2 machinery tests are written against. The provider tests in
    # `test_providers.py` set these explicitly and drive `respx`.
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "SLACK_CLIENT_ID": "",
    "SLACK_CLIENT_SECRET": "",
    "OAUTH_TUNNEL_URL": "",
}

ALL_TABLES = (
    "send_resolutions",
    "jobs",
    "sends",
    "drafts",
    "search_results",
    "adapter_runs",
    "searches",
    "connections",
    "api_keys",
    "users",
)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A real Postgres 17 for the session, migrated with the real migrations.

    Sync and session-scoped on purpose: pytest-asyncio's loop scoping then has
    nothing to say about it, and the container is started once rather than per
    test. ⚠️ The ``event_loop`` fixture was *removed* in pytest-asyncio 1.0 —
    never define one; use ``loop_scope``.
    """
    # Colima publishes its socket at ~/.colima/default/docker.sock, and
    # testcontainers hands that host path to the Ryuk reaper as a bind mount —
    # which fails with `mkdir …/docker.sock: operation not supported`, an error
    # that reads like a broken Docker install rather than a path translation
    # problem. Inside the VM the socket is at the conventional location, so
    # telling testcontainers to mount *that* is the fix.
    os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")

    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as container:
        url = container.get_connection_url()
        os.environ["DATABASE_URL"] = url
        for key, value in TEST_ENV.items():
            os.environ[key] = value

        from core.config import get_settings

        get_settings.cache_clear()

        # Same registry the apps get: importing the module is what registers
        # the handlers, so a test suite that skips this import is exercising an
        # empty registry and every job fails for want of a handler.
        import core.jobs.handlers  # noqa: F401

        _migrate()
        yield url


def _migrate() -> None:
    """``alembic upgrade head`` — the same path production takes.

    Not ``SQLModel.metadata.create_all``: that would test a schema the
    migrations have never produced, and the partial indexes, CHECK constraints
    and storage parameters this design depends on live only in the migration.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "packages" / "core" / "migrations"))
    command.upgrade(cfg, "head")


@pytest.fixture
async def clean_db(database_url: str) -> AsyncIterator[None]:
    """Empty tables before the test, and a disposed pool after it.

    The pool has ``max_overflow=0`` and the engine is cached per event loop, so
    leaving one behind per test walks straight into connection exhaustion — the
    failure mode being "the 40th test hangs", which is a miserable thing to
    debug.
    """
    from core.db import dispose_engine, session_scope
    from sqlalchemy import text

    async with session_scope() as session:
        await session.execute(
            text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    try:
        yield
    finally:
        await dispose_engine()


async def make_user(email: str | None = None) -> int:
    from core.db import session_scope
    from sqlalchemy import text

    async with session_scope() as session:
        user_id = await session.scalar(
            text("INSERT INTO users (email) VALUES (:email) RETURNING id"),
            {"email": email or f"{uuid.uuid4().hex[:8]}@example.test"},
        )
        await session.commit()
    return int(user_id or 0)


async def make_adapter_run(user_id: int, source: str = "web") -> uuid.UUID:
    """A search with one adapter run, ready for a job to reference."""
    from core.db import session_scope
    from sqlalchemy import text

    async with session_scope() as session:
        search_id = await session.scalar(
            text(
                "INSERT INTO searches (user_id, query) VALUES (:u, 'test') RETURNING id"
            ),
            {"u": user_id},
        )
        run_id = await session.scalar(
            text(
                "INSERT INTO adapter_runs (search_id, source) VALUES (:s, :src) RETURNING id"
            ),
            {"s": search_id, "src": source},
        )
        await session.commit()
    assert isinstance(run_id, uuid.UUID)
    return run_id


@pytest.fixture(autouse=True)
def isolated_send_state() -> Iterator[None]:
    """Reset the two pieces of module-level state the send gate carries.

    The fake provider's delivery ledger is process-wide **on purpose** — it has
    to survive across job attempts, which is the whole point of counting
    deliveries rather than rows. That makes it leak between tests unless
    something clears it, and a leaked ledger turns "exactly one delivery" into a
    test that passes or fails depending on file order.
    """
    from core.send import crash, providers

    providers.reset_providers()
    crash.disarm_all()
    try:
        yield
    finally:
        providers.reset_providers()
        crash.disarm_all()


async def make_api_key(email: str | None = None) -> dict[str, object]:
    """A user with an API key and the phase-2 fake connections, via the real route.

    Through ``POST /v1/auth/dev-login`` rather than by inserting rows: the
    connections it provisions are a precondition for every draft, and a helper
    that wrote them directly would let the route rot without a test noticing.
    """
    from api.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(run_worker_inline=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/auth/dev-login",
            json={"email": email or f"{uuid.uuid4().hex[:8]}@example.test"},
        )
    response.raise_for_status()
    return dict(response.json())


@pytest.fixture
async def api_client() -> AsyncIterator[tuple[object, dict[str, str]]]:
    """An in-process client plus the auth header for a fresh user.

    ⚠️ In-process, so it is only valid for assertions about **what** a response
    says. Anything asserting **when** data arrives must cross a real socket —
    both Starlette's ``TestClient`` and httpx's ``ASGITransport`` buffer the
    whole body before returning (contracts.md §5). See ``live_server``.
    """
    from api.main import create_app
    from httpx import ASGITransport, AsyncClient

    key = await make_api_key()
    app = create_app(run_worker_inline=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, {"X-API-Key": str(key["key"])}


@pytest.fixture(scope="session")
def live_server(database_url: str) -> Iterator[str]:
    """A real uvicorn on a real socket, in its own thread (contracts.md §5).

    **Sync and session-scoped**, so pytest-asyncio's loop scoping has nothing to
    say about it and the server starts once. ⚠️ ``timeout_graceful_shutdown``
    defaults to ``None``, i.e. ``shutdown()`` awaits forever — any test leaving a
    stream open would wedge CI permanently. One second, explicitly.

    ``port=0`` and then reading the bound port: ``startup()`` populates
    ``self.servers`` *before* setting ``started = True``, so polling ``started``
    and then reading the port is race-free. ``capture_signals()`` no-ops off the
    main thread, which is why this can live in one at all.
    """
    import threading
    import time

    import uvicorn
    from api.main import create_app

    # The Cloud Run shape: no inline worker. Tests drive `run_once` explicitly,
    # because a background loop claiming jobs underneath a test that asserts on
    # claims is a flake factory.
    config = uvicorn.Config(
        create_app(run_worker_inline=False),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="live-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the live server never started")
        time.sleep(0.02)

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
