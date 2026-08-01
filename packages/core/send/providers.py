"""The dispatch interface, and a fake that counts deliveries (task 5.5).

Two methods, and the second one is the interesting half:

``send``                  perform the side effect, return provider evidence
``probe_by_fingerprint``  ask the provider what actually happened

The probe exists **only** to resolve a crash between provider-accept and our own
commit. The durable claim row does the real work; the provider is never the
source of truth about what we sent.

🔴 **Every client is pinned to zero retries** (risks.md R23). The Slack SDK
defaults to ten retries over thirty minutes and Stripe's to two — sensible for
idempotent APIs, catastrophic for a send with no dedup token, because a
slow-but-successful request gets retried *underneath* the gate entirely and no
amount of correct claim logic helps: the duplicate never passes through it.
Retry decisions belong at the job layer, where the ``sends`` row supplies the
context to decide.

⏸️ **The probe interface is deliberately wider than found/not-found.** Spike 1
(risks.md R3 — does Gmail's ``drafts.get`` return 404 after ``drafts.send``?) is
still unrun, so the possibility that the real Gmail probe is *not* a clean
existence check is live. :class:`ProbeResult` therefore carries evidence and a
provider message id alongside the verdict, and ``INCONCLUSIVE`` is a first-class
answer rather than an error. If the spike comes back CONTRADICTED, the Gmail
implementation changes and this signature does not.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx

from core.enums import ProviderKind
from core.errors import ProviderError

#: Where a cross-process test reads the delivery ledger from. Set to a path and
#: every delivery is appended there as JSONL, so a subprocess killed with
#: ``os._exit(1)`` still leaves its evidence behind — which is the only way to
#: count deliveries across a real hard crash (task 5.11b).
LEDGER_ENV = "FAKE_PROVIDER_LEDGER"

#: When set, the fake provider actually speaks HTTP to this URL before
#: recording a delivery, so ``respx`` can assert that exactly one request leaves
#: the process (task 5.12c). A purely in-memory fake cannot see an SDK's
#: internal retries, which is the failure this test exists to catch.
ENDPOINT_ENV = "FAKE_PROVIDER_ENDPOINT"

#: How long a delivery takes. The sibling of ``SLOW_ADAPTER_DELAY_MS``, and it
#: exists for the same reason: a send that completes in microseconds cannot be
#: interrupted, so "kill the worker mid-dispatch and watch it reconcile" is
#: undemonstrable without it. Zero everywhere except when someone is watching.
DELAY_ENV = "FAKE_PROVIDER_DELAY_MS"


class ProbeVerdict(StrEnum):
    #: The provider has the message. It went out.
    FOUND = "found"
    #: The provider definitively does not have it. Safe to dispatch.
    ABSENT = "absent"
    #: We could not establish either. Never treated as "absent" — that
    #: assumption is what double-sends.
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ProbeResult:
    verdict: ProbeVerdict
    #: Present on FOUND when the provider can tell us *which* message it is.
    #: Load-bearing: `sends_delivered_has_evidence` refuses to mark a row
    #: delivered without one, so a probe that finds a message but cannot name it
    #: cannot resolve the send.
    provider_message_id: str | None = None
    #: Free text, persisted on the send. What an operator reads when they open
    #: an `uncertain` row and want to know what we already tried.
    evidence: str = ""


@dataclass(frozen=True)
class SendRequest:
    #: Derived from the idempotency key and stamped somewhere the provider will
    #: hand back — Gmail's stable draft id, Slack's `metadata.event_payload`.
    fingerprint: str
    channel: ProviderKind
    recipient: str
    recipient_display: str
    subject: str | None
    body: str
    #: Gmail's stable draft id, once we have one. Persisted before any send so
    #: recovery is an existence check rather than a search (design D5).
    provider_draft_id: str | None = None
    #: Which connection's credentials to use. A real provider needs it; the fake
    #: ignores it. Carried on the request rather than baked into the provider
    #: instance because :func:`get_provider` returns one instance per process
    #: and a user may have several connections to the same provider.
    connection_id: int | None = None


@dataclass(frozen=True)
class SendReceipt:
    provider_message_id: str
    provider_draft_id: str | None = None


@dataclass(frozen=True)
class PreparedSend:
    """Provider state that must be durable **before** the send is attempted.

    🔴 This exists because Gmail's mechanism is ``drafts.create`` → persist the
    stable draft id → ``drafts.send``, and a crash between the first two steps
    leaves a draft at Google that nothing in our database points at. Nothing can
    then ask about it, so a send that was in fact perfectly recoverable parks in
    ``uncertain``.

    Writing the id *after* delivery — which is where ``_mark_delivered`` used to
    put it — cannot fix that, because the crash being recovered from happens
    before delivery. So the ordering is: prepare, commit, send.

    Providers with no pre-send state (Slack stamps its fingerprint inline on the
    one call) return an empty instance and the commit is unchanged.
    """

    provider_draft_id: str | None = None


class SendProviderProtocol(Protocol):
    provider: str
    #: MUST be 0. Named on the protocol so a new provider cannot quietly ship
    #: with its SDK default.
    max_retries: int

    async def prepare(self, request: SendRequest) -> PreparedSend: ...

    async def send(self, request: SendRequest) -> SendReceipt: ...

    async def probe_by_fingerprint(self, request: SendRequest) -> ProbeResult: ...

    async def discard(self, request: SendRequest) -> None: ...


@dataclass
class Delivery:
    fingerprint: str
    provider: str
    recipient: str
    body: str
    provider_message_id: str
    at: str


class FakeSendProvider:
    """Records every delivery call. The headline assertion of this phase is
    ``provider.delivery_count == 1`` — never a database row count, because our
    own table saying ``delivered`` once proves nothing about how many messages
    the provider received.

    Failure injection is explicit and one-shot: a test arms the next call and
    the arming is consumed, so a leaked arm cannot silently poison later tests.
    """

    max_retries = 0

    def __init__(self, provider: str, *, endpoint: str | None = None) -> None:
        self.provider = provider
        self._endpoint = endpoint
        self._deliveries: list[Delivery] = []
        self._lock = threading.Lock()
        self._next_failure: BaseException | None = None
        self._accept_then_fail = False
        self._probe_verdict: ProbeVerdict | None = None
        self._delay_seconds = 0.0

    # -- test / demo controls ------------------------------------------------

    def fail_next(self, exc: BaseException, *, after_accepting: bool = False) -> None:
        """Arm one failure. ``after_accepting=True`` models the worst real case:
        the provider took the message and the *response* was lost."""
        self._next_failure = exc
        self._accept_then_fail = after_accepting

    def set_probe_verdict(self, verdict: ProbeVerdict | None) -> None:
        self._probe_verdict = verdict

    def set_delay(self, seconds: float) -> None:
        self._delay_seconds = seconds

    def reset(self) -> None:
        with self._lock:
            self._deliveries.clear()
        self._next_failure = None
        self._accept_then_fail = False
        self._probe_verdict = None
        self._delay_seconds = 0.0

    # -- the ledger ----------------------------------------------------------

    @property
    def deliveries(self) -> list[Delivery]:
        ledger = _ledger_path()
        if ledger is None:
            with self._lock:
                return list(self._deliveries)
        return [
            Delivery(**json.loads(line))
            for line in ledger.read_text().splitlines()
            if line.strip()
        ]

    @property
    def delivery_count(self) -> int:
        return len(self.deliveries)

    def deliveries_for(self, fingerprint: str) -> list[Delivery]:
        return [d for d in self.deliveries if d.fingerprint == fingerprint]

    # -- the interface -------------------------------------------------------

    async def prepare(self, request: SendRequest) -> PreparedSend:
        """No pre-send state. The fake's fingerprint travels on the call itself.

        Kept as a real method rather than an optional one so the dispatch path
        has a single shape for every provider — an ``if hasattr`` there would be
        a provider-shaped branch in exactly the module that must not have one.
        """
        return PreparedSend()

    async def discard(self, request: SendRequest) -> None:
        """Nothing to tidy: a fake leaves no artifact at a provider."""
        return None

    async def send(self, request: SendRequest) -> SendReceipt:
        delay = self._delay_seconds or _delay_from_env()
        if delay:
            await asyncio.sleep(delay)

        failure, accept_first = self._take_failure()
        if failure is not None and not accept_first:
            raise failure

        # A real HTTP request when configured, so `respx` can count what leaves
        # the process. The client is built per call with `retries=0` — the whole
        # point (R23) — rather than reusing a module-level client whose settings
        # someone could later "optimise".
        if self._endpoint or _endpoint_from_env():
            await self._post(request)

        message_id = f"fake-{uuid.uuid4().hex[:16]}"
        self._record(request, message_id)

        if failure is not None:
            # Accepted, then the response was lost. From our side this is
            # indistinguishable from never having arrived — which is exactly the
            # case `uncertain` exists for.
            raise failure

        return SendReceipt(
            provider_message_id=message_id,
            provider_draft_id=request.provider_draft_id,
        )

    async def probe_by_fingerprint(self, request: SendRequest) -> ProbeResult:
        if self._probe_verdict is ProbeVerdict.INCONCLUSIVE:
            return ProbeResult(
                verdict=ProbeVerdict.INCONCLUSIVE,
                evidence="probe forced inconclusive",
            )
        matches = self.deliveries_for(request.fingerprint)
        if matches:
            return ProbeResult(
                verdict=ProbeVerdict.FOUND,
                provider_message_id=matches[0].provider_message_id,
                evidence=(
                    f"{len(matches)} message(s) at the provider carry fingerprint "
                    f"{request.fingerprint}"
                ),
            )
        return ProbeResult(
            verdict=ProbeVerdict.ABSENT,
            evidence=f"no message at the provider carries fingerprint {request.fingerprint}",
        )

    # -- internals -----------------------------------------------------------

    def _take_failure(self) -> tuple[BaseException | None, bool]:
        failure, accept_first = self._next_failure, self._accept_then_fail
        self._next_failure = None
        self._accept_then_fail = False
        return failure, accept_first

    async def _post(self, request: SendRequest) -> None:
        url = self._endpoint or _endpoint_from_env()
        assert url is not None
        transport = httpx.AsyncHTTPTransport(retries=0)
        async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
            response = await client.post(
                url,
                json={
                    "fingerprint": request.fingerprint,
                    "to": request.recipient,
                    "subject": request.subject,
                    "body": request.body,
                },
            )
            if response.status_code >= 400:
                raise ProviderError(
                    provider=self.provider,
                    code="fake_transport_error",
                    detail=response.text,
                    status=response.status_code,
                )

    def _record(self, request: SendRequest, message_id: str) -> None:
        delivery = Delivery(
            fingerprint=request.fingerprint,
            provider=self.provider,
            recipient=request.recipient,
            body=request.body,
            provider_message_id=message_id,
            at=datetime.now(UTC).isoformat(),
        )
        ledger = _ledger_path()
        if ledger is not None:
            # Append-only and flushed immediately: a process about to be killed
            # with `os._exit(1)` gets no chance to flush anything later.
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(delivery.__dict__) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        with self._lock:
            self._deliveries.append(delivery)


def _ledger_path() -> Path | None:
    raw = os.getenv(LEDGER_ENV, "")
    return Path(raw) if raw else None


def _endpoint_from_env() -> str | None:
    return os.getenv(ENDPOINT_ENV) or None


def _delay_from_env() -> float:
    raw = os.getenv(DELAY_ENV, "")
    try:
        return float(raw) / 1000 if raw else 0.0
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PROVIDERS: dict[ProviderKind, SendProviderProtocol] = {}


def _default_for(provider: ProviderKind) -> SendProviderProtocol:
    """The real client when the provider is configured, the fake when it is not.

    Same rule as ``core.adapters.live`` applies to search, and for the same
    reason: with no OAuth client there can be no connection and therefore no
    real delivery, so a real provider would fail every send with a token error.
    Falling back keeps ``make smoke`` and the suite meaningful on a machine with
    no credentials, and the moment credentials exist the same dispatch path is
    live with no further change.

    Imported inside the function on purpose — ``core.send.gmail`` imports
    ``core.connections.tokens``, which imports this module's siblings, and a
    module-level import here would close the loop.
    """
    from core.adapters.live import configured_sources

    configured = configured_sources()
    if provider is ProviderKind.GMAIL and configured.get("gmail"):
        from core.send.gmail import GmailSendProvider

        return GmailSendProvider()
    if provider is ProviderKind.SLACK and configured.get("slack"):
        from core.send.slack import SlackSendProvider

        return SlackSendProvider()
    return FakeSendProvider(provider.value)


def get_provider(provider: ProviderKind) -> SendProviderProtocol:
    """One instance per provider, per process.

    Shared rather than per-call so the delivery ledger accumulates across job
    attempts — a fake that forgot between attempts would report one delivery
    however many times it actually sent, which is the exact thing under test.
    The real providers are stateless and build their client per call from
    ``request.connection_id``, so sharing costs them nothing.
    """
    existing = _PROVIDERS.get(provider)
    if existing is None:
        existing = _default_for(provider)
        _PROVIDERS[provider] = existing
    if existing.max_retries != 0:
        raise RuntimeError(
            f"{provider.value} client has max_retries={existing.max_retries}; "
            "provider retries must be pinned to zero (risks.md R23)"
        )
    return existing


def set_provider(provider: ProviderKind, impl: SendProviderProtocol) -> None:
    _PROVIDERS[provider] = impl


def reset_providers() -> None:
    for impl in _PROVIDERS.values():
        if isinstance(impl, FakeSendProvider):
            impl.reset()
    _PROVIDERS.clear()


def fingerprint_for(idempotency_key: str) -> str:
    """What gets stamped on the outbound message.

    The key itself: it is server-generated, opaque, and already unique per send,
    and a derived value would only add a second thing to keep in step. Slack
    carries it in ``metadata.event_payload``; Gmail's equivalent is the stable
    draft id, persisted before any send.
    """
    return idempotency_key
