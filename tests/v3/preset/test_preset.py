"""The v3 preset: every part on together, each part off apart, nothing changed by default, and how they compose."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import pytest

from autonomy.contracts import (
    Action,
    Channel,
    CustomerState,
    Decision,
    DecisionKind,
    Group,
    Reward,
)
from autonomy.decide.frequency import FrequencyPlanner, LookalikeHazard
from autonomy.decide.journeys import JourneyRunner, load_journeys
from autonomy.decide.models import HazardModel
from autonomy.decide.moments import MomentBook
from autonomy.decide.pacing import OfferPricing
from autonomy.decide.who import WhoBook
from autonomy.runtime.agent import (
    AgentPolicy,
    ClickResponseModel,
    LookalikeClickModel,
    build_agent_policy,
)
from autonomy.runtime.v3 import DEFAULT_OFFER_PRICING, V3Preset
from autonomy.scientist.questions import QuestionKind
from tests.v3.experiments.helpers import GOAL, NOW, brief, ctx, customers_in, library, spec, state


def build(**kw: object) -> AgentPolicy:
    return build_agent_policy([brief()], list(library()), **kw)  # type: ignore[arg-type]


def customer(cid: str, now: datetime = NOW, **kw: Any) -> CustomerState:
    """A lapsed customer reachable by e-mail and WhatsApp, valuable enough for every journey."""
    return state(
        cid,
        as_of=now,
        purchases=3,
        revenue=300.0,
        first_seen_at=now - timedelta(days=300),
        last_purchase_at=now - timedelta(days=80),
        **kw,
    )


def decisions(agent: AgentPolicy, n: int = 30) -> list[Decision]:
    return [agent.decide(customer(f"c{i}"), ctx(seed=i)) for i in range(n)]


# --- the switch --------------------------------------------------------------------------------


def test_the_default_agent_has_no_v3_part() -> None:
    agent = build()
    assert type(agent.bundle.response) is ClickResponseModel
    assert agent.who is None and agent.frequency is None and agent.planner is None
    assert agent.moments is None and agent.offer_pricing is None
    assert agent.policy.journeys is None and agent.experiment_policy is None
    assert type(agent.contact_price.hazard) is HazardModel


def test_v3_false_is_the_default() -> None:
    assert decisions(build(v3=False)) == decisions(build())


DEFAULT_FINGERPRINT = (120, 60, "310d8f3efaf90f03")
"""`fingerprint(build())` on the commit the preset was built on (20db0504, the merge of v3/frequency), before
any of the preset's edits to the shared code (the auction, journey options, `_keep`, `learn`)."""


def fingerprint(agent: AgentPolicy) -> tuple[int, int, str]:
    """Four days of 30 decisions, each day's handed to `nightly`, and rewards for the sends of the first two
    days handed to `learn`: (decisions, sends, a digest of every decision's choice, odds and value)."""
    rows: list[tuple[object, ...]] = []
    acts: dict[str, Decision] = {}
    for day in range(4):
        now = NOW + timedelta(days=day)
        made = [agent.decide(customer(f"c{i}", now), ctx(now, seed=i)) for i in range(30)]
        acts.update({d.decision_id: d for d in made if d.kind is DecisionKind.ACT})
        rows += [
            (
                d.decision_id,
                d.kind.value,
                d.action
                and (d.action.channel.value, d.action.content_id, d.action.offer_id, str(d.action.send_at)),
                round(d.probability, 6),
                round(d.expected_net, 6),
                d.policy_version,
                d.blocked_by,
            )
            for d in made
        ]
        agent.nightly(made, {}, now + timedelta(hours=12))
        if day == 1:
            rewards = [
                Reward(
                    decision_id=did,
                    customer_id=d.customer_id,
                    goal_id=GOAL,
                    window_end=now + timedelta(hours=12),
                    converted=i % 3 == 0,
                    revenue=100.0 if i % 3 == 0 else 0.0,
                    margin=40.0 if i % 3 == 0 else 0.0,
                    cost=0.1,
                    net=39.9 if i % 3 == 0 else -0.1,
                    unsubscribed=False,
                    complained=False,
                )
                for i, (did, d) in enumerate(sorted(acts.items()))
            ]
            agent.learn(rewards, acts)
    digest = hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
    return len(rows), sum(1 for r in rows if r[1] == "act"), digest


