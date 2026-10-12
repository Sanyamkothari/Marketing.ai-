"""The data policy on what the wave-1 tools return (Plan O M233, DEC-1803; review fix).

What it proves: every wave-1 tool's real output on its own test file is allowed by its rule set (so the policy
breaks none of them); the rules the plan gave those tools are applied to what they return - M241's `na` and
lexicon spellings and `indian_scale_words`, M238's question and APPROVE-class sentence, decision O10 on an AI's
merge, the ID and the outcome, M240's `excel_serial`, M237's 2% sentence; and the registry asks after every call
of a tool that proposes, including the READ tools that return pending steps (default deny for them too).
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pandas as pd
import pytest
from pydantic import BaseModel, ConfigDict

from api.access_policy import RoutePolicy
from engine.access.roles import Principal, Role
from engine.agent.config import AgentLevel
from engine.doctor import policy as pol
from engine.doctor.policy import DataPolicyContext, check_tool_output, output_proposals, policy_kind_for
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)
from engine.doctor.tools_files import TOOLS as FILE_TOOLS
from engine.doctor.tools_formats import FindExcelSerialDatesOutput, SerialDateColumn
from engine.doctor.tools_locale import LOCALE_TOOLS
from engine.doctor.tools_rows import ROW_TOOLS, DetectUnitsInput
from tests.unit.doctor.policy_cases import shipped
from tests.unit.doctor.test_tools_files import INPUTS as FILE_INPUTS
from tests.unit.doctor.test_tools_files import _context as files_context
from tests.unit.doctor.test_tools_locale import Ctx as LocaleCtx
from tests.unit.doctor.test_tools_locale import canary_frame
from tests.unit.doctor.test_tools_rows import CALLS as ROW_CALLS
from tests.unit.doctor.test_tools_rows import ctx as rows_context

ANALYST = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")
ALL_LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)


def one_file() -> DataPolicyContext:
    return DataPolicyContext(policy=shipped(), primary_key="customer_id", target="churned", levels=ALL_LEVELS)


def many_files() -> DataPolicyContext:
    return DataPolicyContext(policy=shipped(), levels=ALL_LEVELS)


def code_of(kind: str, output: dict[str, Any], ctx: DataPolicyContext | None = None) -> str | None:
    found = check_tool_output(kind, output, ctx or (many_files() if kind in pol.FILES_KINDS else one_file()))
    return None if found is None else found.code


def row_output(
    name: str, frame: pd.DataFrame | None = None, given: BaseModel | None = None
) -> dict[str, Any]:
    spec = next(spec for spec in ROW_TOOLS if spec.name == name)
    out: dict[str, Any] = spec.run(given or ROW_CALLS[name], rows_context(frame)).model_dump(mode="json")
    return out


def locale_output(name: str) -> dict[str, Any]:
    spec = next(spec for spec in LOCALE_TOOLS if spec.name == name)
    out: dict[str, Any] = spec.run(spec.input_model(), LocaleCtx(canary_frame())).model_dump(mode="json")
    return out


def file_output(name: str) -> dict[str, Any]:
    spec = next(spec for spec in FILE_TOOLS if spec.name == name)
    given = spec.input_model.model_validate(FILE_INPUTS[name])
    out: dict[str, Any] = spec.run(given, files_context()).model_dump(mode="json")
    return out


def serial_output(**column: Any) -> dict[str, Any]:
    found = SerialDateColumn(column="signup", non_empty=10, convertible=10, failed=0, notes=(), **column)
    return FindExcelSerialDatesOutput(columns=(found,)).model_dump(mode="json")


# ---------------------------------------------------------------------------
# What the tools return today is allowed
# ---------------------------------------------------------------------------
def test_every_wave_one_kind_has_a_reader_and_a_rule_set() -> None:
    kinds = set(pol.OUTPUT_READERS) | set(pol.HANDLER_CHECKED)
    assert kinds == set(shipped().rule_sets), "a rule set no output is read by, or a reader with no rule set"


@pytest.mark.parametrize("name", [spec.name for spec in ROW_TOOLS])
def test_each_m238_tool_output_is_allowed(name: str) -> None:
    output = row_output(name)
    assert code_of(policy_kind_for(name), output) is None


def test_the_m238_outputs_hold_proposals_the_rules_read() -> None:
    assert output_proposals("data.synonym_clusters", row_output("synonym_clusters"))
    (latest,) = output_proposals("data.keep_latest_per_id", row_output("keep_latest_per_id"))
    assert latest.asked and latest.sentence and (latest.rows_in, latest.rows_out) == (6, 4)
    (split,) = output_proposals("data.split_file", row_output("split_file"))
    assert split.sentence


@pytest.mark.parametrize("name", ["parse_indian_numbers", "indian_booleans"])
def test_each_m241_tool_output_is_allowed(name: str) -> None:
    output = locale_output(name)
    assert output_proposals(policy_kind_for(name), output), "the canary file has a proposal"
    assert code_of(policy_kind_for(name), output) is None


@pytest.mark.parametrize("name", [spec.name for spec in FILE_TOOLS])
def test_each_m237_tool_output_is_allowed(name: str) -> None:
    assert code_of(policy_kind_for(name), file_output(name)) is None


def test_m240_excel_day_numbers_are_allowed_as_the_rules_fix() -> None:
    assert policy_kind_for("find_excel_serial_dates") == "data.excel_serial_dates"
    assert code_of("data.excel_serial_dates", serial_output()) is None
    assert code_of("data.excel_serial_dates", serial_output(date_1904=True)) is None


# ---------------------------------------------------------------------------
# The rules the plan gave those tools
# ---------------------------------------------------------------------------
def _booleans_with(true: list[str], false: list[str], confidence: str = "check") -> dict[str, Any]:
    output = copy.deepcopy(locale_output("indian_booleans"))
    proposal = next(p for p in output["proposals"] if p["step"] is not None)
    proposal["step"]["params"] = {"true_values": true, "false_values": false}
    proposal["confidence"] = confidence
    return output


def test_m241_na_is_never_mapped_and_the_rules_map_only_lexicon_words() -> None:
    assert code_of("data.indian_booleans", _booleans_with(["haan"], ["nahi"])) is None
    assert (
        code_of("data.indian_booleans", _booleans_with(["haan"], ["nahi", "NA"])) == "POLICY_BOOLEAN_SPELLING"
    )
    assert (
        code_of("data.indian_booleans", _booleans_with(["haan", "ho"], ["nahi"])) == "POLICY_BOOLEAN_SPELLING"
    )
    assert code_of("data.indian_booleans", _booleans_with(["haan"], ["haan"])) == "RECIPE_STEP_INVALID"


def test_m241_numbers_carry_indian_scale_words() -> None:
    output = copy.deepcopy(locale_output("parse_indian_numbers"))
    output["proposals"][0]["step"]["params"].pop("indian_scale_words")
    assert code_of("data.parse_indian_numbers", output) == "RECIPE_STEP_INVALID"


def test_m241_a_sure_proposal_is_offered_ticked_and_allowed() -> None:
    (sure,) = output_proposals("data.indian_booleans", _booleans_with(["haan"], ["nahi"], confidence="sure"))
    assert sure.ticked and sure.confidence == "sure"
    assert code_of("data.indian_booleans", _booleans_with(["haan"], ["nahi"], confidence="sure")) is None


def test_m238_keep_latest_only_through_its_question_and_with_its_sentence() -> None:
    output = row_output("keep_latest_per_id")
    assert code_of("data.keep_latest_per_id", {**output, "question": None}) == "RESHAPE_GUARD_MISSING"
    assert code_of("data.keep_latest_per_id", {**output, "sentence": ""}) == "POLICY_PAYLOAD_INVALID"
    ticked = {**output, "step": {**output["step"], "ticked": True}}
    assert code_of("data.keep_latest_per_id", ticked) == "POLICY_NEVER_TICKED"
    by_outcome = {
        **output,
        "step": {**output["step"], "params": {**output["step"]["params"], "time_column": "churned"}},
    }
    assert code_of("data.keep_latest_per_id", by_outcome) == "RECIPE_STEP_INVALID"


def test_m238_an_ai_merge_is_held_to_decision_o10_and_never_ticked() -> None:
    output = row_output("synonym_clusters")
    step = output["report"]["step"]
    assert step is not None
    ai = {**step, "source": "ai", "changed": 2, "out_of": 6}
    assert code_of("data.synonym_clusters", {**output, "ai_step": ai}) is None
    heavy = {**ai, "changed": 5}
    assert code_of("data.synonym_clusters", {**output, "ai_step": heavy}) == "AI_STEP_CHANGES_TOO_MANY"
    ticked = {**ai, "ticked": True}
    assert code_of("data.synonym_clusters", {**output, "ai_step": ticked}) == "POLICY_NEVER_TICKED"


def test_m238_a_step_on_the_id_or_a_personal_column_is_refused() -> None:
    output = row_output("detect_units")
    assert output["step"] is not None
    on_id = {**output, "step": {**output["step"], "column": "customer_id"}}
    assert code_of("data.detect_units", on_id) == "RECIPE_STEP_INVALID"
    personal = replace(one_file(), personal_columns=frozenset({output["step"]["column"]}))
    assert code_of("data.detect_units", output, personal) == "AGENT_STEP_NOT_ALLOWED"


def test_m240_excel_day_numbers_must_say_so() -> None:
    assert code_of("data.excel_serial_dates", serial_output(excel_serial=False)) == "RECIPE_STEP_INVALID"


def test_m237_a_join_dropping_rows_needs_its_sentence() -> None:
    output = file_output("join_files")
    parts = [{"rows": 100, "rows_dropped": 1}, {"rows": 100, "rows_dropped": 10}]  # the worst file decides
    dropping = {**output, "receipt": {"parts": parts}, "summary": ""}
    (join,) = output_proposals("data.join_files", dropping)
    assert (join.rows_in, join.rows_out) == (100, 90)
    assert code_of("data.join_files", dropping) == "POLICY_PAYLOAD_INVALID"
    assert code_of("data.join_files", {**dropping, "summary": "Join the bills."}) is None


def test_default_deny_on_tool_outputs() -> None:
    assert code_of("data.no_such_tool", {}) == "POLICY_NO_RULES"
    assert code_of("data.keep_latest_per_id", {"step": "drop everything"}) == "POLICY_PAYLOAD_INVALID"
    assert code_of("data.impossible_values", {"findings": [{"step": {"kind": "rm -rf"}}]}) == (
        "POLICY_PAYLOAD_INVALID"
    )
    assert check_tool_output("data.recipe_step", {}, one_file()) is None, "decided in the chat's handler"
    nothing = row_output("detect_units", given=DetectUnitsInput(column="plan"))
    assert nothing["step"] is None
    assert code_of("data.detect_units", nothing) is None, "no step proposed: nothing to decide"


# ---------------------------------------------------------------------------
# The registry asks after every call of a tool that proposes
# ---------------------------------------------------------------------------
class _Given(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Units(BaseModel):
    step: dict[str, Any] | None = None


def _units_tool(step: dict[str, Any] | None, *, step_kinds: tuple[Any, ...]) -> DataTool[Any, Any]:
    return DataTool(
        number=53,
        name="detect_units",
        kind=DataToolKind.READ,
        description="A look that returns a pending step.",
        input_model=_Given,
        output_model=_Units,
        route_policy=RoutePolicy(role=Role.ANALYST, action="doctor.detect_units", purpose="look"),
        exposure=Exposure.SESSION_ONLY,
        run=lambda _given, _ctx: _Units(step=step),
        step_kinds=step_kinds,
    )


def _registry(tool: DataTool[Any, Any]) -> DataToolRegistry:
    registry = DataToolRegistry()
    registry.register(tool)
    return registry


def test_a_read_tool_that_returns_steps_is_decided_by_its_rule_set() -> None:
    from engine.agent.contracts import RecipeStepKind

    step = row_output("detect_units")["step"]
    ctx = DataToolContext(principal=ANALYST, caller="person")
    tool = _units_tool(step, step_kinds=(RecipeStepKind.CONVERT_UNITS,))
    assert tool.proposal_kind == "data.detect_units"
    assert _registry(tool).call("detect_units", {}, ctx).output == {"step": step}
    ticked = _units_tool({**step, "ticked": True}, step_kinds=(RecipeStepKind.CONVERT_UNITS,))
    with pytest.raises(DataToolError) as refused:
        _registry(ticked).call("detect_units", {}, ctx)
    assert refused.value.code == "POLICY_NEVER_TICKED" and refused.value.status == 403
    # A READ tool declaring no step kinds proposes nothing: the policy does not read it.
    plain = _units_tool({**step, "ticked": True}, step_kinds=())
    assert plain.proposal_kind is None
    assert _registry(plain).call("detect_units", {}, ctx).output["step"]["ticked"] is True


def test_a_read_tool_that_returns_steps_with_no_rule_set_is_refused(tmp_path: Any) -> None:
    from engine.agent.contracts import RecipeStepKind

    tool = _units_tool(None, step_kinds=(RecipeStepKind.CONVERT_UNITS,))
    no_policy = DataToolContext(principal=ANALYST, caller="person", config_root=tmp_path)
    with pytest.raises(DataToolError) as refused:
        _registry(tool).call("detect_units", {}, no_policy)
    assert refused.value.code == "POLICY_NO_RULES"
