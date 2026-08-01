"""Gmail delivery: draft-then-send, and a probe that is an existence check.

    drafts.create  ->  persist the stable draft id  ->  drafts.send

🔴 **The middle step is not an optimisation, it is the mechanism.** Gmail's own
drafts guide states the drafts resource "provides a stable ID because the
underlying message IDs change every time the message is replaced", and that on
send "the draft is automatically deleted". Those two facts together make the
draft id a server-side idempotency token whose *existence* answers the only
question a crash leaves open:

    drafts.get(draft_id)  200 -> not yet sent, safe to dispatch
                          404 -> already sent, reconcile the id off the hot path

Deterministic and instant. The rejected alternative — stamping a ``Message-ID``
and probing with ``rfc822msgid:`` — fails twice over: Google documents no
guarantee that a client-supplied ``Message-ID`` survives ``messages.send``, and
Gmail's search index is eventually consistent, so a probe running immediately
after a crash can return a false negative. **A false negative double-sends**
(R3).

⏸️ Spike 1 (does ``drafts.get`` really 404 after ``drafts.send``?) is still
unrun — no Gmail token exists on this machine. The probe is written so that a
CONTRADICTED verdict changes *this file* and not the interface:
:class:`ProbeResult` already carries evidence and a message id alongside the
verdict, and ``INCONCLUSIVE`` is a first-class answer.
"""

from __future__ import annotations

import logging

from core.connections import tokens
from core.errors import ProviderError
from core.providers.gmail import DRAFTS_URL, GmailClient
from core.send.providers import (
    PreparedSend,
    ProbeResult,
    ProbeVerdict,
    SendReceipt,
    SendRequest,
)

logger = logging.getLogger("core.send.gmail")


class GmailSendProvider:
    """🔴 ``max_retries = 0``. ``get_provider`` refuses any other value.

    Not decoration: Google's own client library does not default to zero, and a
    retried send duplicates a message *underneath* the gate — the duplicate
    never passes through the claim, so no amount of correct claim logic helps
    (R23). Our transport is :func:`core.http.provider_client`, which pins
    ``retries=0`` at the connection level; this attribute is the assertion that
    somebody checked.
    """

    provider = "gmail"
    max_retries = 0

    async def prepare(self, request: SendRequest) -> PreparedSend:
        """Create the draft, so its id can be committed before the send.

        **Idempotent on re-entry.** A request that already carries a draft id is
        a retry after a crash, and creating a second draft would leave litter at
        Google and — worse — move the id our recovery path asks about. So the
        existing id is returned untouched.
        """
        if request.provider_draft_id:
            logger.info("reusing draft %s for a re-entered send", request.provider_draft_id)
            return PreparedSend(provider_draft_id=request.provider_draft_id)

        draft_id = await _client(request).create_draft(
            to=request.recipient, subject=request.subject, body=request.body
        )
        return PreparedSend(provider_draft_id=draft_id)

    async def send(self, request: SendRequest) -> SendReceipt:
        draft_id = request.provider_draft_id
        if not draft_id:
            # Unreachable through the dispatch path, which prepares first. Loud
            # rather than falling back to `messages.send`, because that fallback
            # would silently give up the idempotency mechanism at the exact
            # moment it is needed.
            raise ProviderError(
                provider="google",
                code="missing_draft_id",
                detail="a Gmail send reached the provider with no draft id; "
                "draft-then-send is the idempotency mechanism and has no fallback",
                status=500,
            )
        message_id, _thread_id = await _client(request).send_draft(draft_id)
        return SendReceipt(provider_message_id=message_id, provider_draft_id=draft_id)

    async def probe_by_fingerprint(self, request: SendRequest) -> ProbeResult:
        draft_id = request.provider_draft_id
        if not draft_id:
            # No token to ask about. Honest INCONCLUSIVE rather than ABSENT:
            # ABSENT means "definitely not sent, safe to dispatch", and
            # asserting that without evidence is what double-sends.
            return ProbeResult(
                verdict=ProbeVerdict.INCONCLUSIVE,
                evidence="no draft id was recorded for this send, so Gmail "
                "cannot be asked about it",
            )

        client = _client(request)
        try:
            still_a_draft = await client.draft_exists(draft_id)
        except ProviderError as exc:
            return ProbeResult(
                verdict=ProbeVerdict.INCONCLUSIVE,
                evidence=f"drafts.get({draft_id}) failed: {exc}",
            )

        if still_a_draft:
            return ProbeResult(
                verdict=ProbeVerdict.ABSENT,
                evidence=f"draft {draft_id} still exists at Gmail, so it was never sent",
            )

        # The draft is gone, which per Gmail's documented behaviour means it was
        # sent. Recovering *which* message it became is a search — eventually
        # consistent, and deliberately off the hot path. Until it resolves the
        # send is `uncertain` with a link to the user's own mailbox, which is a
        # question a person answers in seconds (task 8.2c).
        return ProbeResult(
            verdict=ProbeVerdict.FOUND,
            provider_message_id=None,
            evidence=(
                f"draft {draft_id} no longer exists at Gmail, which means it was sent; "
                f"the message id could not be recovered from the draft id alone. "
                f"Check {DRAFTS_URL} and your Sent mail."
            ),
        )

    async def discard(self, request: SendRequest) -> None:
        """Delete a draft whose send failed permanently (task 8.2b).

        So a user's Drafts folder is never littered with our failures. Only ever
        acts on an id **we** stored — it never enumerates or modifies drafts the
        user wrote. The scope would permit that, which is exactly why the
        restraint has to live in the code rather than in a promise.
        """
        if request.provider_draft_id:
            await _client(request).delete_draft(request.provider_draft_id)


def _client(request: SendRequest) -> GmailClient:
    connection_id = request.connection_id
    if connection_id is None:
        raise ProviderError(
            provider="google",
            code="missing_connection",
            detail="a Gmail send reached the provider with no connection id",
            status=500,
        )

    async def expire() -> None:
        await tokens.expire_access_token(connection_id)

    return GmailClient(
        get_token=tokens.token_getter(connection_id), expire_access_token=expire
    )
