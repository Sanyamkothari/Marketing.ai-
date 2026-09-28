"""A Guided-setup session (Plan G §5, DEC-1007): the advisor's findings plus what the user decided.

Every function here takes a session and returns a new one; nothing is stored here and nothing
touches data. The API stores the result at `uploads/<upload_id>/agent/agent_session.json`.

* `start_session` runs the advisor once.
* `decide` accepts or rejects proposals. A setting's value may be edited to another value the
  settings schema allows; a recipe step or a role may only be accepted or rejected.
* `answer` records a question's answer. An option that carries a proposal adds it, accepted by the
  user. Answering who the ID or the outcome is re-runs the advisor with that role fixed, because
  every check downstream depends on it; decisions already made carry over to the new advice when
  the same proposal comes back (same kind and subject), and nothing the user chose is dropped.
  Answering "combine the rows?" (M76) re-runs it too: with Combine the rest of the advice is about
  the combined file; with Stop a repeating ID stops the session.
* `status_of` is the one rule for where a session is: stopped when the data cannot work, ready
  when nothing is left to decide, needs review otherwise.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Any

from engine.agent.advisor import ROLE_PRIMARY_KEY, ROLE_TARGET, Advice, advise, summarise
from engine.agent.contracts import (
    AgentSession,
    DecidedBy,
    Proposal,
    ProposalKind,
    ProposalState,
    Question,
    RecipeStep,
    RecipeStepKind,
    SessionStatus,
)
from engine.agent.recommend import setting_allowed, settings_fields
from engine.agent.tools import AgentContext, AgentToolError
from engine.config import ConfigError, advanced_settings_schema, resolve_config
from engine.utils.time import utc_now

__all__ = [
    "SessionError",
    "accept_recommended",
    "answer",
    "decide",
    "identity",
    "reshape_of",
    "roles_of",
    "start_session",
    "status_of",
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
    if session.status is SessionStatus.APPLIED:
        return SessionStatus.APPLIED
    if session.stop_reason is not None:
        return SessionStatus.STOPPED
    return SessionStatus.NEEDS_REVIEW if session.undecided else SessionStatus.READY


def roles_of(session: AgentSession) -> tuple[str | None, str | None]:
    """(primary key, target) the user accepted, else the ones proposed."""

    def pick(path: str) -> str | None:
        chosen = [p for p in session.proposals if p.kind is ProposalKind.ROLE and p.path == path]
        accepted = [p for p in chosen if p.state is ProposalState.ACCEPTED]
        live = accepted or [p for p in chosen if p.state is ProposalState.PENDING]
        return str(live[-1].value) if live else None

    return pick(ROLE_PRIMARY_KEY), pick(ROLE_TARGET)


def _combines(proposal: Proposal | None) -> bool:
    return (
        proposal is not None
        and proposal.step is not None
        and proposal.step.kind is RecipeStepKind.COMBINE_ROWS
    )


def _is_combine_question(question: Question) -> bool:
    """The M76 question "combine the rows?": one of its options adds a `combine_rows` step."""
    return any(_combines(option.proposal) for option in question.options)


def reshape_of(session: AgentSession) -> tuple[RecipeStep | None, bool]:
    """(the `combine_rows` step the user accepted, whether they declined combining), for `advise`."""
    accepted = [
        p.step for p in session.proposals if p.state is ProposalState.ACCEPTED and _combines(p) and p.step
    ]
    declined = any(
        _is_combine_question(q)
        and q.answer is not None
        and not any(_combines(o.proposal) for o in q.options if o.option_id == q.answer)
        for q in session.questions
    )
    return (accepted[-1] if accepted else None), declined


def with_summary(
    session: AgentSession, ctx: AgentContext, *, clock: Callable[[], datetime] = utc_now
) -> AgentSession:
    """The session with its status and pre-Approve summary brought up to date."""
    summary = summarise(
        ctx, session.proposals, assumptions=session.assumptions, engine_hidden=session.engine_hidden
    )
    updated = session.model_copy(update={"summary": summary, "updated_at": clock()})
    return updated.model_copy(update={"status": status_of(updated)})


def _from_advice(
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


def start_session(
    ctx: AgentContext, *, session_id: str, clock: Callable[[], datetime] = utc_now
) -> AgentSession:
    """Run the advisor once and open a session on what it found."""
    now = clock()
    advice = advise(ctx, id_prefix="a1-")
    session = _from_advice(
        advice,
        ctx,
        session_id=session_id,
        agent_name=ctx.config.agent.name_for(ctx.config.name),
        created_at=now,
        now=now,
    )
    return with_summary(session, ctx, clock=clock)


def _decided(proposal: Proposal, state: ProposalState, value: Any = None) -> Proposal:
    update: dict[str, Any] = {"state": state, "decided_by": DecidedBy.USER}
    if value is not None:
        update["value"] = value
    return proposal.model_copy(update=update)


def _check_edit(ctx: AgentContext, session: AgentSession, proposal: Proposal, value: Any) -> None:
    if proposal.kind is not ProposalKind.SETTING or proposal.path is None:
        raise SessionError(
            "AGENT_EDIT_NOT_ALLOWED", "Only a setting's value can be changed; accept or reject the rest."
        )
    key, target = roles_of(session)
    schema = advanced_settings_schema(
        ctx.config, columns=tuple(str(c) for c in ctx.frame.columns), primary_key=key, target=target
    )
    if not setting_allowed(proposal.path, value, settings_fields(schema)):
        raise SessionError("AGENT_VALUE_NOT_ALLOWED", f"{value!r} is not a value this setting can take.")
    try:
        resolve_config(ctx.use_case_id, {proposal.path: value}, root=ctx.config_root)
    except ConfigError as exc:
        raise SessionError(exc.code, exc.message) from exc


def decide(
    session: AgentSession,
    ctx: AgentContext,
    decisions: Sequence[tuple[str, ProposalState, Any]],
    *,
    clock: Callable[[], datetime] = utc_now,
) -> AgentSession:
    """Apply `(proposal_id, accepted|rejected, new value or None)` decisions, all or none."""
    _refuse_if_closed(session)
    by_id = {p.proposal_id: p for p in session.proposals}
    changed: dict[str, Proposal] = {}
    for proposal_id, state, value in decisions:
        proposal = by_id.get(proposal_id)
        if proposal is None:
            raise SessionError(
                "AGENT_PROPOSAL_UNKNOWN", f"There is no suggestion {proposal_id!r} in this session."
            )
        if state is ProposalState.PENDING:
            raise SessionError("AGENT_DECISION_INVALID", "A suggestion is either accepted or rejected.")
        if value is not None and value != proposal.value:
            _check_edit(ctx, session, proposal, value)
        changed[proposal_id] = _decided(proposal, state, value)
    proposals = tuple(changed.get(p.proposal_id, p) for p in session.proposals)
    return with_summary(session.model_copy(update={"proposals": proposals}), ctx, clock=clock)


def accept_recommended(
    session: AgentSession, ctx: AgentContext, *, clock: Callable[[], datetime] = utc_now
) -> AgentSession:
    """Accept every pending suggestion the helper is sure of (and uncertain ones when the use case says so)."""
    tick_uncertain = ctx.config.agent.tick_uncertain
    decisions = [
        (p.proposal_id, ProposalState.ACCEPTED, None)
        for p in session.proposals
        if p.state is ProposalState.PENDING and (p.confidence.value == "sure" or tick_uncertain)
    ]
    return decide(session, ctx, decisions, clock=clock)


def _merge(old: AgentSession, advice: Advice, now: datetime) -> AgentSession:
    decided = {identity(p): p for p in old.proposals if p.state is not ProposalState.PENDING}
    proposals: list[Proposal] = []
    seen: set[tuple[str, ...]] = set()
    for proposal in advice.proposals:
        key = identity(proposal)
        seen.add(key)
        earlier = decided.get(key)
        proposals.append(
            proposal.model_copy(
                update={"state": earlier.state, "decided_by": earlier.decided_by, "value": earlier.value}
            )
            if earlier is not None
            else proposal
        )
    # What the user chose themselves stays, even when the new advice does not suggest it again.
    proposals += [p for key, p in decided.items() if key not in seen and p.decided_by is DecidedBy.USER]
    answered = {q.text: q for q in old.questions if q.answer is not None}
    questions: list[Question] = [answered.pop(q.text, q) for q in advice.questions]
    questions += list(answered.values())
    evidence = {r.evidence_id: r for r in (*old.tool_results, *advice.tool_results)}
    cited = {e for p in proposals for e in p.evidence_ids} | {e for q in questions for e in q.evidence_ids}
    cited |= {
        e for q in questions for o in q.options if o.proposal is not None for e in o.proposal.evidence_ids
    }
    return old.model_copy(
        update={
            "proposals": tuple(proposals),
            "questions": tuple(questions),
            "tool_results": tuple(
                r for key, r in evidence.items() if key in cited or r in advice.tool_results
            ),
            "stop_reason": advice.stop_reason,
            "assumptions": advice.assumptions,
            "engine_hidden": advice.engine_hidden,
            "rounds": old.rounds + 1,
            "updated_at": now,
            "status": SessionStatus.NEEDS_REVIEW,
        }
    )


def answer(
    session: AgentSession,
    ctx: AgentContext,
    question_id: str,
    option_id: str,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> AgentSession:
    """Record the answer; add the option's proposal, accepted; re-advise when a role was chosen."""
    _refuse_if_closed(session)
    question = next((q for q in session.questions if q.question_id == question_id), None)
    if question is None:
        raise SessionError("AGENT_QUESTION_UNKNOWN", f"There is no question {question_id!r} in this session.")
    option = next((o for o in question.options if o.option_id == option_id), None)
    if option is None:
        raise SessionError("AGENT_OPTION_UNKNOWN", f"{option_id!r} is not one of this question's answers.")
    questions = tuple(
        q.model_copy(update={"answer": option_id}) if q is question else q for q in session.questions
    )
    proposals = list(session.proposals)
    chosen = option.proposal
    if chosen is not None:
        proposals = [p for p in proposals if identity(p) != identity(chosen)]
        proposals.append(_decided(chosen, ProposalState.ACCEPTED))
    updated = session.model_copy(update={"questions": questions, "proposals": tuple(proposals)})
    if (chosen is not None and chosen.kind is ProposalKind.ROLE) or _is_combine_question(question):
        key, target = roles_of(updated)
        combine, declined = reshape_of(updated)
        try:
            advice = advise(
                ctx,
                primary_key=key,
                target=target,
                id_prefix=f"a{session.rounds + 1}-",
                combine=combine,
                combine_declined=declined,
            )
        except AgentToolError as exc:
            raise SessionError(exc.code, exc.message) from exc
        updated = _merge(updated, advice, clock())
        role_keys = {identity(chosen)} if chosen is not None and chosen.kind is ProposalKind.ROLE else set()
        updated = updated.model_copy(
            update={
                "proposals": tuple(
                    (
                        _decided(p, ProposalState.ACCEPTED)
                        if identity(p) in role_keys and p.decided_by is None
                        else p
                    )
                    for p in updated.proposals
                )
            }
        )
    return with_summary(updated, ctx, clock=clock)


def _refuse_if_closed(session: AgentSession) -> None:
    if session.status is SessionStatus.APPLIED:
        raise SessionError(
            "AGENT_SESSION_APPLIED", "This setup was already approved. Start again to change it."
        )


def accepted(proposals: Iterable[Proposal]) -> list[Proposal]:
    return [p for p in proposals if p.state is ProposalState.ACCEPTED]
