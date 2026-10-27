"""`propose_features`: behaviour features per customer, built from an order or event history (Step 7).

A model's accuracy on orders and events comes mostly from what each customer did: how long ago, how often, how much,
and the trend. The point-in-time feature library of onboarding (`engine.onboarding.features`: count, sum, mean, min,
max, std, nunique, latest, first, days since first/last, exists, ratio, each with an optional window and a filter)
already writes these as queries that cannot read the future. This tool lets the AI ask for them, by name and
arguments, instead of writing the query itself.

What the tool does:

* It **compiles** the request into ONE read-only query (`compile_features`) and proposes it as the ordinary
  `sql_query` recipe step, through :func:`engine.doctor.tools_sql._propose`. So the trial and every run go through
  :func:`engine.doctor.materialize.materialize_plan` with the same step, order, table names and as-of date: the trial
  equals the run, the policy treats it as the reshape it is (never ticked, the person approves), and next month's
  files replay it (with the as-of rule, when one is given).
* The guarantees are **in code**, not in the prompt:

  - only rows dated on or before the as-of date feed a feature: the query first cuts the table at the as-of date,
    and every aggregate joins through :func:`engine.onboarding.features.point_in_time_join_clause`, re-read by
    :func:`assert_point_in_time` before the query leaves this module;
  - no feature reads the decided outcome column (`FEATURE_READS_OUTCOME`, refused before anything runs);
  - the leak probe runs in every scope: `materialize_plan` builds the table again without the rows dated after the
    as-of date and refuses (`FUTURE_EVENTS_LEAKED`) when a customer's features moved, in the trial and in every run;
  - a feature that is the same for every customer, or blank for almost all of them, is reported as a finding
    (`FEATURE_CONSTANT`, `FEATURE_MOSTLY_BLANK`), never silently kept.
* The sentence for the person lists the features in plain words, each with its fill rate and the as-of date. The
  proposal is pending and never ticked. No cell value is written anywhere: a filter's text value is not repeated.

Not here: scaling, encoding or imputation for the model (AutoML does those).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import RecipeStep
from engine.agent.sql_step import MAX_COMPILED_SQL_CHARS, SqlStepError, date_read_counts, table_alias
from engine.config import (
    AggFunction,
    FeatureDef,
    StrictBase,
    SubAggregation,
    WhereClause,
    WhereOp,
)
from engine.doctor.guards import Finding
from engine.doctor.materialize import CANDIDATE, Full, materialize_plan, table_names
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)
from engine.doctor.tools_sql import (
    MAX_COLUMNS_KEPT,
    ProposeSqlStepArgs,
    SqlStepProposal,
    _propose,
    _trial_items,
)
from engine.onboarding.features import (
    FeatureError,
    assert_point_in_time,
    feature_expression,
    filter_clause,
    point_in_time_join_clause,
    quote_identifier,
)

__all__ = [
    "M_FEATURE_TOOLS",
    "PRESETS",
    "Compiled",
    "FeatureLine",
    "FeatureProposal",
    "FeatureRequest",
    "ProposeFeaturesArgs",
    "compile_features",
    "parse_query_output",
    "register_feature_tools",
]

M_FEATURE_TOOLS: Final[tuple[str, ...]] = ("propose_features",)
PRESETS: Final[tuple[str, ...]] = ("rfm", "rfm_plus")
MAX_FEATURES: Final[int] = 40
MAX_OUTPUT_CHARS: Final[int] = 22_000
"""The answer's cap through the registry: the compiled query (up to `MAX_COMPILED_SQL_CHARS`) is in the step, and
`MAX_FEATURES` lines and their words are in the proposal; the default 5,500 fits neither."""
MOSTLY_BLANK_SHARE: Final[float] = 0.95
"""A feature blank for more than this share of customers is reported."""
_ROLE: Final[str] = "events"
_ENTITY: Final[str] = "entity_key"
_EVENT_TIME: Final[str] = "event_time"
_WINDOWS: Final[tuple[int, int]] = (90, 365)
_ALLOWED: Final[frozenset[AggFunction]] = frozenset(
    {
        AggFunction.COUNT,
        AggFunction.SUM,
        AggFunction.MEAN,
        AggFunction.MIN,
        AggFunction.MAX,
        AggFunction.STD,
        AggFunction.NUNIQUE,
        AggFunction.LATEST,
        AggFunction.FIRST,
        AggFunction.DAYS_SINCE_LAST,
        AggFunction.DAYS_SINCE_FIRST,
        AggFunction.EXISTS,
    }
)
"""What the AI may ask for by name: the library's aggregates (a ratio or a derived feature is a preset's)."""
_NAME: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_WORDS: Final[dict[AggFunction, str]] = {
    AggFunction.COUNT: "number of rows",
    AggFunction.SUM: "total of {c}",
    AggFunction.MEAN: "average of {c}",
    AggFunction.MIN: "smallest {c}",
    AggFunction.MAX: "largest {c}",
    AggFunction.STD: "spread of {c}",
    AggFunction.NUNIQUE: "number of different {c}",
    AggFunction.LATEST: "latest {c}",
    AggFunction.FIRST: "first {c}",
    AggFunction.DAYS_SINCE_LAST: "days since the last row",
    AggFunction.DAYS_SINCE_FIRST: "days since the first row",
    AggFunction.EXISTS: "whether there is any row",
}
_OPS: Final[dict[WhereOp, str]] = {
    WhereOp.EQ: "is",
    WhereOp.NE: "is not",
    WhereOp.GT: "is above",
    WhereOp.GTE: "is at least",
    WhereOp.LT: "is below",
    WhereOp.LTE: "is at most",
    WhereOp.IN: "is one of some values",
    WhereOp.IS_NULL: "is blank",
    WhereOp.NOT_NULL: "is filled",
}


class WhereArg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    column: str = Field(min_length=1, max_length=128)
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "is_null", "not_null"]
    value: Any = None


class FeatureArg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=63, description="Lower case letters, digits and underscores.")
    agg: Literal[
        "count",
        "sum",
        "mean",
        "min",
        "max",
        "std",
        "nunique",
        "latest",
        "first",
        "days_since_last",
        "days_since_first",
        "exists",
    ]
    column: str | None = Field(
        default=None, max_length=128, description="Needed by all but the counts and days."
    )
    window_days: int | None = Field(
        default=None, ge=1, le=3650, description="Only rows in the last N days before the as-of date."
    )
    where: WhereArg | None = Field(default=None, description="Only the rows that meet this filter.")


class ProposeFeaturesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    upload_id: str = Field(
        min_length=1, description="The file of orders or events (one row per order or event)."
    )
    entity_key: str = Field(min_length=1, max_length=128, description="The customer ID column of that file.")
    time_column: str = Field(min_length=1, max_length=128, description="The date of each row.")
    as_of: date | None = Field(
        default=None, description="The date the features are measured at; default: the case's as-of date."
    )
    as_of_rule: Literal["latest_date_in_file", "last_complete_month_end"] | None = Field(
        default=None, description="Keep the as-of date as a rule worked out again on every file."
    )
    features: tuple[FeatureArg, ...] = Field(default=(), max_length=MAX_FEATURES)
    preset: Literal["rfm", "rfm_plus"] | None = Field(
        default=None,
        description="rfm: days since the last order, orders and spend in 90 and 365 days, average order value. "
        "rfm_plus adds return rate, distinct products, days between orders, tenure, spend trend, active months.",
    )
    amount_column: str | None = Field(
        default=None, max_length=128, description="What an order is worth (presets)."
    )
    order_column: str | None = Field(
        default=None,
        max_length=128,
        description="The order or invoice number, when a row is one line of an order.",
    )
    product_column: str | None = Field(default=None, max_length=128, description="The product (rfm_plus).")
    outcome_column: str | None = Field(
        default=None, max_length=128, description="Filled in from the case: no feature may read it."
    )
    output_name: str = Field(default="customer_features", min_length=1, max_length=64)


FeatureRequest = ProposeFeaturesArgs


@dataclass(frozen=True)
class _Col:
    name: str
    expr: str
    phrase: str
    reads: tuple[str, ...]


@dataclass(frozen=True)
class Compiled:
    """The query for one request, the feature columns it makes and every source column it reads."""

    sql: str
    features: tuple[_Col, ...]
    reads: frozenset[str]
    description: str
    notes: tuple[str, ...] = ()


class _CompiledSqlArgs(ProposeSqlStepArgs):
    """`propose_sql_step`'s arguments for a query our own code compiled: it may be longer than one written by hand."""

    sql: str = Field(min_length=1, max_length=MAX_COMPILED_SQL_CHARS)


