"""Onboarding specs and dataset builds (Phase 2 plan §9, M12):

    POST /clients/{id}/onboarding-specs            save a recipe, return the checks it implies
    GET  /clients/{id}/onboarding-specs             a client's recipes
    POST /clients/{id}/onboarding-specs/{sid}/preview   build the recipe on a 200-entity sample
    POST /datasets                                  validate, then submit the real build as a job
    GET  /datasets/{id}                             manifest + build status
    GET  /datasets/{id}/report | /sample | /features.sql
    GET  /datasets                                  dataset manifests, newest first

Two different relationships with `engine.onboarding.build.build_dataset` live in this module, and
keeping them straight is most of what it does:

* **Preview** calls it directly, on the request thread, capped to a 200-entity sample
  (`PREVIEW_SAMPLE_ENTITIES`) so it can answer before the Setup screen's own timeout - it exists so
  a user sees the effect of a window or a filter before committing to a full build, and a slow
  preview defeats that whether or not the *numbers* it eventually returns are honest. `run_onboarding_checks`
  still runs first: a recipe with a structural error (an unmapped entity key, a mapping this call was
  not handed) is refused before a single row is read, exactly as `POST /datasets` refuses it before
  a job is queued - the difference is only that preview answers `200` with an empty result and the
  checks that stopped it, because showing a user what is wrong *is* the preview, where `POST /datasets`
  answers `409` because nothing should start.
* **`POST /datasets`** submits a job that calls it in the background (`JobsDep`, the same shape
  `api/routes/runs.py` already uses for a training or scoring run) and owns writing
  `datasets/<dataset_id>/`'s every artefact itself - `build_status.json` at each stage,
  `dataset.parquet` and `sample.json` when the frame is ready, `features.sql`, `build_report.json`
  and finally `dataset_manifest.json` - mirroring the ownership `engine.pipeline.Pipeline` already
  has over a run directory. This module's job body does exactly one thing afterwards that the build
  function cannot: hand the finished manifest to `ClientStore.register_dataset`, because a client's
  dataset index is not a dataset-directory artefact and so outside `DatasetRegistry`'s remit.

The checks come from `engine.onboarding.validate`, which takes one flat `OnboardingCheckParams` of
**measured** facts rather than the request's ids; `api.routes.mappings.check_params`/`facts_for` are
this API's copy of the config-to-params mapping its docstring asks every caller to do, and that
module's docstring explains what these routes deliberately leave out of it (no frame, so no check
that needs the data itself answers from a route - the build answers those, with the tables in front
of it). The consequence is worth stating plainly: what `POST /datasets` refuses on is every
*structural* problem - no entity source, an unmapped entity key, a required column nothing maps, more
sources or features than the use case allows - and the data-shaped findings arrive in the build
report, where they were actually measured.

`engine.onboarding.build` is landing in parallel (`PARALLEL_WORK_PROTOCOL.md`) and did not exist when
this module was written, so `build_dataset` gets the `_build_callable()` indirection the task calls
for by name: it is called from inside a background job body where an unhandled `ImportError` would
surface as an opaque failed job rather than the coded, business-language refusal
`BuildEngineUnavailableError` gives instead. This branch's assumed signature -
`build_dataset(spec, *, mode, dataset_id, registry, sources, mappings, source_ids=None,
sample_entities=None, cancel=None) -> BuildReport`, writing through the given `DatasetRegistry` as it
runs and returning the finished report - is documented on `_build_callable` and on
`tests/integration/test_api_datasets.py`'s module docstring; if the real signature differs, the fix
is at these call sites.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Annotated, Final, cast

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.deps import ConfigRootDep, JobsDep, StorageDep
from api.routes.clients import ClientStoreDep, load_client
from api.routes.mappings import check_params, facts_for, load_mapping
from api.routes.sources import load_profile, load_source
from api.routes.uploads import http_error, use_case_config
from api.schemas import ErrorBody, ErrorResponse
from engine.clients import ClientStore, ClientStoreError
from engine.config import RunMode, StrictBase, get_roles
from engine.contracts import RunState, Severity
from engine.jobs import CancelToken, JobCancelledError, JobFn
from engine.onboarding.datasets import (
    DATASET_FEATURES_SQL_FILENAME,
    DatasetError,
    DatasetRegistry,
    LocalDatasetRegistry,
    dataset_key,
)
from engine.onboarding.specs import (
    BuildReport,
    BuildStage,
    BuildStatus,
    DatasetManifest,
    FeatureSpec,
    LabelSpec,
    MappingSpec,
    OnboardingCheck,
    OnboardingSpec,
    SnapshotSpec,
    SnapshotStat,
    SourceProfile,
    SourceSpec,
)
from engine.onboarding.validate import run_onboarding_checks
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

router: APIRouter = APIRouter(tags=["datasets"])

_LOGGER = get_logger(__name__)

PREVIEW_SAMPLE_ENTITIES: Final[int] = 200
"""Entities (and everything that joins to them) a preview build reads - plan §9, M12: enough for the
Setup screen's window/filter feedback to mean something, small enough that `build_dataset` returning
in the screen's own budget is the real implementation's problem to solve, not this route's to fake."""

