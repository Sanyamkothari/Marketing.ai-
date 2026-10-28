"""Is the dated history long enough for the goal: the span check, the one-season check, the tool and the plan."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.doctor.history import history_plan_findings, read_history
from engine.doctor.registry import DataToolContext, DataToolError, data_registry
from tests.unit.doctor.test_sql_trial_state import loaded


def days(start: str, end: str) -> pd.Series:
    return pd.Series(pd.date_range(start, end, freq="D"))


def codes(series: pd.Series, **kwargs: object) -> list[str]:
    reading = read_history(series, **kwargs)  # type: ignore[arg-type]
    assert reading is not None
    return [f.code for f in reading.findings]


def test_five_months_and_a_ninety_day_window_warns_in_plain_words() -> None:
    reading = read_history(days("2024-01-01", "2024-05-31"), outcome_window_days=90)
    assert reading is not None and "HISTORY_TOO_SHORT" in [f.code for f in reading.findings]
    text = reading.findings[0].text
    assert text.startswith(
        "The dates cover about 5 months, but a 90-day outcome needs 90 days of history before"
    )
    assert "90 days after it (about 6 months)" in text and "Send more history" in text


def test_two_years_pass() -> None:
    assert codes(days("2023-01-01", "2024-12-31"), outcome_window_days=90) == []


def test_a_fixed_training_date_needs_room_on_both_sides() -> None:
    series = days("2023-01-01", "2024-12-31")
    assert codes(series, outcome_window_days=90, training_as_of=date(2024, 6, 1)) == []
    late = read_history(series, outcome_window_days=90, training_as_of=date(2024, 12, 1))
    assert late is not None and late.findings[0].count == 60  # 30 days short after it
    early = read_history(series, outcome_window_days=90, training_as_of=date(2023, 2, 1))
    assert early is not None and early.findings[0].code == "HISTORY_TOO_SHORT"


def test_without_a_window_only_the_season_rule_runs() -> None:
    assert codes(days("2024-01-01", "2024-03-31"), outcome_window_days=None) == ["HISTORY_ONE_SEASON"]
    assert codes(days("2023-01-01", "2024-03-31"), outcome_window_days=None) == []


def seasonal(busy: int = 400, quiet: int = 50) -> pd.Series:
    """Eight months, three of them (April to June) many times busier than the rest."""
    sizes = [quiet, quiet, quiet, busy, busy, busy, quiet, quiet]
    return pd.Series(
        [
            pd.Timestamp(2024, month, 1 + n % 28)
            for month, size in enumerate(sizes, start=1)
            for n in range(size)
        ]
    )


def test_eight_months_mostly_in_three_warn_that_one_season_is_learnt() -> None:
    reading = read_history(seasonal(), outcome_window_days=30)
    assert reading is not None and [f.code for f in reading.findings] == ["HISTORY_ONE_SEASON"]
    assert "one season only" in reading.findings[0].text


def test_the_season_rule_is_more_than_half_in_three_months_under_a_year() -> None:
    def months(sizes: list[int]) -> pd.Series:
        return pd.Series(
            [pd.Timestamp(2024, m, 1 + n % 28) for m, size in enumerate(sizes, start=1) for n in range(size)]
        )

    assert "HISTORY_ONE_SEASON" in codes(months([102] * 3 + [50] * 5), outcome_window_days=30)  # 55%
    assert "HISTORY_ONE_SEASON" not in codes(months([80] * 3 + [50] * 5), outcome_window_days=30)  # 49%
    assert "HISTORY_ONE_SEASON" not in codes(months([50] * 8), outcome_window_days=30)  # 37.5%
    # a file of four months is one season by definition
    assert "HISTORY_ONE_SEASON" in codes(days("2023-10-01", "2024-01-31"), outcome_window_days=30)


def test_an_even_spread_or_a_full_year_is_not_a_season() -> None:
    assert codes(seasonal(busy=50), outcome_window_days=30) == []
    year = pd.Series(
        [pd.Timestamp(2024, m, 1 + n % 28) for m in range(1, 13) for n in range(5 if m < 4 else 1)]
    )
    assert "HISTORY_ONE_SEASON" not in codes(
        pd.concat([year, pd.Series([pd.Timestamp(2025, 1, 1)])]), outcome_window_days=30
    )


def test_a_column_that_is_not_dates_says_nothing() -> None:
    assert read_history(pd.Series(["a", "b", "c"]), outcome_window_days=30) is None


def test_the_plan_gets_flags_nobody_has_to_say_yes_to() -> None:
    found = history_plan_findings(
        days("2024-01-01", "2024-05-31"), outcome_window_days=90, training_as_of=None
    )
    assert ("HISTORY_TOO_SHORT", "flag", False) in [(f.code, f.level, f.hard) for f in found]


def ctx(frame: pd.DataFrame, **extra: object) -> DataToolContext:
    return DataToolContext(
        files=(loaded(frame),),
        caller="ai",
        principal=Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user"),
        **extra,  # type: ignore[arg-type]
    )


def test_the_tool_reports_the_warning_and_refuses_a_column_that_is_not_dates() -> None:
    frame = pd.DataFrame({"order_date": days("2024-01-01", "2024-05-31"), "who": "x"})
    out = dict(
        data_registry()
        .call("check_history", {"column": "order_date", "outcome_window_days": 90}, ctx(frame))
        .output
    )
    assert out["enough"] is False and out["warnings"][0]["code"] == "HISTORY_TOO_SHORT"
    assert out["needed_days"] == 180 and out["months"] == 5
    with pytest.raises(DataToolError):
        data_registry().call("check_history", {"column": "who"}, ctx(frame))
