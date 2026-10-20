"""Contacting less: the hazard model, the attention price per channel, the weekly budget and the
lower-confidence-bound rule.

Pure maths or a fixed bidder and one customer; no Gym. Each test runs in milliseconds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import (
    Action,
    Bid,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    Group,
    StopMetric,
    stable_id,
)
from autonomy.decide import rules
from autonomy.decide.models import HAZARD_BASE_RATE, HazardModel
from autonomy.decide.pacing import Pacer
from autonomy.decide.policy import AutonomousPolicy, thompson_with_floor
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.gym.scenarios import default_brief, default_content
from autonomy.runtime.agent import (
    LIFT_EVIDENCE_CLICKS,
    RESTRAINT_Z,
    AgentPolicy,
    ClickResponseModel,
    ContactPrice,
    Rhythm,
    best_rhythm,
    build_agent_policy,
)

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
        now=now, briefs=(sample_brief(now),), content=tuple(sample_content(now)), seed=seed
    )


def bid(channel: Channel, net: float, *, uplift: float = 0.05, value: float = 100.0, sd: float = 0.0) -> Bid:
    """A bid whose gross is uplift x value, with the rest of `net` as its fixed cost."""
    gross = uplift * value
    return Bid(
        goal_id=GOAL,
        action=Action(goal_id=GOAL, channel=channel, content_id="ci_0", send_at=NOW),
        uplift=uplift,
        value_if_converted=value,
        channel_cost=max(gross - net, 0.0),
        expected_net=net,
        uncertainty=sd,
    )


class FixedBidder:
    goal_id = GOAL

    def __init__(self, bids: Sequence[Bid]) -> None:
        self._bids = list(bids)

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        return [
            b.model_copy(update={"action": b.action.model_copy(update={"send_at": ctx.now})})
            for b in self._bids
        ]


# --- the hazard model ----------------------------------------------------------------------------------


def feed(model: HazardModel, channel: Channel, contacts: int, sends: int, events: int) -> None:
    model.observe_send(channel, contacts, float(sends))
    model.observe_event(channel, contacts, float(events))


def test_hazard_starts_conservative_and_rises_with_the_weekly_count() -> None:
    model = HazardModel()
    assert model.rate(Channel.EMAIL, 0) == pytest.approx(HAZARD_BASE_RATE)
    rates = [model.rate(Channel.WHATSAPP, n) for n in range(4)]
    assert rates == sorted(rates) and rates[1] > rates[0]
    # Not "near zero": a business that measured nothing pays for half a percent.
    assert rates[0] >= 0.004


def test_hazard_learns_each_channel_apart_and_fast() -> None:
    model = HazardModel()
    feed(model, Channel.WHATSAPP, 0, 2_000, 60)  # 3% on WhatsApp
    feed(model, Channel.EMAIL, 0, 2_000, 2)  # 0.1% on email
    wa, em = model.rate(Channel.WHATSAPP, 0), model.rate(Channel.EMAIL, 0)
    assert wa == pytest.approx(0.03, rel=0.15)
    assert em == pytest.approx(0.001, rel=0.6)
    assert wa > 10 * em


def test_hazard_learns_the_count_shape_and_lends_it_to_the_other_channel() -> None:
    model = HazardModel()
    # WhatsApp: a second message of the week is five times as risky as the first.
    feed(model, Channel.WHATSAPP, 0, 3_000, 30)
    feed(model, Channel.WHATSAPP, 1, 3_000, 150)
    # Email has only ever sent first messages of the week.
    feed(model, Channel.EMAIL, 0, 3_000, 9)
    assert model.rate(Channel.WHATSAPP, 1) / model.rate(Channel.WHATSAPP, 0) == pytest.approx(5.0, rel=0.2)
    # The shape learned on WhatsApp carries over to email's untried second message.
    assert model.rate(Channel.EMAIL, 1) / model.rate(Channel.EMAIL, 0) > 3.0


def test_hazard_cell_with_few_sends_stays_with_the_levels_above() -> None:
    model = HazardModel()
    feed(model, Channel.EMAIL, 0, 5_000, 25)  # 0.5%
    feed(model, Channel.WHATSAPP, 0, 5_000, 25)
    before = model.rate(Channel.EMAIL, 2)  # an untried cell: where the levels above put it
    feed(model, Channel.EMAIL, 2, 10, 1)  # one unlucky unsubscribe in ten sends (10% raw)
    after = model.rate(Channel.EMAIL, 2)
    assert before < after < 2 * before  # it moves, but stays near the levels above
    assert after < 0.1 / 4
    # The well-measured first-message cell barely notices.
    assert model.rate(Channel.EMAIL, 0) == pytest.approx(0.005, rel=0.2)


def test_hazard_fit_does_not_depend_on_the_order_it_was_fed() -> None:
    data = [(Channel.WHATSAPP, 1, 700.0, 9.0), (Channel.EMAIL, 0, 3_000.0, 4.0), (Channel.SMS, 2, 50.0, 1.0)]
    a, b = HazardModel(), HazardModel()
    for channel, n, sends, events in data:
        feed(a, channel, n, int(sends), int(events))
    for channel, n, sends, events in reversed(data):
        feed(b, channel, n, int(sends), int(events))
    assert [a.rate(ch, n) for ch in Channel for n in range(4)] == [
        b.rate(ch, n) for ch in Channel for n in range(4)
    ]


def test_hazard_never_falls_with_more_contacts() -> None:
    model = HazardModel()
    feed(model, Channel.EMAIL, 0, 1_000, 40)
    feed(model, Channel.EMAIL, 1, 5_000, 0)  # plenty of safe second messages
    assert model.rate(Channel.EMAIL, 1) >= model.rate(Channel.EMAIL, 0)


def _event(kind: EventType, did: str, at: datetime, channel: Channel) -> Event:
    return Event(
        event_id=stable_id("ev", kind.value, did),
        customer_id="a1",
        type=kind,
        ts=at,
        source="test",
        channel=channel,
        decision_id=did,
    )


def test_agent_learns_the_hazard_from_every_send_as_its_outcome_arrives() -> None:
    agent = build_agent_policy([default_brief()], default_content())
    goal = default_brief().goal_id
    events: list[Event] = []
    decisions: list[Decision] = []
    for i, (channel, day) in enumerate(((Channel.WHATSAPP, 0), (Channel.WHATSAPP, 2), (Channel.EMAIL, 10))):
        at = NOW + timedelta(days=day)
        did = f"d{i}"
        content = f"gym_{channel.value}_none_reminder"
        decisions.append(
            Decision(
                decision_id=did,
                customer_id="a1",
                made_at=at,
                kind=DecisionKind.ACT,
                action=Action(goal_id=goal, channel=channel, content_id=content, send_at=at),
                probability=1.0,
                reason="test",
                policy_version="t",
            )
        )
        events += [_event(EventType.SEND, did, at, channel), _event(EventType.DELIVER, did, at, channel)]
    lost_at = NOW + timedelta(days=2, hours=3)
    # The second WhatsApp message (one other that week) lost the contact, with a complaint too.
    events += [
        _event(EventType.UNSUBSCRIBE, "d1", lost_at, Channel.WHATSAPP),
        _event(EventType.COMPLAINT, "d1", lost_at, Channel.WHATSAPP),
    ]
    agent._decided = True  # these are the agent's own sends, not history
    agent.nightly(decisions, {"a1": events}, NOW + timedelta(days=11))
    hazard = agent.contact_price.hazard
    assert hazard.counts(Channel.WHATSAPP, 0) == (1.0, 0.0)
    assert hazard.counts(Channel.WHATSAPP, 1) == (1.0, 1.0)  # one lost contact, not two events
    assert hazard.counts(Channel.EMAIL, 0) == (1.0, 0.0)  # eight days later: a fresh week
    assert agent.contact_price.complaints.counts(Channel.WHATSAPP, 1) == (1.0, 1.0)
    assert hazard.rate(Channel.WHATSAPP, 1) > HazardModel().rate(Channel.WHATSAPP, 1)
    # Handing the same log over again counts nothing twice.
    agent.nightly(decisions, {"a1": events}, NOW + timedelta(days=12))
    assert hazard.counts(Channel.WHATSAPP, 1) == (1.0, 1.0)


def _past(kind: EventType, cid: str, at: datetime, channel: Channel, n: int, did: str | None = None) -> Event:
    return Event(
        event_id=stable_id("hist", kind.value, cid, str(n)),
        customer_id=cid,
        type=kind,
        ts=at,
        source="history",
        channel=channel,
        decision_id=did,
        attrs={"offer_id": "none", "theme": "reminder"},
    )


def test_past_campaigns_warm_start_the_hazard_before_day_one() -> None:
    """A weekly WhatsApp campaign to 200 people for 8 weeks, about 3% of messages followed by an
    opt-out a few hours later (no decision ids in this CRM export): day 1 already knows WhatsApp is
    risky, and every opt-out is put down to the message before it."""
    start = NOW - timedelta(days=60)
    history: dict[str, list[Event]] = {}
    n = 0
    for c in range(200):
        cid = f"h{c}"
        for week in range(8):
            at = start + timedelta(days=7 * week, hours=c % 5)
            n += 1
            evs = [
                _past(EventType.SEND, cid, at, Channel.WHATSAPP, n),
                _past(EventType.DELIVER, cid, at, Channel.WHATSAPP, n),
            ]
            if (c * 8 + week) % 50 < 3:
                evs.append(_past(EventType.UNSUBSCRIBE, cid, at + timedelta(hours=3), Channel.WHATSAPP, n))
            history.setdefault(cid, []).extend(evs)
            if (c * 8 + week) % 50 < 3:
                break  # opted out: no more WhatsApp messages
    agent = build_agent_policy([default_brief()], default_content())
    agent.nightly([], history, NOW)
    agent.prepare_day(
        [], DecisionContext(now=NOW, briefs=(default_brief(),), content=tuple(default_content()))
    )
    sends, events = agent.contact_price.hazard.counts(Channel.WHATSAPP, 0)
    opt_outs = sum(e.type is EventType.UNSUBSCRIBE for evs in history.values() for e in evs)
    assert sends == sum(e.type is EventType.DELIVER for evs in history.values() for e in evs)
    assert events == opt_outs and events / sends > 0.025
    assert agent.contact_price.hazard.rate(Channel.WHATSAPP, 0) > 5 * HazardModel().rate(Channel.WHATSAPP, 0)
    assert agent.contact_price.hazard.counts(Channel.EMAIL, 0) == (0.0, 0.0)


def _campaigns(complaint_every: int, weeks: int = 8, people: int = 200) -> dict[str, list[Event]]:
    """Weekly email campaigns; every `complaint_every`-th message drew an opt-out and a complaint."""
    start = NOW - timedelta(days=7 * weeks + 1)
    history: dict[str, list[Event]] = {}
    n = 0
    for c in range(people):
        cid = f"h{c}"
        for week in range(weeks):
            at = start + timedelta(days=7 * week, hours=c % 5)
            n += 1
            evs = [
                _past(EventType.SEND, cid, at, Channel.EMAIL, n),
                _past(EventType.DELIVER, cid, at, Channel.EMAIL, n),
            ]
            if n % complaint_every == 0:
                later = at + timedelta(hours=3)
                evs += [
                    _past(EventType.UNSUBSCRIBE, cid, later, Channel.EMAIL, n),
                    _past(EventType.COMPLAINT, cid, later, Channel.EMAIL, n),
                ]
            history.setdefault(cid, []).extend(evs)
    return history


def test_a_history_far_above_the_complaint_limit_pauses_the_channel_before_day_one() -> None:
    ctx0 = DecisionContext(now=NOW, briefs=(default_brief(),), content=tuple(default_content()))
    storm = build_agent_policy([default_brief()], default_content())
    storm.nightly([], _campaigns(complaint_every=100), NOW)  # 1% complaints against a 0.08% limit
    storm.prepare_day([], ctx0)
    assert (default_brief().goal_id, Channel.EMAIL) in storm.breaker.paused_channels
    assert (default_brief().goal_id, Channel.WHATSAPP) not in storm.breaker.paused_channels
    assert storm.alerts and storm.alerts[0].startswith("Paused email before the first message")
    email_only = state(customer_id="e1", channels={Channel.EMAIL: REACHABLE})
    assert storm.decide(email_only, ctx0).kind is DecisionKind.NOTHING
    calm = build_agent_policy([default_brief()], default_content())
    calm.nightly([], _campaigns(complaint_every=2_000), NOW)  # 0.05%: within the limit
    calm.prepare_day([], ctx0)
    assert not calm.breaker.paused_channels and not calm.alerts


# --- the attention price and the weekly budget ------------------------------------------------------


def price_model(
    fatigue: Sequence[float] = (1.0, 0.4, 0.3), cap: int = 2, second: tuple[int, int] = (150, 60)
) -> ContactPrice:
    """Learned hazards: 0.3% (WhatsApp) and 0.2% (email) for a first message of the week; a
    second message's events per 10,000 sends are `second`."""
    hazard = HazardModel()
    feed(hazard, Channel.WHATSAPP, 0, 10_000, 30)
    feed(hazard, Channel.WHATSAPP, 1, 10_000, second[0])
    feed(hazard, Channel.EMAIL, 0, 10_000, 20)
    feed(hazard, Channel.EMAIL, 1, 10_000, second[1])
    return ContactPrice(lambda n: fatigue[min(n, len(fatigue) - 1)], lambda: cap, hazard=hazard)