def _refuse(code: str, message: str) -> DataToolError:
    return DataToolError(code, message, status=422)


def _where(arg: WhereArg | None) -> WhereClause | None:
    if arg is None:
        return None
    try:
        return WhereClause(column=arg.column, op=WhereOp(arg.op), value=arg.value)
    except Exception as exc:  # the config's own message names the problem, never a value
        raise _refuse("FEATURE_NOT_VALID", f"A filter on '{arg.column}' is not valid: {_first(exc)}") from exc


def _first(exc: Exception) -> str:
    return str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__


def _window(days: int | None) -> str:
    return "" if days is None else f" in the last {days} days"


def _where_phrase(where: WhereClause | None) -> str:
    if where is None:
        return ""
    value = ""
    if isinstance(where.value, (int, float)) and not isinstance(where.value, bool):
        value = (
            f" {where.value:g}"  # a number is the person's own threshold; text from a cell is never repeated
        )
    return f", only rows where {where.column} {_OPS[where.op]}{value}"


def _library(
    name: str,
    function: AggFunction,
    column: str | None = None,
    window: int | None = None,
    where: WhereClause | None = None,
    *,
    phrase: str | None = None,
) -> _Col:
    try:
        feature = FeatureDef(
            name=name, role=_ROLE, function=function, column=column, window_days=window, where=where
        )
        expr = feature_expression(feature)
    except FeatureError as exc:
        raise _refuse(exc.code, f"{exc.message} {exc.suggestion}") from exc
    except Exception as exc:
        raise _refuse("FEATURE_NOT_VALID", f"Feature '{name}' is not valid: {_first(exc)}") from exc
    reads = tuple(c for c in (column, where.column if where else None) if c)
    if function is AggFunction.SUM and (window is not None or where is not None):
        expr = f"COALESCE({expr}, 0)"  # no row in the window is a spend of nothing, not an unknown
    said = phrase or (_WORDS[function].format(c=column or "") + _window(window) + _where_phrase(where))
    return _Col(name, expr, said, reads)


