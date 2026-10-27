"""Proof that means profit: refunds netted, one shop margin, and the minimum detectable effect.

Foundation fix 3 of docs/v3/ARCHITECTURE_REVIEW.md (row 15): a refund or void inside the reward window
is not profit; one shop-level margin feeds the value model, the proof and the 90-day value label; and
every proof carries the smallest effect it could see.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from autonomy.agents.analyst import template_briefing
from autonomy.contracts import (
    Action,
    Channel,
    CustomerState,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Group,
    HealthStatus,
    Objective,
    ProofSummary,
    Reward,
    stable_id,
)
from autonomy.decide.slow_models import SlowModelConfig, build_training_set
from autonomy.fakes.samples import sample_goal
from autonomy.learning.early_signal import DEFAULT_MARGIN_RATE, EarlySignalConfig
from autonomy.proof import (
    Costs,
    build_rewards,
    minimum_detectable_difference,
    minimum_detectable_lift,
    proof_rewards,
    proof_summary,
    smallest_visible_lift,
)
from autonomy.runtime.agent import build_agent_policy
from autonomy.runtime.settings import RuntimeSettings, build_runtime, workspace_policy_factory
from autonomy.store import memory_engine

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
Z_SUM = 1.959964 + 0.841621  # z(0.975) + z(0.80), written out so the module's own constant is checked


def day(n: float) -> datetime:
    return T0 + timedelta(days=n)


def act(did: str = "d1", at: datetime | None = None, *, cid: str = "c1") -> Decision:
    at = at or day(0)
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.ACT,
        action=Action(goal_id="g1", channel=Channel.EMAIL, offer_id="none", send_at=at),
        probability=0.5,
        reason="test",
        policy_version="t",
    )


def idle(did: str, at: datetime, *, cid: str, group: Group) -> Decision:
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.NOTHING,
        group=group,
        probability=1.0,
        reason="test",
        policy_version="t",
    )


def purchase(at: datetime, value: float, *, margin: float | None = None, order: str | None = "o1") -> Event:
    attrs: dict[str, str | int | float | bool] = {"order_id": order} if order is not None else {}
    return Event(
        event_id=stable_id("p", at, value, order),
        customer_id="c1",
        type=EventType.PURCHASE,
        ts=at,
        source="shopify",
        value=value,
        margin=margin,
        attrs=attrs,
    )


def refund(at: datetime, value: float, *, order: str | None = "o1", **attrs: Any) -> Event:
    named: dict[str, Any] = dict(attrs)
    if order is not None:
        named["order_id"] = order
    return Event(
        event_id=stable_id("r", at, value, order, tuple(sorted(attrs))),
        customer_id="c1",
        type=EventType.REFUND,
        ts=at,
        source="shopify",
        value=value,
        attrs=named,
    )


MARGIN = Costs(margin_rate=0.4)
HORIZON = 7


def reward(events: list[Event], *, last_touch: bool = False, costs: Costs = MARGIN) -> Reward:
    build = proof_rewards if last_touch else build_rewards
    out = build([act()], {"c1": events}, HORIZON, Objective.GROSS_PROFIT, costs, now=day(30))
    assert len(out) == 1
    return out[0]


# --- refunds and voids ---------------------------------------------------------------------------


def test_an_order_refunded_inside_the_window_is_not_profit() -> None:
    kept = reward([purchase(day(2), 100.0)])
    assert kept.converted and kept.revenue == 100.0 and kept.margin == pytest.approx(40.0)

    gone = reward([purchase(day(2), 100.0), refund(day(5), 100.0)])
    assert gone.revenue == 0.0 and gone.margin == pytest.approx(0.0) and gone.net == pytest.approx(0.0)
    assert not gone.converted  # a fully refunded order is not a conversion


def test_a_refund_after_the_window_is_still_profit() -> None:
    late = reward([purchase(day(2), 100.0), refund(day(8), 100.0)])  # the window closed on day 7
    assert late.converted and late.revenue == 100.0 and late.margin == pytest.approx(40.0)


def test_a_partial_refund_takes_its_share_of_the_margin() -> None:
    half = reward([purchase(day(2), 100.0), refund(day(4), 25.0)])
    assert half.converted and half.revenue == pytest.approx(75.0) and half.margin == pytest.approx(30.0)
    # a second refund can never take more than the order was worth
    capped = reward([purchase(day(2), 100.0), refund(day(4), 80.0), refund(day(5), 80.0)])
    assert capped.revenue == pytest.approx(0.0) and capped.margin == pytest.approx(0.0)
    assert not capped.converted


def test_a_recorded_margin_is_reversed_in_proportion() -> None:
    r = reward([purchase(day(2), 100.0, margin=55.0), refund(day(3), 50.0)])
    assert r.margin == pytest.approx(27.5) and r.margin_assumed == 0.0


def test_a_shopify_void_is_netted_like_any_refund() -> None:
    """The connector turns a voided order into a REFUND of the unpaid total, refund id 'void'."""
    r = reward([purchase(day(1), 80.0), refund(day(2), 80.0, refund_id="void")])
    assert r.net == pytest.approx(0.0) and not r.converted


def test_cash_on_delivery_returned_to_origin_does_not_count_as_profit() -> None:
    # The courier reports the parcel back; the refund event carries only a token amount but says RTO.
    rto = reward([purchase(day(1), 100.0), refund(day(6), 1.0, rto=True)])
    assert rto.revenue == 0.0 and rto.margin == pytest.approx(0.0) and not rto.converted
    by_reason = reward([purchase(day(1), 100.0), refund(day(6), 1.0, reason="return_to_origin")])
    assert by_reason.margin == pytest.approx(0.0)
    # an RTO reported after the window has closed was not known when the window was judged
    after = reward([purchase(day(1), 100.0), refund(day(9), 1.0, rto=True)])
    assert after.margin == pytest.approx(40.0)


def test_a_refund_with_no_order_id_reverses_the_latest_earlier_purchase() -> None:
    r = reward(
        [
            purchase(day(1), 100.0, order=None),
            purchase(day(3), 60.0, order=None),
            refund(day(4), 60.0, order=None),
        ]
    )
    assert r.revenue == pytest.approx(100.0) and r.margin == pytest.approx(40.0)
    assert r.converted


def test_a_refund_of_an_order_we_do_not_hold_is_ignored() -> None:
    r = reward([purchase(day(2), 100.0), refund(day(3), 100.0, order="from-before-the-history")])
    assert r.revenue == 100.0 and r.margin == pytest.approx(40.0)


def test_refunds_net_under_last_touch_and_window_attribution_alike() -> None:
    events = [purchase(day(2), 100.0), refund(day(4), 100.0)]
    assert reward(events, last_touch=True).margin == pytest.approx(0.0)
    decisions = [act("d1", day(0)), act("d2", day(1))]
    both = build_rewards(decisions, {"c1": events}, HORIZON, Objective.GROSS_PROFIT, MARGIN, now=day(30))
    assert [r.margin for r in both] == [pytest.approx(0.0), pytest.approx(0.0)]


def test_a_refund_nets_only_the_decisions_whose_window_holds_it() -> None:
    """A day-5 purchase is in both windows (day 0-7 and day 4-11); a day-9 refund is in only the second."""
    decisions = [act("d1", day(0)), act("d2", day(4))]
    events = [purchase(day(5), 100.0), refund(day(9), 100.0)]
    out = {
        r.decision_id: r
        for r in build_rewards(
            decisions, {"c1": events}, HORIZON, Objective.GROSS_PROFIT, MARGIN, now=day(30)
        )
    }
    assert out["d1"].margin == pytest.approx(40.0) and out["d1"].converted
    assert out["d2"].margin == pytest.approx(0.0) and not out["d2"].converted


def test_a_refund_of_a_purchase_before_the_first_decision_leaves_the_earned_one_alone() -> None:
    """The day -3 purchase was credited to no decision; its refund must not reverse the day-2 one."""
    r = reward(
        [
            purchase(day(-3), 80.0, order=None),
            purchase(day(2), 50.0, order=None),
            refund(day(3), 80.0, order=None),
        ]
    )
    assert r.converted and r.revenue == pytest.approx(50.0) and r.margin == pytest.approx(20.0)
    # named, the same holds: the order is held even though no decision was credited with it
    named = reward(
        [purchase(day(-3), 80.0, order="old"), purchase(day(2), 50.0), refund(day(3), 80.0, order="old")]
    )
    assert named.converted and named.revenue == pytest.approx(50.0)


def test_a_refund_of_a_purchase_after_the_last_touch_cutoff_leaves_the_counted_one_alone() -> None:
    """With now on day 9 and a 7-day horizon the cutoff is day 2: the day-3 purchase is not counted."""
    events = [
        purchase(day(1), 50.0, order=None),
        purchase(day(3), 50.0, order=None),
        refund(day(4), 50.0, order=None),
    ]
    out = proof_rewards([act()], {"c1": events}, HORIZON, Objective.GROSS_PROFIT, MARGIN, now=day(9))
    assert len(out) == 1
    assert out[0].converted and out[0].revenue == pytest.approx(50.0)


def _line(at: datetime, value: float, n: int, order: str = "o1") -> Event:
    return Event(
        event_id=f"line-{order}-{n}",
        customer_id="c1",
        type=EventType.PURCHASE,
        ts=at,
        source="csv",
        value=value,
        attrs={"order_id": order},
    )


def test_an_order_in_several_lines_is_reversed_in_full() -> None:
    lines = [_line(day(2), 30.0, 1), _line(day(2), 30.0, 2), _line(day(2), 40.0, 3)]
    whole = reward([*lines, refund(day(4), 100.0)])
    assert whole.revenue == pytest.approx(0.0) and whole.margin == pytest.approx(0.0)
    assert not whole.converted
    half = reward([*lines, refund(day(4), 50.0)])  # spread over the lines in proportion
    assert half.converted and half.revenue == pytest.approx(50.0) and half.margin == pytest.approx(20.0)
    rto = reward([*lines, refund(day(4), 1.0, rto=True)])
    assert rto.revenue == pytest.approx(0.0) and not rto.converted


def test_a_negative_refund_is_netted_like_a_positive_one() -> None:
    r = reward([purchase(day(2), 100.0), refund(day(4), -100.0)])
    assert r.revenue == pytest.approx(0.0) and r.margin == pytest.approx(0.0) and not r.converted
    part = reward([purchase(day(2), 100.0), refund(day(4), -25.0)])
    assert part.revenue == pytest.approx(75.0) and part.converted


def test_a_refund_at_the_same_instant_as_its_purchase_is_netted() -> None:
    r = reward([refund(day(2), 100.0), purchase(day(2), 100.0)])  # store order: refund first
    assert r.revenue == pytest.approx(0.0) and not r.converted


def test_two_full_refunds_take_a_conversion_back_once() -> None:
    """A second order still converts after the first is refunded twice over."""
    r = reward(
        [
            purchase(day(1), 100.0, order="a"),
            purchase(day(2), 10.0, order="b"),
            refund(day(3), 100.0, order="a"),
            refund(day(4), 100.0, order="a"),
        ]
    )
    assert r.converted and r.revenue == pytest.approx(10.0)


# --- one shop margin -----------------------------------------------------------------------------


def test_the_margin_setting_prices_a_purchase_with_no_margin_and_is_recorded() -> None:
    r = reward([purchase(day(2), 100.0)], costs=Costs(margin_rate=0.25))
    assert r.margin == pytest.approx(25.0) and r.margin_assumed == pytest.approx(25.0)
    own = reward([purchase(day(2), 100.0, margin=60.0)], costs=Costs(margin_rate=0.25))
    assert own.margin == 60.0 and own.margin_assumed == 0.0  # a recorded margin always wins


def test_unset_margin_counts_full_value_exactly_as_before() -> None:
    unset = Costs()
    assert unset.margin_rate is None and unset.margin_share == 1.0
    assert reward([purchase(day(2), 100.0)], costs=unset).margin == 100.0


def _profits() -> dict[Group, list[float]]:
    rng = np.random.default_rng(4)
    sizes = ((Group.AGENT, 60), (Group.HOLDOUT, 40), (Group.BASELINE, 40))
    return {g: [float(rng.gamma(2.0, 10.0)) for _ in range(n)] for g, n in sizes}


def _summary(costs: Costs | None, *, margin_known: bool = False) -> ProofSummary:
    rewards: list[Reward] = []
    decisions: list[Decision] = []
    for group, margins in _profits().items():
        for i, margin in enumerate(margins):
            cid = f"{group.value}-{i}"
            decisions.append(idle(f"d-{cid}", T0, cid=cid, group=group))
            rewards.append(
                Reward(
                    decision_id=f"d-{cid}",
                    customer_id=cid,
                    goal_id=None,
                    window_end=day(7),
                    converted=True,
                    revenue=margin * 2.0,
                    margin=margin,
                    margin_assumed=0.0 if margin_known else margin,
                    cost=0.0,
                    net=margin,
                )
            )
    return proof_summary(rewards, decisions, day(10), costs=costs)


def test_the_proof_says_so_when_no_shop_margin_is_set() -> None:
    s = _summary(Costs())
    assert s.margin_rate is None
    assert s.margin_note is not None and s.margin_note.startswith("No shop margin is set")
    assert s.margin_note not in s.notes  # shown under the headline, not repeated in the notes
    shop = _summary(Costs(margin_rate=0.35))
    assert shop.margin_rate == 0.35
    assert shop.margin_note is not None and "35%" in shop.margin_note
    assert not shop.margin_note.startswith("No shop margin is set")
    # nothing rests on the assumption when every purchase carries its margin: no note either way
    assert _summary(Costs(), margin_known=True).margin_note is None
    # a caller that does not pass costs gets no statement about the margin
    assert _summary(None).margin_note is None


def test_the_margin_setting_is_the_single_source() -> None:
    """One value in RuntimeSettings reaches the value model, the proof's costs and the 90-day value label."""
    rate = 0.35
    settings = RuntimeSettings.from_env({"AUTONOMY_MARGIN_RATE": "0.35"})
    assert settings.margin_rate == rate and RuntimeSettings().margin_rate is None

    # 1. the decision value model
    runtime = build_runtime(settings, engine=memory_engine())
    assert runtime.margin_rate == rate
    policy: Any = runtime.policy_factory([], [])
    assert policy.bundle.value.default_margin_rate == rate
    state = CustomerState(customer_id="c", as_of=T0)
    assert policy.value.margin_rate(state) == pytest.approx(rate)
    # unset keeps the built-in 0.4
    plain: Any = workspace_policy_factory(runtime.workspace)([], [])
    assert plain.bundle.value.default_margin_rate == 0.4

    # 2. the proof rewards: the read models the API serves count purchases at the same rate
    from api.v2.deps import build_services_from_runtime

    services = build_services_from_runtime(runtime, clock=lambda: day(30))
    assert services.margin_rate == rate

    # 3. the 90-day value label and the early read, with v3 on
    v3: Any = build_agent_policy([], [], margin_rate=rate, v3=True)
    assert v3.who is not None and v3.who.config.slow.margin_rate == rate
    assert EarlySignalConfig().margin_share == DEFAULT_MARGIN_RATE == 0.3
    assert EarlySignalConfig(margin_rate=rate).margin_share == rate


