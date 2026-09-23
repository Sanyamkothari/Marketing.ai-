"""The run index: a table that makes `runs/*/run.json` *findable*, and never replaces it.

Phase 1 answers "show me the last twenty runs" by listing every key under `runs/` and reading each
`run.json` back. On a filesystem that is a directory walk. On S3 it is a `ListObjectsV2` paging
over every object of every run - the artefacts too, not just the records - followed by one `GetObject`
per candidate. The cost grows with the number of *artefacts* in the bucket, which is the wrong thing
for it to grow with, and it grows forever.

The fix is an index, and the word is meant literally. `run.json` stays the artefact of record: it is
written first, it is what `GET /runs/{id}` reads, and it is what a rebuild reads *from*. The table
holds a projection of it that is good enough to filter and order a list page, and nothing else. Three
rules keep it honest (DEC-342):

1. **The row is written after the document, never before.** A row that exists names a run whose
   `run.json` is already there.
2. **A failed row write never fails a run.** `mirror_run` returns `False` and logs; the run is
   finished and its record is safe, and a database that was down for an hour costs a list page its
   completeness, not a customer their training run.
3. **The table is disposable.** `scripts/reconcile_runs.py` rebuilds it from `runs/*/run.json`, so
   the worst case of rules 1 and 2 is a command someone runs, not data that is gone.

This module is deliberately **table-free and driver-free**: no SQLModel table, no SQLAlchemy engine,
no boto3. `engine/pipeline.py` imports `mirror_run` on every path including a laptop's, and it may
not pay for a database driver to do it - on the local backend the index is simply `None` and
`mirror_run` returns `False` without touching anything. The SQL implementation lives in
`engine/aws/postgres.py`, behind the `RunIndex` protocol below.
"""

from __future__ import annotations

from datetime import datetime  # a runtime import: pydantic resolves this module's annotations
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from pydantic import Field

from engine.config import RunMode, StrictBase
from engine.contracts import RunManifest, RunRecord, RunState
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "RUNS_PREFIX",
    "RUN_FILENAME",
    "ReconcileReport",
    "RunIndex",
    "RunIndexEntry",
    "index_entry",
    "mirror_run",
    "reconcile_runs",
]

_LOGGER = get_logger(__name__)

RUNS_PREFIX: Final[str] = "runs/"
"""Where run directories live; the same prefix `api/routes/runs.py` lists today."""

RUN_FILENAME: Final[str] = "run.json"
"""The artefact of record. Spelled here as well so this module needs nothing from the API layer."""


class RunIndexEntry(StrictBase):
    """One indexed run: the fields a list page filters, orders and renders a row from.

    Every value is copied from `run.json`, except the last three, which come from
    `run_manifest.json` when the run wrote one. Nothing here is computed: an index that derives a
    number would be a second opinion about a run, and there is only ever one (DEC-342).

    What is *not* here is as deliberate as what is. `overrides` and `artefacts` are maps whose shape
    is the run's business and whose size is unbounded, and no list page uses them; a caller that
    wants them reads `run.json`, which is exactly the call this index exists to make findable.
    """

    run_id: str = Field(description="Run this entry indexes; the primary key.")
    use_case_id: str = Field(description="Use case the run belongs to; the list page filters on it.")
    use_case_name: str = Field(description="Display name, copied so a list page renders without configs.")
    mode: RunMode = Field(description="train or score; the list page's other filter.")
    state: RunState = Field(description="Overall run state as `run.json` last recorded it.")
    created_at: datetime = Field(description="UTC time the run record was created; the sort key.")
    started_at: datetime | None = Field(default=None, description="UTC time the first stage started.")
    finished_at: datetime | None = Field(default=None, description="UTC time the run reached a final state.")
    upload_id: str = Field(
        description="Upload the run consumed, or the built dataset's id for a dataset run."
    )
    file_name: str = Field(description="The user's original file name, as the Results bar shows it.")
    row_count: int | None = Field(default=None, description="Rows in the upload; null when unknown.")
    model_version_id: str | None = Field(default=None, description="Model produced (train) or used (score).")
    headline_metric: str | None = Field(default=None, description="Metric id behind the headline score.")
    headline_score: float | None = Field(default=None, description="Headline test score; null when none.")
    champion: bool = Field(default=False, description="Whether this run's model is, or became, champion.")
    error_code: str | None = Field(default=None, description="Failure code; null unless the run failed.")
    engine_version: str = Field(description="Engine version that produced the run.")
    duration_s: float | None = Field(
        default=None, description="Wall-clock seconds, from run_manifest.json; null without a manifest."
    )
    compute_backend: str | None = Field(
        default=None, description="What ran the job, from run_manifest.json; null when it reported nothing."
    )
    estimated_usd: float | None = Field(
        default=None,
        description=(
            "The manifest's estimate, copied verbatim, and null whenever the manifest declined to "
            "state one. Read it only with the manifest's `basis`, which says it is a list price and "
            "not a bill (DEC-330); this column carries the number, not the caveat."
        ),
    )
    indexed_at: datetime = Field(description="UTC time this row was written; a rebuild moves it.")


class ReconcileReport(StrictBase):
    """What one rebuild of the index did. Counts only - a report is not a log of customer files."""

    scanned: int = Field(description="`run.json` documents found under the runs prefix.")
    indexed: int = Field(description="Rows written or refreshed.")
    unreadable: int = Field(description="Documents that could not be read or did not validate.")
    forgotten: int = Field(description="Rows dropped because their document is gone.")
    failed: int = Field(description="Documents read successfully whose row could not be written.")


