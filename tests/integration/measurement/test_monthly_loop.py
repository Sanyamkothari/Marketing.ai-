"""Plan J M107 acceptance: the monthly loop runs on its own, read-only, with only the approval left to a person.

A month of the product under a controlled clock, on fakes: the client's tables are files in S3 (moto)
bound to a saved connection, its outcomes are rows in a database (the SQLite-backed fake of
`tests/unit/connections/fakes.py`, reached through the real PostgreSQL connector), jobs run for real on
threads, and the local scheduler's `tick()` is called at the hours the four schedules name:

    02:00 monthly     score       - fetch the newest tables, rebuild the recipe, score with the model in use
    06:00 every day   treat_list  - build the newest scoring run's treat list and record its campaign
    07:00 every day   measure     - once the outcome window has closed, pull the window's outcomes and measure
    08:00 every day   learn       - learn the next model from the measured campaign: a challenger

Every firing is a scheduled one (`system:scheduler`); a person only sets the loop up and, at the end,
approves the challenger. Each step that produces something raises an alert. Nothing is written to the
bucket or the database: every statement the database saw is a catalogue read, `SET ... read only` or a
`SELECT`.

**The world.** `telco-churn`'s recipe (a customer table and an activity log) feeds a use case set up as a
campaign-effect use case, whose outcome is `Retained`. The truth: a contact raises the chance of staying
by 30 points in the North and East, and lowers it by 10 points elsewhere. The model in use was trained on
an experiment whose effect was the reverse, so the model learned from the month's randomised rows (the
list and the 10% explore share) beats it, and is put forward to the Approver.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import boto3
import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient
from moto import mock_aws

from api.main import create_app
from api.routes.schedules import CLOCK_SLOT, app_request, get_firer, get_schedule_store, get_scheduling_engine
from engine.access.roles import SYSTEM_SCHEDULER
from engine.clients import LocalClientStore
from engine.config import RunMode, get_roles, load_use_case
from engine.connections import postgres as postgres_module
from engine.connections import sql as sql_module
from engine.connections.store import ConnectionStore
from engine.contracts import ModelStatus, RunRecord, RunState
from engine.jobs import ThreadJobRunner
from engine.measurement.pull import PullSelection
from engine.onboarding.datasets import dataset_key
from engine.onboarding.mapping import suggested_mapping_spec
from engine.onboarding.sources import FileSourceReader, add_connection_source
from engine.registry import LocalModelRegistry
from engine.scheduling.alerts import AlertKind, AlertQuery, AlertStore
from engine.scheduling.firing import build_dataset_from_spec
from engine.scheduling.scheduler import LocalScheduler
from engine.scheduling.schedules import FiringStatus, FiringTrigger, ScheduleFiring
from engine.settings import Settings
from engine.storage import LocalStorage, run_key
from tests.unit.connections.fakes import FakeServer, driver_module
from tests.unit.production.scheduling_support import FakeClock, tables

pytestmark = [pytest.mark.slow, pytest.mark.integration]

USE_CASE: Final[str] = "telco-churn"
ENTITIES: Final[int] = 3_000
BUCKET: Final[str] = "client-exports"
TARGET: Final[str] = "Retained"
TREATMENT: Final[str] = "contacted"
OUTCOME_WINDOW_DAYS: Final[int] = 20
TICK_HOURS: Final[frozenset[int]] = frozenset({2, 6, 7, 8})
EXPLORE: Final[float] = 0.10
RUN_TIMEOUT_S: Final[float] = 600.0
UPLIFT: Final[dict[str, Any]] = {
    "base_model": "lightgbm",
    "bootstrap_samples": 50,
    "min_arm_rows": 30,
    "min_arm_positives": 10,
    "outcome_window_days": OUTCOME_WINDOW_DAYS,
    "segments": {"persuadable_min_uplift": 0.05},
}


# ---------------------------------------------------------------------------
# The truth
# ---------------------------------------------------------------------------
def effect(frame: pd.DataFrame) -> np.ndarray:
    """What a contact does to the chance of staying: +30 points in the North and East, -10 elsewhere."""
    north_east = frame["region"].astype(str).str.lower().isin({"north", "east"}).to_numpy()
    return np.where(north_east, 0.30, -0.10)


def base_rate(frame: pd.DataFrame) -> np.ndarray:
    active = pd.to_numeric(frame["events_90d"], errors="coerce").fillna(0).to_numpy()
    return np.clip(0.35 + 0.25 * (active > np.median(active)), 0.05, 0.95)


def outcomes(frame: pd.DataFrame, treated: np.ndarray, *, seed: int, scale: float = 1.0) -> np.ndarray:
    chance = np.clip(base_rate(frame) + scale * effect(frame) * treated, 0.0, 1.0)
    return (np.random.default_rng(seed).random(len(frame.index)) < chance).astype(int)


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------
def make_root(config_root: Path, target: Path) -> Path:
    """A copy of `configs/` in which `telco-churn` is a campaign-effect use case with a 10% explore share."""
    shutil.copytree(config_root, target)
    path = target / "use_cases" / "telco_churn.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["problem_type"] = "uplift"
    document["model_search"] = {"metric": "auuc", "metric_choices": ["auuc"]}
    document["target"] = {"column": TARGET, "positive_label": 1, "definition": "The subscriber stayed"}
    document["actions"] = {
        **document.get("actions", {}),
        "explore_fraction": EXPLORE,
        "control_group_fraction": 0.10,
    }
    document["uplift"] = UPLIFT
    document["validation"] = {"min_rows": 200, "min_positive": 20}
    document.setdefault("governance", {})["approval_required"] = False  # the learned model must wait anyway
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


@dataclass
class Loop:
    app: Any
    client: TestClient
    storage: LocalStorage
    registry: LocalModelRegistry
    client_store: LocalClientStore
    clock: FakeClock
    jobs: ThreadJobRunner
    s3: Any
    server: FakeServer
    client_id: str
    spec_id: str
    champion_id: str
    schedules: dict[str, str]
    outcomes_connection: str
    score_day: date
    """The day of the month the score schedule fires on: tomorrow, so the runs' own clocks (which are real
    time) and the controlled clock stay within a day of each other."""

    def scheduler(self) -> LocalScheduler:
        request = app_request(self.app)
        return LocalScheduler(get_schedule_store(request), get_firer(request), clock=self.clock, tick_seconds=60)

    def at(self, day: date, hour: int) -> tuple[ScheduleFiring, ...]:
        """Tick at every schedule hour from where the clock is up to `hour`:01 UTC on `day`.

        A tick moves the clock to the hour, settles the runs that ended, fires what is due, and waits for
        every run a firing started; so no slot is ever missed and every firing is an on-time one.
        """
        produced: list[ScheduleFiring] = []
        target = datetime(day.year, day.month, day.day, hour, 1, tzinfo=UTC)
        moment = self.clock().replace(minute=1, second=0, microsecond=0)
        while moment <= target:
            if moment.hour in TICK_HOURS and moment > self.clock():
                self.clock.now = moment
                for firing in self.scheduler().tick():
                    produced.append(firing)
                    if firing.run_id is not None and firing.status is FiringStatus.RUNNING:
                        self.jobs.wait(firing.run_id, timeout=RUN_TIMEOUT_S)
            moment += timedelta(hours=1)
        self.clock.now = max(self.clock(), target)
        return tuple(produced)

    def firings(self, kind: str) -> list[dict[str, Any]]:
        response = self.client.get(f"/schedules/{self.schedules[kind]}/firings")
        assert response.status_code == 200, response.text
        return list(response.json()["firings"])

    def alerts(self, kind: AlertKind) -> tuple[Any, ...]:
        return AlertStore(get_scheduling_engine(app_request(self.app))).query(AlertQuery(kind=kind))


def _csv(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode()


def _finish(client: TestClient, run_id: str) -> RunRecord:
    import time

    deadline = time.monotonic() + RUN_TIMEOUT_S
    while True:
        record = RunRecord.model_validate(client.get(f"/runs/{run_id}").json()["run"])
        if record.state in {RunState.DONE, RunState.FAILED, RunState.CANCELLED}:
            assert record.state is RunState.DONE, record.error
            return record
        assert time.monotonic() < deadline
        time.sleep(0.2)


@pytest.fixture(scope="module")
def loop(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> Iterator[Loop]:
    base = tmp_path_factory.mktemp("monthly-loop")
    root = make_root(config_root, base / "configs")
    data_dir = base / "data"
    data_dir.mkdir()
    with pytest.MonkeyPatch.context() as patch, mock_aws():
        for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "MARKETING_AI_CONFIG_DIR"):
            patch.delenv(name, raising=False)
        patch.setenv("AWS_ACCESS_KEY_ID", "testing")
        patch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        server = FakeServer({"crm": {"placeholder": (["id"], [(1,)])}})
        server.db.execute('CREATE TABLE "crm"."retention" ("customer_id", "retained", "recorded_on")')
        patch.setattr(postgres_module, "load_sdk", lambda _name: driver_module("psycopg", server, "Error"))
        patch.setattr(sql_module, "reach", lambda host, port: None)
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        made = tables(seed=20261001, entities=ENTITIES, end=date(2026, 6, 1))
        s3.put_object(Bucket=BUCKET, Key="exports/customers/2026-06.csv", Body=_csv(made["customers"]))
        s3.put_object(Bucket=BUCKET, Key="exports/activity/2026-06.csv", Body=_csv(made["activity"]))

        start = datetime.now(UTC).replace(second=0, microsecond=0)
        clock = FakeClock(start)
        storage = LocalStorage(data_dir)
        registry = LocalModelRegistry(data_dir / "registry.db")
        client_store = LocalClientStore(data_dir / "clients.db")
        jobs = ThreadJobRunner(max_workers=1)
        app = create_app(config_root=root, data_dir=data_dir)
        app.state.storage = storage
        app.state.registry = registry
        app.state.client_store = client_store
        app.state.jobs = jobs
        setattr(app.state, CLOCK_SLOT, clock)
        with TestClient(app) as client:
            world = _set_up(app, client, root, storage, registry, client_store, clock, jobs, s3, server)
            try:
                yield world
            finally:
                jobs.shutdown(wait=True)


def _set_up(
    app: Any,
    client: TestClient,
    root: Path,
    storage: LocalStorage,
    registry: LocalModelRegistry,
    client_store: LocalClientStore,
    clock: FakeClock,
    jobs: ThreadJobRunner,
    s3: Any,
    server: FakeServer,
) -> Loop:
    """What a person does once: connections, sources from them, a recipe, the model in use, the schedules."""
    created = client.post("/clients", json={"name": "Demo Telco", "industry": "telecom"})
    assert created.status_code == 201, created.text
    client_id = str(created.json()["client_id"])
    bucket = client.post(
        "/connections",
        json={
            "kind": "s3",
            "name": "Client exports",
            "config": {"bucket": BUCKET, "region": "us-east-1", "prefix": "exports/"},
            "secrets": {"access_key_id": "testing", "secret_access_key": "testing"},
        },
    )
    assert bucket.status_code == 201, bucket.text
    database = client.post(
        "/connections",
        json={
            "kind": "postgres",
            "name": "CRM",
            "config": {"host": "db.example.com", "database": "crm", "user": "reader"},
            "secrets": {"password": "s3cret-pw"},
        },
    )
    assert database.status_code == 201, database.text
    config = load_use_case(USE_CASE, root)
    connections = ConnectionStore(storage, Settings())
    sources = []
    for role, folder in (("entity", "exports/customers/"), ("activity", "exports/activity/")):
        source, _profile = add_connection_source(
            storage,
            client_store,
            connections,
            client_id=client_id,
            connection_id=str(bucket.json()["connection_id"]),
            selection=PullSelection(prefix=folder),
            role=role,
            config=config,
            roles=get_roles(root),
            limit_bytes=500 * 1024 * 1024,
            now=clock(),
        )
        sources.append(source)
    reader = FileSourceReader(storage, config)
    mappings = [
        client_store.save_mapping(
            suggested_mapping_spec(
                reader.profile(source), config, role=source.role or "", use_case=USE_CASE,
                mapping_id=f"m_loop_{source.role}",
            ).with_hash()
        )
        for source in sources
    ]
    from engine.onboarding.specs import (
        FeatureDef,
        FeatureSpec,
        LabelDefinition,
        LabelType,
        OnboardingSpec,
        SnapshotDefinition,
        SnapshotMode,
    )

    spec = client_store.save_spec(
        OnboardingSpec(
            spec_id="sp_loop",
            client_id=client_id,
            use_case=USE_CASE,
            entity_source_id=sources[0].source_id,
            event_source_ids=(sources[1].source_id,),
            mapping_ids=tuple(sorted(m.mapping_id for m in mappings)),
            feature_spec=FeatureSpec(
                features=(
                    FeatureDef(name="events_90d", role="activity", function="count", window_days=90),
                    FeatureDef(name="events_30d", role="activity", function="count", window_days=30),
                )
            ),
            label_spec=LabelDefinition(
                name="churn_next_60d", type=LabelType.EVENT_ABSENCE, role="activity", horizon_days=60
            ),
            snapshot_spec=SnapshotDefinition(
                mode=SnapshotMode.SINGLE,
                start=date(2026, 3, 1),
                end=date(2026, 3, 1),
                min_history_days=90,
                max_snapshots=1,
            ),
            created_at=clock(),
        ).with_hash()
    )
    # The model in use: trained on an experiment the recipe's own features describe, whose effect was the
    # reverse of the truth, and put in use by a person.
    built = build_dataset_from_spec(
        spec, config=config, mode=RunMode.TRAIN, client_store=client_store, storage=storage
    )
    frame = pd.read_parquet(storage.local_path(dataset_key(built.dataset_id, "dataset.parquet")))
    frame = frame.drop(columns=["churn_next_60d"])
    rng = np.random.default_rng(3)
    frame[TREATMENT] = (rng.random(len(frame.index)) < 0.5).astype(int)
    frame[TARGET] = outcomes(frame, frame[TREATMENT].to_numpy(), seed=4, scale=-1.0)
    uploaded = client.post(
        "/uploads",
        files={"file": ("experiment.csv", _csv(frame), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    started = client.post(
        "/uplift/runs",
        json={
            "use_case": USE_CASE,
            "upload_id": uploaded.json()["upload_id"],
            "primary_key": "entity_key",
            "target": TARGET,
            "treatment_column": TREATMENT,
        },
    )
    assert started.status_code == 202, started.text
    trained = _finish(client, started.json()["run_id"])
    champion = registry.get_champion(USE_CASE)
    if champion is None or champion.model_id != trained.model_version_id:
        promoted = client.post(
            f"/models/{trained.model_version_id}/promote",
            json={"promoted_by": "loop test", "reason": "the model in use for this test"},
        )
        assert promoted.status_code == 200, promoted.text
    champion = registry.get_champion(USE_CASE)
    assert champion is not None

    def schedule(kind: str, cron: str, parameters: dict[str, Any] | None = None) -> str:
        body: dict[str, Any] = {
            "use_case_id": USE_CASE,
            "client_id": client_id,
            "kind": kind,
            "cadence": cron,
            "timezone": "UTC",
        }
        if parameters is not None:
            body["parameters"] = parameters
        response = client.post("/schedules", json=body)
        assert response.status_code == 201, response.text
        return str(response.json()["schedule_id"])

    score_day = (clock() + timedelta(days=1)).date()
    schedules = {
        "score": schedule("score", f"0 2 {score_day.day} * *", {"onboarding_spec_id": spec.spec_id}),
        "treat_list": schedule("treat_list", "0 6 * * *"),
        "measure": schedule(
            "measure",
            "0 7 * * *",
            {
                "outcomes": {
                    "connection_id": str(database.json()["connection_id"]),
                    "selection": {"schema_name": "crm", "table": "retention"},
                    "date_column": "recorded_on",
                    "outcome_column": "retained",
                }
            },
        ),
        "learn": schedule("learn", "0 8 * * *"),
    }
    return Loop(
        app=app,
        client=client,
        storage=storage,
        registry=registry,
        client_store=client_store,
        clock=clock,
        jobs=jobs,
        s3=s3,
        server=server,
        client_id=client_id,
        spec_id=spec.spec_id,
        champion_id=champion.model_id,
        schedules=schedules,
        outcomes_connection=str(database.json()["connection_id"]),
        score_day=score_day,
    )


def _the_crm_records_outcomes(loop: Loop, run_id: str, treatment_start: datetime) -> int:
    """The client's CRM writes each scored customer's outcome inside the window, and keeps an older decoy."""
    record = loop.storage.read_model(run_key(run_id, "run.json"), RunRecord)
    from engine.runs import job_spec_key, read_job_spec

    scored_input = pd.read_parquet(loop.storage.local_path(read_job_spec(loop.storage, job_spec_key(run_id)).upload_key))
    assignment = pd.read_parquet(io.BytesIO(loop.storage.read_bytes(run_key(run_id, "holdout_assignment.parquet"))))
    treated = (
        scored_input[[record.primary_key]]
        .merge(assignment[[record.primary_key, "treated"]], on=record.primary_key, how="left")["treated"]
        .fillna(False)
        .to_numpy(dtype=bool)
    )
    stayed = outcomes(scored_input, treated, seed=11)
    inside = (treatment_start + timedelta(days=10)).date().isoformat()
    before = (treatment_start - timedelta(days=20)).date().isoformat()
    rows = [
        (str(key), int(value), inside) for key, value in zip(scored_input[record.primary_key], stayed, strict=True)
    ]
    rows += [
        (str(key), 1 - int(value), before) for key, value in zip(scored_input[record.primary_key], stayed, strict=True)
    ]
    loop.server.db.executemany('INSERT INTO "crm"."retention" VALUES (?, ?, ?)', rows)
    return len(scored_input.index)


def test_a_month_runs_score_treat_list_measure_learn_with_only_the_approval_left_to_a_person(loop: Loop) -> None:
    bucket_before = {o["Key"]: o["ETag"] for o in loop.s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    # The new month's activity lands in the bucket; nobody uploads anything.
    later = tables(seed=20261101, entities=ENTITIES, end=date(2026, 7, 1))["activity"]
    loop.s3.put_object(Bucket=BUCKET, Key="exports/activity/2026-07.csv", Body=_csv(later))
    bucket_before["exports/activity/2026-07.csv"] = loop.s3.head_object(
        Bucket=BUCKET, Key="exports/activity/2026-07.csv"
    )["ETag"]

    # 1. Scoring, at 02:00 on its day of the month.
    month = loop.score_day
    (scored,) = [f for f in loop.at(month, 2) if f.schedule_id == loop.schedules["score"]]
    assert scored.status is FiringStatus.RUNNING and scored.trigger is FiringTrigger.SCHEDULED, scored.error_code
    run_id = scored.run_id or pytest.fail("no scoring run")
    run = loop.storage.read_model(run_key(run_id, "run.json"), RunRecord)
    assert run.state is RunState.DONE and run.model_version_id == loop.champion_id
    assert run.requested_by == SYSTEM_SCHEDULER.user_id
    newest = [
        s for s in loop.client_store.list_sources(loop.client_id)
        if s.binding is not None and s.binding.object_path == "exports/activity/2026-07.csv"
    ]
    assert len(newest) == 1, "the newest object under the folder was fetched, with no upload"

    # 2. The treat list, at 06:00: the run is settled first, then its list is built and its campaign recorded.
    loop.at(month, 6)
    assert loop.firings("score")[0]["result_code"] == "SCORED"
    treat = loop.firings("treat_list")[0]
    assert (treat["status"], treat["result_code"]) == ("succeeded", "TREAT_LIST_READY"), treat
    assert loop.storage.exists(run_key(run_id, "treat_list.csv"))
    campaigns = loop.client.get("/campaigns", params={"run_id": run_id}).json()["campaigns"]
    assert len(campaigns) == 1
    campaign = campaigns[0]
    assert campaign["status"] == "live" and campaign["outcome_window_days"] == OUTCOME_WINDOW_DAYS
    assert loop.alerts(AlertKind.TREAT_LIST_READY)
    # The next day there is nothing new to list.
    loop.at(month + timedelta(days=1), 6)
    assert loop.firings("treat_list")[0]["result_code"] == "NOTHING_NEW"

    # 3. The campaign goes out; the CRM records what happened.
    start = datetime.fromisoformat(campaign["treatment_start"])
    customers = _the_crm_records_outcomes(loop, run_id, start)

    # 4. Measuring waits for the window, then pulls only the window's rows and measures.
    loop.at(month + timedelta(days=2), 7)
    waiting = loop.firings("measure")[0]
    assert (waiting["status"], waiting["result_code"]) == ("succeeded", "CAMPAIGN_NOT_MATURED"), waiting
    closes = (start + timedelta(days=OUTCOME_WINDOW_DAYS)).date() + timedelta(days=1)
    loop.at(closes, 7)
    measured = loop.firings("measure")[0]
    assert (measured["status"], measured["result_code"]) == ("succeeded", "CAMPAIGN_MEASURED"), measured
    view = loop.client.get(f"/campaigns/{campaign['campaign_id']}").json()
    assert view["campaign"]["status"] == "measured"
    assert view["campaign"]["outcomes"]["rows"] == customers, "the window only: no decoy row"
    assert view["report"]["treated_rows"] > 0 and view["report"]["control_rows"] > 0
    assert loop.alerts(AlertKind.CAMPAIGN_MEASURED)

    # 5. Learning, at 08:00 the same day: a challenger, never the model in use.
    loop.at(closes, 8)
    learning = loop.firings("learn")[0]
    assert (learning["status"], learning["result_code"]) == ("running", "LEARNING_STARTED"), learning
    loop.at(closes + timedelta(days=1), 2)  # the next tick settles it
    learned = loop.firings("learn")[0]
    assert (learned["status"], learned["result_code"]) == ("succeeded", "MODEL_PENDING_APPROVAL"), learned
    learned_run = loop.storage.read_model(run_key(learned["run_id"], "run.json"), RunRecord)
    assert learned_run.requested_by == SYSTEM_SCHEDULER.user_id
    challenger = loop.registry.get(learned_run.model_version_id or "")
    assert challenger.status is ModelStatus.PENDING_APPROVAL
    champion = loop.registry.get_champion(USE_CASE)
    assert champion is not None and champion.model_id == loop.champion_id, "nothing changed without a person"
    assert loop.storage.exists(run_key(learned["run_id"], "learned_from.json"))
    assert loop.alerts(AlertKind.CHALLENGER_WAITING)
    # A second learn firing finds nothing new to learn from.
    loop.at(closes + timedelta(days=1), 8)
    assert loop.firings("learn")[0]["result_code"] == "NOTHING_TO_LEARN"

    # Every firing was the scheduler's; nothing was written to the client's systems.
    for kind in loop.schedules:
        assert {f["trigger"] for f in loop.firings(kind)} == {"scheduled"}, kind
    assert {o["Key"]: o["ETag"] for o in loop.s3.list_objects_v2(Bucket=BUCKET)["Contents"]} == bucket_before
    for sql in loop.server.executed:
        assert sql.upper().startswith(("SET ", "SELECT ")), sql
    assert loop.server.read_only

    # 6. The one thing left to a person: approving the challenger.
    approved = loop.client.post(f"/models/{challenger.model_id}/approve", json={"approved_by": "asha"})
    assert approved.status_code == 200, approved.text
    now_in_use = loop.registry.get_champion(USE_CASE)
    assert now_in_use is not None and now_in_use.model_id == challenger.model_id
