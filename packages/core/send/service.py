"""The gate itself: drafts, the send decision table, replay (tasks 5.1-5.4b, 5.8).

**A draft is inert.** Creating one has no external effect of any kind — no
provider call, no job, nothing that could be observed from outside this process.
That is what makes the confirmation step meaningful: the thing being confirmed
already exists and can be read back before anyone commits to it.

**There is no route that takes a recipient and a body and delivers.** The
absence is the feature. Every path to a provider goes draft -> confirm -> send,
and the send call must carry a digest over content the caller has actually seen.

The duplicate response is a deliberate deviation from Stripe: ``200`` with the
current state and ``idempotent_replay: true``, never ``409``. Stripe's 409 exists
because at the moment of conflict it has no committed record to show you; we
commit the ``in_flight`` row *before* contacting the provider, so we always have
something truthful to return. And our caller includes a human double-tapping a
button on a phone, for whom a 409 is an error toast for a send that is working
perfectly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings

# Through the helper, never hand-rolled. This exact path was built by hand once
# and answered 404 on the one surface where a user meets a revoked grant.
from core.connections import service as connections
from core.enums import JobKind, ProviderKind, SendState
from core.jobs import queue
from core.providers.gmail import DRAFTS_URL as GMAIL_DRAFTS_URL
from core.send import retention
from core.send.claim import claim_send, load_send, load_send_by_key
from core.send.digest import confirmation_digest
from core.send.recipients import resolve_recipient, warning_for


class SendGateError(Exception):
    """A refusal the caller can act on.

    Carries the machine-readable ``code`` and ``classification`` from
    api-design.md's error catalog, so a client can decide whether retrying is
    meaningful without parsing prose.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 422,
        classification: str = "permanent",
        action_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.classification = classification
        # 🔴 Where the customer goes to fix it, when there is anywhere to go.
        # `api-design.md` has always said `connection_needs_reconnect` carries
        # this; the gate raised it without one, so the compose screen showed a
        # refusal with nothing to click — a dead end at the exact moment the
        # product is meant to offer the repair. Built by the caller from the
        # connection it just failed to find, never by a client.
        self.action_url = action_url


def new_idempotency_key() -> str:
    """Server-generated, never caller-supplied.

    A caller-chosen key would make the gate's guarantee depend on the caller's
    discipline. 32 hex characters sits inside ``drafts_key_bounded`` (16-255)
    with room either side.
    """
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftView:
    draft: dict[str, Any]
    confirmation: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"draft": self.draft, "confirmation": self.confirmation}


async def create_draft(
    session: AsyncSession,
    *,
    user_id: int,
    channel: ProviderKind,
    recipient: str,
    body: str,
    subject: str | None = None,
    connection_id: int | None = None,
    in_reply_to_result_id: int | None = None,
) -> DraftView:
    """Write the draft and return it with its confirmation payload.

    No provider is contacted. That is asserted by a test rather than promised
    here, because "we did not call anything" is exactly the kind of claim that
    quietly stops being true.
    """
    if not body.strip():
        raise SendGateError("recipient_invalid", "a draft needs a body")
    if not recipient.strip():
        raise SendGateError("recipient_invalid", "a draft needs a recipient")
    if subject is not None and channel is not ProviderKind.GMAIL:
        # Mirrors `drafts_subject_only_email`. Caught here so the caller gets a
        # sentence rather than a constraint name.
        raise SendGateError(
            "recipient_invalid", f"{channel.value} messages do not carry a subject"
        )

    connection = await _resolve_connection(session, user_id, channel, connection_id)
    # Write-time resolution, provider-backed. Persisted to the row below, and
    # the digest is computed from the row — so the send path never needs a
    # provider to be reachable in order to open the gate.
    recipient_display = await resolve_recipient(
        channel, recipient, connection_id=connection["id"]
    )

    row = (
        await session.execute(
            text(
                """
                INSERT INTO drafts (user_id, connection_id, channel, recipient,
                                    recipient_display, subject, body, idempotency_key,
                                    in_reply_to_result_id)
                VALUES (:user_id, :connection_id, CAST(:channel AS provider_kind),
                        :recipient, :recipient_display, :subject, :body, :key,
                        :in_reply_to_result_id)
                RETURNING *
                """
            ),
            {
                "user_id": user_id,
                "connection_id": connection["id"],
                "channel": channel.value,
                "recipient": recipient,
                "recipient_display": recipient_display,
                "subject": subject,
                "body": body,
                "key": new_idempotency_key(),
                "in_reply_to_result_id": in_reply_to_result_id,
            },
        )
    ).mappings().one()
    return _draft_view(dict(row), connection)


