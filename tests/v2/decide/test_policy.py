"""AutonomousPolicy, the hard rules, send-time choice and the plain-English reasons.

Bidders here are tiny fakes that return fixed bids, so these tests pin the auction, rules, sampling
and logging without depending on the real bidders' maths.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from autonomy import interfaces as ifc
from autonomy.contracts import (
    Action,
    Bid,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Envelope,
    Group,
    Reward,
)
from autonomy.decide import explain, rules
from autonomy.decide.policy import AutonomousPolicy, seeded_rng, thompson_with_floor
from autonomy.decide.timing import choose_send_time
from autonomy.fakes import (
    FakeEnvironment,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryStateStore,
    NothingPolicy,
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.loop import run_days

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)  # 12:00 UTC
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


def ctx(seed: int = 0, now: datetime = NOW, **kw: object) -> DecisionContext:
    return DecisionContext(now=now, briefs=(sample_brief(now),), content=tuple(sample_content(now)), seed=seed, **kw)  # type: ignore[arg-type]


def bid(
    net: float,
    sd: float = 0.0,
    channel: Channel = Channel.EMAIL,
    offer: str = "none",
    goal: str = GOAL,
    content: str | None = "ci_0",
    channel_cost: float = 0.001,
) -> Bid:
    return Bid(
        goal_id=goal,
        action=Action(goal_id=goal, channel=channel, offer_id=offer, content_id=content, send_at=NOW),
        uplift=0.05,
        value_if_converted=100.0,
        channel_cost=channel_cost,
        expected_net=net,
        uncertainty=sd,
    )


class FixedBidder:
    def __init__(self, bids: Sequence[Bid], goal_id: str = GOAL) -> None:
        self.goal_id = goal_id
        self._bids = list(bids)
        self.learned: list[Sequence[Reward]] = []

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        """Like the real bidders: proposed for now; the policy's timing step picks the hour."""
        return [
            b.model_copy(update={"action": b.action.model_copy(update={"send_at": ctx.now})})
            for b in self._bids
        ]

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        self.learned.append(rewards)


def policy(*bids: Bid, floor: float | None = 0.0, price: float = 0.0, **kw: object) -> AutonomousPolicy:
    return AutonomousPolicy([FixedBidder(bids)], lambda s: price, floor, **kw)  # type: ignore[arg-type]


# --- the split ------------------------------------------------------------------------------------


def test_policy_satisfies_the_protocol() -> None:
    assert isinstance(policy(), ifc.Policy)


def test_holdout_never_acts() -> None:
    p = policy(bid(500.0, 50.0), bid(300.0, 10.0, Channel.WHATSAPP), floor=0.5)
    for seed in range(200):
        d = p.decide(state(customer_id=f"h{seed}", group=Group.HOLDOUT), ctx(seed))
        assert d.kind is DecisionKind.NOTHING
        assert d.probability == 1.0
        assert d.blocked_by == (rules.HOLDOUT,)
    assert "holdout" in d.reason


def test_baseline_group_delegates_to_the_baseline_policy() -> None:
    p = policy(bid(500.0), baseline_policy=NothingPolicy())
    d = p.decide(state(group=Group.BASELINE), ctx())
    assert d.policy_version == NothingPolicy.version
    assert d.group is Group.BASELINE


# --- the auction ----------------------------------------------------------------------------------


def test_clear_winner_is_sent_with_time_reason_and_probability() -> None:
    p = policy(bid(5.0), bid(1.0, channel=Channel.WHATSAPP, channel_cost=0.03))
    d = p.decide(state(), ctx())
    assert d.kind is DecisionKind.ACT and d.action is not None
    assert d.action.channel is Channel.EMAIL
    assert d.probability == 1.0  # no uncertainty, no floor: a plain argmax
    assert d.action.send_at >= NOW
    assert d.reason.startswith("Sent a reminder by email with no discount")
    assert "email is cheapest" in d.reason
    assert d.bids[0].expected_net == 5.0


def test_attention_price_must_be_beaten() -> None:
    d = policy(bid(3.0), price=4.0).decide(state(), ctx())
    assert d.kind is DecisionKind.NOTHING
    assert d.attention_price == 4.0
    assert d.reason.startswith("Left alone: no message is worth more than the cost of contacting")
    assert policy(bid(5.0), price=4.0).decide(state(), ctx()).kind is DecisionKind.ACT


