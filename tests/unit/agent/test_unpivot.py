"""Plan L M156 (DEC-1560 … DEC-1569): the `unpivot` step - checked, stateless, outcome carried, ordered first."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from engine.agent.advisor import ordered_steps
from engine.agent.config import AgentLevel
from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.recipe import STEP_PHASE, RecipeError, check_recipe, run_recipe
from engine.agent.unpivot import NAME_PATTERNS, find_wide_columns, parse_period

LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)
KEY, TARGET = "customer_id", "churned"


def wide() -> pd.DataFrame:
    return pd.DataFrame(
        {
            KEY: ["C1", "C2", "C3"],
            "region": ["North", "South", "North"],
            "2024_01_spend": [10.0, 20.0, None],
            "2024_02_spend": [11.0, None, 31.0],
            "2024_03_spend": [12.0, 22.0, 32.0],
            "2024_01_calls": [1, 2, 3],
            "2024_02_calls": [4, 5, 6],
            TARGET: [0, 1, 0],
        }
    )


VALUES = [
    {"column": "2024_01_spend", "measure": "spend", "period": "2024_01"},
    {"column": "2024_02_spend", "measure": "spend", "period": "2024_02"},
    {"column": "2024_03_spend", "measure": "spend", "period": "2024_03"},
    {"column": "2024_01_calls", "measure": "calls", "period": "2024_01"},
    {"column": "2024_02_calls", "measure": "calls", "period": "2024_02"},
]


def params(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id_columns": [KEY, "region"],
        "value_columns": [dict(v) for v in VALUES],
        "name_pattern": "{period}_{measure}",
        "period_column": "month",
        "outcome": TARGET,
    }
    base.update(changes)
    return base


def unpivot_step(order: int = 1, column: str = KEY, **changes: Any) -> RecipeStep:
    return RecipeStep(order=order, kind=RecipeStepKind.UNPIVOT, column=column, params=params(**changes))


def run(steps: list[RecipeStep], data: pd.DataFrame | None = None, **kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "upload_id": "u",
        "primary_key": KEY,
        "target": TARGET,
        "levels": LEVELS,
        "max_failure_pct": 5.0,
    }
    options.update(kwargs)
    return run_recipe(wide() if data is None else data, steps, **options)


def test_columns_become_rows_one_per_row_and_period_with_the_outcome_carried() -> None:
    done = run([unpivot_step()])
    out = done.frame
    assert list(out.columns) == [KEY, "region", TARGET, "month", "spend", "calls"]
    assert list(out[KEY]) == ["C1"] * 3 + ["C2"] * 3 + ["C3"] * 3
    assert list(out["month"].dt.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-02-01", "2024-03-01"] * 3
    assert out["spend"].tolist()[:3] == [10.0, 11.0, 12.0]
    assert pd.isna(out["spend"].iloc[4])  # C2 had no February spend: an empty cell, not a guess
    assert pd.isna(out["calls"].iloc[2])  # no 2024_03_calls column: empty
    # The outcome is copied onto every row of its customer, never turned into rows.
    assert out.groupby(KEY)[TARGET].nunique().eq(1).all()
    assert list(out.drop_duplicates(KEY)[TARGET]) == [0, 1, 0]
    (receipt,) = done.receipt.steps
    assert (receipt.kind, receipt.rows, receipt.changed, receipt.failed) == (RecipeStepKind.UNPIVOT, 3, 9, 0)
    assert (done.receipt.rows_in, done.receipt.rows_out) == (3, 9)


def test_it_is_stateless_each_row_on_its_own() -> None:
    """A row's output depends only on that row and the frozen parameters (DEC-1004)."""
    data = wide()
    whole = run([unpivot_step()], data).frame
    parts = pd.concat(
        [run([unpivot_step()], data.iloc[[i]].reset_index(drop=True)).frame for i in range(len(data))],
        ignore_index=True,
    )
    assert_frame_equal(whole, parts, check_dtype=False)
    # Reordering the input rows reorders the output blocks and changes nothing else.
    shuffled = run([unpivot_step()], data.iloc[[2, 0, 1]].reset_index(drop=True)).frame
    assert_frame_equal(
        shuffled.sort_values([KEY, "month"]).reset_index(drop=True),
        whole.sort_values([KEY, "month"]).reset_index(drop=True),
        check_dtype=False,
    )


