"""Plan M M180 (DEC-1610): "Show technical details", remembered per signed-in person.

`GET /auth/me` carries the caller's `preferences` (null with sign-in off) and `PUT /auth/me/preferences` saves
`show_technical` for the caller only. Any role may set their own; the choice never changes a permission.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from api.access_policy import policy_for
from engine.access.preferences import SqlPreferenceStore, UserPreferences, preference_key
from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.platform_db import PLATFORM_DB_FILENAME, PlatformSettingRow, sqlite_engine
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user

pytestmark = pytest.mark.integration

PUT = "/auth/me/preferences"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return local_app(tmp_path)


def permissions(me: dict) -> dict[tuple[str, str], bool]:  # type: ignore[type-arg]
    return {(p["method"], p["path"]): p["allowed"] for p in me["permissions"]}


def test_a_new_person_has_not_chosen_and_me_says_so(app: FastAPI) -> None:
    user = make_user(app, "asha", [Role.VIEWER])
    me = TestClient(app).get("/auth/me", headers=bearer(app, user))
    assert me.status_code == 200
    assert me.json()["preferences"] == {"show_technical": None}


@pytest.mark.parametrize("role", [Role.VIEWER, Role.ANALYST, Role.APPROVER, Role.ADMIN])
def test_any_signed_in_role_sets_their_own_and_it_is_remembered(app: FastAPI, role: Role) -> None:
    user = make_user(app, "asha", [role])
    client = TestClient(app)
    headers = bearer(app, user)
    before = permissions(client.get("/auth/me", headers=headers).json())
    for value in (True, False, True):
        answer = client.put(PUT, json={"show_technical": value}, headers=headers)
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"show_technical": value}
        me = client.get("/auth/me", headers=headers).json()
        assert me["preferences"] == {"show_technical": value}
        # The switch never grants or removes a permission (Plan M §1 M3: roles are unchanged).
        assert permissions(me) == before
    assert before[("PUT", PUT)] is True


def test_another_person_is_unaffected_and_a_new_sign_in_keeps_the_choice(app: FastAPI) -> None:
    asha = make_user(app, "asha", [Role.ANALYST])
    bo = make_user(app, "bob", [Role.VIEWER])
    client = TestClient(app)
    assert client.put(PUT, json={"show_technical": True}, headers=bearer(app, asha)).status_code == 200
    assert client.get("/auth/me", headers=bearer(app, bo)).json()["preferences"] == {"show_technical": None}
    assert client.put(PUT, json={"show_technical": False}, headers=bearer(app, bo)).status_code == 200
    # A second sign-in (another browser) reads the same choice: it is kept on the server, per person.
    assert client.get("/auth/me", headers=bearer(app, asha)).json()["preferences"] == {"show_technical": True}
    assert client.get("/auth/me", headers=bearer(app, bo)).json()["preferences"] == {"show_technical": False}


def test_signed_out_is_refused_and_a_bad_body_is_a_422(app: FastAPI) -> None:
    client = TestClient(app)
    assert client.put(PUT, json={"show_technical": True}).status_code == 401
    user = make_user(app, "asha", [Role.VIEWER])
    headers = bearer(app, user)
    for body in (
        {},
        {"show_technical": "yes"},
        {"show_technical": 1},
        {"show_technical": True, "user_id": "x"},
    ):
        assert client.put(PUT, json=body, headers=headers).status_code == 422, body
    assert client.get("/auth/me", headers=headers).json()["preferences"] == {"show_technical": None}


def test_with_sign_in_off_nothing_is_stored_and_the_browser_keeps_it(tmp_path: Path) -> None:
    client = TestClient(local_app(tmp_path, auth_mode="off"))
    me = client.get("/auth/me").json()
    assert me["auth_mode"] == "off" and me["preferences"] is None
    answer = client.put(PUT, json={"show_technical": True})
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "AUTH_OFF"
    assert client.get("/auth/me").json()["preferences"] is None


def test_the_route_has_a_policy_any_role_passes_and_is_audited(app: FastAPI, tmp_path: Path) -> None:
    policy = policy_for("PUT", PUT)
    assert policy is not None and policy.role == Role.VIEWER and policy.action == "auth.preferences_update"
    user = make_user(app, "asha", [Role.VIEWER])
    assert (
        TestClient(app).put(PUT, json={"show_technical": True}, headers=bearer(app, user)).status_code == 200
    )
    (event,) = audit_log_at(tmp_path).query(AuditQuery(action="auth.preferences_update"))
    assert (event.actor_id, event.object_id, event.outcome) == (user, user, "success")


def test_the_store_needs_no_new_table_and_reads_a_damaged_value_as_not_chosen(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    store = SqlPreferenceStore(engine)
    assert store.get("u1") == UserPreferences()
    assert store.set_show_technical("u1", True) == UserPreferences(show_technical=True)
    assert store.get("u1").show_technical is True and store.get("u2").show_technical is None
    with Session(engine) as session:
        row = session.get(PlatformSettingRow, preference_key("u1"))
        assert row is not None and json.loads(row.value) == {"show_technical": True}
        row.value = "{not json"
        session.add(row)
        session.commit()
    assert store.get("u1") == UserPreferences()
    assert store.set_show_technical("u1", False).show_technical is False
