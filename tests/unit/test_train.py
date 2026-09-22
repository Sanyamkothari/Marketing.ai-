"""The train stage: the recipe-to-AutoGluon mapping, the artefacts, and the hold-out invariant.

The fast tests pin the two pure projections against the M3 design's table, check every one of the
AutoGluon 1.6.3 differences the mapping has to absorb, and prove - with `TabularPredictor.fit`
monkeypatched, so nothing is trained - that no test row ever reaches a fit.

The tests marked `slow` run the installed AutoGluon for real on the committed synthetic generator,
with plan section 10's own settings (`strategy: fast`, `time_limit_minutes: 1`). They are the only
place a model is trained, and they carry plan section 10's golden checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from engine.config import (
    Calibration,
    EvaluationConfig,
    FeaturesConfig,
    Imbalance,
    Metric,
    ModelFamily,
    ModelSearchConfig,
    PrepareConfig,
    ProblemType,
    Recipe,
    SplitConfig,
    Strategy,
    ThresholdMode,
    get_catalog,
    load_use_case,
    recipe_from_config,
)
from engine.contracts import MODEL_DIRECTORY, Leaderboard, LeaderboardEntry
from engine.jobs import CancelToken
from engine.stages.evaluate import compare_to_baseline, evaluate
from engine.stages.prepare import fit_transforms, prepare_rows, split_dataset
from engine.stages.scorer import SCORER_FILENAME, TrainError, load_scorer
from engine.stages.train import (
    AG_NAME_TO_FAMILY,
    AG_PROBLEM_TYPE,
    autogluon_fit_kwargs,
    autogluon_predictor_kwargs,
    build_best_model,
    build_leaderboard,
    class_labels,
    engine_score,
    family_for_model_name,
    hyperparameters_summary,
    predictor_key_for,
    resolve_imbalance,
    train,
    train_detail,
)
from engine.storage import LocalStorage, StorageError, validate_key
from engine.utils.ids import seed_from
from engine.utils.time import utc_now
from tests.fixtures.make_data import GenerationSpec, generate

RUN_ID = "r_20260921_0000beef"
USE_CASE = "targeted-advertisement"
TARGET = "converted_30d"
PRIMARY_KEY = "customer_id"
UNIT_TARGET = "converted"
UNIT_FEATURES = ("signal", "noise")


# ---------------------------------------------------------------------------
# Small recipes and frames for the fast tests
# ---------------------------------------------------------------------------
def make_recipe(**search: object) -> Recipe:
    defaults: dict[str, object] = {"metric": Metric.ROC_AUC}
    defaults.update(search)
    return Recipe(
        use_case_id="unit-test",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        target=UNIT_TARGET,
        primary_key="row_id",
        feature_columns=UNIT_FEATURES,
        prepare=PrepareConfig(),
        split=SplitConfig(),
        features=FeaturesConfig(),
        model_search=ModelSearchConfig.model_validate(defaults),
        seed=4242,
    )


def make_frame(rows: int, *, start: int = 0, seed: int = 1, positive_rate: float = 0.3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=rows)
    outcome = (rng.random(rows) < positive_rate).astype(int)
    return pd.DataFrame(
        {
            "row_id": [f"r{index}" for index in range(start, start + rows)],
            "signal": signal,
            "noise": rng.normal(size=rows),
            UNIT_TARGET: outcome,
        },
        index=pd.RangeIndex(start, start + rows),
    )


def make_parts() -> dict[str, pd.DataFrame]:
    return {
        "train": make_frame(120, start=0, seed=1),
        "validation": make_frame(60, start=1000, seed=2),
        "test": make_frame(60, start=2000, seed=3),
    }


# ---------------------------------------------------------------------------
# D1, D2, D3, D9, D10: the fit keyword arguments
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("strategy", "preset"),
    [
        (Strategy.FAST, "medium_quality"),
        (Strategy.BALANCED, "good_quality"),
        (Strategy.EXHAUSTIVE, "best_quality"),
    ],
)
@pytest.mark.parametrize("ensemble", [True, False])
@pytest.mark.parametrize("imbalance", list(Imbalance))
def test_fit_kwargs_match_the_design_table(strategy, preset, ensemble, imbalance) -> None:
    recipe = make_recipe(strategy=strategy, ensemble=ensemble, folds=5, imbalance=imbalance)
    kwargs = autogluon_fit_kwargs(recipe)
    assert kwargs["presets"] == preset  # D1
    assert kwargs["hyperparameters"] == {"XGB": {}, "GBM": {}, "RF": {}, "LR": {}}
    assert kwargs["time_limit"] == recipe.model_search.time_limit_minutes * 60
    assert kwargs["num_bag_folds"] == (5 if ensemble else 0)
    assert kwargs["num_stack_levels"] == 0
    assert kwargs["fit_weighted_ensemble"] is ensemble
    assert kwargs["use_bag_holdout"] is ensemble  # D2: required to accept an explicit tuning set
    assert kwargs["auto_stack"] is False  # D1: neutralises good_/best_quality
    assert kwargs["dynamic_stacking"] is False  # D1
    assert kwargs["refit_full"] is False  # D3
    assert kwargs["set_best_to_refit_full"] is False  # D3
    assert kwargs["calibrate"] is False  # D9: the engine owns calibration
    assert kwargs["calibrate_decision_threshold"] is False  # D9: the engine owns the threshold
    assert kwargs["raise_on_no_models_fitted"] is True
    assert kwargs["ag_args_fit"] == {"random_seed": recipe.seed}
    assert kwargs["hyperparameter_tune_kwargs"] == {  # DEC-057
        "num_trials": recipe.model_search.tuning_trials,
        "scheduler": "local",
        "searcher": "auto",
    }
    assert (kwargs["use_bag_holdout"] is True) == (kwargs["num_bag_folds"] > 0)


def test_tuning_trials_is_the_hpo_budget_and_reaches_autogluon() -> None:
    """DEC-057: the Model-search control the user moves is the number of trials that get fitted."""
    for trials in (5, 50, 500):
        recipe = make_recipe(tuning_trials=trials)
        assert autogluon_fit_kwargs(recipe)["hyperparameter_tune_kwargs"]["num_trials"] == trials


def test_the_families_are_passed_without_a_search_space_of_our_own() -> None:
    """An empty dict means AutoGluon's own space for that family; see DEC-057 for why not ours."""
    kwargs = autogluon_fit_kwargs(make_recipe())
    assert kwargs["hyperparameters"] == {"XGB": {}, "GBM": {}, "RF": {}, "LR": {}}
    assert all(space == {} for space in kwargs["hyperparameters"].values())


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("LightGBM", ModelFamily.LIGHTGBM),
        ("LightGBM_BAG_L1", ModelFamily.LIGHTGBM),
        ("XGBoost_BAG_L1_FULL", ModelFamily.XGBOOST),
        # HPO appends the trial outside the bagging decorations; missing it would cost the
        # leaderboard its family column and the explain stage its TreeSHAP tier (DEC-057).
        ("LightGBM_BAG_L1/T1", ModelFamily.LIGHTGBM),
        ("XGBoost/T12", ModelFamily.XGBOOST),
        ("RandomForest_BAG_L1/T3", ModelFamily.RANDOM_FOREST),
    ],
)
def test_a_tuned_models_name_still_resolves_to_its_family(name, family) -> None:
    assert family_for_model_name(name) is family


