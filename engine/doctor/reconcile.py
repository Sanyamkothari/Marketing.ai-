"""The reconcile store: how what a producer emits becomes the case's proposals (F5 of
`docs/plans/DATA_DOCTOR_ROOT_CAUSES.md`).

Before this store each producer had its own way in: the rules' items were merged by id and never taken back, the AI
review added its items blindly, the chat checked for twins. So a proposal outlived the facts it rested on ("no
column has the outcome" beside "which column is the outcome?"), a rejected change came back in other words, and a
replaced query stayed beside its replacement.

The producer of the plan is `author` (`engine.doctor.author`), and `detectors` for the detectors' round; a case stored
before it has items made by `rules`, `detectors`, `review` and `chat`, which the author's complete emission treats as
its own (`rules`) or leaves to the detectors' round, or as the AI's (`review`, `chat`). A producer hands over an :class:`Emission`: what it proposes, asks and doubts for its scope. For
that emission the store, by *subject* (`engine.doctor.subjects`):

* **adds** a proposal whose subject is new;
* leaves the same proposal alone (same id), and does not add a **twin** (the same change in other words, compared by
  `agent_apply.meaning_key`, which ignores wording, order and the parameters of a trial); a change the person left
  out does not come back that way;
* **supersedes** a waiting (or agent-applied) proposal when another with the same subject but another payload is
  emitted: the old one is left out, with `superseded_by` and a reason;
* never touches what the person owns. A proposal that would replace one of theirs (ticked, unticked, or given their
  own yes) or one the rules ticked is added waiting, with `replaces` naming it: the plan shows a swap, the old one
  stays until the new one is ticked (`visit.decide` then leaves it out), so the two are never both in the plan;
* **retracts**, when the emission is `complete`, the waiting proposals (the author's: also those in the plan that nobody
  decided), unanswered questions and open doubts of that producer, in that scope, which it no longer emits
  (`withdrawn_because` says why; a doubt becomes `resolved`). The AI's proposals are never taken back by omission.

Questions: an unanswered question whose options changed is asked again (same id, the new options); an answered one
stays answered unless the option chosen is no longer offered, then it is asked again and says why (`refit`, the
visit's `_no_longer_fits`). Doubts have a status: `open`, `resolved`, `superseded`.

Nothing here reads a cell, runs a tool or calls a model: it works on the case's records alone, so the same set of
emissions gives the same plan whatever order the events came in.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Final

from engine.agent.contracts import ProposalSource, ProposalState
from engine.doctor.agent_apply import person_owns, required_by_goal, twin_of
from engine.doctor.case import Case, CaseDoubt, CaseItem, CaseQuestion, EventDraft, TimelineKind
from engine.doctor.protocol import AGENT
from engine.doctor.subjects import doubt_subject, item_subject, options_digest, question_subject

__all__ = [
    "AUTHOR",
    "Emission",
    "Reconciled",
    "find_twin",
    "is_withdrawn",
    "left_out_event",
    "open_doubts",
    "reconcile",
    "take_back",
]

Effects = tuple[Any, ...]

NO_LONGER_EMITTED: Final[str] = "it is no longer proposed: what it rested on has changed"
AUTHOR: Final[str] = "author"
"""The one producer of the plan (`engine.doctor.author`), the AI's and the default author's alike."""
AI_PRODUCERS: Final[frozenset[str]] = frozenset({"review", "chat"})
"""The AI's proposals are taken back only by an explicit withdraw/replace or by the person, never by omission: a
later review or another turn is not the pass that made them, so what it does not mention says nothing. (The author's
own AI items are told apart by `CaseItem.source`.)"""
RULE_PRODUCERS: Final[frozenset[str]] = frozenset({"rules"})
"""The producer of a case stored before the one author that is an alias of it, so its complete emission takes back
what the rules made and it no longer suggests. The detectors' round has its own producer (`detectors`): a fix of
theirs in the plan changes the files they read, so it is not taken back for being fixed."""
REPLACED: Final[str] = "a newer proposal about the same thing took its place"


def _everywhere(_goal_id: str | None) -> bool:
    return True


def _made_by(stored: str | None, producer: str) -> bool:
    """Whether what `stored` names as its producer is `producer`'s to take back."""
    return stored == producer or (producer == AUTHOR and stored in RULE_PRODUCERS)