def _ratio(name: str, top: SubAggregation, bottom: SubAggregation, phrase: str) -> _Col:
    try:
        feature = FeatureDef(name=name, role=_ROLE, function=AggFunction.RATIO, of=top, over=bottom)
        expr = feature_expression(feature)
    except FeatureError as exc:
        raise _refuse(exc.code, f"{exc.message} {exc.suggestion}") from exc
    reads = tuple(
        c for part in (top, bottom) for c in (part.column, part.where.column if part.where else None) if c
    )
    return _Col(name, expr, phrase, reads)


def _col(name: str) -> str:
    try:
        return quote_identifier(name, what="column")
    except FeatureError as exc:
        raise _refuse(exc.code, f"{exc.message} {exc.suggestion}") from exc


def _preset(given: ProposeFeaturesArgs) -> tuple[list[_Col], list[str]]:
    amount, order, product = given.amount_column, given.order_column, given.product_column
    if not amount:
        raise _refuse(
            "FEATURE_NEEDS_COLUMN",
            f"The preset '{given.preset}' needs `amount_column`: what an order is worth.",
        )
    notes: list[str] = []
    short, long = _WINDOWS
    orders = (AggFunction.NUNIQUE, order) if order else (AggFunction.COUNT, None)
    kind = "orders" if order else "rows"
    cols = [
        _library("days_since_last_order", AggFunction.DAYS_SINCE_LAST, phrase="days since the last order"),
        _library(f"orders_{short}d", orders[0], orders[1], short, phrase=f"{kind} in the last {short} days"),
        _library(f"orders_{long}d", orders[0], orders[1], long, phrase=f"{kind} in the last year"),
        _library(f"spend_{short}d", AggFunction.SUM, amount, short, phrase=f"spend in the last {short} days"),
        _library(f"spend_{long}d", AggFunction.SUM, amount, long, phrase="spend in the last year"),
    ]
    if order:
        cols.append(
            _ratio(
                "avg_order_value",
                SubAggregation(function=AggFunction.SUM, column=amount),
                SubAggregation(function=AggFunction.NUNIQUE, column=order),
                "average order value",
            )
        )
    else:
        cols.append(_library("avg_order_value", AggFunction.MEAN, amount, phrase="average order value"))
    if given.preset == "rfm":
        return cols, notes
    every = filter_clause(None, None)
    q = _col(amount)
    counted = _col(order) if order else f"e.{_EVENT_TIME}"
    cols.append(
        _ratio(
            "return_rate",
            SubAggregation(
                function=AggFunction.COUNT, where=WhereClause(column=amount, op=WhereOp.LT, value=0)
            ),
            SubAggregation(function=AggFunction.COUNT),
            "share of rows that are returns (a negative amount)",
        )
    )
    if product:
        cols.append(
            _library("distinct_products", AggFunction.NUNIQUE, product, phrase="number of different products")
        )
    else:
        notes.append("No product column was given, so 'number of different products' is left out.")
    cols.append(
        _Col(
            "days_between_orders",
            f"DATE_DIFF('day', MIN(e.{_EVENT_TIME}){every}, MAX(e.{_EVENT_TIME}){every}) "
            f"/ NULLIF(COUNT(DISTINCT {'e.' + counted if order else counted}){every} - 1, 0)",
            "average days between orders",
            (order,) if order else (),
        )
    )
    cols.append(_library("tenure_days", AggFunction.DAYS_SINCE_FIRST, phrase="days since the first order"))
    last = f"SUM(e.{q}){filter_clause(short, None)}"
    before = (
        f"SUM(e.{q}) FILTER (WHERE e.{_EVENT_TIME} IS NOT NULL "
        f"AND e.{_EVENT_TIME} > s.snapshot_date - INTERVAL {2 * short} DAY "
        f"AND e.{_EVENT_TIME} <= s.snapshot_date - INTERVAL {short} DAY)"
    )
    cols.append(
        _Col(
            "spend_trend",
            f"COALESCE({last}, 0) - COALESCE({before}, 0)",
            f"spend in the last {short} days minus spend in the {short} days before",
            (amount,),
        )
    )
    cols.append(
        _Col(
            "active_months",
            f"COUNT(DISTINCT DATE_TRUNC('month', e.{_EVENT_TIME})){every}",
            "number of different months with an order",
            (),
        )
    )
    return cols, notes