def test_the_default_agent_decides_and_learns_as_before_the_preset() -> None:
    """The shared code the preset edits must leave the default agent's numbers as they were."""
    assert fingerprint(build()) == DEFAULT_FINGERPRINT


def test_the_preset_turns_every_part_on() -> None:
    agent = build(v3=True)
    assert isinstance(agent, AgentPolicy)  # the experiments' wrapper sits inside it, not around it
    assert isinstance(agent.bundle.response, LookalikeClickModel)
    assert isinstance(agent.who, WhoBook)
    assert all(b.who is agent.who for b in agent.bidders.values())
    assert agent.frequency is not None and isinstance(agent.planner, FrequencyPlanner)
    assert isinstance(agent.contact_price.hazard, LookalikeHazard)
    assert isinstance(agent.moments, MomentBook)
    assert all(b.moments is agent.moments for b in agent.bidders.values())
    runner = agent.policy.journeys
    assert isinstance(runner, JourneyRunner)
    assert [j.journey_id for j in runner.config.journeys] == ["A", "B", "C", "D"]  # the shipped file
    assert runner.fade is not None  # a later step is priced at the urgency left on its day
    assert agent.offer_pricing == DEFAULT_OFFER_PRICING
    assert GOAL in agent.offer_prices and all(b.budget_price is not None for b in agent.bidders.values())
    assert agent.experiment_policy is not None
    assert agent.experiment_policy.book.running() == []  # no experiment is running
    assert agent.experiment_policy.book.all() == []


@pytest.mark.parametrize(
    ("part", "gone"),
    [
        ("lookalike", lambda a: type(a.bundle.response) is ClickResponseModel),
        ("who", lambda a: a.who is None),
        ("frequency", lambda a: a.frequency is None and a.planner is None),
        ("moments", lambda a: a.moments is False),
        ("journeys", lambda a: a.policy.journeys is None),
        ("offers", lambda a: a.offer_pricing is None),
        ("experiments", lambda a: a.experiment_policy is None),
    ],
)
def test_each_part_can_be_switched_off_inside_the_preset(part: str, gone: object) -> None:
    parts = ("lookalike", "who", "frequency", "moments", "journeys", "offers", "experiments")
    alone: dict[str, Any] = {part: False}
    others: dict[str, Any] = {p: False for p in parts if p != part}
    assert gone(build(v3=V3Preset(**alone)))  # type: ignore[operator]
    assert not gone(build(v3=V3Preset(**others)))  # type: ignore[operator]  # and it is the only one left on


def test_a_piece_given_by_name_wins_over_the_preset() -> None:
    off = build(v3=True, moments=False)
    assert off.moments is False and off.who is not None
    runner = JourneyRunner(load_journeys())
    own = build(v3=True, journeys=runner)
    assert own.policy.journeys is runner
    pricing = OfferPricing(days=10, initial=0.2)
    assert build(v3=True, offer_pricing=pricing).offer_pricing is pricing
    # with v3 off, the pieces given by name are used as they are
    only = build(journeys=True)
    assert only.policy.journeys is not None and only.who is None and only.experiment_policy is None


def test_false_given_by_name_switches_a_part_off_inside_the_preset() -> None:
    assert type(build(v3=True, lookalike=False).bundle.response) is ClickResponseModel
    assert build(v3=True, who=False).who is None
    no_frequency = build(v3=True, frequency=False)
    assert no_frequency.frequency is None and no_frequency.planner is None
    no_journeys = build(v3=True, journeys=False)
    assert no_journeys.policy.journeys is None and no_journeys.who is not None


def test_the_shops_calendar_comes_from_the_preset() -> None:
    agent = build(v3=V3Preset(moments_country="IN"))
    assert isinstance(agent.moments, MomentBook) and agent.moments.default_country == "IN"


def test_the_experiments_wrapper_changes_nothing_until_an_experiment_runs() -> None:
    wrapped, bare = build(v3=True), build(v3=V3Preset(experiments=False))
    assert decisions(wrapped) == decisions(bare)