def test_no_bids_means_nothing_with_certainty() -> None:
    d = policy().decide(state(), ctx())
    assert d.kind is DecisionKind.NOTHING and d.probability == 1.0
    assert "no active goal" in d.reason


def test_weekly_cap_blocks_everything_and_says_so() -> None:
    d = policy(bid(50.0), bid(40.0, channel=Channel.WHATSAPP)).decide(state(contacts_7d=2), ctx())
    assert d.kind is DecisionKind.NOTHING
    assert d.blocked_by == (rules.WEEKLY_CAP,)
    assert d.reason == "Left alone: contacted twice this week, the weekly limit."


def test_blocked_bids_are_dropped_but_others_survive() -> None:
    s = state(
        channels={
            Channel.EMAIL: REACHABLE.model_copy(update={"unsubscribed": True}),
            Channel.WHATSAPP: REACHABLE,
        }
    )
    d = policy(bid(50.0), bid(10.0, channel=Channel.WHATSAPP)).decide(s, ctx())
    assert d.action is not None and d.action.channel is Channel.WHATSAPP
    assert d.blocked_by == (rules.UNSUBSCRIBED,)


def test_non_finite_bids_never_win() -> None:
    p = policy(bid(float("nan")), bid(float("inf"), channel=Channel.WHATSAPP), bid(1.0, float("inf")))
    d = p.decide(state(), ctx())
    assert d.kind is DecisionKind.NOTHING and d.probability == 1.0
    p = policy(bid(float("nan")), bid(1.0, channel=Channel.WHATSAPP))
    d = p.decide(state(), ctx())
    assert d.action is not None and d.action.channel is Channel.WHATSAPP


# --- sampling and probabilities -------------------------------------------------------------------


@pytest.mark.parametrize("floor", [0.0, 0.05, 0.3])
def test_probabilities_are_a_distribution_and_the_chosen_one_is_positive(floor: float) -> None:
    means, sds = [0.0, 1.0, 0.8, -2.0], [0.0, 1.0, 2.0, 0.5]
    for seed in range(50):
        pick = thompson_with_floor(means, sds, floor, seeded_rng(seed, "c1"), draws=500)
        assert sum(pick.probabilities) == pytest.approx(1.0)
        assert all(0.0 <= p <= 1.0 for p in pick.probabilities)
        assert 0.0 < pick.probability <= 1.0
        assert pick.probability == pick.probabilities[pick.index]
        if floor > 0:
            assert min(pick.probabilities) >= floor / len(means) - 1e-12


def test_logged_probability_matches_how_often_the_option_is_chosen() -> None:
    p = policy(bid(1.0, 2.0), bid(0.5, 1.0, channel=Channel.WHATSAPP), floor=0.1)
    decisions = [p.decide(state(customer_id=f"c{i}"), ctx(seed=7)) for i in range(1500)]
    for kind, channel in ((DecisionKind.NOTHING, None), (DecisionKind.ACT, Channel.EMAIL)):
        mine = [d for d in decisions if d.kind is kind and (d.action is None or d.action.channel is channel)]
        share = len(mine) / len(decisions)
        mean_logged = sum(d.probability for d in mine) / len(mine)
        assert abs(share - mean_logged) < 0.05


def test_same_seed_same_decision_and_seeds_differ() -> None:
    p = policy(bid(1.0, 3.0), bid(1.0, 3.0, channel=Channel.WHATSAPP), floor=0.2)
    first = [p.decide(state(customer_id=f"c{i}"), ctx(seed=3)) for i in range(40)]
    again = [p.decide(state(customer_id=f"c{i}"), ctx(seed=3)) for i in range(40)]
    other = [p.decide(state(customer_id=f"c{i}"), ctx(seed=4)) for i in range(40)]
    assert first == again
    assert [d.action for d in first] != [d.action for d in other]


def test_exploration_floor_frequency() -> None:
    floor, n = 0.3, 3  # do nothing + one great bid + one terrible bid, all certain
    p = policy(bid(100.0), bid(-100.0, channel=Channel.WHATSAPP), floor=floor)
    picks = [p.decide(state(customer_id=f"c{i}"), ctx(seed=11)) for i in range(4000)]
    not_best = sum(1 for d in picks if d.action is None or d.action.channel is not Channel.EMAIL)
    assert not_best / len(picks) == pytest.approx(floor * (n - 1) / n, abs=0.03)
    best = next(d for d in picks if d.action is not None and d.action.channel is Channel.EMAIL)
    assert best.probability == pytest.approx(1 - floor + floor / n)
    worst = next(d for d in picks if d.action is not None and d.action.channel is Channel.WHATSAPP)
    assert worst.probability == pytest.approx(floor / n)
    assert "every option keeps getting tested" in worst.reason


