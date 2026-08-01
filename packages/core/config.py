"""Configuration. One source of truth; see openspec contracts.md §3."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 5433: the compose postgres publishes there to stay clear of a
    # natively-installed Postgres on 5432. See docker-compose.yml.
    database_url: str = "postgresql+asyncpg://app:app@localhost:5433/app"
    app_base_url: str = "http://localhost:8080"
    log_level: str = "INFO"

    run_worker_inline: bool = True
    job_lease_seconds: int = 90
    send_lease_seconds: int = 300
    adapter_deadline_seconds: int = 30
    max_attempts: int = 6
    slow_adapter_delay_ms: int = 0
    slow_adapter_source: str = ""

    # Worker knobs. Not in the contracts.md matrix because they are the same
    # everywhere: on Cloud Run the worker is driven by Cloud Tasks and Cloud
    # Scheduler, so only the inline loop ever reads the intervals.
    job_batch_size: int = 5
    worker_poll_seconds: float = 1.0
    sweep_interval_seconds: float = 15.0
    # The per-job wall clock. `job_lease_seconds` is deliberately 3x this: a
    # slow-but-live job must never be reclaimed, because reclaiming a live send
    # is strictly worse than reclaiming a dead one slowly (risks.md R28).
    # Distinct from `adapter_deadline_seconds`, which is the tighter budget for
    # a single provider call inside an adapter run (task 4.6).
    job_deadline_seconds: int = 30
    # Sends get their own, larger budget: one execution may dispatch *and*
    # reconcile, which does not fit an adapter's 30 s. Kept comfortably under
    # `send_lease_seconds` (300) so the deadline always fires before the lease
    # expires — the other order means the sweeper reconciles a live send.
    send_deadline_seconds: int = 120

    # Dev-only surface: the `/dev` routes the smoke test drives, and the
    # `fault:` seam in the adapter handler. Set DEV_ROUTES=0 on Cloud Run.
    # Task group 9 replaces these with the real authenticated public API.
    dev_routes: bool = True

    # The app fails closed when this is absent and token material is actually
    # needed — there is no plaintext fallback and no generate-if-missing path.
    # `SecretStr` so an accidental `print(settings)` or a pydantic validation
    # traceback is already safe (task 6.2b).
    token_keyring: SecretStr = Field(default=SecretStr(""), repr=False)
    # 🔴 A **file**, not the env var above. Env vars resolve at instance start,
    # so rotating one forces a redeploy, and they leak through
    # /proc/self/environ, debug endpoints and subprocess environments (R9).
    # The env var stays as the local-dev fallback and nothing more.
    token_keyring_path: str = "/secrets/keyring"  # noqa: S105 - a path, not a secret

    # ---------------------------------------------------------------- OAuth
    google_client_id: str = ""
    google_client_secret: SecretStr = Field(default=SecretStr(""), repr=False)
    slack_client_id: str = ""
    slack_client_secret: SecretStr = Field(default=SecretStr(""), repr=False)
    slack_signing_secret: SecretStr = Field(default=SecretStr(""), repr=False)
    # 🔴 Slack rejects `http://localhost` redirect URIs outright, so an HTTPS
    # tunnel is a prerequisite for any local Slack OAuth work rather than a
    # convenience (R18). Empty means "no tunnel"; redirect URIs then fall back
    # to APP_BASE_URL, which works for Google and not for Slack.
    oauth_tunnel_url: str = ""

    # Unset => the search adapter serves a deterministic fixture set and reports
    # mode `mock`, which the UI badges. Tests always run against the mock.
    web_search_api_key: SecretStr = Field(default=SecretStr(""), repr=False)

    @property
    def public_base_url(self) -> str:
        """What a provider must be able to reach us on.

        The tunnel wins when present: a redirect URI has to be an address the
        *provider* can call back, which `localhost` is not for Slack and is not
        for anyone once this is deployed.
        """
        return (self.oauth_tunnel_url or self.app_base_url).rstrip("/")

    def redirect_uri(self, provider: str) -> str:
        """⚠️ Exact match at runtime — scheme, case and trailing slash all count.
        `…/callback` and `…/callback/` are different URIs to Google, and there
        are no wildcards. Built in one place so the value registered in the
        console and the value sent at runtime cannot drift."""
        return f"{self.public_base_url}/v1/connections/callback/{provider}"

    @property
    def sqlalchemy_url(self) -> str:
        """Turn whatever the environment hands us into a URL asyncpg accepts.

        Every provider emits a **libpq** connection string; asyncpg does not
        speak libpq's vocabulary, and the mismatch surfaces as a `TypeError`
        deep inside the pool rather than as a configuration error. Verified
        against a real Neon endpoint 2026-07-31.

        1. Driver — `postgresql://` must become `postgresql+asyncpg://`.
        2. `sslmode` -> `ssl`. asyncpg has no `sslmode` kwarg, so Neon's
           default `?sslmode=require` raises
           `TypeError: connect() got an unexpected keyword argument 'sslmode'`.
           The *values* are the same vocabulary, so this is a pure rename.
        3. `channel_binding` is dropped — libpq-only. asyncpg negotiates SCRAM
           channel binding itself over TLS, so nothing is weakened.
        4. `prepared_statement_cache_size=0` is mandatory behind a
           transaction-mode pooler: pgbouncer cannot route a named prepared
           statement back to the session that created it. SQLAlchemy accepts
           it *only* as a URL argument; as a `create_engine` kwarg it raises
           `TypeError: Invalid argument(s)`.

        Parsed rather than string-patched so a password containing `?` or `&`
        cannot corrupt the query string.
        """
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix) :]
                break

        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))

        if (sslmode := params.pop("sslmode", None)) and "ssl" not in params:
            params["ssl"] = sslmode
        params.pop("channel_binding", None)
        params["prepared_statement_cache_size"] = "0"

        return urlunsplit(parts._replace(query=urlencode(params)))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
