"""Evaluate stage (M3): hold-out metrics, confusion matrix, decile lift and fairness.

THE TEST SPLIT IS FINAL-DECISION-ONLY. It is measured once, to decide, and is never used to
choose a threshold, a model, a calibrator or any hyperparameter. That is why this module is a pure
measurement function: it takes a fitted `Scorer`, a test frame and the `EvaluationConfig` block,
and nothing else. It cannot see the training split, the validation split, the prepare settings or
the model search settings, so it cannot select on the hold-out even by accident.

Everything the stage needs about the model arrives on the scorer, which carries its own decision
threshold (chosen on validation at train time) and its own calibration summary (fitted on
validation at train time). The stage reports those and measures at them; it never fits anything.
Because the signature is this small, the same call can re-score the reigning champion and a
challenger on one held-out frame, which is what the champion rule needs.

The baseline comparison is a separate, pure composition: the train stage evaluates the baseline
scorer through this same `evaluate` call and hands both reports to `compare_to_baseline`.

A row counts as predicted-positive when `score >= threshold`, the sklearn convention. Floats that
reach the UI are rounded to four decimal places at the producing end; `*_pct` fields hold
percentages, `*_rate` fields hold fractions.

Edge cases, decided once and never allowed to reach an artefact as a NaN, a warning or an invented
number (plan section 13.3):

| Edge case | Behaviour |
|---|---|
| Empty test split | `EvaluationError("EVAL_EMPTY_TEST_SPLIT")`. There is nothing to report on. |
| Test frame without the scorer's target column | `EvaluationError("EVAL_TARGET_COLUMN_MISSING")`. |
| Scorer without the call its problem type needs | `EvaluationError("EVAL_SCORER_INCOMPLETE")`, naming the missing method. |
| Scorer that returns the wrong number of scores, or a probability block that is neither one column nor two | `EvaluationError("EVAL_SCORES_SHAPE")`. |
| Classification scorer that does not name its two classes | `EvaluationError("EVAL_CLASSES_UNKNOWN")`. Guessing which value is positive would silently invert every count. |
| Test target holding a value outside the scorer's two classes | `EvaluationError("EVAL_TARGET_NOT_BINARY")`, rather than quietly filing a third class under "negative". |
| Single-class test split (zero base rate, or every row positive) | ROC-AUC is undefined and is left out of `metrics` entirely; PR-AUC is left out when there are no positives. Every other metric that still has a denominator is kept. Which class is positive comes from the scorer, so a one-class split cannot invert it. |
| The primary metric itself is undefined | `EvaluationError("EVAL_PRIMARY_METRIC_UNDEFINED")`. `EvaluationReport.headline_score` is a required float, so the honest alternative to a number would be a fabricated one. |
| A primary metric that cannot score the scorer's problem type | `EvaluationError("EVAL_METRIC_NOT_FOR_PROBLEM")`. |
| Zero-denominator precision / recall / F1 | Left out of `metrics`. `ConfusionMatrix` types the same four rates as required floats, so there they take the sklearn zero-division convention of `0.0`; the four counts are on the same card, so the reader can see the denominator is empty. |
| Zero base rate | Lift has no denominator: every `lift` and `cumulative_lift` is null, and the chart falls back to `unit="%"` with the (all-zero) actual rates and a caption that says why. |
| Perfectly separable split | Nothing special: ROC-AUC and PR-AUC are genuinely 1.0. |
| Fewer rows than deciles | `min(10, rows)` bins of one row each rather than ten bins padded with empty ones; an empty bin has no score range to report. The caption names the real bin count. |
| Ties in the score at a decile boundary | Bins are rank-based and equal-sized (sizes differ by at most one), so tied rows may land either side of a boundary. The order is a stable descending sort, so which side is deterministic: earlier rows first. |
| Scorer carrying no calibration | `EvaluationReport.calibration` is null, per the contract. The measured test-split Brier score is reported in `extra_metrics` either way, because that is a measurement rather than a claim about a fit. |
| Scorer carrying an empty `threshold_detail` | One is written from the mode and the threshold, so the Model page always has a line. |
| A fairness group with fewer than 10 rows | Reported with its row count and positive rate, but left out of the three max-gap numbers, so a gap is never driven by a handful of rows. |
| An empty fairness group | Never emitted. Groups come from the values actually present, so an unused category level simply has no row. |
| Nulls in the sensitive column | Grouped together under `(missing)` rather than dropped, so the row counts still add up to the test split. |
| Fewer than two fairness groups, or fewer than two large enough ones | `evaluated` stays false with a reason for the first, and the gaps are null for the second. Fairness never blocks a run. |
| Regression | No confusion matrix at all (the second element of the return tuple is null), no fairness (the statistics are classification-only), and the decile chart draws actual mean over the overall mean. |
| Regression with a non-positive overall actual mean | That ratio has no meaning, so `values` is empty and the caption says the chart cannot be drawn. |
| Two reports that did not measure the same thing | `compare_to_baseline` raises `EvaluationError("EVAL_BASELINE_MISMATCH")` rather than put two different hold-outs in one table. |
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from engine.config import Metric, ModelFamily, ProblemType, ThresholdMode, get_catalog
from engine.contracts import (
    BaselineComparison,
    BaselineMetric,
    ConfusionMatrix,
    DecileBin,
    DecileLift,
    EvaluationReport,
    FairnessGroup,
    FairnessReport,
    MetricValue,
)
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Literal

    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.config import EvaluationConfig
    from engine.contracts import CalibrationSummary

    FloatArray = npt.NDArray[np.float64]
    BoolArray = npt.NDArray[np.bool_]
    IndexArray = npt.NDArray[np.intp]
    ChartUnit = Literal["x", "%"]


__all__ = [
    "ClassificationScorer",
    "EvaluationError",
    "RegressionScorer",
    "Scorer",
    "compare_to_baseline",
    "evaluate",
]


ROUND_TO: int = 4
"""Decimal places every float destined for the UI is rounded to."""

DECILES: int = 10
"""Bins the lift table asks for; fewer rows than this means fewer bins, never padded ones."""

MIN_FAIRNESS_GROUP_ROWS: int = 10
"""Rows a group needs before it may widen a fairness gap. The plan is silent; this is our floor."""

MISSING_GROUP_LABEL: str = "(missing)"
"""Group name for rows whose sensitive column is null."""

DEFAULT_BASELINE_NAME: str = "baseline (logistic regression)"
"""Display name of the baseline in the model-versus-baseline table, as the Model page prints it."""

_FAIRNESS_NOTE: str = "Reported only; fairness never blocks a run."


class EvaluationError(Exception):
    """Evaluation cannot produce an honest artefact. `code` is machine-readable, `message` human."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# What a fitted model has to look like from here
