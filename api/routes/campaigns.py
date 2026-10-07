"""Campaigns: the one record a campaign's measurement attaches to, and its registered test plan (Plan J M94).

A scoring run makes a list; a **campaign** is that list going out on a given day. These routes create
one from a finished scoring run, give it its outcomes, measure it through the one measurement path
(`engine.measurement.measure.measure_campaign`, which `/runs/{id}/campaign-results` also uses) and
fix how it will be judged before it is (`engine.measurement.plan`):

* `POST /campaigns {run_id, treatment_start?, bands?, outcome_window_days?, name?}` (Analyst) - kind
  `scored`. The assignment (`campaigns/<id>/assignment.parquet`) is built from the run's scores:
  holdout, intended population (an uplift run's `intended_treatment`, or the treat bands), band or
  segment. `treatment_start` is the day the campaign really went out, defaulting to the run's finish
  time and never before it (DEC-1304 (d)), so a campaign sent days later matures on the right date.
* `GET /campaigns` and `GET /campaigns/{id}` (Viewer) - the record, its stored report, the plain
  verdict (none for an early look) and the test plan in force.
* `POST /campaigns/{id}/outcomes {upload_id, ...}` (Analyst) - copies the key, the outcome and the
  optional treatment date of an uploaded file into `campaigns/<id>/outcomes.parquet`.
* `POST /campaigns/{id}/measure {as_of?, ...}` (Analyst) - `as_of` defaults to now *here*, never in
  the engine, and a later one is **422 `CAMPAIGN_INVALID`**: a moment that has not happened would
  call open outcome windows closed and an early look final. While any customer's outcome window is
  still open the answer is **409 `CAMPAIGN_NOT_MATURED`** with `results_available_on`, and nothing is
  stored: a campaign result is never a partial number (DEC-1304 (e)). A measurement that differs from
  the registered plan is **409 `TEST_PLAN_CHANGED`**; the covariate defaults to the plan's, so only a
  different one named in the body is a change.
* `POST /campaigns/{id}/plan` (Analyst) freezes a `TestPlan` with its `plan_hash`; the audit event's
  `after_hash` is `content_hash(plan)` and its details carry `plan_hash` (DEC-1304 (g)). The same
  plan again returns the stored one; a different one is **409 `TEST_PLAN_EXISTS`**. `POST
  /campaigns/{id}/plan/amendments {reason, ...}` writes version n+1 with `amends` set; every version
  is kept and `GET /campaigns/{id}/plan` lists them. Once a final result has been read, the plan
  can no longer be amended (**409 `TEST_PLAN_INVALID`**): that result was judged by the plan in force.
* `GET /campaigns/{id}/plan-preview?holdout=` (Viewer) - the computed points behind the "Plan the test"
  slider: M93's planner (`engine.measurement.planner.power_preview`) at each control-group share, on
  the campaign's own measured population, beside its realised counts. Nothing is stored, nothing is
  interpolated: the slider steps through these points only (DEC-1204).

**Holdout epochs (M92).** `POST /campaigns` records the scoring run's holdout (scope, key and epoch,
from `holdout_assignment.json`; a run that wrote none drew per run). `POST /campaigns/{id}/measure`
refuses **409 `CAMPAIGN_EPOCH_MISMATCH`** when the runs behind the assignment span two epochs, when
the run's epoch no longer matches the record, or when the persistent holdout was redrawn before the
outcomes were all in (`engine.measurement.campaign.epoch_mismatch`).

Customer ids are never in a URL or an audit record: every route names a campaign, a run or an upload.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, Field, ValidationError

from api.access import get_platform_engine, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.measure import MEASURE_NOT_OFFERED
from api.routes.runs import load_run, requested_by
from api.routes.uplift import RUN_NOT_SCORED, _finished_scoring_run, _read_all
from api.routes.uploads import http_error, load_upload, use_case_config
from api.schemas import ErrorBody, ErrorResponse
from engine.access.roles import Role
from engine.audit.events import content_hash
from engine.config import ConfigError, StrictBase, UseCaseConfig, key_columns
from engine.holdout.assign import run_holdout_spec
from engine.holdout.salt import HoldoutLedger
from engine.holdout.spec import PERSISTENT_SCOPES
from engine.measurement.campaign import (
    ASSIGNMENT_FILENAME,
    CAMPAIGN_EPOCH_MISMATCH,
    CAMPAIGN_FILENAME,
    CAMPAIGN_NOT_FOUND,
    CAMPAIGN_NOT_MATURED,
    CAMPAIGN_OUTCOMES_MISSING,
    INTENDED_COLUMN,
    OUTCOMES_FILENAME,
    REPORT_FILENAME,
    TEST_PLAN_FILENAME,
    Campaign,
    CampaignKind,
    CampaignOutcomes,
    CampaignStatus,
    CampaignStore,
    SqlCampaignStore,
    assignment_counts,
    build_assignment,
    campaign_key,
    epoch_mismatch,
    holdout_identity,
    new_campaign_id,
    read_frame,
    save_campaign,
    test_plan_version_filename,
    write_frame,
)
from engine.measurement.measure import campaign_verdict_for, measure_campaign
from engine.measurement.plan import (
    TEST_PLAN_CHANGED,
    TEST_PLAN_EXISTS,
    TEST_PLAN_INVALID,
    TEST_PLAN_NOT_FOUND,
    TestPlan,
    TestPlanAmendment,
    TestPlanChangedError,
    TestPlanInput,
    freeze_plan,
    plan_inputs,
    realised_population,
)
from engine.measurement.planner import (
    MAX_HOLDOUT_SHARE,
    PowerPreviewPoint,
    PowerPreviewRequest,
    power_preview,
)
from engine.pilot.roi import outcome_is_good_by_default
from engine.stages import export
from engine.storage import Storage, StorageError, run_key
from engine.uplift.contracts import IncrementalityReport, IncrementalityStatus
from engine.uplift.measure import CampaignVerdict, detect_outcome_column, measure_offered
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "CAMPAIGN_INVALID",
    "CampaignCreateRequest",
    "CampaignListResponse",
    "CampaignMeasureRequest",
    "CampaignNotMaturedResponse",
    "CampaignOutcomesRequest",
    "CampaignPlanPreview",
    "CampaignView",
    "TestPlanView",
    "get_campaign_store",
    "router",
]

router: APIRouter = APIRouter(tags=["campaigns"])

_LOGGER = get_logger(__name__)

CAMPAIGN_INVALID: Final[str] = "CAMPAIGN_INVALID"
CAMPAIGNS_SHOWN: Final[int] = 100
PREVIEW_SHARES: Final[tuple[float, ...]] = (0.02, 0.03, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50)
"""The control-group shares the plan preview computes when the request names none (the campaign's own
realised share is always added, so the slider can start on it)."""


def _on_campaign(role: Role, action: str, purpose: str) -> RoutePolicy:
    """A policy for a route whose path names one campaign (its id is the audit event's object)."""
    return RoutePolicy(
        role=role, action=action, purpose=purpose, object_type="campaign", object_param="campaign_id"
    )


register(
    {
        ("POST", "/campaigns"): RoutePolicy(
            role=Role.ANALYST, action="campaigns.create", purpose="record a campaign", object_type="campaign"
        ),
        ("GET", "/campaigns"): RoutePolicy(
            role=Role.VIEWER, action="campaigns.list", purpose="see campaigns"
        ),
        ("GET", "/campaigns/{campaign_id}"): _on_campaign(Role.VIEWER, "campaigns.read", "see a campaign"),
        ("POST", "/campaigns/{campaign_id}/outcomes"): _on_campaign(
            Role.ANALYST, "campaigns.outcomes", "add a campaign's outcomes"
        ),
        ("POST", "/campaigns/{campaign_id}/measure"): _on_campaign(
            Role.ANALYST, "campaigns.measure", "measure a campaign"
        ),
        ("GET", "/campaigns/{campaign_id}/plan"): _on_campaign(
            Role.VIEWER, "campaigns.plan_read", "see a campaign's test plan"
        ),
        ("GET", "/campaigns/{campaign_id}/plan-preview"): _on_campaign(
            Role.VIEWER, "campaigns.plan_preview", "preview a campaign's test sizes"
        ),
        ("POST", "/campaigns/{campaign_id}/plan"): _on_campaign(
            Role.ANALYST, "campaigns.plan", "register a campaign's test plan"
        ),
        ("POST", "/campaigns/{campaign_id}/plan/amendments"): _on_campaign(
            Role.ANALYST, "campaigns.plan_amend", "amend a campaign's test plan"
        ),
    }
)

_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


# ---------------------------------------------------------------------------
# Bodies and answers
# ---------------------------------------------------------------------------
class CampaignCreateRequest(StrictBase):
    """Body of `POST /campaigns`: the scoring run whose list went out, and when it went out."""

    run_id: str = Field(description="The finished scoring run whose list the campaign sent.")
    treatment_start: AwareDatetime | None = Field(
        default=None, description="When the campaign actually went out; the run's finish time when null."
    )
    bands: tuple[str, ...] | None = Field(
        default=None,
        description="The bands the campaign treated, for a propensity run; every eligible customer when null.",
    )
    outcome_window_days: int | None = Field(
        default=None, ge=0, description="Days the outcome is counted over; the use case's own when null."
    )
    name: str | None = Field(
        default=None, max_length=120, description="A short name; one is made up when null."
    )


class CampaignOutcomesRequest(StrictBase):
    """Body of `POST /campaigns/{id}/outcomes`: the uploaded outcomes file and how to read it."""

    upload_id: str = Field(description="Upload holding the customer id and the outcome after the campaign.")
    outcome_column: str | None = Field(
        default=None, description="The outcome column; found in the file when null."
    )
    positive_label: str | None = Field(default=None, description="Outcome value that counts as a conversion.")
    treatment_date_column: str | None = Field(
        default=None,
        description="Per-row treatment date in the file; the campaign's treatment start when null.",
    )


class CampaignMeasureRequest(StrictBase):
    """Body of `POST /campaigns/{id}/measure`; everything has a default."""

    as_of: AwareDatetime | None = Field(
        default=None, description="Reference time for maturity; now when null, and never later than now."
    )
    outcome_window_days: int | None = Field(
        default=None, ge=0, description="The outcome window to measure with; the campaign's own when null."
    )
    covariate_column: str | None = Field(
        default=None,
        description="The covariate to adjust with (M102); the test plan's when null, checked against it now.",
    )


class CampaignView(StrictBase):
    """One campaign as its page shows it."""

    campaign: Campaign
    report: IncrementalityReport | None = Field(default=None, description="The stored report, if measured.")
    verdict: CampaignVerdict | None = Field(
        default=None, description="Its plain verdict; null before measurement and for an early look."
    )
    outcome_is_good: bool = Field(description="False when the campaign exists to make the outcome rarer.")
    plan: TestPlan | None = Field(default=None, description="The registered test plan in force.")


class CampaignListResponse(StrictBase):
    """`GET /campaigns`: newest first."""

    campaigns: tuple[Campaign, ...]


class TestPlanView(StrictBase):
    """`GET /campaigns/{id}/plan`: the plan in force and every version, oldest first."""

    __test__ = False

    plan: TestPlan | None = Field(
        default=None, description="The version in force; null before one is registered."
    )
    versions: tuple[TestPlan, ...] = Field(default=(), description="Every registered version, oldest first.")


class CampaignNotMaturedResponse(StrictBase):
    """The 409 of `POST /campaigns/{id}/measure` while outcome windows are still open: a date, no number."""

    detail: ErrorBody
    results_available_on: date | None = Field(description="The day every customer's outcome is in.")


_MEASURE_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": CampaignNotMaturedResponse},
    422: {"model": ErrorResponse},
}