def test_price_is_hazard_times_lost_future_value_per_channel() -> None:
    price = price_model()
    wa, em = bid(Channel.WHATSAPP, 1.97, uplift=0.02), bid(Channel.EMAIL, 0.5, uplift=0.005)
    s = state()
    lost = price.lost_value(s, [wa, em])
    # Before prices, WhatsApp's best rhythm is a burst of two every 8 days (one fresh message worth
    # 1.97 and one at 40%): (1.97 + 0.77) x 7 / 8 a week, for half a year.
    assert lost[Channel.WHATSAPP] == pytest.approx(price.weeks * (1.97 + 0.77) * 7 / 8, rel=1e-6)
    prices = price.prices(s, [wa, em])
    expected = (
        price.hazard.rate(Channel.WHATSAPP, 0) * lost[Channel.WHATSAPP]
        + price.complaints.rate(Channel.WHATSAPP, 0) * price.complaint_cost
    )
    assert prices[Channel.WHATSAPP] == pytest.approx(expected)
    # The dearer future is the one at stake: WhatsApp's message costs more than email's.
    assert prices[Channel.WHATSAPP] > prices[Channel.EMAIL]
    # A customer messages do not move has little to lose.
    idle = price.prices(s, [bid(Channel.WHATSAPP, -0.03, uplift=0.0)])
    assert idle[Channel.WHATSAPP] == pytest.approx(
        price.complaints.rate(Channel.WHATSAPP, 0) * price.complaint_cost
    )


