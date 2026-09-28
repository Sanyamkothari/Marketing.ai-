"""Level 3, many rows per entity (Plan G M76): plan and run a `combine_rows` recipe step.

A file with several rows per customer - an order log, a monthly statement - cannot train a model that
scores one row per customer. `combine_rows` turns it into one row per entity, and this module is the
only code that does it. It writes no aggregator of its own: the step's features are compiled to
onboarding `FeatureDef`s and computed by the onboarding feature engine
(`engine.onboarding.features.build_features`), whose every query carries the point-in-time guard and
is re-read by `assert_point_in_time` before it runs.

**Entity-wise.** An entity's output depends only on its own rows and the step's frozen parameters,
never on other entities or a statistic of the whole file (DEC-1023): the preview (the first 1,000
entities), the Approve on the full file and a scoring file split differently read the same entity
the same way. Every rule below keeps to that.

**The rows.** The file's own rows are the events of one role (`events`): the entity key is the step's
column, the event time is `time_column`. A row with no key or no readable date, or whose entity has
no readable snapshot on any of its rows, belongs to no entity at no time; it is counted as a failure
and left out, and more failures than the use case's `max_conversion_failure_pct` stop the step. A
row with a blank snapshot of its own still counts when another row of its entity has one. An
untyped (object) key column is read as text on every row, so the number 7 and the text `"7"` (a CSV
read in chunks) are one entity, and a key's form never depends on another entity's rows.

**Dates.** A value with a UTC offset, or a time-zone-aware column, is converted to UTC before its
zone is dropped, so `12:00+05:00` is before `09:00Z`; a value without one is kept as written, and a
zoned value that cannot be read in UTC is empty rather than read at its clock time. A date outside
pandas' range (the `9999-12-31` "no end" sentinel) is an empty date. The day/month order of every
date column is frozen per column at planning (`dayfirst`: a map of column to true, false or null):
the order its own values prove (a first part above 12 means day first), else the order the file's
date columns prove when they agree, else null. The combine only reads it, each value on its own, so
a value such as `04/25/2024` in a day-first column still reads the only way it can. A column whose
order is null and which holds a value that reads differently either way stops the step with
`RECIPE_VALUES_UNCONVERTED` instead of being guessed. A single `dayfirst` (true, false or null)
applies to every date column.

**The snapshot.** Each entity is described as of one date, its *snapshot*: the latest value of
`snapshot_column` among its rows, or - when the file has no separate snapshot column - the latest
`time_column`. Only rows dated on or before the snapshot are counted (`<=`, the onboarding guard with
`inclusive_snapshot_time: true`), so an order placed after the snapshot, which is exactly the kind of
row that contains the answer, never reaches a feature. Without a separate snapshot column every row
is on or before its entity's snapshot by construction, and the file must hold only what was known
when the outcome started to be measured; the proposal says so.

**The outcome.** The outcome is read, not aggregated: it is the value on the entity's latest row by
snapshot date, ties broken by the time column and then by position in the file, empty values skipped.
`latest` is the value on the latest row by the time column; between rows of the same date it takes
the later snapshot, then the later row in the file, so a same-date tie goes to the row the outcome
would read. (The two are different rules: across dates, `latest` follows the time column and the
outcome the snapshot.) A scoring file usually has no outcome column; then the combined rows have
none either.

**Features.** The parameters freeze a list of `{name, function, column}` when the user approves the
step, so next month's file is combined into exactly the same columns (DEC-1006). `plan_combine`
chooses them by type: numbers get `sum`, `mean`, `max` and `latest`; categories `latest` (not for free
text) and `nunique`; an ID-like column (an order number) `nunique` only; another date column
`days_since_last` (days from its latest value on or before the snapshot to the snapshot; a value
after the snapshot is not known at the snapshot, so it is ignored). A date column that already holds
a value after its entity's snapshot on a row dated on or before it was written after the snapshot -
a "last order date" overwritten by the export - so whether it is empty would give the answer away:
the plan leaves it out, as it leaves out a date column whose order is null and needed. That rule
also leaves out an honest per-event date such as a refund date that fell after the snapshot; the
runtime masking would make it safe, so this is conservative. On a training file (the full leak
check) a kept date column found written after the snapshot in this file stops the step with
`FUTURE_EVENTS_LEAKED`, since the leak probe re-dates only the time column and cannot see it. The
event time `days_since_first`, and `days_since_last` when the snapshot is a separate column (with
one date column it would be 0 on every row); plus `row_count`. Consent, opt-out and last-contact
columns are carried under their own name (whatever its spelling) as their latest value;
personal-data columns are left out.

**Determinism.** DuckDB runs single-threaded here, so the same rows give the same values in the same
order; ties in `latest` are settled before DuckDB sees them (only one row of a same-date tie keeps
its value in the column `latest` reads). The output is sorted by entity key.

**The leak check.** On a training file the full future-data check of onboarding ruling R1 runs
(`engine.onboarding.build._run_leak_probe` with `full=True`: every entity rebuilt with real rows
re-dated after the last snapshot, and nothing may move); on a scoring file the narrow one. A preview
runs neither.
"""

