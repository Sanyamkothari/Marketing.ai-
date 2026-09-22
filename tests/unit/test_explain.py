"""`engine.stages.explain`: the global chart, the three reason tiers and the parquet round trip.

The tiers are exercised for real, without AutoGluon: `RowScorer` is a protocol, so a fake predictor
that answers the handful of calls each tier makes is enough to drive TreeSHAP over a genuine
scikit-learn forest, KernelSHAP over a genuine model function, and the permutation tier over
nothing but a scoring callable. Each tier is forced by giving the fake exactly what that tier needs
and nothing the tier above it needs, so the fallback chain is observed rather than asserted about.

Every expected number here is worked out independently of the implementation - normalised shares by
hand from the definition, permutation contributions from `base - replaced` computed in the test.

The last test is marked `slow`: it trains a real model with the train stage and explains it, which
is the only way to find out whether the AutoGluon calls in this module are spelled correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

import engine.stages.explain as explain_module
from engine.config import Strategy, load_use_case, recipe_from_config
from engine.contracts import (
    Direction,
    FeatureImportance,
    Reason,
    RowExplanation,
    scores_csv_columns,
)
from engine.stages.explain import (
    DOWN_ARROW,
    EXPLAIN_MAX_ROWS,
    GENERAL_SUFFIX,
    IMPORTANCE_CAPTION,
    IMPORTANCE_UNAVAILABLE_CAPTION,
    KERNEL_SHAP_MAX_ROWS,
    MISSING_VALUE,
    TOP_FEATURES,
    UP_ARROW,
    RowReasons,
    build_feature_importance,
    build_row_explanations,
    explain_detail,
    fallback_note,
    format_value,
    global_importance,
    read_row_explanations,
    reason_column_names,
    reason_columns,
    reason_text,
    reasons_for,
    reasons_for_row,
    row_explanation_schema,
    row_reasons,
    unavailable_importance,
    with_reason_columns,
    write_row_explanations,
)
from engine.stages.export import _reason_column
from engine.storage import LocalStorage
from engine.utils.ids import seed_from

RUN_ID = "r_20260921_0000beef"
USE_CASE = "targeted-advertisement"
TARGET = "converted_30d"
PRIMARY_KEY = "customer_id"
FEATURES = ("visits_last_7d", "spend_last_30d", "plan_tier")


# ---------------------------------------------------------------------------
# Fixtures and test doubles
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def config():
    """The real use case, so the reason count and the time budget come from a shipped file."""
    return load_use_case(USE_CASE)


def with_reasons(config, count: int):
    return config.model_copy(
        update={"evaluation": config.evaluation.model_copy(update={"reasons_per_row": count})}
    )


def make_frame(rows: int = 40) -> pd.DataFrame:
    """A small frame with a numeric, a second numeric and a categorical feature."""
    rng = np.random.default_rng(7)
    return pd.DataFrame(
        {
            PRIMARY_KEY: [f"c{index:03d}" for index in range(rows)],
            "visits_last_7d": rng.integers(0, 20, size=rows).astype(float),
            "spend_last_30d": rng.normal(100.0, 25.0, size=rows).round(2),
            "plan_tier": rng.choice(["basic", "plus", "pro"], size=rows),
        }
    )


def logistic(frame: pd.DataFrame) -> pd.Series:
    """A scoring rule the tests can differentiate by hand: two numerics, one ignored category."""
    logit = 0.20 * frame["visits_last_7d"].astype(float) - 0.02 * frame["spend_last_30d"].astype(float)
    return pd.Series(1.0 / (1.0 + np.exp(-logit.to_numpy())), index=frame.index, dtype="float64")


@dataclass
class FakeScorer:
    """Everything the tiers ask of a fitted model, and nothing else."""

    predictor: Any
    feature_columns: tuple[str, ...] = FEATURES
    target_column: str = TARGET
    calls: list[int] = field(default_factory=list)

    def score(self, frame: pd.DataFrame) -> pd.Series:
        self.calls.append(len(frame))
        return logistic(frame)


class NumericPredictor:
    """Tier 2's predictor: it transforms features and predicts, and knows nothing of trainers."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.numeric = [column for column in FEATURES if column != "plan_tier"]
        self.seen: list[pd.DataFrame] = []

    def transform_features(self, data: pd.DataFrame) -> pd.DataFrame:
        return data[self.numeric].astype(float)

    def predict_proba(self, data: pd.DataFrame, as_multiclass: bool = True, transform_features: bool = True):
        assert as_multiclass is False and transform_features is False
        self.seen.append(data)
        return logistic(data).to_numpy()


class ForestModel:
    """An AutoGluon model wrapper around a real scikit-learn forest, as tier 1 expects to find."""

    def __init__(self, frame: pd.DataFrame, columns: list[str]) -> None:
        from sklearn.ensemble import RandomForestClassifier

        self.columns = columns
        features = frame[columns].astype(float)
        labels = (logistic(frame) > 0.5).astype(int)
        self.model = RandomForestClassifier(n_estimators=6, random_state=0).fit(features, labels)

    def preprocess(self, data: pd.DataFrame) -> pd.DataFrame:
        return data[self.columns].astype(float)


class TreePredictor(NumericPredictor):
    """Tier 1's predictor: a trainer holding one linear model and one tree model."""

    def __init__(self, frame: pd.DataFrame, *, tree: bool = True) -> None:
        super().__init__(frame)
        self.models = {"LinearModel": None}
        if tree:
            self.models["RandomForest"] = ForestModel(frame, self.numeric)
        self._trainer = self
        self.loaded: list[str] = []

    def load_model(self, name: str):
        self.loaded.append(name)
        return self.models[name]

    def leaderboard(self, display: bool = False) -> pd.DataFrame:
        assert display is False
        return pd.DataFrame({"model": list(self.models), "score_val": [0.9, 0.8][: len(self.models)]})

    def model_names(self) -> list[str]:
        return list(self.models)


class LinearOnlyPredictor(TreePredictor):
    """A predictor whose only model is one TreeSHAP cannot explain, so tier 1 must decline."""

    def __init__(self, frame: pd.DataFrame) -> None:
        super().__init__(frame, tree=False)


def shares_sum(importance) -> Decimal:
    """The shares added up as the page prints them, so 33.3 + 33.3 + 33.4 is exactly 100.0."""
    return sum((Decimal(str(item.share_pct)) for item in importance.items), start=Decimal("0"))


def importance_frame(values: dict[str, float], **columns: list[float]) -> pd.DataFrame:
    """AutoGluon's `feature_importance` shape: indexed by feature, `importance` plus extras (D13)."""
    return pd.DataFrame({"importance": list(values.values()), **columns}, index=list(values))


