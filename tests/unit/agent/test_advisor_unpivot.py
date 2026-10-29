"""Plan L M156: the advisor asks to turn a wide sheet into rows, and the answer re-advises (DEC-1563 … DEC-1565)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from engine.agent.advisor import advise
from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStepKind, SessionStatus
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
