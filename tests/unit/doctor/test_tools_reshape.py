"""`propose_combine_rows` (catalogue #87): the advisor's combine question, as a Data Doctor tool.

The cases port `tests/unit/agent/test_advisor_reshape.py`: the same file, the same plan, the same refusals. The
advisor's tests stay; the rules they hold are now this tool's internals too.
"""

from __future__ import annotations

import re

import pytest

from engine.agent.contracts import ProposalSource, RecipeStepKind
from engine.doctor.registry import DataToolContext, DataToolError, data_registry
from engine.doctor.tools_reshape import ProposeCombineRowsInput, propose_combine_rows
from tests.fixtures.agent_bench.make_multirow import multirow_frame
from tests.unit.agent.helpers import context_for

USE_CASE = "retail-win-back"


def _ctx(
    dates: bool = True, target: str | None = "reactivated_90d", key: str | None = "customer_id"
) -> DataToolContext:
    agent = context_for(multirow_frame(dates=dates), USE_CASE, target=target)
    return DataToolContext(agent=agent, primary_key=key)


def test_a_repeating_id_is_proposed_as_a_combine() -> None:
    out = propose_combine_rows(ProposeCombineRowsInput(), _ctx())
    step = out.step
    assert step.kind is RecipeStepKind.COMBINE_ROWS and step.column == "customer_id"
    assert step.source is ProposalSource.RULES and step.ticked is False
    assert step.step_params["snapshot_column"] == "snapshot_date"
    assert step.step_params["outcome"] == "reactivated_90d"
    assert out.rows_per_id == 5.1  # the number in the question is measured on the file
    assert out.rows_in == 7_652 and out.rows_out == out.ids == 1_500
    assert out.question.blocking and out.question.question_id == "reshape-combine-rows"
    assert "7,652" in out.question.text and "1,500" in out.question.text
    assert out.sentence.startswith("Combine the rows into one row per shopper")
    assert "reactivated_90d" not in out.features  # the outcome is read from the latest row, never a feature
    assert all(isinstance(name, str) for name in out.features)


def test_the_tool_returns_names_and_counts_only() -> None:
    dumped = propose_combine_rows(ProposeCombineRowsInput(), _ctx()).model_dump(mode="json")
    frame = multirow_frame()
    names = {str(c) for c in frame.columns}
    cells = {
        str(v)
        for column in frame.columns
        if frame[column].dtype == object
        for v in frame[column].dropna().unique()
    } - names
    words = set(re.findall(r"[A-Za-z0-9_.\-]+", str(dumped)))
    leaked = sorted(cell for cell in cells if cell in words and len(cell) > 3)
    assert leaked == [], leaked[:5]


def test_it_stops_without_a_date_column() -> None:
    with pytest.raises(DataToolError) as caught:
        propose_combine_rows(ProposeCombineRowsInput(), _ctx(dates=False))
    assert "date column" in caught.value.message


def test_the_caller_may_name_the_dates() -> None:
    out = propose_combine_rows(ProposeCombineRowsInput(time_column="snapshot_date"), _ctx())
    assert out.step.step_params["time_column"] == "snapshot_date"
    assert out.step.step_params["snapshot_column"] is None


def test_a_snapshot_needs_its_time_column() -> None:
    with pytest.raises(DataToolError):
        propose_combine_rows(ProposeCombineRowsInput(snapshot_column="snapshot_date"), _ctx())


def test_the_id_and_the_outcome_are_never_dates_or_carried() -> None:
    for given in (
        ProposeCombineRowsInput(time_column="customer_id"),
        ProposeCombineRowsInput(carry=("reactivated_90d",)),
    ):
        with pytest.raises(DataToolError) as caught:
            propose_combine_rows(given, _ctx())
        assert caught.value.code == "DOCTOR_COLUMN_PROTECTED"
    with pytest.raises(DataToolError) as caught:
        propose_combine_rows(ProposeCombineRowsInput(time_column="no_such_column"), _ctx())
    assert caught.value.code == "DOCTOR_COLUMN_UNKNOWN"


def test_it_needs_an_id_column() -> None:
    with pytest.raises(DataToolError):
        propose_combine_rows(ProposeCombineRowsInput(), _ctx(key=None))


