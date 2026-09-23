"""Every route, allowed and denied (plan section 4: "Every route has a role test").

With `auth_mode=local`, for every (method, path) the live app serves:

* no token is **401 `AUTH_REQUIRED`** with `WWW-Authenticate: Bearer` (except the public routes);
* a principal holding every role *except* the required one is **403 `ROLE_REQUIRED`**, with the
  sentence the UI shows - "every role except" is the strongest way to prove no other role implies
  it, which is the separation of duties of DEC-703 (Viewer is implied by all roles, so a
  Viewer-route has no such principal and is covered by the 401 case);
* a principal holding only the required role is **not refused by access control**: the route may
  still answer 404 or 422 for the made-up ids and empty bodies used here, and some routes answer a
  403 of their own (the AWS connection's `CONNECTION_LOCKED`), which is why "refused by access
  control" is read from the error code and not from the status alone.

The route list is read from the app, so a route added later is covered without editing this file.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from tests.integration.production.access_support import (
    ACCESS_CODES,
    LiveRoute,
    bearer,
    live_routes,
    local_app,
    make_user,
)

pytestmark = pytest.mark.integration

ROUTES = live_routes()
PROTECTED = [route for route in ROUTES if route.policy is not None and route.policy.role is not None]
EXCLUSIVE = [
    route for route in PROTECTED if route.policy is not None and route.policy.role is not Role.VIEWER
]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[TestClient, dict[str, str]]:
    """One app with sign-in on, a user per role and a user per "every role but this one"."""
    tmp_path = tmp_path_factory.mktemp("access")
    app = local_app(tmp_path)
    users: dict[str, str] = {}
    for role in Role:
        users[f"only-{role.value}"] = make_user(app, f"only-{role.value}", [role])
        users[f"all-but-{role.value}"] = make_user(
            app, f"all-but-{role.value}", [r for r in Role if r is not role]
        )
    client = TestClient(app, raise_server_exceptions=False)
    client.app_state = app  # type: ignore[attr-defined]
    return client, users


def _code(response: object) -> str | None:
    try:
        detail = response.json().get("detail")  # type: ignore[attr-defined]
    except ValueError:
        return None
    return detail.get("code") if isinstance(detail, dict) else None


@pytest.mark.parametrize("route", PROTECTED, ids=lambda route: route.id)
def test_no_token_is_401_with_a_bearer_challenge(
    world: tuple[TestClient, dict[str, str]], route: LiveRoute
) -> None:
    client, _ = world
    response = route.call(client)
    assert response.status_code == 401, response.text
    assert _code(response) == "AUTH_REQUIRED"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("route", PROTECTED, ids=lambda route: route.id)
def test_a_garbage_token_is_401(world: tuple[TestClient, dict[str, str]], route: LiveRoute) -> None:
    client, _ = world
    response = route.call(client, {"Authorization": "Bearer not-a-session"})
    assert response.status_code == 401
    assert _code(response) == "AUTH_REQUIRED"


@pytest.mark.parametrize("route", EXCLUSIVE, ids=lambda route: route.id)
def test_every_other_role_together_is_refused_with_the_reason(
    world: tuple[TestClient, dict[str, str]], route: LiveRoute
) -> None:
    client, users = world
    assert route.policy is not None and route.policy.role is not None
    required = route.policy.role
    response = route.call(client, bearer(client.app_state, users[f"all-but-{required.value}"]))  # type: ignore[attr-defined]
    assert response.status_code == 403, response.text
    assert _code(response) == "ROLE_REQUIRED"
    message = response.json()["detail"]["message"]
    assert message.startswith("Only ") and required.value.capitalize() in message


@pytest.mark.parametrize("route", PROTECTED, ids=lambda route: route.id)
def test_the_required_role_alone_is_let_through(
    world: tuple[TestClient, dict[str, str]], route: LiveRoute
) -> None:
    client, users = world
    assert route.policy is not None and route.policy.role is not None
    response = route.call(client, bearer(client.app_state, users[f"only-{route.policy.role.value}"]))  # type: ignore[attr-defined]
    assert response.status_code != 401, response.text
    assert _code(response) not in ACCESS_CODES, response.text


def test_the_approver_message_is_the_one_the_plan_quotes(world: tuple[TestClient, dict[str, str]]) -> None:
    client, users = world
    response = client.post(
        "/models/m-1/approve", json={}, headers=bearer(client.app_state, users["only-analyst"])  # type: ignore[attr-defined]
    )
    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "ROLE_REQUIRED",
        "message": "Only an Approver can approve a champion.",
        "path": None,
    }


def test_an_admin_does_not_thereby_approve(world: tuple[TestClient, dict[str, str]]) -> None:
    """DEC-703: Admin manages users and settings; it is not a super-role."""
    client, users = world
    headers = bearer(client.app_state, users["only-admin"])  # type: ignore[attr-defined]
    assert _code(client.post("/models/m-1/approve", json={}, headers=headers)) == "ROLE_REQUIRED"
    assert _code(client.post("/runs", json={}, headers=headers)) == "ROLE_REQUIRED"


def test_public_routes_answer_without_a_token(world: tuple[TestClient, dict[str, str]]) -> None:
    client, _ = world
    assert client.get("/healthz").status_code == 200
    assert client.post("/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401


def test_auth_off_is_the_local_operator_everywhere(tmp_path: Path) -> None:
    client = TestClient(local_app(tmp_path, auth_mode="off"))
    assert client.get("/runs").status_code == 200
    me = client.get("/auth/me").json()
    assert me["auth_mode"] == "off"
    assert me["principal"]["user_id"] == "local-operator"
    assert all(permission["allowed"] for permission in me["permissions"])


def test_auth_off_on_prod_fails_closed_but_stays_up(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(
        local_app(tmp_path, auth_mode="off", env="prod", cors_origins=("https://example.invalid",))
    )
    with caplog.at_level(logging.WARNING, logger="api.access"):
        response = client.get("/runs")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AUTH_NOT_CONFIGURED"
    assert response.json()["detail"]["path"] == "MARKETING_AI_AUTH_MODE"
    assert client.get("/healthz").status_code == 200
    assert any("access control is off" in record.getMessage() for record in caplog.records)


def test_auth_off_on_dev_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="api.access"):
        client = TestClient(local_app(tmp_path, auth_mode="off", env="dev"))
        assert client.get("/runs").status_code == 200
        assert client.get("/runs").status_code == 200
    warnings = [record for record in caplog.records if "access control is off" in record.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING


def test_a_route_without_a_policy_is_refused(tmp_path: Path) -> None:
    """Fail closed: a route nobody declared is unreachable, not open (DEC-717)."""
    app = local_app(tmp_path, auth_mode="off")

    @app.get("/undeclared-for-this-test")
    def undeclared() -> dict[str, str]:
        return {"reached": "yes"}

    response = TestClient(app).get("/undeclared-for-this-test")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ROUTE_HAS_NO_POLICY"


def test_a_disabled_users_token_stops_working_at_once(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    make_user(app, "admin", [Role.ADMIN])
    analyst = make_user(app, "asha", [Role.ANALYST])
    client = TestClient(app)
    headers = bearer(app, analyst)
    assert client.get("/runs", headers=headers).status_code == 200
    app.state.user_store.update_user(analyst, disabled=True)
    assert client.get("/runs", headers=headers).status_code == 401
