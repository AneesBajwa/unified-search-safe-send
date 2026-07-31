"""Configuration. One source of truth; see openspec contracts.md §3."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field
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

    # Present for the contract; unused until phase 1. The app fails closed when
    # this is absent and token material is actually needed — there is no
    # plaintext fallback and no generate-if-missing path.
    token_keyring: str = Field(default="", repr=False)

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
