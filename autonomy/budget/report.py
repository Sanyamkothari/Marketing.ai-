"""The plain one-line entry a budget move gets in the briefing, with every number it uses."""

from __future__ import annotations

from collections.abc import Mapping

from autonomy.agents.analyst import format_money, format_rate
from autonomy.budget.models import MAX_WEEKLY_SHARE, BudgetMove

__all__ = ["move_entry"]


def _name(names: Mapping[str, str], goal_id: str, fallback: str) -> str:
    """A goal's name for a sentence. A name with a digit is replaced: every digit in a briefing must be
    one of its numbers."""
    name = " ".join(names.get(goal_id, "").split())
    return name if name and not any(c.isdigit() for c in name) else fallback


def move_entry(
    move: BudgetMove, names: Mapping[str, str], currency: str = "USD", *, cap: float = MAX_WEEKLY_SHARE
) -> tuple[str, dict[str, float]]:
    """(sentence, numbers) for one move. The sentence also tells the person undo is one click away."""
    weak = _name(names, move.from_goal, "the weaker goal")
    strong = _name(names, move.to_goal, "the stronger goal")
    numbers = {
        "budget_moved": move.amount,
        "stronger_return_low": move.to_return.low,
        "stronger_return_high": move.to_return.high,
        "weaker_return_low": move.from_return.low,
        "weaker_return_high": move.from_return.high,
        "weekly_limit_share": cap,
    }

    def span(lo: float, hi: float) -> str:
        return f"{format_money(lo, currency)} to {format_money(hi, currency)}"

    text = (
        f"Moved {format_money(move.amount, currency)} of budget from {weak} to {strong}: {strong} earned "
        f"{span(move.to_return.low, move.to_return.high)} extra profit for each unit spent and {weak} "
        f"{span(move.from_return.low, move.from_return.high)}, ranges that do not overlap. "
        f"No goal moves more than {format_rate(cap)} of its budget a week, and one click undoes it."
    )
    return text, numbers
