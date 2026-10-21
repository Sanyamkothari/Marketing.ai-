"""Customer-level uplift: who a message moves, learned from a past campaign, read by the bidders.

The agent's response model prices every customer of a segment alike; customer features (a telco's
tariff, a shop's region, a bank's age band) never reach a bid. This package learns, from a past
randomised or propensity-logged campaign in the history, how much each message raises each customer's
chance of buying, validates that out of sample, and hands it to `GoalBidder` as a score per customer
and action, blended with the response model by measured precision.

* `score`: what a bidder reads (`UpliftScore`, `UpliftScorer`), the blend, and the registry that makes
  a scorer travel with a response model (`attach_uplift`, `uplift_of`). Standard library only.
* `features`: customer attributes (`CustomerState.preferences`) and behaviour as one-hot columns.
* `campaign`: the history's latest closed campaign as one row per customer, with the control-group
  convention (`campaign_table`).
* `learner`: the default learner, a shared-baseline logistic with ridge-shrunk effects (numpy).
* `model`: cross-fitting, the out-of-sample calibration slope and its shrinkage (`fit_uplift`).
* `warm`: the one call for the runtime (`warm_start_uplift`).

The v1 AutoML learners (LightGBM / AutoGluon X-learner) plug in as an `UpliftLearner` through
`autonomy.bridges.uplift_bridge.V1UpliftLearner`.
"""

from __future__ import annotations

from autonomy.uplift.campaign import CampaignTable, campaign_table
from autonomy.uplift.features import FeatureEncoder
from autonomy.uplift.learner import SharedBaselineLogit, UpliftLearner
from autonomy.uplift.model import ArmReport, FittedUplift, UpliftReport, fit_uplift

# `autonomy.decide.bidders` imports `autonomy.uplift.score`, and `campaign` imports `autonomy.decide`:
# `score` needs nothing but the contracts, so either import order works.
from autonomy.uplift.score import UpliftScore, UpliftScorer, attach_uplift, blend, detach_uplift, uplift_of
from autonomy.uplift.warm import UpliftWarmStart, warm_start_uplift

__all__ = [
    "ArmReport",
    "CampaignTable",
    "FeatureEncoder",
    "FittedUplift",
    "SharedBaselineLogit",
    "UpliftLearner",
    "UpliftReport",
    "UpliftScore",
    "UpliftScorer",
    "UpliftWarmStart",
    "attach_uplift",
    "blend",
    "campaign_table",
    "detach_uplift",
    "fit_uplift",
    "uplift_of",
    "warm_start_uplift",
]
