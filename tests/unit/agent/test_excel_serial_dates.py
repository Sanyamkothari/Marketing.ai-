"""Dates stored as Excel's day numbers (Plan O M240, DEC-1810 (d))."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.formats import (
    FormatIssueKind,
    find_excel_serial_dates,
    find_format_issues,
    looks_like_date_name,
    parse_dates,
    parse_excel_serial_dates,
)
from engine.agent.recipe import RecipeError, check_recipe, run_recipe

LEVELS = (AgentLevel.CLEAN,)


def series(*values: Any) -> pd.Series[Any]:
    return pd.Series(list(values))


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------
def test_day_numbers_become_dates() -> None:
    out = parse_excel_serial_dates(series(44927, 45000, 36526))
    assert out.values.tolist() == [
        pd.Timestamp("2023-01-01"),
        pd.Timestamp("2023-03-15"),
        pd.Timestamp("2000-01-01"),
    ]
    assert (out.changed, out.failed) == (3, 0)


def test_a_fraction_is_the_time_of_day_rounded_to_the_millisecond() -> None:
    out = parse_excel_serial_dates(series(44927.5, 44927.25, 44927.123456789))
    assert out.values.tolist()[:2] == [pd.Timestamp("2023-01-01 12:00"), pd.Timestamp("2023-01-01 06:00")]
    assert out.values.iloc[2] == pd.Timestamp("2023-01-01 02:57:46.667")


def test_text_numbers_and_floats_read_alike_and_empty_cells_stay_empty() -> None:
    out = parse_excel_serial_dates(series("44927", " 44928 ", 44929.0, None, ""))
    assert out.values.iloc[:3].tolist() == [
        pd.Timestamp("2023-01-01"),
        pd.Timestamp("2023-01-02"),
        pd.Timestamp("2023-01-03"),
    ]
    assert out.values.iloc[3:].isna().all() and (out.changed, out.failed) == (3, 0)


@pytest.mark.parametrize(
    "bad", ["2023-01-01", "abc", "44927x", -5, 0, 60, 3_000_000, 1e12, float("inf"), "1e999"]
)
def test_what_is_not_a_date_fails_and_is_counted_not_guessed(bad: Any) -> None:
    out = parse_excel_serial_dates(series(44927, bad))
    assert out.values.iloc[0] == pd.Timestamp("2023-01-01") and pd.isna(out.values.iloc[1])
    assert (out.changed, out.failed) == (1, 1) and len(out.failed_examples) == 1


def test_the_first_day_excel_counts_correctly_is_the_first_of_march_1900() -> None:
    out = parse_excel_serial_dates(series(61, 59))
    assert out.values.iloc[0] == pd.Timestamp("1900-03-01") and pd.isna(out.values.iloc[1])


def test_the_mac_workbook_calendar_starts_in_1904() -> None:
    out = parse_excel_serial_dates(series(0, 1, 43465), date_1904=True)
    assert out.values.tolist() == [
        pd.Timestamp("1904-01-01"),
        pd.Timestamp("1904-01-02"),
        pd.Timestamp("2023-01-01"),
    ]


def test_a_column_that_is_already_dates_is_left_alone() -> None:
    dates = pd.Series(pd.to_datetime(["2023-01-01", "2023-01-02"]))
    out = parse_excel_serial_dates(dates)
    assert out.values.equals(dates) and (out.changed, out.failed) == (0, 0)


def test_the_parse_is_stateless_one_value_does_not_depend_on_the_rest() -> None:
    whole = parse_excel_serial_dates(series(44927, 44930, "x", 45000)).values
    for position, value in enumerate([44927, 44930, "x", 45000]):
        alone = parse_excel_serial_dates(series(value)).values.iloc[0]
        assert (pd.isna(alone) and pd.isna(whole.iloc[position])) or alone == whole.iloc[position]


# ---------------------------------------------------------------------------
# The recipe step
# ---------------------------------------------------------------------------
def step(order: int = 1, column: str = "joined", **params: Any) -> RecipeStep:
    return RecipeStep(order=order, kind=RecipeStepKind.PARSE_DATE, column=column, params=params)


def run(frame: pd.DataFrame, *steps: RecipeStep) -> Any:
    return run_recipe(
        frame,
        steps,
        upload_id="u1",
        primary_key="customer_id",
        target=None,
        levels=LEVELS,
        max_failure_pct=5.0,
    )


FRAME = pd.DataFrame({"customer_id": ["a", "b", "c"], "joined": [44927, 45000, 45292], "n": [1, 2, 3]})


def test_a_parse_date_step_with_excel_serial_prepares_the_column_and_counts_the_effect() -> None:
    result = run(FRAME, step(excel_serial=True))
    assert result.frame["joined"].tolist() == [
        pd.Timestamp("2023-01-01"),
        pd.Timestamp("2023-03-15"),
        pd.Timestamp("2024-01-01"),
    ]
    receipt = result.receipt.steps[0]
    assert (receipt.kind, receipt.rows, receipt.changed, receipt.failed) == (
        RecipeStepKind.PARSE_DATE,
        3,
        3,
        0,
    )
    assert FRAME["joined"].tolist() == [44927, 45000, 45292]  # the input is never modified


def test_the_1904_calendar_is_a_second_flag_and_changes_the_dates() -> None:
    assert run(FRAME, step(excel_serial=True, date_1904=True)).frame["joined"].iloc[0] == pd.Timestamp(
        "2027-01-02"
    )


def test_a_replay_gives_the_same_rows_and_the_same_hash() -> None:
    first, second = run(FRAME, step(excel_serial=True)), run(FRAME, step(excel_serial=True))
    pd.testing.assert_frame_equal(first.frame, second.frame)
    assert first.receipt.recipe_hash == second.receipt.recipe_hash == recipe_hash((step(excel_serial=True),))
    assert recipe_hash((step(excel_serial=True),)) != recipe_hash((step(dayfirst=True),))


def test_a_day_month_step_is_unchanged_and_still_needs_its_order() -> None:
    assert run(pd.DataFrame({"customer_id": ["a"], "joined": ["25/12/2023"]}), step(dayfirst=True)).frame[
        "joined"
    ].iloc[0] == pd.Timestamp("2023-12-25")
    with pytest.raises(RecipeError, match="day/month order must be decided"):
        run(FRAME, step())
    with pytest.raises(RecipeError, match="day/month order must be decided"):
        run(FRAME, step(excel_serial=False))


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"excel_serial": True, "dayfirst": True}, "no day/month order"),
        ({"excel_serial": "yes"}, "true or false"),
        ({"excel_serial": True, "date_1904": 1}, "true or false"),
        ({"date_1904": True, "dayfirst": True}, "only applies to excel_serial"),
        ({"excel_serial": True, "calendar": "mac"}, "unknown parameter"),
    ],
)
def test_the_step_refuses_what_it_cannot_read(params: dict[str, Any], fragment: str) -> None:
    with pytest.raises(RecipeError, match=fragment):
        check_recipe(
            (step(**params),),
            columns=list(FRAME.columns),
            primary_key="customer_id",
            target=None,
            levels=LEVELS,
        )


def test_the_step_may_not_touch_the_id_or_the_outcome() -> None:
    with pytest.raises(RecipeError):
        run(FRAME, step(column="customer_id", excel_serial=True))
    with pytest.raises(RecipeError):
        run_recipe(
            FRAME,
            (step(column="n", excel_serial=True),),
            upload_id="u1",
            primary_key="customer_id",
            target="n",
            levels=LEVELS,
            max_failure_pct=5.0,
        )


def test_values_that_are_not_day_numbers_are_counted_and_over_the_limit_the_recipe_stops() -> None:
    frame = pd.DataFrame({"customer_id": list("abcd"), "joined": ["44927", "x", "44929", "44930"]})
    with pytest.raises(RecipeError) as caught:
        run(frame, step(excel_serial=True))
    assert caught.value.code == "RECIPE_VALUES_UNCONVERTED"


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("joined", True),
        ("signup_date", True),
        ("LastLoginDate", True),
        ("created_at", True),
        ("dob", True),
        ("startdate", True),
        ("date_of_birth", True),
        ("plan_expiry", True),
        ("amount", False),
        ("candidate_score", False),
        ("update_count", False),
        ("order_value", False),
        ("at", False),
    ],
)
def test_the_name_says_whether_a_column_holds_dates(name: str, expected: bool) -> None:
    assert looks_like_date_name(name) is expected


def issues(**columns: Any) -> list[Any]:
    return find_excel_serial_dates(pd.DataFrame(columns))


def test_a_date_named_column_of_day_numbers_is_found_with_the_fix_and_what_to_check() -> None:
    found = issues(signup_date=[44927, 45000, 45292, 44000, 43000, 45100])
    assert len(found) == 1
    issue = found[0]
    assert (issue.column, issue.kind) == ("signup_date", FormatIssueKind.EXCEL_SERIAL_DATES)
    assert issue.params == {"excel_serial": True, "date_1904": False}
    assert (issue.non_empty, issue.convertible, issue.failed) == (6, 6, 0)
    assert any("2017" in note or "2020" in note for note in issue.notes)
    assert any("1904 calendar" in note for note in issue.notes)
    assert all(example.isdigit() for example in issue.examples)


def test_text_day_numbers_are_found_too() -> None:
    assert [i.column for i in issues(created=["44927", "45000", "45292", "44000", "43000"])] == ["created"]


def test_a_column_with_no_date_in_its_name_is_never_a_date_whatever_it_holds() -> None:
    assert issues(amount=[44927, 45000, 45292, 44000, 43000], plan_value=[44927.5] * 5) == []


def test_numbers_outside_the_years_1920_to_2100_are_not_dates() -> None:
    assert issues(event_date=[1, 2, 3, 4, 5, 6]) == []
    assert issues(event_date=[120000, 130000, 140000, 150000, 160000]) == []
    assert issues(event_date=[44927, 45000, 7, 8, 9, 10]) == []  # fewer than 95% inside


def test_a_few_cells_are_not_enough_and_a_dates_column_is_not_reported() -> None:
    assert issues(event_date=[44927, 45000, 45292]) == []
    assert issues(event_date=pd.to_datetime(["2023-01-01"] * 6)) == []
    assert issues(event_date=["2023-01-01"] * 6) == []
    assert issues(event_date=[True, False] * 3) == []


def test_empty_cells_do_not_count_against_a_column() -> None:
    found = issues(event_date=[44927, None, 45000, 45292, 44000, 43000, None])
    assert found and found[0].non_empty == 5


def test_find_format_issues_is_unchanged_it_does_not_report_day_numbers() -> None:
    frame = pd.DataFrame({"signup_date": [44927, 45000, 45292, 44000, 43000, 45100]})
    assert not find_format_issues(frame)
    text = pd.DataFrame({"signup_date": ["44927", "45000", "45292", "44000", "43000", "45100"]})
    assert FormatIssueKind.EXCEL_SERIAL_DATES not in {i.kind for i in find_format_issues(text)}


def test_what_the_detector_finds_the_step_then_converts_without_loss() -> None:
    frame = pd.DataFrame(
        {"customer_id": list("abcdef"), "signup_date": [44927, 45000, 45292, 44000, 43000, 45100]}
    )
    (issue,) = find_excel_serial_dates(frame)
    out = run(frame, step(column=issue.column, **issue.params))
    assert out.frame["signup_date"].notna().all() and out.receipt.steps[0].failed == 0
    assert np.all(out.frame["signup_date"].dt.year.between(2017, 2024))


def test_the_ordinary_date_parser_would_not_have_read_them() -> None:
    """Without `excel_serial` a day number is no date: every value fails and is counted (nothing is guessed)."""
    for cells in (series("44927", "45000"), series(44927, 45000)):
        assert parse_dates(cells, dayfirst=False).failed == 2
