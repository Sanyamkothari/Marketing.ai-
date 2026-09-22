"""The run lifecycle: `POST /runs`, the history, one run, its artefacts and cancel (design §4.3-§4.7).

`POST /runs` validates **synchronously** (plan §8) and answers `409` with the whole
`ValidationReport` beside M1's error envelope when a blocking error remains, so the Setup screen's
inline list renders from one response. A run that passes gets its directory and *both* of its
documents written before the job is submitted, so the very first `GET /runs/{id}` the Running screen
issues - which can land microseconds after the `202` - always finds something true to render.

Both flows are now the real thing. A **scoring** run resolves the model version through the
engine's one resolver, validates the upload against *that* version's saved schema and pins the
resolved id on `run.json`; a **training** run runs `Pipeline.run_train`. Since M2 a training run
submitted `build_m2_job` instead - two real stages and an honest stop at `prepare` - so training
through this API could not succeed even though the flow behind it was complete; `engine.runs`
routes it to `build_train_job` now (DEC-326).

Design §5.3's `engine/runs.py` has landed, so `create_run`, `update_run`, `update_stage`,
`cancel_run` and the job bodies live there and are re-exported from here: this module is a router
again. What it gained instead is the job *spec*. `POST /runs` writes `job_spec.json` beside
`run.json` and submits `build_job_fn(spec, …)`, so the runner it hands the job to may be a thread
pool that calls the closure or a SageMaker runner that ships the spec's key to a container and
ignores it (DEC-324). `GET /runs/{id}` gives a runner that can lose a job the chance to say so
before the status document is read (DEC-325).

`RunRequest` also carries three fields Phase 2 will use and this phase cannot honour: a composite
`primary_key`, `dataset_id` and `client_id`. They are refused here rather than dropped - accepting
a request and quietly ignoring half of it is how a user comes to believe their rows were joined on
two columns when they were joined on one.

`Pipeline` is still built from the three existing dependencies here rather than from the
`get_pipeline` provider design §4.8 adds to `api/deps.py`.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from api.deps import ConfigRootDep, JobsDep, RegistryDep, SettingsDep, StorageDep
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
from engine.config import RunMode, UseCaseConfig, get_catalog, resolve_config, sole_key
from engine.contracts import (
    ARTEFACT_REGISTRY,
    TABULAR_SCHEMAS,
    FeatureSchema,
    ModelVersion,
    RunRecord,
    RunState,
    RunStatus,
    ValidationReport,
)
from engine.generative.contracts import GENERATIVE_ARTEFACTS, GENERATIVE_TABULAR_SCHEMAS
from engine.jobs import JobRunner, ReconcilingJobRunner
from engine.pipeline import STATUS_FILENAME, Pipeline
from engine.registry import ModelRegistry
from engine.runs import (
    CREATED_ARTEFACTS,
    PROFILE_FILENAME,
    RUN_CONFIG_FILENAME,
    RUN_FILENAME,
    RUN_MANIFEST_FILENAME,
    VALIDATION_FILENAME,
    build_job_fn,
    build_m2_job,
    build_score_job,
    build_train_job,
    cancel_run,
    create_run,
    job_spec_for,
    update_run,
    update_stage,
    write_job_spec,
)
from engine.stages import ingest, validate
from engine.stages.score import (
    CHAMPION_NOT_FOUND,
    MODEL_NOT_FOUND,
    MODEL_USE_CASE_MISMATCH,
    ScoreError,
    resolve_model_version,
)
from engine.storage import Storage, StorageError, run_key, upload_key
from engine.utils.logging import get_logger, log_failure

router: APIRouter = APIRouter(tags=["runs"])

_LOGGER = get_logger(__name__)

__all__ = [
    "CREATED_ARTEFACTS",
    "PROFILE_FILENAME",
    "RUN_CONFIG_FILENAME",
    "RUN_FILENAME",
    "RUN_MANIFEST_FILENAME",
    "VALIDATION_FILENAME",
    "build_job_fn",
    "build_m2_job",
    "build_score_job",
    "build_train_job",
    "cancel_run",
    "create_run",
    "job_spec_for",
    "router",
    "update_run",
    "update_stage",
    "write_job_spec",
]
"""The router, plus the `engine.runs` names this module re-exports.

