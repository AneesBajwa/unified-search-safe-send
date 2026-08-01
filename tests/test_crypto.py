"""Token encryption (openspec task 6.1b, risks.md R9).

Seven properties, and the interesting ones are not "does it round-trip". They
are the two that convert a database leak from total compromise into nothing
happening, and the one that makes rotation a ``WHERE`` clause.

No database and no network: this is pure arithmetic over bytes.
"""

from __future__ import annotations

import base64
import os

import pytest
from core.security import crypto


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


def _ring(*versions: int) -> crypto.Keyring:
    return crypto.parse_keyring(",".join(f"v{v}:{_key()}" for v in versions))


AAD = crypto.aad_for(connection_id=7, provider="gmail", field="refresh")


def test_a_token_round_trips() -> None:
    ring = _ring(1)
    blob = crypto.encrypt("1//0-a-refresh-token", aad=AAD, keyring=ring)
    assert crypto.decrypt(blob, aad=AAD, keyring=ring) == "1//0-a-refresh-token"
    # The plaintext must not be sitting in the envelope in any recoverable form.
    assert b"refresh-token" not in blob


def test_the_key_version_is_readable_without_a_key() -> None:
    """Task 6.1d — this is what makes rotation a `WHERE key_version < n` rather
    than a trial decrypt of every row in the table."""
    ring = _ring(1, 4)
    blob = crypto.encrypt("token", aad=AAD, keyring=ring)
    # Note: no keyring argument. The version is plaintext header, by design.
    assert crypto.key_version_of(blob) == 4


def test_a_row_swap_is_rejected() -> None:
    """🔴 The AAD property, and the reason AAD exists at all.

    An attacker with ``UPDATE`` but no read access copies our ciphertext into a
    connection row **they** control, expecting the app to decrypt it and use it
    on their behalf — never having read the ciphertext themselves. Binding the
    ciphertext to the row identity is what makes that fail.
    """
    ring = _ring(1)
    blob = crypto.encrypt("1//0-victim-token", aad=AAD, keyring=ring)

    attacker_aad = crypto.aad_for(connection_id=8, provider="gmail", field="refresh")
    with pytest.raises(crypto.EnvelopeError):
        crypto.decrypt(blob, aad=attacker_aad, keyring=ring)


def test_a_field_swap_is_rejected() -> None:
    """The same binding, one level finer: a refresh token moved into the
    access-token column. An attacker can do this deliberately; a bad migration
    does it by accident, and both should fail the same way."""
    ring = _ring(1)
    blob = crypto.encrypt("1//0-refresh", aad=AAD, keyring=ring)
    as_access = crypto.aad_for(connection_id=7, provider="gmail", field="access")
    with pytest.raises(crypto.EnvelopeError):
        crypto.decrypt(blob, aad=as_access, keyring=ring)


def test_tampered_ciphertext_is_rejected() -> None:
    ring = _ring(1)
    blob = bytearray(crypto.encrypt("token", aad=AAD, keyring=ring))
    blob[-1] ^= 0x01  # one bit, in the tag
    with pytest.raises(crypto.EnvelopeError):
        crypto.decrypt(bytes(blob), aad=AAD, keyring=ring)


def test_an_old_key_still_decrypts_after_rotation() -> None:
    """Rotation is a background re-encrypt, not an outage: v1 rows stay readable
    while v2 takes over new writes."""
    old = _ring(1)
    legacy = crypto.encrypt("written-under-v1", aad=AAD, keyring=old)

    rotated = crypto.Keyring(keys={**old.keys, 2: os.urandom(32)}, current=2)
    assert crypto.decrypt(legacy, aad=AAD, keyring=rotated) == "written-under-v1"
    assert crypto.key_version_of(crypto.encrypt("new", aad=AAD, keyring=rotated)) == 2


def test_a_retired_key_raises_not_found() -> None:
    """Distinct from a tamper. A row we can no longer read is an operations
    problem — the re-encrypt did not finish before the key was dropped — and it
    must not be reported as an attack, or the response will be wrong."""
    old = _ring(1)
    blob = crypto.encrypt("written-under-v1", aad=AAD, keyring=old)
    without_v1 = crypto.Keyring(keys={2: os.urandom(32)}, current=2)
    with pytest.raises(crypto.KeyNotFound):
        crypto.decrypt(blob, aad=AAD, keyring=without_v1)


def test_nonces_are_random() -> None:
    """Never derived, never a counter. GCM's security collapses entirely on
    nonce reuse under the same key, so this is not a style preference."""
    ring = _ring(1)
    nonces = {
        crypto.encrypt("same plaintext", aad=AAD, keyring=ring)[
            len(crypto.MAGIC) + crypto.KEY_VERSION_BYTES : crypto.HEADER_BYTES
        ]
        for _ in range(64)
    }
    assert len(nonces) == 64


def test_a_foreign_blob_is_not_mistaken_for_ours() -> None:
    with pytest.raises(crypto.EnvelopeError):
        crypto.key_version_of(b"not an envelope at all")


def test_the_keyring_fails_closed_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 R9. No default key, no generate-if-missing, no plaintext fallback.

    An app that silently starts storing unencrypted tokens because a volume
    mount failed is worse than one that refuses to work.
    """
    from core.config import get_settings

    monkeypatch.setenv("TOKEN_KEYRING", "")
    monkeypatch.setenv("TOKEN_KEYRING_PATH", "/nonexistent/keyring")
    get_settings.cache_clear()
    crypto.reset_keyring_cache()
    try:
        with pytest.raises(crypto.KeyringUnavailable):
            crypto.get_keyring()
    finally:
        get_settings.cache_clear()
        crypto.reset_keyring_cache()


def test_a_short_key_is_refused() -> None:
    """AES-256 needs 32 bytes. A 16-byte key would silently give AES-128 in some
    libraries; here it is a startup error naming the length."""
    short = base64.b64encode(os.urandom(16)).decode("ascii")
    with pytest.raises(crypto.KeyringUnavailable, match="32"):
        crypto.parse_keyring(short)


def test_a_bare_key_means_version_one() -> None:
    """The form `.env.example` documents, so local dev is a one-liner."""
    ring = crypto.parse_keyring(_key())
    assert ring.current == 1
