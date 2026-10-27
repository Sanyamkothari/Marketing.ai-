"""The prescription: the one saved unit Data Doctor writes, hashed and replayed as a unit (Plan O M243, DEC-1813).

The design §9.1. Everything Data Doctor decided with the person about a case's files is kept together, under one hash,
and replayed together - at **training and at scoring**, for single files and for built datasets
(`engine.doctor.prescription_replay`):

=================  ====================================================================================================
Part               Holds
=================  ====================================================================================================
files              per file slot: the intake decisions (sheet, header row, intake on or off; missing words noted),
                   the `DataRecipe`, and the column contract of the prepared file
build              the approved build plan (M237's `BuildRecord`: stacks, ID rules, mappings, features, label)
locale             the locale profile (M241) by `profile_hash`, and the money basis (M242: every money column's unit
                   and every frozen rate) by `basis_hash`
post_build         a recipe on the built (or single prepared) file, for training **and** scoring
contract           the column contract of the final prepared file, when it is known at approval
goals              one section per goal brief: the outcome definition (M239, by `definition_hash`), the roles, the
                   settings, the split and time settings from the brief, the acknowledgements
provenance         who approved, receipts, brief hashes, item digests, engine and agent versions, when
=================  ====================================================================================================

**One hash.** :func:`prescription_hash` covers every part except reasons, wording and provenance, as `recipe_hash` does
today: a recipe counts by its logic (`recipe_hash` with the roles, levels and limit `DataRecipe.prepares_like`
compares), a build by its `plan_hash`, an outcome by its `definition_hash`, the locale by `profile_hash` and
`basis_hash`. It is canonical JSON (`engine.agentic.canonical`), so it is the same in every process, and it changes
when any replayed part changes. Who approved, the upload ids the parts were approved on, and when, are provenance: the
same decisions on next month's files hash the same.

**Stored** under the case, content-addressed: `cases/<client_id>/<case_id>/prescriptions/<hash>.json`
(:func:`save_prescription`, written once), read back with its hash checked (:func:`load_prescription`).

**An existing `DataRecipe` reads as a prescription with one part** (:func:`one_part`): one file slot holding the recipe
and nothing else, so a model trained today keeps scoring exactly as it does (the replay of a one-part prescription is
`run_saved_recipe`, byte for byte).

**PRESCRIBE.** :class:`CasePrescriber` is M235's `VisitDeps.prescribe` port: it builds the prescription from the case's
approved items (:func:`prescription_from_case`), writes it, binds the case's uploads and built dataset to it
(`engine.doctor.prescription_runs`), and returns the hash; its :meth:`CasePrescriber.digest` gives the same hash without
writing, which M246's approval record carries as `prescription_hash`.

Everything here runs only behind `data_doctor.unified`: the visit's routes are not mounted while it is off. Rules only;
no AI; no row is stored - the contract holds counts, shares, percentiles and values held by at least ten rows.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
from pydantic import AwareDatetime, Field, JsonValue, ValidationError, model_validator

from engine.agent.contracts import DataRecipe, ProposalState, RecipeStep, RecipeStepKind, recipe_hash
from engine.agentic.canonical import digest
from engine.config import StrictBase
from engine.contracts import Artefact
from engine.doctor.build_driver import BuildRecord
from engine.doctor.currency import MoneyBasis
from engine.doctor.locale import LocaleProfile
from engine.doctor.outcome_definition import (
    OutcomeApproval,
    OutcomeDefinition,
    OutcomeError,
    apply_outcome_values,
)
from engine.doctor.prescription_contract import DataContract, DriftFinding, learn_contract
from engine.intake.contracts import IntakeReport
from engine.storage import Storage, StorageError

if TYPE_CHECKING:
    from engine.config import UseCaseConfig
    from engine.doctor.case import Case, CaseItem
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.guards import GuardedRun

__all__ = [
    "AGENT_VERSION",
    "PRESCRIPTIONS_DIRNAME",
    "PRESCRIPTION_CODES",
    "PRESCRIPTION_DRIFT",
    "PRESCRIPTION_NOT_FOUND",
    "BuildPart",
    "BuiltRows",
    "CasePrescriber",
    "FilePart",
    "GoalSection",
    "IntakeDecision",
    "LocalePart",
    "OutcomePart",
    "Prescription",
    "PrescriptionError",
    "Provenance",
    "SettingChoice",
    "case_tables",
    "load_prescription",
    "make_prescription",
    "one_part",
    "prescription_from_case",
    "prescription_hash",
    "prescription_key",
    "recipe_logic",
    "save_prescription",
    "slot_name",
]

PRESCRIPTION_DRIFT: Final[str] = "PRESCRIPTION_DRIFT"
"""A replay saw a change it cannot prove safe (a column gone or changed, a new value, another locale or intake): it
stops and asks. Never re-learnt."""
PRESCRIPTION_NOT_FOUND: Final[str] = "PRESCRIPTION_NOT_FOUND"
"""A prescription that is not stored under that hash, or whose content no longer has that hash."""
PRESCRIPTION_CODES: Final[frozenset[str]] = frozenset({PRESCRIPTION_DRIFT, PRESCRIPTION_NOT_FOUND})
"""The codes this milestone raises; the integrator adds them to `PLAN_O_CODES` with their help entries."""

PRESCRIPTIONS_DIRNAME: Final[str] = "prescriptions"
AGENT_VERSION: Final[str] = "data-doctor/1"
HASH_PATTERN: Final[str] = r"^[a-f0-9]{64}$"
CONTRACT_ROWS: Final[int] = 100_000
"""At most this many rows of a built dataset are read to learn its contract (shares and percentiles of a sample)."""
POST_BUILD: Final[str] = "post_build"
"""An item payload's `stage` that puts its step in the post-build recipe."""


