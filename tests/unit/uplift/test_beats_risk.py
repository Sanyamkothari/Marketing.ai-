"""Plan J M96: does ranking by uplift beat ranking by risk, and is the predicted uplift calibrated?

`engine.uplift.metrics.compare_with_baselines` scores plain rankings of the hold-out with the same AUUC
and gives the uplift model's AUUC minus each with a PAIRED bootstrap; `calibration_by_decile` sets the
predicted uplift of each decile against what was measured there. Three planted hold-outs pin the
verdicts the acceptance criteria name:

* **uplift = minus risk** - the effect grows with risk and the "model" ranks by minus risk: it does
  not beat risk ranking (the paired interval lies below zero);
* **heterogeneous effect** - the effect is unrelated to risk and the model knows it: it beats risk;
* **equal effect** - every customer gains the same: neither ranking can do better than random, the
  paired interval covers zero and the sentence says "does not beat risk ranking".

All fail on the code before M96, which had none of these functions.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from engine.uplift.metrics import (
    BaselineInput,
    calibration_by_decile,
    compare_with_baselines,
    evaluate_uplift,
    paired_auuc_difference,
    paired_auuc_resamples,
)

ROWS = 8_000
SAMPLES = 200
SEED = 96


@dataclass(frozen=True)
class Holdout:
    risk: np.ndarray
    tau: np.ndarray
    t: np.ndarray
    y: np.ndarray


def holdout(effect: str, *, seed: int = 7, rows: int = ROWS) -> Holdout:
    """A randomised hold-out (half treated) with a planted effect."""
    rng = np.random.default_rng(seed)
    risk = 0.1 + 0.4 * rng.random(rows)
    if effect == "risk":
        tau = 0.5 * (risk - 0.1)  # the riskiest gain the most
    elif effect == "heterogeneous":
        tau = 0.25 * rng.random(rows)  # unrelated to risk
    elif effect == "equal":
        tau = np.full(rows, 0.05)
    else:
        raise AssertionError(effect)
    t = rng.integers(0, 2, rows)
    y = (rng.random(rows) < risk + t * tau).astype(int)
    return Holdout(risk=risk, tau=tau, t=t, y=y)


def compare(pred: np.ndarray, data: Holdout, *, propensity: np.ndarray | None = None):
    baselines = [
        BaselineInput("p_control", data.risk),
        BaselineInput("p_treated", data.risk + data.tau),
        (
            BaselineInput("propensity_model", None, reason="This use case has no approved propensity model.")
            if propensity is None
            else BaselineInput("propensity_model", propensity, model_id="m_prop")
        ),
    ]
    return compare_with_baselines(pred, data.t, data.y, baselines, samples=SAMPLES, seed=SEED)


def test_uplift_equal_to_minus_risk_does_not_beat_risk_ranking() -> None:
    data = holdout("risk")
    result = compare(-data.risk, data)
    assert result.risk_baseline == "p_control"
    assert result.beats_risk is False
    gap = next(row for row in result.baselines if row.baseline == "p_control").difference
    assert gap is not None and gap.ci_high is not None and gap.ci_high < 0.0
    assert "does not beat risk ranking" in result.summary
    assert "lies below zero" in result.summary


def test_a_model_that_knows_a_heterogeneous_effect_beats_risk_ranking() -> None:
    data = holdout("heterogeneous")
    result = compare(data.tau, data)
    assert result.beats_risk is True
    gap = next(row for row in result.baselines if row.baseline == "p_control").difference
    assert gap is not None and gap.ci_low is not None and gap.ci_low > 0.0
    assert result.summary.startswith("Ranking by predicted uplift beats risk ranking")


def test_on_an_equal_effect_the_paired_interval_covers_zero_and_says_so() -> None:
    data = holdout("equal")
    noise = np.random.default_rng(3).random(ROWS)
    result = compare(noise, data)
    gap = next(row for row in result.baselines if row.baseline == "p_control").difference
    assert gap is not None and gap.ci_low is not None and gap.ci_high is not None
    assert gap.ci_low <= 0.0 <= gap.ci_high
    assert result.beats_risk is False
    assert "does not beat risk ranking" in result.summary
    assert "includes zero" in result.summary


def test_the_approved_propensity_model_decides_when_it_was_scored() -> None:
    data = holdout("heterogeneous")
    # A propensity model that happens to rank by the effect itself: uplift cannot beat it.
    result = compare(data.tau, data, propensity=data.tau + 1.0)
    assert result.risk_baseline == "propensity_model"
    assert result.beats_risk is False
    by_kind = {row.baseline: row for row in result.baselines}
    assert by_kind["p_control"].uplift_better is True  # it still beats plain p_control
    assert by_kind["propensity_model"].model_id == "m_prop"
    assert "the approved propensity model's score" in result.summary


def test_a_baseline_that_could_not_be_computed_is_listed_with_its_reason() -> None:
    data = holdout("heterogeneous")
    result = compare(data.tau, data)
    missing = next(row for row in result.baselines if row.baseline == "propensity_model")
    assert missing.available is False
    assert missing.auuc is None and missing.difference is None and missing.uplift_better is None
    assert missing.reason == "This use case has no approved propensity model."
    assert [row.baseline for row in result.baselines] == ["p_control", "p_treated", "propensity_model"]


def test_the_uplift_resamples_are_the_evaluations_own() -> None:
    """Same seed, same draws: the comparison's uplift AUUC interval IS the evaluation's."""
    data = holdout("heterogeneous", rows=3_000)
    evaluation, _ = evaluate_uplift(
        data.tau,
        data.t,
        data.y,
        run_id="r",
        learner="x_learner",  # type: ignore[arg-type]
        base_model="lightgbm",  # type: ignore[arg-type]
        bootstrap_samples=SAMPLES,
        seed=SEED,
        causal=True,
    )
    result = compare(data.tau, data)
    assert result.uplift_auuc == evaluation.auuc


def test_the_bootstrap_is_paired() -> None:
    """A ranking compared with itself differs by exactly zero in every resample (an unpaired
    bootstrap would give a wide interval), and a shifted copy of the same ranking likewise."""
    data = holdout("heterogeneous", rows=3_000)
    same = paired_auuc_difference(data.tau, data.tau + 5.0, data.t, data.y, samples=SAMPLES, seed=SEED)
    assert same.value == 0.0 and same.ci_low == 0.0 and same.ci_high == 0.0
    # Two similar rankings: paired, their resampled AUUCs move together and the difference is narrow.
    close = data.tau + 0.02 * np.random.default_rng(5).random(data.tau.shape[0])
    _, a = paired_auuc_resamples(data.tau, data.t, data.y, samples=SAMPLES, seed=SEED)
    _, b = paired_auuc_resamples(close, data.t, data.y, samples=SAMPLES, seed=SEED)
    _, unpaired = paired_auuc_resamples(close, data.t, data.y, samples=SAMPLES, seed=SEED + 1)
    assert np.std(a - b) < 0.5 * np.std(a - unpaired)


# ---------------------------------------------------------------------------
# calibration_by_decile
# ---------------------------------------------------------------------------
def test_a_model_predicting_the_true_effect_is_calibrated() -> None:
    data = holdout("heterogeneous")
    result = calibration_by_decile(data.tau, data.t, data.y, samples=SAMPLES, seed=SEED)
    assert len(result.deciles) == 10
    assert result.deciles_with_interval == 10
    assert result.well_calibrated is True
    assert result.deciles_covered >= 8
    assert result.summary.startswith("Predicted uplift matches what was measured")


def test_a_model_that_triples_the_effect_is_not() -> None:
    data = holdout("heterogeneous")
    result = calibration_by_decile(3.0 * data.tau, data.t, data.y, samples=SAMPLES, seed=SEED)
    assert result.well_calibrated is False
    assert "does not match" in result.summary


def test_the_weighted_gap_is_the_row_weighted_mean_absolute_gap() -> None:
    data = holdout("heterogeneous", rows=2_005)
    result = calibration_by_decile(data.tau, data.t, data.y, samples=50, seed=SEED)
    rows = np.array([d.rows for d in result.deciles])
    gaps = np.array([abs(d.observed_uplift.value - d.predicted_uplift) for d in result.deciles])  # type: ignore[union-attr]
    assert rows.sum() == 2_005
    assert result.weighted_abs_gap == pytest.approx(float((rows * gaps).sum() / rows.sum()))
    for decile in result.deciles:
        observed = decile.observed_uplift
        assert observed is not None and observed.ci_low is not None and observed.ci_high is not None
        assert decile.within_interval == (observed.ci_low <= decile.predicted_uplift <= observed.ci_high)


def test_the_point_estimates_are_the_decile_tables() -> None:
    from engine.uplift.metrics import decile_table

    data = holdout("heterogeneous", rows=3_000)
    table = decile_table(data.tau, data.t, data.y)
    result = calibration_by_decile(data.tau, data.t, data.y, samples=50, seed=SEED)
    for row, decile in zip(table, result.deciles, strict=True):
        assert decile.rows == row.rows
        assert decile.predicted_uplift == pytest.approx(row.predicted_uplift)
        assert decile.observed_uplift is not None
        assert decile.observed_uplift.value == pytest.approx(row.observed_uplift)


def test_too_few_deciles_with_an_interval_are_not_judged() -> None:
    t = np.array([1, 0] * 3 + [1] * 14)
    y = np.array([1, 0, 0, 1, 1, 0] + [1] * 14)
    pred = np.linspace(1.0, 0.0, t.shape[0])
    result = calibration_by_decile(pred, t, y, samples=20, seed=1)
    assert result.well_calibrated is None
    assert result.summary.startswith("Not judged")
