"""Shopify's three shapes of the same objects, read into one record shape the connector maps from.

A customer, order or product reaches us three ways:

* a **GraphQL node** from a live page (camelCase, `gid://shopify/Order/1` ids, money as
  `{shopMoney: {amount}}` strings, nested connections as `nodes` or `edges`);
* a **bulk-export line** from the JSONL file: the same fields as the node, but each nested
  connection's children come as their own lines (`__parentId`), regrouped by `bulk.py` into the
  parent's `lineItems.nodes` before they get here;
* a **webhook payload**, which is the REST resource (snake_case, numeric ids, money as plain strings
  in the shop's currency).

`customer_*`, `order_*` and `product_*` turn each into one flat-ish record: numeric ids as text,
UTC ISO timestamps (`_time`), money as floats in the shop's currency, lists of plain dicts. Everything is
tolerant: an unknown field is ignored, a missing optional field becomes None, and nothing here
raises for a strange value (a record whose id is missing fails later, in `record_key`, as one
counted problem).

A webhook copy is marked `_via = "webhook"`. It can lack what only GraphQL has (a line item's
product type, a refund's amount in a multi-currency shop), so `record_key` ranks it a millisecond
below the polled copy of the same version: the hourly catch-up poll then reads the full copy once.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

__all__ = [
    "VIA_WEBHOOK",
    "customer_from_graphql",
    "customer_from_rest",
    "gid",
    "money",
    "numeric_id",
    "order_from_graphql",
    "order_from_rest",
    "parse_time",
    "product_from_graphql",
    "product_from_rest",
]

VIA_WEBHOOK: Final[str] = "webhook"


def numeric_id(value: Any) -> str | None:
    """`gid://shopify/Order/123` or 123 or "123" -> "123"; None when there is no id."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("gid://"):
        text = text.rsplit("/", 1)[-1].split("?", 1)[0]
    return text or None


def gid(kind: str, value: str) -> str:
    """The GraphQL id for a numeric id (`gid("Customer", "123")`), or `value` if it already is one."""
    return value if value.startswith("gid://") else f"gid://shopify/{kind}/{value}"


def parse_time(value: Any) -> datetime | None:
    """An ISO 8601 timestamp with an offset (Shopify always sends one) as an aware datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def money(value: Any) -> float | None:
    """`{"shopMoney": {"amount": "1.50"}}`, `{"shop_money": {...}}`, "1.50" or 1.5 -> 1.5."""
    if isinstance(value, Mapping):
        inner = value.get("shopMoney") or value.get("shop_money")
        if isinstance(inner, Mapping):
            return money(inner.get("amount"))
        if "amount" in value:
            return money(value.get("amount"))
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _nodes(connection: Any) -> list[Mapping[str, Any]]:
    if isinstance(connection, list):
        return [n for n in connection if isinstance(n, Mapping)]
    if not isinstance(connection, Mapping):
        return []
    if isinstance(connection.get("nodes"), list):
        return [n for n in connection["nodes"] if isinstance(n, Mapping)]
    if isinstance(connection.get("edges"), list):
        return [
            e["node"]
            for e in connection["edges"]
            if isinstance(e, Mapping) and isinstance(e.get("node"), Mapping)
        ]
    return []


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, Mapping | list):
        return None
    text = str(value).strip()
    return text or None


def _time(value: Any) -> str | None:
    """A timestamp as canonical UTC text (`2026-10-01T12:00:00Z`).

    GraphQL sends UTC (`...Z`), REST webhook bodies the shop's own offset (`...T08:00:00-04:00`).
    Some event keys hold a time (a consent change), so both copies of one instant must read the same,
    or the webhook copy and the polled copy would become two events. Unreadable text is kept as is."""
    parsed = parse_time(value)
    if parsed is None:
        return _text(value)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


# --- customers --------------------------------------------------------------------------------------


def _consent(state: Any, level: Any, updated: Any, collected: Any = None) -> dict[str, Any] | None:
    text = _text(state)
    if text is None:
        return None
    return {
        "state": text.upper(),
        "opt_in_level": (_text(level) or "").upper() or None,
        "updated_at": _time(updated),
        "collected_from": (_text(collected) or "").upper() or None,
    }


def customer_from_graphql(node: Mapping[str, Any]) -> dict[str, Any]:
    email = node.get("defaultEmailAddress")
    phone = node.get("defaultPhoneNumber")
    return {
        "id": numeric_id(node.get("id")),
        "created_at": _time(node.get("createdAt")),
        "updated_at": _time(node.get("updatedAt")),
        "state": _text(node.get("state")),
        "orders_count": _int(node.get("numberOfOrders")),
        "amount_spent": money(node.get("amountSpent")),
        "email_consent": (
            _consent(
                email.get("marketingState"), email.get("marketingOptInLevel"), email.get("marketingUpdatedAt")
            )
            if isinstance(email, Mapping)
            else None
        ),
        "sms_consent": (
            _consent(
                phone.get("marketingState"),
                phone.get("marketingOptInLevel"),
                phone.get("marketingUpdatedAt"),
                phone.get("marketingCollectedFrom"),
            )
            if isinstance(phone, Mapping)
            else None
        ),
        # Only with the `contact=read` setting (protected customer data level 2); for identity
        # linking alone (`autonomy.connectors.identity` hashes them), never mapped into events.
        "email": _text(email.get("emailAddress")) if isinstance(email, Mapping) else None,
        "phone": _text(phone.get("phoneNumber")) if isinstance(phone, Mapping) else None,
    }


def customer_from_rest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A `customers/create` / `customers/update` webhook body (the REST Customer resource)."""
    email = payload.get("email_marketing_consent")
    sms = payload.get("sms_marketing_consent")
    return {
        "id": numeric_id(payload.get("id")),
        "created_at": _time(payload.get("created_at")),
        "updated_at": _time(payload.get("updated_at")),
        "state": (_text(payload.get("state")) or "").upper() or None,
        "orders_count": _int(payload.get("orders_count")),
        "amount_spent": money(payload.get("total_spent")),
        "email_consent": (
            _consent(email.get("state"), email.get("opt_in_level"), email.get("consent_updated_at"))
            if isinstance(email, Mapping) and payload.get("email")
            else None
        ),
        "sms_consent": (
            _consent(
                sms.get("state"),
                sms.get("opt_in_level"),
                sms.get("consent_updated_at"),
                sms.get("consent_collected_from"),
            )
            if isinstance(sms, Mapping) and payload.get("phone")
            else None
        ),
        # Present only for apps with protected customer data level 2; identity linking hashes them.
        "email": _text(payload.get("email")),
        "phone": _text(payload.get("phone")),
        "_via": VIA_WEBHOOK,
    }


