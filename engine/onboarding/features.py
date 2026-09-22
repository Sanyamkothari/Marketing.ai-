"""The aggregation engine (M12, Phase 2 plan section 6.2): a `FeatureSpec` becomes one DuckDB query
per event role, and those queries become the feature columns of the built dataset.

**The point-in-time guard.** A feature for snapshot date T may read only events that had already
happened at T. That is the whole correctness story of this module: a leak here produces a model that
scores beautifully in evaluation and predicts nothing in production, and it leaves no trace anywhere
else in the pipeline. So the clause that enforces it is written in exactly one function,
:func:`point_in_time_join_clause`, and every query this module returns has been through
:func:`assert_point_in_time`, which re-reads the generated SQL and refuses it if the guard is not on
every event join. Neither function is a formality: the assertion is the thing that survives a future
edit to the query builder that quietly drops the clause.

`inclusive_snapshot_time` decides `<=` against `<`. The snapshot is an *instant* - the start of
`snapshot_date` - so `<=` includes an event stamped exactly at that instant and `<` does not. The
label side of the build takes `event_time > T` with the same instant, so features and labels
partition the timeline with no gap and no overlap whichever way the flag is set.

**Why every aggregate carries `FILTER (WHERE e.event_time IS NOT NULL ...)`.** The join is a LEFT
join, so an entity with no qualifying event still gets exactly one row - with every `e.*` column
null. `COUNT(*)` over that row would answer 1 for an entity that has never done anything, which is
the quietest possible way to fabricate data. The join condition cannot be met by a null
`event_time`, so `e.event_time IS NOT NULL` means precisely "this row is a real event", and it is
the first predicate of every filter for that reason.

**What is not compiled to SQL.** A `derive` feature is a whitelisted expression over the entity
table's own columns and `snapshot_date`; it is computed in pandas by `transforms.derive`, the one
expression parser this codebase has (plan section 13, rule 13). It is not point-in-time guarded and
cannot be: an entity table carries one row per entity with no event time, which is exactly what the
ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED check warns about.

**SQL safety.** Every identifier that reaches a query - a role, a column, a feature name - is
matched against a strict pattern first and then quoted, and every `where` value is rendered by one
escaper. A client's column names and filter values are data we were handed, so they are never
interpolated on trust.
"""

from __future__ import annotations

import datetime
import math
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from engine.config import get_roles
from engine.onboarding.specs import AGGREGATES_OVER_COLUMN, AggFunction, FeatureDef, WhereOp

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    import pandas as pd
    from duckdb import DuckDBPyConnection

    from engine.config import UseCaseConfig
    from engine.onboarding.specs import FeatureSpec, SubAggregation, WhereClause


__all__ = [
    "POINT_IN_TIME_MARKER",
    "SNAPSHOT_VIEW",
    "FeatureError",
    "assert_point_in_time",
    "build_features",
    "compile_feature_sql",
    "compile_role_query",
    "point_in_time_join_clause",
    "render_sql_file",
    "suggested_features",
]


class FeatureError(Exception):
    """A feature spec could not be compiled into a query it is safe to run.

    Three parts, like `transforms.TransformError`: a machine-readable `code`, a `message` in the
    language of the person who uploaded the file with the numbers already in it, and a `suggestion`
    saying what to do next. Nothing here ever surfaces a Python traceback or a SQL parser error.
    """

    def __init__(self, code: str, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


SNAPSHOT_VIEW: Final[str] = "snapshots"
"""The view the build registers with one row per `(entity_key, snapshot_date)` to be built."""

POINT_IN_TIME_MARKER: Final[str] = "-- POINT-IN-TIME GUARD: never remove"

_EVENT_ALIAS: Final[str] = "e"
_SNAPSHOT_ALIAS: Final[str] = "s"

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

_JOIN_PATTERN: Final[re.Pattern[str]] = re.compile(r'\bJOIN\s+"[A-Za-z_][A-Za-z0-9_]*"\s+([A-Za-z_]\w*)\b')
_GUARD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bAND\s+([A-Za-z_]\w*)\.event_time\s*(?:<=|<)\s*([A-Za-z_]\w*)\.snapshot_date\s*"
    + re.escape(POINT_IN_TIME_MARKER)
)

