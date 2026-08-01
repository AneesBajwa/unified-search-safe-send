"""Recovery routing (openspec task 3.8b).

This turns "we remembered to think about side effects" into a property the
suite defends. The failure it guards against is not a wrong entry — it is a
*missing* one, added by a future contributor who introduces a side-effecting
job kind and never thinks about crash recovery at all.
"""

from __future__ import annotations

import pytest
from core.enums import JobKind
from core.jobs.recovery import (
    DEFAULT_RECOVERY_POLICY,
    RECOVERY_POLICY,
    RecoveryPolicy,
    policy_for,
)


def test_every_job_kind_has_an_explicit_recovery_policy() -> None:
    """No kind may rely on the default.

    The default exists to be safe when someone forgets; this test exists so
    nobody forgets. Both are needed — a fail-safe default with no test degrades
    into "everything reconciles", and a test with no default breaks the moment
    someone adds a kind at runtime.
    """
    missing = [kind for kind in JobKind if kind not in RECOVERY_POLICY]
    assert not missing, (
        f"job kinds with no recovery policy: {[k.value for k in missing]}. "
        "Decide explicitly whether a crashed one may be re-executed."
    )


def test_the_default_is_reconcile_never_retry() -> None:
    """A future side-effecting kind must not be re-run by omission."""
    assert DEFAULT_RECOVERY_POLICY is RecoveryPolicy.RECONCILE


def test_an_unregistered_kind_falls_through_to_reconcile() -> None:
    class FutureKind(str):
        pass

    assert policy_for(FutureKind("charge_card")) is RecoveryPolicy.RECONCILE  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        # Idempotent and read-only: losing one in flight costs a re-read.
        (JobKind.ADAPTER_RUN, RecoveryPolicy.RETRY),
        # Has an external side effect: establish what the provider did first.
        (JobKind.SEND, RecoveryPolicy.RECONCILE),
    ],
)
def test_known_kinds_route_as_designed(kind: JobKind, expected: RecoveryPolicy) -> None:
    assert policy_for(kind) is expected
