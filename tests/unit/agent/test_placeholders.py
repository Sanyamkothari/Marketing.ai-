"""Placeholder values (DEC-1220 … DEC-1224): detection, the `set_missing` step, its impact, the advice."""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.advisor import NEVER_TICKED_STEPS, advise
from engine.agent.config import AgentLevel
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.agent.placeholders import find_placeholder_values, placeholder_impact, set_missing
from engine.agent.recipe import RecipeError, check_recipe, run_recipe
from engine.agent.session import accept_recommended, start_session
from engine.agent.tools import AgentContext, call_tool
from engine.stages import validate
from tests.unit.agent.helpers import context_for, synthetic

ROWS = 1_000


def _spread(low: int, high: int, rows: int = ROWS) -> list[int]:
    """`low` … `high` evenly, so a test knows the median and the edges exactly."""
    return [low + i % (high - low + 1) for i in range(rows)]


def _with(values: list[Any], code: float, every: int = 40) -> list[Any]:
    """`values` with `code` on every `every`-th row."""
    return [code if i % every == 0 else value for i, value in enumerate(values)]


def _found(frame: pd.DataFrame) -> dict[str, tuple[float, ...]]:
    return {issue.column: issue.values for issue in find_placeholder_values(frame)}


# --- detection: true positives --------------------------------------------------------------------


def test_a_code_far_above_every_other_value_is_found() -> None:
    frame = pd.DataFrame({"tenure_months": _with(_spread(0, 72), 99)})
    (issue,) = find_placeholder_values(frame)
    assert (issue.column, issue.values, issue.rows) == ("tenure_months", (99.0,), (25,))
    assert (issue.other_minimum, issue.other_maximum) == (0.0, 72.0)
    shown = issue.as_json()
    assert shown["kind"] == "placeholder_value"
    assert shown["params"] == {"values": [99]}  # an int, so 99 and 99.0 hash the same
    assert shown["placeholders"] == [{"value": 99, "rows": 25}]
    assert (shown["convertible"], shown["non_empty"], shown["failed"]) == (25, ROWS, 0)


def test_minus_one_in_a_column_that_is_never_negative_is_found() -> None:
    frame = pd.DataFrame({"days_since_order": _with(_spread(0, 400), -1)})
    assert _found(frame) == {"days_since_order": (-1.0,)}


def test_float_forms_and_several_codes_in_one_column_are_found() -> None:
    values = _with(_with([float(v) for v in _spread(0, 50)], 999.0, every=30), -99.0, every=37)
    frame = pd.DataFrame({"score": values})
    assert set(_found(frame)["score"]) == {999.0, -99.0}


def test_a_negative_code_far_below_values_that_do_go_negative_is_found() -> None:
    frame = pd.DataFrame({"temperature": _with(_spread(-30, 40), -999)})
    assert _found(frame) == {"temperature": (-999.0,)}


def test_nullable_integers_with_empty_cells_are_read() -> None:
    values = pd.array(_with(_spread(1, 16), 99), dtype="Int64")
    values[1::50] = pd.NA
    assert _found(pd.DataFrame({"visits": values})) == {"visits": (99.0,)}


# --- detection: the false-positive guard ----------------------------------------------------------


def test_ninety_nine_in_ages_that_commonly_reach_ninety_five_is_a_real_age() -> None:
    frame = pd.DataFrame({"age": _with(_spread(18, 95), 99)})
    assert _found(frame) == {}


def test_a_real_value_inside_the_range_is_never_a_code() -> None:
    frame = pd.DataFrame({"spend": _spread(0, 200)})  # 99 is an ordinary spend here
    assert _found(frame) == {}


def test_minus_one_among_other_negative_values_is_not_a_code() -> None:
    frame = pd.DataFrame({"change": _with(_spread(-5, 40), -1)})
    assert _found(frame) == {}


@pytest.mark.parametrize(
    ("values", "why"),
    [
        (_with(_spread(0, 72), 99, every=250), "only 4 rows: too few to be a code"),
        ([99 if i % 3 else v for i, v in enumerate(_spread(0, 72))], "on most rows: what the column holds"),
        (_with([i % 3 for i in range(ROWS)], 99), "the other values are too few to show where they end"),
        (_with(_spread(0, 72), 98), "98 is not a code"),
        (_with(_spread(0, 72), 99)[:25], "too few numbers to judge"),
    ],
)
def test_no_code_is_reported_without_clear_evidence(values: list[int], why: str) -> None:
    assert _found(pd.DataFrame({"x": values})) == {}, why


