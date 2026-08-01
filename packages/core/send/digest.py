"""The confirmation digest (openspec task 5.4, design D5).

It does double duty, and that is the design rather than a convenience:

- **Confirmation proof.** A caller must echo a digest over content it has
  actually seen. An agent cannot confirm a body it never read, and a draft
  edited after the confirm screen rendered no longer matches.
- **Idempotency fingerprint.** The same key presented with different content is
  a ``422`` rather than a silently replayed wrong answer.

🔴 **Not the body alone.** Hashing only the body lets a caller change the
*recipient* and still pass the check, and sending the right words to the wrong
person is the worst failure this gate exists to prevent (risks.md R6).
"""

from __future__ import annotations

import hashlib

from core.enums import ProviderKind


def confirmation_digest(
    *,
    channel: ProviderKind | str,
    recipient: str,
    recipient_display: str,
    subject: str | None,
    body: str,
) -> str:
    """``sha256`` over channel ‖ recipient ‖ resolved recipient ‖ subject ‖ body.

    Design D5 specifies four parts — channel, resolved recipient, subject, body.
    The raw ``recipient`` is included as a fifth, deliberately: the display form
    is *derived* from it, so two different destinations could in principle
    resolve to the same display string, and the digest exists precisely to make
    a changed destination detectable. Adding it is invisible to callers (the
    server hands them the digest) and strictly stronger.

    **Length-prefixed, not delimiter-joined.** With a plain separator a body
    ending in the separator could be rearranged into a different tuple with an
    identical hash. The whole value of this digest is that a different tuple
    hashes differently, so an ambiguous encoding would quietly undermine it.
    """
    channel_value = channel.value if isinstance(channel, ProviderKind) else channel
    parts = (channel_value, recipient, recipient_display, subject or "", body)
    payload = b"".join(
        f"{len(raw)}:".encode("ascii") + raw
        for raw in (part.encode("utf-8") for part in parts)
    )
    return hashlib.sha256(payload).hexdigest()
