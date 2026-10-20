"""Reading-hour learning, pacing against stop rules, and a plain sentence for every rule code.

Small and fast: fixed bidders, a handful of customers, no Gym.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
    Envelope,
    EventType,
    StopMetric,
    StopRule,
)
from autonomy.decide import explain, rules
from autonomy.decide.pacing import Pacer
from autonomy.decide.policy import AutonomousPolicy
from autonomy.decide.timing import (
    ReadingHourModel,
    allowed_hours,
    choose_send_time,
)
from autonomy.fakes.samples import sample_brief, sample_content

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
GOAL = "goal_winback"
REACHABLE = ChannelStats(address_known=True, consent=True)
ZONES = ("UTC", "America/Los_Angeles", "Asia/Kolkata", "Pacific/Auckland", "America/St_Johns", "Asia/Tokyo")


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


def at_local(day: datetime, hour: int, tz: str, minute: int = 0) -> datetime:
    """`day`'s calendar date at `hour:minute` on the clock of `tz`, as an aware UTC time."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC)


def late_clicks(
    model: ReadingHourModel, cid: str, tz: str, hour: int, n: int, segment: str | None = None
) -> None:
    """n messages, each clicked four hours after it went out, at `hour`:30 local."""
    for i in range(n):
        click = at_local(NOW - timedelta(days=i + 1), hour, tz, 30)
        model.observe_click(cid, click - timedelta(hours=4), click, tz, segment)


# --- reading hours ----------------------------------------------------------------------------------


def test_late_clicks_move_the_posterior_to_the_click_hour() -> None:
    model = ReadingHourModel()
    before = model.profile("c1")[19]
    shares = [before]
    for n in (1, 2, 4):
        late_clicks(model, "c1", "Asia/Tokyo", 19, n)
        shares.append(model.profile("c1")[19])
    assert shares == sorted(shares) and shares[-1] > 3 * shares[0]
    assert model.best_hour("c1") == 19
    assert model.clicks("c1") == pytest.approx(7.0)


def test_quick_click_credits_the_send_hour_and_late_click_the_click_hour() -> None:
    tz = "America/New_York"
    quick = ReadingHourModel()
    for i in range(5):
        day = NOW - timedelta(days=i + 1)
        quick.observe_click("c1", at_local(day, 14, tz), at_local(day, 14, tz, 10), tz)
    assert quick.best_hour("c1") == 14
    late = ReadingHourModel()
    for i in range(5):
        day = NOW - timedelta(days=i + 1)
        late.observe_click("c1", at_local(day, 11, tz), at_local(day, 17, tz, 5), tz)
    assert late.best_hour("c1") == 17
    # a click 90 minutes later is split between the send hour and the click hour
    split = ReadingHourModel()
    split.observe_click("c1", at_local(NOW, 11, tz), at_local(NOW, 12, tz, 30), tz)
    own = split.alpha("c1") - split.alpha("someone_else")
    assert own[11] == pytest.approx(0.5) and own[12] == pytest.approx(0.5)


def test_new_customer_borrows_the_segment_then_the_business() -> None:
    model = ReadingHourModel()
    for k in range(30):
        late_clicks(model, f"night{k}", "UTC", 20, 2, segment="night")
    assert model.best_hour("newcomer", "night") == 20
    # another segment has no clicks: it follows the business, which has moved towards 20 too
    assert model.segment_mean("day")[20] == model.business_mean()[20]
    assert model.business_mean()[20] > model.business_mean()[3]
    # the customer's own clicks win over the segment's once there are a few
    late_clicks(model, "early", "UTC", 8, 5, segment="night")
    assert model.best_hour("early", "night") == 8


def test_pools_judge_quick_clicks_per_send_not_by_where_we_sent() -> None:
    """5,000 sends at 10:00 with 5% clicks against 500 sends at 18:00 with 15%: 18:00 is the better
    hour although three times as many clicks happened at 10:00."""
    model = ReadingHourModel()
    for i in range(5000):
        model.observe_send(f"a{i}", at_local(NOW, 10, "UTC"), "UTC")
        if i % 20 == 0:
            model.observe_click(f"a{i}", at_local(NOW, 10, "UTC"), at_local(NOW, 10, "UTC", 5), "UTC")
    for i in range(500):
        model.observe_send(f"b{i}", at_local(NOW, 18, "UTC"), "UTC")
        if i % 7 == 0:
            model.observe_click(f"b{i}", at_local(NOW, 18, "UTC"), at_local(NOW, 18, "UTC", 5), "UTC")
    mean = model.business_mean()
    assert mean[18] > mean[10]


