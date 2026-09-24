"""Access control and the audit trail, installed on every app `create_app` builds (Phase 4b M46/M47).

`install_access` is registered in `api.main.PHASE_APP_HOOKS` and runs before any router is included,
so the two things it adds reach every route - Phase 1's, every branch's and every Phase 4b router's -
without editing one of them (DEC-700):

**`enforce_access`, a global dependency** (DEC-717). It reads the route FastAPI matched
(`request.scope["route"]`) and looks up `api.access_policy.policy_for(method, path)`:

* no policy: **403 `ROUTE_HAS_NO_POLICY`** - fail closed. A route somebody forgot to declare is
  unreachable, not open; `tests/unit/production/test_route_policies.py` makes it a red test too.
* `role=None`: public; nothing is checked (the probe, signing in).
* otherwise the principal comes from the identity provider `settings.auth_mode` selects
  (`engine.access.identity`): `off` is `LOCAL_OPERATOR`, `local` is a bearer session. No valid
  session is **401 `AUTH_REQUIRED`** with `WWW-Authenticate: Bearer`; a principal without the role
  is **403 `ROLE_REQUIRED`** with the sentence the UI shows ("Only an Approver can approve a
  champion.").
* `auth_mode=off` on `env=prod` answers every non-public route with **503 `AUTH_NOT_CONFIGURED`**:
  the settings deliberately do not refuse to boot, so this is where "off is not a production
  answer" is enforced - fail closed, not fail to start (DEC-702). Off on `dev`/`staging` is logged
  as a warning once per app.

A dependency, not middleware, because only after routing is the route template known, and the
policy is keyed by template. The consequence worth knowing: FastAPI parses a request body before it
solves dependencies, so a malformed JSON body gets its 422 before the sign-in check. Nothing runs
and nothing is disclosed by that 422 beyond "this was not JSON".

**`AuditMiddleware`, pure ASGI** (DEC-718). It writes **exactly one** `AuditEvent` for every
mutating request (POST/PUT/PATCH/DELETE) and for every GET whose policy says `audit_reads`,
whatever happened: 2xx/3xx is `success`, 401/403 `denied`, any other 4xx/5xx or an exception
`failed`. A route never writes its own event; it enriches this one with `set_audit_context`. The
event carries the actor (the principal, or `anonymous`), the policy's action and object (the path
parameter the policy names), a request id, and `after_hash`: the SHA-256 of the exact bytes of a
2xx JSON response, hashed chunk by chunk as they pass - nothing is buffered, so a streaming download
is never held in memory and never delayed. Pure ASGI rather than `BaseHTTPMiddleware`, which wraps
the body in a second stream and breaks both of those.

The request id is a fresh uuid4, or an incoming `X-Request-ID` when it is a short safe token
(8-64 of `[A-Za-z0-9._-]`); either way it is returned as `X-Request-ID` and stored on
`request.state.request_id`, so a person reporting a problem can hand over the one string that
finds their event. An **anonymous** caller's id is not trusted as the event's `request_id` - anyone
could send the same one twice - so its event gets a minted id and keeps the caller's under
`details.client_request_id` (DEC-724).

**Anonymous events are rate-limited per client address** (DEC-724): nobody signed in can make the
undeletable table grow without bound. Past `ANONYMOUS_EVENTS_PER_MINUTE` in a minute from one
address, further anonymous events are dropped and counted on `app.state.audit_events_dropped`, with
one WARNING per address and minute. A HEAD that answered 405 read nothing and is not audited.

**If writing the event fails, the request still gets its response** (DEC-719). The failure is
logged at ERROR with the action and the exception's class - never a value - and counted on
`app.state.audit_write_failures`. The alternative, failing the request, would make the audit
database a single point of failure for scoring and approvals, and it could not be done honestly
anyway: by the time the event is written the response has been sent. A deployment alarms on the
ERROR line; the gap it leaves is visible, not silent.

The dependency providers at the foot of this module are the contract the other Phase 4b routers
build on (`get_audit_log`, `get_user_store`, `get_identity`, `current_principal`,
`set_audit_context`, `platform_data_dir`, and `record_event` re-exported). Like `api/deps.py`, each
is built on first use and cached on `app.state`, and `create_app(data_dir=…)` wins, so a test's
audit trail lands in its `tmp_path`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Final

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from sqlalchemy.engine import Engine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.access_policy import MUTATING_METHODS, RoutePolicy, policy_for, refusal_message
from api.deps import get_settings
from api.routes.uploads import http_error
from engine.access.identity import DisabledIdentity, IdentityProvider, LocalIdentity
from engine.access.roles import LOCAL_OPERATOR, Principal, Role
from engine.access.throttle import LoginThrottle
from engine.access.users import SqlUserStore, UserStore
from engine.audit.events import AuditEvent, AuditLog
from engine.audit.store import (
    ANONYMOUS_ACTOR_ID,
    ANONYMOUS_ACTOR_KIND,
    SqlAuditLog,
    new_event_id,
    record_event,
)
from engine.config import UseCaseConfig
from engine.platform_db import platform_engine
from engine.settings import ENV_VARS, Settings
from engine.utils.time import utc_now

__all__ = [
    "REQUEST_ID_HEADER",
    "AuditLogDep",
    "AuditMiddleware",
    "IdentityDep",
    "PrincipalDep",
    "UserStoreDep",
    "client_address",
    "current_principal",
    "enforce_access",
    "get_audit_log",
    "get_identity",
    "get_login_throttle",
    "get_platform_engine",
    "get_user_store",
    "install_access",
    "platform_data_dir",
    "record_event",
    "require_roles_for_run_overrides",
    "set_audit_context",
]

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER: Final[str] = "X-Request-ID"

_SAFE_REQUEST_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
"""An incoming request id is honoured only when it looks like one; anything else is replaced."""

_SAFE_OBJECT_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
"""A path parameter becomes `object_id` only when it looks like an identifier the API minted.