async def get_draft(
    session: AsyncSession, draft_id: uuid.UUID, *, user_id: int
) -> DraftView:
    row = (
        await session.execute(
            text("SELECT * FROM drafts WHERE id = :id AND user_id = :user_id"),
            {"id": draft_id, "user_id": user_id},
        )
    ).mappings().first()
    if row is None:
        raise SendGateError("not_found", "no such draft", status=404)
    connection = await _connection_by_id(session, int(row["connection_id"]))
    return _draft_view(dict(row), connection)


async def update_draft(
    session: AsyncSession,
    draft_id: uuid.UUID,
    *,
    user_id: int,
    recipient: str | None = None,
    subject: str | None = None,
    body: str | None = None,
) -> DraftView:
    """Edit a draft, which **invalidates any digest already held**.

    Nothing here does the invalidating: the digest is computed from the content,
    so changing the content changes the digest by construction. A confirmation
    screen rendered before this call now carries a stale value and its send is
    rejected — which is the behaviour, expressed as arithmetic rather than as
    bookkeeping that could get out of step.
    """
    current = await get_draft(session, draft_id, user_id=user_id)
    channel = ProviderKind(current.draft["channel"])
    next_recipient = recipient if recipient is not None else current.draft["to"]
    if next_recipient == current.draft["to"]:
        # The recipient did not move, so its display cannot have. Reusing the
        # persisted value is what stops an edit to the *body* costing a provider
        # round trip — "cache the name on the draft; do not look it up twice".
        next_display = current.confirmation["recipient_display"]
    else:
        next_display = await resolve_recipient(
            channel, next_recipient, connection_id=int(current.confirmation["connection_id"])
        )
    row = (
        await session.execute(
            text(
                """
                UPDATE drafts
                   SET recipient = :recipient,
                       recipient_display = :recipient_display,
                       subject = COALESCE(:subject, subject),
                       body = COALESCE(:body, body),
                       updated_at = now()
                 WHERE id = :id AND user_id = :user_id
                RETURNING *
                """
            ),
            {
                "id": draft_id,
                "user_id": user_id,
                "recipient": next_recipient,
                "recipient_display": next_display,
                "subject": subject,
                "body": body,
            },
        )
    ).mappings().one()
    connection = await _connection_by_id(session, int(row["connection_id"]))
    return _draft_view(dict(row), connection)


def _draft_view(row: dict[str, Any], connection: dict[str, Any]) -> DraftView:
    channel = ProviderKind(row["channel"])
    digest = confirmation_digest(
        channel=channel,
        recipient=row["recipient"],
        recipient_display=row["recipient_display"],
        subject=row["subject"],
        body=row["body"],
    )
    draft = {
        # The brief's `Draft` shape, closed. `recipient_display`,
        # `connection_id` and the rest ride the sibling `confirmation` object.
        "id": str(row["id"]),
        "channel": channel.value,
        "to": row["recipient"],
        "subject": row["subject"],
        "body": row["body"],
        "idempotency_key": row["idempotency_key"],
    }
    confirmation = {
        "channel": channel.value,
        "recipient_display": row["recipient_display"],
        "connection_display": connection["display_name"],
        "connection_id": connection["id"],
        "subject": row["subject"],
        "body": row["body"],
        "confirm_sha256": digest,
        "warning": warning_for(channel, row["recipient_display"], connection["display_name"]),
    }
    return DraftView(draft=draft, confirmation=confirmation)


# ---------------------------------------------------------------------------
# The send decision table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendOutcome:
    payload: dict[str, Any]
    status: int
    idempotent_replay: bool


