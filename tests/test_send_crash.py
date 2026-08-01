"""Crash recovery for the send gate (openspec tasks 5.11, 5.11b, 5.12, 5.12b).

The hard failure mode: the process dies after the provider accepted the message
but before we commit ``delivered``. Blind retry double-sends; blind failure loses
a real delivery. Both are wrong, so the send is **reconciled** first — and only
dispatched if reconciliation proves nothing went out.

Every test here asserts on ``provider.delivery_count``. The database saying
``delivered`` once proves nothing about how many messages the provider received.

🔴 The crash is a ``BaseException`` subclass so that production
``except Exception`` handlers cannot swallow it. If one could, these tests would
be exercising the *error* path while claiming to exercise the *crash* path —
which would prove the opposite of what they say.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_ENV, make_api_key
from core.db import session_scope
from core.enums import ProviderKind, SendState
from core.errors import ProviderError
from core.jobs import runtime
from core.send import crash, service
from core.send.crash import CrashPoint
from core.send.providers import FakeSendProvider, get_provider
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("clean_db")

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANNEL = "C024BE91L"

#: `JOB_LEASE_SECONDS` / `SEND_LEASE_SECONDS` are 2 under test (contracts.md §3),
#: so lease expiry is a two-second wait rather than five minutes.
LEASE_EXPIRY_WAIT = 2.4


def _provider() -> FakeSendProvider:
    impl = get_provider(ProviderKind.SLACK)
    assert isinstance(impl, FakeSendProvider)
    return impl


async def _queued_send(body: str = "Confirming for Thursday.") -> tuple[uuid.UUID, int]:
    """A user, a draft, and a claimed send with its job enqueued."""
    key = await make_api_key()
    user_id = int(str(key["user_id"]))
    async with session_scope() as session:
        view = await service.create_draft(
            session,
            user_id=user_id,
            channel=ProviderKind.SLACK,
            recipient=CHANNEL,
            body=body,
        )
        outcome = await service.send_draft(
            session,
            uuid.UUID(view.draft["id"]),
            user_id=user_id,
            confirmed_sha256=view.confirmation["confirm_sha256"],
        )
        await session.commit()
    assert outcome.status == 201
    return uuid.UUID(outcome.payload["send_id"]), user_id


async def _send_row(send_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(
                text("SELECT * FROM sends WHERE id = :id"), {"id": send_id}
            )
        ).mappings().one()
    return dict(row)


async def _drain() -> None:
    while (await runtime.run_once(limit=10)).claimed:
        pass


# ---------------------------------------------------------------------------
# Every seam
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "point",
    [
        CrashPoint.AFTER_LEASE_COMMIT,
        CrashPoint.AFTER_PROVIDER_ACCEPT,
        CrashPoint.BEFORE_DELIVERY_COMMIT,
        CrashPoint.AFTER_DELIVERY_COMMIT,
    ],
)
async def test_a_crash_at_any_seam_still_delivers_exactly_once(point: CrashPoint) -> None:
    """openspec task 5.11 — parametrized over every commit boundary.

    Each seam leaves the system believing something different, and each belief
    needs a different resolution: dispatch, adopt, or nothing at all. The
    invariant across all four is the same one.
    """
    send_id, _ = await _queued_send()

    crash.arm(point)
    await runtime.run_once(limit=1)
    crash.disarm_all()

    # The crash escaped `_execute`'s `except Exception`, surfaced through
    # `run_once`'s `gather(return_exceptions=True)`, and left the lease to
    # expire. That is the correct crash path, not a hole.
    async with session_scope() as session:
        state = await session.scalar(
            text("SELECT state::text FROM jobs WHERE kind = 'send' AND ref_id = :ref"),
            {"ref": send_id},
        )
    assert state == "running", "a crashed job must keep its claim as evidence"

    await asyncio.sleep(LEASE_EXPIRY_WAIT)
    report = await runtime.sweep()
    assert report.reconciled == 1, "a stale send must be reconciled, never blindly retried"

    await _drain()

    assert _provider().delivery_count == 1, (
        f"crash at {point.value} produced {_provider().delivery_count} deliveries"
    )
    row = await _send_row(send_id)
    assert row["state"] == SendState.DELIVERED.value
    assert row["provider_message_id"], "delivered without provider evidence is unrepresentable"


async def test_a_crash_before_the_provider_was_reached_finds_nothing_and_dispatches() -> None:
    """openspec task 5.12 — the other direction.

    Reconciliation must be able to say "nothing went out" as confidently as it
    says "something did". That answer is only available because `dispatched_at`
    was committed in its own transaction before the provider call: without it
    the sweeper cannot tell *never dispatched* from *crashed mid-dispatch*.
    """
    send_id, _ = await _queued_send()

    crash.arm(CrashPoint.AFTER_LEASE_COMMIT)
    await runtime.run_once(limit=1)
    crash.disarm_all()

    row = await _send_row(send_id)
    assert row["dispatched_at"] is not None, "the crash evidence must survive the crash"
    assert _provider().delivery_count == 0

    await asyncio.sleep(LEASE_EXPIRY_WAIT)
    await runtime.sweep()
    await _drain()

    assert _provider().delivery_count == 1
    assert (await _send_row(send_id))["state"] == SendState.DELIVERED.value


async def test_a_timeout_that_actually_succeeded_still_yields_one_delivery() -> None:
    """openspec task 5.12b — the case Google closed as "not planned".

    The provider accepted the message and the *response* was lost. Indistinguishable
    from never arriving, from our side. The full retry ladder runs, and the
    reconciliation on the second attempt adopts the delivery that did happen
    rather than making a second one.
    """
    send_id, _ = await _queued_send()
    _provider().fail_next(
        ProviderError(
            provider="slack",
            code="request_timeout",
            detail="the post may have succeeded",
            status=200,
        ),
        after_accepting=True,
    )

    await runtime.run_once(limit=1)
    row = await _send_row(send_id)
    assert row["state"] == SendState.IN_FLIGHT.value, "a transient failure keeps retrying"
    assert _provider().delivery_count == 1

    # The retry is scheduled with backoff; pull it forward rather than waiting
    # out a jittered delay, which would make this test slow and flaky for no
    # additional coverage.
    async with session_scope() as session:
        await session.execute(
            text("UPDATE jobs SET run_at = now() WHERE kind = 'send' AND ref_id = :ref"),
            {"ref": send_id},
        )
        await session.commit()

    await _drain()

    assert _provider().delivery_count == 1, "the retry re-sent a message that had gone out"
    row = await _send_row(send_id)
    assert row["state"] == SendState.DELIVERED.value
    assert row["reconcile_attempts"] == 1


async def test_reconciliation_is_bounded_and_parks_in_uncertain() -> None:
    """Three attempts, then `uncertain` — never an infinite reconcile loop.

    A revoked grant makes every probe fail the same way forever. `uncertain` is
    the honest terminal state for the residue: amber, not red, because failed
    means we know nothing was sent and uncertain means we do not know.
    """
    send_id, _ = await _queued_send()

    crash.arm(CrashPoint.AFTER_LEASE_COMMIT)
    await runtime.run_once(limit=1)
    crash.disarm_all()

    from core.send.providers import ProbeVerdict

    _provider().set_probe_verdict(ProbeVerdict.INCONCLUSIVE)

    for _ in range(4):
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    UPDATE jobs SET state = 'ready', run_at = now(),
                                    lease_expires_at = NULL,
                                    max_attempts = max_attempts + 1
                     WHERE kind = 'send' AND ref_id = :ref
                    """
                ),
                {"ref": send_id},
            )
            await session.commit()
        await runtime.run_once(limit=1)

    row = await _send_row(send_id)
    assert row["reconcile_attempts"] == 3, row["reconcile_attempts"]
    assert row["state"] == SendState.UNCERTAIN.value
    assert row["dispatched_at"] is not None, "uncertain is only reachable after a dispatch"
    assert _provider().delivery_count == 0


