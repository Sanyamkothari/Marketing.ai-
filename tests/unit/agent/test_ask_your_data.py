"""Ask your data (Plan I, DEC-1250 … DEC-1259): the `rate_by` tool, the read-only explore session and its charts.

`rate_by` is worked out by hand on small frames: the groups of a text column and of a number column, the
rate of a yes/no outcome and the mean of a number, and the small-group rule - a value held by fewer than
`MIN_GROUP_ROWS` rows is never named, a group that small shows no figure, and the figures left out cannot be
worked back from the totals. Then the egress gate on its result, and the explore session through the real
`chat_turn`: `propose_setting` is refused, no setting is listed, and a reply's chart is read from the tool
result, never from the reply.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.config import DataAccess
from engine.agent.contracts import AgentSession, ChatRole, SessionStatus
from engine.agent.egress import Egress
from engine.agent.explore import EXPLORE_NOTE, charts_of, start_explore
from engine.agent.loop import EXPLORE_READ_ONLY, PROPOSE_TOOL, chat_turn
from engine.agent.tools import MIN_GROUP_ROWS, TOOLS, AgentToolError, ToolKind, call_tool
from tests.unit.agent.egress_canary import (
    EMAIL,
    HARD_SECRETS,
    ToolingClient,
    after,
    canary_context,
    guardrails,
    leaks,
    meter_for,
    stray_keys,
)
from tests.unit.agent.helpers import context_for


def _rate(ctx: Any, **args: Any) -> dict[str, Any]:
    return call_tool(ctx, "rate_by", args, evidence_id="e1").result


def _groups(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(g["group"]): g for g in result["groups"]}


def _states_frame() -> pd.DataFrame:
    """400 customers in three states (200 / 120 / 70), four rare ones (one row each) and six empty."""
    states = ["CA"] * 200 + ["NY"] * 120 + ["TX"] * 70 + ["Rare1", "Rare2", "Rare3", "Rare4"] + [None] * 6
    churned = (["yes"] * 50 + ["no"] * 150) + (["yes"] * 60 + ["no"] * 60) + (["yes"] * 7 + ["no"] * 63)
    churned += ["yes"] * 4 + ["no"] * 6
    return pd.DataFrame(
        {
            "customer_id": [f"C{i:04d}" for i in range(400)],
            "state": states,
            "churned": churned,
            "spend": [float(i) for i in range(400)],
        }
    )


# ---------------------------------------------------------------------------
# rate_by: the groups and what is measured
# ---------------------------------------------------------------------------
def test_rate_by_is_a_registered_read_tool() -> None:
    assert TOOLS["rate_by"].kind is ToolKind.READ


def test_a_text_column_gives_each_common_value_its_rate_and_puts_rare_values_in_other() -> None:
    result = _rate(context_for(_states_frame()), column="state", outcome_column="churned")
    assert result["function"] == "positive_rate"
    assert result["outcome"] == {
        "column": "churned",
        "positive_label": "yes",
        "overall_positive_rate": 0.3025,
    }
    groups = _groups(result)
    assert list(groups) == ["CA", "NY", "TX", "(other)"]  # most rows first; (other) last
    assert (groups["CA"]["rows"], groups["CA"]["share"], groups["CA"]["positive_rate"]) == (200, 0.5, 0.25)
    assert (groups["NY"]["rows"], groups["NY"]["positive_rate"]) == (120, 0.5)
    assert (groups["TX"]["rows"], groups["TX"]["positive_rate"]) == (70, 0.1)
    # Four one-row values and six empty cells (fewer than ten): one (other) group of ten, never named.
    assert (groups["(other)"]["rows"], groups["(other)"]["positive_rate"]) == (10, 0.4)
    assert "Rare1" not in json.dumps(result)
    assert result["groups_suppressed"] == 0
    assert sum(g["rows"] for g in result["groups"]) == result["rows"] == 400


def test_top_limits_the_named_values() -> None:
    result = _rate(context_for(_states_frame()), column="state", top=2)
    assert [g["group"] for g in result["groups"]] == ["CA", "NY", "(other)"]
    assert _groups(result)["(other)"]["rows"] == 80
    assert result["function"] == "rows" and result["outcome"] is None


def test_a_number_column_is_cut_into_ranges_of_about_equal_rows() -> None:
    result = _rate(context_for(_states_frame()), column="spend", bins=4, outcome_column="churned")
    assert result["kind"] == "number_ranges"
    groups = result["groups"]
    assert [g["rows"] for g in groups] == [100, 100, 100, 100]
    # Each range is the smallest and largest value the group really holds.
    assert [(g["low"], g["high"], g["group"]) for g in groups] == [
        (0.0, 99.0, "0 to 99"),
        (100.0, 199.0, "100 to 199"),
        (200.0, 299.0, "200 to 299"),
        (300.0, 399.0, "300 to 399"),
    ]
    # Rows 0-49 churned, 200-259, 320-326 and 390-393 too: 50, 0, 60 and 11 of each hundred.
    assert [g["positive_rate"] for g in groups] == [0.5, 0.0, 0.6, 0.11]


def test_a_number_outcome_gives_each_group_its_mean() -> None:
    result = _rate(context_for(_states_frame()), column="state", outcome_column="spend")
    groups = _groups(result)
    assert result["function"] == "mean"
    assert groups["CA"]["mean"] == pytest.approx(99.5)
    assert groups["NY"]["mean"] == pytest.approx(259.5)
    assert result["outcome"]["mean"] == pytest.approx(199.5)


def test_an_outcome_that_is_neither_yes_no_nor_a_number_is_refused() -> None:
    ctx = context_for(_states_frame())
    with pytest.raises(AgentToolError) as caught:
        call_tool(ctx, "rate_by", {"column": "churned", "outcome_column": "state"}, evidence_id="e1")
    assert caught.value.code == "AGENT_TOOL_ARGS_INVALID"
    with pytest.raises(AgentToolError) as same:
        call_tool(ctx, "rate_by", {"column": "state", "outcome_column": "state"}, evidence_id="e1")
    assert same.value.code == "AGENT_TOOL_ARGS_INVALID"
    with pytest.raises(AgentToolError) as unknown:
        call_tool(ctx, "rate_by", {"column": "nope"}, evidence_id="e1")
    assert unknown.value.code == "AGENT_COLUMN_UNKNOWN"


# ---------------------------------------------------------------------------
# rate_by: small groups
# ---------------------------------------------------------------------------
def test_a_small_group_shows_no_figure_and_takes_the_next_smallest_with_it() -> None:
    """Three empty spend cells are a group of three: suppressed. Alone, its rate could be worked back from
    the overall rate, so the smallest range is suppressed with it."""
    frame = _states_frame()
    frame.loc[[0, 1, 2], "spend"] = np.nan
    result = _rate(context_for(frame), column="spend", bins=4, outcome_column="churned")
    groups = _groups(result)
    assert groups["(empty)"] == {
        "group": "(empty)",
        "rows": None,
        "share": None,
        "suppressed": True,
        "positive_rate": None,
    }
    hidden = [g for g in result["groups"] if g["suppressed"]]
    assert len(hidden) == result["groups_suppressed"] == 2
    assert result["min_group_rows"] == MIN_GROUP_ROWS
    shown_rows = sum(g["rows"] for g in result["groups"] if not g["suppressed"])
    assert result["rows"] - shown_rows >= MIN_GROUP_ROWS  # what is left out is never fewer than ten rows


def test_a_group_whose_outcome_is_mostly_empty_is_suppressed() -> None:
    frame = _states_frame()
    frame.loc[frame["state"] == "TX", "churned"] = None
    frame.loc[frame.index[frame["state"] == "TX"][:5], "churned"] = "yes"  # five known outcomes in TX
    groups = _groups(_rate(context_for(frame), column="state", outcome_column="churned"))
    assert groups["TX"]["suppressed"] is True and groups["TX"]["positive_rate"] is None


def test_a_file_smaller_than_one_group_shows_nothing() -> None:
    frame = pd.DataFrame({"plan": ["a", "a", "b", "b", "b"], "y": [1, 0, 1, 1, 0]})
    result = _rate(context_for(frame), column="plan", outcome_column="y")
    assert all(g["suppressed"] for g in result["groups"])
    assert "a" not in [g["group"] for g in result["groups"]]


# ---------------------------------------------------------------------------
# rate_by: privacy
# ---------------------------------------------------------------------------
def test_a_personal_data_column_is_refused_as_group_and_as_outcome() -> None:
    frame = _states_frame()
    frame["contact_email"] = [f"user{i}@example.test" for i in range(len(frame))]
    ctx = context_for(frame)
    for args in ({"column": "contact_email"}, {"column": "state", "outcome_column": "contact_email"}):
        result = _rate(ctx, **args)
        assert result["groups"] == [] and result["personal_data"]
        assert "example.test" not in json.dumps(result)


def test_a_hidden_column_is_refused_too() -> None:
    ctx = context_for(_states_frame())
    agent = ctx.config.agent.model_copy(update={"always_hide_columns": ("state",)})
    ctx = dataclasses.replace(ctx, config=ctx.config.model_copy(update={"agent": agent}))
    result = _rate(ctx, column="state", outcome_column="churned")
    assert result["groups"] == [] and "CA" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["masked_data", "summaries_only"])
def test_the_gate_masks_group_labels_and_keeps_the_figures(mode: str) -> None:
    """A category value that holds an e-mail address is masked whole; in summaries_only it is a shape, while the
    rows, shares and rates (counts the engine measured) stay readable. Every key is one of ours."""
    frame = _states_frame()
    frame["segment"] = [EMAIL if i % 2 else "plain words" for i in range(len(frame))]
    ctx = canary_context(mode=mode, frame=frame)
    tool = call_tool(ctx, "rate_by", {"column": "segment", "outcome_column": "churned"}, evidence_id="e1")
    assert stray_keys(tool.result, ctx.frame.columns) == []
    gate = Egress.build(
        [str(c) for c in ctx.frame.columns],
        personal_columns=[c.name for c in ctx.profile.columns if c.pii_kinds],
        mode=DataAccess(mode),
    )
    prepared = gate.prepare(tool.result)
    text = json.dumps(prepared)
    assert leaks(text, HARD_SECRETS) == []
    assert [g["rows"] for g in prepared["groups"]] == [g["rows"] for g in tool.result["groups"]]
    assert [g["positive_rate"] for g in prepared["groups"]] == [
        g["positive_rate"] for g in tool.result["groups"]
    ]
    if mode == "summaries_only":
        assert "plain words" not in text


# ---------------------------------------------------------------------------
# The explore session through the real chat turn
# ---------------------------------------------------------------------------
def _explore(ctx: Any) -> AgentSession:
    return start_explore(ctx, session_id="x-test")


def test_an_explore_session_is_ready_and_holds_nothing_to_decide() -> None:
    session = _explore(context_for(_states_frame()))
    assert session.explore and session.status is SessionStatus.READY
    assert session.proposals == () and session.questions == ()
    with pytest.raises(ValueError):
        AgentSession.model_validate({**session.model_dump(), "status": "needs_review"})


def test_explore_refuses_propose_setting_and_lists_no_setting() -> None:
    ctx = canary_context(frame=_states_frame())
    session = _explore(ctx)
    client = ToolingClient(
        [
            {
                "action": PROPOSE_TOOL,
                "args": {"path": "model_search.strategy", "value": "fast", "reason": "Faster."},
            }
        ]
    )
    turn = chat_turn(ctx, session, "Make training faster", meter=meter_for(client), guardrails=guardrails())
    assert turn.proposals == ()
    assert turn.reply.turn is not None and EXPLORE_READ_ONLY in turn.reply.turn.error_codes
    prompt = client.sent_text[0]
    assert EXPLORE_NOTE in prompt
    assert "model_search.strategy" not in prompt  # no setting is offered at all
    # The refusal is fed back to the model, so its next prompt says why.
    assert EXPLORE_READ_ONLY in client.sent_text[1]


def test_a_guided_session_still_takes_a_setting_suggestion() -> None:
    """The refusal is explore-only: Guided setup's chat suggests settings as before."""
    from engine.agent.session import start_session

    ctx = canary_context(frame=_states_frame())
    session = start_session(ctx, session_id="g-test")
    client = ToolingClient(
        [
            {
                "action": PROPOSE_TOOL,
                "args": {"path": "model_search.strategy", "value": "fast", "reason": "Faster."},
            }
        ]
    )
    turn = chat_turn(ctx, session, "Make training faster", meter=meter_for(client), guardrails=guardrails())
    assert [p.path for p in turn.proposals] == ["model_search.strategy"]


