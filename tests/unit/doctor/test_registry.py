"""Data Doctor's one tool registry (Plan O M232, DEC-1802; the design §5).

What it proves: the registry holds the 22 existing tools (21 tools and the conversation loop that calls them) and
the first wrappers, each matching its row of the design's catalogue; APPLY can never be registered; a PROPOSE tool
must require at least an Analyst and name its policy rule set; a Viewer calling a PROPOSE tool is refused on that
call; the same input gives the same content-hash evidence id in every run and every process, spelled so the egress
gate never mistakes it for a number; and the MCP surface lists no session-only tool (Plan O lists none, O7).
"""

from __future__ import annotations

import dataclasses
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
from engine.doctor import tools as tools_module
from engine.doctor.catalogue import CATALOGUE, row
from engine.doctor.diagnose import LoadedFile, Offer, diagnose_file, readiness
from engine.doctor.registry import (
    AGGREGATE_BUDGET,
    EXECUTOR_NAMES,
    ROW_LEVEL_BUDGET,
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
from engine.doctor.tools_features import M_FEATURE_TOOLS
from engine.doctor.tools_outcomes import M239_TOOLS
from engine.doctor.tools_preview import M_PREVIEW_TOOLS
from engine.doctor.tools_sql import M_SQL_TOOLS
from engine.doctor.tools_time import M244_TOOLS
from engine.doctor.tools_wave_one import WAVE_ONE_TOOLS
from engine.doctor.tools_wave_two import WAVE_TWO_TOOLS
from engine.doctor.tools_wrappers import M236_TOOLS
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
    assert names == {
        *LOOK_TOOLS,
        *CHAT_PROPOSE,
        "edit_by_hand",
        *FIRST_WRAPPERS.values(),
        *WAVE_ONE_TOOLS,
        *M236_TOOLS,  # Plan O M236 (DEC-1806): the remaining wrappers and `explain`
        *WAVE_TWO_TOOLS,  # Plan O M242 (DEC-1812 (k)): `convert_currency`
        *M239_TOOLS,  # Plan O M239 (DEC-1809): the outcome tools
        *M244_TOOLS,  # Plan O M244 (DEC-1814): the time-settings tools
        *M_SQL_TOOLS,  # the sandboxed SQL step
        *M_FEATURE_TOOLS,  # behaviour features
        *M_PREVIEW_TOOLS,  # the agent looks at its own work
    }
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
        # Nothing new is registered before it is built: the new tools are wave 1's (DEC-1802 (t)), M236's
        # `explain` (DEC-1806), wave 2's (M242's `convert_currency`), M239's outcome tools (DEC-1809) and M244's
        # time-settings tools (DEC-1814).
        built_new = {
            *WAVE_ONE_TOOLS,
            "explain",
            *WAVE_TWO_TOOLS,
            *M239_TOOLS,
            *M244_TOOLS,
            *M_SQL_TOOLS,
            *M_FEATURE_TOOLS,
            *M_PREVIEW_TOOLS,
        }
        assert entry.status in {"existing", "wrapper"} or tool.name in built_new, tool.name
        if tool.number in FIRST_WRAPPERS:
            assert entry.status == "wrapper" and FIRST_WRAPPERS[tool.number] == tool.name
        assert tool.wraps, tool.name


def test_the_catalogue_is_the_designs_eighty_tools() -> None:
    """The design's 80 tools in 81 rows, and since Plan O M236 (DEC-1806) M240's two READ tools as #82, #83."""
    counted = [entry for entry in CATALOGUE if entry.counted_with is None]
    assert len(CATALOGUE) == 86 and [e.number for e in CATALOGUE] == list(range(1, 87))
    assert len(counted) == 85
    assert Counter(e.status for e in counted) == {"existing": 22, "wrapper": 29, "new": 34}
    kinds = Counter(e.kind for e in counted)
    assert kinds["propose"] == 26 and kinds["ask"] == 4
    assert {e.name for e in CATALOGUE if e.exposure == "aggregate"} == {
        "what_can_this_power",
        "check_readiness",
    }
    assert len({e.name for e in CATALOGUE}) == 86
    assert [(e.number, e.name, e.kind) for e in CATALOGUE[81:]] == [
        (82, "read_workbook_sheets", "read"),
        (83, "find_excel_serial_dates", "read"),
        (84, "propose_sql_step", "propose"),
        (85, "preview_working_copy", "read"),
        (86, "propose_features", "propose"),
    ]
    assert not EXECUTOR_NAMES & {e.name for e in CATALOGUE}, "an executor is never in the catalogue"


def test_only_aggregate_tools_may_be_listed_on_mcp_and_plan_o_lists_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert data_registry().mcp_surface() == ()
    monkeypatch.setattr(registry_module, "MCP_LISTED", True)
    listed = data_registry().mcp_surface()
    assert listed, "a later plan could list the aggregate verdicts"
    assert all(tool.exposure is Exposure.AGGREGATE for tool in listed)
    # The catalogue's two aggregates (#36, and M234's #69 since the wave 1 integration).
    assert {tool.name for tool in listed} == {"what_can_this_power", "check_readiness"}


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
        ({"max_output_bytes": None}, "needs an output budget"),
        ({"max_output_bytes": 0}, "output budget of 0"),
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
        save_session=lambda _session: None,
        turn=_Turn(),
        **more,
    )


