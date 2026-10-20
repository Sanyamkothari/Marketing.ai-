"""The case as one conversation (`view.conversation` of `GET /cases/{case_id}/screen`).

The chat-first Data Doctor screen draws a list of cards, in the order a person would read them. This module builds
that list from the case and from what `engine.doctor.screen` already worked out; the rules path and the AI path fill
the very same cards, because both leave their work in the same places (items, questions, doubts, the chat).

Every card has a `type`:

* ``first_look``  what was looked at: files, rows, how many fixes and questions, in one plain sentence;
* ``change``      one fix on the working copy: title, a counts sentence, who made it, its state, and Undo
                  (a pending one waits for a yes: "Use this"; an undone one can be done again);
* ``question``    what only the person can answer, as chips; "I don't know" is the safe choice, never a guess;
* ``doubt``       "I am not sure because / I would / if not";
* ``request``     more data to ask for;
* ``goals``       with no goal yet: what the data is good for (a few cards and how many more there are);
* ``fit``         with the goal chosen first (from New campaign): whether the data fits it;
* ``message``     the person's or the agent's chat message, with the changes it made (`item_ids`) and an answer the
                  agent suggests (`suggestion`; only the person's click records it);
* ``ready``       the one approval: the exact sentence, the stops and own-yes changes inside it, the speeds.

Order: the first look, then everything in the order it happened (a change a chat reply made follows that reply),
the goals or the fit after the first batch, and the ready card last. A change that is in the plan is a change card
with Undo the moment it is made, also when it needs its own yes (the ready card then repeats it as a checkbox, marked
`shown`). A change still waiting for its own yes, and a stop, are not cards of their own: they are inside the ready
card, as the checkboxes of the one approval. What was said about a file before an approved join or stack made it into
one table is not drawn again beside the table's own cards; the join itself stays.

Nothing here writes, and nothing here holds a cell value: titles, counts and sentences the rules or the AI wrote
(already masked in the case), ids and column names the case holds.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import Field, JsonValue

from engine.access.roles import Principal
from engine.agent.contracts import ProposalState
from engine.config import ConfigError, StrictBase, load_engine_config
from engine.doctor.case import Case, CaseItem, CaseQuestion, TimelineEvent, TimelineKind
from engine.doctor.goal_names import goal_display_name
from engine.doctor.plan_check import open_stops, plan_digest
from engine.doctor.protocol import REQUEST_CODES
from engine.doctor.visit import active_section, open_blocking

if TYPE_CHECKING:
    from engine.doctor.screen import CaseScreenView

__all__ = [
    "ChangeCard",
    "ChatOption",
    "ConversationCard",
    "DoubtCard",
    "FirstLookCard",
    "FitCard",
    "GoalChoice",
    "GoalsCard",
    "MessageCard",
    "QuestionCard",
    "ReadyCard",
    "ReadyConfirmation",
    "ReadyStop",
    "RequestCard",
    "Speed",
    "build_conversation",
]

DONT_KNOW: Final[str] = "I don't know"
"""The label of the option every question offers, mapped to its safe choice."""
GOALS_SHOWN: Final[int] = 3
"""Goal cards drawn at most; the rest are counted (`more_count`)."""
_GOAL_STATUS_ORDER: Final[dict[str, int]] = {"ready": 0, "check": 1, "too_small": 2, "missing": 3}
_REQUEST_CODES: Final[frozenset[str]] = frozenset(REQUEST_CODES.values())
_LAST: Final[datetime] = datetime.max.replace(tzinfo=UTC)


class ChatOption(StrictBase):
    """One answer chip of a question."""

    id: str
    label: str
    effect: str = Field(default="", description="What choosing it does, in a plain sentence.")
    recommended: bool = False
    safe: bool = Field(default=False, description='The safe choice that "I don\'t know" maps to.')
    rows_changed: int | None = None
    label_rate: float | None = None


class FirstLookFile(StrictBase):
    upload_id: str
    name: str
    rows: int | None = None
    columns: int | None = None
    kind: str = Field(default="", description='What the file is, in plain words ("a customer list").')


class FirstLookCard(StrictBase):
    type: Literal["first_look"] = "first_look"
    rows: int | None = Field(default=None, description="Rows of all the files together, when known.")
    files: tuple[FirstLookFile, ...] = ()
    fixes_count: int = Field(
        description="Fixes made on the working copy (changes ticked, by the rules or the agent)."
    )
    questions_count: int = Field(description="Questions still waiting for the person.")
    sentence: str = Field(description="The same, in one plain paragraph.")


class ChangeCard(StrictBase):
    type: Literal["change"] = "change"
    item_id: str
    title: str
    counts: str = Field(
        default="", description="Before -> after counts, in a sentence our code measured; may be empty."
    )
    source: Literal["rules", "ai", "person"]
    state: Literal["accepted", "pending", "rejected"] = Field(
        description="accepted: in the plan (Undo); pending: waiting for a yes (Use this); rejected: undone (Do it again)."
    )
    by: Literal["rules", "agent", "you", "someone"] | None = Field(
        default=None,
        description="Who put it in the plan, when it is in it; `someone` is another person on the team.",
    )
    undo: bool = Field(description="True while it is in the plan, so Undo is offered.")
    parked_for: str | None = None


class QuestionCard(StrictBase):
    type: Literal["question"] = "question"
    question_id: str
    code: str = Field(default="", description="The rule that asked it; technical, never drawn as text.")
    text: str
    why: str = ""
    blocking: bool = False
    options: tuple[ChatOption, ...]
    dont_know: ChatOption | None = Field(
        default=None,
        description='"I don\'t know": the safe choice the question offers; null when it has none.',
    )
    answer: str | None = Field(
        default=None, description="The option the person chose; a click on another changes it."
    )
    assumed: bool = Field(
        default=False, description="The rules decided it (too small to ask); the answer can change it."
    )
    note: str = Field(default="", description="An assumption or an earlier answer used, in a plain sentence.")
    counts: str = Field(
        default="",
        description="What the answer changed, in a sentence our code measured (the fix an answer makes has no card of its own).",
    )


class DoubtCard(StrictBase):
    type: Literal["doubt"] = "doubt"
    doubt_id: str
    because: str
    would_do: str
    if_not: str


class RequestCard(StrictBase):
    type: Literal["request"] = "request"
    question_id: str | None = Field(
        default=None, description="The question asking for the data; null for a plain list."
    )
    text: str
    why: str = ""
    options: tuple[ChatOption, ...] = ()
    answer: str | None = None
    goal_ids: tuple[str, ...] = Field(
        default=(), description="The goals the list to send the data team is for."
    )


class GoalChoice(StrictBase):
    goal_id: str
    name: str
    why: str = ""
    customers: int | None = None
    best: bool = False
    status: str = ""


class GoalsCard(StrictBase):
    type: Literal["goals"] = "goals"
    cards: tuple[GoalChoice, ...]
    more_count: int = Field(default=0, description="Goals that fit (ready or to check) but are not drawn.")


class FitCard(StrictBase):
    type: Literal["fit"] = "fit"
    goal_id: str
    name: str
    ok: bool
    sentence: str


class MessageSuggestion(StrictBase):
    question_id: str
    option_id: str
    label: str
    question: str = Field(default="", description="The question it answers, in its own words.")
    used: bool = Field(default=False, description="The question is answered by now.")


class MessageCard(StrictBase):
    type: Literal["message"] = "message"
    role: Literal["user", "agent"]
    text: str
    item_ids: tuple[str, ...] = ()
    withdrawn: tuple[str, ...] = Field(
        default=(), description="Titles of the agent's own changes it took back."
    )
    tools: tuple[str, ...] = ()
    suggestion: MessageSuggestion | None = None
    sent: tuple[dict[str, JsonValue], ...] = Field(default=(), description="What the AI looked at.")


class ReadyStop(StrictBase):
    key: str = Field(description="What `POST /cases/{id}/acknowledgements` takes.")
    text: str
    detail: str | None = Field(
        default=None, description="The expert's side of the text, shown behind a click."
    )
    item_id: str | None = None


class ReadyConfirmation(StrictBase):
    item_id: str
    sentence: str = Field(description="The exact sentence agreed to; sent back word for word.")
    why: str
    role: Literal["analyst", "approver"]
    required: bool = Field(
        description="Ticked already: approving needs its own yes. Otherwise it is left out unless agreed."
    )
    can_agree: bool = True
    cannot_agree: Literal["role", "separation"] | None = None
    separate: bool = False
    shown: bool = Field(
        default=False, description="The change is in the conversation already, as a card with Undo."
    )


class Speed(StrictBase):
    id: str
    name: str
    minutes: int
    recommended: bool = False


class ReadyCard(StrictBase):
    type: Literal["ready"] = "ready"
    stage: Literal["approve", "start", "blocked"] = Field(
        description="approve: one click chains the yeses and Approve; start: approved, ready to start; blocked: not ready."
    )
    sentence: str = Field(
        default="", description="What is approved, word for word (approve), or what was approved."
    )
    stops: tuple[ReadyStop, ...] = ()
    confirmations: tuple[ReadyConfirmation, ...] = ()
    left_out: tuple[str, ...] = Field(default=(), description="Own-yes changes nobody agreed to: left out.")
    blocked: bool = Field(
        default=False, description="A change needs someone else's own yes before this can be approved."
    )
    notes: tuple[str, ...] = Field(
        default=(), description="Why it is not ready, in the check's own sentences."
    )
    speeds: tuple[Speed, ...] = ()
    hold: str = Field(
        default="",
        description="Why Approve is off for now, in a plain sentence (no goal yet, a question still open, or the "
        "last check found something to fix first); empty when it may be clicked.",
    )


ConversationCard = Annotated[
    FirstLookCard
    | ChangeCard
    | QuestionCard
    | DoubtCard
    | RequestCard
    | GoalsCard
    | FitCard
    | MessageCard
    | ReadyCard,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def _n(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _is_agent(actor: str | None) -> bool:
    return actor is not None and actor.startswith("agent")


def _counts(item: CaseItem, step: dict[str, Any] | None) -> str:
    """The counts sentence of a change: a sentence the item carries, else the guards' count of its step."""
    said = item.payload.get("counts")
    if isinstance(said, str) and said.strip():
        return said.strip()
    rows_in = _n((step or {}).get("rows_in", item.payload.get("rows_in")))
    rows_out = _n((step or {}).get("rows_out", item.payload.get("rows_out")))
    parts: list[str] = []
    if rows_in is not None and rows_out is not None:
        if rows_out == rows_in:
            parts.append(f"{_plural(rows_in, 'row', 'rows')}, none left out")
        else:
            parts.append(f"{rows_in:,} rows in, {rows_out:,} out")
    elif (changed := _n(item.payload.get("changed"))) is not None:
        parts.append(f"{_plural(changed, 'value', 'values')} changed")
    lost, twice = _n((step or {}).get("customers_lost")), _n((step or {}).get("customers_duplicated"))
    if lost:
        parts.append(f"{_plural(lost, 'customer', 'customers')} lost")
    if twice:
        parts.append(f"{_plural(twice, 'customer appears', 'customers appear')} twice")
    return " · ".join(parts)


