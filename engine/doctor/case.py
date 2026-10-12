"""The case (Plan O M231, DEC-1801): one persistent object per piece of Data Doctor work.

A **case** replaces three things that today do not know about each other: the one-upload Guided-setup
session (`uploads/<id>/agent/agent_session.json`), the stateless Doctor diagnosis (DEC-1513) and the
in-browser hand-off between them (the design §4.1). It holds the files (by `upload_id`, never their
cells), what was found, the items proposed, the questions, doubts and approvals with who decided, the
standing answers used, the masked chat, the evidence, the prescriptions and readiness reports by hash,
the counters, and an append-only **timeline**.

**Storage** (the design §4.2)::

    cases/<client_id>/<case_id>/case.json                       the case, written atomically
    cases/<client_id>/<case_id>/timeline.jsonl                  append-only processing log
    cases/<client_id>/<case_id>/evidence/<evidence_id>.json     tool results, with the upload they read
    cases/<client_id>/<case_id>/prescriptions/<hash>.json       (M243)
    cases/<client_id>/<case_id>/readiness/<goal_id>_<hash>.json (M234)
    cases/<client_id>/<case_id>/case.lock                       present only while a write is in progress

**One writer at a time, across processes.** Every change names the version it was made against. The
store takes the case's lock file by *exclusive create* (`O_CREAT | O_EXCL` on a local store, a
storage's own `write_bytes_if_absent` where it has one), re-reads the case, and refuses with
`CASE_VERSION_CONFLICT` when the version moved. So of two writers that read version 3, the first
writes version 4 and the second is refused rather than silently overwriting it. This replaces the
per-process thread lock of `docs/AGENTS.md` §11 for Data Doctor's own state. A lock left by a crashed
process is broken after `lock_stale_seconds`. A store with neither exclusive create (an S3 store before
it gains a conditional put) falls back to a process-wide lock plus an exists check: correct within one
API process only. (`cases/` is not yet among the AWS task role's S3 prefixes either; see
`docs/DATA_DOCTOR.md`.)

**Crash consistency.** `case.json` carries the events of its own version (`last_events`). The timeline
is appended after the case is written; the next locked write (a retention or erasure rewrite included),
and every timeline read, replays any of those events the timeline does not hold yet. A crash between the
two writes therefore loses no event. On a local store the timeline is appended in place and only its tail
is read to replay, so a write costs its own events, not the case's history.

**Never unreadable.** A stored `case.json` that fails validation answers `CASE_UNREADABLE`, never an
unhandled error; and a privacy rewrite never turns a valid case into an invalid one (it refuses instead,
and erasure removes whole the list entries that name the person rather than masking an id field).

**Per-client scope** (the design §4.3). Every case lives under its `client_id`; a lookup under another
client's id finds nothing (`CASE_NOT_FOUND`), never a refusal that would say the case exists. A
single-tenant deployment uses the client `default`.

**Retention and erasure** (the design §4.4) are `engine.privacy`'s: `cases` is a store in
`engine.privacy.layout`. When an upload leaves (retention), `forget_upload` removes its file entry, the
items, questions and doubts about it, and its evidence, and marks prescriptions that read it
`orphaned`; the processing log (`case.json` without that data, the timeline, prescriptions) is kept for
`privacy.processing_log_days` (default 365, DPDP Rule 8(3)). An erasure request for a person removes
`correct_cells` corrections and standing answers holding their id (`remove_principal`), then masks any
other occurrence like any JSON artefact.

**Adapters, read-only.** `case_from_session` and `case_from_diagnosis` open an existing Guided-setup
session or a Doctor diagnosis as a case, without writing anything: `read_only` is true and every
change is refused with `CASE_READ_ONLY`. They let the Data Doctor screen (M247) show old work in one
shape until the switch-over (M249).

Nothing here is reachable while `data_doctor.unified` is off: the routes are not mounted
(`api/routes/cases.py`), so no case is ever written and every existing output is byte-identical.
Nothing here logs a value or a column name: counts, ids and codes only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, JsonValue, ValidationError, model_validator

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.config import ConfigError, StrictBase, load_engine_config
from engine.contracts import Artefact
from engine.doctor.visit_contracts import CaseVisit
from engine.storage import LocalStorage, Storage, StorageError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.agent.contracts import AgentSession

__all__ = [
    "CASE_CODES",
    "CASE_FILENAME",
    "CASE_ID_PATTERN",
    "CASE_PREFIX",
    "CLIENT_ID_PATTERN",
    "DEFAULT_CLIENT_ID",
    "LOCK_FILENAME",
    "TIMELINE_FILENAME",
    "Case",
    "CaseApproval",
    "CaseChatEntry",
    "CaseCounters",
    "CaseDoubt",
    "CaseError",
    "CaseEvidenceRef",
    "CaseFile",
    "CaseGoal",
    "CaseItem",
    "CaseOption",
    "CasePrescriptionRef",
    "CaseQuestion",
    "CaseReadinessRef",
    "CaseSource",
    "CaseStore",
    "EventDraft",
    "StandingAnswerUse",
    "TimelineEvent",
    "TimelineKind",
    "VisitState",
    "case_dir",
    "case_from_diagnosis",
    "case_from_session",
    "case_key",
    "data_doctor_unified",
    "evidence_key",
    "forget_upload",
    "is_valid_case",
    "new_case_id",
    "payload_digest",
    "remove_principal",
    "rewrite_case_document",
    "timeline_key",
]

_LOG = get_logger(__name__)

CASE_PREFIX: Final[str] = "cases/"
"""Where cases live in the artefact store; `engine.privacy.layout` knows it as the `cases` store."""
CASE_FILENAME: Final[str] = "case.json"
TIMELINE_FILENAME: Final[str] = "timeline.jsonl"
LOCK_FILENAME: Final[str] = "case.lock"
EVIDENCE_DIRNAME: Final[str] = "evidence"
DEFAULT_CLIENT_ID: Final[str] = "default"
"""The client of a single-tenant deployment (the design §4.3)."""

CASE_CODES: Final[frozenset[str]] = frozenset(
    {"CASE_NOT_FOUND", "CASE_VERSION_CONFLICT", "CASE_READ_ONLY", "CASE_UNREADABLE"}
)
"""The codes this module raises; the integrator adds them to `PLAN_O_CODES` with their help entries."""

CLIENT_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_-]{0,63}$"
"""A client id is a path segment: lower-case letters, digits, `_` and `-` (onboarding's `c_<slug>_<n>` fits)."""
CASE_ID_PATTERN: Final[str] = r"^(case_[a-f0-9]{16}|guided_[A-Za-z0-9_-]{1,64}|doctor_[a-f0-9]{16})$"
"""`case_<16 hex>` for a stored case; `guided_<upload id>` and `doctor_<16 hex>` for the read-only adapters."""
_ITEM_ID: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"

LOCK_WAIT_SECONDS: Final[float] = 5.0
LOCK_STALE_SECONDS: Final[float] = 30.0
_POLL_SECONDS: Final[float] = 0.02


class CaseError(Exception):
    """A refusal with a stable code: `CASE_NOT_FOUND` (404), `CASE_VERSION_CONFLICT`, `CASE_READ_ONLY` and
    `CASE_UNREADABLE` (409)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------
def data_doctor_unified(config_root: Path | None = None) -> bool:
    """Whether `data_doctor.unified` is on (`configs/engine.yaml` defaults; Plan O M230, default false).

    Read leniently: a configuration that does not name the switch, or cannot be read, is off.
    """
    try:
        defaults = load_engine_config(config_root).defaults
    except ConfigError:
        return False
    section = defaults.get("data_doctor")
    return isinstance(section, Mapping) and section.get("unified") is True


# ---------------------------------------------------------------------------
# Contracts (the design §4.1)
# ---------------------------------------------------------------------------
class VisitState(StrEnum):
    """Where the visit is (the design §3.4). M235 drives the transitions; a new case starts at `intake`."""

    INTAKE = "intake"
    UNDERSTAND = "understand"
    RECALL = "recall"
    GOAL_BRIEF = "goal_brief"
    TREAT = "treat"
    ASK = "ask"
    CONVERSE = "converse"
    PREVIEW = "preview"
    APPROVE = "approve"
    PRESCRIBE = "prescribe"
    VERIFY = "verify"
    HAND_OFF = "hand_off"
    FOLLOW_UP = "follow_up"


class CaseSource(StrEnum):
    """Where a case came from: Data Doctor itself, or a read-only adapter over older work."""

    CASE = "case"
    GUIDED_SESSION = "guided_session"
    DOCTOR_DIAGNOSIS = "doctor_diagnosis"


class TimelineKind(StrEnum):
    """What a timeline event records. Later milestones add members; none is ever renamed."""

    CASE_CREATED = "case.created"
    FILE_ADDED = "file.added"
    FILE_FORGOTTEN = "file.forgotten"
    EVIDENCE_ADDED = "evidence.added"
    STATE_CHANGED = "state.changed"
    ITEM_PROPOSED = "item.proposed"
    ITEM_DECIDED = "item.decided"
    QUESTION_ASKED = "question.asked"
    QUESTION_ANSWERED = "question.answered"
    APPROVAL = "approval"
    PREVIEW = "preview"
    HAND_OFF = "hand_off"
    REPLAY = "replay"
    ERASURE = "privacy.erasure"
    # Plan O M235 (DEC-1805): the visit's own events.
    GOAL_SET = "goal.set"
    CHAT = "chat.message"
    PRESCRIPTION = "prescription.saved"
    READINESS = "readiness.checked"


DetailValue = str | int | float | bool | None


class TimelineEvent(StrictBase):
    """One line of `timeline.jsonl`: what happened, when, by whom. Ids and counts only, never a cell value."""

    event_id: Annotated[str, Field(pattern=r"^ev_[a-f0-9]{20}$", description="Unique id of the event.")]
    case_id: str = Field(description="The case it belongs to.")
    client_id: str = Field(description="The case's client.")
    version: Annotated[int, Field(ge=1, description="The case version this event produced.")]
    at: AwareDatetime = Field(description="When it happened, UTC.")
    actor: str = Field(description="The user id that did it, or `system:<job>` for a job.")
    kind: TimelineKind = Field(description="What happened.")
    upload_id: str | None = Field(default=None, description="The file it was about, if one.")
    item_id: str | None = Field(
        default=None, description="The item, question or approval it was about, if one."
    )
    details: dict[str, DetailValue] = Field(
        default_factory=dict, description="Counts, codes and ids; never a cell value or a column's contents."
    )


class CaseFile(StrictBase):
    """One file of the case, by reference: the case never copies a cell (the design §4.2)."""

    upload_id: Annotated[str, Field(min_length=1, max_length=64, description="The upload the file is.")]
    file_name: str = Field(default="", description="The file's name as it arrived.")
    sheet: str | None = Field(default=None, description="The workbook sheet read, if any.")
    intake_key: str | None = Field(
        default=None, description="Storage key of the upload's `intake.json`, if any."
    )
    original_upload_id: str | None = Field(
        default=None, description="The upload this one was handed off or derived from, if any."
    )
    role: str | None = Field(default=None, description="What the file is (an onboarding role), once known.")
    key_columns: tuple[str, ...] = Field(default=(), description="The columns that name each row's customer.")
    fingerprint: str | None = Field(default=None, description="The upload's dataset digest.")
    connection_ref: str | None = Field(
        default=None, description="The connection it was imported from, if any."
    )
    added_at: AwareDatetime = Field(description="When it joined the case.")
    added_by: str = Field(description="Who added it (user id).")


class CaseGoal(StrictBase):
    """A goal brief on the case, by reference (M235 defines `GoalBrief`)."""

    goal_id: str = Field(description="The use case the brief comes from.")
    brief_hash: str | None = Field(default=None, description="`brief_hash` of the brief (M235).")
    active: bool = Field(default=False, description="Exactly one goal is active when any is.")
    set_at: AwareDatetime = Field(description="When it was set.")
    set_by: str = Field(description="Who set it (user id).")


class CaseItem(StrictBase):
    """Every proposal: a step, a setting, a role, a mapping, a build, an outcome definition."""

    item_id: Annotated[str, Field(pattern=_ITEM_ID, description="Stable within the case.")]
    kind: str = Field(description="What it would change (`recipe_step`, `setting`, `role`, ...).")
    title: str = Field(default="", description="What, in one plain sentence.")
    source: ProposalSource = Field(description="`rules`, `ai` or `person`.")
    confidence: AgentConfidence = Field(description="`sure` or `check`.")
    state: ProposalState = Field(default=ProposalState.PENDING, description="Only a person changes it.")
    upload_id: str | None = Field(default=None, description="The file it is about; null for the whole case.")
    evidence_ids: tuple[str, ...] = Field(default=(), description="Evidence it rests on.")
    payload: dict[str, JsonValue] = Field(default_factory=dict, description="What it would do, as data.")
    payload_digest: str = Field(description="sha256 of `payload` (`payload_digest`).")
    proposed_at: AwareDatetime = Field(description="When it was proposed.")
    decided_by: str | None = Field(default=None, description="Who ticked or unticked it (user id).")
    decided_at: AwareDatetime | None = Field(default=None, description="When.")
    goal_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Plan O M235: the goal section it belongs to; null for the shared base.",
    )


class CaseOption(StrictBase):
    """One answer a question offers; it maps to a checkable proposal or to leaving things as they are."""

    option_id: str = Field(description="Stable within the question.")
    label: str = Field(description="Button text.")
    effect_digest: str | None = Field(default=None, description="The proposal it adds; null = leave as is.")


class CaseQuestion(StrictBase):
    """A question, open or answered, with who answered (the design §6.5)."""

    question_id: Annotated[str, Field(pattern=_ITEM_ID, description="Stable within the case.")]
    code: str = Field(default="", description="The detector or rule that asked it.")
    upload_id: str | None = Field(default=None, description="The file it is about, if one.")
    column: str | None = Field(default=None, description="The column it is about, if one.")
    text: str = Field(description="The question in plain words.")
    options: tuple[CaseOption, ...] = Field(default=(), description="The answers offered.")
    blocking: bool = Field(default=False, description="Blocking questions stop the preview.")
    recommended: str | None = Field(default=None, description="An option id, or none.")
    answer: str | None = Field(default=None, description="The chosen option id.")
    answered_by: str | None = Field(default=None, description="Who answered (user id).")
    answered_at: AwareDatetime | None = Field(default=None, description="When.")
    goal_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Plan O M235: the goal section it belongs to; null for the shared base.",
    )


class CaseDoubt(StrictBase):
    """How the agent says it is unsure, in three parts (the design §6.7)."""

    doubt_id: Annotated[str, Field(pattern=_ITEM_ID, description="Stable within the case.")]
    because: str = Field(description="I am not sure because ...")
    would_do: str = Field(description="Here is what I would do ...")
    if_not: str = Field(description="Here is what changes if not ...")
    upload_id: str | None = Field(default=None, description="The file it is about, if one.")
    item_id: str | None = Field(default=None, description="The item it is attached to, if one.")
    evidence_ids: tuple[str, ...] = Field(default=(), description="Evidence every number in it rests on.")


class CaseApproval(StrictBase):
    """An approval, with who, when and the exact text they read (the design §6.6)."""

    approval_id: Annotated[str, Field(pattern=_ITEM_ID, description="Stable within the case.")]
    item_ids: tuple[str, ...] = Field(default=(), description="The items approved.")
    approved_by: str = Field(description="Who approved (user id).")
    approved_at: AwareDatetime = Field(description="When.")
    text_shown: str = Field(default="", description="The sentence the person read, word for word.")
    approve_class: bool = Field(default=False, description="An APPROVE-class item's own confirmation.")
    receipt_id: str | None = Field(default=None, description="The receipt issued, if any.")


class StandingAnswerUse(StrictBase):
    """A standing answer applied in this case (the design §7.2), each revocable."""

    answer_id: str = Field(description="The standing answer's id.")
    detector: str = Field(description="The detector it answers.")
    upload_id: str | None = Field(default=None, description="The file it was applied to, if one.")
    column: str | None = Field(default=None, description="The column it was applied to.")
    value_signature: tuple[str, ...] = Field(default=(), description="The values it covers.")
    option_id: str = Field(description="The option it chose.")
    effect_digest: str | None = Field(default=None, description="The proposal it applied.")
    applied_at: AwareDatetime = Field(description="When it was applied here.")
    revoked_at: AwareDatetime | None = Field(default=None, description="When it was revoked, if it was.")


class CaseChatEntry(StrictBase):
    """One chat message, masked as it was stored, with what was sent to the AI service."""

    role: Literal["user", "agent"] = Field(description="Who wrote it.")
    text: str = Field(description="The message, as stored (masked).")
    at: AwareDatetime = Field(description="When it was written.")
    by: str | None = Field(default=None, description="The user id, for a person's message.")
    sent: tuple[dict[str, JsonValue], ...] = Field(
        default=(), description="What the AI looked at (`docs/AGENTS.md` §7.7)."
    )


class CaseEvidenceRef(StrictBase):
    """One stored tool result: `evidence/<evidence_id>.json`, with the upload it read."""

    evidence_id: Annotated[str, Field(pattern=_ITEM_ID, description="Content-hash id (M232).")]
    tool: str = Field(description="The tool that produced it.")
    upload_id: str | None = Field(default=None, description="The upload it read; null for none.")
    created_at: AwareDatetime = Field(description="When it was produced.")


class CasePrescriptionRef(StrictBase):
    """A prescription by hash (M243), and whether an upload it read has since gone."""

    prescription_hash: str = Field(description="sha256 of the prescription.")
    created_at: AwareDatetime = Field(description="When it was saved.")
    approved_by: str | None = Field(default=None, description="Who approved it (user id).")
    upload_ids: tuple[str, ...] = Field(default=(), description="The uploads it read.")
    orphaned: bool = Field(default=False, description="True once an upload it read is gone.")


class CaseReadinessRef(StrictBase):
    """The latest readiness report of a goal (M234), by hash."""

    goal_id: str = Field(description="The goal it was computed for.")
    report_hash: str = Field(description="sha256 of the report.")
    verdict: str = Field(
        description="`ready`, `ready_with_warnings`, `needs_answers`, `needs_approval`, `not_ready`."
    )
    created_at: AwareDatetime = Field(description="When it was computed.")


class CaseCounters(StrictBase):
    """What the case cost and how much it asked."""

    model_calls: int = Field(default=0, ge=0, description="AI service calls.")
    tool_calls: int = Field(default=0, ge=0, description="Tool calls.")
    cost_usd: float = Field(default=0.0, ge=0.0, description="Estimated AI cost.")
    questions_per_file: dict[str, int] = Field(
        default_factory=dict, description="Upload id -> questions asked."
    )
    expected_training_minutes: float | None = Field(default=None, ge=0.0, description="Promised (M244).")
    actual_training_minutes: float | None = Field(default=None, ge=0.0, description="Measured (M244).")


class Case(Artefact):
    """`cases/<client_id>/<case_id>/case.json`: one piece of Data Doctor work."""

    case_id: Annotated[str, Field(pattern=CASE_ID_PATTERN, description="The case's id.")]
    client_id: Annotated[str, Field(pattern=CLIENT_ID_PATTERN, description="Whose case it is.")]
    created_by: str = Field(description="Who opened it (user id).")
    created_at: AwareDatetime = Field(description="When it was opened.")
    updated_at: AwareDatetime = Field(description="When it last changed.")
    state: VisitState = Field(default=VisitState.INTAKE, description="Where the visit is.")
    version: Annotated[int, Field(ge=1, description="Bumped by every change; a stale writer is refused.")]
    title: str = Field(default="", max_length=200, description="What the person called it.")
    source: CaseSource = Field(default=CaseSource.CASE, description="Data Doctor's own case, or an adapter.")
    read_only: bool = Field(default=False, description="True for an adapter's view: nothing can change it.")
    files: tuple[CaseFile, ...] = Field(default=(), description="The files, in the order they were added.")
    diagnosis: dict[str, JsonValue] | None = Field(
        default=None, description="What the files are and what they can power (`Diagnosis`), when known."
    )
    locale_profile: dict[str, JsonValue] | None = Field(
        default=None, description="The locale profile (M241)."
    )
    goals: tuple[CaseGoal, ...] = Field(default=(), description="Goal briefs, at most one active.")
    items: tuple[CaseItem, ...] = Field(default=(), description="Every proposal.")
    questions: tuple[CaseQuestion, ...] = Field(default=(), description="Open and answered questions.")
    doubts: tuple[CaseDoubt, ...] = Field(default=(), description="Doubts raised.")
    approvals: tuple[CaseApproval, ...] = Field(default=(), description="Approvals, with user ids.")
    standing_answers_used: tuple[StandingAnswerUse, ...] = Field(
        default=(), description="Standing answers applied here, each revocable."
    )
    chat: tuple[CaseChatEntry, ...] = Field(default=(), description="The masked chat, oldest first.")
    evidence: tuple[CaseEvidenceRef, ...] = Field(default=(), description="Stored tool results.")
    prescriptions: tuple[CasePrescriptionRef, ...] = Field(
        default=(), description="Prescriptions, newest last."
    )
    readiness: tuple[CaseReadinessRef, ...] = Field(default=(), description="The latest report per goal.")
    counters: CaseCounters = Field(default_factory=CaseCounters, description="Costs and counts.")
    last_events: tuple[TimelineEvent, ...] = Field(
        default=(),
        description="The timeline events this version wrote; replayed into the timeline after a crash.",
    )
    visit: CaseVisit | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Plan O M235: where the visit stands beyond its state (`engine.doctor.visit`).",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for name, ids in (
            ("file", [f.upload_id for f in self.files]),
            ("item", [i.item_id for i in self.items]),
            ("question", [q.question_id for q in self.questions]),
            ("doubt", [d.doubt_id for d in self.doubts]),
            ("approval", [a.approval_id for a in self.approvals]),
            ("evidence", [e.evidence_id for e in self.evidence]),
            ("goal", [g.goal_id for g in self.goals]),
        ):
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} ids repeat")
        if sum(goal.active for goal in self.goals) > 1:
            raise ValueError("at most one goal is active")
        if self.read_only and self.source is CaseSource.CASE:
            raise ValueError("only an adapter's case is read-only")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at is before created_at")
        return self

    def upload_ids(self) -> tuple[str, ...]:
        """The uploads of the case, in order."""
        return tuple(file.upload_id for file in self.files)


# ---------------------------------------------------------------------------
# Keys and ids
# ---------------------------------------------------------------------------
def case_dir(client_id: str, case_id: str) -> str:
    """`cases/<client_id>/<case_id>/`."""
    return f"{CASE_PREFIX}{client_id}/{case_id}/"


def case_key(client_id: str, case_id: str) -> str:
    return case_dir(client_id, case_id) + CASE_FILENAME


def timeline_key(client_id: str, case_id: str) -> str:
    return case_dir(client_id, case_id) + TIMELINE_FILENAME


def evidence_key(client_id: str, case_id: str, evidence_id: str) -> str:
    return f"{case_dir(client_id, case_id)}{EVIDENCE_DIRNAME}/{evidence_id}.json"


def new_case_id() -> str:
    """`case_<16 hex>`."""
    return f"case_{secrets.token_hex(8)}"


def _new_event_id() -> str:
    return f"ev_{secrets.token_hex(10)}"


def payload_digest(payload: Mapping[str, Any]) -> str:
    """sha256 of a payload's canonical JSON (sorted keys, no spaces): stable across processes."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _valid_id(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value) is not None


def _dump(model: BaseModel) -> bytes:
    return (model.model_dump_json(indent=2, by_alias=True) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------
_PROCESS_LOCKS_GUARD: Final[threading.Lock] = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _process_lock(key: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def _create_exclusive(storage: Storage, key: str, data: bytes) -> bool:
    """Write `key` only if it does not exist; True when this call created it."""
    if isinstance(storage, LocalStorage):
        path = storage.local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return True
    create = getattr(storage, "write_bytes_if_absent", None)
    if callable(create):
        return bool(create(key, data))
    if storage.exists(key):  # within one process the caller already holds the process lock
        return False
    storage.write_bytes(key, data)
    return True


@contextmanager
def _case_lock(
    storage: Storage,
    directory: str,
    *,
    wait_seconds: float,
    stale_seconds: float,
    clock: Callable[[], datetime],
) -> Iterator[None]:
    """Hold the case's lock file for the duration; `CASE_VERSION_CONFLICT` when it cannot be taken in time."""
    key = directory + LOCK_FILENAME
    holder = secrets.token_hex(8)
    body = json.dumps({"holder": holder, "at": clock().isoformat()}).encode("utf-8")
    deadline = time.monotonic() + wait_seconds
    with _process_lock(directory):
        while not _create_exclusive(storage, key, body):
            if _break_if_stale(storage, key, stale_seconds, clock):
                continue
            if time.monotonic() >= deadline:
                raise CaseError(
                    "CASE_VERSION_CONFLICT",
                    "Someone else is changing this case right now. Reload it and try again.",
                )
            time.sleep(_POLL_SECONDS)
        try:
            yield
        finally:
            try:
                if json.loads(storage.read_bytes(key)).get("holder") == holder:
                    storage.delete(key)
            except (StorageError, ValueError, AttributeError):
                _LOG.warning("case.lock could not be released cleanly")


def _break_if_stale(storage: Storage, key: str, stale_seconds: float, clock: Callable[[], datetime]) -> bool:
    """Delete a lock a crashed writer left behind; True when it was deleted."""
    try:
        seen = json.loads(storage.read_bytes(key))
        taken = datetime.fromisoformat(str(seen["at"]))
    except (StorageError, ValueError, KeyError, TypeError):
        return False
    if clock() - taken < timedelta(seconds=stale_seconds):
        return False
    try:  # re-read so a lock someone took meanwhile is not the one deleted
        if json.loads(storage.read_bytes(key)).get("holder") != seen.get("holder"):
            return False
    except (StorageError, ValueError):
        return False
    storage.delete(key)
    _LOG.warning("case.lock broke a stale lock older than %.0f s", stale_seconds)
    return True


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
ChangeResult = tuple[Case, Sequence["EventDraft"]]


class EventDraft(StrictBase):
    """A timeline event before the store gives it an id, a version and a time."""

    kind: TimelineKind
    upload_id: str | None = None
    item_id: str | None = None
    details: dict[str, DetailValue] = Field(default_factory=dict)


class CaseStore:
    """Reads and writes cases under one storage, one writer at a time per case."""

    def __init__(
        self,
        storage: Storage,
        *,
        clock: Callable[[], datetime] = utc_now,
        lock_wait_seconds: float = LOCK_WAIT_SECONDS,
        lock_stale_seconds: float = LOCK_STALE_SECONDS,
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._wait = lock_wait_seconds
        self._stale = lock_stale_seconds

    @property
    def storage(self) -> Storage:
        return self._storage

    # -- reading ----------------------------------------------------------------------------------
    def get(self, client_id: str, case_id: str) -> Case:
        """The case, or `CASE_NOT_FOUND` - also for another client's case, which is never named."""
        if not (_valid_id(CLIENT_ID_PATTERN, client_id) and _valid_id(CASE_ID_PATTERN, case_id)):
            raise _not_found(case_id)
        try:
            case = self._storage.read_model(case_key(client_id, case_id), Case)
        except StorageError as exc:
            raise _not_found(case_id) from exc
        except ValueError as exc:  # pydantic's ValidationError and a JSON error are both ValueErrors
            _LOG.warning("case.unreadable a stored case.json failed validation")
            raise _unreadable() from exc
        if case.client_id != client_id or case.case_id != case_id:
            raise _not_found(case_id)
        return case

    def timeline(self, client_id: str, case_id: str) -> tuple[TimelineEvent, ...]:
        """Every event of the case, oldest first, including any a crash kept out of the file."""
        case = self.get(client_id, case_id)
        events = _read_timeline(self._storage, timeline_key(client_id, case_id))
        seen = {event.event_id for event in events}
        return (*events, *(event for event in case.last_events if event.event_id not in seen))

    def list(self, client_id: str) -> tuple[Case, ...]:
        """The client's cases, newest change first."""
        if not _valid_id(CLIENT_ID_PATTERN, client_id):
            return ()
        found: list[Case] = []
        for key in self._storage.list_keys(f"{CASE_PREFIX}{client_id}/"):
            parts = key.split("/")
            if len(parts) == 4 and parts[3] == CASE_FILENAME:
                try:
                    found.append(self.get(client_id, parts[2]))
                except CaseError:
                    continue
        return tuple(sorted(found, key=lambda case: case.updated_at, reverse=True))

    # -- writing ----------------------------------------------------------------------------------
    def create(
        self,
        *,
        client_id: str,
        created_by: str,
        title: str = "",
        files: Sequence[CaseFile] = (),
    ) -> Case:
        """A new case at version 1, its `case.created` event and one `file.added` per file."""
        now = self._clock()
        case_id = new_case_id()
        drafts: list[EventDraft] = [EventDraft(kind=TimelineKind.CASE_CREATED, details={"files": len(files)})]
        drafts += [EventDraft(kind=TimelineKind.FILE_ADDED, upload_id=file.upload_id) for file in files]
        base = Case(
            case_id=case_id,
            client_id=client_id,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            version=1,
            title=title,
            files=tuple(files),
        )
        directory = case_dir(client_id, case_id)
        with _case_lock(
            self._storage, directory, wait_seconds=self._wait, stale_seconds=self._stale, clock=self._clock
        ):
            if self._storage.exists(directory + CASE_FILENAME):  # 64 random bits: practically never
                raise CaseError("CASE_VERSION_CONFLICT", "A case with this id exists already; try again.")
            case = self._commit(base, drafts, actor=created_by)
        _LOG.info("case.created case=%s files=%d", case_id, len(files))
        return case

    def update(
        self,
        client_id: str,
        case_id: str,
        *,
        expected_version: int,
        actor: str,
        change: Callable[[Case], ChangeResult],
    ) -> Case:
        """Apply `change` to the case as it is at `expected_version`, or refuse with `CASE_VERSION_CONFLICT`.

        `change` returns the new case (its version and time are set here) and the events it caused.
        """
        directory = case_dir(client_id, case_id)
        current = self.get(client_id, case_id)  # CASE_NOT_FOUND before any lock is taken
        if current.read_only:
            raise _read_only(case_id)
        with _case_lock(
            self._storage, directory, wait_seconds=self._wait, stale_seconds=self._stale, clock=self._clock
        ):
            current = self.get(client_id, case_id)
            self._replay(current)
            if current.version != expected_version:
                raise CaseError(
                    "CASE_VERSION_CONFLICT",
                    f"The case changed since you loaded it (version {current.version}, you sent "
                    f"{expected_version}). Reload it and try again.",
                )
            changed, drafts = change(current)
            bumped = changed.model_copy(
                update={"version": current.version + 1, "updated_at": max(self._clock(), current.updated_at)}
            )
            return self._commit(bumped, drafts, actor=actor)

    def add_file(
        self, client_id: str, case_id: str, *, expected_version: int, actor: str, file: CaseFile
    ) -> Case:
        """Add one file; adding a file the case already has changes nothing but the version check."""

        def change(case: Case) -> ChangeResult:
            if file.upload_id in case.upload_ids():
                return case, (
                    EventDraft(
                        kind=TimelineKind.FILE_ADDED, upload_id=file.upload_id, details={"already": True}
                    ),
                )
            return (
                case.model_copy(update={"files": (*case.files, file)}),
                (EventDraft(kind=TimelineKind.FILE_ADDED, upload_id=file.upload_id),),
            )

        return self.update(client_id, case_id, expected_version=expected_version, actor=actor, change=change)

    def put_evidence(
        self,
        client_id: str,
        case_id: str,
        *,
        expected_version: int,
        actor: str,
        evidence_id: str,
        tool: str,
        upload_id: str | None,
        result: Mapping[str, JsonValue],
    ) -> Case:
        """Store one tool result under `evidence/` and reference it from the case (M232 calls this)."""
        now = self._clock()
        ref = CaseEvidenceRef(evidence_id=evidence_id, tool=tool, upload_id=upload_id, created_at=now)
        document = {"evidence_id": evidence_id, "tool": tool, "upload_id": upload_id, "result": dict(result)}

        def change(case: Case) -> ChangeResult:
            if evidence_id in {item.evidence_id for item in case.evidence}:
                return case, ()
            self._storage.write_bytes(
                evidence_key(client_id, case_id, evidence_id),
                (json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            )
            return (
                case.model_copy(update={"evidence": (*case.evidence, ref)}),
                (
                    EventDraft(
                        kind=TimelineKind.EVIDENCE_ADDED,
                        upload_id=upload_id,
                        item_id=evidence_id,
                        details={"tool": tool},
                    ),
                ),
            )

        return self.update(client_id, case_id, expected_version=expected_version, actor=actor, change=change)

    # -- internals --------------------------------------------------------------------------------
    def _commit(self, case: Case, drafts: Sequence[EventDraft], *, actor: str) -> Case:
        """Write `case` with its events: `case.json` first (atomic, holding the events), then the timeline."""
        events = tuple(
            TimelineEvent(
                event_id=_new_event_id(),
                case_id=case.case_id,
                client_id=case.client_id,
                version=case.version,
                at=case.updated_at,
                actor=actor,
                kind=draft.kind,
                upload_id=draft.upload_id,
                item_id=draft.item_id,
                details=draft.details,
            )
            for draft in drafts
        )
        try:
            final = Case.model_validate(case.model_copy(update={"last_events": events}).model_dump())
        except ValidationError as exc:
            raise ValueError(f"the changed case is not valid: {exc.errors()[0]['msg']}") from exc
        self._storage.write_bytes(case_key(case.client_id, case.case_id), _dump(final))
        _append_events(self._storage, timeline_key(case.client_id, case.case_id), events)
        return final

    def _replay(self, case: Case) -> None:
        """Append the events a crash kept out of the timeline (they are in `case.last_events`)."""
        _replay_events(self._storage, timeline_key(case.client_id, case.case_id), case.last_events)


def _not_found(case_id: str) -> CaseError:
    del case_id  # never echoed: an id from the request could carry anything
    return CaseError("CASE_NOT_FOUND", "There is no such case for this client.")


def _unreadable() -> CaseError:
    return CaseError(
        "CASE_UNREADABLE",
        "This case is stored in a form that cannot be read. Nothing was changed; ask an administrator to "
        "look at it.",
    )


def _read_only(case_id: str) -> CaseError:
    del case_id
    return CaseError(
        "CASE_READ_ONLY",
        "This is an earlier Guided setup or Data Doctor result shown as a case; it cannot be changed here.",
    )


def _read_timeline(storage: Storage, key: str) -> tuple[TimelineEvent, ...]:
    try:
        text = storage.read_bytes(key).decode("utf-8")
    except StorageError:
        return ()
    events: list[TimelineEvent] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(TimelineEvent.model_validate_json(line))
        except ValidationError:
            _LOG.warning("case.timeline skipped an unreadable line")
    return tuple(events)


def _appends_in_place(storage: Storage) -> bool:
    """A plain local store appends to the file itself; anything else (a subclass, S3) is read and rewritten."""
    return type(storage) is LocalStorage


def _append_events(storage: Storage, key: str, events: Sequence[TimelineEvent]) -> None:
    """Append `events` to the timeline. The caller holds the case's lock.

    On a local store the lines are appended in place and fsynced, so a write costs its own events, not the
    timeline's length. A line torn by a crash mid-append is closed with a newline first, so it stays one
    unreadable line (skipped on read) and never swallows the next event. A store without append (S3) reads
    the object and writes it back whole.
    """
    if not events:
        return
    lines = "".join(event.model_dump_json() + "\n" for event in events).encode("utf-8")
    if _appends_in_place(storage):
        path = storage.local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    lines = b"\n" + lines
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())
        return
    try:
        existing = storage.read_bytes(key)
    except StorageError:
        existing = b""
    if existing and not existing.endswith(b"\n"):
        existing += b"\n"
    storage.write_bytes(key, existing + lines)


_TAIL_BLOCK: Final[int] = 1 << 16


def _tail_lines(storage: Storage, key: str, count: int) -> tuple[bytes, ...]:
    """The last `count` lines of the timeline, read from the end on a local store."""
    if not _appends_in_place(storage):
        try:
            return tuple(storage.read_bytes(key).splitlines()[-count:])
        except StorageError:
            return ()
    path = storage.local_path(key)
    try:
        with path.open("rb") as handle:
            end = handle.seek(0, os.SEEK_END)
            position, chunk = end, b""
            while position > 0 and chunk.count(b"\n") <= count:
                step = min(_TAIL_BLOCK, position)
                position -= step
                handle.seek(position)
                chunk = handle.read(step) + chunk
    except FileNotFoundError:
        return ()
    return tuple(chunk.splitlines()[-count:])


def _replay_events(storage: Storage, key: str, events: Sequence[TimelineEvent]) -> int:
    """Append those of `events` the timeline does not hold yet; how many were appended.

    `events` are the latest version's (`last_events`), written after every earlier line, so whichever of
    them reached the timeline are among its last `len(events)` lines (one more for a torn line): only that
    tail is read, never the whole log.
    """
    if not events:
        return 0
    present: set[str] = set()
    for line in _tail_lines(storage, key, len(events) + 1):
        try:
            present.add(TimelineEvent.model_validate_json(line).event_id)
        except ValidationError:
            continue
    missing = tuple(event for event in events if event.event_id not in present)
    if missing:
        _append_events(storage, key, missing)
        _LOG.warning("case.timeline replayed %d event(s)", len(missing))
    return len(missing)


def _lenient_events(
    raw: object, scrub: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
) -> tuple[TimelineEvent, ...]:
    """`last_events` of a raw document as events; an entry that is not one (an older engine's) is skipped."""
    events: list[TimelineEvent] = []
    for entry in raw if isinstance(raw, list) else ():
        if not isinstance(entry, dict):
            continue
        cleaned = scrub(entry) if scrub is not None else entry
        if cleaned is None:
            continue
        try:
            events.append(TimelineEvent.model_validate(cleaned))
        except ValidationError:
            _LOG.warning("case.timeline skipped an unreadable pending event")
    return tuple(events)


def is_valid_case(document: object) -> bool:
    """True when a raw document is a `Case` as this engine reads it."""
    try:
        Case.model_validate(document)
    except ValidationError:
        return False
    return True


# ---------------------------------------------------------------------------
# Privacy: raw-document rewrites used by `engine.privacy` (retention and erasure)
# ---------------------------------------------------------------------------
def rewrite_case_document(
    storage: Storage,
    key: str,
    transform: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, int]]],
    *,
    actor: str,
    kind: TimelineKind,
    upload_id: str | None = None,
    clock: Callable[[], datetime] = utc_now,
    scrub_pending: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, int]:
    """Rewrite one stored `case.json` as raw JSON under the case's lock, bump its version, log one event.

    Raw on purpose, as `engine.privacy.layout` reads every document: a retention or erasure pass must not
    stop at a case written by an older engine. `transform` returns the new document and counts; when every
    count is zero nothing is written. Returns the counts.

    Before anything else, the events the last write left pending (`last_events` a crash kept out of the
    timeline) are replayed, as `CaseStore.update` does, so replacing them loses none; `scrub_pending`, when
    given, cleans each first (erasure: the person's id never re-enters the log through a replay). A case
    that was valid must stay valid: a rewrite that would leave it unreadable raises `CASE_UNREADABLE` and
    writes nothing.
    """
    parts = key.split("/")
    if len(parts) != 4 or parts[0] + "/" != CASE_PREFIX or parts[3] != CASE_FILENAME:
        raise ValueError("not a case document key")
    directory = f"{CASE_PREFIX}{parts[1]}/{parts[2]}/"
    with _case_lock(
        storage, directory, wait_seconds=LOCK_WAIT_SECONDS * 6, stale_seconds=LOCK_STALE_SECONDS, clock=clock
    ):
        try:
            document = json.loads(storage.read_bytes(key))
        except (StorageError, ValueError):
            return {}
        if not isinstance(document, dict):
            return {}
        _replay_events(
            storage,
            directory + TIMELINE_FILENAME,
            _lenient_events(document.get("last_events"), scrub_pending),
        )
        was_valid = is_valid_case(document)
        changed, counts = transform(document)
        if not any(counts.values()):
            return counts
        now = clock()
        version = changed.get("version")
        changed["version"] = (
            version if isinstance(version, int) and not isinstance(version, bool) else 0
        ) + 1
        changed["updated_at"] = now.isoformat()
        event = TimelineEvent(
            event_id=_new_event_id(),
            case_id=str(changed.get("case_id", parts[2])),
            client_id=str(changed.get("client_id", parts[1])),
            version=changed["version"],
            at=now,
            actor=actor,
            kind=kind,
            upload_id=upload_id,
            details=dict(counts),
        )
        changed["last_events"] = [event.model_dump(mode="json")]
        if was_valid and not is_valid_case(changed):
            _LOG.warning("case.rewrite refused: the rewritten case would not be readable")
            raise _unreadable()
        storage.write_bytes(key, (json.dumps(changed, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        _append_events(storage, directory + TIMELINE_FILENAME, (event,))
        return counts


def forget_upload(document: dict[str, Any], upload_id: str) -> tuple[dict[str, Any], dict[str, int]]:
    """A raw case document without one upload: its file entry, the items, questions, doubts, standing
    answers and evidence references about it; prescriptions that read it are marked `orphaned`.

    The chat is about the files, so it goes when the last file goes. Approvals stay: they are the
    processing log (who approved what, when), and hold item ids and the sentence shown, not cells.
    """
    out = dict(document)
    counts = {"files": 0, "items": 0, "questions": 0, "doubts": 0, "standing_answers": 0, "evidence": 0}

    def without(field: str, counter: str) -> None:
        values = out.get(field)
        if not isinstance(values, list):
            return
        kept = [
            value for value in values if not (isinstance(value, dict) and value.get("upload_id") == upload_id)
        ]
        counts[counter] += len(values) - len(kept)
        out[field] = kept

    without("files", "files")
    without("items", "items")
    without("questions", "questions")
    without("doubts", "doubts")
    without("standing_answers_used", "standing_answers")
    without("evidence", "evidence")
    prescriptions = out.get("prescriptions")
    if isinstance(prescriptions, list):
        orphaned = 0
        for entry in prescriptions:
            if (
                isinstance(entry, dict)
                and upload_id in (entry.get("upload_ids") or ())
                and not entry.get("orphaned")
            ):
                entry["orphaned"] = True
                orphaned += 1
        counts["prescriptions_orphaned"] = orphaned
    visit = out.get("visit")
    if isinstance(visit, dict):  # M235: the visit forgets the file, and the options of its questions
        asked = {q.get("question_id") for q in out.get("questions") or () if isinstance(q, dict)}
        offers = visit.get("offers")
        out["visit"] = {
            **visit,
            "taken_in": [u for u in visit.get("taken_in") or () if u != upload_id],
            "offers": {k: v for k, v in offers.items() if k in asked} if isinstance(offers, dict) else {},
        }
    files = out.get("files")
    if counts["files"] and isinstance(files, list) and not files and out.get("chat"):
        counts["chat_messages"] = len(out["chat"]) if isinstance(out["chat"], list) else 0
        out["chat"] = []
    if isinstance(out.get("diagnosis"), dict) and counts["files"]:
        out["diagnosis"] = None  # computed over a set of files that no longer exists
    return out, counts


def _text(value: object) -> str:
    """As `engine.agent.value_steps` compares a correction's key: trimmed text, so 7 and "7" are one id."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def remove_principal(document: dict[str, Any], principal_id: str) -> tuple[dict[str, Any], dict[str, int]]:
    """A raw case document without one person's corrections and standing answers.

    * a `correct_cells` item's correction whose `key` is the id is removed (the key *is* the row's ID, so
      a numeric id is matched too, as trimmed text); an item left with no correction is removed whole;
    * a standing answer whose value signature holds the id is removed (`standing_answers_used`, and a
      client's own `standing_answers` list in the same shape, M245).

    Any other occurrence is left to the generic JSON rewrite that runs after this (`engine.privacy`).
    """
    wanted = _text(principal_id)
    out = dict(document)
    counts = {"corrections": 0, "items": 0, "standing_answers": 0}
    items = out.get("items")
    if isinstance(items, list):
        kept_items: list[Any] = []
        for item in items:
            cells_holder = _correct_cells_params(item)
            if cells_holder is None:
                kept_items.append(item)
                continue
            cells = cells_holder["cells"]
            remaining = [c for c in cells if not (isinstance(c, dict) and _text(c.get("key")) == wanted)]
            removed = len(cells) - len(remaining)
            if not removed:
                kept_items.append(item)
                continue
            counts["corrections"] += removed
            if not remaining:
                counts["items"] += 1
                continue
            cells_holder["cells"] = remaining
            payload = item.get("payload")
            if isinstance(payload, dict):
                item["payload_digest"] = payload_digest(payload)
            kept_items.append(item)
        out["items"] = kept_items
    for field in ("standing_answers_used", "standing_answers"):
        answers = out.get(field)
        if not isinstance(answers, list):
            continue
        kept = [
            answer
            for answer in answers
            if not (
                isinstance(answer, dict)
                and any(_text(value) == wanted for value in (answer.get("value_signature") or ()))
            )
        ]
        counts["standing_answers"] += len(answers) - len(kept)
        out[field] = kept
    return out, counts


def _correct_cells_params(item: object) -> dict[str, Any] | None:
    """The dict holding a `correct_cells` step's `cells` list inside an item's payload, if it is one."""
    if not isinstance(item, dict):
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    for holder in (payload, payload.get("step")):
        if not isinstance(holder, dict) or holder.get("kind") != "correct_cells":
            continue
        params = holder.get("params")
        if isinstance(params, dict) and isinstance(params.get("cells"), list):
            return params
    return None


# ---------------------------------------------------------------------------
# Adapters: older work opened as a case, read-only
# ---------------------------------------------------------------------------
_SESSION_STATES: Final[dict[str, VisitState]] = {
    "analysing": VisitState.UNDERSTAND,
    "needs_review": VisitState.TREAT,
    "ready": VisitState.PREVIEW,
    "applied": VisitState.HAND_OFF,
    "stopped": VisitState.TREAT,
    "failed": VisitState.TREAT,
}


def case_from_session(
    session: AgentSession,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    file_name: str = "",
) -> Case:
    """A Guided-setup session as a read-only case (`guided_<upload id>`). Writes nothing.

    Plan G records no user id, so `created_by` and every decision's author read `guided-setup`.
    """
    upload_id = session.upload_id
    items = tuple(
        CaseItem(
            item_id=proposal.proposal_id,
            kind=proposal.step.kind.value if proposal.step is not None else proposal.kind.value,
            title=proposal.title,
            source=proposal.source or ProposalSource.RULES,
            confidence=proposal.confidence,
            state=proposal.state,
            upload_id=upload_id,
            evidence_ids=proposal.evidence_ids,
            payload=(payload := proposal.model_dump(mode="json", exclude={"examples"})),
            payload_digest=payload_digest(payload),
            proposed_at=session.created_at,
            decided_by="guided-setup" if proposal.decided_by is not None else None,
            decided_at=session.updated_at if proposal.decided_by is not None else None,
        )
        for proposal in session.proposals
    )
    questions = tuple(
        CaseQuestion(
            question_id=question.question_id,
            upload_id=upload_id,
            text=question.text,
            options=tuple(
                CaseOption(
                    option_id=option.option_id,
                    label=option.label,
                    effect_digest=(
                        payload_digest(option.proposal.model_dump(mode="json"))
                        if option.proposal is not None
                        else None
                    ),
                )
                for option in question.options
            ),
            blocking=question.blocking,
            answer=question.answer,
            answered_by="guided-setup" if question.answer is not None else None,
            answered_at=session.updated_at if question.answer is not None else None,
        )
        for question in session.questions
    )
    evidence = tuple(
        CaseEvidenceRef(
            evidence_id=result.evidence_id,
            tool=result.tool,
            upload_id=upload_id,
            created_at=result.created_at,
        )
        for result in session.tool_results
    )
    chat = tuple(
        CaseChatEntry(
            role="user" if message.role.value == "user" else "agent",
            text=message.text,
            at=message.created_at,
            sent=tuple(item.model_dump(mode="json") for item in message.sent),
        )
        for message in session.transcript
    )
    return Case(
        case_id=f"guided_{upload_id}",
        client_id=client_id,
        created_by="guided-setup",
        created_at=session.created_at,
        updated_at=max(session.updated_at, session.created_at),
        state=_SESSION_STATES.get(session.status.value, VisitState.TREAT),
        version=1,
        title=file_name[:200],  # `Case.title` holds 200; the file entry keeps the whole name
        source=CaseSource.GUIDED_SESSION,
        read_only=True,
        files=(
            CaseFile(
                upload_id=upload_id, file_name=file_name, added_at=session.created_at, added_by="guided-setup"
            ),
        ),
        goals=(
            CaseGoal(
                goal_id=session.use_case_id, active=True, set_at=session.created_at, set_by="guided-setup"
            ),
        ),
        items=items,
        questions=questions,
        evidence=evidence,
        chat=chat,
        counters=CaseCounters(model_calls=session.llm_calls, tool_calls=len(session.tool_results)),
    )


def case_from_diagnosis(
    diagnosis: BaseModel | Mapping[str, Any],
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    created_at: datetime | None = None,
    created_by: str = "data-doctor",
) -> Case:
    """A Doctor diagnosis (`POST /doctor/diagnosis`) as a read-only case (`doctor_<16 hex>`). Writes nothing.

    The id is a hash of the sorted file ids, so the same set of files opens as the same case in any order;
    the files keep the diagnosis's order.
    """
    document: dict[str, Any] = (
        diagnosis.model_dump(mode="json")
        if isinstance(diagnosis, BaseModel)
        else json.loads(json.dumps(diagnosis))
    )
    moment = created_at or utc_now()
    files_in = [entry for entry in document.get("files", ()) if isinstance(entry, dict)]
    files = tuple(
        CaseFile(
            upload_id=str(entry["upload_id"]),
            file_name=str(entry.get("file_name") or ""),
            role=entry.get("kind") if isinstance(entry.get("kind"), str) else None,
            key_columns=(str(entry["id_column"]),) if isinstance(entry.get("id_column"), str) else (),
            added_at=moment,
            added_by=created_by,
        )
        for entry in files_in
    )
    ids = ",".join(sorted(file.upload_id for file in files))
    return Case(
        case_id=f"doctor_{hashlib.sha256(ids.encode('utf-8')).hexdigest()[:16]}",
        client_id=client_id,
        created_by=created_by,
        created_at=moment,
        updated_at=moment,
        state=VisitState.UNDERSTAND,
        version=1,
        source=CaseSource.DOCTOR_DIAGNOSIS,
        read_only=True,
        files=files,
        diagnosis=document,
    )
