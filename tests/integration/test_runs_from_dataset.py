"""`POST /runs` against a built dataset: plan section 6.5, change 4.

Phase 1 runs read an upload. A dataset is the other thing a run can read, and the whole point of
Phase 2 is that it produces one. What is worth testing here is not that a run starts - it is that a
run which started from a dataset can still be *traced back to it*: `run.json` has to name the
dataset, the client and the fingerprint, or the lineage the build spent all that effort recording
stops at the dataset and never reaches the model trained on it.

The refusals matter as much as the acceptance. A dataset that does not exist, one built for another
use case, and one whose build stopped before it wrote any rows are three different problems with
three different answers, and none of them may be "202 Accepted". A dataset keyed by more than one
column - a periodic one - is accepted since Plan A M34 (DEC-083).

The dataset is built by the same `build_dataset` the flow test exercises, on a handful of tiny
in-memory tables - this is about the boundary between the two phases, not about the build.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import RunMode, load_use_case
from engine.contracts import RunRecord
from engine.onboarding.build import build_dataset
from engine.onboarding.datasets import DATASET_FRAME_FILENAME, LocalDatasetRegistry, dataset_key
from engine.onboarding.mapping import suggested_mapping_spec
from engine.onboarding.sources import FileSourceReader
from engine.onboarding.specs import (
    DecidedBy,
    FeatureDef,
    FeatureSpec,
    LabelDefinition,
    LabelType,
    MappingColumn,
    MappingSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    SourceSpec,
)
from engine.stages.ingest import read_upload
from engine.storage import LocalStorage
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.onboarding.specs import BuildReport

USE_CASE = "telco-churn"
CLIENT = "c_demo"
ENTITIES = 1200
START = date(2025, 6, 1)
END = date(2026, 6, 1)
SNAPSHOT = date(2026, 3, 1)
"""Ninety days before the extract ends, so the 60-day outcome window is complete and nothing is
censored. A snapshot at the end of the data is dropped, and a build with no rows is a different
test."""


def _tables() -> dict[str, pd.DataFrame]:
    """One entity table and one activity log, generated from a per-entity rate.

    The rate matters. If every churner's activity stopped on a fixed day, "no activity in the next
    60 days" would be a near-function of the last 30 days of activity, and the Phase 1 leakage check
    would refuse the dataset - correctly. A rate leaves the label genuinely uncertain given the
    features, which is the only kind of data a run can be demonstrated on.
    """
    import numpy as np

    rng = np.random.default_rng(20260922)
    customers = pd.DataFrame(
        {
            "CUST_ID": [f"C{i:06d}" for i in range(ENTITIES)],
            "TNR_MNTHS": [12 + (i * 7) % 96 for i in range(ENTITIES)],
            "Plan Type": ["Pre" if i % 3 else "Post" for i in range(ENTITIES)],
            "REGION": [("North", "South", "East", "West")[i % 4] for i in range(ENTITIES)],
        }
    )
    # Each customer has a mean gap between events; the gaps themselves are drawn, so how long a
    # customer happens to be silent after the snapshot is not a function of how often they acted
    # before it. That is what keeps the label uncertain given the features, and it is why the rate
    # spans 4 to 70 days: at the quiet end a 60-day silence is likely but never certain.
    means = rng.uniform(4.0, 70.0, size=ENTITIES)
    rows: list[dict[str, object]] = []
    for i in range(ENTITIES):
        day = START + timedelta(days=int(rng.integers(0, 14)))
        while day <= END:
            rows.append({"CUST_ID": f"C{i:06d}", "EVENT_DT": day.isoformat(), "EVENT_TYPE": "use"})
            day += timedelta(days=max(1, int(rng.exponential(means[i]))))
    return {"customers": customers, "activity": pd.DataFrame(rows)}


def _sources(storage: LocalStorage, tables: dict[str, pd.DataFrame], root: Path) -> tuple[SourceSpec, ...]:
    roles = {"customers": "entity", "activity": "activity"}
    specs: list[SourceSpec] = []
    for name, frame in tables.items():
        path = root / f"{name}.csv"
        frame.to_csv(path, index=False)
        key = f"clients/{CLIENT}/sources/{name}.csv"
        storage.write_bytes(key, path.read_bytes())
        read = read_upload(storage, key, file_format="csv")
        specs.append(
            SourceSpec(
                source_id=f"s_{name}",
                client_id=CLIENT,
                file_name=f"{name}.csv",
                storage_key=key,
                file_format="csv",
                role=roles[name],
                rows=read.row_count,
                columns=tuple(str(column) for column in read.frame.columns),
                fingerprint=read.fingerprint,
                created_at=utc_now(),
            )
        )
    return tuple(specs)


def _spec(mappings: tuple[MappingSpec, ...], *, mode: SnapshotMode) -> OnboardingSpec:
    return OnboardingSpec(
        spec_id="sp_runs",
        client_id=CLIENT,
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
        snapshot_spec=(
            SnapshotDefinition(mode=mode, start=SNAPSHOT, end=SNAPSHOT, min_history_days=90, max_snapshots=1)
            if mode is SnapshotMode.SINGLE
            # Periodic snapshots stand on month-ends, so a range has to contain some: a start and
            # an end on the same day contains none and the build correctly produces nothing.
            else SnapshotDefinition(
                mode=mode,
                start=date(2025, 12, 1),
                end=date(2026, 3, 31),
                min_history_days=90,
                max_snapshots=3,
            )
        ),
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
    ).with_hash()


def _build(root: Path, *, mode: SnapshotMode = SnapshotMode.SINGLE) -> tuple[LocalStorage, str, BuildReport]:
    config = load_use_case(USE_CASE)
    storage = LocalStorage(root / "data")
    sources = _sources(storage, _tables(), root)
    reader = FileSourceReader(storage, config)
    mappings = tuple(
        suggested_mapping_spec(
            reader.profile(source),
            config,
            role=source.role or "",
            use_case=USE_CASE,
            mapping_id=f"m_{source.source_id}",
        )
        for source in sources
    )
    registry = LocalDatasetRegistry(storage)
    dataset_id = registry.new_dataset_id(CLIENT, USE_CASE)
    report = build_dataset(
        spec=_spec(mappings, mode=mode),
        config=config,
        sources=sources,
        mappings=mappings,
        reader=reader,
        registry=registry,
        dataset_id=dataset_id,
        mode=RunMode.TRAIN,
    )
    return storage, dataset_id, report


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    root = tmp_path_factory.mktemp("runs-from-dataset")
    _, dataset_id, report = _build(root)
    assert report.passed, [
        (check.code, check.message) for check in report.checks if check.severity.value == "error"
    ]
    return root, dataset_id


@pytest.fixture
def client(built: tuple[Path, str]) -> TestClient:
    root, _ = built
    return TestClient(create_app(data_dir=root / "data"))


def _post(client: TestClient, **body: object) -> object:
    return client.post("/runs", json={"use_case": USE_CASE, "mode": "train", **body})


# ---------------------------------------------------------------------------
# The run starts, and stays traceable to the data it read
# ---------------------------------------------------------------------------
def test_a_run_from_a_dataset_is_accepted_without_an_upload(client: TestClient, built) -> None:
    _, dataset_id = built
    response = _post(client, dataset_id=dataset_id)
    assert response.status_code == 202, response.text
    assert response.headers["Location"].startswith("/runs/")


def test_the_run_record_names_the_dataset_the_client_and_the_fingerprint(client: TestClient, built) -> None:
    """Without these three, the lineage the build recorded stops at the dataset."""
    root, dataset_id = built
    run_id = _post(client, dataset_id=dataset_id).json()["run_id"]
    record = RunRecord.model_validate_json((root / "data" / "runs" / run_id / "run.json").read_text())
    assert record.dataset_id == dataset_id
    assert record.client_id == CLIENT
    assert record.dataset_fingerprint
    assert record.upload_id is None


def test_the_key_and_the_target_come_from_the_manifest_when_the_request_omits_them(
    client: TestClient, built
) -> None:
    """The dataset already knows what a row is and what the outcome is called; asking again is noise."""
    root, dataset_id = built
    run_id = _post(client, dataset_id=dataset_id).json()["run_id"]
    record = RunRecord.model_validate_json((root / "data" / "runs" / run_id / "run.json").read_text())
    assert record.primary_key == "entity_key"
    assert record.target == "churn_next_60d"


def test_a_dataset_run_leaves_no_validation_report_on_any_upload(client: TestClient, built) -> None:
    """A dataset is immutable and several runs may read one, so its checks travel on the run."""
    root, dataset_id = built
    _post(client, dataset_id=dataset_id)
    assert not list((root / "data" / "uploads").glob("*")) if (root / "data" / "uploads").exists() else True


# ---------------------------------------------------------------------------
# The four refusals
# ---------------------------------------------------------------------------
def test_an_unknown_dataset_is_404_and_says_what_to_do(client: TestClient) -> None:
    response = _post(client, dataset_id="ds_nothing")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "DATASET_NOT_FOUND"
    assert "build one" in detail["message"].lower()


def test_a_dataset_built_for_another_use_case_is_409(client: TestClient, built) -> None:
    _, dataset_id = built
    response = client.post(
        "/runs", json={"use_case": "payment-propensity", "mode": "train", "dataset_id": dataset_id}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DATASET_USE_CASE_MISMATCH"


def test_a_dataset_whose_build_wrote_no_rows_is_409_not_404(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A blocked build writes its report and no data. That is a different answer from "no such id".

    This one builds its own dataset rather than borrowing the module's: it removes the frame to
    reach the state a blocked build leaves, and doing that to a shared fixture would make every
    test that ran after it fail for a reason that has nothing to do with what it was testing.
    """
    root = tmp_path_factory.mktemp("runs-unbuilt")
    _, dataset_id, _ = _build(root)
    (root / "data" / dataset_key(dataset_id, DATASET_FRAME_FILENAME)).unlink()
    client = TestClient(create_app(data_dir=root / "data"))
    response = client.post("/runs", json={"use_case": USE_CASE, "mode": "train", "dataset_id": dataset_id})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "DATASET_NOT_BUILT"
    assert "build report" in detail["message"].lower()


