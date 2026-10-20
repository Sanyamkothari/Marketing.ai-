"""Ready mappings and loaders for the repository's sample datasets: online-retail and Hillstrom.

Each loader returns a `LoadResult`: the events, the rows that could not become events (as
`MappingProblem`s, never as a crash), and `notes`, the plain sentences that say what was assumed. The
notes matter, because only one of the three sources is a true event log:

- **The online-retail transaction log** (`library/online-retail/data/online-retail-dataset.csv`, fetched
  by `fetch.py`, git-ignored) *is* an event log. `online_retail_log_events` maps it through
  `ONLINE_RETAIL_LOG_MAPPING` (one event per invoice) or `ONLINE_RETAIL_LINE_MAPPING` (one per line).
  A cancellation (`InvoiceNo` beginning "C", negative `Quantity`) becomes a REFUND with a positive value.
- **The committed online-retail `sample.csv`** is one row per lapsed customer, aggregated at a
  2011-09-10 snapshot. `online_retail_sample_events` rebuilds an approximate history from the
  aggregates; see `ONLINE_RETAIL_SAMPLE_NOTES` for exactly which numbers survive and which are invented.
- **Hillstrom `sample.csv`** is one row per customer of a randomised e-mail experiment, with no dates.
  `hillstrom_events` turns each row into a signup, consent, an approximate purchase history, the SEND
  of the e-mail (treated rows only, with a synthetic `decision_id`) and the visit / purchase outcomes;
  see `HILLSTROM_NOTES`.

The two sample loaders write a long table (one row per event) and map it through the same
`EventMapping` machinery as any client file, so every event gets the same validation and a stable id.
Nothing here reads the clock or draws a random number: the same file always gives the same events.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import pandas as pd

from autonomy.contracts import Channel, Event, EventType, stable_id
from autonomy.data.mapping import (
    EventMapping,
    MappingError,
    MappingProblem,
    ProblemCode,
    customer_key,
    map_frame,
)

__all__ = [
    "HILLSTROM_ARMS",
    "HILLSTROM_CAMPAIGN_AT",
    "HILLSTROM_MAPPING",
    "HILLSTROM_NOTES",
    "HILLSTROM_SAMPLE",
    "HILLSTROM_SOURCE",
    "ONLINE_RETAIL_LINE_MAPPING",
    "ONLINE_RETAIL_LOG",
    "ONLINE_RETAIL_LOG_MAPPING",
    "ONLINE_RETAIL_LOG_NOTES",
    "ONLINE_RETAIL_SAMPLE",
    "ONLINE_RETAIL_SAMPLE_MAPPING",
    "ONLINE_RETAIL_SAMPLE_NOTES",
    "ONLINE_RETAIL_SOURCE",
    "LoadResult",
    "hillstrom_decision_id",
    "hillstrom_events",
    "load_hillstrom",
    "load_online_retail_log",
    "load_online_retail_sample",
    "online_retail_log_events",
    "online_retail_sample_events",
    "read_hillstrom_sample",
    "read_online_retail_log",
    "read_online_retail_sample",
]

LIBRARY: Final[Path] = Path(__file__).resolve().parents[2] / "library"
ONLINE_RETAIL_SAMPLE: Final[Path] = LIBRARY / "online-retail" / "sample.csv"
ONLINE_RETAIL_LOG: Final[Path] = LIBRARY / "online-retail" / "data" / "online-retail-dataset.csv"
HILLSTROM_SAMPLE: Final[Path] = LIBRARY / "hillstrom-email" / "sample.csv"

ONLINE_RETAIL_SOURCE: Final[str] = "online-retail"
HILLSTROM_SOURCE: Final[str] = "hillstrom"

DECISION_ID_PREFIX: Final[str] = "dec"


@dataclass(frozen=True)
class LoadResult:
    """What a loader produced, what it could not use, and what it assumed on the way."""

    events: list[Event]
    problems: list[MappingProblem]
    notes: tuple[str, ...]
    arms: dict[str, str] = field(default_factory=dict)
    """Hillstrom only: customer id -> experiment arm ("no_email", "mens_email", "womens_email")."""


# ---------------------------------------------------------------------------
# The long table both sample loaders write, one row per event
# ---------------------------------------------------------------------------
_TS, _EVENT, _SEQ = "ts", "event", "seq"


def _long_mapping(timezone: str, attrs: Sequence[str]) -> EventMapping:
    """The mapping for a long table built here: our own type names, one row per event."""
    return EventMapping(
        customer_id="customer_id",
        timestamp=_TS,
        timezone=timezone,
        event_type_column=_EVENT,
        value_column="value",
        category_column="category",
        channel_column="channel",
        decision_id_column="decision_id",
        attrs={name: name for name in attrs},
        row_key=("customer_id", _EVENT, _SEQ),
    )


def _event_row(
    customer: str,
    kind: EventType,
    ts: datetime,
    seq: int = 0,
    *,
    value: float | None = None,
    category: str | None = None,
    channel: Channel | None = None,
    decision_id: str | None = None,
    **attrs: object,
) -> dict[str, object]:
    return {
        "customer_id": customer,
        _EVENT: kind.value,
        _SEQ: seq,
        _TS: ts,
        "value": value,
        "category": category,
        "channel": channel.value if channel is not None else None,
        "decision_id": decision_id,
        **attrs,
    }


def _map_long(
    rows: list[dict[str, object]], mapping: EventMapping, source: str
) -> tuple[list[Event], list[MappingProblem]]:
    columns = mapping.columns()
    frame = pd.DataFrame(rows, columns=list(columns)) if rows else pd.DataFrame(columns=list(columns))
    # Object columns keep None as None and bools as bools, so empty attrs stay empty.
    frame = frame.astype(object)
    return map_frame(frame, mapping, source)


def _numbers(
    record: Mapping[Hashable, Any], columns: Sequence[str], row: str
) -> dict[str, float] | MappingProblem:
    """The named cells as floats, or the problem that names the first one that is not a number."""
    numbers: dict[str, float] = {}
    for column in columns:
        cell = record[column]
        try:
            number = float(cell)
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number):
            return MappingProblem(
                row=row, code=ProblemCode.BAD_NUMBER, column=column, message="expected a number"
            )
        numbers[column] = number
    return numbers


def _require(frame: pd.DataFrame, columns: Sequence[str], what: str) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise MappingError(f"this is not {what}: it has no column {', '.join(repr(c) for c in missing)}")


# ---------------------------------------------------------------------------
# Online retail: the raw transaction log
# ---------------------------------------------------------------------------
ONLINE_RETAIL_TIMEZONE: Final[str] = "Europe/London"
"""The retailer is UK-based and the log's times carry no offset; they are read as UK local time."""