class _QuotingClient(ToolingClient):
    """Calls `rate_by`, then replies quoting `text`."""

    def __init__(self, text: str) -> None:
        super().__init__(
            [{"action": "rate_by", "args": {"column": "state", "outcome_column": "churned"}}], per_turn=1
        )
        self.text = text

    def _body(self, system: str, user: str) -> str:
        if self.queue:
            return super()._body(system, user)
        return json.dumps({"action": "reply", "text": self.text, "evidence_ids": ["c1-e1"]})


def test_a_reply_may_quote_a_rate_by_figure_but_not_invent_one() -> None:
    ctx = canary_context(frame=_states_frame())
    ok = chat_turn(
        ctx,
        _explore(ctx),
        "Churn rate by state",
        meter=meter_for(client := _QuotingClient("New York churns most: 0.5 of its 120 customers.")),
        guardrails=guardrails(),
    )
    assert ok.blocked_by is None and "0.5 of its 120" in ok.reply.text
    assert client.calls
    bad = chat_turn(
        ctx,
        _explore(ctx),
        "Churn rate by state",
        meter=meter_for(_QuotingClient("New York churns most: 0.73 of its 120 customers.")),
        guardrails=guardrails(),
    )
    assert bad.blocked_by == "numbers_grounded" and "0.73" not in bad.reply.text


