"""`configs/agentic/mcp.yaml`: the MCP switch and its limits (Plan K M133, DEC-1404 (a)).

`mcp.enabled` is **off** by default. Off, `api.main` imports this module only - no MCP SDK, no tool, no route -
and every response is byte-identical to Plan J's. A missing file is the default (off), so a configuration root
from before M133 behaves as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.agentic.errors import AgenticError

__all__ = [
    "DEMO_WORKSPACE",
    "MCP_CONFIG_FILE",
    "McpConfig",
    "McpSettings",
    "RateLimit",
    "load_mcp_config",
    "mcp_enabled",
]

MCP_CONFIG_FILE: Final[str] = "agentic/mcp.yaml"
DEMO_WORKSPACE: Final[str] = "demo"
"""The one workspace M133 serves. M141 adds per-client workspaces."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RateLimit(_Strict):
    calls_per_minute: Annotated[int, Field(ge=1, le=10_000)] = 60
    concurrent_calls: Annotated[int, Field(ge=1, le=64)] = 2


class McpSettings(_Strict):
    enabled: bool = Field(default=False, description="Mount the MCP server at /mcp.")
    workspace: str = Field(
        default=DEMO_WORKSPACE,
        min_length=1,
        max_length=64,
        description="The workspace served; only `demo` until M141 (anything else: MCP_DEMO_ONLY).",
    )
    demo_use_cases: tuple[str, ...] = Field(
        default=("hillstrom-email",), description="Use cases whose runs and uploads are demo data."
    )
    min_group_rows: Literal[10] = Field(
        default=10, description="Plan I's rate_by minimum; fixed at 10, not a setting to lower."
    )
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    call_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 120.0


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
    """Whether `mcp.enabled` is on under this configuration root."""
    return load_mcp_config(config_root).mcp.enabled
