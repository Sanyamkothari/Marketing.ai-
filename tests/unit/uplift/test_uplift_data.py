"""`engine.uplift.data`: the treatment column, the two arrays, the feature spec and the hold-out.

The feature spec must honour Phase 1's verdicts (a `will_be_dropped` finding drops the column unless
the user acknowledged it; PII goes unless `pii_handling` is `keep`) instead of re-deriving them, and
must keep every configured experiment/run column out of the features.
"""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import RunMode, UseCaseConfig, load_use_case_document
from engine.contracts import Severity, ValidationCheck, ValidationReport
from engine.stages.validate import validate_for_training
from engine.uplift.config import UpliftConfig
from engine.uplift.data import (
    MAX_CATEGORY_LEVELS,
    FeatureSpec,
    apply_feature_spec,
    candidate_feature_columns,
    coerce_outcome,
    coerce_treatment,
    detect_treatment_column,
    fit_feature_spec,
    is_date_like,
    split_holdout,
)
from tests.fixtures.make_uplift_data import make_uplift_data

PK = "customer_id"
TARGET = "reactivated_90d"
FEATURES = (
    "age",
    "tenure_months",
    "visits_30d",
    "monthly_spend",
    "plan",
    "region",
    "support_tickets_90d",
    "noise_a",
)


def use_case(**sections: dict[str, Any]) -> UseCaseConfig:
    """`win-back-campaign`, template emptied, with the given top-level sections merged in."""
    document = copy.deepcopy(load_use_case_document("win-back-campaign"))
    document["template"] = {"columns": []}
    for name, values in sections.items():
        merged = dict(document.get(name) or {})
        merged.update(values)
        document[name] = merged
    return UseCaseConfig.model_validate(document)


def spec_for(frame: pd.DataFrame, config: UseCaseConfig, **kwargs: Any) -> FeatureSpec:
    return fit_feature_spec(
        frame, config, primary_key=PK, target=TARGET, treatment_column="treatment", **kwargs
    )


def report(*checks: ValidationCheck) -> ValidationReport:
    from datetime import UTC, datetime

    return ValidationReport(
        upload_id="u_test",
        mode=RunMode.TRAIN,
        checks=checks,
        error_count=0,
        warning_count=len(checks),
        passed=True,
        validated_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def finding(code: str, column: str, *, acknowledged: bool = False, drop: bool = True) -> ValidationCheck:
    details: dict[str, Any] = {"will_be_dropped": True} if drop else {}
    return ValidationCheck(
        code=code,
        severity=Severity.WARNING,
        message="m",
        column=column,
        details=details,
        acknowledgeable=True,
        acknowledged=acknowledged,
    )


# ---------------------------------------------------------------------------
# detect_treatment_column
# ---------------------------------------------------------------------------
def test_detects_first_hint_present_case_insensitively() -> None:
    config = UpliftConfig()
    assert detect_treatment_column(["a", "Treated", "contacted"], config) == "Treated"
    assert detect_treatment_column(["contacted", "is_treated"], config) == "contacted"
    assert detect_treatment_column(["x", "y"], config) is None


def test_configured_column_wins_and_is_never_replaced_by_a_hint() -> None:
    config = UpliftConfig(treatment_column="arm_flag")
    assert detect_treatment_column(["treatment", "ARM_FLAG"], config) == "ARM_FLAG"
    # Configured but absent: None, even though a hinted column exists.
    assert detect_treatment_column(["treatment"], config) is None


# ---------------------------------------------------------------------------
# coerce_treatment / coerce_outcome
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "values",
    [
        pd.Series([0, 1, 1, 0]),
        pd.Series([0.0, 1.0, 1.0, 0.0]),
        pd.Series([False, True, True, False]),
        pd.Series(["0", "1", " 1 ", "0"]),
        pd.Series(["false", "TRUE", "true", "False"]),
        pd.Series([np.int64(0), np.int64(1), True, "0"], dtype=object),
    ],
)
def test_coerce_treatment_accepts_the_binary_spellings(values: pd.Series) -> None:
    array, bad = coerce_treatment(values)
    assert bad == 0
    assert array is not None
    assert array.tolist() == [0, 1, 1, 0]
    assert array.dtype.kind == "i"


