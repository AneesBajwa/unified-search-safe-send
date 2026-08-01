"""The suite needs no third-party key and no network (openspec task 13.4).

This is the property that makes every other test worth running somewhere other
than this laptop. It is also the one that has already broken once: phase 4 found
that `core.adapters.live` calls `get_settings()` and registers adapters at
**module import**, while the test environment was being applied inside a
fixture — so the first test module to import `api.main` at module scope picked
up real Google and Slack client ids during collection, registered live adapters,
and a fan-out test that had passed for three phases started reporting `failed`
for two sources with no tokens behind them.

It would have failed on a laptop with credentials and passed on CI. That is the
worst shape a defect can have, because the machine that disagrees is the one
nobody is looking at.

So `conftest` applies the environment at import, and these assert the outcome
rather than the mechanism: whatever the developer has in `.env`, a test run sees
no provider configured and therefore reaches no provider.
"""

from __future__ import annotations

import os

from core.adapters import registry
from core.adapters.live import configured_sources
from core.config import get_settings

#: Every setting that would make an adapter or a send reach the real internet.
CREDENTIAL_SETTINGS = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "SLACK_CLIENT_ID",
    "SLACK_CLIENT_SECRET",
    "SLACK_SIGNING_SECRET",
    "WEB_SEARCH_API_KEY",
)


def test_no_provider_credential_is_visible_to_a_test_run() -> None:
    """Read from the environment the app will actually read.

    ⚠️ This one is a **backstop, not the interesting test**, and it is worth
    saying so: `conftest` blanks these at import, so a value exported in the
    shell can no longer reach here. What it defends is somebody quietly dropping
    a name from `TEST_ENV`.

    The assertion with teeth is
    `test_the_registry_serves_fixtures_rather_than_live_adapters` below —
    verified to fail when a live adapter is registered.
    """
    populated = {
        name: value for name in CREDENTIAL_SETTINGS if (value := os.environ.get(name))
    }
    assert not populated, (
        "a real credential is visible to the test suite, so its result now "
        f"depends on whose laptop it runs on: {sorted(populated)}"
    )


def test_every_source_reports_itself_unconfigured() -> None:
    """The claim one layer up: with no credential, nothing runs live."""
    configured = configured_sources()
    assert configured, "no sources at all — the registry never populated"
    assert not any(configured.values()), (
        f"a source believes it is configured during a hermetic run: {configured}"
    )


def test_the_registry_serves_fixtures_rather_than_live_adapters() -> None:
    """🔴 The one that would have caught the phase-4 defect.

    Every default source is registered in a non-live mode, so an adapter run
    resolves to a fixture and says so in `mode`. Registration happens at module
    import, so this fails if the test environment is applied any later than
    `conftest`'s own import — which is exactly the ordering that broke, and it
    broke *only* on a machine with a populated `.env`.

    Verified to fail rather than assumed to: registering `web` as `LIVE` and
    re-running this assertion reports it.
    """
    live = [
        source
        for source in registry.default_sources()
        if registry.registration(source).mode.value == "live"
    ]
    assert not live, (
        f"these sources would reach a real provider during a test run: {live}"
    )


def test_the_token_keyring_is_generated_per_run_and_is_not_a_real_one() -> None:
    """A leaked test fixture must not decrypt anything.

    The keyring is generated in `conftest` rather than checked in, and the
    volume-mount path is pointed at somewhere that does not exist so a developer
    with a real mounted keyring still gets the disposable one.
    """
    settings = get_settings()
    assert settings.token_keyring.get_secret_value(), "no keyring at all"
    assert not os.path.exists(os.environ["TOKEN_KEYRING_PATH"]), (
        "the test run can see a real mounted keyring"
    )


# ---------------------------------------------------------------------------
# Where the console is allowed to be served from
# ---------------------------------------------------------------------------


def test_a_codespace_origin_is_derived_rather_than_configured() -> None:
    """🔴 Verified against a booted Codespace, not reasoned about.

    GitHub forwards a port into the *hostname*, so the console is cross-origin
    from the API there and the host name is generated per Codespace. Putting
    `${containerEnv:CODESPACE_NAME}` in `devcontainer.json` looks right and is
    not: that substitution runs before Codespaces injects its variables, so the
    value arrives as the literal string and the API rejects every request the
    console makes.
    """
    from api.main import _cors_origins

    assert not any(".app.github.dev" in origin for origin in _cors_origins())

    os.environ["CODESPACE_NAME"] = "literate-spork-example"
    os.environ["GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"] = "app.github.dev"
    try:
        origins = _cors_origins()
    finally:
        del os.environ["CODESPACE_NAME"]
        del os.environ["GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"]

    assert "https://literate-spork-example-5173.app.github.dev" in origins
    # The localhost pair still works, so `make dev` inside a Codespace does too.
    assert "http://localhost:5173" in origins


def test_a_missing_keyring_names_itself_as_our_bug() -> None:
    """🔴 Found by booting a Codespace and clicking Connect.

    `.env.example` ships `TOKEN_KEYRING=` empty — it is committed, so it cannot
    carry key material — and every route that touches a credential needs one,
    including the OAuth authorize call, which signs its state with the same
    keyring. Unhandled, `KeyringUnavailable` reached the client as a bare 500
    with a stack trace in the log: indistinguishable from the app being broken,
    on the first meaningful thing a reviewer clicks after following the README.

    `config`, never `needs_reconnect`. Telling somebody to reconnect an account
    sends them round in circles repairing a grant that was never broken, when
    the fix is one line of *our* configuration (R24).
    """
    from api.main import create_app
    from core.config import get_settings
    from fastapi.testclient import TestClient

    previous = {
        name: os.environ.get(name)
        for name in ("TOKEN_KEYRING", "TOKEN_KEYRING_PATH", "GOOGLE_CLIENT_ID",
                     "GOOGLE_CLIENT_SECRET", "OAUTH_TUNNEL_URL")
    }
    os.environ.update(
        TOKEN_KEYRING="",
        TOKEN_KEYRING_PATH="/nonexistent/keyring",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        OAUTH_TUNNEL_URL="https://tunnel.test",
    )
    get_settings.cache_clear()
    try:
        client = TestClient(
            create_app(run_worker_inline=False), raise_server_exceptions=False
        )
        key = client.post(
            "/v1/auth/dev-login", json={"email": "keyring@example.test"}
        ).json()["key"]
        response = client.get(
            "/v1/connections/gmail/authorize", headers={"X-API-Key": key}
        )
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()

    body = response.json()["error"]
    assert body["code"] == "internal_config_error"
    assert body["classification"] == "config"
    assert "reconnect" not in body["code"]