CampaignQuery = Annotated[str | None, Query(description="Keep only this scoring run's campaigns.")]
UseCaseQuery = Annotated[str | None, Query(description="Keep only this use case's campaigns.")]


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def get_campaign_store(request: Request) -> CampaignStore:
    """The campaign store: `app.state.campaign_store` when a test put one there, else the platform database's."""
    existing = getattr(request.app.state, "campaign_store", None)
    if existing is not None:
        store: CampaignStore = existing
        return store
    fresh = SqlCampaignStore(get_platform_engine(request))
    request.app.state.campaign_store = fresh
    return fresh


def _load(store: CampaignStore, campaign_id: str) -> Campaign:
    campaign = store.get(campaign_id)
    if campaign is None:
        raise http_error(404, CAMPAIGN_NOT_FOUND, f"No campaign with id {campaign_id!r}.")
    return campaign


# ---------------------------------------------------------------------------
# POST /campaigns
# ---------------------------------------------------------------------------
@router.post(
    "/campaigns",
    response_model=CampaignView,
    status_code=201,
    responses=_ERRORS,
    summary="Record a campaign sent from a finished scoring run's list",
)
def create_campaign(
    body: CampaignCreateRequest,
    request: Request,
    response: Response,
    root: ConfigRootDep,
    storage: StorageDep,
) -> CampaignView:
    import pandas as pd

    record = load_run(storage, body.run_id)
    finished_at = _finished_scoring_run(record)
    config = use_case_config(record.use_case_id, root)
    if not measure_offered(config):
        raise http_error(
            409,
            MEASURE_NOT_OFFERED,
            f"{config.name} holds nobody back or contacts nobody, so it has no campaign.",
        )
    if body.treatment_start is not None and body.treatment_start < finished_at:
        raise http_error(
            422,
            CAMPAIGN_INVALID,
            "A campaign cannot go out before its list was made: give a treatment start on or after the run finished.",
            path="treatment_start",
        )
    try:
        scores = pd.read_parquet(_bytes(storage, run_key(record.run_id, export.SCORES_PARQUET)))
    except StorageError as exc:
        raise http_error(
            409, RUN_NOT_SCORED, "This run has no scores file to build a campaign from."
        ) from exc
    try:
        assignment = build_assignment(scores, primary_key=record.primary_key, bands=body.bands)
    except ValueError as exc:
        raise http_error(422, CAMPAIGN_INVALID, str(exc), path="bands" if body.bands else None) from exc
    counts = assignment_counts(assignment)
    holdout_scope, holdout_key, holdout_epoch = holdout_identity(run_holdout_spec(storage, record.run_id))
    uplift_run = "intended_treatment" in scores.columns
    start = body.treatment_start or finished_at
    window = body.outcome_window_days if body.outcome_window_days is not None else _default_window(config)
    now = utc_now()
    campaign = Campaign(
        campaign_id=new_campaign_id(now),
        kind=CampaignKind.SCORED,
        name=body.name or f"{config.name}, sent {start.day} {start:%b %Y}",
        use_case_id=record.use_case_id,
        run_ids=(record.run_id,),
        primary_key=record.primary_key,
        treatment_start=start,
        treatment_start_source="entered" if body.treatment_start is not None else "run_finished",
        outcome_window_days=window,
        population="intended" if uplift_run else ("bands" if body.bands is not None else "eligible"),
        bands=body.bands,
        causal=counts.holdout > 0,
        causal_basis="engine_random" if counts.holdout > 0 else "not_random",
        holdout_scope=holdout_scope,
        holdout_scope_key=holdout_key,
        holdout_epoch=holdout_epoch,
        counts=counts,
        status=CampaignStatus.LIVE,
        created_at=now,
        created_by=requested_by(request) or "local",
    )
    assignment_key = campaign_key(campaign.campaign_id, ASSIGNMENT_FILENAME)
    write_frame(storage, assignment_key, assignment)
    try:
        save_campaign(get_campaign_store(request), storage, campaign, create=True)
    except Exception:
        # Without its record the privacy jobs cannot read the assignment's key or date: never leave one.
        _discard(storage, assignment_key, campaign_key(campaign.campaign_id, CAMPAIGN_FILENAME))
        raise
    set_audit_context(request, object_id=campaign.campaign_id, details={"run_id": record.run_id})
    response.headers["Location"] = f"/campaigns/{campaign.campaign_id}"
    _LOGGER.info(
        "campaigns.create campaign=%s run=%s rows=%d intended=%d holdout=%d",
        campaign.campaign_id,
        record.run_id,
        counts.rows,
        counts.intended,
        counts.intended_holdout,
    )
    return _view(storage, campaign, root)


