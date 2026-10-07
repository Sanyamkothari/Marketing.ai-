"""Step 4 of a use case, "Measure the campaign" (Plan H M83): a thin layer over the uplift API.

Three routes, and none of them measures anything of its own:

* `GET /runs/{run_id}/measure` - what step 4 shows for a scoring run: whether the use case has the
  step at all (`engine.uplift.measure.measure_offered`, decided by configuration), the stored
  campaign results, their plain verdict, and whether an uplift model can be learned from them.
* `POST /runs/{run_id}/measure` - the one upload button. The outcomes file needs only the customer
  id and one outcome column; the column is found by `detect_outcome_column`, the outcome window
  comes from the use case (`uplift.outcome_window_days`, else its label's `horizon_days`), and the
  measurement is `POST /runs/{id}/campaign-results` itself (`api.routes.uplift.create_campaign_results`,
  called with the same request body a person would send), so the report is byte for byte the one
  the Campaign results page shows.
* `POST /runs/{run_id}/measure/learn` - "Learn who to contact next time". The experiment file an
  uplift model needs (features, a 0/1 treatment column, the outcome) is built on the server from the
  scored run's own input, its scores' control group and the measured outcomes
  (`engine.uplift.measure.build_experiment_frame`), stored as a new training upload, and handed to
  `POST /uplift/runs` (`api.routes.uplift.create_uplift_run`, the same checks, the same 409, the same
  job). The user never builds a treatment column.

`campaign_measure.json` in the run directory remembers the outcomes file and the uplift run, so the
step reopens where it was left.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, JobsDep, RegistryDep, SettingsDep, StorageDep
from api.routes.runs import load_run
from api.routes.uplift import _read_all, create_campaign_results, create_uplift_run
from api.routes.uploads import (
    UPLOAD_FINGERPRINT_FILENAME,
    UPLOAD_PROFILE_FILENAME,
    UPLOAD_RECORD_FILENAME,
    http_error,
    load_upload,
    profile_row_cap,
    source_filename,
    use_case_config,
)
from api.schemas import (
    CampaignResultsRequest,
    ErrorResponse,
    RunCreatedResponse,
    UpliftRunRequest,
    UpliftValidationErrorResponse,
    UploadRecord,
)
from engine.access.roles import Role
from engine.config import RunMode, StrictBase, UseCaseConfig, resolve_config
from engine.contracts import RunRecord, RunState, ScoringSummary
from engine.pilot.roi import outcome_is_good_by_default
from engine.runs import job_spec_key, read_job_spec
from engine.stages import export, ingest
from engine.storage import Storage, StorageError, run_key, upload_key
from engine.uplift.contracts import INCREMENTALITY_FILENAME, IncrementalityReport
from engine.uplift.measure import (
    CAMPAIGN_MEASURE_FILENAME,
    CampaignMeasure,
    CampaignVerdict,
    LearnReadiness,
    build_experiment_frame,
    campaign_verdict,
    detect_outcome_column,
    learn_readiness,
    measure_offered,
    treatment_column_for,
)
from engine.utils.ids import new_upload_id
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = ["LearnRequest", "MeasureRequest", "MeasureView", "default_outcome_window", "router"]

router: APIRouter = APIRouter(tags=["measure"])

_LOGGER = get_logger(__name__)

MEASURE_NOT_OFFERED: Final[str] = "MEASURE_NOT_OFFERED"
MEASURE_INVALID: Final[str] = "MEASURE_INVALID"
MEASURE_NOT_READY: Final[str] = "MEASURE_NOT_READY"
RUN_NOT_SCORED: Final[str] = "RUN_NOT_SCORED"
SCORING_SUMMARY_FILENAME: Final[str] = "scoring_summary.json"

register(
    {
        ("GET", "/runs/{run_id}/measure"): RoutePolicy(
            role=Role.VIEWER,
            action="measure.read",
            purpose="see a campaign's measured result",
            object_type="run",
            object_param="run_id",
        ),
        ("POST", "/runs/{run_id}/measure"): RoutePolicy(
            role=Role.ANALYST,
            action="measure.create",
            purpose="measure a campaign",
            object_type="run",
            object_param="run_id",
        ),
        ("POST", "/runs/{run_id}/measure/learn"): RoutePolicy(
            role=Role.ANALYST,
            action="measure.learn",
            purpose="learn who to contact from a campaign",
            object_type="run",
            object_param="run_id",
        ),
    }
)

_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_LEARN_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": UpliftValidationErrorResponse},
    422: {"model": ErrorResponse},
}


class MeasureRequest(StrictBase):
    """Body of `POST /runs/{run_id}/measure`: the outcomes file; everything else has a default."""

    upload_id: str = Field(description="Upload holding the customer id and the outcome after the campaign.")
    outcome_column: str | None = Field(
        default=None, description="The outcome column; found in the file when null."
    )
    positive_label: str | None = Field(default=None, description="Outcome value that counts as a conversion.")
    outcome_window_days: int | None = Field(
        default=None, ge=0, description="Days the outcome is counted over; the use case's own when null."
    )
    as_of: datetime | None = Field(default=None, description="Reference time for the window; now when null.")


class LearnRequest(StrictBase):
    """Body of `POST /runs/{run_id}/measure/learn`."""

    overrides: dict[str, Any] = Field(
        default_factory=dict, description="Run overrides for the uplift training run, nested or dotted."
    )


class UpliftRunState(StrictBase):
    """The uplift training run learned from this campaign."""

    run_id: str
    state: RunState


class MeasureView(StrictBase):
    """What step 4 shows for one scoring run."""

    run_id: str
    use_case_id: str
    offered: bool = Field(description="False for a use case whose actions do not contact customers.")
    reason: str | None = Field(default=None, description="Why step 4 is not offered, in one sentence.")
    held_back: int | None = Field(default=None, description="Customers the run held back, when known.")
    outcome_is_good: bool = Field(description="False when the campaign exists to make the outcome rarer.")
    outcome_label: str | None = Field(default=None, description="What counts as the outcome, in words.")
    outcomes: CampaignMeasure | None = Field(default=None, description="The outcomes file last measured.")
    report: IncrementalityReport | None = Field(default=None, description="The stored campaign results.")
    verdict: CampaignVerdict | None = Field(default=None, description="Their plain verdict.")
    learn: LearnReadiness = Field(description="Whether an uplift model can be learned from them.")
    uplift_run: UpliftRunState | None = Field(default=None, description="The uplift run learned from them.")


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/measure
# ---------------------------------------------------------------------------
@router.get(
    "/runs/{run_id}/measure",
    response_model=MeasureView,
    responses={404: {"model": ErrorResponse}},
    summary="Step 4 of a scoring run: its measured campaign, the plain verdict and what can be learned",
)
def read_measure(run_id: str, root: ConfigRootDep, storage: StorageDep) -> MeasureView:
    record = load_run(storage, run_id)
    config = use_case_config(record.use_case_id, root)
    return _view(storage, record, config)


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/measure
# ---------------------------------------------------------------------------


def default_outcome_window(config: UseCaseConfig) -> int | None:
    """The outcome window a measured campaign uses when the request names none: the use case's uplift
    window, else its label's whole window - the horizon plus any grace period (Plan J M93), the same
    days `engine.scheduling.outcomes` waits for. Identical to the horizon while `grace_days` is unset."""
    return config.uplift.outcome_window_days or (
        config.label.window_days if config.label is not None else None
    )


@router.post(
    "/runs/{run_id}/measure",
    response_model=MeasureView,
    responses=_ERRORS,
    summary="Measure a scoring run's campaign from an outcomes file of customer id and outcome",
)
def create_measure(
    run_id: str, body: MeasureRequest, root: ConfigRootDep, storage: StorageDep
) -> MeasureView:
    record = load_run(storage, run_id)
    config = use_case_config(record.use_case_id, root)
    _require_offered(config)
    upload = load_upload(storage, body.upload_id)
    if body.outcome_column is not None:
        outcome_column = body.outcome_column
    else:
        head = ingest.read_upload(storage, upload.source_key, file_format=upload.file_format, max_rows=5)
        try:
            outcome_column = detect_outcome_column(
                [str(name) for name in head.frame.columns],
                primary_key=record.primary_key,
                target_column=_target(config),
                label_name=config.label.name if config.label is not None else None,
            )
        except ValueError as exc:
            raise http_error(422, MEASURE_INVALID, str(exc), path="upload_id") from exc
    window = body.outcome_window_days
    if window is None:
        window = default_outcome_window(config)
    request = CampaignResultsRequest(
        upload_id=upload.upload_id,
        outcome_column=outcome_column,
        positive_label=body.positive_label,
        outcome_window_days=window,
        as_of=body.as_of,
    )
    create_campaign_results(run_id, request, storage)
    storage.write_model(
        run_key(run_id, CAMPAIGN_MEASURE_FILENAME),
        CampaignMeasure(
            run_id=run_id,
            upload_id=upload.upload_id,
            file_name=upload.file_name,
            outcome_column=outcome_column,
            positive_label=body.positive_label,
            outcome_window_days=window,
            outcome_named=body.outcome_column is not None,
            measured_at=utc_now(),
        ),
    )
    return _view(storage, record, config)


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/measure/learn
# ---------------------------------------------------------------------------
@router.post(
    "/runs/{run_id}/measure/learn",
    response_model=RunCreatedResponse,
    status_code=202,
    responses=_LEARN_ERRORS,
    summary="Learn who to contact next time: an uplift training run on the measured campaign",
)
def create_learn(
    run_id: str,
    body: LearnRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    registry: RegistryDep,
    jobs: JobsDep,
    settings: SettingsDep,
    response: Response,
    request: Request,
) -> RunCreatedResponse | JSONResponse:
    record = load_run(storage, run_id)
    config = use_case_config(record.use_case_id, root)
    _require_offered(config)
    measured = _stored(storage, run_key(run_id, CAMPAIGN_MEASURE_FILENAME), CampaignMeasure)
    report = _stored(storage, run_key(run_id, INCREMENTALITY_FILENAME), IncrementalityReport)
    if measured is None or report is None:
        raise http_error(409, MEASURE_NOT_READY, "Measure the campaign first: upload its outcomes.")
    resolved = resolve_config(config.id, body.overrides, root=root).config
    readiness = learn_readiness(report, resolved.uplift)
    if not readiness.ready:
        raise http_error(409, MEASURE_NOT_READY, readiness.reason)

    inputs = _run_input(storage, record)
    scores = _read_scores(storage, run_id)
    outcomes_upload = load_upload(storage, measured.upload_id)
    outcomes = _read_all(storage, outcomes_upload.source_key, outcomes_upload.file_format)
    treatment = treatment_column_for([str(name) for name in inputs.columns])
    target = _target(config)
    try:
        frame = build_experiment_frame(
            inputs,
            scores,
            outcomes,
            primary_key=record.primary_key,
            outcome_column=measured.outcome_column,
            positive_label=measured.positive_label,
            target_column=target,
            treatment_column=treatment,
        )
    except ValueError as exc:
        raise http_error(422, MEASURE_INVALID, str(exc)) from exc
    experiment = _write_upload(
        storage, config, frame, file_name=f"{record.file_name or run_id} + campaign outcomes"
    )
    overrides: dict[str, Any] = {**body.overrides, "target.positive_label": 1}
    started = create_uplift_run(
        UpliftRunRequest(
            use_case=config.id,
            upload_id=experiment.upload_id,
            primary_key=record.primary_key,
            target=target,
            treatment_column=treatment,
            overrides=overrides,
        ),
        root,
        storage,
        registry,
        jobs,
        settings,
        response,
        request,
    )
    if isinstance(started, RunCreatedResponse):
        storage.write_model(
            run_key(run_id, CAMPAIGN_MEASURE_FILENAME),
            measured.model_copy(update={"uplift_run_id": started.run_id}),
        )
        _LOGGER.info("measure-learn: run=%s uplift_run=%s rows=%d", run_id, started.run_id, len(frame.index))
    return started


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_offered(config: UseCaseConfig) -> None:
    if not measure_offered(config):
        raise http_error(409, MEASURE_NOT_OFFERED, _not_offered(config))


def _target(config: UseCaseConfig) -> str:
    """The use case's outcome column; every use case with step 4 names one (a generative one has none)."""
    return config.target.column or "outcome"