def test_floor_defaults_to_the_envelopes() -> None:
    p = AutonomousPolicy([FixedBidder([bid(100.0), bid(-100.0, channel=Channel.WHATSAPP)])])
    d = p.decide(state(), ctx())
    assert d.probability == pytest.approx(1 - 0.05 + 0.05 / 3)  # sample envelope floor is 0.05


# --- rules ----------------------------------------------------------------------------------------


def test_rules_each_block_their_case() -> None:
    env = sample_brief(NOW).envelope
    b = bid(5.0)
    c = ctx()
    assert rules.check_bid(b, state(), c) == rules.OK
    assert rules.holdout(state(group=Group.HOLDOUT)).code == rules.HOLDOUT
    assert rules.check_bid(bid(5.0, goal="other"), state(), c).code == rules.NO_BRIEF
    assert rules.check_bid(bid(5.0, channel=Channel.SMS), state(), c).code == rules.CHANNEL_NOT_ALLOWED
    assert rules.check_bid(bid(5.0, offer="bogo"), state(), c).code == rules.OFFER_NOT_ALLOWED
    unsub = state(channels={Channel.EMAIL: REACHABLE.model_copy(update={"unsubscribed": True})})
    assert rules.check_bid(b, unsub, c).code == rules.UNSUBSCRIBED
    assert rules.check_bid(b, state(channels={}), c).code == rules.NO_CONSENT
    no_addr = state(channels={Channel.EMAIL: ChannelStats(consent=True)})
    assert rules.check_bid(b, no_addr, c).code == rules.NO_ADDRESS
    assert rules.check_bid(b, state(open_complaint=True), c).code == rules.OPEN_COMPLAINT
    assert rules.weekly_cap(state(contacts_7d=1), env.max_contacts_per_week).allowed
    assert rules.check_bid(b, state(contacts_7d=2), c).code == rules.WEEKLY_CAP
    assert rules.check_bid(b, state(), ctx(budget_left={GOAL: 0.0})).code == rules.NO_BUDGET
    assert rules.check_bid(b, state(), ctx(budget_left={GOAL: 10.0})).allowed
    assert rules.check_bid(b, state(), ctx(budget_left={"another_goal": 0.0})).allowed
    late = b.model_copy(update={"action": b.action.model_copy(update={"send_at": NOW.replace(hour=22)})})
    assert rules.check_bid(late, state(), c).code == rules.QUIET_HOURS


def test_quiet_hours_wrap_midnight_and_can_be_off() -> None:
    env = Envelope(channels=(Channel.EMAIL,), budget=1, quiet_start_hour=21, quiet_end_hour=9)
    assert [h for h in range(24) if rules.is_quiet_hour(h, env)] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 21, 22, 23]
    day = env.model_copy(update={"quiet_start_hour": 13, "quiet_end_hour": 15})
    assert [h for h in range(24) if rules.is_quiet_hour(h, day)] == [13, 14]
    off = env.model_copy(update={"quiet_start_hour": 0, "quiet_end_hour": 0})
    assert not any(rules.is_quiet_hour(h, off) for h in range(24))


# --- timing ---------------------------------------------------------------------------------------

ZONES = ("UTC", "America/Los_Angeles", "Asia/Kolkata", "Pacific/Auckland", "America/St_Johns", "Asia/Tokyo")


@pytest.mark.parametrize("tz", ZONES)
def test_send_time_never_quiet_never_past_within_a_day(tz: str) -> None:
    env = sample_brief(NOW).envelope
    night_owl = tuple(1.0 if h == 23 else 0.0 for h in range(24))
    evening = tuple(0.5 if h in (19, 20) else 0.0 for h in range(24))
    # walk across a DST change (early November in the US) in uneven steps
    for i in range(0, 24 * 21, 5):
        now = datetime(2026, 10, 25, tzinfo=UTC) + timedelta(hours=i, minutes=17 * (i % 3))
        for hours in ((), night_owl, evening):
            s = state(timezone=tz, active_hours=hours)
            at = choose_send_time(s, env, now)
            assert now <= at <= now + timedelta(hours=24)
            assert rules.quiet_hours(s, env, at).allowed
            assert not rules.is_quiet_hour(at.astimezone(ZoneInfo(tz)).hour, env)


