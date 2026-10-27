"""`POST /v2/import/orders`: the same report as the command line, over the demo services."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from api.v2.deps import Services, build_demo_services

FIXTURES = Path(__file__).parent / "fixtures"
SHOPIFY = (FIXTURES / "shopify_orders.csv").read_bytes()
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CSV = {"content-type": "text/csv"}


@pytest.fixture
def services() -> Services:
    return build_demo_services(NOW, n_customers=5, days=1)


@pytest.fixture
def client(services: Services) -> Iterator[Any]:
    pytest.importorskip("fastapi", reason="FastAPI is not installed here; the v2 CI job runs these")
    pytest.importorskip("httpx", reason="the FastAPI test client needs httpx")
    from fastapi.testclient import TestClient

    from api.v2.app import create_app

    with TestClient(create_app(services)) as c:
        yield c


def test_upload_returns_the_report_and_stores_the_events(client: Any, services: Services) -> None:
    r = client.post("/v2/import/orders", params={"shop": "in"}, content=SHOPIFY, headers=CSV)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["orders"] == 4
    assert body["customers"] == 3
    assert body["refunds"] == 2
    assert body["contactable"] == 1
    assert body["skipped"] == {"missing_customer": 1}
    assert body["dry_run"] is False
    assert body["events_new"] == body["events_built"]
    state = services.states.get("ravi@example.com")
    assert state is not None
    assert state.revenue == 500.0


def test_upload_twice_adds_nothing(client: Any) -> None:
    client.post("/v2/import/orders", params={"shop": "in"}, content=SHOPIFY, headers=CSV)
    again = client.post("/v2/import/orders", params={"shop": "in"}, content=SHOPIFY, headers=CSV).json()
    assert again["events_new"] == 0
    assert again["events_already_present"] == again["events_built"]


def test_dry_run_writes_nothing(client: Any, services: Services) -> None:
    r = client.post(
        "/v2/import/orders", params={"shop": "in", "dry_run": "true"}, content=SHOPIFY, headers=CSV
    )
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert r.json()["events_new"] == r.json()["events_built"]
    assert services.states.get("ravi@example.com") is None


def test_a_file_that_is_not_an_order_list_is_a_400(client: Any) -> None:
    r = client.post("/v2/import/orders", content=b"a,b\n1,2\n", headers=CSV)
    assert r.status_code == 400
    assert "not an order list" in r.json()["detail"]


def test_an_unknown_time_zone_is_a_400(client: Any) -> None:
    r = client.post("/v2/import/orders", params={"timezone": "Mars/Olympus"}, content=SHOPIFY, headers=CSV)
    assert r.status_code == 400
