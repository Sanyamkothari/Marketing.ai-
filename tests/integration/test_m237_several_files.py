"""Plan L M155's journey, now driven by Data Doctor (Plan O M237, DEC-1807; closes DEC-1550 and DEC-1578).

A customer list keyed `CUST-00123`, three bill exports keyed `123` that overlap by one week each (the third
renamed `amount` to `bill_amount`) and a ticket log keyed `cust_no` go in through `POST /uploads`. The
diagnosis says what each file is and how they fit, and offers the churn sentence measured through the ID rule
(`normalise_key`); the person agrees to it with `POST /doctor/outcome`. Then, with no Manual setup:
`plan_build` (the `build_dataset` proposal) stacks the bills counting the overlap once, matches each log's IDs
to the list's (the list keeps `CUST-00123`; the logs' `123` is written `CUST-00123`), counts the tickets the
list does not have, and maps each file; once the plan is approved, `drive_builder` runs it through the
builder's own routes (`ApiBuilderPort`) and keeps the approved plan beside the recipe; the dataset builds and
passes its checks, `POST /runs` accepts it for training, and a scoring build of the saved recipe and a replay
of next month's files both name the customers `CUST-00123`.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes.doctor import _load
from engine.config import LabelDefinition, SnapshotDefinition
from engine.contracts import RunRecord
from engine.doctor.build_driver import BuildDriveError, approval_for, drive_builder, plan_build, replay_build
from engine.doctor.builder_api import ApiBuilderPort
from engine.doctor.contracts import Diagnosis
from engine.storage import LocalStorage
from tests.unit.production.scheduling_support import RecordingJobs

pytestmark = pytest.mark.integration

USE_CASE = "rca"
CUSTOMERS = 300
START = date(2024, 7, 1)
END = date(2025, 6, 30)
CUTS = ((START, date(2024, 11, 7)), (date(2024, 11, 1), date(2025, 3, 7)), (date(2025, 3, 1), END))
BUILD_TIMEOUT_S = 180.0


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def app(config_root: Path, data_dir: Path) -> FastAPI:
    return create_app(config_root=config_root, data_dir=data_dir)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def customers_csv() -> bytes:
    lines = ["customer_id,region,plan_type"]
    lines += [
        f"CUST-{n:05d},{('North', 'South', 'East', 'West')[n % 4]},{'prepaid' if n % 3 else 'postpaid'}"
        for n in range(1, CUSTOMERS + 1)
    ]
    return ("\n".join(lines) + "\n").encode()


def all_bills() -> list[tuple[int, date, int]]:
    """Each customer is billed at a gap drawn around their own rate, so the outcome is not a function of the
    last month (the leakage check would rightly refuse that); the same customer and day is one bill."""
    rng = np.random.default_rng(237)
    means = rng.uniform(5.0, 75.0, size=CUSTOMERS)
    bills: dict[tuple[int, date], int] = {}
    for index in range(CUSTOMERS):
        day = START + timedelta(days=int(rng.integers(0, 10)))
        while day <= END:
            bills[(index + 1, day)] = 149 + (index % 4) * 50
            day += timedelta(days=max(1, int(rng.exponential(means[index]))))
    return [(customer, day, amount) for (customer, day), amount in sorted(bills.items())]


def bills_csv(first: date, last: date, *, amount: str = "amount") -> bytes:
    lines = [f"customer_id,invoice_date,{amount}"]
    lines += [f"{c},{d.isoformat()},{a}" for c, d, a in all_bills() if first <= d <= last]
    return ("\n".join(lines) + "\n").encode()


def tickets_csv() -> bytes:
    lines = ["ticket_id,cust_no,opened_at,category"]
    lines += [
        f"T{n},{1 + n % (CUSTOMERS + 30)},{(START + timedelta(days=n % 360)).isoformat()},billing"
        for n in range(900)
    ]
    return ("\n".join(lines) + "\n").encode()


def upload(client: TestClient, name: str, data: bytes) -> str:
    response = client.post(
        "/uploads",
        files={"file": (name, data, "text/csv")},
        data={"use_case": "payment-propensity", "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _overlap_rows() -> int:
    """Bills the exports repeat: those dated in each one-week overlap."""
    return sum(
        1 for _, day, _ in all_bills() if CUTS[1][0] <= day <= CUTS[0][1] or CUTS[2][0] <= day <= CUTS[1][1]
    )


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


@pytest.fixture
def journey(client: TestClient, data_dir: Path) -> dict[str, Any]:
    ids = {
        "customers": upload(client, "customers.csv", customers_csv()),
        "jul": upload(client, "bills_2024_07.csv", bills_csv(*CUTS[0])),
        "nov": upload(client, "bills_2024_11.csv", bills_csv(*CUTS[1])),
        "mar": upload(client, "bills_2025_03.csv", bills_csv(*CUTS[2], amount="bill_amount")),
        "tickets": upload(client, "support_tickets.csv", tickets_csv()),
    }
    response = client.post("/doctor/diagnosis", json={"upload_ids": list(ids.values())})
    assert response.status_code == 200, response.text
    diagnosis = Diagnosis.model_validate(response.json())
    storage = LocalStorage(data_dir)
    frames = {upload_id: _load(storage, upload_id, 1_000_000).frame for upload_id in ids.values()}
    return {"ids": ids, "diagnosis": diagnosis, "frames": frames}


def _agreed_label(client: TestClient, diagnosis: Diagnosis) -> LabelDefinition:
    """The churn outcome as a person agrees it: the diagnosis's offer (measured through the ID rule, M237),
    the 60-day sentence, `POST /doctor/outcome` with `agreed: true`, and the `LabelSpec` it returns."""
    verdict = next(item for item in diagnosis.use_cases if item.use_case_id == USE_CASE)
    offer = verdict.outcome_offer
    assert offer is not None, "the IDs meet once written one way, so the outcome is offered"
    choice = next(item for item in offer.choices if item.days == 60)
    agreed = client.post(
        "/doctor/outcome",
        json={
            "use_case": USE_CASE,
            "entity_upload_id": offer.entity_upload_id,
            "event_upload_id": offer.event_upload_id,
            "date_column": offer.date_column,
            "days": 60,
            "sentence": choice.sentence,
            "agreed": True,
        },
    )
    assert agreed.status_code == 201, agreed.text
    return LabelDefinition.model_validate(agreed.json()["label_spec"])


def _tickets_dropped() -> int:
    """Tickets for `cust_no` 301 to 330: customers the list does not have."""
    return sum(1 for n in range(900) if 1 + n % (CUSTOMERS + 30) > CUSTOMERS)


def _dataset_keys(data_dir: Path, client: TestClient, dataset_id: str) -> set[str]:
    final = _poll(client, dataset_id)
    assert final["status"]["state"] == "done", final["status"]
    manifest = final["manifest"]
    frame = pd.read_parquet(data_dir / "datasets" / manifest["dataset_id"] / "dataset.parquet")
    return {str(value) for value in frame[manifest["primary_key"][0]]}


def test_the_offer_measures_the_ids_through_the_rule(journey: dict[str, Any]) -> None:
    """M237 review: the M157 offer no longer needs the IDs to meet as written."""
    verdict = next(item for item in journey["diagnosis"].use_cases if item.use_case_id == USE_CASE)
    offer = verdict.outcome_offer
    assert offer is not None and offer.entity_upload_id == journey["ids"]["customers"]
    assert offer.event_rows_matched == 1.0 and offer.customers == CUSTOMERS
    assert any("written one way" in note for note in offer.notes)


def test_the_m155_journey_trains_through_data_doctor_with_no_manual_setup(
    client: TestClient, app: FastAPI, journey: dict[str, Any], data_dir: Path
) -> None:
    ids, diagnosis, frames = journey["ids"], journey["diagnosis"], journey["frames"]
    label = _agreed_label(client, diagnosis)
    assert (label.name, label.role, label.horizon_days) == ("churn_next_60d", "bills", 60)
    plan = plan_build(
        diagnosis,
        frames,
        use_case=USE_CASE,
        label_spec=label,
        snapshot_spec=SnapshotDefinition(max_snapshots=6),
    )
    assert (plan.state, plan.confidence, plan.never_ticked) == ("pending", "check", True)
    assert plan.blocking == () and plan.questions == () and plan.left_out == ()
    assert plan.entity.upload_ids == (ids["customers"],)
    assert plan.entity.key_rule.prefix == "CUST-" and plan.entity.key_rule.zeros
    roles = {source.role: source for source in plan.events}
    assert set(roles) == {"bills", "complaints"}
    bills = roles["bills"]
    assert bills.upload_ids == (ids["jul"], ids["nov"], ids["mar"]) and bills.stack is not None
    assert (bills.key_column, bills.date_column) == ("customer_id", "invoice_date")
    assert bills.coverage == 0.0 and bills.fixed_coverage == 1.0
    tickets = roles["complaints"]
    assert tickets.key_column == "cust_no"
    assert (tickets.rows, tickets.rows_dropped) == (
        900,
        _tickets_dropped(),
    ), "the strangers' tickets, counted"
    (stacked,) = plan.stack_receipts
    assert stacked.duplicates_removed == _overlap_rows(), "each overlapping bill counted once"
    assert stacked.rows_out == len(all_bills())
    assert {
        "BUILD_REWRITES_IDS",
        "JOIN_DROPS_ROWS",
        "STACK_DROPS_ROWS",
        "BUILD_DEFINES_OUTCOME",
    } <= set(plan.approval_reasons)
    assert f"leaves out {_tickets_dropped()} of 900 rows" in plan.approval_sentence

    created = client.post("/clients", json={"name": "Bills Co", "industry": "telecom", "notes": ""})
    assert created.status_code == 201, created.text
    client_id = created.json()["client_id"]
    port = ApiBuilderPort(client, client_id, storage=LocalStorage(data_dir))

    unapproved = approval_for(plan, user_id="analyst-7", at=datetime.now(UTC)).model_copy(
        update={"plan_hash": "0" * 64}
    )
    with pytest.raises(BuildDriveError) as refused:
        drive_builder(plan, frames, port, unapproved)
    assert refused.value.code == "BUILD_PLAN_NOT_APPROVED"

    result = drive_builder(plan, frames, port, approval_for(plan, user_id="analyst-7", at=datetime.now(UTC)))
    assert result.approved_by == "analyst-7" and result.dataset_id is not None
    assert len(result.source_ids) == 3 and len(result.mapping_ids) == 3
    matched = {receipt.source_name: receipt for receipt in result.match_receipts}
    assert matched["bills_stacked.csv"].rows_matched == len(all_bills()) == matched["bills_stacked.csv"].rows
    assert matched["support_tickets.csv"].rows_not_on_list == _tickets_dropped()
    assert result.record_key is not None

    final = _poll(client, result.dataset_id)
    assert final["status"]["state"] == "done", final["status"]
    manifest = final["manifest"]
    assert manifest["target"] == label.name and manifest["n_rows"] > 0
    report = client.get(f"/datasets/{manifest['dataset_id']}/report").json()
    assert report["passed"], [c for c in report["checks"] if c["severity"] == "error"]
    sources = {source["role"]: source for source in report["sources"]}
    assert sources["bills"]["rows"] == len(all_bills()), "the builder read every bill once"
    assert sources["bills"]["join_coverage"] == 1.0, "every bill met its customer"
    customer_ids = {f"CUST-{n:05d}" for n in range(1, CUSTOMERS + 1)}
    trained = pd.read_parquet(data_dir / "datasets" / manifest["dataset_id"] / "dataset.parquet")
    assert {str(value) for value in trained[manifest["primary_key"][0]]} <= customer_ids, "the list's own IDs"

    # A scoring build of the saved recipe names the customers as the client does.
    scoring = client.post(
        "/datasets", json={"client_id": client_id, "spec_id": result.spec_id, "mode": "score"}
    )
    assert scoring.status_code == 202, scoring.text
    assert _dataset_keys(data_dir, client, scoring.json()["dataset_id"]) <= customer_ids

    # Next month: the kept plan replays the frozen stack and ID rules on new files, without a new question.
    kept = port.load_record(result.spec_id)
    assert kept.plan == plan and kept.approval.approved_by == "analyst-7"
    next_month = {
        "customers.csv": [frames[ids["customers"]]],
        "bills_stacked.csv": [frames[ids["nov"]], frames[ids["mar"]]],
        "support_tickets.csv": [frames[ids["tickets"]]],
    }
    replayed = replay_build(kept, next_month, port, mode="score")
    assert replayed.dataset_id is not None and replayed.spec_id != result.spec_id
    assert _dataset_keys(data_dir, client, replayed.dataset_id) <= customer_ids

    app.state.jobs = RecordingJobs()  # training is accepted; the job itself is not run here
    run = client.post(
        "/runs", json={"use_case": USE_CASE, "mode": "train", "dataset_id": manifest["dataset_id"]}
    )
    assert run.status_code == 202, run.text
    record = RunRecord.model_validate_json(
        (data_dir / "runs" / run.json()["run_id"] / "run.json").read_text()
    )
    assert record.dataset_id == manifest["dataset_id"] and record.target == label.name


def test_the_spec_saved_keeps_the_agreed_label_unchanged(client: TestClient, journey: dict[str, Any]) -> None:
    """DEC-1578's promise, kept by the driver: the agreed `LabelSpec` is saved exactly as it was agreed."""
    diagnosis, frames = journey["diagnosis"], journey["frames"]
    label = _agreed_label(client, diagnosis)
    plan = plan_build(diagnosis, frames, use_case=USE_CASE, label_spec=label)
    client_id = client.post("/clients", json={"name": "Co", "industry": "telecom", "notes": ""}).json()[
        "client_id"
    ]
    result = drive_builder(
        plan,
        frames,
        ApiBuilderPort(client, client_id),
        approval_for(plan, user_id="analyst-7", at=datetime.now(UTC)),
        build=False,
    )
    saved = client.get(f"/clients/{client_id}/onboarding-specs").json()["specs"]
    (spec,) = [item for item in saved if item["spec_id"] == result.spec_id]
    assert LabelDefinition.model_validate(spec["label_spec"]) == label
    assert result.dataset_id is None


