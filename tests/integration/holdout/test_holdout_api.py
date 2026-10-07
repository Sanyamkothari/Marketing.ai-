"""`GET /holdout` (Viewer) and `PUT /holdout` (Admin, audited) - Plan J M92.

With sign-in on, a Viewer reads the holdout and only an Admin starts a new epoch; every PUT writes one
audit event carrying the ledger's hash before and after, never the salt. Nothing returned ever holds
the salt itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.access_policy import policy_for
from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.holdout.salt import HoldoutLedger, salt_fingerprint
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user

pytestmark = pytest.mark.integration

SALT = "holdout-api-salt-000001"
OTHER_SALT = "holdout-api-salt-000002"


def app_with(tmp_path: Path, salt: str | None = SALT) -> FastAPI:
    app = local_app(tmp_path)
    app.state.settings = app.state.settings.model_copy(
        update={"holdout_salt": None if salt is None else SecretStr(salt)}
    )
    return app


def users(app: FastAPI) -> dict[Role, dict[str, str]]:
    return {role: bearer(app, make_user(app, role.value.lower(), [role])) for role in Role}


def test_the_routes_have_their_policies() -> None:
    read = policy_for("GET", "/holdout")
    write = policy_for("PUT", "/holdout")
    assert read is not None and read.role is Role.VIEWER and read.action == "holdout.read"
    assert write is not None and write.role is Role.ADMIN and write.action == "holdout.update"


def test_a_viewer_reads_the_holdout_and_never_the_salt(tmp_path: Path) -> None:
    app = app_with(tmp_path)
    tokens = users(app)
    with TestClient(app) as client:
        response = client.get("/holdout", headers=tokens[Role.VIEWER])
    assert response.status_code == 200, response.text
    body = response.json()
    assert SALT not in response.text
    assert body["salt_configured"] is True
    assert body["salt_id"] == salt_fingerprint(SALT)[:16]
    assert body["recorded_salt_id"] is None and body["salt_matches"] is None
    assert body["holdouts"] == []
    by_id = {item["use_case_id"]: item for item in body["use_cases"]}
    assert by_id["targeted-advertisement"]["scope"] == "run"
    assert by_id["targeted-advertisement"]["epoch"] is None
    assert by_id["targeted-advertisement"]["explore_fraction"] == 0.0


@pytest.mark.parametrize("role", [Role.VIEWER, Role.ANALYST, Role.APPROVER])
def test_only_an_admin_starts_an_epoch(tmp_path: Path, role: Role) -> None:
    app = app_with(tmp_path)
    tokens = users(app)
    with TestClient(app) as client:
        response = client.put("/holdout", json={"scope": "universal", "fraction": 0.1}, headers=tokens[role])
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "ROLE_REQUIRED"


def test_an_admin_starts_an_epoch_and_it_is_audited(tmp_path: Path) -> None:
    app = app_with(tmp_path)
    tokens = users(app)
    with TestClient(app) as client:
        first = client.put(
            "/holdout",
            json={"scope": "use_case", "use_case_id": "telco-churn", "fraction": 0.1},
            headers=tokens[Role.ADMIN],
        )
        second = client.put(
            "/holdout",
            json={"scope": "use_case", "use_case_id": "telco-churn", "fraction": 0.05},
            headers=tokens[Role.ADMIN],
        )
    assert first.status_code == 200 and second.status_code == 200, second.text
    holdouts = second.json()["holdouts"]
    assert [(item["scope_key"], item["epoch"], item["fraction"]) for item in holdouts] == [
        ("telco-churn", 2, 0.05)
    ]
    assert second.json()["salt_matches"] is True
    events = [
        event for event in audit_log_at(tmp_path).query(AuditQuery()) if event.action == "holdout.update"
    ]
    assert len(events) == 2
    latest = events[-1] if events[-1].details.get("count") == 2 else events[0]
    assert latest.details["reason_code"] == "HOLDOUT_NEW_EPOCH"
    assert latest.details["use_case_id"] == "telco-churn"
    assert latest.object_id == "holdout_epoch:use_case:telco-churn"
    assert latest.before_hash and latest.after_hash and latest.before_hash != latest.after_hash
    assert all(SALT not in event.model_dump_json() for event in events)


def test_rotating_the_salt_moves_every_holdout_to_a_new_epoch(tmp_path: Path) -> None:
    ledger = HoldoutLedger(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME))
    app = app_with(tmp_path, SALT)
    tokens = users(app)
    with TestClient(app) as client:
        assert (
            client.put(
                "/holdout", json={"scope": "universal", "fraction": 0.1}, headers=tokens[Role.ADMIN]
            ).status_code
            == 200
        )
        app.state.settings = app.state.settings.model_copy(update={"holdout_salt": SecretStr(OTHER_SALT)})
        stale = client.get("/holdout", headers=tokens[Role.VIEWER]).json()
        assert stale["salt_matches"] is False
        refused = client.put(
            "/holdout", json={"scope": "universal", "fraction": 0.1}, headers=tokens[Role.ADMIN]
        )
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "HOLDOUT_SALT_CHANGED"
        rotated = client.put(
            "/holdout",
            json={"scope": "universal", "fraction": 0.1, "rotate_salt": True},
            headers=tokens[Role.ADMIN],
        )
    assert rotated.status_code == 200, rotated.text
    assert ledger.fingerprint() == salt_fingerprint(OTHER_SALT)
    assert rotated.json()["holdouts"][0]["epoch"] == 2
    assert rotated.json()["salt_matches"] is True


def test_without_a_salt_no_epoch_can_start(tmp_path: Path) -> None:
    app = app_with(tmp_path, None)
    tokens = users(app)
    with TestClient(app) as client:
        response = client.put(
            "/holdout", json={"scope": "universal", "fraction": 0.1}, headers=tokens[Role.ADMIN]
        )
        view = client.get("/holdout", headers=tokens[Role.VIEWER]).json()
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "HOLDOUT_SALT_MISSING"
    assert view["salt_configured"] is False and view["salt_id"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"scope": "use_case", "fraction": 0.1},
        {"scope": "universal", "use_case_id": "telco-churn", "fraction": 0.1},
        {"scope": "run", "fraction": 0.1},
        {"scope": "universal", "fraction": 0.0},
        {"scope": "universal", "fraction": 0.6},
        {"scope": "universal", "fraction": 0.1, "epoch": 3},
    ],
)
def test_a_malformed_request_is_422(tmp_path: Path, body: dict[str, object]) -> None:
    app = app_with(tmp_path)
    tokens = users(app)
    with TestClient(app) as client:
        response = client.put("/holdout", json=body, headers=tokens[Role.ADMIN])
    assert response.status_code == 422, response.text


def test_an_unknown_use_case_is_404(tmp_path: Path) -> None:
    app = app_with(tmp_path)
    tokens = users(app)
    with TestClient(app) as client:
        response = client.put(
            "/holdout",
            json={"scope": "use_case", "use_case_id": "nope", "fraction": 0.1},
            headers=tokens[Role.ADMIN],
        )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"
