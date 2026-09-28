"""The optional connectors (Snowflake, BigQuery, Azure Blob) with fake SDK modules injected (Plan H M80).

Their SDKs are not installed here - that is the point of an optional add-on - so each is exercised
through a small fake module shaped like the part of the SDK the connector calls, and the "not
installed" path is exercised for real: the card is listed, says it needs the add-on, and every call
refuses with the install command.
"""

from __future__ import annotations

import io
import json
import types
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from engine.connections import azure_blob as azure_module
from engine.connections import bigquery as bigquery_module
from engine.connections import snowflake as snowflake_module
from engine.connections.azure_blob import AzureBlobConnector, sas_permissions
from engine.connections.base import ConnectorError, Selection, StepName, StepStatus
from engine.connections.bigquery import BigQueryConnector
from engine.connections.registry import CONNECTORS
from engine.connections.snowflake import SnowflakeConnector
from tests.unit.connections.fakes import FakeError, FakeServer


# --- not installed ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["snowflake", "bigquery", "azure_blob"])
def test_a_missing_add_on_is_listed_and_says_how_to_install_it(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = CONNECTORS[kind]
    module = {"snowflake": snowflake_module, "bigquery": bigquery_module, "azure_blob": azure_module}[kind]
    monkeypatch.setattr(module, "load_sdk", lambda _name: None)
    info = connector.info()
    assert info.tier == "optional" and not info.available and info.addon
    with pytest.raises(ConnectorError) as caught:
        connector.browse({}, {}, "")
    assert caught.value.code == "CONNECTION_NEEDS_ADDON"
    assert f"marketing-ai[{info.addon}]" in (caught.value.fix or "")


# --- Snowflake --------------------------------------------------------------------------------------
@pytest.fixture
def snowflake(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    server = FakeServer(
        {"PUBLIC": {"ORDERS": (["ID", "EMAIL"], [(1, "x@example.com"), (2, "y@example.com")])}}
    )
    server.read_only = True  # Snowflake has no read-only switch; only SELECTs are ever sent
    module = types.ModuleType("snowflake.connector")
    module.connect = server.connect  # type: ignore[attr-defined]
    monkeypatch.setattr(snowflake_module, "load_sdk", lambda _name: module)
    return server


SF = {"account": "myorg-acct", "warehouse": "WH", "database": "DB", "user": "reader"}


def test_snowflake_tests_browses_and_previews_through_quoted_names(snowflake: FakeServer) -> None:
    report = SnowflakeConnector().test(SF, {"password": "pw"})
    assert report.ok
    assert [s.status for s in report.steps] == [StepStatus.OK] * 4 + [StepStatus.SKIPPED]
    connect = snowflake.connects[0]
    assert connect["login_timeout"] <= 10 and connect["network_timeout"] <= 60
    listing = SnowflakeConnector().browse(SF, {"password": "pw"}, "")
    assert [(i.schema_name, i.table) for i in listing.items] == [("PUBLIC", "ORDERS")]
    preview = SnowflakeConnector().preview(
        SF, {"password": "pw"}, Selection(schema_name="PUBLIC", table="ORDERS")
    )
    assert preview.rows[0] == ("1", "[REDACTED:email]")
    assert snowflake.executed[-1] == 'SELECT * FROM "PUBLIC"."ORDERS" LIMIT 20'


def test_snowflake_sign_in_errors_are_plain(snowflake: FakeServer) -> None:
    error = FakeError("Incorrect username or password was specified.")
    error.errno = 390100  # type: ignore[attr-defined]
    snowflake.refuse_sign_in = error
    report = SnowflakeConnector().test(SF, {"password": "pw"})
    assert report.steps[0].status is StepStatus.OK
    assert report.steps[1].message == "The user name or password was refused."
    unreachable = FakeError("could not connect to Snowflake backend")
    unreachable.errno = 250001  # type: ignore[attr-defined]
    snowflake.refuse_sign_in = unreachable
    assert SnowflakeConnector().test(SF, {"password": "pw"}).steps[0].status is StepStatus.FAILED


# --- BigQuery ---------------------------------------------------------------------------------------
@dataclass
class _Field:
    name: str


class _Row:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self._values = values

    def values(self) -> tuple[Any, ...]:
        return self._values


class _Rows:
    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]], page: int) -> None:
        self.schema = [_Field(c) for c in columns]
        self._rows = [_Row(r) for r in rows]
        self._page = page

    def __iter__(self) -> Iterator[_Row]:
        return iter(self._rows)

    @property
    def pages(self) -> Iterator[list[_Row]]:
        for start in range(0, len(self._rows), self._page):
            yield self._rows[start : start + self._page]


