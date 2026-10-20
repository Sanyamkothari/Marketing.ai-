"""The connector state tables on SQLite and Postgres: leases, dedupe, record versions, incidents.

The Postgres variant needs `MARKETING_AI_TEST_DATABASE_URL` (CI sets it) and skips with that reason
otherwise, like `tests/v2/backbone/test_sql_stores.py`, whose fixture this mirrors.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from autonomy.connectors import (
    Connection,
    ConnectionStatus,
    ConnectorStore,
    Lane,
    PendingAuthorization,
    PrivacyKind,
    RecordKey,
    Stream,
    SyncPosition,
    create_connector_tables,
)
from autonomy.connectors.store import Incident, PrivacyRequest

DB_ENV = "MARKETING_AI_TEST_DATABASE_URL"
REQUIRE_ENV = "MARKETING_AI_REQUIRE_POSTGRES"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ConnectorStore]:
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'connectors.db'}")
        create_connector_tables(engine)
        try:
            yield ConnectorStore(engine)
        finally:
            engine.dispose()
        return
    url = os.environ.get(DB_ENV, "").strip()
    if not url:
        reason = f"{DB_ENV} is not set, so the Postgres variant cannot run (CI sets it)"
        if os.environ.get(REQUIRE_ENV, "").strip() in {"1", "true", "yes", "on"}:
            pytest.fail(f"{REQUIRE_ENV} is set: {reason}")
        pytest.skip(reason)
    base = create_engine(url)
    schema = f"v2conn_{uuid.uuid4().hex[:12]}"
    with base.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine: Engine = base.execution_options(schema_translate_map={None: schema})
    try:
        create_connector_tables(engine)
        yield ConnectorStore(engine)
    finally:
        with base.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base.dispose()


def connection(cid: str = "conn_1", external: str = "acme.myshopify.com", **changes: object) -> Connection:
    base = Connection(
        connection_id=cid,
        kind="shopify",
        business_id="b1",
        label=external,
        external_id=external,
        created_at=T0,
        updated_at=T0,
    )
    return base.model_copy(update=changes)


def test_connections_round_trip_and_are_found_by_account(store: ConnectorStore) -> None:
    store.put_connection(connection())
    store.put_connection(connection("conn_2", "other.myshopify.com"))
    assert store.get_connection("conn_1") == connection()
    assert store.find_connection("shopify", "acme.myshopify.com") == connection()
    assert [c.connection_id for c in store.connections(business_id="b1")] == ["conn_1", "conn_2"]
    store.set_status("conn_1", ConnectionStatus.DISCONNECTED, "uninstalled", T0)
    assert store.find_connection("shopify", "acme.myshopify.com") is None
    assert store.get_connection("conn_1").status is ConnectionStatus.DISCONNECTED  # type: ignore[union-attr]


def test_a_lease_has_one_holder_until_it_expires(store: ConnectorStore) -> None:
    ttl = timedelta(seconds=60)
    assert store.acquire_lease("token:c1", "w1", T0, ttl)
    assert not store.acquire_lease("token:c1", "w2", T0 + timedelta(seconds=30), ttl)
    assert store.acquire_lease("token:c1", "w1", T0 + timedelta(seconds=30), ttl)  # renewal
    assert store.acquire_lease("token:c1", "w2", T0 + timedelta(seconds=91), ttl)  # expired: taken over
    store.release_lease("token:c1", "w1")  # not the holder: no effect
    assert not store.acquire_lease("token:c1", "w1", T0 + timedelta(seconds=92), ttl)
    store.release_lease("token:c1", "w2")
    assert store.acquire_lease("token:c1", "w1", T0 + timedelta(seconds=93), ttl)


def test_positions_round_trip(store: ConnectorStore) -> None:
    empty = store.position("c1", Stream.ORDERS, Lane.LIVE)
    assert empty.cursor is None and not empty.in_window
    saved = SyncPosition(
        connection_id="c1",
        stream=Stream.ORDERS,
        lane=Lane.HISTORY,
        window_start=T0 - timedelta(days=7),
        window_end=T0,
        cursor="eyJsYXN0IjoxfQ",
    )
    store.put_position(saved, T0)
    assert store.position("c1", Stream.ORDERS, Lane.HISTORY) == saved
    assert store.positions("c1") == [saved]


def test_deliveries_dedupe_and_are_pruned(store: ConnectorStore) -> None:
    assert store.remember_delivery("c1", "d-1", T0)
    assert not store.remember_delivery("c1", "d-1", T0 + timedelta(minutes=1))
    assert store.delivery_seen("c1", "d-1") and not store.delivery_seen("c2", "d-1")
    assert store.prune_deliveries(T0 + timedelta(days=8)) == 1
    assert not store.delivery_seen("c1", "d-1")


def test_record_versions_keep_the_newest(store: ConnectorStore) -> None:
    t1, t2 = T0, T0 + timedelta(minutes=5)
    assert store.claim_newer("c1", Stream.ORDERS, [RecordKey("o1", t2), RecordKey("o2", t1)]) == {"o1", "o2"}
    assert store.claim_newer("c1", Stream.ORDERS, [RecordKey("o1", t1)]) == set()  # older: skipped
    assert store.claim_newer("c1", Stream.ORDERS, [RecordKey("o1", t2)]) == set()  # same: skipped
    assert store.claim_newer("c1", Stream.ORDERS, [RecordKey("o2", t2), RecordKey("o3", None)]) == {
        "o2",
        "o3",
    }
    assert store.record_versions("c1", Stream.ORDERS, ["o1", "o2", "o9"]) == {"o1": t2, "o2": t2}


def test_oauth_state_is_taken_once(store: ConnectorStore) -> None:
    pending = PendingAuthorization(
        state="st_1",
        kind="klaviyo",
        business_id="b1",
        redirect_uri="r",
        code_verifier="v" * 50,
        created_at=T0,
        expires_at=T0 + timedelta(minutes=10),
    )
    store.put_pending(pending)
    assert store.take_pending("st_1", T0) == pending
    assert store.take_pending("st_1", T0) is None


def test_an_incident_opens_once_and_reopens_after_it_closed(store: ConnectorStore) -> None:
    incident = Incident(
        connection_id="c1",
        code="connector.stale",
        open=True,
        severity="critical",
        message="m",
        opened_at=T0,
        alert_id="a1",
        last_seen_at=T0,
    )
    assert store.open_incident_once(incident)
    assert not store.open_incident_once(incident.model_copy(update={"alert_id": "a2"}))
    store.put_incident(incident.model_copy(update={"open": False, "resolved_at": T0}))
    assert store.open_incidents("c1") == []
    assert store.open_incident_once(incident.model_copy(update={"alert_id": "a3"}))
    assert [i.alert_id for i in store.open_incidents()] == ["a3"]


def test_secrets_and_privacy_requests(store: ConnectorStore) -> None:
    store.write_secret("c1", "sealed-1", "fp", T0)
    store.write_secret("c1", "sealed-2", "fp", T0)
    assert store.read_secret("c1") == ("sealed-2", "fp", 2)
    store.delete_secret("c1")
    assert store.read_secret("c1") is None
    request = PrivacyRequest(
        request_id="p1", connection_id="c1", kind=PrivacyKind.REDACT_SHOP, received_at=T0
    )
    assert store.put_privacy_request(request) and not store.put_privacy_request(request)
    assert store.privacy_requests() == [request]