def test_the_weighted_ensemble_is_still_no_family_however_it_is_decorated() -> None:
    assert family_for_model_name("WeightedEnsemble_L2") is None
    assert family_for_model_name("WeightedEnsemble_L2/T1") is None


def test_the_one_minute_smoke_run_asks_for_sixty_seconds() -> None:
    recipe = make_recipe(strategy=Strategy.FAST, ensemble=False, time_limit_minutes=1)
    kwargs = autogluon_fit_kwargs(recipe)
    assert kwargs["time_limit"] == 60
    assert kwargs["presets"] == "medium_quality"
    assert kwargs["num_bag_folds"] == 0


def test_fit_kwargs_are_pure() -> None:
    recipe = make_recipe()
    twin = make_recipe()
    assert autogluon_fit_kwargs(recipe) == autogluon_fit_kwargs(twin)
    assert autogluon_fit_kwargs(recipe) == autogluon_fit_kwargs(recipe)


def test_every_preset_and_hyperparameter_key_exists_in_the_installed_autogluon() -> None:
    from autogluon.tabular.configs.presets_configs import tabular_presets_dict
    from autogluon.tabular.registry import ag_model_registry

    catalog = get_catalog()
    for preset in catalog.strategy_presets.values():
        assert preset in tabular_presets_dict
    for spec in catalog.model_families.values():
        assert spec.autogluon_key in ag_model_registry.keys


def test_the_reverse_name_map_matches_the_installed_registry() -> None:
    from autogluon.tabular.registry import ag_model_registry

    catalog = get_catalog()
    expected = {
        ag_model_registry.key_to_cls(spec.autogluon_key).ag_name: family
        for family, spec in catalog.model_families.items()
    }
    assert expected == AG_NAME_TO_FAMILY  # D4: `LR` fits a model called LinearModel
    assert set(AG_NAME_TO_FAMILY.values()) == set(ModelFamily)