# --- one decision with every part active --------------------------------------------------------


def test_one_decision_with_every_part_active() -> None:
    agent = build(v3=True)
    cart = NOW - timedelta(hours=6)
    s = customer("c0", last_cart_at=cart, scores={"churn_risk": 0.8})
    quiet = customer("c0", scores={"churn_risk": 0.0})
    explained = agent.explain_bids(s, ctx())
    plain = agent.explain_bids(quiet, ctx())

    assert any(
        e.bid.action.journey_id for e in explained
    )  # journeys: the auction weighs plans, not only messages
    assert any("cart" in r for e in explained for r in e.moment_reasons)  # moments: the cart is live
    assert not any(e.moment_reasons for e in plain)
    # who and moments both raise the urgency of the same bid, and the result is their product
    top, base = explained[0].bid, next(e.bid for e in plain if e.bid.action == explained[0].bid.action)
    assert isinstance(agent.moments, MomentBook) and top.urgency == pytest.approx(
        base.urgency * agent.moments.factor(s, NOW) * 1.4, rel=1e-6
    )

    decision = agent.decide(s, ctx())
    assert decision.kind is DecisionKind.ACT and decision.action is not None
    assert agent.planner is not None and "c0" in agent.planner.last  # frequency: the 3-week plan was made
    assert agent.outcomes is not None and len(agent.outcomes) == 1  # lookalike: the send's profile is kept
    assert agent.who is not None and agent.who.coverage()["prepared"] == 1  # who: prepared for this customer
    assert (
        agent.bidders[GOAL].budget_price is agent.offer_prices[GOAL]
    )  # offers: priced by the goal's budget price
    assert decision.policy_version == agent.version  # experiments: wrapper present, nobody assigned

    # ... and with a test running, the same wrapper puts a customer on an arm
    agent.experiment_policy.book.start(  # type: ignore[union-attr]
        spec("exp1", share=0.10), brief(), NOW - timedelta(hours=1)
    )
    cid = next(customers_in(agent.experiment_policy.book.get("exp1").spec, "freeship", 1))  # type: ignore[union-attr]
    tested = agent.decide(customer(cid), ctx())
    assert tested.policy_version.endswith("|exp:exp1:freeship")
    assert tested.action is not None and tested.action.offer_id == "freeship"


# --- composition: the order, and what was fixed ---------------------------------------------------


def test_urgency_is_the_goals_fade_times_moments_times_leaving_risk() -> None:
    """One order: the goal's own urgency, with the live moments inside it, then who's risk factor
    (1 + 0.5 x churn risk). Both multiply the whole net, so a high risk never rescues a losing bid."""
    agent = build(v3=True)
    cart = NOW - timedelta(hours=6)
    # a single message: its net is exactly urgency x (gain - costs); a journey's also holds later steps' prices
    base = next(
        e.bid
        for e in agent.explain_bids(customer("c0", last_cart_at=cart), ctx())
        if e.bid.action.journey_id is None
    )
    risky = next(
        e.bid
        for e in agent.explain_bids(customer("c0", last_cart_at=cart, scores={"churn_risk": 1.0}), ctx())
        if e.bid.action == base.action
    )
    assert risky.urgency == pytest.approx(base.urgency * 1.5, rel=1e-9)
    assert risky.expected_net == pytest.approx(base.expected_net * 1.5, rel=1e-9)
    assert base.urgency > 1.0  # the cart moment is in it


