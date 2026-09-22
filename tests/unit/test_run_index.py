"""`engine.aws.run_index` and its SQL implementation: an index of `run.json`, never a replacement.

The rules being tested are the three the module docstring states (DEC-342): the row is written
after the document, a failed row write never fails a run, and the table can be rebuilt from storage.
The last one is what makes the first two affordable, so it gets the most tests.

The store tests run on SQLite and, when a server is reachable, on PostgreSQL, for the same reason
`test_registry.py` does: the index is one class over two engines and a suite that only saw one of
them could not tell.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import create_engine

from engine.aws.postgres import RUN_TABLE, SqlRunIndex, create_run_index_tables
from engine.aws.run_index import (
    RUNS_PREFIX,
    RunIndex,
    RunIndexEntry,
    index_entry,
    mirror_run,
    reconcile_runs,
)
from engine.config import ProblemType, RunMode
from engine.contracts import (
    ComputeBackend,
    ComputeInfo,
    CostEstimate,
    DatasetFingerprint,
    RunError,
    RunManifest,
    RunRecord,
    RunState,
)
from engine.storage import LocalStorage, run_key
from scripts.reconcile_runs import EXIT_NO_INDEX, DryRunIndex, main
from tests.fixtures.postgres import (  # noqa: F401 - imported so pytest can resolve them by name
    postgres_engine_fixture,
    postgres_run_index,
    postgres_schema,
    postgres_url_value,
)

USE_CASE: str = "a-use-case"


def make_record(
    run_id: str,
    *,
    use_case_id: str = USE_CASE,
    mode: RunMode = RunMode.TRAIN,
    state: RunState = RunState.DONE,
    minutes: int = 0,
    error: RunError | None = None,
) -> RunRecord:
    """A complete `run.json` with no fabricated numbers beyond the ones a test compares."""
    created = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return RunRecord(
        run_id=run_id,
        use_case_id=use_case_id,
        use_case_name="A Use Case",
        mode=mode,
        state=state,
        created_at=created,
        started_at=created,
        finished_at=created + timedelta(minutes=1),
        upload_id=f"u_{run_id}",
        file_name="customers.csv",
        row_count=1000,
        primary_key="customer_id",
        target="churned" if mode is RunMode.TRAIN else None,
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        model_choice="automl",
        model_version_id=f"m_{run_id}",
        error=error,
        engine_version="0.1.0",
    )


def make_manifest(run_id: str) -> RunManifest:
    """A manifest carrying the three columns a record alone cannot fill."""
    return RunManifest(
        run_id=run_id,
        primary_key="customer_id",
        dataset_fingerprint=DatasetFingerprint(
            hash="0" * 64, algorithm="sha256", n_rows=1000, columns=("customer_id", "churned")
        ),
        seed=7,
        duration_s=12.5,
        cost_estimate=CostEstimate(compute_seconds=12.5, basis="Local run: nothing was billed."),
        # A local run: `ComputeBackend` has two members and `thread` is not one of them - the
        # manifest records *what carried the run*, and a thread pool is this process.
        compute=ComputeInfo(backend=ComputeBackend.LOCAL, duration_s=12.5),
        created_at=datetime(2026, 9, 22, 12, 1, 0, tzinfo=UTC),
    )


class BrokenIndex:
    """A `RunIndex` where every write fails, for proving that a failed write is not a failed run."""

    def __init__(self) -> None:
        self.attempts = 0

    def upsert(self, entry: RunIndexEntry) -> None:
        del entry
        self.attempts += 1
        raise RuntimeError("the database is not there")

    def get(self, run_id: str) -> RunIndexEntry | None:
        del run_id
        return None

    def list_run_ids(
        self, *, limit: int, use_case_id: str | None = None, mode: RunMode | None = None
    ) -> tuple[str, ...]:
        del limit, use_case_id, mode
        return ()

    def run_ids(self) -> tuple[str, ...]:
        return ()

    def forget(self, run_id: str) -> None:
        del run_id


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ]
)
def index(request: pytest.FixtureRequest, tmp_path: Path) -> SqlRunIndex:
    """The index under test: a SQLite file, or a migrated Postgres schema of this test's own."""
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'index.db'}")
        create_run_index_tables(engine)
        return SqlRunIndex(engine)
    built: SqlRunIndex = request.getfixturevalue("postgres_run_index")
    return built


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    """A store to write `run.json` documents into, exactly where the API writes them."""
    return LocalStorage(tmp_path / "data")


def write_run(store: LocalStorage, record: RunRecord) -> None:
    """Write one run's document where `reconcile_runs` will look for it."""
    store.write_model(run_key(record.run_id, "run.json"), record)


