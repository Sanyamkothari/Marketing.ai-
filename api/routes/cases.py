"""Data Doctor's cases (Plan O M231, DEC-1801): one persistent object per piece of data work.

Mounted only when `data_doctor.unified` is on (the `_install_cases` hook in `api/main.py`'s PLAN-O block): with it off -
the default - none of these paths exists and docs/API.md is unchanged. (The OpenAPI schema still gains the
additive retention values `case_data`, `case_log`, `forget_upload`, `RetentionItem.upload_id` and
`RetentionResult.forgotten`, which `engine.privacy` defines whatever the switch.)

| Method and path | Role | Does |
|---|---|---|
| `POST /cases` | Analyst | Opens a case for a client, optionally with files (uploads by id). `201` with the case at version 1. |
| `GET /cases/{case_id}` | Viewer | The case. `guided_<upload id>` opens that upload's Guided-setup session as a read-only case. |
| `POST /cases/{case_id}/files` | Analyst | Adds an upload to the case, against the version the caller read. |
| `GET /cases/{case_id}/timeline` | Viewer | The case's append-only processing log, oldest first. |

**Per client.** Every route takes `client_id` (default `default`, the single-tenant client). A case is
looked up only under its own client: another client's case answers `404 CASE_NOT_FOUND`, exactly like a
case that does not exist, so the answer never says another client has it. Read-only adapters over older
work exist only for the `default` client, because uploads are the deployment's own (`docs/CLIENT_ISOLATION.md`).

**Versions.** A change names the `version` it was made against. Of two people who loaded version 3 and
both add a file, the first gets version 4 and the second `409 CASE_VERSION_CONFLICT`: they reload and see
what changed instead of overwriting it (`engine.doctor.case.CaseStore`).

**Unreadable.** A stored case that no longer validates, or an older Guided session that cannot be shown as
a case, answers `409 CASE_UNREADABLE`, never a server error.

**Who.** Every change records the signed-in user's id in the timeline (`local-operator` with sign-in off).
A Viewer may read a case and its timeline; changing one needs an Analyst, as uploading does.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, ValidationError

from api.access import PrincipalDep, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import StorageDep
from api.routes.uploads import http_error, load_upload
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.agent.contracts import AGENT_SESSION_FILENAME, AgentSession
from engine.config import StrictBase
from engine.doctor.case import (
    CLIENT_ID_PATTERN,
    DEFAULT_CLIENT_ID,
    Case,
    CaseError,
    CaseFile,
    CaseStore,
    TimelineEvent,
    case_from_session,
)
from engine.intake.contracts import IntakeReport
from engine.storage import Storage, StorageError, upload_key
from engine.utils.time import utc_now

__all__ = ["POLICIES", "router"]

router: APIRouter = APIRouter(tags=["cases"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("POST", "/cases"): RoutePolicy(
        role=Role.ANALYST, action="cases.create", purpose="open a Data Doctor case", object_type="case"
    ),
    ("GET", "/cases/{case_id}"): RoutePolicy(
        role=Role.VIEWER,
        action="cases.read",
        purpose="see a Data Doctor case",
        object_type="case",
        object_param="case_id",
    ),
    ("POST", "/cases/{case_id}/files"): RoutePolicy(
        role=Role.ANALYST,
        action="cases.add_file",
        purpose="add a file to a Data Doctor case",
        object_type="case",
        object_param="case_id",
    ),
    ("GET", "/cases/{case_id}/timeline"): RoutePolicy(
        role=Role.VIEWER,
        action="cases.timeline",
        purpose="see what happened in a Data Doctor case",
        object_type="case",
        object_param="case_id",
    ),
}
register(POLICIES)

_ERRORS: Final[dict[int | str, dict[str, object]]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

MAX_FILES: Final[int] = 20
"""Files one request may open a case with, as `POST /doctor/diagnosis` reads at most 20."""

ClientId = Annotated[
    str, Field(pattern=CLIENT_ID_PATTERN, description="Whose case; `default` single-tenant.")
]


_READ_ONLY: Final[str] = "This is an earlier Guided setup result shown as a case; it cannot be changed here."


class CreateCaseRequest(StrictBase):
    """Body of `POST /cases`."""

    client_id: ClientId = DEFAULT_CLIENT_ID
    title: str = Field(default="", max_length=200, description="What the person calls this piece of work.")
    upload_ids: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = Field(
        default=(), max_length=MAX_FILES, description="Uploads to start with, in order."
    )


class AddFileRequest(StrictBase):
    """Body of `POST /cases/{case_id}/files`."""

    client_id: ClientId = DEFAULT_CLIENT_ID
    version: int = Field(ge=1, description="The case version the caller read; a stale one is refused (409).")
    upload_id: str = Field(min_length=1, max_length=64, description="The upload to add.")


class CaseTimeline(StrictBase):
    """Answer of `GET /cases/{case_id}/timeline`."""

    case_id: str = Field(description="The case.")
    events: tuple[TimelineEvent, ...] = Field(description="Every event, oldest first.")


def _error(exc: CaseError) -> Exception:
    return http_error(404 if exc.code == "CASE_NOT_FOUND" else 409, exc.code, exc.message)


def _unmappable() -> CaseError:
    return CaseError(
        "CASE_UNREADABLE",
        "This earlier Guided setup cannot be shown as a case. Nothing was changed; open it from the Guided "
        "setup screen instead.",
    )


def _case_file(storage: Storage, upload_id: str, user_id: str) -> CaseFile:
    """The upload as a case file, by reference; `404 UPLOAD_NOT_FOUND` when there is no such upload."""
    record = load_upload(storage, upload_id)
    sheet: str | None = None
    if record.intake_key is not None and storage.exists(record.intake_key):
        try:
            sheet = storage.read_model(record.intake_key, IntakeReport).sheet
        except (StorageError, ValueError):
            sheet = None
    return CaseFile(
        upload_id=record.upload_id,
        file_name=record.file_name,
        sheet=sheet,
        intake_key=record.intake_key,
        fingerprint=record.fingerprint_hash,
        added_at=utc_now(),
        added_by=user_id,
    )


def _adapted(storage: Storage, client_id: str, case_id: str) -> Case | None:
    """`guided_<upload id>`: that upload's Guided-setup session as a read-only case, or None.

    `CASE_UNREADABLE` when the session exists but does not map onto a case.
    """
    if not case_id.startswith("guided_") or client_id != DEFAULT_CLIENT_ID:
        return None
    upload_id = case_id.removeprefix("guided_")
    try:
        session = storage.read_model(upload_key(upload_id, f"agent/{AGENT_SESSION_FILENAME}"), AgentSession)
    except (StorageError, ValueError):
        return None
    try:
        file_name = load_upload(storage, upload_id).file_name
    except HTTPException:  # an upload record gone leaves the name blank, not the case
        file_name = ""
    try:
        return case_from_session(session, client_id=client_id, file_name=file_name)
    except ValidationError as exc:
        raise _unmappable() from exc


@router.post(
    "/cases",
    response_model=Case,
    status_code=201,
    responses=_ERRORS,
    summary="Open a Data Doctor case, optionally with files",
)
def create_case(
    body: CreateCaseRequest, request: Request, storage: StorageDep, principal: PrincipalDep
) -> Case:
    wanted = tuple(dict.fromkeys(body.upload_ids))
    files = [_case_file(storage, upload_id, principal.user_id) for upload_id in wanted]
    case = CaseStore(storage).create(
        client_id=body.client_id, created_by=principal.user_id, title=body.title, files=files
    )
    set_audit_context(request, object_id=case.case_id, object_type="case")
    return case


@router.get(
    "/cases/{case_id}",
    response_model=Case,
    responses=_ERRORS,
    summary="A Data Doctor case",
)
def get_case(
    case_id: str,
    storage: StorageDep,
    client_id: Annotated[str, Query(pattern=CLIENT_ID_PATTERN)] = DEFAULT_CLIENT_ID,
) -> Case:
    try:
        adapted = _adapted(storage, client_id, case_id)
        return adapted if adapted is not None else CaseStore(storage).get(client_id, case_id)
    except CaseError as exc:
        raise _error(exc) from exc


@router.post(
    "/cases/{case_id}/files",
    response_model=Case,
    responses=_ERRORS,
    summary="Add a file to a Data Doctor case, against the version you read",
)
def add_case_file(
    case_id: str, body: AddFileRequest, request: Request, storage: StorageDep, principal: PrincipalDep
) -> Case:
    store = CaseStore(storage)
    try:
        if _adapted(storage, body.client_id, case_id) is not None:
            raise CaseError("CASE_READ_ONLY", _READ_ONLY)
        store.get(body.client_id, case_id)  # a missing case is named before a missing upload
    except CaseError as exc:
        raise _error(exc) from exc
    file = _case_file(storage, body.upload_id, principal.user_id)
    try:
        case = store.add_file(
            body.client_id, case_id, expected_version=body.version, actor=principal.user_id, file=file
        )
    except CaseError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise _error(exc) from exc
    return case


@router.get(
    "/cases/{case_id}/timeline",
    response_model=CaseTimeline,
    responses=_ERRORS,
    summary="What happened in a Data Doctor case, oldest first",
)
def case_timeline(
    case_id: str,
    storage: StorageDep,
    client_id: Annotated[str, Query(pattern=CLIENT_ID_PATTERN)] = DEFAULT_CLIENT_ID,
) -> CaseTimeline:
    try:
        if _adapted(storage, client_id, case_id) is not None:
            return CaseTimeline(case_id=case_id, events=())
        return CaseTimeline(case_id=case_id, events=CaseStore(storage).timeline(client_id, case_id))
    except CaseError as exc:
        raise _error(exc) from exc
