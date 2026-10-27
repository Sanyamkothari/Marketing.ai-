"""The calibration factor (autonomy/decide/calibration.py, docs/v4/CALIBRATION.md): it never inflates, is 1.0 with
no holdout data, recovers a planted over-valuation, and `build_agent_policy` is unchanged unless asked."""

from __future__ import annotations

import inspect
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
    Estimate,
    Group,
    Reward,
)
from autonomy.decide.calibration import (
    FACTOR_MAX,
    FACTOR_MIN,
    Calibration,
    measured_per_action,
    shrink_factor,
)
from autonomy.gym import default_brief, default_content
from autonomy.runtime.agent import AgentPolicy, build_agent_policy

NOW = datetime(2026, 3, 1, 10, tzinfo=UTC)
REACHABLE = ChannelStats(address_known=True, consent=True)


def _decision(i: int, group: Group, expected: float) -> Decision:
    act = group is Group.AGENT
    return Decision(
        decision_id=f"d{group.value}{i}",
        customer_id=f"c{group.value}{i}",
        made_at=NOW,
        kind=DecisionKind.ACT if act else DecisionKind.NOTHING,
        action=Action(goal_id="g", channel=Channel.EMAIL, send_at=NOW) if act else None,
        group=group,
        probability=1.0,
        expected_net=expected if act else 0.0,
        reason="-",
        policy_version="t",
    )


def _reward(d: Decision, profit: float) -> Reward:
    return Reward(
        decision_id=d.decision_id,
        customer_id=d.customer_id,
        goal_id="g",
        window_end=NOW + timedelta(days=14),
        converted=profit > 0,
        margin=profit,
        net=profit,
    )


def _world(
    n: int, expected: float, true_per_action: float, seed: int = 0
) -> tuple[list[Reward], dict[str, Decision]]:
    """`n` agent customers each given one action the agent values at `expected`, and `n` holdout customers; the
    agent group earns `true_per_action` more per customer than the holdout (so per action, one each)."""
    rng = np.random.default_rng(seed)
    base = rng.normal(2.0, 0.5, size=2 * n)
    decisions: dict[str, Decision] = {}
    rewards: list[Reward] = []
    for i in range(n):
        a, h = _decision(i, Group.AGENT, expected), _decision(i, Group.HOLDOUT, expected)
        decisions[a.decision_id], decisions[h.decision_id] = a, h
        rewards.append(_reward(a, float(base[i]) + true_per_action))
        rewards.append(_reward(h, float(base[n + i])))
    return rewards, decisions


def test_shrink_never_exceeds_one_and_never_goes_under_the_floor() -> None:
    for measured_point in (-5.0, -0.1, 0.0, 0.3, 1.0, 3.0, 50.0):
        for half in (1e-6, 0.05, 1.0, 20.0):
            est = Estimate(point=measured_point, low=measured_point - half, high=measured_point + half)
            f = shrink_factor(est, 1.0)
            assert FACTOR_MIN <= f <= FACTOR_MAX == 1.0
    big = Estimate(point=9.0, low=8.9, high=9.1)  # the holdout says it earns far MORE than expected
    assert shrink_factor(big, 1.0) == 1.0  # never inflates
    bad = Estimate(point=-4.0, low=-4.1, high=-3.9)
    assert shrink_factor(bad, 1.0) == FACTOR_MIN


def test_no_holdout_data_means_no_correction() -> None:
    assert shrink_factor(None, 1.0) == 1.0
    assert shrink_factor(Estimate(point=0.1, low=0.0, high=0.2), None) == 1.0
    assert (
        shrink_factor(Estimate(point=0.1, low=0.0, high=0.2), 0.0) == 1.0
    )  # nothing expected, nothing to check
    assert measured_per_action(None, 10) is None
    assert measured_per_action(Estimate(point=1, low=0, high=2), 0) is None
    cal = Calibration()
    assert cal.factor == 1.0 and cal.scale() == 1.0
    assert cal.observe([], {}) == 1.0
    rewards, decisions = _world(
        20, expected=1.0, true_per_action=0.2
    )  # 20 per group: under the proof's minimum of 30
    assert cal.observe(rewards, decisions) == 1.0
    assert cal.measured is None


def test_a_planted_over_valuation_is_recovered() -> None:
    """The agent expects 1.0 per action; the holdout shows 0.5. The factor lands near 0.5 (a little above: the prior)."""
    rewards, decisions = _world(3000, expected=1.0, true_per_action=0.5)
    cal = Calibration()
    f = cal.observe(rewards, decisions)
    assert f == cal.factor
    assert 0.44 < f < 0.58
    assert cal.expected == pytest.approx(1.0) and cal.actions == 3000
    assert cal.measured is not None and cal.measured.point == pytest.approx(0.5, abs=0.05)
    assert cal.observe(rewards, decisions) == f  # the same rewards again change nothing


