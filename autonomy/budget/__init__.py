"""Auto-budget (plan-v3 decision 8): budget moves from the weakest goal to the strongest.

`goal_returns(proofs)` reads each goal's proven extra profit per unit spent, with its range, from the
per-goal `ProofSummary`. `BudgetLedger.rebalance(proofs, now)` moves at most 20% of a goal's budget a
week, only when the ranges do not overlap, never past a goal's floor or envelope, logs the move, and
`BudgetLedger.undo(move_id, now)` puts it back exactly. `move_entry` is the one-line briefing entry.
Opt-in: `BudgetConfig.enabled` is false until a person turns it on.
"""

from autonomy.budget.ledger import BudgetLedger, RebalanceResult
from autonomy.budget.models import (
    MAX_WEEKLY_SHARE,
    BudgetConfig,
    BudgetError,
    BudgetMove,
    GoalBudget,
    GoalReturn,
)
from autonomy.budget.rebalance import cents, goal_returns, plan_move
from autonomy.budget.report import move_entry

__all__ = [
    "MAX_WEEKLY_SHARE",
    "BudgetConfig",
    "BudgetError",
    "BudgetLedger",
    "BudgetMove",
    "GoalBudget",
    "GoalReturn",
    "RebalanceResult",
    "cents",
    "goal_returns",
    "move_entry",
    "plan_move",
]