@pytest.mark.parametrize(
    ("values", "bad"),
    [
        (pd.Series([0, 1, 2, 1]), 1),
        (pd.Series([0.0, 1.0, np.nan, 0.5]), 2),
        (pd.Series(["0", "1", "yes", None, "control"]), 3),
        (pd.Series([True, None, False], dtype=object), 1),
    ],
)
def test_coerce_treatment_counts_every_bad_value_including_nulls(values: pd.Series, bad: int) -> None:
    array, counted = coerce_treatment(values)
    assert array is None
    assert counted == bad


def test_coerce_outcome_follows_phase_1_positive_label_rules() -> None:
    array, label = coerce_outcome(pd.Series([0, 1, 1, 0]), None)
    assert (array.tolist(), label) == ([0, 1, 1, 0], "1")
    array, label = coerce_outcome(pd.Series(["no", "YES", "yes"]), None)
    assert (array.tolist(), label) == ([0, 1, 1], "yes")
    array, label = coerce_outcome(pd.Series([True, False]), None)
    assert (array.tolist(), label) == ([1, 0], "true")
    # Configured label wins, matched by one normalised spelling (1 == 1.0 == "1").
    array, label = coerce_outcome(pd.Series([1.0, 0.0, 0.0]), 0)
    assert (array.tolist(), label) == ([0, 1, 1], "0")
    # No conventional token: the rarer value is the positive class.
    array, label = coerce_outcome(pd.Series(["stay", "left", "stay"]), None)
    assert (array.tolist(), label) == ([0, 1, 0], "left")


@pytest.mark.parametrize(
    ("values", "positive"),
    [
        (pd.Series([0, 1, 2]), None),
        (pd.Series([1, 1, 1]), None),
        (pd.Series([0, 1, None]), None),
        (pd.Series([0, 1, 0]), "yes"),
    ],
)
def test_coerce_outcome_refuses_a_non_binary_outcome(values: pd.Series, positive: object) -> None:
    with pytest.raises(ValueError, match=r"outcome|positive label"):
        coerce_outcome(values, positive)


# ---------------------------------------------------------------------------
# candidate_feature_columns
# ---------------------------------------------------------------------------
def test_candidates_exclude_every_reserved_and_date_like_column() -> None:
    frame = make_uplift_data(300, seed=1).frame.assign(
        treatment_date="2026-05-01",
        campaign="spring",
        signup_ts="2024-01-01",
        split_group="g",
        consent="yes",
        dropped_by_user=1.0,
        raw_epoch=1_700_000_000,
        loaded_at=pd.Timestamp("2026-01-01", tz="UTC"),
    )
    config = use_case(
        uplift={"treatment_date_column": "treatment_date", "campaign_id_column": "campaign"},
        split={"group_column": "split_group"},
        governance={"consent_column": "consent"},
        prepare={"exclude_columns": ["dropped_by_user"]},
    )
    columns = candidate_feature_columns(
        frame, config, primary_key=PK, target=TARGET, treatment_column="treatment"
    )
    # Integers are never read as dates; the text and datetime date columns are gone.
    assert columns == (*FEATURES, "raw_epoch")


def test_is_date_like_uses_phase_1_type_inference() -> None:
    assert is_date_like(pd.Series(["2026-01-01", "2026-02-03"]))
    assert is_date_like(pd.Series(pd.to_datetime(["2026-01-01"])))
    assert not is_date_like(pd.Series(["12", "13", "14"]))  # digit strings are numbers first
    assert not is_date_like(pd.Series([20260101, 20260102]))
    assert not is_date_like(pd.Series(["north", "south"]))