def _not_offered(config: UseCaseConfig) -> str:
    if not config.actions.contacts_customers or config.ai_type.value == "generative":
        return f"{config.name} does not contact customers, so there is no campaign to measure."
    return f"{config.name} holds nobody back, so there is nothing to compare a campaign with."


def _stored(storage: Storage, key: str, model: type[Any]) -> Any:
    try:
        return storage.read_model(key, model)
    except StorageError:
        return None


def _run_input(storage: Storage, record: RunRecord) -> Any:
    """Every row the scoring run read: the file its `job_spec.json` names, else its upload."""
    try:
        spec = read_job_spec(storage, job_spec_key(record.run_id))
        key, file_format = spec.upload_key, spec.upload_format
    except StorageError:
        if record.upload_id is None:
            raise http_error(
                409,
                MEASURE_NOT_READY,
                "The file this run scored is no longer stored, so there is nothing to learn from.",
            ) from None
        upload = load_upload(storage, record.upload_id)
        key, file_format = upload.source_key, upload.file_format
    return _read_all(storage, key, file_format)


def _read_scores(storage: Storage, run_id: str) -> Any:
    import io

    import pandas as pd

    return pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, export.SCORES_PARQUET))))


def _view(storage: Storage, record: RunRecord, config: UseCaseConfig) -> MeasureView:
    offered = measure_offered(config)
    report = _stored(storage, run_key(record.run_id, INCREMENTALITY_FILENAME), IncrementalityReport)
    measured = _stored(storage, run_key(record.run_id, CAMPAIGN_MEASURE_FILENAME), CampaignMeasure)
    summary = _stored(storage, run_key(record.run_id, SCORING_SUMMARY_FILENAME), ScoringSummary)
    # Which way round the outcome counts: a column found in the file is taken to be the use case's own
    # outcome, whatever the file calls it; a column the person named is judged by its own name.
    outcome = measured.outcome_column if measured is not None and measured.outcome_named else None
    good = outcome_is_good_by_default(config.id, outcome or _target(config))
    label = config.target.definition or None
    uplift_run: UpliftRunState | None = None
    if measured is not None and measured.uplift_run_id is not None:
        learned = _stored(storage, run_key(measured.uplift_run_id, "run.json"), RunRecord)
        if learned is not None:
            uplift_run = UpliftRunState(run_id=learned.run_id, state=learned.state)
    return MeasureView(
        run_id=record.run_id,
        use_case_id=config.id,
        offered=offered,
        reason=None if offered else _not_offered(config),
        held_back=summary.control_group_rows if summary is not None else None,
        outcome_is_good=good,
        outcome_label=label,
        outcomes=measured,
        report=report,
        verdict=campaign_verdict(report, outcome_is_good=good, outcome_label=label) if report else None,
        learn=learn_readiness(report, config.uplift),
        uplift_run=uplift_run,
    )