@pytest.mark.parametrize("name", [t.name for t in data_registry().list(kind=DataToolKind.PROPOSE)])
def test_a_viewer_calling_a_propose_tool_is_refused_on_that_call(
    agent: AgentContext, name: str, tmp_path: Path
) -> None:
    tool = data_registry().get(name)
    caller = "person" if "person" in tool.callers else "ai"
    # What M236's PROPOSE tools also need to exist here (DEC-1806): storage, saved connections, an AI service,
    # and for `second_opinion` Plan K's `agentic.enabled` on, as its route needs (DEC-1409 (g)).
    from engine.connections.store import ConnectionStore
    from engine.settings import Settings
    from engine.storage import LocalStorage
    from tests.unit.agent.helpers import header_help_root

    ctx = _full_context(
        agent,
        VIEWER,
        caller,
        storage=LocalStorage(tmp_path),
        connections=ConnectionStore(LocalStorage(tmp_path / "c"), Settings()),
        meter=object(),
        config_root=header_help_root(),
    )
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


PLAN_G_SUGGESTIONS = {"propose_setting", "propose_step", "edit_by_hand"}
"""PROPOSE tools that run Plan G's own handler, which bounds the suggestion (`check_recipe`, the settings list)."""
NO_CLIENT_DATA = {"data_request"}
"""Session-only by the catalogue, but built from configuration alone: not a row-level result."""


def test_every_tool_gets_the_budget_of_its_exposure() -> None:
    """The design §5.2: 5,500 characters for row-level (`session-only`) tools, 16 KB for aggregates."""
    for tool in data_registry().list():
        if tool.name in PLAN_G_SUGGESTIONS:
            assert tool.budget is None, tool.name
        elif tool.name in NO_CLIENT_DATA:
            assert tool.budget == AGGREGATE_BUDGET, tool.name
        elif tool.exposure is Exposure.SESSION_ONLY:
            assert tool.budget == ROW_LEVEL_BUDGET == 5_500, tool.name
        else:
            assert tool.budget == AGGREGATE_BUDGET == 16_384, tool.name
    assert {t.name for t in data_registry().list() if t.budget is None} == PLAN_G_SUGGESTIONS


def test_a_row_level_result_is_measured_in_characters_of_json() -> None:
    """5,000 two-byte characters fit a row-level tool's 5,500 (10,000 bytes); 5,600 characters do not."""
    for text, fits in (("é" * 5_000, True), ("x" * 5_600, False)):
        registry = DataToolRegistry()
        registry.register(_tool(run=lambda _g, _c, text=text: LookOutput({"rows": [text]})))
        if fits:
            assert registry.call("impossible_values", {}, DataToolContext()).output == {"rows": [text]}
            continue
        with pytest.raises(DataToolError) as refused:
            registry.call("impossible_values", {}, DataToolContext())
        assert refused.value.code == "TOOL_OUTPUT_TOO_LARGE" and "5,500" in refused.value.message


