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

**Uplift artefacts have their own whitelist.** `GET /runs/{id}/uplift/{name}` serves
`engine.uplift.contracts.UPLIFT_ARTEFACTS` and nothing else - a whitelist, never a path join. Since
M53 Phase 1's `GET /runs/{id}/artefacts/{name}` whitelists the same registry, so Phase 1's Data and
Model pages read an uplift run where they read every other run; this route stays as an alias.

**Two-column keys (M53).** `primary_key` may name the customer and the snapshot date. The uplift
checks then refuse a customer treated in one snapshot and held out in another
(`TREATMENT_VARIES_WITHIN_ENTITY`), and campaign results join the outcomes file on every key column.

**Campaign results are measured against what the run did.** The scores file of a finished scoring
run says who was treated, who was held out and - for an uplift run - who the policy intended to
treat; the uploaded outcomes file says who converted. `measure_incrementality` compares treated with
control inside `intended_treatment` for an uplift run, because that is the population both arms
were drawn from; for a Phase 1 run it uses the requested bands or every eligible row. The run's own
finish time is the treatment time unless the outcomes file dates each row. Nothing is estimated for
a row whose outcome window has not elapsed: the report says when results will be available.

**The budget curve is a replay, never a second model (DEC-1200…1203).** `GET
/runs/{id}/uplift/profit-curve` recomputes a finished uplift run's targeting recommendation at every
budget from what the run saved - a training run's hold-out, a scoring run's scores file and its
model's hold-out - with optional cost and value overrides for that answer only. It checks the replay
against the stored recommendation first and refuses (`PROFIT_CURVE_UNAVAILABLE`) rather than plot a
curve that is not the run's. Nothing is written.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from api.access_policy import RoutePolicy, register
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
from engine.access.roles import Role
from engine.config import ProblemType, ResolvedConfig, RunMode, get_catalog, resolve_config
from engine.contracts import RunRecord, RunState, Severity
from engine.keys import normalise_key, row_key_column, split_config_for_key, with_row_key
from engine.pipeline import Pipeline
from engine.registry import RegistryError
from engine.runs import RUN_CONFIG_FILENAME, build_job_fn, create_run, job_spec_for, write_job_spec
from engine.stages import export, ingest, validate
from engine.stages.train import predictor_key_for
from engine.storage import Storage, StorageError, run_key, upload_key
from engine.uplift.actions import (
    CUSTOMER_VALUE_COLUMN,
    INTENDED_TREATMENT_COLUMN,
    SEGMENT_COLUMN,
    TREAT_ACTION,
    tiebreak_keys,
)
from engine.uplift.config import UpliftPolicyConfig
from engine.uplift.contracts import (
    INCREMENTALITY_FILENAME,
    OPE_FILENAME,
    POLICY_FILENAME,
    UPLIFT_ARTEFACTS,
    UPLIFT_VALIDATION_FILENAME,
    IncrementalityReport,
    OpeReport,
    PolicyRecommendation,
    ProfitCurve,
    UpliftModelCard,
    UpliftValidationReport,
)
from engine.uplift.flow import (
    UPLIFT_HOLDOUT_FILENAME,
    check_seed,
    holdout_values,
    model_card_key,
    read_holdout,
    training_value_column,
)
from engine.uplift.policy import DEFAULT_CURVE_POINTS
from engine.utils.ids import seed_from
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from datetime import datetime

    import numpy as np
    import pandas as pd

    from engine.contracts import ValidationReport
    from engine.pilot.roi import ValueCosts
    from engine.registry import ModelRegistry
    from engine.uplift.policy import HoldoutLookups

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
PROFIT_CURVE_UNAVAILABLE: Final[str] = "PROFIT_CURVE_UNAVAILABLE"
PROFIT_CURVE_QUERY_INVALID: Final[str] = "PROFIT_CURVE_QUERY_INVALID"
"""Plan J M97: the budget-curve query names the value of a conversion twice (`value` and `value_per_conversion`)."""

PROFIT_CURVE_PATH: Final[str] = "/runs/{run_id}/uplift/profit-curve"
MAX_CURVE_POINTS: Final[int] = 201

