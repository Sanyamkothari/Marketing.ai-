"""A SQLite-backed stand-in for a DB-API database server, and fake SDK modules (Plan H M80).

`FakeServer` holds one SQLite database with each "schema" attached under its own name, so the SQL a
connector really builds - `SELECT * FROM "sales"."customers" LIMIT 20`, or MySQL's back-ticked form,
which SQLite also accepts - runs for real against real rows. The catalogue queries a connector makes
(`information_schema`, grants, `SET ...`) are answered from SQLite's own catalogue, and every
statement is recorded so a test can assert exactly what was sent.
"""

from __future__ import annotations

import sqlite3
import types
from collections.abc import Iterable, Sequence
from typing import Any


class FakeError(Exception):
    """The driver's error class (`psycopg.Error`, `pymysql.MySQLError`)."""


class FakeServer:
    def __init__(self, schemas: dict[str, dict[str, tuple[Sequence[str], Iterable[Sequence[Any]]]]]) -> None:
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.schemas = list(schemas)
        for schema, tables in schemas.items():
            self.db.execute(f"ATTACH ':memory:' AS {_q(schema)}")
            for table, (columns, rows) in tables.items():
                self.db.execute(
                    f"CREATE TABLE {_q(schema)}.{_q(table)} ({', '.join(_q(c) for c in columns)})"
                )
                marks = ", ".join("?" for _ in columns)
                self.db.executemany(f"INSERT INTO {_q(schema)}.{_q(table)} VALUES ({marks})", list(rows))
        self.executed: list[str] = []
        self.connects: list[dict[str, Any]] = []
        self.write_privileges = 0
        self.superuser = False
        self.grants = ["GRANT SELECT ON `shop`.* TO `reader`@`%`"]
        self.refuse_sign_in: Exception | None = None
        self.read_only = False
        self.closed = 0

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connects.append(kwargs)
        if self.refuse_sign_in is not None:
            raise self.refuse_sign_in
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.read_only = False
        self.autocommit = False

    def cursor(self, *args: Any, **kwargs: Any) -> FakeCursor:
        return FakeCursor(self.server)

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.server.closed += 1


class FakeCursor:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.description: list[tuple[str, ...]] = []
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def execute(self, statement: Any, params: Sequence[Any] = ()) -> None:
        sql = statement if isinstance(statement, str) else statement.as_string(None)
        self.server.executed.append(sql)
        upper = sql.upper()
        if upper.startswith("SET "):
            if "READ ONLY" in upper or "READ_ONLY = ON" in upper:
                self.server.read_only = True
            self._answer(["ok"], [])
        elif "INFORMATION_SCHEMA.SCHEMATA" in upper or "SELECT DISTINCT TABLE_SCHEMA" in upper:
            self._answer(["schema_name"], [(s,) for s in self.server.schemas])
        elif "INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA" in upper:
            schema = str(params[0])
            if schema not in self.server.schemas:
                self._answer(["table_name"], [])
                return
            names = self.server.db.execute(
                f"SELECT name FROM {_q(schema)}.sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            self._answer(["table_name"], names)
        elif "ROLSUPER" in upper:
            self._answer(["rolsuper"], [(self.server.superuser,)])
        elif "TABLE_PRIVILEGES" in upper:
            self._answer(["count"], [(self.server.write_privileges,)])
        elif upper.startswith("SHOW GRANTS"):
            self._answer(["grant"], [(g,) for g in self.server.grants])
        elif upper.startswith("SELECT * FROM "):
            if not self.server.read_only:
                raise AssertionError("a table was read before the session was made read-only")
            cursor = self.server.db.execute(sql)
            self.description = [(d[0],) for d in cursor.description]
            self._rows = cursor.fetchall()
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def _answer(self, columns: list[str], rows: Iterable[Sequence[Any]]) -> None:
        self.description = [(c,) for c in columns]
        self._rows = [tuple(r) for r in rows]

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows, []
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows


def driver_module(name: str, server: FakeServer, error_attr: str) -> types.ModuleType:
    """A module that looks like `psycopg` or `pymysql` to a connector: `connect` and the error class."""
    module = types.ModuleType(name)
    module.connect = server.connect  # type: ignore[attr-defined]
    setattr(module, error_attr, FakeError)
    module.cursors = types.SimpleNamespace(SSCursor=object)  # type: ignore[attr-defined]
    return module


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