def test_a_rare_larger_code_keeps_a_frequent_smaller_one_inside_the_column() -> None:
    """999 on 3 rows stays among the other values, so 99 is not outside them: nothing is reported."""
    values = _with(_with(_spread(0, 72), 99), 999, every=400)
    assert _found(pd.DataFrame({"x": values})) == {}


def test_text_and_boolean_columns_are_left_to_the_format_checks() -> None:
    frame = pd.DataFrame(
        {
            "as_text": [str(v) for v in _with(_spread(0, 72), 99)],
            "flag": [i % 2 == 0 for i in range(ROWS)],
        }
    )
    assert _found(frame) == {}


# --- the step --------------------------------------------------------------------------------------


def test_set_missing_empties_the_listed_values_by_value_and_counts_them() -> None:
    series = pd.Series([99, 5, 99.0, None, 7], dtype="float64")
    out = set_missing(series, values=[99])
    assert out.values.isna().tolist() == [True, False, True, True, False]
    assert (out.changed, out.failed, out.failed_examples) == (2, 0, ())
    assert series.tolist()[0] == 99  # the input is never changed


def test_set_missing_reads_numbers_written_as_text_and_leaves_other_text_alone() -> None:
    series = pd.Series(["99", " 99 ", "ninety-nine", "5", None, True], dtype=object)
    out = set_missing(series, values=[99])
    assert out.values.isna().tolist() == [True, True, False, False, True, False]
    assert out.changed == 2


def test_set_missing_on_nullable_integers_keeps_the_type() -> None:
    series = pd.Series([1, 99, pd.NA, -1], dtype="Int64")
    out = set_missing(series, values=[99, -1])
    assert str(out.values.dtype) == "Int64"
    assert out.values.isna().tolist() == [False, True, True, True]
    assert out.changed == 2


LEVELS = (AgentLevel.CLEAN,)


def _step(order: int, kind: RecipeStepKind, column: str, **params: Any) -> RecipeStep:
    return RecipeStep(order=order, kind=kind, column=column, params=params)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": ["C1", "C2", "C3", "C4", "C5"],
            "tenure": ["12", "99", "30", "99", "7"],
            "visits": [3, 99, 4, -1, 2],
            "email": ["a@b.test"] * 5,
            "converted_30d": [1, 0, 1, 0, 99],
        }
    )


def _run(frame: pd.DataFrame, steps: tuple[RecipeStep, ...]) -> Any:
    return run_recipe(
        frame,
        steps,
        upload_id="u1",
        primary_key="customer_id",
        target="converted_30d",
        levels=LEVELS,
        max_failure_pct=5.0,
    )


def test_a_recipe_runs_set_missing_after_parsing_and_writes_its_receipt() -> None:
    steps = (
        _step(1, RecipeStepKind.PARSE_NUMBER, "tenure", decimal="."),
        _step(2, RecipeStepKind.SET_MISSING, "tenure", values=[99]),
        _step(3, RecipeStepKind.SET_MISSING, "visits", values=[99, -1]),
        _step(4, RecipeStepKind.DROP_COLUMN, "email"),
    )
    frame = _frame()
    run = _run(frame, steps)
    assert run.frame["tenure"].isna().tolist() == [False, True, False, True, False]
    assert run.frame["visits"].tolist()[0] == 3 and run.frame["visits"].isna().sum() == 2
    tenure, visits = run.receipt.steps[1], run.receipt.steps[2]
    assert (tenure.kind, tenure.rows, tenure.changed, tenure.failed) == (RecipeStepKind.SET_MISSING, 5, 2, 0)
    assert (visits.changed, visits.failed, visits.skipped) == (2, 0, False)
    assert frame["visits"].tolist() == [3, 99, 4, -1, 2]  # the input frame is untouched


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"values": []},
        {"values": "99"},
        {"values": ["99"]},
        {"values": [True]},
        {"values": list(range(11))},
        {"values": [99], "also": 1},
    ],
)
def test_check_recipe_refuses_a_set_missing_step_without_a_short_list_of_numbers(
    params: dict[str, Any],
) -> None:
    step = RecipeStep(order=1, kind=RecipeStepKind.SET_MISSING, column="visits", params=params)
    with pytest.raises(RecipeError) as refused:
        check_recipe(
            [step], columns=list(_frame().columns), primary_key="customer_id", target=None, levels=LEVELS
        )
    assert refused.value.code == "RECIPE_STEP_INVALID"


