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
  to send back with Approve (exact-text consent, M246), naming the goal as the screens name it;
* **the APPROVE-class items** of the active section (`engine.doctor.protocol.approve_class_of`): the role their
  own yes needs, why, the exact sentence ("I agree to this sentence"), whether it is given, whether someone
  else must give it (separation of duties), and whether the person reading may give it (`can_agree`, by the rule
  `confirm` applies: `engine.doctor.protocol.confirmation_refusal`), so nobody is offered a button that can only
  be refused;
* **the remembered answers** used here, with the date they were given (`StandingAnswerStore`) and whether they
  were revoked since, and the answers given here that are **remembered for next time** (`remembered`), each by
  its question, so the screen says so where it was given and offers Revoke there;
* **a plain note** under `ready_with_warnings` (`ScreenVerdict.note`), so "Ready, with notes" always shows one even
  when every warning's own sentence is technical;
* **the hand-off** once the visit handed the data on: the file, its ID column and its outcome column, which is
  what `POST /runs` takes (the wizard and a goal's setup start the run with it, as Guided setup's Approve did).

* **the data** (`preview`, `engine.doctor.preview`): the first rows of each file before and after the ticked steps,
  which columns changed, personal columns withheld and every other cell masked; counted once per plan. Its rows
  per step also fill `ledger.steps[].samples`.

Nothing here writes. Nothing here holds a cell value but those masked preview rows: ids, column names the case
already holds, counts, codes and the sentences the rules wrote.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field

from engine.access.roles import Principal
from engine.agent.contracts import ProposalState
from engine.config import ConfigError, StrictBase
from engine.doctor.carry import carry_of, new_line
from engine.doctor.case import Case, CaseError, CaseStore, TimelineEvent, TimelineKind
from engine.doctor.diagnose import LoadedFile
from engine.doctor.plan_check import ledger_view, plan_digest
from engine.doctor.preview import PreviewView, cached_preview, preview_rows
from engine.doctor.protocol import (
    DEFAULT_SETTINGS,
    approve_class_of,
    confirmation_refusal,
    item_sentence,
    role_for,
)
from engine.doctor.readiness_contracts import DataReadinessReport
from engine.doctor.standing import StandingAnswerStore, answer_id_for
from engine.doctor.visit import (
    active_goal,
    active_section,
    approval_goal_name,
    approval_sentence,
    confirmed,
    item_proposers,
    open_blocking,
    prescription_digest,
    stored_brief,
)
from engine.doctor.visit_contracts import HandOffRecord
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger

__all__ = [
    "CASES_LISTED",
    "CaseScreen",
    "CaseScreenView",
    "CaseSummary",
    "ScreenApproveClass",
    "ScreenCarried",
    "ScreenRemembered",
    "ScreenStanding",
    "ScreenStart",
    "ScreenVerdict",
    "VerdictKey",
    "build_screen",
    "case_summaries",
    "screen_view",
]

_LOG = get_logger(__name__)

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

WARNINGS_NOTE: Final[str] = (
    "Data Doctor found a few small things to know about this data. None of them stops this goal."
)
"""The plain note `ready_with_warnings` always carries (the warnings' own sentences may be technical)."""


class ScreenVerdict(StrictBase):
    """The verdict of §10.3 with its one next action, in plain words."""

    key: VerdictKey
    title: str = Field(description="The verdict in a few plain words.")
    next: str = Field(description="What to do now, one sentence.")
    reasons: tuple[str, ...] = Field(
        default=(), description="The readiness report's own sentences (the run's checks, the probes)."
    )
    technical: str | None = Field(default=None, description="The report's reason, with its codes.")
    note: str | None = Field(
        default=None, description="`ready_with_warnings`: one plain sentence, shown when no reason is plain."
    )


class ScreenApproveClass(StrictBase):
    """One accepted or pending APPROVE-class item of the active section: it needs its own yes."""

    item_id: str
    role: Literal["analyst", "approver"] = Field(description="The role the yes needs.")
    why: str = Field(description='Why it needs one ("it decides who may be contacted").')
    sentence: str = Field(description="The exact sentence agreed to; sent back word for word.")
    confirmed: bool = Field(description="Whether its own yes is recorded for what it says now.")
    separate: bool = Field(description="Whether someone other than who proposed or answered it must agree.")
    can_agree: bool = Field(
        default=True,
        description="Whether the person reading may give the yes (their role; not its proposer under separation).",
    )
    cannot_agree: Literal["role", "separation"] | None = Field(
        default=None, description="Why not: they lack the role, or they proposed or answered it themselves."
    )