class PrescriptionError(Exception):
    """A prescription that cannot be made, read or replayed. A replay's stop carries what it found and its question."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        findings: Sequence[DriftFinding] = (),
        part: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.findings = tuple(findings)
        self.part = part

    @property
    def question(self) -> Any:
        """The first stopping finding's question (what the person is asked), or None."""
        stop = next((f for f in self.findings if f.stops), None)
        return None if stop is None else stop.question


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------
class IntakeDecision(StrictBase):
    """How a file was read at approval (`IntakeReport`): what replay reads the same way, or stops."""

    source_format: str
    sheet: str | None = None
    header_row: int = 1
    header_rows: int = 1
    turned_off: bool = False
    missing_words: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Column -> the words read as empty (noted; a new word is a warning).",
    )

    @classmethod
    def from_report(cls, report: IntakeReport) -> IntakeDecision:
        return cls(
            source_format=report.source_format,
            sheet=report.sheet,
            header_row=report.header_row,
            header_rows=report.header_rows,
            turned_off=report.turned_off,
            missing_words={m.column: tuple(sorted(m.words)) for m in report.missing_words},
        )

    def decisions(self) -> dict[str, Any]:
        """The decisions a different read would break (the missing words are noted, not decided)."""
        return self.model_dump(mode="json", exclude={"missing_words"})


class FilePart(StrictBase):
    """One file slot: a file, or the monthly exports stacked into one builder source."""

    slot: str = Field(min_length=1, max_length=128, description="Its stable name (a build source, a role).")
    upload_ids: tuple[str, ...] = Field(default=(), description="Provenance: the uploads it was approved on.")
    file_names: tuple[str, ...] = Field(default=(), description="Provenance: their names.")
    intake: IntakeDecision | None = None
    recipe: DataRecipe | None = None
    contract: DataContract | None = Field(default=None, description="The prepared file's contract.")


class BuildPart(StrictBase):
    """The approved build plan (M237), replayed through the same builder."""

    record: BuildRecord
    dataset_id: str | None = Field(default=None, description="Provenance: the dataset it built at approval.")


class OutcomePart(StrictBase):
    """The approved outcome definition (M239): its meaning, by hash; the approval is provenance."""

    definition: OutcomeDefinition
    approval: OutcomeApproval | None = None


class LocalePart(StrictBase):
    """The locale profile (M241) and the money basis (M242): every money column's unit and every frozen rate."""

    profile: LocaleProfile | None = None
    money_basis: MoneyBasis | None = None

    @property
    def profile_hash(self) -> str | None:
        return None if self.profile is None else self.profile.profile_hash

    @property
    def basis_hash(self) -> str | None:
        return None if self.money_basis is None else self.money_basis.basis_hash


class SettingChoice(StrictBase):
    """One approved setting of a goal section (`path` as a run override names it)."""

    path: str
    value: JsonValue = None


class GoalSection(StrictBase):
    """Everything one goal brief adds (the design §9.1 "Goal sections"; DEC-1805 (j))."""

    goal_id: str
    active: bool = False
    brief_hash: str | None = Field(default=None, description="Provenance: the brief it was approved under.")
    outcome: OutcomePart | None = None
    roles: dict[str, str] = Field(
        default_factory=dict, description="`key`, `outcome`, `as_of`, `consent` -> the column approved."
    )
    settings: tuple[SettingChoice, ...] = ()
    split: dict[str, JsonValue] | None = Field(default=None, description="The brief's split settings.")
    time_settings: dict[str, JsonValue] | None = Field(
        default=None, description="The brief's as-of date, cut points, cadence and training budget."
    )
    acknowledgements: tuple[str, ...] = ()
    item_digests: tuple[str, ...] = Field(default=(), description="Provenance: the items approved in it.")


class Provenance(StrictBase):
    """Who and when: never part of the hash."""

    case_id: str | None = None
    client_id: str | None = None
    approved_by: tuple[str, ...] = ()
    receipts: tuple[str, ...] = ()
    brief_hashes: tuple[str, ...] = ()
    item_digests: tuple[str, ...] = ()
    engine_version: str | None = None
    agent_version: str = AGENT_VERSION
    created_at: AwareDatetime


class Prescription(Artefact):
    """The one saved unit (the design §9.1). `prescription_hash` is checked on load."""

    prescription_hash: str = Field(pattern=HASH_PATTERN)
    files: tuple[FilePart, ...] = ()
    build: BuildPart | None = None
    locale: LocalePart | None = None
    post_build: DataRecipe | None = None
    contract: DataContract | None = None
    goals: tuple[GoalSection, ...] = ()
    provenance: Provenance

    @model_validator(mode="after")
    def _consistent(self) -> Prescription:
        slots = [part.slot for part in self.files]
        if len(set(slots)) != len(slots):
            raise ValueError("file slots repeat")
        goals = [section.goal_id for section in self.goals]
        if len(set(goals)) != len(goals):
            raise ValueError("goal sections repeat")
        if sum(section.active for section in self.goals) > 1:
            raise ValueError("at most one goal section is active")
        if self.prescription_hash != prescription_hash(self):
            raise ValueError("prescription_hash does not match the parts")
        return self

    def file(self, slot: str) -> FilePart | None:
        return next((part for part in self.files if part.slot == slot), None)

    def section(self, goal_id: str | None = None) -> GoalSection | None:
        """The section of `goal_id`, or the active one."""
        if goal_id is None:
            return next((s for s in self.goals if s.active), None)
        return next((s for s in self.goals if s.goal_id == goal_id), None)

    def recipes(self) -> tuple[DataRecipe, ...]:
        """Every recipe it replays, files first."""
        found = [part.recipe for part in self.files if part.recipe is not None]
        if self.post_build is not None:
            found.append(self.post_build)
        return tuple(found)


