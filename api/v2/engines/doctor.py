"""P1, the Data Doctor: the public API for "is this file fit to run marketing on?".

Two endpoints, both stateless (the file is read, answered on and forgotten):

* `POST /doctor/profile` reads a CSV (multipart field `file`, or a raw `text/csv` body) and says how
  ready it is: a readiness score, which columns hold personal data and what to do with each, whether
  the primary key is unique, whether dates parse and none lies after the snapshot, and which columns
  might leak the outcome. The profiler is v2's own and light (pandas and regexes). When the v1
  engine is importable it also runs v1's Indian identifier checks (GSTIN, voter ID, PIN, mobile) and
  folds in what they find; that part is optional and guarded.
* `POST /doctor/map-events` applies an `EventMapping` (multipart field `mapping`, JSON) to a CSV and
  returns the events and the rows that could not become one, via `autonomy.data.mapping`.

**Uploads are capped** at `MAX_UPLOAD_BYTES`; a bigger body is refused with 413 before it is read in
full. Multipart bodies are parsed with the standard library, so the API needs no extra package.

**Personal data detection** reads up to `PII_SAMPLE` non-empty cells of each column and flags a
type when at least `PII_MIN_SHARE` of them are whole matches, or when the header names it:

* email; phone: E.164 (`+` and 8-15 digits), Indian mobiles (ten digits from 6-9, with or without
  `+91`, `91`, `0091` or `0`) and North American `(555) 123-4567` shapes;
* card numbers: 13-19 digits that pass the Luhn check; Aadhaar: 12 digits, the first 2-9, that pass
  the Verhoeff check; PAN: `AAAAA9999A` with a holder-type letter in the fourth place;
* by header only: personal names, street addresses and dates of birth.

Nothing returned ever carries a cell's value: findings are column names, types and counts.

**Readiness score**: 100, less 20 for each error and 7 for each warning in `problems`, never below
0. Errors are what make a file unusable as it is (a duplicated key, unreadable dates, a likely
leak); warnings are what a person should look at.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zlib
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any, Final, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from autonomy.contracts import Event
from autonomy.data.mapping import (
    DEFAULT_ID_PLACEHOLDERS,
    EventMapping,
    MappingProblem,
    MappingQuestionsError,
    inspect_frame,
    map_frame,
    problem_counts,
)

__all__ = [
    "MAX_UPLOAD_BYTES",
    "MapEventsResponse",
    "PiiFinding",
    "Problem",
    "ProfileReport",
    "is_luhn_valid",
    "is_verhoeff_valid",
    "profile_frame",
    "read_csv_bytes",
    "router",
]

router = APIRouter(prefix="/doctor", tags=["engines: P1 data doctor"])

MAX_UPLOAD_BYTES: Final[int] = 20 * 1024 * 1024
PII_SAMPLE: Final[int] = 1000
PII_MIN_SHARE: Final[float] = 0.6
DATE_MIN_SHARE: Final[float] = 0.8
"""Share of cells that must look like dates for a column without a date-like header to be checked."""
LEAK_CORRELATION: Final[float] = 0.95
LEAK_ACCURACY: Final[float] = 0.98
MIN_ROWS: Final[int] = 100

_NA_VALUES: Final[list[str]] = ["", "NA", "N/A", "n/a", "null", "NULL", "None", "nan", "NaN", "#N/A"]

Severity = Literal["error", "warning", "info"]
_PENALTY: Final[dict[str, int]] = {"error": 20, "warning": 7, "info": 0}


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Problem(_Out):
    code: str
    severity: Severity
    column: str | None = None
    message: str


class PiiFinding(_Out):
    column: str
    type: str
    action: Literal["hash", "mask", "drop"]
    detected_by: Literal["value", "header", "v1"]
    match_share: float | None = Field(default=None, description="Share of sampled cells that matched")


class LeakSuspect(_Out):
    column: str
    reason: str
    score: float


class LeakCheck(_Out):
    checked: bool
    target: str | None
    suspects: tuple[LeakSuspect, ...] = ()
    note: str


class KeyCheck(_Out):
    column: str | None
    guessed: bool
    valid: bool | None
    duplicate_keys: int = Field(default=0, description="Distinct key values that appear more than once")
    duplicate_rows: int = Field(default=0, description="Rows beyond the first for a repeated key")
    missing: int = 0


class DateColumn(_Out):
    column: str
    non_empty: int
    unparseable: int
    unparseable_share: float
    future_dated: int
    earliest: datetime | None
    latest: datetime | None
    ambiguous_day_month: int = Field(
        default=0, description="Values such as 03/04 that read as two different days (d/m or m/d)"
    )
    day_first_evidence: int = Field(default=0, description="Values that read only day first (13/04)")
    month_first_evidence: int = Field(default=0, description="Values that read only month first (04/13)")
    placeholder_dates: int = Field(default=0, description="1900-01-01, 1970-01-01 and the like at midnight")
    two_digit_years: int = Field(default=0, description="Values written with a two-digit year")


class TemporalIntegrity(_Out):
    ok: bool
    snapshot: datetime | None
    date_columns: tuple[DateColumn, ...]


ColumnKind = Literal["empty", "number", "date", "boolean", "text"]


class ColumnProfile(_Out):
    name: str
    kind: ColumnKind
    missing_share: float
    distinct: int


class ProfileReport(_Out):
    readiness_score: int
    rows: int
    columns: int
    column_profiles: tuple[ColumnProfile, ...]
    pii_detected: tuple[PiiFinding, ...]
    leak_check: LeakCheck
    primary_key_valid: bool | None
    primary_key: KeyCheck
    temporal_integrity: TemporalIntegrity
    problems: tuple[Problem, ...]
    v1_enrichment: dict[str, Any] | None = Field(
        default=None, description="What v1's checks added, when the v1 engine is importable"
    )


class MapEventsResponse(_Out):
    rows: int
    mapped: int
    events: tuple[Event, ...]
    problems: tuple[MappingProblem, ...]
    problem_counts: dict[str, int]
    inferred: tuple[str, ...] = Field(
        default=(), description="What the file itself settled (a decimal comma, the date order), in words"
    )


# --- checksums and patterns -----------------------------------------------------------------------


def is_luhn_valid(digits: str) -> bool:
    """The Luhn (mod 10) check every payment card number passes."""
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


_VERHOEFF_D: Final[tuple[tuple[int, ...], ...]] = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P: Final[tuple[tuple[int, ...], ...]] = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def is_verhoeff_valid(digits: str) -> bool:
    """The Verhoeff check the last digit of every Aadhaar number satisfies."""
    if not digits.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


_EMAIL: Final = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_E164: Final = re.compile(r"\+[1-9]\d{7,14}")
_INDIAN_MOBILE: Final = re.compile(r"(?:\+?91|0091|0)?[6-9]\d{9}")
_NANP: Final = re.compile(r"(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}")
_PAN: Final = re.compile(r"[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z]")
_SEPARATORS: Final = re.compile(r"[\s().-]")
_CARD_SHAPE: Final = re.compile(r"\d{4}([ -]?\d{2,4}){2,4}")
_AADHAAR_SHAPE: Final = re.compile(r"[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}")


def _is_phone(text: str) -> bool:
    if _NANP.fullmatch(text):
        return True
    compact = _SEPARATORS.sub("", text)
    return bool(_E164.fullmatch(compact) or _INDIAN_MOBILE.fullmatch(compact))


def _is_card(text: str) -> bool:
    if not _CARD_SHAPE.fullmatch(text):
        return False
    digits = re.sub(r"[ -]", "", text)
    return 13 <= len(digits) <= 19 and is_luhn_valid(digits)


def _is_aadhaar(text: str) -> bool:
    return bool(_AADHAAR_SHAPE.fullmatch(text)) and is_verhoeff_valid(re.sub(r"[ -]", "", text))


# Value detectors in the order they are tried: the most specific shape first.
_VALUE_DETECTORS: Final[tuple[tuple[str, Callable[[str], bool]], ...]] = (
    ("card_number", _is_card),
    ("aadhaar", _is_aadhaar),
    ("pan", lambda t: bool(_PAN.fullmatch(t.upper()))),
    ("email", lambda t: bool(_EMAIL.fullmatch(t))),
    ("phone", _is_phone),
)

_HEADER_DETECTORS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("email", re.compile(r"e[-_ ]?mail")),
    ("phone", re.compile(r"phone|mobile|msisdn|whats_?app|cell(_?no)?$|contact_?(no|number)")),
    ("card_number", re.compile(r"card_?(no|num|number)|credit_?card|cc_?num")),
    ("aadhaar", re.compile(r"aadh?aar|uidai")),
    ("pan", re.compile(r"(^|_)pan(_?(no|num|number|card))?$")),
    ("person_name", re.compile(r"^(first|last|full|given|family|sur|middle|customer|contact)?_?name$")),
    ("address", re.compile(r"(^|_)(street|address|addr)(_|$|line)")),
    ("date_of_birth", re.compile(r"(^|_)(dob|date_?of_?birth|birth_?date|birthday)($|_)")),
)
_DOB_HEADER: Final = _HEADER_DETECTORS[-1][1]

_ACTION: Final[dict[str, Literal["hash", "mask", "drop"]]] = {
    "email": "hash",
    "phone": "hash",
    "card_number": "drop",
    "aadhaar": "drop",
    "pan": "drop",
    "person_name": "drop",
    "address": "drop",
    "date_of_birth": "drop",
    "gstin": "mask",
    "epic": "drop",
}
"""Email and phone are how we reach a customer, so they are pseudonymised (kept only in the vault
for sending); identity numbers and anything else personal play no part in deciding and are dropped."""

_DATE_HEADER: Final = re.compile(r"date|time|(^|_)(ts|dt|at|on)$|timestamp|created|updated")
_DATE_SHAPE: Final = re.compile(
    r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}([ T]\d{1,2}:\d{2}.*)?|\d{1,2} [A-Za-z]{3,9} \d{2,4}.*|"
    r"[A-Za-z]{3,9} \d{1,2},? \d{2,4}.*"
)
_KEY_HEADER: Final = re.compile(r"(^|_)id$|^id_|_key$|^key$|(^|_)(uuid|guid)$")
_BOOLEAN: Final = frozenset({"true", "false", "yes", "no", "y", "n", "0", "1", "t", "f"})


# --- messy-file checks: ids, separators, currencies, totals, sentinels -------------------------------

_ID_HEADER: Final = re.compile(
    r"(^|_)(customer|client|user|member|account|buyer|shopper|person|contact|cust)(_?(id|no|num|number|key|code))?$"
)
_PLACEHOLDERS: Final = frozenset(DEFAULT_ID_PLACEHOLDERS)
_SCIENTIFIC: Final = re.compile(r"^[+-]?\d(\.\d+)?[eE][+-]\d{1,3}$")
_TOTAL_WORDS: Final = frozenset({"total", "totals", "grand total", "subtotal", "sum", "sum total"})
_THREE_AFTER: Final = re.compile(r"^[+-]?[1-9]\d{0,2}[.,]\d{3}$")
_POINT_NUMBER: Final = re.compile(r"^[+-]?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?$")
_COMMA_NUMBER: Final = re.compile(r"^[+-]?(\d+|\d{1,3}(\.\d{3})+)(,\d+)?$")
_MONEY_MARK: Final = re.compile(r"^[£$€¥₹R]\s?[-+]?\d|\d\s?[£$€¥₹]$|\d\s?[A-Z]{3}$|^[A-Z]{3}\s?\d")
_NUMBERISH: Final = re.compile(r"^([£$€¥₹]|[A-Z]{3})?\s?[+-]?\d[\d.,\s']*\s?([£$€¥₹]|[A-Z]{3})?$")
_CURRENCY_HEADER: Final = re.compile(r"(^|_)(currency|curr|ccy|currency_code|iso_currency)($|_)")
SENTINEL_MIN_SHARE: Final[float] = 0.01
SENTINEL_RATIO: Final[float] = 10.0
HEAVY_SHARE: Final[float] = 0.2


def _id_problems(column: str, cells: Sequence[str]) -> list[Problem]:
    """Ids that would silently merge, split or vanish people: placeholders, lost digits, zeros, case."""
    out: list[Problem] = []
    n = len(cells)
    if not n:
        return out
    placeholder = sum(1 for c in cells if c.lower() in _PLACEHOLDERS or (c.isdigit() and not c.strip("0")))
    if placeholder:
        out.append(
            Problem(
                code="placeholder_ids",
                severity="warning",
                column=column,
                message=(
                    f"{placeholder:,} rows have a placeholder id (such as 0, unknown, guest or TOTAL) "
                    "instead of a customer; they are left out when mapping, never merged into one customer"
                ),
            )
        )
    scientific = sum(1 for c in cells if _SCIENTIFIC.match(c))
    if scientific:
        out.append(
            Problem(
                code="scientific_ids",
                severity="error",
                column=column,
                message=(
                    f"{scientific:,} ids are numbers in scientific notation (1.23457E+11), as a spreadsheet "
                    "saves long ids: their last digits are lost and many people share one id. Export the "
                    "column as text"
                ),
            )
        )
    zeros = sum(1 for c in cells if c.isdigit() and c.startswith("0") and c.strip("0"))
    if zeros:
        out.append(
            Problem(
                code="leading_zero_ids",
                severity="warning",
                column=column,
                message=(
                    f"{zeros:,} ids start with 0. A spreadsheet or pandas drops those zeros, so another "
                    "file may hold the same customer as a different id; say whether zeros matter "
                    "(id_leading_zeros) when mapping"
                ),
            )
        )
    emails = [c for c in cells if "@" in c]
    if emails and len(set(emails)) > len({e.lower() for e in emails}):
        out.append(
            Problem(
                code="mixed_case_ids",
                severity="warning",
                column=column,
                message=(
                    f"{len(set(emails)) - len({e.lower() for e in emails}):,} e-mail ids also appear in "
                    "another letter case; they are read as one customer (e-mails are lower-cased)"
                ),
            )
        )
    counts = Counter(c for c in cells if c.lower() not in _PLACEHOLDERS)
    top = counts.most_common(2)
    if top and n >= 50:
        biggest, nxt = top[0][1], (top[1][1] if len(top) > 1 else 0)
        if biggest >= HEAVY_SHARE * n and biggest >= 5 * max(nxt, 1):
            out.append(
                Problem(
                    code="heavy_id",
                    severity="warning",
                    column=column,
                    message=(
                        f"one id is on {biggest:,} of {n:,} rows ({biggest / n:.0%}; the next is on "
                        f"{nxt:,}): a stand-in for guests or unknown buyers would merge many people"
                    ),
                )
            )
    return out


def _number_problems(column: str, cells: Sequence[str]) -> list[Problem]:
    """Numbers written in a convention that a plain reading gets wrong."""
    out: list[Problem] = []
    point = comma = ambiguous = money = 0
    for c in cells:
        if _MONEY_MARK.search(c):
            money += 1
        if "," not in c and "." not in c:
            continue
        if _THREE_AFTER.match(c):
            ambiguous += 1
        elif _POINT_NUMBER.match(c) and not _COMMA_NUMBER.match(c):
            point += 1
        elif _COMMA_NUMBER.match(c) and not _POINT_NUMBER.match(c):
            comma += 1
    if ambiguous and not (point and not comma) and not (comma and not point):
        out.append(
            Problem(
                code="ambiguous_separators",
                severity="warning",
                column=column,
                message=(
                    f"{ambiguous:,} numbers look like 2.550 or 1,234 (one separator, three digits) and "
                    "nothing else in the column says whether it is a decimal point or a thousands "
                    "separator; say which (decimal) when mapping"
                ),
            )
        )
    if comma and not point:
        out.append(
            Problem(
                code="decimal_comma",
                severity="info",
                column=column,
                message=f"{comma:,} numbers use a decimal comma (2,55); they are read that way",
            )
        )
    if money:
        out.append(
            Problem(
                code="currency_in_amounts",
                severity="warning",
                column=column,
                message=(
                    f"{money:,} amounts carry a currency sign or code (£2.55, 2.55 GBP); put the "
                    "currency in its own column so amounts in different currencies are never added"
                ),
            )
        )
    return out


def _currency_problems(column: str, cells: Sequence[str]) -> list[Problem]:
    codes = Counter(c.strip().upper() for c in cells if re.fullmatch(r"[A-Za-z]{3}", c.strip()))
    if len(codes) < 2:
        return []
    listed = ", ".join(f"{code} ({n:,} rows)" for code, n in codes.most_common(5))
    return [
        Problem(
            code="mixed_currency",
            severity="warning",
            column=column,
            message=(
                f"amounts in {len(codes)} currencies: {listed}. They are never added together; give "
                "the business currency and exchange rates when mapping (currency, currency_rates)"
            ),
        )
    ]


def _totals_problems(frame: pd.DataFrame, text_columns: Sequence[str]) -> list[Problem]:
    """A totals line at the end of an export: a 'Total' label, or a number that sums all the others."""
    out: list[Problem] = []
    rows = len(frame)
    if rows < 3:
        return out
    for column in text_columns:
        labels = frame[column].astype("string").str.strip().str.lower()
        hits = int(labels.isin(_TOTAL_WORDS).sum())
        if hits:
            out.append(
                Problem(
                    code="totals_row",
                    severity="warning",
                    column=column,
                    message=(
                        f"{hits:,} rows are labelled as a total (Total, Sum, Subtotal); a totals line "
                        "counted as a row doubles the money. Remove it before loading"
                    ),
                )
            )
            return out
    for raw in frame.columns:
        values = _numeric(frame[raw])
        known = values.dropna()
        if len(known) < 3:
            continue
        last = float(known.iloc[-1])
        rest = float(known.iloc[:-1].sum())
        if last != 0 and abs(last - rest) <= 0.005 * abs(last) and abs(last) > known.iloc[:-1].abs().max():
            out.append(
                Problem(
                    code="totals_row",
                    severity="warning",
                    column=str(raw),
                    message=(
                        "the last row's value equals the sum of all the others: a totals line counted "
                        "as a row doubles the money. Remove it before loading"
                    ),
                )
            )
            return out
    return out


def _sentinel_problems(column: str, series: pd.Series[Any]) -> list[Problem]:
    """A number standing for 'unknown' or 'never' (999, -1, 118): far from every other value, and common."""
    values = _numeric(series).dropna()
    n = len(values)
    if n < 50 or len(values) < len(_cells(series)):
        return []
    counts = values.value_counts()
    if len(counts) < 3:
        return []
    for v, top in ((float(values.max()), True), (float(values.min()), False)):
        if v == 0:
            continue
        others = values[values != v]
        nxt = float(others.max()) if top else float(others.min())
        spread = float(others.max() - others.min()) or 1.0
        rows = int(counts[v])
        far = abs(v - nxt) >= 0.1 * spread and abs(v - nxt) >= 1
        common = rows >= max(5, SENTINEL_MIN_SHARE * n, SENTINEL_RATIO * int(counts.get(nxt, 1)))
        if far and common:
            shown = int(v) if v.is_integer() else v
            return [
                Problem(
                    code="sentinel_value",
                    severity="warning",
                    column=column,
                    message=(
                        f"{shown} is on {rows:,} rows ({rows / n:.0%}) and far from every other value "
                        f"({_fmt(float(others.min()))} to {_fmt(float(others.max()))}): it is probably a "
                        "code for 'unknown' or 'never', not a number; treat it as missing"
                    ),
                )
            ]
    return []


def _fmt(x: float) -> str:
    return str(int(x)) if x.is_integer() else f"{x:g}"


# --- the profiler ---------------------------------------------------------------------------------


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _cells(series: pd.Series[Any]) -> list[str]:
    return [str(v).strip() for v in series.dropna().tolist() if str(v).strip()]


def _pii(column: str, cells: Sequence[str]) -> PiiFinding | None:
    """The one personal-data type a column holds, by its values first, else by its header."""
    sample = list(cells[:PII_SAMPLE])
    if sample:
        best: tuple[float, str] | None = None
        for kind, test in _VALUE_DETECTORS:
            share = sum(1 for t in sample if test(t)) / len(sample)
            if share >= PII_MIN_SHARE and (best is None or share > best[0]):
                best = (share, kind)
        if best is not None:
            return PiiFinding(
                column=column,
                type=best[1],
                action=_ACTION[best[1]],
                detected_by="value",
                match_share=round(best[0], 4),
            )
    name = _norm(column)
    for kind, pattern in _HEADER_DETECTORS:
        if pattern.search(name):
            return PiiFinding(column=column, type=kind, action=_ACTION[kind], detected_by="header")
    return None


def _dob_by_header(column: str) -> PiiFinding | None:
    """A date column is personal data only when its header says it is a date of birth."""
    if _DOB_HEADER.search(_norm(column)):
        return PiiFinding(
            column=column, type="date_of_birth", action=_ACTION["date_of_birth"], detected_by="header"
        )
    return None


def _numeric(series: pd.Series[Any]) -> pd.Series[float]:
    text = series.astype("string").str.replace(",", "", regex=False).str.strip()
    out: pd.Series[float] = pd.to_numeric(text, errors="coerce")
    return out


def _parse_dates(series: pd.Series[Any]) -> pd.Series[Any]:
    """Dates as UTC timestamps (naive ones read as UTC), NaT where unreadable.

    ISO 8601 is tried first because it is fast; only the cells it can't read go through the slower
    per-cell "mixed" parser.
    """
    parsed: pd.Series[Any] = pd.to_datetime(series, errors="coerce", utc=True, format="ISO8601")
    missing = parsed.isna()
    if missing.any():
        parsed[missing] = pd.to_datetime(series[missing], errors="coerce", utc=True, format="mixed")
    return parsed


def _kind(series: pd.Series[Any], cells: Sequence[str], is_date: bool) -> ColumnKind:
    if not cells:
        return "empty"
    if is_date:
        return "date"
    if {c.lower() for c in cells} <= _BOOLEAN and len({c.lower() for c in cells}) <= 2:
        return "boolean"
    if _numeric(series).notna().sum() == len(cells):
        return "number"
    return "text"


def _looks_like_dates(column: str, cells: Sequence[str]) -> bool:
    if not cells:
        return False
    if _DATE_HEADER.search(_norm(column)):
        sample = cells[:PII_SAMPLE]
        # A date-ish header over plain numbers ("days_since") is not a date column.
        return sum(1 for c in sample if _DATE_SHAPE.fullmatch(c)) / len(sample) >= 0.5
    sample = cells[:PII_SAMPLE]
    return sum(1 for c in sample if _DATE_SHAPE.fullmatch(c)) / len(sample) >= DATE_MIN_SHARE


_SLASH_DATE: Final = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})(?:[ T].*)?$")
_PLACEHOLDER_DAYS: Final = frozenset({(1900, 1, 1), (1970, 1, 1), (1899, 12, 30), (1899, 12, 31)})


def _day_month_counts(cells: Sequence[str]) -> tuple[int, int, int, int]:
    """(ambiguous, day-first only, month-first only, two-digit years) over d/m/y-shaped cells."""
    ambiguous = day_first = month_first = two_digit = 0
    for text in cells:
        match = _SLASH_DATE.match(text)
        if match is None:
            continue
        a, b, year = int(match.group(1)), int(match.group(2)), match.group(3)
        two_digit += len(year) == 2
        if a > 12 and 1 <= b <= 12:
            day_first += 1
        elif b > 12 and 1 <= a <= 12:
            month_first += 1
        elif 1 <= a <= 12 and 1 <= b <= 12 and a != b:
            ambiguous += 1
    return ambiguous, day_first, month_first, two_digit


def _date_check(column: str, series: pd.Series[Any], snapshot: datetime | None) -> DateColumn:
    present = series.dropna()
    present = present[present.astype(str).str.strip() != ""]
    parsed = _parse_dates(present)
    ok = parsed.dropna()
    bad = int(parsed.isna().sum())
    future = 0
    if snapshot is not None and len(ok):
        future = int((ok > pd.Timestamp(snapshot)).sum())
    placeholder = 0
    if len(ok):
        midnight = (ok.dt.hour == 0) & (ok.dt.minute == 0) & (ok.dt.second == 0)
        days = list(zip(ok.dt.year, ok.dt.month, ok.dt.day, strict=True))
        placeholder = int(sum(1 for d, m in zip(days, midnight, strict=True) if m and d in _PLACEHOLDER_DAYS))
    ambiguous, day_first, month_first, two_digit = _day_month_counts(_cells(present))
    return DateColumn(
        column=column,
        non_empty=len(present),
        unparseable=bad,
        unparseable_share=round(bad / len(present), 4) if len(present) else 0.0,
        future_dated=future,
        earliest=ok.min().to_pydatetime() if len(ok) else None,
        latest=ok.max().to_pydatetime() if len(ok) else None,
        ambiguous_day_month=ambiguous,
        day_first_evidence=day_first,
        month_first_evidence=month_first,
        placeholder_dates=placeholder,
        two_digit_years=two_digit,
    )


def _date_problems(d: DateColumn) -> list[Problem]:
    """Plain-words problems for one date column's day/month order, placeholders and short years."""
    out: list[Problem] = []
    if d.ambiguous_day_month:
        if d.day_first_evidence and not d.month_first_evidence:
            settled = f"{d.day_first_evidence:,} others can only be day first (01/12 = 1 December)"
        elif d.month_first_evidence and not d.day_first_evidence:
            settled = f"{d.month_first_evidence:,} others can only be month first (01/12 = 12 January)"
        elif d.day_first_evidence and d.month_first_evidence:
            settled = (
                f"the file mixes both orders ({d.day_first_evidence:,} only day first, "
                f"{d.month_first_evidence:,} only month first)"
            )
        else:
            settled = "nothing in the column settles which"
        out.append(
            Problem(
                code="ambiguous_day_month",
                severity="warning",
                column=d.column,
                message=(
                    f"{d.ambiguous_day_month:,} dates such as 03/04 read as two different days, day "
                    f"first or month first; {settled}. Say which (date_order) when mapping"
                ),
            )
        )
    if d.placeholder_dates:
        out.append(
            Problem(
                code="placeholder_dates",
                severity="warning",
                column=d.column,
                message=(
                    f"{d.placeholder_dates:,} values are placeholder dates (1900-01-01, 1970-01-01 at "
                    "midnight), written when the real date was missing; they are not real times"
                ),
            )
        )
    if d.two_digit_years:
        out.append(
            Problem(
                code="two_digit_years",
                severity="warning",
                column=d.column,
                message=(
                    f"{d.two_digit_years:,} dates have a two-digit year, which can be read in the wrong "
                    "century (68 as 2068); export four-digit years or check the century"
                ),
            )
        )
    return out


