"""The planner: fill the budget by extra profit per unit of cost, never over, sure things get nothing."""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from autonomy.contracts import (
    NO_OFFER,
    Bid,
    Channel,
    Envelope,
    OfferKind,
    OfferOption,
)
from autonomy.contracts.decisions import Action
from autonomy.decide.planner import (
    OVER_BUDGET,
    SURE_THING,
    OfferCandidate,
    candidates_from_bids,
    choose_at_price,
    offer_economics,
    plan_offers,
)

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)


def cand(customer: str, offer: str, profit: float, cost: float) -> OfferCandidate:
    return OfferCandidate(customer, offer, profit, cost)


def test_anitas_example_free_shipping_beats_twenty_percent_off():
    # Both work for her. 20% off lifts her a little more but costs 180 against 40, so after the cost
    # free shipping leaves more profit (and far more per rupee).
    anita = [cand("anita", "freeship", 100.0, 40.0), cand("anita", "pct20", 80.0, 180.0)]
    plan = plan_offers(anita, budget=1000.0)
    assert plan.offer_for("anita") == "freeship"
    assert plan.spent == 40.0
    # Even when 20% off nets slightly more, a tight budget buys free shipping first.
    anita = [cand("anita", "freeship", 100.0, 40.0), cand("anita", "pct20", 130.0, 180.0)]
    assert plan_offers(anita, budget=100.0).offer_for("anita") == "freeship"
    assert plan_offers(anita, budget=1000.0).offer_for("anita") == "pct20"


def test_the_budget_is_filled_from_the_best_ratio_down():
    pairs = [
        cand("a", "o", 10.0, 10.0),  # ratio 1
        cand("b", "o", 60.0, 20.0),  # ratio 3
        cand("c", "o", 40.0, 20.0),  # ratio 2
        cand("d", "o", 5.0, 10.0),  # ratio 0.5
    ]
    plan = plan_offers(pairs, budget=40.0)
    assert [a.customer_id for a in plan.assignments] == ["b", "c"]
    assert plan.spent == 40.0
    assert plan.left_out == {"a": OVER_BUDGET, "d": OVER_BUDGET}
    # the budget price is the ratio of the best pair that did not fit
    assert plan.price == pytest.approx(1.0)


def test_ratio_not_size_decides_a_big_profit_can_lose_to_two_small_efficient_ones():
    pairs = [cand("big", "o", 100.0, 100.0), cand("s1", "o", 30.0, 10.0), cand("s2", "o", 30.0, 10.0)]
    plan = plan_offers(pairs, budget=100.0)
    assert {a.customer_id for a in plan.assignments} == {"s1", "s2"}
    assert plan.extra_profit == 60.0


def test_a_pair_that_does_not_fit_is_skipped_and_a_smaller_one_below_it_still_goes():
    pairs = [cand("a", "o", 90.0, 30.0), cand("b", "o", 60.0, 30.0), cand("c", "o", 14.0, 10.0)]
    plan = plan_offers(pairs, budget=45.0)
    assert [a.customer_id for a in plan.assignments] == ["a", "c"]
    assert plan.spent == 40.0


def test_never_exceeds_the_budget_on_random_populations():
    rng = random.Random(7)
    for trial in range(40):
        pairs = [
            cand(f"c{i}", f"o{k}", rng.uniform(-5, 40), rng.uniform(0, 30))
            for i in range(60)
            for k in range(rng.randint(1, 3))
        ]
        budget = rng.uniform(0, 600)
        plan = plan_offers(pairs, budget)
        assert plan.spent <= budget + 1e-9, trial
        assert sum(a.cost for a in plan.assignments) == pytest.approx(plan.spent)
        assert len({a.customer_id for a in plan.assignments}) == len(plan.assignments)  # one offer each
        assert all(a.extra_profit > 0 for a in plan.assignments)