# Declared next to the route, as Phase 4b routers do (`api/access_policy.py`); a GET is Viewer (DEC-716).
register(
    {
        ("GET", PROFIT_CURVE_PATH): RoutePolicy(
            role=Role.VIEWER,
            action="uplift.profit_curve",
            purpose="see the budget curve of a targeting recommendation",
            object_type="run",
            object_param="run_id",
        ),
    }
)

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
    # A key of customer + snapshot date (DEC-083) splits by customer, recorded as derived in
    # run_config.json; the uplift flow draws its own hold-out by customer too (M53, DEC-855).
    primary_key = normalise_key(body.primary_key)
    resolved = split_config_for_key(resolved, primary_key)
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
        primary_key=primary_key,
        target=body.target,
        acknowledged=config.validation.acknowledged,
        upload_id=upload.upload_id,
        row_count=profile.row_count,
    )
    checked = run_uplift_checks(
        frame,
        config,
        primary_key=primary_key,
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
        primary_key=primary_key,
        target=body.target,
        model_choice=config.uplift.learner.value,
        model_version_id=None,
        requested_by=requested_by(request),
        synthetic=upload.synthetic,
    )
    storage.write_model(
        run_key(record.run_id, UPLIFT_VALIDATION_FILENAME),
        checked.report.model_copy(update={"run_id": record.run_id}),
    )
    if config.uplift.policy.arm_action_ids:
        # Plan J M100 part B (DEC-1310): the offers' catalogue actions as this root declares them (the
        # root their ids were just checked against), for the costs `arm_policy_value.json` records.
        from engine.decide.catalogue import stamp_checked_catalogue

        stamp_checked_catalogue(storage, record.run_id, config, root=root, created_at=record.created_at)
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
# GET /runs/{run_id}/uplift/profit-curve  (declared before `/uplift/{name}`, which would match it)
# ---------------------------------------------------------------------------
CostQuery = Annotated[
    float | None,
    Query(ge=0.0, description="Cost of one contact to compute with; the run's configured cost when absent."),
]
ValueQuery = Annotated[
    float | None,
    Query(
        ge=0.0, description="Value of one conversion to compute with; the run's configured value when absent."
    ),
]
ValueAliasQuery = Annotated[
    float | None,
    Query(
        ge=0.0,
        description=(
            "Alias of value_per_conversion (Plan J M97). Give one or the other: both answer "
            "422 PROFIT_CURVE_QUERY_INVALID."
        ),
    ),
]
MinRoiQuery = Annotated[
    float | None,
    Query(
        ge=0.0,
        description=(
            "Minimum return on each contact to require, as net value ÷ cost (0.15 is 15 %); the run's "
            "configured min_roi when absent."
        ),
    ),
]
PointsQuery = Annotated[
    int,
    Query(
        ge=2,
        le=MAX_CURVE_POINTS,
        description="Evenly spaced contact counts to plot, 0 and the maximum included.",
    ),
]


@dataclass(frozen=True)
class _HoldoutArrays:
    """A training run's hold-out as the lookups need it (`engine.uplift.policy.holdout_lookups`)."""

    uplift: np.ndarray
    t: np.ndarray
    y: np.ndarray
    p_treated: np.ndarray
    values: np.ndarray | None
    """The hold-out's values of the run's `value_column`, when the training run stored them."""
    samples: int
    seed: int


@dataclass(frozen=True)
class _CurveInputs:
    """What `recommend_policy` was given when the run made its recommendation, read back."""

    uplift: np.ndarray
    segments: np.ndarray
    computed_on: Literal["test", "scored"]
    holdout: _HoldoutArrays | None
    p_treated: np.ndarray
    eligible: np.ndarray | None = None
    tiebreak: np.ndarray | None = None
    treated: np.ndarray | None = None
    """The rows the run marked `Treat` (a scoring run), to check the replay against."""
    values: np.ndarray | None = None
    """Each row's customer value when the run ranked by value (Plan J M97), else `None`."""


