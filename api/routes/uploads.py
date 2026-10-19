"""`POST /uploads` and `GET /uploads/{upload_id}/profile` (plan §8).

The upload route never holds the file in memory: it streams the request body into `Storage` a
megabyte at a time, counting as it goes, so the configured size limit is enforced before the bytes
land and an aborted upload leaves nothing behind. Everything after the write is intake's and ingest's
job; this module only maps an `IngestError` onto the M1 error envelope.

Plan L M150 (DEC-1501): between the write and ingest, Data Doctor's intake rules read the file the way
a person would - an Excel workbook's table, the column names under a title, no "Grand Total" row, "-"
read as empty in a column of numbers - and rewrite it only when one of them applies, keeping the file
as received beside it. The form's `intake=false` reads the file exactly as before.

`engine.stages.ingest` is imported as a module rather than by name because the two upload/run routers
are the only callers of its M2 surface and referencing it through the module gives every test one
seam to stub, without any of them reaching into the route's own globals.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.schemas import ErrorResponse, UploadRecord, UploadResponse
from engine.access.roles import Role
from engine.agent.contracts import DATA_RECIPE_FILENAME, RECIPE_RECEIPT_FILENAME
from engine.config import (
    ConfigError,
    RunMode,
    UseCaseConfig,
    UseCaseStatus,
    list_industries,
    list_use_case_ids,
    load_industry,
    load_use_case,
)
from engine.contracts import DatasetProfile
from engine.doctor.memory import offer_for
from engine.doctor.renames import RENAMED_COPY_FILENAME
from engine.intake.config import IntakeConfig, load_intake_config
from engine.intake.run import INTAKE_REPORT_FILENAME, ReceivedFormat, intake_file, received_format_for
from engine.measurement.pull import PULL_SOURCE_FILENAME
from engine.stages import ingest
from engine.storage import Storage, StorageError, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.time import utc_now

router: APIRouter = APIRouter(tags=["uploads"])
_LOGGER = logging.getLogger(__name__)

CHUNK_BYTES: Final[int] = 1 << 20
"""How much of the request body is read, counted and written at a time."""

DEFAULT_PROFILE_ROW_CAP: Final[int] = 2_000_000
"""Fallback for `config.validation.profile_row_cap` until that config leaf exists."""

UPLOAD_RECORD_FILENAME: Final[str] = "upload.json"
UPLOAD_PROFILE_FILENAME: Final[str] = "profile.json"
UPLOAD_FINGERPRINT_FILENAME: Final[str] = "fingerprint.json"
UPLOAD_VALIDATION_FILENAME: Final[str] = "validation.json"
IMPORT_SOURCE_FILENAME: Final[str] = "connection_source.json"
"""Beside an imported upload's `upload.json`: which connection it came from (written by `api.routes.connections`)."""

LIST_DEFAULT_LIMIT: Final[int] = 100
LIST_MAX_LIMIT: Final[int] = 500

UNSUPPORTED_FORMAT_CODE: Final[str] = "UPLOAD_UNSUPPORTED_FORMAT"
"""The one `IngestError` code that is a 415 rather than a 422."""

FileField = Annotated[UploadFile, File(description="The CSV, Excel (.xlsx) or Parquet file to profile.")]
UseCaseField = Annotated[str, Form(description="Use case whose limits and hints drive the profile.")]
ModeField = Annotated[RunMode, Form(description="train or score; recorded on the upload record.")]
SyntheticField = Annotated[
    bool,
    Form(
        description=(
            "True when the file is generated data, not a client's. Every run that reads it is "
            "quarantined as synthetic: its reports say the effect is planted (Plan J M95)."
        )
    ),
]
SheetField = Annotated[
    str | None,
    Form(
        max_length=255,
        description="For an Excel workbook, the sheet to read; by default the visible sheet with the largest table.",
    ),
]
IntakeField = Annotated[
    bool,
    Form(
        description=(
            "False reads the file exactly as it arrived, without Data Doctor's intake rules (no header "
            "search, no total rows left out, no missing words read as empty). A workbook is still converted."
        )
    ),
]

