"""Auto-budget: moves only when ranges separate, at most 20% a week, inside the envelope, undo exact."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy.agents.analyst import template_briefing
from autonomy.agents.claims import check_numbers
from autonomy.budget import (
    MAX_WEEKLY_SHARE,
    BudgetConfig,
    BudgetError,
    BudgetLedger,
    GoalBudget,
    goal_returns,
    move_entry,
)
from autonomy.contracts import Estimate, Goal, GoalKind, HealthStatus, ProofSummary
from autonomy.inbox.briefing import budget_week

NOW = datetime(2026, 3, 2, tzinfo=UTC)
ON = BudgetConfig(enabled=True)


def proof(goal: str, point: float, low: float, high: float, spend: float = 1000.0) -> ProofSummary:
    """A goal's proof whose profit per unit spent is `point` (range low..high)."""
    return ProofSummary(
        goal_id=goal,
        as_of=NOW,
        agent_n=500,
        baseline_n=100,
        holdout_n=100,
        incremental_revenue=None,
        incremental_profit=Estimate(point=point * spend, low=low * spend, high=high * spend),
        lift_vs_baseline=None,
        spend=spend,
    )


def ledger(config: BudgetConfig = ON, **kw: float | None) -> BudgetLedger:
    return BudgetLedger(
        [GoalBudget(goal_id="win", budget=1000.0), GoalBudget(goal_id="repeat", budget=1000.0)],
        config,
        **kw,  # type: ignore[arg-type]
    )


SEPARATED = {"win": proof("win", 0.2, -0.1, 0.5), "repeat": proof("repeat", 2.0, 1.5, 2.5)}
OVERLAPPING = {"win": proof("win", 0.8, 0.2, 1.4), "repeat": proof("repeat", 1.6, 1.0, 2.2)}


# --- evidence ----------------------------------------------------------------------------------------


def test_return_per_unit_spent_comes_from_the_proof() -> None:
    r = goal_returns({"win": proof("win", 0.5, 0.25, 0.75, spend=400.0)})["win"]
    assert (r.per_unit.low, r.per_unit.point, r.per_unit.high) == pytest.approx((0.25, 0.5, 0.75))
    assert r.profit.point == pytest.approx(200.0)


def test_the_proofs_own_return_range_is_used_when_it_has_one() -> None:
    """A bootstrap proof resamples spend too; auto-budget must show the same range the proof shows."""
    p = proof("win", 0.5, 0.25, 0.75, spend=400.0).model_copy(
        update={"roi": Estimate(point=0.5, low=0.1, high=0.95)}
    )
    r = goal_returns({"win": p})["win"]
    assert (r.per_unit.low, r.per_unit.point, r.per_unit.high) == (0.1, 0.5, 0.95)


def test_a_proof_filed_under_another_goal_is_refused() -> None:
    with pytest.raises(BudgetError):
        goal_returns({"win": proof("repeat", 2.0, 1.5, 2.5)})


def test_goals_without_a_range_or_spend_take_no_part() -> None:
    no_range = proof("a", 1, 1, 1).model_copy(update={"incremental_profit": None})
    no_spend = proof("b", 1, 0.5, 1.5, spend=0.0)
    assert goal_returns({"a": no_range, "b": no_spend}) == {}


# --- when it moves -----------------------------------------------------------------------------------


def test_moves_from_the_weakest_to_the_strongest_when_ranges_separate() -> None:
    led = ledger()
    res = led.rebalance(SEPARATED, NOW)
    assert res.move is not None
    assert (res.move.from_goal, res.move.to_goal) == ("win", "repeat")
    assert res.move.amount == pytest.approx(200.0)  # 20% of the giver's 1000
    assert led.budgets() == {"win": 800.0, "repeat": 1200.0}
    assert led.total() == 2000.0


def test_no_move_when_ranges_overlap() -> None:
    led = ledger()
    res = led.rebalance(OVERLAPPING, NOW)
    assert res.move is None and "overlap" in res.reason
    assert led.budgets() == {"win": 1000.0, "repeat": 1000.0} and led.moves == ()


