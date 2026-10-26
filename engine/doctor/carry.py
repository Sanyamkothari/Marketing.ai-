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

* **Next month, the same.** A new case of the same client whose files look like the table approved last month
  (:func:`last_month`, matched by columns the way the fingerprint matches last time's file) starts from that
  approval: the steps approved then are carried again, in the name of who approved, and the Data Doctor opens only
  what is new or changed. A step that cannot apply (its column is gone), a step a person left out, consent and
  outcome, and anything with a stop or flag are never carried; they are asked.

Nothing here writes, and nothing here holds a cell value: ids, digests, sentences the rules wrote and counts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor.case import Case, CaseApproval, CaseItem, CaseQuestion, payload_digest
from engine.doctor.protocol import AGENT, DEFAULT_SETTINGS, ProtocolSettings, approve_class_of
from engine.doctor.visit_contracts import CarrySource, CaseVisit, PlanCheck

if TYPE_CHECKING:
    from engine.doctor.diagnose import LoadedFile
    from engine.storage import Storage

__all__ = ["Carry", "approved_marks", "carries_on", "carry_of", "last_month", "new_line", "plain_fix"]

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
        else (
            [item.payload["shape"]]  # a join is the same join next month whatever the counts and uploads
            if "shape" in item.payload
            else {k: v for k, v in item.payload.items() if k != "upload_id"}
        )
    )
    return _mark(item.goal_id, item.kind, "step", what), _mark(
        item.goal_id, item.kind, "sentence", item.title
    )


