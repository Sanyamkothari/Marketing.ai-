"""The scoring run's prediction as the adjustment: honest intervals on a yes/no outcome (Plan K M135, DEC-1324).

A campaign measured on a yes/no outcome whose registered plan says `covariate_source: run_score` reports,
beside the Newcombe interval, the adjusted one (`adjusted_interval`): M102's estimator on the 0/1 outcomes,
with the chance of the outcome the scoring run predicted for each customer before the campaign as the
covariate. Checked on simulated campaigns (`predicted_campaign`: the outcome's chance depends on two
features, the prediction is computed from the same features with noise and fixed before the treatment),
measured by the product's `measure_incrementality` exactly as `measure_campaign` calls it, against the band
of `tests/statistical/bands.py` (DEC-1305: the nominal value +/- 4 Monte Carlo standard errors):

a. coverage: with a known effect the adjusted 95% interval covers the true effect within the band, and the
   adjustment removes variance on average (`variance_reduction` > 0);
b. false positives: with no effect the adjusted interval excludes zero about 5% of the time, no more;
c. a useless prediction (pure noise): the adjusted interval is not materially wider than the unadjusted one
   (the safety property: registering a prediction that turns out worthless costs almost nothing).

Each simulation's truth is `predicted_campaign`'s `true_effect`, never the measured value. Nothing here
promises a gain for a real campaign: the report measures it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import numpy as np
import pytest

from engine.measurement.simulate import predicted_campaign
from engine.uplift.contracts import IncrementalityReport
from engine.uplift.incrementality import measure_incrementality
from tests.statistical.bands import assert_share_in_band, seeds

pytestmark = pytest.mark.statistical

SIMS = 2_000
FALSE_POSITIVE_SIMS = 4_000
NOMINAL = 0.95
ALPHA = 0.05
WIDER_TOLERANCE = 0.02
"""How much wider than the unadjusted interval the adjusted one may be with a useless prediction: 2% at most
per campaign, and 0.5% on average (a regression slope estimated on noise costs about one customer's worth
of degrees of freedom; with thousands per arm that is far below either)."""


@contextmanager
def _quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def _measure_many(
    base_seed: int, sims: int, n: int, base_rate: float, effect: float, **options: object
) -> list[tuple[IncrementalityReport, float]]:
    reports = []
    with _quiet():
        for seed in seeds(base_seed, sims):
            campaign = predicted_campaign(n, base_rate, effect, seed=seed, **options)  # type: ignore[arg-type]
            report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.adjusted_kwargs)
            reports.append((report, campaign.true_effect))
    return reports


def _width(low: float | None, high: float | None) -> float:
    assert low is not None and high is not None
    return high - low


def test_a_the_adjusted_interval_covers_the_true_effect_and_removes_variance() -> None:
    draws = _measure_many(135_001, SIMS, 4_000, 0.15, 0.03)
    covered = 0
    reductions = []
    for report, truth in draws:
        interval = report.adjusted_interval
        assert interval is not None and interval.ci_low is not None and interval.ci_high is not None
        covered += interval.ci_low <= truth <= interval.ci_high
        assert report.variance_reduction is not None
        reductions.append(report.variance_reduction)
    assert_share_in_band(covered / SIMS, NOMINAL, SIMS, what="adjusted coverage, informative prediction")
    assert float(np.mean(reductions)) > 0.0, float(np.mean(reductions))


def test_a_sparse_outcome_is_covered_too() -> None:
    """A 3% outcome (sparse, as retention outcomes are): coverage holds; the gain is whatever it is."""
    draws = _measure_many(135_002, SIMS, 10_000, 0.03, 0.01)
    covered = sum(
        r.adjusted_interval is not None
        and r.adjusted_interval.ci_low is not None
        and r.adjusted_interval.ci_high is not None
        and r.adjusted_interval.ci_low <= truth <= r.adjusted_interval.ci_high
        for r, truth in draws
    )
    assert_share_in_band(covered / SIMS, NOMINAL, SIMS, what="adjusted coverage, a 3% outcome")


def test_b_with_no_effect_the_adjusted_interval_excludes_zero_about_five_percent_of_the_time() -> None:
    draws = _measure_many(135_003, FALSE_POSITIVE_SIMS, 3_000, 0.15, 0.0)
    excluded = 0
    for report, _truth in draws:
        interval = report.adjusted_interval
        assert interval is not None
        excluded += interval.excludes_zero
    assert_share_in_band(
        excluded / FALSE_POSITIVE_SIMS, ALPHA, FALSE_POSITIVE_SIMS, what="false positives, adjusted interval"
    )


def test_c_a_useless_prediction_does_not_widen_the_interval() -> None:
    draws = _measure_many(135_004, SIMS, 4_000, 0.15, 0.03, prediction="noise")
    ratios = []
    covered = 0
    for report, truth in draws:
        adjusted, plain = report.adjusted_interval, report.absolute_lift
        assert adjusted is not None and plain is not None
        ratios.append(_width(adjusted.ci_low, adjusted.ci_high) / _width(plain.ci_low, plain.ci_high))
        assert adjusted.ci_low is not None and adjusted.ci_high is not None
        covered += adjusted.ci_low <= truth <= adjusted.ci_high
    assert max(ratios) <= 1.0 + WIDER_TOLERANCE, max(ratios)
    assert float(np.mean(ratios)) <= 1.005, float(np.mean(ratios))
    assert_share_in_band(covered / SIMS, NOMINAL, SIMS, what="adjusted coverage, useless prediction")
