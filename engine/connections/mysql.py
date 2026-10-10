"""MySQL and MariaDB, through `pymysql` (pure Python, pinned; Plan H M80).

The session is made read-only with `SET SESSION TRANSACTION READ ONLY` (MySQL 5.6.5+, MariaDB 10.0+).
An identifier is quoted by `quote_identifier` - wrapped in back-ticks with every back-tick inside
doubled, as MySQL's own grammar defines - and must first appear in `information_schema`; values
(a schema name in a listing query) are passed as parameters, never pasted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Final

from engine.connections.base import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    ConfigValue,
    ConnectorError,
    FieldSpec,
    bool_value,
    int_value,
    load_sdk,
    text_value,
)
from engine.connections.sql import SqlConnector, database_fields, date_literal

__all__ = ["MySqlConnector", "mysql_select", "mysql_window", "quote_identifier"]

_SYSTEM_SCHEMAS: Final[frozenset[str]] = frozenset(
    {"information_schema", "mysql", "performance_schema", "sys"}
)
_MAX_IDENTIFIER: Final[int] = 64
_WRITE_GRANT: Final[re.Pattern[str]] = re.compile(
    r"\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", re.IGNORECASE
)


def quote_identifier(name: str) -> str:
    """A MySQL identifier quoted for SQL: `` `name` ``, with each back-tick inside doubled.

    Refuses what MySQL itself refuses in an identifier (empty, longer than 64 characters, a NUL, or
    trailing spaces), so a name that cannot be a table is never turned into SQL at all.
    """
    if not name or len(name) > _MAX_IDENTIFIER or "\x00" in name or name != name.rstrip(" "):
        raise ConnectorError(
            "CONNECTION_OBJECT_NOT_FOUND",
            "That is not a table name this database could have.",
            "Open the list again and pick a table.",
            status=404,
        )
    return "`" + name.replace("`", "``") + "`"


def mysql_select(schema: str, table: str, limit: int) -> str:
    """`SELECT * FROM `schema`.`table` LIMIT n` with both names quoted and `n` an integer."""
    return f"SELECT * FROM {quote_identifier(schema)}.{quote_identifier(table)} LIMIT {int(limit)}"


def mysql_window(schema: str, table: str, column: str, start: date, end: date, limit: int) -> str:
    """Plan J M107 (DEC-1317): `mysql_select` with one `WHERE` - a date window on a back-ticked column."""
    name = quote_identifier(column)
    return (
        f"SELECT * FROM {quote_identifier(schema)}.{quote_identifier(table)} "
        f"WHERE {name} >= {date_literal(start)} AND {name} < {date_literal(end)} LIMIT {int(limit)}"
    )


class MySqlConnector(SqlConnector):
    kind = "mysql"
    label = "MySQL / MariaDB"
    description = "Tables in a MySQL or MariaDB database (also Amazon RDS and Aurora MySQL)."
    default_port = 3306

    def fields(self) -> list[FieldSpec]:
        fields = [f for f in database_fields(port=self.default_port) if f.name != "schema"]
        return [
            *fields,
            FieldSpec(
                name="ssl",
                label="Require an encrypted connection",
                type="checkbox",
                advanced=True,
                default=False,
            ),
        ]

    def available(self) -> bool:
        return load_sdk("pymysql") is not None

    def connect(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> Any:
        pymysql = load_sdk("pymysql")
        if pymysql is None:
            raise ConnectorError(
                "CONNECTION_NEEDS_ADDON",
                "The MySQL add-on (pymysql) is not installed on this server.",
                "Ask whoever runs Marketing AI to install it: pip install pymysql.",
                status=409,
            )
        options: dict[str, Any] = {}
        if bool_value(config, "ssl"):
            options["ssl"] = {"check_hostname": True}
        try:
            connection = pymysql.connect(
                host=text_value(config, "host"),
                port=int_value(config, "port", self.default_port),
                user=text_value(config, "user"),
                password=secrets.get("password", ""),
                database=text_value(config, "database"),
                connect_timeout=CONNECT_TIMEOUT_S,
                read_timeout=READ_TIMEOUT_S,
                write_timeout=READ_TIMEOUT_S,
                charset="utf8mb4",
                autocommit=True,
                **options,
            )
        except pymysql.MySQLError as exc:
            raise _sign_in_error(exc, config) from None
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
        except pymysql.MySQLError:
            connection.close()
            raise ConnectorError(
                "CONNECTION_NOT_READ_ONLY",
                "The session could not be made read-only, so it was closed.",
                "Marketing AI needs MySQL 5.6.5 or MariaDB 10.0 or later.",
            ) from None
        return connection

    def open_cursor(self, connection: Any) -> Any:
        pymysql = load_sdk("pymysql")
        if pymysql is None:  # pragma: no cover - connect() already required it
            return connection.cursor()
        return connection.cursor(pymysql.cursors.SSCursor)  # unbuffered: rows stream

    def _schemas(self, connection: Any, config: Mapping[str, ConfigValue]) -> list[str]:
        """MySQL's schema *is* the database: only the one named on the connection is browsed."""
        database = text_value(config, "database")
        return [database] if database in self.list_schemas(connection) else []

    def list_schemas(self, connection: Any) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")
            names = [str(row[0]) for row in cursor.fetchall()]
        return [n for n in names if n.lower() not in _SYSTEM_SCHEMAS]

    def list_tables(self, connection: Any, schema: str) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s "
                "ORDER BY table_name LIMIT 1001",
                (schema,),
            )
            return [str(row[0]) for row in cursor.fetchall()]

    def select_sql(self, schema: str, table: str, limit: int) -> Any:
        return mysql_select(schema, table, limit)

    def window_sql(self, schema: str, table: str, column: str, start: date, end: date, limit: int) -> Any:
        return mysql_window(schema, table, column, start, end, limit)

    def write_privileges(self, connection: Any) -> int | None:
        with connection.cursor() as cursor:
            cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
            grants = [str(row[0]) for row in cursor.fetchall()]
        if not grants:
            return None
        return sum(1 for g in grants if _WRITE_GRANT.search(g.split(" ON ", 1)[0]))

    def read_only_note(self) -> str:
        return "The session is read-only, so Marketing AI cannot change anything."


def _sign_in_error(exc: BaseException, config: Mapping[str, ConfigValue]) -> ConnectorError:
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else 0
    user = text_value(config, "user")
    if code == 1045:
        return ConnectorError(
            "CONNECTION_SIGN_IN_FAILED",
            f"The user name or password was refused (user {user}).",
            "Check the user name and enter the password again.",
        )
    if code in {1044, 1049}:
        return ConnectorError(
            "CONNECTION_DATABASE_NOT_FOUND",
            f"There is no database called {text_value(config, 'database')} that this user can open.",
            "Check the database name's spelling, and that the user may read it.",
        )
    if code in {2003, 2005, 2006, 2013}:
        return ConnectorError(
            "CONNECTION_UNREACHABLE",
            "The database server did not answer.",
            "Check the address and port, and that the server accepts connections from this computer.",
        )
    return ConnectorError(
        "CONNECTION_SIGN_IN_FAILED",
        "Signing in to the database failed.",
        "Check the address, database name, user name and password.",
    )
