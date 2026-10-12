"""Data Doctor's one tool registry (Plan O M232, DEC-1802; the design §5).

What it proves: the registry holds the 22 existing tools (21 tools and the conversation loop that calls them) and
the first wrappers, each matching its row of the design's catalogue; APPLY can never be registered; a PROPOSE tool
must require at least an Analyst and name its policy rule set; a Viewer calling a PROPOSE tool is refused on that
call; the same input gives the same content-hash evidence id in every run and every process, spelled so the egress
gate never mistakes it for a number; and the MCP surface lists no session-only tool (Plan O lists none, O7).
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pandas as pd
import pytest
from pydantic import BaseModel, ConfigDict

from api.access_policy import RoutePolicy
from engine.access.roles import Principal, Role
from engine.agent.contracts import Proposal, ProposalSource, ProposalState, ToolResult
from engine.agent.egress import assert_clean
from engine.agent.session import start_session
from engine.agent.tools import TOOLS, AgentContext, ToolKind, call_tool
from engine.doctor import registry as registry_module
from engine.doctor.catalogue import CATALOGUE, row
from engine.doctor.diagnose import LoadedFile, Offer, diagnose_file, readiness
from engine.doctor.registry import (
    EXECUTOR_NAMES,
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
    data_evidence_id,
    data_registry,
    evidence_id_of,
    unified_on,
)
from engine.doctor.tools import LookOutput
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.helpers import context_for

LOOK_TOOLS = [name for name, tool in TOOLS.items() if tool.kind is ToolKind.READ]
CHAT_PROPOSE = ["propose_setting", "propose_step"]
FIRST_WRAPPERS = {
    10: "diagnose_files",
    29: "find_file_role",
    32: "advise",
    33: "first_look",
    34: "how_files_fit",
    36: "what_can_this_power",
    37: "enough_data",
    38: "data_request",
    40: "find_time_columns",
    71: "recall_treatment",
}
EVIDENCE_ID = re.compile(r"ev_[a-p]{16}")


def _person(*roles: Role) -> Principal:
    return Principal(
        user_id=f"u-{'-'.join(r.value for r in roles)}", username="u", roles=frozenset(roles), kind="user"
    )


VIEWER = _person(Role.VIEWER)
ANALYST = _person(Role.ANALYST)


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return generate(GenerationSpec(use_case_id="targeted-advertisement", rows=1_200, seed=7))


@pytest.fixture(scope="module")
def agent(frame: pd.DataFrame) -> AgentContext:
    return context_for(frame)


def _loaded(ctx: AgentContext) -> LoadedFile:
    return LoadedFile(
        upload_id=ctx.upload_id, file_name="customers.csv", frame=ctx.frame, profile=ctx.profile
    )


class _Turn:
    """A chat turn's binding that records whether a handler ran."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def propose(self, name: str, args: dict[str, Any]) -> tuple[ToolResult, Proposal]:
        self.ran.append(name)
        raise AssertionError("a refused call must never reach the handler")


# ---------------------------------------------------------------------------
# What is registered
# ---------------------------------------------------------------------------
def test_the_registry_holds_the_22_existing_tools_and_the_first_wrappers() -> None:
    names = set(data_registry().names())
    assert names == {*LOOK_TOOLS, *CHAT_PROPOSE, "edit_by_hand", *FIRST_WRAPPERS.values()}
    assert len(LOOK_TOOLS) == 18
    # The 22nd existing tool is the conversation loop: it is the caller, not a tool kind.
    assert row("chat_turn") is not None and row("chat_turn").kind is None  # type: ignore[union-attr]
    assert "chat_turn" not in names and "ask_your_data" not in names
    # Plan G's look tools keep `TOOLS` order, so the chat's tool list is the same list.
    assert [t.name for t in data_registry().list(kind=DataToolKind.READ)][:18] == LOOK_TOOLS


def test_every_registered_tool_matches_its_catalogue_row() -> None:
    for tool in data_registry().list():
        entry = row(tool.name)
        assert entry is not None, tool.name
        assert entry.number == tool.number, tool.name
        assert entry.kind == tool.kind.value, tool.name
        assert entry.exposure == tool.exposure.value, tool.name
        assert entry.status in {
            "existing",
            "wrapper",
        }, tool.name  # nothing new is registered before it is built
        if tool.number in FIRST_WRAPPERS:
            assert entry.status == "wrapper" and FIRST_WRAPPERS[tool.number] == tool.name
        assert tool.wraps, tool.name


