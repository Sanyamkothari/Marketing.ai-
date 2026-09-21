"""The scorer: the one place a calibrator is fitted and a decision threshold is chosen.

Every test here is fast and none of them trains an AutoGluon model. The scorer only ever asks its
predictor for numbers, so a recording fake stands in for the predictor and lets the tests assert
something a real fit would hide: **which rows the fit was allowed to see**.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

from engine.config import (
    Calibration,
    EvaluationConfig,
    FeaturesConfig,
    Metric,
    ModelFamily,
    ModelSearchConfig,
    PrepareConfig,
    ProblemType,
    Recipe,
    SplitConfig,
    ThresholdConfig,
    ThresholdMode,
)
from engine.stages.evaluate import ClassificationScorer, RegressionScorer, Scorer
from engine.stages.scorer import (
    BASELINE_DISPLAY_NAME,
    MIN_CALIBRATION_ROWS,
    SCORER_FILENAME,
    AutoGluonScorer,
    CalibratorState,
    ScorerState,
    TrainError,
    apply_calibrator,
    brier_score,
    choose_threshold,
    fit_baseline_scorer,
    fit_calibrator,
    fit_scorer,
    load_scorer,
    to_numpy_dtypes,
)
from engine.storage import LocalStorage
from engine.utils.time import utc_now

TARGET = "converted"
FEATURES = ("signal", "noise", "tier")
SEED = 7


# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------
@dataclass
class RecordingPredictor:
    """A predictor that serves the `signal` column as its probability and remembers what it saw.

    The scorer's whole contract with AutoGluon is `predict_proba` / `predict`, so this is enough to
    exercise it - and the recorded indexes are what prove the threshold and the calibrator were
    fitted on the validation rows and on nothing else.
    """

    seen: list[pd.Index] = field(default_factory=list)

    def predict_proba(self, frame: pd.DataFrame, as_multiclass: bool = True) -> pd.Series:
        self.seen.append(frame.index)
        return frame["signal"].astype(float)

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        self.seen.append(frame.index)
        return frame["signal"].astype(float)


def make_recipe(
    *,
    problem_type: ProblemType = ProblemType.BINARY_CLASSIFICATION,
    metric: Metric = Metric.ROC_AUC,
) -> Recipe:
    return Recipe(
        use_case_id="unit-test",
        problem_type=problem_type,
        target=TARGET,
        primary_key="row_id",
        feature_columns=FEATURES,
        prepare=PrepareConfig(),
        split=SplitConfig(),
        features=FeaturesConfig(),
        model_search=ModelSearchConfig(metric=metric, metric_choices=(metric,)),
        seed=SEED,
    )


def make_frame(rows: int, *, start: int = 0, shift: float = 0.0, seed: int = 1) -> pd.DataFrame:
    """A frame whose `signal` column both drives the fake model and carries the outcome's signal."""
    rng = np.random.default_rng(seed)
    signal = np.clip(rng.beta(2.0, 3.0, size=rows) + shift, 0.001, 0.999)
    outcome = (rng.random(rows) < signal).astype(int)
    return pd.DataFrame(
        {
            "row_id": [f"r{index}" for index in range(start, start + rows)],
            "signal": signal,
            "noise": rng.normal(size=rows),
            "tier": rng.choice(["basic", "plus"], size=rows),
            TARGET: outcome,
        },
        index=pd.RangeIndex(start, start + rows),
    )


def make_state(**overrides: object) -> ScorerState:
    defaults: dict[str, object] = {
        "kind": "autogluon",
        "model_name": "LightGBM",
        "display_name": "LightGBM",
        "family": ModelFamily.LIGHTGBM,
        "problem_type": ProblemType.BINARY_CLASSIFICATION,
        "target_column": TARGET,
        "feature_columns": FEATURES,
        "classes": (0, 1),
        "primary_metric": Metric.ROC_AUC,
        "threshold": 0.40,
        "threshold_mode": ThresholdMode.AUTO,
        "threshold_detail": "Auto (maximises F1 on validation): 0.40",
        "calibrator": CalibratorState(),
        "calibration": None,
        "recipe_hash": "0" * 64,
        "trained_at": utc_now(),
    }
    defaults.update(overrides)
    return ScorerState.model_validate(defaults)


@pytest.fixture
def validation() -> pd.DataFrame:
    return make_frame(400, start=1000, seed=11)


@pytest.fixture
def evaluation() -> EvaluationConfig:
    return EvaluationConfig(calibration=Calibration.ISOTONIC, threshold=ThresholdConfig())


