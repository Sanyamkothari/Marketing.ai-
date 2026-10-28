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

from pathlib import Path

from tests.unit.doctor._ast_calls import function_calls, module_calls

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
    """Whether the module calls one of `names`, as the original name when it was imported or assigned under another."""
    return bool(module_calls(path) & names)


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
    assert "replay_recipe" in function_calls(path, "run_replay"), "run_replay does not call replay_recipe"
    assert _called(ROOT / THE_ONE, frozenset({"run_recipe"})) and _called(
        ROOT / THE_ONE, frozenset({"run_guarded"})
    )


def test_the_other_places_that_apply_an_approved_recipe_call_replay_recipe() -> None:
    """A file split, a time setting's steps and the prescription's part recipes reach `replay_recipe`; none runs a
    step itself (`_learn` is the one approval-time exception, listed in `test_single_materialisation`)."""
    for rel, function in (
        ("engine/doctor/split.py", "split_file"),
        ("engine/doctor/time_settings.py", "apply_time_settings"),
    ):
        assert "replay_steps" in function_calls(ROOT / rel, function), rel
    assert "replay_recipe" in function_calls(ROOT / THE_ONE, "replay_steps")


def test_the_prescription_replay_applies_no_steps() -> None:
    path = ROOT / "engine/doctor/prescription_replay.py"
    assert not _called(path, RUNNERS)
    assert _called(
        path, REPLAYERS
    ), "the prescription replay reaches the recipe through run_saved_recipe/run_replay"


def test_a_new_yes_no_spelling_stops_every_stored_recipe_replay_not_only_the_prescription_one() -> None:
    """The recipe-only checks live in `replay_recipe`, so scoring and a firing meet them too (the scoring routes answer
    the `RecipeError` with `recipe_failure_report`)."""
    from datetime import UTC, datetime

    import pandas as pd
    import pytest

    from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
    from engine.agent.dataset_recipe import run_replay
    from engine.agent.recipe import RecipeError

    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.MAP_BOOLEAN,
        column="active",
        params={"true_values": ["yes"], "false_values": ["no"]},
    )
    recipe = DataRecipe(
        recipe_id="r1",
        use_case_id="u",
        source_fingerprint="f",
        steps=(step,),
        recipe_hash=recipe_hash((step,)),
        created_at=datetime.now(UTC),
    )
    frame = pd.DataFrame({"active": ["yes", "no", "ha", "yes"]})
    with pytest.raises(RecipeError) as stopped:
        run_replay(frame, recipe, levels=("safe",), max_failure_pct=5.0, upload_id="up")
    assert stopped.value.code == "RECIPE_NEW_VALUE" and stopped.value.column == "active"
