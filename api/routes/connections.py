"""`/connections`: set up, test, browse and import from the places a client's data lives (Plan H M80).

Owned by Plan H. The rules are `engine/connections`'; what is added here is what only an HTTP
boundary can get wrong.

**No secret is ever sent back.** A connection's view carries its non-secret settings and the *names*
of the secrets it has saved, never a value. Create and update read their body themselves, like
`/connection/aws`: FastAPI's default `422` repeats the offending input, which for these two routes
would put a password into the response, devtools and any proxy log. Their errors name fields only.

**Every network call runs off the event loop, a few at a time.** A test, a listing, a preview or an
import talks to someone else's server with timeouts of up to a minute; at most
`_MAX_CONCURRENT_CALLS` run at once (imports: `_MAX_CONCURRENT_IMPORTS`) and the rest are told
`429 CONNECTION_BUSY` straight away. Every response is `Cache-Control: no-store`.

**An import is an upload.** `POST /connections/{id}/import` streams the table or file into the
upload store and hands it to `api.routes.uploads.finish_upload`, the same code `POST /uploads` ends
with, so Guided setup, the checks, runs, recipes and scoring treat it exactly as a file a person
uploaded. It is a snapshot: nothing is ever written back.

Access (single-user today, but the policies stay): reading is Viewer, everything that changes a
connection or talks to the service is Analyst.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, Literal, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from api.access import set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, SettingsDep, StorageDep
from api.routes.uploads import delete_upload, finish_upload, http_error, source_filename, use_case_config
from api.schemas import ErrorResponse, UploadResponse
from engine.access.roles import Role
from engine.config import RunMode
from engine.connections import (
    ConnectionRecord,
    ConnectionStore,
    ConnectorError,
    Selection,
    TestReport,
    catalogue,
    check_form,
    connector,
)
from engine.connections.base import BrowseResult, ConfigValue, Connector, FetchResult, KindInfo, Preview
from engine.connections.registry import check_name
from engine.storage import upload_key
from engine.utils.ids import new_upload_id
from engine.utils.time import utc_now

__all__ = ["router"]

router = APIRouter(tags=["connections"])

_T = TypeVar("_T")
_M = TypeVar("_M", bound=BaseModel)

_NO_STORE: Final[str] = "no-store"
_MAX_BODY_BYTES: Final[int] = 64 * 1024
"""A connection's form, with a pasted BigQuery key, is a few kilobytes. Anything larger is not one."""
_MAX_CONCURRENT_CALLS: Final[int] = 4
_MAX_CONCURRENT_IMPORTS: Final[int] = 2
_CALLS: Final[threading.BoundedSemaphore] = threading.BoundedSemaphore(_MAX_CONCURRENT_CALLS)
_IMPORTS: Final[threading.BoundedSemaphore] = threading.BoundedSemaphore(_MAX_CONCURRENT_IMPORTS)
IMPORT_MAX_ROWS: Final[int] = 50_000_000
"""A backstop on a table's rows; the use case's `max_file_size_mb` is the limit that normally applies."""
IMPORT_SOURCE_FILENAME: Final[str] = "connection_source.json"
"""Beside an imported upload's `upload.json`: which connection and which table or file it came from."""

_ID_PARAM = "connection_id"


def _policy(role: Role, action: str, purpose: str, *, object_param: str | None = _ID_PARAM) -> RoutePolicy:
    return RoutePolicy(
        role=role,
        action=action,
        purpose=purpose,
        object_type="connection",
        object_param=object_param,
    )