def test_an_unavailable_family_is_dropped_and_an_empty_candidate_list_is_refused(caplog) -> None:
    pool = (ModelFamily.LIGHTGBM, ModelFamily.NEURAL_NET)
    recipe = make_recipe(candidate_pool=pool, candidates=pool)
    with caplog.at_level("WARNING"):
        kwargs = autogluon_fit_kwargs(recipe)
    assert kwargs["hyperparameters"] == {"GBM": {}}  # D10: NN_TORCH omitted, torch is absent
    assert "NeuralNet" in caplog.text

    only_torch = (ModelFamily.NEURAL_NET,)
    with pytest.raises(TrainError) as raised:
        autogluon_fit_kwargs(make_recipe(candidate_pool=only_torch, candidates=only_torch))
    assert raised.value.code == "TRAIN_NO_MODEL_FAMILY"
    assert "NeuralNet" in raised.value.message


# ---------------------------------------------------------------------------
# D6, D7: the predictor constructor
# ---------------------------------------------------------------------------
def test_predictor_kwargs_speak_autogluons_vocabulary() -> None:
    recipe = make_recipe()
    kwargs = autogluon_predictor_kwargs(recipe, path="/tmp/model", positive_class=1)
    assert kwargs["problem_type"] == "binary"  # D6: `binary_classification` is rejected
    assert AG_PROBLEM_TYPE[ProblemType.REGRESSION] == "regression"
    assert kwargs["label"] == UNIT_TARGET
    assert kwargs["eval_metric"] == "roc_auc"
    assert kwargs["path"] == "/tmp/model"
    assert kwargs["verbosity"] == 1
    assert kwargs["positive_class"] == 1
    assert kwargs["sample_weight"] is None
    # `groups` would make AutoGluon build its own validation split; we always pass tuning_data.
    assert "groups" not in kwargs


def test_class_weights_are_a_constructor_argument_not_a_fit_argument() -> None:
    recipe = make_recipe(imbalance=Imbalance.CLASS_WEIGHTS)
    kwargs = autogluon_predictor_kwargs(recipe, path="/tmp/model", class_weights=True)
    assert kwargs["sample_weight"] == "balance_weight"  # D7
    assert "sample_weight" not in autogluon_fit_kwargs(recipe)
    assert "class_weights" not in autogluon_fit_kwargs(recipe)


def test_a_regression_recipe_names_no_positive_class() -> None:
    recipe = Recipe(
        use_case_id="unit-test",
        problem_type=ProblemType.REGRESSION,
        target=UNIT_TARGET,
        primary_key="row_id",
        feature_columns=UNIT_FEATURES,
        prepare=PrepareConfig(),
        split=SplitConfig(),
        features=FeaturesConfig(),
        model_search=ModelSearchConfig(metric=Metric.RMSE, metric_choices=(Metric.RMSE,)),
        seed=1,
    )
    kwargs = autogluon_predictor_kwargs(recipe, path="/tmp/model", positive_class=1)
    assert kwargs["problem_type"] == "regression"
    assert kwargs["eval_metric"] == "root_mean_squared_error"
    assert "positive_class" not in kwargs


# ---------------------------------------------------------------------------
# D5, D8, D16: the small pure helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("LightGBM", ModelFamily.LIGHTGBM),
        ("LightGBM_BAG_L1", ModelFamily.LIGHTGBM),
        ("XGBoost_BAG_L1_FULL", ModelFamily.XGBOOST),
        ("LinearModel", ModelFamily.LOGISTIC_REGRESSION),
        ("RandomForest_BAG_L2", ModelFamily.RANDOM_FOREST),
        ("CatBoost", ModelFamily.CATBOOST),
        ("WeightedEnsemble_L2", None),
        ("SomethingElse", None),
    ],
)
def test_family_for_model_name_strips_the_decorations(name, expected) -> None:
    assert family_for_model_name(name) is expected  # D5


def test_engine_score_flips_only_the_error_metrics() -> None:
    catalog = get_catalog()
    for metric in (Metric.RMSE, Metric.MAE):
        greater_is_better = catalog.metrics[metric].greater_is_better
        assert greater_is_better is False
        # D8: AutoGluon reports RMSE 0.5067 as -0.506669 because every 1.6.3 scorer is "greater is
        # better". Nothing downstream should ever see that sign.
        assert engine_score(-0.506669, greater_is_better=greater_is_better) == pytest.approx(0.506669)
    assert engine_score(0.84, greater_is_better=True) == 0.84


