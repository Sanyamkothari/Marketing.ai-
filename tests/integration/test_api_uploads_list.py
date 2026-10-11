"""`GET /uploads`: the uploaded files, newest first (Plan M, M179, DEC-1609).

The Data page lists connections and uploaded files in one place. Connections had a list; uploads had only
`POST /uploads` and a profile by id, so the page could not show "the files you uploaded". This is the additive read:
one summary per `upload.json`, nothing about the file's content, readable by a Viewer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.access_policy import policy_for
from api.main import create_app
from engine.access.roles import Role
from engine.storage import LocalStorage
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def _upload(client: TestClient, name: str, rows: int = 300) -> dict[str, Any]:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=rows, variant="clean"))
    response = client.post(
        "/uploads",
        data={"use_case": USE_CASE, "mode": "train"},
        files={"file": (name, frame.to_csv(index=False).encode(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def test_no_uploads_is_an_empty_list_not_an_error(client: TestClient) -> None:
    response = client.get("/uploads")
    assert response.status_code == 200
    assert response.json() == {"uploads": []}


def test_every_upload_is_listed_newest_first_with_what_the_data_page_shows(client: TestClient) -> None:
    first = _upload(client, "customers-march.csv", rows=300)
    second = _upload(client, "customers-april.csv", rows=400)
    body = client.get("/uploads").json()
    listed = body["uploads"]
    assert [u["upload_id"] for u in listed] == [second["upload_id"], first["upload_id"]]
    newest = listed[0]
    assert newest["file_name"] == "customers-april.csv"
    assert newest["file_format"] == "csv"
    assert newest["row_count"] == 400
    assert newest["column_count"] == second["profile"]["column_count"]
    assert newest["file_size_bytes"] > 0
    assert newest["use_case_id"] == USE_CASE
    assert newest["created_at"]
    assert newest["from_connection_id"] is None
    # The summary names no content: no profile, no key into storage, no fingerprint.
    assert set(newest) == {
        "upload_id",
        "file_name",
        "file_format",
        "file_size_bytes",
        "row_count",
        "column_count",
        "use_case_id",
        "mode",
        "synthetic",
        "created_at",
        "from_connection_id",
    }


def test_limit_keeps_the_newest(client: TestClient) -> None:
    for index in range(3):
        _upload(client, f"file-{index}.csv", rows=200)
    ids = [u["upload_id"] for u in client.get("/uploads").json()["uploads"]]
    limited = [u["upload_id"] for u in client.get("/uploads", params={"limit": 2}).json()["uploads"]]
    assert limited == ids[:2]
    assert client.get("/uploads", params={"limit": 0}).status_code == 422


def test_an_unreadable_upload_record_is_skipped_not_fatal(client: TestClient, data_dir: Path) -> None:
    kept = _upload(client, "good.csv", rows=200)
    broken = _upload(client, "broken.csv", rows=200)
    LocalStorage(data_dir).write_text(f"uploads/{broken['upload_id']}/upload.json", "{ not json")
    listed = client.get("/uploads").json()["uploads"]
    assert [u["upload_id"] for u in listed] == [kept["upload_id"]]


def test_a_file_imported_from_a_connection_says_which(client: TestClient, data_dir: Path) -> None:
    upload = _upload(client, "from-a-store.csv", rows=200)
    LocalStorage(data_dir).write_text(
        f"uploads/{upload['upload_id']}/connection_source.json",
        '{"connection_id": "cn_abc", "kind": "s3", "path": "exports/history.csv", '
        '"schema_name": null, "table": null, "imported_at": "2026-10-11T00:00:00Z"}',
    )
    (entry,) = client.get("/uploads").json()["uploads"]
    assert entry["from_connection_id"] == "cn_abc"


def test_reading_the_list_needs_only_the_viewer_role() -> None:
    policy = policy_for("GET", "/uploads")
    assert policy is not None
    assert policy.role is Role.VIEWER
    assert policy.action == "uploads.list"