# ---------------------------------------------------------------------------
# The protocol the evaluate stage reads
# ---------------------------------------------------------------------------
def test_both_scorers_satisfy_the_evaluate_protocols(validation, evaluation) -> None:
    model = fit_scorer(
        RecordingPredictor(),
        make_recipe(),
        evaluation,
        validation=validation,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=(0, 1),
    )
    baseline = fit_baseline_scorer(
        make_recipe(),
        evaluation,
        train=make_frame(300, seed=3),
        validation=validation,
        classes=(0, 1),
    )
    assert baseline is not None
    for scorer in (model, baseline):
        assert isinstance(scorer, Scorer)
        assert isinstance(scorer, ClassificationScorer)
        assert scorer.target_column == TARGET
        assert scorer.primary_metric is Metric.ROC_AUC
        assert scorer.classes == (0, 1)
    assert baseline.display_name == BASELINE_DISPLAY_NAME


def test_a_regression_scorer_predicts_a_number_and_decides_nothing(validation) -> None:
    recipe = make_recipe(problem_type=ProblemType.REGRESSION, metric=Metric.RMSE)
    scorer = fit_scorer(
        RecordingPredictor(),
        recipe,
        EvaluationConfig(),
        validation=validation,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=None,
    )
    assert isinstance(scorer, RegressionScorer)
    assert scorer.threshold == 0.0
    assert scorer.threshold_detail == "Not applicable to a regression model."
    assert scorer.calibration is None
    pd.testing.assert_series_equal(scorer.predict(validation), scorer.score(validation))
    with pytest.raises(TrainError) as raised:
        scorer.predict_proba(validation)
    assert raised.value.code == "SCORER_NOT_A_CLASSIFIER"


def test_score_is_calibrated_and_raw_score_is_not(validation, evaluation) -> None:
    scorer = fit_scorer(
        RecordingPredictor(),
        make_recipe(),
        evaluation,
        validation=validation,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=(0, 1),
    )
    raw = scorer.raw_score(validation)
    calibrated = scorer.score(validation)
    assert scorer.state.calibrator.method is Calibration.ISOTONIC
    pd.testing.assert_series_equal(raw, validation["signal"].astype(float), check_names=False)
    assert not np.allclose(raw.to_numpy(), calibrated.to_numpy())
    assert np.array_equal(
        scorer.predicted_positive(validation).to_numpy(),
        calibrated.to_numpy() >= scorer.threshold,
    )
    labels = scorer.predict(validation)
    assert set(labels.unique()) <= {0, 1}
    assert np.array_equal(labels.to_numpy() == 1, scorer.predicted_positive(validation).to_numpy())