register(
    {
        ("GET", "/connections/kinds"): _policy(
            Role.VIEWER, "connections.kinds", "see the kinds of connection", object_param=None
        ),
        ("GET", "/connections"): _policy(
            Role.VIEWER, "connections.list", "see connections", object_param=None
        ),
        ("POST", "/connections"): _policy(
            Role.ANALYST, "connections.create", "set up a connection", object_param=None
        ),
        ("GET", "/connections/{connection_id}"): _policy(Role.VIEWER, "connections.read", "see a connection"),
        ("PUT", "/connections/{connection_id}"): _policy(
            Role.ANALYST, "connections.update", "change a connection"
        ),
        ("DELETE", "/connections/{connection_id}"): _policy(
            Role.ANALYST, "connections.delete", "delete a connection"
        ),
        ("POST", "/connections/{connection_id}/test"): _policy(
            Role.ANALYST, "connections.test", "test a connection"
        ),
        ("GET", "/connections/{connection_id}/browse"): _policy(
            Role.ANALYST, "connections.browse", "browse a connection"
        ),
        ("POST", "/connections/{connection_id}/preview"): _policy(
            Role.ANALYST, "connections.preview", "preview data from a connection"
        ),
        ("POST", "/connections/{connection_id}/import"): _policy(
            Role.ANALYST, "connections.import", "import data from a connection"
        ),
    }
)


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectionKinds(_Strict):
    """Body of `GET /connections/kinds`: every card of the catalogue, with its form."""

    kinds: tuple[KindInfo, ...]


class ConnectionView(_Strict):
    """One saved connection as the API shows it. Never a secret: only the names of those saved."""

    connection_id: str
    kind: str
    kind_label: str
    name: str
    config: dict[str, ConfigValue] = Field(description="The non-secret settings.")
    secrets_saved: tuple[str, ...] = Field(description="Which secret fields have a saved value (names only).")
    secrets_readable: bool = Field(
        description="False when the saved secrets were encrypted with another key and must be entered again."
    )
    available: bool = Field(description="False when the kind's add-on is not installed on this server.")
    status: Literal["connected", "failed", "not_tested", "needs_addon"]
    failure: str | None = Field(
        default=None, description="The first failed step's message, after a failed test."
    )
    last_test: TestReport | None = None
    created_at: datetime
    updated_at: datetime


class ConnectionList(_Strict):
    connections: tuple[ConnectionView, ...]


class ConnectionCreate(_Strict):
    """Body of `POST /connections`."""

    kind: str = Field(description="One of the kinds `GET /connections/kinds` lists as creatable.")
    name: str | None = Field(default=None, description="What to call it; defaults to the kind's name.")
    config: dict[str, Any] = Field(default_factory=dict, description="The non-secret fields.")
    secrets: dict[str, Any] = Field(default_factory=dict, description="The secret fields; never sent back.")


class ConnectionUpdate(_Strict):
    """Body of `PUT /connections/{id}`. A secret left out or blank keeps its saved value."""

    name: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    clear_secrets: tuple[str, ...] = Field(
        default=(), description="Saved secrets to forget (e.g. to use the machine's sign-in)."
    )


class ConnectionTestResponse(_Strict):
    connection: ConnectionView
    report: TestReport


class ImportRequest(_Strict):
    """Body of `POST /connections/{id}/import`: what to import, and for which use case."""

    use_case: str = Field(description="Use-case id whose limits and hints apply, as for `POST /uploads`.")
    mode: RunMode = Field(default=RunMode.TRAIN, description="train or score, as for `POST /uploads`.")
    path: str | None = Field(default=None, max_length=1024, description="A store's object key.")
    schema_name: str | None = Field(
        default=None, max_length=256, description="A database's schema or dataset."
    )
    table: str | None = Field(default=None, max_length=256, description="A database's table.")

    def selection(self) -> Selection:
        return Selection(path=self.path, schema_name=self.schema_name, table=self.table)


class ImportSource(_Strict):
    """What `connection_source.json` records about an imported upload. Never a secret."""

    connection_id: str
    kind: str
    path: str | None = None
    schema_name: str | None = None
    table: str | None = None
    imported_at: datetime


