"""The Phase 4b UI module (`ui/modules/production/`, M46-M49) as the API serves it, checked statically.

`test_production_ui_js.py` drives the module in jsdom; this file pins what a DOM test cannot see
from inside the page, and what must hold even where node is not installed:

* every module is served under `/ui` and every import it makes - `from` and side-effect alike -
  resolves to a file that is served, and `boot.js`'s import graph never reaches `router.js`
  (`router.js` imports it while still evaluating, so a cycle would throw, DEC-790);
* the module is registered from the Phase 4b blocks of both shared files and nowhere else;
* every endpoint `production/api.js` calls exists in this API;
* every row of the role-gating table (`gate.js`'s `ACTION_CONTROLS`, DEC-792) names a route that has
  an access policy, and every `#id` it gates is still drawn by the screen it belongs to - so renaming
  a button in another phase's screen fails here instead of silently un-gating it;
* the controls the plan names - upload, build/train/score, approve and promote a champion, approve
  campaign copy, settings - are all in that table;
* every control the module's own M48/M49 screens draw refusable (`controls.actionButton`) names a route
  with an access policy, and the actions the plan names for M48/M49 - consent import and lookup,
  erasure, access export, retention apply, schedule create/edit/pause/resume/run now/delete, alert
  acknowledgement, outcome upload - each have one;
* no path the module calls carries a data principal's id (DEC-746: bodies only);
* the token is kept only for the tab (`sessionStorage`), and no prototype sample value or placeholder
  crept into the module (the same lists `tests/integration/test_ui.py` holds the Phase 1 UI to).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.access_policy import all_policies
from api.main import UI_DIR, create_app
from tests.integration.test_ui import PLACEHOLDERS, SAMPLE_VALUES

pytestmark = pytest.mark.integration

PRODUCTION_DIR: Final[Path] = UI_DIR / "modules" / "production"

PRODUCTION_MODULES: Final[tuple[str, ...]] = (
    "alerts.js",
    "api.js",
    "approvals.js",  # Plan D M54: the Approver's screen
    "audit.js",
    "boot.js",
    "controls.js",
    "downloads.js",
    "gate.js",
    "index.js",
    "outcomes.js",
    "privacy.js",
    "retention.js",
    "schedules.js",
    "session.js",
    "signin.js",
    "styles.js",
    "userbar.js",
    "users.js",
)
"""Every file of the module, relative to `ui/modules/production/`."""

ANY_IMPORT: Final[re.Pattern[str]] = re.compile(
    r"""(?:\bfrom|^import)\s+["'](\.\.?/[A-Za-z0-9_./-]+)["']""", re.M
)
"""A relative import: `import x from "./a.js"` and the side-effect form `import "./a.js"` alike."""

LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"(?<![:\"'`])//[^\n]*")
BLOCK_COMMENT: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
API_PATH: Final[re.Pattern[str]] = re.compile(r"[`\"'](/[A-Za-z0-9_${}().,/?=&-]*)[`\"']")
"""`test_ui.py`'s path pattern, widened to take a query string (`/audit/events?${...}`) too."""

CONTROL_ROW: Final[re.Pattern[str]] = re.compile(
    r"""\{\s*selector:\s*(?P<q>['"])(?P<selector>.+?)(?P=q),\s*method:\s*"(?P<method>[A-Z]+)",\s*path:\s*"(?P<path>[^"]+)\"""",
    re.DOTALL,
)
ID_SELECTOR: Final[re.Pattern[str]] = re.compile(r"#([A-Za-z][\w-]*)")

REQUIRED_GATES: Final[dict[str, tuple[str, str]]] = {
    "upload": ("POST", "/uploads"),
    "build / train / score (a run)": ("POST", "/runs"),
    "approve a champion": ("POST", "/models/{model_id}/approve"),
    "promote a champion": ("POST", "/models/{model_id}/promote"),
    "approve campaign copy": ("POST", "/runs/{run_id}/campaign-copy/templates/{template_id}/approve"),
    "settings": ("PUT", "/connection/aws"),
    "build an assistant": ("POST", "/use-cases/{use_case_id}/indexes"),
    # v1 (C13): what a Viewer could still press before
    "measure a campaign's results": ("POST", "/runs/{run_id}/campaign-results"),
    "save a campaign's value inputs": ("PUT", "/pilot/roi/{run_id}"),
    "export all feedback": ("GET", "/pilot/feedback/export"),
    "add a client": ("POST", "/clients"),
    "add a raw table": ("POST", "/clients/{client_id}/sources"),
    "remove a raw table": ("DELETE", "/clients/{client_id}/sources/{source_id}"),
    "save a mapping": ("PUT", "/clients/{client_id}/mappings/{mapping_id}"),
    "build a dataset": ("POST", "/datasets"),
}
"""The actions M46 names ("hide or disable every action the user cannot take"), by the route each calls."""

