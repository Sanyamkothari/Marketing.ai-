"""Tasks for long audits (Plan K M141, DEC-1408 (e)): the tasks extension over the verifier, in process.

A client that supports the extension gets a task handle for a long tool and the same answer a synchronous call
gives once it polls; a client that does not gets the synchronous answer; a task answers only the session that
started it; a cancelled task's result is discarded; a task still working past its deadline has failed
(`MCP_TIMEOUT`) and keeps its place in the session's concurrency budget until its work ends; a record past its
ttl is gone.

On the unchanged code (M133's server) the long tool answers synchronously whatever the client declares, and
there is no `tasks/get`: these tests fail there.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.shared.exceptions import MCPError
from pydantic import Field

from api.access_policy import RoutePolicy
from api.mcp.config import McpSettings, RateLimit, TaskSettings
from api.mcp.limits import RateLimiter
from api.mcp.scopes import McpCaller, scopes_for, token_key
from api.mcp.server import Verifier
from api.mcp.task_client import TasksExtension, cancel_task, get_task, task_result
from api.mcp.tasks import TASKS_PREFIX, TaskHandle, TaskStore
from api.mcp.transport import InProcessTransport
from engine.access.roles import Principal, Role
from engine.agentic.registry import AgentTool, ToolContext, ToolKind, ToolRegistry
from engine.config import StrictBase
from engine.storage import LocalStorage

GATE = threading.Event()


class Ask(StrictBase):
    size: int = Field(default=1, ge=1, le=100)


class Answer(StrictBase):
    size: int
    rows: int


def _long(given: Ask, _ctx: ToolContext) -> Answer:
    GATE.wait(timeout=10)
    return Answer(size=given.size, rows=40)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name, run in (("audit_campaign", _long), ("look", lambda g, _c: Answer(size=g.size, rows=12))):
        registry.register(
            AgentTool(
                name=name,
                kind=ToolKind.READ,
                description=f"The {name} tool.",
                input_model=Ask,
                output_model=Answer,
                route_policy=RoutePolicy(role=Role.VIEWER, action=f"test.{name}", purpose=f"use {name}"),
                mirrors=("GET", f"/{name}"),
                evidence_label=lambda _out: "Descriptive",
                run=run,
            )
        )
    return registry


def _caller(token: str = "t-1") -> McpCaller:
    principal = Principal(user_id="u_1", username="u", roles=frozenset({Role.VIEWER}), kind="user")
    return McpCaller(
        principal=principal, scopes=scopes_for(principal), token_key=token_key(token), client_id="mcp:t"
    )


def _verifier(tmp_path: Path, *, timeout: float = 5.0, concurrent: int = 2) -> Verifier:
    def context_for(caller: McpCaller) -> ToolContext:
        return ToolContext(
            storage=LocalStorage(tmp_path),
            principal=caller.principal,
            config_root=tmp_path,
            clock=lambda: datetime(2026, 10, 11, tzinfo=UTC),
        )

    settings = McpSettings(
        call_timeout_seconds=timeout,
        rate_limit=RateLimit(concurrent_calls=concurrent),
        tasks=TaskSettings(tools=("audit_campaign",), poll_interval_ms=100),
    )
    return Verifier(
        registry=_registry(),
        settings=settings,
        context_for=context_for,
        limiter=RateLimiter(calls_per_minute=100, concurrent_calls=concurrent),
        tasks=TaskStore(LocalStorage(tmp_path)),
    )


def _records(tmp_path: Path) -> tuple[str, ...]:
    return LocalStorage(tmp_path).list_keys(TASKS_PREFIX)


def test_a_client_with_the_extension_gets_a_task_and_the_same_answer(tmp_path: Path) -> None:
    GATE.set()
    verifier = _verifier(tmp_path)
    handles: list[Any] = []

    async def run() -> tuple[Any, Any, Any]:
        extension = TasksExtension(on_task=handles.append)
        async with InProcessTransport(verifier, _caller(), extensions=(extension,)).connect() as client:
            tasked = await client.call_tool("audit_campaign", {"size": 3})
            quick = await client.call_tool("look", {"size": 2})
        async with InProcessTransport(verifier, _caller()).connect() as client:
            plain = await client.call_tool("audit_campaign", {"size": 3})
        return tasked, quick, plain

    tasked, quick, plain = anyio.run(run)
    assert len(handles) == 1, "only the long tool ran as a task"
    assert handles[0].task.status == "working" and handles[0].task.task_id.startswith("task_")
    assert tasked.is_error is False and tasked.structured_content == plain.structured_content == {
        "size": 3,
        "rows": 40,
    }
    assert tasked.meta["marketing-ai/evidence_id"] == plain.meta["marketing-ai/evidence_id"]
    assert quick.is_error is False
    assert len(_records(tmp_path)) == 1, "the synchronous calls wrote no task record"


def test_a_task_answers_only_the_session_that_started_it(tmp_path: Path) -> None:
    GATE.set()
    verifier = _verifier(tmp_path)

    async def run() -> tuple[str, Any]:
        async with InProcessTransport(
            verifier, _caller("t-1"), extensions=(TasksExtension(),)
        ).connect() as client:
            handle: Any = await client.session.call_tool("audit_campaign", {"size": 1}, allow_claimed=True)
            assert isinstance(handle, TaskHandle)
            task_id = handle.task.task_id
        async with InProcessTransport(
            verifier, _caller("t-2"), extensions=(TasksExtension(),)
        ).connect() as other:
            with pytest.raises(MCPError) as caught:
                await get_task(other.session, task_id)
        return task_id, caught.value

    _, error = anyio.run(run)
    assert "MCP_TASK_NOT_FOUND" in str(error) and error.error.data == {"code": "MCP_TASK_NOT_FOUND"}


def test_a_cancelled_task_discards_its_result(tmp_path: Path) -> None:
    GATE.clear()
    verifier = _verifier(tmp_path)
    store = verifier.tasks
    assert store is not None

    async def run() -> tuple[Any, Any, Any]:
        async with InProcessTransport(
            verifier, _caller(), extensions=(TasksExtension(),)
        ).connect() as client:
            handle: Any = await client.session.call_tool("audit_campaign", {"size": 1}, allow_claimed=True)
            cancelled = await cancel_task(client.session, handle.task.task_id)
            GATE.set()
            await anyio.sleep(0.3)
            seen = await get_task(client.session, handle.task.task_id)
            result = await task_result(client.session, handle.task.task_id)
        return cancelled, seen, result

    try:
        cancelled, seen, result = anyio.run(run)
    finally:
        GATE.set()
    assert cancelled.status == seen.status == "cancelled"
    assert result.is_error is True and result.meta["marketing-ai/code"] == "MCP_TASK_CANCELLED"
    assert result.structured_content is None, "the work ended after the cancel: its answer was discarded"


def test_a_task_past_its_deadline_has_failed_and_keeps_its_place_until_its_work_ends(tmp_path: Path) -> None:
    GATE.clear()
    verifier = _verifier(tmp_path, timeout=0.3, concurrent=1)

    async def run() -> tuple[Any, Any, Any]:
        async with InProcessTransport(
            verifier, _caller(), extensions=(TasksExtension(),)
        ).connect() as client:
            handle: Any = await client.session.call_tool("audit_campaign", {"size": 1}, allow_claimed=True)
            await anyio.sleep(0.5)
            seen = await get_task(client.session, handle.task.task_id)
            busy = await client.call_tool("look", {})
            result = await task_result(client.session, handle.task.task_id)
        return seen, busy, result

    try:
        seen, busy, result = anyio.run(run)
    finally:
        GATE.set()
    assert seen.status == "failed" and seen.status_message == "MCP_TIMEOUT"
    assert busy.is_error and busy.meta["marketing-ai/code"] == "MCP_RATE_LIMITED", "the work still counts"
    assert result.is_error and result.meta["marketing-ai/code"] == "MCP_TIMEOUT"


def test_a_record_past_its_ttl_is_gone(tmp_path: Path) -> None:
    store = TaskStore(LocalStorage(tmp_path))
    now = datetime(2026, 10, 11, tzinfo=UTC)
    record = store.create(
        token_key="k", workspace="acme", tool="audit_campaign", now=now, timeout_seconds=5, ttl_seconds=60,
        poll_interval_ms=500,
    )  # fmt: skip
    assert store.get(record.task_id, token_key="k", now=now + timedelta(seconds=59)) is not None
    assert store.get(record.task_id, token_key="other", now=now) is None, "another session's key"
    assert store.get(record.task_id, token_key="k", now=now + timedelta(seconds=61)) is None
    assert not _records(tmp_path), "an expired record is deleted when it is read"
    assert store.get("../../etc/passwd", token_key="k", now=now) is None, "a task id is never a path"
