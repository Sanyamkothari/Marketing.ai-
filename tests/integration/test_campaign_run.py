"""The campaign run (train, score the same customers, contact list, campaign) goes on without the browser.

One real journey through `TestClient` and the local job runner, with the smallest real search (one minute, one
model family, no ensemble; see `test_score_yes_no_columns.py`), then the one rule a restart needs: a run whose job
the server no longer knows is stopped with a reason.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.campaign_run import (
    SPEED_MINUTES,
    CampaignRun,
    CampaignRunRunner,
    CampaignRunState,
    CampaignRunStore,
    Speed,
    StepActions,
    StepState,
    overrides_for,
)
from engine.contracts import RunRecord, RunState, RunStatus
from engine.jobs import NullJobRunner
from engine.storage import LocalStorage, run_key
from tests.fixtures.make_data import CLEAN, GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "telco-churn"
OVERRIDES: dict[str, Any] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.candidates": ["LogisticRegression"],
    "model_search.ensemble": False,
}
TIMEOUT_S = 600.0


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("campaign-run")


@pytest.fixture(scope="module")
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def finished(client: TestClient) -> dict[str, Any]:
    frame = generate(GenerationSpec(USE_CASE, rows=2_500, variant=CLEAN, seed=20261015))
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    uploaded = client.post(
        "/uploads",
        files={"file": ("train.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = {
        "use_case": USE_CASE,
        "upload_id": uploaded.json()["upload_id"],
        "primary_key": "customerID",
        "target": "Churn",
        "overrides": OVERRIDES,
    }
    created = client.post("/campaign-runs", json=body)
    assert created.status_code == 202, created.text
    campaign_run_id = created.json()["record"]["campaign_run_id"]
    again = client.post(
        "/campaign-runs", json=body
    )  # a second click on the same data is refused, not doubled
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "CAMPAIGN_RUN_ACTIVE"
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        view = client.get(f"/campaign-runs/{campaign_run_id}").json()
        if view["record"]["state"] != "running":
            return dict(view)
        assert time.monotonic() < deadline, view
        time.sleep(1)


def test_the_chain_runs_to_a_campaign_with_the_model_just_trained(
    client: TestClient, finished: dict[str, Any]
) -> None:
    record = finished["record"]
    assert record["state"] == "done", record["error"]
    assert [step["state"] for step in record["steps"]] == ["done"] * 4
    assert finished["pct"] == 100
    train = client.get(f"/runs/{record['train_run_id']}").json()["run"]
    score = client.get(f"/runs/{record['score_run_id']}").json()["run"]
    assert (train["mode"], score["mode"]) == ("train", "score")
    assert score["model_version_id"] == train["model_version_id"] == record["model_version_id"]
    assert score["upload_id"] == record["score_upload_id"] != record["upload_id"]  # scored from its own copy
    listed = client.get("/campaign-runs", params={"state": "done"}).json()["campaign_runs"]
    assert [item["record"]["campaign_run_id"] for item in listed] == [record["campaign_run_id"]]
    assert client.get("/campaign-runs", params={"state": "running"}).json()["campaign_runs"] == []


def test_a_run_whose_job_the_server_no_longer_knows_is_stopped_with_a_reason(
    data_dir: Path, finished: dict[str, Any]
) -> None:
    storage = LocalStorage(data_dir)
    train_id = finished["record"]["train_run_id"]
    lost = "r_20261015_deadbeef"  # a training run left "running" by a server that has since restarted
    storage.write_model(
        run_key(lost, "run.json"),
        storage.read_model(run_key(train_id, "run.json"), RunRecord).model_copy(
            update={"run_id": lost, "state": RunState.RUNNING, "finished_at": None}
        ),
    )
    storage.write_model(
        run_key(lost, "status.json"),
        storage.read_model(run_key(train_id, "status.json"), RunStatus).model_copy(
            update={"run_id": lost, "state": RunState.RUNNING}
        ),
    )
    store = CampaignRunStore(storage)
    started = store.create(use_case_id=USE_CASE, upload_id="u_x", train_run_id=lost)
    started = store.save(
        started.model_copy(
            update={
                "steps": (
                    started.steps[0].model_copy(update={"state": StepState.RUNNING}),
                    *started.steps[1:],
                )
            }
        )
    )
    never: Any = None  # no step action is reached
    runner = CampaignRunRunner(
        storage, NullJobRunner(), StepActions(never, never, never, never, never, never)
    )
    stopped = runner.advance(started.campaign_run_id)
    assert isinstance(stopped, CampaignRun)
    assert stopped.state is CampaignRunState.STOPPED
    assert stopped.error is not None and stopped.error.code == "RUN_INTERRUPTED"
    assert "restarted" in stopped.error.message
    assert storage.read_model(run_key(lost, "run.json"), RunRecord).state is RunState.FAILED


def test_the_training_time_choice_sets_the_search_time_unless_the_person_set_one() -> None:
    assert overrides_for({}, Speed.QUICK) == {"model_search.time_limit_minutes": SPEED_MINUTES[Speed.QUICK]}
    assert (
        overrides_for({}, Speed.THOROUGH)["model_search.time_limit_minutes"] > SPEED_MINUTES[Speed.BALANCED]
    )
    assert overrides_for(OVERRIDES, Speed.THOROUGH) == OVERRIDES
