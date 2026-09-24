"""The onboarding panel (`ui/modules/onboarding/`) against the parity properties `test_ui.py` and
`test_prototype_parity.py` already hold the wired Phase 1 screens to (Phase 2 plan §10, M13).

This is a static-analysis test, on purpose: the panel is plain ES modules with no build step, so
there is nothing to import and exercise from Python. What it refuses to be, though, is a test of the
panel against a transcript of the panel. Every property below is pinned to something outside the
three files it is checking - a router's own OpenAPI surface, a pydantic model's own field set, an
enum's own members - so the only way to make one of these pass is to agree with the API, not to
agree with whatever `steps.js` happens to say today:

* the module shape a host imports (`onboardingPanel`, the four steps, every fetch wrapper);
* every path `api.js` fetches is a path one of the four onboarding routers actually declares;
* every `components.schemas` entry the generated forms read exists, and carries choices;
* every request body the panel builds has exactly the fields its request model declares;
* every response field the panel reads is a real field of the model that answers it;
* every enum value the panel branches on - a severity, a run state, a label type, a role kind - is a
  real member of that enum;
* nothing here fabricates a number: no comma-grouped figure, no numeric literal interpolated into
  the page, no placeholder but the one em dash `dom.js` defines (house rule 2, plan §13.3).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from api.routes import clients as clients_routes
from api.routes import datasets as datasets_routes
from api.routes import mappings as mappings_routes
from api.routes import sources as sources_routes
from engine.config import (
    AggFunction,
    DecidedBy,
    LabelType,
    RoleKind,
    SnapshotFrequency,
    SnapshotMode,
    WhereOp,
    get_roles,
)
from engine.contracts import RunState, Severity

ONBOARDING_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "ui" / "modules" / "onboarding"

MODULES: Final[tuple[str, ...]] = ("api.js", "panel.js", "steps.js")

LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"//[^\n]*")
BLOCK_COMMENT: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
PATH_LITERAL: Final[re.Pattern[str]] = re.compile(r"[`\"'](/[A-Za-z0-9_${}().,/-]*)[`\"']")
PARAM: Final[re.Pattern[str]] = re.compile(r"\$\{[^}]+\}")
FASTAPI_PARAM: Final[re.Pattern[str]] = re.compile(r"\{[^}]+\}")

# A number with thousands separators - "12,345" - can only be the *output* of `fmtInt`'s
# `toLocaleString`; nobody hand-types one into a template literal, so its presence anywhere in the
# source is exactly the fabricated-placeholder pattern plan §13.3 forbids (the task's own example).
COMMA_GROUPED_NUMBER: Final[re.Pattern[str]] = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")

# `${12}` - a bare number interpolated straight into rendered HTML. Every number on this screen comes
# out of an API response; one typed into the markup is a sample figure by definition. The step
# number a `stepShell(...)` call passes is the one exception and is a parameter, not a literal here.
INTERPOLATED_NUMBER: Final[re.Pattern[str]] = re.compile(r"\$\{\s*-?\d[\d._]*\s*\}")

PLACEHOLDERS: Final[tuple[str, ...]] = ('"N/A"', "'N/A'", '"TBD"', '">--<"', '"n/a"')
"""The stand-ins `dom.js`'s em dash replaces (`tests/integration/test_ui.py`'s own list) - none of
them may appear here either; there is exactly one placeholder for "nobody measured this"."""


def read(name: str) -> str:
    return (ONBOARDING_DIR / name).read_text(encoding="utf-8")


def code_of(name: str) -> str:
    """One module with its comments stripped, so a path or a number mentioned in prose is never
    mistaken for one actually reachable at runtime - the same precaution `test_ui.py` takes."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", read(name)))


@pytest.fixture(scope="module")
def onboarding_app() -> FastAPI:
    """A bare `FastAPI()` carrying only the four onboarding routers.

    Never `api.main.create_app()`: mounting these routers into that app is the orchestrator's step
    (`PARALLEL_WORK_PROTOCOL.md` §4), and this test is about what the routers themselves declare,
    which is true before that registration lands and stays true after it.
    """
    app = FastAPI()
    for module in (clients_routes, sources_routes, mappings_routes, datasets_routes):
        app.include_router(module.router)
    return app


