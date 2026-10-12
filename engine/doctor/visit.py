r"""The visit (Plan O M235, DEC-1805; the design §3.3-§3.4): Data Doctor's persisted state machine over one case.

The visit is copied from Plan K's planner pattern (`engine.agentic.planner`): **one step per transition, saved
after each, resumable, idempotent**. INTERRUPT means "waiting for the person".

```
INTAKE -> UNDERSTAND -> RECALL -> GOAL_BRIEF (a goal is chosen) -> TREAT
                               \-> TREAT (no goal yet)
TREAT -> ASK (a blocking question is open; INTERRUPT)      ASK -> TREAT (answered)
TREAT -> CONVERSE (the person chats) -> TREAT (proposals land pending)
TREAT -> GOAL_BRIEF (the person picks or changes a goal)
TREAT -> PREVIEW (nothing blocking open) -> APPROVE (INTERRUPT)   PREVIEW/APPROVE -> TREAT (an untick or edit)
APPROVE -> PRESCRIBE (the person approves) -> VERIFY -> HAND_OFF (ready) -> FOLLOW_UP (INTERRUPT)
VERIFY -> TREAT (not ready, with the named reason)        FOLLOW_UP -> INTAKE (next month's file arrives)
```

**Persisted per transition.** Every transition is one write of `case.json` (`CaseStore.update`, with its version
check and its cross-process lock): the new state, everything the state produced (items, questions, doubts,
evidence references, the diagnosis, the goal, the approval, the prescription and readiness references) and the
`state.changed` event, together. A state's work is computed from the case at version *n* and written as version
*n + 1*, so a process killed at any point either wrote the whole step or none of it: the next `resume` reads
the case and continues from the last state written. Nothing is lost (what was written stays; what was not is
computed again) and nothing is repeated (the rules are deterministic, every id is a content hash or a stable
name, and a step merges by id, never appends twice). Side files a step writes before its commit (evidence, the
brief) are content-addressed, so writing one again writes the same bytes. A person's action carries an optional
idempotency key (`op_id`); a retried request whose first attempt was written changes nothing.

**Generic mode and goal mode** (requirement 6; the design §3.1-§3.3). Without a goal the visit understands the
files, asks what does not depend on a goal (which column names each customer) and says what the data can power
and what is missing for the other goals (`what_can_this_power`, `data_request`). Choosing a goal adds a
**goal brief** (`engine.doctor.goal_brief`) to the same case and moves the visit to GOAL_BRIEF: nothing decided
is asked again, because a question's id names its subject, not the mode it was asked in, and every decided item
and answered question stays. Items and questions of a goal carry its `goal_id`: they are that goal's **section**.
Switching to another goal sets the first goal's section aside, it is not deleted; switching back makes it active
again exactly as it was.

**Rules first, and no AI needed.** Every state completes with no AI service connected. CONVERSE is Plan G's
`chat_turn` (:func:`plan_g_converse`); without an AI service the person's message is kept and the reply says the
chat needs one, and the visit goes back to TREAT. The AI only ever adds pending, unticked items.

**What later milestones fill in.** PREVIEW, PRESCRIBE, VERIFY and HAND_OFF call ports (:class:`VisitDeps`). The
defaults are the honest minimum today: PREVIEW counts what would change; PRESCRIBE hashes the approved items, the
answers and the brief (M243 writes the prescription itself); VERIFY runs M234's `check_readiness` on the file that
holds the goal's outcome (:func:`readiness_verify`); HAND_OFF records where the data goes and starts nothing.

Nothing here is reachable while `data_doctor.unified` is off (`api/routes/case_visit.py` is mounted only with it on).
Nothing here logs a value or a column name: counts, ids and codes only.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from pydantic import JsonValue

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agent.tools import TOOLS, AgentToolError
from engine.config import ConfigError
from engine.doctor.case import (
    Case,
    CaseApproval,
    CaseChatEntry,
    CaseDoubt,
    CaseError,
    CaseEvidenceRef,
    CaseFile,
    CaseGoal,
    CaseItem,
    CaseOption,
    CasePrescriptionRef,
    CaseQuestion,
    CaseReadinessRef,
    CaseStore,
    EventDraft,
    TimelineKind,
    VisitState,
    case_dir,
    evidence_key,
    payload_digest,
)
from engine.doctor.goal_brief import GOAL_BRIEF_TOOL, GoalBrief, load_goal_brief, register_goal_brief_tool
from engine.doctor.registry import (
    DataToolContext,
    DataToolKind,
    DataToolRegistry,
    DataToolResult,
    Exposure,
)
from engine.doctor.visit_contracts import CaseVisit, HandOffRecord, OptionEffect, VisitStop, WaitingFor
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.access.roles import Principal
    from engine.agent.tools import AgentContext
    from engine.doctor.diagnose import LoadedFile, Offer
    from engine.generative.guardrails import Guardrails
    from engine.storage import Storage

__all__ = [
    "AI_NEEDED_REPLY",
    "VISIT_CODES",
    "ConversePort",
    "ConverseResult",
    "HandOffPort",
    "PrescribePort",
    "PreviewPort",
    "PreviewSummary",
    "VerifyPort",
    "VerifyResult",
    "VisitDeps",
    "active_goal",
    "active_section",
    "add_files",
    "answer",
    "approval_sentence",
    "approve",
    "brief_key",
    "chat",
    "decide",
    "mode_of",
    "open_blocking",
    "plan_g_converse",
    "prescription_digest",
    "readiness_verify",
    "resume",
    "set_goal",
    "stored_brief",
    "visit_registry",
]

_LOG = get_logger(__name__)

VISIT_CODES: Final[frozenset[str]] = frozenset({"VISIT_NOT_WAITING", "VISIT_UNKNOWN_CHOICE"})
"""The codes this module raises (as `CaseError`); the integrator adds them to `PLAN_O_CODES` with help entries."""

AI_NEEDED_REPLY: Final[str] = (
    "The chat needs an AI service, and none is connected. Everything else works without one: the questions, "
    "the suggestions, the preview and the approval."
)
MAX_STEPS: Final[int] = 40
"""Automatic transitions one `resume` makes at most; the visit rests long before (a full pass is about 12)."""
MAX_OPS: Final[int] = 64
PREVIEW_ROWS: Final[int] = 1_000
"""Rows a preview tries a change on, per file (the design §3.4 PREVIEW)."""
_UNIQUE: Final[float] = 1.05
"""Rows per value at most for a column to name each row (as the key rules read a customer list)."""
AGENT: Final[str] = "agent:data-doctor"

_REST: Final[dict[VisitState, WaitingFor]] = {
    VisitState.ASK: "answer",
    VisitState.APPROVE: "approval",
    VisitState.FOLLOW_UP: "next_file",
}
_AFTER_RECALL: Final[frozenset[VisitState]] = frozenset(
    {
        VisitState.GOAL_BRIEF,
        VisitState.TREAT,
        VisitState.ASK,
        VisitState.CONVERSE,
        VisitState.PREVIEW,
        VisitState.APPROVE,
        VisitState.PRESCRIBE,
        VisitState.VERIFY,
        VisitState.HAND_OFF,
        VisitState.FOLLOW_UP,
    }
)
_CHAT_FROM: Final[frozenset[VisitState]] = frozenset(
    {VisitState.TREAT, VisitState.ASK, VisitState.PREVIEW, VisitState.APPROVE, VisitState.FOLLOW_UP}
)


# ---------------------------------------------------------------------------
# Ports: what later milestones fill in
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConverseResult:
    """One chat turn: the reply, the pending items it proposed, what the AI looked at, what it cost."""

    reply: str
    items: tuple[CaseItem, ...] = ()
    evidence: tuple[DataToolResult, ...] = ()
    sent: tuple[dict[str, JsonValue], ...] = ()
    model_calls: int = 0
    cost_usd: float = 0.0


class ConversePort(Protocol):
    def __call__(self, case: Case, message: str, files: tuple[LoadedFile, ...]) -> ConverseResult: ...


@dataclass(frozen=True)
class PreviewSummary:
    """What a preview found: counts only."""

    files: int
    rows_tried: int
    items_ticked: int
    items_pending: int
    details: Mapping[str, int | str] = field(default_factory=dict)


class PreviewPort(Protocol):
    def __call__(self, case: Case, files: tuple[LoadedFile, ...]) -> PreviewSummary: ...


class PrescribePort(Protocol):
    def __call__(self, case: Case, brief: GoalBrief | None) -> str: ...


@dataclass(frozen=True)
class VerifyResult:
    """VERIFY's verdict: `ready` and `ready_with_warnings` hand off; anything else goes back to TREAT."""

    verdict: str
    reason: str
    ref: CaseReadinessRef | None = None


