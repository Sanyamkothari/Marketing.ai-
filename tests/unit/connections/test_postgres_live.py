"""The PostgreSQL connector against a real server (`make postgres-up`); skips with its reason without one.

It proves what a fake cannot: that psycopg really opens the session read-only (a write through the
connector's own connection is refused by the server) and that the one query runs on a real catalogue.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from engine.connections.base import Selection, StepStatus
from engine.connections.postgres import PostgresConnector
from tests.fixtures.postgres import postgres_url, skip_without_postgres

pytestmark = pytest.mark.postgres


@pytest.fixture
def live() -> Iterator[tuple[dict[str, object], dict[str, str], str]]:
    skip_without_postgres()
    url = make_url(postgres_url())
    schema = f"mai_conn_{uuid.uuid4().hex[:8]}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'CREATE TABLE "{schema}".people (id int, email text)'))
        connection.execute(
            text(f"INSERT INTO \"{schema}\".people VALUES (1, 'a@example.com'), (2, 'b@example.com')")
        )
    config: dict[str, object] = {
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "database": url.database or "",
        "user": url.username or "",
        "schema": schema,
        "sslmode": "disable",
    }
    try:
        yield config, {"password": url.password or ""}, schema
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_a_real_server_passes_and_the_session_refuses_writes(
    live: tuple[dict[str, object], dict[str, str], str],
) -> None:
    config, secrets, schema = live
    connector = PostgresConnector()
    report = connector.test(config, secrets)  # type: ignore[arg-type]
    assert report.ok, report
    assert report.steps[3].status is StepStatus.OK
    preview = connector.preview(config, secrets, Selection(schema_name=schema, table="people"))  # type: ignore[arg-type]
    assert preview.rows == (("1", "[REDACTED:email]"), ("2", "[REDACTED:email]"))
    sink = io.BytesIO()
    connector.fetch(config, secrets, Selection(schema_name=schema, table="people"), sink, limit_bytes=10**6, max_rows=10)  # type: ignore[arg-type]
    assert sink.getvalue().splitlines()[0] == b"id,email"
    connection = connector.connect(config, secrets)  # type: ignore[arg-type]
    try:
        with pytest.raises(Exception, match="read-only"), connection.cursor() as cursor:
            cursor.execute(f'INSERT INTO "{schema}".people VALUES (3, NULL)')
    finally:
        connection.close()