ONLINE_RETAIL_DATE_FORMAT: Final[str] = "%m/%d/%Y %H:%M"
"""The format of `InvoiceDate` in the fetched copy, the same one `fetch.py` parses."""

_LOG_COLUMNS: Final[tuple[str, ...]] = (
    "InvoiceNo",
    "StockCode",
    "Quantity",
    "InvoiceDate",
    "UnitPrice",
    "CustomerID",
    "Country",
)

ONLINE_RETAIL_LINE_MAPPING: Final[EventMapping] = EventMapping(
    customer_id="CustomerID",
    timestamp="InvoiceDate",
    timestamp_format=ONLINE_RETAIL_DATE_FORMAT,
    timezone=ONLINE_RETAIL_TIMEZONE,
    event_type=EventType.PURCHASE,
    quantity_column="Quantity",
    unit_price_column="UnitPrice",
    negative_as=EventType.REFUND,
    item_column="StockCode",
    attrs={"invoice_no": "InvoiceNo", "quantity": "Quantity", "country": "Country"},
)
"""One event per invoice line; the row key is the line's position in the file."""

ONLINE_RETAIL_LOG_MAPPING: Final[EventMapping] = EventMapping(
    customer_id="CustomerID",
    timestamp="InvoiceDate",
    timestamp_format=ONLINE_RETAIL_DATE_FORMAT,
    timezone=ONLINE_RETAIL_TIMEZONE,
    event_type=EventType.PURCHASE,
    value_column="invoice_value",
    negative_as=EventType.REFUND,
    attrs={"invoice_no": "InvoiceNo", "lines": "lines", "items": "items", "country": "Country"},
    row_key=("InvoiceNo", "CustomerID"),
)
"""One event per invoice, applied to the per-invoice table `online_retail_log_events` builds."""

