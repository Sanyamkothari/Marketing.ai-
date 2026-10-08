"""Plan J M96: the cross-fitted, equal-budget comparison of uplift and risk ranking.

The fold-leak tests are the acceptance criterion "the cross-fit never scores a row with a model
trained on it": the fitters record, by the rows' own index labels, every row they were trained on and
every row they scored, and no scored row may be among its model's training rows - for both schemes,
with and without customer groups. The rest pins equal budgets, the identity with `evaluate_policy`,
the ring's folds, the fold AUUC and the refit cost estimate. All fail on the code before M96, which
had no `engine.measurement.compare`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

from engine.measurement.compare import (
    FoldScores,
    cross_fit,
    equal_budget_comparison,
    estimate_refit_seconds,
    fold_assignment,
    fold_auuc_not_computed,
    fold_auuc_report,
    top_share_policy,
    training_folds,
)
from engine.measurement.simulate import uplift_population
from tests.statistical.risk_comparison import linear_risk_fitter, linear_uplift_fitter


@dataclass
class Recorder:
    """Fitters that remember what each model was trained on and what it scored, by index label."""

    uplift_fits: list[tuple[set[object], set[object]]] = field(default_factory=list)
    risk_fits: list[tuple[set[object], set[object]]] = field(default_factory=list)

    def uplift(
        self, features: pd.DataFrame, t: np.ndarray, y: np.ndarray
    ) -> Callable[[pd.DataFrame], FoldScores]:
        trained = set(features.index)
        scored: set[object] = set()
        self.uplift_fits.append((trained, scored))
        inner = linear_uplift_fitter(features, t, y)

        def predict(frame: pd.DataFrame) -> FoldScores:
            scored.update(frame.index)
            return inner(frame)

        return predict

    def risk(self, features: pd.DataFrame, y: np.ndarray) -> Callable[[pd.DataFrame], np.ndarray]:
        trained = set(features.index)
        scored: set[object] = set()
        self.risk_fits.append((trained, scored))
        inner = linear_risk_fitter(features, y)

        def score(frame: pd.DataFrame) -> np.ndarray:
            scored.update(frame.index)
            return inner(frame)

        return score


def _population(rows: int = 1_200, seed: int = 11):
    population = uplift_population(rows, seed=seed, effect="heterogeneous")
    # Index labels unlike positions, so a fitter handed rows by label cannot be fooled by positions.
    features = population.features.set_axis([f"row-{i * 7 + 3}" for i in range(rows)])
    return population, features


@pytest.mark.parametrize("scheme", ["k_fold", "ring"])
@pytest.mark.parametrize("grouped", [False, True])
def test_no_row_is_ever_scored_by_a_model_trained_on_it(scheme: str, grouped: bool) -> None:
    population, features = _population()
    groups = np.array([f"c{i // 3}" for i in range(len(features.index))]) if grouped else None
    recorder = Recorder()
    cross = cross_fit(
        features,
        population.t,
        population.y,
        folds=5,
        seed=3,
        fit_uplift=recorder.uplift,
        fit_risk=recorder.risk,
        scheme=scheme,  # type: ignore[arg-type]
        groups=groups,
    )
    assert len(recorder.uplift_fits) == len(recorder.risk_fits) == 5
    every_scored: list[object] = []
    for trained, scored in [*recorder.uplift_fits, *recorder.risk_fits]:
        assert scored, "each fold model scored its fold"
        assert not trained & scored, "a row was scored by a model trained on it"
    for _, scored in recorder.uplift_fits:
        every_scored.extend(scored)
    assert sorted(every_scored) == sorted(features.index), "every row is scored exactly once"
    for fold, positions in enumerate(cross.trained_on):
        assert not set(positions) & set(np.flatnonzero(cross.fold_of == fold))
    if grouped:
        assert groups is not None
        for customer in set(groups.tolist()):
            assert len(set(cross.fold_of[groups == customer].tolist())) == 1, "a customer's rows share a fold"


def test_the_ring_trains_each_fold_on_the_next_folds_only() -> None:
    assert training_folds(0, folds=5, scheme="ring") == (1, 2)
    assert training_folds(4, folds=5, scheme="ring") == (0, 1)
    assert training_folds(2, folds=5, scheme="k_fold") == (0, 1, 3, 4)
    assert training_folds(0, folds=7, scheme="ring") == (1, 2, 3)
    # For any two folds, one of them never trained the other's models (why the interval is honest).
    for folds in (3, 5, 6, 10):
        for a in range(folds):
            for b in range(folds):
                if a != b:
                    assert a not in training_folds(b, folds=folds, scheme="ring") or b not in training_folds(
                        a, folds=folds, scheme="ring"
                    )
    with pytest.raises(ValueError, match="at least 3 folds"):
        training_folds(0, folds=2, scheme="ring")


def test_folds_are_stratified_on_treatment_and_outcome() -> None:
    population, _ = _population(rows=2_000)
    fold_of = fold_assignment(population.t, population.y, folds=5, seed=1)
    for stratum in range(4):
        members = (population.t * 2 + population.y) == stratum
        counts = np.bincount(fold_of[members], minlength=5)
        assert counts.max() - counts.min() <= 1


def test_both_rankings_spend_the_same_budget_in_every_fold() -> None:
    fold_of = np.repeat(np.arange(4), [10, 11, 9, 13])
    rng = np.random.default_rng(0)
    a = top_share_policy(rng.random(43), fold_of, top_share=0.3)
    b = top_share_policy(rng.random(43), fold_of, top_share=0.3)
    for fold in range(4):
        assert a[fold_of == fold].sum() == b[fold_of == fold].sum() == np.ceil(0.3 * (fold_of == fold).sum())


def _compare(cost: float | None = 25.0, effect: str = "heterogeneous"):
    population = uplift_population(3_000, seed=8, effect=effect)  # type: ignore[arg-type]
    cross = cross_fit(
        population.features,
        population.t,
        population.y,
        folds=5,
        seed=8,
        fit_uplift=linear_uplift_fitter,
        fit_risk=linear_risk_fitter,
        scheme="ring",
    )
    report = equal_budget_comparison(
        t=population.t,
        y=population.y,
        propensity=population.propensity,
        cross=cross,
        top_share=0.25,
        cost_per_contact=cost,
        run_id="r",
        causal=True,
        propensity_source="recorded",
    )
    return population, cross, report


def test_the_comparison_goes_through_evaluate_policy_and_pairs_the_difference() -> None:
    population, cross, report = _compare()
    rows = population.t.shape[0]
    contacts = int(sum(np.ceil(0.25 * np.bincount(cross.fold_of))))
    assert report.uplift.contacts == report.risk.contacts == contacts
    for value in (report.uplift, report.risk):
        dr = next(e.value.value for e in value.ope.estimates if e.method == "dr")
        assert value.incremental_conversions.value == pytest.approx(
            rows * (dr - value.ope.treat_none_value.value)
        )
        assert value.per_rupee is not None
        assert value.per_rupee.value == pytest.approx(value.incremental_conversions.value / (contacts * 25.0))
    assert report.difference.value == pytest.approx(
        report.uplift.incremental_conversions.value - report.risk.incremental_conversions.value
    )
    assert report.difference_per_rupee is not None
    assert report.difference_per_rupee.ci_low == pytest.approx(report.difference.ci_low / (contacts * 25.0))  # type: ignore[operator]
    assert report.propensity_source == "recorded" and report.rows == rows and report.folds == 5


def test_without_a_cost_per_contact_nothing_is_said_per_rupee() -> None:
    _, _, report = _compare(cost=None)
    assert report.uplift.per_rupee is None and report.risk.per_rupee is None
    assert report.difference_per_rupee is None


def test_the_verdict_and_sentence_follow_the_paired_interval() -> None:
    _, _, report = _compare(effect="null")
    assert report.uplift_better is (report.difference.ci_low is not None and report.difference.ci_low > 0.0)
    assert ("does not beat risk ranking" in report.summary) is not report.uplift_better


def test_the_comparison_needs_a_ring_cross_fit_with_a_risk_model() -> None:
    population = uplift_population(600, seed=2, effect="null")
    cross = cross_fit(
        population.features,
        population.t,
        population.y,
        folds=5,
        seed=2,
        fit_uplift=linear_uplift_fitter,
        fit_risk=None,
        scheme="k_fold",
    )
    assert np.isnan(cross.risk).all()
    with pytest.raises(ValueError, match="ring cross-fit"):
        equal_budget_comparison(
            t=population.t,
            y=population.y,
            propensity=population.propensity,
            cross=cross,
            top_share=0.2,
            cost_per_contact=None,
            run_id="r",
            causal=True,
            propensity_source="recorded",
        )


# ---------------------------------------------------------------------------
# Fold AUUC and the refit cost
# ---------------------------------------------------------------------------
def test_fold_auuc_is_measured_on_each_fold_by_a_model_that_never_saw_it() -> None:
    population = uplift_population(4_000, seed=5, effect="heterogeneous")
    cross = cross_fit(
        population.features,
        population.t,
        population.y,
        folds=4,
        seed=5,
        fit_uplift=linear_uplift_fitter,
        fit_risk=None,
    )
    report = fold_auuc_report(cross, population.t, population.y, estimated_refit_seconds=12.0)
    assert report.computed is True and report.folds == 4
    assert [value.fold for value in report.values] == [1, 2, 3, 4]
    assert sum(value.rows for value in report.values) == 4_000
    measured = [value.auuc for value in report.values]
    assert all(value is not None for value in measured)
    assert report.stable is all(value > 0.0 for value in measured if value is not None)
    assert report.minimum == min(v for v in measured if v is not None)
    assert report.estimated_refit_seconds == 12.0 and report.refit_seconds is not None


def test_a_fold_that_does_no_better_than_random_is_unstable() -> None:
    population = uplift_population(1_000, seed=9, effect="null")
    cross = cross_fit(
        population.features,
        population.t,
        population.y,
        folds=5,
        seed=9,
        fit_uplift=linear_uplift_fitter,
        fit_risk=None,
    )
    report = fold_auuc_report(cross, population.t, population.y, estimated_refit_seconds=None)
    if any(value.auuc is not None and value.auuc <= 0.0 for value in report.values):
        assert report.stable is False
        assert report.summary.startswith("Unstable across folds")


def test_when_not_run_it_says_why_and_what_it_would_cost() -> None:
    report = fold_auuc_not_computed(folds=5, reason="Off by default.", estimated_refit_seconds=125.0)
    assert report.computed is False and report.stable is None and report.values == ()
    assert "refits the model 5 times: about 2 minutes" in report.summary


def test_the_refit_estimate_scales_one_fit_to_the_fold_sizes() -> None:
    # One fit of 700 rows took 7 s; 5 K-fold refits of 800 of 1,000 rows each take 5 x 8 s.
    assert estimate_refit_seconds(7.0, folds=5, rows_all=1_000, rows_fitted=700) == 40.0
    assert estimate_refit_seconds(7.0, folds=5, rows_all=1_000, rows_fitted=700, schemes=("ring",)) == 20.0
    assert estimate_refit_seconds(None, folds=5, rows_all=1_000, rows_fitted=700) is None
