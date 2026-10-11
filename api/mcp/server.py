"""The verifier: the MCP tool list and the call pipeline (Plan K M133, DEC-1404 (b); design §7.1).

**The surface is the registry.** `Verifier.exposed` is exactly the agent-safe core's tools
(`engine.agentic.tools.default_registry`) whose kind is `read` or `propose` and whose `RoutePolicy` role maps
to a scope (`api.mcp.scopes`). There is no approve tool, no write tool and no tool written for MCP: a tool
reaches MCP only by being registered in the core, with its route policy. Each is listed with its input
model's JSON schema, its output model's JSON schema, and annotations that say what it does (`readOnlyHint` for
a read; a propose tool is not read-only, not destructive and touches no outside system). `tools/list` shows a
caller only the tools its scopes allow.

**One call** (`Verifier.call_tool`), in this order, every refusal a tool error with its code:

1. the per-session limits (`MCP_RATE_LIMITED`);
2. the tool is exposed (`TOOL_UNKNOWN`) and the caller holds its scope (`MCP_SCOPE_DENIED`);
3. the workspace: on the demo server, the demo's (`MCP_DEMO_ONLY`); on a client-data server (M141), the
   caller's own (`MCP_WORKSPACE_DENIED`);
4. on a worker thread, under `mcp.call_timeout_seconds` (`MCP_TIMEOUT`) - or, for a long tool and a client that
   supports the tasks extension (M141, `api.mcp.tasks`), as a task the client polls:
   the core's context for the caller (`AGENTIC_DISABLED` while the core is off; on a client-data server the
   context writes to the caller's workspace chain of the ledger), the input against the tool's model
   (`TOOL_INPUT_INVALID`), the guard on every artefact it names (demo: `MCP_DEMO_ONLY`; clients:
   `MCP_WORKSPACE_DENIED`; both: `MCP_GROUP_TOO_SMALL`), then **the registry's own `call`** - its role check,
   its output budget, its evidence id and signed `evidence.label` ledger entry, its audit event for the routes
   that audit - as the actor `agent:<client id>`;
5. small-group suppression (`api.mcp.suppress`), the third-party text gate (`api.mcp.gate`), then the output
   model again: what is returned as `structuredContent` validates against the tool's output model
   (`MCP_RESULT_WITHHELD` if gating made it not validate), with a short text written by `api.mcp.render`;
6. one `AuditEvent` for the call (`mcp.tool_call`, the principal, `client_id`, the outcome and code).

Any other fault inside a tool is answered `MCP_TOOL_FAILED`: a code, never the exception's message.

The result's `_meta` carries `marketing-ai/evidence_id`, `marketing-ai/label`, `marketing-ai/receipt` (M141: the
ledger entry the answer was recorded under - its chain, `seq` and `entry_hash` - and the holdout salt's
fingerprint, `engine.agentic.receipts`, for the client to keep) and, when something was withheld,
`marketing-ai/withheld` (counts by path).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

import anyio
import mcp_types as types
from pydantic import ValidationError

from api.mcp.config import ClientWorkspace, McpSettings
from api.mcp.demo import check_workspace, guard
from api.mcp.gate import Gate
from api.mcp.limits import Lease, RateLimiter
from api.mcp.render import render, render_error
from api.mcp.scopes import McpCaller, scope_for
from api.mcp.suppress import narrow, suppress, widen
from engine.agentic.errors import AgenticError
from engine.agentic.registry import AgentTool, ToolContext, ToolKind, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from mcp.server.context import ServerRequestContext

    from api.mcp.tasks import TaskStore
    from engine.audit.events import AuditLog
    from mcp.server import Server

__all__ = [
    "INSTRUCTIONS",
    "SERVER_NAME",
    "Verifier",
    "build_server",
    "input_error",
]

logger = logging.getLogger(__name__)

SERVER_NAME: Final[str] = "marketing-ai-verifier"
INSTRUCTIONS: Final[str] = (
    "Marketing AI's verifier, on the demo library only (public datasets and generated data). Read tools return "
    "figures the product has already computed and stored - test sizes, who may be contacted, what a campaign "
    "measured, a campaign's audit and Value Proof Pack - each with an evidence label and id. Propose tools draft a "
    "proposal that a person must approve in the product; nothing here approves, sends or writes anything. Small "
    "groups (fewer than 10 customers) are never shown."
)
CLIENT_INSTRUCTIONS: Final[str] = (
    "Marketing AI's verifier, on your own workspace's data. Read tools return figures the product has already "
    "computed and stored - test sizes, who may be contacted, what a campaign measured, a campaign's audit and "
    "Value Proof Pack, what the compliance gate allowed before a list was sent - each with an evidence label, its "
    "id and a receipt naming the ledger entry it was recorded under: keep the receipts, they let you check later "
    "that the record was not rewritten. Propose tools draft a proposal that a person must approve in the product; "
    "nothing here approves, sends or writes anything. Small groups (fewer than 10 customers) are never shown, and "
    "no customer's row is ever returned."
)
META_PREFIX: Final[str] = "marketing-ai/"


def input_error(tool: AgentTool[Any, Any], exc: ValidationError) -> AgenticError:
    """`TOOL_INPUT_INVALID` worded as the registry words it (the field path and pydantic's message)."""
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}" for error in exc.errors()
    )
    return AgenticError("TOOL_INPUT_INVALID", f"{tool.name}: {problems}", status=422)


