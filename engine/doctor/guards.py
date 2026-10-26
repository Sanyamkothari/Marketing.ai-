"""The guards every data-changing step goes through: numbers our code counts, never the AI's word.

One small module, used by typed recipe steps, the SQL step and the combine steps (a join or a stack). It reads
frames and counts; it has no state a step can reach, and nothing in it is offered to the AI as a tool.

* **A receipt per step** (:func:`make_receipt`): rows in from each table, rows out, customers (distinct IDs) in and
  out, customers **lost** and customers **repeated** counted apart and never netted off, the total and count of
  every number column before and after, the outcome rate before and after, and which outcome value the lost
  customers had.
* **Findings** at three levels: `stop` (a person must answer before the plan goes on), `flag` (shown on the approval
  page) and `note` (kept in the log). :func:`triage` keeps the interruptions to a handful.
* **Join cardinality**: if the table was one row per customer and the step makes a customer appear twice, it stops
  with the count.
* **Change measure** (:func:`cell_change`): for a step that makes a new table, each column that has the same name
  as before is compared customer by customer. A step that cannot be measured raises :class:`NotMeasurableError`; the
  caller treats it as "needs a yes", never as 0%.
* **A running ledger** (:func:`build_ledger`): customers after each step, the share lost over the whole plan, the
  outcome rate before and after, and where the loss falls. Over 5% lost, or the rate moving more than a point,
  the plan needs a yes that names the steps that cost most.
* **Expectations** (:class:`ExpectationBook`): checks a step declares before it runs, locked by hash, checked by
  code after, every failure returned at once. Two retries per step, then stop and ask. An expectation changed after
  a run is flagged.
* **Run twice** (:func:`run_guarded`): the plan runs twice and every step must give the same rows, or it stops.
* **An empty result stops**: no rows, or a column that came out empty, is an automatic stop.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import pandas as pd
from pandas.api import types as pdt
from pydantic import Field

from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.recipe import RecipeRun, run_recipe
from engine.agent.sql_step import THIS_TABLE, SqlStepError, group_by_column, sql_params
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import StrictBase

__all__ = [
    "LOSS_STOP_PCT",
    "RATE_STOP_POINTS",
    "RETRY_CAP",
    "ExpectationBook",
    "ExpectationOutcome",
    "Expectations",
    "Finding",
    "GuardedRun",
    "Ledger",
    "NotMeasurableError",
    "Receipt",
    "Triage",
    "acknowledge",
    "build_ledger",
    "cell_change",
    "frame_hash",
    "grouping_key",
    "infer_key",
    "make_receipt",
    "names_a_log_row",
    "plan_findings",
    "receipt_findings",
    "run_guarded",
    "triage",
]

Level = Literal["stop", "flag", "note"]

LOSS_STOP_PCT: Final[float] = 5.0
"""A plan that loses more than this share of its customers needs a yes."""
RATE_STOP_POINTS: Final[float] = 1.0
"""A plan that moves the outcome rate by more than this many points needs a yes."""
RETRY_CAP: Final[int] = 2
"""A step whose expectations fail may be tried again twice; then a person is asked."""
MAX_STOPS_SHOWN: Final[int] = 5
MAX_TOTAL_COLUMNS: Final[int] = 25
_TOP_STEPS: Final[int] = 3
_KEEPS_TOTALS: Final[frozenset[str]] = frozenset(
    {RecipeStepKind.SQL_QUERY.value, RecipeStepKind.COMBINE_ROWS.value, "build_dataset", "stack_files"}
)
"""Steps that re-arrange rows, so a total that moves is news. A step whose job is to change values (a fill, a
cap) or to leave rows out moves totals by design: that is a note, not something on the approval page."""
_TOLERANCE: Final[float] = 1e-9


class NotMeasurableError(Exception):
    """A step whose effect on the values cannot be measured here (no ID to line the rows up by)."""


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
class Finding(StrictBase):
    """One thing the guards found, at one of three levels."""

    level: Level
    code: str
    message: str = Field(description="Plain words with the counts; never a cell value.")
    order: int | None = Field(default=None, description="The step it is about; none for the whole plan.")
    count: int | None = None
    detail: str | None = Field(
        default=None,
        description="The expert's side of `message` (which steps, by outcome); shown behind a click.",
    )

    @property
    def key(self) -> str:
        """What a person's yes is tied to: this finding, at this step, with this count."""
        return sha256_hex(canonical_bytes([self.code, self.order, self.count]))[:16]


def acknowledge(findings: Sequence[Finding], keys: Collection[str]) -> tuple[Finding, ...]:
    """The findings with each stop a person said yes to (by :attr:`Finding.key`) lowered to a flag."""
    return tuple(
        (
            f.model_copy(update={"level": "flag", "message": f"{f.message} (confirmed by a person)"})
            if f.level == "stop" and f.key in keys
            else f
        )
        for f in findings
    )


