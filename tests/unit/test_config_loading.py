"""The shipped configuration files load, validate and agree with each other."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.config import (
    CONFIG_DIR_ENV_VAR,
    DEFAULT_CONFIG_ROOT,
    SCHEMA_VERSION,
    TIME_LIKE_PATTERN,
    AiType,
    ConfigError,
    EngineConfig,
    Metric,
    ModelFamily,
    ProblemType,
    Strategy,
    UseCaseStatus,
    config_root,
    get_catalog,
    list_industries,
    list_use_case_ids,
    load_all_use_cases,
    load_engine_config,
    load_industry,
    load_use_case,
    load_yaml,
    use_case_path,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "configs"
SHIPPED_IDS = (
    "fault-prediction",
    "order-fulfillment",
    "payment-propensity",
    "rca",
    "targeted-advertisement",
    "win-back-campaign",
)


def test_engine_yaml_parses_into_engine_config() -> None:
    engine = load_engine_config()
    assert isinstance(engine, EngineConfig)
    assert engine.schema_version == SCHEMA_VERSION
    assert engine.defaults["ai_type"] == "predictive"


def test_catalog_is_complete() -> None:
    catalog = get_catalog()
    assert set(catalog.model_families) == set(ModelFamily)
    assert set(catalog.metrics) == set(Metric)
    assert set(catalog.problem_types) == set(ProblemType)
    assert set(catalog.ai_types) == set(AiType)
    assert set(catalog.strategy_presets) == set(Strategy)
    assert catalog.automl_choice.value == "__automl__"
    assert catalog.model_families[ModelFamily.NEURAL_NET].requires == ("torch",)
    assert catalog.problem_types[ProblemType.FORECASTING].enabled is False


def test_time_like_pattern_matches_the_catalog() -> None:
    assert get_catalog().column_name_patterns.time_like == TIME_LIKE_PATTERN


def test_list_use_case_ids_is_exactly_the_shipped_six() -> None:
    assert list_use_case_ids() == SHIPPED_IDS


@pytest.mark.parametrize("use_case_id", SHIPPED_IDS)
def test_every_use_case_file_loads_and_its_stem_matches_its_id(use_case_id: str) -> None:
    config = load_use_case(use_case_id)
    assert config.id == use_case_id
    assert use_case_path(use_case_id).stem == use_case_id.replace("-", "_")
    assert config.template_stem == use_case_id.replace("-", "_")
    assert config.output.kpi.label
    assert config.ui.pages.data


def test_load_all_use_cases_returns_every_id() -> None:
    assert sorted(load_all_use_cases()) == list(SHIPPED_IDS)


def test_industries_list_and_telecom_loads() -> None:
    assert list_industries() == ("telecom",)
    industry = load_industry("telecom")
    assert industry.name == "Telecom"
    assert industry.journey_label == "Customer Lifecycle"
    assert [stage.name for stage in industry.stages] == [
        "Awareness",
        "Onboarding",
        "Service / Payments",
        "Churn",
        "Win-back",
    ]


def test_industry_stage_markers() -> None:
    catalog = get_catalog()
    industry = load_industry("telecom")
    markers = [catalog.ai_types[stage.ai_type].marker for stage in industry.stages]
    assert markers == ["P", "G", "P", "H", "H"]


def test_industry_available_entries_have_files_and_matching_stage_names() -> None:
    industry = load_industry("telecom")
    available = []
    for stage, ref in industry.all_refs():
        if ref.status is UseCaseStatus.AVAILABLE:
            available.append(ref.id)
            assert ref.name is None and ref.description is None
            assert load_use_case(ref.id).lifecycle_stage == stage.name
    assert sorted(available) == list(SHIPPED_IDS)


def test_industry_planned_entries_have_no_file_but_carry_their_own_copy() -> None:
    industry = load_industry("telecom")
    planned = [ref for _, ref in industry.all_refs() if ref.status is UseCaseStatus.PLANNED]
    assert [ref.id for ref in planned] == ["ai-onboarding-assistant"]
    for ref in planned:
        assert not use_case_path(ref.id).is_file()
        assert ref.name and ref.description


def test_config_root_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_DIR_ENV_VAR, raising=False)
    assert config_root() == DEFAULT_CONFIG_ROOT
    monkeypatch.setenv(CONFIG_DIR_ENV_VAR, str(tmp_path))
    assert config_root() == tmp_path.resolve()
    assert config_root(DEFAULT_CONFIG_ROOT) == DEFAULT_CONFIG_ROOT


def test_load_yaml_error_codes(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as missing:
        load_yaml(tmp_path / "nope.yaml")
    assert missing.value.code == "CONFIG_NOT_FOUND"

    with pytest.raises(ConfigError) as not_mapping:
        load_yaml(FIXTURES / "not_a_mapping.yaml")
    assert not_mapping.value.code == "CONFIG_NOT_A_MAPPING"

    with pytest.raises(ConfigError) as broken:
        load_yaml(FIXTURES / "broken_syntax.yaml")
    assert broken.value.code == "CONFIG_YAML_ERROR"


def test_unknown_use_case_and_schema_version(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as unknown:
        load_use_case("no-such-use-case")
    assert unknown.value.code == "USE_CASE_NOT_FOUND"

    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    engine_yaml = root / "engine.yaml"
    engine_yaml.write_text(engine_yaml.read_text().replace("schema_version: 1", "schema_version: 2", 1))
    with pytest.raises(ConfigError) as version:
        load_engine_config(root)
    assert version.value.code == "ENGINE_SCHEMA_VERSION"


def test_use_case_id_must_match_its_file_name(tmp_path: Path) -> None:
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    target = root / "use_cases" / "rca.yaml"
    target.write_text(target.read_text().replace("id: rca", "id: root-cause", 1))
    with pytest.raises(ConfigError) as mismatch:
        list_use_case_ids(root)
    assert mismatch.value.code == "USE_CASE_ID_MISMATCH"
