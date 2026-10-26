"""The Gym scenario `moments_planted`: carts and refills that make a message worth more for a few days."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from autonomy.contracts import Action, Channel, EventType
from autonomy.data.folder import StateFolder
from autonomy.decide.bidders import use_moments
from autonomy.decide.moments import MomentBook
from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.run import agent_factory
from autonomy.gym.scenarios import SCENARIOS
from autonomy.gym.score import gym_world, run_policy
from autonomy.gym.truth import PlantedTruth

SMALL = {"n_customers": 200, "history_days": 60, "days": 14}


def small(**kw: object) -> ScenarioSpec:
    return get_scenario("moments_planted", **{**SMALL, **kw})


def test_the_scenario_is_last_in_the_registry_and_no_other_plants_moments() -> None:
    assert list(SCENARIOS)[-1] == "moments_planted"
    for name, spec in SCENARIOS.items():
        planted = spec.cart_rate_per_day > 0 or spec.refill_share > 0
        assert planted is (name == "moments_planted")


def test_no_moments_means_a_factor_of_one_and_no_planted_events() -> None:
    spec = get_scenario("smoke")
    truth = PlantedTruth(spec, 0)
    assert not truth.moments_on
    factor = truth.moment_factor(np.arange(5), np.zeros(5))
    assert factor.tolist() == [1.0] * 5
    world = gym_world(spec, [default_brief()], default_content())
    history = world.reset(0)
    assert not [e for e in history if e.type is EventType.CART or e.item_id]


def test_the_world_emits_carts_and_refill_purchases_with_item_ids() -> None:
    world = gym_world(small(), [default_brief()], default_content())
    history = world.reset(0)
    carts = [e for e in history if e.type is EventType.CART]
    refills = [e for e in history if e.type is EventType.PURCHASE and e.item_id]
    assert carts and refills
    assert all(e.item_id and e.category for e in carts)
    assert [e.ts for e in history] == sorted(e.ts for e in history)
    later = list(world.advance(world.start + timedelta(days=14)))
    assert [e for e in later if e.type is EventType.CART]
    assert [e for e in later if e.type is EventType.PURCHASE and e.item_id]
    assert all(e.ts >= world.start for e in later)


def test_events_repeat_exactly_from_the_same_seed() -> None:
    def ids() -> list[str]:
        world = gym_world(small(), [default_brief()], default_content())
        return [e.event_id for e in world.reset(3) if e.type is EventType.CART]

    assert ids() == ids()


def test_a_message_at_a_fresh_cart_has_the_planted_multiple_of_the_lift_and_it_fades() -> None:
    with_moments = PlantedTruth(small(), 0)
    without = PlantedTruth(small(cart_lift_multiplier=1.0, refill_lift_multiplier=1.0), 0)
    # Only the lift differs; the customers are the same.
    assert with_moments.uplift.tolist() == without.uplift.tolist()
    one_cart = np.isfinite(with_moments.cart_s).sum(axis=1) == 1
    no_refill = ~np.isfinite(with_moments.refill_s).any(axis=1)
    row = int(np.argmax(one_cart & no_refill))
    assert one_cart[row] and no_refill[row]
    when = float(with_moments.cart_s[row, 0]) + 1.0  # a second after the cart

    def lift(truth: PlantedTruth, at: float) -> float:
        start = truth.start
        action = Action(goal_id="g", channel=Channel.EMAIL, offer_id="none", send_at=start)
        t = start + timedelta(seconds=at)
        return float(truth.evaluate(np.array([row]), [action], [t]).lift_per_day[0])

    base = lift(without, when)
    assert base > 0
    assert lift(with_moments, when) / base == pytest.approx(3.0, abs=1e-3)
    day_later = lift(with_moments, when + 86_400.0) / lift(without, when + 86_400.0)
    assert day_later == pytest.approx(2.0, abs=1e-3)  # the raise halves every day: 1 + 2 x 0.5
    far = lift(with_moments, when + 20 * 86_400.0) / lift(without, when + 20 * 86_400.0)
    assert far == pytest.approx(1.0, abs=1e-4)


def test_the_refill_window_runs_from_the_lead_to_the_late_days() -> None:
    truth = PlantedTruth(small(cart_rate_per_day=0.0), 0)
    row = int(np.argmax(np.isfinite(truth.refill_s[:, 1])))
    due = truth.refill_s[row, 1]
    inside = truth.moment_factor(np.array([row]), np.array([due - 1.9 * 86_400.0]))[0]
    late = truth.moment_factor(np.array([row]), np.array([due + 3.9 * 86_400.0]))[0]
    early = truth.moment_factor(np.array([row]), np.array([due - 2.1 * 86_400.0]))[0]
    after = truth.moment_factor(np.array([row]), np.array([due + 4.1 * 86_400.0]))[0]
    assert inside == pytest.approx(3.0)
    assert late == pytest.approx(3.0)
    assert early == pytest.approx(1.0)
    assert after == pytest.approx(1.0)


def test_folded_states_carry_the_carts_and_the_refill_rhythm() -> None:
    world = gym_world(small(), [default_brief()], default_content())
    folder = StateFolder()
    states: dict[str, object] = {}
    for e in world.reset(0):
        states[e.customer_id] = folder.fold(states.get(e.customer_id), e)  # type: ignore[arg-type]
    carted = [s for s in states.values() if s.last_cart_at is not None]  # type: ignore[attr-defined]
    rhythm = [s for s in states.values() if s.purchase_gap_days is not None]  # type: ignore[attr-defined]
    assert carted and rhythm


def test_smoke_run_with_moments_on_keeps_every_rule() -> None:
    """200 customers for 14 days. A wiring check only: at this size the money is noise."""
    use_moments(MomentBook.load())
    try:
        card = run_policy(small(), agent_factory, seed=0, folder=StateFolder(), with_oracle=False)
    finally:
        use_moments(None)
    assert card.scenario == "moments_planted"
    assert card.actions > 0
    assert card.violations == 0