@dataclass(frozen=True)
class Triage:
    """Findings sorted by what they ask of the person: stops interrupt, flags sit on the approval page."""

    stops: tuple[Finding, ...]
    flags: tuple[Finding, ...]
    notes: tuple[Finding, ...]
    overflow: int = 0
    """Stops beyond the budget; they are still stops, only not listed one by one."""


def triage(findings: Sequence[Finding], *, budget: int = MAX_STOPS_SHOWN) -> Triage:
    stops = tuple(f for f in findings if f.level == "stop")
    return Triage(
        stops=stops[:budget],
        flags=tuple(f for f in findings if f.level == "flag"),
        notes=tuple(f for f in findings if f.level == "note"),
        overflow=max(0, len(stops) - budget),
    )


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------
class ColumnTotal(StrictBase):
    column: str
    before: float
    after: float
    non_null_before: int
    non_null_after: int

    @property
    def kept(self) -> bool:
        return math.isclose(self.before, self.after, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE)


class Receipt(StrictBase):
    """What one step did, counted by our code from the frames before and after."""

    order: int
    kind: str
    rows_in: dict[str, int] = Field(description="Rows of each table the step read.")
    rows_out: int
    key: str | None = Field(
        default=None, description="The ID the customers were counted by; none if unknown."
    )
    keys_in: int | None = None
    keys_out: int | None = None
    keys_lost: int = Field(default=0, description="Customers in the first table and not in the result.")
    keys_repeated: int = Field(default=0, description="Customers who are on more than one row of the result.")
    extra_rows: int = Field(default=0, description="Rows beyond one per customer in the result.")
    keys_new: int = Field(default=0, description="Customers in the result and not in the first table.")
    one_row_per_key: bool | None = Field(
        default=None, description="Whether the first table had one row per customer (rows with no ID aside)."
    )
    extra_rows_in: int = Field(default=0, description="Rows beyond one per customer in the first table.")
    totals: tuple[ColumnTotal, ...] = ()
    empty_columns: tuple[str, ...] = Field(
        default=(), description="Columns that came out with no value at all."
    )
    rate_in: float | None = Field(default=None, description="Outcome rate (percent) before.")
    rate_out: float | None = Field(default=None, description="Outcome rate (percent) after.")
    lost_by_outcome: dict[str, int] = Field(
        default_factory=dict, description="Lost customers by their outcome value."
    )
    grouped_by: str | None = Field(
        default=None,
        description="The column a query groups by, used to count customers when no ID was confirmed.",
    )
    rows_no_key_in: int = Field(default=0, description="Rows of the first table with no ID.")
    rows_no_key_out: int = Field(default=0, description="Rows of the result with no ID.")

    @property
    def combines_rows(self) -> bool:
        """Whether the step made one row per customer out of more rows: rows folded, not rows deleted."""
        return (
            self.keys_in is not None
            and self.kind == RecipeStepKind.SQL_QUERY.value
            and self.extra_rows_in > 0
            and self.keys_repeated == 0
            and self.rows_out < next(iter(self.rows_in.values()), 0)
        )

    @property
    def lost(self) -> int:
        """What the step cost: customers lost, or rows left out when there is no ID to count by."""
        if self.keys_in is not None:
            return self.keys_lost
        return max(0, next(iter(self.rows_in.values()), 0) - self.rows_out)

    @property
    def units_in(self) -> int:
        return self.keys_in if self.keys_in is not None else next(iter(self.rows_in.values()), 0)

    @property
    def sentence(self) -> str:
        read = ", ".join(f"{n:,} rows" for n in self.rows_in.values())
        out = f"read {read}; made {self.rows_out:,} rows"
        if self.keys_in is None:
            return out + " (no ID column, so customers were not counted)"
        return (
            f"{out}; customers {self.keys_in:,} before, {self.keys_out:,} after: "
            f"{self.keys_lost:,} lost, {self.keys_repeated:,} repeated"
        )


def frame_hash(frame: pd.DataFrame) -> str:
    """A hash of a frame's columns, types and values in row order (two runs that agree give the same hash)."""
    flat = frame.reset_index(drop=True)
    try:
        values = pd.util.hash_pandas_object(flat, index=False).to_numpy().tobytes()
    except TypeError:  # a cell that cannot be hashed (a list): hash its text
        values = pd.util.hash_pandas_object(flat.astype(str), index=False).to_numpy().tobytes()
    head = canonical_bytes([[str(c), str(t)] for c, t in zip(flat.columns, flat.dtypes, strict=True)])
    return sha256_hex(head + values)


def _key_values(series: pd.Series[Any]) -> pd.Series[Any]:
    """The IDs as text, empty ones left out, index kept. A whole-number float ID reads as the integer (a join
    that turns 7 into 7.0 does not lose and re-add every customer)."""
    values = series.dropna()
    if pdt.is_object_dtype(values) or pdt.is_string_dtype(values):
        values = values[values.astype(str).str.strip() != ""]  # an empty ID is no customer
    if pdt.is_float_dtype(values) and bool(((values == values.round()) & (values.abs() < 2**53)).all()):
        values = values.astype("int64")
    return values.astype(str).str.strip()


