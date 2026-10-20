"""The agent in the Gym: it runs, repeats, keeps the hard rules, and its nightly hooks do their job.

These pin the WF7 fixes in `decide` (pending sends count towards the weekly cap, snoozes, offers
that need approval, "no discounts", one decision id per trigger) and the agent's own pieces (brief
refreshes, the breaker, the click model). Gym runs here are tiny; CI runs the real scoreboard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    OfferKind,
    OfferOption,
    stable_id,
)
from autonomy.decide import rules
from autonomy.decide.policy import AutonomousPolicy
from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym.run import POLICIES, agent_factory
from autonomy.gym.score import run_policy
from autonomy.runtime.agent import AgentPolicy, Breaker, ClickResponseModel, LiftPerClick, build_agent_policy

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a Monday, 10:00 UTC
GOAL = default_brief().goal_id
REACHABLE = ChannelStats(address_known=True, consent=True)


def state(**kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": "c1",
        "as_of": NOW,
        "channels": {Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE},
        "purchases": 2,
        "revenue": 120.0,
        "margin": 48.0,
        "first_seen_at": NOW - timedelta(days=200),
        "last_purchase_at": NOW - timedelta(days=20),
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def ctx(now: datetime = NOW, seed: int = 0, **kw: object) -> DecisionContext:
    return DecisionContext(
        now=now, briefs=(default_brief(),), content=tuple(default_content()), seed=seed, **kw  # type: ignore[arg-type]
    )


class OneBid:
    """A bidder with one certain, clearly worthwhile bid per channel x offer it is given."""

    goal_id = GOAL

    def __init__(self, offers: tuple[str, ...] = ("none",), net: float = 5.0) -> None:
        self.offers = offers
        self.net = net

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        out = []
        for offer in self.offers:
            content = (
                f"gym_email_{offer}_"
                + {"none": "reminder", "freeship": "convenience", "pct10": "savings"}[offer]
            )
            out.append(
                Bid(
                    goal_id=GOAL,
                    action=Action(
                        goal_id=GOAL,
                        channel=Channel.EMAIL,
                        offer_id=offer,
                        content_id=content,
                        send_at=ctx.now,
                    ),
                    uplift=0.1,
                    value_if_converted=50.0,
                    expected_net=self.net + (1.0 if offer != "none" else 0.0),
                )
            )
        return out


# --- the agent in a tiny Gym run -----------------------------------------------------------------


def test_agent_is_registered_and_runs_deterministically_in_a_tiny_gym() -> None:
    assert POLICIES["agent"] is agent_factory
    spec = get_scenario("smoke", n_customers=120, days=7, history_days=30)
    first = run_policy(spec, agent_factory, 0, with_oracle=False)
    again = run_policy(spec, agent_factory, 0, with_oracle=False)
    assert first.violations == 0
    assert first.actions > 0
    assert (first.true_value, first.actions, first.contacts) == (
        again.true_value,
        again.actions,
        again.contacts,
    )
    assert first.incremental_profit == again.incremental_profit


def test_nightly_hook_gets_history_holdout_and_sends() -> None:
    spec = get_scenario("smoke", n_customers=120, days=5, history_days=30)
    seen: dict[str, AgentPolicy] = {}

    def factory(world: object) -> AgentPolicy:
        agent = build_agent_policy([default_brief()], default_content())
        seen["agent"] = agent
        return agent

    run_policy(spec, factory, 1, with_oracle=False)
    agent = seen["agent"]
    assert agent.segmenter.data_start is not None  # the history came in on the first night
    groups = set(agent.lift.group.values())
    assert groups == {Group.AGENT, Group.HOLDOUT}  # the holdout was decided for (and left alone)
    model = agent.bundle.response
    assert isinstance(model, ClickResponseModel)
    assert any(sends > 0 for _clicks, sends in model._clicks.values())


# --- the weekly cap counts scheduled sends -------------------------------------------------------


def test_pending_sends_count_towards_the_weekly_cap() -> None:
    policy = AutonomousPolicy([OneBid()], None, 0.0)
    evening = tuple(1.0 if h == 20 else 0.0 for h in range(24))  # the send goes out at 20:00
    s = state(active_hours=evening, contacts_7d=1, recent_contacts=(NOW - timedelta(days=2),))
    first = policy.decide(s, ctx())
    assert first.kind is DecisionKind.ACT and first.action is not None
    assert first.action.send_at.hour == 20
    # An event-triggered decision before 20:00: the state still shows one contact, but one is pending.
    later = policy.decide(s, ctx(NOW + timedelta(hours=3)), trigger="cart:e1")
    assert later.kind is DecisionKind.NOTHING
    assert later.blocked_by == (rules.WEEKLY_CAP,)
    # Deciding again for the same moment and trigger replaces the decision, it does not add a send.
    assert policy.decide(s, ctx()).decision_id == first.decision_id
    assert policy.pending_sends(s, NOW) == 1
    # Once the SEND is folded into the state, it is no longer pending (no double count).
    sent = s.model_copy(update={"contacts_7d": 2, "last_contact_at": first.action.send_at})
    assert policy.pending_sends(sent, NOW + timedelta(hours=11)) == 0
    # A week on, the scheduled send has aged out even if it never showed up.
    assert policy.pending_sends(s, first.action.send_at + timedelta(days=7, minutes=1)) == 0


def test_cap_never_exceeded_with_many_triggers_in_a_day() -> None:
    policy = AutonomousPolicy([OneBid()], None, 0.0)
    evening = tuple(1.0 if h == 20 else 0.0 for h in range(24))
    s = state(active_hours=evening)
    acts = [policy.decide(s, ctx(NOW + timedelta(minutes=10 * i)), trigger=f"visit:{i}") for i in range(12)]
    assert sum(d.kind is DecisionKind.ACT for d in acts) == default_brief().envelope.max_contacts_per_week
    assert len({d.decision_id for d in acts}) == len(acts)


def test_decision_ids_are_unique_per_customer_moment_and_trigger() -> None:
    policy = AutonomousPolicy([OneBid()], None, 0.0)
    s = state()
    sweep = policy.decide(s, ctx())
    cart = policy.decide(s, ctx(), trigger="cart:e9")
    reply = policy.decide(s, ctx(), trigger="reply:e10")
    assert len({sweep.decision_id, cart.decision_id, reply.decision_id}) == 3
    assert policy.decide(s, ctx(), trigger="cart:e9").decision_id == cart.decision_id
    assert policy.decision_id("c1", NOW) == sweep.decision_id
    assert policy.decision_id("c1", NOW, "sweep") == sweep.decision_id


# --- stated preferences and offers that need a person ---------------------------------------------


def test_snooze_is_a_hard_rule() -> None:
    policy = AutonomousPolicy([OneBid()], None, 0.0)
    snoozed = state(preferences={"snooze_until": (NOW + timedelta(days=3)).isoformat()})
    d = policy.decide(snoozed, ctx())
    assert d.kind is DecisionKind.NOTHING and d.blocked_by == (rules.SNOOZED,)
    # after the snooze ends, the customer can be contacted again
    after = policy.decide(snoozed, ctx(NOW + timedelta(days=3, hours=1)))
    assert after.kind is DecisionKind.ACT
    # a snooze without a zone is on the customer's clock; one that cannot be read blocks
    local = state(timezone="Asia/Tokyo", preferences={"snooze_until": "2026-03-02T23:30:00"})
    assert rules.snooze_until(local) == datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    broken = state(preferences={"snooze_until": "next tuesday"})
    assert policy.decide(broken, ctx()).blocked_by == (rules.SNOOZED,)
    assert rules.snooze_until(state(preferences={"snooze_until": ""})) is None


def test_no_discounts_is_honoured_by_the_rules_and_the_agent() -> None:
    policy = AutonomousPolicy([OneBid(("pct10", "freeship", "none"))], None, 0.0)
    keen = policy.decide(state(customer_id="keen"), ctx())
    assert keen.action is not None and keen.action.offer_id != "none"  # offers bid higher
    picky = state(customer_id="picky", preferences={"no_discounts": "True"})
    d = policy.decide(picky, ctx())
    assert d.action is not None and d.action.offer_id == "none"
    assert d.blocked_by == (rules.NO_DISCOUNTS,)
    # and through the whole agent, over many draws, no discount ever reaches them
    agent = build_agent_policy([default_brief()], default_content(), exploration_floor=0.5)
    for i in range(60):
        out = agent.decide(picky.model_copy(update={"customer_id": f"p{i}"}), ctx(seed=i))
        assert out.action is None or out.action.offer_id == "none"


def test_offers_that_need_approval_are_never_sent_by_the_agent() -> None:
    brief = default_brief()
    gated = tuple(
        o.model_copy(update={"needs_approval": True}) if o.kind is OfferKind.PERCENT_OFF else o
        for o in brief.envelope.offers
    )
    gated_brief = brief.model_copy(update={"envelope": brief.envelope.model_copy(update={"offers": gated})})
    c = ctx().model_copy(update={"briefs": (gated_brief,)})
    policy = AutonomousPolicy([OneBid(("pct10", "none"))], None, 0.0)
    d = policy.decide(state(), c)
    assert d.action is not None and d.action.offer_id == "none"
    assert rules.OFFER_NEEDS_APPROVAL in d.blocked_by
    gift = OfferOption(offer_id="gift", label="A gift", kind=OfferKind.GIFT)
    env = gated_brief.envelope.model_copy(update={"offers": (*gated, gift)})
    picky = state(preferences={"no_discounts": "yes"})
    bid = OneBid(("none",)).bids(picky, c)[0]
    gift_bid = bid.model_copy(update={"action": bid.action.model_copy(update={"offer_id": "gift"})})
    assert rules.no_discounts(gift_bid, picky, env).allowed  # a gift is not a discount


# --- the agent's own pieces ----------------------------------------------------------------------


def test_bidders_refresh_when_a_newer_brief_arrives() -> None:
    agent = build_agent_policy([default_brief()], default_content())
    old = agent.bidders[GOAL]
    agent.decide(state(), ctx())
    assert agent.bidders[GOAL] is old
    brief = default_brief()
    newer = brief.model_copy(
        update={
            "version": 2,
            "brief_id": brief.brief_id + "_v2",
            "envelope": brief.envelope.model_copy(update={"channels": (Channel.EMAIL,)}),
        }
    )
    d = agent.decide(state(customer_id="c2"), ctx().model_copy(update={"briefs": (newer,)}))
    assert agent.bidders[GOAL] is not old
    assert agent.bidders[GOAL].brief.version == 2
    assert d.bids  # otherwise the next line would hold for nothing
    assert all(b.action.channel is Channel.EMAIL for b in d.bids)


def test_a_budget_that_runs_out_during_the_day_is_seen_at_once() -> None:
    agent = build_agent_policy([default_brief()], default_content())
    first = agent.decide(state(customer_id="c1"), ctx(budget_left={GOAL: 1_000.0}))
    assert first.bids and rules.NO_BUDGET not in first.blocked_by
    # Same moment, same briefs, but the goal's money is gone: no cached context may hide that.
    broke = agent.decide(state(customer_id="c2"), ctx(budget_left={GOAL: 0.0}))
    assert broke.action is None and broke.blocked_by == (rules.NO_BUDGET,)


def test_nightly_never_counts_an_event_twice_even_given_the_whole_log() -> None:
    agent = build_agent_policy([default_brief()], default_content())
    agent.lift.member("a1", Group.AGENT, NOW)
    bought = Event(
        event_id="buy1",
        customer_id="a1",
        type=EventType.PURCHASE,
        ts=NOW + timedelta(days=1),
        source="test",
        value=40.0,
        margin=16.0,
    )
    log = {"a1": [bought]}
    for day in (2, 40, 41, 60):  # the event is pruned from memory after the second call
        agent.nightly([], log, NOW + timedelta(days=day))
        assert agent.lift.purchases == {"a1": 1}


def test_a_send_that_never_went_out_does_not_hold_the_customer_for_a_week() -> None:
    policy = AutonomousPolicy([OneBid()], None, 0.0, min_gap=timedelta(hours=6))
    evening = tuple(1.0 if h == 20 else 0.0 for h in range(24))
    s = state(active_hours=evening)
    first = policy.decide(s, ctx())
    assert first.action is not None and first.action.send_at.hour == 20
    # Next morning the state shows no contact: the send was dropped (an opt-out came first, say).
    # It still counts towards the cap for a week, but it no longer blocks as "too soon".
    nxt = policy.decide(s, ctx(NOW + timedelta(days=1)))
    assert nxt.kind is DecisionKind.ACT
    assert policy.pending_sends(s, NOW + timedelta(days=1)) == 2


def _outbound(kind: EventType, did: str, at: datetime, cid: str = "c1") -> Event:
    return Event(
        event_id=stable_id("ev", kind.value, did),
        customer_id=cid,
        type=kind,
        ts=at,
        source="test",
        channel=Channel.EMAIL,
        decision_id=did,
    )


def _send(did: str, cid: str, at: datetime) -> Decision:
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.ACT,
        action=Action(goal_id=GOAL, channel=Channel.EMAIL, content_id="gym_email_none_reminder", send_at=at),
        probability=1.0,
        reason="test",
        policy_version="t",
    )


def test_breaker_pauses_the_goal_on_a_complaint_storm_and_stays_paused() -> None:
    agent = build_agent_policy([default_brief()], default_content())
    sends = [_send(f"d{i}", f"c{i}", NOW) for i in range(400)]
    events: dict[str, list[Event]] = {}
    for i, d in enumerate(sends):
        evs = [_outbound(EventType.DELIVER, d.decision_id, NOW + timedelta(minutes=1), d.customer_id)]
        if i % 50 == 0:  # 2% complain: far above the 0.08% stop rule
            evs.append(_outbound(EventType.COMPLAINT, d.decision_id, NOW + timedelta(hours=3), d.customer_id))
        events[d.customer_id] = evs
    news = agent.nightly(sends, events, NOW + timedelta(days=1))
    assert news and "complaints" in news[0]
    assert GOAL in agent.breaker.paused_goals
    d = agent.decide(state(), ctx(NOW + timedelta(days=1)))
    assert d.kind is DecisionKind.NOTHING and not d.bids
    # a person approves a new brief version: the goal runs again
    newer = default_brief().model_copy(update={"version": 2, "brief_id": "b2"})
    agent.decide(
        state(customer_id="c2"), ctx(NOW + timedelta(days=1)).model_copy(update={"briefs": (newer,)})
    )
    assert GOAL not in agent.breaker.paused_goals


def test_breaker_ignores_a_couple_of_unlucky_complaints() -> None:
    breaker = Breaker()
    for i in range(300):
        breaker.record(_outbound(EventType.DELIVER, f"d{i}", NOW), GOAL, Channel.EMAIL)
    for i in range(2):
        breaker.record(_outbound(EventType.COMPLAINT, f"d{i}", NOW), GOAL, Channel.EMAIL)
    assert breaker.check([default_brief()], NOW + timedelta(hours=1)) == []


def test_breaker_copes_with_more_complaints_than_deliveries_in_the_window() -> None:
    # Late complaints about messages delivered before the rolling week: hits exceed trials.
    breaker = Breaker()
    breaker.record(_outbound(EventType.DELIVER, "d0", NOW), GOAL, Channel.EMAIL)
    for i in range(6):
        breaker.record(_outbound(EventType.COMPLAINT, f"old{i}", NOW), GOAL, Channel.EMAIL)
    assert breaker.check([default_brief()], NOW + timedelta(hours=1))
    assert GOAL in breaker.paused_goals


def test_click_model_ranks_by_clicks_and_scales_by_purchases() -> None:
    model = ClickResponseModel()
    model.lift = (0.3, 0.01)
    for i in range(400):
        model.observe_send(f"c{i}", "light", "email|none|reminder", "none")
        model.observe_send(f"c{i}", "light", "email|pct10|savings", "pct10")
        if i % 5 == 0:
            model.observe_click(f"c{i}", "light", "email|pct10|savings", "pct10")
        if i % 20 == 0:
            model.observe_click(f"c{i}", "light", "email|none|reminder", "none")
    s = state(segment="light", customer_id="someone_new")
    up_offer, _ = model.predict(s, "email|pct10|savings")
    up_plain, _ = model.predict(s, "email|none|reminder")
    assert up_offer > 2 * up_plain > 0
    # a customer who clicked on every offer is worth more to send one to than their segment says
    keen = state(segment="light", customer_id="c0")
    assert model.predict(keen, "email|pct10|savings")[0] > up_offer
    # with no extra purchases per click, nothing is worth sending
    model.lift = (0.0, 0.0)
    assert model.predict(s, "email|pct10|savings")[0] == 0.0


def test_lift_per_click_compares_the_agent_group_with_the_holdout() -> None:
    lift = LiftPerClick(prior=(0.25, 0.25), strength=1.0)
    for i in range(200):
        lift.member(f"a{i}", Group.AGENT, NOW)
        lift.member(f"h{i}", Group.HOLDOUT, NOW)
    for i in range(200):
        for _ in range(2 if i % 2 else 1):  # agent: 1.5 purchases each
            lift.purchase(f"a{i}", NOW + timedelta(days=3))
        lift.purchase(f"h{i}", NOW + timedelta(days=3))  # holdout: 1 each
    for _ in range(400):
        lift.click(NOW)
    k, sd = lift.estimate(NOW + timedelta(days=30))
    assert k == pytest.approx(100 / 401 + 0.25 / 401, rel=1e-6)  # 100 extra purchases over 400 clicks
    assert sd > 0


def test_no_new_message_right_after_the_last_or_while_one_is_scheduled() -> None:
    policy = AutonomousPolicy([OneBid()], None, 0.0, min_gap=timedelta(hours=6))
    just_sent = state(last_contact_at=NOW - timedelta(hours=2), contacts_7d=1)
    d = policy.decide(just_sent, ctx())
    assert d.kind is DecisionKind.NOTHING and d.blocked_by == (rules.TOO_SOON,)
    assert policy.decide(just_sent, ctx(NOW + timedelta(hours=5))).kind is DecisionKind.ACT
    # a message scheduled for later today holds an event-triggered one, whatever the gap
    evening = tuple(1.0 if h == 20 else 0.0 for h in range(24))
    s = state(customer_id="c2", active_hours=evening)
    assert policy.decide(s, ctx()).kind is DecisionKind.ACT
    held = policy.decide(s, ctx(NOW + timedelta(hours=1)), trigger="visit:1")
    assert held.blocked_by == (rules.TOO_SOON,)
