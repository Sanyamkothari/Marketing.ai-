"""Who a request is: the identity providers `api/access.py` chooses between by `settings.auth_mode`.

An identity provider turns the request's `Authorization` header into a `Principal`, or into nothing.
It does not decide what the principal may do - that is the route policy table's job
(`api/access_policy.py`) - and it never reads a request body, so a principal cannot be claimed by
posting one.

* `DisabledIdentity` (`auth_mode=off`): every request is `LOCAL_OPERATOR`, one person at a laptop
  holding every role. It ignores the header entirely, so a stale token in a browser cannot turn a
  laptop's request into a refusal (DEC-702).
* `LocalIdentity` (`auth_mode=local`): `Authorization: Bearer <token>` resolved against the built-in
  user store's sessions (DEC-711). Anything else - no header, another scheme, an expired, revoked
  or unknown token, a disabled user - is None, and the enforcement layer answers 401.

**The M50 seam.** Plan M50 delegates sign-in to the identity provider chosen under decision P5
(Amazon Cognito, or the client's own SSO over OIDC/SAML), which is still undecided. That provider
will be a third class here with the same one-method shape - validate a JWT's signature against the
issuer's JWKS, its `aud`/`iss`/`exp`, and map a group claim onto `Role`s - selected by a third
`auth_mode` value. Nothing else changes: the policy table, the audit trail and the UI read only the
`Principal`. It is deliberately not sketched in code, because every one of those choices (which
claim carries the groups, whether roles live in the IdP or in `platform_user`) is P5's to make
(DEC-713).
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from engine.access.roles import LOCAL_OPERATOR, Principal, effective_roles
from engine.access.users import UserStore

__all__ = [
    "BEARER_SCHEME",
    "DisabledIdentity",
    "IdentityProvider",
    "LocalIdentity",
    "bearer_token",
]

BEARER_SCHEME: Final[str] = "bearer"
"""RFC 6750's scheme name, compared case-insensitively as RFC 7235 requires."""

_MAX_TOKEN_CHARS: Final[int] = 256
"""A real token is 43 characters; anything this long is not one and is not looked up."""


@runtime_checkable
class IdentityProvider(Protocol):
    """Turns an `Authorization` header into the principal it proves, or None."""

    def authenticate(self, authorization_header: str | None) -> Principal | None: ...


def bearer_token(authorization_header: str | None) -> str | None:
    """The token of `Bearer <token>`, or None for a missing header, another scheme or a malformed one."""
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != BEARER_SCHEME or not token or " " in token or len(token) > _MAX_TOKEN_CHARS:
        return None
    return token


class DisabledIdentity:
    """`auth_mode=off`: everybody is the local operator (DEC-702)."""

    def authenticate(self, authorization_header: str | None) -> Principal | None:
        """Always `LOCAL_OPERATOR`; the header is not read."""
        del authorization_header
        return LOCAL_OPERATOR


class LocalIdentity:
    """`auth_mode=local`: a bearer session issued by `POST /auth/login` against the built-in store."""

    def __init__(self, users: UserStore) -> None:
        self._users = users

    def authenticate(self, authorization_header: str | None) -> Principal | None:
        """The session's user as a `Principal`, or None when there is no live session."""
        token = bearer_token(authorization_header)
        if token is None:
            return None
        user = self._users.resolve_session(token)
        if user is None:
            return None
        return Principal(
            user_id=user.user_id,
            username=user.username,
            roles=effective_roles(user.roles),
            kind="user",
        )
