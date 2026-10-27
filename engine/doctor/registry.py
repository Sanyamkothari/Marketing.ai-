"""Data Doctor's one tool registry (Plan O M232, DEC-1802; the design §5).

Every data capability Data Doctor has is a **tool** in one registry, of exactly three kinds:

* **READ** looks and returns evidence; it changes nothing.
* **PROPOSE** returns a pending item - a step, a setting, a role, an edit - that a person decides. Nothing
  changes until then.
* **ASK** opens a question, a doubt, an approval request or a data request.

**APPLY is never a tool** (the design §5.1; Plan L's promise 1, Plan K's invariant I1). The executors that change
data (`apply_prescription`, `replay_prescription`, the builder's `build` and `replay`, `memory.remember`) are bound
to an approved prescription and are refused here by name (:data:`EXECUTOR_NAMES`, :data:`FORBIDDEN_PREFIXES`).

The shape is Plan K's `AgentTool` (`engine.agentic.registry`): a name, a kind, typed input and output models,
the `RoutePolicy` whose role every call is checked against, and a run function. Data Doctor adds what data needs:

* :class:`DataToolContext` - Plan G's `AgentContext` (the frame, its profile, the use case), Plan K's principal,
  storage and optional ledger, the case, the egress gate, and the files of a case for the tools that read several.
* **Exposure** per tool: `session-only` (returns a cell, a row or a masked example; callable inside a case, never
  listed on MCP) or `aggregate` (counts and verdicts only; may be listed on MCP in a later plan). Plan O lists no
  Data Doctor tool on MCP at all (decision O7): :meth:`DataToolRegistry.mcp_surface` is empty.
* **Role per call.** A Viewer may call READ tools only: a PROPOSE or ASK tool must require at least an Analyst
  (:meth:`DataToolRegistry.register` refuses one that does not), and every call checks the principal against the
  tool's `RoutePolicy` (`TOOL_ROLE_REQUIRED`), as Plan K does.
* **Who may call.** Each tool says which callers it serves: the rules, the AI, the person. The AI never sees a
  person-only tool (`edit_by_hand`), and in M232 the wrappers serve the rules and the screen only (the AI reaches
  them through the visit, M235).
* **Content-hash evidence ids.** :func:`data_evidence_id` is Plan K's `evidence_id_for` - SHA-256 over the
  canonical JSON of `[tool, typed input, output]`, 64 bits kept - spelled in the letters `a`-`p` instead of hex
  digits, so the egress gate's last check (`engine.agent.egress.assert_clean`, which masks any run of nine
  digits) can never mistake an id for a number. The same question on the same data gives the same id in every
  run and every process, which is what pass^k needs (the design §5.2; Plan G's counter ids block it).

Plan G's chat and rules advisor keep their session ids (`c1-e1`, `e3`): the screen, Ask your data's charts and
every stored session cite them. Behind `data_doctor.unified` (default off) they call their tools through this
registry (`session_call`, `call`); the content-hash id is what the registry's own callers - the visit (M235), the
readiness report (M234), the evals (M248) - cite. With the switch off nothing here is reached.

Errors: :class:`DataToolError` is a Plan G `AgentToolError`, so the chat loop feeds it back to the model as it does
any refusal. The registry's own refusals use Plan K's codes, which have help entries: `TOOL_UNKNOWN`,
`TOOL_INPUT_INVALID`, `TOOL_ROLE_REQUIRED`, `TOOL_OUTPUT_TOO_LARGE`. The Plan G-shaped `session_call` keeps Plan
G's own codes (`AGENT_TOOL_UNKNOWN`, `AGENT_TOOL_ARGS_INVALID`), so what the model reads is unchanged.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Generic, Literal, Protocol, TypeVar

from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from engine.access.roles import ROLE_DESCRIPTIONS, Principal, Role
from engine.agent.contracts import RecipeStepKind
from engine.agent.tools import AgentContext, AgentToolError, call_tool
from engine.agentic.canonical import canonical_bytes, canonical_json, digest, sha256_hex
from engine.agentic.registry import DEFAULT_OUTPUT_BUDGET
from engine.config import StrictBase
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from api.access_policy import RoutePolicy
    from engine.agent.contracts import AgentSession, Proposal, ToolResult
    from engine.agent.egress import Egress
    from engine.agentic.ledger import DecisionLedger
    from engine.clients import ClientStore
    from engine.config import RoleCatalogue, UseCaseConfig
    from engine.connections.store import ConnectionStore
    from engine.doctor.case import CaseItem
    from engine.doctor.diagnose import LoadedFile, Offer
    from engine.doctor.explain import Narrator
    from engine.doctor.locale import LocaleProfile
    from engine.doctor.readiness import CheckReadinessArgs, ReadinessInputs
    from engine.doctor.tools_formats import FormatToolContext
    from engine.storage import Storage

__all__ = [
    "AGGREGATE_BUDGET",
    "BY_EXPOSURE",
    "CALLERS",
    "EXECUTOR_NAMES",
    "FORBIDDEN_PREFIXES",
    "MCP_LISTED",
    "ROW_LEVEL_BUDGET",
    "CaseHandle",
    "ChatTurn",
    "DataTool",
    "DataToolContext",
    "DataToolError",
    "DataToolKind",
    "DataToolRegistry",
    "DataToolResult",
    "Exposure",
    "data_evidence_id",
    "data_registry",
    "evidence_id_of",
    "output_size",
    "unified_on",
]

ROW_LEVEL_BUDGET: Final[int] = 5_500
"""Characters of canonical JSON a row-level (`session-only`) tool may return through `call`: the design §5.2,
Plan G's look-tool cap (`engine.agent.tools.MAX_LOOK_RESULT_CHARS`). The default budget of every `session-only`
tool (`DataTool.budget`)."""
AGGREGATE_BUDGET: Final[int] = DEFAULT_OUTPUT_BUDGET
"""Bytes of canonical JSON an `aggregate` tool may return (Plan K's 16 KB): the default budget of every
`aggregate` tool."""
BY_EXPOSURE: Final = "by-exposure"
"""`DataTool.max_output_bytes`'s default: the budget follows the tool's exposure (see `DataTool.budget`)."""
MCP_LISTED: Final[bool] = False
"""Decision O7: Plan O lists no Data Doctor tool on MCP. Even when a later plan sets this, only `aggregate`
tools are ever listed (:meth:`DataToolRegistry.mcp_surface`)."""

EXECUTOR_NAMES: Final[frozenset[str]] = frozenset(
    {
        "apply_prescription",
        "replay_prescription",
        "apply_agent_session",
        "replay_for_scoring",
        "build",
        "replay",
        "remember",
    }
)
"""The executors that change data, bound to an approved prescription. Never a tool (the design §5.1, §5.5)."""
FORBIDDEN_PREFIXES: Final[tuple[str, ...]] = (
    "apply",
    "replay",
    "execute",
    "approve",
    "write",
    "delete",
    "send",
    "export",
)
"""Name prefixes no tool may have: a name that says it changes or sends something is refused at registration."""

Caller = Literal["rules", "ai", "person"]
CALLERS: Final[frozenset[str]] = frozenset({"rules", "ai", "person"})
_MIN_PROPOSE_ROLE: Final[frozenset[Role]] = frozenset({Role.ANALYST, Role.APPROVER, Role.ADMIN})
_LAST_ROW: Final[int] = 86
"""The catalogue's last row (`engine.doctor.catalogue`): #82 and #83 are M240's tools, given rows in M236;
#84 is `propose_sql_step`, #85 `preview_working_copy`, #86 `propose_features`."""
_LETTERS: Final[dict[int, int]] = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


class DataToolKind(StrEnum):
    """What a tool may do. There is deliberately no APPLY member (the design §5.1)."""

    READ = "read"
    PROPOSE = "propose"
    ASK = "ask"


class Exposure(StrEnum):
    """Where a tool may be called from (the design §5.3)."""

    SESSION_ONLY = "session-only"
    """Inside a case, by the rules, the AI and the screen. Never listed on MCP."""
    AGGREGATE = "aggregate"
    """Counts and verdicts only; may be listed on MCP in a later plan, through Plan K's gate."""


class DataToolError(AgentToolError):
    """A call the registry refused. A Plan G `AgentToolError` (the chat feeds it back), with an HTTP status."""

    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(code, message)
        self.status = status


class CaseHandle(Protocol):
    """The smallest view of a case (M231's `Case`) the registry needs: whose it is, for scope and the ledger."""

    @property
    def case_id(self) -> str: ...

    @property
    def client_id(self) -> str: ...


class ChatTurn(Protocol):
    """What the CONVERSE loop binds for the duration of one turn: Plan G's chat PROPOSE handlers.

    `propose_setting` and `propose_step` need the turn's own state (the evidence gathered this turn, the
    grounding checker, the session's settings). The loop binds them here (`engine.agent.loop`) and calls the
    tools through the registry like any other; outside a turn the two tools are not available. The loop's
    binding hands Plan G's handler the arguments exactly as the model sent them, not the registry's validated
    copy (`args`), so a suggestion is what Plan G alone would make of them.
    """

    def propose(self, name: str, args: dict[str, Any]) -> tuple[ToolResult, Proposal]: ...


@dataclass(frozen=True)
class DataToolContext:
    """What a data tool may read, and who is asking (the design §5.2).

    Plan G's `AgentContext` (one upload's frame, profile and use case) + Plan K's principal, storage and optional
    ledger + the case + the egress gate. Each field is optional because each tool needs different ones; a tool
    whose needs are not met is not available in that context (`DataTool.needs`).
    """

    agent: AgentContext | None = None
    """One file: its rows (capped), profile and use case. The look tools and the rules read it."""
    principal: Principal | None = None
    """Who is asking. None only for the engine's own rules inside a route that has already checked the person
    (Plan G's advisor); the AI and the person always carry one."""
    caller: Caller = "rules"
    storage: Storage | None = None
    ledger: DecisionLedger | None = None
    """When given, every call's evidence label is appended to it, as Plan K's registry does (O9, additive)."""
    case: CaseHandle | None = None
    egress: Egress | None = None
    """The turn's gate. The registry never sends anything itself; whoever puts a result in a prompt uses this."""
    files: tuple[LoadedFile, ...] = ()
    """The files of a case, for the tools that read several (`diagnose_files`, `how_files_fit`, ...)."""
    offers: tuple[Offer, ...] = ()
    """The use cases a verdict is given for; empty means the use case of `agent` alone."""
    roles: RoleCatalogue | None = None
    session: AgentSession | None = None
    save_session: Callable[[AgentSession], None] | None = None
    """Where a tool that changes the session (`edit_by_hand`) hands the new one: the caller stores it, under its
    own lock and version check (the edits route's `_session_lock` and `_save`). A tool that needs it is not
    available without it, so an edit can never be made and lost."""
    turn: ChatTurn | None = None
    config_root: Path | None = None
    clock: Callable[[], datetime] = utc_now
    actor: str = "agent:data-doctor"
    """Who the ledger names."""
    # Plan O wave 1 integration (DEC-1802 (t)): what the tools of M234, M237, M238, M240 and M241 read beyond the
    # fields above. Each is optional; a tool that needs one is not available without it.
    replacing: tuple[str, ...] = ()
    """Case items the caller is taking back or swapping in this very call: a query's trial leaves them out."""
    primary_key: str | None = None
    """The file's ID column as decided in the case or session (M238's and M241's tools never change it)."""
    as_of: date | None = None
    """The case's as-of date (`decisions(case).as_of`): a query is tried with the date a run gives it."""
    training_as_of: date | None = None
    """The case's training snapshot (`decisions(case).training_as_of_date`): where an outcome query is cut."""
    locale_profile: LocaleProfile | None = None
    """The client's confirmed locale profile (M241), when there is one."""
    case_files: FormatToolContext | None = None
    """The case's files by id, inside its client scope (M240's tools; `engine.doctor.case_files.CaseFiles`)."""
    readiness: Callable[[CheckReadinessArgs], ReadinessInputs] | None = None
    """The readiness report's inputs for the case's prepared copy (M234's `check_readiness`), built by the
    caller with `engine.doctor.readiness.inputs_for_upload`."""
    # Plan O M236 (DEC-1806): what the remaining wrappers and `explain` read. Each is optional; a tool that needs
    # one is not available without it.
    connections: ConnectionStore | None = None
    """The deployment's saved connections (`preview_connection`, `propose_import`)."""
    client_store: ClientStore | None = None
    """The onboarding registry of sources, mappings and recipes (`check_refresh`)."""
    meter: Any = None
    """The metered AI service (`engine.generative.budget.Meter`) for `second_opinion`; None with no AI service."""
    guardrails: Any = None
    """The deterministic guardrails (`engine.generative.guardrails.Guardrails`) the AI's words pass."""
    evidence: tuple[DataToolResult, ...] = ()
    """Results of this case's earlier registry calls, recorded by the server (`explain`, `request_approval`
    cite them by evidence id; a model never supplies a result)."""
    narrator: Narrator | None = None
    """The AI service's writer of `explain`'s structured narrative; None means the rules' narrative."""
    case_items: tuple[CaseItem, ...] | None = None
    """The case's items as they stand (the section on screen): the plan `preview_working_copy` tries on the files.
    None where the caller did not hand them over: the tool is then not available."""

    def require_agent(self) -> AgentContext:
        if self.agent is None:  # unreachable through the registry: `needs` makes the tool unavailable
            raise DataToolError("TOOL_UNKNOWN", "This tool needs a file.", status=404)
        return self.agent

    def root(self) -> Path | None:
        """The configuration root: the context's own, else the file's."""
        if self.config_root is not None:
            return self.config_root
        return self.agent.config_root if self.agent is not None else None


Need = Literal[
    "agent",
    "files",
    "storage",
    "session",
    "save_session",
    "turn",
    "case_files",
    "readiness",
    "connections",
    "client_store",
    "meter",
    "evidence",
    "case_items",
]

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


def _always(_ctx: DataToolContext) -> bool:
    return True


@dataclass(frozen=True)
class DataTool(Generic[In, Out]):
    """One registry entry: Plan K's `AgentTool` shape plus exposure, callers and needs (Plan O §5)."""

    number: int
    """The tool's row in the design's catalogue (§5.5), 1 to 83."""
    name: str
    kind: DataToolKind
    description: str
    input_model: type[In]
    output_model: type[Out]
    route_policy: RoutePolicy
    """The role every call is checked against, and what the refusal says (`purpose`)."""
    exposure: Exposure
    run: Callable[[In, DataToolContext], Out]
    callers: frozenset[str] = frozenset(CALLERS)
    needs: tuple[Need, ...] = ()
    available: Callable[[DataToolContext], bool] = _always
    """Beyond `needs`: whether the tool exists in this context (Plan G's M142 tools exist only with Plan K on)."""
    mirrors: tuple[str, str] | None = None
    """`(method, path)` of the HTTP route serving the same data, when there is one."""
    max_output_bytes: int | Literal["by-exposure"] | None = BY_EXPOSURE
    """The most a result may be through `call` (`TOOL_OUTPUT_TOO_LARGE` beyond it). By default it follows the
    exposure: `ROW_LEVEL_BUDGET` characters for `session-only`, `AGGREGATE_BUDGET` bytes for `aggregate`. A tool
    may name its own number when it holds no client data (`data_request`). `None` is for a PROPOSE tool that
    runs a Plan G handler which bounds its own suggestion (`check_recipe`'s 500 spellings, the settings list):
    the registry adds no cap of its own, so the suggestion is what Plan G would make."""
    session_tool: bool = False
    """True for Plan G's look tools: `session_call` runs them as Plan G's `call_tool` does, byte for byte."""
    policy_kind: str | None = None
    """For PROPOSE: the policy rule set that decides it (M233 reads it; default deny when it has no rules)."""
    step_kinds: tuple[RecipeStepKind, ...] = ()
    """The step kinds its results propose (M238's and M240's READ tools return pending steps beside their
    evidence). A tool that declares any is decided by the data policy like a PROPOSE tool (M233, DEC-1803)."""
    wraps: str = ""
    """The engine function it wraps, as file:line (the design §5.5)."""

    @property
    def proposal_kind(self) -> str | None:
        """The policy kind its results are decided by: a PROPOSE tool's `policy_kind`, or - for a READ tool that
        returns pending steps (`step_kinds`) - its own (`policy.policy_kind_for`); None for a tool proposing
        nothing."""
        if self.policy_kind:
            return self.policy_kind
        if self.kind is DataToolKind.PROPOSE or self.step_kinds:
            from engine.doctor.policy import policy_kind_for

            return policy_kind_for(self.name)
        return None

    @property
    def budget(self) -> int | None:
        """The result's cap through `call`: characters for `session-only`, bytes for `aggregate`; None: none."""
        if self.max_output_bytes == BY_EXPOSURE:
            return ROW_LEVEL_BUDGET if self.exposure is Exposure.SESSION_ONLY else AGGREGATE_BUDGET
        return self.max_output_bytes


class DataToolResult(StrictBase):
    """One call's result through `call`: the evidence a proposal, a question or a sentence cites."""

    evidence_id: str = Field(description='"ev_" + 16 letters a-p: a content hash (`data_evidence_id`).')
    tool: str
    kind: DataToolKind
    exposure: Exposure
    input: dict[str, Any]
    output: dict[str, Any]
    computed_at: AwareDatetime = Field(description="When it was computed; not part of evidence_id.")


def data_evidence_id(tool: str, given: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    """The content-hash evidence id of `tool` answering `given` with `output`.

    Plan K's `evidence_id_for` (`sha256(canonical([tool, input, output]))`, 16 hex characters), each hex digit
    spelled as a letter `a`-`p`: the same 64 bits, and never a run of digits the egress gate would mask.
    """
    return "ev_" + sha256_hex(canonical_bytes([tool, given, output]))[:16].translate(_LETTERS)


def output_size(exposure: Exposure, output: Any) -> int:
    """How big `output` is against its budget: characters of canonical JSON for a `session-only` result (the
    design §5.2's "5,500 characters", as Plan G counts), UTF-8 bytes of it for an `aggregate` one (Plan K)."""
    text = canonical_json(output)
    return len(text) if exposure is Exposure.SESSION_ONLY else len(text.encode("utf-8"))


def evidence_id_of(result: ToolResult) -> str:
    """The content-hash id of a Plan G tool result: its tool, its validated arguments and its result."""
    return data_evidence_id(result.tool, result.args, result.result)


def unified_on(config: UseCaseConfig | None) -> bool:
    """Whether `data_doctor.unified` (M230; default off) is on for `config`.

    Read defensively: before M230's setting is merged a configuration has no `data_doctor` section, which reads
    as off. Callers look it up on this module at call time, so a test can switch it on.
    """
    from engine.settings import settings

    if settings().data_doctor_unified:  # MARKETING_AI_DATA_DOCTOR_UNIFIED: the test installation turns it on
        return True
    section = getattr(config, "data_doctor", None)
    return bool(getattr(section, "unified", False))


class DataToolRegistry:
    """The data tools, by name, in registration order: `register`, `get`, `list`, `find`, `call`."""

    def __init__(self) -> None:
        self._tools: dict[str, DataTool[Any, Any]] = {}

    # -- registration ----------------------------------------------------------------------------------
    def register(self, tool: DataTool[Any, Any]) -> None:
        """Add a tool; refuse anything the design forbids (see the module docstring)."""
        name = tool.name
        if not isinstance(tool.kind, DataToolKind):
            raise ValueError(f"Tool {name!r} has kind {tool.kind!r}; only read, propose and ask exist.")
        if name in self._tools:
            raise ValueError(f"A tool called {name!r} is already registered.")
        if not name.replace("_", "").isalnum() or name != name.lower() or not name[:1].isalpha():
            raise ValueError(f"Tool names are snake_case: {name!r}.")
        if name in EXECUTOR_NAMES or name.startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"{name!r} names an executor; APPLY is never a tool (the design §5.1).")
        if tool.input_model.model_config.get("extra") != "forbid":
            raise ValueError(f"Tool {name!r}'s input model must refuse unknown fields.")
        role = tool.route_policy.role
        if role is None:
            raise ValueError(f"Tool {name!r} must require a role; no tool is public.")
        if tool.kind is not DataToolKind.READ and Role(role) not in _MIN_PROPOSE_ROLE:
            raise ValueError(f"Tool {name!r} is {tool.kind.value}: a Viewer may call READ tools only.")
        if not tool.callers or not tool.callers <= CALLERS:
            raise ValueError(f"Tool {name!r} has callers {sorted(tool.callers)}; allowed: {sorted(CALLERS)}.")
        if tool.kind is DataToolKind.PROPOSE and not tool.policy_kind:
            raise ValueError(f"PROPOSE tool {name!r} must name its policy rule set (`policy_kind`).")
        if tool.session_tool and tool.kind is not DataToolKind.READ:
            raise ValueError(f"Only READ tools run as Plan G session tools: {name!r}.")
        if not 1 <= tool.number <= _LAST_ROW:
            raise ValueError(f"Tool {name!r} has no row in the catalogue (number {tool.number}).")
        budget = tool.max_output_bytes
        if budget is None and tool.kind is not DataToolKind.PROPOSE:
            raise ValueError(
                f"Tool {name!r} needs an output budget; only a Plan G suggestion bounds its own."
            )
        if isinstance(budget, int) and budget <= 0:
            raise ValueError(f"Tool {name!r} has an output budget of {budget}.")
        self._tools[name] = tool

    def get(self, name: str) -> DataTool[Any, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise DataToolError("TOOL_UNKNOWN", f"There is no tool called {name[:40]!r}.", status=404)
        return tool

    def list(
        self, *, kind: DataToolKind | None = None, exposure: Exposure | None = None
    ) -> tuple[DataTool[Any, Any], ...]:
        """Every tool, in registration order (Plan G's look tools first, in `TOOLS` order)."""
        return tuple(
            tool
            for tool in self._tools.values()
            if (kind is None or tool.kind is kind) and (exposure is None or tool.exposure is exposure)
        )

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def mcp_surface(self) -> tuple[DataTool[Any, Any], ...]:
        """The tools MCP may list: none in Plan O (O7); never a `session-only` one."""
        if not MCP_LISTED:
            return ()
        return self.list(exposure=Exposure.AGGREGATE)

    # -- availability and roles ------------------------------------------------------------------------
    def find(self, name: str, ctx: DataToolContext) -> DataTool[Any, Any] | None:
        """The tool `name` if this caller may call it here: registered, serving `ctx.caller`, its needs met."""
        tool = self._tools.get(name)
        if tool is None or ctx.caller not in tool.callers or not _met(tool, ctx):
            return None
        return tool if tool.available(ctx) else None

    def listed_for(self, ctx: DataToolContext, kind: DataToolKind) -> tuple[DataTool[Any, Any], ...]:
        """The tools of `kind` this caller may call in this context, in registration order."""
        return tuple(tool for tool in self.list(kind=kind) if self.find(tool.name, ctx) is tool)

    def check_role(self, tool: DataTool[Any, Any], ctx: DataToolContext) -> None:
        """Refuse a principal without the tool's role (`TOOL_ROLE_REQUIRED`, 403): the per-call check."""
        role = tool.route_policy.role
        if ctx.principal is None:
            if ctx.caller == "rules":
                return  # the engine's own rules, inside a route that checked the person
            raise DataToolError("TOOL_ROLE_REQUIRED", f"{tool.name} needs to know who is asking.", status=401)
        if role is not None and not ctx.principal.has(Role(role)):
            what = tool.route_policy.purpose or f"call {tool.name}"
            raise DataToolError("TOOL_ROLE_REQUIRED", f"Only {_article(Role(role))} can {what}.", status=403)

    # -- calls -----------------------------------------------------------------------------------------
    def call(self, name: str, given: Mapping[str, Any], ctx: DataToolContext) -> DataToolResult:
        """Check, run and record one call; its result carries a content-hash evidence id.

        In order: the tool exists for this caller here (`TOOL_UNKNOWN`), the principal holds its role
        (`TOOL_ROLE_REQUIRED`), the input validates and has no unknown field (`TOOL_INPUT_INVALID`), the output
        validates and fits the tool's budget (`TOOL_OUTPUT_TOO_LARGE`; `DataTool.budget`, measured by
        `output_size`). A refusal inside the tool propagates as it is (a Plan G `AgentToolError` keeps its code).
        """
        tool = self.find(name, ctx)
        if tool is None:
            raise DataToolError("TOOL_UNKNOWN", f"There is no tool called {name[:40]!r} here.", status=404)
        self.check_role(tool, ctx)
        kind = tool.proposal_kind
        if kind is not None:
            _require_rule_set(tool, kind, ctx)
        try:
            parsed = tool.input_model.model_validate(dict(given))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}"
                for error in exc.errors()
            )
            raise DataToolError("TOOL_INPUT_INVALID", f"{name}: {problems}"[:400], status=422) from exc
        produced = tool.run(parsed, ctx)
        output = tool.output_model.model_validate(produced.model_dump()).model_dump(mode="json")
        budget = tool.budget
        if budget is not None:
            size = output_size(tool.exposure, output)
            if size > budget:
                unit = "characters" if tool.exposure is Exposure.SESSION_ONLY else "bytes"
                raise DataToolError(
                    "TOOL_OUTPUT_TOO_LARGE",
                    f"{name} would return {size:,} {unit} of JSON, more than its {budget:,}; "
                    "ask a narrower question.",
                    status=413,
                )
        if kind is not None:
            _check_output(kind, output, ctx)
        given_json = parsed.model_dump(mode="json", exclude_none=True)
        result = DataToolResult(
            evidence_id=data_evidence_id(name, given_json, output),
            tool=name,
            kind=tool.kind,
            exposure=tool.exposure,
            input=given_json,
            output=output,
            computed_at=ctx.clock(),
        )
        if ctx.ledger is not None:
            from engine.agentic.ledger import EVIDENCE_LABEL

            ctx.ledger.append(
                EVIDENCE_LABEL,
                ctx.actor,
                {
                    "evidence_id": result.evidence_id,
                    "tool": name,
                    "label": "Descriptive",
                    "input_digest": digest(given_json),
                    "output_digest": digest(output),
                    "artefact_refs": [f.upload_id for f in ctx.files]
                    or ([ctx.agent.upload_id] if ctx.agent is not None else []),
                },
            )
        return result

    def session_call(
        self, name: str, given: Mapping[str, Any] | None, ctx: DataToolContext, *, evidence_id: str
    ) -> ToolResult:
        """A Plan G look tool through the registry, returning Plan G's own `ToolResult`, byte for byte.

        What Plan G's chat and rules advisor call behind `data_doctor.unified`: the registry decides whether the
        tool exists for this caller here and whether the principal may call it; Plan G's `call_tool` runs it with
        the caller's session id, so every stored session, prompt and digest is what it was. An unknown tool is
        Plan G's `AGENT_TOOL_UNKNOWN`, with Plan G's message.
        """
        tool = self.find(name, ctx)
        if tool is None or not tool.session_tool:
            raise DataToolError("AGENT_TOOL_UNKNOWN", f"There is no tool called {name!r}.", status=404)
        self.check_role(tool, ctx)
        return call_tool(ctx.require_agent(), name, given, evidence_id=evidence_id)