def test_the_value_label_uses_the_shop_margin_until_a_purchase_reports_one() -> None:
    events: list[Event] = []
    for i in range(40):
        for month in range(6):
            events.append(
                Event(
                    event_id=f"e-{i}-{month}",
                    customer_id=f"c{i}",
                    type=EventType.PURCHASE,
                    ts=datetime(2025, 1 + month, 5 + (i % 20), 12, tzinfo=UTC),
                    source="t",
                    value=100.0,
                )
            )
    as_of = datetime(2025, 12, 31, tzinfo=UTC)
    full = build_training_set(events, as_of)
    shop = build_training_set(events, as_of, SlowModelConfig(margin_rate=0.25))
    labelled = ~np.isnan(full.value)
    assert labelled.any()
    assert np.allclose(shop.value[labelled], full.value[labelled] * 0.25)


def test_a_bundle_with_another_margin_is_refused() -> None:
    bundle: Any = build_agent_policy([], []).bundle
    with pytest.raises(ValueError, match="build_bundle"):
        build_agent_policy([], [], bundle=bundle, margin_rate=0.2)


# --- the minimum detectable effect ---------------------------------------------------------------


def _expected(n_customers: int, base_rate: float) -> float:
    """Written out from section 5c: 10% holdout, 80% agent group, 80% power, two-sided 5%."""
    n_holdout, n_agent = 0.10 * n_customers, 0.80 * n_customers
    se = math.sqrt(base_rate * (1 - base_rate) * (1 / n_agent + 1 / n_holdout))
    return Z_SUM * se / base_rate


