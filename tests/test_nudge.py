"""The Cloud Tasks nudge (design D4, task 14.10).

Two properties, both cheap and both load-bearing:

1. Unconfigured (local, Codespaces, this suite) the nudge makes **no network
   call at all** — asserted by making any client construction explode.
2. Configured, it creates a task whose OIDC audience is the worker's *service*
   URL, not the ``/work`` path — Cloud Run validates the audience against the
   service URL, and a path-suffixed audience fails IAM with a 401 that reads
   like a permissions mystery.
3. A nudge failure is swallowed: the job row is committed and the sweep will
   find it, so latency is the only acceptable cost. Raising would fail a user
   request whose actual work already succeeded.
"""

from __future__ import annotations

import json

import httpx
import pytest
from core.config import get_settings
from core.jobs import nudge


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    yield
    get_settings.cache_clear()


async def test_unconfigured_nudge_is_a_no_op_with_no_network(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("nudge_worker touched the network while unconfigured")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    await nudge.nudge_worker()  # must simply return


async def test_configured_nudge_posts_a_task_with_service_url_audience(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    monkeypatch.setenv("WORKER_URL", "https://worker.example.run.app/")
    monkeypatch.setenv("TASKS_OIDC_SERVICE_ACCOUNT", "sa@p.iam.gserviceaccount.com")
    get_settings.cache_clear()

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3599})
        return httpx.Response(200, json={"name": "projects/p/.../tasks/1"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    await nudge.nudge_worker()

    assert [c.url.host for c in calls] == [
        "metadata.google.internal",
        "cloudtasks.googleapis.com",
    ]
    task_req = calls[1]
    assert task_req.url.path.endswith("/queues/q/tasks")
    http_request = json.loads(task_req.read())["task"]["httpRequest"]
    assert http_request["url"] == "https://worker.example.run.app/work"
    # The audience is the service URL — no /work suffix, no trailing slash.
    assert http_request["oidcToken"]["audience"] == "https://worker.example.run.app"


async def test_a_failing_nudge_never_raises(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    monkeypatch.setenv("WORKER_URL", "https://worker.example.run.app")
    monkeypatch.setenv("TASKS_OIDC_SERVICE_ACCOUNT", "sa@p.iam.gserviceaccount.com")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    await nudge.nudge_worker()  # swallowed; the sweep is the fallback
