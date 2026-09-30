"""A Guided-setup session (Plan G §5, DEC-1007): the advisor's findings plus what the user decided.

Every function here takes a session and returns a new one; nothing is stored here and nothing
touches data. The API stores the result at `uploads/<upload_id>/agent/agent_session.json`.

* `start_session` runs the advisor once.
* `decide` accepts or rejects proposals. A setting's value may be edited to another value the
  settings schema allows; a recipe step or a role may only be accepted or rejected. The settings
  accepted after a decision must resolve together, and one setting has at most one accepted
  value: accepting a suggestion for a setting rejects any other accepted for it, and when one
  decision accepts several, the latest suggestion wins (a chat request over the helper's own).
  A split by date is decided with its date column: a decision that leaves the data split by date
  without a date column in the file is refused.
* `accept_recommended` accepts what the helper is sure of, but never a setting the person has
  already accepted a value for.
* `answer` records a question's answer. An option that carries a proposal adds it, accepted by the
  user; answering again withdraws what the previous answer added. Answering who the ID or the
  outcome is re-runs the advisor with that role fixed, because every check downstream depends on
  it; decisions already made carry over to the new advice when the same proposal comes back (same
  kind and subject; an accepted one wins over a rejected duplicate), and nothing the user chose is
  dropped - except a recipe step on a column that is now the ID or the outcome, which the recipe
  may never change.
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

from engine.agent.advisor import (
    NEVER_TICKED_STEPS,
    ROLE_PRIMARY_KEY,
    ROLE_TARGET,
    Advice,
    advise,
    run_overrides,
    summarise,
)
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
from engine.agent.untrusted import display_name, plain_value, quoted
from engine.config import (
    ConfigError,
    RunMode,
    SplitType,
    advanced_settings_schema,
    resolve_config,
)
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
        raise SessionError(
            "AGENT_VALUE_NOT_ALLOWED", f"{plain_value(value)} is not a value this setting can take."
        )
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
                "AGENT_PROPOSAL_UNKNOWN", f"There is no suggestion {quoted(proposal_id)} in this session."
            )
        if state is ProposalState.PENDING:
            raise SessionError("AGENT_DECISION_INVALID", "A suggestion is either accepted or rejected.")
        if value is not None and value != proposal.value:
            _check_edit(ctx, session, proposal, value)
        changed[proposal_id] = _decided(proposal, state, value)
    proposals = _one_value_per_setting(
        [changed.get(p.proposal_id, p) for p in session.proposals], set(changed)
    )
    after = run_overrides(proposals)
    if after and after != run_overrides(session.proposals):
        # Each value resolving alone is not enough: a split by date and its column go together.
        try:
            resolve_config(ctx.use_case_id, after, root=ctx.config_root)
        except ConfigError as exc:
            raise SessionError(exc.code, exc.message) from exc
    gap = _split_gap(ctx, proposals)
    if gap is not None and _split_gap(ctx, session.proposals) is None:
        raise SessionError("TIME_COLUMN_MISSING", gap)
    return with_summary(session.model_copy(update={"proposals": proposals}), ctx, clock=clock)


_SPLIT_PATHS = frozenset({"split.type", "split.time_column"})


def _quoted(proposals: Sequence[Proposal]) -> str:
    return " and ".join(f"'{p.title}'" for p in proposals)


def _split_gap(ctx: AgentContext, proposals: Sequence[Proposal]) -> str | None:
    """Why these decisions would split the data by date with no date column to split by, or None.

    `resolve_config` accepts a split by date with no column, but the Run button refuses it
    (`TIME_COLUMN_MISSING`), so accepting a split by date while rejecting its column (or rejecting
    the random split a file without the date column needs) would reach "ready" and then fail. While
    a split suggestion is still pending the pair is being decided, so nothing is said yet.
    """
    if ctx.mode is not RunMode.TRAIN:
        return None
    splits = [p for p in proposals if p.kind is ProposalKind.SETTING and p.path in _SPLIT_PATHS]
    if any(p.state is ProposalState.PENDING for p in splits):
        return None
    try:
        config = resolve_config(ctx.use_case_id, run_overrides(proposals), root=ctx.config_root).config
    except ConfigError:
        return None  # `decide` reports the config's own refusal
    if config.split.type is not SplitType.TIME_BASED:
        return None
    column = config.split.time_column
    hidden = {
        p.step.column
        for p in proposals
        if p.state is ProposalState.ACCEPTED
        and p.step is not None
        and p.step.kind is RecipeStepKind.DROP_COLUMN
    }
    if column and column in ctx.frame.columns and column not in hidden:
        return None
    if not column:
        problem = "no date column is chosen"
    elif column in hidden:
        problem = f"'{display_name(column)}' is hidden"
    else:
        problem = f"this file has no '{display_name(column)}' column"
    message = f"The data would be split by date, but {problem}."
    rejected = [p for p in splits if p.state is ProposalState.REJECTED]
    chosen = [p for p in splits if p.state is ProposalState.ACCEPTED]
    if rejected:
        message += f" Accept {_quoted(rejected)}"
        message += f", or reject {_quoted(chosen)} as well." if chosen else "."
    elif chosen:
        message += f" Reject {_quoted(chosen)}."
    return message


def _one_value_per_setting(proposals: Sequence[Proposal], decided_now: set[str]) -> tuple[Proposal, ...]:
    """At most one accepted setting per path, so the run and the summary agree on its value.

    A setting accepted in this decision rejects any other accepted for the same path. When this
    decision accepts several for one path (the helper's ticked suggestion and the person's chat
    request, sent together by Preview), the one latest in the session wins - the order in which
    `run_overrides` reads them, so a chat request made after the helper's suggestion wins.
    """
    winner: dict[str, str] = {}
    for proposal in proposals:
        if (
            proposal.proposal_id in decided_now
            and proposal.state is ProposalState.ACCEPTED
            and proposal.kind is ProposalKind.SETTING
            and proposal.path is not None
        ):
            winner[proposal.path] = proposal.proposal_id
    return tuple(
        (
            _decided(p, ProposalState.REJECTED)
            if p.kind is ProposalKind.SETTING
            and p.state is ProposalState.ACCEPTED
            and p.path in winner
            and p.proposal_id != winner[p.path]
            else p
        )
        for p in proposals
    )


def accept_recommended(
    session: AgentSession, ctx: AgentContext, *, clock: Callable[[], datetime] = utc_now
) -> AgentSession:
    """Accept every pending suggestion the helper is sure of (and uncertain ones when the use case says so).

    A setting the person already accepted a value for is skipped: its pending suggestion stays for
    them to decide, so this never rejects their choice on their behalf (DEC-1010).
    """
    tick_uncertain = ctx.config.agent.tick_uncertain
    # A value the person has accepted for a setting is theirs: the helper's default never replaces it.
    settled = {
        p.path
        for p in session.proposals
        if p.kind is ProposalKind.SETTING and p.state is ProposalState.ACCEPTED and p.path
    }
    chosen: dict[tuple[str, str], Proposal] = {}
    for p in session.proposals:
        if p.state is ProposalState.PENDING and (p.confidence.value == "sure" or tick_uncertain):
            if p.kind is ProposalKind.SETTING and p.path in settled:
                continue  # left pending, for the person to decide
            if p.step is not None and p.step.kind in NEVER_TICKED_STEPS:
                continue  # only the person decides these (DEC-1224)
            # Two pending suggestions for one setting: the later one, never a conflict.
            key = ("path", p.path) if p.kind is ProposalKind.SETTING and p.path else ("id", p.proposal_id)
            chosen.pop(key, None)
            chosen[key] = p
    decisions = [(p.proposal_id, ProposalState.ACCEPTED, None) for p in chosen.values()]
    return decide(session, ctx, decisions, clock=clock)


def _touches_role(proposal: Proposal, roles: set[str]) -> bool:
    """A recipe step on the ID or the outcome column, which `check_recipe` refuses."""
    step = proposal.step
    return (
        step is not None
        and step.column in roles
        and step.kind not in {RecipeStepKind.COMBINE_ROWS, RecipeStepKind.DERIVE}
    )


def _merge(old: AgentSession, advice: Advice, now: datetime) -> AgentSession:
    decided: dict[tuple[str, ...], Proposal] = {}
    for p in old.proposals:
        if p.state is ProposalState.PENDING:
            continue
        # The same suggestion made twice (a repeated chat request): what the user accepted wins.
        earlier = decided.get(identity(p))
        if (
            earlier is None
            or earlier.state is not ProposalState.ACCEPTED
            or p.state is ProposalState.ACCEPTED
        ):
            decided[identity(p)] = p
    roles = {name for name in (advice.primary_key, advice.target) if name}
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
    # A step on a column that is now the ID or the outcome can no longer apply, so it goes.
    proposals += [
        p
        for key, p in decided.items()
        if key not in seen and p.decided_by is DecidedBy.USER and not _touches_role(p, roles)
    ]
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
        raise SessionError(
            "AGENT_QUESTION_UNKNOWN", f"There is no question {quoted(question_id)} in this session."
        )
    option = next((o for o in question.options if o.option_id == option_id), None)
    if option is None:
        raise SessionError(
            "AGENT_OPTION_UNKNOWN", f"{quoted(option_id)} is not one of this question's answers."
        )
    questions = tuple(
        q.model_copy(update={"answer": option_id}) if q is question else q for q in session.questions
    )
    # Answering again replaces the previous answer: whatever any option added is withdrawn first.
    offered = {identity(o.proposal) for o in question.options if o.proposal is not None}
    proposals = [p for p in session.proposals if identity(p) not in offered]
    chosen = option.proposal
    if chosen is not None:
        proposals.append(_decided(chosen, ProposalState.ACCEPTED))
    updated = session.model_copy(update={"questions": questions, "proposals": tuple(proposals)})
    # A role or the combine changes the file advised on; so does a date format once rows are combined.
    combining = reshape_of(updated)[0] is not None
    if (
        (chosen is not None and chosen.kind is ProposalKind.ROLE)
        or _is_combine_question(question)
        or (combining and chosen is not None and chosen.kind is ProposalKind.RECIPE_STEP)
    ):
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
                decided_steps=[
                    p.step
                    for p in updated.proposals
                    if p.state is ProposalState.ACCEPTED and p.step is not None and not _combines(p)
                ],
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
    # An answer is a decision too: hiding the column an accepted split by date uses is refused.
    gap = _split_gap(ctx, updated.proposals)
    if gap is not None and _split_gap(ctx, session.proposals) is None:
        raise SessionError("TIME_COLUMN_MISSING", gap)
    return with_summary(updated, ctx, clock=clock)


def _refuse_if_closed(session: AgentSession) -> None:
    if session.status is SessionStatus.APPLIED:
        raise SessionError(
            "AGENT_SESSION_APPLIED", "This setup was already approved. Start again to change it."
        )


def accepted(proposals: Iterable[Proposal]) -> list[Proposal]:
    return [p for p in proposals if p.state is ProposalState.ACCEPTED]