_COMPARISONS: Final[Mapping[WhereOp, str]] = MappingProxyType(
    {
        WhereOp.EQ: "=",
        WhereOp.NE: "<>",
        WhereOp.GT: ">",
        WhereOp.GTE: ">=",
        WhereOp.LT: "<",
        WhereOp.LTE: "<=",
    }
)

_AGGREGATE_TEMPLATES: Final[Mapping[AggFunction, str]] = MappingProxyType(
    {
        AggFunction.COUNT: "COUNT(*){filter}",
        AggFunction.SUM: "SUM({column}){filter}",
        AggFunction.MEAN: "AVG({column}){filter}",
        AggFunction.MIN: "MIN({column}){filter}",
        AggFunction.MAX: "MAX({column}){filter}",
        AggFunction.STD: "STDDEV_SAMP({column}){filter}",
        AggFunction.NUNIQUE: "COUNT(DISTINCT {column}){filter}",
        AggFunction.LATEST: "arg_max({column}, {alias}.event_time){filter}",
        AggFunction.FIRST: "arg_min({column}, {alias}.event_time){filter}",
        AggFunction.DAYS_SINCE_LAST: (
            "DATE_DIFF('day', MAX({alias}.event_time){filter}, {snapshot}.snapshot_date)"
        ),
        AggFunction.DAYS_SINCE_FIRST: (
            "DATE_DIFF('day', MIN({alias}.event_time){filter}, {snapshot}.snapshot_date)"
        ),
        AggFunction.EXISTS: "(COUNT(*){filter}) > 0",
    }
)
"""One template per aggregating function. `latest`/`first` use `arg_max`/`arg_min`, which skip rows
where the aggregated column is null, so they answer with the latest *known* value rather than with
the null an entity happened to leave on its most recent event. `ratio` and `derive` are absent
because neither is an aggregate: a ratio is built from two of these, and a derive is not SQL."""

_ALL_HISTORY_LIBRARY_FUNCTIONS: Final[Mapping[AggFunction, str]] = MappingProxyType(
    {
        AggFunction.DAYS_SINCE_LAST: "Days since the most recent {role} event before the snapshot.",
        AggFunction.DAYS_SINCE_FIRST: "Days since the first {role} event before the snapshot.",
    }
)
"""Library functions generated once over all history rather than once per window: "days since the
last complaint in the last 30 days" is bounded by 30 by construction, so it carries no information
the 30-day count does not already carry, and generating one per window would spend the feature
budget on copies of a number."""


# ---------------------------------------------------------------------------
# The guard: generated in one place, asserted on every query
# ---------------------------------------------------------------------------
def point_in_time_join_clause(event_alias: str, snapshot_alias: str, *, inclusive: bool) -> str:
    """The ON clause that joins an event table to the snapshots without letting the future in.

    The only place in the codebase that writes this clause. `inclusive` is the snapshot spec's
    `inclusive_snapshot_time`: the snapshot is the instant at the start of `snapshot_date`, so `<=`
    admits an event stamped exactly at that instant and `<` does not.
    """
    bound = "<=" if inclusive else "<"
    return (
        f"{event_alias}.entity_key = {snapshot_alias}.entity_key\n"
        f" AND {event_alias}.event_time {bound} {snapshot_alias}.snapshot_date  {POINT_IN_TIME_MARKER}"
    )


