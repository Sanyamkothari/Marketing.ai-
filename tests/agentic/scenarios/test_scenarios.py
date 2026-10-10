"""pass^k and the policy mutation test over the M132 scenarios (Plan K M132, design §5.2).

* Every scenario run ten times gives ten identical outcomes (status, codes, payload digest) that match the
  expectation: pass^k = 1 at k = 10, with the `FakeLLMClient` given to the harness (M132 has no model step, so
  this checks that the tool and policy path is deterministic).
* Removing any one policy rule makes at least one scenario fail - in particular the scenarios written to guard
  that rule - so no rule is untested (policy-violation rate 0).
* The whole suite's ledger verifies afterwards: every proposal, refusal and evidence label it wrote is chained
  and every signed entry's signature holds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.agentic.evals import Scenario, mutation_test, pass_k, write_report
from engine.agentic.ledger import verify_ledger
from engine.agentic.policy import RULES
from engine.agentic.registry import ToolContext
from engine.llm import FakeLLMClient
from tests.agentic.scenarios.scenarios import scenarios
from tests.agentic.scenarios.world import PRINCIPALS, World

pytestmark = pytest.mark.integration


def _factory(world: World) -> object:
    def make(scenario: Scenario) -> ToolContext:
        return world.context(principal=scenario.principal, deployment=scenario.deployment)

    return make


def test_the_suite_covers_every_rule_and_has_at_least_fifteen_scenarios(agentic_world: World) -> None:
    suite = scenarios(agentic_world)
    assert len(suite) >= 15
    assert len({scenario.name for scenario in suite}) == len(suite)
    guarded = {rule for scenario in suite for rule in scenario.guards}
    assert guarded == set(RULES), "every policy rule has a scenario written to catch it"
    assert any("holdout" in scenario.name and scenario.expect_status == "refused" for scenario in suite)


def test_every_scenario_gives_the_same_outcome_ten_times(agentic_world: World, tmp_path: Path) -> None:
    report = pass_k(
        scenarios(agentic_world),
        _factory(agentic_world),  # type: ignore[arg-type]
        principals=PRINCIPALS.__getitem__,
        k=10,
        llm=FakeLLMClient(),
    )
    failures = [(result.name, result.failure) for result in report.scenarios if not result.passed]
    assert failures == []
    assert report.k == 10 and report.pass_k == 1.0 and report.failed == 0
    assert all(result.distinct_outcomes == 1 for result in report.scenarios)
    assert report.llm_model_id is not None and report.llm_calls == 0 and report.cost_usd == 0.0
    path = write_report(report, tmp_path / "agentic_eval.json")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["pass_k"] == 1.0 and len(written["scenarios"]) == len(report.scenarios)


def test_removing_any_policy_rule_is_caught_by_a_scenario(agentic_world: World) -> None:
    suite = scenarios(agentic_world)
    caught = mutation_test(suite, _factory(agentic_world), principals=PRINCIPALS.__getitem__)  # type: ignore[arg-type]
    assert set(caught) == set(RULES)
    assert [rule for rule, names in caught.items() if not names] == [], caught
    for scenario in suite:
        for rule in scenario.guards:
            assert scenario.name in caught[rule], f"{scenario.name} does not notice {rule} missing"


def test_the_ledger_the_suite_wrote_verifies(agentic_world: World) -> None:
    from engine.agentic.ledger import signer_for_data_dir
    from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine

    engine = sqlite_engine(agentic_world.data_dir / PLATFORM_DB_FILENAME)
    report = verify_ledger(engine, signer_for_data_dir(agentic_world.data_dir, create=False))
    assert report.ok, report