_UPLOAD_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    415: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}


@router.post(
    "/uploads",
    response_model=UploadResponse,
    status_code=201,
    responses=_UPLOAD_ERRORS,
    summary="Store a CSV, Excel or Parquet file, read it, profile it and return everything the Setup screen renders",
)
async def create_upload(
    storage: StorageDep,
    root: ConfigRootDep,
    response: Response,
    file: FileField,
    use_case: UseCaseField,
    mode: ModeField = RunMode.TRAIN,
    synthetic: SyntheticField = False,
    sheet: SheetField = None,
    intake: IntakeField = True,
) -> UploadResponse:
    """Stream the file in, profile it and persist the `uploads/<upload_id>/` directory.

    The use case is loaded *before* a single byte is read, because its `max_file_size_mb` is the
    limit being enforced and an unknown or planned id must not cost the user an upload.
    """
    config = use_case_config(use_case, root)
    limit_mb = config.validation.max_file_size_mb
    upload_id = new_upload_id()
    try:
        received = received_format_for(file.filename or "")
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc

    source_key = upload_key(upload_id, received_filename(received))
    total = 0
    try:
        with storage.open_write(source_key) as sink:
            while chunk := await file.read(CHUNK_BYTES):
                total += len(chunk)
                if total > limit_mb * 1024 * 1024:
                    raise http_error(
                        413, "UPLOAD_TOO_LARGE", f"The file is larger than the {limit_mb} MB limit."
                    )
                sink.write(chunk)
    except HTTPException:
        delete_upload(storage, upload_id)
        raise

    # Reading a big file takes seconds to minutes: off the event loop, so other requests are served.
    upload = await run_in_threadpool(
        finish_upload,
        storage,
        config,
        upload_id=upload_id,
        source_key=source_key,
        file_format=received,
        file_name=file.filename or received_filename(received),
        file_size_bytes=total,
        mode=mode,
        synthetic=synthetic,
        intake=load_intake_config(root),
        sheet=sheet,
        intake_enabled=intake,
    )
    response.headers["Location"] = f"/uploads/{upload_id}/profile"
    return upload