def test_price_rises_with_the_count_this_week() -> None:
    price = price_model()
    bids = [bid(Channel.WHATSAPP, 1.97, uplift=0.02)]
    fresh = price.prices(state(contacts_7d=0), bids)[Channel.WHATSAPP]
    second = price.prices(state(contacts_7d=1), bids)[Channel.WHATSAPP]
    assert second > fresh


def test_budget_is_one_when_two_tired_messages_are_worth_less_than_one_fresh() -> None:
    harsh = price_model(fatigue=(1.0, 0.4, 0.3))
    mild = price_model(fatigue=(1.0, 0.95, 0.9), second=(30, 20))  # barely tired, no riskier
    bids = [bid(Channel.WHATSAPP, 1.97, uplift=0.02), bid(Channel.EMAIL, 0.7, uplift=0.007)]
    assert harsh.budget(state(), bids) == 1
    assert mild.budget(state(), bids) == 2
    # Never below one: whether the first message of a week goes is the auction's call.
    assert harsh.budget(state(), [bid(Channel.EMAIL, -1.0, uplift=0.0)]) == 1
    assert price_model(cap=1).budget(state(), bids) == 1


def test_rhythms_value_a_burst_of_two_against_one_or_two_spread_out() -> None:
    assert best_rhythm([1.0, 0.1]) == Rhythm(1, False, 1.0)  # a tired second message is not worth it
    burst = best_rhythm([1.0, 0.5])
    assert burst.burst and burst.size == 2
    assert burst.weekly_value == pytest.approx(1.5 * 7 / 8)  # one fresh + one tired every 8 days
    steady = best_rhythm([1.0, 0.95])
    assert not steady.burst and steady.size == 2  # barely tired: two a week, spread out
    assert best_rhythm([-1.0, -2.0]).size == 1
    # A burst's second message goes only right after its first; a steady rhythm's at any time.
    assert burst.allows(0, None) and burst.allows(1, timedelta(hours=20))
    assert not burst.allows(1, timedelta(days=5)) and not burst.allows(2, timedelta(hours=20))
    assert steady.allows(1, timedelta(days=5)) and not steady.allows(2, None)


