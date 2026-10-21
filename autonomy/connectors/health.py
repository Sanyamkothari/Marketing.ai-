"""Connection health: is the data fresh enough to act on, and has a person been told exactly once?

**Per stream** (from the positions and the run log in `ConnectorStore`):

* *freshness*: time since the live lane last finished a window, against the stream's cadence.
  Late after 1.5 cadences, stale after 3 (plus a 15-minute grace). Never synced counts as stale.
* *history*: how far the backfill has walked back; decisions may rely on a stream once it covers
  `StreamSpec.min_history_days`.
* *row-count drop*: rows read over the last 24 hours against the median of the 7 days before.
  Dashboards stay green while a sync "succeeds" with no rows; this is what catches it.
* *schema drift*: the share of records in the last run missing a field the connector relies on.
* *error streak*: consecutive failed runs.

**Per business**: `HealthMonitor.ready_to_act(business_id, now)` is the single answer the runtime
asks before deciding: every connection with a critical stream (orders, consent: `StreamSpec.critical`)
must be active, fresh and have enough history. Otherwise decisions for that business are held,
and the verdict says why in plain words. A connection a person disconnected
(`sync.DISCONNECTED_BY_PERSON`) is left out: disconnecting is a choice, so it stops holding
anything at once. One that needs reconnecting, or whose app was uninstalled on the platform, holds
until it is reconnected or a person removes it on Connect. An optional stream refused with 403
(`ALERT_STREAM_DENIED`) is degraded, never a hold. `send_gate(business_id)` wraps it as the worker's
`SendGate`, so a message queued before the data went stale is held too.

**The data watchdog** (`HealthMonitor.watchdog`, a `WatchdogCheck` such as
`autonomy.inbox.watchdog.DataWatchdog`) reads what actually arrived, not whether the sync ran: a
missing day of orders, a customer id that changed form, a consent jump. Each of its holds is one
more reason in `ready_to_act`, so the same gate holds decisions and queued sends. A watchdog that
raises does not hold (the freshness checks above still do); the verdict carries `watchdog_error`.

**Alerts** go through `IncidentDesk`: one open incident per (connection, problem), opened
atomically (`ConnectorStore.open_incident_once`), so a problem noticed by every hourly check and by
two workers still raises one `Alert`. A root cause hides its symptoms: while a connection needs
reconnecting (or was uninstalled) nothing else is raised for it, and a stale stream hides its own
error streak. An incident closes when its problem clears; if it comes back later it alerts again.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from autonomy.connectors.base import Connection, ConnectionStatus, Lane, StreamSpec
from autonomy.connectors.registry import ConnectorRegistry
from autonomy.connectors.store import ConnectorStore, Incident, SyncPosition, SyncRun
from autonomy.contracts import Alert, Decision, stable_id

__all__ = [
    "ALERT_DISCONNECT",
    "ALERT_DRIFT",
    "ALERT_ERRORS",
    "ALERT_PRIVACY",
    "ALERT_RECONNECT",
    "ALERT_ROW_DROP",
    "ALERT_STALE",
    "ALERT_STREAM_DENIED",
    "ALERT_UNINSTALLED",
    "ALERT_WEBHOOKS",
    "ActVerdict",
    "AlertSink",
    "ConnectionHealth",
    "Freshness",
    "HealthMonitor",
    "HealthPolicy",
    "IncidentDesk",
    "Problem",
    "StreamHealth",
    "WatchdogCheck",
]

ALERT_RECONNECT = "connector.needs_reconnect"
ALERT_UNINSTALLED = "connector.uninstalled"
ALERT_STALE = "connector.stale"
ALERT_ROW_DROP = "connector.row_drop"
ALERT_DRIFT = "connector.schema_drift"
ALERT_ERRORS = "connector.errors"
ALERT_WEBHOOKS = "connector.webhooks"
ALERT_PRIVACY = "connector.privacy_request"
ALERT_STREAM_DENIED = "connector.stream_denied"
ALERT_DISCONNECT = "connector.disconnect_followup"
ACCESS_DENIED_CODE = "connector.access_denied"
"""`base.AccessDenied.code` (not imported: the error classes stay in `base`, the words here)."""
PERSON_DISCONNECT_REASON = "disconnected by a person"
"""`sync.DISCONNECTED_BY_PERSON` (sync imports this module)."""

ROOT_CAUSES: frozenset[str] = frozenset({ALERT_RECONNECT, ALERT_UNINSTALLED})
"""While one of these is open for a connection, nothing else is raised for it."""


class AlertSink(Protocol):
    """Where alerts go: `autonomy.runtime.stores.AlertStore` fits."""

    def put(self, alerts: Iterable[Alert]) -> int: ...


class WatchdogCheck(Protocol):
    """Reasons the connected data should not be trusted right now (`autonomy.inbox.watchdog`)."""

    def holds(self, business_id: str, now: datetime) -> Sequence[str]: ...


@dataclass(frozen=True)
class HealthPolicy:
    late_after: float = 1.5
    stale_after: float = 3.0
    grace: timedelta = timedelta(minutes=15)
    row_drop_ratio: float = 0.2
    row_drop_min_median: float = 20.0
    row_drop_days: int = 7
    drift_ratio: float = 0.2
    drift_min_rows: int = 10
    error_streak_alert: int = 3


class Freshness(StrEnum):
    FRESH = "fresh"
    LATE = "late"
    STALE = "stale"
    NEVER = "never"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Problem(_Model):
    code: str
    severity: Literal["info", "warning", "critical"]
    message: str
    stream: str | None = None


class StreamHealth(_Model):
    stream: str
    critical: bool
    freshness: Freshness
    last_success_at: datetime | None
    lag_seconds: float | None
    cadence_seconds: float
    history_ready: bool
    history_progress: float = Field(ge=0, le=1)
    error_streak: int = 0
    last_error_code: str | None = None
    rows_last_day: int | None = None
    rows_median_day: float | None = None
    history_error_streak: int = 0
    history_error_code: str | None = None


class ConnectionHealth(_Model):
    connection_id: str
    kind: str
    label: str
    status: ConnectionStatus
    state: Literal["healthy", "catching_up", "degraded", "stale", "needs_reconnect", "paused", "disconnected"]
    summary: str = Field(description="One line for a person, e.g. 'Shopify: healthy, 4 min behind'")
    can_act: bool
    streams: tuple[StreamHealth, ...] = ()
    problems: tuple[Problem, ...] = ()
    as_of: datetime


class ActVerdict(_Model):
    business_id: str
    can_act: bool
    reasons: tuple[str, ...] = ()
    connections: tuple[ConnectionHealth, ...] = ()
    as_of: datetime
    watchdog: tuple[str, ...] = Field(default=(), description="The data watchdog's holds (also in reasons)")
    watchdog_error: str | None = None


class IncidentDesk:
    """Opens and closes incidents; turns a newly opened one into exactly one `Alert`."""

    def __init__(self, store: ConnectorStore, alerts: AlertSink | None = None) -> None:
        self.store = store
        self.alerts = alerts

    def raise_problem(
        self,
        connection: Connection,
        problem: Problem,
        now: datetime,
        *,
        auto_paused: bool = False,
        code: str | None = None,
    ) -> Alert | None:
        """Open the incident if it is not open; return the alert raised, or None (open, or suppressed)."""
        key = code or problem.code
        open_now = self.store.open_incidents(connection.connection_id) if key not in ROOT_CAUSES else []
        if any(i.code in ROOT_CAUSES for i in open_now):
            return None  # the root cause's alert already told a person
        alert = Alert(
            alert_id=stable_id("alert", "connector", connection.connection_id, key, now.isoformat()),
            at=now,
            severity=problem.severity,
            code=problem.code,
            message=problem.message,
            auto_paused=auto_paused,
        )
        incident = Incident(
            connection_id=connection.connection_id,
            code=key,
            open=True,
            severity=problem.severity,
            message=problem.message,
            opened_at=now,
            alert_id=alert.alert_id,
            last_seen_at=now,
        )
        if not self.store.open_incident_once(incident):
            return None
        if self.alerts is not None:
            self.alerts.put([alert])
        return alert

    def resolve(self, connection_id: str, code: str, now: datetime) -> bool:
        incident = self.store.incident(connection_id, code)
        if incident is None or not incident.open:
            return False
        self.store.put_incident(incident.model_copy(update={"open": False, "resolved_at": now}))
        return True

    def resolve_others(self, connection_id: str, keep: Iterable[str], now: datetime) -> list[str]:
        """Close every open incident of the connection whose code is not in `keep` (privacy ones stay)."""
        kept = set(keep)
        closed = []
        for incident in self.store.open_incidents(connection_id):
            if incident.code in kept or incident.code.startswith(ALERT_PRIVACY):
                continue
            if self.resolve(connection_id, incident.code, now):
                closed.append(incident.code)
        return closed


class HealthMonitor:
    """Computes health from stored positions and runs; answers "may we act" and raises alerts."""

    def __init__(
        self,
        store: ConnectorStore,
        registry: ConnectorRegistry,
        *,
        policy: HealthPolicy | None = None,
        desk: IncidentDesk | None = None,
        watchdog: WatchdogCheck | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.policy = policy or HealthPolicy()
        self.desk = desk or IncidentDesk(store)
        self.watchdog = watchdog
        """Set after construction too (`autonomy.inbox.install`): its holds join `ready_to_act`."""
        self._gate_cache: dict[tuple[str, datetime], ActVerdict] = {}

    # -- reading ----------------------------------------------------------------------------------

    def stream_health(self, connection: Connection, spec: StreamSpec, now: datetime) -> StreamHealth:
        live = self.store.position(connection.connection_id, spec.stream, Lane.LIVE)
        history = self.store.position(connection.connection_id, spec.stream, Lane.HISTORY)
        cadence = spec.cadence.total_seconds()
        lag = None if live.last_success_at is None else max(0.0, (now - live.last_success_at).total_seconds())
        grace = self.policy.grace.total_seconds()
        if lag is None:
            freshness = Freshness.NEVER
        elif lag > cadence * self.policy.stale_after + grace:
            freshness = Freshness.STALE
        elif lag > cadence * self.policy.late_after + grace:
            freshness = Freshness.LATE
        else:
            freshness = Freshness.FRESH
        ready, progress = _history(connection, spec, history)
        last_day, median_day = self._daily_rows(connection, spec, now)
        return StreamHealth(
            stream=spec.stream.value,
            critical=spec.critical,
            freshness=freshness,
            last_success_at=live.last_success_at,
            lag_seconds=lag,
            cadence_seconds=cadence,
            history_ready=ready,
            history_progress=progress,
            error_streak=live.error_streak,
            last_error_code=live.last_error_code,
            rows_last_day=last_day,
            rows_median_day=median_day,
            history_error_streak=0 if history.done else history.error_streak,
            history_error_code=None if history.done else history.last_error_code,
        )

    def connection_health(self, connection: Connection, now: datetime) -> ConnectionHealth:
        connector = self.registry.find(connection.kind)
        label = connector.capabilities.label if connector is not None else connection.kind
        if connection.status is ConnectionStatus.NEEDS_RECONNECT:
            return self._blocked(connection, "needs_reconnect", f"{label}: needs reconnecting", now)
        if connection.status is ConnectionStatus.DISCONNECTED:
            return self._blocked(connection, "disconnected", f"{label}: disconnected", now)
        if connection.status is ConnectionStatus.PAUSED:
            return self._blocked(connection, "paused", f"{label}: paused", now)
        if connector is None:
            problem = Problem(
                code=ALERT_ERRORS, severity="critical", message=f"No connector for {connection.kind}"
            )
            return self._blocked(connection, "stale", f"{label}: not available", now, (problem,))
        streams = tuple(self.stream_health(connection, s, now) for s in connector.capabilities.streams)
        problems = tuple(self._problems(connection, label, connector.capabilities.streams, streams, now))
        critical = [s for s in streams if s.critical]
        stale = [s for s in critical if s.freshness in (Freshness.STALE, Freshness.NEVER)]
        waiting = [s for s in critical if not s.history_ready]
        if stale:
            state: Literal["healthy", "catching_up", "degraded", "stale"] = "stale"
        elif waiting:
            state = "catching_up"
        elif problems:
            state = "degraded"
        else:
            state = "healthy"
        return ConnectionHealth(
            connection_id=connection.connection_id,
            kind=connection.kind,
            label=connection.label,
            status=connection.status,
            state=state,
            summary=_summary(label, state, streams),
            can_act=state in ("healthy", "degraded"),
            streams=streams,
            problems=problems,
            as_of=now,
        )

    def ready_to_act(self, business_id: str, now: datetime) -> ActVerdict:
        """The one answer the runtime checks before deciding for `business_id`."""
        healths = tuple(
            self.connection_health(c, now)
            for c in self.store.connections(business_id=business_id)
            if not _removed_by_person(c)
        )
        reasons = tuple(_reason(h) for h in healths if not h.can_act)
        held: tuple[str, ...] = ()
        error: str | None = None
        if self.watchdog is not None:
            try:
                held = tuple(self.watchdog.holds(business_id, now))
            except Exception as err:  # the freshness checks still guard; say why the watchdog is quiet
                error = f"{type(err).__name__}: {err}"[:300]
        reasons = (*reasons, *held)
        return ActVerdict(
            business_id=business_id,
            can_act=not reasons,
            reasons=reasons,
            connections=healths,
            as_of=now,
            watchdog=held,
            watchdog_error=error,
        )

    def send_gate(self, business_id: str) -> Callable[[Decision, datetime], str | None]:
        """A `SendGate` (autonomy.runtime.worker) holding queued sends while the data is not fresh."""

        def gate(decision: Decision, now: datetime) -> str | None:  # noqa: ARG001
            key = (business_id, now.replace(second=0, microsecond=0))
            verdict = self._gate_cache.get(key)
            if verdict is None:
                self._gate_cache = {key: self.ready_to_act(business_id, now)}
                verdict = self._gate_cache[key]
            if verdict.can_act:
                return None
            return "data_not_fresh: " + "; ".join(verdict.reasons)

        return gate

    # -- alerting ---------------------------------------------------------------------------------

    def evaluate(self, connection: Connection, now: datetime) -> list[Alert]:
        """Open incidents for current problems, close the ones that cleared; return alerts newly raised."""
        health = self.connection_health(connection, now)
        raised: list[Alert] = []
        if health.status is not ConnectionStatus.ACTIVE:
            return raised  # reconnect / uninstall alerts are raised where the status changes
        present: list[str] = []
        for problem in _one_hold(health, self._label(connection)):
            key = problem.code if problem.stream is None else f"{problem.code}:{problem.stream}"
            present.append(key)
            alert = self.desk.raise_problem(
                connection, problem, now, auto_paused=problem.code == ALERT_STALE, code=key
            )
            if alert is not None:
                raised.append(alert)
        # The webhooks incident belongs to the daily renewal (`ConnectorEngine._reregister`), which
        # closes it when the subscriptions are back. Closing it here would reopen it, and alert a
        # person again, every day the renewal still fails.
        self.desk.resolve_others(connection.connection_id, [*present, ALERT_WEBHOOKS], now)
        return raised

    # -- internals --------------------------------------------------------------------------------

    def _label(self, connection: Connection) -> str:
        connector = self.registry.find(connection.kind)
        return connector.capabilities.label if connector is not None else connection.kind

    def _problems(
        self,
        connection: Connection,
        label: str,
        specs: Sequence[StreamSpec],
        streams: Sequence[StreamHealth],
        now: datetime,
    ) -> Iterable[Problem]:
        for spec, health in zip(specs, streams, strict=True):
            name = spec.stream.value
            denied = ACCESS_DENIED_CODE in (health.last_error_code, health.history_error_code)
            if denied and not spec.critical:
                # Degraded, not broken: the other streams keep the business deciding. One quiet note.
                yield Problem(
                    code=ALERT_STREAM_DENIED,
                    severity="info",
                    message=(
                        f"{label} did not give Autonomy access to {name}; everything else keeps "
                        f"syncing and deciding. To add {name}, reconnect {label} and approve the "
                        "access it asks for."
                    ),
                    stream=name,
                )
                continue
            if health.freshness is Freshness.STALE or (
                health.freshness is Freshness.NEVER
                and now - connection.created_at > spec.cadence * self.policy.stale_after + self.policy.grace
            ):
                behind = "never" if health.last_success_at is None else _age(health.lag_seconds or 0)
                yield Problem(
                    code=ALERT_STALE,
                    severity="critical" if spec.critical else "warning",
                    message=(
                        f"{label} {name} have not synced for {behind}"
                        if health.last_success_at is not None
                        else f"{label} {name} have never synced"
                    )
                    + (
                        ". Decisions for this business are on hold until it catches up."
                        if spec.critical
                        else "."
                    ),
                    stream=name,
                )
                continue  # a stale stream's errors are the same problem
            if health.error_streak >= self.policy.error_streak_alert:
                yield Problem(
                    code=ALERT_ERRORS,
                    severity="warning",
                    message=f"{label} {name}: the last {health.error_streak} syncs failed ({health.last_error_code}).",
                    stream=name,
                )
            if health.history_error_streak >= self.policy.error_streak_alert:
                # A backfill that keeps failing leaves the stream "loading history" for ever, which
                # holds decisions: without this, nobody would be told.
                yield Problem(
                    code=ALERT_ERRORS,
                    severity="warning",
                    message=f"{label} {name}: loading history failed {health.history_error_streak} times "
                    f"in a row ({health.history_error_code}).",
                    stream=f"{name}.history",
                )
            if (
                health.rows_median_day is not None
                and health.rows_last_day is not None
                and health.rows_median_day >= self.policy.row_drop_min_median
                and health.rows_last_day < self.policy.row_drop_ratio * health.rows_median_day
            ):
                yield Problem(
                    code=ALERT_ROW_DROP,
                    severity="warning",
                    message=(
                        f"{label} {name}: {health.rows_last_day} records in the last day against a usual "
                        f"{health.rows_median_day:.0f}. The sync works but brings back far less than normal."
                    ),
                    stream=name,
                )
            drift = self._drift(connection, spec)
            if drift is not None:
                yield Problem(
                    code=ALERT_DRIFT,
                    severity="warning",
                    message=f"{label} {name}: {drift:.0%} of records lack fields we rely on; the API may have changed.",
                    stream=name,
                )

    def _drift(self, connection: Connection, spec: StreamSpec) -> float | None:
        if not spec.required_fields:
            return None
        runs = [
            r
            for r in self.store.recent_runs(connection.connection_id, spec.stream, Lane.LIVE, limit=5)
            if r.ok
        ]
        rows = sum(r.rows for r in runs[:3])
        missing = sum(r.missing_required for r in runs[:3])
        if rows < self.policy.drift_min_rows:
            return None
        ratio = missing / rows
        return ratio if ratio > self.policy.drift_ratio else None

    def _daily_rows(
        self, connection: Connection, spec: StreamSpec, now: datetime
    ) -> tuple[int | None, float | None]:
        if spec.snapshot:
            return None, None
        days = self.policy.row_drop_days
        runs: list[SyncRun] = [
            r
            for r in self.store.recent_runs(connection.connection_id, spec.stream, Lane.LIVE, limit=2000)
            if r.ok and r.finished_at > now - timedelta(days=days + 1)
        ]
        if not runs or min(r.finished_at for r in runs) > now - timedelta(days=days):
            return None, None  # less than a week of runs: no baseline yet
        buckets = [0] * (days + 1)
        for run in runs:
            index = int((now - run.finished_at) // timedelta(days=1))
            if 0 <= index <= days:
                buckets[index] += run.rows  # rows read, not new: webhooks may have delivered them first
        return buckets[0], float(statistics.median(buckets[1:]))

    def _blocked(
        self,
        connection: Connection,
        state: Literal["stale", "needs_reconnect", "paused", "disconnected"],
        summary: str,
        now: datetime,
        problems: tuple[Problem, ...] = (),
    ) -> ConnectionHealth:
        return ConnectionHealth(
            connection_id=connection.connection_id,
            kind=connection.kind,
            label=connection.label,
            status=connection.status,
            state=state,
            summary=summary,
            can_act=False,
            problems=problems,
            as_of=now,
        )


def _one_hold(health: ConnectionHealth, label: str) -> list[Problem]:
    """The problems to alert on: every critical stream that is stale is one hold, so one alert.

    Health keeps a problem per stream (the Connect screen words each); a person is told once that
    the connection holds decisions, whichever of its critical streams fell behind. An optional stream
    stale at the same time is the same outage, so it is only an `info` note beside that one alert."""
    held = [p for p in health.problems if p.code == ALERT_STALE and p.severity == "critical"]
    rest = [p for p in health.problems if p not in held]
    if not held:
        return rest
    # The same outage behind an optional stream is a symptom of the hold, not a second problem to
    # act on: it is kept as a quiet note (for the weekly briefing), never a second warning.
    rest = [
        (
            p.model_copy(update={"severity": "info", "message": p.message + " (Part of the hold above.)"})
            if p.code == ALERT_STALE
            else p
        )
        for p in rest
    ]
    if len(held) == 1:
        one = held[0].model_copy(update={"stream": None})
    else:
        names = [p.stream for p in held if p.stream]
        listed = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else "".join(names)
        one = Problem(
            code=ALERT_STALE,
            severity="critical",
            message=(
                f"{label} {listed} are out of date. Decisions for this business are on hold until they "
                "catch up."
            ),
        )
    return [one, *rest]


def _removed_by_person(connection: Connection) -> bool:
    return (
        connection.status is ConnectionStatus.DISCONNECTED
        and connection.status_reason == PERSON_DISCONNECT_REASON
    )


def _history(connection: Connection, spec: StreamSpec, history: SyncPosition) -> tuple[bool, float]:
    if spec.backfill_days == 0 or history.done:
        return True, 1.0
    if history.reached is None:
        return spec.min_history_days == 0, 0.0
    covered = connection.anchor - history.reached
    progress = min(1.0, max(0.0, covered / timedelta(days=spec.backfill_days)))
    return covered >= timedelta(days=spec.min_history_days), progress


def _age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"


def _summary(label: str, state: str, streams: Sequence[StreamHealth]) -> str:
    lags = [s.lag_seconds for s in streams if s.critical and s.lag_seconds is not None]
    if state == "catching_up":
        waiting = [s for s in streams if s.critical and not s.history_ready]
        progress = min((s.history_progress for s in waiting), default=1.0)
        return f"{label}: loading history ({progress:.0%})"
    if state == "stale":
        return f"{label}: data is out of date"
    behind = f", {_age(max(lags))} behind" if lags else ""
    word = "healthy" if state == "healthy" else "working, with warnings"
    return f"{label}: {word}{behind}"


def _reason(health: ConnectionHealth) -> str:
    if health.state == "needs_reconnect":
        return f"{health.label} needs reconnecting"
    if health.state == "disconnected":
        return f"{health.label} is disconnected"
    if health.state == "paused":
        return f"{health.label} is paused"
    if health.state == "catching_up":
        return f"{health.label} is still loading history"
    stale = [
        s.stream for s in health.streams if s.critical and s.freshness in (Freshness.STALE, Freshness.NEVER)
    ]
    return f"{health.label} {', '.join(stale) or 'data'} is out of date"
