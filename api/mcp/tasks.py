"""Tasks for long audits: the MCP tasks extension on the verifier (Plan K M141, DEC-1408 (e); design §7.1).

The 2026-07-28 revision moved Tasks out of the core protocol into an extension (`io.modelcontextprotocol/tasks`);
the pinned SDK (`mcp==2.3.0`) carries its wire types and the extension mechanism but no implementation, so the
server side is here, over the SDK's own seams (an extension `resultType` and three extension request methods):

* A client that supports the extension says so on each request (`_meta` `clientCapabilities.extensions`). When it
  calls one of `mcp.tasks.tools` (`audit_campaign` and `get_proof_pack` by default: they read and draw stored
  artefacts, the slow part of a large campaign), the server checks the call exactly as a synchronous one - the
  limits, the scope, the workspace - then answers at once with a **task handle**
  (``{"resultType": "task", "task": {"taskId", "status": "working", ...}}``) and runs the same pipeline
  (`api.mcp.server.Verifier._work`: guard, the registry's call, suppression, the text gate) on a worker thread.
* `tasks/get` answers the task's status, `tasks/result` its result (a `CallToolResult`, exactly what the
  synchronous call would have answered; it waits for a task still working, up to the call timeout), and
  `tasks/cancel` cancels it: the result is then discarded when the work ends (a thread cannot be stopped).
* A client that does not declare the extension, and every client of the 2025 revisions, gets the synchronous
  answer, as on M133's server.

**Where a task lives.** Its record is a small JSON document in the artefact store (`agentic/mcp/tasks/<id>.json`),
so any API process behind the load balancer answers `tasks/get` for it, as the stateless transport needs. It holds
the task's status and, once finished, the served result: already suppressed and gated, aggregates only, never a
row. A record is kept `mcp.tasks.ttl_seconds` (an hour by default) after it was made, and is then gone: a read
past that deletes it, and every new task first sweeps the store of the records past theirs, so a task its client
never polls again does not stay behind with its figures. The work runs on a worker
thread of the API process that accepted the call; if that process stops, the task fails at its deadline
(`MCP_TIMEOUT`) instead of staying `working` for ever.

**Whose task it is.** A task answers only the session that started it: the record keeps a digest of the session's
key (itself the sha256 of the bearer token, never the token) and the workspace. Another session asking about it
- even the same person's - is told there is no such task (`MCP_TASK_NOT_FOUND`), so task ids reveal nothing.

**Limits.** A task holds its place in the session's concurrency budget (`mcp.rate_limit.concurrent_calls`) until
its work ends, as a synchronous call does, and counts as a call against `calls_per_minute`; there is no queue, so
a session cannot pile up work. Polling (`tasks/get`) is not counted as a call. The work is audited
(`mcp.tool_call`) when it ends, with the task id; a cancellation is audited as `mcp.task_cancel`.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import secrets
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from mcp_types import Result, Task
from pydantic import AwareDatetime, Field

from engine.config import StrictBase

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "TASKS_EXTENSION",
    "TASKS_PREFIX",
    "TERMINAL",
    "TaskHandle",
    "TaskRecord",
    "TaskStatus",
    "TaskStore",
    "asks_for_tasks",
]

TASKS_EXTENSION: Final[str] = "io.modelcontextprotocol/tasks"
TASKS_PREFIX: Final[str] = "agentic/mcp/tasks/"
_TASK_ID: Final[re.Pattern[str]] = re.compile(r"task_[0-9a-f]{24}")
_LOCK: Final = threading.Lock()

TaskStatus = Literal["working", "completed", "failed", "cancelled"]
TERMINAL: Final[frozenset[str]] = frozenset({"completed", "failed", "cancelled"})


class TaskHandle(Result):
    """`{"resultType": "task", "task": {...}}`: what a long tool answers a client that supports tasks (an
    extension result). The client side claims the same shape (`api.mcp.task_client`)."""

    result_type: Literal["task"] = "task"
    task: Task


def asks_for_tasks(meta: dict[str, Any] | None) -> bool:
    """Whether a request's `_meta` says its client supports the tasks extension."""
    from mcp_types import CLIENT_CAPABILITIES_META_KEY

    capabilities = None if meta is None else meta.get(CLIENT_CAPABILITIES_META_KEY)
    extensions = capabilities.get("extensions") if isinstance(capabilities, dict) else None
    return isinstance(extensions, dict) and TASKS_EXTENSION in extensions


def owner_of(token_key: str) -> str:
    """What a task record keeps of the session that started it: a digest of its key, never the token."""
    return hashlib.sha256(b"marketing-ai/mcp-task-owner\x00" + token_key.encode("ascii")).hexdigest()


