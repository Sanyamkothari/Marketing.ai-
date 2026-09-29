"""The helper's look-at-the-data tools (chat agent): read-only, bounded, and never a personal value.

Each tool is run on a small hand-made frame whose answers are worked out here by hand, then on the
things that must never happen: a planted email, phone number, name or PAN reaching a result, a
result too large for a prompt, a different answer on a second call, or a changed frame.
"""

from __future__ import annotations

import json
import time
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.grounding import evidence_numbers, grounded_numbers, ungrounded_numbers
from engine.agent.tools import TOOLS, AgentToolError, ToolKind, call_tool
from engine.generative.guardrails import Guardrails, load_policy
from engine.llm import FakeLLMClient, FakeLLMMode
from tests.unit.agent.helpers import context_for

LOOK_TOOLS = (
    "sample_rows",
    "value_counts",
    "find_values",
    "describe_numbers",
    "describe_dates",
    "compare_columns",
    "describe_missing",
    "describe_duplicates",
)
MAX_RESULT_CHARS = 6_000


def _run(ctx: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result = call_tool(ctx, name, args, evidence_id="e1")
    assert result.tool == name
    return result.result


def _refused(ctx: Any, name: str, args: dict[str, Any]) -> str:
    with pytest.raises(AgentToolError) as caught:
        call_tool(ctx, name, args, evidence_id="e1")
    return caught.value.code


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(1, 11)],
            "plan": ["basic", "basic", "pro", "pro", "basic", None, "free", "basic", "pro", "basic"],
            "amount": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 1000.0],
        }
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
def test_the_eight_look_tools_are_registered_read_only_tools_with_descriptions() -> None:
    for name in LOOK_TOOLS:
        tool = TOOLS[name]
        assert tool.kind is ToolKind.READ
        assert len(tool.description) > 40, name


def test_a_call_records_a_tool_result_with_its_evidence_id() -> None:
    ctx = context_for(_frame())
    result = call_tool(ctx, "value_counts", {"column": "plan"}, evidence_id="e9")
    assert result.evidence_id == "e9" and result.tool == "value_counts"
    assert result.args == {"column": "plan", "top": 20}
    assert result.model_dump_json()


# ---------------------------------------------------------------------------
# sample_rows
# ---------------------------------------------------------------------------
def test_sample_rows_returns_rows_aligned_to_the_columns_from_the_offset() -> None:
    ctx = context_for(_frame())
    out = _run(ctx, "sample_rows", {"columns": ["customer_id", "plan"], "n": 3, "offset": 2})
    assert out["columns"] == ["customer_id", "plan"]
    assert out["rows"] == [["C3", "pro"], ["C4", "pro"], ["C5", "basic"]]
    assert (out["offset"], out["returned"], out["total_matching"], out["truncated"]) == (2, 3, 10, True)


def test_sample_rows_defaults_to_the_first_columns_and_shows_an_empty_cell_as_null() -> None:
    ctx = context_for(_frame())
    out = _run(ctx, "sample_rows", {"n": 1, "offset": 5})
    assert out["columns"] == ["customer_id", "plan", "amount"]
    assert out["rows"] == [["C6", None, "60.0"]]
    wide = pd.DataFrame({f"c{i}": [i] for i in range(12)})
    assert _run(context_for(wide), "sample_rows")["columns"] == [f"c{i}" for i in range(8)]


def test_sample_rows_filters_on_an_exact_value() -> None:
    ctx = context_for(_frame())
    args = {"columns": ["customer_id"], "where_column": "plan", "equals": "pro"}
    out = _run(ctx, "sample_rows", args)
    assert out["rows"] == [["C3"], ["C4"], ["C9"]]
    assert (out["total_matching"], out["truncated"]) == (3, False)
    short = _run(ctx, "sample_rows", {**args, "n": 2})
    assert (short["returned"], short["total_matching"], short["truncated"]) == (2, 3, True)
    assert _run(ctx, "sample_rows", {**args, "equals": "PRO"})["total_matching"] == 0  # exact, not fuzzy
    numeric = _run(ctx, "sample_rows", {"columns": ["customer_id"], "where_column": "amount", "equals": "30"})
    assert numeric["rows"] == [["C3"]]


def test_sample_rows_past_the_end_is_empty_not_an_error() -> None:
    out = _run(context_for(_frame()), "sample_rows", {"offset": 50})
    assert out["rows"] == [] and out["truncated"] is False and out["total_matching"] == 10


def test_sample_rows_bounds_and_errors() -> None:
    ctx = context_for(_frame())
    assert _refused(ctx, "sample_rows", {"n": 0}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "sample_rows", {"n": 21}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "sample_rows", {"offset": -1}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "sample_rows", {"columns": []}) == "AGENT_TOOL_ARGS_INVALID"
    assert (
        _refused(ctx, "sample_rows", {"columns": [f"c{i}" for i in range(13)]}) == "AGENT_TOOL_ARGS_INVALID"
    )
    assert _refused(ctx, "sample_rows", {"equals": "pro"}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "sample_rows", {"where_column": "plan"}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "sample_rows", {"seed": 3}) == "AGENT_TOOL_ARGS_INVALID"  # deterministic, no seed
    assert _refused(ctx, "sample_rows", {"columns": ["nope"]}) == "AGENT_COLUMN_UNKNOWN"
    assert _refused(ctx, "sample_rows", {"where_column": "nope", "equals": "x"}) == "AGENT_COLUMN_UNKNOWN"


