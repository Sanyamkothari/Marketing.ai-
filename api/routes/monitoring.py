"""Monitoring: outcomes of past scoring runs, the incrementality input, alerts and missed runs (Phase 4b M49).

The measuring is `engine.scheduling.outcomes`' and the alert history is `engine.scheduling.alerts`';
this module decides who may ask and what the one audit event of each request says.

**Who (DEC-780).** Every read - an outcome report, the incrementality input, the alert list, the
missed firings - is Viewer: each holds counts, rates, ids and business language, never a row, like
every other report of a run. Uploading outcomes is Analyst: it is data going in, like `POST
/uploads`, and it can raise a `performance_drop` alert. Acknowledging an alert is Analyst too
(DEC-784): it says "someone is on this", which silences nothing but is a statement about the work,
and a Viewer can see results but not change the state of anything (plan M46). Admin is not needed -
an alert is operational, not a setting.

**Outcomes arrive as a file and are never kept (DEC-771, DEC-785).** `POST /runs/{run_id}/outcomes`
takes a multipart CSV or Parquet file holding the run's primary key and the actual outcome (by
default the column named like the target the model was trained on; `outcome_column` overrides it).
The bytes are read into memory - at most `MAX_OUTCOME_FILE_BYTES` - parsed by the engine, and
dropped: the file is a list of customer ids with what happened to them, and keeping it would be one
more row-level store for retention and erasure to reach. What is written under the run is
aggregates only (`outcome_report.json`, and `incrementality_input.json` for a run with a control
group). The format comes from the file name's suffix. Ingesting again replaces both artefacts; the
audit trail keeps each upload's report hash (`after_hash`), so the history is not lost. The route
answers `201` with the report; the engine's `OutcomeError` codes map to 404/409/422 through
`OUTCOME_ERROR_STATUS`, and a window that has not matured is `409 OUTCOME_WINDOW_NOT_MATURED`, naming
the date it will. The audit event carries the run, model and counts - never the file's name, which
is whatever the client called it.

**Alerts are read newest first and acknowledged once.** Acknowledging records who and when, and a
second acknowledgement keeps the first (the engine's rule), so two people clicking at once leave
one name. The event's `before_hash` is the alert as it was.

**Missed firings are listed across schedules** (`GET /monitoring/missed-firings`): the slots that
passed with nothing running to fire them (DEC-764). A schedule's own history is
`GET /schedules/{schedule_id}/firings?status=missed`.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Annotated, Final, Literal

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from api.access import PrincipalDep, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import get_config_root, get_registry, get_storage
from api.routes.clients import get_client_store
from api.routes.runs import load_run
from api.routes.schedules import get_alert_sink, get_alert_store, get_schedule_store, scheduling_clock
from api.routes.uploads import http_error
from api.schemas import (
    AlertListResponse,
    AlertResponse,
    ErrorResponse,
    FiringListResponse,
    IncrementalityInputResponse,
    OutcomeReportResponse,
)
from engine.access.roles import Role
from engine.audit.events import content_hash
from engine.scheduling.alerts import Alert, AlertError, AlertKind, AlertQuery
from engine.scheduling.outcomes import (
    INCREMENTALITY_INPUT_FILENAME,
    OUTCOME_ERROR_STATUS,
    IncrementalityInput,
    OutcomeError,
    OutcomeReport,
    ingest_outcomes,
    read_outcome_report,
)
from engine.scheduling.schedules import FiringStatus
from engine.storage import StorageError, run_key

__all__ = ["MAX_OUTCOME_FILE_BYTES", "POLICIES", "outcome_file_format", "router"]

router: APIRouter = APIRouter(tags=["monitoring"])

_V: Final[Role] = Role.VIEWER
_AN: Final[Role] = Role.ANALYST

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("POST", "/runs/{run_id}/outcomes"): RoutePolicy(
        role=_AN,
        action="monitoring.outcomes_upload",
        object_type="run",
        object_param="run_id",
        purpose="add a run's real outcomes",
    ),
    ("GET", "/runs/{run_id}/outcomes"): RoutePolicy(
        role=_V,
        action="monitoring.outcomes_read",
        object_type="run",
        object_param="run_id",
        purpose="see a run's real-world performance",
    ),
    ("GET", "/runs/{run_id}/incrementality-input"): RoutePolicy(
        role=_V,
        action="monitoring.incrementality_read",
        object_type="run",
        object_param="run_id",
        purpose="see a run's incrementality input",
    ),
    ("GET", "/monitoring/alerts"): RoutePolicy(
        role=_V, action="monitoring.alerts_list", purpose="see alerts"
    ),
    ("POST", "/monitoring/alerts/{alert_id}/acknowledge"): RoutePolicy(
        role=_AN,
        action="monitoring.alert_acknowledge",
        object_type="alert",
        object_param="alert_id",
        purpose="acknowledge an alert",
    ),
    ("GET", "/monitoring/missed-firings"): RoutePolicy(
        role=_V, action="monitoring.missed_firings", purpose="see missed scheduled runs"
    ),
}
"""This router's rows of the policy table, registered at import (see `api/access_policy.py`, DEC-780)."""

