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
    UseCaseConfig,
    UseCaseStatus,
    advanced_settings_schema,
    config_root,
    get_catalog,
    list_industries,
    list_use_case_ids,
    load_all_use_cases,
    load_engine_config,
    load_industry,
    load_use_case,
    load_use_case_document,
    load_yaml,
    overridable_paths,
    resolve_config,
    use_case_path,
)
from tests.fixtures.planned import PLANNED_ID, planned_config_root

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "configs"


def shipped_use_case_files() -> list[Path]:
    """Every use-case file in `configs/`, read off the directory rather than listed here.

    Adding a use case is adding a YAML file (plan section 2.1). A test that spelled the ids out
    would make that a lie, so the ids under test come from the loader and the files they must
    account for come from the directory.
    """
    return sorted((DEFAULT_CONFIG_ROOT / "use_cases").glob("*.yaml"))


#: The shipped ids, as the loader sees them. `test_list_use_case_ids_accounts_for_every_shipped_file`
#: is what pins this to the directory; everything else may parametrise off it.
SHIPPED_IDS: tuple[str, ...] = list_use_case_ids()


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


def test_list_use_case_ids_accounts_for_every_shipped_file() -> None:
    """The loader sees every file in `configs/use_cases/` and nothing else, sorted and distinct."""
    ids = list_use_case_ids()
    files = shipped_use_case_files()
    assert files, "configs/use_cases/ is empty"
    assert len(ids) == len(set(ids)) == len(files)
    assert {use_case_path(use_case_id) for use_case_id in ids} == set(files)
    assert list(ids) == sorted(ids)


@pytest.mark.parametrize("use_case_id", SHIPPED_IDS)
def test_every_use_case_file_loads_and_its_stem_matches_its_id(use_case_id: str) -> None:
    config = load_use_case(use_case_id)
    assert config.id == use_case_id
    assert use_case_path(use_case_id).stem == use_case_id.replace("-", "_")
    assert config.template_stem == use_case_id.replace("-", "_")
    assert config.output.kpi.label
    assert config.ui.pages.data


def test_load_all_use_cases_returns_every_id() -> None:
    loaded = load_all_use_cases()
    assert sorted(loaded) == sorted(list_use_case_ids())
    assert all(use_case_id == config.id for use_case_id, config in loaded.items())


def shipped_industry_files() -> list[Path]:
    """Every industry file in `configs/`, read off the directory for the same reason as use cases."""
    return sorted((DEFAULT_CONFIG_ROOT / "industries").glob("*.yaml"))


def test_industries_list_and_telecom_loads() -> None:
    """Every industry file is listed and loads with its cross-file rules; telecom is still the demo.

    Pinned to exactly `("telecom",)` until DEC-085 (Plan A ruling D3): one file per industry, and
    the test validates every file rather than asserting there is only one.
    """
    industries = list_industries()
    assert industries == tuple(path.stem for path in shipped_industry_files())
    assert "telecom" in industries
    for industry_id in industries:
        assert load_industry(industry_id).id == industry_id
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
    """Every industry's available entries, and together they reach every shipped use case.

    Telecom used to have to list every file in `configs/use_cases/` itself; since DEC-085 (D3) the
    union over all industry files must, so no use case is shipped that no overview can open.
    """
    available: list[str] = []
    for industry_id in list_industries():
        for stage, ref in load_industry(industry_id).all_refs():
            if ref.status is UseCaseStatus.AVAILABLE:
                available.append(ref.id)
                assert ref.name is None and ref.description is None
                assert load_use_case(ref.id).lifecycle_stage == stage.name
    assert sorted(available) == sorted(list_use_case_ids())


def test_industry_planned_entries_have_no_file_but_carry_their_own_copy(tmp_path: Path) -> None:
    """Read off a fixture root: Phase 3a shipped the last planned use case the real one had."""
    root = planned_config_root(tmp_path)
    industry = load_industry("telecom", root)
    planned = [ref for _, ref in industry.all_refs() if ref.status is UseCaseStatus.PLANNED]
    assert [ref.id for ref in planned] == [PLANNED_ID]
    for ref in planned:
        assert not use_case_path(ref.id, root).is_file()
        assert ref.name and ref.description


