"""Plan J M96: the cross-fitted, equal-budget comparison of uplift and risk ranking.

The fold-leak tests are the acceptance criterion "the cross-fit never scores a row with a model
trained on it": the fitters record, by the rows' own index labels, every row they were trained on and
every row they scored, and no scored row may be among its model's training rows - for both schemes,
with and without customer groups. The rest pins equal budgets, the identity with `evaluate_policy`,
the ring's folds, the fold AUUC and the refit cost estimate. All fail on the code before M96, which
had no `engine.measurement.compare`.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

from engine.measurement.compare import (
    CrossFit,
    FoldScores,
    cross_fit,
    equal_budget_comparison,
    estimate_refit_seconds,
    fold_assignment,
    fold_auuc_not_computed,
    fold_auuc_report,
    rollout_verdict,
    top_share_policy,
    training_folds,
)
from engine.measurement.simulate import uplift_population
from engine.pilot.roi import ValueCosts
from engine.uplift.config import UpliftPolicyConfig
from engine.uplift.contracts import RiskComparison, RolloutVerdict
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


def test_tied_scores_are_not_cut_in_file_order() -> None:
    # A file that lists its treated customers first, and a score with one tied block straddling the
    # cut: with ties in input order the top share was all treated, so pi depended on t, not on x.
    rows = 2_000
    t = np.r_[np.ones(rows // 2, dtype=int), np.zeros(rows // 2, dtype=int)]
    fold_of = np.zeros(rows, dtype=int)
    scores = np.full(rows, 0.5)
    policy = top_share_policy(scores, fold_of, top_share=0.2, seed=4)
    assert policy.sum() == 400
    treated_share = float(t[policy == 1].mean())
    assert 0.4 < treated_share < 0.6, treated_share
    # Distinct scores still decide; only the tie order is drawn, and from the seed alone.
    graded = np.where(np.arange(rows) % 10 == 0, 0.9, 0.5)
    chosen = top_share_policy(graded, fold_of, top_share=0.2, seed=4)
    assert chosen[graded == 0.9].all()
    np.testing.assert_array_equal(chosen, top_share_policy(graded, fold_of, top_share=0.2, seed=4))


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
    intervals = [value.interval for value in report.values]
    assert all(
        band is not None and band.value == value.auuc
        for band, value in zip(intervals, report.values, strict=True)
    )
    clearly_below = any(
        band is not None and band.ci_high is not None and band.ci_high <= 0.0 for band in intervals
    )
    assert report.heterogeneity is not None and report.heterogeneity_critical == 7.815  # chi-square, 3 df
    differ = report.heterogeneity > report.heterogeneity_critical
    assert report.stable is (not clearly_below and not differ)
    assert report.bootstrap_samples == 200
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
    if any(
        value.interval is not None and value.interval.ci_high is not None and value.interval.ci_high <= 0.0
        for value in report.values
    ):
        assert report.stable is False
        assert report.summary.startswith("Unstable across folds")


def _planted(rows: int, *, seed: int, flipped: tuple[int, ...] = (), folds: int = 5):
    from engine.measurement.compare import CrossFit

    population = uplift_population(rows, seed=seed, effect="heterogeneous")
    fold_of = fold_assignment(population.t, population.y, folds=folds, seed=seed)
    # A model that knows the effect's direction, scored with noise as a fold model would be.
    noise = np.random.default_rng(seed + 1).normal(scale=0.05, size=rows)
    uplift = np.asarray(population.tau, dtype=np.float64) + noise
    for fold in flipped:
        uplift[fold_of == fold] *= -1.0
    cross = CrossFit(
        fold_of=fold_of,
        uplift=uplift,
        p_treated=np.full(rows, 0.5),
        p_control=np.full(rows, 0.5),
        risk=np.full(rows, np.nan),
        folds=folds,
        scheme="k_fold",
        seconds=0.0,
    )
    return cross, population.t, population.y


def test_a_sound_model_on_a_typical_file_is_not_called_unstable_by_one_noisy_fold() -> None:
    # 4,000 rows in 5 folds: a fold's AUUC has a standard error about the size of the effect, so a
    # point-estimate rule ("every fold above zero") flagged a third of sound models. Seeds where a
    # fold's point AUUC is at or below zero, judged against its own noise, are stable.
    noisy = 0
    for seed in range(40):
        cross, t, y = _planted(4_000, seed=seed)
        report = fold_auuc_report(cross, t, y, estimated_refit_seconds=None, samples=200, seed=seed)
        if any(value.auuc is not None and value.auuc <= 0.0 for value in report.values):
            noisy += 1
            if report.stable:
                assert report.summary.startswith("Stable across folds")
                return
    raise AssertionError(f"no seed of 40 gave a fold at or below zero that was judged stable ({noisy} noisy)")


def test_the_false_alarm_rate_on_sound_models_is_small() -> None:
    flagged = 0
    for seed in range(40):
        cross, t, y = _planted(4_000, seed=seed)
        report = fold_auuc_report(cross, t, y, estimated_refit_seconds=None, samples=200, seed=seed)
        flagged += report.stable is False
    assert flagged <= 6, f"{flagged} of 40 sound models were called unstable"


def test_a_fold_whose_model_ranks_backwards_is_unstable_and_named() -> None:
    cross, t, y = _planted(10_000, seed=1, flipped=(2,))
    report = fold_auuc_report(cross, t, y, estimated_refit_seconds=None, samples=200, seed=1)
    assert report.stable is False
    assert report.summary.startswith("Unstable across folds")
    assert "fold 3" in report.summary
    assert "95% CI" in report.summary
    third = report.values[2]
    assert third.interval is not None and third.interval.ci_high is not None and third.interval.ci_high <= 0.0


def test_when_not_run_it_says_why_and_what_it_would_cost() -> None:
    report = fold_auuc_not_computed(folds=5, reason="Off by default.", estimated_refit_seconds=125.0)
    assert report.computed is False and report.stable is None and report.values == ()
    assert "refits the model 5 times: about 2 minutes" in report.summary


def test_the_refit_estimate_scales_one_fit_to_the_fold_sizes() -> None:
    # One fit of 700 rows took 7 s; 5 K-fold refits of 800 of 1,000 rows each take 5 x 8 s.
    assert estimate_refit_seconds(7.0, folds=5, rows_all=1_000, rows_fitted=700) == 40.0
    assert estimate_refit_seconds(7.0, folds=5, rows_all=1_000, rows_fitted=700, schemes=("ring",)) == 20.0
    assert estimate_refit_seconds(None, folds=5, rows_all=1_000, rows_fitted=700) is None


# ---------------------------------------------------------------------------
# "Safe to roll out" (Plan K M137, DEC-1325). Fails on the code before M137: no `rollout_verdict`.
# ---------------------------------------------------------------------------
Z95_ONE_SIDED = 1.6448536269514722
"""`NormalDist().inv_cdf(0.95)`, written out so the hand computations do not call the code under test."""


def _hand_bounds(terms: list[float], z: float = Z95_ONE_SIDED) -> tuple[float, float]:
    n = len(terms)
    mean = sum(terms) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in terms) / (n - 1))
    return n * mean - z * math.sqrt(n) * sd, n * mean + z * math.sqrt(n) * sd


def _verdict(terms: list[float], confidence: float = 0.95) -> RolloutVerdict:
    paired = np.asarray(terms, dtype=np.float64)
    return rollout_verdict(
        paired,
        np.sign(paired),
        confidence=confidence,
        gamma=np.abs(paired),
        treated_terms=np.zeros(paired.shape[0]),
    )


def test_a_clear_gain_is_safe_with_hand_computed_one_sided_bounds() -> None:
    terms = [1.0, 2.0, 3.0, 2.0, 2.0]
    verdict = _verdict(terms)
    low, high = _hand_bounds(terms)  # 10 -/+ 1.6449 x sqrt(5) x sqrt(0.5) = 7.399, 12.601
    assert low == pytest.approx(7.399, abs=1e-3) and high == pytest.approx(12.601, abs=1e-3)
    assert verdict.outcome == "safe" and verdict.confidence == 0.95
    assert verdict.lower_bound_conversions == pytest.approx(low)
    assert verdict.upper_bound_conversions == pytest.approx(high)
    assert verdict.rows_needed is None
    assert verdict.value_basis == "conversions"
    assert verdict.lower_bound_inr is None and verdict.upper_bound_inr is None
    assert verdict.rows_without_value is None and verdict.inr_unavailable_reason is None
    assert verdict.summary == (
        "Safe to switch from risk ranking to this model at this budget: at least 7.4 extra conversions "
        "(95% one-sided)."
    )


def test_a_clear_loss_is_not_safe() -> None:
    terms = [-1.0, -2.0, -3.0, -2.0, -2.0]
    verdict = _verdict(terms)
    low, high = _hand_bounds(terms)
    assert verdict.outcome == "not_safe" and high < 0.0
    assert verdict.lower_bound_conversions == pytest.approx(low)
    assert verdict.upper_bound_conversions == pytest.approx(high)
    assert verdict.rows_needed is None
    assert verdict.summary == (
        "Not safe: risk ranking does better at this budget, by at least 7.4 conversions (95% one-sided)."
    )


def test_an_unclear_gain_cannot_tell_and_says_how_many_rows_would_settle_it() -> None:
    terms = [1.0, -1.0, 2.0, -1.0, 0.5]
    verdict = _verdict(terms)
    low, high = _hand_bounds(terms)
    assert low < 0.0 < high and verdict.outcome == "cannot_tell"
    mean = sum(terms) / 5
    sd = math.sqrt(sum((x - mean) ** 2 for x in terms) / 4)
    needed = math.ceil((Z95_ONE_SIDED * sd / mean) ** 2)
    assert verdict.rows_needed == needed
    # At that many rows, with this mean and sd, the lower bound would just clear zero.
    assert needed * mean - Z95_ONE_SIDED * math.sqrt(needed) * sd > 0.0
    assert (needed - 1) * mean - Z95_ONE_SIDED * math.sqrt(needed - 1) * sd <= 0.0
    assert verdict.summary == (
        f"Cannot tell yet: about {needed:,} rows would settle it if the gain seen here is real "
        "(this comparison used 5)."
    )


def test_no_rows_needed_when_no_gain_was_seen() -> None:
    for terms in ([-1.0, 1.0, -0.5, 0.5, -0.2], [1.0, -1.0, 1.0, -1.0]):
        verdict = _verdict(terms)
        assert verdict.outcome == "cannot_tell" and sum(terms) <= 0.0
        assert verdict.rows_needed is None
        assert verdict.summary.startswith("Cannot tell yet: at this budget this model shows no gain")


def test_the_confidence_is_the_one_sided_level_asked_for() -> None:
    terms = [1.0, 2.0, 3.0, 2.0, 2.0]
    verdict = _verdict(terms, confidence=0.995)
    z = 2.5758293035489004  # NormalDist().inv_cdf(0.995)
    assert verdict.lower_bound_conversions == pytest.approx(_hand_bounds(terms, z)[0])
    assert "99.5% one-sided" in verdict.summary
    with pytest.raises(ValueError, match=r"between 0\.5 and 1"):
        _verdict(terms, confidence=1.0)
    with pytest.raises(ValueError, match="at least two"):
        _verdict([1.0])


def test_rows_needed_rounds_up_in_the_sentence_only() -> None:
    from engine.measurement.compare import _about

    assert [_about(n) for n in (7, 95, 100, 40_123, 41_000, 1_234_567)] == [
        "7",
        "95",
        "100",
        "41,000",
        "41,000",
        "1,300,000",
    ]


@dataclass(frozen=True)
class Rollout:
    report: RiskComparison
    switch: np.ndarray
    gamma: np.ndarray
    treated: np.ndarray


def _rollout_compare(
    *,
    policy: UpliftPolicyConfig | None = None,
    values: np.ndarray | None = None,
    costs: ValueCosts | None = None,
    effect: str = "heterogeneous",
) -> Rollout:
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
        cost_per_contact=25.0,
        run_id="r",
        causal=True,
        propensity_source="recorded",
        rollout_confidence=0.95,
        value_policy=policy,
        row_values=values,
        value_costs=costs,
    )
    switch = top_share_policy(cross.uplift, cross.fold_of, top_share=0.25) - top_share_policy(
        cross.risk, cross.fold_of, top_share=0.25
    )
    t, y, e = population.t, population.y, population.propensity
    mu1, mu0 = cross.p_treated, cross.p_control
    gamma = mu1 - mu0 + t * (y - mu1) / e - (1 - t) * (y - mu0) / (1 - e)
    return Rollout(report=report, switch=switch, gamma=gamma, treated=mu1 + t * (y - mu1) / e)


def test_the_verdict_reads_the_comparisons_own_paired_terms() -> None:
    run = _rollout_compare()
    verdict = run.report.rollout
    assert verdict is not None
    low, high = _hand_bounds((run.switch * run.gamma).tolist())
    assert verdict.lower_bound_conversions == pytest.approx(low)
    assert verdict.upper_bound_conversions == pytest.approx(high)
    # One sum, two quantiles: the same centre as M96's two-sided interval, a narrower band.
    difference = run.report.difference
    assert (low + high) / 2 == pytest.approx(difference.value)
    assert difference.ci_low is not None and difference.ci_low < verdict.lower_bound_conversions
    # Without value inputs: no rupees, never a made-up one.
    assert verdict.value_basis == "conversions"
    assert verdict.lower_bound_inr is None and verdict.upper_bound_inr is None


def test_without_the_setting_the_report_is_m96s_and_its_json_has_no_rollout_key() -> None:
    _, _, report = _compare()
    assert report.rollout is None
    assert '"rollout"' not in report.model_dump_json()


def test_the_rupee_bounds_are_m97s_net_value_of_the_same_rows() -> None:
    from engine.pilot.roi import format_inr
    from engine.uplift.policy import customer_net_values

    policy = UpliftPolicyConfig(
        value_column="spend", margin_pct=30.0, horizon_months=6, cost_per_contact=25.0
    )
    values = np.random.default_rng(4).gamma(2.0, 400.0, size=3_000)
    values[[3, 50, 900]] = np.nan
    costs = ValueCosts(offer_cost=0.0, contact_cost=0.86)
    run = _rollout_compare(policy=policy, values=values, costs=costs)
    verdict = run.report.rollout
    assert verdict is not None and verdict.value_basis == "net_inr"
    assert verdict.rows_without_value == 3 and verdict.inr_unavailable_reason is None
    # By hand with M97's own function: Γ as the effect, the contact cost added back (it cancels).
    money = customer_net_values(run.gamma, policy, values=values, p_treated=run.treated, value_costs=costs)
    assert money.net_value is not None
    low, high = _hand_bounds((run.switch * (money.net_value + 25.0)).tolist())
    assert verdict.lower_bound_inr == pytest.approx(low)
    assert verdict.upper_bound_inr == pytest.approx(high)
    # With no offer cost that is exactly (π_u − π_r)·Γ·v_i, v_i = value × 30% × 6 months, a missing value 0.
    v = np.where(np.isfinite(values), values, 0.0) * 0.3 * 6
    assert (low, high) == pytest.approx(_hand_bounds((run.switch * run.gamma * v).tolist()))
    # The contact cost cancels: both rankings make the same number of contacts at the same cost.
    assert run.switch.sum() == 0.0
    assert float((run.switch * money.net_value).sum()) == pytest.approx(
        float((run.switch * run.gamma * v).sum())
    )
    if verdict.outcome == "safe" and low > 0:
        assert verdict.summary.endswith(f"worth at least {format_inr(low)}.")


def test_an_offer_cost_is_charged_to_every_contacted_converter_as_m97_charges_it() -> None:
    policy = UpliftPolicyConfig(value_column="spend", margin_pct=40.0)
    values = np.random.default_rng(5).gamma(2.0, 400.0, size=3_000)
    run = _rollout_compare(policy=policy, values=values, costs=ValueCosts(offer_cost=50.0, contact_cost=1.0))
    verdict = run.report.rollout
    assert verdict is not None and verdict.value_basis == "net_inr"
    # M97: net = uplift × value × margin − offer cost × p_treated − contact; doubly robust terms here.
    low, high = _hand_bounds((run.switch * (run.gamma * values * 0.4 - 50.0 * run.treated)).tolist())
    assert verdict.lower_bound_inr == pytest.approx(low)
    assert verdict.upper_bound_inr == pytest.approx(high)


def test_too_many_missing_values_give_no_rupees_as_m97_gives_no_money() -> None:
    values = np.full(3_000, 100.0)
    values[:400] = np.nan  # 13% missing, above M97's 10%
    run = _rollout_compare(policy=UpliftPolicyConfig(value_column="spend"), values=values, costs=ValueCosts())
    verdict = run.report.rollout
    assert verdict is not None and verdict.value_basis == "conversions"
    assert verdict.lower_bound_inr is None and verdict.upper_bound_inr is None
    assert verdict.rows_without_value == 400
    assert verdict.inr_unavailable_reason is not None and "400 of 3,000" in verdict.inr_unavailable_reason


def test_a_value_column_the_rows_lack_gives_no_rupees_and_says_why() -> None:
    run = _rollout_compare(policy=UpliftPolicyConfig(value_column="spend"), values=None)
    verdict = run.report.rollout
    assert verdict is not None and verdict.value_basis == "conversions"
    assert verdict.lower_bound_inr is None and verdict.rows_without_value is None
    assert verdict.inr_unavailable_reason == (
        "The rows have no 'spend' column, so the difference cannot be put in rupees."
    )


def test_one_value_per_conversion_prices_every_conversion_alike() -> None:
    run = _rollout_compare(policy=UpliftPolicyConfig(value_per_conversion=500.0, margin_pct=50.0))
    verdict = run.report.rollout
    assert verdict is not None and verdict.value_basis == "net_inr"
    low, high = _hand_bounds((run.switch * run.gamma * 250.0).tolist())
    assert verdict.lower_bound_inr == pytest.approx(low) and verdict.upper_bound_inr == pytest.approx(high)
    assert verdict.lower_bound_inr == pytest.approx(verdict.lower_bound_conversions * 250.0)


@pytest.mark.parametrize("effect", ["heterogeneous", "null", "risk"])
def test_every_sentence_is_plain(effect: str) -> None:
    from engine.pilot.plain import jargon_in

    run = _rollout_compare(effect=effect, policy=UpliftPolicyConfig(value_per_conversion=900.0))
    assert run.report.rollout is not None
    assert jargon_in(run.report.rollout.summary) == ()
    for terms in (
        [1.0, 2.0, 3.0, 2.0, 2.0],
        [-1.0, -2.0, -3.0, -2.0, -2.0],
        [1.0, -1.0, 2.0, -1.0, 0.5],
        [-1.0, 1.0, -0.5, 0.5, -0.2],
    ):
        assert jargon_in(_verdict(terms).summary) == ()


def _timed_rollout(rows: int, seed: int) -> float:
    rng = np.random.default_rng(seed)
    t = rng.integers(0, 2, rows)
    y = (rng.random(rows) < 0.2 + 0.05 * t).astype(int)
    cross = CrossFit(
        fold_of=np.arange(rows) % 5,
        uplift=rng.normal(size=rows),
        p_treated=np.full(rows, 0.25),
        p_control=np.full(rows, 0.2),
        risk=rng.random(rows),
        folds=5,
        scheme="ring",
        seconds=0.0,
    )
    values = rng.gamma(2.0, 400.0, size=rows)
    started = time.perf_counter()
    report = equal_budget_comparison(
        t=t,
        y=y,
        propensity=0.5,
        cross=cross,
        top_share=0.2,
        cost_per_contact=1.0,
        run_id="r",
        causal=True,
        propensity_source="treated_share",
        rollout_confidence=0.95,
        value_policy=UpliftPolicyConfig(value_column="spend", margin_pct=30.0),
        row_values=values,
        value_costs=ValueCosts(offer_cost=20.0, contact_cost=0.86),
    )
    elapsed = time.perf_counter() - started
    assert report.rollout is not None and report.rollout.lower_bound_inr is not None
    return elapsed


def test_the_comparison_with_its_verdict_is_linear_in_rows() -> None:
    small = min(_timed_rollout(20_000, seed) for seed in (1, 2, 3))
    large = _timed_rollout(200_000, 4)
    print(f"\n[Perf] comparison with verdict: 20,000 rows {small:.3f}s; 200,000 rows {large:.2f}s")
    # Linear work gives about 10x (sorting about 12x); quadratic about 100x. 30x leaves room for contention.
    assert large < 30 * max(small, 0.02), "ten times the rows took far more than ten times as long"


@pytest.mark.slow
def test_a_million_rows_are_compared_with_their_verdict_in_linear_time() -> None:
    small = min(_timed_rollout(100_000, seed) for seed in (5, 6))
    large = _timed_rollout(1_000_000, 7)
    print(f"\n[Perf] comparison with verdict: 100,000 rows {small:.2f}s; 1,000,000 rows {large:.2f}s")
    assert large < 30 * max(small, 0.05), "ten times the rows took far more than ten times as long"
    assert large < 60.0, f"1,000,000 rows took {large:.1f}s"
