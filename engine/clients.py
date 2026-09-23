"""The client metadata store: `ClientStore` protocol and `LocalClientStore`, its SQLite implementation.

Modelled closely on `engine/registry.py` (Phase 1's model registry): one connection per instance
guarded by a single lock, one transaction per write, a `_require` helper that turns a missing row
into a typed error, and a factory that names every code its errors can carry. It parts ways with
`registry.py` on the storage layer itself - this store uses the standard library's `sqlite3` module
directly rather than SQLModel, because every row here is an opaque JSON document (a `ClientRecord`,
a `SourceSpec`, a `MappingSpec`, ...) rather than a set of typed columns a query needs to filter or
sort by; the handful of columns this module does need to filter or sort by (`client_id`, `use_case`,
`created_at`) are pulled out alongside the document, and the document itself is re-validated on
every read (plan section 6, M8), so a Pydantic schema change is caught here rather than surfacing as
a mysterious `AttributeError` three modules away.

Five tables, one per artefact kind this module owns: `clients`, `sources`, `mappings`, `specs`,
`datasets`. Every table is `(id TEXT PRIMARY KEY, ..., created_at TEXT NOT NULL, document TEXT NOT
NULL)`; `created_at` is stored as a UTC ISO-8601 string, which sorts lexicographically in
chronological order, so "newest first" is a plain `ORDER BY created_at DESC, id DESC` - the id
tie-break makes the order deterministic even when two rows share a timestamp (SQLite's clock
resolution is coarser than two writes in the same transaction batch can be).

Every id this module hands out (only `client_id`; every other id already lives on the artefact the
caller passes in, minted upstream by whichever stage built it) is stable, sortable and
collision-free: `c_<slug>_<n>`, where `<slug>` is the client name normalised to lowercase words
joined by underscores and `<n>` is one more than the highest suffix already used for that slug,
read and written inside the same transaction so two concurrent `create_client` calls for "Acme"
can never both claim `c_acme_1`.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from engine.contracts import dump_artefact
from engine.onboarding.specs import ClientRecord, DatasetManifest, MappingSpec, OnboardingSpec, SourceSpec
from engine.utils.time import utc_now

__all__ = ["ClientStore", "ClientStoreError", "LocalClientStore"]


class ClientStoreError(Exception):
    """A client-store operation failed. `code` is one of NOT_FOUND | DUPLICATE | CLIENT_MISMATCH.

    `entity` names the kind of record involved (client, source, mapping, spec, dataset) and
    `entity_id` the id, when the operation had one to name; together they let a caller build its
    own business-language surface without parsing `message`, exactly as `RegistryError.model_id`
    and `StorageError.key` do for their stores.
    """

    def __init__(
        self, code: str, message: str, *, entity: str | None = None, entity_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.entity = entity
        self.entity_id = entity_id


@runtime_checkable
class ClientStore(Protocol):
    """Where client, source, mapping, spec and dataset metadata live; SQLite now, Postgres in Phase 4a."""

    def create_client(self, name: str, industry: str, notes: str = "") -> ClientRecord: ...

    def get_client(self, client_id: str) -> ClientRecord: ...

    def list_clients(self) -> tuple[ClientRecord, ...]: ...

    def add_source(self, client_id: str, source: SourceSpec) -> SourceSpec: ...

    def get_source(self, source_id: str) -> SourceSpec: ...

    def list_sources(self, client_id: str) -> tuple[SourceSpec, ...]: ...

    def set_source_role(self, source_id: str, role: str | None) -> SourceSpec: ...

    def delete_source(self, source_id: str) -> None: ...

    def save_mapping(self, mapping: MappingSpec) -> MappingSpec: ...

    def get_mapping(self, mapping_id: str) -> MappingSpec: ...

    def list_mappings(self, client_id: str, use_case: str | None = None) -> tuple[MappingSpec, ...]: ...

    def save_spec(self, spec: OnboardingSpec) -> OnboardingSpec: ...

    def get_spec(self, spec_id: str) -> OnboardingSpec: ...

    def list_specs(self, client_id: str, use_case: str | None = None) -> tuple[OnboardingSpec, ...]: ...

    def register_dataset(self, manifest: DatasetManifest) -> None: ...

    def get_dataset(self, dataset_id: str) -> DatasetManifest: ...

    def list_datasets(
        self, client_id: str | None = None, use_case: str | None = None
    ) -> tuple[DatasetManifest, ...]: ...


def _slugify(name: str) -> str:
    """A lowercase, underscore-joined stub for a client id: "Acme, Corp!" -> "acme_corp".

    Never empty: a name with no alphanumeric character at all (an emoji, blank after stripping)
    falls back to "client" rather than producing a degenerate id like "c__1".
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "client"