class ScreenStanding(StrictBase):
    """A remembered answer applied in this case, with the date it was given."""

    answer_id: str
    title: str = Field(description="What the answer does, in one plain sentence.")
    option_label: str = Field(description="The answer chosen, as the person read it.")
    column: str | None = None
    answered_at: datetime = Field(description="When it was given (the date the screen shows).")
    revoked: bool = Field(description="Revoked since: it is asked again next time.")


class ScreenRemembered(StrictBase):
    """An answer given in this case that is remembered for the client's next files (M246's standing answers)."""

    question_id: str
    answer_id: str
    answered_at: datetime = Field(description="When it was remembered (the date the screen shows).")
    revoked: bool = Field(description="Revoked since: it is not used next time.")


class ScreenStart(StrictBase):
    """What the hand-off starts with: the body of `POST /runs` (or `POST /uplift/runs`)."""

    target: Literal["runs", "uplift_runs"]
    goal_id: str
    upload_id: str
    primary_key: str | None
    outcome: str


class ScreenCarried(StrictBase):
    """The plan after a first approval (`engine.doctor.carry`): what is the same as approved stays approved."""

    approved: tuple[str, ...] = Field(
        description="Items that are the same step or sentence as approved before, nothing flagged now: the screen "
        'folds them under "Already approved".'
    )
    gone: tuple[str, ...] = Field(
        default=(), description="Items of files that were made into another table: not shown again."
    )
    line: str | None = Field(
        default=None, description="What is new and why, in one line; none when nothing is."
    )
    last_month: bool = Field(
        default=False,
        description="The approved steps are last month's, for the same kind of table (a new case of the same "
        'client): the screen says "Same as last month" instead of "Already approved".',
    )


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
    remembered: tuple[ScreenRemembered, ...] = Field(
        default=(), description="Answers given here that are remembered for next time, by question."
    )
    ledger: dict[str, Any] | None = Field(
        default=None,
        description="The guards' count of the accepted steps on the case's sample (`engine.doctor.plan_check`): "
        "rows and customers in and out, the outcome rate before and after, a receipt per step (`steps[].item_id`) "
        "and the findings (`level` stop, flag or note, each with its `key`). None until the plan is counted. The "
        "plan screen reads these names; a stop is lowered to a flag once a person said yes to it.",
    )
    preview: PreviewView | None = Field(
        default=None,
        description="The first rows of each file before and after the ticked steps (the person's and the agent's), "
        "personal columns withheld and every other cell masked, with which columns changed "
        "(`engine.doctor.preview`). None when the screen was not given the files to read.",
    )
    carried: ScreenCarried | None = Field(
        default=None,
        description="Once a plan was approved and comes back (the files joined, the goal chosen): what is the "
        "same stays approved and only what is new is open.",
    )
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
    upload_ids: tuple[str, ...] = Field(
        default=(), description="The uploads the check holds (each file's upload and the one it came from)."
    )
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
                upload_ids=tuple(
                    dict.fromkeys(i for f in case.files for i in (f.upload_id, f.original_upload_id) if i)
                ),
                updated_at=case.updated_at,
            )
        )
        if len(out) >= CASES_LISTED:
            break
    return tuple(out)


def build_screen(
    storage: Storage,
    client_id: str,
    case_id: str,
    *,
    config_root: Path | None,
    principal: Principal | None = None,
    load_files: Callable[[Case], Sequence[LoadedFile]] | None = None,
) -> CaseScreen:
    """The case, its timeline and its view (`CASE_NOT_FOUND` / `CASE_UNREADABLE` as `CaseStore.get`), for
    `principal` (who reads it; None: whether they may give a yes is not judged here). With `load_files` (the
    visit's own loader) the view carries the data preview."""
    store = CaseStore(storage)
    case = store.get(client_id, case_id)
    return CaseScreen(
        case=case,
        timeline=store.timeline(client_id, case_id),
        view=screen_view(storage, case, config_root=config_root, principal=principal, load_files=load_files),
    )


