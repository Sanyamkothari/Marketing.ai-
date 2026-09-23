"""Access, audit and privacy-route review fixes, through the real app (DEC-723 … DEC-725, DEC-738).

* an Analyst cannot turn approval off for a run and so crown their own model;
* an Admin cannot grant themselves a role; an Admin's reset of another's password is its own action;
* anonymous requests cannot fill the undeletable audit table, or name their event's request id;
* a prod deployment with sign-in off says so at startup, not at its first request;
* an access request with no client exports the consent history under every client.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.access import ANONYMOUS_EVENTS_PER_MINUTE
from engine.access.roles import Role
from engine.audit.events import AuditQuery
from tests.integration.production.access_support import (
    PASSWORD,
    audit_log_at,
    bearer,
    event_count,
    local_app,
    make_user,
)

pytestmark = pytest.mark.integration

RUN_BODY = {
    "use_case": "telco-churn",
    "mode": "train",
    "upload_id": "up_doesnotexist",
    "primary_key": "customer_id",
}


# --- DEC-723: approval is the Approver's -------------------------------------------------------------
def test_an_analyst_cannot_turn_approval_off_for_a_run(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    client = TestClient(app)
    analyst = bearer(app, make_user(app, "ana", [Role.ANALYST]))
    body = {**RUN_BODY, "overrides": {"governance.approval_required": False}}
    refused = client.post("/runs", json=body, headers=analyst)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "ROLE_REQUIRED"
    assert refused.json()["detail"]["message"] == "Only an Approver can turn off approval for a run."
    (event,) = audit_log_at(tmp_path).query(AuditQuery(action="runs.create"))
    assert (event.outcome, event.details["reason_code"]) == ("denied", "APPROVAL_OVERRIDE_REFUSED")

    nested = {**RUN_BODY, "overrides": {"governance": {"approval_required": False}}}
    assert client.post("/runs", json=nested, headers=analyst).status_code == 403
    stricter = {**RUN_BODY, "overrides": {"governance.approval_required": True}}
    assert client.post("/runs", json=stricter, headers=analyst).json()["detail"]["code"] == "UPLOAD_NOT_FOUND"


def test_an_approver_may_turn_approval_off(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    both = bearer(app, make_user(app, "ana", [Role.ANALYST, Role.APPROVER]))
    body = {**RUN_BODY, "overrides": {"governance.approval_required": False}}
    response = TestClient(app).post("/runs", json=body, headers=both)
    assert response.json()["detail"]["code"] == "UPLOAD_NOT_FOUND", "past the access checks"


# --- DEC-723: an Admin cannot grant themselves a role -----------------------------------------------
def test_an_admin_cannot_grant_themselves_a_role(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    client = TestClient(app)
    admin = make_user(app, "admin1", [Role.ADMIN])
    make_user(app, "admin2", [Role.ADMIN])
    response = client.patch(
        f"/users/{admin}", json={"roles": ["admin", "approver"]}, headers=bearer(app, admin)
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SELF_ROLE_GRANT"
    login = client.post("/auth/login", json={"username": "admin1", "password": PASSWORD}).json()
    assert login["principal"]["roles"] == ["admin", "viewer"]
    refused = audit_log_at(tmp_path).query(AuditQuery(action="users.roles_change"))
    assert [event.details.get("reason_code") for event in refused] == ["SELF_ROLE_GRANT"]


def test_another_admin_may_grant_it_and_an_admin_may_drop_their_own_role(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    client = TestClient(app)
    first = make_user(app, "admin1", [Role.ADMIN, Role.ANALYST])
    second = make_user(app, "admin2", [Role.ADMIN])
    granted = client.patch(
        f"/users/{first}", json={"roles": ["admin", "approver"]}, headers=bearer(app, second)
    )
    assert granted.status_code == 200
    dropped = client.patch(f"/users/{first}", json={"roles": ["admin"]}, headers=bearer(app, first))
    assert dropped.status_code == 200


def test_an_admin_reset_of_another_users_password_is_its_own_audited_action(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    client = TestClient(app)
    admin = make_user(app, "admin", [Role.ADMIN])
    approver = make_user(app, "appro", [Role.APPROVER])
    victim_session = bearer(app, approver)
    response = client.post(
        f"/users/{approver}/password", json={"password": "reset by the admin"}, headers=bearer(app, admin)
    )
    assert response.status_code == 204
    (event,) = audit_log_at(tmp_path).query(AuditQuery(action="users.password_reset"))
    assert (event.actor_id, event.details["target_user_id"], event.details["roles"]) == (
        admin,
        approver,
        "approver",
    )
    assert client.get("/auth/me", headers=victim_session).status_code == 401, "signed out everywhere"


# --- DEC-724: anonymous audit events ------------------------------------------------------------------
def test_anonymous_audit_events_are_rate_limited_per_address(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    client = TestClient(app)
    before = event_count(tmp_path)
    for index in range(ANONYMOUS_EVENTS_PER_MINUTE + 25):
        client.post(f"/no/such/path/{index}", content=b"x")
    assert event_count(tmp_path) - before == ANONYMOUS_EVENTS_PER_MINUTE
    assert app.state.audit_events_dropped == 25
    user = bearer(app, make_user(app, "ana", [Role.ANALYST]))
    client.post("/runs", json=RUN_BODY, headers=user)
    assert (
        event_count(tmp_path) - before == ANONYMOUS_EVENTS_PER_MINUTE + 1
    ), "a signed-in caller is not limited"


def test_an_anonymous_caller_cannot_choose_its_events_request_id(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    client = TestClient(app)
    forged = "forged-request-id-0001"
    for _ in range(2):
        response = client.post(
            "/auth/login", json={"username": "x", "password": "y"}, headers={"X-Request-ID": forged}
        )
        assert response.headers["x-request-id"] == forged
    events = audit_log_at(tmp_path).query(AuditQuery(action="auth.login"))
    assert len(events) == 2
    assert all(event.request_id != forged for event in events)
    assert len({event.request_id for event in events}) == 2
    assert {event.details["client_request_id"] for event in events} == {forged}


def test_a_head_that_answered_405_is_not_audited(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    before = event_count(tmp_path)
    response = TestClient(app).head("/runs/r1/scores.csv")
    assert response.status_code == 405
    assert event_count(tmp_path) == before


# --- DEC-725: the startup error ---------------------------------------------------------------------
def test_a_prod_app_built_with_no_settings_reports_auth_off_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from api.main import create_app

    monkeypatch.setenv("MARKETING_AI_ENV", "prod")
    monkeypatch.setenv("MARKETING_AI_AUTH_MODE", "off")
    monkeypatch.setenv("MARKETING_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MARKETING_AI_CORS_ORIGINS", "https://marketing.example.com")
    app = create_app()
    with caplog.at_level(logging.WARNING, logger="api.access"), TestClient(app) as client:
        started = [record for record in caplog.records if "access control is off" in record.getMessage()]
        assert [record.levelno for record in started] == [logging.ERROR]
        assert client.get("/healthz").status_code == 200


# --- DEC-738: the access export spans every client --------------------------------------------------
def test_an_access_request_with_no_client_exports_every_clients_consent(tmp_path: Path) -> None:
    app = local_app(tmp_path, client_id="acme")
    client = TestClient(app)
    admin = bearer(app, make_user(app, "admin", [Role.ADMIN]))
    person = "C-555-PRIVATE"
    for client_id in (None, "c_acme_1"):
        body = {"principal_id": person, "purpose": "marketing_communication", "status": "granted"}
        if client_id:
            body["client_id"] = client_id
        assert client.post("/privacy/consent", json=body, headers=admin).status_code == 201
    response = client.post("/privacy/access-requests", json={"principal_id": person}, headers=admin)
    assert response.status_code == 200
    history = json.loads(zipfile.ZipFile(io.BytesIO(response.content)).read("consent_history.json"))
    assert sorted(record["client_id"] for record in history) == ["acme", "c_acme_1"]
