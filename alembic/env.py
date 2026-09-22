"""How Alembic finds the database and what it compares against.

Phase 1's registry creates its table with `create_all`, which adds a missing table and never a
missing column. That is a fair trade for a file on a laptop that can be deleted, and it is not one
for a client's model and run history: the first column added after go-live would be missing in
production, silently, until a query failed. So the Postgres schema is owned by migrations from
`0001` on, and `0001` creates the tables as they stand rather than pretending the SQLite file was
ever migrated from (DEC-341).

Three choices worth stating:

**The URL is resolved here, never stored in `alembic.ini`.** `-x url=...` wins, then a
`sqlalchemy.url` an operator has put in their own copy of the ini, then `engine.settings.Settings` -
the same object `api/deps.py` and the job container build from, so `alembic upgrade head` cannot
migrate a database the product is not using (DEC-345).

**`compare_type=True`.** Autogenerate defaults to ignoring type changes, which would let exactly
the defect this milestone fixed - a `TIMESTAMP` where a `TIMESTAMP WITH TIME ZONE` belongs - pass a
drift check silently (DEC-339).

**Only this product's tables are considered.** `SQLModel.metadata` is a process-wide namespace;
`engine.aws.postgres.METADATA_TABLES` is the definite list, and `include_name` holds autogenerate to
it so an unrelated table in the same database is neither dropped nor reported as drift (DEC-340).
"""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import Connection, MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

# Imported for their side effect: a `table=True` class registers itself with `SQLModel.metadata`,
# and a table this process has not imported is a table autogenerate would propose dropping.
from engine.aws.postgres import METADATA_TABLES, PostgresConfig, normalise_url, postgres_engine
from engine.registry import ModelVersionRow
from engine.settings import load_settings

_ = ModelVersionRow  # the import above is the point; this line keeps linters from removing it.

target_metadata: MetaData = SQLModel.metadata
"""What the migrations are compared against. Filtered to `METADATA_TABLES` by `include_name`."""

SCHEMA_ARGUMENT: str = "schema"
"""`-x schema=...`, for running a migration into a schema without describing a whole deployment."""

URL_ARGUMENT: str = "url"
"""`-x url=...`. The first thing `resolve_url` looks at, and the only one a test uses."""


def x_arguments() -> dict[str, str]:
    """The `-x key=value` pairs this invocation was given, as a mapping."""
    raw: dict[str, str] = context.get_x_argument(as_dictionary=True)
    return raw


def resolve_url(fallback: str | None) -> tuple[str, str | None]:
    """`(url, schema)` for this invocation: `-x url=`, then the ini, then `Settings`.

    The schema follows the same source as the URL it belongs to - `-x schema=` with `-x url=`, and
    `postgres_schema` when the URL came from the settings - so the two can never come from
    different deployments.
    """
    arguments = x_arguments()
    explicit = arguments.get(URL_ARGUMENT, "").strip()
    if explicit:
        return normalise_url(explicit), arguments.get(SCHEMA_ARGUMENT) or None
    from_ini = (fallback or "").strip()
    if from_ini:
        return normalise_url(from_ini), arguments.get(SCHEMA_ARGUMENT) or None
    config = PostgresConfig.from_settings(load_settings())
    return config.url, config.schema_name


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Restrict autogenerate to this product's tables (DEC-340)."""
    del parent_names
    if type_ == "table":
        return name in METADATA_TABLES
    return True


def context_options(schema: str | None) -> dict[str, Any]:
    """The options every one of the four configure paths below shares."""
    options: dict[str, Any] = {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
        "include_name": include_name,
        "include_schemas": schema is not None,
    }
    if schema is not None:
        options["version_table_schema"] = schema
    return options


def build_engine(url: str, schema: str | None) -> Engine:
    """An engine for `url`.

    A Postgres URL goes through `postgres_engine`, so a migration connects exactly the way the
    application does - same driver, same `search_path`, same `application_name` in
    `pg_stat_activity`. Anything else (SQLite, in the drift test) gets a plain engine, because those
    connection arguments are libpq's and no other driver accepts them.
    """
    if url.startswith("postgresql"):
        return postgres_engine(PostgresConfig(url=url, schema_name=schema))
    return create_engine(url)


def ensure_schema(connection: Connection, schema: str | None) -> None:
    """Create the configured schema if it is missing, before anything tries to write into it.

    The name has already been checked against `PostgresConfig`'s identifier pattern, which is why it
    can be interpolated here; a DDL statement cannot take a bound parameter for an identifier.
    """
    if schema is None or connection.dialect.name != "postgresql":
        return
    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


def run_migrations_offline(url: str, schema: str | None) -> None:
    """Emit the SQL for this upgrade to stdout instead of running it (`alembic upgrade --sql`)."""
    context.configure(
        url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **context_options(schema)
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online(url: str, schema: str | None) -> None:
    """Connect and run the migrations in one transaction."""
    engine = build_engine(url, schema)
    try:
        with engine.connect() as connection:
            ensure_schema(connection, schema)
            connection.commit()
            context.configure(connection=connection, **context_options(schema))
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


url, schema = resolve_url(context.config.get_main_option("sqlalchemy.url"))
if context.is_offline_mode():
    run_migrations_offline(url, schema)
else:
    run_migrations_online(url, schema)
