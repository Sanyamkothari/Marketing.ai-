"""Starting a stopped campaign run again: only a run that has stopped can restart, as a new run with its choices."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.campaign_run import CampaignRunState, CampaignRunStore, Speed
from engine.storage import LocalStorage
from engine.utils.time import utc_now

pytestmark = pytest.mark.integration

USE_CASE = "telco-churn"


@pytest.fixture
def app_and_store(config_root: Path, tmp_path: Path) -> Iterator[tuple[TestClient, CampaignRunStore]]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path)) as client:
        yield client, CampaignRunStore(LocalStorage(tmp_path))


def test_only_a_stopped_run_restarts(app_and_store: tuple[TestClient, CampaignRunStore]) -> None:
    client, store = app_and_store
    missing = client.post("/campaign-runs/cr_20261015_00000000/restart")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "CAMPAIGN_RUN_NOT_FOUND"
    running = store.create(use_case_id=USE_CASE, upload_id="u_gone")
    refused = client.post(f"/campaign-runs/{running.campaign_run_id}/restart")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "CAMPAIGN_RUN_NOT_STOPPED"


def test_a_stopped_run_goes_through_the_same_checks_as_a_new_one(
    app_and_store: tuple[TestClient, CampaignRunStore],
) -> None:
    client, store = app_and_store
    run = store.create(use_case_id=USE_CASE, upload_id="u_gone", primary_key="customerID", speed=Speed.QUICK)
    store.save(run.model_copy(update={"state": CampaignRunState.STOPPED, "finished_at": utc_now()}))
    # The file is gone, so the new run is refused exactly as `POST /campaign-runs` refuses it: nothing is recorded.
    again = client.post(f"/campaign-runs/{run.campaign_run_id}/restart", json={"confirm_cost": True})
    assert again.status_code == 404, again.text
    assert again.json()["detail"]["code"] != "CAMPAIGN_RUN_NOT_FOUND"
    assert [r.campaign_run_id for r in store.list()] == [run.campaign_run_id]


def test_the_runs_of_one_case_are_listed_by_case_id(
    app_and_store: tuple[TestClient, CampaignRunStore],
) -> None:
    """The Data Doctor screen asks which run an approved case started, so it can say it is going."""
    client, store = app_and_store
    mine = store.create(use_case_id=USE_CASE, upload_id="u_gone", case_id="case_mine")
    store.create(use_case_id=USE_CASE, upload_id="u_gone", case_id="case_other")
    store.create(use_case_id=USE_CASE, upload_id="u_gone")
    found = client.get("/campaign-runs", params={"case_id": "case_mine"})
    assert found.status_code == 200, found.text
    assert [r["record"]["campaign_run_id"] for r in found.json()["campaign_runs"]] == [mine.campaign_run_id]
    assert client.get("/campaign-runs", params={"case_id": "case_none"}).json()["campaign_runs"] == []
