"""The uplift API (plan B §8): Setup's treatment picker, uplift training runs, their artefacts, campaign
results and off-policy evaluation.

**One run path, not two.** `POST /uplift/runs` is `POST /runs` for an uplift training run: it
validates synchronously, answers `409` with the reports when something blocks, and otherwise writes
the run directory and `job_spec.json` and submits the job through exactly the calls
`api/routes/runs.py` makes (`create_run`, `job_spec_for`, `write_job_spec`, `build_job_fn`). What
it adds is the second layer of checks - the six uplift checks of `engine.uplift.checks` - and the
overrides that make the run an uplift run (`problem_type: uplift`, the chosen treatment column).
The job then reaches `Pipeline.run_train`, whose one-line lookup sends it to the uplift flow. A
scoring run of an uplift model needs no route of its own: Phase 1's `POST /runs` with `mode: score`
and the uplift version's id validates the file against the schema that run wrote and the pipeline
sends it to the uplift score flow the same way.

**The 409 carries both reports.** The Setup screen renders Phase 1's validation list and the uplift
list from one response, so the body is M1's envelope plus `validation` and `uplift_validation`, at
the top level like `POST /runs`'s own 409 (DEC-058). The code is `VALIDATION_FAILED` when Phase 1's
checks block, else `UPLIFT_VALIDATION_FAILED`; the finding that blocked - `TREATMENT_NOT_RANDOM`,
for example - is in the report. Acknowledging it is a resend with
`overrides.validation.acknowledged`, the same mechanism Phase 1 uses.

**Uplift artefacts have their own whitelist.** `GET /runs/{id}/artefacts/{name}` serves the Phase 1
and generative registries and cannot grow a third without editing above the shared-file blocks, so
`GET /runs/{id}/uplift/{name}` serves `engine.uplift.contracts.UPLIFT_ARTEFACTS` and nothing else -
a whitelist, never a path join.

**Campaign results are measured against what the run did.** The scores file of a finished scoring
run says who was treated, who was held out and - for an uplift run - who the policy intended to
treat; the uploaded outcomes file says who converted. `measure_incrementality` compares treated with
control inside `intended_treatment` for an uplift run, because that is the population both arms
were drawn from; for a Phase 1 run it uses the requested bands or every eligible row. The run's own
finish time is the treatment time unless the outcomes file dates each row. Nothing is estimated for
a row whose outcome window has not elapsed: the report says when results will be available.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from api.deps import ConfigRootDep, JobsDep, RegistryDep, SettingsDep, StorageDep
from api.routes.runs import ARTEFACT_NAME, load_run, read_frame, requested_by
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
    CampaignResultsRequest,
    ErrorBody,
    ErrorResponse,
    OpeRequest,
    RunCreatedResponse,
    TreatmentCandidate,
    TreatmentCandidatesResponse,
    UpliftRunRequest,
    UpliftValidationErrorResponse,
)
from engine.config import ProblemType, RunMode, get_catalog, resolve_config, sole_key
from engine.contracts import RunRecord, RunState, Severity
from engine.pipeline import Pipeline
from engine.runs import build_job_fn, create_run, job_spec_for, write_job_spec
from engine.stages import export, ingest, validate
from engine.stages.train import predictor_key_for
from engine.storage import Storage, StorageError, run_key, upload_key
from engine.uplift.actions import INTENDED_TREATMENT_COLUMN
from engine.uplift.contracts import (
    INCREMENTALITY_FILENAME,
    OPE_FILENAME,
    UPLIFT_ARTEFACTS,
    UPLIFT_VALIDATION_FILENAME,
    IncrementalityReport,
    OpeReport,
    UpliftModelCard,
    UpliftValidationReport,
)
from engine.uplift.flow import UPLIFT_HOLDOUT_FILENAME, check_seed, model_card_key, read_holdout
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from datetime import datetime

    import pandas as pd

    from engine.contracts import ValidationReport

router: APIRouter = APIRouter(tags=["uplift"])

_LOGGER = get_logger(__name__)

__all__ = ["router"]

VALIDATION_FAILED: Final[str] = "VALIDATION_FAILED"
UPLIFT_VALIDATION_FAILED: Final[str] = "UPLIFT_VALIDATION_FAILED"
RUN_NOT_SCORED: Final[str] = "RUN_NOT_SCORED"
RUN_NOT_UPLIFT: Final[str] = "RUN_NOT_UPLIFT"
CAMPAIGN_RESULTS_INVALID: Final[str] = "CAMPAIGN_RESULTS_INVALID"
CAMPAIGN_RESULTS_NOT_FOUND: Final[str] = "CAMPAIGN_RESULTS_NOT_FOUND"
UPLOAD_MODE_MISMATCH: Final[str] = "UPLOAD_MODE_MISMATCH"

_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}
_RUN_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": UpliftValidationErrorResponse},
    422: {"model": ErrorResponse},
}
_MEASURE_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

UseCaseQuery = Annotated[str, Query(description="Use case whose treatment settings apply.")]


# ---------------------------------------------------------------------------
# GET /uploads/{upload_id}/treatment-candidates
# ---------------------------------------------------------------------------
@router.get(
    "/uploads/{upload_id}/treatment-candidates",
    response_model=TreatmentCandidatesResponse,
    responses=_NOT_FOUND,
    summary="The 0/1 columns of an upload that could record who was treated",
)
def treatment_candidates(
    upload_id: str, use_case: UseCaseQuery, root: ConfigRootDep, storage: StorageDep
) -> TreatmentCandidatesResponse:
    """Every column holding only 0/1 values with both present, and the one the checks would pick.

    The use case's outcome column is left out even when it is 0/1: an outcome is never a treatment.
    `detected` is the uplift checks' own choice (`detect_treatment_column`), so what the picker
    preselects is what the run will be checked against.
    """
    from engine.uplift.data import coerce_treatment, detect_treatment_column

    config = use_case_config(use_case, root)
    upload = load_upload(storage, upload_id)
    frame = read_frame(storage, upload.source_key, upload.file_format, profile_row_cap(config))
    uplift = config.uplift
    wanted = {name.lower() for name in (*uplift.treatment_column_hints, uplift.treatment_column) if name}
    candidates: list[TreatmentCandidate] = []
    for column in (str(name) for name in frame.columns):
        if column == config.target.column:
            continue
        values, _bad = coerce_treatment(frame[column])
        if values is None or len(values) == 0:
            continue
        share = float(values.mean())
        if not 0.0 < share < 1.0:
            continue
        candidates.append(
            TreatmentCandidate(column=column, treated_share=share, hinted=column.lower() in wanted)
        )
    detected = detect_treatment_column([str(name) for name in frame.columns], uplift)
    return TreatmentCandidatesResponse(candidates=tuple(candidates), detected=detected)


# ---------------------------------------------------------------------------
# POST /uplift/runs
# ---------------------------------------------------------------------------
@router.post(
    "/uplift/runs",
    response_model=RunCreatedResponse,
    status_code=202,
    responses=_RUN_ERRORS,
    summary="Validate an upload as an experiment and, when it passes, start an uplift training run",
)
def create_uplift_run(
    body: UpliftRunRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    registry: RegistryDep,
    jobs: JobsDep,
    settings: SettingsDep,
    response: Response,
    request: Request,
) -> RunCreatedResponse | JSONResponse:
    """Phase 1's checks and the six uplift checks, synchronously; `409` with both reports, or `202`."""
    from engine.uplift.checks import run_uplift_checks

    configured = use_case_config(body.use_case, root)  # 404 for a planned or unknown id, before any read
    resolved = resolve_config(body.use_case, _uplift_overrides(body, configured.problem_type), root=root)
    config = resolved.config
    catalog = get_catalog(root)
    upload = load_upload(storage, body.upload_id)
    if upload.mode is not RunMode.TRAIN:
        raise http_error(
            409,
            UPLOAD_MODE_MISMATCH,
            f"This file was uploaded for {upload.mode.value}. Upload it again for train.",
        )
    profile = load_upload_profile(storage, body.upload_id)
    frame = read_frame(storage, upload.source_key, upload.file_format, profile_row_cap(config))
    report = validate.validate_for_training(
        frame,
        config,
        primary_key=body.primary_key,
        target=body.target,
        acknowledged=config.validation.acknowledged,
        upload_id=upload.upload_id,
        row_count=profile.row_count,
    )
    checked = run_uplift_checks(
        frame,
        config,
        primary_key=body.primary_key,
        target=body.target,
        upload_id=upload.upload_id,
        acknowledged=config.validation.acknowledged,
        seed=check_seed(upload.upload_id),
    )
    storage.write_model(upload_key(upload.upload_id, UPLOAD_VALIDATION_FILENAME), report)
    storage.write_model(upload_key(upload.upload_id, UPLIFT_VALIDATION_FILENAME), checked.report)
    if not (report.passed and checked.report.passed):
        return _validation_conflict(report, checked.report)

    record = create_run(
        storage,
        Pipeline(storage, registry, jobs),
        resolved=resolved,
        catalog=catalog,
        upload=upload,
        profile=profile,
        report=report,
        mode=RunMode.TRAIN,
        primary_key=body.primary_key,
        target=body.target,
        model_choice=config.uplift.learner.value,
        model_version_id=None,
        requested_by=requested_by(request),
    )
    storage.write_model(
        run_key(record.run_id, UPLIFT_VALIDATION_FILENAME),
        checked.report.model_copy(update={"run_id": record.run_id}),
    )
    spec = job_spec_for(record, upload=upload, client_id=settings.client_id)
    write_job_spec(storage, spec)
    jobs.submit(spec.job_id, build_job_fn(spec, storage=storage, registry=registry))
    response.headers["Location"] = f"/runs/{record.run_id}"
    return RunCreatedResponse(run_id=record.run_id)


