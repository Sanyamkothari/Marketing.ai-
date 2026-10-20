"""Products and the catalogue: what a shop sells, and which items people buy again and again.

The refill example in the product vision ("bought a serum, nears the likely refill period") needs two
facts about each product that most shops never write down: is it something people run out of and
buy again (*replenishable*), and roughly how long one lasts (*typical cycle*). `Catalogue` takes them
when the merchant gives them and otherwise infers them from repeat purchases across all customers:
an item is replenishable when enough different customers bought it again on a later day, and its
typical cycle is the median gap between those repeat purchases.

This module also holds the small purchase-table helpers the cycle model shares (`purchases_frame`,
`trips`, `repeat_intervals`), so both read purchases the same way, and `clean_retail_lines`, the one
cleaning of the raw online-retail log that both the cycle and the recommender validations start from:

- A purchase is a PURCHASE event, at its UTC timestamp, measured in **days since the Unix epoch**.
- Two purchases of the same thing by the same customer on the same UTC calendar day are one *trip*:
  an order split over two invoices is not a refill.

Pure and deterministic: no clock reads, no randomness, no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import numpy as np
import pandas as pd
from pydantic import Field

from autonomy.contracts import Contract, Event, EventType

__all__ = [
    "DEFAULT_RULES",
    "EPOCH",
    "PURCHASE_COLUMNS",
    "RETAIL_PRODUCT_CODE",
    "Catalogue",
    "InferenceRules",
    "Product",
    "clean_retail_lines",
    "from_days",
    "purchases_frame",
    "repeat_intervals",
    "to_days",
    "trips",
]

EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
"""Time zero for the day numbers every catalogue and cycle calculation works in."""

PURCHASE_COLUMNS: Final[tuple[str, ...]] = ("customer_id", "item_id", "category", "day", "value", "quantity")
"""The columns of a purchase table: `day` is fractional days since `EPOCH` (UTC)."""


def to_days(ts: datetime) -> float:
    """An aware datetime as fractional days since the Unix epoch, in UTC."""
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return (ts - EPOCH).total_seconds() / 86_400.0


def from_days(days: float) -> datetime:
    """The inverse of `to_days`: an aware UTC datetime."""
    return EPOCH + timedelta(days=days)


class Product(Contract):
    """One thing the shop sells. Only `item_id` is required; the rest sharpens the agent's choices."""

    item_id: str = Field(min_length=1)
    name: str = ""
    category: str | None = None
    price: float = Field(default=0.0, ge=0, description="Typical selling price, gross")
    margin_rate: float | None = Field(
        default=None, ge=0, le=1, description="Gross margin as a share of price"
    )
    replenishable: bool = Field(default=False, description="People run out of it and buy it again")
    typical_cycle_days: float | None = Field(
        default=None, gt=0, description="Typical days between one purchase and the next refill"
    )


@dataclass(frozen=True)
class InferenceRules:
    """When the purchase history says an item is replenishable.

    The defaults are deliberately modest: an item counts once at least `min_repeat_customers`
    different customers bought it again on a later day, and at least `min_repeat_share` of its buyers
    did so. Gaps shorter than `min_cycle_days` (a second trip the next day to fix an order) do not
    make an item a refill product on their own: its median repeat gap must reach that length.
    """

    min_repeat_customers: int = 3
    min_repeat_share: float = 0.1
    min_cycle_days: float = 2.0


DEFAULT_RULES: Final[InferenceRules] = InferenceRules()


def purchases_frame(events: Iterable[Event]) -> pd.DataFrame:
    """The PURCHASE events as a purchase table (`PURCHASE_COLUMNS`), sorted by customer and time.

    `quantity` is read from `attrs["quantity"]` when it is a number (the online-retail line mapping
    puts it there) and is NaN otherwise. Other event types are ignored.
    """
    rows: list[tuple[str, str | None, str | None, float, float, float]] = []
    for e in events:
        if e.type is not EventType.PURCHASE:
            continue
        q = e.attrs.get("quantity")
        quantity = float(q) if isinstance(q, int | float) and not isinstance(q, bool) else math.nan
        rows.append(
            (
                e.customer_id,
                e.item_id,
                e.category,
                to_days(e.ts),
                float(e.value) if e.value is not None else math.nan,
                quantity,
            )
        )
    frame = pd.DataFrame(rows, columns=list(PURCHASE_COLUMNS))
    frame["day"] = frame["day"].astype(float)
    return frame.sort_values(["customer_id", "day"], kind="mergesort").reset_index(drop=True)


