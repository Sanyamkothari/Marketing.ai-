"""Plan J M107 (DEC-1317): a campaign's outcomes and a client's consent read from a saved connection.

`POST /campaigns/{id}/outcomes` takes either an upload (as before) or `{connection_id, selection,
date_column, date_from, date_to}`: the rows inside the window are read from the client's database on a
read-only session, stored as an ordinary upload with a `pull_source.json` beside it saying where they came
from, and then read exactly as an uploaded file would be. `POST /privacy/consent/imports/from-connection`
reads a consent table or file the same way and imports it all or nothing, as the file route does.

The database is the SQLite-backed fake of `tests/unit/connections/fakes.py`, reached through the real
PostgreSQL connector. The outcomes table holds a decoy row for every customer dated before the campaign,
with the opposite outcome: a pull that ignored the window would double every customer and fail.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.connections import postgres as postgres_module
from engine.connections import sql as sql_module
from engine.measurement.pull import PULL_SOURCE_FILENAME
from engine.settings import Settings
from engine.storage import LocalStorage, upload_key
from tests.integration.measurement.support import MATURE, SENT, TARGET, ok, propensity_run, upload
from tests.unit.connections.fakes import FakeServer, driver_module

pytestmark = pytest.mark.integration

RUN = "r_20260501_0b0b0b01"
ROWS = 4_000
PRIVACY_SALT = "m107-privacy-salt-0123456789abcdef"


def _outcome_rows(outcomes: Any) -> list[tuple[Any, ...]]:
    """Each customer's outcome dated inside the window, and a decoy with the opposite outcome before it."""
    inside = (SENT + timedelta(days=30)).date().isoformat()
    before = (SENT - timedelta(days=30)).date().isoformat()
    rows: list[tuple[Any, ...]] = []
    for key, value in zip(outcomes["customer_id"], outcomes[TARGET], strict=True):
        rows.append((str(key), int(value), inside))
        rows.append((str(key), 1 - int(value), before))
    return rows


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer({"crm": {"placeholder": (["id"], [(1,)])}})
    monkeypatch.setattr(postgres_module, "load_sdk", lambda _name: driver_module("psycopg", fake, "Error"))
    monkeypatch.setattr(sql_module, "reach", lambda host, port: None)
    return fake


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def client(config_root: Path, data_dir: Path, server: FakeServer) -> Iterator[TestClient]:
    app = create_app(config_root=config_root, data_dir=data_dir)
    app.state.settings = Settings(data_dir=data_dir, privacy_salt=PRIVACY_SALT)  # type: ignore[call-arg]
    with TestClient(app) as test_client:
        yield test_client


def _connection(client: TestClient) -> str:
    body = ok(
        client.post(
            "/connections",
            json={
                "kind": "postgres",
                "name": "CRM",
                "config": {"host": "db.example.com", "database": "crm", "user": "reader"},
                "secrets": {"password": "s3cret-pw"},
            },
        ),
        201,
    )
    return str(body["connection_id"])


def _campaign(client: TestClient, data_dir: Path, server: FakeServer) -> str:
    scored = propensity_run(LocalStorage(data_dir), RUN, rows=ROWS)
    server.db.execute('CREATE TABLE "crm"."outcomes" ("customer_id", "reactivated_90d", "recorded_on")')
    server.db.executemany('INSERT INTO "crm"."outcomes" VALUES (?, ?, ?)', _outcome_rows(scored.outcomes))
    created = ok(client.post("/campaigns", json={"run_id": RUN}), 201)
    return str(created["campaign"]["campaign_id"])


def _pull_body(connection_id: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "connection_id": connection_id,
        "selection": {"schema_name": "crm", "table": "outcomes"},
        "date_column": "recorded_on",
        "date_from": SENT.date().isoformat(),
        "date_to": (SENT + timedelta(days=90)).date().isoformat(),
        "outcome_column": TARGET,
    }
    body.update(changes)
    return body