def assert_point_in_time(sql: str) -> None:
    """Re-read a generated query and refuse it unless every event join carries the guard.

    Written as a check over the finished SQL rather than as a flag the builder sets, because the
    failure this exists to catch is a future edit to the builder: a query that has lost its guard
    still runs, still returns plausible numbers, and is only ever caught by reading what was
    actually generated.
    """
    joined = _JOIN_PATTERN.findall(sql)
    guarded = [event_alias for event_alias, _ in _GUARD_PATTERN.findall(sql)]
    if not joined or sorted(joined) != sorted(guarded):
        raise FeatureError(
            "FEATURE_POINT_IN_TIME_GUARD_MISSING",
            f"A feature query joins {len(joined)} event table(s) but bounds only {len(guarded)} of "
            "them to the snapshot date, so a feature could be built from an event that had not "
            "happened yet.",
            "This is an engine fault, not a problem with your data: stop using the built dataset "
            "and report the build id.",
        )


# ---------------------------------------------------------------------------
# SQL safety
# ---------------------------------------------------------------------------
def _identifier(name: str, *, what: str) -> str:
    """A quoted identifier, or a refusal. Nothing reaches a query without passing through here."""
    if _IDENTIFIER_PATTERN.match(name) is None:
        raise FeatureError(
            "FEATURE_UNSAFE_IDENTIFIER",
            f"The {what} name '{name}' is not a plain name, so it cannot be used to build a query.",
            f"Rename the {what} to letters, digits and underscores, starting with a letter or "
            "underscore, and map it again.",
        )
    return f'"{name}"'


