"""Classification against captured provider payloads (openspec tasks 3.2, 3.2b).

Real response shapes, not invented ones. The whole value of a single
classification boundary is that it can be tested against what the providers
actually emit — and the two behaviours worth defending are that Slack is read
from its ``error`` string at HTTP 200, and that Google's 403 family splits
between transient and permanent on ``reason`` alone.
"""

from __future__ import annotations

import httpx
import pytest
from core.enums import ErrorClass
from core.errors import ProviderError, classify, is_recoverable

# ---------------------------------------------------------------------------
# Slack — captured `chat.postMessage` / `search.messages` error bodies.
# Every one of these arrives with HTTP 200 except `ratelimited`.
# ---------------------------------------------------------------------------

SLACK_CASES = [
    ("ratelimited", ErrorClass.TRANSIENT),
    ("request_timeout", ErrorClass.TRANSIENT),
    ("service_unavailable", ErrorClass.TRANSIENT),
    ("internal_error", ErrorClass.TRANSIENT),
    ("fatal_error", ErrorClass.TRANSIENT),
    ("token_revoked", ErrorClass.NEEDS_RECONNECT),
    ("token_expired", ErrorClass.NEEDS_RECONNECT),
    ("invalid_auth", ErrorClass.NEEDS_RECONNECT),
    ("not_authed", ErrorClass.NEEDS_RECONNECT),
    ("account_inactive", ErrorClass.NEEDS_RECONNECT),
    ("org_login_required", ErrorClass.NEEDS_RECONNECT),
    ("missing_scope", ErrorClass.PERMANENT),
    ("not_allowed_token_type", ErrorClass.PERMANENT),
    ("channel_not_found", ErrorClass.PERMANENT),
    ("is_archived", ErrorClass.PERMANENT),
    ("no_permission", ErrorClass.PERMANENT),
    ("restricted_action", ErrorClass.PERMANENT),
    ("no_text", ErrorClass.PERMANENT),
    ("no_query", ErrorClass.PERMANENT),
    ("invalid_arguments", ErrorClass.PERMANENT),
    ("not_in_channel", ErrorClass.PERMANENT),
]


@pytest.mark.parametrize(("code", "expected"), SLACK_CASES)
def test_slack_is_classified_from_the_error_string_at_http_200(
    code: str, expected: ErrorClass
) -> None:
    """The status is 200 for all of these. Only the `error` field distinguishes them.

    A classifier keyed off the status code would call every one of these a
    success — which is precisely the failure this test exists to prevent.
    """
    exc = ProviderError.from_slack({"ok": False, "error": code}, status=200)
    assert classify(exc) is expected


def test_slack_status_code_never_overrides_the_error_string() -> None:
    """A 200 that says `token_revoked` is a revoked grant, not a success.

    The mirror case matters just as much: Slack sends `ratelimited` at 429, and
    reading only the status would happen to be right there — which is how a
    status-based classifier passes a casual review and then misclassifies the
    other twenty codes above.
    """
    revoked = ProviderError.from_slack({"ok": False, "error": "token_revoked"}, status=200)
    assert classify(revoked) is ErrorClass.NEEDS_RECONNECT

    limited = ProviderError.from_slack(
        {"ok": False, "error": "ratelimited"}, status=429, headers={"Retry-After": "30"}
    )
    assert classify(limited) is ErrorClass.TRANSIENT
    assert limited.retry_after == 30.0


def test_slack_retry_after_is_read_from_the_header() -> None:
    exc = ProviderError.from_slack(
        {"ok": False, "error": "ratelimited"}, status=429, headers={"retry-after": "12"}
    )
    assert exc.retry_after == 12.0


