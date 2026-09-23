"""Stage B: the uplift meta-learners recover a planted effect, persist, and explain themselves."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.uplift.config import UpliftBaseModel, UpliftLearner
from engine.uplift.learners import (
    EXPLANATION_EXACT,
    EXPLANATION_SURROGATE,
    MODEL_FILENAME,
    SLearner,
    TLearner,
    UpliftModel,
    XLearner,
    load_model,
    make_learner,
    save_model,
)
from tests.fixtures.make_uplift_data import PLANS, REGIONS, make_uplift_data

NUMERIC = ("age", "tenure_months", "visits_30d", "monthly_spend", "support_tickets_90d", "noise_a")
CATEGORICAL = {"plan": PLANS, "region": REGIONS}
FEATURES = (*NUMERIC, *CATEGORICAL)
SEED = 13


def prepared(frame: pd.DataFrame) -> pd.DataFrame:
    """What Stage E hands the learners: float64 numerics and fixed-category categoricals."""
    columns: dict[str, pd.Series] = {name: frame[name].astype("float64") for name in NUMERIC}
    for name, levels in CATEGORICAL.items():
        columns[name] = pd.Series(pd.Categorical(frame[name], categories=list(levels)), index=frame.index)
    return pd.DataFrame(columns)[list(FEATURES)]


def arrays(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    return frame["treatment"].to_numpy(dtype=int), frame["reactivated_90d"].to_numpy(dtype=int)


@pytest.fixture(scope="module")
def train_data() -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    data = make_uplift_data(20_000, seed=7)
    t, y = arrays(data.frame)
    return prepared(data.frame), t, y


@pytest.fixture(scope="module")
def holdout() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fresh customers the learners never saw, with their ground truth."""
    data = make_uplift_data(8_000, seed=8)
    return prepared(data.frame), data.truth


@pytest.fixture(scope="module")
def fitted(train_data: tuple[pd.DataFrame, np.ndarray, np.ndarray]) -> dict[UpliftLearner, UpliftModel]:
    x, t, y = train_data
    return {
        learner: make_learner(learner, UpliftBaseModel.LIGHTGBM, seed=SEED).fit(x, t, y)
        for learner in UpliftLearner
    }


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    return float(pd.Series(a).corr(pd.Series(b), method="spearman"))


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("learner", "cls"),
    [
        (UpliftLearner.S_LEARNER, SLearner),
        (UpliftLearner.T_LEARNER, TLearner),
        (UpliftLearner.X_LEARNER, XLearner),
    ],
)
def test_make_learner_builds_the_requested_learner(learner: UpliftLearner, cls: type[UpliftModel]) -> None:
    model = make_learner(learner, UpliftBaseModel.LIGHTGBM, seed=1)
    assert type(model) is cls
    assert model.learner is learner
    assert model.base_model is UpliftBaseModel.LIGHTGBM
    assert not model.fitted
    assert np.isnan(model.propensity)


def test_unfitted_model_refuses_to_predict_or_save(tmp_path: Path) -> None:
    model = make_learner(UpliftLearner.T_LEARNER, UpliftBaseModel.LIGHTGBM, seed=1)
    with pytest.raises(RuntimeError, match="not been fitted"):
        model.predict(pd.DataFrame({"a": [1.0]}))
    with pytest.raises(ValueError, match="fitted"):
        save_model(model, tmp_path)


def test_time_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="time_limit_s"):
        make_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, seed=1, time_limit_s=0.0)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def _small() -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    data = make_uplift_data(600, seed=3)
    t, y = arrays(data.frame)
    return prepared(data.frame), t, y


def test_fit_refuses_mismatched_lengths() -> None:
    x, t, y = _small()
    with pytest.raises(ValueError, match="same number of rows"):
        make_learner(UpliftLearner.T_LEARNER, UpliftBaseModel.LIGHTGBM, seed=1).fit(x, t[:-1], y)


def test_fit_refuses_non_binary_treatment() -> None:
    x, t, y = _small()
    bad = t.copy()
    bad[0] = 2
    with pytest.raises(ValueError, match="t must contain only 0 and 1"):
        make_learner(UpliftLearner.T_LEARNER, UpliftBaseModel.LIGHTGBM, seed=1).fit(x, bad, y)


def test_fit_refuses_an_empty_arm() -> None:
    x, _, y = _small()
    with pytest.raises(ValueError, match="control arm is empty"):
        make_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, seed=1).fit(
            x, np.ones(len(x), int), y
        )


def test_fit_refuses_an_arm_whose_outcome_never_varies() -> None:
    x, t, y = _small()
    constant = np.where(t == 1, 0, y)
    with pytest.raises(ValueError, match="never varies in the treated arm"):
        make_learner(UpliftLearner.T_LEARNER, UpliftBaseModel.LIGHTGBM, seed=1).fit(x, t, constant)