def test_budget_follows_the_rhythm_through_a_burst() -> None:
    price = price_model(fatigue=(1.0, 0.6, 0.5), second=(30, 20))  # a second message: 60%, no riskier
    bids = [bid(Channel.WHATSAPP, 1.97, uplift=0.02)]
    assert price.rhythm(state(), bids).burst
    yesterday = NOW - timedelta(hours=22)
    mid = state(contacts_7d=1, last_contact_at=yesterday, recent_contacts=(yesterday,))
    assert price.budget(mid, bids) == 2  # the burst's second message may go
    old = NOW - timedelta(days=5)
    rest = state(contacts_7d=1, last_contact_at=old, recent_contacts=(old,))
    assert price.budget(rest, bids) == 1  # resting until the week is clear: held


def test_policy_spends_the_budget_on_the_best_option_and_then_holds() -> None:
    price = price_model(fatigue=(1.0, 0.4, 0.3))
    bids = [bid(Channel.WHATSAPP, 1.97, uplift=0.02), bid(Channel.EMAIL, 0.7, uplift=0.007)]
    policy = AutonomousPolicy(
        [FixedBidder(bids)],
        exploration_floor=0.0,
        channel_price_fn=price.prices,
        budget_fn=price.budget,
    )
    first = policy.decide(state(), ctx())
    assert first.action is not None and first.action.channel is Channel.WHATSAPP
    # The cheap email would fit under the brief's cap of 2, but it would tire next week's WhatsApp
    # message: with the budget spent, the customer is held.
    held = policy.decide(state(customer_id="c2", contacts_7d=1), ctx())
    assert held.kind is DecisionKind.NOTHING
    assert rules.WEEKLY_CAP in held.blocked_by
    assert held.reason.startswith("Left alone:")
    # Without a budget the same customer gets the second message up to the cap.
    loose = AutonomousPolicy([FixedBidder(bids)], exploration_floor=0.0, channel_price_fn=price.prices)
    assert loose.decide(state(customer_id="c2", contacts_7d=1), ctx()).kind is DecisionKind.ACT


