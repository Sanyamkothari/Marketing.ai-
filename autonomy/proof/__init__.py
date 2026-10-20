"""WF3 Proof: the three-way split, rewards, the proof numbers and off-policy evaluation.

`split` puts every customer in the agent, baseline or holdout group by a salted hash. `rewards`
turns decisions plus events into closed-window `Reward`s (including "virtual" rewards for doing
nothing). `estimate` compares the groups in money, with 95% intervals, and refuses to claim when the
groups are too small. `ope` estimates how another policy would have done on the logged decisions.
v1 cross-checks live in `autonomy.bridges.uplift_bridge`.
"""

from autonomy.proof.estimate import MIN_GROUP, Z_95, bootstrap_difference, proof_summary, welch_difference
from autonomy.proof.ope import (
    LoggedBandit,
    OpeReport,
    OpeResult,
    doubly_robust,
    evaluate,
    from_decisions,
    ips,
    snips,
)
from autonomy.proof.rewards import Attribution, Costs, build_rewards, proof_rewards
from autonomy.proof.split import assign_group, group_assigner, split_score, with_group

__all__ = [
    "MIN_GROUP",
    "Z_95",
    "Attribution",
    "Costs",
    "LoggedBandit",
    "OpeReport",
    "OpeResult",
    "assign_group",
    "bootstrap_difference",
    "build_rewards",
    "doubly_robust",
    "evaluate",
    "from_decisions",
    "group_assigner",
    "ips",
    "proof_rewards",
    "proof_summary",
    "snips",
    "split_score",
    "welch_difference",
    "with_group",
]
