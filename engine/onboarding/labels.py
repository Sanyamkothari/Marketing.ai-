"""Labels and censoring (Phase 2 plan section 6.3): a `LabelSpec` compiled into one DuckDB query.

A label is the one column of the dataset that is not a measurement of the client's data but a
*definition* of the client's business. "Churn" is not a column anyone stores; it is "no payment in
the next 60 days", and which 60 days is the whole of the difference between a model that works and
a model that scores 0.97 on nothing.

Two rules carry that weight, and both live here.

**The future window.** A label for snapshot date T may read only events *after* T, exactly as a
feature for T may read only events up to T. The two halves are complements of one boundary, so
`future_window_clause` is the only place either operator is written, and `compile_label_query`
refuses to hand back SQL that does not contain the clause it generated. `inclusive` is the same
`SnapshotDefinition.inclusive_snapshot_time` the feature side reads: inclusive puts the instant at
T in the past half and the future window is `(T, T+h]`; exclusive puts it in the future half and
the window is `[T, T+h)`. Passing one value here and the other to the features means an event at
exactly T is counted twice or not at all, which is why this module makes the caller say it rather
than defaulting.

**Censoring.** A snapshot whose outcome window runs past the end of the data has no outcome yet, and
labelling it anyway is the silent bug this module exists to prevent. For an `event_absence` label -
churn - "no event in the next 60 days" is trivially true for every entity in the last 60 days of
the extract, so the final snapshots read as 100% churn, the model learns the calendar instead of the
customer, and the validation metric looks excellent. The end of the data is `max(event_time)` over
the *whole* label role, deliberately not per entity and deliberately not filtered by the label's
`where`: how far the extract runs is a fact about the file, while an entity whose own last event is
old is precisely the churner we are trying to find, so a per-entity cut-off would define the answer
in terms of itself.

**Lapse outcomes (Plan J M93).** "No purchase, renewal or recharge within N days" exists in every
industry, and it usually comes with a grace period: a prepaid customer whose pack ran out may still
recharge for a few days before the number counts as lapsed. `grace_days` extends the outcome window
by that period - a 30-day lapse label with 7 days' grace reads `(T, T+37]` (inclusive) - and the
censoring rule below uses the whole window, so a snapshot whose grace period runs past the end of the
data is dropped like any other unfinished window. `exclude_roles` names event tables whose events
inside the same window take a customer out of the label altogether (a port-out, a move to another
plan): the row is dropped from the dataset rather than counted as a lapse or as staying. Both
windows are written by `future_window_clause`, so the one-boundary rule holds for them too.

Nothing here branches on a use-case id or a client id, and no user text ever reaches SQL unparsed:
a `value_threshold` expression is walked as an `ast` and re-emitted from a closed list of node
shapes, the same way `engine.onboarding.transforms.derive` does, so the only strings interpolated
into a query are ones this module wrote.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, TypeVar

from engine.config import get_roles
from engine.contracts import Severity
from engine.onboarding.specs import (
    LabelSpec,
    LabelType,
    OnboardingCheck,
    SnapshotMode,
    SnapshotStat,
    WhereClause,
    WhereOp,
)

if TYPE_CHECKING:
    import datetime
    from collections.abc import Collection

    import duckdb
    import pandas as pd

__all__ = [
    "LabelError",
    "LabelResult",
    "build_labels",
    "compile_label_query",
    "future_window_clause",
]

_T = TypeVar("_T")

SNAPSHOT_ALIAS: Final[str] = "s"
EVENT_ALIAS: Final[str] = "e"
"""The two table aliases every compiled label query uses. `future_window_clause` writes them into
the clause it generates, so they are part of this module's SQL contract rather than a local choice."""