def _in_plan(item: CaseItem | None) -> bool:
    return item is not None and item.state is ProposalState.ACCEPTED


_COMBINES: Final[frozenset[str]] = frozenset({"build_dataset", "stack_files"})
"""The kinds of the approved join and stack (`engine.doctor.combine_run.JOIN_KIND`, `STACK_KIND`)."""
_CARRIED: Final[frozenset[str]] = frozenset({"outcome_column", "as_of_column", "consent_column"})
"""The decisions on what the data means; they stay with the case when a join or stack makes its files one table."""


def _is_check(item: CaseItem) -> bool:
    """A check that changes nothing (a note, or a flag raised for a person to read): not a change card."""
    return item.kind == "data_check"


def _change(
    item: CaseItem, steps: dict[str, dict[str, Any]], principal: Principal | None = None
) -> ChangeCard | None:
    state = item.state.value
    if state == "rejected" and (item.decided_by is None or _is_agent(item.decided_by)):
        return None  # never in the plan, or taken back by the agent itself (its message says so)
    by: Literal["rules", "agent", "you", "someone"] | None = None
    if item.state is ProposalState.ACCEPTED:
        if _is_agent(item.decided_by):
            by = "agent"
        elif not item.decided_by:
            by = "rules"
        else:  # a person: the reader, or (when we know who reads) a teammate
            by = "you" if principal is None or item.decided_by == principal.user_id else "someone"
    return ChangeCard(
        item_id=item.item_id,
        title=item.title or item.kind,
        counts=_counts(item, steps.get(item.item_id)),
        source=item.source.value,
        state=state,  # type: ignore[arg-type]
        by=by,
        undo=item.state is ProposalState.ACCEPTED,
        parked_for=item.parked_for,
    )


