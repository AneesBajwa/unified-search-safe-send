"""The send job: dispatch, reconcile, resolve (openspec tasks 5.6-5.8b).

Four failure modes, four mechanisms. This module owns two of them — the crash
window and the transient ladder — and defers the other two (duplicate requests,
operator retry) to ``core.send.service``.

🔴 **Every read and write of the ``sends`` row happens in its own session**, and
never in the job's session. Two reasons, and only the second one is obvious.

The visible one: ``dispatched_at`` must be **committed before the provider
call**. If it is written in the same transaction as the delivery result, a crash
rolls it back to NULL and the sweeper cannot distinguish *never dispatched* from
*crashed mid-dispatch* — the only distinction it exists to make (risks.md R6).

The one that cost an hour in phase 1: the job's session stays open for the whole
handler, so a row lock taken in it and a write to the same row from a second
session block each other until ``transaction_timeout`` fires **60 seconds
later**. It presents as a job that is merely slow and then fails with a timeout
that points at the provider. Keeping ``sends`` entirely out of the job's session
makes that shape unreachable rather than merely avoided.

**A send with ``dispatched_at`` set is never dispatched again without asking the
provider first.** That check lives here as well as in the sweeper, on purpose:
the sweeper is the designed path, and this is the one that still holds if a
future change routes a stale send back through a plain retry.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.connections import service as connections
from core.db import session_scope
from core.enums import ErrorClass, JobKind, ProviderKind, SendState
from core.errors import ProviderError, classify
from core.jobs.queue import ClaimedJob
from core.jobs.runtime import ReconcileOutcome, register_handler, register_reconciler
from core.send import crash
from core.send.crash import CrashPoint
from core.send.providers import (
    ProbeVerdict,
    SendProviderProtocol,
    SendReceipt,
    SendRequest,
    fingerprint_for,
    get_provider,
)

logger = logging.getLogger("core.send")

#: Bounded at 3, and the bound is the point. A revoked grant makes every probe
#: fail the same way forever, so an unbounded reconciler would spin until
#: someone noticed. Beyond three the send parks permanently in `uncertain` with
#: its evidence attached, which is a thing a human can act on in seconds.
MAX_RECONCILE_ATTEMPTS = 3


class ReconcileVerdict(StrEnum):
    #: The provider has no record. Dispatching is safe.
    NOT_SENT = "not_sent"
    #: A delivery was found and written to the row. Nothing left to do.
    ADOPTED = "adopted"
    #: Out of attempts, or found something we cannot name. Parked in `uncertain`.
    PARKED_UNCERTAIN = "parked_uncertain"
    #: The probe could not tell us, and attempts remain.
    RETRY_LATER = "retry_later"
    #: The send already reached a terminal state under someone else.
    ALREADY_TERMINAL = "already_terminal"


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


@register_handler(JobKind.SEND)
async def run_send(session: AsyncSession, job: ClaimedJob) -> None:
    send = await _load_send(job.ref_id)
    if send is None:
        logger.warning("send %s no longer exists; job %s is a no-op", job.ref_id, job.id)
        return

    if send["state"] != SendState.IN_FLIGHT.value:
        # A `failed_*` or `uncertain` send is never auto-dispatched — that is an
        # operator's decision, and re-sending a message that may already have
        # arrived is the failure this whole design is arranged around (5.13b).
        logger.info("send %s is %s; nothing to dispatch", send["id"], send["state"])
        return

    if send["dispatched_at"] is not None:
        verdict = await reconcile_send(send["id"])
        if verdict is ReconcileVerdict.RETRY_LATER:
            # Transient so the job backs off and reconciles again rather than
            # dispatching. Raised as a ProviderError so it flows through the one
            # classification boundary like any other provider outcome.
            raise ProviderError(
                provider=str(send["provider"]),
                code="reconciliation_inconclusive",
                detail="could not establish whether the message was sent; will re-probe",
                status=503,
            )
        if verdict is not ReconcileVerdict.NOT_SENT:
            return  # adopted, parked, or already terminal

    draft = await _load_draft(send["draft_id"])
    if draft is None:
        raise LookupError(f"send {send['id']} references draft {send['draft_id']}, which is gone")

    request = _request_for(send, draft)
    provider = get_provider(ProviderKind(send["provider"]))

    # Provider state that has to be durable before the send is attempted —
    # Gmail's ``drafts.create``. Inert: a draft is not a message, so a crash
    # here leaves no delivery and the send is still safely re-dispatchable.
    # Providers with nothing to prepare return an empty result and the ordering
    # below is unchanged for them.
    try:
        prepared = await provider.prepare(request)
    except Exception as exc:
        await _finalize_failure(send["id"], exc, job)
        raise
    if prepared.provider_draft_id:
        request = replace(request, provider_draft_id=prepared.provider_draft_id)

    # ⬇ THE COMMIT THAT MAKES CRASH RECOVERY POSSIBLE. Its own transaction, and
    # it lands before the provider is contacted. Everything downstream of here
    # is recoverable precisely because this is already durable — and that now
    # includes the draft id, without which a crash between `drafts.create` and
    # `drafts.send` would leave a draft at Google that nothing in our database
    # points at, so reconciliation could not ask about it and a recoverable send
    # would park in `uncertain`.
    await _commit_dispatch_attempt(
        send["id"], provider_draft_id=prepared.provider_draft_id
    )
    crash.seam(CrashPoint.AFTER_LEASE_COMMIT)

    try:
        receipt = await provider.send(request)
    except Exception as exc:
        await _finalize_failure(send["id"], exc, job, provider=provider, request=request)
        raise

    crash.seam(CrashPoint.AFTER_PROVIDER_ACCEPT)
    crash.seam(CrashPoint.BEFORE_DELIVERY_COMMIT)

    applied = await _mark_delivered(send["id"], receipt)
    crash.seam(CrashPoint.AFTER_DELIVERY_COMMIT)

    # A delivery is the strongest evidence a connection is healthy. After the
    # delivery commit, never before: this is bookkeeping, and it must not sit
    # anywhere on the path between the provider accepting and us recording that.
    if send["connection_id"] is not None:
        await connections.record_success(int(send["connection_id"]))

    if not applied:
        # The compare-and-swap lost: someone else already resolved this send —
        # a sweeper that adopted the delivery, or an operator. Said out loud
        # rather than silently overwritten, because "two things resolved one
        # send" is exactly the kind of fact that must not be invisible.
        logger.warning(
            "send %s was no longer in_flight when its delivery committed; "
            "provider_message_id=%s was not written",
            send["id"],
            receipt.provider_message_id,
        )


@register_reconciler(JobKind.SEND)
async def reconcile_send_job(_session: AsyncSession, job: ClaimedJob) -> ReconcileOutcome:
    """What the sweeper calls for a send whose worker died holding the lease.

    ``RETRY_LATER`` maps to ``SAFE_TO_DISPATCH`` and that is not a contradiction:
    requeuing a send means running :func:`run_send`, which reconciles *again*
    before it dispatches. So the requeue is a retry of the reconciliation, not a
    blind re-send — and the attempt bound above guarantees it terminates.
    """
    verdict = await reconcile_send(job.ref_id)
    if verdict in (ReconcileVerdict.NOT_SENT, ReconcileVerdict.RETRY_LATER):
        return ReconcileOutcome.SAFE_TO_DISPATCH
    return ReconcileOutcome.RESOLVED


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


async def reconcile_send(send_id: uuid.UUID, *, note: str = "") -> ReconcileVerdict:
    """Ask the provider what actually happened, and record the answer.

    The probe exists **only** to resolve a crash between provider-accept and our
    own commit. It is never the source of truth about what we sent — the claim
    row is. What it settles is a single question: did the side effect happen?
    """
    send = await _load_send(send_id)
    if send is None or send["state"] != SendState.IN_FLIGHT.value:
        return ReconcileVerdict.ALREADY_TERMINAL
    if send["dispatched_at"] is None:
        # Never reached the provider. The `dispatched_at` commit is what makes
        # this answerable at all.
        return ReconcileVerdict.NOT_SENT
    if send["reconcile_attempts"] >= MAX_RECONCILE_ATTEMPTS:
        await _mark_uncertain(send_id, note or "reconciliation attempts exhausted")
        return ReconcileVerdict.PARKED_UNCERTAIN

    draft = await _load_draft(send["draft_id"])
    if draft is None:
        await _mark_uncertain(send_id, "the draft this send was built from no longer exists")
        return ReconcileVerdict.PARKED_UNCERTAIN

    attempts = await _bump_reconcile_attempts(send_id)
    request = _request_for(send, draft)
    provider = get_provider(ProviderKind(send["provider"]))

    try:
        probe = await provider.probe_by_fingerprint(request)
    except Exception as exc:  # noqa: BLE001 - a failed probe is an answer, not a crash
        probe_evidence = f"probe failed: {exc}"
        logger.warning("reconciliation probe for send %s failed: %s", send_id, exc)
        if attempts >= MAX_RECONCILE_ATTEMPTS:
            await _mark_uncertain(send_id, probe_evidence)
            return ReconcileVerdict.PARKED_UNCERTAIN
        await _record_error(send_id, ErrorClass.TRANSIENT, probe_evidence)
        return ReconcileVerdict.RETRY_LATER

    if probe.verdict is ProbeVerdict.FOUND:
        if probe.provider_message_id is None:
            # Found, but the provider cannot name it — so we cannot satisfy
            # `sends_delivered_has_evidence`, and claiming delivery without
            # evidence is exactly what that constraint refuses. Park it honestly.
            await _mark_uncertain(
                send_id,
                f"a matching message exists but carries no id we can record: {probe.evidence}",
            )
            return ReconcileVerdict.PARKED_UNCERTAIN
        adopted = await _mark_delivered(
            send_id,
            SendReceipt(
                provider_message_id=probe.provider_message_id,
                provider_draft_id=send["provider_draft_id"],
            ),
            evidence=probe.evidence,
        )
        logger.info("send %s adopted an existing delivery (%s)", send_id, probe.evidence)
        return ReconcileVerdict.ADOPTED if adopted else ReconcileVerdict.ALREADY_TERMINAL

    if probe.verdict is ProbeVerdict.ABSENT:
        await _record_error(
            send_id, ErrorClass.TRANSIENT, f"reconciled: {probe.evidence}"
        )
        return ReconcileVerdict.NOT_SENT

    if attempts >= MAX_RECONCILE_ATTEMPTS:
        await _mark_uncertain(send_id, probe.evidence or note)
        return ReconcileVerdict.PARKED_UNCERTAIN
    await _record_error(send_id, ErrorClass.TRANSIENT, probe.evidence or "probe inconclusive")
    return ReconcileVerdict.RETRY_LATER


# ---------------------------------------------------------------------------
# Row transitions — each in its own transaction, each a compare-and-swap
# ---------------------------------------------------------------------------


async def _commit_dispatch_attempt(
    send_id: uuid.UUID, *, provider_draft_id: str | None = None
) -> None:
    """``dispatched_at``, the attempt counter, and any pre-send provider state.

    Its own transaction, immediately before the provider call — which is the
    whole basis of crash recovery, and the reason the draft id belongs here
    rather than in ``_mark_delivered``. Writing it after delivery cannot help a
    crash that happens before delivery.

    ``COALESCE`` on both columns rather than plain assignment: on a retry after
    a crash the *original* dispatch time is the more useful fact — it is when
    the message might have gone out — and the *original* draft id is the one
    Gmail will be asked about, so neither may be overwritten by a later attempt.

    One short-lived session, like every other read and write of this row. A row
    lock held open across a provider call deadlocks against the failure path for
    a full ``transaction_timeout``, which presents as a job that is merely slow
    and then fails pointing at the provider.
    """
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE sends
                   SET dispatched_at = COALESCE(dispatched_at, now()),
                       provider_draft_id = COALESCE(provider_draft_id, :draft_id),
                       attempts = attempts + 1,
                       updated_at = now()
                 WHERE id = :id AND state = 'in_flight'
                """
            ),
            {"id": send_id, "draft_id": provider_draft_id},
        )
        await session.commit()


