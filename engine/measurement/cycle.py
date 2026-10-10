"""The monthly loop after scoring: the treat list, the measurement and the learning, for a schedule (Plan J M107).

The value has to arrive every month without someone downloading and uploading files. A scoring schedule
already rebuilds the recipe on the client's newest tables and scores with the model in use
(`engine.scheduling.firing`); three more schedule kinds carry the cycle on, each reading only what the
step before it wrote, so each can run on its own cadence and a missed day simply waits for the next:

* **treat_list** (`treat_list_step`) - the newest finished scoring run of the use case and client that has
  no campaign yet gets its treat list (`engine.decide.treat_list.ensure_treat_list`, M98) and its campaign
  record (`open_campaign`, what `POST /campaigns` does). The list stays a download: nothing is sent.
* **measure** (`measure_step`) - once the newest live campaign's outcome window has closed, its outcomes
  are read from a saved connection for exactly that window (`engine.measurement.pull`, read-only, one
  quoted date-window `WHERE` for a SQL database), kept as an ordinary upload with `pull_source.json` beside
  it, given to the campaign (`record_outcomes`, what `POST /campaigns/{id}/outcomes` does) and measured
  through the one measurement path against its test plan (`measure_recorded`, what
  `POST /campaigns/{id}/measure` does).
* **learn** (`learn_step`) - the newest measured campaign that was not learned from yet teaches the next
  model (`learn_from_campaign`): the frame M106 builds from the cycle's randomised rows
  (`engine.measurement.learn`), trained as an uplift run whose challenger always waits for an Approver.
  Nothing here promotes or approves anything.

`open_campaign`, `record_outcomes` and `measure_recorded` are the engine halves of the three campaign
routes (`api/routes/campaigns.py`), which call them, so a schedule and a person measure a campaign through
the same code. Errors are `CycleError`, with the code, status and message the route answers with.

**Codes.** `CYCLE_SERVICES_MISSING`: a deployment that gives the scheduler no campaign store or no
connections cannot run these kinds. `LEARN_NOT_READY`: the measured campaign cannot teach a model yet (too
few customers or responders in a group, or an amount rather than a yes/no outcome). Both are in
`CYCLE_CODES`, joined into `PLAN_J_CODES` at integration.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import RunMode, StrictBase, UseCaseConfig
from engine.contracts import Artefact, DatasetProfile, RunRecord, RunState
from engine.measurement.campaign import (
    ASSIGNMENT_FILENAME,
    CAMPAIGN_FILENAME,
    CAMPAIGN_OUTCOMES_MISSING,
    CONTACT_FILENAME,
    CONTACT_READOUT_FILENAME,
    INTENDED_COLUMN,
    OUTCOMES_FILENAME,
    REPORT_FILENAME,
    TEST_PLAN_FILENAME,
    Campaign,
    CampaignKind,
    CampaignOutcomes,
    CampaignStatus,
    CampaignStore,
    assignment_counts,
    build_assignment,
    campaign_key,
    chosen_intended_keys,
    epoch_mismatch,
    holdout_identity,
    new_campaign_id,
    read_frame,
    save_campaign,
    write_frame,
)
from engine.storage import Storage, StorageError, run_key, upload_key
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pandas as pd
    from sqlalchemy.engine import Engine

    from engine.access.roles import Principal
    from engine.connections.store import ConnectionStore
    from engine.jobs import JobRunner
    from engine.measurement.audit import AuditReadout
    from engine.measurement.pull import OutcomePullSpec
    from engine.measurement.reconcile import ContactReadout, UnlistedRule
    from engine.measurement.segments import SegmentEffects
    from engine.registry import ModelRegistry
    from engine.uplift.contracts import IncrementalityReport

__all__ = [
    "CYCLE_CODES",
    "CYCLE_SERVICES_MISSING",
    "LEARNED_FILENAME",
    "LEARN_NOT_READY",
    "CampaignLearned",
    "CycleError",
    "CycleServices",
    "Measured",
    "StepOutcome",
    "StoredUpload",
    "contact_readout",
    "covariate_date_column",
    "default_outcome_window",
    "epoch_refusal",
    "learn_from_campaign",
    "learn_step",
    "measure_recorded",
    "measure_step",
    "offer_arguments",
    "open_campaign",
    "record_outcomes",
    "refresh_contacts",
    "segment_effects",
    "store_frame_upload",
    "target_of",
    "treat_list_step",
]

_LOGGER = get_logger(__name__)

CYCLE_SERVICES_MISSING: Final[str] = "CYCLE_SERVICES_MISSING"
LEARN_NOT_READY: Final[str] = "LEARN_NOT_READY"
CYCLE_CODES: Final[frozenset[str]] = frozenset({CYCLE_SERVICES_MISSING, LEARN_NOT_READY})

MEASURE_NOT_OFFERED: Final[str] = "MEASURE_NOT_OFFERED"
"""`api.routes.measure.MEASURE_NOT_OFFERED`: the use case contacts nobody or holds nobody back."""
MEASURE_INVALID: Final[str] = "MEASURE_INVALID"
"""`api.routes.measure.MEASURE_INVALID`: the run's files cannot be turned into an experiment."""
RUN_NOT_SCORED: Final[str] = "RUN_NOT_SCORED"
CAMPAIGN_INVALID: Final[str] = "CAMPAIGN_INVALID"
"""`api.routes.campaigns.CAMPAIGN_INVALID` (M94)."""

LEARNED_FILENAME: Final[str] = "learned.json"
"""`campaigns/<id>/learned.json`: which uplift run a scheduled learn started from the campaign. Ids only."""

UPLOAD_RECORD_FILENAME: Final[str] = "upload.json"
UPLOAD_PROFILE_FILENAME: Final[str] = "profile.json"
UPLOAD_FINGERPRINT_FILENAME: Final[str] = "fingerprint.json"
UPLOAD_VALIDATION_FILENAME: Final[str] = "validation.json"
"""The names `api.routes.uploads` gives an upload's files (`tests/unit/measurement/test_cycle_upload.py`)."""