def test_a_journeys_later_step_is_priced_at_the_urgency_left_on_its_day() -> None:
    """A cart left today raises a message today; three days on, when journey B's WhatsApp step runs, the
    moment has closed. Pricing that step at today's urgency (what journeys did) overstated the journey."""
    s = customer("c0", last_cart_at=NOW - timedelta(hours=6))
    faded, flat = build(v3=True), build(v3=True)
    assert flat.policy.journeys is not None
    flat.policy.journeys.fade = None  # the old pricing

    def journey_b(agent: AgentPolicy) -> tuple[float, float]:
        bids = {
            e.bid.action.journey_id: e.bid for e in agent.explain_bids(s, ctx()) if e.bid.action.journey_id
        }
        return bids["B"].expected_net, bids["A"].expected_net

    (b_faded, a_faded), (b_flat, a_flat) = journey_b(faded), journey_b(flat)
    assert a_faded == pytest.approx(a_flat)  # a one-step journey has no later step
    assert b_faded < b_flat  # the closed moment no longer lifts the WhatsApp step
    quiet = customer("c0")
    plain_faded = {e.bid.action.journey_id: e.bid for e in faded.explain_bids(quiet, ctx())}
    plain_flat = {e.bid.action.journey_id: e.bid for e in flat.explain_bids(quiet, ctx())}
    assert plain_faded["B"].expected_net == pytest.approx(
        plain_flat["B"].expected_net
    )  # nothing fades without a moment


def _journey_b_customer(agent: AgentPolicy, ids: object) -> tuple[str, Decision]:
    for cid in ids:  # type: ignore[attr-defined]
        d = agent.decide(customer(cid), ctx())
        if d.journey_id == "B":
            return str(cid), d
    raise AssertionError("no customer was put on journey B")


def test_a_journey_in_progress_is_not_cut_by_a_test_that_starts_under_it() -> None:
    """A channel arm leaves a journey's later channel nothing to offer, which ended the journey at its next
    step and started a new one: the customer was moved by the test in the middle of a plan. A customer
    with a journey running is the agent's until it ends."""
    channel_test = spec("expc", kind=QuestionKind.CHANNEL, arms=("email", "whatsapp"), share=0.10)

    def follow_up(busy: bool) -> tuple[Decision, AgentPolicy]:
        agent = build(v3=True)
        assert agent.experiment_policy is not None
        if not busy:
            agent.experiment_policy._busy = None  # what the wrapper did before it knew about journeys
        cid, _first = _journey_b_customer(agent, customers_in(channel_test, "email", 40))
        agent.experiment_policy.book.start(channel_test, brief(), NOW + timedelta(hours=1))
        later = NOW + timedelta(days=3)
        return agent.decide(customer(cid, later), ctx(later)), agent

    kept, agent = follow_up(True)
    assert kept.action is not None and kept.action.channel is Channel.WHATSAPP
    assert kept.journey_id == "B" and kept.action.follow_up_step == 1
    assert "exp:" not in kept.policy_version
    assert agent.experiment_policy is not None and agent.experiment_policy.stats["journey_running"] == 1
    cut, _ = follow_up(False)
    assert cut.action is not None and cut.action.channel is Channel.EMAIL  # the WhatsApp step was lost
    assert cut.action.follow_up_step == 0  # and a new first message went out in its place
    assert "exp:expc:email" in cut.policy_version


@pytest.mark.parametrize("ending", ["purchase", "unsubscribe"])
def test_a_customer_whose_journey_ends_on_the_sweep_joins_the_test_at_once(ending: str) -> None:
    """The journey ends on the very sweep the wrapper sees it running (a purchase, a WhatsApp unsubscribe):
    the inner policy then runs a fresh auction with the whole library. That decision is not kept: the
    customer is in the test from this sweep, on the arm the hash gives them, tagged."""
    channel_test = spec("expc", kind=QuestionKind.CHANNEL, arms=("email", "whatsapp"), share=0.10)
    agent = build(v3=True)
    wrapper = agent.experiment_policy
    assert wrapper is not None and agent.policy.journeys is not None
    cid, _first = _journey_b_customer(agent, customers_in(channel_test, "whatsapp", 60))
    wrapper.book.start(channel_test, brief(), NOW + timedelta(hours=1))
    later = NOW + timedelta(days=3)
    after = customer(cid, later)
    if ending == "purchase":
        after = after.model_copy(update={"last_purchase_at": NOW + timedelta(days=1), "purchases": 4})
    else:
        channels = dict(after.channels)
        channels[Channel.WHATSAPP] = channels[Channel.WHATSAPP].model_copy(update={"unsubscribed": True})
        after = after.model_copy(update={"channels": channels})
    d = agent.decide(after, ctx(later))
    assert "exp:expc:whatsapp" in d.policy_version  # in the test, on its arm, tagged
    assert d.action is None or d.action.channel is Channel.WHATSAPP
    code = "purchased" if ending == "purchase" else "unsubscribed"
    assert d.journey_ended == f"B:{code}"  # the journey's end is still on the decision
    assert wrapper.stats["journey_ended"] == 1 and wrapper.stats["journey_running"] == 0
    assert wrapper.stats["assigned:expc:whatsapp"] == 1
    run = agent.policy.journeys.active(cid)
    assert run is None or run.first_decision_id == d.decision_id  # B ended; at most a run this decision began
    # the first attempt left nothing behind: the scheduled send and the look-alike row are the second's
    pending = agent.policy._scheduled.get(cid, {})
    assert (d.decision_id in pending) == (d.action is not None)
    assert agent.outcomes is not None
    kept = agent.outcomes._pending.get(d.decision_id)
    assert kept is None or kept.decision.action == d.action


