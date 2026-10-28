"""Architecture: an approved recipe is applied to a file in one place (roadmap 2.1, "Old replay paths").

`engine/doctor/materialize.py` (`replay_recipe`, beside `materialize_plan`) is the only module that runs a stored
recipe's steps on new rows: training from a file, scoring, a scheduled firing and the prescription's replay all reach
it through `engine.agent.dataset_recipe.run_replay` / `run_saved_recipe`, which only delegate. A module outside
`engine/doctor` that calls `run_recipe`, `run_guarded` or `run_sql` is a second replay that drifts from the first, and
fails here.

The modules listed below call a runner for a reason of their own (each says which: they try a step while it is being
written, they are not a replay of an approved recipe). The list may only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNNERS = frozenset({"run_recipe", "run_guarded", "run_sql"})
REPLAYERS = frozenset({"run_replay", "run_saved_recipe"})
THE_ONE = "engine/doctor/materialize.py"
DEFINES = frozenset({"engine/agent/recipe.py", "engine/agent/sql_step.py"})
"""The modules that define the runners (and call each other)."""
AUTHORING = {
    "engine/agent/advisor.py": "the advisor tries a proposed step on the first rows",
    "engine/agent/edits.py": "a person's edit of a step is tried on the first rows",
    "engine/agent/loop.py": "the Guided-setup agent tries the step it proposes",
    "api/routes/agent.py": "the Guided-setup agent's own recipe route",
}


def _called(path: Path, names: frozenset[str]) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if (func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")) in names:
                return True
    return False


def _outside_doctor() -> set[str]:
    found: set[str] = set()
    for folder in ("engine", "api"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("engine/doctor/"):
                continue  # covered by test_single_materialisation
            if rel in DEFINES:
                continue
            if _called(path, RUNNERS):
                found.add(rel)
    return found


def test_no_replay_outside_materialize_runs_the_steps() -> None:
    unexpected = _outside_doctor() - set(AUTHORING)
    assert (
        not unexpected
    ), f"{sorted(unexpected)} apply recipe steps themselves: call materialize.replay_recipe"


def test_the_list_of_authoring_trials_only_shrinks() -> None:
    stale = set(AUTHORING) - _outside_doctor()
    assert not stale, f"{sorted(stale)} no longer run steps: take them off AUTHORING"


def test_the_stored_recipe_replay_is_a_thin_caller_of_materialize() -> None:
    path = ROOT / "engine/agent/dataset_recipe.py"
    assert not _called(path, RUNNERS), "dataset_recipe applies steps itself"
    assert "replay_recipe" in path.read_text(encoding="utf-8")
    assert _called(ROOT / THE_ONE, frozenset({"run_recipe"})) and _called(
        ROOT / THE_ONE, frozenset({"run_guarded"})
    )


def test_the_prescription_replay_applies_no_steps() -> None:
    path = ROOT / "engine/doctor/prescription_replay.py"
    assert not _called(path, RUNNERS)
    assert _called(
        path, REPLAYERS
    ), "the prescription replay reaches the recipe through run_saved_recipe/run_replay"