async def send_draft(
    session: AsyncSession,
    draft_id: uuid.UUID,
    *,
    user_id: int,
    confirmed_sha256: str | None,
) -> SendOutcome:
    """api-design.md's six-row decision table, in order.

    | existing send | digest | -> |
    |---|---|---|
    | none          | ok      | 201, we own it, dispatch |
    | in_flight     | ok      | 200 + idempotent_replay |
    | delivered     | ok      | 200, same provider_message_id |
    | failed_*/uncertain | ok | 200 with state; **never auto-dispatches** |
    | any           | wrong   | 422 idempotency_key_body_mismatch |
    | —             | missing | 422 confirmation_required |
    | draft edited  | stale   | 422 body_changed_since_confirmation |

    The ordering of the two 422s is the subtle part. A stale digest that matches
    an *existing* send is a legitimate replay of the send that already happened,
    so the existing-row check comes first; only a caller with no send behind
    them can be told their draft moved on.
    """
    view = await get_draft(session, draft_id, user_id=user_id)
    draft = view.draft
    expected = view.confirmation["confirm_sha256"]

    if not confirmed_sha256:
        raise SendGateError(
            "confirmation_required",
            "this send needs a confirmation digest over the exact content being sent",
        )

    existing = await load_send_by_key(
        session, user_id=user_id, idempotency_key=draft["idempotency_key"]
    )
    if existing is not None:
        return _replay(existing, confirmed_sha256)

    if confirmed_sha256 != expected:
        raise SendGateError(
            "body_changed_since_confirmation",
            "the draft changed after it was confirmed; re-read it and confirm again",
        )

    result = await claim_send(
        session,
        user_id=user_id,
        draft_id=uuid.UUID(draft["id"]),
        connection_id=int(view.confirmation["connection_id"]),
        provider=ProviderKind(draft["channel"]),
        idempotency_key=draft["idempotency_key"],
        confirmed_sha256=confirmed_sha256,
    )
    if not result.won:
        # Somebody claimed it between our pre-check and the insert — the
        # double-tap, arriving milliseconds apart. The database picked the
        # winner; we report their row.
        return _replay(result.row, confirmed_sha256)

    await queue.enqueue(
        session,
        kind=JobKind.SEND,
        ref_id=result.row["id"],
        dedupe_key=f"send:{result.row['id']}",
        # One send in flight per connection. Adapter runs stay ungated so
        # fan-out is unaffected (task 2.7f, contracts.md §3).
        partition_key=f"{result.row['provider']}:{result.row['connection_id']}",
    )
    # The claim row and its job commit together: a claim with no job would sit
    # in_flight forever with nothing to move it.
    return SendOutcome(
        payload=send_payload(result.row, idempotent_replay=False),
        status=201,
        idempotent_replay=False,
    )


def _replay(row: dict[str, Any], confirmed_sha256: str) -> SendOutcome:
    if row["confirmed_sha256"] != confirmed_sha256:
        # Same key, genuinely different request. Never silently swallowed:
        # replaying the *first* send's result for a second send's content would
        # tell the caller something false about what was delivered.
        raise SendGateError(
            "idempotency_key_body_mismatch",
            "this idempotency key was already used for different content",
        )
    return SendOutcome(
        payload=send_payload(row, idempotent_replay=True),
        status=200,
        idempotent_replay=True,
    )


def send_payload(row: dict[str, Any], *, idempotent_replay: bool = False) -> dict[str, Any]:
    """Reconstructed from the row every time — there is no response cache.

    Which trivially satisfies the 24-hour replay clock (task 5.7b): the answer
    is always current rather than sometimes stale. ``replay_source`` still
    reports which side of the boundary the caller is on, because "what it looks
    like now" and "what we told you then" are different claims.
    """
    created_at = row.get("created_at") or datetime.now(UTC)
    payload: dict[str, Any] = {
        "send_id": str(row["id"]),
        "state": _state_value(row["state"]),
        "idempotent_replay": idempotent_replay,
        "attempts": row.get("attempts", 0),
        "provider_message_id": row.get("provider_message_id"),
        "delivered_at": row.get("delivered_at"),
        "dispatched_at": row.get("dispatched_at"),
        "reconcile_attempts": row.get("reconcile_attempts", 0),
        "replay_source": retention.replay_source(
            created_at, now=datetime.now(UTC)
        ).value
        if idempotent_replay
        else None,
    }
    error_class = row.get("last_error_class")
    if error_class is not None:
        payload["error"] = {
            "classification": _state_value(error_class),
            "detail": row.get("last_error_detail"),
        }
    return payload


