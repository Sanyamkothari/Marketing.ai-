"""Sign-in, `/auth/me` and user management through the API (M46), and what each leaves in the trail."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditQuery
from tests.integration.production.access_support import PASSWORD, audit_log_at, bearer, local_app, make_user

pytestmark = pytest.mark.integration


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return local_app(tmp_path)


def login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def events(tmp_path: Path, **filters: object) -> list:  # type: ignore[type-arg]
    return list(audit_log_at(tmp_path).query(AuditQuery(**filters)))  # type: ignore[arg-type]


def test_login_me_logout(app: FastAPI, tmp_path: Path) -> None:
    user_id = make_user(app, "Asha", [Role.ANALYST])
    client = TestClient(app)
    response = client.post("/auth/login", json={"username": "asha", "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["principal"] == {
        "user_id": user_id,
        "username": "Asha",
        "display_name": "Asha",
        "roles": ["analyst", "viewer"],
        "kind": "user",
    }
    headers = {"Authorization": f"Bearer {body['token']}"}
    me = client.get("/auth/me", headers=headers).json()
    assert me["auth_mode"] == "local" and me["principal"]["user_id"] == user_id
    permissions = {(p["method"], p["path"]): p for p in me["permissions"]}
    approve = permissions[("POST", "/models/{model_id}/approve")]
    assert approve == {
        "method": "POST",
        "path": "/models/{model_id}/approve",
        "action": "models.approve",
        "role": "approver",
        "allowed": False,
        "reason": "Only an Approver can approve a champion.",
    }
    assert permissions[("POST", "/runs")]["allowed"] is True
    assert permissions[("POST", "/runs")]["reason"] is None
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401

    logout, login_event = events(tmp_path, action="auth.")
    assert (login_event.action, login_event.actor_id, login_event.outcome) == (
        "auth.login",
        user_id,
        "success",
    )
    assert login_event.object_id == user_id
    assert (logout.action, logout.actor_id, logout.outcome) == ("auth.logout", user_id, "success")


@pytest.mark.parametrize("case", ["wrong-password", "unknown-user", "disabled"])
def test_a_failed_login_is_one_uniform_401_and_a_failed_event_without_the_username(
    app: FastAPI, tmp_path: Path, case: str
) -> None:
    make_user(app, "admin", [Role.ADMIN])
    user_id = make_user(app, "asha", [Role.VIEWER])
    if case == "disabled":
        app.state.user_store.update_user(user_id, disabled=True)
    username = "no-such-person" if case == "unknown-user" else "asha"
    password = "a-wrong-password" if case == "wrong-password" else PASSWORD
    response = TestClient(app).post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 401
    assert response.json()["detail"] == {
        "code": "BAD_CREDENTIALS",
        "message": "The username or password is not right.",
        "path": None,
    }
    (event,) = events(tmp_path, action="auth.login")
    assert (event.outcome, event.actor_id) == ("failed", "anonymous")
    assert event.object_id == (None if case == "unknown-user" else user_id)
    assert event.details["reason_code"] == ("USER_DISABLED" if case == "disabled" else "BAD_CREDENTIALS")
    row = event.model_dump_json()
    assert username not in row and password not in row


def test_login_with_sign_in_off_is_refused_plainly(tmp_path: Path) -> None:
    response = TestClient(local_app(tmp_path, auth_mode="off")).post(
        "/auth/login", json={"username": "asha", "password": PASSWORD}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AUTH_OFF"


def test_an_admin_manages_users_and_role_changes_are_audited_and_revoke(app: FastAPI, tmp_path: Path) -> None:
    admin = make_user(app, "admin", [Role.ADMIN])
    client = TestClient(app)
    headers = bearer(app, admin)
    created = client.post(
        "/users",
        json={"username": "bo", "password": "x" * 3, "roles": ["analyst"]},
        headers=headers,
    )
    assert created.status_code == 422  # pydantic: the username is too short
    created = client.post(
        "/users",
        json={"username": "bob", "password": "short", "roles": ["analyst"]},
        headers=headers,
    )
    assert created.status_code == 422 and created.json()["detail"]["code"] == "PASSWORD_TOO_SHORT"
    created = client.post(
        "/users",
        json={"username": "bob", "password": PASSWORD, "roles": ["analyst"], "display_name": "Bob"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    bob = created.json()
    assert bob["roles"] == ["analyst"] and "password" not in bob
    duplicate = client.post(
        "/users", json={"username": "BOB", "password": PASSWORD, "roles": ["viewer"]}, headers=headers
    )
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "USERNAME_TAKEN"
    assert [user["username"] for user in client.get("/users", headers=headers).json()["users"]] == [
        "admin",
        "bob",
    ]

    bob_token = login(client, "bob")
    changed = client.patch(f"/users/{bob['user_id']}", json={"roles": ["approver"]}, headers=headers)
    assert changed.status_code == 200 and changed.json()["roles"] == ["approver"]
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {bob_token}"}).status_code == 401

    (roles_change,) = events(tmp_path, action="users.roles_change")
    assert (roles_change.actor_id, roles_change.object_id) == (admin, bob["user_id"])
    assert roles_change.details["roles"] == "approver"
    assert roles_change.details["target_user_id"] == bob["user_id"]
    assert (
        roles_change.before_hash
        and roles_change.after_hash
        and roles_change.before_hash != roles_change.after_hash
    )
    (create,) = events(tmp_path, action="users.create", outcome="success")
    assert create.object_id == bob["user_id"] and create.details["roles"] == "analyst"
    assert PASSWORD not in "".join(event.model_dump_json() for event in events(tmp_path))

    renamed = client.patch(f"/users/{bob['user_id']}", json={"display_name": "Robert"}, headers=headers)
    assert renamed.status_code == 200
    (update,) = events(tmp_path, action="users.update", outcome="success")
    assert update.details["setting"] == "display_name" and "roles" not in update.details


def test_the_last_admin_cannot_be_removed_through_the_api(app: FastAPI, tmp_path: Path) -> None:
    admin = make_user(app, "admin", [Role.ADMIN])
    client = TestClient(app)
    for body in ({"disabled": True}, {"roles": ["viewer"]}):
        response = client.patch(f"/users/{admin}", json=body, headers=bearer(app, admin))
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "LAST_ADMIN"
    refused = events(tmp_path, outcome="failed")
    assert {event.details.get("reason_code") for event in refused} == {"LAST_ADMIN"}
    assert (
        client.patch("/users/u-nobody", json={"disabled": True}, headers=bearer(app, admin)).status_code
        == 404
    )


def test_a_user_changes_their_own_password_only_with_the_current_one(app: FastAPI) -> None:
    user_id = make_user(app, "asha", [Role.VIEWER])
    other = make_user(app, "bob", [Role.VIEWER])
    client = TestClient(app)
    headers = bearer(app, user_id)
    url = f"/users/{user_id}/password"
    missing = client.post(url, json={"password": "a fresh passphrase"}, headers=headers)
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "CURRENT_PASSWORD_REQUIRED"
    wrong = client.post(
        url, json={"password": "a fresh passphrase", "current_password": "nope"}, headers=headers
    )
    assert wrong.status_code == 422 and wrong.json()["detail"]["code"] == "CURRENT_PASSWORD_WRONG"
    someone_else = client.post(
        f"/users/{other}/password", json={"password": "a fresh passphrase"}, headers=headers
    )
    assert someone_else.status_code == 403 and someone_else.json()["detail"]["code"] == "ROLE_REQUIRED"
    ok = client.post(
        url, json={"password": "a fresh passphrase", "current_password": PASSWORD}, headers=headers
    )
    assert ok.status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401  # every session revoked
    login(client, "asha", "a fresh passphrase")


def test_whitespace_is_part_of_a_password(app: FastAPI) -> None:
    admin = make_user(app, "admin", [Role.ADMIN])
    client = TestClient(app)
    padded = "  padded passphrase  "
    created = client.post(
        "/users",
        json={"username": "asha", "password": padded, "roles": ["viewer"]},
        headers=bearer(app, admin),
    )
    assert created.status_code == 201
    login(client, "asha", padded)
    assert (
        client.post("/auth/login", json={"username": "asha", "password": padded.strip()}).status_code == 401
    )


def test_an_admin_resets_a_password_without_the_old_one(app: FastAPI) -> None:
    admin = make_user(app, "admin", [Role.ADMIN])
    user_id = make_user(app, "asha", [Role.VIEWER])
    client = TestClient(app)
    response = client.post(
        f"/users/{user_id}/password", json={"password": "reset by the admin"}, headers=bearer(app, admin)
    )
    assert response.status_code == 204
    login(client, "asha", "reset by the admin")


def test_a_non_admin_cannot_manage_users(app: FastAPI) -> None:
    analyst = make_user(app, "asha", [Role.ANALYST, Role.APPROVER])
    client = TestClient(app)
    for method, url in (("GET", "/users"), ("POST", "/users"), ("PATCH", f"/users/{analyst}")):
        response = client.request(method, url, json={}, headers=bearer(app, analyst))
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "ROLE_REQUIRED"
        assert response.json()["detail"]["message"].startswith("Only an Admin can ")