# ---------------------------------------------------------------------------
@runtime_checkable
class Scorer(Protocol):
    """Everything this stage is allowed to know about a fitted model.

    The model carries its own decision threshold and its own calibration, both settled on the
    validation split at train time, so evaluation never selects anything on the hold-out. It also
    names its target column and, for classification, its two classes in `(negative, positive)`
    order - the same order `sklearn`'s `classes_` uses.
    """

    problem_type: ProblemType
    target_column: str
    primary_metric: Metric
    threshold: float
    threshold_mode: ThresholdMode
    threshold_detail: str
    calibration: CalibrationSummary | None
    classes: tuple[object, object] | None


@runtime_checkable
class ClassificationScorer(Scorer, Protocol):
    """A scorer that serves calibrated probabilities."""

    def predict_proba(self, frame: pd.DataFrame) -> npt.ArrayLike:
        """Probabilities for `frame`: either one column of positive-class scores or sklearn's two."""


@runtime_checkable
class RegressionScorer(Scorer, Protocol):
    """A scorer that serves predicted values."""

    def predict(self, frame: pd.DataFrame) -> npt.ArrayLike:
        """One predicted number per row of `frame`."""


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _round(value: float) -> float:
    """Round one float to the artefact's four decimal places."""
    return round(float(value), ROUND_TO)


def _round_or_none(value: float | None) -> float | None:
    """Round a float that may legitimately have no value."""
    return None if value is None else _round(value)


def _finite_floats(values: npt.ArrayLike, what: str) -> FloatArray:
    """An array as float64, refusing anything that is not numeric or holds a NaN."""
    import numpy as np

    try:
        array = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise EvaluationError("EVAL_SCORES_NOT_NUMERIC", f"The {what} are not numbers.") from exc
    if array.size and not bool(np.isfinite(array).all()):
        raise EvaluationError("EVAL_SCORES_NOT_FINITE", f"The {what} contain a missing or infinite value.")
    return array