def test_sure_things_never_get_an_offer_even_with_budget_to_spare():
    sure = [
        cand("sure", "pct10", -3.0, 6.0),
        cand("sure", "freeship", -0.5, 4.0),
        cand("sure", "gift", 0.0, 2.0),
    ]
    plan = plan_offers([*sure, cand("persuadable", "pct10", 20.0, 6.0)], budget=1e9)
    assert plan.offer_for("sure") == NO_OFFER.offer_id
    assert plan.left_out == {"sure": SURE_THING}
    assert plan.offer_for("persuadable") == "pct10"
    assert plan.price == 0.0  # the budget did not bind


def test_offer_economics_makes_a_sure_thing_negative():
    # Organic chance 0.9: the offer adds 0.01 to their chance of buying but is paid by 0.91 of them.
    profit, spend = offer_economics(uplift_extra=0.01, value=100.0, redemption_cost=10.0, p_redeem=0.91)
    assert spend == pytest.approx(9.1)
    assert profit == pytest.approx(1.0 - 9.1)
    persuadable, _ = offer_economics(uplift_extra=0.20, value=100.0, redemption_cost=10.0, p_redeem=0.25)
    assert persuadable > 0


def test_a_customer_climbs_to_a_dearer_offer_only_when_the_budget_allows():
    # Cheap: +50 for 10 (ratio 5). Dear: +80 for 40, so the step up is +30 for 30 (ratio 1).
    pairs = [cand("a", "cheap", 50.0, 10.0), cand("a", "dear", 80.0, 40.0), cand("b", "cheap", 40.0, 10.0)]
    tight = plan_offers(pairs, budget=20.0)
    assert {a.customer_id: a.offer_id for a in tight.assignments} == {"a": "cheap", "b": "cheap"}
    roomy = plan_offers(pairs, budget=100.0)
    assert {a.customer_id: a.offer_id for a in roomy.assignments} == {"a": "dear", "b": "cheap"}
    assert roomy.spent == 50.0
    # the upgrade does not fit, and nothing above it is tried
    assert plan_offers(pairs, budget=25.0).offer_for("a") == "cheap"


def test_dominated_offers_are_dropped():
    # "dear" costs more and earns less than "cheap"; the middle one is below the line from cheap to top.
    pairs = [
        cand("a", "cheap", 50.0, 10.0),
        cand("a", "dear", 45.0, 30.0),
        cand("a", "mid", 52.0, 25.0),
        cand("a", "top", 100.0, 30.0),
    ]
    plan = plan_offers(pairs, budget=100.0)
    assert plan.offer_for("a") == "top"


def test_free_offers_that_pay_come_first():
    plan = plan_offers([cand("a", "gift", 5.0, 0.0), cand("b", "o", 50.0, 10.0)], budget=10.0)
    assert {a.customer_id for a in plan.assignments} == {"a", "b"}


def test_min_ratio_keeps_out_pairs_that_barely_pay():
    pairs = [cand("a", "o", 10.0, 100.0), cand("b", "o", 50.0, 10.0)]
    plan = plan_offers(pairs, budget=1e6, min_ratio=0.5)
    assert [a.customer_id for a in plan.assignments] == ["b"]
    assert plan.left_out == {"a": SURE_THING}


def test_zero_budget_gives_only_free_offers():
    plan = plan_offers([cand("a", "o", 10.0, 5.0)], budget=0.0)
    assert plan.assignments == ()
    assert plan.left_out == {"a": OVER_BUDGET}


def test_the_plan_is_deterministic_and_independent_of_input_order():
    rng = random.Random(3)
    pairs = [cand(f"c{i}", "o", 10.0, 5.0) for i in range(20)]  # all tied
    shuffled = pairs[:]
    rng.shuffle(shuffled)
    assert plan_offers(pairs, 50.0) == plan_offers(shuffled, 50.0)


def test_bad_inputs_are_refused():
    with pytest.raises(ValueError):
        plan_offers([], budget=-1.0)
    with pytest.raises(ValueError):
        OfferCandidate("a", "o", float("nan"), 1.0)
    with pytest.raises(ValueError):
        OfferCandidate("a", "o", 1.0, -1.0)


# --- the envelope ---------------------------------------------------------------------------------