def compile_features(given: ProposeFeaturesArgs, table: str, *, outcome: str | None = None) -> Compiled:
    """The query for `given` over the table whose SQL name is `table` (`t_<name>`).

    Raises `DataToolError`: a request no feature library can answer, or a feature that reads the outcome. The query
    carries `$as_of`; it is cut at that date before any feature is computed.
    """
    cols: list[_Col] = []
    notes: list[str] = []
    if given.preset:
        cols, notes = _preset(given)
    for arg in given.features:
        if not _NAME.match(arg.name):
            raise _refuse(
                "FEATURE_NOT_VALID",
                f"The feature name '{arg.name[:40]}' is not lower case letters, digits and underscores.",
            )
        function = AggFunction(arg.agg)
        if function not in _ALLOWED:
            raise _refuse(
                "FEATURE_NOT_VALID", f"'{arg.agg}' is not an aggregate the feature library offers here."
            )
        cols.append(_library(arg.name, function, arg.column, arg.window_days, _where(arg.where)))
    if not cols:
        raise _refuse("FEATURE_NOTHING_ASKED", "Ask for a preset (rfm or rfm_plus) or at least one feature.")
    if len(cols) > MAX_FEATURES:
        raise _refuse("FEATURE_TOO_MANY", f"At most {MAX_FEATURES} features in one step.")
    names = [c.name for c in cols]
    key = given.entity_key
    if len(set(names)) != len(names) or key in names:
        raise _refuse("FEATURE_NAME_REPEATED", "Each feature needs its own name, and not the ID column's.")
    reads = {key, given.time_column, *(r for c in cols for r in c.reads)}
    if outcome is not None and any(r.lower() == outcome.lower() for r in reads):
        raise _refuse(
            "FEATURE_READS_OUTCOME",
            "A feature may not read the outcome column: the model would be handed the answer. Leave it out.",
        )
    if outcome is not None and any(n.lower() == outcome.lower() for n in names):
        raise _refuse("FEATURE_READS_OUTCOME", "A feature may not take the name of the outcome column.")
    if {_ENTITY, _EVENT_TIME} & {r.lower() for r in reads}:
        raise _refuse("FEATURE_NOT_VALID", "Rename the columns called entity_key or event_time first.")
    source = ", ".join(_col(c) for c in sorted(reads - {key, given.time_column}))
    shown = f"{_col(key)} AS {_ENTITY}, TRY_CAST({_col(given.time_column)} AS DATE) AS {_EVENT_TIME}"
    sql = (
        f"WITH ev AS (SELECT {shown}{', ' + source if source else ''} FROM {table} "
        f"WHERE NULLIF(TRIM(CAST({_col(key)} AS VARCHAR)), '') IS NOT NULL AND TRY_CAST({_col(given.time_column)} AS DATE) <= CAST($as_of AS DATE)), "
        f"snapshots AS (SELECT DISTINCT {_ENTITY}, CAST($as_of AS DATE) AS snapshot_date FROM ev) "
        f"SELECT s.{_ENTITY} AS {_col(key)}, "
        + ", ".join(f"{c.expr} AS {_col(c.name)}" for c in cols)
        + ' FROM "snapshots" s LEFT JOIN "ev" e ON '
        + point_in_time_join_clause("e", "s", inclusive=True).replace("\n", " ")
        + f"\nGROUP BY s.{_ENTITY}, s.snapshot_date ORDER BY s.{_ENTITY}"
    )
    if len(sql) > MAX_COMPILED_SQL_CHARS:
        raise _refuse("FEATURE_TOO_MANY", "These features make a query that is too long: ask for fewer.")
    try:
        assert_point_in_time(sql)
    except FeatureError as exc:
        raise _refuse(exc.code, exc.message) from exc
    label = f"preset {given.preset}" if given.preset else f"{len(cols)} features"
    description = (
        f"Behaviour features per {key} ({label}), measured only from rows dated on or before the as-of date."
    )
    return Compiled(sql, tuple(cols), frozenset(reads), description[:300], tuple(notes))