ONLINE_RETAIL_LOG_NOTES: Final[tuple[str, ...]] = (
    "Lines with no CustomerID (guest checkouts) are left out: they cannot be attributed to anyone.",
    "Lines with a zero or negative UnitPrice (postage adjustments, corrections, samples) are left out, "
    "as fetch.py leaves them out of every monetary aggregate.",
    "A cancellation (InvoiceNo beginning 'C', negative Quantity) becomes a REFUND of the positive amount.",
    "InvoiceDate carries no offset and is read as UK local time (Europe/London).",
    "Description is never read: it is free text and the one column that could hold personal data.",
)


def read_online_retail_log(path: Path = ONLINE_RETAIL_LOG) -> pd.DataFrame:
    """The raw log as text-typed key columns, so an id such as '536365' or 'C536379' is read as written."""
    return pd.read_csv(
        path,
        usecols=list(_LOG_COLUMNS),
        dtype={"InvoiceNo": str, "StockCode": str, "CustomerID": str, "InvoiceDate": str, "Country": str},
    )


def online_retail_log_events(
    frame: pd.DataFrame, *, grain: Literal["invoice", "line"] = "invoice"
) -> LoadResult:
    """Events from the raw online-retail log: one per invoice (the default) or one per invoice line.

    One per invoice is what customer state wants: `purchases` then counts orders, as the dataset's own
    `orders_total` does, rather than order lines. The line grain keeps every `StockCode` as `item_id`.
    """
    _require(frame, _LOG_COLUMNS, "the online-retail transaction log")
    quantity = pd.to_numeric(frame["Quantity"], errors="coerce")
    price = pd.to_numeric(frame["UnitPrice"], errors="coerce")
    # Plain lists, not Series.map: a Series may turn the None for "no id" into NaN.
    customers = [customer_key(v) for v in frame["CustomerID"]]
    invoices = [customer_key(v) for v in frame["InvoiceNo"]]  # the same "drop a trailing .0" rule

    problems: list[MappingProblem] = []
    keep: list[bool] = []
    quantities = [float(v) for v in quantity]
    prices = [float(v) for v in price]
    for label, cid, inv, qty, unit in zip(frame.index, customers, invoices, quantities, prices, strict=True):
        row = str(label)
        reason: tuple[str, str | None, str] | None = None
        if cid is None:
            reason = (ProblemCode.EXCLUDED, "CustomerID", "guest checkout: no customer id")
        elif inv is None:
            reason = (ProblemCode.EXCLUDED, "InvoiceNo", "no invoice number")
        elif math.isnan(qty):
            reason = (ProblemCode.BAD_NUMBER, "Quantity", "expected a number")
        elif math.isnan(unit):
            reason = (ProblemCode.BAD_NUMBER, "UnitPrice", "expected a number")
        elif unit <= 0:
            reason = (
                ProblemCode.EXCLUDED,
                "UnitPrice",
                "zero or negative unit price: an adjustment, not a sale",
            )
        if reason is not None:
            problems.append(MappingProblem(row=row, code=reason[0], column=reason[1], message=reason[2]))
        keep.append(reason is None)

    mask = pd.Series(keep, index=frame.index, dtype=bool)
    if grain == "line":
        events, mapped = map_frame(frame.loc[mask], ONLINE_RETAIL_LINE_MAPPING, ONLINE_RETAIL_SOURCE)
        return LoadResult(events, problems + mapped, ONLINE_RETAIL_LOG_NOTES)

    lines = pd.DataFrame(
        {
            "InvoiceNo": pd.Series(invoices, index=frame.index, dtype=object)[mask],
            "CustomerID": pd.Series(customers, index=frame.index, dtype=object)[mask],
            "InvoiceDate": frame.loc[mask, "InvoiceDate"],
            "Country": frame.loc[mask, "Country"],
            "line_value": quantity[mask] * price[mask],
            "abs_quantity": quantity[mask].abs(),
        }
    )
    invoices_table = (
        lines.groupby(["InvoiceNo", "CustomerID"], sort=False)
        .agg(
            InvoiceDate=("InvoiceDate", "first"),
            Country=("Country", "first"),
            invoice_value=("line_value", "sum"),
            lines=("line_value", "size"),
            items=("abs_quantity", "sum"),
        )
        .reset_index()
    )
    events, mapped = map_frame(invoices_table, ONLINE_RETAIL_LOG_MAPPING, ONLINE_RETAIL_SOURCE)
    return LoadResult(events, problems + mapped, ONLINE_RETAIL_LOG_NOTES)


