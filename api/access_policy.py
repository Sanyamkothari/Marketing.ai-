"""Which role every API route requires, and what the audit trail calls it (Phase 4b M46/M47).

One table, keyed by `(HTTP method, route path template)` exactly as FastAPI spells them, so the
whole access model can be reviewed on one screen and a route without an entry is a red test
(`tests/unit/production/test_route_policies.py`) rather than a route that is quietly open.

Phase 1-4a routes are declared in `LEGACY_POLICIES` here, because their router modules belong to
other workstreams and must not be edited (PARALLEL_WORK_PROTOCOL.md §3). A Phase 4b router declares
its own routes by calling `register(...)` at import time, next to the routes themselves.

`role=None` means public: the route answers without a sign-in (the liveness probe, signing in).
Every mutating route is audited; a GET is audited only when its policy says so (an export, a
privacy access request), because reading is not an event worth a row per poll (DEC-704).
"""

from __future__ import annotations

import threading
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from engine.access.roles import Role

__all__ = [
    "LEGACY_POLICIES",
    "MUTATING_METHODS",
    "RoutePolicy",
    "all_policies",
    "policy_for",
    "register",
]

MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RoutePolicy(BaseModel):
    """What one route requires and how it is recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Role | None = Field(description="Role required; None means the route is public.")
    action: str = Field(description="Dotted audit action name, e.g. `models.approve`.")
    object_type: str | None = Field(default=None, description="What the path's identifier names.")
    object_param: str | None = Field(default=None, description="Path parameter holding the object id.")
    audit_reads: bool = Field(default=False, description="Audit this route even though it is a read.")


PolicyKey = tuple[str, str]
"""`(method, path template)`, e.g. `("POST", "/models/{model_id}/approve")`."""

LEGACY_POLICIES: Final[dict[PolicyKey, RoutePolicy]] = {}
"""Every route that existed before Phase 4b. Filled in by M46; see the module docstring."""

_REGISTERED: dict[PolicyKey, RoutePolicy] = {}
_LOCK: Final[threading.Lock] = threading.Lock()


def register(policies: dict[PolicyKey, RoutePolicy]) -> None:
    """Declare the policies of a Phase 4b router. A key declared twice with a different policy is an error."""
    with _LOCK:
        for key, policy in policies.items():
            existing = _REGISTERED.get(key) or LEGACY_POLICIES.get(key)
            if existing is not None and existing != policy:
                raise ValueError(f"route {key} already has a different access policy")
            _REGISTERED[key] = policy


def policy_for(method: str, path: str) -> RoutePolicy | None:
    """The policy of one route, or None when the route declares none (which the enforcement refuses)."""
    key = (method.upper(), path)
    return _REGISTERED.get(key) or LEGACY_POLICIES.get(key)


def all_policies() -> dict[PolicyKey, RoutePolicy]:
    """Every declared policy, legacy and registered."""
    with _LOCK:
        return {**LEGACY_POLICIES, **_REGISTERED}
