"""A raw order log builds a dataset through the onboarding builder (v3 phase 0, B4).

A UCI-shaped log (one row per line of an order, a float `CustomerID` with blanks, `C` cancellations, negative stock
adjustments) goes in through `POST /uploads`. There is no customer file. `plan_build(order_log=...)` derives the list
from the shopper column and rolls the lines up into orders, order lines and returns; once the plan is approved,
`drive_builder` makes the Manual setup calls over the builder's own routes and the dataset builds and passes its
checks. The list is a real entity source, so the builder's `NO_ENTITY_SOURCE` check passes as it is.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes.doctor import _load
from engine.config import load_use_case
from engine.doctor.build_driver import OrderLogInput, approval_for, drive_builder, plan_build, replay_build
from engine.doctor.builder_api import ApiBuilderPort
from engine.doctor.contracts import Diagnosis
from engine.doctor.order_roll_up import find_log_columns, roll_up
from engine.doctor.outcomes import snapshot_rows
from engine.storage import LocalStorage
from tests.fixtures.order_log import order_log_config_root, order_log_frame

pytestmark = [pytest.mark.integration, pytest.mark.slow]

USE_CASE = "retail-win-back"
BUILD_TIMEOUT_S = 240.0
CUSTOMERS = 1_500


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def app(config_root: Path, data_dir: Path, tmp_path: Path) -> FastAPI:
    # The path is on: the builder port registers `order_lines` and `returns`, which the source routes know only then.
    return create_app(config_root=order_log_config_root(config_root, tmp_path), data_dir=data_dir)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _poll(client: TestClient, dataset_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + BUILD_TIMEOUT_S
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/datasets/{dataset_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"]["state"] not in {"pending", "running"}:
            return body
        time.sleep(0.05)
    return body


def test_an_order_log_builds_a_dataset_with_no_customer_file(
    client: TestClient, data_dir: Path, config_root: Path
) -> None:
    log = order_log_frame(customers=CUSTOMERS)
    uploaded = client.post(
        "/uploads",
        files={"file": ("online-retail.csv", log.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    upload_id = str(uploaded.json()["upload_id"])
    diagnosed = client.post("/doctor/diagnosis", json={"upload_ids": [upload_id]})
    assert diagnosed.status_code == 200, diagnosed.text
    diagnosis = Diagnosis.model_validate(diagnosed.json())
    frame = _load(LocalStorage(data_dir), upload_id, 1_000_000).frame
    assert len(frame) == len(log), "the whole log was read"

    config = load_use_case(USE_CASE, config_root)
    columns = find_log_columns(frame, customer="CustomerID", order="InvoiceNo", date="InvoiceDate")
    plan = plan_build(
        diagnosis,
        {upload_id: frame},
        use_case=USE_CASE,
        label_spec=config.order_log_label,
        order_log=OrderLogInput(upload_id, columns),
        suggested=config.order_log_features,
    )
    assert plan.blocking == () and plan.order_log is not None and plan.order_log.skipped_features == ()

    client_id = client.post(
        "/clients", json={"name": "Gifts Co", "industry": "ecommerce", "notes": ""}
    ).json()["client_id"]
    port = ApiBuilderPort(client, client_id, storage=LocalStorage(data_dir))
    result = drive_builder(
        plan, {upload_id: frame}, port, approval_for(plan, user_id="analyst-7", at=datetime.now(UTC))
    )
    assert result.dataset_id is not None and len(result.source_ids) == 4

    final = _poll(client, result.dataset_id)
    assert final["status"]["state"] == "done", final["status"]
    manifest = final["manifest"]
    report = client.get(f"/datasets/{manifest['dataset_id']}/report").json()
    errors = [c for c in report["checks"] if c["severity"] == "error"]
    assert report["passed"], errors
    assert "NO_ENTITY_SOURCE" not in {c["code"] for c in report["checks"]}
    assert {source["role"] for source in report["sources"]} == {"entity", "orders", "order_lines", "returns"}

    rolled = roll_up(frame, columns)
    dataset = pd.read_parquet(data_dir / "datasets" / manifest["dataset_id"] / "dataset.parquet")
    key = manifest["primary_key"][0]
    # Six month-ends, 31 Mar to 31 Aug 2011, as on the real file: at least 90 days of history, and the last month-end
    # whose 90 days are over (30 Sep is censored).
    assert manifest["snapshot_dates"] == [
        "2011-03-31",
        "2011-04-30",
        "2011-05-31",
        "2011-06-30",
        "2011-07-31",
        "2011-08-31",
    ]
    # At each month-end, exactly the shoppers whose first order is on or before it, written as the log writes them.
    signup = rolled.customers.set_index("CustomerID")["signup_date"]
    dates = [datetime.strptime(day, "%Y-%m-%d").date() for day in manifest["snapshot_dates"]]
    expected = snapshot_rows(rolled.customers["CustomerID"], dates, signup)
    built = pd.DataFrame(
        {"entity_key": dataset[key].astype(str), "snapshot_date": pd.to_datetime(dataset["snapshot_date"])}
    )
    as_pairs = {(row.entity_key, row.snapshot_date.date()) for row in expected.itertuples(index=False)}
    assert {(row.entity_key, row.snapshot_date.date()) for row in built.itertuples(index=False)} == as_pairs
    assert manifest["n_rows"] == len(dataset) == len(expected) > 0
    assert set(dataset["reactivated_90d"]) == {0, 1}
    assert manifest["target"] == "reactivated_90d"
    # The built features are the ones the plan kept, from the roll-up (order counts come from `orders`).
    assert {"orders_total", "spend_total", "returned_orders", "distinct_products"} <= set(dataset.columns)

    # Next month, the whole log again, replays the frozen rules without a new question.
    kept = port.load_record(result.spec_id)
    assert kept.plan == plan
    replayed = replay_build(
        kept, {"online-retail.csv": [order_log_frame(customers=CUSTOMERS + 100)]}, port, mode="score"
    )
    again = _poll(client, str(replayed.dataset_id))
    assert again["status"]["state"] == "done", again["status"]
    later = order_log_frame(customers=CUSTOMERS + 100)
    relisted = roll_up(later, columns).customers
    scored = pd.read_parquet(data_dir / "datasets" / again["manifest"]["dataset_id"] / "dataset.parquet")
    assert again["manifest"]["n_rows"] == len(scored) > 0
    assert {str(value) for value in scored[key]} <= set(relisted["CustomerID"])
    assert len(set(relisted["CustomerID"])) > len(
        set(rolled.customers["CustomerID"])
    ), "the new shoppers are listed"
