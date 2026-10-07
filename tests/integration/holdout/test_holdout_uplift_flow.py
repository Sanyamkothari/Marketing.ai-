"""A persistent holdout on a real uplift scoring run, two-column key (Plan J M92).

Trains a small uplift model through the product's own `POST /uplift/runs` (LightGBM, 50 bootstrap
resamples, as `tests/integration/uplift/test_uplift_two_column_keys.py` does), then scores two
snapshots per customer through `Pipeline.run_score` - the flow a dataset run executes - with the use
case switched to a universal holdout, a 10% explore slice and a contact budget (so the policy leaves
persuadables over budget, who are explore candidates). The uplift flow inherits the holdout
through Phase 1's `apply_actions`, so:

* `holdout_assignment.parquet` has every scored row, both key columns, and one membership per
  customer, equal to the plan's rule on the customer column alone;
* the scores' control group is exactly the eligible members;
* no sleeping dog is treated or explored, and every explore row is an eligible non-member the policy
  did not select.
"""

from __future__ import annotations

import hashlib
import io
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine import runs as engine_runs
from engine.config import ResolvedConfig, RunMode
from engine.contracts import RunRecord, RunState
from engine.holdout.assign import HOLDOUT_ASSIGNMENT_FILENAME
from engine.holdout.spec import HOLDOUT_REPORT_FILENAME, HoldoutAssignmentReport, HoldoutConfig
from engine.jobs import CancelToken
from engine.pipeline import Pipeline, StageContext
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key, upload_key
from tests.fixtures.make_uplift_data import make_uplift_snapshots

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
KEY = ["customer_id", "snapshot_date"]
TARGET = "reactivated_90d"
CUSTOMERS = 2_500
SCORE_DATES = ("2026-04-30", "2026-05-31")
TRAIN_RUN = "r_20261007_0d000001"
SCORE_RUN = "r_20261007_0d000002"
SALT = "holdout-uplift-salt-0001"
FRACTION = 0.20
EXPLORE = 0.10
BUDGET = 400
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


class _NoJobs:
    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("the flow runs on this thread")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("the flow runs on this thread")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


@dataclass(frozen=True)
class Scored:
    storage: LocalStorage
    scores: pd.DataFrame
    table: pd.DataFrame
    report: HoldoutAssignmentReport


def _finish(client: TestClient, run_id: str) -> RunRecord:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while True:
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        record = RunRecord.model_validate(response.json()["run"])
        if record.state in {RunState.DONE, RunState.FAILED, RunState.CANCELLED}:
            assert record.state is RunState.DONE, response.json()["status"]
            return record
        assert time.monotonic() < deadline, f"run {run_id} did not finish in {RUN_TIMEOUT_S}s"
        time.sleep(0.2)