@pytest.mark.parametrize(
    ("customers", "base_rate", "review_pct"),
    [(8_000, 0.05, 46), (8_000, 0.20, 21), (300_000, 0.05, 7), (300_000, 0.20, 3)],
)
def test_the_mde_matches_the_arithmetic_for_8k_and_300k_customers(
    customers: int, base_rate: float, review_pct: int
) -> None:
    got = smallest_visible_lift(customers, base_rate)
    assert got is not None
    assert got == pytest.approx(_expected(customers, base_rate), rel=1e-5)
    assert round(got * 100) == review_pct  # the review's table, section 5c
    direct = minimum_detectable_lift(round(0.8 * customers), round(0.1 * customers), base_rate)
    assert direct == pytest.approx(got)


def test_a_small_shop_can_only_see_a_bigger_lift() -> None:
    small, big = smallest_visible_lift(8_000, 0.05), smallest_visible_lift(300_000, 0.05)
    assert small is not None and big is not None and small > 5 * big
    assert smallest_visible_lift(0, 0.05) is None and minimum_detectable_lift(10, 10, 0.0) is None


def test_the_difference_mde_is_2_8_standard_errors() -> None:
    rng = np.random.default_rng(1)
    a, b = rng.normal(5.0, 3.0, 500), rng.normal(5.0, 3.0, 100)
    se = math.sqrt(a.var(ddof=1) / 500 + b.var(ddof=1) / 100)
    assert minimum_detectable_difference(a, b) == pytest.approx(Z_SUM * se, rel=1e-5)
    assert minimum_detectable_difference(a[:1], b) is None