from __future__ import annotations

import numbers
import re
import threading
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np
import pandas as pd

from engine.agent.formats import _NUMERIC_DAY_MONTH, parse_dates
from engine.config import AggFunction, ColumnType, FeatureDef
from engine.contracts import DatasetProfile

__all__ = [
    "COMBINE_FUNCTIONS",
    "ROW_COUNT",
    "CombineError",
    "CombineFeature",
    "CombineResult",
    "CombineSpec",
    "LeakCheck",
    "choose_dates",
    "combine_rows",
    "plan_combine",
]

LeakCheck = Literal["full", "narrow"]

ROW_COUNT: Final[str] = "row_count"
"""The name of the feature that counts an entity's rows on or before its snapshot."""

_ROLE: Final[str] = "events"
_ENTITY_ROLE: Final[str] = "__combine_entity__"  # no view has it: the probe re-dates every event role

_SINGLE_THREAD_LOCK: Final[threading.Lock] = threading.Lock()


@contextmanager
def _single_threaded_duckdb() -> Iterator[None]:
    """Every DuckDB connection opened inside the block runs on one thread (M77 hardening).

    The combine build runs single-threaded so `latest` breaks ties between rows of the same date the
    same way every time. Onboarding's leak probe opens its own connection with DuckDB's default
    thread count; above a few hundred thousand rows DuckDB then scans in parallel, breaks those ties
    differently, and the probe reports a value that "moved" - a false `FUTURE_EVENTS_LEAKED` that
    refused every realistically sized order log at Approve. The probe takes no connection, so for its
    duration `duckdb.connect` hands out single-threaded connections. The lock keeps two probes from
    interleaving the swap; another thread connecting meanwhile only runs slower. The lasting fix is
    for the probe to accept a connection or a thread count (a request to the onboarding owners).
    """
    import duckdb

    with _SINGLE_THREAD_LOCK:
        real = duckdb.connect

        def connect(*args: Any, **kwargs: Any) -> Any:
            con = real(*args, **kwargs)
            con.execute("SET threads TO 1")
            return con

        duckdb.connect = connect
        try:
            yield
        finally:
            duckdb.connect = real


COMBINE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"count", "sum", "mean", "max", "min", "latest", "nunique", "days_since_first", "days_since_last"}
)
_NUMERIC: Final[frozenset[str]] = frozenset({"sum", "mean", "max", "min"})
_ROW_FUNCTIONS: Final[frozenset[str]] = frozenset({"count"})
_EVENT_TIME_FUNCTIONS: Final[frozenset[str]] = frozenset({"days_since_first", "days_since_last"})
_FEATURE_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .\-]{0,127}$")
_ZONED: Final[re.Pattern[str]] = re.compile(
    r"\d:\d{2}(?::\d{2}(?:[.,]\d+)?)?\s*(?:Z|UTC|GMT|[+-]\d{2}(?::?\d{2})?)$", re.IGNORECASE
)
"""A clock time followed by a zone: `12:00+05:00`, `09:00:00Z`, `10:30 UTC`."""
_ONE_KIND: Final[frozenset[str]] = frozenset(
    {"empty", "string", "bytes", "integer", "floating", "decimal", "boolean", "datetime", "date"}
)
"""`pandas.api.types.infer_dtype` answers for a column whose values are all of one kind."""
_SNAPSHOT_NAME: Final[re.Pattern[str]] = re.compile(r"snapshot|(?:^|_)as_?of(?:_|$)", re.IGNORECASE)
"""`snapshot_date`, `SnapshotDate`, `as_of`, `asof_date` - but not `has_offer`."""
_PARAMS: Final[frozenset[str]] = frozenset(
    {"time_column", "snapshot_column", "dayfirst", "outcome", "features"}
)


