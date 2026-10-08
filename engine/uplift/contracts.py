"""Artefact contracts of the uplift problem type (plan B §7).

Every JSON an uplift run writes is one of these models, and the UI renders from them and nothing
else (plan §2.1 principle 2). They extend `engine.contracts.Artefact`, so they are frozen, refuse
unknown keys and carry a `schema_version` like every Phase 1 artefact.

They live here rather than in `engine/contracts.py` because that file's artefact registry is an
immutable mapping built above the shared-file blocks (PARALLEL_WORK_PROTOCOL.md §4). Uplift keeps a
registry of its own, :data:`UPLIFT_ARTEFACTS`, and `api/routes/uplift.py` serves from it (DEC-602).

Uplift validation findings have the same fields as `engine.contracts.ValidationCheck` except the
onboarding-only `source_id`. Their codes live in the platform's one code registry
(`engine.contracts.CHECK_CODE_TABLES`, DEC-950): `UPLIFT_VALIDATION_CODES` is defined there and
re-exported here, `ValidationCheck` accepts it like every other table, and `UpliftCheck` accepts only
the codes of its own table.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from engine.config import Metric
from engine.contracts import (
    UPLIFT_VALIDATION_CODES,
    Artefact,
    DriftReport,
    Severity,
    check_code_table,
)
from engine.uplift.config import UpliftBaseModel, UpliftLearner

__all__ = [
    "INCREMENTALITY_FILENAME",
    "OPE_FILENAME",
    "POLICY_FILENAME",
    "QINI_CURVE_FILENAME",
    "RANKING_CHOICE_FILENAME",
    "RISK_COMPARISON_FILENAME",
    "SEGMENTS_FILENAME",
    "UPLIFT_ARTEFACTS",
    "UPLIFT_DRIFT_FILENAME",
    "UPLIFT_EVALUATION_FILENAME",
    "UPLIFT_MODEL_CARD_FILENAME",
    "UPLIFT_NOT_BETTER_THAN_RISK",
    "UPLIFT_VALIDATION_CODES",
    "UPLIFT_VALIDATION_FILENAME",
    "BaselineAuuc",
    "BaselineComparison",
    "BaselineKind",
    "CalibrationDecile",
    "ConfidenceValue",
    "FoldAuuc",
    "FoldAuucValue",
    "IncrementalityReport",
    "IncrementalityStatus",
    "OpeEstimate",
    "OpeReport",
    "PolicyComparisonValue",
    "PolicyRecommendation",
    "PolicyStopReason",
    "ProfitCurve",
    "ProfitPoint",
    "QiniCurve",
    "QiniPoint",
    "RankingChoice",
    "RiskComparison",
    "Segment",
    "SegmentReport",
    "SegmentSummary",
    "SegmentThresholds",
    "TreatmentShareDrift",
    "UpliftAtK",
    "UpliftCalibration",
    "UpliftCheck",
    "UpliftDecile",
    "UpliftDriftReport",
    "UpliftEvaluation",
    "UpliftModelCard",
    "UpliftValidationReport",
]

UPLIFT_VALIDATION_FILENAME: Final[str] = "uplift_validation.json"
UPLIFT_EVALUATION_FILENAME: Final[str] = "uplift_evaluation.json"
QINI_CURVE_FILENAME: Final[str] = "qini_curve.json"
SEGMENTS_FILENAME: Final[str] = "segments.json"
POLICY_FILENAME: Final[str] = "policy_recommendation.json"
INCREMENTALITY_FILENAME: Final[str] = "incrementality_report.json"
OPE_FILENAME: Final[str] = "ope_report.json"
UPLIFT_DRIFT_FILENAME: Final[str] = "uplift_drift.json"
"""Written by a scoring run of an uplift model: feature PSI and the treated-share check (M53)."""
UPLIFT_MODEL_CARD_FILENAME: Final[str] = "uplift_model.json"
"""Written inside the run's `model/` directory, beside the pickled learner."""

NOT_CAUSAL_NOTE: Final[str] = (
    "Not causal: the treatment was not randomly assigned, so these numbers describe who was "
    "contacted, not what contacting them changed."
)
"""The label every output carries once `TREATMENT_NOT_RANDOM` was acknowledged (plan B §4)."""


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------
class ConfidenceValue(Artefact):
    """A point estimate with its two-sided confidence interval.

    `ci_low`/`ci_high` are null only when an interval cannot be computed (for example a single
    bootstrap resample had no control rows); the page then shows "—", never a made-up band.
    """

    value: float = Field(description="Point estimate.")
    ci_low: float | None = Field(default=None, description="Lower bound of the confidence interval.")
    ci_high: float | None = Field(default=None, description="Upper bound of the confidence interval.")
    confidence_level: float = Field(default=0.95, description="Coverage of the interval, e.g. 0.95.")

    @property
    def excludes_zero(self) -> bool:
        """True when the whole interval lies strictly on one side of zero."""
        if self.ci_low is None or self.ci_high is None:
            return False
        return self.ci_low > 0.0 or self.ci_high < 0.0


class Segment(StrEnum):
    """The four-segment view (plan B §1.3)."""

    PERSUADABLE = "persuadable"
    SURE_THING = "sure_thing"
    LOST_CAUSE = "lost_cause"
    SLEEPING_DOG = "sleeping_dog"


SEGMENT_LABELS: Final[Mapping[Segment, str]] = MappingProxyType(
    {
        Segment.PERSUADABLE: "Persuadables",
        Segment.SURE_THING: "Sure things",
        Segment.LOST_CAUSE: "Lost causes",
        Segment.SLEEPING_DOG: "Sleeping dogs",
    }
)

