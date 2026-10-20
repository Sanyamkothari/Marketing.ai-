"""Live checks against a real Shopify development store. Skipped unless its credentials are set.

* `AUTONOMY_LIVE_SHOPIFY_SHOP` - the store's permanent domain (`acme-dev.myshopify.com`);
* `AUTONOMY_LIVE_SHOPIFY_TOKEN` - an Admin API access token of a custom app on that store with the
  connector's scopes (`read_customers, write_customers, read_orders, read_products, write_discounts`);
* `AUTONOMY_LIVE_SHOPIFY_WRITE=1` - also create one discount code (it expires after a day) and add
  and remove a tag on the first customer. Leave unset for a read-only run.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.connectors import (
    ConnectorEngine,
    ConnectorRegistry,
    ConnectorStore,
    Lane,
    MemoryTokenVault,
    Stream,
    SystemClock,
    create_connector_tables,
    idempotency_key,
)
from autonomy.connectors.shopify import CREATE_DISCOUNT, REMOVE_TAGS, TAG_CUSTOMER, ShopifyConnector
from autonomy.store import SqlEventStore, create_all, memory_engine

NAMES = ("AUTONOMY_LIVE_SHOPIFY_SHOP", "AUTONOMY_LIVE_SHOPIFY_TOKEN")


def live_engine() -> tuple[ConnectorEngine, SqlEventStore]:
    db = memory_engine()
    create_all(db)
    create_connector_tables(db)
    events = SqlEventStore(db)
    engine = ConnectorEngine(
        registry=ConnectorRegistry([ShopifyConnector()]),
        store=ConnectorStore(db),
        vault=MemoryTokenVault(),
        sink=events.append,
        clock=SystemClock(),
    )
    return engine, events


@pytest.mark.live_connector
def test_live_shopify_reads_every_stream_and_exports_products(
    live_env: Callable[..., dict[str, str]],
) -> None:
    env = live_env(*NAMES)
    engine, _events = live_engine()
    now = datetime.now(UTC)
    connection = engine.connect_api_key(
        "shopify",
        business_id="live",
        api_key=env["AUTONOMY_LIVE_SHOPIFY_TOKEN"],
        now=now,
        config={"shop": env["AUTONOMY_LIVE_SHOPIFY_SHOP"]},
    )
    assert connection.external_id == env["AUTONOMY_LIVE_SHOPIFY_SHOP"]
    for stream in (Stream.CUSTOMERS, Stream.ORDERS, Stream.PRODUCTS):
        report = engine.sync(connection.connection_id, stream, Lane.LIVE, now, max_pages=1)
        assert report.status in ("ok", "more"), (stream, report.error_code, report.error)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:  # the products backfill is one bulk operation
        report = engine.sync(connection.connection_id, Stream.PRODUCTS, Lane.HISTORY, datetime.now(UTC))
        assert report.status in ("waiting", "ok", "more", "done"), (report.error_code, report.error)
        if report.status == "done":
            break
        if report.retry_at is not None:
            time.sleep(max(0.0, min(30.0, (report.retry_at - datetime.now(UTC)).total_seconds())))
    else:
        pytest.fail("the products export did not finish within 3 minutes")


@pytest.mark.live_connector
def test_live_shopify_writes_one_code_per_decision(live_env: Callable[..., dict[str, str]]) -> None:
    env = live_env(*NAMES)
    if os.environ.get("AUTONOMY_LIVE_SHOPIFY_WRITE") != "1":
        pytest.skip("live connector test: set AUTONOMY_LIVE_SHOPIFY_WRITE=1 to let it create a discount code")
    engine, _ = live_engine()
    now = datetime.now(UTC)
    connection = engine.connect_api_key(
        "shopify",
        business_id="live",
        api_key=env["AUTONOMY_LIVE_SHOPIFY_TOKEN"],
        now=now,
        config={"shop": env["AUTONOMY_LIVE_SHOPIFY_SHOP"]},
    )
    ctx = engine.context(connection, now)
    connector = ShopifyConnector()
    key = idempotency_key("live-test", now.date().isoformat(), CREATE_DISCOUNT)
    offer = {"percentage": 0.05, "ends_at": (now + timedelta(days=1)).isoformat()}
    first = connector.write(ctx, CREATE_DISCOUNT, offer, idempotency_key=key)
    again = connector.write(ctx, CREATE_DISCOUNT, offer, idempotency_key=key)
    assert first.accepted and again.accepted and first.provider_id == again.provider_id, (first, again)
    tag = {
        "customer_id": os.environ.get("AUTONOMY_LIVE_SHOPIFY_CUSTOMER_ID", ""),
        "tags": ["autonomy-live-test"],
    }
    if tag["customer_id"]:
        assert connector.write(ctx, TAG_CUSTOMER, tag, idempotency_key=key).accepted
        assert connector.write(ctx, REMOVE_TAGS, tag, idempotency_key=key).accepted