def test_every_shipped_use_case_is_available() -> None:
    """The other half: nothing in telecom's real journey is planned, so nothing there is unreachable.

    The library's industries do carry planned entries (the stages no public dataset covers yet);
    `test_industry_available_entries_have_files_and_matching_stage_names` loads them all.
    """
    refs = load_industry("telecom").all_refs()
    assert refs
    assert all(ref.status is UseCaseStatus.AVAILABLE for _, ref in refs)


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


def _root_where_rmse_scores_classification(tmp_path: Path) -> Path:
    """A copy of `configs/` whose catalog also lets `rmse` (relabelled) score a classification problem,
    with a targeted-advertisement file that is only valid under that catalog."""
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    engine_yaml = root / "engine.yaml"
    text = engine_yaml.read_text(encoding="utf-8")
    rmse_line = next(line for line in text.splitlines() if line.strip().startswith("rmse:"))
    assert "problem_types: [regression]" in rmse_line and 'label: "RMSE"' in rmse_line
    patched = rmse_line.replace(
        "problem_types: [regression]", "problem_types: [regression, binary_classification]"
    ).replace('label: "RMSE"', 'label: "Root MSE"')
    engine_yaml.write_text(text.replace(rmse_line, patched, 1), encoding="utf-8")
    use_case = root / "use_cases" / "targeted_advertisement.yaml"
    use_case.write_text(
        use_case.read_text(encoding="utf-8")
        + "\nmodel_search:\n  metric: rmse\n  metric_choices: [rmse, roc_auc]\n",
        encoding="utf-8",
    )
    return root


def test_an_explicit_root_is_validated_and_labelled_against_its_own_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-038: the loaders read the catalog of the root they were given, not the checkout's."""
    monkeypatch.delenv(CONFIG_DIR_ENV_VAR, raising=False)
    root = _root_where_rmse_scores_classification(tmp_path)
    assert get_catalog(root).metrics[Metric.RMSE].problem_types == (
        ProblemType.REGRESSION,
        ProblemType.BINARY_CLASSIFICATION,
    )
    assert get_catalog().metrics[Metric.RMSE].problem_types == (ProblemType.REGRESSION,)

    config = load_use_case("targeted-advertisement", root)
    assert config.catalog is get_catalog(root)
    assert config.model_search.metric is Metric.RMSE
    assert config.trainable_in_phase_1 is True
    assert config.marker == "P"

    # The same document is invalid under the checkout's catalog, which a root-less validation uses.
    with pytest.raises(ConfigError) as error:
        UseCaseConfig.model_validate(load_use_case_document("targeted-advertisement", root))
    assert error.value.code == "METRIC_NOT_FOR_PROBLEM"

    resolved = resolve_config("targeted-advertisement", {"model_search.metric": "roc_auc"}, root=root)
    assert resolved.config.model_search.metric is Metric.ROC_AUC
    assert resolved.config.catalog is get_catalog(root)

    schema = advanced_settings_schema(config)
    metric_field = next(
        field for stage in schema.stages for field in stage.fields if field.path == "model_search.metric"
    )
    assert metric_field.choices is not None
    assert [choice.label for choice in metric_field.choices] == ["Root MSE", "ROC-AUC"]
    assert schema.stages[4].summary.startswith("Root MSE · ")
    assert "model_search.metric" in overridable_paths(config)
    assert load_industry("telecom", root).id == "telecom"


def test_loading_from_an_explicit_root_never_touches_the_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no usable default root, an explicit one must still load, validate and label (DEC-038)."""
    monkeypatch.delenv(CONFIG_DIR_ENV_VAR, raising=False)
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    monkeypatch.setattr("engine.config.DEFAULT_CONFIG_ROOT", tmp_path / "nonexistent" / "configs")
    with pytest.raises(ConfigError) as unusable:
        get_catalog()
    assert unusable.value.code == "CONFIG_NOT_FOUND"

    config = load_use_case("payment-propensity", root)
    assert config.trainable_in_phase_1 is True
    assert config.marker == "P"
    resolved = resolve_config("payment-propensity", {"problem_type": "regression"}, root=root)
    assert resolved.config.model_search.metric is Metric.RMSE
    schema = advanced_settings_schema(resolved.config, columns=["customer_id", "snapshot_date", "x"])
    assert schema.stages[4].summary.startswith("RMSE · ")
    assert load_industry("telecom", root).id == "telecom"
