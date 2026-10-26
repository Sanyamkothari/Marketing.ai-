"""Channel journeys (plan-v3 decision 4): parsing, pricing, running, stopping and logging.

The bidders are small fakes with fixed uplifts, so these tests pin the journey layer and its hooks
in `AutonomousPolicy` without depending on the real bidders' maths.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from autonomy.contracts import (
    Action,
    Bid,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Group,
)
from autonomy.decide import rules
from autonomy.decide.journeys import (
    COMPLAINT,
    CONDITION_FAILED,
    NEVER_READ,
    PURCHASED,
    UNSUBSCRIBED,
    JourneyConfig,
    JourneyRunner,
    StepCondition,
    load_journeys,
    never_read,
    parse_journeys,
)
from autonomy.decide.policy import AutonomousPolicy
from autonomy.fakes.samples import sample_brief, sample_content

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
GOAL = "goal_winback"
REACHABLE = ChannelStats(address_known=True, consent=True)
VALUE = 100.0


def state(**kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": "c1",
        "as_of": NOW,
        "channels": {Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE},
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def ctx(now: datetime = NOW, seed: int = 0, **kw: object) -> DecisionContext:
    return DecisionContext(
        now=now,
        briefs=(sample_brief(NOW),),
        content=tuple(sample_content(NOW)),
        seed=seed,
        **kw,  # type: ignore[arg-type]
    )


def bid(
    channel: Channel,
    uplift: float,
    cost: float,
    content: str = "ci_0",
    value: float = VALUE,
    sd: float = 0.0,
) -> Bid:
    return Bid(
        goal_id=GOAL,
        action=Action(goal_id=GOAL, channel=channel, offer_id="none", content_id=content, send_at=NOW),
        uplift=uplift,
        value_if_converted=value,
        channel_cost=cost,
        expected_net=uplift * value - cost,
        uncertainty=sd,
    )


class PriceBidder:
    """Fixed bids; offers nothing to a customer who has bought since NOW (the audience filter)."""

    def __init__(self, bids: Sequence[Bid], reachable_only: bool = True) -> None:
        self.goal_id = GOAL
        self._bids = list(bids)
        self._reachable_only = reachable_only

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        if state.last_purchase_at is not None and state.last_purchase_at >= NOW:
            return []
        reachable = set(state.contactable_channels())
        return [
            b.model_copy(update={"action": b.action.model_copy(update={"send_at": ctx.now})})
            for b in self._bids
            if b.action.channel in reachable or not self._reachable_only
        ]


EMAIL = bid(Channel.EMAIL, 0.05, 0.001)
WHATSAPP = bid(Channel.WHATSAPP, 0.06, 0.03)


def make(
    bids: Sequence[Bid] = (EMAIL, WHATSAPP),
    *,
    config: JourneyConfig | None = None,
    floor: float | None = 0.0,
    **kw: object,
) -> AutonomousPolicy:
    runner = JourneyRunner(config or load_journeys())
    return AutonomousPolicy([PriceBidder(bids)], None, floor, journeys=runner, **kw)  # type: ignore[arg-type]


def only(journey_ids: Sequence[str]) -> JourneyConfig:
    full = load_journeys()
    return full.model_copy(
        update={"journeys": tuple(j for j in full.journeys if j.journey_id in journey_ids)}
    )


def stops(policy: AutonomousPolicy) -> list[str]:
    """Why journeys ended, in order."""
    return [e.code for e in runner_of(policy).history if e.kind == "stopped"]


def runner_of(policy: AutonomousPolicy) -> JourneyRunner:
    assert policy.journeys is not None
    return policy.journeys


def start_b() -> tuple[AutonomousPolicy, Decision]:
    """A policy that picks journey B for the customer, and that first decision."""
    policy = make(config=only(["B"]))
    first = policy.decide(state(), ctx())
    assert first.kind is DecisionKind.ACT and first.journey_id == "B"
    assert first.action is not None and first.action.channel is Channel.EMAIL
    return policy, first


def due_ctx(first: Decision, days: float = 3.0, extra_hours: float = 2.0) -> DecisionContext:
    assert first.action is not None
    return ctx(first.action.send_at + timedelta(days=days, hours=extra_hours))


def after_first(first: Decision, **kw: object) -> CustomerState:
    """The state when the second step is due: the first message went out and the week has one contact."""
    assert first.action is not None
    base: dict[str, object] = {"contacts_7d": 1, "last_contact_at": first.action.send_at}
    base.update(kw)
    return state(**base)


# --- parsing ---------------------------------------------------------------------------------------


def test_shipped_config_has_the_four_journeys() -> None:
    cfg = load_journeys()
    assert [j.journey_id for j in cfg.journeys] == ["A", "B", "C", "D"]
    a, b, c, d = cfg.journeys
    assert [s.channel for s in a.steps] == [Channel.EMAIL]
    assert [(s.channel, s.wait_days, s.when) for s in b.steps] == [
        (Channel.EMAIL, 0.0, StepCondition.ALWAYS),
        (Channel.WHATSAPP, 3.0, StepCondition.NO_PURCHASE),
    ]
    assert [s.channel for s in c.steps] == [Channel.WHATSAPP]
    assert c.value_bar is not None and c.value_bar > 0
    assert [s.channel for s in d.steps] == [Channel.WHATSAPP, Channel.EMAIL]
    assert a.value_bar is None and b.value_bar is None and d.value_bar is None


def test_describe_and_window() -> None:
    b = load_journeys().get("B")
    assert b is not None
    assert b.describe() == "email now, then WhatsApp after 3 days if they have not bought"
    assert b.window_days(14) == 17.0


def test_parse_reads_ids_settings_and_conditions() -> None:
    raw: dict[str, Any] = yaml.safe_load("""
        settings: {never_open_after_sends: 3}
        journeys:
          - id: X
            name: Push then email
            steps:
              - {channel: push}
              - {channel: email, wait_days: 1.5, when: not_opened}
        """)
    cfg = parse_journeys(raw)
    assert cfg.settings.never_open_after_sends == 3
    x = cfg.journeys[0]
    assert x.steps[1].when is StepCondition.NOT_OPENED and x.steps[1].wait_days == 1.5


@pytest.mark.parametrize(
    "journeys",
    [
        [],  # nothing
        [{"id": "X", "name": "x", "steps": []}],  # a journey needs a step
        [{"id": "X", "name": "x", "steps": [{"channel": "email", "wait_days": 1}]}],  # first step waits
        [
            {"id": "X", "name": "x", "steps": [{"channel": "email", "when": "no_purchase"}]}
        ],  # first has a condition
        [{"id": "X", "name": "x", "steps": [{"channel": "carrier_pigeon"}]}],  # unknown channel
        [{"id": "X", "name": "x", "steps": [{"channel": "email"}, {"channel": "sms", "when": "sometimes"}]}],
        [
            {"id": "X", "name": "x", "steps": [{"channel": "email"}]},
            {"id": "X", "name": "y", "steps": [{"channel": "sms"}]},
        ],  # duplicate id
    ],
)
def test_bad_definitions_are_refused(journeys: list[dict[str, Any]]) -> None:
    with pytest.raises((ValueError, ValidationError)):
        parse_journeys({"journeys": journeys})


def test_a_typo_in_a_step_field_fails_loudly() -> None:
    with pytest.raises(ValidationError):
        parse_journeys({"journeys": [{"id": "X", "name": "x", "steps": [{"channel": "email", "wait": 2}]}]})


# --- pricing: a journey is an option in the auction -------------------------------------------------


def test_journey_a_is_the_plain_email_bid_and_b_adds_the_second_step() -> None:
    runner = JourneyRunner(only(["A", "B"]))
    offered = [EMAIL, WHATSAPP]
    out = runner.expand(offered, state())
    by_journey = {b.action.journey_id: b for b in out if b.action.journey_id}
    assert set(by_journey) == {"A", "B"}
    a, b = by_journey["A"], by_journey["B"]
    assert a.expected_net == pytest.approx(EMAIL.expected_net)
    assert a.channel_cost == EMAIL.channel_cost and a.uplift == EMAIL.uplift
    # B: the second step runs for the 95% who have not bought, with half a first message's uplift
    reach = 1 - EMAIL.uplift
    gain = 0.5 * WHATSAPP.uplift
    assert b.expected_net == pytest.approx(
        EMAIL.expected_net + reach * (gain * VALUE - WHATSAPP.channel_cost)
    )
    assert b.uplift == pytest.approx(EMAIL.uplift + reach * gain)
    assert b.channel_cost == pytest.approx(EMAIL.channel_cost + reach * WHATSAPP.channel_cost)
    # WhatsApp starts no journey in this config, so its own bid goes through as it was
    assert WHATSAPP in out


def test_a_second_step_that_costs_more_than_it_brings_makes_the_journey_worse() -> None:
    dear = bid(Channel.WHATSAPP, 0.0005, 0.03)  # 0.0025 uplift value... half of 0.0005 x 100 = 0.025 < 0.03
    out = JourneyRunner(only(["A", "B"])).expand([EMAIL, dear], state())
    nets = {b.action.journey_id: b.expected_net for b in out if b.action.journey_id}
    assert nets["B"] < nets["A"]


def test_value_bar_keeps_whatsapp_now_for_valuable_customers() -> None:
    runner = JourneyRunner(only(["C"]))
    bar = load_journeys().get("C").value_bar  # type: ignore[union-attr]
    assert bar is not None
    low = bid(Channel.WHATSAPP, 0.06, 0.03, value=bar - 1)
    high = bid(Channel.WHATSAPP, 0.06, 0.03, value=bar)
    assert runner.expand([low], state()) == []
    assert [b.action.journey_id for b in runner.expand([high], state())] == ["C"]


def test_value_bar_can_be_replaced_per_shop() -> None:
    cfg = load_journeys().with_value_bar("C", 500.0)
    assert JourneyRunner(only_ids(cfg, ["C"])).expand([WHATSAPP], state()) == []
    with pytest.raises(KeyError):
        cfg.with_value_bar("Z", 1.0)


def only_ids(cfg: JourneyConfig, ids: Sequence[str]) -> JourneyConfig:
    return cfg.model_copy(update={"journeys": tuple(j for j in cfg.journeys if j.journey_id in ids)})


def test_a_journey_needs_a_bid_on_every_one_of_its_channels() -> None:
    out = JourneyRunner(only(["A", "B"])).expand([EMAIL], state())
    assert [b.action.journey_id for b in out] == ["A"]


def test_customers_who_never_read_a_channel_are_not_offered_journeys_that_use_it() -> None:
    deaf_email = ChannelStats(address_known=True, consent=True, sends=6, delivers=6)
    runner = JourneyRunner(load_journeys())
    ids = {
        b.action.journey_id
        for b in runner.expand(
            [EMAIL, WHATSAPP], state(channels={Channel.EMAIL: deaf_email, Channel.WHATSAPP: REACHABLE})
        )
    }
    assert ids == {"C"}  # A and B start with email, D ends with it
    ids = {b.action.journey_id for b in runner.expand([EMAIL, WHATSAPP], state())}
    assert ids == {"A", "B", "C", "D"}


def test_never_read_needs_enough_sends_and_no_engagement() -> None:
    quiet = ChannelStats(address_known=True, consent=True, sends=5)
    assert never_read(quiet, 5)
    assert not never_read(quiet, 6)
    assert not never_read(quiet, 0)  # switched off
    assert not never_read(quiet.model_copy(update={"clicks": 1}), 5)
    assert not never_read(quiet.model_copy(update={"replies": 1}), 5)
    assert not never_read(None, 5)


# --- logging: the journey's probability --------------------------------------------------------------


def test_journey_probability_sums_every_option_of_the_journey() -> None:
    # Two email contents x journeys A and B, plus a plain WhatsApp bid: 1 + 4 + 1 options. With the
    # floor at 1 every option has chance 1/6, so a journey of two options has chance 2/6.
    bids = [EMAIL, bid(Channel.EMAIL, 0.05, 0.001, content="ci_1"), WHATSAPP]
    seen: set[str] = set()
    for seed in range(60):
        policy = make(bids, config=only(["A", "B"]), floor=1.0)
        d = policy.decide(state(), ctx(seed=seed))
        if d.journey_id is None:
            continue
        seen.add(d.journey_id)
        assert d.kind is DecisionKind.ACT and d.action is not None
        assert d.action.journey_id == d.journey_id
        assert d.probability == pytest.approx(1 / 6)
        assert d.journey_probability == pytest.approx(2 / 6)
    assert seen == {"A", "B"}


def test_journey_probability_is_at_least_the_decisions_own_and_in_range() -> None:
    bids = [EMAIL, bid(Channel.EMAIL, 0.03, 0.001, content="ci_1", sd=1.0), WHATSAPP]
    for seed in range(40):
        d = make(bids, floor=0.2).decide(state(customer_id=f"c{seed}"), ctx(seed=seed))
        if d.journey_id is None:
            assert d.journey_probability is None
            continue
        assert d.journey_probability is not None
        assert d.probability - 1e-9 <= d.journey_probability <= 1.0


def test_a_single_message_decision_has_no_journey_fields() -> None:
    d = make().decide(state(channels={Channel.SMS: REACHABLE}), ctx())
    assert d.journey_id is None and d.journey_probability is None


def test_first_decision_explains_the_journey() -> None:
    _, first = start_b()
    assert first.reason.endswith("Journey B: email now, then WhatsApp after 3 days if they have not bought.")


# --- journeys off: nothing changes ---------------------------------------------------------------------


def test_journeys_off_gives_identical_decisions() -> None:
    bids = [EMAIL, bid(Channel.EMAIL, 0.03, 0.001, content="ci_1", sd=1.0), WHATSAPP]
    for seed in range(30):
        s = state(customer_id=f"c{seed}", contacts_7d=seed % 3)
        plain = AutonomousPolicy([PriceBidder(bids)], None, 0.1)
        off = AutonomousPolicy([PriceBidder(bids)], None, 0.1, journeys=None)
        a, b = plain.decide(s, ctx(seed=seed)), off.decide(s, ctx(seed=seed))
        assert a == b
        assert a.journey_id is None and a.journey_probability is None
        assert a.action is None or a.action.journey_id is None


def test_journeys_on_never_changes_the_single_message_rules_for_other_channels() -> None:
    sms = bid(Channel.SMS, 0.04, 0.02)
    s = state(channels={Channel.SMS: REACHABLE})
    brief = sample_brief(NOW)
    brief = brief.model_copy(
        update={"envelope": brief.envelope.model_copy(update={"channels": (Channel.SMS,)})}
    )
    c = DecisionContext(now=NOW, briefs=(brief,), content=tuple(sample_content(NOW)))
    on = make([sms]).decide(s, c)
    off = AutonomousPolicy([PriceBidder([sms])], None, 0.0).decide(s, c)
    assert on == off


# --- running a journey ----------------------------------------------------------------------------------


def test_the_second_step_goes_out_when_due_with_probability_one() -> None:
    policy, first = start_b()
    second = policy.decide(after_first(first), due_ctx(first))
    assert second.kind is DecisionKind.ACT and second.action is not None
    assert second.action.channel is Channel.WHATSAPP
    assert second.action.journey_id == "B" and second.action.follow_up_step == 1
    assert second.journey_id == "B"
    assert second.probability == 1.0
    assert second.journey_probability == first.journey_probability
    assert second.decision_id != first.decision_id
    assert second.reason.startswith("Sent follow-up 1")
    assert "Part of Journey B" in second.reason
    assert runner_of(policy).active("c1") is None  # B has two steps: it is over
    assert [e.kind for e in runner_of(policy).history] == ["started", "sent", "completed"]


def test_the_customer_is_held_between_steps() -> None:
    policy, first = start_b()
    assert first.action is not None
    midway = ctx(first.action.send_at + timedelta(days=1))
    held = policy.decide(after_first(first), midway)
    assert held.kind is DecisionKind.NOTHING and held.journey_id == "B"
    assert held.probability == 1.0 and held.journey_probability == first.journey_probability
    assert held.reason.startswith("Waiting: Journey B sends its next message, by WhatsApp, on ")
    assert runner_of(policy).active("c1") is not None


def test_cart_triggers_still_reach_the_auction_mid_journey() -> None:
    policy, first = start_b()
    assert first.action is not None
    d = policy.decide(after_first(first), ctx(first.action.send_at + timedelta(days=1)), trigger="cart:e1")
    assert d.reason != "" and not d.reason.startswith("Waiting")


def test_hold_can_be_switched_off() -> None:
    cfg = only(["B"]).model_copy(
        update={"settings": load_journeys().settings.model_copy(update={"hold_during_journey": False})}
    )
    policy = make(config=cfg)
    first = policy.decide(state(), ctx())
    assert first.action is not None
    d = policy.decide(after_first(first), ctx(first.action.send_at + timedelta(days=1)))
    assert not d.reason.startswith("Waiting")


def test_rerunning_the_same_moment_does_not_move_the_journey_twice() -> None:
    policy, first = start_b()
    s, c = after_first(first), due_ctx(first)
    a = policy.decide(s, c)
    b = policy.decide(s, c)
    assert a.kind is DecisionKind.ACT and b.kind is DecisionKind.ACT
    assert a.decision_id == b.decision_id
    assert b.action is not None and b.action.follow_up_step == 1
    # and deciding the first moment again replaces the first decision instead of starting a second journey
    again = policy.decide(state(), ctx())
    assert again.decision_id == first.decision_id


# --- stopping -----------------------------------------------------------------------------------------------


def test_a_purchase_stops_the_journey_at_once_even_before_the_second_step_is_due() -> None:
    policy, first = start_b()
    assert first.action is not None
    bought = after_first(first, last_purchase_at=first.action.send_at + timedelta(hours=5), purchases=2)
    d = policy.decide(bought, ctx(first.action.send_at + timedelta(days=1)))
    assert d.kind is DecisionKind.NOTHING  # the fake audience drops buyers
    assert runner_of(policy).active("c1") is None
    last = runner_of(policy).history[-1]
    assert (last.kind, last.code) == ("stopped", PURCHASED)


def test_a_purchase_stops_the_step_that_is_due() -> None:
    policy, first = start_b()
    assert first.action is not None
    bought = after_first(first, last_purchase_at=first.action.send_at + timedelta(days=2), purchases=2)
    d = policy.decide(bought, due_ctx(first))
    assert d.action is None or d.action.follow_up_step == 0
    assert PURCHASED in stops(policy)


def test_an_older_purchase_does_not_stop_it() -> None:
    policy, first = start_b()
    old = after_first(first, last_purchase_at=NOW - timedelta(days=90), purchases=1)
    d = policy.decide(old, due_ctx(first))
    assert d.action is not None and d.action.follow_up_step == 1


@pytest.mark.parametrize("channel", [Channel.WHATSAPP, Channel.EMAIL])
def test_a_stop_reply_or_unsubscribe_on_any_channel_stops_the_journey(channel: Channel) -> None:
    policy, first = start_b()
    gone = ChannelStats(address_known=True, consent=True, unsubscribed=True)
    channels = {Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE, channel: gone}
    d = policy.decide(after_first(first, channels=channels), due_ctx(first))
    assert d.action is None or d.action.follow_up_step == 0
    assert runner_of(policy).active("c1") is None
    assert UNSUBSCRIBED in stops(policy)


def test_a_stop_reply_stops_the_journey_before_the_step_is_due() -> None:
    policy, first = start_b()
    assert first.action is not None
    gone = ChannelStats(address_known=True, consent=True, unsubscribed=True)
    channels = {Channel.EMAIL: gone, Channel.WHATSAPP: REACHABLE}
    policy.decide(after_first(first, channels=channels), ctx(first.action.send_at + timedelta(hours=3)))
    assert runner_of(policy).active("c1") is None


def test_an_open_complaint_stops_the_journey() -> None:
    policy, first = start_b()
    d = policy.decide(after_first(first, open_complaint=True), due_ctx(first))
    assert d.kind is DecisionKind.NOTHING
    assert COMPLAINT in stops(policy)


def test_leaving_the_agent_group_ends_the_journey() -> None:
    policy, first = start_b()
    d = policy.decide(after_first(first, group=Group.HOLDOUT), due_ctx(first))
    assert d.kind is DecisionKind.NOTHING and d.blocked_by == (rules.HOLDOUT,)
    assert runner_of(policy).active("c1") is None


def test_the_opened_condition_ends_the_journey_when_the_customer_engaged() -> None:
    cfg = parse_journeys(
        {
            "journeys": [
                {
                    "id": "N",
                    "name": "n",
                    "steps": [
                        {"channel": "email"},
                        {"channel": "whatsapp", "wait_days": 3, "when": "not_opened"},
                    ],
                }
            ]
        }
    )
    policy = make(config=cfg)
    first = policy.decide(state(), ctx())
    assert first.journey_id == "N" and first.action is not None
    clicked = ChannelStats(
        address_known=True, consent=True, clicks=1, last_engaged_at=first.action.send_at + timedelta(hours=1)
    )
    d = policy.decide(
        after_first(first, channels={Channel.EMAIL: clicked, Channel.WHATSAPP: REACHABLE}), due_ctx(first)
    )
    assert d.action is None or d.action.follow_up_step == 0
    assert CONDITION_FAILED in stops(policy)
    # the same journey carries on for a customer who did not click
    policy2 = make(config=cfg)
    first2 = policy2.decide(state(), ctx())
    d2 = policy2.decide(after_first(first2), due_ctx(first2))
    assert d2.action is not None and d2.action.channel is Channel.WHATSAPP


def test_a_step_on_a_channel_the_customer_never_reads_is_skipped() -> None:
    policy, first = start_b()
    deaf = ChannelStats(address_known=True, consent=True, sends=6, delivers=6)
    d = policy.decide(
        after_first(first, channels={Channel.EMAIL: REACHABLE, Channel.WHATSAPP: deaf}), due_ctx(first)
    )
    assert d.action is None or d.action.journey_id is None or d.action.channel is not Channel.WHATSAPP
    events = [(e.kind, e.code) for e in runner_of(policy).history]
    assert ("skipped", NEVER_READ) in events and events[-1][0] == "completed"
    assert runner_of(policy).active("c1") is None


def test_cancelled_decisions_end_their_journey() -> None:
    policy, first = start_b()
    policy.forget_pending([first.decision_id])
    assert runner_of(policy).active("c1") is None


# --- each step re-checks the hard rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stats", "code"),
    [
        (ChannelStats(address_known=True, consent=False), rules.NO_CONSENT),
        (ChannelStats(address_known=False, consent=True), rules.NO_ADDRESS),
    ],
)
def test_consent_or_address_lost_before_the_step_skips_it(stats: ChannelStats, code: str) -> None:
    # A bidder that does not filter by reach, so the hard rules themselves are what stop the step.
    policy = AutonomousPolicy(
        [PriceBidder([EMAIL, WHATSAPP], reachable_only=False)],
        None,
        0.0,
        journeys=JourneyRunner(only(["B"])),
    )
    first = policy.decide(state(), ctx())
    assert first.journey_id == "B"
    d = policy.decide(
        after_first(first, channels={Channel.EMAIL: REACHABLE, Channel.WHATSAPP: stats}), due_ctx(first)
    )
    assert d.kind is DecisionKind.NOTHING and d.blocked_by == (code,)
    assert d.journey_id == "B" and "skips" in d.reason
    assert runner_of(policy).active("c1") is None  # that was the last step


def test_a_channel_the_customer_can_no_longer_be_reached_on_ends_the_journey_without_a_send() -> None:
    policy, first = start_b()
    no_consent = ChannelStats(address_known=True, consent=False)
    d = policy.decide(
        after_first(first, channels={Channel.EMAIL: REACHABLE, Channel.WHATSAPP: no_consent}), due_ctx(first)
    )
    assert d.action is None or d.action.channel is not Channel.WHATSAPP
    assert runner_of(policy).active("c1") is None


def test_the_weekly_cap_holds_the_step_until_it_clears_then_it_expires() -> None:
    policy, first = start_b()
    capped = after_first(first, contacts_7d=2)
    c = due_ctx(first)
    held = policy.decide(capped, c)
    assert held.kind is DecisionKind.NOTHING and held.blocked_by == (rules.WEEKLY_CAP,)
    assert "holds" in held.reason
    assert runner_of(policy).active("c1") is not None
    # a day later the week has cleared and the step goes out
    sent = policy.decide(after_first(first, contacts_7d=1), ctx(c.now + timedelta(days=1)))
    assert sent.action is not None and sent.action.channel is Channel.WHATSAPP
    # ... but a step held for longer than max_defer_days ends the journey
    policy2, first2 = start_b()
    late = ctx(first2.action.send_at + timedelta(days=3 + 3))  # type: ignore[union-attr]
    policy2.decide(after_first(first2, contacts_7d=2), late)
    assert runner_of(policy2).active("c1") is None
    assert f"expired:{rules.WEEKLY_CAP}" in stops(policy2)


def test_a_snooze_holds_the_step() -> None:
    policy, first = start_b()
    until = (due_ctx(first).now + timedelta(days=10)).isoformat()
    d = policy.decide(after_first(first, preferences={"snooze_until": until}), due_ctx(first))
    assert d.kind is DecisionKind.NOTHING and d.blocked_by == (rules.SNOOZED,)
    assert runner_of(policy).active("c1") is not None


def test_an_empty_budget_holds_the_step() -> None:
    policy, first = start_b()
    c = due_ctx(first).model_copy(update={"budget_left": {GOAL: 0.0}})
    d = policy.decide(after_first(first), c)
    assert d.blocked_by == (rules.NO_BUDGET,)


def test_the_step_is_timed_outside_quiet_hours_in_the_customers_zone() -> None:
    policy, first = start_b()
    zone = "Asia/Kolkata"
    c = ctx(first.action.send_at + timedelta(days=3, hours=14))  # type: ignore[union-attr]
    d = policy.decide(after_first(first, timezone=zone), c)
    assert d.action is not None and d.action.channel is Channel.WHATSAPP
    envelope = sample_brief(NOW).envelope
    assert rules.quiet_hours(after_first(first, timezone=zone), envelope, d.action.send_at).allowed
    assert d.action.send_at >= c.now


def test_a_step_that_no_longer_pays_is_not_sent() -> None:
    cheap_then_dear = PriceBidderSwitch()
    policy = AutonomousPolicy([cheap_then_dear], None, 0.0, journeys=JourneyRunner(only(["B"])))
    first = policy.decide(state(), ctx())
    assert first.journey_id == "B" and first.action is not None
    cheap_then_dear.dear = True
    d = policy.decide(after_first(first), due_ctx(first))
    assert d.action is None or d.action.follow_up_step == 0
    assert "no_longer_pays" in stops(policy)


class PriceBidderSwitch(PriceBidder):
    """WhatsApp pays on the first call and no longer once `dear` is set."""

    def __init__(self) -> None:
        super().__init__([EMAIL, WHATSAPP])
        self.dear = False

    def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
        out = super().bids(state, ctx)
        if not self.dear:
            return out
        return [
            b if b.action.channel is not Channel.WHATSAPP else bid(Channel.WHATSAPP, 0.0, 0.03) for b in out
        ]


# --- rebuilding from the log ------------------------------------------------------------------------------------


def test_runs_are_rebuilt_from_the_decision_log() -> None:
    policy, first = start_b()
    fresh = JourneyRunner(only(["B"]))
    fresh.rebuild([first])
    run = fresh.active("c1")
    assert run is not None and run.journey_id == "B" and run.next_step == 1
    assert run.probability == first.journey_probability
    second = policy.decide(after_first(first), due_ctx(first))
    fresh.rebuild([second, first])  # order does not matter
    assert fresh.active("c1") is None


def test_mapping_of_conditions_covers_every_journey_step() -> None:
    cfg = load_journeys()
    conditions: Mapping[str, StepCondition] = {j.journey_id: j.steps[-1].when for j in cfg.journeys}
    assert conditions["B"] is StepCondition.NO_PURCHASE and conditions["A"] is StepCondition.ALWAYS
