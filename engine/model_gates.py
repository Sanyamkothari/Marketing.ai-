"""Advisory checks shown beside the champion decision on the Approver's screen (Plan J M96).

The champion rule (`engine.registry.should_promote`) is frozen: an uplift challenger is promoted or
sent for approval on its AUUC alone, as before. What an Approver could not see until M96 is whether
the model *earns its place* - whether it ranks customers better than plain risk ranking does, whether
its predicted uplift matches what was measured, and whether its quality holds when it is refitted on
other rows. This module reads what the training run measured and turns it into checks:

* `UPLIFT_NOT_BETTER_THAN_RISK` - from `UpliftEvaluation.baseline_comparison`: passes when the paired
  AUUC difference against risk ranking has its lower bound above zero. When the run also wrote the
  equal-budget comparison (`risk_comparison.json`), its sentence is added, and so is its "safe to roll
  out" sentence when the run computed one (Plan K M137, `uplift.evidence.rollout_verdict`). The pass
  rule does not change: the verdict is evidence for the Approver.
* `UPLIFT_UNSTABLE_ACROSS_FOLDS` - from `fold_auuc`: passes when every fold was measured, no fold's
  bootstrap interval lies wholly at or below zero, and the folds' AUUCs differ no more than their
  sampling noise explains (`engine.measurement.compare.fold_auuc_report`).
  Off by default; the message then says what turning it on would cost.
* `UPLIFT_MISCALIBRATED` - from `calibration_by_decile`: passes when at least 8 in 10 deciles'
  intervals contain the predicted uplift.

Each check is `{code, passed, message}`. `passed` is null when the evidence does not exist (a model
trained before M96, or a check that is off): "not measured", never a made-up pass. The checks are
advisory: nothing here approves, rejects or blocks. Read-only, from the training run's artefacts; no
model is loaded or scored on request.

Plan L follow-up (DEC-1599) adds one check for a model that is not an uplift model:

* `RESULT_SUSPICIOUSLY_GOOD` - from `evaluation.json` and `feature_importance.json`: fails (a warning,
  never a block) when the model's main measure on the test split is above the use case's "too good to
  be true" line in `configs/model_checks.yaml` (by default an ROC AUC above 0.97). Its sentence names
  the column the model leans on most and asks the person to check whether that column is known only
  after the outcome. A main measure with no line (a regression's error) falls back to the ROC AUC when
  the run measured one; with neither, or without `evaluation.json`, there is nothing to judge and the
  check is not listed at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.config import Metric, config_root, load_yaml
from engine.contracts import EvaluationReport, FeatureImportance, ModelVersion
from engine.storage import Storage, StorageError, run_key
from engine.uplift.contracts import (
    RISK_COMPARISON_FILENAME,
    UPLIFT_EVALUATION_FILENAME,
    UPLIFT_NOT_BETTER_THAN_RISK,
    RiskComparison,
    UpliftEvaluation,
)

__all__ = [
    "DEFAULT_SUSPICION_THRESHOLDS",
    "MODEL_CHECKS_FILENAME",
    "RESULT_SUSPICIOUSLY_GOOD",
    "UPLIFT_GATE_CODES",
    "UPLIFT_MISCALIBRATED",
    "UPLIFT_NOT_BETTER_THAN_RISK",
    "UPLIFT_UNSTABLE_ACROSS_FOLDS",
    "ApprovalCheck",
    "SuspicionConfig",
    "approval_checks",
    "load_suspicion_config",
    "suspiciously_good",
    "uplift_checks",
]

UPLIFT_UNSTABLE_ACROSS_FOLDS: Final[str] = "UPLIFT_UNSTABLE_ACROSS_FOLDS"
UPLIFT_MISCALIBRATED: Final[str] = "UPLIFT_MISCALIBRATED"

UPLIFT_GATE_CODES: Final[frozenset[str]] = frozenset(
    {UPLIFT_NOT_BETTER_THAN_RISK, UPLIFT_UNSTABLE_ACROSS_FOLDS, UPLIFT_MISCALIBRATED}
)
"""M96's codes; joined into `engine.decide.codes.PLAN_J_CODES` at integration (one definition)."""

_M = TypeVar("_M", bound=BaseModel)

NOT_CHECKED: Final[str] = "Not checked: this model was trained before this check existed."


