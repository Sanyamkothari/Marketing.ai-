"""Plan J: planning and measuring a test (M93 onwards).

`engine.measurement.planner` answers the questions asked *before* a campaign runs: how small a change
a control group of a given size can see, how many customers a test needs, and what holding customers
back (or contacting a random few outside the target) costs. Pure functions of counts; no storage, no
network, no customer data.
"""

from __future__ import annotations

from engine.measurement.planner import (
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    ArmSizes,
    CostEstimate,
    HoldoutPlan,
    Mde,
    MoneyRange,
    PowerEstimate,
    PowerPreview,
    PowerPreviewPoint,
    PowerPreviewRequest,
    achieved_power,
    cost_of_explore,
    cost_of_holdout,
    holdout_for_mde,
    mde_two_proportions,
    n_for_mde,
    power_preview,
)

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_POWER",
    "ArmSizes",
    "CostEstimate",
    "HoldoutPlan",
    "Mde",
    "MoneyRange",
    "PowerEstimate",
    "PowerPreview",
    "PowerPreviewPoint",
    "PowerPreviewRequest",
    "achieved_power",
    "cost_of_explore",
    "cost_of_holdout",
    "holdout_for_mde",
    "mde_two_proportions",
    "n_for_mde",
    "power_preview",
]
