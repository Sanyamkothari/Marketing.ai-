"""Money columns keep their currency (Plan O M242, the design §8.2): the `unit` of `parse_number` and the
`convert_currency` step.

Two recipe-level guarantees, both row-wise and stateless (DEC-1004) so a recipe replays the same on next month's file:

* **`parse_number` with `unit`** records the currency a money column is written in (`INR`, `USD`, ...). The numbers
  read exactly as they do without it; what it adds is a stop: a value that names **another** currency (`$12` in a
  rupee column) is `CURRENCY_MIXED`, and the run stops instead of turning dollars and rupees into one column of
  numbers that would later be added together. Without `unit` (every recipe saved before M242) nothing changes.
* **`convert_currency`** multiplies amounts by a rate a person gave, with the rate's date and source frozen in the
  step: `{"from": "USD", "to": "INR", "rate": 83.1, "rate_date": "2026-10-10", "source": "RBI reference rate"}` means
  one US dollar is 83.10 rupees. The product never fetches a rate. `rows` says which rows are in `from`:
  ``"all"`` (the default; a row naming another currency stops with `CURRENCY_MIXED`), ``"marked"`` (only the values
  that show the `from` sign or code; the rest are left as written for `parse_number` to read in `to`), or
  ``{"column": "currency", "values": ["USD", "US"]}`` (the rows whose `currency` cell is one of those spellings - the
  answer to "which rows are which"). Text is read with `parse_number`'s own reader (`decimal`, `indian_scale_words`),
  and each product is exact (`Decimal`), so `12.10` at `83.1` is `1005.51`, not `1005.5099999999999`.

A step never sums or averages anything; the receipt counts what was converted and what could not be read.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import numpy as np
import pandas as pd

from engine.agent.formats import ParseOutcome, currency_codes, masked_cut, parse_numbers

__all__ = [
    "CURRENCY_MIXED",
    "MAX_RATE",
    "MAX_ROW_VALUES",
    "MAX_SOURCE_CHARS",
    "CurrencyStepError",
    "check_convert_params",
    "check_unit_param",
    "convert_currency",
    "foreign_values",
    "is_currency_code",
]

CURRENCY_MIXED: Final[str] = "CURRENCY_MIXED"
MAX_RATE: Final[float] = 1_000_000.0
"""A rate above a million of one currency for one of another is a typing slip, not a rate."""
MAX_SOURCE_CHARS: Final[int] = 200
MAX_ROW_VALUES: Final[int] = 50
_EXAMPLES: Final[int] = 5
_CODE: Final[re.Pattern[str]] = re.compile(r"[A-Z]{3}")
_DATE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")


class CurrencyStepError(Exception):
    """A money step that cannot run on this file without adding currencies together (`CURRENCY_MIXED`)."""

    def __init__(self, code: str, message: str, *, rows: int = 0) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.rows = rows


def is_currency_code(value: Any) -> bool:
    """An ISO 4217-shaped code: three capital letters (`INR`, `USD`, `AED`)."""
    return isinstance(value, str) and _CODE.fullmatch(value) is not None


def _text_of(value: Any) -> str | None:
    """The cell as text when it is text; None for an empty cell or a real number (read by its value)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    return None


def foreign_values(series: pd.Series[Any], unit: str) -> tuple[int, tuple[str, ...]]:
    """How many values of `series` name a currency other than `unit` (or two currencies), with masked examples."""
    if not pd.api.types.is_object_dtype(series) and not pd.api.types.is_string_dtype(series):
        return 0, ()
    count = 0
    examples: list[str] = []
    table: dict[str, bool] = {}
    for value in series.tolist():
        text = _text_of(value)
        if text is None:
            continue
        if text not in table:
            codes = currency_codes(text)
            table[text] = bool(codes) and codes != {unit}
        if table[text]:
            count += 1
            if len(examples) < _EXAMPLES and masked_cut(text, 80) not in examples:
                examples.append(masked_cut(text, 80))
    return count, tuple(examples)