# ---------------------------------------------------------------------------
# fit_feature_spec
# ---------------------------------------------------------------------------
def test_generator_features_are_typed_and_levels_frozen() -> None:
    frame = make_uplift_data(500, seed=2).frame
    spec = spec_for(frame, use_case())
    assert spec.feature_columns == FEATURES
    assert set(spec.categorical_levels) == {"plan", "region"}
    assert spec.categorical_levels["plan"][0] == "basic"  # most frequent first
    assert set(spec.categorical_levels["region"]) == {"north", "south", "east", "west"}
    assert spec.dropped == {}


def test_phase_1_will_be_dropped_findings_drop_columns_unless_acknowledged() -> None:
    frame = make_uplift_data(300, seed=3).frame
    validation = report(
        finding("HIGH_NULL_COLUMN", "age"),
        finding("CONSTANT_COLUMN", "noise_a"),
        finding("HIGH_CARDINALITY_ID_LIKE", "monthly_spend", acknowledged=True),
        finding("HIGH_NULL_COLUMN", "visits_30d", drop=False),
    )
    spec = spec_for(frame, use_case(), validation=validation)
    assert spec.dropped == {"age": "high_null", "noise_a": "constant"}
    assert "monthly_spend" in spec.feature_columns  # acknowledged, so kept
    assert "visits_30d" in spec.feature_columns  # no will_be_dropped flag, so kept


def test_pii_findings_drop_columns_unless_pii_handling_is_keep() -> None:
    frame = make_uplift_data(300, seed=4).frame
    validation = report(finding("PII_DETECTED", "region", drop=False))
    assert spec_for(frame, use_case(), validation=validation).dropped == {"region": "pii"}
    kept = spec_for(frame, use_case(prepare={"pii_handling": "keep"}), validation=validation)
    assert "region" in kept.feature_columns


def test_without_a_report_pii_is_detected_by_the_engine_detector() -> None:
    frame = make_uplift_data(300, seed=5).frame
    frame["email"] = [f"person{index}@example.com" for index in range(len(frame))]
    spec = spec_for(frame, use_case())
    assert spec.dropped["email"] == "pii"
    assert "email" not in spec.feature_columns


def test_direct_constant_id_like_and_date_drops() -> None:
    frame = make_uplift_data(300, seed=6).frame
    frame["channel"] = "email"
    frame["empty"] = np.nan
    frame["account_ref"] = [f"A{index}" for index in range(len(frame))]
    frame["signup_date"] = "2024-03-01"
    frame["seen_at"] = pd.Timestamp("2026-01-01", tz="UTC")
    spec = spec_for(frame, use_case())
    assert spec.dropped == {
        "channel": "constant",
        "empty": "constant",
        "account_ref": "id_like",
        "signup_date": "date",
        "seen_at": "date",
    }
    # noise_a is unique on every row but numeric and not named like an identifier: a feature.
    assert "noise_a" in spec.feature_columns


def test_acknowledged_phase_1_finding_protects_against_the_direct_heuristic() -> None:
    frame = make_uplift_data(300, seed=7).frame
    frame["channel"] = "email"
    validation = report(finding("CONSTANT_COLUMN", "channel", acknowledged=True))
    assert "channel" in spec_for(frame, use_case(), validation=validation).feature_columns


def test_real_phase_1_report_is_honoured() -> None:
    frame = make_uplift_data(1200, seed=8).frame
    frame["mostly_empty"] = [1.0 if index % 10 == 0 else np.nan for index in range(len(frame))]
    config = use_case()
    validation = validate_for_training(frame, config, primary_key=PK, target=TARGET, upload_id="u_real")
    assert any(
        item.code == "HIGH_NULL_COLUMN" and item.column == "mostly_empty" for item in validation.checks
    )
    spec = spec_for(frame, config, validation=validation)
    assert spec.dropped["mostly_empty"] == "high_null"


