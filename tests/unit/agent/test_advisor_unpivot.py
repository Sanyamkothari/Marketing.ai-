"""Plan L M156: the advisor asks to turn a wide sheet into rows, and the answer re-advises (DEC-1563 … DEC-1565)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from engine.agent.advisor import advise, recipe_steps
from engine.agent.config import AgentLevel
from engine.agent.contracts import ProposalState, RecipeStepKind, SessionStatus
from engine.agent.recipe import run_recipe
from engine.agent.session import accept_recommended, answer, start_session, unpivot_declined
from engine.agent.tools import AgentContext
from engine.config import RunMode
from tests.fixtures.agent_bench.make_wide import wide_frame
from tests.unit.agent.helpers import context_for

USE_CASE = "retail-win-back"
KEY, TARGET = "customer_id", "reactivated_90d"
QUESTION = "Your sheet has one column per month for spend and orders. Turn them into rows (one row per shopper per month)?"


@pytest.fixture(scope="module")
def ctx() -> AgentContext:
    return context_for(wide_frame(), USE_CASE)


def _unpivot_question(advice: Any) -> Any:
    return next(q for q in advice.questions if any(o.option_id == "unpivot" for o in q.options))


def test_a_wide_sheet_is_a_blocking_question(ctx: AgentContext) -> None:
    advice = advise(ctx)
    assert advice.status is SessionStatus.NEEDS_REVIEW and advice.stop_reason is None
    question = _unpivot_question(advice)
    assert question.blocking and question.text == QUESTION
    assert [o.label for o in question.options] == ["Turn into rows (recommended)", "Keep as columns"]
    proposal = question.options[0].proposal
    assert proposal is not None and proposal.step is not None
    step = proposal.step
    assert step.kind is RecipeStepKind.UNPIVOT and step.column == KEY
    assert step.params["outcome"] == TARGET and TARGET not in step.params["id_columns"]
    assert step.params["id_columns"] == [KEY, "country"]
    assert step.params["period_column"] == "month"
    assert len(step.params["value_columns"]) == 12
    evidence = {r.evidence_id: r for r in advice.tool_results}
    assert evidence[proposal.evidence_ids[0]].tool == "get_profile"
    # Nothing is checked, and no column about to become rows gets a fix of its own.
    assert not any(r.tool == "check_data" for r in advice.tool_results)
    assert not any(p.step is not None and p.step.column.startswith("2024_") for p in advice.proposals)


def test_no_question_without_reshape_or_on_a_scoring_file(ctx: AgentContext) -> None:
    agent = ctx.config.agent.model_copy(update={"levels": (AgentLevel.CLEAN, AgentLevel.DERIVE)})
    clean = advise(replace(ctx, config=ctx.config.model_copy(update={"agent": agent})))
    assert not any(o.option_id == "unpivot" for q in clean.questions for o in q.options)
    assert clean.stop_reason is None  # a wide sheet is one row per shopper: it still trains as it is
    scoring = advise(context_for(wide_frame(scoring=True), USE_CASE, mode=RunMode.SCORE))
    assert scoring.questions == ()  # a scoring file is prepared by the model's own recipe


def test_months_without_a_year_are_noted_not_asked() -> None:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun")
    advice = advise(context_for(wide_frame(months=months), USE_CASE))
    assert not any(o.option_id == "unpivot" for q in advice.questions for o in q.options)
    (note,) = [line for line in advice.assumptions if "give no year" in line]
    assert "'Jan_spend'" in note and "'2024_01_spend'" in note
    assert advice.stop_reason is None  # the sheet is still one row per shopper, used as it is
    last = advise(context_for(wide_frame(months=months, period_first=False), USE_CASE))
    assert any("'spend_2024_01'" in line for line in last.assumptions)


def test_month_last_names_are_asked_the_same_way() -> None:
    advice = advise(context_for(wide_frame(period_first=False), USE_CASE))
    step = _unpivot_question(advice).options[0].proposal.step
    assert step.params["name_pattern"] == "{measure}_{period}"


def test_keep_as_columns_is_remembered_and_the_sheet_is_advised_as_it_is(ctx: AgentContext) -> None:
    session = start_session(ctx, session_id="s1")
    question = _unpivot_question(session)
    kept = answer(session, ctx, question.question_id, "keep")
    assert unpivot_declined(kept)
    assert kept.stop_reason is None
    assert [q.question_id for q in kept.questions if q.answer is None] == []
    assert not any(p.step is not None and p.step.kind is RecipeStepKind.UNPIVOT for p in kept.proposals)
    assert any(r.tool == "check_data" for r in kept.tool_results)  # advised on the wide sheet now


def test_turn_into_rows_then_combine_reaches_ready(ctx: AgentContext) -> None:
    session = start_session(ctx, session_id="s1")
    turned = answer(session, ctx, _unpivot_question(session).question_id, "unpivot")
    assert not unpivot_declined(turned)
    assert turned.stop_reason is None and turned.rounds == 2
    (combine_question,) = [q for q in turned.questions if q.answer is None]
    assert (
        combine_question.text
        == "Each shopper appears on 6 rows on average. Combine them into one row per shopper?"
    )
    combine: Any = combine_question.options[0].proposal.step  # type: ignore[union-attr]
    assert combine.params["time_column"] == "month" and combine.params["outcome"] == TARGET
    features = {f["name"]: f for f in combine.params["features"]}
    assert features["country"] == {"name": "country", "function": "latest", "column": "country"}  # carried
    assert {"spend_sum", "spend_latest", "orders_mean"} <= set(features)
    combined = answer(turned, ctx, combine_question.question_id, "combine")
    assert combined.stop_reason is None
    decided = accept_recommended(combined, ctx)
    assert decided.status is SessionStatus.READY
    steps = recipe_steps(p for p in decided.proposals if p.state is ProposalState.ACCEPTED)
    assert [s.kind for s in steps] == [RecipeStepKind.UNPIVOT, RecipeStepKind.COMBINE_ROWS]
    assert decided.summary.intended_actions[:2] == (
        "Turn the columns that hold one period each into rows, one per shopper per 'month'.",
        "Combine those rows into one row per shopper by 'customer_id', using only rows dated on or before each "
        "shopper's snapshot, and train on those.",
    )
    # The recipe runs on the wide sheet as it was sent: one row per shopper again, the outcome as it was.
    out = run_recipe(
        ctx.frame,
        steps,
        upload_id="u",
        primary_key=KEY,
        target=TARGET,
        levels=ctx.config.agent.levels,
        max_failure_pct=ctx.config.agent.max_conversion_failure_pct,
        leak_check="full",
    ).frame
    assert len(out) == len(ctx.frame) and out[KEY].is_unique
    original = ctx.frame.set_index(KEY)[TARGET]
    assert (out.set_index(KEY)[TARGET] == original.loc[out[KEY]]).all()
    spend = [c for c in ctx.frame.columns if c.endswith("_spend")]
    assert (
        out.set_index(KEY)["spend_sum"]
        .round(2)
        .equals(
            ctx.frame.set_index(KEY)[spend]
            .sum(axis=1, min_count=1)
            .round(2)
            .loc[out[KEY]]
            .rename("spend_sum")
        )
    )


def test_answering_again_withdraws_the_step(ctx: AgentContext) -> None:
    session = start_session(ctx, session_id="s1")
    question_id = _unpivot_question(session).question_id
    turned = answer(session, ctx, question_id, "unpivot")
    back = answer(turned, ctx, question_id, "keep")
    assert not any(
        p.step is not None and p.step.kind is RecipeStepKind.UNPIVOT and p.state is ProposalState.ACCEPTED
        for p in back.proposals
    )
    assert unpivot_declined(back)
