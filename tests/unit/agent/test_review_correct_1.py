"""Correctness review of the look tools (round 1): each test is a failing repro of a real defect.

Every test names the tool, the input and what a person (or the model reading the result) would expect.
None of them is a style opinion: the numbers are wrong, the result breaks its own size contract, a
personal or hidden column can be read through a side channel, or the tool takes minutes.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import string
import time
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.egress import Egress
from engine.agent.tools import AgentToolError, call_tool
from tests.unit.agent.helpers import context_for

MAX_RESULT_CHARS = 6_000


def _run(ctx: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return call_tool(ctx, name, args, evidence_id="e1").result


def _size(result: dict[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False))


def _hidden_ctx(frame: pd.DataFrame, hide: tuple[str, ...]) -> Any:
    ctx = context_for(frame)
    agent = ctx.config.agent.model_copy(update={"always_hide_columns": hide})
    return dataclasses.replace(ctx, config=ctx.config.model_copy(update={"agent": agent}))


# ---------------------------------------------------------------------------
# CR1-1  describe_numbers: a small number is shown as 0.0 (values rounded to 6 decimals, absolutely)
# ---------------------------------------------------------------------------
def test_describe_numbers_does_not_show_small_values_as_zero() -> None:
    """Rates per impression are ~1e-7. `minimum`, `mean` and the histogram all read 0.0, while `share_zero` says 0."""
    frame = pd.DataFrame({"rate": [1e-7, 2e-7, 3e-7, 4e-7] * 5, "id": range(20)})
    out = _run(context_for(frame), "describe_numbers", {"column": "rate"})
    assert out["share_zero"] == 0.0
    assert out["minimum"] > 0, f"minimum shown as {out['minimum']}"
    assert out["mean"] > 0, f"mean shown as {out['mean']}"
    assert out["maximum"] > out["minimum"]
    assert out["quantiles"]["p50"] > 0
    assert all(bucket["to"] > 0 for bucket in out["histogram"]), out["histogram"]


# ---------------------------------------------------------------------------
# CR1-2  describe_numbers: infinite values vanish from the counts (numbers + empty + unparseable != rows)
# ---------------------------------------------------------------------------
def test_describe_numbers_accounts_for_every_row_when_a_column_holds_infinity() -> None:
    frame = pd.DataFrame({"ratio": [np.inf, -np.inf, 1.0, 2.0, np.nan] * 10, "id": range(50)})
    out = _run(context_for(frame), "describe_numbers", {"column": "ratio"})
    accounted = out["numbers"] + out["empty"] + out["unparseable"] + out.get("infinite", 0)
    assert accounted == out["rows"], (
        f"{out['rows']} rows but numbers={out['numbers']} empty={out['empty']} "
        f"unparseable={out['unparseable']}: 20 infinite values are in no count at all"
    )


# ---------------------------------------------------------------------------
# CR1-3  describe_numbers: the mean of huge integers loses the small ones (float sum, no fsum)
# ---------------------------------------------------------------------------
def test_describe_numbers_mean_is_exact_for_huge_and_small_values() -> None:
    frame = pd.DataFrame({"a": [2**62, -(2**62), 5, 7, 9] * 10, "id": range(50)})
    out = _run(context_for(frame), "describe_numbers", {"column": "a"})
    assert out["mean"] == pytest.approx(4.2, abs=1e-6), f"true mean is 4.2, tool says {out['mean']}"


# ---------------------------------------------------------------------------
# CR1-4  describe_duplicates: 0.0 and -0.0 are different keys
# ---------------------------------------------------------------------------
def test_describe_duplicates_treats_negative_zero_as_zero() -> None:
    frame = pd.DataFrame({"a": [0.0, -0.0, 1.0, 1.0], "b": [7, 7, 8, 9]})
    ctx = context_for(frame)
    by_column = _run(ctx, "describe_duplicates", {"columns": ["a"]})
    assert by_column["duplicate_rows"] == int(frame["a"].duplicated().sum()) == 2
    whole = _run(ctx, "describe_duplicates", {"columns": ["a", "b"]})
    assert whole["duplicate_rows"] == int(frame[["a", "b"]].duplicated().sum()) == 1


# ---------------------------------------------------------------------------
# CR1-5  value_counts: "" is an empty cell in an object column but a value in a string or categorical one
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dtype", ["object", "string", "category"])
def test_value_counts_treats_an_empty_string_the_same_in_every_text_dtype(dtype: str) -> None:
    frame = pd.DataFrame({"c": pd.Series(["a", "", "a", None, "b", ""]).astype(dtype), "id": range(6)})
    out = _run(context_for(frame), "value_counts", {"column": "c"})
    assert out["empty"] == 3
    assert out["distinct"] == 2, f"{dtype}: distinct={out['distinct']} but 3 rows are empty"
    assert [v["value"] for v in out["values"]] == ["a", "b"] or sorted(v["value"] for v in out["values"]) == [
        "a",
        "b",
    ]
    assert sum(v["rows"] for v in out["values"]) + out["empty"] == out["rows"]


# ---------------------------------------------------------------------------
# CR1-6  sample_rows: `equals` on a boolean column depends on whether the column has an empty cell
# ---------------------------------------------------------------------------
def test_sample_rows_equals_on_a_boolean_column_does_not_depend_on_missing_values() -> None:
    complete = pd.DataFrame({"flag": [True, False, True, False], "i": range(4)})
    with_gap = pd.DataFrame({"flag": [True, False, True, None], "i": range(4)})  # read_csv gives object here
    args = {"columns": ["i"], "where_column": "flag", "equals": "true"}
    got_complete = _run(context_for(complete), "sample_rows", args)["total_matching"]
    got_gap = _run(context_for(with_gap), "sample_rows", args)["total_matching"]
    assert got_complete == 2
    assert (
        got_gap == 2
    ), f"'true' matches {got_gap} rows when the column has an empty cell, {got_complete} when it has none"


# ---------------------------------------------------------------------------
# CR1-7  compare_columns: a constant column is reported as a linear function of any other column
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("constant", [5.0, 0.0])
def test_compare_columns_does_not_call_a_constant_column_derived_from_another(constant: float) -> None:
    x = np.random.default_rng(2).normal(size=100)
    frame = pd.DataFrame({"a": x, "b": np.full(100, constant)})
    out = _run(context_for(frame), "compare_columns", {"left": "a", "right": "b"})
    assert out["kind"] not in {"linear", "constant_multiple", "offset"}, (
        f"kind={out['kind']} slope={out.get('slope')}: a constant is not derived from a varying column "
        "(and swapping the columns says 'unrelated')"
    )


# ---------------------------------------------------------------------------
# CR1-8  describe_dates: "09:30" is read as a date - today's - so the answer changes with the day it is asked
# ---------------------------------------------------------------------------
def test_describe_dates_does_not_read_times_of_day_as_todays_date() -> None:
    frame = pd.DataFrame({"t": [f"{i % 24:02d}:{i % 60:02d}" for i in range(100)], "i": range(100)})
    out = _run(context_for(frame), "describe_dates", {"column": "t"})
    today = datetime.date.today().isoformat()
    assert not (
        out.get("dates_found") and out.get("earliest") == today
    ), f"a column of times of day is reported as {out.get('dates')} dates, earliest = latest = {out.get('earliest')} (today)"


# ---------------------------------------------------------------------------
# CR1-9  result size: the 6,000-character contract breaks on quotes, long names and control characters
# ---------------------------------------------------------------------------
def test_value_counts_stays_under_the_size_budget_for_cells_full_of_quotes() -> None:
    cells = ['"' * 58 + f"{i:02d}" for i in range(60)]
    frame = pd.DataFrame({"a": [cells[i % 60] for i in range(400)], "id": range(400)})
    out = _run(context_for(frame), "value_counts", {"column": "a", "top": 50})
    assert _size(out) <= MAX_RESULT_CHARS, _size(out)


def test_value_counts_stays_under_the_size_budget_for_cells_with_control_characters() -> None:
    cells = ["\x01" * 55 + f"{i:05d}" for i in range(60)]
    frame = pd.DataFrame({"a": [cells[i % 60] for i in range(400)], "id": range(400)})
    out = _run(context_for(frame), "value_counts", {"column": "a", "top": 50})
    assert _size(out) <= MAX_RESULT_CHARS, _size(out)


def test_describe_missing_stays_under_the_size_budget_with_ordinary_60_character_column_names() -> None:
    rng = np.random.default_rng(1)
    n, cols = 600, {}
    for g in range(10):
        mask = rng.random(n) < 0.3
        for j in range(14):
            values = rng.random(n)
            values[mask] = np.nan
            cols[f"survey_question_{g:02d}_{j:02d}_answer_".ljust(60, "x")] = values
    for k in range(40):
        cols[f"never_filled_field_{k:02d}_".ljust(60, "y")] = [np.nan] * n
    out = _run(context_for(pd.DataFrame(cols)), "describe_missing")
    assert _size(out) <= MAX_RESULT_CHARS, _size(out)


@pytest.mark.parametrize("tool", ["sample_rows", "describe_duplicates"])
def test_results_stay_bounded_when_column_names_are_very_long(tool: str) -> None:
    frame = pd.DataFrame({f"c{i}_".ljust(1_000, "z"): range(5) for i in range(12)})
    args = {"columns": list(frame.columns), "n": 5} if tool == "sample_rows" else None
    out = _run(context_for(frame), tool, args)
    assert _size(out) <= MAX_RESULT_CHARS, f"{tool}: {_size(out)} characters"


# ---------------------------------------------------------------------------
# CR1-10  a 30,000-character cell makes a look take seconds (a 100,000-character one, most of a minute)
# ---------------------------------------------------------------------------
def test_sample_rows_is_fast_on_one_long_cell() -> None:
    """`_masked` scans the whole cell before cutting it, and the e-mail scanner is quadratic on a long word."""
    frame = pd.DataFrame({"body": ["x" * 30_000, "ok", "fine"], "i": range(3)})
    ctx = context_for(frame)
    started = time.perf_counter()
    _run(ctx, "sample_rows", {"columns": ["body"], "n": 3})
    took = time.perf_counter() - started
    assert took < 1.0, f"sample_rows took {took:.1f}s for one 30,000-character cell"


# ---------------------------------------------------------------------------
# CR1-11  find_values / sample_rows still answer questions about a personal or hidden column
# ---------------------------------------------------------------------------
def _recover_by_counting(ctx: Any, column: str, suffix: str = "@example.test") -> set[str]:
    """Rebuild every value of `column` that ends in `suffix` from find_values' match counts alone."""
    alphabet = string.ascii_lowercase + "._"

    def count(text: str) -> int:
        try:
            out = _run(ctx, "find_values", {"column": column, "contains": text})
        except AgentToolError:
            return 0
        return int(out.get("total_matching_rows") or 0)

    found: set[str] = set()
    stack = [suffix] if count(suffix) else []
    while stack:
        current = stack.pop()
        extended = [c + current for c in alphabet if count(c + current)]
        if extended:
            stack.extend(extended)
        else:
            found.add(current)
    return found