def test_predict_names_a_missing_feature(fitted: dict[UpliftLearner, UpliftModel], holdout) -> None:
    x, _ = holdout
    with pytest.raises(KeyError, match="visits_30d"):
        fitted[UpliftLearner.T_LEARNER].predict(x.drop(columns=["visits_30d"]))


# ---------------------------------------------------------------------------
# Planted-effect recovery (LightGBM)
# ---------------------------------------------------------------------------
def test_fit_records_features_and_treated_share(fitted, train_data) -> None:
    _, t, _ = train_data
    for model in fitted.values():
        assert model.fitted
        assert model.feature_columns == FEATURES
        assert model.propensity == pytest.approx(t.mean())


@pytest.mark.parametrize("learner", [UpliftLearner.T_LEARNER, UpliftLearner.X_LEARNER])
def test_ranking_tracks_the_true_uplift(fitted, holdout, learner: UpliftLearner) -> None:
    x, truth = holdout
    prediction = fitted[learner].predict(x)
    assert spearman(prediction.uplift, truth["true_uplift"].to_numpy()) > 0.5


@pytest.mark.parametrize("learner", [UpliftLearner.T_LEARNER, UpliftLearner.X_LEARNER])
def test_segments_get_the_right_sign(fitted, holdout, learner: UpliftLearner) -> None:
    x, truth = holdout
    uplift = fitted[learner].predict(x).uplift
    segment = truth["true_segment"].to_numpy()
    assert uplift[segment == "sleeping_dog"].mean() < 0.0
    assert uplift[segment == "persuadable"].mean() > 0.1


def test_predictions_are_float64_probabilities(fitted, holdout) -> None:
    x, _ = holdout
    for learner, model in fitted.items():
        prediction = model.predict(x)
        for values in (prediction.p_treated, prediction.p_control, prediction.uplift):
            assert values.dtype == np.float64
            assert values.shape == (len(x),)
            assert np.isfinite(values).all()
        for values in (prediction.p_treated, prediction.p_control):
            assert ((values >= 0.0) & (values <= 1.0)).all()
        if learner is not UpliftLearner.X_LEARNER:
            np.testing.assert_allclose(prediction.uplift, prediction.p_treated - prediction.p_control)


def test_control_probability_tracks_the_truth(fitted, holdout) -> None:
    """Segments need p_control to separate sure things from lost causes."""
    x, truth = holdout
    prediction = fitted[UpliftLearner.X_LEARNER].predict(x)
    assert spearman(prediction.p_control, truth["p_control"].to_numpy()) > 0.5


def test_s_learner_runs_and_points_the_right_way(fitted, holdout) -> None:
    x, truth = holdout
    uplift = fitted[UpliftLearner.S_LEARNER].predict(x).uplift
    segment = truth["true_segment"].to_numpy()
    assert uplift[segment == "sleeping_dog"].mean() < 0.0
    assert uplift[segment == "persuadable"].mean() > 0.1


def test_s_learner_treatment_feature_never_clashes_with_a_real_column() -> None:
    x, t, y = _small()
    x = x.assign(__treatment__=0.0)
    model = make_learner(UpliftLearner.S_LEARNER, UpliftBaseModel.LIGHTGBM, seed=1).fit(x, t, y)
    assert isinstance(model, SLearner)
    assert model.treatment_feature not in x.columns
    assert model.predict(x).uplift.shape == (len(x),)


def test_same_seed_same_predictions(train_data, holdout) -> None:
    x, t, y = train_data
    x_new, _ = holdout
    first = make_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, seed=SEED).fit(x, t, y)
    second = make_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, seed=SEED).fit(x, t, y)
    np.testing.assert_array_equal(first.predict(x_new).uplift, second.predict(x_new).uplift)


def test_null_world_has_no_strong_effect() -> None:
    """effect_scale=0: whatever the learner finds is noise, and it must stay small on average."""
    data = make_uplift_data(10_000, seed=21, effect_scale=0.0)
    t, y = arrays(data.frame)
    x = prepared(data.frame)
    model = make_learner(UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, seed=SEED).fit(x, t, y)
    assert abs(float(model.predict(x).uplift.mean())) < 0.02


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("learner", list(UpliftLearner))
def test_save_load_round_trip_is_identical(fitted, holdout, tmp_path: Path, learner: UpliftLearner) -> None:
    x, _ = holdout
    model = fitted[learner]
    save_model(model, tmp_path / "model")
    assert (tmp_path / "model" / MODEL_FILENAME).is_file()
    loaded = load_model(tmp_path / "model")
    assert type(loaded) is type(model)
    assert loaded.feature_columns == model.feature_columns
    assert loaded.propensity == model.propensity
    before, after = model.predict(x), loaded.predict(x)
    np.testing.assert_array_equal(before.uplift, after.uplift)
    np.testing.assert_array_equal(before.p_treated, after.p_treated)
    np.testing.assert_array_equal(before.p_control, after.p_control)


