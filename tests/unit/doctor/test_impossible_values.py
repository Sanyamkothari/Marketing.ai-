"""`impossible_values` (Plan O M238, DEC-1808 (c)): by the column's meaning and type, rules only, pending."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from engine.agent.advisor import NEVER_TICKED_STEPS
from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStepKind
from engine.agent.recipe import run_recipe
from engine.doctor.impossible import ImpossibleFinding, column_words, find_impossible_values

AS_OF = date(2026, 10, 12)
KEY, TARGET = "customer_id", "churned"


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            KEY: [1, 2, 3, 4, 5, 6],
            "age": [25, 130, -2, 40, 33, 50],
            "Umar": [25, 30, 35, 200, 45, 50],
            "tenure_months": [3, -1, 5, 7, 8, 9],
            "bill_amount": [100.0, -50.0, 20.0, 30.0, 0.0, 10.0],
            "orders": [1, 2, -3, 4, 5, 6],
            "discount_pct": [10, 20, 150, 5, 0, 30],
            "balance": [-100, 20, 30, 40, 50, 60],  # a balance may be negative: no rule
            "joined": ["2024-01-02", "2099-12-31", "2027-05-01", "2023-03-03", "9999-12-31", "1890-01-01"],
            "plan_expiry": [
                "2027-01-01",
                "2027-02-01",
                "2026-12-01",
                "2028-01-01",
                "2027-03-01",
                "2027-04-01",
            ],
            "DateOfBirth": [
                "1990-01-01",
                "1900-05-05",
                "2030-01-01",
                "1985-02-02",
                "1970-03-03",
                "2000-04-04",
            ],
            TARGET: [-1, 200, 0, 1, 0, 1],  # never checked
        }
    )


def findings(data: pd.DataFrame | None = None) -> dict[tuple[str, str], ImpossibleFinding]:
    report = find_impossible_values(
        frame() if data is None else data, as_of=AS_OF, primary_key=KEY, target=TARGET
    )
    return {(f.column, f.rule): f for f in report.findings}


def test_numbers_are_judged_by_what_the_header_means() -> None:
    found = findings()
    assert found[("age", "above_maximum")].count == 1
    assert found[("age", "below_minimum")].count == 1
    assert found[("Umar", "above_maximum")].count == 1  # Hindi for age
    assert found[("tenure_months", "below_minimum")].count == 1
    assert found[("bill_amount", "below_minimum")].count == 1
    assert found[("orders", "below_minimum")].count == 1
    assert found[("discount_pct", "above_maximum")].count == 1
    assert not any(column == "balance" for column, _ in found)


def test_dates_are_judged_by_type_and_meaning() -> None:
    found = findings()
    assert found[("joined", "placeholder_date")].count == 2  # 2099 and 9999
    assert found[("joined", "future_date")].count == 1
    assert found[("joined", "before_1900")].count == 1
    # An expiry date may be in the future.
    assert not any(column == "plan_expiry" for column, _ in found)
    assert found[("DateOfBirth", "age_over_120")].count == 1
    assert found[("DateOfBirth", "future_date")].count == 1


def test_the_id_and_the_outcome_are_never_checked() -> None:
    assert not any(column in {KEY, TARGET} for column, _ in findings())


def test_every_fix_is_a_pending_never_ticked_step_of_a_fixed_kind() -> None:
    for finding in findings().values():
        step = finding.step
        if step is None:
            assert finding.doubt is not None
            continue
        assert (step.state, step.ticked, step.confidence) == ("pending", False, "check")
        assert step.kind in {RecipeStepKind.VALID_RANGE, RecipeStepKind.SET_MISSING_TEXT}
    assert RecipeStepKind.VALID_RANGE in NEVER_TICKED_STEPS


def test_a_future_date_is_a_doubt_not_a_fix() -> None:
    finding = findings()[("joined", "future_date")]
    assert finding.step is None and finding.doubt is not None
    assert "2026-10-12" in finding.doubt.because


def test_the_proposed_range_empties_exactly_the_impossible_values() -> None:
    step = findings()[("age", "above_maximum")].step
    assert step is not None and step.params == {"minimum": 0, "maximum": 120}
    run = run_recipe(
        frame(), [step.as_step(1)], upload_id="u", primary_key=KEY, target=TARGET,
        levels=(AgentLevel.CLEAN,), max_failure_pct=5.0,
    )  # fmt: skip
    assert run.receipt.steps[0].changed == step.changed == 2
    assert run.frame["age"].isna().sum() == 2


def test_the_placeholder_fix_runs_before_dates_are_parsed() -> None:
    step = findings()[("joined", "placeholder_date")].step
    assert step is not None and step.kind is RecipeStepKind.SET_MISSING_TEXT
    assert step.params["values"] == ["2099-12-31", "9999-12-31"]


def test_the_answer_depends_on_the_reference_date_only() -> None:
    later = find_impossible_values(frame(), as_of=date(2030, 1, 1), primary_key=KEY, target=TARGET)
    assert ("joined", "future_date") not in {(f.column, f.rule) for f in later.findings}
    assert find_impossible_values(
        frame(), as_of=AS_OF, primary_key=KEY, target=TARGET
    ) == find_impossible_values(frame(), as_of=AS_OF, primary_key=KEY, target=TARGET)


def test_a_parsed_date_column_is_checked_too() -> None:
    data = frame().assign(
        joined=pd.to_datetime(pd.Series(["2024-01-02", "2099-12-31", "2027-05-01", "2023-03-03", None, None]))
    )
    found = findings(data)
    assert found[("joined", "placeholder_date")].count == 1
    assert found[("joined", "placeholder_date")].step is None  # not text any more


@pytest.mark.parametrize(
    ("header", "words"),
    [("DateOfBirth", {"date", "of", "birth"}), ("bill_amount", {"bill", "amount"}), ("usage", {"usage"})],
)
def test_header_words(header: str, words: set[str]) -> None:
    assert column_words(header) == words


def test_text_that_is_not_mostly_numbers_is_left_alone() -> None:
    data = pd.DataFrame({"age_band": ["18-25", "26-35", "-", "36-45"]})
    assert find_impossible_values(data, as_of=AS_OF, primary_key=None, target=None).findings == ()


def test_a_time_later_on_the_reference_day_is_not_in_the_future() -> None:
    # Review fix: the whole reference day has happened; only the next day on is the future.
    data = pd.DataFrame(
        {
            KEY: range(10),
            "signup_at": ["2026-10-12 14:00"] * 5 + ["2026-10-13 09:00"] + ["2026-01-05 10:00"] * 4,
            TARGET: [0, 1] * 5,
        }
    )
    assert findings(data)[("signup_at", "future_date")].count == 1


def test_a_text_date_column_with_many_spellings_is_still_checked() -> None:
    # Review fix: a column with more than 50,000 different timestamps was skipped without a word. Dates are now
    # read once per spelling, whatever their number.
    stamps = pd.date_range("2020-01-01", periods=60_000, freq="min").strftime("%Y-%m-%d %H:%M").tolist()
    stamps[7] = "2099-12-31 00:00"
    data = pd.DataFrame({KEY: range(60_000), "seen_at": stamps, TARGET: 0})
    report = find_impossible_values(data, as_of=AS_OF, primary_key=KEY, target=TARGET)
    assert report.columns_checked == 1
    assert [(f.column, f.rule, f.count) for f in report.findings] == [("seen_at", "placeholder_date", 1)]