def test_a_test_does_not_price_a_journey_its_arm_cannot_run() -> None:
    """In a channel test the arm's library is cut before the bidders quote, so a journey with a step on the
    other channel is not offered at all: there is no quote for that step to price."""
    agent = build(v3=True)
    assert agent.experiment_policy is not None
    channel_test = spec("expc", kind=QuestionKind.CHANNEL, arms=("email", "whatsapp"), share=0.10)
    agent.experiment_policy.book.start(channel_test, brief(), NOW - timedelta(hours=1))
    seen: set[str | None] = set()
    for cid in customers_in(channel_test, "email", 12):
        d = agent.decide(customer(cid), ctx())
        assert d.action is None or d.action.channel is Channel.EMAIL
        seen.add(d.journey_id)
    assert "B" not in seen and "D" not in seen  # they run on WhatsApp too
    for cid in customers_in(channel_test, "whatsapp", 8):
        d = agent.decide(customer(cid), ctx())
        assert d.action is None or (d.action.channel is Channel.WHATSAPP and d.journey_id in (None, "C"))


def test_a_rebuilt_policy_takes_its_journeys_back_from_the_log() -> None:
    first = build(v3=True)
    cid, started = _journey_b_customer(first, (f"c{i}" for i in range(40)))
    fresh = build(v3=True)
    assert fresh.policy.journeys is not None and fresh.policy.journeys.active(cid) is None
    fresh.nightly([started], {}, NOW)  # the runtime's replay of the decision log
    run = fresh.policy.journeys.active(cid)
    assert run is not None and run.journey_id == "B"
    waiting = fresh.decide(customer(cid, NOW + timedelta(hours=5)), ctx(NOW + timedelta(hours=5)))
    assert (
        waiting.kind is DecisionKind.NOTHING and waiting.journey_id == "B"
    )  # held, not sent a second first message


def test_a_journeys_later_step_is_a_message_but_not_a_fresh_draw_for_the_models() -> None:
    agent = build(v3=True)
    first = Action(goal_id=GOAL, channel=Channel.EMAIL, content_id="email_none", send_at=NOW)
    later = Action(
        goal_id=GOAL,
        channel=Channel.WHATSAPP,
        content_id="whatsapp_none",
        send_at=NOW + timedelta(days=3),
        journey_id="B",
        follow_up_step=1,
    )
    made = [
        Decision(
            decision_id=did,
            customer_id="c9",
            made_at=at,
            kind=DecisionKind.ACT,
            action=a,
            group=Group.AGENT,
            probability=p,
            reason="-",
            policy_version="v",
        )
        for did, at, a, p in (("d1", NOW, first, 0.5), ("d2", NOW + timedelta(days=3), later, 1.0))
    ]
    for d in made:
        agent._keep(d)
    assert set(agent._sends) == {"d1", "d2"}  # the breaker and the tiredness curves see both messages
    assert set(agent._log) == {"d1"} and set(agent._send_keys) == {"d1"}  # the response models learn from one
    assert agent.outcomes is not None and len(agent.outcomes) == 0
    agent._remember(customer("c9"), made[1])
    assert len(agent.outcomes) == 0  # a later step is not a treatment the look-alike model learns from
    agent._remember(customer("c9"), made[0])
    assert len(agent.outcomes) == 1
    # nor through `Policy.learn`, which the Gym calls every day with the ledger's rewards for every send
    rewards = [
        Reward(
            decision_id=d.decision_id,
            customer_id="c9",
            goal_id=GOAL,
            window_end=NOW + timedelta(days=14),
            converted=True,
            revenue=100.0,
            margin=40.0,
            cost=0.1,
            net=39.9,
            unsubscribed=False,
            complained=False,
        )
        for d in made
    ]
    agent.learn(rewards, {d.decision_id: d for d in made})
    assert "d1" in agent.bundle.learned_decisions and "d2" not in agent.bundle.learned_decisions


