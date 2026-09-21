"""The summary-template grammar and the eight rendered lines of design section 4.7."""

from __future__ import annotations

import pytest

from engine.config import (
    ConfigError,
    UseCaseConfig,
    advanced_settings_schema,
    list_use_case_ids,
    load_all_use_cases,
    load_use_case,
    load_use_case_document,
    render_stage_summary,
)

# Verbatim from design section 4.7 (targeted-advertisement on its defaults).
SUMMARIES = (
    "Missing: Auto · outliers: Clip (1st–99th pct) · leakage check · PII: Redact",
    "Random (stratified) · 15% val · 15% test",
    "Auto features on · encoding Auto · text: Ignore · selection: Importance-based · max 100",
    "Balanced · 4 of 4 algorithms · ensembling · 50 trials · 30 min · 5-fold CV",
    "ROC-AUC · calibration Isotonic · threshold Auto · top 3 SHAP reasons · champion if +1%",
    "High ≥ 0.8 · Medium ≥ 0.5 · skip opted-out · skip contacted <14d · 10% control group",
    "Drift alert PSI > 0.2 · retrain: On drift · alert if score drops 5%",
    "Keep uploads 90 days · approval before champion",
)


@pytest.fixture(scope="module")
def targeted() -> UseCaseConfig:
    return load_use_case("targeted-advertisement")


def _variant(**patch: object) -> UseCaseConfig:
    document = load_use_case_document("targeted-advertisement")
    for key, value in patch.items():
        block, _, field = key.partition("__")
        if field:
            document[block] = {**document[block], field: value}
        else:
            document[block] = value
    return UseCaseConfig.model_validate(document)


def test_the_eight_rendered_summaries_are_verbatim(targeted: UseCaseConfig) -> None:
    assert tuple(stage.summary for stage in advanced_settings_schema(targeted).stages) == SUMMARIES


def test_every_stage_carries_the_template_it_was_rendered_from(targeted: UseCaseConfig) -> None:
    for stage in advanced_settings_schema(targeted).stages:
        assert stage.summary == render_stage_summary(stage.summary_template, targeted)
        assert "{" in stage.summary_template


def test_time_column_segment_appears_only_when_a_time_column_is_set() -> None:
    order_fulfillment = load_use_case("order-fulfillment")
    assert advanced_settings_schema(order_fulfillment).stages[1].summary == (
        "Time-based · 15% val · 15% test · by snapshot_date"
    )
    without_column = _variant(
        split={
            "type": "time_based",
            "time_column": None,
            "validation_fraction": 0.15,
            "test_fraction": 0.15,
            "group_column": None,
        }
    )
    assert advanced_settings_schema(without_column).stages[1].summary == "Time-based · 15% val · 15% test"


def test_conditional_and_filter_forms() -> None:
    config = _variant(
        prepare={
            "missing_values": "fill",
            "outliers": "keep",
            "pii_handling": "keep",
            "deduplicate": False,
            "exclude_columns": ["plan_tier", "tenure_months"],
        },
        features={
            "auto_feature_engineering": False,
            "categorical_encoding": "one_hot",
            "numeric_scaling": "none",
            "text_columns": "tfidf",
            "selection": "none",
            "max_features": 40,
        },
        evaluation={
            "calibration": "none",
            "threshold": {"mode": "manual", "value": 0.7},
            "shap": False,
            "reasons_per_row": 3,
            "fairness_column": "region",
            "champion_min_improvement_pct": 2.5,
        },
        governance={"retention_days": 0, "consent_column": "marketing_opt_in", "approval_required": False},
    )
    stages = advanced_settings_schema(config).stages
    # {?path: text} on a falsy value disappears; {path|count} counts.
    assert stages[0].summary == (
        "Missing: Fill (median / mode) · outliers: Keep · leakage check · PII: Keep · 2 excluded"
    )
    # {?!path: text} fires on the falsy value.
    assert stages[2].summary.startswith("Auto features off · encoding One-hot")
    # {?path: ...} suppressed for shap; the fairness segment appears.
    assert stages[4].summary == (
        "ROC-AUC · calibration None · threshold Manual (below) · fairness by region · champion if +2.5%"
    )
    assert stages[7].summary == "Keep uploads 0 days · consent: marketing_opt_in"


def test_equality_conditional_and_lower_filter(targeted: UseCaseConfig) -> None:
    assert (
        render_stage_summary("{?split.type=random_stratified:yes}{?split.type=time_based:no}", targeted)
        == "yes"
    )
    assert render_stage_summary("{model_search.strategy|lower}", targeted) == "balanced"
    assert render_stage_summary("{model_search.strategy|value}", targeted) == "balanced"
    assert render_stage_summary("{split.test_fraction|pct}", targeted) == "15"
    assert render_stage_summary("{model_search.candidates|count}", targeted) == "4"
    assert render_stage_summary("{split.time_column}", targeted) == ""
    assert render_stage_summary("no placeholders here", targeted) == "no placeholders here"


def test_unknown_path_raises(targeted: UseCaseConfig) -> None:
    with pytest.raises(ConfigError) as error:
        render_stage_summary("{prepare.nonsense}", targeted)
    assert error.value.code == "SUMMARY_UNKNOWN_PATH"
    with pytest.raises(ConfigError) as conditional:
        render_stage_summary("{?nope.nope: x}", targeted)
    assert conditional.value.code == "SUMMARY_UNKNOWN_PATH"


def test_unbalanced_template_raises(targeted: UseCaseConfig) -> None:
    with pytest.raises(ConfigError) as error:
        render_stage_summary("{prepare.outliers", targeted)
    assert error.value.code == "SUMMARY_TEMPLATE_UNBALANCED"


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_every_template_renders_for_every_use_case(use_case_id: str) -> None:
    config = load_all_use_cases()[use_case_id]
    for stage in advanced_settings_schema(config).stages:
        rendered = render_stage_summary(stage.summary_template, config)
        assert rendered
        assert "{" not in rendered and "}" not in rendered
