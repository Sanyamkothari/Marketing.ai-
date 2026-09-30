"""The helper's read tools (Plan G M71): wrap existing systems, show no personal data, write nothing."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.agent.tools import TOOLS, AgentToolError, ToolKind, call_tool
from engine.config import resolve_config
from engine.stages import validate
from tests.fixtures.make_data import LEAKY_COLUMN, PII_EMAIL_COLUMN
from tests.unit.agent.helpers import context_for, synthetic


def _run(ctx: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result = call_tool(ctx, name, args, evidence_id="e1")
    assert result.tool == name
    return result.result


def test_m71_ships_only_read_tools() -> None:
    assert set(TOOLS) == {
        "get_profile",
        "inspect_column",
        "describe_outcome",
        "find_format_issues",
        "find_roles",
        "describe_repeats",  # M76
        "check_data",
        # The look-at-the-data tools (tests/unit/agent/test_look_tools.py)
        "sample_rows",
        "value_counts",
        "find_values",
        "describe_numbers",
        "describe_dates",
        "compare_columns",
        "describe_missing",
        "describe_duplicates",
        "describe_placeholder_values",  # DEC-1222
    }
    assert {tool.kind for tool in TOOLS.values()} == {ToolKind.READ}


def test_an_unknown_tool_or_bad_arguments_are_refused_with_a_code() -> None:
    ctx = context_for(synthetic(rows=300))
    with pytest.raises(AgentToolError) as unknown:
        call_tool(ctx, "delete_everything", {}, evidence_id="e1")
    assert unknown.value.code == "AGENT_TOOL_UNKNOWN"
    with pytest.raises(AgentToolError) as bad:
        call_tool(ctx, "inspect_column", {"column": "visits_last_7d", "rows": 5}, evidence_id="e1")
    assert bad.value.code == "AGENT_TOOL_ARGS_INVALID"
    with pytest.raises(AgentToolError) as missing:
        call_tool(ctx, "inspect_column", {"column": "nope"}, evidence_id="e1")
    assert missing.value.code == "AGENT_COLUMN_UNKNOWN"


def test_get_profile_reports_the_detected_roles() -> None:
    result = _run(context_for(synthetic(rows=500)), "get_profile")
    assert result["rows"] == 500
    assert "customer_id" in result["primary_key_candidates"]
    assert result["target_candidate"] == "converted_30d"
    assert {c["name"] for c in result["columns"]} >= {"customer_id", "converted_30d"}


def test_inspect_column_shows_how_the_outcome_rate_moves() -> None:
    ctx = context_for(synthetic(rows=2_000), target="converted_30d")
    result = _run(ctx, "inspect_column", {"column": "visits_last_7d"})
    relation = result["relation_to_outcome"]
    assert relation is not None and relation["buckets"]
    assert sum(bucket["rows"] for bucket in relation["buckets"]) <= 2_000
    assert 0.0 < relation["overall_positive_rate"] < 1.0


def test_a_personal_data_column_never_shows_a_value() -> None:
    frame = synthetic("pii_column", rows=500)
    ctx = context_for(frame, target="converted_30d")
    result = _run(ctx, "inspect_column", {"column": PII_EMAIL_COLUMN})
    assert result["personal_data"]
    assert result["examples"] == [] and result["top_values"] == []
    assert result["relation_to_outcome"] is None
    dumped = str(result)
    assert not any(str(value) in dumped for value in frame[PII_EMAIL_COLUMN].dropna().head(20))


def test_find_format_issues_skips_personal_data_columns() -> None:
    frame = synthetic("pii_column", rows=300)
    frame["bill"] = ["₹1,200"] * len(frame)
    columns = [issue["column"] for issue in _run(context_for(frame), "find_format_issues")["issues"]]
    assert "bill" in columns
    assert PII_EMAIL_COLUMN not in columns


def test_find_roles_uses_the_use_case_hints() -> None:
    frame = synthetic(rows=300).rename(columns={"converted_30d": "Purchased"})
    frame["unsubscribed_flag"] = 0
    result = _run(context_for(frame), "find_roles")
    assert result["configured_target"] == "converted_30d"
    assert result["target_exact"] == []
    assert [c["column"] for c in result["target_by_synonym"]] == ["Purchased"]
    assert "marketing_opt_in" in result["consent"]
    assert "unsubscribed_flag" in result["opt_out"]
    assert "last_contacted_at" in result["recently_contacted"]


@pytest.mark.parametrize(
    "variant", ["clean", "duplicate_keys", "leaky_column", "too_few_positives", "constant_column"]
)
def test_check_data_matches_the_run_buttons_checks(variant: str) -> None:
    """The dry run is `POST /runs`'s own validation: same codes, severities and outcome."""
    frame = synthetic(variant, rows=1_500)
    ctx = context_for(frame)
    result = _run(ctx, "check_data", {"primary_key": "customer_id", "target": "converted_30d"})
    config = resolve_config("targeted-advertisement", {}).config
    expected = validate.validate_for_training(
        frame,
        config,
        primary_key="customer_id",
        target="converted_30d",
        upload_id="u-test",
        row_count=len(frame),
    )
    assert result["passed"] is expected.passed
    assert [(c["code"], c["severity"], c["column"]) for c in result["checks"]] == [
        (c.code, c.severity.value, c.column) for c in expected.checks
    ]


