"""Campaigns: the one record a campaign's measurement attaches to, and its registered test plan (Plan J M94).

A scoring run makes a list; a **campaign** is that list going out on a given day. These routes create
one from a finished scoring run, give it its outcomes, measure it through the one measurement path
(`engine.measurement.measure.measure_campaign`, which `/runs/{id}/campaign-results` also uses) and
fix how it will be judged before it is (`engine.measurement.plan`):

* `POST /campaigns {run_id, treatment_start?, bands?, outcome_window_days?, name?}` (Analyst) - kind
  `scored`. The assignment (`campaigns/<id>/assignment.parquet`) is built from the run's scores:
  holdout, intended population (an uplift run's `intended_treatment` - for a run that chose the offer per
  customer, the customers its policy would give an offer to, DEC-1311 (ai) - or the treat bands), band or
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

**Amounts and the adjusted estimate (M102, DEC-1312).** A plan may register `outcome_kind: continuous`
(revenue) with `mde_value`, `outcome_sd` and an `expected_rho2` for its covariate. `POST
/campaigns/{id}/outcomes` then copies the covariate and the date it was measured up to
(`covariate_column`, `covariate_date_column`; a covariate without its date is **422
`COVARIATE_NOT_BEFORE_CAMPAIGN`**), and the measurement reads the amount with Welch's interval and, with
the registered covariate, the CUPED adjusted estimate. A covariate dated on or after a customer's
treatment date (compared by day: a covariate dated the day of contact is too late) is **422
`COVARIATE_NOT_BEFORE_CAMPAIGN`** and nothing is stored; a covariate or outcome kind the plan did not
register is **409 `TEST_PLAN_CHANGED`**, and so is a covariate on an amount with no plan (a yes/no
outcome never uses one, so it is ignored as before). The plan preview's points of a plan on an amount
carry `mde_amount` (`mde_continuous` with the plan's `outcome_sd` and `expected_rho2`) and no `mde_pp`.

**Holdout epochs (M92).** `POST /campaigns` records the scoring run's holdout (scope, key and epoch,
from `holdout_assignment.json`; a run that wrote none drew per run). `POST /campaigns/{id}/measure`
refuses **409 `CAMPAIGN_EPOCH_MISMATCH`** when the runs behind the assignment span two epochs, when
the run's epoch no longer matches the record, or when the persistent holdout was redrawn before the
outcomes were all in (`engine.measurement.campaign.epoch_mismatch`).

**Audit any campaign, and the programme readout (M103, DEC-1313).** Three routes, all Analyst and
audited, on uploads that went through `POST /uploads`:

* `POST /campaigns/audit` - an assignment file (who was in which group), an outcomes file and the column
  mappings create an `external` campaign (no run record; the report's `run_id` is the campaign id) and
  measure it through `measure_campaign`, unchanged. What the numbers may claim is **Causal** only when the
  groups are verified random, **Random by your statement, not verified** when the person said so and the
  file cannot test it, **Descriptive only** otherwise (`engine.measurement.audit`). Nothing is stored
  until the result is final: results not yet in are **409 `CAMPAIGN_NOT_MATURED`** with the day, as for any
  campaign.
* `POST /campaigns/programme` - every customer not held back against the universal holdout's members over a
  period (intent to treat; `engine.measurement.programme`), as a plain difference in means (a plan sent with a
  finished period could not have been fixed in advance: **409 `TEST_PLAN_INVALID`**). **409
  `PROGRAMME_NO_HOLDOUT`** without a universal holdout, and **409 `CAMPAIGN_EPOCH_MISMATCH`** when the holdout
  began after the period did.
* `POST /campaigns/{id}/contacts` - who was actually contacted (any campaign): the contact rate, the
  contamination of the held-back group and, labelled secondary, the effect on the contacted
  (`engine.measurement.reconcile`). Optional on the two routes above.

**Each group's effect (M104, DEC-1314).** Every route here that stores a campaign report (`measure`, `audit`,
`programme`) stores `campaigns/<id>/segment_effects.json` beside it (`engine.measurement.measure
.measure_campaign_segments`): the measured effect and interval per band, predicted segment and offer, with the
false-alarm guard the Value Proof Pack's backfire check reads (`GET /pilot/proof/{campaign_id}`). Counts only.

**What needs attention, and the value proven to date (M105, DEC-1315).** `GET /campaigns/summary` (Viewer) is computed
from artefacts that already exist and holds no customer row (`engine.measurement.summary`): cards for a campaign
with no control group, an early look, an underpowered plan, contamination, drift, a challenger waiting, a backfiring
group, a model that does not beat risk ranking and an effect that is fading, each naming the artefact it read; and
the sum of every campaign's measured lower bound, one total per unit, with the campaigns that cannot be added
listed apart and the ones excluded with their reason. It is declared before `/campaigns/{campaign_id}` so
"summary" is not read as an id.

Customer ids are never in a URL or an audit record: every route names a campaign, a run or an upload.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, Field, ValidationError, model_validator

from api.access import get_platform_engine, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, RegistryDep, SettingsDep, StorageDep
from api.routes.runs import load_run, requested_by
from api.routes.uplift import _finished_scoring_run, _read_all
from api.routes.uploads import http_error, load_upload, use_case_config
from api.schemas import ErrorBody, ErrorResponse
from engine.access.roles import Role
from engine.audit.events import content_hash
from engine.config import ConfigError, PrimaryKey, RunMode, StrictBase, UseCaseConfig
from engine.connections.base import ConnectorError
from engine.connections.store import ConnectionStore
from engine.contracts import RunRecord, RunState
from engine.holdout.assign import run_holdout_spec
from engine.holdout.salt import HoldoutLedger, configured_salt, salt_fingerprint, salt_id
from engine.holdout.spec import (
    HOLDOUT_SALT_CHANGED,
    UNIVERSAL_SCOPE_KEY,
)
from engine.measurement.audit import (
    AUDIT_FILENAME,
    AUDIT_SEED,
    AssignmentBasis,
    AuditInputError,
    AuditReadout,
    RandomnessCheck,
    audit_verdict,
    build_assignment_frame,
    build_outcomes_frame,
    check_randomness,
    decide_basis,
    earliest_date,
    label_report,
    rename_keys,
)
from engine.measurement.campaign import (
    ASSIGNMENT_FILENAME,
    CAMPAIGN_EPOCH_MISMATCH,
    CAMPAIGN_FILENAME,
    CAMPAIGN_NOT_FOUND,
    CAMPAIGN_NOT_MATURED,
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
    SqlCampaignStore,
    assignment_counts,
    campaign_key,
    new_campaign_id,
    read_frame,
    save_campaign,
    test_plan_version_filename,
    write_frame,
)
from engine.measurement.continuous import COVARIATE_NOT_BEFORE_CAMPAIGN
from engine.measurement.cycle import (  # Plan J M107 (DEC-1317): the routes' engine halves, shared with the loop
    CycleError,
    contact_readout,
    epoch_refusal,
    measure_recorded,
    open_campaign,
    record_outcomes,
    segment_effects,
    store_frame_upload,
    target_of,
)
from engine.measurement.measure import campaign_verdict_for, measure_campaign
from engine.measurement.plan import (
    TEST_PLAN_CHANGED,
    TEST_PLAN_EXISTS,
    TEST_PLAN_INVALID,
    TEST_PLAN_NOT_FOUND,
    TestPlan,
    TestPlanAmendment,
    TestPlanInput,
    freeze_plan,
    plan_inputs,
    realised_population,
)
from engine.measurement.planner import (
    MAX_HOLDOUT_SHARE,
    PowerPreviewPoint,
    PowerPreviewRequest,
    continuous_power_preview,
    cost_of_explore,
    power_preview,
)
from engine.measurement.programme import (
    PROGRAMME_FILENAME,
    PROGRAMME_LABEL,
    PROGRAMME_NO_HOLDOUT,
    ProgrammePeriod,
    ProgrammeReadout,
    period_window_days,
    programme_assignment,
)
from engine.measurement.pull import PULL_SOURCE_FILENAME, DateWindow, PullSelection, pull_frame
from engine.measurement.reconcile import (
    CONTACT_UNREADABLE,
    ContactFileError,
    ContactReadout,
    UnlistedRule,
    normalise_contacts,
)
from engine.measurement.segments import SEGMENT_EFFECTS_FILENAME
from engine.measurement.summary import (
    SUMMARY_NOT_TRACEABLE,
    CampaignSummary,
    SummaryTraceError,
    build_summary,
)
from engine.pilot.roi import outcome_is_good_by_default
from engine.storage import Storage, StorageError, upload_key
from engine.uplift.contracts import IncrementalityReport, IncrementalityStatus
from engine.uplift.measure import CampaignVerdict, detect_outcome_column
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
    "default_outcome_window",
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
        # Plan J M105 (DEC-1315): what wants attention and the value proven to date. It holds no customer row.
        ("GET", "/campaigns/summary"): RoutePolicy(
            role=Role.VIEWER,
            action="campaigns.summary",
            purpose="see what needs attention and the value proven",
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
        # Plan J M103 (DEC-1313): audit another tool's campaign, read the whole programme, say who was contacted.
        ("POST", "/campaigns/audit"): RoutePolicy(
            role=Role.ANALYST,
            action="campaigns.audit",
            purpose="audit a campaign another tool ran",
            object_type="campaign",
        ),
        ("POST", "/campaigns/programme"): RoutePolicy(
            role=Role.ANALYST,
            action="campaigns.programme",
            purpose="measure the whole programme against the universal holdout",
            object_type="campaign",
        ),
        ("POST", "/campaigns/{campaign_id}/contacts"): _on_campaign(
            Role.ANALYST, "campaigns.contacts", "say who a campaign actually contacted"
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
    """Body of `POST /campaigns/{id}/outcomes`: the outcomes file and how to read it.

    The outcomes are an upload (`upload_id`), or, since Plan J M107 (DEC-1317), rows read from a saved
    connection (`connection_id`, `selection` and the date window `date_column`, `date_from`, `date_to`):
    exactly one of the two.
    """

    upload_id: str | None = Field(
        default=None,
        description="Upload holding the customer id and the outcome after the campaign; or give `connection_id`.",
    )
    outcome_column: str | None = Field(
        default=None, description="The outcome column; found in the file when null."
    )
    positive_label: str | None = Field(default=None, description="Outcome value that counts as a conversion.")
    treatment_date_column: str | None = Field(
        default=None,
        description="Per-row treatment date in the file; the campaign's treatment start when null.",
    )
    covariate_column: str | None = Field(
        default=None,
        description="An amount from before the campaign to copy for the adjusted estimate (Plan J M102).",
    )
    covariate_date_column: str | None = Field(
        default=None,
        description="The date each covariate value was measured up to; required with covariate_column.",
    )
    # Plan J M107 (DEC-1317): read the outcomes from a saved connection instead of an upload. Read-only; from a
    # SQL database the only query is one quoted date window on `date_column` (no free SQL).
    connection_id: str | None = Field(
        default=None, max_length=64, description="A saved connection to read the outcomes from (Plan J M107)."
    )
    selection: PullSelection | None = Field(
        default=None, description="The table, file or folder (its newest file) holding the outcomes."
    )
    date_column: str | None = Field(
        default=None, min_length=1, max_length=256, description="The column that dates each row."
    )
    date_from: date | None = Field(default=None, description="The first day of outcomes read.")
    date_to: date | None = Field(default=None, description="The last day of outcomes read (included).")

    @model_validator(mode="after")
    def _one_source(self) -> CampaignOutcomesRequest:
        if (self.upload_id is None) == (self.connection_id is None):
            raise ValueError("give exactly one of upload_id or connection_id")
        pulled = (self.selection, self.date_column, self.date_from, self.date_to)
        if self.connection_id is None and any(value is not None for value in pulled):
            raise ValueError("selection, date_column, date_from and date_to go with connection_id")
        if self.connection_id is not None and any(value is None for value in pulled):
            raise ValueError(
                "outcomes read from a connection name selection, date_column, date_from and date_to"
            )
        if self.date_from is not None and self.date_to is not None and self.date_to < self.date_from:
            raise ValueError("date_to is before date_from")
        return self


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
    outcome_kind: Literal["binary", "continuous"] | None = Field(
        default=None,
        description=(
            "`binary` (a rate) or `continuous` (an amount such as revenue, a mean); the test plan's when null, "
            "else binary (Plan J M102)."
        ),
    )


class AssignmentFile(StrictBase):
    """The assignment upload of `POST /campaigns/audit` and how to read it (Plan J M103)."""

    upload_id: str = Field(
        description="Upload holding one row per customer: the id and which group they were in."
    )
    key_columns: tuple[str, ...] | None = Field(
        default=None,
        description="The id column(s) of this file when they are named differently from `primary_key`.",
    )
    arm_column: str = Field(
        description="The column saying contacted or held back (0/1, yes/no, or an offer)."
    )
    control_value: str | None = Field(
        default=None, description="The value meaning held back; read from the usual words when null."
    )
    treated_values: tuple[str, ...] | None = Field(
        default=None,
        description="The values meaning contacted, in order; with more than one, each is an offer measured against the control.",
    )
    sent_date_column: str | None = Field(
        default=None, description="The date each customer was contacted, if the file has it."
    )
    intended_column: str | None = Field(
        default=None,
        description="Whether the customer was meant to be contacted at all; every customer is when null (intent to treat).",
    )


class AuditOutcomesFile(StrictBase):
    """The outcomes upload of `POST /campaigns/audit`."""

    upload_id: str = Field(description="Upload holding the customer id and what happened to them.")
    key_columns: tuple[str, ...] | None = Field(
        default=None,
        description="The id column(s) of this file when they are named differently from `primary_key`.",
    )
    outcome_column: str | None = Field(
        default=None, description="The outcome column; found in the file when null."
    )
    positive_label: str | None = Field(
        default=None, description="The value that counts as a conversion (yes/no outcomes)."
    )
    outcome_kind: Literal["binary", "continuous"] = Field(
        default="binary", description="`binary` (yes/no) or `continuous` (an amount such as revenue)."
    )
    treatment_date_column: str | None = Field(
        default=None, description="The date each customer was contacted, if this file has it."
    )


class ContactFile(StrictBase):
    """A contact file: who was actually contacted (`POST /campaigns/{id}/contacts`, or beside an audit)."""

    upload_id: str = Field(description="Upload holding the customer id and whether they were contacted.")
    key_columns: tuple[str, ...] | None = Field(
        default=None,
        description="The id column(s) of this file when they are named differently from the campaign's.",
    )
    contacted_column: str = Field(description="The column saying whether each customer was contacted.")
    contacted_label: str | None = Field(
        default=None, description="The value meaning contacted; the usual yes/no words are read when null."
    )
    unlisted_customers: UnlistedRule = Field(
        default="unknown",
        description=(
            "How to read a customer the file does not list: `unknown` (left out), or `not_contacted` for a send log "
            "that lists only the customers it sent to."
        ),
    )


class AuditRequest(StrictBase):
    """Body of `POST /campaigns/audit`: a past campaign's groups and outcomes, and what is known about it."""

    name: str | None = Field(
        default=None, max_length=120, description="A short name; one is made up when null."
    )
    primary_key: PrimaryKey = Field(description="The customer id column(s), named the same in every file.")
    assignment: AssignmentFile
    outcomes: AuditOutcomesFile
    contact: ContactFile | None = Field(default=None, description="Who was actually contacted, if known.")
    assignment_basis: AssignmentBasis = Field(
        description="What you know about how the groups were chosen: `random` or `not_random`. Required: it is never assumed."
    )
    treatment_start: AwareDatetime | None = Field(
        default=None,
        description="When the campaign went out; the earliest date in the files when null (one of them must have dates).",
    )
    outcome_window_days: int | None = Field(
        default=None,
        ge=0,
        description="Days the outcome is counted over; every row counts as final when null.",
    )
    as_of: AwareDatetime | None = Field(
        default=None, description="Reference time for maturity; now when null, and never later than now."
    )
    outcome_is_good: bool = Field(
        default=True, description="False when the campaign aimed to make the outcome rarer (churn, defaults)."
    )


