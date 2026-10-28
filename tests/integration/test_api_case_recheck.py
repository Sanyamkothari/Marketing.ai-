"""Next month's recheck through the API: attach a new upload to a ready case, get the drift report and a linked case."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.doctor import case as doctor_case
from engine.doctor.case import CaseApproval, CaseStore, VisitState
from engine.storage import LocalStorage
from tests.integration.test_api_cases import code_of, upload

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def make_ready(tmp_path: Path, case_id: str) -> None:
    store = CaseStore(LocalStorage(tmp_path / "data"))

    def change(case: doctor_case.Case) -> doctor_case.ChangeResult:
        approval = CaseApproval(approval_id="ap1", approved_by="asha", approved_at=T0)
        return case.model_copy(update={"state": VisitState.FOLLOW_UP, "approvals": (approval,)}), ()

    store.update("default", case_id, expected_version=1, actor="asha", change=change)


def test_a_case_that_is_not_ready_cannot_be_rechecked(client: TestClient) -> None:
    first, second = upload(client, "customers.csv"), upload(client, "customers.csv", seed=3)
    case = client.post("/cases", json={"upload_ids": [first]}).json()
    refused = client.post(f"/cases/{case['case_id']}/recheck", json={"upload_id": second})
    assert refused.status_code == 409 and code_of(refused) == "RECHECK_NOT_READY"


def test_recheck_opens_a_linked_case_and_answers_the_report(client: TestClient, tmp_path: Path) -> None:
    first, second = upload(client, "customers.csv"), upload(client, "customers.csv", seed=3)
    case = client.post("/cases", json={"upload_ids": [first]}).json()
    make_ready(tmp_path, case["case_id"])
    made = client.post(f"/cases/{case['case_id']}/recheck", json={"upload_id": second})
    assert made.status_code == 201, made.text
    report = made.json()
    assert report["source_case_id"] == case["case_id"] and report["uploads"] == {first: second}
    assert report["case_id"] not in (None, case["case_id"])
    assert "TOO_FEW_ROWS" in {f["code"] for f in report["findings"]}  # 300 rows: the hard floor holds
    linked = client.get(f"/cases/{report['case_id']}").json()
    assert linked["recheck_of"]["source_case_id"] == case["case_id"]
    assert [f["upload_id"] for f in linked["files"]] == [second]
    again = client.get(f"/cases/{report['case_id']}/recheck")
    assert again.status_code == 200 and again.json() == report
    assert client.get(f"/cases/{case['case_id']}").json()["files"][0]["upload_id"] == first


def test_the_new_upload_must_match_the_case(client: TestClient) -> None:
    first = upload(client, "customers.csv")
    case = client.post("/cases", json={"upload_ids": [first]}).json()
    wrong = client.post(f"/cases/{case['case_id']}/recheck", json={"uploads": {"nope": first}})
    assert wrong.status_code == 422 and code_of(wrong) == "RECHECK_FILES_MISMATCH"