def _state_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


# ---------------------------------------------------------------------------
# Operator actions
# ---------------------------------------------------------------------------


async def operator_retry(
    session: AsyncSession, send_id: uuid.UUID, *, user_id: int
) -> dict[str, Any]:
    """Retry under the identical key (task 5.8).

    The key lives on the ``sends`` row this job already references, so reuse is
    automatic rather than something to remember — and retrying a ``delivered``
    send is a no-op returning the original result, because the key is the same
    and the row already holds the answer.

    An ``uncertain`` send is **refused**. It is not a failure to retry; it is a
    question to answer, and the answer is `POST /sends/{id}/resolve`. Offering a
    retry here would invite exactly the wrong action — re-sending a message that
    may already have arrived.
    """
    row = await load_send(session, send_id, user_id=user_id)
    if row is None:
        raise SendGateError("not_found", "no such send", status=404)

    state = _state_value(row["state"])
    if state == SendState.DELIVERED.value:
        return send_payload(row, idempotent_replay=True)
    if state == SendState.UNCERTAIN.value:
        raise SendGateError(
            # Its own code, not `confirmation_required`. Both refusals used that
            # one and they disagreed about the status — 422 at the gate, 409
            # here — which is a code a client cannot branch on. This one says
            # what it means, and `resolve` is the action it names.
            "resolution_required",
            "this send is in doubt; resolve it explicitly rather than retrying, "
            "so a message that already arrived is not sent twice",
            status=409,
            classification="permanent",
        )
    if state == SendState.IN_FLIGHT.value:
        return send_payload(row)

    await session.execute(
        text(
            """
            UPDATE sends
               SET state = 'in_flight',
                   -- The operator is asking for a fresh look, so the
                   -- reconciliation budget resets with it. `dispatched_at` is
                   -- deliberately kept: it is the evidence that makes the next
                   -- attempt reconcile before it dispatches.
                   reconcile_attempts = 0,
                   updated_at = now()
             WHERE id = :id AND user_id = :user_id
            """
        ),
        {"id": send_id, "user_id": user_id},
    )
    job_id = await session.scalar(
        text(
            "SELECT id FROM jobs WHERE kind = 'send' AND ref_id = :ref ORDER BY id DESC LIMIT 1"
        ),
        {"ref": send_id},
    )
    if job_id is None:
        await queue.enqueue(
            session,
            kind=JobKind.SEND,
            ref_id=send_id,
            partition_key=f"{_state_value(row['provider'])}:{row['connection_id']}",
        )
    else:
        await queue.operator_retry(session, int(job_id))

    refreshed = await load_send(session, send_id, user_id=user_id)
    assert refreshed is not None
    return send_payload(refreshed)


#: The two ways a person can settle a send we could not settle ourselves.
#: Mirrors the CHECK on ``send_resolutions.resolution``.
RESOLUTIONS = ("marked_delivered", "forced_resend")

#: What goes in ``provider_message_id`` when a **person** attested to delivery.
#:
#: 🔴 Deliberately not provider-shaped. ``sends_delivered_has_evidence`` makes
#: "delivered with no evidence" unrepresentable, and that constraint is right —
#: but the evidence for this row is a human who looked, recorded in
#: ``send_resolutions`` with who and when, and the database cannot see that
#: table. So the column carries a marker no provider would ever emit, and the
#: detail payload carries the ``resolution`` block that says what actually
#: happened. A client renders "you marked this delivered", never "the provider
#: confirmed it" — which are different claims, and this is the state where the
#: difference is the entire content.
OPERATOR_ATTESTED = "operator-attested"


