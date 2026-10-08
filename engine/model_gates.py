"""Advisory checks shown beside the champion decision on the Approver's screen (Plan J M96).

The champion rule (`engine.registry.should_promote`) is frozen: an uplift challenger is promoted or
sent for approval on its AUUC alone, as before. What an Approver could not see until M96 is whether
the model *earns its place* - whether it ranks customers better than plain risk ranking does, whether
its predicted uplift matches what was measured, and whether its quality holds when it is refitted on
other rows. This module reads what the training run measured and turns it into checks:

* `UPLIFT_NOT_BETTER_THAN_RISK` - from `UpliftEvaluation.baseline_comparison`: passes when the paired
  AUUC difference against risk ranking has its lower bound above zero. When the run also wrote the
  equal-budget comparison (`risk_comparison.json`), its sentence is added.
* `UPLIFT_UNSTABLE_ACROSS_FOLDS` - from `fold_auuc`: passes when every fold was measured, no fold's
  bootstrap interval lies wholly at or below zero, and the folds' AUUCs differ no more than their
  sampling noise explains (`engine.measurement.compare.fold_auuc_report`).
  Off by default; the message then says what turning it on would cost.
* `UPLIFT_MISCALIBRATED` - from `calibration_by_decile`: passes when at least 8 in 10 deciles'
  intervals contain the predicted uplift.

Each check is `{code, passed, message}`. `passed` is null when the evidence does not exist (a model
trained before M96, or a check that is off): "not measured", never a made-up pass. The checks are
advisory: nothing here approves, rejects or blocks, and a model that is not an uplift model has none.
Read-only, from the training run's artefacts; no model is loaded or scored on request.
"""

from __future__ import annotations

from typing import Final, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from engine.config import Metric
from engine.contracts import ModelVersion
from engine.storage import Storage, StorageError, run_key
from engine.uplift.contracts import (
    RISK_COMPARISON_FILENAME,
    UPLIFT_EVALUATION_FILENAME,
    UPLIFT_NOT_BETTER_THAN_RISK,
    RiskComparison,
    UpliftEvaluation,
)

__all__ = [
    "UPLIFT_GATE_CODES",
    "UPLIFT_MISCALIBRATED",
    "UPLIFT_NOT_BETTER_THAN_RISK",
    "UPLIFT_UNSTABLE_ACROSS_FOLDS",
    "ApprovalCheck",
    "approval_checks",
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


def approval_checks(storage: Storage, version: ModelVersion) -> tuple[ApprovalCheck, ...]:
    """The advisory checks for a version waiting for approval; empty for a model that is not uplift."""
    if version.metric is not Metric.AUUC:
        return ()
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