@pytest.fixture(scope="module")
def openapi(onboarding_app: FastAPI) -> dict[str, object]:
    document: dict[str, object] = onboarding_app.openapi()
    return document


def components(openapi: dict[str, object]) -> dict[str, dict[str, object]]:
    schemas = openapi.get("components", {})
    assert isinstance(schemas, dict)
    result = schemas.get("schemas", {})
    assert isinstance(result, dict)
    return result


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


@pytest.mark.parametrize("name", MODULES)
def test_relative_imports_resolve_to_a_real_file(name: str) -> None:
    text = code_of(name)
    for match in re.finditer(r"""from\s+["'](\.\.?/[A-Za-z0-9_./-]+)["']""", text):
        target = (ONBOARDING_DIR / match.group(1)).resolve()
        assert target.is_file(), f"{name} imports {match.group(1)}, which does not exist"


# ---------------------------------------------------------------------------
# Nothing fabricated: house rule 2, plan §13.3
# ---------------------------------------------------------------------------
def test_no_comma_grouped_number_is_hand_typed() -> None:
    for name in MODULES:
        text = code_of(name)
        found = COMMA_GROUPED_NUMBER.findall(text)
        assert not found, f"{name} has a literal number that can only be `fmtInt` output: {found}"


def test_no_bare_number_is_interpolated_into_the_page() -> None:
    """`${12}` in a template literal is a figure this file chose; every figure on this screen is one
    the API measured, reached through `dash()` so an unmeasured one is an em dash instead."""
    text = code_of("steps.js")
    found = INTERPOLATED_NUMBER.findall(text)
    assert not found, f"steps.js interpolates hand-typed numbers into its markup: {found}"


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


def test_a_confidence_is_never_read_off_the_edited_copy() -> None:
    """The one number on the mapping screen that a user's own edit could invent.

    `MappingColumn.confidence` is a required float, so a column the user picked by hand has to carry
    *some* value for `PUT /clients/{id}/mappings/{mid}` to accept the body - and that value was
    measured by nobody. The pill therefore reads its number out of `state.mappingSuggested`, the
    untouched `MappingSpec` the API returned, which is what makes the displayed percentage a
    measurement rather than an artefact of the edit that caused it.
    """
    text = code_of("steps.js")
    body = text[text.index("function confidencePill(") :]
    body = body[: body.index("\nfunction ", 1)]
    assert "suggested" in body, "confidencePill does not consult the API's own suggestion"
    assert "decided.confidence" not in body, "confidencePill prints the working copy's confidence"


# ---------------------------------------------------------------------------
# The generated forms are generated: their vocabulary is the API's, not this file's
# ---------------------------------------------------------------------------
def test_no_offered_vocabulary_member_is_hand_typed() -> None:
    """The "Add feature" form offers a function and a filter operator. Not one member of either
    enum may appear in `steps.js`: the choices are `schemaEnum("AggFunction")`'s and
    `schemaEnum("WhereOp")`'s at render time. The members are read from the enums themselves rather
    than sampled into this test, so one added to `engine/config.py` tomorrow is covered the moment
    it exists.

    `SnapshotMode` and `SnapshotFrequency` are deliberately not held to this: their *choices* are
    read from the schema too, but "only a periodic dataset has a frequency" is a branch in the form's
    shape, not an item in a list, and `test_the_snapshot_modes_the_panel_branches_on_are_real` holds
    that branch to the enum from the other side.
    """
    steps_text = code_of("steps.js")
    for enum in (AggFunction, WhereOp):
        for member in enum:
            assert (
                f'"{member.value}"' not in steps_text
            ), f"{enum.__name__}.{member.name} looks hand-typed into steps.js"