def test_thompson_draws_spread_when_unsure_and_agree_when_sure() -> None:
    model = ReadingHourModel()
    env = sample_brief(NOW).envelope
    allowed = allowed_hours(env)
    unsure = {model.sample_hour("nobody", None, np.random.default_rng(s), allowed) for s in range(60)}
    assert len(unsure) >= 4
    late_clicks(model, "sure", "UTC", 16, 12)
    sure = [model.sample_hour("sure", None, np.random.default_rng(s), allowed) for s in range(60)]
    assert sum(1 for h in sure if h in (15, 16, 17)) >= 54
    # same generator seed, same draw
    a = model.sample_profile("sure", None, np.random.default_rng(7))
    b = model.sample_profile("sure", None, np.random.default_rng(7))
    assert a == b


@pytest.mark.parametrize("tz", ZONES)
def test_learned_send_time_respects_quiet_hours_in_every_zone(tz: str) -> None:
    env = sample_brief(NOW).envelope  # quiet 21:00-09:00 local
    model = ReadingHourModel()
    late_clicks(model, "owl", tz, 23, 10)  # reads at 23:00, inside quiet hours
    late_clicks(model, "evening", tz, 19, 10)
    for i in range(0, 24 * 10, 7):  # across the US daylight-saving change on 1 November
        now = datetime(2026, 10, 28, tzinfo=UTC) + timedelta(hours=i, minutes=13 * (i % 4))
        for cid in ("owl", "evening", "nobody"):
            s = state(customer_id=cid, timezone=tz)
            for at in (
                choose_send_time(s, env, now, reading=model),
                choose_send_time(s, env, now, reading=model, rng=np.random.default_rng(i)),
            ):
                assert now <= at <= now + timedelta(hours=24)
                assert not rules.is_quiet_hour(at.astimezone(ZoneInfo(tz)).hour, env)
        evening = choose_send_time(state(customer_id="evening", timezone=tz), env, now, reading=model)
        assert evening.astimezone(ZoneInfo(tz)).hour == 19
        # a 23:00 reader is caught at the last allowed hour before quiet hours, not at the default
        owl = choose_send_time(state(customer_id="owl", timezone=tz), env, now, reading=model)
        assert owl.astimezone(ZoneInfo(tz)).hour == 20


def test_choose_send_time_keeps_its_old_behaviour_without_a_model() -> None:
    env = sample_brief(NOW).envelope
    evening = tuple(1.0 if h == 19 else 0.0 for h in range(24))
    at = choose_send_time(state(timezone="Asia/Tokyo", active_hours=evening), env, NOW)
    assert at.astimezone(ZoneInfo("Asia/Tokyo")).hour == 19
    at = choose_send_time(state(timezone="America/New_York"), env, NOW)
    assert at.astimezone(ZoneInfo("America/New_York")).hour == 10
    # learned scores win over the folder's active hours
    noon = tuple(1.0 if h == 12 else 0.0 for h in range(24))
    at = choose_send_time(state(active_hours=evening), env, NOW, hour_scores=noon)
    assert at.hour == 12


# --- the policy with reading hours and pacing ---------------------------------------------------------


class FixedBidder:
    goal_id = GOAL

    def __init__(self, bids: Sequence[tuple[Channel, float]]) -> None:
        self._bids = bids

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        return [
            Bid(
                goal_id=GOAL,
                action=Action(goal_id=GOAL, channel=ch, content_id="ci_0", send_at=ctx.now),
                uplift=0.05,
                value_if_converted=100.0,
                expected_net=net,
            )
            for ch, net in self._bids
        ]


