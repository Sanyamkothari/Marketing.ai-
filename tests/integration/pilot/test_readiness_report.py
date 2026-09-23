"""The data readiness report on real builds (Plan E M60), without training anything.

Two clients are onboarded through the API exactly as the demo seed does it: one from clean raw
tables, one from the broken extract with a single planted problem (a repeated customer ID).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.pilot.plain import jargon_in
from scripts.seed_demo import TABLE_ROLES, _build, _onboard
from tests.fixtures.raw.make_raw import make_duplicate_entities, make_raw

pytestmark = pytest.mark.integration

CUSTOMERS = 1_100
USAGE_ROWS = 4_000


@pytest.fixture(scope="module")
def client(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path_factory.mktemp("data"))) as test:
        yield test


def built(client: TestClient, tmp: Path, name: str, maker: Any) -> str:
    tables = maker(tmp, customers=CUSTOMERS, usage_rows=USAGE_ROWS)
    client_id = client.post("/clients", json={"name": name, "industry": "telecom"}).json()["client_id"]
    spec_id, _, _ = _onboard(client, client_id, {table: getattr(tables, table) for table in TABLE_ROLES})
    dataset_id, _ = _build(client, client_id, spec_id, "train")
    return dataset_id


@pytest.fixture(scope="module")
def broken(client: TestClient, tmp_path_factory: pytest.TempPathFactory) -> str:
    return built(client, tmp_path_factory.mktemp("broken"), "Broken Telecom", make_duplicate_entities)


@pytest.fixture(scope="module")
def clean(client: TestClient, tmp_path_factory: pytest.TempPathFactory) -> str:
    return built(client, tmp_path_factory.mktemp("clean"), "Clean Telecom", make_raw)


def blocks(client: TestClient, dataset_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/pilot/readiness/{dataset_id}", params={"format": "json"})
    assert response.status_code == 200, response.text
    return list(response.json()["blocks"])


def test_the_broken_extract_is_not_ready_and_names_its_one_problem(client: TestClient, broken: str) -> None:
    document = blocks(client, broken)
    assert document[0]["state"] == "not_ready"
    assert document[0]["title"] == "Not ready: The customer table repeats customer IDs"
    errors = [b for b in document if b["kind"] == "callout" and b["tone"] == "error"]
    assert len(errors) == 1 and "customers.csv" in errors[0]["title"]


def test_the_clean_extract_is_ready_and_every_section_is_there(client: TestClient, clean: str) -> None:
    document = blocks(client, clean)
    assert document[0]["state"] in ("ready", "warnings")
    headings = [b["text"] for b in document if b["kind"] == "heading"]
    for section in (
        "Tables received",
        "How the tables link up",
        "History available and needed",
        "Examples of the outcome",
        "Personal details found and masked",
    ):
        assert section in headings


def test_the_report_is_plain_and_shows_no_customer_level_value(client: TestClient, clean: str) -> None:
    html = client.get(f"/pilot/readiness/{clean}").text
    body = html.split("<main", 1)[1]
    assert "@example.invalid" not in body, "a contact from a complaint's notes reached the report"
    headings_and_titles = [
        b.get("title", "") + " " + b.get("text", "")
        for b in blocks(client, clean)
        if b["kind"] in ("heading", "verdict")
    ]
    assert [t for t in headings_and_titles if jargon_in(t)] == []


def test_the_pdf_is_the_same_report(client: TestClient, broken: str) -> None:
    response = client.get(f"/pilot/readiness/{broken}", params={"format": "pdf"})
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
