"""What the Data Doctor screen shows of a case (Plan O M247, DEC-1817; the design §13).

The screen (`ui/modules/datadoctor/`) is ONE component opened in three places: the Data page, the "New campaign"
wizard's Customers step and a goal's setup. It reads the case itself (`engine.doctor.case.Case`, as `GET
/cases/{case_id}` answers it) and, beside it, what only the server can say without guessing - computed here from
the rules the visit and the protocol already use, never re-implemented in the browser:

* **the verdict** (the design §10.3) with the one next action, in plain words: `needs_files`, `working`,
  `needs_answers`, `needs_approval`, `not_ready`, `ready`, `ready_with_warnings`, or `choose_goal` (the generic
  mode is done: pick a goal to check the data against it). `not_ready` carries the readiness report's own
  sentences (the run's checks and the probes, M234) and the report's reason as the technical detail;
* **the approval sentence** (`engine.doctor.visit.approval_sentence`): what the person approves, word for word,
  to send back with Approve (exact-text consent, M246);
* **the APPROVE-class items** of the active section (`engine.doctor.protocol.approve_class_of`): the role their
  own yes needs, why, the exact sentence ("I agree to this sentence"), whether it is given, and whether someone
  else must give it (separation of duties);
* **the remembered answers** used here, with the date they were given (`StandingAnswerStore`) and whether they
  were revoked since;
* **the hand-off** once the visit handed the data on: the file, its ID column and its outcome column, which is
  what `POST /runs` takes (the wizard and a goal's setup start the run with it, as Guided setup's Approve did).

Nothing here writes. Nothing here holds a cell value: ids, column names the case already holds, counts, codes and
the sentences the rules wrote.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from engine.agent.contracts import ProposalState
from engine.config import ConfigError, StrictBase
from engine.doctor.case import Case, CaseStore, TimelineEvent
from engine.doctor.protocol import DEFAULT_SETTINGS, approve_class_of, item_sentence, role_for
from engine.doctor.readiness_contracts import DataReadinessReport
from engine.doctor.standing import StandingAnswerStore
from engine.doctor.visit import (
    active_goal,
    active_section,
    approval_sentence,
    confirmed,
    open_blocking,
    prescription_digest,
    stored_brief,
)
from engine.doctor.visit_contracts import HandOffRecord
from engine.storage import Storage, StorageError

__all__ = [
    "CASES_LISTED",
    "CaseScreen",
    "CaseScreenView",
    "CaseSummary",
    "ScreenApproveClass",
    "ScreenStanding",
    "ScreenStart",
    "ScreenVerdict",
    "VerdictKey",
    "build_screen",
    "case_summaries",
    "screen_view",
]

VerdictKey = Literal[
    "needs_files",
    "working",
    "needs_answers",
    "needs_approval",
    "not_ready",
    "ready",
    "ready_with_warnings",
    "choose_goal",
]

CASES_LISTED: Final[int] = 20
"""Cases `case_summaries` returns at most, newest change first."""

_REASONS: Final[int] = 4
"""Readiness sentences shown under a `not_ready` verdict at most."""


class ScreenVerdict(StrictBase):
    """The verdict of §10.3 with its one next action, in plain words."""

    key: VerdictKey
    title: str = Field(description="The verdict in a few plain words.")
    next: str = Field(description="What to do now, one sentence.")
    reasons: tuple[str, ...] = Field(
        default=(), description="The readiness report's own sentences (the run's checks, the probes)."
    )
    technical: str | None = Field(default=None, description="The report's reason, with its codes.")


class ScreenApproveClass(StrictBase):
    """One accepted or pending APPROVE-class item of the active section: it needs its own yes."""

    item_id: str
    role: Literal["analyst", "approver"] = Field(description="The role the yes needs.")
    why: str = Field(description='Why it needs one ("it decides who may be contacted").')
    sentence: str = Field(description="The exact sentence agreed to; sent back word for word.")
    confirmed: bool = Field(description="Whether its own yes is recorded for what it says now.")
    separate: bool = Field(description="Whether someone other than who proposed or answered it must agree.")


class ScreenStanding(StrictBase):
    """A remembered answer applied in this case, with the date it was given."""

    answer_id: str
    title: str = Field(description="What the answer does, in one plain sentence.")
    option_label: str = Field(description="The answer chosen, as the person read it.")
    column: str | None = None
    answered_at: datetime = Field(description="When it was given (the date the screen shows).")
    revoked: bool = Field(description="Revoked since: it is asked again next time.")


class ScreenStart(StrictBase):
    """What the hand-off starts with: the body of `POST /runs` (or `POST /uplift/runs`)."""

    target: Literal["runs", "uplift_runs"]
    goal_id: str
    upload_id: str
    primary_key: str | None
    outcome: str


class CaseScreenView(StrictBase):
    """Everything the screen needs beyond the case itself."""

    mode: Literal["generic", "goal"]
    goal_id: str | None
    verdict: ScreenVerdict
    approval_sentence: str | None = Field(
        default=None, description="While the visit waits for Approve: the sentence approved, word for word."
    )
    approve_class: tuple[ScreenApproveClass, ...] = ()
    standing: tuple[ScreenStanding, ...] = ()
    start: ScreenStart | None = None
    missing_for: tuple[str, ...] = Field(
        default=(), description="Goals the files are missing something for (no goal chosen yet)."
    )


class CaseScreen(StrictBase):
    """`GET /cases/{case_id}/screen`: the case, its timeline and the view, in one answer."""

    case: Case
    timeline: tuple[TimelineEvent, ...]
    view: CaseScreenView


class CaseSummary(StrictBase):
    """One line of `GET /data-doctor/cases`."""

    case_id: str
    title: str
    goal_id: str | None
    state: str
    files: tuple[str, ...] = Field(description="The files' names, in order.")
    updated_at: datetime


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def case_summaries(
    storage: Storage, client_id: str, *, goal_id: str | None = None
) -> tuple[CaseSummary, ...]:
    """The client's cases, newest change first; with `goal_id`, only those whose active goal it is."""
    out: list[CaseSummary] = []
    for case in CaseStore(storage).list(client_id):
        goal = active_goal(case)
        active = goal.goal_id if goal is not None else None
        if goal_id is not None and active != goal_id:
            continue
        out.append(
            CaseSummary(
                case_id=case.case_id,
                title=case.title,
                goal_id=active,
                state=case.state.value,
                files=tuple(f.file_name for f in case.files),
                updated_at=case.updated_at,
            )
        )
        if len(out) >= CASES_LISTED:
            break
    return tuple(out)