SEGMENT_ACTIONS: Final[Mapping[Segment, str]] = MappingProxyType(
    {
        Segment.PERSUADABLE: "Treat",
        Segment.SURE_THING: "Don't treat (converts anyway)",
        Segment.LOST_CAUSE: "Don't treat (won't convert)",
        Segment.SLEEPING_DOG: "Never treat (contact makes it worse)",
    }
)
"""The recommended action per segment. Only persuadables inside the budget are ever `Treat`."""


# ---------------------------------------------------------------------------
# uplift_validation.json
# ---------------------------------------------------------------------------
# `UPLIFT_VALIDATION_CODES` (Plan B §4's six codes plus M53's `TREATMENT_VARIES_WITHIN_ENTITY`) is one
# table of the platform's code registry in `engine.contracts` (DEC-950), imported above.


class UpliftCheck(Artefact):
    """One uplift validation finding; the field contract of `engine.contracts.ValidationCheck`."""

    code: str = Field(description="Uplift validation code, for example TREATMENT_NOT_RANDOM.")
    severity: Severity = Field(description="Whether this check blocks the run or is only reported.")
    message: str = Field(description="Business-language message, with the numbers already filled in.")
    suggestion: str = Field(default="", description="What the user can do about it.")
    column: str | None = Field(default=None, description="Column the check is about, when it is about one.")
    details: dict[str, Any] = Field(
        default_factory=dict, description="Machine-readable numbers behind the message."
    )
    acknowledgeable: bool = Field(
        default=False, description="Whether the UI may offer to acknowledge this check."
    )
    acknowledged: bool = Field(
        default=False, description="Whether the user acknowledged it through the run overrides."
    )

    @model_validator(mode="after")
    def _known_code(self) -> UpliftCheck:
        if check_code_table(self.code) != "uplift":
            known = ", ".join(sorted(UPLIFT_VALIDATION_CODES))
            raise ValueError(f"unknown uplift validation code {self.code!r}; known codes: {known}")
        return self


class UpliftValidationReport(Artefact):
    """`uplift_validation.json` - the six plan B §4 checks, errors first.

    Phase 1's `validation.json` is written too, by Phase 1's own checks; a run needs both to pass.
    """

    run_id: str | None = Field(default=None, description="Run id, or null when an upload is checked alone.")
    upload_id: str = Field(description="Upload or dataset the checks ran on.")
    treatment_column: str | None = Field(description="Treatment column used, configured or detected.")
    checks: tuple[UpliftCheck, ...] = Field(description="Every finding, errors first.")
    passed: bool = Field(description="True when no error-severity finding is left unacknowledged.")
    causal: bool = Field(
        description=(
            "False once TREATMENT_NOT_RANDOM was acknowledged: every uplift output of the run is "
            "then labelled not causal."
        )
    )
    randomness_auc: float | None = Field(
        default=None,
        description="Cross-validated AUC of a classifier predicting treatment from the features.",
    )
    rows_checked: int = Field(description="Rows the checks ran on.")
    rows_immature: int = Field(
        default=0, description="Rows whose outcome window had not elapsed; dropped before training."
    )
    checked_at: AwareDatetime = Field(description="UTC time the checks ran.")
    treated_rows: int | None = Field(
        default=None,
        description=(
            "Treated rows the arm checks counted (after immature rows were dropped); null when the "
            "treatment column is missing or not 0/1, or in a report written before M53."
        ),
    )
    control_rows: int | None = Field(
        default=None, description="Control rows the arm checks counted; null exactly when treated_rows is."
    )
    entity_column: str | None = Field(
        default=None,
        description=(
            "Entity column of a two-column key (customer + snapshot date): treatment is assigned per "
            "entity and the arms are counted in entities. Null for a one-column key."
        ),
    )
    treated_entities: int | None = Field(
        default=None, description="Distinct treated entities; set only with a two-column key."
    )
    control_entities: int | None = Field(
        default=None, description="Distinct control entities; set only with a two-column key."
    )


# ---------------------------------------------------------------------------
# uplift_evaluation.json, qini_curve.json
# ---------------------------------------------------------------------------
class UpliftAtK(Artefact):
    """Observed uplift among the top `fraction` of customers ranked by predicted uplift."""

    fraction: float = Field(description="Share of the population targeted, e.g. 0.1 for the top 10%.")
    uplift: ConfidenceValue = Field(
        description="Treated conversion rate minus control conversion rate inside that share."
    )


class UpliftDecile(Artefact):
    """One tenth of the hold-out, ranked by predicted uplift, highest first."""

    decile: int = Field(description="1 is the tenth with the highest predicted uplift.")
    rows: int = Field(description="Rows in the decile.")
    treated_rows: int = Field(description="Treated rows in the decile.")
    control_rows: int = Field(description="Control rows in the decile.")
    treated_rate: float | None = Field(description="Outcome rate among treated rows; null with none.")
    control_rate: float | None = Field(description="Outcome rate among control rows; null with none.")
    observed_uplift: float | None = Field(
        description="treated_rate minus control_rate; null when either arm is empty."
    )
    predicted_uplift: float = Field(description="Mean predicted uplift of the decile's rows.")


# ---------------------------------------------------------------------------
# Plan J M96: does uplift earn its place? (typed here, beside the evaluation they extend; computed by
# `engine.uplift.metrics`, `engine.measurement.compare` and the train flow, read by `engine.model_gates`)
# ---------------------------------------------------------------------------
BaselineKind = Literal["p_control", "p_treated", "propensity_model"]
"""The plain rankings an uplift model is compared with on its own hold-out (M96)."""


