"""`deep_merge`: deep for mappings, replace for lists and scalars, `null` clears (DEC-002)."""

from __future__ import annotations

from typing import Any

from engine.config import deep_merge, leaf_paths, load_engine_config, load_use_case_document, resolve_config


def test_mappings_are_merged_recursively() -> None:
    base = {"model_search": {"strategy": "balanced", "folds": 5}}
    overlay = {"model_search": {"folds": 3}}
    assert deep_merge(base, overlay) == {"model_search": {"strategy": "balanced", "folds": 3}}


def test_a_list_in_the_overlay_replaces_the_base_list() -> None:
    base = {"model_search": {"candidates": ["XGBoost", "LightGBM", "RandomForest"]}}
    overlay = {"model_search": {"candidates": ["LightGBM"]}}
    assert deep_merge(base, overlay)["model_search"]["candidates"] == ["LightGBM"]


def test_null_in_the_overlay_clears_the_base_value() -> None:
    assert deep_merge({"split": {"time_column": "snapshot_date"}}, {"split": {"time_column": None}}) == {
        "split": {"time_column": None}
    }


def test_neither_argument_is_mutated() -> None:
    base: dict[str, Any] = {"a": {"b": [1, 2]}, "keep": 1}
    overlay: dict[str, Any] = {"a": {"b": [3]}}
    merged = deep_merge(base, overlay)
    merged["a"]["b"].append(99)
    assert base == {"a": {"b": [1, 2]}, "keep": 1}
    assert overlay == {"a": {"b": [3]}}


def test_key_order_is_base_first_then_overlay_only_keys() -> None:
    merged = deep_merge({"b": 1, "a": 1}, {"z": 1, "a": 2})
    assert list(merged) == ["b", "a", "z"]


def test_unknown_overlay_keys_survive_the_merge_so_the_error_can_name_them() -> None:
    merged = deep_merge({"prepare": {"deduplicate": True}}, {"prepare": {"nonsense": 1}})
    assert merged["prepare"] == {"deduplicate": True, "nonsense": 1}


def test_merge_order_engine_then_use_case_then_override_on_one_leaf() -> None:
    engine_defaults = load_engine_config().defaults
    assert engine_defaults["model_search"]["time_limit_minutes"] == 30

    use_case_layer = {"model_search": {"time_limit_minutes": 20}}
    merged = deep_merge(engine_defaults, use_case_layer)
    assert merged["model_search"]["time_limit_minutes"] == 20

    resolved = resolve_config("targeted-advertisement", {"model_search.time_limit_minutes": 45})
    assert resolved.config.model_search.time_limit_minutes == 45
    assert resolved.sources["model_search.time_limit_minutes"] == "override"

    untouched = resolve_config("targeted-advertisement")
    assert untouched.config.model_search.time_limit_minutes == 30
    assert untouched.sources["model_search.time_limit_minutes"] == "engine"


def test_the_merged_document_keeps_every_engine_default_block() -> None:
    document = load_use_case_document("targeted-advertisement")
    assert set(load_engine_config().defaults) <= set(document)
    merged_leaves = set(leaf_paths(document))
    assert "id" in merged_leaves
    assert "ui.pages.data" in merged_leaves
    assert "model_search.candidates" in merged_leaves
    # The use-case file supplies template columns where the engine default is an empty list.
    assert "template.columns[0].name" in merged_leaves
