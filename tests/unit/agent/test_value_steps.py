"""Plan L's value steps (M153/M154, DEC-1530 … DEC-1540): each one stateless, counted, and the same on any file."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from engine.agent.value_steps import (
    convert_units,
    correct_cells,
    fill_missing,
    map_values,
    remove_rows,
    set_missing_text,
    split_column,
    valid_range,
)


def test_map_values_merges_spellings_and_keeps_the_rest() -> None:
    series = pd.Series(["Bombay", "Mumbai", " PP ", None, "Delhi"])
    out = map_values(series, mapping={"Bombay": "Mumbai", "PP": "Prepaid"})
    assert out.values.tolist()[:3] == ["Mumbai", "Mumbai", "Prepaid"]
    assert out.values.iloc[3] is None and out.values.iloc[4] == "Delhi"
    assert (out.changed, out.failed) == (2, 0)


def test_set_missing_text_empties_listed_words_only() -> None:
    out = set_missing_text(pd.Series(["Unknown", "gold", " n/a ", None]), values=["unknown", "N/A"])
    assert out.values.isna().tolist() == [True, False, True, True]
    assert out.changed == 2


def test_valid_range_empties_impossible_numbers_and_counts_text() -> None:
    out = valid_range(pd.Series([25, 212, -3, None, "old"]), minimum=0, maximum=120)
    assert out.values.isna().tolist() == [False, True, True, True, False]
    assert (out.changed, out.failed) == (2, 1)
    assert out.failed_examples == ("old",)


def test_convert_units_to_one_unit() -> None:
    out = convert_units(
        pd.Series(["2 GB", "500MB", "1,5 gb", "3 TB", "7", None]), units={"GB": 1024, "MB": 1}
    )
    assert out.values.tolist()[:3] == [2048.0, 500.0, 1536.0]
    assert math.isnan(out.values.iloc[3]) and math.isnan(out.values.iloc[4])
    assert (out.changed, out.failed) == (3, 2)
    bare = convert_units(pd.Series(["7", 9]), units={"GB": 1024}, bare="as_target")
    assert bare.values.tolist() == [7.0, 9.0] and bare.failed == 0


def test_split_column_takes_one_part() -> None:
    series = pd.Series(["Mumbai, MH", "Pune,MH", "Delhi", None])
    first = split_column(series, separator=",", part=0)
    assert first.values.tolist()[:3] == ["Mumbai", "Pune", "Delhi"]
    last = split_column(series, separator=",", part=1)
    assert last.values.tolist()[:2] == ["MH", "MH"] and last.values.iloc[2] is None
    assert last.failed == 1


def test_fill_missing_uses_a_constant_never_a_statistic() -> None:
    out = fill_missing(pd.Series([1.0, np.nan, 3.0]), value=0)
    assert out.values.tolist() == [1.0, 0.0, 3.0] and out.changed == 1
    text = fill_missing(pd.Series(["a", None]), value="none given")
    assert text.values.tolist() == ["a", "none given"]


def test_correct_cells_applies_only_where_the_old_value_is_still_there() -> None:
    keys = pd.Series(["C1", "C2", 3, "C4"])
    values = pd.Series(["Bombay", "Pune", "Agra", "Goa"])
    cells = [
        {"key": "C1", "old": "Bombay", "new": "Mumbai"},
        {"key": "3", "old": "Agra", "new": "Agra (UP)"},
        {"key": "C2", "old": "Puna", "new": "Pune"},  # next month the value is already right
        {"key": "C9", "old": "x", "new": "y"},  # no such customer this month
    ]
    out = correct_cells(values, keys, cells=cells)
    assert out.values.tolist() == ["Mumbai", "Pune", "Agra (UP)", "Goa"]
    assert (out.changed, out.failed) == (2, 2)


@pytest.mark.parametrize(
    ("operator", "values", "kept"),
    [
        ("equals", ["TEST"], [False, True, True, True]),
        ("in", ["TEST", "demo"], [False, False, True, True]),
        ("is_empty", [], [True, True, True, False]),
    ],
)
def test_remove_rows_by_a_rule(operator: str, values: list[str], kept: list[bool]) -> None:
    series = pd.Series(["TEST", "demo", "live", None])
    assert remove_rows(series, operator=operator, values=values).tolist() == kept


def test_remove_rows_before_a_date_keeps_what_is_not_a_date() -> None:
    series = pd.Series(["2024-01-05", "2025-06-01", "soon", None])
    assert remove_rows(series, operator="before", values=["2025-01-01"]).tolist() == [False, True, True, True]
    assert remove_rows(series, operator="after", values=["2025-01-01"]).tolist() == [True, False, True, True]


def test_every_step_reads_one_row_at_a_time() -> None:
    """Stateless (DEC-1004): a row's output is the same in a file of one row and in the whole file."""
    series = pd.Series(["2 GB", "Bombay", "212", None, "Mumbai, MH"])
    runs = [
        lambda s: map_values(s, mapping={"Bombay": "Mumbai"}).values,
        lambda s: set_missing_text(s, values=["bombay"]).values,
        lambda s: valid_range(s, minimum=0, maximum=120).values,
        lambda s: convert_units(s, units={"GB": 1024}).values,
        lambda s: split_column(s, separator=",", part=0).values,
        lambda s: fill_missing(s, value="x").values,
    ]
    for run in runs:
        whole = run(series)
        for index in range(len(series)):
            alone = run(series.iloc[[index]])
            a, b = whole.iloc[index], alone.iloc[0]
            assert (pd.isna(a) and pd.isna(b)) or a == b