def screen_view(
    storage: Storage,
    case: Case,
    *,
    config_root: Path | None,
    principal: Principal | None = None,
    load_files: Callable[[Case], Sequence[LoadedFile]] | None = None,
) -> CaseScreenView:
    """The view of one case (see the module docstring)."""
    goal = active_goal(case)
    separate = _approval_required(case, config_root)
    approve_class = _approve_class(case, separate=separate, principal=principal)
    visit = case.visit
    sentence = (
        approval_sentence(case, goal_name=approval_goal_name(storage, case))
        if case.state.value == "approve"
        else None
    )
    preview = _preview(case, load_files)
    return CaseScreenView(
        mode="goal" if goal is not None else "generic",
        goal_id=goal.goal_id if goal is not None else None,
        verdict=_verdict(storage, case, approve_class),
        approval_sentence=sentence,
        approve_class=approve_class,
        ledger=_with_samples(_ledger(case), preview),
        preview=preview,
        carried=_carried(case),
        standing=_standing(storage, case),
        remembered=_remembered(storage, case),
        start=_start(case, visit.hand_off if visit is not None else None),
        missing_for=visit.missing_for if visit is not None and goal is None else (),
    )


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------
def _ledger(case: Case) -> dict[str, Any] | None:
    """The plan's count for the plan as it stands now; None when the plan changed since it was counted."""
    visit = case.visit
    if visit is None or visit.plan_check is None:
        return None
    if visit.plan_check.digest != plan_digest(case.files, active_section(case)[0]):
        return None
    return ledger_view(visit.plan_check, visit.acknowledged)


def _preview(case: Case, load_files: Callable[[Case], Sequence[LoadedFile]] | None) -> PreviewView | None:
    """The data before and after the plan as it stands (once per plan). A file that cannot be read is no preview,
    never a screen that fails."""
    if load_files is None or not case.files:
        return None
    items, _ = active_section(case)
    try:
        return cached_preview(
            case.case_id, case.files, items, lambda: preview_rows(tuple(load_files(case)), items)
        )
    except Exception:
        _LOG.warning("preview_unavailable", exc_info=True)
        return None


def _with_samples(ledger: dict[str, Any] | None, preview: PreviewView | None) -> dict[str, Any] | None:
    """The ledger with each step's changed rows (`steps[].samples`, masked) where the preview has them."""
    if ledger is None or preview is None or not preview.step_samples:
        return ledger
    steps = [
        (
            {**step, "samples": _samples_view(preview.step_samples[step["item_id"]])}
            if step.get("item_id") in preview.step_samples
            else step
        )
        for step in ledger.get("steps") or []
    ]
    return {**ledger, "steps": steps}


def _samples_view(sample: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"before": list(sample.before), "after": list(sample.after)}
    if sample.personal_columns:
        out["personal_columns"] = list(sample.personal_columns)
    return out


_AWAITING: Final = frozenset({"ask", "treat", "preview", "approve", "converse"})


def _carried(case: Case) -> ScreenCarried | None:
    """What of the plan was approved before in this round, while the plan waits to be approved again."""
    if case.state.value not in _AWAITING:
        return None
    items, _ = active_section(case)
    visit = case.visit
    counted = visit.plan_check if visit is not None and _ledger(case) is not None else None
    carry = carry_of(case, items, counted, confirmed=confirmed)
    if carry is None:
        return None
    return ScreenCarried(
        approved=tuple(i.item_id for i in carry.approved),
        gone=tuple(i.item_id for i in carry.gone),
        line=new_line(case, carry),
        last_month=visit is not None and visit.carry_source is not None,
    )


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


def _approve_class(
    case: Case, *, separate: bool, principal: Principal | None
) -> tuple[ScreenApproveClass, ...]:
    items, _ = active_section(case)
    out: list[ScreenApproveClass] = []
    for item in items:
        if item.state is ProposalState.REJECTED:
            continue
        rule = approve_class_of(item.kind, item.payload, DEFAULT_SETTINGS)
        if rule is None:
            continue
        role = role_for(rule, approval_required=separate)
        sentence = item_sentence(item.title, item.payload)
        why_not: Literal["role", "separation"] | None = None
        if principal is not None:
            # The very rule `visit.confirm` applies, with the sentence as shown: what it would refuse, said first.
            refused = confirmation_refusal(
                rule=rule,
                approver=principal,
                sentence_read=sentence,
                sentence_now=sentence,
                proposed_by=item_proposers(case, item),
                approval_required=separate,
            )
            if refused is not None:
                why_not = "separation" if refused.code == "POLICY_ROLE_SEPARATION" else "role"
        out.append(
            ScreenApproveClass(
                item_id=item.item_id,
                role="approver" if role.value == "approver" else "analyst",
                why=rule.why,
                sentence=sentence,
                confirmed=confirmed(case, item),
                separate=separate,
                can_agree=why_not is None,
                cannot_agree=why_not,
            )
        )
    return tuple(out)