class _BigQueryClient:
    tables: ClassVar[dict[str, dict[str, tuple[list[str], list[tuple[Any, ...]]]]]] = {}
    calls: ClassVar[list[tuple[str, Any]]] = []

    def __init__(self, project: str, credentials: Any) -> None:
        self.project = project
        assert credentials == "creds-for:svc@p.iam"

    def list_datasets(self, max_results: int, timeout: int) -> list[Any]:
        self.calls.append(("list_datasets", timeout))
        return [types.SimpleNamespace(dataset_id=d) for d in self.tables]

    def list_tables(self, ref: str, max_results: int, timeout: int) -> list[Any]:
        dataset = ref.split(".", 1)[1]
        return [types.SimpleNamespace(table_id=t) for t in self.tables[dataset]]

    def list_rows(self, ref: str, max_results: int, page_size: int, timeout: int) -> _Rows:
        self.calls.append(("list_rows", ref))
        _project, dataset, table = ref.split(".", 2)
        columns, rows = self.tables[dataset][table]
        return _Rows(columns, rows[:max_results], page=2)


@pytest.fixture
def bigquery(monkeypatch: pytest.MonkeyPatch) -> type[_BigQueryClient]:
    _BigQueryClient.tables = {
        "crm": {"people": (["id", "phone"], [(i, "+91 98765 43210") for i in range(5)])}
    }
    _BigQueryClient.calls = []
    bq = types.ModuleType("google.cloud.bigquery")
    bq.Client = _BigQueryClient  # type: ignore[attr-defined]
    sa = types.ModuleType("google.oauth2.service_account")
    sa.Credentials = types.SimpleNamespace(  # type: ignore[attr-defined]
        from_service_account_info=lambda info: f"creds-for:{info['client_email']}"
    )
    modules = {"google.cloud.bigquery": bq, "google.oauth2.service_account": sa}
    monkeypatch.setattr(bigquery_module, "load_sdk", modules.get)
    return _BigQueryClient


BQ = {"project": "p"}
KEY = {"credentials_json": json.dumps({"type": "service_account", "client_email": "svc@p.iam"})}


def test_bigquery_uses_the_table_api_with_no_sql(bigquery: type[_BigQueryClient]) -> None:
    report = BigQueryConnector().test(BQ, KEY)
    assert report.ok and report.steps[-1].status is StepStatus.SKIPPED
    listing = BigQueryConnector().browse(BQ, KEY, "")
    assert [(i.schema_name, i.table) for i in listing.items] == [("crm", "people")]
    preview = BigQueryConnector().preview(BQ, KEY, Selection(schema_name="crm", table="people"))
    assert preview.rows[0] == ("0", "[REDACTED:phone]")
    sink = io.BytesIO()
    fetched = BigQueryConnector().fetch(
        BQ, KEY, Selection(schema_name="crm", table="people"), sink, limit_bytes=10**6, max_rows=100
    )
    assert fetched.rows == 5 and sink.getvalue().decode().splitlines()[0] == "id,phone"
    assert len(sink.getvalue().decode().splitlines()) == 6, "one header across three pages"
    assert all(timeout <= 60 for name, timeout in bigquery.calls if name == "list_datasets")


def test_bigquery_rejects_a_key_that_is_not_json_without_quoting_it(bigquery: type[_BigQueryClient]) -> None:
    report = BigQueryConnector().test(BQ, {"credentials_json": "not json at all"})
    sign_in = next(s for s in report.steps if s.name is StepName.SIGN_IN)
    assert sign_in.status is StepStatus.FAILED and "not json" not in report.model_dump_json()
    with pytest.raises(ConnectorError):
        BigQueryConnector().preview(BQ, KEY, Selection(schema_name="crm", table="people; DROP"))


# --- Azure Blob -------------------------------------------------------------------------------------
class ResourceNotFoundError(Exception):
    pass


class ClientAuthenticationError(Exception):
    pass


class _Download:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def readall(self) -> bytes:
        return self.data

    def chunks(self) -> Iterator[bytes]:
        yield self.data[:3]
        yield self.data[3:]