# ---------------------------------------------------------------------------
# The hash
# ---------------------------------------------------------------------------
def recipe_logic(recipe: DataRecipe | None) -> dict[str, Any] | None:
    """What a recipe's replay depends on - `DataRecipe.prepares_like`'s fields - never its id, reasons or approver."""
    if recipe is None:
        return None
    return {
        "recipe_hash": recipe.recipe_hash,
        "primary_key": recipe.primary_key,
        "target": recipe.target,
        "snapshot_column": recipe.snapshot_column,
        "levels": None if recipe.levels is None else [str(level) for level in recipe.levels],
        "max_failure_pct": recipe.max_failure_pct,
        "text_columns": list(recipe.text_columns),
    }


def _contract(contract: DataContract | None) -> Any:
    return None if contract is None else contract.model_dump(mode="json")


def _parts(
    *,
    files: Sequence[FilePart],
    build: BuildPart | None,
    locale: LocalePart | None,
    post_build: DataRecipe | None,
    contract: DataContract | None,
    goals: Sequence[GoalSection],
) -> dict[str, Any]:
    return {
        "files": [
            {
                "slot": part.slot,
                "intake": None if part.intake is None else part.intake.decisions(),
                "missing_words": None if part.intake is None else part.intake.missing_words,
                "recipe": recipe_logic(part.recipe),
                "contract": _contract(part.contract),
            }
            for part in files
        ],
        "build": None if build is None else build.record.plan.plan_hash,
        "locale": (
            None if locale is None else {"profile_hash": locale.profile_hash, "basis_hash": locale.basis_hash}
        ),
        "post_build": recipe_logic(post_build),
        "contract": _contract(contract),
        "goals": [
            {
                "goal_id": section.goal_id,
                "active": section.active,
                "outcome": None if section.outcome is None else section.outcome.definition.definition_hash,
                "roles": dict(section.roles),
                "settings": [s.model_dump(mode="json") for s in section.settings],
                "split": section.split,
                "time_settings": section.time_settings,
                "acknowledgements": list(section.acknowledgements),
            }
            for section in goals
        ],
    }


def prescription_hash(prescription: Prescription) -> str:
    """sha256 (64 hex) of every replayed part's logic; never reasons, wording or provenance."""
    return digest(
        _parts(
            files=prescription.files,
            build=prescription.build,
            locale=prescription.locale,
            post_build=prescription.post_build,
            contract=prescription.contract,
            goals=prescription.goals,
        )
    )


def query_makes(items: Sequence[CaseItem], column: str) -> bool:
    """True when an accepted query step's table has `column`."""
    for item in items:
        made, step = item.payload.get("columns"), item.payload.get("step")
        if (
            item.state is ProposalState.ACCEPTED
            and isinstance(made, list)
            and isinstance(step, dict)
            and step.get("kind") == "sql_query"
            and column in made
        ):
            return True
    return False


def prepared_key(items: Sequence[CaseItem], target: str | None, key: str | None) -> str | None:
    """The ID column a run reads when the outcome comes from an accepted query's table: the file's key where the
    table keeps it, else the table's own ID-named column (a table of one row per customer has no invoice id).
    """
    from engine.doctor.visit import _ID_NAMED

    for item in items:
        made = item.payload.get("columns")
        step = item.payload.get("step")
        if (
            target is not None
            and isinstance(made, list)
            and isinstance(step, dict)
            and step.get("kind") == "sql_query"
            and item.state is ProposalState.ACCEPTED
            and target in made
        ):
            if key in made:
                return key
            return next((str(c) for c in made if _ID_NAMED.search(str(c).strip())), key)
    return key


def make_prescription(
    *,
    provenance: Provenance,
    files: Sequence[FilePart] = (),
    build: BuildPart | None = None,
    locale: LocalePart | None = None,
    post_build: DataRecipe | None = None,
    contract: DataContract | None = None,
    goals: Sequence[GoalSection] = (),
) -> Prescription:
    """A prescription from its parts, with its hash."""
    hash_ = digest(
        _parts(files=files, build=build, locale=locale, post_build=post_build, contract=contract, goals=goals)
    )
    return Prescription(
        prescription_hash=hash_,
        files=tuple(files),
        build=build,
        locale=locale,
        post_build=post_build,
        contract=contract,
        goals=tuple(goals),
        provenance=provenance,
    )


