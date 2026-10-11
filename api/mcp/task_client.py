"""The client side of the tasks extension: what a scripted client passes to `mcp.Client(extensions=[...])` to have
long audits run as tasks (Plan K M141, DEC-1408 (e); `api.mcp.tasks` is the server side).

With :class:`TasksExtension` the SDK's client advertises `io.modelcontextprotocol/tasks` on every request; a
long tool then answers with a task handle, which the extension's claim resolves transparently: it polls
`tasks/get` at the server's interval until the task ends and returns `tasks/result` - so `client.call_tool`
returns the same `CallToolResult` a synchronous call would have. `on_task` (optional) sees each handle, which is
how a test - or a client that wants to cancel - learns the task id; :func:`cancel_task` and :func:`get_task`
send the other two requests.

A client's own MCP stack (Claude, ChatGPT, an in-house agent) implements the extension itself; this module is the
reference the product's tests use, and needs only the pinned SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

import anyio
import mcp_types as types
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim

from api.mcp.tasks import TASKS_EXTENSION, TERMINAL, TaskHandle

if TYPE_CHECKING:
    from mcp.client.session import ClientSession

__all__ = ["TasksExtension", "cancel_task", "get_task", "task_result"]

_MIN_POLL_S: Final[float] = 0.02


async def get_task(session: ClientSession, task_id: str) -> types.GetTaskResult:
    """`tasks/get`: the task's status."""
    return await session.send_request(
        types.GetTaskRequest(params=types.GetTaskRequestParams(task_id=task_id)), types.GetTaskResult
    )


async def task_result(session: ClientSession, task_id: str) -> types.CallToolResult:
    """`tasks/result`: what the call answered (the server waits for a task still working)."""
    return await session.send_request(
        types.GetTaskPayloadRequest(params=types.GetTaskPayloadRequestParams(task_id=task_id)),
        types.CallToolResult,
    )


async def cancel_task(session: ClientSession, task_id: str) -> types.CancelTaskResult:
    """`tasks/cancel`: cancel a task still working."""
    return await session.send_request(
        types.CancelTaskRequest(params=types.CancelTaskRequestParams(task_id=task_id)), types.CancelTaskResult
    )


class TasksExtension(ClientExtension):
    """Advertise the tasks extension and resolve task handles by polling (see the module docstring)."""

    identifier = TASKS_EXTENSION

    def __init__(self, on_task: Callable[[TaskHandle], Any] | None = None) -> None:
        self._on_task = on_task
        self.polls = 0
        """How many `tasks/get` requests the resolver sent (a test reads it)."""

    def claims(self) -> list[ResultClaim[Any]]:
        return [ResultClaim(result_type="task", model=TaskHandle, resolve=self._resolve)]

    async def _resolve(self, handle: TaskHandle, ctx: ClaimContext) -> types.CallToolResult:
        if self._on_task is not None:
            outcome = self._on_task(handle)
            if hasattr(outcome, "__await__"):
                await outcome
        status = handle.task.status
        interval = max((handle.task.poll_interval or 1000) / 1000, _MIN_POLL_S)
        while status not in TERMINAL:
            await anyio.sleep(interval)
            seen = await get_task(ctx.session, handle.task.task_id)
            self.polls += 1
            status = seen.status
        return await task_result(ctx.session, handle.task.task_id)