def test_sample_rows_accepts_the_shown_name_of_a_long_column() -> None:
    long_name = "x" * 90 + "_spend"
    frame = pd.DataFrame({long_name: [1, 2, 3]})
    from engine.agent.untrusted import display_name

    out = _run(context_for(frame), "sample_rows", {"columns": [display_name(long_name)]})
    assert out["columns"] == [display_name(long_name)] and out["rows"] == [["1"], ["2"], ["3"]]  # cut to 64


def test_sample_rows_cuts_cells_and_keeps_the_answer_small_but_says_more_exist() -> None:
    frame = pd.DataFrame({f"c{i}": ["lorem ipsum " * 20 + str(r) for r in range(100)] for i in range(12)})
    out = _run(context_for(frame), "sample_rows", {"columns": list(frame.columns), "n": 20})
    assert all(len(cell) <= 60 for row in out["rows"] for cell in row)
    assert len(json.dumps(out)) < MAX_RESULT_CHARS
    assert out["returned"] == len(out["rows"]) < 20
    assert out["truncated"] is True and out["total_matching"] == 100


def test_sample_rows_shows_a_personal_column_as_the_literal_marker() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(40)],
            "email": [f"person{i}.name@example.com" for i in range(40)],
        }
    )
    out = _run(context_for(frame), "sample_rows", {"columns": ["customer_id", "email"], "n": 2})
    assert out["rows"] == [["C0", "[personal data]"], ["C1", "[personal data]"]]
    assert out["withheld"] == [{"column": "email", "kind": "personal_data"}]


# ---------------------------------------------------------------------------
# value_counts
# ---------------------------------------------------------------------------
def test_value_counts_lists_values_rows_and_share() -> None:
    out = _run(context_for(_frame()), "value_counts", {"column": "plan"})
    assert out["column"] == "plan"
    assert (out["rows"], out["distinct"], out["empty"]) == (10, 3, 1)
    assert out["values"] == [
        {"value": "basic", "rows": 5, "share": 0.5},
        {"value": "pro", "rows": 3, "share": 0.3},
        {"value": "free", "rows": 1, "share": 0.1},
    ]
    assert out["other_rows"] == 0
    top_one = _run(context_for(_frame()), "value_counts", {"column": "plan", "top": 1})
    assert len(top_one["values"]) == 1 and top_one["other_rows"] == 4


def test_value_counts_bounds_and_errors() -> None:
    ctx = context_for(_frame())
    assert _refused(ctx, "value_counts", {"column": "plan", "top": 0}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "value_counts", {"column": "plan", "top": 51}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "value_counts", {}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "value_counts", {"column": "nope"}) == "AGENT_COLUMN_UNKNOWN"
    many = pd.DataFrame({"code": [f"v{i}" for i in range(300)]})
    out = _run(context_for(many), "value_counts", {"column": "code", "top": 50})
    assert len(out["values"]) == 50 and out["distinct"] == 300 and out["other_rows"] == 250


def test_value_counts_of_a_personal_column_is_counts_only() -> None:
    frame = pd.DataFrame({"email": [f"p{i % 7}.x@example.com" for i in range(70)], "customer_id": range(70)})
    out = _run(context_for(frame), "value_counts", {"column": "email"})
    assert out["values"] == [] and out["personal_data"] == ["email"]
    assert (out["rows"], out["distinct"], out["empty"]) == (70, 7, 0)
    assert out["most_rows_for_one_value"] == 10
    assert "example.com" not in json.dumps(out)


# ---------------------------------------------------------------------------
# find_values
# ---------------------------------------------------------------------------
def test_find_values_is_a_case_insensitive_plain_substring() -> None:
    ctx = context_for(_frame())
    out = _run(ctx, "find_values", {"column": "plan", "contains": "AS"})
    assert out["matches"] == [{"value": "basic", "rows": 5}]
    assert (out["total_matching_rows"], out["distinct_matching"]) == (5, 1)
    both = _run(ctx, "find_values", {"column": "plan", "contains": "r"})
    assert both["matches"] == [{"value": "pro", "rows": 3}, {"value": "free", "rows": 1}]
    assert both["total_matching_rows"] == 4
    assert _run(ctx, "find_values", {"column": "amount", "contains": "100"})["matches"] == [
        {"value": "1000.0", "rows": 1}
    ]


def test_find_values_never_treats_the_text_as_a_pattern() -> None:
    ctx = context_for(_frame())
    for text in (".*", "(", "[a-z]+", "b.sic", "\\d"):
        assert _run(ctx, "find_values", {"column": "plan", "contains": text})["total_matching_rows"] == 0
    dotted = pd.DataFrame({"v": ["a.b", "axb", "a.b"]})
    out = _run(context_for(dotted), "find_values", {"column": "v", "contains": "a.b"})
    assert out["matches"] == [{"value": "a.b", "rows": 2}]


