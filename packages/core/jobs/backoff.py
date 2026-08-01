"""Retry scheduling (openspec tasks 3.5, 3.5b, 3.5c).

Full jitter, not "exponential backoff plus a bit of noise". The distinction
matters: equal-jitter and decorrelated variants leave a floor, so a thundering
herd re-forms at that floor. Full jitter spreads retries across the whole
window, which is the property we want when six adapter runs fail against the
same rate-limited provider at the same instant.
"""

from __future__ import annotations

import random

# uniform(0, min(CAP, BASE * 2**n)) — contracts.md §2.
BASE_SECONDS = 2.0
CAP_SECONDS = 300.0

# 2**1024 is not an error in Python, it is a 309-digit integer, and multiplying
# it by a float raises OverflowError. Clamping the exponent keeps a job whose
# attempts somehow ran away from taking the process down with it; anything past
# 8 is already pinned to the cap regardless.
EXPONENT_CLAMP = 16


def full_jitter(
    attempts: int,
    *,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before the next attempt.

    ``attempts`` is the number already made, so the first retry uses ``n=1``.

    A provider's ``Retry-After`` always wins — ``max(retry_after, jitter)``, not
    ``retry_after`` outright, because a 1-second Retry-After under a herd is
    still worth spreading.
    """
    exponent = min(max(attempts, 0), EXPONENT_CLAMP)
    ceiling = min(CAP_SECONDS, BASE_SECONDS * (2.0**exponent))
    # Scheduling jitter, not a secret. A CSPRNG here would cost entropy for no
    # benefit — predicting when a retry fires buys an attacker nothing.
    jitter = (rng or random).uniform(0.0, ceiling)
    if retry_after is not None and retry_after > 0:
        return max(retry_after, jitter)
    return jitter
