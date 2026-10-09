"""M108 (DEC-1318) is opt-in: with no cap set, nothing about starting, polling or cancelling a run changes.

The default configuration has no cap, and a run on a laptop (the thread pool) is billed by nobody. These
tests start runs through `POST /runs` and read them through `GET /runs/{id}` with the cost code made to
fail loudly if it is reached, and pin what the new setting adds to a run's own record: one null key.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from engine.config import load_use_case, recipe_from_config
from engine.contracts import RunRecord
from engine.runs import CREATED_ARTEFACTS, cancel_run
from engine.storage import LocalStorage, run_key
from tests.fixtures.settings import sagemaker_settings
from tests.integration.test_api_runs import (
    install_m2_job_stub,
    install_validate_stub,
    run_body,
    upload,
)
from tests.integration.test_api_uploads import DEMO_ID, install_ingest_stub

pytestmark = pytest.mark.integration


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the cost code ran for a run with no cost cap")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_ingest_stub(monkeypatch)
    install_validate_stub(monkeypatch)
    install_m2_job_stub(monkeypatch)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def test_a_default_run_never_reaches_the_cost_gate_or_the_watcher(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "estimate_run_cost", _boom)
    monkeypatch.setattr(runs, "start_cost_watch", _boom)
    monkeypatch.setattr(runs, "enforce_cost_cap", _boom)
    response = client.post("/runs", json=run_body(upload(client)))
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    client.app.state.jobs.wait(run_id, 10.0)
    assert client.get(f"/runs/{run_id}").status_code == 200


def test_a_cloud_deployment_with_no_cap_starts_a_run_without_pricing_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.app.state.settings = sagemaker_settings(sagemaker_max_runtime_seconds=3600)
    monkeypatch.setattr(runs, "estimate_run_cost", _boom)
    monkeypatch.setattr(runs, "start_cost_watch", _boom)
    response = client.post("/runs", json=run_body(upload(client)))
    assert response.status_code == 202, response.text
    assert not getattr(client.app.state, "cost_watches", [])


def test_the_run_directory_holds_the_same_files_as_before(client: TestClient, data_dir: Path) -> None:
    run_id = client.post("/runs", json=run_body(upload(client))).json()["run_id"]
    client.app.state.jobs.wait(run_id, 10.0)
    keys = set(LocalStorage(data_dir).list_keys(f"runs/{run_id}/"))
    assert keys >= {f"runs/{run_id}/{name}" for name in (*CREATED_ARTEFACTS, "job_spec.json")}
    assert not [key for key in keys if "cost" in key], "the cap writes no artefact of its own"


def test_the_only_thing_the_setting_adds_to_a_runs_own_configuration_is_one_null(
    client: TestClient, data_dir: Path
) -> None:
    run_id = client.post("/runs", json=run_body(upload(client))).json()["run_id"]
    client.app.state.jobs.wait(run_id, 10.0)
    document = json.loads(LocalStorage(data_dir).read_bytes(run_key(run_id, "run_config.json")))
    governance = document["config"]["governance"]
    assert governance == {
        "retention_days": 90,
        "consent_column": None,
        "approval_required": True,
        "max_run_cost_usd": None,
    }


def test_a_cap_changes_no_recipe(config_root: Path) -> None:
    config = load_use_case(DEMO_ID, config_root)
    capped = config.model_copy(
        update={"governance": config.governance.model_copy(update={"max_run_cost_usd": 3.0})}
    )
    shared: dict[str, Any] = {"primary_key": "customer_id", "feature_columns": ("tenure_months",), "seed": 7}
    assert recipe_from_config(capped, **shared) == recipe_from_config(config, **shared)


def test_cancelling_a_run_changes_no_error_unless_asked_to(client: TestClient, data_dir: Path) -> None:
    run_id = client.post("/runs", json=run_body(upload(client))).json()["run_id"]
    client.app.state.jobs.wait(run_id, 10.0)
    store = LocalStorage(data_dir)
    before = store.read_model(run_key(run_id, "run.json"), RunRecord)
    cancelled = cancel_run(store, run_id)
    assert cancelled.state.value == "cancelled"
    assert cancelled.error == before.error, "a plain cancel leaves the record's error exactly as it was"


def test_a_run_is_not_refused_for_an_unrecognised_confirmation_field_missing(client: TestClient) -> None:
    body = run_body(upload(client))
    assert "confirm_cost" not in body
    assert client.post("/runs", json=body).status_code == 202
    assert client.post("/runs", json={**run_body(upload(client)), "confirm_cost": "maybe"}).status_code == 422
