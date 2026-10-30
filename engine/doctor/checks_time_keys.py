"""Guards for time, leaks, keys and dates: small, deterministic checks that our code runs, never the AI.

Every number a check reports is counted here from the rows; no check reads the agent's words, and none can be
edited or switched off by a plan. A check returns :class:`Check` values with one of three levels:

* **stop** - the data cannot be used as it is (Excel rounded the digits of an ID, one date column written in two
  orders, an outcome that looks the wrong way round); it needs an answer or a new file;
* **flag** - it goes on the approval page for a look (a placeholder date, a repeated ID, rows two files share);
* **note** - it goes in the log (an ID report, a date that means "still open").

The functions here are shared by the visit's detector (`engine.doctor.detect_time_keys`), by the SQL step and by
the combine steps; they take frames and series and return values, and touch nothing else.

| Guard | Function |
|---|---|
| Keys that merge on tidying (collision) | :func:`key_rule_checks` |
| Key quality, rounded digits, placeholders, lost zeros | :func:`key_quality` |
| Outcome the wrong way round | :func:`outcome_direction` |
| Placeholder, open-ended, future and out-of-order dates, the Excel calendar | :func:`date_checks` |
| Day and month order written both ways | :func:`date_order_checks` |
| Unfinished last period, thin tail, immature outcomes | :func:`tail_checks` |
| Repeated header rows, duplicate rows, files that share rows | :func:`file_checks` |
| A summary table instead of customers | :func:`summary_check` |
| A value written once per block, blank below it (merged cells) | :func:`block_blank_checks` |
| The same amount column in another unit in a file being stacked | :func:`unit_scale_checks` |
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np
import pandas as pd

from engine.agent.formats import date_order_conflict, masked_cut
from engine.content_cache import ContentCache, cached_column, column_cache, frame_digest, map_distinct
from engine.doctor.impossible import _FUTURE_ALLOWED, column_words

if TYPE_CHECKING:
    from engine.agent.key_steps import KeyRule

__all__ = [
    "ID_NAME",
    "PLACEHOLDER_DATES",
    "Check",
    "Level",
    "block_blank_checks",
    "date_checks",
    "date_order_checks",
    "file_checks",
    "key_quality",
    "key_rule_checks",
    "outcome_direction",
    "read_dates",
    "summary_check",
    "tail_checks",
    "unit_scale_checks",
]

Level = Literal["stop", "flag", "note"]


@dataclass(frozen=True)
class Check:
    """One finding: what it is, how serious, in plain words, and the numbers our code counted."""

    code: str
    level: Level
    text: str
    column: str | None = None
    numbers: Mapping[str, Any] = field(default_factory=dict)
    as_of: date | None = None
    """A tail check's proposal: the last date worth keeping."""


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "0%"


_CELLS: Final[ContentCache[pd.Series[Any]]] = column_cache(8, megabytes=128)
"""The filled cells of a column as text, by what the column holds: a dozen checks read the same cells."""


def _cells(series: pd.Series[Any]) -> pd.Series[Any]:
    """The filled cells as trimmed text."""

    def make() -> pd.Series[Any]:
        present = series.dropna()
        text = map_distinct(present, lambda distinct: distinct.astype(str).str.strip())
        return text[text != ""]

    return cached_column(_CELLS, series, "cells", make)


# ---------------------------------------------------------------------------
# Keys (G4, G5)
# ---------------------------------------------------------------------------
_E_PLUS: Final[re.Pattern[str]] = re.compile(r"^[+-]?\d(\.\d+)?[eE][+-]?\d{1,2}$")
_PLACEHOLDER_KEY: Final[re.Pattern[str]] = re.compile(
    r"^(na|n/a|nan|null|none|nil|unknown|tbd|temp|x+|-+|\.+)$" r"|^(test|dummy|sample|demo)([\W_]|\d|$)",
    re.IGNORECASE,
)
_PLACEHOLDER_DIGITS: Final[re.Pattern[str]] = re.compile(r"^(0+|1+|9+|1234567890|123456789)$")
"""All zeros, ones or nines: a stand-in only when more than one row holds it (`1` and `99` are real IDs too)."""
_SHORT_SHARE: Final[float] = 0.01
_TOP: Final[int] = 5
ID_NAME: Final[re.Pattern[str]] = re.compile(
    r"(^id$|_id$|^id_|_key$|(customer|cust|client|member|account)[_ ]?(no|num|number|code|ref)$"
    r"|phone|mobile|email|^pan$)",
    re.I,
)
"""Columns checked as IDs besides the file's own key: named like an ID, not merely like a customer
(`customer_segment` is not an ID)."""