def _dont_know(question: CaseQuestion) -> tuple[ChatOption | None, str | None]:
    """The option "I don't know" stands for (the question's own, else its safe choice), and the id the chips leave out."""
    own = next((o for o in question.options if o.label == DONT_KNOW), None)
    if own is not None:
        return _option(own, question, effect="Data Doctor takes the safe choice."), own.option_id
    safe = next((o for o in question.options if o.safe), None)
    if safe is None:
        return None, None
    chip = _option(safe, question, effect=f"Data Doctor takes the safe choice: {safe.label}.")
    return chip.model_copy(update={"label": DONT_KNOW}), None


def _option(option: Any, question: CaseQuestion, *, effect: str | None = None) -> ChatOption:
    return ChatOption(
        id=option.option_id,
        label=option.label,
        effect=option.effect if effect is None else effect,
        recommended=question.recommended == option.option_id,
        safe=option.safe,
        rows_changed=option.rows_changed,
        label_rate=option.label_rate,
    )


def _question(question: CaseQuestion, counts: str = "") -> QuestionCard:
    dont_know, own_id = _dont_know(question)
    options = tuple(_option(o, question) for o in question.options if o.option_id != own_id)
    note = ""
    chose = next((o for o in question.options if o.option_id == question.answer), None)
    if question.standing_answer_id:
        note = "Using your earlier answer."
    elif question.assumed:
        note = (
            f"Decided for you: {chose.label if chose else 'the safe choice'}. "
            "The choice changes too little to ask. Pick another answer to change it."
        )
    return QuestionCard(
        question_id=question.question_id,
        code=question.code,
        text=question.text,
        why=question.why,
        blocking=question.blocking,
        options=options,
        dont_know=dont_know,
        answer=question.answer,
        assumed=question.assumed,
        note=note,
        counts=counts,
    )