def _request_body(model: type[BaseModel]) -> dict[str, Any]:
    """The OpenAPI `requestBody` of a route that reads its body itself, so `/docs` still shows it."""
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get(
    "/connections/kinds",
    response_model=ConnectionKinds,
    summary="Every kind of connection, with its set-up form and whether its add-on is installed",
)
def list_kinds(response: Response) -> ConnectionKinds:
    response.headers["Cache-Control"] = _NO_STORE
    return ConnectionKinds(kinds=tuple(catalogue()))


@router.get("/connections", response_model=ConnectionList, summary="Every saved connection, oldest first")
def list_connections(response: Response, storage: StorageDep, current: SettingsDep) -> ConnectionList:
    response.headers["Cache-Control"] = _NO_STORE
    store = ConnectionStore(storage, current)
    return ConnectionList(connections=tuple(_view(store, r, check_secrets=False) for r in store.list()))


@router.post(
    "/connections",
    response_model=ConnectionView,
    status_code=201,
    responses=_ERRORS,
    summary="Save a new connection; its secrets are encrypted and never returned",
    openapi_extra=_request_body(ConnectionCreate),
)
async def create_connection(
    request: Request, response: Response, storage: StorageDep, current: SettingsDep
) -> ConnectionView:
    response.headers["Cache-Control"] = _NO_STORE
    body = await _parse(request, ConnectionCreate)

    def save() -> ConnectionView:
        conn = _catch(lambda: connector(body.kind))
        info = conn.info()
        name = _catch(lambda: check_name(body.name, info))
        config, secrets = _catch(lambda: check_form(info, body.config, body.secrets))
        store = ConnectionStore(storage, current)
        record = _catch(lambda: store.create(kind=body.kind, name=name, config=config, secrets=secrets))
        return _view(store, record)

    view = await run_in_threadpool(save)
    set_audit_context(request, object_id=view.connection_id)
    response.headers["Location"] = f"/connections/{view.connection_id}"
    return view


@router.get(
    "/connections/{connection_id}",
    response_model=ConnectionView,
    responses=_ERRORS,
    summary="One saved connection, without its secrets",
)
def read_connection(
    connection_id: str, response: Response, storage: StorageDep, current: SettingsDep
) -> ConnectionView:
    response.headers["Cache-Control"] = _NO_STORE
    store = ConnectionStore(storage, current)
    return _view(store, _catch(lambda: store.get(connection_id)))


@router.put(
    "/connections/{connection_id}",
    response_model=ConnectionView,
    responses=_ERRORS,
    summary="Change a connection; a secret left blank keeps its saved value",
    openapi_extra=_request_body(ConnectionUpdate),
)
async def update_connection(
    connection_id: str, request: Request, response: Response, storage: StorageDep, current: SettingsDep
) -> ConnectionView:
    response.headers["Cache-Control"] = _NO_STORE
    body = await _parse(request, ConnectionUpdate)

    def save() -> ConnectionView:
        store = ConnectionStore(storage, current)
        record = _catch(lambda: store.get(connection_id))
        info = _catch(lambda: connector(record.kind)).info()
        name = _catch(lambda: check_name(body.name if body.name is not None else record.name, info))
        secret_fields = {f.name for f in info.fields if f.secret}
        unknown = sorted(set(body.clear_secrets) - secret_fields)
        if unknown:
            raise _error(
                422,
                "CONNECTION_FIELD_UNKNOWN",
                f"Only secret fields can be cleared: {', '.join(sorted(secret_fields))}.",
            )
        kept = (
            frozenset(record.secret_names) - set(body.clear_secrets)
            if store.readable(record)
            else frozenset()
        )
        config, secrets = _catch(lambda: check_form(info, body.config, body.secrets, kept_secrets=kept))
        updated = _catch(
            lambda: store.update(
                record, name=name, config=config, secrets=secrets, drop_secrets=frozenset(body.clear_secrets)
            )
        )
        return _view(store, updated)

    return await run_in_threadpool(save)