def test_outcomes_pulled_for_the_window_are_measured_like_an_uploaded_file(
    client: TestClient, data_dir: Path, server: FakeServer
) -> None:
    campaign_id = _campaign(client, data_dir, server)
    connection_id = _connection(client)
    view = ok(client.post(f"/campaigns/{campaign_id}/outcomes", json=_pull_body(connection_id)))
    outcomes = view["campaign"]["outcomes"]
    assert outcomes["rows"] == ROWS, "only the rows inside the window: no decoy, no repeated customer"
    assert outcomes["outcome_column"] == TARGET and outcomes["outcome_named"] is True
    # The rows are an ordinary upload, with where they came from beside it.
    storage = LocalStorage(data_dir)
    source = json.loads(storage.read_bytes(upload_key(outcomes["upload_id"], PULL_SOURCE_FILENAME)))
    assert source["connection_id"] == connection_id and source["filtered_by"] == "database"
    assert source["window"] == {"column": "recorded_on", "date_from": "2026-05-01", "date_to": "2026-07-30"}
    assert source["rows"] == ROWS and "s3cret-pw" not in json.dumps(source)
    assert ok(client.get(f"/uploads/{outcomes['upload_id']}/profile"))["row_count"] == ROWS
    window = [sql for sql in server.executed if sql.startswith("SELECT * FROM") and " WHERE " in sql]
    assert window == [
        'SELECT * FROM "crm"."outcomes" WHERE "recorded_on" >= \'2026-05-01\' '
        "AND \"recorded_on\" < '2026-07-31' LIMIT 50000000"
    ]
    measured = ok(client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat()}))
    assert measured["report"]["treated_rows"] + measured["report"]["control_rows"] > 0


def test_the_same_outcomes_uploaded_as_a_file_give_the_same_report(
    client: TestClient, data_dir: Path, server: FakeServer, tmp_path: Path, config_root: Path
) -> None:
    campaign_id = _campaign(client, data_dir, server)
    pulled = ok(client.post(f"/campaigns/{campaign_id}/outcomes", json=_pull_body(_connection(client))))
    by_pull = ok(client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat()}))

    other = tmp_path / "other"
    other.mkdir()
    scored = propensity_run(LocalStorage(other), RUN, rows=ROWS)
    with TestClient(create_app(config_root=config_root, data_dir=other)) as second:
        created = ok(second.post("/campaigns", json={"run_id": RUN}), 201)["campaign"]["campaign_id"]
        frame = scored.outcomes[["customer_id", TARGET]]
        ok(
            second.post(
                f"/campaigns/{created}/outcomes",
                json={"upload_id": upload(second, frame), "outcome_column": TARGET},
            )
        )
        by_file = ok(second.post(f"/campaigns/{created}/measure", json={"as_of": MATURE.isoformat()}))
    assert pulled["campaign"]["outcomes"]["rows"] == len(frame.index)
    for field in ("treated_rows", "control_rows", "treated_conversions", "control_conversions", "lift"):
        assert by_pull["report"][field] == by_file["report"][field], field


def test_an_outcomes_request_names_one_source(client: TestClient, data_dir: Path, server: FakeServer) -> None:
    campaign_id = _campaign(client, data_dir, server)
    connection_id = _connection(client)
    both = client.post(
        f"/campaigns/{campaign_id}/outcomes", json={**_pull_body(connection_id), "upload_id": "u_x"}
    )
    assert both.status_code == 422, both.text
    neither = client.post(f"/campaigns/{campaign_id}/outcomes", json={"outcome_column": TARGET})
    assert neither.status_code == 422, neither.text
    no_window = _pull_body(connection_id)
    del no_window["date_column"]
    assert client.post(f"/campaigns/{campaign_id}/outcomes", json=no_window).status_code == 422
    backwards = _pull_body(connection_id, date_from="2026-08-01", date_to="2026-05-01")
    assert client.post(f"/campaigns/{campaign_id}/outcomes", json=backwards).status_code == 422
    assert ok(client.get(f"/campaigns/{campaign_id}"))["campaign"]["outcomes"] is None
    assert not any(sql.startswith("SELECT * FROM") and " WHERE " in sql for sql in server.executed)