def test_every_proof_carries_its_mde_next_to_the_numbers() -> None:
    s = _summary(Costs())
    d = s.detectable
    assert d is not None
    profits = _profits()
    agent, holdout = np.array(profits[Group.AGENT]), np.array(profits[Group.HOLDOUT])
    per_customer = minimum_detectable_difference(agent, holdout)
    assert per_customer is not None
    assert d.profit == pytest.approx(per_customer * len(agent))  # the unit of incremental_profit
    assert d.relative == pytest.approx(per_customer / holdout.mean())
    assert d.revenue is not None and d.lift_vs_baseline is not None and d.period_days == 7
    assert "only see" in d.sentence and d.sentence not in s.notes


def test_no_mde_is_claimed_when_the_groups_are_too_small() -> None:
    rewards: list[Reward] = []
    decisions: list[Decision] = []
    for group in (Group.AGENT, Group.HOLDOUT):
        for i in range(5):
            cid = f"{group.value}-{i}"
            decisions.append(idle(f"d-{cid}", T0, cid=cid, group=group))
            rewards.append(
                Reward(
                    decision_id=f"d-{cid}",
                    customer_id=cid,
                    goal_id=None,
                    window_end=day(7),
                    converted=True,
                    margin=float(i),
                    net=float(i),
                )
            )
    assert proof_summary(rewards, decisions, day(10)).detectable is None