@router.delete(
    "/connections/{connection_id}",
    status_code=204,
    responses=_ERRORS,
    summary="Forget a connection and its encrypted secrets; imported uploads stay",
)
def delete_connection(connection_id: str, storage: StorageDep, current: SettingsDep) -> Response:
    store = ConnectionStore(storage, current)
    store.delete(_catch(lambda: store.get(connection_id)))
    return Response(status_code=204, headers={"Cache-Control": _NO_STORE})


@router.post(
    "/connections/{connection_id}/test",
    response_model=ConnectionTestResponse,
    responses=_ERRORS,
    summary="Test a connection step by step: reach, sign in, list, read a sample, read-only check",
)
def test_connection(
    connection_id: str, response: Response, storage: StorageDep, current: SettingsDep
) -> ConnectionTestResponse:
    response.headers["Cache-Control"] = _NO_STORE
    store = ConnectionStore(storage, current)
    record = _catch(lambda: store.get(connection_id))
    conn = _usable(record)
    secrets = _catch(lambda: store.secrets(record))
    result = _bounded(_CALLS, lambda: conn.test(record.config, secrets))
    updated = store.record_test(record, result)
    return ConnectionTestResponse(connection=_view(store, updated), report=result)


@router.get(
    "/connections/{connection_id}/browse",
    response_model=BrowseResult,
    responses=_ERRORS,
    summary="List folders and CSV/Parquet files (stores) or schemas and tables (databases); at most 500",
)
def browse_connection(
    connection_id: str,
    response: Response,
    storage: StorageDep,
    current: SettingsDep,
    path: str = Query(
        default="", max_length=1024, description="A folder prefix or a schema; empty for the top."
    ),
) -> BrowseResult:
    response.headers["Cache-Control"] = _NO_STORE
    store = ConnectionStore(storage, current)
    record = _catch(lambda: store.get(connection_id))
    conn = _usable(record)
    secrets = _catch(lambda: store.secrets(record))
    return _bounded(_CALLS, lambda: conn.browse(record.config, secrets, path))


@router.post(
    "/connections/{connection_id}/preview",
    response_model=Preview,
    responses=_ERRORS,
    summary="The first 20 rows of a table or file, personal data masked",
)
def preview_connection(
    connection_id: str, selection: Selection, response: Response, storage: StorageDep, current: SettingsDep
) -> Preview:
    response.headers["Cache-Control"] = _NO_STORE
    store = ConnectionStore(storage, current)
    record = _catch(lambda: store.get(connection_id))
    conn = _usable(record)
    secrets = _catch(lambda: store.secrets(record))
    return _bounded(_CALLS, lambda: conn.preview(record.config, secrets, selection))


