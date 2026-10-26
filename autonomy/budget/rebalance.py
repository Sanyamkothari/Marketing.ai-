"""Auto-budget: move budget from the weakest goal to the strongest, slowly and only on clear evidence.

**Evidence.** Each goal's proven return is the proof's own return on spend (`ProofSummary.roi`, so a
bootstrap proof's range is the one the proof and the briefing show), or, when the proof has none, its
incremental profit against the holdout divided by what it spent (`goal_returns`), with the 95% range.
Money not spent earns nothing, so the unit is spend, not the unused allowance. A goal with no
interval (too few customers, or another goal acted on the same customers) takes no part.

**The rule** (`plan_move`). The strongest goal is the one with the highest point return, the weakest
the lowest. Budget moves only when the strongest goal's range lies entirely above the weakest's
(ranges that overlap are noise) and entirely above zero (a goal is never rewarded for losing less).
The amount is the smallest of:

* the weekly limit, `max_weekly_share` (at most 20%, decision V-4) of the goal's budget a week ago,
  for the giver and again for the receiver, minus everything that already moved in or out of that
  goal in the last 7 days (the limit is on gross movement, so a goal cannot give 20% and take 20%
  in the same week);
* what the giver can part with: it keeps what it has spent and at least `floor_share` of what it
  started with, so it keeps being measured;
* the receiver's room under its own envelope ceiling, if it has one.

Money is conserved: the programme total never changes, so a move cannot leave the envelope.
Amounts are whole cents, so budgets stay in cents and an undo restores them exactly. After a person
undoes a move, the same giver and receiver are left alone for `cooldown_days`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta

from autonomy.budget.models import BudgetConfig, BudgetError, BudgetMove, GoalBudget, GoalReturn
from autonomy.contracts import Estimate, ProofSummary

__all__ = ["cents", "goal_returns", "plan_move"]

WEEK = timedelta(days=7)


def cents(value: float) -> float:
    """Round to whole cents; every budget and amount goes through here."""
    return round(value, 2)


def _floor_cents(value: float) -> float:
    return math.floor(value * 100 + 1e-9) / 100


def goal_returns(proofs: Mapping[str, ProofSummary]) -> dict[str, GoalReturn]:
    """Per-goal proven return per unit spent, for the goals whose proof has an interval and some spend.

    Raises `BudgetError` when a proof is filed under another goal's id, so money never moves on the
    wrong goal's evidence.
    """
    out: dict[str, GoalReturn] = {}
    for goal_id, proof in sorted(proofs.items()):
        if proof.goal_id is not None and proof.goal_id != goal_id:
            raise BudgetError(f"the proof filed under goal {goal_id} is for goal {proof.goal_id}")
        profit = proof.incremental_profit
        if profit is None or proof.spend <= 0:
            continue
        per_unit = proof.roi or Estimate(
            point=profit.point / proof.spend, low=profit.low / proof.spend, high=profit.high / proof.spend
        )
        out[goal_id] = GoalReturn(goal_id=goal_id, spend=proof.spend, profit=profit, per_unit=per_unit)
    return out


def _moved(moves: tuple[BudgetMove, ...], now: datetime, goal_id: str) -> tuple[float, float]:
    """(out, in): applied budget that left and arrived at a goal in the last 7 days."""
    out = inn = 0.0
    for m in moves:
        if m.status != "applied" or not now - WEEK < m.at <= now:
            continue
        if m.from_goal == goal_id:
            out += m.amount
        if m.to_goal == goal_id:
            inn += m.amount
    return out, inn


def _recently_undone(moves: tuple[BudgetMove, ...], giver: str, taker: str, now: datetime, days: int) -> bool:
    for m in moves:
        if (
            m.status == "undone"
            and m.from_goal == giver
            and m.to_goal == taker
            and m.undone_at is not None
            and now - m.undone_at < timedelta(days=days)
        ):
            return True
    return False


def plan_move(
    returns: Mapping[str, GoalReturn],
    lines: Mapping[str, GoalBudget],
    moves: tuple[BudgetMove, ...],
    now: datetime,
    config: BudgetConfig,
) -> tuple[tuple[str, str, float] | None, str]:
    """(giver, receiver, amount) to move now, or None; and in either case the reason in plain words."""
    if not config.enabled:
        return None, "Auto-budget is switched off."
    known = sorted(g for g in returns if g in lines)
    if len(known) < 2:
        return None, "Fewer than two goals have a proven return yet."
    strong = max(known, key=lambda g: (returns[g].per_unit.point, g))
    weak = min(known, key=lambda g: (returns[g].per_unit.point, g))
    s, w = returns[strong].per_unit, returns[weak].per_unit
    if s.point == w.point:
        return None, "The goals earn the same, so nothing moves."
    if s.low <= w.high:
        return None, "The goals' ranges overlap, so the difference could be noise and nothing moves."
    if s.low <= 0:
        return None, "No goal has proven a profit yet, so nothing moves."
    if _recently_undone(moves, weak, strong, now, config.cooldown_days):
        return None, "A person undid this move recently, so it is not repeated yet."
    giver, taker = lines[weak], lines[strong]
    g_out, g_in = _moved(moves, now, weak)
    t_out, t_in = _moved(moves, now, strong)
    share = config.max_weekly_share
    g_room = share * (giver.budget + g_out - g_in) - (g_out + g_in)
    t_room = share * (taker.budget + t_out - t_in) - (t_out + t_in)
    keep = max(config.floor_share * giver.start, giver.spent)
    can_give = giver.budget - keep
    ceiling_room = math.inf if taker.ceiling is None else taker.ceiling - taker.budget
    amount = _floor_cents(min(g_room, t_room, can_give, ceiling_room))
    if amount < 0.01:
        return (
            None,
            "The weekly limit, the giving goal's floor or the receiving goal's ceiling leaves no room.",
        )
    return (weak, strong, amount), "The stronger goal's range is entirely above the weaker goal's."