def _positive_class_scores(raw: npt.ArrayLike, rows: int) -> FloatArray:
    """One positive-class probability per row, from either a plain vector or sklearn's two columns."""
    array = _finite_floats(raw, "scores the model returned")
    if array.ndim == 2 and array.shape[1] == 2:
        array = array[:, 1]
    elif array.ndim == 2 and array.shape[1] == 1:
        array = array[:, 0]
    elif array.ndim != 1:
        raise EvaluationError(
            "EVAL_SCORES_SHAPE",
            f"The model returned probabilities shaped {array.shape}; one column of positive-class "
            "scores or two columns of class probabilities were expected.",
        )
    if array.shape[0] != rows:
        raise EvaluationError(
            "EVAL_SCORES_SHAPE",
            f"The model returned {array.shape[0]} scores for {rows} test rows.",
        )
    return array


def _predicted_values(raw: npt.ArrayLike, rows: int) -> FloatArray:
    """One predicted number per row, for a regression scorer."""
    array = _finite_floats(raw, "values the model returned")
    if array.ndim == 2 and array.shape[1] == 1:
        array = array[:, 0]
    if array.ndim != 1 or array.shape[0] != rows:
        raise EvaluationError(
            "EVAL_SCORES_SHAPE",
            f"The model returned predictions shaped {array.shape} for {rows} test rows.",
        )
    return array


def _class_labels(scorer: Scorer, outcomes: pd.Series) -> tuple[object, str, str]:
    """The positive class value plus the two display labels the confusion matrix prints."""
    import pandas as pd

    if scorer.classes is None:
        raise EvaluationError(
            "EVAL_CLASSES_UNKNOWN",
            "The model does not name its two classes, so there is no way to tell which outcome "
            "counts as positive.",
        )
    negative, positive = scorer.classes
    known = {str(negative), str(positive)}
    seen = {str(value) for value in pd.unique(outcomes.to_numpy()) if not pd.isna(value)}
    unknown = sorted(seen - known)
    if unknown:
        raise EvaluationError(
            "EVAL_TARGET_NOT_BINARY",
            f"The test split holds outcome value(s) {', '.join(unknown)} that the model does not "
            f"know; its classes are {negative!s} and {positive!s}.",
        )
    return positive, str(positive), str(negative)


def _binarise(series: pd.Series, positive: object) -> BoolArray:
    """True where the row is the positive class, comparing as-is and then as text."""
    import numpy as np

    matched = np.asarray(series.to_numpy() == positive, dtype=np.bool_)
    if not bool(matched.any()):
        as_text = np.asarray(series.astype(str).to_numpy() == str(positive), dtype=np.bool_)
        if bool(as_text.any()):
            return as_text
    return matched


def _brier(actual: BoolArray, scores: FloatArray) -> float | None:
    """Mean squared error of a probability against a 0/1 outcome; null for an empty split."""
    import numpy as np

    if scores.size == 0:
        return None
    return float(np.mean((scores - actual.astype(np.float64)) ** 2))


def _threshold_detail(scorer: Scorer, threshold: float) -> str:
    """The line the Model page prints about the threshold, written for a scorer that left it blank."""
    given = scorer.threshold_detail.strip()
    if given:
        return given
    wording = {
        ThresholdMode.AUTO: "Auto (maximises F1 on validation)",
        ThresholdMode.FIXED: "Fixed",
        ThresholdMode.MANUAL: "Manual",
    }[scorer.threshold_mode]
    return f"{wording}: {threshold:.2f}"


# ---------------------------------------------------------------------------
# Classification metrics and the confusion matrix
# ---------------------------------------------------------------------------
def _counts(actual: BoolArray, predicted: BoolArray) -> tuple[int, int, int, int]:
    """(true positive, false negative, false positive, true negative), the contract's cell order."""
    import numpy as np

    return (
        int(np.count_nonzero(actual & predicted)),
        int(np.count_nonzero(actual & ~predicted)),
        int(np.count_nonzero(~actual & predicted)),
        int(np.count_nonzero(~actual & ~predicted)),
    )


def _rate(numerator: int, denominator: int) -> float | None:
    """A share, or null when the denominator is empty and the share therefore has no value."""
    return None if denominator == 0 else numerator / denominator