def load_online_retail_log(
    path: Path = ONLINE_RETAIL_LOG, *, grain: Literal["invoice", "line"] = "invoice"
) -> LoadResult:
    """Read the fetched raw log (run `python library/online-retail/fetch.py` first) and map it."""
    return online_retail_log_events(read_online_retail_log(path), grain=grain)


# ---------------------------------------------------------------------------
# Online retail: the committed per-customer sample
# ---------------------------------------------------------------------------
REFUND_DELAY: Final[timedelta] = timedelta(days=7)
"""How long after a purchase a reconstructed return is dated."""

OUTCOME_OFFSET: Final[timedelta] = timedelta(days=45)
"""Where a reconstructed reactivation purchase is dated: the middle of the 90-day outcome window."""

_LOCAL_NOON: Final[time] = time(12, 0)

_SAMPLE_ATTRS: Final[tuple[str, ...]] = ("country", "approximate", "outcome")

ONLINE_RETAIL_SAMPLE_MAPPING: Final[EventMapping] = _long_mapping(ONLINE_RETAIL_TIMEZONE, _SAMPLE_ATTRS)

_SAMPLE_NUMBERS: Final[tuple[str, ...]] = (
    "recency_days",
    "tenure_days",
    "orders_total",
    "items_total",
    "spend_total",
    "order_value_mean",
    "order_value_max",
    "returned_orders",
    "returned_items",
    "reactivated_90d",
)

ONLINE_RETAIL_SAMPLE_NOTES: Final[tuple[str, ...]] = (
    "sample.csv is one row per lapsed customer at the 2011-09-10 snapshot, not a log; the events are "
    "rebuilt from its aggregates and every one carries attrs approximate=true.",
    "Exact: the number of orders (orders_total), the first and last order dates (snapshot minus "
    "tenure_days and recency_days), total spend (spend_total), the largest order (order_value_max), "
    "the number of returns (returned_orders) and the country.",
    "Invented: the dates of the orders in between (spaced evenly from first to last), the split of "
    "spend (the largest order first, the rest shared equally), the hour (noon UK time), and each "
    "return's date (7 days after an order) and amount (returned_items x the average item price, "
    "spend_total / items_total, capped at spend_total, shared equally).",
    "spend_total excludes returns, so after the REFUND events a customer's folded revenue is net of returns.",
    "Each customer gets a SIGNUP one hour before their first order, with their country.",
    "E-mail consent is granted to every customer at signup: the sample says nothing about consent, "
    "and a win-back demo needs someone it may contact.",
    "The 90-day outcome (reactivated_90d) is after the snapshot and is left out unless include_outcomes "
    "is set; then a returning customer gets one PURCHASE of order_value_mean 45 days after the "
    "snapshot (attrs outcome=true).",
)


def read_online_retail_sample(path: Path = ONLINE_RETAIL_SAMPLE) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"customer_id": str, "snapshot_date": str, "country": str})