def _request(question: CaseQuestion, goal_ids: tuple[str, ...]) -> RequestCard:
    return RequestCard(
        question_id=question.question_id,
        text=question.text,
        why=question.why,
        options=tuple(_option(o, question) for o in question.options),
        answer=question.answer,
        goal_ids=goal_ids,
    )


def _use_cases(case: Case) -> list[dict[str, Any]]:
    found = (case.diagnosis or {}).get("use_cases")
    return (
        [u for u in found if isinstance(u, dict) and u.get("use_case_id")] if isinstance(found, list) else []
    )


def _goals(case: Case) -> GoalsCard | None:
    found = sorted(_use_cases(case), key=lambda u: _GOAL_STATUS_ORDER.get(str(u.get("status")), 9))
    if not found:
        return None
    fitting = [u for u in found if u.get("status") in {"ready", "check"}]
    shown = fitting[:GOALS_SHOWN]
    cards = tuple(
        GoalChoice(
            goal_id=str(u["use_case_id"]),
            name=goal_display_name(str(u["use_case_id"]), str(u.get("name") or "")),
            why=str(u.get("why") or u.get("summary") or ""),
            customers=_n(u.get("customers")),
            best=index == 0,
            status=str(u.get("status") or ""),
        )
        for index, u in enumerate(shown)
    )
    return GoalsCard(cards=cards, more_count=len(fitting) - len(cards))


