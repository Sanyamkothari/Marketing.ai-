"""Who may do what: the four roles of Phase 4b M46 and the principal a request acts as.

Roles are a *set* per user, not a ladder. Viewer is implied by every role - anybody who may act on a
result may also see it - but nothing else is: an Admin manages users and settings and does **not**
thereby approve champions, and an Approver signs off on work without being able to start it. That
separation is the point of having an Approver at all; a ladder would let the person who trained a
model also crown it (DEC-703).

This module imports nothing from `engine` beyond the standard library and pydantic, so the API's
access layer, the scheduler and the privacy jobs can all name a role without importing each other.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LOCAL_OPERATOR",
    "ROLE_DESCRIPTIONS",
    "SYSTEM_SCHEDULER",
    "Principal",
    "Role",
    "effective_roles",
]


class Role(StrEnum):
    """The four roles of plan M46. The value is what the API, the UI and the audit log spell."""

    VIEWER = "viewer"
    ANALYST = "analyst"
    APPROVER = "approver"
    ADMIN = "admin"


ROLE_DESCRIPTIONS: Final[dict[Role, str]] = {
    Role.VIEWER: "Viewer",
    Role.ANALYST: "Analyst",
    Role.APPROVER: "Approver",
    Role.ADMIN: "Admin",
}
"""How a role is named to a person - in a refusal ("Only an Approver can …") and in the UI."""


def effective_roles(roles: frozenset[Role]) -> frozenset[Role]:
    """`roles` plus Viewer, which every role implies; nothing else is implied (DEC-703)."""
    return roles | {Role.VIEWER} if roles else roles


class Principal(BaseModel):
    """Who a request acts as. Built by the identity layer; never taken from a request body.

    `user_id` is what the audit log records. `username` is shown to people. `kind` separates a
    person from the machinery that acts on a schedule, so an audit reader can tell "Asha approved
    this" from "the monthly scoring schedule started this run".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(description="Stable identifier of the user; what the audit log records.")
    username: str = Field(description="What the person signs in with and is shown as.")
    roles: frozenset[Role] = Field(description="The roles held, Viewer already implied.")
    kind: str = Field(default="user", description="`user`, `local_operator` (auth off) or `system`.")

    def has(self, role: Role) -> bool:
        """Whether this principal holds `role` (Viewer is implied by any role)."""
        return role in effective_roles(self.roles)


LOCAL_OPERATOR: Final[Principal] = Principal(
    user_id="local-operator",
    username="local operator",
    roles=frozenset(Role),
    kind="local_operator",
)
"""Who every request is when `auth_mode=off`: one person at a laptop holding every role.

Named rather than anonymous so the audit trail of a laptop still says something true - that the
action was taken with access control switched off - instead of attributing it to nobody (DEC-702).
"""

SYSTEM_SCHEDULER: Final[Principal] = Principal(
    user_id="system:scheduler",
    username="scheduler",
    roles=frozenset({Role.ANALYST}),
    kind="system",
)
"""What a scheduled firing acts as: it may score, check drift and train a challenger - an Analyst's
work - and it can never approve anything, so retraining can only ever produce a challenger (M49)."""
