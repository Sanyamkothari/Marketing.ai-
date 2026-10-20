"""OAuth 2.0 authorization code flow: state, PKCE, redirect handling, code exchange and refresh.

The connect flow is three steps, and only the first and last involve a person's browser:

1. `begin_authorization` makes a random `state` (and, where the platform supports it, a PKCE
   verifier whose S256 challenge goes in the URL), stores a `PendingAuthorization` for 10 minutes and
   returns the platform's consent URL.
2. The platform redirects back with `code` and `state`. `finish_authorization` takes the pending
   record (single use: a replayed callback finds nothing), checks it has not expired and that the
   platform did not report an error (`error=access_denied` when the person clicked "cancel").
3. `OAuthClient.exchange` trades the code (and verifier) for tokens; `OAuthClient.refresh` later
   trades a refresh token for new ones. Both parse the token response defensively: unknown fields
   are ignored, a missing `expires_in` means "does not expire" (Shopify non-expiring offline tokens),
   and a response without a new refresh token keeps the old one (Klaviyo keeps its refresh token;
   Shopify's expiring offline tokens rotate it and invalidate the old one at once, which is why
   refreshing is serialised per connection in `tokens.TokenManager`).

**Revoked versus transient.** A refresh refused with an error in `provider.revoked_errors`
(`invalid_grant`, and Shopify's `invalid_request` on a dead refresh token) or with HTTP 401 raises
`NeedsReconnect`: retrying cannot help and the connection must be re-authorised by a person. A 5xx,
a 429 or a timeout raises a retryable failure and the token manager tries again later.

Platform notes (pinned in each connector, recorded here for the shape the code expects):

* Klaviyo (docs read Oct 2026): authorize at `https://www.klaviyo.com/oauth/authorize` with PKCE
  S256 (`code_challenge_method=S256`); token at `https://a.klaviyo.com/oauth/token`, client
  authenticated with HTTP Basic; access tokens ~1 h; refresh tokens revoked after 90 days unused
  and refreshes capped at 10 a minute; `invalid_grant` means revoked or uninstalled.
* Shopify (docs read Oct 2026): authorize at `https://{shop}/admin/oauth/authorize`, token at
  `https://{shop}/admin/oauth/access_token`, client id and secret in the body, no PKCE; the
  callback carries an `hmac` over the query (`webhooks.query_hmac_matches`); expiring offline
  tokens are requested with `expiring=1` in the code-exchange POST (`extra_token_params`), last
  1 h with a 90-day rotating refresh token; a dead refresh token is
  `401 {"error": "invalid_request"}`, while on a timeout, 5xx or 429 the docs say to retry with the
  same refresh token (which is why the token call is `retry_safe`).
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets as _secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from autonomy.connectors.base import ConnectorFailure, NeedsReconnect, RemoteError, TransientFailure
from autonomy.connectors.http import ConnectorHttp
from autonomy.connectors.tokens import TokenSet

__all__ = [
    "AppCredentials",
    "AuthStateStore",
    "AuthorizationDenied",
    "AuthorizationError",
    "MemoryAuthStateStore",
    "OAuthClient",
    "OAuthProvider",
    "PendingAuthorization",
    "authorization_url",
    "begin_authorization",
    "finish_authorization",
    "parse_token_response",
    "pkce_challenge",
]

STATE_BYTES: Final[int] = 32
VERIFIER_BYTES: Final[int] = 64
"""64 random bytes → an 86-character url-safe verifier (RFC 7636 allows 43-128)."""
_TEMPLATE_PARAM: Final[re.Pattern[str]] = re.compile(r"\{([a-z_]+)\}")
RandomToken = Callable[[int], str]


class AuthorizationError(ConnectorFailure):
    """The callback cannot be trusted or completed: unknown or reused state, expired, malformed."""

    code = "connector.oauth_invalid"


class AuthorizationDenied(AuthorizationError):  # noqa: N818
    """The person declined on the platform's consent screen."""

    code = "connector.oauth_denied"