def test_a_scoring_file_without_the_outcome_runs() -> None:
    out = run([unpivot_step()], wide().drop(columns=[TARGET])).frame
    assert TARGET not in out.columns and len(out) == 9


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"value_columns": [*VALUES, {"column": KEY, "measure": "spend", "period": "2024_04"}]}, "joined"),
        (
            {"value_columns": [*VALUES[:4], {"column": TARGET, "measure": "calls", "period": "2024_02"}]},
            "joined",
        ),
        ({"id_columns": [KEY, "2024_01_spend"]}, "never turned into rows"),
        ({"id_columns": [KEY, TARGET]}, "carried as the outcome"),
        ({"id_columns": ["region"]}, "must be one of the columns kept"),
        ({"name_pattern": "{measure}{period}"}, "name pattern"),
        ({"name_pattern": "{measure}_{period}"}, "joined by the name pattern"),
        # A column filed under another measure than its name says is refused.
        (
            {
                "value_columns": [
                    {"column": "2024_01_spend", "measure": "calls", "period": "2024_01"},
                    *VALUES[1:],
                ]
            },
            "joined by the name pattern",
        ),
        (
            {"value_columns": [*VALUES, {"column": "202401_spend", "measure": "spend", "period": "202401"}]},
            "same period",
        ),
        ({"value_columns": [{"column": "2024_01_spend", "measure": "spend", "period": "2024_01"}]}, "2 to"),
        ({"value_columns": VALUES[:1] + VALUES[3:4]}, "two periods"),
        ({"value_columns": [{**VALUES[0], "x": 1}, VALUES[1]]}, "{column"),
        ({"period_column": "1st"}, "period column"),
        ({"period_column": "spend"}, "same name"),
        ({"outcome": None}, "outcome column"),
        ({"outcome": "region"}, "outcome"),
        ({"surprise": True}, "unknown parameter"),
    ],
)
def test_bad_parameters_are_refused(changes: dict[str, Any], words: str) -> None:
    with pytest.raises(RecipeError) as caught:
        run([unpivot_step(**changes)])
    assert caught.value.code == "RECIPE_STEP_INVALID", caught.value.message
    assert words in caught.value.message


def test_the_id_and_the_outcome_are_never_value_columns_by_name_pattern_either() -> None:
    # A file whose ID column is itself named like a period column cannot slip it in.
    data = wide().rename(columns={KEY: "2024_04_spend"})
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.UNPIVOT,
        column="2024_04_spend",
        params=params(
            id_columns=["2024_04_spend", "region"],
            value_columns=[*VALUES, {"column": "2024_04_spend", "measure": "spend", "period": "2024_04"}],
        ),
    )
    with pytest.raises(RecipeError) as caught:
        run([step], data, primary_key="2024_04_spend")
    assert "never turned into rows" in caught.value.message


def test_it_is_keyed_on_the_id_and_needs_reshape() -> None:
    with pytest.raises(RecipeError) as other:
        run([unpivot_step(column="region", id_columns=["region", KEY])])
    assert "per ID column" in other.value.message
    with pytest.raises(RecipeError) as level:
        run([unpivot_step()], levels=(AgentLevel.CLEAN, AgentLevel.DERIVE))
    assert "may not use unpivot" in level.value.message
    with pytest.raises(RecipeError) as twice:
        run([unpivot_step(1), unpivot_step(2)])
    assert "only once" in twice.value.message


def test_a_missing_column_stops_and_a_new_period_is_counted() -> None:
    with pytest.raises(RecipeError) as missing:
        run([unpivot_step()], wide().drop(columns=["2024_02_calls"]))
    assert missing.value.code == "RECIPE_COLUMN_MISSING" and missing.value.column == "2024_02_calls"
    # Next month's sheet gained a column: left out, counted on the receipt as cells that did not fit.
    grown = wide()
    grown["2024_04_spend"] = [1.0, None, None]
    grown["notes"] = ["a", "b", "c"]
    done = run([unpivot_step()], grown, max_failure_pct=20.0)
    (receipt,) = done.receipt.steps
    assert receipt.failed == 1 and receipt.examples_failed == ("2024_04_spend",)
    assert "2024_04_spend" not in done.frame.columns and "notes" not in done.frame.columns
    # More of them than the failure limit stops the run instead of training on part of the months.
    full = wide()
    full["2024_04_spend"] = [1.0, 2.0, 3.0]
    full["2024_04_calls"] = [1, 2, 3]
    with pytest.raises(RecipeError) as stopped:
        run([unpivot_step()], full)
    assert stopped.value.code == "RECIPE_VALUES_UNCONVERTED"
    assert "2024_04_spend" in stopped.value.message