class FeatureLine(StrictBase):
    name: str
    phrase: str = Field(
        default="", description="Empty here: the sentence already says what each feature is, in plain words."
    )
    fill_rate: float = Field(description="Share of customers it has a value for (0 to 1).")
    distinct: int = Field(description="How many different values it takes.")
    median: float | None = Field(default=None, description="The middle value of a number feature.")
    state: Literal["ok", "constant", "mostly_blank"] = "ok"


class FeatureProposal(SqlStepProposal):
    """`propose_sql_step`'s answer for the compiled query, with the features listed for the person."""

    entity_key: str
    features_as_of: date
    features: tuple[FeatureLine, ...]
    notes: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        out = super().payload()
        out["features"] = [f.model_dump(mode="json") for f in self.features]
        out["features_as_of"] = self.features_as_of.isoformat()  # not `as_of`: that key sets the case's date
        return out


def parse_query_output(output: Any) -> SqlStepProposal:
    """A registry answer of `propose_sql_step` or `propose_features`, as the model that carries it."""
    if isinstance(output, dict) and "features" in output:
        return FeatureProposal.model_validate(output)
    return SqlStepProposal.model_validate(output)


def _lines(made: pd.DataFrame, cols: tuple[_Col, ...]) -> tuple[list[FeatureLine], list[Finding]]:
    lines: list[FeatureLine] = []
    findings: list[Finding] = []
    total = len(made)
    for col in cols:
        if col.name not in made.columns or total == 0:
            continue
        series = made[col.name]
        filled = int(series.notna().sum())
        rate = filled / total
        try:
            distinct = int(series.nunique(dropna=True))
        except (TypeError, ValueError):
            distinct = 0
        numeric = pd.to_numeric(series, errors="coerce") if series.dtype != bool else None
        median = float(numeric.median()) if numeric is not None and numeric.notna().any() else None
        state: Literal["ok", "constant", "mostly_blank"] = "ok"
        if 1 - rate > MOSTLY_BLANK_SHARE:
            state = "mostly_blank"
            findings.append(
                Finding(
                    level="flag",
                    code="FEATURE_MOSTLY_BLANK",
                    count=total - filled,
                    message=f"'{col.name}' is blank for {total - filled:,} of {total:,} customers; it adds little.",
                )
            )
        elif distinct <= 1 and total > 1:
            state = "constant"
            findings.append(
                Finding(
                    level="flag",
                    code="FEATURE_CONSTANT",
                    count=total,
                    message=f"'{col.name}' is the same for every customer; it tells a model nothing.",
                )
            )
        lines.append(
            FeatureLine(
                name=col.name,
                phrase=col.phrase,
                fill_rate=round(rate, 4),
                distinct=distinct,
                median=None if median is None or median != median else round(median, 4),
                state=state,
            )
        )
    return lines, findings


