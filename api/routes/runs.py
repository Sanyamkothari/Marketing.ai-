"""The run lifecycle: `POST /runs`, the history, one run, its artefacts and cancel (plan §8).

`POST /runs` validates **synchronously** (plan §8) and answers `409` with the whole
`ValidationReport` beside M1's error envelope when a blocking error remains, so the Setup screen's
inline list renders from one response. A run that passes gets its directory and *both* of its
documents written before the job is submitted, so the very first `GET /runs/{id}` the Running screen
issues - which can land microseconds after the `202` - always finds something true to render.

A **scoring** run is now the real thing: the request resolves the model version through the
engine's one resolver, validates the upload against *that* version's saved schema, pins the
resolved id on `run.json`, and submits `Pipeline.run_score`, which owns both documents from then
on. A **training** run still runs the M2 job body, which executes the two stages that milestone
owns for real and then fails at `prepare` with a named error (DEC-060): no stage on the Running
screen ever shows a number nobody measured.

Run creation belongs in its own module, `engine/runs.py`, which is not part of this change;
`create_run`, `update_run`, `cancel_run` and `build_m2_job` live here until it lands, and move
unchanged when it does. Likewise, `Pipeline` is built from the three existing dependencies here
rather than from a `get_pipeline` provider in `api/deps.py`, which does not exist yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from api.deps import ConfigRootDep, JobsDep, RegistryDep, StorageDep
from api.routes.uploads import (
    UPLOAD_VALIDATION_FILENAME,
    http_error,
    ingest_http,
    load_upload,
    load_upload_profile,
    profile_row_cap,
    use_case_config,
)
from api.schemas import (
    ErrorBody,
    ErrorResponse,
    RunCancelResponse,
    RunCreatedResponse,
    RunDetailResponse,
    RunListResponse,
    RunRequest,
    UploadRecord,
    ValidationErrorResponse,
)
from engine import __version__
from engine.config import (
    Catalog,
    PrimaryKey,
    ResolvedConfig,
    RunMode,
    UseCaseConfig,
    get_catalog,
    resolve_config,
    sole_key,
)
from engine.contracts import (
    ARTEFACT_REGISTRY,
    TABULAR_SCHEMAS,
    DatasetProfile,
    FeatureSchema,
    ModelVersion,
    RunError,
    RunRecord,
    RunState,
    RunStatus,
    StageKey,
    StageStatus,
    ValidationReport,
)
from engine.jobs import CancelToken, JobCancelledError, JobFn, JobRunner
from engine.onboarding.specs import DatasetManifest
from engine.pipeline import STATUS_FILENAME, Pipeline, StageContext
from engine.registry import ModelRegistry
from engine.stages import ingest, validate
from engine.stages.score import (
    CHAMPION_NOT_FOUND,
    MODEL_NOT_FOUND,
    MODEL_USE_CASE_MISMATCH,
    ScoreError,
    resolve_model_version,
)
from engine.storage import Storage, StorageError, run_key, upload_key
from engine.utils.ids import new_run_id
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

router: APIRouter = APIRouter(tags=["runs"])

_LOGGER = get_logger(__name__)

RUN_FILENAME: Final[str] = "run.json"
RUN_CONFIG_FILENAME: Final[str] = "run_config.json"
PROFILE_FILENAME: Final[str] = "profile.json"
VALIDATION_FILENAME: Final[str] = "validation.json"
RUN_MANIFEST_FILENAME: Final[str] = "run_manifest.json"
"""Reserved here and written by M3 (DEC-067); the artefact route serves it the moment it is registered."""

CREATED_ARTEFACTS: Final[tuple[str, ...]] = (
    RUN_FILENAME,
    STATUS_FILENAME,
    RUN_CONFIG_FILENAME,
    PROFILE_FILENAME,
    VALIDATION_FILENAME,
)
"""What a run directory holds at creation. `RunRecord.artefacts` is a map, so M3 only adds to it."""

STAGE_NOT_IMPLEMENTED: Final[str] = "STAGE_NOT_IMPLEMENTED"
PREPARE_NOT_IMPLEMENTED_MESSAGE: Final[str] = "Preparing features is not built yet."

SCORE_ERROR_STATUS: Final[dict[str, int]] = {
    CHAMPION_NOT_FOUND: 409,
    MODEL_NOT_FOUND: 404,
    MODEL_USE_CASE_MISMATCH: 409,
}
"""`ScoreError.code` -> HTTP status, for the codes `resolve_model_version` can raise on this route.

