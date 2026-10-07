"""The readiness report's planning sections on a real build, through the API (Plan J M93).

One client is onboarded from the clean raw tables exactly as the demo seed does it, and its dataset
built; `GET /pilot/readiness/{id}` must then carry "The outcome, checked" and "Can we measure it?",
in plain words, and with `treatment_column` a "Past campaigns" section - without the verdict moving.
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
from tests.fixtures.raw.make_raw import make_raw

pytestmark = pytest.mark.integration

CUSTOMERS = 1_100
USAGE_ROWS = 4_000


@pytest.fixture(scope="module")
def client(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path_factory.mktemp("data"))) as test:
        yield test


@pytest.fixture(scope="module")
def dataset_id(client: TestClient, tmp_path_factory: pytest.TempPathFactory) -> str:
    tables = make_raw(tmp_path_factory.mktemp("clean"), customers=CUSTOMERS, usage_rows=USAGE_ROWS)
    client_id = client.post("/clients", json={"name": "Clean Company", "industry": "other"}).json()[
        "client_id"
    ]
    spec_id, _, _ = _onboard(client, client_id, {table: getattr(tables, table) for table in TABLE_ROLES})
    built, _ = _build(client, client_id, spec_id, "train")
    return str(built)


def blocks(client: TestClient, dataset_id: str, **params: str) -> list[dict[str, Any]]:
    response = client.get(f"/pilot/readiness/{dataset_id}", params={"format": "json", **params})
    assert response.status_code == 200, response.text
    return list(response.json()["blocks"])


def strings(found: list[dict[str, Any]]) -> list[str]:
    said: list[str] = []
    for block in found:
        for key in ("text", "title", "caption", "empty_text"):
            if isinstance(block.get(key), str):
                said.append(block[key])
        for key in ("rows", "items", "columns"):
            for row in block.get(key) or ():
                said.extend(
                    cell for cell in (row if isinstance(row, list) else [row]) if isinstance(cell, str)
                )
    return said


def test_the_report_carries_the_planning_sections(client: TestClient, dataset_id: str) -> None:
    document = blocks(client, dataset_id)
    headings = [b["text"] for b in document if b["kind"] == "heading"]
    assert "The outcome, checked" in headings and "Can we measure it?" in headings
    assert "Past campaigns" not in headings
    start = headings.index("Can we measure it?")
    assert headings.index("The outcome, checked") < start
    first = next(i for i, b in enumerate(document) if b.get("text") == "Can we measure it?")
    measure = next(b for b in document[first:] if b["kind"] == "table")
    assert [row[0] for row in measure["rows"]] == ["3%", "5%", "10%", "15%"]
    said = " ".join(strings(document))
    assert "From this dataset:" in said
    assert "Future-data check" in said


def test_a_named_treatment_column_adds_past_campaigns_and_keeps_the_verdict(
    client: TestClient, dataset_id: str
) -> None:
    plain = blocks(client, dataset_id)
    with_column = blocks(client, dataset_id, treatment_column="no_such_column")
    assert with_column[0] == plain[0], "naming a column moved the verdict"
    headings = [b["text"] for b in with_column if b["kind"] == "heading"]
    assert "Past campaigns" in headings
    rows = next(b for b in with_column if b["kind"] == "key_values" and b["rows"][0][0] == "Column")["rows"]
    assert dict(map(tuple, rows))["How customers were chosen"] == "Not known"


def test_nothing_the_planning_sections_say_is_jargon(client: TestClient, dataset_id: str) -> None:
    document = blocks(client, dataset_id, treatment_column="no_such_column")
    headings = [i for i, b in enumerate(document) if b["kind"] == "heading"]
    planning: list[dict[str, Any]] = []
    for position, index in enumerate(headings):
        if document[index]["text"] in ("The outcome, checked", "Can we measure it?", "Past campaigns"):
            end = headings[position + 1] if position + 1 < len(headings) else len(document)
            planning += document[index:end]
    said = strings(planning)
    assert len(said) > 10
    assert [text for text in said if jargon_in(text)] == []
