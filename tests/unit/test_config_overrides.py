"""Run overrides: two wire shapes, one allow-list, index patches, config units only (DEC-029)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.config import (
    DEFAULT_CONFIG_ROOT,
    EXTRA_OVERRIDABLE_PATHS,
    IMMUTABLE_PATHS,
    ConfigError,
    ProblemType,
    RunOverrides,
    apply_overrides,
    expand_paths,
    leaf_paths,
    load_use_case,
    load_use_case_document,
    overridable_paths,
    resolve_config,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "configs"
USE_CASE = "targeted-advertisement"


@pytest.fixture(scope="module")
def base_document() -> dict[str, object]:
    return load_use_case_document(USE_CASE)


@pytest.fixture(scope="module")
def allowed() -> frozenset[str]:
    return overridable_paths(load_use_case(USE_CASE))


def _neural_root(tmp_path: Path) -> Path:
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    shutil.copy(FIXTURES / "neural_demo.yaml", root / "use_cases" / "neural_demo.yaml")
    return root


def test_expand_paths_accepts_nested_dotted_and_mixed_shapes() -> None:
    nested = expand_paths({"prepare": {"exclude_columns": ["region"]}})
    dotted = expand_paths({"prepare.exclude_columns": ["region"]})
    assert nested == dotted == {"prepare": {"exclude_columns": ["region"]}}

    mixed = expand_paths(
        {
            "split": {"test_fraction": 0.20},
            "actions.bands[0].min_score": 0.85,
            "prepare.exclude_columns": ["region"],
        }
    )
    assert mixed == {
        "split": {"test_fraction": 0.20},
        "actions": {"bands": {0: {"min_score": 0.85}}},
        "prepare": {"exclude_columns": ["region"]},
    }
    assert leaf_paths(mixed) == (
        "split.test_fraction",
        "actions.bands[0].min_score",
        "prepare.exclude_columns",
    )


def test_bad_paths_conflicts_and_prefix_collisions() -> None:
    with pytest.raises(ConfigError) as bad:
        expand_paths({"Split.TestFraction": 0.2})
    assert bad.value.code == "OVERRIDE_BAD_PATH"

    with pytest.raises(ConfigError) as conflict:
        expand_paths({"split.test_fraction": 0.2, "split": {"test_fraction": 0.3}})
    assert conflict.value.code == "OVERRIDE_CONFLICTING_PATHS"

    with pytest.raises(ConfigError) as collision:
        expand_paths({"actions.bands": [], "actions.bands[0].min_score": 0.85})
    assert collision.value.code == "OVERRIDE_PREFIX_COLLISION"


def test_an_index_patch_changes_one_band_and_keeps_the_others() -> None:
    resolved = resolve_config(USE_CASE, {"actions.bands[0].min_score": 0.85})
    bands = resolved.config.actions.bands
    assert bands[0].min_score == 0.85
    assert bands[0].name == "High" and bands[0].action == "Serve ad"
    assert [band.min_score for band in bands] == [0.85, 0.50, 0.00]
    assert [band.name for band in bands] == ["High", "Medium", "Low"]
    assert resolved.overrides_applied == {"actions": {"bands": {0: {"min_score": 0.85}}}}
    assert resolved.sources["actions.bands[0].min_score"] == "override"
    assert resolved.sources["actions.bands[0].action"] == "use_case"


def test_a_plain_list_replaces_the_whole_list() -> None:
    resolved = resolve_config(USE_CASE, {"model_search.candidates": ["LightGBM"]})
    assert [family.value for family in resolved.config.model_search.candidates] == ["LightGBM"]


def test_index_patch_out_of_range(base_document: dict[str, object]) -> None:
    with pytest.raises(ConfigError) as error:
        apply_overrides(
            base_document,
            {"actions.bands[7].min_score": 0.5},
            allowed=frozenset({"actions.bands[7].min_score"}),
        )
    assert error.value.code == "OVERRIDE_INDEX_OUT_OF_RANGE"

    with pytest.raises(ConfigError) as unknown:
        resolve_config(USE_CASE, {"actions.bands[7].min_score": 0.5})
    assert unknown.value.code == "OVERRIDE_UNKNOWN_PATH"


@pytest.mark.parametrize(
    "override",
    [
        {"id": "something-else"},
        {"template": {"columns": []}},
        {"ui": {"pages": {"data": "Nope"}}},
        {"output": {"kpi": {"label": "Nope"}}},
        {"name": "Nope"},
        {"entity": "widget"},
    ],
)
def test_immutable_fields_are_rejected(override: dict[str, object]) -> None:
    with pytest.raises(ConfigError) as error:
        resolve_config(USE_CASE, override)
    assert error.value.code == "OVERRIDE_IMMUTABLE_FIELD"
    assert set(override) <= IMMUTABLE_PATHS


@pytest.mark.parametrize("path", ["validation.min_rows", "split.test_fractoin", "model_search.nonsense"])
def test_unknown_paths_are_rejected_with_suggestions(path: str) -> None:
    with pytest.raises(ConfigError) as error:
        resolve_config(USE_CASE, {path: 1})
    assert error.value.code == "OVERRIDE_UNKNOWN_PATH"
    assert "Nearest:" in error.value.message


def test_the_schema_paths_and_the_extra_paths_are_exactly_what_is_overridable(
    allowed: frozenset[str],
) -> None:
    assert allowed >= EXTRA_OVERRIDABLE_PATHS
    for path in (
        "validation.acknowledged",
        "problem_type",
        "target.column",
        "target.positive_label",
        "split.time_column",
        "prepare.exclude_columns",
        "model_search.candidates",
        "validation.min_positive",
        "actions.bands[0].min_score",
    ):
        assert path in allowed
    assert "validation.min_rows" not in allowed


def test_empty_string_becomes_none_for_optional_column_paths() -> None:
    resolved = resolve_config("order-fulfillment", {"split.time_column": ""})
    assert resolved.config.split.time_column is None
    assert resolved.warnings == ("split.type is time_based and no time column is set yet",)


def test_percentages_are_never_guessed() -> None:
    with pytest.raises(ConfigError) as error:
        resolve_config(USE_CASE, {"split.test_fraction": 15})
    assert error.value.code == "CONFIG_INVALID"
    assert error.value.path == "split.test_fraction"
    assert resolve_config(USE_CASE, {"split.test_fraction": 0.20}).config.split.test_fraction == 0.20


def test_extra_overridable_paths_take_effect() -> None:
    acknowledged = resolve_config(USE_CASE, {"validation.acknowledged": ["LEAKAGE_SUSPECTED:converted_30d"]})
    assert acknowledged.config.validation.acknowledged == ("LEAKAGE_SUSPECTED:converted_30d",)

    forecasting = resolve_config(USE_CASE, {"problem_type": "forecasting"})
    assert forecasting.config.problem_type is ProblemType.FORECASTING
    assert forecasting.config.trainable_in_phase_1 is False

    excluded = resolve_config(USE_CASE, {"prepare": {"exclude_columns": ["region"]}})
    assert excluded.config.prepare.exclude_columns == ("region",)


def test_target_column_is_overridable(tmp_path: Path) -> None:
    root = _neural_root(tmp_path)
    resolved = resolve_config("neural-demo", {"target.column": "other_outcome"}, root=root)
    assert resolved.config.target.column == "other_outcome"
    assert resolved.sources["target.column"] == "override"


def test_a_neural_net_candidate_without_torch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _neural_root(tmp_path)
    monkeypatch.setattr("engine.config.dependency_available", lambda module: module != "torch")
    with pytest.raises(ConfigError) as error:
        resolve_config("neural-demo", {"model_search.candidates": ["XGBoost", "NeuralNet"]}, root=root)
    assert error.value.code == "MODEL_FAMILY_UNAVAILABLE"
    assert "make setup EXTRAS=nn" in error.value.message

    monkeypatch.setattr("engine.config.dependency_available", lambda module: True)
    resolved = resolve_config("neural-demo", {"model_search.candidates": ["XGBoost", "NeuralNet"]}, root=root)
    assert [family.value for family in resolved.config.model_search.candidates] == ["XGBoost", "NeuralNet"]


def test_run_overrides_round_trips_both_wire_shapes() -> None:
    overrides = RunOverrides({"actions.bands[0].min_score": 0.85, "split": {"test_fraction": 0.2}})
    assert overrides.expanded() == {
        "actions": {"bands": {0: {"min_score": 0.85}}},
        "split": {"test_fraction": 0.2},
    }
    assert overrides.touched_paths() == ("actions.bands[0].min_score", "split.test_fraction")

    from_json = RunOverrides.model_validate({"actions": {"bands": {"0": {"min_score": 0.85}}}})
    assert from_json.expanded() == {"actions": {"bands": {0: {"min_score": 0.85}}}}
    assert resolve_config(USE_CASE, from_json).config.actions.bands[0].min_score == 0.85
