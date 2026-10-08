"""The equal-budget comparison's 95% intervals cover the truth 95% of the time (Plan J M96).

`engine.measurement.compare.equal_budget_comparison` reports, for uplift top-N and risk top-N at the
same budget, the extra conversions each would cause and the uplift-minus-risk difference, each with a
95% interval, from a ring cross-fit of randomised rows and the rows' recorded treatment probabilities.
Each case simulates 2,000 populations of 2,000 customers whose effect is known per customer
(`engine.measurement.simulate.uplift_population`: treatment drawn per row with a probability that
depends on risk, as M92's holdout and explore slice draw it) and requires the observed coverage to lie
within four Monte Carlo standard errors of 95% (`tests/statistical/bands.py`: 95% +/- 1.95 points).

The truth an interval must hold is what the two cross-fitted policies would add on these customers,
`sum(policy x tau)`, with the per-customer effect `tau` the simulation knows. Three populations: a
heterogeneous effect unrelated to risk (uplift ranking should win), no effect at all (they tie at
zero), and an effect that grows with risk (risk ranking should win). The learners are linear
(`tests/statistical/risk_comparison.py`) so the suite runs in about a minute; the interval is the
estimator's property, not the learner's.

Before the ring (every fold's models trained on all the other folds), the null case covered about 92%
and failed this band: each fold's outcomes helped choose the other folds' contacts. See
`engine.measurement.compare`.
"""

from __future__ import annotations

import pytest

from tests.statistical.bands import assert_share_in_band, seeds
from tests.statistical.risk_comparison import compare_once, covers

pytestmark = pytest.mark.statistical

SIMS = 2_000
NOMINAL = 0.95
ROWS = 2_000


@pytest.mark.parametrize(
    ("effect", "base_seed"),
    [("heterogeneous", 96_001), ("null", 96_002), ("risk", 96_003)],
    ids=["heterogeneous-effect", "null-effect", "risk-driven-effect"],
)
def test_the_equal_budget_intervals_cover_the_truth(effect: str, base_seed: int) -> None:
    draws = [compare_once(seed, n=ROWS, effect=effect) for seed in seeds(base_seed, SIMS)]
    difference = sum(covers(d.report.difference, d.true_difference) for d in draws) / SIMS
    uplift = sum(covers(d.report.uplift.incremental_conversions, d.true_uplift) for d in draws) / SIMS
    risk = sum(covers(d.report.risk.incremental_conversions, d.true_risk) for d in draws) / SIMS
    assert_share_in_band(difference, NOMINAL, SIMS, what=f"uplift-minus-risk coverage ({effect})")
    assert_share_in_band(uplift, NOMINAL, SIMS, what=f"uplift top-N coverage ({effect})")
    assert_share_in_band(risk, NOMINAL, SIMS, what=f"risk top-N coverage ({effect})")
    # Per rupee is the same interval divided by a constant: it covers exactly when the count does.
    for d in draws[:50]:
        spend = d.report.uplift.contacts * 10.0
        per_rupee = d.report.difference_per_rupee
        assert per_rupee is not None and per_rupee.ci_low == pytest.approx(d.report.difference.ci_low / spend)  # type: ignore[operator]
