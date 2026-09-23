"""The migrations: that they run, that `alembic.ini` holds no DSN, and that they match the models.

The heart of this module is the drift test. A migration tool only helps if the migrations and the
models say the same thing, and the way that stops being true is undramatic: somebody adds a column
to a SQLModel class, the local SQLite file picks it up from `create_all`, every test passes, and
the deployment is missing a column nobody will notice until a query fails. So `alembic upgrade
head` is run into an empty database and `alembic.autogenerate.compare_metadata` is asked whether it
would want to change anything. It must want nothing.
"""

from __future__ import annotations

import configparser
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlmodel import SQLModel

from engine.aws.postgres import METADATA_TABLES, PostgresConfig, postgres_engine
from engine.platform_db import PLATFORM_TABLES
from engine.registry import ModelVersionRow
from tests.fixtures.postgres import (  # noqa: F401 - imported so pytest can resolve them by name
    alembic_upgrade,
    postgres_schema,
    postgres_url_value,
)

_ = ModelVersionRow  # imported for its side effect: registering `model_version` with the metadata

REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI: Path = REPO_ROOT / "alembic.ini"
VERSIONS: Path = REPO_ROOT / "alembic" / "versions"


def drift(engine: Engine) -> list[Any]:
    """What autogenerate would still want to change about `engine`, ignoring tables that are not ours.

    `compare_type=True` matters more than anything else here: without it, a `TIMESTAMP` where a
    `TIMESTAMP WITH TIME ZONE` belongs is not drift at all as far as Alembic is concerned, and the
    defect DEC-339 fixed would sail through this test.
    """
    ours = {*METADATA_TABLES}
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        differences = compare_metadata(context, SQLModel.metadata)
    return [item for item in differences if _table_of(item) in ours]


def _table_of(item: Any) -> str | None:
    """The table a difference is about; `alembic_version` and anything else is not this test's."""
    if isinstance(item, list):  # a column change arrives as a list of one-per-attribute tuples
        item = item[0]
    if not isinstance(item, tuple):
        return None
    for part in item:
        if hasattr(part, "name") and hasattr(part, "columns"):  # a Table
            return str(part.name)
    return next((part for part in item if isinstance(part, str) and part in METADATA_TABLES), None)


@pytest.fixture
def sqlite_head(tmp_path: Path) -> Engine:
    """An empty SQLite database migrated to head with the real `alembic upgrade`."""
    path = tmp_path / "head.db"
    alembic_upgrade(f"sqlite:///{path}")
    return create_engine(f"sqlite:///{path}")


# ---------------------------------------------------------------------------
# alembic.ini carries no DSN (DEC-345)
# ---------------------------------------------------------------------------
def test_the_ini_names_no_database() -> None:
    """A DSN in a committed file is a credential in a committed file, whatever today's value is."""
    parser = configparser.ConfigParser()
    parser.read(ALEMBIC_INI)
    assert parser.get("alembic", "sqlalchemy.url", fallback="").strip() == ""
    settings = [
        line
        for line in ALEMBIC_INI.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]
    assert not [line for line in settings if "://" in line], settings


def test_the_migrations_are_one_linear_history_from_the_first() -> None:
    """One linear history. A branch is a thing somebody has to merge, and nothing here needs one.

    Phase 4b adds revisions after `0001` (`0002_access_audit`, ...), so "exactly one" became "one
    chain": the first file is still `0001_initial.py` with no parent, and every later file names the
    one before it as its `down_revision` - no gap, no fork, no second root (DEC-721).
    """
    revisions = sorted(path.name for path in VERSIONS.glob("*.py"))
    assert revisions[0] == "0001_initial.py"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_bases() == ["0001"]
    assert len(script.get_heads()) == 1
    chain = [revision.revision for revision in script.walk_revisions()]  # head first, down to the base
    assert len(chain) == len(revisions)
    assert [name.split("_", 1)[0] for name in revisions] == chain[::-1]