def test_each_reply_gets_the_charts_of_its_own_turn_from_the_tool_result() -> None:
    ctx = canary_context(frame=_states_frame())
    session = _explore(ctx)
    for message, text in (
        ("What is in the file?", "It has customers."),
        ("Churn rate by state", "Here it is."),
    ):
        client = (
            _QuotingClient(text)
            if "rate" in message
            else ToolingClient([{"action": "get_profile", "args": {}}], per_turn=1)
        )
        turn = chat_turn(ctx, session, message, meter=meter_for(client), guardrails=guardrails())
        session = after(session, message, turn)
    charts = charts_of(session)
    assert [c.message_index for c in charts] == [3]  # the second reply; the first used no rate_by
    assert session.transcript[3].role is ChatRole.AGENT
    chart = charts[0]
    stored = next(r for r in session.tool_results if r.tool == "rate_by")
    assert chart.evidence_id == stored.evidence_id == "c2-e1"
    assert (chart.function, chart.column, chart.outcome_column, chart.positive_label) == (
        "positive_rate",
        "state",
        "churned",
        "yes",
    )
    assert [(b.label, b.rows, b.value, b.suppressed) for b in chart.bars] == [
        (g["group"], g["rows"], g["positive_rate"], g["suppressed"]) for g in stored.result["groups"]
    ]


def test_a_refused_rate_by_draws_no_chart() -> None:
    frame = _states_frame()
    frame["contact_email"] = [f"user{i}@example.test" for i in range(len(frame))]
    ctx = canary_context(frame=frame)
    client = ToolingClient([{"action": "rate_by", "args": {"column": "contact_email"}}], per_turn=1)
    turn = chat_turn(ctx, _explore(ctx), "Rate by email", meter=meter_for(client), guardrails=guardrails())
    assert charts_of(after(_explore(ctx), "Rate by email", turn)) == ()
