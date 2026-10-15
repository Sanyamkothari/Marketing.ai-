"""Fixtures shared by every test module.

Only the two path fixtures live here (design §11): everything else a test needs is defined in the
module that needs it, so owners never contend over this file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# The deterministic fake language model is a test tool: a real run with no AI service connected is
# refused with 409 AI_NOT_CONNECTED (DEC-1140), and the suite opts in here. Tests of that refusal
# switch it off for themselves. `setdefault`, so a run that exports the variable itself still wins.
os.environ.setdefault("MARKETING_AI_ALLOW_FAKE_AI", "1")


# Tests of FROZEN features (v1 plan, decision 5): the code stays and still has tests, but a push no
# longer waits for them. `make test` deselects the `frozen` marker; `make test-all` (the nightly)
# runs them. A path matches when it starts with one of these prefixes, so a prefix ending in a
# directory covers the directory and one ending in `test_x` covers `test_x*.py`. Keep what the
# "Measure the campaign" step relies on in `STILL_FAST`. Unfreeze a feature by deleting its line.
FROZEN_PATHS: tuple[str, ...] = (
    # uplift workbench and evidence extras (the measure tests stay in the fast run, see above)
    "tests/unit/uplift/",
    "tests/integration/uplift/",
    # offer catalogue and arbitration, offer choice, variant arms
    "tests/unit/decide/test_arbitrate",
    "tests/unit/decide/test_catalogue",
    "tests/unit/decide/test_offer",
    "tests/unit/decide/test_policy_offers",
    "tests/unit/decide/test_multi_arm_model",
    "tests/unit/decide/test_variant_arms",
    "tests/unit/decide/test_plan_j_codes",
    "tests/unit/decide/test_scheduled_",
    "tests/unit/decide/test_arms_card_js",
    "tests/unit/decide/test_conflicts_card_js",
    "tests/unit/decide/test_offer_choice_card_js",
    "tests/integration/decide/test_arbitration",
    "tests/integration/decide/test_catalogue",
    "tests/integration/decide/test_control_group_arbitration",
    "tests/integration/decide/test_offer",
    "tests/integration/decide/test_policy_offer",
    "tests/integration/decide/test_multi_arm_flow",
    "tests/integration/decide/test_variant_arms_journey",
    # generative copy, root-cause analysis, document assistant
    "tests/unit/generative/",
    "tests/integration/test_api_generative",
    "tests/integration/test_assistant_manage_ui",
    # Plan K agentic layer: campaign planner, ledger, MCP, compliance gate
    "tests/agentic/",
    "tests/unit/agentic/",
    "tests/unit/mcp/",
    "tests/integration/mcp/",
    "tests/unit/compliance/",
    "tests/integration/compliance/",
    # pilot and proof packs
    "tests/unit/pilot/",
    "tests/integration/pilot/",
    # schedules
    "tests/unit/production/test_schedule",
    "tests/unit/production/test_scheduling",
    "tests/unit/production/test_local_scheduler",
    "tests/unit/production/test_eventbridge_scheduler",
    "tests/unit/production/test_fire_schedule_cli",
    "tests/unit/production/test_firing",
    "tests/unit/production/test_cron",
    "tests/unit/production/test_audit_export_schedule",
    "tests/unit/measurement/test_cycle_schedules",
    "tests/integration/production/test_schedul",
    "tests/integration/production/test_scheduling",
    # AWS extras
    "tests/unit/test_aws_",
    "tests/unit/test_s3_",
    "tests/unit/test_sagemaker_",
    "tests/integration/test_jobs_as_sagemaker",
    # privacy / DPDP screens
    "tests/unit/production/test_privacy",
    "tests/unit/production/test_erasure",
    "tests/unit/production/test_case_privacy",
    "tests/integration/production/test_privacy",
)

# Under a frozen prefix, but "Measure the campaign" (step 4) relies on them, so they stay in `make test`.
STILL_FAST: tuple[str, ...] = (
    "tests/unit/uplift/test_measure",
    "tests/unit/uplift/test_incrementality",
    "tests/unit/uplift/test_uplift_config",
    "tests/integration/uplift/test_measure_campaign",
    "tests/integration/uplift/test_campaign_results_phase1",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every test under `FROZEN_PATHS` as `frozen` (nothing is skipped or deleted)."""
    root = Path(__file__).resolve().parent.parent
    for item in items:
        try:
            rel = Path(str(item.path)).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if rel.startswith(FROZEN_PATHS) and not rel.startswith(STILL_FAST):
            item.add_marker(pytest.mark.frozen)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository checkout root (the directory holding pyproject.toml)."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def config_root(repo_root: Path) -> Path:
    """The ``configs/`` directory of the checkout."""
    return repo_root / "configs"
