"""Wide sheets (Plan L M156, DEC-1560 … DEC-1569): plan and run an `unpivot` recipe step.

A hand-kept sheet often has one column per month - `customer_id, 2024_01_spend, 2024_02_spend,
2024_01_calls, 2024_02_calls` - so each month is an unrelated column. `unpivot` turns those columns into
rows: one row per customer per month, with one column per measure (`spend`, `calls`) and one for the
period. The rows can then be combined back into one row per customer by `combine_rows` (level 3), with
totals, averages and the latest value instead of a column per month.

**The grammar.** A column name is a *measure* and a *period* joined by one separator, in a fixed order:
`name_pattern` is one of `{measure}<sep>{period}` or `{period}<sep>{measure}`, with `<sep>` one of `_`,
`-`, `.` or a space. A period is one of a fixed list of tokens (case does not matter):

| Token | Example | Has a year | Written in the period column as |
|---|---|---|---|
| year and month | `2024_01`, `2024-01`, `202401`, `Jan_2024`, `2024 Jan` | yes | the month's first day, `2024-01-01` |
| year and quarter | `2024_Q1`, `Q1 2024`, `2024Q1` | yes | the quarter's first day, `2024-01-01` |
| year | `2024` | yes | the year's first day, `2024-01-01` |
| month | `Jan`, `January`, `Sept` | no | as written, `Jan` |
| month number | `M1` … `M12` | no | as written, `M1` |
| quarter | `Q1` … `Q4` | no | as written, `Q1` |

Every column of one step has the same kind of period (all months with a year, or all quarters, …), so
the period column holds one kind of value. A period with a year is written as the date it starts, so the
rows can be put in date order and combined; a period without one is written as it is in the column name,
and nothing is guessed about its year. The person may give that year (`year`, for months or quarters only): the
periods are then dated like `2024_Jan`, from the year the person gave and never one read off the file.

**Frozen.** The parameters list every value column with its measure and period (`value_columns`), as they
were when the person approved the step; `name_pattern` must rebuild each name from its parts, so a column
cannot be filed under another measure. Next month's file is turned into rows by exactly the same list: a
listed column it lacks stops the run (`RECIPE_COLUMN_MISSING`), and a column it has that fits the
pattern for a listed measure but is not listed (a new month) is left out and its non-empty cells are
counted on the receipt as cells that did not fit; more of them than `max_conversion_failure_pct` of the
cells stops the run (`RECIPE_VALUES_UNCONVERTED`) rather than training or scoring on part of the months.

**Stateless.** A row's output depends only on that row and the frozen parameters (DEC-1004): each input
row becomes one row per listed period, in period order, and the rows of the first *n* input rows are the
first rows of the output. A period with no cell for a measure (a sheet with `2024_03_spend` but no
`2024_03_calls`) gets an empty cell; a row whose cells are all empty is kept, not guessed away.

**Kept columns.** `id_columns` are copied onto every row an input row becomes; they include the step's
own column (the ID). The outcome (`outcome`) is carried the same way when the file has it - never turned
into rows and never one of `id_columns`, so a scoring file without it still runs. Every other column is
left out, so the output has the same columns on every file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

import numpy as np
import pandas as pd

from engine.agent.formats import masked_cut

__all__ = [
    "MAX_ID_COLUMNS",
    "MAX_VALUE_COLUMNS",
    "MAX_YEAR",
    "MIN_YEAR",
    "NAME_PATTERNS",
    "Period",
    "UnpivotError",
    "UnpivotResult",
    "UnpivotSpec",
    "WideGroup",
    "find_wide_columns",
    "parse_period",
    "unpivot",
]

MAX_ID_COLUMNS: Final[int] = 200
MIN_YEAR: Final[int] = 1900
MAX_YEAR: Final[int] = 2100
MAX_VALUE_COLUMNS: Final[int] = 1000
_SEPARATORS: Final[tuple[str, ...]] = ("_", "-", ".", " ")
NAME_PATTERNS: Final[frozenset[str]] = frozenset(
    {f"{{measure}}{sep}{{period}}" for sep in _SEPARATORS}
    | {f"{{period}}{sep}{{measure}}" for sep in _SEPARATORS}
)
"""The only ways a column name may be read as a measure and a period."""

_PARAMS: Final[frozenset[str]] = frozenset(
    {"id_columns", "value_columns", "name_pattern", "period_column", "outcome", "year"}
)
_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_ .\-]{0,127}$")
"""A measure or period column name: starts with a letter, at most 128 characters."""

_MONTHS: Final[dict[str, int]] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH: Final[str] = "|".join(sorted(_MONTHS, key=lambda name: (-len(name), name)))
_YEAR: Final[str] = r"(?:19|20)\d{2}"
_JOIN: Final[str] = r"[_\-. ]?"
_TOKENS: Final[tuple[tuple[str, bool, re.Pattern[str]], ...]] = tuple(
    (kind, dated, re.compile(pattern, re.IGNORECASE))
    for kind, dated, pattern in (
        ("month", True, rf"(?P<y>{_YEAR}){_JOIN}(?P<m>0[1-9]|1[0-2])"),
        ("month", True, rf"(?P<y>{_YEAR}){_JOIN}(?P<mn>{_MONTH})"),
        ("month", True, rf"(?P<mn>{_MONTH}){_JOIN}(?P<y>{_YEAR})"),
        ("quarter", True, rf"(?P<y>{_YEAR}){_JOIN}Q(?P<q>[1-4])"),
        ("quarter", True, rf"Q(?P<q>[1-4]){_JOIN}(?P<y>{_YEAR})"),
        ("year", True, rf"(?P<y>{_YEAR})"),
        ("month", False, rf"(?P<mn>{_MONTH})"),
        ("month", False, r"M(?P<m>1[0-2]|[1-9])"),
        ("quarter", False, r"Q(?P<q>[1-4])"),
    )
)
"""Period tokens, longest first: `2024_01` is a month of 2024 before `2024` alone could be a year."""
_ANY_PERIOD: Final[str] = "|".join(
    "(?:" + re.sub(r"\(\?P<[a-z]+>", "(?:", pattern.pattern) + ")" for _, _, pattern in _TOKENS
)
"""Any period token, without its named parts, for reading a whole column name."""
_PERIOD_FIRST: Final[re.Pattern[str]] = re.compile(
    rf"(?P<period>{_ANY_PERIOD})(?P<sep>[_\-. ])(?P<measure>[A-Za-z][A-Za-z0-9_\-. ]*)", re.IGNORECASE
)
_PERIOD_LAST: Final[re.Pattern[str]] = re.compile(
    rf"(?P<measure>[A-Za-z][A-Za-z0-9_\-. ]*?)(?P<sep>[_\-. ])(?P<period>{_ANY_PERIOD})", re.IGNORECASE
)


class UnpivotError(Exception):
    """An unpivot that cannot run on this file; `code` is a RECIPE_* code the recipe engine passes on."""

    def __init__(self, code: str, message: str, *, column: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.column = column


@dataclass(frozen=True)
class Period:
    """One period token read by the fixed grammar."""

    token: str
    kind: str  # "month", "quarter" or "year"
    year: int | None  # None: the token names no year
    index: int  # the month (1-12) or quarter (1-4); 1 for a year

    @property
    def dated(self) -> bool:
        return self.year is not None

    @property
    def order(self) -> tuple[int, int]:
        return (self.year or 0, self.index)

    @property
    def value(self) -> Any:
        """What the period column holds: the date the period starts, or the token as written."""
        if self.year is None:
            return self.token
        first_month = {"month": self.index, "quarter": 3 * self.index - 2}.get(self.kind, 1)
        return pd.Timestamp(year=self.year, month=first_month, day=1)


def parse_period(token: str, year: int | None = None) -> Period | None:
    """`token` read by the grammar in the module docstring, or None when it is not a period.

    `year` is the year the person gave for months or quarters that name none (`Jan`, `Q1`): they are then dated
    like `2024_Jan`. It is never applied to a token that names its own year.
    """
    year_given = year
    for kind, dated, pattern in _TOKENS:
        match = pattern.fullmatch(token)
        if match is None:
            continue
        parts = match.groupdict()
        year = int(parts["y"]) if dated and parts.get("y") else None
        if parts.get("mn"):
            index = _MONTHS[str(parts["mn"]).casefold()]
        elif parts.get("m"):
            index = int(parts["m"])
        elif parts.get("q"):
            index = int(parts["q"])
        else:
            index = 1
        period = Period(token=token, kind=kind, year=year, index=index)
        return replace(period, year=year_given) if period.year is None and year_given else period
    return None


def _split(name: str, pattern: str) -> tuple[str, str] | None:
    """(measure, period token) when `name` fits `pattern`, else None."""
    regex = _PERIOD_FIRST if pattern.startswith("{period}") else _PERIOD_LAST
    match = regex.fullmatch(name)
    if match is None or match.group("sep") != _separator(pattern):
        return None
    return match.group("measure"), match.group("period")


def _separator(pattern: str) -> str:
    return pattern.replace("{measure}", "").replace("{period}", "")


@dataclass(frozen=True)
class ValueColumn:
    column: str
    measure: str
    period: Period


@dataclass(frozen=True)
class UnpivotSpec:
    """An `unpivot` step's parameters, checked."""

    key: str
    id_columns: tuple[str, ...]
    value_columns: tuple[ValueColumn, ...]
    name_pattern: str
    period_column: str
    outcome: str | None
    year: int | None = None
    """The year the person gave for months or quarters whose names have none; `None` when the names carry it."""

    @property
    def measures(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(v.measure for v in self.value_columns))

    @property
    def periods(self) -> tuple[Period, ...]:
        """Every listed period once, in period order (then as written, for two spellings of one period)."""
        seen: dict[str, Period] = {}
        for value in self.value_columns:
            seen.setdefault(value.period.token, value.period)
        return tuple(sorted(seen.values(), key=lambda p: (p.order, p.token)))

    @property
    def reads(self) -> tuple[str, ...]:
        """Every column the step needs in the file (the outcome is optional: scoring files lack it)."""
        return tuple(dict.fromkeys([*self.id_columns, *(v.column for v in self.value_columns)]))

    def output_columns(self, *, with_outcome: bool) -> tuple[str, ...]:
        names = [*self.id_columns]
        if with_outcome and self.outcome is not None:
            names.append(self.outcome)
        return (*names, self.period_column, *self.measures)

    def unlisted(self, columns: Iterable[str]) -> tuple[str, ...]:
        """Columns of a file that fit the pattern for a listed measure and kind of period but are not
        listed: a period the step was not approved with (next month's new column)."""
        listed = set(self.reads) | {self.outcome}
        kind = self.value_columns[0].period
        measures = set(self.measures)
        found: list[str] = []
        for name in columns:
            if name in listed:
                continue
            parts = _split(name, self.name_pattern)
            if parts is None or parts[0] not in measures:
                continue
            period = parse_period(parts[1], self.year)
            if period is not None and (period.kind, period.dated) == (kind.kind, kind.dated):
                found.append(name)
        return tuple(found)

    @classmethod
    def from_params(cls, key: str, params: Mapping[str, Any]) -> UnpivotSpec:
        """Check `params`; raises `UnpivotError("RECIPE_STEP_INVALID", …)` naming what is wrong."""

        def refuse(what: str) -> UnpivotError:
            return UnpivotError("RECIPE_STEP_INVALID", f"Turning columns into rows: {what}")

        unknown = set(params) - _PARAMS
        if unknown:
            raise refuse(f"unknown parameter {sorted(unknown)[0]!r}.")
        pattern = params.get("name_pattern")
        if pattern not in NAME_PATTERNS:
            raise refuse(
                "the name pattern must be a measure and a period joined by '_', '-', '.' or a space."
            )
        ids = params.get("id_columns")
        if not (
            isinstance(ids, list)
            and 0 < len(ids) <= MAX_ID_COLUMNS
            and all(isinstance(c, str) and c for c in ids)
            and len(set(ids)) == len(ids)
        ):
            raise refuse(f"list 1 to {MAX_ID_COLUMNS} different columns to keep on every row.")
        if key not in ids:
            raise refuse(f"the ID column '{key}' must be one of the columns kept on every row.")
        outcome = params.get("outcome")
        if outcome is not None and not (isinstance(outcome, str) and outcome):
            raise refuse("the outcome must be a column name or null.")
        if outcome in ids:
            raise refuse("the outcome is carried as the outcome, not as a kept column.")
        period_column = params.get("period_column")
        if not (isinstance(period_column, str) and _NAME.match(period_column)):
            raise refuse("name the new period column (a letter first, at most 128 characters).")
        year = params.get("year")
        if year is not None and not (
            isinstance(year, int) and not isinstance(year, bool) and MIN_YEAR <= year <= MAX_YEAR
        ):
            raise refuse("the year is a four-digit year such as 2024, or null.")
        raw = params.get("value_columns")
        if not (isinstance(raw, list) and 2 <= len(raw) <= MAX_VALUE_COLUMNS):
            raise refuse(f"list 2 to {MAX_VALUE_COLUMNS} columns to turn into rows.")
        values: list[ValueColumn] = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"column", "measure", "period"}:
                raise refuse("each column to turn into rows is {column, measure, period}.")
            column, measure, token = item["column"], item["measure"], item["period"]
            if not all(isinstance(v, str) and v for v in (column, measure, token)):
                raise refuse("a column, its measure and its period are text.")
            if not _NAME.match(measure):
                raise refuse(f"{measure!r} is not a usable column name.")
            period = parse_period(token)
            if period is None:
                raise refuse(f"{token!r} is not a period this step reads (a month, a quarter or a year).")
            if year is not None:
                if period.dated:
                    raise refuse(f"'{column}' already names its year, so no year is given for the step.")
                period = parse_period(token, year) or period
            if pattern.replace("{measure}", measure).replace("{period}", token) != column:
                raise refuse(f"'{column}' is not '{measure}' and '{token}' joined by the name pattern.")
            if column in ids or column == outcome:
                raise refuse(
                    f"'{column}' is the ID, the outcome or a kept column, so it is never turned into rows."
                )
            values.append(ValueColumn(column=column, measure=measure, period=period))
        if len({v.column for v in values}) != len(values):
            raise refuse("a column is listed twice.")
        if len({(v.measure, v.period.order) for v in values}) != len(values):
            raise refuse("two columns hold the same measure for the same period.")
        if len({(v.period.kind, v.period.dated) for v in values}) != 1:
            raise refuse(
                "every column must have the same kind of period (all months, all quarters or all years)."
            )
        spec = cls(
            key=key,
            id_columns=tuple(ids),
            value_columns=tuple(values),
            name_pattern=str(pattern),
            period_column=period_column,
            outcome=outcome,
            year=year,
        )
        if len({p.order for p in spec.periods}) < 2:
            raise refuse("the columns must cover at least two periods; one period is not a wide sheet.")
        names = spec.output_columns(with_outcome=True)
        if len(set(names)) != len(names):
            raise refuse("two columns of the result would have the same name.")
        return spec