def _rounded_digits(series: pd.Series[Any]) -> int:
    """Numbers of nine or more digits that end in five or more zeros: an ID Excel rounded to six digits."""
    if pd.api.types.is_bool_dtype(series):
        return 0
    if pd.api.types.is_numeric_dtype(series):
        numbers = pd.to_numeric(series, errors="coerce").dropna()
    else:  # an ID read as text (keeping its zeros) still shows Excel's rounding as trailing zeros
        cells = _cells(series)
        numbers = pd.to_numeric(cells[cells.str.fullmatch(r"\d{9,}")], errors="coerce").dropna()
    big = numbers[numbers.abs() >= 1e9]
    if len(big) < 5:
        return 0
    rounded = big.map(lambda value: int(value) % 100_000 == 0)
    return int(rounded.sum()) if rounded.mean() >= 0.5 else 0


_MOSTLY_UNIQUE: Final[float] = 0.5
"""A key with at least this share of different values is expected to be unique; one below it repeats by design."""


def key_quality(
    series: pd.Series[Any], column: str, *, expect_unique: bool = False, exact: Any = None
) -> list[Check]:
    """The report on one key column: how many are unique, the most common values, placeholders, IDs Excel rounded
    (`1.23457E+11`: a stop), and IDs shorter than the usual length (zeros at the start lost).

    `exact` (the column's exact counts over the whole file, `ColumnStats`) replaces the sample's distinct count
    and top values when it is given."""
    cells = _cells(series)
    if not len(cells):
        return []
    sampled = len(cells)
    filled = int(exact.row_count - exact.null_count) if exact is not None else sampled
    found: list[Check] = []
    rounded_text = int(cells.str.match(_E_PLUS).sum())
    rounded = rounded_text or _rounded_digits(series)
    if rounded:
        found.append(
            Check(
                "KEY_DIGITS_ROUNDED",
                "stop",
                f"{rounded:,} of {sampled:,} values in '{column}' have been rounded off by Excel "
                "(written like 1.23457E+11). The missing digits cannot be recovered, so nothing is matched on "
                "this column. Save the column as Text, or send the .xlsx file.",
                column,
                {"rounded": rounded, "filled": sampled},
            )
        )
    counts = cells.value_counts()
    distinct = int(exact.distinct_count) if exact is not None else len(counts)
    unique_share = distinct / max(1, filled)
    top = [(masked_cut(str(value), 30), int(rows)) for value, rows in counts.head(_TOP).items()]
    found.append(
        Check(
            "KEY_REPORT",
            "note",
            f"'{column}' has {distinct:,} different value{'s' if distinct != 1 else ''} in {filled:,} filled "
            f"row{'s' if filled != 1 else ''}"
            + (" (every value is different)" if distinct == filled else "")
            + f"; the most common value appears {top[0][1]:,} times"
            + (" in the sample." if exact is not None and sampled < filled else "."),
            column,
            {"filled": filled, "distinct": distinct, "unique_share": round(unique_share, 4), "top": top},
        )
    )
    # A column whose values mostly repeat names the customer on each row of a log (many rows per customer): that is
    # what it is for, not duplicates of an ID that should be unique.
    if expect_unique and distinct < filled and unique_share >= _MOSTLY_UNIQUE:
        found.append(
            Check(
                "KEY_NOT_UNIQUE",
                "flag",
                f"'{column}' should name each customer once, but {filled - distinct:,} of {filled:,} values "
                "repeat an earlier one.",
                column,
                {"repeated": filled - distinct, "filled": filled},
            )
        )
    repeated = cells.map(counts) >= 2
    placeholder = cells[cells.str.match(_PLACEHOLDER_KEY) | (cells.str.match(_PLACEHOLDER_DIGITS) & repeated)]
    if len(placeholder):
        named = [masked_cut(str(v), 30) for v in placeholder.value_counts().head(3).index]
        found.append(
            Check(
                "KEY_PLACEHOLDER",
                "flag",
                f"{len(placeholder):,} rows ({_pct(len(placeholder), filled)}) of '{column}' hold a stand-in rather "
                f"than a real ID ({', '.join(named)}). Rows sharing one would be matched to each other.",
                column,
                {"rows": len(placeholder), "filled": filled, "examples": named},
            )
        )
    digits = cells[cells.str.fullmatch(r"\d+")]
    if len(digits) >= 0.8 * filled:
        lengths = Counter(len(v) for v in digits)
        usual = max(lengths, key=lambda n: (lengths[n], n))
        shorter = sum(count for length, count in lengths.items() if length < usual)
        if shorter and shorter / filled >= _SHORT_SHARE and lengths[usual] / filled >= 0.5:
            found.append(
                Check(
                    "KEY_MIXED_LENGTHS",
                    "flag",
                    f"{shorter:,} values in '{column}' ({_pct(shorter, filled)}) are shorter than the usual "
                    f"{usual} digits. Zeros at the start may have been lost, so they would not match the same "
                    "ID in another file.",
                    column,
                    {"shorter": shorter, "usual_length": usual, "filled": filled},
                )
            )
    return found


