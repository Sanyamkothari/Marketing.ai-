"""`GET /holdout` (Viewer) and `PUT /holdout` (Admin, audited) - Plan J M92.

With sign-in on, a Viewer reads the holdout and only an Admin starts a new epoch; every PUT writes one
audit event carrying the ledger's hash before and after, never the salt. Nothing returned ever holds
the salt itself.
"""

from __future__ import annotations

import shutil
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
from engine.settings import DEFAULT_CONFIG_DIR
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


def test_rotating_to_the_recorded_salt_is_refused_and_audited_as_refused(tmp_path: Path) -> None:
    app = app_with(tmp_path, SALT)
    tokens = users(app)
    with TestClient(app) as client:
        started = client.put(
            "/holdout", json={"scope": "universal", "fraction": 0.1}, headers=tokens[Role.ADMIN]
        )
        assert started.status_code == 200, started.text
        refused = client.put(
            "/holdout",
            json={"scope": "universal", "fraction": 0.1, "rotate_salt": True},
            headers=tokens[Role.ADMIN],
        )
        view = client.get("/holdout", headers=tokens[Role.VIEWER]).json()
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "HOLDOUT_SALT_UNCHANGED"
    assert [item["epoch"] for item in view["holdouts"]] == [1], "nothing was reshuffled"
    events = [
        event for event in audit_log_at(tmp_path).query(AuditQuery()) if event.action == "holdout.update"
    ]
    assert all(event.details.get("reason_code") != "HOLDOUT_SALT_ROTATED" for event in events)


def _config_root_with_universal(tmp_path: Path, fraction: float) -> Path:
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_DIR, root)
    path = root / "use_cases" / "targeted_advertisement.yaml"
    text = path.read_text(encoding="utf-8")
    assert "\nactions:\n" in text and "holdout:" not in text
    path.write_text(
        text.replace(
            "\nactions:\n", f"\nactions:\n  holdout: {{scope: universal, fraction: {fraction}}}\n", 1
        ),
        encoding="utf-8",
    )
    return root


def test_a_universal_epoch_must_be_the_share_every_universal_use_case_declares(tmp_path: Path) -> None:
    app = app_with(tmp_path)
    app.state.config_root = _config_root_with_universal(tmp_path, 0.05)
    tokens = users(app)
    with TestClient(app) as client:
        refused = client.put(
            "/holdout", json={"scope": "universal", "fraction": 0.1}, headers=tokens[Role.ADMIN]
        )
        accepted = client.put(
            "/holdout", json={"scope": "universal", "fraction": 0.05}, headers=tokens[Role.ADMIN]
        )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "HOLDOUT_FRACTION_MISMATCH"
    assert "targeted-advertisement" in refused.json()["detail"]["message"]
    assert accepted.status_code == 200, accepted.text
    by_id = {item["use_case_id"]: item for item in accepted.json()["use_cases"]}
    assert by_id["targeted-advertisement"]["scope"] == "universal"
    assert (by_id["targeted-advertisement"]["epoch"], by_id["targeted-advertisement"]["epoch_fraction"]) == (
        1,
        0.05,
    )
    assert by_id["telco-churn"]["epoch"] is None and by_id["telco-churn"]["epoch_fraction"] is None


@pytest.mark.parametrize("reserved", ["universal", "explore"])
def test_a_reserved_use_case_id_cannot_take_its_own_holdout(tmp_path: Path, reserved: str) -> None:
    app = app_with(tmp_path)
    tokens = users(app)
    with TestClient(app) as client:
        response = client.put(
            "/holdout",
            json={"scope": "use_case", "use_case_id": reserved, "fraction": 0.1},
            headers=tokens[Role.ADMIN],
        )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "HOLDOUT_SCOPE_RESERVED"
