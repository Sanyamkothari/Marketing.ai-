"""The dataset registry and lineage helpers (Phase 2 plan §4, §8, M12).

A dataset is **immutable** and **traceable**. Once `LocalDatasetRegistry.write_frame` has run, the
rows behind a `dataset_id` never change; the whole point of this module is that, given nothing but
that id, a reader can recover exactly how those rows came to be - which source files, at which
fingerprints, went through which mappings, at which hashes, under which spec. `DatasetManifest`
(`engine/onboarding/specs.py`) is the document that says so; this module is what writes it, reads it
back, and turns it into the tree the UI's Lineage block renders.

Two halves:

*The registry* (`DatasetRegistry` protocol, `LocalDatasetRegistry`) owns one directory per dataset,
`datasets/<dataset_id>/`, mirroring the shape `engine/registry.py` and `engine/clients.py` already
use for their own stores: a protocol, one `Local*` implementation over `Storage`, and a coded
exception (`DatasetError`) carrying the id, so that everything about *which dataset was asked for*
- missing, blank, unusable as a directory name, filed under the wrong id - reaches a caller as a
code it can turn into a plain-language 404, never as the `StorageError('KEY_INVALID')` that
`engine.storage.validate_key` would otherwise raise several frames deeper. A storage fault that is
genuinely about storage (a full disk, an unreadable file) still surfaces as `StorageError`. Six
files live in that directory (`engine.onboarding.specs.DATASET_ARTEFACTS` is the exhaustive list):
three JSON documents this module round-trips as pydantic models (`dataset_manifest.json`,
`build_report.json`, `build_status.json`), the built table itself (`dataset.parquet`), a 50-row
preview of it for the review screen (`sample.json`), and the compiled feature SQL for debugging and
Phase 4 porting (`features.sql`).

*The lineage helpers* (`build_manifest`, `lineage`, `dataset_fingerprint_of`) are the part that earns
the module its name. `build_manifest` assembles a `DatasetManifest` from the recipe that was
executed, the mappings and source profiles it read, and the frame it produced; `lineage` turns a
built manifest, together with those same mappings and profiles, into a `Lineage` tree the UI walks
without computing anything itself (plan §2.1 principle 2: the UI renders, it never computes). Both
refuse, loudly, to describe a build they were not given the full story of - a manifest naming a
mapping this call was not handed is not a lineage with a gap in it, it is a lineage this module
cannot vouch for, and "nothing fabricated" (house rule 2) means it does not try.

Nothing here branches on a use-case id or a client id; every method reads its arguments and the
`Storage` it was built with, and nothing else.
"""

from __future__ import annotations

import io
import json
import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

from pydantic import Field

from engine import __version__
from engine.contracts import Artefact, DatasetFingerprint
from engine.onboarding.specs import (
    DATASET_ARTEFACTS,
    BuildReport,
    BuildStatus,
    DatasetColumn,
    DatasetManifest,
    MappingSpec,
    OnboardingSpec,
    SnapshotMode,
    SourceProfile,
)
from engine.onboarding.specs import spec_hash as compute_spec_hash
from engine.stages.ingest import MAX_CELL_CHARS, REDACTED, dataset_fingerprint
from engine.storage import Storage, StorageError, default_storage
from engine.utils.time import utc_now

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "DATASET_FEATURES_SQL_FILENAME",
    "DATASET_FRAME_FILENAME",
    "DATASET_MANIFEST_FILENAME",
    "DATASET_REPORT_FILENAME",
    "DATASET_SAMPLE_FILENAME",
    "DATASET_STATUS_FILENAME",
    "SAMPLE_ROWS",
    "DatasetError",
    "DatasetRegistry",
    "Lineage",
    "LineageNode",
    "LocalDatasetRegistry",
    "build_manifest",
    "dataset_fingerprint_of",
    "dataset_key",
    "default_dataset_registry",
    "lineage",
]

