"""`engine.uplift.metrics`: Qini, AUUC, uplift@k, deciles and the bootstrap, checked independently.

Every expected number is worked out without the implementation: by hand from the definitions (the
8-row fixture below, with the cumulative table spelled out), or by a deliberately naive, loop-based
reimplementation in this file. `sklift` and `causalml` are not installed in this environment, so the
brute-force reimplementation is the cross-check; it walks `k = 0..n` one row at a time, with Python
floats and its own trapezoid sum, and re-draws the bootstrap resamples one row at a time.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

import numpy as np
import pytest

from engine.config import Metric
from engine.uplift import metrics
from engine.uplift.config import UpliftBaseModel, UpliftLearner
from engine.uplift.contracts import NOT_CAUSAL_NOTE, QiniCurve, UpliftEvaluation
from engine.uplift.metrics import (
    auuc_score,
    bootstrap_uplift_at,
    decile_table,
    evaluate_uplift,
    qini_coefficient,
    qini_points,
    uplift_at_fraction,
)
from tests.fixtures.make_uplift_data import make_uplift_data

NOW = datetime(2026, 9, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------
# The hand-worked fixture. Already ranked (pred descending), so row k is the k-th best.
#
#   k | t y | N_t N_c Y_t Y_c | Qini(k)            | U(k)
#   0 |     |  0   0   0   0 | 0                  | 0
#   1 | 1 1 |  1   0   1   0 | 1 - 0      = 1     | 0 (no control yet)
#   2 | 0 0 |  1   1   1   0 | 1 - 0      = 1     | (1 - 0)·2/8     = 1/4
#   3 | 1 1 |  2   1   2   0 | 2 - 0      = 2     | (1 - 0)·3/8     = 3/8
#   4 | 0 1 |  2   2   2   1 | 2 - 1·2/2  = 1     | (1 - 1/2)·4/8   = 1/4
#   5 | 1 0 |  3   2   2   1 | 2 - 1·3/2  = 1/2   | (2/3 - 1/2)·5/8 = 5/48
#   6 | 0 0 |  3   3   2   1 | 2 - 1·3/3  = 1     | (2/3 - 1/3)·6/8 = 1/4
#   7 | 1 0 |  4   3   2   1 | 2 - 1·4/3  = 2/3   | (2/4 - 1/3)·7/8 = 7/48
#   8 | 0 1 |  4   4   2   2 | 2 - 2·4/4  = 0     | (2/4 - 2/4)·1   = 0
#
# ATE = 2/4 - 2/4 = 0 and Qini(n) = 0, so both random lines are 0. Trapezoid with dx = 1/8 and zero
# end points is 1/8 · (sum of the interior values):
#   AUUC = 1/8 · (1/4 + 3/8 + 1/4 + 5/48 + 1/4 + 7/48)         = 1/8 · 11/8 = 11/64
#   qini = 1/8 · (1 + 1 + 2 + 1 + 1/2 + 1 + 2/3) / 8            = 43/6 / 64  = 43/384
# ---------------------------------------------------------------------------
PRED = np.array([0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1])
T = np.array([1, 0, 1, 0, 1, 0, 1, 0])
Y = np.array([1, 0, 1, 1, 0, 0, 0, 1])


# ---------------------------------------------------------------------------
# The brute-force reference: one row at a time, plain Python.
# ---------------------------------------------------------------------------
def _brute_order(pred: list[float]) -> list[int]:
    # Python's sort is stable, so equal predictions keep their input order.
    return sorted(range(len(pred)), key=lambda i: -pred[i])


def _brute_curves(pred: list[float], t: list[int], y: list[int]) -> dict[str, list[float] | float]:
    n = len(pred)
    order = _brute_order(pred)
    n_t = n_c = y_t = y_c = 0.0
    qini, curve = [0.0], [0.0]
    for k, i in enumerate(order, start=1):
        if t[i] == 1:
            n_t += 1
            y_t += y[i]
        else:
            n_c += 1
            y_c += y[i]
        qini.append(y_t - (y_c * n_t / n_c if n_c > 0 else 0.0))
        curve.append((y_t / n_t - y_c / n_c) * k / n if n_t > 0 and n_c > 0 else 0.0)
    ate = y_t / n_t - y_c / n_c
    auuc = 0.0
    qini_area = 0.0
    for k in range(n):
        f0, f1 = k / n, (k + 1) / n
        g0, g1 = curve[k] - f0 * ate, curve[k + 1] - f1 * ate
        auuc += (g0 + g1) / 2 / n
        q0 = qini[k] / n - f0 * qini[n] / n
        q1 = qini[k + 1] / n - f1 * qini[n] / n
        qini_area += (q0 + q1) / 2 / n
    return {"qini": qini, "curve": curve, "ate": ate, "auuc": auuc, "qini_area": qini_area}


def _brute_uplift_at(pred: list[float], t: list[int], y: list[int], fraction: float) -> float | None:
    top = _brute_order(pred)[: math.ceil(round(fraction * len(pred), 9))]
    treated = [y[i] for i in top if t[i] == 1]
    control = [y[i] for i in top if t[i] == 0]
    if not treated or not control:
        return None
    return sum(treated) / len(treated) - sum(control) / len(control)


def _percentile_interval(values: list[float]) -> tuple[float, float]:
    low, high = np.percentile(np.array(values), [2.5, 97.5])
    return float(low), float(high)


def _random_data(n: int, seed: int, *, ties: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    pred = rng.normal(size=n)
    if ties:
        pred = np.round(pred, 1)  # plenty of equal predictions
    t = rng.integers(0, 2, size=n)
    y = (rng.random(n) < 0.2 + 0.2 * t * (pred > 0)).astype(int)
    return pred, t, y


def _evaluate(
    pred: np.ndarray, t: np.ndarray, y: np.ndarray, **kwargs: object
) -> tuple[UpliftEvaluation, QiniCurve]:
    options: dict[str, object] = {
        "run_id": "run-1",
        "learner": UpliftLearner.T_LEARNER,
        "base_model": UpliftBaseModel.LIGHTGBM,
        "bootstrap_samples": 50,
        "seed": 11,
        "causal": True,
        "now": NOW,
    }
    options.update(kwargs)
    return evaluate_uplift(pred, t, y, **options)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Hand-computed fixture
# ---------------------------------------------------------------------------
def test_auuc_matches_the_hand_computation() -> None:
    assert auuc_score(PRED, T, Y) == pytest.approx(11 / 64, abs=1e-12)


def test_qini_coefficient_matches_the_hand_computation() -> None:
    assert qini_coefficient(PRED, T, Y) == pytest.approx(43 / 384, abs=1e-12)


def test_uplift_at_fraction_matches_the_hand_computation() -> None:
    # Top ceil(0.25·8) = 2 rows: treated 1/1, control 0/1.
    assert uplift_at_fraction(PRED, T, Y, 0.25) == pytest.approx(1.0)
    # Top 4 rows: treated 2/2, control 1/2.
    assert uplift_at_fraction(PRED, T, Y, 0.5) == pytest.approx(0.5)
    # Top ceil(0.8) = 1 row: no control customer, so no rate difference - not zero.
    assert uplift_at_fraction(PRED, T, Y, 0.1) is None
    # The whole population: 2/4 - 2/4.
    assert uplift_at_fraction(PRED, T, Y, 1.0) == pytest.approx(0.0)


def test_top_rows_use_an_exact_ceiling() -> None:
    # 0.1 · 30 is 3.0000000000000004 in floating point; the top 10% of 30 rows is 3 rows, not 4.
    pred = -np.arange(30, dtype=float)
    t = np.array([1, 0, 1] + [0] * 27)
    y = np.array([1, 0, 0] + [1] * 27)
    # Top 3: treated 1/2, control 0/1. A fourth row (control, converted) would give 0.5 - 0.5 = 0.
    assert uplift_at_fraction(pred, t, y, 0.1) == pytest.approx(0.5)


def test_qini_points_lie_on_the_exact_curve() -> None:
    points = qini_points(PRED, T, Y, points=5)
    # k = 0, 2, 4, 6, 8 → Qini(k)/8 and U(k) from the table.
    assert [p.fraction for p in points] == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert [p.qini for p in points] == pytest.approx([0.0, 1 / 8, 1 / 8, 1 / 8, 0.0])
    assert [p.uplift_curve for p in points] == pytest.approx([0.0, 1 / 4, 1 / 4, 1 / 4, 0.0])
    assert [p.random for p in points] == pytest.approx([0.0] * 5)


def test_qini_points_never_repeat_a_row_count() -> None:
    points = qini_points(PRED, T, Y)  # 101 requested, only 9 distinct k exist
    assert [p.fraction for p in points] == [k / 8 for k in range(9)]
    # Qini(k)/n with the Qini(k) column of the table.
    assert [p.qini * 8 for p in points] == pytest.approx([0, 1, 1, 2, 1, 0.5, 1, 2 / 3, 0], abs=1e-12)
    assert [p.uplift_curve for p in points] == pytest.approx(
        [0, 0, 1 / 4, 3 / 8, 1 / 4, 5 / 48, 1 / 4, 7 / 48, 0], abs=1e-12
    )


def test_qini_points_random_line_uses_the_final_qini() -> None:
    pred, t, y = _random_data(400, 3, ties=False)
    points = qini_points(pred, t, y, points=11)
    final = points[-1].qini
    assert points[-1].fraction == 1.0
    for point in points:
        assert point.random == pytest.approx(point.fraction * final)
    # U(n) is the overall rate difference.
    assert points[-1].uplift_curve == pytest.approx(y[t == 1].mean() - y[t == 0].mean())


def test_deciles_match_a_hand_split() -> None:
    pred = -np.arange(20, dtype=float)  # already ranked; array_split gives ten pairs
    t = np.array([1, 0] * 10)
    y = np.array([1, 0, 1, 1, 0, 0, 0, 1] + [1, 0] * 6)
    deciles = decile_table(pred, t, y)
    assert [d.decile for d in deciles] == list(range(1, 11))
    assert all(d.rows == 2 and d.treated_rows == 1 and d.control_rows == 1 for d in deciles)
    first, second, third, fourth = deciles[:4]
    assert (first.treated_rate, first.control_rate, first.observed_uplift) == (1.0, 0.0, 1.0)
    assert (second.treated_rate, second.control_rate, second.observed_uplift) == (1.0, 1.0, 0.0)
    assert (third.treated_rate, third.control_rate, third.observed_uplift) == (0.0, 0.0, 0.0)
    assert (fourth.treated_rate, fourth.control_rate, fourth.observed_uplift) == (0.0, 1.0, -1.0)
    assert first.predicted_uplift == pytest.approx(-0.5)
    assert deciles[-1].predicted_uplift == pytest.approx(-18.5)


def test_deciles_report_missing_arms_as_none() -> None:
    pred = -np.arange(20, dtype=float)
    t = np.array([1, 1] + [1, 0] * 9)
    y = np.array([1, 0] + [0] * 18)
    first = decile_table(pred, t, y)[0]
    assert first.control_rows == 0
    assert first.control_rate is None
    assert first.observed_uplift is None
    assert first.treated_rate == pytest.approx(0.5)


def test_deciles_of_fewer_than_ten_rows_leave_empty_groups_out() -> None:
    deciles = decile_table(PRED, T, Y)
    assert [d.decile for d in deciles] == list(range(1, 9))
    assert sum(d.rows for d in deciles) == 8


# ---------------------------------------------------------------------------
# Ties and input validation
# ---------------------------------------------------------------------------
def test_ties_keep_input_order() -> None:
    _, t, y = _random_data(300, 5, ties=False)
    flat = np.zeros(300)
    by_position = -np.arange(300, dtype=float)
    assert auuc_score(flat, t, y) == pytest.approx(auuc_score(by_position, t, y), abs=1e-15)
    assert qini_coefficient(flat, t, y) == pytest.approx(qini_coefficient(by_position, t, y), abs=1e-15)


@pytest.mark.parametrize(
    ("pred", "t", "y"),
    [
        ([0.1, 0.2], [1, 0, 1], [0, 1, 0]),
        ([0.1, 0.2], [1, 2], [0, 1]),
        ([0.1, float("nan")], [1, 0], [0, 1]),
        ([0.1, 0.2], [1, 0], [0.5, 1]),
        ([], [], []),
    ],
)
def test_bad_inputs_are_refused(pred: list[float], t: list[float], y: list[float]) -> None:
    with pytest.raises(ValueError):
        auuc_score(pred, t, y)


def test_curve_metrics_need_both_arms() -> None:
    with pytest.raises(ValueError, match="treated and control"):
        auuc_score([0.3, 0.2, 0.1], [1, 1, 1], [0, 1, 1])
    with pytest.raises(ValueError, match="treated and control"):
        qini_coefficient([0.3, 0.2, 0.1], [0, 0, 0], [0, 1, 1])
    with pytest.raises(ValueError, match="treated and control"):
        _evaluate(np.array([0.3, 0.2]), np.array([1, 1]), np.array([0, 1]))
    assert uplift_at_fraction([0.3, 0.2, 0.1], [1, 1, 1], [0, 1, 1], 0.5) is None
    assert bootstrap_uplift_at([0.3, 0.2, 0.1], [1, 1, 1], [0, 1, 1], 0.5, samples=10, seed=0) is None


def test_booleans_are_accepted_for_treatment_and_outcome() -> None:
    assert auuc_score(PRED, T.astype(bool), Y.astype(bool)) == pytest.approx(11 / 64)


# ---------------------------------------------------------------------------
# Brute-force cross-check
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("seed", "ties"), [(1, False), (2, True), (3, True)])
def test_point_metrics_match_the_brute_force(seed: int, ties: bool) -> None:
    pred, t, y = _random_data(250, seed, ties=ties)
    reference = _brute_curves(pred.tolist(), t.tolist(), y.tolist())
    assert auuc_score(pred, t, y) == pytest.approx(reference["auuc"], abs=1e-12)
    assert qini_coefficient(pred, t, y) == pytest.approx(reference["qini_area"], abs=1e-12)
    for fraction in (0.05, 0.1, 0.2, 0.3, 0.5, 1.0):
        expected = _brute_uplift_at(pred.tolist(), t.tolist(), y.tolist(), fraction)
        actual = uplift_at_fraction(pred, t, y, fraction)
        assert (actual is None) == (expected is None)
        if expected is not None:
            assert actual == pytest.approx(expected, abs=1e-12)
    points = qini_points(pred, t, y, points=251)  # n = 250 → every k exactly once
    assert len(points) == 251
    assert [p.qini for p in points] == pytest.approx([q / 250 for q in reference["qini"]], abs=1e-12)  # type: ignore[union-attr]
    assert [p.uplift_curve for p in points] == pytest.approx(reference["curve"], abs=1e-12)


def test_bootstrap_matches_a_loop_based_resampler() -> None:
    pred, t, y = _random_data(120, 9, ties=False)
    samples, seed = 40, 123
    rng = np.random.default_rng(seed)
    treated_rows = np.flatnonzero(t == 1)
    control_rows = np.flatnonzero(t == 0)
    auuc, qini, ate = [], [], []
    at: dict[float, list[float]] = {0.1: [], 0.2: [], 0.3: []}
    for _ in range(samples):
        draw_t = treated_rows[rng.integers(0, treated_rows.size, treated_rows.size)]
        draw_c = control_rows[rng.integers(0, control_rows.size, control_rows.size)]
        # Listed in input order, so a stable sort breaks ties exactly as the module promises.
        rows = np.sort(np.concatenate([draw_t, draw_c]))
        p, tt, yy = pred[rows].tolist(), t[rows].tolist(), y[rows].tolist()
        reference = _brute_curves(p, tt, yy)
        auuc.append(float(reference["auuc"]))  # type: ignore[arg-type]
        qini.append(float(reference["qini_area"]))  # type: ignore[arg-type]
        ate.append(float(reference["ate"]))  # type: ignore[arg-type]
        for fraction, values in at.items():
            value = _brute_uplift_at(p, tt, yy, fraction)
            assert value is not None
            values.append(value)

    evaluation, _ = _evaluate(pred, t, y, bootstrap_samples=samples, seed=seed)
    assert (evaluation.auuc.ci_low, evaluation.auuc.ci_high) == pytest.approx(_percentile_interval(auuc))
    assert (evaluation.qini_coefficient.ci_low, evaluation.qini_coefficient.ci_high) == pytest.approx(
        _percentile_interval(qini)
    )
    assert (
        evaluation.average_treatment_effect.ci_low,
        evaluation.average_treatment_effect.ci_high,
    ) == pytest.approx(_percentile_interval(ate))
    for item in evaluation.uplift_at:
        assert (item.uplift.ci_low, item.uplift.ci_high) == pytest.approx(
            _percentile_interval(at[item.fraction])
        )
        alone = bootstrap_uplift_at(pred, t, y, item.fraction, samples=samples, seed=seed)
        assert alone == item.uplift


def test_bootstrap_does_not_depend_on_the_chunk_size(monkeypatch: pytest.MonkeyPatch) -> None:
    pred, t, y = _random_data(500, 4, ties=True)
    whole, _ = _evaluate(pred, t, y, bootstrap_samples=30)
    monkeypatch.setattr(metrics, "_CHUNK_CELLS", 1_200)  # two resamples per chunk
    chunked, _ = _evaluate(pred, t, y, bootstrap_samples=30)
    assert chunked == whole


def test_bootstrap_is_deterministic_by_seed() -> None:
    pred, t, y = _random_data(600, 6, ties=False)
    first = bootstrap_uplift_at(pred, t, y, 0.3, samples=60, seed=5)
    again = bootstrap_uplift_at(pred, t, y, 0.3, samples=60, seed=5)
    other = bootstrap_uplift_at(pred, t, y, 0.3, samples=60, seed=6)
    assert first is not None and other is not None
    assert first == again
    assert (first.ci_low, first.ci_high) != (other.ci_low, other.ci_high)
    assert first.value == other.value  # the point estimate does not depend on the seed
    one, _ = _evaluate(pred, t, y, seed=5)
    two, _ = _evaluate(pred, t, y, seed=5)
    assert one == two


def test_an_undefined_resample_gives_no_interval() -> None:
    # Top ceil(0.1·20) = 2 rows hold one treated and one control customer; with only 2 control rows
    # in 20, many resamples put no control customer in the top 2, so no honest interval exists.
    pred = -np.arange(20, dtype=float)
    t = np.array([1, 0] + [1] * 17 + [0])
    y = np.array([1, 0] + [0, 1] * 9)
    result = bootstrap_uplift_at(pred, t, y, 0.1, samples=200, seed=1)
    assert result is not None
    assert result.value == pytest.approx(1.0)
    assert result.ci_low is None and result.ci_high is None


def test_bootstrap_of_30k_rows_takes_seconds() -> None:
    pred, t, y = _random_data(30_000, 8, ties=False)
    started = time.perf_counter()
    _evaluate(pred, t, y, bootstrap_samples=200)
    assert time.perf_counter() - started < 30.0


# ---------------------------------------------------------------------------
# Sanity on generated data with a known effect
# ---------------------------------------------------------------------------
def test_perfect_ranking_beats_random_and_reversed_ranking_loses() -> None:
    data = make_uplift_data(12_000, seed=21)
    truth = data.truth["true_uplift"].to_numpy()
    t = data.frame["treatment"].to_numpy()
    y = data.frame["reactivated_90d"].to_numpy()

    best, curve = _evaluate(truth, t, y, bootstrap_samples=100)
    worst, _ = _evaluate(-truth, t, y, bootstrap_samples=100)

    assert best.auuc.value > 0.0 and best.auuc.ci_low is not None and best.auuc.ci_low > 0.0
    assert best.measurable_uplift is True
    assert best.qini_coefficient.value > 0.0
    assert best.summary.startswith("Targeting by predicted uplift beats random targeting: AUUC ")
    assert worst.auuc.value < 0.0 and worst.qini_coefficient.value < 0.0
    assert worst.measurable_uplift is False
    assert worst.summary.startswith("No measurable uplift:")
    # The top decile of the true ranking holds persuadables: its observed uplift is clearly positive.
    assert best.deciles[0].observed_uplift is not None and best.deciles[0].observed_uplift > 0.1
    assert curve.points[0].fraction == 0.0 and curve.points[-1].fraction == 1.0
    assert len(curve.points) == 101


def test_null_effect_with_random_predictions_is_not_measurable() -> None:
    data = make_uplift_data(8_000, seed=4, effect_scale=0.0)
    t = data.frame["treatment"].to_numpy()
    y = data.frame["reactivated_90d"].to_numpy()
    pred = np.random.default_rng(99).normal(size=t.size)
    evaluation, _ = _evaluate(pred, t, y, bootstrap_samples=200)
    assert evaluation.auuc.ci_low is not None and evaluation.auuc.ci_high is not None
    assert evaluation.auuc.ci_low <= 0.0 <= evaluation.auuc.ci_high
    assert evaluation.measurable_uplift is False
    assert "includes zero, so this model cannot be shown to target better than random." in evaluation.summary


# ---------------------------------------------------------------------------
# The artefacts
# ---------------------------------------------------------------------------
def test_evaluation_artefact_is_complete() -> None:
    pred, t, y = _random_data(1_000, 12, ties=False)
    evaluation, curve = _evaluate(pred, t, y, run_id="run-9", causal=True)
    assert evaluation.run_id == "run-9" and curve.run_id == "run-9"
    assert evaluation.primary_metric is Metric.AUUC
    assert evaluation.rows_evaluated == 1_000 == curve.rows_evaluated
    assert evaluation.treated_rows == int(t.sum())
    assert evaluation.control_rows == 1_000 - int(t.sum())
    assert evaluation.treated_rate == pytest.approx(y[t == 1].mean())
    assert evaluation.control_rate == pytest.approx(y[t == 0].mean())
    assert evaluation.average_treatment_effect.value == pytest.approx(
        evaluation.treated_rate - evaluation.control_rate
    )
    assert evaluation.auuc.value == pytest.approx(auuc_score(pred, t, y))
    assert evaluation.qini_coefficient.value == pytest.approx(qini_coefficient(pred, t, y))
    assert [item.fraction for item in evaluation.uplift_at] == [0.1, 0.2, 0.3]
    for item in evaluation.uplift_at:
        assert item.uplift.value == pytest.approx(uplift_at_fraction(pred, t, y, item.fraction))
        assert item.uplift.ci_low is not None and item.uplift.ci_low <= item.uplift.ci_high  # type: ignore[operator]
    assert evaluation.deciles == decile_table(pred, t, y)
    assert evaluation.bootstrap_samples == 50
    assert evaluation.evaluated_at == NOW
    assert evaluation.causal is True
    assert not evaluation.summary.startswith(NOT_CAUSAL_NOTE)


def test_not_causal_summary_carries_the_note() -> None:
    pred, t, y = _random_data(500, 13, ties=False)
    evaluation, _ = _evaluate(pred, t, y, causal=False)
    assert evaluation.causal is False
    assert evaluation.summary.startswith(NOT_CAUSAL_NOTE + " ")


def test_uplift_at_leaves_out_a_share_with_an_empty_arm() -> None:
    # 20 rows, top 2 (10%) treated only, top 4 (20%) and top 6 (30%) mixed.
    pred = -np.arange(20, dtype=float)
    t = np.array([1, 1, 0, 0, 1, 0] + [1, 0] * 7)
    y = np.array([1, 0, 0, 1, 1, 0] + [0, 1] * 7)
    evaluation, _ = _evaluate(pred, t, y, bootstrap_samples=20)
    assert [item.fraction for item in evaluation.uplift_at] == [0.2, 0.3]
