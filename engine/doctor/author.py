"""The one author of the Data Doctor's plan.

Two authors write the plan, and they write it the same way:

* with an AI, the AI turn builds :data:`AuthorAction`s from its tool calls;
* without one, the default author builds the same actions from what the fact tools suggest.

:func:`apply_actions` is the only code that writes the case's items and questions. It takes a case and returns a case:
no storage, no tool call, no model. It runs on top of the reconcile store (`engine.doctor.reconcile`) with the single
producer :data:`PRODUCER`.

Who decides what:

* The author only *proposes*. Whether a proposal is in the plan at once or waits for the person's tick is decided by
  code (:func:`engine.doctor.agent_apply.waits_because`), never by the author.
* Nothing in the plan is changed here, decided or not. Replacing or withdrawing it is a new proposal that waits for
  the person's yes (a swap); their yes then leaves the old one out (`engine.doctor.swaps`). Only a change that is
  still waiting (in no plan, nobody's decision) is the author's own to swap or take back at once.
* `decided_by` is never set here. A change in the plan that nobody decided is "in the plan, undecided"; the approval
  stamps it.

Nothing here reads a cell.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import Field, JsonValue

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.config import StrictBase
from engine.doctor.agent_apply import Roles, person_owns, twin_of, waits_because
from engine.doctor.case import (
    Case,
    CaseDoubt,
    CaseItem,
    CaseQuestion,
    EventDraft,
    TimelineKind,
    payload_digest,
)
from engine.doctor.protocol import (
    REQUEST_CODES,
    Ask,
    DataRequest,
    ProtocolSettings,
    question_budget,
    question_id_of,
    request_data,
    settle,
)
from engine.doctor.reconcile import AUTHOR, Emission, Reconciled, left_out_event, reconcile, take_back
from engine.doctor.standing import StandingAnswerStore, apply_standing, no_longer_fits
from engine.doctor.subjects import SETTING_KIND, WITHDRAW_KIND
from engine.doctor.visit_contracts import CaseVisit, OptionEffect

__all__ = [
    "PRODUCER",
    "Applied",
    "ApplyContext",
    "AskPerson",
    "AuthorAction",
    "Create",
    "Doubt",
    "ItemDraft",
    "Refusal",
    "Request",
    "SetSetting",
    "SettingsView",
    "Withdraw",
    "apply_actions",
    "check_action",
]

PRODUCER: Final[str] = AUTHOR
"""The producer every item, question and doubt of the author carries (`CaseItem.producer`)."""
Effects = tuple[OptionEffect, ...]


class Refusal(StrictBase):
    """Why an action was not applied, in words a person (or the AI, to fix its next try) can act on."""

    code: str = Field(description="What kind of refusal, machine-readable.")
    what_failed: str = Field(description="What was refused, in one plain sentence.")
    value: str | None = Field(default=None, description="The name or count it is about; never a cell value.")
    how_to_fix: str = Field(min_length=1, description="What to change; never empty.")


class ItemDraft(StrictBase):
    """What a fact tool or an AI tool call produced for one plan item, its receipt included."""

    kind: str = Field(description="What it would change (`recipe_step`, `key`, `goal_setting`, ...).")
    payload: dict[str, JsonValue] = Field(description="What it would do, as data.")
    title: str = Field(description="What, in one plain sentence.")
    upload_id: str | None = Field(default=None, description="The file it is about; null for the whole case.")
    goal_id: str | None = Field(
        default=None, description="The goal section it belongs to; null for the base."
    )
    confidence: AgentConfidence = Field(description="`sure` only when the values prove it.")
    evidence_ids: tuple[str, ...] = Field(default=(), description="Evidence it rests on.")


class Create(StrictBase):
    """Put a change into the plan (or, when it must wait, in front of the person)."""

    op: Literal["create"] = "create"
    draft: ItemDraft
    reason: str = Field(description="Why, in the author's words.")
    replaces: str | None = Field(
        default=None,
        description="The item this takes the place of; one in the plan or the person's waits for a yes.",
    )
    basis: str = Field(
        description='What it rests on: "s:<suggestion id>", an evidence reference ("r<n>") or "msg:<n>".'
    )


class Withdraw(StrictBase):
    """Take a change out of the plan; one that is in the plan or the person's waits for a yes."""

    op: Literal["withdraw"] = "withdraw"
    item_id: str
    reason: str
    consequence: str = Field(description="What changes in the training table without it.")


