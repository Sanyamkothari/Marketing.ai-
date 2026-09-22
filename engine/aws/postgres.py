"""The Postgres metadata backend: the engine behind it, the run index table, and nothing else.

`engine/registry.py` already separated *what the registry says* from *what it says it to*. This
module supplies the second half for a deployment: a SQLAlchemy `Engine` pointed at RDS, wrapped in
the same `SqlRegistryStore` the laptop uses, so a champion swap is the identical transaction on both
(DEC-338). It also owns the one table Phase 4a adds, the run index, because a table has to live
somewhere a migration can see it and `engine/aws/run_index.py` is deliberately table-free.

Three things this module does not do.

**It does not read the environment.** `Settings` already did that, once, and a second reader would
be a second answer: `postgres_store(settings)` and `postgres_run_index(settings)` take the frozen
object, and `PostgresConfig.from_settings` is the only translation from a deployment to a DSN
(DEC-343).

**It does not create the Postgres schema.** `create_run_index_tables` exists for SQLite, where the
local and test paths have no migration tool. On Postgres, Alembic owns every table from `0001` on,
because `create_all` adds a missing table and never a missing column - which is survivable for a
development file you can delete and is not survivable for a client's run history (DEC-341).

**It does not import boto3**, like every module in this package (DEC-306); it does not import
psycopg either. SQLAlchemy loads the driver when an engine is first connected, so a checkout without
the `aws` extra can import this module, read its protocols and fail only if it actually tries to
connect.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Self

from sqlalchemy import Column, DateTime
from sqlalchemy.engine import Engine
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, create_engine, select

from engine.aws.run_index import RunIndex, RunIndexEntry
from engine.config import RunMode
from engine.contracts import RunState
from engine.registry import (
    MODEL_VERSION_TABLE,
    SqlRegistryStore,
    aware_utc,
    to_utc,
    to_utc_or_none,
)
from engine.settings import MetadataBackend, Settings, SettingsError

__all__ = [
    "METADATA_TABLES",
    "RUN_TABLE",
    "PostgresConfig",
    "RunRow",
    "SqlRunIndex",
    "create_run_index_tables",
    "postgres_engine",
    "postgres_run_index",
    "postgres_store",
]

RUN_TABLE: Final[str] = "run"
"""Name of the run index table. `run`, singular, matching `model_version` next to it."""

METADATA_TABLES: Final[tuple[str, ...]] = (MODEL_VERSION_TABLE, RUN_TABLE)
"""Every table this product owns, in `SQLModel.metadata`.

`SQLModel.metadata` is a process-wide namespace, so "what is in it" depends on what has been
imported. Alembic's `env.py` and the drift test both need a definite answer instead, and this tuple
is it: a table not named here is not ours, and a table named here that no migration creates is a
drift failure (DEC-340).
"""

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""What `postgres_schema` may be. It is interpolated into `search_path`, so it is checked, not quoted."""

_PSYCOPG3_SCHEME: Final[str] = "postgresql+psycopg://"
"""The driver this product installs. `postgresql://` alone would mean psycopg2, which it does not."""

APPLICATION_NAME: Final[str] = "marketing-ai"
"""What this process calls itself in `pg_stat_activity`, so a DBA can tell whose connection it is."""


def normalise_url(url: str) -> str:
    """Point a bare `postgresql://` DSN at psycopg 3, the driver the `aws` extra actually installs.

    An operator writes the URL RDS shows them, which has no `+driver` in it, and SQLAlchemy reads
    that as psycopg **2**: the failure is an import error about a package nobody asked for, at
    connect time, in a container. Rewriting the scheme here turns a deployment's most likely typo
    into a non-event, and a URL that already names a driver is left exactly as it is (DEC-343).
    """
    if url.startswith("postgresql://"):
        return _PSYCOPG3_SCHEME + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return _PSYCOPG3_SCHEME + url[len("postgres://") :]
    return url