def test_one_row_per_id_has_nothing_to_combine() -> None:
    ctx = _ctx()
    assert ctx.agent is not None
    unique = ctx.agent.frame.drop_duplicates("customer_id")
    agent = context_for(unique, USE_CASE, target="reactivated_90d")
    with pytest.raises(DataToolError) as caught:
        propose_combine_rows(
            ProposeCombineRowsInput(), DataToolContext(agent=agent, primary_key="customer_id")
        )
    assert "nothing to combine" in caught.value.message


def test_the_registry_offers_it_to_the_rules_the_ai_and_the_person_never_ticked() -> None:
    tool = data_registry().get("propose_combine_rows")
    assert tool.number == 87 and tool.step_kinds == (RecipeStepKind.COMBINE_ROWS,)
    assert tool.callers == frozenset({"rules", "ai", "person"})
    from engine.doctor.policy import load_data_policy

    assert RecipeStepKind.COMBINE_ROWS in load_data_policy(None).never_ticked_steps


def test_the_ai_action_is_offered_and_its_arguments_are_only_names() -> None:
    from engine.doctor.actions import ACTIONS, ProposeCombineRowsArgs
    from engine.doctor.agent_chat import _MAKES_CHANGES

    spec = next(a for a in ACTIONS if a.name == "propose_combine_rows")
    assert spec.surfaces == frozenset({"review", "chat"}) and spec.needs_file
    assert set(ProposeCombineRowsArgs.model_fields) == {
        "time_column",
        "snapshot_column",
        "carry",
        "columns",
        "replaces",
    }
    assert "propose_combine_rows" in _MAKES_CHANGES


def _consent_ctx(column: str = "marketing_opt_in") -> DataToolContext:
    frame = multirow_frame()
    frame[column] = (frame["customer_id"].astype(str).str[-1] < "5").astype(int)
    frame["email"] = [f"shopper{n}@example.com" for n in range(len(frame))]
    agent = context_for(frame, USE_CASE, target="reactivated_90d")
    config = agent.config.model_copy(
        update={"governance": agent.config.governance.model_copy(update={"consent_column": column})}
    )
    agent = type(agent)(**{**agent.__dict__, "config": config})
    return DataToolContext(agent=agent, primary_key="customer_id")


def test_the_plan_is_a_table_of_columns_with_facts_and_no_cells() -> None:
    out = propose_combine_rows(ProposeCombineRowsInput(), _ctx())
    by = {row.column: row for row in out.plan}
    assert by["order_value"].functions == ("sum", "mean", "max", "latest")
    assert by["order_value"].one_value_per_id is False and by["order_value"].dtype.startswith("float")
    assert "Per column: 'order_value': sum, mean, max, latest" in out.question.text


def test_the_ai_edits_the_plan_with_reasons_and_the_tool_checks_it() -> None:
    from engine.doctor.tools_reshape import CombineColumnChange

    out = propose_combine_rows(
        ProposeCombineRowsInput(
            columns=(
                CombineColumnChange(
                    column="order_value", functions=("mean",), reason="a price: mean, not sum"
                ),
                CombineColumnChange(column="country", functions=(), reason="the same for every order"),
                CombineColumnChange(column="reactivated_90d", functions=("max",), reason="ever reactivated"),
            ),
        ),
        _ctx(),
    )
    by = {row.column: row for row in out.plan}
    assert by["order_value"].functions == ("mean",) and by["order_value"].reason == "a price: mean, not sum"
    assert "country" in by and by["country"].functions == ()
    params = out.step.step_params
    assert [f["name"] for f in params["features"] if f["column"] == "order_value"] == ["order_value_mean"]
    assert not [f for f in params["features"] if f["column"] == "country"]
    assert params["outcome_function"] == "max" and out.outcome_function == "max"
    assert "(a price: mean, not sum)" in out.question.text and "largest value" in out.step.reason
    assert "ever reactivated" in out.step.reason
    with pytest.raises(DataToolError) as caught:
        propose_combine_rows(
            ProposeCombineRowsInput(
                columns=(CombineColumnChange(column="country", functions=("sum",), reason="x"),)
            ),
            _ctx(),
        )
    assert caught.value.code == "TOOL_INPUT_INVALID"
    with pytest.raises(DataToolError) as unknown:
        propose_combine_rows(
            ProposeCombineRowsInput(
                columns=(CombineColumnChange(column="order_value", functions=("median",), reason="x"),)
            ),
            _ctx(),
        )
    assert unknown.value.code == "TOOL_INPUT_INVALID"