def _answers_mark(questions: Sequence[CaseQuestion]) -> str:
    given = sorted(
        (q.question_id, q.answer) if q.answer_value is None else (q.question_id, q.answer, q.answer_value)
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
    settings: ProtocolSettings = DEFAULT_SETTINGS,
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
        needs_yes = approve_class_of(item.kind, item.payload, settings) is not None and not confirmed(
            case, item
        )
        step_mark, sentence_mark = marks(item)
        # a step is the same when it does the same (its sentence may be reworded); any other item by either mark
        same = step_mark in before or (
            not isinstance(item.payload.get("step"), dict) and sentence_mark in before
        )
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


_SAME_TABLE: Final[float] = 0.5
"""Share of a file's columns that a table approved last month has: below it, the file is another kind of table."""


def _step_column(item: CaseItem) -> str | None:
    step = item.payload.get("step")
    column = step.get("column") if isinstance(step, dict) else None
    return column if isinstance(column, str) else None


def last_month(
    storage: Storage, case: Case, files: Sequence[LoadedFile]
) -> tuple[tuple[str, ...], CarrySource] | None:
    """What the client approved last month for the same kind of table as `files`: the digests (the prior
    `approved_before`) and where they come from; None when there is nothing to go on.

    Same client, another case with a person's approval, whose kept fingerprint shares at least half of the columns
    of one of `files`. The newest such case wins."""
    from engine.doctor.case import CaseStore
    from engine.doctor.fingerprint import FingerprintStore

    prints = FingerprintStore(storage).prints(case.client_id)
    wanted = [cols for f in files if (cols := {str(c) for c in f.frame.columns})]
    if not prints or not wanted:
        return None
    best: tuple[Case, CaseApproval] | None = None
    for prior in CaseStore(storage).list(case.client_id):
        approvals = [a for a in prior.approvals if not a.approve_class]
        if (
            prior.case_id == case.case_id
            or not approvals
            or not (prior.visit and prior.visit.approved_before)
        ):
            continue
        uploads = {u for f in prior.files for u in (f.upload_id, f.original_upload_id) if u}
        looks_same = any(
            len(cols & {c.name for c in p.columns}) / len(cols) >= _SAME_TABLE
            for p in prints
            if p.upload_id in uploads
            for cols in wanted
        )
        if looks_same and (best is None or approvals[-1].approved_at > best[1].approved_at):
            best = (prior, approvals[-1])
    if best is None:
        return None
    prior, approval = best
    approved_before = (prior.visit or CaseVisit()).approved_before
    have = set().union(*wanted)
    # like with like: the files are raw now, so only the steps on last month's raw files say what is missing (a
    # step on a column the approved join built is not asked of raw files; the joined table is still compared with
    # last month's once it is built, because a carried approval keeps its source)
    built = {f.upload_id for f in prior.files if f.original_upload_id and not f.sheet}
    raw_now = not any(f.original_upload_id and not f.sheet for f in case.files)
    asked = [i for i in prior.items if (i.upload_id in built) is not raw_now]
    steps = [s for i in asked if isinstance(s := i.payload.get("step"), dict)]
    made = {s["new_column"] for s in steps if isinstance(s.get("new_column"), str)}
    columns = {c for i in asked if i.state is ProposalState.ACCEPTED and (c := _step_column(i))}
    declined = tuple(
        dict.fromkeys(
            marks(i)[0]
            for i in prior.items
            if i.state is ProposalState.REJECTED and i.decided_by not in {None, AGENT}
        )
    )
    source = CarrySource(
        case_id=prior.case_id,
        approval_id=approval.approval_id,
        approved_by=approval.approved_by,
        missing_columns=tuple(sorted(columns - have - made)),
        declined=declined,
    )
    return approved_before, source


def plain_fix(item: CaseItem, settings: ProtocolSettings = DEFAULT_SETTINGS) -> bool:
    """A sure fix of a value the rules found and ticked, that nobody touched and that needs no yes of its own."""
    return (
        item.kind == "recipe_step"
        and item.source is ProposalSource.RULES
        and item.confidence is AgentConfidence.SURE
        and item.state is ProposalState.ACCEPTED
        and item.decided_by is None
        and item.standing_answer_id is None
        and not item.parked_for
        and approve_class_of(item.kind, item.payload, settings) is None
    )


def carries_on(
    case: Case,
    items: Sequence[CaseItem],
    questions: Sequence[CaseQuestion],
    check: PlanCheck | None,
    *,
    confirmed: Callable[[Case, CaseItem], bool],
    settings: ProtocolSettings = DEFAULT_SETTINGS,
) -> Carry | None:
    """The carry when the visit may go on without asking again, else None: something was approved before in this
    round, everything new is a sure fix or a plain note, no stop or flag is raised, the person has not changed or
    unticked anything on this table, and the answers are the ones approved."""
    carry = carry_of(case, items, check, confirmed=confirmed, settings=settings)
    if carry is None or check is None:
        return None
    _ids, whole = alerted(check, items)
    if whole or any(f.level in {"stop", "flag"} for f in check.findings):
        return None
    if _answers_mark(questions) not in set((case.visit or CaseVisit()).approved_before):
        return None
    if not all(plain_fix(i, settings) or _is_note(i) for i in carry.new):
        return None
    source = (case.visit or CaseVisit()).carry_source
    if source is not None and (
        case.doubts  # last month's approval does not cover what moved in this month's file: a person looks
        or source.missing_columns
        or any(marks(i)[0] in source.declined for i in carry.new)
    ):
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
    source = (case.visit or CaseVisit()).carry_source
    lost = (
        "Last month's steps on "
        + ", ".join(f"‘{c}’" for c in source.missing_columns[:3])
        + (" and others" if len(source.missing_columns) > 3 else "")
        + " cannot be used: this file has no such column, so they are asked again."
        if source is not None and source.missing_columns
        else ""
    )
    if not counted:
        return lost or None

    def things(n: int) -> str:
        return f"{n} new thing to check" if n == 1 else f"{n} new things to check"

    base = [i for i in counted if i.goal_id is None]
    goal = [i for i in counted if i.goal_id is not None]
    joined = {f.upload_id for f in case.files if f.original_upload_id}
    parts: list[str] = []
    if base and any(i.upload_id in joined for i in base):
        parts.append(f"We joined your files into one table; {things(len(base))}")
    elif base:
        parts.append(f"{things(len(base))} since {'last month' if source is not None else 'you approved'}")
    if goal:
        named = list(dict.fromkeys(_ALSO[i.kind] for i in goal if i.kind in _ALSO))
        if named:
            parts.append("For this campaign we also need: " + ", ".join(named))
        else:
            parts.append(f"For this campaign, {things(len(goal))}")
    line = ". ".join(parts) + "."
    return f"{lost} {line}" if lost else line