UseCaseQuery = Annotated[str | None, Query(description="Keep only recipes (or datasets) of this use case.")]
ClientIdQuery = Annotated[str | None, Query(description="Keep only datasets of this client.")]

_SPEC_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_PREVIEW_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}


# ---------------------------------------------------------------------------
# Request/response models (api/schemas.py is shared; ours live beside the routes that use them)
# ---------------------------------------------------------------------------
class OnboardingSpecCreateRequest(StrictBase):
    """Body of `POST /clients/{id}/onboarding-specs`: the whole recipe, plan §9 verbatim."""

    use_case: str
    entity_source_id: str
    event_source_ids: tuple[str, ...] = ()
    mapping_ids: tuple[str, ...]
    feature_spec: FeatureSpec
    label_spec: LabelSpec | None = None
    snapshot_spec: SnapshotSpec


class OnboardingSpecCreateResponse(StrictBase):
    """Body of the `201` from `POST /clients/{id}/onboarding-specs`."""

    spec_id: str
    checks: tuple[OnboardingCheck, ...]


class OnboardingSpecListResponse(StrictBase):
    """Body of `GET /clients/{id}/onboarding-specs`."""

    specs: tuple[OnboardingSpec, ...]


class PreviewResponse(StrictBase):
    """Body of `POST /clients/{id}/onboarding-specs/{sid}/preview`.

    Every field is empty rather than fabricated when the recipe was refused before a build was even
    attempted (`checks` carries the blocking error(s) either way) - house rule 2 extends to a preview
    exactly as it does to a finished dataset: no row, no snapshot stat and no null rate reaches this
    body unless it was actually measured on the sample.
    """

    rows: tuple[dict[str, str], ...]
    per_snapshot: tuple[SnapshotStat, ...]
    feature_null_rates: dict[str, float]
    checks: tuple[OnboardingCheck, ...]


class DatasetBuildRequest(StrictBase):
    """Body of `POST /datasets`."""

    client_id: str
    spec_id: str
    mode: RunMode = RunMode.TRAIN
    source_ids: tuple[str, ...] | None = None


class DatasetCreatedResponse(StrictBase):
    """Body of the `202` from `POST /datasets`: the id `GET /datasets/{id}` polls."""

    dataset_id: str


class DatasetChecksResponse(StrictBase):
    """The `409` body of `POST /datasets`: M1's envelope, plus the whole check list (DEC-058's shape,
    carried over from `api.routes.runs.ValidationErrorResponse` for the onboarding check vocabulary)."""

    detail: ErrorBody
    checks: tuple[OnboardingCheck, ...]


class DatasetGetResponse(StrictBase):
    """Body of `GET /datasets/{id}`.

    `manifest` is null until the build finishes - a running or failed build has a `status` and
    nothing else yet, and the UI renders an em dash rather than this route inventing one early.
    """

    manifest: DatasetManifest | None
    status: BuildStatus


class DatasetSampleResponse(StrictBase):
    """Body of `GET /datasets/{id}/sample`: the same stringified, PII-redacted rows `sample.json` holds."""

    rows: tuple[dict[str, str], ...]


class DatasetListResponse(StrictBase):
    """Body of `GET /datasets`."""

    datasets: tuple[DatasetManifest, ...]


