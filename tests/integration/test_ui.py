"""The wired UI: what `/ui` serves, and the two promises plan §9 makes about it.

The screens themselves are exercised in a browser during development; what is pinned here is
everything a regression could silently break without a browser noticing:

* the prototype's stylesheet is still the one the page loads, rule for rule;
* not one of the prototype's illustrative figures survived into the code (plan §13.3);
* the Setup form names no advanced setting, so a new one in the engine config appears on the
  screen with no change to any module (plan §9.2, the structural requirement of that screen);
* every endpoint the UI calls and every artefact it reads exists in this API.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import UI_DIR, create_app
from engine.contracts import ARTEFACT_REGISTRY, TABULAR_SCHEMAS

pytestmark = pytest.mark.integration

DEMO_ID: Final[str] = "targeted-advertisement"

MODULES: Final[tuple[str, ...]] = (
    "api.js",
    "app.js",
    "dom.js",
    "overview.js",
    "pages.js",
    "settings.js",
    "usecase.js",
)
"""Every module `index.html` reaches, directly or through an import."""

SHARED_MODULES: Final[tuple[str, ...]] = ("modules/router.js",)
"""Modules under `ui/modules/`, shared with the phase branches (PARALLEL_WORK_PROTOCOL.md §4).

The registry itself and nothing else. Every screen a phase branch draws lives in its own
`ui/modules/<phase>/` directory and reaches the page by registering with the router, so a branch
adds files here without this tuple, or any other line of this test, having to learn their names.
"""

ALL_MODULES: Final[tuple[str, ...]] = (*MODULES, *SHARED_MODULES)
"""Everything `index.html` reaches: the Phase 1 modules plus the shared registry."""

FORM_MODULES: Final[tuple[str, ...]] = ("settings.js", "usecase.js")
"""The two modules that build the Setup screen; neither may name a setting."""

PROTOTYPE_RULES: Final[tuple[str, ...]] = (
    ".stage-pill{",
    ".uc{",
    ".block{",
    ".setup-grid{",
    ".fstep{",
    ".control.file{",
    ".preview{",
    ".colchip{",
    "details.adv summary{",
    ".stage-d>summary{",
    ".ptype{",
    ".progress li{",
    ".progress li.active .dot{",
    ".summary{",
    ".runrow{",
    ".tabs-bar{",
    ".kpis{",
    ".card h3{",
    ".bars{",
    ".track{",
    ".cm .cell{",
    ".vchart{",
    ".pill.ok{",
)
"""A rule from every component of the prototype the wired screens still render into."""

PROTOTYPE_TOKENS: Final[tuple[str, ...]] = (
    "--brand-blue:#1E57BD",
    "--brand-yellow:#FFD500",
    "@media (prefers-color-scheme: dark)",
    ':root[data-theme="dark"]',
)

SAMPLE_VALUES: Final[tuple[str, ...]] = (
    "Amazon Redshift",
    "SageMaker",
    "Bedrock",
    "PySpark",
    "OpenSearch",
    "Kinesis",
    "DynamoDB",
    "Titan Text Embeddings",
    "ServiceNow",
    "Comprehend",
    "Feature Store",
    "Illustrative",
    "184K",
    "3.2x",
    "0.84",
    "2.4M",
    "₹",
    "XGBoost",
    "LightGBM",
    "Claude",
    "converted_30d",
    "snapshot_date",
    "Targeted Advertisement",
)
"""Figures, vendors and column names the prototype filled its screens with. None may survive."""

PLACEHOLDERS: Final[tuple[str, ...]] = ('"N/A"', "'N/A'", '"TBD"', '">--<"', '"n/a"')
"""Stand-ins the em dash replaces: plan §9.4 allows exactly one, and it lives in `dom.js`."""

PAGE_ARTEFACT_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"export const PAGE_ARTEFACTS = \{(.*?)\n\};", re.DOTALL
)
PAGE_ARTEFACT_ENTRY: Final[re.Pattern[str]] = re.compile(r"(\w+):\s*\[(.*?)\]", re.DOTALL)
API_PATH: Final[re.Pattern[str]] = re.compile(r"[`\"'](/[A-Za-z0-9_${}().,/-]*)[`\"']")
IMPORT_PATH: Final[re.Pattern[str]] = re.compile(r"""from\s+["'](\.\.?/[A-Za-z0-9_./-]+)["']""")
"""A relative import, captured with its `./` or `../` so it can be resolved against its importer."""
LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"//[^\n]*")
BLOCK_COMMENT: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """One app over the checkout, so `/ui` is mounted from the real directory."""
    return create_app()


@pytest.fixture(scope="module")
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def read(name: str) -> str:
    return (UI_DIR / name).read_text(encoding="utf-8")