def trips(frame: pd.DataFrame, key: str | None) -> pd.DataFrame:
    """One row per (customer, key, UTC calendar day) shopping trip, sorted by customer, key and day.

    `key` is the column that says *what* was bought again (`item_id` or `category`); `None` means
    "any purchase", the customer's order cadence, and every row gets the key `"*"`. Rows whose key is
    missing are dropped. The result has columns `customer_id`, `key`, `day` (the trip's first
    purchase, fractional) and `day_index` (the whole UTC day number).
    """
    keys = pd.Series("*", index=frame.index, dtype=object) if key is None else frame[key]
    out = pd.DataFrame(
        {
            "customer_id": frame["customer_id"].astype(str),
            "key": keys,
            "day": frame["day"].astype(float),
        }
    )
    out = out[out["key"].notna()].copy()
    out["key"] = out["key"].astype(str)
    out["day_index"] = np.floor(out["day"].to_numpy(dtype=float)).astype(np.int64)
    out = out.sort_values(["customer_id", "key", "day"], kind="mergesort")
    out = out.drop_duplicates(["customer_id", "key", "day_index"], keep="first")
    return out.reset_index(drop=True)


def repeat_intervals(trip_table: pd.DataFrame) -> pd.DataFrame:
    """Gaps in whole days between consecutive trips of the same (customer, key)."""
    same = (trip_table["customer_id"] == trip_table["customer_id"].shift()) & (
        trip_table["key"] == trip_table["key"].shift()
    )
    gaps = trip_table["day_index"].diff()
    out = pd.DataFrame(
        {"customer_id": trip_table["customer_id"], "key": trip_table["key"], "interval_days": gaps}
    )
    return out[same.to_numpy(dtype=bool)].reset_index(drop=True)


RETAIL_PRODUCT_CODE: Final[str] = r"^\d{5}"
"""Online-retail StockCodes for real products start with five digits; postage, fees and adjustments do not."""


def clean_retail_lines(log: pd.DataFrame) -> pd.DataFrame:
    """The raw online-retail log's real product sales, one row per kept line, in log order.

    Cleaned the way `autonomy.data.loaders` cleans it: guest lines (no CustomerID), zero or negative
    quantities and prices, and cancellations are left out, and so are the non-product lines (postage,
    carriage, bank charges, manual adjustments), which would otherwise be "bought" by everyone. Times
    are UK local in the log and come back as aware UTC timestamps; a time that is ambiguous at the
    autumn clock change is dropped. The log has no categories, and Description is never read.

    Columns: `customer_id`, `item_id` (StockCode, upper-cased), `invoice`, `ts`, `quantity`, `price`.
    The cycle model and the recommender each turn this into their own purchase table.
    """
    from autonomy.data.loaders import ONLINE_RETAIL_DATE_FORMAT, ONLINE_RETAIL_TIMEZONE
    from autonomy.data.mapping import customer_key

    quantity = pd.to_numeric(log["Quantity"], errors="coerce")
    price = pd.to_numeric(log["UnitPrice"], errors="coerce")
    customers = pd.Series([customer_key(v) for v in log["CustomerID"]], index=log.index, dtype=object)
    stock = log["StockCode"].astype(str).str.strip().str.upper()
    invoice = log["InvoiceNo"].astype(str).str.strip()
    keep = (
        customers.notna()
        & (quantity > 0)
        & (price > 0)
        & ~invoice.str.startswith("C")
        & stock.str.match(RETAIL_PRODUCT_CODE)
    )
    local = pd.to_datetime(log["InvoiceDate"][keep], format=ONLINE_RETAIL_DATE_FORMAT).dt.tz_localize(
        ONLINE_RETAIL_TIMEZONE, ambiguous="NaT", nonexistent="shift_forward"
    )
    lines = pd.DataFrame(
        {
            "customer_id": customers[keep].astype(str),
            "item_id": stock[keep],
            "invoice": invoice[keep],
            "ts": local.dt.tz_convert("UTC"),
            "quantity": quantity[keep].astype(float),
            "price": price[keep].astype(float),
        }
    )
    return lines[lines["ts"].notna()].reset_index(drop=True)


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | np.integer | np.floating):
        f = float(value)
        return None if math.isnan(f) else f
    return None