@dataclass(frozen=True)
class Emission:
    """What one producer emits for its scope."""

    producer: str
    items: Sequence[CaseItem] = ()
    questions: Sequence[tuple[CaseQuestion, Effects]] = ()
    doubts: Sequence[CaseDoubt] = ()
    complete: bool = False
    """The emission is the producer's whole set for `in_scope`: what it made before and no longer emits is taken
    back. False for a producer that adds as it goes (the chat) or whose set depends on what is already on the page."""
    in_scope: Callable[[str | None], bool] = _everywhere
    """Whether a goal section (`None`: the shared base) is the producer's now; other goals' sections are set aside."""
    replaces: Sequence[str] = ()
    """Ids of the person's changes the producer says its new changes replace (the chat's `replace`)."""
    unevaluated: Sequence[str] = ()
    """Parts of its scope the producer did not look at this round (the rules stop early when the outcome is not
    decided): item kinds, question codes and doubt-id prefixes named here are not taken back, because their
    absence says nothing."""


@dataclass
class Reconciled:
    """The case's items, questions and doubts after the emissions so far, and what the store did."""

    items: list[CaseItem]
    questions: list[CaseQuestion]
    doubts: list[CaseDoubt]
    added: list[CaseItem] = field(default_factory=list)
    """Proposals that landed: new, or brought back. The caller applies what it may of them."""
    asked: list[tuple[CaseQuestion, Effects]] = field(default_factory=list)
    """Questions that are new or asked again, with their options' effects."""
    reasked: set[str] = field(default_factory=set)
    twins: dict[str, CaseItem | None] = field(default_factory=dict)
    """For each emitted proposal, the one already there that it is (same id, or the same change in other words)."""
    events: list[EventDraft] = field(default_factory=list)

    @classmethod
    def of(cls, case: Case, items: Sequence[CaseItem] | None = None) -> Reconciled:
        return cls(
            items=list(case.items if items is None else items),
            questions=list(case.questions),
            doubts=list(case.doubts),
        )


def is_withdrawn(item: CaseItem) -> bool:
    """Taken back by the Doctor itself (superseded, or no longer proposed): not a decision of the person's."""
    return item.withdrawn_because is not None


def open_doubts(doubts: Sequence[CaseDoubt]) -> tuple[CaseDoubt, ...]:
    """The doubts that still hold."""
    return tuple(d for d in doubts if d.status == "open")


def find_twin(items: Sequence[CaseItem], item: CaseItem) -> CaseItem | None:
    """The change already in `items` that `item` is (same id, or the same change in other words); a change the
    Doctor took back itself is not one."""
    return twin_of(items, item)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------
def reconcile(
    case: Case,
    emission: Emission,
    *,
    now: datetime,
    refit: Callable[[CaseQuestion, Any], CaseQuestion] | None = None,
    before: Reconciled | None = None,
) -> Reconciled:
    """Fold `emission` into the case's records (`before`: the result of an earlier emission of the same step)."""
    work = before if before is not None else Reconciled.of(case)
    _fold_items(case, emission, work, now)
    _fold_questions(emission, work, refit)
    _fold_doubts(emission, work)
    _close_doubts_of_gone_items(work)
    return work


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
def _skipped(emission: Emission, *names: str | None) -> bool:
    """The producer did not evaluate the part that `names` (a kind, a code or an id) belongs to."""
    return any(
        name is not None
        and any(name == part or name.startswith(part) for part in emission.unevaluated if part)
        for name in names
    )


def _tagged(item: CaseItem, producer: str) -> CaseItem:
    update: dict[str, Any] = {}
    if item.producer is None:
        update["producer"] = producer
    if item.subject is None:
        update["subject"] = item_subject(item)
    return item.model_copy(update=update) if update else item


