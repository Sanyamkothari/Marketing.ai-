"""Tests for the proof engine: the split, rewards, the proof numbers and off-policy evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import (
    Action,
    Channel,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Envelope,
    Event,
    EventType,
    Group,
    Objective,
    Reward,
    stable_id,
)
from autonomy.fakes import (
    ContactEveryonePolicy,
    FakeEnvironment,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryStateStore,
    NothingPolicy,
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.loop import run_days
from autonomy.proof import (
    Costs,
    assign_group,
    bootstrap_difference,
    build_rewards,
    doubly_robust,
    evaluate,
    from_decisions,
    group_assigner,
    ips,
    proof_rewards,
    proof_summary,
    snips,
    welch_difference,
    with_group,
)

T0 = datetime(2026, 1, 5, 10, tzinfo=UTC)


def day(n: float) -> datetime:
    return T0 + timedelta(days=n)


# --- split -----------------------------------------------------------------------------------------


def test_split_shares_within_tolerance_on_100k_ids() -> None:
    ids = [f"cust-{i}" for i in range(100_000)]
    groups = [assign_group(c, "biz-1", 0.10, 0.10) for c in ids]
    share = {g: groups.count(g) / len(ids) for g in Group}
    # Binomial sd is ~0.001 at n = 100k; 0.005 is five of them.
    assert share[Group.HOLDOUT] == pytest.approx(0.10, abs=0.005)
    assert share[Group.BASELINE] == pytest.approx(0.10, abs=0.005)
    assert share[Group.AGENT] == pytest.approx(0.80, abs=0.005)


def test_split_is_stable_and_salted() -> None:
    ids = [f"c{i}" for i in range(2000)]
    first = [assign_group(c, "salt-a", 0.2, 0.1) for c in ids]
    assert first == [assign_group(c, "salt-a", 0.2, 0.1) for c in ids]
    other = [assign_group(c, "salt-b", 0.2, 0.1) for c in ids]
    assert sum(a is not b for a, b in zip(first, other, strict=True)) > 200


def test_growing_one_share_never_moves_the_other_group() -> None:
    ids = [f"c{i}" for i in range(5000)]
    before = {c: assign_group(c, "s", 0.05, 0.10) for c in ids}
    after = {c: assign_group(c, "s", 0.10, 0.10) for c in ids}
    assert all(after[c] is Group.BASELINE for c in ids if before[c] is Group.BASELINE)
    assert all(after[c] is Group.HOLDOUT for c in ids if before[c] is Group.HOLDOUT)
    moved = [c for c in ids if before[c] is not after[c]]
    assert moved and all(before[c] is Group.AGENT and after[c] is Group.HOLDOUT for c in moved)


def test_with_group_and_assigner() -> None:
    state = CustomerState(customer_id="c1", as_of=T0)
    group = assign_group("c1", "s", 0.3, 0.3)
    updated = with_group(state, salt="s", holdout_share=0.3, baseline_share=0.3)
    assert updated.group is group
    assert with_group(updated, salt="s", holdout_share=0.3, baseline_share=0.3) is updated
    env = Envelope(channels=(Channel.EMAIL,), budget=100.0, holdout_share=0.3, baseline_share=0.3)
    assert group_assigner("s", envelope=env)(state).group is group


def test_split_rejects_bad_shares() -> None:
    with pytest.raises(ValueError):
        assign_group("c", "s", 0.6, 0.5)
    with pytest.raises(ValueError):
        assign_group("c", "s", -0.1, 0.1)


# --- rewards ---------------------------------------------------------------------------------------


def act(did: str, at: datetime, *, cid: str = "c1", offer: str = "pct10", goal: str = "g1") -> Decision:
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.ACT,
        action=Action(goal_id=goal, channel=Channel.EMAIL, offer_id=offer, send_at=at),
        probability=0.5,
        reason="test",
        policy_version="t",
    )


def nothing(did: str, at: datetime, *, cid: str = "c1", group: Group = Group.AGENT) -> Decision:
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


def ev(kind: EventType, at: datetime, *, cid: str = "c1", did: str | None = None, **kw: object) -> Event:
    channel = Channel.EMAIL if kind not in (EventType.PURCHASE, EventType.VISIT) else None
    return Event(
        event_id=stable_id("ev", kind, cid, at, did),
        customer_id=cid,
        type=kind,
        ts=at,
        source="test",
        channel=channel,
        decision_id=did,
        **kw,  # type: ignore[arg-type]
    )


COSTS = Costs(channel={Channel.EMAIL: 0.5}, offer={"pct10": 6.0})


def _story() -> tuple[list[Decision], dict[str, list[Event]]]:
    decisions = [act("d1", day(0)), nothing("d2", day(2))]
    events = {
        "c1": [
            ev(EventType.SEND, day(0.1), did="d1"),
            ev(EventType.PURCHASE, day(3), value=100.0, margin=40.0, attrs={"offer_id": "pct10"}),
        ]
    }
    return decisions, events


def test_window_rewards_credit_every_covering_decision_but_charge_the_offer_once() -> None:
    decisions, events = _story()
    rewards = {
        r.decision_id: r
        for r in build_rewards(decisions, events, 7, Objective.GROSS_PROFIT, COSTS, now=day(10))
    }
    d1, d2 = rewards["d1"], rewards["d2"]
    assert d1.converted and d1.revenue == 100.0 and d1.margin == 40.0
    assert d1.cost == pytest.approx(6.5) and d1.net == pytest.approx(33.5)
    assert d1.goal_id == "g1" and d1.window_end == day(7)
    assert d2.converted and d2.revenue == 100.0 and d2.cost == 0.0 and d2.net == 40.0
    assert d2.goal_id is None


def test_last_touch_counts_each_purchase_once() -> None:
    decisions, events = _story()
    rewards = {
        r.decision_id: r
        for r in proof_rewards(decisions, events, 7, Objective.GROSS_PROFIT, COSTS, now=day(11))
    }
    assert rewards["d1"].revenue == 100.0 and rewards["d1"].cost == pytest.approx(6.5)
    assert rewards["d2"].revenue == 0.0 and not rewards["d2"].converted
    assert sum(r.revenue for r in rewards.values()) == 100.0
    # On day 10 the purchase (day 3) is not before now - 7 days, so last touch doesn't count it yet.
    early = proof_rewards(decisions, events, 7, Objective.GROSS_PROFIT, COSTS, now=day(10))
    assert sum(r.revenue for r in early) == 0.0


def test_last_touch_measures_every_group_over_the_same_period() -> None:
    """No effect planted, so the proof must find none.

    Agent customers get one ACT on day 0 then daily "do nothing"; holdout customers get "do nothing"
    every day; both buy every day. Last touch hands an agent customer's purchases to the day-0 ACT,
    whose window closes early, and a holdout customer's to that day's decision, whose window is still
    open near `now`. Without a common cutoff the agent looked days of purchases richer.
    """
    decisions: list[Decision] = []
    events: dict[str, list[Event]] = {}
    for group in (Group.AGENT, Group.HOLDOUT):
        for i in range(30):
            cid = f"{group.value}{i}"
            for k in range(20):
                if group is Group.AGENT and k == 0:
                    decisions.append(act(f"{cid}-{k}", day(k), cid=cid, offer="none"))
                else:
                    decisions.append(nothing(f"{cid}-{k}", day(k), cid=cid, group=group))
            events[cid] = [ev(EventType.PURCHASE, day(k + 0.5), cid=cid, value=10.0) for k in range(20)]
    rewards = proof_rewards(decisions, events, 7, Objective.REVENUE, Costs(), now=day(12))
    s = proof_summary(rewards, decisions, day(12), min_group=10)
    assert s.incremental_revenue is not None
    assert s.incremental_revenue.point == pytest.approx(0.0)
    # Every customer is measured on the purchases of days 0 to 4 (before day 12 - 7).
    per_customer = {r.customer_id: 0.0 for r in rewards}
    for r in rewards:
        per_customer[r.customer_id] += r.revenue
    assert set(per_customer.values()) == {50.0}


def test_open_windows_unsent_actions_and_objectives() -> None:
    decisions, events = _story()
    # d2's window (day 2 to day 9) is still open on day 8.
    early = build_rewards(decisions, events, 7, Objective.GROSS_PROFIT, COSTS, now=day(8))
    assert [r.decision_id for r in early] == ["d1"]
    # Without a SEND the action cost nothing to send, but the redeemed offer still costs.
    unsent = {"c1": [e for e in events["c1"] if e.type is not EventType.SEND]}
    r1, _ = build_rewards(decisions, unsent, 7, Objective.REVENUE, COSTS, now=day(10))
    assert r1.cost == 6.0 and r1.net == 94.0
    conv = build_rewards(decisions, events, 7, Objective.CONVERSION, COSTS, now=day(10))
    assert [r.net for r in conv] == [1.0, 1.0]
    valued = Costs(channel=COSTS.channel, offer=COSTS.offer, value_per_conversion=20.0)
    conv2 = build_rewards(decisions, events, 7, Objective.CONVERSION, valued, now=day(10))
    assert conv2[0].net == pytest.approx(13.5)


def test_margin_fallback_watermark_and_unsubscribes() -> None:
    decisions = [act("d1", day(0), offer="none")]
    events = {
        "c1": [
            ev(EventType.SEND, day(0.1), did="d1"),
            ev(EventType.UNSUBSCRIBE, day(0.2), did="d1"),
            ev(EventType.PURCHASE, day(1), value=50.0),
            ev(EventType.VISIT, day(9)),
        ]
    }
    costs = Costs(channel={Channel.EMAIL: 0.5}, margin_rate=0.3)
    # No `now`: the latest event (day 9) is the watermark, so the 7-day window has closed.
    (r,) = build_rewards(decisions, events, 7, Objective.GROSS_PROFIT, costs)
    assert r.margin == pytest.approx(15.0) and r.cost == 0.5 and r.unsubscribed
    assert build_rewards(decisions, {"c1": events["c1"][:3]}, 7, Objective.GROSS_PROFIT, costs) == []


def test_purchase_outside_every_window_is_ignored() -> None:
    decisions = [nothing("d1", day(0))]
    events = {"c1": [ev(EventType.PURCHASE, day(-1), value=10.0), ev(EventType.PURCHASE, day(8), value=10.0)]}
    (r,) = build_rewards(decisions, events, 7, Objective.REVENUE, Costs(), now=day(9))
    assert r.revenue == 0.0 and not r.converted


# --- estimate --------------------------------------------------------------------------------------


def test_welch_interval_covers_a_known_difference_about_95_percent() -> None:
    rng = np.random.default_rng(7)
    reps, n1, n0 = 600, 300, 150
    a = rng.gamma(2.0, 15.0, size=(reps, n1)) + 3.0  # mean 33
    b = rng.gamma(2.0, 15.0, size=(reps, n0))  # mean 30
    hits = 0
    for i in range(reps):
        est = welch_difference(a[i], b[i], scale=n1)
        assert est is not None
        hits += est.low <= 3.0 * n1 <= est.high
    assert 0.92 <= hits / reps <= 0.98


def test_bootstrap_matches_normal_on_symmetric_data() -> None:
    rng = np.random.default_rng(3)
    a, b = rng.normal(5, 2, 400), rng.normal(4, 2, 400)
    normal, boot = welch_difference(a, b), bootstrap_difference(a, b, n_boot=1000, seed=1)
    assert normal is not None and boot is not None
    assert boot.point == normal.point
    assert boot.low == pytest.approx(normal.low, abs=0.06) and boot.high == pytest.approx(
        normal.high, abs=0.06
    )
    assert bootstrap_difference(a, b, n_boot=1000, seed=1) == boot
    assert welch_difference(a[:1], b) is None


def _population(
    rng: np.random.Generator, sizes: Mapping[Group, int], effect: Mapping[Group, float]
) -> tuple[list[Reward], list[Decision]]:
    """One decision and one closed reward per customer; agent customers spend 2 each."""
    rewards: list[Reward] = []
    decisions: list[Decision] = []
    for group, n in sizes.items():
        for i in range(n):
            cid = f"{group.value}-{i}"
            did = f"d-{cid}"
            decisions.append(nothing(did, T0, cid=cid, group=group))
            margin = float(rng.gamma(2.0, 10.0)) + effect[group]
            cost = 2.0 if group is Group.AGENT else 0.0
            rewards.append(
                Reward.model_construct(
                    decision_id=did,
                    customer_id=cid,
                    goal_id=None,
                    window_end=day(7),
                    converted=margin > 0,
                    revenue=margin * 2.5,
                    margin=margin,
                    cost=cost,
                    net=margin - cost,
                    unsubscribed=False,
                    complained=False,
                )
            )
    return rewards, decisions


def test_proof_summary_intervals_cover_the_planted_effect() -> None:
    rng = np.random.default_rng(11)
    sizes = {Group.AGENT: 240, Group.HOLDOUT: 80, Group.BASELINE: 80}
    effect = {Group.AGENT: 5.0, Group.HOLDOUT: 0.0, Group.BASELINE: 3.0}
    true_profit = (5.0 - 2.0) * 240  # margin lift minus the agent's spend, over 240 customers
    true_revenue = 5.0 * 2.5 * 240
    true_lift = 5.0 - 2.0 - 3.0
    reps = 120
    hits = {"profit": 0, "revenue": 0, "lift": 0}
    for _ in range(reps):
        rewards, decisions = _population(rng, sizes, effect)
        s = proof_summary(rewards, decisions, day(10))
        assert s.incremental_profit and s.incremental_revenue and s.lift_vs_baseline and s.roi
        hits["profit"] += s.incremental_profit.low <= true_profit <= s.incremental_profit.high
        hits["revenue"] += s.incremental_revenue.low <= true_revenue <= s.incremental_revenue.high
        hits["lift"] += s.lift_vs_baseline.low <= true_lift <= s.lift_vs_baseline.high
        assert s.spend == pytest.approx(480.0)
        assert s.roi.point == pytest.approx(s.incremental_profit.point / 480.0)
    for name, h in hits.items():
        assert 0.88 <= h / reps <= 1.0, name


def test_proof_summary_counts_groups_and_bootstrap_runs() -> None:
    rng = np.random.default_rng(5)
    rewards, decisions = _population(
        rng, {Group.AGENT: 100, Group.HOLDOUT: 40, Group.BASELINE: 40}, dict.fromkeys(Group, 0.0)
    )
    s = proof_summary(rewards, {d.decision_id: d for d in decisions}, day(10), method="bootstrap", n_boot=300)
    assert (s.agent_n, s.holdout_n, s.baseline_n) == (100, 40, 40)
    assert s.incremental_profit is not None and s.roi is not None
    assert s == proof_summary(rewards, decisions, day(10), method="bootstrap", n_boot=300)
    assert any("bootstrap" in n for n in s.notes)


def test_proof_summary_refuses_with_small_groups() -> None:
    rng = np.random.default_rng(1)
    rewards, decisions = _population(
        rng, {Group.AGENT: 100, Group.HOLDOUT: 10, Group.BASELINE: 5}, dict.fromkeys(Group, 0.0)
    )
    s = proof_summary(rewards, decisions, day(10))
    assert s.incremental_revenue is None and s.incremental_profit is None and s.roi is None
    assert s.lift_vs_baseline is None
    assert any("Too few" in n and "holdout" in n for n in s.notes)
    # Windows that close after `now` don't count at all.
    assert proof_summary(rewards, decisions, day(5)).agent_n == 0


def test_per_goal_proof_refuses_when_goals_share_customers() -> None:
    rng = np.random.default_rng(2)
    rewards, decisions = _population(
        rng, {Group.AGENT: 60, Group.HOLDOUT: 60, Group.BASELINE: 60}, dict.fromkeys(Group, 0.0)
    )
    alone = proof_summary(rewards, decisions, day(10), goal_id="g1")
    assert alone.incremental_profit is not None and alone.goal_id == "g1"
    extra_d = act("x1", T0, cid="agent-0", goal="g2")
    extra_r = rewards[0].model_copy(update={"decision_id": "x1", "goal_id": "g2"})
    mixed = proof_summary([*rewards, extra_r], [*decisions, extra_d], day(10), goal_id="g1")
    assert mixed.incremental_profit is None and mixed.lift_vs_baseline is None
    assert any("Other goals" in n for n in mixed.notes)


class _RespectsHoldout:
    """Contact everyone except the holdout: just enough of a policy to run the proof end to end."""

    version = "respects-holdout-v0"

    def __init__(self) -> None:
        self._act, self._wait = ContactEveryonePolicy(), NothingPolicy()

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        policy = self._wait if state.group is Group.HOLDOUT else self._act
        return policy.decide(state, ctx)

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


def test_end_to_end_on_the_fake_world() -> None:
    events, log = InMemoryEventStore(), InMemoryDecisionLog()
    brief = sample_brief(T0)
    run_days(
        env=FakeEnvironment(n_customers=120, start=T0, history_days=10),
        policy=_RespectsHoldout(),
        events=events,
        states=InMemoryStateStore(),
        decisions=log,
        folder=SimpleStateFolder(),
        briefs=[brief],
        content=sample_content(T0),
        days=10,
        seed=3,
        assign_group=group_assigner("fake-biz", holdout_share=0.3, baseline_share=0.0),
    )
    decisions = list(log.scan())
    by_customer: dict[str, list[Event]] = {}
    for e in events.scan():
        by_customer.setdefault(e.customer_id, []).append(e)
    costs = Costs.from_brief(brief, channel={Channel.EMAIL: 0.01, Channel.WHATSAPP: 0.05})
    rewards = proof_rewards(decisions, by_customer, 3, Objective.GROSS_PROFIT, costs, now=day(10))
    assert rewards and all(r.window_end <= day(10) for r in rewards)
    held = {d.customer_id for d in decisions if d.group is Group.HOLDOUT}
    assert held and all(d.kind is DecisionKind.NOTHING for d in decisions if d.group is Group.HOLDOUT)
    s = proof_summary(rewards, decisions, day(10), min_group=10)
    assert s.holdout_n == len(held) and s.agent_n + s.holdout_n == 120
    assert s.spend > 0 and s.incremental_profit is not None


# --- off-policy evaluation -------------------------------------------------------------------------

LOGGING = np.array([[0.5, 0.3, 0.2], [0.2, 0.3, 0.5]])  # P(action | context) of the logger
TARGET = np.array([[0.2, 0.0, 0.8], [1.0, 0.0, 0.0]])  # the policy we want to evaluate
MEANS = np.array([[0.10, 0.30, 0.45], [0.60, 0.20, 0.15]])  # true reward rate per context and action
TRUE_VALUE = float(0.5 * (TARGET * MEANS).sum())


def _logged(rng: np.random.Generator, n: int) -> tuple[np.ndarray, ...]:
    x = rng.integers(0, 2, n)
    u = rng.random(n)
    a = (u[:, None] > np.cumsum(LOGGING[x], axis=1)).sum(axis=1)
    r = (rng.random(n) < MEANS[x, a]).astype(float)
    return x, a, r, LOGGING[x, a], TARGET[x, a]


def test_ope_estimators_are_unbiased_on_a_synthetic_logged_bandit() -> None:
    rng = np.random.default_rng(42)
    reps, n = 300, 1500
    wrong_q = np.full((2, 3), 0.3)
    out: dict[str, list[float]] = {"ips": [], "snips": [], "dr_right": [], "dr_wrong": []}
    covered = 0
    for _ in range(reps):
        x, a, r, p, pi = _logged(rng, n)
        est = ips(r, p, pi)
        covered += est.value.low <= TRUE_VALUE <= est.value.high
        out["ips"].append(est.value.point)
        sn = snips(r, p, pi)
        assert sn is not None
        out["snips"].append(sn.value.point)
        for name, q in (("dr_right", MEANS), ("dr_wrong", wrong_q)):
            dr = doubly_robust(r, p, pi, q[x, a], (TARGET[x] * q[x]).sum(axis=1))
            out[name].append(dr.value.point)
    for name, values in out.items():
        v = np.asarray(values)
        assert abs(v.mean() - TRUE_VALUE) < 4 * v.std(ddof=1) / np.sqrt(reps) + 1e-3, name
    assert np.std(out["dr_right"]) < 0.8 * np.std(out["ips"])
    assert 0.9 <= covered / reps <= 0.99


def test_ope_report_validation_and_snips_none() -> None:
    rng = np.random.default_rng(0)
    _, _, r, p, pi = _logged(rng, 200)
    report = evaluate(r, p, pi)
    assert report.dr is None and report.snips is not None and report.n == 200
    assert report.ips.effective_n <= 200
    assert snips(r, p, np.zeros_like(pi)) is None
    with pytest.raises(ValueError):
        ips(r, np.zeros_like(p), pi)
    with pytest.raises(ValueError):
        ips(r[:-1], p, pi)
    with pytest.raises(ValueError):
        ips(r[:1], p[:1], pi[:1])
    with pytest.raises(ValueError):
        evaluate(r, p, pi, q_logged=np.zeros_like(r))
    clipped = ips(r, p, pi, clip=2.0)
    assert clipped.max_weight <= 2.0


def test_from_decisions_keeps_the_agent_log_and_evaluates() -> None:
    decisions = [act(f"a{i}", T0, cid=f"c{i}") for i in range(6)] + [
        nothing(f"h{i}", T0, cid=f"h{i}", group=Group.HOLDOUT) for i in range(3)
    ]
    rewards = [
        Reward(
            decision_id=d.decision_id,
            customer_id=d.customer_id,
            goal_id=None,
            window_end=day(7),
            converted=True,
            net=float(i),
        )
        for i, d in enumerate(decisions)
    ]
    log = from_decisions(decisions, rewards)
    assert len(log.decisions) == 6 and np.allclose(log.logged_prob, 0.5)
    # Evaluating the logging policy itself returns the logged mean exactly.
    same = log.evaluate(lambda d: d.probability)
    assert same.ips.value.point == pytest.approx(log.rewards.mean())
    with_q = log.evaluate(lambda d: 1.0, q=lambda d: (1.0, 1.0))
    assert with_q.dr is not None
