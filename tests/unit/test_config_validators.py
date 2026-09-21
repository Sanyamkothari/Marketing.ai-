"""Every validator of design section 4.3/4.4, by its machine code."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from engine.config import (
    ActionsConfig,
    AiType,
    Band,
    Calibration,
    CategoricalEncoding,
    ColumnRole,
    ColumnSource,
    ColumnType,
    ConfigError,
    EvaluationConfig,
    FeatureSelection,
    FieldType,
    Imbalance,
    Metric,
    MissingValues,
    ModelFamily,
    ModelSearchConfig,
    NumericScaling,
    Outliers,
    PiiHandling,
    PrepareConfig,
    ProblemType,
    Retraining,
    RunMode,
    SplitConfig,
    SplitType,
    Strategy,
    TemplateConfig,
    TextHandling,
    ThresholdConfig,
    ThresholdMode,
    UseCaseConfig,
    UseCaseStatus,
    ValidationConfig,
    Widget,
    load_use_case_document,
    resolve_config,
)

ALL_ENUMS = (
    AiType,
    ProblemType,
    MissingValues,
    Outliers,
    PiiHandling,
    SplitType,
    CategoricalEncoding,
    NumericScaling,
    TextHandling,
    FeatureSelection,
    Strategy,
    Imbalance,
    Metric,
    Calibration,
    ThresholdMode,
    Retraining,
    ModelFamily,
    ColumnRole,
    ColumnType,
    UseCaseStatus,
    RunMode,
    Widget,
    ColumnSource,
    FieldType,
)


def _document(**patch: Any) -> dict[str, Any]:
    document = copy.deepcopy(load_use_case_document("targeted-advertisement"))
    for path, value in patch.items():
        cursor: Any = document
        parts = path.split("__")
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor[parts[-1]] = value
    return document


def _raises(code: str, **patch: Any) -> ConfigError:
    with pytest.raises(ConfigError) as error:
        UseCaseConfig.model_validate(_document(**patch))
    assert error.value.code == code
    return error.value


def test_there_are_twenty_four_enums() -> None:
    assert len(ALL_ENUMS) == 24
    assert len(set(ALL_ENUMS)) == 24


@pytest.mark.parametrize("enum_type", ALL_ENUMS, ids=[enum.__name__ for enum in ALL_ENUMS])
def test_every_enum_rejects_an_unknown_string(enum_type: type) -> None:
    with pytest.raises(ValueError):
        enum_type("definitely-not-a-member")


def test_time_based_without_a_time_column_loads_and_only_warns() -> None:
    config = UseCaseConfig.model_validate(_document(split={"type": "time_based", "time_column": None}))
    assert config.split.type is SplitType.TIME_BASED
    assert config.split.time_column is None
    assert resolve_config("order-fulfillment", {"split.time_column": ""}).warnings == (
        "split.type is time_based and no time column is set yet",
    )


def test_split_fraction_and_column_rules() -> None:
    with pytest.raises(ConfigError) as too_large:
        SplitConfig(validation_fraction=0.35, test_fraction=0.35)
    assert too_large.value.code == "SPLIT_FRACTIONS_TOO_LARGE"

    with pytest.raises(ConfigError) as same_column:
        SplitConfig(time_column="snapshot_date", group_column="snapshot_date")
    assert same_column.value.code == "SPLIT_GROUP_EQUALS_TIME"


def test_band_rules() -> None:
    descending = [
        {"name": "High", "min_score": 0.80, "action": "Act"},
        {"name": "Medium", "min_score": 0.85, "action": "Watch"},
        {"name": "Low", "min_score": 0.00, "action": "None"},
    ]
    with pytest.raises(ConfigError) as order:
        ActionsConfig(bands=descending)
    assert order.value.code == "BANDS_NOT_DESCENDING"
    assert "Medium-risk score (0.85) must be below High-risk score (0.80)." in order.value.message

    with pytest.raises(ConfigError) as floor:
        ActionsConfig(
            bands=[
                {"name": "High", "min_score": 0.80, "action": "Act"},
                {"name": "Low", "min_score": 0.10, "action": "None"},
            ]
        )
    assert floor.value.code == "BANDS_LAST_NOT_ZERO"

    with pytest.raises(ConfigError) as duplicate:
        ActionsConfig(
            bands=[
                {"name": "High", "min_score": 0.80, "action": "Act"},
                {"name": "High", "min_score": 0.50, "action": "Watch"},
                {"name": "Low", "min_score": 0.00, "action": "None"},
            ]
        )
    assert duplicate.value.code == "BANDS_DUPLICATE_NAME"

    with pytest.raises(ConfigError) as too_few:
        ActionsConfig(bands=[{"name": "Only", "min_score": 0.00, "action": "None"}])
    assert too_few.value.code == "BANDS_TOO_FEW"


def test_band_for_is_a_pure_lookup() -> None:
    actions = ActionsConfig()
    assert actions.band_for(0.95) == Band(name="High", min_score=0.80, action="Act now")
    assert actions.band_for(0.50).name == "Medium"
    assert actions.band_for(0.0).name == "Low"


def test_threshold_fixed_must_be_half() -> None:
    with pytest.raises(ConfigError) as error:
        ThresholdConfig(mode="fixed", value=0.7)
    assert error.value.code == "THRESHOLD_FIXED_NOT_HALF"
    assert ThresholdConfig(mode="fixed").value == 0.50
    assert ThresholdConfig(mode="manual", value=0.7).value == 0.7
    assert EvaluationConfig().threshold.mode is ThresholdMode.AUTO


def test_metric_rules() -> None:
    _raises("METRIC_NOT_FOR_PROBLEM", model_search__metric="rmse", model_search__metric_choices=["rmse"])
    with pytest.raises(ConfigError) as not_in_choices:
        ModelSearchConfig(metric="recall", metric_choices=["roc_auc"])
    assert not_in_choices.value.code == "METRIC_NOT_IN_CHOICES"
    with pytest.raises(ConfigError) as empty:
        ModelSearchConfig(metric_choices=[])
    assert empty.value.code == "METRIC_CHOICES_EMPTY"
    with pytest.raises(ConfigError) as duplicate:
        ModelSearchConfig(metric_choices=["roc_auc", "roc_auc"])
    assert duplicate.value.code == "METRIC_CHOICES_DUPLICATE"


def test_candidate_rules() -> None:
    with pytest.raises(ConfigError) as outside:
        ModelSearchConfig(candidate_pool=["XGBoost"], candidates=["LightGBM"])
    assert outside.value.code == "MODEL_SEARCH_NOT_IN_POOL"
    with pytest.raises(ConfigError) as empty:
        ModelSearchConfig(candidates=[])
    assert empty.value.code == "MODEL_SEARCH_NO_CANDIDATES"
    with pytest.raises(ConfigError) as duplicate:
        ModelSearchConfig(candidates=["XGBoost", "XGBoost"])
    assert duplicate.value.code == "MODEL_SEARCH_DUPLICATE"


def test_target_is_required_unless_generative() -> None:
    _raises("TARGET_COLUMN_REQUIRED", target={"column": None}, template={"columns": []})


def test_template_rules() -> None:
    document = _document()
    columns = copy.deepcopy(document["template"]["columns"])
    columns[-1]["name"] = "something_else"
    _raises("TEMPLATE_TARGET_MISMATCH", template={"columns": columns})

    two_keys = copy.deepcopy(document["template"]["columns"])
    two_keys[1]["role"] = "primary_key"
    with pytest.raises(ConfigError) as many:
        TemplateConfig(columns=two_keys)
    assert many.value.code == "TEMPLATE_MANY_PRIMARY_KEYS"

    duplicated = copy.deepcopy(document["template"]["columns"])
    duplicated[1]["name"] = duplicated[0]["name"]
    with pytest.raises(ConfigError) as dupe:
        TemplateConfig(columns=duplicated)
    assert dupe.value.code == "TEMPLATE_DUPLICATE_COLUMN"

    no_key = [column for column in document["template"]["columns"] if column["role"] != "primary_key"]
    with pytest.raises(ConfigError) as missing_key:
        TemplateConfig(columns=no_key)
    assert missing_key.value.code == "TEMPLATE_NO_PRIMARY_KEY"


def test_template_time_and_consent_cross_checks() -> None:
    _raises(
        "TEMPLATE_TIME_MISSING",
        split={
            "type": "time_based",
            "time_column": "not_a_column",
            "validation_fraction": 0.15,
            "test_fraction": 0.15,
            "group_column": None,
        },
    )
    _raises(
        "TEMPLATE_CONSENT_MISSING",
        governance={"retention_days": 90, "consent_column": "nope", "approval_required": True},
    )


def test_kpi_band_and_column_checks() -> None:
    _raises(
        "KPI_UNKNOWN_BAND", output={"kpi": {"label": "X", "formula": 'count_where_band_in(["Critical"])'}}
    )
    _raises(
        "KPI_UNKNOWN_COLUMN",
        output={"kpi": {"label": "X", "formula": 'sum_where_band_in("nope", ["High"])'}},
    )


def test_excluded_columns_may_not_be_columns_the_run_needs() -> None:
    error = _raises("COLUMN_EXCLUDED_AND_USED", prepare={"exclude_columns": ["converted_30d"]})
    assert "the target column" in error.message
    _raises("COLUMN_EXCLUDED_AND_USED", prepare={"exclude_columns": ["marketing_opt_in"]})


def test_prepare_and_validation_list_rules() -> None:
    with pytest.raises(ConfigError) as duplicate:
        PrepareConfig(exclude_columns=["region", "region"])
    assert duplicate.value.code == "PREPARE_EXCLUDE_DUPLICATE"
    with pytest.raises(ConfigError) as empty:
        PrepareConfig(exclude_columns=[""])
    assert empty.value.code == "PREPARE_EXCLUDE_EMPTY"
    with pytest.raises(ConfigError) as inverted:
        ValidationConfig(imbalance_warn_min_rate=0.9, imbalance_warn_max_rate=0.1)
    assert inverted.value.code == "VALIDATION_RATES_INVERTED"
    with pytest.raises(ConfigError) as malformed:
        ValidationConfig(acknowledged=["leakage suspected"])
    assert malformed.value.code == "VALIDATION_ACK_MALFORMED"
    with pytest.raises(ConfigError) as repeated:
        ValidationConfig(acknowledged=["LEAKAGE_SUSPECTED", "LEAKAGE_SUSPECTED"])
    assert repeated.value.code == "VALIDATION_ACK_DUPLICATE"
    assert ValidationConfig(acknowledged=["LEAKAGE_SUSPECTED:converted_30d"]).acknowledged[0].endswith("_30d")


def test_every_config_model_is_frozen_and_forbids_extras() -> None:
    config = UseCaseConfig.model_validate(_document())
    with pytest.raises(ValueError):
        config.entity = "other"  # type: ignore[misc]
    assert config.model_config["extra"] == "forbid"
    assert config.model_config["frozen"] is True
    assert isinstance(config.model_search.candidates, tuple)
    assert isinstance(config.template.columns, tuple)
