"""A property test over the whole send lifecycle (openspec task 5.12d).

The example-based tests each check one story I thought of. This one interleaves
send, worker, crash, lease expiry, sweep and operator retry in orders nobody
wrote down, and holds a single invariant across all of them:

    the provider never receives more than one message

That invariant is the product promise, stated in the smallest possible form. It
is also the one most likely to find a real bug, because the failure mode this
design guards against is a *sequence* — and sequences are exactly what a
human-written test suite under-samples.

The machine drives real Postgres through the real job runtime. Slow, on purpose:
a fast version would be mocking the parts that make the question hard.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from typing import Any

import pytest
from conftest import ALL_TABLES, make_api_key
from core.db import dispose_engine, session_scope
from core.enums import ProviderKind
from core.errors import ProviderError
from core.jobs import runtime
from core.send import crash, providers, service
from core.send.crash import CrashPoint
from core.send.providers import FakeSendProvider, ProbeVerdict
from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from hypothesis.strategies import sampled_from
from sqlalchemy import text

CHANNEL = "C024BE91L"


class SendLifecycle(RuleBasedStateMachine):
    """One send, one idempotency key, arbitrary interleavings of everything else."""

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.deliveries_seen = 0
        self._run(self._setup())

    # -- plumbing ------------------------------------------------------------

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return self.loop.run_until_complete(coro)

    async def _setup(self) -> None:
        async with session_scope() as session:
            await session.execute(
                text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
            )
            await session.commit()
        providers.reset_providers()
        crash.disarm_all()

        key = await make_api_key()
        self.user_id = int(str(key["user_id"]))
        async with session_scope() as session:
            view = await service.create_draft(
                session,
                user_id=self.user_id,
                channel=ProviderKind.SLACK,
                recipient=CHANNEL,
                body="Confirming for Thursday.",
            )
            await session.commit()
        self.draft_id = uuid.UUID(view.draft["id"])
        self.digest = view.confirmation["confirm_sha256"]
        self.send_id: uuid.UUID | None = None

    def _provider(self) -> FakeSendProvider:
        impl = providers.get_provider(ProviderKind.SLACK)
        assert isinstance(impl, FakeSendProvider)
        return impl

    # -- rules ---------------------------------------------------------------

    @rule()
    def send(self) -> None:
        """A caller pressing the button — first time, or the fifth."""

        async def go() -> None:
            async with session_scope() as session:
                outcome = await service.send_draft(
                    session,
                    self.draft_id,
                    user_id=self.user_id,
                    confirmed_sha256=self.digest,
                )
                await session.commit()
            self.send_id = uuid.UUID(outcome.payload["send_id"])

        self._run(go())

    @rule()
    def work(self) -> None:
        """A worker pass, with any backoff already elapsed.

        Making due jobs runnable is folded in rather than being its own rule:
        as a separate rule the random walk needs two specific steps in order to
        reach a *second* attempt, which halves how often the machine gets
        anywhere near the interesting states. "Time passed and a worker ran" is
        one event in the world anyway.
        """

        async def go() -> None:
            async with session_scope() as session:
                await session.execute(
                    text("UPDATE jobs SET run_at = now() WHERE state = 'ready'")
                )
                await session.commit()
            await runtime.run_once(limit=5)

        self._run(go())

    @rule(point=sampled_from(list(CrashPoint)))
    def crash_mid_dispatch(self, point: CrashPoint) -> None:
        async def go() -> None:
            crash.arm(point)
            try:
                await runtime.run_once(limit=5)
            finally:
                crash.disarm_all()

        self._run(go())

    @rule()
    def the_lease_expires_and_a_sweeper_runs(self) -> None:
        """A worker that never came back, then the sweeper finding it.

        Expiry is forced rather than waited out — five real minutes per step
        would make this test unrunnable, and the sweeper cannot tell a lease
        that expired from one that was made to. Nothing is genuinely mid-flight
        when a rule fires, since the machine drives everything itself.
        """

        async def go() -> None:
            async with session_scope() as session:
                await session.execute(
                    text(
                        """
                        UPDATE jobs SET lease_expires_at = now() - interval '1 second'
                         WHERE state = 'running'
                        """
                    )
                )
                await session.commit()
            await runtime.sweep()

        self._run(go())

    @rule()
    def the_response_gets_lost(self) -> None:
        """The provider accepts, then the answer never arrives.

        The single most important rule in this machine. It is the only way the
        job reaches "in flight, already dispatched, and about to be retried by
        the ordinary transient ladder" — a path the sweeper never touches, and
        therefore the one where reconcile-before-dispatch in the *handler* is
        load-bearing rather than belt-and-braces. Without it the machine's
        crash rules are all caught by the sweeper's reconciler and the handler
        could quietly stop checking.
        """
        self._provider().fail_next(
            ProviderError(
                provider="slack",
                code="request_timeout",
                detail="the post may have succeeded",
                status=200,
            ),
            after_accepting=True,
        )

    @rule(verdict=sampled_from([None, ProbeVerdict.INCONCLUSIVE]))
    def the_probe_gets_less_reliable(self, verdict: ProbeVerdict | None) -> None:
        """A provider that cannot answer is not the same as one that says no,
        and conflating them is what double-sends."""
        self._provider().set_probe_verdict(verdict)

    @rule()
    def operator_retries(self) -> None:
        async def go() -> None:
            if self.send_id is None:
                return
            async with session_scope() as session:
                try:
                    await service.operator_retry(
                        session, self.send_id, user_id=self.user_id
                    )
                except service.SendGateError:
                    # Refusing to retry an in-doubt send is correct behaviour,
                    # not a failed step.
                    pass
                await session.commit()

        self._run(go())

    # -- the invariant -------------------------------------------------------

    @invariant()
    def the_provider_never_receives_two_messages(self) -> None:
        count = self._provider().delivery_count
        assert count <= 1, f"the provider received {count} copies of one send"
        # Deliveries are also monotonic — a ledger that went down would mean the
        # count is being reset underneath the invariant, which would make it
        # vacuous rather than true.
        assert count >= self.deliveries_seen
        self.deliveries_seen = count

    @invariant()
    def a_delivered_send_always_carries_its_evidence(self) -> None:
        async def go() -> None:
            async with session_scope() as session:
                bad = await session.scalar(
                    text(
                        """
                        SELECT count(*) FROM sends
                         WHERE state = 'delivered'
                           AND (provider_message_id IS NULL OR delivered_at IS NULL)
                        """
                    )
                )
            assert bad == 0

        self._run(go())

    def teardown(self) -> None:
        self._run(dispose_engine())
        self.loop.close()


#: Tuned by mutation rather than by taste: with reconcile-before-dispatch
#: deleted from the handler, these settings find the double-send. Smaller ones
#: did not, which is the only evidence that matters for a property test —
#: "it passes" is not a measure of whether it can fail.
SendLifecycle.TestCase.settings = settings(
    max_examples=40,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


@pytest.mark.usefixtures("clean_db")
@pytest.mark.timeout(300)
def test_the_send_lifecycle_never_delivers_twice() -> None:
    """The whole gate, in orders nobody wrote down.

    The global 30 s timeout is raised here on purpose: this is the one test in
    the suite whose value is proportional to how long it is allowed to search.
    """
    SendLifecycle.TestCase().runTest()
