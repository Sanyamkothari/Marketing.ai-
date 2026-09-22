"""`advanced_settings_schema()` is the literal table of design section 2, projected onto a config."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from engine.config import (
    ADVISORY_NOTE,
    ADVISORY_PATHS,
    DEFAULT_CONFIG_ROOT,
    FIELD_TABLE,
    AiType,
    ColumnSource,
    ModelFamily,
    UseCaseConfig,
    Widget,
    advanced_settings_schema,
    get_catalog,
    list_use_case_ids,
    load_all_use_cases,
    load_use_case,
    recipe_from_config,
    resolve_config,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "configs"

STAGE_TITLES = (
    (1, "data_preparation", "Data preparation"),
    (2, "data_split", "Data split"),
    (3, "feature_engineering", "Feature engineering"),
    (4, "model_search", "Model search"),
    (5, "evaluation", "Evaluation & explainability"),
    (6, "actions", "Actions & output"),
    (7, "monitoring", "Monitoring & retraining"),
    (8, "governance", "Governance & privacy"),
)

# The 45 schema paths of design section 2 (44 defaultAdv keys + evaluation.threshold.value), in order.
PATHS: tuple[str, ...] = (
    "prepare.missing_values",
    "prepare.outliers",
    "prepare.pii_handling",
    "validation.min_positive",
    "prepare.deduplicate",
    "validation.leakage_check",
    "prepare.exclude_columns",
    "split.type",
    "split.validation_fraction",
    "split.test_fraction",
    "split.time_column",
    "split.group_column",
    "features.auto_feature_engineering",
    "features.categorical_encoding",
    "features.numeric_scaling",
    "features.text_columns",
    "features.selection",
    "features.max_features",
    "model_search.candidates",
    "model_search.strategy",
    "model_search.tuning_trials",
    "model_search.time_limit_minutes",
    "model_search.folds",
    "model_search.imbalance",
    "model_search.ensemble",
    "model_search.metric",
    "evaluation.calibration",
    "evaluation.threshold.mode",
    "evaluation.threshold.value",
    "evaluation.reasons_per_row",
    "evaluation.fairness_column",
    "evaluation.champion_min_improvement_pct",
    "evaluation.shap",
    "actions.bands[0].min_score",
    "actions.bands[1].min_score",
    "actions.control_group_fraction",
    "actions.suppression.recently_contacted_days",
    "actions.suppression.suppress_opted_out",
    "actions.suppression.suppress_recently_contacted",
    "monitoring.drift_psi_threshold",
    "monitoring.retraining",
    "monitoring.performance_alert_drop_pct",
    "governance.retention_days",
    "governance.consent_column",
    "governance.approval_required",
)

# path -> (min, max, step, scale), config units, verbatim from design section 2, except that
# `model_search.time_limit_minutes` starts at 1 in steps of 1 so plan §10's `time_limit_minutes: 1`
# integration override is a valid value (DEC-036).
BOUNDS: dict[str, tuple[float, float, float, int | None]] = {
    "validation.min_positive": (50, 100000, 50, None),
    "split.validation_fraction": (0.05, 0.40, 0.05, 100),
    "split.test_fraction": (0.05, 0.40, 0.05, 100),
    "features.max_features": (10, 500, 10, None),
    "model_search.tuning_trials": (5, 500, 10, None),
    "model_search.time_limit_minutes": (1, 240, 1, None),
    "model_search.folds": (2, 10, 1, None),
    "evaluation.threshold.value": (0.01, 0.99, 0.01, None),
    "evaluation.reasons_per_row": (1, 5, 1, None),
    "evaluation.champion_min_improvement_pct": (0, 20, 0.5, None),
    "actions.bands[0].min_score": (0, 1, 0.05, None),
    "actions.bands[1].min_score": (0, 1, 0.05, None),
    "actions.control_group_fraction": (0, 0.50, 0.01, 100),
    "actions.suppression.recently_contacted_days": (1, 90, 1, None),
    "monitoring.drift_psi_threshold": (0.05, 1.00, 0.05, None),
    "monitoring.performance_alert_drop_pct": (1, 50, 1, None),
    "governance.retention_days": (0, 730, 30, None),
}

LABELS: dict[str, str] = {
    "prepare.missing_values": "Missing values",
    "prepare.outliers": "Outliers",
    "prepare.pii_handling": "PII handling",
    "validation.min_positive": "Min positive examples",
    "prepare.deduplicate": "Remove duplicate rows",
    "validation.leakage_check": "Check for target leakage",
    "prepare.exclude_columns": "Exclude columns from features",
    "split.type": "Split type",
    "split.validation_fraction": "Validation (%)",
    "split.test_fraction": "Test (%)",
    "split.time_column": "Time column",
    "split.group_column": "Group column (keep together)",
    "features.auto_feature_engineering": "Automatic feature engineering (dates, ratios, aggregates)",
    "features.categorical_encoding": "Categorical encoding",
    "features.numeric_scaling": "Numeric scaling",
    "features.text_columns": "Text columns",
    "features.selection": "Feature selection",
    "features.max_features": "Max features",
    "model_search.candidates": "",
    "model_search.strategy": "Search strategy",
    "model_search.tuning_trials": "Tuning trials",
    "model_search.time_limit_minutes": "Time limit (min)",
    "model_search.folds": "CV folds",
    "model_search.imbalance": "Class imbalance",
    "model_search.ensemble": "Ensemble / stack the best models",
    "model_search.metric": "Optimise for",
    "evaluation.calibration": "Probability calibration",
    "evaluation.threshold.mode": "Decision threshold",
    "evaluation.threshold.value": "Threshold value",
    "evaluation.reasons_per_row": "Reasons per row",
    "evaluation.fairness_column": "Fairness check (sensitive column)",
    "evaluation.champion_min_improvement_pct": "Replace champion if better by (%)",
    "evaluation.shap": "Generate SHAP explanations per row",
    "actions.control_group_fraction": "Control group holdout (%)",
    "actions.suppression.recently_contacted_days": "Recently contacted (days)",
    "actions.suppression.suppress_opted_out": "Suppress opted-out customers",
    "actions.suppression.suppress_recently_contacted": "Suppress recently contacted",
    "monitoring.drift_psi_threshold": "Drift alert (PSI above)",
    "monitoring.retraining": "Retraining",
    "monitoring.performance_alert_drop_pct": "Performance alert (% drop)",
    "governance.retention_days": "Data retention (days)",
    "governance.consent_column": "Consent column (use rows where true)",
    "governance.approval_required": "Require approval before a model becomes champion",
}

USE_CASE_IDS = list_use_case_ids()


def _resolve(config: UseCaseConfig, path: str) -> Any:
    cursor: Any = config
    for part in path.replace("]", "").replace("[", ".").split("."):
        cursor = cursor[int(part)] if part.isdigit() else getattr(cursor, part)
    return cursor


def _jsonish(value: Any) -> Any:
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return str(value) if hasattr(value, "value") else value


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_eight_stages_numbered_with_verbatim_titles(use_case_id: str) -> None:
    schema = advanced_settings_schema(load_all_use_cases()[use_case_id])
    assert len(schema.stages) == 8
    assert [(s.number, s.id, s.title) for s in schema.stages] == list(STAGE_TITLES)
    assert schema.use_case_id == use_case_id


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_field_paths_are_the_forty_five_of_section_two(use_case_id: str) -> None:
    schema = advanced_settings_schema(load_all_use_cases()[use_case_id])
    emitted = tuple(field.path for stage in schema.stages for field in stage.fields)
    assert emitted == PATHS
    assert len(set(emitted)) == 45


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_every_value_and_default_is_read_from_the_config(use_case_id: str) -> None:
    config = load_all_use_cases()[use_case_id]
    for stage in advanced_settings_schema(config).stages:
        for field in stage.fields:
            expected = _jsonish(_resolve(config, field.path))
            assert field.value == expected, field.path
            assert field.default == field.value, field.path


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_labels_orders_and_bounds(use_case_id: str) -> None:
    schema = advanced_settings_schema(load_all_use_cases()[use_case_id])
    for stage in schema.stages:
        for order, field in enumerate(stage.fields, start=1):
            assert field.order == order
            if field.path in LABELS:
                assert field.label == LABELS[field.path]
            if field.path in BOUNDS:
                assert (field.min, field.max, field.step, field.scale) == BOUNDS[field.path]
            else:
                assert field.min is None and field.max is None and field.step is None


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_scale_is_on_exactly_the_three_percentage_fields(use_case_id: str) -> None:
    schema = advanced_settings_schema(load_all_use_cases()[use_case_id])
    scaled = {field.path for stage in schema.stages for field in stage.fields if field.scale is not None}
    assert scaled == {"split.validation_fraction", "split.test_fraction", "actions.control_group_fraction"}
    assert all(
        field.scale == 100 for stage in schema.stages for field in stage.fields if field.scale is not None
    )


def _field(schema_stages: Any, path: str) -> Any:
    return next(field for stage in schema_stages for field in stage.fields if field.path == path)


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_metric_choices_follow_metric_choices_order_with_catalog_labels(use_case_id: str) -> None:
    config = load_all_use_cases()[use_case_id]
    catalog = get_catalog()
    field = _field(advanced_settings_schema(config).stages, "model_search.metric")
    assert field.choices is not None
    assert [choice.value for choice in field.choices] == [m.value for m in config.model_search.metric_choices]
    assert [choice.label for choice in field.choices] == [
        catalog.metric_label(m) for m in config.model_search.metric_choices
    ]


def test_fault_prediction_offers_pr_auc_first() -> None:
    field = _field(advanced_settings_schema(load_use_case("fault-prediction")).stages, "model_search.metric")
    assert field.choices is not None
    assert field.choices[0].value == "pr_auc"
    assert field.choices[0].label == "PR-AUC"
    assert field.value == "pr_auc"


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_algorithm_grid_follows_the_candidate_pool(use_case_id: str) -> None:
    config = load_all_use_cases()[use_case_id]
    field = _field(advanced_settings_schema(config).stages, "model_search.candidates")
    assert field.choices is not None
    assert [choice.value for choice in field.choices] == [f.value for f in config.model_search.candidate_pool]
    assert field.min_selected == 1
    assert field.widget is Widget.MULTI_SELECT
    assert field.visible_when is not None
    assert (field.visible_when.path, field.visible_when.equals) == ("__ui.model", "__automl__")
    labels = {choice.value: choice.label for choice in field.choices}
    if "RandomForest" in labels:
        assert labels["RandomForest"] == "Random Forest"
    if "LogisticRegression" in labels:
        assert labels["LogisticRegression"] == "Logistic Regression"
    assert all(choice.enabled for choice in field.choices)


def test_a_family_that_needs_torch_is_greyed_out_not_dropped(tmp_path: Path) -> None:
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    shutil.copy(FIXTURES / "neural_demo.yaml", root / "use_cases" / "neural_demo.yaml")
    config = load_use_case("neural-demo", root)
    field = _field(advanced_settings_schema(config, torch_available=False).stages, "model_search.candidates")
    assert field.choices is not None
    neural = next(choice for choice in field.choices if choice.value == ModelFamily.NEURAL_NET.value)
    assert neural.enabled is False
    assert neural.help == "Requires the optional 'nn' extra"
    assert [choice.value for choice in field.choices] == ["XGBoost", "LightGBM", "NeuralNet"]

    enabled = _field(advanced_settings_schema(config, torch_available=True).stages, "model_search.candidates")
    assert enabled.choices is not None
    assert all(choice.enabled for choice in enabled.choices)


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_band_fields_are_one_per_non_floor_band(use_case_id: str) -> None:
    config = load_all_use_cases()[use_case_id]
    schema = advanced_settings_schema(config)
    band_fields = [field for stage in schema.stages for field in stage.fields if ".bands[" in field.path]
    assert len(band_fields) == len(config.actions.bands) - 1
    assert [field.label for field in band_fields] == [
        f"{band.name}-risk score ≥" for band in config.actions.bands[:-1]
    ]
    assert [field.label for field in band_fields] == ["High-risk score ≥", "Medium-risk score ≥"]


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_visible_when_is_on_exactly_two_config_paths(use_case_id: str) -> None:
    schema = advanced_settings_schema(load_all_use_cases()[use_case_id])
    conditional = {
        field.path: field.visible_when
        for stage in schema.stages
        for field in stage.fields
        if field.visible_when is not None
    }
    assert set(conditional) == {"split.time_column", "evaluation.threshold.value", "model_search.candidates"}
    assert conditional["split.time_column"].path == "split.type"
    assert conditional["split.time_column"].equals == "time_based"
    assert conditional["evaluation.threshold.value"].path == "evaluation.threshold.mode"
    assert conditional["evaluation.threshold.value"].equals == "manual"
    # The one non-config path: UI-local state (design section 2, row 19).
    assert conditional["model_search.candidates"].path == "__ui.model"


def test_column_widget_metadata() -> None:
    schema = advanced_settings_schema(load_use_case("targeted-advertisement"))
    exclude = _field(schema.stages, "prepare.exclude_columns")
    assert exclude.max_visible == 14
    assert exclude.widget is Widget.COLUMN_MULTI_SELECT
    assert exclude.column_source is ColumnSource.FEATURES
    assert _field(schema.stages, "split.time_column").empty_label == "Select…"
    assert _field(schema.stages, "split.time_column").column_source is ColumnSource.TIME_LIKE
    for path in ("split.group_column", "evaluation.fairness_column", "governance.consent_column"):
        assert _field(schema.stages, path).empty_label == "None"
        assert _field(schema.stages, path).column_source is ColumnSource.FEATURES
    # With no uploaded file the UI has nothing to offer yet.
    assert all(
        field.choices is None
        for stage in schema.stages
        for field in stage.fields
        if field.widget in (Widget.COLUMN_SELECT, Widget.COLUMN_MULTI_SELECT)
    )


def test_columns_populate_the_column_widgets() -> None:
    columns = ["customer_id", "snapshot_date", "visits_last_7d", "region", "converted_30d"]
    schema = advanced_settings_schema(
        load_use_case("targeted-advertisement"),
        columns=columns,
        primary_key="customer_id",
        target="converted_30d",
    )
    features = _field(schema.stages, "prepare.exclude_columns")
    assert features.choices is not None
    assert [choice.value for choice in features.choices] == ["snapshot_date", "visits_last_7d", "region"]
    time_like = _field(schema.stages, "split.time_column")
    assert time_like.choices is not None
    assert [choice.value for choice in time_like.choices] == ["snapshot_date"]


def test_time_like_falls_back_to_all_columns() -> None:
    columns = ["customer_id", "visits_last_7d", "region"]
    schema = advanced_settings_schema(
        load_use_case("targeted-advertisement"), columns=columns, primary_key="customer_id"
    )
    time_like = _field(schema.stages, "split.time_column")
    assert time_like.choices is not None
    assert [choice.value for choice in time_like.choices] == columns


def test_output_is_stable_across_two_calls() -> None:
    config = load_use_case("rca")
    first = advanced_settings_schema(config)
    second = advanced_settings_schema(config)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert first.ai_type is AiType.HYBRID


def test_the_static_field_table_matches_a_three_band_config() -> None:
    assert tuple(field.path for stage in FIELD_TABLE for field in stage.fields) == PATHS
    assert [(stage.number, stage.id, stage.title) for stage in FIELD_TABLE] == list(STAGE_TITLES)
    assert all(
        field.value is None and field.default is None for stage in FIELD_TABLE for field in stage.fields
    )


# ---------------------------------------------------------------------------
# DEC-058: settings the schema carries but no stage reads yet
# ---------------------------------------------------------------------------
def test_exactly_the_parked_settings_are_marked_advisory() -> None:
    """The set is written down once; this is the assertion that it is the set the form renders."""
    marked = {field.path for stage in FIELD_TABLE for field in stage.fields if field.advisory}
    assert marked == set(ADVISORY_PATHS)
    assert marked == {
        "features.auto_feature_engineering",
        "features.categorical_encoding",
        "features.numeric_scaling",
        "features.text_columns",
        "features.selection",
        "features.max_features",
        "monitoring.retraining",
        "monitoring.performance_alert_drop_pct",
        "governance.retention_days",
    }


def test_every_advisory_field_says_on_screen_why_it_is_disabled() -> None:
    for stage in FIELD_TABLE:
        for field in stage.fields:
            if field.advisory:
                assert field.help == ADVISORY_NOTE, field.path


def test_a_setting_the_engine_acts_on_is_never_marked_advisory() -> None:
    """The ones a stage really reads. If one of these is parked, something has been unwired."""
    wired = {
        "prepare.missing_values",
        "prepare.outliers",
        "prepare.pii_handling",
        "split.type",
        "split.validation_fraction",
        "model_search.metric",
        "model_search.strategy",
        "model_search.candidates",
        "model_search.tuning_trials",  # DEC-057 wired this one into AutoGluon HPO
        "model_search.time_limit_minutes",
        "model_search.folds",
        "model_search.imbalance",
        "evaluation.calibration",
        "evaluation.reasons_per_row",
        "actions.control_group_fraction",
        "monitoring.drift_psi_threshold",
        "governance.consent_column",
        "governance.approval_required",
    }
    marked = {field.path for stage in FIELD_TABLE for field in stage.fields if field.advisory}
    assert wired.isdisjoint(marked)


def test_the_schema_the_ui_receives_carries_the_advisory_flag() -> None:
    schema = advanced_settings_schema(load_use_case("targeted-advertisement"))
    payload = schema.model_dump(mode="json")
    fields = {field["path"]: field for stage in payload["stages"] for field in stage["fields"]}
    assert fields["features.max_features"]["advisory"] is True
    assert fields["features.max_features"]["help"] == ADVISORY_NOTE
    assert fields["model_search.tuning_trials"]["advisory"] is False


def test_an_advisory_setting_does_not_change_the_recipe_hash() -> None:
    """A control that changes no model must not make two identical models look different."""
    base = resolve_config("targeted-advertisement", {})
    moved = resolve_config(
        "targeted-advertisement",
        {"features.max_features": 300, "features.categorical_encoding": "one_hot"},
    )

    def recipe(resolved):
        return recipe_from_config(
            resolved.config, primary_key="customer_id", feature_columns=("a", "b"), seed=1
        )

    assert recipe(base).recipe_hash == recipe(moved).recipe_hash
    # ... and it is still recorded, so run_config.json and the manifest keep what was chosen.
    assert recipe(moved).features.max_features == 300


def test_a_wired_setting_still_changes_the_recipe_hash() -> None:
    base = resolve_config("targeted-advertisement", {})
    moved = resolve_config("targeted-advertisement", {"model_search.tuning_trials": 9})

    def recipe(resolved):
        return recipe_from_config(
            resolved.config, primary_key="customer_id", feature_columns=("a", "b"), seed=1
        )

    assert recipe(base).recipe_hash != recipe(moved).recipe_hash
