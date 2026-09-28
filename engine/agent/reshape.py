"""Level 3, many rows per entity (Plan G M76): plan and run a `combine_rows` recipe step.

A file with several rows per customer - an order log, a monthly statement - cannot train a model that
scores one row per customer. `combine_rows` turns it into one row per entity, and this module is the
only code that does it. It writes no aggregator of its own: the step's features are compiled to
onboarding `FeatureDef`s and computed by the onboarding feature engine
(`engine.onboarding.features.build_features`), whose every query carries the point-in-time guard and
is re-read by `assert_point_in_time` before it runs.

**The rows.** The file's own rows are the events of one role (`events`): the entity key is the step's
column, the event time is `time_column`. A row with no key or no readable date belongs to no
entity at no time; it is counted as a failure and left out, and more failures than the use case's
`max_conversion_failure_pct` stop the step.

**The snapshot.** Each entity is described as of one date, its *snapshot*: the latest value of
`snapshot_column` among its rows, or - when the file has no separate snapshot column - the latest
`time_column`. Only rows dated on or before the snapshot are counted (`<=`, the onboarding guard with
`inclusive_snapshot_time: true`), so an order placed after the snapshot, which is exactly the kind of
row that contains the answer, never reaches a feature. Without a separate snapshot column every row
is on or before its entity's snapshot by construction, and the file must hold only what was known
when the outcome started to be measured; the proposal says so.

**The outcome.** The outcome is read, not aggregated: it is the value on the entity's latest row by
snapshot date, ties broken by the time column and then by position in the file, empty values skipped.
A scoring file usually has no outcome column; then the combined rows have none either.

**Features.** The parameters freeze a list of `{name, function, column}` when the user approves the
step, so next month's file is combined into exactly the same columns (DEC-1006). `plan_combine`
chooses them by type: numbers get `sum`, `mean`, `max` and `latest`; categories `latest` (not for free
text) and `nunique`; an ID-like column (an order number) `nunique` only; another date column `days_since_last` (days from its latest value to
the snapshot; a value after the snapshot is not known at the snapshot, so it is left empty); the
event time `days_since_first`, and `days_since_last` when the snapshot is a separate column (with
one date column it would be 0 on every row); plus `row_count`. Consent, opt-out and last-contact
columns are carried under their own name as their latest value; personal-data columns are left out.

**Determinism.** DuckDB runs single-threaded here, so the same rows give the same values in the same
order, ties in `latest` included; the output is sorted by entity key.

**The leak check.** On a training file the full future-data check of onboarding ruling R1 runs
(`engine.onboarding.build._run_leak_probe` with `full=True`: every entity rebuilt with real rows
re-dated after the last snapshot, and nothing may move); on a scoring file the narrow one. A preview
runs neither.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import pandas as pd

from engine.agent.formats import date_order, parse_dates
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

COMBINE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"count", "sum", "mean", "max", "min", "latest", "nunique", "days_since_first", "days_since_last"}
)
_NUMERIC: Final[frozenset[str]] = frozenset({"sum", "mean", "max", "min"})
_ROW_FUNCTIONS: Final[frozenset[str]] = frozenset({"count"})
_EVENT_TIME_FUNCTIONS: Final[frozenset[str]] = frozenset({"days_since_first", "days_since_last"})
_FEATURE_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .\-]{0,127}$")
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
    dayfirst: bool
    outcome: str | None
    features: tuple[CombineFeature, ...]

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
        if not isinstance(dayfirst, bool):
            raise refuse("dayfirst must be true or false.")
        raw = params.get("features")
        if not isinstance(raw, list) or not raw:
            raise refuse("list the columns to build.")
        features: list[CombineFeature] = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"name", "function", "column"}:
                raise refuse("each feature is {name, function, column}.")
            name, function, column = item["name"], item["function"], item["column"]
            if not isinstance(name, str) or _FEATURE_NAME.match(name) is None:
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

    add(ROW_COUNT, "count", None)
    stem = _safe(time_column)
    add(f"{stem}_days_since_first", "days_since_first", None)
    if snapshot_column is not None:
        add(f"{stem}_days_since_last", "days_since_last", None)
    for raw in frame.columns:
        name = str(raw)
        if name in skip:
            continue
        profiled = columns.get(name)
        if name in carry:
            add(name, "latest", name)
            continue
        if profiled is not None and profiled.pii_kinds:
            continue  # personal data is never a signal; the engine would hide it anyway
        series = frame[name]
        stem = _safe(name)
        if profiled is not None and profiled.looks_like_id:
            add(f"{stem}_nunique", "nunique", name)  # an ID (an order number) is counted, never added up
        elif pd.api.types.is_bool_dtype(series) or (
            pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_datetime64_any_dtype(series)
        ):
            for function in ("sum", "mean", "max", "latest"):
                add(f"{stem}_{function}", function, name)
        elif pd.api.types.is_datetime64_any_dtype(series) or (
            profiled is not None and profiled.inferred_type in _DATE_TYPES
        ):
            add(f"{stem}_days_since_last", "days_since_last", name)
        else:
            if not (profiled is not None and profiled.inferred_type is ColumnType.TEXT):
                add(f"{stem}_latest", "latest", name)
            add(f"{stem}_nunique", "nunique", name)
    dayfirst = False
    if not pd.api.types.is_datetime64_any_dtype(frame[time_column]):
        dayfirst = bool(date_order(frame[time_column].dropna().astype(str).head(5_000)))
    return {
        "time_column": time_column,
        "snapshot_column": snapshot_column,
        "dayfirst": dayfirst,
        "outcome": outcome,
        "features": [feature.as_json() for feature in features],
    }


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def _dates(series: pd.Series[Any], dayfirst: bool) -> pd.Series[Any]:
    if pd.api.types.is_datetime64_any_dtype(series):
        values = series
        if getattr(values.dt, "tz", None) is not None:
            values = values.dt.tz_localize(None)
        return values.astype("datetime64[ns]")
    return parse_dates(series, dayfirst=dayfirst).values.astype("datetime64[ns]")


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


def _event_columns(
    frame: pd.DataFrame, spec: CombineSpec, rows: pd.Series[Any], max_failure_pct: float
) -> tuple[dict[str, str], dict[str, pd.Series[Any]]]:
    """Each source column once, under a plain alias, converted for what the features do with it."""
    uses: dict[str, set[str]] = {}
    for feature in spec.features:
        if feature.column is not None:
            uses.setdefault(feature.column, set()).add(feature.function)
    aliases: dict[str, str] = {}
    values: dict[str, pd.Series[Any]] = {}
    for index, (column, functions) in enumerate(uses.items()):
        alias = f"c{index}"
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
            converted = _dates(series, spec.dayfirst)
        else:
            converted = _text(series)
        aliases[column] = alias
        values[alias] = converted.reset_index(drop=True)
    return aliases, values


def _feature_defs(spec: CombineSpec, aliases: Mapping[str, str]) -> tuple[FeatureDef, ...]:
    """The step's features as onboarding `FeatureDef`s over the `events` role, named `f0`, `f1`, …"""
    defs: list[FeatureDef] = []
    for index, feature in enumerate(spec.features):
        column = None if feature.column is None else aliases[feature.column]
        if feature.function == "days_since_last" and column is not None:
            # Another date column: its latest value, turned into days before the snapshot afterwards.
            agg = AggFunction.MAX
        else:
            agg = AggFunction(feature.function)
        defs.append(FeatureDef(name=f"f{index}", role=_ROLE, function=agg, column=column))
    return tuple(defs)


