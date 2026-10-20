"""Webhook intake: constant-time HMAC, fast ack, dedupe, out-of-order, uninstall, privacy, catch-up."""

from __future__ import annotations

from datetime import timedelta

from autonomy.connectors import (
    ConnectionStatus,
    IntakeStatus,
    Lane,
    Stream,
    WebhookIntake,
    hmac_matches,
    query_hmac_matches,
)
from autonomy.connectors.sync import SYNC_KIND, WEBHOOK_KIND, slot_after
from autonomy.connectors.webhooks import sign
from autonomy.contracts import EventType
from autonomy.runtime.worker import Worker

from .base_support import T0, connect, iso, make_rig, signed


def order(oid: str, *, updated_minutes: int, total: float = 20.0) -> dict[str, object]:
    return {
        "id": oid,
        "customer_id": "c7",
        "total": total,
        "created_at": iso(T0 - timedelta(hours=3)),
        "updated_at": iso(T0 + timedelta(minutes=updated_minutes)),
        "a_field_added_next_quarter": [1, 2],
    }


def test_hmac_is_checked_over_the_raw_body() -> None:
    body = b'{"id": 1}'
    good = sign("s3", body)
    assert hmac_matches("s3", body, good)
    assert not hmac_matches("s3", body + b" ", good)
    assert not hmac_matches("other", body, good)
    assert (
        not hmac_matches("", body, good)
        and not hmac_matches("s3", body, None)
        and not hmac_matches("s3", body, "")
    )
    hexed = sign("s3", body, encoding="hex")
    assert hmac_matches("s3", body, "sha256=" + hexed.upper(), encoding="hex", prefix="sha256=")
    assert not hmac_matches("s3", body, hexed, encoding="hex", prefix="sha256=")


def test_a_signed_query_string_like_shopifys_callback() -> None:
    params = {
        "code": "0907a61c0c8d55e99db179b68161bc00",
        "shop": "acme.myshopify.com",
        "state": "abc",
        "timestamp": "1337178173",
    }
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items())).encode()
    params["hmac"] = sign("hush", message, encoding="hex")
    assert query_hmac_matches("hush", params)
    assert not query_hmac_matches("hush", params | {"shop": "evil.myshopify.com"})
    assert not query_hmac_matches("hush", {k: v for k, v in params.items() if k != "hmac"})


def test_receive_verifies_queues_and_asks_for_one_catch_up_poll() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    headers, body = signed(order("w1", updated_minutes=1), topic="orders/updated", delivery="d-1")
    result = intake.receive("demo", headers, body, T0)
    assert result.status is IntakeStatus.ACCEPTED and result.http_status == 200
    assert result.connection_id == connection.connection_id
    job = rig.queue.get(result.job_id or "")
    assert job is not None and job.kind == WEBHOOK_KIND and job.payload["body"] == body.decode()
    catch_up = rig.queue.claim([SYNC_KIND], worker="t", now=slot_after(T0, timedelta(hours=1)))
    assert catch_up is not None and catch_up.payload == {
        "connection_id": connection.connection_id,
        "stream": "orders",
        "lane": "live",
    }
    assert rig.events.count() == 0  # nothing parsed on the fast path


def test_bad_signatures_and_unknown_accounts() -> None:
    rig = make_rig()
    connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    headers, body = signed(
        order("w1", updated_minutes=1), topic="orders/updated", delivery="d-1", secret="wrong"
    )
    assert intake.receive("demo", headers, body, T0).http_status == 401
    headers, body = signed(order("w1", updated_minutes=1), topic="orders/updated", delivery="d-1")
    assert intake.receive("demo", headers, body[:-1] + b" ", T0).http_status == 401
    assert intake.receive("nope", headers, body, T0).http_status == 404
    headers, body = signed(
        order("w1", updated_minutes=1), topic="orders/updated", delivery="d-2", account="acct_unknown"
    )
    unknown = intake.receive("demo", headers, body, T0)
    assert unknown.status is IntakeStatus.UNKNOWN and unknown.http_status == 200


def test_a_duplicate_delivery_is_acked_but_not_queued_again() -> None:
    rig = make_rig()
    connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    headers, body = signed(order("w1", updated_minutes=1), topic="orders/updated", delivery="d-1")
    first = intake.receive("demo", headers, body, T0)
    again = intake.receive("demo", headers, body, T0 + timedelta(minutes=5))
    assert first.status is IntakeStatus.ACCEPTED
    assert again.status is IntakeStatus.DUPLICATE and again.http_status == 200 and again.job_id is None


def test_processing_tolerates_out_of_order_and_replayed_deliveries() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    connector = rig.engine.connector(connection)
    newer = signed(order("w9", updated_minutes=10, total=30.0), topic="orders/updated", delivery="d-new")
    older = signed(order("w9", updated_minutes=2, total=25.0), topic="orders/updated", delivery="d-old")

    def parsed(delivery):
        return connector.parse_webhook({k.lower(): v for k, v in delivery[0].items()}, delivery[1])

    assert intake.apply(connection, parsed(newer), T0) == 1
    assert intake.apply(connection, parsed(older), T0) == 0  # arrived late: the newer version stands
    versions = rig.store.record_versions(connection.connection_id, Stream.ORDERS, ["w9"])
    assert versions["w9"] == T0 + timedelta(minutes=10)
    purchases = [e for e in rig.events.scan() if e.type is EventType.PURCHASE]
    assert len(purchases) == 1 and purchases[0].value == 30.0
    assert intake.apply(connection, parsed(newer), T0 + timedelta(hours=1)) == 0  # a replay adds nothing


