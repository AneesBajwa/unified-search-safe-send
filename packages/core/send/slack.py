"""Slack delivery, and the bounded reconciliation probe (tasks 8.3-8.4, R5).

**Confirmed: Slack offers no idempotency mechanism whatsoever.** The full
``chat.postMessage`` argument list contains nothing resembling an idempotency
key — ``client_msg_id`` is not an accepted parameter — so a retry after a
timeout produces a perfect duplicate. The fingerprint therefore rides in
``metadata.event_payload``, which is the only field that survives a round trip
through ``conversations.history``.

🔴 **Never reconcile via ``search.messages``.** Its index is eventually
consistent and a just-posted message can be unfindable for minutes — exactly
when the probe runs. That produces a false ``ABSENT``, and a false ``ABSENT``
double-sends. The probe reads ``conversations.history`` on the **single target
channel**, with ``include_all_metadata=true`` and ``oldest = attempt - 60s``.

🔴 **``include_all_metadata=true`` is load-bearing.** Omit it and the payload
silently vanishes from the response leaving only ``event_type`` — so the probe
sees no fingerprint on a message it posted itself and concludes ABSENT.

🔴 **``attempt`` means ``sends.dispatched_at``, not ``now()``.** See
:func:`_window_start`. Writing that sentence down was not enough: the code said
``now()`` for a whole phase while this docstring said otherwise, and every test
passed because a fixture returns its message whatever ``oldest`` asks for.

✅ Spike 2 — CONFIRMED live (2026-08-01) and again through this module's own
credentials: ``metadata.event_payload`` round-trips through
``conversations.history``, and ``include_all_metadata=true`` is genuinely
load-bearing (without it the key reads back ``None``). Had it come back
CONTRADICTED the fingerprint would have had no carrier, and the fallback was
``oldest``-bounded **text** matching on the one target channel, said out loud in
the evidence string. Never ``search.messages``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from core.connections import tokens
from core.errors import ProviderError
from core.providers.slack import SlackClient
from core.send.providers import (
    PreparedSend,
    ProbeResult,
    ProbeVerdict,
    SendReceipt,
    SendRequest,
)

logger = logging.getLogger("core.send.slack")

#: How far back the probe looks. Wide enough to cover a crash plus a restart,
#: narrow enough that this stays a rare bounded read rather than a channel scan.
PROBE_WINDOW_SECONDS = 60
PROBE_LIMIT = 100


class SlackSendProvider:
    """🔴 ``max_retries = 0``. The Slack SDK defaults to **ten retries over
    thirty minutes** — sensible for idempotent APIs, catastrophic here, and
    documented in the wild as a live duplicate-message bug (R23). We do not use
    the SDK; :func:`core.http.provider_client` pins the transport to zero and
    this attribute is what ``get_provider`` checks."""

    provider = "slack"
    max_retries = 0

    async def prepare(self, request: SendRequest) -> PreparedSend:
        """Nothing to persist. Slack's fingerprint travels on the send call
        itself, so there is no pre-send artifact to recover."""
        return PreparedSend()

    async def send(self, request: SendRequest) -> SendReceipt:
        ts, channel_id = await _client(request).post_message(
            channel=request.recipient,
            text=request.body,
            idempotency_key=request.fingerprint,
        )
        if not ts:
            raise ProviderError(
                provider="slack",
                code="post_without_ts",
                detail="chat.postMessage returned ok with no ts, so the message cannot be named",
                status=502,
            )
        # `channel:ts` rather than `ts`: `ts` is unique only within a channel,
        # and the permalink needs both anyway.
        return SendReceipt(provider_message_id=f"{channel_id}:{ts}")

    async def probe_by_fingerprint(self, request: SendRequest) -> ProbeResult:
        client = _client(request)
        oldest = _window_start(request)
        try:
            messages = await client.channel_history(
                request.recipient,
                oldest=oldest,
                limit=PROBE_LIMIT,
                include_metadata=True,
                # 🔴 Spike 2 finding (2026-08-01): `chat:write.public` lets us
                # post to a channel the bot never joined, but `conversations.
                # history` on that same channel returns `not_in_channel`. Without
                # this, every send to an unjoined channel would probe
                # INCONCLUSIVE and park in `uncertain` — recoverable sends
                # stranded by an asymmetry between posting and reading.
                join_if_needed=True,
            )
        except ProviderError as exc:
            # A probe that failed is an answer of sorts, and the answer is "we
            # do not know". Never ABSENT.
            return ProbeResult(
                verdict=ProbeVerdict.INCONCLUSIVE,
                evidence=f"conversations.history on {request.recipient} failed: {exc}",
            )

        for message in messages:
            if message.idempotency_key == request.fingerprint:
                return ProbeResult(
                    verdict=ProbeVerdict.FOUND,
                    provider_message_id=f"{request.recipient}:{message.ts}",
                    evidence=(
                        f"a message in {request.recipient} carries idempotency key "
                        f"{request.fingerprint} in its metadata"
                    ),
                )

        if not messages:
            # An empty window is not evidence of absence: it is equally
            # consistent with the message landing outside the window, or with
            # the bot having lost read access. INCONCLUSIVE, and the send is
            # allowed to park in `uncertain` rather than be sent twice.
            return ProbeResult(
                verdict=ProbeVerdict.INCONCLUSIVE,
                evidence=(
                    f"no messages at all in {request.recipient} within "
                    f"{PROBE_WINDOW_SECONDS}s, so the window proves nothing"
                ),
            )

        if not any(message.idempotency_key is not None for message in messages):
            # We can see messages but none carries metadata. Either the scope is
            # missing or the payload did not round-trip — the very thing spike 2
            # settles. Either way the fingerprint has no carrier here, so this
            # window cannot answer the question.
            return ProbeResult(
                verdict=ProbeVerdict.INCONCLUSIVE,
                evidence=(
                    f"{len(messages)} messages in {request.recipient} within the window and "
                    "none carries metadata; the fingerprint has no carrier, so absence "
                    "cannot be concluded (risks.md R5, spike 2)"
                ),
            )

        return ProbeResult(
            verdict=ProbeVerdict.ABSENT,
            evidence=(
                f"{len(messages)} messages in {request.recipient} within "
                f"{PROBE_WINDOW_SECONDS}s carry metadata and none matches "
                f"{request.fingerprint}"
            ),
        )

    async def discard(self, request: SendRequest) -> None:
        """Nothing to tidy: a failed Slack send leaves no artifact behind."""
        return None


def _window_start(request: SendRequest) -> float:
    """``oldest`` for the probe — anchored to the **dispatch**, never to ``now()``.

    🔴 Found by hand against the live workspace, and it is worth spelling out
    because the tests could not see it. Reconciliation runs only once a lease
    has expired — 300 seconds for a send — so it is *guaranteed* to run minutes
    after the dispatch it is asking about. ``now() - 60s`` therefore describes a
    window that closed long before the message was posted, and the probe could
    never find its own message on any real timeline.

    The failure had two faces, and the second is the dangerous one:

    - **Quiet channel** — the window is empty, the probe answers INCONCLUSIVE
      (correctly, an empty window proves nothing), and a fully recoverable send
      parks in ``uncertain``. Observed live: a message posted at 01:59:05 and
      confirmed present in ``conversations.history`` with its fingerprint
      intact was reported as "no messages at all within 60s".
    - **Busy channel** — other people's messages fill the window, none carries
      our key, and the probe falls through to **ABSENT**. ABSENT means "safe to
      dispatch", so the message is posted a second time. That is the exact
      duplicate this whole design exists to prevent, and a channel with any
      traffic at all is enough to trigger it.

    ``dispatched_at`` is the right anchor rather than merely a working one: it
    is committed immediately *before* the provider call, and
    ``_commit_dispatch_attempt`` COALESCEs it, so across retries it stays the
    first moment a message could possibly have gone out.

    The fallback to ``now()`` covers a probe with no dispatch recorded, which is
    a send that never reached the provider — nothing to find, and
    :func:`core.send.handler.reconcile_send` returns ``NOT_SENT`` before
    reaching here anyway.
    """
    anchor = request.dispatched_at or datetime.now(UTC)
    return anchor.timestamp() - PROBE_WINDOW_SECONDS


def _client(request: SendRequest) -> SlackClient:
    connection_id = request.connection_id
    if connection_id is None:
        raise ProviderError(
            provider="slack",
            code="missing_connection",
            detail="a Slack send reached the provider with no connection id",
            status=500,
        )
    return SlackClient(get_token=tokens.token_getter(connection_id))
