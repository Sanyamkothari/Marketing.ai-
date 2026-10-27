"""`/v2/inbox` and `/v2/autopilot`: what needs a person, its one button, and Shadow -> Autopilot.

| Route | What it does |
|---|---|
| `GET /v2/inbox` | The open items (one sentence, the recommended action, one button each), worst first, and this week's count with the quiet "Handled without you" line |
| `POST /v2/inbox/{item_id}/act` | Press the item's button: turn on Autopilot, pause, resume in Approve, sync now, trust the data, mark as read. An `open` button answers with where to go |
| `GET /v2/inbox/history?weeks=8` | Items a person had to look at, per week (the founder's 0-2 target) |
| `GET /v2/inbox/data` | Everything the data watchdog sees, notes included (the Data Doctor's quiet view) |
| `GET /v2/autopilot/{goal_id}` | Has the goal earned Autopilot: "it would have made about $X a month ($low to $high)", the checks, the rule |
| `POST /v2/autopilot/{goal_id}/turn-on` | The one click (409 with the reason when the bar is not met) |
| `GET /v2/autopilot/ledger` | Every autonomy change, hash-chained, and whether the chain verifies |

The routes read and act through a `ZeroTouch` (`autonomy.inbox.install`) attached to the app
(`attach_inbox_services`). The local demo builds one over its own runtime and installs its tick
hook, so `POST /v2/runtime/tick` keeps the inbox current; production builds it over the database
the worker writes (`production_inbox_services`), where the worker's hook does the syncing.

The routes are published in `docs/v2/openapi.json` (`IN_SCHEMA`), and `tests/v2/api/test_api.py`'s
route list names them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime, Field
from starlette.applications import Starlette

from autonomy.contracts import Alert, BriefingSection, Contract
from autonomy.inbox.briefing import quiet_week
from autonomy.inbox.conditions import InboxSources
from autonomy.inbox.inbox import Inbox
from autonomy.inbox.install import ZeroTouch, install, zero_touch_for_runtime
from autonomy.inbox.items import InboxItem
from autonomy.inbox.store import InMemoryInboxStore
from autonomy.inbox.watchdog import DataWatchdog, Finding
from autonomy.runtime.promotion import AutonomyChange, AutopilotGuard, InMemoryAutonomyLedger, Readiness

__all__ = [
    "IN_SCHEMA",
    "InboxServices",
    "attach_inbox_services",
    "autopilot_router",
    "local_inbox_services",
    "production_inbox_services",
    "router",
]

IN_SCHEMA = True
PRODUCTION_SYNC_EVERY = timedelta(minutes=5)

router = APIRouter(prefix="/v2/inbox", tags=["inbox"], include_in_schema=IN_SCHEMA)
autopilot_router = APIRouter(prefix="/v2/autopilot", tags=["autopilot"], include_in_schema=IN_SCHEMA)


@dataclass
class InboxServices:
    zero_touch: ZeroTouch
    clock: Callable[[], datetime]
    alerts: Callable[[], Sequence[Alert]] | None = None
    """Every alert (newest first), for the weekly quiet line; None: the sources' alert store."""
    sync_every: timedelta | None = None
    """Reconcile on a read when the last sync is older than this (production: a fallback while no
    worker hook runs); None: only the runtime's tick hook, or `?refresh=true`, syncs."""
    last_sync: datetime | None = None

    def now(self) -> datetime:
        return self.clock()

    def sync_due(self, now: datetime) -> bool:
        if self.sync_every is None:
            return False
        return self.last_sync is None or not timedelta(0) <= now - self.last_sync < self.sync_every


def attach_inbox_services(app: Starlette, services: InboxServices) -> None:
    app.state.inbox_services = services


def get_inbox_services(request: Request) -> InboxServices:
    services = getattr(request.app.state, "inbox_services", None)
    if not isinstance(services, InboxServices):
        raise HTTPException(status_code=503, detail="The inbox is not set up on this server.")
    return services


ServicesDep = Annotated[InboxServices, Depends(get_inbox_services)]


# --- response bodies --------------------------------------------------------------------------------


class InboxView(Contract):
    as_of: AwareDatetime
    items: list[InboxItem]
    count: int = Field(ge=0)
    this_week: int = Field(ge=0, description="Items a person had to look at in the last 7 days")
    quiet: BriefingSection = Field(description="This week's line for the weekly briefing")
    words: str


class ActRequest(Contract):
    by: str = Field(default="owner", min_length=1, max_length=200)


class ActView(Contract):
    item: InboxItem
    done: bool
    words: str
    target: str | None = None


class WeekView(Contract):
    week_start: AwareDatetime
    items: int = Field(ge=0)
    causes: list[str]


class HistoryView(Contract):
    as_of: AwareDatetime
    weeks: list[WeekView]
    average: float = Field(ge=0)
    words: str