def test_ranges_that_just_touch_do_not_count_as_separate() -> None:
    touching = {"win": proof("win", 0.5, 0.1, 1.0), "repeat": proof("repeat", 2.0, 1.0, 3.0)}
    assert ledger().rebalance(touching, NOW).move is None


def test_no_move_to_a_goal_that_has_not_proven_a_profit() -> None:
    losing = {"win": proof("win", -2.0, -2.5, -1.5), "repeat": proof("repeat", -0.3, -0.5, -0.1)}
    res = ledger().rebalance(losing, NOW)
    assert res.move is None and "profit" in res.reason


def test_no_move_with_one_proven_goal_or_when_off() -> None:
    assert ledger().rebalance({"win": SEPARATED["win"]}, NOW).move is None
    off = ledger(BudgetConfig())
    assert not off.config.enabled
    assert off.rebalance(SEPARATED, NOW).move is None
    assert off.budgets() == {"win": 1000.0, "repeat": 1000.0}


# --- the 20% limit -----------------------------------------------------------------------------------


def test_the_limit_cannot_be_set_above_twenty_percent() -> None:
    with pytest.raises(ValueError):
        BudgetConfig(max_weekly_share=0.3)
    assert MAX_WEEKLY_SHARE == 0.20


def test_never_more_than_twenty_percent_in_a_week() -> None:
    led = ledger()
    first = led.rebalance(SEPARATED, NOW)
    assert first.move is not None
    again = led.rebalance(SEPARATED, NOW + timedelta(days=3))
    assert again.move is None  # the week's room is used
    assert led.budgets() == {"win": 800.0, "repeat": 1200.0}
    nxt = led.rebalance(SEPARATED, NOW + timedelta(days=8))
    assert nxt.move is not None
    assert nxt.move.amount <= 0.2 * 800.0 + 1e-9  # 20% of the giver's budget a week ago


def test_the_receiver_is_limited_too() -> None:
    led = BudgetLedger(
        [GoalBudget(goal_id="win", budget=1000.0), GoalBudget(goal_id="repeat", budget=100.0)], ON
    )
    res = led.rebalance(SEPARATED, NOW)
    assert res.move is not None and res.move.amount == pytest.approx(20.0)  # 20% of the receiver's 100


def test_every_week_stays_within_the_limit_over_a_month() -> None:
    led = ledger()
    amounts: list[float | None] = []
    for week in range(5):
        before = led.budgets()
        res = led.rebalance(SEPARATED, NOW + timedelta(days=7 * week + 1))
        after = led.budgets()
        for goal in before:
            assert abs(after[goal] - before[goal]) <= 0.2 * before[goal] + 0.01
        assert led.total() == 2000.0
        amounts.append(None if res.move is None else res.move.amount)
    # 20% of the giver each week (1000, 800, 640), then its floor of half its start (500) binds
    assert amounts == [200.0, 160.0, 128.0, 12.0, None]


def test_the_week_is_a_rolling_seven_days() -> None:
    led = ledger()
    assert led.rebalance(SEPARATED, NOW).move is not None
    assert led.rebalance(SEPARATED, NOW + timedelta(days=6.9)).move is None
    nxt = led.rebalance(SEPARATED, NOW + timedelta(days=7))
    assert nxt.move is not None and nxt.move.amount == pytest.approx(160.0)


def test_the_limit_counts_money_in_and_out_together() -> None:
    """A goal that gave 20% this week cannot also take 20% back the same week (a 40% swing)."""
    led = ledger()
    assert led.rebalance(SEPARATED, NOW).move is not None  # win gives 200 to repeat
    flipped = {"win": proof("win", 2.0, 1.5, 2.5), "repeat": proof("repeat", 0.2, -0.1, 0.5)}
    back = led.rebalance(flipped, NOW + timedelta(days=3))
    assert back.move is None and "weekly limit" in back.reason
    assert led.budgets() == {"win": 800.0, "repeat": 1200.0}


# --- the envelope ------------------------------------------------------------------------------------


