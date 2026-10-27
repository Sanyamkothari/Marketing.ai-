"""Import a shop's order-history CSV into the agent's event store (ARCHITECTURE_REVIEW finding D5).

A new shop that is not on Shopify or Klaviyo has an order export and nothing else. The Data Doctor's
order-log path (`engine/doctor/order_log.py`) reads the same files, but it builds a v1 AutoML table and
never writes an event, so the agent started with an empty memory. This module is the other half: the
same two file shapes in, `PURCHASE` and `REFUND` events (and, only where the file says so, consent)
out, written to the event store.

Two shapes are read, by their column names (case, spaces and punctuation ignored):

- **UCI Online Retail style**: one row per line of an order. `InvoiceNo, StockCode, Quantity,
  InvoiceDate, UnitPrice, CustomerID` (and `Country`). A line with a negative quantity, or on an invoice
  that begins with "C", is a return.
- **Shopify orders export**: one row per line item, the order's own fields on its first row. `Name,
  Email, Created at, Total, Discount Code, Financial Status, Refunded Amount, Lineitem ...` (and
  `Currency`, `Accepts Marketing`, `Cancelled at`).

The detector's column rules are the order-log reader's (an ID-like name for the shopper, a date column,
a repeating order number) but written here with the standard library only: the engine module needs
pandas, the Data Doctor's session objects and the rest of `engine/`, and the v2 CI job installs none of
that.

What it produces:

- **One `PURCHASE` per order**, valued at the sum of its lines (UCI) or the order's `Total` (Shopify), so
  `CustomerState.purchases` counts orders as the Shopify connector's does. It carries `order_id`, the
  discount code (`discount_codes` and `offer_id`, as the connector writes them), the line and unit
  counts and, when known, `currency`.
- **`REFUND` events that net out** (`autonomy.proof.rewards`, docs/v3/PROOF_PROFIT.md). Shopify: one per
  order for `Refunded Amount`, naming `order_id`, and one `void` refund for the unpaid rest of a voided
  order, as the connector writes them. UCI: one per returned line; a UCI return does not say which
  invoice it returns, so it names no order and the proof matches it to the customer's latest earlier
  purchase with exactly that amount left.
- **Consent only when the file says so.** Only a Shopify `Accepts Marketing` of yes becomes a
  `CONSENT_GRANTED` on email. Everything else (UCI, a "no", a blank) has no consent and is not
  contactable. Nothing is invented: no signup event either, because a first order is not a signup date.

**Idempotent** (DECISIONS B-8). An event's id is `stable_id` over a content hash of (shop, kind, order id,
line). The line of a purchase is the whole order; the line of a UCI return is its stock code, quantity and
price plus an occurrence counter, so two identical lines in one invoice stay two. Re-importing the same or
an overlapping export adds nothing twice, and a re-export that lists orders in another order or adds new
ones adds only the new ones. A Shopify refund is keyed by its running total and, given the store, written
as the difference to what an earlier import already holds, so a refund that grew since the last export
adds only the increase.

**Point in time.** Customer ids and order numbers are read as text and never as numbers (`12346.0` is
`12346`, `007` stays `007`). A time with an offset keeps it; a time without one is read in `timezone`
(UTC unless given) and the report says how many were. Events are stored in UTC and carry the shop's zone
as `attrs["timezone"]` when it is not UTC, as `map_frame` does. A slash date that could be day first or
month first is refused with the option that settles it unless the file's own dates settle it.
Shopify's export holds no refund time and no consent time: both are stamped at `exported_at` (the
latest order in the file unless given) and the report says so.

Pure and deterministic, apart from the optional store reads and writes. No clock reads.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import timezone as fixed_offset
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from autonomy.contracts import Channel, Contract, Event, EventType, stable_id
from autonomy.contracts.events import AttrValue
from autonomy.data.mapping import DEFAULT_ID_PLACEHOLDERS, customer_key
from autonomy.interfaces import EventStore

__all__ = [
    "SKIP_REASONS",
    "SOURCE",
    "ImportReport",
    "OrderImportError",
    "Parsed",
    "format_report",
    "import_orders",
    "main",
    "parse_orders",
    "write_events",
]

SOURCE: Final[str] = "order_import"
"""The `Event.source` of everything imported here."""

SHAPE_UCI: Final[str] = "uci_online_retail"
SHAPE_SHOPIFY: Final[str] = "shopify_orders_export"

SKIP_REASONS: Final[dict[str, str]] = {
    "blank_row": "the row is empty",
    "missing_order_id": "no order number",
    "missing_customer": "no customer id or email (a guest checkout)",
    "placeholder_customer": "the customer id is a placeholder such as 0, unknown or guest",
    "bad_customer_id": "the customer id cannot be trusted as written (scientific notation)",
    "bad_timestamp": "the time cannot be read",
    "bad_amount": "a quantity, price or total cannot be read as an amount",
    "non_positive_price": "a zero or negative price (postage, corrections, samples)",
    "zero_quantity": "a quantity of zero",
    "order_customer_conflict": "the order already belongs to another customer",
    "other_currency": "the row is in another currency than the one imported",
}
"""Why a row is left out, in plain words. The report counts skipped rows by these keys."""

_MONEY_UNITS: Final[Decimal] = Decimal("0.005")
_UNPAID_STATUSES: Final[frozenset[str]] = frozenset({"voided", "expired"})
_YES: Final[frozenset[str]] = frozenset({"yes", "true", "1", "y", "subscribed"})
_NO: Final[frozenset[str]] = frozenset({"no", "false", "0", "n", "unsubscribed", "not_subscribed"})
_PLACEHOLDERS: Final[frozenset[str]] = frozenset(DEFAULT_ID_PLACEHOLDERS)

_ISO_TIME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_SLASH_TIME = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_AMOUNT = re.compile(r"^[+-]?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?$")
_SCIENTIFIC = re.compile(r"^[+-]?\d(\.\d+)?[eE][+-]\d{1,3}$")


class OrderImportError(ValueError):
    """The file cannot be imported at all (not a known shape, or a date that needs an option)."""


class ImportReport(Contract):
    """What an import read, built and wrote. Carries counts only, never an id or an address."""

    shop: str
    shape: str = Field(description="`uci_online_retail` or `shopify_orders_export`")
    dry_run: bool = Field(description="True: nothing was written")
    rows_read: int
    rows_used: int
    orders: int = Field(description="Distinct orders that became a PURCHASE")
    customers: int = Field(description="Distinct customers with at least one event")
    purchases: int
    refunds: int = Field(description="REFUND events, including voids")
    refunded_amount: float
    revenue: float = Field(description="Sum of the PURCHASE values, before refunds")
    skipped: dict[str, int] = Field(default_factory=dict, description="Rows left out, by reason")
    contactable: int = Field(
        description="Customers the file says accept marketing by email. Everyone else has no consent"
    )
    consent_events: int
    consent_revoked: int = Field(default=0, description="Consents withdrawn because the file now says no")
    events_built: int
    events_new: int | None = Field(
        default=None, description="Events the store did not hold; None when no store was given"
    )
    events_already_present: int | None = None
    first_order_at: datetime | None = None
    last_order_at: datetime | None = None
    currency: str | None = None
    timezone: str
    naive_times: int = Field(default=0, description="Rows whose time had no offset, read in `timezone`")
    notes: tuple[str, ...] = ()


@dataclass
class Parsed:
    """The events a file produces, the report counts that do not depend on a store, and the notes."""

    events: list[Event]
    report: ImportReport
    customers: frozenset[str] = frozenset()
    withdrawn: frozenset[str] = frozenset()
    """Customers whose file says "no": a store that holds a consent for them gets a revoke."""


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _decode(data: bytes) -> tuple[str, bool]:
    """The text, and whether the file was not UTF-8 (the UCI file is Windows-1252)."""
    try:
        return data.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), True


def _read(data: bytes) -> tuple[list[str], list[dict[str, str]], bool]:
    text, legacy = _decode(data)
    head = text.split("\n", 1)[0]
    delimiter = max((",", ";", "\t"), key=head.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise OrderImportError("the file is empty") from None
    names = [_norm(h) for h in header]
    rows: list[dict[str, str]] = []
    for cells in reader:
        if not cells:
            continue  # a bare blank line is not a row
        row: dict[str, str] = {}
        for name, cell in zip(names, cells, strict=False):
            if name and name not in row:
                row[name] = cell.strip()
        rows.append(row)
    return names, rows, legacy


@dataclass(frozen=True)
class _Columns:
    shape: str
    order: str
    customer: str
    time: str
    cancelled: str | None = None
    price: str = "unitprice"


_UCI_ORDER = ("invoiceno", "invoice", "orderid", "ordernumber")
_UCI_TIME = ("invoicedate", "orderdate", "date", "createdat")
_UCI_PRICE = ("unitprice", "price")
_UCI_CUSTOMER = ("customerid", "customer", "email")


def _first(names: Sequence[str], options: Sequence[str]) -> str | None:
    return next((o for o in options if o in names), None)


def _detect(names: Sequence[str]) -> _Columns:
    have = set(names)
    if {"name", "createdat"} <= have and ("total" in have or "lineitemprice" in have):
        customer = "customerid" if "customerid" in have else "email" if "email" in have else None
        if customer is not None:
            cancelled = "cancelledat" if "cancelledat" in have else None
            return _Columns(SHAPE_SHOPIFY, "name", customer, "createdat", cancelled)
    order, when = _first(names, _UCI_ORDER), _first(names, _UCI_TIME)
    customer = _first(names, _UCI_CUSTOMER)
    if order and when and customer and "quantity" in have and _first(names, _UCI_PRICE):
        return _Columns(SHAPE_UCI, order, customer, when, None, _first(names, _UCI_PRICE) or "unitprice")
    raise OrderImportError(
        "this file is not an order list this importer reads. Expected a UCI Online Retail style file "
        "(InvoiceNo, StockCode, Quantity, InvoiceDate, UnitPrice, CustomerID) or a Shopify orders export "
        "(Name, Email, Created at, Total, Lineitem ...). Columns found: "
        + ", ".join(sorted(have - {""})[:30])
    )


# ---------------------------------------------------------------------------
# Cells: times, amounts, ids
# ---------------------------------------------------------------------------
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise OrderImportError(f"unknown time zone {name!r}") from exc


def _slash_order(texts: Iterable[str], given: Literal["day_first", "month_first"] | None) -> str | None:
    """The day/month order of the file's slash dates: given, else settled by a part above 12, else refused."""
    if given is not None:
        return given
    day_first = month_first = False
    seen = False
    for text in texts:
        m = _SLASH_TIME.match(text)
        if m is None:
            continue
        seen = True
        a, b = int(m.group(1)), int(m.group(2))
        day_first = day_first or a > 12
        month_first = month_first or b > 12
    if not seen:
        return None
    if day_first and month_first:
        raise OrderImportError("the dates contradict each other: some read day first, some month first")
    if day_first:
        return "day_first"
    if month_first:
        return "month_first"
    raise OrderImportError(
        "the dates such as 01/12/2010 could be 1 December or 12 January and the file does not settle which. "
        "Say which with --date-order day_first or --date-order month_first"
    )