def test_sent_decision_logs_its_own_channels_price() -> None:
    price = price_model()
    bids = [bid(Channel.WHATSAPP, 1.97, uplift=0.02), bid(Channel.EMAIL, 0.5, uplift=0.005)]
    policy = AutonomousPolicy([FixedBidder(bids)], exploration_floor=0.0, channel_price_fn=price.prices)
    d = policy.decide(state(), ctx())
    assert d.action is not None
    assert d.attention_price == pytest.approx(price.prices(state(), bids)[d.action.channel])


# --- restraint: the lower-confidence-bound rule ------------------------------------------------------


def test_thompson_mask_keeps_the_floor_and_logs_exact_probabilities() -> None:
    means, sds = [0.0, 1.0, 0.5], [0.0, 2.0, 0.1]
    floor = 0.1
    counts = np.zeros(3)
    for i in range(400):
        pick = thompson_with_floor(
            means, sds, floor, np.random.default_rng(i), 200, eligible=[True, False, True]
        )
        counts[pick.index] += 1
        # The held-back option is picked only by the floor, and logs exactly floor / n.
        assert pick.probabilities[1] == pytest.approx(floor / 3)
        assert sum(pick.probabilities) == pytest.approx(1.0)
        if pick.index == 1:
            assert pick.explored
    assert 0 < counts[1] < 0.1 * 400  # tried now and then, never by sampling
    # All eligible is exactly the unmasked choice.
    a = thompson_with_floor(means, sds, floor, np.random.default_rng(7), 200)
    b = thompson_with_floor(means, sds, floor, np.random.default_rng(7), 200, eligible=[True, True, True])
    assert a == b


