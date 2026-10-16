"""One approval, however often the plan comes back to it (the v1 flow: the files are joined, the goal is chosen).

A person approves the plan once. Later in the same visit the plan is made again (the joined table is read, the goal
adds its checks) and comes back to APPROVE. Asking for the whole plan again feels like three approvals, so:

* **What is the same stays approved.** At every approval the visit keeps short digests of each approved step and each
  sentence (`CaseVisit.approved_before`). A step that comes back as the same step, or with the same sentence, is
  *carried*: the screen folds it under "Already approved". Only what is new or changed is open.
* **What is new is said in one line**, with the reason ("We joined your files into one table; 1 new thing to check",
  "For this campaign we also need: who agreed to be contacted").
* **When what is new is only sure fixes the rules found** (a value read as a number, a date), with no stop and no
  flag, the visit carries on by itself and says so on the timeline (:func:`carries_on`). Anything with a stop or a
  flag, a change that needs its own yes, and everything about meaning (the ID, the outcome, who agreed to be
  contacted) is asked: a person's yes.

Nothing here writes, and nothing here holds a cell value: ids, digests, sentences the rules wrote and counts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor.case import Case, CaseItem, CaseQuestion, payload_digest
from engine.doctor.protocol import AGENT, DEFAULT_SETTINGS, approve_class_of
from engine.doctor.visit_contracts import CaseVisit, PlanCheck

__all__ = ["Carry", "approved_marks", "carries_on", "carry_of", "new_line"]

_ALSO: Final[dict[str, str]] = {
    "consent_column": "who agreed to be contacted",
    "outcome_column": "what counts as a yes",
    "as_of_column": "the date each row describes",
}
"""What a goal's new item is about, in the words of the one line (meaning: always a person's yes)."""
_ANSWERS: Final[str] = "answers"


@dataclass(frozen=True)
class Carry:
    """The live items of the active section, split by what the person already approved."""

    approved: tuple[CaseItem, ...]
    """The same step or the same sentence as approved before, with nothing flagged now: folded."""
    gone: tuple[CaseItem, ...]
    """Items of a file that was made into another table: the table's own items stand for them."""
    new: tuple[CaseItem, ...]
    """New or changed: open."""


def _mark(*parts: Any) -> str:
    return payload_digest({"p": list(parts)})[:16]


def marks(item: CaseItem) -> tuple[str, str]:
    """The digests of an item's step (what it does, whichever file it is on) and of its sentence."""
    step = item.payload.get("step")
    what: Any = (
        [step.get("kind"), step.get("column"), step.get("new_column"), step.get("params")]
        if isinstance(step, dict)
        else {k: v for k, v in item.payload.items() if k != "upload_id"}
    )
    return _mark(item.goal_id, item.kind, "step", what), _mark(
        item.goal_id, item.kind, "sentence", item.title
    )


def _answers_mark(questions: Sequence[CaseQuestion]) -> str:
    given = sorted(
        (q.question_id, q.answer)
        for q in questions
        if q.answer is not None and not q.assumed and q.standing_answer_id is None
    )
    return _mark(_ANSWERS, given)


def _is_note(item: CaseItem) -> bool:
    return (
        item.kind == "data_check"
        and item.source is ProposalSource.RULES
        and item.payload.get("level") == "note"
    )


def approved_marks(items: Sequence[CaseItem], questions: Sequence[CaseQuestion]) -> tuple[str, ...]:
    """What an approval of `items` adds to `CaseVisit.approved_before`: each ticked step and each check the person
    was shown, and the answers they gave."""
    found = [_answers_mark(questions)]
    for item in items:
        if (item.state is ProposalState.ACCEPTED or item.kind == "data_check") and not item.parked_for:
            found.extend(marks(item))
    return tuple(dict.fromkeys(found))


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def alerted(check: PlanCheck | None, items: Sequence[CaseItem]) -> tuple[frozenset[str], bool]:
    """The items with a stop or a flag now (the guards' findings, a step's losses, what the rules put on the item),
    and whether a stop or flag is about the whole plan."""
    ids: set[str] = set()
    whole = False
    if check is not None:
        for found in check.findings:
            if found.level in {"stop", "flag"}:
                if found.item_id:
                    ids.add(found.item_id)
                else:
                    whole = True
        steps = check.ledger.get("steps")
        for row in steps if isinstance(steps, list) else []:
            if not isinstance(row, dict):
                continue
            item_id = row.get("item_id")
            if not isinstance(item_id, str):
                continue
            emptied = _positive(row.get("rows_in")) and row.get("rows_out") == 0
            if emptied or _positive(row.get("customers_lost")) or _positive(row.get("customers_duplicated")):
                ids.add(item_id)
    for item in items:
        p = item.payload
        if (
            _positive(p.get("ids_merged"))
            or _positive(p.get("contact_excluded"))
            or p.get("alert_level") in {"stop", "flag"}
            or p.get("leak_suspect") is True
        ):
            ids.add(item.item_id)
    return frozenset(ids), whole