# ---------------------------------------------------------------------------
# POST /clients/{id}/onboarding-specs
# ---------------------------------------------------------------------------
@router.post(
    "/clients/{client_id}/onboarding-specs",
    response_model=OnboardingSpecCreateResponse,
    status_code=201,
    responses=_SPEC_ERRORS,
    summary="Save a client's onboarding recipe and return the checks it implies",
)
def create_onboarding_spec(
    client_id: str,
    body: OnboardingSpecCreateRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    store: ClientStoreDep,
) -> OnboardingSpecCreateResponse:
    """Every id `body` names must resolve before anything is saved: the entity and event sources, and
    every mapping - each a `404` on its own code, exactly as `api.routes.mappings.load_mapping` and
    `api.routes.sources.load_source` already answer for their own endpoints, so a broken reference
    reads the same whichever screen surfaces it.

    A mapping that targets a file this recipe does not read is refused rather than saved and ignored:
    `OnboardingSpec` documents `mapping_ids` as "one per source", the build has nothing to apply such
    a mapping to, and a recipe that silently drops half of what it was given is how a user comes to
    believe a table was included when it was not.
    """
    load_client(store, client_id)
    config = use_case_config(body.use_case, root)
    source_ids = (body.entity_source_id, *body.event_source_ids)
    sources = load_source_specs(store, client_id, source_ids)
    mappings = load_mappings(store, client_id, body.mapping_ids)
    for mapping in mappings.values():
        if mapping.source_id not in sources:
            raise http_error(
                409,
                "MAPPING_NOT_FOR_SPEC",
                f"Mapping {mapping.mapping_id!r} describes a file this recipe does not read, so there "
                "would be nothing to apply it to. Remove it, or add its file to the recipe.",
            )
    spec = build_onboarding_spec(new_spec_id(), client_id, body)
    saved = store.save_spec(spec)
    return OnboardingSpecCreateResponse(
        spec_id=saved.spec_id,
        checks=spec_checks(config, root, sources=sources, mappings=mappings, spec=saved),
    )


# ---------------------------------------------------------------------------
# GET /clients/{id}/onboarding-specs
# ---------------------------------------------------------------------------
@router.get(
    "/clients/{client_id}/onboarding-specs",
    response_model=OnboardingSpecListResponse,
    responses=_NOT_FOUND,
    summary="A client's saved onboarding recipes, newest first",
)
def list_onboarding_specs(
    client_id: str, store: ClientStoreDep, use_case: UseCaseQuery = None
) -> OnboardingSpecListResponse:
    load_client(store, client_id)
    return OnboardingSpecListResponse(specs=store.list_specs(client_id, use_case))


# ---------------------------------------------------------------------------
# POST /clients/{id}/onboarding-specs/{sid}/preview
# ---------------------------------------------------------------------------
@router.post(
    "/clients/{client_id}/onboarding-specs/{spec_id}/preview",
    response_model=PreviewResponse,
    responses=_PREVIEW_ERRORS,
    summary="Build this recipe on a 200-entity sample, synchronously, to preview its effect",
)
def preview_onboarding_spec(
    client_id: str, spec_id: str, root: ConfigRootDep, storage: StorageDep, store: ClientStoreDep
) -> PreviewResponse:
    """Refuse first, on the same structural checks `POST /datasets` refuses on; otherwise call
    `build_dataset` on `PREVIEW_SAMPLE_ENTITIES` entities and report exactly what it measured.

    The sample is written under a real, freshly-minted dataset id so `build_dataset` needs no
    "preview mode" of its own to write through - it is simply told to sample - and is deleted again
    once this response has read back its sample rows: a preview is not a dataset, and leaving one
    behind under an id `GET /datasets` never lists would only be storage nobody can find again.
    """
    load_client(store, client_id)
    spec = load_spec(store, client_id, spec_id)
    config = use_case_config(spec.use_case, root)
    source_ids = (spec.entity_source_id, *spec.event_source_ids)
    sources = load_source_specs(store, client_id, source_ids)
    mappings = load_mappings(store, client_id, spec.mapping_ids)
    structural = spec_checks(config, root, sources=sources, mappings=mappings, spec=spec)
    if blocking_errors(structural):
        return PreviewResponse(rows=(), per_snapshot=(), feature_null_rates={}, checks=structural)

    profiles = load_profiles(storage, client_id, source_ids)
    registry = LocalDatasetRegistry(storage)
    preview_id = registry.new_dataset_id(client_id, spec.use_case)
    try:
        report = run_build(
            spec,
            mode=build_mode(spec),
            dataset_id=preview_id,
            registry=registry,
            sources=profiles,
            mappings=mappings,
            sample_entities=PREVIEW_SAMPLE_ENTITIES,
        )
        rows = tuple(registry.read_sample(preview_id))
    except BuildEngineUnavailableError as exc:
        # 503, not 422: the recipe this request named is fine and re-sending it unchanged is exactly
        # what the user should do once the engine can build. A 422 would tell them to go and fix a
        # request that has nothing wrong with it.
        raise http_error(503, exc.code, exc.message) from exc
    except DatasetError as exc:
        raise http_error(422, exc.code, exc.message) from exc
    except Exception as exc:  # the engine's own failure; a code and a message, never a raw traceback
        log_failure(_LOGGER, f"onboarding-spec preview spec_id={spec_id}", exc)
        raise http_error(
            422,
            "PREVIEW_BUILD_FAILED",
            "This recipe could not be previewed. Check the mapping and snapshot settings and try again.",
        ) from exc
    finally:
        registry.delete(preview_id)

    return PreviewResponse(
        rows=rows,
        per_snapshot=report.snapshots,
        feature_null_rates={feature.name: feature.null_fraction for feature in report.features},
        checks=report.checks,
    )