class BaselineAuuc(Artefact):
    """One plain ranking of the uplift hold-out, scored with the same AUUC as the model, and the gap."""

    baseline: BaselineKind = Field(
        description=(
            "p_control: the uplift model's own chance of the outcome without contact (plain risk); "
            "p_treated: its chance with contact; propensity_model: the use case's last approved "
            "propensity model."
        )
    )
    label: str = Field(description="The ranking in words, for the Approver's screen.")
    available: bool = Field(description="False when this ranking could not be computed; `reason` says why.")
    reason: str | None = Field(
        default=None, description="Why the ranking is missing; null when it is present."
    )
    model_id: str | None = Field(
        default=None, description="The propensity model's version id (propensity_model only)."
    )
    auuc: ConfidenceValue | None = Field(
        default=None, description="AUUC of ranking the hold-out by this score, highest first."
    )
    difference: ConfidenceValue | None = Field(
        default=None,
        description=(
            "Uplift model's AUUC minus this ranking's, with a PAIRED bootstrap interval: every "
            "resample draws the same customers for both rankings."
        ),
    )
    uplift_better: bool | None = Field(
        default=None, description="True when the difference's lower bound is above zero; null when missing."
    )


class BaselineComparison(Artefact):
    """Whether ranking by predicted uplift beats ranking by plain risk on the same hold-out (M96)."""

    uplift_auuc: ConfidenceValue = Field(description="The uplift model's AUUC, as in `auuc`.")
    baselines: tuple[BaselineAuuc, ...] = Field(
        description="p_control, p_treated, propensity_model, in order."
    )
    risk_baseline: BaselineKind = Field(
        description=(
            "The ranking the beats-risk check is decided against: the propensity model when it could "
            "be scored, otherwise p_control."
        )
    )
    beats_risk: bool = Field(
        description="True only when the paired difference against `risk_baseline` has its lower bound above 0."
    )
    bootstrap_samples: int = Field(description="Resamples behind every interval (the evaluation's own).")
    summary: str = Field(description="One plain sentence for the Approver's screen.")


class CalibrationDecile(Artefact):
    """Predicted against observed uplift in one tenth of the hold-out, highest predicted first."""

    decile: int = Field(description="1 is the tenth with the highest predicted uplift.")
    rows: int = Field(description="Hold-out rows in the decile.")
    predicted_uplift: float = Field(description="Mean predicted uplift of the decile's rows.")
    observed_uplift: ConfidenceValue | None = Field(
        description="Treated rate minus control rate in the decile, with its bootstrap interval; null when an arm is empty."
    )
    within_interval: bool | None = Field(
        description="True when the predicted uplift lies inside the observed interval; null without one."
    )


class UpliftCalibration(Artefact):
    """`calibration_by_decile`: does the model's predicted uplift match what the hold-out measured?"""

    deciles: tuple[CalibrationDecile, ...] = Field(description="Up to ten rows, highest predicted first.")
    weighted_abs_gap: float | None = Field(
        description=(
            "Row-weighted mean of |observed - predicted| over the deciles with an observed uplift; "
            "null when none has one."
        )
    )
    deciles_with_interval: int = Field(description="Deciles whose observed uplift has an interval.")
    deciles_covered: int = Field(description="Of those, how many contain the predicted uplift.")
    well_calibrated: bool | None = Field(
        description=(
            "True when at least 80% of the deciles with an interval contain the prediction; null when "
            "fewer than five deciles have one."
        )
    )
    summary: str = Field(description="One plain sentence for the Approver's screen.")


class FoldAuucValue(Artefact):
    """One fold of the cross-fit: the AUUC of a model refitted without these rows, on these rows."""

    fold: int = Field(description="Fold number, from 1.")
    rows: int = Field(description="Rows in the fold.")
    auuc: float | None = Field(description="AUUC on the fold; null when the fold lacks an arm.")
    interval: ConfidenceValue | None = Field(
        default=None,
        description=(
            "The fold AUUC with its 95% bootstrap interval (resampled within arms on the fold's rows); "
            "null when the fold lacks an arm."
        ),
    )


class FoldAuuc(Artefact):
    """`fold_auuc`: how much the model's AUUC moves when it is refitted on other rows (M96).

    Off by default (`uplift.evidence.fold_auuc`), and only for the LightGBM base model: each fold
    refits the meta-learner. When it was not computed, `computed` is false, `reason` says why and
    `estimated_refit_seconds` says what turning it on would cost on this data.
    """

    computed: bool = Field(description="False when the folds were not refitted; `reason` says why.")
    reason: str | None = Field(default=None, description="Why it was not computed; null when it was.")
    folds: int = Field(description="Folds configured.")
    values: tuple[FoldAuucValue, ...] = Field(default=(), description="One per fold, when computed.")
    mean: float | None = Field(default=None, description="Mean AUUC over the folds with one.")
    sd: float | None = Field(default=None, description="Standard deviation of those AUUCs (ddof 1).")
    minimum: float | None = Field(default=None, description="Lowest fold AUUC.")
    stable: bool | None = Field(
        default=None,
        description=(
            "True when every fold was measured, no fold's 95% interval lies wholly at or below zero, "
            "and the fold AUUCs differ no more than their bootstrap standard errors explain "
            "(`heterogeneity` at or below `heterogeneity_critical`); null when not computed."
        ),
    )
    heterogeneity: float | None = Field(
        default=None,
        description=(
            "Cochran's Q of the fold AUUCs, weights 1/se² from each fold's bootstrap; null when not "
            "computed or a fold's standard error is zero."
        ),
    )
    heterogeneity_critical: float | None = Field(
        default=None,
        description="The chi-square 95th percentile on (measured folds - 1) degrees of freedom Q is held to.",
    )
    bootstrap_samples: int | None = Field(
        default=None, description="Resamples behind each fold's interval; null when not computed."
    )
    estimated_refit_seconds: float | None = Field(
        default=None,
        description="What refitting every fold costs, estimated from this run's own fit time before it runs.",
    )
    refit_seconds: float | None = Field(default=None, description="What the refits took; null when not run.")
    summary: str = Field(description="One plain sentence for the Approver's screen.")


