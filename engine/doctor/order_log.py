"""The Data Doctor's order-log path, first half (v3 phase 0, B2 and B3): read a raw order log, ask who the shopper is,
and derive the customer list from it.

A raw order log (UCI Online Retail: `InvoiceNo, StockCode, Description, Quantity, InvoiceDate, UnitPrice, CustomerID,
Country`) has one row per line of an order. No column says who came back, and no file is a customer list, so every
tool that could build the outcome stops (`docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md`). This module is the rule half of
the path; nothing in it runs unless the caller passes the log in explicitly, and the visit calls it only behind
`data_doctor.order_log` (the switch is `order_log_enabled`).

* **The detector** (`order_log_reading`) says whether a file reads as an order log: a column named like an ID that
  repeats (more than `MIN_ROWS_PER_VALUE` rows per value, blanks allowed up to `ID_MAX_BLANK_SHARE`), a date column
  that parses, and no column that is the goal's outcome. Names are split at capital letters, so `CustomerID` is
  `customer id` and `InvoiceNo` is `invoice no`. It ranks by name first (a shopper-like ID, then another ID, then an
  order number) and by blank share second, for this path only, so a `CustomerID` with a quarter of its cells blank
  is not buried under a fully filled `InvoiceNo` (`engine.onboarding.sources.key_candidates` ranks the other way, for
  telling a customer list from an event table).
* **Q1** (`customer_question`) asks which column names the shopper. It is blocking, it has no recommended answer
  (which column is the customer is meaning-bearing, so only a person decides) and it states, for each candidate,
  counts measured on the file.
* **Q2** (`blank_customer_question`) is asked once Q1 is answered: the lines with no shopper cannot be counted for
  anyone, so they are left out; the person says yes, and that yes is the acknowledgement the over-5% loss guard
  asks for (`engine.doctor.guards.LOSS_STOP_PCT`, unchanged).
* **The derived customer list** (`derive_customer_list`) holds the distinct non-blank values of the confirmed column,
  written the way a person reads them (`12346.0` is `12346`: `engine.agent.key_steps.key_text`, the existing exact
  rule and nothing fuzzy), with each shopper's first order date as `signup_date`.

The numbers in every sentence are counted on the file. A file that was read in part says so.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd

from engine.agent.key_steps import KeyColumn
from engine.doctor.case import CaseOption, CaseQuestion, payload_digest
from engine.doctor.guards import LOSS_STOP_PCT, Finding, Receipt, build_ledger, plan_findings
from engine.doctor.visit_contracts import OptionEffect

if TYPE_CHECKING:
    from pydantic import JsonValue

    from engine.config import RoleCatalogue, UseCaseConfig
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.outcomes import DerivedEntity, OutcomeFile

__all__ = [
    "BLANK_CUSTOMER_CODE",
    "CUSTOMER_COLUMN_CODE",
    "ID_MAX_BLANK_SHARE",
    "MIN_ROWS_PER_VALUE",
    "MIXED_ORDERS_CODE",
    "DerivedCustomers",
    "IdColumn",
    "OrderLogReading",
    "blank_customer_question",
    "customer_question",
    "derive_customer_list",
    "derived_entity",
    "id_tier",
    "key_columns",
    "loss_finding",
    "mixed_orders_question",
    "name_words",
    "order_log_enabled",
    "order_log_reading",
]

CUSTOMER_COLUMN_CODE: Final[str] = "ORDER_LOG_CUSTOMER"
"""The code of Q1, the question that asks which column names the shopper."""

BLANK_CUSTOMER_CODE: Final[str] = "ORDER_LOG_BLANK_CUSTOMER"
"""The code of Q2, the question that asks whether lines with no shopper are left out."""

MIXED_ORDERS_CODE: Final[str] = "ORDER_LOG_MIXED_ORDERS"
"""The code of the question asked when one order has lines with a shopper and lines without."""

MIN_ROWS_PER_VALUE: Final[float] = 1.05
"""A column repeats when it has more than this many rows per different value (blank cells not counted)."""

ID_MAX_BLANK_SHARE: Final[float] = 0.5
"""A column named like an ID names the rows even with blanks (guest checkouts), up to half of them. The same bar as
`engine.doctor.diagnose.ID_MAX_BLANK_SHARE`, repeated here so this module does not import the diagnosis."""


def order_log_enabled(config: UseCaseConfig) -> bool:
    """Whether the path is on for this use case: `data_doctor.order_log` needs `data_doctor.unified`."""
    return bool(config.data_doctor.unified and config.data_doctor.order_log)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
_CAMEL: Final[re.Pattern[str]] = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])"
)
_NON_WORD: Final[re.Pattern[str]] = re.compile(r"[^0-9A-Za-z]+")

_WHO: Final[frozenset[str]] = frozenset(
    {
        "customer", "cust", "shopper", "client", "member", "account", "acct", "user", "buyer", "guest",
        "visitor", "subscriber", "patron", "consumer", "contact",
    }
)  # fmt: skip
"""Words that say a column names a person or an account."""

_ORDERISH: Final[frozenset[str]] = frozenset(
    {
        "order",
        "invoice",
        "bill",
        "receipt",
        "txn",
        "transaction",
        "basket",
        "cart",
        "purchase",
        "sale",
        "checkout",
    }
)
"""Words that say a column names an order, not a person."""

_ID_WORDS: Final[frozenset[str]] = frozenset({"id", "uid", "key", "no", "num", "number", "nbr", "ref"})
"""Words that say a column is an identifier. `code` is not one: `StockCode` names a product, not a shopper."""

_SPLIT_SUFFIXES: Final[tuple[str, ...]] = ("id", "no", "num", "number", "key", "ref")
_SPLIT_PREFIXES: Final[frozenset[str]] = _WHO | _ORDERISH
"""A word run together in capitals (`CUSTOMERID`, `INVOICENO`) is split only where the front is one of the words
above, so `PAID` is never `pa id`."""


def name_words(name: str) -> tuple[str, ...]:
    """The lower-case words of a column name: split at separators, at capital letters and between letters and digits
    (`CustomerID` -> `customer`, `id`; `InvoiceNo` -> `invoice`, `no`; `cust_no` -> `cust`, `no`; `OrderNo2` ->
    `order`, `no`, `2`)."""
    words: list[str] = []
    for chunk in _NON_WORD.split(str(name)):
        for part in _CAMEL.split(chunk):
            word = part.lower()
            if not word:
                continue
            for suffix in _SPLIT_SUFFIXES:
                front = word[: -len(suffix)]
                if word.endswith(suffix) and front in _SPLIT_PREFIXES:
                    words += [front, suffix]
                    break
            else:
                words.append(word)
    return tuple(words)


def id_tier(name: str) -> int | None:
    """How a column's name ranks as the shopper: 0 a shopper-like ID (`CustomerID`, `cust_no`, `customer`),
    1 another ID (`id`, `key`), 2 an order number (`InvoiceNo`, `OrderID`); None when the name is not an ID's.
    """
    words = set(name_words(name))
    if not words:
        return None
    has_id = bool(words & _ID_WORDS)
    only_who = words <= _WHO
    if not has_id and not only_who:
        return None
    if words & _ORDERISH:
        return 2
    if words & _WHO:
        return 0
    return 1


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IdColumn:
    """One column that could name the rows' shopper (or order), measured on the file."""

    column: str
    tier: int
    rows: int
    blank_rows: int
    distinct: int

    @property
    def filled_rows(self) -> int:
        return self.rows - self.blank_rows

    @property
    def rows_per_value(self) -> float:
        return self.filled_rows / self.distinct if self.distinct else 0.0

    @property
    def blank_share(self) -> float:
        return self.blank_rows / self.rows if self.rows else 0.0

    @property
    def looks_like_order_number(self) -> bool:
        return self.tier == 2