class CycleError(Exception):
    """A loop step or a campaign route refused. `status` and `path` are what the route answers with."""

    def __init__(self, code: str, message: str, *, status: int = 409, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.path = path


@dataclass(frozen=True)
class CycleServices:
    """What the loop's kinds need beyond a firing's own services (`FiringServices.cycle`).

    `campaigns` is the platform database's campaign store; `connections` the saved connections, for the
    sources and outcomes read from them (None: nothing is read from a connection); `ledger_engine` the
    platform database, for the persistent holdout's epochs (None: the epoch check reads no ledger).
    """

    campaigns: CampaignStore
    connections: ConnectionStore | None = None
    ledger_engine: Engine | None = None


@dataclass(frozen=True)
class StepOutcome:
    """What one loop step did: its result code, the run it read or started, and the alert to raise."""

    result_code: str
    run_id: str | None = None
    running: bool = False
    """True when `run_id` is a run this step started, which the firing waits for."""
    campaign_id: str | None = None
    alert: str | None = None
    """A sentence for a person when the step produced something to act on; None for a quiet step."""


class CampaignLearned(StrictBase):
    """`campaigns/<id>/learned.json`: the model a scheduled learn started from this campaign."""

    campaign_id: str
    run_id: str = Field(description="The scoring run whose campaign was learned from.")
    uplift_run_id: str = Field(description="The training run started on the campaign's randomised rows.")
    learned_at: AwareDatetime


# ---------------------------------------------------------------------------
# An upload written by the engine
# ---------------------------------------------------------------------------
class _UploadDocument(Artefact):
    """`upload.json`, field for field `api.schemas.UploadRecord`, which the engine may not import (DEC-327).

    `tests/unit/measurement/test_cycle_upload.py` keeps the two identical and reads this document back as
    an `UploadRecord`.
    """

    upload_id: str
    use_case_id: str
    mode: RunMode
    file_name: str
    file_format: Literal["csv", "parquet"]
    file_size_bytes: int
    delimiter: str | None
    encoding: str
    row_count: int
    column_count: int
    source_key: str
    profile_key: str
    fingerprint_key: str
    fingerprint_hash: str
    created_at: AwareDatetime
    synthetic: bool = False


@dataclass(frozen=True)
class StoredUpload:
    """An upload the engine wrote: what `engine.runs.UploadInfo` needs, and its profile."""

    upload_id: str
    file_name: str
    source_key: str
    profile: DatasetProfile
    file_format: Literal["csv", "parquet"] = "parquet"


def store_frame_upload(
    storage: Storage,
    config: UseCaseConfig,
    frame: pd.DataFrame,
    *,
    file_name: str,
    mode: RunMode,
    now: datetime,
    synthetic: bool = False,
) -> StoredUpload:
    """Store `frame` as an ordinary upload, profiled exactly as `POST /uploads` profiles a file.

    The engine half of `api.routes.measure._write_upload`: a pulled outcomes table and a learned experiment
    are uploads in every respect, so the privacy jobs, the Data page and a run read them as any other.
    """
    from engine.stages import ingest
    from engine.utils.ids import new_upload_id

    upload_id = new_upload_id()
    source_key = upload_key(upload_id, "source.parquet")
    with storage.open_write(source_key) as sink:
        frame.to_parquet(sink, index=False)
    read = ingest.read_upload(
        storage, source_key, file_format="parquet", row_cap=ingest.profile_row_cap(config)
    )
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
    document = _UploadDocument(
        upload_id=upload_id,
        use_case_id=config.id,
        mode=mode,
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
        created_at=now,
        synthetic=synthetic,
    )
    storage.write_model(document.profile_key, profile)
    storage.write_model(document.fingerprint_key, profile.fingerprint)
    storage.write_model(upload_key(upload_id, UPLOAD_RECORD_FILENAME), document)
    return StoredUpload(
        upload_id=upload_id, file_name=profile.file_name, source_key=source_key, profile=profile
    )


# ---------------------------------------------------------------------------
# The campaign routes' engine halves
# ---------------------------------------------------------------------------
def default_outcome_window(config: UseCaseConfig) -> int | None:
    """The use case's outcome window, as `POST /campaigns` reads it when none is given."""
    return config.uplift.outcome_window_days or (
        config.label.horizon_days if config.label is not None else None
    )


def target_of(config: UseCaseConfig | None) -> str:
    """The use case's outcome column, as step 4 names it (`api.routes.measure._target`)."""
    return (config.target.column if config is not None else None) or "outcome"


def open_campaign(
    storage: Storage,
    store: CampaignStore,
    record: RunRecord,
    config: UseCaseConfig,
    *,
    finished_at: datetime,
    treatment_start: datetime | None,
    bands: tuple[str, ...] | None,
    outcome_window_days: int | None,
    name: str | None,
    created_by: str,
    now: datetime,
) -> Campaign:
    """Record the campaign a finished scoring run's list went out as (`POST /campaigns`, DEC-1304 (a), (b)).

    `finished_at` is the run's finish time, checked by the caller. The assignment is written before the
    record and removed again when the record cannot be saved.
    """
    import pandas as pd

    from engine.holdout.assign import run_holdout_spec
    from engine.stages import export
    from engine.uplift.measure import measure_offered

    if not measure_offered(config):
        raise CycleError(
            MEASURE_NOT_OFFERED, f"{config.name} holds nobody back or contacts nobody, so it has no campaign."
        )
    if treatment_start is not None and treatment_start < finished_at:
        raise CycleError(
            CAMPAIGN_INVALID,
            "A campaign cannot go out before its list was made: give a treatment start on or after the run finished.",
            status=422,
            path="treatment_start",
        )
    try:
        scores = pd.read_parquet(
            io.BytesIO(storage.read_bytes(run_key(record.run_id, export.SCORES_PARQUET)))
        )
    except StorageError as exc:
        raise CycleError(RUN_NOT_SCORED, "This run has no scores file to build a campaign from.") from exc
    holdout_table = holdout_table_of(storage, record.run_id)
    # A run that chose the offer per customer is measured within the customers its policy meant to contact
    # (DEC-1311 (ai)), not within the first offer's `intended_treatment`.
    intended_keys = (
        chosen_intended_keys(storage, record.run_id) if "intended_treatment" in scores.columns else None
    )
    try:
        assignment = build_assignment(
            scores,
            primary_key=record.primary_key,
            bands=bands,
            holdout=holdout_table,
            intended_keys=intended_keys,
        )
    except ValueError as exc:
        raise CycleError(CAMPAIGN_INVALID, str(exc), status=422, path="bands" if bands else None) from exc
    counts = assignment_counts(assignment)
    holdout_scope, holdout_key, holdout_epoch = holdout_identity(run_holdout_spec(storage, record.run_id))
    uplift_run = "intended_treatment" in scores.columns
    start = treatment_start or finished_at
    campaign = Campaign(
        campaign_id=new_campaign_id(now),
        kind=CampaignKind.SCORED,
        name=name or f"{config.name}, sent {start.day} {start:%b %Y}",
        use_case_id=record.use_case_id,
        run_ids=(record.run_id,),
        primary_key=record.primary_key,
        treatment_start=start,
        treatment_start_source="entered" if treatment_start is not None else "run_finished",
        outcome_window_days=outcome_window_days,
        population="intended" if uplift_run else ("bands" if bands is not None else "eligible"),
        bands=bands,
        causal=counts.holdout > 0,
        causal_basis="engine_random" if counts.holdout > 0 else "not_random",
        holdout_scope=holdout_scope,
        holdout_scope_key=holdout_key,
        holdout_epoch=holdout_epoch,
        counts=counts,
        intended_source=None if intended_keys is None else "offer_choice",
        status=CampaignStatus.LIVE,
        created_at=now,
        created_by=created_by,
    )
    assignment_key = campaign_key(campaign.campaign_id, ASSIGNMENT_FILENAME)
    write_frame(storage, assignment_key, assignment)
    try:
        save_campaign(store, storage, campaign, create=True)
    except Exception:
        # Without its record the privacy jobs cannot read the assignment's key or date: never leave one.
        _discard(storage, assignment_key, campaign_key(campaign.campaign_id, CAMPAIGN_FILENAME))
        raise
    return campaign


def holdout_table_of(storage: Storage, run_id: str) -> Any:
    """The run's `holdout_assignment.parquet` (M92), or None when the run wrote none (a default run).

    `scores.*` carries no explore flag (DEC-1302 (c)): the campaign's assignment takes `explore` and
    `explore_probability` from this file, joined on the key.
    """
    import pandas as pd

    from engine.holdout.assign import HOLDOUT_ASSIGNMENT_FILENAME

    key = run_key(run_id, HOLDOUT_ASSIGNMENT_FILENAME)
    try:
        return pd.read_parquet(io.BytesIO(storage.read_bytes(key))) if storage.exists(key) else None
    except StorageError:
        return None


def record_outcomes(
    storage: Storage,
    store: CampaignStore,
    campaign: Campaign,
    frame: pd.DataFrame,
    *,
    upload_id: str,
    file_name: str,
    config: UseCaseConfig | None,
    outcome_column: str | None,
    positive_label: str | None,
    treatment_date_column: str | None,
    covariate_column: str | None,
    covariate_date_column: str | None,
    now: datetime,
) -> Campaign:
    """Give `campaign` its outcomes (`POST /campaigns/{id}/outcomes`): the needed columns, copied and recorded."""
    from engine.config import key_columns
    from engine.measurement.continuous import COVARIATE_NOT_BEFORE_CAMPAIGN
    from engine.uplift.measure import detect_outcome_column

    columns = [str(name) for name in frame.columns]
    try:
        outcome = outcome_column or detect_outcome_column(
            columns,
            primary_key=campaign.primary_key,
            target_column=target_of(config),
            label_name=config.label.name if config is not None and config.label is not None else None,
        )
    except ValueError as exc:
        raise CycleError(CAMPAIGN_INVALID, str(exc), status=422, path="upload_id") from exc
    wanted = [*key_columns(campaign.primary_key), outcome]
    if treatment_date_column is not None:
        wanted.append(treatment_date_column)
    if covariate_column is not None:  # Plan J M102: the adjustment needs to know when it was measured
        if covariate_date_column is None:
            raise CycleError(
                COVARIATE_NOT_BEFORE_CAMPAIGN,
                f"Name the column holding the date each {covariate_column!r} value was measured up to: "
                f"without it the value cannot be shown to come from before the campaign.",
                status=422,
                path="covariate_date_column",
            )
        wanted += [covariate_column, covariate_date_column]
    missing = [name for name in wanted if name not in columns]
    if missing:
        raise CycleError(
            CAMPAIGN_INVALID,
            f"The outcomes file has no column {', '.join(repr(m) for m in missing)}.",
            status=422,
        )
    kept = frame[list(dict.fromkeys(wanted))]
    try:
        write_frame(storage, campaign_key(campaign.campaign_id, OUTCOMES_FILENAME), kept)
    except (ValueError, TypeError) as exc:  # a column pyarrow cannot store as one type
        raise CycleError(
            CAMPAIGN_INVALID, "The outcomes file mixes kinds of value in one column.", status=422
        ) from exc
    updated = campaign.model_copy(
        update={
            "outcomes": CampaignOutcomes(
                upload_id=upload_id,
                file_name=file_name,
                outcome_column=outcome,
                outcome_named=outcome_column is not None,
                positive_label=positive_label,
                treatment_date_column=treatment_date_column,
                rows=len(kept.index),
                added_at=now,
                covariate_column=covariate_column,
                covariate_date_column=covariate_date_column if covariate_column is not None else None,
            )
        }
    )
    save_campaign(store, storage, updated)
    return updated


@dataclass(frozen=True)
class Measured:
    """`measure_recorded`'s answer: the report, and the campaign as stored (None when nothing was stored)."""

    report: IncrementalityReport
    campaign: Campaign | None
    matured: bool
    """False while some customer's outcome window is open: nothing was stored (CAMPAIGN_NOT_MATURED)."""


def measure_recorded(
    storage: Storage,
    store: CampaignStore,
    campaign: Campaign,
    *,
    as_of: datetime,
    outcome_window_days: int | None,
    covariate_column: str | None,
    outcome_kind: Literal["binary", "continuous"] | None,
    ledger_engine: Callable[[], Engine | None] | None,
) -> Measured:
    """Measure a campaign that has its outcomes through the one path, against its plan (`POST /campaigns/{id}/measure`).

    Raises `CycleError` with the route's codes: `CAMPAIGN_OUTCOMES_MISSING`, `CAMPAIGN_EPOCH_MISMATCH`,
    `TEST_PLAN_CHANGED`, `COVARIATE_NOT_BEFORE_CAMPAIGN`, `CAMPAIGN_INVALID`. A campaign some of whose
    outcome windows are still open stores nothing and answers `matured=False`.
    """
    from engine.measurement.audit import AUDIT_FILENAME, AuditReadout, label_report
    from engine.measurement.campaign import CAMPAIGN_EPOCH_MISMATCH
    from engine.measurement.continuous import COVARIATE_NOT_BEFORE_CAMPAIGN, CovariateNotBeforeCampaignError
    from engine.measurement.measure import measure_campaign
    from engine.measurement.plan import TEST_PLAN_CHANGED, TestPlan, TestPlanChangedError
    from engine.measurement.programme import PROGRAMME_FILENAME, ProgrammeReadout
    from engine.measurement.segments import SEGMENT_EFFECTS_FILENAME
    from engine.uplift.contracts import IncrementalityStatus

    campaign_id = campaign.campaign_id
    if campaign.outcomes is None:
        raise CycleError(CAMPAIGN_OUTCOMES_MISSING, "Add the campaign's outcomes file first.")
    assignment = read_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
    outcomes = read_frame(storage, campaign_key(campaign_id, OUTCOMES_FILENAME))
    plan = _stored(storage, campaign_key(campaign_id, TEST_PLAN_FILENAME), TestPlan)
    window = outcome_window_days if outcome_window_days is not None else campaign.outcome_window_days
    mismatch = epoch_refusal(storage, campaign, ledger_engine=ledger_engine, window=window, as_of=as_of)
    if mismatch is not None:
        raise CycleError(CAMPAIGN_EPOCH_MISMATCH, mismatch)
    # The pre-registered covariate is measured with unless another one is named: leaving it out of the
    # request is not a change of plan.
    covariate = covariate_column
    if covariate is None and plan is not None:
        covariate = plan.covariate_column
    # Plan J M102: the outcome is read as the plan registered it (a different kind named is a change of plan,
    # refused by `measure_campaign`), else as asked, else as yes/no. An audited or programme campaign that
    # measured an amount without a plan keeps its kind (M103).
    audit: AuditReadout | None = _stored(storage, campaign_key(campaign_id, AUDIT_FILENAME), AuditReadout)
    programme = _stored(storage, campaign_key(campaign_id, PROGRAMME_FILENAME), ProgrammeReadout)
    kind = (
        outcome_kind
        or (plan.outcome_kind if plan is not None else None)
        or (audit.outcome_kind if audit is not None else None)
        or (programme.outcome_kind if programme is not None else None)
        or "binary"
    )
    offers = {} if audit is None or not audit.offers else offer_arguments(audit)  # several offers (M103)
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
            outcome_kind=kind,
            covariate_date_column=covariate_date_column(campaign.outcomes, covariate),
            **offers,
        )
    except TestPlanChangedError as exc:
        raise CycleError(TEST_PLAN_CHANGED, str(exc)) from exc
    except CovariateNotBeforeCampaignError as exc:
        raise CycleError(COVARIATE_NOT_BEFORE_CAMPAIGN, str(exc), status=422) from exc
    except ValueError as exc:
        raise CycleError(CAMPAIGN_INVALID, str(exc), status=422) from exc
    if report.status is IncrementalityStatus.IMMATURE or report.rows_immature:
        return Measured(report=report, campaign=None, matured=False)
    if audit is not None:  # an outside campaign keeps the claim its numbers support (M103)
        report = label_report(report, audit.causal_basis)
    segments = segment_effects(
        storage,
        assignment,
        outcomes,
        report,
        campaign=campaign,
        outcome=campaign.outcomes,
        control_level=audit.control_level if audit is not None else None,
    )
    storage.write_model(campaign_key(campaign_id, REPORT_FILENAME), report)
    storage.write_model(campaign_key(campaign_id, SEGMENT_EFFECTS_FILENAME), segments)
    updated = campaign.model_copy(
        update={
            "status": CampaignStatus.LIVE if report.early_look else CampaignStatus.MEASURED,
            "measured_at": report.computed_at,
        }
    )
    save_campaign(store, storage, updated)
    refresh_contacts(storage, updated, report, audit=audit, now=as_of)
    return Measured(report=report, campaign=updated, matured=True)


