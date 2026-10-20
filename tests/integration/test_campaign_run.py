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
    CampaignRun,
    CampaignRunRunner,
    CampaignRunState,
    CampaignRunStore,
    Speed,
    StepActions,
    StepState,
    overrides_for,
    speed_minutes,
)
from engine.config import DEFAULT_CONFIG_ROOT, load_engine_config
from engine.contracts import FeatureSchema, RunRecord, RunState, RunStatus
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
    assert overrides_for({}, Speed.QUICK) == {"model_search.time_limit_minutes": speed_minutes(Speed.QUICK)}
    assert overrides_for({}, Speed.THOROUGH)["model_search.time_limit_minutes"] > speed_minutes(
        Speed.BALANCED
    )
    assert overrides_for(OVERRIDES, Speed.THOROUGH) == OVERRIDES


def test_the_minutes_of_each_speed_come_from_engine_yaml(tmp_path: Path) -> None:
    own = (DEFAULT_CONFIG_ROOT / "engine.yaml").read_text(encoding="utf-8")
    (tmp_path / "engine.yaml").write_text(own.replace("  quick: 5\n", "  quick: 2\n"), encoding="utf-8")
    assert speed_minutes(Speed.QUICK, tmp_path) == 2
    assert speed_minutes(Speed.QUICK) == 5
    assert overrides_for({}, Speed.QUICK, tmp_path) == {"model_search.time_limit_minutes": 2}


@pytest.fixture(scope="module")
def current_model(client: TestClient, finished: dict[str, Any]) -> str:
    """Month one's list was approved: its model is the current one, the model next month can reuse."""
    model_id = str(finished["record"]["model_version_id"])
    approved = client.post(f"/models/{model_id}/approve", json={"approved_by": "an approver"})
    assert approved.status_code == 200, approved.text
    return model_id


def _upload_train(client: TestClient, frame: Any, name: str) -> str:
    uploaded = client.post(
        "/uploads",
        files={"file": (name, frame.to_csv(index=False, lineterminator="\n").encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    return str(uploaded.json()["upload_id"])


def test_next_month_the_current_model_scores_the_new_file_without_learning(
    client: TestClient, current_model: str
) -> None:
    model_id = current_model
    upload_id = _upload_train(
        client, generate(GenerationSpec(USE_CASE, rows=1_500, variant=CLEAN, seed=20261016)), "month2.csv"
    )
    offer = client.get("/campaign-runs/model-offer", params={"use_case": USE_CASE, "upload_id": upload_id})
    assert offer.status_code == 200, offer.text
    assert offer.json()["model_version_id"] == model_id and offer.json()["usable"] is True
    # The Start panel shows the server's own minutes for each speed (`campaign_speeds` in configs/engine.yaml).
    assert offer.json()["speeds"] == load_engine_config(DEFAULT_CONFIG_ROOT).campaign_speeds
    created = client.post(
        "/campaign-runs",
        json={
            "use_case": USE_CASE,
            "upload_id": upload_id,
            "primary_key": "customerID",
            "model_version_id": model_id,
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["record"]["campaign_run_id"]
    deadline = time.monotonic() + TIMEOUT_S
    while (view := client.get(f"/campaign-runs/{run_id}").json())["record"]["state"] == "running":
        assert time.monotonic() < deadline, view
        time.sleep(1)
    record = view["record"]
    assert record["state"] == "done", record["error"]
    assert [s["state"] for s in record["steps"]] == ["skipped", "done", "done", "done"]
    assert record["train_run_id"] is None and record["model_version_id"] == model_id
    score = client.get(f"/runs/{record['score_run_id']}").json()["run"]
    assert score["mode"] == "score" and score["model_version_id"] == model_id
    assert (
        score["upload_id"] != upload_id and record["campaign_id"]
    )  # the same list and campaign as a normal run


def test_a_new_file_the_current_model_cannot_read_disables_reuse_and_is_refused(
    client: TestClient, data_dir: Path, current_model: str
) -> None:
    versions = client.get("/models", params={"use_case": USE_CASE}).json()["versions"]
    version = next(v["version"] for v in versions if v["version"]["model_id"] == current_model)
    needed = next(
        c.name
        for c in LocalStorage(data_dir).read_model(version["schema_key"], FeatureSchema).columns
        if c.required
    )
    frame = generate(GenerationSpec(USE_CASE, rows=1_500, variant=CLEAN, seed=20261017)).drop(
        columns=[needed]
    )
    upload_id = _upload_train(client, frame, "renamed.csv")
    offer = client.get(
        "/campaign-runs/model-offer", params={"use_case": USE_CASE, "upload_id": upload_id}
    ).json()
    assert offer["usable"] is False and f"'{needed}'" in offer["reason"]
    refused = client.post(
        "/campaign-runs",
        json={
            "use_case": USE_CASE,
            "upload_id": upload_id,
            "primary_key": "customerID",
            "model_version_id": version["model_id"],
        },
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "MODEL_CANNOT_READ_FILE"


def test_a_model_that_learned_another_outcome_than_this_months_is_not_reused(
    client: TestClient, data_dir: Path, current_model: str
) -> None:
    learned = next(
        v["version"]
        for v in client.get("/models", params={"use_case": USE_CASE}).json()["versions"]
        if v["version"]["model_id"] == current_model
    )
    target = LocalStorage(data_dir).read_model(learned["schema_key"], FeatureSchema).target
    upload_id = _upload_train(
        client, generate(GenerationSpec(USE_CASE, rows=1_500, variant=CLEAN, seed=20261018)), "month3.csv"
    )
    params = {"use_case": USE_CASE, "upload_id": upload_id}
    same = client.get("/campaign-runs/model-offer", params={**params, "outcome": target}).json()
    assert same["usable"] is True
    other = client.get("/campaign-runs/model-offer", params={**params, "outcome": "churned_90d"}).json()
    assert other["usable"] is False and "'churned_90d'" in other["reason"]
    refused = client.post(
        "/campaign-runs",
        json={
            "use_case": USE_CASE,
            "upload_id": upload_id,
            "primary_key": "customerID",
            "target": "churned_90d",
            "model_version_id": current_model,
        },
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "MODEL_OUTCOME_CHANGED"