def _discard(storage: Storage, *keys: str) -> None:
    for key in keys:
        try:
            storage.delete(key)
        except (StorageError, OSError):  # already gone, or never written
            continue


def _default_window(config: UseCaseConfig) -> int | None:
    """The use case's outcome window, as step 4 reads it (`api.routes.measure`)."""
    return config.uplift.outcome_window_days or (
        config.label.horizon_days if config.label is not None else None
    )


def _bytes(storage: Storage, key: str) -> Any:
    import io

    return io.BytesIO(storage.read_bytes(key))


# ---------------------------------------------------------------------------
# GET /campaigns, GET /campaigns/{id}
# ---------------------------------------------------------------------------
@router.get("/campaigns", response_model=CampaignListResponse, summary="Campaigns, newest first")
def list_campaigns(
    request: Request, run_id: CampaignQuery = None, use_case: UseCaseQuery = None
) -> CampaignListResponse:
    store = get_campaign_store(request)
    return CampaignListResponse(
        campaigns=store.list(run_id=run_id, use_case_id=use_case, limit=CAMPAIGNS_SHOWN)
    )


@router.get(
    "/campaigns/{campaign_id}",
    response_model=CampaignView,
    responses={404: {"model": ErrorResponse}},
    summary="One campaign: its record, its measured result and its test plan",
)
def read_campaign(
    campaign_id: str, request: Request, root: ConfigRootDep, storage: StorageDep
) -> CampaignView:
    return _view(storage, _load(get_campaign_store(request), campaign_id), root)