def test_the_predictor_key_has_no_trailing_slash() -> None:
    key = predictor_key_for(RUN_ID)
    assert key == f"runs/{RUN_ID}/model"
    assert validate_key(key) == key
    # D16: MODEL_DIRECTORY is the plan section 7 checklist name, not a usable storage key.
    assert MODEL_DIRECTORY == "model/"
    with pytest.raises(StorageError):
        validate_key(f"runs/{RUN_ID}/{MODEL_DIRECTORY}")


@pytest.mark.parametrize(
    ("hyperparameters", "expected"),
    [
        ({"max_depth": 6, "learning_rate": 0.05, "n_estimators": 400}, "depth 6 · lr 0.05 · 400 trees"),
        ({"num_leaves": 31, "learning_rate": 0.1}, "31 leaves · lr 0.1"),
        ({"C": 1, "solver": "lbfgs"}, "C 1"),
        ({"iterations": 500.0}, "500 trees"),
        ({"solver": "lbfgs", "penalty": "L2"}, "default hyperparameters"),
        ({}, "default hyperparameters"),
    ],
)
def test_the_hyperparameter_summary_invents_no_number(hyperparameters, expected) -> None:
    assert hyperparameters_summary(hyperparameters) == expected


def test_an_ensemble_summarises_itself_by_its_membership() -> None:
    assert hyperparameters_summary({"max_depth": 3}, ensemble_members=4) == ("weighted ensemble of 4 models")


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ([1, 1, 0, 0, 0, 0], (0, 1)),
        ([True, False, False, False], (False, True)),
        (["yes", "no", "no", "no"], ("no", "yes")),
        (["churned", "stayed", "stayed", "stayed"], ("stayed", "churned")),
    ],
)
def test_the_positive_class_follows_the_prepare_conventions(labels, expected) -> None:
    assert class_labels(pd.Series(labels)) == expected


def test_a_target_that_is_not_binary_is_refused() -> None:
    with pytest.raises(TrainError) as raised:
        class_labels(pd.Series([1, 2, 3]))
    assert raised.value.code == "TRAIN_TARGET_NOT_BINARY"


@pytest.mark.parametrize(
    ("configured", "rate", "expected"),
    [
        (Imbalance.AUTO, 0.04, Imbalance.CLASS_WEIGHTS),
        (Imbalance.AUTO, 0.30, Imbalance.NONE),
        (Imbalance.NONE, 0.01, Imbalance.NONE),
        (Imbalance.CLASS_WEIGHTS, 0.50, Imbalance.CLASS_WEIGHTS),
        (Imbalance.OVERSAMPLING, 0.02, Imbalance.OVERSAMPLING),
    ],
)
def test_auto_imbalance_follows_the_training_positive_rate(configured, rate, expected) -> None:
    assert resolve_imbalance(make_recipe(imbalance=configured), positive_rate=rate) is expected


def test_regression_ignores_the_imbalance_setting() -> None:
    recipe = Recipe(
        use_case_id="unit-test",
        problem_type=ProblemType.REGRESSION,
        target=UNIT_TARGET,
        primary_key="row_id",
        feature_columns=UNIT_FEATURES,
        prepare=PrepareConfig(),
        split=SplitConfig(),
        features=FeaturesConfig(),
        model_search=ModelSearchConfig(
            metric=Metric.RMSE, metric_choices=(Metric.RMSE,), imbalance=Imbalance.CLASS_WEIGHTS
        ),
        seed=1,
    )
    assert resolve_imbalance(recipe, positive_rate=None) is Imbalance.NONE


# ---------------------------------------------------------------------------
# D3, D12: the leaderboard, built from AutoGluon's own frame
# ---------------------------------------------------------------------------
def raw_leaderboard() -> pd.DataFrame:
    """The columns `predictor.leaderboard(test_df)` returns, in AutoGluon's own orientation. (D12.)"""
    return pd.DataFrame(
        {
            "model": ["LightGBM", "XGBoost", "LinearModel", "XGBoost_FULL"],
            "score_test": [0.81, 0.86, 0.70, 0.88],
            "score_val": [0.84, 0.83, 0.71, math.nan],
            "eval_metric": ["roc_auc"] * 4,
            "pred_time_val": [0.10, 0.20, 0.05, 0.3],
            "fit_time": [1.5, 2.5, 0.4, 4.0],
            "stack_level": [1, 1, 1, 1],
            "can_infer": [True, True, True, True],
            "fit_order": [1, 2, 3, 4],
        }
    )