def check_unit_param(params: Mapping[str, Any], refuse: Callable[[str], Exception]) -> None:
    """`parse_number`'s `unit`: a currency code when given."""
    if "unit" in params and not is_currency_code(params["unit"]):
        raise refuse("unit is a currency code such as INR or USD")


def _finite_positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 < value <= MAX_RATE
    )


def check_convert_params(params: Mapping[str, Any], refuse: Callable[[str], Exception]) -> None:
    """`convert_currency`'s frozen rate: two different codes, a positive rate, a real date and the person's source."""
    source_code, target = params.get("from"), params.get("to")
    if not (is_currency_code(source_code) and is_currency_code(target)):
        raise refuse("from and to are currency codes such as USD and INR")
    if source_code == target:
        raise refuse("from and to are different currencies")
    if not _finite_positive(params.get("rate")):
        raise refuse(f"the rate is a number above 0 and at most {MAX_RATE:,.0f}")
    rate_date = params.get("rate_date")
    if not (isinstance(rate_date, str) and _DATE.fullmatch(rate_date)):
        raise refuse("the rate's date is written YYYY-MM-DD")
    try:
        date.fromisoformat(rate_date)
    except ValueError as exc:
        raise refuse("the rate's date is not a real date") from exc
    source = params.get("source")
    if not (isinstance(source, str) and source.strip() and len(source) <= MAX_SOURCE_CHARS):
        raise refuse(f"say where the rate comes from, in at most {MAX_SOURCE_CHARS} characters")
    rows = params.get("rows", "all")
    if isinstance(rows, dict):
        values = rows.get("values")
        if not (
            set(rows) == {"column", "values"}
            and isinstance(rows.get("column"), str)
            and rows["column"]
            and isinstance(values, list)
            and 0 < len(values) <= MAX_ROW_VALUES
            and all(isinstance(v, str) and v.strip() for v in values)
        ):
            raise refuse(f"rows names a column and 1 to {MAX_ROW_VALUES} of its values")
    elif rows not in {"all", "marked"}:
        raise refuse("rows is 'all', 'marked' or a column with its values")
    if params.get("decimal", ".") not in {".", ","}:
        raise refuse("decimal must be '.' or ','")
    if not isinstance(params.get("indian_scale_words", False), bool):
        raise refuse("indian_scale_words is true or false")


def _exact_product(number: float, rate: float) -> float:
    try:
        return float(Decimal(repr(float(number))) * Decimal(repr(float(rate))))
    except InvalidOperation:  # pragma: no cover - a finite float always has a decimal form
        return float(number) * float(rate)


