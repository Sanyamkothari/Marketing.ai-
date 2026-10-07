"""`0006_campaigns` against the model it migrates for (DEC-341's drift check, for Plan J's first table).

The same approach as `tests/unit/production/test_plan_d_migration.py`: upgrade an empty SQLite
database to head and ask `compare_metadata` what it would still change for `campaign`. The
timestamp-with-time-zone half of DEC-339 needs a real Postgres server and is asserted by
`tests/unit/test_postgres_metadata.py` over every platform table.
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

from engine.measurement.campaign import CAMPAIGN_TABLE, CampaignRow
from engine.platform_db import PLATFORM_TABLES
from tests.fixtures.postgres import alembic_upgrade

_ = CampaignRow  # imported to register the table with the metadata

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
    return next((part for part in item if isinstance(part, str) and part == CAMPAIGN_TABLE), None)


def drift(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, SQLModel.metadata)
    return [item for item in differences if _table_of(item) == CAMPAIGN_TABLE]


def _config(path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"), cmd_opts=Namespace(x=[f"url=sqlite:///{path}"]))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


@pytest.fixture
def head(tmp_path: Path) -> Engine:
    path = tmp_path / "head.db"
    alembic_upgrade(f"sqlite:///{path}")
    return create_engine(f"sqlite:///{path}")


def test_the_campaign_table_is_declared_last() -> None:
    assert PLATFORM_TABLES[-1] == CAMPAIGN_TABLE


def test_0006_matches_the_model(head: Engine) -> None:
    try:
        assert drift(head) == []
    finally:
        head.dispose()


def test_the_drift_check_would_notice_a_missing_index(head: Engine) -> None:
    try:
        with head.begin() as connection:
            connection.execute(text("DROP INDEX ix_campaign_run_id"))
        assert any("ix_campaign_run_id" in repr(item) for item in drift(head))
    finally:
        head.dispose()


def test_one_row_per_campaign(head: Engine) -> None:
    row = (
        "INSERT INTO campaign (campaign_id, kind, status, created_at, updated_at, record_json) "
        "VALUES ('c_1', 'scored', 'live', '2026-10-07 10:00:00', '2026-10-07 10:00:00', '{}')"
    )
    try:
        with head.begin() as connection:
            connection.execute(text(row))
        with pytest.raises(IntegrityError), head.begin() as connection:
            connection.execute(text(row))
    finally:
        head.dispose()


def test_0006_downgrades_to_0005(tmp_path: Path) -> None:
    path = tmp_path / "down.db"
    config = _config(path)
    command.upgrade(config, "0006")
    command.downgrade(config, "0005")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            names = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master"))}
        assert CAMPAIGN_TABLE not in names and "ix_campaign_run_id" not in names
        assert "platform_setting" in names, "0005's tables stay"
    finally:
        engine.dispose()
