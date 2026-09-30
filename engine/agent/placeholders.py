"""Placeholder ("sentinel") values in number columns: detection, the `set_missing` step, and its impact.

A number column sometimes holds a code instead of a value: `99` months of tenure where the real
values stop at 72, `-1` days since the last order in a column that is otherwise never negative.
The code usually means "unknown", and a model reads it as a real, extreme number. Three pieces
share this module so they cannot disagree about which cells are meant:

* **Detection** (`find_placeholder_values`) - conservative on purpose (DEC-1221). Only the codes in
  `PLACEHOLDER_CODES` are considered, only in columns pandas already reads as numbers, and a code
  is reported only when it is outside every other value of the column *by a clear gap*, and holds
  a meaningful share of the rows. `99` in an age column whose values commonly reach 95 is a real age
  and is not reported.
* **The step** (`set_missing`) - replaces the listed values with an empty cell. Row-wise and
  stateless (DEC-1004): a cell's output depends only on the cell and the listed values, so it replays
  identically on a scoring file. A number is matched by its value (`99`, `99.0` and the text `"99"`
  are the same cell); anything else is left alone.
* **Impact** (`placeholder_impact`) - what the choice does to the data, measured, never modelled:
  rows affected, the column's mean and median before and after and - when the outcome is known and
  two-valued - the "yes" rate on the affected rows against the rest, and the single-column AUC the
  leakage check uses (`validate.single_feature_auc`) before and after. No model is trained per
  option: picking a fix by a model score would be choosing on the test data (DEC-1223).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd

from engine.agent.formats import ParseOutcome
from engine.agent.untrusted import quoted
from engine.stages import validate

__all__ = [
    "MAX_PLACEHOLDER_VALUES",
    "PLACEHOLDER_CODES",
    "PLACEHOLDER_KIND",
    "PlaceholderIssue",
    "find_placeholder_values",
    "matches_values",
    "number_text",
    "placeholder_impact",
    "set_missing",
]

PLACEHOLDER_KIND: Final[str] = "placeholder_value"
"""The issue kind `find_format_issues` reports for a placeholder in a number column."""

PLACEHOLDER_CODES: Final[tuple[int, ...]] = (99, 999, 9999, -1, -99, -999)
"""The only values ever reported as a placeholder (their float forms included). A fixed list: the
detector never invents a code from the data."""

MAX_PLACEHOLDER_VALUES: Final[int] = 10
"""A `set_missing` step lists at most this many values."""

MIN_NON_EMPTY: Final[int] = 30
"""Fewer numbers than this say too little about where a column's real values end."""
MIN_ROWS: Final[int] = 5
MIN_SHARE: Final[float] = 0.005
"""A code is reported only on at least `MIN_ROWS` rows and at least this share of the numbers."""
MAX_SHARE: Final[float] = 0.50
"""A value on more than half the rows is what the column mostly holds, not a code for a gap in it."""
MIN_OTHER_VALUES: Final[int] = 20
MIN_OTHER_DISTINCT: Final[int] = 5
"""The other values must be enough, and varied enough, to show where the real values end."""
MIN_GAP: Final[float] = 0.25
"""How far outside the other values a code must sit: at least this share of the distance from their
median to their edge on the code's side. `99` above values reaching 72 with a median of 36 is a gap of
27 against 36 (0.75); `99` above ages reaching 95 with a median of 56 is 4 against 39 (0.10)."""

