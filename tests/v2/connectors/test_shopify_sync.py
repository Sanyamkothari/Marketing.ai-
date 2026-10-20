"""Shopify reads: live pages into events, bulk-operation history, cost pacing, throttles, drift, failures."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import httpx

from autonomy.catalog.products import Product
from autonomy.connectors import Connection, ConnectionStatus, Lane, Stream
from autonomy.connectors.shopify import ShopifyConnector
from autonomy.contracts import Channel, EventType

from .shopify_support import T0, FakeShopify, connect_token, make_shopify_rig

CONNECTED = T0
NOW = T0 + timedelta(hours=1)


def shop_with_recent_data() -> FakeShopify:
    fake = FakeShopify()
    fake.add_customer(
        1,
        created=T0 - timedelta(days=400),
        updated=T0 - timedelta(minutes=30),
        email_at=T0 - timedelta(days=3),
    )
    fake.add_customer(
        2,
        created=T0 - timedelta(days=5),
        updated=T0 - timedelta(minutes=40),
        email_state="NOT_SUBSCRIBED",
        sms_state="SUBSCRIBED",
    )
    fake.add_order(
        1001,
        customer=1,
        total=40.0,
        created=T0 - timedelta(days=1),
        updated=T0 - timedelta(minutes=20),
        lines=[(501, "Skincare", 1, 30.0), (502, "Bath", 2, 5.0)],
        codes=("WELCOME10",),
        refunds=[(9001, T0 - timedelta(minutes=25), 10.0)],
    )
    fake.add_order(1002, customer=None, total=15.0, created=T0 - timedelta(minutes=50))  # guest checkout
    fake.add_order(1003, customer=1, total=99.0, created=T0 - timedelta(minutes=45), test=True)
    return fake


def test_live_pages_become_purchase_refund_signup_and_consent_events_once() -> None:
    fake = shop_with_recent_data()
    fake.page_size = 1  # every record its own page: the cursor is saved between them
    rig, _ = make_shopify_rig(fake)
    connection = connect_token(rig, CONNECTED)

    orders = rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, NOW)
    customers = rig.engine.sync(connection.connection_id, Stream.CUSTOMERS, Lane.LIVE, NOW)
    assert (orders.status, customers.status) == ("ok", "ok")
    assert orders.rows == 3 and orders.problems == 2  # the guest checkout and the test order, reported
    events = list(rig.events.scan())
    by_type = {e.type: [x for x in events if x.type is e.type] for e in events}

    (purchase,) = by_type[EventType.PURCHASE]
    assert (purchase.customer_id, purchase.value, purchase.item_id, purchase.category) == (
        "1",
        40.0,
        "501",
        "Skincare",
    )
    assert purchase.attrs["offer_id"] == "WELCOME10" and purchase.attrs["quantity"] == 3
    assert purchase.source == "shopify" and purchase.ts == T0 - timedelta(days=1)
    (refund,) = by_type[EventType.REFUND]
    assert (refund.value, refund.ts, refund.attrs["order_id"]) == (10.0, T0 - timedelta(minutes=25), "1001")
    assert {e.customer_id for e in by_type[EventType.SIGNUP]} == {"1", "2"}
    consent = {(e.customer_id, e.channel, e.type) for e in events if e.channel is not None}
    assert consent == {
        ("1", Channel.EMAIL, EventType.CONSENT_GRANTED),
        ("2", Channel.EMAIL, EventType.CONSENT_REVOKED),
        ("2", Channel.SMS, EventType.CONSENT_GRANTED),
    }

    # the next poll re-reads the overlap: nothing new, nothing duplicated
    count = rig.events.count()
    again = rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, NOW + timedelta(hours=1))
    assert again.status == "ok" and again.events == 0 and rig.events.count() == count

    # a consent change is a new event; the old one stays (history in order)
    fake.customers["1"]["defaultEmailAddress"]["marketingState"] = "UNSUBSCRIBED"
    fake.customers["1"]["defaultEmailAddress"]["marketingUpdatedAt"] = "2026-10-01T13:30:00Z"
    fake.customers["1"]["updatedAt"] = "2026-10-01T13:30:00Z"
    rig.engine.sync(connection.connection_id, Stream.CUSTOMERS, Lane.LIVE, NOW + timedelta(hours=1))
    revoked = [e for e in rig.events.for_customer("1") if e.type is EventType.CONSENT_REVOKED]
    assert len(revoked) == 1 and revoked[0].attrs["consent_state"] == "UNSUBSCRIBED"


def old_orders(fake: FakeShopify, n: int, *, days_ago: int) -> None:
    for i in range(n):
        created = T0 - timedelta(days=days_ago, hours=i)
        fake.add_order(
            2000 + i, customer=10 + i, total=10.0 + i, created=created, lines=[(600 + i, "Tea", 1, 10.0 + i)]
        )


def run_history(rig, connection: Connection, stream: Stream, *, rounds: int = 10):  # type: ignore[no-untyped-def]
    """Run the history lane like the worker would: come back when the export says."""
    report = None
    for _ in range(rounds):
        report = rig.engine.sync(connection.connection_id, stream, Lane.HISTORY, rig.clock.current)
        if report.status in ("ok", "done", "retry"):
            return report
        if report.retry_at is not None:
            rig.clock.current = max(rig.clock.current, report.retry_at)
    return report


def test_history_runs_as_a_bulk_export_paged_by_byte_offset_with_line_items_regrouped() -> None:
    fake = FakeShopify()
    old_orders(fake, 5, days_ago=10)
    connector = ShopifyConnector()
    connector.bulk_page_records = 2
    rig, _ = make_shopify_rig(fake, connector=connector)
    connection = connect_token(rig, CONNECTED)

    first = rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.HISTORY, T0)
    assert first.status == "waiting" and first.retry_at == T0 + timedelta(seconds=15)
    (op,) = fake.bulk_ops.values()
    assert op.resource == "orders"
    assert op.search == "updated_at:>='2026-09-01T12:00:00Z' AND updated_at:<'2026-10-01T12:00:00Z'"

    report = run_history(rig, connection, Stream.ORDERS)
    assert report is not None and report.status == "ok" and report.rows == 5
    purchases = [e for e in rig.events.scan() if e.type is EventType.PURCHASE]
    assert sorted((e.customer_id, e.item_id) for e in purchases) == [
        (str(10 + i), str(600 + i)) for i in range(5)
    ]
    downloads = list(fake.ranges)
    assert downloads[0] is None and all(r and r.startswith("bytes=") for r in downloads[1:])
    assert len(downloads) == 3  # 2 + 2 + 1 orders: each page resumes where the last stopped
    assert rig.history_calls and sum(rig.history_calls) == 5  # the runtime is told to rebuild state
    position = rig.store.position(connection.connection_id, Stream.ORDERS, Lane.HISTORY)
    assert position.reached == T0 - timedelta(days=30) and position.cursor is None


def test_a_failed_export_is_started_again_and_three_in_a_row_show_as_errors() -> None:
    fake = FakeShopify()
    old_orders(fake, 2, days_ago=3)
    rig, _ = make_shopify_rig(fake)
    connection = connect_token(rig, CONNECTED)

    fake.bulk_fail_next = "INTERNAL_SERVER_ERROR"
    report = run_history(rig, connection, Stream.ORDERS)
    assert report is not None and report.status == "ok" and report.rows == 2
    assert [o.status for o in fake.bulk_ops.values()] == ["FAILED", "COMPLETED"]

    # a shop whose exports keep failing: after three, the history lane records errors (one alert later)
    fresh, fake2 = make_shopify_rig(FakeShopify())
    old_orders(fake2, 1, days_ago=3)
    connection2 = connect_token(fresh, CONNECTED)
    fake2.bulk_fail_next, fake2.bulk_fail_count = "INTERNAL_SERVER_ERROR", 3
    report = run_history(fresh, connection2, Stream.ORDERS)
    assert report is not None and report.status == "retry"
    assert len(fake2.bulk_ops) == 3  # started three times in a row, then reported
    position = fresh.store.position(connection2.connection_id, Stream.ORDERS, Lane.HISTORY)
    assert position.error_streak >= 1 and position.last_error_code == "connector.transient"
    # it heals by itself: six hours after the last failure the export is tried afresh
    fresh.clock.current += timedelta(hours=7)
    healed = run_history(fresh, connection2, Stream.ORDERS)
    assert healed is not None and healed.status == "ok" and healed.rows == 1 and len(fake2.bulk_ops) == 4


def test_a_busy_shop_and_an_expired_download_link_are_waited_out_and_restarted() -> None:
    fake = FakeShopify()
    old_orders(fake, 2, days_ago=3)
    fake.bulk_busy = 1
    fake.gone_downloads = 1
    rig, _ = make_shopify_rig(fake)
    connection = connect_token(rig, CONNECTED)
    first = rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.HISTORY, T0)
    assert first.status == "waiting" and first.retry_at == T0 + timedelta(minutes=1) and not fake.bulk_ops
    report = run_history(rig, connection, Stream.ORDERS)
    assert report is not None and report.status == "ok" and report.rows == 2
    assert len(fake.bulk_ops) == 2  # the first file's link had expired: exported again


def test_shopify_cost_limits_are_respected_before_and_after_a_throttle() -> None:
    fake = shop_with_recent_data()
    fake.available, fake.query_cost, fake.restore_rate = 10.0, 50.0, 100.0  # 40 points short: 0.4 s
    throttled = httpx.Response(
        200,
        json={
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 300,
                    "throttleStatus": {"maximumAvailable": 2000, "currentlyAvailable": 0, "restoreRate": 100},
                }
            },
        },
    )
    fake.page_size = 1
    rig, _ = make_shopify_rig(fake)
    connection = connect_token(rig, CONNECTED)
    fake.failures = [throttled, httpx.Response(429, headers={"Retry-After": "2"})]
    report = rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, NOW)
    assert report.status == "ok" and report.rows == 3
    assert 3.0 in rig.clock.slept  # THROTTLED: waited for 300 points at 100/s
    assert 2.0 in rig.clock.slept  # 429: honoured Retry-After
    assert any(abs(s - 0.4) < 1e-9 for s in rig.clock.slept)  # paced ahead of the next query


def test_access_denied_on_a_field_asks_for_reconnect_once() -> None:
    fake = shop_with_recent_data()
    fake.denied_resources = {"customers"}
    rig, _ = make_shopify_rig(fake)
    connection = connect_token(rig, CONNECTED)
    for _ in range(2):
        report = rig.engine.sync(connection.connection_id, Stream.CUSTOMERS, Lane.LIVE, NOW)
    assert report.status in ("reconnect", "skipped")
    current = rig.store.get_connection(connection.connection_id)
    assert current is not None and current.status is ConnectionStatus.NEEDS_RECONNECT
    alerts = [a for a in rig.alerts.list_alerts() if a.code == "connector.needs_reconnect"]
    assert len(alerts) == 1 and "ACCESS_DENIED" in alerts[0].message


def test_schema_drift_is_counted_and_unknown_or_missing_optional_fields_never_crash() -> None:
    fake = FakeShopify()
    order = fake.add_order(3001, customer=7, total=20.0, created=T0 - timedelta(minutes=30))
    del order["totalPriceSet"]  # a renamed field: the record lacks something we rely on
    order["brandNewField"] = {"nested": [1, 2]}
    other = fake.add_order(3002, customer=8, total=12.0, created=T0 - timedelta(minutes=20))
    for optional in ("lineItems", "refunds", "discountCodes", "processedAt", "displayFinancialStatus"):
        del other[optional]
    rig, _ = make_shopify_rig(fake)
    connection = connect_token(rig, CONNECTED)
    report = rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, NOW)
    assert report.status == "ok" and report.rows == 2 and report.missing_required == 1
    purchases = [e for e in rig.events.scan() if e.type is EventType.PURCHASE]
    assert [(e.customer_id, e.value, e.item_id) for e in purchases] == [("8", 12.0, None)]
    assert report.problems == 1  # the order with no total could not be a purchase: one counted problem


def test_products_go_to_the_catalogue_and_make_no_customer_events() -> None:
    received: list[tuple[str, Sequence[Product]]] = []
    fake = FakeShopify()
    fake.add_product(501, title="Serum", ptype="Skincare", price=30.0, updated=T0 - timedelta(minutes=10))
    fake.add_product(502, title="Old soap", ptype="Bath", price=5.0, updated=T0 - timedelta(days=900))
    connector = ShopifyConnector(
        product_sink=lambda c, products: received.append((c.connection_id, products))
    )
    rig, _ = make_shopify_rig(fake, connector=connector)
    connection = connect_token(rig, CONNECTED)
    rig.engine.sync(connection.connection_id, Stream.PRODUCTS, Lane.LIVE, NOW)
    report = run_history(rig, connection, Stream.PRODUCTS)  # one export: every product ever updated
    assert report is not None and report.status == "done"
    items = {p.item_id: p for _, batch in received for p in batch}
    assert items["501"] == Product(item_id="501", name="Serum", category="Skincare", price=30.0)
    assert items["502"].name == "Old soap"
    assert rig.events.count() == 0
    assert fake.bulk_ops and next(iter(fake.bulk_ops.values())).resource == "products"
