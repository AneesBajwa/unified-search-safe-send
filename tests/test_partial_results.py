"""Success criterion 1: a slow source does not hold up the fast ones (task 4.11).

🔴 **This test cannot be written over an in-process transport.** Starlette's
``TestClient`` and httpx's ``ASGITransport`` both collect the whole response body
before returning — verified in Starlette's source, which writes every chunk into
a ``BytesIO`` and only completes when ``more_body`` is false. Run through either
of them, "a slow adapter does not block fast ones" cannot be detected *at all*:
the assertion passes because the transport, not the server, decided when the
bytes arrived (contracts.md §5).

    The rule: assertions about **when** data arrives cross a real socket.
    Assertions about **what** it says may use an in-process transport.

So this file uses the ``live_server`` fixture — a real uvicorn on a real port, in
its own thread, with ``timeout_graceful_shutdown=1`` because the default of
``None`` waits forever and would wedge CI on the first test that left a request
open.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator

import httpx
import pytest
from core.config import get_settings
from core.jobs import runtime

pytestmark = pytest.mark.usefixtures("clean_db")

SLOW_SOURCE = "slack"
SLOW_SECONDS = 3.0


@pytest.fixture
def slow_source() -> Iterator[float]:
    """``SLOW_ADAPTER_SOURCE`` / ``SLOW_ADAPTER_DELAY_MS``, for one test.

    Not ``monkeypatch``: the settings object is ``lru_cache``d, so the cache has
    to be cleared *after* the environment is restored, and a monkeypatch
    finalizer runs after this fixture's teardown rather than before it.

    ``ADAPTER_DEADLINE_SECONDS`` goes up with it. It is 1 second under test
    (contracts.md §3), which a deliberately slow source would blow straight
    through — turning the demo of partial results into a demo of the per-run
    deadline, which is a different feature.
    """
    keys = ("SLOW_ADAPTER_SOURCE", "SLOW_ADAPTER_DELAY_MS", "ADAPTER_DEADLINE_SECONDS")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["SLOW_ADAPTER_SOURCE"] = SLOW_SOURCE
    os.environ["SLOW_ADAPTER_DELAY_MS"] = str(int(SLOW_SECONDS * 1000))
    os.environ["ADAPTER_DEADLINE_SECONDS"] = "20"
    get_settings.cache_clear()
    try:
        yield SLOW_SECONDS
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.mark.headline
@pytest.mark.timeout(120)
async def test_fast_results_are_readable_while_a_slow_source_is_still_running(
    live_server: str, slow_source: float
) -> None:
    async with httpx.AsyncClient(base_url=live_server, timeout=30.0) as client:
        minted = await client.post("/v1/auth/dev-login", json={"email": "slow@example.test"})
        headers = {"X-API-Key": minted.json()["key"]}

        created = await client.post(
            "/v1/searches", json={"query": "acme renewal"}, headers=headers
        )
        assert created.status_code == 202
        search_id = created.json()["search_id"]
        # The response came back before any adapter ran. Partial results are
        # only meaningful because of that ordering.
        assert {source["status"] for source in created.json()["sources"]} == {"pending"}

        started = time.monotonic()
        worker = asyncio.create_task(_drain())

        partial_at: float | None = None
        partial: dict[str, object] = {}
        while time.monotonic() - started < slow_source * 4:
            snapshot = (
                await client.get(f"/v1/searches/{search_id}", headers=headers)
            ).json()
            statuses = {row["source"]: row["status"] for row in snapshot["sources"]}
            if statuses.get(SLOW_SOURCE) in {"pending", "running"} and snapshot["results"]:
                partial_at = time.monotonic() - started
                partial = snapshot
                break
            await asyncio.sleep(0.05)

        assert partial_at is not None, (
            "no results were readable while the slow source was still running — "
            "the fan-out is serializing"
        )
        assert partial_at < slow_source / 2, (
            f"fast results took {partial_at:.2f}s to become readable against a "
            f"{slow_source:.0f}s slow source; they should not be waiting on it"
        )
        assert partial["finished"] is False, "a search with a source still running is not finished"
        fast_sources = {
            row["source"] for row in partial["sources"] if row["status"] == "done"
        }
        assert fast_sources, "no source finished ahead of the slow one"
        assert SLOW_SOURCE not in fast_sources

        await worker
        total = time.monotonic() - started

        # The slow source's delay did not *shift* because fast results arrived.
        # Both halves matter: results early, and the slow one still taking
        # exactly as long as it was told to.
        assert total >= slow_source, (
            f"the whole search finished in {total:.2f}s, faster than the "
            f"{slow_source:.0f}s delay — the slow source did not actually run"
        )
        assert total < slow_source * 2.5, (
            f"the search took {total:.2f}s against a {slow_source:.0f}s delay; "
            "something is serializing behind the slow source"
        )

        final = (await client.get(f"/v1/searches/{search_id}", headers=headers)).json()
        assert final["finished"] is True
        assert {row["status"] for row in final["sources"]} == {"done"}
        # Every source is represented, and the slow one's results are there too.
        assert {row["source"] for row in final["results"]} >= {SLOW_SOURCE}


async def _drain() -> None:
    while (await runtime.run_once(limit=10)).claimed:
        pass
