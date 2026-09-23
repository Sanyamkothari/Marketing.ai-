"""`0002_access_audit` against the models it migrates for (DEC-341's drift check, for Phase 4b's tables).

`tests/unit/test_alembic_migrations.py` compares Phase 4a's tables; `alembic/env.py` holds
autogenerate to `METADATA_TABLES`, so Phase 4b's tables are compared here instead, the same way:
upgrade an empty database to head and ask `compare_metadata` what it would still change. On SQLite
this sees tables, columns, nullability and indexes; the timestamp-with-time-zone half of DEC-339 is
asserted on a real server by `tests/unit/test_postgres_metadata.py`.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, Index, Table, create_engine, text
from sqlalchemy.exc import DatabaseError
from sqlmodel import SQLModel

from engine.access.users import AuthSessionRow, PlatformUserRow
from engine.audit.store import AuditEventRow
from engine.platform_db import PLATFORM_TABLES
from tests.fixtures.postgres import (  # noqa: F401 - imported so pytest can resolve them by name
    alembic_upgrade,
    postgres_engine_fixture,
    postgres_schema,
    postgres_url_value,
)

_ = (AuthSessionRow, PlatformUserRow, AuditEventRow)  # imported to register the tables with the metadata

OURS: frozenset[str] = frozenset({"platform_user", "auth_session", "audit_events"})
REPO_ROOT: Path = Path(__file__).resolve().parents[3]


def _table_of(item: Any) -> str | None:
    if isinstance(item, list):
        item = item[0]
    if not isinstance(item, tuple):
        return None
    for part in item:
        if isinstance(part, Index) and part.table is not None:
            return str(part.table.name)
        if isinstance(part, Table):
            return str(part.name)
    return next((part for part in item if isinstance(part, str) and part in OURS), None)


def drift(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, SQLModel.metadata)
    return [item for item in differences if _table_of(item) in OURS]


@pytest.fixture
def head(tmp_path: Path) -> Engine:
    path = tmp_path / "head.db"
    alembic_upgrade(f"sqlite:///{path}")
    return create_engine(f"sqlite:///{path}")


def test_the_platform_tables_are_declared() -> None:
    assert set(PLATFORM_TABLES) >= OURS


def test_0002_matches_the_models(head: Engine) -> None:
    try:
        assert drift(head) == []
    finally:
        head.dispose()


def test_the_drift_check_would_notice_a_missing_index(head: Engine) -> None:
    try:
        with head.begin() as connection:
            connection.execute(text("DROP INDEX ix_audit_events_action"))
        assert any("ix_audit_events_action" in repr(item) for item in drift(head))
    finally:
        head.dispose()


def test_the_migrated_audit_table_is_append_only(head: Engine) -> None:
    try:
        with head.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_events (event_id, occurred_at, actor_id, actor_kind, action, outcome, "
                    "details_json) VALUES ('e1', '2026-09-23 10:00:00', 'u', 'user', 'runs.create', 'success', '{}')"
                )
            )
        for statement in ("UPDATE audit_events SET action = 'x'", "DELETE FROM audit_events"):
            with pytest.raises(DatabaseError, match="append-only"), head.begin() as connection:
                connection.execute(text(statement))
    finally:
        head.dispose()


def test_0002_downgrades_to_0001(tmp_path: Path) -> None:
    path = tmp_path / "down.db"
    config = Config(str(REPO_ROOT / "alembic.ini"), cmd_opts=Namespace(x=[f"url=sqlite:///{path}"]))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(config, "0002")
    command.downgrade(config, "0001")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            names = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master"))}
        assert names & OURS == set()
        assert not {name for name in names if name.startswith("audit_events_no_")}
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_the_postgres_audit_table_refuses_update_delete_and_truncate(
    postgres_engine_fixture: Engine,  # noqa: F811 - the parameter IS the imported fixture
) -> None:
    """DEC-714 on the server it is written for: 0002's plpgsql trigger, including TRUNCATE."""
    with postgres_engine_fixture.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_events (event_id, occurred_at, actor_id, actor_kind, action, outcome, "
                "details_json) VALUES ('e1', '2026-09-23 10:00:00+00', 'u', 'user', 'runs.create', 'success', '{}')"
            )
        )
    for statement in (
        "UPDATE audit_events SET action = 'x'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ):
        with pytest.raises(DatabaseError, match="append-only"), postgres_engine_fixture.begin() as connection:
            connection.execute(text(statement))
    with postgres_engine_fixture.connect() as connection:
        assert connection.execute(text("SELECT action FROM audit_events")).scalars().all() == ["runs.create"]
