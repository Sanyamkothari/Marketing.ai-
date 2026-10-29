"""Data Doctor's cases through the API (Plan O M231, DEC-1801).

Acceptance (Plan O §M231): `POST /cases`, `GET /cases/{id}`, `POST /cases/{id}/files`, `GET /cases/{id}/timeline`;
two writers of one version, the second `409 CASE_VERSION_CONFLICT`; another client's case `404 CASE_NOT_FOUND`;
an existing Guided session opened as a read-only case; with the switch off, no route and an unchanged API.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.access_policy import all_policies
from api.main import create_app
from api.routes import cases as cases_routes
from engine.access.roles import Role
from engine.doctor import case as doctor_case
from engine.storage import LocalStorage, upload_key
from tests.integration.production.access_support import bearer, local_app, make_user

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ROUTES = {
    ("POST", "/cases"),
    ("GET", "/cases/{case_id}"),
    ("POST", "/cases/{case_id}/files"),
    ("GET", "/cases/{case_id}/timeline"),
}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def bills_csv(seed: int = 0) -> bytes:
    lines = ["customer_id,invoice_date,amount"]
    lines += [f"C{(i + seed) % 150},2025-{1 + i % 12:02d}-{1 + i % 27:02d},{100 + i}" for i in range(300)]
    return ("\n".join(lines) + "\n").encode()


def upload(client: TestClient, name: str = "bills.csv", seed: int = 0) -> str:
    response = client.post(
        "/uploads",
        files={"file": (name, bills_csv(seed), "text/csv")},
        data={"use_case": "payment-propensity", "mode": "train"},
    )
    assert response.status_code == 201, response.text
    upload_id: str = response.json()["upload_id"]
    return upload_id


def code_of(response: Any) -> str:
    detail: dict[str, Any] = response.json()["detail"]
    return str(detail["code"])


def test_open_read_add_and_follow_a_case(client: TestClient) -> None:
    first, second = upload(client, "customers.csv"), upload(client, "bills.csv", seed=3)
    created = client.post("/cases", json={"title": "Win back", "upload_ids": [first]})
    assert created.status_code == 201, created.text
    case = created.json()
    assert case["version"] == 1 and case["client_id"] == "default" and case["state"] == "intake"
    assert [f["upload_id"] for f in case["files"]] == [first]
    assert case["files"][0]["file_name"] == "customers.csv" and case["created_by"] == "local-operator"

    got = client.get(f"/cases/{case['case_id']}")
    assert got.status_code == 200 and got.json() == case

    added = client.post(f"/cases/{case['case_id']}/files", json={"version": 1, "upload_id": second})
    assert added.status_code == 200, added.text
    assert added.json()["version"] > 1, "the visit goes on after the file is added"
    assert [f["upload_id"] for f in added.json()["files"]] == [first, second]

    timeline = client.get(f"/cases/{case['case_id']}/timeline")
    assert timeline.status_code == 200
    events = timeline.json()["events"]
    assert [(e["kind"], e["upload_id"], e["version"]) for e in events][:3] == [
        ("case.created", None, 1),
        ("file.added", first, 1),
        ("file.added", second, 2),
    ], "the first events are the case's own; the visit's follow"
    assert events[0]["actor"] == events[2]["actor"] == "local-operator"


def test_two_writers_of_one_version_the_second_is_refused(client: TestClient) -> None:
    case = client.post("/cases", json={}).json()
    a, b = upload(client, "a.csv"), upload(client, "b.csv", seed=5)
    first = client.post(f"/cases/{case['case_id']}/files", json={"version": 1, "upload_id": a})
    second = client.post(f"/cases/{case['case_id']}/files", json={"version": 1, "upload_id": b})
    assert first.status_code == 200
    assert second.status_code == 409 and code_of(second) == "CASE_VERSION_CONFLICT"
    now = client.get(f"/cases/{case['case_id']}").json()
    assert now["version"] > 1 and [f["upload_id"] for f in now["files"]] == [a], "nothing was overwritten"


def test_another_clients_case_is_not_found(client: TestClient) -> None:
    case = client.post("/cases", json={"client_id": "c_acme_1"}).json()
    assert case["client_id"] == "c_acme_1"
    own = client.get(f"/cases/{case['case_id']}", params={"client_id": "c_acme_1"})
    assert own.status_code == 200
    for other in ({"client_id": "c_other_2"}, {}):
        for response in (
            client.get(f"/cases/{case['case_id']}", params=other),
            client.get(f"/cases/{case['case_id']}/timeline", params=other),
            client.post(f"/cases/{case['case_id']}/files", json={**other, "version": 1, "upload_id": "u_x"}),
        ):
            assert response.status_code == 404 and code_of(response) == "CASE_NOT_FOUND"
            assert case["case_id"] not in response.text
    assert client.get("/cases/case_ffffffffffffffff").status_code == 404


def test_a_bad_client_id_or_a_missing_upload_is_refused_plainly(client: TestClient) -> None:
    assert client.post("/cases", json={"client_id": "../x"}).status_code == 422
    assert client.get("/cases/case_ffffffffffffffff", params={"client_id": "A/B"}).status_code == 422
    missing = client.post("/cases", json={"upload_ids": ["u_000000000999"]})
    assert missing.status_code == 404 and code_of(missing) == "UPLOAD_NOT_FOUND"
    case = client.post("/cases", json={}).json()
    gone = client.post(f"/cases/{case['case_id']}/files", json={"version": 1, "upload_id": "u_000000000999"})
    assert gone.status_code == 404 and code_of(gone) == "UPLOAD_NOT_FOUND"
    assert client.get(f"/cases/{case['case_id']}").json()["version"] == 1


def _session(upload_id: str) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "upload_id": upload_id,
        "use_case_id": "payment-propensity",
        "mode": "train",
        "agent_name": "Helper",
        "status": "ready",
        "tool_results": [
            {"evidence_id": "ev1", "tool": "profile", "result": {"rows": 300}, "created_at": T0.isoformat()}
        ],
        "proposals": [
            {
                "proposal_id": "p1",
                "kind": "recipe_step",
                "title": "Read amount as a number",
                "reason": "Text digits",
                "step": {"order": 1, "kind": "parse_number", "column": "amount", "params": {}},
                "evidence_ids": ["ev1"],
                "confidence": "sure",
                "state": "accepted",
                "decided_by": "user",
            }
        ],
        "created_at": T0.isoformat(),
        "updated_at": T0.isoformat(),
    }


def test_an_existing_guided_session_opens_as_a_read_only_case(client: TestClient, data_dir: Path) -> None:
    upload_id = upload(client, "bills.csv")
    storage = LocalStorage(data_dir)
    session_key = upload_key(upload_id, "agent/agent_session.json")
    storage.write_text(session_key, json.dumps(_session(upload_id)))
    before = sorted(storage.list_keys())

    opened = client.get(f"/cases/guided_{upload_id}")
    assert opened.status_code == 200, opened.text
    case = opened.json()
    assert case["read_only"] is True and case["source"] == "guided_session" and case["state"] == "preview"
    assert [f["upload_id"] for f in case["files"]] == [upload_id] and case["files"][0][
        "file_name"
    ] == "bills.csv"
    assert [(i["kind"], i["state"]) for i in case["items"]] == [("parse_number", "accepted")]

    refused = client.post(f"/cases/guided_{upload_id}/files", json={"version": 1, "upload_id": upload_id})
    assert refused.status_code == 409 and code_of(refused) == "CASE_READ_ONLY"
    assert client.get(f"/cases/guided_{upload_id}/timeline").json()["events"] == []
    assert sorted(storage.list_keys()) == before, "opening older work writes nothing"
    other = client.get(f"/cases/guided_{upload_id}", params={"client_id": "c_acme_1"})
    assert other.status_code == 404, "uploads are the deployment's own: no other client sees them as cases"


def test_a_guided_session_of_a_file_with_a_long_name_opens(client: TestClient, data_dir: Path) -> None:
    """Review M231: an upload's name has no length limit; the case's title keeps its first 200 characters."""
    name = "monthly-bills-" + "x" * 220 + ".csv"
    upload_id = upload(client, name)
    LocalStorage(data_dir).write_text(
        upload_key(upload_id, "agent/agent_session.json"), json.dumps(_session(upload_id))
    )
    opened = client.get(f"/cases/guided_{upload_id}")
    assert opened.status_code == 200, opened.text
    assert opened.json()["title"] == name[:200] and opened.json()["files"][0]["file_name"] == name
    assert client.get(f"/cases/guided_{upload_id}/timeline").status_code == 200


def test_a_guided_session_that_cannot_be_shown_as_a_case_answers_a_code(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M231: an adapter that cannot map a session refuses with a code, never a 500."""
    upload_id = upload(client, "bills.csv")
    LocalStorage(data_dir).write_text(
        upload_key(upload_id, "agent/agent_session.json"), json.dumps(_session(upload_id))
    )

    def unmappable(*args: object, **kwargs: object) -> object:
        return doctor_case.Case.model_validate({})

    monkeypatch.setattr(cases_routes, "case_from_session", unmappable)
    for response in (
        client.get(f"/cases/guided_{upload_id}"),
        client.get(f"/cases/guided_{upload_id}/timeline"),
        client.post(f"/cases/guided_{upload_id}/files", json={"version": 1, "upload_id": upload_id}),
    ):
        assert response.status_code == 409 and code_of(response) == "CASE_UNREADABLE", response.text