def test_the_shrink_toward_one_is_stronger_when_the_holdout_is_small() -> None:
    small = Calibration()
    small.observe(*_world(40, expected=1.0, true_per_action=0.5, seed=3))
    large = Calibration()
    large.observe(*_world(4000, expected=1.0, true_per_action=0.5, seed=3))
    assert large.factor < small.factor <= 1.0
    assert small.factor > 0.6  # 40 customers a side barely move a Normal(1, 0.25^2) prior


def test_an_accurate_agent_is_left_alone() -> None:
    cal = Calibration()
    cal.observe(*_world(3000, expected=0.5, true_per_action=0.5))
    assert 0.85 <= cal.factor <= 1.0


def test_the_factor_in_force_is_taken_out_of_the_logged_expectation() -> None:
    """A calibrated agent logs its net with the value shrunk; the next fit must still compare the agent's raw view."""
    rewards, decisions = _world(3000, expected=0.5, true_per_action=0.5)
    for key, d in decisions.items():
        if d.kind is DecisionKind.ACT and d.action is not None:
            # raw: 1.0 of value (uplift 0.1 x 10), no cost; logged at factor 0.5: 0.5
            chosen = Bid(goal_id="g", action=d.action, uplift=0.1, value_if_converted=10, expected_net=0.5)
            decisions[key] = d.model_copy(update={"bids": (chosen,)})
    cal = Calibration()
    cal.factor = 0.5
    for d in decisions.values():
        cal.note(d)  # made while the factor was 0.5
    cal.observe(rewards, decisions)
    assert cal.expected == pytest.approx(1.0)
    assert 0.44 < cal.factor < 0.58


def test_default_build_is_unchanged_and_calibration_is_opt_in() -> None:
    assert inspect.signature(build_agent_policy).parameters["calibrate"].default is False
    assert inspect.signature(AgentPolicy.__init__).parameters["calibrate"].default is False
    plain = build_agent_policy([default_brief()], default_content())
    assert plain.value_calibration is None and plain.policy.value_scale_fn is None
    assert plain.policy._value_scale() == 1.0
    on = build_agent_policy([default_brief()], default_content(), calibrate=True)
    assert on.value_calibration is not None and on.policy.value_scale_fn == on.value_calibration.scale
    assert on.policy._value_scale() == 1.0  # nothing measured yet


def _bid(uplift: float, value: float, costs: float = 0.0, urgency: float = 1.0) -> Bid:
    action = Action(goal_id="g", channel=Channel.EMAIL, send_at=NOW)
    return Bid(
        goal_id="g",
        action=action,
        uplift=uplift,
        value_if_converted=value,
        channel_cost=costs,
        urgency=urgency,
        expected_net=urgency * (uplift * value - costs),
        uncertainty=0.3,
    )


def test_the_policy_shrinks_every_bids_value_by_the_factor() -> None:
    bid = _bid(0.1, 10)
    on = build_agent_policy([default_brief()], default_content(), calibrate=True)
    assert on.value_calibration is not None
    on.value_calibration.factor = 0.5
    scaled = on.policy._scaled(bid, on.policy._value_scale())
    assert scaled.expected_net == pytest.approx(0.5) and scaled.uncertainty == 0.3
    assert on.policy._scaled(bid, 1.0) is bid


def test_only_the_value_is_shrunk_and_a_bid_never_goes_up() -> None:
    """Costs stay whole: a loss-making bid is never moved toward zero, a costly one keeps its full cost."""
    on = build_agent_policy([default_brief()], default_content(), calibrate=True)
    losing = _bid(0.02, 10, costs=1.0)  # 0.2 of value against 1.0 of cost: -0.8
    assert on.policy._scaled(losing, 0.25).expected_net == pytest.approx(0.25 * 0.2 - 1.0)
    harmful = _bid(-0.05, 10, costs=0.3)  # negative uplift: nothing to shrink
    assert on.policy._scaled(harmful, 0.25) is harmful
    costly = _bid(0.2, 10, costs=1.0, urgency=0.5)  # 0.5 x (2.0 - 1.0) = 0.5
    assert on.policy._scaled(costly, 0.5).expected_net == pytest.approx(0.5 * (0.5 * 2.0 - 1.0))
    for bid in (losing, harmful, costly, _bid(0.3, 4, 0.2)):
        for f in (0.25, 0.5, 0.9, 1.0, 3.0):
            assert on.policy._scaled(bid, f).expected_net <= bid.expected_net


