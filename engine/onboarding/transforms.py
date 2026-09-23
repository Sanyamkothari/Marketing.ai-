"""Mapping transforms (M9, Phase 2 plan section 6.1): pure functions from a raw client column to a
standard one.

A `ColumnTransform` is *saved* - it sits inside a `MappingSpec` on disk - so applying it has to mean
the same thing every time it runs: no clock, no client id, no randomness, nothing read from outside
`series`/`frame` and the transform itself. That is what "replayable" buys (plan section 6.1): a
client's next month's file gets the same mapping and the same result, without a human touching it
again. Every function below is pure for exactly that reason.

Two shapes of transform live here. Most (`cast`, `value_map`, `negate`, `scale`, `strip`, `lower`,
`lstrip_zeros`) run on one column at a time and share one entry point, `apply_transform`. Two do not:
`dedupe` collapses several rows of a table into one per entity, and `derive` computes a new column
from several others - both need the whole mapped frame, not a `pd.Series`, so they are
`apply_dedupe` and `derive`, called directly by the build stage rather than through
`apply_transform`. `ColumnTransform` still carries both as `TransformKind` members (so a mapping can
name them and the UI can render them alongside the rest), but `apply_transform` refuses them with a
coded error that names the function to call instead, rather than silently doing nothing.

`cast_series` is the one place a raw value becomes a `StandardColumn`'s `StandardType`; `cast`
transforms call it, and so does the mapping screen's own type-conflict check (MAPPING_TYPE_CONFLICT,
plan section 7), because a cast that would fail is exactly what that check is measuring. `failed` and
`total` are both counted over *non-null* input only: a column that is legitimately mostly empty must
not have that emptiness dilute the failure rate of the values the cast actually had to parse.

`derive` is the one place a user-authored expression runs (plan section 13, rule 13: users never
write SQL or arbitrary Python). It parses with `ast`, walks the tree once, and accepts only a
closed, explicit list of node shapes - a whitelisted function call, a column name, a number or
string literal, and `+ - * /`. Anything else - attribute access, a subscript, a lambda, a
comprehension, a call to a function not on the list - is rejected with a coded `TransformError`
before it is ever evaluated. Nothing here calls Python's own dynamic-evaluation builtins on user
text; the tree is walked and computed node by node against a fixed table of named functions instead.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from engine.onboarding.specs import ColumnTransform, StandardType, TransformKind

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pandas as pd


__all__ = [
    "TransformError",
    "TransformResult",
    "apply_dedupe",
    "apply_transform",
    "cast_series",
    "derive",
]


# ---------------------------------------------------------------------------
# Errors and the result shape every transform returns
# ---------------------------------------------------------------------------
class TransformError(Exception):
    """A transform could not run as configured.

    `code` is one of the `TRANSFORM_*`, `DEDUPE_*` or `DERIVE_*` families defined by the raises
    below; `message` and `suggestion` are what plan section 13 rule 3's plain-language-failures rule
    puts on screen - never a Python traceback, exactly as `engine.stages.validate` never lets a bad
    frame raise and `engine.clients.ClientStoreError` never lets a store failure raise unlabelled.
    """

    def __init__(self, code: str, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


@dataclass(frozen=True)
class TransformResult:
    """What running one transform over one column produced.

    `total` and `failed` are both counts of *non-null input values* - a null cell is a fact about
    the client's data, not something this transform tried and failed at, so it is never counted
    either way. `failed / total` is exactly the ratio MAPPING_TYPE_CONFLICT compares to 5% (plan
    section 7): "5% of the values we tried to make sense of failed", not "5% of all rows, however
    many were simply empty". `unmapped` is populated by `value_map` alone - the distinct source
    values, sorted for a stable screen, that the map did not list - so the mapping UI can show them
    and let the user extend the map without having to go back to the raw file to find them.
    """

    values: pd.Series[Any]
    failed: int
    total: int
    unmapped: tuple[str, ...] = ()


def _map_non_null(series: pd.Series[Any], fn: Callable[[Any], Any]) -> pd.Series[Any]:
    """Apply `fn` to every non-null cell of `series`, leaving null cells null.

    The one-line body hides the reason this exists at all: a transform must never turn "missing"
    into "some other value" behind the user's back (plan section 2, "nothing fabricated" extends to
    silently manufacturing a non-null result for a cell that was never populated).
    """
    import pandas as pd

    return series.map(lambda value: value if pd.isna(value) else fn(value))


# ---------------------------------------------------------------------------
# cast - the shared caster behind the `cast` transform and mapping's type check
# ---------------------------------------------------------------------------
_BOOLEAN_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"y", "yes", "true", "t", "1"})
_BOOLEAN_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"n", "no", "false", "f", "0"})
"""Case-insensitive, whitespace-trimmed tokens `cast_series` accepts as boolean (plan section 6.1's
own example, DND_FLAG, ships as Y/N; other clients ship 1/0, true/false, yes/no)."""


def _parse_boolean_token(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in _BOOLEAN_TRUE_TOKENS:
        return True
    if token in _BOOLEAN_FALSE_TOKENS:
        return False
    return float("nan")


def cast_series(
    series: pd.Series[Any], to: StandardType, *, date_format: str | None = None
) -> TransformResult:
    """Cast `series` to `to`, the way both a `cast` transform and mapping's own type check need.

    `date_format` is an explicit `strptime` format, honoured when given rather than inferred,
    because a date column read as dd/mm/yyyy and one read as mm/dd/yyyy often both parse under
    inference and disagree silently on every ambiguous day - `ColumnTransform.date_format` exists
    precisely so the engine never has to guess which one a client meant (DATE_FORMAT_AMBIGUOUS,
    plan section 7). Without one, dates are parsed per-value rather than by one format for the whole
    column, which is the best a caller who did not resolve the ambiguity can do.
    """
    import pandas as pd

    if to is StandardType.NUMERIC:
        values = pd.to_numeric(series, errors="coerce")
    elif to is StandardType.BOOLEAN:
        values = _map_non_null(series, _parse_boolean_token)
    elif to is StandardType.DATE:
        if date_format is not None:
            values = pd.to_datetime(series, format=date_format, errors="coerce")
        else:
            values = pd.to_datetime(series, errors="coerce", format="mixed")
    else:  # StandardType.CATEGORICAL, StandardType.TEXT - the schema tells them apart, not the cast
        values = _as_text(series)

    total = int(series.notna().sum())
    failed = int((series.notna() & values.isna()).sum())
    return TransformResult(values=values, failed=failed, total=total, unmapped=())


def _as_text(series: pd.Series[Any]) -> pd.Series[Any]:
    """`str()` of every non-null cell, nulls left as they were - `_map_non_null(series, str)`.

    Two shapes of column get the same answer without a Python call per cell, because for them the
    answer is known: a column of integers has no nulls and `astype(str)` spells each one exactly as
    `str()` does, and a column read from a CSV whose every value is already text is its own answer,
    since `str()` of a string is that string. They are the common case - an id column, a plan code,
    a region - and on a five-million-row event table the per-cell call was most of the mapping
    stage's own time (docs/PERFORMANCE.md). Anything else takes the per-cell path it always took.
    """
    import numpy as np
    import pandas as pd

    if isinstance(series.dtype, np.dtype) and series.dtype.kind in "iu":
        return series.astype(str).astype(object)
    if series.dtype == object and pd.api.types.infer_dtype(series, skipna=True) == "string":
        return series.copy()
    return _map_non_null(series, str)


# ---------------------------------------------------------------------------
# The other per-column kinds
# ---------------------------------------------------------------------------
def _strip_token(value: Any) -> str:
    return str(value).strip()


def _lower_token(value: Any) -> str:
    return str(value).lower()


def _lstrip_zeros_token(value: Any) -> str:
    """Text key normalisation: `"007"` -> `"7"`, and an all-zero key never disappears to `""`."""
    stripped = str(value).strip().lstrip("0")
    return stripped or "0"


def _simple_text_transform(series: pd.Series[Any], fn: Callable[[Any], str]) -> TransformResult:
    values = _map_non_null(series, fn)
    total = int(series.notna().sum())
    failed = int((series.notna() & values.isna()).sum())
    return TransformResult(values=values, failed=failed, total=total, unmapped=())


def _apply_value_map(series: pd.Series[Any], transform: ColumnTransform) -> TransformResult:
    """A source value becomes the standard value `transform.value_map` says it means.

    A value the map does not list is either kept as-is or nulled, per `transform.unmapped` - but it
    is always collected into `unmapped`, whichever policy is in force, because the mapping screen
    needs the full list to let the user extend the map rather than making them re-open the raw file
    to find what was missed. `failed` counts only the cells the "null" policy actually discarded: an
    explicit `{"UNKNOWN": null}` entry is a value the user deliberately chose to blank out, not a
    cast the transform failed at, and MAPPING_TYPE_CONFLICT must not fire on a deliberate decision.
    """
    value_map = transform.value_map
    keep_unmapped = transform.unmapped == "keep"
    seen_unmapped: set[str] = set()
    failed = 0

    def convert(value: Any) -> Any:
        nonlocal failed
        key = str(value)
        if key in value_map:
            return value_map[key]
        seen_unmapped.add(key)
        if keep_unmapped:
            return value
        failed += 1
        return None

    values = _map_non_null(series, convert)
    total = int(series.notna().sum())
    return TransformResult(values=values, failed=failed, total=total, unmapped=tuple(sorted(seen_unmapped)))


def _apply_negate(series: pd.Series[Any]) -> TransformResult:
    """Boolean inversion - the whole of how a client's DND_FLAG becomes our marketing_opt_in.

    A `ColumnTransform` carries exactly one `kind`, so there is no separate `cast` step to chain in
    front of `negate`; it reads the same raw-token vocabulary `cast_series` does for `boolean` and
    inverts the result in the same pass, rather than requiring a mapping to spend two transform
    slots (which do not exist) on one column.
    """
    cast_result = cast_series(series, StandardType.BOOLEAN)
    inverted = _map_non_null(cast_result.values, lambda value: not value)
    return TransformResult(values=inverted, failed=cast_result.failed, total=cast_result.total, unmapped=())


def _apply_scale(series: pd.Series[Any], factor: float) -> TransformResult:
    cast_result = cast_series(series, StandardType.NUMERIC)
    scaled = cast_result.values * factor
    return TransformResult(values=scaled, failed=cast_result.failed, total=cast_result.total, unmapped=())


def apply_transform(series: pd.Series[Any], transform: ColumnTransform, *, column: str) -> TransformResult:
    """Run one saved `ColumnTransform` over one mapped column.

    `dedupe` and `derive` reshape or combine whole columns rather than transforming one column in
    place - they are not per-column operations, so they are refused here with a coded error naming
    the function to call instead (`apply_dedupe`, `derive`); the build stage is the only caller with
    the whole frame those two need. Everything else in `TransformKind` is handled directly.
    """
    if transform.kind is TransformKind.CAST:
        if transform.to_type is None:
            # Unreachable for a `ColumnTransform` built today - `_shape` requires `to_type` on a
            # cast - but a spec saved by an older engine version is read back without re-running
            # today's validator, so this narrows the Optional rather than trusting the past.
            raise TransformError(
                "TRANSFORM_MISSING_TO_TYPE",
                f"The cast transform on '{column}' does not name a target type.",
                "Re-save the mapping; a cast transform always names to_type.",
            )
        return cast_series(series, transform.to_type, date_format=transform.date_format)
    if transform.kind is TransformKind.VALUE_MAP:
        return _apply_value_map(series, transform)
    if transform.kind is TransformKind.NEGATE:
        return _apply_negate(series)
    if transform.kind is TransformKind.SCALE:
        if transform.factor is None:
            raise TransformError(
                "TRANSFORM_MISSING_FACTOR",
                f"The scale transform on '{column}' does not name a factor.",
                "Re-save the mapping; a scale transform always names a factor.",
            )
        return _apply_scale(series, transform.factor)
    if transform.kind is TransformKind.STRIP:
        return _simple_text_transform(series, _strip_token)
    if transform.kind is TransformKind.LOWER:
        return _simple_text_transform(series, _lower_token)
    if transform.kind is TransformKind.LSTRIP_ZEROS:
        return _simple_text_transform(series, _lstrip_zeros_token)
    raise TransformError(
        "TRANSFORM_NOT_PER_COLUMN",
        f"A {transform.kind.value} transform reshapes the whole table, not the single column '{column}'.",
        "Call apply_dedupe for a dedupe transform, or derive for a derive transform.",
    )


# ---------------------------------------------------------------------------
# dedupe - operates on a frame, not a column
# ---------------------------------------------------------------------------
def apply_dedupe(frame: pd.DataFrame, *, key: str, by: str) -> tuple[pd.DataFrame, int]:
    """Keep exactly one row per distinct `key`: the row with the latest `by` date.

    A client's raw extract often carries one row per statement or event rather than one per entity
    (a billing table with a row per bill, not per customer); the build stage needs one row per
    entity before it can register a mapped table as a role view (the cross-module contract every
    other M-task builds against). "Latest" is the only ordering a client-agnostic dedupe can use
    without inventing a preference nobody configured; a `by` value that fails to parse as a date, or
    a tie, keeps the row that appears later in the file - deterministic without being a second thing
    a client has to configure.
    """
    import pandas as pd

    if key not in frame.columns:
        raise TransformError(
            "DEDUPE_KEY_MISSING",
            f"Dedupe key column '{key}' is not in this table.",
            f"Map a source column to '{key}' before deduping the table on it.",
        )
    if by not in frame.columns:
        raise TransformError(
            "DEDUPE_ORDER_COLUMN_MISSING",
            f"Dedupe order column '{by}' is not in this table.",
            f"Map a source column to '{by}' before deduping on it.",
        )

    before = len(frame)
    working = frame.copy()
    working["__mai_row_order__"] = list(range(before))
    working["__mai_order_key__"] = pd.to_datetime(frame[by], errors="coerce").to_numpy()
    working = working.sort_values(
        ["__mai_order_key__", "__mai_row_order__"], na_position="first", kind="stable"
    )
    kept = working.drop_duplicates(subset=key, keep="last").sort_values("__mai_row_order__", kind="stable")
    result = kept.drop(columns=["__mai_row_order__", "__mai_order_key__"]).reset_index(drop=True)
    return result, before - len(result)


# ---------------------------------------------------------------------------
# derive - a whitelisted expression language, never eval/exec (plan section 13, rule 13)
# ---------------------------------------------------------------------------
def _derive_year(value: Any) -> Any:
    import pandas as pd

    parsed = pd.to_datetime(value, errors="coerce")
    if isinstance(parsed, pd.Series):
        return parsed.dt.year.astype("float64")
    return float("nan") if pd.isna(parsed) else float(parsed.year)


def _derive_month(value: Any) -> Any:
    import pandas as pd

    parsed = pd.to_datetime(value, errors="coerce")
    if isinstance(parsed, pd.Series):
        return parsed.dt.month.astype("float64")
    return float("nan") if pd.isna(parsed) else float(parsed.month)


def _derive_months_between(a: Any, b: Any) -> Any:
    """Calendar months from `b` to `a`, ignoring the day of month (a tenure count, not a duration)."""
    return (_derive_year(a) * 12 + _derive_month(a)) - (_derive_year(b) * 12 + _derive_month(b))


def _derive_days_between(a: Any, b: Any) -> Any:
    import pandas as pd

    diff = pd.to_datetime(a, errors="coerce") - pd.to_datetime(b, errors="coerce")
    if isinstance(diff, pd.Series):
        return diff.dt.days.astype("float64")
    return float("nan") if pd.isna(diff) else float(diff.days)


def _derive_coalesce(a: Any, b: Any) -> Any:
    import pandas as pd

    if isinstance(a, pd.Series):
        return a.where(a.notna(), b)
    return b if pd.isna(a) else a


def _derive_lower(value: Any) -> Any:
    import pandas as pd

    if isinstance(value, pd.Series):
        return _map_non_null(value, _lower_token)
    return value if pd.isna(value) else _lower_token(value)


def _derive_abs(value: Any) -> Any:
    import pandas as pd

    if isinstance(value, pd.Series):
        return value.abs()
    return abs(value)


_DERIVE_FUNCTIONS: Final[Mapping[str, Callable[..., Any]]] = MappingProxyType(
    {
        "months_between": _derive_months_between,
        "days_between": _derive_days_between,
        "year": _derive_year,
        "month": _derive_month,
        "coalesce": _derive_coalesce,
        "lower": _derive_lower,
        "abs": _derive_abs,
    }
)
"""The whole vocabulary a derive expression may call. Adding a function is a one-line change here;
nothing about the whitelist walk below has to know the new name exists."""

_DERIVE_ARITY: Final[Mapping[str, int]] = MappingProxyType(
    {"months_between": 2, "days_between": 2, "year": 1, "month": 1, "coalesce": 2, "lower": 1, "abs": 1}
)

_BINARY_OPERATORS: Final[Mapping[type[ast.operator], Callable[[Any, Any], Any]]] = MappingProxyType(
    {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
)
"""Arithmetic a derive expression may use on columns and literals. Deliberately small: comparisons,
boolean logic and exponentiation are not features anyone has asked for, and every operator added
here is one more thing the whitelist walk has to keep safe."""


def _eval_node(node: ast.AST, env: dict[str, Any]) -> Any:
    """Evaluate one `ast` node against `env`, rejecting anything not explicitly whitelisted.

    This is the whole of the whitelist (plan section 13, rule 13): a node is a whitelisted call, a
    column name, a literal, a unary +/-, or `+ - * /` between two whitelisted nodes - or it is a
    coded `TransformError` naming the offending construct before anything is computed. There is no
    fallback branch that computes something for a node this function does not recognise; recursing
    into `node.args`/`node.left`/`node.right` is what lets `__import__(...)`, `os.system(...)` and a
    subscript or comprehension all fail here rather than three frames deeper in a library.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, (bool, int, float, str)):
            return node.value
        raise TransformError(
            "DERIVE_DISALLOWED_CONSTANT",
            f"{node.value!r} is not a kind of literal a derive expression may use.",
            "Use a number, a string, true/false or a plain column name.",
        )
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise TransformError(
                "DERIVE_UNKNOWN_NAME",
                f"'{node.id}' is not a column of this table.",
                "Spell a mapped standard column, or use 'snapshot_date' for the row's snapshot date.",
            )
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return _eval_node(node.operand, env)
        raise TransformError(
            "DERIVE_DISALLOWED_OPERATOR",
            f"'{type(node.op).__name__}' is not an allowed unary operator.",
            "Use only a leading + or - before a number or column.",
        )
    if isinstance(node, ast.BinOp):
        op = _BINARY_OPERATORS.get(type(node.op))
        if op is None:
            raise TransformError(
                "DERIVE_DISALLOWED_OPERATOR",
                f"'{type(node.op).__name__}' is not an allowed operator.",
                "Use only +, -, * or / between columns and numbers.",
            )
        return op(_eval_node(node.left, env), _eval_node(node.right, env))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise TransformError(
                "DERIVE_DISALLOWED_CALL",
                "Only a plain function name may be called in a derive expression.",
                f"Use one of: {', '.join(sorted(_DERIVE_FUNCTIONS))}.",
            )
        name = node.func.id
        fn = _DERIVE_FUNCTIONS.get(name)
        if fn is None:
            raise TransformError(
                "DERIVE_UNKNOWN_FUNCTION",
                f"'{name}' is not one of the functions a derive expression may call.",
                f"Use one of: {', '.join(sorted(_DERIVE_FUNCTIONS))}.",
            )
        if node.keywords:
            raise TransformError(
                "DERIVE_DISALLOWED_CALL",
                f"'{name}' may not be called with keyword arguments.",
                "Pass arguments positionally.",
            )
        expected = _DERIVE_ARITY[name]
        if len(node.args) != expected:
            raise TransformError(
                "DERIVE_WRONG_ARITY",
                f"'{name}' takes {expected} argument(s), got {len(node.args)}.",
                f"Call {name} with exactly {expected} argument(s).",
            )
        return fn(*(_eval_node(arg, env) for arg in node.args))
    raise TransformError(
        "DERIVE_DISALLOWED_CONSTRUCT",
        f"'{type(node).__name__}' is not allowed in a derive expression.",
        "Use only column names, numbers, +, -, *, / and the listed functions.",
    )


