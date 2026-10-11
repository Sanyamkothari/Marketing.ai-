"""pass^k over the planner's scenarios, with models that word things differently (Plan K M139, design §5.2).

* Every scenario run ten times gives ten identical outcomes (status, codes, payload digest) that match the
  digest computed independently from the stored artefacts: pass^k = 1 at k = 10, with a fake model whose
  narrative wording changes from one call to the next (so digests provably exclude prose).
* The narrative an honest model writes is the model's (and varies); an adversarial model's is replaced by the
  rules-only summary and its failures are counted by code in the report; one slip is dropped.
* No rendered sentence carries a number the evidence does not ground (Plan G's check over every narrative).
* The ledger the suite wrote verifies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engine.agentic.evals import PlannerScenarioRequest, Scenario, pass_k, run_once, write_report
from engine.agentic.ledger import signer_for_data_dir, verify_ledger
from engine.agentic.planner import PlannerDeps, PlanRequest, start_plan
from engine.agentic.registry import ToolContext, ToolRegistry
from engine.agentic.tools import default_registry
from engine.config import BudgetConfig, LlmConfig
from engine.generative.budget import Meter, ModelPrice, PriceTable
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from tests.agentic.scenarios.planner_scenarios import expected_digest, planner_scenarios, synthetic_cards
from tests.agentic.scenarios.planner_world import MODELS, HonestModel, PlannerWorld
from tests.agentic.scenarios.world import PRINCIPALS

pytestmark = pytest.mark.integration

PRICES = PriceTable(prices={LlmConfig().generation_model: ModelPrice(input_per_1m=1.0, output_per_1m=2.0)})
"""A price for the fakes' model id, so the report's cost is the Meter's sum (the shipped table is empty)."""


class Harness:
    """The planner deps per scenario: its card source and its model (one per scenario, so wording varies)."""

    def __init__(self, pw: PlannerWorld) -> None:
        self.pw = pw
        self.models: dict[str, Any] = {}

    def context(self, scenario: Scenario) -> ToolContext:
        return self.pw.world.context(principal=scenario.principal, deployment=scenario.deployment)

    def deps(self, scenario: Scenario, registry: ToolRegistry) -> PlannerDeps:
        request: Any = scenario.request
        meter = None
        if request.llm is not None:
            model = self.models.setdefault(scenario.name, MODELS[request.llm]())
            meter = Meter(
                model, job_id=scenario.name, llm=LlmConfig(), budget=BudgetConfig(cache=False), prices=PRICES
            )
        cards = None if scenario.name.startswith("real_") else (lambda: synthetic_cards(self.pw))
        return PlannerDeps(registry=registry, cards=cards, meter=meter)


def test_the_suite_covers_every_card_type_and_both_kinds_of_goal(planner_world: PlannerWorld) -> None:
    from engine.measurement.summary import CARD_CODES

    suite = planner_scenarios(planner_world)
    names = {scenario.name for scenario in suite}
    assert len(names) == len(suite) >= 20
    assert {f"card_{kind}" for kind in CARD_CODES} <= names
    plans = [scenario.request.plan for scenario in suite]  # type: ignore[union-attr]
    assert any("goal" in plan for plan in plans) and any("goal_text" in plan for plan in plans)


def test_every_planner_scenario_gives_the_same_outcome_ten_times(
    planner_world: PlannerWorld, tmp_path: Path
) -> None:
    harness = Harness(planner_world)
    report = pass_k(
        planner_scenarios(planner_world),
        harness.context,
        principals=PRINCIPALS.__getitem__,
        k=10,
        llm=HonestModel(),
        planner=harness.deps,
    )
    failures = [(result.name, result.failure) for result in report.scenarios if not result.passed]
    assert failures == []
    assert report.pass_k == 1.0 and all(result.distinct_outcomes == 1 for result in report.scenarios)
    # The models were called (words, narratives) and the adversarial one's claims were caught, by code.
    assert report.llm_calls > 0
    assert report.claim_failures.get("CLAIM_FREE_TEXT", 0) >= 20  # adversarial and one-slip, ten runs each
    assert report.claim_failures.get("CLAIM_UNREFERENCED", 0) >= 10
    assert report.claim_failures.get("CLAIM_DIRECTION", 0) >= 10  # the adversary's wrong direction, each run
    assert report.narrative_errors == {}, "no AI service error is counted as a claim failure"
    assert report.cost_usd > 0.0, "the Meter's spend is totalled, not left at 0"
    written = json.loads(write_report(report, tmp_path / "agentic_eval.json").read_text(encoding="utf-8"))
    assert written["claim_failures"] == report.claim_failures


def _run(pw: PlannerWorld, model: Any, plan: dict[str, Any]) -> Any:
    return _run_with(pw, Meter(model, job_id="t", llm=LlmConfig(), budget=BudgetConfig(cache=False)), plan)


def _run_with(pw: PlannerWorld, meter: Meter, plan: dict[str, Any]) -> Any:
    return start_plan(
        PlanRequest.model_validate(plan),
        pw.world.context(),
        PlannerDeps(registry=default_registry(), meter=meter),
    )


def test_wording_varies_and_the_digest_does_not(planner_world: PlannerWorld) -> None:
    model = HonestModel()
    plan = {"goal": {"use_case_id": "win-back-campaign", "channel": "sms"}}
    states = [_run(planner_world, model, plan) for _ in range(3)]
    assert len({state.payload_digest for state in states}) == 1
    texts = {state.narrative.text for state in states}
    assert len(texts) == 3, "three wordings of the same verified claims"
    assert all(state.narrative.source == "model" and not state.narrative.failures for state in states)


