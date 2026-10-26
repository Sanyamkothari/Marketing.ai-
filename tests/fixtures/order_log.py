"""A small synthetic order log shaped like UCI Online Retail (v3 phase 0, B2 to B4).

`InvoiceNo, StockCode, Description, Quantity, InvoiceDate, UnitPrice, CustomerID, Country`, one row per line of an
order, with the quirks of the real file that the order-log path has to read:

* `InvoiceNo` is text; a cancellation is `C` and the number of its own, with negative quantities;
* `Quantity` is negative on cancellations and on a few stock adjustments that have no `C` and no customer;
* `UnitPrice` is 0 on a few lines;
* `CustomerID` is a float (`12346.0`) because it has blanks, about a quarter of the lines: guest checkouts are whole
  orders without one, so no order is mixed unless `mixed_orders` asks for some;
* `InvoiceDate` is `12/1/2010 8:26`, month first, no padding; `two_timestamp_orders` orders carry two timestamps;
* `Description` is free text.

Pure: the same arguments give the same frame. This is not the real file (that fixture is B7); it is small enough to
read in a test.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import numpy as np
import pandas as pd

__all__ = ["COLUMNS", "FIRST_DAY", "LAST_DAY", "order_log_frame"]

COLUMNS: Final[tuple[str, ...]] = (
    "InvoiceNo",
    "StockCode",
    "Description",
    "Quantity",
    "InvoiceDate",
    "UnitPrice",
    "CustomerID",
    "Country",
)
FIRST_DAY: Final[datetime] = datetime(2010, 12, 1, 8, 26)
LAST_DAY: Final[datetime] = datetime(2011, 12, 9, 12, 50)
_COUNTRIES: Final[tuple[str, ...]] = ("United Kingdom", "France", "Germany", "EIRE", "Spain")
_COUNTRY_P: Final[tuple[float, ...]] = (0.8, 0.07, 0.07, 0.04, 0.02)
_PRODUCTS: Final[tuple[tuple[str, str, float], ...]] = tuple(
    (code, name, price)
    for code, name, price in (
        ("85123A", "WHITE HANGING HEART T-LIGHT HOLDER", 2.55),
        ("71053", "WHITE METAL LANTERN", 3.39),
        ("84406B", "CREAM CUPID HEARTS COAT HANGER", 2.75),
        ("84029G", "KNITTED UNION FLAG HOT WATER BOTTLE", 3.39),
        ("84029E", "RED WOOLLY HOTTIE WHITE HEART", 3.39),
        ("22752", "SET 7 BABUSHKA NESTING BOXES", 7.65),
        ("21730", "GLASS STAR FROSTED T-LIGHT HOLDER", 4.25),
        ("22633", "HAND WARMER UNION JACK", 1.85),
        ("22632", "HAND WARMER RED POLKA DOT", 1.85),
        ("84879", "ASSORTED COLOUR BIRD ORNAMENT", 1.69),
        ("22745", "POPPYS PLAYHOUSE BEDROOM", 2.1),
        ("22748", "POPPYS PLAYHOUSE KITCHEN", 2.1),
        ("22310", "IVORY KNITTED MUG COSY", 1.65),
        ("21071", "VINTAGE BILLBOARD DRINK ME MUG", 1.25),
        ("22960", "JAM MAKING SET WITH JARS", 4.25),
        ("22961", "JAM MAKING SET PRINTED", 1.45),
        ("23084", "RABBIT NIGHT LIGHT", 2.08),
        ("22423", "REGENCY CAKESTAND 3 TIER", 12.75),
        ("47566", "PARTY BUNTING", 4.95),
        ("20725", "LUNCH BAG RED RETROSPOT", 1.65),
    )
)


def _stamp(moment: datetime) -> str:
    return f"{moment.month}/{moment.day}/{moment.year} {moment.hour}:{moment.minute:02d}"


def order_log_frame(
    customers: int = 150,
    *,
    seed: int = 7,
    guest_share: float = 0.25,
    mixed_orders: int = 0,
    two_timestamp_orders: int = 2,
    adjustments: int = 12,
    named_adjustments: int = 3,
) -> pd.DataFrame:
    """An order log of `customers` shoppers, plus guest checkouts so that about `guest_share` of the lines have no
    `CustomerID`, a few `C` cancellations, `adjustments` stock-adjustment lines, `named_adjustments` negative lines that have a shopper and no `C`, and the other
    quirks above."""
    rng = np.random.default_rng(seed)
    span_days = (LAST_DAY - FIRST_DAY).days
    rows: list[dict[str, object]] = []
    invoice = 536_365

    def order_lines(count: int, customer: float, country: str, when: datetime) -> None:
        nonlocal invoice
        number = str(invoice)
        invoice += 1
        for _ in range(count):
            code, name, price = _PRODUCTS[int(rng.integers(0, len(_PRODUCTS)))]
            rows.append(
                {
                    "InvoiceNo": number,
                    "StockCode": code,
                    "Description": name,
                    "Quantity": int(rng.integers(1, 25)),
                    "InvoiceDate": _stamp(when),
                    "UnitPrice": 0.0 if rng.random() < 0.01 else price,
                    "CustomerID": customer,
                    "Country": country,
                }
            )

    cancelled: list[tuple[int, int, datetime]] = []  # (row index of the order's first line, lines, when)
    for index in range(customers):
        customer = float(12_346 + index * 3)
        country = str(rng.choice(_COUNTRIES, p=_COUNTRY_P))
        orders = 1 + int(rng.geometric(0.35)) % 9
        when = FIRST_DAY + timedelta(
            days=int(rng.integers(0, span_days - 60)), minutes=int(rng.integers(0, 600))
        )
        for _ in range(orders):
            if when > LAST_DAY:
                break
            lines = int(rng.integers(1, 7))
            first = len(rows)
            order_lines(lines, customer, country, when)
            if rng.random() < 0.06:
                cancelled.append((first, lines, when))
            when += timedelta(days=int(rng.gamma(2.0, 25.0)) + 1, minutes=int(rng.integers(0, 300)))
    for (
        first,
        lines,
        when,
    ) in cancelled:  # a cancellation: its own `C` number, the same shopper, negative quantity
        take = int(rng.integers(1, lines + 1))
        number = f"C{invoice}"
        invoice += 1
        for offset in range(take):
            line = dict(rows[first + offset])
            line.update(
                InvoiceNo=number,
                Quantity=-int(line["Quantity"]),  # type: ignore[call-overload]
                InvoiceDate=_stamp(when + timedelta(days=int(rng.integers(1, 20)))),
            )
            rows.append(line)
    named = len(rows)
    guest_lines = round(named * guest_share / (1.0 - guest_share))
    produced = 0
    while produced < guest_lines:  # guest checkouts: whole orders with no CustomerID
        when = FIRST_DAY + timedelta(days=int(rng.integers(0, span_days)), minutes=int(rng.integers(0, 600)))
        lines = int(rng.integers(1, 7))
        before = len(rows)
        order_lines(lines, float("nan"), str(rng.choice(_COUNTRIES, p=_COUNTRY_P)), when)
        produced += len(rows) - before
    for _ in range(adjustments):  # stock adjustments: negative, no `C`, no customer, no price
        code, _name, _price = _PRODUCTS[int(rng.integers(0, len(_PRODUCTS)))]
        when = FIRST_DAY + timedelta(days=int(rng.integers(0, span_days)))
        rows.append(
            {
                "InvoiceNo": str(invoice),
                "StockCode": code,
                "Description": "damaged",
                "Quantity": -int(rng.integers(1, 40)),
                "InvoiceDate": _stamp(when),
                "UnitPrice": 0.0,
                "CustomerID": float("nan"),
                "Country": "United Kingdom",
            }
        )
        invoice += 1
    for index in range(named_adjustments):  # a negative line with a shopper and no `C`: counted by quantity
        code, _name, price = _PRODUCTS[int(rng.integers(0, len(_PRODUCTS)))]
        shopper = float(12_346 + (index * 17 % customers) * 3)
        rows.append(
            {
                "InvoiceNo": str(invoice),
                "StockCode": code,
                "Description": "returned by customer",
                "Quantity": -int(rng.integers(1, 6)),
                "InvoiceDate": _stamp(LAST_DAY - timedelta(days=int(rng.integers(5, 200)))),
                "UnitPrice": price,
                "CustomerID": shopper,
                "Country": "United Kingdom",
            }
        )
        invoice += 1
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    if mixed_orders:  # some lines of an order with a shopper lose it
        orders_with_shopper = frame.loc[frame["CustomerID"].notna(), "InvoiceNo"]
        multi = orders_with_shopper[orders_with_shopper.duplicated(keep=False)].drop_duplicates()
        for number in multi.head(mixed_orders):
            line = frame.index[frame["InvoiceNo"] == number][0]
            frame.loc[line, "CustomerID"] = float("nan")
    if two_timestamp_orders:  # an order whose lines were stamped a minute apart
        multi = frame["InvoiceNo"][
            frame["InvoiceNo"].duplicated(keep=False) & ~frame["InvoiceNo"].str.startswith("C")
        ]
        for number in multi.drop_duplicates().head(two_timestamp_orders):
            line = frame.index[frame["InvoiceNo"] == number][-1]
            moment = datetime.strptime(str(frame.loc[line, "InvoiceDate"]), "%m/%d/%Y %H:%M")
            frame.loc[line, "InvoiceDate"] = _stamp(moment + timedelta(minutes=1))
    order = pd.to_datetime(frame["InvoiceDate"], format="%m/%d/%Y %H:%M").to_numpy().argsort(kind="stable")
    return frame.iloc[order].reset_index(drop=True)