def test_categorical_levels_are_capped_to_the_most_frequent() -> None:
    rows = 3000
    frame = make_uplift_data(rows, seed=9).frame
    # Level L0 appears most often, then L1, ...; 150 levels in all.
    frame["city"] = [f"L{min(index % 300, 149)}" for index in range(rows)]
    spec = spec_for(frame, use_case())
    levels = spec.categorical_levels["city"]
    assert len(levels) == MAX_CATEGORY_LEVELS
    assert levels[0] == "L149"  # it absorbs indices 149..299 of every 300, so it is the most frequent
    assert "L148" in levels


# ---------------------------------------------------------------------------
# apply_feature_spec
# ---------------------------------------------------------------------------
def test_apply_keeps_order_types_and_index_and_nulls_unseen_levels() -> None:
    frame = make_uplift_data(400, seed=10).frame
    spec = spec_for(frame, use_case())
    scoring = frame.iloc[:5].copy()
    scoring.index = [10, 11, 12, 13, 14]
    scoring.loc[10, "region"] = "atlantis"
    scoring["age"] = scoring["age"].astype(str)  # a CSV round trip must still score
    matrix = apply_feature_spec(scoring[list(reversed(scoring.columns))], spec)
    assert tuple(matrix.columns) == spec.feature_columns
    assert list(matrix.index) == [10, 11, 12, 13, 14]
    assert matrix["age"].dtype == np.float64
    assert isinstance(matrix["region"].dtype, pd.CategoricalDtype)
    assert tuple(matrix["region"].cat.categories) == spec.categorical_levels["region"]
    region = matrix["region"].astype(object)
    assert pd.isna(region.loc[10])
    assert region.loc[11] == scoring.loc[11, "region"]


def test_apply_names_a_missing_feature() -> None:
    frame = make_uplift_data(200, seed=11).frame
    spec = spec_for(frame, use_case())
    with pytest.raises(KeyError, match="visits_30d"):
        apply_feature_spec(frame.drop(columns=["visits_30d"]), spec)


def test_boolean_features_become_categoricals() -> None:
    frame = make_uplift_data(200, seed=12).frame
    frame["has_app"] = [index % 3 == 0 for index in range(len(frame))]
    spec = spec_for(frame, use_case())
    assert spec.categorical_levels["has_app"] == ("false", "true")
    matrix = apply_feature_spec(frame, spec)
    assert matrix["has_app"].iloc[0] == "true"


# ---------------------------------------------------------------------------
# split_holdout
# ---------------------------------------------------------------------------
def test_split_holdout_is_sorted_disjoint_complete_and_stratified() -> None:
    dataset = make_uplift_data(5000, seed=13)
    t = dataset.frame["treatment"].to_numpy()
    y = dataset.frame[TARGET].to_numpy()
    train, test = split_holdout(t, y, test_fraction=0.3, seed=1)
    assert np.all(np.diff(train) > 0) and np.all(np.diff(test) > 0)
    assert np.intersect1d(train, test).size == 0
    assert train.size + test.size == t.size
    for arm in (0, 1):
        for label in (0, 1):
            cell = int(((t == arm) & (y == label)).sum())
            in_test = int(((t[test] == arm) & (y[test] == label)).sum())
            assert in_test == int(np.floor(cell * 0.3 + 0.5))


def test_split_holdout_is_deterministic_by_seed() -> None:
    rng = np.random.default_rng(0)
    t = rng.integers(0, 2, 1000)
    y = rng.integers(0, 2, 1000)
    first = split_holdout(t, y, test_fraction=0.25, seed=5)
    again = split_holdout(t, y, test_fraction=0.25, seed=5)
    other = split_holdout(t, y, test_fraction=0.25, seed=6)
    assert np.array_equal(first[1], again[1])
    assert not np.array_equal(first[1], other[1])


def test_split_holdout_refuses_bad_input() -> None:
    with pytest.raises(ValueError, match="test_fraction"):
        split_holdout(np.array([0, 1]), np.array([0, 1]), test_fraction=1.0, seed=0)
    with pytest.raises(ValueError, match="same length"):
        split_holdout(np.array([0, 1]), np.array([0]), test_fraction=0.3, seed=0)