def _optional_text(value: object) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1", "y"):
            return True
        if low in ("false", "no", "0", "n"):
            return False
        return None
    if isinstance(value, int | float | np.integer | np.floating):
        return bool(value)
    return None


class Catalogue:
    """The shop's products by `item_id`, with replenishable items and typical cycles filled in.

    Build it with `Catalogue(products)`, `Catalogue.from_records(...)` or `Catalogue.from_frame(...)`.
    Pass `purchases` (a purchase table from `purchases_frame`, or the events themselves) and every
    product whose `replenishable` or `typical_cycle_days` was not given is inferred from repeat
    purchases; items that appear only in the purchases are added as products too. `given_cycle_items`
    remembers which typical cycles the merchant stated, because the cycle model trusts those as prior
    knowledge but must not count an inferred one twice (it is already in the data).
    """

    def __init__(
        self, products: Iterable[Product], *, given_cycle_items: Iterable[str] | None = None
    ) -> None:
        by_id: dict[str, Product] = {}
        for p in products:
            if p.item_id in by_id:
                raise ValueError(f"item_id {p.item_id!r} appears twice in the catalogue")
            by_id[p.item_id] = p
        self._products: dict[str, Product] = dict(sorted(by_id.items()))
        if given_cycle_items is None:
            given = {i for i, p in self._products.items() if p.typical_cycle_days is not None}
        else:
            given = set(given_cycle_items) & set(self._products)
        self.given_cycle_items: frozenset[str] = frozenset(given)

    # ----------------------------------------------------------------------------------- building
    @classmethod
    def from_records(
        cls,
        records: Sequence[Mapping[str, Any]],
        purchases: pd.DataFrame | Iterable[Event] | None = None,
        rules: InferenceRules = DEFAULT_RULES,
    ) -> Catalogue:
        """From dicts with the `Product` field names. A missing or empty value means "not given"."""
        given: list[dict[str, Any]] = []
        for r in records:
            item = _optional_text(r.get("item_id"))
            if item is None:
                raise ValueError("every catalogue row needs an item_id")
            given.append(
                {
                    "item_id": item,
                    "name": _optional_text(r.get("name")) or "",
                    "category": _optional_text(r.get("category")),
                    "price": _optional_float(r.get("price")),
                    "margin_rate": _optional_float(r.get("margin_rate")),
                    "replenishable": _optional_bool(r.get("replenishable")),
                    "typical_cycle_days": _optional_float(r.get("typical_cycle_days")),
                }
            )
        return cls._build(given, purchases, rules)

    @classmethod
    def from_frame(
        cls,
        frame: pd.DataFrame,
        purchases: pd.DataFrame | Iterable[Event] | None = None,
        rules: InferenceRules = DEFAULT_RULES,
    ) -> Catalogue:
        """From a product table: `item_id` is required, every other `Product` column is optional."""
        if "item_id" not in frame.columns:
            raise ValueError("a catalogue table needs an item_id column")
        records = [{str(k): v for k, v in row.items()} for row in frame.to_dict("records")]
        return cls.from_records(records, purchases, rules)

    @classmethod
    def infer(
        cls, purchases: pd.DataFrame | Iterable[Event], rules: InferenceRules = DEFAULT_RULES
    ) -> Catalogue:
        """A catalogue built from purchases alone: every item bought, with inferred facts."""
        return cls._build([], purchases, rules)

    @classmethod
    def _build(
        cls,
        given: list[dict[str, Any]],
        purchases: pd.DataFrame | Iterable[Event] | None,
        rules: InferenceRules,
    ) -> Catalogue:
        frame = None
        if purchases is not None:
            frame = purchases if isinstance(purchases, pd.DataFrame) else purchases_frame(purchases)
        facts = _item_facts(frame, rules) if frame is not None else {}

        products: list[Product] = []
        stated_cycles: set[str] = set()
        seen: set[str] = set()
        for g in given:
            item = g["item_id"]
            seen.add(item)
            f = facts.get(item)
            replenishable = g["replenishable"]
            if replenishable is None:
                replenishable = f.replenishable if f is not None else False
            cycle = g["typical_cycle_days"]
            if cycle is not None:
                stated_cycles.add(item)
            elif f is not None and f.replenishable:
                cycle = f.median_interval
            products.append(
                Product(
                    item_id=item,
                    name=g["name"],
                    category=g["category"] if g["category"] is not None else (f.category if f else None),
                    price=g["price"] if g["price"] is not None else (f.price if f else 0.0),
                    margin_rate=g["margin_rate"],
                    replenishable=bool(replenishable),
                    typical_cycle_days=cycle,
                )
            )
        for item, f in sorted(facts.items()):
            if item in seen:
                continue
            products.append(
                Product(
                    item_id=item,
                    name=item,
                    category=f.category,
                    price=f.price,
                    replenishable=f.replenishable,
                    typical_cycle_days=f.median_interval if f.replenishable else None,
                )
            )
        return cls(products, given_cycle_items=stated_cycles)

    # ----------------------------------------------------------------------------------- reading
    def __len__(self) -> int:
        return len(self._products)

    def __iter__(self) -> Iterator[Product]:
        return iter(self._products.values())

    def __contains__(self, item_id: object) -> bool:
        return item_id in self._products

    def get(self, item_id: str) -> Product | None:
        return self._products.get(item_id)

    def category_of(self, item_id: str) -> str | None:
        p = self._products.get(item_id)
        return p.category if p is not None else None

    def is_replenishable(self, item_id: str) -> bool:
        p = self._products.get(item_id)
        return p is not None and p.replenishable

    def replenishable_items(self) -> tuple[str, ...]:
        return tuple(i for i, p in self._products.items() if p.replenishable)

    def items_in(self, category: str) -> tuple[str, ...]:
        return tuple(i for i, p in self._products.items() if p.category == category)

    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({p.category for p in self._products.values() if p.category is not None}))

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([p.model_dump() for p in self._products.values()])