def test_the_catalogue_is_the_designs_eighty_tools() -> None:
    counted = [entry for entry in CATALOGUE if entry.counted_with is None]
    assert len(CATALOGUE) == 81 and [e.number for e in CATALOGUE] == list(range(1, 82))
    assert len(counted) == 80
    assert Counter(e.status for e in counted) == {"existing": 22, "wrapper": 29, "new": 29}
    kinds = Counter(e.kind for e in counted)
    assert kinds["propose"] == 24 and kinds["ask"] == 4
    assert {e.name for e in CATALOGUE if e.exposure == "aggregate"} == {
        "what_can_this_power",
        "check_readiness",
    }
    assert len({e.name for e in CATALOGUE}) == 81
    assert not EXECUTOR_NAMES & {e.name for e in CATALOGUE}, "an executor is never in the catalogue"


def test_only_aggregate_tools_may_be_listed_on_mcp_and_plan_o_lists_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert data_registry().mcp_surface() == ()
    monkeypatch.setattr(registry_module, "MCP_LISTED", True)
    listed = data_registry().mcp_surface()
    assert listed, "a later plan could list the aggregate verdicts"
    assert all(tool.exposure is Exposure.AGGREGATE for tool in listed)
    assert {tool.name for tool in listed} == {"what_can_this_power"}


def test_the_mcp_surface_lists_no_data_doctor_tool(tmp_path: Path) -> None:
    """The real MCP server's `tools/list`, as an Admin with every scope: no Data Doctor tool is there."""
    from api.mcp.config import McpSettings
    from api.mcp.limits import RateLimiter
    from api.mcp.scopes import McpCaller, scopes_for, token_key
    from api.mcp.server import Verifier
    from api.mcp.transport import InProcessTransport
    from engine.agentic.registry import ToolContext
    from engine.agentic.tools import default_registry
    from engine.storage import LocalStorage

    admin = _person(Role.VIEWER, Role.ANALYST, Role.APPROVER, Role.ADMIN)

    def context_for(caller: McpCaller) -> ToolContext:
        return ToolContext(
            storage=LocalStorage(tmp_path),
            principal=caller.principal,
            config_root=tmp_path,
            clock=lambda: datetime(2026, 10, 12, tzinfo=UTC),
        )

    verifier = Verifier(
        registry=default_registry(),
        settings=McpSettings(),
        context_for=context_for,
        limiter=RateLimiter(calls_per_minute=100, concurrent_calls=2),
    )
    caller = McpCaller(principal=admin, scopes=scopes_for(admin), token_key=token_key("t"), client_id="mcp:t")

    async def listed() -> list[str]:
        async with InProcessTransport(verifier, caller).connect() as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    names = set(anyio.run(listed))
    assert names, "the MCP surface lists Plan K's tools"
    data = data_registry()
    session_only = {tool.name for tool in data.list(exposure=Exposure.SESSION_ONLY)}
    assert not names & session_only
    assert not names & set(data.names()), "Plan O lists no Data Doctor tool on MCP (O7)"


# ---------------------------------------------------------------------------
# What registration refuses
# ---------------------------------------------------------------------------
class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Lax(BaseModel):
    pass


def _tool(**changes: Any) -> DataTool[Any, Any]:
    values: dict[str, Any] = {
        "number": 52,
        "name": "impossible_values",
        "kind": DataToolKind.READ,
        "description": "A test tool.",
        "input_model": _In,
        "output_model": LookOutput,
        "route_policy": RoutePolicy(role=Role.VIEWER, action="test.look", purpose="look"),
        "exposure": Exposure.SESSION_ONLY,
        "run": lambda _given, _ctx: LookOutput({"rows": 1}),
    }
    values.update(changes)
    return DataTool(**values)


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"name": "apply_prescription"}, "APPLY is never a tool"),
        ({"name": "replay_prescription"}, "APPLY is never a tool"),
        ({"name": "apply_steps"}, "APPLY is never a tool"),
        ({"name": "remember"}, "APPLY is never a tool"),
        ({"name": "send_offer"}, "APPLY is never a tool"),
        ({"name": "Bad-Name"}, "snake_case"),
        ({"kind": "apply"}, "only read, propose and ask"),
        ({"input_model": _Lax}, "refuse unknown fields"),
        ({"route_policy": RoutePolicy(role=None, action="x")}, "no tool is public"),
        (
            {"kind": DataToolKind.PROPOSE, "policy_kind": "data.x"},
            "a Viewer may call READ tools only",
        ),
        (
            {
                "kind": DataToolKind.PROPOSE,
                "route_policy": RoutePolicy(role=Role.ANALYST, action="x"),
            },
            "policy rule set",
        ),
        ({"kind": DataToolKind.ASK}, "a Viewer may call READ tools only"),
        ({"callers": frozenset({"mcp"})}, "callers"),
        ({"number": 99}, "catalogue"),
        (
            {
                "kind": DataToolKind.PROPOSE,
                "policy_kind": "x",
                "session_tool": True,
                "route_policy": RoutePolicy(role=Role.ANALYST, action="x"),
            },
            "session tools",
        ),
    ],
)
def test_registration_refuses_what_the_design_forbids(changes: dict[str, Any], words: str) -> None:
    with pytest.raises(ValueError, match=words):
        DataToolRegistry().register(_tool(**changes))


