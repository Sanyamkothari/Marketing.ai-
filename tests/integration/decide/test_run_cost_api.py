"""M108 (DEC-1318) through the API: the estimate beside the Run button, the cap, and the stop.

Everything goes through the routes a real deployment serves (`GET /use-cases/{id}/cost-estimate`,
`POST /runs`, `GET /runs/{id}`, `/cost/fx-rate`, `/cost/spend`), on a deployment described by
`Settings(job_backend="sagemaker", ...)` with the checkout's own `configs/aws_prices.yaml` as the
price list. Nothing calls AWS: the jobs themselves run on the in-process runner every API test uses,
which is all the cap needs, because the cap reads a run's own record and asks the runner to cancel.

The ingest and validate stand-ins and the blocking job body are the ones `test_api_runs.py` uses for
its own API tests (the real flows are slow and are exercised by their own tests); what is real here is
the gate in `POST /runs`, the cap in the run's own resolved configuration, the watcher and the stop.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.access_policy import policy_for
from api.main import create_app
from api.routes import runs
from engine.access.roles import Role
from engine.aws import run_cost
from engine.aws.prices import (
    PRICES_FILENAME,
    SECONDS_PER_HOUR,
    PriceTable,
    cost_estimate,
    load_price_table,
)
from engine.config import RunMode
from engine.contracts import (
    ComputeBackend,
    ComputeInfo,
    JobEntrypoint,
    RunManifest,
    RunRecord,
    RunState,
)
from engine.jobs import CancelToken, ThreadJobRunner
from engine.pilot.plain import jargon_in
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.make_run import RunSpec, write_run
from tests.fixtures.settings import sagemaker_settings
from tests.integration.test_api_runs import (
    install_m2_job_stub,
    install_validate_stub,
    run_body,
    upload,
)
from tests.integration.test_api_uploads import DEMO_ID, install_ingest_stub

pytestmark = pytest.mark.integration

REGION = "ap-south-1"
TRAIN_INSTANCE = "ml.m5.2xlarge"
SCORE_INSTANCE = "ml.m5.xlarge"
LIMIT_S = 7200
FX_BODY = {"inr_per_usd": 84.5, "source": "Finance sheet, 1 October 2026", "as_of": "2026-10-01"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def set_cap(root: Path, cap: float) -> None:
    """Write the cap into the copy's `governance` defaults - where an Admin would put it."""
    path = root / "engine.yaml"
    text = path.read_text(encoding="utf-8")
    assert "max_run_cost_usd: null" in text, "engine.yaml's governance block must carry the cap setting"
    path.write_text(text.replace("max_run_cost_usd: null", f"max_run_cost_usd: {cap}", 1), encoding="utf-8")


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


def ceiling_usd(table: PriceTable) -> float:
    rate = table.rate(component="training", instance_type=TRAIN_INSTANCE, region=REGION)
    assert rate is not None
    return LIMIT_S * rate.usd_per_hour / SECONDS_PER_HOUR


class RecordingRunner(ThreadJobRunner):
    """The in-process runner, remembering which jobs it was asked to cancel."""

    def __init__(self) -> None:
        super().__init__(max_workers=2)
        self.cancelled: list[str] = []

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return super().cancel(job_id)


AppFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(
    config_root: Path, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[AppFactory]:
    """Build an app over a copy of `configs/`, on a SageMaker-described deployment, with a blocking job."""
    clients: list[TestClient] = []

    def build(
        *,
        cap: float | None = None,
        prices: bool = True,
        deployment: Any = None,
        blocking: bool = True,
        stubs: bool = True,
    ) -> TestClient:
        if stubs:
            install_ingest_stub(monkeypatch)
            install_validate_stub(monkeypatch)
        root = tmp_path / f"configs-{len(clients)}"
        shutil.copytree(config_root, root)
        if cap is not None:
            set_cap(root, cap)
        if not prices:
            (root / PRICES_FILENAME).unlink()
        if blocking:

            def build_blocking(spec: Any, *, storage: Any, registry: Any) -> Any:
                del storage, registry
                return _starting(spec.run_id, data_dir)

            monkeypatch.setattr(runs, "build_job_fn", build_blocking)
        else:
            install_m2_job_stub(monkeypatch)
        app = create_app(config_root=root, data_dir=data_dir)
        # `create_app` refuses settings that name S3 beside a data directory; the deployment is
        # described here after construction, which is how the rest of the API tests do it.
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


def _starting(run_id: str, data_dir: Path) -> Any:
    def job(cancel: CancelToken) -> None:
        runs.update_run(LocalStorage(data_dir), run_id, state=RunState.RUNNING, started_at=utc_now())
        cancel.wait(20)
        cancel.raise_if_cancelled()

    return job


def start(client: TestClient, **extra: Any) -> Any:
    return client.post("/runs", json=run_body(upload(client), **extra))


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def record_of(storage: LocalStorage, run_id: str) -> RunRecord:
    return storage.read_model(run_key(run_id, "run.json"), RunRecord)


def clock_at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(run_cost, "utc_now", lambda: moment)


# ---------------------------------------------------------------------------
# GET /use-cases/{id}/cost-estimate
# ---------------------------------------------------------------------------
def test_the_estimate_beside_the_run_button_is_the_list_price_ceiling(
    make_client: AppFactory, table: PriceTable
) -> None:
    client = make_client()
    body = client.get(f"/use-cases/{DEMO_ID}/cost-estimate").json()
    assert body["mode"] == "train" and body["backend"] == "sagemaker"
    assert body["estimated_usd"] == pytest.approx(ceiling_usd(table), abs=1e-4)
    assert body["inr"] is None and body["inr_reason"]
    assert "list price" in body["basis"].lower()
    assert body["cap_usd"] is None and body["needs_confirmation"] is False


def test_the_scoring_estimate_is_asked_for_by_mode(make_client: AppFactory, table: PriceTable) -> None:
    body = make_client().get(f"/use-cases/{DEMO_ID}/cost-estimate", params={"mode": "score"}).json()
    rate = table.rate(component="processing", instance_type=SCORE_INSTANCE, region=REGION)
    assert rate is not None
    assert body["estimated_usd"] == pytest.approx(LIMIT_S * rate.usd_per_hour / SECONDS_PER_HOUR, abs=1e-4)


def test_the_estimate_is_null_with_a_reason_when_the_price_list_is_missing(make_client: AppFactory) -> None:
    body = make_client(prices=False).get(f"/use-cases/{DEMO_ID}/cost-estimate").json()
    assert body["estimated_usd"] is None
    assert "price list" in body["reason"]
    assert body["inr"] is None


def test_the_estimate_is_null_with_a_reason_without_a_time_limit(make_client: AppFactory) -> None:
    deployment = sagemaker_settings(
        sagemaker_instance_type=TRAIN_INSTANCE, sagemaker_processing_instance_type=SCORE_INSTANCE
    )
    body = make_client(deployment=deployment).get(f"/use-cases/{DEMO_ID}/cost-estimate").json()
    assert body["estimated_usd"] is None and "time limit" in body["reason"]


def test_a_use_case_that_does_not_exist_is_a_404_with_a_code(make_client: AppFactory) -> None:
    response = make_client().get("/use-cases/no-such-use-case/cost-estimate")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"


def test_a_mode_that_is_not_one_is_refused(make_client: AppFactory) -> None:
    assert (
        make_client().get(f"/use-cases/{DEMO_ID}/cost-estimate", params={"mode": "bogus"}).status_code == 422
    )


# ---------------------------------------------------------------------------
# Rupees only with an Admin's rate and its source
# ---------------------------------------------------------------------------
def test_rupees_appear_only_after_an_exchange_rate_and_its_source_are_saved(make_client: AppFactory) -> None:
    client = make_client()
    assert client.get("/cost/fx-rate").json() == {"fx_rate": None}
    before = client.get(f"/use-cases/{DEMO_ID}/cost-estimate").json()
    assert before["inr"] is None

    saved = client.put("/cost/fx-rate", json=FX_BODY)
    assert saved.status_code == 200, saved.text
    assert client.get("/cost/fx-rate").json()["fx_rate"]["source"] == FX_BODY["source"]

    after = client.get(f"/use-cases/{DEMO_ID}/cost-estimate").json()
    assert after["inr"]["amount"] == pytest.approx(after["estimated_usd"] * 84.5, abs=0.01)
    assert after["inr"]["source"] == FX_BODY["source"] and after["inr"]["as_of"] == "2026-10-01"
    assert after["inr"]["inr_per_usd"] == 84.5

    assert client.delete("/cost/fx-rate").status_code == 200
    assert client.get(f"/use-cases/{DEMO_ID}/cost-estimate").json()["inr"] is None


@pytest.mark.parametrize(
    "body",
    [
        {**FX_BODY, "inr_per_usd": 0},
        {**FX_BODY, "inr_per_usd": -3},
        {**FX_BODY, "source": "  "},
        {**FX_BODY, "as_of": "yesterday"},
        {"inr_per_usd": 84.5},
    ],
)
def test_an_exchange_rate_without_a_positive_number_or_a_source_is_refused(
    make_client: AppFactory, body: dict[str, Any]
) -> None:
    client = make_client()
    assert client.put("/cost/fx-rate", json=body).status_code == 422
    assert client.get("/cost/fx-rate").json() == {"fx_rate": None}


def test_the_cost_routes_have_the_right_roles() -> None:
    assert policy_for("GET", "/use-cases/{use_case_id}/cost-estimate").role is Role.VIEWER
    assert policy_for("GET", "/cost/spend").role is Role.VIEWER
    assert policy_for("GET", "/cost/fx-rate").role is Role.VIEWER
    assert policy_for("PUT", "/cost/fx-rate").role is Role.ADMIN
    assert policy_for("DELETE", "/cost/fx-rate").role is Role.ADMIN


# ---------------------------------------------------------------------------
# The cap: refused unless confirmed
# ---------------------------------------------------------------------------
def test_a_capped_run_is_refused_unless_it_is_confirmed(
    make_client: AppFactory, table: PriceTable, storage: LocalStorage
) -> None:
    client = make_client(cap=round(ceiling_usd(table) / 2, 4))
    refused = start(client)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "RUN_COST_NEEDS_CONFIRMATION"
    assert "USD" in detail["message"] and "list price" in detail["message"].lower()
    assert jargon_in(detail["message"]) == ()
    assert storage.list_keys("runs/") == (), "a refused run must leave no run behind"

    accepted = start(client, confirm_cost=True)
    assert accepted.status_code == 202, accepted.text
    assert record_of(storage, accepted.json()["run_id"]).state in {RunState.PENDING, RunState.RUNNING}


def test_the_estimate_tells_the_screen_a_confirmation_is_needed(
    make_client: AppFactory, table: PriceTable
) -> None:
    body = make_client(cap=round(ceiling_usd(table) / 2, 4)).get(f"/use-cases/{DEMO_ID}/cost-estimate").json()
    assert body["over_cap"] is True and body["needs_confirmation"] is True
    assert body["cap_usd"] == pytest.approx(ceiling_usd(table) / 2, abs=1e-3)


def test_a_run_within_the_cap_starts_without_asking(make_client: AppFactory, table: PriceTable) -> None:
    client = make_client(cap=round(ceiling_usd(table) * 2, 4))
    assert start(client).status_code == 202


def test_with_no_cap_nothing_changes_about_starting_a_run(
    make_client: AppFactory, storage: LocalStorage
) -> None:
    client = make_client()
    response = start(client)
    assert response.status_code == 202
    assert set(response.json()) == {"run_id"}
    assert record_of(storage, response.json()["run_id"]).error is None


def test_a_deployment_nothing_bills_never_asks_for_confirmation(
    make_client: AppFactory, tmp_path: Path
) -> None:
    from tests.fixtures.settings import local_settings

    client = make_client(cap=0.01, deployment=local_settings())
    assert start(client).status_code == 202


def test_a_cap_that_cannot_be_checked_asks_too(make_client: AppFactory) -> None:
    client = make_client(cap=5.0, prices=False)
    refused = start(client)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "RUN_COST_NEEDS_CONFIRMATION"
    assert "cannot" in refused.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# The cap: a confirmed run past it is stopped
# ---------------------------------------------------------------------------
def started_run(client: TestClient, storage: LocalStorage) -> tuple[str, datetime]:
    run_id = start(client, confirm_cost=True).json()["run_id"]
    assert wait_for(lambda: record_of(storage, run_id).started_at is not None)
    started = record_of(storage, run_id).started_at
    assert started is not None
    return run_id, started


def test_a_confirmed_run_past_the_cap_is_stopped(
    make_client: AppFactory, table: PriceTable, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = round(ceiling_usd(table) / 2, 4)
    client = make_client(cap=cap)
    run_id, started = started_run(client, storage)
    rate = table.rate(component="training", instance_type=TRAIN_INSTANCE, region=REGION)
    assert rate is not None
    seconds_to_cap = cap / rate.usd_per_hour * SECONDS_PER_HOUR

    clock_at(monkeypatch, started + timedelta(seconds=seconds_to_cap / 2))
    assert client.get(f"/runs/{run_id}").json()["run"]["state"] in {"pending", "running"}

    clock_at(monkeypatch, started + timedelta(seconds=seconds_to_cap + 60))
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["run"]["state"] == "cancelled"
    assert detail["status"]["state"] == "cancelled"
    assert detail["run"]["error"]["code"] == "RUN_COST_CAP_REACHED"
    message = detail["run"]["error"]["message"]
    assert "stopped" in message and "USD" in message and "list price" in message.lower()
    assert jargon_in(message) == ()
    assert run_id in client.app.state.jobs.cancelled, "the runner is asked to stop the job itself"


def test_the_watcher_stops_a_run_nobody_is_polling(
    make_client: AppFactory, table: PriceTable, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_cost, "COST_WATCH_INTERVAL_S", 0.02)
    cap = round(ceiling_usd(table) / 2, 4)
    client = make_client(cap=cap)
    run_id, started = started_run(client, storage)
    clock_at(monkeypatch, started + timedelta(hours=3))
    assert wait_for(lambda: record_of(storage, run_id).state is RunState.CANCELLED)
    error = record_of(storage, run_id).error
    assert error is not None and error.code == "RUN_COST_CAP_REACHED"


def test_a_run_under_the_cap_is_left_alone_however_often_it_is_polled(
    make_client: AppFactory, table: PriceTable, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(cap=round(ceiling_usd(table) * 2, 4))
    run_id, started = started_run(client, storage)
    clock_at(monkeypatch, started + timedelta(seconds=LIMIT_S))
    for _ in range(3):
        assert client.get(f"/runs/{run_id}").json()["run"]["state"] in {"pending", "running"}


def test_without_a_cap_a_long_run_is_never_stopped_for_its_cost(
    make_client: AppFactory, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client()
    run_id, started = started_run(client, storage)
    clock_at(monkeypatch, started + timedelta(days=2))
    assert client.get(f"/runs/{run_id}").json()["run"]["state"] in {"pending", "running"}


def test_a_run_that_cannot_be_priced_is_not_stopped_for_a_cost_nobody_can_state(
    make_client: AppFactory, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(cap=5.0, prices=False)
    run_id = start(client, confirm_cost=True).json()["run_id"]
    assert wait_for(lambda: record_of(storage, run_id).started_at is not None)
    clock_at(monkeypatch, utc_now() + timedelta(days=2))
    assert client.get(f"/runs/{run_id}").json()["run"]["state"] in {"pending", "running"}


def test_a_finished_run_is_never_touched_by_the_cap(
    make_client: AppFactory, table: PriceTable, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(cap=round(ceiling_usd(table) / 2, 4))
    run_id, started = started_run(client, storage)
    runs.update_run(storage, run_id, state=RunState.DONE, finished_at=started + timedelta(minutes=1))
    clock_at(monkeypatch, started + timedelta(days=2))
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["run"]["state"] == "done" and detail["run"]["error"] is None


# ---------------------------------------------------------------------------
# The monthly spend view
# ---------------------------------------------------------------------------
def priced_run(storage: LocalStorage, table: PriceTable, *, when: datetime, billable: float | None) -> str:
    """A finished run of the repository's synthetic-run fixture, its manifest priced by the engine's own code."""
    run_id = write_run(storage, RunSpec(use_case_id=DEMO_ID, mode=RunMode.SCORE, rows=60, created_at=when))
    key = run_key(run_id, "run_manifest.json")
    manifest = storage.read_model(key, RunManifest)
    compute = ComputeInfo(
        backend=ComputeBackend.SAGEMAKER,
        entrypoint=JobEntrypoint.TRAIN,
        instance_type=TRAIN_INSTANCE,
        instance_count=1,
        region=REGION,
        duration_s=3600.0,
        billable_seconds=billable,
        billable_seconds_source="DescribeTrainingJob.BillableTimeInSeconds" if billable else None,
    )
    storage.write_model(
        key,
        manifest.model_copy(
            update={"compute": compute, "cost_estimate": cost_estimate(compute, table=table)}
        ),
    )
    return run_id


def test_the_monthly_spend_adds_up_the_list_price_estimates_by_month(
    make_client: AppFactory, table: PriceTable, storage: LocalStorage
) -> None:
    client = make_client(stubs=False)
    now = utc_now()
    this_month = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    last_month = (this_month - timedelta(days=3)).replace(day=10)
    priced_run(storage, table, when=this_month, billable=3600.0)
    priced_run(storage, table, when=this_month + timedelta(hours=1), billable=1800.0)
    priced_run(storage, table, when=this_month + timedelta(hours=2), billable=None)
    priced_run(storage, table, when=last_month, billable=7200.0)
    rate = table.rate(component="training", instance_type=TRAIN_INSTANCE, region=REGION)
    assert rate is not None

    view = client.get("/cost/spend", params={"months": 3}).json()
    by_month = {row["month"]: row for row in view["months"]}
    current = by_month[f"{this_month:%Y-%m}"]
    assert current["runs"] == 3 and current["priced_runs"] == 2 and current["unpriced_runs"] == 1
    assert current["estimated_usd"] == pytest.approx(rate.usd_per_hour * 1.5, abs=1e-3)
    previous = by_month[f"{last_month:%Y-%m}"]
    assert previous["estimated_usd"] == pytest.approx(rate.usd_per_hour * 2.0, abs=1e-3)
    assert "list price" in view["basis"].lower()
    assert all(row["inr"] is None for row in view["months"]), "no rupees without an exchange rate"

    assert client.put("/cost/fx-rate", json=FX_BODY).status_code == 200
    with_rupees = {
        row["month"]: row for row in client.get("/cost/spend", params={"months": 3}).json()["months"]
    }
    assert with_rupees[f"{this_month:%Y-%m}"]["inr"]["amount"] == pytest.approx(
        current["estimated_usd"] * 84.5, abs=0.01
    )
    assert with_rupees[f"{this_month:%Y-%m}"]["inr"]["source"] == FX_BODY["source"]


def test_a_month_with_no_priced_run_has_no_amount_not_a_zero(
    make_client: AppFactory, table: PriceTable, storage: LocalStorage
) -> None:
    client = make_client(stubs=False)
    when = utc_now().replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    priced_run(storage, table, when=when, billable=None)
    row = next(r for r in client.get("/cost/spend").json()["months"] if r["month"] == f"{when:%Y-%m}")
    assert row["estimated_usd"] is None and row["runs"] == 1 and row["priced_runs"] == 0


def test_a_month_with_no_runs_is_listed_with_no_amount(make_client: AppFactory) -> None:
    months = make_client().get("/cost/spend", params={"months": 2}).json()["months"]
    assert len(months) == 2
    assert all(row["runs"] == 0 and row["estimated_usd"] is None for row in months)


def test_the_number_of_months_is_bounded(make_client: AppFactory) -> None:
    client = make_client()
    assert client.get("/cost/spend", params={"months": 0}).status_code == 422
    assert client.get("/cost/spend", params={"months": 61}).status_code == 422


def test_an_unreadable_exchange_rate_leaves_the_estimate_in_dollars(
    make_client: AppFactory, table: PriceTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.aws import spend

    def broken(self: Any) -> Any:
        raise RuntimeError("the database is not there")

    monkeypatch.setattr(spend.FxStore, "get", broken)
    response = make_client().get(f"/use-cases/{DEMO_ID}/cost-estimate")
    assert response.status_code == 200
    body = response.json()
    assert body["estimated_usd"] == pytest.approx(ceiling_usd(table), abs=1e-4) and body["inr"] is None
