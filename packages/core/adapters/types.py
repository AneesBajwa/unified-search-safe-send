"""The adapter contract (openspec contracts.md §1, design D6, tasks 4.1-4.2).

``Result`` is **closed**. Seven fields, no eighth. The brief fixes the shape and
the criterion it exists to serve is that a consumer can render the merged list
without knowing which source produced a row — a ``score`` field would break that
the moment one source's scores were not comparable with another's. Ranking
metadata therefore rides the response *envelope*; see ``merge``.

``AdapterContext`` deliberately carries **no database session**. Adapters are
pure query-in / results-out: they do not persist, do not enqueue, and do not
know a job exists. That single omission is what keeps the merge layer
source-agnostic — an adapter that could write rows would inevitably start
writing source-shaped ones.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from logging import Logger
from typing import Any, Protocol, runtime_checkable

from core.enums import SourceMode


class TokenKind(StrEnum):
    """Which credential a call needs.

    Not decoration: Slack's ``search.messages`` rejects a bot token with
    ``not_allowed_token_type`` while everything else wants exactly that token,
    so "which token" is a per-call decision rather than a per-connection one
    (risks.md R4).
    """

    USER = "user"
    BOT = "bot"
    OAUTH = "oauth"


class TokenUnavailable(RuntimeError):
    """Raised by :func:`no_token` when an adapter asks for a credential that a
    mock-mode run does not have. Loud on purpose — a fake that reaches for a
    token is a fake that is about to do something real."""


@dataclass(frozen=True)
class AdapterContext:
    """Everything an adapter is allowed to know.

    ``get_token`` is a *callable* rather than a value so a refresh happens
    lazily, at the moment of use and inside the advisory lock — never eagerly in
    the fan-out, which would refresh every connection on every search.
    """

    connection_id: int | None
    provider: str | None
    get_token: Callable[[TokenKind], Awaitable[str]]
    deadline: datetime
    correlation_id: str
    logger: Logger
    mode_hint: SourceMode = SourceMode.LIVE


async def no_token(kind: TokenKind) -> str:
    """The ``get_token`` a mock-mode context carries."""
    raise TokenUnavailable(f"no {kind.value} token in this context")


#: The closed shape, in the order api-design.md documents it. The conformance
#: test reads this tuple rather than restating the field list, so the two can
#: never drift.
RESULT_FIELDS: tuple[str, ...] = (
    "source",
    "id",
    "title",
    "snippet",
    "author",
    "timestamp",
    "url",
)


class NormalizationError(ValueError):
    """A result that cannot be rendered honestly.

    Normalization is the *adapter's* job and it is total. A half-populated
    result is dropped with a warning rather than emitted — a card with a blank
    title tells the customer nothing and looks like a bug in our product rather
    than a gap in the provider's data.
    """


@dataclass(frozen=True)
class Result:
    """The one shape every source produces. Frozen so nothing decorates it later."""

    source: str
    id: str
    title: str
    snippet: str
    url: str
    author: str | None = None
    #: ISO 8601. A string rather than a ``datetime`` because it crosses the API
    #: boundary verbatim and the brief specifies it as one.
    timestamp: str | None = None

    def as_public_dict(self) -> dict[str, Any]:
        """Exactly the documented keys, in the documented order.

        Absent optionals are *omitted* rather than serialized as ``null``,
        matching ``author?: string`` in the brief's TypeScript.
        """
        payload: dict[str, Any] = {
            "source": self.source,
            "id": self.id,
            "title": self.title,
            "snippet": self.snippet,
        }
        if self.author is not None:
            payload["author"] = self.author
        if self.timestamp is not None:
            payload["timestamp"] = self.timestamp
        payload["url"] = self.url
        return payload

    def occurred_at(self) -> datetime | None:
        if self.timestamp is None:
            return None
        return _parse_iso8601(self.timestamp)


def assert_normalized(result: Result) -> Result:
    """Enforce the shape at the boundary (task 4.2).

    Runs on every result leaving an adapter, not only in debug builds: the
    database carries the same rule as ``search_results_shape_complete``, so
    skipping it here would trade a clear ``NormalizationError`` naming the field
    for an ``IntegrityError`` naming a constraint.
    """
    for field in ("source", "id", "title", "snippet", "url"):
        value = getattr(result, field)
        if not isinstance(value, str) or not value.strip():
            raise NormalizationError(
                f"{result.source or '<unknown source>'} produced a result with an "
                f"empty {field!r}; adapters normalize, they do not emit partials"
            )
    if result.timestamp is not None and _parse_iso8601(result.timestamp) is None:
        raise NormalizationError(
            f"{result.source} produced timestamp {result.timestamp!r}, which is not ISO 8601"
        )
    return result


@runtime_checkable
class SearchAdapter(Protocol):
    """Query in, results out.

    ``source`` is ``str`` rather than design D6's ``Literal["gmail","slack","web"]``
    on purpose: a closed literal would make adding a fourth adapter a change to
    this file, contradicting the one-file-plus-one-registry-line guarantee the
    source-agnosticism test defends.
    """

    source: str

    async def search(self, query: str, ctx: AdapterContext) -> list[Result]: ...


def _parse_iso8601(raw: str) -> datetime | None:
    try:
        # `fromisoformat` handles the trailing `Z` from 3.11 on, which is the
        # form every provider we care about emits.
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
