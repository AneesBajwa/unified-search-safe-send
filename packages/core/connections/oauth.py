"""The two OAuth flows, as data plus two functions (tasks 6.3-6.4b, design D8).

Google and Slack differ in almost every detail that matters — where scopes go,
how many tokens come back, what counts as an account identifier — so the
differences live in a descriptor per provider rather than in branches scattered
through the exchange code.

Three rules encoded here that are easy to get wrong and expensive to get wrong:

🔴 **`search:read` goes in Slack's `user_scope`, not `scope`.** A bot token sent
to `search.messages` returns `not_allowed_token_type`. One install therefore
yields **two** tokens and the connection stores both (R4).

🔴 **`prompt=consent` only when we need a refresh token** — never on every
login. Google mints a *new* refresh token each time consent is granted and caps
an account at 100 per client id, silently evicting the oldest. Re-prompting
every login walks into that cap and breaks our own oldest connections with no
error surfaced anywhere (R21).

🔴 **`gmail.compose`, not `gmail.send`.** Draft-then-send is the idempotency
mechanism (R3). Counter-intuitively `gmail.send` is only *sensitive* while
`gmail.readonly` — which search needs regardless — is *restricted*, so the read
scope is what puts us in the restricted tier and `gmail.compose` costs nothing
extra.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from core.config import get_settings
from core.enums import ProviderKind
from core.errors import ProviderError
from core.http import json_body, provider_client


class OAuthConfigError(RuntimeError):
    """A client id or secret is missing.

    Classifies as ``config`` rather than ``needs_reconnect``: it is our bug, and
    telling a user to reconnect an account whose grant was never the problem
    sends them round in circles (R24).
    """


@dataclass(frozen=True)
class Grant:
    """What one authorization yields, provider-shaped differences resolved.

    ``__repr__`` is overridden rather than relying on discipline: this object
    exists for about four lines between a provider response and an encrypted
    column, and those four lines are exactly where a stray ``log.info(grant)``
    or a pydantic traceback would leak every token at once (task 6.2b).
    """

    external_account_id: str
    display_name: str
    granted_scopes: tuple[str, ...] = ()
    access_token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    bot_token: str | None = field(default=None, repr=False)
    access_expires_at: datetime | None = None

    def __repr__(self) -> str:
        return (
            f"Grant(external_account_id={self.external_account_id!r}, "
            f"display_name={self.display_name!r}, "
            f"scopes={len(self.granted_scopes)}, tokens=«redacted»)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class OAuthProvider:
    name: str
    authorize_url: str
    token_url: str
    #: Slack's bot scopes; Google's whole scope list.
    scopes: tuple[str, ...]
    #: Slack only. `search:read` lives here or search fails at runtime (R4).
    user_scopes: tuple[str, ...] = ()

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind(self.name)


GOOGLE = OAuthProvider(
    name="gmail",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",  # noqa: S106 - an endpoint URL
    scopes=(
        "openid",
        "email",
        # Restricted, and unavoidable: `gmail.metadata` is *also* restricted and
        # cannot use the `q` parameter at all, so it cannot search.
        "https://www.googleapis.com/auth/gmail.readonly",
        # Draft-then-send. Never `gmail.send` (R3), never `gmail.modify`,
        # `gmail.insert` or the full `https://mail.google.com/`.
        "https://www.googleapis.com/auth/gmail.compose",
    ),
)

SLACK = OAuthProvider(
    name="slack",
    authorize_url="https://slack.com/oauth/v2/authorize",
    token_url="https://slack.com/api/oauth.v2.access",  # noqa: S106 - an endpoint URL
    scopes=(
        "chat:write",
        # Post to public channels WITHOUT joining — kills most `not_in_channel`.
        "chat:write.public",
        "channels:read",
        # Reconciliation probe and the degraded search fallback.
        "channels:history",
        "channels:join",
        # Requirement is documented inconsistently, so request it and verify
        # empirically (R5, task 8.4b).
        "metadata.message:read",
    ),
    user_scopes=("search:read",),
)

PROVIDERS: dict[str, OAuthProvider] = {GOOGLE.name: GOOGLE, SLACK.name: SLACK}


def provider_for(name: str) -> OAuthProvider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise OAuthConfigError(
            f"no OAuth provider named {name!r}; known: {', '.join(sorted(PROVIDERS))}"
        ) from None


def credentials_for(provider: OAuthProvider) -> tuple[str, str]:
    settings = get_settings()
    if provider is GOOGLE:
        client_id = settings.google_client_id
        secret = settings.google_client_secret.get_secret_value()
    else:
        client_id = settings.slack_client_id
        secret = settings.slack_client_secret.get_secret_value()
    if not client_id or not secret:
        raise OAuthConfigError(
            f"{provider.name} OAuth is not configured: set "
            f"{provider.name.upper()}_CLIENT_ID and _CLIENT_SECRET"
        )
    return client_id, secret


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------


def authorize_url(provider: OAuthProvider, *, state: str, force_consent: bool) -> str:
    """Where the browser goes.

    ``force_consent`` is the R21 switch. True only when we hold no refresh token
    for this account or we are recovering from ``invalid_grant`` — never on an
    ordinary login.
    """
    client_id, _ = credentials_for(provider)
    settings = get_settings()
    redirect_uri = settings.redirect_uri(provider.name)

    if provider is GOOGLE:
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(provider.scopes),
            # Without this Google issues no refresh token at all, and every
            # cold start would need the user present.
            "access_type": "offline",
            "include_granted_scopes": "true",
            "state": state,
        }
        if force_consent:
            params["prompt"] = "consent"
        return f"{provider.authorize_url}?{urlencode(params)}"

    if not settings.oauth_tunnel_url and settings.app_base_url.startswith("http://"):
        # Caught here with a sentence rather than at Slack with `bad_redirect_uri`
        # 30 seconds later in a browser tab (R18).
        raise OAuthConfigError(
            "Slack rejects http:// redirect URIs outright. Set OAUTH_TUNNEL_URL "
            "to an HTTPS tunnel (ngrok or equivalent) and register "
            f"{redirect_uri} as a redirect URL on the Slack app"
        )

    return f"{provider.authorize_url}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": ",".join(provider.scopes),
            # 🔴 Separate list. `search:read` here, not above (R4).
            "user_scope": ",".join(provider.user_scopes),
            "state": state,
        }
    )


# ---------------------------------------------------------------------------
# Exchange and refresh
# ---------------------------------------------------------------------------


async def exchange_code(provider: OAuthProvider, code: str) -> Grant:
    client_id, secret = credentials_for(provider)
    settings = get_settings()
    form = {
        "client_id": client_id,
        "client_secret": secret,
        "code": code,
        # ⚠️ Must be byte-identical to the one sent on the authorize call, on
        # both providers, whenever more than one redirect URL is registered.
        "redirect_uri": settings.redirect_uri(provider.name),
    }
    if provider is GOOGLE:
        form["grant_type"] = "authorization_code"

    payload = await _post_form(provider, form)
    return _grant_from(provider, payload)


async def refresh_access_token(provider: OAuthProvider, refresh_token: str) -> Grant:
    """Exchange a refresh token for a new access token.

    🔴 The returned ``Grant`` carries ``refresh_token=None`` whenever the
    provider omitted one, and the persist path treats ``None`` as **keep what
    you have** — never as "clear the column". Google returns a refresh token
    only on first authorization and omits it on every ordinary refresh, so the
    naive ``token = body.get("refresh_token")`` permanently breaks the
    connection (R21).
    """
    client_id, secret = credentials_for(provider)
    payload = await _post_form(
        provider,
        {
            "client_id": client_id,
            "client_secret": secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    return _grant_from(provider, payload, refreshing=True)


async def _post_form(provider: OAuthProvider, form: dict[str, str]) -> dict[str, Any]:
    async with provider_client() as client:
        response = await client.post(provider.token_url, data=form)
    payload = json_body(response)

    if provider is GOOGLE:
        if response.status_code >= 400 or "error" in payload:
            # 🔴 Classified on the `error` field alone. `error_description` is
            # logged as telemetry and never branched on: Google has changed
            # those strings before, and branching on them is how a working
            # classifier quietly stops classifying (task 6.3e).
            raise ProviderError.from_google(
                payload, status=response.status_code, headers=dict(response.headers)
            )
        return payload

    if not payload.get("ok", False):
        raise ProviderError.from_slack(
            payload, status=response.status_code, headers=dict(response.headers)
        )
    return payload


def _grant_from(
    provider: OAuthProvider, payload: dict[str, Any], *, refreshing: bool = False
) -> Grant:
    if provider is GOOGLE:
        return _google_grant(payload, refreshing=refreshing)
    return _slack_grant(payload)


def _google_grant(payload: dict[str, Any], *, refreshing: bool) -> Grant:
    claims = _id_token_claims(payload.get("id_token"))
    expires_in = payload.get("expires_in")
    return Grant(
        # `sub` is the stable account identifier; email is not — it can change
        # and it is not unique over time. On a refresh there is no id_token, so
        # the caller keeps the identity it already matched on.
        external_account_id=str(claims.get("sub", "")) if not refreshing else "",
        display_name=str(claims.get("email", "")) if not refreshing else "",
        granted_scopes=tuple(str(payload.get("scope", "")).split()),
        access_token=payload.get("access_token"),
        # 🔴 None means "keep the stored one". No `else` branch clears it, ever.
        refresh_token=payload.get("refresh_token"),
        access_expires_at=_expiry(expires_in),
    )


def _slack_grant(payload: dict[str, Any]) -> Grant:
    team = payload.get("team") or {}
    authed_user = payload.get("authed_user") or {}
    team_id = str(team.get("id", ""))
    user_id = str(authed_user.get("id", ""))
    scopes = tuple(str(payload.get("scope", "")).split(","))
    user_scopes = tuple(str(authed_user.get("scope", "")).split(","))
    return Grant(
        # Both halves: a workspace and a user within it. Two people installing
        # into the same workspace are two connections, and must be.
        external_account_id=f"{team_id}:{user_id}",
        display_name=str(team.get("name") or team_id),
        granted_scopes=tuple(scope for scope in scopes + user_scopes if scope),
        # The *user* token — search.messages needs this one and rejects the bot
        # token with `not_allowed_token_type` (R4).
        access_token=authed_user.get("access_token"),
        # Slack access tokens do not expire and token rotation is deliberately
        # off (irreversible once enabled, and it buys nothing here), so there is
        # no refresh token to store.
        refresh_token=None,
        bot_token=payload.get("access_token"),
        access_expires_at=None,
    )


def _expiry(expires_in: Any) -> datetime | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _id_token_claims(id_token: Any) -> dict[str, Any]:
    """Read the claims without verifying the signature.

    Correct in this one narrow case and nowhere else: OpenID Connect's core spec
    explicitly permits skipping signature validation for an id_token received
    **directly from the token endpoint over TLS** in the authorization-code
    flow, because the channel itself is the guarantee. An id_token arriving by
    any other route would need full verification.
    """
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        return {}
    payload = id_token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + padding))
    except (ValueError, binascii.Error):
        return {}
    # A JWT body is JSON but not necessarily an object. Checked rather than
    # trusted, because the caller indexes it.
    return claims if isinstance(claims, dict) else {}