def segment_effects(
    storage: Storage,
    assignment: Any,
    outcomes: Any,
    report: IncrementalityReport,
    *,
    campaign: Campaign,
    outcome: CampaignOutcomes,
    control_level: str | None = None,
) -> SegmentEffects:
    """`segment_effects.json` of a measured campaign (Plan J M104, DEC-1314): each band, segment and offer.

    The offers are the audited file's (each treated customer's offer, against the shared control), else, for
    a run that chose the offer per customer, the offer its policy gave every customer (DEC-1311 (af)).
    """
    from engine.measurement.measure import measure_campaign_segments
    from engine.measurement.segments import policy_offers

    offers = None
    if "offer" in assignment.columns:
        offers = assignment["offer"]
    elif campaign.intended_source == "offer_choice" and campaign.run_ids:
        offers = policy_offers(storage, campaign.run_ids[0], assignment, campaign.primary_key)
    return measure_campaign_segments(
        assignment,
        outcomes,
        report,
        primary_key=campaign.primary_key,
        outcome_column=outcome.outcome_column,
        positive_label=outcome.positive_label,
        intended_column=INTENDED_COLUMN,
        treatment_time=campaign.treatment_start,
        treatment_date_column=outcome.treatment_date_column,
        offers=offers,
        control_level=control_level,
    )


