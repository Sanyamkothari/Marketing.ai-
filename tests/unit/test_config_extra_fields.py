"""Unknown keys and unknown enum values fail loudly and name what is wrong."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.config import (
    DEFAULT_CONFIG_ROOT,
    Catalog,
    ConfigError,
    load_engine_config,
    load_use_case,
    load_yaml,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "configs"


def _root_with(tmp_path: Path, fixture: str) -> Path:
    """A copy of `configs/` with one broken use-case fixture dropped into `use_cases/`."""
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    shutil.copy(FIXTURES / fixture, root / "use_cases" / fixture)
    return root


@pytest.mark.parametrize(
    ("fixture", "use_case_id", "offending_key", "dotted_path"),
    [
        ("broken_extra_top_level.yaml", "broken-extra-top-level", "nonsense_top_level", "nonsense_top_level"),
        ("broken_extra_prepare.yaml", "broken-extra-prepare", "nonsense_prepare", "prepare.nonsense_prepare"),
        ("broken_extra_band.yaml", "broken-extra-band", "nonsense_band", "actions.bands[0].nonsense_band"),
        (
            "broken_extra_template_column.yaml",
            "broken-extra-template-column",
            "nonsense_column",
            "template.columns[0].nonsense_column",
        ),
    ],
)
def test_unknown_keys_raise_and_name_the_key_and_path(
    tmp_path: Path, fixture: str, use_case_id: str, offending_key: str, dotted_path: str
) -> None:
    root = _root_with(tmp_path, fixture)
    with pytest.raises(ConfigError) as error:
        load_use_case(use_case_id, root)
    assert error.value.code == "CONFIG_INVALID"
    assert offending_key in error.value.message
    assert error.value.path == dotted_path
    assert "Extra inputs are not permitted" in error.value.message


def test_unknown_enum_value_lists_the_allowed_values(tmp_path: Path) -> None:
    root = _root_with(tmp_path, "broken_enum_split_type.yaml")
    with pytest.raises(ConfigError) as error:
        load_use_case("broken-enum-split-type", root)
    assert error.value.code == "CONFIG_INVALID"
    assert error.value.path == "split.type"
    assert "random_stratified" in error.value.message
    assert "time_based" in error.value.message


def test_unknown_key_inside_the_catalog_names_itself() -> None:
    catalog = load_yaml(DEFAULT_CONFIG_ROOT / "engine.yaml")["catalog"]
    catalog["nonsense_catalog"] = 1
    with pytest.raises(ValidationError) as error:
        Catalog.model_validate(catalog)
    assert "nonsense_catalog" in str(error.value)


def test_a_broken_engine_yaml_fails_to_load(tmp_path: Path) -> None:
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    shutil.copy(FIXTURES / "broken_engine_extra_catalog.yaml", root / "engine.yaml")
    with pytest.raises(ConfigError) as error:
        load_engine_config(root)
    assert error.value.code == "CONFIG_INVALID"
