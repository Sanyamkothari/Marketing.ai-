"""`engine.uplift.drift`: feature PSI through Phase 1's code, and the treated-share check (M53, DEC-857).

The feature half must be Phase 1's own computation - the same bins and PSI as `drift.json` - so it is
compared with `engine.stages.score.compute_drift` called directly. The treated-share half is an
absolute difference against `uplift.drift_treated_share_tolerance`, and anything it cannot compare is
`not_applicable` with the reason, never a number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.config import Metric, UseCaseConfig, resolve_config
from engine.contracts import DriftBaseline, ModelStatus, ModelVersion
from engine.stages import register
from engine.stages.score import compute_drift
from engine.storage import LocalStorage, run_key
from engine.uplift.config import UpliftBaseModel, UpliftLearner
from engine.uplift.contracts import SegmentThresholds, UpliftDriftReport, UpliftModelCard
from engine.uplift.drift import NO_BASELINE, measure_uplift_drift, treatment_share_drift

USE_CASE = "win-back-campaign"
TRAIN_RUN = "r_20260901_0b000001"
SCORE_RUN = "r_20260902_0b000002"
WHEN = datetime(2026, 9, 1, tzinfo=UTC)


def _config(**uplift: object) -> UseCaseConfig:
    overrides: dict[str, object] = {"problem_type": "uplift"}
    if uplift:
        overrides["uplift"] = dict(uplift)
    return resolve_config(USE_CASE, overrides).config


def _card(propensity: float = 0.5, rows: int = 10_000) -> UpliftModelCard:
    return UpliftModelCard(
        learner=UpliftLearner.X_LEARNER,
        base_model=UpliftBaseModel.LIGHTGBM,
        feature_columns=("age", "plan"),
        categorical_levels={"plan": ("basic", "plus")},
        treatment_column="treatment",
        outcome_column="reactivated_90d",
        positive_label="1",
        propensity=propensity,
        base_rate=0.1,
        training_rows=rows,
        causal=True,
        segment_thresholds=SegmentThresholds(
            persuadable_min_uplift=0.02,
            sleeping_dog_max_uplift=-0.01,
            sure_thing_min_probability=0.1,
            sure_thing_from_base_rate=True,
        ),
        engine_version="test",
        trained_at=WHEN,
    )


def _version(baseline_key: str | None) -> ModelVersion:
    return ModelVersion(
        model_id="m_win-back-campaign_1",
        use_case_id=USE_CASE,
        version=1,
        run_id=TRAIN_RUN,
        created_at=WHEN,
        status=ModelStatus.CANDIDATE,
        metric=Metric.AUUC,
        metric_label="AUUC",
        test_score=0.01,
        validation_score=None,
        model_display_name="X-learner",
        schema_key=run_key(TRAIN_RUN, "schema.json"),
        run_config_key=run_key(TRAIN_RUN, "run_config.json"),
        predictor_key=f"runs/{TRAIN_RUN}/model",
        drift_baseline_key=baseline_key,
        artefact_keys={},
        engine_version="test",
        autogluon_version="not used",
    )


def _frame(
    rows: int, *, seed: int, treated_share: float | None = None, age_shift: float = 0.0
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(rows)],
            "age": rng.normal(40 + age_shift, 10, rows),
            "plan": rng.choice(["basic", "plus"], rows),
        }
    )
    if treated_share is not None:
        frame["treatment"] = (rng.random(rows) < treated_share).astype(int)
    return frame


# ---------------------------------------------------------------------------
# The treated share
# ---------------------------------------------------------------------------
def test_a_file_without_the_treatment_column_is_not_applicable_and_says_why() -> None:
    result = treatment_share_drift(_frame(500, seed=1), _card(), tolerance=0.05)
    assert result.status == "not_applicable"
    assert result.current_treated_share is None and result.absolute_difference is None
    assert result.reason is not None and "'treatment'" in result.reason
    assert result.training_treated_share == 0.5 and result.rows_compared == 0


def test_a_treatment_column_that_is_not_0_or_1_is_not_applicable() -> None:
    frame = _frame(100, seed=1, treated_share=0.5)
    frame.loc[:4, "treatment"] = 7
    result = treatment_share_drift(frame, _card(), tolerance=0.05)
    assert result.status == "not_applicable"
    assert result.reason is not None and "5 value(s)" in result.reason


def test_an_empty_file_is_not_applicable() -> None:
    frame = _frame(0, seed=1, treated_share=0.5)
    assert treatment_share_drift(frame, _card(), tolerance=0.05).status == "not_applicable"


def test_a_share_close_to_training_is_within_tolerance() -> None:
    frame = _frame(4000, seed=2, treated_share=0.5)
    result = treatment_share_drift(frame, _card(0.5), tolerance=0.05)
    assert result.status == "within_tolerance"
    assert result.absolute_difference is not None and result.absolute_difference <= 0.05
    assert result.rows_compared == 4000 and result.p_value is not None


def test_a_share_far_from_training_is_outside_tolerance() -> None:
    frame = _frame(4000, seed=2, treated_share=0.9)
    result = treatment_share_drift(frame, _card(0.5), tolerance=0.05)
    assert result.status == "outside_tolerance"
    assert result.current_treated_share == pytest.approx(0.9, abs=0.02)
    assert result.p_value is not None and result.p_value < 1e-6


def test_the_rule_is_the_absolute_difference_not_the_test() -> None:
    """A 2-point move on a huge file is 'significant' and still within a 5-point tolerance."""
    frame = pd.DataFrame({"treatment": [1] * 52_000 + [0] * 48_000})
    result = treatment_share_drift(frame, _card(0.5, rows=100_000), tolerance=0.05)
    assert result.status == "within_tolerance"
    assert result.p_value is not None and result.p_value < 0.01


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------
@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


def _stored_baseline(storage: LocalStorage, config: UseCaseConfig) -> str:
    train = _frame(5000, seed=3)[["age", "plan"]]
    baseline = register.drift_baseline(
        train, config, run_id=TRAIN_RUN, model_version_id="m_win-back-campaign_1", primary_key="customer_id"
    )
    key = run_key(TRAIN_RUN, register.DRIFT_BASELINE_FILENAME)
    storage.write_model(key, baseline)
    return key


def test_feature_drift_is_phase_1s_psi_on_the_stored_baseline(storage: LocalStorage) -> None:
    config = _config()
    key = _stored_baseline(storage, config)
    scoring = _frame(3000, seed=4, age_shift=15.0)
    report = measure_uplift_drift(
        scoring, config, version=_version(key), card=_card(), run_id=SCORE_RUN, storage=storage
    )
    assert isinstance(report, UpliftDriftReport)
    assert report.features is not None and report.features_reason is None
    expected = compute_drift(storage.read_model(key, DriftBaseline), scoring, config, run_id=SCORE_RUN)
    assert expected is not None
    assert [(f.feature, f.psi) for f in report.features.features] == [
        (f.feature, f.psi) for f in expected.features
    ]
    assert "age" in report.features.drifted_features
    assert report.treatment.status == "not_applicable"
    assert report.summary.startswith(report.features.summary)
    assert report.training_run_id == TRAIN_RUN and report.model_version_id == "m_win-back-campaign_1"


def test_a_model_without_a_baseline_says_feature_drift_was_not_measured(storage: LocalStorage) -> None:
    report = measure_uplift_drift(
        _frame(100, seed=5, treated_share=0.5),
        _config(),
        version=_version(None),
        card=_card(),
        run_id=SCORE_RUN,
        storage=storage,
    )
    assert report.features is None and report.features_reason == NO_BASELINE
    assert report.treatment.status in {"within_tolerance", "outside_tolerance"}
    assert report.summary.startswith("feature drift not measured")


def test_the_tolerance_comes_from_the_configuration(storage: LocalStorage) -> None:
    frame = _frame(4000, seed=6, treated_share=0.6)
    loose = measure_uplift_drift(
        frame,
        _config(drift_treated_share_tolerance=0.2),
        version=_version(None),
        card=_card(0.5),
        run_id=SCORE_RUN,
        storage=storage,
    )
    tight = measure_uplift_drift(
        frame, _config(), version=_version(None), card=_card(0.5), run_id=SCORE_RUN, storage=storage
    )
    assert (loose.treatment.tolerance, loose.treatment.status) == (0.2, "within_tolerance")
    assert (tight.treatment.tolerance, tight.treatment.status) == (0.05, "outside_tolerance")