@dataclass(frozen=True)
class _ItemFacts:
    category: str | None
    price: float
    buyers: int
    repeat_buyers: int
    median_interval: float | None
    replenishable: bool


def _item_facts(frame: pd.DataFrame, rules: InferenceRules) -> dict[str, _ItemFacts]:
    """What the purchases say about each item: category, price, repeat buyers and the median gap."""
    rows = frame[frame["item_id"].notna()]
    if rows.empty:
        return {}
    items = rows["item_id"].astype(str)

    # Category: the most common one the events carry (alphabetical on a tie, for determinism).
    categories: dict[str, str | None] = {}
    with_cat = pd.DataFrame({"item": items, "category": rows["category"]}).dropna()
    if not with_cat.empty:
        counts = with_cat.astype(str).value_counts().reset_index(name="n")
        counts = counts.sort_values(
            ["item", "n", "category"], ascending=[True, False, True], kind="mergesort"
        )
        for item, cat in zip(counts["item"], counts["category"], strict=True):
            categories.setdefault(str(item), str(cat))

    # Price: the median unit price where a quantity is known, else the median purchase value.
    value = rows["value"].to_numpy(dtype=float)
    quantity = rows["quantity"].to_numpy(dtype=float)
    unit = np.where(
        np.isfinite(quantity) & (quantity > 0), value / np.where(quantity > 0, quantity, 1.0), value
    )
    unit_series = pd.Series(unit, index=rows.index)
    prices = unit_series[np.isfinite(unit) & (unit >= 0)].groupby(items).median()

    trip_table = trips(rows, "item_id")
    buyers = trip_table.groupby("key")["customer_id"].nunique()
    gaps = repeat_intervals(trip_table)
    repeat_buyers = gaps.groupby("key")["customer_id"].nunique()
    medians = gaps.groupby("key")["interval_days"].median()

    facts: dict[str, _ItemFacts] = {}
    for item in sorted(set(items)):
        n_buyers = int(buyers.get(item, 0))
        n_repeat = int(repeat_buyers.get(item, 0))
        median = medians.get(item)
        median_f = float(median) if median is not None and not math.isnan(float(median)) else None
        replenishable = (
            n_repeat >= rules.min_repeat_customers
            and n_buyers > 0
            and n_repeat / n_buyers >= rules.min_repeat_share
            and median_f is not None
            and median_f >= rules.min_cycle_days
        )
        price = prices.get(item)
        facts[item] = _ItemFacts(
            category=categories.get(item),
            price=float(price) if price is not None and math.isfinite(float(price)) else 0.0,
            buyers=n_buyers,
            repeat_buyers=n_repeat,
            median_interval=median_f,
            replenishable=replenishable,
        )
    return facts