@router.get(
    PROFIT_CURVE_PATH,
    response_model=ProfitCurve,
    responses=_MEASURE_ERRORS,
    summary="Expected net value against the number of customers contacted, for a finished uplift run",
)
def read_profit_curve(
    run_id: str,
    storage: StorageDep,
    registry: RegistryDep,
    cost_per_contact: CostQuery = None,
    value_per_conversion: ValueQuery = None,
    value: ValueAliasQuery = None,
    min_roi: MinRoiQuery = None,
    points: PointsQuery = DEFAULT_CURVE_POINTS,
) -> ProfitCurve:
    """The targeting recommendation replayed at every budget (`engine.uplift.policy.profit_curve`).

    Read-only and computed on request, never stored: the budget sweep of a training run is on its
    hold-out, of a scoring run on every row it scored, with the scoring run's own eligibility and
    tie-break. Before anything is plotted the replay is checked against the stored recommendation
    (and, on a scoring run, against who its file marks `Treat`); a replay that disagrees answers
    `409 PROFIT_CURVE_UNAVAILABLE` rather than a curve that is not this run's.

    A run configured with `uplift.policy.value_column` (Plan J M97) is replayed ranked by net value,
    with the margin, horizon and minimum ROI it was configured with; `min_roi` overrides the last for
    this answer only. `value` is an alias of `value_per_conversion`. Such a run is replayed with the
    contact and offer costs it recorded, never with `configs/pilot/value.yaml` as it reads now, and
    the replay must also reproduce the recorded cost of the list.
    """
    from engine.uplift.policy import customer_net_values, profit_curve, recommend_policy

    if value is not None and value_per_conversion is not None:
        raise http_error(
            422,
            PROFIT_CURVE_QUERY_INVALID,
            "Give the value of one conversion once: `value` is another name for `value_per_conversion`.",
        )
    value_per_conversion = value if value is not None else value_per_conversion
    record = load_run(storage, run_id)
    if record.state is not RunState.DONE or record.problem_type is not ProblemType.UPLIFT:
        raise http_error(
            409,
            RUN_NOT_UPLIFT,
            "The budget curve needs a finished uplift run: train an uplift model, or score customers with one.",
        )
    try:
        stored = storage.read_model(run_key(run_id, POLICY_FILENAME), PolicyRecommendation)
    except StorageError as exc:
        raise http_error(
            409, RUN_NOT_UPLIFT, "This run made no targeting recommendation, so it has no budget to vary."
        ) from exc

    if record.mode is RunMode.SCORE and _ranked_by_propensity(storage, run_id):
        # Plan J M96: the list was ranked by the approved propensity model, so a budget curve of the
        # uplift ranking would describe a list this run did not make.
        raise http_error(
            409,
            PROFIT_CURVE_UNAVAILABLE,
            "This contact list is ranked by the approved propensity model because the uplift model does "
            "not beat risk ranking, so it has no uplift budget curve.",
        )
    inputs, saved = (
        _training_curve_inputs(storage, record)
        if record.mode is RunMode.TRAIN
        else _scoring_curve_inputs(storage, registry, record)
    )
    # The run's own policy: the budget, cost and value it recorded, and (Plan J M97) the value column,
    # margin, horizon and minimum ROI of its configuration.
    configured = saved.model_copy(
        update={
            "budget_contacts": stored.budget_contacts,
            "cost_per_contact": stored.cost_per_contact,
            "value_per_conversion": stored.value_per_conversion,
        }
    )
    policy = configured.model_copy(
        update={
            "cost_per_contact": stored.cost_per_contact if cost_per_contact is None else cost_per_contact,
            "value_per_conversion": (
                stored.value_per_conversion if value_per_conversion is None else value_per_conversion
            ),
            "min_roi": configured.min_roi if min_roi is None else min_roi,
        }
    )
    value_costs = _recorded_costs(stored)
    replay, replayed = recommend_policy(
        inputs.uplift,
        inputs.segments,
        configured,
        run_id=run_id,
        computed_on=inputs.computed_on,
        causal=stored.causal,
        eligible=inputs.eligible,
        tiebreak=inputs.tiebreak,
        money=customer_net_values(
            inputs.uplift,
            configured,
            values=inputs.values,
            p_treated=inputs.p_treated,
            value_costs=value_costs,
        ),
    )
    agrees = (
        replay.contacts_recommended == stored.contacts_recommended
        and _same_amount(replay.expected_cost, stored.expected_cost)
        and (inputs.treated is None or bool((replayed == inputs.treated).all()))
    )
    if not agrees:
        _LOGGER.warning("profit-curve: run=%s the replayed selection differs from the stored one", run_id)
        raise http_error(
            409,
            PROFIT_CURVE_UNAVAILABLE,
            "This run's saved scores no longer reproduce its targeting recommendation, so no budget "
            "curve is shown for it. Score the customers again to get one.",
        )
    lookups = _lookups(inputs, policy, value_costs)
    return profit_curve(
        inputs.uplift,
        inputs.segments,
        policy,
        run_id=run_id,
        computed_on=inputs.computed_on,
        causal=stored.causal,
        observed=None if lookups is None else lookups.conversions,
        observed_value=None if lookups is None else lookups.value,
        holdout_note=None if lookups is None else lookups.note,
        eligible=inputs.eligible,
        tiebreak=inputs.tiebreak,
        money=customer_net_values(
            inputs.uplift, policy, values=inputs.values, p_treated=inputs.p_treated, value_costs=value_costs
        ),
        points=points,
        overridden=policy != configured,
    )


