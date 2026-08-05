"""The worker's ``/sweep`` must drain due jobs, not just recover leases.

Phase-6 live finding: the API's Cloud Tasks nudge fires only on user-initiated
commits, so a job the *worker* rescheduled — a transient send waiting out its
backoff — has nothing else to wake the worker. ``sweep()`` alone only touches
expired leases; a rescheduled job sits ``ready`` forever on Cloud Run. The
Scheduler tick therefore hits an endpoint that sweeps **and then claims**, and
this test pins that contract on both surfaces that expose it (the worker
service and the ``/dev`` mirror), so neither can quietly go back to
recover-only.
"""

from __future__ import annotations

from typing import Any

import pytest
from core.jobs import runtime


class _Report:
    def as_dict(self) -> dict[str, Any]:
        return {"ran": True}


@pytest.fixture
def _spies(monkeypatch):
    calls: list[str] = []

    async def fake_sweep(**kwargs: Any) -> _Report:
        calls.append("sweep")
        return _Report()

    async def fake_run_once(**kwargs: Any) -> _Report:
        calls.append("run_once")
        return _Report()

    monkeypatch.setattr(runtime, "sweep", fake_sweep)
    monkeypatch.setattr(runtime, "run_once", fake_run_once)
    return calls


async def test_worker_sweep_endpoint_recovers_then_drains(_spies, monkeypatch):
    import worker.main as worker_main

    monkeypatch.setattr(worker_main, "sweep", runtime.sweep)
    monkeypatch.setattr(worker_main, "run_once", runtime.run_once)

    body = await worker_main.sweep_endpoint()
    assert _spies == ["sweep", "run_once"], "recover first, then drain"
    assert set(body) == {"sweep", "work"}


async def test_dev_sweep_mirror_recovers_then_drains(_spies, monkeypatch):
    import api.routes_dev as routes_dev

    monkeypatch.setattr(routes_dev, "sweep", runtime.sweep)
    monkeypatch.setattr(routes_dev, "run_once", runtime.run_once)

    body = await routes_dev.trigger_sweep()
    assert _spies == ["sweep", "run_once"], "the dev mirror must stay the same pass"
    assert set(body) == {"sweep", "work"}
