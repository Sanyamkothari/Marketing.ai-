"""Schedules: create, change, pause, delete and fire them - and the scheduler that fires them (Phase 4b M49).

The work is `engine.scheduling`'s: `service.py` decides what a valid schedule is and keeps the
scheduler in step with the table, `firing.py` decides what a firing does. This module translates a
request into one of those calls and a `ScheduleError` into the API's envelope, decides who may ask,
and owns the one thing an engine module cannot: the scheduler's life inside the API process.

**Who (DEC-780).** Reading schedules and their history is Viewer, like every other result. Creating,
changing, pausing, deleting and firing one is Analyst: a schedule is recurring scoring or training,
and starting that work by hand is `POST /runs`, which is Analyst (DEC-716). No schedule route is
Approver or Admin - a firing acts as `SYSTEM_SCHEDULER`, which holds Analyst only and can never
approve what it trains (DEC-760), so scheduling a retrain grants nothing the Analyst did not have.

**"Fire now" runs in the request, and is audited once (DEC-781).** `POST /schedules/{id}/fire` calls
`ScheduleFirer.fire(trigger=manual)` in FastAPI's worker thread and answers `201` with the firing as
recorded: `running` with its run id when a run was submitted, `succeeded` for a drift check that
finished, or `failed` with its `error_code` (the request itself succeeded - the failure is in the
firing, which also raised its `scheduled_job_failed` alert). The dataset build of a score or retrain
happens inside the request; the run itself goes to the job runner like any other. The firer is given
`audit_log=None`, so the engine writes no event of its own and the middleware's one event for the
request carries `firing_audit_details` through `set_audit_context` - one request, one event (M47).
Firings the scheduler starts on its own are audited by the engine as `system:scheduler`.

**The scheduler's life (DEC-782).** `install_scheduling` is a `PHASE_APP_HOOKS` entry that adds a
startup and a shutdown handler. At startup it reads `settings.scheduler_backend`:

* `none` (the default) - it does nothing at all: no thread, no database file, no AWS client. The
  test suite, and a laptop that never asked for scheduling, therefore pay nothing.
* `local` - it syncs the managed `monitoring.retraining` schedules, then starts `LocalScheduler`'s
  daemon thread; shutdown stops it.
* `eventbridge` - it syncs the managed schedules (which pushes them to EventBridge); AWS fires, so
  there is nothing to start.

A failure at startup is logged at ERROR and does not stop the API from serving: scheduling is one
feature, and every schedule route still answers (a sync failure is retried by the next start or by
`POST /schedules/retraining/sync`). The same `Scheduler` object is what the routes push to on
create, update and delete - `NullScheduler` and `LocalScheduler` accept and ignore the push (the
table is their only state), `EventBridgeScheduler` creates, updates or deletes the AWS schedule.

**One clock.** `app.state.scheduling_clock`, when a test sets one, is the clock of the scheduler, of
every firing, of `next_due_at` on create and update, of outcome windows and of acknowledgements, so
a test drives the whole of M49 through the app one deterministic step at a time.

**Settling on read.** A firing whose run is still going is `running` until something reads the run's
end. The local scheduler settles on every tick; with no local scheduler nothing else would, so
`GET /schedules/{id}/firings` settles first. Settling writes only what the run already decided
(and the `scheduled_job_failed` alert for a failed run); it is never audited as the reader's act.

**The managed retraining schedules are synced on demand as well as at startup (DEC-783).** `POST
/schedules/retraining/sync` syncs every recipe's; saving a labelled recipe syncs its own client and
use case through `sync_recipe_retraining` (DEC-867), so its `monitoring.retraining` schedule exists
at once rather than at the next restart. Both are idempotent, and neither touches a person's own
schedules.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, FastAPI, Query, Request, Response
from sqlalchemy.engine import Engine

from api.access import PrincipalDep, get_audit_log, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import get_config_root, get_jobs, get_registry, get_settings, get_storage
from api.routes.clients import get_client_store
from api.routes.uploads import http_error
from api.schemas import (
    ErrorResponse,
    FiringListResponse,
    FiringResponse,
    RetrainingSyncResponse,
    ScheduleCreateRequest,
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleUpdateRequest,
)
from engine.access.roles import Role
from engine.audit.events import content_hash
from engine.clients import ClientStore, ClientStoreError
from engine.onboarding.specs import OnboardingSpec
from engine.platform_db import platform_engine
from engine.scheduling.alerts import AlertSink, AlertStore, build_alert_sink
from engine.scheduling.firing import FiringServices, ScheduleFirer, firing_audit_details
from engine.scheduling.retraining import privacy_retrain_flags, retraining_targets, sync_retraining_schedules
from engine.scheduling.scheduler import (
    Clock,
    EventBridgeScheduler,
    LocalScheduler,
    Scheduler,
    build_scheduler,
)
from engine.scheduling.schedules import (
    FiringStatus,
    FiringTrigger,
    Schedule,
    ScheduleError,
    ScheduleFiring,
    ScheduleKind,
    ScheduleStore,
    SqlScheduleStore,
)
from engine.scheduling.service import ERROR_STATUS, create_schedule, delete_schedule, update_schedule
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

__all__ = [
    "CLOCK_SLOT",
    "POLICIES",
    "app_request",
    "get_alert_sink",
    "get_alert_store",
    "get_firer",
    "get_schedule_store",
    "get_scheduler",
    "get_scheduling_engine",
    "install_scheduling",
    "router",
    "schedule_error",
    "scheduling_clock",
]

_LOGGER = get_logger(__name__)

router: APIRouter = APIRouter(tags=["schedules"])

_V: Final[Role] = Role.VIEWER
_AN: Final[Role] = Role.ANALYST


def _schedule_policy(role: Role, action: str, purpose: str, *, with_id: bool = True) -> RoutePolicy:
    return RoutePolicy(
        role=role,
        action=action,
        purpose=purpose,
        object_type="schedule",
        object_param="schedule_id" if with_id else None,
    )


POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/schedules"): RoutePolicy(role=_V, action="schedules.list", purpose="see schedules"),
    ("POST", "/schedules"): _schedule_policy(_AN, "schedules.create", "create a schedule", with_id=False),
    ("GET", "/schedules/{schedule_id}"): _schedule_policy(_V, "schedules.read", "see a schedule"),
    ("PATCH", "/schedules/{schedule_id}"): _schedule_policy(_AN, "schedules.update", "change a schedule"),
    ("DELETE", "/schedules/{schedule_id}"): _schedule_policy(_AN, "schedules.delete", "delete a schedule"),
    ("POST", "/schedules/{schedule_id}/enable"): _schedule_policy(
        _AN, "schedules.enable", "resume a schedule"
    ),
    ("POST", "/schedules/{schedule_id}/disable"): _schedule_policy(
        _AN, "schedules.disable", "pause a schedule"
    ),
    ("POST", "/schedules/{schedule_id}/fire"): _schedule_policy(_AN, "schedules.fire", "run a schedule now"),
    ("GET", "/schedules/{schedule_id}/firings"): _schedule_policy(
        _V, "schedules.firings", "see a schedule's history"
    ),
    ("POST", "/schedules/retraining/sync"): _schedule_policy(
        _AN, "schedules.retraining_sync", "sync the retraining schedules", with_id=False
    ),
}
"""This router's rows of the policy table, registered at import (see `api/access_policy.py`, DEC-780)."""

