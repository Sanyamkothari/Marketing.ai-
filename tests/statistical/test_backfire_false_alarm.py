"""The backfire check raises a false alarm in at most one campaign in twenty (Plan J M104, DEC-1314).

The Value Proof Pack flags a group of customers as backfiring when its effect's range, widened to allow for the
number of groups judged (Bonferroni: each at `1 - 0.05 / m`), lies wholly below zero. In a campaign that harmed
nobody, the chance that *any* group is flagged must then be at most 5%. This simulates 2,000 campaigns of eight
groups the campaign did not touch (`engine.measurement.simulate.segmented_campaign`), measures each with the
production chain (`measure_campaign`, `measure_campaign_segments`) and counts the campaigns with a flag. The share
must not exceed 5% by more than four Monte Carlo standard errors (`tests/statistical/bands.py`); a one-sided
check, because Bonferroni is conservative and the harmful side is one tail, so the true rate is about half that.

It also counts how often reading every group at 95% alone would have raised an alarm, which must be far above
5%: the guard is there because that rule cries wolf.
"""

from __future__ import annotations

import logging

import pytest

from engine.measurement.measure import measure_campaign, measure_campaign_segments
from engine.measurement.simulate import segmented_campaign
from tests.statistical.bands import band, seeds

pytestmark = pytest.mark.statistical

SIMS = 2_000
GROUPS = {f"G{index}": 0.0 for index in range(8)}


def test_a_campaign_that_harmed_nobody_is_flagged_at_most_one_time_in_twenty() -> None:
    flagged = naive = 0
    logging.disable(logging.CRITICAL)
    try:
        for seed in seeds(104_001, SIMS):
            campaign = segmented_campaign(4_000, 0.10, GROUPS, seed=seed, control_share=0.3)
            kw = campaign.measure_kwargs
            report = measure_campaign(
                campaign.scores, campaign.outcomes, intended_column="intended_treatment", **kw
            )
            effects = measure_campaign_segments(
                campaign.scores,
                campaign.outcomes,
                report,
                primary_key=kw["primary_key"],
                outcome_column=kw["outcome_column"],
                treatment_time=kw["treatment_time"],
                treatment_date_column=kw["treatment_date_column"],
                intended_column="intended_treatment",
            )
            flagged += any(
                c.family_interval is not None and (c.family_interval.ci_high or 0.0) < 0.0
                for c in effects.cells
            )
            naive += any(c.effect is not None and (c.effect.ci_high or 0.0) < 0.0 for c in effects.cells)
    finally:
        logging.disable(logging.NOTSET)
    _, high = band(0.05, SIMS)
    assert flagged / SIMS <= high, f"{flagged / SIMS:.4f} of campaigns flagged, above {high:.4f}"
    assert naive / SIMS > high, f"reading each group at 95% alone flagged only {naive / SIMS:.4f}"
