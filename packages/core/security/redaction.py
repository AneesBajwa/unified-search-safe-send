"""Keep credentials out of the logs (tasks 6.2-6.2c).

🔴 **The filter attaches to handlers, not loggers.** This is the whole point of
the module and it is genuinely counter-intuitive: a ``Filter`` installed on a
logger runs only for records logged *directly* on that logger. Records that
arrive by **propagation from a child logger never see it**. So the obvious
``logging.getLogger().addFilter(...)`` silently does nothing for
``httpx``/``httpcore``/``core.send`` — which is precisely where a token would be
logged. Handlers, by contrast, filter every record that reaches them, however it
got there.

The second half is making sure library loggers *do* propagate: a library that
installs its own handler would emit straight to stderr, bypassing ours. So they
are given an empty handler list and ``propagate=True``.

Redaction is pattern-based, applied to the formatted message and to every
argument. Pattern-based rather than "redact these known values" because the
value we most need to catch is the one we did not know we were holding — a token
inside a provider error body, say.
"""

from __future__ import annotations

import logging
import re
from typing import Any

#: Providers whose credentials have a recognisable shape. Ordered longest-prefix
#: first so a `xoxb-`-prefixed value is not partly matched by a shorter rule.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Slack: xoxb (bot), xoxp (user), xoxe (refresh), xapp (app-level).
    re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{8,}", re.IGNORECASE),
    re.compile(r"\bxapp-[A-Za-z0-9-]{8,}", re.IGNORECASE),
    # Google access tokens (`ya29.`) and refresh tokens (`1//`).
    re.compile(r"\bya29\.[A-Za-z0-9._\-]{10,}"),
    re.compile(r"\b1//[A-Za-z0-9._\-]{10,}"),
    # Google OAuth client secrets.
    re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{10,}"),
    # Our own API keys.
    re.compile(r"\bsk_live_[A-Za-z0-9_]{8,}"),
    # Anything explicitly presented as a bearer credential.
    re.compile(r"(?i)\b(authorization|bearer)[=:\s]+[A-Za-z0-9._\-]{12,}"),
    # JSON/qs fields that are credentials by name whatever their shape.
    re.compile(
        r"(?i)\"(access_token|refresh_token|client_secret|token|id_token|code)\"\s*:\s*\"[^\"]+\""
    ),
    re.compile(
        r"(?i)\b(access_token|refresh_token|client_secret|id_token)=[A-Za-z0-9._\-]{6,}"
    ),
)

REDACTED = "«redacted»"

#: Libraries that log request URLs, headers or bodies. Forced through our
#: handlers rather than their own, because a library handler writes to stderr
#: without ever meeting a filter of ours.
NOISY_LIBRARIES = ("httpx", "httpcore", "urllib3", "asyncio", "sqlalchemy.engine")


def redact(text: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(_replacement, text)
    return text


def _replacement(match: re.Match[str]) -> str:
    """Keep the *shape* of what was redacted, never a prefix of the value.

    ``xoxb-«redacted»`` tells an operator which credential was involved, which
    is the entire diagnostic value, while leaking nothing: a prefix like
    ``xoxb-1234…`` is a real substring of a real secret and has a habit of being
    enough when combined with something else.
    """
    raw = match.group(0)
    for marker in ("xoxb-", "xoxp-", "xoxe-", "xoxa-", "xoxr-", "xoxs-", "xapp-",
                   "ya29.", "1//", "GOCSPX-", "sk_live_"):
        if raw.lower().startswith(marker.lower()):
            return f"{marker}{REDACTED}"
    key = raw.split("=")[0].split(":")[0].strip().strip('"')
    return f"{key}={REDACTED}" if key else REDACTED


class RedactingFilter(logging.Filter):
    """Rewrites the record in place, message and args alike.

    Returns ``True`` always — this filter never drops a record. Dropping would
    lose the diagnostic; the goal is to keep the sentence and remove the secret.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = _redact_args(record.args)
        # `exc_text` is the cached rendering of a traceback. A provider client
        # that puts a token in an exception message lands here and nowhere else.
        if getattr(record, "exc_text", None):
            record.exc_text = redact(str(record.exc_text))
        return True


def _redact_args(args: Any) -> Any:
    if isinstance(args, dict):
        return {key: _redact_value(value) for key, value in args.items()}
    if isinstance(args, tuple):
        return tuple(_redact_value(value) for value in args)
    return _redact_value(args)


def _redact_value(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


def install_redaction(logger: logging.Logger | None = None) -> RedactingFilter:
    """Attach the filter to every handler on the root logger.

    Idempotent: re-running (a test, a reload) does not stack duplicate filters,
    which would be harmless but would make "is it installed?" unanswerable.
    """
    root = logger or logging.getLogger()
    if not root.handlers:
        # Nothing to attach to yet. A handler is added rather than deferring,
        # because "no handler" means `logging.lastResort` prints to stderr
        # *unfiltered* — the failure mode this module exists to prevent.
        root.addHandler(logging.StreamHandler())

    installed = RedactingFilter()
    for handler in root.handlers:
        if not any(isinstance(existing, RedactingFilter) for existing in handler.filters):
            handler.addFilter(installed)

    for name in NOISY_LIBRARIES:
        library = logging.getLogger(name)
        # Empty handler list + propagate: everything they emit is funnelled
        # through the root handlers, which now carry the filter.
        library.handlers = []
        library.propagate = True

    return installed