def _key_text(frame: pd.DataFrame, key: str) -> pd.Series[Any]:
    return _key_values(frame[next(c for c in frame.columns if str(c) == key)])


def grouping_key(sql: str | None, before: pd.DataFrame, after: pd.DataFrame) -> str | None:
    """The column a query step groups by when both its input and its result have it: the customers of a step
    that makes one row per group are the values of that column. None when it is not a plain one-column group.
    """
    column = group_by_column(sql) if sql else None
    if column is not None and _has(before, column) and _has(after, column):
        return column
    return None


def infer_key(before: pd.DataFrame, after: pd.DataFrame) -> str | None:
    """An ID column both tables have and `before` has once per row, named like an ID; none when there is none."""
    after_names = {str(c) for c in after.columns}
    for column in before.columns:
        name = str(column)
        low = name.lower()
        if name in after_names and (low.endswith(("id", "key")) or "customer" in low):
            series = before[column]
            if series.notna().all() and not series.duplicated().any() and len(series):
                return name
    return None


_LOG_ROW_ID: Final[re.Pattern[str]] = re.compile(
    r"^(order|tag|call|payment|pay|ticket|complaint|event|txn|transaction|invoice|bill|visit|session|"
    r"click|message|msg|claim)s?[_\s-]?(id|no|num|number|code|ref|key)$",
    re.I,
)


def names_a_log_row(column: str | None) -> bool:
    """Whether the ID column names a row of a log (`order_id`, `payment_id`), not a customer. The rows of such a
    side table are not customers: rows it leaves out are said as rows of that file."""
    return column is not None and _LOG_ROW_ID.search(column.strip()) is not None


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series[Any] | None:
    found = next((c for c in frame.columns if str(c) == column), None)
    if found is None:
        return None
    series = frame[found]
    if pdt.is_bool_dtype(series) or not pdt.is_numeric_dtype(series):
        return None
    return series


def _totals(
    sources: Sequence[pd.DataFrame], output: pd.DataFrame, *, skip: set[str], everywhere: bool
) -> tuple[ColumnTotal, ...]:
    """Total and count of each number column the output shares with the input (all inputs when `everywhere`)."""
    found: list[ColumnTotal] = []
    for column in dict.fromkeys(str(c) for c in output.columns):
        after = _numeric(output, column)
        if after is None or column in skip:
            continue
        before = [s for f in sources if (s := _numeric(f, column)) is not None]
        if not before:
            continue
        before = before if everywhere else before[:1]
        found.append(
            ColumnTotal(
                column=column,
                before=float(sum(float(s.sum()) for s in before)),
                after=float(after.sum()),
                non_null_before=sum(int(s.notna().sum()) for s in before),
                non_null_after=int(after.notna().sum()),
            )
        )
        if len(found) >= MAX_TOTAL_COLUMNS:
            break
    return tuple(found)


def _positive(series: pd.Series[Any], positive: object | None) -> pd.Series[Any] | None:
    """True where the outcome is the positive one; none when the positive value is not known."""
    if positive is not None:
        return series.astype(str).str.strip().str.lower() == str(positive).strip().lower()
    if pdt.is_bool_dtype(series):
        return series.fillna(False).astype(bool)
    if pdt.is_numeric_dtype(series) and bool(series.dropna().isin([0, 1]).all()):
        return series == 1  # a 0/1 outcome; a count or an amount has no "positive" to count
    return None


def _rate(frame: pd.DataFrame, target: str | None, positive: object | None) -> float | None:
    if target is None:
        return None
    column = next((c for c in frame.columns if str(c) == target), None)
    if column is None:
        return None
    mask = _positive(frame[column], positive)
    seen = int(frame[column].notna().sum())
    return None if mask is None or not seen else float(mask[frame[column].notna()].sum()) / seen * 100.0


