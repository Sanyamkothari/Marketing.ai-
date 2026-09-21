"""`engine.stages.evaluate`: the test-split measurement, its edge cases and its arithmetic.

Every expected number in this file is worked out independently of the implementation - by hand from
the definition of the metric, or from a deliberately different method - so a shared mistake cannot
make a test pass. The hand-worked numbers are spelled out in the comment above each assertion.

The stage is a pure measurement function, so the test double is a canned scorer: it carries the
threshold, the calibration summary and the classes, and serves scores that the test chose. That is
the whole point of the signature - nothing is fitted here, so nothing has to be trained to test it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.config import Calibration, EvaluationConfig, Metric, ProblemType, ThresholdMode
from engine.contracts import CalibrationSummary, EvaluationReport, dump_artefact
from engine.stages.evaluate import (
    DECILES,
    MIN_FAIRNESS_GROUP_ROWS,
    MISSING_GROUP_LABEL,
    ClassificationScorer,
    EvaluationError,
    RegressionScorer,
    Scorer,
    _bin_sizes,
    _classification_deciles,
    _decile_order,
    compare_to_baseline,
    evaluate,
)

FIXED_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
RUN_ID = "r_20260921_0000beef"
TARGET = "outcome"

# The small fixed classification fixture every hand-computed metric below refers to.
SCORES = [0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20, 0.10, 0.05]
LABELS = [1, 1, 0, 1, 0, 1, 0, 0, 0, 0]


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
@dataclass
class FakeClassifier:
    """A scorer that serves the probabilities the test chose, and nothing else."""

    scores: list[float]
    primary_metric: Metric = Metric.ROC_AUC
    threshold: float = 0.50
    threshold_mode: ThresholdMode = ThresholdMode.FIXED
    threshold_detail: str = "Fixed: 0.50"
    calibration: CalibrationSummary | None = None
    classes: tuple[object, object] | None = (0, 1)
    target_column: str = TARGET
    problem_type: ProblemType = ProblemType.BINARY_CLASSIFICATION
    two_columns: bool = False
    seen: list[pd.DataFrame] = field(default_factory=list)

    def predict_proba(self, frame: pd.DataFrame) -> Any:
        self.seen.append(frame)
        try:
            positive = np.asarray(self.scores, dtype=float)
        except (TypeError, ValueError):
            return np.asarray(self.scores, dtype=object)  # a model that served nonsense
        if self.two_columns:
            return np.column_stack([1.0 - positive, positive])
        return positive


@dataclass
class FakeRegressor:
    """A scorer that serves the predicted values the test chose."""

    values: list[float]
    primary_metric: Metric = Metric.RMSE
    threshold: float = 0.50
    threshold_mode: ThresholdMode = ThresholdMode.FIXED
    threshold_detail: str = "No decision threshold: a regression run predicts a number"
    calibration: CalibrationSummary | None = None
    classes: tuple[object, object] | None = None
    target_column: str = TARGET
    problem_type: ProblemType = ProblemType.REGRESSION

    def predict(self, frame: pd.DataFrame) -> Any:
        return np.asarray(self.values, dtype=float)


@dataclass
class ScorerWithoutAScoringCall:
    """Carries the metadata but neither scoring method, to exercise EVAL_SCORER_INCOMPLETE."""

    primary_metric: Metric = Metric.ROC_AUC
    threshold: float = 0.50
    threshold_mode: ThresholdMode = ThresholdMode.FIXED
    threshold_detail: str = "Fixed: 0.50"
    calibration: CalibrationSummary | None = None
    classes: tuple[object, object] | None = (0, 1)
    target_column: str = TARGET
    problem_type: ProblemType = ProblemType.BINARY_CLASSIFICATION


def frame(outcomes: list[Any], **extra: list[Any]) -> pd.DataFrame:
    """A test frame: the outcome column, a harmless feature and whatever else a test needs."""
    return pd.DataFrame({TARGET: outcomes, "feature": list(range(len(outcomes))), **extra})


def run(
    scorer: Any,
    test_frame: pd.DataFrame,
    config: EvaluationConfig | None = None,
) -> tuple[Any, Any, Any, Any]:
    return evaluate(
        scorer,
        test_frame,
        config if config is not None else EvaluationConfig(),
        run_id=RUN_ID,
        now=FIXED_NOW,
    )


def fixed_run(**scorer_kwargs: Any) -> tuple[Any, Any, Any, Any]:
    """The small fixed fixture, measured at the scorer's 0.5 threshold."""
    return run(FakeClassifier(scores=SCORES, **scorer_kwargs), frame(LABELS))


def metric_values(report: Any) -> dict[Metric, float]:
    return {item.id: item.value for item in report.metrics}


def report_for(scores: list[float], labels: list[int], **scorer_kwargs: Any) -> EvaluationReport:
    return run(FakeClassifier(scores=scores, **scorer_kwargs), frame(labels))[0]


# ---------------------------------------------------------------------------
# The protocol is the whole contract with the model
# ---------------------------------------------------------------------------
def test_the_scorer_protocols_are_runtime_checkable() -> None:
    classifier = FakeClassifier(scores=SCORES)
    regressor = FakeRegressor(values=[1.0])
    assert isinstance(classifier, Scorer)
    assert isinstance(classifier, ClassificationScorer)
    assert not isinstance(classifier, RegressionScorer)
    assert isinstance(regressor, RegressionScorer)
    assert not isinstance(regressor, ClassificationScorer)
    assert isinstance(ScorerWithoutAScoringCall(), Scorer)
    assert not isinstance(ScorerWithoutAScoringCall(), ClassificationScorer)