@dataclass(frozen=True)
class _Answer:
    result: ToolResult
    structured: dict[str, Any]
    withheld: dict[str, int]
    receipt: dict[str, Any] | None = None


@dataclass
class Verifier:
    """The MCP surface over the agent-safe core. Transport-free: `build_server` puts the SDK in front of it."""

    registry: ToolRegistry
    settings: McpSettings
    context_for: Callable[[McpCaller], ToolContext]
    """The core's context for a caller (`engine.agentic.service.agentic_context`); refuses while it is off."""
    limiter: RateLimiter
    audit_log: AuditLog | None = None
    tasks: TaskStore | None = None
    """M141: where task records live (`api.mcp.tasks`); None serves every call synchronously."""

    # -- the surface -------------------------------------------------------------------------------------
    def exposed(self) -> tuple[AgentTool[Any, Any], ...]:
        """Every registered read or propose tool whose route's role maps to a scope."""
        return tuple(
            tool
            for tool in self.registry.list()
            if tool.kind in (ToolKind.READ, ToolKind.PROPOSE) and scope_for(tool) is not None
        )

    def definition(self, tool: AgentTool[Any, Any]) -> types.Tool:
        reads = tool.kind is ToolKind.READ
        what = (
            "Reads figures the product has already computed; changes nothing."
            if reads
            else "Drafts a proposal that a person must approve in the product; nothing changes until then."
        )
        return types.Tool(
            name=tool.name,
            description=f"{tool.description} {what}",
            input_schema=tool.input_model.model_json_schema(mode="validation"),
            output_schema=tool.output_model.model_json_schema(mode="serialization"),
            annotations=types.ToolAnnotations(
                title=tool.name.replace("_", " ").capitalize(),
                read_only_hint=reads,
                destructive_hint=False,
                idempotent_hint=reads,
                open_world_hint=False,
            ),
            _meta={f"{META_PREFIX}kind": tool.kind.value, f"{META_PREFIX}scope": scope_for(tool)},
        )

    def tools_for(self, caller: McpCaller) -> list[types.Tool]:
        """The tools this caller's scopes allow, by name."""
        return [self.definition(tool) for tool in self.exposed() if scope_for(tool) in caller.scopes]

    def tasks_on(self) -> bool:
        """Whether this server answers long tools as tasks to clients that support them (M141)."""
        return self.tasks is not None and bool(self.settings.tasks.tools)

    # -- the workspace -----------------------------------------------------------------------------------
    def _workspace(self, caller: McpCaller) -> ClientWorkspace:
        """The caller's client workspace on a client-data server (`MCP_WORKSPACE_DENIED` when it has none)."""
        from api.mcp.workspace import denied

        found = next((ws for ws in self.settings.workspaces if ws.id == caller.workspace), None)
        if found is None or caller.principal.user_id not in found.members:
            raise denied("workspace")
        return found

    def _check_scope_of_request(self, caller: McpCaller, meta: Mapping[str, Any] | None) -> None:
        if self.settings.serves_clients:
            from api.mcp.workspace import check_named

            check_named(self._workspace(caller), meta)
        else:
            check_workspace(self.settings, meta)

    def _guard(self, parsed: Any, ctx: ToolContext, caller: McpCaller, *, reads: bool) -> None:
        if self.settings.serves_clients:
            from api.mcp.workspace import guard as workspace_guard

            workspace_guard(parsed, ctx, self._workspace(caller), self.settings, reads=reads)
        else:
            guard(parsed, ctx, self.settings, reads=reads)

    # -- one call ----------------------------------------------------------------------------------------
    async def call_tool(
        self,
        caller: McpCaller,
        name: str,
        arguments: Mapping[str, Any] | None,
        meta: Mapping[str, Any] | None = None,
    ) -> types.CallToolResult:
        given: dict[str, Any] = dict(arguments or {})
        gate = Gate.for_arguments(given)
        exposed = {tool.name: tool for tool in self.exposed()}
        tool = exposed.get(name)
        try:
            lease = self.limiter.acquire(caller.token_key)
        except AgenticError as exc:
            return await self._refused(caller, tool, exc, gate)
        handed_over = False
        try:
            if tool is None:
                raise AgenticError(
                    "TOOL_UNKNOWN", "There is no tool of that name on this server.", status=404
                )
            scope = scope_for(tool)
            if scope not in caller.scopes:
                raise AgenticError(
                    "MCP_SCOPE_DENIED",
                    f"This tool needs the {scope} scope, which this sign-in does not have: it maps from the "
                    "Analyst role. No scope approves anything.",
                    status=403,
                )
            self._check_scope_of_request(caller, meta)
            if self.tasks_on() and tool.name in self.settings.tasks.tools:
                from api.mcp.tasks import asks_for_tasks

                if asks_for_tasks(dict(meta) if meta is not None else None):
                    handed_over = True
                    handle = await anyio.to_thread.run_sync(
                        partial(self._start_task, caller, tool, given, gate, lease)
                    )
                    # A claimed extension result (`resultType: task`): the runner serialises it as it is.
                    return cast(types.CallToolResult, handle)
            handed_over = True
            with anyio.fail_after(self.settings.call_timeout_seconds):
                answer = await anyio.to_thread.run_sync(
                    partial(self._work, caller, tool, given, gate, lease), abandon_on_cancel=True
                )
        except TimeoutError:
            return await self._refused(caller, tool, self._late(), gate)
        except AgenticError as exc:
            return await self._refused(caller, tool, exc, gate)
        except Exception as exc:  # a fault inside the product: answered with a code, never its message
            logger.error("mcp tool failed tool=%s error=%s", name[:64], type(exc).__name__)
            return await self._refused(caller, tool, self._failed(), gate)
        finally:
            if not handed_over:
                lease.release()
        return await anyio.to_thread.run_sync(partial(self._answer, caller, tool, answer, gate))

    def _late(self) -> AgenticError:
        return AgenticError(
            "MCP_TIMEOUT",
            f"The tool did not finish within {self.settings.call_timeout_seconds:g} seconds. Ask again later, ask "
            "a narrower question, or use a client that supports tasks for long audits.",
            status=504,
        )

    @staticmethod
    def _failed() -> AgenticError:
        return AgenticError(
            "MCP_TOOL_FAILED",
            "The tool failed inside the product; nothing was changed. The failure is logged and audited.",
            status=500,
        )

    def _answer(
        self,
        caller: McpCaller,
        tool: AgentTool[Any, Any],
        answer: _Answer,
        gate: Gate,
        task_id: str | None = None,
    ) -> types.CallToolResult:
        """The result of a call that succeeded, audited (runs on a worker thread)."""
        self._audit(caller, tool, "success", None, answer.structured, task_id)
        withheld = dict(answer.withheld)
        if gate.withheld:
            withheld["text"] = gate.withheld
        result_meta: dict[str, Any] = {
            f"{META_PREFIX}evidence_id": answer.result.evidence_id,
            f"{META_PREFIX}label": answer.result.label,
        }
        if answer.receipt is not None:
            result_meta[f"{META_PREFIX}receipt"] = answer.receipt
        if withheld:
            result_meta[f"{META_PREFIX}withheld"] = withheld
        text = render(tool.name, answer.result.label, answer.result.evidence_id, answer.structured, withheld)
        if answer.receipt is not None:
            text += (
                f"\nRecorded on the ledger as entry {answer.receipt['ledger_seq']} "
                f"({answer.receipt['ledger_checkpoint'][:16]}...); keep the receipt in _meta to check it later."
            )
        return types.CallToolResult(
            content=[types.TextContent(text=text)],
            structured_content=answer.structured,
            is_error=False,
            _meta=result_meta,
        )

    def _work(
        self, caller: McpCaller, tool: AgentTool[Any, Any], given: dict[str, Any], gate: Gate, lease: Lease
    ) -> _Answer:
        """Everything that reads storage, on a worker thread. The lease is released when it ends."""
        try:
            ctx = self.context_for(caller)
            try:
                parsed = tool.input_model.model_validate(given)
            except ValidationError as exc:
                raise input_error(tool, exc) from exc
            self._guard(parsed, ctx, caller, reads=tool.kind is ToolKind.READ)
            wide, wanted = widen(tool.name, given)
            result = self.registry.call(tool.name, wide, ctx)
            output, withheld = suppress(tool.name, result.output, minimum=self.settings.min_group_rows)
            if wanted is not None:
                output = narrow(tool.name, output, wanted)
            gated = gate.value(output)
            try:
                structured = tool.output_model.model_validate(gated).model_dump(mode="json")
            except ValidationError as exc:
                raise AgenticError(
                    "MCP_RESULT_WITHHELD",
                    "The result held text from outside the product in a field that cannot be left out, so it "
                    "is not served.",
                    status=502,
                ) from exc
            receipt = self._receipt(result, ctx)
            return _Answer(result=result, structured=structured, withheld=withheld, receipt=receipt)
        finally:
            lease.release()

    @staticmethod
    def _receipt(result: ToolResult, ctx: ToolContext) -> dict[str, Any] | None:
        """The receipt of the ledger entry the answer was recorded under (M141), with its evidence id."""
        entry = result.ledger_entry
        if entry is None or ctx.ledger is None:
            return None
        from engine.agentic.receipts import receipt_for

        kept = receipt_for(entry, ctx.ledger.engine).model_dump(mode="json")
        return {**kept, "evidence_id": result.evidence_id}

    # -- tasks (M141) ------------------------------------------------------------------------------------
    def _start_task(
        self, caller: McpCaller, tool: AgentTool[Any, Any], given: dict[str, Any], gate: Gate, lease: Lease
    ) -> Any:
        """Record a task, start its work on a worker thread, and answer the handle (`api.mcp.tasks`)."""
        from api.mcp.tasks import TaskHandle
        from engine.utils.time import utc_now

        assert self.tasks is not None  # tasks_on() was checked
        try:
            record = self.tasks.create(
                token_key=caller.token_key,
                workspace=caller.workspace,
                tool=tool.name,
                now=utc_now(),
                timeout_seconds=self.settings.call_timeout_seconds,
                ttl_seconds=self.settings.tasks.ttl_seconds,
                poll_interval_ms=self.settings.tasks.poll_interval_ms,
            )
            worker = threading.Thread(
                target=self._run_task,
                args=(record.task_id, caller, tool, given, gate, lease),
                name=f"mcp-task-{record.task_id}",
                daemon=True,
            )
            worker.start()
        except BaseException:
            lease.release()
            raise
        return TaskHandle.model_validate({"task": record.wire()})

    def _run_task(
        self,
        task_id: str,
        caller: McpCaller,
        tool: AgentTool[Any, Any],
        given: dict[str, Any],
        gate: Gate,
        lease: Lease,
    ) -> None:
        """The task's work: the same pipeline as a synchronous call, its result stored on the record."""
        from engine.utils.time import utc_now

        assert self.tasks is not None
        failed = False
        try:
            try:
                answer = self._work(caller, tool, given, gate, lease)
                result = self._answer(caller, tool, answer, gate, task_id)
            except AgenticError as exc:
                result = self._refusal(caller, tool, exc, gate, task_id)
            except Exception as exc:  # a fault inside the product: a code, never its message
                logger.error("mcp task failed tool=%s error=%s", tool.name, type(exc).__name__)
                result, failed = self._refusal(caller, tool, self._failed(), gate, task_id), True
            self.tasks.finish(
                task_id, result.model_dump(mode="json", by_alias=True), now=utc_now(), failed=failed
            )
        except Exception as exc:  # the record could not be written: the task fails at its deadline
            logger.error("mcp task record not written task=%s error=%s", task_id, type(exc).__name__)

    def task_error(self, code: str, message: str) -> types.CallToolResult:
        """The result `tasks/result` answers for a task that ended without one (timed out or cancelled)."""
        meta: dict[str, Any] = {f"{META_PREFIX}code": code}
        return types.CallToolResult(
            content=[types.TextContent(text=render_error(code, message))], is_error=True, _meta=meta
        )

    # -- refusals and audit ------------------------------------------------------------------------------
    async def _refused(
        self, caller: McpCaller, tool: AgentTool[Any, Any] | None, exc: AgenticError, gate: Gate
    ) -> types.CallToolResult:
        return await anyio.to_thread.run_sync(partial(self._refusal, caller, tool, exc, gate))

    def _refusal(
        self,
        caller: McpCaller,
        tool: AgentTool[Any, Any] | None,
        exc: AgenticError,
        gate: Gate,
        task_id: str | None = None,
    ) -> types.CallToolResult:
        outcome = "denied" if exc.status in (401, 403, 429) else "failed"
        self._audit(caller, tool, outcome, exc.code, None, task_id)
        message = gate.message(exc.message)
        meta: dict[str, Any] = {f"{META_PREFIX}code": exc.code, f"{META_PREFIX}status": exc.status}
        retry = exc.detail.get("retry_after_seconds")
        if isinstance(retry, int):
            meta[f"{META_PREFIX}retry_after_seconds"] = retry
        return types.CallToolResult(
            content=[types.TextContent(text=render_error(exc.code, message))], is_error=True, _meta=meta
        )

    def _audit(
        self,
        caller: McpCaller,
        tool: AgentTool[Any, Any] | None,
        outcome: str,
        code: str | None,
        served: dict[str, Any] | None,
        task_id: str | None = None,
        action: str = "mcp.tool_call",
    ) -> None:
        """One `AuditEvent` per call: the principal, the client, the tool, the outcome and its code, and as
        `after_hash` the sha256 of the canonical structured content served (`engine.agentic.canonical`).

        A failure to write is logged and counted, never raised - as the audit middleware does (DEC-719): the
        caller has its answer either way, and a missing event shows as an ERROR line, not a silent gap.
        """
        if self.audit_log is None:
            return
        from engine.agentic.canonical import digest
        from engine.audit.store import record_event

        details: dict[str, str | int | float | bool | None] = {
            "route": f"mcp:{tool.name}" if tool is not None else "mcp:unknown",
            "client_id": caller.client_id,
            "request_kind": "mcp",
        }
        if code is not None:
            details["reason_code"] = code
        if task_id is not None:
            # `AuditEvent.details` takes a closed set of keys (DEC-705): a task is named as what triggered the
            # work. The workspace follows from the principal (`mcp.workspaces`).
            details["trigger"] = f"mcp-task:{task_id}"
        try:
            record_event(
                self.audit_log,
                caller.principal,
                action,
                object_type="tool",
                object_id=tool.name if tool is not None else None,
                outcome=outcome,
                after_hash=None if served is None else digest(served),
                details=details,
            )
        except Exception as exc:
            logger.error(
                "mcp audit event could not be written outcome=%s error=%s", outcome, type(exc).__name__
            )