def make_receipt(
    order: int,
    kind: str,
    inputs: Mapping[str, pd.DataFrame],
    output: pd.DataFrame,
    *,
    key: str | None = None,
    target: str | None = None,
    positive: object | None = None,
    stacking: bool = False,
    infer: bool = True,
    sql: str | None = None,
) -> Receipt:
    """The receipt for a step that read `inputs` (the first one is the base) and made `output`.

    When `stacking`, every input is part of the base (a stack puts the files under each other), and the totals
    are of all inputs together. With `infer` false and no `key`, customers are not counted at all (a log's own
    row ID names a row of the log, not a customer). With no `key` but the `sql` of a query that groups by one
    column both tables have, that column counts the customers and the receipt says so (`grouped_by`).
    """
    frames = list(inputs.values())
    rows_in = {name: len(frame) for name, frame in inputs.items()}
    first = frames[0]
    grouped = None if key or not infer else grouping_key(sql, first, output)
    key = key or grouped or (infer_key(first, output) if infer else None)
    base_frames = frames if stacking else [next((f for f in frames if _has(f, key)), first)] if key else []
    receipt: dict[str, Any] = {
        "order": order,
        "kind": kind,
        "rows_in": rows_in,
        "rows_out": len(output),
        "totals": _totals(frames, output, skip={key} if key else set(), everywhere=stacking),
        "empty_columns": tuple(
            str(c)
            for c in output.columns
            if len(output) and output[c].isna().all() and not _was_empty(frames, str(c))
        ),
        "rate_in": _rate(first, target, positive),
        "rate_out": _rate(output, target, positive),
    }
    if key is None or not base_frames or not _has(output, key) or not all(_has(f, key) for f in base_frames):
        return Receipt(**receipt)
    base_keys = pd.concat([_key_text(f, key) for f in base_frames], ignore_index=True)
    out_keys = _key_text(output, key)
    seen = set(base_keys)
    made = set(out_keys)
    counts = out_keys.value_counts()
    extra_in = len(base_keys) - len(seen)
    lost = seen - made
    receipt.update(
        key=key,
        keys_in=len(seen),
        keys_out=len(made),
        keys_lost=len(lost),
        keys_repeated=int((counts > 1).sum()),
        extra_rows=int((counts - 1).clip(lower=0).sum()),
        keys_new=len(made - seen),
        one_row_per_key=extra_in == 0,
        extra_rows_in=extra_in,
        lost_by_outcome=_lost_by_outcome(base_frames[0], key, lost, target),
        grouped_by=grouped,
        rows_no_key_in=_rows_without(base_frames[0], key),
        rows_no_key_out=_rows_without(output, key),
    )
    return Receipt(**receipt)


def _rows_without(frame: pd.DataFrame, key: str) -> int:
    """Rows with no ID: empty, or only spaces."""
    column = frame[next(c for c in frame.columns if str(c) == key)]
    return int((column.isna() | (column.astype(str).str.strip() == "")).sum())


def _has(frame: pd.DataFrame, key: str | None) -> bool:
    return key is not None and any(str(c) == key for c in frame.columns)


def _was_empty(frames: Sequence[pd.DataFrame], column: str) -> bool:
    """Whether the column was already empty in the inputs, so an empty result is not the step's doing. A column
    no input has is not: a new column that comes out empty is the step's."""
    found = [f[c] for f in frames for c in f.columns if str(c) == column]
    return bool(found) and all(s.isna().all() for s in found)


def _lost_by_outcome(base: pd.DataFrame, key: str, lost: set[str], target: str | None) -> dict[str, int]:
    column = next((c for c in base.columns if str(c) == target), None) if target else None
    if column is None or not lost:
        return {}
    flat = base.reset_index(drop=True)
    ids = _key_text(flat, key)
    picked = ids[ids.isin(lost)]
    picked = picked[~picked.duplicated()]  # one row per lost customer
    counts = flat.loc[picked.index, column].map(lambda v: "(blank)" if pd.isna(v) else str(v)).value_counts()
    return {str(k): int(v) for k, v in counts.head(10).items()}


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "0%"


def receipt_findings(
    receipt: Receipt, *, one_row_per_key: bool = True, side_table: str | None = None
) -> list[Finding]:
    """What the guards say about one step. `one_row_per_key` is false for a stack (a customer may repeat).
    `side_table` is the file's name when its rows are not customers (orders, payments): rows left out are
    then said as rows of that file."""
    n = receipt.order
    found: list[Finding] = []
    first_rows = next(iter(receipt.rows_in.values()), 0)
    if receipt.rows_out == 0 and first_rows > 0:
        found.append(
            Finding(
                level="stop",
                code="STEP_ZERO_ROWS",
                order=n,
                count=first_rows,
                message=f"Step {n} left no rows at all (it read {first_rows:,}).",
            )
        )
        return found
    if receipt.empty_columns:
        found.append(
            Finding(
                level="stop",
                code="STEP_EMPTY_COLUMN",
                order=n,
                count=len(receipt.empty_columns),
                message=f"Step {n} made {len(receipt.empty_columns):,} column(s) with no value at all: "
                f"{', '.join(receipt.empty_columns[:5])}.",
            )
        )
    if side_table is not None:
        left_out = max(0, first_rows - receipt.rows_out)
        if left_out:
            found.append(
                Finding(
                    level="flag",
                    code="STEP_LEAVES_OUT_ROWS",
                    order=n,
                    count=left_out,
                    message=f"Step {n} leaves out {left_out:,} of {first_rows:,} rows of {side_table} "
                    f"({_pct(left_out, first_rows)}).",
                )
            )
    elif receipt.keys_in is None:
        found.append(
            Finding(
                level="flag",
                code="STEP_NOT_COUNTED",
                order=n,
                message=f"Step {n} has no ID column both tables share, so the customers it keeps could not be "
                "counted. It needs a person's yes.",
            )
        )
    if receipt.keys_in is not None:
        found.extend(_key_findings(receipt, one_row_per_key=one_row_per_key))
    if receipt.combines_rows:
        found.append(_combined_finding(receipt))
    moved = [t for t in receipt.totals if not t.kept]
    if moved:
        shown = "; ".join(f"{t.column} {t.before:,.2f} to {t.after:,.2f}" for t in moved[:5])
        found.append(
            Finding(
                level="flag" if receipt.kind in _KEEPS_TOTALS else "note",
                code="STEP_TOTALS_CHANGED",
                order=n,
                count=len(moved),
                message=f"Step {n} changes the total of {len(moved):,} number column(s): {shown}.",
            )
        )
    return found