class VerifyPort(Protocol):
    def __call__(
        self, case: Case, brief: GoalBrief | None, files: tuple[LoadedFile, ...]
    ) -> VerifyResult: ...


class HandOffPort(Protocol):
    def __call__(self, case: Case, brief: GoalBrief | None) -> HandOffRecord: ...


@dataclass(frozen=True)
class VisitDeps:
    """What a visit uses: the store, the registry, the files and the ports. Only `storage` and `load_files` are
    required; every port has a default that works with no AI service."""

    storage: Storage
    load_files: Callable[[Case], Sequence[LoadedFile]]
    """The case's files as the Doctor reads them (rows capped, profile, intake notes)."""
    registry: DataToolRegistry | None = None
    """None: :func:`visit_registry`."""
    offers: Sequence[Offer] = ()
    """The goals a generic visit checks the files against (what the data can power); empty: none."""
    config_root: Path | None = None
    converse: ConversePort | None = None
    """None: no AI service; the chat answers :data:`AI_NEEDED_REPLY` and the visit goes on."""
    preview: PreviewPort | None = None
    prescribe: PrescribePort | None = None
    verify: VerifyPort | None = None
    hand_off: HandOffPort | None = None
    clock: Callable[[], datetime] = utc_now
    store: CaseStore | None = None
    """None: a `CaseStore` over `storage` with `clock`."""

    def case_store(self) -> CaseStore:
        return self.store if self.store is not None else CaseStore(self.storage, clock=self.clock)

    def tools(self) -> DataToolRegistry:
        return self.registry if self.registry is not None else visit_registry()


@functools.cache
def visit_registry() -> DataToolRegistry:
    """M232's tools plus `load_goal_brief` (#35). The integrator moves #35 into `register_all`."""
    from engine.doctor.tools import register_all

    registry = DataToolRegistry()
    register_all(registry)
    register_goal_brief_tool(registry)
    return registry


# ---------------------------------------------------------------------------
# Reading a case
# ---------------------------------------------------------------------------
def active_goal(case: Case) -> CaseGoal | None:
    return next((goal for goal in case.goals if goal.active), None)


def mode_of(case: Case) -> Literal["generic", "goal"]:
    """`goal` while a goal is active, `generic` otherwise (the design §3.1-§3.2)."""
    return "goal" if active_goal(case) is not None else "generic"


def _in_section(goal_id: str | None, case: Case) -> bool:
    active = active_goal(case)
    return goal_id is None or (active is not None and goal_id == active.goal_id)


def active_section(case: Case) -> tuple[tuple[CaseItem, ...], tuple[CaseQuestion, ...]]:
    """The shared base and the active goal's section; other goals' sections are set aside."""
    return (
        tuple(item for item in case.items if _in_section(item.goal_id, case)),
        tuple(question for question in case.questions if _in_section(question.goal_id, case)),
    )


def open_blocking(case: Case) -> tuple[CaseQuestion, ...]:
    """Blocking questions of the active section that nobody has answered: they stop PREVIEW."""
    return tuple(q for q in active_section(case)[1] if q.blocking and q.answer is None)


def brief_key(client_id: str, case_id: str, goal_id: str, hash_: str) -> str:
    """`cases/<client>/<case>/briefs/<goal_id>_<hash16>.json`."""
    return f"{case_dir(client_id, case_id)}briefs/{goal_id}_{hash_[:16]}.json"


def stored_brief(storage: Storage, case: Case) -> GoalBrief | None:
    """The active goal's brief as GOAL_BRIEF froze it, or None (no goal, or not loaded yet)."""
    from engine.storage import StorageError

    goal = active_goal(case)
    if goal is None or goal.brief_hash is None:
        return None
    try:
        return storage.read_model(
            brief_key(case.client_id, case.case_id, goal.goal_id, goal.brief_hash), GoalBrief
        )
    except (StorageError, ValueError):
        return None  # GOAL_BRIEF loads and writes it again


def approval_sentence(case: Case) -> str:
    """The sentence the person approves, word for word (recorded with the approval)."""
    items, _ = active_section(case)
    ticked = sum(1 for item in items if item.state is ProposalState.ACCEPTED)
    goal = active_goal(case)
    what = f"{ticked} change{'s' if ticked != 1 else ''}"
    files = len(case.files)
    to = f" for the goal {goal.goal_id}" if goal is not None else ""
    return f"Prepare {files} file{'s' if files != 1 else ''} with {what}{to}. Nothing else changes."


# ---------------------------------------------------------------------------
# One run of the machine
# ---------------------------------------------------------------------------
class _Run:
    """One call's working memory: the files (loaded once), the tool calls made. Never persisted."""

    def __init__(self, deps: VisitDeps, actor: str) -> None:
        self.deps = deps
        self.actor = actor
        self.store = deps.case_store()
        self.registry = deps.tools()
        self.tool_calls = 0
        self._files: tuple[LoadedFile, ...] | None = None

    def files(self, case: Case) -> tuple[LoadedFile, ...]:
        if self._files is None or tuple(f.upload_id for f in self._files) != case.upload_ids():
            self._files = tuple(self.deps.load_files(case))
        return self._files

    def context(self, case: Case, **extra: Any) -> DataToolContext:
        files = extra.pop("files") if "files" in extra else self.files(case)
        return DataToolContext(
            caller="rules",
            storage=self.deps.storage,
            files=files,
            offers=tuple(self.deps.offers),
            config_root=self.deps.config_root,
            clock=self.deps.clock,
            actor=AGENT,
            **extra,
        )

    def call(self, name: str, given: Mapping[str, Any], ctx: DataToolContext) -> DataToolResult:
        self.tool_calls += 1
        return self.registry.call(name, given, ctx)


@dataclass
class _Step:
    """What one state produced, to be written with its transition."""

    to: VisitState
    updates: dict[str, Any] = field(default_factory=dict)
    visit: dict[str, Any] = field(default_factory=dict)
    events: list[EventDraft] = field(default_factory=list)
    evidence: list[DataToolResult] = field(default_factory=list)
    items: list[CaseItem] = field(default_factory=list)
    questions: list[tuple[CaseQuestion, tuple[OptionEffect, ...]]] = field(default_factory=list)
    doubts: list[CaseDoubt] = field(default_factory=list)
    model_calls: int = 0
    cost_usd: float = 0.0


def _upload_of(result: DataToolResult) -> str | None:
    value = result.input.get("upload_id")
    if isinstance(value, str):
        return value
    ids = result.input.get("upload_ids")
    if isinstance(ids, list) and len(ids) == 1 and isinstance(ids[0], str):
        return ids[0]
    return None


