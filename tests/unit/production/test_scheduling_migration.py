"""`0004_scheduling` against the models it migrates for (DEC-341's drift check, for M49's tables).

The same approach as `test_platform_migration.py`: upgrade an empty SQLite database to head and ask
`compare_metadata` what it would still change for `schedule`, `schedule_firing` and `alert`. The
timestamp-with-time-zone half of DEC-339 needs a real Postgres server and is not asserted here.
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
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from engine.platform_db import PLATFORM_TABLES
from engine.scheduling.alerts import ALERT_TABLE, AlertRow
from engine.scheduling.schedules import SCHEDULING_TABLES, ScheduleFiringRow, ScheduleRow
from tests.fixtures.postgres import alembic_upgrade

_ = (AlertRow, ScheduleFiringRow, ScheduleRow)  # imported to register the tables with the metadata

OURS: frozenset[str] = frozenset({*SCHEDULING_TABLES, ALERT_TABLE})
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


def test_the_scheduling_tables_are_declared() -> None:
    assert set(PLATFORM_TABLES) >= OURS


def test_0004_matches_the_models(head: Engine) -> None:
    try:
        assert drift(head) == []
    finally:
        head.dispose()


def test_the_migrated_slot_index_refuses_a_second_claim(head: Engine) -> None:
    row = (
        'INSERT INTO schedule_firing (firing_id, schedule_id, use_case_id, kind, "trigger", status, '
        "scheduled_for, flagged_models_json, fired_at) VALUES ('{id}', 's1', 'u', 'score', 'scheduled', "
        "'running', '2026-09-01 20:30:00', '[]', '2026-09-01 20:30:05')"
    )
    try:
        with head.begin() as connection:
            connection.execute(text(row.format(id="f1")))
        with pytest.raises(IntegrityError), head.begin() as connection:
            connection.execute(text(row.format(id="f2")))
    finally:
        head.dispose()


def test_0004_downgrades_to_0003(tmp_path: Path) -> None:
    path = tmp_path / "down.db"
    config = Config(str(REPO_ROOT / "alembic.ini"), cmd_opts=Namespace(x=[f"url=sqlite:///{path}"]))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(config, "0004")
    command.downgrade(config, "0003")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            names = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master"))}
        assert names & OURS == set()
        assert "consent_record" in names, "0003's tables stay"
    finally:
        engine.dispose()