class ProgrammeOutcomes(StrictBase):
    """The outcomes upload of `POST /campaigns/programme`: one row per customer of the whole base."""

    upload_id: str = Field(description="Upload holding the customer id and the outcome over the period.")
    key_columns: tuple[str, ...] | None = Field(
        default=None, description="The id column(s) of this file, if named differently."
    )
    outcome_column: str | None = Field(
        default=None, description="The outcome column; found in the file when null."
    )
    positive_label: str | None = Field(
        default=None, description="The value that counts as a conversion (yes/no outcomes)."
    )
    outcome_kind: Literal["binary", "continuous"] | None = Field(
        default=None, description="`binary` or `continuous`; the plan's when null, else binary."
    )
    covariate_column: str | None = Field(
        default=None, description="An amount from before the period to adjust by; the plan must name it."
    )
    covariate_date_column: str | None = Field(
        default=None,
        description="The date each covariate value was measured up to; required with covariate_column.",
    )


class ProgrammeRequest(StrictBase):
    """Body of `POST /campaigns/programme`: the period and the outcomes of the whole customer base."""

    period: ProgrammePeriod
    outcome: ProgrammeOutcomes
    primary_key: PrimaryKey = Field(description="The customer id column(s) of the outcomes file.")
    plan: TestPlanInput | None = Field(
        default=None,
        description=(
            "Not accepted: a programme is read after its period has ended, so a plan sent with the outcomes "
            "could not have been fixed in advance (409 TEST_PLAN_INVALID). The difference in means is given."
        ),
    )
    contact: ContactFile | None = Field(default=None, description="Who was actually contacted, if known.")
    name: str | None = Field(
        default=None, max_length=120, description="A short name; one is made up when null."
    )
    as_of: AwareDatetime | None = Field(
        default=None, description="Reference time for maturity; now when null."
    )
    outcome_is_good: bool = Field(
        default=True, description="False when the programme aims to make the outcome rarer."
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
    # Plan J M103 (DEC-1313): left out of the answer while unset, so a scored campaign reads as it did.
    audit: AuditReadout | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="For an audited campaign: what its numbers can claim, and the test behind that.",
    )
    programme: ProgrammeReadout | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="For a programme readout: the period and the universal holdout it used.",
    )
    contacts: ContactReadout | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Who was actually contacted, once a contact file is added.",
    )


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
    record = load_run(storage, body.run_id)
    finished_at = _finished_scoring_run(record)
    config = use_case_config(record.use_case_id, root)
    window = (
        body.outcome_window_days if body.outcome_window_days is not None else default_outcome_window(config)
    )
    try:
        # Plan J M107 (DEC-1317): the engine half, shared with the monthly loop's treat-list step.
        campaign = open_campaign(
            storage,
            get_campaign_store(request),
            record,
            config,
            finished_at=finished_at,
            treatment_start=body.treatment_start,
            bands=body.bands,
            outcome_window_days=window,
            name=body.name,
            created_by=requested_by(request) or "local",
            now=utc_now(),
        )
    except CycleError as exc:
        raise _cycle_http(exc) from exc
    counts = campaign.counts
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