def _sentence(
    lines: list[FeatureLine],
    key: str,
    as_of: date,
    rows: int,
    columns: int,
    name: str,
    notes: tuple[str, ...],
    rows_read: int,
) -> str:
    listed = "; ".join(f"{f.phrase} ({f.fill_rate:.0%} filled)" for f in lines)
    text = (
        f"Add {len(lines)} behaviour features per {key}, measured at {as_of.isoformat()} from rows dated on or "
        f"before it: {listed}. Reads {rows_read:,} rows; the new table '{name}' would have {rows:,} rows (one "
        f"per {key}) and {columns:,} columns."
    )
    return " ".join([text, *notes])


_ROLL_UP: Final[frozenset[str]] = frozenset(
    {"PLAN_LOSES_TOO_MUCH", "STEP_NOT_COUNTED", "STEP_LEAVES_OUT_ROWS"}
)
"""What the row-loss guards say about a step that turns many rows into one row per customer: it is the purpose of
this step, not a loss. The customers and rows read are said in the sentence, and what is really lost (rows with no
ID, no readable date) is a finding of this tool."""


def _input_findings(given: ProposeFeaturesArgs, table: str, frame: pd.DataFrame) -> list[Finding]:
    """What the query would drop from `frame` without a word: rows with no ID, no date, or a date it cannot read.

    Refuses (`FEATURE_TIME_NOT_READ`) when most dates cannot be read: day-first or month-first text, `20240331`.
    """
    key, when = given.entity_key, given.time_column
    for column in (key, when):
        if column not in frame.columns:
            raise _refuse("FEATURE_NOT_VALID", f"The column '{column[:60]}' is not in the file.")
    try:
        n, no_key, no_time, unread = date_read_counts(frame, table, key, when)
    except SqlStepError as exc:
        raise DataToolError(exc.code, exc.message, status=422) from exc
    with_time = n - no_time
    if (with_time and unread * 2 > with_time) or (with_time == 0 and n):
        raise _refuse(
            "FEATURE_TIME_NOT_READ",
            f"{unread:,} of {with_time:,} values of '{when}' could not be read as dates (they are not written "
            "year first, like 2025-06-20), so no feature could be dated. Parse the date column first.",
        )
    found: list[Finding] = []
    if unread:
        found.append(
            Finding(
                level="flag",
                code="FEATURE_TIME_PARTLY_UNREAD",
                count=unread,
                message=f"{unread:,} of {n:,} rows have a '{when}' that could not be read as a date; they feed no feature.",
            )
        )
    if no_time:
        found.append(
            Finding(
                level="flag",
                code="FEATURE_TIME_BLANK",
                count=no_time,
                message=f"{no_time:,} of {n:,} rows have no '{when}'; they feed no feature.",
            )
        )
    if no_key:
        found.append(
            Finding(
                level="flag",
                code="FEATURE_NO_ID",
                count=no_key,
                message=f"{no_key:,} of {n:,} rows have no '{key}'; they belong to no customer and feed no feature.",
            )
        )
    return found