def test_never_above_the_receivers_ceiling() -> None:
    led = BudgetLedger(
        [
            GoalBudget(goal_id="win", budget=1000.0),
            GoalBudget(goal_id="repeat", budget=1000.0, ceiling=1050.0),
        ],
        ON,
    )
    res = led.rebalance(SEPARATED, NOW)
    assert res.move is not None and res.move.amount == pytest.approx(50.0)
    assert led.budgets()["repeat"] == 1050.0
    assert led.rebalance(SEPARATED, NOW + timedelta(days=8)).move is None  # at its ceiling: no room


def test_the_giver_keeps_what_it_spent_and_its_floor() -> None:
    led = BudgetLedger(
        [GoalBudget(goal_id="win", budget=1000.0, spent=900.0), GoalBudget(goal_id="repeat", budget=1000.0)],
        ON,
    )
    res = led.rebalance(SEPARATED, NOW)
    assert res.move is not None and res.move.amount == pytest.approx(100.0)  # 1000 - 900 spent
    floor = BudgetLedger(
        [
            GoalBudget(goal_id="win", budget=1000.0, start_budget=1700.0),
            GoalBudget(goal_id="repeat", budget=1000.0),
        ],
        ON,
    )
    res2 = floor.rebalance(SEPARATED, NOW)
    assert res2.move is not None and res2.move.amount == pytest.approx(150.0)  # keeps half of 1700


def test_the_receivers_ceiling_holds_over_several_weeks() -> None:
    led = BudgetLedger(
        [
            GoalBudget(goal_id="win", budget=1000.0),
            GoalBudget(goal_id="repeat", budget=1000.0, ceiling=1300.0),
        ],
        ON,
    )
    got = [led.rebalance(SEPARATED, NOW + timedelta(days=7 * w)).move for w in range(4)]
    assert [m.amount if m else None for m in got] == [200.0, 100.0, None, None]
    assert led.budgets() == {"win": 700.0, "repeat": 1300.0}
    assert led.total() == 2000.0


def test_the_envelope_and_the_log_are_checked_when_the_ledger_is_built() -> None:
    with pytest.raises(BudgetError):
        BudgetLedger([GoalBudget(goal_id="a", budget=900.0)], ON, envelope_total=800.0)
    move = ledger().rebalance(SEPARATED, NOW).move
    assert move is not None
    lines = [GoalBudget(goal_id="win", budget=800.0), GoalBudget(goal_id="repeat", budget=1200.0)]
    restored = BudgetLedger(lines, ON, moves=[move])
    restored.undo(move.move_id, NOW + timedelta(days=1))
    assert restored.budgets() == {"win": 1000.0, "repeat": 1000.0}
    with pytest.raises(BudgetError):  # a log about goals this ledger does not hold
        BudgetLedger([GoalBudget(goal_id="other", budget=10.0)], ON, moves=[move])
    with pytest.raises(BudgetError):  # the same move logged twice
        BudgetLedger(lines, ON, moves=[move, move])


# --- undo --------------------------------------------------------------------------------------------


def test_undo_restores_the_budgets_exactly() -> None:
    led = BudgetLedger(
        [GoalBudget(goal_id="win", budget=1234.57), GoalBudget(goal_id="repeat", budget=333.33)], ON
    )
    start = led.budgets()
    move = led.rebalance(SEPARATED, NOW).move
    assert move is not None and led.budgets() != start
    done = led.undo(move.move_id, NOW + timedelta(hours=2))
    assert led.budgets() == start  # equal, not approximately
    assert done.status == "undone" and done.undone_at is not None
    assert led.moves[0].status == "undone"
    with pytest.raises(BudgetError):
        led.undo(move.move_id, NOW)


def test_undo_last_and_two_moves_back_to_the_start() -> None:
    led = ledger()
    start = led.budgets()
    led.rebalance(SEPARATED, NOW)
    led.rebalance(SEPARATED, NOW + timedelta(days=8))
    led.undo_last(NOW + timedelta(days=9))
    led.undo_last(NOW + timedelta(days=9))
    assert led.budgets() == start
    with pytest.raises(BudgetError):
        led.undo_last(NOW + timedelta(days=9))
    with pytest.raises(BudgetError):
        led.undo("nope", NOW)