@dataclass(frozen=True)
class OrderLogReading:
    """A file that reads as an order log: its repeating ID columns (best first), its date column and its size."""

    upload_id: str
    file_name: str
    rows: int
    sampled: bool
    date_column: str
    candidates: tuple[IdColumn, ...]

    @property
    def customer_candidates(self) -> tuple[IdColumn, ...]:
        """The columns that could name the shopper: every candidate whose name is not an order number's."""
        return tuple(c for c in self.candidates if not c.looks_like_order_number)

    @property
    def order_number(self) -> IdColumn | None:
        """The best candidate that looks like the order number, if the file has one."""
        return next((c for c in self.candidates if c.looks_like_order_number), None)

    def column(self, name: str) -> IdColumn | None:
        return next((c for c in self.candidates if c.column == name), None)


def _measure(file: LoadedFile, position: int, column: str, tier: int) -> IdColumn:
    """A column's counts over every row of the file when they are known exactly, else over the rows read."""
    exact = file.exact.get(column)
    if exact is not None:
        return IdColumn(column, tier, exact.row_count, exact.null_count, exact.distinct_count)
    keys = KeyColumn(file.frame.iloc[:, position])
    return IdColumn(column, tier, len(file.frame), len(file.frame) - keys.rows, len(keys.texts))