def derive(frame: pd.DataFrame, expression: str, *, snapshot_column: str) -> pd.Series[Any]:
    """Compute one derived column from `frame` by a whitelisted expression (plan section 13, rule 13).

    Every column of `frame` is a name the expression may use; `snapshot_column` is additionally
    bound to the fixed name `snapshot_date`, so an expression such as
    `months_between(snapshot_date, signup_date)` never has to know which of the client's own
    columns holds the row's snapshot date - the caller names it once, here, rather than every
    expression having to spell out whatever the mapping happened to call it.
    """
    import pandas as pd

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise TransformError(
            "DERIVE_INVALID_EXPRESSION",
            f"'{expression}' is not a valid expression.",
            "Check for a typo, a missing operator or an unmatched bracket.",
        ) from exc
    if snapshot_column not in frame.columns:
        raise TransformError(
            "DERIVE_SNAPSHOT_COLUMN_MISSING",
            f"Snapshot column '{snapshot_column}' is not in this table.",
            "Pass the column that holds each row's snapshot date.",
        )

    env: dict[str, Any] = {name: frame[name] for name in frame.columns}
    env["snapshot_date"] = frame[snapshot_column]
    result = _eval_node(tree, env)
    if isinstance(result, pd.Series):
        return result
    return pd.Series(result, index=frame.index)