@dataclass(frozen=True)
class AppCredentials:
    """Our app's client id and secret on one platform. The secret never appears in a repr."""

    client_id: str
    client_secret: str = field(repr=False)

    @staticmethod
    def env_names(kind: str) -> tuple[str, str]:
        prefix = f"AUTONOMY_{kind.upper()}"
        return f"{prefix}_CLIENT_ID", f"{prefix}_CLIENT_SECRET"

    @classmethod
    def from_env(cls, kind: str, environ: Mapping[str, str]) -> AppCredentials | None:
        """`AUTONOMY_<KIND>_CLIENT_ID` and `AUTONOMY_<KIND>_CLIENT_SECRET`, or None when either is unset."""
        id_name, secret_name = cls.env_names(kind)
        client_id, secret = environ.get(id_name, "").strip(), environ.get(secret_name, "").strip()
        if not client_id or not secret:
            return None
        return cls(client_id=client_id, client_secret=secret)


@dataclass(frozen=True)
class OAuthProvider:
    """One platform's OAuth endpoints and rules. URLs may hold `{param}` slots (e.g. `{shop}`)."""

    name: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...] = ()
    scope_separator: str = " "
    use_pkce: bool = False
    client_auth: Literal["basic", "body"] = "body"
    revoke_url: str | None = None
    extra_authorize_params: tuple[tuple[str, str], ...] = ()
    extra_token_params: tuple[tuple[str, str], ...] = ()
    """Added to the code-exchange POST (Shopify: `("expiring", "1")` for expiring offline tokens; the
    docs put it in the `/admin/oauth/access_token` request, not in the authorize URL)."""
    revoked_errors: frozenset[str] = frozenset({"invalid_grant"})
    refresh_unauthorized_is_revoked: bool = True
    state_ttl: timedelta = timedelta(minutes=10)
    refresh_margin: timedelta = timedelta(minutes=5)
    """Refresh an access token this long before it expires."""
    keep_alive_every: timedelta | None = None
    """Refresh at least this often even when idle (platforms that revoke unused refresh tokens)."""
    min_refresh_interval: timedelta = timedelta(seconds=10)
    """Never refresh one connection more often than this (Klaviyo allows 10 refreshes a minute)."""
    param_patterns: tuple[tuple[str, str], ...] = ()
    """`{param}` name -> full-match regex, e.g. ("shop", r"[a-z0-9][a-z0-9-]*\\.myshopify\\.com")."""

    def render(self, template: str, params: Mapping[str, str]) -> str:
        """`template` with its `{param}` slots filled from `params`, each checked against its pattern.

        A slot with no pattern is refused: a URL host built from what a browser sent us must never
        be free text (it would let a callback point our client secret at any server)."""
        patterns = dict(self.param_patterns)

        def fill(match: re.Match[str]) -> str:
            name = match.group(1)
            value = params.get(name, "")
            pattern = patterns.get(name)
            if pattern is None or not re.fullmatch(pattern, value):
                raise AuthorizationError(f"the {name} given is not valid")
            return value

        return _TEMPLATE_PARAM.sub(fill, template)


class PendingAuthorization(BaseModel):
    """A connect flow between "go to the platform" and its callback. Lives at most `state_ttl`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str
    kind: str
    business_id: str
    redirect_uri: str
    code_verifier: str | None = Field(default=None, repr=False)
    params: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    expires_at: datetime
    return_to: str | None = None


class AuthStateStore(Protocol):
    def put_pending(self, pending: PendingAuthorization) -> None: ...

    def take_pending(self, state: str, now: datetime) -> PendingAuthorization | None:
        """Remove and return the pending flow for `state`; None when unknown, used or expired."""
        ...


class MemoryAuthStateStore:
    """For tests and a single-process demo; production uses `ConnectorStore` (SQL)."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingAuthorization] = {}

    def put_pending(self, pending: PendingAuthorization) -> None:
        self._pending[pending.state] = pending

    def take_pending(self, state: str, now: datetime) -> PendingAuthorization | None:
        pending = self._pending.pop(state, None)
        if pending is None or pending.expires_at <= now:
            return None
        return pending


