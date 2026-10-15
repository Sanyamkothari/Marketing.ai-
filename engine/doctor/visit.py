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
from engine.doctor.protocol import (
    DATA_DECISION,
    DATA_PRESCRIPTION_APPROVED,
    DEFAULT_SETTINGS,
    DONT_KNOW,
    Ask,
    DataRequest,
    LedgerPort,
    ProtocolSettings,
    approve_class_of,
    confirmation_refusal,
    decision_body,
    item_sentence,
    question_budget,
    request_data,
    settle,
)
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
    from engine.doctor.ai_guard import AiGuard
    from engine.doctor.diagnose import LoadedFile, Offer
    from engine.generative.guardrails import Guardrails
    from engine.storage import Storage

__all__ = [
    "AI_NEEDED_REPLY",
    "VISIT_CODES",
    "CombinePort",
    "ConversePort",
    "ConverseResult",
    "Detected",
    "DetectorPort",
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
    "approval_goal_name",
    "approval_sentence",
    "approve",
    "brief_key",
    "chat",
    "confirm",
    "confirmed",
    "decide",
    "item_proposers",
    "lazy_converse",
    "mode_of",
    "open_blocking",
    "plan_g_converse",
    "prescription_digest",
    "readiness_verify",
    "resume",
    "revoke_standing",
    "set_goal",
    "stored_brief",
    "unconfirmed",
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
    tokens: int = 0
    ai: bool = True
    """False: no AI service answered (the reply is :data:`AI_NEEDED_REPLY`); the timeline records which."""


class ConversePort(Protocol):
    def __call__(self, case: Case, message: str, files: tuple[LoadedFile, ...]) -> ConverseResult: ...


def lazy_converse(build: Callable[[], ConversePort | None]) -> ConversePort:
    """A CONVERSE port that connects to the AI service only when a chat turn needs it, then keeps it.

    Every visit call can carry it at no cost: a resume that finds a message waiting in CONVERSE (the process
    died during the AI call) answers it with the AI service, as the chat would have. `build` returns None when
    no AI service is connected; then the reply is :data:`AI_NEEDED_REPLY`.
    """
    built: list[ConversePort | None] = []

    def converse(case: Case, message: str, files: tuple[LoadedFile, ...]) -> ConverseResult:
        if not built:
            built.append(build())
        port = built[0]
        if port is None:
            return ConverseResult(reply=AI_NEEDED_REPLY, ai=False)
        return port(case, message, files)

    return converse


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


class CombinePort(Protocol):
    """Builds the table an approved join or stack makes from the whole files, keeps it as a new upload and returns
    it as the case's file. The files it was made from are not touched."""

    def __call__(self, case: Case, item: CaseItem, actor: str) -> CaseFile: ...


@dataclass(frozen=True)
class Detected:
    """Plan O M246 (DEC-1816): what a detector found - ambiguities to settle (AUTO, an assumption, or one
    question), doubts (three parts, every number grounded in `evidence`), and requests for more data."""

    asks: tuple[Ask, ...] = ()
    doubts: tuple[CaseDoubt, ...] = ()
    requests: tuple[DataRequest, ...] = ()
    evidence: tuple[DataToolResult, ...] = ()
    items: tuple[CaseItem, ...] = ()
    """Proposals the detector makes outright (the cleaning fixes): pending, or ticked when the values prove them."""


class DetectorPort(Protocol):
    """Plan O M246: one detector of the design §7.1 (M245 fills them in). Rules only; it may call tools."""

    def __call__(self, case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected: ...


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
    combine: CombinePort | None = None
    """None: an approved join or stack builds nothing (the files stay as they are)."""
    clock: Callable[[], datetime] = utc_now
    store: CaseStore | None = None
    """None: a `CaseStore` over `storage` with `clock`."""
    # Plan O M246 (DEC-1816): the protocol's ports.
    detectors: Sequence[DetectorPort] = ()
    """The design §7.1's detectors, run in TREAT after the rules; empty: none (M245 adds them)."""
    ledger: LedgerPort | None = None
    """Plan K's ledger when `agentic.enabled` is on; None: the case timeline alone holds the record."""
    protocol: ProtocolSettings = DEFAULT_SETTINGS

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


def approval_sentence(case: Case, *, goal_name: str | None = None) -> str:
    """The sentence the person approves, word for word (recorded with the approval).

    Plan O M247 (DEC-1817): the goal is named as the screens name it (`goal_display_name`: its display name, else
    `goal_name` - the brief's configured name, see :func:`approval_goal_name` - else its id), never by its id alone.
    """
    from engine.doctor.goal_names import goal_display_name

    items, _ = active_section(case)
    ticked = sum(1 for item in items if item.state is ProposalState.ACCEPTED)
    goal = active_goal(case)
    what = f"{ticked} change{'s' if ticked != 1 else ''}"
    files = len(case.files)
    to = f" for the goal “{goal_display_name(goal.goal_id, goal_name)}”" if goal is not None else ""
    return f"Prepare {files} file{'s' if files != 1 else ''} with {what}{to}. Nothing else changes."


def approval_goal_name(storage: Storage, case: Case) -> str | None:
    """The configured name of the active goal (its stored brief's `goal_name`), for :func:`approval_sentence`."""
    brief = stored_brief(storage, case)
    return brief.goal_name if brief is not None else None


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
    tokens: int = 0
    after: list[Callable[[Case], None]] = field(default_factory=list)
    """Plan O M246: side effects once the step is written (a standing answer's use or memory); each is
    idempotent, and none may fail the step."""
    within: Callable[[Case], dict[str, Any]] | None = None
    """Plan O M246: the step's record - Plan K's ledger entry of an approval, a standing answer's revocation - run
    inside the case's write, under its lock and after its version check, so it is made only for a write that
    goes ahead. It returns updates merged into the step's (the approval with its receipt). It is idempotent: a
    write that fails after it (or a process that dies) is retried, and the retry finds what it made."""


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
        updates = dict(step.updates)
        if step.within is not None:  # first, so a refusal here writes nothing at all
            updates.update(step.within(current))
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
            # Plan O M246: a question the rules settled (an assumption) or a standing answer applied is not one
            # the person was asked, so it is not counted in the file's question budget.
            settled = question.assumed or question.standing_answer_id is not None
            if question.upload_id is not None and not settled:
                asked_per_file[question.upload_id] = asked_per_file.get(question.upload_id, 0) + 1
            drafts.append(
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
        doubts = list(current.doubts)
        have_doubts = {doubt.doubt_id for doubt in doubts}
        for doubt in step.doubts:
            if doubt.doubt_id not in have_doubts:
                have_doubts.add(doubt.doubt_id)
                doubts.append(doubt)
                drafts.append(  # Plan O M246: a doubt raised is on the timeline
                    EventDraft(
                        kind=TimelineKind.DOUBT,
                        upload_id=doubt.upload_id,
                        item_id=doubt.doubt_id,
                        details={"attached_to": doubt.item_id, "evidence": len(doubt.evidence_ids)},
                    )
                )
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
        if step.to is VisitState.INTAKE and not current.files and not updates.get("files"):
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
                "tokens": current.counters.tokens + step.tokens,
                "cost_usd": current.counters.cost_usd + step.cost_usd,
                "questions_per_file": asked_per_file,
            }
        )
        run.tool_calls = 0
        changed = current.model_copy(
            update={
                **updates,
                "state": step.to,
                "evidence": tuple(refs),
                "items": tuple(updates.get("items", items)),
                "questions": tuple(updates.get("questions", questions)),
                "doubts": tuple(doubts),
                "visit": new_visit,
                "counters": counters,
            }
        )
        return changed, drafts

    return run.store.update(
        case.client_id, case.case_id, expected_version=case.version, actor=run.actor, change=change
    )


def _asked_kind(question: CaseQuestion) -> TimelineKind:
    """How a new question enters the timeline: asked, settled by an assumption, a standing answer, a request."""
    from engine.doctor.protocol import REQUEST_CODES

    if question.standing_answer_id is not None:
        return TimelineKind.STANDING_APPLIED
    if question.assumed:
        return TimelineKind.ASSUMED
    if question.code in REQUEST_CODES.values():
        return TimelineKind.DATA_REQUESTED
    return TimelineKind.QUESTION_ASKED


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
        _after(step, case)
    return case


def _after(step: _Step, case: Case) -> None:
    """Run a written step's side effects; they never undo or fail the step."""
    for effect in step.after:
        try:
            effect(case)
        except Exception as exc:
            _LOG.warning("visit.after_failed case=%s error=%s", case.case_id, type(exc).__name__)


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
        _detect(run, case, brief, step)
        _standing(run, case, step)
        step.visit["done"] = {**visit.done, "treat": inputs}
    asked = {question.question_id for question in case.questions}
    blocking = [
        q
        for q, _effects in step.questions
        if q.blocking and q.answer is None and q.question_id not in asked and _in_section(q.goal_id, case)
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
        ConverseResult(reply=AI_NEEDED_REPLY, ai=False)
        if port is None
        else port(case, message.text, run.files(case))
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
                details={"role": "agent", "ai": result.ai, "proposals": len(items)},
            )
        ],
        model_calls=result.model_calls,
        cost_usd=result.cost_usd,
        tokens=result.tokens,
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


