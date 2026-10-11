"""The outcome from events through the API (Plan L M157, DEC-1573, DEC-1574).

M157's done-when, without a person: a customer list and a recharge log with no "churned" column go in
through `POST /uploads`; the diagnosis offers RCA's outcome as a sentence with its choices, each counted
on the files; `POST /doctor/outcome` records the one the person picked and returns its `LabelSpec`; and
that `LabelSpec`, saved unchanged in an onboarding recipe, builds a dataset - with the month-ends whose
window is not over left out, as the sentence said.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.storage import LocalStorage

pytestmark = pytest.mark.integration

USE_CASE = "rca"
CUSTOMERS = 600
START = date(2024, 7, 1)
END = date(2025, 6, 30)
BUILD_TIMEOUT_S = 120.0


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def customers_csv() -> bytes:
    regions = ("North", "South", "East", "West")
    lines = ["customer_id,region,plan_type"]
    lines += [
        f"C{index:05d},{regions[index % 4]},{'prepaid' if index % 3 else 'postpaid'}"
        for index in range(CUSTOMERS)
    ]
    return ("\n".join(lines) + "\n").encode()


def recharges_csv() -> bytes:
    """Every customer recharges every 15 days; a quarter stop for good at a random date."""
    rng = np.random.default_rng(157)
    span = (END - START).days
    lines = ["recharge_id,customer_id,recharge_date,amount"]
    serial = 0
    for index in range(CUSTOMERS):
        stop = START + timedelta(days=int(rng.integers(60, span))) if rng.random() < 0.25 else END
        day = START + timedelta(days=index % 15)
        while day <= stop:
            lines.append(f"R{serial:07d},C{index:05d},{day.isoformat()},{149 + index % 4 * 50}")
            serial += 1
            day += timedelta(days=15)
    lines.append(f"R{serial:07d},C00000,{END.isoformat()},149")
    return ("\n".join(lines) + "\n").encode()


def upload(client: TestClient, name: str, data: bytes) -> str:
    response = client.post(
        "/uploads",
        files={"file": (name, data, "text/csv")},
        data={"use_case": "payment-propensity", "mode": "train"},
    )
    assert response.status_code == 201, response.text
    upload_id: str = response.json()["upload_id"]
    return upload_id


def offered(client: TestClient, ids: list[str]) -> dict[str, Any]:
    response = client.post("/doctor/diagnosis", json={"upload_ids": ids})
    assert response.status_code == 200, response.text
    verdicts = {item["use_case_id"]: item for item in response.json()["use_cases"]}
    rca = verdicts[USE_CASE]
    assert rca["status"] == "missing" and rca["outcome_offer"] is not None, rca
    offer: dict[str, Any] = rca["outcome_offer"]
    return offer


def agree(client: TestClient, offer: dict[str, Any], pick: int, **change: Any) -> Any:
    choice = next(item for item in offer["choices"] if item["days"] == pick)
    body = {
        "use_case": USE_CASE,
        "entity_upload_id": offer["entity_upload_id"],
        "event_upload_id": offer["event_upload_id"],
        "date_column": offer["date_column"],
        "days": pick,
        "sentence": choice["sentence"],
        "agreed": True,
        **change,
    }
    return client.post("/doctor/outcome", json=body)


@pytest.fixture
def uploaded(client: TestClient) -> tuple[str, str]:
    return upload(client, "customers.csv", customers_csv()), upload(client, "recharges.csv", recharges_csv())


def test_the_diagnosis_offers_the_outcome_as_a_sentence_with_measured_choices(
    client: TestClient, uploaded: tuple[str, str]
) -> None:
    entity_id, events_id = uploaded
    offer = offered(client, [entity_id, events_id])
    assert offer["entity_upload_id"] == entity_id and offer["event_upload_id"] == events_id
    assert offer["event_id_column"] == "customer_id" and offer["date_column"] == "recharge_date"
    assert offer["question"] == "When has a customer churned?"
    assert offer["customers"] == CUSTOMERS and offer["data_end"] == END.isoformat()
    sentences = [choice["sentence"] for choice in offer["choices"]]
    assert sentences == [
        f"A customer has churned when there is no recharge for {days} days after the snapshot."
        for days in (30, 60, 90)
    ]
    for choice in offer["choices"]:
        assert choice["yes"] + choice["no"] == choice["customers"] == CUSTOMERS
        assert choice["label_spec"]["type"] == "event_absence"
        assert choice["label_spec"]["horizon_days"] == choice["days"]
        assert choice["not_over"] >= 1, "the last month-end's window always runs past the file"
    # A longer window can only make more customers "yes" at the same date; at its own (earlier) date
    # the count is whatever the builder measured, so only the shape is asserted here.
    assert all(choice["measured_on"] for choice in offer["choices"])


def test_agreeing_records_the_choice_and_returns_its_label_spec(
    client: TestClient, uploaded: tuple[str, str], data_dir: Path
) -> None:
    offer = offered(client, list(uploaded))
    response = agree(client, offer, 60)
    assert response.status_code == 201, response.text
    agreement = response.json()
    sixty = next(item for item in offer["choices"] if item["days"] == 60)
    assert agreement["sentence"] == sixty["sentence"]
    assert agreement["label_spec"] == sixty["label_spec"]
    assert agreement["choice"]["yes"] == sixty["yes"], "measured again, on the same files, the same count"
    assert agreement["next_steps"] and "POST /datasets" in agreement["next_steps"][-1]
    stored = LocalStorage(data_dir).read_text(f"uploads/{uploaded[0]}/outcome_{USE_CASE}.json")
    assert agreement["agreement_id"] in stored


def test_nothing_is_agreed_without_an_explicit_yes_to_the_exact_sentence(
    client: TestClient, uploaded: tuple[str, str], data_dir: Path
) -> None:
    offer = offered(client, list(uploaded))
    refused = agree(client, offer, 60, agreed=False)
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "OUTCOME_NOT_AGREED"
    edited = agree(client, offer, 60, sentence="A customer has churned when I say so.")
    assert edited.status_code == 409 and edited.json()["detail"]["code"] == "OUTCOME_SENTENCE_CHANGED"
    other = agree(client, offer, 60, days=45)
    assert other.status_code == 422 and other.json()["detail"]["code"] == "OUTCOME_CHOICE_UNKNOWN"
    assert not LocalStorage(data_dir).exists(f"uploads/{uploaded[0]}/outcome_{USE_CASE}.json")


def test_files_that_cannot_define_it_and_use_cases_without_a_sentence_are_refused(
    client: TestClient, uploaded: tuple[str, str]
) -> None:
    offer = offered(client, list(uploaded))
    no_dates = upload(client, "notes.csv", b"customer_id,comment\nC00001,called\nC00002,emailed\n")
    swapped = agree(client, offer, 60, event_upload_id=no_dates)
    assert swapped.status_code == 409 and swapped.json()["detail"]["code"] == "OUTCOME_NOT_OFFERED"
    same = agree(client, offer, 60, event_upload_id=offer["entity_upload_id"])
    assert same.status_code == 422 and same.json()["detail"]["code"] == "OUTCOME_SAME_FILE"
    elsewhere = agree(client, offer, 60, use_case="order-fulfillment")
    assert elsewhere.status_code == 404 and elsewhere.json()["detail"]["code"] == "OUTCOME_NO_SENTENCE"


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


def _mapping(client: TestClient, client_id: str, source_id: str, columns: list[dict[str, Any]]) -> str:
    suggested = client.post(
        f"/clients/{client_id}/mappings/suggest", json={"source_id": source_id, "use_case": USE_CASE}
    )
    assert suggested.status_code == 200, suggested.text
    suggestion = suggested.json()
    saved = client.put(
        f"/clients/{client_id}/mappings/{suggestion['mapping_id']}",
        json={
            "client_id": client_id,
            "source_id": source_id,
            "use_case": USE_CASE,
            "role": suggestion["role"],
            "columns": columns,
        },
    )
    assert saved.status_code == 200, saved.text
    mapping_id: str = saved.json()["mapping_id"]
    return mapping_id


def _column(source: str, standard: str, cast: str | None = None) -> dict[str, Any]:
    transform = None if cast is None else {"kind": "cast", "to_type": cast}
    return {
        "source": source,
        "standard": standard,
        "transform": transform,
        "confidence": 1.0,
        "decided_by": "user",
    }


def test_the_agreed_label_spec_builds_through_the_onboarding_builder(
    client: TestClient, uploaded: tuple[str, str]
) -> None:
    """The agreement's `next_steps`, followed: the `LabelSpec` goes into a recipe unchanged and builds."""
    offer = offered(client, list(uploaded))
    agreement = agree(client, offer, 60).json()
    created = client.post("/clients", json={"name": "Recharge Co", "industry": "telecom", "notes": ""})
    assert created.status_code == 201, created.text
    client_id = created.json()["client_id"]
    entity = client.post(
        f"/clients/{client_id}/sources",
        files={"file": ("customers.csv", customers_csv(), "text/csv")},
        data={"role": "entity"},
    )
    assert entity.status_code == 201, entity.text
    events = client.post(
        f"/clients/{client_id}/sources",
        files={"file": ("recharges.csv", recharges_csv(), "text/csv")},
        data={"role": agreement["event_role"]},
    )
    assert events.status_code == 201, events.text
    entity_id, events_id = entity.json()["source_id"], events.json()["source_id"]
    mappings = [
        _mapping(client, client_id, entity_id, [_column(agreement["entity_id_column"], "entity_key")]),
        _mapping(
            client,
            client_id,
            events_id,
            [
                _column(agreement["event_id_column"], "entity_key"),
                _column(agreement["date_column"], "event_time", "date"),
            ],
        ),
    ]
    spec = client.post(
        f"/clients/{client_id}/onboarding-specs",
        json={
            "use_case": USE_CASE,
            "entity_source_id": entity_id,
            "event_source_ids": [events_id],
            "mapping_ids": mappings,
            "feature_spec": {
                "features": [
                    {
                        "name": "recharges_30d",
                        "role": agreement["event_role"],
                        "function": "count",
                        "window_days": 30,
                    },
                    {
                        "name": "days_since_recharge",
                        "role": agreement["event_role"],
                        "function": "days_since_last",
                    },
                ]
            },
            "label_spec": agreement["label_spec"],
            "snapshot_spec": {"mode": "periodic", "frequency": "monthly"},
        },
    )
    assert spec.status_code == 201, spec.text
    saved = client.get(f"/clients/{client_id}/onboarding-specs").json()["specs"][0]
    assert saved["label_spec"] == agreement["label_spec"], "saved unchanged"

    built = client.post(
        "/datasets", json={"client_id": client_id, "spec_id": spec.json()["spec_id"], "mode": "train"}
    )
    assert built.status_code == 202, built.text
    final = _poll(client, built.json()["dataset_id"])
    assert final["status"]["state"] == "done", final["status"]
    manifest = final["manifest"]
    assert manifest["target"] == "churn_next_60d"
    assert manifest["n_rows"] > 0
    report = client.get(f"/datasets/{manifest['dataset_id']}/report").json()
    assert report["passed"]
    stats = report["snapshots"]
    assert stats and all(stat["positives"] is not None for stat in stats)
    last_counted = max(date.fromisoformat(stat["date"]) for stat in stats if not stat["censored"])
    assert last_counted + timedelta(days=60) <= END, "no month-end whose window is not over is labelled"