async def _mark_delivered(
    send_id: uuid.UUID, receipt: SendReceipt, *, evidence: str = ""
) -> bool:
    """Compare-and-swap to ``delivered``. False means we no longer owned it.

    The rowcount is checked on every CAS in this module. A worker whose lease
    expired mid-job must not be able to overwrite the state written by the
    worker that legitimately took over.
    """
    async with session_scope() as session:
        result = await session.execute(
            text(
                """
                UPDATE sends
                   SET state = 'delivered',
                       provider_message_id = :message_id,
                       provider_draft_id = COALESCE(:draft_id, provider_draft_id),
                       delivered_at = now(),
                       last_error_class = NULL,
                       last_error_detail = CASE WHEN :evidence = '' THEN NULL ELSE :evidence END,
                       updated_at = now()
                 WHERE id = :id AND state = 'in_flight'
                """
            ),
            {
                "id": send_id,
                "message_id": receipt.provider_message_id,
                "draft_id": receipt.provider_draft_id,
                "evidence": evidence,
            },
        )
        await session.commit()
        return _rowcount(result) == 1


async def _mark_uncertain(send_id: uuid.UUID, reason: str) -> bool:
    """The in-doubt state. Amber, not red.

    Reachable only with ``dispatched_at`` set — the database enforces that with
    ``sends_uncertain_was_dispatched``, so "we are unsure about a send that
    never left" is unrepresentable rather than merely unlikely.
    """
    async with session_scope() as session:
        result = await session.execute(
            text(
                """
                UPDATE sends
                   SET state = 'uncertain',
                       last_error_class = 'transient',
                       last_error_detail = :reason,
                       updated_at = now()
                 WHERE id = :id AND state = 'in_flight' AND dispatched_at IS NOT NULL
                """
            ),
            {"id": send_id, "reason": reason},
        )
        await session.commit()
        return _rowcount(result) == 1


