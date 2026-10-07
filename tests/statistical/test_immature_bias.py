"""Leaving immature rows out does not bias the lift; counting them as non-converters would (Plan J M95).

A customer treated ten days ago, with a 30-day outcome window, may yet convert. The outcomes file shows
them as "not converted", which is only true so far. `measure_incrementality` excludes such rows (they are
counted in `rows_immature`, never guessed), and its docstring says why: counting them would bias the lift
towards zero. This shows both halves on simulated campaigns where 30% of customers are immature
(`engine.measurement.simulate`, model step 5: an immature customer treated `e` days ago shows a conversion
only if it fell in those `e` days, so with `e` uniform on 1..29 and the conversion day uniform on 1..30 an
immature converter is seen with probability 0.5 on average).

* **Excluded (the product's rule).** The mean lift over the simulations equals the true effect, within
  four standard errors of the mean (`tests/statistical/bands.py`).
* **Counted as observed (the rule the product does not use).** Passing no outcome window turns the
  maturity filter off, so every row counts. The mean then falls short of the truth by the amount the
  model predicts, `effect x immature_share x (1 - 0.5)`, and the test checks that it does, within the same
  band. This proves the harness can see the bias, so a pass of the first check means something.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from engine.measurement.simulate import population
from engine.uplift.incrementality import measure_incrementality
from tests.statistical.bands import assert_mean_in_band, seeds
from tests.statistical.campaigns import measure_many

pytestmark = pytest.mark.statistical

SIMS = 1_000
SEED = 95_020
N = 3_000
BASE_RATE = 0.05
EFFECT = 0.03
IMMATURE_SHARE = 0.30
MEAN_SHARE_OBSERVED = 0.5
"""The average fraction of an immature converter's conversions already visible: e / 30 averaged over e = 1..29."""


def _lifts(draws: list[Any]) -> list[float]:
    values = []
    for draw in draws:
        assert draw.report.absolute_lift is not None
        values.append(draw.report.absolute_lift.value)
    return values


def test_excluding_immature_rows_leaves_the_lift_unbiased() -> None:
    draws = measure_many(SEED, SIMS, n=N, base_rate=BASE_RATE, effect=EFFECT, immature_share=IMMATURE_SHARE)
    # The rule excluded exactly the immature customers, whatever their arm.
    assert all(d.report.rows_immature == d.immature_rows for d in draws)
    assert sum(d.immature_rows for d in draws) / (SIMS * N) == pytest.approx(IMMATURE_SHARE, abs=0.01)
    assert_mean_in_band(_lifts(draws), EFFECT, what="mean lift with immature rows excluded")


def test_counting_immature_rows_as_observed_biases_the_lift_towards_zero() -> None:
    expected = EFFECT * (1.0 - IMMATURE_SHARE * (1.0 - MEAN_SHARE_OBSERVED))
    lifts = []
    logging.disable(logging.CRITICAL)
    try:
        for seed in seeds(SEED, SIMS):
            campaign = population(N, BASE_RATE, EFFECT, immature_share=IMMATURE_SHARE, seed=seed)
            kwargs = {**campaign.measure_kwargs, "outcome_window_days": None}  # maturity filter off
            report = measure_incrementality(campaign.scores, campaign.outcomes, **kwargs)
            assert report.rows_immature == 0
            assert report.absolute_lift is not None
            lifts.append(report.absolute_lift.value)
    finally:
        logging.disable(logging.NOTSET)
    assert sum(lifts) / SIMS < EFFECT  # biased towards zero ...
    assert_mean_in_band(
        lifts, expected, what="mean lift with immature rows counted as observed"
    )  # ... as modelled