def test_check_data_honours_overrides_the_way_a_run_does() -> None:
    frame = synthetic("leaky_column", rows=1_500)
    ctx = context_for(frame)
    before = _run(ctx, "check_data", {"primary_key": "customer_id", "target": "converted_30d"})
    leak = next(c for c in before["checks"] if c["code"] == "LEAKAGE_SUSPECTED")
    assert leak["override_path"] == "prepare.exclude_columns"
    after = _run(
        ctx,
        "check_data",
        {
            "primary_key": "customer_id",
            "target": "converted_30d",
            "overrides": {"prepare.exclude_columns": [LEAKY_COLUMN]},
        },
    )
    assert not any(c["code"] == "LEAKAGE_SUSPECTED" and c["severity"] == "error" for c in after["checks"])


def test_check_data_refuses_an_illegal_override_with_the_configs_code() -> None:
    ctx = context_for(synthetic(rows=300))
    with pytest.raises(AgentToolError) as caught:
        call_tool(ctx, "check_data", {"overrides": {"agent.enabled": False}}, evidence_id="e1")
    assert caught.value.code == "OVERRIDE_UNKNOWN_PATH"


def test_tools_do_not_change_the_frame() -> None:
    frame = synthetic("pii_column", rows=400)
    before = pd.util.hash_pandas_object(frame, index=True).sum()
    ctx = context_for(frame, target="converted_30d")
    for name, args in [
        ("get_profile", None),
        ("inspect_column", {"column": "region"}),
        ("find_format_issues", None),
        ("find_roles", None),
        ("describe_repeats", {"column": "customer_id"}),
        ("check_data", {"primary_key": "customer_id", "target": "converted_30d"}),
    ]:
        call_tool(ctx, name, args, evidence_id="e1")
    assert pd.util.hash_pandas_object(frame, index=True).sum() == before


def test_results_are_plain_json() -> None:
    ctx = context_for(synthetic(rows=300), target="converted_30d")
    result = call_tool(ctx, "inspect_column", {"column": "tenure_months"}, evidence_id="e7")
    assert result.evidence_id == "e7"
    assert result.model_dump_json()


def test_describe_outcome_counts_yes_by_the_checks_rule() -> None:
    frame = synthetic(rows=2_000)
    result = _run(context_for(frame), "describe_outcome", {"column": "converted_30d"})
    assert result["two_valued"] is True
    assert result["positives"] + result["negatives"] == 2_000
    assert result["positives"] == int((frame["converted_30d"] == 1).sum())
    assert 0.05 < result["positive_rate"] < 0.2


def test_describe_repeats_measures_rows_per_id() -> None:
    frame = pd.DataFrame({"customer_id": ["a", "a", "a", "b", None, "c"], "x": range(6)})
    result = _run(context_for(frame), "describe_repeats", {"column": "customer_id"})
    assert result == {
        "column": "customer_id",
        "rows": 6,
        "empty": 1,
        "ids": 3,
        "rows_per_id": 1.7,
        "most_rows": 3,
        "ids_on_several_rows": 1,
    }