class UpliftEvaluation(Artefact):
    """`uplift_evaluation.json` - how well the model ranks customers by what the action changes.

    Every number is measured on the hold-out split, which the learners never saw. Intervals are
    percentile bootstrap intervals over `bootstrap_samples` resamples of the hold-out rows, drawn
    within each arm; the model is not refitted per resample (DEC-605).
    """

    run_id: str = Field(description="Run the evaluation belongs to.")
    learner: UpliftLearner = Field(description="Meta-learner that produced the uplift estimates.")
    base_model: UpliftBaseModel = Field(description="Model family inside the meta-learner.")
    primary_metric: Metric = Field(default=Metric.AUUC, description="Metric the champion rule uses.")
    rows_evaluated: int = Field(description="Hold-out rows the metrics were measured on.")
    treated_rows: int = Field(description="Treated rows in the hold-out.")
    control_rows: int = Field(description="Control rows in the hold-out.")
    treated_rate: float = Field(description="Outcome rate among treated hold-out rows.")
    control_rate: float = Field(description="Outcome rate among control hold-out rows.")
    average_treatment_effect: ConfidenceValue = Field(
        description="treated_rate minus control_rate over the whole hold-out: what treating everyone gains."
    )
    auuc: ConfidenceValue = Field(
        description=(
            "Area between the model's uplift curve and the random-targeting line, over population "
            "share 0..1. Zero is no better than random; the champion rule needs its lower bound > 0."
        )
    )
    qini_coefficient: ConfidenceValue = Field(
        description="Area between the Qini curve and the random line, per customer in the hold-out."
    )
    uplift_at: tuple[UpliftAtK, ...] = Field(description="Observed uplift in the top 10%, 20% and 30%.")
    deciles: tuple[UpliftDecile, ...] = Field(
        description=(
            "Ten rows, highest predicted uplift first; fewer only on a hold-out of under ten rows, "
            "where the empty groups are left out."
        )
    )
    bootstrap_samples: int = Field(description="Resamples behind every interval.")
    measurable_uplift: bool = Field(description="True when the AUUC interval lies entirely above zero.")
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")
    summary: str = Field(description="One plain-language sentence for the Model page.")
    evaluated_at: AwareDatetime = Field(description="UTC time of the evaluation.")
    holdout_fingerprint: str | None = Field(
        default=None,
        description=(
            "sha256 of the hold-out's sorted primary keys, so two evaluations can be shown to be on "
            "the same customers; null when the caller had no keys (DEC-670)."
        ),
    )
    # Plan J M96 (pre-approved, additive): the beats-risk, calibration and fold-stability evidence. Each is
    # null on an evaluation written before M96, which every reader treats as "not checked".
    baseline_comparison: BaselineComparison | None = Field(
        default=None, description="AUUC of plain risk rankings on the same hold-out, and the paired gap."
    )
    calibration_by_decile: UpliftCalibration | None = Field(
        default=None, description="Predicted against observed uplift per decile, with intervals."
    )
    fold_auuc: FoldAuuc | None = Field(
        default=None, description="AUUC across refitted folds (off by default; LightGBM only)."
    )


class QiniPoint(Artefact):
    """One point of the Qini chart."""

    fraction: float = Field(description="Share of the hold-out targeted, highest predicted uplift first.")
    qini: float = Field(
        description=(
            "Incremental conversions per hold-out customer when targeting that share: treated "
            "conversions minus control conversions scaled to the treated count, divided by n."
        )
    )
    random: float = Field(description="The same quantity for random targeting: fraction × qini at 1.0.")
    uplift_curve: float = Field(
        description="(treated rate − control rate) inside the share, times the share."
    )


class QiniCurve(Artefact):
    """`qini_curve.json` - the points of the Model page chart, with the random line."""

    run_id: str = Field(description="Run the curve belongs to.")
    rows_evaluated: int = Field(description="Hold-out rows the curve is drawn from.")
    points: tuple[QiniPoint, ...] = Field(description="Points from fraction 0 to 1, ascending.")
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")


# ---------------------------------------------------------------------------
# segments.json, policy_recommendation.json
# ---------------------------------------------------------------------------
class SegmentThresholds(Artefact):
    """The cuts the segments were assigned with, as actually applied."""

    persuadable_min_uplift: float = Field(description="Predicted uplift at or above this is persuadable.")
    sleeping_dog_max_uplift: float = Field(description="Predicted uplift at or below this is a sleeping dog.")
    sure_thing_min_probability: float = Field(
        description=(
            "Between the two cuts, P(outcome | not treated) at or above this is a sure thing, below "
            "it a lost cause. Taken from the configuration, or the training base rate when unset."
        )
    )
    sure_thing_from_base_rate: bool = Field(
        description="True when sure_thing_min_probability was the training base rate, not configured."
    )


class SegmentSummary(Artefact):
    """One segment of the four-segment chart."""

    segment: Segment = Field(description="Segment id.")
    label: str = Field(description="Display label.")
    rows: int = Field(description="Customers in the segment.")
    share_pct: float = Field(description="Share of all customers, as a percentage.")
    mean_predicted_uplift: float | None = Field(description="Mean predicted uplift; null when empty.")
    mean_p_treated: float | None = Field(description="Mean P(outcome | treated); null when empty.")
    mean_p_control: float | None = Field(description="Mean P(outcome | not treated); null when empty.")
    action: str = Field(description="Recommended action for the segment.")