def _iso_utc(moment: datetime) -> str:
    """A UTC ISO-8601 string that sorts lexicographically in chronological order.

    `engine.utils.time.to_iso` keeps whatever offset a datetime already carries, which is correct
    for round-tripping a single value but wrong for an ordering column that mixes rows from
    whatever offsets their artefacts happened to be built with (every stage in this engine uses
    `utc_now()`, but nothing stops a test or a future caller from passing an aware, non-UTC
    datetime) - two instants a second apart could then sort in the wrong order. Converting to UTC
    first makes the column's ordering match wall-clock ordering unconditionally.
    """
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


class LocalClientStore:
    """SQLite-backed `ClientStore`. One connection, one lock, one transaction per write.

    `check_same_thread=False` mirrors `LocalModelRegistry`, so a `ThreadJobRunner` worker and the
    API's request thread can share one store; unlike `LocalModelRegistry`, this module also takes
    the lock around reads, because a bare `sqlite3.Connection` (unlike a SQLAlchemy `Session`, which
    registry.py uses) is not safe to drive from two threads at once even when they are only reading -
    the C library serialises statements on one connection object, and interleaved `execute()` calls
    from two threads can hand back rows from each other's cursors. One lock around every method call
    is the simplest thing that is definitely correct for a metadata store this small.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    @property
    def path(self) -> Path:
        """The SQLite file this store writes to."""
        return self._path

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS clients ("
                "client_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, document TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sources ("
                "source_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, created_at TEXT NOT NULL, "
                "document TEXT NOT NULL)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_sources_client ON sources (client_id)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS mappings ("
                "mapping_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, use_case TEXT NOT NULL, "
                "created_at TEXT NOT NULL, document TEXT NOT NULL)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_mappings_client ON mappings (client_id)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS specs ("
                "spec_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, use_case TEXT NOT NULL, "
                "created_at TEXT NOT NULL, document TEXT NOT NULL)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_specs_client ON specs (client_id)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS datasets ("
                "dataset_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, use_case TEXT NOT NULL, "
                "created_at TEXT NOT NULL, document TEXT NOT NULL)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_datasets_client ON datasets (client_id)")

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------
    def create_client(self, name: str, industry: str, notes: str = "") -> ClientRecord:
        """A new client, with a fresh `c_<slug>_<n>` id minted inside this call's own transaction."""
        slug = _slugify(name)
        prefix = f"c_{slug}_"
        now = utc_now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT client_id FROM clients WHERE client_id GLOB ?", (f"{prefix}[0-9]*",)
            ).fetchall()
            # `max` rather than `count`: collision-free even across a future `delete_client`, where a
            # count of surviving rows could reissue an id a still-referenced source or mapping names.
            suffixes = [int(row[0][len(prefix) :]) for row in existing if row[0][len(prefix) :].isdigit()]
            client_id = f"{prefix}{max(suffixes, default=0) + 1}"
            record = ClientRecord(
                client_id=client_id, name=name, industry=industry, notes=notes, created_at=now
            )
            self._conn.execute(
                "INSERT INTO clients (client_id, created_at, document) VALUES (?, ?, ?)",
                (client_id, _iso_utc(now), dump_artefact(record)),
            )
        return record

    def get_client(self, client_id: str) -> ClientRecord:
        """One client; raises `NOT_FOUND` when the id is unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT document FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if row is None:
            raise ClientStoreError(
                "NOT_FOUND", f"No client {client_id!r}.", entity="client", entity_id=client_id
            )
        return ClientRecord.model_validate_json(row[0])

    def list_clients(self) -> tuple[ClientRecord, ...]:
        """Every client, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT document FROM clients ORDER BY created_at DESC, client_id DESC"
            ).fetchall()
        return tuple(ClientRecord.model_validate_json(row[0]) for row in rows)

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------
    def add_source(self, client_id: str, source: SourceSpec) -> SourceSpec:
        """Register an uploaded source under `client_id`; the source keeps the id it already carries.

        Raises `NOT_FOUND` when `client_id` names no client and `CLIENT_MISMATCH` when `source` was
        built for a different client than the one it is being added under - a caller passing the
        wrong pair together is a bug worth catching here rather than a source that silently shows
        up in the wrong client's list.
        """
        if source.client_id != client_id:
            raise ClientStoreError(
                "CLIENT_MISMATCH",
                f"Source {source.source_id!r} belongs to client {source.client_id!r}, not {client_id!r}.",
                entity="source",
                entity_id=source.source_id,
            )
        with self._lock, self._conn:
            self._require_client_locked(client_id)
            self._insert_new_locked(
                "sources",
                "source_id",
                source.source_id,
                "client_id, created_at, document",
                (source.client_id, _iso_utc(source.created_at), dump_artefact(source)),
                entity="source",
            )
        return source

    def get_source(self, source_id: str) -> SourceSpec:
        """One source; raises `NOT_FOUND` when the id is unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT document FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise ClientStoreError(
                "NOT_FOUND", f"No source {source_id!r}.", entity="source", entity_id=source_id
            )
        return SourceSpec.model_validate_json(row[0])

    def list_sources(self, client_id: str) -> tuple[SourceSpec, ...]:
        """Every source of one client, newest first; never a source belonging to another client."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT document FROM sources WHERE client_id = ? ORDER BY created_at DESC, source_id DESC",
                (client_id,),
            ).fetchall()
        return tuple(SourceSpec.model_validate_json(row[0]) for row in rows)

    def set_source_role(self, source_id: str, role: str | None) -> SourceSpec:
        """Confirm (or, with `role=None`, clear) a source's role; raises `NOT_FOUND` when unknown."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT document FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if row is None:
                raise ClientStoreError(
                    "NOT_FOUND", f"No source {source_id!r}.", entity="source", entity_id=source_id
                )
            updated = SourceSpec.model_validate_json(row[0]).model_copy(update={"role": role})
            self._conn.execute(
                "UPDATE sources SET document = ? WHERE source_id = ?",
                (dump_artefact(updated), source_id),
            )
        return updated

    def delete_source(self, source_id: str) -> None:
        """Remove a source; raises `NOT_FOUND` when the id is unknown."""
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
            if cursor.rowcount == 0:
                raise ClientStoreError(
                    "NOT_FOUND", f"No source {source_id!r}.", entity="source", entity_id=source_id
                )

    # ------------------------------------------------------------------
    # Mappings
    # ------------------------------------------------------------------
    def save_mapping(self, mapping: MappingSpec) -> MappingSpec:
        """Save a mapping, stamping it with its own stable hash (`MappingSpec.with_hash`, DEC-105).

        An upsert, not a create: a mapping is edited and re-saved under the id it already has far
        more often than it is created once and left alone (the whole point of the mapping screen is
        adjusting a suggested column and saving again), so `save_mapping` behaves like saving a file
        rather than like `LocalModelRegistry.register`, which must reject a reused id because a
        model version's whole meaning is that it was trained once and is now immutable.
        """
        hashed = mapping.with_hash()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO mappings (mapping_id, client_id, use_case, created_at, document) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (mapping_id) DO UPDATE SET "
                "client_id = excluded.client_id, use_case = excluded.use_case, "
                "created_at = excluded.created_at, document = excluded.document",
                (
                    hashed.mapping_id,
                    hashed.client_id,
                    hashed.use_case,
                    _iso_utc(hashed.created_at),
                    dump_artefact(hashed),
                ),
            )
        return hashed

    def get_mapping(self, mapping_id: str) -> MappingSpec:
        """One mapping; raises `NOT_FOUND` when the id is unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT document FROM mappings WHERE mapping_id = ?", (mapping_id,)
            ).fetchone()
        if row is None:
            raise ClientStoreError(
                "NOT_FOUND", f"No mapping {mapping_id!r}.", entity="mapping", entity_id=mapping_id
            )
        return MappingSpec.model_validate_json(row[0])

    def list_mappings(self, client_id: str, use_case: str | None = None) -> tuple[MappingSpec, ...]:
        """One client's mappings, newest first; `use_case` narrows to one use case's mappings."""
        with self._lock:
            if use_case is None:
                rows = self._conn.execute(
                    "SELECT document FROM mappings WHERE client_id = ? "
                    "ORDER BY created_at DESC, mapping_id DESC",
                    (client_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT document FROM mappings WHERE client_id = ? AND use_case = ? "
                    "ORDER BY created_at DESC, mapping_id DESC",
                    (client_id, use_case),
                ).fetchall()
        return tuple(MappingSpec.model_validate_json(row[0]) for row in rows)

    # ------------------------------------------------------------------
    # Onboarding specs
    # ------------------------------------------------------------------
    def save_spec(self, spec: OnboardingSpec) -> OnboardingSpec:
        """Save a recipe, stamping it with its own stable hash (`OnboardingSpec.with_hash`, DEC-105).

        An upsert, for the same reason as `save_mapping`: a recipe is refined across the onboarding
        screens under one id before the user ever runs a build.
        """
        hashed = spec.with_hash()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO specs (spec_id, client_id, use_case, created_at, document) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (spec_id) DO UPDATE SET "
                "client_id = excluded.client_id, use_case = excluded.use_case, "
                "created_at = excluded.created_at, document = excluded.document",
                (
                    hashed.spec_id,
                    hashed.client_id,
                    hashed.use_case,
                    _iso_utc(hashed.created_at),
                    dump_artefact(hashed),
                ),
            )
        return hashed

    def get_spec(self, spec_id: str) -> OnboardingSpec:
        """One recipe; raises `NOT_FOUND` when the id is unknown."""
        with self._lock:
            row = self._conn.execute("SELECT document FROM specs WHERE spec_id = ?", (spec_id,)).fetchone()
        if row is None:
            raise ClientStoreError("NOT_FOUND", f"No spec {spec_id!r}.", entity="spec", entity_id=spec_id)
        return OnboardingSpec.model_validate_json(row[0])

    def list_specs(self, client_id: str, use_case: str | None = None) -> tuple[OnboardingSpec, ...]:
        """One client's recipes, newest first; `use_case` narrows to one use case's recipes."""
        with self._lock:
            if use_case is None:
                rows = self._conn.execute(
                    "SELECT document FROM specs WHERE client_id = ? ORDER BY created_at DESC, spec_id DESC",
                    (client_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT document FROM specs WHERE client_id = ? AND use_case = ? "
                    "ORDER BY created_at DESC, spec_id DESC",
                    (client_id, use_case),
                ).fetchall()
        return tuple(OnboardingSpec.model_validate_json(row[0]) for row in rows)

    # ------------------------------------------------------------------
    # Dataset manifests
    # ------------------------------------------------------------------
    def register_dataset(self, manifest: DatasetManifest) -> None:
        """Record a built dataset's manifest; raises `DUPLICATE` when `dataset_id` was already registered.

        A create, not an upsert, unlike `save_mapping`/`save_spec`: a `dataset_id` names one
        immutable build (plan section 4.4 - the manifest is that build's lineage), so registering it
        twice means either the build ran twice under one id, which is a bug upstream, or a caller is
        trying to overwrite lineage that a run may already have referenced - neither should succeed
        silently.
        """
        with self._lock, self._conn:
            self._insert_new_locked(
                "datasets",
                "dataset_id",
                manifest.dataset_id,
                "client_id, use_case, created_at, document",
                (manifest.client_id, manifest.use_case, _iso_utc(manifest.built_at), dump_artefact(manifest)),
                entity="dataset",
            )

    def get_dataset(self, dataset_id: str) -> DatasetManifest:
        """One dataset manifest; raises `NOT_FOUND` when the id is unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT document FROM datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
        if row is None:
            raise ClientStoreError(
                "NOT_FOUND", f"No dataset {dataset_id!r}.", entity="dataset", entity_id=dataset_id
            )
        return DatasetManifest.model_validate_json(row[0])

    def list_datasets(
        self, client_id: str | None = None, use_case: str | None = None
    ) -> tuple[DatasetManifest, ...]:
        """Dataset manifests, newest first, optionally narrowed to one client and/or one use case."""
        clauses: list[str] = []
        params: list[str] = []
        if client_id is not None:
            clauses.append("client_id = ?")
            params.append(client_id)
        if use_case is not None:
            clauses.append("use_case = ?")
            params.append(use_case)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT document FROM datasets{where} ORDER BY created_at DESC, dataset_id DESC",
                params,
            ).fetchall()
        return tuple(DatasetManifest.model_validate_json(row[0]) for row in rows)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _require_client_locked(self, client_id: str) -> None:
        """`get_client`'s existence check, for callers that already hold `self._lock`."""
        row = self._conn.execute("SELECT 1 FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        if row is None:
            raise ClientStoreError(
                "NOT_FOUND", f"No client {client_id!r}.", entity="client", entity_id=client_id
            )

    def _insert_new_locked(
        self,
        table: str,
        id_column: str,
        id_value: str,
        other_columns: str,
        other_values: tuple[str, ...],
        *,
        entity: str,
    ) -> None:
        """A strict (non-upsert) insert, for callers that already hold `self._lock` and a transaction.

        `sources` and `datasets` share this rather than an `ON CONFLICT` clause because their
        failure mode is meant to be loud: a reused id there is a bug, where for `mappings`/`specs`
        it is the expected way of editing (see `save_mapping`).
        """
        try:
            self._conn.execute(
                f"INSERT INTO {table} ({id_column}, {other_columns}) "
                f"VALUES ({', '.join(['?'] * (1 + len(other_values)))})",
                (id_value, *other_values),
            )
        except sqlite3.IntegrityError as exc:
            raise ClientStoreError(
                "DUPLICATE",
                f"{entity} {id_value!r} is already registered.",
                entity=entity,
                entity_id=id_value,
            ) from exc
