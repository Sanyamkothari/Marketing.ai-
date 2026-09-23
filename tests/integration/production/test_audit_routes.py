"""The audit viewer's API (M47): filters and paging, the CSV download, and exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.audit.export import CSV_COLUMNS, EXPORT_DIRECTORY
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user

pytestmark = pytest.mark.integration


@pytest.fixture
def world(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    app = local_app(tmp_path)
    admin = make_user(app, "admin", [Role.ADMIN])
    analyst = make_user(app, "asha", [Role.ANALYST])
    client = TestClient(app)
    admin_headers, analyst_headers = bearer(app, admin), bearer(app, analyst)
    client.post("/runs", json={}, headers=analyst_headers)  # 422, failed
    client.post("/models/m-1/approve", json={}, headers=analyst_headers)  # 403, denied
    client.post("/runs/r-9/cancel", json={}, headers=analyst_headers)  # 404, failed
    return client, {"admin": admin, "analyst": analyst, **{"Authorization": admin_headers["Authorization"]}}


def test_the_viewer_lists_newest_first_with_filters_and_a_total(
    world: tuple[TestClient, dict[str, str]],
) -> None:
    client, ids = world
    headers = {"Authorization": ids["Authorization"]}
    page = client.get("/audit/events", headers=headers).json()
    assert page["total"] == 3 and page["limit"] == 100 and page["offset"] == 0
    assert [event["action"] for event in page["events"]] == ["runs.cancel", "models.approve", "runs.create"]
    denied = client.get("/audit/events", params={"outcome": "denied"}, headers=headers).json()
    assert [event["action"] for event in denied["events"]] == ["models.approve"]
    runs = client.get(
        "/audit/events", params={"action": "runs.", "limit": 1, "offset": 1}, headers=headers
    ).json()
    assert runs["total"] == 2 and [event["action"] for event in runs["events"]] == ["runs.create"]
    mine = client.get("/audit/events", params={"actor_id": ids["analyst"]}, headers=headers).json()
    assert mine["total"] == 3
    by_object = client.get("/audit/events", params={"object_id": "r-9"}, headers=headers).json()
    assert [event["object_type"] for event in by_object["events"]] == ["run"]
    later = client.get("/audit/events", params={"since": "2999-01-01T00:00:00+00:00"}, headers=headers).json()
    assert later["total"] == 0
    naive = client.get("/audit/events", params={"since": "2026-01-01T00:00:00"}, headers=headers)
    assert naive.status_code == 422 and naive.json()["detail"]["code"] == "TIMESTAMP_NEEDS_OFFSET"


def test_reading_the_trail_is_not_itself_a_row_but_downloading_it_is(
    world: tuple[TestClient, dict[str, str]], tmp_path: Path
) -> None:
    client, ids = world
    headers = {"Authorization": ids["Authorization"]}
    client.get("/audit/events", headers=headers)
    assert audit_log_at(tmp_path).count(AuditQuery()) == 3
    response = client.get("/audit/events.csv", params={"action": "runs."}, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.text)))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert [row[CSV_COLUMNS.index("action")] for row in rows[1:]] == ["runs.create", "runs.cancel"]
    (download,) = audit_log_at(tmp_path).query(AuditQuery(action="audit.download"))
    assert (download.actor_id, download.details["count"]) == (ids["admin"], 2)


def test_an_export_writes_json_lines_and_its_event_carries_the_files_hash(
    world: tuple[TestClient, dict[str, str]], tmp_path: Path
) -> None:
    client, ids = world
    response = client.post("/audit/exports", json={}, headers={"Authorization": ids["Authorization"]})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["event_count"] == 3 and result["retain_until"] is None
    body = (tmp_path / EXPORT_DIRECTORY / result["key"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == result["sha256"]
    assert [json.loads(line)["action"] for line in body.decode().splitlines()] == [
        "runs.create",
        "models.approve",
        "runs.cancel",
    ]
    (event,) = audit_log_at(tmp_path).query(AuditQuery(action="audit.export"))
    assert event.after_hash == result["sha256"]
    assert event.details["export_key"] == result["key"] and event.details["count"] == 3


def test_a_broken_sink_is_a_502_and_a_failed_event(
    world: tuple[TestClient, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api.routes.audit as audit_routes

    class Broken:
        def write(self, name: str, body: bytes) -> object:
            raise PermissionError("read-only file system")

    monkeypatch.setattr(audit_routes, "export_sink_for", lambda settings, data_dir: Broken())
    client, ids = world
    response = client.post("/audit/exports", json={}, headers={"Authorization": ids["Authorization"]})
    assert response.status_code == 502 and response.json()["detail"]["code"] == "AUDIT_EXPORT_FAILED"
    (event,) = audit_log_at(tmp_path).query(AuditQuery(action="audit.export"))
    assert (event.outcome, event.details["reason_code"]) == ("failed", "PermissionError")


def test_only_an_admin_reads_the_trail(world: tuple[TestClient, dict[str, str]]) -> None:
    client, ids = world
    headers = bearer(client.app, ids["analyst"])  # type: ignore[arg-type]
    for method, url in (("GET", "/audit/events"), ("GET", "/audit/events.csv"), ("POST", "/audit/exports")):
        response = client.request(method, url, json={} if method == "POST" else None, headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"]["message"].startswith("Only an Admin can ")