class SegmentReport(Artefact):
    """`segments.json` - counts and average predicted uplift per segment."""

    run_id: str = Field(description="Run the segments belong to.")
    computed_on: Literal["test", "scored"] = Field(
        description="Hold-out split of a training run, or every row of a scoring run."
    )
    rows: int = Field(description="Customers segmented.")
    thresholds: SegmentThresholds = Field(description="The cuts that were applied.")
    segments: tuple[SegmentSummary, ...] = Field(
        description="Persuadables, sure things, lost causes, sleeping dogs, in that order."
    )
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")


class PolicyStopReason(StrEnum):
    ALL_PERSUADABLES = "all_persuadables"
    BUDGET = "budget"
    VALUE_BELOW_COST = "value_below_cost"
    NO_PERSUADABLES = "no_persuadables"


class PolicyRecommendation(Artefact):
    """`policy_recommendation.json` - "treat the top N by uplift within budget" (plan B §1.4)."""

    run_id: str = Field(description="Run the recommendation belongs to.")
    computed_on: Literal["test", "scored"] = Field(
        description="Hold-out split of a training run, or every row of a scoring run."
    )
    rows: int = Field(description="Customers considered.")
    eligible_persuadables: int = Field(description="Persuadables the policy could choose from.")
    contacts_recommended: int = Field(description="N: how many to treat, highest predicted uplift first.")
    stop_reason: PolicyStopReason = Field(description="Why N is not larger.")
    budget_contacts: int | None = Field(description="Configured contact budget, if any.")
    predicted_incremental_conversions: float = Field(
        description="Sum of the model's predicted uplift over the N chosen customers."
    )
    expected_incremental_conversions: ConfidenceValue | None = Field(
        description=(
            "N × the uplift OBSERVED on the hold-out among the same top share, with its bootstrap "
            "interval; null when the run has no measured hold-out to take it from."
        )
    )
    cost_per_contact: float | None = Field(description="Configured cost of one contact.")
    value_per_conversion: float | None = Field(description="Configured value of one conversion.")
    expected_cost: float | None = Field(
        description=(
            "N × cost_per_contact, when a cost is configured; on a list ranked by value (Plan J M97) the "
            "sum of the chosen customers' contact and offer costs."
        )
    )
    expected_value: float | None = Field(
        description=(
            "Expected incremental conversions × value_per_conversion (× margin × horizon when set); on a "
            "list ranked by value, N × the hold-out's value-weighted observed uplift × margin × horizon."
        )
    )
    expected_net_value: float | None = Field(description="expected_value − expected_cost, when both exist.")
    net_value_low: float | None = Field(
        default=None,
        description="expected_net_value at the low end of the conversions interval; null without one.",
    )
    net_value_high: float | None = Field(
        default=None,
        description="expected_net_value at the high end of the conversions interval; null without one.",
    )
    money_note: str | None = Field(
        default=None,
        description=(
            "Plan J M97 (DEC-1307): why a money field is null, or what the money leaves out (customers "
            "without a value), in plain words; null when there is nothing to say."
        ),
    )
    values_missing: int | None = Field(
        default=None,
        description=(
            "Customers ranked by value (`uplift.policy.value_column`) that have no value: counted at zero "
            "value, never given one. Null when the list is not ranked by value."
        ),
    )
    contact_cost: float | None = Field(
        default=None,
        description=(
            "Plan J M97: the cost of one contact a list ranked by value used, in rupees - cost_per_contact, "
            "else the contact_cost of configs/pilot/value.yaml when the run was made. Recorded so the "
            "budget curve replays the run's own costs; null when the list is not ranked by value."
        ),
    )
    offer_cost: float | None = Field(
        default=None,
        description=(
            "Plan J M97: the offer cost a list ranked by value used (charged × p_treated per customer), "
            "from configs/pilot/value.yaml when the run was made; null when the list is not ranked by value."
        ),
    )
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")


# ---------------------------------------------------------------------------
# The budget curve (`GET /runs/{run_id}/uplift/profit-curve`; computed on request, never stored)
# ---------------------------------------------------------------------------
class ProfitPoint(Artefact):
    """The targeting recommendation had the budget been `contacts`: every money field as in
    `PolicyRecommendation`, with the same rules and the same nulls."""

    contacts: int = Field(description="Customers contacted: the top `contacts` eligible persuadables.")
    ranking_depth: int = Field(
        description="Position of the last one contacted in the ranking of every row (0 when none)."
    )
    predicted_incremental_conversions: float = Field(
        description="Sum of the model's predicted uplift over the customers contacted."
    )
    expected_incremental_conversions: ConfidenceValue | None = Field(
        description=(
            "contacts × the uplift observed on the hold-out among the top ranking_depth/rows share, "
            "with its bootstrap interval scaled the same way; null when the hold-out cannot measure it."
        )
    )
    expected_cost: float | None = Field(description="contacts × cost_per_contact, when a cost is set.")
    expected_value: float | None = Field(
        description="Expected incremental conversions × value_per_conversion, when both exist."
    )
    expected_net_value: float | None = Field(description="expected_value − expected_cost, when both exist.")
    net_value_low: float | None = Field(
        description="expected_net_value at the low end of the conversions interval; null without one."
    )
    net_value_high: float | None = Field(
        description="expected_net_value at the high end of the conversions interval; null without one."
    )
    roi: float | None = Field(
        description="expected_net_value / expected_cost; null when either is null or the cost is 0."
    )


