"""Integration test wrapper for Data Doctor scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.doctor_scenarios import SCENARIOS_DIR, run_scenario

SCENARIO_PATHS = sorted([p for p in SCENARIOS_DIR.iterdir() if p.is_dir()])


@pytest.mark.parametrize("scenario_dir", SCENARIO_PATHS, ids=lambda p: p.name)
def test_doctor_scenario(scenario_dir: Path) -> None:
    """Run a doctor scenario and verify expectation checks pass."""
    result = run_scenario(scenario_dir, with_ai=True)
    assert (
        not result.expectation_failures
    ), f"Expectation failures in {scenario_dir.name}: {result.expectation_failures}"
