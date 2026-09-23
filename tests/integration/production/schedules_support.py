"""Shared helpers of the M49 API tests: an app over a client with real tables, users, and a moved clock.

The world is `tests/unit/production/scheduling_support.make_world`'s - a client with real sources,
mappings and a training recipe, built by the onboarding code - and the app is pointed at the same
data directory, so a firing through the API reads exactly what the engine-level tests read. The app
gets the world's recording job runner (nothing trains unless a test asks), the world's fake clock as
`app.state.scheduling_clock`, and sign-in on with one user per role.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.schedules import CLOCK_SLOT
from engine.access.roles import Role
from engine.audit.events import AuditEvent, AuditQuery
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user
from tests.unit.production.scheduling_support import World, make_world


@dataclass
class Api:
    """The app, a client on it, the world beneath it and a header set per role."""

    app: FastAPI
    client: TestClient
    world: World
    data_dir: Path
    headers: dict[str, dict[str, str]]
    user_ids: dict[str, str]

    def as_(self, who: str) -> dict[str, str]:
        return self.headers[who]

    def events(self, action: str | None = None) -> tuple[AuditEvent, ...]:
        """Audit events, newest first; an action ending in `.` is a family."""
        return audit_log_at(self.data_dir).query(AuditQuery(action=action, limit=1000))


def build_api(root: Path, config_root: Path, *, auth_mode: str = "local", **settings: Any) -> Api:
    """A world under `root` and an app on its data directory, with the world's jobs, stores and clock."""
    world = make_world(root, config_root)
    data_dir = root / "data"
    app = local_app(data_dir, auth_mode=auth_mode, **settings)
    app.state.config_root = config_root
    app.state.storage = world.storage
    app.state.registry = world.registry
    app.state.jobs = world.jobs
    app.state.client_store = world.client_store
    setattr(app.state, CLOCK_SLOT, world.clock)
    headers: dict[str, dict[str, str]] = {}
    user_ids: dict[str, str] = {}
    if auth_mode == "local":
        for role in Role:
            user_ids[role.value] = make_user(app, f"only-{role.value}", [role])
            headers[role.value] = bearer(app, user_ids[role.value])
        user_ids["analyst2"] = make_user(app, "second-analyst", [Role.ANALYST])
        headers["analyst2"] = bearer(app, user_ids["analyst2"])
    client = TestClient(app, raise_server_exceptions=False)
    return Api(app, client, world, data_dir, headers, user_ids)


def code_of(response: Any) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None
