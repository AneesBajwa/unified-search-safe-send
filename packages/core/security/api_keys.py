"""API-key minting and verification (the format half of task 9.1).

``sk_live_{key_id}_{secret}{crc32}``

Three properties, each deliberate:

**The checksum is validated before any database hit.** A malformed or
mistyped key is rejected on arithmetic alone, so garbage traffic never reaches
the connection pool.

**Lookup is by ``key_id``, which is not secret.** It is indexed, and it is safe
to log — so "which key was used" is answerable without ever handling the secret.

**The hash is an unsalted SHA-256, and bcrypt would be the wrong tool.** The
secret is 256 bits of CSPRNG output: there is no dictionary to slow down, so a
work factor buys nothing while costing ~100 ms of billed CPU per request (a free
DoS lever), and bcrypt silently truncates at 72 bytes. Salting buys nothing
either, because the input is already unique and high-entropy — and a per-row
salt would make the lookup impossible to index. Comparison uses
``hmac.compare_digest`` so the check is constant-time.

The authenticated dependency, revocation semantics and the deliberately
indistinguishable auth-failure error live in task group 9.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from dataclasses import dataclass

PREFIX = "sk_live_"
KEY_ID_BYTES = 9  # 12 base64url chars
SECRET_BYTES = 32  # 43 base64url chars — the actual entropy
_CHECKSUM_CHARS = 8

_KEY_RE = re.compile(
    rf"^{re.escape(PREFIX)}([A-Za-z0-9_-]{{12}})_([A-Za-z0-9_-]{{43}})([0-9a-f]{{8}})$"
)


@dataclass(frozen=True)
class MintedKey:
    """The only moment the plaintext exists. It is never stored and never re-derivable."""

    plaintext: str
    key_id: str
    key_hash: str
    prefix_display: str


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint() -> MintedKey:
    key_id = _b64(os.urandom(KEY_ID_BYTES))
    secret = _b64(os.urandom(SECRET_BYTES))
    body = f"{PREFIX}{key_id}_{secret}"
    plaintext = f"{body}{_checksum(body)}"
    return MintedKey(
        plaintext=plaintext,
        key_id=key_id,
        key_hash=hash_secret(plaintext),
        # Enough to recognise a key in a list, never enough to use one.
        prefix_display=f"{PREFIX}{key_id}…{plaintext[-4:]}",
    )


def parse(candidate: str) -> str | None:
    """Return the ``key_id`` if the structure and checksum hold, else ``None``.

    Pure arithmetic — no I/O. This is the cheap gate in front of the database.
    """
    match = _KEY_RE.match(candidate)
    if match is None:
        return None
    key_id, secret, checksum = match.groups()
    body = f"{PREFIX}{key_id}_{secret}"
    if not hmac.compare_digest(checksum, _checksum(body)):
        return None
    return key_id


def hash_secret(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def verify(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(plaintext), stored_hash)


def _checksum(body: str) -> str:
    return f"{binascii.crc32(body.encode('ascii')) & 0xFFFFFFFF:0{_CHECKSUM_CHARS}x}"