def finish_upload(
    storage: Storage,
    config: UseCaseConfig,
    *,
    upload_id: str,
    source_key: str,
    file_format: ReceivedFormat,
    file_name: str,
    file_size_bytes: int,
    mode: RunMode,
    synthetic: bool = False,
    intake: IntakeConfig | None = None,
    sheet: str | None = None,
    intake_enabled: bool = True,
) -> UploadResponse:
    """Read a file already written at `source_key` through intake, profile it and record it as upload `upload_id`.

    Everything `POST /uploads` does after the bytes land, shared with an import from a connection
    (Plan H M80), so an imported table is an upload in every respect. An unreadable file removes the
    whole upload directory and raises the same 415/422 the upload route gives. A workbook arrives at
    `original.xlsx` (`received_filename`); intake writes the table it reads to `source.csv`.
    """
    try:
        prepared = intake_file(
            storage,
            received_key=source_key,
            received_format=file_format,
            csv_key=upload_key(upload_id, source_filename("csv")),
            original_key=upload_key(upload_id, f"original.{file_format}"),
            config=intake or IntakeConfig(),
            sheet=sheet,
            enabled=intake_enabled,
        )
        result = ingest.read_upload(
            storage, prepared.source_key, file_format=prepared.file_format, row_cap=profile_row_cap(config)
        )
        profile = ingest.profile_dataset(
            result.frame,
            config,
            upload_id=upload_id,
            file_name=file_name,
            file_format=result.file_format,
            file_size_bytes=file_size_bytes,
            delimiter=result.delimiter,
            encoding=result.encoding,
            row_count=result.row_count,
        )
    except ingest.IngestError as exc:
        delete_upload(storage, upload_id)
        raise ingest_http(exc.code, exc.message) from exc
    except Exception:
        # No upload.json is written, so retention would never find what was stored: remove it now.
        delete_upload(storage, upload_id)
        raise

    record = UploadRecord(
        upload_id=upload_id,
        use_case_id=config.id,
        mode=mode,
        file_name=profile.file_name,
        file_format=profile.file_format,
        file_size_bytes=file_size_bytes,
        delimiter=profile.delimiter,
        encoding=profile.encoding,
        row_count=profile.row_count,
        column_count=profile.column_count,
        source_key=prepared.source_key,
        original_key=prepared.report.original_key,
        intake_key=upload_key(upload_id, INTAKE_REPORT_FILENAME) if prepared.worth_keeping else None,
        profile_key=upload_key(upload_id, UPLOAD_PROFILE_FILENAME),
        fingerprint_key=upload_key(upload_id, UPLOAD_FINGERPRINT_FILENAME),
        fingerprint_hash=profile.fingerprint.hash,
        created_at=utc_now(),
        synthetic=synthetic,
    )
    storage.write_model(record.profile_key, profile)
    storage.write_model(record.fingerprint_key, profile.fingerprint)
    if record.intake_key is not None:
        storage.write_model(record.intake_key, prepared.report)
    storage.write_model(upload_key(upload_id, UPLOAD_RECORD_FILENAME), record)
    # Plan L M159 (DEC-1593): this shape treated before? Say so; the treatment is offered, never applied.
    treatment = offer_for(
        storage,
        use_case_id=config.id,
        upload_id=upload_id,
        columns=profile.fingerprint.columns,
        intake=prepared.report,
        mode=mode.value,
    )
    return UploadResponse(upload_id=upload_id, profile=profile, intake=prepared.report, treatment=treatment)


@router.get(
    "/uploads/{upload_id}/profile",
    response_model=DatasetProfile,
    responses=_NOT_FOUND,
    summary="The stored dataset profile of one upload",
)
def read_profile(upload_id: str, storage: StorageDep) -> Response:
    """The stored `profile.json`, byte for byte: the Data page reads the same document ingest wrote."""
    try:
        payload = storage.read_bytes(upload_key(upload_id, UPLOAD_PROFILE_FILENAME))
    except StorageError as exc:
        raise upload_not_found(upload_id) from exc
    return Response(content=payload, media_type="application/json")