def online_retail_sample_events(frame: pd.DataFrame, *, include_outcomes: bool = False) -> LoadResult:
    """Approximate event histories from the per-customer online-retail sample (see the notes)."""
    _require(frame, ("customer_id", "snapshot_date", "country", *_SAMPLE_NUMBERS), "the online-retail sample")
    rows: list[dict[str, object]] = []
    problems: list[MappingProblem] = []
    for label, record in zip(frame.index, frame.to_dict("records"), strict=True):
        customer = customer_key(record["customer_id"])
        row = customer or str(label)
        if customer is None:
            problems.append(
                MappingProblem(
                    row=row, code=ProblemCode.MISSING_CUSTOMER, column="customer_id", message="no customer id"
                )
            )
            continue
        numbers = _numbers(record, _SAMPLE_NUMBERS, row)
        if isinstance(numbers, MappingProblem):
            problems.append(numbers)
            continue
        try:
            snapshot = datetime.combine(date.fromisoformat(str(record["snapshot_date"]).strip()), _LOCAL_NOON)
        except ValueError:
            problems.append(
                MappingProblem(
                    row=row,
                    code=ProblemCode.BAD_TIMESTAMP,
                    column="snapshot_date",
                    message="cannot read the snapshot date as YYYY-MM-DD",
                )
            )
            continue
        country = record["country"] if isinstance(record["country"], str) else None
        rows.extend(_retail_history(customer, snapshot, numbers, country, include_outcomes))
    events, mapped = _map_long(rows, ONLINE_RETAIL_SAMPLE_MAPPING, ONLINE_RETAIL_SOURCE)
    return LoadResult(events, problems + mapped, ONLINE_RETAIL_SAMPLE_NOTES)


def _retail_history(
    customer: str,
    snapshot: datetime,
    n: Mapping[str, float],
    country: str | None,
    include_outcomes: bool,
) -> list[dict[str, object]]:
    orders = max(int(n["orders_total"]), 1)
    last = snapshot - timedelta(days=n["recency_days"])
    first = min(snapshot - timedelta(days=n["tenure_days"]), last)
    step = (last - first) / (orders - 1) if orders > 1 else timedelta(0)
    total, largest = n["spend_total"], min(n["order_value_max"], n["spend_total"])
    rest = (total - largest) / (orders - 1) if orders > 1 else 0.0

    signup_at = first - timedelta(hours=1)
    rows = [
        _event_row(customer, EventType.SIGNUP, signup_at, country=country, approximate=True),
        _event_row(customer, EventType.CONSENT_GRANTED, signup_at, channel=Channel.EMAIL, approximate=True),
    ]
    order_dates = [first + step * i for i in range(orders)]
    for i, ts in enumerate(order_dates):
        value = total if orders == 1 else (largest if i == 0 else rest)
        rows.append(_event_row(customer, EventType.PURCHASE, ts, i, value=value, approximate=True))

    returns = max(int(n["returned_orders"]), 0)
    item_price = total / n["items_total"] if n["items_total"] > 0 else 0.0
    # A few customers returned more items than they bought before the snapshot (lines bought before
    # the log starts); the estimate is capped at their spend so folded revenue never goes below zero.
    refunded = min(max(n["returned_items"], 0.0) * item_price, total)
    refund_each = refunded / returns if returns else 0.0
    for j in range(returns):
        ts = order_dates[j % orders] + REFUND_DELAY
        rows.append(_event_row(customer, EventType.REFUND, ts, j, value=refund_each, approximate=True))

    if include_outcomes and n["reactivated_90d"] >= 1:
        rows.append(
            _event_row(
                customer,
                EventType.PURCHASE,
                snapshot + OUTCOME_OFFSET,
                orders,
                value=n["order_value_mean"],
                approximate=True,
                outcome=True,
            )
        )
    return rows


def load_online_retail_sample(
    path: Path = ONLINE_RETAIL_SAMPLE, *, include_outcomes: bool = False
) -> LoadResult:
    return online_retail_sample_events(read_online_retail_sample(path), include_outcomes=include_outcomes)