def _require_rule_set(tool: DataTool[Any, Any], kind: str, ctx: DataToolContext) -> None:
    """Plan O M233 (DEC-1803): default deny. A tool that proposes (PROPOSE, or a READ tool returning pending
    steps) whose kind has no rule set in the data policy (`configs/agentic/policy.yaml`, `data.rule_sets`) is
    refused before it runs (`POLICY_NO_RULES`), as is every such tool when the policy cannot be read. What it
    returns is then decided by :func:`_check_output`; the chat's and the person's kinds are decided in their
    handlers (`loop._propose_step`, `loop._propose`, `edits.add_edit`), where the facts they read are known.
    """
    from engine.agentic.errors import AgenticError
    from engine.doctor.policy import load_data_policy

    try:
        rule_sets = load_data_policy(ctx.root()).rule_sets
    except AgenticError:
        rule_sets = {}
    if kind not in rule_sets:
        raise DataToolError(
            "POLICY_NO_RULES", f"No policy rules are set for a {kind or tool.name} proposal.", status=403
        )


def _check_output(kind: str, output: Mapping[str, Any], ctx: DataToolContext) -> None:
    """Plan O M233 (DEC-1803): every proposal in a tool's output, decided by its kind's rule set after the tool
    ran and before its result is recorded; the first refusal is raised with its code (403). Never ticked unless
    the rules allow it, `na` never mapped, a reshape only through its question, an APPROVE-class item with its
    exact sentence, decision O10 on an AI's merge (`policy.check_tool_output`)."""
    from engine.doctor.policy import check_tool_output, tool_policy_context

    found = check_tool_output(kind, output, tool_policy_context(ctx, kind))
    if found is not None:
        raise DataToolError(
            found.code or "POLICY_NO_RULES", str(found.detail.get("reason", "Not allowed."))[:400], status=403
        )