register(POLICIES)

CLOCK_SLOT: Final[str] = "scheduling_clock"
"""`app.state` attribute a test sets to a clock it moves by hand; `utc_now` otherwise."""

_LOCK: Final[threading.Lock] = threading.Lock()
_ENGINE_SLOT: Final[str] = "scheduling_engine"
_STORE_SLOT: Final[str] = "schedule_store"
_SINK_SLOT: Final[str] = "alert_sink"
_ALERTS_SLOT: Final[str] = "alert_store"
_SCHEDULER_SLOT: Final[str] = "scheduler"
_FIRER_SLOT: Final[str] = "schedule_firer"

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}

_ERROR_PATHS: Final[dict[str, str]] = {
    "CRON_INVALID": "cadence",
    "CRON_NOT_PORTABLE": "cadence",
    "CRON_NEVER_FIRES": "cadence",
    "TIMEZONE_UNKNOWN": "timezone",
    "SCHEDULE_INVALID": "parameters",
    "ONBOARDING_SPEC_NOT_FOUND": "parameters.onboarding_spec_id",
    "ONBOARDING_SPEC_USE_CASE_MISMATCH": "parameters.onboarding_spec_id",
    "USE_CASE_NOT_FOUND": "use_case_id",
}
"""Which request field a refusal is about, so the UI can put the message beside it."""