def _fit(case: Case, goal_id: str, goal_name: str | None, verdict_key: str, verdict_next: str) -> FitCard:
    found = next((u for u in _use_cases(case) if u.get("use_case_id") == goal_id), None)
    if found is not None:
        ok = found.get("status") in {"ready", "check"}
        sentence = str(found.get("why") or found.get("summary") or verdict_next)
    else:
        ok = verdict_key != "not_ready"
        sentence = verdict_next
    return FitCard(goal_id=goal_id, name=goal_display_name(goal_id, goal_name), ok=ok, sentence=sentence)


def _speeds(config_root: Path | None) -> tuple[Speed, ...]:
    try:
        minutes = load_engine_config(config_root).campaign_speeds
    except ConfigError:
        return ()
    return tuple(
        Speed(id=name, name=name.capitalize(), minutes=int(value), recommended=name == "balanced")
        for name, value in minutes.items()
    )


def _hold(case: Case, view: CaseScreenView) -> str:
    """Why Approve waits: nothing to approve it for yet, or something to fix first. Empty: it may go.

    A check that found the data not ready for this goal says the same again until the plan changes (a change makes
    that check old, and the card goes back to asking); a visit that was stopped by something else may try again.
    """
    if view.goal_id is None:
        return "Choose what the campaign is for first."
    if open_blocking(case):
        return "Answer the questions above first."
    if view.verdict.key == "not_ready" and not (case.visit is not None and case.visit.stopped is not None):
        return "Fix what the last check found first."
    return ""


def _ready(
    case: Case, view: CaseScreenView, config_root: Path | None, drawn: Collection[str] = ()
) -> ReadyCard | None:
    verdict = view.verdict
    entries = tuple(view.approve_class)
    accepted = {i.item_id for i in case.items if i.state is ProposalState.ACCEPTED}
    confirmations = tuple(
        ReadyConfirmation(
            item_id=a.item_id,
            sentence=a.sentence,
            why=a.why,
            role=a.role,
            required=a.item_id in accepted,
            can_agree=a.can_agree,
            cannot_agree=a.cannot_agree,
            separate=a.separate,
            shown=a.item_id in drawn,
        )
        for a in entries
        if not a.confirmed
    )
    notes = tuple(verdict.reasons) if verdict.key == "not_ready" else ()
    if view.approval_sentence:
        visit = case.visit
        check = visit.plan_check if visit is not None else None
        stops: tuple[ReadyStop, ...] = ()
        if visit is not None and check is not None and _current(case, check.digest):
            stops = tuple(
                ReadyStop(key=f.key, text=f.text, detail=f.detail, item_id=f.item_id)
                for f in open_stops(check, visit.acknowledged)
            )
        return ReadyCard(
            stage="approve",
            sentence=view.approval_sentence,
            stops=stops,
            confirmations=confirmations,
            blocked=any(c.required and not c.can_agree for c in confirmations),
            notes=notes,
            speeds=_speeds(config_root),
            hold=_hold(case, view),
        )
    if verdict.key in {"ready", "ready_with_warnings"} and view.start is not None:
        last = next((a for a in reversed(case.approvals) if not a.approve_class), None)
        return ReadyCard(
            stage="start",
            sentence=last.text_shown if last is not None else "",
            left_out=tuple(c.sentence for c in confirmations if not c.required),
            speeds=_speeds(config_root),
        )
    if verdict.key == "not_ready":
        return ReadyCard(stage="blocked", sentence=verdict.next, notes=notes)
    return None


def _current(case: Case, digest: str) -> bool:
    items, _ = active_section(case)
    return digest == plan_digest(case.files, items)