def code_of(name: str) -> str:
    """One module with its comments removed, so prose about a setting is not mistaken for code."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", read(name)))


def imports_of(name: str) -> list[str]:
    """Every relative import of `name`, as a path relative to `ui/` - so nested modules resolve.

    Read from `code_of`, not the raw text: an import written out in a comment - the registration
    example in `modules/router.js` is one - is documentation, not an edge the graph has.
    """
    base = PurePosixPath(name).parent
    return [posixpath.normpath(str(base / target)) for target in IMPORT_PATH.findall(code_of(name))]


# ---------------------------------------------------------------------------
# What the API serves
# ---------------------------------------------------------------------------
def test_ui_directory_holds_exactly_the_modules_the_page_loads() -> None:
    assert UI_DIR.is_dir()
    assert sorted(p.name for p in UI_DIR.glob("*.js")) == sorted(MODULES)
    assert (UI_DIR / "index.html").is_file()
    for name in SHARED_MODULES:
        assert (UI_DIR / name).is_file(), name


def test_index_is_served_as_the_prototype_page(client: TestClient) -> None:
    response = client.get("/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert '<div id="app"' in body
    assert '<script type="module" src="./app.js"></script>' in body
    assert "<title>Marketing AI · Minfy</title>" in body


def test_every_module_is_served_as_javascript(client: TestClient) -> None:
    for name in ALL_MODULES:
        response = client.get(f"/ui/{name}")
        assert response.status_code == 200, name
        assert "javascript" in response.headers["content-type"], name


def test_every_import_resolves_to_a_served_module() -> None:
    for name in ALL_MODULES:
        for imported in imports_of(name):
            assert imported in ALL_MODULES, f"{name} imports {imported}"


# ---------------------------------------------------------------------------
# The prototype's look
# ---------------------------------------------------------------------------
def test_the_prototype_stylesheet_is_carried_over_rule_for_rule() -> None:
    css = read("index.html")
    missing = [rule for rule in PROTOTYPE_RULES if rule not in css]
    assert not missing, f"the prototype rules {missing} are gone"


def test_the_prototype_theme_tokens_are_untouched() -> None:
    css = read("index.html")
    for token in PROTOTYPE_TOKENS:
        assert token in css, token


def test_no_illustrative_value_from_the_prototype_survived() -> None:
    sources = {name: read(name) for name in (*ALL_MODULES, "index.html")}
    leaked = [(name, value) for name, text in sources.items() for value in SAMPLE_VALUES if value in text]
    assert not leaked, f"prototype sample values reached the wired UI: {leaked}"


def test_the_em_dash_is_the_only_placeholder() -> None:
    assert 'export const EM_DASH = "—";' in read("dom.js")
    for name in ALL_MODULES:
        text = read(name)
        for placeholder in PLACEHOLDERS:
            assert placeholder not in text, f"{name} uses {placeholder} instead of the em dash"


# ---------------------------------------------------------------------------
# The schema-driven Setup form
# ---------------------------------------------------------------------------
def test_the_setup_form_names_no_advanced_setting(client: TestClient) -> None:
    """The one that matters: a new setting in the config must need no change to any module."""
    schema = client.get(f"/use-cases/{DEMO_ID}").json()["advanced_settings"]
    form = "\n".join(code_of(name) for name in FORM_MODULES)
    named: list[str] = []
    for stage in schema["stages"]:
        for field in stage["fields"]:
            if field["path"] in form:
                named.append(field["path"])
            if field["label"] and field["label"] in form:
                named.append(field["label"])
    assert not named, f"the Setup form hard-codes {named}"


def test_the_setup_form_renders_every_widget_the_schema_can_ask_for(client: TestClient) -> None:
    schema = client.get(f"/use-cases/{DEMO_ID}").json()["advanced_settings"]
    settings = code_of("settings.js")
    for stage in schema["stages"]:
        for field in stage["fields"]:
            assert f'case "{field["widget"]}":' in settings, field["widget"]


def test_the_schema_the_form_reads_still_covers_all_eight_stages(client: TestClient) -> None:
    schema = client.get(f"/use-cases/{DEMO_ID}").json()["advanced_settings"]
    assert [stage["number"] for stage in schema["stages"]] == list(range(1, 9))
    assert sum(len(stage["fields"]) for stage in schema["stages"]) == 45


# ---------------------------------------------------------------------------
# What the screens read
# ---------------------------------------------------------------------------
def page_artefacts() -> dict[str, list[str]]:
    """`PAGE_ARTEFACTS` out of `pages.js`, so the test reads the same list the page does."""
    block = PAGE_ARTEFACT_BLOCK.search(read("pages.js"))
    assert block is not None
    return {key: re.findall(r'"([^"]+)"', body) for key, body in PAGE_ARTEFACT_ENTRY.findall(block.group(1))}


def test_every_artefact_the_pages_read_is_a_registered_contract() -> None:
    known = set(ARTEFACT_REGISTRY) | set(TABULAR_SCHEMAS)
    for page, names in page_artefacts().items():
        assert names, page
        unknown = [name for name in names if name not in known]
        assert not unknown, f"the {page} page reads unregistered artefacts {unknown}"


def test_the_pages_read_what_plan_section_7_assigns_them() -> None:
    pages = page_artefacts()
    assert {"profile.json", "prepare.json", "split.json"} <= set(pages["data"])
    assert {
        "run_config.json",
        "evaluation.json",
        "baseline.json",
        "feature_importance.json",
        "confusion_matrix.json",
        "leaderboard.json",
    } <= set(pages["model"])
    assert {"decile_lift.json", "scoring_summary.json", "drift.json"} <= set(pages["output"])


def test_every_endpoint_the_ui_calls_exists_in_this_api(client: TestClient) -> None:
    """The UI's fetch paths against the app's own OpenAPI surface, parameters normalised away."""
    served = {re.sub(r"\{[^}]+\}", "{}", path) for path in client.get("/openapi.json").json()["paths"]}
    called = {
        re.sub(r"\$\{[^}]+\}", "{}", path).split("?")[0] for path in API_PATH.findall(code_of("api.js"))
    }
    called = {path for path in called if path.startswith("/") and path != "/"}
    assert called, "no API path was found in api.js"
    missing = sorted(path for path in called if path not in served)
    assert not missing, f"the UI calls endpoints this API does not serve: {missing}"