def _classification_metrics(
    actual: BoolArray, scores: FloatArray, predicted: BoolArray
) -> dict[Metric, float]:
    """Every classification metric that is defined on this split; undefined ones are absent."""
    import numpy as np

    values: dict[Metric, float] = {}
    classes = len(np.unique(actual))
    positives = int(np.count_nonzero(actual))
    if classes == 2:
        from sklearn.metrics import roc_auc_score

        values[Metric.ROC_AUC] = float(roc_auc_score(actual, scores))
    if positives > 0:
        from sklearn.metrics import average_precision_score

        values[Metric.PR_AUC] = float(average_precision_score(actual, scores))
    true_positive, false_negative, false_positive, _ = _counts(actual, predicted)
    precision = _rate(true_positive, true_positive + false_positive)
    recall = _rate(true_positive, true_positive + false_negative)
    f1 = _rate(2 * true_positive, 2 * true_positive + false_positive + false_negative)
    if f1 is not None:
        values[Metric.F1] = f1
    if recall is not None:
        values[Metric.RECALL] = recall
    if precision is not None:
        values[Metric.PRECISION] = precision
    return values


def _confusion_matrix(
    run_id: str,
    actual: BoolArray,
    predicted: BoolArray,
    *,
    threshold: float,
    positive_label: str,
    negative_label: str,
) -> ConfusionMatrix:
    """The four-cell grid. Its four rates use the zero-division convention of 0.0 (see the table)."""
    true_positive, false_negative, false_positive, true_negative = _counts(actual, predicted)
    precision = _rate(true_positive, true_positive + false_positive) or 0.0
    recall = _rate(true_positive, true_positive + false_negative) or 0.0
    specificity = _rate(true_negative, true_negative + false_positive) or 0.0
    f1 = _rate(2 * true_positive, 2 * true_positive + false_positive + false_negative) or 0.0
    return ConfusionMatrix(
        run_id=run_id,
        threshold=threshold,
        positive_label=positive_label,
        negative_label=negative_label,
        true_positive=true_positive,
        false_negative=false_negative,
        false_positive=false_positive,
        true_negative=true_negative,
        total=true_positive + false_negative + false_positive + true_negative,
        cells=(true_positive, false_negative, false_positive, true_negative),
        precision=_round(precision),
        recall=_round(recall),
        specificity=_round(specificity),
        f1=_round(f1),
    )


# ---------------------------------------------------------------------------
# Regression metrics
# ---------------------------------------------------------------------------
def _regression_metrics(actual: FloatArray, predicted: FloatArray) -> dict[Metric, float]:
    """RMSE and MAE. Both need at least one row; the caller guarantees that."""
    import numpy as np

    residual = predicted - actual
    return {
        Metric.RMSE: float(np.sqrt(np.mean(residual**2))),
        Metric.MAE: float(np.mean(np.abs(residual))),
    }


def _residual_summary(actual: FloatArray, predicted: FloatArray) -> dict[str, float]:
    """R-squared plus the residual distribution, the only place the contract has for them."""
    import numpy as np

    residual = actual - predicted
    quantiles = np.quantile(residual, [0.05, 0.25, 0.50, 0.75, 0.95])
    summary: dict[str, float] = {
        "residual_mean": _round(float(np.mean(residual))),
        "residual_std": _round(float(np.std(residual))),
        "residual_min": _round(float(np.min(residual))),
        "residual_p05": _round(float(quantiles[0])),
        "residual_p25": _round(float(quantiles[1])),
        "residual_median": _round(float(quantiles[2])),
        "residual_p75": _round(float(quantiles[3])),
        "residual_p95": _round(float(quantiles[4])),
        "residual_max": _round(float(np.max(residual))),
    }
    total_variance = float(np.sum((actual - np.mean(actual)) ** 2))
    if total_variance > 0.0:
        summary["r2"] = _round(1.0 - float(np.sum(residual**2)) / total_variance)
    return summary