# ---------------------------------------------------------------------------
# Global importance
# ---------------------------------------------------------------------------
def test_shares_are_percentages_that_sum_to_exactly_one_hundred() -> None:
    # 6 / 10 = 60%, 3 / 10 = 30%, 1 / 10 = 10%; by hand, not by the implementation.
    importance = build_feature_importance(importance_frame({"a": 6.0, "b": 3.0, "c": 1.0}), run_id=RUN_ID)
    assert [item.share_pct for item in importance.items] == [60.0, 30.0, 10.0]
    assert shares_sum(importance) == Decimal("100.0")
    assert [item.rank for item in importance.items] == [1, 2, 3]
    assert [item.feature for item in importance.items] == ["a", "b", "c"]
    assert importance.method == "permutation"
    assert importance.computed_on == "test"
    assert importance.caption == IMPORTANCE_CAPTION
    assert importance.top_n == 3


def test_the_last_share_absorbs_the_rounding_residue() -> None:
    # Three equal features: 33.333...% each rounds to 33.3, which sums to 99.9, so the last one
    # carries the missing 0.1 and the bar chart still adds up to a whole.
    importance = build_feature_importance(importance_frame({"a": 1.0, "b": 1.0, "c": 1.0}), run_id=RUN_ID)
    assert [item.share_pct for item in importance.items] == [33.3, 33.3, 33.4]
    assert shares_sum(importance) == Decimal("100.0")


def test_a_negative_importance_is_reported_but_never_a_negative_share() -> None:
    importance = build_feature_importance(importance_frame({"a": 4.0, "b": -1.0}), run_id=RUN_ID)
    assert [item.importance for item in importance.items] == [4.0, -1.0]
    assert [item.share_pct for item in importance.items] == [100.0, 0.0]


def test_importance_that_is_zero_everywhere_gives_zero_shares() -> None:
    importance = build_feature_importance(importance_frame({"a": 0.0, "b": -2.0}), run_id=RUN_ID)
    assert [item.share_pct for item in importance.items] == [0.0, 0.0]


def test_only_the_top_twenty_features_reach_the_chart() -> None:
    values = {f"f{index:02d}": float(30 - index) for index in range(30)}
    importance = build_feature_importance(importance_frame(values), run_id=RUN_ID)
    assert importance.top_n == TOP_FEATURES == len(importance.items)
    assert [item.feature for item in importance.items] == [f"f{index:02d}" for index in range(20)]
    assert shares_sum(importance) == Decimal("100.0")


def test_a_frame_that_arrives_unsorted_is_ranked_by_importance() -> None:
    importance = build_feature_importance(importance_frame({"a": 1.0, "b": 9.0}), run_id=RUN_ID)
    assert [item.feature for item in importance.items] == ["b", "a"]


def test_the_confidence_band_columns_are_carried_through_and_nulls_become_none() -> None:
    raw = importance_frame({"a": 2.0, "b": 1.0}, stddev=[0.5, float("nan")], p_value=[0.01, 0.4])
    items = build_feature_importance(raw, run_id=RUN_ID).items
    assert [item.stddev for item in items] == [0.5, None]
    assert [item.p_value for item in items] == [0.01, 0.4]


def test_a_frame_without_the_band_columns_leaves_them_null() -> None:
    items = build_feature_importance(importance_frame({"a": 2.0}), run_id=RUN_ID).items
    assert items[0].stddev is None and items[0].p_value is None


def test_an_empty_importance_frame_is_an_empty_chart() -> None:
    importance = build_feature_importance(importance_frame({}), run_id=RUN_ID)
    assert importance.items == () and importance.top_n == 0
    assert importance.caption == IMPORTANCE_UNAVAILABLE_CAPTION


def test_a_model_that_cannot_be_loaded_gives_an_empty_chart_not_a_failed_run(tmp_path, config) -> None:
    # DEC-068: a run that produced a real model is never discarded because an explanation failed.
    importance = global_importance(
        "runs/r_1/model",
        make_frame(),
        config,
        run_id=RUN_ID,
        storage=LocalStorage(tmp_path / "data"),
    )
    assert importance == unavailable_importance(RUN_ID)


def test_importance_needs_the_label_column_and_says_so_rather_than_guessing(monkeypatch, config) -> None:
    # D13: AutoGluon shuffles a feature and re-scores, which needs labels. Without them there is
    # no honest number to report, so the chart is empty rather than computed on something else.
    import engine.stages.explain as explain

    monkeypatch.setattr(explain, "load_scorer", lambda key, storage: FakeScorer(predictor=object()))
    importance = explain.global_importance(
        "runs/r_1/model", make_frame(), config, run_id=RUN_ID, storage=object()
    )
    assert importance.items == ()
    assert importance.caption == IMPORTANCE_UNAVAILABLE_CAPTION


def test_importance_passes_the_hold_out_with_its_labels_to_the_chosen_model(monkeypatch, config) -> None:
    import engine.stages.explain as explain

    seen: dict[str, Any] = {}

    class RecordingPredictor:
        model_best = "RandomForest"

        def feature_importance(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
            seen["data"] = data
            seen["kwargs"] = kwargs
            return importance_frame({"visits_last_7d": 3.0, "spend_last_30d": 1.0})

    frame = make_frame()
    frame[TARGET] = (logistic(frame) > 0.5).astype(int)
    monkeypatch.setattr(
        explain, "load_scorer", lambda key, storage: FakeScorer(predictor=RecordingPredictor())
    )
    importance = explain.global_importance("runs/r_1/model", frame, config, run_id=RUN_ID, storage=object())

    assert TARGET in seen["data"].columns, "the label column must reach AutoGluon"
    assert set(seen["data"].columns) == {*FEATURES, TARGET}
    assert seen["kwargs"]["model"] == "RandomForest"
    assert seen["kwargs"]["silent"] is True
    assert seen["kwargs"]["subsample_size"] == len(frame)
    assert 30.0 <= seen["kwargs"]["time_limit"] <= 300.0
    assert [item.feature for item in importance.items] == ["visits_last_7d", "spend_last_30d"]


# ---------------------------------------------------------------------------
# Reason text and values
# ---------------------------------------------------------------------------
def test_reason_text_for_a_numeric_feature_carries_the_arrow_and_the_bare_value() -> None:
    assert reason_text("visits_last_7d", "12", Direction.UP, numeric=True) == "visits_last_7d ↑ (12)"
    assert reason_text("visits_last_7d", "12", Direction.DOWN, numeric=True) == "visits_last_7d ↓ (12)"


def test_reason_text_for_a_categorical_feature_reads_as_an_equality() -> None:
    assert reason_text("plan_tier", "basic", Direction.UP, numeric=False) == "plan_tier = basic"
    assert reason_text("opted_in", "true", Direction.DOWN, numeric=False) == "opted_in = true"


def test_reason_text_for_a_missing_value_names_the_absence() -> None:
    assert reason_text("last_contacted_at", MISSING_VALUE, Direction.DOWN, numeric=False, missing=True) == (
        "last_contacted_at missing"
    )
    # A category literally spelled "missing" is a value, not an absence.
    assert reason_text("plan_tier", "missing", Direction.UP, numeric=False) == "plan_tier = missing"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (12, "12"),
        (np.int64(12), "12"),
        (12.0, "12"),
        (3.5, "3.5"),
        (0.123456789, "0.1235"),
        (np.float64(2.50), "2.5"),
        (-0.00001, "0"),
        (True, "true"),
        (False, "false"),
        (np.bool_(True), "true"),
        ("basic", "basic"),
        ("  padded  ", "padded"),
        (None, MISSING_VALUE),
        (float("nan"), MISSING_VALUE),
        (pd.NaT, MISSING_VALUE),
        (pd.NA, MISSING_VALUE),
    ],
)
def test_values_are_stringified_the_way_the_contract_stores_them(value, expected) -> None:
    assert format_value(value) == expected