class TaskRecord(StrictBase):
    """One task, as the artefact store keeps it."""

    task_id: str
    owner: str = Field(description="owner_of(the session's key): only that session sees the task.")
    workspace: str | None = Field(description="The client workspace it ran in; null on the demo server.")
    tool: str
    status: TaskStatus
    status_message: str | None = None
    created_at: AwareDatetime
    last_updated_at: AwareDatetime
    deadline: AwareDatetime = Field(description="created_at + mcp.call_timeout_seconds: failed after it.")
    ttl_ms: int
    poll_interval_ms: int
    result: dict[str, Any] | None = Field(
        default=None,
        description="The CallToolResult served (aggregates, suppressed and gated), once finished.",
    )

    def seen_at(self, now: datetime) -> TaskRecord:
        """The record as a reader sees it at `now`: a task still working past its deadline has failed."""
        if self.status == "working" and now >= self.deadline:
            return self.model_copy(
                update={"status": "failed", "status_message": "MCP_TIMEOUT", "last_updated_at": self.deadline}
            )
        return self

    def wire(self) -> dict[str, Any]:
        """The task as the protocol shows it (`Task`: camelCase, timestamps as ISO 8601)."""
        return {
            "taskId": self.task_id,
            "status": self.status,
            "statusMessage": self.status_message,
            "createdAt": self.created_at.isoformat(),
            "lastUpdatedAt": self.last_updated_at.isoformat(),
            "ttl": self.ttl_ms,
            "pollInterval": self.poll_interval_ms,
        }


class TaskStore:
    """Task records in the artefact store (see the module docstring)."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def _key(self, task_id: str) -> str:
        return f"{TASKS_PREFIX}{task_id}.json"

    def create(
        self,
        *,
        token_key: str,
        workspace: str | None,
        tool: str,
        now: datetime,
        timeout_seconds: float,
        ttl_seconds: int,
        poll_interval_ms: int,
    ) -> TaskRecord:
        record = TaskRecord(
            task_id=f"task_{secrets.token_hex(12)}",
            owner=owner_of(token_key),
            workspace=workspace,
            tool=tool,
            status="working",
            created_at=now,
            last_updated_at=now,
            deadline=now + timedelta(seconds=timeout_seconds),
            ttl_ms=ttl_seconds * 1000,
            poll_interval_ms=poll_interval_ms,
        )
        self.sweep(now)
        self._storage.write_model(self._key(record.task_id), record)
        return record

    def sweep(self, now: datetime) -> int:
        """Delete every record past its ttl (whoever started it); the number deleted. Never fails a call: a
        record that will not read or delete is left for the next sweep."""
        from engine.storage import StorageError

        try:
            keys = self._storage.list_keys(TASKS_PREFIX)
        except (StorageError, OSError):
            return 0
        swept = 0
        for key in keys:
            name = key.rsplit("/", 1)[-1]
            if not name.endswith(".json"):
                continue
            record = self._read(name.removesuffix(".json"))
            if record is None or now < record.created_at + timedelta(milliseconds=record.ttl_ms):
                continue
            with contextlib.suppress(Exception):
                self._storage.delete(key)
                swept += 1
        return swept

    def _read(self, task_id: str) -> TaskRecord | None:
        from engine.storage import StorageError

        if not _TASK_ID.fullmatch(task_id):
            return None
        try:
            return self._storage.read_model(self._key(task_id), TaskRecord)
        except (StorageError, OSError, ValueError):
            return None

    def get(self, task_id: str, *, token_key: str, now: datetime) -> TaskRecord | None:
        """The task as its own session sees it now; None when it is not there, not theirs, or past its ttl."""
        record = self._read(task_id)
        if record is None or record.owner != owner_of(token_key):
            return None
        if now >= record.created_at + timedelta(milliseconds=record.ttl_ms):
            with contextlib.suppress(Exception):  # a record that will not delete is simply read as gone
                self._storage.delete(self._key(task_id))
            return None
        return record.seen_at(now)

    def finish(self, task_id: str, result: dict[str, Any], *, now: datetime, failed: bool = False) -> None:
        """Store the result of a task whose work ended; a cancelled task, or one past its deadline, keeps its
        status and the result is discarded."""
        with _LOCK:
            record = self._read(task_id)
            if record is None or record.seen_at(now).status != "working":
                return
            self._storage.write_model(
                self._key(task_id),
                record.model_copy(
                    update={
                        "status": "failed" if failed else "completed",
                        "last_updated_at": now,
                        "result": result,
                    }
                ),
            )

    def cancel(self, task_id: str, *, token_key: str, now: datetime) -> TaskRecord | None:
        """Cancel a working task of this session; None when there is no such task; the record unchanged when it
        has already ended."""
        with _LOCK:
            record = self.get(task_id, token_key=token_key, now=now)
            if record is None or record.status != "working":
                return record
            cancelled = record.model_copy(
                update={"status": "cancelled", "status_message": "MCP_TASK_CANCELLED", "last_updated_at": now}
            )
            self._storage.write_model(self._key(task_id), cancelled)
            return cancelled