def test_the_briefing_states_the_mde_and_the_unset_margin() -> None:
    s = _summary(Costs())
    health = HealthStatus(
        as_of=day(10), delivery_rate=1.0, complaint_rate=0.0, contacts_per_customer_week=0.4
    )
    b = template_briefing(sample_goal(day(10)), s, health, {}, "weekly", day(10))
    section = b.sections[0]
    assert "would not show up yet" in section.body
    assert "margin is not set" in section.body
    assert "detectable_profit" in section.numbers and "detectable_lift" in section.numbers


def test_the_ledger_summary_shows_the_mde_of_the_conversion_comparison() -> None:
    from api.v2.engines.measurement import measurement_summary

    decisions: list[Decision] = []
    events: list[Event] = []
    for group, n, buyers in ((Group.AGENT, 120, 30), (Group.HOLDOUT, 60, 9)):
        for i in range(n):
            cid = f"{group.value}-{i}"
            decisions.append(idle(f"d-{cid}", T0, cid=cid, group=group))
            if i < buyers:
                events.append(
                    Event(
                        event_id=f"p-{cid}",
                        customer_id=cid,
                        type=EventType.PURCHASE,
                        ts=day(2),
                        source="t",
                        value=50.0,
                        attrs={"order_id": f"o-{cid}"},
                    )
                )
    out = measurement_summary(
        decisions,
        events,
        horizon_days=7,
        objective=Objective.GROSS_PROFIT,
        costs=Costs(),
        now=day(30),
        min_group=30,
    )
    rate = 9 / 60
    expected = Z_SUM * math.sqrt(rate * (1 - rate) * (1 / 120 + 1 / 60)) / rate * 100
    assert out.causal_metrics.minimum_detectable_lift_pct == pytest.approx(expected, rel=1e-4)
    assert out.financial_impact.minimum_detectable_profit is not None
    assert out.financial_impact.margin_note is not None  # no shop margin was given