def test_the_leaderboard_ranks_on_validation_and_only_reports_the_test_score() -> None:
    leaderboard = build_leaderboard(
        raw_leaderboard(), make_recipe(), run_id=RUN_ID, best_model_name="LightGBM"
    )
    assert [entry.model_name for entry in leaderboard.entries] == [
        "LightGBM",
        "XGBoost",
        "LinearModel",
    ]
    # XGBoost has the better TEST score and still ranks second: the hold-out never ranks anything.
    assert leaderboard.entries[1].test_score > leaderboard.entries[0].test_score
    assert [entry.rank for entry in leaderboard.entries] == [1, 2, 3]
    assert leaderboard.models_trained == 3
    assert leaderboard.best_model_name == "LightGBM"
    assert leaderboard.greater_is_better is True
    assert leaderboard.presets == "good_quality"
    assert leaderboard.entries[0].family is ModelFamily.LIGHTGBM
    assert leaderboard.entries[0].family_label == "LightGBM"
    assert leaderboard.entries[2].family_label == "Logistic Regression"


def test_a_row_without_a_validation_score_is_dropped() -> None:
    leaderboard = build_leaderboard(
        raw_leaderboard(), make_recipe(), run_id=RUN_ID, best_model_name="LightGBM"
    )
    # D3: a `*_FULL` row carries `score_val = NaN`; ranking on a number that does not exist is not
    # something the artefact may do, so the row is left out.
    assert "XGBoost_FULL" not in {entry.model_name for entry in leaderboard.entries}


def test_an_error_metric_leaderboard_is_unflipped_and_sorted_upwards() -> None:
    raw = raw_leaderboard().assign(
        score_val=[-0.50, -0.42, -0.61, math.nan], score_test=[-0.55, -0.44, -0.66, -0.4]
    )
    recipe = Recipe(
        use_case_id="unit-test",
        problem_type=ProblemType.REGRESSION,
        target=UNIT_TARGET,
        primary_key="row_id",
        feature_columns=UNIT_FEATURES,
        prepare=PrepareConfig(),
        split=SplitConfig(),
        features=FeaturesConfig(),
        model_search=ModelSearchConfig(metric=Metric.RMSE, metric_choices=(Metric.RMSE,)),
        seed=1,
    )
    leaderboard = build_leaderboard(raw, recipe, run_id=RUN_ID, best_model_name="XGBoost")
    assert leaderboard.greater_is_better is False
    assert [entry.validation_score for entry in leaderboard.entries] == [0.42, 0.50, 0.61]  # D8
    assert all(entry.validation_score > 0 for entry in leaderboard.entries)


@dataclass
class FakePredictor:
    """Just enough of a `TabularPredictor` for `build_best_model`."""

    info: dict[str, object]

    def model_info(self, name: str) -> dict[str, object]:
        assert name
        return self.info


def test_the_best_model_reads_the_winner_autogluon_chose_on_validation() -> None:
    leaderboard = build_leaderboard(
        raw_leaderboard(), make_recipe(), run_id=RUN_ID, best_model_name="LightGBM"
    )
    predictor = FakePredictor(
        info={
            "hyperparameters": {
                "num_leaves": 31,
                "learning_rate": 0.05,
                "ngram_range": (1, 5),
                "callback": object(),
            }
        }
    )
    best = build_best_model(
        predictor,
        leaderboard,
        make_recipe(),
        run_id=RUN_ID,
        training_rows=800,
        predictor_key=predictor_key_for(RUN_ID),
        trained_at=utc_now(),
    )
    assert best.model_name == "LightGBM"
    assert best.display_name == "LightGBM"
    assert best.is_ensemble is False
    assert best.validation_score == 0.84
    assert best.test_score == 0.81
    # D15: `model_info(...)["hyperparameters"]` is the full dict, tuples and all. Anything JSON
    # cannot carry is rendered as its text rather than dropped.
    assert best.hyperparameters["ngram_range"] == (1, 5)
    assert best.hyperparameters["callback"].startswith("<object object")
    assert best.hyperparameters_summary == "31 leaves · lr 0.05"
    assert best.training_rows == 800
    assert best.feature_count == len(UNIT_FEATURES)
    assert best.predictor_key == f"runs/{RUN_ID}/model"


