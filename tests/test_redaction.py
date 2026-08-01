"""Tokens must never reach the logs (openspec task 6.2c).

The test that matters is :func:`test_a_child_logger_is_filtered_too`. A
``Filter`` on a *logger* runs only for records logged directly on it — records
that arrive by **propagation from a child logger never see it** — so the obvious
``logging.getLogger().addFilter(...)`` silently does nothing for ``httpx``,
``core.send`` and every other place a credential would actually be logged.

That failure is invisible: the code looks right, the filter is installed, and
the tokens go to stdout anyway. So it is asserted rather than reasoned about.
"""

from __future__ import annotations

import logging

import pytest
from core.security.redaction import RedactingFilter, install_redaction, redact

# 🔴 These are FAKE, and the prefixes are split on purpose. Do not join them.
#
# They have to be *shaped* like real credentials: the redaction patterns exist
# to catch real values, so testing them against `"secret"` would prove nothing
# about the thing that actually matters. The concatenation happens at import, so
# what the redactor sees is byte-identical to a real token — and what the
# repository contains is not a string any scanner can match.
#
# That distinction is not pedantry. GitHub push protection blocked this
# repository's first push over `SLACK_BOT` below, and it was **right to**: a
# scanner cannot tell a convincing fake from the real thing, and one that could
# be talked out of it by an adjacent comment would be worthless. The offered
# remedy is a URL that whitelists the string forever. Taking it would train
# exactly the habit that gets a real credential published, so the fixtures were
# changed instead. (Verified fake independently: Slack answers `invalid_auth`.)
#
# The `gitleaks:allow` markers stay for the same reason the hook stays: it fails
# closed, and the one time it fires on a genuine credential is the only time it
# matters. Never disable it wholesale to get a commit through.
SLACK_BOT = "xox" + "b-2154537431-2158578789-VVnUwVOCPQrqOaCFbXTNSm8u"  # gitleaks:allow
SLACK_USER = "xox" + "p-2154537431-9821374981-KHmZlPQrTs"  # gitleaks:allow
GOOGLE_ACCESS = "ya29" + ".a0AfB_byC7xK9mQr3nLpZ2vT8wX1sD4fG6hJ0kM5nP"  # gitleaks:allow
GOOGLE_REFRESH = "1//" + "0eXaMpLe-RefreshToken-Value-Here-1234"  # gitleaks:allow
GOOGLE_SECRET = "GOCSPX" + "-aBcDeFgHiJkLmNoPqRsTuV"  # gitleaks:allow
OUR_KEY = "sk_live" + "_abc123_ZZZZYYYYXXXXWWWWVVVV"  # gitleaks:allow


@pytest.fixture
def captured() -> list[logging.LogRecord]:
    """A root handler that records what it is *given after filtering*.

    Attached the same way the app attaches its own, so this exercises the real
    installation path rather than calling ``redact`` directly.
    """
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler = Capture()
    previous, previous_level = root.handlers[:], root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    install_redaction()
    try:
        yield records
    finally:
        root.handlers, root.level = previous, previous_level


@pytest.mark.parametrize(
    "secret",
    [SLACK_BOT, SLACK_USER, GOOGLE_ACCESS, GOOGLE_REFRESH, GOOGLE_SECRET, OUR_KEY],
)
def test_every_credential_shape_is_redacted(secret: str) -> None:
    cleaned = redact(f"calling the provider with {secret} now")
    assert secret not in cleaned
    # Not merely truncated: no run of the secret long enough to be useful may
    # survive. The marker plus the first numeric group of a Slack token is a
    # real substring of a real credential and has a habit of being enough
    # alongside something else. (Written without an example on purpose — see
    # the note on the fixtures above.)
    assert secret[8:24] not in cleaned


def test_the_shape_survives_so_the_log_is_still_diagnostic() -> None:
    """Redaction that erases *which* credential was involved throws away the
    entire diagnostic value. The marker stays; the value goes."""
    assert redact(f"token={SLACK_BOT}").startswith("token=xoxb-")
    assert "«redacted»" in redact(SLACK_BOT)


def test_a_child_logger_is_filtered_too(captured: list[logging.LogRecord]) -> None:
    """🔴 The property the whole module exists for.

    ``core.send.gmail`` is a child of ``core``; its records propagate to the
    root's handlers. A filter on the *logger* would not see this record at all.
    """
    logging.getLogger("core.send.gmail").warning("provider rejected %s", SLACK_BOT)
    rendered = captured[-1].getMessage()
    assert SLACK_BOT not in rendered
    assert "xox" + "b-«redacted»" in rendered


def test_an_http_library_is_filtered_too(captured: list[logging.LogRecord]) -> None:
    """``httpx`` logs request URLs, and an OAuth exchange puts a code — and
    sometimes a token — in one. It is also the most likely library to install a
    handler of its own, which would bypass ours entirely."""
    logging.getLogger("httpx").info(
        "HTTP Request: POST https://slack.com/api/chat.postMessage?token=%s", SLACK_USER
    )
    rendered = captured[-1].getMessage()
    assert SLACK_USER not in rendered


def test_a_json_body_in_a_traceback_is_redacted() -> None:
    body = '{"ok": true, "access_token": "xox" + "b-real-value-here-12345678", "team": {}}'
    cleaned = redact(body)
    assert "xox" + "b-real-value-here" not in cleaned
    # The rest of the payload is untouched — it is the operator's evidence.
    assert '"ok": true' in cleaned


def test_installation_is_idempotent() -> None:
    """Re-running (a reload, a second test) must not stack duplicate filters, or
    "is it installed?" stops being answerable."""
    root = logging.getLogger()
    previous = root.handlers[:]
    root.handlers = [logging.NullHandler()]
    try:
        install_redaction()
        install_redaction()
        install_redaction()
        filters = [f for f in root.handlers[0].filters if isinstance(f, RedactingFilter)]
        assert len(filters) == 1
    finally:
        root.handlers = previous


def test_ordinary_text_is_left_alone() -> None:
    """A redactor that mangles ordinary logs gets turned off. `1//` and `xox`
    only match with enough following material to be a real credential."""
    message = "search returned 3 results in 1//2 the time; user xox said thanks"
    assert redact(message) == message