def test_an_unreadable_stored_case_answers_a_code(client: TestClient, data_dir: Path) -> None:
    case_id = client.post("/cases", json={}).json()["case_id"]
    storage = LocalStorage(data_dir)
    key = doctor_case.case_key("default", case_id)
    document = json.loads(storage.read_bytes(key))
    document["questions"] = [{"question_id": "", "text": "?"}]
    storage.write_text(key, json.dumps(document))
    for response in (
        client.get(f"/cases/{case_id}"),
        client.get(f"/cases/{case_id}/timeline"),
        client.post(f"/cases/{case_id}/files", json={"version": 1, "upload_id": "u_x"}),
    ):
        assert response.status_code == 409 and code_of(response) == "CASE_UNREADABLE", response.text


def test_the_routes_have_their_policies(client: TestClient) -> None:
    policies = all_policies()
    assert set(policies) >= ROUTES
    assert policies[("POST", "/cases")].role is Role.ANALYST
    assert policies[("POST", "/cases/{case_id}/files")].role is Role.ANALYST
    assert policies[("GET", "/cases/{case_id}")].role is Role.VIEWER
    assert policies[("GET", "/cases/{case_id}/timeline")].role is Role.VIEWER
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert {"/cases", "/cases/{case_id}", "/cases/{case_id}/files", "/cases/{case_id}/timeline"} <= set(paths)


def test_a_viewer_reads_a_case_but_cannot_change_it(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    viewer = make_user(app, "vera", [Role.VIEWER])
    analyst = make_user(app, "asha", [Role.ANALYST])
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/cases", json={}).status_code == 401
        refused = client.post("/cases", json={}, headers=bearer(app, viewer))
        assert refused.status_code == 403 and code_of(refused) == "ROLE_REQUIRED"
        created = client.post("/cases", json={}, headers=bearer(app, analyst))
        assert created.status_code == 201 and created.json()["created_by"] == analyst
        case_id = created.json()["case_id"]
        assert client.get(f"/cases/{case_id}", headers=bearer(app, viewer)).status_code == 200
        events = client.get(f"/cases/{case_id}/timeline", headers=bearer(app, viewer)).json()["events"]
        assert [e["actor"] for e in events] == [analyst], "the timeline records the user id"
