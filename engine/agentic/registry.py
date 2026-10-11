"""The tool registry: what an agent may call, and nothing else (Plan K M132, design §1; invariant I1).

A tool is a typed, deterministic function over the engine's own artefacts. It has a **kind**, and there are
exactly two: `read` (computes and returns; no side effect but its ledger entry and, where its route is
audited, its audit event) and `propose`
(returns a proposal; nothing changes until a person approves it). There is deliberately no `write` member,
and :meth:`ToolRegistry.register` refuses anything that is not one of the two, so no tool can send, export
or change anything outside our storage (I1).

**Every call is checked and recorded** (:meth:`ToolRegistry.call`):

1. the tool exists (`TOOL_UNKNOWN`);
2. the principal holds the role of the tool's `route_policy` - the same `RoutePolicy` as the HTTP route that
   serves the same data, so an agent never sees more than the person could (`TOOL_ROLE_REQUIRED`);
3. the input validates against the tool's input model, which refuses unknown fields (`TOOL_INPUT_INVALID`);
4. the output validates against the output model and fits the output budget, 16 KB of canonical JSON by
   default (`TOOL_OUTPUT_TOO_LARGE`): aggregates only, row-level data never leaves a tool;
5. the result gets an `evidence_id`, a **content hash** - ``"ev_" + sha256(canonical([tool, input,
   output]))[:16]`` - so the same question on the same artefacts gives the same id (what makes pass^k testable
   and lets two agents cite one fact), and its evidence label goes on the ledger, signed (I5);
6. a call the mirrored route would audit (a `propose` tool, or a read whose policy has `audit_reads`) writes
   one `AuditEvent` - success, denied or failed - with the route's `action` and object, `route: tool:<name>`
   and `trigger: <agent>` (:func:`audited`), as the audit middleware writes one for the route's request.

A tool never calls a language model, so its result is a function of its input and the stored artefacts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Generic, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, Field, PrivateAttr, ValidationError

from engine.access.roles import Principal, Role
from engine.agentic.canonical import canonical_bytes, digest, sha256_hex
from engine.agentic.errors import AgenticError
from engine.config import StrictBase

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from api.access_policy import RoutePolicy
    from engine.agentic.ledger import DecisionLedger, LedgerEntry
    from engine.measurement.campaign import CampaignStore
    from engine.settings import Settings
    from engine.storage import Storage

__all__ = [
    "DEFAULT_OUTPUT_BUDGET",
    "AgentTool",
    "EvidenceLabel",
    "ToolContext",
    "ToolKind",
    "ToolRegistry",
    "ToolResult",
    "audited",
    "evidence_id_for",
]

DEFAULT_OUTPUT_BUDGET: Final[int] = 16 * 1024
"""The most canonical JSON a tool may return, in bytes (design §1.3)."""

EvidenceLabel = Literal["Causal", "Estimated", "Not proven", "Descriptive"]
"""What a result may claim: proven by a random control group, estimated by a model or a plan, not proven
(a statement nobody verified, or an early look), or a description with no claim on cause."""


class ToolKind(StrEnum):
    """What a tool may do. There is deliberately no WRITE member (I1)."""

    READ = "read"
    """Computes and returns; no side effect except audit and ledger entries."""
    PROPOSE = "propose"
    """Returns a proposal; nothing changes until a person approves it."""


In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


@dataclass(frozen=True)
class ToolContext:
    """What a tool may read, and who is asking. Built by the caller (a route, the harness, M133's server)."""

    storage: Storage
    principal: Principal
    config_root: Path
    clock: Callable[[], datetime]
    actor: str = "agent:core"
    """Who the ledger names: `agent:<name>` for an agent, the user id for a person's own call."""
    platform_engine: Engine | None = None
    campaigns: CampaignStore | None = None
    settings: Settings | None = None
    ledger: DecisionLedger | None = None
    policy_rules: Mapping[str, Any] | None = None
    """The policy's rules by name; None is `engine.agentic.policy.RULES`, every rule. Only the eval harness's
    mutation test passes a map with one rule removed (`engine.agentic.evals`); with a rule removed the others
    still decide, and a kind none of them decides is refused by default deny."""


class ToolResult(StrictBase):
    """One call's result: the evidence a proposal or a sentence may cite by `evidence_id`."""

    evidence_id: str = Field(
        description='"ev_" + sha256(canonical([tool, input, output]))[:16]: a content hash.'
    )
    tool: str
    kind: ToolKind
    input: dict[str, Any]
    output: dict[str, Any]
    label: EvidenceLabel
    computed_at: AwareDatetime = Field(description="When it was computed; not part of evidence_id.")
    artefact_refs: tuple[str, ...] = Field(description="Run ids, campaign ids and plan hashes it rests on.")
    _ledger_entry: Any = PrivateAttr(default=None)

    @property
    def ledger_entry(self) -> LedgerEntry | None:
        """The `evidence.label` entry this result was recorded under (Plan K M141: every MCP answer names it);
        None when the context had no ledger. Not a field: the result's JSON is what it was."""
        entry: LedgerEntry | None = self._ledger_entry
        return entry


def evidence_id_for(tool: str, given: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    """The content-hash evidence id of `tool` answering `given` with `output`."""
    return "ev_" + sha256_hex(canonical_bytes([tool, given, output]))[:16]


@dataclass(frozen=True)
class AgentTool(Generic[In, Out]):
    """One tool: a typed wrapper over an existing engine function. No new statistics live here."""

    name: str
    kind: ToolKind
    description: str
    input_model: type[In]
    output_model: type[Out]
    route_policy: RoutePolicy
    """The same role and audit rules as the HTTP route that serves the same data (`mirrors`): the role is
    checked by :meth:`ToolRegistry.call`, and a call is audited exactly when that route's request is."""
    mirrors: tuple[str, str]
    """`(method, path)` of that route; `tests/unit/agentic/test_registry.py` checks the policy is identical."""
    evidence_label: Callable[[Out], EvidenceLabel]
    run: Callable[[In, ToolContext], Out]
    artefact_refs: Callable[[In, Out], tuple[str, ...]] = field(default=lambda _given, _out: ())
    max_output_bytes: int = DEFAULT_OUTPUT_BUDGET


class ToolRegistry:
    """The tools an agent may call, by name. `register`, `get`, `list` and `call`."""

    def __init__(self) -> None:
        self._tools: dict[str, AgentTool[Any, Any]] = {}

    def register(self, tool: AgentTool[Any, Any]) -> None:
        """Add a tool; refuse a duplicate name, a kind that is not read or propose, or a lax input model."""
        if not isinstance(tool.kind, ToolKind) or tool.kind.value not in {
            member.value for member in ToolKind
        }:
            raise ValueError(f"Tool {tool.name!r} has kind {tool.kind!r}; only read and propose exist.")
        if tool.name in self._tools:
            raise ValueError(f"A tool called {tool.name!r} is already registered.")
        if not tool.name.replace("_", "").isalnum() or tool.name != tool.name.lower():
            raise ValueError(f"Tool names are snake_case: {tool.name!r}.")
        if tool.input_model.model_config.get("extra") != "forbid":
            raise ValueError(f"Tool {tool.name!r}'s input model must refuse unknown fields.")
        if tool.route_policy.role is None:
            raise ValueError(f"Tool {tool.name!r} must require a role; no tool is public.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool[Any, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise AgenticError("TOOL_UNKNOWN", f"There is no tool called {name!r}.", status=404)
        return tool

    def list(self) -> tuple[AgentTool[Any, Any], ...]:
        """Every tool, by name."""
        return tuple(self._tools[name] for name in sorted(self._tools))

    def call(self, name: str, given: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        """Check, run and record one call (see the module docstring)."""
        tool = self.get(name)
        try:
            result = self._call(tool, given, ctx)
        except AgenticError as exc:
            _audit(
                tool, given, ctx, outcome="denied" if exc.status in (401, 403) else "failed", code=exc.code
            )
            raise
        _audit(tool, given, ctx, outcome="success", code=None)
        return result

    def _call(self, tool: AgentTool[Any, Any], given: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        role = tool.route_policy.role
        if role is not None and not ctx.principal.has(Role(role)):
            raise AgenticError(
                "TOOL_ROLE_REQUIRED",
                f"Only a {Role(role).value.capitalize()} can {tool.route_policy.purpose or 'call ' + tool.name}.",
                status=403,
            )
        try:
            parsed = tool.input_model.model_validate(dict(given))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}"
                for error in exc.errors()
            )
            raise AgenticError("TOOL_INPUT_INVALID", f"{tool.name}: {problems}", status=422) from exc
        produced = tool.run(parsed, ctx)
        output_model = tool.output_model.model_validate(produced.model_dump())
        output = output_model.model_dump(mode="json")
        size = len(canonical_bytes(output))
        if size > tool.max_output_bytes:
            raise AgenticError(
                "TOOL_OUTPUT_TOO_LARGE",
                f"{tool.name} would return {size:,} bytes, more than its {tool.max_output_bytes:,}-byte budget; "
                "ask a narrower question.",
                status=413,
                detail={"bytes": size, "budget": tool.max_output_bytes},
            )
        input_json = parsed.model_dump(mode="json")
        label = tool.evidence_label(output_model)
        refs = tuple(sorted(set(tool.artefact_refs(parsed, output_model))))
        result = ToolResult(
            evidence_id=evidence_id_for(tool.name, input_json, output),
            tool=tool.name,
            kind=tool.kind,
            input=input_json,
            output=output,
            label=label,
            computed_at=ctx.clock(),
            artefact_refs=refs,
        )
        if ctx.ledger is not None:
            from engine.agentic.ledger import EVIDENCE_LABEL

            result._ledger_entry = ctx.ledger.append(
                EVIDENCE_LABEL,
                ctx.actor,
                {
                    "evidence_id": result.evidence_id,
                    "tool": tool.name,
                    "label": label,
                    "input_digest": digest(input_json),
                    "output_digest": digest(output),
                    "artefact_refs": list(refs),
                },
            )
        return result


def audited(tool: AgentTool[Any, Any]) -> bool:
    """Whether a call of `tool` writes an audit event: exactly when its mirrored route's request would.

    The audit middleware writes one event for every mutating request and for every read whose policy says
    `audit_reads` (`api.access`); a `propose` tool mirrors a POST, so it is audited, and a `read` tool is
    audited when its route's policy audits reads (the artefact downloads: `check_contactable`,
    `value_segment`).
    """
    return tool.kind is ToolKind.PROPOSE or tool.route_policy.audit_reads


def _audit(
    tool: AgentTool[Any, Any], given: Mapping[str, Any], ctx: ToolContext, *, outcome: str, code: str | None
) -> None:
    """One `AuditEvent` for one audited call, on the platform database (`engine.audit.store`).

    The same fields the middleware writes for the route - the principal, the route's `action`, its object -
    with `route` naming the tool and `trigger` the agent that called it. Nothing is written without a
    platform database (an in-memory context), as nothing is audited without one.
    """
    if not audited(tool) or ctx.platform_engine is None:
        return
    from engine.audit.store import SqlAuditLog, record_event

    policy = tool.route_policy
    raw = given.get(policy.object_param) if policy.object_param else None
    details: dict[str, str | int | float | bool | None] = {
        "route": f"tool:{tool.name}",
        "trigger": ctx.actor[:200],
    }
    if policy.purpose:
        details["purpose"] = policy.purpose[:200]
    if code is not None:
        details["reason_code"] = code
    record_event(
        SqlAuditLog(ctx.platform_engine),
        ctx.principal,
        policy.action,
        object_type=policy.object_type,
        object_id=raw if isinstance(raw, str) and len(raw) <= 64 else None,
        outcome=outcome,
        details=details,
        clock=ctx.clock,
    )