# ---------------------------------------------------------------------------
# The module keeps no table and loads no driver (DEC-342)
# ---------------------------------------------------------------------------
def test_run_index_is_table_free_and_driver_free() -> None:
    """`engine/pipeline.py` imports this on the local path and must not pay for a database.

    Asserted on the import lines rather than on `sys.modules`, because the test process has
    imported SQLAlchemy for other reasons and would pass either way.
    """
    import engine.aws.run_index as module

    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith((" ", "\t"))
    ]
    for line in imports:
        for forbidden in ("sqlmodel", "sqlalchemy", "psycopg", "boto3", "alembic"):
            assert forbidden not in line, line
    assert "table=True" not in source


def test_mirror_run_on_a_local_deployment_writes_nothing_and_says_so() -> None:
    """`index=None` is the laptop. It is not an error and it is not a lie about having written."""
    assert mirror_run(None, make_record("r_1")) is False


def test_a_failed_row_write_never_fails_a_run() -> None:
    """Rule 2 of DEC-342, tested on the worst case: the store raises on every call."""
    broken = BrokenIndex()
    assert mirror_run(broken, make_record("r_1")) is False
    assert broken.attempts == 1


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------
def test_the_projection_copies_the_record_and_computes_nothing() -> None:
    record = make_record("r_1")
    entry = index_entry(record)
    assert entry.run_id == record.run_id
    assert entry.use_case_id == record.use_case_id
    assert entry.mode is record.mode
    assert entry.state is record.state
    assert entry.created_at == record.created_at
    assert entry.file_name == record.file_name
    assert entry.error_code is None
    assert (entry.duration_s, entry.compute_backend, entry.estimated_usd) == (None, None, None)


def test_the_manifest_fills_the_three_columns_a_record_cannot() -> None:
    entry = index_entry(make_record("r_1"), make_manifest("r_1"))
    assert entry.duration_s == 12.5
    assert entry.compute_backend == "local"
    assert entry.estimated_usd is None  # a local run was not billed; DEC-330 forbids a zero


def test_a_failed_run_is_indexed_with_its_code() -> None:
    record = make_record(
        "r_1", state=RunState.FAILED, error=RunError(code="SCHEMA_MISMATCH", message="x", stage=None)
    )
    entry = index_entry(record)
    assert entry.state is RunState.FAILED
    assert entry.error_code == "SCHEMA_MISMATCH"


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def test_a_row_round_trips(index: SqlRunIndex) -> None:
    entry = index_entry(make_record("r_1"), make_manifest("r_1"))
    index.upsert(entry)
    stored = index.get("r_1")
    assert stored == entry


def test_an_unknown_run_is_none_rather_than_an_error(index: SqlRunIndex) -> None:
    assert index.get("r_absent") is None


def test_writing_the_same_run_twice_replaces_the_row(index: SqlRunIndex) -> None:
    """A rebuild calls `upsert` for runs that are already there; it must not need an empty table."""
    index.upsert(index_entry(make_record("r_1", state=RunState.RUNNING)))
    index.upsert(index_entry(make_record("r_1", state=RunState.DONE)))
    stored = index.get("r_1")
    assert stored is not None
    assert stored.state is RunState.DONE
    assert index.run_ids() == ("r_1",)


def test_the_list_is_newest_first_and_filters_the_way_the_run_list_does(index: SqlRunIndex) -> None:
    index.upsert(index_entry(make_record("r_1", minutes=0)))
    index.upsert(index_entry(make_record("r_2", minutes=5, mode=RunMode.SCORE)))
    index.upsert(index_entry(make_record("r_3", minutes=10, use_case_id="another")))
    assert index.list_run_ids(limit=10) == ("r_3", "r_2", "r_1")
    assert index.list_run_ids(limit=10, use_case_id=USE_CASE) == ("r_2", "r_1")
    assert index.list_run_ids(limit=10, mode=RunMode.SCORE) == ("r_2",)
    assert index.list_run_ids(limit=1) == ("r_3",)
    assert index.list_run_ids(limit=10, use_case_id="no-such-use-case") == ()


def test_timestamps_come_back_utc_aware(index: SqlRunIndex) -> None:
    """DEC-339 for the run table: the same instant, wearing UTC, on either backend."""
    kolkata = timezone(timedelta(hours=5, minutes=30))
    record = make_record("r_1")
    entry = index_entry(record).model_copy(
        update={"created_at": datetime(2026, 9, 22, 17, 30, tzinfo=kolkata)}
    )
    index.upsert(entry)
    stored = index.get("r_1")
    assert stored is not None
    assert stored.created_at == datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    assert stored.created_at.utcoffset() == timedelta(0)
    assert stored.indexed_at.utcoffset() == timedelta(0)


def test_forgetting_a_run_that_is_not_there_is_not_an_error(index: SqlRunIndex) -> None:
    index.forget("r_absent")
    assert index.run_ids() == ()


