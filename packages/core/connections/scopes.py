"""The scope pre-flight check (task 6.3b's missing half).

6.3b stored the granted scope set. Nothing compared it to what we need, so a
connection missing a permission looked perfectly healthy right up until a call
failed — Slack with `missing_scope`, or worse `not_allowed_token_type`, which
names nothing and reads like our bug. This turns that into a state the
connections page can show *before* anyone hits it.

## Two rules, both learned the hard way

🔴 **`required ⊆ granted`, never equality.** Slack OAuth v2 grants are
**additive and cannot be narrowed**: re-authorizing with a scope removed from
both the `scope` parameter *and* the app manifest still returns a token carrying
it, and only uninstalling the app from the workspace clears it (risks.md R5). So
`granted` can be a strict superset we are not able to give back, and an equality
check would report every Slack connection as broken forever.

🔴 **Compare what the provider actually returned, not what we asked for.**
Verified against the live grant on this machine: we request `email` and Google
grants `https://www.googleapis.com/auth/userinfo.email`. A subset check written
against the request would report that scope missing on a **working** connection —
and if such a check ever gated calls, it would take a healthy grant offline. The
aliases below are transcribed from a real token response, not from documentation.

## Why this reports rather than blocks

It would be easy to refuse a call when a scope is missing. It would also be a new
way for a working connection to stop working, on the strength of string
comparison against provider vocabulary that has surprised us once already. The
provider is the authority on what a token may do; this is a *hint*, so it says so
and lets the call proceed. A hint that is wrong costs a misleading banner. A gate
that is wrong costs the connection.
"""

from __future__ import annotations

from core.connections.oauth import OAuthProvider

#: What Google grants in exchange for the shorthand we request. Transcribed from
#: a real token response (connection 31, 2026-08-01) — the source of truth is the
#: wire, not the docs.
_GOOGLE_ALIASES: dict[str, str] = {
    "email": "https://www.googleapis.com/auth/userinfo.email",
    "profile": "https://www.googleapis.com/auth/userinfo.profile",
}


def _canonical(scope: str) -> str:
    return _GOOGLE_ALIASES.get(scope, scope)


def required_scopes(provider: OAuthProvider) -> tuple[str, ...]:
    """Everything a fully working connection to this provider needs.

    Bot scopes and user scopes together: they arrive on different tokens, but a
    connection missing either one is equally unable to do its job — and
    ``search:read`` landing in the wrong bucket is precisely the failure R4
    describes.
    """
    return tuple(dict.fromkeys(provider.scopes + provider.user_scopes))


def missing_scopes(provider: OAuthProvider, granted: tuple[str, ...]) -> tuple[str, ...]:
    """What we asked for and did not get. Empty means the grant is sufficient.

    Returned in the vocabulary we **requested**, not the canonical form, because
    that is what a person reads in the consent screen and in our own config.
    """
    have = {_canonical(scope) for scope in granted}
    return tuple(
        scope for scope in required_scopes(provider) if _canonical(scope) not in have
    )


def satisfied(provider: OAuthProvider, granted: tuple[str, ...]) -> bool:
    return not missing_scopes(provider, granted)