@dataclass(frozen=True)
class UnpivotResult:
    frame: pd.DataFrame
    rows_in: int
    rows_out: int
    failed: int
    failed_examples: tuple[str, ...]


def unpivot(
    frame: pd.DataFrame, *, key: str, params: Mapping[str, Any], max_failure_pct: float
) -> UnpivotResult:
    """One row per input row and listed period, by the step's frozen parameters. `frame` is not modified."""
    spec = UnpivotSpec.from_params(key, params)
    for column in spec.reads:
        if column not in frame.columns:
            raise UnpivotError("RECIPE_COLUMN_MISSING", f"The file has no column '{column}'.", column=column)
    unlisted = spec.unlisted(str(c) for c in frame.columns)
    failed = int(sum(int(frame[c].notna().sum()) for c in unlisted))
    present = int(sum(int(frame[v.column].notna().sum()) for v in spec.value_columns))
    if failed and failed / (present + failed) * 100.0 > max_failure_pct:
        shown = ", ".join(f"'{masked_cut(name, 60)}'" for name in unlisted[:5])
        raise UnpivotError(
            "RECIPE_VALUES_UNCONVERTED",
            f"{failed} of {present + failed} values are in columns the step was not approved with ({shown}), "
            f"more than the {max_failure_pct:g}% limit. A new period needs a new setup: start Guided setup "
            "again on this file.",
            column=unlisted[0],
        )
    kept = [
        *spec.id_columns,
        *([spec.outcome] if spec.outcome is not None and spec.outcome in frame.columns else []),
    ]
    # One block of rows per period. Two spellings of one period (`Jan_spend`, `jan_calls`) are one period,
    # written as its first spelling in period order; `from_params` refuses one measure twice in a period.
    by_part = {(v.measure, v.period.order): v.column for v in spec.value_columns}
    blocks: list[pd.DataFrame] = []
    for position, period in enumerate(spec.periods):
        if any(earlier.order == period.order for earlier in spec.periods[:position]):
            continue
        block = frame[kept].copy()
        block[spec.period_column] = period.value
        for measure in spec.measures:
            source = by_part.get((measure, period.order))
            block[measure] = frame[source] if source is not None else np.nan  # no cell for it: empty
        block["__position__"] = np.arange(len(frame))
        block["__period__"] = position
        blocks.append(block)
    out = pd.concat(blocks, ignore_index=True)
    out = out.sort_values(["__position__", "__period__"], kind="stable")
    out = out.drop(columns=["__position__", "__period__"]).reset_index(drop=True)
    if spec.periods[0].dated:
        out[spec.period_column] = pd.to_datetime(out[spec.period_column]).astype("datetime64[ns]")
    return UnpivotResult(
        frame=out,
        rows_in=len(frame),
        rows_out=len(out),
        failed=failed,
        failed_examples=tuple(masked_cut(name, 60) for name in unlisted[:5]),
    )