def key_rule_checks(series: pd.Series[Any], rule: KeyRule, column: str) -> list[Check]:
    """What a key tidy-up rule does to the distinct IDs: IDs it would make the same (`C-123` and `S-123` both
    `123`: a stop), and spellings of one ID it joins (`c-77` and `C-77`: shown as "IDs merged: N")."""
    from engine.agent.key_steps import KeyColumn, hard_merges

    keys = KeyColumn(series)
    before = len(keys.texts)
    hard = hard_merges(keys, rule)
    merged = keys.merged_by(rule)
    numbers = {"distinct_before": before, "distinct_after": before - merged, "merged": merged}
    if hard:
        return [
            Check(
                "KEY_RULE_MERGES_IDS",
                "stop",
                f"Tidying the IDs in '{column}' this way would make {hard:,} different IDs the same ID "
                f"({before:,} IDs before, {before - merged:,} after). Customers would be mixed up, so it is "
                "not done.",
                column,
                {**numbers, "different_ids_merged": hard},
            )
        ]
    if merged:
        return [
            Check(
                "KEY_RULE_MERGES_SPELLINGS",
                "flag",
                f"IDs merged: {merged:,}. In '{column}' {before:,} IDs become {before - merged:,} once spaces "
                "and capitals are ignored.",
                column,
                numbers,
            )
        ]
    return []


# ---------------------------------------------------------------------------
# The outcome (G8)
# ---------------------------------------------------------------------------
_BAD_OUTCOME: Final[re.Pattern[str]] = re.compile(
    r"churn|default|fraud|cancel|lapse|attrition|dormant|closed", re.I
)


def outcome_direction(column: str, yes: int, no: int) -> Check | None:
    """A stop when more than half the customers are "yes" for an outcome that is a loss (churn, default, fraud):
    the file may call the good outcome "yes". `yes` and `no` are counted from the stated meaning of each value.
    """
    total = yes + no
    if not total or yes / total <= 0.5 or not _BAD_OUTCOME.search(column):
        return None
    return Check(
        "OUTCOME_WRONG_WAY_ROUND",
        "stop",
        f"{_pct(yes, total)} of {total:,} customers are marked as '{column}'. That is unusual for an outcome "
        "like this. Is it the right way round?",
        column,
        {"yes": yes, "no": no, "share": round(yes / total, 4)},
    )


# ---------------------------------------------------------------------------
# Dates (G9)
# ---------------------------------------------------------------------------
PLACEHOLDER_DATES: Final[tuple[date, ...]] = (
    date(1899, 12, 30),
    date(1900, 1, 1),
    date(1904, 1, 1),
    date(1970, 1, 1),
)
"""Zero in Excel's two calendars, 1 January 1900, and zero in Unix time: what a blank turns into."""
_OPEN_YEAR: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:9999|2999|2099)[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.](?:9999|2999|2099))(?:[ T].*)?$"
)
"""A cell that is a date in 9999, 2999 or 2099 (year first or last), not any text holding those digits."""
_END_WORDS: Final[frozenset[str]] = frozenset(
    {
        "end",
        "ends",
        "ended",
        "to",
        "until",
        "till",
        "expiry",
        "expires",
        "valid",
        "closed",
        "close",
        "terminated",
    }
)
_SPIKE_SHARE: Final[float] = 0.01
_ROLES: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    (
        "signup",
        frozenset(
            {"signup", "joined", "join", "registered", "registration", "created", "activation", "onboarded"}
        ),
    ),
    ("first", frozenset({"first"})),
    ("last", frozenset({"last", "latest", "recent"})),
)
_ORDER: Final[tuple[tuple[str, str], ...]] = (("signup", "first"), ("signup", "last"), ("first", "last"))
_ROLE_WORDS: Final[dict[str, str]] = {word: role for role, words in _ROLES for word in words}