class ApprovalCheck(BaseModel):
    """One advisory check on the Approver's screen: `{code, passed, message}`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(description="What was checked, e.g. UPLIFT_NOT_BETTER_THAN_RISK.")
    passed: bool | None = Field(description="True passed, false failed, null not measured.")
    message: str = Field(description="The plain sentence the screen shows, numbers filled in.")


def uplift_checks(
    evaluation: UpliftEvaluation, comparison: RiskComparison | None = None
) -> tuple[ApprovalCheck, ...]:
    """The three checks of an uplift model's evaluation (and its equal-budget comparison, if any)."""
    baseline = evaluation.baseline_comparison
    if baseline is None:
        risk = ApprovalCheck(code=UPLIFT_NOT_BETTER_THAN_RISK, passed=None, message=NOT_CHECKED)
    else:
        message = baseline.summary
        if comparison is not None:
            message = f"{message} {comparison.summary}"
            if comparison.rollout is not None:  # Plan K M137: evidence beside the rule, never the rule
                message = f"{message} {comparison.rollout.summary}"
        risk = ApprovalCheck(code=UPLIFT_NOT_BETTER_THAN_RISK, passed=baseline.beats_risk, message=message)
    folds = evaluation.fold_auuc
    stability = ApprovalCheck(
        code=UPLIFT_UNSTABLE_ACROSS_FOLDS,
        passed=None if folds is None or not folds.computed else folds.stable,
        message=NOT_CHECKED if folds is None else folds.summary,
    )
    calibration = evaluation.calibration_by_decile
    calibrated = ApprovalCheck(
        code=UPLIFT_MISCALIBRATED,
        passed=None if calibration is None else calibration.well_calibrated,
        message=NOT_CHECKED if calibration is None else calibration.summary,
    )
    return (risk, stability, calibrated)


def approval_checks(
    storage: Storage, version: ModelVersion, root: Path | None = None
) -> tuple[ApprovalCheck, ...]:
    """The advisory checks for a version waiting for approval.

    An uplift model gets M96's three. Any other model gets `RESULT_SUSPICIOUSLY_GOOD` when its training
    run's evaluation can be read and has a measure with a line (DEC-1599); otherwise none.
    """
    if version.metric is not Metric.AUUC:
        evaluation_report = _read(storage, version, EVALUATION_FILENAME, EvaluationReport)
        if evaluation_report is None:
            return ()
        importance = _read(storage, version, FEATURE_IMPORTANCE_FILENAME, FeatureImportance)
        thresholds = load_suspicion_config(root).thresholds_for(version.use_case_id)
        alarm = suspiciously_good(evaluation_report, importance, thresholds)
        return () if alarm is None else (alarm,)
    evaluation = _read(storage, version, UPLIFT_EVALUATION_FILENAME, UpliftEvaluation)
    if evaluation is None:
        return (
            ApprovalCheck(
                code=UPLIFT_NOT_BETTER_THAN_RISK,
                passed=None,
                message="Not checked: this model's evaluation could not be read.",
            ),
        )
    comparison = _read(storage, version, RISK_COMPARISON_FILENAME, RiskComparison)
    return uplift_checks(evaluation, comparison)