PCT10 = OfferOption(offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10)
VIP = OfferOption(offer_id="vip", label="VIP", kind=OfferKind.PERCENT_OFF, amount=30, needs_approval=True)


def test_the_plan_stays_inside_the_envelope():
    env = Envelope(channels=(Channel.EMAIL,), offers=(NO_OFFER, PCT10, VIP), budget=25.0)
    pairs = [
        cand("a", "pct10", 40.0, 10.0),
        cand("b", "pct10", 30.0, 10.0),
        cand("c", "pct10", 20.0, 10.0),
        cand("d", "vip", 500.0, 5.0),  # waits for a person: never planned
        cand("e", "unlisted", 500.0, 5.0),  # not in the envelope at all
    ]
    plan = plan_offers(pairs, budget=1e6, envelope=env)  # the envelope's budget caps it
    assert plan.budget == 25.0
    assert [a.customer_id for a in plan.assignments] == ["a", "b"]
    assert plan.offer_for("d") == "none" and plan.offer_for("e") == "none"


# --- one customer's choice at a price -------------------------------------------------------------


def test_choosing_alone_at_the_plans_price_stays_within_the_plan():
    rng = random.Random(11)
    menus = {
        f"c{i}": [
            cand(f"c{i}", f"o{k}", rng.uniform(-3, 30), rng.uniform(1, 25)) for k in range(rng.randint(1, 3))
        ]
        for i in range(80)
    }
    plan = plan_offers([c for menu in menus.values() for c in menu], budget=300.0)
    assert plan.price > 0
    planned = {a.customer_id: a for a in plan.assignments}
    spent_alone = 0.0
    for customer_id, menu in menus.items():
        alone = choose_at_price(menu, plan.price)
        if alone is None:
            continue
        # Everything worth more per unit than the marginal pair is in the plan, at least as high up.
        assert customer_id in planned and planned[customer_id].cost >= alone.cost - 1e-9
        spent_alone += alone.cost
    assert 0.8 * plan.spent <= spent_alone <= plan.spent + 1e-9


def test_choose_at_price_never_picks_a_sure_thing_whatever_the_price():
    menu = [cand("s", "a", -1.0, 5.0), cand("s", "b", 0.0, 0.0)]
    for price in (0.0, 0.5, 10.0):
        assert choose_at_price(menu, price) is None


def test_choose_at_price_trades_profit_for_cost():
    menu = [cand("a", "cheap", 50.0, 10.0), cand("a", "dear", 80.0, 40.0)]
    assert choose_at_price(menu, 0.0) is menu[1]
    assert choose_at_price(menu, 2.0) is menu[0]
    assert choose_at_price(menu, 6.0) is None
    with pytest.raises(ValueError):
        choose_at_price(menu, -1.0)


# --- from bids ------------------------------------------------------------------------------------


def bid(offer_id: str, net: float, offer_cost: float = 0.0) -> Bid:
    return Bid(
        goal_id="g",
        action=Action(goal_id="g", channel=Channel.EMAIL, offer_id=offer_id, send_at=NOW),
        uplift=0.1,
        value_if_converted=100.0,
        offer_cost=offer_cost,
        expected_net=net,
    )


def test_candidates_from_bids_measure_each_offer_against_the_best_plain_bid():
    bids = [
        bid("none", 2.0),
        bid("none", 3.0),
        bid("pct10", 8.0, 1.8),
        bid("pct10", 5.0, 1.8),
        bid("ship", 2.5, 4.0),
    ]
    out = {c.offer_id: c for c in candidates_from_bids("c1", bids)}
    assert out["pct10"].extra_profit == pytest.approx(5.0) and out["pct10"].cost == pytest.approx(1.8)
    assert out["ship"].extra_profit == pytest.approx(-0.5)  # a sure thing for this offer
    assert plan_offers(out.values(), 100.0).offer_for("c1") == "pct10"


def test_a_plain_message_not_worth_sending_is_measured_against_doing_nothing():
    out = candidates_from_bids("c1", [bid("none", -4.0), bid("pct10", 1.0, 1.0)])
    assert out[0].extra_profit == pytest.approx(1.0)
