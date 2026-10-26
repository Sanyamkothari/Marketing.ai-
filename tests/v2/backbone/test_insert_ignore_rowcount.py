"""`insert_ignore` counts new rows even where a closed cursor forgets its row count.

psycopg 3 reports -1 for `cursor.rowcount` once the cursor is closed, and SQLAlchemy closes the
cursor of an INSERT without RETURNING before the caller reads `CursorResult.rowcount`. SQLite
keeps the number, so a SQLite run alone cannot catch that. This test gives SQLite psycopg 3's
behaviour by wrapping the DBAPI cursor, which needs no Postgres server.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.engine import Engine

from autonomy.store.sql import insert_ignore

metadata = MetaData()
things = Table("things", metadata, Column("id", Integer, primary_key=True), Column("name", String))


class _ForgetfulCursor:
    """A cursor whose `rowcount` is -1 after `close()`, as psycopg 3's is."""

    def __init__(self, inner: sqlite3.Cursor) -> None:
        self._inner = inner
        self._closed = False

    @property
    def rowcount(self) -> int:
        return -1 if self._closed else self._inner.rowcount

    def close(self) -> None:
        self._closed = True
        self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _Connection:
    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def cursor(self, *args: Any, **kwargs: Any) -> _ForgetfulCursor:
        return _ForgetfulCursor(self._inner.cursor(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _forgetful_engine(path: Path) -> Engine:
    def connect() -> Any:
        return _Connection(sqlite3.connect(path, check_same_thread=False))

    engine = create_engine("sqlite://", creator=connect)
    metadata.create_all(engine)
    return engine


def test_the_wrapper_really_loses_the_count_of_a_plain_insert(tmp_path: Path) -> None:
    """Guards the test itself: without `preserve_rowcount` this wrapper reads -1."""
    engine = _forgetful_engine(tmp_path / "plain.db")
    with engine.begin() as conn:
        assert conn.execute(things.insert().values([{"id": 1, "name": "a"}])).rowcount == -1
    engine.dispose()


def test_insert_ignore_counts_only_the_new_rows(tmp_path: Path) -> None:
    engine = _forgetful_engine(tmp_path / "ignore.db")
    rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 1, "name": "c"}]
    with engine.begin() as conn:
        assert insert_ignore(conn, things, rows) == 2
        assert insert_ignore(conn, things, [{"id": 2, "name": "z"}, {"id": 3, "name": "d"}]) == 1
        assert insert_ignore(conn, things, [{"id": 3, "name": "z"}]) == 0
        assert insert_ignore(conn, things, []) == 0
        assert dict(conn.execute(select(things.c.id, things.c.name)).tuples().all()) == {
            1: "a",
            2: "b",
            3: "d",
        }
    engine.dispose()