def test_the_snapshot_modes_the_panel_branches_on_are_real() -> None:
    source = code_of("steps.js") + code_of("panel.js")
    branched = set(re.findall(r'(?:snap|manifest)\.(?:snapshot_)?mode === "([a-z_]+)"', source))
    assert branched, "nothing in the panel tells a single snapshot from a periodic one"
    assert branched <= {member.value for member in SnapshotMode}
    frequencies = set(re.findall(r'\.frequency === "([a-z_]+)"', source))
    assert frequencies <= {member.value for member in SnapshotFrequency}


def test_the_generated_forms_read_their_choices_from_the_schema() -> None:
    panel_text = code_of("panel.js")
    for name in ("AggFunction", "WhereOp", "SnapshotMode", "SnapshotFrequency"):
        assert f'schemaEnum("{name}")' in panel_text, name
    assert 'schemaProperty("FeatureDef", "name")' in panel_text


def test_every_schema_component_the_panel_reads_exists_and_carries_choices(
    openapi: dict[str, object],
) -> None:
    """`schemaEnum("X")` returning `[]` means the API's document has no `X` - a renamed model, or
    routers that are not mounted. The form would then render an empty `<select>`, so the name it
    asks for has to be one these routers really publish, with members really in it."""
    schemas = components(openapi)
    asked = set(re.findall(r'schemaEnum\("([A-Za-z0-9_]+)"\)', code_of("panel.js")))
    assert asked, "panel.js reads no enum from the schema at all"
    for name in sorted(asked):
        assert name in schemas, f"panel.js reads {name} from /openapi.json, which does not define it"
        members = schemas[name].get("enum")
        assert members, f"{name} publishes no enum members for the form to offer"


def test_every_schema_property_the_panel_reads_exists(openapi: dict[str, object]) -> None:
    """The same, for `schemaProperty(model, field)`: a pattern, a bound or a default the form is
    generated from has to be a property the model really declares."""
    schemas = components(openapi)
    asked = set(re.findall(r'schemaProperty\("([A-Za-z0-9_]+)",\s*"([A-Za-z0-9_]+)"\)', code_of("panel.js")))
    assert asked, "panel.js reads no property from the schema at all"
    for model, field in sorted(asked):
        assert model in schemas, f"panel.js reads {model}.{field}, and {model} is not published"
        properties = schemas[model].get("properties", {})
        assert isinstance(properties, dict)
        assert field in properties, f"{model} has no property {field!r}"


def test_the_snapshot_bounds_are_the_schemas_own(openapi: dict[str, object]) -> None:
    """`max_snapshots` is the one numeric bound this screen puts on an input. It has to come from
    the model; a fallback written here would silently refuse a value the API accepts (or offer one
    it does not), which is a limit nobody set presented as the engine's."""
    properties = components(openapi)["SnapshotDefinition"]["properties"]
    assert isinstance(properties, dict)
    bound = properties["max_snapshots"]
    assert "minimum" in bound and "maximum" in bound and "default" in bound
    steps_text = code_of("steps.js")
    for literal in (str(bound["minimum"]), str(bound["maximum"]), str(bound["default"])):
        assert f"|| {literal}" not in steps_text, f"steps.js falls back to a hand-typed {literal}"


# ---------------------------------------------------------------------------
# Every request body has exactly the fields its request model declares
# ---------------------------------------------------------------------------
def object_literal_keys(text: str, start: str) -> frozenset[str]:
    """The top-level keys of the object literal that opens after `start`.

    Hand-scanned rather than regex-matched, because the three things that would fool a regex all
    occur in these files: a nested object (`feature_spec: {...}`) whose keys are not top-level, a
    shorthand property (`{ role }`) with no colon at all, and a `${...}` interpolation inside the
    path template literal that sits between the marker and the body. Keys are only read where a key
    can legally be - straight after the opening brace, or straight after a comma at that depth - so
    an identifier appearing as a *value* is never mistaken for one.
    """
    index = text.index(start) + len(start)
    while text[(index := text.index("{", index)) - 1] == "$":
        index += 1
    depth, expect_key, keys = 0, False, set()
    while index < len(text):
        char = text[index]
        if char in "\"'`":
            index = _skip_string(text, index)
            continue
        if char in "{[(":
            depth += 1
            expect_key = depth == 1 and char == "{"
            index += 1
            continue
        if char in "}])":
            depth -= 1
            if depth == 0:
                break
            index += 1
            continue
        if depth == 1 and char == ",":
            expect_key = True
            index += 1
            continue
        if expect_key and (char.isalpha() or char == "_"):
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[index:])
            assert match is not None
            keys.add(match.group(0))
            expect_key = False
            index += match.end()
            continue
        if not char.isspace():
            expect_key = False
        index += 1
    return frozenset(keys)


