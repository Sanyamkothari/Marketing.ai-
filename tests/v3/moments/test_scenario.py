"""The Gym scenario `moments_planted`: carts and refills that make a message worth more for a few days."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import Action, Channel, CustomerState, EventType
from autonomy.data.folder import StateFolder
from autonomy.decide.moments import MomentBook
from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.run import AGENT_NO_MOMENTS, full_folder, moments_agent_factory, moments_gain
from autonomy.gym.run import main as gym_main
from autonomy.gym.scenarios import SCENARIOS
from autonomy.gym.score import GymFolder, ScoreCard, gym_world, run_policy
from autonomy.gym.truth import PlantedTruth
from autonomy.gym.world import GymWorld

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
    world = GymWorld(small())
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
    one_cart = np.asarray(np.isfinite(with_moments.cart_s).sum(axis=1) == 1, dtype=bool)
    no_refill = np.asarray(~np.isfinite(with_moments.refill_s).any(axis=1), dtype=bool)
    row = int(np.argmax(one_cart & no_refill))
    assert bool(one_cart[row]) and bool(no_refill[row])
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


def test_the_refill_window_runs_from_the_lead_to_the_refill_purchase() -> None:
    spec = small(cart_rate_per_day=0.0)
    lead = spec.refill_lead_days
    truth = PlantedTruth(spec, 0)
    row = int(np.argmax(np.isfinite(truth.refill_s[:, 1])))
    due = truth.refill_s[row, 1]

    def factor(days_from_due: float) -> float:
        return float(truth.moment_factor(np.array([row]), np.array([due + days_from_due * 86_400.0]))[0])

    assert factor(-(lead - 0.1)) == pytest.approx(3.0)
    assert factor(-0.01) == pytest.approx(3.0)
    assert factor(-(lead + 0.1)) == pytest.approx(1.0)
    # Restocked: the purchase happens whatever is sent, so a message after it is worth no more.
    assert factor(0.0) == pytest.approx(1.0)
    assert factor(1.0) == pytest.approx(1.0)


def test_folded_states_carry_the_carts_and_the_refill_rhythm() -> None:
    world = gym_world(small(), [default_brief()], default_content())
    folder = StateFolder()
    states: dict[str, object] = {}
    for e in world.reset(0):
        states[e.customer_id] = folder.fold(states.get(e.customer_id), e)  # type: ignore[arg-type]
    carted = [s for s in states.values() if s.last_cart_at is not None]  # type: ignore[attr-defined]
    rhythm = [s for s in states.values() if s.purchase_gap_days is not None]  # type: ignore[attr-defined]
    assert carted and rhythm
    # The refill product's own rhythm, apart from the customer's other buying.
    refills = [
        s.bought_items[k]  # type: ignore[attr-defined]
        for s in states.values()
        for k in s.bought_items  # type: ignore[attr-defined]
        if k.endswith("_refill")
    ]
    assert refills
    gaps = [b.gap_days for b in refills if b.gap_days is not None]
    assert gaps and all(20.0 < g < 40.0 for g in gaps)


def test_the_gym_folder_alone_keeps_no_moment_fields_and_the_full_one_does() -> None:
    world = gym_world(small(), [default_brief()], default_content())
    plain, full = GymFolder(), full_folder()
    a: dict[str, CustomerState] = {}
    b: dict[str, CustomerState] = {}
    for e in world.reset(0):
        a[e.customer_id] = plain.fold(a.get(e.customer_id), e)
        b[e.customer_id] = full.fold(b.get(e.customer_id), e)
    assert not any(s.last_cart_at for s in a.values())
    assert any(s.last_cart_at for s in b.values())


def test_the_gym_customers_have_no_country_so_no_calendar_moment() -> None:
    world = gym_world(small(), [default_brief()], default_content())
    folder = full_folder()
    states: dict[str, CustomerState] = {}
    for e in world.reset(0):
        states[e.customer_id] = folder.fold(states.get(e.customer_id), e)
    book = MomentBook.load()
    diwali = datetime(2026, 11, 8, 12, tzinfo=UTC)
    payday = datetime(2026, 2, 1, 12, tzinfo=UTC)
    for s in list(states.values())[:50]:
        live = {m.name for m in book.active(s, diwali)} | {m.name for m in book.active(s, payday)}
        assert not live & {"payday", "festival"}


def test_moments_gain_reads_the_two_agents_per_seed() -> None:
    cards = [
        ScoreCard.model_construct(policy="agent", seed=0, true_value=10.0),
        ScoreCard.model_construct(policy=AGENT_NO_MOMENTS, seed=0, true_value=8.0),
        ScoreCard.model_construct(policy="agent", seed=1, true_value=5.0),
        ScoreCard.model_construct(policy=AGENT_NO_MOMENTS, seed=1, true_value=6.0),
        ScoreCard.model_construct(policy="oracle", seed=1, true_value=99.0),
    ]
    gain = moments_gain(cards)
    assert gain["gain_per_seed"] == [2.0, -1.0]
    assert gain["mean_gain"] == pytest.approx(0.5)
    assert gain["beats_without"] is True
    assert moments_gain([])["mean_gain"] is None


def test_the_moments_flag_is_for_the_agent_only() -> None:
    with pytest.raises(SystemExit):
        gym_main(["--scenario", "moments_planted", "--policy", "rule_baseline", "--moments"])


def test_smoke_run_with_moments_on_keeps_every_rule() -> None:
    """200 customers for 14 days, as `--moments` runs it. A wiring check only: the money is noise."""
    card = run_policy(small(), moments_agent_factory, seed=0, folder=full_folder(), with_oracle=False)
    assert card.scenario == "moments_planted"
    assert card.actions > 0
    assert card.violations == 0
