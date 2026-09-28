"""What Guided setup suggests must survive Approve: every repro here once reached 'ready' and then
failed `apply` (422 or 409), or lost or kept a decision the person had changed."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.advisor import advise, recipe_steps, run_overrides
from engine.agent.checks import check_plan
from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    DecidedBy,
    Proposal,
    ProposalKind,
    ProposalState,
    RecipeStep,
    RecipeStepKind,
)
from engine.agent.recipe import run_recipe
from engine.agent.session import (
    SessionError,
    accept_recommended,
    answer,
    decide,
    roles_of,
    start_session,
)
from engine.agent.tools import AgentContext
from engine.config import SplitType, resolve_config
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.helpers import context_for


def _apply_errors(ctx: AgentContext, session: AgentSession) -> list[str]:
    """What `POST /agent-session/apply` would refuse with: a ConfigError code (422), else the
    unacknowledged error checks (409), exactly as the route runs them."""
    key, target = roles_of(session)
    accepted = [p for p in session.proposals if p.state is ProposalState.ACCEPTED]
    steps = recipe_steps(accepted)
    frame = ctx.frame
    if steps:
        frame = run_recipe(
            ctx.frame,
            steps,
            upload_id=ctx.upload_id,
            primary_key=key,
            target=target,
            levels=ctx.config.agent.levels,
            max_failure_pct=ctx.config.agent.max_conversion_failure_pct,
        ).frame
    try:
        resolved = resolve_config(ctx.use_case_id, run_overrides(session.proposals), root=ctx.config_root)
    except Exception as exc:  # ConfigError -> 422
        return [str(getattr(exc, "code", exc))]
    report = check_plan(
        frame,
        resolved.config,
        mode=ctx.mode,
        primary_key=key,
        target=target,
        upload_id=ctx.upload_id,
        row_count=len(frame),
        schema=ctx.schema,
    )
    return [c.code for c in report.checks if c.severity.value == "error" and not c.acknowledged]


def _answer_all(ctx: AgentContext, session: AgentSession, option: int = 0) -> AgentSession:
    for question in session.questions:
        if question.answer is None:
            session = answer(
                ctx=ctx,
                session=session,
                question_id=question.question_id,
                option_id=question.options[option].option_id,
            )
    return session


def _accept_everything(ctx: AgentContext, session: AgentSession) -> AgentSession:
    pending = [
        (p.proposal_id, ProposalState.ACCEPTED, None)
        for p in session.proposals
        if p.state is ProposalState.PENDING
    ]
    return decide(session, ctx, pending)


def _settings(proposals: Sequence[Proposal]) -> dict[str, Any]:
    return {str(p.path): p.value for p in proposals if p.kind is ProposalKind.SETTING}


# ---------------------------------------------------------------------------
# A split by date is suggested only on a column the use case can split by
# ---------------------------------------------------------------------------
def _with_other_date(use_case_id: str) -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id=use_case_id, rows=3_000))
    if "snapshot_date" in frame.columns:
        return frame.rename(columns={"snapshot_date": "reading_date"})
    days = np.random.default_rng(1).integers(0, 400, len(frame))
    dates = pd.Timestamp("2025-01-01") + pd.to_timedelta(days, unit="D")
    return frame.assign(signup_date=dates.strftime("%Y-%m-%d"))


@pytest.mark.parametrize("use_case_id", ["targeted-advertisement", "telco-churn", "bank-term-deposit"])
def test_a_split_by_date_is_offered_only_on_a_column_the_use_case_can_split_by(use_case_id: str) -> None:
    ctx = context_for(_with_other_date(use_case_id), use_case_id)
    session = start_session(ctx, session_id="s1")
    settings = _settings(session.proposals)
    resolve_config(use_case_id, settings)  # every suggestion together resolves
    assert settings.get("split.type") != SplitType.TIME_BASED.value
    ready = _accept_everything(ctx, _answer_all(ctx, session))
    assert ready.status.value == "ready"
    assert _apply_errors(ctx, ready) == []


def test_editing_a_setting_is_checked_with_the_other_accepted_settings() -> None:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000))
    ctx = context_for(frame.assign(signup_date=frame["snapshot_date"]))
    session = start_session(ctx, session_id="s1")
    split_type = next(p for p in session.proposals if p.path == "split.type")
    column = next(p for p in session.proposals if p.path == "split.time_column")
    session = decide(
        session,
        ctx,
        [
            (split_type.proposal_id, ProposalState.ACCEPTED, None),
            (column.proposal_id, ProposalState.ACCEPTED, None),
        ],
    )
    # 'signup_date' holds dates, but it is not a column this use case can split by.
    with pytest.raises(SessionError) as refused:
        decide(session, ctx, [(column.proposal_id, ProposalState.ACCEPTED, "signup_date")])
    assert refused.value.code == "TEMPLATE_TIME_MISSING"


# ---------------------------------------------------------------------------
# An error no suggestion fixes is never left for Approve to find
# ---------------------------------------------------------------------------
def _fault(variant: str) -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id="fault-prediction", rows=3_000))
    if variant == "renamed":
        return frame.rename(columns={"snapshot_date": "reading_date"})
    broken = frame.copy()
    rows = np.random.default_rng(0).choice(len(frame), len(frame) // 3, replace=False)
    broken["snapshot_date"] = broken["snapshot_date"].astype(str)
    broken.loc[rows, "snapshot_date"] = "not a date"
    return broken


@pytest.mark.parametrize("variant", ["renamed", "unparseable"])
def test_a_use_case_split_by_date_whose_date_column_cannot_be_used_is_split_at_random(variant: str) -> None:
    ctx = context_for(_fault(variant), "fault-prediction")
    session = start_session(ctx, session_id="s1")
    settings = _settings(session.proposals)
    assert settings.get("split.type") == SplitType.RANDOM_STRATIFIED.value
    ready = accept_recommended(ctx=ctx, session=_answer_all(ctx, session))
    rejected = [
        (p.proposal_id, ProposalState.REJECTED, None)
        for p in ready.proposals
        if p.state is ProposalState.PENDING
    ]
    ready = decide(ready, ctx, rejected)
    assert ready.status.value == "ready"
    assert _apply_errors(ctx, ready) == []
    reason = next(p.reason for p in session.proposals if p.path == "split.type")
    assert "no date column" not in reason  # the file has one; it is just not usable


def test_an_error_no_suggestion_can_fix_stops_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The safety net: an error check the advisor neither stops on, asks about nor fixes stops it."""
    from engine.agent import advisor

    monkeypatch.setattr(advisor, "recommend_settings", lambda *args, **kwargs: ())
    ctx = context_for(_fault("renamed"), "fault-prediction")
    advice = advise(ctx)
    assert advice.status.value == "stopped"
    assert advice.stop_reason is not None and "snapshot_date" in advice.stop_reason