def _skip_string(text: str, index: int) -> int:
    """Past the string or template literal opening at `index`, escapes and `${...}` included."""
    quote, index = text[index], index + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if quote == "`" and text.startswith("${", index):
            depth, index = 1, index + 2
            while index < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                index += 1
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return index


def returned_object_keys(text: str, function_marker: str) -> frozenset[str]:
    """The top-level keys of the object a named function returns."""
    body = text[text.index(function_marker) :]
    return object_literal_keys(body, "return ")


def field_names(model: type[BaseModel]) -> frozenset[str]:
    return frozenset(model.model_fields)


def test_the_mapping_save_body_is_exactly_the_request_models_fields() -> None:
    """`MappingSaveRequest` forbids extras and names no `mapping_id`, `created_at` or `hash`: the URL
    owns the first and the store stamps the other two. Sending the whole `MappingSpec` back - the
    obvious thing to write - is a `422`, and sending less than the model requires is another."""
    sent = object_literal_keys(code_of("api.js"), "export const saveMapping =")
    assert sent == field_names(mappings_routes.MappingSaveRequest)


def test_the_onboarding_spec_body_is_exactly_the_request_models_fields() -> None:
    sent = returned_object_keys(code_of("panel.js"), "function specBody()")
    assert sent == field_names(datasets_routes.OnboardingSpecCreateRequest)


def test_the_dataset_build_body_is_accepted_by_its_request_model() -> None:
    """`DatasetBuildRequest`'s optional fields may be left out; its required ones may not, and no
    field it does not declare may be sent (it forbids extras like every request model here)."""
    sent = object_literal_keys(code_of("panel.js"), "await createDataset(")
    model = datasets_routes.DatasetBuildRequest
    required = {name for name, field in model.model_fields.items() if field.is_required()}
    assert required <= sent, f"missing required fields: {sorted(required - sent)}"
    assert sent <= field_names(model), f"fields the model forbids: {sorted(sent - field_names(model))}"


def test_the_mapping_suggest_body_is_exactly_the_request_models_fields() -> None:
    sent = object_literal_keys(code_of("api.js"), "export const suggestMapping =")
    assert sent == field_names(mappings_routes.MappingSuggestRequest)


def test_the_role_patch_body_is_exactly_the_request_models_fields() -> None:
    sent = object_literal_keys(code_of("api.js"), "export const setSourceRole =")
    assert sent == field_names(sources_routes.SourceRoleUpdate)