def test_an_adversarial_narrative_is_replaced_and_a_slip_is_dropped(planner_world: PlannerWorld) -> None:
    from tests.agentic.scenarios.planner_world import AdversarialModel, OneSlipModel

    plan = {"goal": {"use_case_id": "win-back-campaign", "channel": "sms"}}
    bad = _run(planner_world, AdversarialModel(), plan)
    assert bad.narrative.source == "rules" and bad.narrative.replaced
    assert {"CLAIM_DIRECTION", "CLAIM_FREE_TEXT", "CLAIM_UNREFERENCED"} <= {
        f.code for f in bad.narrative.failures
    }
    assert "4,200" not in bad.narrative.text
    slip = _run(planner_world, OneSlipModel(), plan)
    assert slip.narrative.source == "model" and [f.code for f in slip.narrative.failures] == [
        "CLAIM_FREE_TEXT"
    ]
    assert "12%" not in slip.narrative.text
    plain = start_plan(
        PlanRequest.model_validate(plan),
        planner_world.world.context(),
        PlannerDeps(registry=default_registry()),
    )
    assert plain.narrative.source == "rules" and not plain.narrative.failures
    assert bad.payload_digest == slip.payload_digest == plain.payload_digest


def test_no_rendered_sentence_carries_an_ungrounded_number(planner_world: PlannerWorld) -> None:
    """Every number on the screen is a slot the renderer filled from a tool result (I2)."""
    from engine.agent.grounding import grounded_numbers, ungrounded_numbers
    from engine.agentic.proposals import ProposalStore
    from tests.agentic.scenarios.planner_world import AdversarialModel, OneSlipModel

    plan = {"goal": {"use_case_id": "win-back-campaign", "channel": "sms"}}
    for model in (HonestModel(), AdversarialModel(), OneSlipModel(), None):
        state = (
            _run(planner_world, model, plan)
            if model is not None
            else start_plan(
                PlanRequest.model_validate(plan),
                planner_world.world.context(),
                PlannerDeps(registry=default_registry()),
            )
        )
        evidence = grounded_numbers(item.output for item in state.evidence.values())
        stored = ProposalStore(planner_world.world.storage).get(state.proposal_id)
        assert stored.narrative == state.narrative.model_dump(mode="json")
        for sentence in state.narrative.sentences:
            text = sentence.text.replace(state.proposal_id, "")  # the id's hex is a name, not a number
            assert ungrounded_numbers(text, evidence) == [], (type(model).__name__, sentence.text)


def test_a_card_scenario_can_be_rerun_alone(planner_world: PlannerWorld) -> None:
    harness = Harness(planner_world)
    scenario = next(s for s in planner_scenarios(planner_world) if s.name == "real_card_underpowered")
    outcome = run_once(
        scenario, harness.context, default_registry(), principals=PRINCIPALS.__getitem__, planner=harness.deps
    )
    assert outcome.status == "pending" and outcome.payload_digest == scenario.expect_digest


def test_the_ledger_the_planner_wrote_verifies(planner_world: PlannerWorld) -> None:
    engine = sqlite_engine(planner_world.world.data_dir / PLATFORM_DB_FILENAME)
    report = verify_ledger(engine, signer_for_data_dir(planner_world.world.data_dir, create=False))
    assert report.ok, report


def test_an_ai_service_error_is_counted_apart_from_claim_failures(planner_world: PlannerWorld) -> None:
    """A model that cannot answer (a timeout, the budget spent) is not a wrong claim: the rules write the
    narrative, nothing is counted as CLAIM_MALFORMED, and the report counts the error by its code."""
    from engine.llm import LLMError

    class Down(HonestModel):
        def complete(self, prompt: str, **kwargs: Any) -> Any:
            raise LLMError("LLM_UNAVAILABLE", "The AI service did not answer in time.")

    scenario = Scenario(
        name="narrative_service_down",
        description="The AI service fails while writing the narrative: the rules write it.",
        request=PlannerScenarioRequest(plan={"goal": {"use_case_id": "win-back-campaign", "channel": "sms"}}),
        expect_status="pending",
        expect_digest=expected_digest(planner_world, objective="next_cycle"),
    )

    def deps(_scenario: Scenario, registry: ToolRegistry) -> PlannerDeps:
        meter = Meter(Down(), job_id="down", llm=LlmConfig(), budget=BudgetConfig(cache=False))
        return PlannerDeps(registry=registry, meter=meter)

    harness = Harness(planner_world)
    report = pass_k([scenario], harness.context, principals=PRINCIPALS.__getitem__, k=2, planner=deps)
    assert report.pass_k == 1.0, report.scenarios[0].failure
    assert report.claim_failures == {} and report.narrative_errors == {"LLM_UNAVAILABLE": 2}
    budget = Meter(
        HonestModel(), job_id="b", llm=LlmConfig(), budget=BudgetConfig(cache=False, max_calls_per_run=1)
    )
    state = _run_with(planner_world, budget, {"goal_text": "Win back the customers who lapsed, by sms."})
    assert state.narrative is not None and state.narrative.source == "rules" and not state.narrative.failures
    assert state.narrative_error == "BUDGET_EXCEEDED" and state.llm_calls == 1