register(POLICIES)

MAX_OUTCOME_FILE_BYTES: Final[int] = 256 * 1024 * 1024
"""An outcomes file larger than this is refused before it is parsed: it is two columns per scored row."""

_CHUNK_BYTES: Final[int] = 1024 * 1024
_MISSED_SCAN: Final[int] = 1000
"""How many of the newest missed firings a filtered listing reads before filtering by client/use case."""

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_UPLOAD_ERRORS: dict[int | str, dict[str, object]] = {
    **_ERRORS,
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
}

OutcomeFile = Annotated[
    UploadFile,
    File(description="CSV or Parquet: the run's primary key and the actual outcome, one row each."),
]
OutcomeColumnForm = Annotated[
    str | None,
    Form(max_length=128, description="Outcome column; defaults to the target the model was trained on."),
]
ClientQuery = Annotated[str | None, Query(max_length=128, description="Only this client's.")]
UseCaseQuery = Annotated[str | None, Query(max_length=128, description="Only this use case's.")]
KindQuery = Annotated[AlertKind | None, Query(description="Only alerts of this kind.")]
OpenQuery = Annotated[bool, Query(description="Only alerts nobody has acknowledged yet.")]
ScheduleQuery = Annotated[str | None, Query(max_length=128, description="Only this schedule's.")]
AlertLimitQuery = Annotated[int, Query(ge=1, le=1000, description="At most this many, newest first.")]
FiringLimitQuery = Annotated[int, Query(ge=1, le=500, description="At most this many, newest first.")]


def outcome_file_format(file_name: str | None) -> Literal["csv", "parquet"]:
    """`csv` or `parquet` from the file name's suffix; 415 `OUTCOME_FILE_FORMAT_UNSUPPORTED` otherwise."""
    suffix = PurePath(file_name or "").suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in (".parquet", ".pq"):
        return "parquet"
    raise http_error(
        415,
        "OUTCOME_FILE_FORMAT_UNSUPPORTED",
        "Upload the outcomes as a .csv or .parquet file.",
        path="file",
    )


def _outcome_error(exc: OutcomeError) -> Exception:
    return http_error(OUTCOME_ERROR_STATUS.get(exc.code, 500), exc.code, exc.message)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
@router.post(
    "/runs/{run_id}/outcomes",
    response_model=OutcomeReportResponse,
    status_code=201,
    responses={**_UPLOAD_ERRORS, 415: {"model": ErrorResponse}},
    summary="Add a scoring run's real outcomes, once its window has matured",
)
async def upload_outcomes(
    run_id: str, request: Request, file: OutcomeFile, outcome_column: OutcomeColumnForm = None
) -> OutcomeReport:
    """Join the file to the run's scores and measure the model on what really happened (DEC-771/772).

    The file is read in memory and never stored. Writes `outcome_report.json` - and, when the run
    held out a control group, `incrementality_input.json` - and raises a `performance_drop` alert
    when the drop exceeds the use case's `monitoring.performance_alert_drop_pct`.
    """
    file_format = outcome_file_format(file.filename)
    payload = bytearray()
    while chunk := await file.read(_CHUNK_BYTES):
        payload.extend(chunk)
        if len(payload) > MAX_OUTCOME_FILE_BYTES:
            raise http_error(
                413,
                "OUTCOME_FILE_TOO_LARGE",
                f"An outcomes file may be at most {MAX_OUTCOME_FILE_BYTES // (1024 * 1024)} MB.",
                path="file",
            )

    def ingest() -> OutcomeReport:
        return ingest_outcomes(
            run_id,
            bytes(payload),
            file_format=file_format,
            storage=get_storage(request),
            registry=get_registry(request),
            alerts=get_alert_sink(request),
            outcome_column=outcome_column,
            client_store=get_client_store(request),
            config_root=get_config_root(request),
            now=scheduling_clock(request)(),
        )

    try:
        # Parsing and measuring are blocking; off the event loop, like FastAPI runs a plain `def` route.
        report = await run_in_threadpool(ingest)
    except OutcomeError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise _outcome_error(exc) from None
    set_audit_context(
        request,
        details={
            "run_id": report.run_id,
            "use_case_id": report.use_case_id,
            "client_id": report.client_id,
            "model_id": report.model_version_id,
            "count": report.rows_matched,
            "reason_code": "PERFORMANCE_DROP" if report.alert_raised else None,
        },
    )
    return report