def one_part(recipe: DataRecipe, *, slot: str = "file") -> Prescription:
    """An existing `DataRecipe` as a prescription with one part: one file slot holding the recipe, nothing else."""
    from engine import __version__

    return make_prescription(
        files=(FilePart(slot=slot, recipe=recipe),),
        provenance=Provenance(
            approved_by=() if recipe.approved_by is None else (recipe.approved_by,),
            engine_version=__version__,
            created_at=recipe.created_at,
        ),
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def prescription_key(client_id: str, case_id: str, hash_: str) -> str:
    """`cases/<client_id>/<case_id>/prescriptions/<hash>.json`."""
    from engine.doctor.case import case_dir

    if not re.fullmatch(HASH_PATTERN, hash_):
        raise PrescriptionError(PRESCRIPTION_NOT_FOUND, "That is not a prescription's hash.")
    return f"{case_dir(client_id, case_id)}{PRESCRIPTIONS_DIRNAME}/{hash_}.json"


def save_prescription(storage: Storage, client_id: str, case_id: str, prescription: Prescription) -> str:
    """Write the prescription under its hash, once (the same hash is the same content); its key."""
    key = prescription_key(client_id, case_id, prescription.prescription_hash)
    if not storage.exists(key):
        storage.write_model(key, prescription)
    return key


def load_prescription(storage: Storage, client_id: str, case_id: str, hash_: str) -> Prescription:
    """The stored prescription, its hash checked; `PRESCRIPTION_NOT_FOUND` when it is missing or altered."""
    key = prescription_key(client_id, case_id, hash_)
    try:
        if not storage.exists(key):
            raise PrescriptionError(PRESCRIPTION_NOT_FOUND, "No prescription is stored under that hash.")
        found = storage.read_model(key, Prescription)
    except (StorageError, ValidationError, ValueError) as exc:
        raise PrescriptionError(
            PRESCRIPTION_NOT_FOUND, "The stored prescription cannot be read under that hash."
        ) from exc
    if found.prescription_hash != hash_:
        raise PrescriptionError(PRESCRIPTION_NOT_FOUND, "The stored prescription has another hash.")
    return found


# ---------------------------------------------------------------------------
# From a case (PRESCRIBE)
# ---------------------------------------------------------------------------
_SLOT: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]+")
_ROLE_KINDS: Final[Mapping[str, str]] = {
    "key": "key",
    "outcome_column": "outcome",
    "as_of_column": "as_of",
    "consent_column": "consent",
}


def _accepted(items: Sequence[CaseItem]) -> list[CaseItem]:
    return [item for item in items if item.state is ProposalState.ACCEPTED]


def _step_of(item: CaseItem) -> RecipeStep | None:
    step = item.payload.get("step")
    if not isinstance(step, dict):
        return None
    try:
        return RecipeStep.model_validate(step)
    except ValidationError:
        return None


def _column(item: CaseItem) -> str | None:
    column = item.payload.get("column")
    return column if isinstance(column, str) and column else None


def _unique_steps(steps: Sequence[RecipeStep]) -> list[RecipeStep]:
    """Each step once, by what it does (stacked exports approved one by one repeat the same fix)."""
    seen: set[str] = set()
    out: list[RecipeStep] = []
    for step in steps:
        key = digest(
            {
                "kind": step.kind.value,
                "column": step.column,
                "new_column": step.new_column,
                "params": step.params,
            }
        )
        if key not in seen:
            seen.add(key)
            out.append(step)
    return out


def _recipe(
    steps: Sequence[RecipeStep],
    *,
    use_case_id: str,
    fingerprint: str,
    primary_key: str | None,
    target: str | None,
    snapshot_column: str | None,
    config: UseCaseConfig | None,
    approved_by: str | None,
    created_at: datetime,
    acknowledged_stops: tuple[str, ...] | None = None,
) -> DataRecipe | None:
    from engine.agent.advisor import ordered_steps
    from engine.agent.config import AgentConfig, AgentLevel

    ordered = ordered_steps(_unique_steps(steps))
    if not ordered:
        return None
    hash_ = recipe_hash(ordered)
    agent = AgentConfig() if config is None else config.agent
    levels = tuple(agent.levels)
    if any(s.kind is RecipeStepKind.SQL_QUERY for s in ordered) and AgentLevel.RESHAPE not in levels:
        levels = (*levels, AgentLevel.RESHAPE)  # a query makes a new table: the reshape level
    return DataRecipe(
        recipe_id=f"rc-{hashlib.sha256(hash_.encode()).hexdigest()[:16]}",
        use_case_id=use_case_id,
        source_fingerprint=fingerprint,
        steps=ordered,
        recipe_hash=hash_,
        primary_key=primary_key,
        target=target if target != primary_key else None,
        snapshot_column=snapshot_column,
        approved_by=approved_by,
        levels=levels,
        max_failure_pct=agent.max_conversion_failure_pct,
        acknowledged_stops=acknowledged_stops,
        created_at=created_at,
    )


def slot_name(text: str) -> str:
    return (_SLOT.sub("_", text).strip("_") or "file")[:128]


def case_tables(frames: Mapping[str, pd.DataFrame], parts: Sequence[FilePart]) -> dict[str, pd.DataFrame]:
    """The prepared files a `sql_query` step may read: each under its slot's name, and under the name of each
    file it was approved on (a query names a table by its file's name, and next month's file fills the slot).
    """
    tables = dict(frames)
    for part in parts:
        frame = frames.get(part.slot)
        if frame is not None:
            for name in part.file_names:
                tables.setdefault(slot_name(Path(name).stem), frame)
    return tables


