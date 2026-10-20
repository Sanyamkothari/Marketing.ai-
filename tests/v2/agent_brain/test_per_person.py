"""Per-person learning: shrinkage, empirical-Bayes strengths, CUPED, history warm start, determinism.

Pure maths and tiny synthetic data; one small Gym run for determinism. Each test runs in well under
a second except the Gym one (a few seconds).
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import (
    Channel,
    ChannelStats,
    CustomerState,
    DecisionContext,
    Event,
    EventType,
    Group,
    stable_id,
)
from autonomy.decide.bidders import GoalBidder
from autonomy.decide.models import (
    PersonalEffects,
    cuped_adjust,
    fit_gamma_strength,
    product_moments,
    shrink_to_pool,
)
from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym.score import run_policy
from autonomy.learning.observations import ipw_weights, logged_sends, weighted_rate
from autonomy.runtime.agent import (
    ClickResponseModel,
    LiftPerClick,
    ReadingHours,
    _poisson_tail,
    build_agent_policy,
)

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a Monday, 10:00 UTC
REACHABLE = ChannelStats(address_known=True, consent=True)
KEY = "email|pct10|savings"
PLAIN = "email|none|reminder"


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
        "segment": "light",
    }
    base.update(kw)
    return CustomerState.model_validate(base)


# --- shrinkage ------------------------------------------------------------------------------------


def test_few_observations_stay_at_the_segment_many_reach_their_own_rate() -> None:
    people = PersonalEffects(overall_strength=4.0, feature_strength=8.0)
    # A customer who clicks on everything, at five times what their segment predicts (0.1 a send).
    people.observe("few", PLAIN, expected=0.1, clicks=0.0)
    people.observe("few", PLAIN, expected=0.1, clicks=1.0)
    for i in range(300):
        people.observe("many", PLAIN, expected=0.1, clicks=1.0 if i % 2 == 0 else 0.0)
    few, _ = people.factor("few", PLAIN)
    many, many_var = people.factor("many", PLAIN)
    assert 1.0 < few < 1.5  # one click in two sends: far short of their own five-fold rate
    assert many == pytest.approx(5.0, rel=0.02)  # 150 clicks on 30 expected: their own rate
    assert many_var < 1.0
    # most of it is their overall level, so it carries over to actions they have not had yet
    assert people.overall("many")[0] > 4.0
    # nobody known: exactly the segment, with the prior's spread
    stranger, stranger_var = people.overall("stranger")
    assert stranger == 1.0
    assert stranger_var == pytest.approx(0.25)


def test_learning_on_one_offer_informs_the_same_offer_on_another_channel() -> None:
    people = PersonalEffects(overall_strength=4.0, feature_strength=4.0)
    for _ in range(20):
        people.observe("bargain", "email|pct10|savings", expected=0.1, clicks=0.5)
        people.observe("bargain", "email|none|reminder", expected=0.1, clicks=0.05)
    offer_elsewhere, _ = people.factor("bargain", "whatsapp|pct10|savings")
    plain_elsewhere, _ = people.factor("bargain", "whatsapp|none|reminder")
    assert offer_elsewhere > 2 * plain_elsewhere


def test_fitted_strength_is_low_when_people_differ_and_high_when_they_do_not() -> None:
    rng = np.random.default_rng(7)
    expected = 3.0
    alike = [(float(rng.poisson(expected)), expected) for _ in range(2000)]
    differ = [(float(rng.poisson(expected * rng.gamma(2.0, 0.5))), expected) for _ in range(2000)]
    assert fit_gamma_strength(alike, default=3.0) >= 24.0
    assert 1.0 <= fit_gamma_strength(differ, default=3.0) <= 3.0
    assert fit_gamma_strength(differ[:10], default=3.0) == 3.0  # too few units: the default


def test_refit_learns_how_much_customers_differ() -> None:
    rng = np.random.default_rng(3)
    people = PersonalEffects()
    for i in range(600):
        rho = 0.05 if i % 4 == 0 else 1.3  # a quarter never respond (sleeping dogs, the uninterested)
        for _ in range(20):
            people.observe(f"c{i}", PLAIN, expected=0.15, clicks=float(rng.random() < 0.15 * rho))
    people.refit()
    # the true multipliers (0.05 for a quarter, 1.3 for the rest) have variance 0.29: strength 3.4
    assert people.overall_strength == pytest.approx(3.4, rel=0.25)
    dog, _ = people.overall("c0")
    keen, _ = people.overall("c1")
    assert dog < 0.6 < 1.0 < keen


def test_product_moments() -> None:
    m, v = product_moments([(2.0, 0.5), (3.0, 1.0)])
    assert m == 6.0
    assert v == pytest.approx((4 + 0.5) * (9 + 1) - 36)


# --- calibration: CUPED and per-segment shrinkage -------------------------------------------------


def test_cuped_keeps_the_mean_and_cuts_the_variance() -> None:
    rng = np.random.default_rng(11)
    x = rng.gamma(2.0, 1.0, 5000)  # pre-period purchase rate
    y = rng.poisson(3.0 * x) + 0.0  # purchases since: driven by the same habit
    adj = cuped_adjust(x.tolist(), y.tolist())
    assert statistics.fmean(adj) == pytest.approx(float(y.mean()), rel=1e-9)
    assert statistics.pvariance(adj) < 0.5 * float(y.var())


def test_lift_per_click_with_cuped_is_tighter_and_still_unbiased() -> None:
    rng = np.random.default_rng(5)
    estimates: dict[bool, list[float]] = {True: [], False: []}
    spreads: dict[bool, list[float]] = {True: [], False: []}
    for rep in range(40):
        for use_cuped in (True, False):
            lift = LiftPerClick(prior=(0.0, 1.0), strength=1e-6)
            local = np.random.default_rng(rep)
            for i in range(600):
                g = Group.HOLDOUT if i % 6 == 0 else Group.AGENT
                habit = float(local.gamma(1.5, 1.0))
                lift.member(f"c{i}", g, NOW, pre_rate=habit if use_cuped else None)
                bought = int(local.poisson(2.0 * habit + (0.5 if g is Group.AGENT else 0.0)))
                for _ in range(bought):
                    lift.purchase(f"c{i}", NOW + timedelta(days=3))
            for _ in range(500):  # 500 clicks; truth: 0.5 x 500 agent customers / 500 = 0.5 per click
                lift.click(NOW)
            k, sd = lift.estimate(NOW + timedelta(days=30))
            estimates[use_cuped].append(k)
            spreads[use_cuped].append(sd)
    del rng
    assert statistics.fmean(estimates[True]) == pytest.approx(0.5, abs=0.06)
    # purchases ~ Poisson(2 x habit): the habit is two thirds of the variance, and CUPED removes it
    assert statistics.fmean(spreads[True]) < 0.7 * statistics.fmean(spreads[False])
    assert statistics.stdev(estimates[True]) < statistics.stdev(estimates[False])


def test_shrink_to_pool_trusts_precise_groups_and_pools_noisy_ones() -> None:
    noisy = shrink_to_pool({"a": (0.9, 0.5), "b": (0.1, 0.5), "c": (0.3, 0.5)}, pooled=0.3)
    assert all(v[0] == pytest.approx(0.3) for v in noisy.values())  # spread is no more than noise
    precise = shrink_to_pool({"a": (0.9, 0.01), "b": (0.1, 0.01), "c": (0.3, 0.01)}, pooled=0.3)
    assert precise["a"][0] == pytest.approx(0.9, abs=0.01)
    assert precise["b"][0] == pytest.approx(0.1, abs=0.01)


# --- warm start from logged history -----------------------------------------------------------------


def _ev(kind: EventType, cid: str, ts: datetime, did: str | None, **attrs: object) -> Event:
    return Event(
        event_id=stable_id("t", kind.value, cid, ts.isoformat(), did or ""),
        customer_id=cid,
        type=kind,
        ts=ts,
        source="test",
        channel=Channel.EMAIL,
        decision_id=did,
        attrs=attrs,  # type: ignore[arg-type]
    )


def _history(n: int = 400) -> dict[str, list[Event]]:
    """Past campaigns: keen customers (half click) were sent the offer with probability 0.9, the
    rest (a tenth click) with probability 0.1, and every send logged its probability. Each group is
    half the customers, so the segment's true click rate on the offer is 0.3; the naive rate over
    what was sent is about 0.46."""
    rng = np.random.default_rng(1)
    out: dict[str, list[Event]] = {}
    for i in range(n):
        cid = f"c{i}"
        keen = i % 2 == 0
        p = 0.9 if keen else 0.1
        evs = [
            _ev(EventType.SIGNUP, cid, NOW - timedelta(days=300), None),
        ]
        for w in range(10):
            if rng.random() >= p:
                continue
            at = NOW - timedelta(days=70 - 7 * w)
            did = f"hist_{cid}_{w}"
            evs.append(
                _ev(
                    EventType.SEND,
                    cid,
                    at,
                    did,
                    offer_id="pct10",
                    content_id="gym_email_pct10_savings",
                    logged_probability=p,
                )
            )
            evs.append(_ev(EventType.DELIVER, cid, at + timedelta(minutes=1), did))
            if rng.random() < (0.5 if keen else 0.1):
                evs.append(_ev(EventType.CLICK, cid, at + timedelta(hours=5), did))
        out[cid] = evs
    return out


def test_logged_sends_reads_propensities_and_clicks_with_or_without_ids() -> None:
    t0 = NOW - timedelta(days=10)
    evs = {
        "a": [
            _ev(EventType.SEND, "a", t0, "d1", offer_id="none", logged_probability=0.25),
            _ev(EventType.DELIVER, "a", t0 + timedelta(minutes=1), "d1"),
            _ev(EventType.CLICK, "a", t0 + timedelta(hours=1), "d1"),
        ],
        "b": [  # no decision ids: the click is matched to the send before it on the same channel
            _ev(EventType.SEND, "b", t0, None, offer_id="pct10"),
            _ev(EventType.CLICK, "b", t0 + timedelta(hours=30), None),
            _ev(EventType.SEND, "b", t0 + timedelta(days=5), None, offer_id="none"),
        ],
    }
    sends = logged_sends(evs, {c.content_id: c for c in default_content()})
    assert [(s.customer_id, s.offer_id, s.propensity, s.clicked) for s in sends] == [
        ("a", "none", 0.25, True),
        ("b", "pct10", None, True),
        ("b", "none", None, False),
    ]
    assert ipw_weights([0.25, None, 0.01]) == [4.0, 1.0, 20.0]


def test_weighted_rate_undoes_the_targeting() -> None:
    rate, n_eff = weighted_rate([True, False, False, True], [1.0, 9.0, 9.0, 1.0])
    assert rate == pytest.approx(0.1)
    assert n_eff == pytest.approx(400 / 164)


def test_warm_start_uses_logged_propensities_before_day_one() -> None:
    history = _history()
    agent = build_agent_policy([default_brief()], default_content())
    agent.nightly([], history, NOW)
    states = [
        state(
            customer_id=f"c{i}",
            first_seen_at=NOW - timedelta(days=300),
            last_purchase_at=NOW - timedelta(days=50),
            purchases=1,
        )
        for i in range(400)
    ]
    ctx = DecisionContext(now=NOW, briefs=(default_brief(),), content=tuple(default_content()), seed=0)
    agent.prepare_day(states, ctx)
    model = agent.bundle.response
    assert isinstance(model, ClickResponseModel)
    assert agent.history_used > 0
    sent = logged_sends(history, agent.content_by_id)
    naive = sum(s.clicked for s in sent) / len(sent)
    learned = model.click_rate("*", "email|pct10|savings").mean
    assert naive > 0.4
    assert learned == pytest.approx(0.3, abs=0.05)  # the segment's rate, not the targeted few's
    # each person's own record is warm too: the keen clicker is above their segment, the other below
    sends_of: dict[str, list[bool]] = {}
    for x in sent:
        sends_of.setdefault(x.customer_id, []).append(x.clicked)
    keen_id = next(c for c, ys in sends_of.items() if sum(ys) >= 4)
    cold_id = next(c for c, ys in sends_of.items() if len(ys) >= 3 and not any(ys))
    keen, _ = model.personal(keen_id, "email|pct10|savings")
    cold, _ = model.personal(cold_id, "email|pct10|savings")
    assert keen > 1.0 > cold
    # and the late clicks gave reading hours (clicks five hours after a 10:00 UTC send: 15:00)
    assert agent.reading.known() > 50


def test_reading_hours_fall_back_to_the_closest_hour_for_the_segment() -> None:
    hours = ReadingHours()
    assert hours.closest_hour("light", list(range(9, 21))) is None
    for i in range(30):
        hours.observe(f"a{i}", 19, "light")
    for i in range(10):
        hours.observe(f"b{i}", 9, "heavy")
    assert hours.closest_hour("light", list(range(9, 21))) == 19
    # ten heavy buyers read at 09:00: their segment moves towards it, held back by the business's 19:00
    pulled = hours.closest_hour("heavy", list(range(9, 21)))
    assert pulled is not None and pulled <= 12
    for i in range(10, 200):
        hours.observe(f"b{i}", 9, "heavy")
    assert hours.closest_hour("heavy", list(range(9, 21))) in (9, 10)


# --- the bidder ---------------------------------------------------------------------------------------


def test_prune_drops_options_that_cannot_be_the_best() -> None:
    model = ClickResponseModel()
    model.lift = (0.3, 0.0)
    model.organic = lambda _s: 0.5  # buys every other week anyway: a discount mostly pays for that
    for i in range(3000):
        for key in (PLAIN, KEY, "email|freeship|convenience"):
            model.observe_send(f"x{i}", "light", key)
        if i % 4 == 0:
            model.observe_click(f"x{i}", "light", PLAIN)
    ctx = DecisionContext(now=NOW, briefs=(default_brief(),), content=tuple(default_content()), seed=0)
    s = state(channels={Channel.EMAIL: REACHABLE})
    full = GoalBidder(default_brief(), None, model, own_spread=True).bids(s, ctx)
    pruned = GoalBidder(default_brief(), None, model, own_spread=True, prune_z=2.0).bids(s, ctx)
    assert {b.action.offer_id for b in full} == {"none", "pct10", "freeship"}
    assert {b.action.offer_id for b in pruned} == {"none"}
    assert pruned[0].action == full[0].action


def test_prune_never_leaves_only_offers_the_rules_will_block() -> None:
    model = ClickResponseModel()
    model.lift = (0.3, 0.0)
    for i in range(3000):
        for key in (PLAIN, KEY):
            model.observe_send(f"x{i}", "light", key)
        if i % 2 == 0:
            model.observe_click(f"x{i}", "light", KEY)
        if i % 20 == 0:
            model.observe_click(f"x{i}", "light", PLAIN)
    ctx = DecisionContext(now=NOW, briefs=(default_brief(),), content=tuple(default_content()), seed=0)
    bidder = GoalBidder(default_brief(), None, model, own_spread=True, prune_z=2.0)
    keen = state(channels={Channel.EMAIL: REACHABLE})
    assert "none" not in {b.action.offer_id for b in bidder.bids(keen, ctx)}  # 10% off clearly wins
    # but someone who asked for no discounts still gets the plain message
    refuses = state(channels={Channel.EMAIL: REACHABLE}, preferences={"no_discounts": "yes"})
    assert "none" in {b.action.offer_id for b in bidder.bids(refuses, ctx)}


# --- the agent ------------------------------------------------------------------------------------------


def test_cap_is_judged_on_the_week_before_the_planned_send() -> None:
    agent = build_agent_policy([default_brief()], default_content())
    # Two messages a week ago at 16:00 UTC: at the 10:00 sweep they are still inside the week, but
    # a send planned for 18:00 today is judged on the week before 18:00, when they have dropped out.
    week_ago = NOW - timedelta(days=7) + timedelta(hours=6)
    s = state(contacts_7d=2, recent_contacts=(week_ago, week_ago), last_contact_at=week_ago)
    ctx = DecisionContext(now=NOW, briefs=(default_brief(),), content=tuple(default_content()), seed=0)
    assert agent._contacts_at_send(s, ctx, 18) == 0
    assert agent._contacts_at_send(s, ctx, 10) == 2


def test_agent_runs_deterministically_in_a_tiny_gym() -> None:
    spec = get_scenario("smoke", n_customers=200, days=10)

    def factory(world: object) -> object:
        return build_agent_policy([default_brief()], default_content())

    a = run_policy(spec, factory, seed=3, with_oracle=False)  # type: ignore[arg-type]
    b = run_policy(spec, factory, seed=3, with_oracle=False)  # type: ignore[arg-type]
    assert a.true_value == b.true_value
    assert a.contacts == b.contacts > 0
    assert a.violations == 0
    assert math.isfinite(a.true_value)


def test_poisson_tail_stays_exact_at_a_big_senders_volume() -> None:
    # exp(-800) underflows: a running product from it would call 3,000 complaints unsurprising
    assert _poisson_tail(3000, 800.0) < 1e-9  # far below BREAKER_ALPHA (cancellation floor ~1e-13)
    assert _poisson_tail(800, 800.0) == pytest.approx(0.5, abs=0.02)
    assert _poisson_tail(10, 2.0) == pytest.approx(4.65e-5, rel=0.01)
    assert _poisson_tail(0, 5.0) == 1.0


def test_history_handed_over_twice_or_in_pieces_is_counted_once() -> None:
    history = _history(60)
    ctx = DecisionContext(now=NOW, briefs=(default_brief(),), content=tuple(default_content()), seed=0)
    once = build_agent_policy([default_brief()], default_content())
    once.nightly([], history, NOW)
    once.prepare_day([], ctx)
    twice = build_agent_policy([default_brief()], default_content())
    twice.nightly([], history, NOW - timedelta(hours=1))
    twice.nightly([], history, NOW)
    twice.prepare_day([], ctx)
    assert twice.history_used == once.history_used > 0
    # sends in one call, their clicks in the next: still matched
    pieces = build_agent_policy([default_brief()], default_content())
    pieces.nightly(
        [], {c: [e for e in evs if e.type is not EventType.CLICK] for c, evs in history.items()}, NOW
    )
    pieces.nightly([], {c: [e for e in evs if e.type is EventType.CLICK] for c, evs in history.items()}, NOW)
    pieces.prepare_day([], ctx)
    m_once, m_pieces = once.bundle.response, pieces.bundle.response
    assert isinstance(m_once, ClickResponseModel) and isinstance(m_pieces, ClickResponseModel)
    assert m_pieces.click_rate("*", KEY).mean == pytest.approx(m_once.click_rate("*", KEY).mean)