class DataView(Contract):
    as_of: AwareDatetime
    trusted: bool
    findings: list[Finding]
    words: str


class LedgerView(Contract):
    entries: list[AutonomyChange]
    chain_ok: bool
    problem: str = ""


# --- routes -----------------------------------------------------------------------------------------


@router.get("", response_model=InboxView, summary="What needs a person, worst first")
def inbox(services: ServicesDep, refresh: bool = False) -> InboxView:
    now = services.now()
    zt = services.zero_touch
    if refresh or services.sync_due(now):
        zt.tick(now)
        services.last_sync = now
    items = zt.inbox.open_items()
    week_start = now - timedelta(days=7)
    seen = zt.inbox.appearances(week_start, now + timedelta(microseconds=1))
    lines = zt.inbox.store.appearances(
        zt.inbox.business_id, since=week_start, until=now + timedelta(microseconds=1)
    )
    quiet = quiet_week(_alerts(services, week_start), lines, week_start, now + timedelta(microseconds=1))
    if not items:
        words = "Nothing needs you."
    elif len(items) == 1:
        words = "One thing needs you."
    else:
        words = f"{len(items)} things need you."
    return InboxView(as_of=now, items=items, count=len(items), this_week=len(seen), quiet=quiet, words=words)


@router.post(
    "/{item_id}/act",
    response_model=ActView,
    responses={404: {"description": "No such item"}, 409: {"description": "It cannot be done now"}},
    summary="Press the item's one button",
)
def act(item_id: str, services: ServicesDep, body: ActRequest | None = None) -> ActView:
    now = services.now()
    who = (body or ActRequest()).by
    try:
        result = services.zero_touch.act(item_id, now, by=who)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no inbox item {item_id!r}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActView(item=result.item, done=result.done, words=result.words, target=result.target)


@router.get("/history", response_model=HistoryView, summary="Items a person had to look at, per week")
def history(services: ServicesDep, weeks: Annotated[int, Query(ge=1, le=52)] = 8) -> HistoryView:
    now = services.now()
    start = now - timedelta(days=7 * weeks)
    tick = timedelta(microseconds=1)  # the windows are [start, end): this includes `now`
    counts = services.zero_touch.inbox.weekly_counts(start + tick, now + tick)
    rows = [WeekView(week_start=c.week_start, items=c.appearances, causes=list(c.causes)) for c in counts]
    average = sum(r.items for r in rows) / len(rows) if rows else 0.0
    return HistoryView(
        as_of=now,
        weeks=rows,
        average=round(average, 2),
        words=f"{average:.1f} items a week on average over the last {len(rows)} weeks.",
    )


@router.get("/data", response_model=DataView, summary="What the data watchdog sees, notes included")
def data(services: ServicesDep) -> DataView:
    now = services.now()
    watchdog = services.zero_touch.watchdog
    if watchdog is None:
        return DataView(as_of=now, trusted=True, findings=[], words="The data watchdog is not running here.")
    found = watchdog.findings(now)
    trusted = not any(f.level == "hold" for f in found)
    if not found:
        words = "The data looks like the shop's own normal."
    elif trusted:
        words = "The data can be trusted; a few notes for the weekly briefing."
    else:
        words = "Some data should not be trusted yet, so decisions wait."
    return DataView(as_of=now, trusted=trusted, findings=found, words=words)


@autopilot_router.get(
    "/ledger",
    response_model=LedgerView,
    summary="Every autonomy change, hash-chained, and whether it verifies",
)
def ledger(services: ServicesDep) -> LedgerView:
    guard = _guard(services)
    report = guard.ledger.verify_chain()
    return LedgerView(entries=guard.ledger.entries(), chain_ok=report.ok, problem=report.problem)


@autopilot_router.get(
    "/{goal_id}",
    response_model=Readiness,
    responses={404: {"description": "No such goal, or it is already on Autopilot or not running"}},
    summary="Has the goal earned Autopilot",
)
def readiness(goal_id: str, services: ServicesDep) -> Readiness:
    guard = _guard(services)
    if guard.goal(goal_id) is None:
        raise HTTPException(status_code=404, detail=f"no goal {goal_id!r}")
    r = guard.readiness(goal_id, services.now())
    if r is None:
        raise HTTPException(status_code=404, detail="This goal is already on Autopilot, or not running.")
    return r


@autopilot_router.post(
    "/{goal_id}/turn-on",
    response_model=AutonomyChange,
    responses={404: {"description": "No such goal"}, 409: {"description": "The bar is not met"}},
    summary="Turn on Autopilot (one click)",
)
def turn_on(goal_id: str, services: ServicesDep, body: ActRequest | None = None) -> AutonomyChange:
    guard = _guard(services)
    now = services.now()
    try:
        change = guard.promote(goal_id, now, by=(body or ActRequest()).by)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no goal {goal_id!r}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    services.zero_touch.tick(now)  # the offer clears from the inbox at once
    return change