def offer_arguments(audit: AuditReadout) -> dict[str, Any]:
    """`measure_campaign`'s several-offer arguments for an audited campaign with more than one offer."""
    return {"arm_column": "offer", "arms": list(audit.offers or ()), "control_level": audit.control_level}


def covariate_date_column(outcomes: CampaignOutcomes, covariate: str | None) -> str | None:
    """The date column copied with `covariate`, when the outcomes were given that covariate (Plan J M102)."""
    if covariate is None or outcomes.covariate_column != covariate:
        return None
    return outcomes.covariate_date_column


def epoch_refusal(
    storage: Storage,
    campaign: Campaign,
    *,
    ledger_engine: Callable[[], Engine | None] | None,
    window: int | None,
    as_of: datetime,
) -> str | None:
    """`epoch_mismatch` over the campaign's runs and, for a persistent holdout, the ledger's current epoch.

    The outcomes are all in at the end of the outcome window counted from the treatment start; with no
    window, or per-row treatment dates the record does not hold, the measurement's own moment.
    `ledger_engine` is asked for the platform database only when the campaign's holdout is persistent.
    """
    from engine.holdout.assign import run_holdout_spec
    from engine.holdout.salt import HoldoutLedger
    from engine.holdout.spec import PERSISTENT_SCOPES

    run_specs = {run_id: run_holdout_spec(storage, run_id) for run_id in campaign.run_ids}
    ledger = None
    if campaign.holdout_scope in PERSISTENT_SCOPES and campaign.holdout_scope_key is not None:
        engine = ledger_engine() if ledger_engine is not None else None
        if engine is not None:
            ledger = HoldoutLedger(engine).entry(campaign.holdout_scope, campaign.holdout_scope_key)
    outcomes = campaign.outcomes
    per_row = outcomes is not None and outcomes.treatment_date_column is not None
    closes = as_of if window is None or per_row else campaign.treatment_start + timedelta(days=window)
    return epoch_mismatch(campaign, run_specs, ledger, outcomes_in_by=min(closes, as_of))