async def _record_error(send_id: uuid.UUID, error_class: ErrorClass, detail: str) -> None:
    """Attach the last error without moving the state. The send is still in
    flight; this is what the UI renders under "attempt 3 of 6"."""
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE sends
                   SET last_error_class = CAST(:error_class AS error_class),
                       last_error_detail = :detail,
                       updated_at = now()
                 WHERE id = :id AND state = 'in_flight'
                """
            ),
            {"id": send_id, "error_class": error_class.value, "detail": detail},
        )
        await session.commit()


async def _set_terminal(
    send_id: uuid.UUID, state: SendState, error_class: ErrorClass, detail: str
) -> None:
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE sends
                   SET state = CAST(:state AS send_state),
                       last_error_class = CAST(:error_class AS error_class),
                       last_error_detail = :detail,
                       updated_at = now()
                 WHERE id = :id AND state = 'in_flight'
                """
            ),
            {
                "id": send_id,
                "state": state.value,
                "error_class": error_class.value,
                "detail": detail,
            },
        )
        await session.commit()


async def _bump_reconcile_attempts(send_id: uuid.UUID) -> int:
    async with session_scope() as session:
        count = await session.scalar(
            text(
                """
                UPDATE sends SET reconcile_attempts = reconcile_attempts + 1,
                                 updated_at = now()
                 WHERE id = :id
                RETURNING reconcile_attempts
                """
            ),
            {"id": send_id},
        )
        await session.commit()
    return int(count or 0)


