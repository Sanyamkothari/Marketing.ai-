"""Time, leak, key and date guards: each one on a small frame that has exactly the fault, counted by our code."""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.formats import date_order, date_order_conflict, find_format_issues, order_unsettled
from engine.agent.key_steps import KeyColumn, KeyMergeError, KeyRule, hard_merges, normalise_keys
from engine.agent.recipe import RecipeError, run_recipe
from engine.agent.sql_step import (
    AsOfRule,
    SqlStepError,
    check_sql,
    future_rows_moved,
    resolve_as_of_rule,
)
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.checks_time_keys import (
    Check,
    block_blank_checks,
    date_checks,
    date_order_checks,
    file_checks,
    key_quality,
    key_rule_checks,
    outcome_direction,
    summary_check,
    tail_checks,
    unit_scale_checks,
)
from engine.doctor.detect_time_keys import CHECK_KIND, MAX_QUESTIONS, time_keys_detector
from engine.doctor.diagnose import Offer
from engine.doctor.impossible import _as_dates
from engine.stages import validate as v
from engine.stages.ingest import read_sample
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs

LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)
AS_OF = date(2026, 1, 31)


def codes(checks: list[Check]) -> set[str]:
    return {check.code for check in checks}


# ---------------------------------------------------------------------------
# G10: the SQL step takes its date from $as_of, as a rule, and does not use what came later
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t_orders WHERE order_date > DATE '2025-12-01'",
        "SELECT * FROM t_orders WHERE order_date > '2025-12-01'",
        "SELECT * FROM t_orders WHERE order_date > '01/12/2025'",
        "SELECT * FROM t_orders WHERE order_date > make_date(2025, 12, 1)",
        "SELECT * FROM t_orders WHERE order_date > strptime('2025-12-01 10:00:00', '%Y-%m-%d %H:%M:%S')",
        "SELECT * FROM t_orders WHERE order_date > CAST('2025-' || '12-01' AS DATE)",
        "SELECT * FROM t_orders WHERE order_date > make_date(2025, 12, 1 + 0)",
        "SELECT * FROM t_orders WHERE order_date > strptime('20251201', '%Y%m%d')",
        "SELECT * FROM t_orders WHERE order_date > to_timestamp(1764547200)",
    ],
)
def test_a_date_typed_into_the_query_is_refused(sql: str) -> None:
    with pytest.raises(SqlStepError) as caught:
        check_sql(sql, ["t_orders"], as_of=AS_OF)
    assert caught.value.code == "SQL_FIXED_DATE"


def test_the_refusal_of_a_typed_date_names_the_literal_and_what_replaces_it() -> None:
    sql = "SELECT * FROM t_orders WHERE order_date <= $as_of AND order_date < DATE '2026-05-01'"
    with pytest.raises(SqlStepError) as caught:
        check_sql(sql, ["t_orders"], as_of=AS_OF)
    assert caught.value.code == "SQL_FIXED_DATE"
    assert "'2026-05-01'" in caught.value.message and "$as_of + INTERVAL '90' DAY" in caught.value.message
    with pytest.raises(SqlStepError) as before:
        check_sql("SELECT * FROM t_orders WHERE order_date > '2026-01-01'", ["t_orders"], as_of=AS_OF)
    assert "$as_of - INTERVAL '30' DAY" in before.value.message


def test_the_as_of_date_itself_and_text_that_only_looks_like_one_are_allowed() -> None:
    check_sql("SELECT * FROM t_orders WHERE order_date <= DATE '2026-01-31'", ["t_orders"], as_of=AS_OF)
    check_sql("SELECT * FROM t_orders WHERE order_date <= $as_of", ["t_orders"], as_of=AS_OF)
    check_sql("SELECT * FROM t_orders WHERE order_id = 'C-2025-12-01'", ["t_orders"], as_of=AS_OF)
    check_sql("SELECT make_date(y, m, 1) FROM t_orders", ["t_orders"], as_of=AS_OF)
    with pytest.raises(SqlStepError):  # no as-of date given: nothing typed in is the as-of date
        check_sql("SELECT * FROM t_orders WHERE order_date <= DATE '2026-01-31'", ["t_orders"])


def test_the_as_of_rules_give_the_latest_date_and_the_last_complete_month_end() -> None:
    column = pd.Series(["2026-01-05", "2026-01-20", "2026-02-11"])
    latest = AsOfRule(rule="latest_date_in_file", table="orders", column="d")
    month_end = AsOfRule(rule="last_complete_month_end", table="orders", column="d")
    assert resolve_as_of_rule(latest, column) == date(2026, 2, 11)
    assert resolve_as_of_rule(month_end, column) == date(2026, 1, 31)
    assert resolve_as_of_rule(month_end, pd.Series(["2026-01-31", "2026-02-28"])) == date(2026, 2, 28)
    with pytest.raises(SqlStepError):
        resolve_as_of_rule(latest, pd.Series(["03/04/2026"]))