def _names_outcome(file: LoadedFile, config: UseCaseConfig) -> bool:
    """Whether some column of the file is the goal's outcome: the configured name, a recorded synonym, or a
    two-valued column that shares a word with it. An outcome column means the file is not waiting for one."""
    from engine.doctor.diagnose import outcome_columns

    return any(c.level != "guess" or c.two_valued for c in outcome_columns(file.frame, config))


def order_log_reading(
    file: LoadedFile,
    config: UseCaseConfig | None = None,
    *,
    date_columns: Sequence[str] | None = None,
) -> OrderLogReading | None:
    """Whether `file` reads as a raw order log, and what its ID and date columns are; None when it does not.

    It does when a column named like an ID repeats (more than `MIN_ROWS_PER_VALUE` rows per value, at most
    `ID_MAX_BLANK_SHARE` blank), at least one such column is not an order number, a date column parses, and (when
    `config` is given) no column is the goal's outcome. `date_columns` is the file's readable date columns when the
    caller already has them (`FileDiagnosis.date_columns`); otherwise they are read from the file.
    """
    if config is not None and _names_outcome(file, config):
        return None
    ranked = sorted(
        (
            (tier, position, str(column))
            for position, column in enumerate(file.frame.columns)
            if (tier := id_tier(str(column))) is not None
        )
    )
    measured: list[IdColumn] = []
    for tier, position, column in ranked:
        one = _measure(file, position, column, tier)
        if one.distinct and one.blank_share <= ID_MAX_BLANK_SHARE and one.rows_per_value > MIN_ROWS_PER_VALUE:
            measured.append(one)
    candidates = tuple(sorted(measured, key=lambda c: (c.tier, round(c.blank_share, 6))))
    if not any(not c.looks_like_order_number for c in candidates):
        return None
    if date_columns is None:
        from engine.onboarding.sources import time_candidates

        date_columns = [t.column for t in time_candidates(file.frame, file.profile)]
    if not date_columns:
        return None
    rows = file.file_rows if file.file_rows is not None else len(file.frame)
    return OrderLogReading(
        upload_id=file.upload_id,
        file_name=file.file_name,
        rows=rows,
        sampled=any(c.rows != rows for c in candidates) or rows > len(file.frame),
        date_column=date_columns[0],
        candidates=candidates,
    )


def key_columns(file: LoadedFile, customer: str) -> frozenset[str]:
    """The columns of an order log that are labels, not quantities: the shopper column a person chose and every
    column that reads as an order number. No cleaning step reads these as numbers."""
    reading = order_log_reading(file)
    found = {customer}
    if reading is not None:
        found |= {c.column for c in reading.candidates if c.looks_like_order_number}
    return frozenset(found)


# ---------------------------------------------------------------------------
# Q1: which column names the shopper
# ---------------------------------------------------------------------------
def _short(value: Any) -> str:
    return payload_digest({"v": value})[:12]


def _question_id(code: str, goal_id: str | None, subject: Any) -> str:
    return f"q.{code.lower()}.{goal_id or 'base'}.{_short(subject)}"[:128]


def _lines_each(column: IdColumn) -> str:
    each = column.rows_per_value
    return f"{each:.1f}" if each < 10 else f"{round(each):,}"


def _bullet(column: IdColumn) -> str:
    blank = (
        "never blank"
        if column.blank_rows == 0
        else f"blank on {column.blank_rows:,} lines ({column.blank_share:.1%})"
    )
    line = f"• '{column.column}': {column.distinct:,} different values, {_lines_each(column)} lines each on average, {blank}."
    return line + (" This looks like the order number." if column.looks_like_order_number else "")


