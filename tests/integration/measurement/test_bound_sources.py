"""Plan J M107 (DEC-1317): a recipe whose tables are bound to a saved connection reads the newest ones itself.

Acceptance: "a spec bound to a fake S3 prefix picks up the newest object with no upload". The client's
two tables live in S3 (moto) under `exports/customers/` and `exports/activity/`. They are added as
sources **from the connection** (no file is uploaded), a recipe is saved on them, and the champion is
registered as in `tests/unit/production/test_firing.py`. When a newer activity file lands under the
folder, the next scheduled scoring build fetches it before the recipe is re-pointed at its newest
tables (`latest_recipe_inputs`), so the dataset reads it. Nothing is written to the bucket, and no
upload is made. A firing without the connections service, and a source added by upload, behave exactly
as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from api.main import create_app
from engine.config import RunMode, get_roles, load_use_case
from engine.connections.store import ConnectionStore
from engine.measurement.campaign import InMemoryCampaignStore
from engine.measurement.cycle import CycleServices
from engine.measurement.pull import PullSelection
from engine.onboarding.datasets import dataset_key
from engine.onboarding.mapping import suggested_mapping_spec
from engine.onboarding.sources import FileSourceReader, add_connection_source
from engine.onboarding.specs import DatasetManifest, OnboardingSpec, SourceSpec
from engine.scheduling.firing import ScheduleFirer, build_dataset_from_spec
from engine.scheduling.schedules import FiringStatus, ScheduleKind, ScheduleParameters
from engine.settings import Settings
from engine.storage import run_key
from tests.unit.production.scheduling_support import USE_CASE, World, make_world, register_champion, tables

pytestmark = pytest.mark.integration

BUCKET = "client-exports"
JUNE = datetime(2026, 6, 2, tzinfo=UTC)


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _csv(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode()


def _objects(s3: Any) -> dict[str, str]:
    return {o["Key"]: o["ETag"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])}


class Bound:
    """A world whose recipe reads two tables bound to an S3 connection."""

    def __init__(
        self, world: World, connections: ConnectionStore, connection_id: str, spec: OnboardingSpec
    ) -> None:
        self.world = world
        self.connections = connections
        self.connection_id = connection_id
        self.spec = spec

    def cycle(self) -> CycleServices:
        return CycleServices(campaigns=InMemoryCampaignStore(), connections=self.connections)


@pytest.fixture
def bound(tmp_path: Path, config_root: Path, s3: Any) -> Bound:
    world = make_world(tmp_path, config_root)
    made = tables()
    s3.put_object(Bucket=BUCKET, Key="exports/customers/2026-06.csv", Body=_csv(made["customers"]))
    s3.put_object(Bucket=BUCKET, Key="exports/activity/2026-06.csv", Body=_csv(made["activity"]))
    connections = ConnectionStore(world.storage, Settings())
    record = connections.create(
        kind="s3",
        name="Client exports",
        config={"bucket": BUCKET, "region": "us-east-1", "prefix": "exports/"},
        secrets={"access_key_id": "testing", "secret_access_key": "testing"},
    )
    config = load_use_case(USE_CASE)
    roles = get_roles(config_root)
    sources: list[SourceSpec] = []
    for role, folder in (("entity", "exports/customers/"), ("activity", "exports/activity/")):
        source, _profile = add_connection_source(
            world.storage,
            world.client_store,
            connections,
            client_id=world.client_id,
            connection_id=record.connection_id,
            selection=PullSelection(prefix=folder),
            role=role,
            config=config,
            roles=roles,
            limit_bytes=200 * 1024 * 1024,
            now=JUNE,
        )
        sources.append(source)
    reader = FileSourceReader(world.storage, config)
    mappings = [
        world.client_store.save_mapping(
            suggested_mapping_spec(
                reader.profile(source),
                config,
                role=source.role or "",
                use_case=USE_CASE,
                mapping_id=f"m_bound_{source.role}",
            ).with_hash()
        )
        for source in sources
    ]
    spec = world.client_store.save_spec(
        world.spec.model_copy(
            update={
                "spec_id": "sp_bound",
                "entity_source_id": sources[0].source_id,
                "event_source_ids": (sources[1].source_id,),
                "mapping_ids": tuple(sorted(m.mapping_id for m in mappings)),
            }
        ).with_hash()
    )
    return Bound(world, connections, record.connection_id, spec)


def _training_frame(bound: Bound) -> pd.DataFrame:
    manifest = build_dataset_from_spec(
        bound.spec,
        config=load_use_case(USE_CASE),
        mode=RunMode.TRAIN,
        client_store=bound.world.client_store,
        storage=bound.world.storage,
    )
    return pd.read_parquet(
        bound.world.storage.local_path(dataset_key(manifest.dataset_id, "dataset.parquet"))
    )


def _manifest(world: World, dataset_id: str | None) -> DatasetManifest:
    assert dataset_id is not None
    return world.storage.read_model(dataset_key(dataset_id, "dataset_manifest.json"), DatasetManifest)


def test_a_source_from_a_connection_is_bound_and_no_upload_is_made(bound: Bound, s3: Any) -> None:
    sources = bound.world.client_store.list_sources(bound.world.client_id)
    from_bucket = [s for s in sources if s.binding is not None]
    assert {(s.role, s.binding.object_path) for s in from_bucket if s.binding} == {
        ("entity", "exports/customers/2026-06.csv"),
        ("activity", "exports/activity/2026-06.csv"),
    }
    binding = from_bucket[0].binding
    assert binding is not None and binding.connection_id == bound.connection_id and binding.pick == "newest"
    assert not bound.world.storage.list_keys("uploads/")
    # A source uploaded as a file carries no binding, and its stored document is exactly as before.
    uploaded = bound.world.client_store.get_source("s_customers")
    assert uploaded.binding is None and "binding" not in uploaded.model_dump_json()


def test_the_newest_object_under_the_prefix_is_read_by_the_next_scheduled_build(
    bound: Bound, s3: Any
) -> None:
    world = bound.world
    register_champion(world, _training_frame(bound))
    later = tables(seed=7, end=datetime(2026, 7, 1, tzinfo=UTC).date())["activity"]
    s3.put_object(Bucket=BUCKET, Key="exports/activity/2026-07.csv", Body=_csv(later))
    before = _objects(s3)

    firer = ScheduleFirer(world.services(cycle=bound.cycle()))
    schedule = world.schedule(
        ScheduleKind.SCORE, parameters=ScheduleParameters(onboarding_spec_id=bound.spec.spec_id)
    )
    firing = firer.fire(schedule) or pytest.fail("not fired")
    assert firing.status is FiringStatus.RUNNING, firing.error_code
    manifest = _manifest(world, firing.dataset_id)
    newest = [
        s
        for s in world.client_store.list_sources(world.client_id)
        if s.binding is not None and s.binding.object_path == "exports/activity/2026-07.csv"
    ]
    assert len(newest) == 1 and newest[0].role == "activity"
    assert newest[0].source_id in manifest.source_fingerprints
    assert newest[0].rows == len(later.index)
    assert not world.storage.list_keys("uploads/"), "no upload: the table came from the connection"
    assert _objects(s3) == before, "nothing is written to the client's bucket"

    # Nothing newer: the next build reads the same table again and adds no copy of it.
    count = len(world.client_store.list_sources(world.client_id))
    world.clock.advance(days=1)
    again = firer.fire(world.store.get(schedule.schedule_id)) or pytest.fail("not fired")
    assert again.status is FiringStatus.RUNNING, again.error_code
    assert len(world.client_store.list_sources(world.client_id)) == count
    assert newest[0].source_id in _manifest(world, again.dataset_id).source_fingerprints


def test_without_the_connections_service_a_firing_reads_the_saved_tables_as_before(
    bound: Bound, s3: Any
) -> None:
    world = bound.world
    register_champion(world, _training_frame(bound))
    s3.put_object(Bucket=BUCKET, Key="exports/activity/2026-07.csv", Body=_csv(tables(seed=7)["activity"]))
    firer = ScheduleFirer(world.services())
    schedule = world.schedule(
        ScheduleKind.SCORE, parameters=ScheduleParameters(onboarding_spec_id=bound.spec.spec_id)
    )
    firing = firer.fire(schedule) or pytest.fail("not fired")
    assert firing.status is FiringStatus.RUNNING, firing.error_code
    manifest = _manifest(world, firing.dataset_id)
    assert set(manifest.source_fingerprints) == {bound.spec.entity_source_id, *bound.spec.event_source_ids}
    assert not any(
        s.binding is not None and s.binding.object_path == "exports/activity/2026-07.csv"
        for s in world.client_store.list_sources(world.client_id)
    )
    assert world.storage.exists(run_key(firing.run_id or "", "run.json"))


def test_a_connection_that_fails_fails_the_firing_with_its_reason(bound: Bound, s3: Any) -> None:
    world = bound.world
    register_champion(world, _training_frame(bound))
    for key in _objects(s3):
        if key.startswith("exports/activity/"):
            s3.delete_object(Bucket=BUCKET, Key=key)
    firer = ScheduleFirer(world.services(cycle=bound.cycle()))
    schedule = world.schedule(
        ScheduleKind.SCORE, parameters=ScheduleParameters(onboarding_spec_id=bound.spec.spec_id)
    )
    firing = firer.fire(schedule) or pytest.fail("not fired")
    assert firing.status is FiringStatus.FAILED and firing.error_code == "PULL_NOTHING_FOUND"


# --- the route ------------------------------------------------------------------------------------------
@pytest.fixture
def client(config_root: Path, tmp_path: Path, s3: Any) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "api-data")) as test_client:
        yield test_client


def _api_connection(client: TestClient) -> str:
    response = client.post(
        "/connections",
        json={
            "kind": "s3",
            "name": "Client exports",
            "config": {"bucket": BUCKET, "region": "us-east-1"},
            "secrets": {"access_key_id": "testing", "secret_access_key": "testing"},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["connection_id"])


def _api_client(client: TestClient) -> str:
    response = client.post("/clients", json={"name": "Demo Telco", "industry": "telecom"})
    assert response.status_code == 201, response.text
    return str(response.json()["client_id"])


def test_post_from_connection_adds_a_bound_source_with_its_profile(client: TestClient, s3: Any) -> None:
    made = tables()
    s3.put_object(Bucket=BUCKET, Key="exports/customers/2026-06.csv", Body=_csv(made["customers"]))
    client_id = _api_client(client)
    connection_id = _api_connection(client)
    response = client.post(
        f"/clients/{client_id}/sources/from-connection",
        json={
            "connection_id": connection_id,
            "selection": {"prefix": "exports/customers/"},
            "role": "entity",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert response.headers["Location"] == f"/clients/{client_id}/sources/{body['source_id']}"
    assert body["profile"]["profile"]["row_count"] == len(made["customers"].index)
    listed = client.get(f"/clients/{client_id}/sources").json()
    (source,) = listed["sources"]
    assert source["binding"]["object_path"] == "exports/customers/2026-06.csv"
    assert source["binding"]["prefix"] == "exports/customers/" and source["role"] == "entity"
    assert body["source_id"] in listed["profiles"]


def test_post_from_connection_refusals_are_plain(client: TestClient, s3: Any) -> None:
    client_id = _api_client(client)
    connection_id = _api_connection(client)
    empty = client.post(
        f"/clients/{client_id}/sources/from-connection",
        json={"connection_id": connection_id, "selection": {"prefix": "nothing-here/"}},
    )
    assert empty.status_code == 404 and empty.json()["detail"]["code"] == "PULL_NOTHING_FOUND"
    two = client.post(
        f"/clients/{client_id}/sources/from-connection",
        json={"connection_id": connection_id, "selection": {"prefix": "a/", "path": "a/b.csv"}},
    )
    assert two.status_code == 422
    unknown = client.post(
        "/clients/no-such-client/sources/from-connection",
        json={"connection_id": connection_id, "selection": {"prefix": "a/"}},
    )
    assert unknown.status_code == 404
    assert client.get(f"/clients/{client_id}/sources").json()["sources"] == []


def test_a_multipart_source_upload_behaves_exactly_as_today(client: TestClient) -> None:
    client_id = _api_client(client)
    files = {"file": ("customers.csv", _csv(tables()["customers"]), "text/csv")}
    response = client.post(f"/clients/{client_id}/sources", files=files, data={"role": "entity"})
    assert response.status_code == 201, response.text
    (source,) = client.get(f"/clients/{client_id}/sources").json()["sources"]
    assert "binding" not in source
    operation = client.app.openapi()["paths"]["/clients/{client_id}/sources"]["post"]  # type: ignore[attr-defined]
    assert set(operation["requestBody"]["content"]) == {"multipart/form-data"}
    assert response.json()["profile"]["file_name"] == "customers.csv"
