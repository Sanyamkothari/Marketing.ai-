"""At zero effect the measurement says "an effect" about 5% of the time, no more (Plan J M95).

A campaign that did nothing must usually be reported as doing nothing. This simulates 10,000 campaigns
with no effect and counts how often the product would show a result: the 95% interval for the lift
excluding zero (the "range excludes zero" sentence of the report), and, for the p-value the report also
prints, p < 0.05. Both must be within four Monte Carlo standard errors of the 5% a 95% interval promises
(`tests/statistical/bands.py`: 5% +/- 0.87 points at 10,000 simulations).

The rate is a *false-positive rate*, so a measurement that is too eager (too narrow an interval) and
one that is too timid (a rate far below 5%, which would hide real effects) both fail the band.
"""

from __future__ import annotations

import pytest

from tests.statistical.bands import assert_share_in_band
from tests.statistical.campaigns import measure_many

pytestmark = pytest.mark.statistical

SIMS = 10_000
ALPHA = 0.05
SEED = 95_010


def test_a_campaign_with_no_effect_is_called_effective_about_five_percent_of_the_time() -> None:
    draws = measure_many(SEED, SIMS, n=1_500, base_rate=0.05, effect=0.0)
    interval_excludes_zero = 0
    p_below_alpha = 0
    for draw in draws:
        lift = draw.report.absolute_lift
        assert lift is not None
        interval_excludes_zero += lift.excludes_zero
        p_below_alpha += draw.report.p_value is not None and draw.report.p_value < ALPHA
    assert_share_in_band(
        interval_excludes_zero / SIMS, ALPHA, SIMS, what="false positives: 95% interval excludes zero"
    )
    assert_share_in_band(p_below_alpha / SIMS, ALPHA, SIMS, what="false positives: p < 0.05")
