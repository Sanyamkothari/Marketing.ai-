"""Reasons a marketer can read for sends that are not the favourite, and the storm's guard.

Thompson sampling and the exploration floor send options that are not the best estimate; their
reasons now say what is being tested, in the terms that differ ("testing whether WhatsApp beats
email for them"), with the point inside the Live screen's one-line cut. The favourite is ranked
after each channel's own attention price, as the auction ranks it. The storm tests pin what keeps
`complaint_storm` safe: the agent's own breaker trips on its own sends' complaint rate at once in a
real storm, and not on a rate near the limit.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

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
)
from autonomy.decide import explain
from autonomy.decide.policy import AutonomousPolicy
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_brief, sample_content
from autonomy.runtime.agent import Breaker

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
GOAL = "goal_winback"
CONTENT = {c.content_id: c for c in sample_content(NOW)}
OFFERS = {o.offer_id: o for o in (TEN_OFF, FREE_SHIP)}
REACHABLE = ChannelStats(address_known=True, consent=True)
# ci_0 email reminder, ci_1 email savings (10% off), ci_2 email how-to, ci_3 WhatsApp reminder,
# ci_4 WhatsApp savings (free shipping): the sample library.


def bid(
    net: float,
    channel: Channel = Channel.EMAIL,
    offer: str = "none",
    content: str = "ci_0",
    sd: float = 0.0,
    goal: str = GOAL,
) -> Bid:
    return Bid(
        goal_id=goal,
        action=Action(goal_id=goal, channel=channel, offer_id=offer, content_id=content, send_at=NOW),
        uplift=0.05,
        value_if_converted=100.0,
        channel_cost=0.001 if channel is Channel.EMAIL else 0.03,
        expected_net=net,
        uncertainty=sd,
    )


def sent(chosen: Bid, price: float = 0.0) -> Decision:
    return Decision(
        decision_id="d1",
        customer_id="cust_kwame_42",
        made_at=NOW,
        kind=DecisionKind.ACT,
        action=chosen.action,
        probability=0.3,
        expected_net=chosen.expected_net,
        attention_price=price,
        reason="-",
        policy_version="t",
    )


def why(text: str) -> str:
    """What the Live screen shows (live.js `shortReason`): after the first ": " up to the first
    clause break, at most 70 characters."""
    colon = text.find(": ")
    rest = text[colon + 2 :] if 0 < colon < 80 else text
    match = re.match(r"^(.*?)(?:[,;]\s|\.\s|\.$|$)", rest)
    return match.group(1) if match else rest


def say(chosen: Bid, bids: list[Bid], **kw: object) -> str:
    return explain.reason(sent(chosen), bids, content=CONTENT, offers=OFFERS, **kw)  # type: ignore[arg-type]


# --- reasons ---------------------------------------------------------------------------------------


def test_a_channel_test_names_both_channels_and_what_we_know_about_the_person() -> None:
    favourite, chosen = bid(0.60), bid(0.40, Channel.WHATSAPP, content="ci_3")
    first = say(chosen, [favourite, chosen], channels={Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE})
    assert why(first) == "testing whether WhatsApp beats email for them"
    assert "no WhatsApp messages to them yet" in first
    assert "$0.40 expected after all costs, against $0.60 for the current favourite" in first
    seen = {Channel.WHATSAPP: REACHABLE.model_copy(update={"sends": 5, "clicks": 2})}
    assert "2 clicks from 5 WhatsApp messages so far" in say(chosen, [favourite, chosen], channels=seen)
    quiet = {Channel.WHATSAPP: REACHABLE.model_copy(update={"sends": 1})}
    assert "no clicks yet from 1 WhatsApp message;" in say(chosen, [favourite, chosen], channels=quiet)
    assert "uncertain" not in first and "close alternative" not in first


def test_offer_theme_and_fallback_tests_read_as_what_is_being_learned() -> None:
    plain = bid(0.60)
    discount = bid(0.40, offer="pct10", content="ci_1")
    assert why(say(discount, [plain, discount])) == "testing whether 10% off wins them over"
    assert why(say(plain, [discount.model_copy(update={"expected_net": 0.9}), plain])) == (
        "testing whether they buy without 10% off"
    )
    howto = bid(0.30, content="ci_2")
    assert why(say(howto, [plain, howto])) == "testing whether a how-to message beats a reminder for them"
    other_goal = bid(0.30, goal="goal_refill")
    assert why(say(other_goal, [plain, other_goal])) == (
        "not enough evidence yet on which message works best for them"
    )


def test_an_exploration_pick_says_it_is_a_routine_test() -> None:
    favourite, chosen = bid(0.60), bid(-0.20, Channel.WHATSAPP, content="ci_3")
    text = say(chosen, [favourite, chosen], explored=True)
    assert text == (
        "Sent a reminder by WhatsApp with no discount: a routine test of WhatsApp instead of email, "
        "so every option keeps getting tested."
    )
    assert why(text) == "a routine test of WhatsApp instead of email"


def test_the_favourite_is_ranked_after_each_channels_own_price() -> None:
    # WhatsApp's estimate is higher, but its attention price is too: email is the auction's best,
    # so the email send is the favourite, not "a close alternative".
    whatsapp, email = bid(1.00, Channel.WHATSAPP, content="ci_3"), bid(0.70)
    prices = {Channel.WHATSAPP: 0.50, Channel.EMAIL: 0.05}
    text = say(email, [whatsapp, email], prices=prices, base_price=0.05)
    assert why(text) == "expected to add $0.70 after costs"
    assert "testing" not in text
    # Without the prices every bid pays one price and the dearer WhatsApp option looks better.
    assert why(say(email, [whatsapp, email], base_price=0.05)).startswith(
        "testing whether email beats WhatsApp"
    )


def test_left_alone_by_the_draw_reads_as_a_test_of_resting() -> None:
    rest = Decision(
        decision_id="d2",
        customer_id="cust_maya_7",
        made_at=NOW,
        kind=DecisionKind.NOTHING,
        probability=0.4,
        attention_price=0.10,
        reason="-",
        policy_version="t",
    )
    text = explain.reason(rest, [bid(0.30, sd=0.5)])
    assert text == (
        "Left alone this time: testing whether a rest beats a message for them; the best message "
        "would add about $0.20 after costs."
    )
    assert why(text) == "testing whether a rest beats a message for them"
    # per-channel prices: the WhatsApp bid pays its own, dearer price
    priced = explain.reason(
        rest, [bid(0.30, Channel.WHATSAPP, content="ci_3")], prices={Channel.WHATSAPP: 0.40}
    )
    assert priced == "Left alone: no message is worth more than the cost of contacting (best $0.30 vs $0.40)."


def test_every_test_phrase_fits_the_one_line_cut() -> None:
    nouns = [f"{'an' if n[0] in 'aeiou' else 'a'} {n}" for n in explain._THEME_NOUN.values()]
    for mine in nouns:
        for theirs in nouns:
            phrase = explain._testing(("theme", mine, theirs))
            assert len(phrase) <= explain.SHORT_WHY and "," not in phrase and ";" not in phrase


def test_sent_reasons_from_the_policy_are_deterministic_and_never_carry_the_customer() -> None:
    bids = [
        bid(0.5, sd=0.6),
        bid(0.45, Channel.WHATSAPP, content="ci_3", sd=0.6),
        bid(0.4, content="ci_2", sd=0.6),
    ]

    class Fixed:
        goal_id = GOAL

        def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
            return [
                b.model_copy(update={"action": b.action.model_copy(update={"send_at": ctx.now})})
                for b in bids
            ]

    def run() -> list[Decision]:
        p = AutonomousPolicy([Fixed()], None, 0.1, mc_draws=200)
        ctx = DecisionContext(
            now=NOW, briefs=(sample_brief(NOW),), content=tuple(sample_content(NOW)), seed=5
        )
        return [
            p.decide(
                CustomerState(
                    customer_id=f"kwame_{i}",
                    as_of=NOW,
                    group=Group.AGENT,
                    channels={Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE},
                ),
                ctx,
            )
            for i in range(120)
        ]

    first, again = run(), run()
    assert [d.reason for d in first] == [d.reason for d in again]
    acted = [d for d in first if d.kind is DecisionKind.ACT]
    tests = [d for d in acted if why(d.reason).startswith(("testing whether", "a routine test"))]
    assert tests and len(tests) < len(acted)
    for d in first:
        assert "kwame" not in d.reason and "uncertain" not in d.reason
        assert len(why(d.reason)) <= explain.SHORT_WHY


# --- the storm's guard -----------------------------------------------------------------------------


def _outcomes(breaker: Breaker, kind: EventType, n: int, at: datetime) -> None:
    for i in range(n):
        e = Event(
            event_id=f"{kind.value}_{at.isoformat()}_{i}",
            customer_id=f"c{i}",
            type=kind,
            ts=at,
            source="test",
            channel=Channel.EMAIL,
            decision_id=f"d{i}",
        )
        breaker.record(e, GOAL, Channel.EMAIL)


def test_the_agents_breaker_stops_a_real_storm_on_its_own_first_batch() -> None:
    brief = sample_brief(NOW)  # the default 0.08% complaint limit
    storm = Breaker()
    _outcomes(storm, EventType.DELIVER, 450, NOW - timedelta(hours=20))
    _outcomes(storm, EventType.COMPLAINT, 5, NOW - timedelta(hours=10))  # 1.1%, as complaint_storm's sends
    assert storm.check([brief], NOW) and GOAL in storm.paused_goals
    calm = Breaker()
    for day in range(7):  # 1,200 messages a week at about 0.06%: near the limit, not a storm
        _outcomes(calm, EventType.DELIVER, 170, NOW - timedelta(days=day, hours=2))
    _outcomes(calm, EventType.COMPLAINT, 1, NOW - timedelta(days=3))
    assert calm.check([brief], NOW) == [] and not calm.paused_goals


def test_a_send_when_resting_was_the_favourite_never_claims_it_beat_the_cost() -> None:
    # One option, worth $0.20 against a $0.30 price: doing nothing had the best estimate, so a
    # Thompson draw (or the floor) that sends it is a test of messaging at all.
    chosen = bid(0.20, sd=1.0)
    drawn = explain.reason(sent(chosen, 0.30), [chosen], content=CONTENT, offers=OFFERS, base_price=0.30)
    assert why(drawn) == "testing whether a message beats a rest for them"
    assert "-$0.10 expected after all costs, against $0.00 for a rest" in drawn
    assert "more than the" not in drawn
    floor = explain.reason(
        sent(chosen, 0.30), [chosen], content=CONTENT, offers=OFFERS, base_price=0.30, explored=True
    )
    assert why(floor) == "a routine test of a message instead of a rest"
    # With a better option that does pay, the comparison is with that favourite, not with a rest.
    better = bid(0.90, Channel.WHATSAPP, content="ci_3")
    text = say(chosen, [better, chosen], base_price=0.30)
    assert why(text) == "testing whether email beats WhatsApp for them"


def test_a_rest_held_by_restraint_is_not_called_a_test() -> None:
    rest = Decision(
        decision_id="d3",
        customer_id="cust_maya_7",
        made_at=NOW,
        kind=DecisionKind.NOTHING,
        probability=1.0,
        attention_price=0.10,
        reason="-",
        policy_version="t",
    )
    text = explain.reason(rest, [bid(0.30, sd=0.5)], restrained=True)
    assert why(text) == "not sure enough yet that a message pays off for them"
    assert "testing" not in text and "$0.20" in text


def test_the_policy_says_when_restraint_held_the_best_option() -> None:
    held = bid(0.30, sd=0.5)

    class One:
        goal_id = GOAL

        def bids(self, state: CustomerState, ctx: DecisionContext) -> list[Bid]:
            return [held.model_copy(update={"action": held.action.model_copy(update={"send_at": ctx.now})})]

    p = AutonomousPolicy([One()], None, 0.0, margin_fn=lambda s, b: 2.0 * b.uncertainty)
    ctx = DecisionContext(now=NOW, briefs=(sample_brief(NOW),), content=tuple(sample_content(NOW)), seed=5)
    d = p.decide(
        CustomerState(customer_id="c1", as_of=NOW, group=Group.AGENT, channels={Channel.EMAIL: REACHABLE}),
        ctx,
    )
    assert d.kind is DecisionKind.NOTHING
    assert why(d.reason) == "not sure enough yet that a message pays off for them"
