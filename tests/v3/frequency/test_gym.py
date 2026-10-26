"""The frequency agent in the Gym: one small run, to see that it works end to end (CI runs it)."""

from __future__ import annotations

import pytest

from autonomy.decide.frequency import LookalikeHazard
from autonomy.gym import default_brief, default_content, get_scenario
from autonomy.gym.score import run_policy
from autonomy.runtime.agent import AgentPolicy, build_agent_policy


@pytest.mark.slow
def test_the_frequency_agent_runs_in_the_gym_and_learns_from_it() -> None:
    spec = get_scenario("baseline_shop", n_customers=200, days=90, history_days=30)
    built: list[AgentPolicy] = []

    def factory(world: object) -> AgentPolicy:
        policy = build_agent_policy([default_brief()], default_content(), lookalike=True, frequency=True)
        built.append(policy)
        return policy

    card = run_policy(spec, factory, 0, with_oracle=False)
    policy = built[0]
    plans = policy.planner
    assert plans is not None
    held = sum(1 for _when, name, _value in plans.last.values() if name == "wait_14d")
    hazard = policy.contact_price.hazard
    assert isinstance(hazard, LookalikeHazard)
    print(
        f"true_value={card.true_value:.1f} violations={card.violations} "
        f"customers_planned={len(plans.last)} waiting_now={held} chosen={plans.chosen} "
        f"hazard_rows={hazard.rows} ready={hazard.ready}"
    )
    assert card.violations == 0
    assert plans.last and sum(plans.chosen.values()) >= len(plans.last)
