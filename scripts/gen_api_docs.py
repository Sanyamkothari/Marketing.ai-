"""Generate `docs/API.md` from the contracts, the API routes and the configuration schema.

The output is a pure function of the checked-in source: no timestamps, no package versions,
no dict-order dependence. `--check` re-renders and prints a unified diff instead of writing,
so a stale `docs/API.md` fails `make lint` (DEC-014).
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import inspect
import json
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from fastapi.routing import APIRoute
from pydantic import BaseModel

from engine.config import (
    FIELD_TABLE,
    SCHEMA_VERSION,
    FieldSpec,
    apply_overrides,
    deep_merge,
)
from engine.contracts import (
    ARTEFACT_REGISTRY,
    MODEL_DIRECTORY,
    SCORE_ARTEFACTS,
    TABULAR_SCHEMAS,
    TRAIN_ARTEFACTS,
    ModelVersion,
    scores_csv_columns,
)

__all__ = ["main", "render_api_docs"]

COMMAND: str = "python -m scripts.gen_api_docs"
DEFAULT_OUT: Path = Path("docs/API.md")
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
ENGINE_YAML: Path = REPO_ROOT / "configs" / "engine.yaml"


# ---------------------------------------------------------------------------
# Small rendering helpers
# ---------------------------------------------------------------------------
def _cell(text: str) -> str:
    """Make a string safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _scalar(value: Any) -> str:
    """Render a configuration default as one deterministic cell."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(_scalar(item) for item in value) if value else "(empty)"
    if isinstance(value, Mapping):
        return ", ".join(f"{key}: {_scalar(item)}" for key, item in value.items())
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _number(value: float | None) -> str:
    if value is None:
        return "-"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def _first_line(model: type[BaseModel]) -> str:
    doc = inspect.getdoc(model) or ""
    return _cell(doc.split("\n\n", 1)[0].replace("\n", " "))


# ---------------------------------------------------------------------------
# JSON-schema -> Markdown tables
# ---------------------------------------------------------------------------
def _def_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _literal(value: Any) -> str:
    return json.dumps(value) if isinstance(value, str) else _scalar(value)


def _render_type(node: Mapping[str, Any], defs: Mapping[str, Any], nested: list[str]) -> str:
    """One readable type name; records nested model names in first-use order."""
    if "$ref" in node:
        name = _def_name(str(node["$ref"]))
        definition = defs.get(name, {})
        if "enum" in definition:
            return f"{name} ({' | '.join(_literal(v) for v in definition['enum'])})"
        if name not in nested:
            nested.append(name)
        return name
    for key in ("allOf", "oneOf"):
        members = node.get(key)
        if isinstance(members, list) and len(members) == 1:
            return _render_type(members[0], defs, nested)
    members = node.get("anyOf")
    if isinstance(members, list):
        return " | ".join(_render_type(member, defs, nested) for member in members)
    if "const" in node:
        return _literal(node["const"])
    if "enum" in node:
        return " | ".join(_literal(value) for value in node["enum"])
    prefix_items = node.get("prefixItems")
    if isinstance(prefix_items, list):
        inner = ", ".join(_render_type(item, defs, nested) for item in prefix_items)
        return f"tuple[{inner}]"
    node_type = node.get("type")
    if node_type == "array":
        items = node.get("items")
        inner = _render_type(items, defs, nested) if isinstance(items, Mapping) else "any"
        return f"list[{inner}]"
    if node_type == "object":
        values = node.get("additionalProperties")
        if isinstance(values, Mapping) and values:
            return f"object of string -> {_render_type(values, defs, nested)}"
        return "object"
    if node_type == "string" and node.get("format") == "date-time":
        return "datetime (ISO-8601, with timezone)"
    if isinstance(node_type, str):
        return node_type
    if isinstance(node_type, list):
        return " | ".join(str(item) for item in node_type)
    return "any"


def _description(node: Mapping[str, Any], defs: Mapping[str, Any]) -> str:
    text = node.get("description")
    if isinstance(text, str) and text:
        return _cell(text)
    if "$ref" in node:
        definition = defs.get(_def_name(str(node["$ref"])), {})
        inner = definition.get("description")
        if isinstance(inner, str) and inner:
            return _cell(inner)
    return ""


def _field_table(schema: Mapping[str, Any], defs: Mapping[str, Any], nested: list[str]) -> list[str]:
    required = set(schema.get("required", ()))
    lines = ["| Field | Type | Required | Meaning |", "|---|---|---|---|"]
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping) or not properties:
        return [*lines, "| (no fields) | - | - | - |", ""]
    for name, node in properties.items():
        if not isinstance(node, Mapping):
            continue
        flag = "yes" if name in required else "no"
        lines.append(
            f"| `{name}` | {_cell(_render_type(node, defs, nested))} | {flag} | {_description(node, defs)} |"
        )
    lines.append("")
    return lines


def _model_section(heading: str, model: type[BaseModel], level: str, rendered: set[str]) -> list[str]:
    """A heading, the model's own table and a sub-table per nested model not yet documented."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    nested: list[str] = []
    lines = [f"{level} {heading}", ""]
    summary = _first_line(model)
    if summary:
        lines += [summary, ""]
    lines += _field_table(schema, defs, nested)
    index = 0
    while index < len(nested):
        name = nested[index]
        index += 1
        if name in rendered:
            continue
        rendered.add(name)
        definition = defs.get(name, {})
        if not isinstance(definition, Mapping) or "properties" not in definition:
            continue
        lines += [f"{level}# {name}", ""]
        inner = definition.get("description")
        if isinstance(inner, str) and inner:
            lines += [_cell(inner), ""]
        lines += _field_table(definition, defs, nested)
    return lines


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def _load_create_app() -> Callable[[], Any] | None:
    """`api.main.create_app`, imported lazily (and dynamically, so this module type-checks without it)."""
    try:
        module = importlib.import_module("api.main")
    except ImportError:
        return None
    factory: Callable[[], Any] | None = getattr(module, "create_app", None)
    return factory


