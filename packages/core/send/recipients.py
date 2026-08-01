"""Resolving a destination into something a human recognises (task 5.2).

Two rules, and they point in opposite directions on purpose:

- **An email address is echoed verbatim.** Never a display name, never a
  friendly alias. "Dana Chen" hides which of Dana's three addresses is about to
  receive this; ``dana@acme.test`` does not.
- **A channel id is resolved to its name.** ``C024BE91L`` is not something a
  person can check, so a confirmation showing it is a confirmation nobody reads.

🔴 **Resolution happens at draft-write time and never on the send path.**
``resolve_recipient`` is called only from ``create_draft`` and ``update_draft``;
the result is persisted to ``drafts.recipient_display`` and the confirmation
digest is computed **from the row**. That ordering is load-bearing: if a
provider call crept onto the send path, the digest would become uncomputable
during a Slack outage — which means the gate could not be opened for a send
whose content never changed. **A provider being down must not make a confirmed
message unsendable.**

It is async and provider-backed now, and still a pure function of
``(channel, recipient)``: the same pair always produces the same display, and
nothing about a *send* can change it.
"""

from __future__ import annotations

import logging

from core.enums import ProviderKind

logger = logging.getLogger("core.send.recipients")

#: The fixture workspace. Consulted **only** when there is no live connection to
#: ask — which is the same situation in which the send provider is itself a
#: fake, so the whole world is consistent rather than half-invented.
FAKE_CHANNEL_NAMES: dict[str, str] = {
    "C024BE91L": "#acme-renewal",
    "C7X2QF3AA": "#sales",
    "C5N1MK8QQ": "#eng",
}


async def resolve_recipient(
    channel: ProviderKind, recipient: str, *, connection_id: int | None = None
) -> str:
    """The display a human confirms against.

    Resolution order for a channel: what the user already typed, then the
    provider, then the fixture map, then the id itself. Never a plausible-
    looking invention — a confirmation showing a name we are not sure of is
    worse than one that admits it has an id.
    """
    if channel is ProviderKind.GMAIL:
        return recipient
    if recipient.startswith("#"):
        # Already a name. Asking the provider to confirm a name the user typed
        # would be a round trip that can only make the answer worse.
        return recipient

    if connection_id is not None:
        resolved = await _lookup(recipient, connection_id)
        if resolved:
            return resolved

    known = FAKE_CHANNEL_NAMES.get(recipient)
    if known is not None:
        return known
    return f"#{recipient}"


async def _lookup(recipient: str, connection_id: int) -> str | None:
    """``conversations.info`` on the bot token.

    Imported inside the function so this module stays importable without the
    provider stack — the digest helpers next door are pure and several tests
    exercise them with no database and no network.

    Every failure is swallowed into ``None``. A name we could not fetch is a
    fallback display, not a failed draft: refusing to create a draft because
    Slack was briefly unavailable would be the outage-blocks-the-gate failure
    this module is arranged to avoid, moved one step earlier.
    """
    from core.connections import tokens
    from core.providers.slack import SlackClient

    try:
        client = SlackClient(get_token=tokens.token_getter(connection_id))
        return await client.channel_name(recipient)
    except Exception as exc:  # noqa: BLE001 - a display name is never worth a failed draft
        logger.info("could not resolve %s via the provider: %s", recipient, exc)
        return None


def warning_for(channel: ProviderKind, recipient_display: str, connection_display: str) -> str:
    """The one sentence the confirm dialog leads with.

    Phrased as what is about to happen, in the present tense, naming the
    destination — not "are you sure?", which asks the customer to supply the
    detail they should be checking.
    """
    if channel is ProviderKind.GMAIL:
        return f"This will email {recipient_display} from {connection_display}."
    return f"This will post to {recipient_display} in {connection_display}."
