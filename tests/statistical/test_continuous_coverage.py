"""Amounts: the 95% intervals cover the true difference in means 95% of the time (Plan J M102, the M95 band).

A campaign measured on revenue reports two intervals: Welch's on the difference in means
(`mean_difference_ci`) and, when the test plan registered an amount from before the campaign, the
adjusted (CUPED) one (`adjusted_interval`). Both are checked here on 2,000 simulated campaigns per case,
measured by the product's `measure_incrementality` exactly as `measure_campaign` calls it, against the
band of `tests/statistical/bands.py` (95% +/- 4 Monte Carlo standard errors = +/- 1.95 points):

* revenue that is roughly normal, with a covariate correlated 0.6 (the adjustment then removes about
  0.36 of the variance, which is checked too) and with an uncorrelated one (the adjustment then changes
  nothing);
* revenue that is mostly zeros with a long tail (80% of customers spend nothing; a payer spends a
  lognormal amount), at the size Kohavi et al.'s rule asks for (more than 355 g² customers per arm,
  about 9,300 here). Below that size the report must say the range may be too narrow
  (`OUTCOME_SKEWED`), which is checked on smaller campaigns;
* the planner's own n for an amount with an expected rho² of 0.36 has the planned 80% power.

Each simulation's truth is `revenue_campaign`'s `true_effect`, never the measured value.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import numpy as np
import pytest

from engine.measurement.continuous import OUTCOME_SKEWED
from engine.measurement.planner import n_for_mde_continuous
from engine.measurement.simulate import revenue_campaign
from engine.uplift.contracts import ConfidenceValue, IncrementalityReport
from engine.uplift.incrementality import measure_incrementality
from tests.statistical.bands import assert_mean_in_band, assert_share_in_band, band, seeds

pytestmark = pytest.mark.statistical

SIMS = 2_000
NOMINAL = 0.95


@contextmanager
def _quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def _measure_many(
    base_seed: int, sims: int, n: int, effect: float, **options: object
) -> list[IncrementalityReport]:
    reports = []
    with _quiet():
        for seed in seeds(base_seed, sims):
            campaign = revenue_campaign(n, effect, seed=seed, **options)  # type: ignore[arg-type]
            reports.append(
                measure_incrementality(campaign.scores, campaign.outcomes, **campaign.adjusted_kwargs)
            )
    return reports


def _covers(value: ConfidenceValue | None, truth: float) -> bool:
    assert value is not None and value.ci_low is not None and value.ci_high is not None
    return value.ci_low <= truth <= value.ci_high


def _coverage(reports: list[IncrementalityReport], truth: float) -> tuple[float, float]:
    plain = sum(_covers(r.mean_difference_ci, truth) for r in reports) / len(reports)
    adjusted = sum(_covers(r.adjusted_interval, truth) for r in reports) / len(reports)
    return plain, adjusted


def test_both_intervals_cover_with_a_correlated_covariate_and_it_removes_036() -> None:
    reports = _measure_many(102_201, SIMS, 2_000, 4.0, rho=0.6)
    plain, adjusted = _coverage(reports, 4.0)
    assert_share_in_band(plain, NOMINAL, SIMS, what="Welch coverage, normal revenue")
    assert_share_in_band(adjusted, NOMINAL, SIMS, what="adjusted coverage, normal revenue, rho 0.6")
    reductions = np.array([r.variance_reduction for r in reports], dtype=float)
    assert abs(reductions.mean() - 0.36) <= 0.03, reductions.mean()
    assert_mean_in_band(reductions.tolist(), 0.36, what="variance removed at rho 0.6")


def test_an_uncorrelated_covariate_gives_about_the_unadjusted_result() -> None:
    reports = _measure_many(102_202, SIMS, 2_000, 4.0, rho=0.0)
    plain, adjusted = _coverage(reports, 4.0)
    assert_share_in_band(adjusted, NOMINAL, SIMS, what="adjusted coverage, uncorrelated covariate")
    assert abs(plain - adjusted) <= 0.01, (plain, adjusted)
    reductions = np.array([r.variance_reduction for r in reports], dtype=float)
    assert abs(reductions.mean()) < 0.005, reductions.mean()
    # how far the adjustment moves the estimate, as a share of the unadjusted half-range
    gaps = []
    for r in reports:
        ci = r.mean_difference_ci
        assert ci is not None and ci.ci_low is not None and ci.ci_high is not None
        assert r.adjusted_lift is not None and r.mean_difference is not None
        gaps.append(abs(r.adjusted_lift - r.mean_difference) / ((ci.ci_high - ci.ci_low) / 2.0))
    assert float(np.median(gaps)) < 0.05, float(np.median(gaps))


def test_both_intervals_cover_on_zero_inflated_long_tailed_revenue() -> None:
    """80% spend nothing, payers spend 500 x e^(0.75 e): at 10,000 per arm (the rule asks for ~9,300)."""
    reports = _measure_many(102_203, SIMS, 20_000, 10.0, rho=0.6, shape="zero_inflated_lognormal")
    plain, adjusted = _coverage(reports, 10.0)
    assert_share_in_band(plain, NOMINAL, SIMS, what="Welch coverage, zero-inflated lognormal revenue")
    assert_share_in_band(adjusted, NOMINAL, SIMS, what="adjusted coverage, zero-inflated lognormal revenue")


def test_a_long_tail_measured_too_small_says_its_range_may_be_too_narrow() -> None:
    small = _measure_many(102_204, 500, 1_000, 10.0, rho=0.6, shape="zero_inflated_lognormal")
    flagged = sum(r.outcome_warnings == (OUTCOME_SKEWED,) for r in small) / len(small)
    assert flagged >= 0.95, flagged


def test_the_planners_n_for_an_amount_with_rho2_has_its_power() -> None:
    sizes = n_for_mde_continuous(40.0, 4.0, rho2=0.36)
    assert sizes.n_treat is not None and sizes.n_control == sizes.n_treat
    n = 2 * sizes.n_treat
    reports = _measure_many(102_205, SIMS, n, 4.0, rho=0.6)
    seen = sum(r.adjusted_interval is not None and r.adjusted_interval.excludes_zero for r in reports) / SIMS
    low, high = band(0.80, SIMS)
    assert (
        low <= seen <= high
    ), f"power {seen:.4f} at the planner's n={sizes.n_treat} per arm, band [{low:.4f}, {high:.4f}]"