def test_find_values_bounds_and_errors() -> None:
    ctx = context_for(_frame())
    assert _refused(ctx, "find_values", {"column": "plan", "contains": ""}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "find_values", {"column": "plan", "contains": "x" * 41}) == "AGENT_TOOL_ARGS_INVALID"
    assert (
        _refused(ctx, "find_values", {"column": "plan", "contains": "a", "n": 21})
        == "AGENT_TOOL_ARGS_INVALID"
    )
    assert _refused(ctx, "find_values", {"column": "plan"}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "find_values", {"column": "nope", "contains": "a"}) == "AGENT_COLUMN_UNKNOWN"
    many = pd.DataFrame({"v": [f"item{i}" for i in range(100)]})
    out = _run(context_for(many), "find_values", {"column": "v", "contains": "item", "n": 20})
    assert len(out["matches"]) == 20 and out["total_matching_rows"] == 100 and out["distinct_matching"] == 100


def test_find_values_on_a_personal_column_is_a_count_only() -> None:
    frame = pd.DataFrame({"email": [f"p{i}.x@example.com" for i in range(50)], "n": range(50)})
    out = _run(context_for(frame), "find_values", {"column": "email", "contains": "p1"})
    assert out["matches"] == [] and out["personal_data"] == ["email"]
    # No count at all: one that depends on the text is an oracle (review findings 7 and 12).
    assert "total_matching_rows" not in out and "distinct_matching" not in out
    assert out["distinct"] == 50 and out["rows"] == 50
    assert "example.com" not in json.dumps(out)


# ---------------------------------------------------------------------------
# describe_numbers
# ---------------------------------------------------------------------------
def test_describe_numbers_measures_the_spread() -> None:
    out = _run(context_for(_frame()), "describe_numbers", {"column": "amount"})
    assert out["column"] == "amount" and out["numeric"] is True and out["from_text"] is False
    assert (out["rows"], out["numbers"], out["empty"]) == (10, 10, 0)
    assert (out["minimum"], out["maximum"], out["mean"], out["median"]) == (10.0, 1000.0, 145.0, 55.0)
    assert out["quantiles"]["p25"] == 32.5 and out["quantiles"]["p75"] == 77.5
    assert set(out["quantiles"]) == {"p1", "p5", "p25", "p50", "p75", "p95", "p99"}
    assert (out["share_negative"], out["share_zero"], out["share_empty"]) == (0.0, 0.0, 0.0)
    assert out["whole_numbers"] is True
    assert out["outliers"]["high"] == 1 and out["outliers"]["low"] == 0  # 1000 is past 77.5 + 1.5 * 45
    assert len(out["histogram"]) == 10
    assert out["histogram"][0] == {"from": 10.0, "to": 109.0, "rows": 9}
    assert out["histogram"][-1]["rows"] == 1
    assert sum(bucket["rows"] for bucket in out["histogram"]) == 10


def test_describe_numbers_counts_negative_zero_and_empty_shares() -> None:
    frame = pd.DataFrame({"x": [-5, 0, 0, 3, None, 2.5]})
    out = _run(context_for(frame), "describe_numbers", {"column": "x"})
    assert (out["rows"], out["numbers"], out["empty"]) == (6, 5, 1)
    assert out["share_negative"] == pytest.approx(1 / 6, abs=1e-6)
    assert out["share_zero"] == pytest.approx(2 / 6, abs=1e-6)
    assert out["share_empty"] == pytest.approx(1 / 6, abs=1e-6)
    assert out["whole_numbers"] is False
    assert (out["minimum"], out["maximum"]) == (-5.0, 3.0)


def test_describe_numbers_reads_a_text_column_that_mostly_holds_numbers() -> None:
    frame = pd.DataFrame({"spend": ["1,200", "3,400", "n/a", "₹500", None, "2,000"]})
    out = _run(context_for(frame), "describe_numbers", {"column": "spend"})
    assert out["numeric"] is True and out["from_text"] is True
    assert (out["numbers"], out["unparseable"], out["empty"]) == (4, 1, 1)
    assert (out["minimum"], out["maximum"]) == (500.0, 3400.0)


def test_describe_numbers_says_so_when_the_column_is_not_numbers() -> None:
    frame = pd.DataFrame({"colour": ["red", "blue", "green", "red"]})
    out = _run(context_for(frame), "describe_numbers", {"column": "colour"})
    assert out["numeric"] is False and "minimum" not in out and "histogram" not in out
    assert out["numbers"] == 0 and out["message"]


def test_describe_numbers_refuses_a_personal_column_without_a_min_or_max() -> None:
    frame = pd.DataFrame({"phone": [9876500000 + i for i in range(60)], "n": range(60)})
    ctx = context_for(frame)
    out = _run(ctx, "describe_numbers", {"column": "phone"})
    assert out["personal_data"] == ["phone"]  # a phone number stored as a number is still one
    assert all(key not in out for key in ("minimum", "maximum", "mean", "median", "quantiles", "histogram"))
    assert out["message"] and "9876" not in json.dumps(out)
    assert _refused(ctx, "describe_numbers", {"column": "nope"}) == "AGENT_COLUMN_UNKNOWN"