ACTION_BUTTON: Final[re.Pattern[str]] = re.compile(
    r'actionButton\(\s*"(?P<method>[A-Z]+)",\s*"(?P<path>[^"]+)"'
)
"""A control one of the module's own screens draws already refused when `/auth/me` refuses its route."""

REQUIRED_OPS_ACTIONS: Final[dict[str, tuple[str, str]]] = {
    "import a consent file": ("POST", "/privacy/consent/imports"),
    "look up a person's consent": ("POST", "/privacy/consent/lookup"),
    "erase a person": ("POST", "/privacy/erasure"),
    "export a person's data": ("POST", "/privacy/access-requests"),
    "apply a retention plan": ("POST", "/privacy/retention/apply"),
    "create a schedule": ("POST", "/schedules"),
    "edit a schedule": ("PATCH", "/schedules/{schedule_id}"),
    "pause a schedule": ("POST", "/schedules/{schedule_id}/disable"),
    "resume a schedule": ("POST", "/schedules/{schedule_id}/enable"),
    "run a schedule now": ("POST", "/schedules/{schedule_id}/fire"),
    "delete a schedule": ("DELETE", "/schedules/{schedule_id}"),
    "sync the retraining schedules": ("POST", "/schedules/retraining/sync"),
    "acknowledge an alert": ("POST", "/monitoring/alerts/{alert_id}/acknowledge"),
    "upload a run's outcomes": ("POST", "/runs/{run_id}/outcomes"),
}
"""The M48/M49 actions the task names, by route: each must be a refusable control on some screen."""

PHASE_BLOCK: Final[str] = r"PHASE-4B \(production\) — append only below this line ----(.*?)END PHASE-4B"


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app()


@pytest.fixture(scope="module")
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def read(relative: str) -> str:
    return (UI_DIR / relative).read_text(encoding="utf-8")


