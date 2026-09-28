"""Messy-format detection and the stateless parsers behind the recipe's cleaning steps (Plan G §6.2).

Two halves share one module so they cannot disagree: the detector says "this column holds numbers
written as text" only when the parser that `parse_number` steps run would actually convert them.

**Detection** (`find_format_issues`) looks at text columns only - a column pandas already reads as
numbers, dates or booleans needs nothing - and reports five kinds of issue, each with counts, masked
examples and the parameters a fix would use:

* `number_as_text` - `"₹1,200"`, `"Rs. 1,20,000"`, `"45%"`, `"(300)"`, `"1.200,50"`;
* `mixed_dates` - one column in several date styles, or day/month order that has to be decided;
* `boolean_as_text` - `Y` / `yes` / `TRUE` / `0` spellings of a two-valued flag;
* `category_variants` - `"Delhi"`, `"delhi "`, `"DELHI"` as three values of one category;
* `untrimmed_text` - values with leading or trailing spaces and nothing else wrong.

**Parsing** (`parse_numbers`, `parse_dates`, `map_booleans`, `normalise_texts`) is row-wise and
stateless (DEC-1004): each output depends only on the value and the parameters, and every function
returns what it could not convert so a receipt can count it. An empty cell stays empty and is never
counted as a failure.

Examples shown to a person or a prompt pass through `engine.pii.redact_cells` first.

**Speed** (M77, `reports/plan_g_performance.md`): a parser's output for a cell depends only on the
cell's text, so each parser runs once per *distinct* value and the answers are spread back over the
rows with a hash lookup. A million-row column of a few thousand spellings costs a few thousand
parses; a column where every value differs costs one pass of the same function the row loop ran,
without the per-row `Series.at` write that dominated before. The semantics are the row loop's,
value for value - `tests/unit/agent/test_formats.py` compares the two.
"""

from __future__ import annotations

import re
import warnings
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

import numpy as np
import pandas as pd

from engine.pii import redact_cells

__all__ = [
    "BOOLEAN_FALSE",
    "BOOLEAN_TRUE",
    "CURRENCY_TOKENS",
    "FormatIssue",
    "FormatIssueKind",
    "ParseOutcome",
    "date_order",
    "find_format_issues",
    "map_booleans",
    "normalise_texts",
    "parse_dates",
    "parse_numbers",
]

MAX_EXAMPLES: Final[int] = 5
MIN_CONVERT_SHARE: Final[float] = 0.50
"""A text column is reported as numbers or dates only when at least this share of its values converts."""
MAX_CATEGORY_DISTINCT: Final[int] = 500
"""Above this many distinct values a text column is free text or an id, not a category to tidy."""

CURRENCY_TOKENS: Final[tuple[str, ...]] = ("INR", "Rs.", "Rs", "₹", "USD", "US$", "$", "EUR", "€", "GBP", "£")
"""Stripped from a number before parsing; longest spellings first so `Rs.` wins over `Rs`."""

BOOLEAN_TRUE: Final[frozenset[str]] = frozenset({"true", "t", "yes", "y", "1"})
BOOLEAN_FALSE: Final[frozenset[str]] = frozenset({"false", "f", "no", "n", "0"})
"""The same tokens as `engine.stages.ingest.BOOL_TRUE_TOKENS` / `BOOL_FALSE_TOKENS`, compared casefolded."""

_CURRENCY_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(token) for token in CURRENCY_TOKENS), flags=re.IGNORECASE
)
_EU_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"^[+-]?\d{1,3}(\.\d{3})+(,\d+)?$|^[+-]?\d+,\d{1,2}$")
_PLAIN_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

_DATE_STYLES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("iso", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?")),
    ("slash", re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}")),
    ("dash", re.compile(r"^\d{1,2}-\d{1,2}-\d{2,4}")),
    ("dot", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}")),
    ("day_month_name", re.compile(r"^\d{1,2}[ -][A-Za-z]{3,9}[ -,]*\d{2,4}")),
    ("month_name_day", re.compile(r"^[A-Za-z]{3,9} \d{1,2},? \d{2,4}")),
    ("compact", re.compile(r"^\d{8}$")),
)
_NUMERIC_DAY_MONTH: Final[re.Pattern[str]] = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}")


