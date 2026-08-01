"""The claim this phase has to make true, made structural (task 9.14).

    "We should be able to run a search and send a message entirely through the
    API with no UI present."

``test_api_e2e.py`` demonstrates it by driving the whole loop over a socket.
This file proves the *stronger* version: not that the flow happens to work
without a UI, but that there is no route it could work differently for — one
credential type, no cookie path, no session middleware, and therefore no
endpoint the SPA can reach that ``curl`` cannot.

🔴 **Enumerated, not asserted.** A README sentence saying "every route takes an
API key" is true on the day it is written. This walks the router.
"""

from __future__ import annotations

import pytest
from api.deps import require_api_key
from api.main import create_app
from fastapi.routing import APIRoute

#: The routes that do not carry ``X-API-Key``, each with the reason it cannot.
#: An empty value would be a route nobody justified, so the reason is the value.
UNAUTHENTICATED: dict[tuple[str, str], str] = {
    ("POST", "/v1/auth/dev-login"): (
        "the PoC sign-in the brief allows: it is how a caller obtains a key, so "
        "it cannot require one"
    ),
    ("GET", "/v1/connections/callback/{provider}"): (
        "a PROVIDER redirects a browser here, so it cannot carry a header. Not an "
        "exception to the rule: everything it trusts comes out of the signed "
        "`state`, never a query parameter"
    ),
    ("GET", "/health"): "liveness, read by Cloud Run and by the SPA's degraded banner",
    ("GET", "/openapi.json"): "the published schema — task 9.12 generates the client from it",
    ("GET", "/docs"): "FastAPI's Swagger UI, served from the schema above",
    ("GET", "/docs/oauth2-redirect"): "Swagger UI's own OAuth helper page",
    ("GET", "/redoc"): "FastAPI's ReDoc UI, served from the schema above",
    ("POST", "/dev/work"): (
        "a worker trigger, not product surface: on Cloud Run the worker is its "
        "own service driven by Cloud Tasks, and locally the API runs it inline, "
        "so this exists only to drive one pass by hand against a single compose "
        "service. Mounted only when DEV_ROUTES=1, which is 0 on Cloud Run — "
        "pinned by test_dev_routes_are_absent_when_disabled below"
    ),
    ("POST", "/dev/sweep"): "the sweeper half of the same trigger; same reasoning",
}


def _surface(app: object) -> list[tuple[str, str, list[object]]]:
    """Every HTTP route on the app, flattened.

    ⚠️ ``app.routes`` is **not** the list of routes. FastAPI wraps an included
    router in a ``_IncludedRouter`` that matches lazily and reports
    ``routes: []`` — so a naive walk finds ``/health`` and the doc pages, misses
    the entire ``/v1`` surface, and every assertion below passes vacuously. That
    is precisely the failure this file exists to catch, so it is worth catching
    here too: the counts at the bottom of each test are what make an empty
    enumeration fail rather than succeed.
    """
    routes: list[tuple[str, str, list[object]]] = []
    stack = list(app.routes)  # type: ignore[attr-defined]
    while stack:
        route = stack.pop()
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
            continue
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or path is None:
            continue
        # Starlette's own routes (/openapi.json, /docs) carry no dependant; they
        # are unauthenticated by nature and listed in UNAUTHENTICATED with why.
        dependencies = (
            route.dependant.dependencies if isinstance(route, APIRoute) else []
        )
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            routes.append((method, path, dependencies))
    return routes


def _guards(dependencies: list[object]) -> set[object]:
    """Every callable in the dependency tree, so a guard nested one level down
    still counts as present."""
    found: set[object] = set()
    stack = list(dependencies)
    while stack:
        dependency = stack.pop()
        found.add(dependency.call)  # type: ignore[attr-defined]
        stack.extend(dependency.dependencies)  # type: ignore[attr-defined]
    return found


