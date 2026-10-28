"""Proposal-level reading shared by the Data Doctor and the Guided session: who the ID and the outcome are,
what a proposal is about, and the session summary with the required-role questions.

Pure functions over an `AgentSession`'s proposals; nothing is stored here and nothing touches data."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

from engine.agent.advisor import ROLE_PRIMARY_KEY, ROLE_TARGET, Advice, advise, summarise
from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    Proposal,
    ProposalKind,
    ProposalState,
    Question,
    QuestionOption,
    SessionStatus,
)
from engine.agent.tools import AgentContext
from engine.agent.untrusted import display_name
from engine.config import RunMode
from engine.utils.time import utc_now

__all__ = [
    "SessionError",
    "identity",
    "required_roles",
    "roles_of",
    "rules_session",
    "session_from_advice",
    "status_of",
    "unsettled_roles",
    "with_summary",
]


class SessionError(Exception):
    """A request the session refuses; `code` is machine-readable, `message` is for the person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def identity(proposal: Proposal) -> tuple[str, ...]:
    """What a proposal is about, so the same suggestion made twice is recognised as one."""
    if proposal.step is not None:
        return (
            proposal.kind.value,
            proposal.step.kind.value,
            proposal.step.column,
            json.dumps(proposal.step.params, sort_keys=True),
        )
    return (proposal.kind.value, str(proposal.path), json.dumps(proposal.suggested_value, sort_keys=True))


def status_of(session: AgentSession) -> SessionStatus:
    """Applied, stopped, needs review, or ready. Ready only when nothing is left to decide AND every role a run
    needs is chosen: a session never says ready while its ID (or, for training, its outcome) is unsettled."""
    if session.status is SessionStatus.APPLIED:
        return SessionStatus.APPLIED
    if session.stop_reason is not None:
        return SessionStatus.STOPPED
    if session.undecided or unsettled_roles(session):
        return SessionStatus.NEEDS_REVIEW
    return SessionStatus.READY


def required_roles(session: AgentSession) -> tuple[str, ...]:
    """The roles a run cannot start without: the ID column always, the outcome too when training.

    `check_plan` refuses a missing ID in both modes (`PK_MISSING`) and a missing outcome in training
    (`TARGET_MISSING`), so Approve would fail on either. An Ask-your-data session runs nothing and needs none.
    """
    if session.explore:
        return ()
    if session.mode == RunMode.TRAIN.value:
        return (ROLE_PRIMARY_KEY, ROLE_TARGET)
    return (ROLE_PRIMARY_KEY,)


def unsettled_roles(session: AgentSession) -> tuple[str, ...]:
    """The required roles no accepted proposal settles (a suggestion still pending, rejected, or none at all)."""
    return tuple(
        path
        for path in required_roles(session)
        if not any(
            p.kind is ProposalKind.ROLE and p.path == path and p.state is ProposalState.ACCEPTED
            for p in session.proposals
        )
    )


def roles_of(session: AgentSession) -> tuple[str | None, str | None]:
    """(primary key, target) the user accepted, else the ones proposed."""

    def pick(path: str) -> str | None:
        chosen = [p for p in session.proposals if p.kind is ProposalKind.ROLE and p.path == path]
        accepted = [p for p in chosen if p.state is ProposalState.ACCEPTED]
        live = accepted or [p for p in chosen if p.state is ProposalState.PENDING]
        return str(live[-1].value) if live else None

    return pick(ROLE_PRIMARY_KEY), pick(ROLE_TARGET)


def with_summary(
    session: AgentSession, ctx: AgentContext, *, clock: Callable[[], datetime] = utc_now
) -> AgentSession:
    """The session with its status and pre-Approve summary brought up to date."""
    summary = summarise(
        ctx, session.proposals, assumptions=session.assumptions, engine_hidden=session.engine_hidden
    )
    updated = _ask_for_unsettled_roles(session, ctx).model_copy(
        update={"summary": summary, "updated_at": clock()}
    )
    return updated.model_copy(update={"status": status_of(updated)})


_MAX_ROLE_OPTIONS = 4


def _asks_role(question: Question, path: str) -> bool:
    """A question whose answers choose the column for `path` (the advisor's, or `_ask_for_unsettled_roles`')."""
    return any(
        option.proposal is not None
        and option.proposal.kind is ProposalKind.ROLE
        and option.proposal.path == path
        for option in question.options
    )


def _role_choices(session: AgentSession, ctx: AgentContext, path: str) -> list[str]:
    """The columns to offer for `path`, likeliest first; a column already rejected for it comes last.

    The ID: the columns the profile found unique and complete, those holding personal data after the rest. The
    outcome: the two-valued columns that hold no personal data. Never the column the other role already uses. When fewer than two are likely, other columns
    fill in: the answer re-runs the advisor with that column, whose own checks then say why it cannot be used.
    """
    other_path = ROLE_TARGET if path == ROLE_PRIMARY_KEY else ROLE_PRIMARY_KEY
    taken = {
        str(p.value)
        for p in session.proposals
        if p.kind is ProposalKind.ROLE and p.path == other_path and p.state is ProposalState.ACCEPTED
    }
    rejected = {
        str(p.value)
        for p in session.proposals
        if p.kind is ProposalKind.ROLE and p.path == path and p.state is ProposalState.REJECTED
    }
    present = {str(c) for c in ctx.frame.columns}
    columns = [c for c in ctx.profile.columns if c.name in present and c.name not in taken]
    if path == ROLE_PRIMARY_KEY:
        unique = set(ctx.profile.primary_key_candidates)
        # A phone or an email can identify a row, but a column holding no personal data is offered first.
        likely = [c.name for c in columns if c.name in unique and not c.pii_kinds]
        likely += [c.name for c in columns if c.name in unique and c.pii_kinds]
    else:
        likely = [c.name for c in columns if c.distinct_count == 2 and not c.pii_kinds]
    ordered = [c for c in likely if c not in rejected] + [c for c in likely if c in rejected]
    if len(ordered) < 2:
        ordered += [c.name for c in columns if c.name not in ordered and not c.pii_kinds]
    return ordered[:_MAX_ROLE_OPTIONS]