# --- orders -----------------------------------------------------------------------------------------


def _line_from_graphql(node: Mapping[str, Any]) -> dict[str, Any]:
    quantity = _int(node.get("quantity")) or 0
    unit = money(node.get("originalUnitPriceSet"))
    total = money(node.get("discountedTotalSet"))
    return {
        "id": numeric_id(node.get("id")),
        "product_id": numeric_id(_get(node, "product", "id")),
        "variant_id": numeric_id(_get(node, "variant", "id")),
        "product_type": _text(_get(node, "product", "productType")),
        "sku": _text(node.get("sku")),
        "title": _text(node.get("title")),
        "quantity": quantity,
        "unit_price": unit,
        "total": total if total is not None else (unit * quantity if unit is not None else None),
    }


def order_from_graphql(node: Mapping[str, Any]) -> dict[str, Any]:
    lines_conn = node.get("lineItems")
    lines = [_line_from_graphql(n) for n in _nodes(lines_conn)]
    more_lines = bool(_get(lines_conn, "pageInfo", "hasNextPage"))
    refunds = []
    for refund in node.get("refunds") or ():
        if isinstance(refund, Mapping):
            refunds.append(
                {
                    "id": numeric_id(refund.get("id")),
                    "created_at": _time(refund.get("createdAt")),
                    "amount": money(refund.get("totalRefundedSet")),
                }
            )
    codes = node.get("discountCodes")
    return {
        "id": numeric_id(node.get("id")),
        "name": _text(node.get("name")),
        "created_at": _time(node.get("createdAt")),
        "updated_at": _time(node.get("updatedAt")),
        "processed_at": _time(node.get("processedAt")),
        "cancelled_at": _time(node.get("cancelledAt")),
        "test": bool(node.get("test")),
        "financial_status": _text(node.get("displayFinancialStatus")),
        "customer_id": numeric_id(_get(node, "customer", "id")),
        "currency": _text(_get(node, "totalPriceSet", "shopMoney", "currencyCode"))
        or _text(node.get("currencyCode")),
        "total": money(node.get("totalPriceSet")),
        "subtotal": money(node.get("subtotalPriceSet")),
        "total_discounts": money(node.get("totalDiscountsSet")),
        "discount_codes": (
            [str(c) for c in codes if isinstance(c, str) and c] if isinstance(codes, list) else []
        ),
        "line_items": lines,
        "line_items_complete": not more_lines,
        "refunds": refunds,
    }