# ---------------------------------------------------------------------------
# Every response field the panel reads is a real field of the model that answers it
# ---------------------------------------------------------------------------
UI_READS: Final[dict[str, tuple[str, ...]]] = {
    "StandardSchemaResponse": ("standard_schema", "suggested_features", "label", "roles"),
    "StandardSchemaConfig": ("columns", "snapshot_column"),
    "StandardColumn": ("name", "type", "required", "description", "value_aliases", "derivable"),
    "RoleCatalogue": ("roles",),
    "RoleSpec": ("kind", "description", "required_columns", "typical_columns", "optional_columns"),
    "SourceListResponse": ("sources", "profiles"),
    "SourceSpec": ("source_id", "file_name", "role", "rows"),
    "SourceProfile": ("role_candidates", "key_candidates", "time_candidates", "profile"),
    "RoleCandidate": ("role",),
    "KeyCandidate": ("column", "coverage"),
    "TimeCandidate": ("column",),
    "DatasetProfile": ("columns",),
    "ColumnProfile": ("name", "inferred_type", "sample_values", "top_categories"),
    "CategoryCount": ("value",),
    "MappingSpec": ("mapping_id", "columns", "unmapped_source", "missing_required", "value_maps"),
    "MappingColumn": ("source", "standard", "confidence", "decided_by", "transform"),
    "MappingSaveResponse": ("checks",),
    "ValidationCheck": ("code", "severity", "message", "suggestion", "column"),
    "FeatureDef": ("name", "role", "function", "column", "window_days", "where", "description"),
    "LabelDefinition": ("name", "type", "horizon_days"),
    "SnapshotDefinition": ("mode", "frequency", "max_snapshots"),
    "OnboardingSpecCreateResponse": ("spec_id", "checks"),
    "PreviewResponse": ("rows", "per_snapshot", "feature_null_rates", "checks"),
    "DatasetCreatedResponse": ("dataset_id",),
    "DatasetGetResponse": ("manifest", "status"),
    "DatasetChecksResponse": ("checks",),
    "BuildStatus": ("state", "stages", "error", "detail"),
    "BuildStage": ("group_label", "state", "detail"),
    "BuildReport": (
        "sources",
        "snapshots",
        "features",
        "checks",
        "passed",
        "error_count",
        "warning_count",
        "leak_check",
    ),
    "LeakCheckRecord": ("scope", "summary"),
    "SourceStat": ("source_id", "role", "rows", "join_coverage"),
    "SnapshotStat": ("date", "entities", "positive_rate", "censored"),
    "FeatureStat": ("name", "null_fraction", "dropped", "reason"),
    "DatasetManifest": ("target", "columns", "primary_key", "snapshot_mode"),
    "DatasetColumn": ("name", "type"),
}
"""Every wire field the three modules reach for, by the model that sends it.

Pinned from both ends by the two tests below: each name has to be a field the published model really
declares, *and* has to appear in the JS. A field renamed in `engine/` fails the first; an entry that
has drifted into fiction because the screen stopped reading it fails the second. Neither direction
can pass by agreeing only with the other.
"""


def test_every_field_the_ui_reads_is_published_by_its_model(openapi: dict[str, object]) -> None:
    schemas = components(openapi)
    for model, fields in UI_READS.items():
        assert model in schemas, f"{model} is not published by any onboarding route"
        properties = schemas[model].get("properties", {})
        assert isinstance(properties, dict)
        unknown = sorted(set(fields) - set(properties))
        assert not unknown, f"the UI reads {model}.{unknown}, which {model} does not have"


def test_every_field_named_here_is_one_the_ui_actually_reads() -> None:
    source = "".join(code_of(name) for name in MODULES)
    for model, fields in UI_READS.items():
        for field in fields:
            assert field in source, f"{model}.{field} is listed here but read nowhere in the panel"


# ---------------------------------------------------------------------------
# Every enum value the panel branches on is a real member of that enum
# ---------------------------------------------------------------------------
def test_the_severities_the_panel_branches_on_are_real() -> None:
    text = code_of("steps.js")
    branched = set(re.findall(r'check\.severity === "([a-z_]+)"', text)) | set(
        re.findall(r'c\.severity === "([a-z_]+)"', text)
    )
    assert branched, "steps.js never distinguishes a blocking check from a warning"
    assert branched <= {member.value for member in Severity}


def test_the_run_states_the_panel_branches_on_are_real() -> None:
    source = code_of("steps.js") + code_of("panel.js")
    branched = set(re.findall(r'(?:status|s|stage)\.state === "([a-z_]+)"', source))
    branched |= set(re.findall(r'states\.includes\("([a-z_]+)"\)', source))
    assert branched, "nothing in the panel reads a build state"
    assert branched <= {member.value for member in RunState}