def _key_check(frame: pd.DataFrame, primary_key: str | None) -> KeyCheck:
    guessed = False
    column = primary_key
    if column is None:
        candidates = [str(c) for c in frame.columns if _KEY_HEADER.search(_norm(str(c)))]
        if candidates:
            # The most nearly unique id-like column; ties go to the earlier column.
            column = max(candidates, key=lambda c: (frame[c].nunique(dropna=True), -candidates.index(c)))
            guessed = True
    if column is None:
        return KeyCheck(column=None, guessed=False, valid=None)
    values = frame[column].astype("string").str.strip()
    missing = int((values.isna() | (values == "")).sum())
    present = values[values.notna() & (values != "")]
    counts = present.value_counts()
    repeated = counts[counts > 1]
    return KeyCheck(
        column=column,
        guessed=guessed,
        valid=missing == 0 and repeated.empty,
        duplicate_keys=len(repeated),
        duplicate_rows=int((repeated - 1).sum()),
        missing=missing,
    )


_TIMING_HINT: Final = re.compile(
    r"(^|_)(duration|call_?length|call_?time|talk_?time|time_?on_?call|status|final_?status)($|_)"
)
"""Names of things measured during or after the contact (how long the call lasted, how it ended)."""
_LEAK_HINT: Final = re.compile(
    r"(^|_)(churn(ed)?|converted|conversion|outcome|label|target|refund(ed)?|cancel(l?ed)?|result|y)($|_)"
)


