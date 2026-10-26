"""Per-type copy (plan-v3 decision 6): AI writes per customer type, a scoring model picks, live tests decide.

Opt-in through `CopyConfig.enabled`; off by default, so the v2 agent is unchanged. See docs/v3/COPY.md.
"""

from autonomy.content.config import CopyConfig, CostGuard
from autonomy.content.live import Arm, Cut, LiveBoard, LiveTest, wilson_bounds
from autonomy.content.planner import CopyPlanner, TypePlan, WeeklyPlan, select_top
from autonomy.content.scoring import (
    DraftResult,
    PastSend,
    ScoringModel,
    ScoringReport,
    Tally,
    ThemeBaseline,
    draft_features,
    tally_sends,
    within_type_auc,
)
from autonomy.content.types import CustomerType, CustomerTypes

__all__ = [
    "Arm",
    "CopyConfig",
    "CopyPlanner",
    "CostGuard",
    "CustomerType",
    "CustomerTypes",
    "Cut",
    "DraftResult",
    "LiveBoard",
    "LiveTest",
    "PastSend",
    "ScoringModel",
    "ScoringReport",
    "Tally",
    "ThemeBaseline",
    "TypePlan",
    "WeeklyPlan",
    "draft_features",
    "select_top",
    "tally_sends",
    "wilson_bounds",
    "within_type_auc",
]