def test_policy_sends_an_uncertain_option_only_when_its_lower_bound_beats_nothing() -> None:
    risky = bid(Channel.WHATSAPP, 1.0, sd=2.0)  # positive on average, unproven
    sure = bid(Channel.EMAIL, 0.6, sd=0.1)  # smaller, but clear of zero

    def margin(s: CustomerState, b: Bid) -> float:
        return RESTRAINT_Z * b.uncertainty

    held = AutonomousPolicy([FixedBidder([risky])], exploration_floor=0.0, margin_fn=margin)
    sampled = AutonomousPolicy([FixedBidder([risky])], exploration_floor=0.0)
    held_sends = sum(
        held.decide(state(customer_id=f"c{i}"), ctx()).kind is DecisionKind.ACT for i in range(50)
    )
    free_sends = sum(
        sampled.decide(state(customer_id=f"c{i}"), ctx()).kind is DecisionKind.ACT for i in range(50)
    )
    assert held_sends == 0 and free_sends > 25
    both = AutonomousPolicy([FixedBidder([risky, sure])], exploration_floor=0.0, margin_fn=margin)
    for i in range(20):
        d = both.decide(state(customer_id=f"c{i}"), ctx())
        assert d.action is not None and d.action.channel is Channel.EMAIL
    # The floor still tries the held-back option, and its logged probability says so.
    floored = AutonomousPolicy([FixedBidder([risky])], exploration_floor=0.5, margin_fn=margin)
    tried = [floored.decide(state(customer_id=f"c{i}"), ctx()) for i in range(60)]
    acts = [d for d in tried if d.kind is DecisionKind.ACT]
    assert acts and all(d.probability == pytest.approx(0.25) for d in acts)


def agent_with_lift(
    k: float, sd: float, *, days: int, clicks: float
) -> tuple[AgentPolicy, ClickResponseModel]:
    agent = build_agent_policy([default_brief()], default_content())
    model = agent.bundle.response
    assert isinstance(model, ClickResponseModel)
    start = NOW - timedelta(days=days)
    agent.lift.member("h", Group.HOLDOUT, start)
    agent.lift.click_times.extend([start] * int(clicks))
    model.lift = (k, sd)
    model.lift_by_segment = {"lapsed": (k, sd), "heavy": (3 * sd, sd)}
    agent._judge_lift(NOW)
    return agent, model


def test_lift_is_judged_weak_only_when_the_comparison_has_had_its_say() -> None:
    agent, _ = agent_with_lift(0.1, 0.2, days=21, clicks=100)
    assert agent._weak_lift["lapsed"] and agent._weak_lift["*"]
    assert not agent._weak_lift["heavy"]  # this segment's lift is clear of zero
    # A noisy segment in a shop whose lift is proven is not held back on its own evidence.
    shop, model = agent_with_lift(0.3, 0.05, days=21, clicks=500)
    model.lift_by_segment = {"lapsed": (0.1, 0.2)}
    shop._judge_lift(NOW)
    assert shop._weak_lift == {"*": False, "lapsed": False}
    early, _ = agent_with_lift(0.1, 0.2, days=7, clicks=100)
    assert early._weak_lift == {}  # the launch test is still running: the prior rules
    thin, _ = agent_with_lift(0.1, 0.2, days=21, clicks=LIFT_EVIDENCE_CLICKS - 5)
    assert thin._weak_lift == {}
    proven, _ = agent_with_lift(0.3, 0.05, days=21, clicks=500)
    assert not any(proven._weak_lift.values())