def test_it_runs_first_and_later_steps_read_its_rows() -> None:
    assert STEP_PHASE[RecipeStepKind.UNPIVOT] < min(
        v for k, v in STEP_PHASE.items() if k is not RecipeStepKind.UNPIVOT
    )
    assert STEP_PHASE[RecipeStepKind.UNPIVOT] < STEP_PHASE[RecipeStepKind.COMBINE_ROWS]
    fill = RecipeStep(order=1, kind=RecipeStepKind.FILL_MISSING, column="spend", params={"value": 0})
    steps = ordered_steps([fill, unpivot_step()])
    assert [s.kind for s in steps] == [RecipeStepKind.UNPIVOT, RecipeStepKind.FILL_MISSING]
    out = run(list(steps)).frame
    assert out["spend"].isna().sum() == 0
    with pytest.raises(RecipeError) as late:
        check_recipe(
            [fill.model_copy(update={"order": 1, "column": "region"}), unpivot_step(2)],
            columns=list(wide().columns),
            primary_key=KEY,
            target=TARGET,
            levels=LEVELS,
        )
    assert "fixed order" in late.value.message


def test_existing_kinds_keep_their_relative_order_so_saved_recipes_hash_the_same() -> None:
    """The phases before M156, as numbers; only their order matters and it is unchanged."""
    before = {
        "correct_cells": 0,
        "map_values": 0,
        "set_missing_text": 0,
        "parse_number": 1,
        "parse_date": 1,
        "map_boolean": 1,
        "normalise_text": 1,
        "convert_units": 1,
        "split_column": 1,
        "set_missing": 2,
        "valid_range": 2,
        "fill_missing": 3,
        "remove_rows": 3,
        "combine_rows": 4,
        "derive": 5,
        "drop_column": 6,
        "rename_column": 6,
    }
    for a, pa in before.items():
        for b, pb in before.items():
            now_a, now_b = STEP_PHASE[RecipeStepKind(a)], STEP_PHASE[RecipeStepKind(b)]
            assert (pa < pb) == (now_a < now_b) and (pa == pb) == (now_a == now_b), (a, b)


def test_the_step_is_in_the_hash_and_a_saved_recipe_reads_back() -> None:
    step = unpivot_step()
    recipe = DataRecipe(
        recipe_id="r1",
        use_case_id="retail-win-back",
        source_fingerprint="f",
        steps=(step,),
        recipe_hash=recipe_hash((step,)),
        primary_key=KEY,
        target=TARGET,
        created_at=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
    )
    again = DataRecipe.model_validate_json(recipe.model_dump_json())
    assert again.recipe_hash == recipe.recipe_hash
    assert recipe_hash((unpivot_step(period_column="period"),)) != recipe.recipe_hash
    with pytest.raises(ValueError, match="names its new columns"):
        RecipeStep(order=1, kind=RecipeStepKind.UNPIVOT, column=KEY, new_column="x", params=params())


def test_periods_without_a_year_are_kept_as_written() -> None:
    data = pd.DataFrame({KEY: ["C1"], "Jan_spend": [1.0], "Feb_spend": [2.0], TARGET: [1]})
    step = unpivot_step(
        id_columns=[KEY],
        value_columns=[
            {"column": "Feb_spend", "measure": "spend", "period": "Feb"},
            {"column": "Jan_spend", "measure": "spend", "period": "Jan"},
        ],
    )
    out = run([step], data).frame
    assert list(out["month"]) == ["Jan", "Feb"]  # in period order, never given a year
    assert list(out["spend"]) == [1.0, 2.0]


@pytest.mark.parametrize(
    ("token", "kind", "year", "index"),
    [
        ("2024_01", "month", 2024, 1),
        ("2024-11", "month", 2024, 11),
        ("202403", "month", 2024, 3),
        ("Jan_2024", "month", 2024, 1),
        ("2024 Sept", "month", 2024, 9),
        ("2024Q3", "quarter", 2024, 3),
        ("Q2_2023", "quarter", 2023, 2),
        ("2022", "year", 2022, 1),
        ("january", "month", None, 1),
        ("Dec", "month", None, 12),
        ("M12", "month", None, 12),
        ("q4", "quarter", None, 4),
    ],
)
def test_the_period_grammar(token: str, kind: str, year: int | None, index: int) -> None:
    period = parse_period(token)
    assert period is not None
    assert (period.kind, period.year, period.index) == (kind, year, index)


