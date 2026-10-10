"""Plan J M107 (DEC-1317): reading outcomes and tables from a saved connection, read-only.

The query rule amends DEC-1105: a database is read through `SELECT * FROM <schema>.<table>` with, at
most, **one** `WHERE` - a date window on a column the request declared, the column quoted by the
dialect's own rule and the two dates written from `datetime.date` values, never from text a person
typed. Every other way in is refused before a statement is built. A file in a store (S3, Azure) is read
whole and the window is applied here, after reading; "the newest file under a folder" is the store's
own listing, newest last-modified first.

The database is `tests/unit/connections/fakes.py`'s SQLite-backed server: the SQL a connector builds
runs for real against real rows, every statement is recorded, and a `SELECT` before the session was
made read-only fails the test.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
import pytest
from moto import mock_aws
from pydantic import ValidationError

from engine.connections import mysql as mysql_module
from engine.connections import postgres as postgres_module
from engine.connections import sql as sql_module
from engine.connections.base import ConnectorError
from engine.connections.store import ConnectionStore
from engine.measurement.pull import (
    PULL_CODES,
    PULL_INVALID,
    PULL_NOTHING_FOUND,
    DateWindow,
    PullSelection,
    pull_frame,
)
from engine.settings import Settings
from engine.storage import LocalStorage
from tests.unit.connections.fakes import FakeServer, driver_module

NOW = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)
LIMIT = 10 * 1024 * 1024
DAYS = 90
ROWS = [(f"C{i:03d}", i % 2, (date(2026, 1, 1) + timedelta(days=i)).isoformat()) for i in range(DAYS)]
"""One row a day from 1 January to 31 March 2026: customer, converted, the day it was recorded."""
ODD_COLUMN = 'recorded" on'
HOSTILE = "recorded_on\" >= '1900-01-01' OR 1=1 --"
FEBRUARY = DateWindow(column="recorded_on", date_from=date(2026, 2, 1), date_to=date(2026, 2, 28))


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer(
        {
            "crm": {
                "outcomes": (["customer_id", "converted", "recorded_on"], ROWS),
                "odd": (["customer_id", ODD_COLUMN], [(r[0], r[2]) for r in ROWS]),
            }
        }
    )
    monkeypatch.setattr(postgres_module, "load_sdk", lambda _name: driver_module("psycopg", fake, "Error"))
    monkeypatch.setattr(mysql_module, "load_sdk", lambda _name: driver_module("pymysql", fake, "MySQLError"))
    monkeypatch.setattr(sql_module, "reach", lambda host, port: None)
    return fake


@pytest.fixture
def store(tmp_path: Path) -> ConnectionStore:
    return ConnectionStore(LocalStorage(tmp_path / "data"), Settings())


def _database(store: ConnectionStore, kind: str = "postgres") -> str:
    record = store.create(
        kind=kind,
        name="CRM",
        config={"host": "db.example.com", "database": "crm", "user": "reader"},
        secrets={"password": "s3cret-pw"},
    )
    return record.connection_id


def _window_statements(server: FakeServer) -> list[str]:
    """Statements that read rows with a condition (the catalogue's own listings are not row reads)."""
    return [sql for sql in server.executed if sql.startswith("SELECT * FROM") and " WHERE " in sql.upper()]


# --- a database: one quoted WHERE, on a read-only session ----------------------------------------------
def test_a_window_pull_returns_only_the_rows_inside_the_window_on_a_read_only_session(
    server: FakeServer, store: ConnectionStore
) -> None:
    server.read_only = False  # this session must make itself read-only before reading a row
    frame, record = pull_frame(
        store,
        _database(store),
        PullSelection(schema_name="crm", table="outcomes"),
        window=FEBRUARY,
        limit_bytes=LIMIT,
        now=NOW,
    )
    days = pd.to_datetime(frame["recorded_on"])
    assert len(frame) == 28
    assert days.min() == pd.Timestamp("2026-02-01") and days.max() == pd.Timestamp("2026-02-28")
    assert server.read_only
    window = _window_statements(server)
    assert window == [
        'SELECT * FROM "crm"."outcomes" WHERE "recorded_on" >= \'2026-02-01\' '
        "AND \"recorded_on\" < '2026-03-01' LIMIT 50000000"
    ]
    # The session was made read-only before the first row was read, and only the two allowed shapes ran.
    first_select = next(i for i, sql in enumerate(server.executed) if sql.startswith("SELECT * FROM"))
    assert "SET default_transaction_read_only = on" in server.executed[:first_select]
    reads = [sql for sql in server.executed if sql.startswith("SELECT * FROM")]
    assert reads == ['SELECT * FROM "crm"."outcomes" LIMIT 0', *window]
    assert record.filtered_by == "database"
    assert record.rows == 28 and record.window == FEBRUARY
    assert (record.schema_name, record.table, record.kind) == ("crm", "outcomes", "postgres")


def test_mysql_quotes_the_declared_column_with_its_own_rule(
    server: FakeServer, store: ConnectionStore
) -> None:
    frame, _record = pull_frame(
        store,
        _database(store, "mysql"),
        PullSelection(schema_name="crm", table="outcomes"),
        window=FEBRUARY,
        limit_bytes=LIMIT,
        now=NOW,
    )
    assert len(frame) == 28
    assert _window_statements(server) == [
        "SELECT * FROM `crm`.`outcomes` WHERE `recorded_on` >= '2026-02-01' "
        "AND `recorded_on` < '2026-03-01' LIMIT 50000000"
    ]


def test_a_column_name_with_a_quote_in_it_is_quoted_and_still_reads_its_window(
    server: FakeServer, store: ConnectionStore
) -> None:
    frame, _record = pull_frame(
        store,
        _database(store),
        PullSelection(schema_name="crm", table="odd"),
        window=DateWindow(column=ODD_COLUMN, date_from=date(2026, 3, 1), date_to=date(2026, 3, 31)),
        limit_bytes=LIMIT,
        now=NOW,
    )
    assert len(frame) == 31
    assert _window_statements(server) == [
        'SELECT * FROM "crm"."odd" WHERE "recorded"" on" >= \'2026-03-01\' '
        'AND "recorded"" on" < \'2026-04-01\' LIMIT 50000000'
    ]


def test_an_injected_column_never_reaches_a_where(server: FakeServer, store: ConnectionStore) -> None:
    connection = _database(store)
    with pytest.raises(ConnectorError) as caught:
        pull_frame(
            store,
            connection,
            PullSelection(schema_name="crm", table="outcomes"),
            window=DateWindow(column=HOSTILE, date_from=date(2026, 2, 1), date_to=date(2026, 2, 28)),
            limit_bytes=LIMIT,
            now=NOW,
        )
    assert caught.value.code == PULL_INVALID
    assert "1=1" not in caught.value.message  # a refusal names the problem, never echoes the text
    assert _window_statements(server) == []


def test_an_injected_table_or_schema_never_reaches_sql(server: FakeServer, store: ConnectionStore) -> None:
    connection = _database(store)
    for selection in (
        PullSelection(schema_name="crm", table="outcomes; DROP TABLE outcomes; --"),
        PullSelection(schema_name='crm" ; DROP SCHEMA crm; --', table="outcomes"),
    ):
        with pytest.raises(ConnectorError) as caught:
            pull_frame(store, connection, selection, window=FEBRUARY, limit_bytes=LIMIT, now=NOW)
        assert caught.value.code == "CONNECTION_OBJECT_NOT_FOUND"
    assert not any(sql.startswith("SELECT * FROM") for sql in server.executed)


def test_a_window_takes_dates_only_never_text() -> None:
    for bad in ("2026-02-01' OR '1'='1", "2026-02-01; DROP TABLE outcomes", "yesterday"):
        with pytest.raises(ValidationError):
            DateWindow.model_validate({"column": "recorded_on", "date_from": bad, "date_to": "2026-02-28"})
    with pytest.raises(ValidationError):
        DateWindow(column="recorded_on", date_from=date(2026, 3, 1), date_to=date(2026, 2, 1))
    with pytest.raises(ValidationError):
        DateWindow(column=" ", date_from=date(2026, 2, 1), date_to=date(2026, 2, 28))
    with pytest.raises(ValidationError):  # a moment is not a day: the window is whole days
        DateWindow.model_validate(
            {"column": "recorded_on", "date_from": "2026-02-01T10:00:00", "date_to": "2026-02-28"}
        )


def test_a_database_has_no_folders_and_a_table_needs_both_names(
    store: ConnectionStore, server: FakeServer
) -> None:
    connection = _database(store)
    for selection in (
        PullSelection(prefix="exports/"),
        PullSelection(path="exports/outcomes.csv"),
        PullSelection(table="outcomes"),
    ):
        with pytest.raises(ConnectorError) as caught:
            pull_frame(store, connection, selection, window=FEBRUARY, limit_bytes=LIMIT, now=NOW)
        assert caught.value.code == PULL_INVALID
    assert server.executed == []


def test_a_selection_names_one_thing() -> None:
    for fields in (
        {"path": "a.csv", "prefix": "exports/"},
        {"path": "a.csv", "table": "t", "schema_name": "s"},
        {"prefix": "exports/", "table": "t", "schema_name": "s"},
        {},
    ):
        with pytest.raises(ValidationError):
            PullSelection.model_validate(fields)


def test_a_whole_table_can_be_read_without_a_window(server: FakeServer, store: ConnectionStore) -> None:
    frame, record = pull_frame(
        store,
        _database(store),
        PullSelection(schema_name="crm", table="outcomes"),
        window=None,
        limit_bytes=LIMIT,
        now=NOW,
    )
    assert len(frame) == DAYS and record.window is None and record.filtered_by is None
    assert _window_statements(server) == []


def test_the_codes_are_plan_j_codes() -> None:
    assert {PULL_INVALID, PULL_NOTHING_FOUND} <= PULL_CODES


# --- a store: the newest file under a folder, the window applied after reading --------------------------
BUCKET = "acme-exports"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _csv(rows: list[tuple[str, int, str]]) -> bytes:
    return (
        pd.DataFrame(rows, columns=["customer_id", "converted", "recorded_on"]).to_csv(index=False).encode()
    )


def _bucket(store: ConnectionStore) -> str:
    record = store.create(
        kind="s3",
        name="Exports",
        config={"bucket": BUCKET, "region": "us-east-1"},
        secrets={"access_key_id": "testing", "secret_access_key": "testing"},
    )
    return record.connection_id


def test_a_store_file_is_read_whole_and_the_window_applied_here(s3: Any, store: ConnectionStore) -> None:
    s3.put_object(Bucket=BUCKET, Key="outcomes/q1.csv", Body=_csv(ROWS))
    frame, record = pull_frame(
        store,
        _bucket(store),
        PullSelection(path="outcomes/q1.csv"),
        window=FEBRUARY,
        limit_bytes=LIMIT,
        now=NOW,
    )
    assert len(frame) == 28 and set(frame["customer_id"]) == {r[0] for r in ROWS[31:59]}
    assert record.filtered_by == "marketing_ai" and record.path == "outcomes/q1.csv"


def test_the_newest_file_under_a_folder_is_the_one_read(s3: Any, store: ConnectionStore) -> None:
    s3.put_object(Bucket=BUCKET, Key="outcomes/z_older.csv", Body=_csv(ROWS[:10]))
    time.sleep(1.1)  # S3 keeps last-modified to the second
    s3.put_object(Bucket=BUCKET, Key="outcomes/a_newer.csv", Body=_csv(ROWS[:20]))
    s3.put_object(Bucket=BUCKET, Key="outcomes/notes.pdf", Body=b"%PDF")  # never a candidate
    frame, record = pull_frame(
        store, _bucket(store), PullSelection(prefix="outcomes/"), window=None, limit_bytes=LIMIT, now=NOW
    )
    assert record.path == "outcomes/a_newer.csv" and record.prefix == "outcomes/"
    assert len(frame) == 20


def test_an_empty_folder_says_so(s3: Any, store: ConnectionStore) -> None:
    s3.put_object(Bucket=BUCKET, Key="outcomes/readme.pdf", Body=b"%PDF")
    with pytest.raises(ConnectorError) as caught:
        pull_frame(
            store, _bucket(store), PullSelection(prefix="outcomes/"), window=None, limit_bytes=LIMIT, now=NOW
        )
    assert caught.value.code == PULL_NOTHING_FOUND and caught.value.status == 404


def test_rows_whose_day_cannot_be_read_are_left_out_and_counted(s3: Any, store: ConnectionStore) -> None:
    rows = [*ROWS[31:40], ("X001", 1, "not a day"), ("X002", 0, "")]
    s3.put_object(Bucket=BUCKET, Key="outcomes/feb.csv", Body=_csv(rows))
    frame, record = pull_frame(
        store,
        _bucket(store),
        PullSelection(path="outcomes/feb.csv"),
        window=FEBRUARY,
        limit_bytes=LIMIT,
        now=NOW,
    )
    assert len(frame) == 9 and record.unreadable_dates == 2


def test_a_store_file_without_the_declared_column_is_refused(s3: Any, store: ConnectionStore) -> None:
    s3.put_object(Bucket=BUCKET, Key="outcomes/q1.csv", Body=_csv(ROWS))
    with pytest.raises(ConnectorError) as caught:
        pull_frame(
            store,
            _bucket(store),
            PullSelection(path="outcomes/q1.csv"),
            window=DateWindow(column="booked_on", date_from=date(2026, 2, 1), date_to=date(2026, 2, 2)),
            limit_bytes=LIMIT,
            now=NOW,
        )
    assert caught.value.code == PULL_INVALID and "booked_on" in caught.value.message


def test_a_store_folder_outside_the_connection_root_is_refused(s3: Any, store: ConnectionStore) -> None:
    record = store.create(
        kind="s3",
        name="Exports",
        config={"bucket": BUCKET, "region": "us-east-1", "prefix": "outcomes/"},
        secrets={"access_key_id": "testing", "secret_access_key": "testing"},
    )
    s3.put_object(Bucket=BUCKET, Key="private/q1.csv", Body=_csv(ROWS))
    with pytest.raises(ConnectorError) as caught:
        pull_frame(
            store,
            record.connection_id,
            PullSelection(prefix="private/"),
            window=None,
            limit_bytes=LIMIT,
            now=NOW,
        )
    assert caught.value.code == "CONNECTION_OUTSIDE_FOLDER"


def test_nothing_is_written_to_the_bucket(s3: Any, store: ConnectionStore) -> None:
    s3.put_object(Bucket=BUCKET, Key="outcomes/q1.csv", Body=_csv(ROWS))
    before = {o["Key"]: o["ETag"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    pull_frame(
        store, _bucket(store), PullSelection(prefix="outcomes/"), window=FEBRUARY, limit_bytes=LIMIT, now=NOW
    )
    after = {o["Key"]: o["ETag"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert after == before


def test_a_pull_is_bounded_by_the_size_limit(s3: Any, store: ConnectionStore) -> None:
    s3.put_object(Bucket=BUCKET, Key="outcomes/q1.csv", Body=_csv(ROWS))
    with pytest.raises(ConnectorError) as caught:
        pull_frame(
            store,
            _bucket(store),
            PullSelection(path="outcomes/q1.csv"),
            window=None,
            limit_bytes=100,
            now=NOW,
        )
    assert caught.value.code == "CONNECTION_TOO_LARGE"


def test_a_window_over_200k_rows_of_a_store_file_is_read_in_linear_time(
    s3: Any, store: ConnectionStore
) -> None:
    """Read once, filtered by one vectorised comparison: no loop over rows (PLAN_J_DELEGATION §4.7, item 3).

    About 0.6 s here for 200,000 rows read from the bucket and filtered; 1,000,000 rows take about 3 s
    (the filter alone is about 0.25 s at 1,000,000).
    """
    rows = 200_000
    days = pd.date_range("2026-01-01", periods=90).strftime("%Y-%m-%d").to_numpy()
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i:07d}" for i in range(rows)],
            "converted": [i % 2 for i in range(rows)],
            "recorded_on": days[[i % 90 for i in range(rows)]],
        }
    )
    s3.put_object(Bucket=BUCKET, Key="outcomes/big.csv", Body=frame.to_csv(index=False).encode())
    started = time.perf_counter()
    kept, record = pull_frame(
        store,
        _bucket(store),
        PullSelection(path="outcomes/big.csv"),
        window=FEBRUARY,
        limit_bytes=LIMIT,
        now=NOW,
    )
    elapsed = time.perf_counter() - started
    assert record.rows == len(kept.index) == int((frame["recorded_on"].str[5:7] == "02").sum())
    assert elapsed < 10.0, f"{elapsed:.1f} s for {rows:,} rows"


def test_every_refusal_is_in_plain_words(s3: Any, store: ConnectionStore, server: FakeServer) -> None:
    """Each message a person reads passes `jargon_in` (Plan J's rule for every message shown)."""
    from engine.pilot.plain import jargon_in

    database, bucket = _database(store), _bucket(store)
    s3.put_object(Bucket=BUCKET, Key="outcomes/q1.csv", Body=_csv(ROWS))
    unknown = DateWindow(column="nope", date_from=date(2026, 2, 1), date_to=date(2026, 2, 2))
    attempts = [
        (database, PullSelection(prefix="exports/"), FEBRUARY),
        (database, PullSelection(table="outcomes"), FEBRUARY),
        (database, PullSelection(schema_name="crm", table="outcomes"), unknown),
        (bucket, PullSelection(schema_name="crm", table="outcomes"), None),
        (bucket, PullSelection(prefix="empty/"), None),
        (bucket, PullSelection(path="outcomes/q1.csv"), unknown),
    ]
    for connection, selection, window in attempts:
        with pytest.raises(ConnectorError) as caught:
            pull_frame(store, connection, selection, window=window, limit_bytes=LIMIT, now=NOW)
        words = f"{caught.value.message} {caught.value.fix or ''}"
        assert jargon_in(words) == (), (caught.value.code, words)
