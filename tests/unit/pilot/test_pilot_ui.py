"""The Plan E pilot UI module (`ui/modules/pilot/`, M59-M64), checked without a browser.

Static checks in the manner of `tests/unit/uplift/test_uplift_ui.py` and
`tests/integration/production/test_production_ui.py`; they pin what a regression could break silently
in a browser:

* the module holds exactly its files, each parses (`node --check`, skipped with the reason where
  there is no node), and every relative import resolves to a file under `ui/` that is either one of
  the shared primitives (`dom.js`, `api.js`, `modules/router.js`) or the module's own;
* every endpoint `pilot/api.js` calls is served by this API - path parameters normalised, query
  parameters it sends declared on the route, and every report format a screen asks for accepted;
* every JSON field a screen reads from a response (and every field it sends in a body) is on that
  route's schema, so renaming a model field fails here rather than rendering `undefined`;
* `index.html` loads the module once, from its PLAN-E block, after `app.js`, and `router.js` does not
  import it back;
* no prototype sample value, no placeholder other than the em dash, and no hand-typed number reaches
  the rendered markup (the rules `tests/integration/test_ui.py` and `tests/unit/test_onboarding_ui.py`
  hold the other screens to).
"""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
import typing
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest

from api.main import create_app
from engine.pilot.data_request import DataRequest
from engine.pilot.demo import DemoManifest
from engine.pilot.document import ReportDocument
from engine.pilot.feedback import FeedbackCategory
from tests.integration.test_ui import PLACEHOLDERS, SAMPLE_VALUES

REPO: Final[Path] = Path(__file__).resolve().parents[3]
UI_DIR: Final[Path] = REPO / "ui"
MODULE_DIR: Final[Path] = UI_DIR / "modules" / "pilot"
MODULE: Final[str] = "modules/pilot"

EXPECTED_FILES: Final[frozenset[str]] = frozenset(
    {"api.js", "feedback.js", "help.js", "index.js", "screen.js", "styles.js", "tour.js"}
)

SHARED_IMPORTS: Final[frozenset[str]] = frozenset({"dom.js", "api.js", "modules/router.js"})
"""What outside its own folder the module may import (paths relative to `ui/`)."""

API_PREFIXES: Final[tuple[str, ...]] = ("/pilot", "/datasets", "/runs", "/models", "/clients", "/auth")

RUPEE: Final[str] = "₹"
PILOT_SAMPLE_VALUES: Final[tuple[str, ...]] = tuple(v for v in SAMPLE_VALUES if v != RUPEE)
"""`test_ui.py`'s list minus the rupee sign. The prototype printed made-up rupee figures, which is why
the sign is on that list; the pilot's value form legitimately labels its three money inputs "(₹)"
because `RoiInputs.currency` is fixed to INR - the sign names a unit the person types a figure in, not a
figure. `test_the_rupee_sign_only_labels_the_money_inputs` pins it to exactly that use."""

COMMA_GROUPED_NUMBER: Final[re.Pattern[str]] = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
INTERPOLATED_NUMBER: Final[re.Pattern[str]] = re.compile(r"\$\{\s*-?\d[\d._]*\s*\}")
"""`tests/unit/test_onboarding_ui.py`'s two rules: `12,345` can only be formatted output, and `${12}` is a
figure the file chose rather than one the API measured."""

ANY_IMPORT: Final[re.Pattern[str]] = re.compile(
    r"""(?:\bfrom|^import)\s+["'](\.\.?/[A-Za-z0-9_./-]+)["']""", re.M
)
LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"(?<![:\"'`])//[^\n]*")
BLOCK_COMMENT: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_LITERAL: Final[re.Pattern[str]] = re.compile(r"`([^`]*)`|\"([^\"\n]*)\"|'([^'\n]*)'")
QUERY_HELPER: Final[re.Pattern[str]] = re.compile(r"\$\{q\(\{([^}]*)\}\)\}")
INTERPOLATION: Final[re.Pattern[str]] = re.compile(r"\$\{[^}]*\}")
REPORT_CALL: Final[re.Pattern[str]] = re.compile(
    r"\b(readinessUrl|resultsUrl|roiUrl)\([^()]*?,\s*\"(\w+)\"\)"
)
REPORT_ROUTE: Final[dict[str, str]] = {
    "readinessUrl": "/pilot/readiness/{dataset_id}",
    "resultsUrl": "/pilot/results",
    "roiUrl": "/pilot/roi/{run_id}",
}

