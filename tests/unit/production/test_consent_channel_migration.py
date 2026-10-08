"""`0007_consent_channel` (Plan J M99, DEC-1309): a nullable `consent_record.channel`.

Null means every channel, so a record stored before the revision keeps applying to all of them, on
Postgres (Alembic) and on a laptop's SQLite `platform.db` alike (`add_missing_columns`, DEC-882's
pattern). The scoring seam opens the ledger with `create=False`; an old file must not break it.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, text
from sqlmodel import SQLModel

from engine.audit.events import principal_hash
from engine.privacy.consent import ConsentLedger, principal_key
from engine.privacy.tables import CONSENT_RECORD_TABLE, ConsentRecordRow, create_privacy_tables
from tests.fixtures.postgres import alembic_upgrade

_ = ConsentRecordRow  # registers the table with the metadata
REPO_ROOT: Path = Path(__file__).resolve().parents[3]
SALT = "migration-salt-0001"
PURPOSE = "marketing_communication"
AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _config(path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"), cmd_opts=Namespace(x=[f"url=sqlite:///{path}"]))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


def _columns(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {row[1] for row in connection.execute(text(f"PRAGMA table_info({CONSENT_RECORD_TABLE})"))}


def _old_grant(engine: Engine, ledger_hash: str) -> None:
    """A grant written before 0007, by the columns 0003 created."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO consent_record (client_id, principal_hash, purpose, status, source, recorded_at, "
                "created_at) VALUES ('acme', :hash, :purpose, 'granted', 'csv_import', "
                "'2026-09-01 10:00:00', '2026-09-01 10:00:00')"
            ),
            {"hash": ledger_hash, "purpose": PURPOSE},
        )


def _drift(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, SQLModel.metadata)
    return [item for item in differences if CONSENT_RECORD_TABLE in repr(item)]


def test_0007_matches_the_model(tmp_path: Path) -> None:
    path = tmp_path / "head.db"
    alembic_upgrade(f"sqlite:///{path}")
    engine = create_engine(f"sqlite:///{path}")
    try:
        assert "channel" in _columns(engine)
        assert _drift(engine) == []
    finally:
        engine.dispose()


def test_a_record_from_before_0007_applies_to_every_channel(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    config = _config(path)
    command.upgrade(config, "0006")
    engine = create_engine(f"sqlite:///{path}")
    try:
        _old_grant(engine, principal_hash(principal_key("C-1"), salt=SALT))  # no ledger opened yet
        command.upgrade(config, "0007")
        ledger = ConsentLedger(engine, salt=SALT, create=False)
        for channel in (None, "sms", "email"):
            assert "C-1" in ledger.classify("acme", PURPOSE, ["C-1"], AT, channel=channel).valid, channel
    finally:
        engine.dispose()


def test_0007_downgrades_to_0006(tmp_path: Path) -> None:
    path = tmp_path / "down.db"
    config = _config(path)
    command.upgrade(config, "0007")
    command.downgrade(config, "0006")
    engine = create_engine(f"sqlite:///{path}")
    try:
        assert "channel" not in _columns(engine)
    finally:
        engine.dispose()


def test_an_old_sqlite_platform_db_keeps_working_for_the_scoring_seam(tmp_path: Path) -> None:
    """A `platform.db` made before M99 (no `channel`): the scoring seam's `create=False` ledger reads it.

    `create_all` never adds a column, so without `add_missing_columns` every ledger read - which now
    selects `channel` - would fail with "no such column" and a gated scoring run with it.
    """
    path = tmp_path / "platform.db"
    command.upgrade(_config(path), "0006")  # the table exactly as Phase 4b made it
    engine = create_engine(f"sqlite:///{path}")
    try:
        assert "channel" not in _columns(engine)
        ledger = ConsentLedger(engine, salt=SALT, create=False)
        _old_grant(engine, ledger.hash("C-1"))
        assert "channel" in _columns(engine)
        assert "C-1" in ledger.classify("acme", PURPOSE, ["C-1"], AT, channel="sms").valid
        assert "C-1" in ledger.classify("acme", PURPOSE, ["C-1"], AT).valid
        create_privacy_tables(engine)  # idempotent once the column is there
        assert "C-1" in ledger.classify("acme", PURPOSE, ["C-1"], AT, channel="email").valid
    finally:
        engine.dispose()