def customer_question(
    reading: OrderLogReading, *, goal_id: str | None = None
) -> tuple[CaseQuestion, tuple[OptionEffect, ...]]:
    """Q1: each row is one line of an order, not one shopper; which column names the shopper?

    Blocking, with no recommended answer: only a person says which column is the customer. Each candidate is an
    option (`c0`, `c1`, ...) and so is "None of these", which ends the path and leaves the data request.
    """
    bullets = "\n".join(_bullet(c) for c in reading.candidates)
    text = f"Each row of {reading.file_name} is one line of an order, not one shopper. Which column names the shopper?\n{bullets}"
    if reading.sampled:
        text += f"\nCounted on the rows read of {reading.rows:,}."
    choices: list[tuple[str, OptionEffect]] = [
        (
            candidate.column,
            OptionEffect(
                option_id=f"c{index}",
                kind="order_log_customer",
                title=f"“{candidate.column}” names the shopper in {reading.file_name}",
                payload={"upload_id": reading.upload_id, "column": candidate.column},
            ),
        )
        for index, candidate in enumerate(reading.candidates)
    ]
    choices.append(("None of these: this file is not one row per order line", OptionEffect(option_id="none")))
    options = tuple(
        CaseOption(
            option_id=effect.option_id,
            label=label,
            effect_digest=payload_digest(effect.payload) if effect.kind is not None else None,
        )
        for label, effect in choices
    )
    question = CaseQuestion(
        question_id=_question_id(CUSTOMER_COLUMN_CODE, goal_id, {"upload": reading.upload_id}),
        code=CUSTOMER_COLUMN_CODE,
        upload_id=reading.upload_id,
        text=text,
        options=options,
        blocking=True,
        recommended=None,  # meaning-bearing: the rules never recommend which column is the customer
        goal_id=goal_id,
    )
    return question, tuple(effect for _label, effect in choices)


# ---------------------------------------------------------------------------
# The derived customer list (B3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DerivedCustomers:
    """The customer list an order log gives: one row per distinct non-blank shopper, and what was left out.

    `frame` has `column` (the shopper as written by `key_text`) and `signup_date` (the shopper's first order date,
    used only as an "existed by T" filter, never as a feature). `blank_lines` and `blank_orders` count the lines
    with no shopper and, when an order-number column is known, the orders those lines belong to; `mixed_orders`
    counts the orders that have some lines with a shopper and some without."""

    column: str
    date_column: str
    order_column: str | None
    frame: pd.DataFrame
    lines: int
    blank_lines: int
    blank_orders: int | None
    mixed_orders: int | None
    unreadable_dates: int
    no_order_shoppers: int


def _blank_orders(keys: KeyColumn, orders: pd.Series[Any] | None) -> tuple[int | None, int | None]:
    """`(orders whose lines all have no shopper, orders with some lines with and some without)`; (None, None) when
    no order-number column is known."""
    if orders is None:
        return None, None
    order_keys = KeyColumn(orders)
    blank = keys.codes < 0
    named = order_keys.codes >= 0
    if not named.any():
        return None, None
    frame = pd.DataFrame({"order": order_keys.codes[named], "blank": blank[named]})
    per_order = frame.groupby("order")["blank"].agg(["sum", "count"])
    all_blank = int((per_order["sum"] == per_order["count"]).sum())
    mixed = int(((per_order["sum"] > 0) & (per_order["sum"] < per_order["count"])).sum())
    return all_blank, mixed


def derive_customer_list(
    frame: pd.DataFrame,
    column: str,
    date_column: str,
    *,
    order_column: str | None = None,
    quantity_column: str | None = None,
) -> DerivedCustomers:
    """The customer list of `frame`, an order log whose shopper is `column`, by exact rules only.

    Each shopper is one distinct non-blank value of `column`, written as `key_text` writes it (`12346.0` is
    `12346`); two values that differ in case, spaces or leading zeros are two shoppers, never merged on a near match.
    `signup_date` is the day of the shopper's first order: the earliest `date_column` (read as the builder reads
    dates) over the lines with a quantity above zero, or over every line when the log has no `quantity_column`. A
    shopper with no such line stays on the list with a blank `signup_date`, and the snapshot spine then leaves them
    out of every month-end (an unknown date shows nobody existed); `no_order_shoppers` counts them.
    """
    from engine.config import StandardType
    from engine.onboarding.transforms import cast_series

    keys = KeyColumn(frame[column])
    # Two values written differently in the file but read the same (`7` and `7.0` in one column) are one shopper.
    text_codes, texts = pd.factorize(pd.Series(keys.texts, dtype=object))
    shopper_codes = np.where(keys.codes >= 0, text_codes[np.maximum(keys.codes, 0)], -1)
    parsed = pd.Series(cast_series(frame[date_column], StandardType.DATE).values, index=frame.index)
    unreadable = int((frame[date_column].notna() & parsed.isna()).sum())
    named = shopper_codes >= 0
    counts_as_order = named.copy()
    if quantity_column is not None:
        quantity = pd.to_numeric(frame[quantity_column], errors="coerce").to_numpy()
        counts_as_order &= quantity > 0
    first = (
        pd.DataFrame({"code": shopper_codes[counts_as_order], "at": parsed.to_numpy()[counts_as_order]})
        .dropna(subset=["at"])
        .groupby("code", sort=True)["at"]
        .min()
        .reindex(range(len(texts)))
    )
    list_frame = pd.DataFrame(
        {column: np.asarray(texts, dtype=object), "signup_date": first.dt.normalize().to_numpy()}
    )
    blank_orders, mixed_orders = _blank_orders(keys, frame[order_column] if order_column else None)
    return DerivedCustomers(
        column=column,
        date_column=date_column,
        order_column=order_column,
        frame=list_frame,
        lines=len(frame),
        blank_lines=int((~named).sum()),
        blank_orders=blank_orders,
        mixed_orders=mixed_orders,
        unreadable_dates=unreadable,
        no_order_shoppers=int(list_frame["signup_date"].isna().sum()),
    )