def _many_files(agent: AgentContext, count: int) -> tuple[LoadedFile, ...]:
    loaded = _loaded(agent)
    return tuple(
        LoadedFile(upload_id=f"u-{i:02d}", file_name=f"f{i}.csv", frame=loaded.frame, profile=loaded.profile)
        for i in range(count)
    )


def test_diagnose_files_lists_what_fits_and_says_how_many_there_were(agent: AgentContext) -> None:
    ctx = DataToolContext(agent=agent, files=_many_files(agent, 20))
    result = data_registry().call("diagnose_files", {}, ctx)
    assert result.output["files_total"] == 20
    shown = result.output["files"]
    assert 0 < len(shown) < 20, "twenty diagnoses do not fit 5,500 characters"
    assert [f["upload_id"] for f in shown] == [f"u-{i:02d}" for i in range(len(shown))], "cut from the end"
    assert len(json.dumps(result.output, separators=(",", ":"), ensure_ascii=False)) <= ROW_LEVEL_BUDGET


def test_diagnose_files_keeps_the_doctor_routes_caps(
    agent: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes import doctor as doctor_route
    from engine.doctor import diagnose as diagnose_module
    from engine.doctor.contracts import DiagnosisRequest

    assert tools_module.DIAGNOSIS_ROWS == doctor_route.DIAGNOSIS_ROWS
    limit = DiagnosisRequest.model_fields["upload_ids"].metadata
    assert any(getattr(m, "max_length", None) == tools_module.DIAGNOSIS_FILES == 20 for m in limit)
    # More than 20 files: refused, whether named or not.
    ctx = DataToolContext(agent=agent, files=_many_files(agent, 21))
    with pytest.raises(DataToolError) as refused:
        data_registry().call("diagnose_files", {}, ctx)
    assert refused.value.code == "TOOL_INPUT_INVALID"
    with pytest.raises(DataToolError) as named:
        data_registry().call("diagnose_files", {"upload_ids": [f"u-{i:02d}" for i in range(21)]}, ctx)
    assert named.value.code == "TOOL_INPUT_INVALID"
    # An even share of the rows each, never fewer than ADVISE_ROWS.
    read: list[int] = []
    real = diagnose_module.diagnose_file

    def counted(loaded: LoadedFile, *args: Any) -> Any:
        read.append(len(loaded.frame))
        return real(loaded, *args)

    monkeypatch.setattr(diagnose_module, "diagnose_file", counted)
    monkeypatch.setattr(tools_module, "DIAGNOSIS_ROWS", 1_000)
    monkeypatch.setattr(tools_module, "ADVISE_ROWS", 300)
    data_registry().call("diagnose_files", {}, DataToolContext(agent=agent, files=_many_files(agent, 2)))
    assert read == [500, 500] and len(agent.frame) == 1_200
    read.clear()
    data_registry().call("diagnose_files", {}, DataToolContext(agent=agent, files=_many_files(agent, 5)))
    assert read == [300] * 5


def test_data_request_takes_configured_use_cases_only(agent: AgentContext) -> None:
    ctx = DataToolContext(agent=agent, principal=VIEWER, caller="person")
    for ids in (["no-such-thing"], ["../../etc/passwd"], ["Targeted-Advertisement"], ["a"] * 5):
        with pytest.raises(DataToolError) as refused:
            data_registry().call("data_request", {"use_case_ids": ids}, ctx)
        assert refused.value.code == "TOOL_INPUT_INVALID", ids
        assert "/" not in refused.value.message.replace("../../etc/passwd", ""), "no server path is shown"
        assert "configs" not in refused.value.message
    made = data_registry().call("data_request", {"use_case_ids": ["targeted-advertisement"]}, ctx)
    assert made.output


def test_data_request_attaches_forwardable_messages_by_selector(agent: AgentContext) -> None:
    ctx = DataToolContext(agent=agent, principal=VIEWER, caller="person")
    made = data_registry().call("data_request", {"messages": ["IDS_ROUNDED", "CHURN_LIST_COMPLETE"]}, ctx)
    sent = {m["selector"]: m for m in made.output["messages"]}
    assert list(sent) == ["IDS_ROUNDED", "CHURN_LIST_COMPLETE"]
    assert sent["IDS_ROUNDED"]["id"] == "D1"
    assert (
        "[from date]" in sent["CHURN_LIST_COMPLETE"]["message"]
    ), "a blank the Doctor cannot know is left to fill in"
    assert not data_registry().call("data_request", {}, ctx).output["messages"]
    for bad in (["NO_SUCH_MESSAGE"], ["lower"], ["A_" * 30], ["PDF", "D_TWO", "D_THREE"]):
        with pytest.raises(DataToolError) as refused:
            data_registry().call("data_request", {"messages": bad}, ctx)
        assert refused.value.code == "TOOL_INPUT_INVALID", bad


def test_the_longest_data_requests_fit_its_budget() -> None:
    """The four use cases whose requests are longest, asked together with the longest forwardable messages the
    tool attaches, fit `data_request`'s 16 KB."""
    from engine.config import list_use_case_ids
    from engine.pilot.data_request import build_data_request, load_request_messages

    def size(ids: tuple[str, ...]) -> int:
        return len(json.dumps(build_data_request(ids).model_dump(mode="json")).encode())

    longest = sorted(list_use_case_ids(), key=lambda u: size((u,)), reverse=True)
    widest = tuple(longest[: tools_module.DATA_REQUEST_MAX_USE_CASES])
    messages = load_request_messages()
    wordiest = sorted(messages, key=lambda key: len(messages[key].message), reverse=True)
    attached = wordiest[: tools_module.DATA_REQUEST_MAX_MESSAGES]
    ctx = DataToolContext(principal=VIEWER, caller="person")
    args = {"use_case_ids": list(widest), "messages": attached}
    assert data_registry().call("data_request", args, ctx).output


def test_an_edit_made_through_the_registry_is_saved(agent: AgentContext) -> None:
    saved: list[Any] = []
    ctx = dataclasses.replace(_full_context(agent, ANALYST), save_session=saved.append)
    column = next(
        str(c) for c in agent.frame.columns if agent.frame[c].dtype == object and c != "customer_id"
    )
    result = data_registry().call("edit_by_hand", {"kind": "drop_column", "column": column}, ctx)
    assert len(saved) == 1
    stored = [p.proposal_id for p in saved[0].proposals]
    assert result.output["proposal"]["proposal_id"] in stored
    assert ctx.session is not None and result.output["proposal"]["proposal_id"] not in [
        p.proposal_id for p in ctx.session.proposals
    ], "the edit is in the saved session, not the one the call was given"
    # Without somewhere to save it, the tool is not there: an edit is never made and dropped.
    unsaved = dataclasses.replace(ctx, save_session=None)
    assert data_registry().find("edit_by_hand", unsaved) is None


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


def test_a_table_that_powers_many_goals_still_gets_its_verdicts_with_the_best_first_looks() -> None:
    """Every ready/check verdict carries a first look; five goals together went over the 16 KB budget and stopped
    the whole visit ("Something stopped the check") for an ordinary customer table."""
    from engine.config import load_use_case
    from tests.unit.doctor import chat_support, visit_support

    config = load_use_case("targeted-advertisement")
    loaded = visit_support.loaded("u_many", "orders.csv", chat_support.orders_frame(), config)
    ctx = DataToolContext(
        files=(loaded,), offers=tuple(Offer(config=config, stage="") for _ in range(5)), principal=VIEWER
    )
    verdicts = data_registry().call("what_can_this_power", {}, ctx).output["use_cases"]
    assert len(verdicts) == 5, "every goal still has its verdict"
    assert verdicts[0]["first_look"] is not None, "the best goal keeps its first look"
    assert verdicts[-1]["first_look"] is None, "the last ones give theirs up to fit"


def test_unified_on_reads_the_switch_and_is_off_without_it(agent: AgentContext) -> None:
    class Section:
        unified = True

    class Config:
        data_doctor = Section()

    assert unified_on(agent.config) is False, "the shipped configuration has the switch off"
    assert unified_on(None) is False
    assert unified_on(Config()) is True  # type: ignore[arg-type]