class _Container:
    blobs: ClassVar[dict[str, bytes]] = {}
    missing = False
    refuse = False

    def get_container_properties(self) -> dict[str, str]:
        if self.refuse:
            raise ClientAuthenticationError("Server failed to authenticate the request.")
        if self.missing:
            raise ResourceNotFoundError("The specified container does not exist.")
        return {}

    def list_blobs(self, name_starts_with: str | None = None) -> list[Any]:
        return [
            types.SimpleNamespace(name=k, size=len(v))
            for k, v in self.blobs.items()
            if k.startswith(name_starts_with or "")
        ]

    def walk_blobs(self, name_starts_with: str | None, delimiter: str) -> list[Any]:
        prefix = name_starts_with or ""
        seen: dict[str, Any] = {}
        for key, value in self.blobs.items():
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix) :]
            if delimiter in rest:
                folder = prefix + rest.split(delimiter)[0] + delimiter
                seen[folder] = types.SimpleNamespace(name=folder)
            else:
                seen[key] = types.SimpleNamespace(name=key, size=len(value))
        return list(seen.values())

    def download_blob(self, key: str, offset: int | None = None, length: int | None = None) -> _Download:
        data = self.blobs[key]
        start = offset or 0
        return _Download(data[start : start + length] if length else data[start:])

    def get_blob_client(self, key: str) -> Any:
        return types.SimpleNamespace(
            get_blob_properties=lambda: types.SimpleNamespace(size=len(self.blobs[key]))
        )


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    _Container.blobs = {"exports/people.csv": b"id,email\n1,a@example.com\n", "exports/notes.txt": b"x"}
    _Container.missing = _Container.refuse = False
    made: list[dict[str, Any]] = []

    class BlobServiceClient:
        def __init__(self, **kwargs: Any) -> None:
            made.append(kwargs)

        def get_container_client(self, name: str) -> _Container:
            assert name == "data"
            return _Container()

    module = types.ModuleType("azure.storage.blob")
    module.BlobServiceClient = BlobServiceClient  # type: ignore[attr-defined]
    monkeypatch.setattr(azure_module, "load_sdk", lambda _name: module)
    return made


AZ = {"account": "acmedata", "container": "data"}
READ_SAS = {"credential": "?sv=2024-01-01&sp=rl&se=2030-01-01&sig=abc%3D"}


def test_azure_with_a_read_list_sas_passes_including_the_read_only_check(azure: list[dict[str, Any]]) -> None:
    report = AzureBlobConnector().test(AZ, READ_SAS)
    assert report.ok and report.steps[-1].status is StepStatus.OK
    assert azure[0]["account_url"] == "https://acmedata.blob.core.windows.net"
    assert azure[0]["connection_timeout"] <= 10 and azure[0]["read_timeout"] <= 60
    preview = AzureBlobConnector().preview(AZ, READ_SAS, Selection(path="exports/people.csv"))
    assert preview.rows == (("1", "[REDACTED:email]"),)
    listing = AzureBlobConnector().browse(AZ, READ_SAS, "")
    assert [(i.name, i.kind) for i in listing.items] == [("exports", "folder")]
    sink = io.BytesIO()
    AzureBlobConnector().fetch(
        AZ, READ_SAS, Selection(path="exports/people.csv"), sink, limit_bytes=1000, max_rows=1
    )
    assert sink.getvalue() == _Container.blobs["exports/people.csv"]


def test_azure_account_keys_and_writable_sas_tokens_are_warnings(azure: list[dict[str, Any]]) -> None:
    key = AzureBlobConnector().test(AZ, {"credential": "bXlrZXk="})
    assert key.steps[-1].status is StepStatus.WARNING and "SAS" in (key.steps[-1].fix or "")
    writable = AzureBlobConnector().test(AZ, {"credential": "sv=2024&sp=rwdl&sig=x"})
    assert writable.steps[-1].status is StepStatus.WARNING
    assert sas_permissions("sv=1&sp=rl&sig=z") == "rl" and sas_permissions("plainkey") is None


def test_azure_failures_name_the_step(azure: list[dict[str, Any]]) -> None:
    _Container.missing = True
    report = AzureBlobConnector().test(AZ, READ_SAS)
    listed = next(s for s in report.steps if s.name is StepName.LIST)
    assert listed.status is StepStatus.FAILED and "no container called data" in listed.message
    _Container.missing, _Container.refuse = False, True
    report = AzureBlobConnector().test(AZ, READ_SAS)
    assert next(s for s in report.steps if s.name is StepName.SIGN_IN).status is StepStatus.FAILED
    bad = AzureBlobConnector().test({**AZ, "account": "Not Valid!"}, READ_SAS)
    assert next(s for s in bad.steps if s.name is StepName.SIGN_IN).status is StepStatus.FAILED