def test_frames_read_back_are_the_files_sent(journey: dict[str, Any]) -> None:
    frames = journey["frames"]
    assert len(frames[journey["ids"]["customers"]]) == CUSTOMERS
    total = sum(len(frames[journey["ids"][name]]) for name in ("jul", "nov", "mar"))
    assert total == len(all_bills()) + _overlap_rows()
    assert isinstance(frames[journey["ids"]["tickets"]], pd.DataFrame)


def test_an_agreed_outcome_builds_through_the_driver_instead_of_next_steps(
    client: TestClient, data_dir: Path
) -> None:
    """DEC-1578 closed: the agreement's `next_steps` were words; here the same agreement is built by the driver."""
    from tests.integration.test_api_doctor_outcome import customers_csv as m157_customers
    from tests.integration.test_api_doctor_outcome import recharges_csv as m157_recharges

    entity_id = upload(client, "customers.csv", m157_customers())
    events_id = upload(client, "recharges.csv", m157_recharges())
    response = client.post("/doctor/diagnosis", json={"upload_ids": [entity_id, events_id]})
    assert response.status_code == 200, response.text
    diagnosis = Diagnosis.model_validate(response.json())
    verdict = next(item for item in diagnosis.use_cases if item.use_case_id == USE_CASE)
    offer = verdict.outcome_offer
    assert offer is not None
    choice = next(item for item in offer.choices if item.days == 60)
    agreed = client.post(
        "/doctor/outcome",
        json={
            "use_case": USE_CASE,
            "entity_upload_id": offer.entity_upload_id,
            "event_upload_id": offer.event_upload_id,
            "date_column": offer.date_column,
            "days": 60,
            "sentence": choice.sentence,
            "agreed": True,
        },
    )
    assert agreed.status_code == 201, agreed.text
    label = LabelDefinition.model_validate(agreed.json()["label_spec"])
    storage = LocalStorage(data_dir)
    frames = {upload_id: _load(storage, upload_id, 1_000_000).frame for upload_id in (entity_id, events_id)}
    plan = plan_build(diagnosis, frames, use_case=USE_CASE, label_spec=label)
    assert plan.entity.key_rule.changes_nothing, "these IDs meet as written: nothing is rewritten"
    assert "BUILD_REWRITES_IDS" not in plan.approval_reasons
    created = client.post("/clients", json={"name": "Recharge Co", "industry": "telecom", "notes": ""})
    result = drive_builder(
        plan,
        frames,
        ApiBuilderPort(client, created.json()["client_id"]),
        approval_for(plan, user_id="analyst-7", at=datetime.now(UTC)),
    )
    assert result.dataset_id is not None and result.key_receipts == ()
    final = _poll(client, result.dataset_id)
    assert final["status"]["state"] == "done", final["status"]
    assert final["manifest"]["target"] == label.name == "churn_next_60d"
    report = client.get(f"/datasets/{final['manifest']['dataset_id']}/report").json()
    assert report["passed"]