def test_slack_http_date_retry_after_falls_back_rather_than_guessing() -> None:
    """An HTTP-date Retry-After yields None, and our own bounded jitter is used.

    Mis-parsing a date into a wrong delay is worse than not parsing it: the
    computed backoff is already correct and bounded.
    """
    exc = ProviderError.from_slack(
        {"ok": False, "error": "ratelimited"},
        status=429,
        headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    assert exc.retry_after is None


def test_not_in_channel_is_recoverable_but_still_classifies_permanent() -> None:
    """Recovery is an action the dispatch layer takes, not a fifth error class.

    `conversations.join` then one retry happens before classification. If the
    error survives that, it is genuinely permanent — so the two facts coexist
    rather than contradicting each other.
    """
    exc = ProviderError.from_slack({"ok": False, "error": "not_in_channel"})
    assert is_recoverable(exc)
    assert classify(exc) is ErrorClass.PERMANENT


def test_unknown_slack_code_is_not_retried_into_the_same_wall() -> None:
    exc = ProviderError.from_slack({"ok": False, "error": "some_new_error_slack_added"})
    assert classify(exc) is ErrorClass.PERMANENT


# ---------------------------------------------------------------------------
# Google — the token endpoint is flat, the Gmail API nests under error.errors.
# ---------------------------------------------------------------------------


def test_google_invalid_grant_is_the_revocation_signal() -> None:
    """The only reliable one Google emits, and it is never retried.

    It collapses eight distinct causes and there is no way to tell them apart,
    so the classifier does not try.
    """
    exc = ProviderError.from_google(
        {
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        },
        status=400,
    )
    assert classify(exc) is ErrorClass.NEEDS_RECONNECT


@pytest.mark.parametrize(
    "code",
    ["invalid_client", "deleted_client", "unauthorized_client", "invalid_scope", "invalid_request"],
)
def test_google_client_errors_are_config_not_reconnect(code: str) -> None:
    """Our bug, and the user must never be shown a reconnect prompt for it.

    Their grant is fine; reconnecting cannot fix a deleted OAuth client, so the
    prompt just sends them round in circles (risks.md R24).
    """
    exc = ProviderError.from_google({"error": code}, status=401)
    assert classify(exc) is ErrorClass.CONFIG
    assert classify(exc) is not ErrorClass.NEEDS_RECONNECT


def test_google_rate_limit_is_transient() -> None:
    exc = ProviderError.from_google(
        {
            "error": {
                "code": 403,
                "message": "User-rate limit exceeded.",
                "errors": [
                    {
                        "domain": "usageLimits",
                        "reason": "userRateLimitExceeded",
                        "message": "User-rate limit exceeded.",
                    }
                ],
            }
        },
        status=403,
    )
    assert classify(exc) is ErrorClass.TRANSIENT


def test_google_quota_exhausted_is_permanent_despite_sharing_403_with_rate_limits() -> None:
    """The headline case for "never classify on status alone".

    `dailyLimitExceeded` and `userRateLimitExceeded` are both 403 in the
    `usageLimits` domain. One should back off; the other will still be exhausted
    in five minutes.
    """
    exc = ProviderError.from_google(
        {
            "error": {
                "code": 403,
                "errors": [{"domain": "usageLimits", "reason": "dailyLimitExceeded"}],
            }
        },
        status=403,
    )
    assert classify(exc) is ErrorClass.PERMANENT


def test_google_503_backend_error_is_transient() -> None:
    exc = ProviderError.from_google(
        {"error": {"code": 503, "errors": [{"reason": "backendError"}]}}, status=503
    )
    assert classify(exc) is ErrorClass.TRANSIENT


def test_google_invalid_recipient_is_permanent() -> None:
    """No retry. The address will still be malformed on attempt six."""
    exc = ProviderError.from_google(
        {
            "error": {
                "code": 400,
                "message": "Invalid To header",
                "errors": [{"reason": "invalidArgument", "message": "Invalid To header"}],
            }
        },
        status=400,
    )
    assert classify(exc) is ErrorClass.PERMANENT


def test_google_401_is_not_yet_terminal() -> None:
    """A raw 401 means "refresh and try again", not "the grant is dead".

    Escalation to needs-reconnect happens only if the *refresh* returns
    `invalid_grant`. Treating the 401 itself as terminal disconnects healthy
    users constantly (risks.md R24).
    """
    exc = ProviderError.from_google(
        {"error": {"code": 401, "errors": [{"reason": "authError"}]}}, status=401
    )
    assert classify(exc) is ErrorClass.TRANSIENT
    assert classify(exc) is not ErrorClass.NEEDS_RECONNECT


def test_error_description_is_carried_but_never_branched_on() -> None:
    """Same `error`, different prose, same classification.

    Google has changed these strings before; a classifier that branches on them
    stops classifying without anything failing.
    """
    a = ProviderError.from_google(
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
        status=400,
    )
    b = ProviderError.from_google(
        {"error": "invalid_grant", "error_description": "Bad Request"}, status=400
    )
    assert classify(a) is classify(b) is ErrorClass.NEEDS_RECONNECT
    assert "expired or revoked" in a.detail  # kept as telemetry


# ---------------------------------------------------------------------------
# Transport and unknowns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("server disconnected"),
        TimeoutError(),
        ConnectionResetError(),
    ],
)
def test_transport_failures_are_transient(exc: BaseException) -> None:
    assert classify(exc) is ErrorClass.TRANSIENT


def test_our_own_bug_is_permanent_rather_than_retried_six_times() -> None:
    """A TypeError reaching the job boundary is a bug, and retrying buries it.

    Five more identical tracebacks delay the only useful signal, which is the
    first one.
    """
    assert classify(TypeError("cannot concatenate 'str' and 'int'")) is ErrorClass.PERMANENT
