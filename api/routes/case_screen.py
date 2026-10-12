"""What the Data Doctor screen reads (Plan O M247, DEC-1817): a case with its view, and the client's cases.

Mounted only when `data_doctor.unified` is on (the `PLAN-O (M247)` hook in `api/main.py`, beside M231's, M235's and
M246's): with it off - the default - neither path exists and docs/API.md is unchanged.

| Method and path | Role | Does |
|---|---|---|
| `GET /cases/{case_id}/screen` | Viewer | The case, its timeline and what the screen shows beside it: the verdict with the next action, the sentence Approve records, the changes that need their own yes, the remembered answers with their dates, and what the hand-off starts (`engine.doctor.screen`). |
| `GET /data-doctor/cases` | Viewer | The client's cases, newest change first (at most 20); `goal_id` keeps those whose goal it is. The Data page opens the newest; the wizard and a goal's setup open their goal's. |

Refusals: `404 CASE_NOT_FOUND` (also another client's case), `409 CASE_UNREADABLE`.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query, Request

from api.access import set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.config import StrictBase
from engine.doctor.case import CLIENT_ID_PATTERN, DEFAULT_CLIENT_ID, CaseError
from engine.doctor.goal_brief import GOAL_ID_PATTERN
from engine.doctor.screen import CaseScreen, CaseSummary, build_screen, case_summaries

__all__ = ["POLICIES", "CaseList", "router"]

router: APIRouter = APIRouter(tags=["cases"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/cases/{case_id}/screen"): RoutePolicy(
        role=Role.VIEWER,
        action="cases.screen",
        purpose="see a Data Doctor case as its screen shows it",
        object_type="case",
        object_param="case_id",
    ),
    ("GET", "/data-doctor/cases"): RoutePolicy(
        role=Role.VIEWER, action="cases.list", purpose="see the Data Doctor cases of a client"
    ),
}
register(POLICIES)

_ERRORS: Final[dict[int | str, dict[str, object]]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
}


class CaseList(StrictBase):
    """Answer of `GET /data-doctor/cases`."""

    cases: tuple[CaseSummary, ...]


@router.get(
    "/cases/{case_id}/screen",
    response_model=CaseScreen,
    responses=_ERRORS,
    summary="A Data Doctor case as its screen shows it",
)
def case_screen(
    case_id: str,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    client_id: Annotated[str, Query(pattern=CLIENT_ID_PATTERN)] = DEFAULT_CLIENT_ID,
) -> CaseScreen:
    try:
        screen = build_screen(storage, client_id, case_id, config_root=root)
    except CaseError as exc:
        raise http_error(404 if exc.code == "CASE_NOT_FOUND" else 409, exc.code, exc.message) from exc
    set_audit_context(request, object_id=case_id, object_type="case")
    return screen


@router.get(
    "/data-doctor/cases",
    response_model=CaseList,
    responses=_ERRORS,
    summary="A client's Data Doctor cases, newest change first",
)
def list_cases(
    storage: StorageDep,
    client_id: Annotated[str, Query(pattern=CLIENT_ID_PATTERN)] = DEFAULT_CLIENT_ID,
    goal_id: Annotated[str | None, Query(pattern=GOAL_ID_PATTERN)] = None,
) -> CaseList:
    return CaseList(cases=case_summaries(storage, client_id, goal_id=goal_id))
