"""Signing in and managing users (Phase 4b M46): `/auth/*` and the Admin's `/users` routes.

**Sign-in** (`POST /auth/login`) is public and answers a bearer token that the UI sends as
`Authorization: Bearer <token>` (DEC-711). A wrong password, an unknown username and a disabled user
all get the same 401 and the same sentence, so the form confirms nothing about which accounts exist;
the audit event tells them apart with a `reason_code` and, when the user is known, their id - never
the username that was typed and never the password (DEC-720). With `auth_mode=off` there is nobody
to sign in as, and the route says so with `409 AUTH_OFF` rather than issuing a token nothing checks.

**`GET /auth/me`** answers who the caller is and, for every declared route, whether they may use it
and the sentence to show when not ("Only an Approver can approve a champion."). The UI hides and
explains actions from this one response rather than from a copy of the policy table of its own, so
the two cannot drift (plan M46: "The UI hides actions the user cannot take and explains why").

**User management** is Admin-only, with one exception: anybody signed in may change their *own*
password, and must give the current one to do it - a session left open on a shared screen must not
be enough to take over the account. Changing roles or disabling a user revokes their sign-ins at
once; the last active Admin cannot be disabled or demoted (`LAST_ADMIN`, DEC-712). A role change is
audited as `users.roles_change` with the new role set, so "who made Asha an Approver, and when" is
one filter in the audit viewer.

Every route here is audited by the middleware (`api/access.py`); these handlers only enrich that one
event with `set_audit_context`.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Request, Response

from api.access import (
    PrincipalDep,
    UserStoreDep,
    client_address,
    get_login_throttle,
    get_user_store,
    set_audit_context,
)
from api.access_policy import RoutePolicy, all_policies, refusal_message, register
from api.deps import SettingsDep
from api.routes.uploads import http_error
from api.schemas import (
    ErrorResponse,
    LoginRequest,
    LoginResponse,
    MeResponse,
    PasswordChangeRequest,
    PermissionView,
    PrincipalView,
    UserCreateRequest,
    UserListResponse,
    UserUpdateRequest,
    UserView,
)
from engine.access.identity import bearer_token
from engine.access.roles import Principal, Role, effective_roles
from engine.access.throttle import account_key
from engine.access.users import AccessError, UserRecord
from engine.audit.events import content_hash

__all__ = ["POLICIES", "router"]

router: APIRouter = APIRouter(tags=["auth"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("POST", "/auth/login"): RoutePolicy(
        role=None, action="auth.login", object_type="user", purpose="sign in"
    ),
    ("POST", "/auth/logout"): RoutePolicy(
        role=Role.VIEWER, action="auth.logout", object_type="user", purpose="sign out"
    ),
    ("GET", "/auth/me"): RoutePolicy(
        role=Role.VIEWER, action="auth.me", purpose="see who you are signed in as"
    ),
    ("GET", "/users"): RoutePolicy(role=Role.ADMIN, action="users.list", purpose="see the users"),
    ("POST", "/users"): RoutePolicy(
        role=Role.ADMIN, action="users.create", object_type="user", purpose="add a user"
    ),
    ("PATCH", "/users/{user_id}"): RoutePolicy(
        role=Role.ADMIN,
        action="users.update",
        object_type="user",
        object_param="user_id",
        purpose="change a user",
    ),
    ("POST", "/users/{user_id}/password"): RoutePolicy(
        role=Role.VIEWER,
        action="users.password_change",
        object_type="user",
        object_param="user_id",
        purpose="change a password",
    ),
}
"""This router's rows of the policy table, registered at import (see `api/access_policy.py`)."""

register(POLICIES)

ACCESS_ERROR_STATUS: Final[dict[str, int]] = {
    "USER_NOT_FOUND": 404,
    "USERNAME_TAKEN": 409,
    "LAST_ADMIN": 409,
}
"""`AccessError.code` -> status; every other code is the caller's input, a 422."""

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_LOGIN_ERRORS: dict[int | str, dict[str, object]] = {**_ERRORS, 429: {"model": ErrorResponse}}

_LOCKED: Final[str] = (
    "Too many sign-in attempts failed. Try again in {minutes} minute(s), or ask an Admin to reset your password."
)

_BAD_CREDENTIALS: Final[str] = "The username or password is not right."


def access_http(exc: AccessError) -> HTTPException:
    """An `AccessError` in the error envelope."""
    return http_error(ACCESS_ERROR_STATUS.get(exc.code, 422), exc.code, exc.message)


def _sorted_roles(roles: frozenset[Role]) -> tuple[Role, ...]:
    return tuple(sorted(roles, key=lambda role: role.value))


def principal_view(principal: Principal, *, display_name: str | None = None) -> PrincipalView:
    """A `Principal` as the UI shows it."""
    return PrincipalView(
        user_id=principal.user_id,
        username=principal.username,
        display_name=display_name,
        roles=_sorted_roles(effective_roles(principal.roles)),
        kind=principal.kind,
    )


