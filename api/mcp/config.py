"""The MCP switch and `configs/agentic/mcp.yaml`, the server's settings (Plan K M133, DEC-1404 (a)).

The switch is `mcp.enabled` in `configs/agentic/agentic.yaml`, one of Plan K's three switches (M130, DEC-1400 (d);
read through `engine.agentic.config`), and is **off** by default. Off, `api.main` imports this module only - no MCP
SDK, no tool, no route - and every response is byte-identical to Plan J's. `mcp.yaml` holds what the server does
once it is on (the workspace, the demo use cases, the rate limits and the call timeout); a missing file is the
defaults, so a configuration root from before M133 behaves as before. (The builder kept the switch in `mcp.yaml`
too; at integration it was moved to the one place M130 declared it, so there is a single switch.)

**Client workspaces (Plan K M141, DEC-1408 (a)).** `mcp.workspace: demo` (the default) is M133's server, on the
demo library only. `mcp.workspace: clients` serves client data, scoped per client by `mcp.workspaces`: each
workspace names the client whose data it holds (`client_id`, the onboarded client whose runs carry it), or
claims the data no client is recorded on (`unattributed: true`: uploads, runs made from uploads, audited
campaigns - at most one workspace may, the single-client deployment's), and lists the people bound to it
(`members`, their user ids). A person is a member of at most one workspace, so every bearer token of theirs is
bound to exactly one; a person in none is refused at sign-in. Each workspace keeps its own ledger chain
(`engine.agentic.ledger`). Anything else - a workspace with neither a client nor the claim, two workspaces for
one client, a person in two - is refused when the file loads (`CONFIG_INVALID`).

**Tasks (M141, DEC-1408 (e)).** `mcp.tasks`: the long tools a client that supports the tasks extension
(`io.modelcontextprotocol/tasks`) gets a task handle for, how long a finished task is kept, and how often to poll.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from engine.agentic.errors import AgenticError

__all__ = [
    "CLIENT_WORKSPACES",
    "DEMO_WORKSPACE",
    "MCP_CONFIG_FILE",
    "WORKSPACE_ID_PATTERN",
    "ClientWorkspace",
    "McpConfig",
    "McpSettings",
    "RateLimit",
    "TaskSettings",
    "load_mcp_config",
    "mcp_enabled",
]

MCP_CONFIG_FILE: Final[str] = "agentic/mcp.yaml"
DEMO_WORKSPACE: Final[str] = "demo"
"""The one workspace M133 serves. M141 adds per-client workspaces."""
CLIENT_WORKSPACES: Final[str] = "clients"
"""`mcp.workspace: clients` (M141): client data, one workspace per client (`mcp.workspaces`)."""
WORKSPACE_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_-]{0,47}$"
"""A workspace id; it names the workspace's ledger chain (`engine.agentic.ledger.CHAIN_PATTERN`, the same)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RateLimit(_Strict):
    calls_per_minute: Annotated[int, Field(ge=1, le=10_000)] = 60
    concurrent_calls: Annotated[int, Field(ge=1, le=64)] = 2


class ClientWorkspace(_Strict):
    """One client's workspace on a `workspace: clients` server (M141)."""

    id: str = Field(pattern=WORKSPACE_ID_PATTERN, description="Names the workspace and its ledger chain.")
    client_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="The onboarded client whose runs (and the campaigns made from them) are this workspace's.",
    )
    unattributed: bool = Field(
        default=False,
        description=(
            "Also hold the data no client is recorded on: uploads, runs made from uploads and audited campaigns. "
            "For a deployment that serves one client; at most one workspace may claim it."
        ),
    )
    members: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = Field(
        min_length=1, description="User ids bound to this workspace: every token of theirs reaches it alone."
    )


class TaskSettings(_Strict):
    """M141: the MCP tasks extension for long tools."""

    tools: tuple[str, ...] = Field(
        default=("audit_campaign", "get_proof_pack"),
        description="Tools answered with a task handle when the client supports the tasks extension.",
    )
    ttl_seconds: Annotated[int, Field(ge=60, le=7 * 86_400)] = 3_600
    poll_interval_ms: Annotated[int, Field(ge=100, le=60_000)] = 1_000