def _rest_refund_amount(refund: Mapping[str, Any], single_currency: bool) -> float | None:
    """A REST refund's total in the shop's currency: its successful refund transactions.

    Transactions are in the presentment currency, so the amount is only trusted when the order was
    paid in the shop's own currency; otherwise None, and the catch-up poll supplies GraphQL's
    `totalRefundedSet.shopMoney`."""
    total = 0.0
    seen = False
    for tx in refund.get("transactions") or ():
        if not isinstance(tx, Mapping):
            continue
        if str(tx.get("kind") or "").lower() != "refund" or str(tx.get("status") or "").lower() != "success":
            continue
        in_shop = money(_get(tx, "amount_set", "shop_money", "amount"))
        if in_shop is None and single_currency:
            in_shop = money(tx.get("amount"))
        if in_shop is None:
            return None
        total += in_shop
        seen = True
    return total if seen else None


def order_from_rest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """An `orders/create` / `orders/updated` webhook body (the REST Order resource)."""
    currency = _text(payload.get("currency"))
    presentment = _text(payload.get("presentment_currency")) or currency
    single = currency is not None and currency == presentment
    lines = []
    for item in payload.get("line_items") or ():
        if not isinstance(item, Mapping):
            continue
        quantity = _int(item.get("quantity")) or 0
        unit = money(item.get("price_set")) if isinstance(item.get("price_set"), Mapping) else None
        if unit is None:
            unit = money(item.get("price"))
        discount = money(item.get("total_discount")) or 0.0
        lines.append(
            {
                "id": numeric_id(item.get("id")),
                "product_id": numeric_id(item.get("product_id")),
                "variant_id": numeric_id(item.get("variant_id")),
                "product_type": None,  # not in the REST payload: the catch-up poll fills it in
                "sku": _text(item.get("sku")),
                "title": _text(item.get("title")),
                "quantity": quantity,
                "unit_price": unit,
                "total": unit * quantity - discount if unit is not None else None,
            }
        )
    refunds = []
    for refund in payload.get("refunds") or ():
        if isinstance(refund, Mapping):
            refunds.append(
                {
                    "id": numeric_id(refund.get("id")),
                    "created_at": _time(refund.get("created_at")),
                    "amount": _rest_refund_amount(refund, single),
                }
            )
    total = money(payload.get("total_price_set"))
    customer = payload.get("customer")
    return {
        "id": numeric_id(payload.get("id")),
        "name": _text(payload.get("name")),
        "created_at": _time(payload.get("created_at")),
        "updated_at": _time(payload.get("updated_at")),
        "processed_at": _time(payload.get("processed_at")),
        "cancelled_at": _time(payload.get("cancelled_at")),
        "test": bool(payload.get("test")),
        "financial_status": (_text(payload.get("financial_status")) or "").upper() or None,
        "customer_id": numeric_id(customer.get("id")) if isinstance(customer, Mapping) else None,
        "currency": currency,
        "total": total if total is not None else money(payload.get("total_price")),
        "subtotal": money(payload.get("subtotal_price")),
        "total_discounts": money(payload.get("total_discounts")),
        "discount_codes": [
            str(c["code"])
            for c in payload.get("discount_codes") or ()
            if isinstance(c, Mapping) and c.get("code")
        ],
        "line_items": lines,
        "line_items_complete": True,
        "refunds": refunds,
        "_via": VIA_WEBHOOK,
    }


# --- products ---------------------------------------------------------------------------------------


def product_from_graphql(node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": numeric_id(node.get("id")),
        "title": _text(node.get("title")),
        "product_type": _text(node.get("productType")),
        "vendor": _text(node.get("vendor")),
        "status": (_text(node.get("status")) or "").upper() or None,
        "created_at": _time(node.get("createdAt")),
        "updated_at": _time(node.get("updatedAt")),
        "price": money(_get(node, "priceRangeV2", "minVariantPrice")),
        "currency": _text(_get(node, "priceRangeV2", "minVariantPrice", "currencyCode")),
    }


def product_from_rest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A `products/update` webhook body (the REST Product resource)."""
    prices = [
        p
        for p in (money(v.get("price")) for v in payload.get("variants") or () if isinstance(v, Mapping))
        if p is not None
    ]
    return {
        "id": numeric_id(payload.get("id")),
        "title": _text(payload.get("title")),
        "product_type": _text(payload.get("product_type")),
        "vendor": _text(payload.get("vendor")),
        "status": (_text(payload.get("status")) or "").upper() or None,
        "created_at": _time(payload.get("created_at")),
        "updated_at": _time(payload.get("updated_at")),
        "price": min(prices) if prices else None,
        "currency": None,
        "_via": VIA_WEBHOOK,
    }