@pytest.fixture(scope="module")
def scored(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Scored]:
    data_dir = tmp_path_factory.mktemp("holdout-uplift") / "data"
    data_dir.mkdir()
    with (
        pytest.MonkeyPatch.context() as patch,
        TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client,
    ):
        patch.setenv("MARKETING_AI_HOLDOUT_SALT", SALT)
        training = make_uplift_snapshots(CUSTOMERS, seed=7).frame
        payload = training.to_csv(index=False, lineterminator="\n").encode()
        uploaded = client.post(
            "/uploads",
            files={"file": ("train.csv", payload, "text/csv")},
            data={"use_case": USE_CASE, "mode": "train"},
        )
        assert uploaded.status_code == 201, uploaded.text
        patch.setattr(engine_runs, "new_run_id", lambda _moment=None: TRAIN_RUN)
        response = client.post(
            "/uplift/runs",
            json={
                "use_case": USE_CASE,
                "upload_id": uploaded.json()["upload_id"],
                "primary_key": KEY,
                "target": TARGET,
                "treatment_column": "treatment",
                "overrides": OVERRIDES,
            },
        )
        assert response.status_code == 202, response.text
        train = _finish(client, TRAIN_RUN)

        storage = LocalStorage(data_dir)
        registry = LocalModelRegistry(data_dir / REGISTRY_FILENAME)
        frame = make_uplift_snapshots(CUSTOMERS, snapshots=SCORE_DATES, seed=21).frame
        frame = frame.drop(columns=[TARGET, "treatment"])
        frame["marketing_opt_in"] = [index % 10 != 3 for index in range(len(frame.index))]
        source = upload_key("u_holdout_uplift", "source.parquet")
        buffer = io.BytesIO()
        frame.to_parquet(buffer, index=False)
        storage.write_bytes(source, buffer.getvalue())
        version = registry.get(train.model_version_id or "")
        resolved = storage.read_model(run_key(version.run_id, "run_config.json"), ResolvedConfig)
        actions = resolved.config.actions.model_copy(
            update={
                "holdout": HoldoutConfig(scope="universal", fraction=FRACTION),
                "explore_fraction": EXPLORE,
            }
        )
        policy = resolved.config.uplift.policy.model_copy(update={"budget_contacts": BUDGET})
        uplift = resolved.config.uplift.model_copy(update={"policy": policy})
        config = resolved.config.model_copy(update={"actions": actions, "uplift": uplift})
        storage.write_model(run_key(SCORE_RUN, "run_config.json"), resolved)
        context = StageContext(
            run_id=SCORE_RUN,
            mode=RunMode.SCORE,
            config=config,
            resolved=resolved,
            storage=storage,
            registry=registry,
            cancel=CancelToken(),
            primary_key=list(KEY),
            target=None,
            upload_key=source,
            model_version_id=version.model_id,
        )
        record = Pipeline(storage, registry, _NoJobs()).run_score(context)
        assert record.state is RunState.DONE, record.error
        scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(SCORE_RUN, "scores.parquet"))))
        table = pd.read_parquet(
            io.BytesIO(storage.read_bytes(run_key(SCORE_RUN, HOLDOUT_ASSIGNMENT_FILENAME)))
        )
        report = storage.read_model(run_key(SCORE_RUN, HOLDOUT_REPORT_FILENAME), HoldoutAssignmentReport)
        yield Scored(storage=storage, scores=scores, table=table, report=report)


def _rule(customer: str) -> bool:
    digest = hashlib.sha256(f"{SALT}:universal:{customer}".encode()).hexdigest()
    return int(digest[:16], 16) < FRACTION * 2**64


def _joined(scored: Scored) -> pd.DataFrame:
    scores = scored.scores.astype({"customer_id": str, "snapshot_date": str})
    table = scored.table.astype({"customer_id": str, "snapshot_date": str})
    return scores.merge(table, on=KEY, how="inner", validate="one_to_one")


def test_every_row_is_recorded_with_both_key_columns(scored: Scored) -> None:
    assert list(scored.table.columns[:2]) == KEY
    assert len(scored.table.index) == len(scored.scores.index) == CUSTOMERS * len(SCORE_DATES)
    assert len(_joined(scored).index) == len(scored.table.index)


def test_membership_is_the_rule_on_the_customer_and_one_per_customer(scored: Scored) -> None:
    joined = _joined(scored)
    assert (joined.groupby("customer_id")["holdout_member"].nunique() == 1).all()
    assert joined["holdout_member"].tolist() == [_rule(customer) for customer in joined["customer_id"]]
    eligible = joined["suppressed_reason"].isna()
    assert joined["control_group"].tolist() == (eligible & joined["holdout_member"]).tolist()


def test_no_sleeping_dog_is_treated_or_explored(scored: Scored) -> None:
    joined = _joined(scored)
    sleeping = joined["segment"] == "sleeping_dog"
    explore = joined["explore"].astype(bool)
    assert sleeping.any() and explore.any()
    assert not (sleeping & (joined["action"] == "Treat")).any()
    assert not (sleeping & explore).any()
    assert not (explore & joined["holdout_member"]).any()
    assert not (explore & joined["suppressed_reason"].notna()).any()
    assert not (explore & (joined["action"] == "Treat")).any()
    assert set(joined.loc[explore, "explore_probability"]) == {EXPLORE}


def test_the_run_records_its_spec(scored: Scored) -> None:
    spec = scored.report.spec
    assert (spec.scope, spec.fraction, spec.epoch, spec.explore_fraction) == (
        "universal",
        FRACTION,
        1,
        EXPLORE,
    )
    assert scored.report.rows == len(scored.table.index)
    assert SALT not in scored.storage.read_text(run_key(SCORE_RUN, HOLDOUT_REPORT_FILENAME))
