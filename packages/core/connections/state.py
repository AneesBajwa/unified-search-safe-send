"""Signed OAuth `state` — CSRF protection for the callback (task 6.5).

The callback is an unauthenticated GET that a provider redirects a browser to,
so anything it is told has to carry its own proof. ``state`` is that proof: an
HMAC over the fields we need back, so a forged callback cannot bind an
attacker's grant to somebody else's account.

Three fields, each doing a job:

- ``user_id``  — who is connecting. Read from the *signature*, never from a
  query parameter, or the callback becomes "connect this account to any user id
  you like".
- ``nonce``    — makes each authorize URL unique, so a captured state cannot be
  replayed into a second connection.
- ``issued_at``— bounds the window. A state found in browser history a week
  later is not a valid authorization.

The signing key is derived from the token keyring rather than being a separate
secret. That is deliberate: it means the fail-closed property already proven for
tokens covers this too, and there is one fewer secret to provision, rotate and
forget. Domain-separated so the derived key can never be confused with the key
that encrypts tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from core.security import crypto

#: Long enough to type a password and clear a 2FA prompt, short enough that a
#: state sitting in browser history is dead.
STATE_TTL_SECONDS = 900

_DOMAIN = b"unified-search/oauth-state/v1"


class StateInvalid(ValueError):
    """Bad signature, malformed payload, or expired.

    One class on purpose: the caller's response is identical in every case —
    refuse the callback — and distinguishing them for the *caller* would also
    distinguish them for someone probing.
    """


@dataclass(frozen=True)
class OAuthState:
    user_id: int
    provider: str
    nonce: str
    issued_at: int
    #: Where to send the browser once the connection lands. Signed with the
    #: rest so it cannot be rewritten into an open redirect.
    return_to: str = "/connections"
    #: True when this authorization is repairing a broken grant. Carried through
    #: so the callback knows a missing refresh token here is a real problem
    #: rather than Google's ordinary omission (R21).
    reconnect: bool = False


def sign(state: OAuthState) -> str:
    payload = json.dumps(
        {
            "user_id": state.user_id,
            "provider": state.provider,
            "nonce": state.nonce,
            "issued_at": state.issued_at,
            "return_to": state.return_to,
            "reconnect": state.reconnect,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    body = _b64(payload)
    return f"{body}.{_b64(_mac(body.encode('ascii')))}"


def verify(raw: str, *, now: datetime | None = None) -> OAuthState:
    body, _, signature = raw.partition(".")
    if not body or not signature:
        raise StateInvalid("state is not in the expected form")

    # `compare_digest` rather than `==`: the comparison happens on data an
    # attacker supplies and can time.
    if not hmac.compare_digest(signature, _b64(_mac(body.encode("ascii")))):
        raise StateInvalid("state signature does not verify")

    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        raise StateInvalid("state payload is not readable") from None

    issued_at = int(payload.get("issued_at", 0))
    current = int((now or datetime.now(UTC)).timestamp())
    if current - issued_at > STATE_TTL_SECONDS:
        raise StateInvalid("this authorization took too long; start again")

    return OAuthState(
        user_id=int(payload["user_id"]),
        provider=str(payload["provider"]),
        nonce=str(payload["nonce"]),
        issued_at=issued_at,
        return_to=str(payload.get("return_to", "/connections")),
        reconnect=bool(payload.get("reconnect", False)),
    )


def new_state(
    *, user_id: int, provider: str, return_to: str = "/connections", reconnect: bool = False
) -> OAuthState:
    return OAuthState(
        user_id=user_id,
        provider=provider,
        nonce=os.urandom(16).hex(),
        issued_at=int(datetime.now(UTC).timestamp()),
        return_to=return_to,
        reconnect=reconnect,
    )


def _mac(body: bytes) -> bytes:
    ring = crypto.get_keyring()
    # HKDF-shaped derivation via one HMAC: the token key never signs anything
    # directly, so a flaw in one use cannot be levered into the other.
    key = hmac.new(ring.key_for(ring.current), _DOMAIN, hashlib.sha256).digest()
    return hmac.new(key, body, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