def _role_evidence(session: AgentSession, path: str) -> tuple[str, ...]:
    """What a role question rests on: the evidence the helper's own suggestions for `path` cited, else the
    session's latest profile and role reading."""
    known = {r.evidence_id for r in session.tool_results}
    cited = [
        e
        for p in session.proposals
        if p.kind is ProposalKind.ROLE and p.path == path
        for e in p.evidence_ids
        if e in known
    ]
    if not cited:
        cited = [r.evidence_id for r in session.tool_results if r.tool in ("get_profile", "find_roles")][-2:]
    return tuple(dict.fromkeys(cited))


def _role_question(session: AgentSession, ctx: AgentContext, path: str) -> Question | None:
    """A blocking question asking which column is the ID or the outcome; None when the file offers no choice."""
    choices = _role_choices(session, ctx, path)
    evidence = _role_evidence(session, path)
    if len(choices) < 2 or not evidence:
        return (
            None  # a question needs two answers and its evidence; `status_of` keeps the session out of ready
        )
    taken = {q.question_id for q in session.questions}
    question_id = f"a{session.rounds}-role-{'id' if path == ROLE_PRIMARY_KEY else 'outcome'}"
    while question_id in taken:
        question_id += "x"
    if path == ROLE_PRIMARY_KEY:
        text = f"Which column identifies each {ctx.config.entity}? The run needs one."
        as_what = "the ID column"
    else:
        definition = ctx.config.target.definition or "the outcome"
        text = f"Which column says whether {definition.lower()}? The run needs one to learn from."
        as_what = "the outcome"
    options = tuple(
        QuestionOption(
            option_id=f"use-{number}",
            label=display_name(column),
            effect=f"Use '{display_name(column)}'.",
            proposal=Proposal(
                proposal_id=f"{question_id}-p{number}",
                kind=ProposalKind.ROLE,
                title=f"Use '{display_name(column)}' as {as_what}",
                reason="You chose it.",
                path=path,
                value=column,
                suggested_value=column,
                evidence_ids=evidence,
                confidence=AgentConfidence.SURE,
            ),
        )
        for number, column in enumerate(choices, start=1)
    )
    return Question(question_id=question_id, text=text, options=options, blocking=True, evidence_ids=evidence)


def _ask_for_unsettled_roles(session: AgentSession, ctx: AgentContext) -> AgentSession:
    """Keep a blocking question open for each required role nobody has chosen (DEC-1821).

    Rejecting the helper's ID or outcome suggestion used to leave nothing to decide, so the session said ready
    and Approve then failed with `VALIDATION_FAILED`. Now a rejected (or never proposed) role is asked for: a
    role question answered earlier is opened again, otherwise a new one lists the likely columns. A suggestion
    still pending, or a role question still open, already blocks Approve, so nothing is added for it.
    """
    if session.stop_reason is not None or session.status is SessionStatus.APPLIED:
        return session
    questions = list(session.questions)
    for path in unsettled_roles(session):
        if any(
            p.kind is ProposalKind.ROLE and p.path == path and p.state is ProposalState.PENDING
            for p in session.proposals
        ):
            continue
        asked = [i for i, q in enumerate(questions) if _asks_role(q, path)]
        if any(questions[i].answer is None and questions[i].blocking for i in asked):
            continue
        if asked:
            last = asked[-1]
            questions[last] = questions[last].model_copy(update={"answer": None, "blocking": True})
            continue
        question = _role_question(session.model_copy(update={"questions": tuple(questions)}), ctx, path)
        if question is not None:
            questions.append(question)
    if len(questions) == len(session.questions) and all(
        a is b for a, b in zip(questions, session.questions, strict=True)
    ):
        return session
    return session.model_copy(update={"questions": tuple(questions)})


def session_from_advice(
    advice: Advice,
    ctx: AgentContext,
    *,
    session_id: str,
    agent_name: str,
    created_at: datetime,
    now: datetime,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        upload_id=ctx.upload_id,
        use_case_id=ctx.use_case_id,
        mode=ctx.mode.value,
        agent_name=agent_name,
        status=advice.status,
        proposals=advice.proposals,
        questions=advice.questions,
        tool_results=advice.tool_results,
        stop_reason=advice.stop_reason,
        assumptions=advice.assumptions,
        engine_hidden=advice.engine_hidden,
        created_at=created_at,
        updated_at=now,
    )


def rules_session(
    ctx: AgentContext,
    *,
    session_id: str,
    clock: Callable[[], datetime] = utc_now,
    primary_key: str | None = None,
    target: str | None = None,
) -> AgentSession:
    """Run the rules advisor once and open a working session on what it found. `primary_key` and `target` are
    the roles already decided (for example by a case), which the advisor then takes as given."""
    now = clock()
    advice = advise(ctx, id_prefix="a1-", primary_key=primary_key, target=target)
    session = session_from_advice(
        advice,
        ctx,
        session_id=session_id,
        agent_name=ctx.config.agent.name_for(ctx.config.name),
        created_at=now,
        now=now,
    )
    return with_summary(session, ctx, clock=clock)