PLAN_E_START: Final[str] = "<!-- ---- PLAN-E (pilot) — append only below this line ---- -->"
PLAN_E_END: Final[str] = "<!-- ---- END PLAN-E ---- -->"
ROUTER_START: Final[str] = "// ---- PLAN-E (pilot) — append only below this line ----"
ROUTER_END: Final[str] = "// ---- END PLAN-E ----"

NODE: Final[str | None] = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed; `node --check` needs it")

Path_ = tuple[str, ...]
"""A field path into a response: property names, `[]` for an array's items, `{}` for a map's values."""

FIELD_READS: Final[tuple[tuple[str, str, str, str, str, Path_], ...]] = (
    # (file, snippet the file must contain, method, route, "response" | "body", field path)
    ("screen.js", "request.value.tables", "GET", "/pilot/data-request", "response", ("tables",)),
    ("screen.js", "t.title", "GET", "/pilot/data-request", "response", ("tables", "[]", "title")),
    ("screen.js", "t.need", "GET", "/pilot/data-request", "response", ("tables", "[]", "need")),
    ("screen.js", "t.role", "GET", "/pilot/data-request", "response", ("tables", "[]", "role")),
    ("screen.js", "view.value.inputs", "GET", "/pilot/roi/{run_id}", "response", ("inputs",)),
    ("screen.js", "demo.demo_mode", "GET", "/pilot/demo", "response", ("demo_mode",)),
    ("screen.js", "demo.seeded", "GET", "/pilot/demo", "response", ("seeded",)),
    ("screen.js", "demo.how_to_seed", "GET", "/pilot/demo", "response", ("how_to_seed",)),
    ("screen.js", "demo.manifest", "GET", "/pilot/demo", "response", ("manifest",)),
    ("screen.js", "m.broken_dataset_id", "GET", "/pilot/demo", "response", ("manifest", "broken_dataset_id")),
    ("screen.js", "datasets.value.datasets", "GET", "/datasets", "response", ("datasets",)),
    ("screen.js", "d.dataset_id", "GET", "/datasets", "response", ("datasets", "[]", "dataset_id")),
    ("screen.js", "d.use_case", "GET", "/datasets", "response", ("datasets", "[]", "use_case")),
    ("screen.js", "d.n_rows", "GET", "/datasets", "response", ("datasets", "[]", "n_rows")),
    ("screen.js", "d.built_at", "GET", "/datasets", "response", ("datasets", "[]", "built_at")),
    ("screen.js", "models.value.versions", "GET", "/models", "response", ("versions",)),
    ("screen.js", "v.is_champion", "GET", "/models", "response", ("versions", "[]", "is_champion")),
    (
        "screen.js",
        "c.version.use_case_id",
        "GET",
        "/models",
        "response",
        ("versions", "[]", "version", "use_case_id"),
    ),
    (
        "screen.js",
        "c.version.version",
        "GET",
        "/models",
        "response",
        ("versions", "[]", "version", "version"),
    ),
    (
        "screen.js",
        "c.version.model_display_name",
        "GET",
        "/models",
        "response",
        ("versions", "[]", "version", "model_display_name"),
    ),
    ("screen.js", "runs.value.runs", "GET", "/runs", "response", ("runs",)),
    ("screen.js", "r.state", "GET", "/runs", "response", ("runs", "[]", "state")),
    ("screen.js", "r.use_case_name", "GET", "/runs", "response", ("runs", "[]", "use_case_name")),
    ("screen.js", "r.run_id", "GET", "/runs", "response", ("runs", "[]", "run_id")),
    ("screen.js", "r.finished_at", "GET", "/runs", "response", ("runs", "[]", "finished_at")),
    ("screen.js", "r.created_at", "GET", "/runs", "response", ("runs", "[]", "created_at")),
    ("index.js", "demo.demo_mode", "GET", "/pilot/demo", "response", ("demo_mode",)),
    ("index.js", "demo.seeded", "GET", "/pilot/demo", "response", ("seeded",)),
    ("index.js", "demo.manifest.client_name", "GET", "/pilot/demo", "response", ("manifest", "client_name")),
    ("tour.js", "demo.demo_mode", "GET", "/pilot/demo", "response", ("demo_mode",)),
    ("tour.js", "demo.manifest", "GET", "/pilot/demo", "response", ("manifest",)),
    ("tour.js", "m.use_case_id", "GET", "/pilot/demo", "response", ("manifest", "use_case_id")),
    ("tour.js", "m.train_run_id", "GET", "/pilot/demo", "response", ("manifest", "train_run_id")),
    ("tour.js", "m.score_run_id", "GET", "/pilot/demo", "response", ("manifest", "score_run_id")),
    ("help.js", "catalogue.codes[", "GET", "/pilot/help", "response", ("codes", "{}")),
    ("help.js", "catalogue.settings[", "GET", "/pilot/help", "response", ("settings", "{}")),
    ("help.js", "entry.title", "GET", "/pilot/help", "response", ("codes", "{}", "title")),
    ("help.js", "entry.meaning", "GET", "/pilot/help", "response", ("codes", "{}", "meaning")),
    ("help.js", "entry.fix", "GET", "/pilot/help", "response", ("codes", "{}", "fix")),
    ("help.js", "entry.meaning", "GET", "/pilot/help", "response", ("settings", "{}", "meaning")),
    ("help.js", "codes[code].title", "GET", "/pilot/help", "response", ("codes", "{}", "title")),
    ("feedback.js", "saved.redacted", "POST", "/pilot/feedback", "response", ("redacted",)),
    ("feedback.js", "screen: route()", "POST", "/pilot/feedback", "body", ("screen",)),
    ("feedback.js", "category,", "POST", "/pilot/feedback", "body", ("category",)),
    ("feedback.js", "text }", "POST", "/pilot/feedback", "body", ("text",)),
    # v1 (WP8): names, clients, tags and verdicts instead of ids
    ("screen.js", "r.client_id", "GET", "/runs", "response", ("runs", "[]", "client_id")),
    ("screen.js", "r.use_case_id", "GET", "/runs", "response", ("runs", "[]", "use_case_id")),
    ("screen.js", "d.client_id", "GET", "/datasets", "response", ("datasets", "[]", "client_id")),
    ("screen.js", "d.target", "GET", "/datasets", "response", ("datasets", "[]", "target")),
    (
        "screen.js",
        "c.version.approved_at",
        "GET",
        "/models",
        "response",
        ("versions", "[]", "version", "approved_at"),
    ),
    (
        "screen.js",
        "c.version.created_at",
        "GET",
        "/models",
        "response",
        ("versions", "[]", "version", "created_at"),
    ),
    ("screen.js", "clients.value.clients", "GET", "/clients", "response", ("clients",)),
    ("screen.js", "cl.client_id", "GET", "/clients", "response", ("clients", "[]", "client_id")),
    ("screen.js", "found.name", "GET", "/clients", "response", ("clients", "[]", "name")),
    ("screen.js", "payload.industries", "GET", "/industries", "response", ("industries",)),
    ("screen.js", "industry.stages", "GET", "/industries", "response", ("industries", "[]", "stages")),
    (
        "screen.js",
        "stage.use_cases",
        "GET",
        "/industries",
        "response",
        ("industries", "[]", "stages", "[]", "use_cases"),
    ),
    (
        "screen.js",
        "u.ai_type",
        "GET",
        "/industries",
        "response",
        ("industries", "[]", "stages", "[]", "use_cases", "[]", "ai_type"),
    ),
    (
        "screen.js",
        "u.status",
        "GET",
        "/industries",
        "response",
        ("industries", "[]", "stages", "[]", "use_cases", "[]", "status"),
    ),
    ("screen.js", "m.client_name", "GET", "/pilot/demo", "response", ("manifest", "client_name")),
    ("screen.js", "m.broken_client_id", "GET", "/pilot/demo", "response", ("manifest", "broken_client_id")),
    ("screen.js", "m.campaigns", "GET", "/pilot/demo", "response", ("manifest", "campaigns")),
    (
        "screen.js",
        "c.score_run_id",
        "GET",
        "/pilot/demo",
        "response",
        ("manifest", "campaigns", "[]", "score_run_id"),
    ),
    (
        "screen.js",
        "campaign.title",
        "GET",
        "/pilot/demo",
        "response",
        ("manifest", "campaigns", "[]", "title"),
    ),
    ("screen.js", "v.status", "GET", "/pilot/roi/{run_id}", "response", ("status",)),
    ("screen.js", "v.use_case_id", "GET", "/pilot/roi/{run_id}", "response", ("use_case_id",)),
    (
        "screen.js",
        "v.results_available_on",
        "GET",
        "/pilot/roi/{run_id}",
        "response",
        ("results_available_on",),
    ),
    (
        "screen.js",
        "view.value.outcome_is_good",
        "GET",
        "/pilot/roi/{run_id}",
        "response",
        ("outcome_is_good",),
    ),
    ("screen.js", "view.benefit_label", "GET", "/pilot/roi/{run_id}", "response", ("benefit_label",)),
    ("screen.js", "b.low", "GET", "/pilot/roi/{run_id}", "response", ("benefit", "low")),
    ("screen.js", "view.net_value", "GET", "/pilot/roi/{run_id}", "response", ("net_value", "high")),
    ("screen.js", "view.summary", "GET", "/pilot/roi/{run_id}", "response", ("summary",)),
    ("screen.js", "doc.facts", "GET", "/pilot/readiness/{dataset_id}", "response", ("facts",)),
    ("screen.js", "doc.facts", "GET", "/pilot/results", "response", ("facts",)),
    ("screen.js", "report.blocks", "GET", "/pilot/readiness/{dataset_id}", "response", ("blocks",)),
    (
        "screen.js",
        "verdict.state",
        "GET",
        "/pilot/readiness/{dataset_id}",
        "response",
        ("blocks", "[]", "state"),
    ),
    (
        "screen.js",
        "report.client_name",
        "GET",
        "/pilot/readiness/{dataset_id}",
        "response",
        ("client_name",),
    ),
    ("screen.js", "report.client_name", "GET", "/pilot/results", "response", ("client_name",)),
    ("api.js", "me.permissions", "GET", "/auth/me", "response", ("permissions",)),
    ("api.js", "p.method", "GET", "/auth/me", "response", ("permissions", "[]", "method")),
    ("api.js", "p.path", "GET", "/auth/me", "response", ("permissions", "[]", "path")),
    ("api.js", "found.allowed", "GET", "/auth/me", "response", ("permissions", "[]", "allowed")),
)
"""Every field the screens read from (or send to) an endpoint, beside the code that does it."""

