"""The 95% interval of `measure_incrementality` covers the true effect 95% of the time (Plan J M95).

Every `measure_incrementality` report carries a 95% Newcombe interval for the absolute lift. If it is
honest, then across many campaigns whose true effect is known, the interval holds the truth in 95% of
them. This simulates 2,000 campaigns per case and requires the observed coverage to lie within four
Monte Carlo standard errors of 95% (`tests/statistical/bands.py` states the rule: 95% +/- 1.95 points).

The three base rates are the cases the interval is most likely to get wrong: at 2% each arm has only a
few dozen conversions, where a score interval matters most. At these arm sizes a Wald interval would
also pass every band here (measured: 94.6% at 2%), so this case guards against an interval that is too
narrow by more than about 2 points of coverage, not against the choice of method. The effect at each is a realistic one to three
points, a relative lift of about 40% at 5%, 10% at 20% and 100% at 2%. The populations are small
(2,000 to 4,000 customers) to keep the suite's runtime reasonable; the number of simulations, not the
population size, is what sets the band.
"""

from __future__ import annotations

import pytest

from tests.statistical.bands import assert_share_in_band
from tests.statistical.campaigns import covers, measure_many

pytestmark = pytest.mark.statistical

SIMS = 2_000
NOMINAL = 0.95


@pytest.mark.parametrize(
    ("base_rate", "effect", "n", "seed"),
    [
        (0.02, 0.01, 4_000, 95_001),
        (0.05, 0.02, 2_500, 95_002),
        (0.20, 0.02, 1_200, 95_003),
    ],
    ids=["base-2pct", "base-5pct", "base-20pct"],
)
def test_the_interval_covers_the_true_effect(base_rate: float, effect: float, n: int, seed: int) -> None:
    draws = measure_many(seed, SIMS, n=n, base_rate=base_rate, effect=effect)
    assert_share_in_band(
        sum(covers(d) for d in draws) / SIMS, NOMINAL, SIMS, what=f"coverage at a {base_rate:.0%} base rate"
    )


def test_the_interval_covers_the_true_itt_when_not_everyone_receives_the_treatment() -> None:
    """With 60% compliance and 10% contamination the ITT is half the effect on the treated; the interval
    for the assigned arms' difference must cover that, not the effect on the treated."""
    draws = measure_many(
        95_004, SIMS, n=2_500, base_rate=0.05, effect=0.04, compliance=0.6, contamination=0.1
    )
    assert abs(draws[0].true_itt - 0.02) < 1e-12  # (0.6 - 0.1) * 0.04
    assert_share_in_band(
        sum(covers(d) for d in draws) / SIMS, NOMINAL, SIMS, what="ITT coverage with compliance 0.6, leak 0.1"
    )
