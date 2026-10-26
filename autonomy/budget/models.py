"""The shapes of auto-budget: its limits, a goal's budget, its proven return, and a logged move."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import AwareDatetime, Field, model_validator

from autonomy.contracts import Contract, Estimate

__all__ = [
    "MAX_WEEKLY_SHARE",
    "BudgetConfig",
    "BudgetError",
    "BudgetMove",
    "GoalBudget",
    "GoalReturn",
]

MAX_WEEKLY_SHARE: Final[float] = 0.20
"""Decision V-4: no goal's budget moves by more than this share in a week. A config can only lower it."""


class BudgetError(Exception):
    """A budget operation that cannot be done, with a plain reason."""


class BudgetConfig(Contract):
    enabled: bool = Field(
        default=False, description="Off by default: no budget moves until a person turns it on"
    )
    max_weekly_share: float = Field(default=MAX_WEEKLY_SHARE, gt=0, le=MAX_WEEKLY_SHARE)
    floor_share: float = Field(
        default=0.5,
        ge=0,
        le=1,
        description="A goal keeps at least this share of the budget it started with, so it keeps being measured",
    )
    cooldown_days: int = Field(
        default=28, ge=0, description="After an undo, the same move is not repeated for this long"
    )


class GoalBudget(Contract):
    """One goal's allowance inside the approved envelope."""

    goal_id: str
    budget: float = Field(ge=0)
    start_budget: float | None = Field(
        default=None, ge=0, description="What the person approved; the floor is a share of it"
    )
    ceiling: float | None = Field(default=None, ge=0, description="The most this goal's own envelope allows")
    spent: float = Field(default=0.0, ge=0, description="Already spent; budget never drops below it")

    @model_validator(mode="after")
    def _within_ceiling(self) -> GoalBudget:
        if self.ceiling is not None and self.budget > self.ceiling + 1e-9:
            raise ValueError("a goal's budget is above its envelope ceiling")
        return self

    @property
    def start(self) -> float:
        return self.budget if self.start_budget is None else self.start_budget


class GoalReturn(Contract):
    """What a goal has proven: extra profit per unit spent, with its 95% interval."""

    goal_id: str
    spend: float
    profit: Estimate = Field(description="Incremental profit against the holdout (from the proof)")
    per_unit: Estimate = Field(description="Incremental profit per unit spent")


class BudgetMove(Contract):
    """One logged move of budget from the weakest goal to the strongest, undoable."""

    move_id: str
    at: AwareDatetime
    from_goal: str
    to_goal: str
    amount: float = Field(gt=0)
    from_before: float
    from_after: float
    to_before: float
    to_after: float
    from_return: Estimate
    to_return: Estimate
    status: Literal["applied", "undone"] = "applied"
    undone_at: AwareDatetime | None = None