def test_describe_numbers_of_a_constant_column_has_one_bucket() -> None:
    out = _run(context_for(pd.DataFrame({"x": [4, 4, 4]})), "describe_numbers", {"column": "x"})
    assert out["histogram"] == [{"from": 4.0, "to": 4.0, "rows": 3}]
    assert out["outliers"] == {"low": 0, "high": 0, "count": 0}


# ---------------------------------------------------------------------------
# describe_dates
# ---------------------------------------------------------------------------
def test_describe_dates_reads_text_dates_and_their_written_shapes() -> None:
    frame = pd.DataFrame({"joined": ["01/02/2024", "13/02/2024", "20/03/2024", None, "n/a"]})
    out = _run(context_for(frame), "describe_dates", {"column": "joined"})
    assert out["column"] == "joined" and out["dates_found"] is True
    assert (out["rows"], out["empty"], out["dates"], out["unparseable"]) == (5, 1, 3, 1)
    assert (out["earliest"], out["latest"]) == ("2024-02-01", "2024-03-20")
    assert (out["distinct_days"], out["distinct_months"], out["longest_gap_days"]) == (3, 2, 36)
    assert out["share_with_time"] == 0.0
    assert out["day_first"] is True and out["order_ambiguous"] is False
    assert out["shapes"] == [{"shape": "99/99/9999", "rows": 3}, {"shape": "a/a", "rows": 1}]


def test_describe_dates_flags_a_day_month_order_it_cannot_tell() -> None:
    frame = pd.DataFrame({"d": ["01/02/2024", "03/04/2024", "05/06/2024"]})
    out = _run(context_for(frame), "describe_dates", {"column": "d"})
    assert out["day_first"] is None and out["order_ambiguous"] is True
    month_first = pd.DataFrame({"d": ["01/13/2024", "03/04/2024"]})
    out = _run(context_for(month_first), "describe_dates", {"column": "d"})
    assert out["day_first"] is False and out["order_ambiguous"] is False
    iso = pd.DataFrame({"d": ["2024-01-02", "2024-03-04"]})
    assert _run(context_for(iso), "describe_dates", {"column": "d"})["order_ambiguous"] is False


def test_describe_dates_reads_a_real_date_column_with_a_time_part() -> None:
    frame = pd.DataFrame({"at": pd.to_datetime(["2024-01-01 00:00", "2024-01-01 12:30", "2024-01-04 00:00"])})
    out = _run(context_for(frame), "describe_dates", {"column": "at"})
    assert (out["earliest"], out["latest"]) == ("2024-01-01", "2024-01-04")
    assert (out["distinct_days"], out["distinct_months"], out["longest_gap_days"]) == (2, 1, 3)
    assert out["share_with_time"] == pytest.approx(1 / 3, abs=1e-6)
    assert out["unparseable"] == 0


def test_describe_dates_says_so_when_the_column_holds_no_dates() -> None:
    out = _run(context_for(pd.DataFrame({"c": ["red", "blue", "red"]})), "describe_dates", {"column": "c"})
    assert out["dates_found"] is False and "earliest" not in out and out["message"]


def test_describe_dates_refuses_a_personal_column_and_an_unknown_one() -> None:
    frame = pd.DataFrame({"email": [f"p{i}.x@example.com" for i in range(40)], "n": range(40)})
    ctx = context_for(frame)
    out = _run(ctx, "describe_dates", {"column": "email"})
    assert out["personal_data"] == ["email"] and "earliest" not in out
    assert _refused(ctx, "describe_dates", {"column": "nope"}) == "AGENT_COLUMN_UNKNOWN"


# ---------------------------------------------------------------------------
# compare_columns
# ---------------------------------------------------------------------------
def _pair(**extra: Any) -> pd.DataFrame:
    frame = _frame()
    frame["amount_x2"] = frame["amount"] * 2
    frame["amount_plus5"] = frame["amount"] + 5
    frame["plan_copy"] = frame["plan"]
    frame["plan_code"] = frame["plan"].map({"basic": "b", "pro": "p", "free": "f"})
    frame["plan_group"] = frame["plan"].map({"basic": "paid", "pro": "paid", "free": "free"})
    for name, values in extra.items():
        frame[name] = values
    return frame


def test_compare_columns_finds_a_constant_multiple_and_an_offset() -> None:
    ctx = context_for(_pair())
    out = _run(ctx, "compare_columns", {"left": "amount", "right": "amount_x2"})
    assert out["columns"] == ["amount", "amount_x2"] and out["kind"] == "constant_multiple"
    assert out["slope"] == 2.0 and out["intercept"] == 0.0
    assert out["pearson"] == 1.0 and out["spearman"] == 1.0
    out = _run(ctx, "compare_columns", {"left": "amount", "right": "amount_plus5"})
    assert out["kind"] == "offset" and out["slope"] == 1.0 and out["intercept"] == 5.0
    assert out["equal_share"] == 0.0


def test_compare_columns_reports_identical_columns_with_their_empty_shares() -> None:
    out = _run(context_for(_pair()), "compare_columns", {"left": "plan", "right": "plan_copy"})
    assert out["kind"] == "identical"
    assert out["equal_share"] == 0.9 and out["both_empty_share"] == 0.1
    assert out["equal_of_both_present_share"] == 1.0
    assert (out["only_left_empty_share"], out["only_right_empty_share"]) == (0.0, 0.0)