def _task_methods(server: Server[Any], verifier: Verifier, caller_of: Callable[..., McpCaller]) -> None:
    """`tasks/get`, `tasks/result` and `tasks/cancel` (M141, `api.mcp.tasks`), for the caller's own tasks."""
    from api.mcp.tasks import TASKS_EXTENSION, TERMINAL
    from engine.utils.time import utc_now

    store = verifier.tasks
    assert store is not None

    def missing() -> types.ErrorData:
        return types.ErrorData(
            code=types.INVALID_PARAMS,
            message="There is no such task for this session (MCP_TASK_NOT_FOUND).",
            data={"code": "MCP_TASK_NOT_FOUND"},
        )

    async def get_task(ctx: ServerRequestContext[Any, Any], params: types.GetTaskRequestParams) -> Any:
        caller = caller_of(ctx)
        record = await anyio.to_thread.run_sync(
            partial(store.get, params.task_id, token_key=caller.token_key, now=utc_now())
        )
        return missing() if record is None else types.GetTaskResult.model_validate(record.wire())

    async def task_result(
        ctx: ServerRequestContext[Any, Any], params: types.GetTaskPayloadRequestParams
    ) -> Any:
        caller = caller_of(ctx)
        record = None
        with anyio.move_on_after(verifier.settings.call_timeout_seconds):
            while True:
                record = await anyio.to_thread.run_sync(
                    partial(store.get, params.task_id, token_key=caller.token_key, now=utc_now())
                )
                if record is None or record.status in TERMINAL:
                    break
                await anyio.sleep(record.poll_interval_ms / 1000)
        if record is None:
            return missing()
        if record.status == "working":
            return types.ErrorData(
                code=types.INVALID_PARAMS,
                message="The task is still working; poll tasks/get and ask again.",
                data={"code": "MCP_TIMEOUT"},
            )
        if record.result is not None:
            return types.CallToolResult.model_validate(record.result)
        if record.status == "cancelled":
            return verifier.task_error(
                "MCP_TASK_CANCELLED", "The task was cancelled; its result was discarded."
            )
        return verifier.task_error(
            "MCP_TIMEOUT",
            f"The task did not finish within {verifier.settings.call_timeout_seconds:g} seconds; ask again later.",
        )

    async def cancel_task(ctx: ServerRequestContext[Any, Any], params: types.CancelTaskRequestParams) -> Any:
        caller = caller_of(ctx)
        record = await anyio.to_thread.run_sync(
            partial(store.cancel, params.task_id, token_key=caller.token_key, now=utc_now())
        )
        if record is None:
            return missing()
        if record.status != "cancelled":
            return types.ErrorData(
                code=types.INVALID_PARAMS,
                message=f"The task has already ended ({record.status}); there is nothing to cancel.",
                data={"code": "MCP_TASK_CANCELLED"},
            )
        tool = next((t for t in verifier.exposed() if t.name == record.tool), None)
        await anyio.to_thread.run_sync(
            partial(verifier._audit, caller, tool, "success", None, None, record.task_id, "mcp.task_cancel")
        )
        return types.CancelTaskResult.model_validate(record.wire())

    server.add_request_handler("tasks/get", types.GetTaskRequestParams, get_task)
    server.add_request_handler("tasks/result", types.GetTaskPayloadRequestParams, task_result)
    server.add_request_handler("tasks/cancel", types.CancelTaskRequestParams, cancel_task)
    server.extensions[TASKS_EXTENSION] = {}


def build_server(
    verifier: Verifier, caller_of: Callable[[ServerRequestContext[Any, Any]], McpCaller]
) -> Server[Any]:
    """The SDK's low-level `Server` over a verifier; `caller_of` says who a request is (the transport knows)."""
    from engine import __version__
    from mcp.server import Server

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=verifier.tools_for(caller_of(ctx)), ttl_ms=0, cache_scope="private"
        )

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        meta = dict(params.meta) if params.meta is not None else None
        return await verifier.call_tool(caller_of(ctx), params.name, params.arguments, meta)

    clients = verifier.settings.serves_clients
    server: Server[Any] = Server(
        SERVER_NAME,
        version=__version__,
        title=(
            "Marketing AI verifier (client workspaces)" if clients else "Marketing AI verifier (demo library)"
        ),
        instructions=CLIENT_INSTRUCTIONS if clients else INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    if verifier.tasks_on():
        _task_methods(server, verifier, caller_of)
    return server
