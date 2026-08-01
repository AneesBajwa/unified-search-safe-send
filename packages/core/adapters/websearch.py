"""The public-web adapter (tasks 7.4-7.5).

The third source, and the one that carries a load nothing else does: **it needs
no connection at all.** A user with zero connected accounts still gets a working
search, which is why ``requires_connection`` is False here and why the seed
set's `board deck` query exists — it demonstrates exactly that case.

With no API key configured the adapter serves a deterministic fixture set and
the source reports ``mode: mock``, which the UI badges. That is not a
convenience for tests (though CI needs no key and no network because of it): it
is the honesty rule applied to ourselves. A source with no provider behind it
must never render as ``live``.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from core.adapters.types import AdapterContext, Result
from core.config import get_settings
from core.errors import ProviderError
from core.http import json_body, provider_client

logger = logging.getLogger("core.adapters.websearch")

SOURCE = "web"
DEFAULT_LIMIT = 10

#: Brave. Chosen over Tavily on one criterion: a permanent free tier that needs
#: no card. The response shape is read defensively either way — swapping the
#: provider is a change to `_call` and `_parse`, not to the adapter.
ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def has_api_key() -> bool:
    """Whether this source can run live.

    Read at registration time to pick the source's mode, and again per run —
    a key added to the environment mid-session should not require a restart to
    take effect, and a key removed should not leave the chip lying.
    """
    return bool(get_settings().web_search_api_key.get_secret_value().strip())


class WebSearchAdapter:
    source = SOURCE

    def __init__(self, *, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = limit
        self.reported_mode = None

    async def search(self, query: str, ctx: AdapterContext) -> list[Result]:
        if not has_api_key():
            return _mock_results(query, limit=min(self._limit, 3))
        payload = await self._call(query)
        return _parse(payload, limit=self._limit)

    async def _call(self, query: str) -> dict[str, Any]:
        key = get_settings().web_search_api_key.get_secret_value()
        async with provider_client() as client:
            response = await client.get(
                ENDPOINT,
                params={"q": query, "count": str(self._limit)},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
            )
        payload = json_body(response)
        if response.status_code >= 400:
            raise ProviderError(
                provider=SOURCE,
                # No provider vocabulary to read, so the status is all there is
                # — which is the narrow case `classify`'s fallback exists for.
                code=str(payload.get("error") or f"http_{response.status_code}"),
                detail=str(payload)[:2000],
                status=response.status_code,
            )
        return payload


def _parse(payload: dict[str, Any], *, limit: int) -> list[Result]:
    rows = ((payload.get("web") or {}).get("results")) or []
    results: list[Result] = []
    for row in rows[:limit]:
        url = str(row.get("url", ""))
        if not url:
            # No link means nothing to click, and a search result you cannot
            # open is not a result. Dropped rather than rendered dead.
            continue
        title = str(row.get("title", "")).strip()
        description = str(row.get("description", "")).strip()
        results.append(
            Result(
                source=SOURCE,
                # The URL is the identity of a web result; there is no other
                # stable id, and hashing it keeps the column bounded.
                id=hashlib.sha256(url.encode()).hexdigest()[:16],
                title=title or url,
                snippet=description or title or url,
                author=str(row.get("profile", {}).get("name") or "") or None,
                timestamp=_iso(row.get("page_age")),
                url=url,
            )
        )
    return results


def _iso(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _mock_results(query: str, *, limit: int) -> list[Result]:
    """Deterministic, and **labelled as fake in the text itself**.

    Deterministic on the query so a demo run twice looks the same twice. The
    snippet says out loud that there is no provider behind it, so a screenshot
    of mock data cannot be mistaken for a screenshot of real data even with the
    status chip cropped out.
    """
    now = datetime.now(UTC)
    titles = (
        "{query} | industry briefing",
        "What {query} means for federal buyers",
        "{query}: a practitioner's summary",
    )
    results: list[Result] = []
    for index, title in enumerate(titles[:limit]):
        digest = hashlib.sha256(f"{SOURCE}|{query}|{index}".encode()).hexdigest()
        results.append(
            Result(
                source=SOURCE,
                id=digest[:16],
                title=title.format(query=query),
                snippet=(
                    f"…matched “{query}”. Mock result: no web-search key is "
                    "configured, so this source is serving fixtures."
                ),
                url=f"https://example.test/{SOURCE}/{digest[:8]}",
                author="example.test",
                timestamp=(now - timedelta(hours=(int(digest[:4], 16) % 96) + index)).isoformat(),
            )
        )
    return results
