"""The onboarding panel (`ui/modules/onboarding/`) against the parity properties `test_ui.py` and
`test_prototype_parity.py` already hold the wired Phase 1 screens to (Phase 2 plan §10, M13).

This is a static-analysis test, on purpose: the panel is plain ES modules with no build step, so
there is nothing to import and exercise from Python. What is checked instead, by parsing the three
files as text, is exactly what a browser cannot tell us and a careless edit could slip past review:

* the module shape the router/host will actually import (`onboardingPanel`, the four step functions,
  every fetch wrapper `steps.js`/`panel.js` calls);
* that nothing here fabricates a number - no comma-grouped figure, no "N/A"-style placeholder other
  than the one em dash `dom.js` defines (house rule 2, plan §13.3);
* that the "Add feature" form's vocabulary (function, filter operator) is read off the API's own
  OpenAPI schema at runtime rather than typed into the file by hand;
* that every path the panel fetches is one this API actually defines - checked for real, through a
  bare `FastAPI()` app carrying only the onboarding routers, wherever those routers already exist to
  check against; the two routers this branch's sibling tasks were still writing when this test was
  written (`api/routes/mappings.py`, `api/routes/datasets.py`) fall back to a transcription of the
  exact contract they were commissioned against, so a typo'd path fails loudly either way and a
  path that matches the contract passes before the sibling PR lands, not only after it.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Final

import pytest

ONBOARDING_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "ui" / "modules" / "onboarding"

MODULES: Final[tuple[str, ...]] = ("api.js", "panel.js", "steps.js")

LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"//[^\n]*")
BLOCK_COMMENT: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
PATH_LITERAL: Final[re.Pattern[str]] = re.compile(r"[`\"'](/[A-Za-z0-9_${}().,/-]*)[`\"']")
PARAM: Final[re.Pattern[str]] = re.compile(r"\$\{[^}]+\}")

# A number with thousands separators - "12,345" - can only be the *output* of `fmtInt`'s
# `toLocaleString`; nobody hand-types one into a template literal, so its presence anywhere in the
# source is exactly the fabricated-placeholder pattern plan §13.3 forbids (the task's own example).
COMMA_GROUPED_NUMBER: Final[re.Pattern[str]] = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")

PLACEHOLDERS: Final[tuple[str, ...]] = ('"N/A"', "'N/A'", '"TBD"', '">--<"', '"n/a"')
"""The stand-ins `dom.js`'s em dash replaces (`tests/integration/test_ui.py`'s own list) - none of
them may appear here either; there is exactly one placeholder for "nobody measured this"."""

FUNCTION_VOCABULARY_LITERALS: Final[tuple[str, ...]] = (
    '"count"',
    '"sum"',
    '"nunique"',
    '"days_since_last"',
    '"days_since_first"',
)
"""A sample of `AggFunction`'s own member values. None may be hand-typed into `steps.js`: the
"Add feature" form's function choices are supposed to come from `schemaEnum("AggFunction")` at
render time, not from a list copied out of `engine/config.py` and frozen in the UI."""


def read(name: str) -> str:
    return (ONBOARDING_DIR / name).read_text(encoding="utf-8")


def code_of(name: str) -> str:
    """One module with its comments stripped, so a path or a number mentioned in prose is never
    mistaken for one actually reachable at runtime - the same precaution `test_ui.py` takes."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", read(name)))


# ---------------------------------------------------------------------------
# The files exist and export the shape a host imports them for
# ---------------------------------------------------------------------------
def test_the_three_modules_exist() -> None:
    assert ONBOARDING_DIR.is_dir()
    for name in MODULES:
        assert (ONBOARDING_DIR / name).is_file(), name


def test_panel_exports_the_documented_entry_point() -> None:
    """The task's own signature: `onboardingPanel(container, {clientId, useCaseId, onDatasetReady})`.
    A host - `ui/usecase.js`'s Step 1, once the orchestrator wires it in - imports exactly this."""
    text = code_of("panel.js")
    assert re.search(r"export function onboardingPanel\(\s*container\s*,", text)
    assert "clientId" in text and "useCaseId" in text and "onDatasetReady" in text


def test_the_four_steps_are_exported_in_order() -> None:
    text = code_of("steps.js")
    for name in ("sourcesStep", "mappingStep", "featuresStep", "buildStep"):
        assert f"export function {name}(state)" in text, name
    # each one renders as one of the four collapsible, green-ticked steps the plan names
    assert text.count("stepShell(") >= 4
    for step_id in ("sources", "mapping", "features", "build"):
        assert f'"{step_id}"' in text, step_id


def test_api_exports_every_call_the_steps_orchestration_needs() -> None:
    text = code_of("api.js")
    expected = (
        "ApiError",
        "getStandardSchema",
        "getOpenApiSchema",
        "schemaEnum",
        "schemaProperty",
        "listSources",
        "createSource",
        "setSourceRole",
        "deleteSource",
        "suggestMapping",
        "saveMapping",
        "createOnboardingSpec",
        "previewOnboardingSpec",
        "createDataset",
        "getDataset",
        "getDatasetReport",
    )
    for name in expected:
        assert re.search(rf"export (const|(async )?function|class) {name}\b", text), name


# ---------------------------------------------------------------------------
# Nothing fabricated: house rule 2, plan §13.3
# ---------------------------------------------------------------------------
def test_no_comma_grouped_number_is_hand_typed() -> None:
    for name in MODULES:
        text = code_of(name)
        found = COMMA_GROUPED_NUMBER.findall(text)
        assert not found, f"{name} has a literal number that can only be `fmtInt` output: {found}"


def test_the_em_dash_is_the_only_placeholder() -> None:
    for name in MODULES:
        text = read(name)
        for placeholder in PLACEHOLDERS:
            assert placeholder not in text, f"{name} uses {placeholder} instead of the em dash"