@router.post(
    "/connections/{connection_id}/import",
    response_model=UploadResponse,
    status_code=201,
    responses=_ERRORS,
    summary="Import a table or file as an ordinary upload (a snapshot), exactly like POST /uploads",
)
def import_from_connection(
    connection_id: str,
    body: ImportRequest,
    request: Request,
    response: Response,
    storage: StorageDep,
    current: SettingsDep,
    root: ConfigRootDep,
) -> UploadResponse:
    response.headers["Cache-Control"] = _NO_STORE
    config = use_case_config(body.use_case, root)  # 404 for a planned or unknown use case, before any call
    store = ConnectionStore(storage, current)
    record = _catch(lambda: store.get(connection_id))
    conn = _usable(record)
    secrets = _catch(lambda: store.secrets(record))
    selection = body.selection()
    file_format = _catch(lambda: conn.file_format(selection))
    limit_bytes = config.validation.max_file_size_mb * 1024 * 1024
    upload_id = new_upload_id()
    source_key = upload_key(upload_id, source_filename(file_format))

    def fetch() -> FetchResult:
        with storage.open_write(source_key) as sink:
            return conn.fetch(
                record.config, secrets, selection, sink, limit_bytes=limit_bytes, max_rows=IMPORT_MAX_ROWS
            )

    try:
        fetched = _bounded(_IMPORTS, fetch)
    except BaseException:
        delete_upload(storage, upload_id)
        raise
    upload = finish_upload(
        storage,
        config,
        upload_id=upload_id,
        source_key=source_key,
        file_format=fetched.file_format,
        file_name=fetched.file_name,
        file_size_bytes=fetched.size_bytes,
        mode=body.mode,
    )
    storage.write_model(
        upload_key(upload_id, IMPORT_SOURCE_FILENAME),
        ImportSource(
            connection_id=record.connection_id,
            kind=record.kind,
            path=selection.path,
            schema_name=selection.schema_name,
            table=selection.table,
            imported_at=utc_now(),
        ),
    )
    set_audit_context(request, details={"use_case_id": config.id})
    response.headers["Location"] = f"/uploads/{upload_id}/profile"
    return upload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _view(store: ConnectionStore, record: ConnectionRecord, *, check_secrets: bool = True) -> ConnectionView:
    try:
        conn = connector(record.kind)
        available, label = conn.available(), conn.label
    except ConnectorError:
        available, label = False, record.kind
    return ConnectionView(
        connection_id=record.connection_id,
        kind=record.kind,
        kind_label=label,
        name=record.name,
        config=dict(record.config),
        secrets_saved=record.secret_names,
        secrets_readable=store.readable(record) if check_secrets else True,
        available=available,
        status=record.status(available),
        failure=record.failure(),
        last_test=record.last_test,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _usable(record: ConnectionRecord) -> Connector:
    conn = _catch(lambda: connector(record.kind))
    if not conn.available():
        info = conn.info()
        raise _error(
            409,
            "CONNECTION_NEEDS_ADDON",
            f"{info.label} needs an add-on that is not installed on this server. Ask whoever runs Marketing AI "
            f"to install it: pip install 'marketing-ai[{info.addon}]'.",
        )
    return conn


def _catch(action: Callable[[], _T]) -> _T:
    try:
        return action()
    except ConnectorError as exc:
        raise _connector_http(exc) from None


def _bounded(gate: threading.BoundedSemaphore, action: Callable[[], _T]) -> _T:
    if not gate.acquire(blocking=False):
        busy = _error(
            429, "CONNECTION_BUSY", "Other connection calls are running. Try again in a few seconds."
        )
        busy.headers = {**(busy.headers or {}), "Retry-After": "5"}
        raise busy
    try:
        return _catch(action)
    finally:
        gate.release()


def _connector_http(exc: ConnectorError) -> HTTPException:
    message = f"{exc.message} {exc.fix}" if exc.fix else exc.message
    return _error(exc.status, exc.code, message)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    error = http_error(status_code, code, message)
    error.headers = {"Cache-Control": _NO_STORE}
    return error


async def _parse(request: Request, model: type[_M]) -> _M:
    """`model` from the raw body; errors name the accepted fields and never repeat a value."""
    raw = await _read_body(request)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError):
        raise _error(422, "BODY_INVALID", "The request body is not JSON.") from None
    if not isinstance(data, dict):
        raise _error(422, "BODY_INVALID", "The request body must be a JSON object.")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        accepted = set(model.model_fields)
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]} & accepted)
        named = f" Check {', '.join(f'`{f}`' for f in fields)}." if fields else ""
        raise _error(
            422,
            "BODY_INVALID",
            f"This endpoint accepts {', '.join(f'`{f}`' for f in model.model_fields)}.{named}",
        ) from None


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (
        not (declared.isascii() and declared.isdigit()) or int(declared) > _MAX_BODY_BYTES
    ):
        raise _too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large() -> HTTPException:
    return _error(413, "BODY_TOO_LARGE", "This request body is larger than a connection's settings can be.")
