"""The API-side half of design D4's hot path: enqueue a Cloud Tasks push.

Locally and in Codespaces the inline loop polls, so this module is a no-op
unless all three settings are present — which is only true on Cloud Run. There
is deliberately no ``google-cloud-tasks`` dependency: the REST surface is one
POST, the access token comes from the metadata server, and the client library
would be the only Google package in the tree.

A failed nudge is swallowed with a warning, never raised: the job row is
already committed, and the ``*/15`` sweep will find it. Losing the nudge costs
latency; failing the user's request over it would cost correctness of the
API's contract (the search/send was accepted).

🔴 Call this **after** the transaction that inserted the job commits. A task
delivered before the commit lands claims nothing, returns 200, and is never
retried — the job then waits for the sweep, which is exactly the latency the
nudge exists to avoid.
"""

from __future__ import annotations

import logging

import httpx

from core.config import get_settings

logger = logging.getLogger("core.jobs.nudge")

#: GCE/Cloud Run metadata server. Fixed address, link-local, no TLS by design.
#: Not a secret — the ruff suppression is for S105's "token" heuristic.
_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"  # noqa: S105
)


def _configured() -> bool:
    s = get_settings()
    return bool(s.cloud_tasks_queue and s.worker_url and s.tasks_oidc_service_account)


async def nudge_worker() -> None:
    """Create one Cloud Tasks task targeting the worker's ``/work``.

    One nudge per user action, not per job: ``/work`` claims a batch, so a
    fan-out of N adapter runs still needs only one delivery. At-least-once
    delivery is fine because ``/work`` is idempotent and cheap when idle.
    """
    if not _configured():
        return
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            token_resp = await client.get(
                _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
            )
            token_resp.raise_for_status()
            access_token = token_resp.json()["access_token"]

            worker_url = s.worker_url.rstrip("/")
            resp = await client.post(
                f"https://cloudtasks.googleapis.com/v2/{s.cloud_tasks_queue}/tasks",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "task": {
                        "httpRequest": {
                            "url": f"{worker_url}/work",
                            "httpMethod": "POST",
                            "oidcToken": {
                                "serviceAccountEmail": s.tasks_oidc_service_account,
                                # Cloud Run's IAM check validates the audience
                                # against the service URL, not the full path.
                                "audience": worker_url,
                            },
                        }
                    }
                },
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - latency, not correctness
        logger.warning("worker nudge failed (sweep will recover): %s", exc)