class CombineError(Exception):
    """A combine that cannot run on this file; `code` is a RECIPE_* code the recipe engine passes on."""

    def __init__(self, code: str, message: str, *, column: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.column = column


@dataclass(frozen=True)
class CombineFeature:
    """One output column: `function` over `column` (null for a row count or the event time)."""

    name: str
    function: str
    column: str | None

    def as_json(self) -> dict[str, str | None]:
        return {"name": self.name, "function": self.function, "column": self.column}


@dataclass(frozen=True)
class CombineSpec:
    """A `combine_rows` step's parameters, checked."""

    key: str
    time_column: str
    snapshot_column: str | None
    dayfirst: bool | Mapping[str, bool | None] | None  # per date column; null: nothing proved it
    outcome: str | None
    features: tuple[CombineFeature, ...]

    def order(self, column: str) -> bool | None:
        """The frozen day/month order of `column` (True: day first); None when it was not decided."""
        if isinstance(self.dayfirst, Mapping):
            return self.dayfirst.get(column)
        return self.dayfirst

    @property
    def snapshot_output(self) -> str:
        """The output column holding each entity's snapshot date."""
        return self.snapshot_column or self.time_column

    @property
    def reads(self) -> tuple[str, ...]:
        """Every column the step needs in the file (the outcome is optional: scoring files lack it)."""
        needed = [self.key, self.time_column]
        if self.snapshot_column is not None:
            needed.append(self.snapshot_column)
        needed += [f.column for f in self.features if f.column is not None]
        return tuple(dict.fromkeys(needed))

    def output_columns(self, *, with_outcome: bool) -> tuple[str, ...]:
        names = [self.key, self.snapshot_output, *(f.name for f in self.features)]
        if with_outcome and self.outcome is not None:
            names.append(self.outcome)
        return tuple(names)

    @classmethod
    def from_params(cls, key: str, params: Mapping[str, Any]) -> CombineSpec:
        """Check `params`; raises `CombineError("RECIPE_STEP_INVALID", …)` naming what is wrong."""

        def refuse(what: str) -> CombineError:
            return CombineError("RECIPE_STEP_INVALID", f"Combining rows: {what}")

        unknown = set(params) - _PARAMS
        if unknown:
            raise refuse(f"unknown parameter {sorted(unknown)[0]!r}.")
        time_column = params.get("time_column")
        if not isinstance(time_column, str) or not time_column:
            raise refuse("the date column that orders the rows must be named.")
        snapshot = params.get("snapshot_column")
        if snapshot is not None and (not isinstance(snapshot, str) or not snapshot):
            raise refuse("the snapshot column must be a column name or null.")
        if snapshot == time_column:
            snapshot = None
        outcome = params.get("outcome")
        if outcome is not None and not isinstance(outcome, str):
            raise refuse("the outcome must be a column name or null.")
        dayfirst = params.get("dayfirst", False)
        if isinstance(dayfirst, Mapping):
            if not all(
                isinstance(column, str) and (order is None or isinstance(order, bool))
                for column, order in dayfirst.items()
            ):
                raise refuse("dayfirst maps each date column to true, false or null.")
            dayfirst = dict(dayfirst)
        elif dayfirst is not None and not isinstance(dayfirst, bool):
            raise refuse("dayfirst must be true, false, null or a map of date columns to one of those.")
        raw = params.get("features")
        if not isinstance(raw, list) or not raw:
            raise refuse("list the columns to build.")
        features: list[CombineFeature] = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"name", "function", "column"}:
                raise refuse("each feature is {name, function, column}.")
            name, function, column = item["name"], item["function"], item["column"]
            carried = function == "latest" and name == column  # a column kept under its own header
            if not isinstance(name, str) or not (carried or _FEATURE_NAME.match(name) is not None):
                raise refuse(f"{name!r} is not a usable column name.")
            if function not in COMBINE_FUNCTIONS:
                raise refuse(f"{function!r} is not a way of combining rows.")
            if column is not None and not isinstance(column, str):
                raise refuse(f"feature {name!r} names its column as text.")
            if function in _ROW_FUNCTIONS and column is not None:
                raise refuse(f"feature {name!r} counts rows, so it reads no column.")
            if function not in _ROW_FUNCTIONS | _EVENT_TIME_FUNCTIONS and column is None:
                raise refuse(f"feature {name!r} needs the column it combines.")
            if column is not None and column in {key, time_column, snapshot, outcome}:
                raise refuse(f"feature {name!r} may not combine the ID, date, snapshot or outcome column.")
            features.append(CombineFeature(name=name, function=str(function), column=column))
        spec = cls(
            key=key,
            time_column=time_column,
            snapshot_column=snapshot,
            dayfirst=dayfirst,
            outcome=outcome,
            features=tuple(features),
        )
        names = spec.output_columns(with_outcome=True)
        if len(set(names)) != len(names):
            raise refuse("two new columns would have the same name.")
        if key in {time_column, snapshot, outcome}:
            raise refuse("the ID column cannot also be the date or the outcome.")
        if isinstance(dayfirst, dict) and not set(dayfirst) <= set(spec.reads):
            raise refuse(
                f"dayfirst names {sorted(set(dayfirst) - set(spec.reads))[0]!r}, which the step does not read."
            )
        return spec