def _parse_time(text: str, zone: ZoneInfo, order: str | None) -> tuple[datetime, bool] | None:
    """(the time in UTC, whether it had no offset of its own and was read in `zone`), or None."""
    text = text.strip()
    try:
        m = _ISO_TIME.match(text)
        if m is not None:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hour, minute, second = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
            offset = m.group(7)
            return _stamp(year, month, day, hour, minute, second, offset, zone)
        m = _SLASH_TIME.match(text)
        if m is not None and order is not None:
            a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            day, month = (a, b) if order == "day_first" else (b, a)
            hour, minute, second = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
            return _stamp(year, month, day, hour, minute, second, None, zone)
    except ValueError:
        return None
    return None


def _stamp(
    year: int, month: int, day: int, hour: int, minute: int, second: int, offset: str | None, zone: ZoneInfo
) -> tuple[datetime, bool]:
    if offset is None:
        return datetime(year, month, day, hour, minute, second, tzinfo=zone).astimezone(UTC), True
    if offset == "Z":
        tz = UTC
    else:
        digits = offset[1:].replace(":", "")
        delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        tz = fixed_offset(delta if offset[0] == "+" else -delta)
    return datetime(year, month, day, hour, minute, second, tzinfo=tz).astimezone(UTC), False


def _amount(text: str) -> Decimal | None:
    """A plain or comma-grouped amount ("1234.50", "1,234.50"); anything ambiguous or odd is None."""
    cleaned = text.strip()
    if not cleaned or not _AMOUNT.match(cleaned):
        return None
    try:
        value = Decimal(cleaned.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _customer(cell: str) -> tuple[str | None, str | None]:
    """(the customer key, the reason it is None). The id is text; `12346.0` is `12346`."""
    if not cell.strip():
        return None, "missing_customer"
    key = customer_key(cell)
    if key is not None:
        return key, None
    if cell.strip().casefold() in _PLACEHOLDERS or set(cell.strip()) <= {"0"}:
        return None, "placeholder_customer"
    return None, "bad_customer_id"


def _money(value: Decimal) -> float:
    return float(round(value, 2))


def _event_id(shop: str, kind: str, *line: object) -> str:
    """The id of one event: a content hash of the shop, what kind of event and which order line."""
    return stable_id("evt", SOURCE, shop, kind, *line)


# ---------------------------------------------------------------------------
# The two shapes
# ---------------------------------------------------------------------------
@dataclass
class _Build:
    shop: str
    zone: ZoneInfo
    zone_name: str
    currency: str | None
    events: list[Event] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    naive: int = 0
    used: int = 0
    notes: list[str] = field(default_factory=list)
    granted: dict[str, datetime] = field(default_factory=dict)
    withdrawn: set[str] = field(default_factory=set)

    def skip(self, reason: str, rows: int = 1) -> None:
        self.skipped[reason] += rows

    def attrs(self, **values: AttrValue | None) -> dict[str, AttrValue]:
        out = {k: v for k, v in values.items() if v is not None and v != ""}
        out["import_shop"] = self.shop
        if self.zone_name != "UTC":
            out["timezone"] = self.zone_name
        if self.currency is not None:
            out["currency"] = self.currency
        return out


def _uci(
    rows: list[dict[str, str]], cols: _Columns, b: _Build, order: str | None
) -> tuple[dict[str, dict[str, Any]], list[Event]]:
    orders: dict[str, dict[str, Any]] = {}
    returns: list[Event] = []
    occurrence: Counter[tuple[str, str, str, str]] = Counter()
    for row in rows:
        if not any(row.values()):
            b.skip("blank_row")
            continue
        invoice = row.get(cols.order, "")
        if not invoice:
            b.skip("missing_order_id")
            continue
        customer, why = _customer(row.get(cols.customer, ""))
        if customer is None:
            b.skip(why or "missing_customer")
            continue
        when = _parse_time(row.get(cols.time, ""), b.zone, order)
        if when is None:
            b.skip("bad_timestamp")
            continue
        qty, price = _amount(row.get("quantity", "")), _amount(row.get(cols.price, ""))
        if qty is None or price is None:
            b.skip("bad_amount")
            continue
        if qty == 0:
            b.skip("zero_quantity")
            continue
        if price <= 0:
            b.skip("non_positive_price")
            continue
        ts, naive = when
        b.naive += naive
        sku = row.get("stockcode", "")
        country = row.get("country", "")
        b.used += 1
        if qty < 0 or invoice.upper().startswith("C"):
            line = (invoice, sku, str(abs(qty)), str(price))
            occurrence[line] += 1
            returns.append(
                Event(
                    event_id=_event_id(b.shop, "refund", *line, occurrence[line]),
                    customer_id=customer,
                    type=EventType.REFUND,
                    ts=ts,
                    source=SOURCE,
                    value=_money(abs(qty) * price),
                    item_id=sku or None,
                    attrs=b.attrs(cancellation_id=invoice, quantity=int(abs(qty)), country=country or None),
                )
            )
            continue
        o = orders.setdefault(
            invoice,
            {"customer": customer, "ts": ts, "value": Decimal(0), "lines": 0, "units": Decimal(0)},
        )
        if o["customer"] != customer:
            b.skip("order_customer_conflict")
            b.used -= 1
            continue
        value = qty * price
        if "best" not in o or value > o["best"]:
            o["best"], o["sku"] = value, sku
        o["ts"] = min(o["ts"], ts)
        o["value"] += value
        o["lines"] += 1
        o["units"] += qty
        o.setdefault("country", country)
    return orders, returns


def _shopify(
    rows: list[dict[str, str]], cols: _Columns, b: _Build, order: str | None
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if not any(row.values()):
            b.skip("blank_row")
            continue
        name = row.get(cols.order, "")
        if not name:
            b.skip("missing_order_id")
            continue
        groups.setdefault(name, []).append(row)

    def head(lines: list[dict[str, str]], column: str) -> str:
        return next((r[column] for r in lines if r.get(column)), "")

    currencies = Counter(
        head(lines, "currency").upper() for lines in groups.values() if head(lines, "currency")
    )
    if b.currency is None and currencies:
        b.currency = currencies.most_common(1)[0][0]
        if len(currencies) > 1:
            b.notes.append(
                f"The file holds orders in {len(currencies)} currencies; only {b.currency} (the most common) "
                "was imported. Pass --currency to choose."
            )
    orders: dict[str, dict[str, Any]] = {}
    for oid, lines in groups.items():
        n = len(lines)
        customer, why = _customer(head(lines, cols.customer))
        if customer is None:
            b.skip(why or "missing_customer", n)
            continue
        code = head(lines, "currency").upper()
        if currencies and code and code != b.currency:
            b.skip("other_currency", n)
            continue
        when = _parse_time(head(lines, cols.time), b.zone, order)
        if when is None:
            b.skip("bad_timestamp", n)
            continue
        total = _amount(head(lines, "total"))
        if total is None or total < 0:
            b.skip("bad_amount", n)
            continue
        ts, naive = when
        b.naive += naive * n
        b.used += n
        units = sum((_amount(r.get("lineitemquantity", "")) or Decimal(0)) for r in lines)
        best = max(
            lines,
            key=lambda r: (
                (_amount(r.get("lineitemprice", "")) or Decimal(0))
                * (_amount(r.get("lineitemquantity", "")) or Decimal(1))
            ),
        )
        refunded = _amount(head(lines, "refundedamount")) or Decimal(0)
        status = head(lines, "financialstatus").lower()
        cancelled = _parse_time(head(lines, cols.cancelled), b.zone, order) if cols.cancelled else None
        orders[oid] = {
            "customer": customer,
            "ts": ts,
            "value": total,
            "lines": n,
            "units": units,
            "sku": best.get("lineitemsku", ""),
            "code": head(lines, "discountcode"),
            "status": status,
            "refunded": refunded,
            "cancelled_at": cancelled[0] if cancelled else None,
            "accepts": head(lines, "acceptsmarketing").lower(),
        }
    return orders


def _purchase_events(b: _Build, orders: dict[str, dict[str, Any]]) -> list[Event]:
    out: list[Event] = []
    for oid, o in orders.items():
        code = o.get("code") or None
        out.append(
            Event(
                event_id=_event_id(b.shop, "purchase", oid),
                customer_id=o["customer"],
                type=EventType.PURCHASE,
                ts=o["ts"],
                source=SOURCE,
                value=_money(o["value"]),
                item_id=o.get("sku") or None,
                attrs=b.attrs(
                    order_id=oid,
                    lines=o["lines"],
                    quantity=int(o["units"]),
                    discount_codes=code,
                    offer_id=code.split(",")[0].strip() if code else None,
                    country=o.get("country") or None,
                    financial_status=o.get("status") or None,
                ),
            )
        )
    return out


def _shopify_refunds(b: _Build, orders: dict[str, dict[str, Any]], exported_at: datetime) -> list[Event]:
    """One refund per order for the refunded total, one void refund for the unpaid rest of a voided order."""
    out: list[Event] = []
    for oid, o in orders.items():
        refunded: Decimal = o["refunded"]
        if refunded > _MONEY_UNITS:
            amount = min(refunded, o["value"]) if o["value"] > 0 else refunded
            out.append(
                Event(
                    event_id=_event_id(b.shop, "refund", oid, "refunded", str(amount)),
                    customer_id=o["customer"],
                    type=EventType.REFUND,
                    ts=max(exported_at, o["ts"]),
                    source=SOURCE,
                    value=_money(amount),
                    attrs=b.attrs(
                        order_id=oid, refund_id="refunded", refund_total=_money(amount), time_known=False
                    ),
                )
            )
        if o["status"] in _UNPAID_STATUSES:
            unpaid = o["value"] - min(refunded, o["value"])
            if unpaid > _MONEY_UNITS:
                out.append(
                    Event(
                        event_id=_event_id(b.shop, "refund", oid, "void"),
                        customer_id=o["customer"],
                        type=EventType.REFUND,
                        ts=max(o["cancelled_at"] or exported_at, o["ts"]),
                        source=SOURCE,
                        value=_money(unpaid),
                        attrs=b.attrs(
                            order_id=oid, refund_id="void", time_known=o["cancelled_at"] is not None
                        ),
                    )
                )
    return out


def _consents(b: _Build, orders: dict[str, dict[str, Any]], exported_at: datetime) -> list[Event]:
    """Consent from `Accepts Marketing`, read at each customer's latest order. Only an explicit yes grants."""
    latest: dict[str, dict[str, Any]] = {}
    for o in orders.values():
        if o["customer"] not in latest or o["ts"] >= latest[o["customer"]]["ts"]:
            latest[o["customer"]] = o
    out: list[Event] = []
    for customer, o in sorted(latest.items()):
        answer = o.get("accepts", "")
        if answer in _YES:
            b.granted[customer] = exported_at
            out.append(
                Event(
                    event_id=_event_id(b.shop, "consent", customer, "email", "granted"),
                    customer_id=customer,
                    type=EventType.CONSENT_GRANTED,
                    ts=exported_at,
                    source=SOURCE,
                    channel=Channel.EMAIL,
                    attrs={"import_shop": b.shop, "collected_from": "orders_export_accepts_marketing"},
                )
            )
        elif answer in _NO:
            b.withdrawn.add(customer)
    return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def parse_orders(
    data: bytes,
    *,
    shop: str,
    timezone: str = "UTC",
    currency: str | None = None,
    date_order: Literal["day_first", "month_first"] | None = None,
    exported_at: datetime | None = None,
) -> Parsed:
    """Read an order CSV (as bytes) into events and the counts of a report. No store is touched.

    Raises `OrderImportError` for a file that is not a known shape or has dates it cannot settle.
    """
    if not shop.strip():
        raise OrderImportError("give the shop id the events belong to")
    if exported_at is not None and exported_at.tzinfo is None:
        raise OrderImportError("exported_at needs a time zone")
    zone = _zone(timezone)
    names, rows, legacy = _read(data)
    cols = _detect(names)
    order = _slash_order((r.get(cols.time, "") for r in rows), date_order)
    b = _Build(
        shop=shop.strip(),
        zone=zone,
        zone_name=timezone,
        currency=currency.strip().upper() if currency else None,
    )
    if legacy:
        b.notes.append("The file is not UTF-8; it was read as Windows-1252.")
    if cols.shape == SHAPE_UCI:
        orders, returns = _uci(rows, cols, b, order)
        events = _purchase_events(b, orders) + returns
        consents: list[Event] = []
        b.notes.append("This file has no consent column: nobody in it is contactable.")
        last = max((o["ts"] for o in orders.values()), default=None)
    else:
        orders = _shopify(rows, cols, b, order)
        last = max((o["ts"] for o in orders.values()), default=None)
        stamp = exported_at or last or datetime(1970, 1, 1, tzinfo=UTC)
        events = _purchase_events(b, orders) + _shopify_refunds(b, orders, stamp)
        consents = _consents(b, orders, stamp)
        if "acceptsmarketing" not in names:
            b.notes.append("This file has no Accepts Marketing column: nobody in it is contactable.")
        else:
            b.notes.append(
                f"Consent and refund times are not in the export; they are stamped {stamp.isoformat()} "
                f"({'exported_at as given' if exported_at else 'the latest order in the file'})."
            )
    if b.naive:
        b.notes.append(
            f"{b.naive:,} rows had a time with no offset and were read in {timezone}"
            + ("" if timezone != "UTC" else " (UTC: pass --timezone if the shop's clock is local)")
            + "."
        )
    if b.currency is None:
        b.notes.append("The file names no currency; pass --currency to record the shop's.")
    events = sorted(events + consents, key=lambda e: (e.ts, e.event_id))
    purchases = [e for e in events if e.type is EventType.PURCHASE]
    refunds = [e for e in events if e.type is EventType.REFUND]
    customers = {e.customer_id for e in events}
    report = ImportReport(
        shop=b.shop,
        shape=cols.shape,
        dry_run=False,
        rows_read=len(rows),
        rows_used=b.used,
        orders=len(purchases),
        customers=len(customers),
        purchases=len(purchases),
        refunds=len(refunds),
        refunded_amount=round(sum(e.value or 0.0 for e in refunds), 2),
        revenue=round(sum(e.value or 0.0 for e in purchases), 2),
        skipped={k: b.skipped[k] for k in SKIP_REASONS if b.skipped[k]},
        contactable=len(b.granted),
        consent_events=len(consents),
        events_built=len(events),
        first_order_at=min((e.ts for e in purchases), default=None),
        last_order_at=max((e.ts for e in purchases), default=None),
        currency=b.currency,
        timezone=timezone,
        naive_times=b.naive,
        notes=tuple(b.notes),
    )
    return Parsed(
        events=events, report=report, customers=frozenset(customers), withdrawn=frozenset(b.withdrawn)
    )


def _held(store: EventStore, customer: str, cache: dict[str, list[Event]]) -> list[Event]:
    if customer not in cache:
        cache[customer] = store.for_customer(customer)
    return cache[customer]


def _reconcile(parsed: Parsed, store: EventStore) -> tuple[list[Event], int]:
    """Events to offer the store: a Shopify refund as the increase over what is held, and a revoke for a
    customer the file now says declines marketing while the store still holds their consent."""
    cache: dict[str, list[Event]] = {}
    out: list[Event] = []
    revoked = 0
    shop = parsed.report.shop
    for e in parsed.events:
        if e.type is EventType.REFUND and e.attrs.get("refund_id") == "refunded":
            held = sum(
                x.value or 0.0
                for x in _held(store, e.customer_id, cache)
                if x.type is EventType.REFUND
                and x.source == SOURCE
                and x.attrs.get("import_shop") == shop
                and x.attrs.get("order_id") == e.attrs.get("order_id")
                and x.attrs.get("refund_id") == "refunded"
                and x.event_id != e.event_id
            )
            increase = round((e.value or 0.0) - held, 2)
            if increase <= float(_MONEY_UNITS):
                continue
            e = e.model_copy(update={"value": increase})
        out.append(e)
    stamp = max((e.ts for e in parsed.events), default=None)
    for customer in sorted(parsed.withdrawn):
        events = _held(store, customer, cache)
        consents = sorted(
            (
                x
                for x in events
                if x.channel is Channel.EMAIL
                and x.type in (EventType.CONSENT_GRANTED, EventType.CONSENT_REVOKED, EventType.UNSUBSCRIBE)
            ),
            key=lambda x: (x.ts, x.event_id),
        )
        if stamp is not None and consents and consents[-1].type is EventType.CONSENT_GRANTED:
            out.append(
                Event(
                    event_id=_event_id(shop, "consent", customer, "email", "revoked", stamp.isoformat()),
                    customer_id=customer,
                    type=EventType.CONSENT_REVOKED,
                    ts=max(stamp, consents[-1].ts),
                    source=SOURCE,
                    channel=Channel.EMAIL,
                    attrs={"import_shop": shop, "collected_from": "orders_export_accepts_marketing"},
                )
            )
            revoked += 1
    return out, revoked


def write_events(events: Sequence[Event], store: EventStore) -> tuple[list[Event], int]:
    """Append events one at a time; return the ones the store did not hold, and how many it held."""
    new = [e for e in events if store.append([e])]
    return new, len(events) - len(new)


def _count_new(events: Sequence[Event], store: EventStore) -> int:
    cache: dict[str, list[Event]] = {}
    held: dict[str, set[str]] = {}
    new = 0
    for e in events:
        if e.customer_id not in held:
            held[e.customer_id] = {x.event_id for x in _held(store, e.customer_id, cache)}
        if e.event_id not in held[e.customer_id]:
            new += 1
    return new


def import_orders(
    data: bytes,
    *,
    shop: str,
    store: EventStore | None = None,
    ingest: Callable[[Sequence[Event]], int] | None = None,
    dry_run: bool = False,
    timezone: str = "UTC",
    currency: str | None = None,
    date_order: Literal["day_first", "month_first"] | None = None,
    exported_at: datetime | None = None,
) -> tuple[ImportReport, list[Event]]:
    """Parse an order CSV and, unless `dry_run`, write its events.

    `store` is read to tell new events from held ones (and to size a refund that grew, or withdraw a
    consent). Events are written by `ingest` when given (it returns how many were new, and should fold
    them into state, as `api.v2.deps.make_ingest` does), else appended to `store` directly. With neither,
    nothing is read or written and the events are returned. Returns the report and the events written
    (none on a dry run, or when `ingest` wrote them).
    """
    parsed = parse_orders(
        data, shop=shop, timezone=timezone, currency=currency, date_order=date_order, exported_at=exported_at
    )
    report = parsed.report.model_copy(update={"dry_run": dry_run})
    if store is None:
        return report, [] if dry_run else list(parsed.events)
    offered, revoked = _reconcile(parsed, store)
    written: list[Event] = []
    if dry_run:
        fresh = _count_new(offered, store)
    elif ingest is not None:
        fresh = ingest(offered)
    else:
        written, _ = write_events(offered, store)
        fresh = len(written)
    report = report.model_copy(
        update={
            "events_built": len(offered),
            "events_new": fresh,
            "events_already_present": len(offered) - fresh,
            "consent_revoked": revoked,
        }
    )
    return report, written


# ---------------------------------------------------------------------------
# The report as plain text, and the command line
# ---------------------------------------------------------------------------
def format_report(report: ImportReport) -> str:
    """The report as plain lines."""
    lines = [
        f"Order import for shop {report.shop} ({'dry run, nothing written' if report.dry_run else 'written'})",
        f"  File shape:       {report.shape}",
        f"  Rows read:        {report.rows_read:,} ({report.rows_used:,} used)",
        f"  Orders:           {report.orders:,}",
        f"  Customers:        {report.customers:,}",
        f"  Refunds:          {report.refunds:,} (total {report.refunded_amount:,.2f})",
        f"  Revenue (gross):  {report.revenue:,.2f}" + (f" {report.currency}" if report.currency else ""),
        f"  Contactable:      {report.contactable:,} (customers the file says accept marketing by email)",
    ]
    if report.first_order_at and report.last_order_at:
        lines.append(
            f"  Orders span:      {report.first_order_at.isoformat()} to {report.last_order_at.isoformat()}"
        )
    skipped = sum(report.skipped.values())
    lines.append(f"  Skipped rows:     {skipped:,}")
    for reason, count in report.skipped.items():
        lines.append(f"    {reason}: {count:,} ({SKIP_REASONS[reason]})")
    lines.append(f"  Events built:     {report.events_built:,}")
    if report.events_new is not None:
        verb = "would be new" if report.dry_run else "new"
        lines.append(
            f"  Events {verb}:    {report.events_new:,} ({report.events_already_present:,} already held)"
        )
    if report.consent_revoked:
        lines.append(f"  Consent withdrawn: {report.consent_revoked:,}")
    lines.extend(f"  Note: {n}" for n in report.notes)
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m autonomy.data.order_import",
        description="Import an order-history CSV (UCI Online Retail or Shopify orders export) as events.",
    )
    p.add_argument("csv", type=Path, help="the order CSV")
    p.add_argument("--shop", required=True, help="the shop id the events belong to")
    p.add_argument("--dry-run", action="store_true", help="read and report; write nothing")
    p.add_argument("--timezone", default="UTC", help="IANA zone for times with no offset (default UTC)")
    p.add_argument("--currency", help="ISO currency code of the amounts")
    p.add_argument("--date-order", choices=("day_first", "month_first"), help="for dates such as 01/12/2010")
    p.add_argument("--exported-at", help="when the export was made (ISO 8601 with offset)")
    p.add_argument(
        "--database-url",
        help="SQLAlchemy URL of the event store (default AUTONOMY_DATABASE_URL, else sqlite:///autonomy.db). "
        "Without it a dry run opens no store",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        exported = datetime.fromisoformat(args.exported_at) if args.exported_at else None
        data = args.csv.read_bytes()
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    store: EventStore | None = None
    ingest: Callable[[Sequence[Event]], int] | None = None
    if args.database_url or not args.dry_run:
        from autonomy.data import StateFolder, apply_events
        from autonomy.runtime.settings import RuntimeSettings, engine_for
        from autonomy.store import SqlEventStore, SqlStateStore, create_all

        url = args.database_url or RuntimeSettings.from_env().database_url
        engine = engine_for(url)
        create_all(engine)
        store, states, folder = SqlEventStore(engine), SqlStateStore(engine), StateFolder()

        def ingest(batch: Sequence[Event]) -> int:
            new = [e for e in batch if store.append([e])]
            apply_events(new, states, folder, now=datetime.now(UTC))
            return len(new)

    try:
        report, _ = import_orders(
            data,
            shop=args.shop,
            store=store,
            ingest=ingest,
            dry_run=args.dry_run,
            timezone=args.timezone,
            currency=args.currency,
            date_order=args.date_order,
            exported_at=exported,
        )
    except OrderImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