def _read(storage: Storage, version: ModelVersion, name: str, model: type[_M]) -> _M | None:
    key = version.artefact_keys.get(name, run_key(version.run_id, name))
    try:
        return storage.read_model(key, model) if storage.exists(key) else None
    except (StorageError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Plan L follow-up (DEC-1599): a result too good to be true
# ---------------------------------------------------------------------------
RESULT_SUSPICIOUSLY_GOOD: Final[str] = "RESULT_SUSPICIOUSLY_GOOD"

MODEL_CHECKS_FILENAME: Final[str] = "model_checks.yaml"
"""The "too good to be true" lines, relative to the configuration root."""

EVALUATION_FILENAME: Final[str] = "evaluation.json"
FEATURE_IMPORTANCE_FILENAME: Final[str] = "feature_importance.json"

_HIGHER_IS_BETTER: Final[frozenset[Metric]] = frozenset(
    {Metric.ROC_AUC, Metric.PR_AUC, Metric.F1, Metric.RECALL, Metric.PRECISION}
)
"""The measures a "too good" line can be set for: higher is better and 1 is perfect."""

DEFAULT_SUSPICION_THRESHOLDS: Final[dict[Metric, float]] = {Metric.ROC_AUC: 0.97}
"""Used when `configs/model_checks.yaml` is absent: an ROC AUC above 0.97 is rare in marketing data."""


class SuspicionConfig(BaseModel):
    """`configs/model_checks.yaml`: per measure, the score above which a result is suspiciously good."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    default: dict[Metric, float] = Field(
        default_factory=lambda: dict(DEFAULT_SUSPICION_THRESHOLDS),
        description="Measure to line, for every use case.",
    )
    use_cases: dict[str, dict[Metric, float]] = Field(
        default_factory=dict, description="Use case id to its own lines, replacing the default per measure."
    )

    @field_validator("default")
    @classmethod
    def _default_lines(cls, value: dict[Metric, float]) -> dict[Metric, float]:
        return _checked_lines(value)

    @field_validator("use_cases")
    @classmethod
    def _use_case_lines(cls, value: dict[str, dict[Metric, float]]) -> dict[str, dict[Metric, float]]:
        for lines in value.values():
            _checked_lines(lines)
        return value

    def thresholds_for(self, use_case_id: str) -> dict[Metric, float]:
        """The default lines with the use case's own on top."""
        return {**self.default, **self.use_cases.get(use_case_id, {})}


def _checked_lines(lines: dict[Metric, float]) -> dict[Metric, float]:
    for metric, line in lines.items():
        if metric not in _HIGHER_IS_BETTER:
            raise ValueError(f"a 'too good' line needs a measure where higher is better, not {metric.value}")
        if not 0.0 < line <= 1.0:
            raise ValueError(f"the line for {metric.value} must be above 0 and at most 1, not {line}")
    return lines


def load_suspicion_config(root: Path | None = None) -> SuspicionConfig:
    """`configs/model_checks.yaml`, or the built-in default line when the file is absent."""
    path = config_root(root) / MODEL_CHECKS_FILENAME
    if not path.is_file():
        return SuspicionConfig()
    return SuspicionConfig.model_validate(load_yaml(path))


def _judged_measure(
    evaluation: EvaluationReport, thresholds: dict[Metric, float]
) -> tuple[str, float, float] | None:
    """(label, score, line) of the measure the alarm judges: the main one, else the ROC AUC; or None."""
    if evaluation.primary_metric in thresholds:
        return (
            evaluation.primary_metric_label,
            evaluation.headline_score,
            thresholds[evaluation.primary_metric],
        )
    for item in evaluation.metrics:
        if item.id is Metric.ROC_AUC and Metric.ROC_AUC in thresholds:
            return (item.label, item.value, thresholds[Metric.ROC_AUC])
    return None


def suspiciously_good(
    evaluation: EvaluationReport,
    importance: FeatureImportance | None,
    thresholds: dict[Metric, float],
) -> ApprovalCheck | None:
    """`RESULT_SUSPICIOUSLY_GOOD`: failed (a warning) above the line; None when no measure has a line.

    Advisory like M96's checks: it never approves, rejects or blocks a model. The sentence names the
    column the model leans on most (rank 1 of `feature_importance.json`), or says it cannot when the run
    saved no importances.
    """
    judged = _judged_measure(evaluation, thresholds)
    if judged is None:
        return None
    label, score, line = judged
    if score <= line:
        return ApprovalCheck(
            code=RESULT_SUSPICIOUSLY_GOOD,
            passed=True,
            message=(
                f"{label} on the test set is {score:.3f}, not above {line:.2f}: "
                "good without being too good to be true."
            ),
        )
    items = sorted(importance.items, key=lambda item: item.rank) if importance is not None else []
    if items:
        top = items[0]
        culprit = (
            f"The column the model leans on most is '{top.feature}' ({top.share_pct:.0f}% of the importance). "
            f"Check whether '{top.feature}' is known only after the outcome (for example a cancellation date, "
            "a closing status or a refund); if it is, hide it and train again."
        )
    else:
        culprit = (
            "This run saved no ranking of the columns, so we cannot name the one it leans on most: check "
            "each column for one that is known only after the outcome, hide it and train again."
        )
    return ApprovalCheck(
        code=RESULT_SUSPICIOUSLY_GOOD,
        passed=False,
        message=(
            f"{label} on the test set is {score:.3f}, above {line:.2f}. Results this good are rare with "
            f"marketing data and often mean a column gives the answer away. {culprit} "
            "This is a warning only: nothing is blocked."
        ),
    )