def derived_entity(
    log: OutcomeFile,
    derived: DerivedCustomers,
    roles: RoleCatalogue,
    reading: OrderLogReading,
    *,
    quantity_column: str | None = None,
) -> tuple[DerivedEntity, OutcomeFile]:
    """The explicit `derived_entity` argument of `offer_outcome` and `propose_event_outcome`, and the log as the
    event file they read it from.

    The list is a file read as the entity role with one row per shopper; the log is read as `orders` (a person has
    just said each row is a line of an order), with the date column the reading found first. Both are built from the
    one confirmed column, so the log meets the list on it by construction, written the same way. With a
    `quantity_column`, only the lines with a quantity above zero are events: a return or a cancellation is not an
    order, and the outcome is "an order within the window"."""
    from engine.doctor.contracts import FileDiagnosis
    from engine.doctor.outcomes import DerivedEntity, OutcomeFile

    base = log.diagnosis
    listed = FileDiagnosis(
        upload_id=f"{base.upload_id}~customers",
        file_name=f"the shoppers of {base.file_name}",
        rows=len(derived.frame),
        columns=len(derived.frame.columns),
        kind=roles.entity_role,
        kind_label="a customer list",
        kind_reason=f"the distinct values of '{derived.column}', the column a person named as the shopper",
        id_column=derived.column,
        rows_per_id=1.0,
        date_columns=("signup_date",),
        personal_columns=0,
        fixable_columns=0,
        notes=(),
    )
    dates = (reading.date_column, *(c for c in base.date_columns if c != reading.date_column))
    events = base.model_copy(
        update={
            "kind": "orders",
            "kind_label": "a log of order lines",
            "kind_reason": f"each row is one line of an order; '{derived.column}' names the shopper",
            "id_column": derived.column,
            "date_columns": dates,
        }
    )
    entity = OutcomeFile(diagnosis=listed, frame=derived.frame)
    frame = log.frame
    if quantity_column is not None:
        orders = frame.loc[pd.to_numeric(frame[quantity_column], errors="coerce") > 0]
        if len(frame) == base.rows:  # the whole file was read: the rows are now those of the orders
            events = events.model_copy(update={"rows": len(orders)})
        frame = orders
    return (
        DerivedEntity(file=entity, source_upload_id=base.upload_id, join_column=derived.column),
        OutcomeFile(diagnosis=events, frame=frame),
    )


# ---------------------------------------------------------------------------
# Q2: lines with no shopper
# ---------------------------------------------------------------------------
def loss_finding(derived: DerivedCustomers, *, file_name: str = "the log") -> Finding | None:
    """What the existing loss guard says about leaving the lines with no shopper out: its own stop
    (`PLAN_LOSES_TOO_MUCH`, over `LOSS_STOP_PCT`) or nothing. The ledger is the guard's, over lines, so the limit,
    the wording and the key a person's yes is tied to are not copied here."""
    if derived.blank_lines == 0:
        return None
    receipt = Receipt(
        order=1,
        kind="leave_out_lines_without_shopper",
        rows_in={file_name: derived.lines},
        rows_out=derived.lines - derived.blank_lines,
    )
    found = plan_findings(build_ledger([receipt]))
    return found[0] if found else None


