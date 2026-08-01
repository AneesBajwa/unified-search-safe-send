"""Recovery routing for jobs orphaned by a crashed worker (tasks 3.8, 3.8b).

A registry with a **fail-safe default**, which is the whole point. A future job
kind that someone forgets to register does not fall through to "just run it
again" — it falls through to RECONCILE, which refuses to re-execute anything
without first establishing what already happened. Omission is the failure mode
this defends against, because omission is the one nobody notices.
"""

from __future__ import annotations

from enum import StrEnum

from core.enums import JobKind


class RecoveryPolicy(StrEnum):
    #: Idempotent and read-only. Losing an in-flight one costs a re-read.
    RETRY = "retry"
    #: Has, or may have, an external side effect. Establish what the provider
    #: actually did before deciding whether to dispatch.
    RECONCILE = "reconcile"


RECOVERY_POLICY: dict[JobKind, RecoveryPolicy] = {
    JobKind.ADAPTER_RUN: RecoveryPolicy.RETRY,
    JobKind.SEND: RecoveryPolicy.RECONCILE,
}

#: Where an unregistered kind lands. Never change this to RETRY.
DEFAULT_RECOVERY_POLICY = RecoveryPolicy.RECONCILE


def policy_for(kind: JobKind) -> RecoveryPolicy:
    return RECOVERY_POLICY.get(kind, DEFAULT_RECOVERY_POLICY)