def test_an_ensemble_names_its_members_families_in_leaderboard_order() -> None:
    raw = raw_leaderboard()
    raw.loc[len(raw)] = ["WeightedEnsemble_L2", 0.89, 0.88, "roc_auc", 0.4, 5.0, 2, True, 5]
    leaderboard = build_leaderboard(raw, make_recipe(), run_id=RUN_ID, best_model_name="WeightedEnsemble_L2")
    predictor = FakePredictor(
        info={
            "hyperparameters": {},
            "stacker_info": {"base_model_names": ["XGBoost", "LightGBM", "LinearModel"]},
        }
    )
    best = build_best_model(
        predictor,
        leaderboard,
        make_recipe(),
        run_id=RUN_ID,
        training_rows=800,
        predictor_key=predictor_key_for(RUN_ID),
        trained_at=utc_now(),
    )
    assert best.is_ensemble is True  # D14
    assert best.ensemble_members == ("XGBoost", "LightGBM", "LinearModel")
    assert best.display_name == "Ensemble (LightGBM + XGBoost + Logistic Regression)"
    assert best.hyperparameters_summary == "weighted ensemble of 3 models"


def test_a_winner_without_a_test_score_is_refused() -> None:
    raw = raw_leaderboard().drop(columns=["score_test"])
    leaderboard = build_leaderboard(raw, make_recipe(), run_id=RUN_ID, best_model_name="LightGBM")
    assert leaderboard.entries[0].test_score is None
    with pytest.raises(TrainError) as raised:
        build_best_model(
            FakePredictor(info={"hyperparameters": {}}),
            leaderboard,
            make_recipe(),
            run_id=RUN_ID,
            training_rows=1,
            predictor_key=predictor_key_for(RUN_ID),
            trained_at=utc_now(),
        )
    assert raised.value.code == "TRAIN_NO_TEST_SCORE"


def test_the_detail_line_reports_what_actually_happened() -> None:
    leaderboard = build_leaderboard(
        raw_leaderboard(), make_recipe(), run_id=RUN_ID, best_model_name="LightGBM"
    )
    best = build_best_model(
        FakePredictor(info={"hyperparameters": {}}),
        leaderboard,
        make_recipe(),
        run_id=RUN_ID,
        training_rows=10,
        predictor_key=predictor_key_for(RUN_ID),
        trained_at=utc_now(),
    )
    assert train_detail(leaderboard, best, make_recipe(ensemble=True, folds=5)) == (
        "3 models trained · 5-fold CV · best: LightGBM"
    )
    assert train_detail(leaderboard, best, make_recipe(ensemble=False)) == (
        "3 models trained · held-out validation · best: LightGBM"
    )


# ---------------------------------------------------------------------------
# THE TEST SPLIT IS FINAL-DECISION-ONLY
# ---------------------------------------------------------------------------
class FitInterruptedError(Exception):
    """Raised by the monkeypatched `fit` once it has recorded what it was given."""


def test_the_test_split_is_never_handed_to_a_fit(tmp_path, monkeypatch) -> None:
    from autogluon.tabular import TabularPredictor

    seen: dict[str, pd.DataFrame] = {}

    def fake_fit(self, train_data, tuning_data=None, **kwargs):
        seen["train"] = train_data
        seen["tuning"] = tuning_data
        seen["kwargs"] = kwargs
        raise FitInterruptedError

    monkeypatch.setattr(TabularPredictor, "fit", fake_fit)
    parts = make_parts()
    with pytest.raises(FitInterruptedError):
        train(
            make_recipe(ensemble=False),
            parts,
            EvaluationConfig(),
            run_id=RUN_ID,
            storage=LocalStorage(tmp_path / "data"),
            cancel=CancelToken(),
        )
    test_index = parts["test"].index
    assert len(seen["train"].index.intersection(test_index)) == 0
    assert seen["tuning"] is not None, "the validation split must be passed as tuning_data"
    assert seen["tuning"].index.equals(parts["validation"].index)
    assert len(seen["tuning"].index.intersection(test_index)) == 0
    # and the fit was asked for exactly the arguments the projection promises
    assert seen["kwargs"]["calibrate"] is False
    assert seen["kwargs"]["calibrate_decision_threshold"] is False


def test_a_caller_that_passes_the_test_frame_for_training_is_refused(tmp_path) -> None:
    parts = make_parts()
    leaked = {**parts, "train": pd.concat([parts["train"], parts["test"].iloc[:5]])}
    with pytest.raises(TrainError) as raised:
        train(
            make_recipe(ensemble=False),
            leaked,
            EvaluationConfig(),
            run_id=RUN_ID,
            storage=LocalStorage(tmp_path / "data"),
            cancel=CancelToken(),
        )
    assert raised.value.code == "TEST_SPLIT_LEAKED_INTO_FIT"

    leaked_tuning = {**parts, "validation": parts["test"]}
    with pytest.raises(TrainError) as raised:
        train(
            make_recipe(ensemble=False),
            leaked_tuning,
            EvaluationConfig(),
            run_id=RUN_ID,
            storage=LocalStorage(tmp_path / "data"),
            cancel=CancelToken(),
        )
    assert raised.value.code == "TEST_SPLIT_LEAKED_INTO_FIT"