def test_policy_times_sends_by_the_reading_hour_model() -> None:
    model = ReadingHourModel()
    late_clicks(model, "c1", "Asia/Kolkata", 18, 15)
    bidder = FixedBidder([(Channel.EMAIL, 5.0)])
    on = AutonomousPolicy([bidder], exploration_floor=0.0, reading_hours=model)
    d = on.decide(state(timezone="Asia/Kolkata"), ctx())
    assert d.action is not None and d.action.send_at.astimezone(ZoneInfo("Asia/Kolkata")).hour == 18
    off = AutonomousPolicy([bidder], exploration_floor=0.0)
    d = off.decide(state(timezone="Asia/Kolkata"), ctx())
    assert d.action is not None and d.action.send_at.astimezone(ZoneInfo("Asia/Kolkata")).hour == 10
    # the hour draw has its own generator: the auction's probabilities are the same with it on
    assert (
        on.decide(state(customer_id="c2"), ctx()).probability
        == off.decide(state(customer_id="c2"), ctx()).probability
    )


def strict_envelope(threshold: float = 0.0008) -> Envelope:
    return Envelope(
        channels=(Channel.EMAIL, Channel.WHATSAPP),
        budget=1000,
        stop_rules=(StopRule(metric=StopMetric.COMPLAINT_RATE, threshold=threshold),),
    )


def fill(
    pacer: Pacer, channel: Channel, delivered: int, complaints: int, at: datetime = NOW, goal: str = GOAL
) -> None:
    for i in range(delivered):
        pacer.record(goal, channel, EventType.DELIVER, at - timedelta(minutes=i))
    for i in range(complaints):
        pacer.record(goal, channel, EventType.COMPLAINT, at - timedelta(minutes=i))


def test_pressure_rises_with_complaints_and_falls_with_deliveries() -> None:
    env = strict_envelope()
    by_complaints = []
    for c in range(7):
        pacer = Pacer()
        fill(pacer, Channel.WHATSAPP, 1000, c)
        by_complaints.append(pacer.pressure(GOAL, Channel.WHATSAPP, env, NOW))
    assert by_complaints == sorted(by_complaints)
    assert by_complaints[0] == 0.0 and by_complaints[-1] == 1.0
    by_volume = []
    for n in (300, 1000, 3000, 10000, 30000):
        pacer = Pacer()
        fill(pacer, Channel.WHATSAPP, n, 3)
        by_volume.append(pacer.pressure(GOAL, Channel.WHATSAPP, env, NOW))
    assert by_volume == sorted(by_volume, reverse=True)
    assert by_volume[-1] == 0.0


def test_pressure_blocks_near_the_limit_and_lands_on_the_channel_that_complains() -> None:
    env = strict_envelope()
    pacer = Pacer()
    fill(pacer, Channel.WHATSAPP, 600, 5)  # one more complaint could trip the breaker
    fill(pacer, Channel.EMAIL, 1500, 0)
    assert pacer.pressure(GOAL, Channel.WHATSAPP, env, NOW) == 1.0
    assert pacer.surcharge(Channel.WHATSAPP, 1.0, 0.5, NOW) == float("inf")
    assert pacer.pressure(GOAL, Channel.EMAIL, env, NOW) < 0.5
    # other goals are judged on their own outcomes
    assert pacer.pressure("other_goal", Channel.WHATSAPP, env, NOW) == 0.0
    # the same complaints among many clean emails leave the goal far from its limit (a fresh pacer:
    # judging a later day drops old outcomes for good, so time must not go backwards)
    diluted = Pacer()
    fill(diluted, Channel.WHATSAPP, 600, 5)
    fill(diluted, Channel.EMAIL, 7500, 0)
    assert diluted.counts(GOAL, Channel.WHATSAPP, NOW) == (600.0, 5.0, 0.0)
    assert diluted.pressure(GOAL, Channel.WHATSAPP, env, NOW) < 0.5
    # a week later the complaints have left the window
    assert pacer.pressure(GOAL, Channel.WHATSAPP, env, NOW + timedelta(days=8)) == 0.0


def test_window_holds_for_outcomes_recorded_out_of_order() -> None:
    """A fresh delivery recorded before an old complaint must not keep that complaint in the week."""
    pacer = Pacer()
    pacer.record(GOAL, Channel.WHATSAPP, EventType.DELIVER, NOW)
    for i in range(5):
        pacer.record(GOAL, Channel.WHATSAPP, EventType.COMPLAINT, NOW - timedelta(days=9, minutes=i))
    assert pacer.counts(GOAL, None, NOW) == (1.0, 0.0, 0.0)


