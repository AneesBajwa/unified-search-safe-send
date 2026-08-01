"""The adapters this phase actually runs (openspec tasks 4.4, 4.10).

Fakes rather than providers, and that is the point of phase 2: with no OAuth
anywhere the entire product loop — fan out, partial results, compose, confirm,
send, reconcile — is exercisable end to end. Phase 3 replaces the bodies of
``search`` and leaves every other line of the system alone. If swapping them in
requires touching the orchestrator, the merge layer or the UI, the seam was in
the wrong place.

**This is the only module in the adapter layer that names a source.** The
literals live here deliberately, so that the modules the source-agnosticism test
scans can be free of them (task 4.9).

Two things carried over from phase 1:

- ``SLOW_ADAPTER_SOURCE`` / ``SLOW_ADAPTER_DELAY_MS`` (task 4.10) moved out of
  this module in phase 3 and into ``execute_adapter_run``, so the knob applies
  to the **real** adapters too. It had to: a demo of "one source is
  artificially slow" that only worked on fakes would stop working at exactly
  the moment the sources became real.
- The ``fault:`` seam is now three ordinary registered adapters rather than a
  special case inside the job handler. ``make smoke`` keeps its failure paths
  and the registry can reject a genuinely unknown source.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

from core.adapters.registry import AdapterFactory, register
from core.adapters.types import AdapterContext, Result, SearchAdapter
from core.enums import SourceMode
from core.errors import ProviderError

#: Long enough that the per-run deadline always fires first — which is the
#: behaviour being demonstrated, not an arbitrary number.
HANG_SECONDS = 3600

FAULT_PREFIX = "fault:"


class FakeAdapter:
    """Deterministic results for a source, with an optional injected delay.

    Deterministic on ``(source, query)`` so a demo run twice looks the same
    twice, and so a ranking test can assert an order rather than a shape.
    """

    def __init__(self, source: str, *, titles: tuple[str, ...], author: str) -> None:
        self.source = source
        self._titles = titles
        self._author = author

    async def search(self, query: str, ctx: AdapterContext) -> list[Result]:
        # No delay injection here any more: `SLOW_ADAPTER_SOURCE` moved up into
        # `execute_adapter_run` in phase 3, so it applies to the real adapters
        # too. Honouring it in both places would double the delay.
        now = datetime.now(UTC)
        results: list[Result] = []
        for index, title in enumerate(self._titles):
            # A stable per-(source, query, index) digest gives every fake row a
            # believable-but-fixed id and age.
            digest = hashlib.sha256(
                f"{self.source}|{query}|{index}".encode()
            ).hexdigest()
            age_hours = (int(digest[:4], 16) % 96) + index
            results.append(
                Result(
                    source=self.source,
                    id=digest[:16],
                    title=title.format(query=query),
                    snippet=(
                        f"…matched “{query}” — {title.format(query=query).lower()}. "
                        "Fake content: this source has no provider behind it yet."
                    ),
                    url=f"https://example.test/{self.source}/{digest[:8]}",
                    author=self._author,
                    timestamp=(now - timedelta(hours=age_hours)).isoformat(),
                )
            )
        return results


class FaultAdapter:
    """A source that fails on purpose, so the retry ladder is demonstrable.

    Registered rather than special-cased: the dispatch path has exactly one way
    to resolve a source, and these ride it like anything else.
    """

    def __init__(self, source: str, mode: str) -> None:
        self.source = source
        self._mode = mode

    async def search(self, query: str, ctx: AdapterContext) -> list[Result]:
        if self._mode == "reconnect":
            # Google's `invalid_grant` — the only reliable revocation signal it
            # emits, and the one `classify` maps to `needs_reconnect`.
            #
            # No longer standing in for something unproven: the same journey has
            # been run against a genuinely broken live grant (2026-08-01), where
            # Google's own token endpoint answered `{"error": "invalid_grant",
            # "error_description": "Bad Request"}` and the run reported
            # `needs_reconnect` with a `reconnect_url`. This adapter is kept
            # because it makes that journey **repeatable on demand** — for
            # `make smoke`, for the demo, and without breaking a real grant to
            # do it. `connections.service.invalidate_stored_token` is the other
            # half: it breaks a real one reversibly.
            raise ProviderError(
                provider="google",
                code="invalid_grant",
                detail='{"error": "invalid_grant", "error_description": '
                '"Token has been expired or revoked."}',
                status=400,
            )
        if self._mode == "transient":
            # 503 with no recognised code is the one case `classify` is allowed
            # to read the status, and it lands on `transient`.
            raise ProviderError(
                provider="fault",
                code="synthetic_unavailable",
                detail="injected transient failure (fault adapter)",
                status=503,
            )
        if self._mode == "permanent":
            raise ProviderError(
                provider="fault",
                code="synthetic_bad_request",
                detail="injected permanent failure (fault adapter)",
                status=400,
            )
        if self._mode == "hang":
            await asyncio.sleep(HANG_SECONDS)
        raise ProviderError(
            provider="fault",
            code="unknown_fault_mode",
            detail=f"unknown fault mode {self._mode!r}",
            status=400,
        )


_FIXTURES: dict[str, tuple[tuple[str, ...], str]] = {
    "gmail": (
        (
            "Re: {query} — contract redlines attached",
            "{query}: pricing approved by finance",
            "FW: {query} kickoff notes",
            "{query} — signature requested",
        ),
        "dana@acme.test",
    ),
    "slack": (
        (
            "#acme-renewal: anyone have the {query} deck?",
            "#sales: {query} moved to Thursday",
            "#eng: {query} rollout checklist",
        ),
        "@priya",
    ),
    "web": (
        (
            "{query} | industry briefing",
            "What {query} means for federal buyers",
        ),
        "example.test",
    ),
}


def register_defaults() -> None:
    """Every fake, sources and faults alike. Idempotent — registration
    overwrites by name."""
    register_fake_sources()
    register_fault_adapters()


def register_fake_sources(only: tuple[str, ...] | None = None) -> None:
    """Register the fake search sources, optionally only some of them.

    ``only`` exists because from phase 3 the choice is **per provider**: a
    developer with Google credentials and no Slack app runs one live source and
    one fake one, and both have to be labelled truthfully. See
    ``core.adapters.live``.
    """
    for source, (titles, author) in _FIXTURES.items():
        if only is not None and source not in only:
            continue
        register(
            source,
            _fake_factory(source, titles, author),
            participates_by_default=True,
            # Never `live`: a mocked source that reports itself as live is the
            # dishonesty the status chip exists to prevent.
            mode=SourceMode.MOCK,
            # Fakes need no credential, and the fan-out must keep working with
            # zero connections.
            requires_connection=False,
        )


def register_fault_adapters() -> None:
    """The deliberate failures. Kept live in phase 3 and beyond: they are
    ``make smoke``'s failure paths and the only way to demonstrate the retry
    ladder on demand."""
    for mode in ("transient", "permanent", "hang", "reconnect"):
        source = f"{FAULT_PREFIX}{mode}"
        register(
            source,
            _fault_factory(source, mode),
            # Reachable only by asking for it by name, so a plain search never
            # fans out into a deliberate failure.
            participates_by_default=False,
            mode=SourceMode.MOCK,
            requires_connection=False,
        )


def _fake_factory(
    source: str, titles: tuple[str, ...], author: str
) -> AdapterFactory:
    """A closure per source, so the registry stores a factory rather than a
    shared instance — adapters are cheap and per-run construction keeps them
    from accumulating state between searches."""

    def factory() -> SearchAdapter:
        return FakeAdapter(source, titles=titles, author=author)

    return factory


def _fault_factory(source: str, mode: str) -> AdapterFactory:
    def factory() -> SearchAdapter:
        return FaultAdapter(source, mode)

    return factory
