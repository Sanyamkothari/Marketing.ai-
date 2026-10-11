"""SELECT reads M137's rollout verdict, and a crashed plan resumes from its last step (Plan K M139).

The world's model was trained without `uplift.evidence.rollout_verdict`, so its stored comparison has no
verdict; these tests stand a registry in front of the real one that adds a verdict to `evaluate_policy`'s answer
(the M137 field, `risk_comparison.rollout.outcome`), as a run with the switch on would store it. Everything else
the planner reads is the world's own.
"""

from __future__ import annotations

from typing import Any

import pytest

import engine.agentic.planner as planner
from engine.agentic.planner import PlannerDeps, PlanRequest, load_plan, resume_plan, start_plan
from engine.agentic.policy import CampaignPlanPayload
from engine.agentic.proposals import ProposalStore
from engine.agentic.registry import ToolResult
from engine.agentic.tools import default_registry
from tests.agentic.scenarios.planner_scenarios import expected_digest
from tests.agentic.scenarios.planner_world import PlannerWorld

pytestmark = pytest.mark.integration

FORM = {"goal": {"use_case_id": "win-back-campaign", "channel": "sms"}}


class VerdictRegistry:
    """The real registry, with M137's verdict set on the stored equal-budget comparison it returns."""

    def __init__(self, outcome: str) -> None:
        self._real = default_registry()
        self._outcome = outcome

    def call(self, name: str, given: Any, ctx: Any) -> ToolResult:
        result = self._real.call(name, given, ctx)
        if name != "evaluate_policy" or not result.output.get("risk_comparison"):
            return result
        output = dict(result.output)
        output["risk_comparison"] = {**output["risk_comparison"], "rollout": {"outcome": self._outcome}}
        return result.model_copy(update={"output": output})


def _plan(pw: PlannerWorld, registry: Any) -> Any:
    return start_plan(PlanRequest.model_validate(FORM), pw.world.context(), PlannerDeps(registry=registry))


def test_an_uplift_list_that_is_not_safe_to_roll_out_is_never_selected(planner_world: PlannerWorld) -> None:
    state = _plan(planner_world, VerdictRegistry("not_safe"))
    assert state.step == "STOPPED" and state.stopped is not None
    assert state.stopped.code == "PLANNER_NO_CANDIDATE" and "not safe to roll out" in state.stopped.message
    assert state.proposal_id is None and all(c.excluded for c in state.candidates)


@pytest.mark.parametrize("outcome", ["safe", "cannot_tell"])
def test_a_safe_or_undecided_verdict_is_planned_and_recorded(
    planner_world: PlannerWorld, outcome: str
) -> None:
    state = _plan(planner_world, VerdictRegistry(outcome))
    assert state.step == "INTERRUPT" and state.selected is not None and state.selected.rollout == outcome
    proposal = ProposalStore(planner_world.world.storage).get(state.proposal_id)
    assert CampaignPlanPayload.model_validate(proposal.payload).rollout == outcome


def test_a_plan_that_crashed_between_steps_resumes_from_the_last_one_written(
    planner_world: PlannerWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = planner_world
    ctx = pw.world.context()
    before = set(ctx.storage.list_keys(planner.PLANS_PREFIX))
    real = planner._candidates

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the worker was stopped")

    monkeypatch.setattr(planner, "_candidates", crash)
    with pytest.raises(RuntimeError):
        start_plan(PlanRequest.model_validate(FORM), ctx, PlannerDeps(registry=default_registry()))
    (key,) = set(ctx.storage.list_keys(planner.PLANS_PREFIX)) - before
    plan_id = key.split("/")[1]
    halted = load_plan(ctx, plan_id)
    assert halted.step == "SIZE" and halted.size is not None and halted.proposal_id is None
    monkeypatch.setattr(planner, "_candidates", real)
    resumed = resume_plan(plan_id, ctx, PlannerDeps(registry=default_registry()))
    assert resumed.step == "INTERRUPT"
    assert [t.step for t in resumed.history] == [
        "FRAME",
        "SIZE",
        "CANDIDATES",
        "SELECT",
        "CHECK",
        "DRAFT",
        "INTERRUPT",
    ]
    assert resumed.payload_digest == expected_digest(pw, objective="next_cycle")
    assert (
        resume_plan(plan_id, ctx, PlannerDeps(registry=default_registry())) == resumed
    ), "nothing runs twice"