@runtime_checkable
class RunIndex(Protocol):
    """A store that can answer "which runs, newest first" without reading every run.

    Small on purpose. There is no `search`, no aggregate and no join: every question this protocol
    answers is one a list page asks, and anything more would be the table starting to become the
    record rather than the index of it (DEC-342).

    `list_run_ids` returns *ids*, not entries, because `GET /runs` returns `RunRecord` documents and
    those must come from storage. The index chooses which twenty runs are on the page; `run.json`
    still says what each of them is.
    """

    def upsert(self, entry: RunIndexEntry) -> None: ...

    def get(self, run_id: str) -> RunIndexEntry | None: ...

    def list_run_ids(
        self, *, limit: int, use_case_id: str | None = None, mode: RunMode | None = None
    ) -> tuple[str, ...]: ...

    def run_ids(self) -> tuple[str, ...]: ...

    def forget(self, run_id: str) -> None: ...


def index_entry(record: RunRecord, manifest: RunManifest | None = None) -> RunIndexEntry:
    """The row `record` projects to, with the manifest's three extras when there is one.

    A pure function with no store in sight, so the projection can be tested on its own and so
    `reconcile_runs` and `mirror_run` cannot disagree about what a row contains.
    """
    return RunIndexEntry(
        run_id=record.run_id,
        use_case_id=record.use_case_id,
        use_case_name=record.use_case_name,
        mode=record.mode,
        state=record.state,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        upload_id=record.upload_id or record.dataset_id or record.run_id,
        file_name=record.file_name,
        row_count=record.row_count,
        model_version_id=record.model_version_id,
        headline_metric=None if record.headline_metric is None else str(record.headline_metric),
        headline_score=record.headline_score,
        champion=record.champion,
        error_code=None if record.error is None else record.error.code,
        engine_version=record.engine_version,
        duration_s=None if manifest is None else manifest.duration_s,
        compute_backend=None if manifest is None or manifest.compute is None else manifest.compute.backend,
        estimated_usd=None if manifest is None else manifest.cost_estimate.estimated_usd,
        indexed_at=utc_now(),
    )


def mirror_run(index: RunIndex | None, record: RunRecord, manifest: RunManifest | None = None) -> bool:
    """Write `record`'s row into the index. Returns whether it was written, and never raises.

    Call this **after** `run.json` has been written, never before: the index promises that a row
    names a document that exists, and the only way to keep that promise is the order of the two
    writes (DEC-342).

    `index=None` is the local deployment, which has no table, and it returns `False` without
    touching anything - that is the case `engine/pipeline.py` is in on a laptop. Everything else
    that can go wrong is caught here on purpose. A run that has finished has already produced the
    thing the customer asked for; losing the database connection at that moment must cost a list
    page its completeness and nothing more, and `scripts/reconcile_runs.py` puts even that back. The
    failure is logged with the exception's class name and never its message (plan section 13.7), and
    the run id is an id the engine minted, not anything the customer typed.
    """
    if index is None:
        return False
    try:
        index.upsert(index_entry(record, manifest))
    except Exception as exc:
        log_failure(_LOGGER, f"run_index.mirror run={record.run_id}", exc)
        return False
    return True


def reconcile_runs(storage: Storage, index: RunIndex, *, prefix: str = RUNS_PREFIX) -> ReconcileReport:
    """Rebuild the index from `runs/*/run.json`, which is the only thing that was ever authoritative.

    This is the expensive listing the index exists to avoid, run deliberately and rarely instead of
    on every list page: after an outage that lost some rows, after a restore, or the first time the
    table is introduced to a bucket that already has runs in it.

    It is idempotent and additive-then-subtractive: every document found is upserted, and only then
    are rows whose document is gone dropped. A document that cannot be read or does not validate is
    counted and skipped, never deleted - an index has no business deciding that an unreadable run
    did not happen.

    The manifest is deliberately not read. A rebuild that fetched `run_manifest.json` for every run
    would double an already expensive scan to recover three optional columns; the rows it rewrites
    therefore have `duration_s`, `compute_backend` and `estimated_usd` back at null, which is the
    honest value for "this row was rebuilt from the record alone".
    """
    scanned = indexed = unreadable = failed = 0
    seen: set[str] = set()
    for key in storage.list_keys(prefix):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        scanned += 1
        try:
            record = storage.read_model(key, RunRecord)
        except Exception as exc:
            log_failure(_LOGGER, "run_index.unreadable", exc)
            unreadable += 1
            continue
        seen.add(record.run_id)
        try:
            index.upsert(index_entry(record))
        except Exception as exc:
            log_failure(_LOGGER, f"run_index.upsert run={record.run_id}", exc)
            failed += 1
            continue
        indexed += 1
    forgotten = 0
    for run_id in index.run_ids():
        if run_id in seen:
            continue
        try:
            index.forget(run_id)
        except Exception as exc:
            log_failure(_LOGGER, f"run_index.forget run={run_id}", exc)
            continue
        forgotten += 1
    report = ReconcileReport(
        scanned=scanned, indexed=indexed, unreadable=unreadable, forgotten=forgotten, failed=failed
    )
    _LOGGER.info(
        "run_index.reconcile scanned=%d indexed=%d unreadable=%d forgotten=%d failed=%d",
        report.scanned,
        report.indexed,
        report.unreadable,
        report.forgotten,
        report.failed,
    )
    return report