# ---------------------------------------------------------------------------
# A column a leak question may hide is never the column a setting depends on
# ---------------------------------------------------------------------------
def _drifting() -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000))
    dates = pd.to_datetime(frame["snapshot_date"])
    late = dates >= sorted(dates.unique())[-max(1, dates.nunique() // 6)]
    return frame.assign(converted_30d=late.astype(int))


def test_the_leak_question_and_the_split_suggestion_never_disagree_about_a_column() -> None:
    ctx = context_for(_drifting())
    session = start_session(ctx, session_id="s1")
    leak = [q for q in session.questions if "almost perfectly predicts" in q.text]
    assert leak and "snapshot_date" in leak[0].text
    assert "snapshot_date" not in _settings(session.proposals).values()
    ready = _accept_everything(ctx, _answer_all(ctx, session, option=0))  # Hide (recommended)
    assert ready.status.value == "ready"
    assert _apply_errors(ctx, ready) == []


# ---------------------------------------------------------------------------
# Re-advice keeps what the person accepted, and drops only what can no longer apply
# ---------------------------------------------------------------------------
def _two_outcomes() -> AgentContext:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000))
    frame = frame.drop(columns=["converted_30d"]).assign(
        bought=frame["converted_30d"], purchased=frame["converted_30d"]
    )
    return context_for(frame)


def _chat_setting(proposal_id: str, path: str, value: Any, evidence: str) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        kind=ProposalKind.SETTING,
        title=f"Set {path} to {value}",
        reason="You asked for it.",
        path=path,
        value=value,
        suggested_value=value,
        evidence_ids=(evidence,),
        confidence=AgentConfidence.CHECK,
    )


