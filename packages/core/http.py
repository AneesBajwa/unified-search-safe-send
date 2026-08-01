"""The one way this codebase talks to a provider (risks.md R23).

🔴 **Every client is pinned to zero retries.** The Slack SDK defaults to ten
retries over thirty minutes and Google's client library to a non-zero default of
its own. Sensible for idempotent APIs, catastrophic for a send with no dedup
token: a slow-but-successful request gets retried *underneath* the gate, so the
duplicate never passes through the claim at all and no amount of correct claim
logic helps.

Built here rather than per module so there is exactly one place the retry count
can be set — and one place a reviewer has to check. ``tests/test_send_transport``
asserts at the socket that one HTTP request leaves the process across repeated
sends, which is the only assertion that can catch a client library retrying.
"""

from __future__ import annotations

from typing import Any

import httpx

#: Deliberately short. A provider call happens inside a job with its own
#: deadline, and for the token refresh it happens while holding
#: ``pg_advisory_xact_lock`` — which pins a Postgres connection from a small
#: free-tier pool for the duration (R11). Hanging on a slow provider is how that
#: pool gets exhausted.
DEFAULT_TIMEOUT_SECONDS = 15.0


def provider_client(*, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """An ``AsyncClient`` that will never retry for us.

    ``retries=0`` is on the *transport*, which is where httpx's connection-level
    retries live; the request-level behaviour is already no-retry. Constructed
    per call site rather than shared, so nobody can "optimise" a module-level
    client's settings for one caller and change every caller.
    """
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0),
        timeout=timeout,
        follow_redirects=False,
    )


def json_body(response: httpx.Response) -> dict[str, Any]:
    """Parse, or hand back the raw text as evidence.

    A provider returning an HTML error page (a proxy, a maintenance window) must
    not surface as a ``JSONDecodeError`` from inside our parsing code — that
    reads as our bug and buries the actual response. The spec forbids reducing a
    provider error to a generic message, so the body survives as ``detail``.
    """
    try:
        parsed = response.json()
    except ValueError:
        return {"error": "non_json_response", "detail": response.text[:2000]}
    return parsed if isinstance(parsed, dict) else {"error": "unexpected_json_shape"}
