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
  visible-text check.

The classic check is differential, so it holds on whatever main M174 is merged onto: this module copies `ui/` and
the node tests into a scratch tree, removes M174 from that copy (every line between the `// <M174>` and
`// </M174>` marks of `ui/app.js` and `ui/chrome.js`; the marks must pair up, and nothing of M174 may be left
after), draws every page there as the reference, and then compares the real tree with it. Node and jsdom are a
development dependency of that one directory: without them the jsdom tests skip with the install command, or fail
under `REQUIRE_JSDOM=1` as CI sets it (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from tests.fixtures.node import skip_without_jsdom
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
SHELL_DIR: Final[Path] = NODE_DIR / "shell"
UI_DEMO: Final[Path] = ROOT / "tests" / "fixtures" / "ui_demo"

# The files M174 edits rather than adds, and its marks in them: everything between a pair is M174.
MARKED: Final[tuple[str, ...]] = ("app.js", "chrome.js")
BEGIN: Final[str] = "// <M174>"
END: Final[str] = "// </M174>"
# What may not survive the removal: any use of the two files M174 adds.
M174_NAMES: Final[tuple[str, ...]] = (
    "layout.js",
    "shell.js",
    "layoutRedirect",
    "layoutDrawn",
    "isCampaignLayout",
)

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


def without_m174(text: str) -> str:
    """`text` with every line from a `// <M174>` mark to its `// </M174>` mark, both included, removed."""
    kept: list[str] = []
    inside = False
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        mark = line.strip()
        if mark == BEGIN:
            assert not inside, f"line {number}: {BEGIN} inside another"
            inside = True
        elif mark == END:
            assert inside, f"line {number}: {END} without {BEGIN}"
            inside = False
        elif not inside:
            kept.append(line)
    assert not inside, f"{BEGIN} never closed"
    return "".join(kept)


def test_removing_m174_leaves_nothing_of_it() -> None:
    """Runs without node: the marks pair up, and the files without them never reach layout.js or shell.js."""
    for name in MARKED:
        text = (ROOT / "ui" / name).read_text(encoding="utf-8")
        assert text.count(BEGIN) == text.count(END) >= 2, name
        stripped = without_m174(text)
        assert [word for word in M174_NAMES if word in stripped] == [], name
    unmarked = {
        path.name for path in (ROOT / "ui").rglob("*.js") if BEGIN in path.read_text(encoding="utf-8")
    }
    assert unmarked == set(MARKED), "M174 marks only the two files it edits"


def build_reference_tree(tmp_path: Path) -> Path:
    """A scratch copy of the page and its node tests with M174 removed from ui/app.js and ui/chrome.js."""
    ref = tmp_path / "before-m174"
    shutil.copytree(ROOT / "ui", ref / "ui")
    for name in MARKED:
        source = (ROOT / "ui" / name).read_text(encoding="utf-8")
        (ref / "ui" / name).write_text(without_m174(source), encoding="utf-8")
    node_dir = ref / NODE_DIR.relative_to(ROOT)
    shutil.copytree(NODE_DIR, node_dir, ignore=shutil.ignore_patterns("node_modules"))
    (node_dir / "node_modules").symlink_to(NODE_DIR / "node_modules", target_is_directory=True)
    shutil.copytree(UI_DEMO, ref / UI_DEMO.relative_to(ROOT))
    return node_dir


def _node_test(node: str, cwd: Path, tests: list[str], env: dict[str, str]) -> None:
    clean = {k: v for k, v in os.environ.items() if k not in ("SHELL_CLASSIC_OUT", "SHELL_CLASSIC_REF")}
    result = subprocess.run(
        [node, "--test", "--test-concurrency=1", *tests],
        cwd=cwd,
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout


def test_the_shell_in_jsdom(tmp_path: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)
    out = write_fixtures(tmp_path)
    reference = tmp_path / "classic_before_m174.json"
    ref_node_dir = build_reference_tree(tmp_path)
    _node_test(
        node,
        ref_node_dir,
        [str(ref_node_dir / "shell" / "classic.test.mjs")],
        {"SH_FIXTURES": str(out), "SHELL_CLASSIC_OUT": str(reference)},
    )
    drawn = json.loads(reference.read_text(encoding="utf-8"))
    assert len(drawn["bars"]) >= 35
    assert all(
        bar.startswith('<header class="topbar" role="banner" id="pb-bar">') for bar in drawn["bars"].values()
    )
    tests = sorted(str(p) for p in SHELL_DIR.glob("*.test.mjs"))
    assert len(tests) == 3, tests
    _node_test(node, NODE_DIR, tests, {"SH_FIXTURES": str(out), "SHELL_CLASSIC_REF": str(reference)})