def _view(storage: Storage, campaign: Campaign, root: Any) -> CampaignView:
    report = _stored(storage, campaign_key(campaign.campaign_id, REPORT_FILENAME), IncrementalityReport)
    good, label = _direction(campaign, root)
    verdict = (
        campaign_verdict_for(report, outcome_is_good=good, outcome_label=label)
        if report is not None
        else None
    )
    return CampaignView(
        campaign=campaign,
        report=report,
        verdict=verdict,
        outcome_is_good=good,
        plan=_stored(storage, campaign_key(campaign.campaign_id, TEST_PLAN_FILENAME), TestPlan),
    )


def _direction(campaign: Campaign, root: Any) -> tuple[bool, str | None]:
    """Which way round the outcome counts, and its words: exactly as step 4 decides (`api.routes.measure`).

    A column found in the outcomes file is taken to be the use case's own outcome, whatever the file
    calls it; a column the person named is judged by its own name.
    """
    if campaign.use_case_id is None:
        return True, None
    outcomes = campaign.outcomes
    named = outcomes.outcome_column if outcomes is not None and outcomes.outcome_named else None
    try:
        config = use_case_config(campaign.use_case_id, root)
    except (ConfigError, HTTPException):  # the use case has gone: judge the column by its own name
        column = outcomes.outcome_column if outcomes is not None else ""
        return outcome_is_good_by_default(campaign.use_case_id, column, root), None
    good = outcome_is_good_by_default(config.id, named or _target_of(config), root)
    return good, config.target.definition or None