class ProfitCurve(Artefact):
    """Net value against the number of customers contacted, under the targeting policy's own rules.

    Every point is what `policy_recommendation.json` would say with `budget_contacts` set to its
    `contacts`: only eligible persuadables, highest predicted uplift first, never a sleeping dog, and
    no customer whose expected gain is below the contact cost. `configured` is the run's own budget
    and equals its recommendation exactly; `optimum` maximises the expected net value over every
    possible contact count, not only the plotted ones.
    """

    run_id: str = Field(description="Run the curve belongs to.")
    computed_on: Literal["test", "scored"] = Field(
        description="Hold-out split of a training run, or every row of a scoring run."
    )
    rows: int = Field(description="Customers considered.")
    eligible_persuadables: int = Field(description="Persuadables the policy could choose from.")
    max_contacts: int = Field(
        description="Most customers the rules allow: the eligible persuadables that pay for their contact."
    )
    max_contacts_reason: PolicyStopReason = Field(
        description="Why no more: value_below_cost, all_persuadables or no_persuadables."
    )
    budget_contacts: int | None = Field(description="The run's configured contact budget, if any.")
    cost_per_contact: float | None = Field(description="Cost of one contact the curve was computed with.")
    value_per_conversion: float | None = Field(
        description="Value of one conversion the curve was computed with."
    )
    overridden: bool = Field(
        description="True when the cost or value differs from what the run was configured with."
    )
    points: tuple[ProfitPoint, ...] = Field(
        description="From 0 contacts to max_contacts, ascending, including the configured point and the optimum."
    )
    configured: ProfitPoint = Field(
        description="The point at the configured budget (every persuadable without one)."
    )
    configured_stop_reason: PolicyStopReason = Field(description="Why the configured point is not larger.")
    optimum: ProfitPoint | None = Field(
        description="The point of highest expected net value; null when the net value cannot be computed."
    )
    optimum_note: str | None = Field(
        description="Why there is no optimum, in plain words; null when there is one."
    )
    bands_available: bool = Field(description="True when the points carry a low/high net value band.")
    bands_note: str = Field(description="What the band is, or why there is none.")
    value_weighted: bool = Field(
        default=False,
        description=(
            "True when customers are ranked by their own value (`uplift.policy.value_column`, Plan J M97) "
            "and the money comes from the hold-out's value-weighted observed uplift."
        ),
    )
    value_basis: str | None = Field(
        default=None,
        description=(
            "What the money is based on, in words: the value column, or the value of one conversion, "
            "with the margin and horizon when set; null when there is no value."
        ),
    )
    money_note: str | None = Field(
        default=None,
        description="Why a money field is null, or what the money leaves out, in plain words (DEC-1307).",
    )
    values_missing: int | None = Field(
        default=None,
        description="Customers without a value, counted at zero value; null when not ranked by value.",
    )
    contact_cost: float | None = Field(
        default=None,
        description=(
            "The cost of one contact the curve used when ranked by value (the run's recorded cost unless "
            "cost_per_contact overrides it); null when not ranked by value."
        ),
    )
    offer_cost: float | None = Field(
        default=None,
        description="The offer cost (× p_treated) the curve used when ranked by value; null otherwise.",
    )
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")


# ---------------------------------------------------------------------------
# incrementality_report.json
# ---------------------------------------------------------------------------
class IncrementalityStatus(StrEnum):
    MATURE = "mature"
    IMMATURE = "immature"


class IncrementalityReport(Artefact):
    """`incrementality_report.json` - did the campaign work? Treated rate minus control rate.

    Rates, lifts and intervals are null while the outcome window has not elapsed for any row:
    "Results available on <date>" is shown instead (plan B §8). Rows whose window has not elapsed
    are excluded and counted, never guessed.
    """

    run_id: str = Field(description="Scoring run whose actions (treated and control) are measured.")
    campaign_id: str | None = Field(default=None, description="Campaign the report is about, if known.")
    outcome_column: str = Field(description="Outcome column in the outcomes file.")
    outcome_window_days: int | None = Field(description="Days after treatment the outcome is measured over.")
    as_of: AwareDatetime = Field(description="Reference time maturity was judged against.")
    status: IncrementalityStatus = Field(description="mature when at least one row's window has elapsed.")
    results_available_on: date | None = Field(
        description="First date every row's outcome window has elapsed; null when already mature."
    )
    treated_rows: int = Field(description="Mature treated rows measured.")
    treated_conversions: int = Field(description="Outcomes among them.")
    treated_rate: float | None = Field(description="treated_conversions / treated_rows.")
    control_rows: int = Field(description="Mature control rows measured.")
    control_conversions: int = Field(description="Outcomes among them.")
    control_rate: float | None = Field(description="control_conversions / control_rows.")
    absolute_lift: ConfidenceValue | None = Field(
        description="treated_rate − control_rate, with a Newcombe (Wilson score) interval."
    )
    relative_lift: float | None = Field(
        description="absolute_lift / control_rate; null when control_rate is 0."
    )
    incremental_conversions: ConfidenceValue | None = Field(
        description="absolute_lift × treated_rows: conversions the campaign caused, with its interval."
    )
    p_value: float | None = Field(description="Two-sided two-proportion z-test p-value.")
    rows_immature: int = Field(description="Rows excluded because their outcome window had not elapsed.")
    rows_without_outcome: int = Field(
        description="Treated or control rows of the run with no matching row in the outcomes file."
    )
    rows_suppressed_or_untreated: int = Field(
        description="Scored rows that were neither treated nor held out (suppressed, or not selected)."
    )
    causal: bool = Field(description="True: the control group was drawn at random by the engine.")
    summary: str = Field(description="One plain-language sentence for the Campaign results page.")
    computed_at: AwareDatetime = Field(description="UTC time the report was computed.")
    # Plan J M94 (DEC-1304 (f), pre-approved additive fields in Phase 3b's contract; typed and written by
    # `engine.measurement.measure.measure_campaign`): the registered test plan the report was read
    # against, and whether it was read before that plan's analysis date. Defaults keep every report
    # measured without a plan exactly as it was.
    test_plan_hash: str | None = Field(
        default=None, description="`plan_hash` of the registered test plan this report was measured against."
    )
    early_look: bool = Field(
        default=False,
        description="True when measured before the test plan's analysis date: an early look, not a final result.",
    )


