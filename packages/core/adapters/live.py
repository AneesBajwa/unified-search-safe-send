"""Where the real adapters are registered (tasks 7.4-7.5, 7.7, design D6).

This module and ``fakes`` are the **only** two in the adapter layer that name a
source. That is deliberate and it is checked: a test greps ``orchestrator.py``
and ``merge.py`` for ``gmail``/``slack``/``web`` and fails on any hit, so adding
a fourth source stays what it claims to be — one adapter file, plus one call
below.

🔴 **A source runs live only when it is actually configured.** With no
`GOOGLE_CLIENT_ID` there is no OAuth client, so there can be no connection, so
there is no live Gmail search — and registering the live adapter anyway would
produce a source that fails every run with a token error and reports ``mode:
live`` while doing it. That is the exact dishonesty the status chip exists to
prevent, aimed at ourselves.

So the rule the web adapter already followed — *fixtures until a key is
configured, and say so* — is applied to all three sources uniformly. It has two
consequences worth stating plainly:

- ``make smoke`` and the test suite stay green on a machine with no credentials,
  because they get fakes badged ``mock`` rather than live adapters that cannot
  work. Neither is being fooled: the mode is on every source chip and in every
  API payload.
- The moment credentials land in the environment, the same code path is live
  with no further change. That is the substitution this phase is for.

Three registration arguments change meaning against phase 2 for a source that
*is* configured, and all three carry weight:

- **``mode=LIVE``**, so the chip stops saying `mock`. ``degraded`` is a real
  third value a run can report for itself (``core.adapters.slack``).
- **``requires_connection=True``**, so ``plan_search`` attaches the connection
  whose ``provider`` matches the source name. The web source stays False: the
  zero-connections case has to keep working.
- **The factory is called per run**, never once. Nothing here closes over a
  client, because a client holds per-request state and two concurrent searches
  would share it.
"""

from __future__ import annotations

import logging

from core.adapters import fakes
from core.adapters.gmail import GmailAdapter
from core.adapters.registry import register
from core.adapters.slack import SlackAdapter
from core.adapters.websearch import WebSearchAdapter, has_api_key
from core.config import get_settings
from core.enums import SourceMode

logger = logging.getLogger("core.adapters.live")


def configured_sources() -> dict[str, bool]:
    """Which of the three have real credentials behind them right now.

    Read from settings rather than cached at import so a `.env` filled in
    between runs takes effect on the next process start without a code change,
    and so this is answerable by a health endpoint or a status page.
    """
    settings = get_settings()
    return {
        "gmail": bool(
            settings.google_client_id and settings.google_client_secret.get_secret_value()
        ),
        "slack": bool(
            settings.slack_client_id and settings.slack_client_secret.get_secret_value()
        ),
        "web": has_api_key(),
    }


def register_defaults() -> None:
    """Populate the registry. Idempotent — registration overwrites by name."""
    configured = configured_sources()

    if configured["gmail"]:
        register(
            "gmail",
            GmailAdapter,
            participates_by_default=True,
            mode=SourceMode.LIVE,
            requires_connection=True,
        )
    if configured["slack"]:
        register(
            "slack",
            SlackAdapter,
            participates_by_default=True,
            mode=SourceMode.LIVE,
            requires_connection=True,
        )

    register(
        "web",
        WebSearchAdapter,
        participates_by_default=True,
        mode=SourceMode.LIVE if configured["web"] else SourceMode.MOCK,
        # 🔴 False. A user with no connected accounts still gets a working
        # search, and the seed set's `board deck` query exists to show it.
        requires_connection=False,
    )

    unconfigured = tuple(
        source for source in ("gmail", "slack") if not configured[source]
    )
    if unconfigured:
        # Registered *after* the live ones so the choice is unambiguous, and
        # logged so "why is this source mocked?" is answerable from the logs
        # rather than by reading this file.
        logger.info(
            "no OAuth client configured for %s; those sources serve fixtures and "
            "report mode=mock",
            ", ".join(unconfigured),
        )
        fakes.register_fake_sources(only=unconfigured)

    fakes.register_fault_adapters()