def _combined_finding(receipt: Receipt) -> Finding:
    """Rows folded into one per customer are said apart from customers lost and from rows left out."""
    n = receipt.order
    rows = next(iter(receipt.rows_in.values()), 0)
    no_key = max(0, receipt.rows_no_key_in - receipt.rows_no_key_out)
    text = (
        f"Step {n} combined {rows:,} rows into {receipt.keys_out:,} customers (counted by {receipt.key}"
        + (", the column it groups by" if receipt.grouped_by else "")
        + f"); {receipt.keys_lost:,} customers lost"
    )
    if no_key:
        text += f"; {no_key:,} rows with no {receipt.key} left out"
    return Finding(
        level="flag" if no_key else "note",
        code="STEP_COMBINES_ROWS",
        order=n,
        count=no_key or None,
        message=text + ".",
    )


def _key_findings(receipt: Receipt, *, one_row_per_key: bool) -> list[Finding]:
    n = receipt.order
    found: list[Finding] = []
    if receipt.keys_repeated and one_row_per_key and receipt.one_row_per_key:
        found.append(
            Finding(
                level="stop",
                code="JOIN_REPEATS_CUSTOMERS",
                order=n,
                count=receipt.keys_repeated,
                message=f"Step {n} puts {receipt.keys_repeated:,} customers on more than one row "
                f"({receipt.extra_rows:,} extra rows), although each was on one row before. "
                "Anything added up after this would count them twice.",
            )
        )
    elif receipt.extra_rows > receipt.extra_rows_in and one_row_per_key:
        found.append(
            Finding(
                level="flag",
                code="STEP_REPEATS_CUSTOMERS",
                order=n,
                count=receipt.keys_repeated,
                message=f"Step {n} adds rows for customers already in the table: {receipt.extra_rows_in:,} "
                f"extra rows before, {receipt.extra_rows:,} after.",
            )
        )
    if receipt.keys_lost:
        by = ", ".join(f"{k}: {v:,}" for k, v in receipt.lost_by_outcome.items())
        found.append(
            Finding(
                level="flag",
                code="STEP_LOSES_CUSTOMERS",
                order=n,
                count=receipt.keys_lost,
                message=f"Step {n} loses {receipt.keys_lost:,} of {receipt.keys_in:,} customers "
                f"({_pct(receipt.keys_lost, receipt.keys_in or 0)})"
                + (f"; by outcome, {by}." if by else "."),
            )
        )
    if receipt.keys_new:
        found.append(
            Finding(
                level="note",
                code="STEP_ADDS_CUSTOMERS",
                order=n,
                count=receipt.keys_new,
                message=f"Step {n} has {receipt.keys_new:,} customers that were not in the first table.",
            )
        )
    return found


# ---------------------------------------------------------------------------
# The change measure
# ---------------------------------------------------------------------------
def cell_change(before: pd.DataFrame, after: pd.DataFrame, *, key: str | None) -> tuple[int, int]:
    """(changed, out of): of the cells `before` filled in a column `after` also has, for the customers in both,
    how many are different now. Raises :class:`NotMeasurableError` when there is no ID to line the rows up by.
    """
    key = key or infer_key(before, after)
    if key is None or not _has(before, key) or not _has(after, key):
        raise NotMeasurableError("No ID column both tables have, so the change could not be measured.")
    kb = next(c for c in before.columns if str(c) == key)
    ka = next(c for c in after.columns if str(c) == key)
    if before[kb].isna().any() or before[kb].duplicated().any():
        raise NotMeasurableError(
            "The first table does not have one row per customer, so the rows cannot be lined up."
        )
    shared = [c for c in before.columns if str(c) != key and any(str(a) == str(c) for a in after.columns)]
    if not shared:
        raise NotMeasurableError("No column keeps its name, so there is nothing to compare.")
    left = before.assign(**{"__k": _key_values(before[kb])}).set_index("__k")
    right = after.assign(**{"__k": _key_values(after[ka])}).dropna(subset=["__k"])
    right = right[right["__k"].isin(left.index)]
    changed = out_of = 0
    for column in shared:
        a = left[column].reindex(right["__k"]).reset_index(drop=True)
        b = right[next(c for c in after.columns if str(c) == str(column))].reset_index(drop=True)
        filled = a.notna()
        same = (a == b) | (a.isna() & b.isna())
        out_of += int(filled.sum())
        changed += int((filled & ~same.fillna(False)).sum())
    return changed, out_of


# ---------------------------------------------------------------------------
# The running ledger
# ---------------------------------------------------------------------------
class LedgerStep(StrictBase):
    order: int
    kind: str
    units_after: int
    rows_after: int
    lost: int
    rate_after: float | None = None


