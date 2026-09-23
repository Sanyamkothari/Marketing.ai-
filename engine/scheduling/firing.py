"""What a firing does: score, check drift or retrain - through the engine's own run path (M49).

A scheduled firing is the API's work done at a time nobody clicked, so it must do *exactly* what the
API does, not a scheduler's private imitation of it (DEC-766):

* **score** - rebuild a dataset from the client's latest tables through the saved onboarding recipe
  (`engine.onboarding.build.build_dataset`, the function `POST /datasets` submits), register it with
  the client (`ClientStore.register_dataset`, as that route's job body does), then start a scoring
  run against the champion (`engine.stages.score.resolve_model_version`, the one resolver) through
  `start_dataset_run`: the dataset checks, `validate_against_schema`, `engine.runs.create_run`,
  `job_spec_for`, `write_job_spec` and `JobRunner.submit(build_job_fn(...))` - the calls
  `api/routes/runs.py:create_run_endpoint` makes, in its order.
* **retrain** - the same, in training mode: a fresh training dataset and a normal training run. The
  run's register stage applies the champion rule and `governance.approval_required` exactly as for a
  run a person started, so with approval required a retrain yields a `candidate` or a
  `pending_approval` version and **never** a champion. Nothing here promotes or approves anything,
  and the firing acts as `SYSTEM_SCHEDULER`, which could not approve if it tried.
* **drift_check** - take the latest finished scoring run of the client and use case, and compare its
  data with the *champion's* training baseline: reuse the run's own `drift.json` when the champion
  scored it, otherwise re-measure with the champion's recorded preparation (`prepare.replay`) and
  `engine.stages.score.compute_drift` - the same PSI the predict stage writes. Above
  `monitoring.drift_psi_threshold` it raises a `drift_above_threshold` alert, and under
  `monitoring.retraining: on_drift` it starts a retrain (so does an open erasure flag on a model of
  the use case, DEC-768).

**Why `start_dataset_run` exists here and not in `api/routes/runs.py`.** The run path lives inside an
HTTP handler, which raises `HTTPException`s and reads FastAPI dependencies, and the engine may not
import `api` (the job container has no FastAPI). Changing that handler is a cross-branch edit to a
Phase 1 route; instead this module holds the minimal engine-level sequence the handler's dataset
branch performs - the same engine calls, with `FiringError` codes where the handler has HTTP
statuses. The codes are the handler's own (`DATASET_NOT_BUILT`, `DATASET_COMPOSITE_KEY_NOT_WIRED`,
`CHAMPION_NOT_FOUND`, ...), so a failed firing and a refused request say the same thing.

**A firing's lifecycle.** It claims its slot (`ScheduleStore.claim_firing`, DEC-763), runs the kind's
synchronous part - which for score and retrain ends when the run is *submitted* - and is saved as
`running` (with the run id) or `succeeded`/`failed`. `settle` later moves each `running` firing to
`succeeded` or `failed` from its run's `run.json`, raises `scheduled_job_failed` for a failed one,
and clears the erasure flags a successful retrain answered for. The local scheduler settles on every
tick; the CLI settles after waiting for its own run.

**Audit.** Every firing - including each batch of `missed` slots - writes exactly one audit event as
`SYSTEM_SCHEDULER` (`schedules.fire` / `schedules.missed`, object `schedule`), carrying ids and
codes only. A firing started by a person through the API passes `audit_log=None`: the API's
middleware already writes the one event that request is allowed, and the route enriches it with
`firing_audit_details` (the one-event-per-request rule of M47).
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.access.roles import SYSTEM_SCHEDULER, Principal
from engine.audit.events import AuditEvent, AuditLog
from engine.clients import ClientStore, ClientStoreError
from engine.config import (
    Catalog,
    ConfigError,
    ResolvedConfig,
    Retraining,
    RunMode,
    UseCaseConfig,
    get_catalog,
    resolve_config,
)
from engine.contracts import (
    DriftBaseline,
    DriftReport,
    FeatureSchema,
    ModelVersion,
    RunRecord,
    RunState,
)
from engine.jobs import CancelToken, JobRunner, ReconcilingJobRunner
from engine.onboarding.datasets import (
    DATASET_FRAME_FILENAME,
    DATASET_MANIFEST_FILENAME,
    DatasetError,
    LocalDatasetRegistry,
    dataset_key,
)
from engine.onboarding.specs import DatasetManifest, MappingSpec, OnboardingSpec, SourceSpec
from engine.pipeline import Pipeline
from engine.registry import ModelRegistry, RegistryError, to_utc
from engine.runs import (
    RUN_FILENAME,
    DatasetLineage,
    build_job_fn,
    create_run,
    job_spec_for,
    job_spec_key,
    read_job_spec,
    write_job_spec,
)
from engine.scheduling.alerts import AlertKind, AlertSink, new_alert
from engine.scheduling.retraining import NO_RETRAIN_FLAGS, RetrainFlags, flagged_for_use_case
from engine.scheduling.scheduler import Clock
from engine.scheduling.schedules import (
    MAX_MISSED_RECORDED,
    DueSlots,
    FiringStatus,
    FiringTrigger,
    Schedule,
    ScheduleError,
    ScheduleFiring,
    ScheduleKind,
    ScheduleStore,
    due_slots,
    new_firing_id,
)
from engine.stages import ingest, validate
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

__all__ = [
    "ABANDONED_AFTER",
    "DRIFT_CHECK_DIRECTORY",
    "DriftCheckReport",
    "FiringError",
    "FiringServices",
    "RecipeInputs",
    "ScheduleFirer",
    "build_dataset_from_spec",
    "drift_against_champion",
    "fire",
    "firing_audit_details",
    "latest_recipe_inputs",
    "latest_scored_run",
    "load_built_dataset",
    "start_dataset_run",
]

_LOGGER = get_logger(__name__)

EVENTBRIDGE_GRACE: Final[timedelta] = timedelta(minutes=30)
"""How late EventBridge may deliver a slot before a sweep, or a later slot's delivery, calls it missed."""

SCHEDULED_TRAINING_OVERRIDES: Final[dict[str, bool]] = {"governance.approval_required": True}
"""What every scheduler-started training run is resolved with: its challenger waits for an Approver."""

ABANDONED_AFTER: Final[timedelta] = timedelta(hours=6)
"""A firing still `running` with no run after this long died mid-build; `settle` fails it."""

DRIFT_CHECK_DIRECTORY: Final[str] = "drift_checks"
"""`runs/<scoring run>/drift_checks/<firing id>.json`: a drift check's report sits with the data it read."""