def test_mirror_run_writes_the_row(index: SqlRunIndex) -> None:
    assert mirror_run(index, make_record("r_1"), make_manifest("r_1")) is True
    stored = index.get("r_1")
    assert stored is not None
    assert stored.duration_s == 12.5


def test_the_index_satisfies_the_protocol(index: SqlRunIndex) -> None:
    assert isinstance(index, RunIndex)


def test_the_sqlite_index_holds_only_the_run_table(tmp_path: Path) -> None:
    """DEC-340 from the other side: creating a run index must not conjure a registry alongside it."""
    path = tmp_path / "only-run.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        create_run_index_tables(engine)
        with sqlite3.connect(path) as connection:
            names = {
                row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert names == {RUN_TABLE}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Rebuilding from storage (rule 3)
# ---------------------------------------------------------------------------
def test_a_rebuild_indexes_every_run_document(index: SqlRunIndex, storage: LocalStorage) -> None:
    for number in range(3):
        write_run(storage, make_record(f"r_{number}", minutes=number))
    report = reconcile_runs(storage, index)
    assert (report.scanned, report.indexed, report.unreadable, report.failed) == (3, 3, 0, 0)
    assert index.list_run_ids(limit=10) == ("r_2", "r_1", "r_0")


def test_a_rebuild_is_idempotent(index: SqlRunIndex, storage: LocalStorage) -> None:
    write_run(storage, make_record("r_1"))
    reconcile_runs(storage, index)
    second = reconcile_runs(storage, index)
    assert second.indexed == 1
    assert second.forgotten == 0
    assert index.run_ids() == ("r_1",)


def test_a_rebuild_drops_a_row_whose_document_is_gone(index: SqlRunIndex, storage: LocalStorage) -> None:
    """The index is not a graveyard: a run whose document was deleted is no longer a run."""
    index.upsert(index_entry(make_record("r_deleted")))
    write_run(storage, make_record("r_kept"))
    report = reconcile_runs(storage, index)
    assert report.forgotten == 1
    assert index.run_ids() == ("r_kept",)


def test_a_rebuild_counts_an_unreadable_document_and_keeps_going(
    index: SqlRunIndex, storage: LocalStorage
) -> None:
    """An index has no business deciding that a run it cannot parse did not happen."""
    write_run(storage, make_record("r_good"))
    storage.write_text(run_key("r_broken", "run.json"), "{not json")
    report = reconcile_runs(storage, index)
    assert (report.scanned, report.indexed, report.unreadable) == (2, 1, 1)
    assert index.run_ids() == ("r_good",)


def test_a_rebuild_ignores_everything_that_is_not_a_run_document(
    index: SqlRunIndex, storage: LocalStorage
) -> None:
    write_run(storage, make_record("r_1"))
    storage.write_text(run_key("r_1", "status.json"), "{}")
    storage.write_text(run_key("r_1", "model/predictor.pkl"), "not json either")
    report = reconcile_runs(storage, index)
    assert report.scanned == 1


def test_a_rebuild_of_an_empty_store_reports_nothing_rather_than_failing(
    index: SqlRunIndex, storage: LocalStorage
) -> None:
    report = reconcile_runs(storage, index)
    assert (report.scanned, report.indexed, report.forgotten) == (0, 0, 0)


def test_the_rebuild_reads_the_prefix_it_is_given(index: SqlRunIndex, storage: LocalStorage) -> None:
    write_run(storage, make_record("r_1"))
    assert reconcile_runs(storage, index, prefix=RUNS_PREFIX).scanned == 1
    assert reconcile_runs(storage, index, prefix="uploads/").scanned == 0


# ---------------------------------------------------------------------------
# scripts/reconcile_runs.py
# ---------------------------------------------------------------------------
def test_a_dry_run_reads_the_real_index_and_writes_nothing(index: SqlRunIndex, storage: LocalStorage) -> None:
    index.upsert(index_entry(make_record("r_gone")))
    write_run(storage, make_record("r_1"))
    dry = DryRunIndex(index)
    report = reconcile_runs(storage, dry)
    assert (report.indexed, report.forgotten) == (1, 1)
    assert (dry.upserts, dry.forgets) == (1, 1)
    assert index.run_ids() == ("r_gone",)
    assert index.get("r_1") is None


def test_the_command_says_plainly_that_a_sqlite_deployment_has_no_index(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2, not 0: "there was nothing to do" and "there is no table here" are different answers."""
    monkeypatch.delenv("MARKETING_AI_METADATA_BACKEND", raising=False)
    monkeypatch.delenv("MARKETING_AI_DATABASE_URL", raising=False)
    assert main([]) == EXIT_NO_INDEX
    assert "no run index" in capsys.readouterr().err
