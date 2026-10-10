"""`POST /doctor/diagnosis` and `POST /doctor/handoff`: Data Doctor, the place (Plan L M151, DEC-1512).

Home's "Bring your data" uploads each file the ordinary way (`POST /uploads`, so intake, limits,
retention and erasure are an upload's) and then asks here what the files are and what they can power.
Picking a use case asks for a hand-off: the same file, read the same way (the same sheet, intake on or
off), becomes an upload of that use case, so its Guided setup opens on it with nothing to redo. The
original upload is left as it is.

Both routes need an Analyst, as uploading does: they read the files' columns and make uploads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import APIRouter, Response

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.uploads import (
    finish_upload,
    http_error,
    load_upload,
    load_upload_profile,
    profile_row_cap,
    use_case_config,
)
from api.schemas import ErrorResponse, UploadResponse
from engine.access.roles import Role
from engine.config import (
    DEFAULT_INDUSTRY,
    ConfigError,
    RunMode,
    UseCaseStatus,
    get_roles,
    list_industries,
    load_industry,
)
from engine.doctor.contracts import Diagnosis, DiagnosisRequest, HandoffRequest
from engine.doctor.diagnose import LoadedFile, Offer, diagnose_file, readiness
from engine.intake.config import load_intake_config
from engine.intake.contracts import IntakeReport
from engine.intake.run import received_format_for
from engine.stages import ingest
from engine.storage import Storage, upload_key
from engine.utils.ids import new_upload_id

router: APIRouter = APIRouter(tags=["doctor"])

_ERRORS: Final[dict[int | str, dict[str, object]]] = {
    404: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    415: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

register(
    {
        ("POST", "/doctor/diagnosis"): RoutePolicy(
            role=Role.ANALYST, action="doctor.diagnose", purpose="ask Data Doctor about uploaded files"
        ),
        ("POST", "/doctor/handoff"): RoutePolicy(
            role=Role.ANALYST,
            action="doctor.handoff",
            purpose="start a use case from a file Data Doctor looked at",
            object_type="upload",
        ),
    }
)


def offers(root_path: Path) -> tuple[Offer, ...]:
    """The trainable use cases Home lists (the default journey's available cards), in Home's order."""
    industries = list_industries(root_path)
    chosen = DEFAULT_INDUSTRY if DEFAULT_INDUSTRY in industries else (industries[0] if industries else None)
    if chosen is None:
        return ()
    found: list[Offer] = []
    seen: set[str] = set()
    for stage, ref in load_industry(chosen, root_path).all_refs():
        if ref.status is not UseCaseStatus.AVAILABLE or ref.id in seen:
            continue
        try:
            config = use_case_config(ref.id, root_path)
        except ConfigError:
            continue
        if config.trainable_in_phase_1 and config.target.column:
            seen.add(ref.id)
            found.append(Offer(config=config, stage=stage.name))
    return tuple(found)


def _load(storage: Storage, upload_id: str, cap: int) -> LoadedFile:
    record = load_upload(storage, upload_id)
    profile = load_upload_profile(storage, upload_id)
    frame = ingest.read_upload(
        storage, record.source_key, file_format=record.file_format, max_rows=None, row_cap=cap
    ).frame
    notes: tuple[str, ...] = ()
    if record.intake_key is not None and storage.exists(record.intake_key):
        notes = storage.read_model(record.intake_key, IntakeReport).notes
    return LoadedFile(
        upload_id=upload_id, file_name=record.file_name, frame=frame, profile=profile, notes=notes
    )


@router.post(
    "/doctor/diagnosis",
    response_model=Diagnosis,
    responses=_ERRORS,
    summary="What each uploaded file is, and which use cases the files can power",
)
def diagnose(body: DiagnosisRequest, storage: StorageDep, root: ConfigRootDep) -> Diagnosis:
    available = offers(root)
    if not available:
        raise http_error(404, "DOCTOR_NO_USE_CASES", "There is no use case to check the files against.")
    any_config = available[0].config
    cap = min(profile_row_cap(offer.config) for offer in available)
    files = [_load(storage, upload_id, cap) for upload_id in dict.fromkeys(body.upload_ids)]
    roles = get_roles(root)
    return Diagnosis(
        files=tuple(diagnose_file(loaded, any_config, roles) for loaded in files),
        use_cases=readiness(files, available, root),
    )


@router.post(
    "/doctor/handoff",
    response_model=UploadResponse,
    status_code=201,
    responses=_ERRORS,
    summary="The file as an upload of the chosen use case, read the same way, for its Guided setup",
)
def handoff(
    body: HandoffRequest, response: Response, storage: StorageDep, root: ConfigRootDep
) -> UploadResponse:
    config = use_case_config(body.use_case, root)
    record = load_upload(storage, body.upload_id)
    if record.use_case_id == config.id:
        response.status_code = 200
        profile = load_upload_profile(storage, record.upload_id)
        return UploadResponse(
            upload_id=record.upload_id, profile=profile, intake=_intake(storage, record.intake_key)
        )
    previous = _intake(storage, record.intake_key)
    received = received_format_for(record.file_name) if record.original_key else record.file_format
    source = record.original_key or record.source_key
    upload_id = new_upload_id()
    target = upload_key(upload_id, "original.xlsx" if received == "xlsx" else f"source.{received}")
    size = storage.size_bytes(source)
    if size > config.validation.max_file_size_mb * 1024 * 1024:
        raise http_error(
            413,
            "UPLOAD_TOO_LARGE",
            f"The file is larger than the {config.validation.max_file_size_mb} MB limit.",
        )
    with storage.open_read(source) as reader, storage.open_write(target) as writer:
        while chunk := reader.read(1 << 20):
            writer.write(chunk)
    upload = finish_upload(
        storage,
        config,
        upload_id=upload_id,
        source_key=target,
        file_format=received,
        file_name=record.file_name,
        file_size_bytes=size,
        mode=record.mode if record.mode is RunMode.SCORE else RunMode.TRAIN,
        synthetic=record.synthetic,
        intake=load_intake_config(root),
        sheet=previous.sheet if previous is not None else None,
        intake_enabled=not (previous is not None and previous.turned_off),
    )
    response.headers["Location"] = f"/uploads/{upload_id}/profile"
    return upload


def _intake(storage: Storage, key: str | None) -> IntakeReport | None:
    return storage.read_model(key, IntakeReport) if key is not None and storage.exists(key) else None