def test_every_route_is_reachable_with_an_api_key() -> None:
    """No route requires a browser session, and none is unguarded by accident."""
    app = create_app(run_worker_inline=False)

    unguarded = [
        (method, path)
        for method, path, dependencies in _surface(app)
        if require_api_key not in _guards(dependencies)
    ]

    # Every unguarded route is one we named and justified...
    unjustified = [route for route in unguarded if route not in UNAUTHENTICATED]
    assert not unjustified, (
        f"these routes require no API key and no reason was written down: {unjustified}. "
        "Either guard them or add them to UNAUTHENTICATED with why."
    )

    # ...and every route we named is still unguarded, so the allowlist cannot rot
    # into a list of routes that were fixed years ago and never removed.
    stale = [
        route
        for route in UNAUTHENTICATED
        if route not in unguarded and route in {(m, p) for m, p, _ in _surface(app)}
    ]
    assert not stale, f"these are now authenticated and can leave the allowlist: {stale}"


def test_no_route_authenticates_with_a_cookie_or_session() -> None:
    """There is no second way in, so the claim needs no auditing (design D10).

    A session middleware or a cookie-reading dependency anywhere on this app
    would mean the SPA could hold a credential ``curl`` cannot present — which is
    the exact shape of "the UI is privileged", stated as an absence rather than
    a promise.
    """
    app = create_app(run_worker_inline=False)

    middlewares = [middleware.cls.__name__ for middleware in app.user_middleware]
    assert "SessionMiddleware" not in middlewares, (
        f"a session middleware is installed: {middlewares}"
    )

    cookie_params = [
        (method, path, param.name)
        for method, path, dependencies in _surface(app)
        for dependency in _walk(dependencies)
        for param in dependency.cookie_params  # type: ignore[attr-defined]
    ]
    assert not cookie_params, f"a route reads a cookie: {cookie_params}"


def _walk(dependencies: list[object]) -> list[object]:
    out: list[object] = []
    stack = list(dependencies)
    while stack:
        dependency = stack.pop()
        out.append(dependency)
        stack.extend(dependency.dependencies)  # type: ignore[attr-defined]
    return out


def test_dev_routes_are_absent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/dev/work`` and ``/dev/sweep`` are unauthenticated worker triggers.

    They exist so a developer can drive a pass by hand against a single compose
    service, and they are the one place the API grants a capability an API client
    is not offered — so the thing worth proving is that ``DEV_ROUTES=0`` actually
    removes them, which is the setting Cloud Run runs with.
    """
    from core.config import get_settings

    monkeypatch.setenv("DEV_ROUTES", "0")
    get_settings.cache_clear()
    try:
        app = create_app(run_worker_inline=False)
        dev = [path for _, path, _ in _surface(app) if path.startswith("/dev")]
        assert dev == [], f"dev routes survived DEV_ROUTES=0: {dev}"
    finally:
        monkeypatch.delenv("DEV_ROUTES", raising=False)
        get_settings.cache_clear()


async def test_every_v1_route_refuses_an_unauthenticated_call(clean_db: None) -> None:
    """The enumeration above reads the dependency tree. This one calls the routes.

    🔴 The distinction is the whole lesson of phase 3: a guard that is *declared*
    is not a guard that *fires*. Every ``/v1`` route outside the allowlist is
    called with no key and must answer 401 — the same 401, whatever it is, so
    nothing discloses which resources exist to an unauthenticated prober.
    """
    from httpx import ASGITransport, AsyncClient

    app = create_app(run_worker_inline=False)
    checked = 0
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method, path, _ in _surface(app):
            if not path.startswith("/v1") or (method, path) in UNAUTHENTICATED:
                continue
            # Any syntactically valid value: the guard runs before the handler,
            # so the response must be 401 rather than a 404 or a 422 about the
            # path parameter — and if it is not, that ordering is the bug.
            concrete = (
                path.replace("{search_id}", "00000000-0000-4000-8000-000000000000")
                .replace("{draft_id}", "00000000-0000-4000-8000-000000000000")
                .replace("{send_id}", "00000000-0000-4000-8000-000000000000")
                .replace("{connection_id}", "1")
                .replace("{provider}", "gmail")
                .replace("{key_id}", "abcdefghijkl")
            )
            response = await client.request(method, concrete, json={})
            assert response.status_code == 401, (
                f"{method} {path} answered {response.status_code} without a key"
            )
            assert response.json()["error"]["code"] == "unauthorized"
            checked += 1

    assert checked >= 15, f"only {checked} routes were exercised; the surface should be larger"
