"""`alembic upgrade head --sql` with a schema: the script creates the schema and points at it first.

The M50 checklist reviews the DDL before it is applied to the dev account, and it does that with
Alembic's offline mode. Walking that step found the script unusable with the deployment's schema
(`postgres_schema=marketing_ai`, written by `infra/compute.py`): it qualified `alembic_version` with
a schema nothing had created, and left every other table to the `search_path` of whoever ran it -
`public` on a fresh database, which is not where the application looks first. Online migrations
never had the problem, because `postgres_engine` sets the path on the connection and `ensure_schema`
creates it. These tests pin the offline half to the same shape, without a database.
"""

from __future__ import annotations

import io
from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

REPO = Path(__file__).resolve().parents[2]
URL = "postgresql://app:secret@db.invalid:5439/marketing"


def _offline_sql(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> str:
    monkeypatch.chdir(REPO)
    buffer = io.StringIO()
    config = Config(str(REPO / "alembic.ini"), cmd_opts=Namespace(x=list(arguments)), output_buffer=buffer)
    command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def test_the_schema_is_created_and_selected_before_the_first_table(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = _offline_sql(monkeypatch, f"url={URL}", "schema=marketing_ai")
    create_schema = sql.index('CREATE SCHEMA IF NOT EXISTS "marketing_ai"')
    search_path = sql.index('SET search_path TO "marketing_ai", public')
    first_table = sql.index("CREATE TABLE")
    assert create_schema < search_path < first_table


def test_without_a_schema_the_script_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = _offline_sql(monkeypatch, f"url={URL}")
    assert "CREATE SCHEMA" not in sql
    assert "search_path" not in sql
    assert "CREATE TABLE alembic_version" in sql


def test_a_schema_name_is_quoted_not_interpolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`-x schema=` is whatever was typed; an embedded quote is doubled, as SQL quotes an identifier."""
    sql = _offline_sql(monkeypatch, f"url={URL}", 'schema=odd"name')
    assert 'CREATE SCHEMA IF NOT EXISTS "odd""name"' in sql
