"""The predict stage: loading a stored model, replaying its transforms and scoring calibrated.

The fast tests stand a real `AutoGluonScorer` - real `ScorerState`, real calibrator, really written
to and read back from storage - around a stand-in predictor, so everything the stage owns is
exercised for real and only the AutoGluon binary is faked. They prove the three things that would
silently corrupt every customer's score if they broke: the scores are the **calibrated** ones, the
transforms come from the **training** run's recorded parameters rather than from a fresh fit on the
scored file, and the same model and frame always give the same numbers.

The `slow` test at the bottom runs the installed AutoGluon for real: it trains a small model on the
synthetic generator, registers it as the champion, and scores a fresh scoring file through the full
path - registry, `prepare.json`, `scorer.json`, predictor and drift baseline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import (
    Calibration,
    Metric,
    ProblemType,
    Strategy,
    ThresholdMode,
    UseCaseConfig,
    load_use_case,
    recipe_from_config,
    resolve_config,
)
from engine.contracts import (
    CalibrationSummary,
    DriftBaseline,
    DriftStatus,
    DroppedColumn,
    ModelStatus,
    ModelVersion,
    PrepareReport,
    Transform,
)
from engine.jobs import CancelToken
from engine.registry import LocalModelRegistry
from engine.stages.actions import assign_bands
from engine.stages.prepare import fit_transforms, prepare_rows, split_dataset
from engine.stages.register import drift_baseline
from engine.stages.score import (
    PREPARE_REPORT_FILENAME,
    SCORE_ERRORS,
    PredictResult,
    ScoreError,
    compute_drift,
    predict,
    score_error,
)
from engine.stages.score import _running_autogluon_version as running_autogluon_version
from engine.stages.scorer import (
    SCORER_FILENAME,
    AutoGluonScorer,
    CalibratorState,
    ScorerState,
    TrainError,
    apply_calibrator,
    load_scorer,
)
from engine.stages.train import train
from engine.storage import LocalStorage, run_key
from engine.utils.ids import seed_from
from engine.utils.time import utc_now
from tests.fixtures.make_data import GenerationSpec, generate

USE_CASE = "targeted-advertisement"
TARGET = "converted_30d"
PRIMARY_KEY = "customer_id"
SCORE_FIELD = "propensity"

TRAIN_RUN = "r_20260921_0000beef"
SCORE_RUN = "r_20260922_0000cafe"
MODEL_ID = "m_targeted-advertisement_1"

FEATURES: tuple[str, ...] = ("visits_last_7d", "ad_ctr_90d")

# The isotonic calibrator the stored scorer carries. It is deliberately far from the identity, so a
# stage that exported the raw model output instead of the calibrated one could not pass.
KNOTS_X: tuple[float, ...] = (0.0, 0.5, 1.0)
KNOTS_Y: tuple[float, ...] = (0.0, 0.9, 1.0)

# What the training run recorded in prepare.json. Every number here is a TRAINING statistic; no
# scoring file in this module has values anywhere near them.
TRAIN_CLIP_LOWER = 0.0
TRAIN_CLIP_UPPER = 20.0
TRAIN_MEDIAN_CTR = 0.25


# ---------------------------------------------------------------------------
# A predictor stand-in and the stored model around it
# ---------------------------------------------------------------------------
class RecordingPredictor:
    """A fitted `TabularPredictor`'s scoring surface: deterministic, and it keeps what it was given."""

    def __init__(self) -> None:
        self.seen: list[pd.DataFrame] = []

    def predict_proba(self, features: pd.DataFrame, as_multiclass: bool = True) -> np.ndarray:
        assert as_multiclass is False, "the engine asks for the positive-class column only"
        self.seen.append(features.copy())
        return raw_probability(features)

    def predict(self, features: pd.DataFrame) -> np.ndarray:  # pragma: no cover - regression path
        return raw_probability(features)