def read_dates(series: pd.Series[Any]) -> pd.Series[Any] | None:
    """The column as dates, or None when it is not dates or its day/month order cannot be told (never guessed)."""
    from engine.doctor.time_settings import date_values

    return date_values(series)


def _role(name: str) -> str | None:
    for word in sorted(column_words(name)):
        if word in _ROLE_WORDS:
            return _ROLE_WORDS[word]
    return None


def date_checks(frame: pd.DataFrame, as_of: date) -> list[Check]:
    """Placeholder-date spikes, "still open" end dates, dates after `as_of`, related dates out of order, and
    Excel day numbers that read as a calendar four years early."""
    found: list[Check] = []
    readable: dict[str, pd.Series[Any]] = {}
    for name in frame.columns:
        column = str(name)
        series = frame[name]
        if not pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_datetime64_any_dtype(series):
            open_cells = _cells(series)
            open_cells = open_cells[open_cells.str.contains(_OPEN_YEAR)]
            if len(open_cells):
                still_open = bool(column_words(column) & _END_WORDS)
                found.append(
                    Check(
                        "DATE_STILL_OPEN" if still_open else "DATE_PLACEHOLDER_FAR",
                        "note" if still_open else "flag",
                        f"{len(open_cells):,} rows in '{column}' hold a date in the year {open_cells.iloc[0][:10]}"
                        + (
                            ", which means 'no end date': read as still open, not as a date."
                            if still_open
                            else ", a stand-in rather than a real date."
                        ),
                        column,
                        {"rows": len(open_cells)},
                    )
                )
        values = read_dates(series)
        if values is None:
            found.extend(_excel_calendar(series, column, as_of))
            continue
        readable[column] = values
        days = values.dropna().dt.normalize()
        if days.empty:
            continue
        counts = days.value_counts()
        for placeholder in PLACEHOLDER_DATES:
            rows = int(counts.get(pd.Timestamp(placeholder), 0))
            if rows >= 3 and rows / len(days) >= _SPIKE_SHARE:
                found.append(
                    Check(
                        "DATE_PLACEHOLDER",
                        "flag",
                        f"{rows:,} rows ({_pct(rows, len(days))}) of '{column}' are {placeholder.isoformat()}, "
                        "which is usually a blank written as a date.",
                        column,
                        {"rows": rows, "date": placeholder.isoformat(), "filled": len(days)},
                    )
                )
        if not (column_words(column) & _FUTURE_ALLOWED):
            later = days[days > pd.Timestamp(as_of)]
            if len(later):
                found.append(
                    Check(
                        "DATE_AFTER_AS_OF",
                        "flag",
                        f"{len(later):,} rows of '{column}' are dated after {as_of.isoformat()} (the latest is "
                        f"{later.max().date().isoformat()}), which cannot have happened yet.",
                        column,
                        {
                            "rows": len(later),
                            "as_of": as_of.isoformat(),
                            "latest": later.max().date().isoformat(),
                        },
                    )
                )
    found.extend(_out_of_order(readable))
    return found


def _out_of_order(readable: Mapping[str, pd.Series[Any]]) -> list[Check]:
    roles: dict[str, str] = {}
    for name in readable:
        role = _role(name)
        if role is not None and role not in roles:
            roles[role] = name
    found: list[Check] = []
    for early, late in _ORDER:
        if early not in roles or late not in roles:
            continue
        a, b = readable[roles[early]], readable[roles[late]]
        both = a.notna() & b.notna()
        wrong = int((a[both].dt.normalize() > b[both].dt.normalize()).sum())
        if wrong >= 3 or (both.sum() and wrong / both.sum() >= _SPIKE_SHARE and wrong):
            found.append(
                Check(
                    "DATE_ORDER_VIOLATED",
                    "flag",
                    f"{wrong:,} of {int(both.sum()):,} rows have '{roles[early]}' later than '{roles[late]}', "
                    "which should come the other way round.",
                    roles[early],
                    {
                        "rows": wrong,
                        "both_filled": int(both.sum()),
                        "earlier": roles[early],
                        "later": roles[late],
                    },
                )
            )
    return found