def _binary(series: pd.Series[Any]) -> pd.Series[float]:
    lowered = series.astype("string").str.strip().str.lower()
    mapped = lowered.map(
        {"1": 1.0, "0": 0.0, "true": 1.0, "false": 0.0, "yes": 1.0, "no": 0.0, "y": 1.0, "n": 0.0}
    )
    numeric = _numeric(series)
    out: pd.Series[float] = mapped.astype("Float64").fillna(numeric).astype("float64")
    return out


def _leak_check(
    frame: pd.DataFrame,
    target: str | None,
    date_columns: Sequence[str],
    known_after: Sequence[str] = (),
) -> LeakCheck:
    """Columns that may already contain the outcome: near-perfect correlation or purity with it."""
    if target is None:
        told = tuple(
            LeakSuspect(column=c, reason="you said it is recorded after the outcome", score=1.0)
            for c in known_after
        )
        return LeakCheck(
            checked=False, target=None, suspects=told, note="No target column given, so no leak check ran."
        )
    y = _binary(frame[target])
    known = y.notna()
    if known.sum() < 10 or y[known].nunique() < 2:
        return LeakCheck(
            checked=False,
            target=target,
            note="The target has fewer than ten readable values or only one value; no leak check ran.",
        )
    suspects: list[LeakSuspect] = []
    base = float(y[known].value_counts(normalize=True).max())
    for raw in frame.columns:
        column = str(raw)
        if column == target or column in date_columns:
            continue
        if column in known_after:
            suspects.append(
                LeakSuspect(column=column, reason="you said it is recorded after the outcome", score=1.0)
            )
            continue
        x = frame[column]
        numeric = _numeric(x)
        both = known & numeric.notna()
        if both.sum() >= 10 and numeric[both].nunique() > 1:
            with np.errstate(invalid="ignore", divide="ignore"):  # a constant side gives NaN, not a warning
                corr = float(np.corrcoef(numeric[both].to_numpy(), y[both].to_numpy())[0, 1])
            if np.isfinite(corr) and abs(corr) >= LEAK_CORRELATION:
                suspects.append(
                    LeakSuspect(
                        column=column,
                        reason="moves almost exactly with the target",
                        score=round(abs(corr), 4),
                    )
                )
                continue
        text = x.astype("string").str.strip()
        rows = known & text.notna()
        n_values = text[rows].nunique()
        if rows.sum() >= 10 and 1 < n_values <= max(2, int(rows.sum()) // 5):
            # Share of rows whose target is the most common one for their value: 1.0 means the
            # column's values alone tell the target apart.
            pairs = Counter(zip(text[rows].tolist(), y[rows].tolist(), strict=True))
            best: dict[str, int] = {}
            for (value, _), count in pairs.items():
                best[value] = max(best.get(value, 0), count)
            accuracy = sum(best.values()) / int(rows.sum())
            if accuracy >= LEAK_ACCURACY and base < 0.9:
                suspects.append(
                    LeakSuspect(
                        column=column,
                        reason="its values alone tell the target apart",
                        score=round(accuracy, 4),
                    )
                )
                continue
        if _LEAK_HINT.search(_norm(column)):
            suspects.append(
                LeakSuspect(column=column, reason="its name suggests it records the outcome", score=0.0)
            )
        elif _TIMING_HINT.search(_norm(column)):
            suspects.append(
                LeakSuspect(
                    column=column,
                    reason=(
                        "its name suggests it is measured during or after the contact (how long it "
                        "lasted, how it ended); use it only if it is known before you decide"
                    ),
                    score=0.0,
                )
            )
    return LeakCheck(
        checked=True,
        target=target,
        suspects=tuple(suspects),
        note=(
            "A suspect may have been recorded after the outcome was known; leave it out of anything that "
            "predicts the target unless it is known before the decision."
        ),
    )


def profile_frame(
    frame: pd.DataFrame,
    *,
    snapshot: datetime | None = None,
    primary_key: str | None = None,
    target: str | None = None,
    date_columns: Sequence[str] | None = None,
    use_v1: bool = True,
    known_after: Sequence[str] = (),
) -> ProfileReport:
    """Profile a table read as text (`read_csv_bytes`); see the module docstring for the rules.

    `snapshot` is the moment the data is meant to describe: a date after it is "future-dated". It
    is passed in, never read from the clock. `known_after` names columns recorded after the outcome
    (a call's duration, a final status): each is a leak suspect. Unknown column names raise ValueError.
    """
    for name, given in (("primary_key", primary_key), ("target", target)):
        if given is not None and given not in frame.columns:
            raise ValueError(f"{name} {given!r} is not a column of the file")
    for given in date_columns or ():
        if given not in frame.columns:
            raise ValueError(f"date column {given!r} is not a column of the file")
    for given in known_after:
        if given not in frame.columns:
            raise ValueError(f"known_after column {given!r} is not a column of the file")

    problems: list[Problem] = []
    rows = len(frame)
    profiles: list[ColumnProfile] = []
    pii: list[PiiFinding] = []
    dates: list[str] = list(date_columns) if date_columns is not None else []
    for raw in frame.columns:
        column = str(raw)
        series = frame[raw]
        cells = _cells(series)
        if date_columns is None and _looks_like_dates(column, cells):
            dates.append(column)
        kind = _kind(series, cells, column in dates)
        profiles.append(
            ColumnProfile(
                name=column,
                kind=kind,
                missing_share=round(1 - len(cells) / rows, 4) if rows else 1.0,
                distinct=len(set(cells)),
            )
        )
        if kind == "empty":
            problems.append(
                Problem(code="empty_column", severity="warning", column=column, message="no values")
            )
            continue
        if len(set(cells)) == 1 and rows > 1:
            problems.append(
                Problem(
                    code="constant_column", severity="info", column=column, message="one value throughout"
                )
            )
        # A date column's values are dates, not identifiers, so only its header is read: a date of
        # birth is personal data even though every cell parses as a date.
        finding = _pii(column, cells) if column not in dates else _dob_by_header(column)
        if finding is not None:
            pii.append(finding)

    v1 = _v1_enrichment(frame, pii) if use_v1 else None
    if v1 is not None:
        pii.extend(v1.pop("_findings"))
    for f in pii:
        problems.append(
            Problem(
                code="personal_data",
                severity="info",
                column=f.column,
                message=f"holds {f.type.replace('_', ' ')}; {f.action} it before use",
            )
        )

    if frame.attrs.get("decoded_as") == "latin-1":
        problems.append(
            Problem(
                code="not_utf8",
                severity="warning",
                message=(
                    "the file is not UTF-8, so it was read as Latin-1; letters outside Western European "
                    "alphabets (Cyrillic, Greek, ...) come out garbled. Save it as UTF-8"
                ),
            )
        )
    if rows < MIN_ROWS:
        problems.append(
            Problem(code="few_rows", severity="warning", message=f"{rows:,} rows; at least {MIN_ROWS:,} help")
        )
    repeats = int(frame.duplicated().sum()) if rows else 0
    if repeats:
        problems.append(
            Problem(
                code="duplicate_rows", severity="warning", message=f"{repeats:,} rows repeat another exactly"
            )
        )

    key = _key_check(frame, primary_key)
    if key.column is None:
        problems.append(Problem(code="no_key", severity="warning", message="no id column was found or given"))
    elif not key.valid:
        parts = []
        if key.duplicate_keys:
            parts.append(f"{key.duplicate_keys:,} values repeat ({key.duplicate_rows:,} extra rows)")
        if key.missing:
            parts.append(f"{key.missing:,} rows have no key")
        problems.append(
            Problem(
                code="duplicate_key" if key.duplicate_keys else "missing_key",
                severity="error" if not key.guessed else "warning",
                column=key.column,
                message="; ".join(parts),
            )
        )

    id_columns = [c for c in dict.fromkeys([key.column] if key.column else []) if c not in dates]
    id_columns += [
        str(c) for c in frame.columns if _ID_HEADER.search(_norm(str(c))) and str(c) not in id_columns
    ]
    id_columns = [c for c in id_columns if c not in dates]
    for column in id_columns:
        problems.extend(_id_problems(column, _cells(frame[column])))
    problems.extend(_totals_problems(frame, id_columns))
    for raw in frame.columns:
        column = str(raw)
        if column in dates or column in id_columns:
            continue
        cells = _cells(frame[raw])
        if _CURRENCY_HEADER.search(_norm(column)):
            problems.extend(_currency_problems(column, cells))
            continue
        if cells and sum(1 for c in cells if _NUMBERISH.match(c)) >= 0.8 * len(cells):
            problems.extend(_number_problems(column, cells))
            problems.extend(_sentinel_problems(column, frame[raw]))

    checks = tuple(_date_check(c, frame[c], snapshot) for c in dates)
    for d in checks:
        if d.unparseable:
            problems.append(
                Problem(
                    code="unparseable_dates",
                    severity="error" if d.unparseable_share > 0.05 else "warning",
                    column=d.column,
                    message=f"{d.unparseable:,} of {d.non_empty:,} values are not readable dates",
                )
            )
        if d.future_dated:
            problems.append(
                Problem(
                    code="future_dates",
                    severity="error",
                    column=d.column,
                    message=f"{d.future_dated:,} values fall after the snapshot",
                )
            )
        problems.extend(_date_problems(d))
    if not dates:
        problems.append(Problem(code="no_date_column", severity="warning", message="no column holds dates"))
    temporal = TemporalIntegrity(
        ok=all(d.unparseable == 0 and d.future_dated == 0 for d in checks) and bool(checks),
        snapshot=snapshot,
        date_columns=checks,
    )

    leak = _leak_check(frame, target, dates, known_after)
    for s in leak.suspects:
        problems.append(
            Problem(
                code="leak_suspected",
                severity="error" if s.score > 0 else "warning",
                column=s.column,
                message=s.reason,
            )
        )

    score = max(0, 100 - sum(_PENALTY[p.severity] for p in problems))
    return ProfileReport(
        readiness_score=score,
        rows=rows,
        columns=len(frame.columns),
        column_profiles=tuple(profiles),
        pii_detected=tuple(pii),
        leak_check=leak,
        primary_key_valid=key.valid,
        primary_key=key,
        temporal_integrity=temporal,
        problems=tuple(problems),
        v1_enrichment=v1,
    )


def _v1_enrichment(frame: pd.DataFrame, found: Sequence[PiiFinding]) -> dict[str, Any] | None:
    """v1's Indian identifier checks (GSTIN, voter ID, mobile, PIN), when the v1 engine is importable.

    Returns counts only, plus `_findings`: personal-data findings for columns v2 did not flag. Any
    failure inside v1 is swallowed: enrichment must never break the profile.
    """
    try:
        from engine.doctor.india_ids import detect_india_ids
    except Exception:  # the v1 engine is optional here
        return None
    try:
        results = detect_india_ids(frame)
    except Exception:  # a v1 failure must not break the v2 profile
        return None
    flagged = {f.column for f in found}
    extra: list[PiiFinding] = []
    for r in results:
        if not r.personal or r.column in flagged:
            continue
        kind = "phone" if r.kind == "phone_in" else r.kind
        extra.append(
            PiiFinding(
                column=r.column,
                type=kind,
                action=_ACTION.get(kind, "drop"),
                detected_by="v1",
                match_share=round(r.share, 4),
            )
        )
    return {
        "source": "engine.doctor.india_ids",
        "india_ids": [r.as_json() for r in results],
        "_findings": extra,
    }


# --- uploads --------------------------------------------------------------------------------------


MAX_EXPANDED_BYTES: Final[int] = 10 * MAX_UPLOAD_BYTES
"""A gzipped upload is unpacked up to this size; a bigger one is refused (a compression bomb)."""


def _unwrap(data: bytes) -> tuple[str, str]:
    """The upload as text, and the encoding it was read in; refuses formats that are not CSV by name."""
    head = data[:8]
    if head.startswith(b"\x1f\x8b"):
        try:
            unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = unpacker.decompress(data, MAX_EXPANDED_BYTES)
        except zlib.error as exc:
            raise ValueError(f"the file is gzip-compressed but cannot be unpacked: {exc}") from exc
        if unpacker.unconsumed_tail:
            raise ValueError(
                f"the gzipped file unpacks to more than {MAX_EXPANDED_BYTES // 2**20} MB; send a smaller one"
            )
        head = data[:8]
    if head.startswith(b"PK\x03\x04") or head.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError(
            "this is an Excel workbook (or a zip file), not a CSV: save every sheet as CSV and upload "
            "each one (opening it with pandas' read_excel default reads only the first sheet)"
        )
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        text, encoding = data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = data.decode("latin-1"), "latin-1"
    first = text.lstrip()[:1]
    if first in ("{", "["):
        raise ValueError(
            "this looks like JSON (one record per line), not a CSV: export it as CSV, with nested "
            "fields as their own columns"
        )
    return text, encoding


def _header(text: str, delimiter: str) -> list[str]:
    first = next(csv.reader(io.StringIO(text), delimiter=delimiter), [])
    return [h.strip("\ufeff") for h in first]


def _swallowed_rows(frame: pd.DataFrame, delimiter: str) -> tuple[int, int] | None:
    """(row, lines) of a text cell that holds whole CSV lines: a stray quote ran on until a later one.

    A line counts as a swallowed row only when it splits into exactly as many fields as the file
    has columns, and most of the cell's later lines do: a multi-line comment with a few commas in
    it (a product review) is ordinary text, not a broken file."""
    width = len(frame.columns)
    least = 1 if width >= 3 else 2
    for raw in frame.columns:
        series = frame[raw]
        multi = series[series.astype("string").str.contains("\n", regex=False, na=False)]
        for label, cell in multi.items():
            lines = [line for line in str(cell).splitlines()[1:] if line.strip()]
            rowlike = sum(1 for line in lines if len(_header(line, delimiter)) == width)
            if rowlike >= least and 2 * rowlike >= len(lines):
                position = frame.index.get_loc(label)
                return int(position) if isinstance(position, int) else 0, rowlike
    return None


def read_csv_bytes(data: bytes, *, delimiter: str = ",") -> pd.DataFrame:
    """A CSV read entirely as text (ids keep their leading zeros), common blanks read as missing.

    Refuses, in plain words, what would otherwise be read wrong without a sound: a workbook, JSON, a
    file whose delimiter is not the one given (one column comes back), two columns with the same
    header, and a stray quote that swallows whole rows into one cell. Gzip and UTF-16 (Excel's
    "Unicode text") are read. A file that is not UTF-8 is read as Latin-1 and says so in
    `frame.attrs["decoded_as"]`.
    """
    text, encoding = _unwrap(data)
    if not text.strip():
        raise ValueError("the file is empty")
    header = _header(text, delimiter)
    if len(header) == 1:
        for other, name in ((";", "semicolons"), ("\t", "tabs"), ("|", "bars")):
            if other != delimiter and other in header[0]:
                raise ValueError(
                    f"only one column was found and its header holds {name}: the file seems to separate "
                    f"columns with {other!r}; send delimiter={other!r}"
                )
    repeated = [name for name, n in Counter(header).items() if n > 1 and name.strip()]
    if repeated:
        places = ", ".join(
            f"{name!r} (columns {', '.join(str(i + 1) for i, h in enumerate(header) if h == name)})"
            for name in repeated
        )
        raise ValueError(
            f"two columns have the same header: {places}; rename one so a mapping knows which to read"
        )
    try:
        frame = pd.read_csv(
            io.StringIO(text), dtype=str, keep_default_na=False, na_values=_NA_VALUES, sep=delimiter
        )
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
        raise ValueError(f"cannot read the file as CSV: {exc}") from exc
    swallowed = _swallowed_rows(frame, delimiter)
    if swallowed is not None:
        row, lines = swallowed
        raise ValueError(
            f"a quote in data row {row + 1:,} opens a text that runs over {lines:,} more lines that look "
            "like whole rows, so they were read as one cell and would vanish; fix the stray quote "
            f"(near line {row + 2:,} of the file)"
        )
    frame.attrs["decoded_as"] = encoding
    return frame


async def _body(request: Request) -> bytes:
    """The request body, refused with 413 as soon as it passes `MAX_UPLOAD_BYTES`."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"the upload is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"the upload is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


async def _upload(request: Request) -> tuple[bytes, dict[str, str]]:
    """The CSV bytes and the text fields, from a multipart form or a raw CSV body plus query params.

    Multipart is parsed with the standard library's MIME parser, so no extra package is needed.
    """
    content_type = request.headers.get("content-type", "")
    body = await _body(request)
    fields = dict(request.query_params)
    if not content_type.lower().startswith("multipart/form-data"):
        return body, fields
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    if not isinstance(message, EmailMessage) or not message.is_multipart():
        raise HTTPException(400, "the multipart body could not be read")
    file: bytes | None = None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        if name == "file":
            file = data
        elif isinstance(name, str):
            fields[name] = data.decode("utf-8", errors="replace")
    if file is None:
        raise HTTPException(422, "the form has no 'file' field")
    return file, fields


def _flag(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_SNAPSHOT: Final = TypeAdapter(AwareDatetime)


@router.post(
    "/profile",
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "snapshot": {"type": "string", "format": "date-time"},
                            "primary_key": {"type": "string"},
                            "target": {"type": "string"},
                            "date_columns": {"type": "string", "description": "Comma-separated"},
                            "delimiter": {"type": "string", "default": ","},
                            "use_v1": {"type": "boolean", "default": True},
                            "known_after": {
                                "type": "string",
                                "description": "Comma-separated columns recorded after the outcome",
                            },
                        },
                    }
                },
                "text/csv": {"schema": {"type": "string"}},
            }
        }
    },
)
async def post_profile(request: Request) -> ProfileReport:
    """Profile an uploaded CSV. Options are form fields (or query params with a raw CSV body)."""
    data, fields = await _upload(request)
    snapshot: datetime | None = None
    if fields.get("snapshot"):
        try:
            snapshot = _SNAPSHOT.validate_python(fields["snapshot"])
        except ValidationError as exc:
            raise HTTPException(422, "snapshot must be an ISO 8601 time with a zone offset") from exc
    dates_field = fields.get("date_columns")
    date_columns = [c.strip() for c in dates_field.split(",") if c.strip()] if dates_field else None
    try:
        frame = read_csv_bytes(data, delimiter=fields.get("delimiter") or ",")
        # Profiling a big file takes seconds of CPU; a worker thread keeps the event loop serving.
        return await run_in_threadpool(
            profile_frame,
            frame,
            snapshot=snapshot,
            primary_key=fields.get("primary_key") or None,
            target=fields.get("target") or None,
            date_columns=date_columns,
            use_v1=_flag(fields.get("use_v1"), True),
            known_after=[c.strip() for c in (fields.get("known_after") or "").split(",") if c.strip()],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/map-events",
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file", "mapping"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "mapping": {"type": "string", "description": "An EventMapping as JSON"},
                            "source": {"type": "string", "default": "csv"},
                            "delimiter": {"type": "string", "default": ","},
                            "ask": {
                                "type": "boolean",
                                "default": True,
                                "description": "Refuse with plain questions (422) instead of guessing",
                            },
                            "snapshot": {
                                "type": "string",
                                "format": "date-time",
                                "description": "Latest plausible time; default the upload's moment",
                            },
                        },
                    }
                }
            }
        }
    },
)
async def post_map_events(request: Request) -> MapEventsResponse:
    """Turn an uploaded CSV into events with an `EventMapping`; bad rows come back as problems."""
    data, fields = await _upload(request)
    if "mapping" not in fields:
        raise HTTPException(422, "the form has no 'mapping' field (an EventMapping as JSON)")
    try:
        mapping = EventMapping.model_validate(json.loads(fields["mapping"]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(422, f"the mapping is not a valid EventMapping: {exc}") from exc
    if mapping.not_after is None:
        # Nothing can have happened after the upload (or the snapshot the caller names): a later
        # time is a misread date, such as a two-digit year put in the wrong century.
        latest = _upload_time()
        if fields.get("snapshot"):
            try:
                latest = _SNAPSHOT.validate_python(fields["snapshot"])
            except ValidationError as exc:
                raise HTTPException(422, "snapshot must be an ISO 8601 time with a zone offset") from exc
        mapping = mapping.model_copy(update={"not_after": latest})
    ask = _flag(fields.get("ask"), True)
    try:
        frame = read_csv_bytes(data, delimiter=fields.get("delimiter") or ",")
        inspection = await run_in_threadpool(inspect_frame, frame, mapping)
        if ask and inspection.questions:
            raise MappingQuestionsError(inspection.questions)
        # Already inspected: map without asking again (the same questions, so nothing is skipped).
        events, problems = await run_in_threadpool(
            lambda: map_frame(frame, mapping, fields.get("source") or "csv")
        )
    except MappingQuestionsError as exc:
        raise HTTPException(
            422, {"message": str(exc), "questions": [q.model_dump() for q in exc.questions]}
        ) from exc
    except ValueError as exc:  # MappingError is one too
        raise HTTPException(422, str(exc)) from exc
    return MapEventsResponse(
        rows=len(frame),
        mapped=len(events),
        events=tuple(events),
        problems=tuple(problems),
        problem_counts=problem_counts(problems),
        inferred=inspection.inferred,
    )


def _upload_time() -> datetime:
    """The moment of the upload: the API's edge reads the clock once, and passes it in as `not_after`."""
    return datetime.now(UTC)
