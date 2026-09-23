"""`engine.onboarding.datasets`: the dataset registry and the lineage helpers (Phase 2 plan §4, §8, M12).

A dataset is immutable and traceable, so what this file is really pinning is three promises: every
artefact `LocalDatasetRegistry` writes comes back byte-for-byte identical (the round trips),
`build_manifest`/`lineage` refuse to describe a build they were not handed the whole story of (the
`DatasetError` cases) rather than silently drawing an incomplete picture, and every number that
leaves this module for a screen is one that was measured - the sample rows show the digits that are
in the parquet, and a lineage card never counts something nobody counted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.config import ProblemType, RunMode
from engine.contracts import DatasetFingerprint, DatasetProfile, RunRecord, RunState
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
    run_source_key,
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
from engine.stages.ingest import MAX_CELL_CHARS, REDACTED, dataset_fingerprint
from engine.storage import LocalStorage, upload_key
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
    assert len(registry.read_frame("ds_1", max_rows=0)) == 0
    assert len(registry.read_frame("ds_1")) == 10


def test_read_frame_refuses_a_negative_row_limit(registry: LocalDatasetRegistry) -> None:
    """`DataFrame.head(-2)` means "all but the last two", so a caller that computed a limit and got
    a negative would silently be handed a frame missing its tail instead of an error."""
    registry.write_frame("ds_1", _frame(10))
    with pytest.raises(DatasetError) as excinfo:
        registry.read_frame("ds_1", max_rows=-2)
    assert excinfo.value.code == "DATASET_MAX_ROWS_INVALID"


def test_write_frame_returns_the_ingest_fingerprint_of_what_it_stored(
    registry: LocalDatasetRegistry,
) -> None:
    """The returned fingerprint is `engine.stages.ingest`'s - the one Phase 1 already trusts, not a
    second scheme - and it still describes the table after the parquet round trip. That second half
    is the immutability promise itself: a fingerprint a caller records but that no longer matches
    what `read_frame` hands back would make a manifest's `fingerprint` field a claim about a file
    nobody can reproduce."""
    frame = _frame(5)
    returned = registry.write_frame("ds_1", frame)
    assert returned == dataset_fingerprint(frame)
    assert returned == dataset_fingerprint_of(registry.read_frame("ds_1"))


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


def test_sample_shows_a_large_integer_id_with_the_digits_it_was_built_with(
    registry: LocalDatasetRegistry,
) -> None:
    """A customer id past 2**53 beside a float amount is the ordinary shape of a built dataset, and
    it is exactly the shape that a row-wise read of the frame would ruin: a row is a `Series`, so it
    has one dtype, and the id comes back as a float with different digits. The review screen would
    then show an id that is in no file anywhere, which is the worst kind of invented number because
    it still looks like an id."""
    frame = pd.DataFrame(
        {
            "entity_key": pd.Series([9007199254740993, 123456789012345678], dtype="int64"),
            "total_spend": pd.Series([1.5, 2.5], dtype="float64"),
        }
    )
    registry.write_frame("ds_1", frame)
    sample = registry.read_sample("ds_1")
    assert [row["entity_key"] for row in sample] == ["9007199254740993", "123456789012345678"]
    assert [row["total_spend"] for row in sample] == ["1.5", "2.5"]


def test_sample_keeps_each_columns_own_type(registry: LocalDatasetRegistry) -> None:
    """Booleans stay words and dates stay ISO whatever else is in the row, for the same reason."""
    frame = pd.DataFrame(
        {
            "entity_key": ["c0"],
            "active": pd.Series([True], dtype="bool"),
            "signed_up": pd.to_datetime(pd.Series(["2026-01-31"])),
            "visits": pd.Series([3], dtype="int64"),
        }
    )
    registry.write_frame("ds_1", frame)
    assert registry.read_sample("ds_1") == [
        {"entity_key": "c0", "active": "true", "signed_up": "2026-01-31", "visits": "3"}
    ]


def test_sample_cuts_a_cell_at_the_same_length_every_other_review_surface_does(
    registry: LocalDatasetRegistry,
) -> None:
    """`sample.json` is rendered straight onto the review screen, so one 50 kB free-text cell would
    be 50 kB of screen; `MAX_CELL_CHARS` is the length the Setup preview already cuts at, imported
    rather than repeated so the two surfaces cannot cut the same value at different places."""
    registry.write_frame("ds_1", pd.DataFrame({"entity_key": ["c0"], "notes": ["x" * 5_000]}))
    cell = registry.read_sample("ds_1")[0]["notes"]
    assert len(cell) == MAX_CELL_CHARS
    assert cell.endswith("…")


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
# An id that cannot name a directory
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("dataset_id", "code"),
    [
        ("", "DATASET_ID_BLANK"),
        ("   ", "DATASET_ID_BLANK"),
        ("ds_1/dataset.parquet", "DATASET_ID_INVALID"),
        ("..", "DATASET_ID_INVALID"),
        ("../runs", "DATASET_ID_INVALID"),
    ],
)
@pytest.mark.parametrize(
    "call",
    [
        lambda r, dataset_id: r.read_status(dataset_id),
        lambda r, dataset_id: r.read_manifest(dataset_id),
        lambda r, dataset_id: r.read_sample(dataset_id),
        lambda r, dataset_id: r.read_frame(dataset_id),
        lambda r, dataset_id: r.write_status(dataset_id, _status(dataset_id)),
        lambda r, dataset_id: r.write_frame(dataset_id, _frame(2)),
        lambda r, dataset_id: r.write_features_sql(dataset_id, "select 1"),
        lambda r, dataset_id: r.exists(dataset_id),
        lambda r, dataset_id: r.delete(dataset_id),
    ],
)
def test_an_id_that_cannot_name_a_directory_is_a_coded_error(
    registry: LocalDatasetRegistry, call, dataset_id: str, code: str
) -> None:
    """A path parameter that arrived empty, or carrying a separator, must fail in this module's own
    vocabulary. Left to `engine.storage.validate_key` it would be a `StorageError('KEY_INVALID')` -
    which every write path here lets straight through, so the API would answer a bad dataset id with
    a 500 and no wording rather than a plain-language 404."""
    with pytest.raises(DatasetError) as excinfo:
        call(registry, dataset_id)
    assert excinfo.value.code == code
    assert excinfo.value.dataset_id == dataset_id


def test_an_unreadable_sample_file_is_a_coded_error(registry: LocalDatasetRegistry) -> None:
    registry.write_frame("ds_1", _frame(3))
    registry.storage.write_text(dataset_key("ds_1", "sample.json"), "{not json")
    with pytest.raises(DatasetError) as excinfo:
        registry.read_sample("ds_1")
    assert excinfo.value.code == "DATASET_SAMPLE_UNREADABLE"
    assert excinfo.value.dataset_id == "ds_1"


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


def test_build_manifest_refuses_a_snapshot_value_it_cannot_read_as_a_date() -> None:
    """`snapshot_dates` is what the Lineage card and the Build review count snapshots from. Coercing
    an unreadable value to `NaT` and dropping it would quietly report one snapshot fewer than the
    dataset has - a count nobody measured, presented as one that was."""
    frame = _periodic_frame(5).assign(snapshot_date=["2026-01-31", "last tuesday", "2026-02-28", "", ""])
    with pytest.raises(DatasetError) as excinfo:
        build_manifest(
            "ds_bad",
            _spec("sp_bad", mode=SnapshotMode.PERIODIC),
            mappings={"map_1": _mapping("map_1", "src_1")},
            sources={"src_1": _source_profile("src_1")},
            frame=frame,
            columns=_periodic_columns(),
            entity_key="entity_key",
            snapshot_column="snapshot_date",
            target=None,
        )
    assert excinfo.value.code == "DATASET_SNAPSHOT_VALUE_INVALID"
    assert "snapshot_date" in str(excinfo.value)
    assert "last tuesday" not in str(excinfo.value)  # house rule 4: count the rows, never quote one


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
    # Every parent id is a node that is actually in the tree: `parents` is what the UI draws an
    # edge from, so an id with no card behind it is a broken diagram.
    drawn = {node.id for node in (*tree.sources, *tree.mappings, tree.spec, tree.dataset)}
    for node in (*tree.sources, *tree.mappings, tree.spec, tree.dataset):
        assert set(node.parents) <= drawn


def test_lineage_never_tells_a_single_snapshot_dataset_it_has_none() -> None:
    """A single-snapshot manifest deliberately records no dates - there is one snapshot and the
    build did not date it - so counting `snapshot_dates` would put "0 snapshot(s)" on the card of a
    dataset that has exactly one. The mode is the measured fact; the count is only a fact when the
    dataset is periodic."""
    mapping, source = _mapping("map_1", "src_1"), _source_profile("src_1")
    manifest = build_manifest(
        "ds_single",
        _spec("sp_single"),
        mappings={"map_1": mapping},
        sources={"src_1": source},
        frame=_frame(5),
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )
    detail = lineage(manifest, mappings={"map_1": mapping}, sources={"src_1": source}).dataset.detail
    assert "0 snapshot" not in detail
    assert "5 row(s)" in detail and "5 entities" in detail


def test_lineage_counts_the_snapshots_a_periodic_dataset_actually_has() -> None:
    mapping, source = _mapping("map_1", "src_1"), _source_profile("src_1")
    manifest = build_manifest(
        "ds_periodic",
        _spec("sp_periodic", mode=SnapshotMode.PERIODIC),
        mappings={"map_1": mapping},
        sources={"src_1": source},
        frame=_periodic_frame(5),
        columns=_periodic_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )
    detail = lineage(manifest, mappings={"map_1": mapping}, sources={"src_1": source}).dataset.detail
    assert "2 snapshot(s)" in detail


def test_lineage_shows_an_em_dash_for_a_role_nobody_settled() -> None:
    """`ui/dom.js` renders an em dash where nothing was measured; these cards are pre-formatted
    here, so the em dash has to be written here. A word like "unconfirmed" in its place would be a
    stand-in this module invented and the screen would read it as a finding."""
    mapping = _mapping("map_1", "src_1")
    source = _source_profile("src_1", role=None)
    manifest = build_manifest(
        "ds_role",
        _spec("sp_role"),
        mappings={"map_1": mapping},
        sources={"src_1": source},
        frame=_frame(3),
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )
    tree = lineage(manifest, mappings={"map_1": mapping}, sources={"src_1": source})
    assert tree.sources[0].detail.endswith("role —")


def test_lineage_refuses_a_mapping_that_reads_a_source_the_dataset_does_not_record() -> None:
    """The mapping was supplied, so neither "missing" check fires - but its `source_id` is not one
    of the manifest's sources, so its node's only parent would be an id no card in the tree carries.
    A dangling edge is the same gap as a blank card, wearing a different shape."""
    mapping, source = _mapping("map_1", "src_1"), _source_profile("src_1")
    manifest = build_manifest(
        "ds_dangling",
        _spec("sp_dangling"),
        mappings={"map_1": mapping},
        sources={"src_1": source},
        frame=_frame(3),
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
    )
    stray = _mapping("map_1", "src_elsewhere")
    with pytest.raises(DatasetError) as excinfo:
        lineage(manifest, mappings={"map_1": stray}, sources={"src_1": source})
    assert excinfo.value.code == "DATASET_LINEAGE_INCOMPLETE"
    assert "map_1" in str(excinfo.value)


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


# ---------------------------------------------------------------------------
# run_source_key
# ---------------------------------------------------------------------------
def _run(**source: str | None) -> RunRecord:
    return RunRecord(
        run_id="run_1",
        use_case_id="telco_churn",
        use_case_name="Telco churn",
        mode=RunMode.SCORE,
        state=RunState.DONE,
        created_at=NOW,
        file_name="source",
        primary_key="customer_id",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        model_choice="automl",
        engine_version="0.0.0-test",
        **source,
    )


def test_a_run_read_from_an_upload_goes_back_to_the_upload_source_file() -> None:
    assert run_source_key(_run(upload_id="up_1"), "csv") == upload_key("up_1", "source.csv")


def test_a_run_read_from_a_built_dataset_goes_back_to_the_dataset_frame() -> None:
    record = _run(dataset_id="ds_1", client_id="acme", dataset_fingerprint="abc")
    assert run_source_key(record, "parquet") == dataset_key("ds_1", DATASET_FRAME_FILENAME)


def test_a_run_naming_neither_source_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="neither an upload nor a dataset"):
        run_source_key(_run(), "csv")