def _stored(storage: Storage, key: str, model: type[Any]) -> Any:
    try:
        return storage.read_model(key, model)
    except StorageError:
        return None


# ---------------------------------------------------------------------------
# POST /campaigns/{id}/outcomes
# ---------------------------------------------------------------------------
@router.post(
    "/campaigns/{campaign_id}/outcomes",
    response_model=CampaignView,
    responses=_ERRORS,
    summary="Give a campaign its outcomes file: customer id, outcome and optionally a treatment date",
)
def add_campaign_outcomes(
    campaign_id: str,
    body: CampaignOutcomesRequest,
    request: Request,
    root: ConfigRootDep,
    storage: StorageDep,
) -> CampaignView:
    store = get_campaign_store(request)
    campaign = _load(store, campaign_id)
    upload = load_upload(storage, body.upload_id)
    frame = _read_all(storage, upload.source_key, upload.file_format)
    columns = [str(name) for name in frame.columns]
    config = _config(campaign, root)
    try:
        outcome = body.outcome_column or detect_outcome_column(
            columns,
            primary_key=campaign.primary_key,
            target_column=_target_of(config),
            label_name=config.label.name if config is not None and config.label is not None else None,
        )
    except ValueError as exc:
        raise http_error(422, CAMPAIGN_INVALID, str(exc), path="upload_id") from exc
    wanted = [*key_columns(campaign.primary_key), outcome]
    if body.treatment_date_column is not None:
        wanted.append(body.treatment_date_column)
    missing = [name for name in wanted if name not in columns]
    if missing:
        raise http_error(
            422, CAMPAIGN_INVALID, f"The outcomes file has no column {', '.join(repr(m) for m in missing)}."
        )
    kept = frame[list(dict.fromkeys(wanted))]
    try:
        write_frame(storage, campaign_key(campaign_id, OUTCOMES_FILENAME), kept)
    except (ValueError, TypeError) as exc:  # a column pyarrow cannot store as one type
        raise http_error(
            422, CAMPAIGN_INVALID, "The outcomes file mixes kinds of value in one column."
        ) from exc
    updated = campaign.model_copy(
        update={
            "outcomes": CampaignOutcomes(
                upload_id=upload.upload_id,
                file_name=upload.file_name,
                outcome_column=outcome,
                outcome_named=body.outcome_column is not None,
                positive_label=body.positive_label,
                treatment_date_column=body.treatment_date_column,
                rows=len(kept.index),
                added_at=utc_now(),
            )
        }
    )
    save_campaign(store, storage, updated)
    _LOGGER.info("campaigns.outcomes campaign=%s rows=%d", campaign_id, len(kept.index))
    return _view(storage, updated, root)


def _config(campaign: Campaign, root: Any) -> UseCaseConfig | None:
    return use_case_config(campaign.use_case_id, root) if campaign.use_case_id is not None else None


def _target_of(config: UseCaseConfig | None) -> str:
    """The use case's outcome column, as step 4 names it (`api.routes.measure._target`)."""
    return (config.target.column if config is not None else None) or "outcome"