# ---------------------------------------------------------------------------
# ope_report.json
# ---------------------------------------------------------------------------
class OpeEstimate(Artefact):
    """One off-policy estimate of a policy's mean outcome per customer."""

    method: Literal["ips", "snips", "dr"] = Field(
        description="Inverse propensity scoring, its self-normalised form, or doubly robust."
    )
    value: ConfidenceValue = Field(description="Estimated outcome rate if the policy had been followed.")


class OpeReport(Artefact):
    """`ope_report.json` - how a candidate policy would have done on logged, randomised data.

    Phase 5's action-policy agent is evaluated with this (plan B §12).
    """

    run_id: str = Field(description="Run whose logged data the policy was evaluated on.")
    policy_description: str = Field(description="What the candidate policy does, in words.")
    rows: int = Field(description="Logged rows used.")
    policy_treat_share: float = Field(description="Share of rows the candidate policy would treat.")
    propensity: float = Field(description="Logged P(treated); constant under random assignment.")
    estimates: tuple[OpeEstimate, ...] = Field(
        description=(
            "IPS, SNIPS and DR, in that order. SNIPS is left out when it is undefined: no logged row "
            "took an action the policy would take, so its ratio is 0/0."
        )
    )
    logged_value: float = Field(description="Observed outcome rate under the logging policy.")
    treat_all_value: ConfidenceValue = Field(description="DR estimate of treating everyone.")
    treat_none_value: ConfidenceValue = Field(description="DR estimate of treating no one.")
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")
    computed_at: AwareDatetime = Field(description="UTC time of the estimate.")


# ---------------------------------------------------------------------------
# risk_comparison.json and ranking_choice.json (Plan J M96)
# ---------------------------------------------------------------------------
RISK_COMPARISON_FILENAME: Final[str] = "risk_comparison.json"
"""Written by an uplift training run when `uplift.evidence.risk_comparison` is on (M96)."""
RANKING_CHOICE_FILENAME: Final[str] = "ranking_choice.json"
"""Written by an uplift scoring run whose model carries the beats-risk check (M96)."""

UPLIFT_NOT_BETTER_THAN_RISK: Final[str] = "UPLIFT_NOT_BETTER_THAN_RISK"
"""The uplift model does not beat plain risk ranking (paired AUUC difference's lower bound <= 0)."""


class PolicyComparisonValue(Artefact):
    """One ranking's top-N at the comparison's budget: what it is estimated to add."""

    ranking: Literal["uplift", "risk"] = Field(description="Which ranking chose the contacts.")
    contacts: int = Field(description="Customers it contacts (the same for both rankings).")
    incremental_conversions: ConfidenceValue = Field(
        description="Extra conversions over contacting nobody, doubly robust, with its 95% interval."
    )
    per_rupee: ConfidenceValue | None = Field(
        description="incremental_conversions / (contacts x cost per contact); null without a cost."
    )
    ope: OpeReport = Field(description="`evaluate_policy`'s IPS/SNIPS/DR report of this top-N.")


class RiskComparison(Artefact):
    """`risk_comparison.json` - uplift top-N against risk top-N at equal budget, cross-fitted (M96).

    Every row is scored by fold models that never saw it: the uplift learner and a plain risk model
    (a LightGBM classifier of the outcome) are refitted on the other folds. Both rankings contact the
    same number of customers in every fold. The value of each top-N is estimated off-policy with the
    rows' recorded treatment probabilities, and the difference is a paired, per-row estimate.
    """

    run_id: str = Field(description="Training run whose randomised rows were used.")
    rows: int = Field(description="Randomised rows the comparison used.")
    rows_excluded: int = Field(
        description="Rows left out because their recorded treatment probability was not strictly inside (0, 1)."
    )
    folds: int = Field(description="Cross-fitting folds.")
    top_share: float = Field(description="Share of each fold both rankings contact.")
    propensity_source: Literal["recorded", "treated_share"] = Field(
        description=(
            "recorded: each row's own P(treated); treated_share: the constant share treated (random "
            "assignment)."
        )
    )
    cost_per_contact: float | None = Field(description="Cost of one contact; null leaves per_rupee null.")
    uplift: PolicyComparisonValue = Field(description="Ranking by cross-fitted predicted uplift.")
    risk: PolicyComparisonValue = Field(description="Ranking by cross-fitted plain risk.")
    difference: ConfidenceValue = Field(
        description="Uplift minus risk incremental conversions, paired per row, with its 95% interval."
    )
    difference_per_rupee: ConfidenceValue | None = Field(
        description="The difference per rupee spent; null without a cost per contact."
    )
    uplift_better: bool = Field(description="True when the difference's lower bound is above zero.")
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")
    summary: str = Field(description="One plain sentence for the Model page and the Approver's screen.")
    computed_at: AwareDatetime = Field(description="UTC time of the comparison.")