@dataclass(frozen=True)
class CombineResult:
    frame: pd.DataFrame
    rows_in: int
    entities: int
    failed: int
    leak_check: str | None


# ---------------------------------------------------------------------------
# Planning (the advisor calls this once; the plan is frozen into the step)
# ---------------------------------------------------------------------------
_DATE_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.DATE, ColumnType.DATETIME})


def _safe(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    return text if text and not text[0].isdigit() else f"c_{text}"


def choose_dates(
    profile: DatasetProfile, *, exclude: Sequence[str], snapshot_hint: str | None
) -> tuple[str, str | None] | None:
    """(time column, separate snapshot column or None), or None when the file has no usable date.

    A date column named like a snapshot (`snapshot`, `as_of`, or the use case's split column) is the
    snapshot when another date column can order the rows; otherwise the first time-like date column
    orders the rows and each entity's latest date is its snapshot.
    """
    skipped = set(exclude)
    dates = [c.name for c in profile.columns if c.inferred_type in _DATE_TYPES and c.name not in skipped]
    if not dates:
        return None
    ranked = [name for name in profile.time_column_candidates if name in dates]
    ranked += [name for name in dates if name not in ranked]
    snapshots = [d for d in ranked if d == snapshot_hint or _SNAPSHOT_NAME.search(d)]
    if snapshots and len(ranked) > 1:
        snapshot = snapshots[0]
        return next(d for d in ranked if d != snapshot), snapshot
    return ranked[0], None


def plan_combine(
    frame: pd.DataFrame,
    profile: DatasetProfile,
    *,
    key: str,
    time_column: str,
    snapshot_column: str | None,
    outcome: str | None,
    carry: Sequence[str] = (),
) -> dict[str, Any]:
    """The `combine_rows` parameters for this file, by the rules in the module docstring."""
    columns = {c.name: c for c in profile.columns}
    skip = {key, time_column, snapshot_column, outcome}
    taken = {key, snapshot_column or time_column, *([outcome] if outcome else [])}
    features: list[CombineFeature] = []

    def add(name: str, function: str, column: str | None) -> None:
        final, n = name, 2
        while final in taken:
            final, n = f"{name}_{n}", n + 1
        taken.add(final)
        features.append(CombineFeature(name=final, function=function, column=column))

    def kind(name: str) -> str | None:
        """How the column is combined, or None when it is left out."""
        if name in skip:
            return None
        profiled = columns.get(name)
        if name in carry:
            return "carry"
        if profiled is not None and profiled.pii_kinds:
            return None  # personal data is never a signal; the engine would hide it anyway
        series = frame[name]
        if profiled is not None and profiled.looks_like_id:
            return "id"
        if pd.api.types.is_bool_dtype(series) or (
            pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_datetime64_any_dtype(series)
        ):
            return "number"
        if pd.api.types.is_datetime64_any_dtype(series) or (
            profiled is not None and profiled.inferred_type in _DATE_TYPES
        ):
            return "date"
        if profiled is not None and profiled.inferred_type is ColumnType.TEXT:
            return "text"
        return "category"

    kinds = {str(raw): kind(str(raw)) for raw in frame.columns}
    dates = [time_column, *([snapshot_column] if snapshot_column is not None else [])]
    others = [name for name, how in kinds.items() if how == "date"]
    orders, left_out = _plan_dates(frame, key, dates, others)

    add(ROW_COUNT, "count", None)
    stem = _safe(time_column)
    add(f"{stem}_days_since_first", "days_since_first", None)
    if snapshot_column is not None:
        add(f"{stem}_days_since_last", "days_since_last", None)
    for name, how in kinds.items():
        stem = _safe(name)
        if how == "carry":
            add(name, "latest", name)
        elif how == "id":
            add(f"{stem}_nunique", "nunique", name)  # an ID (an order number) is counted, never added up
        elif how == "number":
            for function in ("sum", "mean", "max", "latest"):
                add(f"{stem}_{function}", function, name)
        elif how == "date":
            if name not in left_out:
                add(f"{stem}_days_since_last", "days_since_last", name)
        elif how is not None:
            if how != "text":
                add(f"{stem}_latest", "latest", name)
            add(f"{stem}_nunique", "nunique", name)
    return {
        "time_column": time_column,
        "snapshot_column": snapshot_column,
        "dayfirst": {name: order for name, order in orders.items() if name not in left_out},
        "outcome": outcome,
        "features": [feature.as_json() for feature in features],
    }


def _plan_dates(
    frame: pd.DataFrame, key: str, dates: Sequence[str], others: Sequence[str]
) -> tuple[dict[str, bool | None], set[str]]:
    """Each date column's frozen day/month order, and the other date columns the plan leaves out.

    A column's order is the one its own values prove; else the one the file's date columns prove
    (`dates`: the time and snapshot columns; `others`: the date columns turned into days since),
    when they agree; else null. Frozen per column, so the combine never decides it again from a
    whole file (DEC-1023: an entity's output depends only on its own rows and the parameters). An
    other date column is left out when its order is null and its values need one, or when it holds
    a date after its entity's snapshot on a row dated on or before that snapshot.
    """
    found = {name: _order(frame[name]) for name in (*dates, *others)}
    proven = {order for order, _ in found.values() if order is not None}
    settled = next(iter(proven)) if len(proven) == 1 else None
    orders = {name: own if own is not None else settled for name, (own, _) in found.items()}
    left_out = {name for name in others if orders[name] is None and found[name][1]}
    remaining = [name for name in others if name not in left_out]
    if remaining:
        # An unproven time or snapshot order stops the combine itself; here any order will do.
        guessed = {name: bool(order) for name, order in orders.items()}
        line = _timeline(frame, key, dates[0], dates[1] if len(dates) > 1 else None, guessed)
        for name in remaining:
            if _written_after(line, _dates(frame[name], guessed[name], name)):
                left_out.add(name)  # written after the snapshot: its blanks would give the answer away
    return orders, left_out


# ---------------------------------------------------------------------------
# Reading keys and dates
# ---------------------------------------------------------------------------
def _key_text(value: Any) -> str:
    if isinstance(value, bool | np.bool_):
        return str(bool(value))
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, numbers.Real) and float(value).is_integer():
        return str(int(float(value)))
    return str(value)


