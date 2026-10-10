"""Plan L's step kinds in the recipe engine (M153/M154, DEC-1530, DEC-1540): checked, run, ordered, replayed."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.agent.advisor import ordered_steps
from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.recipe import RecipeError, check_recipe, run_recipe

LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE)


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": ["C1", "C2", "C3", "C4", "C5"],
            "city": ["Bombay", "Pune", "Mumbai, MH", "TEST", "Agra"],
            "age": [25, 212, -3, 40, None],
            "data_used": ["2 GB", "500 MB", "1 GB", "unknown", "3 GB"],
            "converted": [1, 0, 0, 1, 0],
        }
    )


def step(
    kind: RecipeStepKind, column: str, order: int = 1, new_column: str | None = None, **params: Any
) -> RecipeStep:
    return RecipeStep(order=order, kind=kind, column=column, new_column=new_column, params=params)


def run(
    steps: list[RecipeStep], data: pd.DataFrame | None = None, levels: tuple[AgentLevel, ...] = LEVELS
) -> Any:
    return run_recipe(
        frame() if data is None else data,
        ordered_steps(steps),
        upload_id="u",
        primary_key="customer_id",
        target="converted",
        levels=levels,
        max_failure_pct=50.0,
    )


ALL = [
    step(
        RecipeStepKind.CORRECT_CELLS,
        "city",
        key_column="customer_id",
        cells=[{"key": "C5", "old": "Agra", "new": "Agra (UP)"}],
    ),
    step(RecipeStepKind.MAP_VALUES, "city", mapping={"Bombay": "Mumbai"}),
    step(RecipeStepKind.SET_MISSING_TEXT, "data_used", values=["unknown"]),
    step(RecipeStepKind.CONVERT_UNITS, "data_used", units={"GB": 1024, "MB": 1}),
    step(RecipeStepKind.SPLIT_COLUMN, "city", new_column="city_name", separator=",", part=0),
    step(RecipeStepKind.VALID_RANGE, "age", minimum=0, maximum=120),
    step(RecipeStepKind.FILL_MISSING, "age", value=0),
    step(RecipeStepKind.REMOVE_ROWS, "city", operator="equals", values=["TEST"]),
    step(RecipeStepKind.RENAME_COLUMN, "data_used", new_column="data_mb"),
]


def test_every_new_kind_runs_in_the_fixed_order_and_is_counted() -> None:
    done = run(list(reversed(ALL)))
    out = done.frame
    assert list(out["customer_id"]) == ["C1", "C2", "C3", "C5"]
    assert list(out["city"]) == ["Mumbai", "Pune", "Mumbai, MH", "Agra (UP)"]
    assert list(out["city_name"]) == ["Mumbai", "Pune", "Mumbai", "Agra (UP)"]
    assert list(out["age"]) == [25, 0, 0, 0]
    assert "data_used" not in out.columns and list(out["data_mb"]) == [2048.0, 500.0, 1024.0, 3072.0]
    kinds = [receipt.kind for receipt in done.receipt.steps]
    # Text fixes first (within a phase the order given is kept), renames last.
    assert set(kinds[:3]) == {
        RecipeStepKind.CORRECT_CELLS,
        RecipeStepKind.MAP_VALUES,
        RecipeStepKind.SET_MISSING_TEXT,
    }
    assert kinds[-1] is RecipeStepKind.RENAME_COLUMN
    removed = next(r for r in done.receipt.steps if r.kind is RecipeStepKind.REMOVE_ROWS)
    assert removed.changed == 1 and done.receipt.rows_out == 4


def test_next_months_file_replays_the_same_steps() -> None:
    later = frame().assign(customer_id=["C9", "C2", "C3", "C4", "C5"], age=[30, 150, 20, 18, 61])
    done = run(ALL, later)
    assert list(done.frame["age"]) == [30, 0, 20, 61]
    corrected = next(r for r in done.receipt.steps if r.kind is RecipeStepKind.CORRECT_CELLS)
    assert (corrected.changed, corrected.failed) == (1, 0)


@pytest.mark.parametrize(
    "bad",
    [
        step(RecipeStepKind.MAP_VALUES, "converted", mapping={"1": "0"}),
        step(RecipeStepKind.FILL_MISSING, "converted", value=1),
        step(RecipeStepKind.REMOVE_ROWS, "converted", operator="equals", values=["1"]),
        step(RecipeStepKind.VALID_RANGE, "customer_id", minimum=0),
        step(RecipeStepKind.RENAME_COLUMN, "converted", new_column="outcome"),
    ],
    ids=["map-outcome", "fill-outcome", "filter-on-outcome", "range-on-id", "rename-outcome"],
)
def test_the_id_and_the_outcome_are_never_touched(bad: RecipeStep) -> None:
    with pytest.raises(RecipeError) as caught:
        run([bad])
    assert caught.value.code == "RECIPE_STEP_INVALID"


@pytest.mark.parametrize(
    "bad",
    [
        step(RecipeStepKind.MAP_VALUES, "city", mapping={}),
        step(RecipeStepKind.SET_MISSING_TEXT, "city", values=["x"] * 21),
        step(RecipeStepKind.CONVERT_UNITS, "data_used", units={"GB": -1}),
        step(RecipeStepKind.VALID_RANGE, "age", minimum=10, maximum=5),
        step(RecipeStepKind.FILL_MISSING, "age", value="mean"),
        step(RecipeStepKind.FILL_MISSING, "age"),
        step(RecipeStepKind.REMOVE_ROWS, "city", operator="matches", values=["T.*"]),
        step(RecipeStepKind.REMOVE_ROWS, "city", operator="before", values=["not a date"]),
        step(RecipeStepKind.SPLIT_COLUMN, "city", new_column="x", separator="", part=0),
        step(RecipeStepKind.CORRECT_CELLS, "city", key_column="customer_id", cells=[{"key": "C1"}]),
    ],
)
def test_bad_parameters_are_refused(bad: RecipeStep) -> None:
    with pytest.raises(RecipeError) as caught:
        run([bad])
    assert caught.value.code == "RECIPE_STEP_INVALID"


def test_fill_missing_with_a_word_is_a_constant_not_a_statistic() -> None:
    """'mean' would be a constant word, but refusing it keeps anyone from mistaking it for the mean."""
    done = run([step(RecipeStepKind.FILL_MISSING, "city", value="unknown city")])
    assert done.receipt.steps[0].changed == 0


def test_split_and_remove_rows_need_the_derive_level() -> None:
    for kind_step in (ALL[4], ALL[7]):
        with pytest.raises(RecipeError, match="may not use"):
            run([kind_step], levels=(AgentLevel.CLEAN,))


def test_a_rename_onto_an_existing_column_is_refused() -> None:
    with pytest.raises(RecipeError, match="already a column"):
        check_recipe(
            [step(RecipeStepKind.RENAME_COLUMN, "age", new_column="city")],
            columns=list(frame().columns),
            primary_key="customer_id",
            target="converted",
            levels=LEVELS,
        )


def test_a_correction_keyed_on_a_missing_column_is_refused() -> None:
    with pytest.raises(RecipeError) as caught:
        run(
            [
                step(
                    RecipeStepKind.CORRECT_CELLS,
                    "city",
                    key_column="account",
                    cells=[{"key": "1", "old": "a", "new": "b"}],
                )
            ]
        )
    assert caught.value.code == "RECIPE_COLUMN_MISSING"
