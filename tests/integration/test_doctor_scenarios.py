"""The Data Doctor scenarios (AI-first build brief, Stage 0): the real app, a person who is a script, a replay model.

Each scenario folder of `tests/fixtures/doctor_scenarios` is run once with the AI and once without it. Every
checkpoint of its `expect.json` (a named step of the person's script, or `final`) is one test, and so is every
invariant. The suite is honest about the product: where the Data Doctor is not AI-first yet, the check is marked
`xfail(strict=True)` with the stage that fixes it, taken from the scenario's `expect.json` (`"xfail"`). A check that
starts to pass turns into an error, so its mark is removed in the commit that fixes it; every other check must pass.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.doctor_scenario_checks import INVARIANTS, same_plan_without_ai
from scripts.doctor_scenarios import SCENARIOS_DIR, ScenarioResult, run_scenario

pytestmark = [pytest.mark.integration]

SCENARIO_PATHS = sorted(p for p in SCENARIOS_DIR.iterdir() if p.is_dir())
SAME_PLAN = "same_plan_without_ai"
REASON_PREFIX = "AI-first Stage "


def _expect(scenario: Path) -> dict[str, Any]:
    return json.loads((scenario / "expect.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _checkpoints(expect: dict[str, Any]) -> list[str]:
    return [*expect.get("after", {}), *(["final"] if "final" in expect else [])]


def _marks(scenario: Path, key: str) -> list[pytest.MarkDecorator]:
    reason = _expect(scenario).get("xfail", {}).get(key)
    # A scenario's run takes seconds: `--dist loadgroup` (the nightly run) keeps its checks on one worker.
    marks = [pytest.mark.xdist_group(f"doctor_scenario_{scenario.name}")]
    return [*marks, pytest.mark.xfail(strict=True, reason=reason)] if reason else marks


CHECKPOINT_CASES = [
    pytest.param(p.name, checkpoint, marks=_marks(p, checkpoint), id=f"{p.name}::{checkpoint}")
    for p in SCENARIO_PATHS
    for checkpoint in _checkpoints(_expect(p))
]
INVARIANT_CASES = [
    pytest.param(
        p.name, invariant, marks=_marks(p, f"invariant:{invariant}"), id=f"{p.name}::invariant:{invariant}"
    )
    for p in SCENARIO_PATHS
    for invariant in INVARIANTS
]


def _tuples(value: Any) -> Any:
    return tuple(_tuples(v) for v in value) if isinstance(value, list) else value


@pytest.fixture(scope="session")
def ran(tmp_path_factory: pytest.TempPathFactory, worker_id: str) -> Callable[[str, bool], ScenarioResult]:
    """A scenario's result, run once for the whole session: under `pytest -n` the workers share it through files in
    the run's temp folder, and the one that gets there first runs the scenario while the others wait for it.
    """
    base = tmp_path_factory.getbasetemp()
    folder = (base if worker_id == "master" else base.parent) / "doctor_scenarios"
    folder.mkdir(exist_ok=True)

    def get(scenario: str, with_ai: bool) -> ScenarioResult:
        stem = folder / f"{scenario}.{'ai' if with_ai else 'no_ai'}"
        with Path(f"{stem}.lock").open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = Path(f"{stem}.json")
            if not saved.exists():
                saved.write_text(
                    json.dumps(dataclasses.asdict(run_scenario(SCENARIOS_DIR / scenario, with_ai=with_ai)))
                )
            data = json.loads(saved.read_text(encoding="utf-8"))
        return ScenarioResult(**{**data, "plan_kind": _tuples(data["plan_kind"])})

    return get


@pytest.mark.parametrize(("scenario", "checkpoint"), CHECKPOINT_CASES)
def test_expectation(ran: Callable[[str, bool], ScenarioResult], scenario: str, checkpoint: str) -> None:
    """What `expect.json` says must be true after a named step of the person's script, or at the end."""
    failures = ran(scenario, True).checkpoints[checkpoint]
    assert not failures, f"{scenario} after '{checkpoint}': {failures}"


@pytest.mark.parametrize(("scenario", "invariant"), INVARIANT_CASES)
def test_invariant(ran: Callable[[str, bool], ScenarioResult], scenario: str, invariant: str) -> None:
    """An invariant of the brief holds after every step of the scenario, with the AI and without it."""
    with_ai = ran(scenario, True)
    without_ai = ran(scenario, False)
    if invariant == SAME_PLAN:
        problems = same_plan_without_ai(with_ai.plan_kind, without_ai.plan_kind)
        problems += [f"without an AI the person's script: {n}" for n in without_ai.notes if problems]
    else:
        problems = [
            *with_ai.invariants[invariant],
            *(f"without an AI: {v}" for v in without_ai.invariants[invariant]),
        ]
    assert not problems, f"{scenario}, invariant {invariant}: {problems}"


@pytest.mark.parametrize("scenario", SCENARIO_PATHS, ids=lambda p: p.name)
def test_every_recording_was_made_on_the_instructions_it_replays_on(
    ran: Callable[[str, bool], ScenarioResult], scenario: Path
) -> None:
    """A recorded reply was given to the instructions the AI was shown then. When the prompt, the actions or the tools
    change, the scenario must be recorded again (`python -m scripts.doctor_scenarios --scenario <n> --seal`).
    """
    stale = ran(scenario.name, True).stale
    assert not stale, f"{scenario.name} needs re-recording: {stale}"


@pytest.mark.parametrize("scenario", SCENARIO_PATHS, ids=lambda p: p.name)
def test_every_xfail_names_a_real_check_and_the_stage_that_fixes_it(scenario: Path) -> None:
    """A typo in `"xfail"` would leave a failing check unmarked or a mark on nothing: both are refused."""
    expect = _expect(scenario)
    real = {*_checkpoints(expect), *(f"invariant:{i}" for i in INVARIANTS)}
    for key, reason in expect.get("xfail", {}).items():
        assert key in real, f"{scenario.name}: xfail '{key}' is not a checkpoint or an invariant"
        assert reason.startswith(
            REASON_PREFIX
        ), f"{scenario.name}: the reason of '{key}' names no stage: {reason!r}"
