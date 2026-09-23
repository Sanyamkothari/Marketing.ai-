"""Shared helpers of the M46/M47 tests: the live route list, apps with sign-in on, users and tokens.

Imported by plain module import (never through a conftest), as the rest of the suite does. The route
list is read from a freshly built `create_app()` - never from a hand-kept list - so a route another
Phase 4b router adds later is covered by every "for every route" test here the day it lands.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.access_policy import MUTATING_METHODS, RoutePolicy, policy_for
from api.main import create_app
from engine.access.roles import Role
from engine.access.users import SqlUserStore
from engine.audit.events import AuditQuery
from engine.audit.store import SqlAuditLog
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings

PASSWORD: Final[str] = "correct horse battery staple"
FAST_ITERATIONS: Final[int] = 300

ACCESS_CODES: Final[frozenset[str]] = frozenset(
    {"AUTH_REQUIRED", "ROLE_REQUIRED", "ROUTE_HAS_NO_POLICY", "AUTH_NOT_CONFIGURED"}
)
"""The refusals `enforce_access` answers with; a 403 carrying any other code came from the route."""

BODIES: Final[dict[tuple[str, str], Any]] = {
    # Would otherwise check the configured AWS identity against the network; an unknown use case
    # makes it answer 404 before any AWS call.
    ("POST", "/connection/aws/test"): {"use_case_id": "not-a-use-case"},
}
"""A request body for the routes where the empty object would reach something outside the process."""

PATH_VALUES: Final[dict[str, str]] = {"name": "run.json"}
"""A value for a path parameter; every other parameter gets `does-not-exist`."""


@dataclass(frozen=True)
class LiveRoute:
    """One (method, path template) the app serves, with its route object."""

    method: str
    path: str
    route: APIRoute

    @property
    def id(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def policy(self) -> RoutePolicy | None:
        return policy_for(self.method, self.path)

    def url(self) -> str:
        url = self.path
        for param in self.route.dependant.path_params:
            url = url.replace("{" + param.name + "}", PATH_VALUES.get(param.name, "does-not-exist"))
        return url

    def call(self, client: TestClient, headers: dict[str, str] | None = None) -> Any:
        body = BODIES.get((self.method, self.path), {})
        kwargs: dict[str, Any] = {"headers": headers or {}}
        if self.method in MUTATING_METHODS:
            kwargs["json"] = body
        return client.request(self.method, self.url(), **kwargs)


def _walk(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every `APIRoute` reachable from `routes` with its effective path (FastAPI >= 0.140 nests them)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        nested = getattr(route, "original_router", None)
        if nested is None:
            continue
        context = getattr(route, "include_context", None)
        yield from _walk(nested.routes, prefix + str(getattr(context, "prefix", "") or ""))


def live_routes(app: FastAPI | None = None) -> list[LiveRoute]:
    """Every (method, path) of `app` (a fresh `create_app()` by default), HEAD folded into GET."""
    found: dict[tuple[str, str], LiveRoute] = {}
    for path, route in _walk((app or create_app()).routes):
        for method in sorted(set(route.methods or ()) - {"HEAD", "OPTIONS"}):
            found.setdefault((method, path), LiveRoute(method, path, route))
    return sorted(found.values(), key=lambda item: (item.path, item.method))


def local_app(tmp_path: Path, *, auth_mode: str = "local", env: str = "local", **extra: Any) -> FastAPI:
    """An app on `tmp_path` with sign-in `auth_mode`, and a fast-hashing user store already in place."""
    settings = Settings(auth_mode=auth_mode, env=env, data_dir=tmp_path, **extra)  # type: ignore[arg-type]
    # Settings are set after construction, as tests/integration/test_api_connection_security.py does:
    # `create_app(settings=...)` also reconfigures the process's logging, which would leak into every
    # later test (tests/unit/test_logging_audit.py asserts on the pristine root logger).
    app = create_app(data_dir=tmp_path)
    app.state.settings = settings
    app.state.user_store = SqlUserStore(
        sqlite_engine(tmp_path / PLATFORM_DB_FILENAME),
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        iterations=FAST_ITERATIONS,
    )
    return app


def store_of(app: FastAPI) -> SqlUserStore:
    store = app.state.user_store
    assert isinstance(store, SqlUserStore)
    return store


def make_user(app: FastAPI, username: str, roles: Iterable[Role]) -> str:
    """Create a user directly in the store; returns the user id."""
    return store_of(app).create_user(username, PASSWORD, roles=roles, created_by="test").user_id


def bearer(app: FastAPI, user_id: str) -> dict[str, str]:
    """A fresh session for `user_id`, as request headers."""
    return {"Authorization": f"Bearer {store_of(app).create_session(user_id).token}"}


def audit_log_at(tmp_path: Path) -> SqlAuditLog:
    return SqlAuditLog(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME))


def event_count(tmp_path: Path) -> int:
    return audit_log_at(tmp_path).count(AuditQuery())
