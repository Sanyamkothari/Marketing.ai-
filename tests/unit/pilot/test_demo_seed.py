"""The demo is big enough to show its churn campaign's planted effect (Plan E M63, DEC-960).

`scripts/seed_demo.py` simulates the churn campaign's outcomes from the champion's own scores
(`_churn_outcomes`): a customer leaves with a chance equal to their score, and contact removes
`CONTACT_EFFECT` of it. Whether Campaign results can then tell the effect from noise depends on how
many customers are scored, a tenth of whom are held back as the control group. At 2,000 customers
that control group was 200, and about one seed in six put zero inside the 95% interval, so the
value view said "the range includes zero" of an effect the demo planted itself. This checks the size
without training anything: it draws scores shaped like the demo champion's, runs the seed's own
outcome simulation on `CUSTOMERS` of them, and measures the interval the way Campaign results does.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from engine.uplift.incrementality import newcombe_interval
from scripts.seed_demo import CUSTOMERS, _churn_outcomes

TARGET = "churn_next_60d"
DRAWS = 200
SCORE_SHAPE = (0.04, 0.05)
"""Beta parameters of a score shaped like the demo champion's: mean 0.44 and spread 0.48, the mean
0.443-0.449 and standard deviation 0.476-0.478 measured on four seeded demos. The synthetic
subscribers are easy to separate, so most scores sit near 0 or near 1."""
CONTROL_SHARE = 10
"""One customer in ten is held back, as the scoring run's control group does (`control_group`)."""
ROBUST = 0.98
"""At most one seeded demo in fifty may show zero inside the range. The simulated share is 0.83 at
2,000 customers, 0.975 at 3,000 and 0.995 at 4,000 (normal-approximation power 0.86, 0.96, 0.99)."""


def _scores(rng: np.random.Generator) -> pd.DataFrame:
    """A scored month as `scores.csv` has it: one row per customer, a tenth of them held back."""
    control = np.zeros(CUSTOMERS, dtype=bool)
    control[rng.choice(CUSTOMERS, CUSTOMERS // CONTROL_SHARE, replace=False)] = True
    return pd.DataFrame(
        {
            "entity_key": [f"C{index:05d}" for index in range(CUSTOMERS)],
            "snapshot_date": "2025-06-30",
            "churn_prob": rng.beta(*SCORE_SHAPE, CUSTOMERS).astype(str),
            "control_group": np.where(control, "True", "False"),
            "suppressed_reason": "",
        }
    )


def test_the_churn_campaigns_interval_excludes_zero_on_almost_every_seed() -> None:
    rng = np.random.default_rng(20260923)
    excluded = 0
    for draw in range(DRAWS):
        scores = _scores(rng)
        left = _churn_outcomes(scores, "churn_prob", TARGET, seed=draw)[TARGET].to_numpy()
        held_back = scores["control_group"].eq("True").to_numpy()
        _, _, high = newcombe_interval(
            int(left[~held_back].sum()),
            int((~held_back).sum()),
            int(left[held_back].sum()),
            int(held_back.sum()),
        )
        excluded += high < 0.0
    assert excluded / DRAWS >= ROBUST, (
        f"with {CUSTOMERS:,} customers the churn campaign's 95% interval excluded zero on only "
        f"{excluded} of {DRAWS} simulated seeds"
    )
