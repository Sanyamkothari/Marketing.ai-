"""M13, end to end: four raw client tables become one dataset, or no dataset at all.

The tables below are what a client actually sends - a customer master with the client's own column
names, one row per invoice, one row per ticket, one row per login - and nothing in them has been
shaped to suit the engine. Everything the build has to get right is therefore observable here and
nowhere in the unit suites: that the snapshot spine and the features and the label agree about which
instant each row stands at, that a snapshot whose outcome window runs past the end of the extract is
dropped rather than labelled, and that a recipe that cannot be built leaves a report and no data.

The activity log is deliberately generated from a per-entity rate rather than from a churn date.
Stopping every churner's activity on a fixed day would make "no activity in the next 60 days" a
function of the last 30 days of activity, the built dataset would carry a feature that predicts the
label almost perfectly, and the Phase 1 leakage check would - correctly - refuse it. A rate makes the
label genuinely uncertain given the features, which is the only kind of data this flow can be
demonstrated on.

No model is trained here. Phase 1's own integration tests do that, on a file of this exact shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest

from engine.config import RunMode, StandardType, UseCaseConfig, load_use_case
from engine.contracts import Severity
from engine.onboarding import features as feature_engine
from engine.onboarding.build import build_dataset
from engine.onboarding.datasets import (
    DATASET_FEATURES_SQL_FILENAME,
    DATASET_FRAME_FILENAME,
    DatasetError,
    LocalDatasetRegistry,
    dataset_key,
)
from engine.onboarding.sources import FileSourceReader
from engine.onboarding.specs import (
    AggFunction,
    BuildReport,
    ColumnTransform,
    DecidedBy,
    FeatureDef,
    FeatureSpec,
    LabelSpec,
    LabelType,
    MappingColumn,
    MappingSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotFrequency,
    SnapshotMode,
    SourceSpec,
    SubAggregation,
    TransformKind,
    WhereClause,
    WhereOp,
)
from engine.stages.ingest import dataset_fingerprint
from engine.storage import LocalStorage

if TYPE_CHECKING:
    from engine.onboarding.specs import DatasetManifest

pytestmark = [pytest.mark.integration]

USE_CASE = "telco-churn"
CLIENT = "cl_flow"

ENTITIES = 500
DATA_START = date(2023, 1, 1)
DATA_END = date(2025, 2, 28)
ACTIVITY_STEP_DAYS = 5
QUIET_SHARE = 0.45
QUIET_RATE = 0.06
BUSY_RATE = 0.50
HORIZON_DAYS = 60

#: The four dates `plan_snapshots` lands on for the spec below; the last has no complete outcome
#: window (its horizon runs past the last activity row) and is expected to be dropped.
EXPECTED_DATES = (date(2024, 9, 30), date(2024, 10, 31), date(2024, 11, 30), date(2024, 12, 31))
CENSORED_DATE = date(2024, 12, 31)


# ---------------------------------------------------------------------------
# The client's raw tables
# ---------------------------------------------------------------------------
def _keys() -> list[str]:
    return [f"C{index:04d}" for index in range(ENTITIES)]


def _month_starts(first: date, last: date) -> list[date]:
    months: list[date] = []
    year, month = first.year, first.month
    while date(year, month, 1) <= last:
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _customers(rng: np.random.Generator) -> pd.DataFrame:
    keys = _keys()
    signups = [date(2021, 6, 1) + timedelta(days=int(offset)) for offset in rng.integers(0, 540, ENTITIES)]
    return pd.DataFrame(
        {
            "Cust ID": keys,
            "SIGNUP_DT": [day.isoformat() for day in signups],
            "Plan Name": rng.choice(["prepaid", "postpaid"], ENTITIES),
            "Circle": rng.choice(["North", "South", "East", "West"], ENTITIES),
            "MRC": np.round(rng.uniform(200, 1500, ENTITIES), 2),
            "DND Flag": rng.choice(["Y", "N"], ENTITIES),
            "Sales Rep": rng.choice(["ravi", "anita", "joy"], ENTITIES),
        }
    )


def _bills(rng: np.random.Generator, charges: pd.Series[float]) -> pd.DataFrame:
    months = _month_starts(DATA_START, DATA_END)
    keys = _keys()
    rows = {
        "Cust ID": np.repeat(keys, len(months)),
        "Bill Date": [day.isoformat() for _ in keys for day in months],
        "Amount": np.round(
            np.repeat(charges.to_numpy(), len(months)) * rng.uniform(0.8, 1.2, ENTITIES * len(months)), 2
        ),
    }
    return pd.DataFrame(rows)


def _complaints(rng: np.random.Generator) -> pd.DataFrame:
    months = _month_starts(DATA_START, DATA_END)
    raised: list[str] = []
    keys: list[str] = []
    for key in _keys():
        for day in months:
            if rng.random() < 0.10:
                keys.append(key)
                raised.append((day + timedelta(days=9)).isoformat())
    resolved = [
        None if rng.random() < 0.4 else (date.fromisoformat(day) + timedelta(days=3)).isoformat()
        for day in raised
    ]
    return pd.DataFrame(
        {
            "Cust ID": keys,
            "Ticket Date": raised,
            "Resolved On": resolved,
            "Category": rng.choice(["network", "billing", "service"], len(keys)),
        }
    )


def _activity(rng: np.random.Generator) -> pd.DataFrame:
    grid = [
        DATA_START + timedelta(days=step)
        for step in range(0, (DATA_END - DATA_START).days + 1, ACTIVITY_STEP_DAYS)
    ]
    rates = np.where(rng.random(ENTITIES) < QUIET_SHARE, QUIET_RATE, BUSY_RATE)
    hits = rng.random((ENTITIES, len(grid))) < rates[:, None]
    rows, columns = np.nonzero(hits)
    keys = np.array(_keys())
    return pd.DataFrame(
        {
            "Cust ID": keys[rows],
            "Event Date": [grid[column].isoformat() for column in columns],
            "Event Type": rng.choice(["login", "recharge", "call"], len(rows)),
        }
    )


# ---------------------------------------------------------------------------
# The recipe
# ---------------------------------------------------------------------------
def _cast(to: StandardType) -> ColumnTransform:
    return ColumnTransform(kind=TransformKind.CAST, to_type=to)


def _columns(pairs: list[tuple[str, str, ColumnTransform | None]]) -> tuple[MappingColumn, ...]:
    return tuple(
        MappingColumn(
            source=source, standard=standard, transform=transform, confidence=1.0, decided_by=DecidedBy.USER
        )
        for source, standard, transform in pairs
    )


ENTITY_COLUMNS = _columns(
    [
        ("Cust ID", "entity_key", None),
        ("SIGNUP_DT", "signup_date", _cast(StandardType.DATE)),
        ("Plan Name", "plan_type", _cast(StandardType.CATEGORICAL)),
        ("Circle", "region", _cast(StandardType.CATEGORICAL)),
        ("MRC", "monthly_charges", _cast(StandardType.NUMERIC)),
        ("DND Flag", "marketing_opt_in", ColumnTransform(kind=TransformKind.NEGATE)),
    ]
)

MAPPINGS: dict[str, tuple[str, tuple[MappingColumn, ...]]] = {
    "src_customers": ("entity", ENTITY_COLUMNS),
    "src_bills": (
        "bills",
        _columns(
            [
                ("Cust ID", "entity_key", None),
                ("Bill Date", "event_time", _cast(StandardType.DATE)),
                ("Amount", "amount", _cast(StandardType.NUMERIC)),
            ]
        ),
    ),
    "src_complaints": (
        "complaints",
        _columns(
            [
                ("Cust ID", "entity_key", None),
                ("Ticket Date", "event_time", _cast(StandardType.DATE)),
                ("Resolved On", "resolved_time", _cast(StandardType.DATE)),
                ("Category", "category", _cast(StandardType.CATEGORICAL)),
            ]
        ),
    ),
    "src_activity": (
        "activity",
        _columns(
            [
                ("Cust ID", "entity_key", None),
                ("Event Date", "event_time", _cast(StandardType.DATE)),
                ("Event Type", "event_type", _cast(StandardType.CATEGORICAL)),
            ]
        ),
    ),
}

FEATURES = FeatureSpec(
    features=(
        FeatureDef(name="complaints_90d", role="complaints", function=AggFunction.COUNT, window_days=90),
        FeatureDef(
            name="complaints_open_30d",
            role="complaints",
            function=AggFunction.COUNT,
            window_days=30,
            where=WhereClause(column="resolved_time", op=WhereOp.IS_NULL),
        ),
        FeatureDef(
            name="avg_bill_6m",
            role="bills",
            function=AggFunction.MEAN,
            column="amount",
            window_days=180,
        ),
        FeatureDef(
            name="bill_trend_3m_vs_6m",
            role="bills",
            function=AggFunction.RATIO,
            of=SubAggregation(function=AggFunction.MEAN, column="amount", window_days=90),
            over=SubAggregation(function=AggFunction.MEAN, column="amount", window_days=180),
        ),
        FeatureDef(name="activity_30d", role="activity", function=AggFunction.COUNT, window_days=30),
    )
)

LABEL = LabelSpec(
    name="churn_next_60d",
    type=LabelType.EVENT_ABSENCE,
    role="activity",
    horizon_days=HORIZON_DAYS,
    description="No activity of any kind in the 60 days after the snapshot date.",
)

SNAPSHOTS = SnapshotDefinition(
    mode=SnapshotMode.PERIODIC,
    frequency=SnapshotFrequency.MONTHLY,
    end=date(2024, 12, 31),
    max_snapshots=4,
    min_history_days=90,
    inclusive_snapshot_time=True,
)


# ---------------------------------------------------------------------------
# Wiring: files on disk, a reader over them, a registry beside them
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Flow:
    """One executed build and everything needed to read what it wrote."""

    report: BuildReport
    registry: LocalDatasetRegistry
    dataset_id: str
    spec: OnboardingSpec
    sources: tuple[SourceSpec, ...]
    mappings: tuple[MappingSpec, ...]
    config: UseCaseConfig
    reader: FileSourceReader

    @property
    def manifest(self) -> DatasetManifest:
        return self.registry.read_manifest(self.dataset_id)

    @property
    def frame(self) -> pd.DataFrame:
        return self.registry.read_frame(self.dataset_id)


def _raw_tables() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(20240101)
    customers = _customers(rng)
    return {
        "src_customers": customers,
        "src_bills": _bills(rng, customers["MRC"]),
        "src_complaints": _complaints(rng),
        "src_activity": _activity(rng),
    }


def _source_specs(storage: LocalStorage, tables: dict[str, pd.DataFrame]) -> tuple[SourceSpec, ...]:
    specs: list[SourceSpec] = []
    for source_id, frame in tables.items():
        key = f"sources/{source_id}/raw.csv"
        storage.write_text(key, frame.to_csv(index=False))
        specs.append(
            SourceSpec(
                source_id=source_id,
                client_id=CLIENT,
                file_name=f"{source_id}.csv",
                storage_key=key,
                file_format="csv",
                role=MAPPINGS[source_id][0],
                rows=len(frame),
                columns=tuple(str(name) for name in frame.columns),
                fingerprint=dataset_fingerprint(frame),
                created_at=datetime.now(UTC),
            )
        )
    return tuple(specs)


def _mapping_specs(entity_columns: tuple[MappingColumn, ...]) -> tuple[MappingSpec, ...]:
    return tuple(
        MappingSpec(
            mapping_id=f"map_{source_id}",
            client_id=CLIENT,
            source_id=source_id,
            use_case=USE_CASE,
            role=role,
            columns=entity_columns if role == "entity" else columns,
            created_at=datetime.now(UTC),
        ).with_hash()
        for source_id, (role, columns) in MAPPINGS.items()
    )


def _onboarding_spec(mappings: tuple[MappingSpec, ...]) -> OnboardingSpec:
    return OnboardingSpec(
        spec_id="spec_flow",
        client_id=CLIENT,
        use_case=USE_CASE,
        entity_source_id="src_customers",
        event_source_ids=("src_activity", "src_bills", "src_complaints"),
        mapping_ids=tuple(sorted(mapping.mapping_id for mapping in mappings)),
        feature_spec=FEATURES,
        label_spec=LABEL,
        snapshot_spec=SNAPSHOTS,
        created_at=datetime.now(UTC),
    ).with_hash()


def _run(root: Path, *, entity_columns: tuple[MappingColumn, ...]) -> Flow:
    config = load_use_case(USE_CASE)
    storage = LocalStorage(root)
    sources = _source_specs(storage, _raw_tables())
    mappings = _mapping_specs(entity_columns)
    spec = _onboarding_spec(mappings)
    registry = LocalDatasetRegistry(storage)
    dataset_id = registry.new_dataset_id(CLIENT, USE_CASE)
    reader = FileSourceReader(storage, config)
    report = build_dataset(
        spec=spec,
        config=config,
        sources=sources,
        mappings=mappings,
        reader=reader,
        registry=registry,
        dataset_id=dataset_id,
        mode=RunMode.TRAIN,
    )
    return Flow(report, registry, dataset_id, spec, sources, mappings, config, reader)


@pytest.fixture(scope="module")
def flow(tmp_path_factory: pytest.TempPathFactory) -> Flow:
    return _run(tmp_path_factory.mktemp("onboarding-flow"), entity_columns=ENTITY_COLUMNS)


# ---------------------------------------------------------------------------
# 1. The dataset
# ---------------------------------------------------------------------------
def test_the_build_passed_and_wrote_one_row_per_entity_per_surviving_snapshot(flow: Flow) -> None:
    assert flow.report.passed, [
        (check.code, check.message) for check in flow.report.checks if check.severity is Severity.ERROR
    ]
    frame = flow.frame
    surviving = [day for day in EXPECTED_DATES if day != CENSORED_DATE]
    assert len(frame) == ENTITIES * len(surviving)
    assert not frame.duplicated(subset=["entity_key", "snapshot_date"]).any()
    assert sorted(pd.to_datetime(frame["snapshot_date"]).dt.date.unique()) == surviving


def test_the_dataset_carries_the_keys_the_attributes_the_features_and_the_label(flow: Flow) -> None:
    manifest = flow.manifest
    assert manifest.primary_key == ("entity_key", "snapshot_date")
    assert manifest.target == LABEL.name
    origins = {column.name: column.origin for column in manifest.columns}
    assert origins["entity_key"] == "key"
    assert origins["snapshot_date"] == "key"
    assert origins["plan_type"] == "mapped"
    # `tenure_months` is nobody's column: the use case says it can be worked out from the signup
    # date the client did map, and the assemble stage is what works it out.
    assert origins["tenure_months"] == "derived"
    assert set(FEATURES.names) <= set(origins)
    assert origins[LABEL.name] == "label"
    assert set(origins) == set(flow.frame.columns)


# ---------------------------------------------------------------------------
# 2. Lineage
# ---------------------------------------------------------------------------
def test_the_manifest_names_every_source_fingerprint_mapping_hash_and_spec_hash(flow: Flow) -> None:
    manifest = flow.manifest
    assert manifest.spec_hash == flow.spec.hash
    assert manifest.feature_spec_hash == FEATURES.hash
    assert manifest.source_fingerprints.keys() == {source.source_id for source in flow.sources}
    for source in flow.sources:
        assert manifest.source_fingerprints[source.source_id].hash == source.fingerprint.hash
    assert manifest.mapping_hashes == {mapping.mapping_id: mapping.hash for mapping in flow.mappings}


# ---------------------------------------------------------------------------
# 3. The outcome, date by date
# ---------------------------------------------------------------------------
def test_every_snapshot_reports_a_plausible_positive_rate(flow: Flow) -> None:
    stats = flow.report.snapshots
    assert tuple(stat.date for stat in stats) == EXPECTED_DATES
    for stat in stats:
        assert stat.entities == ENTITIES
        assert stat.positive_rate is not None
        assert 0.05 < stat.positive_rate < 0.60, stat


def test_the_snapshot_whose_outcome_window_is_incomplete_was_dropped(flow: Flow) -> None:
    censored = [stat for stat in flow.report.snapshots if stat.censored]
    assert [stat.date for stat in censored] == [CENSORED_DATE]
    assert censored[0].dropped_reason is not None
    assert CENSORED_DATE not in flow.manifest.snapshot_dates
    assert "LABEL_HORIZON_CENSORED" in {check.code for check in flow.report.checks}


# ---------------------------------------------------------------------------
# 4. What the build left behind for a human to read
# ---------------------------------------------------------------------------
def test_features_sql_was_written_and_every_query_carries_the_point_in_time_guard(flow: Flow) -> None:
    key = dataset_key(flow.dataset_id, DATASET_FEATURES_SQL_FILENAME)
    assert flow.report.features_sql_path == key
    sql = flow.registry.storage.read_text(key)
    for role in ("activity", "bills", "complaints"):
        assert f"-- role: {role}" in sql
    assert sql.count(feature_engine.POINT_IN_TIME_MARKER) >= 3


def test_the_status_document_names_one_stage_per_mapped_event_table(flow: Flow) -> None:
    status = flow.registry.read_status(flow.dataset_id)
    assert status.progress_pct == 100
    assert [stage.key for stage in status.stages] == [
        "apply_mappings",
        "snapshots",
        "features:activity",
        "features:bills",
        "features:complaints",
        "labels",
        "assemble",
        "validate",
        "write",
    ]


# ---------------------------------------------------------------------------
# 5. A recipe that cannot be built
# ---------------------------------------------------------------------------
def test_a_recipe_with_no_entity_key_reports_errors_and_writes_no_dataset(tmp_path: Path) -> None:
    broken = tuple(column for column in ENTITY_COLUMNS if column.standard != "entity_key")
    flow = _run(tmp_path, entity_columns=broken)

    assert not flow.report.passed
    assert flow.report.error_count > 0
    assert "ENTITY_KEY_UNMAPPED" in {check.code for check in flow.report.checks}
    assert flow.report.rows_out == 0

    assert not flow.registry.storage.exists(dataset_key(flow.dataset_id, DATASET_FRAME_FILENAME))
    with pytest.raises(DatasetError) as raised:
        flow.registry.read_manifest(flow.dataset_id)
    assert raised.value.code == "DATASET_NOT_FOUND"


def test_a_feature_builder_that_lost_its_time_bound_is_caught_and_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break the point-in-time bound, and the build must refuse its own output.

    Without this the passing build above proves nothing about the probe: a probe that compared
    nothing would report no leak just as convincingly. `engine.onboarding.features` will not emit a
    query without its guard, so the guard and the assertion that enforces it are removed together -
    which is precisely the shape of the future edit the probe exists to survive.
    """
    monkeypatch.setattr(
        feature_engine,
        "point_in_time_join_clause",
        lambda event, snapshot, *, inclusive: f"{event}.entity_key = {snapshot}.entity_key",
    )
    monkeypatch.setattr(feature_engine, "assert_point_in_time", lambda sql: None)

    flow = _run(tmp_path, entity_columns=ENTITY_COLUMNS)

    leaked = [check for check in flow.report.checks if check.code == "FUTURE_EVENTS_LEAKED"]
    assert leaked, [check.code for check in flow.report.checks]
    assert all(check.severity is Severity.ERROR for check in leaked)
    assert not flow.report.passed
    assert not flow.registry.storage.exists(dataset_key(flow.dataset_id, DATASET_FRAME_FILENAME))