They are re-exported rather than merely moved because `api.routes.runs.create_run` is the name four
test modules and every reader of design §5.3 already know, and because a monkeypatch of a job body
has to address the module the route looks it up in. Listing them here says the imports are the
public surface of this module and not leftovers (DEC-327).
"""

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
_ONBOARDING_FIELDS: Final[tuple[str, ...]] = ("dataset_id", "client_id")
"""`RunRequest` fields whose shape exists for Phase 2 and whose behaviour does not exist yet."""


def _reject_unimplemented_onboarding(body: RunRequest) -> None:
    """`422` when a request names an onboarded dataset, which nothing in this phase can resolve.

    The fields are on `RunRequest` because the contract they belong to is shared and append-only,
    so it had to be settled before the branches started. Until the onboarding work lands there is
    nothing behind them, and a run started from a `dataset_id` would silently score the upload the
    request also carried - a different file from the one the caller asked for.
    """
    named = [name for name in _ONBOARDING_FIELDS if getattr(body, name) is not None]
    if not named:
        return
    fields = " and ".join(named)
    raise http_error(
        422,
        "DATASET_ONBOARDING_NOT_AVAILABLE",
        f"This engine cannot start a run from {fields} yet. Upload the file and run against upload_id.",
    )


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
    settings: SettingsDep,
    response: Response,
) -> RunCreatedResponse | JSONResponse:
    """Validate synchronously; `409` with the full report, or `202` with a run that is already pollable.

    The run directory, including `job_spec.json`, is complete before anything is submitted: the spec
    is what a remote runner ships to a container, so writing it after the submit would be a race
    against a job that has already started looking for it (DEC-324).
    """
    use_case_config(body.use_case, root)  # 404 for a planned or unknown id, before anything is read
    primary_key = sole_key(body.primary_key, what="A run")
    _reject_unimplemented_onboarding(body)
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

    version: ModelVersion | None = None
    if body.mode is RunMode.TRAIN:
        report = validate.validate_for_training(
            read_frame(storage, upload, profile_row_cap(config)),
            config,
            primary_key=primary_key,
            target=body.target or "",
            acknowledged=config.validation.acknowledged,
            upload_id=body.upload_id,
            row_count=profile.row_count,
        )
    else:
        # Resolved once, here, and pinned on the run record below: the file is checked against this
        # version's columns, so this version is the one that must score it, whatever is promoted
        # while the job waits in the queue.
        version = score_version(registry, config=config, version_id=body.model_version_id)
        report = validate.validate_against_schema(
            read_frame(storage, upload, profile_row_cap(config)),
            storage.read_model(version.schema_key, FeatureSchema),
            primary_key=primary_key,
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
        primary_key=primary_key,
        target=body.target,
        model_choice=body.model_choice or catalog.automl_choice.value,
        model_version_id=body.model_version_id if version is None else version.model_id,
    )
    spec = job_spec_for(record, upload=upload, client_id=settings.client_id)
    write_job_spec(storage, spec)
    jobs.submit(spec.job_id, build_job_fn(spec, storage=storage, registry=registry))
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
def read_run(run_id: str, storage: StorageDep, jobs: JobsDep) -> RunDetailResponse:
    """`run.json` + `status.json` (plan §8). Both exist from the moment the `202` is returned.

    A runner whose jobs can end without this process hearing about it is asked to reconcile first
    (DEC-325). `ThreadJobRunner` is not one of those and is not asked; a remote runner is, and this
    is the poll that turns "running for ever" into the failure it has been since the container died.
    """
    record = load_run(storage, run_id)
    reconcile_run(jobs, run_id)
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
    """A whitelist, not a path join: no segment of the URL ever reaches the filesystem.

    The whitelist is the union of the predictive registry and the generative one (DEC-210): a
    root-cause or campaign-copy job writes `root_cause_summary.json`, `copy_batch.json` and the rest
    into the *run's own* directory rather than inventing a second artefact route, so this is the one
    place both maps are checked together. `GENERATIVE_ARTEFACTS`/`GENERATIVE_TABULAR_SCHEMAS` also
    name a knowledge index's own files (`doc_index_manifest.json`, `chunks.parquet`, ...), which this
    run never wrote; whitelisting them here costs nothing beyond a normal `ARTEFACT_NOT_FOUND` for a
    name this run's directory does not hold, the same 404 an unproduced predictive artefact already
    answers with.
    """
    known = name in ARTEFACT_REGISTRY or name in TABULAR_SCHEMAS
    known = known or name in GENERATIVE_ARTEFACTS or name in GENERATIVE_TABULAR_SCHEMAS
    if not ARTEFACT_NAME.fullmatch(name) or not known:
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


def reconcile_run(jobs: JobRunner, run_id: str) -> None:
    """Let a runner that can lose a job write the ending it never got to write.

    A no-op for every runner that is not a `ReconcilingJobRunner`, which is how `ThreadJobRunner`
    pays nothing for a capability it does not need (DEC-325). A reconciliation that fails must not
    fail the read: the status document is still the answer to this request, and a runner that
    cannot reach its control plane has not made the stored documents any less true. The failure is
    logged by the exception's class, never its message (plan §13.7).
    """
    if not isinstance(jobs, ReconcilingJobRunner):
        return
    try:
        jobs.reconcile(run_id)
    except Exception as exc:  # a read must not fail because a control plane did
        log_failure(_LOGGER, "runs.reconcile", exc)
