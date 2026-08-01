"""The console is a pure consumer, checked rather than asserted (task 11.9).

``ui-architecture.md`` says it in prose: *nothing under `components/` or
`routes/` decides whether a send may proceed, whether an error is retryable, or
how results rank.* Prose is not a guarantee — the backend's equivalent claim
(the fan-out layer does not know which sources exist) is defended by a test that
greps the module, and it is the only reason that claim is still true three
phases later. This is that test, pointed at ``apps/web``.

It is a grep, and a grep is exactly the right tool here. The property is
*syntactic*: the console must not contain the code, not merely avoid calling it.
A behavioural test would pass against a `canSend` helper that happens to agree
with the API today and drifts from it in six months, which is the failure this
is written to prevent.

Deliberately **not** forbidden: reading a decision the API made. The console
renders `error.classification` as amber or red, and branches on `error.code` to
pick "Connect" over "Reconnect". That is presenting a verdict, not reaching one.
The line is between *reading* a field and *computing* one, and the names below
are all names for computing.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_SOURCE = REPO_ROOT / "apps" / "web" / "src"

#: Each entry is (pattern, why it is forbidden). The patterns match identifiers
#: a person would only write if they were deriving the decision locally.
FORBIDDEN = (
    (
        r"\b(can|may|should|is)(Send|Sendable|Deliverable)\b",
        "whether a send may proceed is the API's decision — ask it, never derive it",
    ),
    (
        r"\b(can|should|is)(Retry|Retryable)\b",
        "retryability arrives as `retryable_by_operator`; a local copy of that rule "
        "will disagree with the gate eventually",
    ),
    (
        r"\b(classify|classifyError|isTransient|isPermanent)\s*\(",
        "error classification belongs to `core.errors.classify`, on the other side "
        "of the boundary",
    ),
    (
        r"\b(blended_score|source_rank|recency_weight|blendedScore|sourceRank)\b",
        "ranking inputs ride the envelope under `?debug=1` and are not for "
        "re-deriving an order the API already chose",
    ),
    (
        r"\bnew\s+EventSource\s*\(",
        "EventSource cannot set headers, so it forces the API key into the query "
        "string — where Cloud Run writes it to the request log (risks.md R7). The "
        "stream is `fetch` + `ReadableStream`",
    ),
    (
        r"crypto\.subtle\.digest|\bsha256\s*\(",
        "the confirmation digest is computed by the API over what it will actually "
        "send; a digest the client computes attests to nothing",
    ),
)

#: Fields the console must be *reading*. If one of these disappears from the
#: tree, the decision it carries has either been dropped or is being recomputed
#: somewhere — and the forbidden list above only catches the recomputation that
#: was named honestly.
REQUIRED_READS = (
    "retryable_by_operator",
    "action_url",
    "next_attempt_at",
    "backoff_seconds",
    "confirm_sha256",
)


#: Lines that are wholly a comment. Unlike the backend's source-agnosticism
#: test — where a comment naming a provider is a strong hint the code below it
#: is about to — the names forbidden here are the names of the *rules*, and the
#: files that explain why a rule exists have to be able to say it. A comment
#: cannot recompute anything, so scanning it buys nothing and costs the ability
#: to document the boundary at the boundary.
COMMENT_LINE = re.compile(r"^\s*(//|/\*|\*)")


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SOURCE.rglob("*")
        if path.suffix in {".ts", ".tsx"} and path.is_file()
    )


def test_the_console_source_tree_is_where_we_think_it_is() -> None:
    """A grep over an empty tree passes, and would pass forever."""
    files = _sources()
    assert len(files) >= 15, f"expected the console's sources under {WEB_SOURCE}, found {files}"


def test_the_console_never_recomputes_a_decision_the_api_makes() -> None:
    offences: list[str] = []
    for path in _sources():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if COMMENT_LINE.match(line):
                continue
            for pattern, why in FORBIDDEN:
                if re.search(pattern, line):
                    rel = path.relative_to(REPO_ROOT)
                    offences.append(f"{rel}:{lineno}: {line.strip()}\n    -> {why}")
    assert not offences, (
        "the console is a pure consumer of the documented API and holds no "
        "business rules:\n\n" + "\n\n".join(offences)
    )


def test_the_console_still_reads_the_decisions_rather_than_dropping_them() -> None:
    blob = "\n".join(path.read_text() for path in _sources())
    missing = [field for field in REQUIRED_READS if field not in blob]
    assert not missing, (
        "these fields exist on the wire so the console does not have to guess; "
        f"nothing in apps/web reads them any more: {missing}"
    )