KindQuery = Annotated[ScheduleKind | None, Query(description="Only schedules of this kind.")]
ClientQuery = Annotated[str | None, Query(max_length=128, description="Only this client's.")]
UseCaseQuery = Annotated[str | None, Query(max_length=128, description="Only this use case's.")]
StatusQuery = Annotated[FiringStatus | None, Query(description="Only firings in this status, e.g. `missed`.")]
LimitQuery = Annotated[int, Query(ge=1, le=500, description="At most this many, newest first.")]


# ---------------------------------------------------------------------------
# Providers (cached on app.state, like api/deps.py and api/access.py)
# ---------------------------------------------------------------------------
def app_request(app: FastAPI) -> Request:
    """A `Request` over `app` alone, so the startup handler reuses the providers routes use.

    Every provider reads `request.app.state` and nothing else of the request; a startup handler has
    an app but no request, and building the scheduler a second, different way there would let the
    two drift apart.
    """
    return Request(
        {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/", "query_string": b""}
    )


def _cached(request: Request, slot: str, build: Callable[[], Any]) -> Any:
    state = request.app.state
    existing = getattr(state, slot, None)
    if existing is not None:
        return existing
    fresh = build()  # built outside the lock: the providers it calls take api.deps' own lock
    with _LOCK:
        existing = getattr(state, slot, None)
        if existing is None:
            setattr(state, slot, fresh)
            return fresh
    return existing


def scheduling_clock(request: Request) -> Clock:
    """`app.state.scheduling_clock` when a test set one, else `utc_now`."""
    clock = getattr(request.app.state, CLOCK_SLOT, None)
    return clock if callable(clock) else utc_now


def get_scheduling_engine(request: Request) -> Engine:
    """The platform database the schedule, firing and alert tables live in (DEC-706).

    `create_app(data_dir=…)` wins, as for every other service; otherwise `platform_engine` decides
    (SQLite beside the artefacts, or the registry's Postgres, whose schema Alembic's 0004 owns).
    """

    def build() -> Engine:
        explicit = getattr(request.app.state, "data_dir", None)
        return platform_engine(
            get_settings(request), data_dir=None if explicit is None else Path(str(explicit))
        )

    engine: Engine = _cached(request, _ENGINE_SLOT, build)
    return engine


def get_schedule_store(request: Request) -> ScheduleStore:
    """The schedule and firing tables."""
    store: ScheduleStore = _cached(
        request, _STORE_SLOT, lambda: SqlScheduleStore(get_scheduling_engine(request))
    )
    return store


def get_alert_sink(request: Request) -> AlertSink:
    """Where alerts go: the `alert` table, and SNS when `alert_backend=sns`."""

    def build() -> AlertSink:
        return build_alert_sink(get_settings(request), engine=get_scheduling_engine(request))

    sink: AlertSink = _cached(request, _SINK_SLOT, build)
    return sink


def get_alert_store(request: Request) -> AlertStore:
    """The alert history, for the monitoring page to read and acknowledge."""
    alerts: AlertStore = _cached(request, _ALERTS_SLOT, lambda: AlertStore(get_scheduling_engine(request)))
    return alerts


def _services(request: Request, *, audited: bool) -> FiringServices:
    """What a firing touches: the API's own storage, registry, jobs, configuration and client store.

    `audited=True` gives the engine the audit log, for firings no request is behind (the scheduler's);
    `False` is for "fire now", whose one event is the middleware's (DEC-781).
    """
    engine = get_scheduling_engine(request)
    settings = get_settings(request)
    return FiringServices(
        store=get_schedule_store(request),
        storage=get_storage(request),
        registry=get_registry(request),
        jobs=get_jobs(request),
        config_root=get_config_root(request),
        alerts=get_alert_sink(request),
        audit_log=get_audit_log(request) if audited else None,
        client_store=get_client_store(request),
        retrain_flags=privacy_retrain_flags(engine),
        clock=scheduling_clock(request),
        job_client_tag=settings.client_id,
    )


def get_firer(request: Request) -> ScheduleFirer:
    """The scheduler's firer: audited by the engine, as `system:scheduler`."""
    firer: ScheduleFirer = _cached(
        request, _FIRER_SLOT, lambda: ScheduleFirer(_services(request, audited=True))
    )
    return firer


def _manual_firer(request: Request) -> ScheduleFirer:
    """The firer "fire now" uses: the scheduler's services without the engine's own audit event."""
    return ScheduleFirer(replace(get_firer(request).services, audit_log=None))


def get_scheduler(request: Request) -> Scheduler:
    """The scheduler `settings.scheduler_backend` names; one per app, started by the startup hook."""

    def build() -> Scheduler:
        settings = get_settings(request)
        firer = get_firer(request) if settings.scheduler_backend == "local" else None
        built = build_scheduler(
            settings, store=get_schedule_store(request), firer=firer, clock=scheduling_clock(request)
        )
        if isinstance(built, LocalScheduler):
            built.housekeeping = _audit_export_housekeeping(request)
        return built

    scheduler: Scheduler = _cached(request, _SCHEDULER_SLOT, build)
    return scheduler


def _audit_export_housekeeping(request: Request) -> Callable[[datetime], object]:
    """The local scheduler's per-tick housekeeping: the scheduled audit export when one is due."""
    from api.access import platform_data_dir
    from engine.audit.export import export_due, export_sink_for

    def housekeeping(now: datetime) -> object:
        sink = export_sink_for(get_settings(request), platform_data_dir(request))
        return export_due(get_audit_log(request), sink, now=now)

    return housekeeping


def schedule_error(exc: ScheduleError) -> Exception:
    """A `ScheduleError` as the API's envelope, with the status `ERROR_STATUS` gives its code."""
    return http_error(ERROR_STATUS.get(exc.code, 500), exc.code, exc.message, path=_ERROR_PATHS.get(exc.code))


# ---------------------------------------------------------------------------
# The scheduler's life in the API process (DEC-782)
# ---------------------------------------------------------------------------
def _sync_managed(
    request: Request, scheduler: Scheduler, *, only: tuple[str, str] | None = None
) -> tuple[int, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Sync the managed retraining schedules; `only` narrows it to one `(client_id, use_case_id)`."""
    targets = retraining_targets(get_client_store(request), get_config_root(request))
    if only is not None:
        targets = tuple(target for target in targets if (target.client_id, target.use_case_id) == only)
    result = sync_retraining_schedules(
        get_schedule_store(request), scheduler, targets, now=scheduling_clock(request)()
    )
    return len(targets), result.created, result.updated, result.removed


def sync_recipe_retraining(request: Request, spec: OnboardingSpec) -> None:
    """After a recipe is saved: its client x use case's managed retraining schedules, now (DEC-867).

    Phase 2's save routes (`POST /clients/{id}/onboarding-specs` and `.../replay`) call this, so a
    labelled recipe's `monitoring.retraining` schedule exists at once rather than at the next start
    (DEC-783). It is the code `POST /schedules/retraining/sync` runs, narrowed to the recipe's own
    client and use case, so saving one recipe pushes nothing for anybody else's.

    Nothing to do for a recipe with no label (it trains nothing, so `retraining_targets` never lists
    it) or for `scheduler_backend=none`, where startup syncs nothing either and saving a recipe
    touches no scheduling table (DEC-782). Best-effort: a failure is logged with its class name only
    and the save stands; the next start or `POST /schedules/retraining/sync` repairs it.
    """
    if spec.label_spec is None:
        return
    try:
        if get_settings(request).scheduler_backend == "none":
            return
        targets, created, updated, removed = _sync_managed(
            request, get_scheduler(request), only=(spec.client_id, spec.use_case)
        )
        _LOGGER.info(
            "scheduling.recipe_retraining_sync targets=%d created=%d updated=%d removed=%d",
            targets,
            len(created),
            len(updated),
            len(removed),
        )
    except Exception as exc:  # never fails the save; retried by the next start or by the sync route
        log_failure(_LOGGER, "scheduling.recipe_retraining_sync", exc, level=logging.ERROR)


def start_scheduling(app: FastAPI) -> None:
    """Startup: nothing for `none`; sync the managed schedules, then start the thread for `local`."""
    request = app_request(app)
    try:
        backend = get_settings(request).scheduler_backend
    except Exception as exc:  # the first request will report the settings problem with its envelope
        log_failure(_LOGGER, "scheduling.start settings", exc)
        return
    if backend == "none":
        return
    try:
        scheduler = get_scheduler(request)
    except Exception as exc:  # e.g. eventbridge without its ARNs: the API still serves everything else
        log_failure(_LOGGER, f"scheduling.start backend={backend}", exc, level=logging.ERROR)
        return
    try:
        targets, created, updated, removed = _sync_managed(request, scheduler)
        _LOGGER.info(
            "scheduling.retraining_sync targets=%d created=%d updated=%d removed=%d",
            targets,
            len(created),
            len(updated),
            len(removed),
        )
    except Exception as exc:  # retried by the next start or by POST /schedules/retraining/sync
        log_failure(_LOGGER, "scheduling.retraining_sync", exc, level=logging.ERROR)
    if isinstance(scheduler, EventBridgeScheduler):
        try:
            scheduler.ensure_housekeeping()  # the hourly sweep and audit export (DEC-775, DEC-726)
        except Exception as exc:  # retried by the next start
            log_failure(_LOGGER, "scheduling.housekeeping", exc, level=logging.ERROR)
    scheduler.start()
    _LOGGER.info("scheduling.started backend=%s", scheduler.backend)


def stop_scheduling(app: FastAPI) -> None:
    """Shutdown: stop whatever the startup started (only `LocalScheduler` has anything to stop)."""
    scheduler = getattr(app.state, _SCHEDULER_SLOT, None)
    if scheduler is None:
        return
    scheduler.stop()
    if isinstance(scheduler, LocalScheduler):
        _LOGGER.info("scheduling.stopped backend=local")


def install_scheduling(app: FastAPI) -> None:
    """`PHASE_APP_HOOKS` entry: start the scheduler with the app and stop it with the app (DEC-782)."""
    app.router.on_startup.append(lambda: start_scheduling(app))
    app.router.on_shutdown.append(lambda: stop_scheduling(app))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load(request: Request, schedule_id: str) -> Schedule:
    try:
        return get_schedule_store(request).get(schedule_id)
    except ScheduleError as exc:
        raise schedule_error(exc) from None


def _schedule_hash(schedule: Schedule) -> str | None:
    return content_hash(schedule.model_dump(mode="json"))


def _audit_schedule(request: Request, schedule: Schedule, *, before: Schedule | None = None) -> None:
    set_audit_context(
        request,
        object_id=schedule.schedule_id,
        before_hash=None if before is None else _schedule_hash(before),
        details={
            "schedule_id": schedule.schedule_id,
            "schedule_kind": schedule.kind.value,
            "use_case_id": schedule.use_case_id,
            "client_id": schedule.client_id,
            "dataset_id": schedule.parameters.dataset_id,
        },
    )


def _client_for(body: ScheduleCreateRequest, clients: ClientStore) -> str | None:
    """The body's client (which must exist), else the client whose recipe the schedule rebuilds."""
    if body.client_id is not None:
        try:
            clients.get_client(body.client_id)
        except ClientStoreError:
            raise http_error(
                404, "CLIENT_NOT_FOUND", f"No client with id {body.client_id!r}.", path="client_id"
            ) from None
        return body.client_id
    spec_id = body.parameters.onboarding_spec_id
    if spec_id is None:
        return None
    try:
        return clients.get_spec(spec_id).client_id
    except ClientStoreError:
        raise http_error(
            404,
            "ONBOARDING_SPEC_NOT_FOUND",
            f"No onboarding recipe with id {spec_id!r}.",
            path="parameters.onboarding_spec_id",
        ) from None


def _update(request: Request, schedule_id: str, **changes: Any) -> Schedule:
    before = _load(request, schedule_id)
    try:
        changed = update_schedule(
            get_schedule_store(request),
            get_scheduler(request),
            schedule_id,
            client_store=get_client_store(request),
            now=scheduling_clock(request)(),
            **changes,
        )
    except ScheduleError as exc:
        _audit_schedule(request, before)
        set_audit_context(request, details={"reason_code": exc.code})
        raise schedule_error(exc) from None
    _audit_schedule(request, changed, before=before)
    return changed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/schedules", response_model=ScheduleListResponse, responses=_ERRORS, summary="List schedules")
def list_schedules(
    request: Request,
    client_id: ClientQuery = None,
    use_case_id: UseCaseQuery = None,
    kind: KindQuery = None,
) -> ScheduleListResponse:
    """Every schedule matching the filters, oldest first - people's own and the managed ones."""
    store = get_schedule_store(request)
    return ScheduleListResponse(schedules=store.list(client_id=client_id, use_case_id=use_case_id, kind=kind))


@router.post(
    "/schedules",
    response_model=ScheduleResponse,
    status_code=201,
    responses=_ERRORS,
    summary="Create a schedule",
)
def create_schedule_endpoint(
    body: ScheduleCreateRequest, request: Request, principal: PrincipalDep
) -> Schedule:
    """Validate, store and register a new schedule; its first due slot is `next_due_at`.

    A `score` schedule needs a recipe (rebuilt against the client's latest tables at every firing)
    or a fixed dataset. The client defaults to the recipe's. `502 SCHEDULER_SYNC_FAILED` means
    EventBridge refused it, and nothing was kept.
    """
    clients = get_client_store(request)
    client_id = _client_for(body, clients)
    set_audit_context(
        request,
        details={"schedule_kind": body.kind.value, "use_case_id": body.use_case_id, "client_id": client_id},
    )
    try:
        schedule = create_schedule(
            get_schedule_store(request),
            get_scheduler(request),
            config_root=get_config_root(request),
            client_id=client_id,
            use_case_id=body.use_case_id,
            kind=body.kind,
            cadence=body.cadence,
            created_by=principal.user_id,
            timezone=body.timezone,
            parameters=body.parameters,
            enabled=body.enabled,
            client_store=clients,
            now=scheduling_clock(request)(),
        )
    except ScheduleError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise schedule_error(exc) from None
    _audit_schedule(request, schedule)
    return schedule


@router.get(
    "/schedules/{schedule_id}", response_model=ScheduleResponse, responses=_ERRORS, summary="One schedule"
)
def read_schedule(schedule_id: str, request: Request) -> Schedule:
    """The schedule, with when it is next due and when it last fired (UTC)."""
    return _load(request, schedule_id)


@router.patch(
    "/schedules/{schedule_id}",
    response_model=ScheduleResponse,
    responses=_ERRORS,
    summary="Change a schedule's cadence, timezone, data or state",
)
def update_schedule_endpoint(schedule_id: str, body: ScheduleUpdateRequest, request: Request) -> Schedule:
    """Change what was given. A managed schedule can only be paused or resumed (`SCHEDULE_MANAGED`)."""
    return _update(
        request,
        schedule_id,
        cadence=body.cadence,
        timezone=body.timezone,
        enabled=body.enabled,
        parameters=body.parameters,
    )


@router.post(
    "/schedules/{schedule_id}/enable",
    response_model=ScheduleResponse,
    responses=_ERRORS,
    summary="Resume a schedule",
)
def enable_schedule(schedule_id: str, request: Request) -> Schedule:
    """Resume firing. The next slot is counted from now, so a paused month leaves no missed runs."""
    return _update(request, schedule_id, enabled=True)


@router.post(
    "/schedules/{schedule_id}/disable",
    response_model=ScheduleResponse,
    responses=_ERRORS,
    summary="Pause a schedule",
)
def disable_schedule(schedule_id: str, request: Request) -> Schedule:
    """Stop firing; the schedule and its history are kept. Managed schedules can be paused too."""
    return _update(request, schedule_id, enabled=False)


@router.delete(
    "/schedules/{schedule_id}",
    status_code=204,
    response_class=Response,
    responses=_ERRORS,
    summary="Delete a schedule",
)
def delete_schedule_endpoint(schedule_id: str, request: Request) -> Response:
    """Remove the scheduler's registration, then the schedule. Its firing history is kept."""
    before = _load(request, schedule_id)
    _audit_schedule(request, before)
    set_audit_context(request, before_hash=_schedule_hash(before))
    try:
        delete_schedule(get_schedule_store(request), get_scheduler(request), schedule_id)
    except ScheduleError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise schedule_error(exc) from None
    return Response(status_code=204)


@router.post(
    "/schedules/{schedule_id}/fire",
    response_model=FiringResponse,
    status_code=201,
    responses=_ERRORS,
    summary="Run a schedule's work now",
)
def fire_schedule(schedule_id: str, request: Request) -> ScheduleFiring:
    """Fire once, now, as a `manual` firing - also for a paused schedule (DEC-781).

    Answers with the firing as recorded: `running` with its `run_id` while the run goes on,
    `succeeded`, or `failed` with an `error_code` (and a `scheduled_job_failed` alert). A manual
    firing claims no slot, so it never stops the next scheduled one.
    """
    schedule = _load(request, schedule_id)
    firing = _manual_firer(request).fire(schedule, trigger=FiringTrigger.MANUAL)
    if firing is None:  # a manual firing has no slot to lose; kept for the protocol's honesty
        raise http_error(409, "FIRING_SLOT_TAKEN", "This schedule is being fired already. Try again shortly.")
    set_audit_context(request, details=firing_audit_details(firing))
    return firing


@router.get(
    "/schedules/{schedule_id}/firings",
    response_model=FiringListResponse,
    responses=_ERRORS,
    summary="A schedule's firing history, newest first",
)
def list_firings(
    schedule_id: str, request: Request, status: StatusQuery = None, limit: LimitQuery = 100
) -> FiringListResponse:
    """Every firing of the schedule, including `missed` slots; running firings are settled first."""
    _load(request, schedule_id)
    try:
        get_firer(request).settle()
    except Exception as exc:  # a run record that cannot be read must not hide the history
        log_failure(_LOGGER, "schedules.settle", exc)
    firings = get_schedule_store(request).list_firings(schedule_id=schedule_id, status=status, limit=limit)
    return FiringListResponse(firings=firings)


@router.post(
    "/schedules/retraining/sync",
    response_model=RetrainingSyncResponse,
    responses=_ERRORS,
    summary="Create or remove the schedules monitoring.retraining asks for",
)
def sync_retraining(request: Request) -> RetrainingSyncResponse:
    """Make the managed retraining schedules match every recipe's use-case setting now (DEC-783)."""
    try:
        targets, created, updated, removed = _sync_managed(request, get_scheduler(request))
    except ScheduleError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise schedule_error(exc) from None
    except Exception as exc:  # the scheduler's own failure (EventBridge); the table keeps what was written
        log_failure(_LOGGER, "schedules.retraining_sync", exc, level=logging.ERROR)
        set_audit_context(request, details={"reason_code": "SCHEDULER_SYNC_FAILED"})
        raise http_error(
            502,
            "SCHEDULER_SYNC_FAILED",
            "The retraining schedules could not all be registered with the scheduler. Try again; the "
            "sync is safe to repeat.",
        ) from None
    set_audit_context(request, details={"count": len(created) + len(updated) + len(removed)})
    return RetrainingSyncResponse(targets=targets, created=created, updated=updated, removed=removed)
