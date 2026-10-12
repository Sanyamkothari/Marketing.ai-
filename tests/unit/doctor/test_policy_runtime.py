"""The data policy where Plan G's handlers ask it (Plan O M233, DEC-1803), behind `data_doctor.unified`.

What it proves: a PROPOSE tool with no rule set is refused by the registry (`POLICY_NO_RULES`); on a fixture set of
everything the chat, the settings and the person's edits refuse today, every refusal is still a refusal with the
same code when the switch is on; the rules the policy adds (yes/no spellings, decision O10) refuse only with the
switch on; with the switch off the policy file is never read; the never-ticked kinds are read from the policy too.

The switch is M230's `data_doctor.unified`; these tests switch it on where it is read
(`engine.doctor.registry.unified_on`), as M232's runtime tests do.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import ProposalState, RecipeStepKind
from engine.agent.edits import add_edit
from engine.agent.loop import chat_turn
from engine.agent.session import SessionError, accept_recommended, start_session
from engine.doctor import policy as policy_module
from engine.doctor import registry as registry_module
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.unit.agent.helpers import context_for
from tests.unit.agent.test_session_and_loop import ScriptedMeter, _guardrails

ANALYST = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def set_to(on: bool) -> None:
        monkeypatch.setattr(registry_module, "unified_on", lambda _config: on)

    return set_to


def _frame() -> pd.DataFrame:
    frame = messy_frame()
    frame["city"] = [
        "Bombay" if index % 3 == 0 else "Mumbai" if index % 3 == 1 else "Pune" for index in range(len(frame))
    ]
    return frame


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return _frame()


@pytest.fixture(scope="module")
def wide_frame() -> pd.DataFrame:
    """The frame with a column of sizes in two units and one of codes to split (decision O10's other steps)."""
    wide = _frame()
    wide["data_usage"] = ["2 GB" if index % 2 else "500 MB" for index in range(len(wide))]
    wide["region_code"] = [f"{region}-{index % 5}" for index, region in enumerate(wide["region"])]
    return wide


def _look(column: str) -> str:
    return json.dumps({"action": "value_counts", "args": {"column": column}})


def _step(
    kind: str,
    column: str,
    evidence: tuple[str, ...] = ("c1-e1",),
    new_column: str | None = None,
    **params: Any,
) -> str:
    args: dict[str, Any] = {
        "kind": kind,
        "column": column,
        "params": params,
        "reason": "Fix it.",
        "evidence_ids": list(evidence),
    }
    if new_column is not None:
        args["new_column"] = new_column
    return json.dumps({"action": "propose_step", "args": args})


def _setting(path: str, value: Any) -> str:
    return json.dumps(
        {"action": "propose_setting", "args": {"path": path, "value": value, "reason": "Try it."}}
    )


def _reply() -> str:
    return json.dumps({"action": "reply", "text": "Done.", "evidence_ids": []})


def _chat(frame: pd.DataFrame, *script: str, config_root: Path | None = None) -> Any:
    ctx = context_for(frame, config_root=config_root)
    session = start_session(ctx, session_id="s1")
    meter = ScriptedMeter([*script, _reply()])
    return chat_turn(ctx, session, "Fix it", meter=meter, guardrails=_guardrails(), principal=ANALYST)


# ---------------------------------------------------------------------------
# Default deny in the registry
# ---------------------------------------------------------------------------
def _propose_tool(policy_kind: str) -> DataTool[Any, Any]:
    from pydantic import BaseModel, ConfigDict

    from api.access_policy import RoutePolicy

    class Given(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class Made(BaseModel):
        made: bool = True

    ran: list[str] = []

    def run(_given: Given, _ctx: DataToolContext) -> Made:
        ran.append("ran")
        return Made()

    tool: DataTool[Any, Any] = DataTool(
        number=41,
        name="propose_role",
        kind=DataToolKind.PROPOSE,
        description="A PROPOSE tool for the test.",
        input_model=Given,
        output_model=Made,
        route_policy=RoutePolicy(role=Role.ANALYST, action="doctor.propose_role", purpose="suggest a role"),
        exposure=Exposure.SESSION_ONLY,
        run=run,
        policy_kind=policy_kind,
    )
    return tool


def test_a_propose_tool_with_no_rule_set_is_refused_with_policy_no_rules() -> None:
    registry = DataToolRegistry()
    # A kind with no rule set yet (M236 gave `data.propose_role` one, DEC-1806; M239 gave `data.define_outcome` one,
    # DEC-1809; M242 gave `data.convert_currency` one, DEC-1812; M244 gave `data.propose_time_settings` one,
    # DEC-1814).
    registry.register(_propose_tool("data.map_consent"))
    ctx = DataToolContext(principal=ANALYST, caller="person")
    with pytest.raises(DataToolError) as refused:
        registry.call("propose_role", {}, ctx)
    assert refused.value.code == "POLICY_NO_RULES" and refused.value.status == 403


def test_a_propose_tool_whose_kind_has_rules_runs(tmp_path: Path, config_root: Path) -> None:
    registry = DataToolRegistry()
    registry.register(_propose_tool("data.setting"))  # decided in its handler: the registry runs it
    assert registry.call("propose_role", {}, DataToolContext(principal=ANALYST, caller="person")).output == {
        "made": True
    }
    # A wave-1 kind's output is read as proposals after the call: one the policy cannot read is refused.
    other = DataToolRegistry()
    other.register(_propose_tool("data.split_file"))
    with pytest.raises(DataToolError) as unread:
        other.call("propose_role", {}, DataToolContext(principal=ANALYST, caller="person"))
    assert unread.value.code == "POLICY_PAYLOAD_INVALID" and unread.value.status == 403
    # Without a readable policy every PROPOSE tool is refused (default deny), and a READ tool still runs.
    with pytest.raises(DataToolError) as refused:
        registry.call(
            "propose_role", {}, DataToolContext(principal=ANALYST, caller="person", config_root=tmp_path)
        )
    assert refused.value.code == "POLICY_NO_RULES"


def test_the_role_is_checked_before_the_policy() -> None:
    registry = DataToolRegistry()
    registry.register(_propose_tool("data.propose_role"))
    viewer = Principal(user_id="u-v", username="v", roles=frozenset({Role.VIEWER}), kind="user")
    with pytest.raises(DataToolError) as refused:
        registry.call("propose_role", {}, DataToolContext(principal=viewer, caller="person"))
    assert refused.value.code == "TOOL_ROLE_REQUIRED"


# ---------------------------------------------------------------------------
# Every refusal today is still a refusal, with the same code
# ---------------------------------------------------------------------------
CHAT_REFUSALS: dict[str, tuple[str, ...]] = {
    "not the chat's kind": (_look("city"), _step("correct_cells", "city"), _step("correct_cells", "city")),
    "combining rows": (_look("city"), _step("combine_rows", "city"), _step("combine_rows", "city")),
    "made-up kind": (_look("city"), _step("rm -rf", "city"), _step("rm -rf", "city")),
    "no look": (_step("map_values", "city", (), mapping={"Bombay": "Mumbai"}),) * 2,
    "a look at another column": (
        _look("region"),
        *(_step("map_values", "city", mapping={"Bombay": "Mumbai"}),) * 2,
    ),
    "the outcome": (_look("converted_30d"), *(_step("fill_missing", "converted_30d", value=0),) * 2),
    "the ID": (_look("customer_id"), *(_step("normalise_text", "customer_id", strip=True),) * 2),
    "personal data": (_look("contact_email"), *(_step("normalise_text", "contact_email", strip=True),) * 2),
    "Excel day numbers": (
        _look("signup_date"),
        *(_step("parse_date", "signup_date", excel_serial=True),) * 2,
    ),
    "Excel 1904": (
        _look("signup_date"),
        *(_step("parse_date", "signup_date", excel_serial=True, date_1904=True),) * 2,
    ),
    "a bad parameter": (_look("city"), *(_step("fill_missing", "city", value="median"),) * 2),
    "an unknown parameter": (
        _look("city"),
        *(_step("map_values", "city", mapping={"Bombay": "Mumbai"}, x=1),) * 2,
    ),
    "no effect": (_look("city"), *(_step("map_values", "city", mapping={"Delhi": "New Delhi"}),) * 2),
    "hiding a column as a setting": (_setting("prepare.exclude_columns", ["city"]),) * 2,
    "a safeguard": (_setting("validation.min_rows", 10),) * 2,
    "a setting no stage reads": (_setting("features.selection", "auto"),) * 2,
    "an unknown setting": (_setting("no.such.setting", 1),) * 2,
    # A malformed yes/no step is `check_recipe`'s RECIPE_STEP_INVALID today; the policy keeps that code.
    "yes/no: one spelling on both sides": (
        _look("is_premium"),
        *(_step("map_boolean", "is_premium", true_values=["yes", "Y"], false_values=["yes", "N"]),) * 2,
    ),
    "yes/no: no spelling of no": (
        _look("is_premium"),
        *(_step("map_boolean", "is_premium", true_values=["yes", "Y"], false_values=[]),) * 2,
    ),
    "yes/no: no list of no": (
        _look("is_premium"),
        *(_step("map_boolean", "is_premium", true_values=["yes", "Y"]),) * 2,
    ),
}


@pytest.mark.parametrize("name", sorted(CHAT_REFUSALS))
def test_every_chat_refusal_today_is_still_a_refusal_with_the_same_code(
    name: str, frame: pd.DataFrame, switch: Callable[[bool], None]
) -> None:
    switch(False)
    off = _chat(frame, *CHAT_REFUSALS[name])
    switch(True)
    on = _chat(frame, *CHAT_REFUSALS[name])
    assert off.proposals == () and off.blocked_by is not None, "a refusal today"
    assert on.proposals == () and on.blocked_by == off.blocked_by, (off.blocked_by, on.blocked_by)


EDIT_REFUSALS: dict[str, dict[str, Any]] = {
    "parsing is the rules'": {"kind": RecipeStepKind.PARSE_NUMBER, "column": "monthly_spend", "params": {}},
    "combining is the rules'": {"kind": RecipeStepKind.COMBINE_ROWS, "column": "city", "params": {}},
    "personal data": {
        "kind": RecipeStepKind.MAP_VALUES,
        "column": "contact_email",
        "params": {"mapping": {"a": "b"}},
    },
    "the outcome": {"kind": RecipeStepKind.FILL_MISSING, "column": "converted_30d", "params": {"value": 0}},
    "a bad value": {"kind": RecipeStepKind.FILL_MISSING, "column": "city", "params": {"value": "median"}},
}


@pytest.mark.parametrize("name", sorted(EDIT_REFUSALS))
def test_every_edit_refusal_today_is_still_a_refusal_with_the_same_code(
    name: str, frame: pd.DataFrame, switch: Callable[[bool], None]
) -> None:
    ctx = context_for(frame)
    session = start_session(ctx, session_id="s1")
    found: list[str] = []
    for on in (False, True):
        switch(on)
        with pytest.raises(SessionError) as refused:
            add_edit(session, ctx, **EDIT_REFUSALS[name])
        found.append(refused.value.code)
    assert found[0] == found[1], found


def test_what_is_allowed_today_is_still_allowed(frame: pd.DataFrame, switch: Callable[[bool], None]) -> None:
    script = (
        _look("city"),
        _step("map_values", "city", mapping={"Bombay": "Mumbai"}),
        _setting("split.test_fraction", 0.2),
    )
    switch(False)
    off = _chat(frame, *script)
    switch(True)
    on = _chat(frame, *script)
    assert len(off.proposals) == 2 and [p.model_dump() for p in on.proposals] == [
        p.model_dump() for p in off.proposals
    ]
    ctx = context_for(frame)
    session = start_session(ctx, session_id="s1")
    edit: dict[str, Any] = {
        "kind": RecipeStepKind.MAP_VALUES,
        "column": "city",
        "params": {"mapping": {"Bombay": "Mumbai"}},
    }
    switch(False)
    made_off = add_edit(session, ctx, **edit)
    switch(True)
    made_on = add_edit(session, ctx, **edit)
    assert [p.model_dump() for p in made_off.proposals] == [p.model_dump() for p in made_on.proposals]
    assert [r.result for r in made_off.tool_results] == [r.result for r in made_on.tool_results]


ALLOWED_TODAY: dict[str, tuple[str, str]] = {
    # Decision O10 counts only what a step rewrites in its own column: a rename and a split into a new column
    # rewrite none, hiding a column rewrites none, and a unit conversion re-types every value it can read.
    "rename a column": ("city", _step("rename_column", "city", new_column="town")),
    "split into a new column": (
        "region_code",
        _step("split_column", "region_code", new_column="region_part", separator="-", part=0),
    ),
    "convert units": ("data_usage", _step("convert_units", "data_usage", units={"GB": 1024, "MB": 1})),
    "hide a column": ("city", _step("drop_column", "city")),
    "read numbers": ("monthly_spend", _step("parse_number", "monthly_spend")),
}


@pytest.mark.parametrize("name", sorted(ALLOWED_TODAY))
def test_every_chat_step_kind_allowed_today_is_still_allowed(
    name: str, wide_frame: pd.DataFrame, switch: Callable[[bool], None]
) -> None:
    column, step = ALLOWED_TODAY[name]
    script = (_look(column), step)
    switch(False)
    off = _chat(wide_frame, *script)
    switch(True)
    on = _chat(wide_frame, *script)
    assert len(off.proposals) == 1, (off.blocked_by, "allowed today")
    assert [p.model_dump() for p in on.proposals] == [p.model_dump() for p in off.proposals], on.blocked_by


# ---------------------------------------------------------------------------
# What the policy adds, only with the switch on
# ---------------------------------------------------------------------------
def test_na_is_never_mapped_to_yes_or_no_with_the_switch_on(
    frame: pd.DataFrame, switch: Callable[[bool], None]
) -> None:
    attempt = _step("map_boolean", "is_premium", true_values=["yes", "Y"], false_values=["No", "N", "na"])
    script = (_look("is_premium"), attempt, attempt)
    switch(False)
    assert _chat(frame, *script).proposals, "today the chat may map 'na'"
    switch(True)
    on = _chat(frame, *script)
    assert on.proposals == () and on.blocked_by == "POLICY_BOOLEAN_SPELLING"
    fine = _step("map_boolean", "is_premium", true_values=["yes", "Y"], false_values=["No", "N"])
    (made,) = _chat(frame, _look("is_premium"), fine).proposals
    assert made.state is ProposalState.PENDING, "an AI's yes/no is pending, never ticked"


def test_an_ai_step_changing_most_of_a_column_is_refused_with_the_switch_on(
    frame: pd.DataFrame, switch: Callable[[bool], None]
) -> None:
    """Decision O10: 667 of the first 1,000 values (67%) is refused; 334 (33%) is allowed (with a warning)."""
    heavy = _step("map_values", "city", mapping={"Bombay": "Mumbai", "Pune": "Mumbai"})
    switch(False)
    assert len(_chat(frame, _look("city"), heavy).proposals) == 1
    switch(True)
    refused = _chat(frame, _look("city"), heavy, heavy)
    assert refused.proposals == () and refused.blocked_by == "AI_STEP_CHANGES_TOO_MANY"
    light = _step("map_values", "city", mapping={"Bombay": "Mumbai"})
    assert len(_chat(frame, _look("city"), light).proposals) == 1


def test_a_chat_with_no_rule_set_for_its_fixes_suggests_none(
    frame: pd.DataFrame, switch: Callable[[bool], None], tmp_path: Path, config_root: Path
) -> None:
    copy = tmp_path / "configs"
    shutil.copytree(config_root, copy)
    path = copy / "agentic" / "policy.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("    data.recipe_step:\n", "    data.recipe_step_retired:\n"), encoding="utf-8"
    )
    script = (_look("city"), *(_step("map_values", "city", mapping={"Bombay": "Mumbai"}),) * 2)
    switch(False)
    assert _chat(frame, *script, config_root=copy).proposals
    switch(True)
    on = _chat(frame, *script, config_root=copy)
    assert on.proposals == () and on.blocked_by == "POLICY_NO_RULES"


def test_with_the_switch_off_the_policy_is_never_read(
    frame: pd.DataFrame, switch: Callable[[bool], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the data policy was read with the switch off")

    monkeypatch.setattr(policy_module, "load_data_policy", unreadable)
    switch(False)
    ctx = context_for(frame)
    session = accept_recommended(start_session(ctx, session_id="s1"), ctx)
    assert session.proposals
    made = _chat(frame, _look("city"), _step("map_values", "city", mapping={"Bombay": "Mumbai"}))
    assert len(made.proposals) == 1
    add_edit(
        session, ctx, kind=RecipeStepKind.MAP_VALUES, column="city", params={"mapping": {"Bombay": "Mumbai"}}
    )


def test_the_never_ticked_kinds_are_read_from_the_policy_too(
    frame: pd.DataFrame, switch: Callable[[bool], None], tmp_path: Path, config_root: Path
) -> None:
    ctx = context_for(frame)
    sure = [
        p.step.kind
        for p in accept_recommended(start_session(ctx, session_id="s1"), ctx).proposals
        if p.state is ProposalState.ACCEPTED and p.step is not None
    ]
    assert sure, "the rules tick some steps they are sure of"
    extra = sure[0]
    copy = tmp_path / "configs"
    shutil.copytree(config_root, copy)
    path = copy / "agentic" / "policy.yaml"
    text = path.read_text(encoding="utf-8")
    old = "never_ticked_steps: [set_missing, valid_range, keep_latest_per_id]"
    path.write_text(text.replace(old, old[:-1] + f", {extra.value}]"), encoding="utf-8")
    ctx = context_for(frame, config_root=copy)
    for on, ticked in ((False, True), (True, False)):
        switch(on)
        session = accept_recommended(start_session(ctx, session_id="s1"), ctx)
        kinds = {
            p.step.kind for p in session.proposals if p.state is ProposalState.ACCEPTED and p.step is not None
        }
        assert (extra in kinds) is ticked, (on, extra)
    switch(True)
    assert policy_module.never_ticked_steps(ctx.config, copy) >= {extra, RecipeStepKind.VALID_RANGE}
    switch(False)
    assert extra not in policy_module.never_ticked_steps(ctx.config, copy)