@pytest.mark.parametrize("token", ["M13", "M0", "Q5", "2024_13", "1850", "total", "janx", "spend", "01"])
def test_what_is_not_a_period(token: str) -> None:
    assert parse_period(token) is None


def test_quarters_and_years_start_on_their_first_day() -> None:
    assert parse_period("2024_Q2").value == pd.Timestamp("2024-04-01")  # type: ignore[union-attr]
    assert parse_period("2023").value == pd.Timestamp("2023-01-01")  # type: ignore[union-attr]


def test_the_name_patterns_are_a_fixed_list() -> None:
    assert len(NAME_PATTERNS) == 8
    assert "{measure}_{period}" in NAME_PATTERNS and "{period} {measure}" in NAME_PATTERNS


# ---------------------------------------------------------------------------
# Detection, on column names only
# ---------------------------------------------------------------------------
def test_month_first_and_month_last_columns_are_found() -> None:
    first = find_wide_columns(["id", "jan_spend", "feb_spend", "mar_spend"], skip=["id"])
    assert first is not None and first.name_pattern == "{period}_{measure}" and not first.dated
    assert first.columns == ("jan_spend", "feb_spend", "mar_spend") and first.measures == ("spend",)
    last = find_wide_columns(["id", "spend_jan", "spend_feb"], skip=["id"])
    assert last is not None and last.name_pattern == "{measure}_{period}"
    dated = find_wide_columns(["spend_2024_01", "spend_2024_02", "calls_2024_01", "calls_2024_02"])
    assert dated is not None and dated.dated and dated.kind == "month"
    assert dated.measures == ("spend", "calls")


def test_mixed_name_orders_are_not_one_group() -> None:
    """`spend_jan` beside `feb_spend`: each measure has one period in each pattern, so nothing is wide."""
    assert find_wide_columns(["spend_jan", "feb_spend"]) is None


def test_a_single_period_column_is_not_wide() -> None:
    assert find_wide_columns(["customer_id", "jan_spend", "age"]) is None
    group = find_wide_columns(["2024_01_spend", "2024_02_spend", "2024_01_calls"])
    assert group is not None and group.measures == ("spend",)  # calls has one month: left as it is


def test_unrelated_columns_sharing_a_word_are_not_wide() -> None:
    assert find_wide_columns(["total_spend", "avg_spend", "max_spend", "spend"]) is None
    assert find_wide_columns(["call_count", "call_minutes", "calls_dropped"]) is None
    assert find_wide_columns(["first_order_date", "last_order_date"]) is None


def test_quarters_years_and_month_numbers_are_found() -> None:
    quarters = find_wide_columns(["sales_Q1", "sales_Q2", "sales_Q3"])
    assert quarters is not None and quarters.kind == "quarter" and not quarters.dated
    years = find_wide_columns(["revenue_2022", "revenue_2023"])
    assert years is not None and years.kind == "year" and years.dated
    numbered = find_wide_columns(["M1_visits", "M2_visits"])
    assert numbered is not None and numbered.kind == "month" and not numbered.dated
    quarter_years = find_wide_columns(["spend_2024_Q1", "spend_2024_Q2"])
    assert quarter_years is not None and quarter_years.kind == "quarter" and quarter_years.dated


def test_a_measure_already_a_column_is_left_out_and_the_id_and_outcome_are_skipped() -> None:
    assert find_wide_columns(["spend", "spend_2024_01", "spend_2024_02"]) is None
    assert find_wide_columns(["2024_01_spend", "2024_02_spend"], skip=["2024_01_spend"]) is None


def test_the_largest_group_wins_and_its_params_pass_the_check() -> None:
    columns = [*wide().columns, "visits_2024_01", "visits_2024_02"]
    group = find_wide_columns(columns, skip=[KEY, TARGET])
    assert group is not None and group.measures == ("spend", "calls")
    kept = [c for c in columns if c not in group.columns and c != TARGET]
    made = group.params(key=KEY, id_columns=kept, outcome=TARGET, taken=columns)
    assert made["period_column"] == "month" and made["id_columns"][0] == KEY
    check_recipe(
        [RecipeStep(order=1, kind=RecipeStepKind.UNPIVOT, column=KEY, params=made)],
        columns=columns,
        primary_key=KEY,
        target=TARGET,
        levels=LEVELS,
    )
    taken = group.params(key=KEY, id_columns=kept, outcome=TARGET, taken=[*columns, "Month"])
    assert taken["period_column"] == "period"