def _uplift_overrides(body: UpliftRunRequest, configured: ProblemType) -> dict[str, Any]:
    """The caller's overrides, with the two that make this an uplift run applied last.

    `problem_type` goes last so a stray override cannot turn an uplift request into something else;
    `engine.config` normalises the nested and dotted spellings together (DEC-039 re-derives the
    metric for the new problem type, so `model_search.metric` becomes `auuc`). On a use case that is
    already *configured* as uplift the problem type is not overridden at all - any caller override
    of it is dropped instead - so the run records it as the use case's own setting, which is what
    lets the uplift model take the use case's empty champion slot (DEC-609).
    """
    overrides: dict[str, Any] = dict(body.overrides)
    if configured is ProblemType.UPLIFT:
        overrides.pop("problem_type", None)
    else:
        overrides["problem_type"] = ProblemType.UPLIFT.value
    if body.treatment_column is not None:
        overrides["uplift.treatment_column"] = body.treatment_column
    return overrides


def _validation_conflict(report: ValidationReport, uplift: UpliftValidationReport) -> JSONResponse:
    """The `409`: M1's envelope, the Phase 1 report and the uplift report, side by side."""
    if not report.passed:
        count = report.error_count
        code, message = VALIDATION_FAILED, (
            f"{count} problem{'s' if count != 1 else ''} must be fixed before this data can be used."
        )
    else:
        blocking = [
            check.code
            for check in uplift.checks
            if check.severity is Severity.ERROR and not check.acknowledged
        ]
        code = UPLIFT_VALIDATION_FAILED
        message = (
            f"This file cannot be used as an experiment yet: {', '.join(blocking)}."
            if blocking
            else "This file cannot be used as an experiment yet."
        )
    body = UpliftValidationErrorResponse(
        detail=ErrorBody(code=code, message=message, path=None), validation=report, uplift_validation=uplift
    )
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/uplift/{name}
# ---------------------------------------------------------------------------
@router.get(
    "/runs/{run_id}/uplift/{name}",
    response_class=Response,
    responses=_NOT_FOUND,
    summary="One uplift artefact of a run, whitelisted against the uplift artefact registry",
)
def read_uplift_artefact(run_id: str, name: str, storage: StorageDep) -> Response:
    """A whitelist, not a path join: only `UPLIFT_ARTEFACTS` names are ever read."""
    if not ARTEFACT_NAME.fullmatch(name) or name not in UPLIFT_ARTEFACTS:
        raise http_error(404, "ARTEFACT_UNKNOWN", f"There is no uplift artefact called {name!r}.")
    load_run(storage, run_id)
    try:
        payload = storage.read_bytes(run_key(run_id, name))
    except StorageError as exc:
        raise http_error(404, "ARTEFACT_NOT_FOUND", f"This run has not produced {name!r}.") from exc
    return Response(content=payload, media_type="application/json")