def test_a_boolean_reads_as_an_equality_even_though_it_is_a_number_to_python() -> None:
    reason = reasons_for_row({"opted_in": 0.4}, {"opted_in": True}, limit=1)[0]
    assert reason.value == "true"
    assert reason.text == "opted_in = true"


def test_a_missing_value_reads_as_missing_whatever_the_contribution_was() -> None:
    reason = reasons_for_row({"last_seen": -0.4}, {"last_seen": None}, limit=1)[0]
    assert reason.value == MISSING_VALUE
    assert reason.direction is Direction.DOWN
    assert reason.text == "last_seen missing"


# ---------------------------------------------------------------------------
# Ranking, the zero rule and the reason count
# ---------------------------------------------------------------------------
def test_reasons_are_ranked_by_absolute_contribution_strongest_first() -> None:
    reasons = reasons_for_row(
        {"a": 0.1, "b": -0.9, "c": 0.4},
        {"a": 1, "b": 2, "c": 3},
        limit=3,
    )
    assert [reason.feature for reason in reasons] == ["b", "c", "a"]
    assert [reason.direction for reason in reasons] == [Direction.DOWN, Direction.UP, Direction.UP]


def test_a_tie_is_broken_by_global_importance_and_then_alphabetically() -> None:
    reasons = reasons_for_row(
        {"zebra": 0.5, "apple": 0.5, "ranked": 0.5},
        {"zebra": 1, "apple": 1, "ranked": 1},
        limit=3,
        rank={"ranked": 1},
    )
    assert [reason.feature for reason in reasons] == ["ranked", "apple", "zebra"]


def test_a_zero_contribution_is_not_a_reason() -> None:
    reasons = reasons_for_row({"a": 0.0, "b": 0.3}, {"a": 1, "b": 2}, limit=3)
    assert [reason.feature for reason in reasons] == ["b"]


def test_a_row_the_model_had_no_reason_for_gets_no_reasons_at_all() -> None:
    assert reasons_for_row({"a": 0.0, "b": -0.0}, {"a": 1, "b": 2}, limit=3) == ()


def test_fewer_non_zero_contributions_than_asked_for_are_never_padded() -> None:
    reasons = reasons_for_row({"a": 0.2, "b": 0.0}, {"a": 1, "b": 2}, limit=5)
    assert len(reasons) == 1


def test_the_reason_count_comes_from_the_configuration(config) -> None:
    contributions = {"a": 0.5, "b": 0.4, "c": 0.3, "d": 0.2, "e": 0.1}
    values = dict.fromkeys(contributions, 1)
    for count in (1, 3, 5):
        reasons = reasons_for_row(contributions, values, limit=count)
        assert len(reasons) == count
    assert len(reasons_for_row(contributions, values, limit=config.evaluation.reasons_per_row)) == 3


def test_a_contribution_smaller_than_the_stored_precision_is_not_a_reason() -> None:
    # Rounded to the six decimals the contract stores, this feature did not move the score.
    assert reasons_for_row({"a": 1e-9}, {"a": 1}, limit=3) == ()


# ---------------------------------------------------------------------------
# Rows -> explanations
# ---------------------------------------------------------------------------
def test_generated_columns_are_summed_back_onto_the_column_the_user_uploaded() -> None:
    rows = pd.DataFrame({PRIMARY_KEY: ["a"], "snapshot_date": ["2026-01-01"], "visits_last_7d": [3]})
    contributions = pd.DataFrame(
        {"snapshot_date.year": [0.3], "snapshot_date.month": [0.2], "visits_last_7d": [-0.4]}
    )
    explanations = build_row_explanations(
        rows,
        contributions,
        [0.7],
        features=["snapshot_date", "visits_last_7d"],
        primary_key=PRIMARY_KEY,
        limit=3,
    )
    reasons = explanations[0].reasons
    assert [reason.feature for reason in reasons] == ["snapshot_date", "visits_last_7d"]
    assert reasons[0].contribution == pytest.approx(0.5)
    assert reasons[0].value == "2026-01-01"
    assert reasons[0].text == "snapshot_date = 2026-01-01"


def test_an_explanation_carries_the_key_and_the_score_it_explains() -> None:
    rows = make_frame(3)
    contributions = pd.DataFrame(
        {"visits_last_7d": [0.5, -0.5, 0.25], "plan_tier": [0.1, 0.1, 0.1]}, index=rows.index
    )
    explanations = build_row_explanations(
        rows,
        contributions,
        [0.1234567, 0.5, 0.9],
        features=list(FEATURES),
        primary_key=PRIMARY_KEY,
        limit=2,
    )
    assert [explanation.primary_key for explanation in explanations] == ["c000", "c001", "c002"]
    assert explanations[0].score == 0.123457
    assert all(isinstance(explanation, RowExplanation) for explanation in explanations)
    assert [reason.feature for reason in explanations[0].reasons] == ["visits_last_7d", "plan_tier"]


def test_row_order_is_the_frame_order() -> None:
    rows = make_frame(5).iloc[::-1]
    contributions = pd.DataFrame({"visits_last_7d": np.linspace(0.5, 0.1, 5)}, index=rows.index)
    explanations = build_row_explanations(
        rows, contributions, [0.5] * 5, features=list(FEATURES), primary_key=PRIMARY_KEY, limit=1
    )
    assert [explanation.primary_key for explanation in explanations] == list(rows[PRIMARY_KEY])


