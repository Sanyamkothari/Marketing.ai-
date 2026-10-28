"""An order log with blank customer IDs, driven to approval: the questions and stops say only what is true.

About 2,000 shoppers, a quarter of the lines with no customer ID. The person is asked which column names the customer
(not shown a row number or a date), is shown how many rows each column leaves blank, and is told which one the build
uses; once the build they approved supplies the customer the question is gone, the outcome question points at the
column that build makes, and the approval has no stop for the two dates of a training table or for the shoppers the
chosen audience leaves out.
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
from tests.integration.doctor_wait import settled
from tests.integration.test_api_case_buildable_goal import GOAL, order_log

BLANK_SHARE = 0.25


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def blank_log() -> pd.DataFrame:
    frame = order_log()
    rng = np.random.default_rng(11)
    frame.loc[rng.random(len(frame)) < BLANK_SHARE, "customer_id"] = None
    return frame


def open_question(case: dict[str, Any], code: str) -> dict[str, Any] | None:
    return next((q for q in case["questions"] if q["code"] == code and q["answer"] is None), None)


def answer(
    client: TestClient, case_id: str, case: dict[str, Any], code: str, option_id: str
) -> dict[str, Any]:
    asked = open_question(case, code)
    assert asked is not None, code
    sent = client.post(
        f"/cases/{case_id}/answers",
        json={"version": case["version"], "question_id": asked["question_id"], "option_id": option_id},
    )
    assert sent.status_code == 200, sent.text
    return settled(client, case_id)


def test_an_order_log_with_blank_ids_asks_which_column_names_the_customer_and_goes_to_approval(
    client: TestClient,
) -> None:
    buffer = io.StringIO()
    blank_log().to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("orders.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    case = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()
    case_id = case["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()
    counted = next(u for u in case["diagnosis"]["use_cases"] if u["use_case_id"] == GOAL)["buildable"]
    assert counted["entity_key"] == "customer_id"

    # 1. the customer question: the columns that repeat per customer, each with its blank share, the build's
    # column recommended with the reason, and no row number (a row number names a row, not a customer)
    key = open_question(case, "KEY_NOT_UNIQUE")
    assert key is not None and key["blocking"]
    by_label = {o["label"]: o["option_id"] for o in key["options"]}
    customer = next(label for label in by_label if label.startswith("customer_id"))
    assert "25% blank" in customer and "cannot be tied to a person" in customer, customer
    assert not any("row number" in label for label in by_label)
    assert not any(
        label.startswith("order_date") for label in by_label
    ), "a column of dates names no customer"
    assert key["recommended"] == by_label[customer]
    assert "customer_id" in key["why"]

    # 2. the goal: audience, build and approval
    client.post(f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": GOAL, "op_id": "g1"})
    case = settled(client, case_id)
    case = answer(client, case_id, case, "BUILD_AUDIENCE", "active")
    (proposal,) = [i for i in case["items"] if i["kind"] == "recipe_step" and i.get("goal_id") == GOAL]
    sentence = proposal["payload"]["sentence"]
    left_out = counted["ids"] - counted["active_before"]
    assert (
        f"The {left_out:,} of the file's {counted['ids']:,}" in sentence and "audience you chose" in sentence
    )
    assert f"would have {counted['active_before']:,} rows" in sentence, sentence
    assert open_question(case, "OUTCOME_COLUMN") is None, "not asked before the build is approved"
    approved = client.post(
        f"/cases/{case_id}/decisions",
        json={"version": case["version"], "item_id": proposal["item_id"], "accept": True},
    )
    assert approved.status_code == 200, approved.text
    case = settled(client, case_id)

    # 3. the approved build supplies the customer: that question is gone; the outcome points at the built column
    assert open_question(case, "KEY_NOT_UNIQUE") is None
    outcome = open_question(case, "OUTCOME_COLUMN")
    assert outcome is not None and outcome["recommended"] is not None
    chosen = next(o for o in outcome["options"] if o["option_id"] == outcome["recommended"])
    assert chosen["label"] == "reactivated_90d" and "approved" in outcome["why"]
    case = answer(client, case_id, case, "OUTCOME_COLUMN", outcome["recommended"])

    # 4. nothing left to stop at: the build's own yes, and no stop for the two dates or for the audience's shoppers
    approval = client.get(f"/cases/{case_id}/screen").json()["view"]["approval"]
    stops = [y["sentence"] for y in approval["yeses"] if y["kind"] == "acknowledge"]
    assert not [s for s in stops if "reads the data as of" in s or "leaves out" in s], stops
    assert "confirm" in [y["kind"] for y in approval["yeses"]]
    shown = next(y["sentence"] for y in approval["yeses"] if y["item_id"] == proposal["item_id"])
    assert "the audience you chose" in shown and "sampled" not in shown
    # ... and the approval goes through to ready, the customers the audience leaves out being agreed
    case = settled(client, case_id)
    done = client.post(
        f"/cases/{case_id}/approval",
        json={
            "version": case["version"],
            "digest": approval["digest"],
            "yeses": [
                {k: y[k] for k in ("kind", "key", "item_id", "sentence") if y.get(k) is not None}
                for y in approval["yeses"]
            ],
        },
    )
    assert done.status_code == 200, done.text
    (readiness,) = done.json()["readiness"]
    assert readiness["goal_id"] == GOAL and readiness["verdict"] == "ready"


def test_a_customer_column_chosen_for_a_log_is_not_expected_to_be_unique(client: TestClient) -> None:
    buffer = io.StringIO()
    blank_log().to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("orders.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    case = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()
    case_id = case["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()
    key = open_question(case, "KEY_NOT_UNIQUE")
    assert key is not None and key["recommended"] is not None
    case = answer(client, case_id, case, "KEY_NOT_UNIQUE", key["recommended"])
    assert open_question(case, "KEY_NOT_UNIQUE") is None
    said = [i["title"] for i in case["items"] if i["kind"] == "data_check"]
    assert not [t for t in said if "should name each customer once" in t], said