# ---------------------------------------------------------------------------
# Detection (the advisor calls this on the column names; nothing reads a cell)
# ---------------------------------------------------------------------------
_KIND_WORDS: Final[Mapping[str, str]] = {"month": "month", "quarter": "quarter", "year": "year"}


@dataclass(frozen=True)
class WideGroup:
    """Columns that hold the same measures for different periods, read by one name pattern."""

    name_pattern: str
    kind: str  # "month", "quarter" or "year"
    dated: bool
    value_columns: tuple[tuple[str, str, str], ...]  # (column, measure, period token)

    @property
    def measures(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(measure for _, measure, _ in self.value_columns))

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(column for column, _, _ in self.value_columns)

    def params(
        self,
        *,
        key: str,
        id_columns: Sequence[str],
        outcome: str | None,
        taken: Iterable[str],
        year: int | None = None,
    ) -> dict[str, Any]:
        """The `unpivot` parameters for this group; the period column is named after the kind of period."""
        used = {name.casefold() for name in (*taken, *self.measures)}
        period_column = _KIND_WORDS[self.kind]
        for candidate in (period_column, "period", *(f"period_{n}" for n in range(2, 100))):
            if candidate.casefold() not in used:
                period_column = candidate
                break
        return {
            "id_columns": [key, *(c for c in id_columns if c != key)],
            "value_columns": [{"column": c, "measure": m, "period": p} for c, m, p in self.value_columns],
            "name_pattern": self.name_pattern,
            "period_column": period_column,
            "outcome": outcome,
            **({"year": year} if year is not None else {}),
        }