def test_re_advice_keeps_an_accepted_suggestion_even_when_a_duplicate_was_rejected() -> None:
    ctx = _two_outcomes()
    session = start_session(ctx, session_id="s1")
    evidence = session.tool_results[0].evidence_id
    chat = (
        _chat_setting("c1-p1", "model_search.strategy", "fast", evidence),
        _chat_setting("c2-p1", "model_search.strategy", "fast", evidence),
    )
    session = session.model_copy(update={"proposals": (*session.proposals, *chat)})
    session = decide(
        session, ctx, [("c1-p1", ProposalState.ACCEPTED, None), ("c2-p1", ProposalState.REJECTED, None)]
    )
    question = next(q for q in session.questions if "Which column" in q.text)
    bought = next(o for o in question.options if o.label == "bought")
    session = answer(session, ctx, question.question_id, bought.option_id)
    assert run_overrides(session.proposals).get("model_search.strategy") == "fast"
    assert any(
        p.path == "model_search.strategy" and p.state is ProposalState.ACCEPTED for p in session.proposals
    )


def test_changing_an_answer_withdraws_the_previous_answers_suggestion() -> None:
    ctx = context_for(messy_frame())
    session = start_session(ctx, session_id="s1")
    question = next(q for q in session.questions if "almost perfectly predicts" in q.text)
    session = answer(session, ctx, question.question_id, "hide")
    session = answer(session, ctx, question.question_id, "keep")
    answered = next(q for q in session.questions if q.question_id == question.question_id)
    assert answered.answer == "keep"
    accepted = [p for p in session.proposals if p.state is ProposalState.ACCEPTED]
    assert not any(
        p.step is not None and p.step.kind is RecipeStepKind.DROP_COLUMN
        for p in accepted
        if p.step and p.step.column == "campaign_result_score"
    )
    assert not any(
        t.startswith("Hide 'campaign_result_score'") for t in session.summary.decisions if session.summary
    )
    assert any(p.kind is ProposalKind.ACKNOWLEDGEMENT for p in accepted)


def test_a_hidden_column_chosen_as_the_outcome_is_no_longer_hidden() -> None:
    ctx = _two_outcomes()
    session = start_session(ctx, session_id="s1")
    evidence = session.tool_results[0].evidence_id
    hide = Proposal(
        proposal_id="u-p1",
        kind=ProposalKind.RECIPE_STEP,
        title="Hide 'bought'",
        reason="You chose to hide it.",
        step=RecipeStep(order=1, kind=RecipeStepKind.DROP_COLUMN, column="bought", params={}),
        evidence_ids=(evidence,),
        confidence=AgentConfidence.SURE,
        state=ProposalState.ACCEPTED,
        decided_by=DecidedBy.USER,
    )
    session = session.model_copy(update={"proposals": (*session.proposals, hide)})
    question = next(q for q in session.questions if "Which column" in q.text)
    bought = next(o for o in question.options if o.label == "bought")
    session = answer(session, ctx, question.question_id, bought.option_id)
    assert roles_of(session)[1] == "bought"
    assert not any(
        p.state is ProposalState.ACCEPTED and p.step is not None and p.step.column == "bought"
        for p in session.proposals
    )
    ready = _accept_everything(ctx, _answer_all(ctx, session))
    assert _apply_errors(ctx, ready) == []


def test_two_accepted_suggestions_for_one_setting_leave_one_in_the_summary() -> None:
    ctx = context_for(messy_frame())
    session = start_session(ctx, session_id="s1")
    evidence = session.tool_results[0].evidence_id
    chat = (
        _chat_setting("c1-p1", "model_search.strategy", "fast", evidence),
        _chat_setting("c2-p1", "model_search.strategy", "exhaustive", evidence),
    )
    session = session.model_copy(update={"proposals": (*session.proposals, *chat)})
    session = decide(session, ctx, [("c1-p1", ProposalState.ACCEPTED, None)])
    session = decide(session, ctx, [("c2-p1", ProposalState.ACCEPTED, None)])
    states = {p.proposal_id: p.state for p in session.proposals if p.proposal_id in {"c1-p1", "c2-p1"}}
    assert states == {"c1-p1": ProposalState.REJECTED, "c2-p1": ProposalState.ACCEPTED}
    assert run_overrides(session.proposals)["model_search.strategy"] == "exhaustive"
    assert session.summary is not None
    titles = [t for t in session.summary.decisions if t.startswith("Set model_search.strategy")]
    assert titles == ["Set model_search.strategy to exhaustive"]
    # Both accepted at once (Preview sends every ticked box): the later suggestion wins, no 422.
    both = decide(
        session,
        ctx,
        [("c2-p1", ProposalState.ACCEPTED, None), ("c1-p1", ProposalState.ACCEPTED, None)],
    )
    states = {p.proposal_id: p.state for p in both.proposals if p.proposal_id in {"c1-p1", "c2-p1"}}
    assert states == {"c1-p1": ProposalState.REJECTED, "c2-p1": ProposalState.ACCEPTED}
    assert run_overrides(both.proposals)["model_search.strategy"] == "exhaustive"


