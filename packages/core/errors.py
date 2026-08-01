"""The single error-classification boundary (design D9, openspec tasks 3.1-3.2b).

One function decides whether a failure is retried, surfaced, or turned into a
reconnect prompt. One place to look, one place to fix, and — because the tables
below are data rather than scattered ``if`` statements — one place to test
against captured provider payloads.

**Never infer a class from the HTTP status alone.** Slack returns ``200`` with
``{"ok": false, "error": "..."}`` for almost every failure, and Google's ``403``
is retryable or terminal depending entirely on the ``reason`` field. Status is
consulted only as a last resort, when the provider gave us no code at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.adapters.types import TokenUnavailable
from core.enums import ErrorClass

# ---------------------------------------------------------------------------
# Google
#
# 🔴 Classify on the `error` field only. Google has changed `error_description`
# strings before; branching on them is how a working classifier quietly stops
# classifying. Log the description as telemetry, never branch on it.
# ---------------------------------------------------------------------------

# The only reliable revocation signal Google emits. It collapses eight distinct
# causes (user revoked, password changed, token expired, account deleted, …) and
# there is no way to tell them apart — so do not try. Never retried.
GOOGLE_NEEDS_RECONNECT = frozenset({"invalid_grant"})

# Our bug, not the user's. Showing a reconnect prompt for these sends the user
# round in circles reconnecting a grant that was never the problem (R24).
GOOGLE_CONFIG = frozenset(
    {
        "invalid_client",
        "deleted_client",
        "unauthorized_client",
        "invalid_scope",
        "invalid_request",
    }
)

GOOGLE_TRANSIENT = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "backendError",
        "internalError",
        # A raw 401 is NOT terminal: the ladder is refresh -> retry once ->
        # escalate only if the *refresh* returns invalid_grant. Treating a bare
        # 401 as a dead grant disconnects healthy users constantly (R24, task
        # 6.3c owns the ladder itself).
        "authError",
    }
)

GOOGLE_PERMANENT = frozenset(
    {
        "insufficientPermissions",  # scope added after consent; re-consent, not reconnect
        "domainPolicy",  # a Workspace admin has to act
        "dailyLimitExceeded",  # quota gone for the day; same 403 family as the transient ones
        "invalidArgument",
        "failedPrecondition",
        "invalid_recipient",
    }
)

# ---------------------------------------------------------------------------
# Slack — everything at HTTP 200 except rate limiting, which is 429.
# ---------------------------------------------------------------------------

SLACK_TRANSIENT = frozenset(
    {
        "ratelimited",
        # ⚠️ The post may have succeeded. Transient here means "the job may run
        # again", and the send path must reconcile before it re-dispatches —
        # which is exactly why sends carry the RECONCILE recovery policy.
        "request_timeout",
        "service_unavailable",
        "internal_error",
        "fatal_error",
    }
)

SLACK_NEEDS_RECONNECT = frozenset(
    {
        "token_revoked",  # user token: app removed / user deleted
        "account_inactive",  # the bot-token equivalent
        "token_expired",
        "invalid_auth",
        "not_authed",
        "org_login_required",
    }
)

SLACK_PERMANENT = frozenset(
    {
        "missing_scope",  # needs reinstall; the response names the scope
        "not_allowed_token_type",  # bot token sent to a user-only method — our bug
        "channel_not_found",
        "is_archived",
        "no_permission",
        "restricted_action",
        "no_text",
        "no_query",
        "invalid_arguments",
        "not_in_channel",
    }
)

# Recoverable *before* classification, not a class of its own. The dispatch path
# handles it with `conversations.join` then one retry (task 8.3); usually
# `chat:write.public` prevents it arising at all. If it survives that recovery
# it is genuinely permanent, which is where the table above puts it.
SLACK_RECOVERABLE = frozenset({"not_in_channel"})


@dataclass
class ProviderError(Exception):
    """A provider failure, carrying the provider's own vocabulary.

    ``code`` is the identifier the provider actually returned — Slack's
    ``error`` string, Google's ``error`` or ``errors[0].reason``. ``detail`` is
    the full payload: the spec forbids reducing it to a generic message, and it
    is the operator's only evidence. Truncation happens at persist time
    (``core.jobs.queue``), not here.
    """

    provider: str
    code: str
    detail: str = ""
    status: int | None = None
    # Seconds. When a provider tells us how long to wait, that always wins over
    # our computed backoff — ignoring it is how Slack rate-limits you harder.
    retry_after: float | None = None
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        return f"{self.provider}:{self.code} {self.detail}".strip()

    @classmethod
    def from_slack(
        cls,
        payload: dict[str, Any],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> ProviderError:
        retry_after = _retry_after(headers)
        return cls(
            provider="slack",
            code=str(payload.get("error", "")),
            detail=_dump(payload),
            status=status,
            retry_after=retry_after,
            payload=payload,
        )

    @classmethod
    def from_google(
        cls,
        payload: dict[str, Any],
        *,
        status: int,
        headers: dict[str, str] | None = None,
    ) -> ProviderError:
        """Read the code from wherever Google put it this time.

        The token endpoint returns a flat ``{"error": "invalid_grant"}``; the
        Gmail API returns ``{"error": {"errors": [{"reason": "..."}]}}``. Both
        shapes are live, so both are read.
        """
        code = ""
        err = payload.get("error")
        if isinstance(err, str):
            code = err
        elif isinstance(err, dict):
            errors = err.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                code = str(errors[0].get("reason", ""))
            if not code:
                code = str(err.get("status", ""))
        return cls(
            provider="google",
            code=code,
            detail=_dump(payload),
            status=status,
            retry_after=_retry_after(headers),
            payload=payload,
        )


class NeedsReconnect(Exception):
    """The grant is gone and only the account owner can restore it.

    Raised by the token helper once a refresh has come back ``invalid_grant``,
    so the escalation happens **once**, at the bottom of the ladder, rather than
    being re-derived by every caller from a raw 401 (R24).

    It lives here rather than in ``core.connections`` for one structural reason:
    :func:`classify` is the single classification boundary, and an exception it
    has to special-case would otherwise force ``errors`` to import
    ``connections`` — which already imports ``errors``.

    Deliberately **not** a ``ProviderError``. A ``ProviderError`` carries a
    provider's own vocabulary for something that just happened; this carries our
    conclusion about a connection, which is a different kind of claim.
    """

    def __init__(self, connection_id: int, detail: str) -> None:
        super().__init__(detail)
        self.connection_id = connection_id
        self.detail = detail


def classify(exc: BaseException, provider: str | None = None) -> ErrorClass:
    """Map any failure onto the four-member taxonomy.

    ``provider`` is only a hint used when the exception does not already name
    one; a ``ProviderError`` always classifies against its own provider.
    """
    if isinstance(exc, NeedsReconnect):
        return ErrorClass.NEEDS_RECONNECT

    # 🔴 A source with no usable connection is `needs_reconnect`, never
    # `permanent`. It reached this function as a bare RuntimeError and therefore
    # fell through to the catch-all below, which meant a **brand-new user's very
    # first search** reported Gmail and Slack as `provider_unavailable`,
    # classification `permanent` — "this provider is broken and will stay
    # broken", about a provider they simply had not connected yet.
    #
    # That is the R16 failure in its purest form: not "we looked and found
    # nothing" but "we could not look", reported as neither. And `permanent`
    # carries no action, when the action is one click away. `needs_reconnect` is
    # the class that renders as an action rather than an error, which is exactly
    # what this is.
    if isinstance(exc, TokenUnavailable):
        return ErrorClass.NEEDS_RECONNECT

    if isinstance(exc, ProviderError):
        return _classify_provider_error(exc)

    # Transport-level failures, where there is no provider vocabulary to read.
    # A connection reset is transient regardless of who was on the other end.
    if isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, asyncio.TimeoutError | TimeoutError | ConnectionError | OSError):
        return ErrorClass.TRANSIENT

    if isinstance(exc, httpx.HTTPStatusError):
        return _classify_status(exc.response.status_code)

    # Anything else is our own bug reaching the job boundary. `permanent` on
    # purpose: retrying a TypeError six times buries the traceback under five
    # identical ones and delays the only useful signal, which is the error text.
    return ErrorClass.PERMANENT


def is_recoverable(exc: BaseException) -> bool:
    """True for errors a caller can fix in-line and retry once (Slack `not_in_channel`).

    Deliberately separate from :func:`classify`: recovery is an action the
    dispatch layer takes *before* classification, not a fifth error class. Once
    recovery has been attempted and failed, the classification stands.
    """
    return (
        isinstance(exc, ProviderError)
        and exc.provider == "slack"
        and exc.code in SLACK_RECOVERABLE
    )


def _classify_provider_error(exc: ProviderError) -> ErrorClass:
    if exc.provider == "slack":
        if exc.code in SLACK_TRANSIENT:
            return ErrorClass.TRANSIENT
        if exc.code in SLACK_NEEDS_RECONNECT:
            return ErrorClass.NEEDS_RECONNECT
        if exc.code in SLACK_PERMANENT:
            return ErrorClass.PERMANENT
        # An unrecognised Slack code with no status to fall back on. Permanent,
        # so it surfaces with its full payload rather than being retried six
        # times into the same wall.
        return _fallback(exc, default=ErrorClass.PERMANENT)

    if exc.provider == "google":
        if exc.code in GOOGLE_NEEDS_RECONNECT:
            return ErrorClass.NEEDS_RECONNECT
        if exc.code in GOOGLE_CONFIG:
            return ErrorClass.CONFIG
        if exc.code in GOOGLE_TRANSIENT:
            return ErrorClass.TRANSIENT
        if exc.code in GOOGLE_PERMANENT:
            return ErrorClass.PERMANENT
        return _fallback(exc, default=ErrorClass.PERMANENT)

    return _fallback(exc, default=ErrorClass.PERMANENT)


def _fallback(exc: ProviderError, *, default: ErrorClass) -> ErrorClass:
    """Last resort for a code we do not recognise.

    Only reached when the provider gave us no usable identifier — this is the
    narrow case the "never infer from status alone" rule leaves room for, since
    a 503 with an unreadable body is still a 503.
    """
    if exc.status is not None and exc.status >= 500:
        return ErrorClass.TRANSIENT
    if exc.status == 429:
        return ErrorClass.TRANSIENT
    return default


def _classify_status(status: int) -> ErrorClass:
    if status == 429 or status >= 500:
        return ErrorClass.TRANSIENT
    return ErrorClass.PERMANENT


def _retry_after(headers: dict[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # Retry-After may be an HTTP-date. We do not parse it: a bad parse
        # producing a wrong delay is worse than falling back to our own jitter,
        # which is already bounded and correct.
        return None


def _dump(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=True, sort_keys=True)