def test_check_recipe_keeps_set_missing_in_its_place_and_off_the_outcome() -> None:
    columns = list(_frame().columns)

    def check(*steps: RecipeStep) -> str:
        with pytest.raises(RecipeError) as refused:
            check_recipe(
                steps, columns=columns, primary_key="customer_id", target="converted_30d", levels=LEVELS
            )
        return refused.value.code

    before_parse = (
        _step(1, RecipeStepKind.SET_MISSING, "tenure", values=[99]),
        _step(2, RecipeStepKind.PARSE_NUMBER, "tenure", decimal="."),
    )
    assert check(*before_parse) == "RECIPE_STEP_INVALID"
    after_drop = (
        _step(1, RecipeStepKind.DROP_COLUMN, "email"),
        _step(2, RecipeStepKind.SET_MISSING, "visits", values=[99]),
    )
    assert check(*after_drop) == "RECIPE_STEP_INVALID"
    assert check(_step(1, RecipeStepKind.SET_MISSING, "converted_30d", values=[99])) == "RECIPE_STEP_INVALID"
    assert check(_step(1, RecipeStepKind.SET_MISSING, "no_such", values=[99])) == "RECIPE_COLUMN_MISSING"
    with pytest.raises(ValueError, match="empties cells of its own column"):
        RecipeStep(
            order=1,
            kind=RecipeStepKind.SET_MISSING,
            column="visits",
            new_column="v2",
            params={"values": [99]},
        )


def test_the_same_step_prepares_next_months_file_identically() -> None:
    """Stateless: a scoring file's cells are emptied by the listed values alone, not by its own statistics."""
    step = (_step(1, RecipeStepKind.SET_MISSING, "visits", values=[99]),)
    later = pd.DataFrame({"customer_id": ["D1", "D2"], "visits": [99, 250], "converted_30d": [0, 1]})
    run = _run(later, step)
    assert run.frame["visits"].isna().tolist() == [True, False]  # 250 is a real value; only 99 is listed


# --- impact ---------------------------------------------------------------------------------------


def test_impact_numbers_on_a_hand_built_frame() -> None:
    column = pd.Series([1, 2, 3, 4, 99, 99, 5, 6, 7, 8] * 20, dtype="int64")
    positive = pd.Series([0, 0, 1, 0, 1, 1, 0, 1, 0, 0] * 20, dtype="float64")
    impact = placeholder_impact(column, [99], positive=positive)
    assert impact["rows"] == 200 and impact["affected_rows"] == 40 and impact["affected_share"] == 0.2
    assert impact["mean_before"] == pytest.approx((36 + 198) / 10) and impact["mean_after"] == 4.5
    assert impact["median_before"] == 5.5 and impact["median_after"] == 4.5
    assert impact["affected_positive_rate"] == 1.0
    assert impact["other_positive_rate"] == 0.25  # 2 of the 8 other rows per block are "yes"
    # By hand, per block: "yes" rows hold 3, 6, 99, 99 and "no" rows 1, 2, 4, 5, 7, 8. Before, 3 beats 2 of
    # the six, 6 beats 4 and each 99 beats all six: 18 / 24 = 0.75. After, only 3 and 6 are left: 6 / 12 =
    # 0.5 - the code carried the "yes" rows' signal.
    assert (impact["auc_before"], impact["auc_after"]) == (0.75, 0.5)
    after = column.astype("float64").mask(column == 99)
    before_auc = validate.single_feature_auc(column.astype("float64"), positive)
    after_auc = validate.single_feature_auc(after, positive)
    assert before_auc is not None and after_auc is not None  # the leakage check's own helper agrees
    assert (impact["auc_before"], impact["auc_after"]) == (round(before_auc, 4), round(after_auc, 4))


def test_impact_without_a_known_outcome_says_nothing_about_it() -> None:
    impact = placeholder_impact(pd.Series([1, 2, 99] * 20), [99])
    assert impact["affected_rows"] == 20
    for key in ("affected_positive_rate", "other_positive_rate", "auc_before", "auc_after"):
        assert impact[key] is None, key


# --- the tools --------------------------------------------------------------------------------------


def _placeholder_frame() -> pd.DataFrame:
    frame = synthetic(rows=3_000)
    frame.loc[frame.index % 25 == 0, "visits_last_7d"] = 99
    return frame


def _ctx(frame: pd.DataFrame, **kwargs: Any) -> AgentContext:
    return context_for(frame, **kwargs)


