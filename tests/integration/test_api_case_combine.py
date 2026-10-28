"""Joining files through the API: a customer list and its orders become one stored upload after the person's own yes,
and the case goes on with that upload as its one file; the two files it was made from are untouched."""

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

GOAL = "targeted-advertisement"
CUSTOMERS = 250


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
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
    assert "50 customers have no rows in orders.csv; they stay, with 0." in item["title"]

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


def _walk_customers(n: int = 3000) -> pd.DataFrame:
    """Customers as an Indian retailer exports them: haan/nahi for consent, rupees as text, dates day first."""
    rng = np.random.default_rng(11)
    day = pd.Timestamp("2025-09-01") + pd.to_timedelta(rng.integers(0, 300, n), unit="D")
    visits = rng.integers(0, 16, n)
    converted = (rng.random(n) < 0.05 + 0.02 * visits).astype(int)
    contacted = pd.Timestamp("2025-06-01") + pd.to_timedelta(rng.integers(0, 330, n), unit="D")
    return pd.DataFrame(
        {
            "customer_id": [f"C-{10000 + k}" for k in range(n)],
            "snapshot_date": day.strftime("%Y-%m-%d"),
            "visits_last_7d": visits,
            "plan_tier": np.array(["basic", "standard", "premium"])[rng.integers(0, 3, n)],
            "region": np.array(["north", "south", "east", "west"])[rng.integers(0, 4, n)],
            "marketing_opt_in": np.where(rng.random(n) < 0.8, "haan", "nahi"),
            "last_contacted_at": contacted.strftime("%d/%m/%Y"),
            "converted_30d": converted,
            "lifetime_spend": [f"₹{int(v):,}" for v in rng.gamma(2.0, 1500, n)],
        }
    )


def _walk_orders(customers: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(12)
    rows: list[tuple[str, str, str, int]] = []
    for cid, k in zip(customers["customer_id"], rng.integers(0, 4, len(customers)), strict=True):
        for _ in range(int(k)):
            when = pd.Timestamp("2025-09-01") + pd.Timedelta(days=int(rng.integers(0, 300)))
            rows.append((cid, f"O-{len(rows) + 1:06d}", when.strftime("%Y-%m-%d"), int(rng.gamma(2, 600))))
    return pd.DataFrame(rows, columns=["customer_id", "order_id", "order_date", "order_value"])


def test_a_join_then_approve_ends_ready_with_the_decisions_carried_and_nothing_drawn_twice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The main customers + orders path: accept 'Combine', approve; the case goes on with the combined table, its
    outcome and consent decisions follow it, the readiness check reads that table with its changes applied, and the
    conversation does not draw the files' earlier fixes beside the table's own."""
    customers = _walk_customers()
    ids = [
        _upload(client, "customers.csv", customers),
        _upload(client, "orders.csv", _walk_orders(customers)),
    ]
    case: dict[str, Any] = client.post("/cases", json={"upload_ids": ids}).json()
    cid = case["case_id"]

    def post(path: str, **body: Any) -> dict[str, Any]:
        response = client.post(f"/cases/{cid}/{path}", json=body)
        assert response.status_code == 200, (path, response.text)
        result: dict[str, Any] = response.json()
        return result

    case = post("visit")
    case = post("goal", goal_id=GOAL, version=case["version"])
    for _ in range(8):  # every question: its safe choice, else the first
        open_ones = [q for q in case["questions"] if q.get("answer") is None]
        if not open_ones:
            break
        q = open_ones[0]
        option = next((o for o in q["options"] if o.get("safe")), q["options"][0])
        case = post(
            "answers", question_id=q["question_id"], option_id=option["option_id"], version=case["version"]
        )
    join = next(i for i in case["items"] if i["kind"] == "build_dataset")
    case = post("decisions", item_id=join["item_id"], accept=True, version=case["version"])

    for _ in range(
        6
    ):  # approve until the case rests: the join is built after the first, the table is approved next
        screen = client.get(f"/cases/{cid}/screen").json()
        case = screen["case"]
        if case["state"] != "approve":
            break
        ready = next(c for c in screen["view"]["conversation"] if c["type"] == "ready")
        for stop in ready["stops"]:
            case = post("acknowledgements", key=stop["key"], reason="test", version=case["version"])
        for yes in ready["confirmations"]:
            case = post(
                "confirmations", item_id=yes["item_id"], sentence=yes["sentence"], version=case["version"]
            )
        sentence = client.get(f"/cases/{cid}/screen").json()["view"]["approval_sentence"]
        client.post(f"/cases/{cid}/approval", json={"version": case["version"], "sentence": sentence})

    screen = client.get(f"/cases/{cid}/screen").json()
    case, view = screen["case"], screen["view"]
    (table,) = case["files"]
    assert table["upload_id"] not in ids and case["state"] == "follow_up", case["state"]
    assert view["verdict"]["key"] in {"ready", "ready_with_warnings"}, view["verdict"]
    assert view["start"] is not None and view["start"]["upload_id"] not in ids
    start_profile = client.get(f"/uploads/{view['start']['upload_id']}/profile").json()
    assert {"orders_90d", "marketing_opt_in"} <= {
        c["name"] for c in start_profile["columns"]
    }, "the table, prepared"
    assert view["start"]["outcome"] == "converted_30d"

    by_id = {i["item_id"]: i for i in case["items"]}
    cards = view["conversation"]
    changes = [c for c in cards if c["type"] == "change"]
    carried = {"build_dataset", "outcome_column", "as_of_column", "consent_column"}
    for card in changes:
        item = by_id[card["item_id"]]
        assert item["upload_id"] in {None, table["upload_id"]} or item["kind"] in carried, card["title"]
    titles = [c["title"] for c in changes]
    assert len(titles) == len(set(titles)), "a fix is drawn once"
    assert any(
        i["kind"] == "outcome_column" for i in by_id.values() if i["upload_id"] in ids
    ), "kept on the case"
    assert cards[-1]["type"] == "ready" and cards[-1]["stage"] == "start" and cards[-1]["hold"] == ""
    first = cards[0]
    in_plan = [
        i
        for i in case["items"]
        if i["state"] == "accepted"
        and i["kind"] != "data_check"
        and (i["upload_id"] in {None, table["upload_id"]} or i["kind"] in carried)
    ]
    assert first["type"] == "first_look" and first["fixes_count"] == len(
        in_plan
    ), "one count of changes: the plan's own, the number the approval sentence says"
    assert first["fixes_count"] >= sum(1 for c in changes if c["state"] == "accepted")