@dataclass(frozen=True)
class PostgresConfig:
    """Everything needed to open a connection, derived from `Settings` and from nothing else.

    A dataclass rather than a second settings object, and with no `from_env` of its own: the
    environment is read in exactly one place in this product, and this is not it (DEC-343).
    """

    url: str
    """The SQLAlchemy URL, driver included. It carries a password, so it is never logged."""

    schema_name: str | None = None
    """Schema the tables live in; `None` leaves the connection's own `search_path` alone."""

    application_name: str = APPLICATION_NAME
    """Filled into `application_name`, which is what `pg_stat_activity` shows."""

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """The config this deployment describes; `SettingsError` when it describes none.

        `Settings` has already refused `metadata_backend=postgres` without a `database_url`, so the
        null branch here is the case where somebody called this for a SQLite deployment.
        """
        if settings.database_url is None:
            raise SettingsError(
                "SETTINGS_INCOMPLETE",
                "metadata_backend=postgres needs database_url.",
                field="database_url",
            )
        schema_name = settings.postgres_schema
        if schema_name is not None and not _IDENTIFIER.fullmatch(schema_name):
            raise SettingsError(
                "SETTINGS_INVALID",
                "postgres_schema must be a plain SQL identifier: a letter or underscore followed "
                "by letters, digits or underscores.",
                field="postgres_schema",
            )
        return cls(
            url=normalise_url(settings.database_url.get_secret_value()),
            schema_name=schema_name,
            application_name=settings.client_id or APPLICATION_NAME,
        )

    def connect_args(self) -> dict[str, str]:
        """libpq keywords psycopg is given on every connection.

        `search_path` is set through `options` rather than with a `SET` after connecting, so a
        connection handed back by the pool is already pointed at the right schema and no statement
        can run before it is. `public` stays on the path behind it because extensions install
        themselves there.
        """
        args = {"application_name": self.application_name}
        if self.schema_name is not None:
            args["options"] = f"-csearch_path={self.schema_name},public"
        return args


def postgres_engine(config: PostgresConfig) -> Engine:
    """A pooled `Engine` for `config`.

    `pool_pre_ping=True` is the one non-default: a connection in the pool of a long-lived container
    can be closed by anything between it and the database - an idle timeout, a failover, a NAT
    gateway - and without the ping the next request is the thing that discovers it. Everything else
    is left at SQLAlchemy's default on purpose, because a pool size or a recycle interval is a
    number, and this product has not measured one (plan section 13.3).

    `hide_parameters=True` keeps bound values out of SQLAlchemy's own exception text, which is the
    library equivalent of plan section 13.7: a failing INSERT otherwise quotes the row (DEC-343).
    """
    return create_engine(
        config.url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=config.connect_args(),
    )


def postgres_store(settings: Settings) -> SqlRegistryStore:
    """The registry store this deployment describes. Called by `engine.settings.build_registry`."""
    return SqlRegistryStore(postgres_engine(PostgresConfig.from_settings(settings)))


def postgres_run_index(settings: Settings) -> SqlRunIndex:
    """The run index this deployment describes; `SettingsError` when it is not a Postgres one.

    Refusing rather than returning `None` keeps the caller honest: `scripts/reconcile_runs.py` has
    to say out loud that a SQLite deployment has no table to rebuild, instead of reporting that it
    rebuilt nothing.
    """
    if settings.metadata_backend is not MetadataBackend.POSTGRES:
        raise SettingsError(
            "SETTINGS_INCOMPLETE",
            f"There is no run index on metadata_backend={settings.metadata_backend.value}; "
            "the run documents in storage are the whole history.",
            field="metadata_backend",
        )
    return SqlRunIndex(postgres_engine(PostgresConfig.from_settings(settings)))