def carry_of(
    case: Case,
    items: Sequence[CaseItem],
    check: PlanCheck | None,
    *,
    confirmed: Callable[[Case, CaseItem], bool],
) -> Carry | None:
    """`items` (the active section) split by what was approved before in this round; None before any approval.
    `check` is the guards' count for the plan as it stands (None when not counted for it)."""
    before = set((case.visit or CaseVisit()).approved_before)
    if not before:
        return None
    files = {f.upload_id for f in case.files}
    flagged, _whole = alerted(check, items)
    approved: list[CaseItem] = []
    gone: list[CaseItem] = []
    new: list[CaseItem] = []
    for item in items:
        if item.state is ProposalState.REJECTED:
            continue
        needs_yes = approve_class_of(item.kind, item.payload, DEFAULT_SETTINGS) is not None and not confirmed(
            case, item
        )
        same = bool(before & set(marks(item)))
        shown = (item.state is ProposalState.ACCEPTED or item.kind == "data_check") and not item.parked_for
        if same and shown and not needs_yes and item.item_id not in flagged:
            approved.append(item)
        elif (
            item.upload_id is not None
            and item.upload_id not in files
            and not (needs_yes and item.state is ProposalState.ACCEPTED)
        ):
            gone.append(item)  # its file is now part of another table; what it said is in that table's items
        else:
            new.append(item)
    return Carry(tuple(approved), tuple(gone), tuple(new))


def _plain_fix(item: CaseItem) -> bool:
    """A sure fix of a value the rules found and ticked, that nobody touched and that needs no yes of its own."""
    return (
        item.kind == "recipe_step"
        and item.source is ProposalSource.RULES
        and item.confidence is AgentConfidence.SURE
        and item.state is ProposalState.ACCEPTED
        and item.decided_by is None
        and item.standing_answer_id is None
        and not item.parked_for
        and approve_class_of(item.kind, item.payload, DEFAULT_SETTINGS) is None
    )


def carries_on(
    case: Case,
    items: Sequence[CaseItem],
    questions: Sequence[CaseQuestion],
    check: PlanCheck | None,
    *,
    confirmed: Callable[[Case, CaseItem], bool],
) -> Carry | None:
    """The carry when the visit may go on without asking again, else None: something was approved before in this
    round, everything new is a sure fix or a plain note, no stop or flag is raised, the person has not changed or
    unticked anything on this table, and the answers are the ones approved."""
    carry = carry_of(case, items, check, confirmed=confirmed)
    if carry is None or check is None:
        return None
    _ids, whole = alerted(check, items)
    if whole or any(f.level in {"stop", "flag"} for f in check.findings):
        return None
    if _answers_mark(questions) not in set((case.visit or CaseVisit()).approved_before):
        return None
    if not all(_plain_fix(i) or _is_note(i) for i in carry.new):
        return None
    files = {f.upload_id for f in case.files}
    touched = any(
        i.state is ProposalState.REJECTED
        and i.decided_by not in {None, AGENT}
        and (i.upload_id is None or i.upload_id in files)
        for i in items
    )
    return None if touched else carry


def new_line(case: Case, carry: Carry) -> str | None:
    """What is new and why, in one line; None when nothing is."""
    counted = [i for i in carry.new if not _is_note(i) and not i.parked_for]
    if not counted:
        return None
    thing = "new thing to check" if len(counted) == 1 else "new things to check"
    base = [i for i in counted if i.goal_id is None]
    goal = [i for i in counted if i.goal_id is not None]
    joined = {f.upload_id for f in case.files if f.original_upload_id}
    parts: list[str] = []
    if base and any(i.upload_id in joined for i in base):
        parts.append(f"We joined your files into one table; {len(base)} {thing}")
    elif base:
        parts.append(f"{len(base)} {thing} since you approved")
    if goal:
        named = list(dict.fromkeys(_ALSO[i.kind] for i in goal if i.kind in _ALSO))
        if named:
            parts.append("For this campaign we also need: " + ", ".join(named))
        else:
            parts.append(f"For this campaign, {len(goal)} {thing}")
    return ". ".join(parts) + "."
