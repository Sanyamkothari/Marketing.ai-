"""How several files fit together, through `POST /doctor/diagnosis` (Plan L M155, DEC-1551, DEC-1552).

M155's files without a person: a customer list keyed `CUST-00123`, three monthly bills keyed `123` (the third
renamed a column) and a ticket log go in through `POST /uploads`. The diagnosis says how each log meets the
customer list - measured as written, and again once the fix it names is applied - and that the three bills are
the same report for different months, with the renamed column as drift.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app

CUSTOMERS = 150


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def customers_csv() -> bytes:
    lines = ["customer_id,city,plan"]
    lines += [
        f"CUST-{index:05d},{('Pune', 'Delhi')[index % 2]},{('pre', 'post')[index % 3 % 2]}"
        for index in range(1, CUSTOMERS + 1)
    ]
    return ("\n".join(lines) + "\n").encode()


def bills_csv(month: int, *, amount: str = "amount") -> bytes:
    lines = [f"customer_id,invoice_date,{amount}"]
    lines += [
        f"{1 + index % CUSTOMERS},2025-{month:02d}-{1 + index % 28:02d},{100 + index}" for index in range(450)
    ]
    return ("\n".join(lines) + "\n").encode()


def tickets_csv() -> bytes:
    lines = ["ticket_id,cust_no,opened_at,category"]
    lines += [
        f"T{index},{1 + index % (CUSTOMERS + 30)},2025-02-{1 + index % 28:02d},billing"
        for index in range(300)
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


def test_the_diagnosis_says_how_the_files_fit_together(client: TestClient) -> None:
    ids = {
        "customers": upload(client, "customers.csv", customers_csv()),
        "jan": upload(client, "bills_2025_01.csv", bills_csv(1)),
        "feb": upload(client, "bills_2025_02.csv", bills_csv(2)),
        "mar": upload(client, "bills_2025_03.csv", bills_csv(3, amount="bill_amount")),
        "tickets": upload(client, "support_tickets.csv", tickets_csv()),
    }
    response = client.post("/doctor/diagnosis", json={"upload_ids": list(ids.values())})
    assert response.status_code == 200, response.text
    diagnosis: dict[str, Any] = response.json()
    kinds = {file["upload_id"]: file["kind"] for file in diagnosis["files"]}
    assert kinds[ids["customers"]] == "entity"
    assert {kinds[ids[month]] for month in ("jan", "feb", "mar")} == {"bills"}
    assert kinds[ids["tickets"]] == "complaints"

    links = {link["upload_id"]: link for link in diagnosis["links"]}
    assert set(links) == {ids["jan"], ids["feb"], ids["mar"], ids["tickets"]}
    bills = links[ids["jan"]]
    assert (bills["list_upload_id"], bills["list_column"], bills["column"]) == (
        ids["customers"],
        "customer_id",
        "customer_id",
    )
    assert bills["coverage"] == 0.0 and bills["fixed_coverage"] == 1.0
    assert "'CUST-'" in bills["fix"] and "zeros" in bills["fix"]
    tickets = links[ids["tickets"]]
    assert tickets["column"] == "cust_no"
    # 300 tickets over 180 numbers, 150 of them on the list: measured, not assumed.
    expected = round(sum(1 for index in range(300) if 1 + index % 180 <= CUSTOMERS) / 300, 4)
    assert tickets["coverage"] == 0.0 and tickets["fixed_coverage"] == expected

    (stack,) = diagnosis["stacks"]
    assert stack["upload_ids"] == [ids["jan"], ids["feb"], ids["mar"]]
    assert stack["rows"] == 1350
    (drift,) = stack["drift"]
    assert drift["upload_id"] == ids["mar"]
    assert drift["renamed"] == [{"before": "amount", "after": "bill_amount"}]
    assert "can be stacked" in stack["summary"]


def test_one_file_has_no_links_and_no_stacks(client: TestClient) -> None:
    response = client.post(
        "/doctor/diagnosis", json={"upload_ids": [upload(client, "bills.csv", bills_csv(1))]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["links"] == [] and response.json()["stacks"] == []
