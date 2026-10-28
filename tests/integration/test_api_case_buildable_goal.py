"""A raw order log fits a goal whose outcome it can define: offered as buildable, built on the person's yes, ready.

The deadlock this closes: a log of orders has no `reactivated_90d` column, so the goal fit (which looked only for the
outcome's name) said nothing fits, and choosing the goal asked for a column Data Doctor can build itself. Here the
log is generated in the test (about 2,000 shoppers over 14 months: ID, date, quantity, unit price) and goes raw file ->
the goal is offered as `buildable` with counts -> the goal step proposes the build -> the person approves it -> the
outcome is the built table's column and the fit counts that table.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.integration.doctor_wait import settled

GOAL = "retail-win-back"
SHOPPERS = 2_000
LAST_DAY = "2024-02-29"


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def order_log(seed: int = 5) -> pd.DataFrame:
    """About 2,000 shoppers, 14 months (2023-01-01 to 2024-02-29), one row per order line."""
    rng = np.random.default_rng(seed)
    first = pd.Timestamp("2023-01-01")
    span = (pd.Timestamp(LAST_DAY) - first).days
    rows: list[dict[str, Any]] = []
    for shopper in range(SHOPPERS):
        day = int(rng.integers(0, span - 30))
        habit = rng.uniform(
            0.004, 0.03
        )  # chance of an order on any later day: some shoppers lapse, some stay
        while day <= span:
            for _line in range(int(rng.integers(1, 4))):
                rows.append(
                    {
                        "invoice_no": f"{shopper:05d}-{day}",
                        "customer_id": f"C{shopper:05d}",
                        "order_date": (first + pd.Timedelta(days=day)).strftime("%Y-%m-%d"),
                        "quantity": int(rng.integers(1, 6)),
                        "unit_price": round(float(rng.uniform(2, 40)), 2),
                        "product": f"P{int(rng.integers(0, 300)):03d}",
                    }
                )
            day += 1 + int(rng.geometric(habit))
    return pd.DataFrame(rows)


def _checks(node: Any) -> list[dict[str, Any]]:
    """Every check of the stored readiness report (each has an `id` and a `status`), wherever the report keeps them."""
    if isinstance(node, dict):
        own = [node] if "id" in node and "status" in node else []
        return own + [c for value in node.values() for c in _checks(value)]
    if isinstance(node, list):
        return [c for value in node for c in _checks(value)]
    return []


def _audience(
    client: TestClient, case_id: str, case: dict[str, Any], counted: dict[str, Any]
) -> dict[str, Any]:
    """The goal names a narrower audience (shoppers who have gone quiet): the build waits for the person to say which
    shoppers it is about, from both readings' counts. Nothing is built before; the person picks every shopper active
    before the snapshot here (the quiet ones are too few in this log, and the option says so)."""
    assert not [i for i in case["items"] if i["kind"] == "recipe_step" and i.get("goal_id") == GOAL]
    (asked,) = [q for q in case["questions"] if q["code"] == "BUILD_AUDIENCE" and q["answer"] is None]
    assert asked["blocking"] and asked["recommended"] is None, "the rules never pick the audience"
    labels = {o["option_id"]: o["label"] for o in asked["options"]}
    active, quiet = counted["audiences"]
    assert f"{active['customers']:,}" in labels["active"] and f"{quiet['customers']:,}" in labels["quiet"]
    assert "gone quiet" in labels["quiet"] and ("too few" in labels["quiet"]) is not quiet["enough"]
    answered = client.post(
        f"/cases/{case_id}/answers",
        json={"version": case["version"], "question_id": asked["question_id"], "option_id": "active"},
    )
    assert answered.status_code == 200, answered.text
    return settled(client, case_id)


def _goal_cards(client: TestClient, case_id: str) -> list[dict[str, Any]]:
    view = client.get(f"/cases/{case_id}/screen").json()["view"]
    return [c for card in view["conversation"] if card.get("type") == "goals" for c in card["cards"]]


def test_a_raw_order_log_fits_the_goal_as_buildable_then_builds(client: TestClient, tmp_path: Path) -> None:
    order_rows = order_log()
    buffer = io.StringIO()
    order_rows.to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("orders.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    case = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()
    case_id = case["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()

    # 1. no file has the outcome, yet the goal is not missing: it fits once the outcome is built, with counts
    verdict = next(u for u in case["diagnosis"]["use_cases"] if u["use_case_id"] == GOAL)
    assert verdict["status"] == "buildable" and verdict["reason_code"] == "BUILDABLE", verdict
    counted = verdict["buildable"]
    assert counted["snapshot"] == "2023-11-30" and counted["window_days"] == 90
    assert counted["active_before"] >= 1_000 and counted["yes"] >= 200
    assert verdict["customers"] == counted["active_before"] and verdict["yes_count"] == counted["yes"]
    assert "one row per" in verdict["why"]
    assert GOAL not in case["visit"]["missing_for"]
    assert [c["goal_id"] for c in _goal_cards(client, case_id)].count(GOAL) == 1, "drawn as a goal card"

    # 2. choosing it: the build is offered as a proposal, not a request for a column
    chosen = client.post(
        f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": GOAL, "op_id": "g1"}
    )
    assert chosen.status_code == 200, chosen.text
    case = _audience(client, case_id, settled(client, case_id), counted)
    assert not [r for r in case.get("requests", []) if r.get("kind") == "more_data"]
    proposals = [i for i in case["items"] if i["kind"] == "recipe_step" and i.get("goal_id") == GOAL]
    assert len(proposals) == 1 and proposals[0]["state"] == "pending"
    params = proposals[0]["payload"]["step"]["params"]
    assert params["reads_future"] and params["outcome_window_days"] == 90 and params["as_of"] == "2023-11-30"
    assert "reactivated_90d" in proposals[0]["payload"]["columns"]
    assert proposals[0]["payload"]["audience"]["audience_id"] == "active"

    # 3. the person approves it: the built table is what the fit counts
    approved = client.post(
        f"/cases/{case_id}/decisions",
        json={"version": case["version"], "item_id": proposals[0]["item_id"], "accept": True},
    )
    assert approved.status_code == 200, approved.text
    case = settled(client, case_id)
    question = next(
        (q for q in case["questions"] if q["code"] == "OUTCOME_COLUMN" and q["answer"] is None), None
    )
    if question is not None:  # the person says which column is the outcome (the built one)
        answered = client.post(
            f"/cases/{case_id}/answers",
            json={"version": case["version"], "question_id": question["question_id"], "option_id": "c0"},
        )
        assert answered.status_code == 200, answered.text
        case = settled(client, case_id)
    outcome = [i for i in case["items"] if i["kind"] == "outcome_column" and i["state"] == "accepted"]
    assert outcome and outcome[0]["payload"]["column"] == "reactivated_90d"

    while (
        case["state"] == "ask"
    ):  # what is left is asked of the raw log (its repeating customer), as for any log
        open_ = next(q for q in case["questions"] if q["blocking"] and q["answer"] is None)
        answered = client.post(
            f"/cases/{case_id}/answers",
            json={"version": case["version"], "question_id": open_["question_id"], "option_id": "c0"},
        )
        assert answered.status_code == 200, answered.text
        case = answered.json()
    case = settled(client, case_id)
    approval = client.get(f"/cases/{case_id}/screen").json()["view"]["approval"]
    kinds = [y["kind"] for y in approval["yeses"]]
    assert "confirm" in kinds, "the build waits for the person's own yes"
    # shoppers whose first order is after the snapshot are not in the table: the person sees that stop and says yes to it
    approved = client.post(
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
    assert approved.status_code == 200, approved.text
    (readiness,) = approved.json()["readiness"]
    assert readiness["goal_id"] == GOAL and readiness["verdict"] == "ready"
    (stored,) = (tmp_path / "data" / "cases" / "default" / case_id / "readiness").glob(f"{GOAL}_*.json")
    report = json.loads(stored.read_text())
    # ready on the table the goal trains on: the built one-row-per-shopper table, not the 19,000-line log
    assert report["source"]["rows_parsed"] == counted["active_before"]
    assert report["source"]["original_rows_parsed"] == len(order_rows)
    assert report["roles"]["target"] == "reactivated_90d" and report["roles"]["primary_key"] == [
        "customer_id"
    ]
    floors = {
        c["id"]: c["status"]
        for c in _checks(report)
        if c["id"] in {"ROWS_TOO_FEW", "TARGET_TOO_FEW_POSITIVES"}
    }
    assert floors == {"ROWS_TOO_FEW": "pass", "TARGET_TOO_FEW_POSITIVES": "pass"}, floors


def test_a_declined_build_asks_for_the_column_instead(client: TestClient) -> None:
    buffer = io.StringIO()
    order_log().to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("orders.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    case_id = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()
    counted = next(u for u in case["diagnosis"]["use_cases"] if u["use_case_id"] == GOAL)["buildable"]
    client.post(f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": GOAL, "op_id": "g1"})
    case = _audience(client, case_id, settled(client, case_id), counted)
    (proposal,) = [i for i in case["items"] if i["kind"] == "recipe_step" and i.get("goal_id") == GOAL]
    declined = client.post(
        f"/cases/{case_id}/decisions",
        json={"version": case["version"], "item_id": proposal["item_id"], "accept": False},
    )
    assert declined.status_code == 200, declined.text
    case = settled(client, case_id)
    assert [i["state"] for i in case["items"] if i["kind"] == "recipe_step"] == ["rejected"]
    assert any(q["code"] == "DATA_REQUESTED" and q["answer"] is None for q in case["questions"]), "the column"
    assert any(d["doubt_id"].startswith("d.outcome_missing") for d in case.get("doubts", []))
    view = client.get(f"/cases/{case_id}/screen").json()["view"]
    (fit,) = [c for c in view["conversation"] if c.get("type") == "fit"]
    assert fit["ok"] is False
