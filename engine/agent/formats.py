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

Examples shown to a person or a prompt are masked whole with the gate's scanner set (`masked_cut`) and then cut.

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
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

import numpy as np
import pandas as pd

from engine.agent.egress import assert_clean
from engine.agent.untrusted import quoted
from engine.pii import REDACTION_MARKER_PATTERN
from engine.stages import ingest

__all__ = [
    "BOOLEAN_FALSE",
    "BOOLEAN_TRUE",
    "CURRENCY_CODES",
    "CURRENCY_TOKENS",
    "INDIAN_SCALE_WORDS",
    "FormatIssue",
    "FormatIssueKind",
    "ParseOutcome",
    "currency_codes",
    "date_order",
    "date_order_conflict",
    "decimal_evidence",
    "find_excel_serial_dates",
    "find_format_issues",
    "map_booleans",
    "masked_cut",
    "normalise_texts",
    "order_unsettled",
    "parse_dates",
    "parse_excel_serial_dates",
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

INDIAN_SCALE_WORDS: Final[dict[str, int]] = {
    "k": 10**3,
    "m": 10**6,
    "l": 10**5,
    "lac": 10**5,
    "lacs": 10**5,
    "lakh": 10**5,
    "lakhs": 10**5,
    "cr": 10**7,
    "crore": 10**7,
    "crores": 10**7,
}
"""Plan O M241 (the design §8.1): the number words `parse_number` reads when its `indian_scale_words` parameter
is on, compared casefolded - `₹2.5 L` is 250000, `1.2 Cr` 12000000, `12K` 12000. A trailing `/-` (`Rs 3,50,000/-`)
is read too. Off (the default, and every recipe saved before M241), none of them is a number, as before."""

_SCALED_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<number>.*?\d\.?)(?P<word>[A-Za-z]+)\.?$")
"""A number followed by one word (spaces already removed): `2.5L`, `1.2Cr`, `12K`, `5lakhs`."""

_CURRENCY_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(token) for token in CURRENCY_TOKENS), flags=re.IGNORECASE
)
CURRENCY_CODES: Final[dict[str, str]] = {
    "inr": "INR",
    "rs.": "INR",
    "rs": "INR",
    "₹": "INR",
    "usd": "USD",
    "us$": "USD",
    "$": "USD",
    "eur": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "£": "GBP",
}
"""Plan O M242: each of `CURRENCY_TOKENS`, casefolded, to the currency it names (ISO 4217). A bare `$` is read as
US dollars; the question that asks about a foreign currency names it, so a person can say otherwise."""


def currency_codes(text: str) -> frozenset[str]:
    """The currencies `text` names with a sign or code (`₹`, `Rs`, `$`, `USD`, ...); empty when it names none.

    Plan O M242. Two codes mean the value itself is mixed (`$12 (₹1,000)`), which no step reads.
    """
    return frozenset(CURRENCY_CODES[match.group(0).casefold()] for match in _CURRENCY_RE.finditer(text))


_PIN_NAME_RE: Final[re.Pattern[str]] = re.compile(r"(?i)(?:^|[^a-z0-9])pin(?:code)?$")
"""`pin`, `pincode`: ingest's ID-name rule leaves them out (`pin_code` and `PIN Code` it has)."""
_PLAIN_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_PLAIN_COMMA_RE: Final[re.Pattern[str]] = re.compile(r"^[+-]?(\d+(,\d*)?|,\d+)([eE][+-]?\d+)?$")
_GROUPED_RE: Final[dict[str, re.Pattern[str]]] = {
    # 1,200.50 / 1,000,000 and the Indian 1,20,000; the first group never starts with 0.
    ".": re.compile(r"^([1-9]\d{0,2}(,\d{3})+|[1-9]\d?(,\d{2})*,\d{3})(\.\d*)?$"),
    # 1.200,50 / 1.000.000
    ",": re.compile(r"^[1-9]\d{0,2}(\.\d{3})+(,\d*)?$"),
}
"""A grouping mark is accepted only where grouping puts it; anything else is not a number."""
_COMMA_THOUSANDS_RE: Final[re.Pattern[str]] = re.compile(r"^[1-9]\d{0,2}(,\d{3})+$")
"""`1,200`: a comma before exactly three digits reads as grouping when the column says nothing else."""
_NUMERIC_TYPES: Final[tuple[type, ...]] = (int, float, np.number)

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
_HAS_DIGIT_RE: Final[re.Pattern[str]] = re.compile(r"\d")
_TIME_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\d{1,2}:\d{2}(:\d{2}([.,]\d+)?)?\s*([AaPp]\.?[Mm]\.?)?\s*$"
)
"""`09:30`, `9:30:15`, `9:30 pm`: a time of day names no date, and pandas would use today's."""


