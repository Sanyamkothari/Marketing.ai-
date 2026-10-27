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
APPROVE -> PRESCRIBE by itself, when the plan comes back after an approval with only sure fixes new (carry.py)
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
chat (`engine.doctor.agent_chat`): the AI review's loop with the conversation in front of it. Without an AI service
the person's message is kept and the reply says the chat needs one, and the visit goes back to TREAT. The AI makes
pending items; the ones whose guard run shows no stop and no flag, in the kinds listed in
:data:`AGENT_APPLIED_STEPS`, are applied for the person (decided by the agent, one Undo each, on the timeline).
Nothing about meaning is ever applied or answered by the AI: it may suggest an answer, and only a click records it.

**What later milestones fill in.** PREVIEW, PRESCRIBE, VERIFY and HAND_OFF call ports (:class:`VisitDeps`). The
defaults are the honest minimum today: PREVIEW counts what would change; PRESCRIBE hashes the approved items, the
answers and the brief (M243 writes the prescription itself); VERIFY runs M234's `check_readiness` on the file that
holds the goal's outcome (:func:`readiness_verify`); HAND_OFF records where the data goes and starts nothing.

Nothing here is reachable while `data_doctor.unified` is off (`api/routes/case_visit.py` is mounted only with it on).
Nothing here logs a value or a column name: counts, ids and codes only.
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from pydantic import JsonValue

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agent.tools import AgentToolError
from engine.config import ConfigError
from engine.doctor.agent_apply import AGENT_APPLIED_STEPS, agent_applies, person_owns, roles_of, twin_of
from engine.doctor.carry import approved_marks, carries_on, last_month, plain_fix
from engine.doctor.case import (
    PARK_ROLES,
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
    ChatSuggestion,
    EventDraft,
    TimelineKind,
    VisitState,
    case_dir,
    evidence_key,
    payload_digest,
)
from engine.doctor.decisions import decisions, sets_of_effect
from engine.doctor.goal_brief import GOAL_BRIEF_TOOL, GoalBrief, load_goal_brief, register_goal_brief_tool
from engine.doctor.materialize import Full, materialize, stop_finding
from engine.doctor.plan_check import (
    open_stops,
    plan_check_of,
    plan_digest,
    readiness_acknowledged,
    stop_is_open,
    with_full_findings,
)
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
    DataToolRegistry,
    DataToolResult,
)
from engine.doctor.visit_contracts import (
    Acknowledgement,
    CaseVisit,
    HandOffRecord,
    OptionEffect,
    PlanCheck,
    PlanFinding,
    VisitStop,
    WaitingFor,
)
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.access.roles import Principal
    from engine.doctor.diagnose import LoadedFile, Offer
    from engine.storage import Storage

