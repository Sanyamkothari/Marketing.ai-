"""Scopes and the caller (Plan K M133, DEC-1404 (c); design §7.2).

Two scopes, mapped from the product's roles - there is **no scope that approves**:

| Scope | Role | Tools |
|---|---|---|
| `marketing-ai:read` | Viewer (every signed-in person) | the `read` tools whose `RoutePolicy` asks for a Viewer |
| `marketing-ai:propose` | Analyst | the `propose` tools (their routes ask for an Analyst) |

Per tool, the tool's own `RoutePolicy` decides (`scope_for`): the scope is the one its role maps to. A tool
whose route asks for any other role (an Approver, an Admin) maps to no scope and is never exposed, so a tool
added to the registry later cannot reach MCP with more than the person behind the token could do.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from engine.access.roles import Principal, Role

if TYPE_CHECKING:
    from engine.agentic.registry import AgentTool

__all__ = [
    "ROLE_SCOPES",
    "SCOPE_PROPOSE",
    "SCOPE_READ",
    "McpCaller",
    "client_label",
    "scope_for",
    "scopes_for",
    "token_key",
]

SCOPE_READ: Final[str] = "marketing-ai:read"
SCOPE_PROPOSE: Final[str] = "marketing-ai:propose"

ROLE_SCOPES: Final[dict[Role, str]] = {Role.VIEWER: SCOPE_READ, Role.ANALYST: SCOPE_PROPOSE}
"""The only roles that map to a scope. Approver and Admin map to none: nothing over MCP approves or administers."""

_CLIENT_NAME: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class McpCaller:
    """Who is calling: the signed-in principal, the scopes it holds, and how the call is keyed and named."""

    principal: Principal
    scopes: frozenset[str]
    token_key: str
    """sha256 of the bearer token: the rate-limit key. The token itself is never kept."""
    client_id: str
    """`mcp:<the name the client declared>`, cleaned; `mcp:unnamed` when it declared none."""


def scopes_for(principal: Principal) -> frozenset[str]:
    """The scopes a principal's roles map to (Viewer is implied by every role)."""
    return frozenset(scope for role, scope in ROLE_SCOPES.items() if principal.has(role))


def scope_for(tool: AgentTool[Any, Any]) -> str | None:
    """The scope a tool needs, from its `RoutePolicy`'s role; None means the tool is never exposed."""
    role = tool.route_policy.role
    return None if role is None else ROLE_SCOPES.get(Role(role))


def token_key(token: str) -> str:
    """The key a session's limits are counted under: its sha256, never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_label(declared: str | None) -> str:
    """`mcp:<name>` from the name a client declared about itself: untrusted text, so letters, digits, `.`,
    `_` and `-` only, at most 48 characters."""
    cleaned = _CLIENT_NAME.sub("-", declared or "").strip("-.")[:48]
    return f"mcp:{cleaned or 'unnamed'}"