class LabelError(Exception):
    """A label definition could not be compiled or run, with a code the UI can switch on.

    Same shape as `engine.onboarding.transforms.TransformError`: a machine-readable code, a
    business-language message and a suggestion that says what to do. Problems with the *client's
    data* - an unmapped role, a censored or degenerate snapshot - are not raised, they come back as
    `OnboardingCheck`s on the result, because the build report is where a user reads about their
    own data. This is for a definition the engine cannot execute at all.
    """

    def __init__(self, code: str, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


@dataclass(frozen=True)
class LabelResult:
    """The labelled rows that survived, one stat row per snapshot date, and what to tell the user.

    `per_snapshot` covers every snapshot date that was labelled, including the ones dropped, because
    the build review screen has to be able to show *why* a date is missing from the dataset; `frame`
    holds only the rows that survived.
    """

    frame: pd.DataFrame
    per_snapshot: tuple[SnapshotStat, ...]
    checks: tuple[OnboardingCheck, ...]
    excluded: int = 0
    """Rows a lapse label left out because of an event in one of its `exclude_roles` (Plan J M93)."""


# ---------------------------------------------------------------------------
# The one place the future window is written
# ---------------------------------------------------------------------------
def future_window_clause(horizon_days: int, *, inclusive: bool, event_alias: str = EVENT_ALIAS) -> str:
    """The join condition that confines a label to events strictly after the snapshot date.

    The only function in this module that writes a comparison between `event_time` and
    `snapshot_date`. Everything else interpolates what this returns, and `compile_label_query`
    checks the returned SQL still contains it, so a future edit cannot quietly widen the window
    back over the past - which would not fail any test about counts, it would just make the label
    a summary of what already happened.

    `horizon_days` is the whole window, grace period included (`LabelDefinition.window_days`).
    `event_alias` names the event table the clause is about; only the exclusion subqueries of a
    lapse label (`exclude_roles`) pass anything but the default.
    """
    after = ">" if inclusive else ">="
    until = "<=" if inclusive else "<"
    return (
        f"{event_alias}.event_time {after} {SNAPSHOT_ALIAS}.snapshot_date "
        f"AND {event_alias}.event_time {until} "
        f"{SNAPSHOT_ALIAS}.snapshot_date + INTERVAL {horizon_days} DAY"
    )


EXCLUDED_COLUMN: Final[str] = "__label_excluded"
"""The flag column a lapse label with `exclude_roles` selects beside the label: 1 for a row left out
because the customer had an event in one of those tables inside the window. `build_labels` drops the
flagged rows and the column; it never reaches a dataset."""

EXCLUDE_ALIAS: Final[str] = "x"
"""The event alias of the exclusion subqueries, so their window cannot be mistaken for the label's."""


# ---------------------------------------------------------------------------
# SQL fragments: identifiers, literals, the where filter, the expression
# ---------------------------------------------------------------------------
def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    raise LabelError(
        "LABEL_VALUE_NOT_COMPARABLE",
        f"A label filter cannot compare a column against a {type(value).__name__}.",
        "Compare against a number, a piece of text or true/false.",
    )


_WHERE_OPERATORS: Final[dict[WhereOp, str]] = {
    WhereOp.EQ: "=",
    WhereOp.NE: "<>",
    WhereOp.GT: ">",
    WhereOp.GTE: ">=",
    WhereOp.LT: "<",
    WhereOp.LTE: "<=",
}


def _where_sql(where: WhereClause) -> str:
    column = f"{EVENT_ALIAS}.{_identifier(where.column)}"
    if where.op is WhereOp.IS_NULL:
        return f"{column} IS NULL"
    if where.op is WhereOp.NOT_NULL:
        return f"{column} IS NOT NULL"
    if where.op is WhereOp.IN:
        values = ", ".join(_literal(value) for value in where.value)
        return f"{column} IN ({values})"
    return f"{column} {_WHERE_OPERATORS[where.op]} {_literal(where.value)}"


_COMPARISONS: Final[dict[type[ast.cmpop], str]] = {
    ast.Eq: "=",
    ast.NotEq: "<>",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}

_ARITHMETIC: Final[dict[type[ast.operator], str]] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
}