def contact_readout(
    assignment: Any,
    outcomes: Any,
    contacts: Any,
    campaign: Campaign,
    report: IncrementalityReport | None,
    *,
    unlisted: UnlistedRule,
    offers: tuple[str, ...] | None,
    now: datetime,
    source: tuple[str | None, bool] = (None, False),
) -> ContactReadout:
    """The contact readout of `campaign` from its frames; the effect on the contacted only with a report.

    `source` is the contact file's upload id and whether it was generated (DEC-1314): recorded on the readout
    so a Value Proof Pack never presents a generated contact file to finance.
    """
    from engine.measurement.reconcile import reconcile_contacts

    held = campaign.outcomes
    measured = report is not None and held is not None and outcomes is not None
    readout = reconcile_contacts(
        assignment,
        outcomes if measured else None,
        contacts,
        campaign_id=campaign.campaign_id,
        primary_key=campaign.primary_key,
        outcome_column=held.outcome_column if measured and held is not None else None,
        positive_label=held.positive_label if held is not None else None,
        outcome_kind=(report.outcome_kind or "binary") if report is not None else "binary",
        treatment_time=campaign.treatment_start,
        treatment_date_column=held.treatment_date_column if held is not None else None,
        outcome_window_days=(
            report.outcome_window_days if report is not None else campaign.outcome_window_days
        ),
        as_of=report.as_of if measured and report is not None else None,
        report_treated_rows=report.treated_rows if report is not None else None,
        report_control_rows=report.control_rows if report is not None else None,
        unlisted=unlisted,
        intended_column=INTENDED_COLUMN,
        offer_column="offer" if offers else None,
        first_offer=offers[0] if offers else None,
        computed_at=now,
    )
    upload_id, synthetic = source
    return readout.model_copy(update={"contact_upload_id": upload_id, "synthetic": synthetic})


def refresh_contacts(
    storage: Storage,
    campaign: Campaign,
    report: IncrementalityReport | None,
    *,
    audit: AuditReadout | None,
    now: datetime,
) -> None:
    """Recompute `contact_readout.json` after a (re)measurement, when the campaign has a contact file."""
    from engine.measurement.reconcile import ContactReadout

    contact_key = campaign_key(campaign.campaign_id, CONTACT_FILENAME)
    if not storage.exists(contact_key):
        return
    readout_key = campaign_key(campaign.campaign_id, CONTACT_READOUT_FILENAME)
    previous = _stored(storage, readout_key, ContactReadout)
    outcomes = (
        read_frame(storage, campaign_key(campaign.campaign_id, OUTCOMES_FILENAME))
        if campaign.outcomes is not None
        else None
    )
    readout = contact_readout(
        read_frame(storage, campaign_key(campaign.campaign_id, ASSIGNMENT_FILENAME)),
        outcomes,
        read_frame(storage, contact_key),
        campaign,
        report,
        unlisted=previous.unlisted_customers if previous is not None else "unknown",
        offers=audit.offers if audit is not None else None,
        now=now,
        source=(previous.contact_upload_id, previous.synthetic) if previous is not None else (None, False),
    )
    storage.write_model(readout_key, readout)


# ---------------------------------------------------------------------------
# The loop's steps
# ---------------------------------------------------------------------------
def treat_list_step(
    storage: Storage,
    store: CampaignStore,
    *,
    config: UseCaseConfig,
    client_id: str | None,
    config_root: Any,
    created_by: str,
    now: datetime,
) -> StepOutcome:
    """The newest finished scoring run without a campaign: its treat list, then its campaign record."""
    from engine.decide.treat_list import TreatListError, ensure_treat_list
    from engine.scheduling.firing import latest_scored_run

    run = latest_scored_run(storage, config.id, client_id)
    if run is None or store.list(run_id=run.run_id, limit=1) or run.finished_at is None:
        return StepOutcome("NOTHING_NEW")
    try:
        ensure_treat_list(storage, run.run_id, config_root=config_root)
    except TreatListError as exc:
        raise CycleError(getattr(exc, "code", RUN_NOT_SCORED), str(exc)) from exc
    campaign = open_campaign(
        storage,
        store,
        run,
        config,
        finished_at=run.finished_at,
        treatment_start=None,
        bands=None,
        outcome_window_days=default_outcome_window(config),
        name=None,
        created_by=created_by,
        now=now,
    )
    _LOGGER.info(
        "cycle.treat_list run=%s campaign=%s rows=%d", run.run_id, campaign.campaign_id, campaign.counts.rows
    )
    return StepOutcome(
        "TREAT_LIST_READY",
        run_id=run.run_id,
        campaign_id=campaign.campaign_id,
        alert=(
            f"The treat list of the latest scoring run for {config.name} is ready to download (run {run.run_id}). "
            f"Its campaign {campaign.campaign_id} is recorded as sent when the run finished; enter the day it "
            "really went out on the campaign's page if it differs."
        ),
    )


