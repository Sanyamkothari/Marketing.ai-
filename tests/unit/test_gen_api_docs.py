"""docs/API.md is generated, complete and free of anything that changes between runs."""

from __future__ import annotations

import re

import pytest

from engine.config import FIELD_TABLE, StageSpec
from engine.contracts import ARTEFACT_REGISTRY, TABULAR_SCHEMAS
from scripts.gen_api_docs import render_api_docs

DOCS = render_api_docs()

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|\b\d{10}\b")


def test_render_is_deterministic() -> None:
    assert render_api_docs() == DOCS


def test_render_has_no_timestamp() -> None:
    assert TIMESTAMP.search(DOCS) is None


@pytest.mark.parametrize("filename", sorted(set(ARTEFACT_REGISTRY) | set(TABULAR_SCHEMAS)))
def test_every_artefact_is_documented(filename: str) -> None:
    assert f"### `{filename}`" in DOCS


def test_the_registry_record_is_documented() -> None:
    assert "## Registry record" in DOCS
    assert "`ModelVersion`" in DOCS


@pytest.mark.parametrize("stage", FIELD_TABLE, ids=lambda stage: stage.id)
def test_every_stage_title_is_documented(stage: StageSpec) -> None:
    assert f"### {stage.number}. {stage.title}" in DOCS


@pytest.mark.parametrize("path", sorted({field.path for stage in FIELD_TABLE for field in stage.fields}))
def test_every_advanced_settings_path_is_documented(path: str) -> None:
    assert f"| `{path}` |" in DOCS


def test_merge_and_override_rules_are_documented() -> None:
    assert "## Merge and override rules" in DOCS
    assert "engine.config.deep_merge" in DOCS
    assert "engine.config.apply_overrides" in DOCS


ROUTE_ROW = re.compile(r"^\| (?P<method>[A-Z]+) \| `(?P<path>[^`]+)` \|", re.MULTILINE)


def test_every_route_path_is_documented() -> None:
    """The endpoint table equals the app's OpenAPI paths, which are independent of the generator's route walk.

    FastAPI >= 0.140 wraps `include_router()` in a nested route object, so a walk that keeps only the
    top-level `APIRoute`s would document `/healthz` alone and this test would pass vacuously if it used
    the same walk; the OpenAPI schema is built by FastAPI itself and lists every mounted route.
    """
    main = pytest.importorskip("api.main", reason="api/main.py (owner E) is not present yet")
    expected = set(main.create_app().openapi()["paths"])
    assert {"/healthz", "/industries"} <= expected, "create_app() exposes fewer routes than M1 defines"
    documented = {match.group("path") for match in ROUTE_ROW.finditer(DOCS)}
    assert documented == expected


def test_every_route_method_is_documented() -> None:
    main = pytest.importorskip("api.main", reason="api/main.py (owner E) is not present yet")
    expected = {
        (method.upper(), path)
        for path, operations in main.create_app().openapi()["paths"].items()
        for method in operations
        if method.upper() not in {"HEAD", "OPTIONS"}  # the table documents resources, not header probes
    }
    documented = {(match.group("method"), match.group("path")) for match in ROUTE_ROW.finditer(DOCS)}
    assert documented == expected
