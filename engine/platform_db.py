"""The one SQL database Phase 4b's tables live in: users, the audit trail, consent, schedules.

Locally that is `platform.db`, a SQLite file beside the artefacts - a sibling of `registry.db`,
not a table inside it, because the registry's file is Phase 1's and a laptop that deletes it to
pick up a new column (DEC-341) must not lose its users and audit trail with it. On a deployment with
`metadata_backend=postgres` it is the same database as the registry, through the same engine
factory, and Alembic owns the schema there (`alembic/versions/0002_*`), exactly as DEC-341 decided
for Phase 4a's tables (DEC-706).

Every module that declares a Phase 4b table creates *only its own* tables through
`create_tables(engine, names)`, never an unscoped `create_all`, for DEC-340's reason: SQLModel's
metadata is one process-wide namespace and an unscoped call would conjure every table anything has
imported into whichever file it was pointed at.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

from engine.settings import Settings

__all__ = ["PLATFORM_DB_FILENAME", "PLATFORM_TABLES", "create_tables", "platform_engine", "sqlite_engine"]

PLATFORM_DB_FILENAME: Final[str] = "platform.db"
"""The SQLite file's name, inside the data directory."""

PLATFORM_TABLES: Final[tuple[str, ...]] = (
    # 0002_access_audit (M46/M47)
    "platform_user",
    "auth_session",
    "audit_events",
    # 0003_privacy (M48) - `engine.privacy.tables.PRIVACY_TABLES`
    "consent_record",
    "erasure_request",
    "model_retrain_flag",
    # 0004_scheduling (M49) - `engine.scheduling.schedules.SCHEDULING_TABLES` and `alerts.ALERT_TABLE`
    "schedule",
    "schedule_firing",
    "alert",
)
"""Every Phase 4b table a migration creates, beside `engine.aws.postgres.METADATA_TABLES`.

The definite answer to "which tables does `alembic upgrade head` make", for the same reason
`METADATA_TABLES` exists (DEC-340): `SQLModel.metadata` holds whatever has been imported. A Phase 4b
migration that creates a table adds its name here, in the migration's order (DEC-721)."""

_ENGINES: dict[str, Engine] = {}
_LOCK: Final[threading.Lock] = threading.Lock()


def sqlite_engine(path: Path) -> Engine:
    """A SQLite engine at `path`, shared per file within the process, safe for the job threads.

    Cached per resolved path: every store in the process that points at the same file gets the same
    engine, so SQLite's single-writer lock is taken by one pool rather than contended by several.
    """
    resolved = str(Path(path).resolve())
    with _LOCK:
        cached = _ENGINES.get(resolved)
        if cached is not None:
            return cached
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{resolved}", connect_args={"check_same_thread": False, "timeout": 30}
        )
        event.listen(engine, "connect", _sqlite_pragmas)
        _ENGINES[resolved] = engine
        return engine


def _sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    """WAL so a reader (the audit viewer) never blocks a writer (a request being audited)."""
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def platform_engine(settings: Settings, *, data_dir: Path | None = None) -> Engine:
    """The engine this deployment's Phase 4b tables live on.

    `data_dir` wins, as it does for every other service (`api/deps.py`): it is how a test points the
    app at a `tmp_path`. Otherwise SQLite beside the artefacts, or the registry's Postgres.
    """
    if data_dir is not None or settings.metadata_backend == "sqlite":
        return sqlite_engine((data_dir if data_dir is not None else settings.data_dir) / PLATFORM_DB_FILENAME)
    # A deliberate local import: psycopg is an optional dependency (DEC-306).
    from engine.aws.postgres import PostgresConfig, postgres_engine

    return postgres_engine(PostgresConfig.from_settings(settings))


def create_tables(engine: Engine, names: Iterable[str]) -> None:
    """Create exactly the named tables if they are missing (DEC-340). Never used against Postgres."""
    if engine.dialect.name != "sqlite":
        return
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[name] for name in names])