def measure_step(
    storage: Storage,
    store: CampaignStore,
    connections: ConnectionStore,
    *,
    config: UseCaseConfig,
    client_id: str | None,
    pull: OutcomePullSpec,
    ledger_engine: Callable[[], Engine | None] | None,
    now: datetime,
) -> StepOutcome:
    """Once the newest live campaign's window has closed: read its outcomes for that window and measure it."""
    from engine.measurement.pull import PULL_SOURCE_FILENAME, DateWindow, pull_frame

    live = [
        campaign
        for campaign in store.list(use_case_id=config.id, limit=None)
        if campaign.kind is CampaignKind.SCORED
        and campaign.status is CampaignStatus.LIVE
        and _client_matches(storage, campaign, client_id)
    ]
    if not live:
        return StepOutcome("NOTHING_TO_MEASURE")
    closed = [campaign for campaign in live if _window_end(campaign) <= now]
    if not closed:
        newest = max(live, key=lambda campaign: (campaign.treatment_start, campaign.campaign_id))
        return StepOutcome("CAMPAIGN_NOT_MATURED", campaign_id=newest.campaign_id)
    campaign = max(closed, key=lambda campaign: (campaign.treatment_start, campaign.campaign_id))
    window = DateWindow(
        column=pull.date_column,
        date_from=campaign.treatment_start.date(),
        date_to=min(_window_end(campaign), now).date(),
    )
    frame, record = pull_frame(
        connections,
        pull.connection_id,
        pull.selection,
        window=window,
        limit_bytes=config.validation.max_file_size_mb * 1024 * 1024,
        now=now,
    )
    named = record.table or record.path or "outcomes"
    upload = store_frame_upload(
        storage,
        config,
        frame,
        file_name=f"{named} {window.date_from.isoformat()} to {window.date_to.isoformat()}",
        mode=RunMode.SCORE,
        now=now,
    )
    storage.write_model(upload_key(upload.upload_id, PULL_SOURCE_FILENAME), record)
    given = record_outcomes(
        storage,
        store,
        campaign,
        frame,
        upload_id=upload.upload_id,
        file_name=upload.file_name,
        config=config,
        outcome_column=pull.outcome_column,
        positive_label=pull.positive_label,
        treatment_date_column=None,
        covariate_column=None,
        covariate_date_column=None,
        now=now,
    )
    measured = measure_recorded(
        storage,
        store,
        given,
        as_of=now,
        outcome_window_days=None,
        covariate_column=None,
        outcome_kind=None,
        ledger_engine=ledger_engine,
    )
    if not measured.matured:
        return StepOutcome("CAMPAIGN_NOT_MATURED", campaign_id=campaign.campaign_id)
    if measured.report.early_look:
        return StepOutcome("EARLY_LOOK", campaign_id=campaign.campaign_id)
    _LOGGER.info("cycle.measure campaign=%s rows=%d", campaign.campaign_id, record.rows)
    return StepOutcome(
        "CAMPAIGN_MEASURED",
        run_id=campaign.run_ids[0] if campaign.run_ids else None,
        campaign_id=campaign.campaign_id,
        alert=(
            f"The campaign {campaign.campaign_id} for {config.name} has been measured: its outcomes were read "
            f"from the saved connection for {window.date_from.isoformat()} to {window.date_to.isoformat()}. "
            "Open the campaign to see the result."
        ),
    )


def learn_step(
    storage: Storage,
    store: CampaignStore,
    registry: ModelRegistry,
    jobs: JobRunner,
    *,
    config: UseCaseConfig,
    client_id: str | None,
    config_root: Any,
    principal: Principal,
    client_tag: str | None,
    now: datetime,
) -> StepOutcome:
    """The newest measured campaign, unless it was learned from already: the next model, as a challenger."""
    measured = [
        campaign
        for campaign in store.list(use_case_id=config.id, limit=None)
        if campaign.kind is CampaignKind.SCORED
        and campaign.status is CampaignStatus.MEASURED
        and _client_matches(storage, campaign, client_id)
    ]
    if not measured:
        return StepOutcome("NOTHING_TO_LEARN")
    campaign = max(measured, key=lambda item: (item.treatment_start, item.campaign_id))
    if storage.exists(campaign_key(campaign.campaign_id, LEARNED_FILENAME)):
        return StepOutcome("NOTHING_TO_LEARN", campaign_id=campaign.campaign_id)
    started = learn_from_campaign(
        storage,
        registry,
        jobs,
        campaign=campaign,
        config_root=config_root,
        principal=principal,
        client_tag=client_tag,
        now=now,
    )
    return StepOutcome(
        "LEARNING_STARTED", run_id=started.run_id, running=True, campaign_id=campaign.campaign_id
    )