def test_esc_and_dash_carry_every_rendered_value() -> None:
    """Every screen that draws HTML from an API response goes through `dom.js`'s two escape/format
    primitives - the same rule `pages.js` and `usecase.js` follow, checked here as usage counts
    because there is no build step and therefore no AST to walk more precisely than that."""
    steps_text = code_of("steps.js")
    assert 'from "../../dom.js"' in steps_text
    assert steps_text.count("esc(") >= 20, "steps.js renders far fewer escaped values than it has rows"
    assert (
        steps_text.count("dash(") >= 8
    ), "steps.js renders far fewer possibly-absent values than it has fields"
    # `table()`, `errorBox()` and `barTrack()` are dom.js's other escaping call sites - used, not reimplemented
    for helper in ("table(", "errorBox(", "barTrack("):
        assert helper in steps_text, helper


# ---------------------------------------------------------------------------
# The Add-feature form is generated, never hardcoded (the task's own wording)
# ---------------------------------------------------------------------------
def test_the_feature_vocabulary_is_read_from_the_schema_not_hardcoded() -> None:
    steps_text = code_of("steps.js")
    panel_text = code_of("panel.js")
    for literal in FUNCTION_VOCABULARY_LITERALS:
        assert literal not in steps_text, f"{literal} looks hand-typed into the feature form"
    assert 'schemaEnum("AggFunction")' in panel_text
    assert 'schemaEnum("WhereOp")' in panel_text
    assert 'schemaEnum("SnapshotMode")' in panel_text
    assert 'schemaEnum("SnapshotFrequency")' in panel_text
    assert 'schemaProperty("FeatureDef", "name")' in panel_text


# ---------------------------------------------------------------------------
# Every fetch targets a URL this API defines
# ---------------------------------------------------------------------------
FALLBACK_PATHS: Final[frozenset[str]] = frozenset(
    {
        # api/routes/mappings.py, api/routes/datasets.py (Phase 2 plan §9, M12): the exact contract
        # this panel was commissioned against, transcribed here so a typo fails before that sibling
        # PR lands rather than only after it - see the module docstring.
        "/clients/{}/mappings/suggest",
        "/clients/{}/mappings/{}",
        "/clients/{}/onboarding-specs",
        "/clients/{}/onboarding-specs/{}/preview",
        "/datasets",
        "/datasets/{}",
        "/datasets/{}/report",
    }
)


FASTAPI_PARAM: Final[re.Pattern[str]] = re.compile(r"\{[^}]+\}")


def _normalize_called(path: str) -> str:
    """A JS template literal's `${...}` interpolation, collapsed to `{}` - `api.js`'s own params."""
    return PARAM.sub("{}", path).split("?")[0]


def _normalize_served(path: str) -> str:
    """A FastAPI route's own `{name}` path parameter, collapsed to `{}` the same way."""
    return FASTAPI_PARAM.sub("{}", path).split("?")[0]


def served_paths() -> frozenset[str]:
    """The onboarding routers' own `/openapi.json` paths, for whichever of them already exist.

    A bare `FastAPI()` carrying only these routers - never `api.main.create_app()` - because the
    phase routers are not mounted into that app yet (`PARALLEL_WORK_PROTOCOL.md` §3: registering
    them is the orchestrator's job, reported, not done here); this checks the routers' own declared
    surface directly, which is real regardless of when that registration lands.
    """
    from fastapi import FastAPI

    app = FastAPI()
    from api.routes import clients as clients_routes
    from api.routes import sources as sources_routes

    app.include_router(clients_routes.router)
    app.include_router(sources_routes.router)
    for module_name in ("mappings", "datasets"):
        try:
            module = importlib.import_module(f"api.routes.{module_name}")
        except ImportError:
            continue
        app.include_router(module.router)
    paths = {_normalize_served(path) for path in app.openapi()["paths"]}
    paths.add("/openapi.json")  # FastAPI serves this itself; it is not a route the app declares
    return frozenset(paths)


def called_paths() -> frozenset[str]:
    text = code_of("api.js")
    literals = {_normalize_called(match) for match in PATH_LITERAL.findall(text)}
    return frozenset(path for path in literals if path.startswith("/") and path != "/")


def test_every_fetch_call_targets_a_url_the_api_defines() -> None:
    called = called_paths()
    assert called, "no API path literal was found in api.js"
    known = served_paths() | FALLBACK_PATHS
    missing = sorted(path for path in called if path not in known)
    assert not missing, f"api.js calls endpoints this API does not define: {missing}"


def test_the_fallback_contract_itself_is_exactly_what_api_js_relies_on() -> None:
    """`FALLBACK_PATHS` is a transcription, and a transcription can drift from the code it stands in
    for. This pins it the other way round: every path `api.js` calls that real introspection could
    not yet confirm must be *in* the fallback list, not silently passing because the list is a
    superset with room to spare."""
    called = called_paths()
    covered_for_real = served_paths()
    uncovered = called - covered_for_real
    assert uncovered, "every path was confirmed by a live router; FALLBACK_PATHS is untested dead weight"
    assert uncovered <= FALLBACK_PATHS, uncovered - FALLBACK_PATHS


# ---------------------------------------------------------------------------
# Every module `api.js` imports something from actually exists (sanity: no typo'd relative import)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", MODULES)
def test_relative_imports_resolve_to_a_real_file(name: str) -> None:
    text = code_of(name)
    for match in re.finditer(r"""from\s+["'](\.\.?/[A-Za-z0-9_./-]+)["']""", text):
        target = (ONBOARDING_DIR / match.group(1)).resolve()
        assert target.is_file(), f"{name} imports {match.group(1)}, which does not exist"