async def _finalize_failure(
    send_id: uuid.UUID,
    exc: Exception,
    job: ClaimedJob,
    *,
    provider: SendProviderProtocol | None = None,
    request: SendRequest | None = None,
) -> None:
    """Decide what the *send* becomes, mirroring what the job is about to do.

    Deliberately mirrors ``queue.fail``'s ladder rather than reading it back:
    the job's fate is decided in ``runtime._execute`` after this returns, and
    threading the outcome back here would mean the send state could only be
    written after the job's transaction had already committed a different story.

    The one place the two ladders differ is the ceiling. A job at the ceiling is
    simply parked; a *send* at the ceiling has to answer "did anything go out?"
    before it is allowed to say `failed`. So it reconciles once more, and only
    a definitive "the provider has no record" earns `failed_transient`.
    """
    error_class = classify(exc, provider=exc.provider if isinstance(exc, ProviderError) else None)
    detail = str(exc)

    if error_class is ErrorClass.TRANSIENT and job.attempts < job.max_attempts:
        await _record_error(send_id, error_class, detail)
        return

    if error_class is ErrorClass.TRANSIENT:
        verdict = await reconcile_send(send_id, note=f"attempt ceiling reached: {detail}")
        if verdict is ReconcileVerdict.NOT_SENT:
            await _set_terminal(send_id, SendState.FAILED_TRANSIENT, error_class, detail)
        elif verdict is ReconcileVerdict.RETRY_LATER:
            await _mark_uncertain(send_id, detail)
        return

    # `needs_reconnect` and `config` land in `failed_permanent` because the
    # send_state enum has four terminal members and none of them is a
    # classification. The class itself is preserved on `last_error_class`, which
    # is what lets the API render a reconnect action rather than a generic
    # failure — the distinction lives in the error class, not in the state.
    await _set_terminal(send_id, SendState.FAILED_PERMANENT, error_class, detail)

    # Tidy the pre-send artifact, if there was one and this is really over
    # (task 8.2b). Gmail's draft is the only case today; other providers return
    # immediately. Best-effort and never allowed to mask the real failure — the
    # send has already been given its terminal state above, and a tidy-up that
    # threw here would replace a useful provider error with a housekeeping one.
    if provider is not None and request is not None and request.provider_draft_id:
        try:
            await provider.discard(request)
        except Exception as tidy_failure:  # noqa: BLE001 - see above
            logger.warning(
                "could not discard the draft for send %s: %s", send_id, tidy_failure
            )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _load_send(send_id: uuid.UUID) -> dict[str, Any] | None:
    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, user_id, draft_id, connection_id, provider::text AS provider,
                           idempotency_key, state::text AS state, confirmed_sha256,
                           provider_draft_id, provider_message_id, attempts,
                           reconcile_attempts, dispatched_at, delivered_at,
                           last_error_class::text AS last_error_class, last_error_detail
                      FROM sends WHERE id = :id
                    """
                ),
                {"id": send_id},
            )
        ).mappings().first()
    return dict(row) if row is not None else None


async def _load_draft(draft_id: uuid.UUID) -> dict[str, Any] | None:
    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, channel::text AS channel, recipient, recipient_display,
                           subject, body, idempotency_key
                      FROM drafts WHERE id = :id
                    """
                ),
                {"id": draft_id},
            )
        ).mappings().first()
    return dict(row) if row is not None else None


def _request_for(send: dict[str, Any], draft: dict[str, Any]) -> SendRequest:
    return SendRequest(
        fingerprint=fingerprint_for(str(send["idempotency_key"])),
        channel=ProviderKind(draft["channel"]),
        recipient=draft["recipient"],
        recipient_display=draft["recipient_display"],
        subject=draft["subject"],
        body=draft["body"],
        provider_draft_id=send["provider_draft_id"],
        # Which credentials this send uses. Read from the `sends` row rather
        # than resolved here, so a send always goes out from the connection it
        # was claimed against even if the user has since added another.
        connection_id=int(send["connection_id"]) if send.get("connection_id") else None,
        # What a time-bounded probe measures backwards from. Committed before
        # the provider call, so it is durable exactly when reconciliation needs
        # it — and it is the *original* attempt, because `_commit_dispatch_
        # attempt` COALESCEs rather than overwrites.
        dispatched_at=send.get("dispatched_at"),
    )


def _rowcount(result: Any) -> int:
    return int(result.rowcount)