# ---------------------------------------------------------------------------
# The fallback chain, each tier forced
# ---------------------------------------------------------------------------
def test_tier_one_explains_with_treeshap_when_a_tree_model_is_there(config) -> None:
    frame = make_frame()
    scorer = FakeScorer(predictor=TreePredictor(frame))
    result = reasons_for(scorer, frame, config, primary_key=PRIMARY_KEY, seed=1)
    assert result.method == "TreeSHAP"
    assert len(result.explanations) == len(frame)
    assert scorer.predictor.loaded == ["RandomForest"], "the linear model is skipped, not loaded"
    assert any(explanation.reasons for explanation in result.explanations)
    for explanation in result.explanations:
        assert len(explanation.reasons) <= config.evaluation.reasons_per_row
        for reason in explanation.reasons:
            assert reason.feature in FEATURES
            assert reason.text


def test_tier_two_takes_over_when_no_tree_model_can_be_explained(config) -> None:
    frame = make_frame()
    result = reasons_for(
        FakeScorer(predictor=LinearOnlyPredictor(frame)), frame, config, primary_key=PRIMARY_KEY, seed=1
    )
    assert result.method == "KernelSHAP"
    assert len(result.explanations) == len(frame)
    assert any(explanation.reasons for explanation in result.explanations)


def test_tier_three_needs_nothing_but_a_scorer(config) -> None:
    frame = make_frame()
    scorer = FakeScorer(predictor=object())
    result = reasons_for(scorer, frame, config, primary_key=PRIMARY_KEY, seed=1)
    assert result.method == "permutation"
    assert len(result.explanations) == len(frame)
    # base + one batched prediction per perturbed feature, and one more for the reported scores.
    assert scorer.calls == [len(frame)] * (len(FEATURES) + 2)


def test_the_permutation_tier_computes_base_minus_replaced(config) -> None:
    frame = make_frame()
    scorer = FakeScorer(predictor=object())
    result = reasons_for(scorer, frame, config, primary_key=PRIMARY_KEY, seed=1)

    # Worked out here, independently: the median visit count replaces the row's own, and the
    # contribution is what the score lost by it.
    replaced = frame.copy()
    replaced["visits_last_7d"] = frame["visits_last_7d"].median()
    expected = (logistic(frame) - logistic(replaced)).round(6)
    found = {
        explanation.primary_key: reason.contribution
        for explanation in result.explanations
        for reason in explanation.reasons
        if reason.feature == "visits_last_7d"
    }
    for position, key in enumerate(frame[PRIMARY_KEY]):
        if key in found:
            assert found[key] == pytest.approx(expected.iloc[position], abs=1e-6)


def test_a_tier_that_raises_falls_through_to_the_next_one(config) -> None:
    class BrokenPredictor(TreePredictor):
        def transform_features(self, data: pd.DataFrame) -> pd.DataFrame:
            raise RuntimeError("no features today")

    frame = make_frame()
    result = reasons_for(
        FakeScorer(predictor=BrokenPredictor(frame)), frame, config, primary_key=PRIMARY_KEY, seed=1
    )
    assert result.method == "permutation"


def test_the_same_run_explains_the_same_rows_the_same_way(config) -> None:
    frame = make_frame()
    for predictor in (TreePredictor(frame), object()):
        first = reasons_for(FakeScorer(predictor=predictor), frame, config, primary_key=PRIMARY_KEY, seed=5)
        second = reasons_for(FakeScorer(predictor=predictor), frame, config, primary_key=PRIMARY_KEY, seed=5)
        assert first == second


def test_an_empty_frame_explains_nothing_rather_than_failing(config) -> None:
    empty = make_frame(0)
    result = reasons_for(FakeScorer(predictor=object()), empty, config, primary_key=PRIMARY_KEY, seed=1)
    assert result.explanations == ()


def test_the_reason_count_is_respected_by_the_tiers(config) -> None:
    frame = make_frame()
    result = reasons_for(
        FakeScorer(predictor=TreePredictor(frame)),
        frame,
        with_reasons(config, 1),
        primary_key=PRIMARY_KEY,
        seed=1,
    )
    assert all(len(explanation.reasons) <= 1 for explanation in result.explanations)


def test_more_rows_than_the_cap_are_sampled_in_frame_order(config) -> None:
    frame = make_frame(40)
    result = reasons_for(
        FakeScorer(predictor=object()), frame, config, primary_key=PRIMARY_KEY, seed=3, max_rows=10
    )
    keys = [explanation.primary_key for explanation in result.explanations]
    assert len(keys) == 10
    assert keys == sorted(keys), "the sample keeps the frame's own row order"
    assert set(keys) <= set(frame[PRIMARY_KEY])


def test_the_kernel_tier_explains_at_most_a_thousand_rows() -> None:
    assert KERNEL_SHAP_MAX_ROWS == 1000
    assert EXPLAIN_MAX_ROWS == 5000


# ---------------------------------------------------------------------------
# How many rows get explained is the caller's choice, not the module's
# ---------------------------------------------------------------------------
def test_a_scoring_shaped_call_puts_a_reason_on_every_row(config) -> None:
    # Plan section 6.3 wants a reason for every scored row, so the score flow asks for every row -
    # a file above the train flow's cap must come back fully explained, not sampled.
    frame = make_frame(EXPLAIN_MAX_ROWS + 200)
    result = reasons_for(
        FakeScorer(predictor=object()),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=2,
        max_rows=None,
    )
    assert len(result.explanations) == len(frame)
    assert [explanation.primary_key for explanation in result.explanations] == list(frame[PRIMARY_KEY])
    assert any(explanation.reasons for explanation in result.explanations)


def test_the_same_frame_is_sampled_when_the_train_budget_is_asked_for(config) -> None:
    frame = make_frame(EXPLAIN_MAX_ROWS + 200)
    result = reasons_for(
        FakeScorer(predictor=object()),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=2,
        max_rows=EXPLAIN_MAX_ROWS,
    )
    assert len(result.explanations) == EXPLAIN_MAX_ROWS


def test_the_train_flows_call_keeps_sampling_by_default(config) -> None:
    # The pipeline's existing call passes no budget, so the default has to stay the train sample.
    import inspect

    assert inspect.signature(reasons_for).parameters["max_rows"].default == EXPLAIN_MAX_ROWS
    assert inspect.signature(reasons_for).parameters["kernel_max_rows"].default == KERNEL_SHAP_MAX_ROWS
    # The stage entry point has no default at all: both flows call it and want opposite answers.
    assert inspect.signature(row_reasons).parameters["max_rows"].default is inspect.Parameter.empty