def test_find_format_issues_lists_the_placeholder_but_never_on_the_outcome() -> None:
    frame = _placeholder_frame()
    frame.loc[frame.index % 20 == 0, "converted_30d"] = 99
    issues = call_tool(_ctx(frame, target="converted_30d"), "find_format_issues", {}, evidence_id="e1").result
    kinds = {(i["column"], i["kind"]) for i in issues["issues"]}  # type: ignore[union-attr, index]
    assert ("visits_last_7d", "placeholder_value") in kinds
    assert not any(column == "converted_30d" for column, _ in kinds)


def test_describe_placeholder_values_measures_with_and_without_the_outcome() -> None:
    frame = _placeholder_frame()
    known = call_tool(
        _ctx(frame, target="converted_30d"),
        "describe_placeholder_values",
        {"column": "visits_last_7d"},
        evidence_id="e1",
    ).result
    assert known["values"] == ["99"] and known["affected_rows"] == 120
    assert known["auc_before"] is not None and known["affected_positive_rate"] is not None
    unknown = call_tool(
        _ctx(frame),
        "describe_placeholder_values",
        {"column": "visits_last_7d", "values": [99]},
        evidence_id="e2",
    ).result
    assert unknown["affected_rows"] == 120 and unknown["auc_before"] is None
    nothing = call_tool(
        _ctx(frame), "describe_placeholder_values", {"column": "tenure_months"}, evidence_id="e3"
    )
    assert nothing.result["values"] == [] and "message" in nothing.result


# --- the advice -------------------------------------------------------------------------------------


def test_the_advisor_proposes_set_missing_to_check_with_its_measured_impact() -> None:
    advice = advise(_ctx(_placeholder_frame()))
    (proposal,) = [
        p for p in advice.proposals if p.step is not None and p.step.kind is RecipeStepKind.SET_MISSING
    ]
    assert proposal.confidence.value == "check" and proposal.state is ProposalState.PENDING
    assert proposal.step is not None and proposal.step.params == {"values": [99]}
    assert proposal.title == "Treat '99' in 'visits_last_7d' as missing"
    assert proposal.reason.startswith(
        "120 rows in 'visits_last_7d' hold '99', far above every other value (the largest is '16'). "
        "It is probably a code for unknown, not a real value."
    )
    assert proposal.examples == ("99", "16")
    cited = {r.evidence_id: r for r in advice.tool_results if r.evidence_id in proposal.evidence_ids}
    assert {r.tool for r in cited.values()} == {"find_format_issues", "describe_placeholder_values"}
    impact = next(r for r in cited.values() if r.tool == "describe_placeholder_values").result
    assert impact["affected_rows"] == 120 and impact["auc_before"] is not None  # the outcome was known


def test_a_set_missing_suggestion_is_never_ticked_for_the_person() -> None:
    ctx = _ctx(_placeholder_frame())
    eager = dataclasses.replace(
        ctx,
        config=ctx.config.model_copy(
            update={"agent": ctx.config.agent.model_copy(update={"tick_uncertain": True})}
        ),
    )
    assert RecipeStepKind.SET_MISSING in NEVER_TICKED_STEPS
    session = accept_recommended(start_session(eager, session_id="s1"), eager)
    (proposal,) = [
        p for p in session.proposals if p.step is not None and p.step.kind is RecipeStepKind.SET_MISSING
    ]
    assert proposal.state is ProposalState.PENDING, "even with tick_uncertain, only the person decides"
    others = [p for p in session.proposals if p is not proposal]
    assert all(p.state is ProposalState.ACCEPTED for p in others)


def test_a_target_holding_a_code_still_stops_as_not_binary_and_gets_no_set_missing() -> None:
    frame = synthetic(rows=3_000)
    frame.loc[frame.index % 20 == 0, "converted_30d"] = 99
    advice = advise(_ctx(frame))
    assert advice.status.value == "stopped" and advice.target == "converted_30d"
    assert advice.stop_reason is not None and "A yes/no model needs exactly two" in advice.stop_reason
    assert not any(p.step is not None and p.step.kind is RecipeStepKind.SET_MISSING for p in advice.proposals)
    checks = next(r for r in advice.tool_results if r.tool == "check_data").result["checks"]
    codes = {c["code"] for c in checks if c["severity"] == "error"}  # type: ignore[index, union-attr]
    assert "TARGET_NOT_BINARY" in codes


def test_detection_ignores_row_order() -> None:
    frame = pd.DataFrame({"tenure_months": _with(_spread(0, 72), 99)})
    shuffled = frame.sample(frac=1.0, random_state=np.random.default_rng(1).integers(1_000))
    assert _found(frame) == _found(shuffled)