# ---------------------------------------------------------------------------
# POST /datasets
# ---------------------------------------------------------------------------
@router.post(
    "/datasets",
    response_model=DatasetCreatedResponse,
    status_code=202,
    responses={**_NOT_FOUND, 409: {"model": DatasetChecksResponse}},
    summary="Validate a recipe and, when it passes, start building a dataset from it",
)
def create_dataset(
    body: DatasetBuildRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    store: ClientStoreDep,
    jobs: JobsDep,
    response: Response,
) -> DatasetCreatedResponse | JSONResponse:
    """Validate synchronously, exactly as `api.routes.runs.create_run_endpoint` validates an upload:
    `409` with the whole check list when a blocking error remains, else a dataset directory with its
    `build_status.json` already written, and a job submitted against it, before this call returns -
    so the very first `GET /datasets/{id}` the Build screen issues, which can land microseconds after
    the `202`, always finds something true to render.
    """
    load_client(store, body.client_id)
    spec = load_spec(store, body.client_id, body.spec_id)
    use_case_config(spec.use_case, root)
    if body.source_ids is not None:
        for source_id in body.source_ids:
            load_source(store, body.client_id, source_id)
    sources = load_sources(store, storage, body.client_id, (spec.entity_source_id, *spec.event_source_ids))
    mappings = load_mappings(store, body.client_id, spec.mapping_ids)
    checks = run_onboarding_checks(sources=sources, mappings=mappings, spec=spec)
    if blocking_errors(checks):
        return dataset_checks_conflict(checks)

    registry = LocalDatasetRegistry(storage)
    dataset_id = registry.new_dataset_id(body.client_id, spec.use_case)
    registry.write_status(dataset_id, queued_status(dataset_id, body.client_id, spec.spec_id))
    jobs.submit(
        dataset_id,
        build_m12_job(
            store,
            registry,
            dataset_id=dataset_id,
            spec=spec,
            mode=body.mode,
            sources=sources,
            mappings=mappings,
            source_ids=body.source_ids,
        ),
    )
    response.headers["Location"] = f"/datasets/{dataset_id}"
    return DatasetCreatedResponse(dataset_id=dataset_id)


# ---------------------------------------------------------------------------
# GET /datasets/{id}, /report, /sample, /features.sql
# ---------------------------------------------------------------------------
@router.get(
    "/datasets/{dataset_id}",
    response_model=DatasetGetResponse,
    responses=_NOT_FOUND,
    summary="One dataset: its manifest, once built, and the status the Build screen polls",
)
def read_dataset(dataset_id: str, storage: StorageDep) -> DatasetGetResponse:
    registry = LocalDatasetRegistry(storage)
    status = load_status(registry, dataset_id)
    try:
        manifest: DatasetManifest | None = registry.read_manifest(dataset_id)
    except DatasetError:
        manifest = None
    return DatasetGetResponse(manifest=manifest, status=status)


@router.get(
    "/datasets/{dataset_id}/report",
    response_model=BuildReport,
    responses=_NOT_FOUND,
    summary="The build review screen's report for one dataset",
)
def read_dataset_report(dataset_id: str, storage: StorageDep) -> BuildReport:
    registry = LocalDatasetRegistry(storage)
    try:
        return registry.read_report(dataset_id)
    except DatasetError as exc:
        raise dataset_not_found(dataset_id) from exc


