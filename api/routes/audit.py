"""The audit viewer's API (Phase 4b M47): read the trail, download it as CSV, export it.

All three routes are Admin-only. Reading the trail is not itself a row per page view - the viewer
polls, and a row per poll would bury the events it is there to show - but downloading it is: the
CSV is `audit_reads`, so "who took a copy of the audit log" is in the audit log (DEC-704).

`POST /audit/exports` writes the window as JSON lines to the data directory, or, when
`audit_export_bucket` is set, to S3 under Object Lock in compliance mode (DEC-715). Its own audit
event carries the file's SHA-256 as `after_hash` and its key as `export_key`, so the event proves
which file the export produced and the file can be checked against the event later.

Filters are exactly `AuditQuery`'s: actor, action (exact, or a family ending in `.`), object type
and id, outcome, and a `[since, until)` window.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query, Request, Response

from api.access import AuditLogDep, platform_data_dir, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import SettingsDep
from api.routes.uploads import http_error
from api.schemas import AuditEventPage, AuditExportRequest, AuditExportResponse, ErrorResponse
from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.audit.export import collect_events, export_events, export_sink_for, render_csv

__all__ = ["POLICIES", "router"]

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(tags=["audit"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/audit/events"): RoutePolicy(
        role=Role.ADMIN, action="audit.query", purpose="read the audit log"
    ),
    ("GET", "/audit/events.csv"): RoutePolicy(
        role=Role.ADMIN, action="audit.download", purpose="download the audit log", audit_reads=True
    ),
    ("POST", "/audit/exports"): RoutePolicy(
        role=Role.ADMIN, action="audit.export", object_type="audit_export", purpose="export the audit log"
    ),
}
"""This router's rows of the policy table, registered at import (see `api/access_policy.py`)."""

register(POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}

ActorQuery = Annotated[str | None, Query(max_length=128, description="Only this actor's events.")]
ActionQuery = Annotated[
    str | None, Query(max_length=120, description="Exact action, or a family ending in `.` (e.g. `models.`).")
]
ObjectTypeQuery = Annotated[
    str | None, Query(max_length=64, description="Only events on this kind of object.")
]
ObjectIdQuery = Annotated[str | None, Query(max_length=200, description="Only events on this object.")]
OutcomeQuery = Annotated[str | None, Query(max_length=16, description="`success`, `denied` or `failed`.")]
SinceQuery = Annotated[datetime | None, Query(description="Inclusive lower bound (ISO 8601 with an offset).")]
UntilQuery = Annotated[datetime | None, Query(description="Exclusive upper bound (ISO 8601 with an offset).")]
LimitQuery = Annotated[int, Query(ge=1, le=1000, description="Page size.")]
OffsetQuery = Annotated[int, Query(ge=0, description="Events to skip, newest first.")]


def _window(
    actor_id: str | None,
    action: str | None,
    object_type: str | None,
    object_id: str | None,
    outcome: str | None,
    since: datetime | None,
    until: datetime | None,
) -> AuditQuery:
    for moment in (since, until):
        if moment is not None and moment.tzinfo is None:
            raise http_error(
                422, "TIMESTAMP_NEEDS_OFFSET", "Give `since` and `until` with a time-zone offset."
            )
    return AuditQuery(
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        outcome=outcome,
        since=since,
        until=until,
    )


@router.get("/audit/events", response_model=AuditEventPage, responses=_ERRORS, summary="Read the audit log")
def list_events(
    audit_log: AuditLogDep,
    actor_id: ActorQuery = None,
    action: ActionQuery = None,
    object_type: ObjectTypeQuery = None,
    object_id: ObjectIdQuery = None,
    outcome: OutcomeQuery = None,
    since: SinceQuery = None,
    until: UntilQuery = None,
    limit: LimitQuery = 100,
    offset: OffsetQuery = 0,
) -> AuditEventPage:
    """One page of matching events, newest first, with the total count. Admin only."""
    window = _window(actor_id, action, object_type, object_id, outcome, since, until)
    page = window.model_copy(update={"limit": limit, "offset": offset})
    return AuditEventPage(
        events=audit_log.query(page), total=audit_log.count(window), limit=limit, offset=offset
    )


@router.get(
    "/audit/events.csv",
    response_class=Response,
    responses={**_ERRORS, 200: {"content": {"text/csv": {}}}},
    summary="Download the audit log as CSV",
)
def download_events(
    audit_log: AuditLogDep,
    request: Request,
    actor_id: ActorQuery = None,
    action: ActionQuery = None,
    object_type: ObjectTypeQuery = None,
    object_id: ObjectIdQuery = None,
    outcome: OutcomeQuery = None,
    since: SinceQuery = None,
    until: UntilQuery = None,
) -> Response:
    """Every matching event, oldest first, as CSV; formula-looking cells are neutralised."""
    events = collect_events(
        audit_log, _window(actor_id, action, object_type, object_id, outcome, since, until)
    )
    set_audit_context(request, details={"count": len(events)})
    return Response(
        content=render_csv(events),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="audit_events.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/audit/exports",
    response_model=AuditExportResponse,
    status_code=201,
    responses=_ERRORS,
    summary="Export the audit log (JSON lines; S3 Object Lock when configured)",
)
def create_export(
    body: AuditExportRequest, request: Request, audit_log: AuditLogDep, settings: SettingsDep
) -> AuditExportResponse:
    """Write the window to the export sink and record the file's hash in this request's audit event."""
    window = AuditQuery(since=body.since, until=body.until, action=body.action)
    sink = export_sink_for(settings, platform_data_dir(request))
    try:
        result = export_events(audit_log, sink, window)
    except Exception as exc:
        # boto3's errors are not importable on a laptop without it; the class name is enough to act on.
        logger.error("audit export failed error=%s", type(exc).__name__)
        set_audit_context(request, details={"reason_code": type(exc).__name__[:64]})
        raise http_error(
            502,
            "AUDIT_EXPORT_FAILED",
            "The audit export could not be written. The server log has the reason.",
        ) from None
    set_audit_context(
        request,
        object_id=result.key,
        after_hash=result.sha256,
        details={"export_key": result.key, "count": result.event_count},
    )
    return result
