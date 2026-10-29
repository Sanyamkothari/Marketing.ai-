"""Data Doctor's intake through the API (Plan L M150, DEC-1501, DEC-1504).

`POST /uploads` and `POST /clients/{id}/sources` run intake between the write and ingest: a workbook
becomes its table, a report export loses its title and total rows, the response says what was done,
the file as received is kept, and `intake=false` reads the file as before. Everything an upload
directory gains is removed with it.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook  # type: ignore[import-untyped]

from api.main import create_app
from engine.storage import LocalStorage
from tests.unit.intake.bench import CASES, ROWS, write

USE_CASE = "targeted-advertisement"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BY_NAME = {case.name: case for case in CASES}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


@pytest.fixture
def storage(data_dir: Path) -> LocalStorage:
    return LocalStorage(data_dir)


def upload(client: TestClient, path: Path, *, mode: str = "train", **form: str) -> Any:
    kind = XLSX if path.suffix == ".xlsx" else "text/csv"
    return client.post(
        "/uploads",
        files={"file": (path.name, path.read_bytes(), kind)},
        data={"use_case": USE_CASE, "mode": mode, **form},
    )


def stored(storage: LocalStorage, upload_id: str) -> set[str]:
    return {key.rsplit("/", 1)[-1] for key in storage.list_keys(f"uploads/{upload_id}/")}


def test_a_report_export_csv_is_read_as_its_table(
    client: TestClient, storage: LocalStorage, tmp_path: Path
) -> None:
    response = upload(client, write(BY_NAME["11_report_export"], tmp_path))
    assert response.status_code == 201, response.text
    body = response.json()
    assert [column["name"] for column in body["profile"]["columns"]] == BY_NAME[
        "11_report_export"
    ].expected_columns
    assert body["profile"]["row_count"] == ROWS
    intake = body["intake"]
    assert intake["applied"] and intake["rows_above_header"] == 3 and intake["footer_rows"] == 2
    assert intake["notes"], "the upload screen has nothing to say"
    assert stored(storage, body["upload_id"]) >= {"source.csv", "original.csv", "intake.json", "upload.json"}
    record = client.get(f"/uploads/{body['upload_id']}/profile")
    assert record.status_code == 200


def test_a_clean_csv_is_stored_exactly_as_before(
    client: TestClient, storage: LocalStorage, tmp_path: Path
) -> None:
    path = write(BY_NAME["01_clean_table"], tmp_path)
    body = upload(client, path).json()
    assert not body["intake"]["applied"] and body["intake"]["notes"] == []
    assert "intake.json" not in stored(storage, body["upload_id"])
    assert "original.csv" not in stored(storage, body["upload_id"])
    assert storage.read_bytes(f"uploads/{body['upload_id']}/source.csv") == path.read_bytes()


def test_intake_off_reads_the_title_as_the_header_as_before(client: TestClient, tmp_path: Path) -> None:
    body = upload(client, write(BY_NAME["02_one_title"], tmp_path), intake="false").json()
    assert body["intake"]["turned_off"]
    assert body["profile"]["columns"][0]["name"] == "Customer export"


def test_repeated_column_names_are_numbered_instead_of_refused(client: TestClient, tmp_path: Path) -> None:
    response = upload(client, write(BY_NAME["07_repeated_name"], tmp_path))
    assert response.status_code == 201, response.text
    assert [column["name"] for column in response.json()["profile"]["columns"]][-1] == "plan_2"


def test_a_workbook_is_read_from_its_table_sheet(
    client: TestClient, storage: LocalStorage, tmp_path: Path
) -> None:
    response = upload(client, write(BY_NAME["26_xlsx_report"], tmp_path))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["profile"]["row_count"] == ROWS
    assert body["profile"]["file_name"] == "26_xlsx_report.xlsx"
    assert body["intake"]["sheet"] == "Customers"
    assert any('"Summary"' in note for note in body["intake"]["notes"])
    assert stored(storage, body["upload_id"]) >= {"source.csv", "original.xlsx", "intake.json"}
    record = storage.read_text(f"uploads/{body['upload_id']}/upload.json")
    assert '"original_key"' in record and "original.xlsx" in record


def test_another_sheet_can_be_chosen(client: TestClient, tmp_path: Path) -> None:
    body = upload(client, write(BY_NAME["25_xlsx_other_sheets"], tmp_path), sheet="Summary").json()
    assert body["intake"]["sheet"] == "Summary"
    assert [column["name"] for column in body["profile"]["columns"]] == ["plan", "customers"]


def test_a_sheet_that_is_not_there_is_422_and_leaves_nothing(
    client: TestClient, storage: LocalStorage, tmp_path: Path
) -> None:
    response = upload(client, write(BY_NAME["25_xlsx_other_sheets"], tmp_path), sheet="Bills")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UPLOAD_SHEET_NOT_FOUND"
    assert storage.list_keys("uploads/") == ()


def test_an_old_xls_file_is_415_with_what_to_do(client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "customers.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0 not really a workbook")
    response = upload(client, path)
    assert response.status_code == 415
    assert "save it as .xlsx or CSV" in response.json()["detail"]["message"]


def test_a_damaged_workbook_is_422_and_leaves_nothing(
    client: TestClient, storage: LocalStorage, tmp_path: Path
) -> None:
    path = tmp_path / "customers.xlsx"
    path.write_bytes(b"id,plan\n1,Gold\n")
    response = upload(client, path)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UPLOAD_WORKBOOK_UNREADABLE"
    assert storage.list_keys("uploads/") == ()


def test_next_months_scoring_workbook_reads_to_the_training_columns(
    client: TestClient, tmp_path: Path
) -> None:
    train = upload(client, write(BY_NAME["26_xlsx_report"], tmp_path), mode="train").json()
    score = upload(client, write(BY_NAME["30_next_month_xlsx"], tmp_path), mode="score").json()
    assert [c["name"] for c in train["profile"]["columns"]] == [
        c["name"] for c in score["profile"]["columns"]
    ]
    assert [c["inferred_type"] for c in train["profile"]["columns"]] == [
        c["inferred_type"] for c in score["profile"]["columns"]
    ]


def test_ask_your_data_opens_on_an_intake_rewritten_upload(client: TestClient, tmp_path: Path) -> None:
    upload_id = upload(client, write(BY_NAME["27_xlsx_everything"], tmp_path)).json()["upload_id"]
    response = client.get(f"/uploads/{upload_id}/ask")
    assert response.status_code == 200, response.text
    assert response.json()["session"] is None


# ---------------------------------------------------------------------------
# Client sources (the raw-tables builder)
# ---------------------------------------------------------------------------
def test_a_workbook_source_is_read_and_deleted_whole(client: TestClient, storage: LocalStorage) -> None:
    created = client.post("/clients", json={"name": "Acme Water Co", "industry": "telecom", "notes": ""})
    assert created.status_code == 201, created.text
    client_id = created.json()["client_id"]
    book = Workbook()
    sheet = book.active
    sheet.title = "Bills"
    for row in (["Billing export"], [], ["account", "bill_date", "amount"]):
        sheet.append(row)
    for index in range(30):
        sheet.append([f"A{index}", f"2026-0{1 + index % 9}-01", 100 + index])
    sheet.append(["Grand Total", None, 4935])
    buffer = io.BytesIO()
    book.save(buffer)
    response = client.post(
        f"/clients/{client_id}/sources",
        files={"file": ("bills.xlsx", buffer.getvalue(), XLSX)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["intake"]["footer_rows"] == 1 and body["intake"]["rows_above_header"] == 2
    assert body["profile"]["profile"]["row_count"] == 30
    folder = f"clients/{client_id}/sources/{body['source_id']}/"
    assert {key.rsplit("/", 1)[-1] for key in storage.list_keys(folder)} >= {
        "raw.csv",
        "original.xlsx",
        "intake.json",
    }
    deleted = client.delete(f"/clients/{client_id}/sources/{body['source_id']}")
    assert deleted.status_code == 204
    assert storage.list_keys(folder) == ()


# ---------------------------------------------------------------------------
# A failed upload leaves nothing behind (Plan L M161)
# ---------------------------------------------------------------------------
def test_a_damaged_workbook_is_a_422_and_leaves_nothing(client: TestClient, data_dir: Path) -> None:
    from tests.unit.intake.test_intake_run import rewritten, small_table

    data = rewritten(small_table(), lambda xml: xml.replace(b'r="A15"', b'r="A!15"'))
    response = client.post(
        "/uploads",
        files={"file": ("damaged.xlsx", data, XLSX)},
        data={"use_case": "payment-propensity", "mode": "train"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "UPLOAD_WORKBOOK_UNREADABLE"
    assert not list(data_dir.glob("uploads/*/*"))


def test_an_unexpected_failure_leaves_no_file_retention_cannot_find(
    config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.stages import ingest

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(ingest, "profile_dataset", broken)
    with (
        TestClient(create_app(config_root=config_root, data_dir=data_dir)) as local,
        pytest.raises(RuntimeError),
    ):
        local.post(
            "/uploads",
            files={"file": ("a.csv", b"Report\nid,spend\nC1,1\nC2,2\n", "text/csv")},
            data={"use_case": "payment-propensity", "mode": "train"},
        )
    assert not list(data_dir.glob("uploads/*/*"))