def _is_dateable(text: str) -> bool:
    """Whether `text` can hold a date: it has a digit and is not only a time of day."""
    return bool(_HAS_DIGIT_RE.search(text)) and _TIME_ONLY_RE.match(text) is None


class FormatIssueKind(StrEnum):
    NUMBER_AS_TEXT = "number_as_text"
    MIXED_DATES = "mixed_dates"
    BOOLEAN_AS_TEXT = "boolean_as_text"
    CATEGORY_VARIANTS = "category_variants"
    UNTRIMMED_TEXT = "untrimmed_text"
    EXCEL_SERIAL_DATES = "excel_serial_dates"
    """Plan O M240 (DEC-1810 (d)): dates stored as Excel's day numbers (`44927`). Reported by
    `find_excel_serial_dates` only, never by `find_format_issues`, so that tool's output is unchanged."""


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
    example_cells: tuple[str, ...] = ()
    """The cells that `examples` quote (`'Basic' → 'BASIC'` holds `Basic` and `BASIC`), each masked and cut
    on its own, so a sentence built from `examples` can be found piece by piece (`advisor._cells`)."""

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
            "params": _params_json(self.params),
            "notes": list(self.notes),
            "example_cells": list(self.example_cells),
        }


def _params_json(params: Mapping[str, Any]) -> dict[str, Any]:
    """A fix's parameters as a *tool result* shows them: `merge` is a list of `{from, to}` pairs.

    The recipe step keeps `merge` as a dict keyed by the spelling (`normalise_texts` looks it up), but a
    dict keyed by cell values would put cells where only labels belong (`egress` reads a key as a label),
    so the result lists the pairs and `advisor` builds the dict back (`merge_from_pairs`).
    """
    merge = params.get("merge")
    if not isinstance(merge, Mapping):
        return dict(params)
    return {
        **params,
        "merge": [{"from": str(spelling), "to": str(canonical)} for spelling, canonical in merge.items()],
    }


