"""The budget price: moved day by day by pacing, it steers per-customer choices to the global plan."""

from __future__ import annotations

import math
import random

import pytest

from autonomy.decide.pacing import BudgetPrice
from autonomy.decide.planner import OfferCandidate, choose_at_price, plan_offers

DAYS = 30
PER_DAY = 300


def menu(rng: random.Random, customer_id: str) -> list[OfferCandidate]:
    """Two offers per customer; a quarter are sure things (every offer loses money)."""
    if rng.random() < 0.25:
        return [OfferCandidate(customer_id, "pct", -rng.uniform(0.5, 5.0), rng.uniform(5.0, 20.0))]
    cheap_cost = rng.uniform(2.0, 10.0)
    cheap = OfferCandidate(customer_id, "ship", cheap_cost * rng.uniform(0.3, 6.0), cheap_cost)
    dear_cost = rng.uniform(10.0, 30.0)
    dear = OfferCandidate(
        customer_id, "pct", cheap.extra_profit + dear_cost * rng.uniform(0.0, 2.0), dear_cost
    )
    return [cheap, dear]


def population(seed: int, day: int) -> list[list[OfferCandidate]]:
    rng = random.Random(seed * 1000 + day)
    return [menu(rng, f"d{day}c{i}") for i in range(PER_DAY)]


def spend_at(menus: list[list[OfferCandidate]], price: float) -> float:
    return sum(c.cost for m in menus if (c := choose_at_price(m, price)) is not None)


def test_the_price_converges_to_the_plans_and_spend_keeps_to_budget():
    # Fresh customers every day from the same distribution. The planner, told the day's budget and
    # shown everyone, knows the right price; the daily loop sees one customer at a time.
    reference = population(0, -1)
    pool = [c for m in reference for c in m]
    full_info = plan_offers(pool, budget=2000.0)  # a day's worth of the budget below
    assert full_info.price > 0

    budget = 2000.0 * DAYS
    price = BudgetPrice(budget, DAYS, initial=0.1)  # start far from the right price
    daily = []
    prices = []
    for day in range(DAYS):
        daily.append(spend_at(population(1, day), price()))
        prices.append(price())
        price.update(daily[-1])

    # The whole budget is used and not exceeded, to within a percent.
    assert budget * 0.99 <= price.spent <= budget * 1.01
    # The price found without seeing anyone else is the planner's, to within a quarter, from the
    # second week on.
    assert all(full_info.price / 1.25 <= p <= full_info.price * 1.25 for p in prices[10:])
    # It started too low, so the first days overspent; the price rose until spend came back under
    # the (now smaller) daily target.
    assert daily[0] > 2000.0 * 1.5
    assert prices[0] < prices[5] < full_info.price * 1.25


def test_the_price_comes_down_when_the_budget_is_loose():
    budget = 1e6
    price = BudgetPrice(budget, DAYS, initial=5.0)
    for day in range(DAYS // 2):
        price.update(spend_at(population(2, day), price()))
    assert price() == 0.0  # not binding: every offer that pays fits


def test_the_price_reads_zero_unbound_and_the_ceiling_when_spent():
    p = BudgetPrice(100.0, 4, initial=0.5)
    assert p() == pytest.approx(0.5)
    p.update(0.0)  # spent nothing against 25: down
    assert 0 < p() < 0.5
    p.update(100.0)  # blew the whole budget in one day
    assert p.exhausted and p() == p.ceiling
    assert p.daily_target == 0.0


def test_a_miss_is_made_up_over_the_days_left():
    p = BudgetPrice(120.0, 4)
    assert p.daily_target == 30.0
    p.update(0.0)
    assert p.daily_target == pytest.approx(40.0)
    p.update(70.0)
    assert p.daily_target == pytest.approx(25.0)
    assert (p.spent, p.remaining, p.days_left) == (70.0, 50.0, 2)


def test_one_wild_day_cannot_throw_the_price_off_by_more_than_the_cap():
    p = BudgetPrice(1000.0, 10, initial=1.0, gain=0.5, max_miss=3.0)
    p.update(99.0)  # a day of 99 against a target of 100 (no change worth noting)
    before = p()
    p.update(500.0)  # five times the target, capped at a miss of 3
    assert p() / before <= math.exp(0.5 * 3.0) + 1e-9


def test_budget_price_rejects_nonsense():
    with pytest.raises(ValueError):
        BudgetPrice(-1.0, 3)
    with pytest.raises(ValueError):
        BudgetPrice(10.0, 0)
    with pytest.raises(ValueError):
        BudgetPrice(10.0, 3, gain=0.0)
    with pytest.raises(ValueError):
        BudgetPrice(10.0, 3).update(-1.0)
