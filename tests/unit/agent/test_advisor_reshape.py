"""The advisor and the session at level 3 (Plan G M76): ask to combine instead of stopping, when allowed."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from engine.agent.advisor import ROLE_PRIMARY_KEY, advise, recipe_steps, summarise
from engine.agent.config import AgentLevel
from engine.agent.contracts import ProposalKind, ProposalState, RecipeStepKind, SessionStatus
from engine.agent.session import accept_recommended, answer, reshape_of, start_session
from engine.agent.tools import AgentContext
from tests.fixtures.agent_bench.make_multirow import multirow_frame
from tests.unit.agent.helpers import context_for

USE_CASE = "retail-win-back"


@pytest.fixture(scope="module")
def ctx() -> AgentContext:
    return context_for(multirow_frame(), USE_CASE)


def _without_reshape(ctx: AgentContext) -> AgentContext:
    agent = ctx.config.agent.model_copy(update={"levels": (AgentLevel.CLEAN, AgentLevel.DERIVE)})
    return replace(ctx, config=ctx.config.model_copy(update={"agent": agent}))


def _combine_question(advice: Any) -> Any:
    return next(q for q in advice.questions if any(o.option_id == "combine" for o in q.options))


def test_a_repeating_id_is_a_question_when_reshape_is_allowed(ctx: AgentContext) -> None:
    advice = advise(ctx)
    assert advice.status is SessionStatus.NEEDS_REVIEW
    assert advice.stop_reason is None
    question = _combine_question(advice)
    assert question.blocking
    assert (
        question.text == "Each shopper appears on 5.1 rows on average. Combine them into one row per shopper?"
    )
    assert [o.label for o in question.options] == ["Combine (recommended)", "Stop"]
    proposal = question.options[0].proposal
    assert proposal is not None and proposal.step is not None
    assert proposal.step.kind is RecipeStepKind.COMBINE_ROWS
    assert proposal.step.column == "customer_id"
    assert proposal.step.params["snapshot_column"] == "snapshot_date"
    assert proposal.step.params["outcome"] == "reactivated_90d"
    evidence = {r.evidence_id: r for r in advice.tool_results}
    repeats = evidence[proposal.evidence_ids[0]]
    assert repeats.tool == "describe_repeats"
    assert repeats.result["rows_per_id"] == 5.1  # the number in the question is measured evidence
    # Nothing is checked, and no setting suggested, on rows that are about to be combined.
    assert not any(r.tool == "check_data" for r in advice.tool_results)
    assert not any(p.kind is ProposalKind.SETTING for p in advice.proposals)
    key = next(p for p in advice.proposals if p.path == ROLE_PRIMARY_KEY)
    assert key.value == "customer_id" and "combined" in key.reason


def test_it_still_stops_when_reshape_is_not_allowed(ctx: AgentContext) -> None:
    advice = advise(_without_reshape(ctx))
    assert advice.status is SessionStatus.STOPPED
    assert advice.stop_reason is not None and "5.1 rows per shopper" in advice.stop_reason  # PK_NOT_UNIQUE
    assert advice.questions == ()


def test_it_still_stops_without_a_date_column() -> None:
    advice = advise(context_for(multirow_frame(dates=False), USE_CASE))
    assert advice.status is SessionStatus.STOPPED
    assert advice.stop_reason is not None and "date column" in advice.stop_reason
    assert advice.questions == ()


def test_it_stops_when_the_user_declines(ctx: AgentContext) -> None:
    advice = advise(ctx, primary_key="customer_id", target="reactivated_90d", combine_declined=True)
    assert advice.status is SessionStatus.STOPPED
    assert advice.questions == ()


def test_after_combining_the_advice_is_about_the_combined_rows(ctx: AgentContext) -> None:
    step = _combine_question(advise(ctx)).options[0].proposal.step
    advice = advise(ctx, primary_key="customer_id", target="reactivated_90d", combine=step, id_prefix="a2-")
    assert advice.stop_reason is None
    checks = next(r for r in advice.tool_results if r.tool == "check_data")
    assert not any(c["code"] == "PK_NOT_UNIQUE" for c in checks.result["checks"])
    assert not any(c["code"] == "LEAKAGE_SUSPECTED" for c in checks.result["checks"])  # later orders left out
    outcome = next(r for r in advice.tool_results if r.tool == "describe_outcome")
    assert outcome.result["rows"] == 1_500  # one row per shopper
    assert not any(_combine_question_or_none(q) for q in advice.questions)
    assert any(p.path == "model_search.time_limit_minutes" for p in advice.proposals)


def _combine_question_or_none(question: Any) -> bool:
    return any(o.option_id == "combine" for o in question.options)


def test_the_session_combines_on_the_answer_and_approves_on_the_combined_file(ctx: AgentContext) -> None:
    session = start_session(ctx, session_id="s1")
    question = _combine_question(session)
    combined = answer(session, ctx, question.question_id, "combine")
    step, declined = reshape_of(combined)
    assert step is not None and not declined
    assert combined.rounds == 2
    assert combined.stop_reason is None
    decided = accept_recommended(combined, ctx)
    pending = [p for p in decided.proposals if p.state is ProposalState.PENDING]
    assert pending == []
    assert decided.status is SessionStatus.READY
    steps = recipe_steps(p for p in decided.proposals if p.state is ProposalState.ACCEPTED)
    assert [s.kind for s in steps] == [RecipeStepKind.COMBINE_ROWS]
    summary = summarise(ctx, decided.proposals, assumptions=decided.assumptions, engine_hidden=())
    assert "Combine the rows into one row per shopper" in summary.decisions
    assert any(
        line.startswith("Combine the 7,652 rows into one row per shopper")
        for line in summary.intended_actions
    )


def test_answering_stop_stops_the_session(ctx: AgentContext) -> None:
    session = start_session(ctx, session_id="s1")
    stopped = answer(session, ctx, _combine_question(session).question_id, "stop")
    assert reshape_of(stopped) == (None, True)
    assert stopped.status is SessionStatus.STOPPED
    assert stopped.stop_reason is not None