class FormatIssueKind(StrEnum):
    NUMBER_AS_TEXT = "number_as_text"
    MIXED_DATES = "mixed_dates"
    BOOLEAN_AS_TEXT = "boolean_as_text"
    CATEGORY_VARIANTS = "category_variants"
    UNTRIMMED_TEXT = "untrimmed_text"


@dataclass(frozen=True)
class ParseOutcome:
    """A parsed column and what could not be parsed. `failed` counts non-empty cells only."""

    values: pd.Series[Any]
    changed: int
    failed: int
    failed_examples: tuple[str, ...]


@dataclass(frozen=True)
class FormatIssue:
    """One column's formatting problem, measured on the whole column."""

    column: str
    kind: FormatIssueKind
    non_empty: int
    convertible: int
    failed: int
    examples: tuple[str, ...]
    failed_examples: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def convert_share(self) -> float:
        return self.convertible / self.non_empty if self.non_empty else 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "kind": self.kind.value,
            "non_empty": self.non_empty,
            "convertible": self.convertible,
            "failed": self.failed,
            "convert_share": round(self.convert_share, 4),
            "examples": list(self.examples),
            "failed_examples": list(self.failed_examples),
            "params": self.params,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _text_cells(series: pd.Series[Any]) -> pd.Series[Any]:
    """Non-empty cells as stripped strings, index kept."""
    present = series.dropna()
    text = present.astype(str)
    return text[text.str.strip() != ""]


def _present(series: pd.Series[Any]) -> tuple[np.ndarray[Any, Any], pd.Series[Any]]:
    """(row positions, text) of the non-empty cells of `series` - the cells a parser reads."""
    notna = series.notna().to_numpy()
    text = series[notna].astype(str)
    keep = (text.str.strip() != "").to_numpy()
    return np.flatnonzero(notna)[keep], text[keep]


def _per_value(cells: pd.Series[Any], fn: Callable[[str], Any]) -> list[Any]:
    """`fn` of every cell, computed once per distinct value; the order of `cells`."""
    values = cells.tolist()
    table = {value: fn(value) for value in dict.fromkeys(values)}
    return [table[value] for value in values]


def _masked(values: Sequence[str]) -> tuple[str, ...]:
    return redact_cells(value[:80] for value in values[:MAX_EXAMPLES])


def _distinct_examples(values: pd.Series[Any]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.append(text)
        if len(seen) >= MAX_EXAMPLES:
            break
    return _masked(seen)


def _is_text(series: pd.Series[Any]) -> bool:
    return bool(pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series))


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
def _number_one(text: str, decimal: str, percent_to_fraction: bool) -> float | None:
    raw = text.strip()
    if not raw:
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative, raw = True, raw[1:-1].strip()
    percent = raw.endswith("%")
    if percent:
        raw = raw[:-1].strip()
    raw = _CURRENCY_RE.sub("", raw).strip()
    if raw.startswith("-"):
        negative, raw = (not negative), raw[1:].strip()
    elif raw.startswith("+"):
        raw = raw[1:].strip()
    raw = raw.replace(" ", "").replace(" ", "").replace("'", "")
    # Grouping marks go; the decimal mark becomes a point.
    raw = raw.replace(".", "").replace(",", ".") if decimal == "," else raw.replace(",", "")
    if not _PLAIN_NUMBER_RE.match(raw):
        return None
    number = float(raw)
    if percent and percent_to_fraction:
        number = number / 100.0
    return -number if negative else number


