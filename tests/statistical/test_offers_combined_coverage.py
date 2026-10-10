"""Every offer added together: the 95% interval covers the true total 95% of the time (DEC-1314 (s)).

A campaign of several offers measures each offer against one shared held-back group. The Value Proof Pack adds
the offers' extra outcomes, `sum_k n_k (p_k - p_c)`, and needs an interval for that sum. The offers' errors are
correlated through the shared control (each difference subtracts the same `p_c`), so combining the offers'
own intervals as if they were independent is too narrow. The engine measures the sum directly instead: the sum
is exactly `N_T (pbar_T - p_c)`, every contacted customer of every offer pooled against the shared control, so
its interval is the pooled comparison's Newcombe interval times `N_T` (`offers_combined` of the report,
`engine.measurement.arms.measure_arms`). With the offers' sizes fixed by design the pooled treated rate's
variance is `sum_k n_k p_k (1 - p_k) / N_T^2`, never more than the binomial `pbar (1 - pbar) / N_T` the
interval assumes, so the interval is valid, and only slightly wide when the offers' rates differ a lot.

Checked through `measure_campaign` itself on 2,000 simulated campaigns of three offers and a shared control per
case, against the M95 band (95% +/- 4 Monte Carlo standard errors, `tests/statistical/bands.py`). The same
simulations show the independent combination of the offers' own intervals falling well below 95%: the shared
control is what the pooled interval accounts for.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import multi_arm_campaign
from tests.statistical.bands import assert_share_in_band, band, seeds

pytestmark = pytest.mark.statistical

SIMS = 2_000
NOMINAL = 0.95
CASES = {
    "three offers that each help": (0.05, (0.02, 0.04, 0.06), 100_101),
    "one helps, one harms, one does nothing": (0.10, (0.04, -0.03, 0.0), 100_102),
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_combined_interval_of_every_offer_covers_the_true_total(case: str) -> None:
    base_rate, effects, base_seed = CASES[case]
    covered = independent = 0
    for seed in seeds(base_seed, SIMS):
        campaign = multi_arm_campaign(4_000, base_rate, effects, seed=seed)
        report = measure_campaign(
            campaign.scores, campaign.outcomes, **campaign.measure_kwargs, combine_offers=True
        )
        assert report.arms is not None and report.offers_combined is not None
        whole = report.offers_combined.incremental_conversions
        assert whole is not None and whole.ci_low is not None and whole.ci_high is not None
        sizes = [arm.treated_rows or 0 for arm in report.arms]
        truth = sum(n * effect for n, effect in zip(sizes, effects, strict=True))
        covered += whole.ci_low <= truth <= whole.ci_high
        # The independent combination, for contrast: half-widths added in quadrature as if unrelated.
        halves = []
        for arm in report.arms:
            extra = arm.incremental_conversions
            assert extra is not None and extra.ci_low is not None and extra.ci_high is not None
            halves.append((extra.ci_high - extra.ci_low) / 2.0)
        centre = sum(arm.incremental_conversions.value for arm in report.arms if arm.incremental_conversions)
        half = math.sqrt(sum(h * h for h in halves))
        independent += centre - half <= truth <= centre + half
    assert_share_in_band(
        covered / SIMS, NOMINAL, SIMS, what=f"coverage of every offer added together ({case})"
    )
    low, _ = band(NOMINAL, SIMS)
    assert independent / SIMS < low, (
        f"the independent combination should under-cover with a shared control ({case}): "
        f"{independent / SIMS:.4f}"
    )


def test_the_combined_estimate_is_the_sum_of_the_offers_own() -> None:
    for seed in seeds(100_103, 50):
        campaign = multi_arm_campaign(4_000, 0.05, (0.02, 0.04, 0.06), seed=seed)
        report = measure_campaign(
            campaign.scores, campaign.outcomes, **campaign.measure_kwargs, combine_offers=True
        )
        assert report.arms is not None and report.offers_combined is not None
        whole = report.offers_combined.incremental_conversions
        parts = [arm.incremental_conversions for arm in report.arms]
        assert whole is not None and all(part is not None for part in parts)
        assert whole.value == pytest.approx(sum(part.value for part in parts if part is not None))
        assert np.isclose(
            report.offers_combined.treated_rows, sum(arm.treated_rows or 0 for arm in report.arms)
        )