class RunRow(SQLModel, table=True):
    """One row per run: the SQL projection of `engine.aws.run_index.RunIndexEntry`.

    `run.json` is still the record; this is an index of it (DEC-342). The columns are the ones a
    list page filters, orders or renders with, which is why `created_at` and `use_case_id` are
    indexed and `file_name` is not.

    Every timestamp is `DateTime(timezone=True)` for the reason `ModelVersionRow` is: a plain
    `datetime` compiles to `TIMESTAMP WITHOUT TIME ZONE`, and a server whose time zone is not UTC
    then stores its own wall clock in place of the instant (DEC-339). Each column is a fresh
    `Column` object because a `Column` belongs to one table and cannot be shared with the one next
    door.
    """

    __tablename__ = RUN_TABLE

    run_id: str = SQLField(primary_key=True)
    use_case_id: str = SQLField(index=True)
    use_case_name: str
    mode: str = SQLField(index=True)
    state: str = SQLField(index=True)
    created_at: datetime = SQLField(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, index=True)
    )
    started_at: datetime | None = SQLField(
        default=None, sa_column=Column("started_at", DateTime(timezone=True), nullable=True)
    )
    finished_at: datetime | None = SQLField(
        default=None, sa_column=Column("finished_at", DateTime(timezone=True), nullable=True)
    )
    upload_id: str
    file_name: str
    row_count: int | None = None
    model_version_id: str | None = SQLField(default=None, index=True)
    headline_metric: str | None = None
    headline_score: float | None = None
    champion: bool = False
    error_code: str | None = None
    engine_version: str
    duration_s: float | None = None
    compute_backend: str | None = None
    estimated_usd: float | None = None
    indexed_at: datetime = SQLField(sa_column=Column("indexed_at", DateTime(timezone=True), nullable=False))

    @classmethod
    def from_entry(cls, entry: RunIndexEntry) -> RunRow:
        """The row that stores `entry`; enums become their values, as the registry's row does.

        Every timestamp goes through `to_utc` first. Postgres would convert an aware value itself,
        but SQLite has no timestamp type and stores exactly what it is handed, so a value that
        arrived at +05:30 would be read back as that clock reading in UTC - the wrong instant
        (DEC-339).
        """
        return cls(
            run_id=entry.run_id,
            use_case_id=entry.use_case_id,
            use_case_name=entry.use_case_name,
            mode=str(entry.mode),
            state=str(entry.state),
            created_at=to_utc(entry.created_at),
            started_at=to_utc_or_none(entry.started_at),
            finished_at=to_utc_or_none(entry.finished_at),
            upload_id=entry.upload_id,
            file_name=entry.file_name,
            row_count=entry.row_count,
            model_version_id=entry.model_version_id,
            headline_metric=entry.headline_metric,
            headline_score=entry.headline_score,
            champion=entry.champion,
            error_code=entry.error_code,
            engine_version=entry.engine_version,
            duration_s=entry.duration_s,
            compute_backend=entry.compute_backend,
            estimated_usd=entry.estimated_usd,
            indexed_at=to_utc(entry.indexed_at),
        )

    def to_entry(self) -> RunIndexEntry:
        """The entry this row stores, with every timestamp back in UTC (DEC-339).

        `aware_utc` is `engine.registry`'s, imported rather than repeated: the two tables read
        their timestamps back from the same two backends and must agree about what they mean.
        """
        return RunIndexEntry(
            run_id=self.run_id,
            use_case_id=self.use_case_id,
            use_case_name=self.use_case_name,
            mode=RunMode(self.mode),
            state=RunState(self.state),
            created_at=aware_utc(self.created_at),
            started_at=None if self.started_at is None else aware_utc(self.started_at),
            finished_at=None if self.finished_at is None else aware_utc(self.finished_at),
            upload_id=self.upload_id,
            file_name=self.file_name,
            row_count=self.row_count,
            model_version_id=self.model_version_id,
            headline_metric=self.headline_metric,
            headline_score=self.headline_score,
            champion=self.champion,
            error_code=self.error_code,
            engine_version=self.engine_version,
            duration_s=self.duration_s,
            compute_backend=self.compute_backend,
            estimated_usd=self.estimated_usd,
            indexed_at=aware_utc(self.indexed_at),
        )


