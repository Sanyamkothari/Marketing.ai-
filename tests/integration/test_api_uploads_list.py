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
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.fixtures.make_data import GenerationSpec, generate
from tests.integration.agent.test_api_session import _decide_everything

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
    assert response.json() == {"uploads": [], "total": 0}


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
        "made_by",
        "source_upload_id",
    }
    assert newest["made_by"] == "file"
    assert body["total"] == 2


def test_limit_keeps_the_newest(client: TestClient) -> None:
    for index in range(3):
        _upload(client, f"file-{index}.csv", rows=200)
    ids = [u["upload_id"] for u in client.get("/uploads").json()["uploads"]]
    limited = [u["upload_id"] for u in client.get("/uploads", params={"limit": 2}).json()["uploads"]]
    assert limited == ids[:2]
    assert client.get("/uploads", params={"limit": 0}).status_code == 422


def test_total_counts_every_upload_even_when_the_list_is_cut(client: TestClient) -> None:
    for index in range(3):
        _upload(client, f"file-{index}.csv", rows=200)
    body = client.get("/uploads", params={"limit": 2}).json()
    assert len(body["uploads"]) == 2
    assert body["total"] == 3, "the page words its 'newest N' button from this"


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
    assert entry["made_by"] == "import"


def test_a_file_pulled_from_a_connection_says_which(client: TestClient, data_dir: Path) -> None:
    """Outcomes pulled from a saved connection (`POST /campaigns/{id}/measure`) leave `pull_source.json`, not an import marker."""
    upload = _upload(client, "orders 2026-09-01 to 2026-09-30.csv", rows=200)
    LocalStorage(data_dir).write_text(
        f"uploads/{upload['upload_id']}/pull_source.json",
        '{"connection_id": "cn_pull", "kind": "postgres", "path": null, "prefix": null, "schema_name": "public", '
        '"table": "orders", "window": null, "filtered_by": null, "rows": 200, "unreadable_dates": 0, '
        '"pulled_at": "2026-10-11T00:00:00Z"}',
    )
    (entry,) = client.get("/uploads").json()["uploads"]
    assert entry["from_connection_id"] == "cn_pull"
    assert entry["made_by"] == "pull"


def test_a_renamed_copy_made_through_the_route_says_it_is_a_copy(client: TestClient) -> None:
    """The copy has the same file name as the file sent, so only the marker tells the two rows apart."""
    sent = _upload(client, "billing.csv", rows=300)
    column = next(iter(sent["profile"]["fingerprint"]["columns"]))
    copied = client.post(
        f"/uploads/{sent['upload_id']}/renamed-copy",
        json={"renames": [{"column": column, "new_name": "renamed_by_a_person"}]},
    )
    assert copied.status_code == 201, copied.text
    listed = {u["upload_id"]: u for u in client.get("/uploads").json()["uploads"]}
    assert listed[sent["upload_id"]]["made_by"] == "file"
    assert listed[copied.json()["upload_id"]]["made_by"] == "renamed_copy"
    assert listed[copied.json()["upload_id"]]["file_name"] == "billing.csv"


def test_a_dataset_built_by_guided_setup_says_it_was_built(client: TestClient) -> None:
    response = client.post(
        "/uploads",
        data={"use_case": USE_CASE, "mode": "train"},
        files={"file": ("messy.csv", messy_frame(3_000, seed=7).to_csv(index=False).encode(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]
    started = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    assert started.status_code == 201, started.text
    assert _decide_everything(client, upload_id)["status"] == "ready"
    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    listed = {u["upload_id"]: u["made_by"] for u in client.get("/uploads").json()["uploads"]}
    assert listed[applied.json()["upload_id"]] == "recipe"
    assert listed[upload_id] == "file"
    sources = {u["upload_id"]: u["source_upload_id"] for u in client.get("/uploads").json()["uploads"]}
    assert (
        sources[applied.json()["upload_id"]] == upload_id
    ), "a prepared copy names the upload it was made from"
    assert sources[upload_id] is None


def test_reading_the_list_needs_only_the_viewer_role() -> None:
    policy = policy_for("GET", "/uploads")
    assert policy is not None
    assert policy.role is Role.VIEWER
    assert policy.action == "uploads.list"