# ---------------------------------------------------------------------------
# The URL resolution order (DEC-345)
# ---------------------------------------------------------------------------
def test_the_x_argument_decides_where_the_migration_goes(tmp_path: Path) -> None:
    """`-x url=` is first in the order, and it is the one every test and every operator uses."""
    path = tmp_path / "explicit.db"
    alembic_upgrade(f"sqlite:///{path}")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            names = {
                row[0]
                for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            }
        assert names == {*METADATA_TABLES, *PLATFORM_TABLES, "alembic_version"}
    finally:
        engine.dispose()


def test_the_ini_url_is_used_when_there_is_no_x_argument(tmp_path: Path) -> None:
    """Second in the order: an operator's own copy of the ini, which is not the committed one."""
    path = tmp_path / "from-ini.db"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    assert path.is_file()


def test_the_settings_are_the_last_resort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Third in the order: the same description the API and the job container are built from.

    A SQLite URL is used here so the test needs no server; what is being proved is that `env.py`
    reached `Settings` at all, not that it reached Postgres.
    """
    path = tmp_path / "from-settings.db"
    monkeypatch.setattr(
        PostgresConfig,
        "from_settings",
        classmethod(lambda cls, settings: cls(url=f"sqlite:///{path}")),
    )
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(config, "head")
    assert path.is_file()


# ---------------------------------------------------------------------------
# The drift test
# ---------------------------------------------------------------------------
def test_the_migrations_match_the_models_on_sqlite(sqlite_head: Engine) -> None:
    """The drift check, on the backend that is always there - and what it cannot see.

    SQLite CAN catch: a table or a column a migration forgot, a column the models dropped and the
    migration still creates, a nullability that disagrees, and an index that is in one and not the
    other. That is the majority of the ways these two drift apart, and it runs on every machine.

    SQLite CANNOT catch anything that only exists in Postgres. Its type affinity means `TIMESTAMP`
    and `TIMESTAMP WITH TIME ZONE` reflect back the same, so the DEC-339 defect is **invisible
    here**; likewise a schema qualification, a server default's SQL text, a Postgres-specific type
    such as `JSONB`, and constraint names the two dialects render differently. Those need the
    Postgres run below, which is why a machine with no server gets a skip with a reason and CI gets
    a failure instead (DEC-344).
    """
    try:
        assert drift(sqlite_head) == []
    finally:
        sqlite_head.dispose()


@pytest.mark.postgres
def test_the_migrations_match_the_models_on_postgres(
    postgres_url_value: str,  # noqa: F811 - the parameter IS the imported fixture
    postgres_schema: str,  # noqa: F811
) -> None:
    """The same check where the types are real; this is the one that sees DEC-339."""
    engine = postgres_engine(PostgresConfig(url=postgres_url_value, schema_name=postgres_schema))
    try:
        assert drift(engine) == []
    finally:
        engine.dispose()


def test_the_drift_check_would_notice_a_missing_column(sqlite_head: Engine) -> None:
    """The guard against a vacuous pass: an empty diff must mean "they agree", not "nothing ran"."""
    try:
        with sqlite_head.connect() as connection:
            connection.execute(text("ALTER TABLE model_version DROP COLUMN promotion_note"))
            connection.commit()
        differences = drift(sqlite_head)
        assert differences, "the comparison found nothing after a column was dropped"
        assert any("promotion_note" in repr(item) for item in differences)
    finally:
        sqlite_head.dispose()


def test_the_migration_is_reversible(tmp_path: Path) -> None:
    """`downgrade base` empties the schema, so a bad deploy is recoverable at the schema level."""
    path = tmp_path / "reversible.db"
    config = Config(str(ALEMBIC_INI), cmd_opts=Namespace(x=[f"url=sqlite:///{path}"]))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            names = {
                row[0]
                for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            }
        assert names & {*METADATA_TABLES, *PLATFORM_TABLES} == set()
    finally:
        engine.dispose()