def test_reset_forgets_a_goal_like_the_breakers_resume() -> None:
    env = strict_envelope()
    pacer = Pacer()
    fill(pacer, Channel.WHATSAPP, 600, 5)
    fill(pacer, Channel.WHATSAPP, 600, 5, goal="other_goal")
    pacer.set_unsubscribe_baseline(GOAL, 0.01)
    assert pacer.pressure(GOAL, Channel.WHATSAPP, env, NOW) == 1.0
    pacer.reset(GOAL)
    assert pacer.pressure(GOAL, Channel.WHATSAPP, env, NOW) == 0.0
    assert pacer.pressure("other_goal", Channel.WHATSAPP, env, NOW) == 1.0


def test_unsubscribe_rule_is_paced_only_once_its_baseline_is_known() -> None:
    env = Envelope(
        channels=(Channel.EMAIL,),
        budget=1000,
        stop_rules=(StopRule(metric=StopMetric.UNSUBSCRIBE_MULTIPLE, threshold=3.0),),
    )
    pacer = Pacer()
    for i in range(500):
        pacer.record(GOAL, Channel.EMAIL, EventType.DELIVER, NOW - timedelta(minutes=i))
    for i in range(12):
        pacer.record(GOAL, Channel.EMAIL, EventType.UNSUBSCRIBE, NOW - timedelta(minutes=i))
    assert pacer.pressure(GOAL, Channel.EMAIL, env, NOW) == 0.0
    pacer.set_unsubscribe_baseline(GOAL, 0.004)
    assert pacer.pressure(GOAL, Channel.EMAIL, env, NOW) > 0.5


def test_surcharge_grows_with_pressure() -> None:
    pacer = Pacer()
    pacer.observe_bid(Channel.WHATSAPP, 2.0, NOW)
    pacer.observe_bid(Channel.WHATSAPP, 4.0, NOW)
    assert pacer.reference(Channel.WHATSAPP) == 0.0  # frozen until the next day
    pacer.observe_bid(Channel.WHATSAPP, 3.0, NOW + timedelta(days=1))
    assert pacer.reference(Channel.WHATSAPP) == pytest.approx(3.0)
    later = NOW + timedelta(days=1)
    charges = [pacer.surcharge(Channel.WHATSAPP, p, 0.1, later) for p in (0.0, 0.2, 0.5, 0.8, 0.99)]
    assert charges[0] == 0.0 and charges == sorted(charges)
    assert charges[2] == pytest.approx(3.0)


def paced_ctx(now: datetime = NOW) -> DecisionContext:
    brief = sample_brief(now).model_copy(update={"envelope": strict_envelope()})
    return DecisionContext(now=now, briefs=(brief,), content=tuple(sample_content(now)))


def test_policy_holds_a_channel_at_full_pressure_and_says_so() -> None:
    pacer = Pacer()
    fill(pacer, Channel.WHATSAPP, 600, 5)
    fill(pacer, Channel.EMAIL, 1500, 0)
    both = AutonomousPolicy(
        [FixedBidder([(Channel.WHATSAPP, 9.0), (Channel.EMAIL, 2.0)])], exploration_floor=0.0, pacer=pacer
    )
    d = both.decide(state(), paced_ctx())
    assert d.action is not None and d.action.channel is Channel.EMAIL
    assert rules.PACING in d.blocked_by
    only = AutonomousPolicy([FixedBidder([(Channel.WHATSAPP, 9.0)])], exploration_floor=0.0, pacer=pacer)
    d = only.decide(state(), paced_ctx())
    assert d.kind is DecisionKind.NOTHING
    assert d.reason == explain.BLOCK_SENTENCE[rules.PACING]
    # switched off (the default), the same customer gets the WhatsApp message
    off = AutonomousPolicy([FixedBidder([(Channel.WHATSAPP, 9.0)])], exploration_floor=0.0)
    d = off.decide(state(), paced_ctx())
    assert d.action is not None and d.action.channel is Channel.WHATSAPP