def test_the_max_outcome_reads_a_yes_on_any_row_at_the_snapshot() -> None:
    from engine.agent.reshape import combine_rows

    ctx = _ctx()
    latest = propose_combine_rows(ProposeCombineRowsInput(), ctx).step.step_params
    anyrow = propose_combine_rows(ProposeCombineRowsInput(outcome_function="max"), ctx).step.step_params
    frame = ctx.require_agent().frame
    a = combine_rows(frame, key="customer_id", params=latest, max_failure_pct=5.0).frame
    b = combine_rows(frame, key="customer_id", params=anyrow, max_failure_pct=5.0).frame
    assert (b["reactivated_90d"] >= a["reactivated_90d"]).all()


def test_a_consent_or_personal_column_is_never_the_date_nor_combined() -> None:
    from engine.doctor.tools_reshape import CombineColumnChange

    ctx = _consent_ctx()
    out = propose_combine_rows(ProposeCombineRowsInput(), ctx)
    by = {row.column: row for row in out.plan}
    assert by["marketing_opt_in"].carried and by["marketing_opt_in"].functions == ("latest",)
    assert "email" not in by and "email" in out.left_out
    for given in (
        ProposeCombineRowsInput(time_column="marketing_opt_in"),
        ProposeCombineRowsInput(time_column="order_date", snapshot_column="email"),
        ProposeCombineRowsInput(carry=("email",)),
        ProposeCombineRowsInput(
            columns=(CombineColumnChange(column="marketing_opt_in", functions=("sum",), reason="x"),)
        ),
    ):
        with pytest.raises(DataToolError) as caught:
            propose_combine_rows(given, ctx)
        assert caught.value.code == "DOCTOR_COLUMN_PROTECTED", given


def test_the_policy_round_trip_and_its_refusals() -> None:
    from engine.doctor import policy as pol
    from engine.doctor.reshape_guard import doctor_levels

    output = propose_combine_rows(ProposeCombineRowsInput(), _ctx()).model_dump(mode="json")
    policy = pol.load_data_policy(None)
    ctx = pol.DataPolicyContext(
        policy=policy, primary_key="customer_id", target="reactivated_90d", levels=doctor_levels(())
    )
    kind = "data.propose_combine_rows"
    assert pol.check_tool_output(kind, output, ctx) is None
    ticked = pol.check_tool_output(kind, {**output, "step": {**output["step"], "ticked": True}}, ctx)
    assert ticked is not None and ticked.code == "POLICY_NEVER_TICKED"
    unasked = pol.check_tool_output(kind, {**output, "question": None}, ctx)
    assert unasked is not None and unasked.code == "RESHAPE_GUARD_MISSING"
    flat = pol.DataPolicyContext(policy=policy, primary_key="customer_id", target="reactivated_90d")
    no_level = pol.check_tool_output(kind, output, flat)
    assert no_level is not None and no_level.code == "RECIPE_STEP_INVALID"


def test_applying_the_step_gives_one_row_per_id_and_passes_the_full_leak_check() -> None:
    from engine.agent.contracts import AgentLevel
    from engine.agent.recipe import run_recipe

    ctx = _ctx()
    out = propose_combine_rows(ProposeCombineRowsInput(), ctx)
    frame = ctx.require_agent().frame
    run = run_recipe(
        frame,
        [out.step.as_step(1)],
        upload_id="u",
        primary_key="customer_id",
        target="reactivated_90d",
        levels=(AgentLevel.CLEAN, AgentLevel.RESHAPE),
        max_failure_pct=5.0,
        leak_check="full",
    )
    # the full future-data check ran inside the step (it raises on a leak)
    assert len(run.frame) == 1_500 and run.frame["customer_id"].is_unique