def test_a_name_is_registered_once() -> None:
    registry = DataToolRegistry()
    registry.register(_tool())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_tool())


# ---------------------------------------------------------------------------
# Role per call
# ---------------------------------------------------------------------------
def _full_context(
    agent: AgentContext, principal: Principal, caller: str = "person", **more: Any
) -> DataToolContext:
    session = start_session(agent, session_id="s-1")
    return DataToolContext(
        agent=agent,
        principal=principal,
        caller=caller,  # type: ignore[arg-type]
        files=(_loaded(agent),),
        session=session,
        turn=_Turn(),
        **more,
    )


@pytest.mark.parametrize("name", [t.name for t in data_registry().list(kind=DataToolKind.PROPOSE)])
def test_a_viewer_calling_a_propose_tool_is_refused_on_that_call(agent: AgentContext, name: str) -> None:
    tool = data_registry().get(name)
    caller = "person" if "person" in tool.callers else "ai"
    ctx = _full_context(agent, VIEWER, caller)
    with pytest.raises(DataToolError) as refused:
        data_registry().call(name, {"kind": "drop_column", "column": "x"}, ctx)
    assert refused.value.code == "TOOL_ROLE_REQUIRED" and refused.value.status == 403
    assert "Only an Analyst can" in refused.value.message
    assert ctx.turn is not None and ctx.turn.ran == []  # type: ignore[attr-defined]


def test_every_propose_tool_needs_at_least_an_analyst() -> None:
    for tool in data_registry().list():
        if tool.kind is not DataToolKind.READ:
            assert tool.route_policy.role in {Role.ANALYST, Role.APPROVER, Role.ADMIN}, tool.name


def test_an_analyst_may_edit_by_hand_through_the_registry(agent: AgentContext) -> None:
    ctx = _full_context(agent, ANALYST)
    column = next(
        str(c) for c in agent.frame.columns if agent.frame[c].dtype == object and c != "customer_id"
    )
    result = data_registry().call("edit_by_hand", {"kind": "drop_column", "column": column}, ctx)
    proposal = Proposal.model_validate(result.output["proposal"])
    assert proposal.source is ProposalSource.PERSON and proposal.state is ProposalState.ACCEPTED
    assert result.kind is DataToolKind.PROPOSE and result.exposure is Exposure.SESSION_ONLY


def test_a_viewer_may_call_an_aggregate_read_tool_and_not_a_row_level_one(agent: AgentContext) -> None:
    ctx = DataToolContext(agent=agent, principal=VIEWER, caller="person", files=(_loaded(agent),))
    verdicts = data_registry().call("what_can_this_power", {}, ctx)
    assert verdicts.exposure is Exposure.AGGREGATE and verdicts.output["use_cases"]
    with pytest.raises(DataToolError) as refused:
        data_registry().call("sample_rows", {}, ctx)
    assert refused.value.code == "TOOL_ROLE_REQUIRED"


def test_the_ai_never_sees_a_person_only_tool_or_a_wrapper_in_m232(agent: AgentContext) -> None:
    ctx = _full_context(agent, ANALYST, "ai")
    assert data_registry().find("edit_by_hand", ctx) is None
    for name in FIRST_WRAPPERS.values():
        assert data_registry().find(name, ctx) is None, name
    with pytest.raises(DataToolError) as refused:
        data_registry().call("edit_by_hand", {"kind": "drop_column", "column": "x"}, ctx)
    assert refused.value.code == "TOOL_UNKNOWN"
    listed = [t.name for t in data_registry().listed_for(ctx, DataToolKind.READ)]
    assert listed == [name for name in LOOK_TOOLS if name != "draft_outcome_labels"]  # Plan K off