def _excel_calendar(series: pd.Series[Any], column: str, as_of: date) -> list[Check]:
    """Day numbers read in Excel's 1900 calendar end more than three years before `as_of`, while the 1904
    calendar (the Mac one) puts the newest within a year of it: the workbook may use the other calendar."""
    from engine.agent.formats import looks_like_date_name, parse_excel_serial_dates

    if not pd.api.types.is_numeric_dtype(series) or not looks_like_date_name(column):
        return []
    numbers = pd.to_numeric(series, errors="coerce").dropna()
    if len(numbers) < 20 or not numbers.between(1, 60_000).mean() >= 0.95:
        return []
    newest = {
        flag: parse_excel_serial_dates(numbers.astype(float), date_1904=flag).values.dropna()
        for flag in (False, True)
    }
    if newest[False].empty or newest[True].empty:
        return []
    old, mac = newest[False].max(), newest[True].max()
    if pd.Timestamp(as_of) - old > pd.Timedelta(days=3 * 365) and abs(
        pd.Timestamp(as_of) - mac
    ) <= pd.Timedelta(days=366):
        return [
            Check(
                "DATE_EXCEL_CALENDAR",
                "flag",
                f"'{column}' holds Excel day numbers. Read normally the newest is {old.date().isoformat()}; in the "
                f"Mac calendar it is {mac.date().isoformat()}. Which calendar did the workbook use?",
                column,
                {"newest_1900": old.date().isoformat(), "newest_1904": mac.date().isoformat()},
            )
        ]
    return []


def date_order_checks(frame: pd.DataFrame) -> list[Check]:
    """A stop for a date column written both day first and month first: no one order reads it, and nothing is
    guessed (a column that cannot tell is the advisor's question)."""
    found: list[Check] = []
    for name in frame.columns:
        series = frame[name]
        if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
            continue
        cells = pd.Series(_cells(series).unique(), dtype=object)
        if len(cells) and date_order_conflict(cells):
            found.append(
                Check(
                    "DATE_ORDER_BOTH",
                    "stop",
                    f"'{name}' has dates written day first on some rows (25/03/2024) and month first on others "
                    "(03/25/2024), so no single order reads it. It is not read until the dates are written one way.",
                    str(name),
                    {"distinct_spellings": len(cells)},
                )
            )
    return found


# ---------------------------------------------------------------------------
# The newest period (G11)
# ---------------------------------------------------------------------------
_COVERED: Final[float] = 0.9
_THIN: Final[float] = 0.6
_MIN_MEDIAN: Final[int] = 5
_IMMATURE: Final[float] = 0.5