def test_the_offer_price_follows_a_budget_that_moves_and_renews_when_its_plan_has_run() -> None:
    agent = build(v3=True)
    agent.decide(customer("c0"), ctx())
    price = agent.offer_prices[GOAL]
    assert price.budget == 5000
    moved = brief().model_copy(update={"version": 2, "brief_id": "brief_goal_exp_2"})
    moved = moved.model_copy(update={"envelope": moved.envelope.model_copy(update={"budget": 3000.0})})
    agent.decide(customer("c1"), ctx().model_copy(update={"briefs": (moved,)}))
    assert price.budget == 3000.0  # an auto-budget move arrives as a new brief version; the price takes it
    # a plan of 30 days that has run: with renew the price is not pinned at the ceiling for good
    for day in range(1, 32):
        agent.nightly([], {}, NOW + timedelta(days=day))
    assert price.price < 1000.0 and price.days_left > 0
    plain = build(v3=V3Preset(offers=OfferPricing(initial=0.0)))  # renew off, as the module default
    plain.decide(customer("c0"), ctx())
    for day in range(1, 32):
        plain.nightly([], {}, NOW + timedelta(days=day))
    assert plain.offer_prices[GOAL].price == 1000.0


def test_a_journeys_later_step_is_not_vetoed_by_the_weekly_rhythm() -> None:
    """The weekly rhythm is one message a week for most customers, so it held every step that fell in the
    week of the one before it until the step expired, while the journey had been priced as if it went out
    (a Gym run of 200 customers x 35 days: 124 journeys B started, none of their WhatsApp steps sent)."""

    def second_message(follow_rhythm: bool) -> Decision:
        agent = build(v3=True)
        assert agent.policy.journeys is not None
        agent.policy.journeys.follow_rhythm = follow_rhythm
        agent.policy.budget_fn = lambda st, bids, cost: 1  # a customer whose best rhythm is one a week
        cid, _first = _journey_b_customer(agent, (f"c{i}" for i in range(40)))
        later = NOW + timedelta(days=3)
        return agent.decide(customer(cid, later), ctx(later))

    assert build(v3=True).policy.journeys.follow_rhythm is False  # type: ignore[union-attr]
    sent = second_message(False)
    assert (
        sent.action is not None
        and sent.action.channel is Channel.WHATSAPP
        and sent.action.follow_up_step == 1
    )
    held = second_message(True)  # the runner's own behaviour, as the journeys stream built it
    assert held.kind is DecisionKind.NOTHING and held.blocked_by == ("weekly_cap",)


def test_a_journey_that_cannot_fit_the_weekly_cap_is_not_offered() -> None:
    shipped = {j.journey_id: j.most_in_a_week() for j in load_journeys().journeys}
    assert shipped == {"A": 1, "B": 2, "C": 1, "D": 2}

    def offered(cap: int) -> set[str | None]:
        b = brief()
        b = b.model_copy(update={"envelope": b.envelope.model_copy(update={"max_contacts_per_week": cap})})
        agent = build_agent_policy([b], list(library()), v3=True)
        context = ctx().model_copy(update={"briefs": (b,)})
        return {e.bid.action.journey_id for e in agent.explain_bids(customer("c0"), context)} - {None}

    assert offered(1) <= {"A", "C"}  # B and D send two messages inside a week
    assert {"A", "B"} <= offered(2)
