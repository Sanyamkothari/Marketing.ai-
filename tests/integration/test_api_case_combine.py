"""Joining files through the API: a customer list and its orders become one stored upload after the person's own yes,
and the case goes on with that upload as its one file; the two files it was made from are untouched."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.doctor import case as doctor_case

GOAL = "targeted-advertisement"
CUSTOMERS = 250


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setattr(doctor_case, "data_doctor_unified", lambda root=None: True)
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def _upload(client: TestClient, name: str, frame: pd.DataFrame) -> str:
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    response = client.post(
        "/uploads",
        files={"file": (name, buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def test_an_approved_join_becomes_the_cases_one_stored_table(client: TestClient, tmp_path: Path) -> None:
    customers = pd.DataFrame(
        {
            "customer_id": [f"C-{n:05d}" for n in range(1, CUSTOMERS + 1)],
            "region": [("North", "South", "East")[n % 3] for n in range(CUSTOMERS)],
            "age": [20 + n % 50 for n in range(CUSTOMERS)],
        }
    )
    orders = pd.DataFrame(
        {
            "order_id": [f"O{n}" for n in range(1000)],
            "customer_id": [1 + n % 200 for n in range(1000)],
            "order_date": [f"2026-0{1 + n % 6}-{1 + n % 27:02d}" for n in range(1000)],
            "amount": [100 + n % 9 for n in range(1000)],
        }
    )
    ids = [_upload(client, "customers.csv", customers), _upload(client, "orders.csv", orders)]
    case: dict[str, Any] = client.post("/cases", json={"upload_ids": ids}).json()
    case = client.post(f"/cases/{case['case_id']}/visit", json={}).json()
    item = next(i for i in case["items"] if i["kind"] == "build_dataset")
    assert "50 customers have no orders; they stay, with 0 orders" in item["title"]

    ticked = client.post(  # nobody is lost: ticked like any change, no yes of its own
        f"/cases/{case['case_id']}/decisions",
        json={"version": case["version"], "item_id": item["item_id"], "accept": True},
    )
    assert ticked.status_code == 200, ticked.text
    case = ticked.json()
    approved = client.post(f"/cases/{case['case_id']}/approval", json={"version": case["version"]})
    assert approved.status_code == 200, approved.text
    case = approved.json()

    (only,) = case["files"]
    assert only["upload_id"] not in ids and only["original_upload_id"] == ids[0]
    profile = client.get(f"/uploads/{only['upload_id']}/profile").json()
    assert profile["row_count"] == CUSTOMERS, "one row per customer"
    assert {"customer_id", "orders_90d", "days_since_last_orders"} <= {c["name"] for c in profile["columns"]}
    for original in ids:  # the files it was made from are as they were
        assert client.get(f"/uploads/{original}/profile").status_code == 200
    assert (tmp_path / "data" / "uploads" / only["upload_id"]).is_dir()