class AskPerson(StrictBase):
    """An ambiguity, settled by the protocol: proven, assumed or asked within the file's question budget."""

    op: Literal["ask"] = "ask"
    ask: Ask


class SetSetting(StrictBase):
    """A goal's setting (window, audience) to a value its config allows."""

    op: Literal["setting"] = "setting"
    goal_id: str
    setting: str
    value: str
    reason: str


class Doubt(StrictBase):
    """The author is unsure and says so in three parts."""

    op: Literal["doubt"] = "doubt"
    doubt: CaseDoubt


class Request(StrictBase):
    """No choice makes the data usable: what to collect, from whom, and why."""

    op: Literal["request"] = "request"
    request: DataRequest


AuthorAction = Annotated[
    Create | Withdraw | AskPerson | SetSetting | Doubt | Request, Field(discriminator="op")
]


class SettingsView(Protocol):
    """What the author needs to know of the chosen goal's settings (`engine.doctor.goal_settings.GoalSettings`)."""

    def allowed(self, setting: str) -> Sequence[str | int]:
        """The values the goal's config allows for `setting`; none for an unknown setting."""
        ...


@dataclass(frozen=True)
class ApplyContext:
    """What the author's actions are applied under."""

    now: datetime
    author: Literal["ai", "default"]
    roles: Roles
    settings: ProtocolSettings
    goal: SettingsView | None = None
    complete: bool = False
    """The default author says the whole set it stands for: what it made before and no longer suggests is taken
    back. The AI adds as it goes, and only withdraws or replaces by name."""
    requested_by: str | None = None
    """The user id whose message caused the turn."""
    unevaluated: tuple[str, ...] = ()
    """Parts of its scope the author did not look at this round (item kinds, question codes, doubt-id prefixes): a
    complete emission does not take them back, because their absence says nothing."""
    standing: StandingAnswerStore | None = None
    """The client's remembered answers, applied to the questions asked now (`engine.doctor.standing`)."""
    suggested: frozenset[str] = frozenset()
    """The ids of the suggestions the fact tools made this turn: the only `s:<id>` basis the AI may be sure by."""


@dataclass
class Applied:
    """The case after the actions, and what happened to each."""

    case: Case
    events: list[EventDraft] = field(default_factory=list)
    refused: list[tuple[int, Refusal]] = field(default_factory=list)
    """The index of the action and why it was not applied."""
    made: list[str] = field(default_factory=list)
    """Ids of the items, questions and doubts created or changed."""
    after: list[Callable[[Case], None]] = field(default_factory=list)
    """Side effects once the case is written (a standing answer's use); each is idempotent."""


def check_action(case: Case, action: AuthorAction, ctx: ApplyContext) -> Refusal | None:
    """The guard layer's check of one action, before anything is written (filled in with `guards_layer`)."""
    del case, action, ctx  # the hook's signature: P4's guards read all three
    return None


def apply_actions(case: Case, actions: Sequence[AuthorAction], ctx: ApplyContext) -> Applied:
    """The case with `actions` applied, in order; a refused action is recorded and the next one still runs.

    Pure: the same case, actions and context give the same result, and applying the same actions again to the
    result changes nothing."""
    run = _Run(case, ctx)
    for index, action in enumerate(actions):
        refusal = check_action(case, action, ctx) or run.apply(action)
        if refusal is not None:
            run.refused.append((index, refusal))
    return run.finish()