_KIND_LABEL: Final[dict[ScheduleKind, str]] = {
    ScheduleKind.SCORE: "scoring",
    ScheduleKind.DRIFT_CHECK: "drift check",
    ScheduleKind.RETRAIN: "retraining",
}

_TERMINAL_RUN_STATES: Final[frozenset[RunState]] = frozenset(
    {RunState.DONE, RunState.FAILED, RunState.CANCELLED}
)


class FiringError(Exception):
    """A firing could not do its work. `code` is a short token (the engine's own where one exists),
    `message` a sentence a person can act on; `dataset_id` names a dataset the firing did build."""

    def __init__(self, code: str, message: str, *, dataset_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.dataset_id = dataset_id


@dataclass(frozen=True)
class FiringServices:
    """Everything a firing touches, injected: the API's own services, or the CLI's."""

    store: ScheduleStore
    storage: Storage
    registry: ModelRegistry
    jobs: JobRunner
    config_root: Path
    alerts: AlertSink
    audit_log: AuditLog | None
    """`None` when the caller audits the firing itself (the API's middleware, for "fire now")."""
    client_store: ClientStore | None = None
    retrain_flags: RetrainFlags = NO_RETRAIN_FLAGS
    clock: Clock = utc_now
    job_client_tag: str | None = None
    """`Settings.client_id`: the `client` cost-allocation tag every job carries (DEC-324)."""
    principal: Principal = SYSTEM_SCHEDULER


@dataclass(frozen=True, slots=True)
class _Outcome:
    """The synchronous result of one kind's work."""

    status: FiringStatus
    result_code: str
    run_id: str | None = None
    dataset_id: str | None = None
    flagged_models: tuple[str, ...] = ()


class DriftCheckReport(BaseModel):
    """`runs/<scoring run>/drift_checks/<firing id>.json` - one drift check's verdict (stable schema)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Bumped only on a breaking change.")
    firing_id: str = Field(description="The firing that ran the check.")
    schedule_id: str = Field(description="Its schedule.")
    use_case_id: str = Field(description="Use case checked.")
    client_id: str | None = Field(description="Client checked, if any.")
    scoring_run_id: str = Field(description="The latest finished scoring run, whose data was compared.")
    champion_model_id: str = Field(description="The champion whose training baseline it was compared with.")
    reused_run_drift: bool = Field(
        description="True when the run's own drift.json was measured against this champion and reused."
    )
    drift: DriftReport = Field(description="PSI per feature against the champion's baseline.")
    above_threshold: bool = Field(description="Whether any feature reached monitoring.drift_psi_threshold.")
    flagged_models: tuple[str, ...] = Field(description="Erasure-flagged versions of this use case.")
    retrain_run_id: str | None = Field(description="Training run started by this check, if any.")
    checked_at: datetime = Field(description="UTC time of the check.")


# ---------------------------------------------------------------------------
# The engine-level run path (the dataset branch of POST /runs)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _DatasetUpload:
    """A built dataset seen through `engine.runs.UploadInfo`, as `api.routes.runs._DatasetSource` is."""

    upload_id: str
    file_name: str
    source_key: str
    file_format: Literal["csv", "parquet"] = "parquet"


def load_built_dataset(
    storage: Storage, dataset_id: str, *, config: UseCaseConfig, client_id: str | None
) -> DatasetManifest:
    """A dataset a run can read, or `FiringError` with the code `POST /runs` would answer."""
    try:
        manifest = storage.read_model(dataset_key(dataset_id, DATASET_MANIFEST_FILENAME), DatasetManifest)
    except StorageError as exc:
        raise FiringError(
            "DATASET_NOT_FOUND", f"No dataset with id {dataset_id!r}. Build one from your tables first."
        ) from exc
    if not storage.exists(dataset_key(dataset_id, DATASET_FRAME_FILENAME)):
        raise FiringError(
            "DATASET_NOT_BUILT",
            f"Dataset {dataset_id!r} has a manifest but no rows: its build did not finish.",
            dataset_id=dataset_id,
        )
    if manifest.use_case != config.id:
        raise FiringError(
            "DATASET_USE_CASE_MISMATCH",
            f"Dataset {dataset_id!r} was built for {manifest.use_case!r}, not {config.id!r}.",
            dataset_id=dataset_id,
        )
    if client_id is not None and manifest.client_id != client_id:
        raise FiringError(
            "DATASET_CLIENT_MISMATCH",
            f"Dataset {dataset_id!r} belongs to another client.",
            dataset_id=dataset_id,
        )
    if len(manifest.primary_key) > 1:
        raise FiringError(
            "DATASET_COMPOSITE_KEY_NOT_WIRED",
            f"Dataset {dataset_id!r} has one row per {' and '.join(manifest.primary_key)}, and a run "
            "still takes a single key column. Save the recipe with a single snapshot to schedule it.",
            dataset_id=dataset_id,
        )
    return manifest


def _row_cap(config: UseCaseConfig) -> int:
    """`api.routes.uploads.profile_row_cap`, read the same defensive way."""
    return int(getattr(config.validation, "profile_row_cap", ingest.DEFAULT_PROFILE_ROW_CAP))


def start_dataset_run(
    *,
    storage: Storage,
    registry: ModelRegistry,
    jobs: JobRunner,
    resolved: ResolvedConfig,
    catalog: Catalog,
    manifest: DatasetManifest,
    mode: RunMode,
    version: ModelVersion | None,
    client_tag: str | None,
    now: datetime | None = None,
    requested_by: str | None = None,
) -> RunRecord:
    """Validate a built dataset, write the run directory and its job spec, and submit the job.

    The dataset branch of `api.routes.runs.create_run_endpoint`, call for call, minus HTTP. `version`
    is the resolved scoring model (pinned on `run.json`, as the route pins it) and `None` for a
    training run. A blocking validation error is `FiringError(VALIDATION_FAILED)`; the report is not
    written anywhere, exactly as the route writes a dataset's report nowhere but its `409`.
    """
    config = resolved.config
    dataset_id = manifest.dataset_id
    frame_key = dataset_key(dataset_id, DATASET_FRAME_FILENAME)
    primary_key = str(manifest.primary_key[0])
    target = manifest.target
    try:
        read = ingest.read_upload(storage, frame_key, file_format="parquet")
        profile = ingest.profile_dataset(
            read.frame,
            config,
            upload_id=dataset_id,
            file_name=f"{dataset_id}.parquet",
            file_format="parquet",
            file_size_bytes=storage.size_bytes(frame_key),
            delimiter=None,
            encoding=read.encoding,
            row_count=read.row_count,
            fingerprint=read.fingerprint,
        )
        capped = ingest.read_upload(storage, frame_key, file_format="parquet", row_cap=_row_cap(config)).frame
    except ingest.IngestError as exc:
        raise FiringError(exc.code, exc.message, dataset_id=dataset_id) from exc
    if mode is RunMode.TRAIN:
        report = validate.validate_for_training(
            capped,
            config,
            primary_key=primary_key,
            target=target or "",
            acknowledged=config.validation.acknowledged,
            upload_id=dataset_id,
            row_count=profile.row_count,
        )
    else:
        if version is None:
            raise ValueError("a scoring run needs the model version it scores with")
        report = validate.validate_against_schema(
            capped,
            storage.read_model(version.schema_key, FeatureSchema),
            primary_key=primary_key,
            config=config,
            acknowledged=config.validation.acknowledged,
            upload_id=dataset_id,
            row_count=profile.row_count,
        )
    if not report.passed:
        count = report.error_count
        raise FiringError(
            "VALIDATION_FAILED",
            f"{count} problem{'s' if count != 1 else ''} must be fixed before dataset {dataset_id} can be used.",
            dataset_id=dataset_id,
        )
    source = _DatasetUpload(upload_id=dataset_id, file_name=f"{dataset_id} (built)", source_key=frame_key)
    record = create_run(
        storage,
        Pipeline(storage, registry, jobs),
        resolved=resolved,
        catalog=catalog,
        upload=source,
        dataset=DatasetLineage(
            dataset_id=dataset_id, client_id=manifest.client_id, fingerprint=manifest.fingerprint.hash
        ),
        profile=profile,
        report=report,
        mode=mode,
        primary_key=primary_key,
        target=target,
        model_choice=catalog.automl_choice.value,
        model_version_id=None if version is None else version.model_id,
        now=now,
        requested_by=requested_by,
    )
    spec = job_spec_for(record, upload=source, client_id=client_tag)
    write_job_spec(storage, spec)
    jobs.submit(spec.job_id, build_job_fn(spec, storage=storage, registry=registry))
    _LOGGER.info(
        "schedule.run_started run_id=%s mode=%s dataset_id=%s", record.run_id, mode.value, dataset_id
    )
    return record


# ---------------------------------------------------------------------------
# Rebuilding a recipe against the latest tables
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RecipeInputs:
    """A recipe re-pointed at the newest matching tables, and what it will read."""

    spec: OnboardingSpec
    sources: tuple[SourceSpec, ...]
    mappings: tuple[MappingSpec, ...]
    rebound: tuple[str, ...]
    """Source ids of the recipe that were replaced by a newer table."""


def latest_recipe_inputs(client_store: ClientStore, spec: OnboardingSpec) -> RecipeInputs:
    """Each of the recipe's tables replaced by the client's newest table of the same role (DEC-766).

    "Monthly scoring on new tables through the saved recipe" needs a rule for which tables are new.
    A table qualifies when it has the same confirmed role as the one it replaces, carries every
    column that table's mapping reads, and was uploaded later; the newest qualifying one wins, and
    its mapping is the saved mapping re-pointed at it (the mapping describes columns, not a file).
    When a recipe maps two tables to the same role the tables are ambiguous, and both are kept as
    saved rather than guessed at. With no newer table the saved one is read again, which scores the
    same data again - an honest result, not an error.
    """
    try:
        mappings = [client_store.get_mapping(mapping_id) for mapping_id in spec.mapping_ids]
        role_counts = Counter(mapping.role for mapping in mappings)
        candidates = client_store.list_sources(spec.client_id)
        replacements: dict[str, str] = {}
        rebound_mappings: list[MappingSpec] = []
        for mapping in mappings:
            original = client_store.get_source(mapping.source_id)
            chosen = original
            if role_counts[mapping.role] == 1:
                needed = {column.source for column in mapping.columns}
                newer = [
                    source
                    for source in candidates
                    if source.role == mapping.role
                    and needed <= set(source.columns)
                    and (to_utc(source.created_at), source.source_id)
                    > (to_utc(original.created_at), original.source_id)
                ]
                if newer:
                    chosen = max(newer, key=lambda source: (to_utc(source.created_at), source.source_id))
            if chosen.source_id != original.source_id:
                replacements[original.source_id] = chosen.source_id
                mapping = mapping.model_copy(update={"source_id": chosen.source_id}).with_hash()
            rebound_mappings.append(mapping)
        source_ids = (spec.entity_source_id, *spec.event_source_ids)
        sources = tuple(client_store.get_source(replacements.get(sid, sid)) for sid in source_ids)
    except ClientStoreError as exc:
        raise FiringError(
            "ONBOARDING_INPUT_MISSING",
            "A table or mapping this recipe names no longer exists. Open the recipe and save it again.",
        ) from exc
    rebound_spec = spec
    if replacements:
        rebound_spec = spec.model_copy(
            update={
                "entity_source_id": replacements.get(spec.entity_source_id, spec.entity_source_id),
                "event_source_ids": tuple(
                    sorted(replacements.get(sid, sid) for sid in spec.event_source_ids)
                ),
            }
        )
    return RecipeInputs(
        spec=rebound_spec,
        sources=sources,
        mappings=tuple(rebound_mappings),
        rebound=tuple(sorted(replacements)),
    )


def build_dataset_from_spec(
    spec: OnboardingSpec,
    *,
    config: UseCaseConfig,
    mode: RunMode,
    client_store: ClientStore,
    storage: Storage,
    cancel: CancelToken | None = None,
) -> DatasetManifest:
    """Build a fresh dataset from `spec` on the latest tables, register it, and return its manifest.

    `build_dataset` writes `datasets/<id>/` itself, as it does for `POST /datasets`; a build whose
    checks found a blocking problem writes its report and no rows, and is `DATASET_CHECKS_FAILED`
    here with the dataset id, so the firing's history links to the report that says why.
    """
    from engine.onboarding import build
    from engine.onboarding.sources import FileSourceReader

    inputs = latest_recipe_inputs(client_store, spec)
    registry = LocalDatasetRegistry(storage)
    dataset_id = registry.new_dataset_id(spec.client_id, spec.use_case)
    try:
        report = build.build_dataset(
            spec=inputs.spec,
            config=config,
            sources=inputs.sources,
            mappings=inputs.mappings,
            reader=FileSourceReader(storage, config),
            registry=registry,
            dataset_id=dataset_id,
            mode=mode,
            cancel=cancel or CancelToken(),
            # Ruling R1 (DEC-871): a recipe never built for this client gets the full leak check.
            first_build_of_recipe=build.is_first_build_of_recipe(
                inputs.spec,
                inputs.mappings,
                client_store.list_datasets(spec.client_id, spec.use_case),
            ),
        )
        if not report.passed:
            raise FiringError(
                "DATASET_CHECKS_FAILED",
                f"The scheduled build of dataset {dataset_id} found problems that must be fixed first. "
                "Open its build report to see them.",
                dataset_id=dataset_id,
            )
        manifest = registry.read_manifest(dataset_id)
        client_store.register_dataset(manifest)
    except (DatasetError, ClientStoreError) as exc:
        raise FiringError(exc.code, exc.message, dataset_id=dataset_id) from exc
    _LOGGER.info(
        "schedule.dataset_built dataset_id=%s mode=%s rows=%d rebound_sources=%d",
        dataset_id,
        mode.value,
        manifest.n_rows,
        len(inputs.rebound),
    )
    return manifest


# ---------------------------------------------------------------------------
# Drift against the champion
# ---------------------------------------------------------------------------
def latest_scored_run(storage: Storage, use_case_id: str, client_id: str | None) -> RunRecord | None:
    """The newest scoring run of this use case (and client, when given) that finished."""
    for key in sorted(storage.list_keys("runs/"), reverse=True):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        try:
            record = storage.read_model(key, RunRecord)
        except (StorageError, ValueError):
            continue
        if record.mode is not RunMode.SCORE or record.state is not RunState.DONE:
            continue
        if record.use_case_id != use_case_id:
            continue
        if client_id is not None and record.client_id != client_id:
            continue
        return record
    return None


def drift_against_champion(
    storage: Storage, run: RunRecord, champion: ModelVersion, config: UseCaseConfig
) -> tuple[DriftReport | None, bool]:
    """`(drift, reused)`: the run's data against the champion's training baseline.

    Reused from the run's `drift.json` when the champion is the version that scored it; otherwise
    re-measured on the run's source with the champion's own recorded preparation. `None` when drift
    cannot be measured (no stored baseline, no preparation record, or no comparable features) - the
    same "not measured" the predict stage reports, never an invented zero.
    """
    from engine.stages.prepare import replay
    from engine.stages.score import ScoreError, _prepare_report, compute_drift

    if run.model_version_id == champion.model_id:
        drift_key = run_key(run.run_id, "drift.json")
        if storage.exists(drift_key):
            return storage.read_model(drift_key, DriftReport), True
    if champion.drift_baseline_key is None or not storage.exists(champion.drift_baseline_key):
        return None, False
    try:
        spec = read_job_spec(storage, job_spec_key(run.run_id))
        frame = ingest.read_upload(storage, spec.upload_key, file_format=spec.upload_format).frame
        prepared = replay(frame, _prepare_report(champion, storage=storage))
    except (StorageError, ScoreError, ingest.IngestError) as exc:
        log_failure(_LOGGER, f"schedule.drift_unmeasured run_id={run.run_id}", exc)
        return None, False
    baseline = storage.read_model(champion.drift_baseline_key, DriftBaseline)
    return compute_drift(baseline, prepared, config, run_id=run.run_id), False


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def firing_audit_details(firing: ScheduleFiring) -> dict[str, str | int | float | bool | None]:
    """The `details` of a firing's audit event: ids, the trigger and the codes - never a value."""
    return {
        "schedule_id": firing.schedule_id,
        "firing_id": firing.firing_id,
        "schedule_kind": firing.kind.value,
        "use_case_id": firing.use_case_id,
        "client_id": firing.client_id,
        "trigger": firing.trigger.value,
        "outcome": firing.status.value,
        "run_id": firing.run_id,
        "dataset_id": firing.dataset_id,
        "reason_code": firing.error_code or firing.result_code,
        "models_flagged": len(firing.flagged_models),
    }


# ---------------------------------------------------------------------------
# The firer
# ---------------------------------------------------------------------------
class ScheduleFirer:
    """Fires schedules, records missed slots and settles running firings. The local scheduler's
    `Firer`, the CLI's engine and the API's "fire now" - one class, so the three cannot drift."""

    def __init__(self, services: FiringServices) -> None:
        self.services = services

    # --- one firing ----------------------------------------------------------------------------------
    def fire(
        self,
        schedule: Schedule,
        *,
        trigger: FiringTrigger = FiringTrigger.MANUAL,
        scheduled_for: datetime | None = None,
    ) -> ScheduleFiring | None:
        """Run the schedule's work once. `None` when this slot was already claimed (DEC-763)."""
        services = self.services
        now = services.clock()
        claimed = services.store.claim_firing(
            ScheduleFiring(
                firing_id=new_firing_id(now),
                schedule_id=schedule.schedule_id,
                client_id=schedule.client_id,
                use_case_id=schedule.use_case_id,
                kind=schedule.kind,
                trigger=trigger,
                status=FiringStatus.RUNNING,
                scheduled_for=None if scheduled_for is None else to_utc(scheduled_for),
                fired_at=now,
            )
        )
        if claimed is None:
            _LOGGER.info(
                "schedule.fire skipped schedule_id=%s reason=slot_already_claimed", schedule.schedule_id
            )
            return None
        try:
            outcome = self._execute(schedule, claimed)
        except Exception as exc:  # every failure becomes a failed firing and an alert, never a crash
            code = _error_code(exc)
            log_failure(_LOGGER, f"schedule.fire schedule_id={schedule.schedule_id} code={code}", exc)
            finished = claimed.model_copy(
                update={
                    "status": FiringStatus.FAILED,
                    "error_code": code,
                    "dataset_id": getattr(exc, "dataset_id", None),
                    "finished_at": services.clock(),
                }
            )
            services.store.save_firing(finished)
            self._alert_failed(finished)
        else:
            finished = claimed.model_copy(
                update={
                    "status": outcome.status,
                    "result_code": outcome.result_code,
                    "run_id": outcome.run_id,
                    "dataset_id": outcome.dataset_id,
                    "flagged_models": outcome.flagged_models,
                    "finished_at": services.clock() if outcome.status is FiringStatus.SUCCEEDED else None,
                }
            )
            services.store.save_firing(finished)
        self._touch(schedule.schedule_id, last_fired_at=now)
        self._audit("schedules.fire", finished.schedule_id, finished)
        _LOGGER.info(
            "schedule.fired schedule_id=%s firing_id=%s kind=%s trigger=%s status=%s",
            finished.schedule_id,
            finished.firing_id,
            finished.kind.value,
            finished.trigger.value,
            finished.status.value,
        )
        return finished

    def fire_scheduled(
        self, schedule_id: str, *, scheduled_for: datetime | None, grace: timedelta = EVENTBRIDGE_GRACE
    ) -> ScheduleFiring | None:
        """What an EventBridge invocation runs: fire `scheduled_for` and move `next_due_at` past it.

        Without a slot (somebody ran the task by hand) it is a manual firing. A disabled schedule
        fires nothing - EventBridge holds it disabled too, so this only covers a race with the switch.

        Slots between `next_due_at` and this one were never delivered (DEC-775): they are recorded as
        missed here, *before* `next_due_at` moves past them, because once it has moved no sweep can
        see them. A skipped slot still inside `grace` may yet arrive (EventBridge retries), so it is
        left for the sweep and `next_due_at` stops at it.
        """
        schedule = self.services.store.get(schedule_id)
        if not schedule.enabled:
            _LOGGER.info("schedule.fire skipped schedule_id=%s reason=disabled", schedule_id)
            return None
        if scheduled_for is None:
            return self.fire(schedule, trigger=FiringTrigger.MANUAL)
        slot = to_utc(scheduled_for)
        following = schedule.next_slot_after(slot)
        skipped: tuple[datetime, ...] = ()
        if schedule.next_due_at is not None and to_utc(schedule.next_due_at) < slot:
            skipped = schedule.expression.slots_between(
                to_utc(schedule.next_due_at),
                slot - timedelta(microseconds=1),
                timezone=schedule.timezone,
                limit=MAX_MISSED_RECORDED,
            )
        now = self.services.clock()
        undelivered = tuple(item for item in skipped if now - item > grace)
        pending = [item for item in skipped if now - item <= grace]
        if pending:
            self._touch(schedule_id, next_due_at=min(pending))
        elif schedule.next_due_at is None or (
            following is not None and following > to_utc(schedule.next_due_at)
        ):
            self._touch(schedule_id, next_due_at=following)
        if undelivered:
            self.record_missed(schedule, undelivered)
        return self.fire(schedule, trigger=FiringTrigger.SCHEDULED, scheduled_for=slot)

    # --- due slots and missed ones (DEC-764) ---------------------------------------------------------
    def run_due(self, schedule: Schedule, *, now: datetime, grace: timedelta) -> tuple[ScheduleFiring, ...]:
        """Fire the due slot, or record the missed ones and run one catch-up."""
        return self._due(schedule, now=now, grace=grace, fire_on_time=True)

    def sweep_missed(self, *, now: datetime, grace: timedelta) -> tuple[ScheduleFiring, ...]:
        """Record every enabled schedule's missed slots and run each one catch-up.

        For deployments where EventBridge fires: a slot still inside its grace period is left alone
        (EventBridge is about to fire it, or is firing it now), so the sweep never races the target.
        """
        produced: list[ScheduleFiring] = []
        for schedule in self.services.store.list():
            if schedule.enabled:
                produced.extend(self._due(schedule, now=now, grace=grace, fire_on_time=False))
        return tuple(produced)

    def _due(
        self, schedule: Schedule, *, now: datetime, grace: timedelta, fire_on_time: bool
    ) -> tuple[ScheduleFiring, ...]:
        if schedule.next_due_at is None:
            if schedule.enabled:
                self._touch(schedule.schedule_id, next_due_at=schedule.next_slot_after(now))
            return ()
        decision = due_slots(schedule, now, grace=grace)
        if not fire_on_time:
            return self._sweep_one(schedule, decision, now=now, grace=grace)
        if decision.next_due_at != schedule.next_due_at:
            # Moved on *before* the work, so a crash half-way through cannot make the slot due again;
            # the slot's claim is what stops a second process, this is what stops the next tick.
            self._touch(schedule.schedule_id, next_due_at=decision.next_due_at)
        produced: list[ScheduleFiring] = []
        if decision.on_time is not None:
            fired = self.fire(schedule, trigger=FiringTrigger.SCHEDULED, scheduled_for=decision.on_time)
            if fired is not None:
                produced.append(fired)
        if decision.missed:
            recorded = self.record_missed(schedule, decision.missed, truncated=decision.missed_truncated)
            produced.extend(recorded)
            # Only the process that claimed the newest missed slot catches up: two processes can
            # each win some of the slots, but exactly one wins the newest (DEC-779).
            if decision.catch_up and _claimed_newest(recorded, decision.missed):
                caught_up = self.fire(schedule, trigger=FiringTrigger.CATCH_UP)
                if caught_up is not None:
                    produced.append(caught_up)
        return tuple(produced)

    def _sweep_one(
        self, schedule: Schedule, decision: DueSlots, *, now: datetime, grace: timedelta
    ) -> tuple[ScheduleFiring, ...]:
        """The sweep's half of `_due`: only slots older than the grace are missed.

        A slot inside the grace belongs to EventBridge, which is firing it or about to: recording it
        as missed would claim it, and the real invocation would then find it taken and do nothing.
        So `next_due_at` stops at the oldest such slot (the invocation moves it on), and no catch-up
        runs beside it - that invocation *is* the catch-up.
        """
        due = [*decision.missed, *([decision.on_time] if decision.on_time is not None else [])]
        old = tuple(slot for slot in due if now - slot > grace)
        if not old:
            return ()
        recent = [slot for slot in due if now - slot <= grace]
        self._touch(schedule.schedule_id, next_due_at=min(recent) if recent else decision.next_due_at)
        recorded = self.record_missed(schedule, old, truncated=decision.missed_truncated)
        produced = list(recorded)
        if _claimed_newest(recorded, old) and not recent:
            caught_up = self.fire(schedule, trigger=FiringTrigger.CATCH_UP)
            if caught_up is not None:
                produced.append(caught_up)
        return tuple(produced)

    def record_missed(
        self, schedule: Schedule, slots: Sequence[datetime], *, truncated: bool = False
    ) -> tuple[ScheduleFiring, ...]:
        """One `missed` firing per slot not already claimed, one alert and one audit event for them all.

        The alert is raised only by the process that claimed the newest of `slots` - the one that
        also runs the catch-up - and counts every slot in `slots`, so two processes sweeping the same
        backlog send one alert, not one each (DEC-779). The audit event records what this process
        claimed.
        """
        services = self.services
        now = services.clock()
        recorded: list[ScheduleFiring] = []
        for slot in slots:
            missed = services.store.claim_firing(
                ScheduleFiring(
                    firing_id=new_firing_id(now),
                    schedule_id=schedule.schedule_id,
                    client_id=schedule.client_id,
                    use_case_id=schedule.use_case_id,
                    kind=schedule.kind,
                    trigger=FiringTrigger.SCHEDULED,
                    status=FiringStatus.MISSED,
                    scheduled_for=to_utc(slot),
                    error_code="SCHEDULE_MISSED",
                    fired_at=now,
                    finished_at=now,
                )
            )
            if missed is not None:
                recorded.append(missed)
        if not recorded:
            return ()
        self._audit("schedules.missed", schedule.schedule_id, recorded[-1], count=len(recorded))
        if not _claimed_newest(recorded, slots):
            return tuple(recorded)
        count = f"{len(slots)}{' or more' if truncated else ''}"
        services.alerts.raise_alert(
            new_alert(
                AlertKind.SCHEDULE_MISSED,
                use_case_id=schedule.use_case_id,
                client_id=schedule.client_id,
                schedule_id=schedule.schedule_id,
                message=(
                    f"The {_KIND_LABEL[schedule.kind]} schedule for {schedule.use_case_id} missed {count} "
                    "run(s) while the scheduler was not running. It has been run once now to catch up; "
                    "check the schedule's history if the missed runs matter."
                ),
                now=now,
            )
        )
        return tuple(recorded)

    # --- settling running firings -------------------------------------------------------------------
    def settle(self) -> tuple[ScheduleFiring, ...]:
        """Move each `running` firing whose run has ended to `succeeded` or `failed`."""
        settled: list[ScheduleFiring] = []
        for firing in self.services.store.list_firings(status=FiringStatus.RUNNING, limit=500):
            finished = self._settle_one(firing)
            if finished is not None:
                settled.append(finished)
        return tuple(settled)

    def _settle_one(self, firing: ScheduleFiring) -> ScheduleFiring | None:
        services = self.services
        now = services.clock()
        if firing.run_id is None:
            if now - to_utc(firing.fired_at) < ABANDONED_AFTER:
                return None
            return self._fail(firing, "FIRING_ABANDONED", now)
        if isinstance(services.jobs, ReconcilingJobRunner):
            try:
                services.jobs.reconcile(firing.run_id)
            except Exception as exc:  # a control plane that cannot answer leaves the documents as they are
                log_failure(_LOGGER, "schedule.settle reconcile", exc)
        try:
            record = services.storage.read_model(run_key(firing.run_id, RUN_FILENAME), RunRecord)
        except StorageError:
            return self._fail(firing, "RUN_NOT_FOUND", now)
        if record.state not in _TERMINAL_RUN_STATES:
            return None
        if record.state is not RunState.DONE:
            code = record.error.code if record.error is not None else f"RUN_{record.state.value.upper()}"
            return self._fail(firing, code, now)
        result_code = "SCORED"
        if record.mode is RunMode.TRAIN:
            result_code = self._trained(firing, record)
        finished = firing.model_copy(
            update={"status": FiringStatus.SUCCEEDED, "result_code": result_code, "finished_at": now}
        )
        return finished if services.store.settle_firing(finished) else None

    def _trained(self, firing: ScheduleFiring, record: RunRecord) -> str:
        """`MODEL_<STATUS>` for the version a finished training run registered; clears its flags."""
        if record.model_version_id is None:
            return "MODEL_NOT_REGISTERED"
        try:
            status = self.services.registry.get(record.model_version_id).status.value
        except RegistryError:
            return "MODEL_NOT_REGISTERED"
        for model_id in firing.flagged_models:
            self.services.retrain_flags.clear(model_id)
        if firing.flagged_models:
            _LOGGER.info(
                "schedule.retrain_flags_cleared firing_id=%s count=%d",
                firing.firing_id,
                len(firing.flagged_models),
            )
        return f"MODEL_{status.upper()}"

    def _fail(self, firing: ScheduleFiring, code: str, now: datetime) -> ScheduleFiring | None:
        """Settle a firing as failed; its alert is raised only by the settler whose update won."""
        finished = firing.model_copy(
            update={"status": FiringStatus.FAILED, "error_code": code, "finished_at": now}
        )
        if not self.services.store.settle_firing(finished):
            return None
        self._alert_failed(finished)
        return finished

    # --- the three kinds ----------------------------------------------------------------------------
    def _execute(self, schedule: Schedule, firing: ScheduleFiring) -> _Outcome:
        try:
            resolved = resolve_config(schedule.use_case_id, root=self.services.config_root)
        except ConfigError as exc:
            raise FiringError(
                "USE_CASE_UNAVAILABLE",
                f"The use case {schedule.use_case_id!r} could not be loaded, so nothing was run.",
            ) from exc
        if schedule.kind is ScheduleKind.SCORE:
            return self._score(schedule, resolved)
        if schedule.kind is ScheduleKind.RETRAIN:
            return self._retrain(schedule, resolved)
        return self._drift_check(schedule, firing, resolved)

    def _score(self, schedule: Schedule, resolved: ResolvedConfig) -> _Outcome:
        from engine.stages.score import ScoreError, resolve_model_version

        services = self.services
        config = resolved.config
        try:
            version = resolve_model_version(
                config, registry=services.registry, model_version_id=schedule.parameters.model_version_id
            )
        except ScoreError as exc:
            raise FiringError(exc.code, exc.message) from exc
        manifest = self._dataset(schedule, config, mode=RunMode.SCORE)
        record = self._start(resolved, manifest, mode=RunMode.SCORE, version=version)
        return _Outcome(FiringStatus.RUNNING, "SCORING_STARTED", record.run_id, manifest.dataset_id)

    def _retrain(self, schedule: Schedule, resolved: ResolvedConfig) -> _Outcome:
        services = self.services
        flagged = flagged_for_use_case(services.retrain_flags, services.registry, schedule.use_case_id)
        manifest = self._training_dataset(schedule, resolved.config)
        record = self._start(resolved, manifest, mode=RunMode.TRAIN, version=None)
        return _Outcome(FiringStatus.RUNNING, "TRAINING_STARTED", record.run_id, manifest.dataset_id, flagged)

    def _drift_check(self, schedule: Schedule, firing: ScheduleFiring, resolved: ResolvedConfig) -> _Outcome:
        services = self.services
        config = resolved.config
        # Read first: an erasure flag is answered whether or not drift can be measured (DEC-777).
        flagged = flagged_for_use_case(services.retrain_flags, services.registry, schedule.use_case_id)
        champion = services.registry.get_champion(schedule.use_case_id)
        if champion is None:
            return self._erasure_retrain(schedule, resolved, flagged, "NO_CHAMPION")
        run = latest_scored_run(services.storage, schedule.use_case_id, schedule.client_id)
        if run is None:
            return self._erasure_retrain(schedule, resolved, flagged, "NO_SCORED_DATA")
        drift, reused = drift_against_champion(services.storage, run, champion, config)
        if drift is None:
            return self._erasure_retrain(schedule, resolved, flagged, "DRIFT_NOT_MEASURED")
        above = bool(drift.drifted_features)
        if above:
            services.alerts.raise_alert(
                new_alert(
                    AlertKind.DRIFT_ABOVE_THRESHOLD,
                    use_case_id=schedule.use_case_id,
                    client_id=schedule.client_id,
                    schedule_id=schedule.schedule_id,
                    run_id=run.run_id,
                    model_id=champion.model_id,
                    message=(
                        f"The latest scored data for {config.name} has shifted away from the data the "
                        f"champion model was trained on: {len(drift.drifted_features)} of "
                        f"{len(drift.features)} features are beyond the drift threshold. Review the drift "
                        "report before relying on the scores"
                        + (
                            "; a retraining run has been started."
                            if config.monitoring.retraining is Retraining.ON_DRIFT
                            else "."
                        )
                    ),
                    now=services.clock(),
                )
            )
        retrain = config.monitoring.retraining is Retraining.ON_DRIFT and (above or bool(flagged))
        retrain_run: RunRecord | None = None
        dataset_id: str | None = None
        if retrain:
            manifest = self._training_dataset(schedule, config)
            dataset_id = manifest.dataset_id
            retrain_run = self._start(resolved, manifest, mode=RunMode.TRAIN, version=None)
        services.storage.write_model(
            run_key(run.run_id, f"{DRIFT_CHECK_DIRECTORY}/{firing.firing_id}.json"),
            DriftCheckReport(
                firing_id=firing.firing_id,
                schedule_id=schedule.schedule_id,
                use_case_id=schedule.use_case_id,
                client_id=schedule.client_id,
                scoring_run_id=run.run_id,
                champion_model_id=champion.model_id,
                reused_run_drift=reused,
                drift=drift,
                above_threshold=above,
                flagged_models=flagged,
                retrain_run_id=None if retrain_run is None else retrain_run.run_id,
                checked_at=services.clock(),
            ),
        )
        code = "DRIFT_ABOVE_THRESHOLD" if above else "DRIFT_OK"
        if retrain_run is None:
            return _Outcome(FiringStatus.SUCCEEDED, code)
        reason = code if above else "RETRAIN_FOR_ERASURE"
        return _Outcome(
            FiringStatus.RUNNING, reason, retrain_run.run_id, dataset_id, flagged if retrain else ()
        )

    def _erasure_retrain(
        self, schedule: Schedule, resolved: ResolvedConfig, flagged: tuple[str, ...], code: str
    ) -> _Outcome:
        """A drift check that could not measure drift still retrains, under `on_drift`, when a model
        of the use case is flagged by an erasure - otherwise the flag would never close (DEC-777)."""
        if resolved.config.monitoring.retraining is not Retraining.ON_DRIFT or not flagged:
            return _Outcome(FiringStatus.SUCCEEDED, code)
        manifest = self._training_dataset(schedule, resolved.config)
        retrain_run = self._start(resolved, manifest, mode=RunMode.TRAIN, version=None)
        return _Outcome(
            FiringStatus.RUNNING, "RETRAIN_FOR_ERASURE", retrain_run.run_id, manifest.dataset_id, flagged
        )

    # --- data for the kinds -------------------------------------------------------------------------
    def _start(
        self,
        resolved: ResolvedConfig,
        manifest: DatasetManifest,
        *,
        mode: RunMode,
        version: ModelVersion | None,
    ) -> RunRecord:
        services = self.services
        if mode is RunMode.TRAIN and not resolved.config.governance.approval_required:
            # Scheduled training yields a challenger, never an automatic champion (plan section 4,
            # DEC-743): whatever the use case says, a run nobody started waits for an Approver
            # (DEC-778). Re-resolved as an override, so `run_config.json` shows where it came from.
            if resolved.overrides_applied:  # never, today: a firing resolves the use case as it is
                governance = resolved.config.governance.model_copy(update={"approval_required": True})
                resolved = resolved.model_copy(
                    update={"config": resolved.config.model_copy(update={"governance": governance})}
                )
            else:
                resolved = resolve_config(
                    resolved.use_case_id,
                    SCHEDULED_TRAINING_OVERRIDES,
                    root=services.config_root,
                    now=resolved.resolved_at,
                )
        return start_dataset_run(
            storage=services.storage,
            registry=services.registry,
            jobs=services.jobs,
            resolved=resolved,
            catalog=get_catalog(services.config_root),
            manifest=manifest,
            mode=mode,
            version=version,
            client_tag=services.job_client_tag,
            now=services.clock(),
            requested_by=services.principal.user_id,  # Plan D, DEC-862
        )

    def _dataset(self, schedule: Schedule, config: UseCaseConfig, *, mode: RunMode) -> DatasetManifest:
        """A score schedule's data: its fixed dataset, or a fresh build of its recipe."""
        parameters = schedule.parameters
        if parameters.dataset_id is not None:
            return load_built_dataset(
                self.services.storage, parameters.dataset_id, config=config, client_id=schedule.client_id
            )
        if parameters.onboarding_spec_id is not None:
            spec = self._spec(schedule, parameters.onboarding_spec_id, mode=mode)
            return self._build(spec, config, mode=mode)
        raise FiringError(
            "SCHEDULE_NO_DATA", "This schedule names no recipe and no dataset, so there is nothing to read."
        )

    def _training_dataset(self, schedule: Schedule, config: UseCaseConfig) -> DatasetManifest:
        """A retrain's data, most specific first: the schedule's own dataset or recipe, the recipe the
        champion was trained from, then the client's newest training recipe for this use case."""
        parameters = schedule.parameters
        if parameters.dataset_id is not None or parameters.onboarding_spec_id is not None:
            return self._dataset(schedule, config, mode=RunMode.TRAIN)
        spec_id = self._champion_spec_id(schedule) or self._newest_training_spec_id(schedule)
        if spec_id is None:
            raise FiringError(
                "NO_TRAINING_DATA",
                f"There is no saved training recipe for {config.name} to retrain from. Build a dataset "
                "from the client's tables once, or name a recipe on the schedule.",
            )
        return self._build(self._spec(schedule, spec_id, mode=RunMode.TRAIN), config, mode=RunMode.TRAIN)

    def _champion_spec_id(self, schedule: Schedule) -> str | None:
        services = self.services
        champion = services.registry.get_champion(schedule.use_case_id)
        if champion is None:
            return None
        try:
            trained = services.storage.read_model(run_key(champion.run_id, RUN_FILENAME), RunRecord)
            if trained.dataset_id is None:
                return None
            manifest = services.storage.read_model(
                dataset_key(trained.dataset_id, DATASET_MANIFEST_FILENAME), DatasetManifest
            )
        except (StorageError, ValueError):
            return None
        if schedule.client_id is not None and manifest.client_id != schedule.client_id:
            return None
        return manifest.spec_id

    def _newest_training_spec_id(self, schedule: Schedule) -> str | None:
        client_store = self.services.client_store
        if client_store is None or schedule.client_id is None:
            return None
        for spec in client_store.list_specs(schedule.client_id, schedule.use_case_id):
            if spec.label_spec is not None:
                return spec.spec_id
        return None

    def _spec(self, schedule: Schedule, spec_id: str, *, mode: RunMode) -> OnboardingSpec:
        client_store = self._client_store()
        try:
            spec = client_store.get_spec(spec_id)
        except ClientStoreError as exc:
            raise FiringError(
                "ONBOARDING_SPEC_NOT_FOUND", f"No onboarding recipe with id {spec_id!r}."
            ) from exc
        if schedule.client_id is not None and spec.client_id != schedule.client_id:
            raise FiringError("ONBOARDING_SPEC_NOT_FOUND", f"No onboarding recipe with id {spec_id!r}.")
        if spec.use_case != schedule.use_case_id:
            raise FiringError(
                "ONBOARDING_SPEC_USE_CASE_MISMATCH",
                f"Recipe {spec_id!r} builds data for {spec.use_case!r}, not {schedule.use_case_id!r}.",
            )
        if mode is RunMode.TRAIN and spec.label_spec is None:
            raise FiringError(
                "ONBOARDING_SPEC_HAS_NO_LABEL",
                f"Recipe {spec_id!r} defines no outcome to learn, so it cannot be used to retrain.",
            )
        return spec

    def _build(self, spec: OnboardingSpec, config: UseCaseConfig, *, mode: RunMode) -> DatasetManifest:
        manifest = build_dataset_from_spec(
            spec, config=config, mode=mode, client_store=self._client_store(), storage=self.services.storage
        )
        # Every refusal POST /runs makes on a dataset applies to a freshly built one too.
        return load_built_dataset(
            self.services.storage, manifest.dataset_id, config=config, client_id=spec.client_id
        )

    def _client_store(self) -> ClientStore:
        if self.services.client_store is None:
            raise FiringError(
                "CLIENT_STORE_UNAVAILABLE",
                "This deployment has no client store, so a recipe cannot be rebuilt on a schedule.",
            )
        return self.services.client_store

    # --- bookkeeping -------------------------------------------------------------------------------
    def _touch(
        self,
        schedule_id: str,
        *,
        last_fired_at: datetime | None = None,
        next_due_at: datetime | None = None,
    ) -> None:
        """Re-read the schedule and save one or two timestamps on it; a deleted schedule is left deleted."""
        store = self.services.store
        try:
            current = store.get(schedule_id)
        except ScheduleError:
            return
        changes: dict[str, datetime | None] = {}
        if last_fired_at is not None:
            changes["last_fired_at"] = last_fired_at
        if next_due_at is not None:
            changes["next_due_at"] = next_due_at
        if changes:
            store.save(current.model_copy(update=changes))

    def _alert_failed(self, firing: ScheduleFiring) -> None:
        self.services.alerts.raise_alert(
            new_alert(
                AlertKind.SCHEDULED_JOB_FAILED,
                use_case_id=firing.use_case_id,
                client_id=firing.client_id,
                schedule_id=firing.schedule_id,
                run_id=firing.run_id,
                message=(
                    f"The scheduled {_KIND_LABEL[firing.kind]} for {firing.use_case_id} did not finish "
                    f"(reason: {firing.error_code}). Open the schedule's history to see which step "
                    "stopped it, fix that, and fire the schedule again."
                ),
                now=self.services.clock(),
            )
        )

    def _audit(
        self, action: str, schedule_id: str, firing: ScheduleFiring, *, count: int | None = None
    ) -> None:
        services = self.services
        if services.audit_log is None:
            return
        details = firing_audit_details(firing)
        if count is not None:
            details["count"] = count
        event = AuditEvent(
            event_id=uuid.uuid4().hex,
            occurred_at=services.clock(),
            actor_id=services.principal.user_id,
            actor_kind=services.principal.kind,
            action=action,
            object_type="schedule",
            object_id=schedule_id,
            outcome="failed" if firing.status in (FiringStatus.FAILED, FiringStatus.MISSED) else "success",
            details=details,
        )
        try:
            services.audit_log.append(event)
        except Exception as exc:  # the work has happened; say loudly that its record did not
            log_failure(_LOGGER, f"schedule.audit firing_id={firing.firing_id}", exc, level=logging.ERROR)


def fire(
    schedule: Schedule,
    services: FiringServices,
    *,
    trigger: FiringTrigger = FiringTrigger.MANUAL,
    scheduled_for: datetime | None = None,
) -> ScheduleFiring | None:
    """Run `schedule`'s work once (see `ScheduleFirer.fire`)."""
    return ScheduleFirer(services).fire(schedule, trigger=trigger, scheduled_for=scheduled_for)


def _error_code(exc: BaseException) -> str:
    """The failure's own code when it carries one (every engine error does), else a generic one."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.replace("_", "").isalnum() and code.isupper():
        return code
    return "FIRING_FAILED"


def _claimed_newest(recorded: Sequence[ScheduleFiring], slots: Sequence[datetime]) -> bool:
    """Whether this process's missed firings include the newest of `slots` (DEC-779)."""
    if not recorded or not slots:
        return False
    newest = to_utc(max(slots))
    return any(item.scheduled_for is not None and to_utc(item.scheduled_for) == newest for item in recorded)