def test_the_target_column_is_taken_off_the_frame_before_the_model_sees_it() -> None:
    scorer = FakeClassifier(scores=SCORES)
    run(scorer, frame(LABELS, region=["A"] * 10))
    assert len(scorer.seen) == 1
    assert TARGET not in scorer.seen[0].columns
    # Everything else the frame carried is handed through untouched; the model selects what it needs.
    assert list(scorer.seen[0].columns) == ["feature", "region"]
    assert scorer.seen[0].shape[0] == 10


def test_sklearn_style_two_column_probabilities_are_read_from_the_positive_column() -> None:
    one_column, _, _, _ = fixed_run()
    two_column, _, _, _ = fixed_run(two_columns=True)
    assert metric_values(two_column) == metric_values(one_column)


# ---------------------------------------------------------------------------
# Metrics, hand-computed on the fixed fixture
# ---------------------------------------------------------------------------
def test_roc_auc_is_the_share_of_correctly_ordered_positive_negative_pairs() -> None:
    report, _, _, _ = fixed_run()
    # 4 positives (0.9, 0.8, 0.6, 0.4) x 6 negatives (0.7, 0.5, 0.3, 0.2, 0.1, 0.05) = 24 pairs.
    # 0.9 beats 6, 0.8 beats 6, 0.6 beats 5, 0.4 beats 4 -> 21 of 24 = 0.875.
    assert metric_values(report)[Metric.ROC_AUC] == pytest.approx(0.875)


def test_pr_auc_is_the_step_sum_of_precision_at_each_recall_gain() -> None:
    report, _, _, _ = fixed_run()
    # Walking the ranking: 1/1 at r=.25, 2/2 at r=.50, 3/4 at r=.75, 4/6 at r=1.0, each step +0.25:
    # 0.25*(1 + 1 + 0.75 + 2/3) = 0.8541666...
    assert metric_values(report)[Metric.PR_AUC] == pytest.approx(0.8542, abs=5e-5)


def test_precision_recall_and_f1_come_from_the_four_counts_at_the_threshold() -> None:
    report, matrix, _, _ = fixed_run()
    values = metric_values(report)
    # At 0.5 the top five rows are predicted positive: labels 1,1,0,1,0 -> tp=3, fp=2; below: fn=1, tn=4.
    assert (matrix.true_positive, matrix.false_negative, matrix.false_positive, matrix.true_negative) == (
        3,
        1,
        2,
        4,
    )
    assert values[Metric.PRECISION] == pytest.approx(0.6)  # 3 / (3 + 2)
    assert values[Metric.RECALL] == pytest.approx(0.75)  # 3 / (3 + 1)
    assert values[Metric.F1] == pytest.approx(0.6667, abs=5e-5)  # 6 / (6 + 2 + 1)


def test_the_confusion_matrix_is_the_four_counts_in_render_order_with_its_own_rates() -> None:
    _, matrix, _, _ = fixed_run()
    assert matrix.cells == (3, 1, 2, 4)
    assert matrix.total == 10
    assert matrix.threshold == 0.50
    assert matrix.specificity == pytest.approx(0.6667, abs=5e-5)  # tn 4 / (tn 4 + fp 2)
    assert matrix.f1 == pytest.approx(0.6667, abs=5e-5)


def test_accuracy_specificity_and_the_measured_brier_score_ride_along_in_extra_metrics() -> None:
    report, _, _, _ = fixed_run()
    assert report.extra_metrics["accuracy"] == pytest.approx(0.7)  # (3 + 4) / 10
    assert report.extra_metrics["specificity"] == pytest.approx(0.6667, abs=5e-5)  # tn 4 / (4 + 2)
    # Brier: mean of (.01,.04,.49,.16,.25,.36,.09,.04,.01,.0025) = 1.4525 / 10.
    assert report.extra_metrics["brier_score"] == pytest.approx(0.14525, abs=1e-4)
    assert report.positive_rate == pytest.approx(0.4)  # 4 positives of 10
    assert report.rows_evaluated == 10


def test_metrics_arrive_in_catalog_order_with_catalog_labels() -> None:
    report, _, _, _ = fixed_run()
    assert [item.id for item in report.metrics] == [
        Metric.ROC_AUC,
        Metric.PR_AUC,
        Metric.F1,
        Metric.RECALL,
        Metric.PRECISION,
    ]
    assert [item.label for item in report.metrics] == [
        "ROC-AUC",
        "PR-AUC",
        "F1 score",
        "Recall",
        "Precision",
    ]
    assert all(item.greater_is_better for item in report.metrics)
    assert report.headline_score == report.metrics[0].value
    assert report.primary_metric_label == "ROC-AUC"


def test_the_headline_follows_the_metric_the_scorer_was_optimised_for() -> None:
    report, _, _, _ = fixed_run(primary_metric=Metric.RECALL)
    assert report.primary_metric is Metric.RECALL
    assert report.primary_metric_label == "Recall"
    assert report.headline_score == pytest.approx(0.75)


def test_every_float_that_reaches_the_ui_is_rounded_to_four_places() -> None:
    report, matrix, lift, _ = fixed_run()
    floats = [
        *(item.value for item in report.metrics),
        *report.extra_metrics.values(),
        report.headline_score,
        matrix.precision,
        matrix.recall,
        matrix.specificity,
        matrix.f1,
        *(value for bin_ in lift.bins for value in (bin_.score_min, bin_.mean_score, bin_.lift or 0.0)),
    ]
    assert all(value == round(value, 4) for value in floats)


