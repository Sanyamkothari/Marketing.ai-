"""Shopify records (see `normalize.py`) as Autonomy events, and products as catalogue `Product`s.

| Record | Events | Stable key (after the shop domain) |
|---|---|---|
| order | one `PURCHASE` per order (not per line, so `purchases` counts orders) | `purchase`, order id |
| order refund | one `REFUND` per refund, at the refund's time, positive amount | `refund`, order id, refund id |
| voided order | one `REFUND` of what was never paid (total less refunds), at `cancelled_at` | `refund`, order id, `void` |
| customer | one `SIGNUP` at `created_at` | `signup`, customer id |
| customer consent | `CONSENT_GRANTED` (state `SUBSCRIBED`) or `CONSENT_REVOKED` (anything else) per channel | `consent`, customer id, channel, state, time |
| product | no event: a `Product` for the catalogue (`products_from_records`) | |

A purchase's `value` is the order total in the shop's currency (`totalPriceSet.shopMoney`), its
`item_id` the product id of its biggest line and `category` that product's type; `attrs` carry the
discount codes used (`offer_id` is the first one, which is what the state fold reads as "bought
with an offer"), the line and unit counts and the currency. Test orders are left out, and so are
guest checkouts (no customer id), each reported as an excluded row rather than silently dropped.

An order whose payment was voided or whose authorisation expired (`VOIDED`, `EXPIRED`: typically
a cancelled order that was never captured) brought in no money, but its `PURCHASE` may already be
stored (events are immutable). So it keeps its purchase and gets one `REFUND` for the unpaid
amount, the same convention as a cancellation in `autonomy.data.loaders`: revenue nets to what was
actually paid, whichever copy of the order is read first.

Consent changes are their own events, keyed by state and time, so a re-read of an unchanged
customer adds nothing and every change is kept in order. A customer with no email (or no phone)
gets no email (or SMS) consent event: Shopify sends no consent object then.

The customer id is Shopify's numeric customer id, as text: the same id Klaviyo's Shopify
integration knows the profile by.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from autonomy.catalog.products import Product
from autonomy.connectors.base import MappedRecords, map_with
from autonomy.contracts import EventType
from autonomy.data.mapping import EventMapping, MappingProblem, ProblemCode

__all__ = [
    "CONSENT_MAPPING",
    "PURCHASE_MAPPING",
    "REFUND_MAPPING",
    "SIGNUP_MAPPING",
    "SOURCE",
    "map_customers",
    "map_orders",
    "products_from_records",
]

SOURCE: Final[str] = "shopify"
GRANTED_STATES: Final[frozenset[str]] = frozenset({"SUBSCRIBED"})
UNPAID_STATUSES: Final[frozenset[str]] = frozenset({"VOIDED", "EXPIRED"})
"""Financial statuses of an order that took no money (`displayFinancialStatus` / REST, upper-cased)."""
VOID_REFUND_ID: Final[str] = "void"

PURCHASE_MAPPING: Final[EventMapping] = EventMapping(
    customer_id="customer_id",
    timestamp="ts",
    event_type=EventType.PURCHASE,
    value_column="value",
    item_column="item_id",
    category_column="category",
    attrs={
        "order_id": "order_id",
        "order_name": "order_name",
        "currency": "currency",
        "discount_codes": "discount_codes",
        "offer_id": "offer_id",
        "discounts": "discounts",
        "lines": "lines",
        "quantity": "quantity",
        "financial_status": "financial_status",
    },
    row_key=("_row", "order_id"),
)

REFUND_MAPPING: Final[EventMapping] = EventMapping(
    customer_id="customer_id",
    timestamp="ts",
    event_type=EventType.REFUND,
    value_column="value",
    attrs={"order_id": "order_id", "refund_id": "refund_id", "currency": "currency"},
    row_key=("_row", "order_id", "refund_id"),
)

SIGNUP_MAPPING: Final[EventMapping] = EventMapping(
    customer_id="customer_id",
    timestamp="ts",
    event_type=EventType.SIGNUP,
    attrs={"customer_state": "customer_state"},
    row_key=("_row", "customer_id"),
)

CONSENT_MAPPING: Final[EventMapping] = EventMapping(
    customer_id="customer_id",
    timestamp="ts",
    event_type_column="type",
    channel_column="channel",
    attrs={
        "consent_state": "consent_state",
        "opt_in_level": "opt_in_level",
        "collected_from": "collected_from",
    },
    row_key=("_row", "customer_id", "channel", "consent_state", "ts"),
)


def _combine(*parts: MappedRecords, extra: Sequence[MappingProblem] = ()) -> MappedRecords:
    events = tuple(e for p in parts for e in p.events)
    problems = tuple(extra) + tuple(q for p in parts for q in p.problems)
    return MappedRecords(events=events, problems=problems)


def _excluded(row: str, column: str, message: str) -> MappingProblem:
    return MappingProblem(row=row, code=ProblemCode.EXCLUDED, column=column, message=message)


def map_orders(records: Sequence[Mapping[str, Any]], account: str) -> MappedRecords:
    purchases: list[dict[str, Any]] = []
    refunds: list[dict[str, Any]] = []
    problems: list[MappingProblem] = []
    for order in records:
        oid = order.get("id")
        if not oid:
            continue
        if order.get("test"):
            problems.append(_excluded(str(oid), "test", "a test order"))
            continue
        customer = order.get("customer_id")
        if not customer:
            problems.append(_excluded(str(oid), "customer_id", "guest checkout: no customer id"))
            continue
        lines = [li for li in order.get("line_items") or () if isinstance(li, Mapping)]
        main = max(lines, key=lambda li: (li.get("total") or 0.0, str(li.get("id") or "")), default=None)
        codes = [str(c) for c in order.get("discount_codes") or () if c]
        purchases.append(
            {
                "_row": "purchase",
                "order_id": str(oid),
                "customer_id": str(customer),
                "ts": order.get("processed_at") or order.get("created_at"),
                "value": order.get("total"),
                "item_id": main.get("product_id") if main else None,
                "category": main.get("product_type") if main else None,
                "order_name": order.get("name"),
                "currency": order.get("currency"),
                "discount_codes": ",".join(codes)[:500] or None,
                "offer_id": codes[0] if codes else None,
                "discounts": order.get("total_discounts"),
                "lines": len(lines),
                "quantity": sum(int(li.get("quantity") or 0) for li in lines),
                "financial_status": order.get("financial_status"),
            }
        )
        refunded = 0.0
        for refund in order.get("refunds") or ():
            if not isinstance(refund, Mapping) or not refund.get("id"):
                continue
            amount = refund.get("amount")
            if amount is None or amount <= 0:
                continue  # a refund of shipping only, or an amount only the poll can tell
            refunded += float(amount)
            refunds.append(
                {
                    "_row": "refund",
                    "order_id": str(oid),
                    "refund_id": str(refund["id"]),
                    "customer_id": str(customer),
                    "ts": refund.get("created_at") or order.get("updated_at"),
                    "value": amount,
                    "currency": order.get("currency"),
                }
            )
        total = order.get("total")
        status = str(order.get("financial_status") or "").upper()
        if status in UNPAID_STATUSES and isinstance(total, int | float) and total - refunded > 0:
            refunds.append(
                {
                    "_row": "refund",
                    "order_id": str(oid),
                    "refund_id": VOID_REFUND_ID,
                    "customer_id": str(customer),
                    "ts": order.get("cancelled_at") or order.get("updated_at"),
                    "value": round(total - refunded, 2),
                    "currency": order.get("currency"),
                }
            )
    return _combine(
        map_with(PURCHASE_MAPPING, purchases, source=SOURCE, account=account),
        map_with(REFUND_MAPPING, refunds, source=SOURCE, account=account),
        extra=problems,
    )


def map_customers(records: Sequence[Mapping[str, Any]], account: str) -> MappedRecords:
    signups: list[dict[str, Any]] = []
    consents: list[dict[str, Any]] = []
    for customer in records:
        cid = customer.get("id")
        if not cid:
            continue
        created = customer.get("created_at")
        signups.append(
            {
                "_row": "signup",
                "customer_id": str(cid),
                "ts": created,
                "customer_state": customer.get("state"),
            }
        )
        for channel, key in (("email", "email_consent"), ("sms", "sms_consent")):
            consent = customer.get(key)
            if not isinstance(consent, Mapping) or not consent.get("state"):
                continue
            state = str(consent["state"]).upper()
            consents.append(
                {
                    "_row": "consent",
                    "customer_id": str(cid),
                    "channel": channel,
                    "type": (
                        EventType.CONSENT_GRANTED.value
                        if state in GRANTED_STATES
                        else EventType.CONSENT_REVOKED.value
                    ),
                    # When Shopify does not say when consent changed, the customer's creation time
                    # keeps the event id stable across re-reads (a later change has a new state).
                    "ts": consent.get("updated_at") or created,
                    "consent_state": state,
                    "opt_in_level": consent.get("opt_in_level"),
                    "collected_from": consent.get("collected_from"),
                }
            )
    return _combine(
        map_with(SIGNUP_MAPPING, signups, source=SOURCE, account=account),
        map_with(CONSENT_MAPPING, consents, source=SOURCE, account=account),
    )


def products_from_records(records: Sequence[Mapping[str, Any]]) -> list[Product]:
    """Catalogue products (`autonomy.catalog.products.Product`): id, title, type and lowest price.

    `item_id` is the product id, the same id purchases carry. Archived products are kept (old
    purchases still refer to them); a record without an id is skipped."""
    products: list[Product] = []
    for record in records:
        pid = record.get("id")
        if not pid:
            continue
        price = record.get("price")
        products.append(
            Product(
                item_id=str(pid),
                name=str(record.get("title") or ""),
                category=record.get("product_type") or None,
                price=float(price) if isinstance(price, int | float) and price >= 0 else 0.0,
            )
        )
    return products
