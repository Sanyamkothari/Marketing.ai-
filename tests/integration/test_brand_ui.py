"""Plan M M182 (DEC-1612): the brand in the browser, in jsdom, against REAL `GET /auth/me` answers.

The node suites in `tests/integration/production/ui/brand/` (sharing that directory's jsdom install, as the other
screens' tests do) load the whole page as `ui/index.html` does, every module in its script order, against the API
answers recorded from the validated Hillstrom demo, with `GET /auth/me` answered by the bodies this module writes from
the real app:

* `custom.test.mjs` - a deployment's own name, logo, logo name and colour, drawn in both layouts, signed in and out,
  with the tab's title, a logo that does not load, a malformed answer and an answer with no brand;
* `remembered.test.mjs` - the last custom brand is remembered in the browser and checked like an answer;
* `unit.test.mjs` - `ui/brand.js` on its own;
* `checks.test.mjs` - a custom brand adds no accessibility defect and no engine word to any page;
* `default.test.mjs` - with the default brand every page is byte for byte what it was, in both layouts.

The last is differential, like M174's classic check, so it holds on whatever main M182 is merged onto: this module copies
`ui/` and the node tests into a scratch tree, takes M182 out of the copy (every line between a `// <M182>` and its
`// </M182>` mark; today's literal text put back where the bar's markup calls the brand; `ui/brand.js` deleted so
that nothing can still import it), draws the pages there as the reference, and then compares the real tree with it.
Node and jsdom are a development dependency of that one directory: without them the jsdom tests skip with the
install command, or fail under `REQUIRE_JSDOM=1` as CI sets it (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import json
import os
import re
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
BRAND_DIR: Final[Path] = NODE_DIR / "brand"
UI_DEMO: Final[Path] = ROOT / "tests" / "fixtures" / "ui_demo"

# The files M182 edits rather than adds, and its marks in them: everything between a pair is M182.
MARKED: Final[tuple[str, ...]] = ("app.js", "chrome.js", "shell.js")
BEGIN: Final[str] = "// <M182>"
END: Final[str] = "// </M182>"
# What puts today's text back where the markup calls the brand: (the call, today's literal, how many times, in which file).
LITERALS: Final[tuple[tuple[str, str, str, int], ...]] = (
    ("chrome.js", "${brandNameHtml()}", "Marketing AI", 1),
    ("chrome.js", "${esc(brandLogoAlt())}", "Minfy", 1),
    ("chrome.js", "${brandLogoHtml(LOGO)}", "${LOGO}", 1),
    ("shell.js", "${brandNameHtml()}", "Marketing AI", 2),
    ("shell.js", "${esc(brandLogoAlt())}", "Minfy", 2),
    ("shell.js", "${brandLogoHtml(LOGO)}", "${LOGO}", 2),
    ("app.js", "brandName()", '"Marketing AI"', 1),
)
# What may not survive the removal: any use of the file M182 adds.
M182_NAMES: Final[tuple[str, ...]] = (
    "brand.js",
    "brandName",
    "brandNameHtml",
    "brandLogoAlt",
    "brandLogoHtml",
    "BRAND_EVENT",
)

CUSTOM: Final[dict[str, str]] = {
    "brand_name": "Acme Growth",
    "brand_logo_url": "https://cdn.example.com/acme-logo.svg",
    "brand_logo_alt": "Acme",
    "brand_color": "#0B5D3B",
}


def _write(directory: Path, name: str, body: Any) -> None:
    (directory / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")


def _me(tmp_path: Path, name: str, **settings: Any) -> dict[str, Any]:
    app = local_app(tmp_path / name, auth_mode="off", **settings)
    with TestClient(app) as client:
        response = client.get("/auth/me")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def write_fixtures(tmp_path: Path) -> Path:
    """`GET /auth/me` as the real app answers it: default and custom, in both layouts, and signed in as a Viewer."""
    out = tmp_path / "fixtures"
    out.mkdir()
    for layout in ("classic", "campaigns"):
        _write(out, f"default_{layout}", _me(tmp_path, f"default-{layout}", ui_layout=layout))
        _write(out, f"custom_{layout}", _me(tmp_path, f"custom-{layout}", ui_layout=layout, **CUSTOM))
    _write(
        out,
        "custom_name_classic",
        _me(
            tmp_path, "custom-name", brand_name=CUSTOM["brand_name"], brand_logo_alt=CUSTOM["brand_logo_alt"]
        ),
    )
    signed_in = local_app(tmp_path / "viewer", ui_layout="campaigns", **CUSTOM)
    user_id = make_user(signed_in, "viewer-person", (Role.VIEWER,))
    with TestClient(signed_in) as client:
        response = client.get("/auth/me", headers=bearer(signed_in, user_id))
        assert response.status_code == 200, response.text
        _write(out, "custom_viewer_campaigns", response.json())
    return out


def test_the_fixtures_carry_the_brand(tmp_path: Path) -> None:
    """Runs without node: the answers the jsdom tests replay say what to draw."""
    out = write_fixtures(tmp_path)
    for layout in ("classic", "campaigns"):
        default = json.loads((out / f"default_{layout}.json").read_text(encoding="utf-8"))
        assert default["brand"] == {
            "name": "Marketing AI",
            "logo_url": "",
            "logo_alt": "Minfy",
            "color": "#1E57BD",
        }
        assert default["ui_layout"] == layout
        custom = json.loads((out / f"custom_{layout}.json").read_text(encoding="utf-8"))
        assert custom["brand"] == {
            "name": "Acme Growth",
            "logo_url": "https://cdn.example.com/acme-logo.svg",
            "logo_alt": "Acme",
            "color": "#0B5D3B",
        }
    viewer = json.loads((out / "custom_viewer_campaigns.json").read_text(encoding="utf-8"))
    assert viewer["auth_mode"] == "local"
    assert viewer["brand"]["name"] == "Acme Growth"
    name_only = json.loads((out / "custom_name_classic.json").read_text(encoding="utf-8"))
    assert name_only["brand"]["color"] == "#1E57BD"
    assert name_only["brand"]["logo_url"] == ""


def without_m182(text: str) -> str:
    """`text` with every line from a `// <M182>` mark to its `// </M182>` mark, both included, removed."""
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


def before_m182(name: str, text: str) -> str:
    """The file as it was before M182: the marks' lines out, and today's literals where the brand is called."""
    stripped = without_m182(text)
    for file, call, literal, count in LITERALS:
        if file != name:
            continue
        assert (
            stripped.count(call) == count
        ), f"{name}: expected {count} x {call!r}, found {stripped.count(call)}"
        stripped = stripped.replace(call, literal)
    return stripped


def test_removing_m182_leaves_nothing_of_it() -> None:
    """Runs without node: the marks pair up, every call is accounted for, and nothing of the brand is left after."""
    for name in MARKED:
        text = (ROOT / "ui" / name).read_text(encoding="utf-8")
        assert text.count(BEGIN) == text.count(END) >= 1, name
        stripped = before_m182(name, text)
        assert [word for word in M182_NAMES if word in stripped] == [], name
    marked = {path.name for path in (ROOT / "ui").rglob("*.js") if BEGIN in path.read_text(encoding="utf-8")}
    assert marked == set(MARKED), "M182 marks only the files it edits"
    for path in (ROOT / "ui").rglob("*.js"):
        if path.name == "brand.js" or path.name in MARKED:
            continue
        # `\bbrand\.js\b`: the module itself, not a file that merely ends in it (`onboarding_brand.json`)
        assert not re.search(
            r"\bbrand\.js\b|brandName|brandLogo", path.read_text(encoding="utf-8")
        ), f"{path.name} reaches the brand without a mark"


def test_the_page_itself_is_not_edited() -> None:
    """ui/index.html is a shared, append-only file: M182 leaves its bytes alone and draws the brand from ui/brand.js.

    The logo reads `--brand-blue` there, as it always did; a custom colour keeps Minfy's mark in Minfy's blue with two
    rules `ui/brand.js` writes into `#brand-styles` (`custom.test.mjs`), so the page's own <style> is the one on main.
    """
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert "--logo-blue" not in html
    assert ".logo .b{fill:var(--brand-blue)}" in html and ".logo .s{stroke:var(--brand-blue)}" in html
    assert "brand-styles" not in html and "brand.js" not in html, "the page loads the brand through app.js"
    assert ".run-pct{" in html and "rgba(30,87,189,0.2)" in html


def build_reference_tree(tmp_path: Path) -> Path:
    """A scratch copy of the page and its node tests with M182 taken out of ui/app.js, chrome.js and shell.js."""
    ref = tmp_path / "before-m182"
    shutil.copytree(ROOT / "ui", ref / "ui")
    for name in MARKED:
        source = (ROOT / "ui" / name).read_text(encoding="utf-8")
        (ref / "ui" / name).write_text(before_m182(name, source), encoding="utf-8")
    (ref / "ui" / "brand.js").unlink()
    node_dir = ref / NODE_DIR.relative_to(ROOT)
    shutil.copytree(NODE_DIR, node_dir, ignore=shutil.ignore_patterns("node_modules"))
    (node_dir / "node_modules").symlink_to(NODE_DIR / "node_modules", target_is_directory=True)
    shutil.copytree(UI_DEMO, ref / UI_DEMO.relative_to(ROOT))
    return node_dir


def _node_test(node: str, cwd: Path, tests: list[str], env: dict[str, str]) -> None:
    clean = {k: v for k, v in os.environ.items() if k not in ("BRAND_REF_OUT", "BRAND_REF", "BRAND_FIXTURES")}
    result = subprocess.run(
        [node, "--test", "--test-concurrency=1", *tests],
        cwd=cwd,
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout


def test_the_brand_in_jsdom(tmp_path: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)
    out = write_fixtures(tmp_path)
    reference = tmp_path / "default_before_m182.json"
    ref_node_dir = build_reference_tree(tmp_path)
    _node_test(
        node,
        ref_node_dir,
        [str(ref_node_dir / "brand" / "default.test.mjs")],
        {"BRAND_FIXTURES": str(out), "BRAND_REF_OUT": str(reference)},
    )
    drawn = json.loads(reference.read_text(encoding="utf-8"))
    assert len(drawn["states"]) == 5
    for one in drawn["states"]:
        assert len(one["pages"]) >= 16, one["name"]
        assert all(page["bar"].startswith('<header class="topbar') for page in one["pages"].values()), one[
            "name"
        ]
    tests = sorted(str(p) for p in BRAND_DIR.glob("*.test.mjs"))
    assert len(tests) == 5, tests
    _node_test(node, NODE_DIR, tests, {"BRAND_FIXTURES": str(out), "BRAND_REF": str(reference)})