@router.get(
    "/runs/{run_id}/outcomes",
    response_model=OutcomeReportResponse,
    responses=_ERRORS,
    summary="A scoring run's real-world performance",
)
def read_outcomes(run_id: str, request: Request) -> OutcomeReport:
    """The stored `OutcomeReport`; 404 `OUTCOME_REPORT_NOT_FOUND` until outcomes were added."""
    storage = get_storage(request)
    load_run(storage, run_id)
    try:
        return read_outcome_report(storage, run_id)
    except OutcomeError as exc:
        raise _outcome_error(exc) from None


@router.get(
    "/runs/{run_id}/incrementality-input",
    response_model=IncrementalityInputResponse,
    responses=_ERRORS,
    summary="The treated-versus-control outcomes Plan B's incrementality report reads",
)
def read_incrementality_input(run_id: str, request: Request) -> IncrementalityInput:
    """Aggregates by group and band (DEC-773); 404 when the run had no control group or no outcomes yet."""
    storage = get_storage(request)
    load_run(storage, run_id)
    try:
        return storage.read_model(run_key(run_id, INCREMENTALITY_INPUT_FILENAME), IncrementalityInput)
    except StorageError:
        raise http_error(
            404,
            "INCREMENTALITY_INPUT_NOT_FOUND",
            "This run has no incrementality input: add its outcomes first, and it needs a control group.",
        ) from None


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
@router.get("/monitoring/alerts", response_model=AlertListResponse, responses=_ERRORS, summary="List alerts")
def list_alerts(
    request: Request,
    client_id: ClientQuery = None,
    use_case_id: UseCaseQuery = None,
    kind: KindQuery = None,
    unacknowledged_only: OpenQuery = False,
    limit: AlertLimitQuery = 100,
) -> AlertListResponse:
    """Drift, performance drops, failed and missed scheduled jobs - newest first."""
    query = AlertQuery(
        client_id=client_id,
        use_case_id=use_case_id,
        kind=kind,
        unacknowledged_only=unacknowledged_only,
        limit=limit,
    )
    return AlertListResponse(alerts=get_alert_store(request).query(query))


@router.post(
    "/monitoring/alerts/{alert_id}/acknowledge",
    response_model=AlertResponse,
    responses=_ERRORS,
    summary="Acknowledge an alert",
)
def acknowledge_alert(alert_id: str, request: Request, principal: PrincipalDep) -> Alert:
    """Record who is dealing with it. A second acknowledgement keeps the first's name and time."""
    store = get_alert_store(request)
    try:
        before = store.get(alert_id)
        after = store.acknowledge(alert_id, by=principal.user_id, now=scheduling_clock(request)())
    except AlertError as exc:
        raise http_error(404, exc.code, exc.message) from None
    set_audit_context(
        request,
        before_hash=content_hash(before.model_dump(mode="json")),
        details={
            "use_case_id": after.use_case_id,
            "client_id": after.client_id,
            "schedule_id": after.schedule_id,
            "run_id": after.run_id,
            "model_id": after.model_id,
        },
    )
    return after


# ---------------------------------------------------------------------------
# Missed runs
# ---------------------------------------------------------------------------
@router.get(
    "/monitoring/missed-firings",
    response_model=FiringListResponse,
    responses=_ERRORS,
    summary="Scheduled runs that were missed, across schedules",
)
def list_missed_firings(
    request: Request,
    schedule_id: ScheduleQuery = None,
    client_id: ClientQuery = None,
    use_case_id: UseCaseQuery = None,
    limit: FiringLimitQuery = 100,
) -> FiringListResponse:
    """Every `missed` firing, newest first: slots that passed while nothing was running (DEC-764).

    With a client or use-case filter, the newest `_MISSED_SCAN` missed firings are read and filtered.
    """
    filtered = client_id is not None or use_case_id is not None
    firings = get_schedule_store(request).list_firings(
        schedule_id=schedule_id, status=FiringStatus.MISSED, limit=_MISSED_SCAN if filtered else limit
    )
    matching = tuple(
        firing
        for firing in firings
        if (client_id is None or firing.client_id == client_id)
        and (use_case_id is None or firing.use_case_id == use_case_id)
    )
    return FiringListResponse(firings=matching[:limit])