def _keys(series: pd.Series[Any]) -> pd.Series[Any]:
    """The entity key; an untyped (object) column as text, so `7` and `"7"` are one entity.

    pandas groups the number 7 and the text "7" apart while DuckDB reads both as "7"; a CSV read in
    chunks gives exactly that mix when one chunk of an ID column is all digits. Every value of an
    object column becomes text, whatever the other rows hold, so a key never changes form because
    of another entity's rows (DEC-1023).
    """
    if series.dtype != object or pd.api.types.infer_dtype(series, skipna=True) in {"string", "empty"}:
        return series
    return series.map(_key_text, na_action="ignore")


def _distinct_text(series: pd.Series[Any]) -> tuple[pd.Series[Any], pd.Series[Any]]:
    """(each distinct non-empty value once, the same values as stripped text)."""
    distinct = pd.Series(pd.unique(series.dropna()), dtype=object)
    return distinct, distinct.astype(str).str.strip()


def _order_of(text: pd.Series[Any]) -> tuple[bool | None, bool]:
    """(the day/month order the values prove - True is day first - or None, whether a value needs it).

    The rule of `engine.agent.formats.date_order`: a first part above 12 proves day first, a second
    part above 12 month first, both at once prove nothing. `10/09/2011` needs the order to be read;
    `10/10/2011` does not.
    """
    parts = text.str.extract(_NUMERIC_DAY_MONTH).dropna()
    if parts.empty:
        return None, False
    first, second = parts[0].astype(int), parts[1].astype(int)
    day_first = bool(((first > 12) & (second <= 12)).any())
    month_first = bool(((second > 12) & (first <= 12)).any())
    needed = bool(((first <= 12) & (second <= 12) & (first != second)).any())
    if day_first != month_first:
        return day_first, needed
    return None, needed


def _order(series: pd.Series[Any]) -> tuple[bool | None, bool]:
    if pd.api.types.is_datetime64_any_dtype(series) or pd.api.types.is_numeric_dtype(series):
        return None, False
    return _order_of(_distinct_text(series)[1])