def take_back(item: CaseItem, now: datetime, because: str, *, by: str | None = None) -> CaseItem:
    """`item` left out by the Doctor itself, with the reason (and the item that took its place, if one did)."""
    return item.model_copy(
        update={
            "state": ProposalState.REJECTED,
            "decided_by": AGENT,
            "decided_at": now,
            "parked_for": None,
            "withdrawn_because": because,
            "superseded_by": by,
        }
    )


def left_out_event(kind: str, item: CaseItem, **details: str | int | bool) -> EventDraft:
    """The timeline line of an item the Doctor took back (not the person's no): `kind` says why."""
    return EventDraft(
        kind=TimelineKind.ITEM_DECIDED,
        upload_id=item.upload_id,
        item_id=item.item_id,
        details={"state": "rejected", "by": "agent", "withdrawn": True, "why": kind, **details},
    )


def _holds_its_place(case: Case, item: CaseItem, emission: Emission | None = None) -> bool:
    """A change a newer one may only swap for, never replace: the person's, or ticked by anyone but the AI itself
    (the rules' proven key, a standing answer). Not the author's own, in the plan and nobody's decision: its
    complete set of suggestions supersedes that directly (the goal's build, a key it now suggests differently).
    """
    if person_owns(case, item):
        return True
    if (
        emission is not None
        and emission.complete
        and emission.producer not in AI_PRODUCERS
        and item.decided_by is None
        and item.source is not ProposalSource.AI
        and _made_by(item.producer, emission.producer)
    ):
        return False
    return item.state is ProposalState.ACCEPTED and not (
        item.source is ProposalSource.AI and item.decided_by == AGENT
    )


def _fold_items(case: Case, emission: Emission, work: Reconciled, now: datetime) -> None:
    items = work.items
    kept = {i.item_id for i in emission.items}  # what this producer still says: never superseded or retracted
    swapped: list[CaseItem] = []
    for raw in sorted(emission.items, key=lambda i: i.item_id):
        item = _tagged(raw, emission.producer)
        slot = next((n for n, i in enumerate(items) if i.item_id == item.item_id), None)
        if slot is not None:
            current = items[slot]
            if current.producer is None and emission.complete and current.withdrawn_because is None:
                # A case stored before producers were kept: the one that says it again owns it from now on.
                current = items[slot] = current.model_copy(
                    update={
                        "producer": emission.producer,
                        **({} if current.subject else {"subject": item.subject}),
                    }
                )
            work.twins[item.item_id] = current
            if current.withdrawn_because is None or person_owns(case, current):
                continue  # already there (or the person's decision): left as it is
            by = next((i for i in items if i.item_id == current.superseded_by), None)
            if by is not None and by.state is not ProposalState.REJECTED:
                continue  # its replacement still stands: bringing it back would put the two against each other
        else:
            twin = twin_of(items, item)
            if twin is not None:
                work.twins[item.item_id] = twin
                kept.add(twin.item_id)
                continue
        work.twins[item.item_id] = None
        rivals = [
            i
            for i in items
            if i.item_id != item.item_id
            and i.item_id not in kept
            and i.withdrawn_because is None
            and i.state is not ProposalState.REJECTED
            and item_subject(i) == item.subject
        ]
        holding = [i for i in rivals if _holds_its_place(case, i, emission)]
        if holding:  # a swap to approve: it waits, whatever the producer thought of it
            item = item.model_copy(
                update={
                    "replaces": holding[0].item_id,
                    "state": ProposalState.PENDING,
                    "decided_by": None,
                    "decided_at": None,
                }
            )
            swapped.append(item)
            work.events.append(left_out_event("swap", item, replaces=holding[0].item_id))
        for rival in rivals:
            if rival in holding:
                continue
            items[items.index(rival)] = take_back(rival, now, REPLACED, by=item.item_id)
            work.events.append(left_out_event("superseded", rival, by=item.item_id))
        if slot is not None:
            items[slot] = item
        else:
            items.append(item)
        work.added.append(item)
    _pair_replacements(case, emission, work, swapped)
    if emission.complete and emission.producer not in AI_PRODUCERS:
        for n, current in enumerate(items):
            if (
                _made_by(current.producer, emission.producer)
                # the AI's items are taken back by name, never by the author's omission
                and not (emission.producer == AUTHOR and current.source is ProposalSource.AI)
                and emission.in_scope(current.goal_id)
                and (
                    current.state is ProposalState.PENDING
                    or required_by_goal(current)
                    # in the plan and nobody's decision: the author takes it back when it no longer suggests it
                    or (emission.producer == AUTHOR and current.state is ProposalState.ACCEPTED)
                )
                and current.withdrawn_because is None
                and current.item_id not in kept
                and not person_owns(case, current)
                and not _skipped(emission, current.kind)
            ):
                items[n] = take_back(current, now, NO_LONGER_EMITTED)
                work.events.append(left_out_event("retracted", current))