def test_the_label_phrases_cover_exactly_the_label_types() -> None:
    """The label sentence puts a phrase where the label's `type` goes. A type with no phrase would
    fall through to the raw enum value in the middle of an English sentence, so the map is held to
    the enum itself rather than to whatever the use cases happen to configure today."""
    text = code_of("steps.js")
    block = text[text.index("LABEL_TYPE_PHRASE = {") : text.index("};", text.index("LABEL_TYPE_PHRASE = {"))]
    phrased = set(re.findall(r"^\s*([a-z_]+):", block, re.MULTILINE))
    assert phrased == {member.value for member in LabelType}


def test_the_role_kind_and_decided_by_values_are_real() -> None:
    source = code_of("steps.js") + code_of("panel.js")
    kinds = set(re.findall(r'\.kind === "([a-z_]+)"', source))
    assert kinds, "nothing tells the entity table apart from an event log"
    assert kinds <= {member.value for member in RoleKind}
    decided = set(re.findall(r'decided_by: "([a-z_]+)"', source))
    assert decided <= {member.value for member in DecidedBy}


def test_the_entity_key_and_event_time_are_reachable_without_being_typed_here() -> None:
    """The two names an event log must map are `entity_key` and `event_time`, and neither is a
    `StandardColumn`: `StandardSchemaConfig` forbids them among its `columns`. They reach the
    `<select>` only through a role's `required_columns`, which is why that field - and
    `typical_columns` and `optional_columns`, the rest of `engine.onboarding.mapping._targets` - are
    all read, and why neither name appears as a literal anywhere in the panel.
    """
    steps_text = code_of("steps.js")
    for field in ("required_columns", "typical_columns", "optional_columns"):
        assert field in steps_text, f"steps.js never reads a role's {field}"
    for name in ("entity_key", "event_time"):
        assert f'"{name}"' not in steps_text, f"{name} is hand-typed into steps.js"
    roles = get_roles()
    for role, spec in roles.roles.items():
        assert "entity_key" in spec.required_columns, role
        if spec.is_event:
            assert "event_time" in spec.required_columns, role


# ---------------------------------------------------------------------------
# Every fetch targets a URL one of these routers defines
# ---------------------------------------------------------------------------
def _normalize_called(path: str) -> str:
    """A JS template literal's `${...}` interpolation, collapsed to `{}` - `api.js`'s own params."""
    return PARAM.sub("{}", path).split("?")[0]


def _normalize_served(path: str) -> str:
    """A FastAPI route's own `{name}` path parameter, collapsed to `{}` the same way."""
    return FASTAPI_PARAM.sub("{}", path).split("?")[0]


def served_paths(openapi: dict[str, object]) -> frozenset[str]:
    paths = openapi["paths"]
    assert isinstance(paths, dict)
    served = {_normalize_served(path) for path in paths}
    served.add("/openapi.json")  # FastAPI serves this itself; it is not a route the app declares
    return frozenset(served)


def called_paths() -> frozenset[str]:
    text = code_of("api.js")
    literals = {_normalize_called(match) for match in PATH_LITERAL.findall(text)}
    return frozenset(path for path in literals if path.startswith("/") and path != "/")


def test_every_fetch_call_targets_a_url_the_api_defines(openapi: dict[str, object]) -> None:
    called = called_paths()
    assert called, "no API path literal was found in api.js"
    missing = sorted(path for path in called if path not in served_paths(openapi))
    assert not missing, f"api.js calls endpoints these routers do not define: {missing}"


def test_every_path_is_confirmed_by_a_router_and_not_by_a_transcript(openapi: dict[str, object]) -> None:
    """The other half of the previous assertion, and the reason this file no longer keeps a
    hand-written list of "paths the sibling routers are going to have": every path `api.js` calls is
    confirmed by introspecting a router that exists. A transcription would let a typo pass for as
    long as the transcription repeated it."""
    uncovered = called_paths() - served_paths(openapi)
    assert not uncovered, f"no router declares: {sorted(uncovered)}"