def _expression_sql(node: ast.AST) -> str:
    """Re-emit one whitelisted `ast` node as SQL, refusing everything not explicitly listed.

    A `value_threshold` label is the one place a user's own text decides a row's outcome, so it is
    parsed and rebuilt rather than pasted: a node is a comparison, an `and`/`or`/`not`, arithmetic,
    a literal or a bare column name, or it is a coded failure before any SQL exists. There is no
    branch that passes an unrecognised node through, which is what keeps a subscript, an attribute
    or a function call from reaching DuckDB at all.
    """
    if isinstance(node, ast.Expression):
        return _expression_sql(node.body)
    if isinstance(node, ast.Constant):
        if node.value is None:
            raise LabelError(
                "LABEL_EXPRESSION_NULL_LITERAL",
                "A label expression cannot compare a column against 'None'; nothing equals unknown, "
                "so the test would never be true for any row.",
                "Use the label's filter with 'is_null' or 'not_null' to test for a missing value.",
            )
        return _literal(node.value)
    if isinstance(node, ast.Name):
        return f"{EVENT_ALIAS}.{_identifier(node.id)}"
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise LabelError(
                "LABEL_EXPRESSION_CHAINED_COMPARISON",
                "A label expression compares two things at a time.",
                "Write 'a > 1 and a < 10' rather than '1 < a < 10'.",
            )
        operator = _COMPARISONS.get(type(node.ops[0]))
        if operator is None:
            raise LabelError(
                "LABEL_EXPRESSION_DISALLOWED_OPERATOR",
                f"'{type(node.ops[0]).__name__}' is not a comparison a label expression may use.",
                "Use one of: ==, !=, <, <=, > or >=.",
            )
        return f"({_expression_sql(node.left)} {operator} {_expression_sql(node.comparators[0])})"
    if isinstance(node, ast.BoolOp):
        joiner = " AND " if isinstance(node.op, ast.And) else " OR "
        return "(" + joiner.join(_expression_sql(value) for value in node.values) + ")"
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return f"(NOT {_expression_sql(node.operand)})"
        if isinstance(node.op, ast.USub):
            return f"(-{_expression_sql(node.operand)})"
        if isinstance(node.op, ast.UAdd):
            return _expression_sql(node.operand)
    if isinstance(node, ast.BinOp):
        operator = _ARITHMETIC.get(type(node.op))
        if operator is not None:
            return f"({_expression_sql(node.left)} {operator} {_expression_sql(node.right)})"
    raise LabelError(
        "LABEL_EXPRESSION_DISALLOWED_CONSTRUCT",
        f"'{type(node).__name__}' is not allowed in a label expression.",
        "Use only column names, numbers, text, and, or, not, +, -, *, / and comparisons.",
    )


def _predicate_sql(expression: str) -> str:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise LabelError(
            "LABEL_EXPRESSION_INVALID",
            f"'{expression}' is not a valid condition.",
            "Check for a typo, a missing operator or an unmatched bracket.",
        ) from exc
    return _expression_sql(tree)


# ---------------------------------------------------------------------------
# Compiling the query
# ---------------------------------------------------------------------------
def _require(value: _T | None, *, field: str, name: str) -> _T:
    """Narrow a field the model's own validator already guarantees for this label type.

    A spec saved by an older engine version is read back without re-running today's validator, so
    this says so in the user's language rather than raising an `AttributeError` three frames deeper
    (the same reason `transforms.apply_transform` narrows `to_type`).
    """
    if value is None:
        raise LabelError(
            "LABEL_SPEC_INCOMPLETE",
            f"The label '{name}' does not say what its {field} is.",
            f"Re-open the label definition and set its {field}, then save it again.",
        )
    return value


def _aggregate_sql(spec: LabelSpec) -> str:
    matched = f"COUNT({EVENT_ALIAS}.entity_key)"
    if spec.type is LabelType.EVENT_PRESENCE:
        return f"CAST({matched} > 0 AS INTEGER)"
    if spec.type is LabelType.EVENT_ABSENCE:
        return f"CAST({matched} = 0 AS INTEGER)"
    predicate = _predicate_sql(_require(spec.expression, field="condition", name=spec.name))
    holds = f"{matched} FILTER (WHERE {predicate})"
    if spec.any_event:
        return f"CAST({holds} > 0 AS INTEGER)"
    # "the condition held for every event" is unanswerable when there were no events, and answering
    # it `1` - which is what a bare `holds = matched` does, both being zero - would mark every
    # entity that did nothing in the window as a positive. NULL is the honest answer and Phase 1
    # validation already knows what to do with an unlabelled row.
    return f"CASE WHEN {matched} = 0 THEN NULL ELSE CAST({holds} = {matched} AS INTEGER) END"