def _first_look(case: Case, fixes: int, questions: int, timeline: Sequence[TimelineEvent]) -> FirstLookCard:
    listed = (case.diagnosis or {}).get("files")
    diagnosed = {
        str(f.get("upload_id")): f
        for f in (listed if isinstance(listed, list) else [])
        if isinstance(f, dict)
    }
    taken = {
        e.upload_id: e.details
        for e in timeline
        if e.kind is TimelineKind.FILE_ADDED and e.details.get("taken_in")
    }
    files: list[FirstLookFile] = []
    for f in case.files:
        d, t = diagnosed.get(f.upload_id, {}), taken.get(f.upload_id, {})
        rows = _n(d.get("rows")) if d else _n(t.get("rows"))
        columns = _n(d.get("columns")) if d else _n(t.get("columns"))
        files.append(
            FirstLookFile(
                upload_id=f.upload_id,
                name=f.file_name or "your file",
                rows=rows,
                columns=columns,
                kind=str(d.get("kind_label") or "") if d else "",
            )
        )
    known = [f.rows for f in files if f.rows is not None]
    listed = ", ".join(
        f"{f.name} ({_plural(f.rows, 'row', 'rows')})" if f.rows is not None else f.name for f in files
    )
    sentence = f"I looked at {_plural(len(files), 'file', 'files')}: {listed}."
    if fixes:
        sentence += f" I made {_plural(fixes, 'fix', 'fixes')} on a copy. Your files stay as they are."
    else:
        sentence += " Your files stay as they are."
    if questions:
        sentence += f" {_plural(questions, 'question is', 'questions are')} only yours to answer."
    return FirstLookCard(
        rows=sum(known) if known else None,
        files=tuple(files),
        fixes_count=fixes,
        questions_count=questions,
        sentence=sentence,
    )


def _message(case: Case, entry: Any) -> MessageCard:
    titles = {i.item_id: i.title or i.kind for i in case.items}
    suggestion: MessageSuggestion | None = None
    wanted = getattr(entry, "suggestion", None)
    if wanted is not None:
        question = next((q for q in case.questions if q.question_id == wanted.question_id), None)
        if question is not None and any(o.option_id == wanted.option_id for o in question.options):
            suggestion = MessageSuggestion(
                question_id=question.question_id,
                option_id=wanted.option_id,
                label=wanted.label,
                question=question.text,
                used=question.answer is not None,
            )
    return MessageCard(
        role=entry.role,
        text=entry.text,
        item_ids=tuple(getattr(entry, "item_ids", ()) or ()),
        withdrawn=tuple(titles.get(i, i) for i in (getattr(entry, "withdrawn", ()) or ())),
        tools=tuple(getattr(entry, "tools", ()) or ()),
        suggestion=suggestion,
        sent=tuple(entry.sent),
    )


