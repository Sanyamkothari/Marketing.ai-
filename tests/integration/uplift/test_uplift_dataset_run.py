"""Plan J M106: an uplift training run can read a built dataset, not only an upload (DEC-1316).

`POST /uplift/runs` took only `upload_id`, so a campaign-effect model could not be retrained from the
data the onboarding pipeline builds from a client's own tables. Now the request names exactly one of
`upload_id` and `dataset_id`; a dataset run reads the dataset's manifest and rows, records the dataset,
its client and its fingerprint on `run.json` (as `POST /runs` does), and seeds its randomness check
from the dataset id, so the request and the run agree.

The dataset is built by the product's own `build_dataset` from two tiny tables: customers (with the
group each was randomly put in by a past campaign, mapped as a standard column `treatment` that a
copy of the configuration declares - a client's own schema extension) and their visits. The planted
effect: frequent visitors come back more often when contacted.

Fails on b5ea557, where `dataset_id` is an unknown field and `upload_id` is required.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import LabelDefinition, LabelType, ResolvedConfig, RunMode, load_use_case
from engine.contracts import RunRecord
from engine.onboarding.build import build_dataset
from engine.onboarding.datasets import LocalDatasetRegistry
from engine.onboarding.mapping import suggested_mapping_spec
from engine.onboarding.sources import FileSourceReader
from engine.onboarding.specs import (
    FeatureDef,
    FeatureSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    SourceSpec,
)
from engine.stages.ingest import read_upload
from engine.storage import LocalStorage, run_key
from engine.uplift.checks import run_uplift_checks
from engine.uplift.contracts import UpliftValidationReport
from engine.uplift.flow import check_seed
from engine.utils.time import utc_now
from tests.integration.uplift.test_uplift_api import FAST_OVERRIDES, App, finish, pinned_run_id

pytestmark = pytest.mark.integration

USE_CASE: Final[str] = "win-back-campaign"
CLIENT: Final[str] = "c_learn"
CUSTOMERS: Final[int] = 4_000
SNAPSHOT: Final[date] = date(2026, 3, 1)
TARGET: Final[str] = "reactivated_90d"
RUN_ID: Final[str] = "r_20261009_6b000001"


def _root(config_root: Path, target: Path) -> Path:
    """`configs/` with one more standard column on win-back: the campaign group a customer was put in."""
    shutil.copytree(config_root, target)
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["standard_schema"]["columns"].append(
        {
            "name": "treatment",
            "type": "categorical",
            "aliases": ["campaign_group"],
            "description": "1 when a past campaign contacted the customer, 0 when it held them back at random.",
        }
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def _tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(20261009)
    visits = rng.poisson(4.0, CUSTOMERS)
    treated = (rng.random(CUSTOMERS) < 0.5).astype(int)
    customers = pd.DataFrame(
        {
            "customer_id": [f"C{i:06d}" for i in range(CUSTOMERS)],
            "signup_date": "2024-01-01",
            "marketing_opt_in": "true",
            "treatment": treated,
        }
    )
    rows: list[dict[str, str]] = []
    for i in range(CUSTOMERS):
        customer = f"C{i:06d}"
        for _ in range(int(visits[i])):
            day = SNAPSHOT - timedelta(days=int(rng.integers(1, 80)))
            rows.append({"customer_id": customer, "event_time": day.isoformat(), "event_type": "visit"})
        chance = 0.05 + (0.25 if treated[i] and visits[i] >= 5 else 0.0)
        if rng.random() < chance:
            day = SNAPSHOT + timedelta(days=int(rng.integers(1, 80)))
            rows.append({"customer_id": customer, "event_time": day.isoformat(), "event_type": "visit"})
    # The extract runs past the snapshot's 90-day window, so every outcome is complete.
    rows.append({"customer_id": "C000000", "event_time": "2026-07-01", "event_type": "visit"})
    return customers, pd.DataFrame(rows)


def _build(root: Path, storage: LocalStorage) -> str:
    config = load_use_case(USE_CASE, root=root)
    customers, activity = _tables()
    sources: list[SourceSpec] = []
    for name, frame, role in (("customers", customers, "entity"), ("activity", activity, "activity")):
        key = f"clients/{CLIENT}/sources/{name}.csv"
        storage.write_bytes(key, frame.to_csv(index=False).encode())
        read = read_upload(storage, key, file_format="csv")
        sources.append(
            SourceSpec(
                source_id=f"s_{name}",
                client_id=CLIENT,
                file_name=f"{name}.csv",
                storage_key=key,
                file_format="csv",
                role=role,
                rows=read.row_count,
                columns=tuple(str(column) for column in read.frame.columns),
                fingerprint=read.fingerprint,
                created_at=utc_now(),
            )
        )
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
    assert "treatment" in {column.standard for column in mappings[0].columns}
    spec = OnboardingSpec(
        spec_id="sp_learn",
        client_id=CLIENT,
        use_case=USE_CASE,
        entity_source_id="s_customers",
        event_source_ids=("s_activity",),
        mapping_ids=tuple(sorted(mapping.mapping_id for mapping in mappings)),
        feature_spec=FeatureSpec(
            features=(FeatureDef(name="visits_80d", role="activity", function="count", window_days=80),)
        ),
        label_spec=LabelDefinition(
            name=TARGET, type=LabelType.EVENT_PRESENCE, role="activity", horizon_days=90
        ),
        snapshot_spec=SnapshotDefinition(
            mode=SnapshotMode.SINGLE, start=SNAPSHOT, end=SNAPSHOT, min_history_days=60, max_snapshots=1
        ),
        created_at=datetime(2026, 7, 2, tzinfo=UTC),
    ).with_hash()
    registry = LocalDatasetRegistry(storage)
    dataset_id = registry.new_dataset_id(CLIENT, USE_CASE)
    report = build_dataset(
        spec=spec,
        config=config,
        sources=tuple(sources),
        mappings=mappings,
        reader=reader,
        registry=registry,
        dataset_id=dataset_id,
        mode=RunMode.TRAIN,
    )
    assert report.passed, [
        (check.code, check.message) for check in report.checks if check.severity.value == "error"
    ]
    return dataset_id


@dataclass(frozen=True)
class World:
    app: App
    dataset_id: str


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    base = tmp_path_factory.mktemp("uplift-dataset")
    root = _root(config_root, base / "configs")
    data_dir = base / "data"
    data_dir.mkdir()
    dataset_id = _build(root, LocalStorage(data_dir))
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
        with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
            yield World(App(client=client, data_dir=data_dir), dataset_id)


def _body(**fields: object) -> dict[str, object]:
    return {
        "use_case": USE_CASE,
        "primary_key": "entity_key",
        "target": TARGET,
        "treatment_column": "treatment",
        "overrides": FAST_OVERRIDES,
        **fields,
    }


def test_a_dataset_backed_uplift_run_trains_and_names_its_dataset(world: World) -> None:
    with pinned_run_id(RUN_ID):
        response = world.app.client.post("/uplift/runs", json=_body(dataset_id=world.dataset_id))
    assert response.status_code == 202, response.text
    record = finish(world.app, RUN_ID)
    assert record.problem_type.value == "uplift" and record.model_version_id is not None
    assert record.upload_id is None
    assert record.dataset_id == world.dataset_id
    assert record.client_id == CLIENT
    assert record.dataset_fingerprint
    stored = world.app.storage.read_model(run_key(RUN_ID, "run.json"), RunRecord)
    assert stored.dataset_id == world.dataset_id
    checked = world.app.storage.read_model(run_key(RUN_ID, "uplift_validation.json"), UpliftValidationReport)
    assert checked.passed and checked.causal and checked.upload_id == world.dataset_id
    # The request's check and the run's own are seeded alike, from the dataset id: the same check on the
    # dataset's rows, seeded from its id, gives the run's randomness figure exactly.
    resolved = world.app.storage.read_model(run_key(RUN_ID, "run_config.json"), ResolvedConfig)
    again = run_uplift_checks(
        LocalDatasetRegistry(world.app.storage).read_frame(world.dataset_id),
        resolved.config,
        primary_key="entity_key",
        target=TARGET,
        upload_id=world.dataset_id,
        acknowledged=resolved.config.validation.acknowledged,
        seed=check_seed(world.dataset_id),
    )
    assert checked.randomness_auc is not None and again.report.randomness_auc == checked.randomness_auc
    # A dataset is never written to: its checks travel on the run.
    assert not any(
        path.name.startswith("upload")
        for path in (world.app.data_dir / "datasets" / world.dataset_id).iterdir()
    )


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"upload_id": "up_unknown", "dataset_id": "ds_unknown"}, id="both"),
        pytest.param({}, id="neither"),
    ],
)
def test_naming_both_sources_or_neither_is_422_with_the_reason(world: World, fields: dict[str, str]) -> None:
    response = world.app.client.post("/uplift/runs", json=_body(**fields))
    assert response.status_code == 422, response.text
    assert "exactly one of upload_id and dataset_id" in response.text