def _select_sql(
    *, value: str, name: str, role: str, conditions: list[str], aggregated: bool, extra: str = ""
) -> str:
    joined = "\n   AND ".join(conditions)
    grouping = f"\nGROUP BY {SNAPSHOT_ALIAS}.entity_key, {SNAPSHOT_ALIAS}.snapshot_date" if aggregated else ""
    return (
        f"SELECT {SNAPSHOT_ALIAS}.entity_key AS entity_key,\n"
        f"       {SNAPSHOT_ALIAS}.snapshot_date AS snapshot_date,\n"
        f"       {value} AS {_identifier(name)}{extra}\n"
        f"FROM snapshots {SNAPSHOT_ALIAS}\n"
        f"LEFT JOIN {_identifier(role)} {EVENT_ALIAS}\n"
        f"    ON {joined}{grouping}"
    )


def compile_label_query(spec: LabelSpec, *, inclusive: bool) -> str:
    """Compile a `LabelSpec` into one query over `snapshots` and the label's role view.

    The result selects `(entity_key, snapshot_date, <label name>)` for every row of `snapshots`,
    including entities with no matching event at all - a `LEFT JOIN`, because "nothing happened" is
    the answer an `event_absence` label is looking for, and an inner join would silently drop
    exactly the rows that are positive.
    """
    if spec.type is LabelType.COLUMN:
        column = _require(spec.column, field="column", name=spec.name)
        return _select_sql(
            value=f"{EVENT_ALIAS}.{_identifier(column)}",
            name=spec.name,
            role=get_roles().entity_role,
            conditions=[f"{EVENT_ALIAS}.entity_key = {SNAPSHOT_ALIAS}.entity_key"],
            aggregated=False,
        )

    _require(spec.horizon_days, field="outcome window", name=spec.name)
    window = _require(spec.window_days, field="outcome window", name=spec.name)
    clause = future_window_clause(window, inclusive=inclusive)
    conditions = [f"{EVENT_ALIAS}.entity_key = {SNAPSHOT_ALIAS}.entity_key", clause]
    if spec.where is not None:
        conditions.append(_where_sql(spec.where))
    value = _aggregate_sql(spec)
    extra = ""
    if spec.exclude_roles:
        excluded = _excluded_sql(spec.exclude_roles, window, inclusive=inclusive)
        value = f"CASE WHEN {excluded} THEN NULL ELSE {value} END"
        extra = f",\n       CAST({excluded} AS INTEGER) AS {_identifier(EXCLUDED_COLUMN)}"
    sql = _select_sql(
        value=value,
        name=spec.name,
        role=_require(spec.role, field="table", name=spec.name),
        conditions=conditions,
        aggregated=True,
        extra=extra,
    )
    if clause not in sql:
        raise LabelError(
            "LABEL_WINDOW_NOT_ENFORCED",
            f"The query for label '{spec.name}' was built without its outcome window, so it would "
            "read the whole history instead of what happens next.",
            "This is an engine fault rather than a problem with the data; send the build id to support.",
        )
    return sql


def _excluded_sql(roles: tuple[str, ...], window: int, *, inclusive: bool) -> str:
    """True when the customer has an event in any of `roles` inside the label's own window."""
    clause = future_window_clause(window, inclusive=inclusive, event_alias=EXCLUDE_ALIAS)
    tests = [
        f"EXISTS (SELECT 1 FROM {_identifier(role)} {EXCLUDE_ALIAS} "
        f"WHERE {EXCLUDE_ALIAS}.entity_key = {SNAPSHOT_ALIAS}.entity_key AND {clause})"
        for role in roles
    ]
    return "(" + " OR ".join(tests) + ")"


