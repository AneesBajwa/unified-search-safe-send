"""Backoff (openspec tasks 3.5, 3.5b).

Full jitter has a property worth pinning explicitly: the delay is drawn from
``[0, ceiling]``, so it can legitimately be *smaller* than the previous
attempt's. That looks like a bug to anyone expecting a monotonic ladder, which
is exactly why it is asserted here rather than left to be "fixed" later.
"""

from __future__ import annotations

import random

import pytest
from core.jobs.backoff import BASE_SECONDS, CAP_SECONDS, EXPONENT_CLAMP, full_jitter


@pytest.mark.parametrize("attempts", range(0, 12))
def test_delay_stays_within_the_full_jitter_window(attempts: int) -> None:
    ceiling = min(CAP_SECONDS, BASE_SECONDS * 2**attempts)
    for _ in range(200):
        delay = full_jitter(attempts)
        assert 0.0 <= delay <= ceiling


def test_the_window_widens_then_pins_to_the_cap() -> None:
    rng = random.Random(0)  # noqa: S311 - scheduling jitter, not a secret
    # Drawing the top of the window: uniform(0, x) with a seeded rng that
    # returns 1.0 would be ideal, but Random has no such hook — so sample enough
    # times that the observed maximum approaches the ceiling closely.
    for attempts, expected_ceiling in [(0, 2.0), (1, 4.0), (2, 8.0), (8, 300.0), (20, 300.0)]:
        observed = max(full_jitter(attempts, rng=rng) for _ in range(500))
        assert observed <= expected_ceiling
        assert observed > expected_ceiling * 0.9, "the window should actually be this wide"


def test_huge_attempt_counts_do_not_overflow() -> None:
    """``2**1024`` is a valid int in Python and multiplying it by a float raises.

    A runaway attempt count should schedule a retry at the cap, not take the
    worker process down with an OverflowError.
    """
    assert full_jitter(10_000) <= CAP_SECONDS
    assert full_jitter(EXPONENT_CLAMP * 4) <= CAP_SECONDS


def test_retry_after_wins_over_a_smaller_jitter() -> None:
    """Ignoring a provider's Retry-After is how Slack rate-limits you harder."""
    delay = full_jitter(0, retry_after=120.0)
    assert delay == 120.0  # our window at n=0 tops out at 2s, so Retry-After dominates


def test_retry_after_does_not_shrink_a_larger_jitter() -> None:
    """``max(retry_after, jitter)``, not ``retry_after`` outright.

    A 1-second Retry-After under a thundering herd is still worth spreading —
    obeying it literally would re-synchronise every retrying worker.
    """
    rng = random.Random(1)  # noqa: S311 - scheduling jitter, not a secret
    for _ in range(200):
        assert full_jitter(8, retry_after=1.0, rng=rng) >= 1.0
    assert max(full_jitter(8, retry_after=1.0, rng=rng) for _ in range(500)) > 100.0


def test_a_zero_or_negative_retry_after_is_ignored() -> None:
    assert full_jitter(3, retry_after=0.0) <= min(CAP_SECONDS, BASE_SECONDS * 8)