class Ledger(StrictBase):
    """The whole plan, step by step: where the customers went and whether the outcome rate moved."""

    unit: Literal["customers", "rows"]
    start: int
    end: int
    steps: tuple[LedgerStep, ...]
    lost_total: int = Field(
        description="Customers lost step by step, added up (a step that adds some back does not hide it)."
    )
    lost_pct: float
    lost_by_outcome: dict[str, int]
    rate_start: float | None = None
    rate_end: float | None = None
    rate_move: float | None = Field(default=None, description="End minus start, in points.")
    top_loss_steps: tuple[int, ...] = ()
    top_rate_steps: tuple[int, ...] = ()


def build_ledger(receipts: Sequence[Receipt]) -> Ledger | None:
    """The ledger over `receipts` in order; none for an empty plan."""
    if not receipts:
        return None
    counted = receipts[0].keys_in is not None
    unit: Literal["customers", "rows"] = "customers" if counted else "rows"
    start = receipts[0].units_in
    steps = tuple(
        LedgerStep(
            order=r.order,
            kind=r.kind,
            units_after=(r.keys_out if counted and r.keys_out is not None else r.rows_out),
            rows_after=r.rows_out,
            lost=r.lost,
            rate_after=r.rate_out,
        )
        for r in receipts
    )
    by_outcome: dict[str, int] = {}
    for r in receipts:
        for name, count in r.lost_by_outcome.items():
            by_outcome[name] = by_outcome.get(name, 0) + count
    lost = sum(s.lost for s in steps)
    rate_end = next((r.rate_out for r in reversed(receipts) if r.rate_out is not None), None)
    rate_start = receipts[0].rate_in
    moves = {
        r.order: abs(r.rate_out - r.rate_in)
        for r in receipts
        if r.rate_in is not None and r.rate_out is not None
    }
    return Ledger(
        unit=unit,
        start=start,
        end=steps[-1].units_after,
        steps=steps,
        lost_total=lost,
        lost_pct=lost / start * 100.0 if start else 0.0,
        lost_by_outcome=by_outcome,
        rate_start=rate_start,
        rate_end=rate_end,
        rate_move=None if rate_start is None or rate_end is None else rate_end - rate_start,
        top_loss_steps=tuple(s.order for s in sorted(steps, key=lambda s: -s.lost)[:_TOP_STEPS] if s.lost),
        top_rate_steps=tuple(o for o, m in sorted(moves.items(), key=lambda kv: -kv[1])[:_TOP_STEPS] if m),
    )


def _naming(orders: Sequence[int]) -> str:
    return ", ".join(f"step {o}" for o in orders)


_YES_VALUES: Final[frozenset[str]] = frozenset({"1", "1.0", "true", "yes", "y"})
_NO_VALUES: Final[frozenset[str]] = frozenset({"0", "0.0", "false", "no", "n"})


def _who_were_lost(by_outcome: Mapping[str, int]) -> str:
    """One plain sentence on who was lost (most were a yes, or most were not); nothing when it cannot be said."""
    total = sum(by_outcome.values())
    if not total:
        return ""
    yes = sum(v for k, v in by_outcome.items() if k.strip().casefold() in _YES_VALUES)
    no = sum(v for k, v in by_outcome.items() if k.strip().casefold() in _NO_VALUES)
    if no * 2 > total:
        return " Most of them were not a yes."
    if yes * 2 > total:
        return " Most of them were a yes."
    return ""


def plan_findings(ledger: Ledger | None, *, side_table: bool = False) -> list[Finding]:
    """The plan-level gate: over 5% lost, or the outcome rate moved over a point, needs a yes naming the steps.
    The loss gate is about customers: a log's rows (`side_table`) are said per step, they do not stop the plan.
    """
    if ledger is None:
        return []
    found: list[Finding] = []
    if ledger.lost_pct > LOSS_STOP_PCT and not side_table:
        by = ", ".join(f"{k}: {v:,}" for k, v in ledger.lost_by_outcome.items())
        who = (
            "This step leaves"
            if sum(1 for st in ledger.steps if st.lost) <= 1
            else "Together these steps leave"
        )
        found.append(
            Finding(
                level="stop",
                code="PLAN_LOSES_TOO_MUCH",
                count=ledger.lost_total,
                message=f"{who} out {ledger.lost_total:,} of {ledger.start:,} {ledger.unit} "
                f"({ledger.lost_pct:.1f}%), more than the {LOSS_STOP_PCT:g}% we allow without asking."
                + (_who_were_lost(ledger.lost_by_outcome) if ledger.rate_start is not None else ""),
                detail=f"Most from {_naming(ledger.top_loss_steps)}." + (f" By outcome, {by}." if by else ""),
            )
        )
    if ledger.rate_move is not None and abs(ledger.rate_move) > RATE_STOP_POINTS:
        points = f"{RATE_STOP_POINTS:g} point" + ("" if RATE_STOP_POINTS == 1 else "s")
        found.append(
            Finding(
                level="stop",
                code="PLAN_MOVES_OUTCOME_RATE",
                message=f"These steps change the share of {ledger.unit} who are a yes from {ledger.rate_start:.1f}% "
                f"to {ledger.rate_end:.1f}% ({ledger.rate_move:+.1f} points), more than the {points} we allow "
                "without asking.",
                detail=f"The outcome rate moves most at {_naming(ledger.top_rate_steps)}.",
            )
        )
    return found


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------
class Expectations(StrictBase):
    """Checks a step declares before it runs. Each is a number our code compares with the receipt."""

    rows_out_equals: int | None = None
    rows_out_min: int | None = None
    rows_out_max: int | None = None
    keys_unique: bool = False
    totals_kept: tuple[str, ...] = ()
    max_keys_lost_pct: float | None = None
    max_keys_repeated: int | None = None

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_bytes(self.model_dump(mode="json")))