def pkce_challenge(verifier: str) -> str:
    """The S256 code challenge: base64url(SHA-256(verifier)) without padding (RFC 7636 §4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(
    provider: OAuthProvider,
    app: AppCredentials,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str | None,
    params: Mapping[str, str],
) -> str:
    query: dict[str, str] = {
        "client_id": app.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if provider.scopes:
        query["scope"] = provider.scope_separator.join(provider.scopes)
    if code_challenge is not None:
        query["code_challenge"] = code_challenge
        query["code_challenge_method"] = "S256"
    query.update(dict(provider.extra_authorize_params))
    base = provider.render(provider.authorize_url, params)
    return f"{base}{'&' if '?' in base else '?'}{urlencode(query)}"


def begin_authorization(
    provider: OAuthProvider,
    app: AppCredentials,
    states: AuthStateStore,
    *,
    kind: str,
    business_id: str,
    redirect_uri: str,
    now: datetime,
    params: Mapping[str, str] | None = None,
    return_to: str | None = None,
    random_token: RandomToken = _secrets.token_urlsafe,
) -> tuple[str, PendingAuthorization]:
    """Start a connect: store the pending flow and return (consent URL, pending record)."""
    given = dict(params or {})
    for name in _TEMPLATE_PARAM.findall(provider.authorize_url + provider.token_url):
        provider.render("{" + name + "}", given)  # refuse a bad shop domain before anything is stored
    verifier = random_token(VERIFIER_BYTES) if provider.use_pkce else None
    pending = PendingAuthorization(
        state=random_token(STATE_BYTES),
        kind=kind,
        business_id=business_id,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        params=given,
        created_at=now,
        expires_at=now + provider.state_ttl,
        return_to=return_to,
    )
    states.put_pending(pending)
    url = authorization_url(
        provider,
        app,
        redirect_uri=redirect_uri,
        state=pending.state,
        code_challenge=pkce_challenge(verifier) if verifier is not None else None,
        params=given,
    )
    return url, pending


def finish_authorization(
    query: Mapping[str, str], states: AuthStateStore, now: datetime
) -> tuple[PendingAuthorization, str]:
    """Check a callback's `state`; return the pending flow and the authorization code.

    The pending record is taken (removed) before anything else is checked, so a callback can be
    used once, whether it succeeds or not."""
    state = query.get("state", "")
    if not state:
        raise AuthorizationError("the callback has no state")
    pending = states.take_pending(state, now)
    if pending is None:
        raise AuthorizationError("this connect link has expired or was already used; start again")
    error = query.get("error")
    if error:
        if error == "access_denied":
            raise AuthorizationDenied("the connection was not approved on the platform's screen")
        raise AuthorizationError(f"the platform reported {error!r}")
    code = query.get("code", "")
    if not code:
        raise AuthorizationError("the callback has no authorization code")
    return pending, code


def parse_token_response(body: Any, now: datetime, previous: TokenSet | None = None) -> TokenSet:
    """A token endpoint's JSON as a `TokenSet`. Tolerant of extra fields and of missing optional ones."""
    if not isinstance(body, Mapping) or not isinstance(body.get("access_token"), str):
        raise RemoteError("the token endpoint answered without an access token")
    expires_at = _after(now, body.get("expires_in"))
    refresh = body.get("refresh_token")
    refresh_token = refresh if isinstance(refresh, str) and refresh else None
    if refresh_token is None and previous is not None:
        refresh_token = previous.refresh_token
    refresh_expires_at = _after(now, body.get("refresh_token_expires_in"))
    if refresh_expires_at is None and previous is not None and refresh_token == previous.refresh_token:
        refresh_expires_at = previous.refresh_expires_at
    scope = body.get("scope")
    kept = dict(previous.extra) if previous is not None else {}
    kept |= {k: str(v) for k, v in body.items() if k in {"associated_user_scope", "account_id", "shop"}}
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=refresh_token,
        token_type=str(body.get("token_type") or "bearer"),
        scope=scope if isinstance(scope, str) else (previous.scope if previous else None),
        expires_at=expires_at,
        refresh_expires_at=refresh_expires_at,
        obtained_at=previous.obtained_at if previous is not None else now,
        refreshed_at=now,
        extra=kept,
    )