@router.get(
    "/datasets/{dataset_id}/sample",
    response_model=DatasetSampleResponse,
    responses=_NOT_FOUND,
    summary="A stringified, PII-redacted sample of one built dataset",
)
def read_dataset_sample(dataset_id: str, storage: StorageDep) -> DatasetSampleResponse:
    registry = LocalDatasetRegistry(storage)
    try:
        rows = registry.read_sample(dataset_id)
    except DatasetError as exc:
        raise dataset_not_found(dataset_id) from exc
    return DatasetSampleResponse(rows=tuple(rows))


@router.get(
    "/datasets/{dataset_id}/features.sql",
    response_class=Response,
    responses=_NOT_FOUND,
    summary="The compiled feature SQL of one built dataset, for debugging and Phase 4 porting",
)
def read_dataset_features_sql(dataset_id: str, storage: StorageDep) -> Response:
    key = dataset_key(dataset_id, DATASET_FEATURES_SQL_FILENAME)
    try:
        text = storage.read_text(key)
    except StorageError as exc:
        raise dataset_not_found(dataset_id) from exc
    return Response(content=text, media_type="text/plain")


# ---------------------------------------------------------------------------
# GET /datasets
# ---------------------------------------------------------------------------
@router.get("/datasets", response_model=DatasetListResponse, summary="Dataset manifests, newest first")
def list_datasets(
    store: ClientStoreDep, client_id: ClientIdQuery = None, use_case: UseCaseQuery = None
) -> DatasetListResponse:
    return DatasetListResponse(datasets=store.list_datasets(client_id, use_case))


# ---------------------------------------------------------------------------
# The M12 job body
# ---------------------------------------------------------------------------
def build_m12_job(
    store: ClientStore,
    registry: DatasetRegistry,
    *,
    dataset_id: str,
    spec: OnboardingSpec,
    mode: RunMode,
    sources: Mapping[str, SourceProfile],
    mappings: Mapping[str, MappingSpec],
    source_ids: tuple[str, ...] | None,
) -> JobFn:
    """The background job `POST /datasets` submits: call the build engine, then register the result.

    Mirrors `api.routes.runs.build_score_job` more than `build_m2_job`: `run_build`/`build_dataset`
    owns every write to `datasets/<dataset_id>/` as it runs (`build_score_job`'s own docstring gives
    the reason this shape is preferred whenever the callee already owns the artefacts - re-deriving
    "what stage are we in" here as well would be one more place for the two to disagree), so this
    body does the one thing it cannot: register the finished manifest with the client's own index,
    which is not part of the dataset directory `DatasetRegistry` writes.

    A `JobCancelledError` is left to propagate, exactly as `build_score_job`'s does, so
    `ThreadJobRunner` records the job as cancelled rather than failed; `build_dataset` is expected to
    leave `build_status.json` saying so too, since it is the one writing that document.
    """

    def job(cancel: CancelToken) -> None:
        try:
            report = run_build(
                spec,
                mode=mode,
                dataset_id=dataset_id,
                registry=registry,
                sources=sources,
                mappings=mappings,
                source_ids=source_ids,
                cancel=cancel,
            )
        except JobCancelledError:
            raise
        except BuildEngineUnavailableError as exc:
            write_build_unavailable(
                registry, dataset_id=dataset_id, client_id=spec.client_id, spec_id=spec.spec_id, error=exc
            )
            return
        if report.passed:
            manifest = registry.read_manifest(dataset_id)
            store.register_dataset(manifest)

    return job


# ---------------------------------------------------------------------------
# The build engine indirection (see module docstring)
# ---------------------------------------------------------------------------
BuildDatasetFn = Callable[..., BuildReport]
"""The assumed shape of `engine.onboarding.build.build_dataset`; see the module docstring for the
full call this branch makes against it. Typed loosely on its parameters (mypy does not check
keyword arguments against `...`) so a signature this branch guessed slightly wrong is a runtime
`TypeError` at the one call site (`run_build`), not a wall of unrelated type errors here."""