# ---------------------------------------------------------------------------
# The dataset directory: filenames, key and errors
# ---------------------------------------------------------------------------
DATASET_MANIFEST_FILENAME: Final[str] = "dataset_manifest.json"
DATASET_REPORT_FILENAME: Final[str] = "build_report.json"
DATASET_STATUS_FILENAME: Final[str] = "build_status.json"
DATASET_FRAME_FILENAME: Final[str] = "dataset.parquet"
DATASET_SAMPLE_FILENAME: Final[str] = "sample.json"
DATASET_FEATURES_SQL_FILENAME: Final[str] = "features.sql"
"""The six filenames `engine.onboarding.specs.DATASET_ARTEFACTS` lists; `test_datasets.py` pins the set."""

SAMPLE_ROWS: Final[int] = 50
"""`sample.json` holds exactly this many leading rows - enough for the review screen, small enough
that the Setup screen renders it without ever reading `dataset.parquet`."""

_DATASET_ID_PREFIX: Final[str] = "ds"
_DATASET_ID_TIMESTAMP: Final[str] = "%Y%m%dT%H%M%S%f"
"""Microsecond precision: the whole prefix, not just the date, so a directory listing of
`data/datasets/` sorts in build order the way `data/runs/` already does (`engine.utils.ids.new_run_id`)."""


def dataset_key(dataset_id: str, filename: str) -> str:
    """Key of one artefact inside a dataset directory - `run_key`/`upload_key`'s shape, one level up."""
    return f"datasets/{dataset_id}/{filename}"


def _dataset_prefix(dataset_id: str) -> str:
    return f"datasets/{dataset_id}/"


