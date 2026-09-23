"""Shared builders for the M49 scheduling tests: a client with real tables, a recipe, a champion.

The dataset is built by the real `build_dataset`, from a client store holding real sources and
mappings, on the tiny generated tables `tests/integration/test_runs_from_dataset.py` uses - so a
scheduled firing here goes through the same onboarding code a person's build does. The champion is
"registered" without training: its `schema.json` and `drift_baseline.json` are the register stage's
own functions applied to the built dataset, which is all a scoring *validation* and a drift check
read. Nothing here fits a model; the one test that does is marked slow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from engine.audit.events import AuditEvent, AuditQuery
from engine.clients import LocalClientStore
from engine.config import load_use_case, resolve_config
from engine.contracts import ModelStatus, ModelVersion, RunState
from engine.jobs import JobInfo
from engine.onboarding.mapping import suggested_mapping_spec
from engine.onboarding.sources import FileSourceReader
from engine.onboarding.specs import (
    ClientRecord,
    FeatureDef,
    FeatureSpec,
    LabelDefinition,
    LabelType,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    SourceSpec,
)
from engine.platform_db import sqlite_engine
from engine.registry import LocalModelRegistry
from engine.scheduling.alerts import AlertStore, LogAlertSink
from engine.scheduling.firing import FiringServices
from engine.scheduling.schedules import Schedule, ScheduleKind, ScheduleParameters, SqlScheduleStore
from engine.stages.ingest import read_upload
from engine.stages.register import drift_baseline, feature_schema
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now

USE_CASE = "telco-churn"
ENTITIES = 1200
START = date(2025, 6, 1)
END = date(2026, 6, 1)
SNAPSHOT = date(2026, 3, 1)
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


class FakeClock:
    """A clock a test moves by hand."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


class MemoryAudit:
    """An `AuditLog` that keeps events in a list (M47's own store is another agent's; not used here)."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        del query
        return tuple(self.events)

    def count(self, query: AuditQuery) -> int:
        del query
        return len(self.events)


class RecordingJobs:
    """A `JobRunner` that records what was submitted and runs nothing: the job is the pipeline's."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, job_id: str, fn: Any) -> JobInfo:
        del fn
        self.submitted.append(job_id)
        return JobInfo(job_id=job_id, state=RunState.PENDING, submitted_at=utc_now())

    def status(self, job_id: str) -> JobInfo:
        raise KeyError(job_id)

    def cancel(self, job_id: str) -> bool:
        del job_id
        return False

    def shutdown(self, *, wait: bool = True) -> None:
        del wait


class MemoryFlags:
    """`RetrainFlags` over a set, recording what was cleared."""

    def __init__(self, *flagged: str) -> None:
        self.open: set[str] = set(flagged)
        self.cleared: list[str] = []

    def flagged(self) -> tuple[str, ...]:
        return tuple(sorted(self.open))

    def clear(self, model_id: str) -> int:
        if model_id in self.open:
            self.open.discard(model_id)
            self.cleared.append(model_id)
            return 1
        return 0


def tables(seed: int = 20260922, *, entities: int = ENTITIES, end: date = END) -> dict[str, pd.DataFrame]:
    """One entity table and one activity log whose label is genuinely uncertain given the features."""
    import numpy as np

    rng = np.random.default_rng(seed)
    customers = pd.DataFrame(
        {
            "CUST_ID": [f"C{i:06d}" for i in range(entities)],
            "TNR_MNTHS": [12 + (i * 7) % 96 for i in range(entities)],
            "Plan Type": ["Pre" if i % 3 else "Post" for i in range(entities)],
            "REGION": [("North", "South", "East", "West")[i % 4] for i in range(entities)],
        }
    )
    means = rng.uniform(4.0, 70.0, size=entities)
    rows: list[dict[str, object]] = []
    for i in range(entities):
        day = START + timedelta(days=int(rng.integers(0, 14)))
        while day <= end:
            rows.append({"CUST_ID": f"C{i:06d}", "EVENT_DT": day.isoformat(), "EVENT_TYPE": "use"})
            day += timedelta(days=max(1, int(rng.exponential(means[i]))))
    return {"customers": customers, "activity": pd.DataFrame(rows)}