__all__ = [
    "AGENT_APPLIED_STEPS",
    "AI_NEEDED_REPLY",
    "CARRIED_KINDS",
    "COMBINE_KINDS",
    "VISIT_CODES",
    "CombinePort",
    "ConversePort",
    "ConverseResult",
    "Detected",
    "DetectorPort",
    "HandOffPort",
    "PrepareError",
    "PrescribePort",
    "PreviewPort",
    "PreviewSummary",
    "VerifyPort",
    "VerifyResult",
    "VisitDeps",
    "acknowledge_stop",
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
    "is_approved",
    "item_proposers",
    "lazy_converse",
    "mode_of",
    "open_blocking",
    "plan_change_count",
    "plan_change_items",
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

VISIT_CODES: Final[frozenset[str]] = frozenset(
    {"VISIT_NOT_WAITING", "VISIT_UNKNOWN_CHOICE", "VISIT_ANSWER_VALUE_INVALID"}
)
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
    asks: tuple[Ask, ...] = ()
    doubts: tuple[CaseDoubt, ...] = ()
    requests: tuple[DataRequest, ...] = ()
    withdrawn: tuple[str, ...] = ()
    """Ids of the agent's own earlier changes it took back (waiting, or applied by the agent itself)."""
    suggestion: tuple[str, str] | None = None
    """`(question_id, option_id)`: the answer it suggests to an open question; never recorded by the AI."""
    suggestions: tuple[tuple[str, str], ...] = ()
    """Every answer it suggests, one per open question (`suggestion` alone is the older single form)."""
    tools: tuple[str, ...] = ()
    """What it did to answer, in order (the looks and the actions)."""
    model_calls: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    ai: bool = True
    """False: no AI service answered (the reply is :data:`AI_NEEDED_REPLY`); the timeline records which."""
    blocked_by: str | None = None
    """Why the AI's own answer is missing, when it is: `no_consent` and `budget` (the two replies that ask nothing),
    `unavailable` (the AI service did not answer), `prompt_too_large`, the code of a refusal it met twice, or `failed`
    (a step of the turn broke). None when it answered, and when the reply is :data:`AI_NEEDED_REPLY` (no AI service is
    connected: `ai` says so)."""
    error_codes: tuple[str, ...] = ()
    """For an AI service that did not answer, the codes that say why, in order: the failure's own code (`LLM_UNAVAILABLE`;
    `LLM_REFUSED` for a model that ran out of room; `BUDGET_EXCEEDED`), then the kind the HTTP client found
    (`LLM_KEY_EXPIRED`, `LLM_OUT_OF_ROOM` ...); written here, never the service's own words."""


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
    findings: tuple[PlanFinding, ...] = ()
    """What the whole-file check found, for the case's ledger: the guards' counts on every row and each readiness
    requirement that fails. A verdict that is not ready with findings rests at APPROVE with them to read and, unless
    hard, to say yes to; with none it goes back to TREAT."""


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
    model_calls: int = 0
    tokens: int = 0
    """What an AI detector (the review) used; counted on the case, so the spending limit holds across visits."""
    summary: str = ""
    sent: tuple[dict[str, JsonValue], ...] = ()
    """The review's plain-language summary for the chat, and what it sent to the AI service."""
    blocked_by: str | None = None
    error_codes: tuple[str, ...] = ()
    reply: str = ""
    """Set when the AI service did not answer the review's first call (`blocked_by` is `unavailable`): the codes that
    say why (as `ConverseResult.error_codes`) and the sentence the person reads (`ConverseResult.reply`). The review
    did not happen, so TREAT does not remember it and asks again when it runs next."""


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
    load_full: Callable[[Case], Sequence[LoadedFile]] | None = None
    """The case's files with every row (up to the profile row cap), for the whole-file count before APPROVE and at
    VERIFY. None: the rows `load_files` gives (the whole file when it fits)."""
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
    agent: DetectorPort | None = None
    """The AI review: one more check, run once after the rules' pass, when nothing blocking is open. It decides
    for itself that it may not run (no AI service, no consent, no spending left) and then finds nothing."""
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


_APPROVED_STATES: Final[frozenset[VisitState]] = frozenset(
    {VisitState.PRESCRIBE, VisitState.VERIFY, VisitState.HAND_OFF, VisitState.FOLLOW_UP}
)


def is_approved(case: Case) -> bool:
    """Whether the person approved the plan as it stands: the visit is past APPROVE and an approval of the whole
    plan (not just one change's own yes) is recorded. Any later change takes the visit back before APPROVE."""
    return case.state in _APPROVED_STATES and any(not a.approve_class for a in case.approvals)


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


COMBINE_KINDS: Final[frozenset[str]] = frozenset({"build_dataset", "stack_files"})
"""The kinds of the approved join and stack (`engine.doctor.combine_run.JOIN_KIND`, `STACK_KIND`)."""
CARRIED_KINDS: Final[frozenset[str]] = frozenset({"outcome_column", "as_of_column", "consent_column"})
"""The decisions on what the data means; they stay with the case when a join or stack makes its files one table."""
_CHECK_KIND: Final[str] = "data_check"
"""A note or a flag for a person to read: nothing changes, so it is never counted as a change."""


def plan_change_items(case: Case) -> tuple[CaseItem, ...]:
    """The changes in the plan: the ticked items of the active section that change something, on the tables as they
    stand now. What was said about a file before a join or stack made it into one table is that table's (the join
    itself and the decisions on what the data means stay)."""
    items, _ = active_section(case)
    here = {f.upload_id for f in case.files}
    return tuple(
        item
        for item in items
        if item.state is ProposalState.ACCEPTED
        and item.kind != _CHECK_KIND
        and (
            item.upload_id is None
            or item.upload_id in here
            or item.kind in COMBINE_KINDS
            or item.kind in CARRIED_KINDS
        )
    )


def plan_change_count(case: Case) -> int:
    """How many changes the plan holds. The one number: the approval sentence, the first look and the checklist all
    say this one."""
    return len(plan_change_items(case))


def approval_sentence(case: Case, *, goal_name: str | None = None) -> str:
    """The sentence the person approves, word for word (recorded with the approval).

    Plan O M247 (DEC-1817): the goal is named as the screens name it (`goal_display_name`: its display name, else
    `goal_name` - the brief's configured name, see :func:`approval_goal_name` - else its id), never by its id alone.
    """
    from engine.doctor.goal_names import goal_display_name

    ticked = plan_change_count(case)
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
        self._full: tuple[LoadedFile, ...] | None = None

    def files(self, case: Case) -> tuple[LoadedFile, ...]:
        if self._files is None or tuple(f.upload_id for f in self._files) != case.upload_ids():
            self._files = tuple(self.deps.load_files(case))
        return self._files

    def full_files(self, case: Case) -> tuple[LoadedFile, ...]:
        """The files with every row, for the count of the whole plan (read once per call)."""
        if self.deps.load_full is None:
            return self.files(case)
        if self._full is None or tuple(f.upload_id for f in self._full) != case.upload_ids():
            self._full = tuple(self.deps.load_full(case))
        return self._full

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
    carried: bool = False
    """An approval the visit made by itself (`engine.doctor.carry`): if its record cannot be made, the visit
    waits for the person instead of failing whatever they just did."""
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
        try:
            case = _write(run, case, step)
        except CaseError:
            if not step.carried:
                raise
            # the automatic approval could not be recorded (nothing was approved): the person's yes is asked
            return run.store.get(case.client_id, case.case_id)
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
    here = {f.upload_id for f in case.files}
    made = {
        f.upload_id
        for f in case.files
        if f.original_upload_id in visit.taken_in and f.original_upload_id not in here
    }  # the table the approved join or stack made: its files gave way to it
    fresh = any(upload_id not in made for upload_id in new)
    round_: dict[str, Any] = {}
    if fresh and not case.approvals:
        # a new case of a client whose last month's table looks like this one starts from that approval
        found = last_month(run.deps.storage, case, list(files.values()))
        round_ = {"approved_before": found[0] if found else (), "carry_source": found[1] if found else None}
    elif fresh and visit.approved_before:
        round_ = {"approved_before": (), "carry_source": None}  # a file the person never saw is another round
    return _Step(
        to=VisitState.UNDERSTAND,
        visit={"taken_in": tuple(dict.fromkeys((*visit.taken_in, *new))), **round_},
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
        decisions(case).digest,  # what the answers and accepted items decide (the as-of date, the roles)
        # A decided outcome whose query the person left out is asked about again (the rules run once more).
        goal is not None
        and (decided := decisions(case).pair("outcome", goal.goal_id)) is not None
        and _outcome_is_gone(case, run.files(case), decided),
    )
    visit = case.visit or CaseVisit()
    step = _Step(to=VisitState.PREVIEW)
    if visit.done.get("treat") != inputs:
        _base_rules(run, case, step)
        if brief is None:
            _generic_rules(run, case, step)
        else:
            _goal_rules(run, case, brief, step)
        review = _digest(
            [(f.upload_id, f.fingerprint) for f in case.files],
            goal.goal_id if goal is not None else None,
            brief.brief_hash if brief is not None else None,
        )  # the AI review's inputs: not the answers or the ticks, so neither pays for another review
        _detect(run, case, brief, step)
        _standing(run, case, step)
        reviewed = visit.done.get("agent") != review and _review(run, case, brief, step)
        step.visit["done"] = {**visit.done, "treat": inputs, **({"agent": review} if reviewed else {})}
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
    """ASK: waits while a blocking question of the active section is open; then back to TREAT.
    A file added while it waits goes to INTAKE first, as at APPROVE."""
    if _pending_files(case):
        return _Step(to=VisitState.INTAKE)
    if open_blocking(case):
        return None
    return _Step(to=VisitState.TREAT)


def _converse(run: _Run, case: Case) -> _Step:
    """CONVERSE: the chat's turn on the person's last message. What the AI makes lands pending; the clean changes
    are applied for the person to undo; its own earlier changes it took back are rejected. No AI: a fixed reply.
    """
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
    step = _Step(
        to=VisitState.TREAT,
        evidence=list(result.evidence),
        model_calls=result.model_calls,
        cost_usd=result.cost_usd,
        tokens=result.tokens,
    )
    made = [
        item.model_copy(
            update={
                "source": ProposalSource.AI,
                "state": ProposalState.PENDING,
                "confidence": AgentConfidence.CHECK,
            }
        )
        for item in result.items  # the AI never ticks: whatever a port returns lands pending, `check`
    ]
    # The same change in other words (the id hashes the model's wording) is the change already there: a person's
    # "no" stands, and an applied one is not applied twice.
    twins = {item.item_id: twin_of(case.items, item) for item in made}
    fresh = [item for item in made if twins[item.item_id] is None]
    kept_ids = {t.item_id for t in twins.values() if t is not None}
    back = _taken_back(
        case, [i for i in result.withdrawn if i not in kept_ids], {item.item_id for item in fresh}
    )
    plan = [_decided_as(i, ProposalState.REJECTED, now) if i.item_id in back else i for i in case.items]
    active = [i for i in plan if _in_section(i.goal_id, case)]
    placed = (
        [
            item
            for item, _waits in agent_applies(
                run.files(case),
                case.files,
                active,
                fresh,
                now,
                roles_of(case, run.deps.config_root),
                decisions(case).as_of_date,
            )
        ]
        if fresh
        else []
    )
    step.items.extend(placed)
    if back:  # `_write` adds new items itself; a change to an existing one is written whole
        step.updates["items"] = [*plan, *placed]
    step.events.extend(
        EventDraft(
            kind=TimelineKind.ITEM_DECIDED,
            upload_id=next((i.upload_id for i in case.items if i.item_id == item_id), None),
            item_id=item_id,
            details={"state": "rejected", "by": "agent", "withdrawn": True},
        )
        for item_id in back
    )
    step.events.extend(
        EventDraft(
            kind=TimelineKind.ITEM_DECIDED,
            upload_id=item.upload_id,
            item_id=item.item_id,
            details={"state": "accepted", "by": "agent"},
        )
        for item in placed
        if item.state is ProposalState.ACCEPTED
    )
    step.doubts.extend(result.doubts)
    for request in result.requests:
        _add_request(step, request)
    _settle_asks(run, case, step, result.asks, counted=False)
    text = result.reply
    again = list(
        {t.item_id: t for t in twins.values() if t is not None and t.state is ProposalState.REJECTED}.values()
    )
    if again:  # an earlier "no" stands: the reply says so instead of claiming work that was not done
        text += (
            f" I did not put back “{again[0].title}”: it was left out earlier; tick it again if you want it."
        )
    left_out = {i.item_id for i in again}
    given = result.suggestions or ((result.suggestion,) if result.suggestion is not None else ())
    shown = [found for found in (_suggested(case, step, g) for g in given) if found is not None]
    suggestion = shown[0] if shown else None
    if len(shown) < len(given):  # a question was settled meanwhile: no button for it
        text += " (The answer I pointed at is no longer open, so there is nothing to click for it.)"
    entry = CaseChatEntry(
        role="agent",
        text=text,
        at=now,
        sent=result.sent,
        item_ids=tuple(
            dict.fromkeys(
                [i.item_id for i in placed]
                + [t.item_id for t in twins.values() if t is not None and t.item_id not in left_out]
            )
        ),
        withdrawn=tuple(back),
        tools=result.tools,
        suggestion=suggestion,
        suggestions=tuple(shown),
        blocked_by=result.blocked_by,
        error_codes=result.error_codes,
    )
    step.updates["chat"] = (*case.chat, entry)
    chat_event = EventDraft(
        kind=TimelineKind.CHAT,
        details={
            "role": "agent",
            "ai": result.ai,
            "proposals": len(fresh),
            "applied": sum(1 for i in placed if i.state is ProposalState.ACCEPTED),
            "withdrawn": len(back),
        },
    )
    _say_why(chat_event, result.blocked_by, result.error_codes)
    step.events.append(chat_event)
    return step


def _say_why(event: EventDraft, blocked_by: str | None, error_codes: Sequence[str]) -> None:
    """Add to a `chat.message` event why the AI did not answer, when it did not, so the timeline tells an expired key
    from a spent case. A detail is one value, so the codes are joined by commas; both are left out while empty, as in
    the chat entry."""
    if blocked_by:
        event.details["blocked_by"] = blocked_by
    if error_codes:
        event.details["error_codes"] = ",".join(error_codes)


def _decided_as(item: CaseItem, state: ProposalState, now: datetime) -> CaseItem:
    return item.model_copy(
        update={"state": state, "decided_by": AGENT, "decided_at": now, "parked_for": None}
    )


def _taken_back(case: Case, ids: Sequence[str], made: set[str]) -> list[str]:
    """The agent's own changes it may take back: waiting for the person, or applied by the agent itself. A change the
    person ticked, unticked or gave their own yes to is theirs, and one made in this very turn is not an earlier one.
    """
    mine = {i.item_id: i for i in case.items}
    found: list[str] = []
    for item_id in dict.fromkeys(ids):
        item = mine.get(item_id)
        if (
            item is not None
            and item_id not in made
            and item.source is ProposalSource.AI
            and not person_owns(case, item)
            and (
                item.state is ProposalState.PENDING
                or (item.state is ProposalState.ACCEPTED and item.decided_by == AGENT)
            )
        ):
            found.append(item_id)
    return found


def _suggested(case: Case, step: _Step, given: tuple[str, str] | None) -> ChatSuggestion | None:
    """The answer the agent points at, if that question is open and the answer is a real one. The person's click
    records it; nothing here does."""
    if given is None:
        return None
    question_id, option_id = given
    asked = [q for q, _effects in step.questions]
    question = next((q for q in (*case.questions, *asked) if q.question_id == question_id), None)
    if question is None or question.answer is not None:
        return None
    option = next((o for o in question.options if o.option_id == option_id), None)
    if option is None or option.safe or option.label == DONT_KNOW or option.value_kind is not None:
        return None  # a suggestion carries no value, so it cannot point at an answer that needs one
    return ChatSuggestion(question_id=question_id, option_id=option_id, label=option.label)


def _preview(run: _Run, case: Case) -> _Step:
    """PREVIEW: what would change, tried on at most 1,000 rows per file. No AI."""
    files = run.files(case)
    summary = run.deps.preview(case, files) if run.deps.preview is not None else _count_preview(case, files)
    items, _ = active_section(case)
    check = _counted(run, case, items)  # counted once per plan; the screen and Approve read it
    visit = case.visit or CaseVisit()
    same_plan = visit.plan_check is not None and visit.plan_check.digest == check.digest
    return _Step(
        to=VisitState.APPROVE,
        visit={"plan_check": check, "acknowledged": visit.acknowledged if same_plan else ()},
        events=[
            EventDraft(
                kind=TimelineKind.PREVIEW,
                details={
                    "files": summary.files,
                    "rows_tried": summary.rows_tried,
                    "ticked": summary.items_ticked,
                    "pending": summary.items_pending,
                    "stops": sum(1 for f in check.findings if f.level == "stop"),
                    **dict(summary.details),
                },
            )
        ],
    )


def _counted(run: _Run, case: Case, items: Sequence[CaseItem]) -> PlanCheck:
    """The plan counted on every row of its files (:class:`Full`), so the whole file's stops are known BEFORE the
    person approves, not after. Kept by the plan's digest and the as-of date it was counted under: the same plan
    is not counted again (the stops VERIFY adds to it stay)."""
    visit = case.visit or CaseVisit()
    digest = plan_digest(case.files, items)
    basis = _digest(digest, decisions(case).digest)
    kept = visit.plan_check
    if kept is not None and kept.digest == digest and kept.basis == basis:
        return kept
    return plan_check_of(materialize(case, items, run.full_files(case), Full()), basis=basis)


def _count_preview(case: Case, files: tuple[LoadedFile, ...]) -> PreviewSummary:
    items, _ = active_section(case)
    return PreviewSummary(
        files=len(files),
        rows_tried=sum(min(PREVIEW_ROWS, loaded.profile.row_count) for loaded in files),
        items_ticked=sum(1 for item in items if item.state is ProposalState.ACCEPTED),
        items_pending=sum(1 for item in items if item.state is ProposalState.PENDING),
    )


def _approve_wait(run: _Run, case: Case) -> _Step | None:
    """APPROVE: waits for the person (:func:`approve`). A file added while it waits is read first: the plan the
    person is shown must cover every file the case holds, so back to INTAKE (the same as FOLLOW_UP does).

    One approval (`engine.doctor.carry`): when the plan comes back after the person approved, and what is new is
    only sure fixes with no stop or flag, the visit carries on by itself, in the name of who approved before, and
    says so on the timeline. Anything else waits for the person."""
    if _pending_files(case):
        return _Step(to=VisitState.INTAKE)
    before = [a for a in case.approvals if not a.approve_class]
    visit = case.visit or CaseVisit()
    items, questions = active_section(case)
    if visit.plan_check is not None and visit.plan_check.basis is None:
        # A case kept when only a sample was counted: the whole file is counted now and the count is KEPT, so the
        # screen shows the stops approval would stop at, and a yes is asked for those. The old yeses were given to
        # the sample's stops (keys that no longer exist), so the person is asked again.
        return _Step(
            to=VisitState.APPROVE,
            visit={"plan_check": _counted(run, case, items), "acknowledged": ()},
        )
    check = visit.plan_check
    if (
        check is not None
        and any(f.code == "VERIFY_NOT_READY" for f in check.findings)
        and visit.done.get("verify_inputs") != _verify_inputs(case, stored_brief(run.deps.storage, case))
    ):
        # The no-progress stop was written for other answers or confirmations: what it asked has been done, so the
        # stop goes and the check runs again when the person approves.
        return _Step(
            to=VisitState.APPROVE,
            visit={
                "plan_check": check.model_copy(
                    update={"findings": tuple(f for f in check.findings if f.code != "VERIFY_NOT_READY")}
                ),
                "done": {k: v for k, v in visit.done.items() if k not in {"verify_back", "verify_inputs"}},
            },
        )
    # no approval yet in this case: last month's is what it goes on from
    by, from_ = (
        (before[-1].approved_by, before[-1].approval_id)
        if before
        else (
            (visit.carry_source.approved_by, f"{visit.carry_source.approval_id}@{visit.carry_source.case_id}")
            if visit.carry_source is not None
            else ("", "")
        )
    )
    if not from_ or not visit.approved_before or visit.plan_check is None:
        return None
    if visit.plan_check.digest != plan_digest(case.files, items):
        return None
    settings = run.deps.protocol
    if unconfirmed(case, settings):
        return None
    carry = carries_on(case, items, questions, visit.plan_check, confirmed=confirmed, settings=settings)
    if carry is None:
        return None
    try:
        return _approval(
            run,
            case,
            actor=by,
            carried_from=from_,
            carried_new=sum(1 for i in carry.new if plain_fix(i, settings)),
        )
    except CaseError:
        return None  # a stop, a changed sentence: the person decides


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
        files = [f for f in files if f.upload_id not in sources and f.upload_id != made.upload_id] + [made]
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
            "answers": sorted(
                (
                    (q.question_id, q.answer or "")
                    if q.answer_value is None
                    else (q.question_id, q.answer or "", q.answer_value)
                )  # a typed date is part of the answer
                for q in questions
                if q.answer is not None
            ),
            "files": list(case.upload_ids()),
        }
    )


