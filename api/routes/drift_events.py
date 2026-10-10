"""Events noted against a scoring run's drift report (Plan J M109, DEC-1319).

* `GET /runs/{run_id}/drift-events` (Viewer): the notes beside the drift the run measured, with the
  server's own reading of which ones can be a reason (`engine.decide.drift_annotations.build_view`).
* `POST /runs/{run_id}/drift-events` (Analyst, audited): note an event.
* `DELETE /runs/{run_id}/drift-events/{annotation_id}` (Analyst, audited): remove a note.

A note changes nothing about scoring: `drift.json` and the run's verdict are never written here. The
audit event carries the run, the kind of event and the count - never the note's text, which is free
text a person typed. No customer id travels in any URL or body.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from api.access import set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import StorageDep
from api.routes.runs import load_run, requested_by
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.decide.drift_annotations import (
    DRIFT_ANNOTATION_INVALID,
    DRIFT_ANNOTATION_LIMIT,
    DRIFT_ANNOTATION_NOT_FOUND,
    DRIFT_NOT_MEASURED,
    MAX_NOTE_CHARACTERS,
    DriftAnnotationError,
    DriftEventsView,
    EventKind,
    add_annotation,
    build_view,
    remove_annotation,
)
from engine.storage import StorageError

__all__ = ["POLICIES", "DriftEventRequest", "router"]

router: APIRouter = APIRouter(tags=["drift events"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/runs/{run_id}/drift-events"): RoutePolicy(
        role=Role.VIEWER,
        action="drift_events.read",
        object_type="run",
        object_param="run_id",
        purpose="see the events noted against a run's change report",
    ),
    ("POST", "/runs/{run_id}/drift-events"): RoutePolicy(
        role=Role.ANALYST,
        action="drift_events.add",
        object_type="run",
        object_param="run_id",
        purpose="note an event that may explain a change in the customers",
    ),
    ("DELETE", "/runs/{run_id}/drift-events/{annotation_id}"): RoutePolicy(
        role=Role.ANALYST,
        action="drift_events.remove",
        object_type="run",
        object_param="run_id",
        purpose="remove a noted event",
    ),
}
"""This router's rows of the policy table, registered at import (see `api/access_policy.py`)."""

register(POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

_STATUS: Final[dict[str, int]] = {
    DRIFT_NOT_MEASURED: 409,
    DRIFT_ANNOTATION_LIMIT: 409,
    DRIFT_ANNOTATION_INVALID: 422,
    DRIFT_ANNOTATION_NOT_FOUND: 404,
}


class DriftEventRequest(BaseModel):
    """`POST /runs/{run_id}/drift-events`: one event."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    event_date: date = Field(description="The day the event happened.")
    kind: EventKind = Field(description="What kind of event it was.")
    note: str = Field(
        min_length=1,
        max_length=MAX_NOTE_CHARACTERS,
        description="A short plain note. Do not put personal data in it.",
    )
    measures: tuple[str, ...] = Field(
        default=(),
        max_length=50,
        description="The measures the event touches; leave empty for a general event.",
    )


def _refuse(error: DriftAnnotationError, request: Request) -> Exception:
    set_audit_context(request, details={"reason_code": error.code})
    return http_error(_STATUS.get(error.code, 409), error.code, error.message)


@router.get(
    "/runs/{run_id}/drift-events",
    response_model=DriftEventsView,
    responses=_ERRORS,
    summary="Events noted against a scoring run's change report",
)
def read_drift_events(run_id: str, storage: StorageDep) -> DriftEventsView:
    """The notes with the server's reading of each; `drift_measured` false for a run with no change report."""
    load_run(storage, run_id)
    try:
        return build_view(storage, run_id)
    except StorageError as exc:
        raise http_error(404, "ARTEFACT_NOT_FOUND", "The run's change report could not be read.") from exc


@router.post(
    "/runs/{run_id}/drift-events",
    response_model=DriftEventsView,
    status_code=201,
    responses=_ERRORS,
    summary="Note an event that may explain a change in the customers",
)
def add_drift_event(
    run_id: str, body: DriftEventRequest, request: Request, storage: StorageDep
) -> DriftEventsView:
    """Stores the note and answers the whole view, so the screen redraws from one answer."""
    load_run(storage, run_id)
    try:
        add_annotation(
            storage,
            run_id,
            event_date=body.event_date,
            kind=body.kind,
            note=body.note,
            measures=body.measures,
            recorded_by=requested_by(request),
        )
    except DriftAnnotationError as exc:
        raise _refuse(exc, request) from None
    view = build_view(storage, run_id)
    set_audit_context(
        request,
        details={
            "run_id": run_id,
            "count": len(view.events),
            "reason_code": f"EVENT_{body.kind.value.upper()}",
        },
    )
    return view


@router.delete(
    "/runs/{run_id}/drift-events/{annotation_id}",
    response_model=DriftEventsView,
    responses=_ERRORS,
    summary="Remove a noted event",
)
def remove_drift_event(
    run_id: str, annotation_id: str, request: Request, storage: StorageDep
) -> DriftEventsView:
    load_run(storage, run_id)
    try:
        remove_annotation(storage, run_id, annotation_id)
    except DriftAnnotationError as exc:
        raise _refuse(exc, request) from None
    view = build_view(storage, run_id)
    set_audit_context(request, details={"run_id": run_id, "count": len(view.events)})
    return view
