"""`0005_plan_d` against the models it migrates for (DEC-341's drift check, for Plan D's tables).

The same approach as `test_scheduling_migration.py`: upgrade an empty SQLite database to head and ask
`compare_metadata` what it would still change for `model_decision`, `erasure_progress`,
`platform_setting` and the column 0005 adds to `erasure_request` (DEC-882, DEC-883). The
timestamp-with-time-zone half of DEC-339 needs a real Postgres server and is asserted in
`tests/unit/test_postgres_metadata.py`.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, Index, Table, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from engine.approvals import MODEL_DECISION_TABLE, ModelDecisionRow
from engine.platform_db import PLATFORM_SETTING_TABLE, PLATFORM_TABLES, PlatformSettingRow
from engine.privacy.erasure import erasure_request, queue_request
from engine.privacy.tables import (
    ERASURE_PROGRESS_TABLE,
    ERASURE_REQUEST_TABLE,
    ErasureProgressRow,
    ErasureRequestRow,
    create_privacy_tables,
)
from tests.fixtures.postgres import alembic_upgrade

# imported to register the tables with the metadata
_ = (ModelDecisionRow, ErasureProgressRow, ErasureRequestRow, PlatformSettingRow)

OURS: frozenset[str] = frozenset({MODEL_DECISION_TABLE, ERASURE_PROGRESS_TABLE, PLATFORM_SETTING_TABLE})
"""The tables 0005 creates."""
CHECKED: frozenset[str] = OURS | {ERASURE_REQUEST_TABLE}
"""What the drift check looks at: 0005's tables and the table it adds `history_all_clients` to."""
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
    return next((part for part in item if isinstance(part, str) and part in CHECKED), None)


def drift(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, SQLModel.metadata)
    return [item for item in differences if _table_of(item) in CHECKED]


@pytest.fixture
def head(tmp_path: Path) -> Engine:
    path = tmp_path / "head.db"
    alembic_upgrade(f"sqlite:///{path}")
    return create_engine(f"sqlite:///{path}")


def test_the_plan_d_tables_are_declared() -> None:
    assert set(PLATFORM_TABLES) >= OURS


def test_0005_matches_the_models(head: Engine) -> None:
    try:
        assert drift(head) == []
    finally:
        head.dispose()


def test_one_progress_row_per_request_and_store(head: Engine) -> None:
    row = (
        "INSERT INTO erasure_progress (request_id, store, status, files_total, files_done, attempts, updated_at) "
        "VALUES ('er_1', 'uploads', 'pending', 1, 0, 0, '2026-09-23 10:00:00')"
    )
    try:
        with head.begin() as connection:
            connection.execute(text(row))
        with pytest.raises(IntegrityError), head.begin() as connection:
            connection.execute(text(row))
    finally:
        head.dispose()


def _config(path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"), cmd_opts=Namespace(x=[f"url=sqlite:///{path}"]))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


def _erasure_columns(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {row[1] for row in connection.execute(text("PRAGMA table_info(erasure_request)"))}


_OLD_REQUEST = (
    "INSERT INTO erasure_request (request_id, principal_hash, status, mode, store_counts_json, "
    "models_flagged_json, rows_deleted, rows_tombstoned, cells_masked, files_rewritten, files_deleted, "
    "requested_by, requested_at) VALUES ('er_old', 'h', 'failed', 'delete', '{}', '[]', 0, 0, 0, 0, 0, "
    "'u_1', '2026-09-01 10:00:00')"
)


def test_0005_downgrades_to_0004(tmp_path: Path) -> None:
    path = tmp_path / "down.db"
    config = _config(path)
    command.upgrade(config, "0005")
    command.downgrade(config, "0004")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            names = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master"))}
        assert names & OURS == set()
        assert "alert" in names, "0004's tables stay"
        assert ERASURE_REQUEST_TABLE in names and "history_all_clients" not in _erasure_columns(engine)
    finally:
        engine.dispose()


def test_a_request_from_before_0005_did_not_ask_for_every_clients_history(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    config = _config(path)
    command.upgrade(config, "0004")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.execute(text(_OLD_REQUEST))
        command.upgrade(config, "0005")
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT history_all_clients FROM erasure_request WHERE request_id = 'er_old'")
            ).scalar_one()
        assert value in (0, False)
    finally:
        engine.dispose()


def test_a_phase_4b_sqlite_file_gets_the_new_column(tmp_path: Path) -> None:
    """`create_all` never adds a column, so `create_privacy_tables` does, on SQLite only (DEC-882)."""
    path = tmp_path / "platform.db"
    command.upgrade(_config(path), "0004")  # the table exactly as Phase 4b made it
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.execute(text(_OLD_REQUEST))
        assert "history_all_clients" not in _erasure_columns(engine)
        create_privacy_tables(engine)
        create_privacy_tables(engine)  # idempotent
        assert "history_all_clients" in _erasure_columns(engine)
        old = erasure_request(engine, "er_old")
        assert old is not None and old.history_all_clients is False
        queue_request(
            engine,
            request_id="er_new",
            principal_hash="h",
            client_id="acme",
            mode="delete",
            requested_by="u_1",
            requested_at=datetime(2026, 9, 2, tzinfo=UTC),
            history_all_clients=True,
        )
        new = erasure_request(engine, "er_new")
        assert new is not None and new.history_all_clients is True
    finally:
        engine.dispose()


def test_autogenerate_sees_every_platform_table_and_finds_nothing_to_do(tmp_path: Path) -> None:
    """`alembic check` - what `make revision` runs first - over a database migrated to head (DEC-888).

    `env.py` admits the registry's and the platform's tables and imports every model behind them, so
    a head database has nothing to add and, crucially, nothing to drop: a table the filter admitted
    without its model imported would come back as a `remove_table`.
    """
    path = tmp_path / "check.db"
    config = Config(str(REPO_ROOT / "alembic.ini"), cmd_opts=Namespace(x=[f"url=sqlite:///{path}"]))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(config, "head")
    command.check(config)  # raises AutoGenerateDiffsDetected on any difference