def test_the_row_budget_is_a_per_call_argument(config) -> None:
    frame = make_frame(40)
    counted = {
        limit: len(
            reasons_for(
                FakeScorer(predictor=object()),
                frame,
                config,
                primary_key=PRIMARY_KEY,
                seed=3,
                max_rows=limit,
            ).explanations
        )
        for limit in (5, 25, 40, 100, None)
    }
    assert counted == {5: 5, 25: 25, 40: 40, 100: 40, None: 40}


def test_the_kernel_tier_declines_rather_than_sample_when_every_row_was_asked_for(config) -> None:
    # KernelSHAP costs about a fifth of a second a row, so it cannot explain a whole scoring file;
    # but it may not quietly explain a tenth of one either. It steps aside and tier 3 covers all.
    frame = make_frame(30)
    result = reasons_for(
        FakeScorer(predictor=LinearOnlyPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        max_rows=None,
        kernel_max_rows=10,
    )
    assert result.method == "permutation"
    assert len(result.explanations) == len(frame)


def test_the_kernel_tier_still_samples_when_the_caller_asked_for_a_sample(config) -> None:
    frame = make_frame(30)
    result = reasons_for(
        FakeScorer(predictor=LinearOnlyPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        max_rows=30,
        kernel_max_rows=10,
    )
    assert result.method == "KernelSHAP"
    assert len(result.explanations) == 10


def test_a_caller_may_lift_the_kernel_cap_and_pay_for_it(config) -> None:
    frame = make_frame(30)
    result = reasons_for(
        FakeScorer(predictor=LinearOnlyPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        max_rows=None,
        kernel_max_rows=None,
    )
    assert result.method == "KernelSHAP"
    assert len(result.explanations) == len(frame)


# ---------------------------------------------------------------------------
# DEC-056: no row leaves this module without a reason
# ---------------------------------------------------------------------------
class FlatPredictor(LinearOnlyPredictor):
    """A predictor whose probability never moves, so KernelSHAP measures every row as zero.

    `FakeScorer.score` is still the real logistic, so the permutation tier - which asks the scorer
    rather than the predictor - can move the same rows this tier cannot. That gap is exactly the
    one DEC-056's per-row retry exists to close, and it is here so the retry is observed.
    """

    def predict_proba(self, data: pd.DataFrame, as_multiclass: bool = True, transform_features: bool = True):
        assert as_multiclass is False and transform_features is False
        return np.full(len(data.index), 0.5, dtype="float64")


@dataclass
class FlatScorer(FakeScorer):
    """A scorer nothing moves: every tier measures zero, so only a general reason is left."""

    def score(self, frame: pd.DataFrame) -> pd.Series:
        self.calls.append(len(frame))
        return pd.Series(np.full(len(frame.index), 0.5), index=frame.index, dtype="float64")


def chart(*features: str) -> FeatureImportance:
    """A global chart naming `features` in the order a general reason should quote them."""
    return build_feature_importance(
        importance_frame({name: float(len(features) - position) for position, name in enumerate(features)}),
        run_id=RUN_ID,
    )


def test_a_row_the_first_tier_cannot_move_is_retried_on_the_next_one(config) -> None:
    frame = make_frame(12)
    result = reasons_for(
        FakeScorer(predictor=FlatPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        max_rows=None,
        kernel_max_rows=None,
    )
    # KernelSHAP answered for every row and moved none of them, so no row carries its reasons and
    # `method` does not name it: the permutation tier retried them all and rescued them all.
    assert len(result.explanations) == len(frame)
    assert {item.method for item in result.explanations} == {"permutation"}
    assert result.method == "permutation"
    assert all(item.reasons for item in result.explanations)
    # Every row ended on the tier `method` names, and each one is a real per-row measurement, so
    # none of them is on a fallback in the sense the Output page reports.
    assert result.fallback_rows == 0


def test_a_row_a_later_tier_never_covered_is_not_re_exported_blank(config, monkeypatch) -> None:
    """The kernel cap is a coverage budget for a run, not a licence to export empty cells.

    Tier 1 answers for all thirty rows and moves none of them. The KernelSHAP cap is ten, so before
    DEC-056's fix the twenty rows it could not have looked at were put straight back carrying tier
    1's empty reason tuple - stamped with tier 1's name, so `fallback_rows` did not even count them.
    """
    frame = make_frame(30)
    monkeypatch.setattr(
        explain_module,
        "_tree_shap",
        lambda scorer, rows, features: explain_module._Contributions(
            rows=rows,
            values=pd.DataFrame(0.0, index=rows.index, columns=list(features)),
            method=explain_module.ReasonMethod.TREE_SHAP,
        ),
    )
    result = reasons_for(
        FlatScorer(predictor=TreePredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        importance=chart("visits_last_7d", "plan_tier"),
        max_rows=30,
        kernel_max_rows=10,  # smaller than the thirty rows tier 1 blanks
    )
    assert len(result.explanations) == 30
    assert all(item.reasons for item in result.explanations), "no row may reach export unexplained"
    assert len({item.primary_key for item in result.explanations}) == 30
    assert result.fallback_rows == 30
    assert "TreeSHAP" not in {str(item.method) for item in result.explanations}


def test_a_lone_pending_row_is_still_rescuable_by_the_permutation_tier(config) -> None:
    """The retry keeps the whole sample as its reference, so one row is not replaced by itself."""
    frame = make_frame(20)
    seen: list[int] = []
    scorer = FakeScorer(predictor=LinearOnlyPredictor(frame))
    result = reasons_for(
        scorer, frame, config, primary_key=PRIMARY_KEY, seed=1, max_rows=None, kernel_max_rows=0
    )
    seen.append(len(result.explanations))
    assert seen == [20]
    # The permutation tier ran over the real logistic scorer, so these are measured, not general.
    assert {item.method for item in result.explanations} == {"permutation"}
    assert all(item.reasons for item in result.explanations)


def test_a_row_no_tier_can_move_keeps_general_reasons_from_the_chart(config) -> None:
    frame = make_frame(8)
    result = reasons_for(
        FlatScorer(predictor=LinearOnlyPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        importance=chart("spend_last_30d", "plan_tier", "visits_last_7d"),
        max_rows=None,
        kernel_max_rows=0,  # tier 2 declines; tier 3 runs and measures nothing
    )
    assert result.method == "permutation"
    assert result.fallback_rows == len(frame)
    assert {item.method for item in result.explanations} == {"general"}
    for item in result.explanations:
        assert item.reasons, "plan §6.3: a reason for every row"
        # The chart's order, top first, cut to reasons_per_row.
        assert [reason.feature for reason in item.reasons] == [
            "spend_last_30d",
            "plan_tier",
            "visits_last_7d",
        ][: config.evaluation.reasons_per_row]


def test_a_general_reason_claims_no_direction_and_says_it_is_general(config) -> None:
    frame = make_frame(4)
    result = reasons_for(
        FlatScorer(predictor=LinearOnlyPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        importance=chart("visits_last_7d", "plan_tier"),
        max_rows=None,
        kernel_max_rows=0,
    )
    first = result.explanations[0].reasons[0]
    assert first.direction is Direction.NONE
    assert first.contribution == 0.0
    assert first.text.endswith(GENERAL_SUFFIX)
    assert UP_ARROW not in first.text and DOWN_ARROW not in first.text
    # The value is the row's own, not the chart's: a general reason still describes this row.
    assert first.value == format_value(frame["visits_last_7d"].iloc[0])


def test_general_reasons_fall_back_to_what_this_run_measured_when_no_chart_is_given(config) -> None:
    frame = make_frame(6)
    result = reasons_for(
        FlatScorer(predictor=LinearOnlyPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        max_rows=None,
        kernel_max_rows=0,
    )
    assert {item.method for item in result.explanations} == {"general"}
    for item in result.explanations:
        assert item.reasons
        assert {reason.feature for reason in item.reasons} <= set(FEATURES)


def test_fallback_rows_is_zero_when_the_primary_tier_explained_everything(config) -> None:
    frame = make_frame(12)
    result = reasons_for(
        FakeScorer(predictor=TreePredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        max_rows=None,
    )
    assert result.method == "TreeSHAP"
    assert result.fallback_rows == 0
    assert {item.method for item in result.explanations} == {"TreeSHAP"}
    assert fallback_note(result) == ""


def test_the_detail_line_names_the_rows_that_needed_a_fallback() -> None:
    reasons = RowReasons(explanations=(), method="TreeSHAP", fallback_rows=7)
    assert fallback_note(reasons) == " · 7 on fallback reasons"


def test_the_method_survives_the_parquet_round_trip(tmp_path, config) -> None:
    frame = make_frame(5)
    result = reasons_for(
        FlatScorer(predictor=LinearOnlyPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        importance=chart("visits_last_7d"),
        max_rows=None,
        kernel_max_rows=0,
    )
    storage = LocalStorage(tmp_path)
    key = write_row_explanations(result.explanations, run_id=RUN_ID, storage=storage)
    assert read_row_explanations(key, storage=storage) == result.explanations


# ---------------------------------------------------------------------------
# The adapter: explanations -> the reason columns export reads
# ---------------------------------------------------------------------------
def explanation(key: str, count: int = 2, *, score: float = 0.5) -> RowExplanation:
    """One explanation whose reason text names its own row, so a wrong join is visible."""
    return RowExplanation(
        primary_key=key,
        score=score,
        reasons=tuple(
            Reason(
                feature=f"feature_{slot}",
                value="12",
                contribution=round(0.5 - slot / 10, 6),
                direction=Direction.UP,
                text=f"slot {slot} of {key}",
            )
            for slot in range(count)
        ),
    )


def test_the_adapter_produces_the_columns_the_csv_header_names(config) -> None:
    frame = make_frame(3)
    columns = reason_columns(
        [explanation(key) for key in frame[PRIMARY_KEY]], frame, config, primary_key=PRIMARY_KEY
    )
    names = reason_column_names(config)
    assert list(columns.columns) == list(names)
    assert len(names) == config.evaluation.reasons_per_row
    assert set(names) <= set(scores_csv_columns(config, PRIMARY_KEY))


def test_the_adapter_joins_on_the_primary_key_and_not_on_row_position(config) -> None:
    frame = make_frame(6)
    explanations = [explanation(key) for key in frame[PRIMARY_KEY]]
    shuffled = frame.iloc[[4, 0, 5, 1, 3, 2]]
    assert list(shuffled[PRIMARY_KEY]) != [item.primary_key for item in explanations]

    columns = reason_columns(explanations, shuffled, config, primary_key=PRIMARY_KEY)

    assert list(columns.index) == list(shuffled.index), "the columns line up with the frame's own rows"
    for position, key in enumerate(shuffled[PRIMARY_KEY]):
        assert columns["reason_1"].iloc[position].text == f"slot 0 of {key}"
        assert columns["reason_2"].iloc[position].text == f"slot 1 of {key}"


def test_a_row_with_fewer_reasons_than_slots_gets_nulls_in_the_rest(config) -> None:
    frame = make_frame(3)
    explanations = [explanation(key, count) for key, count in zip(frame[PRIMARY_KEY], (0, 1, 2), strict=True)]
    columns = reason_columns(explanations, frame, config, primary_key=PRIMARY_KEY)
    assert [cell is None for cell in columns["reason_1"]] == [True, False, False]
    assert [cell is None for cell in columns["reason_2"]] == [True, True, False]
    assert all(cell is None for cell in columns["reason_3"])


def test_reasons_beyond_the_configured_slots_are_left_out(config) -> None:
    frame = make_frame(1)
    narrow = with_reasons(config, 2)
    columns = reason_columns([explanation("c000", 4)], frame, narrow, primary_key=PRIMARY_KEY)
    assert list(columns.columns) == ["reason_1", "reason_2"]
    assert columns["reason_2"].iloc[0].text == "slot 1 of c000"


def test_every_cell_is_a_whole_reason_never_its_text(config) -> None:
    # export refuses a bare string, because a sentence can fill the CSV but never `sample_rows`.
    frame = make_frame(4)
    columns = reason_columns(
        [explanation(key) for key in frame[PRIMARY_KEY]], frame, config, primary_key=PRIMARY_KEY
    )
    cells = [cell for name in columns.columns for cell in columns[name]]
    assert cells, "the adapter produced no cells at all"
    assert all(cell is None or isinstance(cell, Reason) for cell in cells)
    assert not any(isinstance(cell, str) for cell in cells)
    # And the export stage reads exactly these back.
    assert _reason_column(columns, "reason_1") == list(columns["reason_1"])


def test_export_refuses_the_text_of_a_reason_in_place_of_the_reason(config) -> None:
    frame = make_frame(2)
    columns = reason_columns(
        [explanation(key) for key in frame[PRIMARY_KEY]], frame, config, primary_key=PRIMARY_KEY
    )
    flattened = columns.copy()
    flattened["reason_1"] = [cell.text for cell in columns["reason_1"]]
    with pytest.raises(ValueError, match="reason column holds Reason objects"):
        _reason_column(flattened, "reason_1")


def test_a_frame_the_explanations_do_not_cover_is_refused_with_the_fix(config) -> None:
    # This is the scoring file that was explained with the train flow's sample: most of scores.csv
    # would have empty reason cells, so the run says so instead of shipping them.
    frame = make_frame(10)
    sampled = [explanation(key) for key in frame[PRIMARY_KEY][:4]]
    with pytest.raises(ValueError, match="max_rows=None"):
        reason_columns(sampled, frame, config, primary_key=PRIMARY_KEY)


def test_explanations_that_were_never_computed_leave_empty_cells_not_an_error(config) -> None:
    # `evaluation.shap: false`, or a stage that did not run: export documents empty cells for it.
    frame = make_frame(5)
    columns = reason_columns((), frame, config, primary_key=PRIMARY_KEY)
    assert list(columns.columns) == list(reason_column_names(config))
    assert all(cell is None for name in columns.columns for cell in columns[name])


def test_two_explanations_for_one_key_are_refused(config) -> None:
    frame = make_frame(2)
    doubled = [explanation("c000"), explanation("c000"), explanation("c001")]
    with pytest.raises(ValueError, match="one explanation per row"):
        reason_columns(doubled, frame, config, primary_key=PRIMARY_KEY)


def test_a_frame_without_the_primary_key_cannot_be_joined(config) -> None:
    frame = make_frame(2).drop(columns=[PRIMARY_KEY])
    with pytest.raises(ValueError, match=re.escape(f"carries no {PRIMARY_KEY!r} column")):
        reason_columns([explanation("c000")], frame, config, primary_key=PRIMARY_KEY)


def test_a_key_that_is_not_a_string_joins_the_way_the_explanations_stored_it(config) -> None:
    # `RowExplanation.primary_key` is a string; an integer key column must still find its row.
    frame = pd.DataFrame({PRIMARY_KEY: [101, 102], "visits_last_7d": [1.0, 2.0]})
    columns = reason_columns([explanation("102"), explanation("101")], frame, config, primary_key=PRIMARY_KEY)
    assert [cell.text for cell in columns["reason_1"]] == ["slot 0 of 101", "slot 0 of 102"]


def test_the_columns_can_be_attached_to_the_frame_without_disturbing_it(config) -> None:
    frame = make_frame(4)
    before = frame.copy()
    merged = with_reason_columns(
        [explanation(key) for key in frame[PRIMARY_KEY]], frame, config, primary_key=PRIMARY_KEY
    )
    assert list(merged.columns) == [*frame.columns, *reason_column_names(config)]
    assert merged[list(frame.columns)].equals(frame)
    assert list(merged.index) == list(frame.index)
    assert frame.equals(before), "the caller's frame is left alone"
    assert [cell.text for cell in merged["reason_1"]] == [f"slot 0 of {key}" for key in frame[PRIMARY_KEY]]


def test_the_reasons_a_scoring_run_produces_reach_every_row_of_the_frame(config) -> None:
    # The whole path in one: explain every row, join by key, and find a reason on each row.
    frame = make_frame(60)
    result = reasons_for(
        FakeScorer(predictor=TreePredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=4,
        max_rows=None,
    )
    scored = frame.iloc[::-1]  # the scored frame need not be in the order the tier produced
    merged = with_reason_columns(result.explanations, scored, config, primary_key=PRIMARY_KEY)
    explained = {item.primary_key: item for item in result.explanations}
    for position, key in enumerate(merged[PRIMARY_KEY]):
        expected = explained[key].reasons
        for slot, name in enumerate(reason_column_names(config)):
            cell = merged[name].iloc[position]
            assert cell == (expected[slot] if slot < len(expected) else None)


# ---------------------------------------------------------------------------
# The detail line
# ---------------------------------------------------------------------------
def test_the_detail_line_names_the_tier_that_actually_ran() -> None:
    importance = build_feature_importance(importance_frame({"a": 1.0, "b": 2.0}), run_id=RUN_ID)
    reasons = RowReasons(explanations=(), method="TreeSHAP")
    assert explain_detail(importance, reasons) == "top 2 features · TreeSHAP reasons for 0 rows"
    assert explain_detail(importance, None) == "top 2 features · per-row reasons turned off"


# ---------------------------------------------------------------------------
# row_explanations.parquet
# ---------------------------------------------------------------------------
def sample_explanations() -> tuple[RowExplanation, ...]:
    return (
        RowExplanation(
            primary_key="c001",
            score=0.8125,
            reasons=(
                Reason(
                    feature="visits_last_7d",
                    value="12",
                    contribution=0.42,
                    direction=Direction.UP,
                    text="visits_last_7d ↑ (12)",
                ),
                Reason(
                    feature="plan_tier",
                    value="basic",
                    contribution=-0.1,
                    direction=Direction.DOWN,
                    text="plan_tier = basic",
                ),
            ),
        ),
        RowExplanation(primary_key="c002", score=0.01, reasons=()),
    )


def test_the_parquet_file_round_trips_through_the_contract(tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    explanations = sample_explanations()
    key = write_row_explanations(explanations, run_id=RUN_ID, storage=storage)
    assert key == f"runs/{RUN_ID}/row_explanations.parquet"
    assert read_row_explanations(key, storage=storage) == explanations


def test_the_parquet_schema_is_explicit_about_every_column() -> None:
    schema = row_explanation_schema()
    assert schema.names == ["schema_version", "primary_key", "score", "method", "reasons"]
    reason_struct = schema.field("reasons").type.value_type
    assert [field.name for field in reason_struct] == [
        "feature",
        "value",
        "contribution",
        "direction",
        "text",
    ]


def test_an_empty_explanation_set_still_writes_a_readable_file(tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    key = write_row_explanations((), run_id=RUN_ID, storage=storage)
    assert read_row_explanations(key, storage=storage) == ()


def test_the_reasons_the_export_stage_expects_are_the_reasons_this_stage_makes() -> None:
    # The scored frame carries one `Reason` per slot; export reads them back through this helper,
    # so a mismatch here would be a silent integration break rather than a failing test.
    explanations = sample_explanations()
    frame = pd.DataFrame(
        {
            "reason_1": [
                explanation.reasons[0] if explanation.reasons else None for explanation in explanations
            ],
            "reason_2": [
                explanation.reasons[1] if len(explanation.reasons) > 1 else None
                for explanation in explanations
            ],
        }
    )
    assert _reason_column(frame, "reason_1") == [explanations[0].reasons[0], None]
    assert _reason_column(frame, "reason_2") == [explanations[0].reasons[1], None]
    assert _reason_column(frame, "reason_3") == [None, None]


# ---------------------------------------------------------------------------
# The real thing: a model trained by the train stage, explained by this stage
# ---------------------------------------------------------------------------
@dataclass
class Trained:
    """One real training run, shared by the slow tests below."""

    result: Any
    parts: dict[str, pd.DataFrame]
    storage: LocalStorage
    config: Any


@pytest.fixture(scope="module")
def trained(tmp_path_factory) -> Trained:
    from engine.jobs import CancelToken
    from engine.stages.prepare import fit_transforms, prepare_rows, split_dataset
    from engine.stages.train import train
    from tests.fixtures.make_data import GenerationSpec, generate

    base = load_use_case(USE_CASE)
    search = base.model_search.model_copy(
        update={
            "time_limit_minutes": 1,
            "strategy": Strategy.FAST,
            "ensemble": False,
            "tuning_trials": 5,  # DEC-073: a trial is a real fit; 5 is the schema floor
        }
    )
    config = base.model_copy(update={"model_search": search})
    frame = generate(GenerationSpec(USE_CASE, rows=3_000, variant="clean"))
    rows_frame, plan = prepare_rows(frame, config, primary_key=PRIMARY_KEY, target=TARGET)
    parts, _ = split_dataset(rows_frame, config, run_id=RUN_ID, target=TARGET)
    prepared, report = fit_transforms(rows_frame, config, plan, run_id=RUN_ID, fit_index=parts["train"].index)
    split_parts = {name: prepared.loc[part.index] for name, part in parts.items()}
    recipe = recipe_from_config(
        config,
        primary_key=PRIMARY_KEY,
        feature_columns=report.feature_columns,
        seed=seed_from(RUN_ID),
        target=TARGET,
    )
    storage = LocalStorage(tmp_path_factory.mktemp("explain-flow") / "data")
    result = train(
        recipe,
        split_parts,
        config.evaluation,
        run_id=RUN_ID,
        storage=storage,
        cancel=CancelToken(),
    )
    return Trained(result=result, parts=split_parts, storage=storage, config=config)


@pytest.mark.slow
@pytest.mark.integration
def test_a_real_model_gets_a_real_importance_chart(trained) -> None:
    importance = global_importance(
        trained.result.predictor_key,
        trained.parts["test"],
        trained.config,
        run_id=RUN_ID,
        storage=trained.storage,
    )
    assert isinstance(importance, FeatureImportance)
    assert importance.items, "AutoGluon returned no importance for a model it just trained"
    assert importance.caption == IMPORTANCE_CAPTION
    assert len(importance.items) <= TOP_FEATURES
    assert shares_sum(importance) == Decimal("100.0")
    assert [item.rank for item in importance.items] == list(range(1, len(importance.items) + 1))
    assert all(item.feature in trained.result.model.feature_columns for item in importance.items)


@pytest.mark.slow
@pytest.mark.integration
def test_a_real_model_gets_real_per_row_reasons(trained) -> None:
    rows = trained.parts["test"].head(50)
    result = reasons_for(
        trained.result.model,
        rows,
        trained.config,
        primary_key=PRIMARY_KEY,
        seed=seed_from(RUN_ID),
    )
    # A leaderboard with a tree model on it must be explained by tier 1. Falling back would not
    # only be less exact, it would be far slower, so a silent fallback is worth failing over.
    from engine.config import ModelFamily

    families = {entry.family for entry in trained.result.leaderboard.entries}
    trees = {ModelFamily.XGBOOST, ModelFamily.LIGHTGBM, ModelFamily.RANDOM_FOREST, ModelFamily.CATBOOST}
    assert result.method == ("TreeSHAP" if families & trees else "permutation")
    assert len(result.explanations) == len(rows)
    assert [explanation.primary_key for explanation in result.explanations] == [
        str(key) for key in rows[PRIMARY_KEY]
    ]
    assert any(explanation.reasons for explanation in result.explanations)
    for explanation in result.explanations:
        assert len(explanation.reasons) <= trained.config.evaluation.reasons_per_row
        for reason in explanation.reasons:
            assert reason.feature in trained.result.model.feature_columns
            assert reason.text and reason.feature in reason.text
            assert (reason.direction is Direction.UP) == (reason.contribution > 0)

    key = write_row_explanations(result.explanations, run_id=RUN_ID, storage=trained.storage)
    assert read_row_explanations(key, storage=trained.storage) == result.explanations


@pytest.mark.slow
@pytest.mark.integration
def test_the_reasons_of_a_real_model_come_out_the_same_way_twice(trained) -> None:
    rows = trained.parts["test"].head(30)
    first = reasons_for(trained.result.model, rows, trained.config, primary_key=PRIMARY_KEY, seed=11)
    second = reasons_for(trained.result.model, rows, trained.config, primary_key=PRIMARY_KEY, seed=11)
    assert first == second


@pytest.mark.slow
@pytest.mark.integration
def test_the_stage_entry_point_loads_the_model_it_is_pointed_at(trained) -> None:
    rows = trained.parts["test"].head(20)
    explanations = row_reasons(
        trained.result.predictor_key,
        rows,
        trained.config,
        primary_key=PRIMARY_KEY,
        storage=trained.storage,
        max_rows=None,
    )
    assert len(explanations) == 20
    assert [explanation.primary_key for explanation in explanations] == [
        str(key) for key in rows[PRIMARY_KEY]
    ]
    assert all(isinstance(explanation, RowExplanation) for explanation in explanations)


@pytest.mark.parametrize("dtype", ["Int64", "int64"])
def test_an_integer_feature_is_replaced_by_a_whole_number_even_when_its_median_is_a_half(dtype: str) -> None:
    """The permutation fallback swaps a feature for its median; for an integer column that must be an integer.

    The median of an even number of integers can be x.5. A nullable `Int64` column cannot hold it, so
    the explain stage raised `TypeError: Invalid value '17.5' for dtype 'Int64'` - on any real run
    whose model had no tree explainer and whose sample happened to give that median, which is why a
    full test run hit it and the same test alone did not. A plain `int64` column took it silently and
    handed the scorer a tenure of 17.5 months, a value the model never saw in training.
    """
    import pandas as pd

    from engine.stages.explain import _permutation_contributions

    class _TenureScorer:
        def score(self, frame: pd.DataFrame) -> pd.Series:
            assert pd.api.types.is_integer_dtype(frame["tenure"]), "the replacement changed the column's type"
            return frame["tenure"].astype("float64")

    rows = pd.DataFrame({"tenure": pd.array([17, 18], dtype=dtype)})
    result = _permutation_contributions(_TenureScorer(), rows, ["tenure"], {"tenure": 0})
    assert result.values["tenure"].tolist() == [-1.0, 0.0]
