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
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
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
from engine.config import (
    PrimaryKey,
    ProblemType,
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
    RunRecord,
    RunState,
    RunStatus,
    ValidationReport,
)
from engine.generative.contracts import GENERATIVE_ARTEFACTS, GENERATIVE_TABULAR_SCHEMAS
from engine.jobs import JobRunner, ReconcilingJobRunner
from engine.keys import normalise_key, split_config_for_key
from engine.onboarding.specs import DatasetManifest
from engine.pipeline import STATUS_FILENAME, Pipeline
from engine.registry import ModelRegistry
from engine.runs import (
    CREATED_ARTEFACTS,
    PROFILE_FILENAME,
    RUN_CONFIG_FILENAME,
    RUN_FILENAME,
    RUN_MANIFEST_FILENAME,
    VALIDATION_FILENAME,
    DatasetLineage,
    UploadInfo,
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
from engine.uplift.contracts import UPLIFT_ARTEFACTS
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
ClientIdQuery = Annotated[
    str | None,
    Query(description="Keep only runs of this client's built datasets; a run of an upload names no client."),
]

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
DATASET_CLIENT_MISMATCH: Final[str] = "DATASET_CLIENT_MISMATCH"
UPLIFT_REQUIRES_UPLIFT_ROUTE: Final[str] = "UPLIFT_REQUIRES_UPLIFT_ROUTE"
"""An uplift training run asked of `POST /runs`, which cannot run the uplift checks first (M53)."""


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
    def primary_key(self) -> PrimaryKey:
        """The dataset's own key: one column, or the entity key and the snapshot date (DEC-083)."""
        return normalise_key(list(self.manifest.primary_key))

    # --- `engine.runs.UploadInfo`, so the job path runs a dataset with no code of its own -------
    # Phase 4a's job spec, thread runner and SageMaker container read a run's source through that
    # four-property protocol. A dataset satisfies it as it stands; `lineage` carries the rest.
    @property
    def upload_id(self) -> str:
        return self.manifest.dataset_id

    @property
    def file_name(self) -> str:
        """What the Results bar calls a run's data when it came from a build rather than a file."""
        return f"{self.manifest.dataset_id} (built)"

    @property
    def file_format(self) -> Literal["csv", "parquet"]:
        return "parquet"

    @property
    def lineage(self) -> DatasetLineage:
        return DatasetLineage(
            dataset_id=self.manifest.dataset_id,
            client_id=self.manifest.client_id,
            fingerprint=self.manifest.fingerprint.hash,
        )


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
    settings: SettingsDep,
    response: Response,
    request: Request,
) -> RunCreatedResponse | JSONResponse:
    """Validate synchronously; `409` with the full report, or `202` with a run that is already pollable.

    The run directory, including `job_spec.json`, is complete before anything is submitted: the spec
    is what a remote runner ships to a container, so writing it after the submit would be a race
    against a job that has already started looking for it (DEC-324).
    """
    own = use_case_config(body.use_case, root)  # 404 for a planned or unknown id, before anything is read
    resolved = resolve_config(body.use_case, body.overrides, root=root)
    config = resolved.config
    # PHASE-4B: a run may not loosen a setting owned by a role its caller lacks (DEC-723). Imported
    # here: api.access imports api.routes.uploads, whose package imports this module.
    from api.access import require_roles_for_run_overrides

    require_roles_for_run_overrides(request, use_case=own, resolved=config)
    _refuse_uplift_training(body.mode, resolved)
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
        primary_key = normalise_key(body.primary_key) if body.primary_key else dataset.primary_key
        # A periodic dataset holds each customer at several dates: its rows are split by customer
        # (or by snapshot date), and run_config.json records the split as derived (DEC-083).
        resolved = split_config_for_key(resolved, primary_key)
        config = resolved.config
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

    # One source for the job path whichever the run read: an upload is an `UploadInfo` already, and
    # a dataset is one through `_DatasetSource`. Everything below is then the same for both.
    source: UploadInfo = upload if upload is not None else _require_dataset(dataset)
    record = create_run(
        storage,
        Pipeline(storage, registry, jobs),
        resolved=resolved,
        catalog=catalog,
        upload=source,
        dataset=dataset.lineage if dataset is not None else None,
        profile=profile,
        report=report,
        mode=body.mode,
        primary_key=primary_key,
        target=target,
        model_choice=body.model_choice or catalog.automl_choice.value,
        model_version_id=body.model_version_id if version is None else version.model_id,
        requested_by=requested_by(request),
    )
    spec = job_spec_for(record, upload=source, client_id=settings.client_id)
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
    client_id: ClientIdQuery = None,
) -> RunListResponse:
    """The Previous runs card. Run ids sort chronologically, so a reversed key listing is the order.

    `client_id` is the filter `RunRecord.client_id` was added for (plan section 6.5, change 4) and
    that `GET /datasets?client_id=` already offers one step upstream: without it, a screen working
    on one client's datasets could only show every client's history, and a run of another client's
    data sat one click away from being read as this client's. It matches `run.json`'s own field
    exactly, so a run of an upload - which names no client - is never listed under one
    (docs/CLIENT_ISOLATION.md). Omitting it lists every run, as before.
    """
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
        if client_id is not None and record.client_id != client_id:
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
    # Uplift runs write their artefacts into the run's own directory too, so Phase 1's Data, Model and
    # Output pages read them here like any other (M53; `GET /runs/{id}/uplift/{name}` stays an alias).
    known = known or name in UPLIFT_ARTEFACTS
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
def _require_upload_id(upload_id: str | None) -> str:
    """Narrow the id `RunRequest` has already made non-null; only a wiring slip reaches it."""
    if upload_id is None:  # pragma: no cover - RunRequest refuses this shape before the route sees it
        raise http_error(422, "RUN_REQUEST_INCOMPLETE", "upload_id is required for this kind of run.")
    return upload_id