def build_screen(storage: Storage, client_id: str, case_id: str, *, config_root: Path | None) -> CaseScreen:
    """The case, its timeline and its view (`CASE_NOT_FOUND` / `CASE_UNREADABLE` as `CaseStore.get`)."""
    store = CaseStore(storage)
    case = store.get(client_id, case_id)
    return CaseScreen(
        case=case,
        timeline=store.timeline(client_id, case_id),
        view=screen_view(storage, case, config_root=config_root),
    )


def screen_view(storage: Storage, case: Case, *, config_root: Path | None) -> CaseScreenView:
    """The view of one case (see the module docstring)."""
    goal = active_goal(case)
    separate = _approval_required(case, config_root)
    approve_class = _approve_class(case, separate=separate)
    visit = case.visit
    sentence = approval_sentence(case) if case.state.value == "approve" else None
    return CaseScreenView(
        mode="goal" if goal is not None else "generic",
        goal_id=goal.goal_id if goal is not None else None,
        verdict=_verdict(storage, case, approve_class),
        approval_sentence=sentence,
        approve_class=approve_class,
        standing=_standing(storage, case),
        start=_start(case, visit.hand_off if visit is not None else None),
        missing_for=visit.missing_for if visit is not None and goal is None else (),
    )


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------
def _approval_required(case: Case, config_root: Path | None) -> bool:
    """The active goal's `governance.approval_required` (separation of duties); False with no goal."""
    from engine.config import load_use_case

    goal = active_goal(case)
    if goal is None:
        return False
    try:
        return bool(load_use_case(goal.goal_id, config_root).governance.approval_required)
    except ConfigError:
        return False


def _approve_class(case: Case, *, separate: bool) -> tuple[ScreenApproveClass, ...]:
    items, _ = active_section(case)
    out: list[ScreenApproveClass] = []
    for item in items:
        if item.state is ProposalState.REJECTED:
            continue
        rule = approve_class_of(item.kind, item.payload, DEFAULT_SETTINGS)
        if rule is None:
            continue
        role = role_for(rule, approval_required=separate)
        out.append(
            ScreenApproveClass(
                item_id=item.item_id,
                role="approver" if role.value == "approver" else "analyst",
                why=rule.why,
                sentence=item_sentence(item.title, item.payload),
                confirmed=confirmed(case, item),
                separate=separate,
            )
        )
    return tuple(out)


def _standing(storage: Storage, case: Case) -> tuple[ScreenStanding, ...]:
    if not case.standing_answers_used:
        return ()
    answers = {a.answer_id: a for a in StandingAnswerStore(storage).list(case.client_id)}
    out: list[ScreenStanding] = []
    seen: set[str] = set()
    for use in case.standing_answers_used:
        answer = answers.get(use.answer_id)
        if answer is None or use.answer_id in seen:
            continue
        seen.add(use.answer_id)
        out.append(
            ScreenStanding(
                answer_id=answer.answer_id,
                title=answer.title or answer.option_label,
                option_label=answer.option_label,
                column=answer.column,
                answered_at=answer.answered_at,
                revoked=use.revoked_at is not None or answer.revoked_at is not None,
            )
        )
    return tuple(out)