def _expectation_failures(receipt: Receipt, exp: Expectations) -> list[str]:
    out: list[str] = []
    rows = receipt.rows_out
    if exp.rows_out_equals is not None and rows != exp.rows_out_equals:
        out.append(f"expected {exp.rows_out_equals:,} rows out, got {rows:,}")
    if exp.rows_out_min is not None and rows < exp.rows_out_min:
        out.append(f"expected at least {exp.rows_out_min:,} rows out, got {rows:,}")
    if exp.rows_out_max is not None and rows > exp.rows_out_max:
        out.append(f"expected at most {exp.rows_out_max:,} rows out, got {rows:,}")
    counted = receipt.keys_in is not None
    if exp.keys_unique and (not counted or receipt.keys_repeated):
        out.append(
            f"expected every customer once, {receipt.keys_repeated:,} are repeated"
            if counted
            else "expected every customer once, but customers could not be counted"
        )
    if exp.max_keys_repeated is not None and counted and receipt.keys_repeated > exp.max_keys_repeated:
        out.append(
            f"expected at most {exp.max_keys_repeated:,} repeated customers, got {receipt.keys_repeated:,}"
        )
    if exp.max_keys_lost_pct is not None:
        share = receipt.keys_lost / receipt.keys_in * 100.0 if receipt.keys_in else 0.0
        if not counted or share > exp.max_keys_lost_pct:
            out.append(
                f"expected at most {exp.max_keys_lost_pct:g}% of customers lost, got "
                + (f"{share:.1f}%" if counted else "an unknown share")
            )
    totals = {t.column: t for t in receipt.totals}
    for column in exp.totals_kept:
        total = totals.get(column)
        if total is None:
            out.append(f"expected the total of '{column}' to be kept, but it could not be compared")
        elif not total.kept:
            out.append(
                f"expected the total of '{column}' to be kept, it went from {total.before:,.2f} to {total.after:,.2f}"
            )
    return out


@dataclass(frozen=True)
class ExpectationOutcome:
    order: int
    failures: tuple[str, ...]
    attempt: int
    retries_left: int
    must_ask: bool
    findings: tuple[Finding, ...] = ()


@dataclass
class ExpectationBook:
    """Expectations by step, locked at the first declaration. Owned by the run, not by the step or the AI."""

    _locked: dict[int, Expectations] = field(default_factory=dict)
    _hashes: dict[int, str] = field(default_factory=dict)
    _attempts: dict[int, int] = field(default_factory=dict)
    _failed: dict[int, int] = field(default_factory=dict)

    def declare(self, order: int, exp: Expectations) -> list[Finding]:
        """Lock `exp` for step `order`. A different one declared after the step has run replaces it and is flagged."""
        if order not in self._locked:
            self._locked[order], self._hashes[order] = exp, exp.digest
            return []
        if self._hashes[order] == exp.digest:
            return []
        ran = self._attempts.get(order, 0)
        self._locked[order], self._hashes[order] = exp, exp.digest
        if not ran:
            return []
        failed = self._failed.get(order, 0)
        return [
            Finding(
                level="flag",
                code="EXPECTATION_CHANGED_AFTER_FAILURE" if failed else "EXPECTATION_CHANGED_AFTER_RUN",
                order=order,
                message=f"The checks for step {order} were changed after the step "
                + ("failed them." if failed else "had run.")
                + " A person should look at the new ones.",
            )
        ]

    def check(self, order: int, receipt: Receipt) -> ExpectationOutcome:
        """Check the receipt against what was locked; all failures at once. Two retries, then ask."""
        exp = self._locked.get(order)
        attempt = self._attempts.get(order, 0) + 1
        self._attempts[order] = attempt
        failures = tuple(_expectation_failures(receipt, exp)) if exp is not None else ()
        if not failures:
            return ExpectationOutcome(order, (), attempt, RETRY_CAP, False)
        failed = self._failed.get(order, 0) + 1
        self._failed[order] = failed
        retries_left = max(0, RETRY_CAP - (failed - 1))
        must_ask = failed > RETRY_CAP
        words = (
            "It has failed too often; a person must say what to do."
            if must_ask
            else f"{retries_left} {'retry' if retries_left == 1 else 'retries'} left."
        )
        finding = Finding(
            level="stop",
            code="EXPECTATION_FAILED",
            order=order,
            count=len(failures),
            message=f"Step {order} missed {len(failures):,} of its own checks (try {failed}): "
            + "; ".join(failures)
            + f". {words}",
        )
        return ExpectationOutcome(order, failures, attempt, retries_left, must_ask, (finding,))


