"""The Data Doctor's order-log path, second half (v3 phase 0, B4): roll an order log up, line to order to shopper.

A raw order log has one row per line of an order. The builder needs events, one row per thing that happened, so the
path rolls the log up into three tables, deterministically (the same log gives the same tables, row for row):

* `orders`: one row per order that has a line with a quantity above zero: the shopper, the earliest timestamp of its
  lines (31 orders of the UCI file carry two), `order_value` (quantity times price over those lines; a price of zero
  or below adds nothing), `quantity` (units on them), `order_month`, `days_since_previous_order` (the shopper's own
  earlier orders only, so an order never depends on a later one) and `country` when the log has one;
* `order_lines`: one row per line with a quantity above zero, stamped with its order's earliest timestamp, so an
  order is never split across two month-ends: `product_code`, `quantity`, `line_value`;
* `returns`: one row per order number that has a line with a quantity below zero: the shopper, the earliest timestamp
  of those lines and `quantity`, as a positive number.

An order is a line with a quantity above zero and a return is a line with a quantity below zero. The rule works on
quantity, not on the `C` an invoice number starts with, because the prefix is one dataset's habit (proposal §4); the
prefix is only counted, as evidence, in the sentence a person approves. Lines with no shopper are left out and
counted; if one order number carries two shoppers the roll-up stops, because it would be guessing whose order it is.
Nothing here reads `Description`: free text may carry personal data.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from engine.agent.key_steps import KeyColumn, key_text
from engine.config import StandardType
from engine.onboarding.transforms import cast_series

__all__ = [
    "ORDERS_ROLE",
    "ORDER_LINES_ROLE",
    "RETURNS_ROLE",
    "LogColumns",
    "OrderLogError",
    "RollUpCounts",
    "RolledUp",
    "audience_words",
    "find_log_columns",
    "key_codes",
    "list_matches_log",
    "read_times",
    "roll_up",
]

ORDERS_ROLE: Final[str] = "orders"
ORDER_LINES_ROLE: Final[str] = "order_lines"
RETURNS_ROLE: Final[str] = "returns"

EVENT_TIME: Final[str] = "event_time"
"""The date column of each rolled-up table, mapped to the standard `event_time`."""


def audience_words(role: str, no_event_days: int, *, rows: str, inclusive: bool) -> str:
    """The lapsed audience (B5) as the build and outcome sentences say it, as it runs: a month-end's snapshot is the
    instant its day begins, so only what came before that instant (and, with `inclusive`, at it) decides, and an order
    later that day is part of the outcome window. `rows` is what a shopper who counts has: "a row" in the build
    sentence, "a place in the count" in the outcome sentence. `inclusive` is the snapshot spec's
    `inclusive_snapshot_time`."""
    event, noun = ("an order", "order") if role == ORDERS_ROLE else (f"an event in {role}", "event")
    midnight = (
        f"an {noun} later on the month-end day counts towards the outcome instead (one stamped exactly at midnight, as "
        "a date with no time is, is before the day began)"
        if inclusive
        else f"an {noun} on the month-end day counts towards the outcome instead, even one stamped exactly at "
        "midnight, as a date with no time is"
    )
    return (
        f"Only shoppers who have lapsed count: a shopper has {rows} at a month-end when they had {event} before that "
        f"day began and the last such {noun} was {no_event_days} or more days earlier; {midnight}. Every other "
        "shopper is left out of that month-end."
    )


def key_codes(series: pd.Series[Any]) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.object_]]:
    """`(codes, texts)` of an ID column read as `key_text` writes it: one code per line (-1 for a blank) and the
    distinct texts in order of first appearance. Two cells written differently but read the same (`7`, `"7"` and
    `7.0`, as a spreadsheet export mixing number and text cells gives them) are one code, so one shopper or one
    order; case, spaces and leading zeros still keep two texts apart. `KeyColumn` alone keeps `7` and `"7"` apart
    (two cells of different types), which is why both the list and the roll-up read their IDs through here."""
    keys = KeyColumn(series)
    if not len(keys.texts):
        return np.full(len(series), -1, dtype=np.int64), np.asarray([], dtype=object)
    text_codes, texts = pd.factorize(pd.Series(keys.texts, dtype=object))
    codes = np.where(keys.codes >= 0, np.asarray(text_codes, dtype=np.int64)[np.maximum(keys.codes, 0)], -1)
    return codes.astype(np.int64), np.asarray(texts, dtype=object)


def read_times(series: pd.Series[Any]) -> pd.Series[Any]:
    """`series` read as the builder reads a date (`cast_series`), as timestamps without a zone, on `series`'s index.

    A time with an offset is moved to UTC and its zone dropped, the repository's rule for a time with a zone
    (`engine.stages.prepare`, `engine.doctor.impossible`), so times with different offsets, or with and without
    one, sort and compare together and against a month-end; a time without one is taken as it is. A value that
    does not read is NaT."""
    parsed = pd.Series(cast_series(series, StandardType.DATE).values, index=series.index)
    if parsed.dtype.kind == "M" and not isinstance(parsed.dtype, pd.DatetimeTZDtype):
        return parsed
    stamps: pd.Series[Any] = pd.to_datetime(parsed, errors="coerce", utc=True)
    out: pd.Series[Any] = stamps.dt.tz_convert("UTC").dt.tz_localize(None)
    return out


class OrderLogError(Exception):
    """A log the roll-up will not guess about. `code` is a code Plan O already raises (`BUILD_PLAN_INCOMPLETE`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Which column is which
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LogColumns:
    """The columns of an order log that play a part. `customer`, `order` and `date` are decided (a person named the
    shopper, the reading found the order number and the date); the others are found by name and may be missing,
    and a missing one only leaves out what needs it."""

    customer: str
    order: str | None
    date: str
    quantity: str | None = None
    price: str | None = None
    product: str | None = None
    country: str | None = None

    def used(self) -> tuple[str, ...]:
        """The log's columns the roll-up reads, in a fixed order. `Description` is never one of them."""
        names = (self.customer, self.order, self.date, self.quantity, self.price, self.product, self.country)
        return tuple(dict.fromkeys(name for name in names if name is not None))


def _find(
    frame: pd.DataFrame, taken: set[str], any_of: frozenset[str], also: frozenset[str] = frozenset()
) -> str | None:
    from engine.doctor.order_log import name_words

    for column in frame.columns:
        name = str(column)
        words = set(name_words(name))
        if name not in taken and words & any_of and (not also or words & also):
            return name
    return None


def find_log_columns(frame: pd.DataFrame, *, customer: str, order: str | None, date: str) -> LogColumns:
    """The log's quantity, price, product and country columns, by name (`Quantity`, `UnitPrice`, `StockCode`,
    `Country`), each the first column that has the word and is not one already taken."""
    taken = {customer, date, *([order] if order else [])}
    quantity = _find(frame, taken, frozenset({"quantity", "qty"}))
    taken |= {quantity} if quantity else set()
    price = _find(frame, taken, frozenset({"price"}))
    taken |= {price} if price else set()
    product = _find(
        frame,
        taken,
        frozenset({"stock", "sku", "product", "item"}),
        frozenset({"code", "id", "sku", "no", "number"}),
    )
    taken |= {product} if product else set()
    country = _find(frame, taken, frozenset({"country"}))
    return LogColumns(customer, order, date, quantity, price, product, country)


# ---------------------------------------------------------------------------
# The roll-up
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RollUpCounts:
    """Every line of the log, accounted for: used, or left out for one named reason. They add up to `lines`."""

    lines: int
    lines_without_shopper: int
    lines_without_order: int
    lines_unreadable_date: int
    lines_unreadable_quantity: int
    order_lines: int
    return_lines: int
    zero_lines: int
    orders: int
    return_orders: int
    cancelled_return_orders: int
    """Of `return_orders`, those whose number starts with `C`: shown as evidence, never the rule."""
    return_lines_without_c: int
    """Of `return_lines`, those on an order number that does not start with `C` (stock adjustments)."""
    two_timestamp_orders: int
    shoppers: int
    shoppers_without_orders: int
    unpriced_lines: int
    """Order lines whose price is zero, negative or unreadable: they add nothing to spend."""
    duplicate_lines: int = 0
    """Used lines identical to an earlier used line in every column the roll-up reads. They are kept and summed,
    as the log has them (the library's file keeps them too, DEC-1822 (q)), and said in the sentence."""

    @property
    def lines_used(self) -> int:
        return self.order_lines + self.return_lines


@dataclass(frozen=True)
class RolledUp:
    """The three tables, the customer list and the counts. All frames have the shopper in `customer_column`."""

    customer_column: str
    customers: pd.DataFrame
    orders: pd.DataFrame
    order_lines: pd.DataFrame
    returns: pd.DataFrame
    counts: RollUpCounts

    def tables(self) -> dict[str, pd.DataFrame]:
        return {ORDERS_ROLE: self.orders, ORDER_LINES_ROLE: self.order_lines, RETURNS_ROLE: self.returns}


def _numbers(series: pd.Series[Any]) -> pd.Series[float]:
    return pd.to_numeric(series, errors="coerce").astype("float64")


def _whole(values: pd.Series[Any]) -> pd.Series[Any]:
    """`values` as integers when every one is a whole number (units are counted, not measured), else as they are."""
    if len(values) and bool((values == values.round()).all()):
        return values.astype("int64")
    return values


def roll_up(log: pd.DataFrame, columns: LogColumns) -> RolledUp:
    """Roll `log` up into orders, order lines and returns, and derive the customer list from the same column.

    Raises `OrderLogError` (`BUILD_PLAN_INCOMPLETE`) when the log names no order number, or when one order number
    carries two shoppers; both are questions for a person, never guesses.
    """
    if columns.order is None:
        raise OrderLogError(
            "BUILD_PLAN_INCOMPLETE",
            "No column names the order, so the lines cannot be rolled up into orders and nothing is guessed.",
        )
    n = len(log)
    shopper_codes, shopper_texts = key_codes(log[columns.customer])
    if n and not len(shopper_texts):
        raise OrderLogError(
            "BUILD_PLAN_INCOMPLETE",
            f"No line names a shopper: '{columns.customer}' is blank on every one of the {n:,} lines.",
        )
    order_codes, order_texts = key_codes(log[columns.order])
    times = read_times(log[columns.date])
    quantity = _numbers(log[columns.quantity]) if columns.quantity else pd.Series(1.0, index=log.index)
    price = _numbers(log[columns.price]) if columns.price else pd.Series(np.nan, index=log.index)

    no_shopper = shopper_codes < 0
    no_order = ~no_shopper & (order_codes < 0)
    bad_date = ~no_shopper & ~no_order & times.isna().to_numpy()
    bad_quantity = ~no_shopper & ~no_order & ~bad_date & quantity.isna().to_numpy()
    used = ~(no_shopper | no_order | bad_date | bad_quantity)

    lines = pd.DataFrame(
        {
            "shopper": shopper_codes,
            "order": order_codes,
            "at": times.to_numpy(),
            "quantity": quantity.to_numpy(),
            "price": price.to_numpy(),
            "position": np.arange(n),
        }
    )
    if columns.product:
        lines["product"] = log[columns.product].to_numpy()
    if columns.country:
        lines["country"] = log[columns.country].to_numpy()
    lines = lines.loc[used].sort_values(["at", "order", "position"], kind="stable").reset_index(drop=True)

    shoppers_per_order = lines.groupby("order")["shopper"].nunique()
    if (shoppers_per_order > 1).any():
        raise OrderLogError(
            "BUILD_PLAN_INCOMPLETE",
            f"{int((shoppers_per_order > 1).sum()):,} order numbers in '{columns.order}' have lines for more than one "
            f"'{columns.customer}', so whose order they are cannot be told. Nothing is guessed.",
        )

    bought = lines.loc[lines["quantity"] > 0].copy()
    gave_back = lines.loc[lines["quantity"] < 0].copy()
    zero = int((lines["quantity"] == 0).sum())

    name = columns.customer
    shopper_text = pd.Series(shopper_texts, dtype=object)  # the shopper as `key_text` writes it, by code
    order_text = pd.Series(order_texts, dtype=object)

    bought["value"] = bought["quantity"] * bought["price"].clip(lower=0).fillna(0.0)
    unpriced = int((~(bought["price"] > 0)).sum()) if columns.price else 0
    first = bought.groupby("order", sort=False)["at"].transform("min")
    stamps = bought.groupby("order", sort=False)["at"].nunique()
    grouped = bought.groupby("order", sort=False)
    orders = grouped.agg(
        shopper=("shopper", "first"), at=("at", "min"), quantity=("quantity", "sum"), value=("value", "sum")
    )
    if columns.country:
        orders["country"] = grouped["country"].first()
    orders = orders.reset_index().sort_values(["at", "order"], kind="stable").reset_index(drop=True)
    orders["days_since_previous_order"] = orders.groupby("shopper")["at"].transform(
        lambda moments: moments.dt.normalize().diff().dt.days
    )
    orders_frame = pd.DataFrame(
        {
            name: shopper_text.iloc[orders["shopper"]].to_numpy(),
            EVENT_TIME: orders["at"].to_numpy(),
            "order_value": orders["value"].round(6).to_numpy() if columns.price else np.nan,
            "quantity": _whole(orders["quantity"]).to_numpy(),
            "order_month": orders["at"].dt.strftime("%Y-%m").to_numpy(),
            "days_since_previous_order": orders["days_since_previous_order"].to_numpy(),
        }
    )
    if not columns.price:
        orders_frame = orders_frame.drop(columns="order_value")
    if columns.country:
        orders_frame["country"] = orders["country"].to_numpy()

    kept = (
        bought.assign(at=first).sort_values(["at", "order", "position"], kind="stable").reset_index(drop=True)
    )
    lines_frame = pd.DataFrame(
        {name: shopper_text.iloc[kept["shopper"]].to_numpy(), EVENT_TIME: kept["at"].to_numpy()}
    )
    if columns.product:
        lines_frame["product_code"] = kept["product"].to_numpy()
    lines_frame["quantity"] = _whole(kept["quantity"]).to_numpy()
    if columns.price:
        lines_frame["line_value"] = kept["value"].round(6).to_numpy()

    back = gave_back.groupby("order", sort=False).agg(
        shopper=("shopper", "first"), at=("at", "min"), quantity=("quantity", "sum")
    )
    back = back.reset_index().sort_values(["at", "order"], kind="stable").reset_index(drop=True)
    returns_frame = pd.DataFrame(
        {
            name: shopper_text.iloc[back["shopper"]].to_numpy(),
            EVENT_TIME: back["at"].to_numpy(),
            "quantity": _whole(-back["quantity"]).to_numpy(),
        }
    )

    texts = order_text.iloc[back["order"]].astype(str)
    from_c = texts.str.startswith("C")
    line_orders = order_text.iloc[gave_back["order"]].astype(str)
    from engine.doctor.order_log import derive_customer_list

    list_frame = derive_customer_list(
        log, columns.customer, columns.date, order_column=columns.order, quantity_column=columns.quantity
    ).frame
    read = [c for c in ("shopper", "order", "at", "quantity", "price", "product", "country") if c in lines]
    duplicates = int(lines.duplicated(subset=read).sum()) if len(lines) else 0
    counts = RollUpCounts(
        lines=n,
        lines_without_shopper=int(no_shopper.sum()),
        lines_without_order=int(no_order.sum()),
        lines_unreadable_date=int(bad_date.sum()),
        lines_unreadable_quantity=int(bad_quantity.sum()),
        order_lines=len(bought),
        return_lines=len(gave_back),
        zero_lines=zero,
        orders=len(orders_frame),
        return_orders=len(returns_frame),
        cancelled_return_orders=int(from_c.sum()),
        return_lines_without_c=int((~line_orders.str.startswith("C")).sum()),
        two_timestamp_orders=int((stamps > 1).sum()),
        shoppers=len(list_frame),
        shoppers_without_orders=int(list_frame["signup_date"].isna().sum()),
        unpriced_lines=unpriced,
        duplicate_lines=duplicates,
    )
    return RolledUp(name, list_frame, orders_frame, lines_frame, returns_frame, counts)


def list_matches_log(
    customers: pd.DataFrame,
    log: pd.DataFrame,
    customer: str,
    tables: Iterable[pd.DataFrame] = (),
) -> bool:
    """Whether the customer list holds exactly the distinct non-blank shoppers of the log: nobody added, nobody
    missing, nobody twice, and every shopper of the rolled-up `tables` on it.

    The log's IDs are read here value by value with `key_text`, the rule itself, not through `key_codes` the list
    was built with, so a fault in the vectorised reading (a merge too many or too few) shows as a mismatch."""
    listed = customers[customer].astype(str)
    if bool(listed.duplicated().any()):
        return False
    names = set(listed)
    values = log[customer].tolist()
    try:  # each distinct cell once; by type too, so `True` and `1` (equal in Python) stay two cells
        distinct: Iterable[Any] = [value for _kind, value in dict.fromkeys((type(v), v) for v in values)]
    except TypeError:  # an unhashable cell: read every one
        distinct = values
    found = {text for value in distinct if (text := key_text(value)) is not None}
    if names != found:
        return False
    return all(set(map(str, table[customer].tolist())) <= names for table in tables)