def parse_numbers(
    series: pd.Series[Any], *, decimal: str = ".", percent_to_fraction: bool = True
) -> ParseOutcome:
    """Text to float: currency symbols, thousands separators, `%` and `(negative)` handled.

    `decimal` is `"."` (1,200.50) or `","` (1.200,50). Numbers already numeric pass through.
    """
    if decimal not in {".", ","}:
        raise ValueError("decimal must be '.' or ','")
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return ParseOutcome(values=series.astype("float64"), changed=0, failed=0, failed_examples=())
    positions, cells = _present(series)
    numbers = _per_value(cells, lambda text: _number_one(text, decimal, percent_to_fraction))
    parsed = np.array([np.nan if number is None else number for number in numbers], dtype="float64")
    failed = np.array([number is None for number in numbers], dtype=bool)
    values = np.full(len(series), np.nan, dtype="float64")
    values[positions] = parsed
    failures = cells[failed].head(MAX_EXAMPLES).tolist()
    return ParseOutcome(
        values=pd.Series(values, index=series.index, dtype="float64"),
        changed=int((~failed).sum()),  # text became a number, whatever it looked like
        failed=int(failed.sum()),
        failed_examples=_masked(failures),
    )


def _decimal_style(cells: pd.Series[Any]) -> str:
    eu = int(cells.str.strip().str.match(_EU_NUMBER_RE).sum())
    us = int(
        cells.str.contains(r"\d,\d{3}(?!\d)", regex=True).sum()
        + cells.str.contains(r"\.\d", regex=True).sum()
    )
    return "," if eu > us else "."