# --- the shown numbers: the API proof and the ledger -----------------------------------------------


@pytest.mark.parametrize(("rate", "per_order"), [(0.35, 35.0), (None, 100.0)])
def test_the_api_proof_prices_a_purchase_with_no_margin_at_the_setting(
    rate: float | None, per_order: float
) -> None:
    """`StoreReadModels.proof` (what the proof page shows) counts an unmargined order at value x rate."""
    from api.v2.deps import build_services_from_runtime

    env = {"AUTONOMY_MARGIN_RATE": str(rate)} if rate is not None else {}
    runtime = build_runtime(RuntimeSettings.from_env(env), engine=memory_engine())
    decisions = [
        idle(f"d-{g.value}-{i}", T0, cid=f"{g.value}-{i}", group=g)
        for g in (Group.AGENT, Group.HOLDOUT)
        for i in range(30)
    ]
    runtime.decisions.record(decisions)
    runtime.events.append(
        Event(
            event_id=f"p-{i}",
            customer_id=f"agent-{i}",
            type=EventType.PURCHASE,
            ts=day(2),
            source="shopify",
            value=100.0,
            attrs={"order_id": f"o-{i}"},
        )
        for i in range(30)
    )
    services = build_services_from_runtime(runtime, clock=lambda: day(60))
    proof = services.proof(None)
    assert proof is not None and proof.incremental_profit is not None
    assert proof.incremental_profit.point == pytest.approx(30 * per_order)
    assert proof.margin_rate == rate
    assert proof.margin_note is not None
    assert proof.margin_note.startswith("No shop margin is set") is (rate is None)


def test_a_who_book_with_another_margin_is_refused() -> None:
    from autonomy.decide.who import WhoBook, WhoConfig

    with pytest.raises(ValueError, match="WhoBook"):
        build_agent_policy([], [], who=WhoBook(), margin_rate=0.35)
    same = WhoBook(WhoConfig(slow=SlowModelConfig(margin_rate=0.35)))
    policy: Any = build_agent_policy([], [], who=same, margin_rate=0.35)
    assert policy.who is same