def blank_customer_question(
    reading: OrderLogReading, derived: DerivedCustomers, *, goal_id: str | None = None
) -> tuple[CaseQuestion, tuple[OptionEffect, ...], Finding | None] | None:
    """Q2: lines with no shopper cannot be counted for anyone; leave them out? None when there are no such lines.

    Blocking. "Leave them out" is the person's acknowledgement for the loss guard when the lines are over its
    limit (`loss_finding`; the returned finding's `key` is in the option's payload, so the yes is tied to this
    finding with this count and goes stale if the count changes); "Stop" keeps the data request."""
    if derived.blank_lines == 0:
        return None
    finding = loss_finding(derived, file_name=reading.file_name)
    share = derived.blank_lines / derived.lines if derived.lines else 0.0
    orders = (
        f", {derived.blank_orders:,} {'order' if derived.blank_orders == 1 else 'orders'}, likely guest checkouts"
        if derived.blank_orders is not None
        else ""
    )
    text = (
        f"{derived.blank_lines:,} of {derived.lines:,} lines ({share:.1%}) have no '{derived.column}'{orders}. "
        "Nobody can be counted for them. Leave them out?"
    )
    if finding is not None:
        text += f" That is more than the {LOSS_STOP_PCT:g}% that needs your yes."
    payload: dict[str, JsonValue] = {
        "upload_id": reading.upload_id,
        "column": derived.column,
        "lines": derived.blank_lines,
        "of": derived.lines,
    }
    if finding is not None:
        payload["finding_key"] = finding.key
    leave = OptionEffect(
        option_id="leave_out",
        kind="order_log_blank_customers",
        title=f"Leave out the {derived.blank_lines:,} lines with no '{derived.column}'",
        payload=payload,
    )
    stop = OptionEffect(option_id="stop")
    choices = [("Leave them out", leave), ("Stop: I will get the shopper for these orders", stop)]
    options = tuple(
        CaseOption(
            option_id=effect.option_id,
            label=label,
            effect_digest=payload_digest(effect.payload) if effect.kind is not None else None,
            rows_changed=derived.blank_lines if effect.kind is not None else None,
            safe=effect.kind is None,
        )
        for label, effect in choices
    )
    question = CaseQuestion(
        question_id=_question_id(
            BLANK_CUSTOMER_CODE, goal_id, {"upload": reading.upload_id, "c": derived.column}
        ),
        code=BLANK_CUSTOMER_CODE,
        upload_id=reading.upload_id,
        column=derived.column,
        text=text,
        options=options,
        blocking=True,
        recommended=None,
        goal_id=goal_id,
    )
    return question, tuple(effect for _label, effect in choices), finding


def mixed_orders_question(
    reading: OrderLogReading, derived: DerivedCustomers, *, goal_id: str | None = None
) -> tuple[CaseQuestion, tuple[OptionEffect, ...]] | None:
    """An order with lines that have a shopper and lines that do not: whose is it? None when no order is mixed (or
    no order-number column is known). The path never assumes: the person says to keep the lines that have a
    shopper and leave out the rest, or stops. In the UCI file no order is mixed (0 of 3,710)."""
    if not derived.mixed_orders or derived.order_column is None:
        return None
    many = derived.mixed_orders
    text = (
        f"{many:,} {'order' if many == 1 else 'orders'} in '{derived.order_column}' "
        f"{'has' if many == 1 else 'have'} some lines with a '{derived.column}' and some without. "
        f"Keep the lines that have a '{derived.column}' and leave out the others?"
    )
    keep = OptionEffect(
        option_id="keep_named",
        kind="order_log_mixed_orders",
        title=f"Keep only the lines with a '{derived.column}' in the {many:,} mixed orders",
        payload={
            "upload_id": reading.upload_id,
            "column": derived.column,
            "order_column": derived.order_column,
            "orders": many,
        },
    )
    stop = OptionEffect(option_id="stop")
    choices = [
        (f"Keep the lines with a '{derived.column}'", keep),
        ("Stop: I will fix the file", stop),
    ]
    options = tuple(
        CaseOption(
            option_id=effect.option_id,
            label=label,
            effect_digest=payload_digest(effect.payload) if effect.kind is not None else None,
            safe=effect.kind is None,
        )
        for label, effect in choices
    )
    question = CaseQuestion(
        question_id=_question_id(
            MIXED_ORDERS_CODE, goal_id, {"upload": reading.upload_id, "c": derived.column}
        ),
        code=MIXED_ORDERS_CODE,
        upload_id=reading.upload_id,
        column=derived.order_column,
        text=text,
        options=options,
        blocking=True,
        recommended=None,
        goal_id=goal_id,
    )
    return question, tuple(effect for _label, effect in choices)