def _met(tool: DataTool[Any, Any], ctx: DataToolContext) -> bool:
    for need in tool.needs:
        if need == "agent" and ctx.agent is None:
            return False
        if need == "files" and not ctx.files:
            return False
        if need == "storage" and ctx.storage is None:
            return False
        if need == "session" and ctx.session is None:
            return False
        if need == "save_session" and ctx.save_session is None:
            return False
        if need == "turn" and ctx.turn is None:
            return False
        if need == "case_files" and ctx.case_files is None:
            return False
        if need == "readiness" and ctx.readiness is None:
            return False
        if need == "connections" and ctx.connections is None:
            return False
        if need == "client_store" and ctx.client_store is None:
            return False
        if need == "meter" and ctx.meter is None:
            return False
        if need == "case_items" and ctx.case_items is None:
            return False
        if need == "evidence" and not ctx.evidence:
            return False
    return True


def _article(role: Role) -> str:
    label = ROLE_DESCRIPTIONS[role]
    return ("an " if label[:1] in "AEIOU" else "a ") + label


@functools.cache
def data_registry() -> DataToolRegistry:
    """The registry holding every Data Doctor tool built so far (`engine.doctor.tools`), built once."""
    from engine.doctor.tools import register_all

    registry = DataToolRegistry()
    register_all(registry)
    return registry
