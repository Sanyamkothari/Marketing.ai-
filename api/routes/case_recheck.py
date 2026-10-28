"""Next month's recheck: a ready case's approved plan replays on a new upload, and only what changed is asked again.

Mounted beside the visit's routes (`api/main.py`).

| Method and path | Role | Does |
|---|---|---|
| `POST /cases/{case_id}/recheck` | Analyst | Takes the new upload(s) for a ready case, replays the approved plan on them through the one materialisation (`engine.doctor.materialize`), and opens a NEW case linked to the old one (`recheck_of`). Approved items keep their approval, except what the outcome and consent mean and any item the moved dates changed (those wait for a yes again); each thing that differs from the approved run is a question or a stop in the new case (a hard rule is a stop no yes lifts), and the visit goes on from there without reading the files from the start. Answers the drift report. |
| `GET /cases/{case_id}/recheck` | Viewer | The drift report a recheck made, for the case it opened. |

Refusals: `404 CASE_NOT_FOUND`, `404 UPLOAD_NOT_FOUND`, `409 RECHECK_NOT_READY` (the case was not approved and taken to
ready), `422 RECHECK_FILES_MISMATCH` (one new upload for each file of the case, and no others), `409
RECHECK_BASELINE_FAILED` (the approved plan no longer runs on the approved files). The old case is never changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Query, Request
from pydantic import Field, model_validator

from api.access import PrincipalDep, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.case_visit import _load_full
from api.routes.cases import ClientId, _adapted, _case_file
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.config import StrictBase
from engine.doctor.case import (
    CLIENT_ID_PATTERN,
    DEFAULT_CLIENT_ID,
    Case,
    CaseError,
    CaseStore,
    _process_lock,
)
from engine.doctor.recheck import (
    RecheckError,
    RecheckReport,
    open_recheck_case,
    read_report,
    recheck,
    report_key,
)
from engine.storage import Storage
from engine.utils.time import utc_now

__all__ = ["POLICIES", "router"]

router: APIRouter = APIRouter(tags=["cases"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("POST", "/cases/{case_id}/recheck"): RoutePolicy(
        role=Role.ANALYST,
        action="cases.recheck",
        purpose="replay a ready case's approved plan on next month's file",
        object_type="case",
        object_param="case_id",
    ),
    ("GET", "/cases/{case_id}/recheck"): RoutePolicy(
        role=Role.VIEWER,
        action="cases.recheck_read",
        purpose="see the drift report of a recheck",
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


class RecheckRequest(StrictBase):
    """Body of `POST /cases/{case_id}/recheck`: the new upload for each file of the case."""

    client_id: ClientId = DEFAULT_CLIENT_ID
    upload_id: str | None = Field(
        default=None, min_length=1, max_length=64, description="The new upload, for a case of one file."
    )
    uploads: dict[str, str] = Field(
        default_factory=dict, description="Old upload id -> the new upload that stands in for it."
    )

    @model_validator(mode="after")
    def _one_way(self) -> RecheckRequest:
        if (self.upload_id is None) == (not self.uploads):
            raise ValueError("give `upload_id` (one file) or `uploads` (several), not both and not neither")
        return self


def _error(exc: CaseError | RecheckError) -> Exception:
    status = {"CASE_NOT_FOUND": 404, "RECHECK_FILES_MISMATCH": 422}.get(exc.code, 409)
    return http_error(status, exc.code, exc.message)


@router.post(
    "/cases/{case_id}/recheck",
    response_model=RecheckReport,
    status_code=201,
    responses=_ERRORS,
    summary="Replay a ready case's approved plan on next month's file",
)
def recheck_case(
    case_id: str,
    body: RecheckRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> RecheckReport:
    store = CaseStore(storage)
    try:
        if _adapted(storage, body.client_id, case_id) is not None:
            raise CaseError("CASE_READ_ONLY", "An earlier Guided setup result cannot be rechecked here.")
        case = store.get(body.client_id, case_id)
        uploads = dict(body.uploads)
        if body.upload_id is not None:
            if len(case.files) != 1:
                raise RecheckError(
                    "RECHECK_FILES_MISMATCH",
                    "This case has several files: say which new upload replaces which.",
                )
            uploads = {case.files[0].upload_id: body.upload_id}
        if set(uploads) != set(case.upload_ids()):
            raise RecheckError(
                "RECHECK_FILES_MISMATCH", "Give one new file for each file of the case, and no others."
            )
        with _process_lock(f"recheck/{body.client_id}/{case_id}"):
            report = _recheck_once(store, storage, root, case, uploads, body.client_id, principal.user_id)
    except (CaseError, RecheckError) as exc:
        raise _error(exc) from exc
    set_audit_context(request, object_id=report.case_id, object_type="case")
    return report


def _recheck_once(
    store: CaseStore,
    storage: Storage,
    root: Path,
    case: Case,
    uploads: dict[str, str],
    client_id: str,
    actor: str,
) -> RecheckReport:
    """One follow-up case per (source case, new uploads): a retried request answers the case it already opened, and
    a report that was not written (the request died between the two writes) is written then."""
    made = next(
        (
            c
            for c in store.list(client_id)
            if c.recheck_of is not None
            and c.recheck_of.source_case_id == case.case_id
            and c.recheck_of.uploads == uploads
        ),
        None,
    )
    if made is not None:
        kept = read_report(storage, client_id, made.case_id)
        if kept is not None:
            return kept
    new_ids = [uploads[f.upload_id] for f in case.files]
    rechecked = recheck(
        case,
        uploads,
        _load_full(storage, case.upload_ids(), root),
        _load_full(storage, new_ids, root),
        now=utc_now(),
    )
    if made is None:
        new_case_files = [_case_file(storage, upload_id, actor) for upload_id in new_ids]
        made = open_recheck_case(store, case, rechecked, new_case_files, actor=actor)
    report = rechecked.report.model_copy(update={"case_id": made.case_id})
    storage.write_model(report_key(client_id, made.case_id), report)
    return report


@router.get(
    "/cases/{case_id}/recheck",
    response_model=RecheckReport,
    responses=_ERRORS,
    summary="The drift report of the recheck that opened this case",
)
def recheck_report(
    case_id: str,
    storage: StorageDep,
    client_id: Annotated[str, Query(pattern=CLIENT_ID_PATTERN)] = DEFAULT_CLIENT_ID,
) -> RecheckReport:
    try:
        CaseStore(storage).get(client_id, case_id)
    except CaseError as exc:
        raise _error(exc) from exc
    report = read_report(storage, client_id, case_id)
    if report is None:
        raise http_error(404, "RECHECK_NOT_FOUND", "This case was not opened by a recheck.")
    return report