def _combine(run: _Run, case: Case) -> _Step | None:
    """The approved joins and stacks, built: each one's files give way to the one table it makes, and the visit
    reads that table from INTAKE, so what is approved next is the table that goes on."""
    from engine.doctor.combine_run import JOIN_KIND, STACK_KIND, upload_ids_of

    port = run.deps.combine
    done = (case.visit or CaseVisit()).combined
    items, _ = active_section(case)
    todo = [i for i in items if i.kind in {JOIN_KIND, STACK_KIND} and i.state is ProposalState.ACCEPTED]
    todo = [i for i in todo if i.item_id not in done]
    if port is None or not todo:
        return None
    files = list(case.files)
    events: list[EventDraft] = []
    for item in todo:
        sources = set(upload_ids_of(item))
        if not sources or not sources <= {f.upload_id for f in files}:
            continue  # an earlier change already took one of its files
        made = port(case, item, run.actor)
        files = [f for f in files if f.upload_id not in sources] + [made]
        events.append(
            EventDraft(
                kind=TimelineKind.FILE_ADDED,
                upload_id=made.upload_id,
                item_id=item.item_id,
                details={"combined": len(sources)},
            )
        )
    return _Step(
        to=VisitState.INTAKE,
        updates={"files": tuple(files)},
        visit={"combined": (*done, *(i.item_id for i in todo))},
        events=events,
    )