def _slot_tables(
    files: Sequence[LoadedFile],
    upload_ids: Sequence[str],
    prepared_by_slot: Mapping[str, pd.DataFrame],
    parts: Sequence[FilePart],
) -> dict[str, pd.DataFrame]:
    """What a file's query may read besides its own rows: every other file by its name (as uploaded), and a slot
    already prepared as it was prepared (:func:`case_tables`)."""
    from engine.agent.dataset_recipe import consistent_types

    tables = {
        slot_name(Path(f.file_name).stem): consistent_types(f.frame)
        for f in files
        if f.upload_id not in upload_ids and f.file_name
    }
    tables.update(case_tables(prepared_by_slot, parts))
    return tables


def _off_own_table(step: RecipeStep) -> RecipeStep:
    """A query step that runs after a build, or beside other files, reads each file by name from the prepared
    tables the run gives it: the one-file shortcut (`this_table`, the working frame under the file's name) no longer
    holds there, since the working frame is then the built table or another file."""
    if step.kind is not RecipeStepKind.SQL_QUERY or "this_table" not in step.params:
        return step
    params = {k: v for k, v in step.params.items() if k not in ("this_table", "tried_after")}
    return step.model_copy(update={"params": params})


def _slots(case: Case, build: BuildRecord | None) -> list[tuple[str, tuple[str, ...]]]:
    """Each file slot and its uploads: a build's sources first (a stacked source is one slot), then the rest."""
    taken: set[str] = set()
    out: list[tuple[str, tuple[str, ...]]] = []
    placed: set[str] = set()
    if build is not None:
        for source in (build.plan.entity, *build.plan.events):
            name = slot_name(source.source_name)
            ids = tuple(u for u in source.upload_ids if u not in placed)
            if not ids or name in taken:
                continue
            taken.add(name)
            placed.update(ids)
            out.append((name, ids))
    for file in case.files:
        if file.upload_id in placed:
            continue
        stem = Path(file.file_name).stem if file.file_name else ""
        base = slot_name(file.role or stem or file.upload_id)
        name, n = base, 2
        while name in taken:
            name, n = f"{base}_{n}", n + 1
        taken.add(name)
        placed.add(file.upload_id)
        out.append((name, (file.upload_id,)))
    return out


def _intake(storage: Storage, case: Case, upload_ids: Sequence[str]) -> IntakeDecision | None:
    for file in case.files:
        if file.upload_id in upload_ids and file.intake_key:
            try:
                if storage.exists(file.intake_key):
                    return IntakeDecision.from_report(storage.read_model(file.intake_key, IntakeReport))
            except (StorageError, ValidationError, ValueError):
                return None
    return None


def _outcome(case: Case, items: Sequence[CaseItem]) -> OutcomePart | None:
    for item in items:
        raw = item.payload.get("definition")
        if not isinstance(raw, dict):
            continue
        try:
            definition = OutcomeDefinition.model_validate(raw)
        except ValidationError:
            continue
        approval: OutcomeApproval | None = None
        given = item.payload.get("approval")
        if isinstance(given, dict):
            try:
                approval = OutcomeApproval.model_validate(given)
            except ValidationError:
                approval = None
        if approval is None:
            wanted = f"outcome-{definition.definition_hash[:16]}"
            recorded = next((a for a in case.approvals if a.approval_id == wanted), None)
            if recorded is not None:
                approval = OutcomeApproval(
                    definition_hash=definition.definition_hash,
                    sentence=recorded.text_shown or "approved",
                    approved_by=recorded.approved_by,
                    approved_at=recorded.approved_at,
                )
        return OutcomePart(definition=definition, approval=approval)
    return None


def _brief_dump(brief: GoalBrief | None, field: str) -> dict[str, JsonValue] | None:
    if brief is None:
        return None
    value = getattr(brief, field, None)
    return None if value is None else dict(value.model_dump(mode="json"))


def _acknowledged(settings: Sequence[SettingChoice]) -> tuple[str, ...]:
    found: list[str] = []
    for setting in settings:
        if setting.path.startswith("validation.acknowledged"):
            values = setting.value if isinstance(setting.value, list) else [setting.value]
            found.extend(str(v) for v in values if v is not None)
    return tuple(sorted(set(found)))


def _section(
    case: Case,
    goal_id: str,
    *,
    active: bool,
    brief_hash: str | None,
    brief: GoalBrief | None,
) -> GoalSection:
    from engine.doctor.plan_check import readiness_acknowledged

    items = _accepted([item for item in case.items if item.goal_id == goal_id])
    roles: dict[str, str] = {}
    for item in items:
        role = _ROLE_KINDS.get(item.kind)
        column = _column(item)
        if role is not None and column is not None:
            roles[role] = column
    settings = tuple(
        SettingChoice(path=path, value=item.payload.get("value"))
        for item in items
        if item.kind == "setting" and isinstance(path := item.payload.get("path"), str)
    )
    return GoalSection(
        goal_id=goal_id,
        active=active,
        brief_hash=brief_hash,
        outcome=_outcome(case, items),
        roles=roles,
        settings=settings,
        split=_brief_dump(brief, "split"),
        time_settings=_brief_dump(brief, "time_settings"),
        # a yes the person gave at APPROVE to a readiness stop goes into the approved prescription with the settings'
        # own, so the file VERIFY called ready is the file the run is let through with (the active goal's only)
        acknowledgements=tuple(
            sorted({*_acknowledged(settings), *(readiness_acknowledged(case.visit) if active else ())})
        ),
        item_digests=tuple(sorted(item.payload_digest for item in items)),
    )


def _build(items: Sequence[CaseItem]) -> BuildPart | None:
    for item in items:
        raw = item.payload.get("build_record")
        if not isinstance(raw, dict):
            continue
        try:
            record = BuildRecord.model_validate(raw)
        except ValidationError:
            continue
        dataset = item.payload.get("dataset_id")
        return BuildPart(record=record, dataset_id=dataset if isinstance(dataset, str) else None)
    return None


