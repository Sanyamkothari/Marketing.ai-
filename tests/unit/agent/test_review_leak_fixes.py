"""The fixes for the leak review (findings 1-7, 12), tested on their own terms.

The reviewers' repro tests (`test_review_leak_<n>.py`, `test_review_bypass_<n>.py`) prove that each hole is
closed through the real `chat_turn`. These tests pin the design that closes them: a suggestion carries the
cells it quotes (`Proposal.examples`) and the model's copy is built from them, the person's screen keeps the
full text, an old session still loads, a tool result has no dict keyed by cells, and the search tools are
not oracles.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest

from engine.agent import egress
from engine.agent.config import DataAccess
from engine.agent.contracts import AgentSession, Proposal
from engine.agent.egress import HIDDEN_BY_SETTINGS, Egress
from engine.agent.formats import find_format_issues, merge_from_pairs
from engine.agent.loop import (
    MAX_FIND_VALUES_PER_COLUMN_SESSION,
    MAX_FIND_VALUES_PER_COLUMN_TURN,
    _state,
    chat_turn,
)
from engine.agent.session import start_session
from engine.agent.tools import AgentToolError, call_tool
from engine.agent.untrusted import quoted
from tests.unit.agent.egress_canary import (
    ToolingClient,
    canary_context,
    guardrails,
    meter_for,
    run_session,
    stored_text,
)
from tests.unit.agent.helpers import context_for, synthetic


def _gate(*, mode: DataAccess = DataAccess.MASKED_DATA, hide: tuple[str, ...] = ()) -> Egress:
    return Egress.build(["shop_type", "region", "notes"], mode=mode, always_hide=hide)


# ---------------------------------------------------------------------------
# (a) and (b): a suggestion is hidden, or shaped, as a whole - from the cells it carries
# ---------------------------------------------------------------------------
def test_a_suggestion_about_a_hidden_column_hides_the_examples_in_every_sentence() -> None:
    gate = _gate(hide=("shop_type",))
    title = "Merge different spellings in 'shop_type'"
    reason = f"The same value is written differently ({quoted('JANE ROE')} → {quoted('Jane Roe')})."
    shown_title, shown_reason = gate.sentences([title, reason], ["JANE ROE", "Jane Roe"])
    assert shown_title == title  # the column's name is not a value
    assert "JANE" not in shown_reason and "Roe" not in shown_reason
    assert shown_reason.count(HIDDEN_BY_SETTINGS) == 2


def test_the_step_column_alone_hides_the_examples_when_no_sentence_names_it() -> None:
    gate = _gate(hide=("shop_type",))
    (reason,) = gate.sentences(
        [f"It is spelled two ways ({quoted('Kids')})."], ["Kids"], columns=["shop_type"]
    )
    assert "Kids" not in reason and HIDDEN_BY_SETTINGS in reason


@pytest.mark.parametrize("cell", ["O'Brien", "Men's Wear", "Boys' Wear", "'Quoted' inside", "a'"])
def test_summaries_only_shapes_a_cell_however_it_is_quoted(cell: str) -> None:
    """The cell is found by value, so an apostrophe in it cannot hide it from the gate."""
    gate = _gate(mode=DataAccess.SUMMARIES_ONLY)
    (shown,) = gate.sentences([f"It is written two ways ({quoted(cell)}, {quoted('kids')})."], [cell, "kids"])
    for word in ("Brien", "Men", "Wear", "Boys", "Quoted", "inside", "kids"):
        assert word not in shown, shown


def test_masked_data_keeps_a_visible_example_but_masks_a_secret_in_it() -> None:
    gate = _gate()
    (shown,) = gate.sentences(
        [f"It is written two ways ({quoted('Kids')}, {quoted('call +91 98765 43210')})."],
        ["Kids", "call +91 98765 43210"],
    )
    assert "'Kids'" in shown and "98765" not in shown


def test_the_gate_still_reads_quotes_in_a_sentence_without_examples() -> None:
    """A session saved before `examples` existed has none: the quoted pieces are read as before."""
    gate = _gate(mode=DataAccess.SUMMARIES_ONLY)
    assert "Roe" not in gate.text(f"({quoted('Jane O' + chr(39) + 'Roe')})", examples=True)
    hidden = _gate(hide=("shop_type",))
    assert "JANE" not in hidden.sentences(["Merge in 'shop_type'", "(as 'JANE ROE')"], [])[1]


def test_an_old_stored_suggestion_without_examples_still_loads() -> None:
    proposal = Proposal.model_validate(
        {
            "proposal_id": "p1",
            "kind": "setting",
            "title": "t",
            "reason": "r",
            "path": "a.b",
            "evidence_ids": ["e1"],
            "confidence": "sure",
        }
    )
    assert proposal.examples == ()


def _spelling_frame() -> pd.DataFrame:
    frame = synthetic("clean", rows=400)
    frame["shop_type"] = ["Men's Wear", "MEN'S WEAR", "Kids", "kids"] * 100
    return frame


def test_the_advisor_records_the_cells_it_quotes_and_the_screen_keeps_them() -> None:
    ctx = canary_context(mode="summaries_only", frame=_spelling_frame())
    session = start_session(ctx, session_id="fix-1")
    merge = next(p for p in session.proposals if "shop_type" in p.title and p.step is not None)
    assert "Men's Wear" in merge.reason, "the person's own screen shows the full examples"
    assert {"Men's Wear", "MEN'S WEAR"} <= set(merge.examples)
    state = _state(session, Egress.build(list(ctx.frame.columns), mode=DataAccess.SUMMARIES_ONLY))
    sent = json.dumps(state)
    assert "Men" not in sent and "Wear" not in sent
    assert "Men's Wear" in merge.reason  # nothing was changed in the session itself


def test_a_question_quotes_its_examples_and_they_are_masked_for_the_model() -> None:
    frame = synthetic("clean", rows=300)
    frame["joined"] = ["03/04/1985", "12/05/1990", "01/02/1980", "05/06/1979"] * 75
    ctx = canary_context(mode="masked_data", frame=frame, always_hide=["joined"])
    session = start_session(ctx, session_id="fix-q")
    question = next(q for q in session.questions if "joined" in q.text)
    assert question.examples and "03/04/1985" in question.text
    state = _state(session, Egress.build(list(frame.columns), always_hide=["joined"]))
    assert "1985" not in json.dumps(state) and "1990" not in json.dumps(state)


# ---------------------------------------------------------------------------
# (c): no dict keyed by cells
# ---------------------------------------------------------------------------
def _variants_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"account_holder": ["zelda", "Zelda", "Zelda", "quill roe", "Quill Roe", "Quill Roe"] * 50}
    )


def test_format_issues_list_their_merge_as_pairs_and_the_step_keeps_a_dict() -> None:
    ctx = context_for(_variants_frame())
    result = call_tool(ctx, "find_format_issues", {}, evidence_id="e1").result
    merge = result["issues"][0]["params"]["merge"]
    assert isinstance(merge, list) and {"from", "to"} == set(merge[0])
    assert merge_from_pairs(merge) == {"zelda": "Zelda", "quill roe": "Quill Roe"}
    issue = find_format_issues(_variants_frame())[0]
    assert issue.params["merge"] == {"zelda": "Zelda", "quill roe": "Quill Roe"}
    session = start_session(ctx, session_id="fix-c")
    step = next(p.step for p in session.proposals if p.step is not None)
    assert step is not None and step.params["merge"] == {"zelda": "Zelda", "quill roe": "Quill Roe"}


@pytest.mark.parametrize(
    ("mode", "hide"),
    [(DataAccess.SUMMARIES_ONLY, ()), (DataAccess.MASKED_DATA, ("account_holder",))],
)
def test_a_dict_key_that_is_not_a_field_name_is_shaped_or_hidden(
    mode: DataAccess, hide: tuple[str, ...]
) -> None:
    gate = Egress.build(["account_holder"], mode=mode, always_hide=hide)
    shown = gate.prepare({"column": "account_holder", "counts": {"zelda": 3, "quill roe": 2}})
    assert "zelda" not in json.dumps(shown) and "quill" not in json.dumps(shown)


def test_a_field_name_of_ours_is_kept_in_every_mode() -> None:
    for mode in DataAccess:
        gate = Egress.build(["account_holder"], mode=mode, always_hide=["account_holder"])
        shown = gate.prepare(
            {"column": "account_holder", "rows": 3, "distinct": 2, "most_rows_for_one_value": 2}
        )
        assert set(shown) == {"column", "rows", "distinct", "most_rows_for_one_value"}
        assert shown["rows"] == 3 and shown["distinct"] == 2


# ---------------------------------------------------------------------------
# (d), (e): the median is a cell; the preview is what was sent
# ---------------------------------------------------------------------------
def test_the_median_is_a_shape_in_summaries_only_and_the_mean_is_kept() -> None:
    gate = Egress.build(["pay"], mode=DataAccess.SUMMARIES_ONLY)
    shown = gate.prepare({"column": "pay", "median": 73890.0, "p50": 73890.0, "mean": 77913.7, "rows": 7})
    assert shown["median"] == shown["p50"] == egress.shape_of("73890.0")
    assert shown["mean"] == 77913.7 and shown["rows"] == 7


def test_a_ten_digit_mean_or_median_is_masked_in_both_modes() -> None:
    for mode in DataAccess:
        gate = Egress.build(["callback_ref"], mode=mode)
        shown = gate.prepare({"column": "callback_ref", "mean": 5553333333.0, "median": 5552223333.0})
        assert "555" not in json.dumps(shown), (mode, shown)


def test_the_sent_preview_is_taken_after_the_last_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate that lets a phone number through: the prompt is masked late, and so is the stored preview."""
    monkeypatch.setattr(Egress, "_number", lambda self, value, key, hidden: value)
    frame = pd.DataFrame(
        {
            "callback_ref": [5551234567.0, 5559876543.0, 5550001111.0, 5552223333.0, 5554445555.0],
            "converted_30d": [1, 0, 0, 1, 0],
        }
    )
    ctx = canary_context(frame=frame)
    client = ToolingClient([{"action": "describe_numbers", "args": {"column": "callback_ref"}}])
    session, turns = run_session(ctx, client)
    assert "EGRESS_LATE_MASK" in turns[0].reply.turn.error_codes if turns[0].reply.turn else False
    assert "5552223333" not in "\n".join(client.sent_text)
    assert "5552223333" not in stored_text(session)
    assert "5559876543" not in stored_text(session)


