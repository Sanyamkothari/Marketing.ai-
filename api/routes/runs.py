"""The run lifecycle: `POST /runs`, the history, one run, its artefacts and cancel (design §4.3-§4.7).

`POST /runs` validates **synchronously** (plan §8) and answers `409` with the whole
`ValidationReport` beside M1's error envelope when a blocking error remains, so the Setup screen's
inline list renders from one response. A run that passes gets its directory and *both* of its
documents written before the job is submitted, so the very first `GET /runs/{id}` the Running screen
issues - which can land microseconds after the `202` - always finds something true to render.

The M2 job body runs the two stages this milestone owns for real and then fails at `prepare` with a
named error (DEC-060): no stage on the Running screen ever shows a number nobody measured.

Design §5.3 gives run creation its own module, `engine/runs.py`, which is not part of this change;
`create_run`, `update_run`, `cancel_run` and `build_m2_job` live here until it lands, and move
unchanged when it does. Likewise, `Pipeline` is built from the three existing dependencies here
rather than from the `get_pipeline` provider design §4.8 adds to `api/deps.py`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Final

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
from engine.config import Catalog, ResolvedConfig, RunMode, get_catalog, resolve_config
from engine.contracts import (
    ARTEFACT_REGISTRY,
    TABULAR_SCHEMAS,
    DatasetProfile,
    FeatureSchema,
    RunError,
    RunRecord,
    RunState,
    RunStatus,
    StageKey,
    StageStatus,
    ValidationReport,
)
from engine.jobs import CancelToken, JobCancelledError, JobFn
from engine.pipeline import STATUS_FILENAME, Pipeline
from engine.registry import ModelRegistry, RegistryError
from engine.stages import ingest, validate
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

ARTEFACT_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z_]+\.(json|csv|parquet)$")
"""Shape a URL segment must have before it is even looked up in the registry (design §4.6)."""

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
    """Validate synchronously; `409` with the full report, or `202` with a run that is already pollable."""
    use_case_config(body.use_case, root)  # 404 for a planned or unknown id, before anything is read
    upload = load_upload(storage, body.upload_id)
    if upload.mode is not body.mode:
        raise http_error(
            409,
            "UPLOAD_MODE_MISMATCH",
            f"This file was uploaded for {upload.mode.value}. Upload it again for {body.mode.value}.",
        )
    profile = load_upload_profile(storage, body.upload_id)
    resolved = resolve_config(body.use_case, body.overrides, root=root)
    config = resolved.config
    catalog = get_catalog(root)

    if body.mode is RunMode.TRAIN:
        report = validate.validate_for_training(
            read_frame(storage, upload, profile_row_cap(config)),
            config,
            primary_key=body.primary_key,
            target=body.target or "",
            acknowledged=config.validation.acknowledged,
            upload_id=body.upload_id,
            row_count=profile.row_count,
        )
    else:
        schema = score_schema(storage, registry, use_case=body.use_case, version_id=body.model_version_id)
        report = validate.validate_against_schema(
            read_frame(storage, upload, profile_row_cap(config)),
            schema,
            primary_key=body.primary_key,
            config=config,
            acknowledged=config.validation.acknowledged,
            upload_id=body.upload_id,
            row_count=profile.row_count,
        )

    storage.write_model(upload_key(body.upload_id, UPLOAD_VALIDATION_FILENAME), report)
    if not report.passed:
        return validation_conflict(report)

    record = create_run(
        storage,
        Pipeline(storage, registry, jobs),
        resolved=resolved,
        catalog=catalog,
        upload=upload,
        profile=profile,
        report=report,
        mode=body.mode,
        primary_key=body.primary_key,
        target=body.target,
        model_choice=body.model_choice or catalog.automl_choice.value,
        model_version_id=body.model_version_id,
    )
    jobs.submit(
        record.run_id,
        build_m2_job(
            storage,
            run_id=record.run_id,
            profile=profile,
            report=report.model_copy(update={"run_id": record.run_id}),
            mode=body.mode,
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
    """A whitelist, not a path join: no segment of the URL ever reaches the filesystem (design §4.6)."""
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
# 5.3-5.4 the run directory (design §5.3: `engine/runs.py` when that module lands)
# ---------------------------------------------------------------------------
def create_run(
    storage: Storage,
    pipeline: Pipeline,
    *,
    resolved: ResolvedConfig,
    catalog: Catalog,
    upload: UploadRecord,
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
        upload_id=upload.upload_id,
        file_name=upload.file_name,
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
def read_frame(storage: Storage, upload: UploadRecord, row_cap: int) -> Any:
    """The upload's rows, capped, for the checks that need values rather than statistics (DEC-045)."""
    try:
        return ingest.read_upload(
            storage, upload.source_key, file_format=upload.file_format, row_cap=row_cap
        ).frame
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc


def score_schema(
    storage: Storage, registry: ModelRegistry, *, use_case: str, version_id: str | None
) -> FeatureSchema:
    """The feature schema of the named version, or of the champion; `409 NO_CHAMPION_MODEL` if neither.

    In M2 no train flow has landed, so there is never a champion and this always refuses (DEC-059).
    """
    version = None
    if version_id is None:
        version = registry.get_champion(use_case)
    else:
        try:
            version = registry.get(version_id)
        except RegistryError:
            version = None
    if version is None:
        raise http_error(
            409, "NO_CHAMPION_MODEL", "No approved model exists for this use case yet. Train one first."
        )
    return storage.read_model(version.schema_key, FeatureSchema)


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
    """Done stages over total, as whole percent (design §5.4)."""
    if not stages:
        return 0
    return round(100 * sum(1 for row in stages if row.state in _FINISHED) / len(stages))