def test_compare_columns_finds_one_derived_from_the_other_and_shows_a_crosstab() -> None:
    ctx = context_for(_pair())
    out = _run(ctx, "compare_columns", {"left": "plan", "right": "plan_code"})
    assert out["kind"] == "one_to_one"
    assert out["crosstab"][0] == {"left_value": "basic", "right_value": "b", "rows": 5}
    assert len(out["crosstab"]) == 3 and out["distinct_pairs"] == 3
    out = _run(ctx, "compare_columns", {"left": "plan", "right": "plan_group"})
    assert out["kind"] == "left_determines_right"
    assert out["right_given_left_share"] == 1.0 and out["left_given_right_share"] < 1.0
    reverse = _run(ctx, "compare_columns", {"left": "plan_group", "right": "plan"})
    assert reverse["kind"] == "right_determines_left"


def test_compare_columns_measures_correlation_of_unrelated_numbers() -> None:
    x = np.arange(1, 11, dtype=float)
    y = np.array([5, 3, 8, 1, 9, 2, 7, 4, 10, 6], dtype=float)
    out = _run(context_for(pd.DataFrame({"x": x, "y": y})), "compare_columns", {"left": "x", "right": "y"})
    assert out["pearson"] == pytest.approx(float(np.corrcoef(x, y)[0, 1]), abs=1e-6)
    assert out["kind"] == "unrelated" and "slope" not in out
    rising = pd.DataFrame({"x": x, "y": x**2})
    out = _run(context_for(rising), "compare_columns", {"left": "x", "right": "y"})
    assert out["spearman"] == 1.0 and out["kind"] == "correlated"


def test_compare_columns_errors_and_personal_refusal() -> None:
    ctx = context_for(_pair())
    assert _refused(ctx, "compare_columns", {"left": "plan", "right": "nope"}) == "AGENT_COLUMN_UNKNOWN"
    assert _refused(ctx, "compare_columns", {"left": "plan"}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "compare_columns", {"left": "plan", "right": "plan"}) == "AGENT_TOOL_ARGS_INVALID"
    frame = pd.DataFrame(
        {
            "email": [f"p{i % 5}.x@example.com" for i in range(50)],
            "email2": [f"p{i % 5}.x@example.com" for i in range(50)],
        }
    )
    frame["region"] = ["north", "south"] * 25
    out = _run(context_for(frame), "compare_columns", {"left": "email", "right": "region"})
    assert out["personal_data"] == ["email"] and "crosstab" not in out and out["message"]
    assert "example.com" not in json.dumps(out)


# ---------------------------------------------------------------------------
# describe_missing
# ---------------------------------------------------------------------------
def _gappy() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "a": [None, None, None, "x", "x", "x", "x", "x", "x", "x"],
            "b": [None, None, None, "y", "y", "y", "y", "y", "y", "y"],
            "c": [1, 2, 3, 4, None, 6, 7, 8, 9, 10],
            "d": list("abcdefghij"),
            "y": [1, 1, 1, 0, 0, 0, 0, 0, 0, 0],
        }
    )


def test_describe_missing_lists_empty_shares_and_columns_empty_together() -> None:
    out = _run(context_for(_gappy()), "describe_missing")
    assert out["rows"] == 10 and out["columns_total"] == 5 and out["columns_with_missing"] == 3
    assert out["missing"] == [
        {"column": "a", "empty": 3, "share": 0.3},
        {"column": "b", "empty": 3, "share": 0.3},
        {"column": "c", "empty": 1, "share": 0.1},
    ]
    assert out["groups"] == [{"columns": ["a", "b"], "rows": 3}]
    assert out["always_empty"] == {"columns": []}
    dead = _gappy().assign(dead=None)
    assert _run(context_for(dead), "describe_missing")["always_empty"] == {"columns": ["dead"]}
    assert "outcome" not in out or out["outcome"] is None  # no outcome known


def test_describe_missing_relates_the_most_missing_columns_to_the_outcome() -> None:
    out = _run(context_for(_gappy(), target="y"), "describe_missing")
    outcome = out["outcome"]
    assert outcome["column"] == "y" and outcome["overall_positive_rate"] == 0.3
    by = {item["column"]: item for item in outcome["by_column"]}
    assert by["a"] == {
        "column": "a",
        "empty_rows": 3,
        "positive_rate_when_empty": 1.0,
        "positive_rate_when_filled": 0.0,
    }
    assert by["c"]["positive_rate_when_empty"] == 0.0
    assert by["c"]["positive_rate_when_filled"] == pytest.approx(1 / 3, abs=1e-6)


def test_describe_missing_can_be_narrowed_to_some_columns_and_is_bounded() -> None:
    ctx = context_for(_gappy())
    out = _run(ctx, "describe_missing", {"columns": ["c", "d"]})
    assert [item["column"] for item in out["missing"]] == ["c"]
    assert _refused(ctx, "describe_missing", {"columns": ["nope"]}) == "AGENT_COLUMN_UNKNOWN"
    assert (
        _refused(ctx, "describe_missing", {"columns": [f"c{i}" for i in range(31)]})
        == "AGENT_TOOL_ARGS_INVALID"
    )
    assert _refused(ctx, "describe_missing", {"columns": []}) == "AGENT_TOOL_ARGS_INVALID"