class BuildEngineUnavailableError(Exception):
    """`engine.onboarding.build` has not landed yet.

    Carries the same `(code, message)` shape as every other coded exception in this codebase
    (`engine.clients.ClientStoreError`, `engine.onboarding.datasets.DatasetError`, ...), so
    `run_build`'s callers translate it exactly like any other engine failure - a code, a
    business-language message, no Python traceback (house rule 3) - rather than needing a special
    case for "the module this branch depends on does not exist yet".
    """

    def __init__(self) -> None:
        self.code: str = "BUILD_ENGINE_NOT_AVAILABLE"
        self.message: str = (
            "The dataset build engine is not available yet. This is expected while "
            "engine.onboarding.build is still being written; try again once it has landed."
        )
        super().__init__(self.message)


def _build_callable() -> BuildDatasetFn:
    """`engine.onboarding.build.build_dataset`, imported lazily so `api/routes/datasets.py` imports
    cleanly whether or not that module exists yet (`PARALLEL_WORK_PROTOCOL.md`: another agent is
    writing it in parallel, and the task for this milestone asks for exactly this indirection).
    Raises `BuildEngineUnavailableError` rather than letting a bare `ImportError` reach `run_build`'s
    caller as an unhandled 500 or a silently failed background job.
    """
    try:
        from engine.onboarding.build import build_dataset
    except ImportError as exc:
        raise BuildEngineUnavailableError() from exc
    # `cast`: `engine.onboarding.build` has no py.typed marker yet (it does not exist - see the
    # module docstring), so mypy sees this import as `Any`; the cast asserts the contract this
    # branch wrote `run_build` against and becomes a no-op once the real module lands.
    return cast(BuildDatasetFn, build_dataset)


def run_build(
    spec: OnboardingSpec,
    *,
    mode: RunMode,
    dataset_id: str,
    registry: DatasetRegistry,
    sources: Mapping[str, SourceProfile],
    mappings: Mapping[str, MappingSpec],
    source_ids: tuple[str, ...] | None = None,
    sample_entities: int | None = None,
    cancel: CancelToken | None = None,
) -> BuildReport:
    """The one call site every build - preview or real - makes against the engine, so a signature
    mismatch is a `TypeError` here and nowhere else."""
    build = _build_callable()
    return build(
        spec,
        mode=mode,
        dataset_id=dataset_id,
        registry=registry,
        sources=sources,
        mappings=mappings,
        source_ids=source_ids,
        sample_entities=sample_entities,
        cancel=cancel or CancelToken(),
    )


def write_build_unavailable(
    registry: DatasetRegistry,
    *,
    dataset_id: str,
    client_id: str,
    spec_id: str,
    error: BuildEngineUnavailableError,
) -> None:
    """Overwrite `build_status.json` as failed, so a poller sees a plain-language reason instead of
    a build that silently never moves past "queued" (the M2 `STAGE_NOT_IMPLEMENTED` precedent in
    `api.routes.runs.build_m2_job`, for the same "not built yet" situation)."""
    stage = BuildStage(
        key="build",
        title="Build",
        group_label="Build",
        state=RunState.FAILED,
        detail=error.message,
    )
    registry.write_status(
        dataset_id,
        BuildStatus(
            dataset_id=dataset_id,
            client_id=client_id,
            spec_id=spec_id,
            state=RunState.FAILED,
            updated_at=utc_now(),
            stages=(stage,),
            current_stage=None,
            progress_pct=0,
            detail=error.message,
            error=error.code,
        ),
    )


# ---------------------------------------------------------------------------
# Ids, construction, checks and loading
# ---------------------------------------------------------------------------
def new_spec_id() -> str:
    """`spec_<12 hex>` - the same shape as `api.routes.sources.new_source_id` and
    `api.routes.mappings.new_mapping_id`, for the same reason."""
    return f"spec_{secrets.token_hex(6)}"


def build_onboarding_spec(spec_id: str, client_id: str, body: OnboardingSpecCreateRequest) -> OnboardingSpec:
    """`body` plus the id and timestamp the store's write owns, or a `422` naming the first problem.

    `OnboardingSpec`'s own validator (`_sources_are_distinct`: the entity source is not also named
    as an event source, no event source repeated) runs on construction, so a body that fails it never
    reaches `store.save_spec`; `exc.errors()[0]["msg"]` is already the business-language sentence the
    validator raised, exactly as `api.routes.mappings.build_mapping_spec` reads the same shape of
    error for `MappingSpec`.
    """
    try:
        return OnboardingSpec(
            spec_id=spec_id,
            client_id=client_id,
            use_case=body.use_case,
            entity_source_id=body.entity_source_id,
            event_source_ids=body.event_source_ids,
            mapping_ids=body.mapping_ids,
            feature_spec=body.feature_spec,
            label_spec=body.label_spec,
            snapshot_spec=body.snapshot_spec,
            created_at=utc_now(),
        )
    except ValidationError as exc:
        raise http_error(422, "ONBOARDING_SPEC_INVALID", str(exc.errors()[0]["msg"])) from exc


