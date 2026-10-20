"""Shopify's three record shapes read into one, and the events made from them (pure functions)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.connectors import CursorExpired, SyncWindow
from autonomy.connectors.shopify import map_customers, map_orders, products_from_records
from autonomy.connectors.shopify.api import LIVE_PAGE_SIZE, LIVE_QUERIES, MAX_QUERY_COST
from autonomy.connectors.shopify.bulk import BulkCursor, parse_cursor, search_filter
from autonomy.connectors.shopify.normalize import (
    customer_from_graphql,
    customer_from_rest,
    money,
    numeric_id,
    order_from_graphql,
    order_from_rest,
    product_from_rest,
)
from autonomy.contracts import Channel, EventType

from .shopify_support import T0, customer_node, order_node, rest_customer, rest_order


def test_ids_money_and_times_are_read_the_same_from_every_shape() -> None:
    assert numeric_id("gid://shopify/Order/820982911946154508") == "820982911946154508"
    assert numeric_id(820982911946154508) == "820982911946154508"
    assert numeric_id(None) is None and numeric_id(True) is None
    assert (
        money({"shopMoney": {"amount": "1.50"}})
        == 1.5
        == money({"shop_money": {"amount": "1.50"}})
        == money("1.50")
    )
    assert money("N/A") is None and money({"presentmentMoney": {"amount": "2"}}) is None


def test_a_webhook_order_and_a_polled_order_give_the_same_purchase() -> None:
    created, updated = T0 - timedelta(days=1), T0
    rest = order_from_rest(rest_order(1001, customer=1, total=40.0, created=created, updated=updated))
    node = order_from_graphql(
        order_node(
            1001,
            customer=1,
            total=40.0,
            created=created,
            updated=updated,
            lines=[(501, "Skin", 1, 40.0)],
            codes=("WELCOME10",),
        )
    )
    a = map_orders([rest], "acme.myshopify.com").events
    b = map_orders([node], "acme.myshopify.com").events
    assert len(a) == len(b) == 1
    assert a[0].event_id == b[0].event_id  # same stable id: whichever comes first, the other is a no-op
    assert (a[0].value, a[0].item_id, a[0].attrs["offer_id"]) == (
        b[0].value,
        b[0].item_id,
        b[0].attrs["offer_id"],
    )
    assert rest["_via"] == "webhook" and "_via" not in node
    other_shop = map_orders([node], "other.myshopify.com").events[0]
    assert other_shop.event_id != b[0].event_id  # two shops' order 1001 never collide


def test_rest_refunds_are_trusted_only_in_the_shops_own_currency() -> None:
    refund = [(9001, T0, 8.0)]
    same = order_from_rest(rest_order(1, customer=1, total=40.0, created=T0, updated=T0, refunds=refund))
    foreign = order_from_rest(
        rest_order(1, customer=1, total=40.0, created=T0, updated=T0, refunds=refund, presentment="EUR")
    )
    assert same["refunds"][0]["amount"] == 8.0 and foreign["refunds"][0]["amount"] is None
    assert [e.type for e in map_orders([foreign], "s").events] == [EventType.PURCHASE]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("SUBSCRIBED", EventType.CONSENT_GRANTED),
        ("NOT_SUBSCRIBED", EventType.CONSENT_REVOKED),
        ("UNSUBSCRIBED", EventType.CONSENT_REVOKED),
        ("PENDING", EventType.CONSENT_REVOKED),
        ("REDACTED", EventType.CONSENT_REVOKED),
        ("INVALID", EventType.CONSENT_REVOKED),
    ],
)
def test_only_subscribed_is_consent(state: str, expected: EventType) -> None:
    record = customer_from_graphql(customer_node(5, created=T0, email_state=state))
    events = [e for e in map_customers([record], "s").events if e.channel is Channel.EMAIL]
    assert [e.type for e in events] == [expected]
    assert events[0].ts == T0  # no consent time from Shopify: the creation time keeps the id stable


def test_a_customer_without_email_or_phone_gets_no_consent_event_and_rest_matches_graphql() -> None:
    bare = customer_from_graphql(customer_node(6, created=T0, email_state=None))
    assert [e.type for e in map_customers([bare], "s").events] == [EventType.SIGNUP]
    rest = customer_from_rest(rest_customer(6, created=T0, updated=T0 + timedelta(hours=1)))
    assert rest["email_consent"]["state"] == "SUBSCRIBED" and rest["sms_consent"] is None
    assert rest["state"] == "ENABLED"


def test_products_become_catalogue_entries_from_either_shape() -> None:
    rest = product_from_rest(
        {
            "id": 7,
            "title": "Serum",
            "product_type": "Skincare",
            "status": "active",
            "updated_at": "2026-10-01T12:00:00Z",
            "variants": [{"price": "30.00"}, {"price": "24.50"}, {"price": None}],
        }
    )
    (product,) = products_from_records([rest, {"title": "no id"}])
    assert (product.item_id, product.name, product.category, product.price) == (
        "7",
        "Serum",
        "Skincare",
        24.5,
    )


def test_search_filters_and_bulk_cursors() -> None:
    end = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert search_filter(SyncWindow(None, end)) == "updated_at:<'2026-10-01T12:00:00Z'"
    assert search_filter(SyncWindow(end - timedelta(hours=3), end)).startswith(
        "updated_at:>='2026-10-01T09:00:00Z' AND"
    )
    cursor = BulkCursor("gid://shopify/BulkOperation/77", 1234, 2)
    assert parse_cursor(cursor.text()) == cursor
    assert parse_cursor("bulk:new~1") == BulkCursor(None, 0, 1)
    with pytest.raises(CursorExpired):
        parse_cursor("bulk:garbage")


def test_a_voided_order_keeps_its_purchase_and_gets_one_refund_of_what_was_never_paid() -> None:
    cancelled = T0 + timedelta(hours=2)
    node = order_node(
        1001,
        customer=1,
        total=40.0,
        created=T0,
        updated=cancelled,
        displayFinancialStatus="VOIDED",
        cancelledAt=cancelled.isoformat().replace("+00:00", "Z"),
    )
    events = map_orders([order_from_graphql(node)], "s").events
    assert [(e.type, e.value) for e in events] == [(EventType.PURCHASE, 40.0), (EventType.REFUND, 40.0)]
    (void,) = [e for e in events if e.type is EventType.REFUND]
    assert void.ts == cancelled and void.attrs["refund_id"] == "void"
    # the webhook copy of the same cancellation gives the same two event ids
    rest = rest_order(1001, customer=1, total=40.0, created=T0, updated=cancelled)
    rest.update(financial_status="voided", cancelled_at="2026-10-01T10:00:00-04:00")
    again = map_orders([order_from_rest(rest)], "s").events
    assert [e.event_id for e in again] == [e.event_id for e in events]
    assert again[1].ts == cancelled
    # a paid order, or one already refunded in full, gets no extra refund
    paid = map_orders([order_from_graphql(order_node(1002, customer=1, total=9.0, created=T0))], "s")
    assert [e.type for e in paid.events] == [EventType.PURCHASE]
    refunded = order_node(
        1003,
        customer=1,
        total=9.0,
        created=T0,
        refunds=[(77, T0, 9.0)],
        displayFinancialStatus="VOIDED",
    )
    types = [e.type for e in map_orders([order_from_graphql(refunded)], "s").events]
    assert types == [EventType.PURCHASE, EventType.REFUND]


def test_a_consent_change_read_from_a_webhook_and_from_the_poll_is_one_event() -> None:
    changed = T0 + timedelta(hours=1)
    body = rest_customer(7, created=T0 - timedelta(days=9), updated=changed, email_state="unsubscribed")
    body["email_marketing_consent"]["consent_updated_at"] = "2026-10-01T09:00:00-04:00"  # shop's offset
    rest = customer_from_rest(body)
    polled = customer_from_graphql(
        customer_node(
            7, created=T0 - timedelta(days=9), updated=changed, email_state="UNSUBSCRIBED", email_at=changed
        )
    )
    assert (
        rest["email_consent"]["updated_at"] == polled["email_consent"]["updated_at"] == "2026-10-01T13:00:00Z"
    )
    ids = [
        {e.event_id for e in map_customers([r], "s").events if e.channel is Channel.EMAIL}
        for r in (rest, polled)
    ]
    assert ids[0] == ids[1] and len(ids[0]) == 1


def _requested_cost(text: str, first: int) -> int:
    """Shopify's requested query cost, as documented: an object 1, a scalar 0, a connection (or a
    list taking `first`) 2 + first x one node's cost, multiplied through the nesting; `$first` is
    the page size the connector sends."""
    tokens = re.findall(r"\$?[A-Za-z_][A-Za-z0-9_]*|[0-9]+|\.\.\.|\S", text[text.index("{") :])
    pos = 0

    def selection() -> int:
        nonlocal pos
        assert tokens[pos] == "{"
        pos += 1
        total = 0
        while tokens[pos] != "}":
            name, size = tokens[pos], None
            pos += 1
            if tokens[pos] == "(":
                depth, args = 0, []
                while True:
                    token = tokens[pos]
                    pos += 1
                    args.append(token)
                    depth += {"(": 1, ")": -1}.get(token, 0)
                    if depth == 0:
                        break
                for i, arg in enumerate(args[:-2]):
                    if arg == "first":
                        value = args[i + 2]
                        size = first if value.startswith("$") else int(value)
            if tokens[pos] == "{":
                child = selection()
                if name in ("nodes", "edges", "node"):
                    total += child
                elif size is not None:
                    total += 2 + size * child
                else:
                    total += 1 + child
        pos += 1
        return total

    return selection()


def test_every_live_query_stays_under_shopifys_single_query_cost_limit() -> None:
    for stream, text in LIVE_QUERIES.items():
        cost = _requested_cost(text, LIVE_PAGE_SIZE[stream])
        assert 0 < cost < MAX_QUERY_COST * 0.8, (stream, cost)  # a margin for Shopify's own rounding
    assert _requested_cost(LIVE_QUERIES["orders"], 20) > MAX_QUERY_COST  # the estimate does bite
