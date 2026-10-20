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

import io
import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
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
from autonomy.data.mapping import EventMapping, MappingProblem, map_frame, problem_counts

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


def _date_check(column: str, series: pd.Series[Any], snapshot: datetime | None) -> DateColumn:
    present = series.dropna()
    present = present[present.astype(str).str.strip() != ""]
    parsed = _parse_dates(present)
    ok = parsed.dropna()
    bad = int(parsed.isna().sum())
    future = 0
    if snapshot is not None and len(ok):
        future = int((ok > pd.Timestamp(snapshot)).sum())
    return DateColumn(
        column=column,
        non_empty=len(present),
        unparseable=bad,
        unparseable_share=round(bad / len(present), 4) if len(present) else 0.0,
        future_dated=future,
        earliest=ok.min().to_pydatetime() if len(ok) else None,
        latest=ok.max().to_pydatetime() if len(ok) else None,
    )


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


def _leak_check(frame: pd.DataFrame, target: str | None, date_columns: Sequence[str]) -> LeakCheck:
    """Columns that may already contain the outcome: near-perfect correlation or purity with it."""
    if target is None:
        return LeakCheck(checked=False, target=None, note="No target column given, so no leak check ran.")
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
) -> ProfileReport:
    """Profile a table read as text (`read_csv_bytes`); see the module docstring for the rules.

    `snapshot` is the moment the data is meant to describe: a date after it is "future-dated". It
    is passed in, never read from the clock. Unknown column names raise ValueError.
    """
    for name, given in (("primary_key", primary_key), ("target", target)):
        if given is not None and given not in frame.columns:
            raise ValueError(f"{name} {given!r} is not a column of the file")
    for given in date_columns or ():
        if given not in frame.columns:
            raise ValueError(f"date column {given!r} is not a column of the file")

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
    if not dates:
        problems.append(Problem(code="no_date_column", severity="warning", message="no column holds dates"))
    temporal = TemporalIntegrity(
        ok=all(d.unparseable == 0 and d.future_dated == 0 for d in checks) and bool(checks),
        snapshot=snapshot,
        date_columns=checks,
    )

    leak = _leak_check(frame, target, dates)
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


def read_csv_bytes(data: bytes, *, delimiter: str = ",") -> pd.DataFrame:
    """A CSV read entirely as text (ids keep their leading zeros), common blanks read as missing."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if not text.strip():
        raise ValueError("the file is empty")
    try:
        return pd.read_csv(
            io.StringIO(text), dtype=str, keep_default_na=False, na_values=_NA_VALUES, sep=delimiter
        )
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
        raise ValueError(f"cannot read the file as CSV: {exc}") from exc


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
    try:
        frame = read_csv_bytes(data, delimiter=fields.get("delimiter") or ",")
        events, problems = await run_in_threadpool(map_frame, frame, mapping, fields.get("source") or "csv")
    except ValueError as exc:  # MappingError is one too
        raise HTTPException(422, str(exc)) from exc
    return MapEventsResponse(
        rows=len(frame),
        mapped=len(events),
        events=tuple(events),
        problems=tuple(problems),
        problem_counts=problem_counts(problems),
    )