def test_an_undone_move_is_not_repeated_straight_away() -> None:
    led = ledger()
    move = led.rebalance(SEPARATED, NOW).move
    assert move is not None
    led.undo(move.move_id, NOW + timedelta(days=1))
    held = led.rebalance(SEPARATED, NOW + timedelta(days=9))
    assert held.move is None and "undid" in held.reason
    later = led.rebalance(SEPARATED, NOW + timedelta(days=40))
    assert later.move is not None


def test_undoing_a_move_the_receiver_has_spent_is_refused() -> None:
    led = ledger()
    move = led.rebalance(SEPARATED, NOW).move
    assert move is not None
    led.update_spent({"repeat": 1100.0})
    with pytest.raises(BudgetError):
        led.undo(move.move_id, NOW)
    assert led.budgets() == {"win": 800.0, "repeat": 1200.0}


# --- the briefing line ---------------------------------------------------------------------------------


def test_the_briefing_entry_is_one_plain_line_with_backed_numbers() -> None:
    led = ledger()
    move = led.rebalance(SEPARATED, NOW).move
    assert move is not None
    names = {"win": "Win-back", "repeat": "Repeat purchase"}
    text, numbers = move_entry(move, names)
    assert "Win-back" in text and "Repeat purchase" in text and "one click" in text and "\n" not in text
    assert check_numbers(text, numbers) == []
    # a name with digits would break the number check, so it is replaced
    text2, numbers2 = move_entry(move, {"win": "Q4 push", "repeat": "Repeat purchase"})
    assert "Q4" not in text2 and check_numbers(text2, numbers2) == []
    section = budget_week([move], NOW - timedelta(days=1), NOW + timedelta(days=6), names)
    assert section is not None and check_numbers(section.body, section.numbers) == []
    lower = budget_week([move], NOW - timedelta(days=1), NOW + timedelta(days=6), names, cap=0.1)
    assert lower is not None and "10.0%" in lower.body and "20.0%" not in lower.body
    assert lower.numbers["weekly_limit_share"] == 0.1 and check_numbers(lower.body, lower.numbers) == []


def test_the_briefing_is_silent_when_nothing_moved_or_it_was_undone() -> None:
    led = ledger()
    assert budget_week(led.moves, NOW - timedelta(days=7), NOW + timedelta(days=1), {}) is None
    move = led.rebalance(SEPARATED, NOW).move
    assert move is not None
    undone = led.undo(move.move_id, NOW)
    assert budget_week([undone], NOW - timedelta(days=7), NOW + timedelta(days=1), {}) is None
    assert budget_week([move], NOW + timedelta(days=1), NOW + timedelta(days=8), {}) is None  # other week


def test_two_moves_in_a_week_keep_distinct_numbers() -> None:
    led = ledger()
    m1 = led.rebalance(SEPARATED, NOW).move
    m2 = led.rebalance(SEPARATED, NOW + timedelta(days=8)).move
    assert m1 is not None and m2 is not None
    section = budget_week([m1, m2], NOW - timedelta(days=1), NOW + timedelta(days=9), {})
    assert section is not None and "budget_moved_1" in section.numbers
    assert check_numbers(section.body, section.numbers) == []


def test_the_analyst_briefing_is_unchanged_without_an_early_read() -> None:
    goal = Goal(
        goal_id="g",
        business_id="b",
        text="win back",
        kind=GoalKind.WIN_BACK,
        budget=100.0,
        starts_at=NOW,
        created_at=NOW,
    )
    health = HealthStatus(as_of=NOW)
    plain = template_briefing(goal, SEPARATED["win"], health, {}, "weekly", NOW)
    assert [s.title for s in plain.sections] == [
        "Extra profit vs people we never contact",
        "Against the simple rule",
        "Customer health",
    ]