# ---------------------------------------------------------------------------
# Building the labels
# ---------------------------------------------------------------------------
def _positive_count(values: pd.Series[Any]) -> int | None:
    """How many of the non-null `values` are the positive class, or None when that is not knowable.

    A derived label is always 0, 1 or NULL, but a `column` label carries whatever the client stores
    - "Y"/"N", "active"/"lapsed" - and which of the two means "the event happened" is Phase 1's
    question, answered from the run config. Guessing it here to fill in a positive rate would be a
    number nobody measured, so the rate is left unreported for such a column, and for a snapshot
    whose every label came back NULL.
    """
    import pandas as pd

    if values.empty:
        return None
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.isna().any() or not bool(numeric.isin((0, 1)).all()):
        return None
    return int((numeric == 1).sum())


def _empty_frame(snapshots: pd.DataFrame, name: str) -> pd.DataFrame:
    frame = snapshots.iloc[0:0][["entity_key", "snapshot_date"]].copy()
    frame[name] = None
    return frame


def _data_end(con: duckdb.DuckDBPyConnection, role: str) -> Any:
    """The last `event_time` anywhere in the label role - how far the client's extract runs."""
    row = con.execute(f"SELECT max(event_time) FROM {_identifier(role)}").fetchone()
    return None if row is None else row[0]


def _role_missing(snapshots: pd.DataFrame, spec: LabelSpec, role: str, *, excluded: bool) -> LabelResult:
    """LABEL_ROLE_MISSING: the label's own table, or a table it leaves customers out by, is not mapped."""
    if excluded:
        message = (
            f"The outcome '{spec.name}' leaves out customers with an event in the {role} table, which "
            f"has not been mapped, so no row can be labelled."
        )
        suggestion = (
            f"Upload the {role} table and map its columns, or take it off the outcome's list of tables "
            f"that leave a customer out."
        )
    else:
        message = (
            f"The outcome '{spec.name}' is worked out from the {role} table, which has "
            f"not been mapped, so no row can be labelled."
        )
        suggestion = (
            f"Upload the {role} table and map its columns, or choose an outcome that is "
            f"worked out from a table you already have."
        )
    return LabelResult(
        frame=_empty_frame(snapshots, spec.name),
        per_snapshot=(),
        checks=(
            OnboardingCheck(
                code="LABEL_ROLE_MISSING",
                severity=Severity.ERROR,
                message=message,
                suggestion=suggestion,
                details={"role": role, "label": spec.name, **({"excluded_role": True} if excluded else {})},
            ),
        ),
    )