_PLAIN_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def number_text(value: float) -> str:
    """A value as a person reads it: `99`, `-1`, `2.5` - never `99.0`."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _json_number(value: float) -> int | float:
    """A value as a step parameter: an int when whole, so `99` and `99.0` hash the same."""
    return int(value) if float(value).is_integer() else float(value)


def _cell_number(value: Any) -> float:
    """One cell as a number, or NaN: a real number by its value, a plain numeric text by its digits."""
    if value is None or isinstance(value, (bool, np.bool_)):
        return math.nan
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    text = str(value).strip()
    return float(text) if _PLAIN_NUMBER_RE.match(text) else math.nan


def _as_numbers(series: pd.Series[Any]) -> np.ndarray[Any, Any]:
    """`series` as float64 with NaN wherever a cell is empty or not a number."""
    if pd.api.types.is_bool_dtype(series):
        return np.full(len(series), np.nan, dtype="float64")
    if pd.api.types.is_numeric_dtype(series):
        return np.asarray(pd.to_numeric(series, errors="coerce").to_numpy(dtype="float64", na_value=np.nan))
    raw = series.to_numpy(dtype=object)
    table: dict[Any, float] = {}
    out = np.empty(len(raw), dtype="float64")
    for index, value in enumerate(raw):
        try:
            key: Any = value if isinstance(value, str) else (type(value), value)
            if key not in table:
                table[key] = _cell_number(value)
            out[index] = table[key]
        except TypeError:  # an unhashable cell: never a number
            out[index] = math.nan
    return out


def _holding(numbers: np.ndarray[Any, Any], values: Sequence[float]) -> np.ndarray[Any, Any]:
    listed = np.asarray([float(v) for v in values], dtype="float64")
    found: np.ndarray[Any, Any] = np.isin(numbers, listed) & ~np.isnan(numbers)
    return found


def matches_values(series: pd.Series[Any], values: Sequence[float]) -> np.ndarray[Any, Any]:
    """Which cells of `series` hold one of `values`, compared as numbers."""
    return _holding(_as_numbers(series), values)


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------
def set_missing(series: pd.Series[Any], *, values: Sequence[float]) -> ParseOutcome:
    """`series` with every cell that holds one of `values` made empty; every other cell as it was.

    An integer column becomes a float column when a cell is emptied (pandas has no empty int64 cell).
    Nothing can fail to convert, so `failed` is always 0.
    """
    mask = matches_values(series, values)
    out = series.mask(pd.Series(mask, index=series.index)) if mask.any() else series.copy()
    return ParseOutcome(values=out, changed=int(mask.sum()), failed=0, failed_examples=())


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlaceholderIssue:
    """A number column holding placeholder codes, measured on the whole column."""

    column: str
    non_empty: int
    values: tuple[float, ...]
    rows: tuple[int, ...]
    """Rows holding each value, in the order of `values`."""
    other_minimum: float
    other_maximum: float

    @property
    def affected(self) -> int:
        return sum(self.rows)

    def as_json(self) -> dict[str, Any]:
        """The issue in `find_format_issues`' shape, plus where the other values end."""
        shown = [number_text(v) for v in self.values]
        return {
            "column": self.column,
            "kind": PLACEHOLDER_KIND,
            "non_empty": self.non_empty,
            "convertible": self.affected,
            "failed": 0,
            "convert_share": round(self.affected / self.non_empty, 4) if self.non_empty else 0.0,
            "examples": [f"{quoted(text)} ({count})" for text, count in zip(shown, self.rows, strict=True)],
            "failed_examples": [],
            "params": {"values": [_json_number(v) for v in self.values]},
            "notes": [
                f"every other value is between {number_text(self.other_minimum)} and "
                f"{number_text(self.other_maximum)}"
            ],
            "example_cells": shown,
            "placeholders": [
                {"value": _json_number(v), "rows": count}
                for v, count in zip(self.values, self.rows, strict=True)
            ],
            "other_minimum": _json_number(self.other_minimum),
            "other_maximum": _json_number(self.other_maximum),
        }


def _outside(code: float, others: np.ndarray[Any, Any]) -> bool:
    """Whether `code` sits clearly outside `others` (see `MIN_GAP`); a negative code below values that
    are never negative always does (`-1` days since the last order)."""
    low, high, middle = float(others.min()), float(others.max()), float(np.median(others))
    if code > high:
        spread = high - middle or high - low
        return spread > 0 and code - high >= MIN_GAP * spread
    if code < low:
        if code < 0 <= low:
            return True
        spread = middle - low or high - low
        return spread > 0 and low - code >= MIN_GAP * spread
    return False


