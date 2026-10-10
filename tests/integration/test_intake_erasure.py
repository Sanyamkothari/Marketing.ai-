"""Erasure and access reach the files intake keeps as they arrived (Plan L M161, DEC-1507).

A workbook is a zip, so its bytes never show an id; an original CSV with titles above its header cannot
be matched by key column. Both are found, and erasure deletes them rather than rewriting them, so no
copy of the person is left behind; Data Doctor's hand-off then uses the table read from the file.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook  # type: ignore[import-untyped]

from api.main import create_app
from engine.access.roles import LOCAL_OPERATOR
from engine.platform_db import sqlite_engine
from engine.privacy.erasure import erase, find_principal
from engine.storage import LocalStorage

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PERSON = "CUST-1005"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def workbook(extra_sheet: bool = False) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Customers"
    sheet.append(["Customer export"])
    sheet.append(["customer_id", "plan", "spend", "paid_on_time"])
    for index in range(1000, 1300):
        sheet.append([f"CUST-{index}", "Gold" if index % 2 else "Silver", index % 97, index % 2])
    if extra_sheet:
        notes = book.create_sheet("Callbacks")
        notes.append(["who", "when"])
        notes.append(["CUST-9999", "Monday"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def upload(client: TestClient, name: str, data: bytes, kind: str) -> str:
    response = client.post(
        "/uploads",
        files={"file": (name, data, kind)},
        data={"use_case": "payment-propensity", "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def test_a_person_in_a_kept_workbook_is_found_and_the_workbook_deleted(
    client: TestClient, data_dir: Path, tmp_path: Path
) -> None:
    upload_id = upload(client, "export.xlsx", workbook(), XLSX)
    storage = LocalStorage(data_dir)
    original = f"uploads/{upload_id}/original.xlsx"
    assert PERSON.encode() not in storage.read_bytes(original), "zipped: the bytes alone never show the id"

    keys = {location.key for location in find_principal(storage, PERSON).locations}
    assert {original, f"uploads/{upload_id}/source.csv"} <= keys

    outcome = erase(
        storage,
        PERSON,
        engine=sqlite_engine(tmp_path / "platform.db"),
        principal=LOCAL_OPERATOR,
        salt="s" * 32,
    )
    assert outcome.status in {"completed", "completed_with_exceptions"}
    assert original not in outcome.unrewritable_keys
    assert not storage.exists(original)
    assert not find_principal(storage, PERSON).locations

    # The hand-off now takes the table read from the workbook.
    handed = client.post(
        "/doctor/handoff", json={"upload_id": upload_id, "use_case": "targeted-advertisement"}
    )
    assert handed.status_code == 201, handed.text


def test_a_person_only_on_another_sheet_is_found_in_the_kept_workbook(
    client: TestClient, data_dir: Path
) -> None:
    upload_id = upload(client, "export.xlsx", workbook(extra_sheet=True), XLSX)
    storage = LocalStorage(data_dir)
    keys = {location.key for location in find_principal(storage, "CUST-9999").locations}
    assert keys == {f"uploads/{upload_id}/original.xlsx"}


def test_a_kept_csv_under_titles_goes_with_the_table_read_from_it(client: TestClient, data_dir: Path) -> None:
    rows = "\n".join(f"{index},Gold,{index % 50},{index % 2}" for index in range(100, 400))
    data = f"Monthly export\n\ncustomer_id,plan,spend,paid_on_time\n{rows}\n".encode()
    upload_id = upload(client, "export.csv", data, "text/csv")
    storage = LocalStorage(data_dir)
    keys = {location.key for location in find_principal(storage, "105").locations}
    assert f"uploads/{upload_id}/original.csv" in keys
