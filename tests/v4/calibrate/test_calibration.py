"""The calibration factor (autonomy/decide/calibration.py, docs/v4/CALIBRATION.md): it never inflates, is 1.0 with
no holdout data, recovers a planted over-valuation, and `build_agent_policy` is unchanged unless asked."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import Action, Bid, Channel, Decision, DecisionKind, Estimate, Group, Reward
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
    assert shrink_factor(Estimate(point=0.1, low=0.0, high=0.2), 0.0) == 1.0  # nothing expected, nothing to check
    assert measured_per_action(None, 10) is None
    assert measured_per_action(Estimate(point=1, low=0, high=2), 0) is None
    cal = Calibration()
    assert cal.factor == 1.0 and cal.scale() == 1.0
    assert cal.observe([], {}) == 1.0
    rewards, decisions = _world(20, expected=1.0, true_per_action=0.2)  # 20 per group: under the proof's minimum of 30
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


def test_the_factor_in_force_is_divided_out_of_the_logged_expectation() -> None:
    """A calibrated agent logs expected_net x factor; the next fit must still compare the agent's raw view."""
    rewards, decisions = _world(3000, expected=0.5, true_per_action=0.5)  # logged 0.5 = raw 1.0 x factor 0.5
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


def test_the_policy_multiplies_every_bid_by_the_factor() -> None:
    action = Action(goal_id="g", channel=Channel.EMAIL, send_at=NOW)
    bid = Bid(goal_id="g", action=action, uplift=0.1, value_if_converted=10, expected_net=1.0, uncertainty=0.3)
    on = build_agent_policy([default_brief()], default_content(), calibrate=True)
    assert on.value_calibration is not None
    on.value_calibration.factor = 0.5
    scaled = on.policy._scaled(bid, on.policy._value_scale())
    assert scaled.expected_net == pytest.approx(0.5) and scaled.uncertainty == 0.3
    assert on.policy._scaled(bid, 1.0) is bid