def test_agent_margin_is_a_lower_bound_on_the_option_and_zero_when_proven() -> None:
    agent, _ = agent_with_lift(0.1, 0.2, days=21, clicks=100)
    clear = bid(Channel.WHATSAPP, 1.0, sd=0.3)  # pays even 1.64 sd down
    marginal = bid(Channel.EMAIL, 0.3, sd=0.3)  # positive on average, not at its lower bound
    weak = state(segment="lapsed")
    assert agent._margin(weak, clear) == pytest.approx(RESTRAINT_Z * 0.3)
    assert clear.expected_net > agent._margin(weak, clear)
    assert marginal.expected_net < agent._margin(weak, marginal)
    assert agent._margin(state(segment="heavy"), marginal) == 0.0  # its own lift is clear of zero
    proven, _ = agent_with_lift(0.3, 0.05, days=21, clicks=500)
    assert proven._margin(weak, marginal) == 0.0


def test_switches_turn_each_part_off() -> None:
    agent = build_agent_policy(
        [default_brief()],
        default_content(),
        pacing=False,
        reading_hour_model=False,
        restraint=False,
        weekly_budget=False,
    )
    assert agent.pacer is None and agent.hour_model is None
    assert agent.policy.margin_fn is None and agent.policy.budget_fn is None
    on = build_agent_policy([default_brief()], default_content())
    assert on.hour_model is not None and on.policy.margin_fn is not None and on.policy.budget_fn is not None
    assert on.pacer is None  # off by default: see docs/v2/CONTACT_LESS.md
    paced = build_agent_policy([default_brief()], default_content(), pacing=True)
    assert paced.pacer is not None and paced.policy.pacer is paced.pacer


def test_pacer_lets_a_goal_clearly_over_the_limit_reach_the_breaker() -> None:
    """Near the limit the pacer holds the channel; plainly over it, it steps aside so the breaker
    can stop the goal instead of the sending trickling on as each week rolls by."""
    envelope = default_brief().envelope
    goal = default_brief().goal_id
    near = Pacer(step_aside=True)
    for i in range(3_000):
        near.record(goal, Channel.EMAIL, EventType.DELIVER, NOW - timedelta(hours=i % 100))
    for i in range(4):
        near.record(goal, Channel.EMAIL, EventType.COMPLAINT, NOW - timedelta(hours=i))
    assert not near.in_breach(goal, StopMetric.COMPLAINT_RATE, 0.0008, NOW)
    assert near.pressure(goal, Channel.EMAIL, envelope, NOW) > 0.0  # 0.13% on 3,000: near 0.08%
    over = Pacer(step_aside=True)
    for i in range(300):
        over.record(goal, Channel.EMAIL, EventType.DELIVER, NOW - timedelta(hours=i % 100))
    for i in range(4):
        over.record(goal, Channel.EMAIL, EventType.COMPLAINT, NOW - timedelta(hours=i))
    assert over.in_breach(goal, StopMetric.COMPLAINT_RATE, 0.0008, NOW)  # 1.3% against 0.08%
    assert over.pressure(goal, Channel.EMAIL, envelope, NOW) == 0.0
    assert Pacer().pressure(goal, Channel.EMAIL, envelope, NOW) == 0.0  # (no outcomes)
    legacy = Pacer()  # the default keeps holding just short of the trip point
    for i in range(300):
        legacy.record(goal, Channel.EMAIL, EventType.DELIVER, NOW - timedelta(hours=i % 100))
    for i in range(4):
        legacy.record(goal, Channel.EMAIL, EventType.COMPLAINT, NOW - timedelta(hours=i))
    assert legacy.pressure(goal, Channel.EMAIL, envelope, NOW) == 1.0
    assert build_agent_policy([default_brief()], default_content(), pacing=True).policy.pacer is not None