def _ledger(customers: int, base_rate: float) -> Any:
    """The ledger summary over `customers` split 80 / 10 / 10, the holdout buying at `base_rate`."""
    from api.v2.engines.measurement import measurement_summary

    decisions: list[Decision] = []
    events: list[Event] = []
    for group, share in ((Group.AGENT, 0.8), (Group.HOLDOUT, 0.1), (Group.BASELINE, 0.1)):
        n = round(customers * share)
        for i in range(n):
            cid = f"{group.value}-{i}"
            decisions.append(idle(f"d-{cid}", T0, cid=cid, group=group))
            if i < round(n * base_rate):
                events.append(
                    Event(
                        event_id=f"p-{cid}",
                        customer_id=cid,
                        type=EventType.PURCHASE,
                        ts=day(2),
                        source="t",
                        value=50.0,
                        attrs={"order_id": f"o-{cid}"},
                    )
                )
    return measurement_summary(
        decisions, events, horizon_days=7, objective=Objective.GROSS_PROFIT, costs=Costs(), now=day(30)
    )


@pytest.mark.parametrize(("base_rate", "shown"), [(0.05, 45.8), (0.20, 21.0)])
def test_the_ledger_shows_the_review_mde_for_8k_customers(base_rate: float, shown: float) -> None:
    got = _ledger(8_000, base_rate).causal_metrics.minimum_detectable_lift_pct
    assert got == pytest.approx(_expected(8_000, base_rate) * 100, rel=1e-6)
    assert round(got, 1) == shown  # docs/v3/PROOF_PROFIT.md, from the review's section 5c


@pytest.mark.parametrize(("base_rate", "shown"), [(0.05, 7.5), (0.20, 3.4)])
def test_the_ledger_mde_for_300k_customers(base_rate: float, shown: float) -> None:
    """The function the ledger calls, on 240,000 agent and 30,000 holdout conversion flags."""
    from api.v2.engines.measurement import _detectable_lift_pct

    holdout = np.zeros(30_000)
    holdout[: round(30_000 * base_rate)] = 1.0
    got = _detectable_lift_pct(np.zeros(240_000), holdout)
    assert got is not None
    assert got == pytest.approx(_expected(300_000, base_rate) * 100, rel=1e-6)
    assert round(got, 1) == shown


@pytest.mark.slow
def test_the_ledger_shows_the_review_mde_for_300k_customers() -> None:
    got = _ledger(300_000, 0.05).causal_metrics.minimum_detectable_lift_pct
    assert got is not None and round(got, 1) == 7.5


def test_no_mde_is_claimed_when_nobody_varies() -> None:
    """Nobody bought in either group: a zero spread must not read as 'it can see any gain'."""
    assert minimum_detectable_difference([0.0] * 40, [0.0] * 40) is None
    rewards: list[Reward] = []
    decisions: list[Decision] = []
    for group in (Group.AGENT, Group.HOLDOUT):
        for i in range(40):
            cid = f"{group.value}-{i}"
            decisions.append(idle(f"d-{cid}", T0, cid=cid, group=group))
            rewards.append(
                Reward(
                    decision_id=f"d-{cid}",
                    customer_id=cid,
                    goal_id=None,
                    window_end=day(7),
                    converted=False,
                    net=0.0,
                )
            )
    s = proof_summary(rewards, decisions, day(10))
    assert s.detectable is None
    assert any("does not vary yet" in n for n in s.notes)


def test_the_briefing_states_the_revenue_and_rule_mde() -> None:
    s = _summary(Costs(margin_rate=0.4))
    assert s.detectable is not None and s.detectable.revenue is not None
    health = HealthStatus(
        as_of=day(10), delivery_rate=1.0, complaint_rate=0.0, contacts_per_customer_week=0.4
    )
    b = template_briefing(sample_goal(day(10)), s, health, {}, "weekly", day(10))
    money = next(x for x in b.sections if "detectable_profit" in x.numbers)
    assert "detectable_revenue" in money.numbers and "revenue gain below about" in money.body
    rule = next(x for x in b.sections if "detectable_lift_per_customer" in x.numbers)
    assert "simple rule below about" in rule.body
