"""Public-data replay (plan-v2 section 8): off-policy estimates of our kind of policy on real logs.

`datasets` reads Open Bandit, Hillstrom and Criteo uplift into one `LoggedBandit` shape; `harness`
evaluates a target policy against the logging policy and fits a simple uplift-style segment policy;
`fetch` downloads the full files (never used by tests).
"""

from autonomy.proof.replay.datasets import (
    HILLSTROM_SAMPLE,
    LoggedBandit,
    read_criteo_uplift,
    read_hillstrom,
    read_open_bandit,
)
from autonomy.proof.replay.harness import (
    PolicyEstimates,
    PolicyFn,
    ReplayReport,
    SegmentPolicy,
    evaluate,
    fit_segment_policy,
    fixed_action_policy,
    per_action_mean_model,
    replay,
)

__all__ = [
    "HILLSTROM_SAMPLE",
    "LoggedBandit",
    "PolicyEstimates",
    "PolicyFn",
    "ReplayReport",
    "SegmentPolicy",
    "evaluate",
    "fit_segment_policy",
    "fixed_action_policy",
    "per_action_mean_model",
    "read_criteo_uplift",
    "read_hillstrom",
    "read_open_bandit",
    "replay",
]