def raw_probability(features: pd.DataFrame) -> np.ndarray:
    """The stand-in model's raw output: a logistic of the two features, and nothing random."""
    visits = pd.to_numeric(features["visits_last_7d"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    ctr = pd.to_numeric(features["ad_ctr_90d"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    return np.asarray(1.0 / (1.0 + np.exp(-(visits / 10.0 + ctr - 1.0))), dtype=np.float64)


def scorer_state(features: tuple[str, ...] = FEATURES) -> ScorerState:
    """The state a training run would have persisted: threshold and calibrator fitted on validation."""
    return ScorerState(
        kind="autogluon",
        model_name="LightGBM",
        display_name="LightGBM",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        target_column=TARGET,
        feature_columns=features,
        classes=(0, 1),
        primary_metric=Metric.ROC_AUC,
        threshold=0.37,
        threshold_mode=ThresholdMode.AUTO,
        threshold_detail="Auto (maximises F1 on validation): 0.37",
        calibrator=CalibratorState(method=Calibration.ISOTONIC, x_thresholds=KNOTS_X, y_thresholds=KNOTS_Y),
        calibration=CalibrationSummary(method=Calibration.ISOTONIC, brier_before=0.21, brier_after=0.17),
        recipe_hash="0" * 16,
        trained_at=utc_now(),
    )


def store_model(
    storage: LocalStorage, *, run_id: str = TRAIN_RUN, features: tuple[str, ...] = FEATURES
) -> str:
    """Write a stored model - predictor directory plus `scorer.json` - and return its key."""
    predictor_key = run_key(run_id, "model")
    directory = storage.local_path(predictor_key)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "predictor.pkl").write_bytes(b"not a real predictor")
    AutoGluonScorer(RecordingPredictor(), scorer_state(features)).save(storage, predictor_key)
    return predictor_key


def patch_loader(monkeypatch: pytest.MonkeyPatch, predictor: RecordingPredictor) -> None:
    """Rebuild the scorer from the stored `scorer.json`, around the stand-in predictor.

    Only `TabularPredictor.load` is replaced; the state - feature list, threshold, calibrator - is
    the one the test really wrote to storage and is really read back through `ScorerState`.
    """
    import engine.stages.scorer as scorer_module

    def loader(predictor_key: str, storage: LocalStorage) -> AutoGluonScorer:
        path = storage.local_path(predictor_key)
        if not (path / "predictor.pkl").is_file():
            raise TrainError("MODEL_NOT_SAVED", "The saved model could not be found on disk.")
        return AutoGluonScorer.from_json(predictor, storage.read_text(f"{predictor_key}/{SCORER_FILENAME}"))

    monkeypatch.setattr(scorer_module, "load_scorer", loader)


# ---------------------------------------------------------------------------
# prepare.json, the registry record and the frames
# ---------------------------------------------------------------------------
def prepare_report(run_id: str = TRAIN_RUN, features: tuple[str, ...] = FEATURES) -> PrepareReport:
    """A training report whose parameters are unmistakably training-time numbers."""
    return PrepareReport(
        run_id=run_id,
        rows_in=1_000,
        rows_out=1_000,
        columns_in=4,
        columns_out=3,
        feature_columns=features,
        dropped_columns=(DroppedColumn(name="customer_email", reason="pii", detail="email address"),),
        row_removals=(),
        transforms=(
            Transform(
                order=1,
                kind="clip_percentile",
                columns=("visits_last_7d",),
                parameters={"lower": TRAIN_CLIP_LOWER, "upper": TRAIN_CLIP_UPPER, "quantile": 0.99},
            ),
            Transform(
                order=2,
                kind="fill_median",
                columns=("ad_ctr_90d",),
                parameters={"value": TRAIN_MEDIAN_CTR, "value_kind": "number"},
            ),
        ),
        detail="1K rows prepared",
        prepared_at=utc_now(),
    )


def write_prepare_report(storage: LocalStorage, report: PrepareReport) -> None:
    storage.write_model(run_key(report.run_id, PREPARE_REPORT_FILENAME), report)


def register_champion(
    registry: LocalModelRegistry,
    *,
    predictor_key: str,
    model_id: str = MODEL_ID,
    use_case_id: str = USE_CASE,
    run_id: str = TRAIN_RUN,
    version_number: int = 1,
    drift_baseline_key: str | None = None,
    autogluon_version: str = "1.6.3",
    promote: bool = True,
) -> ModelVersion:
    """Register a model version the way the register stage does, and promote it to champion."""
    version = ModelVersion(
        model_id=model_id,
        use_case_id=use_case_id,
        version=version_number,
        run_id=run_id,
        created_at=utc_now(),
        status=ModelStatus.CANDIDATE,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.83,
        validation_score=0.84,
        model_display_name="LightGBM",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=predictor_key,
        drift_baseline_key=drift_baseline_key,
        engine_version="0.1.0",
        autogluon_version=autogluon_version,
    )
    registry.register(version)
    if not promote:
        return version
    return registry.promote(model_id, by="test", note="fixture champion")


def scoring_frame(rows: int = 3) -> pd.DataFrame:
    """A file to score whose statistics are nothing like the training file's.

    `visits_last_7d` is two orders of magnitude above the recorded clip, and the column that was
    median-filled at training time is mostly null here - so its own median (0.9) is nowhere near
    the recorded training median (0.25). Anything refitted on this frame is visible immediately.
    """
    return pd.DataFrame(
        {
            PRIMARY_KEY: [f"C-{index}" for index in range(rows)],
            "visits_last_7d": [1_000.0, 2_000.0, 5.0][:rows],
            "ad_ctr_90d": [float("nan"), 0.9, float("nan")][:rows],
            "customer_email": ["a@example.com", "b@example.com", "c@example.com"][:rows],
        }
    )


@pytest.fixture
def config(config_root: Path) -> UseCaseConfig:
    """The shipped use case: score field `propensity`, drift threshold 0.20."""
    return resolve_config(USE_CASE, root=config_root).config


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


@pytest.fixture
def registry(tmp_path: Path) -> LocalModelRegistry:
    return LocalModelRegistry(tmp_path / "registry.db")


@pytest.fixture
def predictor() -> RecordingPredictor:
    return RecordingPredictor()


@pytest.fixture
def champion(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    predictor: RecordingPredictor,
    monkeypatch: pytest.MonkeyPatch,
) -> ModelVersion:
    """A stored, registered champion with its training run's `prepare.json` on disk."""
    predictor_key = store_model(storage)
    write_prepare_report(storage, prepare_report())
    patch_loader(monkeypatch, predictor)
    return register_champion(registry, predictor_key=predictor_key)


def score(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    *,
    model_version_id: str | None = None,
) -> PredictResult:
    return predict(
        frame,
        config,
        run_id=SCORE_RUN,
        storage=storage,
        registry=registry,
        model_version_id=model_version_id,
    )


# ---------------------------------------------------------------------------
# Loading the model
# ---------------------------------------------------------------------------
def test_the_champion_scores_the_file_when_no_version_was_chosen(
    champion: ModelVersion, config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    result = score(scoring_frame(), config, storage, registry)

    assert result.model_version.model_id == champion.model_id
    assert result.model_version.status is ModelStatus.CHAMPION
    assert list(result.scores.index) == list(scoring_frame().index)
    assert result.scores.name == SCORE_FIELD
    assert str(result.scores.dtype) == "float64"


def test_a_named_version_is_used_instead_of_the_champion(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    config: UseCaseConfig,
    predictor: RecordingPredictor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_prepare_report(storage, prepare_report())
    patch_loader(monkeypatch, predictor)
    register_champion(registry, predictor_key=store_model(storage))
    chosen = register_champion(
        registry,
        predictor_key=store_model(storage, run_id="r_20260920_0000feed"),
        model_id="m_targeted-advertisement_2",
        run_id="r_20260920_0000feed",
        version_number=2,
        promote=False,
    )
    write_prepare_report(storage, prepare_report(run_id="r_20260920_0000feed"))

    result = score(scoring_frame(), config, storage, registry, model_version_id=chosen.model_id)

    assert result.model_version.model_id == chosen.model_id
    assert result.model_version.status is ModelStatus.CANDIDATE, "an explicit choice is honoured"


def test_a_use_case_with_no_champion_is_refused(
    config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    with pytest.raises(ScoreError) as raised:
        score(scoring_frame(), config, storage, registry)

    assert raised.value.code == "CHAMPION_NOT_FOUND"
    assert config.name in raised.value.message


def test_an_unknown_model_version_is_refused(
    champion: ModelVersion, config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    with pytest.raises(ScoreError) as raised:
        score(scoring_frame(), config, storage, registry, model_version_id="m_does_not_exist")

    assert raised.value.code == "MODEL_NOT_FOUND"
    assert "m_does_not_exist" in raised.value.message


def test_a_model_trained_for_another_use_case_is_refused(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    config: UseCaseConfig,
    predictor: RecordingPredictor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_prepare_report(storage, prepare_report())
    patch_loader(monkeypatch, predictor)
    other = register_champion(
        registry,
        predictor_key=store_model(storage),
        model_id="m_payment-propensity_1",
        use_case_id="payment-propensity",
        promote=False,
    )

    with pytest.raises(ScoreError) as raised:
        score(scoring_frame(), config, storage, registry, model_version_id=other.model_id)

    assert raised.value.code == "MODEL_USE_CASE_MISMATCH"
    assert "payment-propensity" in raised.value.message


def test_a_champion_without_a_stored_predictor_is_refused(
    storage: LocalStorage, registry: LocalModelRegistry, config: UseCaseConfig
) -> None:
    """No monkeypatch here: the real `load_scorer` refuses before AutoGluon is even imported."""
    write_prepare_report(storage, prepare_report())
    register_champion(registry, predictor_key=run_key(TRAIN_RUN, "model"))

    with pytest.raises(ScoreError) as raised:
        score(scoring_frame(), config, storage, registry)

    assert raised.value.code == "MODEL_NOT_SAVED"
    assert MODEL_ID in raised.value.message


def test_a_stored_predictor_without_its_scorer_is_refused(
    storage: LocalStorage, registry: LocalModelRegistry, config: UseCaseConfig
) -> None:
    """A predictor without its threshold and calibration cannot reproduce approved numbers."""
    predictor_key = run_key(TRAIN_RUN, "model")
    directory = storage.local_path(predictor_key)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "predictor.pkl").write_bytes(b"not a real predictor")
    write_prepare_report(storage, prepare_report())
    register_champion(registry, predictor_key=predictor_key)

    with pytest.raises(ScoreError) as raised:
        score(scoring_frame(), config, storage, registry)

    assert raised.value.code == "SCORER_NOT_SAVED"


def test_a_training_run_without_a_prepare_report_is_refused(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    config: UseCaseConfig,
    predictor: RecordingPredictor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_loader(monkeypatch, predictor)
    register_champion(registry, predictor_key=store_model(storage))

    with pytest.raises(ScoreError) as raised:
        score(scoring_frame(), config, storage, registry)

    assert raised.value.code == "PREPARE_REPORT_NOT_SAVED"
    assert predictor.seen == [], "nothing was scored"


def test_a_predictor_that_will_not_load_is_a_business_error_naming_both_versions(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    config: UseCaseConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An AutoGluon version mismatch is a coded refusal, not an exception nobody catches.

    `load_scorer` raises `TrainError` only for the two cases it checks itself. AutoGluon's own
    `require_version_match=True` raises something else entirely, and so does a half-copied
    predictor directory; the stage used to let those through uncoded, so the run reported
    "training failed unexpectedly" instead of naming the versions that do not match.
    """
    import engine.stages.scorer as scorer_module

    mismatch = AssertionError("predictor was fit with AutoGluon 0.9.9, current version is 1.6.3")

    def refuse(predictor_key: str, storage_: LocalStorage) -> AutoGluonScorer:
        raise mismatch

    monkeypatch.setattr(scorer_module, "load_scorer", refuse)
    write_prepare_report(storage, prepare_report())
    register_champion(registry, predictor_key=store_model(storage), autogluon_version="0.9.9")

    with pytest.raises(ScoreError) as raised:
        score(scoring_frame(), config, storage, registry)

    assert raised.value.code == "SCORER_UNREADABLE"
    assert raised.value.__cause__ is mismatch, "the original failure is chained, not swallowed"
    assert "0.9.9" in raised.value.message, "the version the model was stored with"
    assert running_autogluon_version() in raised.value.message, "the version this engine runs"
    assert MODEL_ID in raised.value.message
    assert raised.value.suggestion, "a coded error carries its suggestion (plan §13.4)"


def test_every_score_error_code_is_registered_with_a_message_and_a_suggestion() -> None:
    """The codes are written down, so the API envelope and the generated docs can name them.

    `SCORE_ERRORS` is the table, in the shape `engine.errors.ENGINE_ERRORS` already uses, and
    `score_error` is the only way this module raises - so a code that is not in the table cannot
    reach a user.
    """
    import engine.stages.score as score_module

    declared = {
        value for name, value in vars(score_module).items() if isinstance(value, str) and name == value
    }
    assert declared == set(SCORE_ERRORS), "every declared code is in the table, and vice versa"
    for code, (message, suggestion) in SCORE_ERRORS.items():
        assert message.strip() and suggestion.strip(), code
        assert code not in message, f"{code} reads as business language, not as its own code"

    error = score_error("CHAMPION_NOT_FOUND", use_case="Targeted advertisement")
    assert (error.code, error.suggestion) == ("CHAMPION_NOT_FOUND", SCORE_ERRORS[error.code][1])
    assert "Targeted advertisement" in error.message


# ---------------------------------------------------------------------------
# Replay: the training parameters, never a fresh fit
# ---------------------------------------------------------------------------
def test_replay_applies_the_recorded_training_parameters_not_the_scored_files_own(
    champion: ModelVersion,
    config: UseCaseConfig,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    predictor: RecordingPredictor,
) -> None:
    """The scored file's statistics are wild; the numbers applied are the training run's."""
    frame = scoring_frame()
    own_median = float(frame["ad_ctr_90d"].median())  # 0.9 - the fresh fit this must not do
    assert own_median != TRAIN_MEDIAN_CTR

    result = score(frame, config, storage, registry)

    seen = predictor.seen[-1]
    assert list(seen["visits_last_7d"]) == [TRAIN_CLIP_UPPER, TRAIN_CLIP_UPPER, 5.0]
    assert list(seen["ad_ctr_90d"]) == [TRAIN_MEDIAN_CTR, 0.9, TRAIN_MEDIAN_CTR]
    assert list(result.prepared["visits_last_7d"]) == [TRAIN_CLIP_UPPER, TRAIN_CLIP_UPPER, 5.0]
    assert list(result.prepared["ad_ctr_90d"]) == [TRAIN_MEDIAN_CTR, 0.9, TRAIN_MEDIAN_CTR]
    assert frame["visits_last_7d"].tolist() == [1_000.0, 2_000.0, 5.0], "the input frame is untouched"


def test_a_column_the_training_run_dropped_is_dropped_again(
    champion: ModelVersion, config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    result = score(scoring_frame(), config, storage, registry)

    assert "customer_email" not in result.prepared.columns


def test_extra_columns_the_model_does_not_know_are_kept_but_not_scored(
    champion: ModelVersion,
    config: UseCaseConfig,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    predictor: RecordingPredictor,
) -> None:
    plain = scoring_frame()
    extended = plain.assign(campaign_code=["A", "B", "C"], marketing_opt_in=[True, False, True])

    with_extras = score(extended, config, storage, registry)
    without = score(plain, config, storage, registry)

    assert list(predictor.seen[-1].columns) == list(FEATURES), "the model sees its own columns only"
    assert "campaign_code" in with_extras.prepared.columns, "the actions stage still needs them"
    assert "marketing_opt_in" in with_extras.prepared.columns
    pd.testing.assert_series_equal(with_extras.scores, without.scores)


def test_a_frame_missing_a_feature_is_refused_by_column_name(
    champion: ModelVersion,
    config: UseCaseConfig,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    predictor: RecordingPredictor,
) -> None:
    with pytest.raises(ScoreError) as raised:
        score(scoring_frame().drop(columns=["ad_ctr_90d"]), config, storage, registry)

    assert raised.value.code == "SCORE_SCHEMA_MISMATCH"
    assert "ad_ctr_90d" in raised.value.message
    assert MODEL_ID in raised.value.message
    assert predictor.seen == [], "a mismatched frame is never scored"


def test_an_empty_frame_scores_no_rows(
    champion: ModelVersion,
    config: UseCaseConfig,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    predictor: RecordingPredictor,
) -> None:
    result = score(scoring_frame().iloc[0:0], config, storage, registry)

    assert len(result.scores) == 0
    assert str(result.scores.dtype) == "float64"
    assert result.scores.name == SCORE_FIELD
    assert predictor.seen == [], "AutoGluon is not asked to score nothing"
    assert result.detail.startswith("0 rows scored · ")


def test_the_scores_keep_the_input_frames_own_index(
    champion: ModelVersion, config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    frame = scoring_frame().set_index(pd.Index([7, 8, 9], name="row"))

    result = score(frame, config, storage, registry)

    assert list(result.scores.index) == [7, 8, 9]
    frame[SCORE_FIELD] = result.scores  # what the pipeline does next
    assert frame[SCORE_FIELD].notna().all()


# ---------------------------------------------------------------------------
# Calibration and determinism
# ---------------------------------------------------------------------------
def test_the_scores_are_the_calibrated_ones(
    champion: ModelVersion,
    config: UseCaseConfig,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    predictor: RecordingPredictor,
) -> None:
    """The exported score is the calibrated probability the bands are configured against."""
    result = score(scoring_frame(), config, storage, registry)

    raw = raw_probability(predictor.seen[-1])
    calibrated = np.interp(raw, KNOTS_X, KNOTS_Y)
    assert np.allclose(result.scores.to_numpy(), calibrated, atol=1e-12)
    assert not np.allclose(raw, calibrated), "the fixture's calibrator is not the identity"
    assert np.allclose(
        result.scores.to_numpy(),
        apply_calibrator(result.scorer.state.calibrator, raw),
        atol=1e-12,
    )
    assert set(assign_bands(result.scores, config)) <= {band.name for band in config.actions.bands}


def test_a_stored_scorer_reproduces_the_scores_it_was_saved_with(
    champion: ModelVersion,
    config: UseCaseConfig,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    predictor: RecordingPredictor,
) -> None:
    """`scorer.json` carries the threshold and the calibrator through a round trip to disk."""
    result = score(scoring_frame(), config, storage, registry)
    reloaded = AutoGluonScorer.from_json(
        predictor, storage.read_text(f"{champion.predictor_key}/{SCORER_FILENAME}")
    )

    expected = scorer_state().model_dump(exclude={"trained_at"})
    assert reloaded.state.model_dump(exclude={"trained_at"}) == expected
    assert reloaded.threshold == 0.37
    assert reloaded.state.calibrator.x_thresholds == KNOTS_X
    assert np.allclose(reloaded.score(result.prepared).to_numpy(), result.scores.to_numpy(), atol=1e-12)
    assert list(reloaded.predicted_positive(result.prepared)) == list(result.scores >= 0.37)


def test_the_same_model_and_frame_give_the_same_scores(
    champion: ModelVersion, config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    first = score(scoring_frame(), config, storage, registry)
    second = score(scoring_frame(), config, storage, registry)

    pd.testing.assert_series_equal(first.scores, second.scores)
    assert first.detail == second.detail
    differing = [
        index for index, (a, b) in enumerate(zip(first.scores, second.scores, strict=True)) if a != b
    ]
    assert differing == []


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------
def training_frame(rows: int = 100) -> pd.DataFrame:
    """The prepared training rows a drift baseline would have been built from."""
    return pd.DataFrame(
        {
            PRIMARY_KEY: [f"T-{index}" for index in range(rows)],
            "visits_last_7d": [float(index % 20) for index in range(rows)],
            "ad_ctr_90d": [index / rows for index in range(rows)],
        }
    )


def store_baseline(storage: LocalStorage, config: UseCaseConfig, *, run_id: str = TRAIN_RUN) -> str:
    baseline = drift_baseline(
        training_frame(), config, run_id=run_id, model_version_id=MODEL_ID, primary_key=PRIMARY_KEY
    )
    key = run_key(run_id, "drift_baseline.json")
    storage.write_model(key, baseline)
    return key


def test_drift_is_measured_against_the_baseline_stored_with_the_model(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    config: UseCaseConfig,
    predictor: RecordingPredictor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_prepare_report(storage, prepare_report())
    patch_loader(monkeypatch, predictor)
    register_champion(
        registry, predictor_key=store_model(storage), drift_baseline_key=store_baseline(storage, config)
    )

    result = score(scoring_frame(), config, storage, registry)

    assert result.drift is not None
    assert result.drift.run_id == SCORE_RUN
    assert result.drift.baseline_run_id == TRAIN_RUN
    assert result.drift.model_version_id == MODEL_ID
    assert result.drift.threshold == config.monitoring.drift_psi_threshold
    assert {drift.feature for drift in result.drift.features} == set(FEATURES)
    assert result.drift.status is DriftStatus.DRIFTED, "every visit was clipped onto one bin"
    assert result.detail.endswith(result.drift.summary)


def test_drift_is_measured_on_the_replayed_frame(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    config: UseCaseConfig,
    predictor: RecordingPredictor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The baseline summarises prepared training rows, so the scored side must be prepared too."""
    write_prepare_report(storage, prepare_report())
    patch_loader(monkeypatch, predictor)
    key = store_baseline(storage, config)
    register_champion(registry, predictor_key=store_model(storage), drift_baseline_key=key)
    baseline = storage.read_model(key, DriftBaseline)

    result = score(scoring_frame(), config, storage, registry)

    replayed = compute_drift(baseline, result.prepared, config, run_id=SCORE_RUN)
    raw = compute_drift(baseline, scoring_frame(), config, run_id=SCORE_RUN)
    assert result.drift is not None and replayed is not None and raw is not None
    assert result.drift.max_psi == replayed.max_psi
    assert replayed.max_psi != raw.max_psi, "the fixture's transforms really move the distribution"


def test_a_file_with_no_rows_reports_drift_not_measured_rather_than_drifted(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    config: UseCaseConfig,
    predictor: RecordingPredictor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to compare is "not measured", the same as no baseline at all.

    A file with no rows puts no mass in any baseline bin, so every bin reads as emptied and PSI
    comes out at its maximum: the old report called an empty upload heavily drifted against a
    baseline it could not possibly have matched. The same baseline still produces a real verdict
    for a file that does have rows, which is what makes this about the rows and not the baseline.
    """
    write_prepare_report(storage, prepare_report())
    patch_loader(monkeypatch, predictor)
    register_champion(
        registry, predictor_key=store_model(storage), drift_baseline_key=store_baseline(storage, config)
    )

    empty = score(scoring_frame().iloc[0:0], config, storage, registry)
    populated = score(scoring_frame(), config, storage, registry)

    assert empty.drift is None
    assert empty.detail == "0 rows scored · LightGBM (v1) · drift not measured"
    assert len(empty.scores) == 0, "an empty file still scores, exactly as it did before"
    assert populated.drift is not None, "the same baseline measures a file that has rows"


def test_the_prepare_report_of_the_training_run_comes_back_with_the_result(
    champion: ModelVersion, config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    """The stage reads `prepare.json` to score at all, so the scoring run can write one of its own.

    It used to be loaded, used and dropped, which left a scoring run with no `prepare.json` and the
    Data page with nothing to render for it. The report that comes back is the *training* run's,
    unchanged - the transforms these rows were really prepared with.
    """
    stored = storage.read_model(run_key(TRAIN_RUN, PREPARE_REPORT_FILENAME), PrepareReport)

    result = score(scoring_frame(), config, storage, registry)

    assert result.prepare_report == stored
    assert result.prepare_report.run_id == TRAIN_RUN == champion.run_id
    assert [transform.kind for transform in result.prepare_report.transforms] == [
        "clip_percentile",
        "fill_median",
    ]
    assert result.prepare_report.transforms[0].parameters["upper"] == TRAIN_CLIP_UPPER
    assert list(result.prepared["visits_last_7d"]) == [TRAIN_CLIP_UPPER, TRAIN_CLIP_UPPER, 5.0]


def test_a_model_without_a_stored_baseline_still_scores(
    champion: ModelVersion, config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    result = score(scoring_frame(), config, storage, registry)

    assert champion.drift_baseline_key is None
    assert result.drift is None
    assert result.detail.endswith("drift not measured")
    assert len(result.scores) == 3, "drift is a signal, never a gate on scoring"


def test_a_baseline_key_whose_file_is_gone_still_scores(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    config: UseCaseConfig,
    predictor: RecordingPredictor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_prepare_report(storage, prepare_report())
    patch_loader(monkeypatch, predictor)
    register_champion(
        registry,
        predictor_key=store_model(storage),
        drift_baseline_key=run_key(TRAIN_RUN, "drift_baseline.json"),
    )

    result = score(scoring_frame(), config, storage, registry)

    assert result.drift is None
    assert len(result.scores) == 3


def test_the_detail_line_reports_rows_the_model_and_drift(
    champion: ModelVersion, config: UseCaseConfig, storage: LocalStorage, registry: LocalModelRegistry
) -> None:
    result = score(scoring_frame(), config, storage, registry)

    assert result.detail == "3 rows scored · LightGBM (v1) · drift not measured"


# ---------------------------------------------------------------------------
# The real thing: a trained model, stored, then a fresh file scored through it
# ---------------------------------------------------------------------------
@dataclass
class Scored:
    """One real train-then-score round trip, shared by the slow tests below."""

    config: UseCaseConfig
    storage: LocalStorage
    registry: LocalModelRegistry
    frame: pd.DataFrame
    result: PredictResult


def train_and_register(
    tmp_path: Path, *, rows: int
) -> tuple[UseCaseConfig, LocalStorage, LocalModelRegistry]:
    """Train a small real model on the synthetic file and register it as the champion."""
    base = load_use_case(USE_CASE)
    search = base.model_search.model_copy(
        update={
            "time_limit_minutes": 1,
            "strategy": Strategy.FAST,
            "ensemble": False,
            "folds": 3,
            "tuning_trials": 5,  # DEC-073: a trial is a real fit; 5 is the schema floor
        }
    )
    config = base.model_copy(update={"model_search": search})

    frame = generate(GenerationSpec(USE_CASE, rows=rows, variant="clean"))
    rows_frame, plan = prepare_rows(frame, config, primary_key=PRIMARY_KEY, target=TARGET)
    parts, _ = split_dataset(rows_frame, config, run_id=TRAIN_RUN, target=TARGET)
    prepared, report = fit_transforms(
        rows_frame, config, plan, run_id=TRAIN_RUN, fit_index=parts["train"].index
    )
    split_parts = {name: prepared.loc[part.index] for name, part in parts.items()}
    recipe = recipe_from_config(
        config,
        primary_key=PRIMARY_KEY,
        feature_columns=report.feature_columns,
        seed=seed_from(TRAIN_RUN),
        target=TARGET,
    )
    storage = LocalStorage(tmp_path / "data")
    trained = train(
        recipe, split_parts, config.evaluation, run_id=TRAIN_RUN, storage=storage, cancel=CancelToken()
    )

    write_prepare_report(storage, report)
    baseline = drift_baseline(
        split_parts["train"],
        config,
        run_id=TRAIN_RUN,
        model_version_id=MODEL_ID,
        primary_key=PRIMARY_KEY,
    )
    baseline_key = run_key(TRAIN_RUN, "drift_baseline.json")
    storage.write_model(baseline_key, baseline)

    registry = LocalModelRegistry(tmp_path / "registry.db")
    register_champion(registry, predictor_key=trained.predictor_key, drift_baseline_key=baseline_key)
    return config, storage, registry


@pytest.fixture(scope="module")
def scored(tmp_path_factory: pytest.TempPathFactory) -> Scored:
    config, storage, registry = train_and_register(tmp_path_factory.mktemp("score-flow"), rows=3_000)
    frame = generate(GenerationSpec(USE_CASE, rows=500, seed=99, variant="scoring"))
    result = predict(frame, config, run_id=SCORE_RUN, storage=storage, registry=registry)
    return Scored(config, storage, registry, frame, result)


@pytest.mark.slow
def test_a_real_champion_scores_a_fresh_file_end_to_end(scored: Scored) -> None:
    result = scored.result

    assert len(result.scores) == len(scored.frame), "every uploaded row comes back with a score"
    assert list(result.scores.index) == list(scored.frame.index)
    assert result.scores.name == SCORE_FIELD
    assert result.scores.notna().all()
    assert result.scores.between(0.0, 1.0).all(), "a calibrated probability"
    assert result.model_version.model_id == MODEL_ID
    assert PRIMARY_KEY in result.prepared.columns, "the actions stage needs the key"
    assert result.detail.startswith("500 rows scored · ")


@pytest.mark.slow
def test_the_real_scores_are_the_calibrated_output_of_the_stored_scorer(scored: Scored) -> None:
    """The one number the bands are configured against, taken from the reloaded model."""
    result = scored.result
    reloaded = load_scorer(result.model_version.predictor_key, scored.storage)

    assert np.allclose(reloaded.score(result.prepared).to_numpy(), result.scores.to_numpy(), atol=1e-12)
    raw = reloaded.raw_score(result.prepared).to_numpy()
    assert np.allclose(apply_calibrator(reloaded.state.calibrator, raw), result.scores.to_numpy(), atol=1e-12)
    bands = assign_bands(result.scores, scored.config)
    assert set(bands) <= {band.name for band in scored.config.actions.bands}


@pytest.mark.slow
def test_a_real_scoring_run_is_deterministic_and_reports_drift(scored: Scored) -> None:
    again = predict(
        scored.frame,
        scored.config,
        run_id=SCORE_RUN,
        storage=scored.storage,
        registry=scored.registry,
    )

    pd.testing.assert_series_equal(scored.result.scores, again.scores)
    drift = scored.result.drift
    assert drift is not None
    assert drift.baseline_run_id == TRAIN_RUN and drift.run_id == SCORE_RUN
    assert drift.features, "the baseline named at least one feature"
    assert all(math.isfinite(feature.psi) for feature in drift.features)
    assert drift.status is not DriftStatus.DRIFTED, "the same generator, the same distribution"


@pytest.mark.slow
def test_a_real_scoring_frame_missing_a_feature_is_refused(scored: Scored) -> None:
    missing = scored.result.scorer.feature_columns[0]

    with pytest.raises(ScoreError) as raised:
        predict(
            scored.frame.drop(columns=[missing]),
            scored.config,
            run_id=SCORE_RUN,
            storage=scored.storage,
            registry=scored.registry,
        )

    assert raised.value.code == "SCORE_SCHEMA_MISMATCH"
    assert missing in raised.value.message


def test_predict_result_is_frozen() -> None:
    """The stage hands the pipeline a record, not a mutable scratchpad."""
    fields: dict[str, Any] = PredictResult.__dataclass_fields__
    assert set(fields) == {
        "model_version",
        "scorer",
        "scores",
        "prepared",
        "prepare_report",
        "drift",
        "detail",
    }
    assert PredictResult.__dataclass_params__.frozen is True