def _utc(texts: pd.Series[Any], dayfirst: bool) -> pd.Series[Any]:
    """Text with a UTC offset as naive UTC times (`datetime64[ns]`); unreadable text is NaT."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            parsed = pd.to_datetime(texts, format="mixed", dayfirst=dayfirst, utc=True, errors="coerce")
        except (ValueError, TypeError, OverflowError):
            parsed = pd.Series(
                [pd.to_datetime(text, dayfirst=dayfirst, utc=True, errors="coerce") for text in texts],
                index=texts.index,
                dtype="datetime64[ns, UTC]",
            )
    return parsed.dt.tz_localize(None).astype("datetime64[ns]")


def _dates(series: pd.Series[Any], order: bool | None, column: str) -> pd.Series[Any]:
    """`column` as naive `datetime64[ns]`: zoned values in UTC, others as written, out of range NaT.

    Text is read in the column's frozen day/month `order`, each value on its own; with no order
    frozen, a value that reads differently either way stops the step rather than being guessed. A
    zoned value that cannot be read in UTC is empty, never read at its clock time.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        values = series
        if getattr(values.dt, "tz", None) is not None:
            values = values.dt.tz_convert("UTC").dt.tz_localize(None)
        if values.dtype != np.dtype("datetime64[ns]"):  # a parquet file's datetime64[us] can hold 9999
            values = values.where((values >= pd.Timestamp.min) & (values <= pd.Timestamp.max))
        return values.astype("datetime64[ns]")
    distinct, text = _distinct_text(series)
    if order is None and not pd.api.types.is_numeric_dtype(series) and _order_of(text)[1]:
        raise CombineError(
            "RECIPE_VALUES_UNCONVERTED",
            f"The dates in '{column}' could be read day first or month first, and nothing in the file "
            "showed which when the step was planned, so they are not guessed. Export the column with "
            "the year first (2011-09-10) and upload the file again.",
            column=column,
        )
    values = parse_dates(series, dayfirst=bool(order)).values.astype("datetime64[ns]")
    zoned = text.str.contains(_ZONED)
    if not zoned.any():
        return values
    lookup = pd.Series(
        _utc(text[zoned], bool(order)).to_numpy(), index=pd.Index(distinct[zoned], dtype=object)
    )
    in_utc = pd.Series(series.map(lookup), index=series.index, dtype="datetime64[ns]")
    return values.mask(series.isin(lookup.index), in_utc)


def _numbers(series: pd.Series[Any]) -> tuple[pd.Series[Any], int]:
    """Numbers, and how many non-empty values were not numbers."""
    if pd.api.types.is_bool_dtype(series):
        return series.astype("float64"), 0
    if pd.api.types.is_numeric_dtype(series):
        return series, 0
    parsed = pd.to_numeric(series, errors="coerce")
    return parsed, int((series.notna() & parsed.isna()).sum())


def _text(series: pd.Series[Any]) -> pd.Series[Any]:
    return series.astype("string")


@dataclass(frozen=True)
class _Timeline:
    """Each row's entity key, date, snapshot and its entity's snapshot, and whether it is usable."""

    keys: pd.Series[Any]
    event_time: pd.Series[Any]
    snapshot_at: pd.Series[Any]
    known_by: pd.Series[Any]
    """The row's entity snapshot: the latest readable snapshot over the entity's rows, else NaT."""
    usable: pd.Series[Any]
    """A key, a readable date and an entity with a snapshot (not necessarily on this row)."""

    @property
    def dated(self) -> pd.Series[Any]:
        """Rows that give their entity a snapshot: a key and a readable snapshot."""
        return self.keys.notna() & self.snapshot_at.notna()


def _timeline(
    frame: pd.DataFrame,
    key: str,
    time_column: str,
    snapshot_column: str | None,
    orders: Mapping[str, bool | None],
) -> _Timeline:
    keys = _keys(frame[key])
    event_time = _dates(frame[time_column], orders.get(time_column), time_column)
    snapshot_at = (
        event_time
        if snapshot_column is None
        else _dates(frame[snapshot_column], orders.get(snapshot_column), snapshot_column)
    )
    dated = keys.notna() & snapshot_at.notna()
    latest = snapshot_at[dated].groupby(keys[dated]).max()
    known_by = pd.Series(keys.where(keys.notna()).map(latest), index=keys.index, dtype="datetime64[ns]")
    usable = keys.notna() & event_time.notna() & known_by.notna()
    return _Timeline(keys, event_time, snapshot_at, known_by, usable)


def _written_after(line: _Timeline, values: pd.Series[Any]) -> bool:
    """Whether a date column holds a date after its entity's snapshot on a row counted at it."""
    counted = line.usable & (line.event_time <= line.known_by)
    return bool((counted & (values > line.known_by)).any())


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def _last_of_ties(values: pd.Series[Any], group: pd.Series[Any], snapshot: pd.Series[Any]) -> pd.Series[Any]:
    """`values` with one known value kept per (entity, date) group, for `latest`.

    The kept value is from the row with the latest snapshot, then the later row in the file: among
    rows of one date, the order the outcome also uses (`_outcome`).
    """
    known = values.notna().to_numpy()
    rows = pd.DataFrame({"g": group.to_numpy(), "s": snapshot.to_numpy(), "pos": np.arange(len(values))})
    rows = rows[known].sort_values(["s", "pos"], kind="stable", na_position="first")
    kept = rows["pos"][~rows["g"].duplicated(keep="last")].to_numpy()
    earlier = np.ones(len(values), dtype=bool)
    earlier[kept] = False
    return values.mask(earlier)