# ---------------------------------------------------------------------------
# POST /campaigns/{id}/measure
# ---------------------------------------------------------------------------
@router.post(
    "/campaigns/{campaign_id}/measure",
    response_model=CampaignView,
    responses=_MEASURE_ERRORS,
    summary="Measure a campaign through the one measurement path, against its test plan",
)
def measure_campaign_results(
    campaign_id: str, body: CampaignMeasureRequest, request: Request, root: ConfigRootDep, storage: StorageDep
) -> CampaignView | JSONResponse:
    now = utc_now()
    if body.as_of is not None and body.as_of > now:
        raise http_error(
            422,
            CAMPAIGN_INVALID,
            "A campaign can only be measured as of now or an earlier moment, not a later one.",
            path="as_of",
        )
    store = get_campaign_store(request)
    campaign = _load(store, campaign_id)
    if campaign.outcomes is None:
        raise http_error(409, CAMPAIGN_OUTCOMES_MISSING, "Add the campaign's outcomes file first.")
    assignment = read_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
    outcomes = read_frame(storage, campaign_key(campaign_id, OUTCOMES_FILENAME))
    plan = _stored(storage, campaign_key(campaign_id, TEST_PLAN_FILENAME), TestPlan)
    window = (
        body.outcome_window_days if body.outcome_window_days is not None else campaign.outcome_window_days
    )
    as_of = body.as_of or now
    mismatch = _epoch_mismatch(request, storage, campaign, window=window, as_of=as_of)
    if mismatch is not None:
        set_audit_context(request, details={"reason_code": CAMPAIGN_EPOCH_MISMATCH})
        raise http_error(409, CAMPAIGN_EPOCH_MISMATCH, mismatch)
    # The pre-registered covariate is measured with unless another one is named: leaving it out of
    # the body is not a change of plan.
    covariate = body.covariate_column
    if covariate is None and plan is not None:
        covariate = plan.covariate_column
    try:
        report = measure_campaign(
            assignment,
            outcomes,
            run_id=campaign.run_ids[0] if campaign.run_ids else campaign_id,
            primary_key=campaign.primary_key,
            outcome_column=campaign.outcomes.outcome_column,
            positive_label=campaign.outcomes.positive_label,
            intended_column=INTENDED_COLUMN,
            treatment_time=campaign.treatment_start,
            treatment_date_column=campaign.outcomes.treatment_date_column,
            outcome_window_days=window,
            as_of=as_of,
            campaign_id=campaign_id,
            plan=plan,
            covariate_column=covariate,
        )
    except TestPlanChangedError as exc:
        set_audit_context(request, details={"reason_code": TEST_PLAN_CHANGED})
        raise http_error(409, TEST_PLAN_CHANGED, str(exc)) from exc
    except ValueError as exc:
        raise http_error(422, CAMPAIGN_INVALID, str(exc)) from exc
    if report.status is IncrementalityStatus.IMMATURE or report.rows_immature:
        set_audit_context(request, details={"reason_code": CAMPAIGN_NOT_MATURED})
        when = report.results_available_on
        message = (
            f"Some customers are still inside the outcome window: measure again on or after {when.isoformat()}."
            if when is not None
            else "Some customers are still inside the outcome window: measure again once it is over."
        )
        refusal = CampaignNotMaturedResponse(
            detail=ErrorBody(code=CAMPAIGN_NOT_MATURED, message=message), results_available_on=when
        )
        return JSONResponse(status_code=409, content=refusal.model_dump(mode="json"))
    storage.write_model(campaign_key(campaign_id, REPORT_FILENAME), report)
    updated = campaign.model_copy(
        update={
            "status": CampaignStatus.LIVE if report.early_look else CampaignStatus.MEASURED,
            "measured_at": report.computed_at,
        }
    )
    save_campaign(store, storage, updated)
    _LOGGER.info(
        "campaigns.measure campaign=%s treated=%d control=%d early_look=%s",
        campaign_id,
        report.treated_rows,
        report.control_rows,
        report.early_look,
    )
    return _view(storage, updated, root)


def _epoch_mismatch(
    request: Request, storage: Storage, campaign: Campaign, *, window: int | None, as_of: datetime
) -> str | None:
    """`epoch_mismatch` over the campaign's runs and, for a persistent holdout, the ledger's current epoch.

    The outcomes are all in at the end of the outcome window counted from the treatment start; with
    no window, or per-row treatment dates the record does not hold, the measurement's own moment.
    """
    run_specs = {run_id: run_holdout_spec(storage, run_id) for run_id in campaign.run_ids}
    ledger = None
    if campaign.holdout_scope in PERSISTENT_SCOPES and campaign.holdout_scope_key is not None:
        ledger = HoldoutLedger(get_platform_engine(request)).entry(
            campaign.holdout_scope, campaign.holdout_scope_key
        )
    outcomes = campaign.outcomes
    per_row = outcomes is not None and outcomes.treatment_date_column is not None
    closes = as_of if window is None or per_row else campaign.treatment_start + timedelta(days=window)
    return epoch_mismatch(campaign, run_specs, ledger, outcomes_in_by=min(closes, as_of))


# ---------------------------------------------------------------------------
# GET /campaigns/{id}/plan-preview
# ---------------------------------------------------------------------------
class CampaignPlanPreview(StrictBase):
    """`GET /campaigns/{id}/plan-preview`: the planner's points on this campaign's own population."""

    campaign_id: str = Field(description="The campaign.")
    eligible: int = Field(description="Customers in the population the campaign is measured on.")
    n_treat: int = Field(description="Of them, contacted (the campaign's realised split).")
    n_holdout: int = Field(description="Of them, held back.")
    n_explore: int = Field(description="Of them, in the explore slice (M92).")
    holdout_fraction: float = Field(description="The campaign's realised held-back share.")
    base_rate: float | None = Field(
        description="The rate expected without the campaign the points use; null when not known."
    )
    base_rate_source: Literal["request", "plan"] | None = Field(
        description="Where it came from: the request, else the registered plan; null when neither gave one."
    )
    direction: Literal["up", "down", "either"] = Field(
        description="Which way the use case's campaigns aim to move the outcome (the smallest change is in it)."
    )
    points: tuple[PowerPreviewPoint, ...] = Field(
        description="One computed point per control-group share, smallest share first; empty with `reason`."
    )
    current_index: int | None = Field(
        description="The point at the campaign's realised share, where the slider starts; null when none is."
    )
    basis: str | None = Field(description="What the numbers rest on, in one plain paragraph.")
    reason: str | None = Field(description="Why there are no points; null when there are.")