# ---------------------------------------------------------------------------
# One call of apply_actions
# ---------------------------------------------------------------------------
def item_id_of(kind: str, payload: dict[str, JsonValue], goal_id: str | None) -> str:
    """A proposal's id is its content, so proposing it again finds it. What a trial on the first rows counted
    (`tried`) is not part of what the change is."""
    body = {k: v for k, v in payload.items() if k != "tried"}
    return f"{kind}.{goal_id or 'base'}.{payload_digest({'v': body})[:12]}"[:128]


def _in_section(goal_id: str | None, case: Case) -> bool:
    active = next((g for g in case.goals if g.active), None)
    return goal_id is None or (active is not None and goal_id == active.goal_id)


def _asked_kind(question: CaseQuestion) -> TimelineKind:
    """How a new question enters the timeline: asked, settled by an assumption, a standing answer, a request."""
    if question.standing_answer_id is not None:
        return TimelineKind.STANDING_APPLIED
    if question.assumed:
        return TimelineKind.ASSUMED
    if question.code in REQUEST_CODES.values():
        return TimelineKind.DATA_REQUESTED
    return TimelineKind.QUESTION_ASKED


def _not_found(item_id: str, case: Case) -> Refusal:
    live = [
        i.item_id
        for i in case.items
        if i.state is not ProposalState.REJECTED and _in_section(i.goal_id, case)
    ]
    return Refusal(
        code="ITEM_NOT_FOUND",
        what_failed=f"There is no item “{item_id}” in the plan.",
        value=item_id,
        how_to_fix="Use the id of an item in the plan"
        + (f": {', '.join(live[:8])}." if live else "; the plan is empty."),
    )