def _after(now: datetime, seconds: Any) -> datetime | None:
    if isinstance(seconds, bool):
        return None
    if isinstance(seconds, int | float) and seconds > 0:
        return now + timedelta(seconds=float(seconds))
    if isinstance(seconds, str) and seconds.isdigit() and int(seconds) > 0:
        return now + timedelta(seconds=int(seconds))
    return None


HttpFactory = Callable[[str], ConnectorHttp]
"""Builds an unauthenticated client for a base URL (the token endpoint's origin)."""


class OAuthClient:
    """Calls one platform's token endpoint, through a client built for that endpoint's host.

    The host can differ per connection (Shopify's is the shop's own domain), so the client is
    built per call by `http_factory` from the rendered, pattern-checked token URL."""

    def __init__(self, provider: OAuthProvider, app: AppCredentials, http_factory: HttpFactory) -> None:
        self.provider = provider
        self.app = app
        self.http_factory = http_factory

    def exchange(self, pending: PendingAuthorization, code: str, now: datetime) -> TokenSet:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": pending.redirect_uri}
        form.update(dict(self.provider.extra_token_params))
        if pending.code_verifier is not None:
            form["code_verifier"] = pending.code_verifier
        body = self._token_call(form, pending.params, refreshing=False)
        return parse_token_response(body, now)

    def refresh(self, tokens: TokenSet, now: datetime, params: Mapping[str, str]) -> TokenSet:
        if tokens.refresh_token is None:
            raise NeedsReconnect(f"{self.provider.name} gave no refresh token; connect again")
        form = {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
        body = self._token_call(form, params, refreshing=True)
        return parse_token_response(body, now, previous=tokens)

    def _token_call(self, form: dict[str, str], params: Mapping[str, str], *, refreshing: bool) -> Any:
        headers: dict[str, str] = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.provider.client_auth == "basic":
            pair = f"{self.app.client_id}:{self.app.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
        else:
            form = form | {"client_id": self.app.client_id, "client_secret": self.app.client_secret}
        url = self.provider.render(self.provider.token_url, params)
        parts = urlsplit(url)
        http = self.http_factory(f"{parts.scheme}://{parts.netloc}")
        # A token call is safe to retry after a timeout: an authorization code is single-use, so a
        # repeat fails cleanly (invalid_grant) rather than issuing two token sets.
        try:
            result = http.send("POST", url, form=form, headers=headers, authenticated=False, retry_safe=True)
        finally:
            http.close()
        if result.ok:
            return result.json()
        error = _error_code(result.body)
        if error in self.provider.revoked_errors or (
            refreshing and result.status == 401 and self.provider.refresh_unauthorized_is_revoked
        ):
            raise NeedsReconnect(
                f"{self.provider.name} refused the {'refresh' if refreshing else 'authorization'} "
                f"({error or result.status}); the account must be connected again",
                status=result.status,
            )
        if result.status >= 500:
            raise TransientFailure(f"{self.provider.name}'s token endpoint answered {result.status}")
        raise RemoteError(
            f"{self.provider.name}'s token endpoint refused the request ({error or result.status})",
            status=result.status,
        )


def _error_code(body: bytes) -> str | None:
    import json

    try:
        data = json.loads(body) if body else None
    except ValueError:
        return None
    if isinstance(data, Mapping):
        error = data.get("error")
        if isinstance(error, str):
            return error
        if isinstance(data.get("errors"), list) and data["errors"]:
            first = data["errors"][0]
            if isinstance(first, Mapping) and isinstance(first.get("code"), str):
                return str(first["code"])
    return None
