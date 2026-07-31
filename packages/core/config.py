"""Configuration. One source of truth; see openspec contracts.md §3."""

from __future__ import annotations

from functools import lru_cache

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
        """Normalise to the asyncpg driver and disable the statement cache.

        Two fixes applied once here rather than at every call site:

        1. Neon, Cloud Run and most tooling hand out bare ``postgresql://``
           URLs. Passing one to an async engine fails with a driver error
           that reads like a network problem.
        2. ``prepared_statement_cache_size=0`` is mandatory behind a
           transaction-mode pooler — pgbouncer cannot route a named prepared
           statement back to the session that created it. SQLAlchemy accepts
           this only as a URL query argument; as a ``create_engine`` kwarg it
           raises ``TypeError: Invalid argument(s)``.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

        if "prepared_statement_cache_size" not in url:
            url += ("&" if "?" in url else "?") + "prepared_statement_cache_size=0"
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
