"""M108's request to M106, done at M106's integration (DEC-1318 (n), DEC-1316 (p)): `POST /uplift/runs` passes
the same run cost gate and starts the same cost watcher as `POST /runs`.

The upload, its profile and both layers of checks (Phase 1's and the six uplift checks) are real, on the
uplift fixture's randomised campaign; only the job body is a stand-in that marks the run running and waits,
because the cap reads the run's own record and asks the runner to cancel. The deployment is described as
SageMaker with the checkout's own price list, as `test_run_cost_api.py` does for `POST /runs`.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import uplift as uplift_routes
from engine.aws import run_cost
from engine.aws.prices import PRICES_FILENAME, PriceTable, load_price_table
from engine.contracts import RunState
from engine.pilot.plain import jargon_in
from engine.storage import LocalStorage
from tests.fixtures.make_uplift_data import make_uplift_data
from tests.fixtures.settings import local_settings, sagemaker_settings
from tests.integration.decide.test_run_cost_api import (
    LIMIT_S,
    SCORE_INSTANCE,
    TRAIN_INSTANCE,
    RecordingRunner,
    _starting,
    ceiling_usd,
    record_of,
    set_cap,
    wait_for,
)

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
ROWS = 2_000
OVERRIDES: dict[str, Any] = {
    "uplift": {
        "base_model": "lightgbm",
        "bootstrap_samples": 20,
        "min_arm_rows": 200,
        "min_arm_positives": 20,
    },
    "governance": {"approval_required": False},
}


@pytest.fixture
def table(config_root: Path) -> PriceTable:
    loaded = load_price_table(config_root / PRICES_FILENAME)
    assert loaded is not None
    return loaded


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def storage(data_dir: Path) -> LocalStorage:
    return LocalStorage(data_dir)


AppFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(
    config_root: Path, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[AppFactory]:
    clients: list[TestClient] = []

    def build(*, cap: float | None = None, deployment: Any = None) -> TestClient:
        root = tmp_path / f"configs-{len(clients)}"
        shutil.copytree(config_root, root)
        if cap is not None:
            set_cap(root, cap)

        def build_blocking(spec: Any, *, storage: Any, registry: Any) -> Any:
            del storage, registry
            return _starting(spec.run_id, data_dir)

        monkeypatch.setattr(uplift_routes, "build_job_fn", build_blocking)
        app = create_app(config_root=root, data_dir=data_dir)
        app.state.settings = deployment or sagemaker_settings(
            sagemaker_instance_type=TRAIN_INSTANCE,
            sagemaker_processing_instance_type=SCORE_INSTANCE,
            sagemaker_max_runtime_seconds=LIMIT_S,
        )
        app.state.jobs = RecordingRunner()
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        for watch in getattr(client.app.state, "cost_watches", []):
            watch.stop()
        client.app.state.jobs.shutdown(wait=False)
        client.__exit__(None, None, None)


def start(client: TestClient, **extra: Any) -> Any:
    payload = make_uplift_data(ROWS, seed=11).frame.to_csv(index=False, lineterminator="\n").encode()
    uploaded = client.post(
        "/uploads",
        files={"file": ("train.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = {
        "use_case": USE_CASE,
        "upload_id": uploaded.json()["upload_id"],
        "primary_key": "customer_id",
        "target": "reactivated_90d",
        "treatment_column": "treatment",
        "overrides": OVERRIDES,
        **extra,
    }
    return client.post("/uplift/runs", json=body)


def test_a_capped_uplift_run_is_refused_unless_confirmed_and_then_watched(
    make_client: AppFactory, table: PriceTable, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(cap=round(ceiling_usd(table) / 2, 4))
    refused = start(client)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "RUN_COST_NEEDS_CONFIRMATION"
    assert "USD" in detail["message"] and jargon_in(detail["message"]) == ()
    assert storage.list_keys("runs/") == (), "a refused run must leave no run behind"

    monkeypatch.setattr(run_cost, "COST_WATCH_INTERVAL_S", 0.02)
    accepted = start(client, confirm_cost=True)
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    assert len(client.app.state.cost_watches) == 1, "a confirmed capped run is watched as POST /runs' is"
    assert wait_for(lambda: record_of(storage, run_id).started_at is not None)
    started = record_of(storage, run_id).started_at
    assert started is not None
    monkeypatch.setattr(run_cost, "utc_now", lambda: started + timedelta(hours=3))
    assert wait_for(lambda: record_of(storage, run_id).state is RunState.CANCELLED)
    error = record_of(storage, run_id).error
    assert error is not None and error.code == "RUN_COST_CAP_REACHED"


def test_an_uplift_run_within_the_cap_starts_without_asking(
    make_client: AppFactory, table: PriceTable
) -> None:
    client = make_client(cap=round(ceiling_usd(table) * 2, 4))
    response = start(client)
    assert response.status_code == 202, response.text
    assert len(client.app.state.cost_watches) == 1


def test_with_no_cap_an_uplift_run_starts_as_before_and_is_not_watched(make_client: AppFactory) -> None:
    client = make_client()
    response = start(client)
    assert response.status_code == 202, response.text
    assert set(response.json()) == {"run_id"}
    assert getattr(client.app.state, "cost_watches", []) == []


def test_a_deployment_nothing_bills_never_asks_about_an_uplift_run(make_client: AppFactory) -> None:
    client = make_client(cap=0.01, deployment=local_settings())
    response = start(client)
    assert response.status_code == 202, response.text
    assert getattr(client.app.state, "cost_watches", []) == []