# ---------------------------------------------------------------------------
# POST / GET /runs/{run_id}/campaign-results
# ---------------------------------------------------------------------------
@router.post(
    "/runs/{run_id}/campaign-results",
    response_model=IncrementalityReport,
    responses=_MEASURE_ERRORS,
    summary="Measure a scoring run's campaign from an uploaded outcomes file",
)
def create_campaign_results(
    run_id: str, body: CampaignResultsRequest, storage: StorageDep
) -> IncrementalityReport:
    """Treated rate minus control rate on the mature rows, stored as `incrementality_report.json`."""
    import pandas as pd

    from engine.uplift.incrementality import measure_incrementality

    record = load_run(storage, run_id)
    treatment_time = _finished_scoring_run(record)
    try:
        scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, export.SCORES_PARQUET))))
    except StorageError as exc:
        raise http_error(409, RUN_NOT_SCORED, "This run has no scores file to measure against.") from exc
    uplift_run = INTENDED_TREATMENT_COLUMN in scores.columns
    if uplift_run and body.bands is not None:
        raise http_error(
            422,
            CAMPAIGN_RESULTS_INVALID,
            "An uplift run is measured within the customers its policy intended to treat; bands do "
            "not apply to it.",
            path="bands",
        )
    upload = load_upload(storage, body.upload_id)
    outcomes = _read_all(storage, upload.source_key, upload.file_format)
    try:
        report = measure_incrementality(
            scores,
            outcomes,
            run_id=run_id,
            primary_key=sole_key(record.primary_key, what="A run"),
            outcome_column=body.outcome_column,
            positive_label=body.positive_label,
            intended_column=INTENDED_TREATMENT_COLUMN if uplift_run else None,
            bands=None if uplift_run else body.bands,
            treatment_time=treatment_time,
            treatment_date_column=body.treatment_date_column,
            outcome_window_days=body.outcome_window_days,
            as_of=body.as_of or utc_now(),
            campaign_id=body.campaign_id,
        )
    except ValueError as exc:
        raise http_error(422, CAMPAIGN_RESULTS_INVALID, str(exc)) from exc
    storage.write_model(run_key(run_id, INCREMENTALITY_FILENAME), report)
    _LOGGER.info(
        "campaign-results: run=%s status=%s treated=%d control=%d immature=%d",
        run_id,
        report.status.value,
        report.treated_rows,
        report.control_rows,
        report.rows_immature,
    )
    return report