The registry table in `api.routes.models` works the same way, and for the same reason: a code this
router has not been taught is answered as a server fault rather than a guessed 4xx. `409` is the
status this endpoint already used for "your file and this use case do not fit together yet", and a
`model_version_id` naming a version that does not exist is a `404` like every other id in a
request body. The codes themselves are `engine.stages.score.SCORE_ERRORS`, which is where their
messages and suggestions live (DEC-052).
"""

UNMAPPED_STATUS: Final[int] = 500
"""A code this router has not been taught is a server fault, not the caller's; reported as one."""

ARTEFACT_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z_]+\.(json|csv|parquet)$")
"""Shape a URL segment must have before it is even looked up in the registry."""

MEDIA_TYPES: Final[dict[str, str]] = {"json": "application/json", "csv": "text/csv"}
DEFAULT_MEDIA_TYPE: Final[str] = "application/octet-stream"

DEFAULT_LIMIT: Final[int] = 20
MAX_LIMIT: Final[int] = 100

_FINISHED: Final[frozenset[RunState]] = frozenset({RunState.DONE, RunState.SKIPPED})
_UNFINISHED: Final[frozenset[RunState]] = frozenset({RunState.PENDING, RunState.RUNNING})

UseCaseQuery = Annotated[str | None, Query(description="Keep only runs of this use case.")]
ModeQuery = Annotated[RunMode | None, Query(description="Keep only train or only score runs.")]
LimitQuery = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="How many runs to return, newest first.")]

_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}
_RUN_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ValidationErrorResponse},
    422: {"model": ErrorResponse},
}


# ---------------------------------------------------------------------------
# 4.3 POST /runs
# ---------------------------------------------------------------------------
DATASET_NOT_FOUND: Final[str] = "DATASET_NOT_FOUND"
DATASET_NOT_BUILT: Final[str] = "DATASET_NOT_BUILT"
DATASET_USE_CASE_MISMATCH: Final[str] = "DATASET_USE_CASE_MISMATCH"
DATASET_COMPOSITE_KEY_NOT_WIRED: Final[str] = "DATASET_COMPOSITE_KEY_NOT_WIRED"
DATASET_CLIENT_MISMATCH: Final[str] = "DATASET_CLIENT_MISMATCH"


@dataclass(frozen=True, slots=True)
class _DatasetSource:
    """A built dataset, in the shape the upload path already consumes.

    A dataset and an upload differ in provenance, not in what a run needs from them: a storage key
    holding one row per entity, a profile of it, and a row count. Reducing the dataset to those
    three lets the whole validate-create-submit path below stay exactly as it was, which is the
    difference between Phase 2 reaching the engine and Phase 2 forking it (DEC-107).
    """

    manifest: DatasetManifest
    profile: DatasetProfile
    source_key: str

    @property
    def primary_key(self) -> str:
        return str(self.manifest.primary_key[0])