def test_ai_and_person_calls_must_say_who_is_asking(agent: AgentContext) -> None:
    for caller in ("ai", "person"):
        ctx = DataToolContext(agent=agent, caller=caller)
        with pytest.raises(DataToolError) as refused:
            data_registry().call("value_counts", {"column": "plan_tier"}, ctx)
        assert refused.value.code == "TOOL_ROLE_REQUIRED" and refused.value.status == 401
    rules = DataToolContext(agent=agent)  # the engine's own rules, inside a route that checked the person
    assert data_registry().call("value_counts", {"column": "plan_tier"}, rules).output


def test_unknown_tools_bad_input_and_missing_needs_are_refused_with_codes(agent: AgentContext) -> None:
    ctx = DataToolContext(agent=agent, principal=ANALYST, caller="person")
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("no_such_tool", {}, "TOOL_UNKNOWN"),
        ("value_counts", {"column": "plan_tier", "sneaky": 1}, "TOOL_INPUT_INVALID"),
        ("how_files_fit", {}, "TOOL_UNKNOWN"),  # needs the case's files
        ("recall_treatment", {}, "TOOL_UNKNOWN"),  # needs storage
    ]
    for name, given, code in cases:
        with pytest.raises(DataToolError) as refused:
            data_registry().call(name, given, ctx)
        assert refused.value.code == code, name
    # A refusal inside a Plan G tool keeps its own code.
    with pytest.raises(Exception) as inner:
        data_registry().call("value_counts", {"column": "no such column"}, ctx)
    assert getattr(inner.value, "code", None) == "AGENT_COLUMN_UNKNOWN"


def test_a_result_over_its_budget_is_refused() -> None:
    registry = DataToolRegistry()
    registry.register(
        _tool(run=lambda _g, _c: LookOutput({"rows": ["x" * 100] * 200}), max_output_bytes=1_000)
    )
    with pytest.raises(DataToolError) as refused:
        registry.call("impossible_values", {}, DataToolContext())
    assert refused.value.code == "TOOL_OUTPUT_TOO_LARGE" and refused.value.status == 413


# ---------------------------------------------------------------------------
# Content-hash evidence ids
# ---------------------------------------------------------------------------
_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("value_counts", {"column": "plan_tier"}),
    ("describe_numbers", {"column": "tenure_months"}),
    ("diagnose_files", {}),
    ("find_time_columns", {}),
]


def _ids(agent: AgentContext) -> list[str]:
    ctx = DataToolContext(agent=agent, files=(_loaded(agent),))
    return [data_registry().call(name, given, ctx).evidence_id for name, given in _CALLS]


_CHILD = """
import json, sys
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.helpers import context_for
from tests.unit.doctor.test_registry import _ids
print(json.dumps(_ids(context_for(generate(GenerationSpec(use_case_id="targeted-advertisement", rows=1_200, seed=7))))))
"""


def test_the_same_input_gives_the_same_evidence_id_across_runs_and_processes(agent: AgentContext) -> None:
    here = _ids(agent)
    assert here == _ids(agent), "the same call twice gives the same id"
    assert all(EVIDENCE_ID.fullmatch(e) for e in here)
    assert len(set(here)) == len(here)
    root = Path(__file__).resolve().parents[3]
    child = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": "12345", "PATH": "/usr/bin:/bin", "PYTHONPATH": str(root)},
    )
    assert json.loads(child.stdout.strip().splitlines()[-1]) == here


def test_a_different_input_or_output_gives_a_different_id(agent: AgentContext) -> None:
    ctx = DataToolContext(agent=agent)
    one = data_registry().call("value_counts", {"column": "plan_tier"}, ctx).evidence_id
    top = data_registry().call("value_counts", {"column": "plan_tier", "top": 3}, ctx).evidence_id
    other = data_registry().call("value_counts", {"column": "region"}, ctx).evidence_id
    assert len({one, top, other}) == 3
    assert data_evidence_id("t", {"a": 1}, {"b": 2}) != data_evidence_id("t", {"a": 1}, {"b": 3})