def test_find_values_does_not_let_the_model_spell_out_a_personal_column() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(6)],
            "email": ["zoe.kapoor@example.test", "raj.mehta@example.test", "ann.lee@example.test"] * 2,
            "plan": ["a", "b"] * 3,
        }
    )
    ctx = context_for(frame)
    assert any(c.pii_kinds for c in ctx.profile.columns if c.name == "email")
    recovered = _recover_by_counting(ctx, "email")
    assert not recovered, f"rebuilt {sorted(recovered)} from match counts of a personal-data column"


def test_find_values_does_not_let_the_model_spell_out_an_always_hidden_column() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(6)],
            "notes": ["zoe_kapoor.acme", "raj_mehta.acme", "ann_lee.acme"] * 2,
            "plan": ["a", "b"] * 3,
        }
    )
    ctx = _hidden_ctx(frame, ("notes",))
    assert not any(c.pii_kinds for c in ctx.profile.columns if c.name == "notes")  # only the setting hides it
    recovered = _recover_by_counting(ctx, "notes", suffix=".acme")
    assert (
        not recovered
    ), f"rebuilt {sorted(recovered)} from match counts of a column listed in always_hide_columns"


def test_sample_rows_refuses_to_pick_rows_by_a_hidden_column() -> None:
    frame = pd.DataFrame(
        {"customer_id": [f"C{i}" for i in range(4)], "notes": ["a", "b", "c", "d"], "plan": list("wxyz")}
    )
    ctx = _hidden_ctx(frame, ("notes",))
    # The property is "a hidden column cannot be probed": the answer is the same whatever `equals` is and
    # shows no row (fix-leaks answers with an empty result; a refusal would also be safe). The reviewer's
    # first form demanded the refusal, while test_review_bypass_12 demands a result independent of `equals`
    # - one answer satisfies both, so this test now checks the property rather than the exception.
    hit = _run(ctx, "sample_rows", {"columns": ["plan"], "where_column": "notes", "equals": "c"})
    miss = _run(ctx, "sample_rows", {"columns": ["plan"], "where_column": "notes", "equals": "no such"})
    assert hit == miss and hit["rows"] == [] and hit["total_matching"] is None