def create_run_index_tables(engine: Engine) -> None:
    """Create `run` if it is missing, and nothing else.

    The mirror image of `engine.registry.create_registry_tables`, and scoped for the same reason: an
    unscoped `create_all` would put `model_version` into whatever database this engine points at,
    including a SQLite file that only ever wanted a run index (DEC-340).

    Postgres does not call this. Alembic owns that schema from `0001` (DEC-341); this is for SQLite,
    where there is no migration tool and none is wanted.
    """
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[RUN_TABLE]])


class SqlRunIndex:
    """A `RunIndex` over any SQLAlchemy engine: Postgres in a deployment, SQLite in a test.

    The same shape as `SqlRegistryStore` - one session per call, one lock around the writes - and
    for the same reason: `ThreadJobRunner` workers mirror their own runs, from several threads.

    Writes are an upsert rather than an insert, because `mirror_run` is called again by
    `scripts/reconcile_runs.py` for runs that are already indexed, and a rebuild must not depend on
    the table being empty first. It is deliberately a read-then-write rather than a dialect-specific
    `ON CONFLICT`: this class has to run on SQLite for the tests to be worth anything, and the
    concurrency it faces is one process mirroring its own runs, not a contended hot row (DEC-342).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()

    @property
    def engine(self) -> Engine:
        """The engine every statement runs on."""
        return self._engine

    def upsert(self, entry: RunIndexEntry) -> None:
        """Write `entry`, replacing whatever row that run had before."""
        fresh = RunRow.from_entry(entry)
        with self._lock, Session(self._engine) as session:
            existing = session.get(RunRow, entry.run_id)
            if existing is None:
                session.add(fresh)
            else:
                for name in RunRow.model_fields:
                    setattr(existing, name, getattr(fresh, name))
                session.add(existing)
            session.commit()

    def get(self, run_id: str) -> RunIndexEntry | None:
        """The indexed entry for one run, or `None` when the run is not in the index."""
        with Session(self._engine) as session:
            row = session.get(RunRow, run_id)
            return None if row is None else row.to_entry()

    def list_run_ids(
        self, *, limit: int, use_case_id: str | None = None, mode: RunMode | None = None
    ) -> tuple[str, ...]:
        """Up to `limit` run ids, newest first, optionally filtered the way `GET /runs` filters.

        Ordered by `created_at` and then by `run_id`, which is itself sortable, so two runs created
        in the same clock tick still come back in a stable order rather than the table's.
        """
        with Session(self._engine) as session:
            statement = select(RunRow)
            if use_case_id is not None:
                statement = statement.where(col(RunRow.use_case_id) == use_case_id)
            if mode is not None:
                statement = statement.where(col(RunRow.mode) == str(mode))
            statement = statement.order_by(col(RunRow.created_at).desc(), col(RunRow.run_id).desc()).limit(
                limit
            )
            return tuple(row.run_id for row in session.exec(statement).all())

    def run_ids(self) -> tuple[str, ...]:
        """Every indexed run id, newest first. A maintenance call: `reconcile_runs` uses it."""
        with Session(self._engine) as session:
            statement = select(RunRow).order_by(col(RunRow.created_at).desc(), col(RunRow.run_id).desc())
            return tuple(row.run_id for row in session.exec(statement).all())

    def forget(self, run_id: str) -> None:
        """Drop one run's row. A no-op when it is not there; it deletes no document, only the index."""
        with self._lock, Session(self._engine) as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return
            session.delete(row)
            session.commit()


_: type[RunIndex] = SqlRunIndex
"""`SqlRunIndex` must satisfy the protocol; mypy checks this line so no test has to."""
