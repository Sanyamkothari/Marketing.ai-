"""Shopify webhooks: HMAC, dedupe, REST payloads, out-of-order copies, uninstall, compliance, subscriptions."""

from __future__ import annotations

from datetime import timedelta

from autonomy.connectors import ConnectionStatus, IntakeStatus, Lane, PrivacyKind, Stream, WebhookIntake
from autonomy.connectors.sync import WEBHOOK_KIND
from autonomy.contracts import Channel, EventType

from .base_support import Rig
from .shopify_support import (
    SHOP,
    T0,
    FakeShopify,
    connect_oauth,
    connect_token,
    gid,
    make_shopify_rig,
    order_node,
    queued,
    rest_customer,
    rest_order,
    webhook,
)

CALLBACK_BASE = "https://autonomy.example/v2/connectors/webhooks/shopify/"


def process(rig: Rig, intake: WebhookIntake) -> int:
    """Run every queued webhook job, like the worker does; return how many ran."""
    ran = 0
    while (job := rig.queue.claim([WEBHOOK_KIND], worker="w", now=rig.clock.current)) is not None:
        intake.handle_job(job, rig.clock.current)
        rig.queue.complete(job.job_id)
        ran += 1
    return ran


def test_deliveries_are_verified_deduped_and_acked_before_any_parsing() -> None:
    rig, fake = make_shopify_rig()
    connection = connect_oauth(rig, fake, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    payload = rest_order(1001, customer=1, total=40.0, created=T0, updated=T0 + timedelta(minutes=5))
    headers, body = webhook("orders/updated", payload, event_id="ev-1")

    accepted = intake.receive("shopify", headers, body, T0)  # app-level route: found by shop domain
    assert (accepted.status, accepted.http_status, accepted.connection_id) == (
        IntakeStatus.ACCEPTED,
        200,
        connection.connection_id,
    )
    assert intake.receive("shopify", headers, body, T0).status is IntakeStatus.DUPLICATE
    retried = {**headers, "X-Shopify-Webhook-Id": "wh-retry-2"}  # Shopify's retry of the same event
    assert intake.receive("shopify", retried, body, T0).status is IntakeStatus.DUPLICATE

    tampered = intake.receive("shopify", headers, body.replace(b"40.00", b"4.00"), T0)
    assert (tampered.status, tampered.http_status) == (IntakeStatus.REJECTED, 401)
    other_secret = webhook("orders/updated", payload, event_id="ev-2", secret="not_ours")
    assert intake.receive("shopify", *other_secret, T0).http_status == 401
    unknown_shop = webhook("orders/updated", payload, event_id="ev-3", shop="stranger.myshopify.com")
    assert (intake.receive("shopify", *unknown_shop, T0).status, 200) == (IntakeStatus.UNKNOWN, 200)

    catch_up = [j for j in queued(rig, "connector_sync") if j.payload["stream"] == "orders"]
    assert len(catch_up) == 1 and catch_up[0].run_at == T0 + timedelta(hours=1)  # the hourly safety net
    assert process(rig, intake) == 1
    (purchase,) = [e for e in rig.events.scan() if e.type is EventType.PURCHASE]
    assert (purchase.customer_id, purchase.value, purchase.item_id, purchase.attrs["offer_id"]) == (
        "1",
        40.0,
        "501",
        "WELCOME10",
    )


def test_an_older_copy_after_a_newer_one_changes_nothing_and_the_poll_completes_what_rest_lacks() -> None:
    rig, fake = make_shopify_rig()
    connection = connect_oauth(rig, fake, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    created, refunded_at = T0 - timedelta(hours=1), T0 + timedelta(minutes=10)
    # paid in EUR in a USD shop: REST refund transactions are in EUR, so the webhook copy has no amount
    newer = rest_order(
        1001,
        customer=1,
        total=40.0,
        created=created,
        updated=refunded_at,
        refunds=[(9001, refunded_at, 8.0)],
        presentment="EUR",
    )
    older = rest_order(1001, customer=1, total=40.0, created=created, updated=created, presentment="EUR")
    intake.receive("shopify", *webhook("orders/updated", newer, event_id="ev-new"), T0)
    intake.receive("shopify", *webhook("orders/create", older, event_id="ev-old"), T0)
    assert process(rig, intake) == 2
    assert [e.type for e in rig.events.scan()] == [EventType.PURCHASE]  # the old copy added nothing
    stored = rig.store.record_versions(connection.connection_id, Stream.ORDERS, ["1001"])["1001"]
    assert stored == refunded_at - timedelta(milliseconds=1)  # a webhook copy ranks just below the poll's

    # the catch-up poll reads the same version in full (GraphQL shopMoney) and adds the refund
    fake.orders["1001"] = order_node(
        1001, customer=1, total=40.0, created=created, updated=refunded_at, refunds=[(9001, refunded_at, 7.5)]
    )
    rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, T0 + timedelta(hours=1))
    (refund,) = [e for e in rig.events.scan() if e.type is EventType.REFUND]
    assert (refund.value, refund.ts) == (7.5, refunded_at)
    assert len([e for e in rig.events.scan() if e.type is EventType.PURCHASE]) == 1


def test_a_customer_webhook_updates_consent() -> None:
    rig, fake = make_shopify_rig()
    connect_oauth(rig, fake, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    body = rest_customer(7, created=T0 - timedelta(days=9), updated=T0, email_state="unsubscribed")
    assert (
        intake.receive("shopify", *webhook("customers/update", body, event_id="ev-c"), T0).http_status == 200
    )
    process(rig, intake)
    types = {(e.type, e.channel) for e in rig.events.for_customer("7")}
    assert types == {(EventType.SIGNUP, None), (EventType.CONSENT_REVOKED, Channel.EMAIL)}


def test_uninstall_stops_everything_with_one_alert_and_the_later_shop_redact_is_still_recorded() -> None:
    rig, fake = make_shopify_rig()
    connection = connect_oauth(rig, fake, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    intake.receive("shopify", *webhook("app/uninstalled", {"id": 1, "domain": SHOP}, event_id="ev-u"), T0)
    intake.receive("shopify", *webhook("app/uninstalled", {"id": 1, "domain": SHOP}, event_id="ev-u2"), T0)
    process(rig, intake)
    current = rig.store.get_connection(connection.connection_id)
    assert current is not None and current.status is ConnectionStatus.DISCONNECTED
    assert rig.engine.tokens.vault.load(connection.connection_id) is None
    assert len([a for a in rig.alerts.list_alerts() if a.code == "connector.uninstalled"]) == 1

    later = T0 + timedelta(hours=48)
    redact = {"shop_id": 1, "shop_domain": SHOP}
    result = intake.receive(
        "shopify", *webhook("shop/redact", redact, event_id="ev-r", triggered=later), later
    )
    assert result.status is IntakeStatus.ACCEPTED
    rig.clock.current = later
    process(rig, intake)
    (request,) = rig.store.privacy_requests()
    assert request.kind is PrivacyKind.REDACT_SHOP and request.subject["shop_domain"] == SHOP


def test_compliance_requests_keep_only_ids_and_hashes_of_who_they_are_about() -> None:
    rig, fake = make_shopify_rig()
    connect_oauth(rig, fake, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    data_request = {
        "shop_id": 1,
        "shop_domain": SHOP,
        "orders_requested": [1001, 1002],
        "customer": {"id": 7, "email": "Jo@Example.com", "phone": "555-625-1199"},
        "data_request": {"id": 9999},
    }
    redact = {
        "shop_id": 1,
        "shop_domain": SHOP,
        "customer": {"id": 7, "email": "jo@example.com"},
        "orders_to_redact": [1001],
    }
    intake.receive("shopify", *webhook("customers/data_request", data_request, event_id="ev-d"), T0)
    intake.receive("shopify", *webhook("customers/redact", redact, event_id="ev-x"), T0)
    process(rig, intake)
    requests = {r.kind: r for r in rig.store.privacy_requests()}
    asked = requests[PrivacyKind.DATA_REQUEST].subject
    assert (
        asked["customer_id"] == "7"
        and asked["order_ids"] == ["1001", "1002"]
        and asked["data_request_id"] == "9999"
    )
    assert "email" not in asked and len(asked["email_sha256"]) == 64
    assert (
        asked["email_sha256"] == requests[PrivacyKind.REDACT_CUSTOMER].subject["email_sha256"]
    )  # case-folded
    assert len([a for a in rig.alerts.list_alerts() if a.code == "connector.privacy_request"]) == 2


def test_subscriptions_are_created_once_renewed_daily_and_moved_when_the_address_changes() -> None:
    rig, fake = make_shopify_rig()
    connection = connect_oauth(rig, fake, seed=False)
    callback = CALLBACK_BASE + connection.connection_id
    rig.engine.daily(connection.connection_id, T0, rig.queue)
    topics = sorted(s["topic"] for s in fake.subscriptions.values())
    assert topics == [
        "APP_UNINSTALLED",
        "CUSTOMERS_CREATE",
        "CUSTOMERS_UPDATE",
        "ORDERS_CREATE",
        "ORDERS_UPDATED",
        "PRODUCTS_UPDATE",
    ]  # the three compliance topics live in the app's configuration, not per shop
    assert {s["uri"] for s in fake.subscriptions.values()} == {callback}

    rig.engine.daily(connection.connection_id, T0 + timedelta(days=1), rig.queue)
    assert len(fake.subscriptions) == 6  # idempotent

    first = next(iter(fake.subscriptions))
    fake.subscriptions[first]["uri"] = (
        "https://old-host.example/v2/connectors/webhooks/shopify/" + connection.connection_id
    )
    del fake.subscriptions[list(fake.subscriptions)[-1]]  # Shopify removed one after failed deliveries
    rig.engine.daily(connection.connection_id, T0 + timedelta(days=2), rig.queue)
    assert len(fake.subscriptions) == 6 and {s["uri"] for s in fake.subscriptions.values()} == {callback}
    assert not [a for a in rig.alerts.list_alerts() if a.code == "connector.webhooks"]


def test_a_custom_app_connection_is_polled_only_and_raises_no_webhook_warning() -> None:
    fake = FakeShopify(app_api_key="their_own_custom_app")  # secret-scan: allow (fake test value)
    rig, _ = make_shopify_rig(fake)
    connection = connect_token(rig)
    rig.engine.daily(connection.connection_id, T0, rig.queue)
    assert fake.subscriptions == {}
    assert not [a for a in rig.alerts.list_alerts() if a.code == "connector.webhooks"]
    assert gid("Customer", 1) == "gid://shopify/Customer/1"