# ---------------------------------------------------------------------------
# (f): masked whole with the complete scanner set, then cut
# ---------------------------------------------------------------------------
FAKE_KEY = (
    "zk_live_51HxAbCdEf123456789012345"  # secret-scan: allow (a fake key the masking tests plant on purpose)
)


@pytest.mark.parametrize("secret", [FAKE_KEY, "4111 1111 1111 1111", "192.0.2.44"])
def test_a_tool_masks_a_whole_cell_before_it_cuts_it(secret: str) -> None:
    from engine.agent.tools import _masked

    for filler in (30, 43, 50):
        cell = "x" * filler + f" {secret} thanks"
        shown = _masked(cell, 60)
        assert secret[:8] not in shown and "51Hx" not in shown, (filler, shown)


# ---------------------------------------------------------------------------
# (g): counts are not an oracle
# ---------------------------------------------------------------------------
def _hidden_ctx(mode: str = "masked_data") -> Any:
    return canary_context(mode=mode, always_hide=["notes"], rows=300)


def test_find_values_on_a_hidden_column_answers_nothing_about_the_text() -> None:
    result = call_tool(
        _hidden_ctx(), "find_values", {"column": "notes", "contains": "jane"}, evidence_id="e1"
    ).result
    assert (
        result["matches"] == [] and "total_matching_rows" not in result and "distinct_matching" not in result
    )
    assert result["distinct"] > 0 and "hidden by your settings" in result["message"]