def test_a_client_id_that_does_not_own_the_dataset_is_409(client: TestClient, built) -> None:
    _, dataset_id = built
    response = _post(client, dataset_id=dataset_id, client_id="c_someone_else")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DATASET_CLIENT_MISMATCH"


def test_a_periodic_dataset_runs_on_both_key_columns_and_splits_by_entity(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Plan A M34 (DEC-083) replaced the honest 501 this used to pin.

    A periodic dataset has one row per entity per snapshot date. The run is accepted, carries both
    key columns on `run.json` - never the first alone, which would join on the customer and lose the
    date - and its resolved configuration splits by entity, recorded as a choice the engine made.
    """
    from engine.config import ResolvedConfig

    root = tmp_path_factory.mktemp("runs-periodic")
    _, dataset_id, report = _build(root, mode=SnapshotMode.PERIODIC)
    assert report.passed, [
        (check.code, check.message) for check in report.checks if check.severity.value == "error"
    ]
    client = TestClient(create_app(data_dir=root / "data"))
    response = client.post("/runs", json={"use_case": USE_CASE, "mode": "train", "dataset_id": dataset_id})
    assert response.status_code == 202, response.text
    run_dir = root / "data" / "runs" / response.json()["run_id"]
    record = RunRecord.model_validate_json((run_dir / "run.json").read_text())
    assert record.primary_key == ["entity_key", "snapshot_date"]
    resolved = ResolvedConfig.model_validate_json((run_dir / "run_config.json").read_text())
    assert resolved.config.split.group_column == "entity_key"
    assert resolved.sources["split.group_column"] == "derived"


# ---------------------------------------------------------------------------
# The upload path is untouched
# ---------------------------------------------------------------------------
def test_naming_both_a_dataset_and_an_upload_is_refused_by_the_request_model(client: TestClient) -> None:
    response = _post(client, dataset_id="ds_x", upload_id="up_x", primary_key="customer_id")
    assert response.status_code == 422


def test_naming_neither_is_refused_by_the_request_model(client: TestClient) -> None:
    assert _post(client).status_code == 422


def test_an_unmapped_mapping_column_never_reaches_the_dataset(built) -> None:
    """A sanity check on the build this module leans on, so a failure here is not mistaken for a run bug."""
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    frame = LocalDatasetRegistry(storage).read_frame(dataset_id)
    assert "CUST_ID" not in frame.columns
    assert "entity_key" in frame.columns


def test_the_mapping_columns_are_the_ones_the_suggester_decided(built) -> None:
    """Guards the fixture: if suggestion changes, these tests should fail loudly, not silently pass."""
    root, _ = built
    config = load_use_case(USE_CASE)
    storage = LocalStorage(root / "data")
    reader = FileSourceReader(storage, config)
    sources = _sources(storage, _tables(), root)
    mapping = suggested_mapping_spec(
        reader.profile(sources[0]), config, role="entity", use_case=USE_CASE, mapping_id="m_check"
    )
    assert "entity_key" in mapping.standard_names
    assert all(column.decided_by in {DecidedBy.AUTO, DecidedBy.USER} for column in mapping.columns)
    assert MappingColumn in {type(column) for column in mapping.columns} or mapping.columns