class RankingChoice(Artefact):
    """`ranking_choice.json` - which ranking a scoring run's contact list used, and why (M96, J5).

    Written only when the uplift model's training evaluation carries the beats-risk check
    (`baseline_comparison`); a model trained before M96 ranks by uplift exactly as before and no file
    is written.
    """

    run_id: str = Field(description="Scoring run the choice was made for.")
    model_version_id: str = Field(description="The uplift model version that scored the rows.")
    ranking: Literal["uplift", "propensity_model"] = Field(description="What ordered the contact list.")
    code: str | None = Field(
        description="UPLIFT_NOT_BETTER_THAN_RISK when the uplift model failed the check; null when it passed."
    )
    beats_risk: bool = Field(description="The training evaluation's beats-risk verdict.")
    propensity_model_id: str | None = Field(
        description="The approved propensity model that ranked the list, when it did."
    )
    compared_baseline: BaselineKind | None = Field(
        default=None,
        description=(
            "The plain ranking the training run's beats-risk check was decided against "
            "(`baseline_comparison.risk_baseline`)."
        ),
    )
    compared_model_id: str | None = Field(
        default=None,
        description=(
            "The propensity model that check compared the uplift model with; null when it compared "
            "with the model's own p_control (no propensity model was approved then)."
        ),
    )
    fallback_matches_check: bool | None = Field(
        default=None,
        description=(
            "When the list is ranked by a propensity model: true when it is the model the training "
            "check compared with, false when it is another (newer) one; null when the list is not "
            "ranked by a propensity model."
        ),
    )
    contacts: int = Field(description="Customers marked Treat (the uplift policy's own count: equal budget).")
    reason: str = Field(description="The plain reason the Output page shows.")
    computed_at: AwareDatetime = Field(description="UTC time the choice was made.")


# ---------------------------------------------------------------------------
# model/uplift_model.json
# ---------------------------------------------------------------------------
class UpliftModelCard(Artefact):
    """What a scoring run needs to replay a trained uplift model; saved beside the pickle."""

    learner: UpliftLearner = Field(description="Meta-learner.")
    base_model: UpliftBaseModel = Field(description="Model family inside it.")
    feature_columns: tuple[str, ...] = Field(description="Features, in fit order.")
    categorical_levels: dict[str, tuple[str, ...]] = Field(
        description="Category levels seen at fit time per categorical feature; unseen levels score as missing."
    )
    dropped_columns: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Columns of the training file that were not used as features, mapped to why: high_null, "
            "constant, id_like, pii, date or unsupported_type. Reserved columns (key, outcome, "
            "treatment and the other configured roles) are never listed."
        ),
    )
    treatment_column: str = Field(description="Treatment column of the training data.")
    outcome_column: str = Field(description="Outcome column of the training data.")
    positive_label: str = Field(description="Outcome value counted as a conversion, stringified.")
    propensity: float = Field(description="Share of training rows that were treated.")
    base_rate: float = Field(description="Outcome rate over the training rows.")
    training_rows: int = Field(description="Rows the learners were fitted on.")
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")
    segment_thresholds: SegmentThresholds = Field(description="Cuts applied when this model segments rows.")
    engine_version: str = Field(description="Engine version that trained the model.")
    trained_at: AwareDatetime = Field(description="UTC time the model was trained.")


# ---------------------------------------------------------------------------
# uplift_drift.json
# ---------------------------------------------------------------------------
class TreatmentShareDrift(Artefact):
    """Whether new data's treated share is close to the training data's (M53, DEC-857).

    Only a file that carries the model's treatment column, with 0/1 values, can be compared; for
    any other file the status is `not_applicable` and `reason` says why - nothing is estimated.
    """

    status: Literal["within_tolerance", "outside_tolerance", "not_applicable"] = Field(
        description="The verdict of the absolute-difference rule, or not_applicable."
    )
    treatment_column: str = Field(description="The model's treatment column.")
    training_treated_share: float = Field(description="Share of training rows that were treated.")
    current_treated_share: float | None = Field(
        default=None, description="Share of this file's rows that are treated; null when not applicable."
    )
    absolute_difference: float | None = Field(
        default=None, description="|current - training|; null when not applicable."
    )
    tolerance: float = Field(description="`uplift.drift_treated_share_tolerance` the difference is held to.")
    p_value: float | None = Field(
        default=None,
        description=(
            "Two-sided two-proportion z-test p-value, for information only: on a large file it flags "
            "differences too small to matter, so the verdict is the absolute difference."
        ),
    )
    rows_compared: int = Field(default=0, description="Rows of this file with a 0/1 treatment value.")
    training_rows: int = Field(description="Rows the training share was measured on.")
    reason: str | None = Field(default=None, description="Why the check did not apply; null when it did.")


class UpliftDriftReport(Artefact):
    """`uplift_drift.json` - a scoring run's data against its uplift model's training data (M53).

    Feature drift is Phase 1's PSI (`engine.stages.score.compute_drift`) against the baseline the
    uplift training run stored; `features` is null - and `features_reason` says why - when no
    baseline exists (a model trained before M53) or there is nothing to compare.
    """

    run_id: str = Field(description="Scoring run this report belongs to.")
    model_version_id: str = Field(description="Uplift model version that scored the file.")
    training_run_id: str = Field(description="Training run of that model version.")
    features: DriftReport | None = Field(description="Phase 1's per-feature PSI report, or null.")
    features_reason: str | None = Field(
        default=None, description="Why feature drift was not measured; null when it was."
    )
    treatment: TreatmentShareDrift = Field(description="The treated-share check.")
    summary: str = Field(description="One line for the Data and Output pages.")
    computed_at: AwareDatetime = Field(description="UTC time the report was computed.")


UPLIFT_ARTEFACTS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        UPLIFT_VALIDATION_FILENAME: UpliftValidationReport,
        UPLIFT_EVALUATION_FILENAME: UpliftEvaluation,
        QINI_CURVE_FILENAME: QiniCurve,
        SEGMENTS_FILENAME: SegmentReport,
        POLICY_FILENAME: PolicyRecommendation,
        INCREMENTALITY_FILENAME: IncrementalityReport,
        OPE_FILENAME: OpeReport,
        UPLIFT_DRIFT_FILENAME: UpliftDriftReport,
        RISK_COMPARISON_FILENAME: RiskComparison,  # Plan J M96
        RANKING_CHOICE_FILENAME: RankingChoice,  # Plan J M96
    }
)
"""Uplift artefact filename -> its model. Served by `api/routes/uplift.py` (DEC-602)."""
