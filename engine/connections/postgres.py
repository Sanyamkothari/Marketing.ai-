"""PostgreSQL, and Amazon Redshift through the same protocol (Plan H M80).

The session is made read-only twice over: psycopg opens every transaction `READ ONLY`
(`connection.read_only = True`), and `SET default_transaction_read_only = on` makes the server
refuse a write even from a transaction someone forgot to mark. The only query built from a name is
`SELECT * FROM {schema}.{table} LIMIT {n}` composed with `psycopg.sql.Identifier` and
`psycopg.sql.Literal` - never by pasting strings.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from engine.connections.base import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    ConfigValue,
    ConnectorError,
    FieldSpec,
    addon_missing,
    int_value,
    load_sdk,
    text_value,
)
from engine.connections.sql import SqlConnector, database_fields

__all__ = ["PostgresConnector", "postgres_select"]

_SYSTEM_SCHEMAS: Final[frozenset[str]] = frozenset({"information_schema", "pg_catalog", "pg_toast"})
_SSL_MODES: Final[tuple[str, ...]] = ("prefer", "require", "verify-full", "disable")

_WRITE_PRIVILEGES_SQL: Final[str] = (
    "SELECT count(*) FROM information_schema.table_privileges "
    "WHERE grantee = current_user AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')"
)
_SUPERUSER_SQL: Final[str] = "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
_SCHEMAS_SQL: Final[str] = "SELECT DISTINCT table_schema FROM information_schema.tables ORDER BY table_schema"
_TABLES_SQL: Final[str] = (
    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name LIMIT 1001"
)


def postgres_select(schema: str, table: str, limit: int) -> Any:
    """`SELECT * FROM "<schema>"."<table>" LIMIT <limit>` as a psycopg `Composed`, quoted by psycopg."""
    from psycopg import sql

    return sql.SQL("SELECT * FROM {}.{} LIMIT {}").format(
        sql.Identifier(schema), sql.Identifier(table), sql.Literal(int(limit))
    )


class PostgresConnector(SqlConnector):
    """PostgreSQL (`kind="postgres"`) or Amazon Redshift (`kind="redshift"`)."""

    def __init__(self, *, redshift: bool = False) -> None:
        self.redshift = redshift
        self.kind = "redshift" if redshift else "postgres"
        self.label = "Amazon Redshift" if redshift else "PostgreSQL"
        self.description = (
            "Tables in an Amazon Redshift data warehouse."
            if redshift
            else "Tables in a PostgreSQL database (also Amazon RDS, Aurora, Azure and Google Cloud SQL for PostgreSQL)."
        )
        self.default_port = 5439 if redshift else 5432
        self.extra = "aws"  # psycopg comes with the `aws` extra (DEC-306): optional, like the other SDKs

    def fields(self) -> list[FieldSpec]:
        return [
            *database_fields(port=self.default_port),
            FieldSpec(
                name="sslmode",
                label="Encryption",
                type="select",
                options=_SSL_MODES,
                advanced=True,
                default="require" if self.redshift else "prefer",
                help="“require” encrypts the connection; “verify-full” also checks the server's certificate.",
            ),
        ]

    def available(self) -> bool:
        return load_sdk("psycopg") is not None

    def connect(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> Any:
        psycopg = load_sdk("psycopg")
        if psycopg is None:
            raise addon_missing(self.label, "aws")
        sslmode = text_value(config, "sslmode") or ("require" if self.redshift else "prefer")
        if sslmode not in _SSL_MODES:
            sslmode = "prefer"
        try:
            connection = psycopg.connect(
                host=text_value(config, "host"),
                port=int_value(config, "port", self.default_port),
                dbname=text_value(config, "database"),
                user=text_value(config, "user"),
                password=secrets.get("password", ""),
                connect_timeout=CONNECT_TIMEOUT_S,
                sslmode=sslmode,
                application_name="marketing-ai",
                # Every statement stops after READ_TIMEOUT_S; PostgreSQL only (Redshift ignores options).
                options="" if self.redshift else f"-c statement_timeout={READ_TIMEOUT_S * 1000}",
            )
        except psycopg.Error as exc:
            raise _sign_in_error(exc, config) from None
        try:
            connection.read_only = True
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("SET default_transaction_read_only = on")
            connection.autocommit = False
        except psycopg.Error:
            if not self.redshift:
                connection.close()
                raise ConnectorError(
                    "CONNECTION_NOT_READ_ONLY",
                    "The session could not be made read-only, so it was closed.",
                    "Check the database allows `SET default_transaction_read_only`.",
                ) from None
            connection.autocommit = False  # Redshift: every transaction still begins READ ONLY
        return connection

    def open_cursor(self, connection: Any) -> Any:
        return connection.cursor(name="marketing_ai_fetch")  # server-side: rows stream, never all at once

    def list_schemas(self, connection: Any) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute(_SCHEMAS_SQL)
            names = [str(row[0]) for row in cursor.fetchall()]
        connection.rollback()
        return [n for n in names if n not in _SYSTEM_SCHEMAS and not n.startswith("pg_")]

    def list_tables(self, connection: Any, schema: str) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute(_TABLES_SQL, (schema,))
            names = [str(row[0]) for row in cursor.fetchall()]
        connection.rollback()
        return names

    def select_sql(self, schema: str, table: str, limit: int) -> Any:
        return postgres_select(schema, table, limit)

    def write_privileges(self, connection: Any) -> int | None:
        with connection.cursor() as cursor:
            cursor.execute(_SUPERUSER_SQL)
            row = cursor.fetchone()
            if row is not None and bool(row[0]):
                connection.rollback()
                return 1
            cursor.execute(_WRITE_PRIVILEGES_SQL)
            count = cursor.fetchone()
        connection.rollback()
        return None if count is None else int(count[0])


def _sign_in_error(exc: BaseException, config: Mapping[str, ConfigValue]) -> ConnectorError:
    """A psycopg connection error in plain words. The driver's own text is matched, never repeated."""
    text = str(exc).lower()
    user = text_value(config, "user")
    database = text_value(config, "database")
    if "password authentication failed" in text or "authentication failed" in text:
        return ConnectorError(
            "CONNECTION_SIGN_IN_FAILED",
            f"The user name or password was refused (user {user}).",
            "Check the user name and enter the password again.",
        )
    if "does not exist" in text and "database" in text:
        return ConnectorError(
            "CONNECTION_DATABASE_NOT_FOUND",
            f"There is no database called {database} on this server.",
            "Check the database name's spelling (it is case-sensitive).",
        )
    if "pg_hba.conf" in text or "no encryption" in text:
        return ConnectorError(
            "CONNECTION_SIGN_IN_REFUSED",
            "The server does not accept connections from this computer with these settings.",
            "Ask whoever runs the database to allow this computer's address, or set Encryption to "
            "“require” under More options.",
        )
    if "ssl" in text or "certificate" in text:
        return ConnectorError(
            "CONNECTION_ENCRYPTION_FAILED",
            "The encrypted connection could not be set up.",
            "Try Encryption “require” (or “prefer”) under More options.",
        )
    if "timeout" in text or "timed out" in text:
        return ConnectorError(
            "CONNECTION_UNREACHABLE",
            f"The server did not finish signing in within {CONNECT_TIMEOUT_S} seconds.",
            "Check the address and port, and that the database accepts connections from this computer.",
        )
    return ConnectorError(
        "CONNECTION_SIGN_IN_FAILED",
        "Signing in to the database failed.",
        "Check the address, database name, user name and password.",
    )