HoldoutShares = Annotated[
    list[float] | None,
    Query(
        alias="holdout",
        description="Control-group shares to compute, each more than 0 and at most 0.5; a fixed ladder when none.",
    ),
]
OptionalRate = Annotated[
    float | None, Query(ge=0.0, le=1.0, description="Rate expected without the campaign, 0 to 1.")
]
OptionalMoney = Annotated[float | None, Query(ge=0.0, le=1e9, description="Rupees; null when not known.")]


@router.get(
    "/campaigns/{campaign_id}/plan-preview",
    response_model=CampaignPlanPreview,
    responses=_ERRORS,
    summary="The computed points behind the 'Plan the test' slider, on the campaign's own population",
)
def preview_plan(
    campaign_id: str,
    request: Request,
    root: ConfigRootDep,
    storage: StorageDep,
    holdout: HoldoutShares = None,
    base_rate: OptionalRate = None,
    value_per_conversion: OptionalMoney = None,
    contact_cost: OptionalMoney = None,
    offer_cost: OptionalMoney = None,
) -> CampaignPlanPreview:
    """`engine.measurement.planner.power_preview` at each share; every number is the planner's."""
    campaign = _load(get_campaign_store(request), campaign_id)
    realised = _realised(storage, campaign_id)
    plan = _current_plan(storage, campaign_id)
    rate, source = (base_rate, "request") if base_rate is not None else (None, None)
    if rate is None and plan is not None and plan.base_rate is not None:
        rate, source = plan.base_rate, "plan"
    good, _ = _direction(campaign, root)
    aim: Literal["up", "down", "either"] = (
        "either" if campaign.use_case_id is None else ("up" if good else "down")
    )
    current = realised.holdout_fraction
    asked = tuple(holdout) if holdout else PREVIEW_SHARES
    with_current = (*asked, current) if 0.0 < current <= MAX_HOLDOUT_SHARE and not holdout else asked
    shares = tuple(sorted(set(with_current)))
    answer: dict[str, Any] = {
        "campaign_id": campaign_id,
        "eligible": realised.population_rows,
        "n_treat": realised.n_treat,
        "n_holdout": realised.n_holdout,
        "n_explore": realised.n_explore,
        "holdout_fraction": current,
        "base_rate": rate,
        "base_rate_source": source,
        "direction": aim,
    }
    if realised.population_rows < 1:
        return CampaignPlanPreview(
            **answer,
            points=(),
            current_index=None,
            basis=None,
            reason="This campaign measures nobody, so there is no test to size.",
        )
    try:
        preview_request = PowerPreviewRequest(
            eligible=realised.population_rows,
            base_rate=rate,
            holdout_shares=shares,
            explore_share=min(realised.explore_fraction, 0.10),
            alpha=plan.alpha if plan is not None else 0.05,
            power=plan.power if plan is not None else 0.8,
            value_per_conversion=value_per_conversion,
            contact_cost=contact_cost,
            offer_cost=offer_cost,
            direction=aim,
        )
    except ValidationError as exc:
        raise http_error(
            422,
            CAMPAIGN_INVALID,
            "Each control-group share must be more than 0 and at most 0.5, and at most 20 may be asked for.",
            path="holdout",
        ) from exc
    preview = power_preview(preview_request)
    index = next((i for i, point in enumerate(preview.points) if point.holdout_share == current), None)
    return CampaignPlanPreview(
        **answer, points=preview.points, current_index=index, basis=preview.basis, reason=None
    )


# ---------------------------------------------------------------------------
# The test plan
# ---------------------------------------------------------------------------
@router.get(
    "/campaigns/{campaign_id}/plan",
    response_model=TestPlanView,
    responses={404: {"model": ErrorResponse}},
    summary="A campaign's registered test plan and every earlier version",
)
def read_plan(campaign_id: str, request: Request, storage: StorageDep) -> TestPlanView:
    campaign = _load(get_campaign_store(request), campaign_id)
    return TestPlanView(plan=_current_plan(storage, campaign_id), versions=_versions(storage, campaign))


@router.post(
    "/campaigns/{campaign_id}/plan",
    response_model=TestPlan,
    status_code=201,
    responses=_ERRORS,
    summary="Register a campaign's test plan before its outcomes are read",
)
def register_plan(
    campaign_id: str, body: TestPlanInput, request: Request, response: Response, storage: StorageDep
) -> TestPlan:
    store = get_campaign_store(request)
    campaign = _load(store, campaign_id)
    decided = _resolved(campaign, body)
    current = _current_plan(storage, campaign_id)
    if current is not None:
        if plan_inputs(current) == decided:
            response.status_code = 200
            _audit_plan(request, current)
            return current
        set_audit_context(request, details={"reason_code": TEST_PLAN_EXISTS, "plan_hash": current.plan_hash})
        raise http_error(
            409,
            TEST_PLAN_EXISTS,
            "This campaign already has a registered test plan. Change it through an amendment, with a reason.",
        )
    if _stored(storage, campaign_key(campaign_id, REPORT_FILENAME), IncrementalityReport) is not None:
        raise http_error(
            409,
            TEST_PLAN_INVALID,
            "This campaign has already been measured, so a plan registered now would not be fixed in advance.",
        )
    plan = freeze_plan(
        decided,
        _realised(storage, campaign_id),
        campaign_id=campaign_id,
        registered_by=requested_by(request) or "local",
        registered_at=utc_now(),
    )
    _store_plan(store, storage, campaign, plan)
    _audit_plan(request, plan)
    return plan


