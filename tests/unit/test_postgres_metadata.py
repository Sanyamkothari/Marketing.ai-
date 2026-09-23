"""`engine.aws.postgres`: the translation from a `Settings` to a connection, and what it must not do.

Two kinds of test here. The offline ones build engines without connecting to anything - SQLAlchemy
does not dial until a statement runs - and check the translation, the table scoping and the rules
this module is supposed to keep (no second reader of the environment, no boto3, no `create_all` on
Postgres). The `postgres`-marked ones connect to a real server and check the things only a real
server can answer: that the columns really are `timestamp with time zone`, and that `search_path`
really points where the config said.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from engine.aws import postgres as postgres_module
from engine.aws.postgres import (
    METADATA_TABLES,
    RUN_TABLE,
    PostgresConfig,
    SqlRunIndex,
    create_run_index_tables,
    normalise_url,
    postgres_engine,
    postgres_run_index,
    postgres_store,
)
from engine.platform_db import PLATFORM_TABLES
from engine.registry import (
    MODEL_VERSION_TABLE,
    LocalModelRegistry,
    SqlRegistryStore,
    create_registry_tables,
)
from engine.settings import ENV_VARS, Settings, SettingsError
from tests.fixtures.postgres import (  # noqa: F401 - imported so pytest can resolve them by name
    postgres_engine_fixture,
    postgres_schema,
    postgres_url_value,
)

FAKE_URL: str = "postgresql://someone:secret@db.example.invalid:5432/marketing"
"""Never connected to. It exists to be translated, and its password is here to prove it is hidden."""


def deployment(**overrides: object) -> Settings:
    """A Postgres deployment, described the way `Settings` wants it."""
    values: dict[str, object] = {
        "metadata_backend": "postgres",
        "postgres_dsn": FAKE_URL,
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# The translation from Settings to a connection (DEC-343)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_a_bare_dsn_is_pointed_at_the_driver_this_product_installs(given: str, expected: str) -> None:
    """The URL RDS shows an operator names no driver, and SQLAlchemy reads that as psycopg 2."""
    assert normalise_url(given) == expected


def test_from_settings_takes_the_url_the_schema_and_the_client_id() -> None:
    settings = deployment(postgres_schema="marketing", client_id="telco")
    config = PostgresConfig.from_settings(settings)
    assert config.url == "postgresql+psycopg://someone:secret@db.example.invalid:5432/marketing"
    assert config.schema_name == "marketing"
    assert config.application_name == "telco"


def test_the_search_path_is_set_on_the_connection_not_afterwards() -> None:
    config = PostgresConfig.from_settings(deployment(postgres_schema="marketing"))
    assert config.connect_args()["options"] == "-csearch_path=marketing,public"


def test_no_schema_leaves_the_connections_own_search_path_alone() -> None:
    assert "options" not in PostgresConfig.from_settings(deployment()).connect_args()


def test_a_schema_that_is_not_an_identifier_is_refused_by_name() -> None:
    """It is interpolated into `search_path`, so it is checked rather than quoted and hoped for."""
    with pytest.raises(SettingsError) as excinfo:
        PostgresConfig.from_settings(deployment(postgres_schema="public;DROP SCHEMA public"))
    assert excinfo.value.code == "SETTINGS_INVALID"
    assert excinfo.value.env_var == ENV_VARS["postgres_schema"]
    assert "DROP" not in excinfo.value.message


def test_a_sqlite_deployment_has_no_postgres_config() -> None:
    with pytest.raises(SettingsError) as excinfo:
        PostgresConfig.from_settings(Settings())
    assert excinfo.value.code == "SETTINGS_INCOMPLETE"
    assert excinfo.value.env_var == ENV_VARS["postgres_dsn"]


def test_the_password_is_not_in_the_engines_repr() -> None:
    """SQLAlchemy hides it in `repr(engine.url)`; this asserts the product relies on that, not luck."""
    engine = postgres_engine(PostgresConfig.from_settings(deployment()))
    try:
        assert "secret" not in repr(engine)
        assert "secret" not in str(engine.url)
    finally:
        engine.dispose()


def test_postgres_store_is_the_same_store_the_laptop_uses() -> None:
    """`engine.settings.build_registry` calls exactly this; the signature is fixed (DEC-338)."""
    store = postgres_store(deployment())
    try:
        assert isinstance(store, SqlRegistryStore)
        assert store.engine.dialect.name == "postgresql"
    finally:
        store.engine.dispose()


def test_a_sqlite_deployment_is_told_it_has_no_run_index_rather_than_given_an_empty_one() -> None:
    with pytest.raises(SettingsError) as excinfo:
        postgres_run_index(Settings())
    assert excinfo.value.env_var == ENV_VARS["metadata_backend"]
    assert "no run index" in excinfo.value.message


def test_this_module_is_not_a_second_reader_of_the_environment() -> None:
    """DEC-343: `Settings` reads the environment once. A `from_env` here would be a second answer."""
    source = Path(postgres_module.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv" not in source
    assert not hasattr(PostgresConfig, "from_env")
    assert not hasattr(postgres_module, "metadata_backend")


def test_this_module_imports_no_driver_and_no_boto3_at_module_scope() -> None:
    """The package rule (DEC-306), plus psycopg: SQLAlchemy loads the driver at connect time."""
    source = Path(postgres_module.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and not line.startswith((" ", "\t")):
            assert "boto3" not in stripped
            assert "psycopg" not in stripped


# ---------------------------------------------------------------------------
# DEC-340: create_all is scoped to one table
# ---------------------------------------------------------------------------
def table_names(path: Path) -> set[str]:
    """Every table in a SQLite file, by name."""
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_a_registry_file_never_gets_the_run_table_just_because_this_module_was_imported() -> None:
    """The defect DEC-340 exists to prevent, asserted on the file itself.

    `engine.aws.postgres` is imported at the top of this module, so `SQLModel.metadata` already
    holds `run`. An unscoped `SQLModel.metadata.create_all` in `LocalModelRegistry.__init__` would
    therefore create a run index inside every `registry.db` in the world - on a deployment that
    chose SQLite precisely because it wanted none of this.
    """
    assert RUN_TABLE in {table.name for table in postgres_module.SQLModel.metadata.sorted_tables}


def test_the_registry_creates_only_its_own_table(tmp_path: Path) -> None:
    registry = LocalModelRegistry(tmp_path / "registry.db")
    try:
        assert table_names(registry.db_path) == {MODEL_VERSION_TABLE}
    finally:
        registry.engine.dispose()


def test_the_run_index_creates_only_its_own_table(tmp_path: Path) -> None:
    from sqlmodel import create_engine

    path = tmp_path / "index.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        create_run_index_tables(engine)
        assert table_names(path) == {RUN_TABLE}
    finally:
        engine.dispose()


def test_the_two_creators_together_make_exactly_the_migrated_set(tmp_path: Path) -> None:
    """Whatever `0001` creates, these two together must create, or the local path is not the same."""
    from sqlmodel import create_engine

    path = tmp_path / "both.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        create_registry_tables(engine)
        create_run_index_tables(engine)
        assert table_names(path) == set(METADATA_TABLES)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Against a real server
# ---------------------------------------------------------------------------
@pytest.mark.postgres
def test_every_timestamp_column_really_is_timestamptz(
    postgres_engine_fixture: Engine,  # noqa: F811 - the parameter IS the imported fixture
    postgres_schema: str,  # noqa: F811
) -> None:
    """DEC-339 as the server reports it, which is the only report that settles the question.

    A `TIMESTAMP WITHOUT TIME ZONE` here would mean the migration and the models agree with each
    other and are both wrong; asking `information_schema` asks something that agrees with neither.
    """
    with postgres_engine_fixture.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = :schema AND data_type LIKE 'timestamp%' "
                "ORDER BY table_name, column_name"
            ),
            {"schema": postgres_schema},
        ).all()
    found = {(table, column): kind for table, column, kind in rows}
    assert found == {
        ("model_version", "approved_at"): "timestamp with time zone",
        ("model_version", "created_at"): "timestamp with time zone",
        ("model_version", "promoted_at"): "timestamp with time zone",
        ("run", "created_at"): "timestamp with time zone",
        ("run", "finished_at"): "timestamp with time zone",
        ("run", "indexed_at"): "timestamp with time zone",
        ("run", "started_at"): "timestamp with time zone",
        # Phase 4b, 0002_access_audit (DEC-721)
        ("audit_events", "occurred_at"): "timestamp with time zone",
        ("auth_session", "created_at"): "timestamp with time zone",
        ("auth_session", "expires_at"): "timestamp with time zone",
        ("auth_session", "revoked_at"): "timestamp with time zone",
        ("platform_user", "created_at"): "timestamp with time zone",
        ("platform_user", "updated_at"): "timestamp with time zone",
        # Phase 4b, 0003_privacy (DEC-733)
        ("consent_record", "created_at"): "timestamp with time zone",
        ("consent_record", "expires_at"): "timestamp with time zone",
        ("consent_record", "recorded_at"): "timestamp with time zone",
        ("erasure_request", "completed_at"): "timestamp with time zone",
        ("erasure_request", "requested_at"): "timestamp with time zone",
        ("model_retrain_flag", "cleared_at"): "timestamp with time zone",
        ("model_retrain_flag", "created_at"): "timestamp with time zone",
        # Phase 4b, 0004_scheduling (DEC-774)
        ("alert", "acknowledged_at"): "timestamp with time zone",
        ("alert", "created_at"): "timestamp with time zone",
        ("schedule", "created_at"): "timestamp with time zone",
        ("schedule", "last_fired_at"): "timestamp with time zone",
        ("schedule", "next_due_at"): "timestamp with time zone",
        ("schedule", "updated_at"): "timestamp with time zone",
        ("schedule_firing", "finished_at"): "timestamp with time zone",
        ("schedule_firing", "fired_at"): "timestamp with time zone",
        ("schedule_firing", "scheduled_for"): "timestamp with time zone",
        # Plan D, 0005_plan_d (DEC-862, DEC-863)
        ("erasure_progress", "updated_at"): "timestamp with time zone",
        ("model_decision", "decided_at"): "timestamp with time zone",
    }


@pytest.mark.postgres
def test_the_connection_lands_in_the_configured_schema(
    postgres_engine_fixture: Engine,  # noqa: F811
    postgres_schema: str,  # noqa: F811
) -> None:
    """A write with no schema qualification must go where `postgres_schema` said, not to `public`."""
    index = SqlRunIndex(postgres_engine_fixture)
    assert index.run_ids() == ()
    with postgres_engine_fixture.connect() as connection:
        assert connection.execute(text("SELECT current_schema()")).scalar_one() == postgres_schema


@pytest.mark.postgres
def test_the_migrated_schema_holds_exactly_this_products_tables(
    postgres_engine_fixture: Engine,  # noqa: F811
    postgres_schema: str,  # noqa: F811
) -> None:
    with postgres_engine_fixture.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"),
                {"schema": postgres_schema},
            )
        }
    assert names == {*METADATA_TABLES, *PLATFORM_TABLES, "alembic_version"}