def user_view(user: UserRecord) -> UserView:
    """A `UserRecord` as an Admin sees it."""
    return UserView(
        user_id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        roles=_sorted_roles(user.roles),
        disabled=user.disabled,
        created_at=user.created_at,
        created_by=user.created_by,
        updated_at=user.updated_at,
    )


def _as_principal(user: UserRecord) -> Principal:
    return Principal(
        user_id=user.user_id, username=user.username, roles=effective_roles(user.roles), kind="user"
    )


def _roles_detail(roles: frozenset[Role]) -> str:
    return ",".join(role.value for role in _sorted_roles(roles))


# ---------------------------------------------------------------------------
# /auth
# ---------------------------------------------------------------------------
@router.post("/auth/login", response_model=LoginResponse, responses=_LOGIN_ERRORS, summary="Sign in")
def login(body: LoginRequest, request: Request, settings: SettingsDep) -> LoginResponse:
    """Exchange a username and password for a bearer token (DEC-711, DEC-720).

    Rate limited per username and per client address (Plan D M54, DEC-861): while either is locked
    the answer is **429 `LOGIN_LOCKED`** with `Retry-After`, and the password is not checked at all.
    The failure that starts a lock-out is audited with `lockout` naming the scope(s) it locked, and
    every refused attempt while locked is audited as `denied` with `reason_code: LOGIN_LOCKED`.
    """
    if settings.auth_mode != "local":
        set_audit_context(request, outcome="failed", details={"reason_code": "AUTH_OFF"})
        raise http_error(
            409,
            "AUTH_OFF",
            "Sign-in is switched off on this deployment: everyone acts as the local operator.",
        )
    users = get_user_store(request)
    throttle = get_login_throttle(request)
    account, address = account_key(body.username), client_address(request, settings)
    lock = throttle.locked(account, address)
    if lock is not None:
        known = users.find_by_username(body.username)
        set_audit_context(
            request,
            outcome="denied",
            object_id=None if known is None else known.user_id,
            details={"reason_code": "LOGIN_LOCKED", "lockout": lock.scope},
        )
        exc = http_error(429, "LOGIN_LOCKED", _LOCKED.format(minutes=-(-lock.retry_after_seconds // 60)))
        exc.headers = {"Retry-After": str(lock.retry_after_seconds)}
        raise exc
    try:
        user = users.check_credentials(body.username, body.password)
    except AccessError as exc:
        started = throttle.record_failure(account, address)
        details: dict[str, str | int | float | bool | None] = {"reason_code": exc.code}
        if started:
            details["lockout"] = ",".join(started)
        set_audit_context(request, outcome="failed", object_id=exc.user_id, details=details)
        raise http_error(401, "BAD_CREDENTIALS", _BAD_CREDENTIALS) from None
    throttle.record_success(account)
    issued = users.create_session(user.user_id)
    principal = _as_principal(user)
    view = principal_view(principal, display_name=user.display_name)
    # `after_hash` names the principal signed in as, not the response: the response holds the token,
    # and even a hash of a secret has no business in the audit trail.
    set_audit_context(request, actor=principal, object_id=user.user_id, after_hash=content_hash(view))
    return LoginResponse(token=issued.token, expires_at=issued.expires_at, principal=view)


@router.post("/auth/logout", status_code=204, response_class=Response, responses=_ERRORS, summary="Sign out")
def logout(request: Request, principal: PrincipalDep, settings: SettingsDep) -> Response:
    """Revoke the token this request was made with. With sign-in off there is nothing to revoke."""
    set_audit_context(request, object_id=principal.user_id)
    token = bearer_token(request.headers.get("authorization"))
    if settings.auth_mode == "local" and token is not None:
        get_user_store(request).revoke_session(token)
    return Response(status_code=204)


@router.get("/auth/me", response_model=MeResponse, responses=_ERRORS, summary="Who am I, and what may I do")
def me(request: Request, principal: PrincipalDep, settings: SettingsDep) -> MeResponse:
    """The caller, the sign-in mode, and one permission per declared route (plan M46)."""
    display_name: str | None = None
    if principal.kind == "user" and settings.auth_mode == "local":
        user = get_user_store(request).get_user(principal.user_id)
        display_name = user.display_name if user is not None else None
    permissions = []
    for (method, path), policy in sorted(all_policies().items(), key=lambda item: (item[0][1], item[0][0])):
        allowed = policy.role is None or principal.has(policy.role)
        permissions.append(
            PermissionView(
                method=method,
                path=path,
                action=policy.action,
                role=policy.role,
                allowed=allowed,
                reason=None if allowed else refusal_message(policy),
            )
        )
    return MeResponse(
        principal=principal_view(principal, display_name=display_name),
        auth_mode=settings.auth_mode,
        permissions=tuple(permissions),
    )


# ---------------------------------------------------------------------------
# /users (Admin)
# ---------------------------------------------------------------------------
@router.get("/users", response_model=UserListResponse, responses=_ERRORS, summary="List users")
def list_users(users: UserStoreDep) -> UserListResponse:
    """Every user, by username. Admin only."""
    return UserListResponse(users=tuple(user_view(user) for user in users.list_users()))


@router.post("/users", response_model=UserView, status_code=201, responses=_ERRORS, summary="Add a user")
def create_user(
    body: UserCreateRequest, request: Request, principal: PrincipalDep, users: UserStoreDep
) -> UserView:
    """Create a user with a password and at least one role. Admin only."""
    try:
        user = users.create_user(
            body.username,
            body.password,
            roles=body.roles,
            created_by=principal.user_id,
            display_name=body.display_name,
        )
    except AccessError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise access_http(exc) from None
    view = user_view(user)
    set_audit_context(
        request,
        object_id=user.user_id,
        after_hash=content_hash(view),
        details={"target_user_id": user.user_id, "roles": _roles_detail(user.roles)},
    )
    return view


@router.patch("/users/{user_id}", response_model=UserView, responses=_ERRORS, summary="Change a user")
def update_user(
    user_id: str, body: UserUpdateRequest, request: Request, principal: PrincipalDep, users: UserStoreDep
) -> UserView:
    """Change roles, display name or disabled. Roles or disabling revoke the user's sign-ins.

    An Admin cannot add a role to their own account (409 `SELF_ROLE_GRANT`, DEC-723): Admin does not
    include Approver (DEC-703), and a separation of duties the Admin could grant themselves is none.
    Removing one of your own roles is allowed (the last-Admin rule still applies).
    """
    before = users.get_user(user_id)
    if before is None:
        raise http_error(404, "USER_NOT_FOUND", "There is no user with that id.")
    roles_changing = body.roles is not None and frozenset(body.roles) != before.roles
    if (
        roles_changing
        and body.roles is not None
        and principal.kind == "user"
        and principal.user_id == user_id
        and not effective_roles(frozenset(body.roles)) <= effective_roles(before.roles)
    ):
        set_audit_context(
            request,
            action="users.roles_change",
            details={"target_user_id": user_id, "reason_code": "SELF_ROLE_GRANT"},
        )
        raise http_error(
            409,
            "SELF_ROLE_GRANT",
            "You cannot give yourself a role. Another Admin has to grant it.",
            path="roles",
        )
    changed = [
        name
        for name, value in (
            ("display_name", body.display_name),
            ("roles", body.roles),
            ("disabled", body.disabled),
        )
        if value is not None
    ]
    details: dict[str, str | int | float | bool | None] = {
        "target_user_id": user_id,
        "setting": ",".join(changed),
    }
    if roles_changing and body.roles is not None:
        details["roles"] = _roles_detail(frozenset(body.roles))
    set_audit_context(
        request,
        action="users.roles_change" if roles_changing else None,
        before_hash=content_hash(user_view(before)),
        details=details,
    )
    try:
        after = users.update_user(
            user_id, display_name=body.display_name, roles=body.roles, disabled=body.disabled
        )
    except AccessError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise access_http(exc) from None
    view = user_view(after)
    set_audit_context(request, after_hash=content_hash(view))
    return view


@router.post(
    "/users/{user_id}/password",
    status_code=204,
    response_class=Response,
    responses=_ERRORS,
    summary="Change a password",
)
def change_password(
    user_id: str, body: PasswordChangeRequest, request: Request, principal: PrincipalDep, users: UserStoreDep
) -> Response:
    """An Admin may set anyone's password; anybody may change their own, giving the current one.

    An Admin's reset of someone else's password is audited as its own action,
    `users.password_reset`, with the target's roles, and it signs the target out everywhere, so a
    takeover of an Approver's account is neither silent to them nor hidden in the trail (DEC-723).
    """
    own = principal.user_id == user_id
    if not own and not principal.has(Role.ADMIN):
        raise http_error(403, "ROLE_REQUIRED", "Only an Admin can change another user's password.")
    target = users.get_user(user_id)
    if target is None:
        raise http_error(404, "USER_NOT_FOUND", "There is no user with that id.")
    set_audit_context(request, details={"target_user_id": user_id})
    if not own:
        set_audit_context(
            request, action="users.password_reset", details={"roles": _roles_detail(target.roles)}
        )
    if own and principal.kind == "user":
        if not body.current_password:
            raise http_error(422, "CURRENT_PASSWORD_REQUIRED", "Give your current password to change it.")
        try:
            users.check_credentials(target.username, body.current_password)
        except AccessError:
            set_audit_context(request, details={"reason_code": "CURRENT_PASSWORD_WRONG"})
            raise http_error(422, "CURRENT_PASSWORD_WRONG", "The current password is not right.") from None
    try:
        users.set_password(user_id, body.password)
    except AccessError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise access_http(exc) from None
    return Response(status_code=204)
