"""Token encryption: AES-256-GCM in a versioned envelope (tasks 6.1-6.1d, R9).

    magic ‖ key_version ‖ nonce ‖ ciphertext‖tag
     4 B       2 B         12 B        …

Four properties, each of which is load-bearing rather than decorative.

🔴 **AAD binds every ciphertext to its row**: ``conn:{id}:{provider}:{field}``.
Without it an attacker with ``UPDATE`` but no read access could copy our token
blob into a connection row they control and have the app decrypt and use it on
their behalf — never having read the ciphertext at all. Including the *field*
name additionally stops a refresh token being swapped into the access-token
column, which is the same bug a bad migration introduces by accident.

🔴 **``key_version`` is readable without any key.** Rotation therefore selects
rows to re-encrypt with a plain ``WHERE key_version < :current`` rather than
trial-decrypting the table.

🔴 **Fail closed.** No default key, no generate-if-missing, no plaintext
fallback. An app that silently starts storing unencrypted tokens because a
volume mount failed is worse than one that refuses to boot — and for restricted
Gmail scopes, encryption at rest is a *compliance* requirement rather than a
design preference (R9).

Nonces are ``os.urandom(12)``, never derived and never counted. At a few
thousand tokens we sit ~6 orders of magnitude inside GCM's 2³² birthday bound,
so random nonces are correct here.

**The dev key is never the production key.** A developer restoring a production
dump gets :class:`KeyNotFound` on every row. That is the protection working, not
a bug to route around.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    # Type-only, so the runtime import stays inside `_settings` — see there.
    from core.config import Settings

#: Four bytes so a blob that is not ours is rejected as malformed rather than
#: as a decryption failure — the two want very different operator responses.
MAGIC = b"USK1"
KEY_VERSION_BYTES = 2
NONCE_BYTES = 12
HEADER_BYTES = len(MAGIC) + KEY_VERSION_BYTES + NONCE_BYTES
KEY_BYTES = 32

#: Where Cloud Run mounts the Secret Manager volume. A *file*, not an env var:
#: env vars resolve at instance start (so rotation forces a redeploy) and leak
#: through ``/proc/self/environ``, debug endpoints and subprocess environments.
DEFAULT_KEYRING_PATH = "/secrets/keyring"


class KeyringUnavailable(RuntimeError):
    """No key material at all. Raised at the first token operation, not at
    import, so a process with no connections still starts and serves ``/health``
    — but nothing that *touches* a credential can proceed."""


class KeyNotFound(LookupError):
    """The envelope names a key version this keyring does not carry.

    Retired-key decrypt, or a production dump opened with a dev keyring. Never
    silently downgraded to "return the ciphertext" or "try the other keys".
    """


class EnvelopeError(ValueError):
    """Malformed envelope, wrong AAD, or a tampered tag.

    Deliberately one class: distinguishing "this was for another row" from "this
    was modified" hands an attacker an oracle, and the operator response —
    refuse, log, do not use — is the same either way.
    """


@dataclass(frozen=True)
class Keyring:
    """Version -> 32-byte key. ``current`` is what new writes use.

    Several versions coexist so rotation is a background re-encrypt rather than
    an outage: the old key still decrypts while the new one takes over writes.
    """

    keys: dict[int, bytes]
    current: int

    def key_for(self, version: int) -> bytes:
        try:
            return self.keys[version]
        except KeyError:
            raise KeyNotFound(
                f"no key with version {version} in this keyring; "
                f"available: {sorted(self.keys) or '(none)'}"
            ) from None


def parse_keyring(raw: str) -> Keyring:
    """Read ``v1:<b64>,v2:<b64>`` — or a bare base64 key, which means version 1.

    The bare form keeps ``.env.example``'s one-liner honest for local dev while
    the versioned form is what a rotation actually needs. Highest version wins
    as ``current``, so promoting a new key is appending to the string.
    """
    entries = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    if not entries:
        raise KeyringUnavailable("TOKEN_KEYRING is empty")

    keys: dict[int, bytes] = {}
    for entry in entries:
        version, _, material = entry.partition(":")
        if not material:
            version, material = "1", entry
        try:
            parsed_version = int(version.lstrip("vV"))
        except ValueError:
            raise KeyringUnavailable(
                f"keyring entry {entry[:8]!r}… has a non-numeric version"
            ) from None
        try:
            key = base64.b64decode(material, validate=True)
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            raise KeyringUnavailable(
                f"keyring version {parsed_version} is not valid base64"
            ) from None
        if len(key) != KEY_BYTES:
            raise KeyringUnavailable(
                f"keyring version {parsed_version} is {len(key)} bytes; "
                f"AES-256-GCM needs exactly {KEY_BYTES}"
            )
        keys[parsed_version] = key

    return Keyring(keys=keys, current=max(keys))


@lru_cache(maxsize=1)
def get_keyring() -> Keyring:
    """The volume mount first, then the env var (task 6.1c).

    Order matters: on Cloud Run the mount is authoritative and rotating it must
    not require a redeploy. The env fallback exists for local dev and tests, and
    is the *only* reason a developer machine works without a mounted secret.
    """
    settings = _settings()
    path = Path(settings.token_keyring_path or DEFAULT_KEYRING_PATH)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if not raw:
        raw = settings.token_keyring.get_secret_value().strip()
    if not raw:
        raise KeyringUnavailable(
            "no token-encryption key: mount one at "
            f"{path} or set TOKEN_KEYRING. There is deliberately no plaintext "
            "fallback and no generate-if-missing path (risks.md R9)"
        )
    return parse_keyring(raw)


def reset_keyring_cache() -> None:
    """Tests and rotation drills only."""
    get_keyring.cache_clear()


def aad_for(*, connection_id: int, provider: str, field: str) -> bytes:
    """The row identity a ciphertext is bound to.

    Any change to this string is a breaking change to every stored token — it is
    authenticated data, so a mismatch is a decrypt failure rather than a silent
    difference. Kept as one function so there is a single place to be careful.
    """
    return f"conn:{connection_id}:{provider}:{field}".encode()


def encrypt(plaintext: str, *, aad: bytes, keyring: Keyring | None = None) -> bytes:
    ring = keyring or get_keyring()
    key = ring.key_for(ring.current)
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return MAGIC + ring.current.to_bytes(KEY_VERSION_BYTES, "big") + nonce + ct


def decrypt(blob: bytes, *, aad: bytes, keyring: Keyring | None = None) -> str:
    ring = keyring or get_keyring()
    version = key_version_of(blob)
    key = ring.key_for(version)
    nonce = blob[len(MAGIC) + KEY_VERSION_BYTES : HEADER_BYTES]
    try:
        plaintext = AESGCM(key).decrypt(nonce, blob[HEADER_BYTES:], aad)
    except InvalidTag:
        raise EnvelopeError(
            "token failed authentication: it was encrypted for a different row "
            "or field, or it has been modified"
        ) from None
    return plaintext.decode("utf-8")


def key_version_of(blob: bytes) -> int:
    """Readable with no key at all — which is what makes rotation a ``WHERE``
    clause rather than a trial decrypt of every row (task 6.1d)."""
    if len(blob) < HEADER_BYTES or not blob.startswith(MAGIC):
        raise EnvelopeError("not a token envelope: bad magic or truncated header")
    return int.from_bytes(blob[len(MAGIC) : len(MAGIC) + KEY_VERSION_BYTES], "big")


def _settings() -> Settings:
    # Imported lazily so `core.config` is not pulled in at module import time,
    # which keeps this module usable from the rotation script with a keyring
    # passed in explicitly.
    from core.config import get_settings

    return get_settings()
