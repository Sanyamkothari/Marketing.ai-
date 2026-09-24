"""The Phase 4b UI modules exercised in jsdom against REAL API responses (M46/M47, DEC-790…794).

`ui/modules/production/` is plain ES modules with no build step, like the rest of `ui/`, so its
behaviour - the token on every call, the sign-in redirect, the gated controls, the Users and Audit
screens - is tested where it runs: in a DOM. `tests/integration/production/ui/*.test.mjs` do that
with jsdom under `node --test`.

What makes them more than a mock-up test is where their fixtures come from. This module builds the
real app with `auth_mode=local`, signs in one person per role, and writes what the API actually
answered - `GET /auth/me` (the permission list and the refusal sentences the UI shows), `POST
/auth/login`, `GET /users`, `GET /audit/events`, a `409 LAST_ADMIN` - into a directory the node tests
read through `PB_FIXTURES`. A change to the policy table, a refusal sentence or a response shape
reaches these tests the same day, instead of drifting from a hand-written copy.

Node and jsdom are a development dependency of this one directory (as `tests/prototype/` is of the
prototype's tests): without them this test is skipped with the command that installs them - or,
under `REQUIRE_JSDOM=1` as CI sets it, fails (`tests/fixtures/node.py`) - and the static checks in
`test_production_ui.py` still run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from tests.fixtures.node import skip_without_jsdom
from tests.integration.production.access_support import PASSWORD, bearer, local_app, make_user

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "ui"
"""The node package holding the jsdom tests and their `node_modules`."""

ROLES_BY_NAME: Final[dict[str, tuple[Role, ...]]] = {
    "viewer": (Role.VIEWER,),
    "analyst": (Role.ANALYST,),
    "approver": (Role.APPROVER,),
    "admin": (Role.ADMIN,),
}


def _write(directory: Path, name: str, body: Any) -> None:
    (directory / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")


def write_fixtures(tmp_path: Path) -> Path:
    """Every response body the node tests replay, captured from the real app."""
    out = tmp_path / "fixtures"
    out.mkdir()
    app = local_app(tmp_path / "local")
    ids = {name: make_user(app, f"{name}-person", roles) for name, roles in ROLES_BY_NAME.items()}
    with TestClient(app) as client:
        for name, user_id in ids.items():
            response = client.get("/auth/me", headers=bearer(app, user_id))
            assert response.status_code == 200, response.text
            _write(out, f"me_{name}", response.json())
        login = client.post("/auth/login", json={"username": "admin-person", "password": PASSWORD})
        assert login.status_code == 200, login.text
        _write(out, "login_ok", login.json())
        refused = client.post(
            "/auth/login", json={"username": "admin-person", "password": "not the password"}
        )
        assert refused.status_code == 401, refused.text
        _write(out, "login_refused", refused.json())
        admin = bearer(app, ids["admin"])
        users = client.get("/users", headers=admin)
        assert users.status_code == 200, users.text
        _write(out, "users", users.json())
        last_admin = client.patch(f"/users/{ids['admin']}", json={"disabled": True}, headers=admin)
        assert last_admin.status_code == 409, last_admin.text
        _write(out, "last_admin", last_admin.json())
        events = client.get("/audit/events", params={"limit": 2}, headers=admin)
        assert events.status_code == 200, events.text
        assert events.json()["total"] > 2, "the fixture needs more than one page of events"
        _write(out, "audit_page", events.json())
        demote = client.patch(f"/users/{ids['admin']}", json={"roles": ["viewer"]}, headers=admin)
        assert demote.status_code == 409, demote.text
        assert demote.json()["detail"]["code"] == "LAST_ADMIN", demote.text
        _write(out, "last_admin_demote", demote.json())
        _write(out, "ids", ids)
        connection = client.get("/connection/aws", headers=bearer(app, ids["viewer"]))
        assert connection.status_code == 200, connection.text
        _write(out, "connection", connection.json())
        industries = client.get("/industries", headers=bearer(app, ids["viewer"]))
        assert industries.status_code == 200, industries.text
        _write(out, "industries", industries.json())
        # last: the viewer's own reads above need the account enabled
        disabled = client.patch(f"/users/{ids['viewer']}", json={"disabled": True}, headers=admin)
        assert disabled.status_code == 200, disabled.text
        _write(out, "user_disabled", disabled.json())
    off = local_app(tmp_path / "off", auth_mode="off")
    with TestClient(off) as client:
        response = client.get("/auth/me")
        assert response.status_code == 200, response.text
        _write(out, "me_off", response.json())
    return out


def test_the_fixtures_are_what_the_ui_expects(tmp_path: Path) -> None:
    """Runs without node: the shapes the jsdom tests rely on are really what the API answers."""
    out = write_fixtures(tmp_path)
    viewer = json.loads((out / "me_viewer.json").read_text(encoding="utf-8"))
    approve = [p for p in viewer["permissions"] if p["path"] == "/models/{model_id}/approve"]
    assert approve == [
        {
            "method": "POST",
            "path": "/models/{model_id}/approve",
            "action": "models.approve",
            "role": "approver",
            "allowed": False,
            "reason": "Only an Approver can approve a champion.",
        }
    ]
    off = json.loads((out / "me_off.json").read_text(encoding="utf-8"))
    assert off["auth_mode"] == "off"
    assert all(p["allowed"] for p in off["permissions"]), "with sign-in off nothing may be gated"


def test_the_production_ui_modules_in_jsdom(tmp_path: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)
    out = write_fixtures(tmp_path)
    tests = sorted(str(p) for p in NODE_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "PB_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-3000:]