def _refuse_uplift_training(mode: RunMode, resolved: ResolvedConfig) -> None:
    """`422 UPLIFT_REQUIRES_UPLIFT_ROUTE` for a training run whose problem type resolves to uplift.

    An uplift run needs its treatment column chosen and the uplift checks passed *before* the run
    exists, which only `POST /uplift/runs` does; here the run's own validate stage would find out
    late and in generic words (docs/UPLIFT.md §3). Scoring an uplift model stays on this route: a
    scoring file needs no treatment column and is checked against the model's schema.
    """
    if mode is not RunMode.TRAIN or resolved.config.problem_type is not ProblemType.UPLIFT:
        return
    overridden = resolved.sources.get("problem_type") == "override"
    raise http_error(
        422,
        UPLIFT_REQUIRES_UPLIFT_ROUTE,
        "An uplift model is trained through POST /uplift/runs, which checks the treatment column and "
        "that it was randomly assigned before the run starts.",
        path="overrides.problem_type" if overridden else None,
    )


def _require_primary_key(primary_key: PrimaryKey | None) -> PrimaryKey:
    """Same narrowing for the key an upload-driven run cannot do without."""
    if primary_key is None:  # pragma: no cover - RunRequest refuses this shape before the route sees it
        raise http_error(422, "RUN_REQUEST_INCOMPLETE", "primary_key is required with an uploaded file.")
    return primary_key


def _require_dataset(dataset: _DatasetSource | None) -> _DatasetSource:
    """Narrow the source a run read; one of the upload and the dataset is always present."""
    if dataset is None:  # pragma: no cover - the endpoint sets one of the two on every path
        raise http_error(422, "RUN_REQUEST_INCOMPLETE", "A run needs an upload or a dataset.")
    return dataset


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


def requested_by(request: Request) -> str | None:
    """Who is starting a run: the principal `api.access.enforce_access` resolved, or None without one.

    Recorded on `run.json` so the approval screen can keep the person who trained a model from
    approving it (Plan D M54, DEC-862). Read from the request state rather than through
    `api.access.current_principal`, which would raise on an app built without access control.
    """
    principal = getattr(request.state, "principal", None)
    user_id = getattr(principal, "user_id", None)
    return user_id if isinstance(user_id, str) else None


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