# ---------------------------------------------------------------------------
# The threshold is read, never chosen: the test split is final-decision-only
# ---------------------------------------------------------------------------
def test_the_threshold_the_scorer_carries_is_the_one_that_is_measured_and_reported() -> None:
    report, matrix, _, _ = fixed_run(
        threshold=0.30,
        threshold_mode=ThresholdMode.MANUAL,
        threshold_detail="Manual: 0.30",
    )
    assert (report.threshold, report.threshold_mode) == (0.30, ThresholdMode.MANUAL)
    assert report.threshold_detail == "Manual: 0.30"
    # At 0.3 the top seven rows are predicted positive: labels 1,1,0,1,0,1,0 -> tp=4, fp=3, fn=0, tn=3.
    assert matrix.cells == (4, 0, 3, 3)
    assert matrix.threshold == 0.30


def test_an_auto_threshold_chosen_on_validation_is_passed_through_untouched() -> None:
    report, _, _, _ = fixed_run(
        threshold=0.43,
        threshold_mode=ThresholdMode.AUTO,
        threshold_detail="Auto (maximises F1 on validation): 0.43",
    )
    assert report.threshold == 0.43
    assert report.threshold_mode is ThresholdMode.AUTO
    assert report.threshold_detail == "Auto (maximises F1 on validation): 0.43"


def test_a_scorer_that_left_the_threshold_line_blank_still_gets_one() -> None:
    for mode, expected in (
        (ThresholdMode.AUTO, "Auto (maximises F1 on validation): 0.43"),
        (ThresholdMode.FIXED, "Fixed: 0.43"),
        (ThresholdMode.MANUAL, "Manual: 0.43"),
    ):
        report, _, _, _ = fixed_run(threshold=0.43, threshold_mode=mode, threshold_detail="  ")
        assert report.threshold_detail == expected


def test_the_evaluation_config_threshold_block_is_not_re_applied_to_the_hold_out() -> None:
    # The config says manual 0.90; the scorer says 0.50. The scorer wins, because choosing a
    # threshold here would be selecting on the final hold-out.
    config = EvaluationConfig.model_validate({"threshold": {"mode": "manual", "value": 0.90}})
    report, matrix, _, _ = run(FakeClassifier(scores=SCORES), frame(LABELS), config)
    assert report.threshold == 0.50
    assert matrix.cells == (3, 1, 2, 4)


# ---------------------------------------------------------------------------
# Calibration is reported, never fitted
# ---------------------------------------------------------------------------
def test_the_scorers_calibration_summary_is_passed_through_unchanged() -> None:
    summary = CalibrationSummary(method=Calibration.ISOTONIC, brier_before=0.2506, brier_after=0.1675)
    report, _, _, _ = fixed_run(calibration=summary)
    assert report.calibration == summary
    assert report.calibration is not None
    assert report.calibration.fitted_on == "validation"


def test_a_scorer_without_calibration_reports_none_but_still_measures_the_brier_score() -> None:
    report, _, _, _ = fixed_run(calibration=None)
    assert report.calibration is None
    assert report.extra_metrics["brier_score"] == pytest.approx(0.14525, abs=1e-4)


def test_nothing_is_measured_from_uncalibrated_scores_because_none_are_offered() -> None:
    # The scorer serves calibrated probabilities; every number comes from exactly those.
    calibrated = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95]
    report, matrix, lift, _ = run(
        FakeClassifier(scores=calibrated, classes=(0, 1)),
        frame([0, 0, 0, 0, 0, 1, 1, 1, 1, 1]),
    )
    assert matrix.cells == (5, 0, 0, 5)  # the top five are exactly the five positives
    assert [bin_.score_max for bin_ in lift.bins] == sorted(calibrated, reverse=True)
    assert metric_values(report)[Metric.ROC_AUC] == 1.0