def merge_from_pairs(pairs: Any) -> dict[str, str]:
    """`{spelling: canonical}` from the `{from, to}` pairs `find_format_issues` returns (or a dict as is)."""
    if isinstance(pairs, Mapping):
        return {str(k): str(v) for k, v in pairs.items()}
    return {
        str(pair["from"]): str(pair["to"])
        for pair in (pairs or [])
        if isinstance(pair, Mapping) and "from" in pair and "to" in pair
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


def _numeric_cells(series: pd.Series[Any], positions: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Which of the cells at `positions` are real numbers (not text, not booleans).

    A chunked CSV read can hand a text column that mixes Python floats with strings; such a cell is
    read by its value, never through `str()` - `"1200.0"` is not evidence of a decimal point, and
    under a comma decimal mark it would read as 12000.
    """
    if pd.api.types.is_bool_dtype(series):
        return np.zeros(len(positions), dtype=bool)
    if pd.api.types.is_numeric_dtype(series):
        return np.ones(len(positions), dtype=bool)
    if not pd.api.types.is_object_dtype(series):
        return np.zeros(len(positions), dtype=bool)
    raw = series.to_numpy(dtype=object)[positions]
    return np.fromiter(
        (isinstance(v, _NUMERIC_TYPES) and not isinstance(v, (bool, np.bool_)) for v in raw),
        dtype=bool,
        count=len(raw),
    )


def _per_value(cells: pd.Series[Any], fn: Callable[[str], Any]) -> list[Any]:
    """`fn` of every cell, computed once per distinct value; the order of `cells`."""
    values = cells.tolist()
    table = {value: fn(value) for value in dict.fromkeys(values)}
    return [table[value] for value in values]


def _masked(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(masked_cut(value, 80) for value in values[:MAX_EXAMPLES])


def masked_cut(value: str, limit: int) -> str:
    """`value` masked whole, then cut to `limit` characters; a marker the cut would split is left out.

    Masked with the complete scanner set (`egress.assert_clean`: e-mail, phone, PAN, Aadhaar, card, IBAN, IP,
    URL, token, nine-digit run) and never cut first, so half of a secret can never escape the masker.
    White space is kept as written: a leading or doubled space is what an example is there to show.
    """
    masked = assert_clean(value)[0]
    cut = limit
    for marker in REDACTION_MARKER_PATTERN.finditer(masked):
        if marker.start() < cut < marker.end():
            cut = marker.start()
    return masked[:cut]


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
def _plain_number(core: str, decimal: str) -> str | None:
    """`core` (no sign, symbol or spaces) as text `float()` reads, or None when it is not a number
    written with `decimal` as the decimal mark.

    A grouping mark is removed only where grouping puts it, so a value written in the other
    convention - `1,5` under a decimal point, `1.5` under a decimal comma - fails and is counted,
    instead of being read as 15.
    """
    if decimal == ",":
        if _PLAIN_COMMA_RE.match(core):
            return core.replace(",", ".")
        return core.replace(".", "").replace(",", ".") if _GROUPED_RE[","].match(core) else None
    if _PLAIN_NUMBER_RE.match(core):
        return core
    return core.replace(",", "") if _GROUPED_RE["."].match(core) else None


def _scaled(raw: str, decimal: str) -> float | None:
    """`raw` (no sign, symbol or spaces) read with a number word or a trailing `/-` (M241); None when it is not
    such a number. Multiplied exactly (`Decimal`), so `1.2 Cr` is 12000000 and not 11999999.999999998."""
    if raw.endswith("/-"):
        raw = raw[:-2]
    match = _SCALED_RE.match(raw)
    scale = 1
    if match is not None:
        word = INDIAN_SCALE_WORDS.get(match.group("word").casefold())
        if word is None:
            return None
        raw, scale = match.group("number"), word
    plain = _plain_number(raw, decimal)
    if plain is None:
        return None
    try:
        return float(Decimal(plain) * scale)
    except InvalidOperation:  # pragma: no cover - `_plain_number` only returns what `float()` reads
        return None


def _number_one(
    text: str, decimal: str, percent_to_fraction: bool, scale_words: bool = False
) -> float | None:
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
    plain = _plain_number(raw, decimal)
    if plain is None and scale_words and not percent:
        scaled = _scaled(raw, decimal)
        return None if scaled is None else (-scaled if negative else scaled)
    if plain is None:
        return None
    number = float(plain)
    if percent and percent_to_fraction:
        number = number / 100.0
    return -number if negative else number


def parse_numbers(
    series: pd.Series[Any],
    *,
    decimal: str = ".",
    percent_to_fraction: bool = True,
    indian_scale_words: bool = False,
) -> ParseOutcome:
    """Text to float: currency symbols, thousands separators, `%` and `(negative)` handled.

    `decimal` is `"."` (1,200.50) or `","` (1.200,50). Numbers already numeric pass through, and so
    does a real number inside a text column: it is read by its value, not by its text.
    `indian_scale_words` (Plan O M241) also reads the words of `INDIAN_SCALE_WORDS` and a trailing `/-`;
    a value that reads without them reads exactly as it does with the parameter off.
    """
    if decimal not in {".", ","}:
        raise ValueError("decimal must be '.' or ','")
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return ParseOutcome(values=series.astype("float64"), changed=0, failed=0, failed_examples=())
    positions, cells = _present(series)
    numeric = _numeric_cells(series, positions)
    text = cells[~numeric]
    numbers = _per_value(
        text, lambda value: _number_one(value, decimal, percent_to_fraction, indian_scale_words)
    )
    parsed = np.array([np.nan if number is None else number for number in numbers], dtype="float64")
    failed = np.array([number is None for number in numbers], dtype=bool)
    values = np.full(len(series), np.nan, dtype="float64")
    values[positions[~numeric]] = parsed
    values[positions[numeric]] = series.to_numpy(dtype=object)[positions[numeric]].astype("float64")
    failures = text[failed].head(MAX_EXAMPLES).tolist()
    return ParseOutcome(
        values=pd.Series(values, index=series.index, dtype="float64"),
        changed=int((~failed).sum()),  # text became a number, whatever it looked like
        failed=int(failed.sum()),
        failed_examples=_masked(failures),
    )


def _decimal_vote(text: str) -> str | None:
    """What one value says about the decimal mark: '.', ',', '?' (it reads both ways, differently) or
    None (it says nothing - no mark, or not a number either way)."""
    point = _number_one(text, ".", False)
    comma = _number_one(text, ",", False)
    if point is None and comma is None:
        return None
    if comma is None:
        return "."
    if point is None:
        return ","
    if point == comma:
        return None
    # Only `1,200` and `1.200` read both ways. A comma before exactly three digits is grouping (a
    # decimal comma with three decimals is rare in money); `1.200` is 1.2 or 1200 and proves nothing.
    return "." if "," in text else "?"


def _decimal_style(cells: pd.Series[Any]) -> str | None:
    """The decimal mark the values prove, by majority; None when they cannot tell.

    A value counts only when it reads one way and not the other, so `1.200,50` is never evidence for
    a decimal point. With no evidence either way the mark is `.` unless some value reads differently
    under the two marks (`1.200`) - then, as with an undecided day/month order, nothing is guessed.
    """
    votes = Counter(_per_value(cells, _decimal_vote))
    if votes["."] != votes[","]:
        return "." if votes["."] > votes[","] else ","
    return "." if not votes["."] and not votes["?"] else None


def decimal_evidence(cells: pd.Series[Any]) -> tuple[int, int]:
    """How many distinct values prove a decimal point and how many a decimal comma (Plan O M241's locale profile).

    The votes `_decimal_style` counts: a value counts only when it reads one way and not the other.
    """
    votes = Counter(
        _per_value(pd.Series(list(dict.fromkeys(cells.astype(str))), dtype=object), _decimal_vote)
    )
    return votes["."], votes[","]


def _number_issue(name: str, present: pd.Series[Any]) -> FormatIssue | None:
    """`present` is the column's non-empty cells as they are - text, and real numbers a chunked read
    may have mixed in, which count as converted but are no evidence of a decimal mark."""
    if present.empty:
        return None
    cells = present.astype(str)
    named_id = ingest.is_key_like_name(name) or _PIN_NAME_RE.search(name.strip()) is not None
    if named_id or any(map(ingest.is_leading_zero_cell, cells)):
        # An ID, PIN, phone or postcode is a label: reading it as a number drops its leading zeros.
        return None
    numeric = _numeric_cells(present, np.arange(len(present)))
    decimal = _decimal_style(cells[~numeric])
    if decimal is None:
        return None  # 1.200 could be 1.2 or 1200: the column is left as text rather than misread
    parsed = parse_numbers(present, decimal=decimal)
    convertible = int(parsed.values.notna().sum())
    if convertible / len(cells) < MIN_CONVERT_SHARE:
        return None
    examples = _distinct_examples(cells)
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
        examples=examples,
        failed_examples=parsed.failed_examples,
        params={"decimal": decimal, "percent_to_fraction": True},
        notes=tuple(notes),
        example_cells=(*examples, *parsed.failed_examples),
    )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def _style_of(text: str) -> str | None:
    for style, pattern in _DATE_STYLES:
        if pattern.match(text.strip()):
            return style
    return None


def _order_proofs(cells: pd.Series[Any]) -> tuple[bool, bool]:
    """`(some value can only be day first, some value can only be month first)`."""
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
    return day_first, month_first


def date_order(cells: pd.Series[Any]) -> bool | None:
    """Whether numeric day/month dates put the day first: True, False, or None.

    None means the values do not settle it: every value fits both orders, or the column holds both (`25/03/2024`
    and `03/25/2024`). A caller never reads a None as "month first": it asks (`date_order_conflict` tells the two
    apart)."""
    day_first, month_first = _order_proofs(cells)
    if day_first and not month_first:
        return True
    if month_first and not day_first:
        return False
    return None


def order_unsettled(cells: pd.Series[Any]) -> bool:
    """True when the cells hold numeric day/month dates and their values do not say which comes first: reading
    them needs an answer, never a default."""
    return date_order(cells) is None and bool(
        pd.Series(cells, dtype=object).astype(str).str.match(_NUMERIC_DAY_MONTH).any()
    )


def date_order_conflict(cells: pd.Series[Any]) -> bool:
    """True when some dates can only be day first and others only month first: two systems wrote one column, and
    no single order reads it."""
    day_first, month_first = _order_proofs(cells)
    return day_first and month_first


def _one_datetime(text: str, dayfirst: bool) -> pd.Timestamp | None:
    if not _is_dateable(text):
        return None  # "now" / "today" / "09:30": pandas reads them as the current time or date
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            stamp = pd.to_datetime(text, format="mixed", dayfirst=dayfirst, errors="coerce", utc=True)
        except (ValueError, TypeError, OverflowError):
            return None
    if stamp is None or pd.isna(stamp):
        return None
    return stamp.tz_localize(None)


def _to_datetimes(cells: pd.Series[Any], *, dayfirst: bool) -> pd.Series[Any]:
    """Each value parsed on its own, as naive `datetime64[ns]`.

    A value with a UTC offset is converted to UTC before the offset is dropped, so two values written
    in different zones compare as the instants they are; a value with no offset is kept as written.
    The rule is per value (DEC-1004): it does not depend on what else the column holds. A value with
    no digit is never a date - pandas would read `now` and `today` as the time of the run, a
    different value on every run and later than every real event - and neither is a bare time of
    day (`09:30`, which pandas reads as *today* at that time), so both fail and are counted.

    `pd.to_datetime(format="mixed", utc=True)` does this in one call; should it raise on some
    spelling, the column falls back to one value at a time with the same rule (M77).
    """
    distinct = pd.Series(list(dict.fromkeys(cells.tolist())), dtype=object)
    dated = distinct.map(lambda text: _is_dateable(str(text))).to_numpy(dtype=bool)
    parsed: pd.Series[Any] | None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            parsed = pd.to_datetime(
                distinct.where(dated), format="mixed", dayfirst=dayfirst, errors="coerce", utc=True
            )
        except (ValueError, TypeError, OverflowError):
            parsed = None
    if parsed is None or not pd.api.types.is_datetime64_any_dtype(parsed):
        stamps = [_one_datetime(str(text), dayfirst) for text in distinct]
        parsed = pd.Series([pd.NaT if s is None else s for s in stamps], dtype="datetime64[ns]")
    elif parsed.dt.tz is not None:
        parsed = parsed.dt.tz_localize(None)
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


# ---------------------------------------------------------------------------
# Excel serial dates (Plan O M240, DEC-1810 (d))
# ---------------------------------------------------------------------------
_EXCEL_EPOCH_1900: Final[np.datetime64] = np.datetime64("1899-12-30", "ms")
_EXCEL_EPOCH_1904: Final[np.datetime64] = np.datetime64("1904-01-01", "ms")
EXCEL_FIRST_SERIAL_1900: Final[int] = 61
"""1 March 1900. Excel counts a 29 February 1900 that never existed, so earlier day numbers are one off."""
EXCEL_LAST_SERIAL: Final[int] = 2_958_465
"""31 December 9999, the last date Excel holds."""
_NS_LAST_SERIAL_1900: Final[float] = 109_205.0
"""Day number of 11 April 2262 in the 1900 system: the last day `datetime64[ns]` can hold."""
_NS_LAST_SERIAL_1904: Final[float] = _NS_LAST_SERIAL_1900 - 1462.0
EXCEL_PLAUSIBLE_SERIALS: Final[tuple[int, int]] = (7_306, 73_415)
"""1 January 1920 to 31 December 2100: where a customer file's dates are. A detector reads a column of
numbers as dates only inside it (and only when its name says date), so an amount is never one."""
EXCEL_MIN_CELLS: Final[int] = 5
EXCEL_MIN_SHARE: Final[float] = 0.95

_DATE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "date", "dt", "datetime", "timestamp", "day", "dob", "birthday", "birth", "expiry", "expires",
        "expiration", "joined", "created", "updated", "signup", "renewal", "anniversary", "dated",
    }
)  # fmt: skip
_DATE_SUFFIX_WORDS: Final[frozenset[str]] = frozenset({"on", "at"})
_NOT_DATE_WORDS: Final[frozenset[str]] = frozenset(
    {"candidate", "mandate", "validate", "consolidate", "update", "updates", "invalidate"}
)
_NAME_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _name_words(name: str) -> list[str]:
    """`LastLoginDate` / `last_login_date` / `signup-dt` as lower-case words."""
    return [token.lower() for token in _NAME_TOKEN_RE.findall(str(name))]


def looks_like_date_name(name: str) -> bool:
    """Whether a column's name says it holds dates: a word such as `date`, `created` or `dob`, or a closing `_on` / `_at`."""
    words = _name_words(name)
    if not words:
        return False
    if words[-1] in _DATE_SUFFIX_WORDS and len(words) > 1:
        return True
    for word in words:
        if word in _NOT_DATE_WORDS:
            continue
        if word in _DATE_WORDS or (len(word) > 4 and (word.endswith("date") or word.startswith("date"))):
            return True
    return False


def _serials(cells: pd.Series[Any]) -> pd.Series[Any]:
    """The cells as floats; text that is not a plain finite number is NaN."""
    numbers = pd.to_numeric(cells, errors="coerce")
    return numbers.where(np.isfinite(numbers))


def parse_excel_serial_dates(series: pd.Series[Any], *, date_1904: bool = False) -> ParseOutcome:
    """Excel's day numbers to dates, one value at a time: `44927` is 2023-01-01, `44927.5` is noon that day.

    The result is stateless (DEC-1004): a value's date depends on it and on the calendar alone. A day number
    below 61 (before 1 March 1900, where Excel's own calendar is wrong), one past the dates a table can hold,
    a negative one, and anything that is not a plain number fail and are counted. The time of day is rounded
    to the millisecond. `date_1904` reads the Mac workbook calendar, which starts on 1 January 1904.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return ParseOutcome(values=series, changed=0, failed=0, failed_examples=())
    cells = _text_cells(series)
    distinct = pd.Series(list(dict.fromkeys(cells.tolist())), dtype=object)
    numbers = _serials(distinct).to_numpy(dtype=float)
    low = 0.0 if date_1904 else float(EXCEL_FIRST_SERIAL_1900)
    high = min(float(EXCEL_LAST_SERIAL), _NS_LAST_SERIAL_1904 if date_1904 else _NS_LAST_SERIAL_1900)
    valid = np.isfinite(numbers) & (numbers >= low) & (numbers <= high)
    epoch = _EXCEL_EPOCH_1904 if date_1904 else _EXCEL_EPOCH_1900
    millis = np.zeros(len(numbers), dtype="int64")
    millis[valid] = np.round(numbers[valid] * 86_400_000.0).astype("int64")
    stamps = (epoch + millis.astype("timedelta64[ms]")).astype("datetime64[ns]")
    stamps[~valid] = np.datetime64("NaT")
    lookup = pd.Series(stamps, index=pd.Index(distinct, dtype=object))
    spread = lookup.reindex(pd.Index(cells.tolist(), dtype=object))
    parsed = pd.Series(spread.array, index=cells.index)
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    ok = parsed.notna()
    out.loc[parsed.index[ok]] = parsed[ok]
    failures = cells[~ok].tolist()
    return ParseOutcome(
        values=out, changed=int(ok.sum()), failed=len(failures), failed_examples=_masked(failures)
    )


def find_excel_serial_dates(frame: pd.DataFrame) -> list[FormatIssue]:
    """The columns that hold dates as Excel's day numbers, measured on the whole column.

    A column is reported when its name says date (`looks_like_date_name`), at least `EXCEL_MIN_CELLS` cells
    are filled, and at least 95% of them are numbers between 1 January 1920 and 31 December 2100. Both must
    hold: a column of day numbers with no date in its name, or an amount named `date_fee`, is left alone. The
    fix is a `parse_date` step with `excel_serial` true; the workbook's calendar (1900 or 1904) cannot be told
    from the numbers, so the note says what to check.
    """
    issues: list[FormatIssue] = []
    for name in frame.columns:
        column = frame[name]
        if pd.api.types.is_bool_dtype(column) or pd.api.types.is_datetime64_any_dtype(column):
            continue
        if not looks_like_date_name(str(name)):
            continue
        cells = _text_cells(column)
        if len(cells) < EXCEL_MIN_CELLS:
            continue
        numbers = _serials(cells)
        low, high = EXCEL_PLAUSIBLE_SERIALS
        inside = numbers.between(low, high)
        if inside.sum() / len(cells) < EXCEL_MIN_SHARE:
            continue
        parsed = parse_excel_serial_dates(cells)
        shown = cells[inside]
        examples = _distinct_examples(shown) if len(shown) else ()
        dates = parsed.values.dropna()
        notes = [
            f"{int(inside.sum())} of {len(cells)} values are whole day numbers as Excel stores dates",
            f"read this way they run from {dates.min().date()} to {dates.max().date()}",
            "if they come out four years early, the workbook used the 1904 calendar",
        ]
        issues.append(
            FormatIssue(
                column=str(name),
                kind=FormatIssueKind.EXCEL_SERIAL_DATES,
                non_empty=len(cells),
                convertible=len(cells) - parsed.failed,
                failed=parsed.failed,
                examples=examples,
                failed_examples=parsed.failed_examples,
                params={"excel_serial": True, "date_1904": False},
                notes=tuple(notes),
                example_cells=examples,
            )
        )
    return issues


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
    conflict = date_order_conflict(cells)
    ambiguous_order = order is None and bool(cells.str.match(_NUMERIC_DAY_MONTH).any())
    if len(styles) < 2 and not ambiguous_order and order is None:
        return None
    examples = _distinct_examples(cells)
    notes = [f"{count} values in {style} style" for style, count in styles.most_common()]
    if conflict:
        notes.append("some dates can only be day first and others only month first")
    elif ambiguous_order:
        notes.append("day and month order cannot be told from the values")
    return FormatIssue(
        column=name,
        kind=FormatIssueKind.MIXED_DATES,
        non_empty=len(cells),
        convertible=convertible,
        failed=parsed.failed,
        examples=examples,
        failed_examples=parsed.failed_examples,
        params={"dayfirst": order, "order_conflict": conflict},
        notes=tuple(notes),
        example_cells=(*examples, *parsed.failed_examples),
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

    # A real number (a 0/1 flag read as floats because of a blank) matches a spelling that is a
    # number by value: 1.0 is the spelling "1", not the text "1.0".
    true_numbers = {float(v) for v in truthy if _PLAIN_NUMBER_RE.match(v)}
    false_numbers = {float(v) for v in falsy if _PLAIN_NUMBER_RE.match(v)}

    def one_number(value: Any) -> float:
        number = float(value)
        return 1.0 if number in true_numbers else 0.0 if number in false_numbers else np.nan

    positions, cells = _present(series)
    numeric = _numeric_cells(series, positions)
    parsed = np.empty(len(cells), dtype="float64")
    parsed[~numeric] = np.array(_per_value(cells[~numeric], one), dtype="float64")
    raw = series.to_numpy(dtype=object)[positions[numeric]]
    parsed[numeric] = np.array(_per_value(pd.Series(raw, dtype=object), one_number), dtype="float64")
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
    shown = [(masked_cut(s, 80), count) for s, count in spellings.most_common(MAX_EXAMPLES)]
    return FormatIssue(
        column=name,
        kind=FormatIssueKind.BOOLEAN_AS_TEXT,
        non_empty=len(cells),
        convertible=len(cells),
        failed=0,
        examples=tuple(f"{quoted(s)} ({count})" for s, count in shown),
        failed_examples=(),
        params={"true_values": true_values, "false_values": false_values},
        notes=(f"{len(spellings)} spellings of yes / no",),
        example_cells=tuple(s for s, _ in shown),
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
        if isinstance(out.dtype, pd.CategoricalDtype):
            out = out.astype(object)  # a Parquet dictionary column: a new spelling is not a category yet
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
        if len({s.strip() for s in spellings}) < 2:
            continue
        canonical = max(stripped, key=lambda s: sum(counts[o] for o in spellings if " ".join(o.split()) == s))
        for spelling in spellings:
            # Keyed by the spelling `normalise_texts` looks up - the value stripped, inner spaces as
            # written - so `new  delhi` is merged too, not only its collapsed form.
            if spelling.strip() != canonical:
                merge[spelling.strip()] = canonical
    untrimmed = [s for s in counts if s != s.strip()]
    if not merge and not untrimmed:
        return None
    affected = sum(counts[s] for s in counts if s != s.strip() or s.strip() in merge)
    kind = FormatIssueKind.CATEGORY_VARIANTS if merge else FormatIssueKind.UNTRIMMED_TEXT
    examples = (
        sorted(merge.items())[:MAX_EXAMPLES] if merge else [(s, s.strip()) for s in untrimmed[:MAX_EXAMPLES]]
    )
    pairs = [(masked_cut(a, 80), masked_cut(b, 80)) for a, b in examples[:MAX_EXAMPLES]]
    return FormatIssue(
        column=name,
        kind=kind,
        non_empty=len(cells),
        convertible=affected,
        failed=0,
        examples=tuple(f"{quoted(a)} → {quoted(b)}" for a, b in pairs),
        failed_examples=(),
        params={"strip": True, "merge": dict(sorted(merge.items()))},
        notes=(f"{len(merge)} spellings merge into others",) if merge else ("values have extra spaces",),
        example_cells=tuple(dict.fromkeys(piece for pair in pairs for piece in pair)),
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
        positions, cells = _present(series)
        if cells.empty:
            continue
        present = series.iloc[positions]  # the numbers detector reads real numbers by value
        for detector in (_number_issue, _date_issue, _boolean_issue, _category_issue):
            issue = detector(str(name), present if detector is _number_issue else cells)
            if issue is not None:
                issues.append(issue)
                break
    return tuple(issues)