Anything else - an address, a sentence, a value somebody typed into a URL - is withheld, because
the audit log must never hold a data value (DEC-705) and a path parameter is whatever was sent."""

_STATE_PRINCIPAL: Final[str] = "principal"
_STATE_POLICY: Final[str] = "access_policy"
_STATE_ROUTE: Final[str] = "access_route"
_STATE_AUDIT: Final[str] = "audit_context"
_STATE_REQUEST_ID: Final[str] = "request_id"

_STATE_CLIENT_REQUEST_ID: Final[str] = "client_request_id"

_AUDITED_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})
_LOCK: Final[threading.Lock] = threading.Lock()

ANONYMOUS_EVENTS_PER_MINUTE: Final[int] = 120
"""Audit events an unauthenticated client address may cause per minute before they are dropped."""

_LIMITER_MAX_ADDRESSES: Final[int] = 10_000


class AnonymousEventLimiter:
    """A fixed one-minute window per client address, for events with no principal (DEC-724)."""

    def __init__(self, per_minute: int = ANONYMOUS_EVENTS_PER_MINUTE) -> None:
        self._per_minute = per_minute
        self._lock = threading.Lock()
        self._window = -1
        self._counts: dict[str, int] = {}

    def allow(self, address: str, now: float) -> tuple[bool, bool]:
        """`(allowed, first_refusal)` for one more event from `address` at `now` (epoch seconds)."""
        window = int(now // 60)
        with self._lock:
            if window != self._window or len(self._counts) > _LIMITER_MAX_ADDRESSES:
                self._window, self._counts = window, {}
            count = self._counts.get(address, 0) + 1
            self._counts[address] = count
        return count <= self._per_minute, count == self._per_minute + 1


# ---------------------------------------------------------------------------
# Providers (cached on app.state, like api/deps.py)
# ---------------------------------------------------------------------------
def _explicit_data_dir(request: Request) -> Path | None:
    value = getattr(request.app.state, "data_dir", None)
    return None if value is None else Path(str(value))


def platform_data_dir(request: Request) -> Path | None:
    """The local directory Phase 4b's files live in, or None on a deployment with no local disk.

    `create_app(data_dir=…)` first, exactly as `api/deps.py` decides; then `settings.data_dir` when
    artefacts are on the local filesystem; None when they are in S3.
    """
    explicit = _explicit_data_dir(request)
    if explicit is not None:
        return explicit
    settings = get_settings(request)
    return settings.data_dir if settings.storage_backend == "local" else None


def _cached(request: Request, slot: str, build: Any) -> Any:
    state = request.app.state
    existing = getattr(state, slot, None)
    if existing is not None:
        return existing
    fresh = build()  # built outside the lock: `get_settings` takes api.deps' own lock
    with _LOCK:
        existing = getattr(state, slot, None)
        if existing is None:
            setattr(state, slot, fresh)
            return fresh
    return existing


def get_audit_log(request: Request) -> AuditLog:
    """The audit log: `audit_events` in `platform.db` locally, in Postgres on a deployment."""

    def build() -> AuditLog:
        return SqlAuditLog(platform_engine(get_settings(request), data_dir=_explicit_data_dir(request)))

    log: AuditLog = _cached(request, "audit_log", build)
    return log


def get_platform_engine(request: Request) -> Engine:
    """The platform database's engine, as `get_audit_log` builds it (Plan D: approval decisions)."""

    def build() -> Engine:
        return platform_engine(get_settings(request), data_dir=_explicit_data_dir(request))

    engine: Engine = _cached(request, "platform_engine", build)
    return engine