def _frames(files: Sequence[LoadedFile], upload_ids: Sequence[str]) -> pd.DataFrame | None:
    found = [f.frame for f in files if f.upload_id in upload_ids]
    if not found:
        return None
    return found[0] if len(found) == 1 else pd.concat(found, ignore_index=True)


def _learn(
    frame: pd.DataFrame | None,
    recipe: DataRecipe | None,
    *,
    primary_key: str | None,
    target: str | None,
    tables: Mapping[str, pd.DataFrame] | None = None,
    guards: list[GuardedRun] | None = None,
    part: str | None = None,
) -> tuple[pd.DataFrame | None, DataContract | None]:
    """The prepared sample and its contract; none when there are no rows to learn from.

    An approved recipe that cannot run on the sample is a :class:`PrescriptionError` (naming `part`), never a missing
    contract nobody is told about: the plan check stops such a step long before approval, so reaching it here is a
    difference between the two worlds that must be seen.

    The recipe runs under the guards (`engine.doctor.guards`): a receipt for each step, the plan's ledger, and the
    plan run twice. The result is added to `guards` when given; the prepared sample is the same as a plain run's.
    """
    from engine.agent.dataset_recipe import consistent_types
    from engine.agent.recipe import RecipeError
    from engine.doctor.guards import run_guarded

    if frame is None:
        return None, None
    prepared = frame
    if recipe is not None:
        try:
            guarded = run_guarded(
                consistent_types(frame),
                recipe.steps,
                primary_key=recipe.primary_key,
                target=recipe.target,
                upload_id="prescription",
                levels=recipe.levels or (),
                max_failure_pct=recipe.max_failure_pct or 0.0,
                snapshot_column=recipe.snapshot_column,
                tables=tables,
                twice=guards is not None,  # the second run only pays off when someone reads the findings
            )
        except RecipeError as exc:
            raise PrescriptionError(exc.code, exc.message, part=part) from exc
        prepared = guarded.run.frame
        if guards is not None:
            guards.append(guarded)
    return prepared, learn_contract(prepared, primary_key=primary_key, target=target)


def _with_outcome(frame: pd.DataFrame, section: GoalSection | None) -> pd.DataFrame | None:
    """The built rows with the approved outcome from a column's values applied (M239), as a training replay applies
    it before the post-build recipe and the contract; the rows as built for any other outcome. None when the approved
    outcome cannot run on them (no contract is learnt from rows a replay would never see)."""
    outcome = None if section is None else section.outcome
    if outcome is None or outcome.definition.kind != "column_values" or outcome.approval is None:
        return frame
    try:
        return apply_outcome_values(frame, outcome.definition, outcome.approval)[0]
    except OutcomeError:
        return None


def _money(
    recipes: Sequence[DataRecipe],
    profile: LocaleProfile | None,
    items: Sequence[CaseItem],
    config: UseCaseConfig | None,
) -> MoneyBasis | None:
    from engine.agent.contracts import RecipeStepKind
    from engine.agent.currency_steps import is_currency_code
    from engine.doctor.currency import ApprovedRate, CurrencyError, money_basis, workspace_currency_for

    steps = [step for recipe in recipes for step in recipe.steps]
    units: dict[str, str] = {}
    if profile is not None and profile.money_units.source == "confirmed":
        units = {c: u for c, u in profile.money_units.value.items() if is_currency_code(u)}
    money = any(
        step.kind is RecipeStepKind.CONVERT_CURRENCY
        or (step.kind is RecipeStepKind.PARSE_NUMBER and "unit" in step.params)
        for step in steps
    )
    if not money and not units:
        return None
    rates: list[ApprovedRate] = []
    for item in items:
        raw = item.payload.get("approved_rate")
        if isinstance(raw, dict):
            try:
                rates.append(ApprovedRate.model_validate(raw))
            except ValidationError:
                continue
    workspace = workspace_currency_for(None if config is None else config.data_doctor, profile).code
    try:
        return money_basis(steps, workspace_currency=workspace, rates=rates, units=units)
    except CurrencyError as exc:
        raise PrescriptionError(exc.code, exc.message, part="locale") from exc