def test_policy_surcharge_sheds_the_low_value_sends_first() -> None:
    pacer = Pacer()
    pacer.observe_bid(Channel.WHATSAPP, 3.0, NOW - timedelta(days=1))  # typical message: $3
    fill(pacer, Channel.WHATSAPP, 1000, 3)
    pressure = pacer.pressure(GOAL, Channel.WHATSAPP, strict_envelope(), NOW)
    assert 0.0 < pressure < 1.0
    low = AutonomousPolicy([FixedBidder([(Channel.WHATSAPP, 0.5)])], exploration_floor=0.0, pacer=pacer)
    d = low.decide(state(), paced_ctx())
    assert d.kind is DecisionKind.NOTHING
    assert d.reason.startswith("Left alone: we are slowing down on WhatsApp")
    high = AutonomousPolicy([FixedBidder([(Channel.WHATSAPP, 50.0)])], exploration_floor=0.0, pacer=pacer)
    d = high.decide(state(customer_id="c9"), paced_ctx())
    assert d.action is not None and d.attention_price > 0
    assert "slow that channel down" in d.reason


def test_reason_judges_options_after_the_surcharge() -> None:
    """WhatsApp is worth more before its surcharge, email after it: sending email is the top option,
    not "a close alternative that won the draw"."""
    bids = [
        Bid(
            goal_id=GOAL,
            action=Action(goal_id=GOAL, channel=ch, content_id="ci_0", send_at=NOW),
            uplift=0.05,
            value_if_converted=100.0,
            expected_net=net,
        )
        for ch, net in ((Channel.WHATSAPP, 9.0), (Channel.EMAIL, 2.0))
    ]
    surcharges = {(GOAL, Channel.WHATSAPP): 8.0}
    sent = Decision(
        decision_id="d",
        customer_id="c1",
        made_at=NOW,
        kind=DecisionKind.ACT,
        action=bids[1].action,
        probability=1.0,
        expected_net=2.0,
        attention_price=0.5,
        reason="-",
        policy_version="t",
    )
    text = explain.reason(sent, bids, surcharges=surcharges, base_price=0.5)
    assert "expected to add $2.00" in text and "uncertain" not in text
    rested = sent.model_copy(update={"kind": DecisionKind.NOTHING, "action": None, "expected_net": 0.0})
    text = explain.reason(rested, bids, surcharges=surcharges)
    assert "about $1.50 after costs" in text  # email's $2.00 less the $0.50 price, not WhatsApp's $9


# --- every rule code has a sentence -------------------------------------------------------------------


def one_sentence(text: str) -> bool:
    body = text[:-1]
    return text.endswith(".") and ". " not in body and "\n" not in text and len(text) < 160


@pytest.mark.parametrize("code", rules.ALL_CODES)
def test_every_rule_code_reads_as_one_plain_sentence(code: str) -> None:
    assert code in explain.BLOCK_SENTENCE
    blocked = Decision(
        decision_id="d",
        customer_id="c1",
        made_at=NOW,
        kind=DecisionKind.NOTHING,
        probability=1.0,
        reason="-",
        blocked_by=(code,),
        policy_version="t",
    )
    sentence = explain.reason(blocked, [])
    assert sentence == explain.BLOCK_SENTENCE[code]
    assert one_sentence(sentence), sentence
    assert "_" not in sentence  # no bare snake_case codes


def test_new_codes_say_what_happened() -> None:
    def said(*codes: str) -> str:
        d = Decision(
            decision_id="d",
            customer_id="c1",
            made_at=NOW,
            kind=DecisionKind.NOTHING,
            probability=1.0,
            reason="-",
            blocked_by=codes,
            policy_version="t",
        )
        return explain.reason(d, [], contacts_7d=2)

    assert "pause" in said(rules.SNOOZED)
    assert "hours ago" in said(rules.TOO_SOON)
    assert "no discounts" in said(rules.NO_DISCOUNTS)
    assert "approval" in said(rules.OFFER_NEEDS_APPROVAL)
    assert "slowing down" in said(rules.PACING)
    # a rule that stops every message is named before one that stops only some options
    assert said(rules.NO_DISCOUNTS, rules.TOO_SOON) == explain.BLOCK_SENTENCE[rules.TOO_SOON]
    assert said(rules.WEEKLY_CAP, rules.PACING) == "Left alone: contacted twice this week, the weekly limit."
    assert said(rules.UNSUBSCRIBED, rules.NO_CONSENT).startswith("Left alone: no allowed channel can reach")