def get_user_store(request: Request) -> UserStore:
    """The built-in user store. A test may put a faster-hashing `SqlUserStore` on `app.state.user_store`."""

    def build() -> UserStore:
        settings = get_settings(request)
        return SqlUserStore(
            platform_engine(settings, data_dir=_explicit_data_dir(request)),
            session_ttl_seconds=settings.auth_session_ttl_seconds,
        )

    store: UserStore = _cached(request, "user_store", build)
    return store


def get_identity(request: Request) -> IdentityProvider:
    """The identity provider `settings.auth_mode` selects (the M50 provider will be a third branch)."""

    def build() -> IdentityProvider:
        settings = get_settings(request)
        if settings.auth_mode == "local":
            return LocalIdentity(get_user_store(request))
        return DisabledIdentity()

    identity: IdentityProvider = _cached(request, "identity", build)
    return identity


def get_login_throttle(request: Request) -> LoginThrottle:
    """The sign-in rate limiter (Plan D M54, DEC-861). A test may put one with its own clock on `app.state`."""

    def build() -> LoginThrottle:
        settings = get_settings(request)
        return LoginThrottle(
            max_failures_per_account=settings.login_max_failures_per_account,
            max_failures_per_address=settings.login_max_failures_per_address,
            window_seconds=settings.login_failure_window_seconds,
            lockout_seconds=settings.login_lockout_seconds,
        )

    throttle: LoginThrottle = _cached(request, "login_throttle", build)
    return throttle


def client_address(request: Request, settings: Settings) -> str:
    """The address a request came from, for rate limiting (DEC-861).

    The peer address, unless `trusted_proxy_hops` says N proxies (a load balancer) sit in front of
    the API: then the N-th `X-Forwarded-For` entry from the right, the one the outermost trusted proxy
    appended. Entries further left are whatever the client wrote and are never used. A request with
    fewer entries than trusted hops did not come through the proxies and is counted by its peer.

    Every `X-Forwarded-For` header line is read, in order, and joined before splitting (DEC-867): a
    client may send its own line, and a proxy may add a second line rather than extend the first, so
    reading only the first line would count the client's forged value instead of the proxy's.
    The deployed ALB is one trusted hop; `infra/compute.py` publishes `trusted_proxy_hops=1`.
    """
    peer = request.client.host if request.client is not None else "unknown"
    hops = settings.trusted_proxy_hops
    if hops == 0:
        return peer
    forwarded = [
        part.strip()
        for line in request.headers.getlist("x-forwarded-for")
        for part in line.split(",")
        if part.strip()
    ]
    return forwarded[-hops] if len(forwarded) >= hops else peer


def current_principal(request: Request) -> Principal:
    """Who this request acts as, as `enforce_access` resolved it. 401 on a public route (nobody is)."""
    principal = getattr(request.state, _STATE_PRINCIPAL, None)
    if not isinstance(principal, Principal):
        raise _auth_required()
    return principal


