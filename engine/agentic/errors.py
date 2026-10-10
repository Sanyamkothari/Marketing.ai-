"""The one exception the agent-safe core raises: a code from `PLAN_K_CODES`, a plain message and a status.

The status is the HTTP status the API answers with, so `api.routes.agentic` maps an error without a table
of its own, and an in-process caller (the scenario harness, M133's MCP server, M139's planner) reads the
code without HTTP.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["AgenticError"]


class AgenticError(Exception):
    """A refusal or failure with a `PLAN_K_CODES` code (or an existing engine code it passes on)."""

    def __init__(
        self, code: str, message: str, *, status: int = 409, detail: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail: dict[str, Any] = dict(detail or {})