def _dataset_source(
    storage: Storage, dataset_id: str, *, config: UseCaseConfig, client_id: str | None
) -> _DatasetSource:
    """Resolve `dataset_id` into something a run can consume, or refuse it by name.

    Everything this checks is a question the user can act on: does the dataset exist, did its build
    actually finish, was it built for this use case, and is its key one the stages can carry. A run
    that started on a half-built dataset would train on whatever rows happened to be written.
    """
    from engine.onboarding.datasets import dataset_key

    manifest_key = dataset_key(dataset_id, "dataset_manifest.json")
    try:
        manifest = storage.read_model(manifest_key, DatasetManifest)
    except StorageError as exc:
        raise http_error(
            404,
            DATASET_NOT_FOUND,
            f"No dataset with id {dataset_id!r}. Build one from your tables first.",
        ) from exc

    frame_key = dataset_key(dataset_id, "dataset.parquet")
    if not storage.exists(frame_key):
        # A build that found a blocking check writes its report and no dataset, on purpose.
        raise http_error(
            409,
            DATASET_NOT_BUILT,
            f"Dataset {dataset_id!r} has a manifest but no rows: its build did not finish. "
            "Open the build report to see what stopped it.",
        )
    if manifest.use_case != config.id:
        raise http_error(
            409,
            DATASET_USE_CASE_MISMATCH,
            f"Dataset {dataset_id!r} was built for {manifest.use_case!r}, not {config.id!r}.",
        )
    if client_id is not None and manifest.client_id != client_id:
        raise http_error(
            409,
            DATASET_CLIENT_MISMATCH,
            f"Dataset {dataset_id!r} belongs to client {manifest.client_id!r}, not {client_id!r}.",
        )
    if len(manifest.primary_key) > 1:
        # Plan section 6.5 change 1 widens the key on the contracts, which is done; teaching
        # prepare, split, explain, actions and export to carry more than one column is the half of
        # that change still outstanding. Refusing here is the honest stop: a run that silently took
        # the first column would join on the customer and lose the snapshot date, training one row
        # per customer out of twelve and reporting success.
        raise http_error(
            501,
            DATASET_COMPOSITE_KEY_NOT_WIRED,
            f"Dataset {dataset_id!r} has one row per {' and '.join(manifest.primary_key)}, and a run "
            "still takes a single key column. Build it with a single snapshot to train on it now.",
        )

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
    return _DatasetSource(manifest=manifest, profile=profile, source_key=frame_key)