def set_audit_context(
    request: Request,
    *,
    object_id: str | None = None,
    object_type: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    details: Mapping[str, str | int | float | bool | None] | None = None,
    action: str | None = None,
    outcome: str | None = None,
    actor: Principal | None = None,
) -> None:
    """Enrich the one audit event the middleware writes for this request.

    Each given field replaces what the middleware would have derived; `details` are merged (keys
    must be in `DETAIL_KEYS`). `action` lets a route name a more specific action than its policy
    (`users.roles_change` for a `PATCH /users/{id}` that changed roles), `outcome` a more specific
    outcome than the status code (`failed` for a wrong password answered with 401), and `actor` the
    principal of a public route that has just established one (a successful sign-in).
    """
    state: dict[str, Any] = request.scope.setdefault("state", {})
    context: dict[str, Any] = state.setdefault(_STATE_AUDIT, {})
    for key, value in (
        ("object_id", object_id),
        ("object_type", object_type),
        ("before_hash", before_hash),
        ("after_hash", after_hash),
        ("action", action),
        ("outcome", outcome),
        ("actor", actor),
    ):
        if value is not None:
            context[key] = value
    if details:
        context.setdefault("details", {}).update(details)


def require_roles_for_run_overrides(
    request: Request, *, use_case: UseCaseConfig, resolved: UseCaseConfig
) -> None:
    """Refuse a run whose overrides loosen a setting that belongs to a role the caller lacks (DEC-723).

    `governance.approval_required` is on the run form, so `POST /runs` - Analyst - could send
    `false` and have the register stage crown its own model with no Approver involved. A run may
    make approval *stricter*; turning it off where the use case requires it takes an Approver, the
    role that would otherwise have had to approve. `use_case` and `resolved` are the use case's own
    configuration and the run's merged one.
    """
    if use_case.governance.approval_required and not resolved.governance.approval_required:
        principal = current_principal(request)
        if not principal.has(Role.APPROVER):
            set_audit_context(request, details={"reason_code": "APPROVAL_OVERRIDE_REFUSED"})
            raise http_error(
                403,
                "ROLE_REQUIRED",
                "Only an Approver can turn off approval for a run.",
                path="governance.approval_required",
            )