def test_describe_missing_lists_at_most_thirty_columns_most_empty_first() -> None:
    rng = np.random.default_rng(3)
    frame = pd.DataFrame(
        {f"col{i:02d}": np.where(rng.random(200) < (i + 1) / 60, None, "v") for i in range(40)}
    )
    out = _run(context_for(frame), "describe_missing")
    assert len(out["missing"]) == 30
    shares = [item["share"] for item in out["missing"]]
    assert shares == sorted(shares, reverse=True)
    assert out["columns_with_missing"] >= 30


# ---------------------------------------------------------------------------
# describe_duplicates
# ---------------------------------------------------------------------------
def _dupes() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": ["a", "a", "b", "c", "c", "c", "d", None, None, "e"],
            "val": [1, 1, 2, 3, 4, 3, 5, 6, 7, 8],
        }
    )


def test_describe_duplicates_by_key_counts_rows_keys_and_groups_that_differ() -> None:
    out = _run(context_for(_dupes()), "describe_duplicates", {"columns": ["id"]})
    assert out["whole_row"] is False and out["columns"] == ["id"]
    assert (out["rows"], out["duplicate_rows"], out["share"]) == (10, 3, 0.3)
    assert (out["repeated_keys"], out["rows_in_repeated_keys"], out["largest_group"]) == (2, 5, 3)
    assert out["empty_key_rows"] == 2
    assert out["top_keys"] == [{"cells": ["c"], "rows": 3}, {"cells": ["a"], "rows": 2}]
    assert out["groups_differing_in_other_columns"] == 1  # `c` has val 3, 4, 3; `a` is the same twice


def test_describe_duplicates_of_whole_rows_and_of_a_two_column_key() -> None:
    ctx = context_for(_dupes())
    out = _run(ctx, "describe_duplicates")
    assert out["whole_row"] is True
    assert (out["duplicate_rows"], out["repeated_keys"]) == (2, 2)  # (a, 1) twice and (c, 3) twice
    assert out["groups_differing_in_other_columns"] == 0
    both = _run(ctx, "describe_duplicates", {"columns": ["id", "val"]})
    assert (
        both["whole_row"] is False
        and both["duplicate_rows"] == 2
        and both["top_keys"][0]["cells"]
        in (
            ["a", "1"],
            ["c", "3"],
        )
    )


def test_describe_duplicates_bounds_and_errors() -> None:
    ctx = context_for(_dupes())
    assert _refused(ctx, "describe_duplicates", {"columns": []}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "describe_duplicates", {"columns": list("abcdef")}) == "AGENT_TOOL_ARGS_INVALID"
    assert _refused(ctx, "describe_duplicates", {"columns": ["nope"]}) == "AGENT_COLUMN_UNKNOWN"
    unique = _run(context_for(pd.DataFrame({"k": range(5)})), "describe_duplicates", {"columns": ["k"]})
    assert (unique["duplicate_rows"], unique["repeated_keys"], unique["top_keys"]) == (0, 0, [])


# ---------------------------------------------------------------------------
# Personal data never leaves a tool
# ---------------------------------------------------------------------------
FIRST = ("Priya", "Rahul", "Anita", "John", "Maria")
LAST = ("Sharma", "Verma", "Desai", "Smith", "Garcia")
PLANTED = (
    "@example.com",
    "priya.sharma",
    "rahul.verma",
    "98765",
    "Priya",
    "Sharma",
    "Garcia",
    "ABCDE1",
    "ABCDE2",
)


def _personal_frame() -> pd.DataFrame:
    rows = 80
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(rows)],
            "email": [f"{FIRST[i % 5].lower()}.{LAST[i % 5].lower()}{i}@example.com" for i in range(rows)],
            "phone": [f"+91 98765 {43210 + i}" for i in range(rows)],
            "customer_name": [f"{FIRST[i % 5]} {LAST[i % 5]}" for i in range(rows)],
            "pan": [f"ABCDE{1000 + i % 30}F" for i in range(rows)],
            "amount": [float(i) for i in range(rows)],
        }
    )
    frame["notes"] = [
        f"Asked to be called on +91 98765 {43210 + i}, mail {FIRST[i % 5].lower()}.{LAST[i % 5].lower()}{i}@example.com, PAN ABCDE{1000 + i % 30}F"
        for i in range(rows)
    ]
    return frame


def _every_call(frame: pd.DataFrame) -> list[dict[str, Any]]:
    ctx = context_for(frame)
    columns = [str(c) for c in frame.columns]
    calls: list[tuple[str, dict[str, Any]]] = [
        ("sample_rows", {"columns": columns[:12], "n": 20}),
        ("sample_rows", {"columns": columns, "n": 5, "where_column": "customer_id", "equals": "C3"}),
        ("describe_missing", {}),
        ("describe_duplicates", {}),
    ]
    for column in columns:
        calls += [
            ("value_counts", {"column": column, "top": 50}),
            ("describe_numbers", {"column": column}),
            ("describe_dates", {"column": column}),
            ("describe_duplicates", {"columns": [column]}),
        ]
        for text in ("a", "priya", "98765", "ABCDE", "@", "example", "Sharma", "1"):
            calls.append(("find_values", {"column": column, "contains": text, "n": 20}))
        for other in columns:
            if other != column:
                calls.append(("compare_columns", {"left": column, "right": other}))
    results: list[dict[str, Any]] = []
    for name, args in calls:
        try:
            results.append(call_tool(ctx, name, args, evidence_id="e1").result)
        except AgentToolError as exc:
            # A refusal for a personal column must not carry a value either.
            results.append({"error": exc.code, "message": exc.message})
    return results