def _verify(run: _Run, case: Case) -> _Step:
    """VERIFY: the readiness verdict for the active goal (M234), or straight on with no goal.

    A verdict that is not ready never throws its reason away. What the whole-file check found (the guards' stops on
    every row, each readiness requirement that fails) is written into the plan's ledger as whole-file findings and
    the visit rests at APPROVE with them: the person reads each one on the screen and says yes to it
    (`acknowledge_stop`) unless it is a hard rule, or changes the plan. A not-ready verdict with nothing to show
    goes back to TREAT once; the same plan and the same reason again rests at APPROVE with a stop that says so
    (no-progress guard), so the visit never goes round TREAT, APPROVE and VERIFY on its own."""
    brief = stored_brief(run.deps.storage, case)
    port = run.deps.verify or readiness_verify(
        run.deps.storage, run.deps.config_root, full_files=run.full_files
    )
    if brief is None:
        result = VerifyResult(verdict="no_goal", reason="No goal is chosen: pick one to check it will train.")
    else:
        result = port(case, brief, run.files(case))
    updates: dict[str, Any] = {}
    if result.ref is not None:
        ref = result.ref.model_copy(update={"plan_digest": prescription_digest(case, brief)})
        updates["readiness"] = (*(r for r in case.readiness if r.goal_id != ref.goal_id), ref)
    ready = result.verdict in {"ready", "ready_with_warnings", "no_goal"}
    to: VisitState = VisitState.HAND_OFF
    visit_update: dict[str, Any] = {}
    found = result.findings
    if not ready:
        to, visit_update, found = _not_ready(case, result, brief)
    return _Step(
        to=to,
        updates=updates,
        visit=visit_update,  # not ready: the plan comes back whole, with the reason
        events=[
            EventDraft(
                kind=TimelineKind.READINESS,
                item_id=brief.goal_id if brief is not None else None,
                details={
                    "verdict": result.verdict,
                    "reason": result.reason[:200],
                    "stops": sum(1 for f in found if f.level == "stop"),
                    # Ties this check to the stored report it made, so the screen never shows an older check's reasons.
                    "report_hash": result.ref.report_hash if result.ref is not None else None,
                },
            )
        ],
    )