def _is_number(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
        return False
    return not math.isnan(float(value))


def _key(value: Any) -> str | None:
    """A selector cell as `rows.values` spells it (trimmed, casefolded); None when it is empty."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    return text.strip().casefold() or None


def _selected(
    series: pd.Series[Any], source_code: str, rows: Any, selector: pd.Series[Any] | None
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """(the rows in `from` by the step's `rows` rule, the non-empty rows); refuses (`CURRENCY_MIXED`) a chosen row
    naming another currency."""
    values = series.tolist()
    present = np.array([_text_of(v) is not None or _is_number(v) for v in values], dtype=bool)
    codes = [currency_codes(v) if isinstance(v, str) else frozenset() for v in values]
    if rows == "marked":
        chosen = np.array([c == {source_code} for c in codes], dtype=bool)
        mixed = [i for i, c in enumerate(codes) if len(c) > 1]
    else:
        if isinstance(rows, dict):
            if selector is None:  # pragma: no cover - the recipe passes the column `rows` names
                raise ValueError("rows names a column the step was not given")
            wanted = {str(v).strip().casefold() for v in rows["values"]}
            chosen = np.array([_key(s) in wanted for s in selector.tolist()], dtype=bool)
        else:
            chosen = np.ones(len(values), dtype=bool)
        chosen &= present
        mixed = [i for i, c in enumerate(codes) if chosen[i] and c and c != {source_code}]
    if mixed:
        examples = tuple(dict.fromkeys(masked_cut(str(values[i]), 80) for i in mixed[:50]))[:_EXAMPLES]
        raise CurrencyStepError(
            CURRENCY_MIXED,
            f"{len(mixed)} values to convert from {source_code} name another currency "
            f"(for example {', '.join(examples)}); amounts in different currencies are never added together.",
            rows=len(mixed),
        )
    return np.asarray(chosen & present, dtype=bool), present


def _left_foreign(
    series: pd.Series[Any],
    chosen: np.ndarray[Any, Any],
    present: np.ndarray[Any, Any],
    *,
    source_code: str,
    allowed: frozenset[str],
) -> None:
    """Refuses (`CURRENCY_MIXED`) a row a partial conversion leaves as written whose value names a currency outside
    `allowed` (the currency converted to, and those a later step of the recipe converts on this column): the next
    `parse_number` - with or without a `unit` - would read `€5` as 5 of the column's currency."""
    values = series.tolist()
    left = [
        i
        for i in np.flatnonzero(present & ~chosen)
        if isinstance(values[i], str) and not currency_codes(values[i]) <= allowed
    ]
    if left:
        examples = tuple(dict.fromkeys(masked_cut(str(values[i]), 80) for i in left[:50]))[:_EXAMPLES]
        raise CurrencyStepError(
            CURRENCY_MIXED,
            f"{len(left)} values left unconverted by the conversion from {source_code} name a currency other than "
            f"{' or '.join(sorted(allowed))} (for example {', '.join(examples)}); amounts in different currencies "
            "are never added together. Say which rows are in which currency, or convert them at a rate you give.",
            rows=len(left),
        )


def convert_currency(
    series: pd.Series[Any],
    *,
    source_code: str,
    rate: float,
    rows: Any = "all",
    selector: pd.Series[Any] | None = None,
    decimal: str = ".",
    indian_scale_words: bool = False,
    target: str | None = None,
    pending: frozenset[str] = frozenset(),
) -> ParseOutcome:
    """The amounts of `series` in `from`, times `rate`; rows `rows` does not choose are left exactly as written.

    `selector` is the column `rows` names (`{"column": ..., "values": [...]}`), else None. `target` is the step's
    `to`: with it, a partial conversion (`rows` not "all") guards itself - a row it leaves as written that names a
    currency other than `target` (or one of `pending`, the currencies later steps convert on this column) stops with
    `CURRENCY_MIXED`, so no later step reads foreign money as the column's currency.
    """
    chosen, present = _selected(series, source_code, rows, selector)
    if target is not None and rows != "all":
        _left_foreign(series, chosen, present, source_code=source_code, allowed=frozenset({target}) | pending)
    positions = np.flatnonzero(chosen)
    if not len(positions):
        return ParseOutcome(values=series.copy(), changed=0, failed=0, failed_examples=())
    picked = series.iloc[positions]
    read = parse_numbers(
        picked.astype(object),
        decimal=decimal,
        percent_to_fraction=False,
        indian_scale_words=indian_scale_words,
    )
    numbers = read.values.to_numpy(dtype="float64")
    table: dict[float, float] = {}
    converted = np.full(len(numbers), np.nan, dtype="float64")
    for i, number in enumerate(numbers):
        if math.isnan(number):
            continue
        if number not in table:
            table[number] = _exact_product(number, rate)
        converted[i] = table[number]
    if rows == "all" or bool((chosen == present).all()):
        values = pd.Series(np.full(len(series), np.nan, dtype="float64"), index=series.index, dtype="float64")
        values.iloc[positions] = converted
    else:
        values = series.astype(object).copy()
        out = values.to_numpy(dtype=object)
        for position, number in zip(positions, converted, strict=True):
            out[position] = None if math.isnan(number) else float(number)
        values = pd.Series(out, index=series.index, dtype=object)
    changed = int((~np.isnan(converted)).sum())
    return ParseOutcome(
        values=values,
        changed=changed,
        failed=read.failed,
        failed_examples=read.failed_examples,
    )
