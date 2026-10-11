"""What your file already says, through the API (DEC-1596).

`POST /doctor/diagnosis` on a customer history with the outcome and a recharge log without one: the ready
use case carries the history's first look (its rate of "yes" and the rate by its most informative columns),
and the log carries its recency picture. Every figure is recomputed here from the files with pandas.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.doctor.first_look import FIRST_LOOK_LABEL
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def history() -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=3000, variant="clean"))
    frame["email"] = [f"person{index}@example.com" for index in range(len(frame))]
    return frame


def recharges() -> pd.DataFrame:
    rng = np.random.default_rng(5)
    days = rng.integers(0, 400, size=3000)
    return pd.DataFrame(
        {
            "customer_id": [f"C-{10000 + index}" for index in rng.integers(0, 400, size=3000)],
            "recharge_date": (pd.Timestamp("2025-01-01") + pd.to_timedelta(days, unit="D")).strftime(
                "%Y-%m-%d"
            ),
            "amount": rng.integers(10, 500, size=3000),
        }
    )


def upload(client: TestClient, name: str, frame: pd.DataFrame) -> str:
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    response = client.post(
        "/uploads",
        files={"file": (name, buffer.getvalue().encode(), "text/csv")},
        data={"use_case": "payment-propensity", "mode": "train"},
    )
    assert response.status_code == 201, response.text
    upload_id: str = response.json()["upload_id"]
    return upload_id


def test_the_diagnosis_says_what_each_file_already_says(client: TestClient) -> None:
    frame, log = history(), recharges()
    history_id = upload(client, "history.csv", frame)
    log_id = upload(client, "recharges.csv", log)
    response = client.post("/doctor/diagnosis", json={"upload_ids": [history_id, log_id]})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()

    verdict = next(item for item in body["use_cases"] if item["use_case_id"] == USE_CASE)
    assert verdict["status"] in {"ready", "check"} and verdict["upload_id"] == history_id
    look = verdict["first_look"]
    assert look["kind"] == "outcome" and look["label"] == FIRST_LOOK_LABEL
    assert look["upload_id"] == history_id and look["outcome_column"] == "converted_30d"
    outcome = pd.to_numeric(frame["converted_30d"])
    assert look["base_rate"] == pytest.approx(outcome.mean(), abs=1e-6)
    assert 0 < len(look["columns"]) <= 5
    used = {column["column"] for column in look["columns"]}
    assert {"email", "customer_id", "converted_30d"}.isdisjoint(used)
    left = {item["column"]: item["reason"] for item in look["left_out"]}
    assert left["email"] == "personal" and left["customer_id"] == "id"
    checked = 0
    for column in look["columns"]:
        if column["kind"] != "values":
            continue
        rates = outcome.groupby(frame[column["column"]].astype(str)).mean()
        for group in column["groups"]:
            if not group["group"].startswith("("):
                assert group["rate"] == pytest.approx(rates[group["group"]], abs=1e-6)
                checked += 1
    assert checked >= 2
    for column in look["columns"]:
        assert all(group["rows"] >= 10 for group in column["groups"])

    files = {file["upload_id"]: file for file in body["files"]}
    assert files[history_id]["first_look"] is None  # its picture is the use case's
    recency = files[log_id]["first_look"]
    assert recency["kind"] == "recency" and recency["label"] == FIRST_LOOK_LABEL
    assert recency["id_column"] == "customer_id" and recency["date_column"] == "recharge_date"
    assert recency["customers"] == log["customer_id"].nunique()
    times = pd.to_datetime(log["recharge_date"])
    last = times.groupby(log["customer_id"]).max()
    recent = int(((times.max() - last).dt.days <= 30).sum())
    shown = {group["group"]: group["rows"] for group in recency["recency"]["groups"]}
    assert shown["In the last 30 days"] == recent


def test_a_use_case_no_file_powers_has_no_first_look(client: TestClient) -> None:
    log_id = upload(client, "recharges.csv", recharges())
    body = client.post("/doctor/diagnosis", json={"upload_ids": [log_id]}).json()
    assert all(item["first_look"] is None for item in body["use_cases"])