class DatasetError(Exception):
    """A dataset-registry or lineage operation failed.

    `code` is one of DATASET_NOT_FOUND | DATASET_ID_BLANK | DATASET_ID_INVALID |
    DATASET_ID_MISMATCH | DATASET_MAPPING_MISSING | DATASET_SOURCE_MISSING |
    DATASET_COLUMNS_MISMATCH | DATASET_ENTITY_KEY_MISSING | DATASET_SNAPSHOT_COLUMN_MISSING |
    DATASET_SNAPSHOT_VALUE_INVALID | DATASET_SAMPLE_UNREADABLE | DATASET_MAX_ROWS_INVALID |
    DATASET_LINEAGE_INCOMPLETE. `dataset_id` is set whenever the operation named one, exactly as
    `StorageError.key` and `RegistryError.model_id` do for their own stores.
    """

    def __init__(self, code: str, message: str, *, dataset_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.dataset_id = dataset_id


def _not_found(dataset_id: str) -> DatasetError:
    return DatasetError("DATASET_NOT_FOUND", f"No dataset {dataset_id!r}.", dataset_id=dataset_id)


def _require_id(dataset_id: str) -> str:
    """Return `dataset_id` when it can name a dataset directory, else raise a coded `DatasetError`.

    Every public registry method starts here. `datasets/<dataset_id>/<filename>` is a storage key,
    and `engine.storage.validate_key` rejects an empty segment or a `..` one with a
    `StorageError` - a code the onboarding API has no plain-language wording for and which, on the
    write side, nothing catches at all. An id that arrived blank from a path parameter, or carrying
    a separator, is a question about *which dataset*, so it is answered here in this module's own
    vocabulary before a key is ever built.

    `dataset_key` itself stays a pure string builder and is deliberately left free of this check:
    `api/routes/datasets.py` calls it directly and handles the `StorageError` that follows.
    """
    if not dataset_id.strip():
        raise DatasetError("DATASET_ID_BLANK", "A dataset id is required.", dataset_id=dataset_id)
    if any(char in dataset_id for char in "/\\\x00") or dataset_id in {".", ".."}:
        raise DatasetError(
            "DATASET_ID_INVALID",
            f"{dataset_id!r} cannot name a dataset directory; a dataset id carries no path separator.",
            dataset_id=dataset_id,
        )
    return dataset_id


def _check_id(dataset_id: str, other: str, *, what: str) -> None:
    """Refuse to file a document under one id when it says on its face it belongs to another."""
    if other != dataset_id:
        raise DatasetError(
            "DATASET_ID_MISMATCH",
            f"This {what} is for dataset {other!r}, not {dataset_id!r}.",
            dataset_id=dataset_id,
        )


# ---------------------------------------------------------------------------
# The lineage tree
# ---------------------------------------------------------------------------
class LineageNode(Artefact):
    """One block of the Lineage tree: one card the UI draws, already worded.

    `parents` is how the tree is a tree without four separate node types: a source has none, a
    mapping's parent is the source it reads, the spec's parents are every mapping it applies, and
    the dataset's parent is the spec that built it. `detail` is filled in here, not in
    `ui/modules/onboarding/**`, because "the UI renders, it never computes" (plan §2.1 principle 2)
    applies to a lineage card exactly as it applies to a KPI tile.
    """

    kind: Literal["source", "mapping", "spec", "dataset"] = Field(
        description="Which layer of the tree this node belongs to."
    )
    id: str = Field(description="Id of the source, mapping, spec or dataset this node describes.")
    label: str = Field(description="Short heading for the card.")
    detail: str = Field(description="Pre-formatted one-line summary, numbers already filled in.")
    parents: tuple[str, ...] = Field(default=(), description="Ids of the node(s) one layer up.")


class Lineage(Artefact):
    """`sources -> mappings -> spec -> dataset`, pre-formatted, for the UI's Lineage block."""

    dataset_id: str = Field(description="Dataset this lineage describes.")
    sources: tuple[LineageNode, ...] = Field(description="One node per source read, sorted by id.")
    mappings: tuple[LineageNode, ...] = Field(description="One node per mapping applied, sorted by id.")
    spec: LineageNode = Field(description="The recipe that tied the mappings to a build.")
    dataset: LineageNode = Field(description="The built dataset itself, the root of the tree.")


def _short_hash(value: str) -> str:
    """A hash trimmed for a one-line card; the full value is still on the underlying document."""
    return value if len(value) <= 18 else f"{value[:18]}…"


_UNKNOWN: Final[str] = "—"
"""What a card shows where nobody settled a value - `ui/dom.js`'s `EM_DASH`, written server-side
because these strings are pre-formatted here (house rule 2: never a stand-in that reads as a fact)."""


def lineage(
    manifest: DatasetManifest,
    *,
    mappings: Mapping[str, MappingSpec],
    sources: Mapping[str, SourceProfile],
) -> Lineage:
    """The renderable lineage tree of a built dataset.

    `mappings` and `sources` must cover everything `manifest` names - every key of
    `manifest.mapping_hashes` and `manifest.source_fingerprints` - or this refuses rather than
    drawing a card for a mapping or a source it was not handed a profile for: a lineage with a
    silently blank card is worse than an explicit error, because it looks complete. A mapping that
    reads a source the manifest does not record is refused for the same reason: `parents` is what
    the UI draws an edge from, so such a node would point the Lineage block at a card that is not
    in the tree, which is the same gap wearing a different shape.
    """
    missing_mappings = sorted(set(manifest.mapping_hashes) - set(mappings))
    if missing_mappings:
        raise DatasetError(
            "DATASET_LINEAGE_INCOMPLETE",
            f"Mapping(s) {', '.join(missing_mappings)} were not supplied; the lineage would be incomplete.",
            dataset_id=manifest.dataset_id,
        )
    missing_sources = sorted(set(manifest.source_fingerprints) - set(sources))
    if missing_sources:
        raise DatasetError(
            "DATASET_LINEAGE_INCOMPLETE",
            f"Source(s) {', '.join(missing_sources)} were not supplied; the lineage would be incomplete.",
            dataset_id=manifest.dataset_id,
        )
    dangling = sorted(
        mapping_id
        for mapping_id in manifest.mapping_hashes
        if mappings[mapping_id].source_id not in manifest.source_fingerprints
    )
    if dangling:
        raise DatasetError(
            "DATASET_LINEAGE_INCOMPLETE",
            f"Mapping(s) {', '.join(dangling)} read a source this dataset does not record; "
            "the tree would carry an edge to a card that is not in it.",
            dataset_id=manifest.dataset_id,
        )

    source_nodes = tuple(
        LineageNode(
            kind="source",
            id=source_id,
            label=sources[source_id].file_name,
            detail=f"{fingerprint.n_rows:,} row(s) · role {sources[source_id].role or _UNKNOWN}",
            parents=(),
        )
        for source_id, fingerprint in sorted(manifest.source_fingerprints.items(), key=lambda kv: kv[0])
    )
    mapping_nodes = tuple(
        LineageNode(
            kind="mapping",
            id=mapping_id,
            label=f"Mapping {mapping_id}",
            detail=f"{len(mappings[mapping_id].columns)} column(s) mapped · role {mappings[mapping_id].role}",
            parents=(mappings[mapping_id].source_id,),
        )
        for mapping_id in sorted(manifest.mapping_hashes)
    )
    spec_node = LineageNode(
        kind="spec",
        id=manifest.spec_id,
        label=f"Recipe {manifest.spec_id}",
        detail=(
            f"{len(manifest.mapping_hashes)} mapping(s) · {len(manifest.columns)} column(s) · "
            f"feature spec {_short_hash(manifest.feature_spec_hash)}"
        ),
        parents=tuple(sorted(manifest.mapping_hashes)),
    )
    entity_word = "entity" if manifest.n_entities == 1 else "entities"
    # A single-snapshot manifest records no dates at all (`build_manifest` leaves `snapshot_dates`
    # empty, because there is one snapshot and the build did not date it), so counting that tuple
    # would tell the card "0 snapshot(s)" about a dataset that has exactly one. The mode is the
    # measured fact here; the count is only a fact when the mode is periodic (house rule 2).
    snapshot_clause = (
        f"{len(manifest.snapshot_dates):,} snapshot(s)"
        if manifest.snapshot_mode is SnapshotMode.PERIODIC
        else "a single snapshot"
    )
    dataset_node = LineageNode(
        kind="dataset",
        id=manifest.dataset_id,
        label=f"Dataset {manifest.dataset_id}",
        detail=f"{manifest.n_rows:,} row(s) · {manifest.n_entities:,} {entity_word} · {snapshot_clause}",
        parents=(manifest.spec_id,),
    )
    return Lineage(
        dataset_id=manifest.dataset_id,
        sources=source_nodes,
        mappings=mapping_nodes,
        spec=spec_node,
        dataset=dataset_node,
    )


def dataset_fingerprint_of(frame: pd.DataFrame) -> DatasetFingerprint:
    """The identity of a built table - `engine.stages.ingest.dataset_fingerprint`, reused whole.

    There is exactly one fingerprinter in this engine (the docstring on `dataset_fingerprint` is
    explicit about that), and it is deliberately sensitive to row order as well as to cell content:
    two frames holding the same rows in a different order fingerprint differently. That property is
    inherited here rather than relaxed, on purpose. `write_frame` is the one and only writer of
    `dataset.parquet`, and a dataset is supposed to be *immutable*: "immutable" has to mean "this
    exact file, in this exact row order", because a caller that reads `dataset.parquet` twice and
    gets the same bytes back both times is the whole guarantee this module exists to give. Treating
    row order as part of identity is the stricter reading, and a second, laxer fingerprint scheme
    living next to the one Phase 1 already trusts would be one more thing to keep in sync for no
    benefit - `test_datasets.py` pins this by asserting a mere reorder changes the fingerprint the
    same way a changed cell does.
    """
    return dataset_fingerprint(frame)


def build_manifest(
    dataset_id: str,
    spec: OnboardingSpec,
    *,
    mappings: Mapping[str, MappingSpec],
    sources: Mapping[str, SourceProfile],
    frame: pd.DataFrame,
    columns: tuple[DatasetColumn, ...],
    entity_key: str,
    snapshot_column: str,
    target: str | None,
    built_at: datetime | None = None,
    engine_version: str = __version__,
) -> DatasetManifest:
    """Assemble a `DatasetManifest` from the recipe that ran, what it read, and what it produced.

    Every id `spec` names - `spec.mapping_ids`, `spec.entity_source_id`, `spec.event_source_ids` -
    must be a key of `mappings` / `sources`, and `columns` must name exactly the columns `frame`
    holds, in the order it holds them: a manifest is only as trustworthy as the check that it
    matches what was actually built, and this function is the one place that check happens, once,
    rather than trusted to every future caller.

    `primary_key` is derived, never accepted as an argument: `spec.snapshot_spec.mode` already says
    whether a row is one per entity or one per entity per snapshot, so a caller-supplied key could
    only ever be redundant with that or wrong about it. `DatasetManifest`'s own validator refuses a
    periodic key of the wrong width; this function is what has to get the width right in the first
    place, which is the whole reason M12 exists.
    """
    missing_mappings = sorted(set(spec.mapping_ids) - set(mappings))
    if missing_mappings:
        raise DatasetError(
            "DATASET_MAPPING_MISSING",
            f"The recipe names mapping(s) {', '.join(missing_mappings)}, which were not supplied.",
            dataset_id=dataset_id,
        )
    source_ids = (spec.entity_source_id, *spec.event_source_ids)
    missing_sources = sorted(set(source_ids) - set(sources))
    if missing_sources:
        raise DatasetError(
            "DATASET_SOURCE_MISSING",
            f"The recipe names source(s) {', '.join(missing_sources)}, which were not supplied.",
            dataset_id=dataset_id,
        )
    column_names = tuple(column.name for column in columns)
    frame_columns = tuple(str(name) for name in frame.columns)
    if column_names != frame_columns:
        raise DatasetError(
            "DATASET_COLUMNS_MISMATCH",
            f"The manifest lists columns {list(column_names)} but the built frame has {list(frame_columns)}.",
            dataset_id=dataset_id,
        )
    if entity_key not in frame.columns:
        raise DatasetError(
            "DATASET_ENTITY_KEY_MISSING",
            f"The built frame has no {entity_key!r} column to count entities by.",
            dataset_id=dataset_id,
        )

    snapshot_mode = spec.snapshot_spec.mode
    primary_key: tuple[str, ...]
    snapshot_dates: tuple[date, ...]
    if snapshot_mode is SnapshotMode.PERIODIC:
        if snapshot_column not in frame.columns:
            raise DatasetError(
                "DATASET_SNAPSHOT_COLUMN_MISSING",
                f"The built frame has no {snapshot_column!r} column for a periodic dataset.",
                dataset_id=dataset_id,
            )
        primary_key = (entity_key, snapshot_column)
        snapshot_dates = _snapshot_dates(frame, snapshot_column, dataset_id=dataset_id)
    else:
        primary_key = (entity_key,)
        snapshot_dates = ()

    return DatasetManifest(
        dataset_id=dataset_id,
        client_id=spec.client_id,
        use_case=spec.use_case,
        spec_id=spec.spec_id,
        spec_hash=spec.hash or compute_spec_hash(spec),
        mapping_hashes={
            mapping_id: (mappings[mapping_id].hash or compute_spec_hash(mappings[mapping_id]))
            for mapping_id in spec.mapping_ids
        },
        source_fingerprints={source_id: sources[source_id].fingerprint for source_id in source_ids},
        feature_spec_hash=spec.feature_spec.hash,
        snapshot_mode=snapshot_mode,
        primary_key=primary_key,
        target=target,
        columns=columns,
        n_rows=len(frame),
        n_entities=int(frame[entity_key].nunique()),
        snapshot_dates=snapshot_dates,
        fingerprint=dataset_fingerprint_of(frame),
        built_at=built_at or utc_now(),
        engine_version=engine_version,
    )


def _snapshot_dates(frame: pd.DataFrame, snapshot_column: str, *, dataset_id: str) -> tuple[date, ...]:
    """Every distinct snapshot date in `frame`, earliest first; a value it cannot read is refused.

    `snapshot_dates` is what the Lineage card and the Build review count snapshots from, so a value
    that does not read as a date must not simply fall out of the set: a periodic dataset holding one
    unreadable snapshot value would otherwise report one snapshot fewer than it has, and a count
    nobody measured presented as one that was is exactly what house rule 2 forbids. Coercing and
    then counting what was lost is the only way to tell the two apart.

    Stopping is right *here* even though "bad data is never an exception" holds one stage earlier
    (`engine.stages.ingest`): this column is not a client's file, it is what the snapshot planner
    just built, so an unreadable value in it is a fault in the build rather than a fact about the
    client worth reporting as a check. The message counts rows, never quotes one (house rule 4).
    """
    import pandas as pd

    column = frame[snapshot_column]
    parsed = pd.to_datetime(column, errors="coerce")
    unreadable = int((parsed.isna() & column.notna()).sum())
    if unreadable:
        raise DatasetError(
            "DATASET_SNAPSHOT_VALUE_INVALID",
            f"{unreadable} row(s) hold a {snapshot_column!r} value that is not a date, so the "
            "snapshots of this dataset cannot be listed. Rebuild it with the snapshot column cast "
            "to a date.",
            dataset_id=dataset_id,
        )
    return tuple(sorted({value.date() for value in parsed.dropna()}))


# ---------------------------------------------------------------------------
# Sample rows: stringified, PII-redacted, exactly as the review screen shows them
# ---------------------------------------------------------------------------
def _stringify_cell(value: object) -> str:
    """One sample cell as the review screen shows it: empty for null, ISO for a date, `str()` else.

    A local twin of `engine.stages.ingest._cell_str`, which answers the same question for the Setup
    preview but is module-private there and so not importable. The two are not kept in sync by a
    shared call because there is nothing to share - both exist to answer "what does one cell look
    like on a screen?", the same way twice, because that is what a screen wants. The one thing they
    *do* share is `MAX_CELL_CHARS`, imported rather than repeated: how long a cell may be before it
    is cut is one decision about one review surface, and two copies of it would drift into a Setup
    preview and a Build review that cut the same value at different places.
    """
    import pandas as pd

    if value is None or value is pd.NaT or value is pd.NA:
        return ""
    if pd.api.types.is_bool(value):
        return "true" if value else "false"
    if isinstance(value, pd.Timestamp):
        rendered = value.date().isoformat() if value == value.normalize() else value.isoformat()
    elif isinstance(value, float):
        if math.isnan(value):
            return ""
        rendered = str(int(value)) if value.is_integer() else str(value)
    elif isinstance(value, (datetime, date)):
        rendered = value.isoformat()
    else:
        rendered = str(value)
    if len(rendered) > MAX_CELL_CHARS:
        return rendered[: MAX_CELL_CHARS - 1] + "…"
    return rendered


def _sample_rows(frame: pd.DataFrame, *, pii_columns: frozenset[str]) -> list[dict[str, str]]:
    """The first `SAMPLE_ROWS` rows, stringified, a PII column's values replaced by `REDACTED`.

    Read column by column, never with `DataFrame.iterrows`: a row pulled out of a frame is a
    `Series`, so it has to have *one* dtype, and a frame of `int64` ids beside a `float64` amount -
    a built dataset's ordinary shape - hands every id back as a float. A customer id past 2**53 then
    reaches the review screen with different digits than went into the parquet, which is a fabricated
    number on a screen (house rule 2) and the hardest kind to notice, because it still looks like an
    id. Reading each column in its own dtype is what makes `sample.json` show what was built.
    """
    head = frame.head(SAMPLE_ROWS)
    rendered: list[tuple[str, list[str]]] = [
        (
            str(name),
            (
                [REDACTED] * len(head)
                if str(name) in pii_columns
                else [_stringify_cell(value) for value in head.iloc[:, position].tolist()]
            ),
        )
        for position, name in enumerate(head.columns)
    ]
    return [{name: values[index] for name, values in rendered} for index in range(len(head))]


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
@runtime_checkable
class DatasetRegistry(Protocol):
    """Where a built dataset's files live; `Storage`-backed now, unchanged when `Storage` grows S3."""

    def new_dataset_id(self, client_id: str, use_case: str) -> str: ...

    def write_status(self, dataset_id: str, status: BuildStatus) -> None: ...

    def read_status(self, dataset_id: str) -> BuildStatus: ...

    def write_manifest(self, dataset_id: str, manifest: DatasetManifest) -> None: ...

    def read_manifest(self, dataset_id: str) -> DatasetManifest: ...

    def write_report(self, dataset_id: str, report: BuildReport) -> None: ...

    def read_report(self, dataset_id: str) -> BuildReport: ...

    def write_frame(
        self, dataset_id: str, frame: pd.DataFrame, *, pii_columns: Iterable[str] = ()
    ) -> DatasetFingerprint: ...

    def read_frame(self, dataset_id: str, *, max_rows: int | None = None) -> pd.DataFrame: ...

    def write_features_sql(self, dataset_id: str, sql: str) -> str: ...

    def read_sample(self, dataset_id: str) -> list[dict[str, str]]: ...

    def exists(self, dataset_id: str) -> bool: ...

    def delete(self, dataset_id: str) -> None: ...


class LocalDatasetRegistry:
    """`DatasetRegistry` over a `Storage`: every method is `dataset_key` plus one storage call.

    No SQLite, unlike `LocalModelRegistry` and `LocalClientStore`: nothing here needs a query
    `Storage.list_keys` cannot already answer - "does this id exist" is "is its prefix non-empty",
    and "list every dataset" is `ClientStore.list_datasets`'s job (it indexes `DatasetManifest` by
    `client_id`/`use_case`, which this module deliberately does not need to know how to do twice).
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    @property
    def storage(self) -> Storage:
        """The store every dataset directory is written into."""
        return self._storage

    def new_dataset_id(self, client_id: str, use_case: str) -> str:
        """A new id: `ds_<UTC timestamp>[_<n>]`, sortable and *verified* collision-free.

        `client_id` and `use_case` are validated, not encoded: a dataset built for nobody in
        particular cannot be found by `ClientStore.list_datasets(client_id=...)` later, which is a
        bug worth catching here rather than three screens downstream. The id itself names neither,
        on purpose - it has one job, sorting `data/datasets/` into build order, and a client or
        use-case slug in the middle of it would not help that.

        Unlike `engine.utils.ids.new_run_id`, which trusts eight random hex characters not to
        collide within a day, this generator checks the store: a `dataset_id` is the only key a
        manifest can ever be recovered from, so a silent collision would mean the wrong lineage over
        the wrong parquet, not merely a `409` on the next call. The microsecond-precision timestamp
        already makes a collision vanishingly unlikely; the loop hands back only an id nothing has
        been written under, and terminates because only finitely many datasets share one prefix.

        What the loop cannot rule out is two callers minting in the same microsecond before either
        has written anything - nothing is reserved here, because a reservation would be a seventh
        file in a directory `DATASET_ARTEFACTS` says holds six. That race is caught one step later
        instead: every writer passes its document through `_check_id`, and a build files its
        `build_status.json` before it does anything else.
        """
        if not client_id.strip():
            raise DatasetError("DATASET_ID_BLANK", "A dataset id needs the client it was built for.")
        if not use_case.strip():
            raise DatasetError("DATASET_ID_BLANK", "A dataset id needs the use case it was built for.")
        prefix = f"{_DATASET_ID_PREFIX}_{utc_now().strftime(_DATASET_ID_TIMESTAMP)}"
        candidate = prefix
        attempt = 0
        while self.exists(candidate):
            attempt += 1
            candidate = f"{prefix}_{attempt}"
        return candidate

    def write_status(self, dataset_id: str, status: BuildStatus) -> None:
        _require_id(dataset_id)
        _check_id(dataset_id, status.dataset_id, what="build status")
        self._storage.write_model(dataset_key(dataset_id, DATASET_STATUS_FILENAME), status)

    def read_status(self, dataset_id: str) -> BuildStatus:
        _require_id(dataset_id)
        try:
            return self._storage.read_model(dataset_key(dataset_id, DATASET_STATUS_FILENAME), BuildStatus)
        except StorageError as exc:
            raise _not_found(dataset_id) from exc

    def write_manifest(self, dataset_id: str, manifest: DatasetManifest) -> None:
        _require_id(dataset_id)
        _check_id(dataset_id, manifest.dataset_id, what="dataset manifest")
        self._storage.write_model(dataset_key(dataset_id, DATASET_MANIFEST_FILENAME), manifest)

    def read_manifest(self, dataset_id: str) -> DatasetManifest:
        _require_id(dataset_id)
        try:
            return self._storage.read_model(
                dataset_key(dataset_id, DATASET_MANIFEST_FILENAME), DatasetManifest
            )
        except StorageError as exc:
            raise _not_found(dataset_id) from exc

    def write_report(self, dataset_id: str, report: BuildReport) -> None:
        _require_id(dataset_id)
        _check_id(dataset_id, report.dataset_id, what="build report")
        self._storage.write_model(dataset_key(dataset_id, DATASET_REPORT_FILENAME), report)

    def read_report(self, dataset_id: str) -> BuildReport:
        _require_id(dataset_id)
        try:
            return self._storage.read_model(dataset_key(dataset_id, DATASET_REPORT_FILENAME), BuildReport)
        except StorageError as exc:
            raise _not_found(dataset_id) from exc

    def write_frame(
        self, dataset_id: str, frame: pd.DataFrame, *, pii_columns: Iterable[str] = ()
    ) -> DatasetFingerprint:
        """Write `dataset.parquet` and `sample.json`, and return the table's fingerprint.

        The fingerprint is computed from `frame` itself, not read back from the file just written:
        a `Storage` round-trip is not part of what identifies a dataset, and computing it once here
        means a caller never has to read the whole parquet file back just to learn its own identity.
        """
        _require_id(dataset_id)
        buffer = io.BytesIO()
        frame.to_parquet(buffer, engine="pyarrow", index=False)
        self._storage.write_bytes(dataset_key(dataset_id, DATASET_FRAME_FILENAME), buffer.getvalue())
        sample = _sample_rows(frame, pii_columns=frozenset(pii_columns))
        self._storage.write_text(
            dataset_key(dataset_id, DATASET_SAMPLE_FILENAME), json.dumps(sample, indent=2) + "\n"
        )
        return dataset_fingerprint_of(frame)

    def read_frame(self, dataset_id: str, *, max_rows: int | None = None) -> pd.DataFrame:
        """The built table, optionally only its first `max_rows` rows.

        A negative `max_rows` is refused rather than passed to `head`, which reads it as "all but
        the last n" and would hand a caller that meant "no rows" a frame missing its tail with
        nothing to say so.
        """
        import pandas as pd

        _require_id(dataset_id)
        if max_rows is not None and max_rows < 0:
            raise DatasetError(
                "DATASET_MAX_ROWS_INVALID",
                f"A row limit cannot be negative; {max_rows} was asked for.",
                dataset_id=dataset_id,
            )
        key = dataset_key(dataset_id, DATASET_FRAME_FILENAME)
        if not self._storage.exists(key):
            raise _not_found(dataset_id)
        frame = pd.read_parquet(self._storage.local_path(key), engine="pyarrow")
        return frame if max_rows is None else frame.head(max_rows)

    def write_features_sql(self, dataset_id: str, sql: str) -> str:
        """Write the compiled feature SQL and return its storage key."""
        _require_id(dataset_id)
        key = dataset_key(dataset_id, DATASET_FEATURES_SQL_FILENAME)
        self._storage.write_text(key, sql)
        return key

    def read_sample(self, dataset_id: str) -> list[dict[str, str]]:
        """The rows `write_frame` put in `sample.json`, exactly as it wrote them."""
        _require_id(dataset_id)
        key = dataset_key(dataset_id, DATASET_SAMPLE_FILENAME)
        try:
            text = self._storage.read_text(key)
        except StorageError as exc:
            raise _not_found(dataset_id) from exc
        try:
            rows: list[dict[str, str]] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DatasetError(
                "DATASET_SAMPLE_UNREADABLE",
                f"The stored sample of dataset {dataset_id!r} is not readable. Rebuild the dataset.",
                dataset_id=dataset_id,
            ) from exc
        return rows

    def exists(self, dataset_id: str) -> bool:
        """Whether any artefact has been written for this id - any of the six, in any order.

        Six `Storage.exists` calls rather than one `list_keys`: `DATASET_ARTEFACTS` is the
        exhaustive list of what a dataset directory ever holds, so the two answer the same question,
        and `LocalStorage.list_keys` answers it by walking the *whole* data directory and filtering
        by prefix - a cost `new_dataset_id` would pay on every single mint, growing with every run,
        upload and model the store has ever held.
        """
        _require_id(dataset_id)
        return any(self._storage.exists(dataset_key(dataset_id, name)) for name in DATASET_ARTEFACTS)

    def delete(self, dataset_id: str) -> None:
        """Remove every artefact of `dataset_id`; deleting an id nothing was ever written for is a no-op.

        Prefix-based, unlike `exists`: deleting is the one operation that must also carry off a file
        this module did not put there, so that a directory a future artefact once occupied does not
        outlive the dataset it belonged to.
        """
        _require_id(dataset_id)
        for key in self._storage.list_keys(_dataset_prefix(dataset_id)):
            self._storage.delete(key)


def default_dataset_registry() -> LocalDatasetRegistry:
    """The process-wide default dataset registry, over the same store every other artefact uses."""
    return LocalDatasetRegistry(default_storage())