def _outcome(
    frame: pd.DataFrame, spec: CombineSpec, event_time: pd.Series[Any], snapshot_at: pd.Series[Any]
) -> pd.Series[Any]:
    """Entity key → outcome on its latest row (by snapshot, then time, then file position)."""
    assert spec.outcome is not None
    rows = pd.DataFrame(
        {
            "k": frame[spec.key].to_numpy(),
            "s": snapshot_at.to_numpy(),
            "t": event_time.to_numpy(),
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
    event_time = _dates(frame[spec.time_column], spec.dayfirst)
    snapshot_at = (
        event_time if spec.snapshot_column is None else _dates(frame[spec.snapshot_column], spec.dayfirst)
    )
    has_key = frame[key].notna()
    usable = has_key & event_time.notna()
    failed = int((~usable).sum())
    if len(frame) and failed / len(frame) * 100.0 > max_failure_pct:
        raise CombineError(
            "RECIPE_VALUES_UNCONVERTED",
            f"{failed} of {len(frame)} rows have no '{key}' or no readable date in '{spec.time_column}' "
            f"({failed / len(frame):.1%}), more than the {max_failure_pct:g}% limit.",
            column=spec.time_column,
        )
    dated = has_key & snapshot_at.notna()
    snapshots = (
        pd.DataFrame(
            {"entity_key": frame.loc[dated, key].to_numpy(), "snapshot_date": snapshot_at[dated].to_numpy()}
        )
        .groupby("entity_key", sort=True)["snapshot_date"]
        .max()
        .reset_index()
    )
    aliases, values = _event_columns(frame, spec, usable, max_failure_pct)
    events = pd.DataFrame(
        {
            "entity_key": frame.loc[usable, key].to_numpy(),
            "event_time": event_time[usable].to_numpy(),
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
        answers = _outcome(frame, spec, event_time, snapshot_at)
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