def build_conversation(
    case: Case,
    view: CaseScreenView,
    timeline: Sequence[TimelineEvent] = (),
    *,
    goal_name: str | None = None,
    config_root: Path | None = None,
    principal: Principal | None = None,
) -> tuple[ConversationCard, ...]:
    """The case as an ordered list of cards. `view` is the screen's view built without the conversation: its
    verdict, approve-class items, approval sentence, ledger and start say what the cards need."""
    if not case.files:
        return ()
    items, questions = active_section(case)
    # A change not in the plan yet that needs its own yes is inside the ready card. One that is in the plan (the AI
    # made it, the person ticked it) is a change card with Undo like any other, whether or not it has its yes.
    needs_own_yes = {
        a.item_id
        for a in view.approve_class
        if not a.confirmed and not _in_plan(next((i for i in items if i.item_id == a.item_id), None))
    }
    # What was said about a file before an approved join or stack made it into one table is not drawn again beside
    # the table's own cards. The join itself is, and so are the person's decisions on what the data means (the
    # outcome, the date, who agreed): they go on the table (`current_upload`).
    here = {f.upload_id for f in case.files}
    items = tuple(
        i
        for i in items
        if i.upload_id is None or i.upload_id in here or i.kind in _COMBINES or i.kind in _CARRIED
    )
    questions = tuple(q for q in questions if q.upload_id is None or q.upload_id in here)
    steps = {
        s["item_id"]: s
        for s in (view.ledger or {}).get("steps", [])
        if isinstance(s, dict) and "item_id" in s
    }
    carried_gone = set(view.carried.gone) if view.carried is not None else set()
    asked_at: dict[str, datetime] = {}
    for event in timeline:
        if event.item_id and event.kind in _ASKED:
            asked_at.setdefault(event.item_id, event.at)
    messages = [(index, entry) for index, entry in enumerate(case.chat)]
    first_message = min((entry.at for _, entry in messages), default=None)

    placed: dict[str, int] = {}  # item id -> index of the message that made it
    for index, entry in messages:
        for item_id in getattr(entry, "item_ids", ()) or ():
            placed.setdefault(item_id, index)

    found: list[tuple[datetime, int, int, Any]] = []  # (at, rank, seq, card)

    def add(at: datetime, rank: int, card: Any) -> None:
        found.append((at, rank, len(found), card))

    def in_reply(at: datetime) -> datetime:
        """A question or doubt raised while the agent answered (after the person wrote, before the reply was stored)
        is drawn with that reply, below its text."""
        before = max((e.at for _, e in messages if e.at <= at), default=None)
        if before is None or next(e for _, e in messages if e.at == before).role != "user":
            return at
        return min((e.at for _, e in messages if e.at > at and e.role == "agent"), default=at)

    # An answer makes its fix itself (id `<question_id>:<option>`, ids cut at 128): that fix belongs to the question
    # card, so the answer is told once and changed by tapping another chip, never twice.
    made: dict[str, CaseItem] = {}
    for q in questions:
        prefix = f"{q.question_id}:"
        for item in items:
            if item.item_id.startswith(prefix):
                made[item.item_id] = item

    changes: list[ChangeCard] = []
    for item in items:
        if (
            item.item_id in made
            or item.item_id in needs_own_yes
            or item.item_id in carried_gone
            or _is_check(item)
        ):
            continue
        card = _change(item, steps, principal)
        if card is None:
            continue
        changes.append(card)
        if item.item_id in placed:
            add(messages[placed[item.item_id]][1].at, 2, card)
        else:
            add(item.proposed_at, 2, card)
    for _, entry in messages:
        add(entry.at, 1, _message(case, entry))

    def said(question: CaseQuestion) -> str:
        prefix = f"{question.question_id}:"
        for item in made.values():
            if item.item_id.startswith(prefix) and item.state is ProposalState.ACCEPTED:
                return _counts(item, steps.get(item.item_id))
        return ""

    shown_questions = [q for q in questions if q.code not in _REQUEST_CODES]
    open_questions = [q for q in shown_questions if q.answer is None]
    for q in shown_questions:
        add(in_reply(asked_at.get(q.question_id, case.created_at)), 3, _question(q, said(q)))
    goal_ids = (view.goal_id,) if view.goal_id else tuple(view.missing_for[:4])
    for q in questions:
        if q.code in _REQUEST_CODES:
            add(in_reply(asked_at.get(q.question_id, case.created_at)), 5, _request(q, goal_ids))
    for doubt in case.doubts:
        if doubt.upload_id is not None and doubt.upload_id not in here:
            continue
        add(
            in_reply(
                next(
                    (e.at for e in timeline if e.kind is TimelineKind.DOUBT and e.item_id == doubt.doubt_id),
                    case.created_at,
                )
            ),
            4,
            DoubtCard(
                doubt_id=doubt.doubt_id, because=doubt.because, would_do=doubt.would_do, if_not=doubt.if_not
            ),
        )

    # The goals (or the fit) follow the first batch, before the chat starts.
    base = [at for at, rank, _, _ in found if rank != 1 and (first_message is None or at < first_message)]
    base_end = max(base, default=case.created_at)
    if view.goal_id:
        add(base_end, 6, _fit(case, view.goal_id, goal_name, view.verdict.key, view.verdict.next))
    elif (goals := _goals(case)) is not None:
        add(base_end, 6, goals)

    fixes = sum(1 for c in changes if c.state == "accepted")
    add(case.created_at, 0, _first_look(case, fixes, len(open_questions), timeline))
    ready = _ready(case, view, config_root, {c.item_id for c in changes})
    if ready is not None:
        add(_LAST, 9, ready)
    found.sort(key=lambda entry: entry[:3])
    return tuple(card for _, _, _, card in found)


_ASKED: Final[frozenset[TimelineKind]] = frozenset(
    {
        TimelineKind.QUESTION_ASKED,
        TimelineKind.ASSUMED,
        TimelineKind.STANDING_APPLIED,
        TimelineKind.DATA_REQUESTED,
    }
)