def find_wide_columns(columns: Sequence[str], *, skip: Iterable[str] = ()) -> WideGroup | None:
    """The largest group of columns that repeat a measure over periods, or None.

    A measure counts only when it appears for at least two different periods (one month's column is
    not a wide sheet); columns whose other part is not a period (`total_spend`, `avg_spend`) never
    match. A measure whose name is already a column of the file is left out, so turning the group into
    rows never overwrites a column. Among several patterns (`spend_jan` beside `feb_calls`), the group
    with the most columns wins, then the first pattern in a fixed order.
    """
    skipped = set(skip)
    existing = {str(c).casefold() for c in columns}
    groups: dict[tuple[str, str, bool], dict[str, dict[tuple[int, int], tuple[str, str]]]] = {}
    for name in (str(c) for c in columns):
        if name in skipped:
            continue
        for first in (True, False):
            match = (_PERIOD_FIRST if first else _PERIOD_LAST).fullmatch(name)
            if match is None:
                continue
            measure, token, sep = match.group("measure"), match.group("period"), match.group("sep")
            period = parse_period(token)
            if period is None or parse_period(measure) is not None or measure.casefold() in existing:
                continue
            if not _NAME.match(measure) or measure[-1] in _SEPARATORS:
                continue
            pattern = f"{{period}}{sep}{{measure}}" if first else f"{{measure}}{sep}{{period}}"
            per_measure = groups.setdefault((pattern, period.kind, period.dated), {}).setdefault(measure, {})
            per_measure.setdefault(period.order, (name, token))
    best: WideGroup | None = None
    for (pattern, kind, dated), measures in sorted(groups.items()):
        chosen = [
            (column, measure, token)
            for measure, periods in measures.items()
            if len(periods) >= 2
            for _, (column, token) in sorted(periods.items())
        ]
        if not chosen:
            continue
        group = WideGroup(name_pattern=pattern, kind=kind, dated=dated, value_columns=tuple(chosen))
        if best is None or len(group.value_columns) > len(best.value_columns):
            best = group
    return best
