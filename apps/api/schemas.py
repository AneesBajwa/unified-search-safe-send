"""Response models for the two shapes the brief **closes** (task 9.12).

`Result` and `Draft` are fixed by the brief field for field. Everywhere else the
API returns plain dictionaries, which is fine — but these two are generated into
`apps/web/src/api/schema.d.ts` and asserted against the brief by
``test_schema_conformance.py``, so they have to exist as declared models rather
than as whatever a route happened to build that day.

🔴 **Declaring them is what makes the drift test possible at all.** A route
annotated ``-> dict[str, Any]`` publishes ``{}`` in OpenAPI: the generated client
types would be `unknown`, the conformance test would compare nothing to nothing
and pass, and the first field renamed on the server would surface as a blank
card on demo day. That is the phase-3 failure mode exactly — an assertion about
a shape that never touched the value.

**Nothing here filters.** Every key the routes already emitted is modelled, so
attaching these changed no payload. ``extra="forbid"`` means a key added to a
route without being added here fails in the route rather than silently widening
a shape the brief says is closed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Result(BaseModel):
    """CLOSED. The brief's interface, exactly — no score, no raw, no extras.

    Ranking metadata rides the envelope (``?debug=1`` adds a parallel ``ranking``
    array), never a result, so this stays the same shape whatever the merge layer
    learns to do.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    id: str
    title: str
    snippet: str
    author: str | None = None
    timestamp: str | None = None
    url: str


class Draft(BaseModel):
    """CLOSED. ``recipient_display``, ``connection_id`` and the rest of what a
    confirmation screen needs ride the sibling ``confirmation`` object — merging
    them in here would violate the fixed interface."""

    model_config = ConfigDict(extra="forbid")

    id: str
    channel: str
    to: str
    subject: str | None = None
    body: str
    idempotency_key: str


class Confirmation(BaseModel):
    """The sibling of ``Draft``, and the reason ``Draft`` can stay closed.

    ``confirm_sha256`` is over channel‖recipient‖subject‖body. It is what the
    send call must echo, and editing the draft changes it by construction — so a
    confirmation screen rendered before an edit carries a stale value and its
    send is refused. That is arithmetic, not bookkeeping that could get out of
    step.
    """

    model_config = ConfigDict(extra="forbid")

    channel: str
    recipient_display: str
    connection_display: str
    connection_id: int
    subject: str | None = None
    body: str
    confirm_sha256: str
    warning: str


class DraftEnvelope(BaseModel):
    """``{draft, confirmation}`` as **siblings** — never merged."""

    model_config = ConfigDict(extra="forbid")

    draft: Draft
    confirmation: Confirmation


class SourceError(BaseModel):
    """Why a source produced nothing, at the point of failure.

    ``reconnect_url`` is present only for ``needs_reconnect``, which is what
    lets a client render that case as an **action** rather than as an error —
    the one repair for a revoked grant, on the surface where a user meets one.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    classification: str
    message: str | None = None
    #: Where to send the user to fix it, present whenever the failure is
    #: actionable — a first connect or a re-grant. One field, so a client
    #: renders "there is something to do here" without knowing which.
    action_url: str | None = None
    #: The same value, under the name the console already reads. Present only
    #: for a grant that existed and was revoked.
    reconnect_url: str | None = None


class SourceStatus(BaseModel):
    """Per-source status, reported honestly.

    ``status: "failed"`` with ``result_count: 0`` and ``status: "done"`` with
    ``result_count: 0`` are **different facts** and clients must render them
    differently — a throttled source must never look like an empty one
    (risks.md R16). Everything needed to tell them apart is here.

    🔴 **One entry per (source, connection), not per source.** A user with two
    Gmail accounts gets two ``gmail`` entries, because each grant is an
    independent adapter run that can succeed, fail or need reconnecting on its
    own — which is the whole content of "multiple accounts per provider". So
    ``source`` alone is **not** a key; ``(source, connection_id)`` is, and
    ``display_name`` is what tells two entries for the same provider apart on
    screen.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    status: str
    mode: str
    result_count: int
    #: Null for a source that needs no grant (web), and for a provider the user
    #: has not connected at all — which still gets an entry, because "we could
    #: not look" has to be reportable.
    connection_id: int | None = None
    #: The account this run went through, for a source that has one.
    display_name: str | None = None
    error: SourceError | None = None


class SearchSnapshot(BaseModel):
    """The authoritative view. Partial while sources are still running.

    ``finished`` is false while **any** source is non-terminal, including the
    ones that failed — a search where one adapter died is finished, not
    permanently in progress.
    """

    model_config = ConfigDict(extra="forbid")

    search_id: str
    query: str
    is_seed: bool
    finished: bool
    sources: list[SourceStatus]
    results: list[Result]
    #: ``?debug=1`` only. Outside ``results``, always: a ``score`` key on a
    #: result would break the closed shape the brief fixes (design D7).
    ranking: list[dict[str, Any]] | None = None


class SearchResults(BaseModel):
    """Results only, partial-safe: whatever has landed, without waiting.

    Carries ``finished`` so a caller reading this route alone can tell "these are
    all of them" from "these are the ones so far" — the same distinction the
    snapshot makes, and the one worth never losing.
    """

    model_config = ConfigDict(extra="forbid")

    search_id: str
    finished: bool
    results: list[Result]
