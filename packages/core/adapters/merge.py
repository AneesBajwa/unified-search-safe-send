"""Merge and rank (openspec tasks 4.7-4.8, design D7).

Deliberately simple and explainable. The honest answer is that relevance scores
from an inbox, a chat archive and a search API are **not comparable** — one is
a BM25-ish text score, one is recency-dominated, one is a proprietary blend —
so pretending to a unified score would be worse than an interleave a customer
can predict. What we blend is *rank within a source* (which is comparable,
because it is ordinal) with *recency* (which is comparable, because it is
seconds).

Two rules this module exists to keep:

1. **Ranking metadata never touches a result.** Scores ride the response
   envelope as a parallel array. A ``score`` key on a result would break the
   stated criterion that a consumer can render the list without knowing where a
   row came from.
2. **This module never names a source.** A test greps it, and the orchestrator,
   for provider names and fails on any hit (task 4.9). Everything here works off
   whatever strings the registry happens to hold.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from core.adapters.types import Result

#: Reciprocal-rank smoothing. 60 is the reciprocal-rank-fusion convention and is
#: far too flat for lists of four; 5 keeps first place meaningfully ahead of
#: fourth over the list lengths a person actually reads.
RECIPROCAL_RANK_K = 5.0

#: How much of the score comes from position versus age. Position dominates:
#: recency is a tie-breaker between plausible matches, not a ranking of its own,
#: or the newest irrelevant row wins every search.
RANK_WEIGHT = 0.65
RECENCY_WEIGHT = 0.35

#: Exponential decay: a day-old row keeps half its recency component.
RECENCY_HALF_LIFE_HOURS = 24.0

#: No source may contribute more than this to a merged page, however many it
#: returned. A chatty source flooding the list is indistinguishable, to the
#: person reading it, from the other sources having found nothing.
PER_SOURCE_CAP = 10


@dataclass(frozen=True)
class Ranked:
    """A result plus the numbers that placed it — kept strictly beside it."""

    result: Result
    source_rank: int
    recency_weight: float
    blended_score: float

    def ranking_entry(self) -> dict[str, object]:
        """The ``?debug=1`` envelope row. Parallel to the results array, never
        merged into it."""
        return {
            "source": self.result.source,
            "id": self.result.id,
            "source_rank": self.source_rank,
            "recency_weight": round(self.recency_weight, 6),
            "blended_score": round(self.blended_score, 6),
        }


def recency_weight(occurred_at: datetime | None, *, now: datetime) -> float:
    """1.0 for right now, decaying by half every :data:`RECENCY_HALF_LIFE_HOURS`.

    An unknown timestamp scores 0 rather than 1. "We do not know when this
    happened" must never outrank "this happened five minutes ago".
    """
    if occurred_at is None:
        return 0.0
    age_hours = (now - occurred_at).total_seconds() / 3600
    if age_hours <= 0:
        return 1.0
    return float(0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS))


def blended_score(source_rank: int, occurred_at: datetime | None, *, now: datetime) -> float:
    """Blend ordinal position within a source with age.

    ``source_rank`` is 1-based: the adapter's own idea of its best match.
    """
    rank_component = 1.0 / (RECIPROCAL_RANK_K + source_rank)
    return RANK_WEIGHT * rank_component + RECENCY_WEIGHT * recency_weight(
        occurred_at, now=now
    )


def rank_within_source(results: Sequence[Result], *, now: datetime) -> list[Ranked]:
    """Score one adapter's output, preserving the order it returned.

    Adapters return their own best-first order and we do not second-guess it —
    that ordering is the one genuinely source-specific piece of relevance
    knowledge in the system, and it is exactly what this layer must not throw
    away.
    """
    ranked: list[Ranked] = []
    for position, result in enumerate(results, start=1):
        weight = recency_weight(result.occurred_at(), now=now)
        ranked.append(
            Ranked(
                result=result,
                source_rank=position,
                recency_weight=weight,
                blended_score=blended_score(position, result.occurred_at(), now=now),
            )
        )
    return ranked


def merge(ranked: Iterable[Ranked], *, per_source_cap: int = PER_SOURCE_CAP) -> list[Ranked]:
    """Interleave sources under a per-source cap, best-first within each.

    The interleave is a round robin over sources ordered by the score of their
    best remaining row. So the strongest single match still leads the page, but
    the second slot goes to a *different* source — which is what makes a unified
    search feel unified rather than feel like one source with an appendix.

    Ties broken by source name so the order is stable across runs, and stable is
    what makes a demo repeatable.
    """
    buckets: dict[str, list[Ranked]] = {}
    for item in ranked:
        buckets.setdefault(item.result.source, []).append(item)

    for source, items in buckets.items():
        items.sort(key=lambda item: (-item.blended_score, item.result.id))
        buckets[source] = items[:per_source_cap]

    merged: list[Ranked] = []
    while any(buckets.values()):
        # Re-sorted every round rather than once: after a source hands over its
        # best row its *next* row may well be weaker than another source's head,
        # and a fixed order would ignore that.
        order = sorted(
            (source for source, items in buckets.items() if items),
            key=lambda source: (-buckets[source][0].blended_score, source),
        )
        for source in order:
            merged.append(buckets[source].pop(0))
    return merged
