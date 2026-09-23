"""Artefact contracts of the uplift problem type (plan B §7).

Every JSON an uplift run writes is one of these models, and the UI renders from them and nothing
else (plan §2.1 principle 2). They extend `engine.contracts.Artefact`, so they are frozen, refuse
unknown keys and carry a `schema_version` like every Phase 1 artefact.

They live here rather than in `engine/contracts.py` because that file's artefact registry is an
immutable mapping built above the shared-file blocks (PARALLEL_WORK_PROTOCOL.md §4). Uplift keeps a
registry of its own, :data:`UPLIFT_ARTEFACTS`, and `api/routes/uplift.py` serves from it (DEC-602).

Uplift validation findings have the same five fields as `engine.contracts.ValidationCheck`, but
their own code table: `ValidationCheck` accepts only the Phase 1 table, which tests pin at nineteen
codes. Phase 2 met the same wall with `OnboardingCheck` (DEC-101); the request to unify all three
behind a code registry is in `docs/CROSS_BRANCH_REQUESTS.md` (DEC-603).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from engine.config import Metric
from engine.contracts import Artefact, Severity
from engine.uplift.config import UpliftBaseModel, UpliftLearner

__all__ = [
    "INCREMENTALITY_FILENAME",
    "OPE_FILENAME",
    "POLICY_FILENAME",
    "QINI_CURVE_FILENAME",
    "SEGMENTS_FILENAME",
    "UPLIFT_ARTEFACTS",
    "UPLIFT_EVALUATION_FILENAME",
    "UPLIFT_MODEL_CARD_FILENAME",
    "UPLIFT_VALIDATION_CODES",
    "UPLIFT_VALIDATION_FILENAME",
    "ConfidenceValue",
    "IncrementalityReport",
    "IncrementalityStatus",
    "OpeEstimate",
    "OpeReport",
    "PolicyRecommendation",
    "PolicyStopReason",
    "QiniCurve",
    "QiniPoint",
    "Segment",
    "SegmentReport",
    "SegmentSummary",
    "SegmentThresholds",
    "UpliftAtK",
    "UpliftCheck",
    "UpliftDecile",
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
UPLIFT_VALIDATION_CODES: Final[frozenset[str]] = frozenset(
    {
        "TREATMENT_COLUMN_MISSING",
        "TREATMENT_NOT_BINARY",
        "TREATMENT_ARM_TOO_SMALL",
        "TREATMENT_NOT_RANDOM",
        "OUTCOME_WINDOW_IMMATURE",
        "FEATURE_AFTER_TREATMENT",
    }
)
"""Plan B §4, verbatim. Phase 1's table is `engine.contracts.VALIDATION_CODES`."""


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
        if self.code not in UPLIFT_VALIDATION_CODES:
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
    deciles: tuple[UpliftDecile, ...] = Field(description="Ten rows, highest predicted uplift first.")
    bootstrap_samples: int = Field(description="Resamples behind every interval.")
    measurable_uplift: bool = Field(description="True when the AUUC interval lies entirely above zero.")
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")
    summary: str = Field(description="One plain-language sentence for the Model page.")
    evaluated_at: AwareDatetime = Field(description="UTC time of the evaluation.")


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
    expected_cost: float | None = Field(description="N × cost_per_contact, when a cost is configured.")
    expected_value: float | None = Field(
        description="Expected incremental conversions × value_per_conversion, when configured."
    )
    expected_net_value: float | None = Field(description="expected_value − expected_cost, when both exist.")
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
    estimates: tuple[OpeEstimate, ...] = Field(description="IPS, SNIPS and DR, in that order.")
    logged_value: float = Field(description="Observed outcome rate under the logging policy.")
    treat_all_value: ConfidenceValue = Field(description="DR estimate of treating everyone.")
    treat_none_value: ConfidenceValue = Field(description="DR estimate of treating no one.")
    causal: bool = Field(description="False when the treatment was acknowledged as not random.")
    computed_at: AwareDatetime = Field(description="UTC time of the estimate.")


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


UPLIFT_ARTEFACTS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        UPLIFT_VALIDATION_FILENAME: UpliftValidationReport,
        UPLIFT_EVALUATION_FILENAME: UpliftEvaluation,
        QINI_CURVE_FILENAME: QiniCurve,
        SEGMENTS_FILENAME: SegmentReport,
        POLICY_FILENAME: PolicyRecommendation,
        INCREMENTALITY_FILENAME: IncrementalityReport,
        OPE_FILENAME: OpeReport,
    }
)
"""Uplift artefact filename -> its model. Served by `api/routes/uplift.py` (DEC-602)."""