class _Run:
    """The work of one :func:`apply_actions`: the case's records as they change, and what is emitted at the end."""

    def __init__(self, case: Case, ctx: ApplyContext) -> None:
        self.case = case
        self.ctx = ctx
        self.work = Reconciled.of(case)
        self.emit: list[CaseItem] = []
        self.asks: list[Ask] = []
        self.requests: list[tuple[CaseQuestion, Effects]] = []
        self.doubts: list[CaseDoubt] = []
        self.refused: list[tuple[int, Refusal]] = []
        self.changed: list[str] = []
        self.events: list[EventDraft] = []

    # -- the actions ---------------------------------------------------------------------------------------------
    def apply(self, action: AuthorAction) -> Refusal | None:
        match action:
            case Create():
                return self._create(action)
            case Withdraw():
                return self._withdraw(action)
            case SetSetting():
                return self._setting(action)
            case AskPerson():
                self.asks.append(action.ask)
            case Doubt():
                self.doubts.append(action.doubt)
            case Request():
                self.requests.append(request_data(action.request))
        return None

    def _item(
        self, draft: ItemDraft, basis: str, *, state: ProposalState, replaces: str | None = None
    ) -> CaseItem:
        ctx = self.ctx
        # Only what the values prove is sure; the AI says so by adopting a suggestion a fact tool really made.
        proven = ctx.author == "default" or (basis.startswith("s:") and basis[2:] in ctx.suggested)
        return CaseItem(
            item_id=item_id_of(draft.kind, draft.payload, draft.goal_id),
            kind=draft.kind,
            title=draft.title,
            source=ProposalSource.AI if ctx.author == "ai" else ProposalSource.RULES,
            confidence=draft.confidence if proven else AgentConfidence.CHECK,
            state=state,
            upload_id=draft.upload_id,
            evidence_ids=tuple(dict.fromkeys((*draft.evidence_ids, basis))),
            payload=dict(draft.payload),
            payload_digest=payload_digest(draft.payload),
            proposed_at=ctx.now,
            goal_id=draft.goal_id,
            requested_by=ctx.requested_by,
            producer=PRODUCER,
            replaces=replaces,
        )

    def _find(self, item_id: str) -> CaseItem | None:
        return next(
            (i for i in self.work.items if i.item_id == item_id and _in_section(i.goal_id, self.case)), None
        )

    def _create(self, action: Create) -> Refusal | None:
        item = self._item(action.draft, action.basis, state=ProposalState.PENDING)
        twin = twin_of([*self.work.items, *self.emit], item)
        if twin is not None and twin.state is ProposalState.REJECTED and twin.withdrawn_because is None:
            return Refusal(
                code="LEFT_OUT_BY_PERSON",
                what_failed=f"“{twin.title}” was left out by the person, so it is not put back.",
                value=twin.item_id,
                how_to_fix="Ask the person to tick it again, or propose a different change.",
            )
        if twin is not None:
            self.emit.append(item)  # already there: said again, so a complete emission keeps it
            return None
        # Whether it is in the plan or waits for the person's tick is code's call, never the author's.
        placed = waits_because(item, self.ctx.roles) is None
        if action.replaces is not None:
            old = self._find(action.replaces)
            if old is None:
                return _not_found(action.replaces, self.case)
            if old.state is ProposalState.REJECTED:
                return Refusal(
                    code="ITEM_ALREADY_LEFT_OUT",
                    what_failed=f"“{old.title}” is already left out of the plan, so nothing is replaced.",
                    value=old.item_id,
                    how_to_fix="Propose the new change without `replaces`.",
                )
            if person_owns(self.case, old) or old.state is ProposalState.ACCEPTED:
                # A swap for the person to approve: what is in the plan stays until their yes leaves it out.
                item = item.model_copy(update={"replaces": old.item_id})
                placed = False
            else:
                self._take_out(old, action.reason, by=item.item_id, why="replaced")
        self.emit.append(
            item.model_copy(update={"state": ProposalState.ACCEPTED if placed else ProposalState.PENDING})
        )
        return None

    def _take_out(self, old: CaseItem, because: str, *, by: str | None, why: str) -> None:
        slot = self.work.items.index(old)
        self.work.items[slot] = take_back(old, self.ctx.now, because, by=by)
        self.events.append(left_out_event(why, old, **({"by": by} if by else {})))
        self.changed.append(old.item_id)

    def _withdraw(self, action: Withdraw) -> Refusal | None:
        old = self._find(action.item_id)
        if old is None:
            return _not_found(action.item_id, self.case)
        if old.state is ProposalState.REJECTED:
            return Refusal(
                code="ITEM_ALREADY_LEFT_OUT",
                what_failed=f"“{old.title}” is already left out of the plan.",
                value=old.item_id,
                how_to_fix="Nothing to withdraw; check the plan for what is still in it.",
            )
        if old.state is ProposalState.PENDING and not person_owns(self.case, old):
            self._take_out(old, action.reason, by=None, why="withdrawn")
            return None
        # In the plan, or the person's: taking it out is a change for them to say yes to.
        payload: dict[str, JsonValue] = {
            "item_id": old.item_id,
            "consequence": action.consequence,
            "reason": action.reason,
        }
        draft = ItemDraft(
            kind=WITHDRAW_KIND,
            payload=payload,
            title=f"Leave out: {old.title}",
            upload_id=old.upload_id,
            goal_id=old.goal_id,
            confidence=AgentConfidence.CHECK,
        )
        item = self._item(draft, f"withdraw:{old.item_id}", state=ProposalState.PENDING, replaces=old.item_id)
        twin = twin_of([*self.work.items, *self.emit], item)
        if twin is not None and twin.state is ProposalState.REJECTED and twin.withdrawn_because is None:
            return Refusal(
                code="LEFT_OUT_BY_PERSON",
                what_failed=f"The person already said no to leaving out “{old.title}”.",
                value=twin.item_id,
                how_to_fix="Leave it in the plan, or ask the person what they want instead.",
            )
        self.emit.append(item)
        return None

    def _setting(self, action: SetSetting) -> Refusal | None:
        allowed = [str(v) for v in self.ctx.goal.allowed(action.setting)] if self.ctx.goal is not None else []
        if action.value not in allowed:
            return Refusal(
                code="SETTING_VALUE_NOT_ALLOWED",
                what_failed=f"{action.value} is not an allowed value of {action.setting}.",
                value=action.setting,
                how_to_fix=(
                    f"Use one of: {', '.join(allowed)}."
                    if allowed
                    else f"The chosen goal has no setting named {action.setting}."
                ),
            )
        current = [
            i
            for i in self.work.items
            if i.kind == SETTING_KIND
            and i.payload.get("goal_id") == action.goal_id
            and i.payload.get("setting") == action.setting
            and i.state is not ProposalState.REJECTED
        ]
        draft = ItemDraft(
            kind=SETTING_KIND,
            payload={"goal_id": action.goal_id, "setting": action.setting, "value": action.value},
            title=f"Use {action.value} for {action.setting.replace('_', ' ')}.",
            goal_id=action.goal_id,
            confidence=AgentConfidence.CHECK,
        )
        return self._create(
            Create(
                draft=draft,
                reason=action.reason,
                replaces=current[-1].item_id if current else None,
                basis=f"setting:{action.setting}",
            )
        )

    # -- the end -------------------------------------------------------------------------------------------------
    def _settle(self) -> tuple[list[tuple[CaseQuestion, Effects]], list[CaseItem]]:
        """AUTO, a stated assumption or one question per ambiguity, within the file's question budget. The
        questions of an answer to the person's message are their own conversation: the budget does not cut them
        short (they still count towards the file's total)."""
        case, ctx = self.case, self.ctx
        questions = list(self.requests)
        items: list[CaseItem] = []
        # Asked already (a question the store took back and is asked again is not a new one): still asked now.
        known = {q.question_id for q in case.questions} | set((case.visit or CaseVisit()).offers)
        have = {q.question_id for q, _e in questions}
        asked = dict(case.counters.questions_per_file)
        for pending, _effects in questions:
            if pending.upload_id is not None and pending.answer is None and pending.question_id not in known:
                asked[pending.upload_id] = asked.get(pending.upload_id, 0) + 1
        ambiguities: dict[str, int] = {}
        for ask in self.asks:
            ambiguities[ask.upload_id or ""] = ambiguities.get(ask.upload_id or "", 0) + 1
        for ask in self.asks:
            key = ask.upload_id or ""
            budget = question_budget(asked.get(key, 0) + ambiguities[key], ctx.settings)
            over = (
                ctx.requested_by is None
                and asked.get(key, 0) >= budget
                and question_id_of(ask.code, ask.subject, ask.goal_id) not in known
            )
            settled = settle(ask, now=ctx.now, settings=ctx.settings, over_budget=over)
            if settled.question is not None and settled.question.question_id not in have:
                have.add(settled.question.question_id)
                questions.append((settled.question, settled.effects))
                if settled.how == "ask" and settled.question.question_id not in known:
                    asked[key] = asked.get(key, 0) + 1
            if settled.item is not None:
                # In the plan, undecided: the approval is what records the person's decision.
                items.append(
                    settled.item.model_copy(
                        update={
                            "source": ProposalSource.AI if ctx.author == "ai" else ProposalSource.RULES,
                            "decided_by": None,
                            "decided_at": None,
                            "requested_by": ctx.requested_by,
                        }
                    )
                )
        return questions, items

    def finish(self) -> Applied:
        case, ctx = self.case, self.ctx
        questions, settled = self._settle()
        items = [*self.emit, *settled]
        events = list(self.events)
        updates: dict[str, Any] = {}
        after: list[Callable[[Case], None]] = []
        if ctx.standing is not None:
            standing = apply_standing(
                ctx.standing, case, questions, now=ctx.now, in_scope=lambda g: _in_section(g, case)
            )
            questions, items = standing.questions, [*items, *standing.items]
            events.extend(standing.events)
            after.extend(standing.after)
            if standing.uses != case.standing_answers_used:
                updates["standing_answers_used"] = standing.uses
        work = reconcile(
            case,
            Emission(
                producer=PRODUCER,
                items=items,
                questions=questions,
                doubts=self.doubts,
                complete=ctx.complete,
                in_scope=lambda g: _in_section(g, case),
                unevaluated=ctx.unevaluated,
            ),
            now=ctx.now,
            refit=no_longer_fits,
            before=self.work,
        )
        events.extend(
            EventDraft(
                kind=TimelineKind.ITEM_PROPOSED,
                upload_id=item.upload_id,
                item_id=item.item_id,
                details={
                    "source": item.source.value,
                    "goal": item.goal_id,
                    "in_plan": item.state is ProposalState.ACCEPTED,
                },
            )
            for item in work.added
        )
        events.extend(work.events)
        made = [*self.changed, *(i.item_id for i in work.added)]
        offers, asked = self._book(work, events, made)
        known_doubts = {d.doubt_id for d in case.doubts}
        for doubt in work.doubts:
            if doubt.doubt_id not in known_doubts:
                made.append(doubt.doubt_id)
                events.append(  # a doubt raised is on the timeline
                    EventDraft(
                        kind=TimelineKind.DOUBT,
                        upload_id=doubt.upload_id,
                        item_id=doubt.doubt_id,
                        details={"attached_to": doubt.item_id, "evidence": len(doubt.evidence_ids)},
                    )
                )
        visit = case.visit or CaseVisit()
        if offers != visit.offers:
            updates["visit"] = visit.model_copy(update={"offers": offers})
        if asked != case.counters.questions_per_file:
            updates["counters"] = case.counters.model_copy(update={"questions_per_file": asked})
        out = case.model_copy(
            update={
                **updates,
                "items": tuple(work.items),
                "questions": tuple(work.questions),
                "doubts": tuple(work.doubts),
            }
        )
        return Applied(
            case=out,
            events=events,
            refused=self.refused,
            made=list(dict.fromkeys(made)),
            after=after,
        )

    def _book(
        self, work: Reconciled, events: list[EventDraft], made: list[str]
    ) -> tuple[dict[str, Effects], dict[str, int]]:
        """What each new question's options add (`case.visit.offers`), the file's question count, and the timeline."""
        case = self.case
        visit = case.visit or CaseVisit()
        offers = dict(visit.offers)
        asked = dict(case.counters.questions_per_file)
        have = {q.question_id for q in case.questions}
        for question, effects in work.asked:
            if question.question_id in have:
                if (
                    question.question_id in work.reasked
                ):  # asked again with other options: their effects answer it
                    offers[question.question_id] = effects
                    made.append(question.question_id)
                    events.append(
                        EventDraft(
                            kind=_asked_kind(question),
                            upload_id=question.upload_id,
                            item_id=question.question_id,
                            details={"code": question.code, "blocking": question.blocking, "reasked": True},
                        )
                    )
                continue  # asked once: an answered question is never asked again
            have.add(question.question_id)
            made.append(question.question_id)
            offers[question.question_id] = effects
            # A question the rules settled (an assumption) or a standing answer applied is not one the person was
            # asked, so it is not counted in the file's question budget.
            settled = question.assumed or question.standing_answer_id is not None
            if question.upload_id is not None and not settled and question.question_id not in visit.offers:
                asked[question.upload_id] = asked.get(question.upload_id, 0) + 1
            events.append(
                EventDraft(
                    kind=_asked_kind(question),
                    upload_id=question.upload_id,
                    item_id=question.question_id,
                    details={
                        "code": question.code,
                        "blocking": question.blocking,
                        "goal": question.goal_id,
                        **(
                            {"option": question.answer, "standing": question.standing_answer_id}
                            if settled
                            else {}
                        ),
                    },
                )
            )
        return offers, asked