def _column_issue(name: str, series: pd.Series[Any]) -> PlaceholderIssue | None:
    if pd.api.types.is_bool_dtype(series) or not pd.api.types.is_numeric_dtype(series):
        return None
    numbers = _as_numbers(series)
    numbers = numbers[np.isfinite(numbers)]
    if numbers.size < MIN_NON_EMPTY:
        return None
    counts = {code: int((numbers == code).sum()) for code in PLACEHOLDER_CODES}
    # Only codes frequent enough to be codes leave the "other values"; a rare 999 stays in them, so a
    # 99 below it is not outside the column.
    frequent = [
        code
        for code, count in counts.items()
        if count >= MIN_ROWS and MIN_SHARE <= count / numbers.size <= MAX_SHARE
    ]
    if not frequent:
        return None
    others = numbers[~np.isin(numbers, np.asarray(frequent, dtype="float64"))]
    if others.size < MIN_OTHER_VALUES or np.unique(others).size < MIN_OTHER_DISTINCT:
        return None
    found = [code for code in frequent if _outside(float(code), others)]
    if not found:
        return None
    return PlaceholderIssue(
        column=name,
        non_empty=int(numbers.size),
        values=tuple(float(code) for code in found),
        rows=tuple(counts[code] for code in found),
        other_minimum=float(others.min()),
        other_maximum=float(others.max()),
    )


def find_placeholder_values(
    frame: pd.DataFrame, *, columns: Sequence[str] | None = None
) -> tuple[PlaceholderIssue, ...]:
    """Every number column of `frame` (or of `columns`) that holds placeholder codes, one issue each."""
    names = [str(c) for c in frame.columns] if columns is None else [c for c in columns if c in frame.columns]
    issues: list[PlaceholderIssue] = []
    for name in names:
        issue = _column_issue(name, frame[name])
        if issue is not None:
            issues.append(issue)
    return tuple(issues)


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------
def _stat(values: np.ndarray[Any, Any], how: str) -> float | None:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    return round(float(np.mean(finite) if how == "mean" else np.median(finite)), 4)


def _rate(flags: np.ndarray[Any, Any]) -> float | None:
    known = flags[~np.isnan(flags)]
    return round(float(known.mean()), 4) if known.size else None


def _auc(x: np.ndarray[Any, Any], y: np.ndarray[Any, Any]) -> float | None:
    auc = validate.single_feature_auc(pd.Series(x, dtype="float64"), pd.Series(y, dtype="float64"))
    return None if auc is None else round(auc, 4)


def placeholder_impact(
    series: pd.Series[Any], values: Sequence[float], *, positive: pd.Series[Any] | None = None
) -> dict[str, Any]:
    """What emptying `values` in `series` does, measured on these rows (DEC-1222).

    `positive` is the outcome as 1 / 0 / empty by the engine's own label rule, or None when the outcome
    is unknown or not two-valued: then every outcome figure is None, never a guess. The AUC "after" is
    over the rows that still hold a number, as the leakage check reads a column with empty cells.
    """
    before = _as_numbers(series)
    mask = _holding(before, values)
    after = np.where(mask, np.nan, before)
    affected = int(mask.sum())
    result: dict[str, Any] = {
        "rows": len(series),
        "affected_rows": affected,
        "affected_share": round(affected / len(series), 4) if len(series) else 0.0,
        "mean_before": _stat(before, "mean"),
        "mean_after": _stat(after, "mean"),
        "median_before": _stat(before, "median"),
        "median_after": _stat(after, "median"),
        "affected_positive_rate": None,
        "other_positive_rate": None,
        "auc_before": None,
        "auc_after": None,
    }
    if positive is None:
        return result
    y = np.asarray(pd.to_numeric(positive, errors="coerce").to_numpy(dtype="float64", na_value=np.nan))
    result.update(
        {
            "affected_positive_rate": _rate(y[mask]),
            "other_positive_rate": _rate(y[~mask]),
            "auc_before": _auc(before, y),
            "auc_after": _auc(after, y),
        }
    )
    return result