def prescription_from_case(
    case: Case,
    *,
    storage: Storage,
    briefs: Mapping[str, GoalBrief] | None = None,
    files: Sequence[LoadedFile] = (),
    config: UseCaseConfig | None = None,
    built: BuiltRows | None = None,
    guards: list[GuardedRun] | None = None,
) -> Prescription:
    """The prescription a case's approved items make (the design §9.1), goal sections from `CaseItem.goal_id`.

    When `guards` is given, each recipe's run on the sample adds its :class:`~engine.doctor.guards.GuardedRun` to it
    (receipts, ledger and findings, counted by code) for the caller to show; the prescription itself is the same.

    Item payloads read: a recipe step (`step`; `stage: post_build`, or no file, puts it in the post-build recipe), a
    role (`key`, `outcome_column`, `as_of_column`, `consent_column` items' `column`), a setting (`path`, `value`), an
    outcome definition (`definition`, with its `approval` or the case's `outcome-<hash16>` approval), a build
    (`build_record`, M237's `BuildRecord`, and `dataset_id`), an approved rate (`approved_rate`, M242). The base and
    the active goal's section make the recipes; every goal on the case has its section. The contracts are learnt on
    `files` (the rows the visit reads) after their recipes, and on `built` (the built dataset's rows) after the
    post-build recipe, once, here. With one file and no build, the file *is* the built file: a post-build step joins
    its recipe, so a model trained on it carries one recipe and scores exactly as a Guided-setup model does.
    """
    from engine import __version__
    from engine.doctor.combine_run import current_upload
    from engine.doctor.plan_check import acknowledged_codes
    from engine.doctor.visit import active_section

    briefs = dict(briefs or {})
    section_items, _ = active_section(case)
    approved = _accepted(section_items)
    active = next((goal for goal in case.goals if goal.active), None)
    created_at = max((a.approved_at for a in case.approvals), default=case.updated_at)
    approver = case.approvals[-1].approved_by if case.approvals else None
    build = _build(approved)
    acked = acknowledged_codes(case.visit)
    # A decision made on a file goes on the table an approved join or stack made from it (the outcome, the date).
    keys = {
        current_upload(case, item.upload_id): column
        for item in approved
        if item.kind == "key" and item.upload_id and (column := _column(item))
    }
    targets = {
        current_upload(case, item.upload_id): column
        for item in approved
        if item.kind == "outcome_column" and item.upload_id and (column := _column(item))
    }
    as_of = {
        current_upload(case, item.upload_id): column
        for item in approved
        if item.kind == "as_of_column" and item.upload_id and (column := _column(item))
    }
    steps_by_upload: dict[str | None, list[RecipeStep]] = {}
    for item in approved:
        step = _step_of(item)
        if step is None:
            continue
        where = None if item.payload.get("stage") == POST_BUILD else item.upload_id
        steps_by_upload.setdefault(where, []).append(step)
    slots = _slots(case, None if build is None else build.record)
    if build is None and len(slots) == 1 and None in steps_by_upload:
        only = slots[0][1][0]
        steps_by_upload.setdefault(only, []).extend(steps_by_upload.pop(None))
    use_case_id = active.goal_id if active is not None else "data-doctor"
    fingerprints = {f.upload_id: f.fingerprint for f in case.files}
    parts: list[FilePart] = []
    prepared: list[pd.DataFrame] = []
    prepared_by_slot: dict[str, pd.DataFrame] = {}
    for slot, upload_ids in slots:
        first = upload_ids[0]
        key = next((keys[u] for u in upload_ids if u in keys), None)
        target = next((targets[u] for u in upload_ids if u in targets), None)
        key = prepared_key(approved, target, key)
        recipe = _recipe(
            [s for u in upload_ids for s in steps_by_upload.get(u, [])],
            use_case_id=use_case_id,
            fingerprint=fingerprints.get(first) or f"case:{case.case_id}:{first}",
            primary_key=key,
            target=target,
            snapshot_column=next((as_of[u] for u in upload_ids if u in as_of), None),
            config=config,
            approved_by=approver,
            created_at=created_at,
            acknowledged_stops=acked,
        )
        frame, contract = _learn(
            _frames(files, upload_ids),
            recipe,
            primary_key=key,
            target=target,
            guards=guards,
            tables=_slot_tables(files, upload_ids, prepared_by_slot, parts),
            part=slot,
        )
        if frame is not None:
            prepared.append(frame)
            prepared_by_slot[slot] = frame
        parts.append(
            FilePart(
                slot=slot,
                upload_ids=upload_ids,
                file_names=tuple(f.file_name for f in case.files if f.upload_id in upload_ids),
                intake=_intake(storage, case, upload_ids),
                recipe=recipe,
                contract=contract,
            )
        )
    post_build = _recipe(
        [_off_own_table(s) for s in steps_by_upload.get(None, [])],
        use_case_id=use_case_id,
        fingerprint=f"case:{case.case_id}:post_build",
        primary_key=None,
        target=None,
        snapshot_column=None,
        config=config,
        approved_by=approver,
        created_at=created_at,
        acknowledged_stops=acked,
    )
    goals = tuple(
        _section(
            case,
            goal.goal_id,
            active=goal.active,
            brief_hash=goal.brief_hash,
            brief=briefs.get(goal.goal_id),
        )
        for goal in case.goals
    )
    final: DataContract | None = None
    if build is not None and built is not None:
        # Learnt in the order a training replay checks it: the outcome, then the post-build recipe.
        rows = _with_outcome(built.frame, next((g for g in goals if g.active), None))
        _, final = _learn(
            rows,
            post_build,
            primary_key=built.primary_key,
            target=built.target,
            tables=case_tables(prepared_by_slot, parts),
            guards=guards,
            part="post_build",
        )
    profile: LocaleProfile | None = None
    if case.locale_profile is not None:
        try:
            profile = LocaleProfile.model_validate(case.locale_profile)
        except ValidationError:
            profile = None
    recipes = [p.recipe for p in parts if p.recipe is not None] + ([post_build] if post_build else [])
    basis = _money(recipes, profile, approved, config)
    locale = None if profile is None and basis is None else LocalePart(profile=profile, money_basis=basis)
    return make_prescription(
        files=parts,
        build=build,
        locale=locale,
        post_build=post_build,
        contract=final,
        goals=goals,
        provenance=Provenance(
            case_id=case.case_id,
            client_id=case.client_id,
            approved_by=tuple(sorted({a.approved_by for a in case.approvals})),
            receipts=tuple(a.receipt_id for a in case.approvals if a.receipt_id),
            brief_hashes=tuple(g.brief_hash for g in case.goals if g.brief_hash),
            item_digests=tuple(sorted(item.payload_digest for item in approved)),
            engine_version=__version__,
            created_at=created_at,
        ),
    )


