"""The budget ledger: each goal's budget, every move logged, and one call to undo any move."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from autonomy.budget.models import BudgetConfig, BudgetError, BudgetMove, GoalBudget, GoalReturn
from autonomy.budget.rebalance import cents, goal_returns, plan_move
from autonomy.contracts import ProofSummary, stable_id

__all__ = ["BudgetLedger", "RebalanceResult"]


@dataclass(frozen=True)
class RebalanceResult:
    move: BudgetMove | None
    reason: str
    returns: Mapping[str, GoalReturn]


class BudgetLedger:
    """Holds the budgets and the log. The caller writes `budgets()` back to its goals after a change."""

    def __init__(
        self,
        lines: Iterable[GoalBudget],
        config: BudgetConfig | None = None,
        *,
        envelope_total: float | None = None,
        moves: Iterable[BudgetMove] = (),
    ) -> None:
        self.config = config or BudgetConfig()
        self._lines: dict[str, GoalBudget] = {}
        for line in lines:
            if line.goal_id in self._lines:
                raise BudgetError(f"goal {line.goal_id} is listed twice")
            self._lines[line.goal_id] = line.model_copy(
                update={"budget": cents(line.budget), "start_budget": cents(line.start)}
            )
        total = cents(sum(line.budget for line in self._lines.values()))
        self.envelope_total = total if envelope_total is None else cents(envelope_total)
        if total > self.envelope_total:
            raise BudgetError("the goals' budgets add up to more than the approved envelope")
        self._moves: list[BudgetMove] = list(moves)

    @property
    def moves(self) -> tuple[BudgetMove, ...]:
        return tuple(self._moves)

    def budgets(self) -> dict[str, float]:
        return {g: line.budget for g, line in self._lines.items()}

    def total(self) -> float:
        return cents(sum(line.budget for line in self._lines.values()))

    def update_spent(self, spent: Mapping[str, float]) -> None:
        """Record what each goal has spent so far; a goal's budget never drops below it."""
        for goal_id, value in spent.items():
            if goal_id in self._lines:
                self._lines[goal_id] = self._lines[goal_id].model_copy(update={"spent": max(value, 0.0)})

    def rebalance(self, proofs: Mapping[str, ProofSummary], now: datetime) -> RebalanceResult:
        """Move budget if the rule allows it, log the move, and say why or why not."""
        returns = goal_returns(proofs)
        plan, reason = plan_move(returns, self._lines, self.moves, now, self.config)
        if plan is None:
            return RebalanceResult(None, reason, returns)
        giver, taker, amount = plan
        a, b = self._lines[giver], self._lines[taker]
        move = BudgetMove(
            move_id=stable_id("bmv", giver, taker, now.isoformat(), len(self._moves)),
            at=now,
            from_goal=giver,
            to_goal=taker,
            amount=amount,
            from_before=a.budget,
            from_after=cents(a.budget - amount),
            to_before=b.budget,
            to_after=cents(b.budget + amount),
            from_return=returns[giver].per_unit,
            to_return=returns[taker].per_unit,
        )
        self._set(giver, move.from_after)
        self._set(taker, move.to_after)
        self._moves.append(move)
        return RebalanceResult(move, reason, returns)

    def undo(self, move_id: str, now: datetime) -> BudgetMove:
        """Reverse one applied move: the amount goes back, to the cent. The same call undoes any move."""
        for i, m in enumerate(self._moves):
            if m.move_id != move_id:
                continue
            if m.status == "undone":
                raise BudgetError("that move was already undone")
            taker = self._lines[m.to_goal]
            if cents(taker.budget - m.amount) < taker.spent:
                raise BudgetError(
                    "the receiving goal has already spent that money, so it cannot be taken back"
                )
            self._set(m.from_goal, cents(self._lines[m.from_goal].budget + m.amount))
            self._set(m.to_goal, cents(taker.budget - m.amount))
            done = m.model_copy(update={"status": "undone", "undone_at": now})
            self._moves[i] = done
            return done
        raise BudgetError(f"no budget move {move_id}")

    def undo_last(self, now: datetime) -> BudgetMove:
        """Undo the most recent move that is still applied."""
        for m in reversed(self._moves):
            if m.status == "applied":
                return self.undo(m.move_id, now)
        raise BudgetError("there is no budget move to undo")

    def _set(self, goal_id: str, budget: float) -> None:
        line = self._lines[goal_id]
        if line.ceiling is not None and budget > line.ceiling + 1e-9:
            raise BudgetError("that would put a goal above its envelope")
        self._lines[goal_id] = line.model_copy(update={"budget": budget})