def _guard(services: InboxServices) -> AutopilotGuard:
    guard = services.zero_touch.guard
    if guard is None:
        raise HTTPException(status_code=503, detail="Autopilot promotion is not set up on this server.")
    return guard


def _alerts(services: InboxServices, since: datetime) -> list[Alert]:
    if services.alerts is not None:
        return [a for a in services.alerts() if a.at >= since]
    reader = services.zero_touch.sources.alerts
    return [] if reader is None else reader.list_alerts(since=since)


# --- building the services ---------------------------------------------------------------------------


class _ProviderAlerts:
    """An alert reader over the API's `alerts()` provider (the fake demo has no alert store)."""

    def __init__(self, provider: Callable[[], Sequence[Alert]]) -> None:
        self.provider = provider

    def list_alerts(
        self, *, goal_id: str | None = None, since: datetime | None = None, limit: int | None = None
    ) -> list[Alert]:
        rows = [
            a
            for a in self.provider()
            if (goal_id is None or a.goal_id in (goal_id, None)) and (since is None or a.at >= since)
        ]
        rows.sort(key=lambda a: (a.at, a.alert_id), reverse=True)
        return rows if limit is None else rows[:limit]


def local_inbox_services(services: Any) -> InboxServices:
    """For the local demo app: in memory, over the demo's runtime when it has one (hook installed, so
    each operator tick keeps the inbox current), else over its stores. Synced once at start."""
    runtime = getattr(services.runtime, "inner", services.runtime)
    clock: Callable[[], datetime] = services.clock
    if runtime is not None and hasattr(runtime, "before_tick") and hasattr(runtime, "workspace"):
        zt = zero_touch_for_runtime(runtime, business_id=services.business_id)
        install(runtime, zt)
    else:
        store = InMemoryInboxStore()
        inbox_ = Inbox(store, services.business_id)
        alerts = _ProviderAlerts(services.alerts)
        watchdog = DataWatchdog(services.business_id, services.events, states=services.states)
        guard = AutopilotGuard(
            business_id=services.business_id,
            workspace=services.workspace,
            decisions=services.decisions,
            events=services.events,
            ledger=InMemoryAutonomyLedger(),
            alerts=alerts,
            data_trusted=watchdog.trusted,
            require_evidence=True,
        )
        zt = ZeroTouch(
            inbox=inbox_,
            sources=InboxSources(
                business_id=services.business_id,
                workspace=services.workspace,
                alerts=alerts,
                decisions=services.decisions,
                events=services.events,
                guard=guard,
                watchdog=watchdog,
                handled_at=inbox_.handled_at,
            ),
            guard=guard,
            watchdog=watchdog,
        )
    at = clock()
    zt.tick(at)
    _audit_to_ledger(services, zt)
    return InboxServices(zero_touch=zt, clock=clock, alerts=services.alerts, last_sync=at)


def _audit_to_ledger(services: Any, zt: Any) -> None:
    """Also write the evidence gate's audit entries (`Services.audit_log`) to the autonomy ledger."""
    guard = getattr(zt, "guard", None)
    if guard is None:
        return

    def sink(entry: Any) -> None:
        guard.record(
            entry.goal_id,
            entry.at,
            from_level=entry.from_level,
            to_level=entry.to_level,
            actor=f"person:{entry.actor}",
            reason=entry.reason,
            words=entry.words,
            evidence=entry.evidence,
        )

    services.audit_sink = sink


def production_inbox_services(services: Any, *, engine: Any, wiring: Any = None) -> InboxServices:
    """Over the production database the worker writes: the API reads, and acts on a person's click.

    The worker installs the same `ZeroTouch` on its runtime (`autonomy.inbox.install.install`), whose
    tick hook keeps the items current; this process never syncs on its own schedule."""
    runtime = getattr(services.runtime, "inner", services.runtime)
    zt = zero_touch_for_runtime(runtime, engine=engine, wiring=wiring, business_id=services.business_id)
    # A channel paused by a breaker lives in the worker's memory, not this process's: this runtime's
    # set is always empty. Reading it would hide every channel item (and resolve the worker's, which
    # then never reopens); resuming it would say "resumed" while the worker keeps the channel paused.
    zt.sources.paused_channels = None
    zt.resume_channel = None
    monitor = getattr(getattr(wiring, "engine", None), "monitor", None)
    if monitor is not None:
        monitor.watchdog = zt.watchdog
    _audit_to_ledger(services, zt)
    return InboxServices(
        zero_touch=zt, clock=services.clock, alerts=services.alerts, sync_every=PRODUCTION_SYNC_EVERY
    )
