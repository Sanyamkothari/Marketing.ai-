"""Bidders and the small models they read: audience filters, response, value, fatigue, GoalBidder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import (
    Action,
    Audience,
    AudienceFilter,
    Channel,
    ChannelStats,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    GoalKind,
    Group,
    Objective,
    OfferKind,
    OfferOption,
    Reward,
    Theme,
)
from autonomy.decide import (
    ANY,
    DEFAULT_CHANNEL_COSTS,
    AutonomousPolicy,
    BetaResponseModel,
    FatigueModel,
    FatigueObservation,
    GoalBidder,
    ResponseModel,
    ResponseObservation,
    Urgency,
    ValueModel,
    action_key,
    matches,
)
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.interfaces import Bidder

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
REACHABLE = ChannelStats(address_known=True, consent=True)


def state(**kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": "c1",
        "as_of": NOW,
        "purchases": 2,
        "revenue": 200.0,
        "margin": 60.0,
        "last_purchase_at": NOW - timedelta(days=90),
        "channels": {Channel.EMAIL: REACHABLE},
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def aud(*filters: AudienceFilter) -> Audience:
    return Audience(description="t", filters=filters)


def f(field: str, op: str, value: object = None) -> AudienceFilter:
    return AudienceFilter.model_validate({"field": field, "op": op, "value": value})


# --- audience ---------------------------------------------------------------------------------


def test_empty_audience_is_everyone():
    assert matches(state(), aud(), NOW)


@pytest.mark.parametrize(
    ("flt", "expected"),
    [
        (f("purchases", "gte", 2), True),
        (f("purchases", "gt", 2), False),
        (f("revenue", "lt", 200.5), True),
        (f("purchases", "eq", 2), True),
        (f("purchases", "ne", 2), False),
        (f("group", "eq", "agent"), True),
        (f("top_categories", "in", ("shoes", "hats")), True),
        (f("top_categories", "not_in", ("shoes",)), False),
        (f("top_categories", "not_in", ("toys",)), True),
        (f("segment", "in", ("vip",)), True),
        (f("last_purchase_at", "days_since_gt", 60), True),
        (f("last_purchase_at", "days_since_lt", 60), False),
        (f("last_purchase_at", "lt", (NOW - timedelta(days=30)).isoformat()), True),
        (f("avg_order_value", "gte", 100), True),  # a property
        (f("scores.churn_risk", "gt", 0.5), True),
        (f("channels.email.consent", "eq", True), True),
        (f("first_seen_at", "is_null"), True),
        (f("first_seen_at", "not_null"), False),
    ],
)
def test_filter_ops(flt: AudienceFilter, expected: bool):
    s = state(top_categories=("shoes",), segment="vip", scores={"churn_risk": 0.7})
    assert matches(s, aud(flt), NOW) is expected


@pytest.mark.parametrize("op", ["eq", "ne", "gt", "in", "is_null", "not_null", "days_since_gt"])
def test_missing_fields_are_false_for_every_op(op: str):
    value: object = ("x",) if op == "in" else 1
    assert not matches(state(), aud(f("no_such_field", op, value)), NOW)
    assert not matches(state(), aud(f("scores.absent", op, value)), NOW)


def test_none_value_fails_comparisons_never_bought_is_not_lapsed():
    s = state(purchases=0, revenue=0.0, margin=0.0, last_purchase_at=None)
    assert not matches(s, aud(f("last_purchase_at", "days_since_gt", 60)), NOW)
    assert not matches(s, aud(f("last_purchase_at", "days_since_lt", 60)), NOW)
    assert not matches(s, aud(f("last_purchase_at", "ne", "x")), NOW)
    assert matches(s, aud(f("last_purchase_at", "is_null")), NOW)


def test_mismatched_types_are_false_not_errors():
    assert not matches(state(), aud(f("purchases", "gt", "two")), NOW)
    assert not matches(state(), aud(f("last_purchase_at", "gt", "not a date")), NOW)
    assert not matches(state(), aud(f("purchases", "in", 2)), NOW)


def test_all_filters_must_pass():
    assert not matches(state(), aud(f("purchases", "gte", 1), f("purchases", "gte", 5)), NOW)


# --- response model ---------------------------------------------------------------------------


def test_untried_action_has_small_positive_uplift_and_wide_spread():
    mean, std = BetaResponseModel().predict(state(), "email|none|reminder")
    assert 0.0 < mean < 0.03
    assert std > 0.03


def test_learns_uplift_from_treated_and_control():
    m = BetaResponseModel()
    key = "email|none|reminder"
    m.update([ResponseObservation(None, key, converted=i < 300) for i in range(1000)])
    m.update([ResponseObservation(None, None, converted=i < 100) for i in range(1000)])
    mean, std = m.predict(state(), key)
    assert mean == pytest.approx(0.2, abs=0.01)
    assert std < 0.02


def test_sure_things_get_no_uplift():
    m = BetaResponseModel()
    key = "email|pct10|savings"
    m.update([ResponseObservation("loyal", key, converted=i < 800) for i in range(1000)])
    m.update([ResponseObservation("loyal", None, converted=i < 800) for i in range(1000)])
    mean, _ = m.predict(state(segment="loyal"), key)
    assert abs(mean) < 0.01


def test_segments_learn_separately_but_borrow_from_each_other():
    m = BetaResponseModel(pool_strength=20)
    key = "email|none|reminder"
    m.update([ResponseObservation("a", key, converted=i < 500) for i in range(1000)])
    a_rate = m.posterior_treated("a", key).mean
    new_rate = m.posterior_treated("b", key).mean
    untouched = BetaResponseModel().posterior_treated("b", key).mean
    assert a_rate == pytest.approx(0.5, abs=0.01)
    assert untouched < new_rate < a_rate  # borrowed, but only a little


def test_injected_priors_most_specific_wins():
    priors = BetaResponseModel.priors_from_rates(
        {
            "treated/*/email|*|*": 0.10,
            "treated/*/email|none|education": 0.20,
            "treated/vip/email|*|*": 0.30,
            "control/*": 0.02,
        },
        strength=100,
    )
    m = BetaResponseModel(priors)
    assert m.treated_rate(state(), "email|none|education") == pytest.approx(0.20)
    assert m.treated_rate(state(), "email|pct10|savings") == pytest.approx(0.10)
    assert m.treated_rate(state(segment="vip"), "email|none|education") == pytest.approx(0.30)
    assert m.posterior_control(ANY).mean == pytest.approx(0.02)
    assert m.treated_rate(state(), "whatsapp|none|reminder") == pytest.approx(0.05)  # default


def test_action_key_shape():
    assert action_key(Channel.EMAIL, "pct10", "savings") == "email|pct10|savings"


# --- value and fatigue ------------------------------------------------------------------------


def test_value_uses_own_history_then_defaults():
    vm = ValueModel(default_order_value=50, default_margin_rate=0.4, future_orders=2)
    assert vm.value_if_converted(state()) == pytest.approx(100 * 0.3)
    assert vm.value_if_converted(state(), Objective.REVENUE) == pytest.approx(100)
    newbie = state(purchases=0, revenue=0.0, margin=0.0)
    assert vm.value_if_converted(newbie) == pytest.approx(20)
    assert vm.future_value(newbie) == pytest.approx(40)
    assert vm.future_value(state(scores={"clv_90d": 77.0})) == pytest.approx(77)


def test_attention_price_rises_with_contacts():
    fm = FatigueModel()
    prices = [fm.price(state(contacts_7d=n)) for n in range(5)]
    assert prices == sorted(prices)
    assert prices[0] == pytest.approx(0.002 * 60, rel=0.01)  # base rate x (100 x 0.3 x 2)
    assert prices[4] > 4 * prices[0]


def test_fatigue_learns_from_unsubscribes_and_stays_monotone():
    fm = FatigueModel(prior_strength=100)
    before = fm.p_unsub(1)
    fm.update([FatigueObservation(1, unsubscribed=i < 50) for i in range(1000)])
    assert fm.p_unsub(1) > 10 * before
    assert fm.p_unsub(2) >= fm.p_unsub(1)  # more contacts never make the next one safer
    assert fm.p_unsub(99) == fm.p_unsub(6)


# --- GoalBidder -------------------------------------------------------------------------------


def ctx(now: datetime = NOW, **kw: object) -> DecisionContext:
    return DecisionContext.model_validate({"now": now, "briefs": (sample_brief(now),), **kw})


def test_goal_bidder_is_a_bidder():
    assert isinstance(GoalBidder(sample_brief(NOW), sample_content(NOW)), Bidder)
    assert isinstance(BetaResponseModel(), ResponseModel)


def test_not_in_audience_gives_no_bids():
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW))
    recent = state(last_purchase_at=NOW - timedelta(days=5))
    assert bidder.bids(recent, ctx()) == []


def test_unreachable_customer_gives_no_bids():
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW))
    gone = state(channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True, unsubscribed=True)})
    assert bidder.bids(gone, ctx()) == []


def test_candidates_are_reachable_channels_x_offers_x_matching_content():
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW))
    bids = bidder.bids(state(), ctx())
    # email only: none/reminder, none/education, pct10/savings; freeship has no email content
    combos = sorted((b.action.channel, b.action.offer_id, b.action.content_id) for b in bids)
    assert combos == [
        (Channel.EMAIL, "none", "ci_0"),
        (Channel.EMAIL, "none", "ci_2"),
        (Channel.EMAIL, "pct10", "ci_1"),
    ]
    both = state(channels={Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE})
    assert len(bidder.bids(both, ctx())) == 5
    assert all(b.goal_id == "goal_winback" and b.action.send_at == NOW for b in bids)


def test_content_from_context_and_only_approved_for_this_goal():
    items = sample_content(NOW)
    items[0] = items[0].model_copy(update={"status": ContentStatus.DRAFT})
    items[2] = items[2].model_copy(update={"goal_id": "other_goal"})
    bidder = GoalBidder(sample_brief(NOW))
    bids = bidder.bids(state(), ctx(content=tuple(items)))
    assert [b.action.content_id for b in bids] == ["ci_1"]


def test_expected_net_formula():
    priors = BetaResponseModel.priors_from_rates(
        {"treated/*/email|*|*": 0.10, "treated/*/email|pct10|*": 0.30, "control/*": 0.05}, strength=1e6
    )
    costs = {Channel.EMAIL: 0.5}
    bidder = GoalBidder(
        sample_brief(NOW), sample_content(NOW), BetaResponseModel(priors), ValueModel(), costs
    )
    bids = {b.action.offer_id + "/" + str(b.action.content_id): b for b in bidder.bids(state(), ctx())}
    value = 100 * 0.3
    plain = bids["none/ci_0"]
    assert plain.uplift == pytest.approx(0.05, abs=1e-4)
    assert plain.offer_cost == 0.0
    assert plain.expected_net == pytest.approx(0.05 * value - 0.5, abs=1e-3)
    offer = bids["pct10/ci_1"]
    assert offer.offer_cost == pytest.approx(6.0 * 0.30, abs=1e-3)  # cost x p_redeem
    assert offer.expected_net == pytest.approx(0.25 * value - 1.8 - 0.5, abs=1e-3)
    assert offer.uncertainty < 0.05  # very strong priors leave little spread
    assert next(iter(bids.values())) is offer  # best first


def test_uncertainty_scales_with_value_and_model_spread():
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW))
    b = bidder.bids(state(), ctx())[0]
    _, std = BetaResponseModel().predict(state(), "email|none|reminder")
    assert b.uncertainty == pytest.approx(b.value_if_converted * std)


def test_per_theme_false_keeps_best_theme_per_channel_offer():
    priors = BetaResponseModel.priors_from_rates({"treated/*/email|none|education": 0.4}, strength=100)
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW), BetaResponseModel(priors), per_theme=False)
    bids = bidder.bids(state(), ctx())
    none_bids = [b for b in bids if b.action.offer_id == "none"]
    assert [b.action.content_id for b in none_bids] == ["ci_2"]  # education beats reminder


def test_per_theme_false_picks_best_net_not_best_uplift():
    # The offer costs more than a sale is worth, so the theme that converts more loses more money.
    costly = OfferOption(offer_id="big", label="Big gift", kind=OfferKind.GIFT, cost_per_redemption=100.0)
    brief = sample_brief(NOW)
    brief = brief.model_copy(update={"envelope": brief.envelope.model_copy(update={"offers": (costly,)})})
    items = [
        sample_content(NOW)[0].model_copy(update={"content_id": cid, "offer_id": "big", "theme": theme})
        for cid, theme in (("hi_lift", Theme.URGENCY), ("lo_lift", Theme.QUALITY))
    ]
    priors = BetaResponseModel.priors_from_rates(
        {"treated/*/email|big|urgency": 0.5, "treated/*/email|big|quality": 0.1, "control/*": 0.04},
        strength=1e6,
    )
    model = BetaResponseModel(priors)
    both = GoalBidder(brief, items, model).bids(state(), ctx())
    assert both[0].action.content_id == "lo_lift"
    assert max(both, key=lambda b: b.uplift).action.content_id == "hi_lift"
    best = GoalBidder(brief, items, model, per_theme=False).bids(state(), ctx())
    assert [b.action.content_id for b in best] == ["lo_lift"]


def test_partial_cost_override_keeps_defaults_for_other_channels():
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW), costs={Channel.EMAIL: 0.5})
    s = state(channels={Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE})
    cost = {b.action.channel: b.channel_cost for b in bidder.bids(s, ctx())}
    assert cost[Channel.EMAIL] == 0.5
    assert cost[Channel.WHATSAPP] == DEFAULT_CHANNEL_COSTS[Channel.WHATSAPP]


def test_cart_recovery_urgency_decays_with_days_since_cart():
    brief = sample_brief(NOW).model_copy(update={"audience": aud()})
    bidder = GoalBidder(brief, sample_content(NOW), kind=GoalKind.CART_RECOVERY)
    fresh = bidder.bids(state(last_seen_at=NOW), ctx())[0]
    stale = bidder.bids(state(last_seen_at=NOW - timedelta(days=3)), ctx())[0]
    assert fresh.urgency == pytest.approx(1.0)
    assert stale.urgency == pytest.approx(0.125)
    assert stale.expected_net == pytest.approx(fresh.expected_net * 0.125)
    scored = state(as_of=NOW - timedelta(days=1), scores={"days_since_cart": 1.0})
    assert bidder.bids(scored, ctx())[0].urgency == pytest.approx(0.25)


def test_win_back_does_not_fade_and_custom_urgency_overrides():
    plain = GoalBidder(sample_brief(NOW), sample_content(NOW), kind=GoalKind.WIN_BACK)
    assert plain.bids(state(last_seen_at=NOW - timedelta(days=30)), ctx())[0].urgency == 1.0
    custom = GoalBidder(sample_brief(NOW), sample_content(NOW), urgency=Urgency(half_life_days=2, floor=0.3))
    assert custom.bids(state(last_seen_at=NOW - timedelta(days=30)), ctx())[0].urgency == pytest.approx(0.3)


def test_bids_are_deterministic():
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW))
    s = state(channels={Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE})
    assert bidder.bids(s, ctx()) == bidder.bids(s, ctx())


# --- learning ---------------------------------------------------------------------------------


def _decision(i: int, group: Group, content_id: str | None, goal_id: str = "goal_winback") -> Decision:
    action = (
        None
        if content_id is None
        else Action(
            goal_id=goal_id, channel=Channel.EMAIL, offer_id="none", content_id=content_id, send_at=NOW
        )
    )
    return Decision(
        decision_id=f"d{i}",
        customer_id=f"c{i}",
        made_at=NOW,
        kind=DecisionKind.NOTHING if action is None else DecisionKind.ACT,
        action=action,
        group=group,
        probability=1.0,
        reason="t",
        policy_version="t",
    )


def _reward(d: Decision, converted: bool) -> Reward:
    goal = d.action.goal_id if d.action is not None else None
    return Reward(
        decision_id=d.decision_id,
        customer_id=d.customer_id,
        goal_id=goal,
        window_end=NOW + timedelta(days=14),
        converted=converted,
        net=0.0,
    )


def test_learn_turns_rewards_into_uplift_through_the_policy():
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW))
    before, _ = bidder.response_model.predict(state(), "email|none|reminder")
    decisions: list[Decision] = []
    rewards: list[Reward] = []
    for i in range(400):
        if i % 2:  # treated with the reminder (ci_0): 40% buy
            d = _decision(i, Group.AGENT, "ci_0")
            rewards.append(_reward(d, i % 5 < 2))
        else:  # holdout: 10% buy
            d = _decision(i, Group.HOLDOUT, None)
            rewards.append(_reward(d, i % 20 == 0))
        decisions.append(d)
    by_id = {d.decision_id: d for d in decisions}
    AutonomousPolicy([bidder]).learn(rewards, by_id)
    after, _ = bidder.response_model.predict(state(), "email|none|reminder")
    assert before < 0.05 and after == pytest.approx(0.3, abs=0.05)


def test_learn_skips_other_goals_unknown_content_and_agent_do_nothing():
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW))
    before = bidder.response_model.predict(state(), "email|none|reminder")
    ds = [
        _decision(1, Group.AGENT, "ci_0", goal_id="other_goal"),
        _decision(2, Group.AGENT, "ci_unknown"),
        _decision(3, Group.AGENT, None),  # the agent's own do-nothing: not a fair control
    ]
    bidder.learn([_reward(d, True) for d in ds], {d.decision_id: d for d in ds})
    assert bidder.response_model.predict(state(), "email|none|reminder") == before


def test_learn_uses_the_segment_seen_at_bid_time_or_segment_of():
    model = BetaResponseModel(pool_strength=0.0)
    bidder = GoalBidder(sample_brief(NOW), sample_content(NOW), model)
    bidder.bids(state(customer_id="c1", segment="vip"), ctx())
    d = _decision(1, Group.AGENT, "ci_0")
    bidder.learn([_reward(d, True)] * 50, {"d1": d})
    assert model.treated_rate(state(segment="vip"), "email|none|reminder") > 0.5
    assert model.treated_rate(state(segment="new"), "email|none|reminder") < 0.1

    other = GoalBidder(
        sample_brief(NOW),
        sample_content(NOW),
        BetaResponseModel(pool_strength=0.0),
        segment_of=lambda _: "new",
    )
    other.learn([_reward(d, True)] * 50, {"d1": d})
    assert other.response_model.treated_rate(state(segment="new"), "email|none|reminder") > 0.5