@router.post(
    "/runs",
    response_model=RunCreatedResponse,
    status_code=202,
    responses=_RUN_ERRORS,
    summary="Validate an upload and, when it passes, start a run",
)
def create_run_endpoint(
    body: RunRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    registry: RegistryDep,
    jobs: JobsDep,
    response: Response,
) -> RunCreatedResponse | JSONResponse:
    """Validate synchronously; `409` with the full report, or `202` with a run that is already pollable.

    `RunRequest` carries three fields Phase 2 will use and this phase cannot honour: a composite
    `primary_key`, `dataset_id` and `client_id`. They are refused here rather than dropped -
    accepting a request and quietly ignoring half of it is how a user comes to believe their rows
    were joined on two columns when they were joined on one.
    """
    use_case_config(body.use_case, root)  # 404 for a planned or unknown id, before anything is read
    resolved = resolve_config(body.use_case, body.overrides, root=root)
    config = resolved.config
    catalog = get_catalog(root)

    upload: UploadRecord | None = None
    dataset: _DatasetSource | None = None
    file_format: Literal["csv", "parquet"]
    if body.dataset_id is not None:
        dataset = _dataset_source(storage, body.dataset_id, config=config, client_id=body.client_id)
        profile = dataset.profile
        source_key, file_format = dataset.source_key, "parquet"
        # The manifest knows what a row is and what the outcome is called; a request that repeats
        # them is honoured, a request that omits them is answered rather than refused.
        primary_key = sole_key(body.primary_key, what="A run") if body.primary_key else dataset.primary_key
        target = body.target or dataset.manifest.target
    else:
        upload_id = _require_upload_id(body.upload_id)
        primary_key = sole_key(_require_primary_key(body.primary_key), what="A run")
        upload = load_upload(storage, upload_id)
        if upload.mode is not body.mode:
            raise http_error(
                409,
                "UPLOAD_MODE_MISMATCH",
                f"This file was uploaded for {upload.mode.value}. Upload it again for {body.mode.value}.",
            )
        profile = load_upload_profile(storage, upload_id)
        source_key, file_format = upload.source_key, upload.file_format
        target = body.target
    source_id: str = dataset.manifest.dataset_id if dataset is not None else upload_id

    version: ModelVersion | None = None
    if body.mode is RunMode.TRAIN:
        report = validate.validate_for_training(
            read_frame(storage, source_key, file_format, profile_row_cap(config)),
            config,
            primary_key=primary_key,
            target=target or "",
            acknowledged=config.validation.acknowledged,
            upload_id=source_id,
            row_count=profile.row_count,
        )
    else:
        # Resolved once, here, and pinned on the run record below: the file is checked against this
        # version's columns, so this version is the one that must score it, whatever is promoted
        # while the job waits in the queue.
        version = score_version(registry, config=config, version_id=body.model_version_id)
        report = validate.validate_against_schema(
            read_frame(storage, source_key, file_format, profile_row_cap(config)),
            storage.read_model(version.schema_key, FeatureSchema),
            primary_key=primary_key,
            config=config,
            acknowledged=config.validation.acknowledged,
            upload_id=source_id,
            row_count=profile.row_count,
        )

    if upload is not None:
        # A dataset's checks travel on its run, not back onto the dataset: a dataset is immutable
        # and several runs can read one, so writing there would have each run overwrite the last.
        storage.write_model(upload_key(upload.upload_id, UPLOAD_VALIDATION_FILENAME), report)
    if not report.passed:
        return validation_conflict(report)

    record = create_run(
        storage,
        Pipeline(storage, registry, jobs),
        resolved=resolved,
        catalog=catalog,
        upload=upload,
        dataset=dataset,
        profile=profile,
        report=report,
        mode=body.mode,
        primary_key=primary_key,
        target=target,
        model_choice=body.model_choice or catalog.automl_choice.value,
        model_version_id=body.model_version_id if version is None else version.model_id,
    )
    jobs.submit(
        record.run_id,
        (
            build_m2_job(
                storage,
                run_id=record.run_id,
                profile=profile,
                report=report.model_copy(update={"run_id": record.run_id}),
                mode=body.mode,
            )
            if version is None
            else build_score_job(
                storage, registry, jobs, resolved=resolved, record=record, source_key=source_key
            )
        ),
    )
    response.headers["Location"] = f"/runs/{record.run_id}"
    return RunCreatedResponse(run_id=record.run_id)


