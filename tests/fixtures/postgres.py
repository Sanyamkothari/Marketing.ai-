"""Postgres fixtures, and the one rule about them: a test that does not run says so out loud.

A skipped test is a test that proved nothing, and the dangerous kind is the one nobody notices.
Everything here is built around making the Postgres tests impossible to lose quietly:

* `postgres_unavailable_reason()` returns a **sentence** - which URL was tried and what the driver
  said - and every skip is raised with it, so `-rs` (set in `pyproject.toml`'s `addopts`) prints the
  reason next to the name of each test that did not run.
* `MARKETING_AI_REQUIRE_POSTGRES=1` turns the skip into a failure. That is what CI sets. A pipeline
  that is supposed to have a database and does not should go red, not green with a note.
* The reason is computed once per session and reused, so an unreachable server costs one connection
  attempt and not one per test (DEC-344).

Each test gets its **own schema**, created and dropped around it, so tests do not have to agree
about the order they run in or clean up after each other. The schema is created by Alembic, not by
`create_all`, so what the tests exercise is what a deployment actually gets (DEC-341).

These fixtures are imported by name into the modules that need them - `from tests.fixtures.postgres
import postgres_store` - rather than living in a conftest, because `tests/conftest.py` holds the two
path fixtures and nothing else (design section 11).
"""

from __future__ import annotations

import os
import uuid
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from engine.aws.postgres import PostgresConfig, SqlRunIndex, postgres_engine
from engine.registry import SqlRegistryStore

__all__ = [
    "DEFAULT_TEST_DATABASE_URL",
    "REQUIRE_ENV_VAR",
    "TEST_DATABASE_URL_ENV_VAR",
    "alembic_upgrade",
    "postgres_engine_fixture",
    "postgres_registry",
    "postgres_run_index",
    "postgres_schema",
    "postgres_unavailable_reason",
    "postgres_url",
    "require_postgres",
    "skip_without_postgres",
]

TEST_DATABASE_URL_ENV_VAR: str = "MARKETING_AI_TEST_DATABASE_URL"
"""Where the tests look for a server. Declared in `engine.settings.NON_FIELD_ENV_VARS`: it describes
a test run, not a deployment, and `resolve_values` would otherwise refuse it as a typo (DEC-304)."""

REQUIRE_ENV_VAR: str = "MARKETING_AI_REQUIRE_POSTGRES"
"""Set to `1` to turn "no server" from a skip into a failure. CI sets it; a laptop does not."""

DEFAULT_TEST_DATABASE_URL: str = "postgresql+psycopg://marketing:marketing@127.0.0.1:55432/marketing"
"""The server `make postgres-up` starts. A local development address and not a credential."""

_reason: str | None = None
_probed = False


def postgres_url() -> str:
    """The URL the Postgres tests use: `$MARKETING_AI_TEST_DATABASE_URL`, or the local default."""
    return os.environ.get(TEST_DATABASE_URL_ENV_VAR, "").strip() or DEFAULT_TEST_DATABASE_URL


def _probe(url: str) -> str | None:
    """`None` when a connection succeeded, otherwise a sentence saying what stopped it."""
    try:
        engine = postgres_engine(PostgresConfig(url=url))
    except Exception as exc:
        return f"{type(exc).__name__} building an engine for {_safe(url)}"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        return (
            f"no PostgreSQL server at {_safe(url)}: {type(exc).__name__}. Start one with "
            f"`make postgres-up`, or point {TEST_DATABASE_URL_ENV_VAR} at your own."
        )
    finally:
        engine.dispose()
    return None


def postgres_unavailable_reason() -> str | None:
    """Why the Postgres tests cannot run, or `None` when they can.

    Probed once per session: an unreachable server otherwise costs a TCP timeout per test, and the
    answer cannot change halfway through a run in any way a test should be written against.
    """
    global _reason, _probed
    if not _probed:
        _reason = _probe(postgres_url())
        _probed = True
    return _reason


def require_postgres() -> bool:
    """Whether a missing server must fail the run rather than skip it (`$MARKETING_AI_REQUIRE_POSTGRES`)."""
    return os.environ.get(REQUIRE_ENV_VAR, "").strip() in {"1", "true", "yes", "on"}


def skip_without_postgres() -> None:
    """Skip - or, under `$MARKETING_AI_REQUIRE_POSTGRES`, fail - naming the reason either way."""
    reason = postgres_unavailable_reason()
    if reason is None:
        return
    if require_postgres():
        pytest.fail(f"{REQUIRE_ENV_VAR} is set, so this had to run against PostgreSQL: {reason}")
    pytest.skip(reason)


def _safe(url: str) -> str:
    """A URL with its password replaced, for a message that is allowed to name the server."""
    if "@" not in url:
        return url
    prefix, _, host = url.partition("@")
    scheme, _, credentials = prefix.rpartition("//")
    user = credentials.split(":", 1)[0]
    return f"{scheme}//{user}:***@{host}"


def alembic_upgrade(url: str, schema: str | None = None) -> None:
    """Run `alembic upgrade head` against `url`, exactly as an operator would.

    Through Alembic's own `command` API rather than a subprocess, so a failure raises here with a
    traceback instead of becoming an exit code somebody has to go and read.
    """
    from alembic import command
    from alembic.config import Config

    arguments = [f"url={url}"] + ([] if schema is None else [f"schema={schema}"])
    root = Path(__file__).resolve().parent.parent.parent
    config = Config(str(root / "alembic.ini"), cmd_opts=Namespace(x=arguments))
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def postgres_url_value() -> str:
    """The URL under test, after skipping the whole module when there is no server."""
    skip_without_postgres()
    return postgres_url()


@pytest.fixture
def postgres_schema(postgres_url_value: str) -> Iterator[str]:
    """A schema of this test's own, migrated to head, dropped when the test ends.

    A fresh schema per test rather than a fresh database: creating a database needs a connection to
    another one and a moment's wait, and a schema gives the same isolation for the price of two DDL
    statements.
    """
    name = f"test_{uuid.uuid4().hex[:12]}"
    alembic_upgrade(postgres_url_value, name)
    try:
        yield name
    finally:
        engine = postgres_engine(PostgresConfig(url=postgres_url_value))
        try:
            with engine.connect() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
                connection.commit()
        finally:
            engine.dispose()


@pytest.fixture
def postgres_engine_fixture(postgres_url_value: str, postgres_schema: str) -> Iterator[Engine]:
    """An `Engine` pointed at this test's schema, disposed afterwards."""
    engine = postgres_engine(PostgresConfig(url=postgres_url_value, schema_name=postgres_schema))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def postgres_registry(postgres_engine_fixture: Engine) -> SqlRegistryStore:
    """A registry store over a migrated, empty Postgres schema."""
    return SqlRegistryStore(postgres_engine_fixture)


@pytest.fixture
def postgres_run_index(postgres_engine_fixture: Engine) -> SqlRunIndex:
    """A run index over a migrated, empty Postgres schema."""
    return SqlRunIndex(postgres_engine_fixture)