def _pair_replacements(case: Case, emission: Emission, work: Reconciled, swapped: list[CaseItem]) -> None:
    """The chat names a change of the person's it replaces without saying which new change does: when exactly one
    new change of the same kind in the same file is left unpaired, that is the swap."""
    taken = {i.replaces for i in swapped}
    for old_id in dict.fromkeys(emission.replaces):
        old = next((i for i in work.items if i.item_id == old_id), None)
        if (
            old is None
            or old_id in taken
            or old.state is ProposalState.REJECTED
            or not _holds_its_place(case, old)
        ):
            continue
        fits = [
            i
            for i in work.added
            if i.replaces is None and i.kind == old.kind and i.upload_id == old.upload_id and i is not old
        ]
        if len(fits) != 1:
            continue
        paired = fits[0].model_copy(
            update={
                "replaces": old_id,
                "state": ProposalState.PENDING,
                "decided_by": None,
                "decided_at": None,
            }
        )
        work.items[work.items.index(next(i for i in work.items if i.item_id == paired.item_id))] = paired
        work.added[work.added.index(fits[0])] = paired
        taken.add(old_id)
        work.events.append(left_out_event("swap", paired, replaces=old_id))


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
def _chosen_gone(current: CaseQuestion, emitted: CaseQuestion) -> bool:
    """The answer given is to an option the question no longer offers (the same words doing the same thing)."""
    chosen = next((o for o in current.options if o.option_id == current.answer), None)
    if chosen is None:
        return True
    return not any(
        o.label == chosen.label and (o.effect_digest or "") == (chosen.effect_digest or "")
        for o in emitted.options
    )


def _fold_questions(
    emission: Emission, work: Reconciled, refit: Callable[[CaseQuestion, Any], CaseQuestion] | None
) -> None:
    questions = work.questions
    emitted_ids = {q.question_id for q, _e in emission.questions}
    emitted_subjects = {question_subject(q) for q, _e in emission.questions}
    for emitted, effects in sorted(emission.questions, key=lambda pair: pair[0].question_id):
        if emitted.producer is None:
            emitted = emitted.model_copy(update={"producer": emission.producer})
        slot = next((n for n, q in enumerate(questions) if q.question_id == emitted.question_id), None)
        if slot is None:
            # The same matter asked under another id (another producer's wording): the unanswered one is replaced.
            rival = next(
                (
                    n
                    for n, q in enumerate(questions)
                    if q.answer is None
                    and q.question_id not in emitted_ids
                    and question_subject(q) == question_subject(emitted)
                ),
                None,
            )
            if rival is not None:
                del questions[rival]
            questions.append(emitted)
            work.asked.append((emitted, effects))
            continue
        current = questions[slot]
        if current.answer is None:
            if current.producer is None and emission.complete:
                current = questions[slot] = current.model_copy(update={"producer": emission.producer})
            if options_digest(current) != options_digest(emitted):
                again = emitted.model_copy(update={"producer": current.producer or emitted.producer})
                questions[slot] = again
                work.asked.append((again, effects))
                work.reasked.add(again.question_id)
            continue
        if emitted.answer is None and _chosen_gone(current, emitted):
            when = current.answered_at
            label = next((o.label for o in current.options if o.option_id == current.answer), "")
            note = SimpleNamespace(answered_at=when, option_label=label)
            again = emitted if refit is None or when is None else refit(emitted, note)
            questions[slot] = again.model_copy(update={"producer": current.producer or emitted.producer})
            work.asked.append((questions[slot], effects))
            work.reasked.add(emitted.question_id)
    if emission.complete:
        gone = [
            q
            for q in questions
            if _made_by(q.producer, emission.producer)
            and q.answer is None
            and emission.in_scope(q.goal_id)
            and q.question_id not in emitted_ids
            and question_subject(q) not in emitted_subjects
            and not _skipped(emission, q.code)
        ]
        work.events.extend(
            EventDraft(
                kind=TimelineKind.ITEM_DECIDED,
                upload_id=q.upload_id,
                item_id=q.question_id,
                details={"state": "rejected", "by": "agent", "withdrawn": True, "why": "question retracted"},
            )
            for q in gone
        )
        questions[:] = [
            q
            for q in questions
            if not (
                _made_by(q.producer, emission.producer)
                and q.answer is None
                and emission.in_scope(q.goal_id)
                and q.question_id not in emitted_ids
                and question_subject(q) not in emitted_subjects
                and not _skipped(emission, q.code)
            )
        ]