def build_labels(
    con: duckdb.DuckDBPyConnection,
    spec: LabelSpec,
    snapshots: pd.DataFrame,
    *,
    drop_censored: bool,
    mapped_roles: Collection[str],
    mode: SnapshotMode,
    inclusive: bool,
) -> LabelResult:
    """Label every row of `snapshots`, then drop the snapshot dates that cannot honestly carry one.

    `snapshots` is registered as the `snapshots` view the compiled query runs against, so the rows
    labelled are exactly the ones handed in rather than whatever was last left under that name.

    `inclusive` is `SnapshotDefinition.inclusive_snapshot_time` and is required rather than
    defaulted: it has to be the same value the features were built with, and a default would let
    the two halves of one boundary disagree without anything failing.
    """
    import duckdb
    import pandas as pd

    if spec.type is LabelType.COLUMN and mode is SnapshotMode.PERIODIC:
        dates = int(pd.Series(snapshots["snapshot_date"]).nunique())
        return LabelResult(
            frame=_empty_frame(snapshots, spec.name),
            per_snapshot=(),
            checks=(
                OnboardingCheck(
                    code="ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED",
                    severity=Severity.ERROR,
                    message=(
                        f"The outcome '{spec.name}' is read from the column '{spec.column}', which "
                        f"holds one value per customer rather than one per date, so it cannot say "
                        f"what the outcome was at each of {dates} different snapshot dates."
                    ),
                    suggestion=(
                        "Build a single snapshot instead, or define the outcome from an event table "
                        "and an outcome window."
                    ),
                    column=spec.column,
                    details={"snapshot_dates": dates, "label": spec.name},
                ),
            ),
        )

    role = (
        get_roles().entity_role
        if spec.type is LabelType.COLUMN
        else _require(spec.role, field="table", name=spec.name)
    )
    if role not in mapped_roles:
        return _role_missing(snapshots, spec, role, excluded=False)
    for excluded_role in spec.exclude_roles:
        if excluded_role not in mapped_roles:
            return _role_missing(snapshots, spec, excluded_role, excluded=True)

    sql = compile_label_query(spec, inclusive=inclusive)
    con.register("snapshots", snapshots)
    try:
        labelled = con.execute(sql).df()
    except duckdb.Error as exc:
        raise LabelError(
            "LABEL_QUERY_FAILED",
            f"The outcome '{spec.name}' could not be worked out from the {role} table.",
            f"Check that every column the outcome names is a mapped column of the {role} table.",
        ) from exc

    excluded = 0
    if EXCLUDED_COLUMN in labelled.columns:
        flagged = labelled[EXCLUDED_COLUMN].fillna(0).astype(bool)
        excluded = int(flagged.sum())
        labelled = labelled.loc[~flagged].drop(columns=[EXCLUDED_COLUMN]).reset_index(drop=True)

    window = spec.window_days
    horizon = None if window is None else pd.Timedelta(days=window)
    end = None if horizon is None else _data_end(con, role)
    days = pd.to_datetime(labelled["snapshot_date"]).dt.date

    stats: list[SnapshotStat] = []
    degenerate_checks: list[OnboardingCheck] = []
    dropped: set[datetime.date] = set()
    censored_count = 0

    for day in sorted(days.unique()):
        rows = labelled[days == day]
        values = rows[spec.name].dropna()
        positives = _positive_count(values)
        # `end is None` is an empty label table: every window is unobserved, not every window empty.
        censored = horizon is not None and (end is None or pd.Timestamp(day) + horizon > pd.Timestamp(end))
        reason: str | None = None
        if censored:
            censored_count += 1
            if drop_censored:
                reason = "the outcome window is not complete yet"
        if reason is None and not values.empty and int(values.nunique()) == 1:
            reason = "every row has the same outcome"
            degenerate_checks.append(
                OnboardingCheck(
                    code="LABEL_DEGENERATE_SNAPSHOT",
                    severity=Severity.WARNING,
                    message=(
                        f"Every one of the {len(rows):,} rows at {day} has the same outcome for "
                        f"'{spec.name}', so that snapshot date was dropped: there is nothing there "
                        f"for a model to tell apart."
                    ),
                    suggestion=(
                        "Use a longer history, or widen the outcome window so that both outcomes "
                        "appear at this date."
                    ),
                    details={"snapshot_date": str(day), "rows": len(rows), "label": spec.name},
                )
            )
        if reason is not None:
            dropped.add(day)
        stats.append(
            SnapshotStat(
                date=day,
                entities=int(rows["entity_key"].nunique()),
                positives=positives,
                positive_rate=None if positives is None else round(positives / len(values), 4),
                censored=censored,
                dropped_reason=reason,
            )
        )

    checks: list[OnboardingCheck] = []
    if censored_count and drop_censored:
        checks.append(
            OnboardingCheck(
                code="LABEL_HORIZON_CENSORED",
                severity=Severity.WARNING,
                message=(
                    f"{censored_count} of {len(stats)} snapshot dates were dropped because the "
                    f"outcome window is not complete yet."
                ),
                suggestion=(
                    f"Upload data that runs at least {window} days past the last "
                    f"snapshot date, or shorten the outcome window of '{spec.name}'."
                ),
                details={
                    "censored": censored_count,
                    "snapshot_dates": len(stats),
                    "horizon_days": spec.horizon_days,
                    **({"grace_days": spec.grace_days} if spec.grace_days else {}),
                    "data_end": None if end is None else str(pd.Timestamp(end).date()),
                },
            )
        )
    checks.extend(degenerate_checks)

    frame = labelled[~days.isin(dropped)].reset_index(drop=True)
    return LabelResult(frame=frame, per_snapshot=tuple(stats), checks=tuple(checks), excluded=excluded)