def test_no_tool_result_carries_a_planted_personal_value() -> None:
    frame = _personal_frame()
    ctx = context_for(frame)
    personal = {c.name for c in ctx.profile.columns if c.pii_kinds}
    assert {"email", "phone", "customer_name", "pan"} <= personal, personal  # the profile sees them
    results = _every_call(frame)
    assert len(results) > 100
    dumped = "\n".join(json.dumps(result) for result in results)
    for planted in PLANTED:
        assert planted not in dumped, planted
    # the free-text column is shown, masked, and the mask is what appears
    sample = _run(ctx, "sample_rows", {"columns": ["notes"], "n": 2})
    assert "[REDACTED:" in json.dumps(sample)


def test_a_personal_column_cannot_be_used_to_filter_and_learn_a_value() -> None:
    ctx = context_for(_personal_frame())
    assert _refused(ctx, "sample_rows", {"where_column": "email", "equals": "priya.sharma0@example.com"}) == (
        "AGENT_TOOL_ARGS_INVALID"
    )


def test_a_free_text_column_is_masked_cell_by_cell_before_the_cut() -> None:
    frame = pd.DataFrame(
        {
            "notes": [
                f"mail priya.sharma{i}@example.com or ring +91 98765 4321{i % 10} now" for i in range(60)
            ],
            "n": range(60),
        }
    )
    ctx = context_for(frame)
    for name, args in (
        ("sample_rows", {"columns": ["notes"], "n": 10}),
        ("value_counts", {"column": "notes"}),
        ("find_values", {"column": "notes", "contains": "mail"}),
        ("describe_duplicates", {"columns": ["notes"]}),
    ):
        text = json.dumps(_run(ctx, name, args))
        assert "@" not in text and "98765" not in text and "priya.sharma" not in text, name