def _write(run: _Run, case: Case, step: _Step, *, op_id: str | None = None) -> Case:
    """Write `step` and its transition as the next version of `case` (one atomic write)."""
    storage = run.deps.storage
    now = run.deps.clock()

    def change(current: Case) -> tuple[Case, list[EventDraft]]:
        drafts: list[EventDraft] = []
        known = {ref.evidence_id for ref in current.evidence}
        refs = list(current.evidence)
        for result in step.evidence:
            if result.evidence_id in known:
                continue
            known.add(result.evidence_id)
            upload_id = _upload_of(result)
            document = {
                "evidence_id": result.evidence_id,
                "tool": result.tool,
                "upload_id": upload_id,
                "result": {"input": result.input, "output": result.output},
            }
            storage.write_bytes(
                evidence_key(current.client_id, current.case_id, result.evidence_id),
                _json_bytes(document),
            )
            refs.append(
                CaseEvidenceRef(
                    evidence_id=result.evidence_id, tool=result.tool, upload_id=upload_id, created_at=now
                )
            )
            drafts.append(
                EventDraft(
                    kind=TimelineKind.EVIDENCE_ADDED,
                    upload_id=upload_id,
                    item_id=result.evidence_id,
                    details={"tool": result.tool},
                )
            )
        items = list(current.items)
        have_items = {item.item_id for item in items}
        for item in step.items:
            if item.item_id in have_items:
                continue  # a decided item is never proposed again over the person's decision
            have_items.add(item.item_id)
            items.append(item)
            drafts.append(
                EventDraft(
                    kind=TimelineKind.ITEM_PROPOSED,
                    upload_id=item.upload_id,
                    item_id=item.item_id,
                    details={"source": item.source.value, "goal": item.goal_id},
                )
            )
        questions = list(current.questions)
        have_questions = {question.question_id for question in questions}
        visit = current.visit or CaseVisit()
        offers = dict(visit.offers)
        asked_per_file = dict(current.counters.questions_per_file)
        for question, effects in step.questions:
            if question.question_id in have_questions:
                continue  # asked once: an answered question is never asked again, in any mode
            have_questions.add(question.question_id)
            questions.append(question)
            offers[question.question_id] = effects
            if question.upload_id is not None:
                asked_per_file[question.upload_id] = asked_per_file.get(question.upload_id, 0) + 1
            drafts.append(
                EventDraft(
                    kind=TimelineKind.QUESTION_ASKED,
                    upload_id=question.upload_id,
                    item_id=question.question_id,
                    details={"code": question.code, "blocking": question.blocking, "goal": question.goal_id},
                )
            )
        doubts = list(current.doubts)
        have_doubts = {doubt.doubt_id for doubt in doubts}
        for doubt in step.doubts:
            if doubt.doubt_id not in have_doubts:
                have_doubts.add(doubt.doubt_id)
                doubts.append(doubt)
        drafts.extend(step.events)
        ops = visit.ops
        if op_id is not None:
            ops = (*ops, op_id)[-MAX_OPS:]
        moved = step.to is not current.state
        if moved:
            drafts.append(
                EventDraft(
                    kind=TimelineKind.STATE_CHANGED,
                    details={"from": current.state.value, "to": step.to.value, "mode": mode_of(current)},
                )
            )
        waiting: WaitingFor | None = _REST.get(step.to)
        if step.to is VisitState.INTAKE and not current.files and not step.updates.get("files"):
            waiting = "files"
        new_visit = visit.model_copy(
            update={
                "offers": offers,
                "ops": ops,
                "waiting_for": waiting,
                "transitions": visit.transitions + (1 if moved else 0),
                "stopped": None,
                **step.visit,
            }
        )
        counters = current.counters.model_copy(
            update={
                "tool_calls": current.counters.tool_calls + run.tool_calls,
                "model_calls": current.counters.model_calls + step.model_calls,
                "cost_usd": current.counters.cost_usd + step.cost_usd,
                "questions_per_file": asked_per_file,
            }
        )
        run.tool_calls = 0
        changed = current.model_copy(
            update={
                **step.updates,
                "state": step.to,
                "evidence": tuple(refs),
                "items": tuple(step.updates.get("items", items)),
                "questions": tuple(step.updates.get("questions", questions)),
                "doubts": tuple(doubts),
                "visit": new_visit,
                "counters": counters,
            }
        )
        return changed, drafts

    return run.store.update(
        case.client_id, case.case_id, expected_version=case.version, actor=run.actor, change=change
    )


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    import json

    return (json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode(
        "utf-8"
    )


def _stop(run: _Run, case: Case, state: VisitState, exc: AgentToolError | ConfigError) -> Case:
    """Record why `state` could not complete; the state is kept, and the next resume tries it again."""
    code = getattr(exc, "code", "VISIT_STOPPED")
    message = str(getattr(exc, "message", "")) or str(exc)

    def change(current: Case) -> tuple[Case, list[EventDraft]]:
        visit = (current.visit or CaseVisit()).model_copy(
            update={"stopped": VisitStop(code=code, message=message[:400], state=state.value)}
        )
        return current.model_copy(update={"visit": visit}), []

    _LOG.warning("visit.stopped case=%s state=%s code=%s", case.case_id, state.value, code)
    return run.store.update(
        case.client_id, case.case_id, expected_version=case.version, actor=run.actor, change=change
    )


def _digest(*parts: Any) -> str:
    return payload_digest({"parts": list(parts)})


def _pending_files(case: Case) -> bool:
    taken = set((case.visit or CaseVisit()).taken_in)
    return any(upload_id not in taken for upload_id in case.upload_ids())


def _advance(run: _Run, case: Case) -> Case:
    """Run automatic states until the visit rests (an INTERRUPT, or no files yet)."""
    for _ in range(MAX_STEPS):
        handler = _HANDLERS.get(case.state)
        if handler is None:
            return case
        try:
            step = handler(run, case)
        except (AgentToolError, ConfigError) as exc:
            return _stop(run, case, case.state, exc)
        if step is None:
            return case
        case = _write(run, case, step)
    return case


# ---------------------------------------------------------------------------
# The states
# ---------------------------------------------------------------------------
def _intake(run: _Run, case: Case) -> _Step | None:
    """INTAKE: each new file is read as intake read it (rows, columns, intake notes); no AI."""
    if not case.files:
        visit = case.visit
        if visit is not None and visit.waiting_for == "files":
            return None
        return _Step(to=VisitState.INTAKE)
    files = {loaded.upload_id: loaded for loaded in run.files(case)}
    visit = case.visit or CaseVisit()
    new = [upload_id for upload_id in case.upload_ids() if upload_id not in visit.taken_in]
    events = [
        EventDraft(
            kind=TimelineKind.FILE_ADDED,
            upload_id=upload_id,
            details={
                "taken_in": True,
                "rows": files[upload_id].profile.row_count if upload_id in files else 0,
                "columns": files[upload_id].profile.column_count if upload_id in files else 0,
                "notes": len(files[upload_id].notes) if upload_id in files else 0,
            },
        )
        for upload_id in new
    ]
    return _Step(
        to=VisitState.UNDERSTAND,
        visit={"taken_in": tuple(dict.fromkeys((*visit.taken_in, *new)))},
        events=events,
    )


def _understand(run: _Run, case: Case) -> _Step:
    """UNDERSTAND: what each file is, its ID, its dates, its health; how the files fit (`diagnose_files`,
    `how_files_fit`). Rules only."""
    from engine.doctor.contracts import Diagnosis, FileDiagnosis

    ctx = run.context(case)
    evidence: list[DataToolResult] = []
    diagnosed: list[FileDiagnosis] = []
    for upload_id in case.upload_ids():
        result = run.call("diagnose_files", {"upload_ids": [upload_id]}, ctx)
        evidence.append(result)
        diagnosed.extend(FileDiagnosis.model_validate(f) for f in result.output["files"])
    links: tuple[Any, ...] = ()
    stacks: tuple[Any, ...] = ()
    if len(case.files) > 1:
        fit = run.call("how_files_fit", {}, ctx).output
        links, stacks = tuple(fit["links"]), tuple(fit["stacks"])
    earlier = (case.diagnosis or {}).get("use_cases") or []
    diagnosis = Diagnosis.model_validate(
        {
            "files": [d.model_dump(mode="json") for d in diagnosed],
            "use_cases": earlier,
            "links": list(links),
            "stacks": list(stacks),
        }
    )
    by_id = {d.upload_id: d for d in diagnosed}
    files = tuple(
        file.model_copy(
            update={
                "role": by_id[file.upload_id].kind if file.upload_id in by_id else file.role,
                "key_columns": (
                    (by_id[file.upload_id].id_column,)
                    if file.upload_id in by_id and by_id[file.upload_id].id_column
                    else file.key_columns
                ),
            }
        )
        for file in case.files
    )
    return _Step(
        to=VisitState.RECALL,
        updates={"diagnosis": diagnosis.model_dump(mode="json"), "files": files},
        evidence=evidence,
    )


def _recall(run: _Run, case: Case) -> _Step:
    """RECALL: a treatment approved before for a file of the same shape, for the goal or the offered goals."""
    from engine.agent.tools import AgentContext
    from engine.config import RunMode, load_use_case

    goal = active_goal(case)
    configs = (
        [load_use_case(goal.goal_id, run.deps.config_root)]
        if goal is not None
        else [offer.config for offer in run.deps.offers]
    )
    evidence: list[DataToolResult] = []
    items: list[CaseItem] = []
    now = run.deps.clock()
    for loaded in run.files(case):
        for config in configs:
            agent = AgentContext(
                use_case_id=config.id,
                config=config,
                config_root=run.deps.config_root,
                upload_id=loaded.upload_id,
                mode=RunMode.TRAIN,
                profile=loaded.profile,
                frame=loaded.frame,
            )
            result = run.call("recall_treatment", {}, run.context(case, agent=agent, files=(loaded,)))
            offer = result.output.get("offer")
            if not isinstance(offer, dict) or offer.get("kind") != "same_shape":
                continue
            evidence.append(result)
            payload: dict[str, JsonValue] = {
                "use_case_id": offer.get("use_case_id"),
                "recipe_id": offer.get("recipe_id"),
                "shape_hash": offer.get("shape_hash"),
                "steps": offer.get("steps"),
                "upload_id": loaded.upload_id,
            }
            items.append(
                _item(
                    "recalled_treatment",
                    payload,
                    title=str(
                        offer.get("sentence") or "Use the treatment approved before for this kind of file."
                    ),
                    upload_id=loaded.upload_id,
                    confidence=AgentConfidence.CHECK,
                    evidence=(result.evidence_id,),
                    now=now,
                )
            )
    to = VisitState.GOAL_BRIEF if goal is not None else VisitState.TREAT
    return _Step(to=to, evidence=evidence, items=items)


def _goal_brief(run: _Run, case: Case) -> _Step:
    """GOAL_BRIEF: `load_goal_brief` for the active goal, frozen into the case by its hash."""
    goal = active_goal(case)
    if goal is None:
        return _Step(to=VisitState.TREAT)
    result = run.call(GOAL_BRIEF_TOOL, {"goal_id": goal.goal_id}, run.context(case, files=()))
    brief = GoalBrief.model_validate(result.output)
    run.deps.storage.write_model(
        brief_key(case.client_id, case.case_id, goal.goal_id, brief.brief_hash), brief
    )
    goals = tuple(
        g.model_copy(update={"brief_hash": brief.brief_hash}) if g.goal_id == goal.goal_id else g
        for g in case.goals
    )
    changed = goal.brief_hash is not None and goal.brief_hash != brief.brief_hash
    return _Step(
        to=VisitState.TREAT,
        updates={"goals": goals},
        evidence=[result],
        events=[
            EventDraft(
                kind=TimelineKind.GOAL_SET,
                item_id=goal.goal_id,
                details={"brief_loaded": True, "brief_changed": changed},
            )
        ],
    )


def _treat(run: _Run, case: Case) -> _Step:
    """TREAT: the rules' proposals, questions and doubts for the base and, with a goal, the goal's section."""
    if _pending_files(case):
        return _Step(to=VisitState.INTAKE)
    goal = active_goal(case)
    brief = stored_brief(run.deps.storage, case) if goal is not None else None
    if goal is not None and brief is None:
        return _Step(to=VisitState.GOAL_BRIEF)
    answers = sorted((q.question_id, q.answer or "") for q in case.questions)
    inputs = _digest(
        [(f.upload_id, f.fingerprint) for f in case.files],
        goal.goal_id if goal is not None else None,
        brief.brief_hash if brief is not None else None,
        answers,
    )
    visit = case.visit or CaseVisit()
    step = _Step(to=VisitState.PREVIEW)
    if visit.done.get("treat") != inputs:
        _base_rules(run, case, step)
        if brief is None:
            _generic_rules(run, case, step)
        else:
            _goal_rules(run, case, brief, step)
        step.visit["done"] = {**visit.done, "treat": inputs}
    asked = {question.question_id for question in case.questions}
    blocking = [
        q
        for q, _effects in step.questions
        if q.blocking and q.question_id not in asked and _in_section(q.goal_id, case)
    ]
    if open_blocking(case) or blocking:
        step.to = VisitState.ASK
    return step


def _ask(_run: _Run, case: Case) -> _Step | None:
    """ASK: waits while a blocking question of the active section is open; then back to TREAT."""
    if open_blocking(case):
        return None
    return _Step(to=VisitState.TREAT)


def _converse(run: _Run, case: Case) -> _Step:
    """CONVERSE: Plan G's chat turn on the person's last message; proposals land pending. No AI: a fixed reply."""
    message = case.chat[-1] if case.chat and case.chat[-1].role == "user" else None
    if message is None:
        return _Step(to=VisitState.TREAT)
    now = run.deps.clock()
    port = run.deps.converse
    result = (
        ConverseResult(reply=AI_NEEDED_REPLY) if port is None else port(case, message.text, run.files(case))
    )
    reply = CaseChatEntry(role="agent", text=result.reply, at=now, sent=result.sent)
    items = [
        item.model_copy(
            update={
                "source": ProposalSource.AI,
                "state": ProposalState.PENDING,
                "confidence": AgentConfidence.CHECK,
            }
        )
        for item in result.items  # the AI never ticks: whatever a port returns lands pending, `check`
    ]
    return _Step(
        to=VisitState.TREAT,
        updates={"chat": (*case.chat, reply)},
        items=items,
        evidence=list(result.evidence),
        events=[
            EventDraft(
                kind=TimelineKind.CHAT,
                details={"role": "agent", "ai": port is not None, "proposals": len(items)},
            )
        ],
        model_calls=result.model_calls,
        cost_usd=result.cost_usd,
    )


def _preview(run: _Run, case: Case) -> _Step:
    """PREVIEW: what would change, tried on at most 1,000 rows per file. No AI."""
    files = run.files(case)
    summary = run.deps.preview(case, files) if run.deps.preview is not None else _count_preview(case, files)
    return _Step(
        to=VisitState.APPROVE,
        events=[
            EventDraft(
                kind=TimelineKind.PREVIEW,
                details={
                    "files": summary.files,
                    "rows_tried": summary.rows_tried,
                    "ticked": summary.items_ticked,
                    "pending": summary.items_pending,
                    **dict(summary.details),
                },
            )
        ],
    )


def _count_preview(case: Case, files: tuple[LoadedFile, ...]) -> PreviewSummary:
    items, _ = active_section(case)
    return PreviewSummary(
        files=len(files),
        rows_tried=sum(min(PREVIEW_ROWS, loaded.profile.row_count) for loaded in files),
        items_ticked=sum(1 for item in items if item.state is ProposalState.ACCEPTED),
        items_pending=sum(1 for item in items if item.state is ProposalState.PENDING),
    )


def _approve_wait(_run: _Run, _case: Case) -> None:
    """APPROVE: waits for the person (:func:`approve`)."""
    return


def _prescribe(run: _Run, case: Case) -> _Step:
    """PRESCRIBE: the approved items, the answers and the brief, hashed (M243 writes the prescription)."""
    brief = stored_brief(run.deps.storage, case)
    hash_ = (
        run.deps.prescribe(case, brief)
        if run.deps.prescribe is not None
        else prescription_digest(case, brief)
    )
    latest = case.prescriptions[-1] if case.prescriptions else None
    updates: dict[str, Any] = {}
    if latest is None or latest.prescription_hash != hash_:
        approved_by = case.approvals[-1].approved_by if case.approvals else None
        ref = CasePrescriptionRef(
            prescription_hash=hash_,
            created_at=run.deps.clock(),
            approved_by=approved_by,
            upload_ids=case.upload_ids(),
        )
        updates["prescriptions"] = (*case.prescriptions, ref)
    return _Step(
        to=VisitState.VERIFY,
        updates=updates,
        events=[
            EventDraft(kind=TimelineKind.PRESCRIPTION, item_id=hash_[:16], details={"new": bool(updates)})
        ],
    )


def prescription_digest(case: Case, brief: GoalBrief | None) -> str:
    """The default PRESCRIBE: sha256 over the ticked items of the base and the active goal, the answers and the
    brief hash. Wording and who decided are left out, as `recipe_hash` leaves out reasons."""
    items, questions = active_section(case)
    return payload_digest(
        {
            "base": sorted(
                i.payload_digest for i in items if i.goal_id is None and i.state is ProposalState.ACCEPTED
            ),
            "goal": (
                None
                if brief is None
                else {
                    "goal_id": brief.goal_id,
                    "brief_hash": brief.brief_hash,
                    "items": sorted(
                        i.payload_digest
                        for i in items
                        if i.goal_id is not None and i.state is ProposalState.ACCEPTED
                    ),
                }
            ),
            "answers": sorted((q.question_id, q.answer or "") for q in questions if q.answer is not None),
            "files": list(case.upload_ids()),
        }
    )


def _verify(run: _Run, case: Case) -> _Step:
    """VERIFY: the readiness verdict for the active goal (M234), or straight on with no goal."""
    brief = stored_brief(run.deps.storage, case)
    port = run.deps.verify or readiness_verify(run.deps.storage, run.deps.config_root)
    if brief is None:
        result = VerifyResult(verdict="no_goal", reason="No goal is chosen: pick one to check it will train.")
    else:
        result = port(case, brief, run.files(case))
    updates: dict[str, Any] = {}
    if result.ref is not None:
        updates["readiness"] = (*(r for r in case.readiness if r.goal_id != result.ref.goal_id), result.ref)
    ready = result.verdict in {"ready", "ready_with_warnings", "no_goal"}
    return _Step(
        to=VisitState.HAND_OFF if ready else VisitState.TREAT,
        updates=updates,
        events=[
            EventDraft(
                kind=TimelineKind.READINESS,
                item_id=brief.goal_id if brief is not None else None,
                details={"verdict": result.verdict, "reason": result.reason[:200]},
            )
        ],
    )


def _hand_off(run: _Run, case: Case) -> _Step:
    """HAND_OFF: where the prepared data goes. The default records it and starts nothing."""
    brief = stored_brief(run.deps.storage, case)
    port = run.deps.hand_off
    record = port(case, brief) if port is not None else _default_hand_off(case, brief)
    return _Step(
        to=VisitState.FOLLOW_UP,
        visit={"hand_off": record},
        events=[
            EventDraft(
                kind=TimelineKind.HAND_OFF,
                item_id=record.goal_id,
                details={"target": record.target, "run_id": record.run_id, "files": len(record.upload_ids)},
            )
        ],
    )


def _default_hand_off(case: Case, brief: GoalBrief | None) -> HandOffRecord:
    latest = case.prescriptions[-1].prescription_hash if case.prescriptions else None
    if brief is None:
        return HandOffRecord(target="choose_goal", upload_ids=case.upload_ids(), prescription_hash=latest)
    target: Literal["runs", "uplift_runs"] = "uplift_runs" if brief.problem_type == "uplift" else "runs"
    return HandOffRecord(
        target=target, goal_id=brief.goal_id, upload_ids=case.upload_ids(), prescription_hash=latest
    )


def _follow_up(_run: _Run, case: Case) -> _Step | None:
    """FOLLOW_UP: rests until next month's file arrives; then the visit reads it from INTAKE."""
    if _pending_files(case):
        return _Step(to=VisitState.INTAKE)
    return None


_HANDLERS: Final[dict[VisitState, Callable[[_Run, Case], _Step | None]]] = {
    VisitState.INTAKE: _intake,
    VisitState.UNDERSTAND: _understand,
    VisitState.RECALL: _recall,
    VisitState.GOAL_BRIEF: _goal_brief,
    VisitState.TREAT: _treat,
    VisitState.ASK: _ask,
    VisitState.CONVERSE: _converse,
    VisitState.PREVIEW: _preview,
    VisitState.APPROVE: _approve_wait,
    VisitState.PRESCRIBE: _prescribe,
    VisitState.VERIFY: _verify,
    VisitState.HAND_OFF: _hand_off,
    VisitState.FOLLOW_UP: _follow_up,
}


# ---------------------------------------------------------------------------
# The rules of TREAT
# ---------------------------------------------------------------------------
def _short(value: Any) -> str:
    return payload_digest({"v": value})[:12]


def _item(
    kind: str,
    payload: dict[str, JsonValue],
    *,
    title: str,
    upload_id: str | None,
    confidence: AgentConfidence,
    evidence: tuple[str, ...],
    now: datetime,
    goal_id: str | None = None,
    ticked: bool = False,
    source: ProposalSource = ProposalSource.RULES,
) -> CaseItem:
    """A rules' proposal; its id is its content, so proposing it again finds it."""
    item_id = f"{kind}.{goal_id or 'base'}.{_short(payload)}"
    return CaseItem(
        item_id=item_id[:128],
        kind=kind,
        title=title,
        source=source,
        confidence=confidence,
        state=ProposalState.ACCEPTED if ticked else ProposalState.PENDING,
        upload_id=upload_id,
        evidence_ids=evidence,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=now,
        goal_id=goal_id,
    )


def _question(
    code: str,
    subject: Mapping[str, JsonValue],
    *,
    text: str,
    choices: Sequence[tuple[str, OptionEffect]],
    upload_id: str | None,
    column: str | None = None,
    goal_id: str | None = None,
    blocking: bool = True,
) -> tuple[CaseQuestion, tuple[OptionEffect, ...]]:
    """A question whose id is its code, its goal (none for the base) and its subject - never the mode it is asked
    in - so a question answered once is found, not asked again, whenever the rules need the same answer."""
    question_id = f"q.{code.lower()}.{goal_id or 'base'}.{_short(dict(subject))}"[:128]
    options = tuple(
        CaseOption(
            option_id=effect.option_id,
            label=label,
            effect_digest=payload_digest(effect.payload) if effect.kind is not None else None,
        )
        for label, effect in choices
    )
    question = CaseQuestion(
        question_id=question_id,
        code=code,
        upload_id=upload_id,
        column=column,
        text=text,
        options=options,
        blocking=blocking,
        recommended=None,  # meaning-bearing: the rules never recommend which column is which
        goal_id=goal_id,
    )
    return question, tuple(effect for _label, effect in choices)


_DONT_KNOW: Final[str] = "I don't know"


def _key_of(case: Case, upload_id: str) -> str | None:
    """The file's ID column as decided: a ticked `key` item of the base, else none."""
    for item in case.items:
        if item.kind == "key" and item.upload_id == upload_id and item.state is ProposalState.ACCEPTED:
            column = item.payload.get("column")
            return column if isinstance(column, str) else None
    return None


def _base_rules(run: _Run, case: Case, step: _Step) -> None:
    """What does not depend on a goal: which column names each row of each file (the key)."""
    now = run.deps.clock()
    ctx = run.context(case)
    for loaded in run.files(case):
        upload_id = loaded.upload_id
        if _key_of(case, upload_id) is not None:
            continue
        result = run.call("find_file_role", {"upload_id": upload_id}, ctx)
        step.evidence.append(result)
        unique = [
            key
            for key in result.output.get("keys", [])
            if isinstance(key, dict)
            and float(key.get("rows_per_key") or 99.0) <= _UNIQUE
            and float(key.get("null_rate") or 0.0) < 0.05
        ]
        name = loaded.file_name or "this file"
        if len(unique) == 1:
            column = str(unique[0]["column"])
            step.items.append(
                _item(
                    "key",
                    {"upload_id": upload_id, "column": column},
                    title=f"“{column}” names each row of {name}.",
                    upload_id=upload_id,
                    confidence=AgentConfidence.SURE,
                    evidence=(result.evidence_id,),
                    now=now,
                    ticked=True,  # AUTO: the values prove it; shown, and the person may untick it
                )
            )
        elif len(unique) > 1:
            choices = [
                (
                    str(key["column"]),
                    OptionEffect(
                        option_id=f"c{index}",
                        kind="key",
                        title=f"“{key['column']}” names each row of {name}.",
                        payload={"upload_id": upload_id, "column": str(key["column"])},
                    ),
                )
                for index, key in enumerate(unique[:5])
            ]
            choices.append((_DONT_KNOW, OptionEffect(option_id="none")))
            step.questions.append(
                _question(
                    "KEY_AMBIGUOUS",
                    {"upload_id": upload_id},
                    text=f"Which column names each row of {name}? Several hold a different value on every row.",
                    choices=choices,
                    upload_id=upload_id,
                )
            )


def _generic_rules(run: _Run, case: Case, step: _Step) -> None:
    """No goal: what the data can power, and what is missing for the other goals (the design §3.1)."""
    if not run.deps.offers:
        return
    ctx = run.context(case)
    power = run.call("what_can_this_power", {}, ctx)
    verdicts = list(power.output["use_cases"])
    missing = tuple(
        str(v["use_case_id"])
        for v in verdicts
        if isinstance(v, dict) and v.get("status") in {"missing", "too_small"}
    )
    step.updates["diagnosis"] = {
        **(case.diagnosis or {"files": [], "links": [], "stacks": []}),
        "use_cases": verdicts,
    }
    step.visit["missing_for"] = missing
    if missing:
        request = run.call("data_request", {"use_case_ids": list(missing[:4])}, ctx)
        step.evidence.append(request)
        step.visit["data_request"] = request.evidence_id


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _named(columns: Sequence[str], names: Sequence[str], *, parts: bool) -> list[str]:
    """Columns whose name is one of `names` (case and punctuation aside); with `parts`, also a `_`-part of it."""
    wanted = {_norm(name) for name in names if name}
    found: list[str] = []
    for column in columns:
        norm = _norm(column)
        if norm in wanted or (parts and wanted & set(norm.split("_"))):
            found.append(column)
    return found


def _goal_rules(run: _Run, case: Case, brief: GoalBrief, step: _Step) -> None:
    """The goal's own checks and questions: the outcome column, the date each row describes, consent."""
    now = run.deps.clock()
    goal = brief.goal_id
    files = run.files(case)
    # The outcome: the configured column where it is; otherwise one question over the files' likely columns.
    outcome = _decided(case, "outcome_column", goal)
    if outcome is None:
        exact = [
            (loaded, column)
            for loaded in files
            for column in _named(
                [str(c) for c in loaded.frame.columns], [brief.outcome.column or ""], parts=False
            )
        ]
        if len(exact) == 1:
            loaded, column = exact[0]
            step.items.append(
                _item(
                    "outcome_column",
                    {"upload_id": loaded.upload_id, "column": column},
                    title=f"“{column}” in {loaded.file_name or 'this file'} is the outcome: {brief.outcome.definition}",
                    upload_id=loaded.upload_id,
                    confidence=AgentConfidence.SURE,
                    evidence=(),
                    now=now,
                    goal_id=goal,
                    ticked=True,
                )
            )
            outcome = (loaded.upload_id, column)
        else:
            likely = exact or [
                (loaded, column)
                for loaded in files
                for column in _named(
                    [str(c) for c in loaded.frame.columns], brief.outcome.synonyms, parts=True
                )
            ]
            if likely:
                choices = [
                    (
                        f"{column} ({loaded.file_name})" if len(files) > 1 else column,
                        OptionEffect(
                            option_id=f"c{index}",
                            kind="outcome_column",
                            title=f"“{column}” is the outcome: {brief.outcome.definition}",
                            payload={"upload_id": loaded.upload_id, "column": column},
                        ),
                    )
                    for index, (loaded, column) in enumerate(likely[:5])
                ]
                choices.append(("None of these", OptionEffect(option_id="none")))
                what = brief.outcome.definition.rstrip(".") or f"the outcome of {brief.goal_name}"
                step.questions.append(
                    _question(
                        "OUTCOME_COLUMN",
                        {"files": [f.upload_id for f in files]},
                        text=f"Which column says whether: {what}?",
                        choices=choices,
                        upload_id=(
                            likely[0][0].upload_id if len({lf.upload_id for lf, _c in likely}) == 1 else None
                        ),
                        goal_id=goal,
                    )
                )
            else:
                step.doubts.append(
                    CaseDoubt(
                        doubt_id=f"d.outcome_missing.{goal}"[:128],
                        because=f"none of the files has a column saying whether: {brief.outcome.definition}",
                        would_do="ask the team that has it for that column, one row per customer (the data request)",
                        if_not=f"{brief.goal_name} cannot be trained on these files",
                    )
                )
    if outcome is not None:
        _time_rule(run, case, brief, outcome[0], step, now)
        _consent_rule(case, brief, outcome[0], step, files, now)


def _decided(case: Case, kind: str, goal_id: str) -> tuple[str, str] | None:
    for item in case.items:
        if item.kind == kind and item.goal_id == goal_id and item.state is ProposalState.ACCEPTED:
            upload_id, column = item.payload.get("upload_id"), item.payload.get("column")
            if isinstance(upload_id, str) and isinstance(column, str):
                return upload_id, column
    return None


def _time_rule(run: _Run, case: Case, brief: GoalBrief, upload_id: str, step: _Step, now: datetime) -> None:
    """The date each row describes, for a goal whose split or outcome window needs one."""
    need = brief.column("time")
    if need is None or _decided(case, "as_of_column", brief.goal_id) is not None:
        return
    loaded = next((lf for lf in run.files(case) if lf.upload_id == upload_id), None)
    if loaded is None:
        return
    named = _named([str(c) for c in loaded.frame.columns], need.names, parts=False)
    if named:
        step.items.append(
            _item(
                "as_of_column",
                {"upload_id": upload_id, "column": named[0]},
                title=f"“{named[0]}” is the date each row describes.",
                upload_id=upload_id,
                confidence=AgentConfidence.SURE,
                evidence=(),
                now=now,
                goal_id=brief.goal_id,
                ticked=True,
            )
        )
        return
    if not need.required:
        return
    result = run.call("find_time_columns", {"upload_id": upload_id}, run.context(case))
    step.evidence.append(result)
    candidates = [str(c["column"]) for c in result.output.get("columns", []) if isinstance(c, dict)]
    if not candidates:
        step.doubts.append(
            CaseDoubt(
                doubt_id=f"d.time_missing.{brief.goal_id}"[:128],
                because="no column of the file reads as a date",
                would_do="ask for the date each row describes",
                if_not=f"{brief.goal_name} splits by date and cannot be tested without one",
            )
        )
        return
    choices = [
        (
            column,
            OptionEffect(
                option_id=f"c{index}",
                kind="as_of_column",
                title=f"“{column}” is the date each row describes.",
                payload={"upload_id": upload_id, "column": column},
            ),
        )
        for index, column in enumerate(candidates[:5])
    ]
    choices.append((_DONT_KNOW, OptionEffect(option_id="none")))
    step.questions.append(
        _question(
            "AS_OF_COLUMN",
            {"upload_id": upload_id},
            text="Which date does each row describe? Testing uses the newest dates.",
            choices=choices,
            upload_id=upload_id,
            goal_id=brief.goal_id,
        )
    )


def _consent_rule(
    case: Case, brief: GoalBrief, upload_id: str, step: _Step, files: tuple[LoadedFile, ...], now: datetime
) -> None:
    """A contacting goal's consent column: proposed, never ticked (it decides who may be contacted)."""
    need = brief.column("consent")
    if need is None or _decided(case, "consent_column", brief.goal_id) is not None:
        return
    loaded = next((lf for lf in files if lf.upload_id == upload_id), None)
    if loaded is None:
        return
    named = _named([str(c) for c in loaded.frame.columns], need.names, parts=False)
    if named:
        step.items.append(
            _item(
                "consent_column",
                {"upload_id": upload_id, "column": named[0]},
                title=f"“{named[0]}” says who agreed to be contacted.",
                upload_id=upload_id,
                confidence=AgentConfidence.CHECK,
                evidence=(),
                now=now,
                goal_id=brief.goal_id,
            )
        )
    elif need.required:
        step.doubts.append(
            CaseDoubt(
                doubt_id=f"d.consent_missing.{brief.goal_id}"[:128],
                because="no column says who agreed to be contacted",
                would_do="leave everyone without a recorded agreement off the list",
                if_not="people who did not agree could be contacted",
            )
        )


# ---------------------------------------------------------------------------
# The default VERIFY: M234's readiness report on the file holding the outcome
# ---------------------------------------------------------------------------
def readiness_verify(storage: Storage, config_root: Path | None) -> VerifyPort:
    """VERIFY with M234's `check_readiness` on the prepared copy of the file holding the goal's outcome."""

    def verify(case: Case, brief: GoalBrief | None, files: tuple[LoadedFile, ...]) -> VerifyResult:
        from engine.config import RunMode, load_use_case
        from engine.doctor.readiness import check_readiness, inputs_for_upload, store_readiness

        del files  # the stored upload is read the way POST /runs reads it

        if brief is None:
            return VerifyResult(verdict="no_goal", reason="No goal is chosen.")
        outcome = _decided(case, "outcome_column", brief.goal_id)
        if outcome is None:
            return VerifyResult(
                verdict="not_ready", reason="VISIT_NO_OUTCOME: no file has the goal's outcome yet."
            )
        upload_id, column = outcome
        key = _key_of(case, upload_id)
        if key is None:
            diagnosed = next((f for f in case.files if f.upload_id == upload_id), None)
            key = diagnosed.key_columns[0] if diagnosed is not None and diagnosed.key_columns else None
        config = load_use_case(brief.goal_id, config_root)
        inputs = inputs_for_upload(
            storage,
            upload_id,
            config=config,
            mode=RunMode.TRAIN,
            primary_key=key,
            target=column,
            goal=brief.goal_id,
        )
        report = check_readiness(inputs)
        store_readiness(storage, report, client_id=case.client_id, case_id=case.case_id)
        ref = CaseReadinessRef(
            goal_id=brief.goal_id,
            report_hash=report.report_hash,
            verdict=report.verdict,
            created_at=utc_now(),
        )
        return VerifyResult(verdict=report.verdict, reason=report.verdict_reason, ref=ref)

    return verify


# ---------------------------------------------------------------------------
# CONVERSE: Plan G's chat turn
# ---------------------------------------------------------------------------
def plan_g_converse(
    context_for: Callable[[Case, tuple[LoadedFile, ...]], AgentContext],
    *,
    meter: Any,
    guardrails: Guardrails,
    principal: Principal | None = None,
    config_root: Path | None = None,
) -> ConversePort:
    """CONVERSE as Plan G's `chat_turn` (the design §3.4): the same tools, egress gate, guardrails and budgets.

    `context_for` gives the file the chat is about (Plan G's chat reads one file). The turn runs on a session
    opened for it, holding the case's chat so far; its pending proposals become the case's items (`ai`, `check`,
    never ticked): a setting belongs to the active goal's section, a step to the base.
    """

    def converse(case: Case, message: str, files: tuple[LoadedFile, ...]) -> ConverseResult:
        from engine.agent.contracts import ChatMessage, ChatRole
        from engine.agent.loop import chat_turn
        from engine.agent.session import start_session
        from engine.doctor.registry import evidence_id_of

        ctx = context_for(case, files)
        session = start_session(ctx, session_id=f"dd-{case.case_id}"[:64])
        history = tuple(
            ChatMessage(
                role=ChatRole.USER if entry.role == "user" else ChatRole.AGENT,
                text=entry.text,
                created_at=entry.at,
            )
            for entry in case.chat[:-1]
        )
        session = session.model_copy(update={"transcript": history})
        turn = chat_turn(
            ctx,
            session,
            message,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
            principal=principal,
        )
        evidence = {result.evidence_id: result for result in turn.tool_results}
        hashed = {key: evidence_id_of(result) for key, result in evidence.items()}
        goal = active_goal(case)
        now = ctx.clock()
        items = tuple(
            _item(
                proposal.kind.value,
                {
                    "upload_id": ctx.upload_id,
                    "path": proposal.path,
                    "value": proposal.value,
                    "step": None if proposal.step is None else proposal.step.model_dump(mode="json"),
                },
                title=proposal.title,
                upload_id=ctx.upload_id,
                confidence=AgentConfidence.CHECK,
                evidence=tuple(hashed.get(e, e) for e in proposal.evidence_ids),
                now=now,
                goal_id=goal.goal_id if goal is not None and proposal.kind.value == "setting" else None,
                source=ProposalSource.AI,
            )
            for proposal in turn.proposals
        )
        results = tuple(
            DataToolResult(
                evidence_id=hashed[result.evidence_id],
                tool=result.tool,
                kind=DataToolKind.READ if result.tool in TOOLS else DataToolKind.PROPOSE,
                exposure=Exposure.SESSION_ONLY,
                input={**result.args, "upload_id": ctx.upload_id},
                output=dict(result.result),
                computed_at=now,
            )
            for result in turn.tool_results
        )
        return ConverseResult(
            reply=turn.reply.text,
            items=items,
            evidence=results,
            sent=tuple(item.model_dump(mode="json") for item in turn.reply.sent),
            model_calls=turn.llm_calls,
        )

    return converse


# ---------------------------------------------------------------------------
# What the person does
# ---------------------------------------------------------------------------
def _seen(case: Case, op_id: str | None) -> bool:
    return op_id is not None and case.visit is not None and op_id in case.visit.ops


def _person_step(
    run: _Run,
    client_id: str,
    case_id: str,
    expected_version: int,
    op_id: str | None,
    build: Callable[[Case], _Step],
) -> Case:
    """Apply one action of the person as one transition, then run the machine to its next rest."""
    case = run.store.get(client_id, case_id)
    if _seen(case, op_id):
        return case  # its first attempt was written: nothing is applied twice
    if case.version != expected_version:
        raise CaseError(
            "CASE_VERSION_CONFLICT",
            f"The case changed since you loaded it (version {case.version}, you sent {expected_version}). "
            "Reload it and try again.",
        )
    if case.read_only:
        raise CaseError("CASE_READ_ONLY", "This case cannot be changed here.")
    step = build(case)
    case = _write(run, case, step, op_id=op_id)
    return _advance(run, case)


def resume(client_id: str, case_id: str, deps: VisitDeps, *, actor: str = AGENT) -> Case:
    """Continue the visit from the last state written: after a crash, a restart, or a new file."""
    run = _Run(deps, actor)
    case = run.store.get(client_id, case_id)
    if case.read_only:
        return case
    for _ in range(3):  # another writer moved the case on: read it again and continue from there
        try:
            return _advance(run, case)
        except CaseError as exc:
            if exc.code != "CASE_VERSION_CONFLICT":
                raise
            case = run.store.get(client_id, case_id)
    return _advance(run, case)


def add_files(
    client_id: str,
    case_id: str,
    files: Sequence[CaseFile],
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    op_id: str | None = None,
) -> Case:
    """Add files to the case; the visit reads them from INTAKE (next month's file: FOLLOW_UP -> INTAKE)."""
    run = _Run(deps, actor)

    def build(case: Case) -> _Step:
        have = set(case.upload_ids())
        new = tuple(file for file in files if file.upload_id not in have)
        events = [EventDraft(kind=TimelineKind.FILE_ADDED, upload_id=file.upload_id) for file in new]
        to = VisitState.INTAKE if new and case.state not in {VisitState.INTAKE} else case.state
        return _Step(to=to, updates={"files": (*case.files, *new)} if new else {}, events=events)

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def set_goal(
    client_id: str,
    case_id: str,
    goal_id: str | None,
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    op_id: str | None = None,
) -> Case:
    """Choose, switch or clear the goal on a live case (the design §3.3).

    Choosing a goal adds its brief to the same case (GOAL_BRIEF), keeping every decided item and answered
    question. Switching sets the previous goal's section aside; switching back makes it active as it was.
    `None` goes back to the generic mode. A goal that is not a use case is refused (`USE_CASE_NOT_FOUND`)."""
    if goal_id is not None:
        load_goal_brief(goal_id, root=deps.config_root)  # refuses an unknown goal before anything is written
    run = _Run(deps, actor)
    now = deps.clock()

    def build(case: Case) -> _Step:
        previous = active_goal(case)
        if goal_id is None:
            goals = tuple(g.model_copy(update={"active": False}) for g in case.goals)
        elif goal_id in {g.goal_id for g in case.goals}:
            goals = tuple(g.model_copy(update={"active": g.goal_id == goal_id}) for g in case.goals)
        else:
            goals = (
                *(g.model_copy(update={"active": False}) for g in case.goals),
                CaseGoal(goal_id=goal_id, active=True, set_at=now, set_by=actor),
            )
        started = case.state in _AFTER_RECALL
        if goal_id is None:
            to = VisitState.TREAT if started else case.state
        else:
            to = VisitState.GOAL_BRIEF if started else case.state
        return _Step(
            to=to,
            updates={"goals": goals},
            events=[
                EventDraft(
                    kind=TimelineKind.GOAL_SET,
                    item_id=goal_id,
                    details={
                        "previous": previous.goal_id if previous is not None else None,
                        "set_aside": previous is not None and previous.goal_id != goal_id,
                    },
                )
            ],
        )

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def answer(
    client_id: str,
    case_id: str,
    question_id: str,
    option_id: str,
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    op_id: str | None = None,
) -> Case:
    """Answer a question with one of its options; the option's proposal is added, ticked by the person."""
    run = _Run(deps, actor)
    now = deps.clock()

    def build(case: Case) -> _Step:
        question = next((q for q in case.questions if q.question_id == question_id), None)
        if question is None or not _in_section(question.goal_id, case):
            raise _unknown("There is no such question open in this case.")
        if option_id not in {option.option_id for option in question.options}:
            raise _unknown("That is not one of the question's answers.")
        effects = {e.option_id: e for e in (case.visit or CaseVisit()).offers.get(question_id, ())}
        effect = effects.get(option_id)
        replaced = f"{question_id}:{question.answer}" if question.answer is not None else None
        items = [item for item in case.items if item.item_id != replaced]
        if effect is not None and effect.kind is not None:
            items.append(
                CaseItem(
                    item_id=f"{question_id}:{option_id}"[:128],
                    kind=effect.kind,
                    title=effect.title,
                    source=ProposalSource.RULES,
                    confidence=AgentConfidence.SURE,
                    state=ProposalState.ACCEPTED,
                    upload_id=question.upload_id or _str(effect.payload.get("upload_id")),
                    payload=dict(effect.payload),
                    payload_digest=payload_digest(effect.payload),
                    proposed_at=now,
                    decided_by=actor,
                    decided_at=now,
                    goal_id=question.goal_id,
                )
            )
        answered = question.model_copy(update={"answer": option_id, "answered_by": actor, "answered_at": now})
        questions = [answered if q.question_id == question_id else q for q in case.questions]
        return _Step(
            to=VisitState.TREAT if case.state in _AFTER_RECALL else case.state,
            updates={"items": items, "questions": questions},
            events=[
                EventDraft(
                    kind=TimelineKind.QUESTION_ANSWERED,
                    upload_id=question.upload_id,
                    item_id=question_id,
                    details={"option": option_id, "changed": replaced is not None},
                )
            ],
        )

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def decide(
    client_id: str,
    case_id: str,
    item_id: str,
    accept: bool,
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    op_id: str | None = None,
) -> Case:
    """Tick or untick an item of the active section; the visit goes back to TREAT and previews again."""
    run = _Run(deps, actor)
    now = deps.clock()

    def build(case: Case) -> _Step:
        item = next((i for i in case.items if i.item_id == item_id), None)
        if item is None or not _in_section(item.goal_id, case):
            raise _unknown("There is no such suggestion in this case (or its goal is set aside).")
        state = ProposalState.ACCEPTED if accept else ProposalState.REJECTED
        decided = item.model_copy(update={"state": state, "decided_by": actor, "decided_at": now})
        return _Step(
            to=VisitState.TREAT if case.state in _AFTER_RECALL else case.state,
            updates={"items": [decided if i.item_id == item_id else i for i in case.items]},
            events=[
                EventDraft(
                    kind=TimelineKind.ITEM_DECIDED,
                    upload_id=item.upload_id,
                    item_id=item_id,
                    details={"state": state.value},
                )
            ],
        )

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def approve(
    client_id: str,
    case_id: str,
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    op_id: str | None = None,
) -> Case:
    """The person approves what the preview showed: recorded with their user id and the sentence they read."""
    run = _Run(deps, actor)
    now = deps.clock()

    def build(case: Case) -> _Step:
        if case.state is not VisitState.APPROVE:
            raise _not_waiting("There is nothing waiting for approval now.")
        items, _ = active_section(case)
        goal = active_goal(case)
        approval = CaseApproval(
            approval_id=f"approval.{len(case.approvals) + 1}",
            item_ids=tuple(i.item_id for i in items if i.state is ProposalState.ACCEPTED),
            approved_by=actor,
            approved_at=now,
            text_shown=approval_sentence(case),
        )
        return _Step(
            to=VisitState.PRESCRIBE,
            updates={"approvals": (*case.approvals, approval)},
            events=[
                EventDraft(
                    kind=TimelineKind.APPROVAL,
                    item_id=approval.approval_id,
                    details={
                        "items": len(approval.item_ids),
                        "goal": goal.goal_id if goal is not None else None,
                    },
                )
            ],
        )

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def chat(
    client_id: str,
    case_id: str,
    message: str,
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    op_id: str | None = None,
) -> Case:
    """The person writes to Data Doctor: the message is masked and kept, then CONVERSE answers it."""
    from engine.pii import redact_text

    run = _Run(deps, actor)
    now = deps.clock()
    text = redact_text(message[:4_000])[0]

    def build(case: Case) -> _Step:
        if case.state not in _CHAT_FROM:
            raise _not_waiting("Data Doctor is still reading the files; write again in a moment.")
        entry = CaseChatEntry(role="user", text=text, at=now, by=actor)
        return _Step(
            to=VisitState.CONVERSE,
            updates={"chat": (*case.chat, entry)},
            events=[EventDraft(kind=TimelineKind.CHAT, details={"role": "user"})],
        )

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def _unknown(message: str) -> CaseError:
    return CaseError("VISIT_UNKNOWN_CHOICE", message)


def _not_waiting(message: str) -> CaseError:
    return CaseError("VISIT_NOT_WAITING", message)