class McpSettings(_Strict):
    workspace: str = Field(
        default=DEMO_WORKSPACE,
        min_length=1,
        max_length=64,
        description=(
            "`demo` (M133: the demo library only) or `clients` (M141: client data, per `workspaces`); anything "
            "else is refused with MCP_DEMO_ONLY."
        ),
    )
    demo_use_cases: tuple[str, ...] = Field(
        default=("hillstrom-email",), description="Use cases whose runs and uploads are demo data."
    )
    min_group_rows: Literal[10] = Field(
        default=10, description="Plan I's rate_by minimum; fixed at 10, not a setting to lower."
    )
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    call_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 120.0
    workspaces: tuple[ClientWorkspace, ...] = Field(
        default=(), description="M141: the client workspaces of a `workspace: clients` server."
    )
    tasks: TaskSettings = Field(default_factory=TaskSettings)

    @model_validator(mode="after")
    def _one_workspace_each(self) -> McpSettings:
        """Every workspace holds some data, no client is in two, at most one claims the unattributed data, and
        nobody is a member of two (a token is bound to one workspace)."""
        ids = [workspace.id for workspace in self.workspaces]
        if len(set(ids)) != len(ids):
            raise ValueError("two workspaces share an id")
        clients = [workspace.client_id for workspace in self.workspaces if workspace.client_id is not None]
        if len(set(clients)) != len(clients):
            raise ValueError("a client is named by two workspaces")
        if sum(workspace.unattributed for workspace in self.workspaces) > 1:
            raise ValueError("more than one workspace claims the data no client is recorded on")
        for workspace in self.workspaces:
            if workspace.client_id is None and not workspace.unattributed:
                raise ValueError(
                    f"workspace {workspace.id} holds no data: give it a client_id or unattributed"
                )
        members = [member for workspace in self.workspaces for member in workspace.members]
        if len(set(members)) != len(members):
            raise ValueError("a person is a member of two workspaces; a token is bound to one")
        if self.workspace == CLIENT_WORKSPACES and not self.workspaces:
            raise ValueError("workspace: clients needs at least one entry under workspaces")
        return self

    def workspace_of(self, user_id: str) -> ClientWorkspace | None:
        """The client workspace `user_id` is bound to, or None."""
        return next((workspace for workspace in self.workspaces if user_id in workspace.members), None)

    @property
    def serves_clients(self) -> bool:
        """Whether this is a client-data server (M141) rather than M133's demo one."""
        return self.workspace == CLIENT_WORKSPACES


class McpConfig(_Strict):
    """`configs/agentic/mcp.yaml`."""

    mcp: McpSettings = Field(default_factory=McpSettings)


def _root(config_root: Path | None) -> Path:
    from engine.config import config_root as resolve_root

    return resolve_root(config_root)


def load_mcp_config(config_root: Path | None = None) -> McpConfig:
    """`mcp.yaml` under the root; the defaults (off) when the file is not there."""
    path = _root(config_root) / MCP_CONFIG_FILE
    if not path.is_file():
        return McpConfig()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AgenticError(
            "CONFIG_INVALID", f"{MCP_CONFIG_FILE} could not be read: {exc}.", status=500
        ) from exc
    try:
        return McpConfig.model_validate(document)
    except ValidationError as exc:
        raise AgenticError("CONFIG_INVALID", f"{MCP_CONFIG_FILE} is not valid: {exc}.", status=500) from exc


def mcp_enabled(config_root: Path | None = None) -> bool:
    """Whether `mcp.enabled` (`configs/agentic/agentic.yaml`) is on under this configuration root.

    On, `mcp.yaml` is read as well, so an invalid settings file refuses the app at start-up (`CONFIG_INVALID`)
    instead of at the first call."""
    from engine.agentic.config import load_agentic_config

    if not load_agentic_config(config_root).mcp.enabled:
        return False
    load_mcp_config(config_root)
    return True
