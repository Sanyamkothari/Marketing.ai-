"""The interval for the effect on the contacted covers the truth 95% of the time (Plan J M103, DEC-1313).

`engine.measurement.reconcile` divides the main difference by the difference in contact rates and gives the
ratio Fieller's interval. If it is honest, then across many campaigns in which only some of the customers
meant to be reached were, and some held-back ones were reached too, the interval holds the true effect on a
contacted customer in 95 of 100. This simulates 2,000 campaigns per case with `engine.measurement.simulate`
(who receives the treatment is drawn separately from who was assigned to it, so the effect on a customer who
receives it is `effect` and the Wald ratio's target is exactly that) and measures each with the production
chain: `build_assignment`, `measured_rows` and `complier_effect`. The observed coverage must lie within four
Monte Carlo standard errors of 95% (`tests/statistical/bands.py`: 95% +/- 1.95 points).

**Unbounded draws count as covering.** When the contact rates are close, a draw's data cannot tell the
effect on the contacted from anything at all, Fieller's set is the whole line, and the product gives no
interval (it says why). The set does hold the truth, so the draw counts as covered; the test also reports the
share of such draws, which must stay small where the contact rates differ by a fair margin, so the interval
is not a sleight of hand that answers nothing. The case with 5% reached and 2% leaked is the one that
produces them: that is the "honest when compliance is low" case.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import numpy as np
import pandas as pd
import pytest

from engine.measurement.campaign import build_assignment
from engine.measurement.reconcile import complier_effect, measured_rows
from engine.measurement.simulate import AS_OF, KEY_COLUMN, OUTCOME_WINDOW_DAYS, population
from tests.statistical.bands import assert_share_in_band, seeds

pytestmark = pytest.mark.statistical

SIMS = 2_000
NOMINAL = 0.95


@contextmanager
def quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def coverage(
    base_seed: int, *, n: int, base_rate: float, effect: float, compliance: float, contamination: float
) -> tuple[float, float]:
    """`(share covered, share unbounded)` over `SIMS` simulated campaigns, each through the production chain."""
    covered = unbounded = 0
    with quiet():
        for seed in seeds(base_seed, SIMS):
            sim = population(
                n, base_rate, effect, seed=seed, compliance=compliance, contamination=contamination
            )
            assignment = build_assignment(sim.scores, primary_key=KEY_COLUMN)
            contacts = pd.DataFrame(
                {
                    KEY_COLUMN: sim.scores[KEY_COLUMN],
                    "contacted": pd.array(sim.received_treatment, dtype="boolean"),
                }
            )
            rows, _, _ = measured_rows(
                assignment,
                sim.outcomes,
                contacts,
                primary_key=KEY_COLUMN,
                outcome_column="converted",
                positive_label=None,
                outcome_kind="binary",
                treatment_time=AS_OF,
                treatment_date_column="treatment_date",
                outcome_window_days=OUTCOME_WINDOW_DAYS,
                as_of=AS_OF,
            )
            result, _ = complier_effect(
                rows["treated"].to_numpy(dtype=bool),
                rows["d"].to_numpy(dtype=np.float64),
                rows["y"].to_numpy(dtype=np.float64),
                unit="rate",
            )
            if result is None:
                unbounded += 1
                covered += 1  # the whole line holds the truth
                continue
            low, high = result.effect.ci_low, result.effect.ci_high
            assert low is not None and high is not None
            covered += int(low <= effect <= high)
    return covered / SIMS, unbounded / SIMS


@pytest.mark.parametrize(
    ("compliance", "contamination", "effect", "n", "seed"),
    [
        (0.6, 0.1, 0.05, 3_000, 103_001),
        (0.9, 0.0, 0.03, 3_000, 103_002),
        (0.4, 0.05, 0.06, 4_000, 103_003),
    ],
    ids=["reached-60pct-leak-10pct", "reached-90pct-no-leak", "reached-40pct-leak-5pct"],
)
def test_the_interval_for_the_effect_on_the_contacted_covers_the_truth(
    compliance: float, contamination: float, effect: float, n: int, seed: int
) -> None:
    share, unbounded = coverage(
        seed, n=n, base_rate=0.10, effect=effect, compliance=compliance, contamination=contamination
    )
    assert_share_in_band(
        share,
        NOMINAL,
        SIMS,
        what=f"coverage of the effect on the contacted with {compliance:.0%} reached and {contamination:.0%} leaked",
    )
    assert (
        unbounded < 0.01
    ), f"{unbounded:.1%} of draws gave no interval where the contact rates differ by a wide margin"


def test_when_few_are_reached_the_interval_is_still_honest_and_says_so_often() -> None:
    """5% reached against 2% leaked: the contact rates differ by 3 points, about 3 standard errors, so some draws
    cannot be told apart from nobody being reached and their set is the whole line."""
    share, unbounded = coverage(
        103_004, n=1_500, base_rate=0.10, effect=0.20, compliance=0.05, contamination=0.02
    )
    assert_share_in_band(share, NOMINAL, SIMS, what="coverage of the effect on the contacted with 5% reached")
    assert unbounded > 0.01, "the case is chosen so that some draws cannot be told apart from no reach at all"
    assert unbounded < 0.60, "and most draws still give an interval"