def _api_routes(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every `APIRoute` reachable from `routes`, with its effective (prefixed) path.

    FastAPI >= 0.140 no longer flattens `include_router()` into `app.routes`: it appends one wrapper per
    included router that exposes the router as `original_router` and the include prefix on
    `include_context.prefix`, and it builds the effective path as `prefix + route.path` (nested includes
    concatenate their prefixes). This walk mirrors that composition. /openapi.json, /docs, /redoc and
    static mounts are plain Starlette routes and are skipped: they are not part of the contract.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        nested = getattr(route, "original_router", None)
        if nested is None:
            continue
        context = getattr(route, "include_context", None)
        nested_prefix = getattr(context, "prefix", "") if context is not None else ""
        yield from _api_routes(nested.routes, prefix + str(nested_prefix))


def _route_rows() -> list[tuple[str, str, str, str]] | None:
    """(method, path, summary, response model) per real API route, sorted by path then method."""
    create_app = _load_create_app()
    if create_app is None:
        return None
    rows: list[tuple[str, str, str, str]] = []
    for path, route in _api_routes(create_app().routes):
        model = getattr(route, "response_model", None)
        model_name = getattr(model, "__name__", "-") if model is not None else "-"
        summary = route.summary or route.name.replace("_", " ")
        for method in sorted(set(route.methods or ()) - {"HEAD", "OPTIONS"}):
            rows.append((method, path, summary, model_name))
    rows.sort(key=lambda row: (row[1], row[0]))
    return rows


# ---------------------------------------------------------------------------
# engine.yaml keys and their comments
# ---------------------------------------------------------------------------
def _yaml_comments(path: Path) -> list[tuple[str, str]]:
    """(dotted key, comment text) for every commented key of `configs/engine.yaml`, file order."""
    rows: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            continue
        key, separator, rest = stripped.partition(":")
        if not separator or not key or key.startswith("-"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else ""
        dotted = f"{parent}.{key}" if parent else key
        stack.append((indent, dotted))
        comment = rest.partition("#")[2].strip() if "#" in rest else ""
        if comment:
            rows.append((dotted, comment))
    return rows


def _yaml_value(document: Mapping[str, Any], dotted: str) -> Any:
    node: Any = document
    for segment in dotted.split("."):
        name, bracket, index = segment.partition("[")
        if isinstance(node, Mapping):
            node = node.get(name)
        else:
            return None
        if bracket:
            position = int(index.rstrip("]"))
            if isinstance(node, list) and position < len(node):
                node = node[position]
            else:
                return None
    return node


def _field_row(field: FieldSpec, defaults: Mapping[str, Any]) -> str:
    default = _scalar(_yaml_value(defaults, field.path))
    scale = "-" if field.scale is None else str(field.scale)
    return (
        f"| `{field.path}` | {_cell(field.label)} | {field.widget.value} | {_cell(default)} | "
        f"{_number(field.min)} | {_number(field.max)} | {_number(field.step)} | {scale} |"
    )


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------
def render_api_docs() -> str:
    """Render `docs/API.md`.

    Deterministic, in this order: header, endpoints, run artefacts, tabular artefacts,
    the registry record, the configuration reference and the merge/override rules.
    `api.main.create_app` is imported lazily; while it is absent the endpoint table says so
    and every other section still renders.
    """
    lines: list[str] = [
        "# API and artefact reference",
        "",
        f"Generated by `{COMMAND}`. Do not edit by hand: run the command and commit the result.",
        "",
        f"Contract schema version: {SCHEMA_VERSION}.",
        "",
    ]

    # 2. Endpoints
    lines += ["## Endpoints", ""]
    rows = _route_rows()
    if rows is None:
        lines += [
            "`api.main.create_app` could not be imported, so no endpoint is documented in this render.",
            "",
        ]
    else:
        lines += ["| Method | Path | Summary | Response model |", "|---|---|---|---|"]
        lines += [f"| {m} | `{p}` | {_cell(s)} | {_cell(r)} |" for m, p, s, r in rows]
        lines.append("")

    # 3. Run artefacts
    lines += [
        "## Run artefacts",
        "",
        "One file per stage in `data/runs/<run_id>/`. Every field below is what the UI renders.",
        "",
        f"A training run writes: {', '.join(f'`{name}`' for name in sorted(TRAIN_ARTEFACTS))}.",
        "",
        f"A scoring run writes: {', '.join(f'`{name}`' for name in sorted(SCORE_ARTEFACTS))}.",
        "",
    ]
    rendered: set[str] = set()
    for filename, model in ARTEFACT_REGISTRY.items():
        lines += _model_section(f"`{filename}`", model, "###", rendered)

    # 4. Tabular artefacts
    lines += [
        "## Tabular artefacts",
        "",
        "Row-oriented files. The model below describes ONE ROW of the file.",
        "",
    ]
    for filename, model in TABULAR_SCHEMAS.items():
        lines += _model_section(f"`{filename}`", model, "###", rendered)
    header_rule = inspect.getdoc(scores_csv_columns) or ""
    lines += [
        "### `scores.csv` header rule",
        "",
        "`engine.contracts.scores_csv_columns(config, primary_key)` builds the header:",
        "",
        "```",
        *header_rule.splitlines(),
        "```",
        "",
    ]

    # 5. Registry record
    lines += [
        "## Registry record",
        "",
        "Stored in SQLite by `engine.registry`, not in a run directory.",
        "",
    ]
    lines += _model_section("`ModelVersion`", ModelVersion, "###", rendered)
    lines += [
        f"The saved predictor lives in the `{MODEL_DIRECTORY}` directory of the run; it has no JSON contract.",
        "",
    ]

    # 6. Configuration reference
    document = yaml.safe_load(ENGINE_YAML.read_text(encoding="utf-8"))
    defaults: Mapping[str, Any] = document.get("defaults", {}) if isinstance(document, Mapping) else {}
    ui_paths = {field.path for stage in FIELD_TABLE for field in stage.fields}
    lines += [
        "## Configuration reference",
        "",
        "Advanced settings in the UI are exactly the fields of the use-case schema. Values are in",
        "config units: fractions stay fractions, and `scale` is the factor the UI multiplies by for display.",
        "",
    ]
    for stage in FIELD_TABLE:
        lines += [
            f"### {stage.number}. {stage.title}",
            "",
            "| Path | Label | Widget | Default | Min | Max | Step | Scale |",
            "|---|---|---|---|---|---|---|---|",
        ]
        lines += [_field_row(field, defaults) for field in stage.fields]
        lines.append("")
    lines += [
        "### Other `configs/engine.yaml` keys",
        "",
        "Keys of the default document that no advanced-settings field renders, with their file comments.",
        "",
        "| Key | Notes |",
        "|---|---|",
    ]
    catalog_rows: list[tuple[str, str]] = []
    for dotted, comment in _yaml_comments(ENGINE_YAML):
        if dotted.startswith("catalog"):
            catalog_rows.append((dotted, comment))
            continue
        path = dotted[len("defaults.") :] if dotted.startswith("defaults.") else dotted
        if path in ui_paths or dotted == "defaults":
            continue
        lines.append(f"| `{path}` | {_cell(comment)} |")
    lines += [
        "",
        "### Catalog keys",
        "",
        "Engine constants. The catalog is never merged into a use case and never overridable (DEC-001).",
        "",
        "| Key | Notes |",
        "|---|---|",
    ]
    lines += [f"| `{dotted}` | {_cell(comment)} |" for dotted, comment in catalog_rows]
    lines.append("")

    # 7. Merge and override rules
    lines += ["## Merge and override rules", ""]
    for function in (deep_merge, apply_overrides):
        lines += [
            f"### `engine.config.{function.__name__}`",
            "",
            "```",
            *(inspect.getdoc(function) or "").splitlines(),
            "```",
            "",
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """`--out docs/API.md` (default) and `--check`; returns the process exit code."""
    parser = argparse.ArgumentParser(prog=COMMAND, description="Generate docs/API.md from the contracts.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="file to write (default: docs/API.md)")
    parser.add_argument("--check", action="store_true", help="write nothing; diff and exit 1 on drift")
    args = parser.parse_args(argv)
    if _load_create_app() is None:
        raise RuntimeError(
            "api.main.create_app could not be imported, so docs/API.md cannot be generated completely. "
            "Land the API module first, then run `python -m scripts.gen_api_docs`."
        )
    rendered = render_api_docs()
    out: Path = args.out
    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current == rendered:
            return 0
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(out),
            tofile=f"{out} (generated by {COMMAND})",
        )
        sys.stdout.writelines(diff)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
