"""Plan M M174 (DEC-1604): the new shell, in jsdom, against REAL `GET /auth/me` answers.

The node suites in `tests/integration/production/ui/shell/` (sharing that directory's jsdom install, as the other
screens' tests do) load the whole page as `ui/index.html` does, every module in its script order, against the API
answers recorded from the validated Hillstrom demo (`tests/fixtures/ui_demo/api.json`, M170), with `GET /auth/me`
answered by the bodies this module writes from the real app:

* `shell.test.mjs` - the campaigns layout: five places for every role, the place marked by the route, the approvals
  bell with its count for an Approver only, Help, the keyboard path through the bar, the folded sidebar (the Menu
  button below 900 px), signing out, and switching back to the classic bar;
* `routes.test.mjs` - the redirect table (`ui/layout.js`): every route a module registers has a home, every old
  address goes where Plan M §3 says, redirects replace the history entry, never go to a page this build lacks, and
  never bounce;
* `classic.test.mjs` - the classic layout is byte for byte the bar it was before M174, on every page of the
  visible-text check, and the shell itself shows no never-shown word (an empty allow-list).

`classic.test.mjs` compares with `shell/classic_bar.json`, written from the code before M174 (see that file's
`how`). Node and jsdom are a development dependency of that one directory: without them the jsdom tests skip with the
install command, or fail under `REQUIRE_JSDOM=1` as CI sets it (`tests/fixtures/node.py`).
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
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
SHELL_DIR: Final[Path] = NODE_DIR / "shell"

ROLES: Final[dict[str, tuple[Role, ...]]] = {
    "viewer": (Role.VIEWER,),
    "analyst": (Role.ANALYST,),
    "approver": (Role.APPROVER,),
    "admin": (Role.ADMIN,),
}


def _write(directory: Path, name: str, body: Any) -> None:
    (directory / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")


def write_fixtures(tmp_path: Path) -> Path:
    """`GET /auth/me` as the real app answers it, per role, in the campaigns layout and (sign-in off) in both."""
    out = tmp_path / "fixtures"
    out.mkdir()
    app = local_app(tmp_path / "local", ui_layout="campaigns")
    ids = {name: make_user(app, f"{name}-person", roles) for name, roles in ROLES.items()}
    with TestClient(app) as client:
        for name, user_id in ids.items():
            response = client.get("/auth/me", headers=bearer(app, user_id))
            assert response.status_code == 200, response.text
            _write(out, f"me_{name}", response.json())
    for layout in ("campaigns", "classic"):
        off = local_app(tmp_path / f"off-{layout}", auth_mode="off", ui_layout=layout)
        with TestClient(off) as client:
            response = client.get("/auth/me")
            assert response.status_code == 200, response.text
            _write(out, f"me_off_{layout}", response.json())
    return out


def test_the_fixtures_carry_the_layout(tmp_path: Path) -> None:
    """Runs without node: the answers the jsdom tests replay say which layout to draw, for every role."""
    out = write_fixtures(tmp_path)
    for name in ROLES:
        body = json.loads((out / f"me_{name}.json").read_text(encoding="utf-8"))
        assert body["ui_layout"] == "campaigns", name
        assert body["auth_mode"] == "local", name
    assert json.loads((out / "me_off_classic.json").read_text(encoding="utf-8"))["ui_layout"] == "classic"
    approver = json.loads((out / "me_approver.json").read_text(encoding="utf-8"))
    approve = [p for p in approver["permissions"] if p["path"] == "/models/{model_id}/approve"]
    assert approve and approve[0]["allowed"], "the bell's count needs an Approver who may approve"


def test_the_classic_bar_snapshot_is_a_plain_table() -> None:
    """Runs without node: `{page: bar markup}` for every page, with a note on how it was made."""
    snapshot = json.loads((SHELL_DIR / "classic_bar.json").read_text(encoding="utf-8"))
    assert snapshot["how"].startswith("Written by classic.test.mjs")
    assert len(snapshot["bars"]) >= 35
    assert all(
        bar.startswith('<header class="topbar" role="banner" id="pb-bar">')
        for bar in snapshot["bars"].values()
    )


def test_the_shell_in_jsdom(tmp_path: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)
    out = write_fixtures(tmp_path)
    tests = sorted(str(p) for p in SHELL_DIR.glob("*.test.mjs"))
    assert len(tests) == 3, tests
    result = subprocess.run(
        [node, "--test", "--test-concurrency=1", *tests],
        cwd=NODE_DIR,
        env={
            **{key: value for key, value in os.environ.items() if key != "SHELL_WRITE_CLASSIC"},
            "SH_FIXTURES": str(out),
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