def _recorded_costs(stored: PolicyRecommendation) -> ValueCosts | None:
    """The contact and offer costs a list ranked by value recorded (Plan J M97), as `ValueCosts`.

    `None` when the run recorded none (a list ranked by uplift, whose cost is `cost_per_contact`): the
    replay then needs no `configs/pilot/value.yaml` cost.
    """
    from engine.pilot.roi import ValueCosts

    if stored.contact_cost is None or stored.offer_cost is None:
        return None
    return ValueCosts(contact_cost=stored.contact_cost, offer_cost=stored.offer_cost)


def _same_amount(replayed: float | None, recorded: float | None) -> bool:
    """Whether the replay's cost is the recorded one: both absent, or equal up to float summation."""
    if replayed is None or recorded is None:
        return replayed is recorded
    return math.isclose(replayed, recorded, rel_tol=1e-9, abs_tol=1e-9)


def _lookups(
    inputs: _CurveInputs, policy: UpliftPolicyConfig, value_costs: ValueCosts | None
) -> HoldoutLookups | None:
    """The hold-out lookups the run quoted, for `policy` and the run's recorded costs; `None` when the
    hold-out could not be read on a list ranked by uplift (a list ranked by value says why)."""
    from engine.uplift.policy import HOLDOUT_UNREADABLE_NOTE, HoldoutLookups, holdout_lookups

    ranked_by_value = inputs.values is not None and policy.value_column is not None
    holdout = inputs.holdout
    if holdout is None:
        return HoldoutLookups(None, None, HOLDOUT_UNREADABLE_NOTE) if ranked_by_value else None
    return holdout_lookups(
        holdout.uplift,
        holdout.t,
        holdout.y,
        policy=policy,
        samples=holdout.samples,
        seed=holdout.seed,
        ranked_by_value=ranked_by_value,
        values=holdout.values,
        p_treated=holdout.p_treated,
        value_costs=value_costs,
    )


def _ranked_by_propensity(storage: Storage, run_id: str) -> bool:
    """Whether the run's `ranking_choice.json` (Plan J M96) says the propensity model ranked its list."""
    from engine.uplift.contracts import RANKING_CHOICE_FILENAME, RankingChoice

    key = run_key(run_id, RANKING_CHOICE_FILENAME)
    try:
        return storage.exists(key) and storage.read_model(key, RankingChoice).ranking == "propensity_model"
    except (StorageError, ValueError, OSError):
        return False


def _holdout_arrays(
    holdout: pd.DataFrame, *, values: np.ndarray | None, samples: int, seed: int
) -> _HoldoutArrays:
    import numpy as np

    return _HoldoutArrays(
        uplift=np.asarray(holdout["uplift"], dtype=np.float64),
        t=np.asarray(holdout["t"], dtype=np.int64),
        y=np.asarray(holdout["y"], dtype=np.int64),
        p_treated=np.asarray(holdout["p_treated"], dtype=np.float64),
        values=values,
        samples=samples,
        seed=seed,
    )


def _training_curve_inputs(storage: Storage, record: RunRecord) -> tuple[_CurveInputs, UpliftPolicyConfig]:
    """A training run's hold-out, segmented as its evaluate stage segmented it, and its policy."""
    import numpy as np

    from engine.uplift.segments import assign_segments

    try:
        holdout = read_holdout(storage, run_key(record.run_id, UPLIFT_HOLDOUT_FILENAME))
        card = storage.read_model(model_card_key(predictor_key_for(record.run_id)), UpliftModelCard)
        resolved = storage.read_model(run_key(record.run_id, RUN_CONFIG_FILENAME), ResolvedConfig)
    except StorageError as exc:
        raise http_error(
            409,
            RUN_NOT_UPLIFT,
            "This run did not train an uplift model, so it has no hold-out to vary the budget on.",
        ) from exc
    saved = resolved.config.uplift.policy
    # The hold-out's values are the ones the run ranked by: its own value column, never filled in.
    values = holdout_values(holdout, saved.value_column, saved.value_column)
    arrays = _holdout_arrays(
        holdout,
        values=values,
        samples=resolved.config.uplift.bootstrap_samples,
        seed=seed_from(record.run_id),
    )
    inputs = _CurveInputs(
        uplift=arrays.uplift,
        segments=assign_segments(
            arrays.uplift, np.asarray(holdout["p_control"], dtype=np.float64), card.segment_thresholds
        ),
        computed_on="test",
        holdout=arrays,
        p_treated=arrays.p_treated,
        values=values,
    )
    return inputs, saved


