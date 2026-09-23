"""`engine.pipeline.uplift_flow_for`: the one lookup that sends a run to the uplift flows (plan B §5).

Its contract is mostly about what it leaves alone. A classification or regression run - and the bare
sentinel the pipeline's own dispatch test drives both entry points with - must come back `None`, so
`Pipeline.run_train` and `run_score` execute exactly the flow they did before Phase 3b. Only an
uplift configuration (train), or a score run whose model is recorded as AUUC, is claimed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from engine.config import Metric, ResolvedConfig, RunMode, resolve_config
from engine.contracts import ModelStatus, ModelVersion
from engine.jobs import CancelToken, NullJobRunner
from engine.pipeline import Pipeline, StageContext, uplift_flow_for
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage
from engine.uplift.flow import UpliftScoreFlow, UpliftTrainFlow
from engine.utils.time import utc_now

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    return Pipeline(
        LocalStorage(tmp_path / "data"), LocalModelRegistry(tmp_path / "registry.db"), NullJobRunner()
    )


def context(
    pipeline: Pipeline, resolved: ResolvedConfig, mode: RunMode, model_version_id: str | None = None
) -> StageContext:
    return StageContext(
        run_id="r_20260923_000000_dispatch",
        mode=mode,
        config=resolved.config,
        resolved=resolved,
        storage=pipeline.storage,
        registry=pipeline.registry,
        cancel=CancelToken(),
        primary_key="customer_id",
        target="reactivated_90d",
        upload_key="uploads/u_1/source.csv",
        model_version_id=model_version_id,
    )


def register(pipeline: Pipeline, *, model_id: str, metric: Metric, status: ModelStatus) -> ModelVersion:
    version = ModelVersion(
        model_id=model_id,
        use_case_id=USE_CASE,
        version=pipeline.registry.next_version(USE_CASE),
        run_id="r_20260901_000000_trained",
        created_at=utc_now(),
        status=status,
        metric=metric,
        metric_label=metric.value,
        test_score=0.1,
        model_display_name="a model",
        schema_key="runs/r/schema.json",
        run_config_key="runs/r/run_config.json",
        predictor_key="runs/r/model",
        engine_version="0",
        autogluon_version="0",
    )
    return pipeline.registry.register(version)


def test_a_context_without_a_configuration_is_never_claimed(pipeline: Pipeline) -> None:
    sentinel = cast(StageContext, object())
    assert uplift_flow_for(pipeline, sentinel, RunMode.TRAIN) is None
    assert uplift_flow_for(pipeline, sentinel, RunMode.SCORE) is None


def test_a_classification_run_is_left_to_phase_1(pipeline: Pipeline) -> None:
    resolved = resolve_config(USE_CASE)
    assert uplift_flow_for(pipeline, context(pipeline, resolved, RunMode.TRAIN), RunMode.TRAIN) is None
    assert uplift_flow_for(pipeline, context(pipeline, resolved, RunMode.SCORE), RunMode.SCORE) is None


def test_an_uplift_configuration_trains_and_scores_as_uplift(pipeline: Pipeline) -> None:
    resolved = resolve_config(USE_CASE, {"problem_type": "uplift"})
    train = uplift_flow_for(pipeline, context(pipeline, resolved, RunMode.TRAIN), RunMode.TRAIN)
    score = uplift_flow_for(pipeline, context(pipeline, resolved, RunMode.SCORE), RunMode.SCORE)
    assert isinstance(train, UpliftTrainFlow)
    assert isinstance(score, UpliftScoreFlow)


def test_a_score_run_follows_the_metric_of_the_model_it_scores(pipeline: Pipeline) -> None:
    resolved = resolve_config(USE_CASE)
    register(pipeline, model_id="m_propensity", metric=Metric.ROC_AUC, status=ModelStatus.CANDIDATE)
    register(pipeline, model_id="m_uplift", metric=Metric.AUUC, status=ModelStatus.CANDIDATE)

    def flow(model_version_id: str | None) -> Any:
        ctx = context(pipeline, resolved, RunMode.SCORE, model_version_id)
        return uplift_flow_for(pipeline, ctx, RunMode.SCORE)

    assert isinstance(flow("m_uplift"), UpliftScoreFlow)
    assert flow("m_propensity") is None
    assert flow("m_unknown") is None  # Phase 1's resolver refuses it, in its own words, at validate
    assert flow(None) is None  # no champion yet: Phase 1 reports CHAMPION_NOT_FOUND
    pipeline.registry.promote("m_uplift", by="test", note="uplift champion")
    assert isinstance(flow(None), UpliftScoreFlow)