# ---------------------------------------------------------------------------
# Decile lift
# ---------------------------------------------------------------------------
def _bin_sizes(rows: int) -> list[int]:
    """Equal-sized rank bins, largest first, differing by at most one row."""
    if rows == 0:
        return []
    count = min(DECILES, rows)
    base, remainder = divmod(rows, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _decile_order(scores: FloatArray) -> IndexArray:
    """Row positions sorted by descending score; ties keep their original order (stable)."""
    import numpy as np

    return np.argsort(-scores, kind="stable")


def _classification_deciles(
    scores: FloatArray, actual: BoolArray, base_rate: float
) -> tuple[list[DecileBin], list[float | None]]:
    """One bin per decile with positives, actual rate, lift, cumulative lift and capture."""
    import numpy as np

    order = _decile_order(scores)
    total_positives = int(np.count_nonzero(actual))
    bins: list[DecileBin] = []
    lifts: list[float | None] = []
    start = 0
    seen_rows = 0
    seen_positives = 0
    for index, size in enumerate(_bin_sizes(scores.size), start=1):
        rows = order[start : start + size]
        start += size
        bin_scores = scores[rows]
        positives = int(np.count_nonzero(actual[rows]))
        seen_rows += size
        seen_positives += positives
        rate = positives / size
        lift = rate / base_rate if base_rate > 0.0 else None
        cumulative_rate = seen_positives / seen_rows
        cumulative_lift = cumulative_rate / base_rate if base_rate > 0.0 else None
        capture = 100.0 * seen_positives / total_positives if total_positives > 0 else None
        lifts.append(_round_or_none(lift))
        bins.append(
            DecileBin(
                decile=index,
                label=f"D{index}",
                rows=size,
                score_min=_round(float(np.min(bin_scores))),
                score_max=_round(float(np.max(bin_scores))),
                mean_score=_round(float(np.mean(bin_scores))),
                positives=positives,
                actual_rate_pct=_round(100.0 * rate),
                lift=_round_or_none(lift),
                cumulative_lift=_round_or_none(cumulative_lift),
                cumulative_capture_pct=_round_or_none(capture),
            )
        )
    return bins, lifts


def _regression_deciles(
    scores: FloatArray, actual: FloatArray, overall_mean: float
) -> tuple[list[DecileBin], list[float | None]]:
    """One bin per decile with the actual and predicted means, and actual mean over overall mean."""
    import numpy as np

    order = _decile_order(scores)
    total_actual = float(np.sum(actual))
    bins: list[DecileBin] = []
    lifts: list[float | None] = []
    start = 0
    seen_rows = 0
    seen_actual = 0.0
    for index, size in enumerate(_bin_sizes(scores.size), start=1):
        rows = order[start : start + size]
        start += size
        bin_scores = scores[rows]
        bin_actual = actual[rows]
        actual_mean = float(np.mean(bin_actual))
        seen_rows += size
        seen_actual += float(np.sum(bin_actual))
        lift = actual_mean / overall_mean if overall_mean > 0.0 else None
        cumulative_lift = (seen_actual / seen_rows) / overall_mean if overall_mean > 0.0 else None
        capture = 100.0 * seen_actual / total_actual if total_actual > 0.0 else None
        lifts.append(_round_or_none(lift))
        bins.append(
            DecileBin(
                decile=index,
                label=f"D{index}",
                rows=size,
                score_min=_round(float(np.min(bin_scores))),
                score_max=_round(float(np.max(bin_scores))),
                mean_score=_round(float(np.mean(bin_scores))),
                actual_mean=_round(actual_mean),
                predicted_mean=_round(float(np.mean(bin_scores))),
                lift=_round_or_none(lift),
                cumulative_lift=_round_or_none(cumulative_lift),
                cumulative_capture_pct=_round_or_none(capture),
            )
        )
    return bins, lifts


def _short_table_note(rows: int, bins: int) -> str:
    """The sentence that admits the table has fewer than ten bins, or nothing when it has ten."""
    return "" if bins == DECILES else f" Only {rows} test rows, so the table has {bins} bins."


def _classification_lift(
    run_id: str, scores: FloatArray, actual: BoolArray, computed_at: datetime
) -> DecileLift:
    """The Output page's chart for a classification run: lift per decile, or rates when it cannot."""
    import numpy as np

    base_rate = float(np.mean(actual.astype(np.float64)))
    base_rate_pct = _round(100.0 * base_rate)
    bins, lifts = _classification_deciles(scores, actual, base_rate)
    note = _short_table_note(scores.size, len(bins))
    values: tuple[float, ...]
    unit: ChartUnit
    if base_rate > 0.0:
        values = tuple(value for value in lifts if value is not None)
        unit = "x"
        caption = (
            f"Actual positive rate versus the overall rate of {base_rate_pct:g}%, as a multiple. "
            f"D1 holds the highest scores.{note}"
        )
    else:
        values = tuple(0.0 if bin_.actual_rate_pct is None else bin_.actual_rate_pct for bin_ in bins)
        unit = "%"
        caption = (
            "No positives in the test split, so lift has no base rate to divide by; the bars show "
            f"the actual positive rate per decile. D1 holds the highest scores.{note}"
        )
    return DecileLift(
        run_id=run_id,
        mode="classification",
        base_rate_pct=base_rate_pct,
        bins=tuple(bins),
        values=values,
        unit=unit,
        caption=caption,
        computed_at=computed_at,
    )


def _regression_lift(
    run_id: str, scores: FloatArray, actual: FloatArray, computed_at: datetime
) -> DecileLift:
    """The Output page's chart for a regression run: actual mean over the overall actual mean."""
    import numpy as np

    overall_mean = float(np.mean(actual))
    bins, lifts = _regression_deciles(scores, actual, overall_mean)
    note = _short_table_note(scores.size, len(bins))
    values: tuple[float, ...]
    if overall_mean > 0.0:
        values = tuple(value for value in lifts if value is not None)
        caption = (
            f"Actual mean versus the overall mean of {_round(overall_mean):g}, as a multiple. "
            f"D1 holds the highest predicted values.{note}"
        )
    else:
        values = ()
        caption = (
            "The overall actual mean is not positive, so actual-versus-overall has no meaning and "
            f"the bars are left undrawn; the table below still holds every decile.{note}"
        )
    return DecileLift(
        run_id=run_id,
        mode="regression",
        base_rate_pct=None,
        bins=tuple(bins),
        values=values,
        unit="x",
        caption=caption,
        computed_at=computed_at,
    )


# ---------------------------------------------------------------------------
# Fairness
# ---------------------------------------------------------------------------
def _group_labels(groups: pd.Series) -> list[str]:
    """The sensitive column as display strings, with nulls collected under one visible label."""
    import pandas as pd

    return [MISSING_GROUP_LABEL if pd.isna(value) else str(value) for value in groups.to_numpy()]


def _fairness_group(value: str, actual: BoolArray, predicted: BoolArray) -> FairnessGroup:
    """Positive rate, recall and precision inside one group."""
    import numpy as np

    rows = int(actual.size)
    predicted_positive = int(np.count_nonzero(predicted))
    true_positive = int(np.count_nonzero(actual & predicted))
    actual_positive = int(np.count_nonzero(actual))
    return FairnessGroup(
        value=value,
        rows=rows,
        positive_rate=_round(predicted_positive / rows),
        recall=_round_or_none(_rate(true_positive, actual_positive)),
        precision=_round_or_none(_rate(true_positive, predicted_positive)),
    )


def _max_gap(values: list[float]) -> float | None:
    """The spread between the best and worst group, or null when fewer than two groups qualify."""
    return None if len(values) < 2 else _round(max(values) - min(values))


def _not_evaluated(run_id: str, column: str | None, reason: str) -> FairnessReport:
    """A fairness report that says, in one line, why there is nothing to show."""
    return FairnessReport(
        run_id=run_id,
        column=column,
        evaluated=False,
        reason_not_evaluated=reason,
        groups=(),
        max_positive_rate_gap=None,
        max_recall_gap=None,
        max_precision_gap=None,
        note=_FAIRNESS_NOTE,
    )


def _fairness_report(
    run_id: str,
    column: str | None,
    problem_type: ProblemType,
    test_frame: pd.DataFrame,
    actual: BoolArray,
    predicted: BoolArray,
) -> FairnessReport:
    """Per-group positive rate, recall and precision plus the max gaps. Never blocks."""
    import numpy as np

    if column is None:
        return _not_evaluated(run_id, None, "No sensitive column was configured.")
    if problem_type is not ProblemType.BINARY_CLASSIFICATION:
        return _not_evaluated(run_id, column, "Fairness statistics are defined for classification runs only.")
    if column not in test_frame.columns:
        return _not_evaluated(run_id, column, f"The sensitive column {column!r} was not in the test split.")
    labels = _group_labels(test_frame[column])
    distinct = sorted(set(labels))
    if len(distinct) < 2:
        return _not_evaluated(
            run_id,
            column,
            f"The sensitive column {column!r} has one group in the test split, so there is nothing "
            "to compare.",
        )
    label_array = np.asarray(labels, dtype=object)
    reported = [
        _fairness_group(value, actual[label_array == value], predicted[label_array == value])
        for value in distinct
    ]
    reported.sort(key=lambda group: (-group.rows, group.value))
    large = [group for group in reported if group.rows >= MIN_FAIRNESS_GROUP_ROWS]
    return FairnessReport(
        run_id=run_id,
        column=column,
        evaluated=True,
        reason_not_evaluated=None,
        groups=tuple(reported),
        max_positive_rate_gap=_max_gap([group.positive_rate for group in large]),
        max_recall_gap=_max_gap([g.recall for g in large if g.recall is not None]),
        max_precision_gap=_max_gap([g.precision for g in large if g.precision is not None]),
        note=_FAIRNESS_NOTE,
    )


# ---------------------------------------------------------------------------
# Baseline, as a pure composition of two measurements
# ---------------------------------------------------------------------------
def compare_to_baseline(
    model_report: EvaluationReport,
    baseline_report: EvaluationReport,
    *,
    baseline_name: str = DEFAULT_BASELINE_NAME,
    baseline_family: ModelFamily = ModelFamily.LOGISTIC_REGRESSION,
) -> BaselineComparison:
    """`baseline.json` from two reports this stage produced on the same held-out frame.

    Nothing is fitted here. The caller evaluates the baseline scorer through `evaluate`, exactly as
    it evaluates the model, and hands both reports in; this function only lines them up. Both must
    have measured the same problem, the same primary metric and the same number of rows, or the
    table would silently compare two different hold-outs.
    """
    if model_report.problem_type is not baseline_report.problem_type:
        raise EvaluationError(
            "EVAL_BASELINE_MISMATCH",
            f"The model was measured as a {model_report.problem_type.value} problem and the "
            f"baseline as a {baseline_report.problem_type.value} one.",
        )
    if model_report.primary_metric is not baseline_report.primary_metric:
        raise EvaluationError(
            "EVAL_BASELINE_MISMATCH",
            f"The model's headline metric is {model_report.primary_metric_label} and the "
            f"baseline's is {baseline_report.primary_metric_label}.",
        )
    if model_report.rows_evaluated != baseline_report.rows_evaluated:
        raise EvaluationError(
            "EVAL_BASELINE_MISMATCH",
            f"The model was measured on {model_report.rows_evaluated} rows and the baseline on "
            f"{baseline_report.rows_evaluated}; a comparison needs one held-out set.",
        )
    baseline_values = {item.id: item.value for item in baseline_report.metrics}
    rows: list[BaselineMetric] = []
    for item in model_report.metrics:
        baseline_value = baseline_values.get(item.id)
        if baseline_value is None:
            delta: float | None = None
            better: bool | None = None
        else:
            delta = _round(item.value - baseline_value)
            better = item.value > baseline_value if item.greater_is_better else item.value < baseline_value
        rows.append(
            BaselineMetric(
                id=item.id,
                label=item.label,
                model_value=item.value,
                baseline_value=baseline_value,
                delta=delta,
                model_better=better,
            )
        )
    primary_row = next((row for row in rows if row.id is model_report.primary_metric), None)
    return BaselineComparison(
        run_id=model_report.run_id,
        baseline_name=baseline_name,
        baseline_family=baseline_family,
        rows=tuple(rows),
        model_beats_baseline=bool(primary_row is not None and primary_row.model_better),
    )


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------
def evaluate(
    scorer: Scorer,
    test_frame: pd.DataFrame,
    evaluation_config: EvaluationConfig,
    *,
    run_id: str,
    now: datetime | None = None,
) -> tuple[EvaluationReport, ConfusionMatrix | None, DecileLift, FairnessReport]:
    """Measure `scorer` on `test_frame` once, for the decision, and describe what was measured.

    FINAL-DECISION-ONLY: the frame handed in is the hold-out. Nothing here chooses a threshold, a
    calibrator, a model or a hyperparameter from it - the scorer arrives with all of those already
    settled on the validation split. Point this at any model and any held-out frame and the numbers
    are comparable, which is what re-scoring a champion against a challenger needs.

    `evaluation_config` contributes the sensitive column for the fairness table; the threshold and
    calibration blocks describe the policy that produced the scorer at train time and are
    deliberately not re-applied here. Returns the report, the confusion matrix (null for a
    regression run, which has no classes to confuse), the decile table and the fairness report.
    """
    import numpy as np

    catalog = get_catalog()
    evaluated_at = utc_now() if now is None else now
    rows = int(test_frame.shape[0])
    if rows == 0:
        raise EvaluationError("EVAL_EMPTY_TEST_SPLIT", "The test split has no rows to evaluate.")
    if scorer.target_column not in test_frame.columns:
        raise EvaluationError(
            "EVAL_TARGET_COLUMN_MISSING",
            f"The test split has no column {scorer.target_column!r} to score against.",
        )
    outcomes = test_frame[scorer.target_column]
    features = test_frame.drop(columns=[scorer.target_column])
    primary = scorer.primary_metric
    metric_order = catalog.metrics_for(scorer.problem_type)
    if primary not in metric_order:
        raise EvaluationError(
            "EVAL_METRIC_NOT_FOR_PROBLEM",
            f"{catalog.metric_label(primary)} cannot score a {scorer.problem_type.value} problem.",
        )
    # The scorer's own threshold, settled on validation: read, never re-chosen.
    threshold = _round(scorer.threshold)

    if scorer.problem_type is ProblemType.REGRESSION:
        if not isinstance(scorer, RegressionScorer):
            raise EvaluationError(
                "EVAL_SCORER_INCOMPLETE", "A regression model must expose a predict method."
            )
        actual_values = _finite_floats(outcomes.to_numpy(), "test outcomes")
        predictions = _predicted_values(scorer.predict(features), rows)
        model_metrics = _regression_metrics(actual_values, predictions)
        extra = _residual_summary(actual_values, predictions)
        lift = _regression_lift(run_id, predictions, actual_values, evaluated_at)
        matrix: ConfusionMatrix | None = None
        positive_rate: float | None = None
        no_classes = np.zeros(rows, dtype=np.bool_)
        fairness = _fairness_report(
            run_id, evaluation_config.fairness_column, scorer.problem_type, test_frame, no_classes, no_classes
        )
    else:
        if not isinstance(scorer, ClassificationScorer):
            raise EvaluationError(
                "EVAL_SCORER_INCOMPLETE", "A classification model must expose a predict_proba method."
            )
        positive, positive_label, negative_label = _class_labels(scorer, outcomes)
        actual = _binarise(outcomes, positive)
        scores = _positive_class_scores(scorer.predict_proba(features), rows)
        predicted = np.asarray(scores >= threshold, dtype=np.bool_)
        model_metrics = _classification_metrics(actual, scores, predicted)
        extra = {"accuracy": _round(float(np.mean(actual == predicted)))}
        measured_brier = _round_or_none(_brier(actual, scores))
        if measured_brier is not None:
            extra["brier_score"] = measured_brier
        matrix = _confusion_matrix(
            run_id,
            actual,
            predicted,
            threshold=threshold,
            positive_label=positive_label,
            negative_label=negative_label,
        )
        extra["specificity"] = matrix.specificity
        lift = _classification_lift(run_id, scores, actual, evaluated_at)
        positive_rate = _round(float(np.mean(actual.astype(np.float64))))
        fairness = _fairness_report(
            run_id, evaluation_config.fairness_column, scorer.problem_type, test_frame, actual, predicted
        )

    if primary not in model_metrics:
        raise EvaluationError(
            "EVAL_PRIMARY_METRIC_UNDEFINED",
            f"{catalog.metric_label(primary)} has no value on this test split, so the run has no "
            "headline score to report.",
        )
    metrics = tuple(
        MetricValue(
            id=metric,
            label=catalog.metric_label(metric),
            value=_round(model_metrics[metric]),
            greater_is_better=catalog.metrics[metric].greater_is_better,
        )
        for metric in metric_order
        if metric in model_metrics
    )
    report = EvaluationReport(
        run_id=run_id,
        problem_type=scorer.problem_type,
        rows_evaluated=rows,
        positive_rate=positive_rate,
        primary_metric=primary,
        primary_metric_label=catalog.metric_label(primary),
        headline_score=_round(model_metrics[primary]),
        metrics=metrics,
        extra_metrics=extra,
        threshold=threshold,
        threshold_mode=scorer.threshold_mode,
        threshold_detail=_threshold_detail(scorer, threshold),
        calibration=scorer.calibration,
        evaluated_at=evaluated_at,
    )
    return report, matrix, lift, fairness
