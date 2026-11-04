"""A look tool's arguments are read for their shape, not their spelling (`call_tool`): a null is no argument, a lone
string is a list of one, an unknown key is dropped and said, and a column is found by its name ignoring case, spaces
and underscores. A refusal names what would work."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.agent.tools import AgentToolError, call_tool
from tests.unit.agent.helpers import context_for


def _ctx() -> Any:
    return context_for(
        pd.DataFrame(
            {"order_amount": [1.0, 2.0, 3.0], "Order Date": ["a", "b", "c"], "plan": ["x", "y", "x"]}
        )
    )


def _refusal(name: str, args: dict[str, Any]) -> AgentToolError:
    with pytest.raises(AgentToolError) as caught:
        call_tool(_ctx(), name, args, evidence_id="e1")
    return caught.value


def test_a_null_argument_is_no_argument() -> None:
    result = call_tool(_ctx(), "value_counts", {"column": "plan", "top": None}, evidence_id="e1")
    assert result.args["top"] == 20 and "ignored_arguments" not in result.result


def test_an_unknown_argument_is_dropped_and_said() -> None:
    result = call_tool(_ctx(), "value_counts", {"column": "plan", "colour": "red"}, evidence_id="e1")
    assert "colour" in result.result["ignored_arguments"]


def test_a_lone_string_is_a_list_of_one_where_a_list_is_wanted() -> None:
    result = call_tool(_ctx(), "find_format_issues", {"columns": "plan"}, evidence_id="e1")
    assert list(result.args["columns"]) == ["plan"]


@pytest.mark.parametrize("spelled", ["Order_Amount", "order amount", "ORDERAMOUNT"])
def test_a_column_is_found_ignoring_case_spaces_and_underscores(spelled: str) -> None:
    result = call_tool(_ctx(), "describe_numbers", {"column": spelled}, evidence_id="e1")
    assert result.result["column"] == "order_amount"


def test_an_unknown_column_names_the_three_closest() -> None:
    caught = _refusal("describe_numbers", {"column": "order_amt"})
    assert caught.code == "AGENT_COLUMN_UNKNOWN"
    assert "'order_amount'" in caught.refusal.how_to_fix and caught.refusal.how_to_fix.count("'") == 6


def test_a_refused_argument_lists_the_tools_arguments() -> None:
    caught = _refusal("value_counts", {"top": 5})
    assert caught.code == "AGENT_TOOL_ARGS_INVALID"
    assert "column (required)" in caught.refusal.how_to_fix and "top" in caught.refusal.how_to_fix
