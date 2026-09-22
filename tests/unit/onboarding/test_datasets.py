"""`engine.onboarding.datasets`: the dataset registry and the lineage helpers (Phase 2 plan §4, §8, M12).

A dataset is immutable and traceable, so what this file is really pinning is two promises: every
artefact `LocalDatasetRegistry` writes comes back byte-for-byte identical (the round trips), and
`build_manifest`/`lineage` refuse to describe a build they were not handed the whole story of (the
`DatasetError` cases) rather than silently drawing an incomplete picture.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.contracts import DatasetFingerprint, DatasetProfile, RunState
from engine.onboarding.datasets import (
    DATASET_FEATURES_SQL_FILENAME,
    DATASET_FRAME_FILENAME,
    DATASET_MANIFEST_FILENAME,
    DATASET_REPORT_FILENAME,
    DATASET_SAMPLE_FILENAME,
    DATASET_STATUS_FILENAME,
    DatasetError,
    DatasetRegistry,
    LocalDatasetRegistry,
    build_manifest,
    dataset_fingerprint_of,
    dataset_key,
    lineage,
)
from engine.onboarding.specs import (
    DATASET_ARTEFACTS,
    BuildReport,
    BuildStage,
    BuildStatus,
    DatasetColumn,
    DatasetManifest,
    DecidedBy,
    FeatureSpec,
    MappingColumn,
    MappingSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    SourceProfile,
    StandardType,
)
from engine.stages.ingest import REDACTED, dataset_fingerprint
from engine.storage import LocalStorage
from engine.utils.time import utc_now

NOW = utc_now()


# ---------------------------------------------------------------------------
# Factories - the smallest artefact that satisfies its own model's validators
# ---------------------------------------------------------------------------
def _fingerprint(n_rows: int = 10, columns: tuple[str, ...] = ("entity_key",)) -> DatasetFingerprint:
    return DatasetFingerprint(
        hash="sha256:v1:deadbeef", algorithm="sha256:v1", n_rows=n_rows, columns=columns
    )


def _dataset_profile(*, fingerprint: DatasetFingerprint) -> DatasetProfile:
    return DatasetProfile(
        upload_id="u_1",
        file_name="customers.csv",
        file_size_bytes=1_000,
        file_format="csv",
        delimiter=",",
        encoding="utf-8",
        row_count=fingerprint.n_rows,
        column_count=len(fingerprint.columns),
        columns=(),
        primary_key_candidates=(),
        time_column_candidates=(),
        target_candidate=None,
        preview_rows=(),
        missing_value_rate_pct=0.0,
        fingerprint=fingerprint,
        profiled_at=NOW,
    )


def _source_profile(
    source_id: str,
    *,
    client_id: str = "acme",
    role: str | None = "entity",
    fingerprint: DatasetFingerprint | None = None,
) -> SourceProfile:
    fp = fingerprint or _fingerprint()
    return SourceProfile(
        source_id=source_id,
        client_id=client_id,
        file_name="customers.csv",
        role=role,
        role_decided_by=DecidedBy.AUTO if role else None,
        profile=_dataset_profile(fingerprint=fp),
    )


def _mapping(
    mapping_id: str,
    source_id: str,
    *,
    client_id: str = "acme",
    use_case: str = "churn",
    role: str = "entity",
) -> MappingSpec:
    return MappingSpec(
        mapping_id=mapping_id,
        client_id=client_id,
        source_id=source_id,
        use_case=use_case,
        role=role,
        columns=(
            MappingColumn(
                source="cust_id", standard="entity_key", confidence=0.99, decided_by=DecidedBy.AUTO
            ),
        ),
        created_at=NOW,
    ).with_hash()


def _spec(
    spec_id: str,
    *,
    client_id: str = "acme",
    use_case: str = "churn",
    entity_source_id: str = "src_1",
    event_source_ids: tuple[str, ...] = (),
    mapping_ids: tuple[str, ...] = ("map_1",),
    mode: SnapshotMode = SnapshotMode.SINGLE,
) -> OnboardingSpec:
    return OnboardingSpec(
        spec_id=spec_id,
        client_id=client_id,
        use_case=use_case,
        entity_source_id=entity_source_id,
        event_source_ids=event_source_ids,
        mapping_ids=mapping_ids,
        feature_spec=FeatureSpec(features=()),
        snapshot_spec=SnapshotDefinition(mode=mode),
        created_at=NOW,
    ).with_hash()


def _status(dataset_id: str, *, state: RunState = RunState.RUNNING) -> BuildStatus:
    return BuildStatus(
        dataset_id=dataset_id,
        client_id="acme",
        spec_id="sp_1",
        state=state,
        updated_at=NOW,
        stages=(BuildStage(key="apply_mappings", title="Apply mappings", group_label="Build", state=state),),
        progress_pct=0,
    )


def _report(dataset_id: str) -> BuildReport:
    return BuildReport(
        dataset_id=dataset_id,
        checks=(),
        sources=(),
        snapshots=(),
        features=(),
        rows_out=10,
        entities_out=10,
        duration_s=1.5,
        error_count=0,
        warning_count=0,
        passed=True,
        built_at=NOW,
    )


def _frame(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "entity_key": [f"c{i}" for i in range(n)],
            "total_spend": [float(100 + i) for i in range(n)],
            "email": [f"user{i}@example.com" for i in range(n)],
        }
    )


def _single_columns() -> tuple[DatasetColumn, ...]:
    return (
        DatasetColumn(name="entity_key", type=StandardType.CATEGORICAL, origin="key"),
        DatasetColumn(name="total_spend", type=StandardType.NUMERIC, origin="feature"),
        DatasetColumn(name="email", type=StandardType.TEXT, origin="mapped"),
    )


def _periodic_frame(n: int = 5) -> pd.DataFrame:
    dates = ["2026-01-31", "2026-01-31", "2026-02-28", "2026-02-28", "2026-02-28"]
    return pd.DataFrame(
        {
            "entity_key": [f"c{i % 3}" for i in range(n)],
            "snapshot_date": dates[:n],
            "total_spend": [float(100 + i) for i in range(n)],
        }
    )


def _periodic_columns() -> tuple[DatasetColumn, ...]:
    return (
        DatasetColumn(name="entity_key", type=StandardType.CATEGORICAL, origin="key"),
        DatasetColumn(name="snapshot_date", type=StandardType.DATE, origin="key"),
        DatasetColumn(name="total_spend", type=StandardType.NUMERIC, origin="feature"),
    )


@pytest.fixture
def registry(tmp_path: Path) -> LocalDatasetRegistry:
    return LocalDatasetRegistry(LocalStorage(tmp_path))


# ---------------------------------------------------------------------------
# The registry shape
# ---------------------------------------------------------------------------
def test_local_dataset_registry_satisfies_the_protocol(registry: LocalDatasetRegistry) -> None:
    assert isinstance(registry, DatasetRegistry)


def test_dataset_key_matches_run_key_and_upload_keys_shape() -> None:
    assert dataset_key("ds_1", "dataset_manifest.json") == "datasets/ds_1/dataset_manifest.json"


def test_filenames_are_exactly_the_dataset_artefact_set() -> None:
    assert {
        DATASET_MANIFEST_FILENAME,
        DATASET_REPORT_FILENAME,
        DATASET_STATUS_FILENAME,
        DATASET_FRAME_FILENAME,
        DATASET_SAMPLE_FILENAME,
        DATASET_FEATURES_SQL_FILENAME,
    } == DATASET_ARTEFACTS


# ---------------------------------------------------------------------------
# new_dataset_id
# ---------------------------------------------------------------------------
def test_new_dataset_id_is_sortable_and_collision_free(
    registry: LocalDatasetRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr("engine.onboarding.datasets.utc_now", lambda: fixed)
    expected_prefix = f"ds_{fixed.strftime('%Y%m%dT%H%M%S%f')}"

    first = registry.new_dataset_id("acme", "churn")
    assert first == expected_prefix
    registry.write_status(first, _status(first))

    # Same instant, again: the id this call would otherwise mint is already claimed, so it must
    # not be handed out twice - this is the "collision-free" guarantee, exercised for real rather
    # than assumed from enough random bits.
    second = registry.new_dataset_id("acme", "churn")
    assert second == f"{expected_prefix}_1"
    assert second != first
    registry.write_status(second, _status(second))

    third = registry.new_dataset_id("acme", "churn")
    assert third == f"{expected_prefix}_2"

    # Sortable: a later moment produces a lexicographically later id.
    later = datetime(2026, 9, 22, 12, 0, 1, tzinfo=UTC)
    monkeypatch.setattr("engine.onboarding.datasets.utc_now", lambda: later)
    assert registry.new_dataset_id("acme", "churn") > third


def test_new_dataset_id_refuses_a_blank_client_or_use_case(registry: LocalDatasetRegistry) -> None:
    with pytest.raises(DatasetError) as excinfo:
        registry.new_dataset_id("", "churn")
    assert excinfo.value.code == "DATASET_ID_BLANK"

    with pytest.raises(DatasetError) as excinfo:
        registry.new_dataset_id("acme", "   ")
    assert excinfo.value.code == "DATASET_ID_BLANK"


# ---------------------------------------------------------------------------
# status / manifest / report round trips
# ---------------------------------------------------------------------------
def test_status_round_trips(registry: LocalDatasetRegistry) -> None:
    status = _status("ds_1")
    registry.write_status("ds_1", status)
    assert registry.read_status("ds_1") == status


def test_manifest_round_trips(registry: LocalDatasetRegistry) -> None:
    manifest = build_manifest(
        "ds_1",
        _spec("sp_1"),
        mappings={"map_1": _mapping("map_1", "src_1")},
        sources={"src_1": _source_profile("src_1")},
        frame=_frame(5),
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
        built_at=NOW,
        engine_version="0.0.0-test",
    )
    registry.write_manifest("ds_1", manifest)
    assert registry.read_manifest("ds_1") == manifest


def test_report_round_trips(registry: LocalDatasetRegistry) -> None:
    report = _report("ds_1")
    registry.write_report("ds_1", report)
    assert registry.read_report("ds_1") == report


@pytest.mark.parametrize(
    ("write", "wrong"),
    [
        (lambda r, dataset_id: r.write_status(dataset_id, _status("ds_other")), "build status"),
        (
            lambda r, dataset_id: r.write_manifest(
                dataset_id,
                build_manifest(
                    "ds_other",
                    _spec("sp_1"),
                    mappings={"map_1": _mapping("map_1", "src_1")},
                    sources={"src_1": _source_profile("src_1")},
                    frame=_frame(2),
                    columns=_single_columns(),
                    entity_key="entity_key",
                    snapshot_column="snapshot_date",
                    target=None,
                ),
            ),
            "dataset manifest",
        ),
        (lambda r, dataset_id: r.write_report(dataset_id, _report("ds_other")), "build report"),
    ],
)
def test_writing_under_the_wrong_id_is_refused(registry: LocalDatasetRegistry, write, wrong: str) -> None:
    with pytest.raises(DatasetError) as excinfo:
        write(registry, "ds_1")
    assert excinfo.value.code == "DATASET_ID_MISMATCH"
    assert wrong in str(excinfo.value)


# ---------------------------------------------------------------------------
# write_frame / read_frame / fingerprints
# ---------------------------------------------------------------------------
def test_write_frame_round_trips_the_rows(registry: LocalDatasetRegistry) -> None:
    frame = _frame(5)
    registry.write_frame("ds_1", frame)
    back = registry.read_frame("ds_1")
    pd.testing.assert_frame_equal(back, frame)


def test_read_frame_respects_max_rows(registry: LocalDatasetRegistry) -> None:
    registry.write_frame("ds_1", _frame(10))
    assert len(registry.read_frame("ds_1", max_rows=3)) == 3
    assert len(registry.read_frame("ds_1")) == 10


def test_write_frame_returns_the_frames_own_fingerprint(registry: LocalDatasetRegistry) -> None:
    frame = _frame(5)
    assert registry.write_frame("ds_1", frame) == dataset_fingerprint_of(frame)


def test_fingerprint_changes_when_a_cell_changes(registry: LocalDatasetRegistry) -> None:
    base = _frame(5)
    changed = base.copy()
    changed.loc[0, "total_spend"] = 999.0
    assert registry.write_frame("ds_a", base) != registry.write_frame("ds_b", changed)


def test_fingerprint_is_stable_for_the_same_rows(registry: LocalDatasetRegistry) -> None:
    frame = _frame(5)
    assert registry.write_frame("ds_a", frame) == registry.write_frame("ds_b", frame.copy())


def test_fingerprint_changes_when_only_row_order_differs(registry: LocalDatasetRegistry) -> None:
    """`dataset_fingerprint_of` deliberately treats row order as part of a dataset's identity (see
    its docstring): a dataset is supposed to be immutable, which has to mean "this exact file, in
    this exact row order", not merely "these rows, in some order". A reorder therefore changes the
    fingerprint exactly the way a changed cell does - this is the deliberate choice the M12 task
    description allows either way, made explicit and pinned here."""
    base = _frame(5)
    reordered = base.iloc[::-1].reset_index(drop=True)
    assert registry.write_frame("ds_a", base) != registry.write_frame("ds_b", reordered)


def test_dataset_fingerprint_of_reuses_the_ingest_fingerprinter_and_nothing_else() -> None:
    frame = _frame(4)
    assert dataset_fingerprint_of(frame) == dataset_fingerprint(frame)


# ---------------------------------------------------------------------------
# sample.json
# ---------------------------------------------------------------------------
def test_sample_is_fifty_rows_and_redacts_a_pii_column(registry: LocalDatasetRegistry) -> None:
    registry.write_frame("ds_1", _frame(60), pii_columns=("email",))
    sample = registry.read_sample("ds_1")
    assert len(sample) == 50
    assert all(row["email"] == REDACTED for row in sample)
    assert sample[0]["entity_key"] == "c0"
    assert sample[0]["total_spend"] == "100"


def test_sample_is_shorter_than_fifty_when_the_frame_is(registry: LocalDatasetRegistry) -> None:
    registry.write_frame("ds_1", _frame(3))
    sample = registry.read_sample("ds_1")
    assert len(sample) == 3
    assert all(row["email"] != REDACTED for row in sample)


# ---------------------------------------------------------------------------
# features.sql
# ---------------------------------------------------------------------------
def test_write_features_sql_returns_the_key_and_is_stored(registry: LocalDatasetRegistry) -> None:
    key = registry.write_features_sql("ds_1", "select 1")
    assert key == dataset_key("ds_1", "features.sql")
    assert registry.storage.read_text(key) == "select 1"


# ---------------------------------------------------------------------------
# exists / delete
# ---------------------------------------------------------------------------
def test_exists_and_delete(registry: LocalDatasetRegistry) -> None:
    assert not registry.exists("ds_1")
    registry.write_status("ds_1", _status("ds_1"))
    assert registry.exists("ds_1")

    registry.delete("ds_1")
    assert not registry.exists("ds_1")
    registry.delete("ds_1")  # deleting an id nothing was written for is a no-op, not an error


# ---------------------------------------------------------------------------
# Reading a missing dataset
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "read",
    [
        lambda r: r.read_status("ds_missing"),
        lambda r: r.read_manifest("ds_missing"),
        lambda r: r.read_report("ds_missing"),
        lambda r: r.read_frame("ds_missing"),
        lambda r: r.read_sample("ds_missing"),
    ],
)
def test_reading_a_missing_dataset_raises_a_coded_error_naming_the_id(
    registry: LocalDatasetRegistry, read
) -> None:
    with pytest.raises(DatasetError) as excinfo:
        read(registry)
    assert excinfo.value.code == "DATASET_NOT_FOUND"
    assert excinfo.value.dataset_id == "ds_missing"
    assert "ds_missing" in str(excinfo.value)


# ---------------------------------------------------------------------------
# build_manifest
# ---------------------------------------------------------------------------
def test_build_manifest_single_mode_is_keyed_by_the_entity_alone(registry: LocalDatasetRegistry) -> None:
    spec = _spec("sp_1")
    mapping = _mapping("map_1", "src_1")
    source = _source_profile("src_1")
    frame = _frame(5)
    manifest = build_manifest(
        "ds_1",
        spec,
        mappings={"map_1": mapping},
        sources={"src_1": source},
        frame=frame,
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )
    assert manifest.primary_key == ("entity_key",)
    assert manifest.snapshot_mode is SnapshotMode.SINGLE
    assert manifest.n_rows == 5
    assert manifest.n_entities == 5
    assert manifest.snapshot_dates == ()
    assert manifest.mapping_hashes == {"map_1": mapping.hash}
    assert manifest.source_fingerprints == {"src_1": source.fingerprint}
    assert manifest.spec_hash == spec.hash
    assert manifest.fingerprint == dataset_fingerprint_of(frame)


def test_build_manifest_periodic_mode_is_keyed_by_entity_and_snapshot() -> None:
    spec = _spec("sp_2", mode=SnapshotMode.PERIODIC)
    frame = _periodic_frame(5)
    manifest = build_manifest(
        "ds_2",
        spec,
        mappings={"map_1": _mapping("map_1", "src_1")},
        sources={"src_1": _source_profile("src_1")},
        frame=frame,
        columns=_periodic_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )
    assert manifest.primary_key == ("entity_key", "snapshot_date")
    assert manifest.n_rows == 5
    assert manifest.n_entities == 3
    assert manifest.snapshot_dates == (date(2026, 1, 31), date(2026, 2, 28))


def test_build_manifest_refuses_a_mapping_it_was_not_handed() -> None:
    spec = _spec("sp_3", mapping_ids=("map_missing",))
    with pytest.raises(DatasetError) as excinfo:
        build_manifest(
            "ds_3",
            spec,
            mappings={},
            sources={"src_1": _source_profile("src_1")},
            frame=_frame(2),
            columns=_single_columns(),
            entity_key="entity_key",
            snapshot_column="snapshot_date",
            target=None,
        )
    assert excinfo.value.code == "DATASET_MAPPING_MISSING"


def test_build_manifest_refuses_a_source_it_was_not_handed() -> None:
    spec = _spec("sp_4", entity_source_id="src_missing")
    with pytest.raises(DatasetError) as excinfo:
        build_manifest(
            "ds_4",
            spec,
            mappings={"map_1": _mapping("map_1", "src_missing")},
            sources={},
            frame=_frame(2),
            columns=_single_columns(),
            entity_key="entity_key",
            snapshot_column="snapshot_date",
            target=None,
        )
    assert excinfo.value.code == "DATASET_SOURCE_MISSING"


def test_build_manifest_refuses_columns_that_do_not_match_the_frame() -> None:
    spec = _spec("sp_5")
    with pytest.raises(DatasetError) as excinfo:
        build_manifest(
            "ds_5",
            spec,
            mappings={"map_1": _mapping("map_1", "src_1")},
            sources={"src_1": _source_profile("src_1")},
            frame=_frame(2),
            columns=(DatasetColumn(name="not_a_real_column", type=StandardType.CATEGORICAL, origin="key"),),
            entity_key="entity_key",
            snapshot_column="snapshot_date",
            target=None,
        )
    assert excinfo.value.code == "DATASET_COLUMNS_MISMATCH"


def test_build_manifest_refuses_a_periodic_spec_missing_its_snapshot_column() -> None:
    spec = _spec("sp_6", mode=SnapshotMode.PERIODIC)
    with pytest.raises(DatasetError) as excinfo:
        build_manifest(
            "ds_6",
            spec,
            mappings={"map_1": _mapping("map_1", "src_1")},
            sources={"src_1": _source_profile("src_1")},
            frame=_frame(2),  # no snapshot_date column
            columns=_single_columns(),
            entity_key="entity_key",
            snapshot_column="snapshot_date",
            target=None,
        )
    assert excinfo.value.code == "DATASET_SNAPSHOT_COLUMN_MISSING"


def test_a_periodic_manifest_with_a_one_column_key_is_refused() -> None:
    """The invariant `build_manifest` exists to get right: `DatasetManifest`'s own validator refuses
    a periodic dataset whose primary key does not also carry the snapshot column."""
    with pytest.raises(ValidationError, match="2-column primary key"):
        DatasetManifest(
            dataset_id="ds_1",
            client_id="acme",
            use_case="churn",
            spec_id="sp_1",
            spec_hash="sha256:v1:aaa",
            feature_spec_hash="sha256:v1:bbb",
            snapshot_mode=SnapshotMode.PERIODIC,
            primary_key=("entity_key",),
            target=None,
            columns=(DatasetColumn(name="entity_key", type=StandardType.CATEGORICAL, origin="key"),),
            n_rows=1,
            n_entities=1,
            snapshot_dates=(),
            fingerprint=_fingerprint(),
            built_at=NOW,
            engine_version="0.0.0-test",
        )


# ---------------------------------------------------------------------------
# lineage
# ---------------------------------------------------------------------------
def test_lineage_builds_the_sources_mappings_spec_dataset_tree() -> None:
    spec = _spec("sp_7")
    mapping = _mapping("map_1", "src_1")
    source = _source_profile("src_1")
    frame = _frame(5)
    manifest = build_manifest(
        "ds_7",
        spec,
        mappings={"map_1": mapping},
        sources={"src_1": source},
        frame=frame,
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )

    tree = lineage(manifest, mappings={"map_1": mapping}, sources={"src_1": source})

    assert [node.id for node in tree.sources] == ["src_1"]
    assert tree.sources[0].parents == ()
    assert [node.id for node in tree.mappings] == ["map_1"]
    assert tree.mappings[0].parents == ("src_1",)
    assert tree.spec.id == "sp_7"
    assert tree.spec.parents == ("map_1",)
    assert tree.dataset.id == "ds_7"
    assert tree.dataset.parents == ("sp_7",)
    assert tree.dataset_id == "ds_7"


def test_lineage_refuses_a_mapping_it_was_not_handed() -> None:
    spec = _spec("sp_8")
    mapping = _mapping("map_1", "src_1")
    source = _source_profile("src_1")
    manifest = build_manifest(
        "ds_8",
        spec,
        mappings={"map_1": mapping},
        sources={"src_1": source},
        frame=_frame(3),
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )
    with pytest.raises(DatasetError) as excinfo:
        lineage(manifest, mappings={}, sources={"src_1": source})
    assert excinfo.value.code == "DATASET_LINEAGE_INCOMPLETE"


def test_lineage_refuses_a_source_it_was_not_handed() -> None:
    spec = _spec("sp_9")
    mapping = _mapping("map_1", "src_1")
    source = _source_profile("src_1")
    manifest = build_manifest(
        "ds_9",
        spec,
        mappings={"map_1": mapping},
        sources={"src_1": source},
        frame=_frame(3),
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )
    with pytest.raises(DatasetError) as excinfo:
        lineage(manifest, mappings={"map_1": mapping}, sources={})
    assert excinfo.value.code == "DATASET_LINEAGE_INCOMPLETE"
