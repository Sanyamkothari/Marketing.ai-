"""The `/mcp` endpoint: sign-in, then the SDK's streamable HTTP transport, stateless (Plan K M133, DEC-1404 (c)).

`install_mcp` is the hook `api.main` registers. With `mcp.enabled` off it returns at once - nothing mounted, the
SDK never imported. On, it adds one plain route, `/mcp`, **POST only** - the stateless server has no GET stream
and no session to delete, so GET and DELETE are answered 405 by the router before sign-in or the transport runs,
and no request can hold a connection open outside the per-session limits - and starts the SDK's `StreamableHTTPSessionManager` (`stateless=True`, JSON responses) with the app's lifespan, a
fresh manager each time the app starts. Stateless means no session is kept between requests, so the server sits
behind the existing load balancer with no sticky sessions; 2026-07-28 clients send one self-contained request
per POST, and clients of the 2025 revisions are served statelessly too.

**Sign-in is required, always.** Before the transport sees a byte:

* sign-in switched off (`MARKETING_AI_AUTH_MODE=off`): **503 `AUTH_NOT_CONFIGURED`** - the verifier never acts
  as the local operator, even on a laptop;
* no valid bearer session: **401 `AUTH_REQUIRED`** with `WWW-Authenticate: Bearer`;
* otherwise the principal's roles give its scopes (`api.mcp.scopes`); every signed-in person holds
  `marketing-ai:read`.

The bearer token is the product's own session (`POST /auth/login`), resolved by the deployment's identity
provider exactly as `api.access.enforce_access` resolves it; the endpoint is a resource server and issues
nothing.

**The client id.** A call is audited, and recorded on the ledger, as `mcp:<name>`: the name the client declared
in the request (`clientInfo`, which a 2026-07-28 client sends with every request). A client of the 2025
revisions declares it once, in an `initialize` POST of its own that the stateless server does not keep, so its
later calls are named from their HTTP `User-Agent` instead (`mcp:ua-<agent>`), never left unnamed.

The audit middleware's event for the HTTP request names the principal and the action `mcp.request`;
each tool call adds its own `mcp.tool_call` event (`api.mcp.server`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import anyio
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from api.access_policy import RoutePolicy, register
from engine.access.roles import Principal, Role

if TYPE_CHECKING:
    from mcp.server.context import ServerRequestContext
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from api.mcp.scopes import McpCaller
    from api.mcp.server import Verifier

__all__ = ["MCP_PATH", "POLICIES", "McpEndpoint", "install_mcp"]

logger = logging.getLogger(__name__)

MCP_PATH: Final[str] = "/mcp"
_CALLER: Final[str] = "mcp_caller"

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("POST", MCP_PATH): RoutePolicy(
        role=Role.VIEWER, action="mcp.request", purpose="call the verifier over MCP", object_type="mcp"
    ),
}
"""The endpoint's row of the access table. The endpoint is a plain route (the transport is ASGI), so it checks
sign-in itself; per tool, the tool's own `RoutePolicy` decides (`api.mcp.scopes.scope_for`)."""

register(POLICIES)


@dataclass(frozen=True)
class _SignedIn:
    principal: Principal
    scopes: frozenset[str]
    token_key: str


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"detail": {"code": code, "message": message}}, headers=headers
    )


def _request_for(app: FastAPI) -> Request:
    """A request carrying only the app, for the dependency providers that cache on `app.state`."""
    return Request({"type": "http", "app": app, "headers": [], "method": "GET", "path": MCP_PATH})


def caller_of(ctx: ServerRequestContext[Any, Any]) -> McpCaller:
    """Who an HTTP request is: the sign-in the endpoint resolved, and the name the client declared (or, for a
    client of the 2025 revisions, which declared it in an earlier request, its `User-Agent`)."""
    from api.mcp.scopes import McpCaller, client_label

    request = ctx.request
    state = getattr(request, "scope", {}).get("state", {}) if request is not None else {}
    signed_in = state.get(_CALLER)
    if not isinstance(signed_in, _SignedIn):  # never: the endpoint sets it before the transport runs
        raise RuntimeError("an MCP request reached the server without a sign-in")
    params = ctx.session.client_params
    declared = params.client_info.name if params is not None and params.client_info is not None else None
    if not declared and request is not None:
        agent = getattr(request, "headers", {}).get("user-agent")
        declared = f"ua-{agent}" if agent else None
    return McpCaller(
        principal=signed_in.principal,
        scopes=signed_in.scopes,
        token_key=signed_in.token_key,
        client_id=client_label(declared),
    )