def _decided_outcome(ctx: DataToolContext, upload_id: str, columns: list[str]) -> str | None:
    """The outcome column the case has decided for this file (an accepted item), else the agent's target."""
    from engine.doctor.materialize import roles_of

    if ctx.case_items:
        target = roles_of(_trial_items(ctx.case_items, ctx.replacing), upload_id, columns)[1]
        if target is not None:
            return target
    return ctx.agent.target if ctx.agent is not None else None


def _propose_features(given: ProposeFeaturesArgs, ctx: DataToolContext) -> FeatureProposal:
    loaded = {f.upload_id: f for f in ctx.files}
    if given.upload_id not in loaded:
        raise DataToolError("TOOL_INPUT_INVALID", "That file is not in this case.", status=422)
    if given.as_of is None and given.as_of_rule is None and ctx.as_of is None:
        raise _refuse(
            "FEATURE_NEEDS_AS_OF",
            "Features are measured at a date: ask the person for the as-of date, or name `as_of_rule`.",
        )
    as_of = given.as_of or (ctx.as_of if given.as_of_rule is None else None)
    outcome = given.outcome_column or _decided_outcome(
        ctx, given.upload_id, [str(c) for c in loaded[given.upload_id].frame.columns]
    )
    names = table_names(ctx.files)
    table = names[given.upload_id]
    compiled = compile_features(given, table_alias(table), outcome=outcome)
    sql_args = _CompiledSqlArgs(
        description=compiled.description,
        sql=compiled.sql,
        output_name=given.output_name,
        upload_ids=(given.upload_id,),
        as_of=as_of,
        as_of_rule=given.as_of_rule,
        as_of_upload_id=given.upload_id if given.as_of_rule else None,
        as_of_column=given.time_column if given.as_of_rule else None,
    )
    # What the query would drop without a word is counted on the file as the plan leaves it (before the query).
    items = _trial_items(ctx.case_items or (), ctx.replacing)
    before = materialize_plan((), items, ctx.files, Full(), twice=False, as_of=ctx.as_of)
    source = before.tables.get(table)
    inputs = _input_findings(given, table, source) if source is not None else []
    try:
        base = _propose(
            sql_args,
            ctx,
            extra={
                "compiled": True,
                "leak_column": given.time_column,
                "feature_reads": sorted(compiled.reads),
            },
        )
    except SqlStepError as exc:
        raise DataToolError(exc.code, exc.message, status=422) from exc
    # The same step, once more on every row, through the one materialisation (its future-rows check is the leak
    # probe: a feature that moves when the rows after the as-of date are taken out fails here, whoever wrote it).
    when = ctx.as_of or date.fromisoformat(str(base.step["params"]["as_of"]))
    tried = RecipeStep.model_validate(base.step)
    tried = tried.model_copy(update={"params": {k: v for k, v in tried.params.items() if k != "as_of_rule"}})
    whole = materialize_plan(
        (),
        items,
        ctx.files,
        Full(),
        candidate=(given.upload_id, tried),
        as_of=ctx.as_of,
    )
    own = next((f for f in whole.failures if f.item_id == CANDIDATE), None)
    if own is not None:
        raise DataToolError(own.code, own.message, status=422)
    made = whole.tables.get(table)
    if made is not None and len(made) == 0:
        raise _refuse(
            "FEATURE_NO_ROWS",
            f"No row is dated on or before {when.isoformat()} with an ID, so there is no customer to build "
            "features for. Check the as-of date and the date column.",
        )
    findings = [f for f in base.findings if f.code not in _ROLL_UP and f.code != "STEP_NOT_MEASURED"]
    seen = {(f.code, f.message) for f in findings}
    for plan in whole.findings:
        if (
            plan.item_id in (CANDIDATE, None)
            and plan.code not in _ROLL_UP
            and plan.code != "STEP_NOT_MEASURED"
        ):
            found = Finding(
                level=plan.level, code=plan.code, message=plan.text, count=plan.count, detail=plan.detail
            )
            if (found.code, found.message) not in seen:
                findings.append(found)
                seen.add((found.code, found.message))
    findings.extend(inputs)
    lines: list[FeatureLine] = []
    if made is not None:
        lines, extra = _lines(made, compiled.features)
        findings.extend(extra)
    rows_out = len(made) if made is not None else base.rows_out
    columns_out = len(made.columns) if made is not None else base.columns_out
    rows_read = len(source) if source is not None else base.rows_in
    sentence = _sentence(
        lines, given.entity_key, when, rows_out, columns_out, given.output_name, compiled.notes, rows_read
    )
    # The sentence already says what each feature is: the answer carries the numbers, not the words twice.
    lines = [line.model_copy(update={"phrase": ""}) for line in lines]
    names_out = tuple(str(c) for c in made.columns) if made is not None else base.all_columns
    data = base.model_dump()
    data.update(
        title=compiled.description.rstrip("."),
        sentence=sentence,
        findings=tuple(findings),
        needs_yes=any(f.level == "stop" for f in findings),
        entity_key=given.entity_key,
        features_as_of=when,
        features=tuple(lines),
        notes=compiled.notes,
        rows_out=rows_out,
        columns_out=columns_out,
        sampled=False,
        columns=(),
        all_columns=names_out[:MAX_COLUMNS_KEPT],
        two_valued=(),
    )
    return FeatureProposal.model_validate(data)


def register_feature_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `propose_features` (catalogue #86), for the rules and the AI; its names."""
    registry.register(
        DataTool(
            number=86,
            name="propose_features",
            kind=DataToolKind.PROPOSE,
            description=(
                "Propose behaviour features per customer from a file of orders or events (days since the last "
                "order, orders and spend in 90 and 365 days, ...), from a preset (rfm, rfm_plus) and/or a list of "
                "{name, agg, column, window_days, where}. Only rows dated on or before the as-of date feed a "
                "feature; none may read the outcome. Shown as a plain list with fill rates; nothing is changed "
                "until the person approves the plan."
            ),
            input_model=ProposeFeaturesArgs,
            output_model=FeatureProposal,
            route_policy=RoutePolicy(
                role=Role.ANALYST,
                action="doctor.propose_features",
                purpose="ask Data Doctor for a suggestion",
            ),
            exposure=Exposure.SESSION_ONLY,
            run=_propose_features,
            callers=frozenset({"rules", "ai"}),
            needs=("files",),
            max_output_bytes=MAX_OUTPUT_CHARS,
            policy_kind="data.propose_features",
            wraps="engine/onboarding/features.py (point-in-time library); the sql_query recipe step",
        )
    )
    return M_FEATURE_TOOLS