def test_find_values_in_summaries_only_reports_nothing_under_five_rows() -> None:
    frame = pd.DataFrame(
        {"code": ["AB-1"] * 2 + ["AB-2"] * 9 + ["ZZ-9"] * 30, "converted_30d": [1, 0] * 20 + [1]}
    )
    ctx = canary_context(mode="summaries_only", frame=frame)
    one = call_tool(ctx, "find_values", {"column": "code", "contains": "AB-1"}, evidence_id="e1").result
    assert one["total_matching_rows"] is None and one["matches"] == []
    many = call_tool(ctx, "find_values", {"column": "code", "contains": "AB-"}, evidence_id="e2").result
    assert many["total_matching_rows"] == 9 and [m["rows"] for m in many["matches"]] == [9]


def test_sample_rows_does_not_pick_rows_by_a_hidden_column() -> None:
    ctx = _hidden_ctx()
    args = {"columns": ["converted_30d"], "where_column": "notes", "equals": "Great service, thanks"}
    result = call_tool(ctx, "sample_rows", args, evidence_id="e1").result
    assert result["rows"] == [] and result["total_matching"] is None
    other = call_tool(ctx, "sample_rows", {**args, "equals": "no such note"}, evidence_id="e2").result
    assert result == other


def test_a_hidden_column_is_personal_for_the_tools_but_still_read_by_the_rules() -> None:
    frame = synthetic("clean", rows=400)
    frame["shop_type"] = ["Jane Roe", "JANE ROE", "Kids", "kids"] * 100
    ctx = canary_context(frame=frame, always_hide=["Shop_Type"])
    counts = call_tool(ctx, "value_counts", {"column": "shop_type"}, evidence_id="e1").result
    assert counts["values"] == [] and "hidden by your settings" in counts["message"]
    issues = call_tool(ctx, "find_format_issues", {}, evidence_id="e2").result["issues"]
    assert any(issue["column"] == "shop_type" for issue in issues), "the advisor still fixes a hidden column"