def test_a_cancelled_run_stops_before_it_fits(tmp_path) -> None:
    from engine.jobs import JobCancelledError

    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(JobCancelledError):
        train(
            make_recipe(ensemble=False),
            make_parts(),
            EvaluationConfig(),
            run_id=RUN_ID,
            storage=LocalStorage(tmp_path / "data"),
            cancel=cancel,
        )


# ---------------------------------------------------------------------------
# The real thing: plan section 10's settings, the installed AutoGluon
# ---------------------------------------------------------------------------
@dataclass
class Fitted:
    """One real training run, shared by the slow tests below."""

    recipe: Recipe
    parts: dict[str, pd.DataFrame]
    storage: LocalStorage
    evaluation: EvaluationConfig
    result: object


def run_training(tmp_path, *, rows: int, ensemble: bool, run_id: str) -> Fitted:
    """Prepare, split and train the synthetic file exactly as the pipeline will."""
    base = load_use_case(USE_CASE)
    search = base.model_search.model_copy(
        update={
            "time_limit_minutes": 1,  # plan section 10, verbatim
            "strategy": Strategy.FAST,
            "ensemble": ensemble,
            "folds": 3,
        }
    )
    config = base.model_copy(update={"model_search": search})
    frame = generate(GenerationSpec(USE_CASE, rows=rows, variant="clean"))
    rows_frame, plan = prepare_rows(frame, config, primary_key=PRIMARY_KEY, target=TARGET)
    parts, _ = split_dataset(rows_frame, config, run_id=run_id, target=TARGET)
    prepared, report = fit_transforms(rows_frame, config, plan, run_id=run_id, fit_index=parts["train"].index)
    split_parts = {name: prepared.loc[part.index] for name, part in parts.items()}
    recipe = recipe_from_config(
        config,
        primary_key=PRIMARY_KEY,
        feature_columns=report.feature_columns,
        seed=seed_from(run_id),
        target=TARGET,
    )
    storage = LocalStorage(tmp_path / "data")
    result = train(
        recipe,
        split_parts,
        config.evaluation,
        run_id=run_id,
        storage=storage,
        cancel=CancelToken(),
    )
    return Fitted(recipe, split_parts, storage, config.evaluation, result)


@pytest.fixture(scope="module")
def fitted(tmp_path_factory) -> Fitted:
    return run_training(tmp_path_factory.mktemp("train-flow"), rows=10_000, ensemble=False, run_id=RUN_ID)


@pytest.mark.slow
def test_a_real_fit_produces_a_leaderboard_and_a_best_model(fitted) -> None:
    leaderboard = fitted.result.leaderboard
    assert isinstance(leaderboard, Leaderboard)
    assert leaderboard.entries, "the search finished without a single model"
    assert leaderboard.models_trained == len(leaderboard.entries)
    assert [entry.rank for entry in leaderboard.entries] == list(range(1, len(leaderboard.entries) + 1))
    scores = [entry.validation_score for entry in leaderboard.entries]
    assert scores == sorted(scores, reverse=True)
    for entry in leaderboard.entries:
        assert isinstance(entry, LeaderboardEntry)
        assert entry.fit_time_seconds > 0.0
        assert entry.test_score is not None
        assert 0.0 <= entry.validation_score <= 1.0
        assert (entry.family is None) == entry.is_ensemble

    best = fitted.result.best
    assert best.model_name == leaderboard.best_model_name
    assert best.display_name
    assert best.training_rows == len(fitted.parts["train"])
    assert best.feature_count == len(fitted.recipe.feature_columns)
    assert best.predictor_key == f"runs/{RUN_ID}/model"
    assert best.hyperparameters, "AutoGluon reports the winner's full hyperparameter dict"
    assert fitted.result.detail.startswith(f"{leaderboard.models_trained} models trained · ")


@pytest.mark.slow
def test_the_saved_model_round_trips_through_storage_and_scores_identically(fitted) -> None:
    key = fitted.result.predictor_key
    assert fitted.storage.exists(f"{key}/{SCORER_FILENAME}")
    assert (fitted.storage.local_path(key) / "predictor.pkl").is_file()

    reloaded = load_scorer(key, fitted.storage)
    assert reloaded.state == fitted.result.model.state
    assert reloaded.recipe_hash == fitted.recipe.recipe_hash
    test_frame = fitted.parts["test"]
    assert np.allclose(
        reloaded.score(test_frame).to_numpy(),
        fitted.result.model.score(test_frame).to_numpy(),
        atol=1e-12,
    )
    assert reloaded.threshold == fitted.result.model.threshold
    ok, reason = reloaded.can_score(test_frame)
    assert (ok, reason) == (True, "")
    ok, reason = reloaded.can_score(test_frame.drop(columns=[fitted.recipe.feature_columns[0]]))
    assert ok is False and fitted.recipe.feature_columns[0] in reason


