"""Each offer's 95% interval covers its true effect 95% of the time (Plan J M100, the M95 band).

A run of several offers reports every offer against the one shared control twice: on the training
hold-out (`uplift_evaluation.json`'s `arms`, the bootstrap within each arm of DEC-605) and on a measured
campaign (`measure_campaign(..., arm_column=)`'s `arms`, the Newcombe interval). Both are checked here
through the code that writes them - `engine.measurement.arms.arm_from_evaluation` over `evaluate_uplift`
on one offer's customers and the control's, exactly as the train flow does, and `measure_campaign` -
on 2,000 simulated three-arm campaigns per case, against the M95 band (95% +/- 4 Monte Carlo standard
errors, `tests/statistical/bands.py`). The offers share their control, so a wrong split of the control
between offers would show here as coverage away from 95% for one offer or both.
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.measurement.arms import arm_from_evaluation
from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import multi_arm_campaign
from engine.uplift.config import UpliftBaseModel, UpliftLearner
from engine.uplift.metrics import evaluate_uplift
from tests.statistical.bands import assert_share_in_band, seeds

pytestmark = pytest.mark.statistical

SIMS = 2_000
NOMINAL = 0.95
EFFECTS = (0.02, 0.04)
"""Offer 1 adds two points, offer 2 four: a realistic pair at a 5% base rate."""


def _covers(low: float | None, high: float | None, truth: float) -> bool:
    return low is not None and high is not None and low <= truth <= high


def test_each_offers_measured_interval_covers_its_true_effect() -> None:
    covered = np.zeros(len(EFFECTS), dtype=np.int_)
    for seed in seeds(100_001, SIMS):
        campaign = multi_arm_campaign(3_000, 0.05, EFFECTS, seed=seed)
        report = measure_campaign(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
        assert report.arms is not None
        for k, summary in enumerate(report.arms):
            effect = summary.effect
            covered[k] += effect is not None and _covers(effect.ci_low, effect.ci_high, EFFECTS[k])
    for k, size in enumerate(EFFECTS):
        assert_share_in_band(
            covered[k] / SIMS, NOMINAL, SIMS, what=f"measured coverage of offer {k + 1} (+{size:.0%})"
        )


def test_each_offers_hold_out_interval_covers_its_true_effect() -> None:
    """The training hold-out's per-offer effect: rows dealt at random to the control and two offers, the
    effect interval read off the evaluation of each offer's customers and the control's."""
    covered = np.zeros(len(EFFECTS), dtype=np.int_)
    lift = np.array([0.0, *EFFECTS])
    for seed in seeds(100_002, SIMS):
        rng = np.random.default_rng(seed)
        rows = 3_000
        arm = rng.permutation(np.arange(rows) % 3)
        y = (rng.random(rows) < 0.05 + lift[arm]).astype(np.int_)
        prediction = rng.random(rows)  # the effect interval does not depend on the ranking
        for k in (1, 2):
            mine = (arm == 0) | (arm == k)
            t = np.asarray(arm[mine] == k, dtype=np.int_)
            evaluation, _ = evaluate_uplift(
                prediction[mine],
                t,
                y[mine],
                run_id="simulated",
                learner=UpliftLearner.X_LEARNER,
                base_model=UpliftBaseModel.LIGHTGBM,
                bootstrap_samples=200,
                seed=seed,
                causal=True,
            )
            effect = arm_from_evaluation(f"offer_{k}", k, "none", evaluation, t, y[mine]).effect
            covered[k - 1] += effect is not None and _covers(effect.ci_low, effect.ci_high, lift[k])
    for k, size in enumerate(EFFECTS):
        assert_share_in_band(
            covered[k] / SIMS, NOMINAL, SIMS, what=f"hold-out coverage of offer {k + 1} (+{size:.0%})"
        )