def test_load_refuses_a_pickle_that_is_not_an_uplift_model(tmp_path: Path) -> None:
    import pickle

    (tmp_path / MODEL_FILENAME).write_bytes(pickle.dumps({"not": "a model"}))
    with pytest.raises(TypeError, match="uplift model"):
        load_model(tmp_path)


# ---------------------------------------------------------------------------
# Contributions
# ---------------------------------------------------------------------------
def test_x_learner_contributions_are_exact(fitted, holdout) -> None:
    x, _ = holdout
    model = fitted[UpliftLearner.X_LEARNER]
    matrix, expected = model.contributions(x)
    uplift = model.predict(x).uplift
    assert matrix.shape == (len(x), len(FEATURES))
    np.testing.assert_allclose(matrix.sum(axis=1) + expected, uplift, atol=1e-6)
    assert model.explanation_method == EXPLANATION_EXACT
    assert model.explanation_fidelity == pytest.approx(1.0, abs=1e-9)


def test_x_learner_contributions_single_out_the_planted_drivers(fitted, holdout) -> None:
    x, _ = holdout
    matrix, _ = fitted[UpliftLearner.X_LEARNER].contributions(x)
    importance = dict(zip(FEATURES, np.abs(matrix).mean(axis=0), strict=True))
    top_three = sorted(importance, key=importance.__getitem__, reverse=True)[:3]
    assert "visits_30d" in top_three
    assert importance["noise_a"] < importance["visits_30d"]


@pytest.mark.parametrize("learner", [UpliftLearner.S_LEARNER, UpliftLearner.T_LEARNER])
def test_surrogate_contributions_report_their_fidelity(fitted, holdout, learner: UpliftLearner) -> None:
    x, _ = holdout
    model = fitted[learner]
    matrix, expected = model.contributions(x)
    uplift = model.predict(x).uplift
    assert matrix.shape == (len(x), len(FEATURES))
    assert model.explanation_method == EXPLANATION_SURROGATE
    fidelity = model.explanation_fidelity
    assert fidelity is not None
    # The recorded fidelity IS the R² of the explanation's row sums against the predicted uplift.
    sums = matrix.sum(axis=1) + expected
    r2 = 1.0 - np.sum((uplift - sums) ** 2) / np.sum((uplift - uplift.mean()) ** 2)
    assert fidelity == pytest.approx(r2)
    assert fidelity > 0.8


# ---------------------------------------------------------------------------
# AutoGluon (slow)
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_autogluon_x_learner_is_self_contained_after_save(tmp_path: Path) -> None:
    pytest.importorskip("autogluon.tabular")
    import shutil

    data = make_uplift_data(4_000, seed=5)
    t, y = arrays(data.frame)
    x = prepared(data.frame)
    work = tmp_path / "work"
    model = make_learner(
        UpliftLearner.X_LEARNER, UpliftBaseModel.AUTOGLUON_FAST, seed=1, time_limit_s=40.0, work_dir=work
    ).fit(x, t, y)
    assert {path.name for path in work.iterdir()} == {"mu0", "mu1", "tau0", "tau1"}
    before = model.predict(x.head(500))

    save_model(model, tmp_path / "saved")
    shutil.rmtree(work)  # the saved directory must not depend on the work directory
    moved = tmp_path / "moved"
    shutil.move(str(tmp_path / "saved"), moved)
    loaded = load_model(moved)
    after = loaded.predict(x.head(500))
    np.testing.assert_allclose(before.uplift, after.uplift)
    np.testing.assert_allclose(before.p_control, after.p_control)

    matrix, expected = loaded.contributions(x.head(500))
    assert matrix.shape == (500, len(FEATURES))
    assert loaded.explanation_method == EXPLANATION_SURROGATE
    assert np.isfinite(expected)


@pytest.mark.slow
def test_autogluon_t_learner_saved_in_its_work_dir(tmp_path: Path) -> None:
    pytest.importorskip("autogluon.tabular")
    data = make_uplift_data(3_000, seed=6)
    t, y = arrays(data.frame)
    x = prepared(data.frame)
    model = make_learner(
        UpliftLearner.T_LEARNER, UpliftBaseModel.AUTOGLUON_FAST, seed=1, time_limit_s=20.0, work_dir=tmp_path
    ).fit(x, t, y)
    save_model(model, tmp_path)  # saving into the work directory copies nothing
    loaded = load_model(tmp_path)
    np.testing.assert_allclose(model.predict(x.head(200)).uplift, loaded.predict(x.head(200)).uplift)
