"""3-week plans: pricing of later messages, waiting, re-planning, caps and journeys.

A fixed bidder and one customer; no Gym. Each test runs in milliseconds.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import (
    Action,
    Bid,
    Channel,
    ChannelStats,
    CustomerState,
    DecisionContext,
    DecisionKind,
)
from autonomy.decide import rules
from autonomy.decide.frequency import (
    DEFAULT_PLANS,
    NOTHING,
    NOW_AND_10D,
    WAIT_14D,
    FrequencyPlanner,
    PlanConfig,
    PlanSpec,
)
from autonomy.decide.journeys import JourneyRunner, load_journeys
from autonomy.decide.policy import AutonomousPolicy
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.runtime.agent import ContactPrice

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
GOAL = "goal_winback"
REACHABLE = ChannelStats(address_known=True, consent=True)


def state(**kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": "c1",
        "as_of": NOW,
        "channels": {Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE},
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def ctx(now: datetime = NOW, seed: int = 0) -> DecisionContext:
    return DecisionContext(
        now=now, briefs=(sample_brief(NOW),), content=tuple(sample_content(NOW)), seed=seed
    )


def bid(
    channel: Channel = Channel.EMAIL,
    uplift: float = 0.05,
    *,
    value: float = 100.0,
    cost: float = 0.01,
    urgency: float = 1.0,
) -> Bid:
    return Bid(
        goal_id=GOAL,
        action=Action(goal_id=GOAL, channel=channel, content_id="ci_0", send_at=NOW),
        uplift=uplift,
        value_if_converted=value,
        channel_cost=cost,
        urgency=urgency,
        expected_net=urgency * (uplift * value - cost),
    )


class Linear:
    """A pricing with round numbers: no fatigue, and a hazard of 1% x (1 + count) of a 100 loss."""

    def __init__(self) -> None:
        self.calls: list[tuple[Channel, float]] = []

    def lost_value(self, state: CustomerState, bids: Sequence[Bid]) -> dict[Channel, float]:
        return {b.action.channel: 100.0 for b in bids}

    def price_at(self, state: CustomerState, channel: Channel, contacts: float, lost: float) -> float:
        self.calls.append((channel, contacts))
        return 0.01 * (1.0 + contacts) * lost

    def freshness(self, contacts: float) -> float:
        return 1.0


# --- the plans and what they are worth ---------------------------------------------------------------


def test_the_fixed_set_is_the_four_plans_of_the_decision() -> None:
    assert [(p.name, p.days) for p in DEFAULT_PLANS] == [
        ("nothing", ()),
        ("now", (0.0,)),
        ("now_and_10d", (0.0, 10.0)),
        ("wait_14d", (14.0,)),
    ]
    assert PlanConfig().horizon_days == 21.0


def test_a_later_message_is_priced_with_the_hazard_the_earlier_one_raises() -> None:
    pricing = Linear()
    cfg = PlanConfig(memory_days=10.0, weekly_discount=1.0)
    choice = FrequencyPlanner(pricing, cfg).choose(state(contacts_7d=0), [bid()])
    values = dict(choice.values)
    gross, fixed = 0.05 * 100.0, 0.01
    first = gross - fixed - 0.01 * (1.0 + 0.0) * 100.0  # priced as the auction prices it now
    raised = math.exp(-10.0 / 10.0)  # the first message, ten days on, still counts for e^-1 of a message
    second = gross - fixed - 0.01 * (1.0 + raised) * 100.0
    assert values["now"] == pytest.approx(first)
    assert values["now_and_10d"] == pytest.approx(first + second)
    assert values["wait_14d"] == pytest.approx(gross - fixed - 0.01 * 100.0)  # nothing before it: not raised
    assert values["nothing"] == 0.0
    # the second message really is dearer than a fresh one
    assert second < first
    assert any(c is Channel.EMAIL and n == pytest.approx(raised) for c, n in pricing.calls)


def test_messages_already_this_week_fade_into_the_planned_ones() -> None:
    pricing = Linear()
    cfg = PlanConfig(plans=(PlanSpec("later", (5.0,)),), memory_days=5.0, weekly_discount=1.0)
    FrequencyPlanner(pricing, cfg).choose(state(contacts_7d=2), [bid()])
    assert len(pricing.calls) == 1
    assert pricing.calls[0][1] == pytest.approx(2.0 * math.exp(-1.0))


def test_the_first_message_of_every_plan_is_priced_at_the_weeks_count_exactly() -> None:
    pricing = Linear()
    FrequencyPlanner(pricing).choose(state(contacts_7d=1), [bid()])
    now_calls = [c for c in pricing.calls if c[1] == 1.0]
    assert len(now_calls) == 2  # "now" and "now_and_10d" both open at the count the auction sees


def test_a_longer_memory_makes_the_second_message_dearer() -> None:
    def second_value(memory: float) -> float:
        cfg = PlanConfig(memory_days=memory, weekly_discount=1.0)
        values = dict(FrequencyPlanner(Linear(), cfg).choose(state(), [bid()]).values)
        return values["now_and_10d"] - values["now"]

    assert second_value(2.0) > second_value(10.0) > second_value(60.0)


def test_a_waiting_message_loses_a_little_to_the_discount() -> None:
    cheap = dict(FrequencyPlanner(Linear(), PlanConfig(weekly_discount=1.0)).choose(state(), [bid()]).values)
    worth_less = dict(
        FrequencyPlanner(Linear(), PlanConfig(weekly_discount=0.9)).choose(state(), [bid()]).values
    )
    assert worth_less["now"] == cheap["now"]
    assert worth_less["wait_14d"] == pytest.approx(cheap["wait_14d"] * 0.9**2)


def test_with_no_bids_the_plan_is_nothing() -> None:
    choice = FrequencyPlanner(Linear()).choose(state(), [])
    assert choice.best.name == NOTHING and not choice.wait and not choice.send_now


def test_ties_go_to_the_plan_listed_first_and_the_shipped_order_lists_sending_now_first() -> None:
    equal = PlanConfig(plans=(PlanSpec("wait", (14.0,)), PlanSpec("now", (0.0,))), weekly_discount=1.0)
    assert FrequencyPlanner(Linear(), equal).choose(state(), [bid()]).best.name == "wait"
    shipped = FrequencyPlanner(Linear(), PlanConfig(weekly_discount=1.0)).choose(state(), [bid()])
    assert shipped.best.name == NOW_AND_10D and shipped.send_now


def test_the_config_refuses_nonsense() -> None:
    with pytest.raises(ValueError):
        PlanConfig(plans=(PlanSpec("late", (30.0,)),))
    with pytest.raises(ValueError):
        PlanConfig(plans=(PlanSpec("backwards", (10.0, 0.0)),))
    with pytest.raises(ValueError):
        PlanConfig(weekly_discount=0.0)
    with pytest.raises(ValueError):
        PlanConfig(plans=(PlanSpec("a", ()), PlanSpec("a", (0.0,))))


# --- who waits ---------------------------------------------------------------------------------------


def priced() -> ContactPrice:
    """The agent's attention price with a hazard that learned a second message of a week is dearer
    (1.2% against 0.5% for the first)."""
    price = ContactPrice(lambda n: 0.7**n, lambda: 3)
    price.hazard.observe_send(Channel.EMAIL, 0, 3000.0)
    price.hazard.observe_event(Channel.EMAIL, 0, 15.0)
    price.hazard.observe_send(Channel.EMAIL, 1, 3000.0)
    price.hazard.observe_event(Channel.EMAIL, 1, 36.0)
    return price


def test_a_customer_with_a_free_week_is_planned_a_message_now() -> None:
    choice = FrequencyPlanner(priced()).choose(state(contacts_7d=0), [bid(uplift=0.03)])
    assert choice.send_now and choice.best.name == NOW_AND_10D


def test_a_customer_just_contacted_is_planned_to_wait_two_weeks() -> None:
    choice = FrequencyPlanner(priced()).choose(state(contacts_7d=1), [bid(uplift=0.03)])
    assert choice.wait and choice.best.name == WAIT_14D


class Bidder:
    """A fixed bid whose uplift and urgency jump for two days after a purchase (a cross-sell moment)."""

    goal_id = GOAL

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        bought = state.last_purchase_at is not None and state.last_purchase_at >= ctx.now - timedelta(days=2)
        b = bid(uplift=0.12 if bought else 0.03, urgency=2.0 if bought else 1.0)
        return [b.model_copy(update={"action": b.action.model_copy(update={"send_at": ctx.now})})]


def agent_group(price: ContactPrice | None = None) -> tuple[AutonomousPolicy, FrequencyPlanner]:
    price = price or priced()
    planner = FrequencyPlanner(price)
    policy = AutonomousPolicy(
        [Bidder()],
        price.floor,
        0.0,
        channel_price_fn=price.prices,
        plan_fn=planner.gate,
        mc_draws=50,
    )
    return policy, planner


def test_a_waiting_plan_leaves_the_customer_alone_and_says_why() -> None:
    policy, planner = agent_group()
    d = policy.decide(state(contacts_7d=1), ctx())
    assert d.kind is DecisionKind.NOTHING and d.probability == 1.0
    assert rules.FREQUENCY_PLAN in d.blocked_by
    assert (
        d.reason == "Left alone: waiting is worth more for them than a message today, by their 3-week plan."
    )
    when, name, value = planner.last["c1"]
    assert (when, name) == (NOW, WAIT_14D) and value > 0


def test_replanning_follows_a_purchase() -> None:
    policy, planner = agent_group()
    before = policy.decide(state(contacts_7d=1), ctx())
    assert before.kind is DecisionKind.NOTHING and planner.last["c1"][1] == WAIT_14D
    # the next day: the customer bought, the cross-sell moment is open, the plan is made again
    tomorrow = NOW + timedelta(days=1)
    bought = state(
        contacts_7d=1,
        as_of=tomorrow,
        purchases=1,
        revenue=60.0,
        last_purchase_at=tomorrow - timedelta(hours=3),
    )
    after = policy.decide(bought, ctx(tomorrow))
    assert after.kind is DecisionKind.ACT and rules.FREQUENCY_PLAN not in after.blocked_by
    assert planner.last["c1"][1] == NOW_AND_10D


def test_the_plan_is_made_again_every_day_from_the_state() -> None:
    policy, planner = agent_group()
    names = []
    for day in range(0, 9):
        now = NOW + timedelta(days=day)
        # one message went out on day 0; it leaves the 7-day count on day 7
        policy.decide(state(contacts_7d=1 if day < 7 else 0, as_of=now), ctx(now))
        names.append(planner.last["c1"][1])
    assert names[:7] == [WAIT_14D] * 7
    assert names[7] == NOW_AND_10D  # the week is clear again: send


def test_observe_false_changes_nothing() -> None:
    _, planner = agent_group()
    assert planner.gate(state(contacts_7d=1), [bid(uplift=0.03)], NOW, False) == rules.FREQUENCY_PLAN
    assert planner.last == {}


def test_the_plan_only_ever_holds_it_never_releases() -> None:
    policy, _ = agent_group()
    plain = AutonomousPolicy([Bidder()], priced().floor, 0.0, channel_price_fn=priced().prices, mc_draws=50)
    for contacts in (0, 1, 2):
        s = state(contacts_7d=contacts)
        with_plan, without = policy.decide(s, ctx()), plain.decide(s, ctx())
        if without.kind is DecisionKind.NOTHING:
            assert with_plan.kind is DecisionKind.NOTHING


# --- the hard caps stay a backstop -------------------------------------------------------------------


def test_the_weekly_cap_still_holds_whatever_the_plan_says() -> None:
    calls: list[int] = []

    def always_send(s: CustomerState, bids: Sequence[Bid], now: datetime, observe: bool) -> str | None:
        calls.append(len(bids))
        return None

    policy = AutonomousPolicy([Bidder()], None, 0.0, plan_fn=always_send, mc_draws=50)
    cap = sample_brief(NOW).envelope.max_contacts_per_week
    d = policy.decide(state(contacts_7d=cap), ctx())
    assert d.kind is DecisionKind.NOTHING and rules.WEEKLY_CAP in d.blocked_by
    assert calls == []  # the rules removed every bid before the plan was asked


def test_quiet_hours_and_consent_still_hold() -> None:
    policy, planner = agent_group()
    d = policy.decide(state(contacts_7d=0, channels={Channel.EMAIL: ChannelStats(address_known=True)}), ctx())
    assert d.kind is DecisionKind.NOTHING and rules.FREQUENCY_PLAN not in d.blocked_by
    assert "c1" not in planner.last


# --- a customer inside a journey follows the journey -------------------------------------------------


def test_a_customer_in_a_journey_follows_it_even_when_the_plan_would_wait() -> None:
    asked: list[int] = []

    def wait_after_one(s: CustomerState, bids: Sequence[Bid], now: datetime, observe: bool) -> str | None:
        asked.append(s.contacts_7d)
        return rules.FREQUENCY_PLAN if s.contacts_7d >= 1 else None

    full = load_journeys()
    only_b = full.model_copy(update={"journeys": tuple(j for j in full.journeys if j.journey_id == "B")})

    class Fixed:
        goal_id = GOAL

        def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
            return [
                b.model_copy(update={"action": b.action.model_copy(update={"send_at": ctx.now})})
                for b in (bid(Channel.EMAIL, 0.05, cost=0.001), bid(Channel.WHATSAPP, 0.06, cost=0.03))
            ]

    policy = AutonomousPolicy(
        [Fixed()], None, 0.0, journeys=JourneyRunner(only_b), plan_fn=wait_after_one, mc_draws=50
    )
    first = policy.decide(state(), ctx())
    assert first.kind is DecisionKind.ACT and first.journey_id == "B" and first.action is not None
    later = first.action.send_at + timedelta(days=3, hours=2)
    mid = state(contacts_7d=1, as_of=later, last_contact_at=first.action.send_at)
    second = policy.decide(mid, ctx(later))
    assert second.kind is DecisionKind.ACT and second.journey_id == "B"
    assert second.action is not None and second.action.channel is Channel.WHATSAPP
    assert rules.FREQUENCY_PLAN not in second.blocked_by
    assert asked == [0]  # the plan was asked about the journey's start, not about its later step
    # a customer outside any journey, in the same state, is held by the plan
    other = state(customer_id="c2", contacts_7d=1, as_of=later)
    held = policy.decide(other, ctx(later))
    assert held.kind is DecisionKind.NOTHING and rules.FREQUENCY_PLAN in held.blocked_by