# ---------------------------------------------------------------------------
# CR1-12  the egress gate blanks a whole result when one column in it is personal or hidden
# ---------------------------------------------------------------------------
def _gate(ctx: Any, hide: tuple[str, ...] = ()) -> Egress:
    return Egress.build(
        [str(c) for c in ctx.frame.columns],
        personal_columns=[c.name for c in ctx.profile.columns if c.pii_kinds],
        always_hide=hide,
    )


def _with_email() -> Any:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(30)],
            "email": [f"user{i % 10}@example.com" for i in range(30)],
            "plan": ["basic", "pro"] * 15,
            "amount": range(30),
        }
    )
    return context_for(frame)


def test_default_sample_rows_still_shows_the_other_columns_when_one_column_is_personal() -> None:
    """The default is the first 8 columns: an e-mail column among them turned every cell into '[personal data]'."""
    ctx = _with_email()
    raw = _run(ctx, "sample_rows", {"n": 2})
    assert raw["rows"][0][2] == "basic"  # the tool itself hides only the e-mail cells
    shown = _gate(ctx).prepare(raw)
    assert shown["rows"][0][2] == "basic", shown["rows"][0]
    assert (
        shown["returned"] == 2 and shown["next_offset"] == 2
    ), "the model cannot page: returned/next_offset are null"