def _start(case: Case, hand_off: HandOffRecord | None) -> ScreenStart | None:
    """The run's file, ID column and outcome column, once the visit handed the data on to a goal."""
    if hand_off is None or hand_off.target == "choose_goal" or hand_off.goal_id is None:
        return None
    goal_id = hand_off.goal_id
    outcome = next(
        (
            item
            for item in case.items
            if item.kind == "outcome_column"
            and item.goal_id == goal_id
            and item.state is ProposalState.ACCEPTED
        ),
        None,
    )
    if outcome is None:
        return None
    upload_id, column = outcome.payload.get("upload_id"), outcome.payload.get("column")
    if not isinstance(upload_id, str) or not isinstance(column, str):
        return None
    key = next(
        (
            item.payload.get("column")
            for item in case.items
            if item.kind == "key" and item.upload_id == upload_id and item.state is ProposalState.ACCEPTED
        ),
        None,
    )
    return ScreenStart(
        target=hand_off.target,
        goal_id=goal_id,
        upload_id=upload_id,
        primary_key=key if isinstance(key, str) else None,
        outcome=column,
    )


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _current_readiness(storage: Storage, case: Case) -> tuple[str, DataReadinessReport | None] | None:
    """The active goal's readiness verdict and report (when it can be read), when it was computed for what is
    approved now; None when there is none, or something changed since it was approved."""
    from engine.doctor.readiness import readiness_key

    goal = active_goal(case)
    if goal is None or not case.prescriptions:
        return None
    ref = next((r for r in case.readiness if r.goal_id == goal.goal_id), None)
    if ref is None:
        return None
    if case.prescriptions[-1].prescription_hash != prescription_digest(case, stored_brief(storage, case)):
        return None  # something changed since it was approved: the verdict is the visit's again
    try:
        key = readiness_key(
            client_id=case.client_id, case_id=case.case_id, goal=goal.goal_id, report_hash=ref.report_hash
        )
        return ref.verdict, storage.read_model(key, DataReadinessReport)
    except (StorageError, ValueError):
        return ref.verdict, None


def _reasons(report: DataReadinessReport, *, warnings: bool = False) -> tuple[str, ...]:
    """The report's own sentences: what fails (blocking checks, then probes), or with `warnings` what warns."""
    wanted = "warn" if warnings else "fail"
    found: list[str] = []
    for requirement in report.requirements:
        if requirement.status == wanted and (warnings or requirement.blocking) and requirement.message:
            found.append(requirement.message)
    for probe in report.probes:
        if probe.status == wanted:
            found.append(probe.message if warnings else (probe.fix or probe.message))
    out: list[str] = []
    for text in found:
        if text and text not in out:
            out.append(text)
    return tuple(out[:_REASONS])


def _verdict(storage: Storage, case: Case, approve_class: tuple[ScreenApproveClass, ...]) -> ScreenVerdict:
    visit = case.visit
    if not case.files or (visit is not None and visit.waiting_for == "files"):
        return ScreenVerdict(
            key="needs_files",
            title="Waiting for your file",
            next="Add the file of your customers to start.",
        )
    if visit is not None and visit.stopped is not None:
        return ScreenVerdict(
            key="not_ready",
            title="Not ready yet",
            next="Something stopped the check. Try again, or add a different file.",
            reasons=(visit.stopped.message,),
            technical=visit.stopped.code,
        )
    blocking = open_blocking(case)
    if blocking:
        return ScreenVerdict(
            key="needs_answers",
            title="Needs your answer",
            next=f"Answer {_plural(len(blocking), 'question', 'questions')} below to go on.",
        )
    waiting = [a for a in approve_class if not a.confirmed and _accepted(case, a.item_id)]
    if waiting:
        return ScreenVerdict(
            key="needs_approval",
            title="Needs your approval",
            next=f"{_plural(len(waiting), 'change needs', 'changes need')} your own yes before you approve.",
        )
    current = _current_readiness(storage, case)
    if current is not None:
        verdict, report = current
        warned = verdict == "ready_with_warnings"
        reasons = _reasons(report, warnings=warned) if report is not None else ()
        technical = report.verdict_reason if report is not None else None
        if verdict in {"ready", "ready_with_warnings"}:
            return ScreenVerdict(
                key="ready_with_warnings" if warned else "ready",
                title="Ready, with notes" if warned else "Ready",
                next="Your data is ready for this goal.",
                reasons=reasons if warned else (),
                technical=technical,
            )
        if verdict == "not_ready":
            return ScreenVerdict(
                key="not_ready",
                title="Not ready yet",
                next="The data cannot be used for this goal yet. Here is what is missing.",
                reasons=reasons,
                technical=technical,
            )
    state = case.state.value
    if state == "approve":
        return ScreenVerdict(
            key="needs_approval",
            title="Ready for your approval",
            next="Read the changes below, then approve them.",
        )
    if state in {"hand_off", "follow_up"} and active_goal(case) is None:
        return ScreenVerdict(
            key="choose_goal",
            title="Checked",
            next="Pick a goal to check this data against it.",
        )
    return ScreenVerdict(key="working", title="Checking", next="Data Doctor is looking at your data.")


def _accepted(case: Case, item_id: str) -> bool:
    return any(i.item_id == item_id and i.state is ProposalState.ACCEPTED for i in case.items)