def _event_columns(
    frame: pd.DataFrame, spec: CombineSpec, line: _Timeline, max_failure_pct: float, *, guard: bool
) -> tuple[dict[tuple[str, str], str], dict[str, pd.Series[Any]]]:
    """Each source column under a plain alias, converted for what the features do with it.

    A column is read under up to three aliases: as it is (`all`), for `latest` with same-date ties
    settled (`latest`, see `_last_of_ties`), and for another date's `days_since_last` with the dates
    after the entity's snapshot emptied (`known`). With `guard` (a training file's full leak check),
    a date column that holds a date after its entity's snapshot on a row counted at it stops the
    step: the plan left such columns out, and this file's copy was overwritten after the snapshot.
    """
    rows = line.usable
    uses: dict[str, set[str]] = {}
    for feature in spec.features:
        if feature.column is not None:
            uses.setdefault(feature.column, set()).add(feature.function)
    aliases: dict[tuple[str, str], str] = {}
    values: dict[str, pd.Series[Any]] = {}
    group: pd.Series[Any] | None = None

    def put(column: str, how: str, series: pd.Series[Any]) -> None:
        alias = f"c{len(values)}"
        aliases[(column, how)] = alias
        values[alias] = series

    for column, functions in uses.items():
        series = frame.loc[rows, column]
        if functions & _NUMERIC or (
            "latest" in functions and pd.api.types.is_numeric_dtype(frame[column].dtype)
        ):
            converted, bad = _numbers(series)
            present = int(series.notna().sum())
            if present and bad / present * 100.0 > max_failure_pct:
                raise CombineError(
                    "RECIPE_VALUES_UNCONVERTED",
                    f"{bad} of {present} values in '{column}' are not numbers, so they cannot be added up "
                    f"({bad / present:.1%}), more than the {max_failure_pct:g}% limit.",
                    column=column,
                )
        elif "days_since_last" in functions:
            every = _dates(frame[column], spec.order(column), column)
            if guard and _written_after(line, every):
                raise CombineError(
                    "RECIPE_STEP_INVALID",
                    f"FUTURE_EVENTS_LEAKED: '{column}' holds dates after the snapshot on rows dated "
                    "before it, so it was written after the snapshot and whether it is empty could give "
                    "the answer away. It was not like this when the step was planned; start Guided setup "
                    "again on this file.",
                    column=column,
                )
            converted = every[rows]
        else:
            converted = _text(series)
        converted = converted.reset_index(drop=True)
        if functions - {"latest", "days_since_last"}:
            put(column, "all", converted)
        if "latest" in functions:
            if group is None:
                ties = pd.DataFrame({"k": line.keys[rows].to_numpy(), "t": line.event_time[rows].to_numpy()})
                group = pd.Series(ties.groupby(["k", "t"], sort=False).ngroup().to_numpy())
            put(column, "latest", _last_of_ties(converted, group, line.snapshot_at[rows]))
        if "days_since_last" in functions:
            known = converted
            if pd.api.types.is_datetime64_any_dtype(converted):
                known = converted.mask(converted > line.known_by[rows].reset_index(drop=True))
            put(column, "known", known)
    return aliases, values


def _alias_of(feature: CombineFeature) -> tuple[str, str] | None:
    if feature.column is None:
        return None
    if feature.function == "latest":
        return feature.column, "latest"
    if feature.function == "days_since_last":
        return feature.column, "known"
    return feature.column, "all"


def _feature_defs(spec: CombineSpec, aliases: Mapping[tuple[str, str], str]) -> tuple[FeatureDef, ...]:
    """The step's features as onboarding `FeatureDef`s over the `events` role, named `f0`, `f1`, …"""
    defs: list[FeatureDef] = []
    for index, feature in enumerate(spec.features):
        which = _alias_of(feature)
        column = None if which is None else aliases[which]
        if feature.function == "days_since_last" and column is not None:
            # Another date column: its latest value on or before the snapshot, in days afterwards.
            agg = AggFunction.MAX
        else:
            agg = AggFunction(feature.function)
        defs.append(FeatureDef(name=f"f{index}", role=_ROLE, function=agg, column=column))
    return tuple(defs)


def _outcome(frame: pd.DataFrame, spec: CombineSpec, line: _Timeline) -> pd.Series[Any]:
    """Entity key → outcome on its latest row (by snapshot, then time, then file position)."""
    assert spec.outcome is not None
    rows = pd.DataFrame(
        {
            "k": line.keys.to_numpy(),
            "s": line.snapshot_at.to_numpy(),
            "t": line.event_time.to_numpy(),
            "y": frame[spec.outcome].to_numpy(),
            "pos": range(len(frame)),
        }
    )
    rows = rows[rows["k"].notna() & rows["s"].notna()]
    latest = rows.groupby("k", sort=False)["s"].transform("max")
    rows = rows[(rows["s"] == latest) & rows["y"].notna()]
    rows = rows.sort_values(["t", "pos"], kind="stable", na_position="first")
    return rows.groupby("k", sort=False)["y"].last()


