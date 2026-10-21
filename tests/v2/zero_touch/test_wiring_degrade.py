"""A 403 on one optional stream degrades that stream only; the connection stays active and decisions go on."""

from __future__ import annotations

from datetime import timedelta

from autonomy.connectors import ConnectionStatus, Lane, Stream
from autonomy.connectors.health import ALERT_STREAM_DENIED
from autonomy.connectors.sync import DENIED_RETRY
from tests.v2.zero_touch.wiring_support import BUSINESS, T0, make_shop


def test_a_403_on_an_optional_stream_degrades_it_and_nothing_else() -> None:
    shop = make_shop(hold=True)
    shop.shopify.denied_resources.add("products")  # the products scope was not granted
    connection = shop.connect_shopify_oauth()
    shop.fresh(connection, T0)
    for minutes in (0, 60):
        report = shop.engine.sync(
            connection.connection_id, Stream.PRODUCTS, Lane.LIVE, shop.at(T0 + timedelta(minutes=minutes))
        )
        assert report.status == "retry" and report.retry_at == shop.clock.now() + DENIED_RETRY
    stored = shop.engine.store.get_connection(connection.connection_id)
    assert stored is not None and stored.status is ConnectionStatus.ACTIVE  # not "needs reconnect"
    denied = [a for a in shop.alerts.list_alerts() if a.code == ALERT_STREAM_DENIED]
    assert len(denied) == 1 and denied[0].severity == "info" and "products" in denied[0].message
    health = shop.engine.monitor.connection_health(stored, shop.clock.now())
    assert health.state == "degraded" and health.can_act
    assert shop.engine.monitor.ready_to_act(BUSINESS, shop.clock.now()).can_act
    # The other streams keep syncing.
    report = shop.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, shop.clock.now())
    assert report.status == "ok", report


def test_a_403_on_a_critical_stream_still_asks_for_a_reconnect() -> None:
    shop = make_shop(hold=True)
    shop.shopify.denied_resources.add("customers")  # consent rides on customers: never act without it
    connection = shop.connect_shopify_oauth()
    report = shop.engine.sync(connection.connection_id, Stream.CUSTOMERS, Lane.LIVE, shop.clock.now())
    assert report.status == "reconnect"
    stored = shop.engine.store.get_connection(connection.connection_id)
    assert stored is not None and stored.status is ConnectionStatus.NEEDS_RECONNECT


def test_a_webhooks_problem_is_one_alert_not_one_a_day() -> None:
    from autonomy.connectors.health import ALERT_WEBHOOKS, Problem

    shop = make_shop(hold=True)
    connection = shop.connect_shopify_oauth()
    shop.fresh(connection, T0)
    monitor = shop.engine.monitor
    problem = Problem(code=ALERT_WEBHOOKS, severity="warning", message="webhooks not active")
    for day in range(3):  # the daily renewal fails three days running; health is checked between
        at = shop.at(T0 + timedelta(days=day))
        shop.fresh(connection, at)
        monitor.desk.raise_problem(connection, problem, at)
        monitor.evaluate(connection, at + timedelta(minutes=5))
    raised = [a for a in shop.alerts.list_alerts() if a.code == ALERT_WEBHOOKS]
    assert len(raised) == 1