# ---------------------------------------------------------------------------
# Doubts
# ---------------------------------------------------------------------------
def _fold_doubts(emission: Emission, work: Reconciled) -> None:
    doubts = work.doubts
    emitted_ids = {d.doubt_id for d in emission.doubts}
    emitted_subjects = {doubt_subject(d) for d in emission.doubts}
    for raw in sorted(emission.doubts, key=lambda d: d.doubt_id):
        doubt = raw.model_copy(update={"producer": raw.producer or emission.producer})
        slot = next((n for n, d in enumerate(doubts) if d.doubt_id == doubt.doubt_id), None)
        if slot is None:
            for n, other in enumerate(doubts):
                if (
                    other.status == "open"
                    and other.doubt_id not in emitted_ids
                    and doubt_subject(other) == doubt_subject(doubt)
                ):
                    doubts[n] = other.model_copy(update={"status": "superseded", "closed_because": REPLACED})
            doubts.append(doubt)
        elif doubts[slot].status != "open":
            doubts[slot] = doubt  # it holds again
        elif (doubts[slot].producer is None or _made_by(doubts[slot].producer, emission.producer)) and _words(
            doubts[slot]
        ) != _words(doubt):
            doubts[slot] = doubt  # the same doubt with newer words: one doubt, not two
        elif doubts[slot].producer is None and emission.complete:
            doubts[slot] = doubts[slot].model_copy(
                update={"producer": emission.producer}
            )  # stored before producers
    if emission.complete:
        for n, doubt in enumerate(doubts):
            if (
                doubt.status == "open"
                and _made_by(doubt.producer, emission.producer)
                and doubt.doubt_id not in emitted_ids
                and doubt_subject(doubt) not in emitted_subjects
                and not _skipped(emission, doubt.doubt_id)
            ):
                doubts[n] = doubt.model_copy(
                    update={"status": "resolved", "closed_because": NO_LONGER_EMITTED}
                )
                work.events.append(
                    EventDraft(
                        kind=TimelineKind.ITEM_DECIDED,
                        upload_id=doubt.upload_id,
                        item_id=doubt.doubt_id,
                        details={
                            "state": "rejected",
                            "by": "agent",
                            "withdrawn": True,
                            "why": "doubt resolved",
                        },
                    )
                )


def _words(doubt: CaseDoubt) -> tuple[str, str, str]:
    return doubt.because, doubt.would_do, doubt.if_not


def _close_doubts_of_gone_items(work: Reconciled) -> None:
    """A doubt about a change that was taken back is about nothing now."""
    gone = {i.item_id for i in work.items if i.withdrawn_because is not None}
    for n, doubt in enumerate(work.doubts):
        if doubt.status == "open" and doubt.item_id in gone:
            work.doubts[n] = doubt.model_copy(
                update={"status": "superseded", "closed_because": "the change it was about was taken back"}
            )