def test_find_values_is_capped_per_column() -> None:
    ctx = canary_context(rows=300)
    turn = [
        {"action": "find_values", "args": {"column": "monthly_spend", "contains": str(i)}} for i in range(9)
    ]
    client = ToolingClient(turn, per_turn=20)
    session = start_session(ctx, session_id="fix-cap")

    result = chat_turn(ctx, session, "look", meter=meter_for(client), guardrails=guardrails())
    used = [r for r in result.tool_results if r.tool == "find_values"]
    assert len(used) == MAX_FIND_VALUES_PER_COLUMN_TURN
    assert MAX_FIND_VALUES_PER_COLUMN_SESSION > MAX_FIND_VALUES_PER_COLUMN_TURN
    assert result.blocked_by == "AGENT_TOOL_LIMIT"


def test_a_session_cap_counts_the_searches_of_earlier_turns() -> None:
    from engine.agent.loop import _limit_searches

    ctx = canary_context(rows=300)
    session = start_session(ctx, session_id="fix-cap2")
    earlier = [
        call_tool(ctx, "find_values", {"column": "monthly_spend", "contains": str(i)}, evidence_id=f"e{i}")
        for i in range(MAX_FIND_VALUES_PER_COLUMN_SESSION)
    ]
    full = session.model_copy(update={"tool_results": (*session.tool_results, *earlier)})
    with pytest.raises(AgentToolError) as error:
        _limit_searches(ctx, full, [], {"column": "monthly_spend", "contains": "9"})
    assert error.value.code == "AGENT_TOOL_LIMIT"
    _limit_searches(ctx, full, [], {"column": "signup_date", "contains": "9"})  # another column is fine


def test_a_typo_in_always_hide_columns_is_a_warning_in_the_assumptions() -> None:
    ctx = canary_context(always_hide=["notes", "custmer_notes"])
    session: AgentSession = start_session(ctx, session_id="fix-typo")
    warned = [a for a in session.assumptions if "custmer_notes" in a]
    assert len(warned) == 1 and warned[0].startswith("Warning:")
    assert not any("'notes'" in a and "not a column" in a for a in session.assumptions)