def test_the_evidence_id_is_plan_ks_hash_spelled_in_letters() -> None:
    from engine.agentic.registry import evidence_id_for

    given, output = {"column": "x"}, {"rows": 3}
    hex_id = evidence_id_for("value_counts", given, output)[3:]
    letters = data_evidence_id("value_counts", given, output)[3:]
    assert letters == "".join("abcdefghijklmnop"["0123456789abcdef".index(c)] for c in hex_id)


def test_no_evidence_id_is_ever_masked_by_the_egress_gate() -> None:
    """A hex id holds a run of nine digits about one time in twenty; the gate would mask it. Letters never are."""
    rng = random.Random(232)
    ids = [data_evidence_id("t", {"n": rng.random()}, {"k": i}) for i in range(2_000)]
    prompt = json.dumps({"results": [{"evidence_id": e} for e in ids]})
    assert assert_clean(prompt) == (prompt, 0)
    assert assert_clean(" ".join(ids)) == (" ".join(ids), 0)


def test_evidence_id_of_a_plan_g_result_is_its_content_hash(agent: AgentContext) -> None:
    first = call_tool(agent, "value_counts", {"column": "plan_tier"}, evidence_id="c1-e1")
    second = call_tool(agent, "value_counts", {"column": "plan_tier"}, evidence_id="c9-e4")
    assert first.evidence_id != second.evidence_id
    assert evidence_id_of(first) == evidence_id_of(second)
    through = data_registry().call("value_counts", {"column": "plan_tier"}, DataToolContext(agent=agent))
    assert through.evidence_id == evidence_id_of(first)


def test_session_call_returns_plan_gs_result_byte_for_byte(agent: AgentContext) -> None:
    clock = lambda: datetime(2026, 10, 12, tzinfo=UTC)  # noqa: E731
    fixed = AgentContext(**{**agent.__dict__, "clock": clock})
    for name, given in [("value_counts", {"column": "plan_tier"}), ("get_profile", {}), ("find_roles", {})]:
        direct = call_tool(fixed, name, given, evidence_id="e7")
        through = data_registry().session_call(name, given, DataToolContext(agent=fixed), evidence_id="e7")
        assert through.model_dump_json() == direct.model_dump_json(), name
    with pytest.raises(DataToolError) as refused:
        data_registry().session_call("advise", {}, DataToolContext(agent=fixed), evidence_id="e8")
    assert refused.value.code == "AGENT_TOOL_UNKNOWN"


def test_a_ledger_records_each_calls_evidence_label(agent: AgentContext) -> None:
    class Ledger:
        def __init__(self) -> None:
            self.entries: list[tuple[str, str, dict[str, Any]]] = []

        def append(self, kind: str, actor: str, body: dict[str, Any]) -> None:
            self.entries.append((kind, actor, body))

    ledger = Ledger()
    ctx = DataToolContext(agent=agent, ledger=ledger, actor="agent:data-doctor")  # type: ignore[arg-type]
    result = data_registry().call("value_counts", {"column": "plan_tier"}, ctx)
    [(kind, actor, body)] = ledger.entries
    assert kind == "evidence.label" and actor == "agent:data-doctor"
    assert body["evidence_id"] == result.evidence_id and body["tool"] == "value_counts"
    assert set(body) == {"evidence_id", "tool", "label", "input_digest", "output_digest", "artefact_refs"}


# ---------------------------------------------------------------------------
# Wrappers answer what the wrapped function answers
# ---------------------------------------------------------------------------
def test_diagnose_files_and_what_can_this_power_equal_the_wrapped_functions(agent: AgentContext) -> None:
    from engine.config import config_root, get_roles

    loaded = _loaded(agent)
    ctx = DataToolContext(agent=agent, files=(loaded,))
    diagnosed = data_registry().call("diagnose_files", {}, ctx).output["files"]
    assert diagnosed == [diagnose_file(loaded, agent.config, get_roles(None)).model_dump(mode="json")]
    verdicts = data_registry().call("what_can_this_power", {}, ctx).output["use_cases"]
    direct = readiness([loaded], [Offer(config=agent.config, stage="")], config_root(None))
    assert verdicts == [v.model_dump(mode="json") for v in direct]


def test_unified_on_reads_the_switch_and_is_off_without_it(agent: AgentContext) -> None:
    class Section:
        unified = True

    class Config:
        data_doctor = Section()

    assert unified_on(agent.config) is False, "the shipped configuration has the switch off"
    assert unified_on(None) is False
    assert unified_on(Config()) is True  # type: ignore[arg-type]