def blocking_errors(checks: Iterable[OnboardingCheck]) -> tuple[OnboardingCheck, ...]:
    """Errors that are not acknowledged - `engine.stages.validate`'s own `ValidationReport.passed`
    rule (plan §7's onboarding table reuses Phase 1's acknowledge mechanism, not a second one)."""
    return tuple(check for check in checks if check.severity is Severity.ERROR and not check.acknowledged)


def dataset_checks_conflict(checks: tuple[OnboardingCheck, ...]) -> JSONResponse:
    """The `409` body of `POST /datasets`: M1's envelope, plus the whole check list (mirrors
    `api.routes.runs.validation_conflict`)."""
    count = len(blocking_errors(checks))
    body = DatasetChecksResponse(
        detail=ErrorBody(
            code="ONBOARDING_CHECKS_FAILED",
            message=f"{count} problem{'s' if count != 1 else ''} must be fixed before this dataset can be built.",
            path=None,
        ),
        checks=checks,
    )
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


def queued_status(
    dataset_id: str, client_id: str, spec_id: str, *, now: datetime | None = None
) -> BuildStatus:
    """The placeholder `build_status.json` written before a job is even submitted, so the very first
    poll after the `202` finds something true: the build really is queued, nothing about its eventual
    stages is guessed at (house rule 2 - a single honest "queued" stage, not an invented full list)."""
    moment = now or utc_now()
    stage = BuildStage(key="queued", title="Queued", group_label="Build", state=RunState.PENDING)
    return BuildStatus(
        dataset_id=dataset_id,
        client_id=client_id,
        spec_id=spec_id,
        state=RunState.PENDING,
        updated_at=moment,
        stages=(stage,),
        current_stage=None,
        progress_pct=0,
        detail="Queued for build.",
        error=None,
    )


def load_sources(
    store: ClientStore, storage: Storage, client_id: str, source_ids: Iterable[str]
) -> dict[str, SourceProfile]:
    """Every named source's stored profile, keyed by id; each a `404 SOURCE_NOT_FOUND` on its own,
    exactly as `api.routes.sources` answers for its own endpoints."""
    profiles: dict[str, SourceProfile] = {}
    for source_id in source_ids:
        load_source(store, client_id, source_id)
        profiles[source_id] = load_profile(storage, client_id, source_id)
    return profiles


def load_mappings(store: ClientStore, client_id: str, mapping_ids: Iterable[str]) -> dict[str, MappingSpec]:
    """Every named mapping, keyed by id; each a `404 MAPPING_NOT_FOUND` on its own
    (`api.routes.mappings.load_mapping`)."""
    return {mapping_id: load_mapping(store, client_id, mapping_id) for mapping_id in mapping_ids}


def load_spec(store: ClientStore, client_id: str, spec_id: str) -> OnboardingSpec:
    """One recipe belonging to `client_id`, or a `404 ONBOARDING_SPEC_NOT_FOUND`.

    A recipe that exists but belongs to a *different* client answers the same 404, exactly as
    `api.routes.sources.load_source` hides a cross-client id behind one not-found."""
    try:
        spec = store.get_spec(spec_id)
    except ClientStoreError as exc:
        raise spec_not_found(spec_id) from exc
    if spec.client_id != client_id:
        raise spec_not_found(spec_id)
    return spec


def spec_not_found(spec_id: str) -> HTTPException:
    return http_error(404, "ONBOARDING_SPEC_NOT_FOUND", f"No onboarding spec with id {spec_id!r}.")


def load_status(registry: DatasetRegistry, dataset_id: str) -> BuildStatus:
    try:
        return registry.read_status(dataset_id)
    except DatasetError as exc:
        raise dataset_not_found(dataset_id) from exc


def dataset_not_found(dataset_id: str) -> HTTPException:
    return http_error(404, "DATASET_NOT_FOUND", f"No dataset with id {dataset_id!r}.")


__all__ = ["router"]
