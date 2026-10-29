"""The chat suggests a data fix (Plan L M152, DEC-1520): allowed kinds only, grounded, checked, measured."""

from __future__ import annotations

import json
from typing import Any

import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState, RecipeStepKind
from engine.agent.loop import chat_turn
from engine.agent.session import start_session
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.unit.agent.analyst import ANALYST
from tests.unit.agent.helpers import context_for
from tests.unit.agent.test_session_and_loop import ScriptedMeter, _guardrails

LOOK = json.dumps({"action": "value_counts", "args": {"column": "city"}})


def reply(text: str = "Done.") -> str:
    return json.dumps({"action": "reply", "text": text, "evidence_ids": []})


def step(kind: str, column: str = "city", evidence: tuple[str, ...] = ("c1-e1",), **params: Any) -> str:
    return json.dumps(
        {
            "action": "propose_step",
            "args": {
                "kind": kind,
                "column": column,
                "params": params,
                "reason": "Bombay and Mumbai are the same city.",
                "evidence_ids": list(evidence),
            },
        }
    )


@pytest.fixture(scope="module")
def started() -> tuple[Any, Any]:
    frame = messy_frame()
    frame["city"] = [
        "Bombay" if index % 3 == 0 else "Mumbai" if index % 3 == 1 else "Pune" for index in range(len(frame))
    ]
    ctx = context_for(frame)
    return ctx, start_session(ctx, session_id="s1")


def turn(started: tuple[Any, Any], *script: str) -> Any:
    ctx, session = started
    return chat_turn(
        ctx,
        session,
        "Bombay is Mumbai",
        meter=ScriptedMeter(list(script)),
        guardrails=_guardrails(),
        principal=ANALYST,
    )


def test_a_grounded_fix_becomes_a_pending_ai_suggestion_with_its_measured_effect(
    started: tuple[Any, Any],
) -> None:
    result = turn(started, LOOK, step("map_values", mapping={"Bombay": "Mumbai"}), reply())
    assert result.blocked_by is None
    (proposal,) = result.proposals
    assert proposal.source is ProposalSource.AI
    assert proposal.confidence is AgentConfidence.CHECK and proposal.state is ProposalState.PENDING
    assert proposal.step is not None and proposal.step.kind is RecipeStepKind.MAP_VALUES
    assert proposal.title == "Merge spellings in 'city' that mean the same"
    assert "On the first 1000 rows it changes 334 values." in proposal.reason
    assert "c1-e1" in proposal.evidence_ids
    measured = next(r for r in result.tool_results if r.tool == "propose_step")
    assert measured.result["changed"] == 334


def test_a_fix_with_no_look_at_the_column_is_refused(started: tuple[Any, Any]) -> None:
    bare = step("map_values", evidence=(), mapping={"Bombay": "Mumbai"})
    result = turn(started, bare, bare)
    assert result.proposals == () and result.blocked_by == "AGENT_STEP_UNGROUNDED"


def test_a_look_at_another_column_does_not_ground_it(started: tuple[Any, Any]) -> None:
    other = json.dumps({"action": "value_counts", "args": {"column": "region"}})
    attempt = step("map_values", mapping={"Bombay": "Mumbai"})
    result = turn(started, other, attempt, attempt)
    assert result.blocked_by == "AGENT_STEP_UNGROUNDED"


@pytest.mark.parametrize("kind", ["correct_cells", "remove_rows", "combine_rows", "derive", "rm -rf"])
def test_kinds_that_are_not_the_chats_are_refused(started: tuple[Any, Any], kind: str) -> None:
    attempt = step(kind)
    result = turn(started, LOOK, attempt, attempt)
    assert result.proposals == () and result.blocked_by == "AGENT_STEP_NOT_ALLOWED"


def test_the_outcome_is_never_a_fix_target(started: tuple[Any, Any]) -> None:
    look = json.dumps({"action": "value_counts", "args": {"column": "converted_30d"}})
    attempt = step("fill_missing", column="converted_30d", value=0)
    result = turn(started, look, attempt, attempt)
    assert result.proposals == () and result.blocked_by == "RECIPE_STEP_INVALID"


def test_a_fix_that_changes_nothing_is_refused(started: tuple[Any, Any]) -> None:
    attempt = step("map_values", mapping={"Delhi": "New Delhi"})
    result = turn(started, LOOK, attempt, attempt)
    assert result.blocked_by == "AGENT_STEP_NO_EFFECT"


def test_a_fix_that_only_matters_below_the_first_rows_is_judged_on_the_whole_column() -> None:
    frame = messy_frame()
    frame["city"] = ["Mumbai" if index < 1500 else "Bombay" for index in range(len(frame))]
    ctx = context_for(frame)
    late = (ctx, start_session(ctx, session_id="s2"))
    result = turn(late, LOOK, step("map_values", mapping={"Bombay": "Mumbai"}), reply())
    assert result.blocked_by is None
    (proposal,) = result.proposals
    assert "In the whole column it changes 1500 of 3000 rows." in proposal.reason
    measured = next(r for r in result.tool_results if r.tool == "propose_step")
    assert (measured.result["rows"], measured.result["changed"]) == (3000, 1500)
    assert measured.result["warnings"]  # 50% of the column: above O10's warning line, below its refusal
    nothing = step("map_values", mapping={"Delhi": "New Delhi"})
    refused = turn(late, LOOK, nothing, nothing)
    assert refused.blocked_by == "AGENT_STEP_NO_EFFECT"


def test_a_fix_that_changes_most_of_the_column_below_the_first_rows_is_refused_by_o10() -> None:
    frame = messy_frame()
    frame["city"] = ["Mumbai" if index < 1000 else "Bombay" for index in range(len(frame))]
    ctx = context_for(frame)
    late = (ctx, start_session(ctx, session_id="s3"))
    attempt = step("map_values", mapping={"Bombay": "Mumbai"})
    result = turn(late, LOOK, attempt, attempt)  # 2000 of 3000 rows (67%), none of them in the first 1000
    assert result.proposals == () and result.blocked_by == "AI_STEP_CHANGES_TOO_MANY"


def test_a_bad_parameter_is_refused_by_the_recipe_engine(started: tuple[Any, Any]) -> None:
    attempt = step("fill_missing", value="median")
    result = turn(started, LOOK, attempt, attempt)
    assert result.blocked_by == "RECIPE_STEP_INVALID"


def test_the_prompt_teaches_the_action(started: tuple[Any, Any]) -> None:
    ctx, session = started
    meter = ScriptedMeter([reply()])
    chat_turn(ctx, session, "hello", meter=meter, guardrails=_guardrails(), principal=ANALYST)
    system = meter.rendered[0].system
    assert "propose_step" in system and "map_values" in system and "never a mean or median" in system


@pytest.mark.parametrize("params", [{"excel_serial": True}, {"excel_serial": True, "date_1904": True}])
def test_the_excel_day_number_parameters_are_not_the_chats_to_propose(
    started: tuple[Any, Any], params: dict[str, Any]
) -> None:
    """Plan O M240: `excel_serial` is accepted by a recipe, but a model cannot reach it through the chat."""
    look = json.dumps({"action": "value_counts", "args": {"column": "signup_date"}})
    attempt = step("parse_date", column="signup_date", **params)
    result = turn(started, look, attempt, attempt)
    assert result.proposals == () and result.blocked_by == "RECIPE_STEP_INVALID"
