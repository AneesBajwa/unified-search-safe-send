"""The connection endpoints (openspec tasks 6.3-6.11, at the HTTP boundary).

``make smoke`` drives the *unconfigured* branch of these on every run — there
are no credentials on this machine — so the configured branch is covered here
instead, with the client ids set. Without this, the half of the surface that
matters once someone fills in a `.env` would ship untested.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from conftest import make_api_key
from core.connections import state

pytestmark = pytest.mark.usefixtures("clean_db")

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
ID_TOKEN = (
    "eyJhbGciOiJSUzI1NiJ9."
    "eyJzdWIiOiAiMTIzNDU2Nzg5MCIsICJlbWFpbCI6ICJkYW5hQGFjbWUudGVzdCJ9."
    "signature-not-verified-for-a-token-from-the-token-endpoint"
)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from core.config import get_settings

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-google-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-test")
    monkeypatch.setenv("SLACK_CLIENT_ID", "test-slack-client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "slack-test")
    monkeypatch.setenv("OAUTH_TUNNEL_URL", "https://tunnel.test")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str], int]]:
    from api.main import create_app

    key = await make_api_key()
    app = create_app(run_worker_inline=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http, {"X-API-Key": str(key["key"])}, int(str(key["user_id"]))


async def test_the_listing_reports_which_providers_are_connectable(
    client: tuple[httpx.AsyncClient, dict[str, str], int],
) -> None:
    """With nothing configured the console renders a "not configured" state
    rather than a Connect button that leads to an OAuth error page."""
    http, auth, _ = client
    body: dict[str, Any] = (await http.get("/v1/connections", headers=auth)).json()
    available = {row["provider"]: row for row in body["available"]}
    assert set(available) == {"gmail", "slack"}
    for row in available.values():
        assert row["configured"] is False
        assert row["authorize_url"] is None


async def test_authorize_mints_a_provider_url(
    client: tuple[httpx.AsyncClient, dict[str, str], int], configured: None
) -> None:
    http, auth, _ = client
    response = await http.get("/v1/connections/gmail/authorize", headers=auth)
    assert response.status_code == 200

    body = response.json()
    assert body["authorize_url"].startswith("https://accounts.google.com/")
    # Task 8.2d — the explanation lives in the connect flow, not only the README.
    # Draft access is the most surprising thing on Google's consent screen, and
    # an unexplained scope is the likeliest reason someone abandons the flow.
    assert "never send twice" in body["rationale"]

    # The redirect URI is built from the tunnel, because it has to be an address
    # the *provider* can reach — `localhost` is not one.
    assert "tunnel.test" in body["authorize_url"]


async def test_an_unconfigured_provider_is_a_config_error_not_a_reconnect_prompt(
    client: tuple[httpx.AsyncClient, dict[str, str], int],
) -> None:
    """🔴 R24. A missing client id is **our** bug. Rendering "reconnect your
    account" for it sends the user round in circles fixing a grant that was
    never the problem."""
    http, auth, _ = client
    response = await http.get("/v1/connections/gmail/authorize", headers=auth)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "provider_not_configured"
    assert error["classification"] == "config"
    assert error["classification"] != "needs_reconnect"


async def test_authorize_requires_a_key(
    client: tuple[httpx.AsyncClient, dict[str, str], int], configured: None
) -> None:
    http, _, _ = client
    assert (await http.get("/v1/connections/gmail/authorize")).status_code == 401


@respx.mock
async def test_the_callback_completes_a_connection(
    client: tuple[httpx.AsyncClient, dict[str, str], int], configured: None
) -> None:
    """The callback is unauthenticated by necessity — a provider redirects a
    browser to it — so it carries no API key and trusts only the signed state."""
    http, auth, user_id = client
    respx.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ya29.new",
                "refresh_token": "1//0-new",
                "expires_in": 3599,
                "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
                "id_token": ID_TOKEN,
            },
        )
    )
    signed = state.sign(state.new_state(user_id=user_id, provider="gmail"))

    response = await http.get(
        "/v1/connections/callback/gmail", params={"code": "abc", "state": signed}
    )
    assert response.status_code == 200
    connection = response.json()["connection"]
    assert connection["provider"] == "gmail"
    assert connection["status"] == "active"
    assert "gmail.readonly" in " ".join(connection["granted_scopes"])

    listed = (await http.get("/v1/connections", headers=auth)).json()["connections"]
    assert any(row["id"] == connection["id"] for row in listed)


async def test_a_callback_with_a_forged_state_is_refused(
    client: tuple[httpx.AsyncClient, dict[str, str], int], configured: None
) -> None:
    http, _, _ = client
    response = await http.get(
        "/v1/connections/callback/gmail",
        params={"code": "abc", "state": "not.a-real-signature"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "state_invalid"


async def test_a_denied_authorization_is_reported_not_swallowed(
    client: tuple[httpx.AsyncClient, dict[str, str], int], configured: None
) -> None:
    http, _, _ = client
    response = await http.get(
        "/v1/connections/callback/gmail", params={"error": "access_denied"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "authorization_denied"


async def test_a_needs_reconnect_row_carries_the_way_back(
    client: tuple[httpx.AsyncClient, dict[str, str], int],
) -> None:
    """The fix, one click away, on the row that needs it — rather than a generic
    error the customer has to translate into an action."""
    http, auth, _ = client
    existing = (await http.get("/v1/connections", headers=auth)).json()["connections"]
    target = existing[0]

    dropped = await http.delete(f"/v1/connections/{target['id']}", headers=auth)
    assert dropped.status_code == 200

    after = (await http.get("/v1/connections", headers=auth)).json()["connections"]
    row = next(r for r in after if r["id"] == target["id"])
    assert row["status"] == "needs_reconnect"
    assert row["reconnect_url"].endswith(f"reconnect={target['id']}")


async def test_disconnecting_someone_elses_connection_is_a_404(
    client: tuple[httpx.AsyncClient, dict[str, str], int],
) -> None:
    """Scoped by user_id in the UPDATE itself, so a mismatch matches no rows
    rather than relying on a check somebody could later move."""
    http, _, _ = client
    other = await make_api_key()
    stranger = {"X-API-Key": str(other["key"])}
    mine = (await http.get("/v1/connections", headers=stranger)).json()["connections"]

    http2, auth, _ = client
    response = await http2.delete(f"/v1/connections/{mine[0]['id']}", headers=auth)
    assert response.status_code == 404