def tail_checks(
    dates: pd.Series[Any], yes: pd.Series[Any] | None = None, covered: pd.Series[Any] | None = None
) -> list[Check]:
    """Whether the newest rows are a finished picture. Each finding proposes the date to treat as the as-of date
    (`Check.as_of`: the last date worth keeping) and the rows after it.

    * the last calendar month covers less than 90% of its days (for dates on every day; for one row per month,
      `covered` says how many days each row covers, e.g. a `days_covered` column);
    * the file's final days hold under 60% of the rows the same weekday had on average over the 4 weeks before;
    * the newest month's outcome rate is under half the earlier months' median (outcomes not final yet).
    """
    days = dates.dropna().dt.normalize()
    monthly = days.nunique() >= 2 and bool(
        (days.dt.day == 1).all()
    )  # months written as 2026-09 read as the 1st
    if days.nunique() < 14 and not monthly:
        return []
    last = days.max()
    found: list[Check] = []
    month_start = last.replace(day=1)
    in_month = (last + pd.offsets.MonthEnd(0) - month_start).days + 1
    if monthly:
        in_last = (
            pd.to_numeric(covered, errors="coerce")[dates.dt.normalize() == last]
            if covered is not None
            else None
        )
        got = float(in_last.median()) if in_last is not None and in_last.notna().any() else None
        shown = last.strftime("%Y-%m")
    else:
        got = float((last - month_start).days + 1) if last != last + pd.offsets.MonthEnd(0) else None
        shown = last.date().isoformat()
    if got is not None and got / in_month < _COVERED:
        covered_days = int(got)
        keep = (month_start - pd.Timedelta(days=1)).date()
        after = int((days > pd.Timestamp(keep)).sum())
        found.append(
            Check(
                "PARTIAL_LAST_MONTH",
                "flag",
                f"The newest month is only partly in the file: {covered_days} of {in_month} days "
                f"(up to {shown}). Counting it as a full month would make everything look "
                f"lower. {after:,} rows are dated after {keep.isoformat()}.",
                None,
                {
                    "days_covered": covered_days,
                    "days_in_month": in_month,
                    "rows_after": after,
                    "latest": last.date().isoformat(),
                },
                keep,
            )
        )
    thin = None if monthly else _thin_tail(days)
    if thin is not None:
        keep_day, run, after = thin
        found.append(
            Check(
                "THIN_TAIL",
                "flag",
                f"The last {run} days of the file hold far fewer rows than the same weekdays did before "
                f"(under {_THIN:.0%}): data from the end of the file may still be arriving. {after:,} rows are "
                f"dated after {keep_day.isoformat()}.",
                None,
                {"thin_days": run, "rows_after": after, "latest": last.date().isoformat()},
                keep_day,
            )
        )
    if yes is not None:
        immature = _immature_month(days, yes.reindex(dates.index).fillna(False).astype(bool))
        if immature is not None:
            newest, older, keep_day, after = immature
            found.append(
                Check(
                    "IMMATURE_PERIOD",
                    "flag",
                    f"The newest month is not finished: its outcome rate is {newest:.1%}, against {older:.1%} in "
                    f"the months before, so its outcomes are probably not final yet. {after:,} rows are dated "
                    f"after {keep_day.isoformat()}.",
                    None,
                    {"newest_rate": round(newest, 4), "earlier_rate": round(older, 4), "rows_after": after},
                    keep_day,
                )
            )
    return found


def _thin_tail(days: pd.Series[Any]) -> tuple[date, int, int] | None:
    daily = days.value_counts().sort_index()
    full = daily.reindex(pd.date_range(daily.index.min(), daily.index.max(), freq="D"), fill_value=0)
    if len(full) < 35:
        return None
    thin_run = 0
    counts = [int(n) for n in full.tolist()]
    for offset in range(len(full) - 1, max(len(full) - 8, 27), -1):
        median = statistics.median(counts[offset - 7 * k] for k in range(1, 5))
        if median < _MIN_MEDIAN or counts[offset] >= _THIN * median:
            break
        thin_run += 1
    if thin_run < 2:
        return None
    keep = (full.index[len(full) - thin_run] - pd.Timedelta(days=1)).date()
    return keep, thin_run, int((days > pd.Timestamp(keep)).sum())


def _immature_month(days: pd.Series[Any], hits: pd.Series[Any]) -> tuple[float, float, date, int] | None:
    months = days.dt.to_period("M")
    rates = hits.groupby(months).agg(["mean", "count"])
    if len(rates) < 4:
        return None
    newest = rates.iloc[-1]
    earlier = rates.iloc[-4:-1]
    median = float(earlier["mean"].median())
    if newest["count"] < 30 or median <= 0 or newest["mean"] >= _IMMATURE * median:
        return None
    keep = (months.iloc[-1].start_time - pd.Timedelta(days=1)).date()
    return float(newest["mean"]), median, keep, int((days > pd.Timestamp(keep)).sum())