def _prescribe(run: _Run, case: Case) -> _Step:
    """PRESCRIBE: the approved items, the answers and the brief, hashed (M243 writes the prescription)."""
    combined = _combine(run, case)
    if combined is not None:
        return combined
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


def _record_hash(deps: VisitDeps, case: Case, brief: GoalBrief | None) -> str | None:
    """The `prescription_hash` M246's approval record carries: the default digest, or - Plan O M243 (DEC-1813) - the
    hash the PRESCRIBE port will write, when the port can say it without writing (`CasePrescriber.digest`)."""
    if deps.prescribe is None:
        return prescription_digest(case, brief)
    digest_of = getattr(deps.prescribe, "digest", None)
    return str(digest_of(case, brief)) if callable(digest_of) else None


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
    from engine.doctor.fingerprint import record_fingerprint

    brief = stored_brief(run.deps.storage, case)
    port = run.deps.hand_off
    record = port(case, brief) if port is not None else _default_hand_off(case, brief)
    # What the client's data looked like when it was approved: next visit's files are compared with it.
    remembered = record_fingerprint(run.deps.storage, case, run.files(case), brief, at=run.deps.clock())
    return _Step(
        to=VisitState.FOLLOW_UP,
        visit={"hand_off": record},
        events=[
            EventDraft(
                kind=TimelineKind.HAND_OFF,
                item_id=record.goal_id,
                details={
                    "target": record.target,
                    "run_id": record.run_id,
                    "files": len(record.upload_ids),
                    "fingerprint_saved": remembered is not None,
                },
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
                # Plan O M246 (DEC-1816; the design §6.8): ask for the data, not just stop.
                _add_request(
                    step,
                    DataRequest(
                        kind="more_data",
                        what=f"a column saying whether: {brief.outcome.definition.rstrip('.')}",
                        why=f"{brief.goal_name} learns from it.",
                        columns=tuple(c for c in (brief.outcome.column,) if c),
                        goal_id=goal,
                    ),
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
    guard: AiGuard | None = None,
) -> ConversePort:
    """CONVERSE as Plan G's `chat_turn` (the design §3.4): the same tools, egress gate, guardrails and budgets.

    With a `guard` (which is then also the `meter`), the AI is asked only when the client has allowed it and the
    case has spending left; otherwise the reply says so and the visit goes on with its rules.

    `context_for` gives the file the chat is about (Plan G's chat reads one file). The turn runs on a session
    opened for it, holding the case's chat so far; its pending proposals become the case's items (`ai`, `check`,
    never ticked): a setting belongs to the active goal's section, a step to the base.
    """

    def converse(case: Case, message: str, files: tuple[LoadedFile, ...]) -> ConverseResult:
        from engine.agent.contracts import ChatMessage, ChatRole
        from engine.agent.loop import chat_turn
        from engine.agent.session import start_session
        from engine.doctor.ai_guard import AI_LIMIT_REPLY, AI_NO_CONSENT_REPLY, file_overviews
        from engine.doctor.registry import evidence_id_of

        if guard is not None:
            allowed = guard.ask(case)
            if allowed != "ok":
                return ConverseResult(
                    reply=AI_NO_CONSENT_REPLY if allowed == "no_consent" else AI_LIMIT_REPLY, ai=False
                )
        ctx = context_for(case, files)
        # Every other file of the case is looked at too, each through its own egress gate.
        others = file_overviews([context_for(case, (f,)) for f in files if f.upload_id != ctx.upload_id])
        tokens_before = 0 if guard is None else guard.tokens_used()
        calls_before = 0 if guard is None else guard.calls_used()
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
            case_files=others,
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
                    # Plan O M246 (DEC-1816): decision O10's warning, when the AI's step changes many values.
                    **_warnings_of(proposal.evidence_ids, evidence),
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
        out_of_money = guard is not None and turn.blocked_by == "unavailable" and guard.spent()
        return ConverseResult(
            reply=AI_LIMIT_REPLY if out_of_money else turn.reply.text,
            items=items,
            evidence=results,
            sent=tuple(item.model_dump(mode="json") for item in turn.reply.sent),
            model_calls=turn.llm_calls if guard is None else guard.calls_used() - calls_before,
            tokens=0 if guard is None else guard.tokens_used() - tokens_before,
        )

    return converse


def _warnings_of(evidence_ids: Sequence[str], results: Mapping[str, Any]) -> dict[str, JsonValue]:
    """`{"warnings": [...]}` from the chat's own step result a proposal cites (M246), or nothing."""
    found: list[JsonValue] = []
    for evidence_id in evidence_ids:
        result = results.get(evidence_id)
        raw = getattr(result, "result", None)
        warnings = raw.get("warnings") if isinstance(raw, dict) else None
        if isinstance(warnings, list):
            found.extend(str(w) for w in warnings if str(w) not in found)
    return {"warnings": found} if found else {}


# ---------------------------------------------------------------------------
# The protocol in TREAT (Plan O M246, DEC-1816): detectors, assumptions, the budget, standing answers
# ---------------------------------------------------------------------------
def _ground(case: Case, doubt: CaseDoubt, outputs: Mapping[str, Any], names: Sequence[str]) -> None:
    """Every number in a doubt is in a result it cites (`DOUBT_UNGROUNDED` otherwise)."""
    from engine.doctor.protocol import ungrounded_in

    cited = {e: outputs[e] for e in doubt.evidence_ids if e in outputs}
    loose = ungrounded_in(doubt, cited, names)
    if loose:
        raise AgentToolError(
            "DOUBT_UNGROUNDED",
            f"A doubt about case {case.case_id} states a number ({loose[0][:20]}) none of its results holds.",
        )


def _add_request(step: _Step, request: DataRequest) -> None:
    step.questions.append(request_data(request))


def _detect(run: _Run, case: Case, brief: GoalBrief | None, step: _Step) -> None:
    """The detectors' findings (M245 fills the port): asks settled by the protocol, grounded doubts, requests."""
    if not run.deps.detectors:
        return
    files = run.files(case)
    names = sorted({str(c) for loaded in files for c in loaded.frame.columns})
    asks: list[Ask] = []
    for detector in run.deps.detectors:
        found = detector(case, files, brief)
        step.evidence.extend(found.evidence)
        step.items.extend(found.items)
        outputs = {result.evidence_id: result.output for result in found.evidence}
        for doubt in found.doubts:
            _ground(case, doubt, outputs, names)
            step.doubts.append(doubt)
        for request in found.requests:
            _add_request(step, request)
        asks.extend(found.asks)
    _settle_asks(run, case, step, asks)


def _settle_asks(run: _Run, case: Case, step: _Step, asks: Sequence[Ask]) -> None:
    """AUTO, a stated assumption or one question per ambiguity, within the file's question budget (§6.3)."""
    now = run.deps.clock()
    settings = run.deps.protocol
    have = {q.question_id for q in case.questions} | {q.question_id for q, _e in step.questions}
    asked = dict(case.counters.questions_per_file)
    known = {q.question_id for q in case.questions}
    for pending, _effects in step.questions:  # the rules' own questions of this step count too
        if pending.upload_id is not None and pending.answer is None and pending.question_id not in known:
            asked[pending.upload_id] = asked.get(pending.upload_id, 0) + 1
    ambiguities: dict[str, int] = {}
    for ask in asks:
        ambiguities[ask.upload_id or ""] = ambiguities.get(ask.upload_id or "", 0) + 1
    for ask in asks:
        key = ask.upload_id or ""
        budget = question_budget(asked.get(key, 0) + ambiguities[key], settings)
        settled = settle(ask, now=now, settings=settings, over_budget=asked.get(key, 0) >= budget)
        if settled.question is not None:
            if settled.question.question_id in have:
                continue  # asked once: never again, in any mode
            have.add(settled.question.question_id)
            step.questions.append((settled.question, settled.effects))
            if settled.how == "ask":
                asked[key] = asked.get(key, 0) + 1
        if settled.item is not None:
            step.items.append(settled.item)


def _standing(run: _Run, case: Case, step: _Step) -> None:
    """Apply this client's standing answers to the questions TREAT would ask now: pre-ticked, visible, revocable."""
    from engine.doctor.case import StandingAnswerUse
    from engine.doctor.standing import STANDING_CODES, StandingAnswerStore, describe, may_stand, meaning_of

    candidates = {
        q.question_id
        for q, _e in step.questions
        if q.code in STANDING_CODES and q.answer is None and _in_section(q.goal_id, case)
    }
    if not candidates:
        return
    store = StandingAnswerStore(run.deps.storage)
    now = run.deps.clock()
    have = {q.question_id for q in case.questions}
    uses = list(case.standing_answers_used)
    kept: list[tuple[CaseQuestion, tuple[OptionEffect, ...]]] = []
    for question, effects in step.questions:
        answer = None
        if question.question_id not in have and question.question_id in candidates:
            answer = store.find(case.client_id, question)
        option = (
            None
            if answer is None
            else next((o for o in question.options if o.label == answer.option_label), None)
        )
        effect = None if option is None else {e.option_id: e for e in effects}.get(option.option_id)
        if answer is not None and (
            option is None or effect is None or answer.meaning != meaning_of(effect.kind, effect.payload)
        ):
            # The same question, but the choice behind the earlier answer now does something else (or is gone):
            # it is asked again, and says why, never applied to a change the person did not choose.
            kept.append((_no_longer_fits(question, answer), effects))
            step.events.append(
                EventDraft(
                    kind=TimelineKind.STANDING_UNFIT,
                    upload_id=question.upload_id,
                    item_id=answer.answer_id,
                    details={"code": question.code, "question": question.question_id},
                )
            )
            continue
        if (
            answer is None
            or option is None
            or effect is None
            or not may_stand(question, effect.kind, effect.payload)
        ):
            kept.append((question, effects))
            continue
        kept.append(
            (
                question.model_copy(
                    update={
                        "answer": option.option_id,
                        "answered_by": answer.answered_by,
                        "answered_at": now,
                        "standing_answer_id": answer.answer_id,
                    }
                ),
                effects,
            )
        )
        if effect.kind is not None:
            step.items.append(
                CaseItem(
                    item_id=f"{question.question_id}:{option.option_id}"[:128],
                    kind=effect.kind,
                    title=f"{describe(answer)}. {effect.title}".strip(),
                    source=ProposalSource.RULES,
                    confidence=AgentConfidence.SURE,
                    state=ProposalState.ACCEPTED,
                    upload_id=question.upload_id or _str(effect.payload.get("upload_id")),
                    payload=dict(effect.payload),
                    payload_digest=payload_digest(effect.payload),
                    proposed_at=now,
                    decided_by=answer.answered_by,
                    decided_at=now,
                    goal_id=question.goal_id,
                    standing_answer_id=answer.answer_id,
                )
            )
        uses.append(
            StandingAnswerUse(
                answer_id=answer.answer_id,
                detector=question.code,
                upload_id=question.upload_id,
                column=question.column,
                value_signature=answer.value_signature,
                option_id=option.option_id,
                effect_digest=option.effect_digest,
                applied_at=now,
            )
        )
        step.after.append(functools.partial(_used, store, answer.answer_id))
    step.questions = kept
    if len(uses) != len(case.standing_answers_used):
        step.updates["standing_answers_used"] = tuple(uses)


def _no_longer_fits(question: CaseQuestion, answer: Any) -> CaseQuestion:
    """The question, saying that the client's earlier answer was not applied because it no longer fits."""
    when = answer.answered_at.strftime("%d %b").lstrip("0")
    note = (
        f"Your answer from {when} (“{answer.option_label}”) was not used: that choice now makes a different "
        "change, so please answer again."
    )
    return question.model_copy(update={"why": f"{note} {question.why}".strip()})


def _used(store: Any, answer_id: str, written: Case) -> None:
    store.used(written.client_id, answer_id, written.case_id)


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
    """Apply one action of the person as one transition, then run the machine to its next rest.

    The action is written first and the machine advanced after, so both a retry whose first attempt was written
    (the process may have died before advancing) and a race with another writer end at the next rest: an
    applied action never answers `CASE_VERSION_CONFLICT`.
    """
    case = run.store.get(client_id, case_id)
    if _seen(case, op_id):
        # its first attempt was written: nothing is applied twice, and the visit goes on to its next rest
        return case if case.read_only else _settle(run, client_id, case_id, case, applied=True)
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
    _after(step, case)
    return _settle(run, client_id, case_id, case, applied=True)


_SETTLE_TRIES: Final[int] = 3


def _settle(run: _Run, client_id: str, case_id: str, case: Case, *, applied: bool = False) -> Case:
    """Advance to the next rest; when another writer moved the case on, read it again and continue from there.

    `applied`: the person's action is already written, so a writer that keeps racing does not make it fail;
    the case is answered as that writer left it.
    """
    for _ in range(_SETTLE_TRIES):
        try:
            return _advance(run, case)
        except CaseError as exc:
            if exc.code != "CASE_VERSION_CONFLICT":
                raise
            case = run.store.get(client_id, case_id)
    if applied:
        try:
            return _advance(run, case)
        except CaseError as exc:
            if exc.code != "CASE_VERSION_CONFLICT":
                raise
            return run.store.get(client_id, case_id)
    return _advance(run, case)


def resume(client_id: str, case_id: str, deps: VisitDeps, *, actor: str = AGENT) -> Case:
    """Continue the visit from the last state written: after a crash, a restart, or a new file."""
    run = _Run(deps, actor)
    case = run.store.get(client_id, case_id)
    if case.read_only:
        return case
    return _settle(run, client_id, case_id, case)


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
    """Add files to the case; the visit reads them from INTAKE (next month's file: FOLLOW_UP -> INTAKE).

    Plan O M243 (DEC-1813): a file added to a case that already has a stored prescription is bound to it, so a run
    on next month's file replays the approved preparation (and stops and asks where it changed)."""
    run = _Run(deps, actor)
    added: list[str] = []

    def build(case: Case) -> _Step:
        have = set(case.upload_ids())
        new = tuple(file for file in files if file.upload_id not in have)
        added[:] = [file.upload_id for file in new]
        events = [EventDraft(kind=TimelineKind.FILE_ADDED, upload_id=file.upload_id) for file in new]
        to = VisitState.INTAKE if new and case.state not in {VisitState.INTAKE} else case.state
        return _Step(to=to, updates={"files": (*case.files, *new)} if new else {}, events=events)

    case = _person_step(run, client_id, case_id, expected_version, op_id, build)
    if added:
        from engine.doctor.prescription_runs import bind_case_uploads

        bind_case_uploads(deps.storage, case, added)
    return case


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
        answered = question.model_copy(
            update={
                "answer": option_id,
                "answered_by": actor,
                "answered_at": now,
                "assumed": False,  # Plan O M246: the person's answer replaces an assumption or a standing answer
                "standing_answer_id": None,
            }
        )
        questions = [answered if q.question_id == question_id else q for q in case.questions]
        events = [
            EventDraft(
                kind=TimelineKind.QUESTION_ANSWERED,
                upload_id=question.upload_id,
                item_id=question_id,
                details={
                    "option": option_id,
                    "changed": replaced is not None,
                    # Plan O M246: whether the answer changes the data; questions that never do are candidates
                    # for deciding without asking (the design §6.5).
                    "changes_data": effect is not None and effect.kind is not None,
                },
            )
        ]
        remember = _remember_answer(run, case, answered, effect, actor=actor, now=now)
        if remember is not None:
            events.append(
                EventDraft(
                    kind=TimelineKind.STANDING_REMEMBERED,
                    upload_id=question.upload_id,
                    item_id=remember[0],
                    details={"code": question.code, "question": question_id},
                )
            )
        return _Step(
            to=VisitState.TREAT if case.state in _AFTER_RECALL else case.state,
            updates={"items": items, "questions": questions},
            events=events,
            # The client-wide memory is written once the answer is: a refused answer remembers nothing.
            after=[] if remember is None else [remember[1]],
        )

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def _remember_answer(
    run: _Run, case: Case, question: CaseQuestion, effect: OptionEffect | None, *, actor: str, now: datetime
) -> tuple[str, Callable[[Case], None]] | None:
    """Plan O M246: when the answer may stand (never APPROVE-class, never "I don't know"), its standing answer's
    id and the write that remembers it for the client - run only once the case holds the answer (`_Step.after`),
    so a refused or conflicting answer is never remembered. Keyed by its content: a retry writes the same."""
    from engine.doctor.standing import StandingAnswerStore, answer_id_for, may_stand, meaning_of

    option = next((o for o in question.options if o.option_id == question.answer), None)
    if option is None or option.label == DONT_KNOW or (effect is None and option.effect_digest is not None):
        return None
    kind = None if effect is None else effect.kind
    payload: Mapping[str, Any] = {} if effect is None else effect.payload
    if not may_stand(question, kind, payload):
        return None
    title = effect.title if effect is not None and effect.title else option.label

    def remember(written: Case) -> None:
        StandingAnswerStore(run.deps.storage).remember(
            written.client_id,
            question,
            option_id=option.option_id,
            effect_digest=option.effect_digest,
            meaning=meaning_of(kind, payload),
            title=title,
            actor=actor,
            at=now,
        )

    return answer_id_for(case.client_id, question), remember


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
    sentence: str | None = None,
) -> Case:
    """The person approves what the preview showed: recorded with their user id and the sentence they read.

    Plan O M246 (DEC-1816): refused while an accepted APPROVE-class item lacks its own yes
    (`APPROVAL_CONFIRMATION_MISSING`, :func:`confirm`); `sentence`, when sent, must be the sentence the server shows
    now, word for word (`APPROVAL_SENTENCE_CHANGED`: nothing is recorded, the error carries the new one). The
    record - the items' digests and the sentence, the prescription hash - goes to Plan K's ledger as
    `data.prescription.approved` when `deps.ledger` is set (`agentic.enabled`), and to the timeline always."""
    from engine.doctor.patterns import record_approved

    run = _Run(deps, actor)
    now = deps.clock()

    def build(case: Case) -> _Step:
        if case.state is not VisitState.APPROVE:
            raise _not_waiting("There is nothing waiting for approval now.")
        missing = unconfirmed(case, deps.protocol)
        if missing:
            raise CaseError(
                "APPROVAL_CONFIRMATION_MISSING",
                f"{len(missing)} change{'s' if len(missing) != 1 else ''} need{'' if len(missing) != 1 else 's'} "
                f"their own yes first: “{missing[0].title}”.",
            )
        text = approval_sentence(case, goal_name=approval_goal_name(run.deps.storage, case))
        if sentence is not None and sentence != text:
            raise CaseError(
                "APPROVAL_SENTENCE_CHANGED",
                f"What would be approved has changed since you read it. It now reads: “{text}”",
            )
        items, _ = active_section(case)
        goal = active_goal(case)
        accepted = [i for i in items if i.state is ProposalState.ACCEPTED]
        brief = stored_brief(run.deps.storage, case)
        approval_id = f"approval.{len(case.approvals) + 1}"
        body = decision_body(
            case_id=case.case_id,
            upload_id=case.files[0].upload_id if len(case.files) == 1 else None,
            item_digests=[i.payload_digest for i in accepted],
            text=text,
            decided_by=actor,
            kind="prescription",
            prescription_hash=_record_hash(deps, case, brief),
            approval_id=approval_id,
            case_version=case.version,
        )
        approval = CaseApproval(
            approval_id=approval_id,
            item_ids=tuple(i.item_id for i in accepted),
            approved_by=actor,
            approved_at=now,
            text_shown=text,
            payload_digest=_str(body["payload_digest"]),
        )
        ai_steps = [
            (str(step["kind"]), dict(params) if isinstance(params := step.get("params"), dict) else {})
            for i in accepted
            if i.source is ProposalSource.AI
            and isinstance(step := i.payload.get("step"), dict)
            and isinstance(step.get("kind"), str)
        ]
        after: list[Callable[[Case], None]] = []

        def count_patterns(written: Case) -> None:
            record_approved(
                run.deps.storage, ai_steps, place=written.case_id, client_id=written.client_id, at=now
            )

        if ai_steps:  # [REQ] 11: an approved AI pattern counts towards a candidate rule for the product team
            after.append(count_patterns)
        return _Step(
            to=VisitState.PRESCRIBE,
            within=_recorded(deps, DATA_PRESCRIPTION_APPROVED, actor, body, approval),
            events=[
                EventDraft(
                    kind=TimelineKind.APPROVAL,
                    item_id=approval.approval_id,
                    details={
                        "items": len(approval.item_ids),
                        "goal": goal.goal_id if goal is not None else None,
                        "receipt": "ledger" if deps.ledger is not None else "timeline",
                        **_timeline_body(body),
                    },
                )
            ],
            after=after,
        )

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def _record(deps: VisitDeps, kind: str, actor: str, body: Mapping[str, Any], ref: str) -> str:
    """The receipt of one decision: Plan K's ledger entry hash when the ledger is on, else the approval's place in
    the case's timeline. A ledger that cannot be written refuses the decision."""
    if deps.ledger is not None:
        try:
            return f"ledger:{deps.ledger(kind, actor, body)}"
        except Exception as exc:
            code = getattr(exc, "code", "LEDGER_CHAIN_BROKEN")
            raise CaseError(str(code), "The decision could not be recorded; nothing was approved.") from exc
    return f"timeline:{ref}"


def _recorded(
    deps: VisitDeps, kind: str, actor: str, body: Mapping[str, Any], approval: CaseApproval
) -> Callable[[Case], dict[str, Any]]:
    """The approval's record, made inside the case's write (:attr:`_Step.within`): after the version check, so
    no entry is written for an approval the case does not store, and idempotent (the body names the approval and
    the case version; :func:`engine.doctor.protocol.ledger_port`), so a retry finds the entry instead of adding
    one. Returns the case's approvals with this one and its receipt."""

    def within(current: Case) -> dict[str, Any]:
        receipt = _record(deps, kind, actor, body, approval.approval_id)
        return {"approvals": (*current.approvals, approval.model_copy(update={"receipt_id": receipt}))}

    return within


def _timeline_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """The ledger body as the timeline holds it: the same keys but `case_id`, which is the timeline's own case."""
    return {key: value for key, value in body.items() if key != "case_id"}


def _approval_required(deps: VisitDeps, case: Case) -> bool:
    """The active goal's `governance.approval_required` (separation of duties, O17); False with no goal."""
    from engine.config import load_use_case

    goal = active_goal(case)
    if goal is None:
        return False
    return bool(load_use_case(goal.goal_id, deps.config_root).governance.approval_required)


def _proposers(case: Case, item: CaseItem) -> tuple[str, ...]:
    """Everyone who proposed an item or answered the question it came from (separation of duties, O17).

    An item a question's answer added (`<question_id>:<option_id>`) was proposed by whoever answered it; whoever
    ticked or unticked it since (`decided_by`) took part too. Data Doctor (the rules, the AI) is no one."""
    found: list[str] = []
    origin = next((q for q in case.questions if item.item_id.startswith(f"{q.question_id}:")), None)
    for who in (origin.answered_by if origin is not None else None, item.decided_by):
        if who is not None and who != AGENT and who not in found:
            found.append(who)
    return tuple(found)


def item_proposers(case: Case, item: CaseItem) -> tuple[str, ...]:
    """Everyone who proposed or answered an item (`_proposers`): what :func:`confirm` checks separation against,
    so the screen can say before a click who may give the yes (Plan O M247)."""
    return _proposers(case, item)


def confirmed(case: Case, item: CaseItem) -> bool:
    """Whether an APPROVE-class item has its own yes for what it says now (its payload and its sentence)."""
    from engine.doctor.protocol import approval_digest

    wanted = approval_digest([item.payload_digest], item_sentence(item.title, item.payload))
    return any(
        approval.approve_class and item.item_id in approval.item_ids and approval.payload_digest == wanted
        for approval in case.approvals
    )


def unconfirmed(case: Case, settings: ProtocolSettings = DEFAULT_SETTINGS) -> tuple[CaseItem, ...]:
    """The accepted APPROVE-class items of the active section still waiting for their own yes."""
    items, _ = active_section(case)
    return tuple(
        item
        for item in items
        if item.state is ProposalState.ACCEPTED
        and approve_class_of(item.kind, item.payload, settings) is not None
        and not confirmed(case, item)
    )


def confirm(
    client_id: str,
    case_id: str,
    item_id: str,
    deps: VisitDeps,
    *,
    principal: Principal,
    sentence: str | None,
    expected_version: int,
    op_id: str | None = None,
) -> Case:
    """Plan O M246 (DEC-1816): the person's own yes to one APPROVE-class item, with the exact sentence they read.

    Refused - and nothing recorded - when the approver lacks the item's role (`TOOL_ROLE_REQUIRED`), the sentence
    is not the one the server shows now (`APPROVAL_SENTENCE_CHANGED`, with the new sentence), or separation of
    duties is on and they proposed or answered it themselves (`POLICY_ROLE_SEPARATION`). The yes ticks the item.
    """
    from engine.doctor.protocol import item_kind, role_for

    run = _Run(deps, principal.user_id)
    now = deps.clock()

    def build(case: Case) -> _Step:
        items, _ = active_section(case)
        item = next((i for i in items if i.item_id == item_id), None)
        if item is None:
            raise _unknown("There is no such suggestion in this case (or its goal is set aside).")
        rule = approve_class_of(item.kind, item.payload, deps.protocol)
        if rule is None:
            raise _unknown("This change does not need its own yes; it is approved with the others.")
        text = item_sentence(item.title, item.payload)
        proposed_by = _proposers(case, item)
        required = _approval_required(deps, case)
        refused = confirmation_refusal(
            rule=rule,
            approver=principal,
            sentence_read=sentence,
            sentence_now=text,
            proposed_by=proposed_by,
            approval_required=required,
        )
        if refused is not None:
            reason = str(refused.detail.get("reason", "Not allowed."))
            if refused.code == "APPROVAL_SENTENCE_CHANGED":
                reason = f"{reason} It now reads: “{text}”"
            raise CaseError(refused.code or "POLICY_NO_RULES", reason)
        role = role_for(rule, approval_required=required)
        approval_id = f"approval.{len(case.approvals) + 1}"
        body = decision_body(
            case_id=case.case_id,
            upload_id=item.upload_id,
            item_digests=[item.payload_digest],
            text=text,
            decided_by=principal.user_id,
            proposed_by=proposed_by,
            kind=item_kind(item.kind, item.payload),
            identity_verified=False if principal.kind != "user" else None,
            approval_id=approval_id,
            case_version=case.version,
        )
        approval = CaseApproval(
            approval_id=approval_id,
            item_ids=(item.item_id,),
            approved_by=principal.user_id,
            approved_at=now,
            text_shown=text,
            approve_class=True,
            payload_digest=_str(body["payload_digest"]),
            role=role.value,
        )
        ticked = (
            item
            if item.state is ProposalState.ACCEPTED
            else item.model_copy(
                update={"state": ProposalState.ACCEPTED, "decided_by": principal.user_id, "decided_at": now}
            )
        )
        return _Step(
            to=VisitState.TREAT if case.state in _AFTER_RECALL else case.state,
            updates={"items": [ticked if i.item_id == item_id else i for i in case.items]},
            within=_recorded(deps, DATA_DECISION, principal.user_id, body, approval),
            events=[
                EventDraft(
                    kind=TimelineKind.CONFIRMED,
                    upload_id=item.upload_id,
                    item_id=item.item_id,
                    details={
                        "approval": approval.approval_id,
                        "role": role.value,
                        "receipt": "ledger" if deps.ledger is not None else "timeline",
                        **_timeline_body(body),
                    },
                )
            ],
        )

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


def revoke_standing(
    client_id: str,
    case_id: str,
    answer_id: str,
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    op_id: str | None = None,
) -> Case:
    """Plan O M246 (DEC-1816): revoke a standing answer from a case. It stops standing for the client, and in this
    case its question is asked again and the item it ticked goes (`STANDING_ANSWER_NOT_FOUND` for none)."""
    from engine.doctor.protocol import ProtocolError
    from engine.doctor.standing import StandingAnswerStore

    run = _Run(deps, actor)
    now = deps.clock()
    store = StandingAnswerStore(deps.storage)

    def revoke(current: Case) -> dict[str, Any]:
        # Inside the case's write, after its version check (`_Step.within`): a refused or conflicting revoke
        # changes nothing for the client. Revoking twice is revoking once, so a retry is safe.
        try:
            store.revoke(client_id, answer_id, actor=actor, at=now)
        except ProtocolError as exc:
            raise CaseError(exc.code, exc.message) from exc
        del current
        return {}

    def build(case: Case) -> _Step:
        if store.get(client_id, answer_id) is None:
            raise CaseError(
                "STANDING_ANSWER_NOT_FOUND", "There is no such remembered answer for this client."
            )
        questions = [
            (
                q.model_copy(
                    update={
                        "answer": None,
                        "answered_by": None,
                        "answered_at": None,
                        "standing_answer_id": None,
                    }
                )
                if q.standing_answer_id == answer_id
                else q
            )
            for q in case.questions
        ]
        uses = tuple(
            (
                u.model_copy(update={"revoked_at": now})
                if u.answer_id == answer_id and u.revoked_at is None
                else u
            )
            for u in case.standing_answers_used
        )
        return _Step(
            to=VisitState.TREAT if case.state in _AFTER_RECALL else case.state,
            updates={
                "questions": questions,
                "items": [i for i in case.items if i.standing_answer_id != answer_id],
                "standing_answers_used": uses,
            },
            events=[EventDraft(kind=TimelineKind.STANDING_REVOKED, item_id=answer_id, details={"by": actor})],
            within=revoke,
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