# ---------------------------------------------------------------------------
# 4.4-4.7 reading and cancelling
# ---------------------------------------------------------------------------
@router.get("/runs", response_model=RunListResponse, summary="Run history, newest first")
def list_runs(
    storage: StorageDep,
    use_case: UseCaseQuery = None,
    mode: ModeQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> RunListResponse:
    """The Previous runs card. Run ids sort chronologically, so a reversed key listing is the order."""
    kept: list[RunRecord] = []
    for key in sorted(storage.list_keys("runs/"), reverse=True):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        try:
            record = storage.read_model(key, RunRecord)
        except (StorageError, ValueError):
            _LOGGER.warning("runs.unreadable key=%s", key)
            continue
        if use_case is not None and record.use_case_id != use_case:
            continue
        if mode is not None and record.mode is not mode:
            continue
        kept.append(record)
        if len(kept) == limit:
            break
    return RunListResponse(runs=tuple(kept))


@router.get(
    "/runs/{run_id}",
    response_model=RunDetailResponse,
    responses=_NOT_FOUND,
    summary="One run: its record and the status the Running screen polls",
)
def read_run(run_id: str, storage: StorageDep) -> RunDetailResponse:
    """`run.json` + `status.json` (plan §8). Both exist from the moment the `202` is returned."""
    record = load_run(storage, run_id)
    try:
        status = storage.read_model(run_key(run_id, STATUS_FILENAME), RunStatus)
    except StorageError as exc:
        raise run_not_found(run_id) from exc
    return RunDetailResponse(run=record, status=status)


@router.get(
    "/runs/{run_id}/artefacts/{name}",
    response_class=Response,
    responses=_NOT_FOUND,
    summary="One artefact of a run, whitelisted against the artefact registry",
)
def read_artefact(run_id: str, name: str, storage: StorageDep) -> Response:
    """A whitelist, not a path join: no segment of the URL ever reaches the filesystem."""
    if not ARTEFACT_NAME.fullmatch(name) or not (name in ARTEFACT_REGISTRY or name in TABULAR_SCHEMAS):
        raise http_error(404, "ARTEFACT_UNKNOWN", f"There is no artefact called {name!r}.")
    load_run(storage, run_id)
    try:
        payload = storage.read_bytes(run_key(run_id, name))
    except StorageError as exc:
        raise http_error(404, "ARTEFACT_NOT_FOUND", f"This run has not produced {name!r}.") from exc
    return Response(content=payload, media_type=media_type_for(name))


@router.get(
    "/runs/{run_id}/scores.csv",
    response_class=Response,
    responses=_NOT_FOUND,
    summary="The scored rows of a scoring run as CSV",
)
def read_scores(run_id: str, storage: StorageDep) -> Response:
    """Registered now, produced by M4; until then every run answers `404 ARTEFACT_NOT_FOUND`."""
    return read_artefact(run_id, "scores.csv", storage)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunCancelResponse,
    responses=_NOT_FOUND,
    summary="Ask a pending or running run to stop",
)
def cancel_run_endpoint(run_id: str, storage: StorageDep, jobs: JobsDep) -> RunCancelResponse:
    """`cancelled` is true only for a job that was still pending or running; never a 500."""
    record = load_run(storage, run_id)
    if not jobs.cancel(run_id):
        return RunCancelResponse(run_id=run_id, cancelled=False, state=record.state)
    cancelled = cancel_run(storage, run_id)
    return RunCancelResponse(run_id=run_id, cancelled=True, state=cancelled.state)


# ---------------------------------------------------------------------------
# The run directory (moves to `engine/runs.py` when that module lands)
# ---------------------------------------------------------------------------
def create_run(
    storage: Storage,
    pipeline: Pipeline,
    *,
    resolved: ResolvedConfig,
    catalog: Catalog,
    upload: UploadRecord | None = None,
    dataset: _DatasetSource | None = None,
    profile: DatasetProfile,
    report: ValidationReport,
    mode: RunMode,
    primary_key: str,
    target: str | None,
    model_choice: str,
    model_version_id: str | None,
    now: datetime | None = None,
) -> RunRecord:
    """Write the whole run directory, in this order, then return. Nothing is submitted here.

    `status.json` is written before `run.json` and both before the caller submits, so a poller can
    never observe a run that exists but has nothing to show.
    """
    moment = now or utc_now()
    run_id = new_run_id(moment)
    config = resolved.config
    artefacts = {name: run_key(run_id, name) for name in CREATED_ARTEFACTS}
    storage.write_model(artefacts[RUN_CONFIG_FILENAME], resolved)
    storage.write_model(artefacts[PROFILE_FILENAME], profile)
    storage.write_model(artefacts[VALIDATION_FILENAME], report.model_copy(update={"run_id": run_id}))
    storage.write_model(artefacts[STATUS_FILENAME], pipeline.initial_status(run_id, mode))
    metric = config.model_search.metric
    record = RunRecord(
        run_id=run_id,
        use_case_id=config.id,
        use_case_name=config.name,
        mode=mode,
        state=RunState.PENDING,
        created_at=moment,
        started_at=None,
        finished_at=None,
        upload_id=upload.upload_id if upload is not None else None,
        dataset_id=dataset.manifest.dataset_id if dataset is not None else None,
        client_id=dataset.manifest.client_id if dataset is not None else None,
        dataset_fingerprint=dataset.manifest.fingerprint.hash if dataset is not None else None,
        file_name=upload.file_name if upload is not None else _dataset_file_name(dataset),
        row_count=profile.row_count,
        primary_key=primary_key,
        target=target,
        problem_type=config.problem_type,
        model_choice=model_choice,
        model_version_id=model_version_id,
        best_model=None,
        headline_metric=metric,
        headline_metric_label=catalog.metric_label(metric),
        headline_score=None,
        champion=False,
        beat_previous_champion=False,
        overrides=dict(resolved.overrides_applied),
        artefacts=artefacts,
        error=None,
        engine_version=__version__,
    )
    storage.write_model(artefacts[RUN_FILENAME], record)
    return record