def _write_upload(storage: Storage, config: UseCaseConfig, frame: Any, *, file_name: str) -> UploadRecord:
    """Store `frame` as a new training upload, profiled exactly as `POST /uploads` profiles a file."""
    upload_id = new_upload_id()
    source_key = upload_key(upload_id, source_filename("parquet"))
    with storage.open_write(source_key) as sink:
        frame.to_parquet(sink, index=False)
    read = ingest.read_upload(storage, source_key, file_format="parquet", row_cap=profile_row_cap(config))
    profile = ingest.profile_dataset(
        read.frame,
        config,
        upload_id=upload_id,
        file_name=file_name,
        file_format="parquet",
        file_size_bytes=storage.size_bytes(source_key),
        delimiter=None,
        encoding=read.encoding,
        row_count=read.row_count,
        fingerprint=read.fingerprint,
    )
    record = UploadRecord(
        upload_id=upload_id,
        use_case_id=config.id,
        mode=RunMode.TRAIN,
        file_name=profile.file_name,
        file_format="parquet",
        file_size_bytes=profile.file_size_bytes,
        delimiter=None,
        encoding=profile.encoding,
        row_count=profile.row_count,
        column_count=profile.column_count,
        source_key=source_key,
        profile_key=upload_key(upload_id, UPLOAD_PROFILE_FILENAME),
        fingerprint_key=upload_key(upload_id, UPLOAD_FINGERPRINT_FILENAME),
        fingerprint_hash=profile.fingerprint.hash,
        created_at=utc_now(),
    )
    storage.write_model(record.profile_key, profile)
    storage.write_model(record.fingerprint_key, profile.fingerprint)
    storage.write_model(upload_key(upload_id, UPLOAD_RECORD_FILENAME), record)
    return record