def _literal(value: Any) -> str:
    """A `where` value as a SQL literal: quotes doubled, and no type we cannot render exactly."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return f"'{value.isoformat()}'"
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    raise FeatureError(
        "FEATURE_WHERE_VALUE_UNSUPPORTED",
        f"A filter compares against a {type(value).__name__}, which cannot be written into a query.",
        "Compare against a number, a date, true/false or a piece of text.",
    )


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------
def _where_predicate(where: WhereClause) -> str:
    column = f"{_EVENT_ALIAS}.{_identifier(where.column, what='column')}"
    if where.op is WhereOp.IS_NULL:
        return f"{column} IS NULL"
    if where.op is WhereOp.NOT_NULL:
        return f"{column} IS NOT NULL"
    if where.op is WhereOp.IN:
        values = ", ".join(_literal(item) for item in where.value)
        return f"{column} IN ({values})"
    return f"{column} {_COMPARISONS[where.op]} {_literal(where.value)}"


def _filter_clause(window_days: int | None, where: WhereClause | None) -> str:
    """The `FILTER (WHERE ...)` every aggregate carries; see the module docstring for the first term."""
    predicates = [f"{_EVENT_ALIAS}.event_time IS NOT NULL"]
    if window_days is not None:
        predicates.append(
            f"{_EVENT_ALIAS}.event_time > " f"{_SNAPSHOT_ALIAS}.snapshot_date - INTERVAL {window_days} DAY"
        )
    if where is not None:
        predicates.append(_where_predicate(where))
    return f" FILTER (WHERE {' AND '.join(predicates)})"


def _aggregate(
    function: AggFunction, column: str | None, window_days: int | None, where: WhereClause | None
) -> str:
    template = _AGGREGATE_TEMPLATES[function]
    quoted = "" if column is None else f"{_EVENT_ALIAS}.{_identifier(column, what='column')}"
    return template.format(
        column=quoted,
        filter=_filter_clause(window_days, where),
        alias=_EVENT_ALIAS,
        snapshot=_SNAPSHOT_ALIAS,
    )


def _part(part: SubAggregation) -> str:
    return _aggregate(part.function, part.column, part.window_days, part.where)


def _feature_expression(feature: FeatureDef) -> str:
    if feature.function is AggFunction.DERIVE:
        raise FeatureError(
            "FEATURE_DERIVE_NOT_SQL",
            f"Feature '{feature.name}' is derived from the entity table's own columns, so it is "
            "computed from those columns rather than aggregated out of an event table.",
            "Leave derived features out of the role queries; build_features computes them.",
        )
    if feature.function is AggFunction.RATIO and feature.of is not None and feature.over is not None:
        # NULLIF is what makes a ratio with nothing underneath it null rather than an error or a
        # fabricated zero: dividing by NULL is NULL, all the way out to the built column.
        return f"({_part(feature.of)}) / NULLIF({_part(feature.over)}, 0)"
    return _aggregate(feature.function, feature.column, feature.window_days, feature.where)


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------
def compile_role_query(role: str, features: Sequence[FeatureDef], *, inclusive: bool) -> str:
    """One query computing every feature of one event role, for every row of `snapshots`."""
    columns = [f"{_SNAPSHOT_ALIAS}.entity_key", f"{_SNAPSHOT_ALIAS}.snapshot_date"]
    columns += [
        f"{_feature_expression(feature)} AS {_identifier(feature.name, what='feature')}"
        for feature in features
    ]
    selected = ",\n       ".join(columns)
    sql = (
        f"SELECT {selected}\n"
        f'FROM "{SNAPSHOT_VIEW}" {_SNAPSHOT_ALIAS}\n'
        f"LEFT JOIN {_identifier(role, what='role')} {_EVENT_ALIAS}\n"
        f"  ON {point_in_time_join_clause(_EVENT_ALIAS, _SNAPSHOT_ALIAS, inclusive=inclusive)}\n"
        f"GROUP BY {_SNAPSHOT_ALIAS}.entity_key, {_SNAPSHOT_ALIAS}.snapshot_date"
    )
    assert_point_in_time(sql)
    return sql


def compile_feature_sql(spec: FeatureSpec, *, roles: Collection[str], inclusive: bool) -> dict[str, str]:
    """One query per event role in `spec`, keyed by role, for the roles the client actually mapped.

    A role the client did not map is skipped rather than queried against a view that does not exist:
    a use case suggests features for every role it knows about, and a client who uploaded no
    complaints file is not a client whose build should fail.
    """
    queries: dict[str, str] = {}
    for role in spec.roles:
        if role not in roles:
            continue
        aggregated = [f for f in spec.for_role(role) if f.function is not AggFunction.DERIVE]
        if aggregated:
            queries[role] = compile_role_query(role, aggregated, inclusive=inclusive)
    return queries


def render_sql_file(queries: Mapping[str, str]) -> str:
    """`features.sql`: the compiled queries, saved beside the dataset so a build can be re-read.

    Kept because "why is this feature that number?" is answerable from a file a human can run, and
    because Phase 4 porting the aggregation to a warehouse starts by reading what we generated.
    """
    blocks = [
        "-- features.sql - generated by engine.onboarding.features, one query per event role.",
        f"-- Every join is bounded to the snapshot date; the line marked {POINT_IN_TIME_MARKER}",
        "-- is what keeps a feature from reading an event that had not happened yet.",
    ]
    blocks += [f"\n-- role: {role}\n{queries[role]};" for role in sorted(queries)]
    return "\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _registered_views(con: DuckDBPyConnection) -> frozenset[str]:
    rows = con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    return frozenset(str(row[0]) for row in rows)


def _derived_frame(con: DuckDBPyConnection, role: str, features: Sequence[FeatureDef]) -> pd.DataFrame:
    from engine.onboarding.transforms import derive

    frame = con.execute(
        f"SELECT {_SNAPSHOT_ALIAS}.entity_key, {_SNAPSHOT_ALIAS}.snapshot_date, "
        f"{_EVENT_ALIAS}.* EXCLUDE (entity_key)\n"
        f'FROM "{SNAPSHOT_VIEW}" {_SNAPSHOT_ALIAS}\n'
        f"LEFT JOIN {_identifier(role, what='role')} {_EVENT_ALIAS}\n"
        f"  ON {_EVENT_ALIAS}.entity_key = {_SNAPSHOT_ALIAS}.entity_key"
    ).df()
    for feature in features:
        if feature.expression is not None:
            frame[feature.name] = derive(frame, feature.expression, snapshot_column="snapshot_date")
    return frame


def build_features(con: DuckDBPyConnection, spec: FeatureSpec, *, inclusive: bool) -> pd.DataFrame:
    """Every feature of `spec`, one row per `(entity_key, snapshot_date)` of the `snapshots` view.

    The frame starts as the snapshots themselves and every role is merged onto it with a left join,
    so an entity with no complaints keeps its row and gets nulls: a missing row would silently drop
    that entity from training, which is a far worse answer than "nothing happened".
    """
    keys = ["entity_key", "snapshot_date"]
    built = con.execute(f'SELECT entity_key, snapshot_date FROM "{SNAPSHOT_VIEW}"').df()
    roles = _registered_views(con)

    for sql in compile_feature_sql(spec, roles=roles, inclusive=inclusive).values():
        built = built.merge(con.execute(sql).df(), on=keys, how="left")

    derived = [f for f in spec.features if f.function is AggFunction.DERIVE and f.role in roles]
    for role in sorted({feature.role for feature in derived}):
        of_role = [feature for feature in derived if feature.role == role]
        frame = _derived_frame(con, role, of_role)
        built = built.merge(frame[[*keys, *(f.name for f in of_role)]], on=keys, how="left")

    return built[[*keys, *(f.name for f in spec.features if f.name in built.columns)]]


# ---------------------------------------------------------------------------
# Suggestion
# ---------------------------------------------------------------------------
def _library_name(role: str, function: AggFunction, column: str | None, window_days: int | None) -> str:
    parts = [role, function.value, *([] if column is None else [column])]
    if window_days is not None:
        parts.append(f"{window_days}d")
    return "_".join(parts).lower()


def _library_description(role: str, function: AggFunction, column: str | None, window_days: int) -> str:
    over = f"{role} events in the {window_days} days before the snapshot"
    if column is None:
        return f"The {function.value.replace('_', ' ')} of {over}."
    return f"The {function.value.replace('_', ' ')} of {column} across {over}."


def _generated(
    role: str, columns: Sequence[str], functions: Sequence[AggFunction], windows: Sequence[int]
) -> list[FeatureDef]:
    features: list[FeatureDef] = []
    for function in functions:
        targets: Sequence[str | None] = columns if function in AGGREGATES_OVER_COLUMN else [None]
        for column in targets:
            all_history = _ALL_HISTORY_LIBRARY_FUNCTIONS.get(function)
            if all_history is not None:
                features.append(
                    FeatureDef(
                        name=_library_name(role, function, column, None),
                        role=role,
                        function=function,
                        column=column,
                        description=all_history.format(role=role),
                    )
                )
                continue
            features += [
                FeatureDef(
                    name=_library_name(role, function, column, window),
                    role=role,
                    function=function,
                    column=column,
                    window_days=window,
                    description=_library_description(role, function, column, window),
                )
                for window in windows
            ]
    return features


def suggested_features(
    config: UseCaseConfig, mapped_roles: Mapping[str, Sequence[str]]
) -> tuple[FeatureDef, ...]:
    """What to offer this client: the use case's own features, then the generated library.

    `mapped_roles` maps each role the client mapped to the standard columns that role's mapped table
    carries. A use-case feature wins over a generated one of the same name - that is how "a use case
    adds to or overrides the library" survives DEC-002's rule that a list in a use-case file replaces
    rather than merges: the use case names the features it cares about, and the library fills in
    around them instead of colliding with them.
    """
    chosen: dict[str, FeatureDef] = {
        feature.name: feature for feature in config.suggested_features if feature.role in mapped_roles
    }
    library = config.onboarding.features.library
    if not library.enabled:
        return tuple(chosen.values())

    catalogue = get_roles()
    for role in sorted(mapped_roles):
        role_spec = catalogue.get(role)
        if role_spec is None or not role_spec.is_event:
            continue
        # The role's required columns are its key and its event time: aggregating either of them
        # says nothing days_since_last does not already say.
        columns = sorted(set(mapped_roles[role]) - set(role_spec.required_columns))
        for feature in _generated(role, columns, library.functions, library.windows_days):
            chosen.setdefault(feature.name, feature)
    return tuple(chosen.values())