# ---------------------------------------------------------------------------
# Hillstrom: a randomised e-mail experiment
# ---------------------------------------------------------------------------
HILLSTROM_CAMPAIGN_AT: Final[datetime] = datetime(2008, 3, 3, 14, 0, tzinfo=UTC)
"""When the e-mail is dated. The file has no dates; this Monday in March 2008 (the challenge was
published that month) is an assumption, and any other aware datetime can be passed instead."""

HILLSTROM_ARMS: Final[dict[str, str]] = {
    "No E-Mail": "no_email",
    "Mens E-Mail": "mens_email",
    "Womens E-Mail": "womens_email",
}
"""`segment` -> arm. Each arm was assigned to a third of customers at random."""

ARM_PROBABILITY: Final[float] = 1 / 3
"""The chance of each arm under the experiment's design, recorded on each SEND for off-policy evaluation."""

VISIT_OFFSET: Final[timedelta] = timedelta(days=3)
CONVERSION_OFFSET: Final[timedelta] = timedelta(days=4)
"""Outcomes fall in the two weeks after the e-mail; they are dated a few days in, visit before purchase."""

YEAR: Final[timedelta] = timedelta(days=365)
MONTH: Final[timedelta] = timedelta(days=30)

_HILLSTROM_ATTRS: Final[tuple[str, ...]] = (
    "area",
    "purchase_channel",
    "newbie",
    "history_segment",
    "arm",
    "probability",
    "approximate",
    "outcome",
)

HILLSTROM_MAPPING: Final[EventMapping] = _long_mapping("UTC", _HILLSTROM_ATTRS)

_HILLSTROM_NUMBERS: Final[tuple[str, ...]] = (
    "recency",
    "history",
    "mens",
    "womens",
    "newbie",
    "visit",
    "conversion",
    "spend",
)

HILLSTROM_NOTES: Final[tuple[str, ...]] = (
    "The file has no dates. The e-mail is dated campaign_at (default 2008-03-03 14:00 UTC) and every "
    "other date is placed relative to it.",
    "History is approximate (attrs approximate=true): `history`, the past year's spend, becomes one "
    "PURCHASE dated `recency` months before the campaign (30 x recency - 15 days, the middle of that "
    "month), with category 'mens' or 'womens'. A customer who bought both gets two purchases, the "
    "spend split equally, the second halfway back to a year before the campaign.",
    "Each customer gets a SIGNUP with their area (zip_code), purchase channel and newbie flag: a newbie "
    "364 days before the campaign (new within the year), anyone else two years before.",
    "E-mail consent is granted to every customer at signup: two thirds of them were e-mailed in the "
    "experiment, and the control third were eligible for the same e-mail.",
    "A treated customer (Mens or Womens E-Mail) gets a SEND on e-mail at campaign_at with a synthetic "
    "decision_id (stable from the customer id) and attrs arm and probability=1/3, the design's chance "
    "of each arm. The control third get no event; LoadResult.arms names every customer's arm.",
    "Outcomes (attrs outcome=true): a visit is a VISIT 3 days after the e-mail; a conversion is a "
    "PURCHASE of `spend` 4 days after. Both are left out when include_outcomes is false.",
    "Money is 2008 US dollars, as in the file.",
)


def read_hillstrom_sample(path: Path = HILLSTROM_SAMPLE) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"customer_id": str})


