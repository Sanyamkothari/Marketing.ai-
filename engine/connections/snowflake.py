"""Snowflake, an optional add-on: `pip install 'marketing-ai[snowflake]'` (Plan H M80).

The SDK (`snowflake-connector-python`) is imported only when a Snowflake connection is used, so a
server without it still lists the card - marked "needs the add-on" - and starts as fast as before.
Snowflake has no read-only session switch, so the only statements run are the listing queries and
`SELECT * FROM "<schema>"."<table>" LIMIT n` with both names quoted by `quote_identifier`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from engine.connections.base import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    ConfigValue,
    ConnectorError,
    FieldSpec,
    addon_missing,
    load_sdk,
    text_value,
)
from engine.connections.sql import SqlConnector

__all__ = ["SnowflakeConnector", "quote_identifier", "snowflake_select"]


def quote_identifier(name: str) -> str:
    """A Snowflake identifier in double quotes, each double quote inside doubled (exact, case-sensitive)."""
    if not name or "\x00" in name or len(name) > 255:
        raise ConnectorError(
            "CONNECTION_OBJECT_NOT_FOUND",
            "That is not a table name.",
            "Open the list again and pick a table.",
            status=404,
        )
    return '"' + name.replace('"', '""') + '"'


def snowflake_select(schema: str, table: str, limit: int) -> str:
    return f"SELECT * FROM {quote_identifier(schema)}.{quote_identifier(table)} LIMIT {int(limit)}"


class SnowflakeConnector(SqlConnector):
    kind = "snowflake"
    label = "Snowflake"
    description = "Tables in a Snowflake data warehouse. Needs the Snowflake add-on."
    default_port = 443
    tier = "optional"
    extra = "snowflake"

    def fields(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                name="account",
                label="Account identifier",
                required=True,
                placeholder="myorg-myaccount",
                help="The part of your Snowflake address before .snowflakecomputing.com.",
            ),
            FieldSpec(name="warehouse", label="Warehouse", required=True, placeholder="COMPUTE_WH"),
            FieldSpec(name="database", label="Database", required=True),
            FieldSpec(
                name="user", label="User name", required=True, help="Ask for a user whose role can only read."
            ),
            FieldSpec(name="password", label="Password", type="password", secret=True, required=True),
            FieldSpec(name="role", label="Role", advanced=True, placeholder="READER"),
            FieldSpec(name="schema", label="Only this schema", advanced=True),
        ]

    def available(self) -> bool:
        return load_sdk("snowflake.connector") is not None

    def needs_reach(self, config: Mapping[str, ConfigValue]) -> tuple[str, int] | None:  # noqa: ARG002
        return None  # the SDK reaches the account itself

    def connect(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> Any:
        sdk = load_sdk("snowflake.connector")
        if sdk is None:
            raise addon_missing(self.label, self.extra or "snowflake")
        options: dict[str, Any] = {}
        if text_value(config, "role"):
            options["role"] = text_value(config, "role")
        try:
            return sdk.connect(
                account=text_value(config, "account"),
                user=text_value(config, "user"),
                password=secrets.get("password", ""),
                warehouse=text_value(config, "warehouse"),
                database=text_value(config, "database"),
                login_timeout=CONNECT_TIMEOUT_S,
                network_timeout=READ_TIMEOUT_S,
                application="marketing-ai",
                **options,
            )
        except Exception as exc:
            raise _sign_in_error(exc) from None

    def list_schemas(self, connection: Any) -> list[str]:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")
            names = [str(row[0]) for row in cursor.fetchall()]
        finally:
            cursor.close()
        return [n for n in names if n.upper() != "INFORMATION_SCHEMA"]

    def list_tables(self, connection: Any, schema: str) -> list[str]:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s "
                "ORDER BY table_name LIMIT 1001",
                (schema,),
            )
            return [str(row[0]) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def select_sql(self, schema: str, table: str, limit: int) -> Any:
        return snowflake_select(schema, table, limit)

    def write_privileges(self, connection: Any) -> int | None:  # noqa: ARG002
        return None

    def read_only_note(self) -> str:
        return "Marketing AI only runs SELECT queries."


def _sign_in_error(exc: BaseException) -> ConnectorError:
    errno = getattr(exc, "errno", None)
    text = str(exc).lower()
    if errno in {250001, 250003} or "could not connect" in text or "timed out" in text:
        return ConnectorError(
            "CONNECTION_UNREACHABLE",
            "Snowflake did not answer for this account.",
            "Check the account identifier: it is the part of your Snowflake address before "
            ".snowflakecomputing.com.",
        )
    if errno == 390100 or "incorrect username or password" in text:
        return ConnectorError(
            "CONNECTION_SIGN_IN_FAILED",
            "The user name or password was refused.",
            "Check the user name and enter the password again.",
        )
    return ConnectorError(
        "CONNECTION_SIGN_IN_FAILED",
        "Signing in to Snowflake failed.",
        "Check the account, warehouse, database, user name and password.",
    )
