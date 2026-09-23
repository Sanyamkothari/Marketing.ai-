"""The 44 `defaultAdv()` keys of the prototype against the merged configs (DEC-004).

The table below is transcribed from the design's section 2 mapping table (which lists every
`defaultAdv()` key and its config path) and section 1.1 (which lists every value the design
deliberately changed). Thirty-six keys must survive the move to YAML unchanged; the eight in
`DEVIATIONS` must differ, and each names the decision that allows it — delete the decision and
this test fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine.config import (
    ModelFamily,
    ThresholdConfig,
    ThresholdMode,
    UseCaseConfig,
    list_use_case_ids,
    load_all_use_cases,
)

DECISIONS = Path(__file__).resolve().parents[2] / "docs" / "DECISIONS.md"

# defaultAdv key -> (config path, prototype value)
PROTOTYPE_DEFAULTS: dict[str, tuple[str, Any]] = {
    "missing": ("prepare.missing_values", "auto"),
    "outliers": ("prepare.outliers", "clip"),
    "pii": ("prepare.pii_handling", "redact"),
    "minpos": ("validation.min_positive", 500),
    "dedupe": ("prepare.deduplicate", True),
    "leakage": ("validation.leakage_check", True),
    "excl": ("prepare.exclude_columns", []),
    "split": ("split.type", "random_stratified"),
    "val": ("split.validation_fraction", 15),
    "test": ("split.test_fraction", 15),
    "timeCol": ("split.time_column", ""),
    "groupCol": ("split.group_column", ""),
    "autofeat": ("features.auto_feature_engineering", True),
    "encoding": ("features.categorical_encoding", "auto"),
    "scaling": ("features.numeric_scaling", "auto"),
    "text": ("features.text_columns", "ignore"),
    "fsel": ("features.selection", "importance"),
    "maxfeat": ("features.max_features", 100),
    "algos": ("model_search.candidates", "SETUP[id].models"),
    "strategy": ("model_search.strategy", "balanced"),
    "trials": ("model_search.tuning_trials", 50),
    "limit": ("model_search.time_limit_minutes", 30),
    "folds": ("model_search.folds", 5),
    "imbalance": ("model_search.imbalance", "auto"),
    "ensemble": ("model_search.ensemble", True),
    "metric": ("model_search.metric", "roc_auc"),
    "calib": ("evaluation.calibration", "isotonic"),
    "thr": ("evaluation.threshold.mode", "Auto"),
    "reasons": ("evaluation.reasons_per_row", 3),
    "fair": ("evaluation.fairness_column", ""),
    "champ": ("evaluation.champion_min_improvement_pct", 1),
    "shap": ("evaluation.shap", True),
    "hi": ("actions.bands[0].min_score", 0.8),
    "md": ("actions.bands[1].min_score", 0.5),
    "control": ("actions.control_group_fraction", 10),
    "recentDays": ("actions.suppression.recently_contacted_days", 14),
    "supOpt": ("actions.suppression.suppress_opted_out", True),
    "supRecent": ("actions.suppression.suppress_recently_contacted", True),
    "drift": ("monitoring.drift_psi_threshold", 0.2),
    "retrain": ("monitoring.retraining", "on_drift"),
    "alert": ("monitoring.performance_alert_drop_pct", 5),
    "retention": ("governance.retention_days", 90),
    "consentCol": ("governance.consent_column", ""),
    "approval": ("governance.approval_required", True),
}

DEVIATIONS: dict[str, str] = {
    "minpos": "DEC-004",
    "test": "DEC-005",
    "val": "DEC-005",
    "control": "DEC-005",
    "thr": "DEC-006",
    "hi": "DEC-008",
    "md": "DEC-008",
    "algos": "DEC-009",
}

# What the use case's own YAML legitimately changes (design section 3), keyed by use case.
PER_USE_CASE: dict[str, dict[str, Any]] = {
    "order-fulfillment": {"split": "time_based", "timeCol": "snapshot_date"},
    "fault-prediction": {"split": "time_based", "timeCol": "snapshot_date", "metric": "pr_auc"},
    "rca": {"split": "time_based", "timeCol": "snapshot_date"},
    # From the library (DEC-085): ranked on the positives, as its own YAML explains.
    "retail-win-back": {"metric": "pr_auc"},
}


def _resolve(config: UseCaseConfig, path: str) -> Any:
    cursor: Any = config
    for part in path.replace("]", "").replace("[", ".").split("."):
        cursor = cursor[int(part)] if part.isdigit() else getattr(cursor, part)
    return cursor


def _equal(actual: Any, expected: Any) -> bool:
    """Prototype equality: "" and None are both "not selected"; lists and tuples are one shape."""
    if actual is None and expected == "":
        return True
    if isinstance(actual, (tuple, list)) and isinstance(expected, (tuple, list)):
        return [str(item) for item in actual] == [str(item) for item in expected]
    return bool(actual == expected)


def _expected(use_case_id: str, key: str) -> Any:
    return PER_USE_CASE.get(use_case_id, {}).get(key, PROTOTYPE_DEFAULTS[key][1])


UNCHANGED = tuple(key for key in PROTOTYPE_DEFAULTS if key not in DEVIATIONS)


def test_the_table_is_the_whole_prototype_default_set() -> None:
    assert len(PROTOTYPE_DEFAULTS) == 44
    assert len(DEVIATIONS) == 8
    assert len(UNCHANGED) == 36
    assert set(DEVIATIONS) <= set(PROTOTYPE_DEFAULTS)


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
@pytest.mark.parametrize("key", UNCHANGED)
def test_thirty_six_prototype_defaults_survive_unchanged(key: str, use_case_id: str) -> None:
    config = load_all_use_cases()[use_case_id]
    path, _ = PROTOTYPE_DEFAULTS[key]
    actual = _resolve(config, path)
    expected = _expected(use_case_id, key)
    assert _equal(
        actual, expected
    ), f"{use_case_id}: {key} ({path}) is {actual!r}, prototype had {expected!r}"


def test_min_positive_follows_the_plan_not_the_prototype() -> None:
    config = load_all_use_cases()["targeted-advertisement"]
    assert PROTOTYPE_DEFAULTS["minpos"][1] == 500
    assert config.validation.min_positive == 200


@pytest.mark.parametrize(
    ("key", "path", "value"),
    [
        ("val", "split.validation_fraction", 0.15),
        ("test", "split.test_fraction", 0.15),
        ("control", "actions.control_group_fraction", 0.10),
    ],
)
def test_percent_keys_became_fractions(key: str, path: str, value: float) -> None:
    config = load_all_use_cases()["targeted-advertisement"]
    assert _resolve(config, path) == value == PROTOTYPE_DEFAULTS[key][1] / 100


def test_threshold_became_an_object() -> None:
    config = load_all_use_cases()["targeted-advertisement"]
    assert isinstance(config.evaluation.threshold, ThresholdConfig)
    assert config.evaluation.threshold.mode is ThresholdMode.AUTO
    assert config.evaluation.threshold.mode.value != PROTOTYPE_DEFAULTS["thr"][1]
    assert config.evaluation.threshold.value == 0.50


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_hi_and_md_became_band_list_entries(use_case_id: str) -> None:
    bands = load_all_use_cases()[use_case_id].actions.bands
    assert [band.min_score for band in bands] == [0.80, 0.50, 0.00]
    assert bands[0].min_score == PROTOTYPE_DEFAULTS["hi"][1]
    assert bands[1].min_score == PROTOTYPE_DEFAULTS["md"][1]
    # The list carries what two loose numbers cannot: names and actions.
    assert [band.name for band in bands] == ["High", "Medium", "Low"]
    assert all(band.action for band in bands)


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_algorithms_are_restricted_to_the_supported_families(use_case_id: str) -> None:
    search = load_all_use_cases()[use_case_id].model_search
    assert set(search.candidates) <= set(search.candidate_pool) <= set(ModelFamily)
    assert search.candidates


@pytest.mark.parametrize(("key", "decision"), sorted(DEVIATIONS.items()))
def test_every_deviation_names_a_decision_that_still_exists(key: str, decision: str) -> None:
    assert key in PROTOTYPE_DEFAULTS
    assert decision in DECISIONS.read_text(encoding="utf-8")
