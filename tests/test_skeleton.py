"""Phase 0 skeleton tests.

Small on purpose. Each one pins a decision that is easy to undo by accident
later, so the value is in the regression, not the coverage.
"""

from __future__ import annotations

import asyncio

import pytest
from core.config import Settings
from core.db import get_engine


def test_engine_cannot_be_created_at_import_time() -> None:
    """Task 1.4c: the engine is built inside the loop, never at import.

    An engine created at import binds its pool to whichever loop imported it;
    reusing it from another loop raises "attached to a different loop" errors
    that read like data corruption. `get_running_loop()` raising here is the
    guard, so assert it actually raises rather than trusting the comment.
    """
    with pytest.raises(RuntimeError, match="no running event loop"):
        get_engine()


def test_engine_is_cached_per_loop() -> None:
    """Same loop reuses one engine; a different loop gets its own.

    Deliberately a sync test driving two loops itself — the assertion is
    *about* loop identity, so it cannot be made from inside one of them.
    """
    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        a1 = loop_a.run_until_complete(_engine_in_this_loop())
        a2 = loop_a.run_until_complete(_engine_in_this_loop())
        b1 = loop_b.run_until_complete(_engine_in_this_loop())

        assert a1 is a2, "same loop must reuse its engine"
        assert a1 is not b1, "a second loop must get its own engine, not a shared pool"
    finally:
        loop_a.close()
        loop_b.close()


async def _engine_in_this_loop() -> object:
    return get_engine()


@pytest.mark.parametrize(
    "given",
    [
        "postgres://u:p@h/db",
        "postgresql://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
    ],
)
def test_url_normalises_to_asyncpg_and_disables_statement_cache(given: str) -> None:
    """Both fixes ride on the URL, because SQLAlchemy accepts them nowhere else.

    prepared_statement_cache_size=0 is mandatory behind Neon's
    transaction-mode pooler; as a create_engine kwarg it raises TypeError.
    """
    url = Settings(database_url=given).sqlalchemy_url
    assert url.startswith("postgresql+asyncpg://")
    assert "prepared_statement_cache_size=0" in url


def test_existing_query_string_is_preserved() -> None:
    url = Settings(database_url="postgresql://u:p@h/db?sslmode=require").sqlalchemy_url
    assert "sslmode=require" in url
    assert "prepared_statement_cache_size=0" in url
    assert url.count("?") == 1