class McpEndpoint:
    """The ASGI app behind `/mcp`: sign-in, then the session manager (running only while the app is)."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self.manager: StreamableHTTPSessionManager | None = None
        self.verifier: Verifier | None = None

    def build_verifier(self) -> Verifier:
        """The verifier over this app's storage, platform database, ledger and configuration."""
        from api.access import get_audit_log, get_platform_engine, platform_data_dir
        from api.deps import get_config_root, get_settings, get_storage
        from api.mcp.config import load_mcp_config
        from api.mcp.limits import RateLimiter
        from api.mcp.server import Verifier
        from api.routes.campaigns import get_campaign_store
        from engine.agentic.ledger import signer_for_data_dir
        from engine.agentic.service import agentic_context
        from engine.agentic.tools import default_registry

        request = _request_for(self._app)
        root = get_config_root(request)
        settings = load_mcp_config(root).mcp

        def context_for(caller: McpCaller) -> Any:
            app_settings = get_settings(request)
            return agentic_context(
                storage=get_storage(request),
                principal=caller.principal,
                config_root=root,
                platform_engine=get_platform_engine(request),
                signer=signer_for_data_dir(platform_data_dir(request) or app_settings.data_dir),
                settings=app_settings,
                campaigns=get_campaign_store(request),
                actor=f"agent:{caller.client_id}",
            )

        return Verifier(
            registry=default_registry(),
            settings=settings,
            context_for=context_for,
            limiter=RateLimiter(
                calls_per_minute=settings.rate_limit.calls_per_minute,
                concurrent_calls=settings.rate_limit.concurrent_calls,
            ),
            audit_log=get_audit_log(request),
        )

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Start a fresh verifier and session manager for this run of the app; stop them with it."""
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        from api.mcp.server import build_server

        verifier = await anyio.to_thread.run_sync(self.build_verifier)
        manager = StreamableHTTPSessionManager(
            app=build_server(verifier, caller_of), json_response=True, stateless=True
        )
        async with manager.run():
            self.verifier, self.manager = verifier, manager
            try:
                yield
            finally:
                self.verifier, self.manager = None, None

    def _sign_in(self, request: Request) -> _SignedIn | JSONResponse:
        from api.access import get_identity
        from api.deps import get_settings
        from api.mcp.scopes import SCOPE_READ, scopes_for, token_key
        from engine.access.identity import bearer_token

        if get_settings(request).auth_mode == "off":
            return _error(
                503,
                "AUTH_NOT_CONFIGURED",
                "The MCP server needs sign-in, and sign-in is switched off on this deployment.",
            )
        header = request.headers.get("authorization")
        principal = get_identity(request).authenticate(header)
        token = bearer_token(header)
        if principal is None or token is None:
            return _error(401, "AUTH_REQUIRED", "Sign in to do this.", {"WWW-Authenticate": "Bearer"})
        scopes = scopes_for(principal)
        if SCOPE_READ not in scopes:
            return _error(403, "MCP_SCOPE_DENIED", "This sign-in holds no scope on the MCP server.")
        return _SignedIn(principal=principal, scopes=scopes, token_key=token_key(token))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from api.access import set_audit_context

        request = Request(scope, receive)
        set_audit_context(request, action="mcp.request", object_type="mcp", details={"request_kind": "mcp"})
        signed_in = await anyio.to_thread.run_sync(self._sign_in, request)
        if isinstance(signed_in, JSONResponse):
            await signed_in(scope, receive, send)
            return
        set_audit_context(request, actor=signed_in.principal)
        scope.setdefault("state", {})[_CALLER] = signed_in
        manager = self.manager
        if manager is None:
            await _error(503, "MCP_NOT_STARTED", "The MCP server is not running yet; try again in a moment.")(
                scope, receive, send
            )
            return
        await manager.handle_request(scope, receive, send)


def install_mcp(app: FastAPI) -> None:
    """Mount `/mcp` and start its manager with the app (`api.main` calls it only when `mcp.enabled` is on)."""
    endpoint = McpEndpoint(app)
    app.state.mcp_endpoint = endpoint
    app.router.routes.append(Route(MCP_PATH, endpoint=endpoint, methods=["POST"]))
    inner: Callable[[Any], AbstractAsyncContextManager[Any]] = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(owner: Any) -> AsyncIterator[Any]:
        async with inner(owner) as state, endpoint.running():
            yield state

    app.router.lifespan_context = lifespan
