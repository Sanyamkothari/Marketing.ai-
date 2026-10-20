"""Live checks against real sandboxes. Skipped unless credentials are in the environment.

The base has no platform of its own, so these drive whichever built-in connectors this build has
(Shopify, Klaviyo) through the base engine: connect with a private key / custom-app token, sync one
page of each stream, and check health. Variables:

* Shopify development store: `AUTONOMY_LIVE_SHOPIFY_SHOP` (acme.myshopify.com) and
  `AUTONOMY_LIVE_SHOPIFY_TOKEN` (a custom-app Admin API access token, read scopes only).
* Klaviyo test account: `AUTONOMY_LIVE_KLAVIYO_API_KEY` (a private key, read scopes only).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from autonomy.connectors import (
    ConnectorEngine,
    ConnectorStore,
    Lane,
    MemoryTokenVault,
    SystemClock,
    create_connector_tables,
    default_registry,
)
from autonomy.store import SqlEventStore, create_all, memory_engine


def engine_for(kind: str) -> tuple[ConnectorEngine, SqlEventStore]:
    registry = default_registry()
    if kind not in registry:
        pytest.skip(f"the {kind} connector is not part of this build")
    db = memory_engine()
    create_all(db)
    create_connector_tables(db)
    events = SqlEventStore(db)
    engine = ConnectorEngine(
        registry=registry,
        store=ConnectorStore(db),
        vault=MemoryTokenVault(),
        sink=events.append,
        clock=SystemClock(),
    )
    return engine, events


def smoke(engine: ConnectorEngine, kind: str, key: str, config: dict[str, str]) -> None:
    now = datetime.now(UTC)
    connection = engine.connect_api_key(kind, business_id="live", api_key=key, now=now, config=config)
    for spec in engine.connector(connection).capabilities.streams:
        report = engine.sync(connection.connection_id, spec.stream, Lane.LIVE, now, max_pages=1)
        assert report.status in ("ok", "more", "waiting"), (spec.stream, report.error_code, report.error)


@pytest.mark.live_connector
def test_live_shopify_sync(live_env) -> None:
    env = live_env("AUTONOMY_LIVE_SHOPIFY_SHOP", "AUTONOMY_LIVE_SHOPIFY_TOKEN")
    engine, _ = engine_for("shopify")
    smoke(engine, "shopify", env["AUTONOMY_LIVE_SHOPIFY_TOKEN"], {"shop": env["AUTONOMY_LIVE_SHOPIFY_SHOP"]})


@pytest.mark.live_connector
def test_live_klaviyo_sync(live_env) -> None:
    env = live_env("AUTONOMY_LIVE_KLAVIYO_API_KEY")
    engine, _ = engine_for("klaviyo")
    smoke(engine, "klaviyo", env["AUTONOMY_LIVE_KLAVIYO_API_KEY"], {})