# ---------------------------------------------------------------------------
# Size, determinism, read-only, speed
# ---------------------------------------------------------------------------
def _wide_frame(rows: int = 600, columns: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    data: dict[str, Any] = {}
    for i in range(columns):
        name = f"feature_{i:04d}_{'w' * 30}"
        if i % 3 == 0:
            data[name] = rng.normal(100, 30, rows)
        elif i % 3 == 1:
            data[name] = rng.choice([f"category number {j} " + "z" * 30 for j in range(400)], rows)
        else:
            data[name] = np.where(rng.random(rows) < 0.3, None, rng.choice(list("abcdef"), rows))
    return pd.DataFrame(data)


def test_every_result_on_a_300_column_frame_fits_well_inside_a_prompt() -> None:
    frame = _wide_frame()
    ctx = context_for(frame)
    columns = [str(c) for c in frame.columns]
    calls: list[tuple[str, dict[str, Any]]] = [
        ("sample_rows", {"columns": columns[:12], "n": 20}),
        ("sample_rows", {}),
        ("describe_missing", {}),
        ("describe_missing", {"columns": columns[:30]}),
        ("describe_duplicates", {}),
        ("describe_duplicates", {"columns": columns[:5]}),
        ("value_counts", {"column": columns[1], "top": 50}),
        ("value_counts", {"column": columns[0], "top": 50}),
        ("find_values", {"column": columns[1], "contains": "category", "n": 20}),
        ("describe_numbers", {"column": columns[0]}),
        ("describe_dates", {"column": columns[1]}),
        ("compare_columns", {"left": columns[1], "right": columns[4]}),
        ("compare_columns", {"left": columns[0], "right": columns[3]}),
    ]
    for name, args in calls:
        size = len(json.dumps(_run(ctx, name, args)))
        assert size < MAX_RESULT_CHARS, (name, args.get("column"), size)


def test_the_same_call_gives_the_same_answer_and_the_frame_is_untouched() -> None:
    frame = _pair(noise=[3, 1, 2, 3, 1, 2, 3, 1, 2, 3])
    before = pd.util.hash_pandas_object(frame, index=True).sum()
    ctx = context_for(frame, target="noise")
    calls: list[tuple[str, dict[str, Any]]] = [
        ("sample_rows", {"n": 4, "offset": 1}),
        ("value_counts", {"column": "noise"}),
        ("find_values", {"column": "plan", "contains": "a"}),
        ("describe_numbers", {"column": "amount"}),
        ("describe_dates", {"column": "plan"}),
        ("compare_columns", {"left": "plan", "right": "noise"}),
        ("describe_missing", {}),
        ("describe_duplicates", {"columns": ["noise"]}),
    ]
    for name, args in calls:
        first, second = _run(ctx, name, args), _run(ctx, name, args)
        assert first == second, name
    assert pd.util.hash_pandas_object(frame, index=True).sum() == before


@pytest.mark.slow
def test_every_tool_is_fast_on_a_million_rows() -> None:
    rng = np.random.default_rng(5)
    n = 1_000_000
    frame = pd.DataFrame(
        {
            "customer_id": np.char.add("C-", np.arange(n).astype(str)),
            "plan": rng.choice(["basic", "pro", "free", "team"], n),
            "amount": rng.gamma(2.0, 50.0, n),
            "signed_up": pd.Series(pd.date_range("2022-01-01", periods=730, freq="D").strftime("%d/%m/%Y"))
            .sample(n, replace=True, random_state=1)
            .to_numpy(),
            "score": np.where(rng.random(n) < 0.2, np.nan, rng.random(n)),
            "y": rng.integers(0, 2, n),
        }
    )
    ctx = context_for(frame.head(2_000), target="y")
    ctx = type(ctx)(**{**ctx.__dict__, "frame": frame})
    for name, args in (
        ("sample_rows", {"where_column": "plan", "equals": "pro"}),
        ("value_counts", {"column": "plan"}),
        ("value_counts", {"column": "customer_id", "top": 50}),
        ("find_values", {"column": "customer_id", "contains": "99"}),
        ("describe_numbers", {"column": "amount"}),
        ("describe_dates", {"column": "signed_up"}),
        ("compare_columns", {"left": "plan", "right": "signed_up"}),
        ("compare_columns", {"left": "amount", "right": "score"}),
        ("describe_missing", {}),
        ("describe_duplicates", {"columns": ["customer_id"]}),
        ("describe_duplicates", {}),
    ):
        started = time.perf_counter()
        result = _run(ctx, name, args)
        elapsed = time.perf_counter() - started
        assert len(json.dumps(result)) < MAX_RESULT_CHARS, name
        assert elapsed < 8.0, (name, args, elapsed)


# ---------------------------------------------------------------------------
# Grounding: numbers in these results are evidence
# ---------------------------------------------------------------------------
def test_a_number_from_value_counts_is_grounded_and_an_invented_one_is_not() -> None:
    ctx = context_for(_frame())
    result = call_tool(ctx, "value_counts", {"column": "plan"}, evidence_id="e1")
    grounded = grounded_numbers([]).union(evidence_numbers(result.result, result.args))
    names = ("customer_id", "plan", "amount")
    assert ungrounded_numbers("The basic plan has 5 rows, half of the file (50%).", grounded, names) == []
    assert ungrounded_numbers("The basic plan has 7 rows.", grounded, names) == ["7"]
    assert ungrounded_numbers("There are 3 distinct plans and 1 empty cell.", grounded, names) == []


def test_an_empty_string_counts_as_empty_like_a_null() -> None:
    frame = pd.DataFrame(
        {
            "plan": ["basic", "", None, "pro", "basic", ""],
            "seen": ["2024-01-05", "", "", "2024-02-01", None, "2024-01-09"],
            "spend": ["10", "", "20", None, "30", ""],
            "y": [1, 0, 1, 0, 0, 0],
        }
    )
    ctx = context_for(frame)
    counts = _run(ctx, "value_counts", {"column": "plan"})
    assert (counts["empty"], counts["distinct"]) == (3, 2)
    assert [v["value"] for v in counts["values"]] == ["basic", "pro"]
    assert _run(ctx, "describe_dates", {"column": "seen"})["empty"] == 3
    numbers = _run(ctx, "describe_numbers", {"column": "spend"})
    assert (numbers["empty"], numbers["numbers"], numbers["unparseable"]) == (3, 3, 0)
    missing = _run(ctx, "describe_missing")["missing"]
    assert {(m["column"], m["empty"]) for m in missing} == {("plan", 3), ("seen", 3), ("spend", 3)}
    assert _run(ctx, "find_values", {"column": "plan", "contains": "a"})["total_matching_rows"] == 2


# ---------------------------------------------------------------------------
# The chat loop can call them (the fake client's GROUNDED mode drives sample_rows and value_counts)
# ---------------------------------------------------------------------------
class _FakeClientMeter:
    """A `Meter` stand-in that hands the rendered prompt to the fake model."""

    def __init__(self) -> None:
        self.client = FakeLLMClient(mode=FakeLLMMode.GROUNDED)

    def complete(self, rendered: Any, purpose: Any) -> Any:
        del purpose
        return self.client.complete(rendered.user, system=rendered.system)


@pytest.mark.parametrize(
    ("message", "tool"),
    [
        ("Show me a sample of the rows please", "sample_rows"),
        ("What is the most common 'plan_tier'?", "value_counts"),
    ],
)
def test_a_chat_turn_can_look_at_rows_and_values_and_the_reply_is_grounded(message: str, tool: str) -> None:
    from engine.agent.loop import chat_turn
    from engine.agent.session import start_session
    from tests.unit.agent.helpers import synthetic

    ctx = context_for(synthetic(rows=400))
    session = start_session(ctx, session_id="s-look")
    turn = chat_turn(ctx, session, message, meter=_FakeClientMeter(), guardrails=Guardrails(load_policy()))
    assert turn.blocked_by is None, turn.reply.text
    assert [r.tool for r in turn.tool_results] == [tool]
    assert turn.reply.evidence_ids == (turn.tool_results[0].evidence_id,)
    assert turn.llm_calls == 2
