"""SELECT reads M137's rollout verdict, and a crashed plan resumes from its last step (Plan K M139).

The world's model was trained without `uplift.evidence.rollout_verdict`, so its stored comparison has no
verdict; these tests stand a registry in front of the real one that adds a verdict to `evaluate_policy`'s answer
(the M137 field, `risk_comparison.rollout.outcome`), as a run with the switch on would store it. Everything else
the planner reads is the world's own.
"""

from __future__ import annotations

from datetime import UTC
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


def test_an_amount_outcome_is_planned_as_an_amount_never_as_a_rate(fresh_planner_world: PlannerWorld) -> None:
    """The use case's newest measured campaign measured an amount (M102): the drafted test plan is an amount's
    (`outcome_kind: continuous`, no rate or points), so the next cycle is not measured as yes/no; a smallest
    effect given in points for an amount is refused with a plain reason."""
    from engine.measurement.campaign import REPORT_FILENAME, campaign_key
    from engine.uplift.contracts import IncrementalityReport

    pw = fresh_planner_world
    storage = pw.world.storage
    key = campaign_key(pw.measured_campaign, REPORT_FILENAME)
    report = storage.read_model(key, IncrementalityReport)
    storage.write_model(
        key,
        report.model_copy(
            update={
                "outcome_kind": "continuous",
                "treated_rate": None,
                "control_rate": None,
                "absolute_lift": None,
                "incremental_conversions": None,
                "treated_mean": 1200.0,
                "control_mean": 1000.0,
                "mean_difference": 200.0,
                "mean_difference_ci": report.absolute_lift,
            }
        ),
    )
    state = _plan(pw, default_registry())
    assert state.step == "INTERRUPT", state.stopped
    assert (
        state.size is not None
        and state.size.outcome_kind == "continuous"
        and "plan_test" not in state.evidence
    )
    proposal = ProposalStore(storage).get(state.proposal_id)
    plan = CampaignPlanPayload.model_validate(proposal.payload).test_plan
    assert plan is not None and plan.outcome_kind == "continuous"
    assert plan.mde_pp is None and plan.base_rate is None and plan.base_rate_source is None
    in_points = start_plan(
        PlanRequest.model_validate(
            {"goal": {"use_case_id": "win-back-campaign", "channel": "sms", "mde_pp": 2.0}}
        ),
        pw.world.context(),
        PlannerDeps(registry=default_registry()),
    )
    assert in_points.stopped is not None and in_points.stopped.code == "PLANNER_NO_CANDIDATE"
    assert "amount" in in_points.stopped.message


def test_an_approval_too_late_for_the_drafted_analysis_date_is_stale(planner_world: PlannerWorld) -> None:
    """The campaign starts at the approval: a draft whose analysis date would close the outcome window early
    is refused with PROPOSAL_STALE before anything is built."""
    import dataclasses
    from datetime import datetime, time, timedelta

    from engine.agentic.errors import AgenticError
    from engine.agentic.planner import ExecuteActions, execute_plan

    pw = planner_world
    state = _plan(pw, default_registry())
    proposal = ProposalStore(pw.world.storage).get(state.proposal_id)
    plan = CampaignPlanPayload.model_validate(proposal.payload).test_plan
    assert plan is not None and plan.outcome_window_days is not None
    late = datetime.combine(plan.analysis_date, time(12), UTC) - timedelta(days=plan.outcome_window_days - 1)
    made: list[str] = []

    def never(*args: Any) -> Any:
        made.append(str(args))
        raise AssertionError("nothing is built for a stale plan")

    actions = ExecuteActions(build_treat_list=never, open_campaign=never, register_plan=never)
    with pytest.raises(AgenticError) as refused:
        execute_plan(proposal, dataclasses.replace(pw.world.context(), clock=lambda: late), actions)
    assert refused.value.code == "PROPOSAL_STALE" and made == []
