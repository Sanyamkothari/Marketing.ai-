"""`engine.clients`: the SQLite metadata store for clients, sources, mappings, specs and datasets.

Every factory below builds the smallest artefact that satisfies its model's own validators, with
explicit `minutes` offsets from a fixed `NOW` (the `test_registry.py` pattern) wherever a test needs
a known creation order - the real wall clock is never trusted to order two calls a test cares about.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.clients import ClientStore, ClientStoreError, LocalClientStore
from engine.onboarding.specs import (
    DatasetColumn,
    DatasetFingerprint,
    DatasetManifest,
    DecidedBy,
    FeatureSpec,
    MappingColumn,
    MappingSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    SourceSpec,
    StandardType,
    spec_hash,
)
from engine.utils.time import utc_now

NOW = utc_now()


def _fingerprint(n_rows: int = 10) -> DatasetFingerprint:
    return DatasetFingerprint(hash="deadbeef", algorithm="sha256", n_rows=n_rows, columns=("id", "name"))


def _source(
    client_id: str,
    source_id: str,
    *,
    role: str | None = None,
    minutes: int = 0,
) -> SourceSpec:
    return SourceSpec(
        source_id=source_id,
        client_id=client_id,
        file_name="customers.csv",
        storage_key=f"clients/{client_id}/sources/{source_id}/raw.csv",
        file_format="csv",
        role=role,
        rows=100,
        columns=("id", "name"),
        fingerprint=_fingerprint(),
        created_at=NOW + timedelta(minutes=minutes),
    )


def _mapping(
    client_id: str,
    source_id: str,
    mapping_id: str,
    *,
    use_case: str = "test_uc",
    minutes: int = 0,
) -> MappingSpec:
    return MappingSpec(
        mapping_id=mapping_id,
        client_id=client_id,
        source_id=source_id,
        use_case=use_case,
        role="entity",
        columns=(
            MappingColumn(
                source="cust_id", standard="entity_key", confidence=0.99, decided_by=DecidedBy.AUTO
            ),
        ),
        created_at=NOW + timedelta(minutes=minutes),
    )


def _spec(
    client_id: str,
    spec_id: str,
    *,
    use_case: str = "test_uc",
    entity_source_id: str = "src_1",
    mapping_ids: tuple[str, ...] = ("map_1",),
    minutes: int = 0,
) -> OnboardingSpec:
    return OnboardingSpec(
        spec_id=spec_id,
        client_id=client_id,
        use_case=use_case,
        entity_source_id=entity_source_id,
        mapping_ids=mapping_ids,
        feature_spec=FeatureSpec(features=()),
        snapshot_spec=SnapshotDefinition(mode=SnapshotMode.SINGLE),
        created_at=NOW + timedelta(minutes=minutes),
    )


def _manifest(
    client_id: str,
    dataset_id: str,
    *,
    use_case: str = "test_uc",
    minutes: int = 0,
) -> DatasetManifest:
    return DatasetManifest(
        dataset_id=dataset_id,
        client_id=client_id,
        use_case=use_case,
        spec_id="spec_1",
        spec_hash=spec_hash(_spec(client_id, "spec_1")),
        feature_spec_hash="sha256:v1:deadbeef",
        snapshot_mode=SnapshotMode.SINGLE,
        primary_key=("entity_key",),
        target=None,
        columns=(DatasetColumn(name="entity_key", type=StandardType.CATEGORICAL, origin="key"),),
        n_rows=10,
        n_entities=10,
        snapshot_dates=(),
        fingerprint=_fingerprint(),
        built_at=NOW + timedelta(minutes=minutes),
        engine_version="0.0.0-test",
    )


@pytest.fixture
def store(tmp_path: Path) -> LocalClientStore:
    return LocalClientStore(tmp_path / "clients.db")


def test_implements_the_protocol(store: LocalClientStore) -> None:
    assert isinstance(store, ClientStore)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def test_a_client_round_trips(store: LocalClientStore) -> None:
    created = store.create_client("Acme Water Co", "utilities", notes="pilot account")
    fetched = store.get_client(created.client_id)
    assert fetched == created
    assert created.client_id.startswith("c_acme_water_co_")


def test_client_ids_are_stable_and_collision_free_per_slug(store: LocalClientStore) -> None:
    first = store.create_client("Acme", "utilities")
    second = store.create_client("Acme", "utilities")
    third = store.create_client("Acme Inc", "utilities")
    assert first.client_id != second.client_id
    assert first.client_id == "c_acme_1"
    assert second.client_id == "c_acme_2"
    assert third.client_id == "c_acme_inc_1"


def test_a_name_with_no_word_characters_falls_back_to_a_plain_slug(store: LocalClientStore) -> None:
    created = store.create_client("!!!", "utilities")
    assert created.client_id == "c_client_1"


def test_get_client_not_found(store: LocalClientStore) -> None:
    with pytest.raises(ClientStoreError) as error:
        store.get_client("c_does_not_exist_1")
    assert error.value.code == "NOT_FOUND"
    assert error.value.entity == "client"


def test_list_clients_is_newest_first(store: LocalClientStore) -> None:
    # Same name each time, so the monotonic id suffix orders these independent of clock resolution.
    a = store.create_client("Repeat Co", "utilities")
    b = store.create_client("Repeat Co", "utilities")
    c = store.create_client("Repeat Co", "utilities")
    assert store.list_clients() == (c, b, a)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def test_a_source_round_trips(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    source = _source(client.client_id, "src_1")
    added = store.add_source(client.client_id, source)
    assert added == source
    assert store.get_source("src_1") == source


def test_add_source_to_an_unknown_client_is_not_found(store: LocalClientStore) -> None:
    with pytest.raises(ClientStoreError) as error:
        store.add_source("c_missing_1", _source("c_missing_1", "src_1"))
    assert error.value.code == "NOT_FOUND"
    assert error.value.entity == "client"


def test_add_source_refuses_a_client_mismatch(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    other = store.create_client("Widgets", "manufacturing")
    mismatched = _source(other.client_id, "src_1")
    with pytest.raises(ClientStoreError) as error:
        store.add_source(client.client_id, mismatched)
    assert error.value.code == "CLIENT_MISMATCH"


def test_get_source_not_found(store: LocalClientStore) -> None:
    with pytest.raises(ClientStoreError) as error:
        store.get_source("src_missing")
    assert error.value.code == "NOT_FOUND"
    assert error.value.entity == "source"


def test_list_sources_is_newest_first(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    older = _source(client.client_id, "src_1", minutes=0)
    newer = _source(client.client_id, "src_2", minutes=5)
    store.add_source(client.client_id, older)
    store.add_source(client.client_id, newer)
    assert store.list_sources(client.client_id) == (newer, older)


def test_two_clients_sources_never_leak(store: LocalClientStore) -> None:
    a = store.create_client("Acme", "utilities")
    b = store.create_client("Widgets", "manufacturing")
    store.add_source(a.client_id, _source(a.client_id, "src_a"))
    store.add_source(b.client_id, _source(b.client_id, "src_b"))
    assert [s.source_id for s in store.list_sources(a.client_id)] == ["src_a"]
    assert [s.source_id for s in store.list_sources(b.client_id)] == ["src_b"]


def test_a_sources_role_can_be_set_and_cleared(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    store.add_source(client.client_id, _source(client.client_id, "src_1", role=None))
    confirmed = store.set_source_role("src_1", "entity")
    assert confirmed.role == "entity"
    assert store.get_source("src_1").role == "entity"
    cleared = store.set_source_role("src_1", None)
    assert cleared.role is None
    assert store.get_source("src_1").role is None


def test_set_source_role_not_found(store: LocalClientStore) -> None:
    with pytest.raises(ClientStoreError) as error:
        store.set_source_role("src_missing", "entity")
    assert error.value.code == "NOT_FOUND"


def test_delete_source_removes_it_and_is_idempotent_only_once(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    store.add_source(client.client_id, _source(client.client_id, "src_1"))
    store.delete_source("src_1")
    assert store.list_sources(client.client_id) == ()
    with pytest.raises(ClientStoreError) as error:
        store.get_source("src_1")
    assert error.value.code == "NOT_FOUND"
    with pytest.raises(ClientStoreError) as error:
        store.delete_source("src_1")
    assert error.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------
def test_a_mapping_round_trips_and_carries_its_stable_hash(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    mapping = _mapping(client.client_id, "src_1", "map_1")
    saved = store.save_mapping(mapping)
    assert saved.hash != ""
    assert saved.hash == spec_hash(saved)
    assert store.get_mapping("map_1") == saved


def test_save_mapping_is_an_upsert_by_id(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    store.save_mapping(_mapping(client.client_id, "src_1", "map_1"))
    edited = _mapping(client.client_id, "src_1", "map_1", use_case="test_uc_v2")
    store.save_mapping(edited)
    assert len(store.list_mappings(client.client_id)) == 1
    assert store.get_mapping("map_1").use_case == "test_uc_v2"


def test_get_mapping_not_found(store: LocalClientStore) -> None:
    with pytest.raises(ClientStoreError) as error:
        store.get_mapping("map_missing")
    assert error.value.code == "NOT_FOUND"
    assert error.value.entity == "mapping"


def test_list_mappings_is_newest_first_and_scoped_to_client(store: LocalClientStore) -> None:
    a = store.create_client("Acme", "utilities")
    b = store.create_client("Widgets", "manufacturing")
    older = store.save_mapping(_mapping(a.client_id, "src_1", "map_1", minutes=0))
    newer = store.save_mapping(_mapping(a.client_id, "src_1", "map_2", minutes=5))
    store.save_mapping(_mapping(b.client_id, "src_2", "map_3"))
    assert store.list_mappings(a.client_id) == (newer, older)


def test_list_mappings_can_narrow_to_one_use_case(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    churn = store.save_mapping(_mapping(client.client_id, "src_1", "map_1", use_case="churn_like"))
    store.save_mapping(_mapping(client.client_id, "src_1", "map_2", use_case="other_uc"))
    assert store.list_mappings(client.client_id, use_case="churn_like") == (churn,)


# ---------------------------------------------------------------------------
# Onboarding specs
# ---------------------------------------------------------------------------
def test_a_spec_round_trips_and_carries_its_stable_hash(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    spec = _spec(client.client_id, "spec_1")
    saved = store.save_spec(spec)
    assert saved.hash != ""
    assert saved.hash == spec_hash(saved)
    assert store.get_spec("spec_1") == saved


def test_save_spec_is_an_upsert_by_id(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    store.save_spec(_spec(client.client_id, "spec_1", entity_source_id="src_1"))
    store.save_spec(_spec(client.client_id, "spec_1", entity_source_id="src_2"))
    assert len(store.list_specs(client.client_id)) == 1
    assert store.get_spec("spec_1").entity_source_id == "src_2"


def test_get_spec_not_found(store: LocalClientStore) -> None:
    with pytest.raises(ClientStoreError) as error:
        store.get_spec("spec_missing")
    assert error.value.code == "NOT_FOUND"
    assert error.value.entity == "spec"


def test_list_specs_is_newest_first_and_scoped_to_client(store: LocalClientStore) -> None:
    a = store.create_client("Acme", "utilities")
    b = store.create_client("Widgets", "manufacturing")
    older = store.save_spec(_spec(a.client_id, "spec_1", minutes=0))
    newer = store.save_spec(_spec(a.client_id, "spec_2", minutes=5))
    store.save_spec(_spec(b.client_id, "spec_3"))
    assert store.list_specs(a.client_id) == (newer, older)


def test_list_specs_can_narrow_to_one_use_case(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    churn = store.save_spec(_spec(client.client_id, "spec_1", use_case="churn_like"))
    store.save_spec(_spec(client.client_id, "spec_2", use_case="other_uc"))
    assert store.list_specs(client.client_id, use_case="churn_like") == (churn,)


# ---------------------------------------------------------------------------
# Dataset manifests
# ---------------------------------------------------------------------------
def test_a_dataset_manifest_round_trips(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    manifest = _manifest(client.client_id, "ds_1")
    store.register_dataset(manifest)
    assert store.get_dataset("ds_1") == manifest


def test_register_dataset_refuses_a_reused_id(store: LocalClientStore) -> None:
    client = store.create_client("Acme", "utilities")
    store.register_dataset(_manifest(client.client_id, "ds_1"))
    with pytest.raises(ClientStoreError) as error:
        store.register_dataset(_manifest(client.client_id, "ds_1"))
    assert error.value.code == "DUPLICATE"
    assert error.value.entity == "dataset"


def test_get_dataset_not_found(store: LocalClientStore) -> None:
    with pytest.raises(ClientStoreError) as error:
        store.get_dataset("ds_missing")
    assert error.value.code == "NOT_FOUND"
    assert error.value.entity == "dataset"


def test_list_datasets_is_newest_first_and_can_narrow_by_client_and_use_case(
    store: LocalClientStore,
) -> None:
    a = store.create_client("Acme", "utilities")
    b = store.create_client("Widgets", "manufacturing")
    older = _manifest(a.client_id, "ds_1", use_case="churn_like", minutes=0)
    newer = _manifest(a.client_id, "ds_2", use_case="churn_like", minutes=5)
    other_client = _manifest(b.client_id, "ds_3", use_case="churn_like")
    other_use_case = _manifest(a.client_id, "ds_4", use_case="other_uc", minutes=10)
    for manifest in (older, newer, other_client, other_use_case):
        store.register_dataset(manifest)
    assert store.list_datasets() == (other_use_case, newer, other_client, older)
    assert store.list_datasets(client_id=a.client_id) == (other_use_case, newer, older)
    assert store.list_datasets(client_id=a.client_id, use_case="churn_like") == (newer, older)


# ---------------------------------------------------------------------------
# Re-validation on read
# ---------------------------------------------------------------------------
def test_a_document_that_no_longer_matches_its_model_fails_on_read(store: LocalClientStore) -> None:
    """The task's own words: "store the JSON documents in TEXT columns and re-validate on read" - a
    schema that moved on since a row was written must surface as a clear error the moment it is read
    back, not as a silent `AttributeError` somewhere downstream."""
    client = store.create_client("Acme", "utilities")
    store.add_source(client.client_id, _source(client.client_id, "src_1"))
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE sources SET document = '{\"not\": \"a source\"}' WHERE source_id = 'src_1'")
        conn.commit()
    with pytest.raises(ValidationError):
        store.get_source("src_1")