def default_outcome_window(config: UseCaseConfig) -> int | None:
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
    "/campaigns/summary",
    response_model=CampaignSummary,
    responses={500: {"model": ErrorResponse}},
    summary="What needs attention across the campaigns, and the value proven to date",
)
def campaigns_summary(
    request: Request, root: ConfigRootDep, storage: StorageDep, registry: RegistryDep
) -> CampaignSummary:
    """Declared before `/campaigns/{campaign_id}`, so "summary" is never read as a campaign id (DEC-1315 (a)).

    Reads every campaign, not the newest page the list shows: a total "to date" that dropped older proven
    campaigns would fall from one day to the next without saying so."""
    campaigns = get_campaign_store(request).list(limit=None)
    try:
        return build_summary(storage, campaigns, registry=registry, root=root)
    except SummaryTraceError as exc:
        _LOGGER.error("campaigns summary: %d figure(s) did not trace", len(exc.failures))
        raise http_error(
            500,
            SUMMARY_NOT_TRACEABLE,
            "A number of the campaigns' summary could not be traced back to the record it comes from, so the "
            "summary is not shown.",
        ) from exc


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
    audit = programme = None
    if (
        campaign.kind is CampaignKind.EXTERNAL
    ):  # Plan J M103: what the numbers of an outside campaign can claim
        audit = _stored(storage, campaign_key(campaign.campaign_id, AUDIT_FILENAME), AuditReadout)
        good = audit.outcome_is_good if audit is not None else good
    elif campaign.kind is CampaignKind.PROGRAMME:
        programme = _stored(storage, campaign_key(campaign.campaign_id, PROGRAMME_FILENAME), ProgrammeReadout)
        good = programme.outcome_is_good if programme is not None else good
    if report is None:
        verdict = None
    elif audit is not None:
        verdict = audit_verdict(report, audit, outcome_label=label)
    else:
        verdict = campaign_verdict_for(report, outcome_is_good=good, outcome_label=label)
    return CampaignView(
        campaign=campaign,
        report=report,
        verdict=verdict,
        outcome_is_good=good,
        plan=_stored(storage, campaign_key(campaign.campaign_id, TEST_PLAN_FILENAME), TestPlan),
        audit=audit,
        programme=programme,
        contacts=_stored(
            storage, campaign_key(campaign.campaign_id, CONTACT_READOUT_FILENAME), ContactReadout
        ),
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
    good = outcome_is_good_by_default(config.id, named or target_of(config), root)
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
    responses={**_ERRORS, 413: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    summary="Give a campaign its outcomes file: customer id, outcome and optionally a treatment date",
)
def add_campaign_outcomes(
    campaign_id: str,
    body: CampaignOutcomesRequest,
    request: Request,
    root: ConfigRootDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> CampaignView:
    store = get_campaign_store(request)
    campaign = _load(store, campaign_id)
    config = _config(campaign, root)
    if (
        body.connection_id is not None
    ):  # Plan J M107 (DEC-1317): the window's rows, read from a saved connection
        upload_id, file_name, frame = _pulled_outcomes(storage, settings, campaign, config, body)
    else:
        upload = load_upload(storage, body.upload_id or "")
        frame = _read_all(storage, upload.source_key, upload.file_format)
        upload_id, file_name = upload.upload_id, upload.file_name
    try:
        updated = record_outcomes(
            storage,
            store,
            campaign,
            frame,
            upload_id=upload_id,
            file_name=file_name,
            config=config,
            outcome_column=body.outcome_column,
            positive_label=body.positive_label,
            treatment_date_column=body.treatment_date_column,
            covariate_column=body.covariate_column,
            covariate_date_column=body.covariate_date_column,
            now=utc_now(),
        )
    except CycleError as exc:
        raise _cycle_http(exc) from exc
    rows = updated.outcomes.rows if updated.outcomes is not None else 0
    _LOGGER.info("campaigns.outcomes campaign=%s rows=%d", campaign_id, rows)
    return _view(storage, updated, root)


def _pulled_outcomes(
    storage: Storage,
    settings: Any,
    campaign: Campaign,
    config: UseCaseConfig | None,
    body: CampaignOutcomesRequest,
) -> tuple[str, str, Any]:
    """`(upload id, file name, rows)`: the date window's outcomes read from a saved connection (Plan J M107).

    The rows are kept as an ordinary upload, with `pull_source.json` beside it saying which connection, which
    table or file and which window; nothing is written to the client's system.
    """
    if config is None or body.connection_id is None or body.selection is None:
        raise http_error(
            422, CAMPAIGN_INVALID, "This campaign names no use case, so its outcomes cannot be read for it."
        )
    if body.date_column is None or body.date_from is None or body.date_to is None:  # the body's own check
        raise http_error(422, CAMPAIGN_INVALID, "Name the date column and the first and last day to read.")
    window = DateWindow(column=body.date_column, date_from=body.date_from, date_to=body.date_to)
    now = utc_now()
    try:
        frame, record = pull_frame(
            ConnectionStore(storage, settings),
            body.connection_id,
            body.selection,
            window=window,
            limit_bytes=config.validation.max_file_size_mb * 1024 * 1024,
            now=now,
        )
    except ConnectorError as exc:
        raise http_error(
            exc.status, exc.code, f"{exc.message} {exc.fix}" if exc.fix else exc.message
        ) from None
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
    _LOGGER.info("campaigns.outcomes_pulled campaign=%s rows=%d", campaign.campaign_id, record.rows)
    return upload.upload_id, upload.file_name, frame


def _cycle_http(exc: CycleError) -> HTTPException:
    """A `CycleError` from the engine half of a campaign route, as the route's own answer."""
    return http_error(exc.status, exc.code, exc.message, path=exc.path)


def _config(campaign: Campaign, root: Any) -> UseCaseConfig | None:
    return use_case_config(campaign.use_case_id, root) if campaign.use_case_id is not None else None


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
    try:
        # Plan J M107 (DEC-1317): the engine half, shared with the monthly loop's measure step.
        measured = measure_recorded(
            storage,
            store,
            campaign,
            as_of=body.as_of or now,
            outcome_window_days=body.outcome_window_days,
            covariate_column=body.covariate_column,
            outcome_kind=body.outcome_kind,
            ledger_engine=lambda: get_platform_engine(request),
            now=utc_now(),
        )
    except CycleError as exc:
        if exc.code in _AUDITED_REFUSALS:
            set_audit_context(request, details={"reason_code": exc.code})
        raise _cycle_http(exc) from exc
    if not measured.matured or measured.campaign is None:
        refusal_response = _not_matured(request, measured.report)
        assert refusal_response is not None  # an unmatured report is always refused
        return refusal_response
    report, updated = measured.report, measured.campaign
    _LOGGER.info(
        "campaigns.measure campaign=%s treated=%d control=%d early_look=%s",
        campaign_id,
        report.treated_rows,
        report.control_rows,
        report.early_look,
    )
    return _view(storage, updated, root)


_AUDITED_REFUSALS: Final[frozenset[str]] = frozenset(
    {CAMPAIGN_EPOCH_MISMATCH, TEST_PLAN_CHANGED, COVARIATE_NOT_BEFORE_CAMPAIGN}
)
"""The measure refusals whose code the request's audit event records (as before Plan J M107)."""

_segment_effects = segment_effects
"""`engine.measurement.cycle.segment_effects`, the audit and programme routes' name for it (Plan J M107)."""


def _not_matured(request: Request, report: IncrementalityReport) -> JSONResponse | None:
    """The 409 `CAMPAIGN_NOT_MATURED` answer when some customer's outcome window is still open, else None."""
    if report.status is not IncrementalityStatus.IMMATURE and not report.rows_immature:
        return None
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


def _epoch_mismatch(
    request: Request, storage: Storage, campaign: Campaign, *, window: int | None, as_of: datetime
) -> str | None:
    """`engine.measurement.cycle.epoch_refusal`, reading the persistent holdout's ledger from the platform database."""
    return epoch_refusal(
        storage, campaign, ledger_engine=lambda: get_platform_engine(request), window=window, as_of=as_of
    )


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
    amount = (
        plan is not None and plan.outcome_kind == "continuous"
    )  # Plan J M102: points in the amount's unit
    rate, source = (base_rate, "request") if base_rate is not None and not amount else (None, None)
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
    if plan is not None and amount:
        if plan.outcome_sd is None:
            return CampaignPlanPreview(
                **answer,
                points=(),
                current_index=None,
                basis=None,
                reason=(
                    "The test plan measures an amount but gives no spread for it, so the smallest change "
                    "the test can see cannot be worked out. Amend the plan with the amount's spread."
                ),
            )
        preview = continuous_power_preview(preview_request, plan.outcome_sd, rho2=plan.expected_rho2 or 0.0)
    else:
        preview = power_preview(preview_request)
    # The explore cost is the campaign's own explore slice, counted, not a share of it rounded back
    # (the request caps the share at 10% of the measured population, which an explore slice drawn
    # outside a narrow selection can exceed).
    explore_cost = cost_of_explore(realised.n_explore, contact_cost, offer_cost).amount
    points = tuple(point.model_copy(update={"cost_of_explore": explore_cost}) for point in preview.points)
    index = next((i for i, point in enumerate(points) if point.holdout_share == current), None)
    return CampaignPlanPreview(**answer, points=points, current_index=index, basis=preview.basis, reason=None)


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
    # Plan J M102 (DEC-1312): an amount (`outcome_kind: continuous`) can be planned and measured now; the
    # refusal M94 put here ("amounts such as revenue come later") is lifted.
    decided = body.model_copy(update={"outcome_window_days": window})
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


# ---------------------------------------------------------------------------
# Plan J M103 (DEC-1313): audit any campaign, the programme readout, who was contacted
# ---------------------------------------------------------------------------
def _check_as_of(as_of: datetime | None, now: datetime) -> None:
    if as_of is not None and as_of > now:
        raise http_error(
            422,
            CAMPAIGN_INVALID,
            "A campaign can only be measured as of now or an earlier moment, not a later one.",
            path="as_of",
        )


def _upload_frame(storage: Storage, upload_id: str) -> tuple[Any, Any]:
    """An upload's record and every row of its file (the one upload path every file here went through)."""
    upload = load_upload(storage, upload_id)
    return upload, _read_all(storage, upload.source_key, upload.file_format)


def _contact_frame(storage: Storage, spec: ContactFile, primary_key: PrimaryKey) -> tuple[Any, Any]:
    """`(upload, contact frame)`: the contact file as stored; a file that cannot be read is 422."""
    upload, frame = _upload_frame(storage, spec.upload_id)
    try:
        renamed = rename_keys(frame, spec.key_columns, primary_key, "contact")
        contacts = normalise_contacts(
            renamed,
            primary_key=primary_key,
            contacted_column=spec.contacted_column,
            contacted_label=spec.contacted_label,
        )
    except (AuditInputError, ContactFileError) as exc:
        raise http_error(422, CONTACT_UNREADABLE, str(exc), path="contact") from exc
    return upload, contacts


_contact_readout = contact_readout
"""`engine.measurement.cycle.contact_readout` (Plan J M107), the name the audit, programme and contact routes use."""


def _contact_source(upload: Any) -> tuple[str | None, bool]:
    """`(upload id, generated?)` of a contact file's upload record."""
    return str(upload.upload_id), bool(upload.synthetic)


def _persist_new(
    request: Request,
    storage: Storage,
    campaign: Campaign,
    *,
    frames: dict[str, Any],
    models: dict[str, Any],
) -> None:
    """Write a new campaign's files and its record together; leave nothing behind when any of it fails.

    The privacy jobs read a campaign's key columns from its record, so a file without a record is never kept.
    """
    written = [campaign_key(campaign.campaign_id, name) for name in (*frames, *models)]
    written.append(campaign_key(campaign.campaign_id, CAMPAIGN_FILENAME))
    try:
        for name, frame in frames.items():
            try:
                write_frame(storage, campaign_key(campaign.campaign_id, name), frame)
            except (ValueError, TypeError) as exc:  # a column pyarrow cannot store as one type
                raise http_error(422, CAMPAIGN_INVALID, "A file mixes kinds of value in one column.") from exc
        for name, model in models.items():
            storage.write_model(campaign_key(campaign.campaign_id, name), model)
        save_campaign(get_campaign_store(request), storage, campaign, create=True)
    except BaseException:
        _discard(storage, *written)
        raise


def _treatment_start(
    entered: datetime | None, sent: Any, outcomes: Any, date_column: str | None
) -> tuple[datetime, Literal["assignment", "outcomes", "entered"]]:
    """When the campaign went out: the entered moment, else the earliest date in the files (422 when neither)."""
    source: Literal["assignment", "outcomes", "entered"] = "entered"
    values = None
    if sent is not None:
        source, values = "assignment", sent
    elif date_column is not None:
        source, values = "outcomes", outcomes[date_column]
    if entered is not None:
        return entered, source
    found = earliest_date(values) if values is not None else None
    if found is None:
        raise http_error(
            422,
            CAMPAIGN_INVALID,
            "Say when the campaign went out (treatment_start), or give a column with the date each customer was "
            "contacted that can be read as a date.",
            path="treatment_start",
        )
    return found, source


def _offer_arguments_for(built: Any) -> dict[str, Any]:
    return {
        "arm_column": "offer",
        "arms": list(built.offers),
        "control_level": built.control_level,
        "combine_offers": True,  # every offer together, for the Value Proof Pack (DEC-1314 (s))
    }


def _audit_notes(
    built: Any, report: IncrementalityReport, body: AuditRequest, *, synthetic: bool
) -> tuple[str, ...]:
    notes: list[str] = []
    if built.rows_without_group:
        notes.append(
            f"{built.rows_without_group:,} customers in the assignment file have no group and were left out of "
            f"both groups."
        )
    if body.assignment.intended_column is None:
        notes.append(
            "Every customer in the assignment file was compared, whether or not they were meant to be reached "
            "(the usual way to read a campaign: the groups as they were chosen)."
        )
    if body.outcome_window_days is None:
        notes.append(
            "No outcome window was given, so every outcome in the file was counted as final. If customers were "
            "still responding, the result is early."
        )
    if report.rows_without_outcome:
        notes.append(
            f"{report.rows_without_outcome:,} customers in the comparison have no outcome in the outcomes file. "
            f"They were left out, not counted as customers who did not respond."
        )
    if body.assignment.sent_date_column is None and body.outcomes.treatment_date_column is None:
        notes.append("Every customer was taken to be contacted on the date the campaign went out.")
    if synthetic:
        notes.append(
            "A file was marked as generated rather than a client's, so this result is a demonstration."
        )
    return tuple(notes)


@router.post(
    "/campaigns/audit",
    response_model=CampaignView,
    status_code=201,
    responses=_MEASURE_ERRORS,
    summary="Audit a campaign another tool ran: who was in which group, and what happened",
)
def audit_campaign(
    body: AuditRequest, request: Request, response: Response, root: ConfigRootDep, storage: StorageDep
) -> CampaignView | JSONResponse:
    """Create an `external` campaign from two uploads and measure it, with the claim its numbers can bear.

    Nothing is stored unless the result is final: outcomes still inside their window answer 409
    `CAMPAIGN_NOT_MATURED` with the day to come back (send the files again then). The campaign has no run
    record: its report's `run_id` is the campaign id.
    """
    from engine.uplift.config import UpliftConfig

    now = utc_now()
    _check_as_of(body.as_of, now)
    a_upload, a_frame = _upload_frame(storage, body.assignment.upload_id)
    o_upload, o_frame = _upload_frame(storage, body.outcomes.upload_id)
    spec, outcome_spec = body.assignment, body.outcomes
    try:
        built = build_assignment_frame(
            a_frame,
            primary_key=body.primary_key,
            arm_column=spec.arm_column,
            file_keys=spec.key_columns,
            control_value=spec.control_value,
            treated_values=spec.treated_values,
            sent_date_column=spec.sent_date_column,
            intended_column=spec.intended_column,
        )
        outcome_column = outcome_spec.outcome_column or detect_outcome_column(
            [str(name) for name in o_frame.columns],
            primary_key=list(outcome_spec.key_columns) if outcome_spec.key_columns else body.primary_key,
            target_column="outcome",
        )
        outcomes, date_column = build_outcomes_frame(
            o_frame,
            primary_key=body.primary_key,
            outcome_column=outcome_column,
            file_keys=outcome_spec.key_columns,
            treatment_date_column=outcome_spec.treatment_date_column,
            assignment=built,
        )
    except AuditInputError as exc:
        raise http_error(422, exc.code, str(exc), path=exc.path) from exc
    except ValueError as exc:
        raise http_error(422, CAMPAIGN_INVALID, str(exc), path="outcomes") from exc
    start, date_source = _treatment_start(body.treatment_start, built.sent, outcomes, date_column)
    assignment = built.assignment
    campaign_id = new_campaign_id(now)
    kind = outcome_spec.outcome_kind
    try:
        report = measure_campaign(
            assignment,
            outcomes,
            run_id=campaign_id,
            primary_key=body.primary_key,
            outcome_column=outcome_column,
            positive_label=outcome_spec.positive_label,
            intended_column=INTENDED_COLUMN,
            treatment_time=start,
            treatment_date_column=date_column,
            outcome_window_days=body.outcome_window_days,
            as_of=body.as_of or now,
            campaign_id=campaign_id,
            outcome_kind=kind,
            **(_offer_arguments_for(built) if built.offers else {}),
        )
    except ValueError as exc:
        raise http_error(422, CAMPAIGN_INVALID, str(exc)) from exc
    refusal = _not_matured(request, report)
    if refusal is not None:
        return refusal
    threshold = UpliftConfig().randomness_auc_max
    check = (
        check_randomness(built, primary_key=body.primary_key, threshold=threshold, seed=AUDIT_SEED)
        if body.assignment_basis == "random"
        else RandomnessCheck(
            status="not_run",
            threshold=threshold,
            reason="You said the customers were not chosen at random, so there was nothing to test.",
        )
    )
    basis, causal, label, explanation = decide_basis(body.assignment_basis, check)
    report = label_report(report, basis)
    counts = assignment_counts(assignment)
    synthetic = bool(a_upload.synthetic or o_upload.synthetic)
    readout = AuditReadout(
        campaign_id=campaign_id,
        stated_basis=body.assignment_basis,
        causal_basis=basis,
        causal=causal,
        label=label,
        explanation=explanation,
        randomness=check,
        assignment_upload_id=a_upload.upload_id,
        assignment_file_name=a_upload.file_name,
        assignment_rows=len(assignment.index),
        rows_without_group=built.rows_without_group,
        offers=built.offers or None,
        control_level=built.control_level,
        outcome_kind=kind,
        outcome_is_good=body.outcome_is_good,
        sent_dates=date_source,
        synthetic=synthetic,
        notes=_audit_notes(built, report, body, synthetic=synthetic),
        audited_at=now,
    )
    campaign = Campaign(
        campaign_id=campaign_id,
        kind=CampaignKind.EXTERNAL,
        name=body.name or f"Audit of {a_upload.file_name}, sent {start.day} {start:%b %Y}",
        use_case_id=None,
        run_ids=(),
        primary_key=body.primary_key,
        treatment_start=start,
        treatment_start_source="entered" if body.treatment_start is not None else "file",
        outcome_window_days=body.outcome_window_days,
        population="intended" if spec.intended_column is not None else "eligible",
        causal=causal,
        causal_basis=basis,
        counts=counts,
        status=CampaignStatus.MEASURED,
        outcomes=CampaignOutcomes(
            upload_id=o_upload.upload_id,
            file_name=o_upload.file_name,
            outcome_column=outcome_column,
            outcome_named=outcome_spec.outcome_column is not None,
            positive_label=outcome_spec.positive_label,
            treatment_date_column=date_column,
            rows=len(outcomes.index),
            added_at=now,
        ),
        created_at=now,
        created_by=requested_by(request) or "local",
        measured_at=report.computed_at,
    )
    frames: dict[str, Any] = {ASSIGNMENT_FILENAME: assignment, OUTCOMES_FILENAME: outcomes}
    models: dict[str, Any] = {REPORT_FILENAME: report, AUDIT_FILENAME: readout}
    assert campaign.outcomes is not None  # set just above
    models[SEGMENT_EFFECTS_FILENAME] = _segment_effects(
        storage,
        assignment,
        outcomes,
        report,
        campaign=campaign,
        outcome=campaign.outcomes,
        control_level=built.control_level,
    )
    if body.contact is not None:
        contact_upload, contacts = _contact_frame(storage, body.contact, body.primary_key)
        frames[CONTACT_FILENAME] = contacts
        models[CONTACT_READOUT_FILENAME] = _contact_readout(
            assignment,
            outcomes,
            contacts,
            campaign,
            report,
            unlisted=body.contact.unlisted_customers,
            offers=built.offers or None,
            now=now,
            source=_contact_source(contact_upload),
        )
    _persist_new(request, storage, campaign, frames=frames, models=models)
    # The audit trail's detail keys are a closed list (DEC-705): the campaign record names both uploads.
    set_audit_context(request, object_id=campaign_id, details={"outcome": basis})
    response.headers["Location"] = f"/campaigns/{campaign_id}"
    _LOGGER.info(
        "campaigns.audit campaign=%s rows=%d treated=%d control=%d basis=%s",
        campaign_id,
        counts.rows,
        report.treated_rows,
        report.control_rows,
        basis,
    )
    return _view(storage, campaign, root)


# --- the whole programme ---------------------------------------------------------------------------
def _period_text(period: ProgrammePeriod) -> str:
    start, end = period.start, period.end
    return f"{start.day} {start:%b %Y} to {end.day} {end:%b %Y}"


SAMPLE_RATIO_ALPHA: Final[float] = 0.001
"""Below this chance the file's share of held-back customers is called far from what the rule gives."""


def _share_p_value(members: int, rows: int, fraction: float) -> float | None:
    """Two-sided chance of a share this far from `fraction` (normal approximation), or None for a tiny file."""
    spread = rows * fraction * (1.0 - fraction)
    if rows <= 0 or spread < 5.0:
        return None
    z = abs(members - rows * fraction) / math.sqrt(spread)
    return math.erfc(z / math.sqrt(2.0))


def _runs_outside_the_universal_holdout(
    storage: Storage, period: ProgrammePeriod
) -> tuple[int, tuple[str, ...]]:
    """`(runs, use cases)`: scoring runs finished inside `period` that did not use the universal control group.

    A run that wrote no `holdout_assignment.json` drew its control group per run. Their lists may have gone to
    customers the universal holdout keeps back, which contaminates the control group of the programme.
    """
    from engine.runs import RUN_FILENAME

    runs, use_cases = 0, set()
    for key in storage.list_keys("runs/"):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        try:
            record = storage.read_model(key, RunRecord)
        except (StorageError, ValueError):
            continue
        if (
            record.mode is not RunMode.SCORE
            or record.state is not RunState.DONE
            or record.finished_at is None
        ):
            continue
        if not period.start <= record.finished_at.astimezone(UTC).date() <= period.end:
            continue
        spec = run_holdout_spec(storage, record.run_id)
        if spec is not None and spec.scope == "universal":
            continue
        runs += 1
        use_cases.add(record.use_case_id or record.run_id)
    return runs, tuple(sorted(use_cases))


def _programme_notes(
    counts: Any,
    fraction: float,
    *,
    outside_runs: int,
    outside_use_cases: tuple[str, ...],
    has_contacts: bool,
) -> tuple[str, ...]:
    if not counts.rows:
        return ("The outcomes file has no customer.",)
    notes: list[str] = []
    p_value = _share_p_value(counts.holdout, counts.rows, fraction)
    if p_value is not None and p_value < SAMPLE_RATIO_ALPHA:
        notes.append(
            f"The share of held-back customers in this file ({counts.holdout / counts.rows:.1%}) is far from what "
            f"the rule gives ({fraction:.0%}). The file may not hold the whole customer base, for example if only "
            f"customers who were active were kept, and then the comparison is not fair. Check the file before "
            f"relying on the result."
        )
    notes.append(
        f"The universal control group keeps {fraction:.0%} of customers out of every list scored with it. By "
        f"chance the share in this file can differ a little: here it is {counts.holdout / counts.rows:.1%}."
    )
    if outside_runs:
        names = ", ".join(outside_use_cases)
        notes.append(
            f"{outside_runs} list{'s' if outside_runs != 1 else ''} made in this period ({names}) did not use the "
            f"universal control group, so some customers it keeps back may have been contacted. This narrows the "
            f"difference between the groups, so the result understates what the programme does. Campaigns sent "
            f"outside this tool are not known either."
        )
    if not has_contacts:
        notes.append(
            "Add a contact file to see whether any of the customers kept back were contacted anyway."
        )
    if counts.holdout < 2 or counts.treated < 2:
        notes.append("One of the two groups has fewer than two customers, so no result can be given.")
    return tuple(notes)


@router.post(
    "/campaigns/programme",
    response_model=CampaignView,
    status_code=201,
    responses=_MEASURE_ERRORS,
    summary="The whole programme: every customer not held back against the universal holdout, over a period",
)
def programme_readout(
    body: ProgrammeRequest,
    request: Request,
    response: Response,
    root: ConfigRootDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> CampaignView | JSONResponse:
    """Intent to treat for everything done over `period`, read from one outcomes file of the whole customer base.

    The universal holdout's members are found by the salted rule every scoring run used, at the fraction the
    ledger recorded for the current epoch. That is only the split of the period when the epoch began before
    it (409 `CAMPAIGN_EPOCH_MISMATCH` otherwise). The period is over by the time the outcomes exist, so no
    plan could have been registered first: a `plan` or an earlier-amount column is refused (409
    `TEST_PLAN_INVALID`) and an amount is read as a plain difference in means.
    """
    now = utc_now()
    _check_as_of(body.as_of, now)
    salt = configured_salt(settings)
    ledger = HoldoutLedger(get_platform_engine(request))
    stored, entry = ledger.fingerprint(), ledger.entry(UNIVERSAL_SCOPE_KEY, UNIVERSAL_SCOPE_KEY)
    if salt is None or stored is None or entry is None:
        raise http_error(
            409,
            PROGRAMME_NO_HOLDOUT,
            "The programme is read against the universal holdout, and none has been used yet: set "
            "actions.holdout.scope to universal on the use cases, with the holdout secret set, and score once.",
        )
    if stored != salt_fingerprint(salt):
        raise http_error(
            409,
            HOLDOUT_SALT_CHANGED,
            "The holdout secret of this deployment is not the one the universal holdout was drawn with, so the "
            "customers cannot be split as the lists were.",
        )
    outcome_spec = body.outcome
    window = period_window_days(body.period)
    start = datetime(body.period.start.year, body.period.start.month, body.period.start.day, tzinfo=UTC)
    began = entry.started_at if entry.started_at.tzinfo is not None else entry.started_at.replace(tzinfo=UTC)
    if began > start:
        set_audit_context(request, details={"reason_code": CAMPAIGN_EPOCH_MISMATCH})
        raise http_error(
            409,
            CAMPAIGN_EPOCH_MISMATCH,
            f"The universal control group was started or redrawn on {began.date().isoformat()}, after the period "
            f"began on {body.period.start.isoformat()}, so the customers held back during the period cannot be "
            f"found. Read a period that begins on or after that day.",
        )
    if body.plan is not None or outcome_spec.covariate_column is not None:
        set_audit_context(request, details={"reason_code": TEST_PLAN_INVALID})
        raise http_error(
            409,
            TEST_PLAN_INVALID,
            "A programme is read after its period has ended, so a plan or an earlier-amount column sent with "
            "the outcomes could not have been fixed before they were seen. Send the request without them: the "
            "difference between the groups is given as it is.",
            path="plan" if body.plan is not None else "outcome.covariate_column",
        )
    upload, frame = _upload_frame(storage, outcome_spec.upload_id)
    try:
        outcome_column = outcome_spec.outcome_column or detect_outcome_column(
            [str(name) for name in frame.columns],
            primary_key=list(outcome_spec.key_columns) if outcome_spec.key_columns else body.primary_key,
            target_column="outcome",
        )
        outcomes, _ = build_outcomes_frame(
            frame,
            primary_key=body.primary_key,
            outcome_column=outcome_column,
            file_keys=outcome_spec.key_columns,
        )
    except AuditInputError as exc:
        raise http_error(422, exc.code, str(exc), path=exc.path) from exc
    except ValueError as exc:
        raise http_error(422, CAMPAIGN_INVALID, str(exc), path="outcome") from exc
    assignment = programme_assignment(
        outcomes, primary_key=body.primary_key, salt=salt, fraction=entry.fraction
    )
    counts = assignment_counts(assignment)
    campaign_id = new_campaign_id(now)
    campaign = Campaign(
        campaign_id=campaign_id,
        kind=CampaignKind.PROGRAMME,
        name=body.name or f"Whole programme, {_period_text(body.period)}",
        use_case_id=None,
        run_ids=(),
        primary_key=body.primary_key,
        treatment_start=start,
        treatment_start_source="entered",
        outcome_window_days=window,
        population="eligible",
        causal=True,
        causal_basis="engine_random",
        holdout_scope="universal",
        holdout_scope_key=UNIVERSAL_SCOPE_KEY,
        holdout_epoch=entry.epoch,
        counts=counts,
        status=CampaignStatus.MEASURED,
        outcomes=CampaignOutcomes(
            upload_id=upload.upload_id,
            file_name=upload.file_name,
            outcome_column=outcome_column,
            outcome_named=outcome_spec.outcome_column is not None,
            positive_label=outcome_spec.positive_label,
            rows=len(outcomes.index),
            added_at=now,
        ),
        created_at=now,
        created_by=requested_by(request) or "local",
    )
    as_of = body.as_of or now
    mismatch = _epoch_mismatch(request, storage, campaign, window=window, as_of=as_of)
    if mismatch is not None:
        set_audit_context(request, details={"reason_code": CAMPAIGN_EPOCH_MISMATCH})
        raise http_error(409, CAMPAIGN_EPOCH_MISMATCH, mismatch)
    kind = outcome_spec.outcome_kind or "binary"
    try:
        report = measure_campaign(
            assignment,
            outcomes,
            run_id=campaign_id,
            primary_key=body.primary_key,
            outcome_column=outcome_column,
            positive_label=outcome_spec.positive_label,
            intended_column=INTENDED_COLUMN,
            treatment_time=start,
            treatment_date_column=None,
            outcome_window_days=window,
            as_of=as_of,
            campaign_id=campaign_id,
            outcome_kind=kind,
        )
    except ValueError as exc:
        raise http_error(422, CAMPAIGN_INVALID, str(exc)) from exc
    refusal = _not_matured(request, report)
    if refusal is not None:
        return refusal
    outside_runs, outside_use_cases = _runs_outside_the_universal_holdout(storage, body.period)
    readout = ProgrammeReadout(
        campaign_id=campaign_id,
        period_start=body.period.start,
        period_end=body.period.end,
        holdout_epoch=entry.epoch,
        holdout_fraction=entry.fraction,
        salt_id=salt_id(stored),
        customers=counts.rows,
        holdout_members=counts.holdout,
        other_customers=counts.treated,
        realised_share=counts.holdout / counts.rows if counts.rows else None,
        outcome_kind=kind,
        outcome_is_good=body.outcome_is_good,
        label=PROGRAMME_LABEL,
        explanation=(
            "Everyone who was not held back is compared with the customers the universal control group kept out of "
            "every list scored with it. That group is chosen at random from the customer id, so the comparison is "
            "fair. Not everyone outside it was contacted, so this is the effect of running the whole programme, "
            "not of one message on the customers who received it."
        ),
        synthetic=bool(upload.synthetic),
        notes=_programme_notes(
            counts,
            entry.fraction,
            outside_runs=outside_runs,
            outside_use_cases=outside_use_cases,
            has_contacts=body.contact is not None,
        ),
        computed_at=now,
    )
    updated = campaign.model_copy(
        update={
            "status": CampaignStatus.LIVE if report.early_look else CampaignStatus.MEASURED,
            "measured_at": report.computed_at,
        }
    )
    frames: dict[str, Any] = {ASSIGNMENT_FILENAME: assignment, OUTCOMES_FILENAME: outcomes}
    models: dict[str, Any] = {REPORT_FILENAME: report, PROGRAMME_FILENAME: readout}
    assert updated.outcomes is not None  # set when the record was made above
    models[SEGMENT_EFFECTS_FILENAME] = _segment_effects(
        storage, assignment, outcomes, report, campaign=updated, outcome=updated.outcomes
    )
    if body.contact is not None:
        contact_upload, contacts = _contact_frame(storage, body.contact, body.primary_key)
        frames[CONTACT_FILENAME] = contacts
        models[CONTACT_READOUT_FILENAME] = _contact_readout(
            assignment,
            outcomes,
            contacts,
            updated,
            report,
            unlisted=body.contact.unlisted_customers,
            offers=None,
            now=now,
            source=_contact_source(contact_upload),
        )
    _persist_new(request, storage, updated, frames=frames, models=models)
    # The audit trail's detail keys are a closed list (DEC-705): the record names the upload and the epoch.
    set_audit_context(request, object_id=campaign_id, details={"outcome": f"universal_epoch_{entry.epoch}"})
    response.headers["Location"] = f"/campaigns/{campaign_id}"
    _LOGGER.info(
        "campaigns.programme campaign=%s customers=%d holdout=%d epoch=%d",
        campaign_id,
        counts.rows,
        counts.holdout,
        entry.epoch,
    )
    return _view(storage, updated, root)


# --- who was actually contacted --------------------------------------------------------------------
@router.post(
    "/campaigns/{campaign_id}/contacts",
    response_model=CampaignView,
    responses=_ERRORS,
    summary="Say who a campaign actually contacted: the contact rate, contamination and the effect on the contacted",
)
def add_campaign_contacts(
    campaign_id: str, body: ContactFile, request: Request, root: ConfigRootDep, storage: StorageDep
) -> CampaignView:
    """Store a contact file and recompute the readout; the effect on the contacted needs a measured campaign."""
    store = get_campaign_store(request)
    campaign = _load(store, campaign_id)
    contact_upload, contacts = _contact_frame(storage, body, campaign.primary_key)
    report = _stored(storage, campaign_key(campaign_id, REPORT_FILENAME), IncrementalityReport)
    audit = _stored(storage, campaign_key(campaign_id, AUDIT_FILENAME), AuditReadout)
    assignment = read_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
    outcomes = (
        read_frame(storage, campaign_key(campaign_id, OUTCOMES_FILENAME))
        if campaign.outcomes is not None
        else None
    )
    try:
        readout = _contact_readout(
            assignment,
            outcomes,
            contacts,
            campaign,
            report,
            unlisted=body.unlisted_customers,
            offers=audit.offers if audit is not None else None,
            now=utc_now(),
            source=_contact_source(contact_upload),
        )
        write_frame(storage, campaign_key(campaign_id, CONTACT_FILENAME), contacts)
    except (ValueError, TypeError) as exc:
        raise http_error(422, CONTACT_UNREADABLE, str(exc), path="contact") from exc
    storage.write_model(campaign_key(campaign_id, CONTACT_READOUT_FILENAME), readout)
    _LOGGER.info(
        "campaigns.contacts campaign=%s listed=%d treated_listed=%d holdout_listed=%d",
        campaign_id,
        readout.contact_rows,
        readout.treated_listed,
        readout.holdout_listed,
    )
    return _view(storage, campaign, root)
