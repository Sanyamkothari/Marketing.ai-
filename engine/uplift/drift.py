"""Drift for uplift models: Phase 1's feature PSI, plus whether the treated share moved (M53).

A scoring run of an uplift model asks two questions of its file.

**Do the customers still look like the ones the model was trained on?** That is Phase 1's drift
question, and it is answered by Phase 1's code: the uplift training run stores a
`drift_baseline.json` through `engine.stages.register.drift_baseline` (over the raw columns its
feature spec kept), and this module hands it to `engine.stages.score.compute_drift` - the same
bins, the same PSI, the same `monitoring.drift_psi_threshold` and the same `DriftReport`. Nothing
about PSI is re-implemented here. A model trained before M53 stored no baseline, and then feature
drift is `null` with the reason, never an invented zero (DEC-051).

**Is the treated share still what it was?** An uplift model's segments and the policy's expected
conversions were measured on a campaign with a particular treated share. A file that carries the
model's treatment column - a re-scored campaign, or the next wave of the same experiment - can be
compared with it: `|current share - training share| <= uplift.drift_treated_share_tolerance`
(default 0.05, absolute). An absolute difference is used, not a significance test, because the
test's verdict depends on the file's size: on a million rows a z-test calls a 0.2-point difference
"significant", and on two hundred it misses a 10-point one. The two-proportion p-value is still
recorded, for information. A file without the column, or whose column is not 0/1, gets
`not_applicable` with the reason - a scoring file usually carries no treatment at all, because the
campaign it is scored for has not happened yet (DEC-857).

`numpy` and `pandas` are imported inside function bodies, as everywhere in the engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from engine.storage import StorageError
from engine.uplift.contracts import TreatmentShareDrift, UpliftDriftReport
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import DriftReport, ModelVersion
    from engine.storage import Storage
    from engine.uplift.contracts import UpliftModelCard

__all__ = [
    "NOTHING_TO_COMPARE",
    "NO_BASELINE",
    "measure_uplift_drift",
    "treatment_share_drift",
]

_LOGGER = get_logger(__name__)

NO_BASELINE: Final[str] = (
    "This model version stored no drift baseline (it was trained before uplift drift existed), so "
    "feature drift was not measured."
)
NOTHING_TO_COMPARE: Final[str] = (
    "There were no rows or no features to compare, so feature drift was not measured."
)
_COLUMN_ABSENT: Final[str] = "This file has no '{column}' column, so its treated share cannot be compared."
_COLUMN_NOT_BINARY: Final[str] = (
    "'{column}' holds {bad} value(s) that are not 0 or 1, so this file's treated share cannot be compared."
)
_NO_ROWS: Final[str] = "This file has no rows, so its treated share cannot be compared."


def treatment_share_drift(
    frame: pd.DataFrame, card: UpliftModelCard, *, tolerance: float
) -> TreatmentShareDrift:
    """The treated-share check of the module docstring, on `frame` against the model card's share."""
    from engine.uplift.data import coerce_treatment
    from engine.uplift.incrementality import two_proportion_p_value

    column = card.treatment_column
    not_applicable = TreatmentShareDrift(
        status="not_applicable",
        treatment_column=column,
        training_treated_share=round(card.propensity, 4),
        tolerance=tolerance,
        training_rows=card.training_rows,
    )
    if column not in frame.columns:
        return not_applicable.model_copy(update={"reason": _COLUMN_ABSENT.format(column=column)})
    if not len(frame.index):
        return not_applicable.model_copy(update={"reason": _NO_ROWS})
    values, bad = coerce_treatment(frame[column])
    if values is None:
        return not_applicable.model_copy(update={"reason": _COLUMN_NOT_BINARY.format(column=column, bad=bad)})
    rows = len(values)
    treated = int(values.sum())
    share = treated / rows
    difference = abs(share - card.propensity)
    training_treated = round(card.propensity * card.training_rows)
    return not_applicable.model_copy(
        update={
            "status": "within_tolerance" if difference <= tolerance else "outside_tolerance",
            "current_treated_share": round(share, 4),
            "absolute_difference": round(difference, 4),
            "p_value": two_proportion_p_value(treated, rows, training_treated, card.training_rows),
            "rows_compared": rows,
        }
    )


def _feature_drift(
    frame: pd.DataFrame, config: UseCaseConfig, *, version: ModelVersion, run_id: str, storage: Storage
) -> tuple[DriftReport | None, str | None]:
    """Phase 1's `compute_drift` against the version's baseline, or `(None, why not)`."""
    from engine.contracts import DriftBaseline
    from engine.stages.score import compute_drift, not_drift_features

    key = version.drift_baseline_key
    if key is None or not storage.exists(key):
        return None, NO_BASELINE
    try:
        baseline = storage.read_model(key, DriftBaseline)
    except (StorageError, ValueError):
        _LOGGER.warning("uplift drift: the baseline of %s could not be read", version.model_id)
        return None, NO_BASELINE
    # Never the label or the key, even from a baseline written before DEC-957 that lists them.
    not_features = not_drift_features(version, storage=storage)
    report = compute_drift(baseline, frame, config, run_id=run_id, not_features=not_features)
    return report, None if report is not None else NOTHING_TO_COMPARE


def _summary(features: DriftReport | None, treatment: TreatmentShareDrift) -> str:
    """`PSI 0.03, stable · treated share within 0.05 of training`, every part measured or said not to be."""
    feature_part = "feature drift not measured" if features is None else features.summary
    if treatment.status == "not_applicable":
        share_part = "treated share not compared"
    elif treatment.status == "within_tolerance":
        share_part = f"treated share within {treatment.tolerance:g} of training"
    else:
        share_part = (
            f"treated share {treatment.current_treated_share:.1%} vs {treatment.training_treated_share:.1%} "
            "in training"
        )
    return f"{feature_part} · {share_part}"


def measure_uplift_drift(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    version: ModelVersion,
    card: UpliftModelCard,
    run_id: str,
    storage: Storage,
) -> UpliftDriftReport:
    """`uplift_drift.json` for a scoring run: feature PSI and the treated-share check."""
    features, reason = _feature_drift(frame, config, version=version, run_id=run_id, storage=storage)
    treatment = treatment_share_drift(frame, card, tolerance=config.uplift.drift_treated_share_tolerance)
    if treatment.status == "outside_tolerance":
        _LOGGER.warning(
            "uplift drift: treated share moved by %.4f (tolerance %.2f)",
            treatment.absolute_difference or 0.0,
            treatment.tolerance,
        )
    return UpliftDriftReport(
        run_id=run_id,
        model_version_id=version.model_id,
        training_run_id=version.run_id,
        features=features,
        features_reason=reason,
        treatment=treatment,
        summary=_summary(features, treatment),
        computed_at=utc_now(),
    )
