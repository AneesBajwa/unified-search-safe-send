"""Named crash seams at every commit boundary (openspec task 5.8b).

The exactly-once claim is only as good as the crash tests behind it, and a crash
test can only be written where the code offers a place to crash. So the dispatch
path carries four named seams — no-ops unless armed — one at each boundary where
the answer to "what does the system believe now?" changes.

🔴 **``InjectedCrash`` derives from ``BaseException``, not ``Exception``.** A
production ``except Exception`` handler must not be able to swallow it; if one
did, the test would pass by exercising the error path instead of the crash path
and would prove the opposite of what it claims. It will escape
``runtime._execute``'s ``except Exception``, surface through ``run_once``'s
``gather(return_exceptions=True)``, and leave the lease to expire — which is the
correct crash path, not a hole.

Two arming mechanisms, because there are two kinds of crash to model:

- :func:`arm` — in-process, for the parametrized seam tests.
- ``CRASH_AT`` / ``CRASH_MODE=exit`` — environment, so a **subprocess** can be
  told where to die. With ``CRASH_MODE=exit`` the seam calls ``os._exit(1)``:
  no unwinding, no ``finally``, no rollback handler, no atexit. That is the only
  faithful model of a container being killed (task 5.11b).
"""

from __future__ import annotations

import os
from enum import StrEnum

CRASH_AT_ENV = "CRASH_AT"
CRASH_MODE_ENV = "CRASH_MODE"


class CrashPoint(StrEnum):
    """The four boundaries. Each one is a different belief about the world."""

    #: After `dispatched_at` is committed, before the provider is contacted.
    #: The system believes "a dispatch was attempted"; the provider has seen
    #: nothing. Reconciliation must find nothing and dispatch.
    AFTER_LEASE_COMMIT = "after_lease_commit"
    #: The provider accepted and we have a message id in hand, uncommitted.
    #: The dangerous one: our database says in_flight, the message went out.
    AFTER_PROVIDER_ACCEPT = "after_provider_accept"
    #: Same belief as above, one instruction later. Kept distinct because a
    #: reordering of these two lines is a real regression and only a test that
    #: names both would catch it.
    BEFORE_DELIVERY_COMMIT = "before_delivery_commit"
    #: Everything is durable. A crash here must cost nothing at all.
    AFTER_DELIVERY_COMMIT = "after_delivery_commit"


class InjectedCrash(BaseException):
    """Deliberately not an ``Exception``. See the module docstring."""

    def __init__(self, point: CrashPoint) -> None:
        super().__init__(f"injected crash at {point.value}")
        self.point = point


_armed: set[CrashPoint] = set()


def arm(point: CrashPoint) -> None:
    _armed.add(point)


def disarm_all() -> None:
    _armed.clear()


def armed_points() -> frozenset[CrashPoint]:
    return frozenset(_armed)


def seam(point: CrashPoint) -> None:
    """A no-op unless armed. Called on the happy path, so it stays cheap.

    Deliberately synchronous: an ``await`` here would introduce a scheduling
    point that does not exist in production, and the whole value of a seam is
    that arming it changes *only* whether it raises.
    """
    if point not in _armed and os.getenv(CRASH_AT_ENV) != point.value:
        return
    if os.getenv(CRASH_MODE_ENV) == "exit":
        # No unwinding. No `finally`. No rollback. No flush of anything not
        # already fsynced. This is a container being killed, not an exception.
        os._exit(1)
    raise InjectedCrash(point)