def update_run(storage: Storage, run_id: str, **fields: Any) -> RunRecord:
    """Read, `model_copy` and write `run.json`; the only writer of that document after creation."""
    key = run_key(run_id, RUN_FILENAME)
    record = storage.read_model(key, RunRecord).model_copy(update=fields)
    storage.write_model(key, record)
    return record


def update_stage(
    storage: Storage,
    run_id: str,
    stage: StageKey,
    *,
    state: RunState,
    detail: str | None = None,
    error: RunError | None = None,
    now: datetime | None = None,
) -> RunStatus:
    """Move one stage, recompute the run-level state and progress, and rewrite `status.json` in full."""
    moment = now or utc_now()
    key = run_key(run_id, STATUS_FILENAME)
    status = storage.read_model(key, RunStatus)
    stages = tuple(
        _moved(row, moment, state=state, detail=detail, error=error) if row.key is stage else row
        for row in status.stages
    )
    updated = status.model_copy(
        update={
            "stages": stages,
            "state": _run_state(stages),
            "current_stage": stage if state is RunState.RUNNING else None,
            "progress_pct": _progress(stages),
            "updated_at": moment,
        }
    )
    storage.write_model(key, updated)
    return updated


def cancel_run(storage: Storage, run_id: str, *, now: datetime | None = None) -> RunRecord:
    """Rewrite both documents as cancelled: every unfinished stage stops, and the record is terminal."""
    moment = now or utc_now()
    key = run_key(run_id, STATUS_FILENAME)
    status = storage.read_model(key, RunStatus)
    stages = tuple(
        (
            row.model_copy(update={"state": RunState.CANCELLED, "ended_at": moment})
            if row.state in _UNFINISHED
            else row
        )
        for row in status.stages
    )
    storage.write_model(
        key,
        status.model_copy(
            update={
                "stages": stages,
                "state": RunState.CANCELLED,
                "current_stage": None,
                "progress_pct": _progress(stages),
                "updated_at": moment,
            }
        ),
    )
    return update_run(storage, run_id, state=RunState.CANCELLED, finished_at=moment)


def build_score_job(
    storage: Storage,
    registry: ModelRegistry,
    jobs: JobRunner,
    *,
    resolved: ResolvedConfig,
    record: RunRecord,
    source_key: str,
) -> JobFn:
    """The score flow of plan §6.2, off the request thread: `Pipeline.run_score` and nothing else.

    The pipeline owns both documents from here on. It rewrites `status.json` at every stage
    transition, records a failure or a cancellation *on the stage it happened at* with the detail
    line that stage had earned, and writes `run_manifest.json` whatever the outcome - so this body
    adds nothing to either path. In particular a `JobCancelledError` is left to propagate: the
    runner reads it as "cancelled", and calling `cancel_run` here as the M2 body does would
    overwrite the stage the pipeline stopped at with a blanket cancellation.

    The model version is the one the request resolved and pinned on `run.json`
    (`record.model_version_id`), never "the champion" again. The upload was validated against that
    version's schema, so that version is the one that must score it.
    """
    pipeline = Pipeline(storage, registry, jobs)

    def job(cancel: CancelToken) -> None:
        pipeline.run_score(
            StageContext(
                run_id=record.run_id,
                mode=RunMode.SCORE,
                config=resolved.config,
                resolved=resolved,
                storage=storage,
                registry=registry,
                cancel=cancel,
                primary_key=sole_key(record.primary_key, what="A scoring run"),
                target=record.target,
                upload_key=source_key,
                model_version_id=record.model_version_id,
            )
        )

    return job


