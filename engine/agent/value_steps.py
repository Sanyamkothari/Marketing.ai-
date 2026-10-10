"""Plan L's recipe steps (M153 and M154, DEC-1530 … DEC-1532, DEC-1540): more ways to fix a value, all stateless.

Every function here takes one column (and, for `correct_cells`, the ID column beside it) and the step's
frozen parameters, and returns a `ParseOutcome`: the new values, how many it changed and how many
non-empty values it could not handle, with masked examples. Nothing reads another row or a statistic of
the file (DEC-1004), so a step gives the same answer on next month's file and leaks nothing across the
train/test split. That is also why there is no "fill with the mean": a mean is learnt from the whole
file, and the model's own preprocessing does that inside the split.

`remove_rows` is the one step that changes the number of rows; it returns the rows to keep instead.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

import pandas as pd

from engine.agent.formats import ParseOutcome, masked_cut

__all__ = [
    "MAX_CORRECTIONS",
    "MAX_MAPPINGS",
    "MAX_MISSING_WORDS",
    "REMOVE_OPERATORS",
    "convert_units",
    "correct_cells",
    "fill_missing",
    "map_values",
    "remove_rows",
    "set_missing_text",
    "split_column",
    "valid_range",
]

MAX_MAPPINGS: Final[int] = 500
"""A `map_values` step lists at most this many spellings."""

MAX_MISSING_WORDS: Final[int] = 20
"""A `set_missing_text` step lists at most this many words."""

MAX_CORRECTIONS: Final[int] = 1000
"""A `correct_cells` step corrects at most this many cells (plan §12)."""

REMOVE_OPERATORS: Final[frozenset[str]] = frozenset({"equals", "in", "is_empty", "before", "after"})

_EXAMPLES: Final[int] = 5
_UNIT_VALUE: Final[re.Pattern[str]] = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*([A-Za-z%/]+)?\s*$")


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _examples(values: Sequence[Any]) -> tuple[str, ...]:
    return tuple(masked_cut(_text(value), 60) for value in list(dict.fromkeys(values))[:_EXAMPLES])


def map_values(series: pd.Series[Any], *, mapping: Mapping[str, str]) -> ParseOutcome:
    """Each listed spelling (compared trimmed, as written) replaced by its canonical one; the rest kept."""
    keys = {str(k).strip(): str(v) for k, v in mapping.items()}

    def one(value: Any) -> Any:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return value
        return keys.get(_text(value), value)

    out = series.map(one).astype(object)
    changed = int(sum(1 for a, b in zip(series, out, strict=True) if not _same(a, b)))
    return ParseOutcome(values=out, changed=changed, failed=0, failed_examples=())


def _same(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        if pd.isna(a) and pd.isna(b):
            return True
    except (TypeError, ValueError):
        pass
    return bool(a == b)


def set_missing_text(series: pd.Series[Any], *, values: Sequence[str]) -> ParseOutcome:
    """Every cell whose trimmed, lower-cased text is one of `values` made empty."""
    words = {str(v).strip().casefold() for v in values}
    mask = series.map(lambda v: isinstance(v, str) and v.strip().casefold() in words).astype(bool)
    out = series.mask(mask) if mask.any() else series.copy()
    return ParseOutcome(values=out, changed=int(mask.sum()), failed=0, failed_examples=())


def valid_range(series: pd.Series[Any], *, minimum: float | None, maximum: float | None) -> ParseOutcome:
    """Numbers below `minimum` or above `maximum` made empty; text that is not a number is counted, kept."""
    numbers = pd.to_numeric(series, errors="coerce")
    present = series.notna()
    not_number = present & numbers.isna()
    outside = pd.Series(False, index=series.index)
    if minimum is not None:
        outside |= numbers < minimum
    if maximum is not None:
        outside |= numbers > maximum
    out = series.mask(outside) if outside.any() else series.copy()
    return ParseOutcome(
        values=out,
        changed=int(outside.sum()),
        failed=int(not_number.sum()),
        failed_examples=_examples(list(series[not_number])),
    )


def convert_units(series: pd.Series[Any], *, units: Mapping[str, float], bare: str = "fail") -> ParseOutcome:
    """`"2 GB"` → 2 × units["GB"]: every value to one unit. A unit compares ignoring case.

    A number with no unit is kept as it is under `bare: "as_target"` (the column's own unit) and
    counted as not converted under `bare: "fail"`; an unknown unit is always counted.
    """
    factors = {str(k).strip().casefold(): float(v) for k, v in units.items()}
    values: list[Any] = []
    failed: list[Any] = []
    changed = 0
    for value in series:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            values.append(math.nan)
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if bare == "as_target":
                values.append(float(value))
            else:
                values.append(math.nan)
                failed.append(value)
            continue
        match = _UNIT_VALUE.match(str(value))
        unit = match.group(2).casefold() if match and match.group(2) else None
        if (
            match is None
            or (unit is None and bare != "as_target")
            or (unit is not None and unit not in factors)
        ):
            values.append(math.nan)
            failed.append(value)
            continue
        number = float(match.group(1).replace(",", "."))
        values.append(number * factors[unit] if unit is not None else number)
        changed += 1
    return ParseOutcome(
        values=pd.Series(values, index=series.index, dtype="float64"),
        changed=changed,
        failed=len(failed),
        failed_examples=_examples(failed),
    )


def split_column(series: pd.Series[Any], *, separator: str, part: int) -> ParseOutcome:
    """One part of each value split on `separator` (`part` 0 is the first, -1 the last), trimmed.

    A value with fewer parts gives an empty cell and is counted; an empty value stays empty.
    """
    values: list[Any] = []
    short: list[Any] = []
    for value in series:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            values.append(None)
            continue
        pieces = _text(value).split(separator)
        if -len(pieces) <= part < len(pieces) and (len(pieces) > 1 or part in (0, -1)):
            values.append(pieces[part].strip() or None)
        else:
            values.append(None)
            short.append(value)
    out = pd.Series(values, index=series.index, dtype=object)
    return ParseOutcome(
        values=out, changed=int(out.notna().sum()), failed=len(short), failed_examples=_examples(short)
    )


def fill_missing(series: pd.Series[Any], *, value: str | float) -> ParseOutcome:
    """Every empty cell given the one constant `value`; filled cells are kept."""
    empty = series.isna()
    if isinstance(value, str) and pd.api.types.is_numeric_dtype(series):
        out = series.astype(object).where(~empty, value)
    else:
        out = series.where(~empty, value)
    return ParseOutcome(values=out, changed=int(empty.sum()), failed=0, failed_examples=())


def correct_cells(
    series: pd.Series[Any], keys: pd.Series[Any], *, cells: Sequence[Mapping[str, Any]]
) -> ParseOutcome:
    """The person's corrections: where the row's ID is `key` and the cell still reads `old`, write `new`.

    Compared as trimmed text, so 7 and "7" are one ID. A correction whose ID is on no row, or whose
    cell no longer reads `old` (next month's value changed), is left alone and counted as `failed`,
    so the receipt says how many corrections still applied.
    """
    wanted: dict[str, tuple[str, Any]] = {}
    for cell in cells:
        wanted[_text(cell["key"])] = (
            _text(cell["old"]) if cell.get("old") is not None else "",
            cell.get("new"),
        )
    out = series.astype(object).copy()
    applied: set[str] = set()
    for index, (key, value) in enumerate(zip(keys, series, strict=True)):
        if key is None or (isinstance(key, float) and math.isnan(key)):
            continue
        found = wanted.get(_text(key))
        if found is None:
            continue
        old, new = found
        current = "" if value is None or (isinstance(value, float) and math.isnan(value)) else _text(value)
        if current == old:
            out.iat[index] = new
            applied.add(_text(key))
    missed = [key for key in wanted if key not in applied]
    return ParseOutcome(
        values=out, changed=len(applied), failed=len(missed), failed_examples=_examples(missed)
    )


def remove_rows(series: pd.Series[Any], *, operator: str, values: Sequence[Any]) -> pd.Series[Any]:
    """True for each row to keep: the rows the rule names are left out.

    `equals` / `in` compare trimmed text (so 7 and "7" match), `is_empty` needs no value, `before` /
    `after` compare dates (a value that is not a date is kept).
    """
    if operator in {"equals", "in"}:
        wanted = {_text(v) for v in values}
        hit = series.map(
            lambda v: v is not None and not (isinstance(v, float) and math.isnan(v)) and _text(v) in wanted
        )
    elif operator == "is_empty":
        hit = series.isna() | series.map(lambda v: isinstance(v, str) and not v.strip())
    else:
        when = pd.to_datetime(series, errors="coerce", utc=True)
        limit = pd.Timestamp(str(values[0]))
        limit = limit.tz_localize("UTC") if limit.tzinfo is None else limit.tz_convert("UTC")
        hit = (when < limit) if operator == "before" else (when > limit)
        hit = hit.fillna(False)
    return ~hit.astype(bool)