# ---------------------------------------------------------------------------
# Running a plan under the guards
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GuardedRun:
    run: RecipeRun
    receipts: tuple[Receipt, ...]
    ledger: Ledger | None
    findings: tuple[Finding, ...]
    outcomes: tuple[ExpectationOutcome, ...] = ()

    @property
    def stopped(self) -> bool:
        return any(f.level == "stop" for f in self.findings)


def _inputs_of(
    step: RecipeStep, before: pd.DataFrame, tables: Mapping[str, pd.DataFrame] | None
) -> dict[str, pd.DataFrame]:
    """The tables a step read: the one being prepared, and for a query the ones it names."""
    if step.kind is not RecipeStepKind.SQL_QUERY:
        return {THIS_TABLE: before}
    try:
        names = sql_params(step.params).inputs
    except SqlStepError:
        return {THIS_TABLE: before}
    found = {n: (before if n == THIS_TABLE else (tables or {}).get(n)) for n in names}
    return {n: f for n, f in found.items() if f is not None} or {THIS_TABLE: before}


def _sql_of(step: RecipeStep) -> str | None:
    text = step.params.get("sql") if step.kind is RecipeStepKind.SQL_QUERY else None
    return text if isinstance(text, str) else None


def run_guarded(
    frame: pd.DataFrame,
    steps: Sequence[RecipeStep],
    *,
    primary_key: str | None,
    target: str | None,
    positive: object | None = None,
    tables: Mapping[str, pd.DataFrame] | None = None,
    expectations: Mapping[int, Expectations] | None = None,
    book: ExpectationBook | None = None,
    twice: bool = True,
    source_name: str | None = None,
    watch: Callable[[RecipeStep, pd.DataFrame, pd.DataFrame], None] | None = None,
    **recipe_args: Any,
) -> GuardedRun:
    """`run_recipe`, with a receipt for every step, the plan's ledger and the findings; run twice by default.

    A table whose `primary_key` names the rows of a log (:func:`names_a_log_row`: `order_id`, `payment_id`) is a
    side table: its rows are counted as rows of `source_name`, never as customers.

    `expectations` (by step order) are locked in `book` before the plan runs. `watch(step, before, after)` sees each
    step's input and output once, on the first run (the preview takes its sample rows from it). Raises what
    `run_recipe` raises.
    """
    book = book if book is not None else ExpectationBook()
    side_table = (source_name or "this file") if names_a_log_row(primary_key) else None
    findings: list[Finding] = []
    for order, exp in (expectations or {}).items():
        findings.extend(book.declare(order, exp))

    def once(watching: bool = False) -> tuple[RecipeRun, list[Receipt], list[str]]:
        receipts: list[Receipt] = []
        hashes: list[str] = []

        def observe(step: RecipeStep, before: pd.DataFrame, after: pd.DataFrame) -> None:
            receipts.append(
                make_receipt(
                    step.order,
                    step.kind.value,
                    _inputs_of(step, before, tables),
                    after,
                    key=None if side_table else primary_key,
                    target=target,
                    positive=positive,
                    infer=side_table is None,
                    sql=_sql_of(step),
                )
            )
            hashes.append(frame_hash(after))
            if watching and watch is not None:
                watch(step, before, after)

        run = run_recipe(
            frame,
            steps,
            primary_key=primary_key,
            target=target,
            tables=tables,
            observe=observe,
            **recipe_args,
        )
        return run, receipts, hashes

    run, receipts, hashes = once(watching=True)
    if twice:
        _, _, again = once()
        if hashes != again:
            bad = next((r.order for r, a, b in zip(receipts, hashes, again, strict=False) if a != b), None)
            findings.append(
                Finding(
                    level="stop",
                    code="RUN_NOT_REPEATABLE",
                    order=bad,
                    message="Running the plan twice on the same rows gave different tables"
                    + (f", first at step {bad}." if bad is not None else ".")
                    + " Next month's run could not be trusted.",
                )
            )
    outcomes: list[ExpectationOutcome] = []
    for receipt in receipts:
        findings.extend(receipt_findings(receipt, one_row_per_key=True, side_table=side_table))
        if receipt.order in (expectations or {}):
            outcome = book.check(receipt.order, receipt)
            outcomes.append(outcome)
            findings.extend(outcome.findings)
    ledger = build_ledger(receipts)
    findings.extend(plan_findings(ledger, side_table=side_table is not None))
    return GuardedRun(run, tuple(receipts), ledger, tuple(findings), tuple(outcomes))