def _remembered(storage: Storage, case: Case) -> tuple[ScreenRemembered, ...]:
    """The answers given in this case (not taken from an earlier one, not assumed) that the client's memory holds
    for the same answer: what `visit.answer` remembered (`STANDING_REMEMBERED`)."""
    asked = [
        q
        for q in active_section(case)[1]
        if q.answer is not None and q.standing_answer_id is None and not q.assumed
    ]
    if not asked:
        return ()
    answers = {a.answer_id: a for a in StandingAnswerStore(storage).list(case.client_id)}
    out: list[ScreenRemembered] = []
    for question in asked:
        answer = answers.get(answer_id_for(case.client_id, question))
        if answer is None or answer.option_id != question.answer:
            continue
        out.append(
            ScreenRemembered(
                question_id=question.question_id,
                answer_id=answer.answer_id,
                answered_at=answer.answered_at,
                revoked=answer.revoked_at is not None,
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
    now = prescription_digest(case, stored_brief(storage, case))
    # The digest the verdict was computed for (a prescriber of its own writes another hash than the default digest).
    if (ref.plan_digest or case.prescriptions[-1].prescription_hash) != now:
        return None  # something changed since it was approved: the verdict is the visit's again
    try:
        key = readiness_key(
            client_id=case.client_id, case_id=case.case_id, goal=goal.goal_id, report_hash=ref.report_hash
        )
        return ref.verdict, storage.read_model(key, DataReadinessReport)
    except (StorageError, ValueError):
        return ref.verdict, None


_CODE_PREFIX = re.compile(r"(^|; )(Not ready: )?[A-Z][A-Z0-9_]{2,}: ")


def _plain(said: str) -> str:
    """A stored reason as the person reads it: no "Not ready:" and no "CODE:" in front of any of its parts."""
    return _CODE_PREFIX.sub(r"\1", said.removeprefix("Not ready: ")).strip()


def _reasons(report: DataReadinessReport, *, warnings: bool = False) -> tuple[str, ...]:
    """The report's own sentences: what fails (blocking checks, then probes), or with `warnings` what warns."""
    wanted = "warn" if warnings else "fail"
    found: list[str] = []
    for requirement in report.requirements:
        if requirement.status == wanted and (warnings or requirement.blocking) and requirement.message:
            found.append(_plain(requirement.message))
    for probe in report.probes:
        if probe.status == wanted:
            found.append(_plain(probe.message if warnings else (probe.fix or probe.message)))
    out: list[str] = []
    for text in found:
        if text and text not in out:
            out.append(text)
    return tuple(out[:_REASONS])


_PASSED: Final = frozenset({"ready", "ready_with_warnings", "no_goal"})


def _last_readiness_failure(storage: Storage, case: Case) -> tuple[tuple[str, ...], str | None] | None:
    """Why the last readiness check for the active goal did not pass, while the visit waits for approval again and no
    approval has come since: the plan changed, so the check no longer counts for it, but the person must still be told
    why. None when the last check passed, none ran, or an approval came after it. Plain reasons from the stored report
    of that very check when there is one, else the sentence the visit wrote."""
    goal = active_goal(case)
    if goal is None:
        return None
    try:
        events = [
            e
            for e in CaseStore(storage).timeline(case.client_id, case.case_id)
            if e.kind is TimelineKind.READINESS and e.item_id == goal.goal_id
        ]
    except (CaseError, StorageError, ValueError):
        return None
    if not events or events[-1].details.get("verdict") in _PASSED:
        return None
    last = events[-1]
    if any(a.approved_at >= last.at for a in case.approvals if not a.approve_class):
        return None
    from engine.doctor.readiness import readiness_key

    ref = next((r for r in case.readiness if r.goal_id == goal.goal_id), None)
    # Only the report this check made: an older check's report belongs to an older plan.
    if ref is not None and ref.report_hash == last.details.get("report_hash"):
        try:
            key = readiness_key(
                client_id=case.client_id, case_id=case.case_id, goal=ref.goal_id, report_hash=ref.report_hash
            )
            report = storage.read_model(key, DataReadinessReport)
        except (StorageError, ValueError):
            report = None
        if report is not None and _reasons(report):
            return _reasons(report), report.verdict_reason
    said = str(last.details.get("reason") or "")
    plain = _plain(said)
    return ((plain,) if plain else ()), (said or None)


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
            reasons=(_plain(visit.stopped.message),),
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
                note=WARNINGS_NOTE if warned else None,
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
        failed = _last_readiness_failure(storage, case)
        if failed is not None:
            reasons, technical = failed
            return ScreenVerdict(
                key="not_ready",
                title="Not ready yet",
                next="The last check said the data is not ready for this goal. Here is what it found. "
                "Fix it below, then approve again.",
                reasons=reasons,
                technical=technical,
            )
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
