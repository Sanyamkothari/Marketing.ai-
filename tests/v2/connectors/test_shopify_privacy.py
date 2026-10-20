"""Shopify's mandatory privacy webhooks carried out on our copy: redact a customer, export one, redact a shop."""

from __future__ import annotations

from datetime import timedelta

from autonomy.connectors import Lane, PrivacyKind, Stream, WebhookIntake
from autonomy.connectors.shopify import PRIVACY_JOB_KIND, ShopifyPrivacyDesk, privacy_handlers, privacy_job
from autonomy.contracts import CustomerState, Event, EventType
from autonomy.store import SqlStateStore

from .base_support import Rig
from .shopify_support import SHOP, T0, FakeShopify, connect_oauth, make_shopify_rig, queued, webhook
from .test_shopify_webhooks import process

NOW = T0 + timedelta(hours=1)


def synced_shop() -> tuple[Rig, FakeShopify, str, WebhookIntake, ShopifyPrivacyDesk]:
    fake = FakeShopify()
    for cid in (1, 2):
        fake.add_customer(cid, created=T0 - timedelta(days=20), updated=T0 - timedelta(minutes=30))
        fake.add_order(
            1000 + cid,
            customer=cid,
            total=20.0,
            created=T0 - timedelta(minutes=40),
            lines=[(501, "Skincare", 1, 20.0)],
        )
    rig, _ = make_shopify_rig(fake)
    connection = connect_oauth(rig, fake, seed=False)
    for stream in (Stream.CUSTOMERS, Stream.ORDERS):
        assert rig.engine.sync(connection.connection_id, stream, Lane.LIVE, NOW).status == "ok"
    states = SqlStateStore(rig.store.engine)
    states.put([CustomerState(customer_id="1", as_of=NOW), CustomerState(customer_id="2", as_of=NOW)])
    rig.events.append(
        [
            Event(
                event_id="evt_from_klaviyo",
                customer_id="1",
                type=EventType.OPEN,
                ts=NOW,
                source="klaviyo",
                channel="email",
            )
        ]
    )
    rig.clock.current = NOW
    desk = ShopifyPrivacyDesk(rig.store, rig.store.engine, desk=rig.engine.monitor.desk)
    return rig, fake, connection.connection_id, WebhookIntake(rig.engine, rig.queue), desk


def test_customers_redact_deletes_everything_we_hold_about_that_customer_only() -> None:
    rig, _, connection_id, intake, desk = synced_shop()
    body = {
        "shop_id": 1,
        "shop_domain": SHOP,
        "customer": {"id": 1, "email": "jo@example.com"},
        "orders_to_redact": [1001],
    }
    intake.receive("shopify", *webhook("customers/redact", body, event_id="ev-x", triggered=NOW), NOW)
    process(rig, intake)
    assert len([a for a in rig.alerts.list_alerts() if a.code == "connector.privacy_request"]) == 1

    (outcome,) = desk.run_pending(NOW)
    assert outcome.kind is PrivacyKind.REDACT_CUSTOMER and outcome.status == "done"
    assert (
        outcome.removed["events"] == 4 and outcome.removed["states"] == 1 and outcome.removed["records"] == 2
    )
    assert rig.events.for_customer("1") == []  # every source: the Klaviyo open went too
    assert {e.type for e in rig.events.for_customer("2")} == {
        EventType.SIGNUP,
        EventType.CONSENT_GRANTED,
        EventType.PURCHASE,
    }
    states = SqlStateStore(rig.store.engine)
    assert states.get("1") is None and states.get("2") is not None
    assert rig.store.record_versions(connection_id, Stream.CUSTOMERS, ["1", "2"]).keys() == {"2"}
    assert rig.store.privacy_requests() == [] and rig.store.privacy_requests(status="done")
    assert not rig.store.open_incidents(connection_id)  # done by itself: nobody needs to act
    assert desk.run_pending(NOW) == []


def test_a_data_request_is_exported_and_waits_for_a_person_to_send_it() -> None:
    rig, _, connection_id, intake, desk = synced_shop()
    body = {
        "shop_id": 1,
        "shop_domain": SHOP,
        "customer": {"id": 2},
        "orders_requested": [1002],
        "data_request": {"id": 5},
    }
    intake.receive("shopify", *webhook("customers/data_request", body, event_id="ev-d", triggered=NOW), NOW)
    process(rig, intake)
    (outcome,) = desk.run_pending(NOW)
    assert outcome.status == "exported"
    (request,) = rig.store.privacy_requests(status="exported")
    result = desk.result(request.request_id)
    assert result is not None
    export = result["export"]
    assert (
        export["customer_id"] == "2" and len(export["events"]) == 3 and export["state"]["customer_id"] == "2"
    )
    assert rig.events.for_customer("2")  # nothing deleted
    assert rig.store.open_incidents(connection_id)  # the export must still reach the merchant


def test_shop_redact_after_uninstall_removes_the_shops_data_and_keeps_other_sources() -> None:
    rig, _, connection_id, intake, desk = synced_shop()
    intake.receive("shopify", *webhook("app/uninstalled", {"id": 1}, event_id="ev-u", triggered=NOW), NOW)
    process(rig, intake)
    later = NOW + timedelta(hours=48)
    rig.clock.current = later
    intake.receive(
        "shopify", *webhook("shop/redact", {"shop_id": 1, "shop_domain": SHOP}, event_id="ev-s"), later
    )
    process(rig, intake)
    (outcome,) = desk.run_pending(later)
    assert outcome.kind is PrivacyKind.REDACT_SHOP and outcome.status == "done"
    remaining = list(rig.events.scan())
    assert [e.source for e in remaining] == ["klaviyo"]  # not Shopify's data
    assert rig.store.record_versions(connection_id, Stream.ORDERS, ["1001", "1002"]) == {}
    assert rig.store.position(connection_id, Stream.ORDERS, Lane.LIVE).high_watermark is None


def test_the_privacy_job_runs_every_hour() -> None:
    rig, _, _, _, desk = synced_shop()
    handlers = privacy_handlers(desk, rig.queue)
    handlers[PRIVACY_JOB_KIND](privacy_job(NOW), NOW)
    (job,) = queued(rig, PRIVACY_JOB_KIND)
    assert job.run_at == T0 + timedelta(hours=2)
    handlers[PRIVACY_JOB_KIND](privacy_job(NOW), NOW)
    assert len(queued(rig, PRIVACY_JOB_KIND)) == 1