def code_of(relative: str) -> str:
    """A module with its comments removed, so prose naming a file or a path is not taken for code."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", read(relative)))


def imports_of(relative: str) -> list[str]:
    base = PurePosixPath(relative).parent
    return [posixpath.normpath(str(base / target)) for target in ANY_IMPORT.findall(code_of(relative))]


def block_of(relative: str) -> str:
    match = re.search(PHASE_BLOCK, read(relative), re.DOTALL)
    assert match is not None, relative
    return match.group(1)


def control_rows() -> list[dict[str, str]]:
    rows = [m.groupdict() for m in CONTROL_ROW.finditer(code_of("modules/production/gate.js"))]
    assert rows, "ACTION_CONTROLS was not found in gate.js"
    return rows


# ---------------------------------------------------------------------------
# Served, wired, and free of cycles
# ---------------------------------------------------------------------------
def test_the_module_holds_exactly_the_listed_files() -> None:
    assert sorted(p.name for p in PRODUCTION_DIR.glob("*.js")) == sorted(PRODUCTION_MODULES)


def test_every_production_module_is_served_as_javascript(client: TestClient) -> None:
    for name in PRODUCTION_MODULES:
        response = client.get(f"/ui/modules/production/{name}")
        assert response.status_code == 200, name
        assert "javascript" in response.headers["content-type"], name


def test_every_import_resolves_to_a_file_the_ui_serves(client: TestClient) -> None:
    for name in PRODUCTION_MODULES:
        for imported in imports_of(f"modules/production/{name}"):
            assert (UI_DIR / imported).is_file(), f"{name} imports {imported}"
            assert client.get(f"/ui/{imported}").status_code == 200, imported


def test_boot_never_imports_the_router_back() -> None:
    """`router.js` imports `boot.js` while it is still evaluating: a path back would throw at load."""
    seen: set[str] = set()
    pending = ["modules/production/boot.js"]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(imports_of(current))
    assert "modules/router.js" not in seen
    assert "modules/production/index.js" not in seen


def test_the_module_is_registered_from_both_phase_4b_blocks() -> None:
    assert '<script type="module" src="./modules/production/index.js"></script>' in block_of("index.html")
    assert 'import "./production/boot.js";' in block_of("modules/router.js")
    assert read("index.html").count("modules/production/") == 1, "index.html loads only index.js"
    assert code_of("modules/router.js").count("production/") == 1, "router.js imports only boot.js"


def test_the_index_page_still_loads_app_js_first(client: TestClient) -> None:
    body = client.get("/ui/").text
    assert body.index('src="./app.js"') < body.index('src="./modules/production/index.js"')


# ---------------------------------------------------------------------------
# The API it calls, and the table it gates by
# ---------------------------------------------------------------------------
def test_every_endpoint_the_module_calls_exists(client: TestClient) -> None:
    served = {re.sub(r"\{[^}]+\}", "{}", path) for path in client.get("/openapi.json").json()["paths"]}
    called = {
        re.sub(r"\$\{[^}]+\}", "{}", path).split("?")[0]
        for path in API_PATH.findall(code_of("modules/production/api.js"))
    }
    called = {path for path in called if path.startswith("/") and path != "/"}
    assert {"/auth/login", "/auth/logout", "/auth/me", "/users", "/audit/events", "/audit/exports"} <= called
    assert {
        "/privacy/purposes",
        "/privacy/consent/imports",
        "/privacy/consent/lookup",
        "/privacy/erasure",
        "/privacy/access-requests",
        "/privacy/retention/plan",
        "/privacy/retention/apply",
        "/privacy/retrain-flags",
        "/privacy/runs/{}/consent-report",
        "/schedules",
        "/schedules/{}",
        "/schedules/{}/fire",
        "/schedules/{}/firings",
        "/schedules/retraining/sync",
        "/monitoring/alerts",
        "/monitoring/alerts/{}/acknowledge",
        "/monitoring/missed-firings",
        "/runs",
        "/runs/{}/outcomes",
        "/runs/{}/incrementality-input",
    } <= called
    missing = sorted(path for path in called if path not in served)
    assert not missing, f"the production UI calls endpoints this API does not serve: {missing}"


def test_every_gated_control_names_a_route_with_an_access_policy() -> None:
    policies = all_policies()
    for row in control_rows():
        policy = policies.get((row["method"], row["path"]))
        assert (
            policy is not None
        ), f"{row['selector']} gates {row['method']} {row['path']}, which has no policy"
        assert policy.role is not None, f"{row['selector']} gates a public route"


def test_every_gated_id_is_still_drawn_by_a_screen() -> None:
    """Other phases' files, not this module's: a renamed button would otherwise go quietly ungated."""
    screens = "\n".join(
        path.read_text(encoding="utf-8")
        for path in UI_DIR.rglob("*.js")
        if PRODUCTION_DIR not in path.parents
    )
    for row in control_rows():
        for element_id in ID_SELECTOR.findall(row["selector"]):
            assert f'id="{element_id}"' in screens, f"no screen draws #{element_id} any more"
    assert "data-approve=" in screens and "data-regen=" in screens
    assert 'name="c-source"' in screens


def test_the_actions_the_plan_names_are_all_gated() -> None:
    gated = {(row["method"], row["path"]) for row in control_rows()}
    missing = [name for name, key in REQUIRED_GATES.items() if key not in gated]
    assert not missing, f"no control is gated for: {missing}"


def action_buttons() -> list[tuple[str, str, str]]:
    """`(file, method, path)` of every `actionButton(...)` call in the module."""
    found = [
        (name, match["method"], match["path"])
        for name in PRODUCTION_MODULES
        for match in ACTION_BUTTON.finditer(code_of(f"modules/production/{name}"))
    ]
    assert found, "no actionButton call was found"
    return found


def test_every_refusable_control_on_our_own_screens_names_a_route_with_an_access_policy() -> None:
    policies = all_policies()
    for name, method, path in action_buttons():
        policy = policies.get((method, path))
        assert policy is not None, f"{name} draws a control for {method} {path}, which has no policy"
        assert policy.role is not None, f"{name} refuses a control for a public route {method} {path}"


def test_the_m48_and_m49_actions_are_all_offered_and_refusable() -> None:
    offered = {(method, path) for _, method, path in action_buttons()}
    missing = [name for name, key in REQUIRED_OPS_ACTIONS.items() if key not in offered]
    assert not missing, f"no refusable control for: {missing}"


def test_no_path_the_module_calls_can_carry_a_data_principal_id() -> None:
    """DEC-746: a principal id goes in a POST body; a URL is logged by every proxy and kept in history."""
    code = code_of("modules/production/api.js")
    for path in API_PATH.findall(code):
        assert "principal" not in path.lower(), path
    for route in ("/privacy/consent/lookup", "/privacy/erasure", "/privacy/access-requests"):
        assert re.search(rf'"{re.escape(route)}"\)?,\s*json\("POST"', code), f"{route} must be a JSON POST"


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------
def test_the_token_lives_only_as_long_as_the_tab() -> None:
    for name in PRODUCTION_MODULES:
        assert "localStorage" not in code_of(f"modules/production/{name}"), name
        assert "document.cookie" not in code_of(f"modules/production/{name}"), name
    assert "sessionStorage" in code_of("modules/production/session.js")


def test_no_prototype_sample_value_or_placeholder_is_in_the_module() -> None:
    for name in PRODUCTION_MODULES:
        text = read(f"modules/production/{name}")
        leaked = [value for value in SAMPLE_VALUES if value in text]
        assert not leaked, f"{name} carries prototype values {leaked}"
        stand_ins = [value for value in PLACEHOLDERS if value in text]
        assert not stand_ins, f"{name} uses {stand_ins} instead of the em dash"


def test_refusal_sentences_come_from_the_server() -> None:
    """The UI shows the policy's own sentence (`refusal_message`); it never writes a role rule of its own."""
    for name in PRODUCTION_MODULES:
        code = code_of(f"modules/production/{name}")
        assert not re.search(r"Only an? (Viewer|Analyst|Approver|Admin) can", code), name