def test_free_sql_or_an_injected_name_is_refused_before_any_row_is_read(
    client: TestClient, data_dir: Path, server: FakeServer
) -> None:
    campaign_id = _campaign(client, data_dir, server)
    connection_id = _connection(client)
    injected = client.post(
        f"/campaigns/{campaign_id}/outcomes",
        json=_pull_body(connection_id, date_column="recorded_on\" > '1900-01-01' OR 1=1 --"),
    )
    assert injected.status_code == 422 and injected.json()["detail"]["code"] == "PULL_INVALID"
    free_sql = client.post(
        f"/campaigns/{campaign_id}/outcomes",
        json={**_pull_body(connection_id), "query": "SELECT * FROM crm.outcomes"},
    )
    assert free_sql.status_code == 422
    table = client.post(
        f"/campaigns/{campaign_id}/outcomes",
        json=_pull_body(connection_id, selection={"schema_name": "crm", "table": "outcomes; DROP TABLE x"}),
    )
    assert table.status_code == 404 and table.json()["detail"]["code"] == "CONNECTION_OBJECT_NOT_FOUND"
    assert not any(sql.startswith("SELECT * FROM") and " WHERE " in sql for sql in server.executed)


# --- consent ---------------------------------------------------------------------------------------------
CONSENT_COLUMNS = ["principal_id", "purpose", "status", "recorded_at", "source"]


def _consent_rows(count: int) -> list[tuple[str, ...]]:
    return [
        (f"C-{index:05d}", "marketing_communication", "granted", "2026-05-01T09:00:00+00:00", "crm")
        for index in range(count)
    ]


def test_a_consent_table_is_imported_from_a_connection(client: TestClient, server: FakeServer) -> None:
    server.db.execute(f'CREATE TABLE "crm"."consent" ({", ".join(CONSENT_COLUMNS)})')
    server.db.executemany('INSERT INTO "crm"."consent" VALUES (?, ?, ?, ?, ?)', _consent_rows(25))
    connection_id = _connection(client)
    imported = ok(
        client.post(
            "/privacy/consent/imports/from-connection",
            json={
                "connection_id": connection_id,
                "selection": {"schema_name": "crm", "table": "consent"},
                "client_id": "acme",
            },
        ),
        201,
    )
    assert imported["rows_imported"] == 25 and imported["imported"] is True
    assert server.read_only
    assert [sql for sql in server.executed if sql.startswith("SELECT * FROM")][-1] == (
        'SELECT * FROM "crm"."consent" LIMIT 50000000'
    )


def test_a_consent_table_with_a_bad_row_imports_nothing(client: TestClient, server: FakeServer) -> None:
    rows = [
        *_consent_rows(5),
        ("C-99999", "marketing_communication", "maybe", "2026-05-01T09:00:00+00:00", "crm"),
    ]
    server.db.execute(f'CREATE TABLE "crm"."consent" ({", ".join(CONSENT_COLUMNS)})')
    server.db.executemany('INSERT INTO "crm"."consent" VALUES (?, ?, ?, ?, ?)', rows)
    refused = client.post(
        "/privacy/consent/imports/from-connection",
        json={
            "connection_id": _connection(client),
            "selection": {"schema_name": "crm", "table": "consent"},
            "client_id": "acme",
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["rows_imported"] == 0 and refused.json()["imported"] is False
    assert "C-99999" not in refused.text


def test_the_consent_file_route_is_unchanged(client: TestClient) -> None:
    operation = client.app.openapi()["paths"]["/privacy/consent/imports"]["post"]  # type: ignore[attr-defined]
    assert set(operation["requestBody"]["content"]) == {"multipart/form-data"}
