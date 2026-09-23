"""The three SQL tables of M48: `consent_record`, `erasure_request` and `model_retrain_flag`.

They live in the platform database (`engine/platform_db.py`, DEC-706): `platform.db` beside the
artefacts on a laptop, the registry's Postgres on a deployment, where `alembic/versions/0003_privacy.py`
owns the schema. Each class declares exactly the columns and indexes that migration creates, so the
drift test in `tests/unit/test_alembic_migrations.py` has nothing to report.

**No table holds a data principal's raw id** (DEC-733). `consent_record` is keyed by
`principal_hash` - the salted SHA-256 of `engine.audit.events.principal_hash` - and a lookup hashes
the ids it is asked about. That keeps the ledger a lookup rather than a second copy of the customer
list, lets an erasure request leave the consent evidence in place without leaving the id behind,
and means the sentinel search of the erasure test (`tests/unit/production/test_erasure.py`) can
byte-search `platform.db` along with everything else.

Every timestamp is `DateTime(timezone=True)` through an explicit `sa_column`, for DEC-339's reason:
without it Postgres stores the session's wall clock instead of the instant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Final

from sqlalchemy import Boolean, Column, DateTime, Index, false, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

from engine.platform_db import create_tables

__all__ = [
    "CONSENT_RECORD_TABLE",
    "ERASURE_PROGRESS_TABLE",
    "ERASURE_REQUEST_TABLE",
    "MODEL_RETRAIN_FLAG_TABLE",
    "PRIVACY_TABLES",
    "ConsentRecordRow",
    "ErasureProgressRow",
    "ErasureRequestRow",
    "ModelRetrainFlagRow",
    "create_privacy_tables",
]

CONSENT_RECORD_TABLE: Final[str] = "consent_record"
ERASURE_REQUEST_TABLE: Final[str] = "erasure_request"
MODEL_RETRAIN_FLAG_TABLE: Final[str] = "model_retrain_flag"
PRIVACY_TABLES: Final[tuple[str, ...]] = (
    CONSENT_RECORD_TABLE,
    ERASURE_REQUEST_TABLE,
    MODEL_RETRAIN_FLAG_TABLE,
)
"""The tables this package owns, in creation order. `alembic/versions/0003_privacy.py` creates them."""

ERASURE_PROGRESS_TABLE: Final[str] = "erasure_progress"
"""Plan D M54 (DEC-863): a background erasure's progress per store. `0005_plan_d` creates it."""


class ConsentRecordRow(SQLModel, table=True):
    """One consent event: granted or withdrawn, for one principal, one purpose, at one time.

    Append-only in practice: a change of mind is a new row, and the latest row as of a moment decides
    (`engine.privacy.consent`). `seq` is the tie-break between two rows recorded at the same instant -
    the one written later wins - which is why it is an autoincrementing integer rather than a uuid.
    """

    __tablename__ = CONSENT_RECORD_TABLE
    __table_args__: ClassVar[Any] = (
        Index("ix_consent_record_lookup", "client_id", "purpose", "principal_hash"),
    )

    seq: int | None = SQLField(default=None, primary_key=True)
    client_id: str
    principal_hash: str
    purpose: str
    status: str
    source: str
    recorded_at: datetime = SQLField(sa_column=Column("recorded_at", DateTime(timezone=True), nullable=False))
    expires_at: datetime | None = SQLField(
        default=None, sa_column=Column("expires_at", DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))


class ErasureRequestRow(SQLModel, table=True):
    """One erasure request and its outcome. The principal appears only as its hash."""

    __tablename__ = ERASURE_REQUEST_TABLE

    request_id: str = SQLField(primary_key=True)
    client_id: str | None = None
    principal_hash: str = SQLField(index=True)
    status: str
    mode: str
    store_counts_json: str = "{}"
    models_flagged_json: str = "[]"
    rows_deleted: int = 0
    rows_tombstoned: int = 0
    cells_masked: int = 0
    files_rewritten: int = 0
    files_deleted: int = 0
    requested_by: str
    requested_at: datetime = SQLField(
        sa_column=Column("requested_at", DateTime(timezone=True), nullable=False)
    )
    completed_at: datetime | None = SQLField(
        default=None, sa_column=Column("completed_at", DateTime(timezone=True), nullable=True)
    )
    error_code: str | None = None
    history_all_clients: bool = SQLField(
        default=False,
        sa_column=Column("history_all_clients", Boolean(), nullable=False, server_default=false()),
    )
    """Whether the consent history goes under every client (the Admin named none), kept so a retry
    does what the request asked (Plan D, DEC-882). Added by `0005_plan_d`."""


class ErasureProgressRow(SQLModel, table=True):
    """How far one erasure request has got in one store. Holds counts and codes, never the principal."""

    __tablename__ = ERASURE_PROGRESS_TABLE

    request_id: str = SQLField(primary_key=True)
    store: str = SQLField(primary_key=True)
    status: str
    files_total: int = 0
    files_done: int = 0
    attempts: int = 0
    error_code: str | None = None
    updated_at: datetime = SQLField(sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))


class ModelRetrainFlagRow(SQLModel, table=True):
    """A model version whose training data included an erased principal, until retraining clears it."""

    __tablename__ = MODEL_RETRAIN_FLAG_TABLE

    flag_id: int | None = SQLField(default=None, primary_key=True)
    model_id: str = SQLField(index=True)
    request_id: str = SQLField(index=True)
    reason: str
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    cleared_at: datetime | None = SQLField(
        default=None, sa_column=Column("cleared_at", DateTime(timezone=True), nullable=True)
    )


_ADDED_COLUMNS: Final[tuple[tuple[str, str, str], ...]] = (
    (ERASURE_REQUEST_TABLE, "history_all_clients", "BOOLEAN NOT NULL DEFAULT 0"),
)
"""Columns a later revision added to a table Phase 4b's `create_tables` may already have made in a
SQLite file: `create_all` adds a missing table, never a missing column (DEC-341), so they are added
here - `(table, column, SQLite DDL)`. Postgres gets them from Alembic (`0005_plan_d`)."""


def create_privacy_tables(engine: Engine) -> None:
    """Create this package's tables if missing (SQLite only; Alembic owns Postgres, DEC-340).

    On SQLite, a column a later revision added (`_ADDED_COLUMNS`) is added to a table an earlier
    version of this code created without it, so a laptop's `platform.db` keeps working (DEC-882).
    """
    create_tables(engine, (*PRIVACY_TABLES, ERASURE_PROGRESS_TABLE))
    if engine.dialect.name != "sqlite":
        return
    for table, column, ddl in _ADDED_COLUMNS:
        present = {item["name"] for item in inspect(engine).get_columns(table)}
        if column in present:
            continue
        try:
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        except OperationalError:  # another thread added it first
            if column not in {item["name"] for item in inspect(engine).get_columns(table)}:
                raise