# ---------------------------------------------------------------------------
# Plan M, M179 (DEC-1609): the list of uploaded files, for the Data page
# ---------------------------------------------------------------------------
register(
    {
        ("GET", "/uploads"): RoutePolicy(
            role=Role.VIEWER,
            action="uploads.list",
            purpose="see the uploaded files",
            object_type="upload",
            object_param=None,
        )
    }
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


MadeBy = Literal["file", "import", "pull", "renamed_copy", "recipe"]
"""How an upload came to be, from the marker file beside its `upload.json`. `file` means no marker: a file posted to
`POST /uploads`, or one a route stored without leaving a marker. It is not a claim that a person uploaded it."""


class UploadSummary(_Strict):
    """One uploaded file as the Data page lists it: what it is and when it came, never its content."""

    upload_id: str
    file_name: str
    file_format: str
    file_size_bytes: int
    row_count: int
    column_count: int
    use_case_id: str
    mode: RunMode
    synthetic: bool
    created_at: datetime
    from_connection_id: str | None = None
    made_by: MadeBy = "file"
    source_upload_id: str | None = None
    """For a prepared copy (`made_by` "recipe"): the upload its recipe read, so the Data page can group the copy."""


class UploadListResponse(_Strict):
    """Body of `GET /uploads`: the newest uploads, and how many there are in all."""

    uploads: tuple[UploadSummary, ...]
    total: int


def _connection_of(storage: Storage, key: str) -> str | None:
    """The `connection_id` a marker file (`connection_source.json`, `pull_source.json`) records, or None."""
    try:
        document = json.loads(storage.read_bytes(key))
    except (StorageError, ValueError):
        return None
    value = document.get("connection_id") if isinstance(document, dict) else None
    return value if isinstance(value, str) else None


def _made_by(storage: Storage, keys: frozenset[str], upload_id: str) -> tuple[MadeBy, str | None]:
    """How `upload_id` came to be and, for an import or a pull, which connection it came from.

    The markers are looked up in the key listing the route already holds, so a plain file costs no extra read.
    """
    import_key = upload_key(upload_id, IMPORT_SOURCE_FILENAME)
    if import_key in keys:
        return "import", _connection_of(storage, import_key)
    pull_key = upload_key(upload_id, PULL_SOURCE_FILENAME)
    if pull_key in keys:
        return "pull", _connection_of(storage, pull_key)
    if upload_key(upload_id, RENAMED_COPY_FILENAME) in keys:
        return "renamed_copy", None
    if upload_key(upload_id, DATA_RECIPE_FILENAME) in keys:
        return "recipe", None
    return "file", None


def _source_of(storage: Storage, keys: frozenset[str], upload_id: str) -> str | None:
    """The upload a prepared copy was made from, as its `recipe_receipt.json` records it, or None."""
    key = upload_key(upload_id, RECIPE_RECEIPT_FILENAME)
    if key not in keys:
        return None
    try:
        document = json.loads(storage.read_bytes(key))
    except (StorageError, ValueError):
        return None
    value = document.get("upload_id") if isinstance(document, dict) else None
    return value if isinstance(value, str) and value != upload_id else None


@router.get(
    "/uploads",
    response_model=UploadListResponse,
    summary="The uploaded files, newest first",
)
def list_uploads(
    storage: StorageDep,
    limit: Annotated[
        int, Query(ge=1, le=LIST_MAX_LIMIT, description="How many uploads to return, newest first.")
    ] = LIST_DEFAULT_LIMIT,
) -> UploadListResponse:
    """One summary per `upload.json`, with `total` counting every readable one. A record that cannot be read is left
    out and logged, never fatal."""
    found: list[UploadRecord] = []
    keys = frozenset(storage.list_keys("uploads/"))
    for key in keys:
        if not key.endswith(f"/{UPLOAD_RECORD_FILENAME}"):
            continue
        try:
            found.append(storage.read_model(key, UploadRecord))
        except (StorageError, ValueError):
            _LOGGER.warning("uploads.unreadable key=%s", key)
    found.sort(key=lambda record: (record.created_at, record.upload_id), reverse=True)
    summaries: list[UploadSummary] = []
    for record in found[:limit]:
        made_by, connection_id = _made_by(storage, keys, record.upload_id)
        summaries.append(
            UploadSummary(
                upload_id=record.upload_id,
                file_name=record.file_name,
                file_format=record.file_format,
                file_size_bytes=record.file_size_bytes,
                row_count=record.row_count,
                column_count=record.column_count,
                use_case_id=record.use_case_id,
                mode=record.mode,
                synthetic=record.synthetic,
                created_at=record.created_at,
                from_connection_id=connection_id,
                made_by=made_by,
                source_upload_id=_source_of(storage, keys, record.upload_id) if made_by == "recipe" else None,
            )
        )
    return UploadListResponse(uploads=tuple(summaries), total=len(found))


# ---------------------------------------------------------------------------
# Helpers shared with `api.routes.runs`
# ---------------------------------------------------------------------------
def use_case_config(use_case_id: str, root: Path) -> UseCaseConfig:
    """The merged config of `use_case_id`, or a `ConfigError` the app renders as a 404.

    A planned id is `USE_CASE_PLANNED` and an id nobody declares is `USE_CASE_NOT_FOUND`, exactly as
    `GET /use-cases/{id}` distinguishes them (DEC-003).
    """
    if use_case_id in list_use_case_ids(root):
        return load_use_case(use_case_id, root)
    for industry_id in list_industries(root):
        for _stage, ref in load_industry(industry_id, root).all_refs():
            if ref.id == use_case_id and ref.status is UseCaseStatus.PLANNED:
                raise ConfigError("USE_CASE_PLANNED", "This use case is not available yet.")
    raise ConfigError("USE_CASE_NOT_FOUND", f"There is no use case called {use_case_id!r}.", path=use_case_id)


def profile_row_cap(config: UseCaseConfig) -> int:
    """`config.validation.profile_row_cap`, or `DEFAULT_PROFILE_ROW_CAP` until that leaf exists.

    The leaf is added to `engine/config.py` by the change that owns it; reading it defensively
    keeps these routes independent of that change's landing order and costs one attribute lookup.
    """
    return int(getattr(config.validation, "profile_row_cap", DEFAULT_PROFILE_ROW_CAP))


def source_filename(file_format: str) -> str:
    """`source.csv` or `source.parquet` - the one place the stored name is spelled."""
    return f"source.{file_format}"


def received_filename(received: ReceivedFormat) -> str:
    """Where a received file lands: `source.csv` / `source.parquet`, or `original.xlsx` for a workbook,
    which intake turns into `source.csv` (Plan L M150)."""
    return "original.xlsx" if received == "xlsx" else source_filename(received)


def load_upload(storage: Storage, upload_id: str) -> UploadRecord:
    """The upload's `upload.json`, or a 404 `UPLOAD_NOT_FOUND`."""
    try:
        return storage.read_model(upload_key(upload_id, UPLOAD_RECORD_FILENAME), UploadRecord)
    except StorageError as exc:
        raise upload_not_found(upload_id) from exc


def load_upload_profile(storage: Storage, upload_id: str) -> DatasetProfile:
    """The upload's `profile.json`, or a 404 `UPLOAD_NOT_FOUND`."""
    try:
        return storage.read_model(upload_key(upload_id, UPLOAD_PROFILE_FILENAME), DatasetProfile)
    except StorageError as exc:
        raise upload_not_found(upload_id) from exc


def existing_upload_with_bytes(
    storage: Storage, use_case_id: str, mode: RunMode, file_name: str, data: bytes
) -> UploadRecord | None:
    """An upload already stored for this use case and mode with this name and exactly these bytes, or None.

    Approving the same data again makes the same prepared or combined file; it is reused, never saved twice.
    """
    wanted = hashlib.sha256(data).hexdigest()
    for key in storage.list_keys("uploads/"):
        if not key.endswith(f"/{UPLOAD_RECORD_FILENAME}"):
            continue
        try:
            record = storage.read_model(key, UploadRecord)
            if (
                record.use_case_id == use_case_id
                and record.mode is mode
                and record.file_name == file_name
                and record.file_size_bytes == len(data)
                and hashlib.sha256(storage.read_bytes(record.source_key)).hexdigest() == wanted
            ):
                return record
        except (StorageError, ValueError):
            continue
    return None


def delete_upload(storage: Storage, upload_id: str) -> None:
    """Remove every key of `uploads/<upload_id>/`, so a failed upload leaves no orphan."""
    for key in storage.list_keys(f"uploads/{upload_id}/"):
        storage.delete(key)


def http_error(status_code: int, code: str, message: str, path: str | None = None) -> HTTPException:
    """M1's error envelope as an exception: `{"detail": {"code", "message", "path"}}`."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, "path": path})


def ingest_http(code: str, message: str) -> HTTPException:
    """Every `IngestError` is a 422 except the unsupported suffix, which is a 415."""
    return http_error(415 if code == UNSUPPORTED_FORMAT_CODE else 422, code, message)


def upload_not_found(upload_id: str) -> HTTPException:
    return http_error(404, "UPLOAD_NOT_FOUND", f"No upload with id {upload_id!r}.")