def learn_from_campaign(
    storage: Storage,
    registry: ModelRegistry,
    jobs: JobRunner,
    *,
    campaign: Campaign,
    config_root: Any,
    principal: Principal,
    client_tag: str | None,
    now: datetime,
) -> RunRecord:
    """Start the uplift training run that learns from a measured campaign (M106's frame), as a challenger.

    The engine half of `POST /runs/{id}/measure/learn` for a campaign: the frame is M106's
    (`build_randomised_frame` for a cycle that engaged the holdout service, refused with `LEARN_NO_OVERLAP`
    when part of its customers had no chance of contact; `build_experiment_frame` otherwise), stored as a
    training upload, checked by Phase 1's and the six uplift checks, and trained with
    `governance.approval_required` forced on, so its model always waits for an Approver.
    """
    from engine.config import ProblemType, resolve_config
    from engine.holdout.spec import HOLDOUT_REPORT_FILENAME, HoldoutAssignmentReport
    from engine.measurement.learn import LEARN_NO_OVERLAP, LEARN_RECORD_FILENAME, overlap_refusal

    if not campaign.run_ids or campaign.outcomes is None:
        raise CycleError(LEARN_NOT_READY, "This campaign has no scoring run or no outcomes to learn from.")
    run_id = campaign.run_ids[0]
    try:
        record = storage.read_model(run_key(run_id, "run.json"), RunRecord)
    except StorageError as exc:
        raise CycleError(RUN_NOT_SCORED, "The scoring run of this campaign is no longer stored.") from exc
    if record.mode is not RunMode.SCORE or record.state is not RunState.DONE:
        raise CycleError(RUN_NOT_SCORED, "A model is learned from a finished scoring run's campaign.")
    configured = resolve_config(record.use_case_id, root=config_root).config
    holdout: HoldoutAssignmentReport | None = _stored(
        storage, run_key(run_id, HOLDOUT_REPORT_FILENAME), HoldoutAssignmentReport
    )
    if holdout is not None and (refusal := overlap_refusal(holdout)) is not None:
        raise CycleError(LEARN_NO_OVERLAP, refusal)
    report = _stored(storage, campaign_key(campaign.campaign_id, REPORT_FILENAME), _report_model())
    readiness = _readiness(report, configured)
    if readiness is not None:
        raise CycleError(LEARN_NOT_READY, readiness)
    treatment, frame, learned = _experiment(storage, record, campaign, configured, holdout, now=now)
    if holdout is not None and learned is not None:
        from engine.measurement.learn import frame_refusal

        late = frame_refusal(
            learned, explore_candidates=holdout.explore_candidates, min_rows=configured.uplift.min_arm_rows
        )
        if late is not None:
            raise CycleError(LEARN_NO_OVERLAP, late)
    upload = store_frame_upload(
        storage,
        configured,
        frame,
        file_name=f"{record.file_name or run_id} + campaign outcomes",
        mode=RunMode.TRAIN,
        now=now,
        synthetic=record.synthetic,
    )
    overrides: dict[str, Any] = {
        "target.positive_label": 1,
        "governance": {"approval_required": True},  # like a scheduled training run (DEC-743): a challenger
        "uplift.treatment_column": treatment,
    }
    if configured.problem_type is not ProblemType.UPLIFT:
        overrides["problem_type"] = ProblemType.UPLIFT.value
    started = start_learn_run(
        storage,
        registry,
        jobs,
        use_case_id=record.use_case_id,
        overrides=overrides,
        upload=upload,
        primary_key=record.primary_key,
        target=target_of(configured),
        config_root=config_root,
        requested_by=principal.user_id,
        synthetic=record.synthetic,
        client_tag=client_tag,
        now=now,
    )
    if learned is not None:
        storage.write_model(
            run_key(started.run_id, LEARN_RECORD_FILENAME),
            learned.model_copy(update={"uplift_run_id": started.run_id}),
        )
    storage.write_model(
        campaign_key(campaign.campaign_id, LEARNED_FILENAME),
        CampaignLearned(
            campaign_id=campaign.campaign_id, run_id=run_id, uplift_run_id=started.run_id, learned_at=now
        ),
    )
    _LOGGER.info(
        "cycle.learn campaign=%s run=%s uplift_run=%s rows=%d",
        campaign.campaign_id,
        run_id,
        started.run_id,
        len(frame.index),
    )
    return started


def start_learn_run(
    storage: Storage,
    registry: ModelRegistry,
    jobs: JobRunner,
    *,
    use_case_id: str,
    overrides: Mapping[str, Any],
    upload: StoredUpload,
    primary_key: Any,
    target: str,
    config_root: Any,
    requested_by: str,
    synthetic: bool,
    client_tag: str | None,
    now: datetime,
) -> RunRecord:
    """Check an experiment upload and start its uplift training run: `POST /uplift/runs`, call for call, minus HTTP.

    Phase 1's checks and the six uplift checks run first, and their reports are written beside the upload
    as the route writes them; a refusal is `VALIDATION_FAILED` or `UPLIFT_VALIDATION_FAILED` (409).
    """
    from engine.config import get_catalog, resolve_config
    from engine.contracts import Severity
    from engine.decide.catalogue import stamp_checked_catalogue, stamped_at_creation
    from engine.keys import normalise_key, split_config_for_key
    from engine.pipeline import Pipeline
    from engine.runs import build_job_fn, create_run, job_spec_for, write_job_spec
    from engine.stages import ingest, validate
    from engine.uplift.checks import run_uplift_checks
    from engine.uplift.contracts import UPLIFT_VALIDATION_FILENAME
    from engine.uplift.flow import check_seed

    resolved = resolve_config(use_case_id, dict(overrides), root=config_root, now=now)
    key = normalise_key(primary_key)
    resolved = split_config_for_key(resolved, key)
    config = resolved.config
    frame = ingest.read_upload(
        storage, upload.source_key, file_format="parquet", row_cap=ingest.profile_row_cap(config)
    ).frame
    report = validate.validate_for_training(
        frame,
        config,
        primary_key=key,
        target=target,
        acknowledged=config.validation.acknowledged,
        upload_id=upload.upload_id,
        row_count=upload.profile.row_count,
    )
    checked = run_uplift_checks(
        frame,
        config,
        primary_key=key,
        target=target,
        upload_id=upload.upload_id,
        acknowledged=config.validation.acknowledged,
        seed=check_seed(upload.upload_id),
    )
    storage.write_model(upload_key(upload.upload_id, UPLOAD_VALIDATION_FILENAME), report)
    storage.write_model(upload_key(upload.upload_id, UPLIFT_VALIDATION_FILENAME), checked.report)
    if not report.passed:
        count = report.error_count
        raise CycleError(
            "VALIDATION_FAILED",
            f"{count} problem{'s' if count != 1 else ''} must be fixed before the learned experiment can be used.",
        )
    if not checked.report.passed:
        blocking = [
            check.code
            for check in checked.report.checks
            if check.severity is Severity.ERROR and not check.acknowledged
        ]
        raise CycleError(
            "UPLIFT_VALIDATION_FAILED",
            "The campaign's rows cannot be used as an experiment yet: " + ", ".join(blocking) + ".",
        )
    catalog = get_catalog(config_root)
    record = create_run(
        storage,
        Pipeline(storage, registry, jobs),
        resolved=resolved,
        catalog=catalog,
        upload=upload,
        profile=upload.profile,
        report=report,
        mode=RunMode.TRAIN,
        primary_key=key,
        target=target,
        model_choice=config.uplift.learner.value,
        model_version_id=None,
        now=now,
        requested_by=requested_by,
        synthetic=synthetic,
    )
    storage.write_model(
        run_key(record.run_id, UPLIFT_VALIDATION_FILENAME),
        checked.report.model_copy(update={"run_id": record.run_id}),
    )
    if stamped_at_creation(config, scoring=False):
        stamp_checked_catalogue(
            storage, record.run_id, config, root=config_root, created_at=record.created_at
        )
    spec = job_spec_for(record, upload=upload, client_id=client_tag)
    write_job_spec(storage, spec)
    jobs.submit(spec.job_id, build_job_fn(spec, storage=storage, registry=registry))
    return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _report_model() -> Any:
    from engine.uplift.contracts import IncrementalityReport

    return IncrementalityReport