def _verify_inputs(case: Case, brief: GoalBrief | None) -> str:
    """What a VERIFY verdict depends on beyond the files: the plan's ticked items, the answers, the brief and each
    confirmation (the person's own yes to an APPROVE-class change). Answering a question or confirming a change
    moves it, so a stop written for the old inputs is not held against the new ones."""
    return _digest(
        prescription_digest(case, brief),
        sorted(a.payload_digest or "" for a in case.approvals if a.approve_class),
    )


def _not_ready(
    case: Case, result: VerifyResult, brief: GoalBrief | None
) -> tuple[VisitState, dict[str, Any], tuple[PlanFinding, ...]]:
    """Where a verdict that is not ready goes, what the visit keeps, and the findings it writes to the ledger.

    The no-progress guard reads the stops of the verdict, not its flags and notes: a verdict with only flags says
    nothing the person can say yes to, so it goes back to TREAT once and then rests with a stop of its own."""
    items, _ = active_section(case)
    visit = case.visit or CaseVisit()
    digest = plan_digest(case.files, items)
    inputs = _verify_inputs(case, brief)
    found = result.findings
    again = _digest(digest, result.reason, inputs)
    if not any(f.level == "stop" for f in found) and visit.done.get("verify_back") == again:
        # Back from TREAT with the same plan, the same answers and the same reason: nothing the visit can do by
        # itself changed it.
        found = (
            *found,
            stop_finding(
                "VERIFY_NOT_READY",
                f"{result.reason.removeprefix('Not ready: ')} Nothing in the plan changed since the last check "
                "said so: change the plan, answer a question, or change the data.",
                Full(),
            ),
        )
    if not any(f.level == "stop" for f in found):
        return (
            VisitState.TREAT,
            {"approved_before": (), "done": {**visit.done, "verify_back": again}},
            found,
        )
    kept = visit.plan_check
    current = kept is not None and kept.digest == digest
    check = with_full_findings(kept if current and kept is not None else PlanCheck(digest=digest), found)
    return (
        VisitState.APPROVE,
        {
            "plan_check": check,
            "acknowledged": visit.acknowledged if current else (),
            "approved_before": (),
            "done": {**visit.done, "verify_back": again, "verify_inputs": inputs},
        },
        found,
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
        return _Step(to=VisitState.INTAKE, visit={"approved_before": ()})  # a new month is another round
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
    """A rules' proposal; its id is its content, so proposing it again finds it. What a trial on the first rows
    counted (`tried`) is not part of what the change is: the rules' and the chat's version of one step are one.
    """
    item_id = f"{kind}.{goal_id or 'base'}.{_short({k: v for k, v in payload.items() if k != 'tried'})}"
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
            sets=sets_of_effect(effect.kind),
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


_ID_NAMED: Final[re.Pattern[str]] = re.compile(
    r"(^|[_\s-])id$|^id[_\s-]|_key$|(customer|cust|client|member|account)[_\s-]?(id|no|num|number|code|ref)$",
    re.I,
)
"""A column named like the ID of a person or an account: `customer_id`, `Customer ID`, `account_no`, `member_code`."""


def _id_named(unique: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Of the columns with a different value on every row, the ID-named ones when there are any: a spend or a
    score that happens to be unique does not compete with `customer_id`. Two ID-named columns still compete.
    """
    named = [key for key in unique if _ID_NAMED.search(str(key["column"]).strip())]
    return named or unique


def _base_rules(run: _Run, case: Case, step: _Step) -> None:
    """What does not depend on a goal: which column names each row of each file (the key)."""
    now = run.deps.clock()
    ctx = run.context(case)
    for loaded in run.files(case):
        upload_id = loaded.upload_id
        if decisions(case).key(upload_id) is not None:
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
        unique = _id_named(unique)
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
    outcome = decisions(case).pair("outcome", goal)
    if outcome is not None and _outcome_is_gone(case, files, outcome):
        step.doubts.append(
            CaseDoubt(
                doubt_id=f"d.outcome_gone.{goal}"[:128],
                because=f"the outcome “{outcome[1]}” is in no file and in no table the plan still makes",
                would_do="ask again which column says it once the plan makes a table that has one",
                if_not=f"{brief.goal_name} cannot be checked",
            )
        )
        return
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
            if not likely:
                likely = _prepared_outcomes(case, brief, files)
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
        from engine.doctor.combine_run import current_upload

        # The table an approved join made from the outcome's file is where the date and consent are looked for now.
        table = current_upload(case, outcome[0])
        _time_rule(run, case, brief, table, step, now)
        _consent_rule(case, brief, table, step, files, now)


_STEM: Final[int] = 5
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "the",
        "and",
        "that",
        "after",
        "before",
        "with",
        "from",
        "this",
        "day",
        "days",
        "date",
        "snapshot",
        "placed",
    }
)
"""Words that say nothing about which column is the outcome (every window is in days, after a snapshot)."""


def _stems(text: str) -> set[str]:
    """The word-starts of a name or a sentence (`ordered_again_90d` and "placed another order in the 90 days" share
    `order` and `90`): letters cut to five, numbers whole."""
    words = re.findall(r"[a-z]+|\d+", text.lower())
    return {w[:_STEM] for w in words if w not in _STOP_WORDS and (len(w) >= 3 or w.isdigit())}


def _query_tables(
    case: Case, files: Sequence[LoadedFile]
) -> list[tuple[CaseItem, LoadedFile, dict[str, Any], list[str]]]:
    """The query steps of the active goal's section that make a table (accepted, or proposed by the AI and not yet
    answered): `(item, the file the query reads, the step, the names of the columns it makes)`. A step that runs
    after the build, with no file of its own, is left out when there are several files: the prescription carries it
    only for a single file."""
    from engine.doctor.prescription import slot_name

    found: list[tuple[CaseItem, LoadedFile, dict[str, Any], list[str]]] = []
    for item in active_section(case)[0]:
        step = item.payload.get("step")
        columns = item.payload.get("columns")
        if (
            not isinstance(step, dict)
            or step.get("kind") != "sql_query"
            or not isinstance(columns, list)
            or not (item.state is ProposalState.ACCEPTED or item.source is ProposalSource.AI)
            or item.state is ProposalState.REJECTED
            or (item.upload_id is None and len(files) > 1 and item.payload.get("stage") == "post_build")
        ):
            continue
        given = step.get("params")
        params: dict[str, Any] = given if isinstance(given, dict) else {}
        inputs = params.get("inputs")
        reads = [t for t in (inputs if isinstance(inputs, list) else []) if isinstance(t, str)]
        origin = next(
            (
                lf
                for lf in files
                if (item.upload_id is not None and lf.upload_id == item.upload_id)
                or (item.upload_id is None and slot_name(Path(lf.file_name or "").stem) in reads)
            ),
            files[0] if files and item.upload_id is None else None,
        )
        if origin is not None:
            found.append((item, origin, step, [str(c) for c in columns]))
    return found


def _prepared_outcomes(
    case: Case, brief: GoalBrief, files: Sequence[LoadedFile]
) -> list[tuple[LoadedFile, str]]:
    """The columns the case's own query steps make that read as the goal's outcome, best first: a column named like
    it or a synonym, then one whose name shares two of the outcome's words, then one sharing one word while the
    step's description shares two (the AI names its columns freely). Except for the first kind, the column must be
    two-valued on the rows the step was tried on (a yes/no), so an order count or a date is never offered. Only
    candidates for the person's question, never a decision. Each is a `(file, column)` pair."""
    named = {_norm(n) for n in (brief.outcome.column, *brief.outcome.synonyms) if n}
    words = _stems(f"{brief.outcome.definition} {brief.outcome.column or ''}")
    ranked: list[tuple[int, int, LoadedFile, str]] = []
    for item, origin, step, columns in _query_tables(case, files):
        given = step.get("params")
        params: dict[str, Any] = given if isinstance(given, dict) else {}
        said = _stems(f"{step.get('reason') or ''} {params.get('description') or ''}")
        binary = item.payload.get("two_valued")
        for column in columns:
            mine = _stems(column)
            if _norm(column) in named or set(_named([column], [*named], parts=True)):
                tier = 0
            elif len(mine & words) >= 2:
                tier = 1
            elif len(mine & words) >= 1 and len(said & words) >= 2:
                tier = 2
            else:
                continue
            if tier and isinstance(binary, list) and column not in binary:
                continue
            ranked.append((tier, len(ranked), origin, column))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [(origin, column) for _t, _i, origin, column in ranked]


def _outcome_is_gone(case: Case, files: Sequence[LoadedFile], decided: tuple[str, str]) -> bool:
    """True when the decided outcome column is in no file and in no query step still in the plan (the person left
    the step out, or the AI proposed it again with other names)."""
    upload_id, column = decided
    raw = next((lf for lf in files if lf.upload_id == upload_id), None)
    if raw is None or column in {str(c) for c in raw.frame.columns}:
        return False
    return not any(column in made for _i, _o, _s, made in _query_tables(case, files))


def _time_rule(run: _Run, case: Case, brief: GoalBrief, upload_id: str, step: _Step, now: datetime) -> None:
    """The date each row describes, for a goal whose split or outcome window needs one."""
    need = brief.column("time")
    if need is None or decisions(case).pair("as_of_column", brief.goal_id) is not None:
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
    if need is None or decisions(case).pair("consent", brief.goal_id) is not None:
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
class PrepareError(Exception):
    """`readiness_verify`'s `prepare` cannot prepare the file under the approved changes; the message is the reason.

    `code` is the refusal's own code when it has one (a `RECIPE_*` or `PRESCRIPTION_*` code): the ledger shows it as
    a hard stop, since a yes would not make the file preparable."""

    def __init__(self, message: str, *, code: str = "VERIFY_NOT_PREPARED") -> None:
        super().__init__(message)
        self.code = code


_HARD_READINESS: Final[frozenset[str]] = frozenset(
    {"LEAKAGE_SUSPECTED", "ROWS_TOO_FEW", "TARGET_TOO_FEW_POSITIVES", "CONSENT_COLUMN_MISSING"}
)
"""Readiness checks that are rules whatever `ValidationCheck.acknowledgeable` says (the registry lets a run's config
acknowledge some of them): too few rows or positives, leakage, consent. They win over the check's own flag, so
the Doctor never offers a yes to them. Any other `CONSENT_*` code is hard too (:func:`_is_hard_readiness`)."""


def _is_hard_readiness(code: str, acknowledgeable: bool) -> bool:
    return not acknowledgeable or code in _HARD_READINESS or code.startswith("CONSENT_")


def _readiness_findings(report: Any) -> tuple[PlanFinding, ...]:
    """Each requirement of the readiness report that fails, as a whole-file stop for the ledger.

    The run's own checks that a person may acknowledge (`ValidationCheck.acknowledgeable`) are stops a yes lifts;
    every other failure (too few rows or positives, future data in a feature, consent, a requirement of the run, a
    probe) is a hard rule: shown, never acknowledgeable. Names, counts and the checks' own sentences only."""
    from engine.agentic.canonical import canonical_bytes, sha256_hex
    from engine.contracts import Severity

    found: list[PlanFinding] = []
    covered: set[str] = set()

    def add(code: str, column: str | None, text: str, *, hard: bool, detail: str | None = None) -> None:
        subject = code if column is None else f"{code}:{column}"
        found.append(
            PlanFinding(
                key=sha256_hex(canonical_bytes([code, subject]))[:16],
                level="stop",
                code=code,
                text=text,
                detail=detail,
                column=column,
                scope="full",
                hard=hard,
                source="readiness",
            )
        )

    dry = report.dry_run_report
    for check in () if dry is None else dry.checks:
        if check.severity is Severity.ERROR and not check.acknowledged:
            covered.add(check.code)
            add(
                check.code,
                check.column,
                check.message,
                hard=_is_hard_readiness(check.code, check.acknowledgeable),
                detail=check.suggestion or None,
            )
    for requirement in report.requirements:
        code = requirement.engine_code or requirement.id
        if requirement.status == "fail" and requirement.blocking and code not in covered:
            covered.add(code)
            add(
                code, requirement.column, requirement.message or f"The run needs {requirement.id}.", hard=True
            )
    for probe in report.probes:
        if probe.status == "fail":
            add(probe.code, probe.column, probe.message, hard=True, detail=probe.fix or None)
    return tuple(found)


def readiness_verify(
    storage: Storage,
    config_root: Path | None,
    prepare: Callable[[Any, str, Any, str], str | None] | None = None,
    *,
    full_files: Callable[[Case], Sequence[LoadedFile]] | None = None,
    store_frame: Callable[[Any, str, Any], str] | None = None,
) -> VerifyPort:
    """VERIFY with M234's `check_readiness` on the prepared copy of the file holding the goal's outcome.

    The prepared copy is the plan materialised on every row (:func:`engine.doctor.materialize.materialize`, `Full`),
    the one place the plan is worked out. The guards' stops on the whole file come first: one nobody said yes to is
    a not-ready verdict that carries it as a finding, so the visit rests at APPROVE with it. `full_files(case)` reads
    the files with every row (default: the files the visit already holds).

    `prepare(config, upload_id, primary_key, target)` gives the id of the upload a run would read once the approved
    changes are applied to `upload_id` (the same preparation `POST /runs` does), or raises :class:`PrepareError`;
    None means no prescription is bound (or they are off). Then the materialised table itself is stored with
    `store_frame(config, upload_id, frame)` and checked. A plan that changes nothing has the upload as its table.
    The raw upload is never checked in place of a prepared table that could not be made: that is a named stop.
    """

    def verify(case: Case, brief: GoalBrief | None, files: tuple[LoadedFile, ...]) -> VerifyResult:
        from engine.config import RunMode, load_use_case
        from engine.doctor.combine_run import current_upload
        from engine.doctor.readiness import check_readiness, inputs_for_upload, store_readiness

        if brief is None:
            return VerifyResult(verdict="no_goal", reason="No goal is chosen.")
        outcome = decisions(case).pair("outcome", brief.goal_id)
        if outcome is None:
            return VerifyResult(
                verdict="not_ready", reason="VISIT_NO_OUTCOME: no file has the goal's outcome yet."
            )
        if _outcome_is_gone(case, files, outcome):
            return VerifyResult(
                verdict="not_ready",
                reason=f"Not ready: the outcome “{outcome[1]}” is in no file and in no table the plan makes.",
            )
        # An approved join or stack gave its files' place to the table it made: that table is what a run reads.
        upload_id, column = current_upload(case, outcome[0]), outcome[1]
        key = decisions(case).key(upload_id)
        if key is None:
            diagnosed = next((f for f in case.files if f.upload_id == upload_id), None)
            key = diagnosed.key_columns[0] if diagnosed is not None and diagnosed.key_columns else None
        from engine.doctor.prescription import prepared_key

        key = prepared_key(case.items, column, key)
        config = load_use_case(brief.goal_id, config_root)
        visit = case.visit
        items, _ = active_section(case)
        prepared = materialize(
            case, items, full_files(case) if full_files is not None else files, Full(), twice=False
        )  # repeatability was checked at approval
        said = () if visit is None else visit.acknowledged
        found = prepared.findings
        stops = [f for f in found if stop_is_open(f, said)]
        if stops:
            return VerifyResult(
                verdict="not_ready",
                reason=f"Not ready: {' '.join(f.text for f in stops[:3])}",
                findings=found,
            )
        made: str | None = None
        if prepare is not None:
            try:
                made = prepare(config, upload_id, key, column)
            except PrepareError as exc:
                hard = stop_finding(exc.code, str(exc), Full())
                return VerifyResult(verdict="not_ready", reason=f"Not ready: {exc}", findings=(*found, hard))
        if made is None:  # nothing is bound to a prescription: the plan's own table is what is checked
            table = prepared.by_upload.get(upload_id)
            changed = any(done.upload_id == upload_id for done in prepared.files)
            if upload_id in prepared.failed:  # its steps could not run: never the raw upload in their place
                stuck = stop_finding(
                    "VERIFY_NOT_PREPARED",
                    "A step of the plan could not run on the file, so the file it makes was not checked. "
                    "Change the plan.",
                    Full(),
                )
                return VerifyResult(
                    verdict="not_ready", reason=f"Not ready: {stuck.text}", findings=(*found, stuck)
                )
            if not changed:
                made = upload_id  # no step touches this file: its table is the upload as it is
            elif store_frame is None or table is None:
                none = stop_finding(
                    "VERIFY_NOT_PREPARED",
                    "The file the plan makes could not be put together to check it, so it was not checked. "
                    "Try again, or change the plan.",
                    Full(),
                )
                return VerifyResult(
                    verdict="not_ready", reason=f"Not ready: {none.text}", findings=(*found, none)
                )
            else:
                made = store_frame(config, upload_id, prepared.tables[table])
        upload_id = made
        entries = readiness_acknowledged(visit)
        if entries:  # what a person said yes to at APPROVE is acknowledged for the run's own checks
            config = config.model_copy(
                update={
                    "validation": config.validation.model_copy(
                        update={
                            "acknowledged": (
                                *config.validation.acknowledged,
                                *(e for e in entries if e not in config.validation.acknowledged),
                            )
                        }
                    )
                }
            )
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
        ready = report.verdict in {"ready", "ready_with_warnings"}
        return VerifyResult(
            verdict=report.verdict,
            reason=report.verdict_reason,
            ref=ref,
            findings=found if ready else (*found, *_readiness_findings(report)),
        )

    return verify


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


def _review(run: _Run, case: Case, brief: GoalBrief | None, step: _Step) -> bool:
    """The AI review (`deps.agent`), after the rules, the detectors and the standing answers, and only when
    nothing blocking is open or has just been asked. Its questions go through the same protocol; its items land
    pending, `check`, whatever the port returns. True when it made a call that was answered, so TREAT remembers it:
    a review whose first call the AI service did not answer (an expired key, a model that ran out of room) did not
    happen, and is tried again the next time TREAT runs. What it spent still counts, and the chat and the timeline
    say why it did not happen."""
    agent = run.deps.agent
    if agent is None or open_blocking(case):
        return False
    if any(q.blocking and q.answer is None and _in_section(q.goal_id, case) for q, _e in step.questions):
        return False
    found = agent(case, run.files(case), brief)
    step.evidence.extend(found.evidence)
    step.items.extend(
        item.model_copy(
            update={
                "source": ProposalSource.AI,
                "state": ProposalState.PENDING,
                "confidence": AgentConfidence.CHECK,
            }
        )
        for item in found.items
    )
    step.model_calls += found.model_calls
    step.tokens += found.tokens
    step.doubts.extend(found.doubts)  # grounded when the review made them (`express_doubt`)
    for request in found.requests:
        _add_request(step, request)
    _settle_asks(run, case, step, found.asks)
    if found.blocked_by:
        last = case.chat[-1] if case.chat else None
        if last is None or last.role != "agent" or last.error_codes != found.error_codes:
            # Said once: a chat turn that just failed the same way (the review follows it when the person has only
            # now said yes to the AI) has told the person already, and the timeline still records the review.
            entry = CaseChatEntry(
                role="agent",
                text=found.reply,
                at=run.deps.clock(),
                blocked_by=found.blocked_by,
                error_codes=found.error_codes,
            )
            step.updates["chat"] = (*case.chat, entry)
        event = EventDraft(
            kind=TimelineKind.CHAT,
            details={"role": "agent", "ai": True, "proposals": 0, "review": True},
        )
        _say_why(event, found.blocked_by, found.error_codes)
        step.events.append(event)
        return False
    if found.model_calls and found.summary:
        entry = CaseChatEntry(role="agent", text=found.summary, at=run.deps.clock(), sent=found.sent)
        step.updates["chat"] = (*case.chat, entry)
        step.events.append(
            EventDraft(
                kind=TimelineKind.CHAT,
                details={"role": "agent", "ai": True, "proposals": len(found.items), "review": True},
            )
        )
    return found.model_calls > 0


def _settle_asks(run: _Run, case: Case, step: _Step, asks: Sequence[Ask], *, counted: bool = True) -> None:
    """AUTO, a stated assumption or one question per ambiguity, within the file's question budget (§6.3).

    `counted` False: the questions are the person's own conversation (the chat's), which the budget does not cut
    short; they still count towards the file's total."""
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
        settled = settle(ask, now=now, settings=settings, over_budget=counted and asked.get(key, 0) >= budget)
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
    value: str | None = None,
) -> Case:
    """Answer a question with one of its options; the option's proposal is added, ticked by the person.

    `value` is what the person typed with the answer (a date for "I will type a date"): an option that asks for
    one needs it, in its kind; any other option refuses one (`VISIT_ANSWER_VALUE_INVALID`).
    """
    run = _Run(deps, actor)
    now = deps.clock()

    def build(case: Case) -> _Step:
        question = next((q for q in case.questions if q.question_id == question_id), None)
        if question is None or not _in_section(question.goal_id, case):
            raise _unknown("There is no such question open in this case.")
        chosen = next((o for o in question.options if o.option_id == option_id), None)
        if chosen is None:
            raise _unknown("That is not one of the question's answers.")
        typed = _typed_value(chosen.value_kind, value)
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
                "answer_value": typed,
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
                    **({} if typed is None else {"value": typed}),
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
    if question.answer_value is not None:
        return None  # a typed value belongs to this case alone: remembering the option would drop it
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
    park_for: str | None = None,
) -> Case:
    """Tick or untick an item of the active section; the visit goes back to TREAT and previews again.

    `park_for` (one of `PARK_ROLES`) leaves the item out and notes who would know ("waiting for the CRM admin");
    any later tick or untick resumes it."""
    run = _Run(deps, actor)
    now = deps.clock()

    def build(case: Case) -> _Step:
        item = next((i for i in case.items if i.item_id == item_id), None)
        if item is None or not _in_section(item.goal_id, case):
            raise _unknown("There is no such suggestion in this case (or its goal is set aside).")
        if park_for is not None and (accept or park_for not in PARK_ROLES):
            raise _unknown("A step can be parked only unticked, for one of the people who would know.")
        state = ProposalState.ACCEPTED if accept else ProposalState.REJECTED
        decided = item.model_copy(
            update={"state": state, "decided_by": actor, "decided_at": now, "parked_for": park_for}
        )
        return _Step(
            to=VisitState.TREAT if case.state in _AFTER_RECALL else case.state,
            updates={"items": [decided if i.item_id == item_id else i for i in case.items]},
            events=[
                EventDraft(
                    kind=TimelineKind.ITEM_DECIDED,
                    upload_id=item.upload_id,
                    item_id=item_id,
                    details={"state": state.value, **({"parked_for": park_for} if park_for else {})},
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
    run = _Run(deps, actor)

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
        return _approval(run, case, actor=actor, sentence=sentence)

    return _person_step(run, client_id, case_id, expected_version, op_id, build)


CARRIED_ON: Final[str] = "Carried on from your earlier approval:"
"""What the sentence of an approval the visit carried on by itself starts with (`engine.doctor.carry`)."""
CARRIED_ON_LAST_MONTH: Final[str] = "Carried on from last month's approval:"
"""The same, when the approval is another case's (last month's)."""


def _approval(
    run: _Run,
    case: Case,
    *,
    actor: str,
    sentence: str | None = None,
    carried_from: str | None = None,
    carried_new: int = 0,
) -> _Step:
    """The step that records an approval of the plan as it stands (:func:`approve`'s, and the carry-on's with
    `carried_from`, the approval it goes on from, and `carried_new`, the number of new sure fixes it carries; it
    is recorded in the name of whoever approved before, and says in its record that it was carried)."""
    from engine.doctor.patterns import record_approved

    deps = run.deps
    now = deps.clock()
    items, questions = active_section(case)
    visit = case.visit or CaseVisit()
    check, said = visit.plan_check, visit.acknowledged
    if check is None or check.basis is None or check.digest != plan_digest(case.files, items):
        # never another plan's counts, nor a count of a sample only (a case kept before the whole file was counted)
        said = said if check is not None and check.digest == plan_digest(case.files, items) else ()
        check = _counted(run, case, items)
    stops = open_stops(check, said)
    if stops:
        raise CaseError(
            "APPROVAL_STOP_OPEN",
            f"{len(stops)} safety check{'s' if len(stops) != 1 else ''} stop{'' if len(stops) != 1 else 's'} "
            f"this plan: {stops[0].text} Say yes to each one, or change the plan.",
        )
    shown = approval_sentence(case, goal_name=approval_goal_name(deps.storage, case))
    if sentence is not None and sentence != shown:
        raise CaseError(
            "APPROVAL_SENTENCE_CHANGED",
            f"What would be approved has changed since you read it. It now reads: “{shown}”",
        )
    lead = CARRIED_ON_LAST_MONTH if carried_from is not None and "@" in carried_from else CARRIED_ON
    text = shown if carried_from is None else f"{lead} {shown}"
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
        carried_from=carried_from,
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
        record_approved(deps.storage, ai_steps, place=written.case_id, client_id=written.client_id, at=now)

    if ai_steps:  # [REQ] 11: an approved AI pattern counts towards a candidate rule for the product team
        after.append(count_patterns)
    return _Step(
        to=VisitState.PRESCRIBE,
        visit={
            "plan_check": check,
            "acknowledged": said,
            "approved_before": tuple(
                dict.fromkeys((*visit.approved_before, *approved_marks(items, questions)))
            ),
            # last month's approval starts the case; only a person's own approval makes it theirs. A carried one keeps the
            # source, so the joined table (round 2) is still judged against last month's before it carries on
            "carry_source": visit.carry_source if carried_from is not None else None,
        },
        within=_recorded(deps, DATA_PRESCRIPTION_APPROVED, actor, body, approval),
        carried=carried_from is not None,
        events=[
            EventDraft(
                kind=TimelineKind.APPROVAL,
                item_id=approval.approval_id,
                details={
                    "items": len(approval.item_ids),
                    "goal": goal.goal_id if goal is not None else None,
                    "receipt": "ledger" if deps.ledger is not None else "timeline",
                    **(
                        {}
                        if carried_from is None
                        else {"carried": True, "new": carried_new, "last_month": "@" in carried_from}
                    ),
                    **_timeline_body(body),
                },
            )
        ],
        after=after,
    )


def acknowledge_stop(
    client_id: str,
    case_id: str,
    key: str,
    deps: VisitDeps,
    *,
    expected_version: int,
    actor: str,
    reason: str = "",
    op_id: str | None = None,
) -> Case:
    """The person's explicit yes to one stop of the plan, by its key (`view.ledger.findings[].key`), with a reason.

    Only while the visit waits for approval, only for a stop of the plan as it stands (found on a sample or on the
    whole file), and never for a hard one. The yes records the count it was given at and holds for that plan alone,
    while a new count of the stop is at or below it: a bigger loss needs a new yes, and any change to the plan
    counts the checks again and the yes is gone (:func:`_preview`)."""
    run = _Run(deps, actor)
    now = deps.clock()

    def build(case: Case) -> _Step:
        if case.state is not VisitState.APPROVE:
            raise _not_waiting("There is nothing waiting for approval now.")
        items, _ = active_section(case)
        visit = case.visit or CaseVisit()
        check = visit.plan_check
        if check is None or check.digest != plan_digest(case.files, items):
            raise _unknown("The plan changed since its checks were counted. Look at the checks again.")
        found = next((f for f in check.findings if f.key == key and f.level == "stop"), None)
        if found is None:
            raise _unknown("There is no such stop in the plan now.")
        if found.hard:
            raise CaseError(
                "STOP_NOT_ACKNOWLEDGEABLE",
                "No one can say yes to this one: it is a rule the data must meet. Change the plan or the file.",
            )
        said = Acknowledgement(key=key, count=found.count, by=actor, at=now, reason=reason.strip()[:500])
        return _Step(
            to=case.state,
            visit={"acknowledged": (*(a for a in visit.acknowledged if a.key != key), said)},
            events=[
                EventDraft(
                    kind=TimelineKind.STOP_ACKNOWLEDGED,
                    details={"finding": key, "reason": said.reason},
                )
            ],
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
    asked = next(  # a change the agent made in answer to a message was asked for by whoever wrote it
        (
            case.chat[n - 1].by
            for n, entry in enumerate(case.chat)
            if n
            and entry.role == "agent"
            and item.item_id in entry.item_ids
            and case.chat[n - 1].role == "user"
        ),
        None,
    )
    for who in (origin.answered_by if origin is not None else None, asked, item.decided_by):
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
            if item.state is ProposalState.ACCEPTED and item.decided_by != AGENT
            else item.model_copy(  # a change the agent applied becomes the person's own with their yes
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


def _typed_value(kind: str | None, value: str | None) -> str | None:
    """The value typed with an answer, checked against what its option asks for (None when it asks for none)."""
    given = None if value is None else value.strip()
    if kind is None:
        if given:
            raise CaseError("VISIT_ANSWER_VALUE_INVALID", "This answer does not take a typed value.")
        return None
    if not given:
        raise CaseError("VISIT_ANSWER_VALUE_INVALID", f"This answer needs {_KIND_WORDS[kind]}.")
    if kind == "date":
        try:
            parsed = date.fromisoformat(given)
        except ValueError:
            parsed = None
        if parsed is None or parsed.isoformat() != given:
            raise CaseError(
                "VISIT_ANSWER_VALUE_INVALID", "Type the date as year-month-day, for example 2011-11-30."
            )
    elif kind == "number":
        try:
            if not math.isfinite(float(given)):
                raise ValueError(given)
        except ValueError:
            raise CaseError("VISIT_ANSWER_VALUE_INVALID", "Type a number, for example 12.5.") from None
    return given


_KIND_WORDS: Final[dict[str, str]] = {"date": "a date", "number": "a number", "text": "some text"}


def _unknown(message: str) -> CaseError:
    return CaseError("VISIT_UNKNOWN_CHOICE", message)


def _not_waiting(message: str) -> CaseError:
    return CaseError("VISIT_NOT_WAITING", message)