@pytest.mark.slow
def test_the_threshold_and_the_calibrator_were_fitted_on_validation(fitted) -> None:
    model = fitted.result.model
    assert model.calibration is not None
    assert model.calibration.fitted_on == "validation"
    assert model.calibration.method is Calibration.ISOTONIC
    assert model.threshold_mode is ThresholdMode.AUTO
    assert 0.0 < model.threshold < 1.0
    assert model.threshold_detail.startswith("Auto (maximises F1 on validation): ")

    # The threshold maximises F1 on the VALIDATION split; on the test split some other cut is
    # usually better, which is precisely the number the engine refuses to chase.
    validation = fitted.parts["validation"]
    scores = model.score(validation).to_numpy()
    actual = validation[fitted.recipe.target].to_numpy() == 1
    chosen = f1_at(scores, actual, model.threshold)
    assert chosen >= f1_at(scores, actual, 0.5) - 1e-12


def f1_at(scores: np.ndarray, actual: np.ndarray, threshold: float) -> float:
    predicted = scores >= threshold
    true_positive = int(np.count_nonzero(predicted & actual))
    denominator = int(np.count_nonzero(predicted)) + int(np.count_nonzero(actual))
    return 0.0 if denominator == 0 else 2.0 * true_positive / denominator


@pytest.mark.slow
def test_the_golden_checks_hold_on_the_synthetic_data(fitted) -> None:
    """Plan section 10: "If this fails, the pipeline is broken, not the data.\" """
    test_frame = fitted.parts["test"]
    model_report, confusion, deciles, _ = evaluate(
        fitted.result.model, test_frame, fitted.evaluation, run_id=RUN_ID
    )
    assert fitted.result.baseline is not None, "the baseline is what the model is judged against"
    baseline_report, _, _, _ = evaluate(fitted.result.baseline, test_frame, fitted.evaluation, run_id=RUN_ID)
    comparison = compare_to_baseline(model_report, baseline_report)

    broken = "If this fails, the pipeline is broken, not the data."
    assert model_report.headline_score > 0.7, broken
    assert comparison.model_beats_baseline is True, broken
    roc_auc_row = next(row for row in comparison.rows if row.id is Metric.ROC_AUC)
    assert roc_auc_row.baseline_value is not None
    assert roc_auc_row.model_value > roc_auc_row.baseline_value, broken

    assert confusion is not None
    assert confusion.total == len(test_frame)
    assert len(deciles.bins) == 10
    assert deciles.bins[0].lift is not None and deciles.bins[-1].lift is not None
    assert deciles.bins[0].lift > deciles.bins[-1].lift

    # The measured golden numbers belong in the test log, so a regression is diagnosable.
    print(
        f"golden: roc_auc={roc_auc_row.model_value} baseline={roc_auc_row.baseline_value} "
        f"models_trained={fitted.result.leaderboard.models_trained} "
        f"best={fitted.result.best.display_name}"
    )


@pytest.mark.slow
def test_a_real_fit_never_saw_a_test_row(fitted) -> None:
    # The winner's own record of how many rows it was fitted on can only be the training split.
    predictor = fitted.result.model.predictor
    info = predictor.model_info(fitted.result.best.model_name)
    assert info["num_samples"] <= len(fitted.parts["train"])
    assert info["num_samples"] + len(fitted.parts["test"]) <= len(fitted.parts["train"]) + len(
        fitted.parts["test"]
    )
    # AutoGluon's own validation score exists, so a tuning set was used rather than a re-split.
    assert fitted.result.best.validation_score > 0.0


@pytest.mark.slow
def test_bagging_accepts_an_explicit_tuning_set(tmp_path) -> None:
    # D2: with bagging on and `tuning_data` given, `fit()` raises unless `use_bag_holdout` is set.
    fitted = run_training(tmp_path, rows=4_000, ensemble=True, run_id="r_20260921_0000cafe")
    leaderboard = fitted.result.leaderboard
    assert leaderboard.entries
    assert any("_BAG" in entry.model_name for entry in leaderboard.entries)
    assert all(entry.family is not None or entry.is_ensemble for entry in leaderboard.entries)
    assert fitted.result.best.validation_score > 0.0
    assert "-fold CV" in fitted.result.detail