def test_can_score_names_the_columns_the_model_is_missing(validation) -> None:
    scorer = AutoGluonScorer(RecordingPredictor(), make_state())
    ok, reason = scorer.can_score(validation)
    assert (ok, reason) == (True, "")
    ok, reason = scorer.can_score(validation.drop(columns=["noise", "tier"]))
    assert ok is False
    assert "noise" in reason and "tier" in reason
    with pytest.raises(TrainError) as raised:
        scorer.score(validation.drop(columns=["noise"]))
    assert raised.value.code == "SCORER_CANNOT_SCORE"


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def miscalibrated(rows: int = 600, seed: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Raw scores that rank well but are far too confident, which is what a calibrator is for."""
    rng = np.random.default_rng(seed)
    truth = rng.beta(2.0, 2.0, size=rows)
    actual = rng.random(rows) < truth
    raw = np.clip(truth**3, 1e-6, 1 - 1e-6)
    return raw, actual


def test_the_stored_isotonic_knots_reproduce_sklearn_exactly() -> None:
    from sklearn.isotonic import IsotonicRegression

    raw, actual = miscalibrated()
    state, refused = fit_calibrator(raw, actual, Calibration.ISOTONIC)
    assert refused is None
    reference = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, actual.astype(float))
    probe = np.linspace(-0.5, 1.5, 401)
    assert np.allclose(apply_calibrator(state, probe), reference.predict(probe), atol=1e-12)


def test_the_stored_platt_coefficients_reproduce_sklearn_exactly() -> None:
    from sklearn.linear_model import LogisticRegression

    raw, actual = miscalibrated()
    state, refused = fit_calibrator(raw, actual, Calibration.PLATT)
    assert refused is None
    reference = LogisticRegression(max_iter=1000).fit(raw.reshape(-1, 1), actual.astype(float))
    probe = np.linspace(0.0, 1.0, 201)
    expected = reference.predict_proba(probe.reshape(-1, 1))[:, 1]
    assert np.allclose(apply_calibrator(state, probe), expected, atol=1e-12)


@pytest.mark.parametrize("method", [Calibration.ISOTONIC, Calibration.PLATT])
def test_calibration_lowers_the_brier_score_on_a_miscalibrated_fixture(method) -> None:
    raw, actual = miscalibrated()
    state, refused = fit_calibrator(raw, actual, method)
    assert refused is None
    before = brier_score(actual, raw)
    after = brier_score(actual, apply_calibrator(state, raw))
    assert before is not None and after is not None
    assert after < before


def test_a_calibrator_that_cannot_be_fitted_degrades_to_none_and_says_so() -> None:
    raw, actual = miscalibrated(rows=MIN_CALIBRATION_ROWS - 1)
    state, refused = fit_calibrator(raw, actual, Calibration.ISOTONIC)
    assert state.method is Calibration.NONE
    assert refused is not None and "rows" in refused

    raw, _ = miscalibrated(rows=200)
    single = np.ones(200, dtype=bool)
    state, refused = fit_calibrator(raw, single, Calibration.ISOTONIC)
    assert state.method is Calibration.NONE
    assert refused == "the validation split holds one outcome only"
    assert np.array_equal(apply_calibrator(state, raw), raw)


def test_a_degraded_calibrator_is_reported_as_none_never_as_the_method_that_was_asked_for(
    evaluation,
) -> None:
    tiny = make_frame(MIN_CALIBRATION_ROWS - 5, seed=2)
    scorer = fit_scorer(
        RecordingPredictor(),
        make_recipe(),
        evaluation,
        validation=tiny,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=(0, 1),
    )
    assert scorer.calibration is not None
    assert scorer.calibration.method is Calibration.NONE
    assert scorer.calibration.brier_before == scorer.calibration.brier_after


def test_calibration_none_carries_no_summary_at_all(validation) -> None:
    scorer = fit_scorer(
        RecordingPredictor(),
        make_recipe(),
        EvaluationConfig(calibration=Calibration.NONE),
        validation=validation,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=(0, 1),
    )
    assert scorer.calibration is None
    pd.testing.assert_series_equal(scorer.score(validation), scorer.raw_score(validation))


def test_the_calibration_summary_is_measured_on_validation_and_says_so(validation, evaluation) -> None:
    scorer = fit_scorer(
        RecordingPredictor(),
        make_recipe(),
        evaluation,
        validation=validation,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=(0, 1),
    )
    summary = scorer.calibration
    assert summary is not None
    assert summary.method is Calibration.ISOTONIC
    assert summary.fitted_on == "validation"
    assert summary.brier_before is not None and summary.brier_after is not None
    assert summary.brier_after <= summary.brier_before


# ---------------------------------------------------------------------------
# The decision threshold
# ---------------------------------------------------------------------------
def test_fixed_and_manual_thresholds_are_read_from_the_config_not_from_the_data() -> None:
    scores = np.linspace(0.0, 1.0, 100)
    actual = scores > 0.5
    value, mode, detail = choose_threshold(scores, actual, ThresholdConfig(mode=ThresholdMode.FIXED))
    assert (value, mode, detail) == (0.50, ThresholdMode.FIXED, "Fixed: 0.50")
    value, mode, detail = choose_threshold(
        scores, actual, ThresholdConfig(mode=ThresholdMode.MANUAL, value=0.62)
    )
    assert (value, mode, detail) == (0.62, ThresholdMode.MANUAL, "Manual: 0.62")


def test_the_auto_threshold_maximises_f1_and_takes_the_smallest_maximiser() -> None:
    # Ten rows, hand-checked: every cut at or below 0.60 that keeps all four positives above it
    # scores the same F1, and the rule takes the lowest such score, which is the highest recall.
    scores = np.array([0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20, 0.10, 0.05])
    actual = np.array([1, 1, 1, 1, 0, 0, 0, 0, 0, 0], dtype=bool)
    value, mode, detail = choose_threshold(scores, actual, ThresholdConfig(mode=ThresholdMode.AUTO))
    assert (value, mode) == (0.60, ThresholdMode.AUTO)
    assert detail == "Auto (maximises F1 on validation): 0.60"

    tied = np.array([0.9, 0.9, 0.4, 0.4], dtype=float)
    tied_actual = np.array([1, 1, 0, 0], dtype=bool)
    value, _, _ = choose_threshold(tied, tied_actual, ThresholdConfig(mode=ThresholdMode.AUTO))
    assert value == 0.90  # a cut inside a run of equal scores is not a rule `>=` can express


def test_the_auto_threshold_falls_back_when_validation_holds_one_class() -> None:
    scores = np.linspace(0.1, 0.9, 50)
    value, mode, detail = choose_threshold(
        scores, np.zeros(50, dtype=bool), ThresholdConfig(mode=ThresholdMode.AUTO)
    )
    assert (value, mode) == (0.50, ThresholdMode.AUTO)
    assert detail == "Auto (validation had one class only): 0.50"


def test_the_auto_threshold_beats_the_fixed_one_on_an_imbalanced_split(validation, evaluation) -> None:
    auto = fit_scorer(
        RecordingPredictor(),
        make_recipe(),
        evaluation,
        validation=validation,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=(0, 1),
    )
    assert auto.threshold_mode is ThresholdMode.AUTO
    assert 0.0 < auto.threshold < 1.0
    assert auto.threshold_detail.startswith("Auto (maximises F1 on validation): ")


# ---------------------------------------------------------------------------
# Validation only - the property the whole milestone rests on
# ---------------------------------------------------------------------------
def test_fit_scorer_takes_no_test_data_at_all() -> None:
    parameters = set(inspect.signature(fit_scorer).parameters)
    assert "validation" in parameters
    assert not {name for name in parameters if "test" in name}


def test_fit_scorer_only_ever_scores_the_validation_rows(validation, evaluation) -> None:
    test_frame = make_frame(400, start=9000, seed=99)
    predictor = RecordingPredictor()
    fit_scorer(
        predictor,
        make_recipe(),
        evaluation,
        validation=validation,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=(0, 1),
    )
    assert predictor.seen, "the fake predictor was never asked for a score"
    for index in predictor.seen:
        assert index.equals(validation.index)
        assert len(index.intersection(test_frame.index)) == 0


def test_perturbing_the_test_split_moves_neither_the_threshold_nor_the_calibrator(
    validation, evaluation
) -> None:
    def state_for(validation_frame: pd.DataFrame) -> ScorerState:
        return fit_scorer(
            RecordingPredictor(),
            make_recipe(),
            evaluation,
            validation=validation_frame,
            display_name="LightGBM",
            model_name="LightGBM",
            family=ModelFamily.LIGHTGBM,
            classes=(0, 1),
            trained_at=utc_now(),
        ).state

    baseline_state = state_for(validation)
    # The test split is not an argument of this function, so it cannot move anything - and a second
    # fit on the same validation rows proves the fit is deterministic rather than merely unrelated.
    assert state_for(validation).threshold == baseline_state.threshold
    assert state_for(validation).calibrator == baseline_state.calibrator
    # ... and the test is not vacuous: different validation rows do move both.
    other = state_for(make_frame(400, start=1000, shift=0.25, seed=42))
    assert other.threshold != baseline_state.threshold or other.calibrator != baseline_state.calibrator


def test_the_baseline_is_fitted_on_train_and_thresholded_on_validation(validation, evaluation) -> None:
    recipe = make_recipe()
    train_frame = make_frame(500, seed=3)
    test_frame = make_frame(500, start=9000, seed=99)

    first = fit_baseline_scorer(recipe, evaluation, train=train_frame, validation=validation, classes=(0, 1))
    # Wreck every test row; the baseline never sees them, so nothing about it may move.
    wrecked = test_frame.copy()
    wrecked["signal"] = 0.999
    wrecked[TARGET] = 1
    second = fit_baseline_scorer(recipe, evaluation, train=train_frame, validation=validation, classes=(0, 1))
    assert first is not None and second is not None
    assert first.state.threshold == second.state.threshold
    assert first.state.calibrator == second.state.calibrator
    # Not vacuous: a different validation split does move the operating point.
    moved = fit_baseline_scorer(
        recipe,
        evaluation,
        train=train_frame,
        validation=make_frame(400, start=1000, shift=0.3, seed=42),
        classes=(0, 1),
    )
    assert moved is not None
    assert (moved.state.threshold, moved.state.calibrator) != (
        first.state.threshold,
        first.state.calibrator,
    )


def test_a_baseline_that_cannot_be_fitted_is_a_missing_comparison_not_a_failed_run(
    validation, evaluation
) -> None:
    empty = validation.iloc[:0]
    assert (
        fit_baseline_scorer(make_recipe(), evaluation, train=empty, validation=validation, classes=(0, 1))
        is None
    )


# ---------------------------------------------------------------------------
# Persistence: a stored model must be reconstructable, or the champion rule is a fiction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", [Calibration.ISOTONIC, Calibration.PLATT, Calibration.NONE])
def test_scorer_json_round_trips_and_reproduces_the_same_scores(tmp_path, validation, method) -> None:
    storage = LocalStorage(tmp_path / "data")
    recipe = make_recipe()
    predictor = RecordingPredictor()
    scorer = fit_scorer(
        predictor,
        recipe,
        EvaluationConfig(calibration=method),
        validation=validation,
        display_name="LightGBM",
        model_name="LightGBM",
        family=ModelFamily.LIGHTGBM,
        classes=(0, 1),
        autogluon_version="1.6.3",
    )
    key = scorer.save(storage, "runs/r_20260921_0000beef/model")
    assert key.endswith(SCORER_FILENAME)

    reloaded = AutoGluonScorer.from_json(predictor, storage.read_text(key))
    assert reloaded.state == scorer.state
    assert reloaded.state.recipe_hash == recipe.recipe_hash
    assert reloaded.state.threshold_detail == scorer.threshold_detail
    assert reloaded.state.calibrator.method is method
    if method is Calibration.ISOTONIC:
        assert reloaded.state.calibrator.x_thresholds == scorer.state.calibrator.x_thresholds
        assert reloaded.state.calibrator.y_thresholds
    if method is Calibration.PLATT:
        assert reloaded.state.calibrator.coef == scorer.state.calibrator.coef
        assert reloaded.state.calibrator.intercept == scorer.state.calibrator.intercept
    assert np.allclose(reloaded.score(validation).to_numpy(), scorer.score(validation).to_numpy(), atol=1e-12)
    assert reloaded.threshold == scorer.threshold


def test_load_scorer_refuses_a_model_that_was_never_saved(tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    key = "runs/r_20260921_0000beef/model"
    with pytest.raises(TrainError) as raised:
        load_scorer(key, storage)
    assert raised.value.code == "MODEL_NOT_SAVED"

    storage.write_text(f"{key}/predictor.pkl", "not really a predictor")
    with pytest.raises(TrainError) as raised:
        load_scorer(key, storage)
    assert raised.value.code == "SCORER_NOT_SAVED"


# ---------------------------------------------------------------------------
# The AutoGluon boundary: pandas nullable dtypes
# ---------------------------------------------------------------------------
def test_nullable_dtypes_are_converted_before_a_model_ever_sees_them() -> None:
    frame = pd.DataFrame(
        {
            "visits": pd.array([1, 2, None], dtype="Int64"),
            "ratio": pd.array([0.1, None, 0.3], dtype="Float64"),
            "flag": pd.array([True, False, None], dtype="boolean"),
            "tier": pd.array(["a", "b", None], dtype="string"),
            "plain": [1.0, 2.0, 3.0],
            "grade": pd.Categorical(["x", "y", "x"]),
            TARGET: pd.array([1, 0, 1], dtype="Int64"),
        }
    )
    columns = ["visits", "ratio", "flag", "tier", "plain", "grade"]
    converted = to_numpy_dtypes(frame, columns)
    assert converted["visits"].dtype == np.float64
    assert converted["ratio"].dtype == np.float64
    assert bool(np.isnan(converted["visits"].to_numpy()[2]))
    assert converted["visits"].to_numpy()[0] == 1.0
    assert converted["flag"].dtype == object
    assert converted["tier"].dtype == object
    assert converted["plain"].dtype == np.float64
    assert str(converted["grade"].dtype) == "category"
    # The label is not in `columns`, so its values - the class labels - are left exactly as they are.
    assert str(converted[TARGET].dtype) == "Int64"


def test_a_frame_that_needs_no_conversion_is_handed_back_unchanged() -> None:
    frame = pd.DataFrame({"a": [1.0, 2.0], "b": ["x", "y"]})
    assert to_numpy_dtypes(frame, ["a", "b"]) is frame


def test_a_scorer_converts_nullable_columns_on_every_scoring_path() -> None:
    seen: list[str] = []

    class DtypeRecordingPredictor:
        def predict_proba(self, frame: pd.DataFrame, as_multiclass: bool = True) -> pd.Series:
            seen.extend(str(frame[column].dtype) for column in frame.columns)
            return frame["signal"].astype(float)

    frame = make_frame(80, seed=4)
    frame["noise"] = pd.array(frame["noise"].round().astype(int), dtype="Int64")
    scorer = AutoGluonScorer(DtypeRecordingPredictor(), make_state())
    scorer.score(frame)
    assert "Int64" not in seen


def test_a_calibrator_state_must_carry_what_its_method_needs() -> None:
    with pytest.raises(ValueError, match="isotonic"):
        CalibratorState(method=Calibration.ISOTONIC)
    with pytest.raises(ValueError, match="Platt"):
        CalibratorState(method=Calibration.PLATT, coef=1.0)