# ---------------------------------------------------------------------------
# A real hard crash
# ---------------------------------------------------------------------------


async def test_a_subprocess_killed_with_os_exit_reconciles_to_one_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """openspec task 5.11b — no unwinding, no rollback, no cleanup.

    An in-process ``raise`` still runs every ``finally`` on its way out, which is
    exactly the code a real crash skips. ``os._exit(1)`` in a child process is
    the only faithful model, and it forces the delivery ledger onto disk so the
    parent can still count what the dead process did.
    """
    ledger = tmp_path / "deliveries.jsonl"
    ledger.touch()
    monkeypatch.setenv("FAKE_PROVIDER_LEDGER", str(ledger))

    send_id, _ = await _queued_send()

    child_env = {
        **os.environ,
        **TEST_ENV,
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "FAKE_PROVIDER_LEDGER": str(ledger),
        "CRASH_AT": CrashPoint.AFTER_PROVIDER_ACCEPT.value,
        "CRASH_MODE": "exit",
    }
    # Blocking `subprocess.run` inside an async test, deliberately: the whole
    # point is to wait for a child to die, and there is nothing else for this
    # loop to do while it does. (ASYNC221)
    completed = subprocess.run(  # noqa: S603,ASYNC221 - fixed argv, no shell
        [sys.executable, str(REPO_ROOT / "tests" / "_crash_child.py")],
        env=child_env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 1, (
        "the child did not die at the seam: "
        f"rc={completed.returncode} stderr={completed.stderr.decode()[-2000:]}"
    )
    assert len(ledger.read_text().splitlines()) == 1, (
        "the child should have delivered exactly once before dying"
    )

    # The parent now sees a send that is `in_flight` with `dispatched_at` set,
    # a job whose worker will never report back, and a provider that has the
    # message. Only the ledger can prove the last of those.
    await asyncio.sleep(LEASE_EXPIRY_WAIT)
    await runtime.sweep()
    await _drain()

    assert len(ledger.read_text().splitlines()) == 1, "the sweeper re-sent a delivered message"
    row = await _send_row(send_id)
    assert row["state"] == SendState.DELIVERED.value
    assert row["provider_message_id"]
