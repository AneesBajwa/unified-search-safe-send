"""Source name -> adapter (openspec task 4.3).

The registry is the *only* way a source is resolved. That is what makes the
orchestrator's ignorance of `gmail`/`slack`/`web` enforceable rather than
aspirational (task 4.9): there is no second path it could take.

Registering a source also retires the phase-1 ``fault:`` seam in
``core.jobs.handlers``. That seam existed because ``adapter_runs.source`` is
free text and there was no registry to consult, so any string was as valid as
any other. Now an unknown source is a **permanent** failure naming what was
asked for — and the fault modes are ordinary registered adapters, so they keep
working without a special case in the dispatch path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.adapters.types import SearchAdapter
from core.enums import SourceMode

AdapterFactory = Callable[[], SearchAdapter]


class UnknownSource(LookupError):
    """Asked for a source nobody registered.

    Classifies as ``permanent`` (``core.errors.classify`` falls through to it),
    which is right: retrying a typo six times arrives at the same answer more
    slowly.
    """


@dataclass(frozen=True)
class Registration:
    source: str
    factory: AdapterFactory
    #: Whether a plain "search everything" fans out to this source. False for
    #: the fault adapters, which are reachable only by asking for them by name.
    participates_by_default: bool
    #: What ``adapter_runs.mode`` records. The UI always shows `mock` and
    #: `degraded`; a source that is quietly faking is the one thing a status
    #: chip must never allow.
    mode: SourceMode
    #: True when the source needs a provider connection to mean anything, so
    #: the fan-out can attach one. Web-style sources need none, and that is the
    #: zero-connections case the product has to keep working.
    requires_connection: bool


_REGISTRY: dict[str, Registration] = {}


def register(
    source: str,
    factory: AdapterFactory,
    *,
    participates_by_default: bool = True,
    mode: SourceMode = SourceMode.LIVE,
    requires_connection: bool = True,
) -> None:
    _REGISTRY[source] = Registration(
        source=source,
        factory=factory,
        participates_by_default=participates_by_default,
        mode=mode,
        requires_connection=requires_connection,
    )


def registration(source: str) -> Registration:
    try:
        return _REGISTRY[source]
    except KeyError:
        raise UnknownSource(
            f"no adapter registered for source {source!r}; "
            f"known sources: {', '.join(sorted(_REGISTRY)) or '(none)'}"
        ) from None


def get(source: str) -> SearchAdapter:
    return registration(source).factory()


def default_sources() -> tuple[str, ...]:
    """The sources a plain search fans out to, in registration order.

    Registration order rather than alphabetical so the demo's source list is
    stable and deliberate rather than an accident of naming.
    """
    return tuple(
        reg.source for reg in _REGISTRY.values() if reg.participates_by_default
    )


def known_sources() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def clear() -> None:
    """Tests only. Registration is module-level state and a test that adds a
    source must be able to take it away again."""
    _REGISTRY.clear()