def _scoring_curve_inputs(
    storage: Storage, registry: ModelRegistry, record: RunRecord
) -> tuple[_CurveInputs, UpliftPolicyConfig]:
    """A scoring run's scored rows, eligibility and tie-break, its model's training hold-out, its policy.

    The tie-break is rebuilt from the saved keys with the scoring run's id, as the actions stage built
    it (`engine.uplift.actions.tiebreak_keys`); the hold-out is the one the score flow used - the
    training run's hold-out and seed, this run's bootstrap size - and is absent, as it was there, when
    it cannot be read. A list ranked by value reads each customer's value back from the scores file's
    `customer_value` column, and the hold-out's values only when the training run stored them for the
    same column.
    """
    import numpy as np
    import pandas as pd

    try:
        scores = pd.read_parquet(
            io.BytesIO(storage.read_bytes(run_key(record.run_id, export.SCORES_PARQUET)))
        )
        resolved = storage.read_model(run_key(record.run_id, RUN_CONFIG_FILENAME), ResolvedConfig)
    except StorageError as exc:
        raise http_error(409, RUN_NOT_SCORED, "This run has no scores file to vary the budget on.") from exc
    saved = resolved.config.uplift.policy
    try:
        keyed = with_row_key(scores, record.primary_key)
        tiebreak = tiebreak_keys(keyed[row_key_column(record.primary_key)], run_id=record.run_id)
        uplift = scores["uplift"].to_numpy(dtype=np.float64)
        p_treated = scores["p_treated"].to_numpy(dtype=np.float64)
        segments = scores[SEGMENT_COLUMN].to_numpy(dtype=object)
        eligible = scores["suppressed_reason"].isna().to_numpy(dtype=bool) & ~scores[
            "control_group"
        ].to_numpy(dtype=bool)
        treated = (scores["action"] == TREAT_ACTION).to_numpy(dtype=bool)
    except (KeyError, ValueError) as exc:
        raise http_error(
            409,
            PROFIT_CURVE_UNAVAILABLE,
            "This run's scores file does not have the columns a budget curve needs.",
        ) from exc
    values = (
        scores[CUSTOMER_VALUE_COLUMN].to_numpy(dtype=np.float64)
        if saved.value_column is not None and CUSTOMER_VALUE_COLUMN in scores.columns
        else None
    )
    holdout: _HoldoutArrays | None = None
    try:
        version = registry.get(record.model_version_id or "")
        frame = read_holdout(
            storage,
            version.artefact_keys.get(
                UPLIFT_HOLDOUT_FILENAME, run_key(version.run_id, UPLIFT_HOLDOUT_FILENAME)
            ),
        )
        holdout = _holdout_arrays(
            frame,
            values=holdout_values(frame, training_value_column(storage, version.run_id), saved.value_column),
            samples=resolved.config.uplift.bootstrap_samples,
            seed=seed_from(version.run_id),
        )
    except (RegistryError, StorageError, OSError, ValueError, KeyError):
        _LOGGER.warning("profit-curve: run=%s the training hold-out could not be read", record.run_id)
    inputs = _CurveInputs(
        uplift=uplift,
        segments=segments,
        computed_on="scored",
        holdout=holdout,
        p_treated=p_treated,
        eligible=eligible,
        tiebreak=tiebreak,
        treated=treated,
        values=values,
    )
    return inputs, saved


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
    """Treated rate minus control rate on the mature rows, stored as `incrementality_report.json`.

    The measurement is Plan J's one path, `engine.measurement.measure.measure_campaign` (M94,
    DEC-1304 (c)), given the run's scores and no test plan: exactly `measure_incrementality`'s report.
    """
    import pandas as pd

    from engine.measurement.measure import measure_campaign

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
        report = measure_campaign(
            scores,
            outcomes,
            run_id=run_id,
            # Every key column, so a run keyed by customer + snapshot date is joined on both (M53).
            primary_key=record.primary_key,
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