def test_accepting_the_recommended_ones_never_accepts_two_values_for_one_setting() -> None:
    ctx = context_for(messy_frame())
    ticking = ctx.config.model_copy(
        update={"agent": ctx.config.agent.model_copy(update={"tick_uncertain": True})}
    )
    ctx = replace(ctx, config=ticking)
    session = start_session(ctx, session_id="s1")
    evidence = session.tool_results[0].evidence_id
    chat = (
        _chat_setting("c1-p1", "model_search.strategy", "fast", evidence),
        _chat_setting("c2-p1", "model_search.strategy", "exhaustive", evidence),
    )
    session = session.model_copy(update={"proposals": (*session.proposals, *chat)})
    session = accept_recommended(session, ctx)
    states = {p.proposal_id: p.state for p in session.proposals if p.proposal_id in {"c1-p1", "c2-p1"}}
    assert states == {"c1-p1": ProposalState.PENDING, "c2-p1": ProposalState.ACCEPTED}


def _helper_and_chat_minutes() -> tuple[AgentContext, AgentSession]:
    """The helper suggests a 10-minute search (sure, so ticked); the person asks in chat for 30."""
    ctx = context_for(generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000)))
    session = start_session(ctx, session_id="s1")
    helper = next(p for p in session.proposals if p.path == "model_search.time_limit_minutes")
    assert (helper.value, helper.confidence) == (10, AgentConfidence.SURE)
    evidence = session.tool_results[0].evidence_id
    chat = _chat_setting("c1-p1", "model_search.time_limit_minutes", 30, evidence)
    return ctx, session.model_copy(update={"proposals": (*session.proposals, chat)})


def test_accepting_the_recommended_ones_never_overrides_a_value_the_person_accepted() -> None:
    ctx, session = _helper_and_chat_minutes()
    session = decide(session, ctx, [("c1-p1", ProposalState.ACCEPTED, None)])
    session = accept_recommended(session, ctx)
    assert run_overrides(session.proposals)["model_search.time_limit_minutes"] == 30
    minutes = {p.proposal_id: p for p in session.proposals if p.path == "model_search.time_limit_minutes"}
    assert minutes["c1-p1"].state is ProposalState.ACCEPTED
    assert minutes["c1-p1"].decided_by is DecidedBy.USER
    # The helper's own suggestion is left for the person: nothing is decided on their behalf.
    helper = next(p for pid, p in minutes.items() if pid != "c1-p1")
    assert (helper.state, helper.decided_by) == (ProposalState.PENDING, None)


def test_preview_with_a_chat_request_and_the_helpers_ticked_suggestion_keeps_the_chat_request() -> None:
    """Preview sends every box as it stands: the helper's 10 minutes (ticked by default) and the
    person's own 30. The later suggestion - the chat request - wins, and the session is ready."""
    ctx, session = _helper_and_chat_minutes()
    session = _answer_all(ctx, session)
    ticked = {p.proposal_id for p in session.proposals if p.confidence is AgentConfidence.SURE} | {"c1-p1"}
    boxes = [
        (p.proposal_id, ProposalState.ACCEPTED if p.proposal_id in ticked else ProposalState.REJECTED, None)
        for p in session.proposals
        if p.state is ProposalState.PENDING
    ]
    ready = decide(session, ctx, boxes)
    assert ready.status.value == "ready"
    assert run_overrides(ready.proposals)["model_search.time_limit_minutes"] == 30
    accepted = [
        p.proposal_id
        for p in ready.proposals
        if p.path == "model_search.time_limit_minutes" and p.state is ProposalState.ACCEPTED
    ]
    assert accepted == ["c1-p1"]
    assert _apply_errors(ctx, ready) == []


# ---------------------------------------------------------------------------
# A split by date is decided with its date column
# ---------------------------------------------------------------------------
def _split_pair() -> tuple[AgentContext, AgentSession, Proposal, Proposal]:
    ctx = context_for(generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000)))
    session = _answer_all(ctx, start_session(ctx, session_id="s1"))
    split_type = next(p for p in session.proposals if p.path == "split.type")
    column = next(p for p in session.proposals if p.path == "split.time_column")
    assert split_type.value == SplitType.TIME_BASED.value and column.value == "snapshot_date"
    return ctx, session, split_type, column