@dataclass
class World:
    """A data directory with a client, its sources, mappings and a recipe - and the firing services."""

    root: Path
    storage: LocalStorage
    client_store: LocalClientStore
    registry: LocalModelRegistry
    store: SqlScheduleStore
    alerts: LogAlertSink
    audit: MemoryAudit
    jobs: RecordingJobs
    clock: FakeClock
    client_id: str
    spec: OnboardingSpec
    config_root: Path
    flags: MemoryFlags = field(default_factory=MemoryFlags)

    def services(self, **changes: Any) -> FiringServices:
        values: dict[str, Any] = {
            "store": self.store,
            "storage": self.storage,
            "registry": self.registry,
            "jobs": self.jobs,
            "config_root": self.config_root,
            "alerts": self.alerts,
            "audit_log": self.audit,
            "client_store": self.client_store,
            "retrain_flags": self.flags,
            "clock": self.clock,
        }
        values.update(changes)
        return FiringServices(**values)

    def add_source(self, name: str, frame: pd.DataFrame, *, role: str, created_at: datetime) -> SourceSpec:
        key = f"clients/{self.client_id}/sources/{name}/raw.csv"
        self.storage.write_bytes(key, frame.to_csv(index=False).encode())
        read = read_upload(self.storage, key, file_format="csv")
        source = SourceSpec(
            source_id=f"s_{name}",
            client_id=self.client_id,
            file_name=f"{name}.csv",
            storage_key=key,
            file_format="csv",
            role=role,
            rows=read.row_count,
            columns=tuple(str(column) for column in read.frame.columns),
            fingerprint=read.fingerprint,
            created_at=created_at,
        )
        return self.client_store.add_source(self.client_id, source)

    def schedule(self, kind: ScheduleKind, **fields: Any) -> Schedule:
        values: dict[str, Any] = {
            "schedule_id": f"sch_{kind.value}",
            "client_id": self.client_id,
            "use_case_id": USE_CASE,
            "kind": kind,
            "cron": "0 2 * * *",
            "created_by": "u_test",
            "created_at": self.clock(),
            "updated_at": self.clock(),
        }
        if kind is ScheduleKind.SCORE and "parameters" not in fields:
            values["parameters"] = ScheduleParameters(onboarding_spec_id=self.spec.spec_id)
        values.update(fields)
        schedule = Schedule(**values)
        return self.store.create(
            schedule.model_copy(update={"next_due_at": schedule.next_slot_after(self.clock())})
        )


def make_world(root: Path, config_root: Path, *, seed: int = 20260922) -> World:
    """A client with two tables, their mappings and a single-snapshot training recipe."""
    storage = LocalStorage(root / "data")
    client_store = LocalClientStore(root / "data" / "clients.db")
    record: ClientRecord = client_store.create_client("Demo Telco", "telecom")
    engine = sqlite_engine(root / "data" / "platform.db")
    world = World(
        root=root,
        storage=storage,
        client_store=client_store,
        registry=LocalModelRegistry(root / "data" / "registry.db"),
        store=SqlScheduleStore(engine),
        alerts=LogAlertSink(AlertStore(engine)),
        audit=MemoryAudit(),
        jobs=RecordingJobs(),
        clock=FakeClock(),
        client_id=record.client_id,
        spec=None,  # type: ignore[arg-type]
        config_root=config_root,
    )
    config = load_use_case(USE_CASE)
    reader = FileSourceReader(storage, config)
    made = tables(seed)
    uploaded = datetime(2026, 6, 2, tzinfo=UTC)
    sources = (
        world.add_source("customers", made["customers"], role="entity", created_at=uploaded),
        world.add_source("activity", made["activity"], role="activity", created_at=uploaded),
    )
    mappings = []
    for source in sources:
        mapping = suggested_mapping_spec(
            reader.profile(source),
            config,
            role=source.role or "",
            use_case=USE_CASE,
            mapping_id=f"m_{source.source_id}",
        )
        mappings.append(client_store.save_mapping(mapping.with_hash()))
    spec = OnboardingSpec(
        spec_id="sp_sched",
        client_id=record.client_id,
        use_case=USE_CASE,
        entity_source_id="s_customers",
        event_source_ids=("s_activity",),
        mapping_ids=tuple(sorted(mapping.mapping_id for mapping in mappings)),
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
            mode=SnapshotMode.SINGLE, start=SNAPSHOT, end=SNAPSHOT, min_history_days=90, max_snapshots=1
        ),
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
    ).with_hash()
    world.spec = client_store.save_spec(spec)
    return world


def register_champion(
    world: World, frame: pd.DataFrame, *, model_number: int = 1, status: ModelStatus = ModelStatus.CHAMPION
) -> ModelVersion:
    """A registered version whose schema and drift baseline describe `frame` (no predictor is fitted)."""
    config = resolve_config(USE_CASE, root=world.config_root).config
    model_id = f"m_{USE_CASE}_{model_number}"
    train_run = f"r_20260601_{model_number:08x}"
    schema = feature_schema(
        frame, config, primary_key="entity_key", target="churn_next_60d", model_version_id=model_id
    )
    baseline = drift_baseline(
        frame, config, run_id=train_run, model_version_id=model_id, primary_key="entity_key"
    )
    world.storage.write_model(run_key(train_run, "schema.json"), schema)
    world.storage.write_model(run_key(train_run, "drift_baseline.json"), baseline)
    version = ModelVersion(
        model_id=model_id,
        use_case_id=USE_CASE,
        version=model_number,
        run_id=train_run,
        created_at=utc_now(),
        status=ModelStatus.CANDIDATE,
        metric=config.model_search.metric,
        metric_label=config.catalog.metric_label(config.model_search.metric),
        test_score=0.80,
        model_display_name="Test model",
        schema_key=run_key(train_run, "schema.json"),
        run_config_key=run_key(train_run, "run_config.json"),
        predictor_key=run_key(train_run, "model"),
        drift_baseline_key=run_key(train_run, "drift_baseline.json"),
        engine_version="test",
        autogluon_version="test",
    )
    world.registry.register(version)
    if status is ModelStatus.CHAMPION:
        return world.registry.promote(model_id, by="tests", note="test champion")
    return world.registry.get(model_id)