def build_m2_job(
    storage: Storage,
    *,
    run_id: str,
    profile: DatasetProfile,
    report: ValidationReport,
    mode: RunMode,
) -> JobFn:
    """The M2 job body (DEC-060): ingest and validate for real, then an honest stop at `prepare`.

    Both stages have already done their work on the request thread - the job replays them onto the
    status document so the Running screen shows real detail lines - and `prepare` fails with a named
    error instead of an unhandled `NotImplementedError`. M3 replaces this one function.
    """
    checked = StageKey.VALIDATE if mode is RunMode.TRAIN else StageKey.VALIDATE_AGAINST_SCHEMA

    def job(cancel: CancelToken) -> None:
        try:
            update_run(storage, run_id, state=RunState.RUNNING, started_at=utc_now())
            update_stage(storage, run_id, StageKey.INGEST, state=RunState.RUNNING)
            cancel.raise_if_cancelled()
            update_stage(
                storage, run_id, StageKey.INGEST, state=RunState.DONE, detail=ingest.ingest_detail(profile)
            )
            update_stage(storage, run_id, checked, state=RunState.RUNNING)
            cancel.raise_if_cancelled()
            update_stage(
                storage, run_id, checked, state=RunState.DONE, detail=validate.validation_detail(report)
            )
            cancel.raise_if_cancelled()
            failure = RunError(
                code=STAGE_NOT_IMPLEMENTED,
                message=PREPARE_NOT_IMPLEMENTED_MESSAGE,
                stage=StageKey.PREPARE,
            )
            update_stage(storage, run_id, StageKey.PREPARE, state=RunState.FAILED, error=failure)
            update_run(storage, run_id, state=RunState.FAILED, finished_at=utc_now(), error=failure)
        except JobCancelledError:
            cancel_run(storage, run_id)
            raise

    return job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_upload_id(upload_id: str | None) -> str:
    """Narrow the id `RunRequest` has already made non-null; only a wiring slip reaches it."""
    if upload_id is None:  # pragma: no cover - RunRequest refuses this shape before the route sees it
        raise http_error(422, "RUN_REQUEST_INCOMPLETE", "upload_id is required for this kind of run.")
    return upload_id


def _require_primary_key(primary_key: PrimaryKey | None) -> PrimaryKey:
    """Same narrowing for the key an upload-driven run cannot do without."""
    if primary_key is None:  # pragma: no cover - RunRequest refuses this shape before the route sees it
        raise http_error(422, "RUN_REQUEST_INCOMPLETE", "primary_key is required with an uploaded file.")
    return primary_key


def _dataset_file_name(dataset: _DatasetSource | None) -> str:
    """What the Results bar calls a run's data when it came from a build rather than a file."""
    if dataset is None:  # pragma: no cover - one of the two is always present
        raise http_error(422, "RUN_REQUEST_INCOMPLETE", "A run needs an upload or a dataset.")
    return f"{dataset.manifest.dataset_id} (built)"


def read_frame(
    storage: Storage, source_key: str, file_format: Literal["csv", "parquet"], row_cap: int
) -> Any:
    """The source's rows, capped, for the checks that need values rather than statistics (DEC-045).

    Takes a key and a format rather than an `UploadRecord` so that a built dataset - which has no
    upload record and never will - reaches the same checks through the same call.
    """
    try:
        return ingest.read_upload(storage, source_key, file_format=file_format, row_cap=row_cap).frame
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc


def score_version(registry: ModelRegistry, *, config: UseCaseConfig, version_id: str | None) -> ModelVersion:
    """The version this run would score with, resolved by the engine's rule, as an HTTP refusal.

    `engine.stages.score.resolve_model_version` is that rule and the only one (DEC-054): this
    endpoint asks the predict stage which version a scoring run would use, instead of re-deriving
    the answer from the registry with a laxer rule of its own. The old local version accepted a
    `model_version_id` belonging to *another* use case - it only checked that the registry knew the
    id - so the run was accepted, the file was validated against a foreign model's schema, and the
    run then failed at the predict stage with `MODEL_USE_CASE_MISMATCH`. Now the refusal happens
    before a run directory exists, in the stage's own words.

    The codes are the stage's too, which retires this module's `NO_CHAMPION_MODEL`: there is one
    code for "this use case has no champion", `CHAMPION_NOT_FOUND`, and it is the one the engine
    raises when the same condition stops a run mid-flight (DEC-054).

    The endpoint keeps the *version*, not only its schema, because the answer is pinned on
    `run.json` and handed to the job: the file is checked against these columns, so this model is
    the one that scores it even if the champion changes before the job starts.
    """
    try:
        return resolve_model_version(config, registry=registry, model_version_id=version_id)
    except ScoreError as exc:
        raise score_http(exc) from exc


def score_http(exc: ScoreError) -> HTTPException:
    """A `ScoreError` in M1's envelope. An unmapped code is a 500: this router does not guess."""
    return http_error(SCORE_ERROR_STATUS.get(exc.code, UNMAPPED_STATUS), exc.code, exc.message)


def validation_conflict(report: ValidationReport) -> JSONResponse:
    """The `409` body of DEC-058: M1's envelope, plus the whole report under a sibling key."""
    count = report.error_count
    body = ValidationErrorResponse(
        detail=ErrorBody(
            code="VALIDATION_FAILED",
            message=f"{count} problem{'s' if count != 1 else ''} must be fixed before this data can be used.",
            path=None,
        ),
        validation=report,
    )
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


def load_run(storage: Storage, run_id: str) -> RunRecord:
    """The run's `run.json`, or a 404 `RUN_NOT_FOUND`."""
    try:
        return storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    except StorageError as exc:
        raise run_not_found(run_id) from exc


def run_not_found(run_id: str) -> HTTPException:
    return http_error(404, "RUN_NOT_FOUND", f"No run with id {run_id!r}.")


def media_type_for(name: str) -> str:
    return MEDIA_TYPES.get(name.rsplit(".", 1)[-1], DEFAULT_MEDIA_TYPE)


def _moved(
    row: StageStatus,
    moment: datetime,
    *,
    state: RunState,
    detail: str | None,
    error: RunError | None,
) -> StageStatus:
    """One stage row after a transition; `started_at` is kept once set so durations stay honest."""
    started = moment if state is RunState.RUNNING else row.started_at
    ended = None if state in _UNFINISHED else moment
    changes: dict[str, Any] = {
        "state": state,
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": None if started is None or ended is None else (ended - started).total_seconds(),
    }
    if detail is not None:
        changes["detail"] = detail
    if error is not None:
        changes["error"] = error
    return row.model_copy(update=changes)


def _run_state(stages: tuple[StageStatus, ...]) -> RunState:
    """The run-level state implied by its stages: failure wins, then completion, then progress."""
    states = {row.state for row in stages}
    if RunState.FAILED in states:
        return RunState.FAILED
    if RunState.CANCELLED in states:
        return RunState.CANCELLED
    if states <= _FINISHED:
        return RunState.DONE
    if states & {RunState.RUNNING, RunState.DONE}:
        return RunState.RUNNING
    return RunState.PENDING


def _progress(stages: tuple[StageStatus, ...]) -> int:
    """Done stages over total, as whole percent."""
    if not stages:
        return 0
    return round(100 * sum(1 for row in stages if row.state in _FINISHED) / len(stages))
