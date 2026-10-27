"""The one approval screen (`view.review` of `GET /cases/{case_id}/screen`; roadmap 2.5).

The plan, what was assumed or decided, the problems, the swaps and Approve used to live in different panels. This
module reads the case and the view `engine.doctor.screen` already worked out and says it as one list, so one screen
can show it and its single Approve button knows when it may be clicked:

* ``plan``       every change in the plan, one line each;
* ``decisions``  what the data means (customer ID, outcome, dates, consent) and every other question or assumption:
                 what is decided now and by whom, the answers that change it and, for an answer that needs a typed
                 value, its `value_kind` (the input to draw). An answer the person gave is final; one the rules
                 assumed, or an earlier answer used again, needs the person's yes or a change (`needs_yes`);
* ``problems``   the stops and hard rules of the ledger, each with the reason and what would fix it. A hard rule has
                 `acknowledgeable` false: no one can say yes to it;
* ``agreements`` changes that need their own yes (`ReadyConfirmation`'s source);
* ``swaps``      an AI change that replaces a step the person accepted, old beside new;
* ``approve``    the sentence, whether the button may be clicked from the server's point of view, and what it waits for.

The actions are the existing ones (answers, decisions, confirmations, acknowledgements, approval); nothing here
writes. Names, counts, sentences and column names only; never a cell value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from engine.agent.contracts import ProposalState
from engine.config import StrictBase
from engine.doctor.case import Case, CaseItem, CaseOption, CaseQuestion
from engine.doctor.conversation import _counts, _is_agent
from engine.doctor.decisions import AS_OF_QUESTION, decisions
from engine.doctor.ledger import findings_of
from engine.doctor.protocol import AGENT, REQUEST_CODES
from engine.doctor.visit import CARRIED_KINDS, active_section, plan_change_items

if TYPE_CHECKING:
    from engine.doctor.screen import CaseScreenView

__all__ = [
    "ApprovalReview",
    "ReviewAgreement",
    "ReviewApprove",
    "ReviewDecision",
    "ReviewLine",
    "ReviewOption",
    "ReviewProblem",
    "ReviewSwap",
    "build_review",
]

DecisionKind = Literal["customer_id", "outcome", "date", "consent", "other"]
_FOLDED: Final[frozenset[str]] = frozenset({"TRAINING_BUDGET"})
"""Questions the approval screen does not ask: the learning time is chosen where the campaign starts."""
_SETS_KIND: Final[dict[str, DecisionKind]] = {
    "key": "customer_id",
    "outcome": "outcome",
    "as_of": "date",
    "training_as_of": "date",
    "as_of_column": "date",
    "consent": "consent",
}
_ITEM_KIND: Final[dict[str, DecisionKind]] = {
    "key": "customer_id",
    "outcome": "outcome",
    "as_of_column": "date",
    "consent": "consent",
    "as_of": "date",
    "training_as_of": "date",
}
_TITLES: Final[dict[DecisionKind, str]] = {
    "customer_id": "Which column names the customer",
    "outcome": "What counts as the outcome",
    "date": "Which date the data is as of",
    "consent": "Who may be contacted",
    "other": "",
}


class ReviewLine(StrictBase):
    """One change in the plan, in one line."""

    item_id: str
    text: str
    counts: str = Field(default="", description="Before -> after counts, in a sentence our code measured.")
    by: Literal["rules", "agent", "person"]
    undo: bool = True


class ReviewOption(StrictBase):
    """One answer of a decision; `value_kind` says it needs a typed value (the input to draw)."""

    id: str
    label: str
    effect: str = ""
    recommended: bool = False
    safe: bool = False
    value_kind: Literal["date", "number", "text"] | None = None


class ReviewDecision(StrictBase):
    """What was decided or assumed, with the way to say yes or change it."""

    kind: DecisionKind
    title: str
    why: str = ""
    current: str | None = Field(
        default=None, description="The decision now, in words; null while unanswered."
    )
    decided_by: Literal["person", "rules", "agent", "earlier"] | None = Field(
        default=None, description="Who decided it; `earlier` is this client's remembered answer."
    )
    question_id: str | None = Field(
        default=None, description="Change it by answering this (`POST /answers`)."
    )
    item_id: str | None = Field(
        default=None, description="A decision with no question: untick it (`POST /decisions`) to change it."
    )
    options: tuple[ReviewOption, ...] = ()
    answer: str | None = None
    answer_value: str | None = Field(default=None, description="The value typed with the answer, if any.")
    needs_yes: bool = Field(
        description="The rules assumed it or an earlier answer was used: the person says yes or changes it."
    )
    answered: bool = Field(description="Nothing more is needed from the server's side (answered or decided).")
    blocking: bool = False


class ReviewProblem(StrictBase):
    """A stop or hard rule: yes (go on anyway) or no (change the plan or the file)."""

    key: str = Field(description="What `POST /acknowledgements` takes.")
    text: str
    detail: str | None = None
    hard: bool = Field(description="A rule nobody may say yes to.")
    acknowledgeable: bool
    acknowledged: bool = False
    fix: str = Field(default="", description="What would fix it, in a plain sentence.")
    fix_item_ids: tuple[str, ...] = ()


class ReviewAgreement(StrictBase):
    """A change that needs its own yes, with its sentence word for word."""

    item_id: str
    sentence: str
    why: str
    required: bool = Field(
        description="In the plan: Approve records this yes. Otherwise it is left out unless ticked."
    )
    can_agree: bool
    cannot_agree: Literal["role", "separation"] | None = None
    separate: bool = False


class ReviewSwap(StrictBase):
    """The AI changed a step the person had accepted: the old and the new, side by side."""

    new_item_id: str
    old_item_id: str
    old_title: str
    old_counts: str = ""
    new_title: str
    new_counts: str = ""
    by: Literal["rules", "agent", "person"]
    state: Literal["pending", "accepted"] = Field(
        description="pending: choose (`POST /decisions` on the new one: true uses it, false keeps the old); accepted: already swapped."
    )


class ReviewApprove(StrictBase):
    stage: Literal["approve", "start", "blocked", "waiting"]
    sentence: str = Field(default="", description="What Approve records, word for word.")
    enabled: bool = Field(description="From the server's side: nothing it knows of is unanswered.")
    waiting: tuple[str, ...] = Field(
        default=(), description="What Approve waits for, one plain sentence each."
    )


class ApprovalReview(StrictBase):
    plan: tuple[ReviewLine, ...] = ()
    decisions: tuple[ReviewDecision, ...] = ()
    problems: tuple[ReviewProblem, ...] = ()
    agreements: tuple[ReviewAgreement, ...] = ()
    swaps: tuple[ReviewSwap, ...] = ()
    approve: ReviewApprove


def _by(item: CaseItem) -> Literal["rules", "agent", "person"]:
    if _is_agent(item.decided_by) or (not item.decided_by and item.source.value == "ai"):
        return "agent"
    return "person" if item.decided_by else "rules"


def _kind_of_question(question: CaseQuestion) -> DecisionKind:
    for option in question.options:
        if option.sets in _SETS_KIND:
            return _SETS_KIND[option.sets]
    if question.code == AS_OF_QUESTION or any(o.value_kind == "date" for o in question.options):
        return "date"
    return "other"


def _option(option: CaseOption, question: CaseQuestion) -> ReviewOption:
    return ReviewOption(
        id=option.option_id,
        label=option.label,
        effect=option.effect,
        recommended=question.recommended == option.option_id,
        safe=option.safe,
        value_kind=option.value_kind,
    )


def _question_decision(question: CaseQuestion) -> ReviewDecision:
    kind = _kind_of_question(question)
    chosen = next((o for o in question.options if o.option_id == question.answer), None)
    who: Literal["person", "rules", "agent", "earlier"] | None = None
    if chosen is not None:
        if question.standing_answer_id:
            who = "earlier"
        elif question.assumed:
            who = "rules"
        elif question.answered_by == AGENT or _is_agent(question.answered_by):
            who = "agent"
        else:
            who = "person"
    current = None
    if chosen is not None:
        current = chosen.label if not question.answer_value else f"{chosen.label}: {question.answer_value}"
    return ReviewDecision(
        kind=kind,
        title=question.text,
        why=question.why,
        current=current,
        decided_by=who,
        question_id=question.question_id,
        options=tuple(_option(o, question) for o in question.options),
        answer=question.answer,
        answer_value=question.answer_value,
        needs_yes=who in {"rules", "earlier"},
        answered=chosen is not None,
        blocking=question.blocking,
    )


def _item_decisions(case: Case) -> list[ReviewDecision]:
    """The decisions with no question behind them (a column the rules or the AI proposed and a person ticked)."""
    out: list[ReviewDecision] = []
    for record in decisions(case).records:
        if not record.source.startswith("item:"):
            continue
        kind = _ITEM_KIND.get(record.kind, "other")
        item_id = record.source.removeprefix("item:")
        item = next((i for i in case.items if i.item_id == item_id), None)
        who: Literal["person", "rules", "agent", "earlier"] = (
            "agent" if _is_agent(record.by) else ("person" if record.by else "rules")
        )
        out.append(
            ReviewDecision(
                kind=kind,
                title=_TITLES.get(kind) or (item.title if item is not None else record.kind),
                why=(item.assumption or "") if item is not None else "",
                current=str(record.value),
                decided_by=who,
                item_id=item_id,
                needs_yes=who != "person",
                answered=True,
            )
        )
    return out


def _problems(case: Case, titles: dict[str, str]) -> tuple[ReviewProblem, ...]:
    out: list[ReviewProblem] = []
    for f in findings_of(case):
        if f.source == "doubt" or f.level not in {"stop", "hard"}:
            continue
        fix = next((r for r in f.resolutions if r.kind == "fix"), None)
        ids = fix.item_ids if fix is not None else ()
        steps = ", ".join(titles.get(i, i) for i in ids)
        fix_text = ""
        if fix is not None:
            fix_text = fix.label + (f": {steps}" if steps else "")
        out.append(
            ReviewProblem(
                key=f.key,
                text=f.text,
                detail=f.detail,
                hard=f.level == "hard",
                acknowledgeable=f.acknowledgeable,
                acknowledged=f.acknowledged,
                fix=fix_text,
                fix_item_ids=ids,
            )
        )
    return tuple(sorted(out, key=lambda p: (not p.hard, p.acknowledged)))


def _swaps(case: Case, steps: dict[str, dict[str, object]]) -> tuple[ReviewSwap, ...]:
    by_id = {i.item_id: i for i in case.items}
    out: list[ReviewSwap] = []
    for item in case.items:
        old = by_id.get(item.replaces) if item.replaces else None
        if old is None or item.state is ProposalState.REJECTED:
            continue
        out.append(
            ReviewSwap(
                new_item_id=item.item_id,
                old_item_id=old.item_id,
                old_title=old.title or old.kind,
                old_counts=_counts(old, steps.get(old.item_id)),
                new_title=item.title or item.kind,
                new_counts=_counts(item, steps.get(item.item_id)),
                by=_by(item),
                state="accepted" if item.state is ProposalState.ACCEPTED else "pending",
            )
        )
    return tuple(out)


def build_review(case: Case, view: CaseScreenView) -> ApprovalReview:
    """The approval screen of a case; `view` is the screen's view built without the review."""
    items, questions = active_section(case)
    titles = {i.item_id: i.title or i.kind for i in case.items}
    steps = {
        s["item_id"]: s
        for s in (view.ledger or {}).get("steps", [])
        if isinstance(s, dict) and "item_id" in s
    }
    plan = tuple(
        ReviewLine(
            item_id=i.item_id,
            text=i.title or i.kind,
            counts=_counts(i, steps.get(i.item_id)),
            by=_by(i),
        )
        for i in plan_change_items(case)
        if i.kind not in CARRIED_KINDS
    )
    asked = [
        q
        for q in questions
        if q.code not in set(REQUEST_CODES.values()) | _FOLDED and not (q.answer is None and not q.options)
    ]
    shown = [_question_decision(q) for q in asked]
    covered = {d.kind for d in shown if d.kind != "other" and d.answered}
    shown.extend(d for d in _item_decisions(case) if d.kind not in covered)
    order: dict[str, int] = {"customer_id": 0, "outcome": 1, "date": 2, "consent": 3, "other": 4}
    shown.sort(key=lambda d: (order[d.kind], d.answered))
    problems = _problems(case, titles)
    swaps = _swaps(case, steps)
    agreements = tuple(
        ReviewAgreement(
            item_id=a.item_id,
            sentence=a.sentence,
            why=a.why,
            required=next(
                (i.state is ProposalState.ACCEPTED for i in items if i.item_id == a.item_id), False
            ),
            can_agree=a.can_agree,
            cannot_agree=a.cannot_agree,
            separate=a.separate,
        )
        for a in view.approve_class
        if not a.confirmed
    )
    waiting: list[str] = []
    if view.goal_id is None:
        waiting.append("Choose what the campaign is for.")
    open_q = [d for d in shown if not d.answered and d.question_id]
    if open_q:
        waiting.append(
            f"{len(open_q)} {'question is' if len(open_q) == 1 else 'questions are'} not answered."
        )
    pending = [s for s in swaps if s.state == "pending"]
    if pending:
        waiting.append(f"Choose between the old and the new step ({len(pending)}).")
    hard = [p for p in problems if p.hard]
    if hard:
        waiting.append(f"{len(hard)} {'rule is' if len(hard) == 1 else 'rules are'} not met.")
    if any(a.required and not a.can_agree for a in agreements):
        waiting.append("A change needs someone else's own yes.")
    if view.verdict.key == "not_ready":
        waiting.append("Fix what the last check found.")
    stage: Literal["approve", "start", "blocked", "waiting"]
    if view.approval_sentence:
        stage = "approve"
    elif view.verdict.key in {"ready", "ready_with_warnings"} and view.start is not None:
        stage = "start"
    elif view.verdict.key == "not_ready":
        stage = "blocked"
    else:
        stage = "waiting"
    return ApprovalReview(
        plan=plan,
        decisions=tuple(shown),
        problems=problems,
        agreements=agreements,
        swaps=swaps,
        approve=ReviewApprove(
            stage=stage,
            sentence=view.approval_sentence or "",
            enabled=stage == "approve" and not waiting,
            waiting=tuple(waiting),
        ),
    )