UNTYPED_JSON: Final[dict[tuple[str, str], type[Any]]] = {
    ("GET", "/pilot/data-request"): DataRequest,
    ("GET", "/pilot/readiness/{dataset_id}"): ReportDocument,
    ("GET", "/pilot/results"): ReportDocument,
}
"""Routes that answer JSON without declaring its schema in OpenAPI (a `Response` built by hand, because
the same route also answers Markdown): the model the JSON is dumped from stands in."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    return create_app().openapi()


def module_files() -> list[Path]:
    return sorted(MODULE_DIR.glob("*.js"))


def read(name: str) -> str:
    return (MODULE_DIR / name).read_text(encoding="utf-8")


def code_of(name: str) -> str:
    """A module with its comments removed, so prose is not mistaken for code."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", read(name)))


def imports_of(name: str) -> list[str]:
    base = PurePosixPath(MODULE) / name
    return [posixpath.normpath(str(base.parent / target)) for target in ANY_IMPORT.findall(code_of(name))]


def between(text: str, start: str, end: str) -> str:
    begin = text.index(start) + len(start)
    return text[begin : text.index(end, begin)]


def normalise(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def api_calls() -> dict[str, set[str]]:
    """Every API path `api.js` builds (parameters as `{}`), with the query keys it sends to it."""
    calls: dict[str, set[str]] = {}
    for match in STRING_LITERAL.finditer(code_of("api.js")):
        literal = next(group for group in match.groups() if group is not None)
        if not literal.startswith(API_PREFIXES):
            continue
        keys = {
            part.split(":")[0].strip()
            for found in QUERY_HELPER.findall(literal)
            for part in found.split(",")
            if part.strip()
        }
        bare = QUERY_HELPER.sub("", literal)
        path, _, query = INTERPOLATION.sub("{}", bare).partition("?")
        keys |= {pair.split("=")[0] for pair in query.split("&") if pair}
        calls.setdefault(path, set()).update(keys)
    return calls


def served(openapi: dict[str, Any]) -> dict[str, str]:
    """Normalised path -> the path as the API declares it."""
    return {normalise(path): path for path in openapi["paths"]}


def resolve(doc: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """A schema flattened: `$ref`s followed, `anyOf`/`oneOf`/`allOf` spread, `null` dropped."""
    while "$ref" in schema:
        node: Any = doc
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    out: list[dict[str, Any]] = []
    for key in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(key, []):
            out.extend(resolve(doc, option))
    if not out and schema.get("type") != "null":
        out.append(schema)
    return out


def has_path(doc: dict[str, Any], schema: dict[str, Any], path: Path_) -> bool:
    if not path:
        return True
    head, rest = path[0], path[1:]
    for option in resolve(doc, schema):
        if head == "[]":
            child = option.get("items")
        elif head == "{}":
            child = option.get("additionalProperties")
        else:
            child = option.get("properties", {}).get(head)
        if isinstance(child, dict) and has_path(doc, child, rest):
            return True
    return False


def schema_of(
    openapi: dict[str, Any], method: str, route: str, where: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(document to resolve refs in, schema)` of a route's JSON response or JSON body."""
    operation = openapi["paths"][route][method.lower()]
    if where == "body":
        content = operation["requestBody"]["content"]
    else:
        ok = next(code for code in ("200", "201") if code in operation["responses"])
        content = operation["responses"][ok].get("content", {})
    schema = content.get("application/json", {}).get("schema")
    if schema:
        return openapi, schema
    model = UNTYPED_JSON[(method, route)]
    standalone = model.model_json_schema()
    return standalone, standalone


def roi_input_names() -> list[str]:
    block = re.search(r"const INPUTS = \[(.*?)\n\];", code_of("screen.js"), re.DOTALL)
    assert block is not None, "INPUTS was not found in screen.js"
    names = re.findall(r"\[\s*\"(\w+)\",", block.group(1))
    assert names, "INPUTS names no field"
    return names


# ---------------------------------------------------------------------------
# (1) files, (8) parse, (2) imports
# ---------------------------------------------------------------------------
def test_the_module_holds_exactly_its_files() -> None:
    assert {p.name for p in module_files()} == EXPECTED_FILES


@needs_node
@pytest.mark.parametrize("name", sorted(EXPECTED_FILES))
def test_every_module_file_parses(name: str) -> None:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--check", str(MODULE_DIR / name)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr


def test_every_import_resolves_to_a_shared_primitive_or_the_module_itself() -> None:
    seen = 0
    for name in sorted(EXPECTED_FILES):
        for imported in imports_of(name):
            seen += 1
            assert (UI_DIR / imported).is_file(), f"{name} imports {imported}, which does not exist"
            own = PurePosixPath(imported).parent == PurePosixPath(MODULE)
            assert (
                own or imported in SHARED_IMPORTS
            ), f"{name} imports {imported} from outside ui/'s primitives"
    assert seen, "no import was found in the module"


# ---------------------------------------------------------------------------
# (3) the API it calls
# ---------------------------------------------------------------------------
def test_every_endpoint_the_module_calls_exists(openapi: dict[str, Any]) -> None:
    calls = api_calls()
    assert {"/pilot/help", "/pilot/demo", "/pilot/roi/{}", "/datasets", "/runs", "/models"} <= set(calls)
    known = served(openapi)
    missing = sorted(path for path in calls if path not in known)
    assert not missing, f"pilot/api.js calls endpoints this API does not serve: {missing}"


def test_the_screens_reach_every_pilot_route(openapi: dict[str, Any]) -> None:
    pilot_routes = {path for path in served(openapi) if path.startswith("/pilot")}
    assert pilot_routes, "the API serves no /pilot route"
    unreached = sorted(pilot_routes - set(api_calls()))
    assert not unreached, f"no screen reaches: {unreached}"


def test_every_query_parameter_sent_is_declared(openapi: dict[str, Any]) -> None:
    known = served(openapi)
    checked = 0
    for path, keys in api_calls().items():
        if not keys:
            continue
        operation = openapi["paths"][known[path]]["get"]
        declared = {p["name"] for p in operation.get("parameters", []) if p["in"] == "query"}
        assert keys <= declared, f"{path} is sent {sorted(keys - declared)}, which it does not declare"
        checked += 1
    assert checked >= 4, "the query helper calls were not found in api.js"


def test_every_report_format_a_screen_asks_for_is_accepted(openapi: dict[str, Any]) -> None:
    asked = REPORT_CALL.findall(code_of("screen.js"))
    assert {helper for helper, _ in asked} == set(REPORT_ROUTE)
    for helper, fmt in asked:
        parameters = openapi["paths"][REPORT_ROUTE[helper]]["get"]["parameters"]
        schema = next(p["schema"] for p in parameters if p["name"] == "format")
        assert fmt in schema["enum"], f"{helper}(..., {fmt!r}) asks for a format the route refuses"


# ---------------------------------------------------------------------------
# (7) the fields it reads and sends
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "snippet", "method", "route", "where", "path"),
    FIELD_READS,
    ids=[f"{row[0]}:{row[3]}:{'.'.join(row[5])}" for row in FIELD_READS],
)
def test_every_field_a_screen_reads_is_on_the_response(
    openapi: dict[str, Any], name: str, snippet: str, method: str, route: str, where: str, path: Path_
) -> None:
    assert snippet in code_of(name), f"{name} no longer contains {snippet!r}: update FIELD_READS"
    doc, schema = schema_of(openapi, method, route, where)
    assert has_path(doc, schema, path), f"{name} reads {'.'.join(path)} from {method} {route}, which lacks it"


def test_the_value_form_reads_and_sends_only_roi_input_fields(openapi: dict[str, Any]) -> None:
    """`RoiInputs` forbids extra fields: a form field it does not know would make every save a 422."""
    get_doc, view = schema_of(openapi, "GET", "/pilot/roi/{run_id}", "response")
    put_doc, body = schema_of(openapi, "PUT", "/pilot/roi/{run_id}", "body")
    for field in roi_input_names():
        assert has_path(get_doc, view, ("inputs", field)), f"RoiView.inputs has no {field}"
        assert has_path(put_doc, body, (field,)), f"PUT /pilot/roi takes no {field}"


def test_the_feedback_categories_are_the_ones_the_api_accepts() -> None:
    offered = re.findall(r"\[\s*\"(\w+)\",\s*\"", between(code_of("feedback.js"), "const CATEGORIES", "];"))
    assert sorted(offered) == sorted(typing.get_args(FeedbackCategory))


def test_the_demo_raw_variants_linked_are_the_ones_the_seed_makes() -> None:
    linked = set(re.findall(r"demoRawUrl\(\"(\w+)\"\)", code_of("screen.js")))
    assert linked, "screen.js links no demo raw variant"
    assert linked <= set(DemoManifest.model_fields["raw_variants"].default)


# ---------------------------------------------------------------------------
# (4) registration
# ---------------------------------------------------------------------------
def test_index_html_loads_the_module_once_from_the_plan_e_block() -> None:
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    inside = between(html, PLAN_E_START, PLAN_E_END)
    assert inside.strip() == '<script type="module" src="./modules/pilot/index.js"></script>'
    assert html.count("modules/pilot/index.js") == 1
    assert html.count("modules/pilot/") == 1, "index.html loads only the module's entry point"
    assert html.index('src="./app.js"') < html.index('src="./modules/pilot/index.js"')


def test_the_router_does_not_import_the_module_back() -> None:
    router = (UI_DIR / "modules" / "router.js").read_text(encoding="utf-8")
    inside = LINE_COMMENT.sub("", between(router, ROUTER_START, ROUTER_END))
    assert "import" not in inside, "router.js must not import the pilot module (index.js imports router.js)"
    assert "pilot/" not in LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", router))


# ---------------------------------------------------------------------------
# (5) sample values and placeholders, (6) hand-typed numbers
# ---------------------------------------------------------------------------
def test_no_prototype_sample_value_reaches_the_module() -> None:
    leaked = [(p.name, v) for p in module_files() for v in PILOT_SAMPLE_VALUES if v in p.read_text("utf-8")]
    assert not leaked, f"prototype sample values reached the pilot module: {leaked}"


def test_the_rupee_sign_only_labels_the_money_inputs() -> None:
    for path in module_files():
        text = path.read_text(encoding="utf-8")
        if path.name != "screen.js":
            assert RUPEE not in text, f"{path.name} prints a rupee sign"
    inputs = between(read("screen.js"), "const INPUTS = [", "\n];")
    assert read("screen.js").count(RUPEE) == inputs.count(f'({RUPEE})"') == 3


def test_the_em_dash_is_the_only_placeholder() -> None:
    for path in module_files():
        text = path.read_text(encoding="utf-8")
        for placeholder in PLACEHOLDERS:
            assert placeholder not in text, f"{path.name} uses {placeholder} instead of the em dash"


def test_no_comma_grouped_number_is_hand_typed() -> None:
    for name in sorted(EXPECTED_FILES):
        found = COMMA_GROUPED_NUMBER.findall(code_of(name))
        assert not found, f"{name} has a literal number that can only be formatted output: {found}"


def test_no_bare_number_is_interpolated_into_the_page() -> None:
    for name in sorted(EXPECTED_FILES):
        found = INTERPOLATED_NUMBER.findall(code_of(name))
        assert not found, f"{name} interpolates hand-typed numbers into its markup: {found}"