def test_queued_deliveries_are_processed_by_the_worker() -> None:
    rig = make_rig()
    connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    for i in range(3):
        intake.receive(
            "demo", *signed(order(f"q{i}", updated_minutes=i), topic="orders/updated", delivery=f"d-{i}"), T0
        )
    outcomes = Worker(queue=rig.queue, handlers=rig.engine.handlers(rig.queue)).drain(T0)
    assert [o.status for o in outcomes if o.kind == WEBHOOK_KIND] == ["done"] * 3
    assert len([e for e in rig.events.scan() if e.type is EventType.PURCHASE]) == 3


def test_uninstall_disconnects_and_alerts_once() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    for delivery in ("u-1", "u-2"):
        intake.receive("demo", *signed({"id": "acct_9"}, topic="app/uninstalled", delivery=delivery), T0)
    Worker(queue=rig.queue, handlers=rig.engine.handlers(rig.queue)).drain(T0)
    stored = rig.store.get_connection(connection.connection_id)
    assert stored is not None and stored.status is ConnectionStatus.DISCONNECTED
    assert rig.engine.tokens.vault.load(connection.connection_id) is None
    assert [a.code for a in rig.alerts.list_alerts()] == ["connector.uninstalled"]
    assert not rig.engine.monitor.ready_to_act("biz_1", T0).can_act


def test_privacy_requests_are_recorded_with_one_alert_each() -> None:
    rig = make_rig()
    connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    payload = {"shop_id": 1, "customer": {"id": 7, "email": "x@example.com"}, "orders_to_redact": [1]}
    intake.receive("demo", *signed(payload, topic="customers/redact", delivery="p-1"), T0)
    intake.receive("demo", *signed(payload, topic="customers/redact", delivery="p-1"), T0)  # retried
    Worker(queue=rig.queue, handlers=rig.engine.handlers(rig.queue)).drain(T0)
    requests = rig.store.privacy_requests()
    assert len(requests) == 1 and requests[0].kind.value == "redact_customer"
    assert requests[0].subject["customer"] == {"id": 7, "email": "x@example.com"}
    alerts = [a for a in rig.alerts.list_alerts() if a.code == "connector.privacy_request"]
    assert len(alerts) == 1 and "x@example.com" not in alerts[0].message


def test_webhooks_for_a_paused_connection_are_acked_but_not_ingested() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    rig.store.set_status(connection.connection_id, ConnectionStatus.PAUSED, "paused by a person", T0)
    intake = WebhookIntake(rig.engine, rig.queue)
    result = intake.receive(
        "demo", *signed(order("w1", updated_minutes=1), topic="orders/updated", delivery="d-1"), T0
    )
    assert result.http_status == 200
    Worker(queue=rig.queue, handlers=rig.engine.handlers(rig.queue)).drain(T0)
    assert rig.events.count() == 0
    assert rig.store.position(connection.connection_id, Stream.ORDERS, Lane.LIVE).last_attempt_at is None


def test_a_privacy_request_after_uninstall_is_still_recorded() -> None:
    """Shopify sends shop/redact about 48 h after app/uninstalled, to the app-level route."""
    rig = make_rig()
    connection = connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    intake.receive("demo", *signed({"id": "acct_9"}, topic="app/uninstalled", delivery="u-1"), T0)
    Worker(queue=rig.queue, handlers=rig.engine.handlers(rig.queue)).drain(T0)
    later = T0 + timedelta(hours=48)
    payload = {"shop_id": 1, "customer": {"id": 7}, "orders_to_redact": []}
    result = intake.receive("demo", *signed(payload, topic="customers/redact", delivery="p-9"), later)
    assert result.status is IntakeStatus.ACCEPTED and result.connection_id == connection.connection_id
    Worker(queue=rig.queue, handlers=rig.engine.handlers(rig.queue)).drain(later)
    assert [r.kind.value for r in rig.store.privacy_requests()] == ["redact_customer"]
    # an ordinary topic for the uninstalled account is still not routed to it
    orders = intake.receive(
        "demo", *signed(order("w1", updated_minutes=1), topic="orders/updated", delivery="d-9"), later
    )
    assert orders.status is IntakeStatus.UNKNOWN


def test_credential_headers_are_not_stored_with_the_delivery() -> None:
    rig = make_rig()
    connect(rig, seed=False)
    intake = WebhookIntake(rig.engine, rig.queue)
    headers, body = signed(order("w1", updated_minutes=1), topic="orders/updated", delivery="d-1")
    result = intake.receive(
        "demo", headers | {"Cookie": "session=abc", "Authorization": "Bearer x"}, body, T0
    )
    job = rig.queue.get(result.job_id or "")
    assert job is not None and "session=abc" not in job.payload["headers"]
    assert "Bearer x" not in job.payload["headers"] and "x-demo-topic" in job.payload["headers"]


def test_query_hmac_escapes_like_shopifys_libraries() -> None:
    params = {"shop": "acme.myshopify.com", "state": "a&b=c", "note": "100%", "timestamp": "1"}
    message = "&".join(
        sorted(["note=100%25", "shop=acme.myshopify.com", "state=a%26b=c", "timestamp=1"])
    ).encode()
    signed_params = params | {"hmac": sign("hush", message, encoding="hex")}
    assert query_hmac_matches("hush", signed_params)
    # the unescaped join would let "state=a&b=c" pose as two parameters; it must not verify
    naive = "&".join(f"{k}={v}" for k, v in sorted(params.items())).encode()
    assert not query_hmac_matches("hush", params | {"hmac": sign("hush", naive, encoding="hex")})