AuditLogDep = Annotated[AuditLog, Depends(get_audit_log)]
UserStoreDep = Annotated[UserStore, Depends(get_user_store)]
IdentityDep = Annotated[IdentityProvider, Depends(get_identity)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------
def _auth_required() -> HTTPException:
    exc = http_error(401, "AUTH_REQUIRED", "Sign in to do this.")
    exc.headers = {"WWW-Authenticate": "Bearer"}
    return exc


def route_path(scope: Scope) -> str | None:
    """The template of the route FastAPI matched, e.g. `/models/{model_id}/approve`, or None.

    FastAPI >= 0.140 keeps included routers unflattened and puts the *original* `APIRoute` in
    `scope["route"]`; its `path` lacks any prefix given to `include_router`. The effective path is
    on the route context FastAPI stores beside it, which is preferred when present so a router
    mounted with a prefix is still looked up under the path the policy table (and the test that
    enumerates the app) spells.
    """
    route = scope.get("route")
    if not isinstance(route, APIRoute):
        return None
    context = scope.get("fastapi", {}).get("effective_route_context")
    path = getattr(context, "path", None)
    return path if isinstance(path, str) and path else route.path


def policy_method(method: str) -> str:
    """The method a policy is declared under: HEAD is a GET without a body and shares its policy."""
    upper = method.upper()
    return "GET" if upper == "HEAD" else upper


def _warn_if_open(app: Any, settings: Settings) -> None:
    """Log once per app that access control is off somewhere other than a laptop."""
    if settings.auth_mode != "off" or settings.env == "local" or getattr(app.state, "auth_off_warned", False):
        return
    app.state.auth_off_warned = True
    if settings.env == "prod":
        logger.error(
            "access control is off on env=prod; every non-public route answers 503 until %s is set",
            ENV_VARS["auth_mode"],
        )
    else:
        logger.warning(
            "access control is off on env=%s: every request acts as the local operator with every role",
            settings.env,
        )


def enforce_access(request: Request) -> None:
    """The global dependency: find the route's policy and check the principal against it (DEC-717)."""
    path = route_path(request.scope)
    policy = None if path is None else policy_for(policy_method(request.method), path)
    state = request.state
    setattr(state, _STATE_ROUTE, path)
    if policy is None:
        raise http_error(
            403,
            "ROUTE_HAS_NO_POLICY",
            "This action is not available: it has no access rule. Report this as a defect.",
        )
    setattr(state, _STATE_POLICY, policy)
    if policy.role is None:
        return
    settings = get_settings(request)
    if settings.auth_mode == "off":
        _warn_if_open(request.app, settings)
        if settings.env == "prod":
            raise http_error(
                503,
                "AUTH_NOT_CONFIGURED",
                "Sign-in is switched off, and a production deployment does not answer without it.",
                path=ENV_VARS["auth_mode"],
            )
        principal: Principal | None = LOCAL_OPERATOR
    else:
        principal = get_identity(request).authenticate(request.headers.get("authorization"))
    if principal is None:
        raise _auth_required()
    setattr(state, _STATE_PRINCIPAL, principal)
    if not principal.has(policy.role):
        raise http_error(403, "ROLE_REQUIRED", refusal_message(policy))


# ---------------------------------------------------------------------------
# The audit middleware
# ---------------------------------------------------------------------------
def _outcome(status: int) -> str:
    if status < 400:
        return "success"
    if status in (401, 403):
        return "denied"
    return "failed"


def _request_id(scope: Scope) -> tuple[str, bool]:
    """`(request id, whether the client supplied it)`."""
    for name, value in scope.get("headers", ()):
        if name == b"x-request-id":
            candidate = bytes(value).decode("latin-1")
            if _SAFE_REQUEST_ID.fullmatch(candidate):
                return candidate, True
            break
    return new_event_id(), False


def _policy_of(scope: Scope, state: Mapping[str, Any]) -> tuple[str | None, RoutePolicy | None]:
    """The route template and policy, from `enforce_access` when it ran, else from the match."""
    policy = state.get(_STATE_POLICY)
    if isinstance(policy, RoutePolicy):
        return state.get(_STATE_ROUTE), policy
    path = route_path(scope)
    return path, (None if path is None else policy_for(policy_method(str(scope["method"])), path))


class AuditMiddleware:
    """One audit event per mutating or `audit_reads` request, whatever its status (DEC-718, DEC-719)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request through, stamping `X-Request-ID` and hashing a 2xx JSON body as it goes."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id, supplied = _request_id(scope)
        state: dict[str, Any] = scope.setdefault("state", {})
        state[_STATE_REQUEST_ID] = request_id
        if supplied:
            state[_STATE_CLIENT_REQUEST_ID] = request_id
        method = str(scope["method"]).upper()
        progress: dict[str, Any] = {"status": None, "hasher": None}

        def audited() -> bool:
            if method in MUTATING_METHODS:
                return True
            if method not in _AUDITED_READ_METHODS:
                return False
            _, policy = _policy_of(scope, state)
            return policy is not None and policy.audit_reads

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                progress["status"] = status
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.lower().encode("latin-1"), request_id.encode("latin-1")))
                message["headers"] = headers
                content_type = next(
                    (value for name, value in headers if name.lower() == b"content-type"), b""
                )
                if 200 <= status < 300 and content_type.lower().startswith(b"application/json") and audited():
                    progress["hasher"] = hashlib.sha256()
            elif message["type"] == "http.response.body" and progress["hasher"] is not None:
                progress["hasher"].update(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if audited():
                await self._write(scope, state, request_id, 500, None)
            raise
        status = progress["status"] if progress["status"] is not None else 500
        if audited() and not (method == "HEAD" and status == 405):
            hasher = progress["hasher"]
            await self._write(
                scope, state, request_id, status, None if hasher is None else hasher.hexdigest()
            )

    async def _write(
        self, scope: Scope, state: Mapping[str, Any], request_id: str, status: int, body_hash: str | None
    ) -> None:
        """Build the event and append it off the event loop; a failure is logged and counted, never raised."""
        action = "?"
        try:
            event = self._event(scope, state, request_id, status, body_hash)
            action = event.action
            if event.actor_id == ANONYMOUS_ACTOR_ID and not _anonymous_allowed(scope):
                return
            request = Request(scope)
            await anyio.to_thread.run_sync(partial(_append, request, event))
        except Exception as exc:
            app = scope.get("app")
            if app is not None:
                app.state.audit_write_failures = int(getattr(app.state, "audit_write_failures", 0)) + 1
            logger.error("audit event could not be written action=%s error=%s", action, type(exc).__name__)

    @staticmethod
    def _event(
        scope: Scope, state: Mapping[str, Any], request_id: str, status: int, body_hash: str | None
    ) -> AuditEvent:
        method = str(scope["method"]).upper()
        path, policy = _policy_of(scope, state)
        context: Mapping[str, Any] = state.get(_STATE_AUDIT, {})
        actor = context.get("actor") or state.get(_STATE_PRINCIPAL)
        if policy is not None:
            default_action = policy.action
        else:
            default_action = "request.unmatched" if path is None else "request.no_policy"
        object_id = context.get("object_id")
        if object_id is None and policy is not None and policy.object_param:
            raw = scope.get("path_params", {}).get(policy.object_param)
            object_id = raw if isinstance(raw, str) and _SAFE_OBJECT_ID.fullmatch(raw) else None
        details: dict[str, str | int | float | bool | None] = dict(context.get("details", {}))
        details.update({"method": method, "route": path, "status_code": status})
        if not isinstance(actor, Principal) and state.get(_STATE_CLIENT_REQUEST_ID) == request_id:
            # an anonymous caller chose this id; it may not name the event (DEC-724)
            details["client_request_id"] = request_id
            request_id = new_event_id()
        return AuditEvent(
            event_id=new_event_id(),
            occurred_at=utc_now(),
            actor_id=actor.user_id if isinstance(actor, Principal) else ANONYMOUS_ACTOR_ID,
            actor_kind=actor.kind if isinstance(actor, Principal) else ANONYMOUS_ACTOR_KIND,
            action=context.get("action") or default_action,
            object_type=context.get("object_type") or (policy.object_type if policy is not None else None),
            object_id=object_id,
            before_hash=context.get("before_hash"),
            after_hash=context.get("after_hash") or (body_hash if 200 <= status < 300 else None),
            request_id=request_id,
            outcome=context.get("outcome") or _outcome(status),
            details=details,
        )


def _append(request: Request, event: AuditEvent) -> None:
    get_audit_log(request).append(event)


def _anonymous_allowed(scope: Scope) -> bool:
    """Whether one more anonymous event from this client address may be written (DEC-724)."""
    app = scope.get("app")
    if app is None:
        return True
    limiter = getattr(app.state, "anonymous_event_limiter", None)
    if not isinstance(limiter, AnonymousEventLimiter):
        return True
    client = scope.get("client")
    address = str(client[0]) if client else "unknown"
    allowed, first_refusal = limiter.allow(address, time.time())
    if not allowed:
        app.state.audit_events_dropped = int(getattr(app.state, "audit_events_dropped", 0)) + 1
        if first_refusal:
            logger.warning(
                "anonymous audit events over %d a minute from one address are being dropped",
                ANONYMOUS_EVENTS_PER_MINUTE,
            )
    return allowed


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------
def install_access(app: FastAPI) -> None:
    """`PHASE_APP_HOOKS` entry: the global dependency, the audit middleware, the off-warning."""
    app.router.dependencies.append(Depends(enforce_access))
    app.add_middleware(AuditMiddleware)
    app.state.audit_write_failures = 0
    app.state.audit_events_dropped = 0
    app.state.anonymous_event_limiter = AnonymousEventLimiter()
    settings = getattr(app.state, "settings", None)
    if isinstance(settings, Settings):
        _warn_if_open(app, settings)
    else:
        # The deployed `api.main:app` is built with no settings; resolve them at startup so an open
        # prod deployment says so at once, not at its first non-public request (DEC-725).
        app.router.on_startup.append(partial(_warn_at_startup, app))


def _warn_at_startup(app: FastAPI) -> None:
    """Startup: resolve the settings the way `api.deps.get_settings` does, then warn if access is off."""
    try:
        settings = get_settings(Request({"type": "http", "app": app, "headers": [], "method": "GET"}))
    except Exception as exc:  # the first request will report a settings problem properly
        logger.error("access control could not read the settings at startup error=%s", type(exc).__name__)
        return
    _warn_if_open(app, settings)
