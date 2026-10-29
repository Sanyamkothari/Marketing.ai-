"""Architecture: the plan is run in one place (root-cause plan, Step 1).

`engine/doctor/materialize.py` is the only module of `engine/doctor` and `api/routes` that runs the plan's steps for
the preview, the plan check, the AI's query trial and the final check. A new caller of `run_recipe`, `run_guarded` or
`run_sql` there is a second world that drifts from the first (different rows, tables, order, as-of date), and fails
here instead of at the person's run.

The modules listed below still run a recipe for a reason of their own (each says which). Each entry names its reason; a "TODO Step 4" entry is the plan's remaining second world, taken off when that step
lands. The list may only shrink:
a module that stops calling is a stale entry and fails too, so it is taken off. Calls are read with import aliases
resolved, in every module under `engine/doctor` (subpackages too) and `api/routes`.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.unit.doctor._ast_calls import module_calls

ROOT = Path(__file__).resolve().parents[3]
RUNNERS = frozenset({"run_recipe", "run_guarded", "run_sql"})
THE_ONE = "engine/doctor/materialize.py"
STILL_OWN_RUN = {
    "engine/doctor/guards.py": "defines run_guarded over run_recipe",
    "engine/doctor/prescription.py": "`_learn` runs the recipe being approved, at approval, for the guards' receipts and"
    " the contract it learns (not a replay: it keeps the findings, twice, and never stops on them); see 'Replay paths'",
    "engine/doctor/build_driver.py": "tries a build, not the plan's steps",
    "engine/doctor/change_share.py": "TODO Step 4: measures a step on rows that are not the materialised tables",
    "engine/doctor/evals.py": "the eval harness replays a recipe (a test tool, not a customer path)",
    "engine/doctor/agent_unpivot.py": "TODO Step 4: the AI unpivot question tries its one step on the first rows of"
    " the agent's file, before any case plan exists",
}


def callers() -> set[str]:
    found: set[str] = set()
    for folder in ("engine/doctor", "api/routes"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if RUNNERS & module_calls(path):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def test_only_the_one_materialisation_runs_the_plan() -> None:
    found = callers()
    assert THE_ONE in found
    unexpected = found - {THE_ONE} - set(STILL_OWN_RUN)
    assert not unexpected, f"{sorted(unexpected)} run the plan themselves: call materialize_plan instead"


def test_the_list_of_other_runners_only_shrinks() -> None:
    stale = set(STILL_OWN_RUN) - callers()
    assert not stale, f"{sorted(stale)} no longer run a recipe: take them off STILL_OWN_RUN"


def test_the_trial_the_preview_and_the_plan_check_use_it() -> None:
    for name in ("tools_sql", "preview", "plan_check"):
        source = (ROOT / "engine/doctor" / f"{name}.py").read_text(encoding="utf-8")
        assert "materialize_plan" in source, name
        assert not RUNNERS & {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}, name
