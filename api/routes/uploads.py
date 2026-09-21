"""`POST /uploads` and `GET /uploads/{upload_id}/profile` (design §4.1, §4.2).

The upload route never holds the file in memory: it streams the request body into `Storage` a
megabyte at a time, counting as it goes, so the configured size limit is enforced before the bytes
land and an aborted upload leaves nothing behind. Everything after the write is ingest's job; this
module only maps an `IngestError` onto the M1 error envelope.

`engine.stages.ingest` is imported as a module rather than by name because the two upload/run routers
are the only callers of its M2 surface and referencing it through the module gives every test one
seam to stub, without any of them reaching into the route's own globals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from api.deps import ConfigRootDep, StorageDep
from api.schemas import ErrorResponse, UploadRecord, UploadResponse
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
from engine.stages import ingest
from engine.storage import Storage, StorageError, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.time import utc_now

router: APIRouter = APIRouter(tags=["uploads"])

CHUNK_BYTES: Final[int] = 1 << 20
"""How much of the request body is read, counted and written at a time."""

DEFAULT_PROFILE_ROW_CAP: Final[int] = 2_000_000
"""Fallback for `config.validation.profile_row_cap` until that leaf lands (design §7, owner H)."""

UPLOAD_RECORD_FILENAME: Final[str] = "upload.json"
UPLOAD_PROFILE_FILENAME: Final[str] = "profile.json"
UPLOAD_FINGERPRINT_FILENAME: Final[str] = "fingerprint.json"
UPLOAD_VALIDATION_FILENAME: Final[str] = "validation.json"

UNSUPPORTED_FORMAT_CODE: Final[str] = "UPLOAD_UNSUPPORTED_FORMAT"
"""The one `IngestError` code that is a 415 rather than a 422 (design §4.1)."""

FileField = Annotated[UploadFile, File(description="The CSV or Parquet file to profile.")]
UseCaseField = Annotated[str, Form(description="Use case whose limits and hints drive the profile.")]
ModeField = Annotated[RunMode, Form(description="train or score; recorded on the upload record.")]

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
    summary="Store a CSV or Parquet file, profile it and return everything the Setup screen renders",
)
async def create_upload(
    storage: StorageDep,
    root: ConfigRootDep,
    response: Response,
    file: FileField,
    use_case: UseCaseField,
    mode: ModeField = RunMode.TRAIN,
) -> UploadResponse:
    """Stream the file in, profile it and persist the upload directory of design §5.1.

    The use case is loaded *before* a single byte is read, because its `max_file_size_mb` is the
    limit being enforced and an unknown or planned id must not cost the user an upload.
    """
    config = use_case_config(use_case, root)
    limit_mb = config.validation.max_file_size_mb
    upload_id = new_upload_id()
    try:
        file_format = ingest.file_format_for(file.filename or "")
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc

    source_key = upload_key(upload_id, source_filename(file_format))
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

    try:
        result = ingest.read_upload(
            storage, source_key, file_format=file_format, row_cap=profile_row_cap(config)
        )
        profile = ingest.profile_dataset(
            result.frame,
            config,
            upload_id=upload_id,
            file_name=file.filename or source_filename(file_format),
            file_format=result.file_format,
            file_size_bytes=total,
            delimiter=result.delimiter,
            encoding=result.encoding,
            row_count=result.row_count,
        )
    except ingest.IngestError as exc:
        delete_upload(storage, upload_id)
        raise ingest_http(exc.code, exc.message) from exc

    record = UploadRecord(
        upload_id=upload_id,
        use_case_id=config.id,
        mode=mode,
        file_name=profile.file_name,
        file_format=profile.file_format,
        file_size_bytes=total,
        delimiter=profile.delimiter,
        encoding=profile.encoding,
        row_count=profile.row_count,
        column_count=profile.column_count,
        source_key=source_key,
        profile_key=upload_key(upload_id, UPLOAD_PROFILE_FILENAME),
        fingerprint_key=upload_key(upload_id, UPLOAD_FINGERPRINT_FILENAME),
        fingerprint_hash=profile.fingerprint.hash,
        created_at=utc_now(),
    )
    storage.write_model(record.profile_key, profile)
    storage.write_model(record.fingerprint_key, profile.fingerprint)
    storage.write_model(upload_key(upload_id, UPLOAD_RECORD_FILENAME), record)
    response.headers["Location"] = f"/uploads/{upload_id}/profile"
    return UploadResponse(upload_id=upload_id, profile=profile)


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

    The leaf is added to `engine/config.py` by its own owner (design §7, H); reading it defensively
    keeps these routes independent of that change's landing order and costs one attribute lookup.
    """
    return int(getattr(config.validation, "profile_row_cap", DEFAULT_PROFILE_ROW_CAP))


def source_filename(file_format: str) -> str:
    """`source.csv` or `source.parquet` - the one place the stored name is spelled."""
    return f"source.{file_format}"


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


def delete_upload(storage: Storage, upload_id: str) -> None:
    """Remove every key of `uploads/<upload_id>/`, so a failed upload leaves no orphan."""
    for key in storage.list_keys(f"uploads/{upload_id}/"):
        storage.delete(key)


def http_error(status_code: int, code: str, message: str, path: str | None = None) -> HTTPException:
    """M1's error envelope as an exception: `{"detail": {"code", "message", "path"}}`."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, "path": path})


def ingest_http(code: str, message: str) -> HTTPException:
    """Every `IngestError` is a 422 except the unsupported suffix, which is a 415 (design §4.1)."""
    return http_error(415 if code == UNSUPPORTED_FORMAT_CODE else 422, code, message)


def upload_not_found(upload_id: str) -> HTTPException:
    return http_error(404, "UPLOAD_NOT_FOUND", f"No upload with id {upload_id!r}.")