def _rest(session: AgentSession, *skip: Proposal) -> list[tuple[str, ProposalState, Any]]:
    ids = {p.proposal_id for p in skip}
    return [
        (p.proposal_id, ProposalState.ACCEPTED, None)
        for p in session.proposals
        if p.state is ProposalState.PENDING and p.proposal_id not in ids
    ]


def test_accepting_a_split_by_date_without_its_column_is_refused_before_approve() -> None:
    ctx, session, split_type, column = _split_pair()
    decisions = [
        *_rest(session, split_type, column),
        (split_type.proposal_id, ProposalState.ACCEPTED, None),
        (column.proposal_id, ProposalState.REJECTED, None),
    ]
    with pytest.raises(SessionError) as refused:
        decide(session, ctx, decisions)
    assert refused.value.code == "TIME_COLUMN_MISSING"
    assert column.title in refused.value.message and split_type.title in refused.value.message
    # One at a time is fine while the other half is still to be decided...
    half = decide(session, ctx, [(split_type.proposal_id, ProposalState.ACCEPTED, None)])
    assert half.status.value == "needs_review"
    # ...but rejecting the column afterwards is refused all the same.
    with pytest.raises(SessionError):
        decide(half, ctx, [(column.proposal_id, ProposalState.REJECTED, None)])


@pytest.mark.parametrize("state", [ProposalState.ACCEPTED, ProposalState.REJECTED])
def test_a_split_by_date_decided_with_its_column_is_approved(state: ProposalState) -> None:
    ctx, session, split_type, column = _split_pair()
    decisions = [
        *_rest(session, split_type, column),
        (split_type.proposal_id, state, None),
        (column.proposal_id, state, None),
    ]
    ready = decide(session, ctx, decisions)
    assert ready.status.value == "ready"
    assert _apply_errors(ctx, ready) == []


def test_accepting_only_the_date_column_keeps_the_random_split_and_is_approved() -> None:
    ctx, session, split_type, column = _split_pair()
    decisions = [
        *_rest(session, split_type, column),
        (split_type.proposal_id, ProposalState.REJECTED, None),
        (column.proposal_id, ProposalState.ACCEPTED, None),
    ]
    ready = decide(session, ctx, decisions)
    assert ready.status.value == "ready"
    assert _apply_errors(ctx, ready) == []


def test_rejecting_the_random_split_a_file_without_the_date_column_needs_is_refused() -> None:
    ctx = context_for(_fault("renamed"), "fault-prediction")
    session = _answer_all(ctx, start_session(ctx, session_id="s1"))
    random_split = next(p for p in session.proposals if p.path == "split.type")
    with pytest.raises(SessionError) as refused:
        decide(session, ctx, [(random_split.proposal_id, ProposalState.REJECTED, None)])
    assert refused.value.code == "TIME_COLUMN_MISSING"
    assert "snapshot_date" in refused.value.message and random_split.title in refused.value.message


# ---------------------------------------------------------------------------
# The random-split reason says what is wrong with the date column
# ---------------------------------------------------------------------------
def test_a_date_column_with_too_few_dates_is_not_called_unreadable() -> None:
    frame = generate(GenerationSpec(use_case_id="fault-prediction", rows=3_000))
    frame["snapshot_date"] = np.where(np.arange(len(frame)) % 2 == 0, "2026-08-01", "2026-08-02")
    ctx = context_for(frame, "fault-prediction")
    session = start_session(ctx, session_id="s1")
    split = next(p for p in session.proposals if p.path == "split.type")
    assert split.value == SplitType.RANDOM_STRATIFIED.value
    assert "fewer than three different dates" in split.reason
    assert "read" not in split.reason
    # Its values are dates, so the Run button's date check passes: the column is not cleared.
    assert not any(p.path == "split.time_column" for p in session.proposals)
    ready = accept_recommended(_answer_all(ctx, session), ctx)
    assert ready.status.value == "ready"
    assert _apply_errors(ctx, ready) == []


def test_an_unreadable_date_column_is_named_as_unreadable_and_cleared() -> None:
    ctx = context_for(_fault("unparseable"), "fault-prediction")
    session = start_session(ctx, session_id="s1")
    split = next(p for p in session.proposals if p.path == "split.type")
    assert "cannot be read as dates" in split.reason
    cleared = next(p for p in session.proposals if p.path == "split.time_column")
    assert cleared.value is None
    check = next(r for r in session.tool_results if r.tool == "check_data")
    assert check.evidence_id in cleared.evidence_ids  # the check that could not read it is cited