async def resolve_send(
    session: AsyncSession,
    send_id: uuid.UUID,
    *,
    user_id: int,
    resolution: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Settle an in-doubt send (api-design.md ``POST /sends/{id}/resolve``).

    ``uncertain`` means *we do not know*, which is not a failure and must never
    be offered a retry — retrying a message that already arrived is the exact
    misfire this product exists to prevent. What it needs is a decision, and
    only a person who can look at the provider can make it. The send detail
    hands them the evidence to do so: ``dispatched_at``, ``reconcile_attempts``,
    and a ``verify_url`` into their own mailbox or channel.

    **``marked_delivered``** — they saw it. The row becomes ``delivered`` with an
    operator attestation rather than a provider id (see ``OPERATOR_ATTESTED``).

    **``forced_resend``** — they looked and it is not there. This is the only
    path in the system that clears ``dispatched_at``, and clearing it is the
    whole point: with it set, :func:`core.send.handler.run_send` reconciles
    instead of dispatching, and a probe that has already failed three times will
    fail a fourth. The user has answered the question the probe could not, so the
    next attempt dispatches. Every other caller leaves that column alone.
    """
    if resolution not in RESOLUTIONS:
        raise SendGateError(
            "recipient_invalid",
            f"resolution must be one of {', '.join(RESOLUTIONS)}",
        )

    row = await load_send(session, send_id, user_id=user_id)
    if row is None:
        raise SendGateError("not_found", "no such send", status=404)
    if _state_value(row["state"]) != SendState.UNCERTAIN.value:
        raise SendGateError(
            "recipient_invalid",
            f"only an uncertain send needs resolving; this one is {_state_value(row['state'])}",
        )

    # Written first and unconditionally: the audit of who decided what is the
    # durable part, and it must not depend on the state change that follows
    # succeeding.
    await session.execute(
        text(
            """
            INSERT INTO send_resolutions (send_id, user_id, resolution, note)
            VALUES (:id, :user_id, :resolution, :note)
            """
        ),
        {"id": send_id, "user_id": user_id, "resolution": resolution, "note": note},
    )

    if resolution == "marked_delivered":
        await session.execute(
            text(
                """
                UPDATE sends
                   SET state = 'delivered',
                       provider_message_id = COALESCE(provider_message_id, :attested),
                       delivered_at = COALESCE(delivered_at, now()),
                       updated_at = now()
                 WHERE id = :id AND user_id = :user_id AND state = 'uncertain'
                """
            ),
            {"id": send_id, "user_id": user_id, "attested": OPERATOR_ATTESTED},
        )
    else:
        await session.execute(
            text(
                """
                UPDATE sends
                   SET state = 'in_flight',
                       reconcile_attempts = 0,
                       dispatched_at = NULL,
                       updated_at = now()
                 WHERE id = :id AND user_id = :user_id AND state = 'uncertain'
                """
            ),
            {"id": send_id, "user_id": user_id},
        )
        # 🔴 The send must come out of this with runnable work behind it, and
        # that takes both branches rather than either one.
        #
        # A send reaches `uncertain` because `reconcile_send` parked it and
        # `run_send` then **returned normally** — so its job is `succeeded`,
        # which `operator_retry` does not match (it resumes `parked`/`failed`).
        # Taking its result on trust left the row `in_flight` with no job at
        # all, and nothing recovers that: the sweeper reclaims expired *leases*,
        # and there is no lease on a job that already finished. The one
        # resolution whose entire purpose is to dispatch quietly did nothing,
        # for ever.
        #
        # So: resume the old job when it is resumable, otherwise book new work.
        # `enqueue` returning None means a live duplicate already exists, which
        # is also "work is scheduled" — the partial unique index is scoped to
        # `ready`/`running` precisely so a finished job cannot block this.
        job_id = await session.scalar(
            text(
                "SELECT id FROM jobs WHERE kind = 'send' AND ref_id = :ref "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"ref": send_id},
        )
        resumed = job_id is not None and await queue.operator_retry(session, int(job_id))
        if not resumed:
            await queue.enqueue(
                session,
                kind=JobKind.SEND,
                ref_id=send_id,
                partition_key=f"{_state_value(row['provider'])}:{row['connection_id']}",
            )

    refreshed = await load_send(session, send_id, user_id=user_id)
    assert refreshed is not None
    payload = send_payload(refreshed)
    payload["resolution"] = resolution
    return payload


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


#: The columns every send view needs, in one place so the list and the detail
#: cannot drift into showing different facts about the same row.
#:
#: The lateral join is what turns "retrying" from a spinner into a countdown
#: (task 11.5c): ``run_at`` is when the next attempt fires and
#: ``backoff_seconds`` is how long the wait was, both read from the job that
#: will actually run. A UI cannot compute either — the backoff has full jitter,
#: so the only honest source is the row the worker will claim.
_SEND_COLUMNS = """
        SELECT s.*, d.recipient_display, d.subject AS draft_subject, d.body,
               c.display_name AS connection_display,
               j.run_at AS job_run_at, j.backoff_seconds AS job_backoff_seconds,
               j.state::text AS job_state
          FROM sends s
          JOIN drafts d ON d.id = s.draft_id
          JOIN connections c ON c.id = s.connection_id
          LEFT JOIN LATERAL (
              SELECT run_at, backoff_seconds, state
                FROM jobs
               WHERE kind = 'send' AND ref_id = s.id
               ORDER BY id DESC LIMIT 1
          ) j ON true
"""


async def list_sends(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = 25,
    include_seed: bool = True,
    before: tuple[datetime, uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Newest first, keyset-paged.

    One row over the limit is fetched deliberately and discarded by the caller:
    it is the evidence that another page exists, which is what lets the response
    say "no more" instead of offering a cursor that resolves to nothing.
    """
    rows = (
        await session.execute(
            text(
                _SEND_COLUMNS
                + """
                 WHERE s.user_id = :user_id
                   AND (:include_seed OR NOT s.is_seed)
                   -- Explicitly cast: asyncpg infers parameter types from
                   -- the statement, and a bare NULL inside a row comparison
                   -- gives it nothing to infer from — the failure is an
                   -- AmbiguousParameterError at execute time, not at parse.
                   AND (CAST(:cursor_at AS timestamptz) IS NULL
                        OR (s.created_at, s.id)
                           < (CAST(:cursor_at AS timestamptz), CAST(:cursor_id AS uuid)))
                 ORDER BY s.created_at DESC, s.id DESC
                 LIMIT :limit
                """
            ),
            {
                "user_id": user_id,
                "limit": limit,
                "include_seed": include_seed,
                "cursor_at": before[0] if before else None,
                "cursor_id": before[1] if before else None,
            },
        )
    ).mappings().all()
    return [_detail_payload(dict(row)) for row in rows]


async def send_detail(
    session: AsyncSession, send_id: uuid.UUID, *, user_id: int
) -> dict[str, Any]:
    row = (
        await session.execute(
            text(_SEND_COLUMNS + " WHERE s.id = :id AND s.user_id = :user_id"),
            {"id": send_id, "user_id": user_id},
        )
    ).mappings().first()
    if row is None:
        raise SendGateError("not_found", "no such send", status=404)

    payload = _detail_payload(dict(row))
    resolution = (
        await session.execute(
            text(
                """
                SELECT resolution, note, created_at
                  FROM send_resolutions WHERE send_id = :id
                 ORDER BY id DESC LIMIT 1
                """
            ),
            {"id": send_id},
        )
    ).mappings().first()
    if resolution is not None:
        # A resolved send says who settled it and how. Without this the history
        # row reads as though the provider answered, when in fact a person did —
        # and which of the two happened is the whole content of this state.
        payload["resolution"] = dict(resolution)
    if payload["state"] == SendState.UNCERTAIN.value:
        # The evidence a person needs to settle it themselves, in seconds,
        # without asking us. `uncertain` renders amber, not red: failed means we
        # know nothing was sent, uncertain means we do not know.
        payload["uncertainty"] = {
            "dispatched_at": row["dispatched_at"],
            "reconcile_attempts": row["reconcile_attempts"],
            "reason": row["last_error_detail"],
            "verify_url": _verify_url(dict(row)),
            "resolutions": ["marked_delivered", "forced_resend"],
        }
    return payload


def _detail_payload(row: dict[str, Any]) -> dict[str, Any]:
    state = _state_value(row["state"])
    payload = send_payload(row)
    # A retry that is genuinely waiting, and only then. Populated for a job that
    # is `ready` with a run_at still ahead of us — a terminal failure has no next
    # attempt, and reporting one would render a countdown that never fires,
    # which is a worse lie than a spinner because it looks like information.
    waiting = (
        row.get("job_state") == "ready"
        and row.get("job_run_at") is not None
        and row["job_run_at"] > datetime.now(UTC)
    )
    payload.update(
        {
            "next_attempt_at": row["job_run_at"] if waiting else None,
            "backoff_seconds": row.get("job_backoff_seconds") if waiting else None,
            "channel": _state_value(row["provider"]),
            "recipient_display": row.get("recipient_display"),
            "connection_display": row.get("connection_display"),
            "subject": row.get("draft_subject"),
            # Exactly what was transmitted, never a summary. This is the record
            # a customer checks when they want to know what went out.
            "body": row.get("body"),
            # Read from settings rather than stored on the row: the ceiling is a
            # deployment decision, and a send that outlives a config change
            # should render against the ceiling actually in force.
            "max_attempts": get_settings().max_attempts,
            "is_seed": row.get("is_seed", False),
            "created_at": row.get("created_at"),
            # Only a terminal *transient* failure is worth an operator retry. A
            # permanent one arrives at the same answer more slowly, and an
            # in-doubt one needs a decision rather than a repeat.
            "retryable_by_operator": state == SendState.FAILED_TRANSIENT.value,
        }
    )
    return payload


def _verify_url(row: dict[str, Any]) -> str:
    """Where a human goes to check for themselves (task 8.2c).

    The artifact of an uncertain send lives in the *user's* account, not in
    ours, so the most useful thing this payload can carry is the means to settle
    it without us. For Gmail that is their own Drafts folder: if the draft is
    still there the message was never sent, which is the same question our probe
    asks and one a person can answer in about three seconds.

    Not a ``rfc822msgid:`` search — that index is eventually consistent, so it
    can tell a person "not found" about a message that was very much sent, which
    is precisely the wrong answer to hand someone deciding whether to re-send.
    """
    provider = _state_value(row["provider"])
    if provider == ProviderKind.GMAIL.value:
        return GMAIL_DRAFTS_URL
    # Slack's documented deep link. It opens the channel in whichever client the
    # user has, and needs no team id — which we do not have on this row and
    # would otherwise have to fetch to build a permalink.
    channel = str(row.get("recipient_display") or "").lstrip("#")
    return f"https://slack.com/app_redirect?channel={channel}"


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


async def _resolve_connection(
    session: AsyncSession,
    user_id: int,
    channel: ProviderKind,
    connection_id: int | None,
) -> dict[str, Any]:
    if connection_id is not None:
        connection = await _connection_by_id(session, connection_id)
        if connection["user_id"] != user_id:
            raise SendGateError("not_found", "no such connection", status=404)
        return connection
    row = (
        await session.execute(
            text(
                """
                SELECT * FROM connections
                 WHERE user_id = :user_id AND provider = CAST(:provider AS provider_kind)
                   AND status = 'active'
                 ORDER BY id LIMIT 1
                """
            ),
            {"user_id": user_id, "provider": channel.value},
        )
    ).mappings().first()
    if row is None:
        # Same two cases the search snapshot distinguishes, and the same reason:
        # offering to *re*connect an account somebody never linked reads as
        # though we lost something of theirs. A row that exists but is not
        # active is repaired in place; no row at all is a first connect.
        broken = (
            await session.execute(
                text(
                    """
                    SELECT id FROM connections
                     WHERE user_id = :user_id
                       AND provider = CAST(:provider AS provider_kind)
                     ORDER BY id LIMIT 1
                    """
                ),
                {"user_id": user_id, "provider": channel.value},
            )
        ).first()
        raise SendGateError(
            "connection_needs_reconnect",
            f"no active {channel.value} connection to send from",
            status=409,
            classification="needs_reconnect",
            action_url=(
                connections.reconnect_url(int(broken[0]), channel.value)
                if broken is not None
                else f"/v1/connections/{channel.value}/authorize"
            ),
        )
    return dict(row)


async def _connection_by_id(session: AsyncSession, connection_id: int) -> dict[str, Any]:
    row = (
        await session.execute(
            text("SELECT * FROM connections WHERE id = :id"), {"id": connection_id}
        )
    ).mappings().first()
    if row is None:
        raise SendGateError("not_found", "no such connection", status=404)
    return dict(row)
