"""Uplift on a two-column key, end to end (M53): train, score and measure keyed by customer + snapshot.

The training file holds 2,500 customers at three month-ends, each customer in one arm for all three
(`make_uplift_snapshots`, a persistent hold-out). It goes through the product's own
`POST /uplift/runs` with `primary_key: [customer_id, snapshot_date]`, and the run must:

* train, with neither key column (nor the joined row key) as a feature;
* keep every customer's snapshots on one side of the hold-out (`split.json` names the group column);
* count the arms in customers in `uplift_validation.json`;
* store a drift baseline, so scoring can measure drift.

A file where some customers change arm between snapshots is refused with
`TREATMENT_VARIES_WITHIN_ENTITY` before a run exists.

Scoring on a two-column key needs a built dataset on Phase 1's `POST /runs` (uploads keep a single
key there, DEC-083), so the scoring run is driven through `Pipeline.run_score` with the composite
key on the same storage and registry - the flow a dataset run executes. Its `scores.csv` must carry
both key columns, hold customers out at every snapshot or at none, and its `uplift_drift.json` must
measure feature drift and say honestly that a file without a treatment column has no treated share.
Campaign results are then measured through the API, joined on both columns.

LightGBM, 50 bootstrap resamples and pinned run ids, as in `test_uplift_api.py`: seconds, not minutes.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine import keys
from engine import runs as engine_runs
from engine.config import ResolvedConfig, RunMode
from engine.contracts import RunManifest, RunRecord, RunState, SplitReport, load_artefact
from engine.jobs import CancelToken
from engine.pipeline import MANIFEST_FILENAME, Pipeline, StageContext
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.stages import register
from engine.storage import LocalStorage, run_key, upload_key
from engine.uplift.contracts import (
    UPLIFT_DRIFT_FILENAME,
    IncrementalityReport,
    UpliftDriftReport,
    UpliftModelCard,
    UpliftValidationReport,
)
from engine.uplift.flow import UPLIFT_HOLDOUT_FILENAME, model_card_key, read_holdout
from tests.fixtures.make_uplift_data import make_uplift_snapshots

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
KEY = ["customer_id", "snapshot_date"]
TARGET = "reactivated_90d"
CUSTOMERS = 2_500
SCORE_DATES = ("2026-04-30", "2026-05-31")
TRAIN_RUN = "r_20260923_0c000001"
SCORE_RUN = "r_20260923_0c000002"
SHARE_RUN = "r_20260923_0c000003"
RUN_TIMEOUT_S = 600.0
OVERRIDES: dict[str, Any] = {
    "uplift": {
        "base_model": "lightgbm",
        "bootstrap_samples": 50,
        "min_arm_rows": 200,
        "min_arm_positives": 20,
    },
    "governance": {"approval_required": False},
}


@dataclass(frozen=True)
class World:
    client: TestClient
    data_dir: Path

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)

    @property
    def registry(self) -> LocalModelRegistry:
        return LocalModelRegistry(self.data_dir / REGISTRY_FILENAME)


def _upload(world: World, frame: pd.DataFrame, *, mode: str) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = world.client.post(
        "/uploads",
        files={"file": (f"{mode}.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _body(upload_id: str) -> dict[str, Any]:
    return {
        "use_case": USE_CASE,
        "upload_id": upload_id,
        "primary_key": KEY,
        "target": TARGET,
        "treatment_column": "treatment",
        "overrides": OVERRIDES,
    }


def _finish(world: World, run_id: str) -> RunRecord:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while True:
        response = world.client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        record = RunRecord.model_validate(response.json()["run"])
        if record.state in {RunState.DONE, RunState.FAILED, RunState.CANCELLED}:
            assert record.state is RunState.DONE, response.json()["status"]
            return record
        assert time.monotonic() < deadline, f"run {run_id} did not finish in {RUN_TIMEOUT_S}s"
        time.sleep(0.2)


def _score(world: World, frame: pd.DataFrame, *, run_id: str, version_id: str, name: str) -> RunRecord:
    """A scoring run on the composite key, as a dataset run executes it (see the module docstring)."""
    storage = world.storage
    source = upload_key(name, "source.parquet")
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(source, buffer.getvalue())
    trained = world.registry.get(version_id)
    resolved = storage.read_model(run_key(trained.run_id, "run_config.json"), ResolvedConfig)
    storage.write_model(run_key(run_id, "run_config.json"), resolved)
    context = StageContext(
        run_id=run_id,
        mode=RunMode.SCORE,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=world.registry,
        cancel=CancelToken(),
        primary_key=list(KEY),
        target=None,
        upload_key=source,
        model_version_id=version_id,
    )
    record = Pipeline(storage, world.registry, _NoJobs()).run_score(context)
    assert record.state is RunState.DONE, record.error
    return record


class _NoJobs:
    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("the flow runs on this thread")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("the flow runs on this thread")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


def _scoring_frame(seed: int, *, with_treatment: bool) -> pd.DataFrame:
    data = make_uplift_snapshots(CUSTOMERS, snapshots=SCORE_DATES, seed=seed)
    frame = data.frame.drop(columns=[TARGET])
    if not with_treatment:
        frame = frame.drop(columns=["treatment"])
    frame["marketing_opt_in"] = [index % 10 != 3 for index in range(len(frame.index))]
    return frame


@dataclass(frozen=True)
class Runs:
    world: World
    train: RunRecord
    score: RunRecord
    scored_input: pd.DataFrame


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("uplift-two-column") / "data"
    data_dir.mkdir()
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(client=client, data_dir=data_dir)


@pytest.fixture(scope="module")
def runs(world: World) -> Runs:
    upload_id = _upload(world, make_uplift_snapshots(CUSTOMERS, seed=7).frame, mode="train")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(engine_runs, "new_run_id", lambda _moment=None: TRAIN_RUN)
        response = world.client.post("/uplift/runs", json=_body(upload_id))
    assert response.status_code == 202, response.text
    train = _finish(world, TRAIN_RUN)
    scored_input = _scoring_frame(21, with_treatment=False)
    score = _score(
        world, scored_input, run_id=SCORE_RUN, version_id=train.model_version_id or "", name="u_score_2col"
    )
    return Runs(world=world, train=train, score=score, scored_input=scored_input)


def _artefact(runs: Runs, run_id: str, name: str) -> object:
    return load_artefact(name, runs.world.storage.read_text(run_key(run_id, name)))


def _scores(runs: Runs, run_id: str) -> pd.DataFrame:
    text = runs.world.storage.read_text(run_key(run_id, "scores.csv"))
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
def test_the_run_records_both_key_columns_and_is_an_uplift_run(runs: Runs) -> None:
    assert runs.train.primary_key == KEY
    assert runs.train.problem_type.value == "uplift"
    manifest = _artefact(runs, runs.train.run_id, MANIFEST_FILENAME)
    assert isinstance(manifest, RunManifest) and manifest.primary_key == KEY


def test_no_key_column_is_a_feature(runs: Runs) -> None:
    storage = runs.world.storage
    card = storage.read_model(model_card_key(f"runs/{runs.train.run_id}/model"), UpliftModelCard)
    assert not {*KEY, keys.ROW_KEY_COLUMN} & set(card.feature_columns)
    assert not {*KEY, keys.ROW_KEY_COLUMN} & set(card.dropped_columns)


def test_the_arms_are_counted_in_customers(runs: Runs) -> None:
    storage = runs.world.storage
    report = storage.read_model(run_key(runs.train.run_id, "uplift_validation.json"), UpliftValidationReport)
    assert report.passed and report.entity_column == "customer_id"
    assert report.treated_entities is not None and report.control_entities is not None
    assert report.treated_entities + report.control_entities == CUSTOMERS
    assert report.treated_rows == 3 * report.treated_entities


def test_no_customer_is_on_both_sides_of_the_hold_out(runs: Runs) -> None:
    split = _artefact(runs, runs.train.run_id, "split.json")
    assert isinstance(split, SplitReport) and split.group_column == "customer_id"
    holdout = read_holdout(runs.world.storage, run_key(runs.train.run_id, UPLIFT_HOLDOUT_FILENAME))
    test_customers = {key.partition(keys.KEY_SEPARATOR)[0] for key in holdout["primary_key"]}
    # Every hold-out customer is there at all three snapshots: none of their rows was trained on.
    per_customer = pd.Series([key.partition(keys.KEY_SEPARATOR)[0] for key in holdout["primary_key"]])
    assert (per_customer.value_counts() == 3).all()
    train_rows = split.parts[0].rows
    assert train_rows + len(holdout.index) == 3 * CUSTOMERS
    assert 0 < len(test_customers) < CUSTOMERS


def test_the_training_run_stores_a_drift_baseline_on_the_version(runs: Runs) -> None:
    version = runs.world.registry.get(runs.train.model_version_id or "")
    assert version.drift_baseline_key == run_key(runs.train.run_id, register.DRIFT_BASELINE_FILENAME)
    assert runs.world.storage.exists(version.drift_baseline_key)


def test_a_customer_in_both_arms_is_refused_before_a_run_exists(world: World) -> None:
    upload_id = _upload(world, make_uplift_snapshots(1_200, seed=3, mixed_customers=25).frame, mode="train")
    before = set(world.storage.list_keys("runs/"))
    response = world.client.post("/uplift/runs", json=_body(upload_id))
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["detail"]["code"] == "UPLIFT_VALIDATION_FAILED"
    assert "TREATMENT_VARIES_WITHIN_ENTITY" in body["detail"]["message"]
    codes = [check["code"] for check in body["uplift_validation"]["checks"]]
    assert "TREATMENT_VARIES_WITHIN_ENTITY" in codes
    assert set(world.storage.list_keys("runs/")) == before


def test_phase_1s_route_refuses_an_uplift_training_run(world: World) -> None:
    upload_id = _upload(
        world, make_uplift_snapshots(300, snapshots=("2026-01-31",), seed=1).frame, mode="train"
    )
    response = world.client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": "customer_id",
            "target": TARGET,
            "overrides": {"problem_type": "uplift"},
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "UPLIFT_REQUIRES_UPLIFT_ROUTE"
    assert "POST /uplift/runs" in detail["message"]
    assert detail["path"] == "overrides.problem_type"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def test_scores_carry_both_key_columns_for_every_row(runs: Runs) -> None:
    scores = _scores(runs, runs.score.run_id)
    assert list(scores.columns[:2]) == KEY
    assert keys.ROW_KEY_COLUMN not in scores.columns
    got = set(zip(scores["customer_id"], scores["snapshot_date"], strict=True))
    expected = set(zip(runs.scored_input["customer_id"], runs.scored_input["snapshot_date"], strict=True))
    assert got == expected and len(scores.index) == len(runs.scored_input.index)
    assert (scores["segment"] != "").all() and (scores["action"] != "").all()


def test_a_customer_is_held_out_at_every_snapshot_or_at_none(runs: Runs) -> None:
    scores = _scores(runs, runs.score.run_id)
    assert (scores.groupby("customer_id")["control_group"].nunique() == 1).all()
    assert (scores["control_group"] == "True").any()


def test_the_scoring_run_measures_feature_drift_and_says_why_it_has_no_treated_share(runs: Runs) -> None:
    report = runs.world.storage.read_model(
        run_key(runs.score.run_id, UPLIFT_DRIFT_FILENAME), UpliftDriftReport
    )
    assert report.features is not None and report.features_reason is None
    assert report.training_run_id == runs.train.run_id
    assert report.treatment.status == "not_applicable" and report.treatment.reason
    response = runs.world.client.get(f"/runs/{runs.score.run_id}/artefacts/{UPLIFT_DRIFT_FILENAME}")
    assert response.status_code == 200
    assert UpliftDriftReport.model_validate_json(response.content) == report


def test_a_scoring_file_with_a_treatment_column_gets_the_treated_share_check(runs: Runs) -> None:
    frame = _scoring_frame(22, with_treatment=True)
    _score(runs.world, frame, run_id=SHARE_RUN, version_id=runs.train.model_version_id or "", name="u_share")
    report = runs.world.storage.read_model(run_key(SHARE_RUN, UPLIFT_DRIFT_FILENAME), UpliftDriftReport)
    assert report.treatment.status == "within_tolerance"
    assert report.treatment.current_treated_share == pytest.approx(frame["treatment"].mean(), abs=1e-4)


def test_campaign_results_join_the_outcomes_on_both_columns(runs: Runs) -> None:
    scores = _scores(runs, runs.score.run_id)
    rng = np.random.default_rng(5)
    outcomes = scores[KEY].copy()
    outcomes[TARGET] = (rng.random(len(outcomes.index)) < 0.2).astype(int)
    upload_id = _upload(runs.world, outcomes, mode="score")
    response = runs.world.client.post(
        f"/runs/{runs.score.run_id}/campaign-results",
        json={"upload_id": upload_id, "outcome_column": TARGET},
    )
    assert response.status_code == 200, response.text
    report = IncrementalityReport.model_validate(response.json())
    assert report.rows_without_outcome == 0
    intended = scores["intended_treatment"] == "True"
    assert report.treated_rows + report.control_rows == int(intended.sum())