# ---------------------------------------------------------------------------
# Rows that repeat, and files that share rows (G12)
# ---------------------------------------------------------------------------
def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _repeated_headers(frame: pd.DataFrame) -> int:
    names = [str(c).strip().casefold() for c in frame.columns]
    if len(names) < 2 or frame.empty:
        return 0
    matches = np.zeros(len(frame), dtype=np.int64)
    for position, name in enumerate(names):
        column = frame.iloc[:, position]
        if (
            pd.api.types.is_numeric_dtype(column)
            and not pd.api.types.is_bool_dtype(column)
            and not _is_number(name)
        ):
            continue  # a number is never written as a word
        # Each distinct spelling is read once, then spread over the rows.
        codes, spellings = pd.factorize(column.astype(str))
        same = np.fromiter(
            (text.strip().casefold() == name for text in spellings), dtype=bool, count=len(spellings)
        )
        matches += same[codes]
    return int((matches >= max(2, len(names) // 2)).sum())


_FILE_CHECKS: Final[ContentCache[tuple[Check, ...]]] = ContentCache(8)
"""The checks of one file by what it holds (they read every cell, and a visit asks again at every step)."""


def file_checks(files: Sequence[tuple[str, pd.DataFrame]]) -> list[Check]:
    """For each file `(name, rows)`: header rows repeated in the middle and rows that are complete copies of
    another (reported, never dropped); for files laid out alike: rows two of them both hold."""
    found: list[Check] = []
    for name, frame in files:
        digest = frame_digest(frame)
        key = None if digest is None else f"{name}|{digest}"
        found.extend(_FILE_CHECKS.get_or_compute(key, partial(_file_checks, name, frame)))
    for i, (name_a, a) in enumerate(files):
        for name_b, b in files[i + 1 :]:
            found.extend(_shared_rows(name_a, a, name_b, b))
    return found


def _file_checks(name: str, frame: pd.DataFrame) -> tuple[Check, ...]:
    found: list[Check] = []
    headers = _repeated_headers(frame)
    if headers:
        found.append(
            Check(
                "REPEATED_HEADER_ROWS",
                "flag",
                f"{headers:,} rows of {name} repeat the column names (the file looks pasted together from "
                "several pieces). They are counted as customers unless they are removed.",
                None,
                {"rows": headers, "file": name},
            )
        )
    copies = int(frame.duplicated().sum()) if len(frame.columns) else 0
    if copies:
        found.append(
            Check(
                "DUPLICATE_ROWS",
                "flag",
                f"{copies:,} rows of {name} are exact copies of another row ({_pct(copies, len(frame))}). "
                "Ticking this keeps them as they are: nothing is removed without your yes, and spend or counts "
                "would be inflated while they stay.",
                None,
                {"rows": copies, "file": name, "file_rows": len(frame)},
            )
        )
    return tuple(found)


def _shared_rows(name_a: str, a: pd.DataFrame, name_b: str, b: pd.DataFrame) -> list[Check]:
    common = [c for c in a.columns if c in set(b.columns)]
    if len(common) < 3 or len(common) < 0.8 * max(len(a.columns), len(b.columns)):
        return []
    ha = set(pd.util.hash_pandas_object(a[common].astype(str), index=False).tolist())
    hb = set(pd.util.hash_pandas_object(b[common].astype(str), index=False).tolist())
    shared = len(ha & hb)
    if not shared:
        return []
    smaller = min(len(ha), len(hb))
    return [
        Check(
            "FILES_SHARE_ROWS",
            "flag",
            f"{shared:,} rows appear in both {name_a} and {name_b} ({_pct(shared, smaller)} of the smaller "
            "one): the two exports overlap, and anything added up across both would count them twice.",
            None,
            {"rows": shared, "files": [name_a, name_b]},
        )
    ]


# ---------------------------------------------------------------------------
# A summary, merged cells, and another unit (F05, F04, V01)
# ---------------------------------------------------------------------------
_TOTAL_ROW: Final[re.Pattern[str]] = re.compile(r"^(grand\s+|sub[\s-]?)?totals?$", re.I)
_COUNT_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(count|counts|n|num|number|customers|members|accounts|users|(customer|member|user)_?(count|total))$",
    re.I,
)
_BLOCK_BLANK_SHARE: Final[float] = 0.2
_BLOCK_RUN: Final[float] = 5.0
"""Blanks that come in runs this long, after a value, are a merged cell; a few scattered blanks are not."""
_UNIT_GAP: Final[float] = 50.0


def _total_rows(series: pd.Series[Any]) -> int:
    """How many rows of a column read 'Total' (each distinct spelling is matched once)."""
    counts = _cells(series).value_counts()
    return (
        int(counts[counts.index.to_series().str.match(_TOTAL_ROW).to_numpy(dtype=bool)].sum())
        if len(counts)
        else 0
    )


def summary_check(frame: pd.DataFrame) -> Check | None:
    """A stop when the table is a summary, not customers: a 'Total' row, or counts per group with no ID column and
    no text column that is different on every row. Totals cannot be turned back into people."""
    if len(frame) < 2:
        return None
    text = [c for c in frame.columns if not pd.api.types.is_numeric_dtype(frame[c])]
    total_rows = sum(_total_rows(frame[c]) for c in text)
    one_each = any(ID_NAME.search(str(c)) for c in frame.columns) or any(
        frame[c].notna().all() and frame[c].is_unique for c in text
    )
    counts = [str(c) for c in frame.columns if _COUNT_NAME.match(str(c).strip())]
    if not total_rows and (one_each or not counts or not text):
        return None
    why = (
        "a 'Total' row"
        if total_rows
        else f"counts per group ('{counts[0]}') and no ID that is different on every row"
    )
    return Check(
        "SUMMARY_NOT_ROWS",
        "stop",
        f"This looks like a summary report (a pivot table or totals), not one row per customer: it has {why}. "
        "Totals cannot be turned back into customers, so no customers are made up from it.",
        None,
        {"total_rows": total_rows, "count_columns": counts, "rows": len(frame)},
    )


def block_blank_checks(frame: pd.DataFrame) -> list[Check]:
    """A text column filled once and then blank for a run of rows (a merged cell in Excel): at least a fifth of the
    rows blank, the first row filled, the blanks in runs of five or more on average, and each filled value different
    from the one before it (a sparse column such as a cancellation reason repeats its values). Dates are left out
    (an end date is blank while still open). Rows must be in file order.
    """
    found: list[Check] = []
    for name in frame.columns:
        series = frame[name]
        if pd.api.types.is_numeric_dtype(series) or ID_NAME.search(str(name)) or len(series) < 10:
            continue
        blank = series.isna() | (series.astype(str).str.strip() == "")
        runs = int((blank & ~blank.shift(fill_value=False)).sum())
        share = int(blank.sum()) / len(series)
        if blank.iloc[0] or not runs or share < _BLOCK_BLANK_SHARE or blank.sum() / runs < _BLOCK_RUN:
            continue
        filled = series[~blank].astype(str).str.strip()
        if (filled == filled.shift()).any() or read_dates(series) is not None:
            continue
        column = str(name)
        found.append(
            Check(
                "BLOCK_BLANKS",
                "flag",
                f"'{column}' is filled on {int((~blank).sum()):,} rows and blank on the {int(blank.sum()):,} rows "
                "below them, the way merged cells in Excel come out. Does a blank mean the same as the row above?",
                column,
                {"blank": int(blank.sum()), "filled": int((~blank).sum()), "rows": len(series)},
            )
        )
    return found


def unit_scale_checks(files: Sequence[tuple[str, pd.DataFrame]]) -> list[Check]:
    """For files about to be stacked: a number column in two files whose medians are 50 times apart or more (rupees
    in one, paise in the other), largest gap first. IDs, and columns with zeros as their middle, are not compared.
    """
    medians: dict[str, dict[str, float]] = {}
    for name, frame in files:
        for column in frame.columns:
            if ID_NAME.search(str(column)) or not pd.api.types.is_numeric_dtype(frame[column]):
                continue
            middle = float(pd.to_numeric(frame[column], errors="coerce").abs().median())
            if middle > 0:
                medians.setdefault(str(column), {})[name] = middle
    found: list[Check] = []
    for column, by_file in medians.items():
        if len(by_file) < 2:
            continue
        small, big = min(by_file, key=lambda n: by_file[n]), max(by_file, key=lambda n: by_file[n])
        gap = by_file[big] / by_file[small]
        if gap >= _UNIT_GAP:
            found.append(
                Check(
                    "UNIT_SCALE_DIFFERS",
                    "stop",
                    f"'{column}' is about {gap:,.0f} times larger in {big} (typically {by_file[big]:,.0f}) than in "
                    f"{small} (typically {by_file[small]:,.0f}), as if the files use different units "
                    "(for example paise and rupees).",
                    column,
                    {"gap": round(gap, 1), "larger": big, "smaller": small},
                )
            )
    return sorted(found, key=lambda c: -float(c.numbers["gap"]))