def _number_issue(name: str, cells: pd.Series[Any]) -> FormatIssue | None:
    if cells.empty:
        return None
    decimal = _decimal_style(cells)
    parsed = parse_numbers(cells, decimal=decimal)
    convertible = int(parsed.values.notna().sum())
    if convertible / len(cells) < MIN_CONVERT_SHARE:
        return None
    currency = sorted({m.group(0) for text in cells.head(2000) for m in _CURRENCY_RE.finditer(text)})
    percent = int(cells.str.strip().str.endswith("%").sum())
    notes: list[str] = []
    if currency:
        notes.append(f"currency symbols: {', '.join(currency)}")
    if percent:
        notes.append(f"{percent} values end in %, read as fractions")
    if decimal == ",":
        notes.append("comma is the decimal mark")
    return FormatIssue(
        column=name,
        kind=FormatIssueKind.NUMBER_AS_TEXT,
        non_empty=len(cells),
        convertible=convertible,
        failed=parsed.failed,
        examples=_distinct_examples(cells),
        failed_examples=parsed.failed_examples,
        params={"decimal": decimal, "percent_to_fraction": True},
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def _style_of(text: str) -> str | None:
    for style, pattern in _DATE_STYLES:
        if pattern.match(text.strip()):
            return style
    return None


def date_order(cells: pd.Series[Any]) -> bool | None:
    """Whether numeric day/month dates put the day first: True, False, or None when every value fits both."""
    day_first = month_first = False
    for text in dict.fromkeys(str(value) for value in cells):  # each spelling once; the answer is an OR
        match = _NUMERIC_DAY_MONTH.match(text.strip())
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12 >= second:
            day_first = True
        elif second > 12 >= first:
            month_first = True
    if day_first and not month_first:
        return True
    if month_first and not day_first:
        return False
    return None


def _one_datetime(text: str, dayfirst: bool) -> pd.Timestamp | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            stamp = pd.to_datetime(text, format="mixed", dayfirst=dayfirst, errors="coerce")
        except (ValueError, TypeError, OverflowError):
            return None
    if stamp is None or pd.isna(stamp):
        return None
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def _to_datetimes(cells: pd.Series[Any], *, dayfirst: bool) -> pd.Series[Any]:
    """Each value parsed on its own, as `datetime64[ns]`; a value with a UTC offset keeps its clock time.

    `pd.to_datetime(format="mixed")` does this in one call unless the column mixes UTC offsets (or
    offsets and none), where it returns objects - or, in later pandas, raises - and the step used to
    fail with a 500. That case falls back to one value at a time with the same rule (M77).
    """
    distinct = pd.Series(list(dict.fromkeys(cells.tolist())), dtype=object)
    parsed: pd.Series[Any] | None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            parsed = pd.to_datetime(distinct, format="mixed", dayfirst=dayfirst, errors="coerce")
        except (ValueError, TypeError, OverflowError):
            parsed = None
    if parsed is None or not pd.api.types.is_datetime64_any_dtype(parsed):
        stamps = [_one_datetime(str(text), dayfirst) for text in distinct]
        parsed = pd.Series([pd.NaT if s is None else s for s in stamps], dtype="datetime64[ns]")
    # Each distinct spelling parsed once (format="mixed" parses values independently), then spread.
    lookup = pd.Series(parsed.array, index=pd.Index(distinct, dtype=object))
    spread = lookup.reindex(pd.Index(cells.tolist(), dtype=object))
    return pd.Series(spread.array, index=cells.index)


def parse_dates(series: pd.Series[Any], *, dayfirst: bool) -> ParseOutcome:
    """Text to datetime, one value at a time (`format="mixed"`), with the day/month order decided."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return ParseOutcome(values=series, changed=0, failed=0, failed_examples=())
    cells = _text_cells(series)
    parsed = _to_datetimes(cells, dayfirst=dayfirst)
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    ok = parsed.notna()
    out.loc[parsed.index[ok]] = parsed[ok].dt.tz_localize(None) if parsed.dt.tz is not None else parsed[ok]
    failures = cells[~ok].tolist()
    return ParseOutcome(
        values=out, changed=int(ok.sum()), failed=len(failures), failed_examples=_masked(failures)
    )


def _date_issue(name: str, cells: pd.Series[Any]) -> FormatIssue | None:
    if cells.empty or not cells.str.contains(r"\d", regex=True).all():
        return None
    styles = Counter(style for style in (_style_of(text) for text in cells.head(5000)) if style is not None)
    if sum(styles.values()) / min(len(cells), 5000) < MIN_CONVERT_SHARE:
        return None
    order = date_order(cells)
    parsed = parse_dates(cells, dayfirst=bool(order))
    convertible = len(cells) - parsed.failed
    if convertible / len(cells) < MIN_CONVERT_SHARE:
        return None
    ambiguous_order = order is None and bool(cells.str.match(_NUMERIC_DAY_MONTH).any())
    if len(styles) < 2 and not ambiguous_order and order is None:
        return None
    notes = [f"{count} values in {style} style" for style, count in styles.most_common()]
    if ambiguous_order:
        notes.append("day and month order cannot be told from the values")
    return FormatIssue(
        column=name,
        kind=FormatIssueKind.MIXED_DATES,
        non_empty=len(cells),
        convertible=convertible,
        failed=parsed.failed,
        examples=_distinct_examples(cells),
        failed_examples=parsed.failed_examples,
        params={"dayfirst": order},
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Booleans
# ---------------------------------------------------------------------------
def map_booleans(
    series: pd.Series[Any], *, true_values: Sequence[str], false_values: Sequence[str]
) -> ParseOutcome:
    """Listed spellings to 1.0 / 0.0, compared stripped and casefolded; anything else fails."""
    truthy = {value.strip().casefold() for value in true_values}
    falsy = {value.strip().casefold() for value in false_values}
    if truthy & falsy:
        raise ValueError(f"a value cannot be both true and false: {sorted(truthy & falsy)}")

    def one(text: str) -> float:
        key = text.strip().casefold()
        return 1.0 if key in truthy else 0.0 if key in falsy else np.nan

    positions, cells = _present(series)
    parsed = np.array(_per_value(cells, one), dtype="float64")
    failed = np.isnan(parsed)
    values = np.full(len(series), np.nan, dtype="float64")
    values[positions] = parsed
    return ParseOutcome(
        values=pd.Series(values, index=series.index, dtype="float64"),
        changed=int((~failed).sum()),
        failed=int(failed.sum()),
        failed_examples=_masked(cells[failed].head(MAX_EXAMPLES).tolist()),
    )


def _boolean_issue(name: str, cells: pd.Series[Any]) -> FormatIssue | None:
    if cells.empty:
        return None
    spellings = Counter(cells.str.strip())
    keys = {spelling.casefold() for spelling in spellings}
    if not keys <= BOOLEAN_TRUE | BOOLEAN_FALSE or not keys & BOOLEAN_TRUE or not keys & BOOLEAN_FALSE:
        return None
    if keys <= {"0", "1"} and len(spellings) == len(keys):
        return None  # plain 0/1 text reads as numbers already
    true_values = sorted(s for s in spellings if s.casefold() in BOOLEAN_TRUE)
    false_values = sorted(s for s in spellings if s.casefold() in BOOLEAN_FALSE)
    return FormatIssue(
        column=name,
        kind=FormatIssueKind.BOOLEAN_AS_TEXT,
        non_empty=len(cells),
        convertible=len(cells),
        failed=0,
        examples=_masked([f"{s} ({spellings[s]})" for s, _ in spellings.most_common()]),
        failed_examples=(),
        params={"true_values": true_values, "false_values": false_values},
        notes=(f"{len(spellings)} spellings of yes / no",),
    )


# ---------------------------------------------------------------------------
# Categories and whitespace
# ---------------------------------------------------------------------------
def normalise_texts(series: pd.Series[Any], *, merge: Mapping[str, str], strip: bool = True) -> ParseOutcome:
    """Strip each value and replace listed spellings with their canonical value; others pass through."""

    def one(text: str) -> str:
        value = text.strip() if strip else text
        return merge.get(value, value)

    out = series.copy()
    positions, cells = _present(series)
    tidy = np.array(_per_value(cells, one), dtype=object)
    differs = tidy != cells.to_numpy(dtype=object)
    if differs.any():
        out.iloc[positions[differs]] = tidy[differs]
    return ParseOutcome(values=out, changed=int(differs.sum()), failed=0, failed_examples=())


def _category_issue(name: str, cells: pd.Series[Any]) -> FormatIssue | None:
    if cells.empty:
        return None
    counts = Counter(cells)
    if len(counts) > MAX_CATEGORY_DISTINCT:
        return None
    groups: dict[str, list[str]] = {}
    for spelling in counts:
        groups.setdefault(" ".join(spelling.split()).casefold(), []).append(spelling)
    merge: dict[str, str] = {}
    for spellings in groups.values():
        # File order, so a tie goes to the spelling the file uses first, not to whichever sorts first.
        stripped = list(dict.fromkeys(" ".join(s.split()) for s in spellings))
        if len(stripped) < 2:
            continue
        canonical = max(stripped, key=lambda s: sum(counts[o] for o in spellings if " ".join(o.split()) == s))
        for spelling in spellings:
            if " ".join(spelling.split()) != canonical:
                merge[" ".join(spelling.split())] = canonical
    untrimmed = [s for s in counts if s != s.strip()]
    if not merge and not untrimmed:
        return None
    affected = sum(counts[s] for s in counts if s != s.strip() or s.strip() in merge)
    kind = FormatIssueKind.CATEGORY_VARIANTS if merge else FormatIssueKind.UNTRIMMED_TEXT
    examples = (
        sorted(merge.items())[:MAX_EXAMPLES] if merge else [(s, s.strip()) for s in untrimmed[:MAX_EXAMPLES]]
    )
    return FormatIssue(
        column=name,
        kind=kind,
        non_empty=len(cells),
        convertible=affected,
        failed=0,
        examples=_masked([f"{a!r} → {b!r}" for a, b in examples]),
        failed_examples=(),
        params={"strip": True, "merge": dict(sorted(merge.items()))},
        notes=(f"{len(merge)} spellings merge into others",) if merge else ("values have extra spaces",),
    )


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------
def find_format_issues(
    frame: pd.DataFrame, *, columns: Sequence[str] | None = None
) -> tuple[FormatIssue, ...]:
    """Every formatting problem in the text columns of `frame`, one issue per column at most.

    Checks run in order - numbers, dates, booleans, categories - and the first that fits wins, so a
    column of `"1,200"` is reported as numbers, never also as a category with variants.
    """
    names = list(frame.columns) if columns is None else [c for c in columns if c in frame.columns]
    issues: list[FormatIssue] = []
    for name in names:
        series = frame[name]
        if not _is_text(series):
            continue
        cells = _text_cells(series)
        if cells.empty:
            continue
        for detector in (_number_issue, _date_issue, _boolean_issue, _category_issue):
            issue = detector(str(name), cells)
            if issue is not None:
                issues.append(issue)
                break
    return tuple(issues)
