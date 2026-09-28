"""PostgreSQL / Redshift and MySQL connectors (Plan H M80) against a SQLite-backed fake server.

What is pinned here: the one query each dialect builds, with its identifiers quoted by the dialect's
own rule and never pasted; that a name must appear in the database's listing before it is ever put
into SQL; that the session is made read-only before any row is read; the named test steps and their
plain fixes; and the bounded, masked preview and fetch. A real PostgreSQL server, when there is one,
is exercised in `test_postgres_live.py`.
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd
import pytest

from engine.connections import mysql as mysql_module
from engine.connections import postgres as postgres_module
from engine.connections import sql as sql_module
from engine.connections.base import ConnectorError, Selection, StepName, StepStatus
from engine.connections.mysql import MySqlConnector, mysql_select, quote_identifier
from engine.connections.postgres import PostgresConnector, postgres_select
from engine.connections.snowflake import quote_identifier as snowflake_quote
from engine.connections.snowflake import snowflake_select
from tests.unit.connections.fakes import FakeError, FakeServer, driver_module

ROWS = [(i, f"user{i}@example.com", i * 10) for i in range(1, 31)]
WEIRD = 'odd "name"; DROP TABLE customers; --'
PG = {"host": "db.example.com", "database": "crm", "user": "reader"}
SECRET = {"password": "s3cret-pw"}


def schemas() -> dict[str, Any]:
    return {
        "sales": {
            "customers": (["id", "email", "spend"], ROWS),
            WEIRD: (["id"], [(1,)]),
        },
        "empty": {},
    }


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer(schemas())
    monkeypatch.setattr(postgres_module, "load_sdk", lambda _name: driver_module("psycopg", fake, "Error"))
    monkeypatch.setattr(mysql_module, "load_sdk", lambda _name: driver_module("pymysql", fake, "MySQLError"))
    monkeypatch.setattr(sql_module, "reach", lambda host, port: None)
    return fake


# --- the SQL each dialect builds ---------------------------------------------------------------------
def test_postgres_quotes_both_names_with_psycopg_identifiers() -> None:
    assert (
        postgres_select("sales", "customers", 20).as_string(None)
        == 'SELECT * FROM "sales"."customers" LIMIT 20'
    )
    injected = postgres_select('s"x', "t; DROP TABLE users; --", 5).as_string(None)
    assert injected == 'SELECT * FROM "s""x"."t; DROP TABLE users; --" LIMIT 5'


def test_the_limit_is_always_an_integer() -> None:
    with pytest.raises(ValueError):
        postgres_select("s", "t", "1; DROP TABLE t")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mysql_select("s", "t", "1; DROP TABLE t")  # type: ignore[arg-type]


def test_mysql_quotes_with_doubled_backticks_and_refuses_impossible_names() -> None:
    assert quote_identifier("customers") == "`customers`"
    assert quote_identifier("a`b") == "`a``b`"
    assert (
        mysql_select("shop", "x`; DROP TABLE t; --", 20)
        == "SELECT * FROM `shop`.`x``; DROP TABLE t; --` LIMIT 20"
    )
    for bad in ("", "a" * 65, "nul\x00byte", "trailing "):
        with pytest.raises(ConnectorError):
            quote_identifier(bad)


def test_snowflake_quotes_with_doubled_double_quotes() -> None:
    assert snowflake_quote('A"B') == '"A""B"'
    assert snowflake_select("PUBLIC", "Orders", 3) == 'SELECT * FROM "PUBLIC"."Orders" LIMIT 3'


# --- PostgreSQL -------------------------------------------------------------------------------------
def test_postgres_passes_every_step_on_a_read_only_user(server: FakeServer) -> None:
    report = PostgresConnector().test(PG, SECRET)
    assert report.ok, report
    assert [(s.name, s.status) for s in report.steps] == [
        (StepName.REACH, StepStatus.OK),
        (StepName.SIGN_IN, StepStatus.OK),
        (StepName.LIST, StepStatus.OK),
        (StepName.READ_SAMPLE, StepStatus.OK),
        (StepName.READ_ONLY_CHECK, StepStatus.OK),
    ]
    assert server.executed[0] == "SET default_transaction_read_only = on"
    connect = server.connects[0]
    assert connect["connect_timeout"] <= 10 and connect["password"] == "s3cret-pw"
    assert "statement_timeout" in connect["options"]
    assert 'SELECT * FROM "sales"."' in "\n".join(server.executed)


def test_postgres_warns_when_the_user_could_write(server: FakeServer) -> None:
    server.write_privileges = 3
    check = PostgresConnector().test(PG, SECRET).steps[-1]
    assert check.status is StepStatus.WARNING and "only read" in (check.fix or "")
    server.write_privileges, server.superuser = 0, True
    assert PostgresConnector().test(PG, SECRET).steps[-1].status is StepStatus.WARNING


def test_a_refused_password_is_a_sign_in_failure_that_never_repeats_it(server: FakeServer) -> None:
    server.refuse_sign_in = FakeError(
        'FATAL: password authentication failed for user "reader" password=s3cret-pw'
    )
    report = PostgresConnector().test(PG, SECRET)
    sign_in = report.steps[1]
    assert sign_in.status is StepStatus.FAILED
    assert sign_in.message == "The user name or password was refused (user reader)."
    assert "s3cret-pw" not in report.model_dump_json()
    assert [s.status for s in report.steps[2:]] == [StepStatus.SKIPPED] * 3


def test_an_unreachable_host_fails_the_first_step(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(host: str, port: int) -> None:
        raise ConnectorError(
            "CONNECTION_UNREACHABLE", f"{host} refused a connection on port {port}.", "Check the port."
        )

    monkeypatch.setattr(sql_module, "reach", refuse)
    report = PostgresConnector(redshift=True).test(PG, SECRET)
    assert report.steps[0].status is StepStatus.FAILED and "5439" in report.steps[0].message
    assert server.connects == []


def test_browse_lists_schemas_then_tables(server: FakeServer) -> None:
    top = PostgresConnector().browse(PG, SECRET, "")
    assert sorted((i.name, i.kind) for i in top.items) == [("empty", "schema"), ("sales", "schema")]
    inner = PostgresConnector().browse(PG, SECRET, "sales")
    assert inner.parent == ""
    assert {(i.schema_name, i.table, i.importable) for i in inner.items} == {
        ("sales", "customers", True),
        ("sales", WEIRD, True),
    }
    only = PostgresConnector().browse({**PG, "schema": "sales"}, SECRET, "")
    assert only.path == "sales" and only.parent is None
    with pytest.raises(ConnectorError) as caught:
        PostgresConnector().browse(PG, SECRET, "secret_schema")
    assert caught.value.status == 404


def test_preview_is_twenty_masked_rows_read_through_the_one_query(server: FakeServer) -> None:
    preview = PostgresConnector().preview(PG, SECRET, Selection(schema_name="sales", table="customers"))
    assert preview.columns == ("id", "email", "spend")
    assert len(preview.rows) == 20
    assert preview.rows[0] == ("1", "[REDACTED:email]", "10")
    assert server.executed[-1] == 'SELECT * FROM "sales"."customers" LIMIT 20'
    assert server.read_only


def test_a_table_with_a_hostile_name_is_quoted_not_executed(server: FakeServer) -> None:
    preview = PostgresConnector().preview(PG, SECRET, Selection(schema_name="sales", table=WEIRD))
    assert preview.rows == (("1",),)
    assert server.executed[-1] == 'SELECT * FROM "sales"."odd ""name""; DROP TABLE customers; --" LIMIT 20'
    assert PostgresConnector().preview(PG, SECRET, Selection(schema_name="sales", table="customers")).rows


def test_a_name_the_database_did_not_list_never_reaches_sql(server: FakeServer) -> None:
    for selection in (
        Selection(schema_name="sales", table="customers; DROP TABLE customers"),
        Selection(schema_name="nope", table="customers"),
        Selection(schema_name="sales"),
    ):
        with pytest.raises(ConnectorError):
            PostgresConnector().preview(PG, SECRET, selection)
    assert not any(s.startswith("SELECT * FROM") for s in server.executed)


def test_fetch_streams_the_whole_table_as_csv_within_the_limit(server: FakeServer) -> None:
    sink = io.BytesIO()
    result = PostgresConnector().fetch(
        PG, SECRET, Selection(schema_name="sales", table="customers"), sink, limit_bytes=10**6, max_rows=1000
    )
    frame = pd.read_csv(io.BytesIO(sink.getvalue()))
    assert result.rows == 30 and len(frame) == 30 and list(frame.columns) == ["id", "email", "spend"]
    assert result.file_format == "csv" and result.file_name == "sales.customers.csv"
    assert server.executed[-1] == 'SELECT * FROM "sales"."customers" LIMIT 1000'
    with pytest.raises(ConnectorError) as caught:
        PostgresConnector().fetch(
            PG,
            SECRET,
            Selection(schema_name="sales", table="customers"),
            io.BytesIO(),
            limit_bytes=100,
            max_rows=1000,
        )
    assert caught.value.code == "CONNECTION_TOO_LARGE"


def test_an_empty_table_imports_as_its_header(server: FakeServer) -> None:
    fake = FakeServer({"sales": {"none": (["a", "b"], [])}})
    server.db, server.schemas = fake.db, fake.schemas
    sink = io.BytesIO()
    result = PostgresConnector().fetch(
        PG, SECRET, Selection(schema_name="sales", table="none"), sink, limit_bytes=10**6, max_rows=10
    )
    assert sink.getvalue() == b"a,b\n" and result.rows == 0


# --- MySQL ------------------------------------------------------------------------------------------
MY = {"host": "db.example.com", "database": "sales", "user": "reader"}


def test_mysql_is_made_read_only_and_browses_its_one_database(server: FakeServer) -> None:
    report = MySqlConnector().test(MY, SECRET)
    assert report.ok and report.steps[-1].status is StepStatus.OK
    assert server.executed[0] == "SET SESSION TRANSACTION READ ONLY"
    connect = server.connects[0]
    assert connect["connect_timeout"] <= 10 and connect["read_timeout"] <= 60
    listing = MySqlConnector().browse(MY, SECRET, "")
    assert listing.path == "sales" and {i.table for i in listing.items} == {"customers", WEIRD}
    preview = MySqlConnector().preview(MY, SECRET, Selection(schema_name="sales", table="customers"))
    assert len(preview.rows) == 20 and server.executed[-1] == "SELECT * FROM `sales`.`customers` LIMIT 20"


def test_mysql_grants_decide_the_read_only_check(server: FakeServer) -> None:
    server.grants = ["GRANT SELECT, INSERT ON `sales`.* TO `reader`@`%`"]
    assert MySqlConnector().test(MY, SECRET).steps[-1].status is StepStatus.WARNING
    server.grants = ["GRANT ALL PRIVILEGES ON *.* TO `root`@`%`"]
    assert MySqlConnector().test(MY, SECRET).steps[-1].status is StepStatus.WARNING


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1045, "CONNECTION_SIGN_IN_FAILED"),
        (1049, "CONNECTION_DATABASE_NOT_FOUND"),
        (2003, "CONNECTION_UNREACHABLE"),
    ],
)
def test_mysql_error_codes_become_plain_words(server: FakeServer, code: int, expected: str) -> None:
    server.refuse_sign_in = FakeError(code, "driver text with password=s3cret-pw")
    with pytest.raises(ConnectorError) as caught:
        MySqlConnector().connect(MY, SECRET)
    assert caught.value.code == expected and "s3cret" not in caught.value.message


def test_the_forms_ask_for_little_and_hide_the_rest() -> None:
    for connector in (PostgresConnector(), PostgresConnector(redshift=True), MySqlConnector()):
        info = connector.info()
        visible = [f.name for f in info.fields if not f.advanced]
        assert visible == ["host", "database", "user", "password"]
        assert [f.name for f in info.fields if f.secret] == ["password"]
        assert info.available
    redshift = {f.name: f for f in PostgresConnector(redshift=True).info().fields}
    assert redshift["port"].default == 5439 and redshift["sslmode"].default == "require"