def test_describe_duplicates_on_a_personal_key_keeps_its_counts() -> None:
    """'Are there duplicate customers by e-mail?' is the first duplicates question; repeated_keys came back null."""
    ctx = _with_email()
    raw = _run(ctx, "describe_duplicates", {"columns": ["email"]})
    shown = _gate(ctx).prepare(raw)
    for key in ("share", "repeated_keys", "rows_in_repeated_keys", "largest_group"):
        assert (
            shown[key] == raw[key]
        ), f"{key}: the tool measured {raw[key]!r}, the model is shown {shown[key]!r}"


def test_value_counts_on_a_personal_column_keeps_its_repeat_counts() -> None:
    ctx = _with_email()
    raw = _run(ctx, "value_counts", {"column": "email"})
    shown = _gate(ctx).prepare(raw)
    assert shown["most_rows_for_one_value"] == raw["most_rows_for_one_value"] == 3
    assert shown["values_seen_once"] == raw["values_seen_once"]


# ---------------------------------------------------------------------------
# CR1-13  the look tools cannot ask about whitespace: arguments are stripped before they are used
# ---------------------------------------------------------------------------
def test_find_values_and_sample_rows_can_ask_about_leading_and_trailing_spaces() -> None:
    """Untrimmed text ('pro ' next to 'pro') is the commonest mess; `str_strip_whitespace` makes it unaskable."""
    frame = pd.DataFrame({"plan": ["pro ", "pro", "basic", " pro"], "i": range(4)})
    ctx = context_for(frame)
    found = _run(ctx, "find_values", {"column": "plan", "contains": " "})
    assert found["total_matching_rows"] == 2, found
    exact = _run(ctx, "sample_rows", {"columns": ["i"], "where_column": "plan", "equals": "pro "})
    assert exact["total_matching"] == 1, exact
