"""A use-case file carries only what differs from `engine.yaml:defaults` (design section 3)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from engine.config import DEFAULT_CONFIG_ROOT, list_use_case_ids, load_engine_config, load_yaml, use_case_path

IDENTITY_PREFIXES = (
    "id",
    "name",
    "description",
    "lifecycle_stage",
    "target",
    "template",
    "ui.pages",
    "output.kpi",
    # `label` is the Phase 2 spelling of `target`: it says what the outcome IS for this use case,
    # derived from events rather than read from a column. Like `target` it is per-use-case identity
    # with no engine default to differ from - `engine.yaml` carries `label: null`, because a use
    # case that has not been onboarded has no outcome definition at all.
    "label",
)


def _file_leaves(node: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted leaves of a raw YAML document; a list is one leaf (lists replace whole, DEC-002)."""
    leaves: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            leaves.update(_file_leaves(value, path))
        else:
            leaves[path] = value
    return leaves


def _at(document: Mapping[str, Any], path: str) -> Any:
    cursor: Any = document
    for part in path.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            return _MISSING
        cursor = cursor[part]
    return cursor


_MISSING = object()


@pytest.fixture(scope="module")
def engine_defaults() -> dict[str, Any]:
    return load_engine_config().defaults


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_every_leaf_outside_the_identity_block_differs_from_the_engine_default(
    use_case_id: str, engine_defaults: dict[str, Any]
) -> None:
    document = load_yaml(use_case_path(use_case_id))
    for path, value in _file_leaves(document).items():
        if path.startswith(IDENTITY_PREFIXES):
            continue
        default = _at(engine_defaults, path)
        assert default is not _MISSING, f"{use_case_id}: {path} has no engine default"
        assert value != default, f"{use_case_id}: {path} restates the engine default {default!r}"


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_no_redundant_ai_type_or_problem_type(use_case_id: str) -> None:
    document = load_yaml(use_case_path(use_case_id))
    assert document.get("ai_type") != "predictive"
    assert "problem_type" not in document


def test_every_use_case_file_is_covered() -> None:
    files = sorted(path.name for path in (DEFAULT_CONFIG_ROOT / "use_cases").glob("*.yaml"))
    assert files == sorted(f"{use_case_id.replace('-', '_')}.yaml" for use_case_id in list_use_case_ids())
    assert Path(DEFAULT_CONFIG_ROOT / "use_cases").is_dir()