def _orders(days: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {"customer_id": ["C1", "C2"] * (len(days) // 2), "order_date": days, "amount": range(len(days))}
    )


def _sql_step(sql: str, **extra: Any) -> RecipeStep:
    params: dict[str, Any] = {
        "sql": sql,
        "inputs": ["this"],
        "output_name": "totals",
        "description": "Add up the bills.",
        **extra,
    }
    return RecipeStep(order=1, kind=RecipeStepKind.SQL_QUERY, column="totals", params=params)


def _run(frame: pd.DataFrame, step: RecipeStep, **kwargs: Any) -> pd.DataFrame:
    return run_recipe(
        frame,
        [step],
        upload_id="u",
        primary_key=None,
        target=None,
        levels=LEVELS,
        max_failure_pct=5.0,
        **kwargs,
    ).frame


def test_the_saved_rule_is_worked_out_again_on_next_months_file() -> None:
    sql = "SELECT customer_id, sum(amount) AS total FROM t_this WHERE CAST(order_date AS DATE) <= $as_of GROUP BY 1 ORDER BY 1"
    rule = {"rule": "latest_date_in_file", "table": "this", "column": "order_date"}
    step = _sql_step(sql, as_of="2026-01-10", as_of_rule=rule)
    january = _orders(["2026-01-05", "2026-01-10", "2026-01-10", "2026-01-10"])
    february = _orders(["2026-01-05", "2026-01-10", "2026-02-20", "2026-02-20"])
    assert _run(january, step)["total"].tolist() == [2, 4]  # everything up to the latest date: 0+2, 1+3
    assert _run(february, step)["total"].tolist() == [2, 4]  # next month's latest date, not 2026-01-10
    frozen = _sql_step(sql, as_of="2026-01-10")
    assert _run(february, frozen)["total"].tolist() == [0, 1]  # a date frozen in the step cuts next month off


def test_a_table_that_uses_rows_dated_after_the_as_of_date_is_caught() -> None:
    frame = _orders(["2026-01-05", "2026-01-20", "2026-02-11", "2026-02-12"])
    leaky = "SELECT customer_id, sum(amount) AS total FROM t_this GROUP BY 1 ORDER BY 1"
    clean = "SELECT customer_id, sum(amount) AS total FROM t_this WHERE CAST(order_date AS DATE) <= $as_of GROUP BY 1 ORDER BY 1"
    assert future_rows_moved(leaky, {"this": frame}, output_name="t", as_of=AS_OF) == 2
    assert future_rows_moved(clean, {"this": frame}, output_name="t", as_of=AS_OF) == 0
    assert future_rows_moved(clean, {"this": frame.head(2)}, output_name="t", as_of=AS_OF) is None
    with pytest.raises(RecipeError) as caught:
        _run(frame, _sql_step(leaky, as_of="2026-01-31"), leak_check="full")
    assert caught.value.code == "FUTURE_EVENTS_LEAKED"
    assert len(_run(frame, _sql_step(clean, as_of="2026-01-31"), leak_check="full")) == 2
    # A query that builds the outcome is let off for the outcome column only, never for the whole table.
    outcome_step = _sql_step(leaky, as_of="2026-01-31", reads_future=True)
    built = run_recipe(
        frame,
        [outcome_step],
        upload_id="u",
        primary_key=None,
        target="total",
        levels=LEVELS,
        max_failure_pct=5.0,
        leak_check="full",
    ).frame
    assert len(built) == 2
    with pytest.raises(RecipeError) as caught:  # no outcome named: every column is checked
        _run(frame, outcome_step, leak_check="full")
    assert caught.value.code == "FUTURE_EVENTS_LEAKED"
    feature = _sql_step(
        "SELECT customer_id, sum(amount) AS spend, count(*) AS total FROM t_this GROUP BY 1 ORDER BY 1",
        as_of="2026-01-31",
        reads_future=True,
    )
    with pytest.raises(RecipeError) as caught:  # `spend` reads later rows and is not the outcome
        run_recipe(
            frame,
            [feature],
            upload_id="u",
            primary_key=None,
            target="total",
            levels=LEVELS,
            max_failure_pct=5.0,
            leak_check="full",
        )
    assert caught.value.code == "FUTURE_EVENTS_LEAKED"
    assert len(_run(frame, _sql_step(leaky, as_of="2026-01-31"))) == 2  # a preview does not run the check


# ---------------------------------------------------------------------------
# G4 / G5: keys
# ---------------------------------------------------------------------------
def test_a_rule_that_makes_two_customers_one_is_refused_and_counted() -> None:
    ids = pd.Series(["C-123", "123", "C-456", "789"])
    assert hard_merges(ids, KeyRule(prefix="C-")) == 1
    assert hard_merges(pd.Series(["007", "7", "8"]), KeyRule(zeros=True)) == 1
    assert hard_merges(pd.Series(["C-1", "S-1", "C-2"]), KeyRule(prefix="C-")) == 0  # S-1 keeps its letters
    with pytest.raises(KeyMergeError) as caught:
        normalise_keys(ids, KeyRule(prefix="C-"))
    assert caught.value.merged == 1
    step = RecipeStep(order=1, kind=RecipeStepKind.NORMALISE_KEY, column="id", params={"prefix": "C-"})
    with pytest.raises(RecipeError) as refused:
        _run(pd.DataFrame({"id": ids}), step)
    assert refused.value.code == "KEY_RULE_MERGES_IDS"
    stop = key_rule_checks(ids, KeyRule(prefix="C-"), "id")[0]
    assert (
        stop.level == "stop" and stop.numbers["distinct_before"] == 4 and stop.numbers["distinct_after"] == 3
    )


def test_capitals_and_spaces_that_join_spellings_are_shown_not_refused() -> None:
    ids = pd.Series(["c-77", "C-77", " C-78", "C-78"])
    shown = key_rule_checks(ids, KeyRule(case=True, spaces=True), "id")
    assert len(shown) == 1 and shown[0].level == "flag" and shown[0].text.startswith("IDs merged: 2")
    assert KeyColumn(ids).merged_by(KeyRule(case=True, spaces=True)) == 2
    assert normalise_keys(ids, KeyRule(case=True, spaces=True)).failed == 0


def test_key_report_names_rounded_digits_stand_ins_and_lost_zeros() -> None:
    rounded = pd.Series([f"{1 + n % 9}.{n:05d}E+11" for n in range(29)] + ["9876543210"])
    found = key_quality(rounded, "mobile")
    stop = next(check for check in found if check.level == "stop")
    assert stop.code == "KEY_DIGITS_ROUNDED" and stop.numbers["rounded"] == 29
    floats = pd.Series([123_457_000_000.0 + 100_000 * n for n in range(30)])
    assert "KEY_DIGITS_ROUNDED" in codes(key_quality(floats, "mobile"))

    phones = pd.Series(["9999999999"] * 12 + [f"98{n:08d}" for n in range(88)])
    placeholder = next(check for check in key_quality(phones, "phone") if check.code == "KEY_PLACEHOLDER")
    assert placeholder.level == "flag" and placeholder.numbers["rows"] == 12
    mails = pd.Series(["test@example.com"] * 3 + [f"a{n}@x.com" for n in range(40)] + ["NA", "NA"])
    assert "KEY_PLACEHOLDER" in codes(key_quality(mails, "email"))

    ids = pd.Series([f"{n:07d}" for n in range(1, 101)] + [str(n) for n in range(1, 6)])
    short = next(check for check in key_quality(ids, "customer_id") if check.code == "KEY_MIXED_LENGTHS")
    assert short.numbers == {"shorter": 5, "usual_length": 7, "filled": 105}

    sequential = pd.Series([str(n) for n in range(1, 200)])  # 1, 9, 11, 99, 111: real IDs, each once
    assert "KEY_PLACEHOLDER" not in codes(key_quality(sequential, "customer_id"))
    as_text = pd.Series([str(123_457_000_000 + 100_000 * n) for n in range(30)])  # read as text, zeros kept
    assert "KEY_DIGITS_ROUNDED" in codes(key_quality(as_text, "mobile"))

    repeated = key_quality(pd.Series(["a", "a", "b"]), "id", expect_unique=True)
    report = next(check for check in repeated if check.code == "KEY_REPORT")
    assert report.numbers["distinct"] == 2 and report.numbers["top"][0] == ("a", 2)
    assert "KEY_NOT_UNIQUE" in codes(repeated)


# ---------------------------------------------------------------------------
# G7: the leak that a blank gives away
# ---------------------------------------------------------------------------
def _churn_frame(rows: int = 1000) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    churned = (rng.random(rows) < 0.2).astype(int)
    return pd.DataFrame(
        {
            "customer_id": [f"C{n}" for n in range(rows)],
            "tenure": rng.integers(1, 60, rows),
            "churned": churned,
            "exit_note": [f"reason {n}" if flag else None for n, flag in enumerate(churned)],
            "support_text": [f"called about thing {n}" if flag else "" for n, flag in enumerate(churned)],
            "gift_note": [None if n % 3 else f"gift {n}" for n in range(rows)],
        }
    )


def test_a_column_filled_only_for_the_customers_who_left_is_found() -> None:
    frame = _churn_frame()
    params = v.CheckParams(target="churned", key_columns=("customer_id",), leakage_baseline=False)
    found = {check.column: check for check in v.check_leakage_suspected(frame, params).findings}
    assert found["exit_note"].details["reason"] == "blank_pattern"
    assert found["exit_note"].details["positive_rate_filled"] == 1.0
    assert found["exit_note"].details["positive_rate_blank"] == 0.0
    assert found["support_text"].details["reason"] == "blank_pattern"  # an empty string is blank too
    assert "gift_note" not in found  # blank at random: says nothing about the outcome
    assert "tenure" not in found


def test_the_name_pattern_has_the_new_words() -> None:
    for name in ("cancellation_reason", "refund_amount", "exit_date", "retention_offer", "risk_score"):
        assert re.search(v.LEAKAGE_PATTERN, name, re.IGNORECASE), name
    assert not re.search(v.LEAKAGE_PATTERN, "monthly_spend", re.IGNORECASE)


# ---------------------------------------------------------------------------
# G8: the positive label is stated, never the rarer value
# ---------------------------------------------------------------------------
def test_the_doctor_never_picks_the_rarer_value_as_the_outcome() -> None:
    frame = pd.DataFrame({"status": ["left"] * 300 + ["stayed"] * 700})
    guessing = v.CheckParams(target="status")
    assert v.resolve_positive_label(frame, guessing)[0] == "left"  # Guided setup's old behaviour
    stated = v.CheckParams(target="status", allow_rarer_label=False)
    assert v.resolve_positive_label(frame, stated) == (None, 0, 0)
    said = v.CheckParams(target="status", allow_rarer_label=False, positive_label="stayed")
    assert v.resolve_positive_label(frame, said) == ("stayed", 700, 300)
    words = pd.DataFrame({"status": ["churned"] * 700 + ["active"] * 300})
    assert v.resolve_positive_label(words, stated)[0] == "churned"  # a known word is a stated meaning


def test_an_outcome_that_is_mostly_yes_for_a_loss_asks_which_way_round() -> None:
    check = outcome_direction("churned", 700, 300)
    assert check is not None and check.level == "stop" and check.numbers["share"] == 0.7
    assert outcome_direction("churned", 200, 800) is None
    assert outcome_direction("purchased", 700, 300) is None


# ---------------------------------------------------------------------------
# G9: dates
# ---------------------------------------------------------------------------
def test_dates_written_both_ways_stop_and_are_never_read_month_first() -> None:
    both = pd.Series(["25/03/2024", "03/25/2024", "01/02/2024"] * 10)
    assert date_order(both) is None and date_order_conflict(both) and order_unsettled(both)
    assert _as_dates(both) is None  # the old default read this month first
    frame = pd.DataFrame({"joined": both})
    stop = date_order_checks(frame)
    assert [c.code for c in stop] == ["DATE_ORDER_BOTH"] and stop[0].level == "stop"
    issue = find_format_issues(frame)[0]
    assert issue.params["order_conflict"] is True and issue.params["dayfirst"] is None
    plain = pd.Series(["03/04/2024", "05/06/2024"] * 10)  # every value fits both: unsettled, but no conflict
    assert order_unsettled(plain) and not date_order_conflict(plain) and _as_dates(plain) is None
    assert not date_order_checks(pd.DataFrame({"joined": plain}))
    assert _as_dates(pd.Series(["25/03/2024", "26/03/2024"] * 10)) is not None  # proven day first: still read


def test_placeholder_open_future_and_out_of_order_dates_are_found() -> None:
    n = 100
    frame = pd.DataFrame(
        {
            "signup_date": ["1970-01-01"] * 10
            + [f"2025-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(n - 10)],
            "first_order_date": ["2025-01-01"] * n,
            "valid_to": ["9999-12-31"] * 40 + ["2025-12-01"] * 60,
            "login_date": ["2026-03-01"] * 5 + ["2025-06-01"] * (n - 5),
        }
    )
    found = {check.code + ":" + str(check.column): check for check in date_checks(frame, date(2025, 12, 31))}
    assert found["DATE_PLACEHOLDER:signup_date"].numbers["rows"] == 10
    assert found["DATE_STILL_OPEN:valid_to"].level == "note"
    assert found["DATE_AFTER_AS_OF:login_date"].numbers["rows"] == 5
    out_of_order = found["DATE_ORDER_VIOLATED:signup_date"]
    assert out_of_order.numbers["both_filled"] == n and out_of_order.numbers["rows"] > 0
    assert not any(key.endswith(":valid_to") and key.startswith("DATE_AFTER") for key in found)
    # Those digits inside text that is not a date say nothing about dates.
    not_dates = pd.DataFrame({"ref": ["AB-9999-X", "+91 9999 123456", "PO Box 2099"] * 10})
    assert not codes(date_checks(not_dates, date(2025, 12, 31)))


def test_excel_day_numbers_from_the_mac_calendar_are_flagged() -> None:
    days = pd.Series(
        [
            (pd.Timestamp("2024-01-01") + pd.Timedelta(days=i) - pd.Timestamp("1904-01-01")).days
            for i in range(60)
        ]
    )
    found = date_checks(pd.DataFrame({"order_date": days}), date(2024, 6, 30))
    assert "DATE_EXCEL_CALENDAR" in codes(found)
    right = pd.Series(
        [
            (pd.Timestamp("2024-01-01") + pd.Timedelta(days=i) - pd.Timestamp("1899-12-30")).days
            for i in range(60)
        ]
    )
    assert "DATE_EXCEL_CALENDAR" not in codes(
        date_checks(pd.DataFrame({"order_date": right}), date(2024, 6, 30))
    )


# ---------------------------------------------------------------------------
# G11: the newest rows
# ---------------------------------------------------------------------------
def _daily(start: str, end: str, per_day: int | dict[str, int]) -> pd.Series[Any]:
    days = pd.date_range(start, end)
    counts = [per_day if isinstance(per_day, int) else per_day.get(str(d.date()), 10) for d in days]
    return pd.Series(np.repeat(days.to_numpy(), counts))


def test_a_last_month_that_is_only_partly_there_proposes_the_month_before() -> None:
    found = tail_checks(_daily("2025-07-01", "2025-10-12", 3))
    partial = next(c for c in found if c.code == "PARTIAL_LAST_MONTH")
    assert partial.as_of == date(2025, 9, 30) and partial.numbers["days_covered"] == 12
    assert partial.numbers["rows_after"] == 36
    assert not tail_checks(_daily("2025-07-01", "2025-10-31", 3))


def test_final_days_far_below_the_usual_for_their_weekday_are_a_thin_tail() -> None:
    thin = {"2025-04-18": 2, "2025-04-19": 2, "2025-04-20": 2}
    found = tail_checks(
        _daily(
            "2025-01-01",
            "2025-04-30",
            {**thin, **{str(d.date()): 10 for d in pd.date_range("2025-04-21", "2025-04-30")}},
        )
    )
    assert "THIN_TAIL" not in codes(found)  # the thin days are not the last ones
    ended = tail_checks(_daily("2025-01-01", "2025-04-20", thin))
    tail = next(c for c in ended if c.code == "THIN_TAIL")
    assert (
        tail.as_of == date(2025, 4, 17) and tail.numbers["thin_days"] == 3 and tail.numbers["rows_after"] == 6
    )


def test_a_newest_month_with_far_fewer_yes_is_not_final_yet() -> None:
    days = _daily("2025-01-01", "2025-05-31", 4)
    months = days.dt.to_period("M")
    rows = np.arange(len(days))
    yes = pd.Series(np.where(months == pd.Period("2025-05"), rows % 50 == 0, rows % 5 == 0))
    found = tail_checks(days, yes)
    immature = next(c for c in found if c.code == "IMMATURE_PERIOD")
    assert immature.as_of == date(2025, 4, 30) and immature.numbers["newest_rate"] < 0.05
    assert "IMMATURE_PERIOD" not in codes(tail_checks(days, pd.Series(rows % 5 == 0)))


def _one_row_per_month(
    last_rate: float, covered: int | None = None
) -> tuple[pd.Series, pd.Series, pd.Series]:
    months = pd.Series(pd.to_datetime([f"2026-{m:02d}-01" for m in range(3, 10) for _ in range(40)]))
    rate = np.where(months == months.max(), last_rate, 0.25)
    yes = pd.Series(np.arange(len(months)) % 40 < rate * 40)  # the first rows of each month of 40 are "yes"
    days = pd.Series(np.where(months == months.max(), covered or 30, 30))
    return months, yes, days


def test_a_file_with_one_date_per_month_still_gets_the_newest_month_checked() -> None:
    months, yes, _ = _one_row_per_month(0.03)
    found = tail_checks(months, yes)
    assert codes(found) == {"IMMATURE_PERIOD"}  # not "partly in the file": every date is a 1st
    assert found[0].as_of == date(2026, 8, 31) and found[0].numbers["rows_after"] == 40
    assert tail_checks(months, _one_row_per_month(0.25)[1]) == []


def test_a_days_covered_column_says_the_newest_month_is_partial() -> None:
    months, _, days = _one_row_per_month(0.25, covered=12)
    partial = next(c for c in tail_checks(months, None, days) if c.code == "PARTIAL_LAST_MONTH")
    assert partial.numbers["days_covered"] == 12 and partial.as_of == date(2026, 8, 31)
    assert tail_checks(months, None, _one_row_per_month(0.25)[2]) == []


def test_the_date_that_says_how_far_the_data_reaches_is_not_a_birth_or_signup_date() -> None:
    from engine.doctor.detect_time_keys import _date_column

    frame = pd.DataFrame(
        {
            "date_of_birth": ["1990-01-01"] * 3,
            "signup_date": ["2024-01-01"] * 3,
            "last_order_date": ["2026-01-01"] * 3,
            "month_start": ["2026-01-01"] * 3,
        }
    )
    case = SimpleNamespace(items=(), questions=(), goals=())
    # A last-order date is one per customer: its newest rows are the most active customers, not unfinished data.
    assert _date_column(case, frame, "u") == "month_start"  # type: ignore[arg-type]
    assert _date_column(case, frame.drop(columns="month_start"), "u") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A summary, merged cells, another unit
# ---------------------------------------------------------------------------
def test_a_pivot_table_is_a_summary_and_a_customer_table_is_not() -> None:
    pivot = pd.DataFrame(
        {"region": ["N", "S", "Grand Total"], "customers": [5, 7, 12], "total_spend": [10, 20, 30]}
    )
    counts_only = pd.DataFrame(
        {"region": ["N", "N", "S", "S"], "plan": list("abab"), "customers": [1, 2, 3, 4]}
    )
    people = pd.DataFrame(
        {"customer_id": ["a", "b", "c"], "region": ["N", "N", "S"], "total_spend": [1, 2, 3]}
    )
    assert summary_check(pivot) is not None and summary_check(pivot).level == "stop"  # type: ignore[union-attr]
    assert summary_check(counts_only) is not None
    visits = pd.DataFrame({"customer_id": ["a", "a", "b"], "day": ["d1", "d2", "d1"], "count": [3, 1, 2]})
    assert summary_check(people) is None
    assert summary_check(visits) is None  # counts per customer and day: an ID column, so rows, not a summary


def test_a_value_written_once_per_block_is_found_but_scattered_blanks_are_not() -> None:
    block = pd.DataFrame({"region": (["North"] + [""] * 14 + ["East"] + [""] * 14), "n": range(30)})
    scattered = pd.DataFrame({"reason": ["moved", "", "", "price", "", "moved", "", "", "", "price"] * 3})
    leading = pd.DataFrame({"last_order": [""] * 12 + ["2026-01-01"] * 8})
    # Sparse, but a reason repeats from one filled row to the next: not a merged cell; nor is an end date.
    sparse = pd.DataFrame(
        {"reason": [v for r in ["price", "price", "moved", "moved"] for v in [r] + [""] * 9]}
    )
    ended = pd.DataFrame({"end_date": [v for d in range(1, 4) for v in [f"2026-01-0{d}"] + [""] * 9]})
    assert [c.column for c in block_blank_checks(block)] == ["region"]
    assert block_blank_checks(scattered) == [] and block_blank_checks(leading) == []
    assert block_blank_checks(sparse) == [] and block_blank_checks(ended) == []


def test_the_same_amount_column_a_hundred_times_larger_in_one_file_is_a_unit_question() -> None:
    rupees = pd.DataFrame({"order_id": range(50), "amount": np.arange(50) * 10 + 500})
    paise = pd.DataFrame({"order_id": range(50, 100), "amount": (np.arange(50) * 10 + 500) * 100})
    found = unit_scale_checks([("a.csv", rupees), ("b.csv", paise)])
    assert [(c.column, c.numbers["larger"]) for c in found] == [
        ("amount", "b.csv")
    ]  # the ID column is not compared
    assert unit_scale_checks([("a.csv", rupees), ("c.csv", rupees.copy())]) == []


# ---------------------------------------------------------------------------
# G12: rows that repeat, files that overlap
# ---------------------------------------------------------------------------
def test_repeated_headers_copies_and_shared_rows_are_reported_not_dropped() -> None:
    january = pd.DataFrame(
        {"order_id": [f"O{n}" for n in range(50)], "customer_id": range(50), "amount": range(50)}
    )
    pasted = pd.concat(
        [
            january,
            pd.DataFrame([{"order_id": "order_id", "customer_id": "customer_id", "amount": "amount"}]),
            january.head(5),
        ]
    )
    february = january.tail(20).copy()
    found = file_checks([("jan.csv", pasted), ("feb.csv", february)])
    by_code = {check.code: check for check in found}
    assert by_code["REPEATED_HEADER_ROWS"].numbers["rows"] == 1
    assert by_code["DUPLICATE_ROWS"].numbers["rows"] == 5
    assert by_code["FILES_SHARE_ROWS"].numbers["rows"] == 20
    assert len(pasted) == 56  # nothing was dropped
    alone = {check.code for check in file_checks([("jan.csv", pasted)])}
    assert {"REPEATED_HEADER_ROWS", "DUPLICATE_ROWS"} <= alone
    assert not file_checks(
        [("a.csv", january), ("b.csv", january.assign(order_id=lambda f: "X" + f.order_id))]
    )


# ---------------------------------------------------------------------------
# G13 / G19: a random sample of the whole file, IDs as text
# ---------------------------------------------------------------------------
def test_the_sample_comes_from_the_whole_file_with_ids_as_text_and_exact_counts(tmp_path: Any) -> None:
    storage = LocalStorage(tmp_path)
    rows = 5000
    body = "\n".join(f"{n:07d},{'churned' if n >= 4000 else 'active'},{n}" for n in range(rows))
    storage.write_bytes("u/f.csv", f"customer_id,status,n\n{body}\n".encode())
    one = read_sample(storage, "u/f.csv", rows=500, seed=7, exact_columns=("status", "customer_id"))
    two = read_sample(storage, "u/f.csv", rows=500, seed=7, exact_columns=("status", "customer_id"))
    pd.testing.assert_frame_equal(one.frame, two.frame)  # the same file and seed: the same rows
    assert one.row_count == rows and one.sampled and len(one.frame) == 500
    assert one.frame["customer_id"].iloc[0].startswith("0")  # text, zeros kept
    assert one.frame["n"].is_monotonic_increasing  # in file order
    assert one.frame["n"].max() > 4000 and one.frame["n"].min() < 1000  # both ends of the file
    assert one.exact["status"].value_counts == {"active": 4000, "churned": 1000}
    assert one.exact["customer_id"].distinct_count == rows
    assert not read_sample(storage, "u/f.csv", rows=9000, seed=7).sampled


def test_a_sample_read_again_in_another_encoding_counts_each_row_once(tmp_path: Any) -> None:
    storage = LocalStorage(tmp_path)
    rows = 150_000  # past the first chunk, so the UTF-8 pass fails part way and the next encoding is tried
    lines = [f"{n:07d},{'caf' + chr(233) if n == 140_000 else 'x'}" for n in range(rows)]
    storage.write_bytes("u/f.csv", ("customer_id,name\n" + "\n".join(lines) + "\n").encode("latin-1"))
    sample = read_sample(storage, "u/f.csv", rows=500, seed=1, exact_columns=("customer_id",))
    assert sample.row_count == rows and sample.exact["customer_id"].row_count == rows
    assert not sample.frame["customer_id"].duplicated().any()


# ---------------------------------------------------------------------------
# The detector, through a visit
# ---------------------------------------------------------------------------
def _messy_customers() -> pd.DataFrame:
    rows = 400
    rng = np.random.default_rng(2)
    return pd.DataFrame(
        {
            "customer_id": [f"C{n:05d}" for n in range(rows)],
            "mobile": [f"{n}.98765E+11" for n in range(rows)],
            "joined": ["25/03/2024" if n % 2 else "03/25/2024" for n in range(rows)],
            "signup_date": ["1970-01-01"] * 40 + ["2025-03-01"] * (rows - 40),
            "churned": rng.choice(["yes", "no"], rows, p=[0.8, 0.2]),
        }
    )


def test_the_detector_stops_on_rounded_ids_and_mixed_dates_and_flags_the_rest(tmp_path: Any) -> None:
    config = load_use_case(vs.GOAL_A)
    w = vs.World(
        files={vs.FIRST: vs.loaded(vs.FIRST, "customers.csv", _messy_customers(), config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    storage = LocalStorage(tmp_path)
    case = vs.open_case(storage, w)
    found = time_keys_detector()(case, tuple(w.files.values()), None)
    assert {a.code for a in found.asks} == {"DATE_ORDER_BOTH", "DATE_PLACEHOLDER"}
    assert {a.code for a in found.asks if a.blocking} == {"DATE_ORDER_BOTH"}
    # The stand-in date is a question with a ready answer: treat 1970-01-01 as blank (a set_missing_text step).
    stand_in = next(a for a in found.asks if a.code == "DATE_PLACEHOLDER")
    step: Any = stand_in.options[0].effect.payload["step"]
    assert stand_in.recommended == "blank" and step["kind"] == "set_missing_text"
    assert [r.kind for r in found.requests] == ["different_file"] and "mobile" in found.requests[0].what
    flags = {i.payload["code"] for i in found.items}
    assert "KEY_DIGITS_ROUNDED" in flags and "DATE_PLACEHOLDER" not in flags
    assert all(i.kind == CHECK_KIND and i.state.value == "pending" for i in found.items)
    # The outcome is mostly "yes" for a churn column, but it is only asked once the outcome column is chosen.
    assert "OUTCOME_WRONG_WAY_ROUND" not in {a.code for a in found.asks}
    deps = vs.deps_for(storage, w, detectors=(time_keys_detector(),))
    after = visit.resume(vs.CLIENT, case.case_id, deps, actor=vs.ANALYST)
    assert any(q.code == "DATE_ORDER_BOTH" for q in after.questions)
    assert any(i.kind == CHECK_KIND for i in after.items)


def test_every_stop_is_asked_whatever_the_question_budget(tmp_path: Any) -> None:
    config = load_use_case(vs.GOAL_A)
    rows = 100
    frame = pd.DataFrame(
        {
            f"date_{k}": ["25/03/2024" if n % 2 else "03/25/2024" for n in range(rows)]
            for k in range(MAX_QUESTIONS + 2)
        }
    )
    w = vs.World(
        files={vs.FIRST: vs.loaded(vs.FIRST, "dates.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = vs.open_case(LocalStorage(tmp_path), w)
    found = time_keys_detector()(case, tuple(w.files.values()), None)
    assert len(found.asks) == MAX_QUESTIONS + 2 and all(a.blocking for a in found.asks)


def _dated(last_day: str) -> pd.DataFrame:
    days = pd.date_range("2026-04-01", last_day, freq="D")
    return pd.DataFrame(
        {"customer_id": [f"C{n:05d}" for n in range(len(days))], "order_date": days.strftime("%Y-%m-%d")}
    )


def test_a_finding_the_question_covers_is_not_listed_again(tmp_path: Any) -> None:
    config = load_use_case(vs.GOAL_A)
    w = vs.World(
        files={vs.FIRST: vs.loaded(vs.FIRST, "orders.csv", _dated("2026-07-08"), config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = vs.open_case(LocalStorage(tmp_path), w)
    found = time_keys_detector()(case, tuple(w.files.values()), None)
    assert "THIN_LAST_PERIOD" in {a.code for a in found.asks}
    listed = {i.payload["code"] for i in found.items}
    assert not listed & {"PARTIAL_LAST_MONTH", "THIN_TAIL", "IMMATURE_PERIOD"}


def test_the_same_sentence_for_two_files_is_listed_once(tmp_path: Any) -> None:
    config = load_use_case(vs.GOAL_A)
    frame = _messy_customers()
    w = vs.World(
        files={
            vs.FIRST: vs.loaded(vs.FIRST, "a.csv", frame, config),
            vs.SECOND: vs.loaded(vs.SECOND, "b.csv", frame, config),
        },
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = vs.open_case(LocalStorage(tmp_path), w)
    found = time_keys_detector()(case, tuple(w.files.values()), None)
    assert [i.payload["code"] for i in found.items].count("KEY_DIGITS_ROUNDED") == 1


def test_a_sentence_a_source_file_already_carries_is_not_listed_again_for_another_upload(
    tmp_path: Any,
) -> None:
    config = load_use_case(vs.GOAL_A)
    frame = _messy_customers()
    w = vs.World(
        files={vs.FIRST: vs.loaded(vs.FIRST, "a.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = vs.open_case(LocalStorage(tmp_path), w)
    first = time_keys_detector()(case, tuple(w.files.values()), None)
    sentence = next(i for i in first.items if i.payload["code"] == "KEY_DIGITS_ROUNDED")
    other = vs.loaded(vs.SECOND, "combined.csv", frame, config)
    case = case.model_copy(update={"items": (*case.items, sentence)})
    again = time_keys_detector()(case, (other,), None)
    assert "KEY_DIGITS_ROUNDED" not in [i.payload["code"] for i in again.items]
    same = time_keys_detector()(case, tuple(w.files.values()), None)
    assert "KEY_DIGITS_ROUNDED" in [
        i.payload["code"] for i in same.items
    ], "the same file's own item is refreshed"


def test_a_check_of_a_table_that_did_not_change_is_not_read_again() -> None:
    """A visit asks for the same checks at every step: a table with the same cells gets the answer found before, and a
    changed cell is read again (the answer follows the cells, never the object)."""
    from engine.doctor import checks_time_keys as keys

    block = pd.DataFrame({"region": (["North"] + [""] * 14 + ["East"] + [""] * 14), "n": range(30)})
    keys._FRAME_CHECKS.clear()
    first = block_blank_checks(block)
    assert [c.column for c in first] == ["region"] and keys._FRAME_CHECKS.misses == 1
    again = block_blank_checks(block.copy())
    assert again == first and keys._FRAME_CHECKS.hits == 1 and keys._FRAME_CHECKS.misses == 1
    filled = block.copy()
    filled.loc[1, "region"] = "West"
    block_blank_checks(filled)
    assert keys._FRAME_CHECKS.misses == 2, "a changed cell is a new table: it is read"
    asked = key_quality(pd.Series(["a", "b", "b"], name="id"), "id", expect_unique=True)
    assert key_quality(pd.Series(["a", "b", "b"], name="id"), "id", expect_unique=True) == asked
    assert keys._FRAME_CHECKS.hits == 2