# ---------------------------------------------------------------------------
# Decile construction, lift and capture
# ---------------------------------------------------------------------------
def test_ten_rows_give_ten_single_row_bins_with_hand_computed_lift() -> None:
    _, _, lift, _ = fixed_run()
    assert lift.mode == "classification"
    assert lift.unit == "x"
    assert lift.base_rate_pct == pytest.approx(40.0)
    assert [bin_.label for bin_ in lift.bins] == [f"D{i}" for i in range(1, 11)]
    assert [bin_.rows for bin_ in lift.bins] == [1] * 10
    # One row per bin, so the rate is 100% or 0%, and lift is that over the 40% base rate.
    assert [bin_.actual_rate_pct for bin_ in lift.bins] == [
        100.0,
        100.0,
        0.0,
        100.0,
        0.0,
        100.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    assert list(lift.values) == [2.5, 2.5, 0.0, 2.5, 0.0, 2.5, 0.0, 0.0, 0.0, 0.0]
    assert [bin_.lift for bin_ in lift.bins] == list(lift.values)


def test_cumulative_lift_and_capture_are_running_totals_over_the_deciles() -> None:
    _, _, lift, _ = fixed_run()
    # Positives seen after each decile: 1,2,2,3,3,4,4,4,4,4 of 4 in total.
    assert [bin_.cumulative_capture_pct for bin_ in lift.bins] == [
        25.0,
        50.0,
        50.0,
        75.0,
        75.0,
        100.0,
        100.0,
        100.0,
        100.0,
        100.0,
    ]
    # Cumulative rate over the 0.4 base rate: (1/1)/.4, (2/2)/.4, (2/3)/.4, (3/4)/.4 ...
    assert [bin_.cumulative_lift for bin_ in lift.bins] == [
        2.5,
        2.5,
        1.6667,
        1.875,
        1.5,
        1.6667,
        1.4286,
        1.25,
        1.1111,
        1.0,
    ]


def test_the_score_range_of_each_bin_is_reported() -> None:
    _, _, lift, _ = fixed_run()
    assert [bin_.score_max for bin_ in lift.bins] == SCORES
    assert [bin_.score_min for bin_ in lift.bins] == SCORES
    assert [bin_.mean_score for bin_ in lift.bins] == SCORES


def test_a_short_input_gets_one_bin_per_row_and_says_so() -> None:
    _, _, lift, _ = run(FakeClassifier(scores=[0.9, 0.6, 0.4, 0.1]), frame([1, 0, 1, 0]))
    assert len(lift.bins) == 4
    assert [bin_.rows for bin_ in lift.bins] == [1, 1, 1, 1]
    assert "4 bins" in lift.caption


def test_a_long_input_spreads_the_remainder_over_the_first_bins() -> None:
    scores = [(23 - index) / 23 for index in range(23)]
    labels = [index % 2 for index in range(23)]
    _, _, lift, _ = run(FakeClassifier(scores=scores), frame(labels))
    # 23 rows over 10 bins: three bins of 3 then seven of 2.
    assert [bin_.rows for bin_ in lift.bins] == [3, 3, 3, 2, 2, 2, 2, 2, 2, 2]
    assert sum(bin_.rows for bin_ in lift.bins) == 23
    assert "Only" not in lift.caption  # ten real bins, so no short-table note


def test_tied_scores_at_a_bin_boundary_keep_their_original_order() -> None:
    # The three middle rows are tied; a stable descending sort puts them in input order.
    _, _, lift, _ = run(FakeClassifier(scores=[0.9, 0.5, 0.5, 0.5, 0.1]), frame([1, 1, 0, 0, 1]))
    assert [bin_.positives for bin_ in lift.bins] == [1, 1, 0, 0, 1]
    assert [bin_.score_max for bin_ in lift.bins] == [0.9, 0.5, 0.5, 0.5, 0.1]


def test_all_scores_tied_still_partitions_every_row_exactly_once() -> None:
    labels = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
    _, _, lift, _ = run(FakeClassifier(scores=[0.5] * 10), frame(labels))
    assert sum(bin_.rows for bin_ in lift.bins) == 10
    assert [bin_.positives for bin_ in lift.bins] == labels


# ---------------------------------------------------------------------------
# Decile invariants (hypothesis)
# ---------------------------------------------------------------------------
score_lists = st.lists(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=140,
)


@settings(deadline=None, max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(scores=score_lists)
def test_bins_hold_every_row_exactly_once(scores: list[float]) -> None:
    array = np.asarray(scores, dtype=np.float64)
    order = _decile_order(array)
    sizes = _bin_sizes(array.size)
    assigned: list[int] = []
    start = 0
    for size in sizes:
        assigned.extend(int(position) for position in order[start : start + size])
        start += size
    assert sorted(assigned) == list(range(array.size))


@settings(deadline=None, max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(scores=score_lists)
def test_bin_sizes_differ_by_at_most_one_and_there_are_never_more_than_ten(
    scores: list[float],
) -> None:
    sizes = _bin_sizes(len(scores))
    assert len(sizes) == min(DECILES, len(scores))
    assert sum(sizes) == len(scores)
    assert max(sizes) - min(sizes) <= 1
    assert sizes == sorted(sizes, reverse=True)


@settings(deadline=None, max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(scores=score_lists)
def test_bins_are_ordered_by_score_with_the_highest_decile_first(scores: list[float]) -> None:
    array = np.asarray(scores, dtype=np.float64)
    actual = np.zeros(array.size, dtype=np.bool_)
    bins, _ = _classification_deciles(array, actual, 0.0)
    assert [bin_.decile for bin_ in bins] == list(range(1, len(bins) + 1))
    for higher, lower in pairwise(bins):
        assert higher.score_min >= lower.score_max
        assert higher.mean_score >= lower.mean_score


# ---------------------------------------------------------------------------
# The baseline comparison, a pure composition of two measurements
# ---------------------------------------------------------------------------
def test_the_baseline_table_lines_up_two_reports_of_the_same_hold_out() -> None:
    labels = [1, 1, 0, 0]
    model = report_for([0.9, 0.8, 0.2, 0.1], labels)  # perfect ordering
    baseline = report_for([0.1, 0.2, 0.8, 0.9], labels)  # exactly backwards
    comparison = compare_to_baseline(model, baseline)
    rows = {row.id: row for row in comparison.rows}
    assert rows[Metric.ROC_AUC].model_value == 1.0
    assert rows[Metric.ROC_AUC].baseline_value == 0.0
    assert rows[Metric.ROC_AUC].delta == 1.0
    assert rows[Metric.ROC_AUC].model_better is True
    assert comparison.model_beats_baseline is True
    assert comparison.run_id == RUN_ID
    assert comparison.baseline_name == "baseline (logistic regression)"
    assert comparison.baseline_family.value == "LogisticRegression"


def test_a_losing_model_is_reported_as_losing() -> None:
    labels = [1, 1, 0, 0]
    model = report_for([0.1, 0.2, 0.8, 0.9], labels)
    baseline = report_for([0.9, 0.8, 0.2, 0.1], labels)
    comparison = compare_to_baseline(model, baseline)
    rows = {row.id: row for row in comparison.rows}
    assert rows[Metric.ROC_AUC].delta == -1.0
    assert rows[Metric.ROC_AUC].model_better is False
    assert comparison.model_beats_baseline is False


def test_a_tie_on_the_primary_metric_is_not_a_win() -> None:
    model = report_for(SCORES, LABELS)
    baseline = report_for(SCORES, LABELS)
    comparison = compare_to_baseline(model, baseline)
    rows = {row.id: row for row in comparison.rows}
    assert rows[Metric.ROC_AUC].delta == 0.0
    assert rows[Metric.ROC_AUC].model_better is False
    assert comparison.model_beats_baseline is False


def test_lower_is_better_metrics_are_compared_the_right_way_round() -> None:
    labels = [1.0, 2.0, 3.0, 4.0]
    model = run(FakeRegressor(values=[1.5, 2.5, 2.5, 3.5]), frame(labels))[0]
    baseline = run(FakeRegressor(values=[1.0, 2.0, 3.0, 4.0]), frame(labels))[0]
    comparison = compare_to_baseline(model, baseline, baseline_name="baseline (linear regression)")
    rows = {row.id: row for row in comparison.rows}
    # RMSE 0.5 against a perfect 0.0: bigger is worse, so the model loses.
    assert (rows[Metric.RMSE].model_value, rows[Metric.RMSE].baseline_value) == (0.5, 0.0)
    assert rows[Metric.RMSE].delta == 0.5
    assert rows[Metric.RMSE].model_better is False
    assert comparison.model_beats_baseline is False
    assert comparison.baseline_name == "baseline (linear regression)"


def test_a_metric_the_baseline_could_not_report_leaves_that_row_blank() -> None:
    labels = [1, 1, 0, 0]
    model = report_for([0.9, 0.8, 0.2, 0.1], labels)
    # The baseline predicts nobody positive, so it has no precision to report.
    baseline = report_for([0.2, 0.1, 0.3, 0.05], labels)
    assert Metric.PRECISION not in metric_values(baseline)
    comparison = compare_to_baseline(model, baseline)
    rows = {row.id: row for row in comparison.rows}
    assert rows[Metric.PRECISION].baseline_value is None
    assert rows[Metric.PRECISION].delta is None
    assert rows[Metric.PRECISION].model_better is None
    assert rows[Metric.ROC_AUC].model_better is True  # the primary metric still decides


def test_the_baseline_table_lists_the_model_metrics_in_catalog_order() -> None:
    model = report_for(SCORES, LABELS)
    baseline = report_for(SCORES[::-1], LABELS)
    comparison = compare_to_baseline(model, baseline)
    assert [row.id for row in comparison.rows] == [item.id for item in model.metrics]


def test_two_reports_of_different_problems_are_refused() -> None:
    classification = report_for(SCORES, LABELS)
    regression = run(FakeRegressor(values=[1.5, 2.5, 2.5, 3.5]), frame([1.0, 2.0, 3.0, 4.0]))[0]
    with pytest.raises(EvaluationError) as raised:
        compare_to_baseline(classification, regression)
    assert raised.value.code == "EVAL_BASELINE_MISMATCH"


def test_two_reports_with_different_headline_metrics_are_refused() -> None:
    model = report_for(SCORES, LABELS)
    baseline = report_for(SCORES, LABELS, primary_metric=Metric.F1)
    with pytest.raises(EvaluationError) as raised:
        compare_to_baseline(model, baseline)
    assert raised.value.code == "EVAL_BASELINE_MISMATCH"


def test_two_reports_of_different_hold_outs_are_refused() -> None:
    model = report_for(SCORES, LABELS)
    baseline = report_for([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0])
    with pytest.raises(EvaluationError) as raised:
        compare_to_baseline(model, baseline)
    assert raised.value.code == "EVAL_BASELINE_MISMATCH"
    assert "one held-out set" in raised.value.message


# ---------------------------------------------------------------------------
# Fairness
# ---------------------------------------------------------------------------
def two_group_fixture() -> tuple[list[int], list[float], list[str]]:
    """Group A: 4 of 10 predicted positive; group B: 8 of 10. Rates worked out in the test."""
    labels = [*[1, 1, 1, 0], *[1, 0, 0, 0, 0, 0], *[1, 1, 1, 1, 0, 0, 0, 0], *[0, 0]]
    scores = [*[0.9] * 4, *[0.1] * 6, *[0.9] * 8, *[0.1] * 2]
    groups = [*["A"] * 10, *["B"] * 10]
    return labels, scores, groups


FAIRNESS_CONFIG = EvaluationConfig(fairness_column="region")


def test_fairness_reports_positive_rate_recall_and_precision_per_group() -> None:
    labels, scores, groups = two_group_fixture()
    _, _, _, fairness = run(FakeClassifier(scores=scores), frame(labels, region=groups), FAIRNESS_CONFIG)
    assert fairness.evaluated is True
    assert fairness.column == "region"
    assert fairness.reason_not_evaluated is None
    by_value = {group.value: group for group in fairness.groups}
    # A: 4 of 10 scored above 0.5 -> rate .4; of its 4 positives 3 are caught -> recall .75;
    #    of its 4 predictions 3 are right -> precision .75.
    assert (by_value["A"].rows, by_value["A"].positive_rate) == (10, 0.4)
    assert (by_value["A"].recall, by_value["A"].precision) == (0.75, 0.75)
    # B: 8 of 10 above 0.5 -> rate .8; all 4 positives caught -> recall 1.0; 4 of 8 right -> .5.
    assert (by_value["B"].rows, by_value["B"].positive_rate) == (10, 0.8)
    assert (by_value["B"].recall, by_value["B"].precision) == (1.0, 0.5)


def test_fairness_gaps_are_the_spread_between_the_best_and_worst_group() -> None:
    labels, scores, groups = two_group_fixture()
    _, _, _, fairness = run(FakeClassifier(scores=scores), frame(labels, region=groups), FAIRNESS_CONFIG)
    assert fairness.max_positive_rate_gap == pytest.approx(0.4)  # .8 - .4
    assert fairness.max_recall_gap == pytest.approx(0.25)  # 1.0 - .75
    assert fairness.max_precision_gap == pytest.approx(0.25)  # .75 - .5
    assert fairness.note == "Reported only; fairness never blocks a run."


def test_a_third_group_too_small_to_trust_is_listed_but_never_widens_a_gap() -> None:
    labels, scores, groups = two_group_fixture()
    labels = [*labels, 1, 1, 1, 1, 1]
    scores = [*scores, 0.9, 0.9, 0.9, 0.9, 0.9]
    groups = [*groups, "C", "C", "C", "C", "C"]
    _, _, _, fairness = run(FakeClassifier(scores=scores), frame(labels, region=groups), FAIRNESS_CONFIG)
    by_value = {group.value: group for group in fairness.groups}
    assert set(by_value) == {"A", "B", "C"}
    assert by_value["C"].rows == 5 < MIN_FAIRNESS_GROUP_ROWS
    assert by_value["C"].positive_rate == 1.0
    # C's perfect rate would make the gap 0.6 if small groups counted; they do not.
    assert fairness.max_positive_rate_gap == pytest.approx(0.4)
    assert fairness.max_recall_gap == pytest.approx(0.25)
    assert fairness.max_precision_gap == pytest.approx(0.25)


def test_groups_arrive_largest_first() -> None:
    labels, scores, groups = two_group_fixture()
    labels, scores, groups = [*labels, 1], [*scores, 0.9], [*groups, "C"]
    _, _, _, fairness = run(FakeClassifier(scores=scores), frame(labels, region=groups), FAIRNESS_CONFIG)
    assert [group.rows for group in fairness.groups] == [10, 10, 1]
    assert [group.value for group in fairness.groups] == ["A", "B", "C"]


def test_nulls_in_the_sensitive_column_become_their_own_visible_group() -> None:
    labels, scores, groups = two_group_fixture()
    with_nulls: list[Any] = [*groups[:18], None, None]
    _, _, _, fairness = run(FakeClassifier(scores=scores), frame(labels, region=with_nulls), FAIRNESS_CONFIG)
    by_value = {group.value: group for group in fairness.groups}
    assert MISSING_GROUP_LABEL in by_value
    assert sum(group.rows for group in fairness.groups) == 20


def test_a_group_with_no_actual_positives_has_no_recall_and_never_widens_the_recall_gap() -> None:
    labels = [*[1, 1, 0, 0, 0, 0, 0, 0, 0, 0], *[0] * 12]
    scores = [*[0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1], *[0.9] * 6, *[0.1] * 6]
    groups = [*["A"] * 10, *["B"] * 12]
    _, _, _, fairness = run(FakeClassifier(scores=scores), frame(labels, region=groups), FAIRNESS_CONFIG)
    by_value = {group.value: group for group in fairness.groups}
    assert by_value["B"].recall is None  # no actual positives in B
    assert by_value["B"].precision == 0.0  # 0 of 6 predictions right
    assert fairness.max_recall_gap is None  # only one group has a recall at all
    assert fairness.max_precision_gap == pytest.approx(1.0)  # 1.0 - 0.0


def test_fairness_is_skipped_with_a_reason_when_no_column_is_configured() -> None:
    _, _, _, fairness = fixed_run()
    assert fairness.evaluated is False
    assert fairness.column is None
    assert fairness.reason_not_evaluated == "No sensitive column was configured."
    assert fairness.groups == ()
    assert fairness.max_positive_rate_gap is None


def test_fairness_is_skipped_when_the_column_is_configured_but_absent_from_the_frame() -> None:
    _, _, _, fairness = run(FakeClassifier(scores=SCORES), frame(LABELS), FAIRNESS_CONFIG)
    assert fairness.evaluated is False
    assert "not in the test split" in (fairness.reason_not_evaluated or "")


def test_fairness_is_skipped_when_the_column_has_one_group() -> None:
    _, _, _, fairness = run(FakeClassifier(scores=SCORES), frame(LABELS, region=["A"] * 10), FAIRNESS_CONFIG)
    assert fairness.evaluated is False
    assert "one group" in (fairness.reason_not_evaluated or "")


def test_two_big_groups_are_needed_before_a_gap_is_reported() -> None:
    labels = [1, 0] * 6
    scores = [0.9, 0.1] * 6
    groups = [*["A"] * 11, "B"]
    _, _, _, fairness = run(FakeClassifier(scores=scores), frame(labels, region=groups), FAIRNESS_CONFIG)
    assert fairness.evaluated is True
    assert len(fairness.groups) == 2
    assert fairness.max_positive_rate_gap is None


# ---------------------------------------------------------------------------
# Documented edge cases
# ---------------------------------------------------------------------------
def test_an_empty_test_split_is_refused() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(FakeClassifier(scores=[]), frame([]))
    assert raised.value.code == "EVAL_EMPTY_TEST_SPLIT"


def test_a_frame_without_the_target_column_is_refused() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(FakeClassifier(scores=SCORES), pd.DataFrame({"feature": list(range(10))}))
    assert raised.value.code == "EVAL_TARGET_COLUMN_MISSING"


def test_a_scorer_without_the_call_its_problem_type_needs_is_refused() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(ScorerWithoutAScoringCall(), frame(LABELS))
    assert raised.value.code == "EVAL_SCORER_INCOMPLETE"
    assert "predict_proba" in raised.value.message


def test_a_scorer_that_returns_the_wrong_number_of_scores_is_refused() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(FakeClassifier(scores=SCORES[:4]), frame(LABELS))
    assert raised.value.code == "EVAL_SCORES_SHAPE"


def test_a_classifier_that_does_not_name_its_classes_is_refused() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(FakeClassifier(scores=SCORES, classes=None), frame(LABELS))
    assert raised.value.code == "EVAL_CLASSES_UNKNOWN"


def test_an_outcome_the_model_never_saw_is_refused_rather_than_filed_as_negative() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(
            FakeClassifier(scores=[0.9, 0.8, 0.2, 0.1], classes=("no", "yes")),
            frame(["yes", "no", "maybe", "no"]),
        )
    assert raised.value.code == "EVAL_TARGET_NOT_BINARY"
    assert "maybe" in raised.value.message


def test_a_single_class_test_split_drops_roc_auc_rather_than_inventing_one() -> None:
    report, _, _, _ = run(
        FakeClassifier(scores=[0.9, 0.8, 0.2, 0.1], primary_metric=Metric.F1), frame([0, 0, 0, 0])
    )
    ids = set(metric_values(report))
    assert Metric.ROC_AUC not in ids  # undefined with one class
    assert Metric.PR_AUC not in ids  # undefined with no positives
    assert Metric.RECALL not in ids  # no actual positives to recall
    # F1 and precision still have a denominator: two rows were predicted positive and both are wrong.
    assert metric_values(report) == {Metric.F1: 0.0, Metric.PRECISION: 0.0}
    assert report.positive_rate == 0.0


def test_an_all_positive_test_split_keeps_pr_auc_and_drops_roc_auc() -> None:
    report, _, _, _ = run(
        FakeClassifier(scores=[0.9, 0.8, 0.2, 0.1], primary_metric=Metric.PR_AUC),
        frame([1, 1, 1, 1]),
    )
    values = metric_values(report)
    assert Metric.ROC_AUC not in values
    assert values[Metric.PR_AUC] == 1.0  # precision is 1 at every recall
    assert values[Metric.RECALL] == 0.5  # 2 of 4 above the 0.5 threshold
    assert values[Metric.PRECISION] == 1.0
    assert report.positive_rate == 1.0


def test_an_undefined_primary_metric_stops_the_stage_instead_of_fabricating_a_headline() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(FakeClassifier(scores=[0.9, 0.8, 0.2, 0.1]), frame([0, 0, 0, 0]))
    assert raised.value.code == "EVAL_PRIMARY_METRIC_UNDEFINED"
    assert "ROC-AUC" in raised.value.message


def test_a_primary_metric_that_cannot_score_the_problem_type_is_refused() -> None:
    # Forecasting has no catalog metrics in Phase 1, so no metric can be its headline.
    with pytest.raises(EvaluationError) as raised:
        run(FakeClassifier(scores=[0.9, 0.1], problem_type=ProblemType.FORECASTING), frame([1, 0]))
    assert raised.value.code == "EVAL_METRIC_NOT_FOR_PROBLEM"


def test_a_zero_base_rate_leaves_lift_null_and_draws_the_rates_instead() -> None:
    _, _, lift, _ = run(
        FakeClassifier(scores=[index / 12 for index in range(12)], primary_metric=Metric.F1),
        frame([0] * 12),
    )
    assert lift.base_rate_pct == 0.0
    assert lift.unit == "%"
    assert all(bin_.lift is None for bin_ in lift.bins)
    assert all(bin_.cumulative_lift is None for bin_ in lift.bins)
    assert all(bin_.cumulative_capture_pct is None for bin_ in lift.bins)
    assert list(lift.values) == [0.0] * 10
    assert "no base rate" in lift.caption


def test_the_zero_denominator_rates_of_the_confusion_matrix_fall_back_to_zero() -> None:
    _, matrix, _, _ = run(
        FakeClassifier(scores=[0.9, 0.8, 0.2, 0.1], primary_metric=Metric.F1), frame([0, 0, 0, 0])
    )
    assert matrix.cells == (0, 0, 2, 2)
    assert (matrix.precision, matrix.recall, matrix.f1) == (0.0, 0.0, 0.0)
    assert matrix.specificity == 0.5  # tn 2 / (tn 2 + fp 2), a real number


def test_a_perfectly_separable_split_reports_ones() -> None:
    report, matrix, _, _ = run(
        FakeClassifier(scores=[0.99, 0.80, 0.70, 0.20, 0.10, 0.05]), frame([1, 1, 1, 0, 0, 0])
    )
    values = metric_values(report)
    assert values[Metric.ROC_AUC] == 1.0
    assert values[Metric.PR_AUC] == 1.0
    assert values[Metric.F1] == 1.0
    assert matrix.cells == (3, 0, 0, 3)


def test_non_numeric_scores_are_refused() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(FakeClassifier(scores=["a"] * 10), frame(LABELS))
    assert raised.value.code == "EVAL_SCORES_NOT_NUMERIC"


def test_a_missing_score_is_refused_rather_than_averaged_over() -> None:
    with pytest.raises(EvaluationError) as raised:
        run(FakeClassifier(scores=[*SCORES[:9], float("nan")]), frame(LABELS))
    assert raised.value.code == "EVAL_SCORES_NOT_FINITE"


def test_the_classes_name_both_sides_of_the_matrix() -> None:
    _, matrix, _, _ = run(
        FakeClassifier(scores=[0.9, 0.8, 0.2, 0.1], classes=("lapsed", "converted")),
        frame(["converted", "converted", "lapsed", "lapsed"]),
    )
    assert (matrix.positive_label, matrix.negative_label) == ("converted", "lapsed")
    assert matrix.cells == (2, 0, 0, 2)


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------
def test_regression_reports_rmse_mae_and_r_squared() -> None:
    report, matrix, _, _ = run(FakeRegressor(values=[1.5, 2.5, 2.5, 3.5]), frame([1.0, 2.0, 3.0, 4.0]))
    values = metric_values(report)
    # Residuals are +.5, +.5, -.5, -.5: mean square .25 -> RMSE .5, mean absolute .5.
    assert values[Metric.RMSE] == 0.5
    assert values[Metric.MAE] == 0.5
    # R2 = 1 - 1.0 / 5.0, where 5.0 is the total squared spread around the mean of 2.5.
    assert report.extra_metrics["r2"] == 0.8
    assert report.headline_score == 0.5
    assert report.primary_metric_label == "RMSE"
    assert all(item.greater_is_better is False for item in report.metrics)
    assert matrix is None
    assert report.positive_rate is None
    assert report.calibration is None


def test_regression_reports_the_residual_distribution() -> None:
    report, _, _, _ = run(FakeRegressor(values=[1.5, 2.5, 2.5, 3.5]), frame([1.0, 2.0, 3.0, 4.0]))
    # Residuals (actual - predicted) are -.5, -.5, +.5, +.5.
    assert report.extra_metrics["residual_mean"] == 0.0
    assert report.extra_metrics["residual_min"] == -0.5
    assert report.extra_metrics["residual_max"] == 0.5
    assert report.extra_metrics["residual_median"] == 0.0
    assert report.extra_metrics["residual_std"] == 0.5
    assert {"residual_p05", "residual_p25", "residual_p75", "residual_p95"} <= set(report.extra_metrics)


def test_regression_deciles_compare_the_actual_mean_with_the_overall_mean() -> None:
    _, _, lift, _ = run(FakeRegressor(values=[1.5, 2.5, 2.5, 3.5]), frame([1.0, 2.0, 3.0, 4.0]))
    assert lift.mode == "regression"
    assert lift.base_rate_pct is None
    assert lift.unit == "x"
    # Sorted by prediction: 3.5 (actual 4), then the two tied 2.5s in input order (2, then 3), then 1.5 (1).
    assert [bin_.predicted_mean for bin_ in lift.bins] == [3.5, 2.5, 2.5, 1.5]
    assert [bin_.actual_mean for bin_ in lift.bins] == [4.0, 2.0, 3.0, 1.0]
    # Overall actual mean is 2.5, so the ratios are 1.6, 0.8, 1.2 and 0.4.
    assert list(lift.values) == [1.6, 0.8, 1.2, 0.4]
    assert [bin_.cumulative_capture_pct for bin_ in lift.bins] == [40.0, 60.0, 90.0, 100.0]
    assert [bin_.cumulative_lift for bin_ in lift.bins] == [1.6, 1.2, 1.2, 1.0]
    assert all(bin_.positives is None and bin_.actual_rate_pct is None for bin_ in lift.bins)


def test_regression_leaves_the_chart_undrawn_when_the_overall_mean_is_not_positive() -> None:
    _, _, lift, _ = run(FakeRegressor(values=[0.4, 0.3, 0.2, 0.1]), frame([-1.0, -2.0, 1.0, 2.0]))
    assert lift.values == ()
    assert all(bin_.lift is None for bin_ in lift.bins)
    assert "not positive" in lift.caption


def test_regression_reports_whatever_threshold_line_the_scorer_carries() -> None:
    report, _, _, _ = run(FakeRegressor(values=[1.5, 2.5, 2.5, 3.5]), frame([1.0, 2.0, 3.0, 4.0]))
    assert report.threshold == 0.50
    assert "No decision threshold" in report.threshold_detail


def test_regression_skips_fairness_because_the_statistics_are_classification_only() -> None:
    _, _, _, fairness = run(
        FakeRegressor(values=[1.5, 2.5, 2.5, 3.5]),
        frame([1.0, 2.0, 3.0, 4.0], region=["A", "A", "B", "B"]),
        FAIRNESS_CONFIG,
    )
    assert fairness.evaluated is False
    assert "classification runs only" in (fairness.reason_not_evaluated or "")


def test_a_regressor_without_a_predict_call_is_refused() -> None:
    broken = ScorerWithoutAScoringCall()
    broken.problem_type = ProblemType.REGRESSION
    broken.primary_metric = Metric.RMSE
    with pytest.raises(EvaluationError) as raised:
        run(broken, frame([1.0, 2.0]))
    assert raised.value.code == "EVAL_SCORER_INCOMPLETE"
    assert "predict" in raised.value.message


# ---------------------------------------------------------------------------
# Determinism and the champion-versus-challenger shape
# ---------------------------------------------------------------------------
def test_two_runs_over_the_same_input_produce_byte_identical_artefacts() -> None:
    labels, scores, groups = two_group_fixture()

    def once() -> list[str]:
        artefacts = run(
            FakeClassifier(
                scores=scores,
                threshold=0.42,
                threshold_mode=ThresholdMode.AUTO,
                threshold_detail="Auto (maximises F1 on validation): 0.42",
                calibration=CalibrationSummary(
                    method=Calibration.ISOTONIC, brier_before=0.2506, brier_after=0.1675
                ),
            ),
            frame(labels, region=groups),
            FAIRNESS_CONFIG,
        )
        return [dump_artefact(artefact) for artefact in artefacts if artefact is not None]

    assert once() == once()


def test_the_same_frame_measured_by_two_models_is_comparable_row_for_row() -> None:
    # This is the champion-versus-challenger shape: one frame, two scorers, one call each.
    labels, scores, groups = two_group_fixture()
    held_out = frame(labels, region=groups)
    champion, _, _, _ = run(FakeClassifier(scores=scores), held_out, FAIRNESS_CONFIG)
    challenger, _, _, _ = run(FakeClassifier(scores=scores[::-1]), held_out, FAIRNESS_CONFIG)
    assert champion.rows_evaluated == challenger.rows_evaluated == 20
    assert champion.positive_rate == challenger.positive_rate
    comparison = compare_to_baseline(challenger, champion)
    assert len(comparison.rows) == len(challenger.metrics)


def test_the_artefacts_carry_the_run_id_and_the_supplied_timestamp() -> None:
    report, matrix, lift, fairness = fixed_run()
    assert report.run_id == RUN_ID
    assert matrix.run_id == report.run_id
    assert lift.run_id == report.run_id
    assert fairness.run_id == report.run_id
    assert report.evaluated_at == FIXED_NOW
    assert lift.computed_at == FIXED_NOW