@router.get(
    "/runs/{run_id}/campaign-results",
    response_model=IncrementalityReport,
    responses=_NOT_FOUND,
    summary="The stored campaign results of a scoring run",
)
def read_campaign_results(run_id: str, storage: StorageDep) -> Response:
    """The stored `incrementality_report.json`, byte for byte, or `404` before any was measured."""
    load_run(storage, run_id)
    try:
        payload = storage.read_bytes(run_key(run_id, INCREMENTALITY_FILENAME))
    except StorageError as exc:
        raise http_error(
            404, CAMPAIGN_RESULTS_NOT_FOUND, "No campaign results have been measured for this run yet."
        ) from exc
    return Response(content=payload, media_type="application/json")


def _finished_scoring_run(record: RunRecord) -> datetime:
    """The run's finish time - the treatment time of its campaign - or a 409 saying why there is none."""
    if record.mode is not RunMode.SCORE or record.state is not RunState.DONE or record.finished_at is None:
        raise http_error(
            409,
            RUN_NOT_SCORED,
            "Campaign results are measured against a finished scoring run: its scores say who was "
            "treated and who was held out.",
        )
    return record.finished_at


def _read_all(storage: Storage, key: str, file_format: Literal["csv", "parquet"]) -> pd.DataFrame:
    """Every row of an uploaded file; a profiling-capped first read is repeated at the exact count."""
    try:
        read = ingest.read_upload(storage, key, file_format=file_format)
        if read.truncated:
            read = ingest.read_upload(storage, key, file_format=file_format, row_cap=read.row_count)
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc
    return read.frame


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/uplift/ope
# ---------------------------------------------------------------------------
@router.post(
    "/runs/{run_id}/uplift/ope",
    response_model=OpeReport,
    responses=_MEASURE_ERRORS,
    summary="Off-policy estimate of a targeting rule on an uplift training run's hold-out",
)
def create_ope(run_id: str, body: OpeRequest, storage: StorageDep) -> OpeReport:
    """IPS, SNIPS and DR of "treat by this rule", from the logged, randomised hold-out.

    The hold-out predictions come from a model that never saw those rows, which is what the doubly
    robust estimate needs from its outcome model. The logging propensity is the treated share the
    model was trained on - constant, because the assignment was random.
    """
    import numpy as np

    from engine.uplift.ope import evaluate_policy, policy_from_rule

    record = load_run(storage, run_id)
    if record.mode is not RunMode.TRAIN or record.state is not RunState.DONE:
        raise http_error(409, RUN_NOT_UPLIFT, "Off-policy evaluation needs a finished uplift training run.")
    try:
        holdout = read_holdout(storage, run_key(run_id, UPLIFT_HOLDOUT_FILENAME))
        card = storage.read_model(model_card_key(predictor_key_for(run_id)), UpliftModelCard)
    except StorageError as exc:
        raise http_error(
            409, RUN_NOT_UPLIFT, "This run did not train an uplift model, so it has no hold-out to replay."
        ) from exc
    policy, description = policy_from_rule(
        np.asarray(holdout["uplift"], dtype=np.float64), top_share=body.top_share, min_uplift=body.min_uplift
    )
    try:
        report = evaluate_policy(
            np.asarray(holdout["t"], dtype=np.int_),
            np.asarray(holdout["y"], dtype=np.int_),
            policy,
            p_treated=np.asarray(holdout["p_treated"], dtype=np.float64),
            p_control=np.asarray(holdout["p_control"], dtype=np.float64),
            propensity=card.propensity,
            run_id=run_id,
            description=description,
            causal=card.causal,
        )
    except ValueError as exc:
        raise http_error(422, "OPE_INVALID", str(exc)) from exc
    storage.write_model(run_key(run_id, OPE_FILENAME), report)
    return report