def test_send_time_prefers_active_hour_then_default_ten() -> None:
    env = sample_brief(NOW).envelope
    morning = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
    evening = tuple(1.0 if h == 19 else 0.0 for h in range(24))
    at = choose_send_time(state(timezone="Asia/Tokyo", active_hours=evening), env, morning)
    assert at.astimezone(ZoneInfo("Asia/Tokyo")).hour == 19
    at = choose_send_time(state(timezone="America/New_York"), env, morning)
    local = at.astimezone(ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (10, 0)
    assert at - morning < timedelta(hours=24)
    # segment profile used when the customer's own hours are unknown
    at = choose_send_time(state(), env, morning, fallback_hours=evening)
    assert at.hour == 19
    # already inside the best hour: send now, not tomorrow
    assert choose_send_time(state(), env, NOW.replace(hour=10, minute=30)) == NOW.replace(hour=10, minute=30)


def test_policy_sets_send_time_in_the_customers_zone() -> None:
    d = policy(bid(5.0)).decide(state(timezone="Asia/Kolkata"), ctx())
    assert d.action is not None
    local = d.action.send_at.astimezone(ZoneInfo("Asia/Kolkata"))
    assert (local.hour, local.minute) == (10, 0)  # 12:00 UTC is 17:30 there, so tomorrow 10:00


# --- reasons and learning -------------------------------------------------------------------------


def test_reasons_read_as_plain_sentences() -> None:
    base = Decision(
        decision_id="d",
        customer_id="c1",
        made_at=NOW,
        kind=DecisionKind.NOTHING,
        probability=1.0,
        reason="-",
        policy_version="t",
    )
    reach = base.model_copy(update={"blocked_by": (rules.UNSUBSCRIBED, rules.NO_CONSENT)})
    assert explain.reason(reach, []).startswith("Left alone: no allowed channel can reach")
    complaint = base.model_copy(update={"blocked_by": (rules.WEEKLY_CAP, rules.OPEN_COMPLAINT)})
    assert explain.reason(complaint, []) == "Left alone: this customer has an open complaint."
    uncertain = base.model_copy(update={"attention_price": 1.0})
    assert explain.reason(uncertain, [bid(3.0, 5.0)]).startswith("Left alone this time")
    # resting chosen by the exploration floor, with nothing uncertain, says so rather than "uncertain"
    rested = explain.reason(base, [bid(3.0)], explored=True)
    assert rested.startswith("Left alone this time: resting is still being tested")
    assert "uncertain" not in rested
    for d in (reach, complaint, uncertain):
        sentence = explain.reason(d, [bid(3.0, 5.0)] if d is uncertain else [])
        assert sentence.endswith(".") and sentence.count(".") <= 2


def test_learn_forwards_goal_rewards_to_bidders() -> None:
    mine, theirs = FixedBidder([bid(1.0)]), FixedBidder([], goal_id="other")
    p = AutonomousPolicy([mine, theirs], None, 0.0)

    def reward(did: str, goal: str | None) -> Reward:
        return Reward(decision_id=did, customer_id="c1", goal_id=goal, window_end=NOW, converted=False, net=0)

    p.learn([reward("a", GOAL), reward("b", None), reward("c", "third")], {})
    assert [r.decision_id for r in mine.learned[0]] == ["a", "b"]
    assert [r.decision_id for r in theirs.learned[0]] == ["b"]


def test_runs_in_the_reference_loop_against_the_fake_world() -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    brief = sample_brief(start)
    p = AutonomousPolicy(
        [FixedBidder([bid(2.0, 1.0), bid(1.5, 1.0, channel=Channel.WHATSAPP, channel_cost=0.03)])],
        lambda s: 0.5 * s.contacts_7d,
    )
    result = run_days(
        env=FakeEnvironment(n_customers=30, start=start, history_days=5),
        policy=p,
        events=InMemoryEventStore(),
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        folder=SimpleStateFolder(),
        briefs=[brief],
        content=sample_content(start),
        days=7,
    )
    assert len(result.decisions) == 30 * 7
    assert all(0 < d.probability <= 1 and d.reason for d in result.decisions)
    acts = [d for d in result.decisions if d.action is not None]
    assert acts
    by_customer: dict[str, int] = {}
    for d in acts:
        by_customer[d.customer_id] = by_customer.get(d.customer_id, 0) + 1
    assert max(by_customer.values()) <= 2  # the weekly cap of 2 holds over the 7 days
