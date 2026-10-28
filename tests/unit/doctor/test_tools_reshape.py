"""`propose_combine_rows` (catalogue #87): the advisor's combine question, as a Data Doctor tool.

The cases port `tests/unit/agent/test_advisor_reshape.py`: the same file, the same plan, the same refusals. The
advisor's tests stay; the rules they hold are now this tool's internals too.
"""

from __future__ import annotations

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
    cells = {str(v) for v in frame["customer_id"].head(50)}
    text = str(dumped)
    assert not any(cell in text for cell in list(cells)[:5] if len(cell) > 6)


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
    assert set(ProposeCombineRowsArgs.model_fields) == {"time_column", "snapshot_column", "carry", "replaces"}
    assert "propose_combine_rows" in _MAKES_CHANGES
