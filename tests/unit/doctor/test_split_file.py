"""`split_file` (Plan O M238, DEC-1808 (e)): two prepared uploads with one recipe; a split by date keeps the
point-in-time guard; never by the ID or the outcome; counted and replayed."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind, recipe_hash
from engine.doctor.split import SplitError, SplitParts, SplitRule, split_file, split_rows
from engine.doctor.tools_rows import ROW_TOOLS, RowToolError, RowToolFrame, SplitFileInput

KEY, TARGET = "customer_id", "churned"
LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE)


def customers() -> pd.DataFrame:
    return pd.DataFrame(
        {
            KEY: ["C1", "C2", "C3", "C4", "C5", "C6"],
            "joined": ["2024-01-10", "2024-02-28", "2024-03-31", "2024-04-01", "2024-05-15", "2024-03-15"],
            "last_order": [
                "2024-02-01",
                "2024-03-01",
                "2024-03-31",
                "2024-04-10",
                "2024-05-20",
                "2024-06-01",
            ],
            "spend": ["₹1,200", "₹300", "₹2,500", "₹50", "₹900", "₹1,000"],
            "segment": ["gold", "silver", "gold", "new", "new", "silver"],
            TARGET: [0, 1, 0, None, None, 1],
        }
    )


BY_DATE = SplitRule(column="joined", by="date", cut="2024-03-31")
PARSE = RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="spend", params={"decimal": "."})


def split(data: pd.DataFrame, rule: SplitRule = BY_DATE, max_failure_pct: float = 5.0) -> SplitParts:
    return split_rows(data, rule, primary_key=KEY, target=TARGET, max_failure_pct=max_failure_pct)


def test_split_file_gives_two_prepared_uploads_with_one_recipe() -> None:
    run = split_file(
        customers(),
        [PARSE],
        BY_DATE,
        upload_id="u1",
        primary_key=KEY,
        target=TARGET,
        levels=LEVELS,
        max_failure_pct=5.0,
        training_upload_id="u1-train",
        scoring_upload_id="u1-score",
    )
    assert run.training.receipt.recipe_hash == run.scoring.receipt.recipe_hash == recipe_hash((PARSE,))
    assert run.split.recipe_hash == recipe_hash((PARSE,))
    assert (run.training.receipt.derived_upload_id, run.scoring.receipt.derived_upload_id) == (
        "u1-train",
        "u1-score",
    )
    assert run.training.frame[KEY].tolist() == ["C1", "C2", "C3", "C6"]
    assert run.scoring.frame[KEY].tolist() == ["C4", "C5"]
    # The one recipe prepared both parts the same way.
    assert run.training.frame["spend"].tolist() == [1200.0, 300.0, 2500.0, 1000.0]
    assert run.scoring.frame["spend"].tolist() == [50.0, 900.0]
    assert list(run.training.frame.columns) == list(run.scoring.frame.columns)
    assert run.training.receipt.rows_out + run.scoring.receipt.rows_out == run.training.receipt.rows_in == 6


def test_a_split_by_date_keeps_the_point_in_time_guard() -> None:
    parts = split(customers())
    joined = pd.to_datetime(parts.training["joined"])
    assert (joined <= pd.Timestamp("2024-03-31 23:59:59")).all()
    assert (pd.to_datetime(parts.scoring["joined"]) > pd.Timestamp("2024-03-31")).all()
    receipt = parts.receipt
    assert receipt.point_in_time == "Every one of the 4 training rows has 'joined' on or before 2024-03-31."
    # C6 joined in March but its last order is in June: written after the cut, so it is reported, not hidden.
    assert [(d.column, d.rows) for d in receipt.later_dates] == [("last_order", 1)]
    assert receipt.warnings == ("SPLIT_LATER_DATES_IN_TRAINING",)


def test_the_whole_cut_day_is_on_or_before_the_cut() -> None:
    data = customers().assign(joined=pd.to_datetime(customers()["joined"]) + pd.Timedelta(hours=18))
    parts = split(data)
    assert "C3" in set(parts.training[KEY])


def test_unreadable_dates_are_counted_and_stop_above_the_limit() -> None:
    data = customers()
    data.loc[0, "joined"] = "soon"
    parts = split(data, max_failure_pct=20.0)
    assert parts.receipt.rows_unassigned == 1
    with pytest.raises(SplitError) as stopped:
        split(data, max_failure_pct=5.0)
    assert stopped.value.code == "SPLIT_VALUES_UNREADABLE"


def test_text_dates_whose_order_nothing_proved_are_not_guessed() -> None:
    data = customers().assign(
        joined=["01/02/2024", "02/03/2024", "03/04/2024", "04/05/2024", "05/06/2024", "06/07/2024"]
    )
    with pytest.raises(SplitError) as stopped:
        split(data)
    assert stopped.value.code == "SPLIT_VALUES_UNREADABLE"
    frozen = SplitRule(column="joined", by="date", cut="2024-03-31", dayfirst=True)
    assert split(data, frozen).receipt.rows_training == 2  # 1 Feb and 2 Mar


@pytest.mark.parametrize("column", [KEY, TARGET])
def test_a_file_is_never_split_by_the_id_or_the_outcome(column: str) -> None:
    with pytest.raises(SplitError) as refused:
        split(customers(), SplitRule(column=column, by="values", scoring_values=("1",)))
    assert refused.value.code == "SPLIT_RULE_INVALID"


def test_a_rule_that_leaves_a_part_empty_is_refused() -> None:
    with pytest.raises(SplitError) as refused:
        split(customers(), SplitRule(column="joined", by="date", cut="2030-01-01"))
    assert refused.value.code == "SPLIT_PART_EMPTY"


def test_a_split_by_values() -> None:
    parts = split(customers(), SplitRule(column="segment", by="values", scoring_values=("new",)))
    assert parts.scoring[KEY].tolist() == ["C4", "C5"]
    assert parts.receipt.rows_unassigned == 0 and parts.receipt.point_in_time is None


def test_the_rule_is_row_wise_so_parts_of_the_file_split_the_same_way() -> None:
    data = customers()
    whole = split(data)
    halves = [data.iloc[[0, 1, 3]], data.iloc[[2, 4, 5]]]
    training = pd.concat(
        [split_rows(h, BY_DATE, primary_key=KEY, target=TARGET, max_failure_pct=5.0).training for h in halves]
    )
    assert sorted(training[KEY]) == sorted(whole.training[KEY])


def test_the_same_rule_on_the_same_file_gives_the_same_receipt() -> None:
    assert split(customers()).receipt == split(customers()).receipt
    assert BY_DATE.rule_hash == SplitRule(column="joined", by="date", cut="2024-03-31").rule_hash


@pytest.mark.parametrize(
    "fields",
    [
        {"column": "joined", "by": "date"},
        {"column": "joined", "by": "date", "cut": "31/03/2024"},
        {"column": "joined", "by": "date", "cut": "2024-03-31", "scoring_values": ("x",)},
        {"column": "segment", "by": "values"},
        {"column": "segment", "by": "values", "scoring_values": ("x",), "cut": "2024-01-01"},
        {"column": "segment", "by": "values", "scoring_values": ("x",), "extra": 1},
    ],
)
def test_a_malformed_rule_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SplitRule(**fields)  # type: ignore[arg-type]


def test_the_tool_proposes_an_approve_class_item_with_the_exact_text() -> None:
    tool = next(t for t in ROW_TOOLS if t.name == "split_file")
    assert (tool.kind, tool.approve_class, tool.role.value) == ("propose", True, "analyst")
    ctx = RowToolFrame(frame=customers(), primary_key=KEY, target=TARGET, as_of=date(2026, 10, 12))
    out = tool.run(SplitFileInput(rule=BY_DATE), ctx)
    assert out.sentence == BY_DATE.sentence()
    assert out.receipt.rows_training == 4 and out.approve_class is True
    with pytest.raises(RowToolError) as refused:
        tool.run(SplitFileInput(rule=SplitRule(column=TARGET, by="values", scoring_values=("1",))), ctx)
    assert refused.value.code == "DOCTOR_COLUMN_PROTECTED"