def _readiness(report: Any, config: UseCaseConfig) -> str | None:
    """Why a measured campaign cannot teach a model yet, or None (step 4's `learn_readiness`, M102's amount rule)."""
    from engine.uplift.measure import learn_readiness

    if report is None:
        return "The campaign has no measured result yet."
    if report.early_look:
        return "The campaign's result is an early look; a model is learned from the final result only."
    if report.outcome_kind == "continuous":
        return (
            "This campaign was measured on an amount. Learning who to contact next time needs a yes/no "
            "outcome: measure it again on one, such as whether the customer bought."
        )
    readiness = learn_readiness(report, config.uplift)
    return None if readiness.ready else readiness.reason


def _experiment(
    storage: Storage,
    record: RunRecord,
    campaign: Campaign,
    config: UseCaseConfig,
    holdout: Any,
    *,
    now: datetime,
) -> tuple[str, pd.DataFrame, Any]:
    """`(treatment column, experiment frame, learn record or None)` from the run and the campaign's outcomes."""
    import pandas as pd

    from engine.config import ResolvedConfig
    from engine.holdout.assign import HOLDOUT_ASSIGNMENT_FILENAME
    from engine.holdout.spec import effective_holdout_fraction
    from engine.measurement.learn import build_randomised_frame, delivery_from
    from engine.measurement.reconcile import ContactReadout
    from engine.runs import RUN_CONFIG_FILENAME, job_spec_key, read_job_spec
    from engine.stages import export, ingest
    from engine.uplift.measure import build_experiment_frame, treatment_column_for

    assert campaign.outcomes is not None
    try:
        spec = read_job_spec(storage, job_spec_key(record.run_id))
        read = ingest.read_upload(storage, spec.upload_key, file_format=spec.upload_format)
        if read.truncated:
            read = ingest.read_upload(
                storage, spec.upload_key, file_format=spec.upload_format, row_cap=read.row_count
            )
        inputs = read.frame
        scores = pd.read_parquet(
            io.BytesIO(storage.read_bytes(run_key(record.run_id, export.SCORES_PARQUET)))
        )
    except (StorageError, ingest.IngestError) as exc:
        raise CycleError(
            LEARN_NOT_READY,
            "The file this run scored is no longer stored, so there is nothing to learn from.",
        ) from exc
    outcomes = read_frame(storage, campaign_key(campaign.campaign_id, OUTCOMES_FILENAME))
    treatment = treatment_column_for([str(name) for name in inputs.columns])
    target = target_of(config)
    try:
        if holdout is None:
            frame = build_experiment_frame(
                inputs,
                scores,
                outcomes,
                primary_key=record.primary_key,
                outcome_column=campaign.outcomes.outcome_column,
                positive_label=campaign.outcomes.positive_label,
                target_column=target,
                treatment_column=treatment,
            )
            return treatment, frame, None
        try:
            assignment = pd.read_parquet(
                io.BytesIO(storage.read_bytes(run_key(record.run_id, HOLDOUT_ASSIGNMENT_FILENAME)))
            )
            run_config = storage.read_model(
                run_key(record.run_id, RUN_CONFIG_FILENAME), ResolvedConfig
            ).config
        except StorageError as exc:
            raise CycleError(
                LEARN_NOT_READY,
                "This run's record of who was held back and who was explored is no longer stored, so there is "
                "nothing to learn from.",
            ) from exc
        readout = _stored(
            storage, campaign_key(campaign.campaign_id, CONTACT_READOUT_FILENAME), ContactReadout
        )
        built = build_randomised_frame(
            inputs,
            scores,
            assignment,
            outcomes,
            run_config,
            primary_key=record.primary_key,
            outcome_column=campaign.outcomes.outcome_column,
            positive_label=campaign.outcomes.positive_label,
            target_column=target,
            treatment_column=treatment,
            run_id=record.run_id,
            model_id=record.model_version_id,
            holdout_fraction=effective_holdout_fraction(run_config.actions),
            explore_fraction=holdout.spec.explore_fraction,
            samples=config.uplift.bootstrap_samples,
            now=now,
            delivery=delivery_from([readout] if readout is not None else []),
        )
    except ValueError as exc:
        raise CycleError(MEASURE_INVALID, str(exc), status=422) from exc
    return treatment, built.frame, built.record


def _window_end(campaign: Campaign) -> datetime:
    """When the campaign's last outcome is in: its treatment start plus its window (the start with none)."""
    return campaign.treatment_start + timedelta(days=campaign.outcome_window_days or 0)


def _client_matches(storage: Storage, campaign: Campaign, client_id: str | None) -> bool:
    """Whether the campaign's scoring run belongs to `client_id` (any client when None)."""
    if client_id is None:
        return True
    if not campaign.run_ids:
        return False
    record = _stored(storage, run_key(campaign.run_ids[0], "run.json"), RunRecord)
    return record is not None and record.client_id == client_id


def _stored(storage: Storage, key: str, model: type[Any]) -> Any:
    try:
        return storage.read_model(key, model)
    except StorageError:
        return None


def _discard(storage: Storage, *keys: str) -> None:
    for key in keys:
        try:
            storage.delete(key)
        except (StorageError, OSError):  # already gone, or never written
            continue