def hillstrom_events(
    frame: pd.DataFrame,
    *,
    campaign_at: datetime = HILLSTROM_CAMPAIGN_AT,
    include_outcomes: bool = True,
) -> LoadResult:
    """Events from Hillstrom rows (the sample or the full file): history, consent, sends, outcomes."""
    if campaign_at.tzinfo is None:
        raise ValueError("campaign_at must carry a time zone")
    _require(
        frame,
        ("customer_id", "segment", "history_segment", "zip_code", "channel", *_HILLSTROM_NUMBERS),
        "a Hillstrom table",
    )
    rows: list[dict[str, object]] = []
    problems: list[MappingProblem] = []
    arms: dict[str, str] = {}
    for label, record in zip(frame.index, frame.to_dict("records"), strict=True):
        customer = customer_key(record["customer_id"])
        row = customer or str(label)
        if customer is None:
            problems.append(
                MappingProblem(
                    row=row, code=ProblemCode.MISSING_CUSTOMER, column="customer_id", message="no customer id"
                )
            )
            continue
        arm = HILLSTROM_ARMS.get(str(record["segment"]).strip())
        if arm is None:
            problems.append(
                MappingProblem(
                    row=row,
                    code=ProblemCode.UNKNOWN_EVENT_TYPE,
                    column="segment",
                    message=f"not one of the experiment's arms: {', '.join(HILLSTROM_ARMS)}",
                )
            )
            continue
        numbers = _numbers(record, _HILLSTROM_NUMBERS, row)
        if isinstance(numbers, MappingProblem):
            problems.append(numbers)
            continue
        arms[customer] = arm
        rows.extend(_hillstrom_rows(customer, record, numbers, arm, campaign_at, include_outcomes))
    events, mapped = _map_long(rows, HILLSTROM_MAPPING, HILLSTROM_SOURCE)
    return LoadResult(events, problems + mapped, HILLSTROM_NOTES, arms)


def hillstrom_decision_id(customer: str) -> str:
    """The synthetic decision behind a treated customer's e-mail: stable, so reloads agree."""
    return stable_id(DECISION_ID_PREFIX, HILLSTROM_SOURCE, customer, "campaign")


def _hillstrom_rows(
    customer: str,
    record: Mapping[Hashable, Any],
    n: Mapping[str, float],
    arm: str,
    campaign_at: datetime,
    include_outcomes: bool,
) -> list[dict[str, object]]:
    year_ago = campaign_at - YEAR
    newbie = n["newbie"] >= 1
    signup_at = year_ago + timedelta(days=1) if newbie else campaign_at - 2 * YEAR
    rows = [
        _event_row(
            customer,
            EventType.SIGNUP,
            signup_at,
            area=_cell_text(record["zip_code"]),
            purchase_channel=_cell_text(record["channel"]),
            newbie=newbie,
            approximate=True,
        ),
        _event_row(customer, EventType.CONSENT_GRANTED, signup_at, channel=Channel.EMAIL, approximate=True),
    ]

    recency = min(max(n["recency"], 1.0), 12.0)  # the file's range; keeps history inside the year
    last_at = campaign_at - (MONTH * recency - timedelta(days=15))
    bought: list[str | None] = [c for c in ("mens", "womens") if n[c] >= 1]
    categories = bought or [None]
    share = n["history"] / len(categories)
    for i, category in enumerate(categories):
        ts = last_at if i == 0 else last_at - (last_at - year_ago) / 2
        rows.append(
            _event_row(
                customer,
                EventType.PURCHASE,
                ts,
                i,
                value=share,
                category=category,
                history_segment=_cell_text(record["history_segment"]),
                approximate=True,
            )
        )

    if arm != "no_email":
        rows.append(
            _event_row(
                customer,
                EventType.SEND,
                campaign_at,
                channel=Channel.EMAIL,
                decision_id=hillstrom_decision_id(customer),
                arm=arm,
                probability=ARM_PROBABILITY,
            )
        )
    if include_outcomes:
        if n["visit"] >= 1:
            rows.append(_event_row(customer, EventType.VISIT, campaign_at + VISIT_OFFSET, outcome=True))
        if n["conversion"] >= 1:
            rows.append(
                _event_row(
                    customer,
                    EventType.PURCHASE,
                    campaign_at + CONVERSION_OFFSET,
                    len(categories),
                    value=n["spend"],
                    outcome=True,
                )
            )
    return rows


def load_hillstrom(
    path: Path = HILLSTROM_SAMPLE,
    *,
    campaign_at: datetime = HILLSTROM_CAMPAIGN_AT,
    include_outcomes: bool = True,
) -> LoadResult:
    return hillstrom_events(
        read_hillstrom_sample(path), campaign_at=campaign_at, include_outcomes=include_outcomes
    )


def _cell_text(cell: object) -> str | None:
    if not isinstance(cell, str):
        return None
    return cell.strip() or None
