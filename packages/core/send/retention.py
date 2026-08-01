"""Three clocks (openspec task 5.7b, design D5).

Most implementations collapse these into one TTL and get it wrong in one of two
directions: a short TTL expires real history, a long one wedges crashed sends.
They measure different things and none of them is a storage feature.

1. **The ``sends`` row is retained indefinitely.** It *is* the business record
   and the history view — unlike Stripe's idempotency table, which is a cache in
   front of durable charges. Nothing expires it.
2. **The replay response expires at 24 hours.** Past that, the answer to a
   duplicate is *reconstructed from current state* rather than replayed from a
   cache. We reconstruct always, which satisfies this trivially and is the
   reason there is no response cache to invalidate — but the boundary is still
   reported, because "this is what the send looks like now" and "this is what we
   told you at the time" are different claims and a caller may care which.
3. **The in-flight lease is 5 minutes**, self-expiring, and it is what lets the
   sweeper reclaim a crashed worker.

**Every expiry is computed in application code**, never delegated to a storage
TTL. A row disappearing because a TTL fired is a row nobody decided to delete,
and the audit trail cannot distinguish it from one that was never written.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

#: Clock 2. Chosen to match the IETF idempotency-key draft's guidance and
#: Stripe's own 24-hour window, so a caller's expectations transfer.
REPLAY_CACHE_TTL = timedelta(hours=24)

#: Clock 1. Not a number: the absence of one.
SEND_ROW_RETENTION: None = None


class ReplaySource(StrEnum):
    #: Inside the window: the response is the one the original call would have
    #: produced.
    CACHED = "cached"
    #: Past it: rebuilt from the row as it stands now, which may have moved on.
    RECONSTRUCTED = "reconstructed"


def replay_source(created_at: datetime, *, now: datetime) -> ReplaySource:
    if now - created_at <= REPLAY_CACHE_TTL:
        return ReplaySource.CACHED
    return ReplaySource.RECONSTRUCTED