@dataclass(frozen=True)
class BuiltRows:
    """The rows of the dataset a case's build made (capped), with its ID and outcome columns."""

    frame: pd.DataFrame
    primary_key: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class CasePrescriber:
    """M235's PRESCRIBE port (`VisitDeps.prescribe`): writes `prescriptions/<hash>.json` and binds the case's files.

    :meth:`digest` is the same hash without writing anything: what M246's approval record carries as
    `prescription_hash` (the approval comes just before PRESCRIBE, on the same items, so the two agree).
    """

    storage: Storage
    load_files: Callable[[Case], Sequence[LoadedFile]] | None = None
    config_root: Path | None = None

    def _briefs(self, case: Case, brief: GoalBrief | None) -> dict[str, GoalBrief]:
        from engine.doctor.goal_brief import GoalBrief as Brief
        from engine.doctor.visit import brief_key

        found: dict[str, GoalBrief] = {}
        for goal in case.goals:
            if brief is not None and goal.goal_id == brief.goal_id:
                found[goal.goal_id] = brief
                continue
            if goal.brief_hash is None:
                continue
            try:
                found[goal.goal_id] = self.storage.read_model(
                    brief_key(case.client_id, case.case_id, goal.goal_id, goal.brief_hash), Brief
                )
            except (StorageError, ValidationError, ValueError):
                continue
        return found

    def _config(self, case: Case) -> UseCaseConfig | None:
        from engine.config import ConfigError, load_use_case

        active = next((goal for goal in case.goals if goal.active), None)
        if active is None:
            return None
        try:
            return load_use_case(active.goal_id, self.config_root)
        except ConfigError:
            return None

    def _built(self, case: Case, config: UseCaseConfig | None) -> BuiltRows | None:
        """The rows of the dataset the case's approved build made, when it was built and can be read."""
        from engine.doctor.visit import active_section
        from engine.onboarding.datasets import DATASET_FRAME_FILENAME, DATASET_MANIFEST_FILENAME, dataset_key
        from engine.onboarding.specs import DatasetManifest
        from engine.stages import ingest

        build = _build(_accepted(active_section(case)[0]))
        if build is None or build.dataset_id is None:
            return None
        try:
            manifest = self.storage.read_model(
                dataset_key(build.dataset_id, DATASET_MANIFEST_FILENAME), DatasetManifest
            )
            cap = min(CONTRACT_ROWS, ingest.profile_row_cap(config) if config is not None else CONTRACT_ROWS)
            frame = ingest.read_upload(
                self.storage,
                dataset_key(build.dataset_id, DATASET_FRAME_FILENAME),
                file_format="parquet",
                row_cap=cap,
            ).frame
        except (StorageError, ValidationError, ValueError, ingest.IngestError):
            return None
        return BuiltRows(
            frame=frame,
            primary_key=str(manifest.primary_key[0]) if manifest.primary_key else None,
            target=manifest.target,
        )

    def prescription(self, case: Case, brief: GoalBrief | None) -> Prescription:
        """The case's prescription; a part that cannot be made (a conversion nobody approved) is the visit's
        `CaseError` with the part's code, so the visit answers it like any other refusal."""
        from engine.doctor.case import CaseError

        files = tuple(self.load_files(case)) if self.load_files is not None else ()
        config = self._config(case)
        try:
            return prescription_from_case(
                case,
                storage=self.storage,
                briefs=self._briefs(case, brief),
                files=files,
                config=config,
                built=self._built(case, config),
            )
        except PrescriptionError as exc:
            raise CaseError(exc.code, exc.message) from exc

    def digest(self, case: Case, brief: GoalBrief | None) -> str:
        """The hash PRESCRIBE will write, without writing anything."""
        return self.prescription(case, brief).prescription_hash

    def __call__(self, case: Case, brief: GoalBrief | None) -> str:
        from engine.doctor.prescription_runs import PrescriptionRef, bind_dataset, bind_spec, bind_upload

        prescription = self.prescription(case, brief)
        save_prescription(self.storage, case.client_id, case.case_id, prescription)
        ref = PrescriptionRef(
            client_id=case.client_id,
            case_id=case.case_id,
            prescription_hash=prescription.prescription_hash,
        )
        for upload_id in case.upload_ids():
            bind_upload(self.storage, upload_id, ref)
        if prescription.build is not None and prescription.build.dataset_id is not None:
            bind_dataset(self.storage, prescription.build.dataset_id, ref)
            spec = self._spec_of(prescription.build.dataset_id)
            if spec is not None:
                # Every dataset a firing builds fresh from the same spec follows this prescription too.
                bind_spec(self.storage, spec[0], spec[1], ref)
        return prescription.prescription_hash

    def _spec_of(self, dataset_id: str) -> tuple[str, str] | None:
        """`(client_id, spec_id)` of the onboarding spec that built `dataset_id`, when its manifest can be read."""
        from engine.onboarding.datasets import DATASET_MANIFEST_FILENAME, dataset_key
        from engine.onboarding.specs import DatasetManifest

        try:
            manifest = self.storage.read_model(
                dataset_key(dataset_id, DATASET_MANIFEST_FILENAME), DatasetManifest
            )
        except (StorageError, ValidationError, ValueError):
            return None
        return manifest.client_id, manifest.spec_id