def combine_rows(
    frame: pd.DataFrame,
    *,
    key: str,
    params: Mapping[str, Any],
    max_failure_pct: float,
    leak_check: LeakCheck | None = None,
) -> CombineResult:
    """One row per entity of `frame`, by the step's frozen parameters. `frame` is not modified."""
    import duckdb

    from engine.onboarding.build import _run_leak_probe  # the onboarding probe itself (ruling R1)
    from engine.onboarding.features import SNAPSHOT_VIEW, build_features
    from engine.onboarding.specs import FeatureSpec

    spec = CombineSpec.from_params(key, params)
    for column in spec.reads:
        if column not in frame.columns:
            raise CombineError("RECIPE_COLUMN_MISSING", f"The file has no column '{column}'.", column=column)
    line = _timeline(
        frame, key, spec.time_column, spec.snapshot_column, {name: spec.order(name) for name in spec.reads}
    )
    usable = line.usable
    failed = int((~usable).sum())
    if len(frame) and failed / len(frame) * 100.0 > max_failure_pct:
        what, column = f"no readable '{spec.time_column}'", spec.time_column
        if spec.snapshot_column is not None:
            what += f", or no readable '{spec.snapshot_column}' on any row of their '{key}'"
            unsnapshotted = line.keys.notna() & line.event_time.notna() & line.known_by.isna()
            if int(unsnapshotted.sum()) > int(line.event_time.isna().sum()):
                column = spec.snapshot_column
        raise CombineError(
            "RECIPE_VALUES_UNCONVERTED",
            f"{failed} of {len(frame)} rows have no '{key}', {what} "
            f"({failed / len(frame):.1%}), more than the {max_failure_pct:g}% limit.",
            column=column,
        )
    dated = line.dated
    snapshots = (
        pd.DataFrame(
            {"entity_key": line.keys[dated].to_numpy(), "snapshot_date": line.snapshot_at[dated].to_numpy()}
        )
        .groupby("entity_key", sort=True)["snapshot_date"]
        .max()
        .reset_index()
    )
    aliases, values = _event_columns(frame, spec, line, max_failure_pct, guard=leak_check == "full")
    events = pd.DataFrame(
        {
            "entity_key": line.keys[usable].to_numpy(),
            "event_time": line.event_time[usable].to_numpy(),
            **values,
        }
    )
    features = FeatureSpec(features=_feature_defs(spec, aliases))
    con = duckdb.connect()
    try:
        con.execute("SET threads TO 1")  # a fixed scan order: the same rows give the same values
        con.register(_ROLE, events)
        con.register(SNAPSHOT_VIEW, snapshots)
        built = build_features(con, features, inclusive=True)
    finally:
        con.close()
    summary: str | None = None
    if leak_check is not None:
        with _single_threaded_duckdb():
            probe = _run_leak_probe(
                {_ROLE: events},
                snapshots,
                features,
                built,
                entity_role=_ENTITY_ROLE,
                inclusive=True,
                full=leak_check == "full",
            )
        if probe.leaked:
            raise CombineError(
                "RECIPE_STEP_INVALID",
                "FUTURE_EVENTS_LEAKED: a combined value changed when rows dated after the snapshot were "
                "added, so the combined file could contain the answer. This is an engine fault, not a "
                "problem with the file.",
                column=key,
            )
        scope = "Full" if leak_check == "full" else "Narrow"
        summary = (
            f"{scope} future-data check: {probe.rows_probed:,} of {probe.rows_total:,} combined rows were "
            "rebuilt with rows dated after the last snapshot added, and no value changed."
        )
    out = pd.DataFrame({key: built["entity_key"], spec.snapshot_output: built["snapshot_date"]})
    for index, feature in enumerate(spec.features):
        result: pd.Series[Any] = built[f"f{index}"]
        if feature.function == "days_since_last" and feature.column is not None:
            days = (built["snapshot_date"] - result).dt.days
            result = days.where(days >= 0)  # a date after the snapshot was not known at the snapshot
        out[feature.name] = result
    if spec.outcome is not None and spec.outcome in frame.columns:
        answers = _outcome(frame, spec, line)
        mapped = out[key].map(answers)
        source = frame[spec.outcome].dtype
        if not mapped.isna().any():
            mapped = mapped.astype(source)
        elif pd.api.types.is_integer_dtype(source):
            mapped = mapped.astype("Int64")
        out[spec.outcome] = mapped
    return CombineResult(
        frame=out.reset_index(drop=True),
        rows_in=len(frame),
        entities=len(out),
        failed=failed,
        leak_check=summary,
    )
