"""Plan J M96 end to end: the evidence a training run stores, and the ranking a scoring run uses.

One LightGBM uplift model is trained through `Pipeline.run_train` with `uplift.evidence` switched on
(3 folds), as `test_uplift_consent.py` trains one: seconds, not minutes. Then:

* the training run's `uplift_evaluation.json` carries `baseline_comparison`, `calibration_by_decile`
  and a computed `fold_auuc`, and it wrote `risk_comparison.json`, which `GET
  /runs/{run_id}/risk-comparison` serves read-only;
* a scoring run of that model writes `ranking_choice.json`; a model whose evaluation carries no
  beats-risk check (one trained before M96) writes none and ranks exactly as before;
* when the stored verdict is "does not beat risk ranking" and the use case has an approved propensity
  model, the scoring run's contact list is ranked by that model's score - the same number of Treat
  rows the uplift policy chose - with `UPLIFT_NOT_BETTER_THAN_RISK`, and its uplift budget curve is
  refused rather than drawn for a list the run did not make.

The propensity model's score is stubbed (`engine.decide.ranking.score_rows`): scoring through an
AutoGluon model is Phase 1's tested path and takes minutes, and what is under test here is which
ranking the flow uses. Fails on the code before M96.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import Metric, ResolvedConfig, RunMode, resolve_config
from engine.contracts import ModelStatus, ModelVersion, RunRecord, RunState
from engine.jobs import CancelToken, NullJobRunner
from engine.pipeline import Pipeline, StageContext
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key, upload_key
from engine.uplift.actions import TREAT_ACTION
from engine.uplift.contracts import (
    RANKING_CHOICE_FILENAME,
    RISK_COMPARISON_FILENAME,
    UPLIFT_NOT_BETTER_THAN_RISK,
    RankingChoice,
    RiskComparison,
    UpliftEvaluation,
)
from engine.utils.time import utc_now
from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
TARGET = "reactivated_90d"
TREATMENT = "treatment"
TRAIN_RUN = "r_20261008_0d960010"
TRAIN_ROWS = 5_000
SCORE_ROWS = 1_000
OVERRIDES: dict[str, Any] = {
    "problem_type": "uplift",
    "uplift.treatment_column": TREATMENT,
    "uplift.base_model": "lightgbm",
    "uplift.bootstrap_samples": 50,
    "uplift.min_arm_rows": 200,
    "uplift.min_arm_positives": 20,
    "uplift.evidence.fold_auuc": True,
    "uplift.evidence.risk_comparison": True,
    "uplift.evidence.folds": 3,
    "uplift.policy.budget_contacts": 80,
    "uplift.policy.cost_per_contact": 20.0,
    "governance.approval_required": False,
}


@dataclass(frozen=True)
class World:
    data_dir: Path
    resolved: ResolvedConfig
    version: ModelVersion
    scoring_frame: pd.DataFrame

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)

    @property
    def registry(self) -> LocalModelRegistry:
        return LocalModelRegistry(self.data_dir / REGISTRY_FILENAME)

    def context(
        self, run_id: str, *, mode: RunMode, upload: str, target: str | None, model: str | None
    ) -> StageContext:
        return StageContext(
            run_id=run_id,
            mode=mode,
            config=self.resolved.config,
            resolved=self.resolved,
            storage=self.storage,
            registry=self.registry,
            cancel=CancelToken(),
            primary_key=["customer_id"],
            target=target,
            upload_key=upload,
            model_version_id=model,
        )

    def score(self, run_id: str) -> RunRecord:
        upload = upload_key(f"u_{run_id[-8:]}", "source.csv")
        _write_csv(self.storage, upload, self.scoring_frame)
        context = self.context(
            run_id, mode=RunMode.SCORE, upload=upload, target=None, model=self.version.model_id
        )
        record = Pipeline(self.storage, self.registry, NullJobRunner()).run_score(context)
        assert record.state is RunState.DONE, record.error
        return record

    def evaluation(self) -> UpliftEvaluation:
        return self.storage.read_model(run_key(TRAIN_RUN, "uplift_evaluation.json"), UpliftEvaluation)

    def store_evaluation(self, evaluation: UpliftEvaluation) -> None:
        self.storage.write_model(run_key(TRAIN_RUN, "uplift_evaluation.json"), evaluation)


def _write_csv(storage: LocalStorage, key: str, frame: pd.DataFrame) -> None:
    storage.write_bytes(key, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("uplift-m96") / "data"
    data_dir.mkdir()
    resolved = resolve_config(USE_CASE, OVERRIDES, root=config_root)
    storage = LocalStorage(data_dir)
    upload = upload_key("u_0d9600000001", "source.csv")
    _write_csv(storage, upload, make_uplift_data(TRAIN_ROWS, seed=7).frame)
    registry = LocalModelRegistry(data_dir / REGISTRY_FILENAME)
    world = World(data_dir=data_dir, resolved=resolved, version=None, scoring_frame=pd.DataFrame())  # type: ignore[arg-type]
    record = Pipeline(storage, registry, NullJobRunner()).run_train(
        world.context(TRAIN_RUN, mode=RunMode.TRAIN, upload=upload, target=TARGET, model=None)
    )
    assert record.state is RunState.DONE, record.error
    assert record.model_version_id is not None
    campaign = make_winback_campaign(SCORE_ROWS, seed=11).frame
    frame = campaign.drop(columns=[TARGET, TREATMENT, "treatment_date"], errors="ignore")
    yield World(
        data_dir=data_dir,
        resolved=resolved,
        version=registry.get(record.model_version_id),
        scoring_frame=frame,
    )


def _scores(storage: LocalStorage, run_id: str) -> pd.DataFrame:
    return pd.read_csv(
        io.BytesIO(storage.read_bytes(run_key(run_id, "scores.csv"))), dtype={"customer_id": str}
    )


# ---------------------------------------------------------------------------
# The training run's evidence
# ---------------------------------------------------------------------------
def test_the_training_run_stores_the_beats_risk_calibration_and_fold_evidence(world: World) -> None:
    evaluation = world.evaluation()
    comparison = evaluation.baseline_comparison
    assert comparison is not None
    assert [row.baseline for row in comparison.baselines] == ["p_control", "p_treated", "propensity_model"]
    no_model = comparison.baselines[2]
    assert (
        no_model.available is False and no_model.reason == "This use case has no approved propensity model."
    )
    assert comparison.risk_baseline == "p_control"
    assert comparison.uplift_auuc == evaluation.auuc, "the paired resamples are the evaluation's own"
    assert (
        evaluation.calibration_by_decile is not None and len(evaluation.calibration_by_decile.deciles) == 10
    )
    folds = evaluation.fold_auuc
    assert folds is not None and folds.computed is True and folds.folds == 3 and len(folds.values) == 3
    assert folds.estimated_refit_seconds is not None and folds.refit_seconds is not None


def test_the_equal_budget_comparison_is_written_and_served_read_only(world: World) -> None:
    stored = world.storage.read_model(run_key(TRAIN_RUN, RISK_COMPARISON_FILENAME), RiskComparison)
    assert stored.folds == 3 and stored.top_share == 0.2 and stored.propensity_source == "treated_share"
    assert stored.rows == TRAIN_ROWS - stored.rows_excluded
    assert stored.uplift.contacts == stored.risk.contacts
    assert stored.cost_per_contact == 20.0 and stored.difference_per_rupee is not None
    client = TestClient(create_app(data_dir=world.data_dir))
    response = client.get(f"/runs/{TRAIN_RUN}/risk-comparison")
    assert response.status_code == 200, response.text
    assert RiskComparison.model_validate(response.json()) == stored
    missing = client.get(f"/runs/{TRAIN_RUN}x/risk-comparison")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# The scoring run's ranking
# ---------------------------------------------------------------------------
def test_a_model_without_the_check_ranks_exactly_as_before(world: World) -> None:
    stored = world.evaluation()
    world.store_evaluation(
        stored.model_copy(
            update={"baseline_comparison": None, "calibration_by_decile": None, "fold_auuc": None}
        )
    )
    try:
        record = world.score("r_20261008_0d960011")
    finally:
        world.store_evaluation(stored)
    assert RANKING_CHOICE_FILENAME not in record.artefacts
    assert not world.storage.exists(run_key(record.run_id, RANKING_CHOICE_FILENAME))


def test_a_scoring_run_records_which_ranking_it_used(world: World) -> None:
    record = world.score("r_20261008_0d960012")
    choice = world.storage.read_model(run_key(record.run_id, RANKING_CHOICE_FILENAME), RankingChoice)
    assert choice.ranking == "uplift", "with no approved propensity model there is nothing to fall back to"
    assert choice.beats_risk is world.evaluation().baseline_comparison.beats_risk  # type: ignore[union-attr]
    if not choice.beats_risk:
        assert choice.code == UPLIFT_NOT_BETTER_THAN_RISK
        assert "no approved propensity model" in choice.reason


def test_an_uplift_model_that_does_not_beat_risk_hands_the_list_to_the_propensity_model(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = world.evaluation()
    assert stored.baseline_comparison is not None
    failing = stored.baseline_comparison.model_copy(
        update={"beats_risk": False, "summary": "This model does not beat risk ranking (planted)."}
    )
    world.store_evaluation(stored.model_copy(update={"baseline_comparison": failing}))
    propensity = world.registry.register(
        world.version.model_copy(
            update={
                "model_id": "m_prop_m96",
                "version": world.registry.next_version(USE_CASE),
                "metric": Metric.ROC_AUC,
                "metric_label": "ROC AUC",
                "status": ModelStatus.ARCHIVED,
                "model_display_name": "LightGBM",
                "promoted_at": utc_now() - timedelta(days=10),
            }
        )
    )
    calls: list[str] = []

    def fake_score(frame: pd.DataFrame, version: ModelVersion, **_: Any) -> np.ndarray:
        calls.append(version.model_id)
        return np.asarray(frame["monthly_spend"], dtype=np.float64)

    monkeypatch.setattr("engine.decide.ranking.score_rows", fake_score)
    try:
        record = world.score("r_20261008_0d960013")
    finally:
        world.store_evaluation(stored)
    assert calls == [propensity.model_id]
    choice = world.storage.read_model(run_key(record.run_id, RANKING_CHOICE_FILENAME), RankingChoice)
    assert choice.ranking == "propensity_model" and choice.code == UPLIFT_NOT_BETTER_THAN_RISK
    assert choice.propensity_model_id == "m_prop_m96"
    assert choice.reason.startswith("This model does not beat risk ranking (planted).")

    scores = _scores(world.storage, record.run_id)
    policy = json.loads(world.storage.read_bytes(run_key(record.run_id, "policy_recommendation.json")))
    treat = scores["action"].to_numpy() == TREAT_ACTION
    assert int(treat.sum()) == policy["contacts_recommended"] == choice.contacts > 0
    assert (
        policy["expected_incremental_conversions"] is None
    ), "the hold-out's top uplift share does not describe it"
    spend = (
        world.scoring_frame.set_index("customer_id").loc[scores["customer_id"], "monthly_spend"].to_numpy()
    )
    eligible = scores["suppressed_reason"].isna().to_numpy() & ~scores["control_group"].to_numpy(dtype=bool)
    candidates = eligible & (scores["segment"].to_numpy() != "sleeping_dog")
    cut = np.sort(spend[candidates])[::-1][int(treat.sum()) - 1]
    assert (spend[treat] >= cut).all(), "Treat is the top of the propensity ranking"
    assert not (scores["segment"].to_numpy()[treat] == "sleeping_dog").any()

    client = TestClient(create_app(data_dir=world.data_dir))
    curve = client.get(f"/runs/{record.run_id}/uplift/profit-curve")
    assert curve.status_code == 409
    assert curve.json()["detail"]["code"] == "PROFIT_CURVE_UNAVAILABLE"
    assert "ranked by the approved propensity model" in curve.json()["detail"]["message"]
    served = client.get(f"/runs/{record.run_id}/artefacts/{RANKING_CHOICE_FILENAME}")
    assert served.status_code == 200 and served.json()["ranking"] == "propensity_model"