@router.post(
    "/campaigns/{campaign_id}/plan/amendments",
    response_model=TestPlan,
    status_code=201,
    responses=_ERRORS,
    summary="Amend a campaign's test plan: a new version with a reason, the old one kept",
)
def amend_plan(campaign_id: str, body: TestPlanAmendment, request: Request, storage: StorageDep) -> TestPlan:
    store = get_campaign_store(request)
    campaign = _load(store, campaign_id)
    current = _current_plan(storage, campaign_id)
    if current is None:
        raise http_error(
            404, TEST_PLAN_NOT_FOUND, "This campaign has no test plan to amend; register one first."
        )
    stored = _stored(storage, campaign_key(campaign_id, REPORT_FILENAME), IncrementalityReport)
    if stored is not None and not stored.early_look:
        set_audit_context(request, details={"reason_code": TEST_PLAN_INVALID, "plan_hash": current.plan_hash})
        raise http_error(
            409,
            TEST_PLAN_INVALID,
            "This campaign's final result has been read under the plan in force, so the plan can no longer "
            "be amended.",
        )
    decided = _resolved(campaign, TestPlanInput.model_validate(body.model_dump(exclude={"reason"})))
    if plan_inputs(current) == decided:
        raise http_error(422, TEST_PLAN_INVALID, "This amendment changes nothing in the plan in force.")
    plan = freeze_plan(
        decided,
        _realised(storage, campaign_id),
        campaign_id=campaign_id,
        registered_by=requested_by(request) or "local",
        registered_at=utc_now(),
        version=current.version + 1,
        amends=current.plan_hash,
        amendment_reason=body.reason,
    )
    _store_plan(store, storage, campaign, plan)
    _audit_plan(request, plan)
    return plan


def _resolved(campaign: Campaign, body: TestPlanInput) -> TestPlanInput:
    """The input with the campaign's own window filled in, checked against what can be measured."""
    window = (
        body.outcome_window_days if body.outcome_window_days is not None else campaign.outcome_window_days
    )
    decided = body.model_copy(update={"outcome_window_days": window})
    if decided.outcome_kind != "binary":
        raise http_error(
            422,
            TEST_PLAN_INVALID,
            "Only yes-or-no outcomes can be measured today; amounts such as revenue come later.",
            path="outcome_kind",
        )
    if window is not None:
        from datetime import timedelta

        complete = (campaign.treatment_start + timedelta(days=window)).date()
        if decided.analysis_date < complete:
            raise http_error(
                422,
                TEST_PLAN_INVALID,
                f"The analysis date is before every outcome is in ({complete.isoformat()}); choose that day or later.",
                path="analysis_date",
            )
    return decided


def _realised(storage: Storage, campaign_id: str) -> Any:
    assignment = read_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
    return realised_population(assignment, intended_column=INTENDED_COLUMN)


def _current_plan(storage: Storage, campaign_id: str) -> TestPlan | None:
    plan: TestPlan | None = _stored(storage, campaign_key(campaign_id, TEST_PLAN_FILENAME), TestPlan)
    return plan


def _versions(storage: Storage, campaign: Campaign) -> tuple[TestPlan, ...]:
    found: list[TestPlan] = []
    for version in range(1, (campaign.test_plan_version or 0) + 1):
        stored = _stored(
            storage, campaign_key(campaign.campaign_id, test_plan_version_filename(version)), TestPlan
        )
        if stored is not None:
            found.append(stored)
    return tuple(found)


def _store_plan(store: CampaignStore, storage: Storage, campaign: Campaign, plan: TestPlan) -> None:
    storage.write_model(campaign_key(campaign.campaign_id, test_plan_version_filename(plan.version)), plan)
    storage.write_model(campaign_key(campaign.campaign_id, TEST_PLAN_FILENAME), plan)
    save_campaign(
        store,
        storage,
        campaign.model_copy(update={"test_plan_hash": plan.plan_hash, "test_plan_version": plan.version}),
    )
    _LOGGER.info("campaigns.plan campaign=%s version=%d", campaign.campaign_id, plan.version)


def _audit_plan(request: Request, plan: TestPlan) -> None:
    """The plan's own hash as the event's `after_hash`, not the response bytes' (DEC-1304 (g))."""
    set_audit_context(request, after_hash=content_hash(plan), details={"plan_hash": plan.plan_hash})
