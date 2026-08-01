"""The error catalog (openspec task 9.11).

``api-design.md`` publishes a table of error codes, each with an HTTP status and
a ``classification`` a client uses to decide whether retrying is meaningful. That
table is a promise, and until this module existed it was a promise kept by hand
in a dozen ``raise`` sites.

Two properties, and the second is the one worth having:

**One code, one status, one classification.** Declared once, here. A code that
means 422 in one route and 409 in another is a code a client cannot branch on,
and we had exactly that: ``confirmation_required`` was raised at 422 by the send
gate and at 409 when refusing to retry an in-doubt send. The second is now
``resolution_required``, which is what it always meant.

**Every code is proven to have crossed the wire.** ``test_error_catalog.py``
drives a real request for each entry and asserts the response carries it.
🔴 This is the phase-3 lesson applied to a surface built for exactly this
mistake: a *documented* error code is not one that has ever been returned, and
the three defects phase 3 shipped green were each a shape asserted in place of a
value. An entry here that cannot be reached fails the suite.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorSpec:
    """One row of ``api-design.md``'s error table."""

    code: str
    status: int
    classification: str
    #: What a client should do about it. Rendered into the README's error table,
    #: so the documentation cannot drift from the declaration.
    client_action: str
    #: How the conformance test reaches it. ``None`` means the code is carried
    #: *inside* a payload (a per-source ``error`` block, a send's
    #: ``last_error_class``) rather than as a top-level refusal, so there is no
    #: request that returns it as an envelope — stated explicitly rather than
    #: left as an untested row.
    reachable_as_envelope: bool = True


#: The published catalog. Ordered as api-design.md orders it, then the OAuth
#: callback codes, which the table did not carry and the callback has always
#: raised.
CATALOG: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        "unauthorized",
        401,
        "permanent",
        "Identical for wrong, revoked, expired and unknown keys — never disclose which.",
    ),
    ErrorSpec(
        "not_found",
        404,
        "permanent",
        "Also returned for another user's resource, so existence is not disclosed.",
    ),
    ErrorSpec(
        "confirmation_required",
        422,
        "permanent",
        "Create a draft, read it back, and send its confirm_sha256.",
    ),
    ErrorSpec(
        "body_changed_since_confirmation",
        422,
        "permanent",
        "Re-read the draft and confirm the content it holds now.",
    ),
    ErrorSpec(
        "idempotency_key_body_mismatch",
        422,
        "permanent",
        "Caller bug: this key was already used for different content.",
    ),
    ErrorSpec(
        "resolution_required",
        409,
        "permanent",
        (
            "An in-doubt send needs a decision, not a repeat. "
            "POST /v1/sends/{id}/resolve with marked_delivered or forced_resend."
        ),
    ),
    ErrorSpec(
        "connection_needs_reconnect",
        409,
        "needs_reconnect",
        "A grant existed and was revoked. Send the user to action_url to re-grant.",
    ),
    ErrorSpec(
        "connection_not_connected",
        409,
        "needs_reconnect",
        (
            "This provider was never connected. Send the user to action_url to "
            "connect it. Distinct from the code above because offering to "
            "*re*connect an account someone never linked reads as though we lost "
            "something of theirs."
        ),
        # Only ever carried inside a search snapshot's per-source `error` block:
        # the send gate refuses a missing connection with
        # `connection_needs_reconnect` before a search is involved.
        reachable_as_envelope=False,
    ),
    ErrorSpec(
        "recipient_invalid",
        422,
        "permanent",
        "Do not retry — the recipient cannot receive this.",
    ),
    ErrorSpec(
        "channel_not_found",
        422,
        "permanent",
        "Do not retry — the channel does not exist or is not reachable.",
        # Raised by the Slack provider inside a send attempt and surfaced on the
        # send row's error block; no request returns it as an envelope.
        reachable_as_envelope=False,
    ),
    ErrorSpec(
        "provider_rate_limited",
        503,
        "transient",
        "Auto-retried with backoff; Retry-After is honoured over our own jitter.",
        reachable_as_envelope=False,
    ),
    ErrorSpec(
        "provider_unavailable",
        503,
        "transient",
        "Auto-retried with backoff.",
        # Returned as an envelope nowhere, but carried on a failed source's
        # `error.code` in the search snapshot, which is where a client reads it.
        reachable_as_envelope=False,
    ),
    ErrorSpec(
        "internal_config_error",
        500,
        "config",
        "Our bug. Alert the operator; never prompt the user to reconnect (R24).",
        reachable_as_envelope=False,
    ),
    ErrorSpec(
        "invalid_cursor",
        422,
        "permanent",
        "Drop the cursor and re-read the first page.",
    ),
    ErrorSpec(
        "provider_not_configured",
        503,
        "config",
        (
            "Our bug, not the user's: no OAuth client is configured for this "
            "provider. `config`, never `needs_reconnect` — telling someone to "
            "reconnect sends them round in circles repairing a grant that was "
            "never broken (R24)."
        ),
    ),
    ErrorSpec(
        "authorization_denied",
        400,
        "permanent",
        "The user declined at the provider's consent screen. Offer the connect action again.",
    ),
    ErrorSpec(
        "authorization_incomplete",
        400,
        "permanent",
        "The callback carried no authorization code. Restart the connect flow.",
    ),
    ErrorSpec(
        "state_invalid",
        400,
        "permanent",
        "The signed state was missing, tampered with, or expired. Restart the connect flow.",
    ),
    ErrorSpec(
        "reconnect_account_mismatch",
        409,
        "permanent",
        (
            "The re-grant authorized a different provider account. Never silently "
            "rebound: drafts and sends reference connection.id, so repointing it "
            "would rewrite what all of them mean."
        ),
    ),
)

BY_CODE: dict[str, ErrorSpec] = {spec.code: spec for spec in CATALOG}


def spec(code: str) -> ErrorSpec:
    """Look a code up, failing loudly on one that was never declared.

    Called from the ``ApiError`` constructor, so an undeclared code cannot reach
    a client: the catalog and the raise sites are the same fact, checked at the
    moment of raising rather than by review.
    """
    try:
        return BY_CODE[code]
    except KeyError:  # pragma: no cover - a programming error, not a runtime path
        raise KeyError(
            f"{code!r} is not in the published error catalog (apps/api/catalog.py). "
            "Add it there — with its status, classification and client action — "
            "so it lands in the README and the conformance test."
        ) from None
