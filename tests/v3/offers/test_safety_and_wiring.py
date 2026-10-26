"""The new offer kinds stay inside v2's safety rules, the Gym keeps its worlds, and the budget price
is wired into the agent's daily loop (opt-in)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.creative import OFFER_THEMES, Creative
from autonomy.agents.critic import BrandRules, check_rules
from autonomy.agents.strategist import TEMPLATES
from autonomy.contracts import (
    NO_OFFER,
    Action,
    Bid,
    Brief,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    OfferKind,
    OfferOption,
)
from autonomy.decide import BetaResponseModel, GoalBidder, ValueModel, rules
from autonomy.decide.pacing import BudgetPrice, OfferPricing
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.gym.scenarios import get_scenario
from autonomy.gym.truth import KIND_INDEX, PlantedTruth
from autonomy.runtime.agent import build_agent_policy

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
V2_KINDS = (
    OfferKind.NONE,
    OfferKind.PERCENT_OFF,
    OfferKind.AMOUNT_OFF,
    OfferKind.FREE_SHIPPING,
    OfferKind.GIFT,
    OfferKind.LOYALTY,
    OfferKind.CONTENT,
)
BUNDLE = OfferOption(
    offer_id="bundle", label="Kettle and tea, 15% off", kind=OfferKind.BUNDLE, amount=15, bundle_discount=20.0
)
EARLY = OfferOption(
    offer_id="early", label="Early access", kind=OfferKind.EARLY_ACCESS, cost_per_redemption=1.0
)


def customer(customer_id: str = "c1", **prefs: str) -> CustomerState:
    return CustomerState.model_validate(
        {
            "customer_id": customer_id,
            "as_of": NOW,
            "purchases": 2,
            "revenue": 200.0,
            "margin": 60.0,
            "last_purchase_at": NOW - timedelta(days=90),
            "preferences": prefs,
            "channels": {Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
        }
    )


def brief_with(*offers: OfferOption) -> Brief:
    brief = sample_brief(NOW)
    env = brief.envelope.model_copy(update={"offers": (*brief.envelope.offers, *offers)})
    return brief.model_copy(update={"envelope": env})


# --- the Gym's random stream ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seed", "theme_sum", "hour_sum", "favourite_sum", "offer_sum"),
    [
        # Computed on the base commit's truth.py with the seven v2 offer kinds (the arrays were also
        # compared element by element against it): the new kinds must not move any of them.
        (1, 638.6366244113, 863, 114, 494.9999755062),
        (7, 636.7386654457, 806, 116, 491.4047025593),
    ],
)
def test_the_gym_plants_the_same_customers_as_v2(
    seed: int, theme_sum: float, hour_sum: int, favourite_sum: int, offer_sum: float
):
    truth = PlantedTruth(get_scenario("discount_cannibalisation", n_customers=60), seed)
    assert float(truth.theme_mult.sum()) == pytest.approx(theme_sum, abs=1e-8)
    assert int(truth.best_hour.sum()) == hour_sum
    assert int(truth.favourite.sum()) == favourite_sum
    v2_columns = [KIND_INDEX[k] for k in V2_KINDS]
    assert v2_columns == list(range(7))  # the old kinds keep their columns
    assert float(truth.offer_mult[:, v2_columns].sum()) == pytest.approx(offer_sum, abs=1e-8)
    # The new kinds have their own stream; with no persona value they multiply by 1.
    assert (truth.offer_mult[:, KIND_INDEX[OfferKind.BUNDLE]] == 1.0).all()


# --- the hard rules -------------------------------------------------------------------------------


def test_a_customer_who_wants_no_discounts_gets_no_bundle():
    brief = brief_with(BUNDLE, EARLY)
    ctx = DecisionContext.model_validate({"now": NOW, "briefs": (brief,)})
    no_deals = customer(no_discounts="true")

    def check(offer_id: str, state: CustomerState) -> rules.RuleResult:
        action = Action(goal_id=brief.goal_id, channel=Channel.EMAIL, offer_id=offer_id, send_at=NOW)
        bid = Bid(
            goal_id=brief.goal_id, action=action, uplift=0.1, value_if_converted=100.0, expected_net=5.0
        )
        return rules.check_bid(bid, state, ctx)

    assert check("bundle", no_deals) == rules.RuleResult(False, rules.NO_DISCOUNTS)
    assert check("early", no_deals).allowed  # early access is not money off
    assert check("bundle", customer()).allowed


# --- the agents -----------------------------------------------------------------------------------


def test_every_offer_kind_has_themes_and_no_template_proposes_a_new_kind():
    assert set(OFFER_THEMES) == set(OfferKind)
    for template in TEMPLATES.values():
        assert template.offer_kinds <= set(V2_KINDS)


def _script(system: str, prompt: str) -> str:
    import json

    has_offer = "{{offer_label}}" in prompt
    body = "Hi {{first_name}}, an idea for {{top_category}}." + (" Use {{offer_label}}." if has_offer else "")
    return json.dumps(
        {"variants": [{"subject": "A note", "body": body}, {"subject": "A note 2", "body": body}]}
    )


def test_the_creative_writes_for_every_new_kind():
    brief = brief_with(BUNDLE, EARLY)
    env = brief.envelope.model_copy(update={"channels": (Channel.EMAIL,)})
    brief = brief.model_copy(update={"envelope": env})
    items = Creative(ScriptedLLM(_script)).build_library(brief, brand_voice="Plain.", n_per_cell=1, now=NOW)
    assert {"bundle", "early"} <= {i.offer_id for i in items}


def test_the_critic_lets_a_bundle_state_its_own_percent_only():
    from autonomy.contracts import ContentItem, Theme

    rules_ = BrandRules(offers=(NO_OFFER, BUNDLE))
    base: dict[str, object] = {
        "content_id": "ci_test",
        "goal_id": "goal_winback",
        "channel": Channel.EMAIL,
        "theme": Theme.SAVINGS,
        "offer_id": "bundle",
        "subject": "Something for you, {{first_name}}",
        "slots": ("first_name",),
        "created_at": NOW,
    }
    tail = " off the kettle and tea this week. Unsubscribe any time."
    ok = ContentItem.model_validate({**base, "body": "15%" + tail})
    bad = ContentItem.model_validate({**base, "body": "30%" + tail})
    assert not any("discount figure" in p for p in check_rules(ok, rules_))
    assert any("unapproved discount figure: 30%" in p for p in check_rules(bad, rules_))


# --- GoalBidder and BudgetPrice over a few days ---------------------------------------------------

PRIORS = BetaResponseModel.priors_from_rates(
    {"treated/*/email|*|*": 0.10, "treated/*/email|pct10|*": 0.30, "control/*": 0.05}, strength=1e6
)


def _run_days(budget: float, days: int = 10) -> tuple[BudgetPrice, list[float], list[int]]:
    """Ten customers a day, worth more the more they spent before; each takes its best bid."""
    price = BudgetPrice(budget=budget, days=days, initial=1.0)
    bidder = GoalBidder(
        sample_brief(NOW),
        sample_content(NOW),
        BetaResponseModel(PRIORS),
        ValueModel(),
        {Channel.EMAIL: 0.5},
        prune_z=0.0,
        budget_price=price,
    )
    ctx = DecisionContext.model_validate({"now": NOW, "briefs": (sample_brief(NOW),)})
    prices: list[float] = []
    offers: list[int] = []
    for day in range(days):
        spend, n = 0.0, 0
        for i in range(10):
            revenue = 40.0 + 40.0 * i
            state = customer(f"d{day}c{i}").model_copy(update={"revenue": revenue, "margin": 0.3 * revenue})
            best = max(bidder.bids(state, ctx), key=lambda b: b.expected_net)
            if best.expected_net > 0 and best.action.offer_id != NO_OFFER.offer_id:
                spend, n = spend + best.offer_cost, n + 1
        prices.append(price())
        offers.append(n)
        price.update(spend)
    return price, prices, offers


def test_the_bidder_and_the_budget_price_keep_offer_spend_to_the_budget():
    # At the starting price the bidder would spend about 126 over ten days; the budget is 80.
    price, prices, offers = _run_days(80.0)
    assert 0.9 * 80.0 <= price.spent <= 1.1 * 80.0
    assert prices[-1] > prices[0]  # the price rose to hold spend back
    assert offers[-1] < offers[0]  # and the customers whose offers earn least per unit stopped getting one
    # A budget that does not bind: the price falls and more customers get the offer.
    _, loose_prices, loose_offers = _run_days(1e6)
    assert loose_prices[-1] < loose_prices[0] and loose_offers[-1] > loose_offers[0]


# --- the agent's daily loop -----------------------------------------------------------------------


def test_off_the_agent_builds_bidders_without_the_hooks():
    policy = build_agent_policy([sample_brief(NOW)], sample_content(NOW))
    bidder = next(iter(policy.bidders.values()))
    assert bidder.budget_price is None and bidder.offer_cost_fn is None
    assert policy.offer_prices == {}


def test_on_the_agent_charges_a_price_and_moves_it_each_day():
    brief = sample_brief(NOW)
    policy = build_agent_policy(
        [brief], sample_content(NOW), offer_pricing=OfferPricing(days=10, initial=0.5, default_basket=50.0)
    )
    bidder = policy.bidders[brief.goal_id]
    price = policy.offer_prices[brief.goal_id]
    assert bidder.budget_price is price and bidder.offer_cost_fn is not None
    assert price.budget == brief.envelope.budget

    action = Action(goal_id=brief.goal_id, channel=Channel.EMAIL, offer_id="pct10", send_at=NOW)
    sent = Decision(
        decision_id="d1",
        customer_id="c1",
        made_at=NOW,
        kind=DecisionKind.ACT,
        action=action,
        probability=1.0,
        reason="test",
        policy_version="t",
    )
    used = Event(
        event_id="p1",
        customer_id="c1",
        type=EventType.PURCHASE,
        ts=NOW + timedelta(hours=5),
        value=100.0,
        source="test",
        attrs={"offer_id": "pct10", "offer_cost": 4500.0},
    )
    policy._decided = True  # as after the first decision; history before it is not spend
    policy.nightly([sent], {"c1": [used]}, NOW + timedelta(hours=20))
    # One day closed with an offer spend of 4,500 against a target of about 500: the price rose.
    assert price.days_left == 9
    assert price.spent == pytest.approx(4500.0)
    assert price.other_spent > 0  # the email's own cost counts against the budget too
    assert price() > 0.5
    # A second nightly the same day does not close another day.
    policy.nightly([sent], {"c1": [used]}, NOW + timedelta(hours=21))
    assert price.days_left == 9 and price.spent == pytest.approx(4500.0)
