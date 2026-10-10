"""Data Doctor, the place, through the API (Plan L M151, DEC-1512).

M151's done-when, without a person: three mixed files (a customer history workbook with a title row and
the outcome, a bills log and a notes sheet) go in through `POST /uploads`, `POST /doctor/diagnosis` says
what each one is and that Targeted Advertisement can be started from the history file, and
`POST /doctor/handoff` makes that file an upload of the use case on which Guided setup starts - with no
Manual setup on the way.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook  # type: ignore[import-untyped]

from api.main import create_app
from engine.storage import LocalStorage
from tests.fixtures.make_data import GenerationSpec, generate

USE_CASE = "targeted-advertisement"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def history_workbook(rows: int = 3000) -> bytes:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=rows, variant="clean"))
    book = Workbook()
    sheet = book.active
    sheet.title = "History"
    sheet.append(["Customer history export"])
    sheet.append(list(frame.columns))
    for row in frame.itertuples(index=False):
        sheet.append([None if pd.isna(value) else value for value in row])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def bills_csv() -> bytes:
    lines = ["customer_id,invoice_date,amount"]
    lines += [
        f"C{index % 150},2025-{1 + index % 12:02d}-{1 + index % 27:02d},{100 + index}" for index in range(600)
    ]
    return ("\n".join(lines) + "\n").encode()


def upload(client: TestClient, name: str, data: bytes, kind: str = "text/csv") -> dict[str, Any]:
    response = client.post(
        "/uploads",
        files={"file": (name, data, kind)},
        data={"use_case": "payment-propensity", "mode": "train"},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def test_three_mixed_files_reach_guided_setup_without_manual_setup(
    client: TestClient, data_dir: Path
) -> None:
    history = upload(client, "history.xlsx", history_workbook(), XLSX)
    bill_log = upload(client, "bills_2025.csv", bills_csv())
    notes = upload(client, "notes.csv", b"topic,owner,comment\nexport,ops,monthly\nlabels,crm,see wiki\n")
    ids = [history["upload_id"], bill_log["upload_id"], notes["upload_id"]]

    response = client.post("/doctor/diagnosis", json={"upload_ids": ids})
    assert response.status_code == 200, response.text
    diagnosis = response.json()
    files = {file["upload_id"]: file for file in diagnosis["files"]}
    assert files[history["upload_id"]]["kind"] == "entity"
    assert files[history["upload_id"]]["notes"], "the title row intake skipped is reported"
    assert files[bill_log["upload_id"]]["kind"] == "bills"
    verdicts = {item["use_case_id"]: item for item in diagnosis["use_cases"]}
    ad = verdicts[USE_CASE]
    assert ad["status"] in {"ready", "check"}, ad["summary"]
    assert ad["upload_id"] == history["upload_id"]
    statuses = [item["status"] for item in diagnosis["use_cases"]]
    assert statuses == sorted(statuses, key=["ready", "check", "too_small", "missing"].index)
    missing = [
        item for item in diagnosis["use_cases"] if item["status"] == "missing" and item["upload_id"] is None
    ]
    assert missing and all(item["needs"] for item in missing)

    handed = client.post("/doctor/handoff", json={"upload_id": ad["upload_id"], "use_case": USE_CASE})
    assert handed.status_code == 201, handed.text
    upload_id = handed.json()["upload_id"]
    assert upload_id != history["upload_id"]
    record = LocalStorage(data_dir).read_text(f"uploads/{upload_id}/upload.json")
    assert f'"use_case_id": "{USE_CASE}"' in record or f'"use_case_id":"{USE_CASE}"' in record
    assert handed.json()["intake"]["sheet"] == "History"
    assert handed.json()["profile"]["row_count"] == 3000

    session = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    assert session.status_code in {200, 201}, session.text
    assert session.json()["session"]["status"] in {"needs_review", "ready"}


def test_a_handoff_to_the_files_own_use_case_is_the_same_upload(client: TestClient) -> None:
    bill_log = upload(client, "bills.csv", bills_csv())
    response = client.post(
        "/doctor/handoff", json={"upload_id": bill_log["upload_id"], "use_case": "payment-propensity"}
    )
    assert response.status_code == 200
    assert response.json()["upload_id"] == bill_log["upload_id"]


def test_a_file_read_as_it_is_is_handed_over_as_it_is(client: TestClient) -> None:
    response = client.post(
        "/uploads",
        files={"file": ("raw.csv", b"Report,,\nid,a,b\n1,2,3\n4,5,6\n", "text/csv")},
        data={"use_case": "payment-propensity", "mode": "train", "intake": "false"},
    )
    upload_id = response.json()["upload_id"]
    handed = client.post("/doctor/handoff", json={"upload_id": upload_id, "use_case": USE_CASE})
    assert handed.status_code == 201, handed.text
    assert handed.json()["intake"]["turned_off"]
    assert handed.json()["profile"]["columns"][0]["name"] == "Report"


def test_unknown_uploads_and_use_cases_are_404(client: TestClient) -> None:
    assert client.post("/doctor/diagnosis", json={"upload_ids": ["u_nope"]}).status_code == 404
    bill_log = upload(client, "bills.csv", bills_csv())
    response = client.post(
        "/doctor/handoff", json={"upload_id": bill_log["upload_id"], "use_case": "no-such-case"}
    )
    assert response.status_code == 404


def test_an_empty_request_is_422(client: TestClient) -> None:
    assert client.post("/doctor/diagnosis", json={"upload_ids": []}).status_code == 422


def test_a_diagnosis_reads_a_bounded_share_of_each_file_and_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every file is held at once, so the rows read are shared out (M161, DEC-1507), and a share says when."""
    from api.routes import doctor

    monkeypatch.setattr(doctor, "DIAGNOSIS_ROWS", 400)
    monkeypatch.setattr(doctor, "ADVISE_ROWS", 100)
    customers = upload(
        client, "customers.csv", ("customer_id,plan\n" + "".join(f"C{i},a\n" for i in range(150))).encode()
    )
    bills = upload(client, "bills.csv", bills_csv())
    diagnosis = client.post(
        "/doctor/diagnosis", json={"upload_ids": [customers["upload_id"], bills["upload_id"]]}
    )
    assert diagnosis.status_code == 200, diagnosis.text
    files = {item["upload_id"]: item for item in diagnosis.json()["files"]}
    assert files[bills["upload_id"]]["rows"] == 600, "the file's own row count, not the rows read"
    links = diagnosis.json()["links"]
    assert links and all(link["sampled"] for link in links)