def _state(i: int, **kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": f"s{i}",
        "as_of": NOW,
        "channels": {Channel.EMAIL: REACHABLE, Channel.WHATSAPP: REACHABLE},
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def test_decisions_are_identical_without_calibration_and_at_factor_one() -> None:
    """The default build and a calibrated build whose factor is 1.0 decide exactly alike on fixed states."""
    ctx = DecisionContext(now=NOW, briefs=(default_brief(),), content=tuple(default_content()), seed=7)
    states = [
        _state(0),
        _state(1, purchases=1, revenue=80.0, last_purchase_at=NOW - timedelta(days=60)),
        _state(2, purchases=4, revenue=80.0, last_purchase_at=NOW - timedelta(days=20)),
        _state(3, purchases=2, revenue=80.0, last_purchase_at=NOW - timedelta(days=200)),
    ]
    plain = build_agent_policy([default_brief()], default_content())
    on = build_agent_policy([default_brief()], default_content(), calibrate=True)
    for state in states:
        assert plain.decide(state, ctx) == on.decide(state, ctx)


def test_the_event_feed_recovers_a_planted_over_valuation_and_waits_for_closed_windows() -> None:
    """What the Gym and the nightly loop hand the agent: decisions, purchases, a refit (a week apart at most)."""
    n, rng = 3000, np.random.default_rng(1)
    cal = Calibration()
    for i in range(n):
        agent, holdout = _decision(i, Group.AGENT, 1.0), _decision(i, Group.HOLDOUT, 0.0)
        cal.record(agent, cost=0.1)
        cal.record(holdout)
        cal.purchase(
            agent.customer_id, NOW + timedelta(days=3), float(rng.normal(2.0, 0.5)) + 0.6
        )  # +0.5 net of the 0.1 cost
        cal.purchase(holdout.customer_id, NOW + timedelta(days=3), float(rng.normal(2.0, 0.5)))
        cal.purchase(
            holdout.customer_id, NOW - timedelta(days=30), 99.0
        )  # before the agent started: not counted
    assert cal.refit(NOW + timedelta(days=5), 14) == 1.0  # the windows have not closed yet
    assert cal.measured is None
    # Day 18: the cutoff (day 4) is past the day-3 purchases, the same period autonomy.proof measures.
    f = cal.refit(NOW + timedelta(days=18), 14, force=True)
    assert 0.44 < f < 0.58
    assert cal.expected == pytest.approx(1.0) and cal.actions == n
    assert cal.measured is not None and cal.measured.point == pytest.approx(0.5, abs=0.05)
    cal.factor = 0.5
    assert cal.refit(NOW + timedelta(days=19), 14) == 0.5  # inside the week: not refitted


def test_the_event_feed_ignores_purchases_from_sends_that_are_still_open() -> None:
    """Sends go on after the cutoff and earn too: those purchases are not set against the closed sends."""
    n, rng = 3000, np.random.default_rng(2)
    cal = Calibration()
    for i in range(n):
        agent, holdout = _decision(i, Group.AGENT, 1.0), _decision(i, Group.HOLDOUT, 0.0)
        cal.record(agent)
        cal.record(holdout)
        cal.purchase(agent.customer_id, NOW + timedelta(days=2), float(rng.normal(2.0, 0.5)) + 0.5)
        cal.purchase(holdout.customer_id, NOW + timedelta(days=2), float(rng.normal(2.0, 0.5)))
        late = agent.model_copy(update={"decision_id": f"late{i}", "made_at": NOW + timedelta(days=20)})
        cal.record(late)  # a second send, still open at the refit
        cal.purchase(agent.customer_id, NOW + timedelta(days=21), 3.0)  # what the open send earned
    f = cal.refit(NOW + timedelta(days=26), 14)  # cutoff day 12: only the first sends are closed
    assert cal.actions == n
    assert cal.measured is not None and cal.measured.point == pytest.approx(0.5, abs=0.05)
    assert 0.44 < f < 0.58


@pytest.mark.slow
def test_in_a_small_gym_world_the_calibrated_agent_is_the_plain_agent_until_the_holdout_can_be_measured() -> (
    None
):
    """200 customers have fewer than the proof's 30 holdout customers, so the factor stays 1.0 and every
    number is the plain agent's. The calibration is still fed: it has seen the agent's contacts."""
    from autonomy.gym import get_scenario
    from autonomy.gym.run import POLICIES
    from autonomy.gym.score import run_policy

    spec = get_scenario("baseline_shop", n_customers=200, days=40)
    built: list[AgentPolicy] = []

    def calibrated(world: object) -> AgentPolicy:
        agent = POLICIES["agent_calibrated"](world)  # type: ignore[arg-type]
        assert isinstance(agent, AgentPolicy)
        built.append(agent)
        return agent

    on = run_policy(spec, calibrated, 0, with_oracle=False)
    off = run_policy(spec, POLICIES["agent"], 0, with_oracle=False)
    cal = built[0].value_calibration
    assert cal is not None and cal.factor == 1.0 and cal.refit_at is not None and cal._sent
    assert on.violations == 0
    assert on.true_value == off.true_value and on.actions == off.actions
