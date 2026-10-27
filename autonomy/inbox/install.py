"""Put the inbox, the Autopilot guard and the data watchdog on a running business.

`ZeroTouch` holds the three for one business. `tick(now)` is a runtime `before_tick` hook: the guard
runs first (an automatic demotion must be in the inbox the same minute), then the conditions are
collected and the inbox reconciled. `act(item_id, now, by=...)` is the one button of an item.

    zt = zero_touch_for_runtime(runtime, engine=db, wiring=connectors)   # SQL stores in production
    install(runtime, zt)          # the hook, and the watchdog on the data gate

`install` appends the hook to `runtime.before_tick` and attaches the watchdog: to the connector
`HealthMonitor` when given (its holds join `ready_to_act`, which the runtime's data gate already
asks); else as the data gate itself (a runtime with no connectors), or joined to the gate it has.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Engine

from autonomy.contracts import AutonomyLevel, Channel, GoalStatus
from autonomy.inbox.conditions import Collected, InboxSources, collect
from autonomy.inbox.inbox import Inbox, SyncReport
from autonomy.inbox.items import InboxItem
from autonomy.inbox.store import InboxStore, InMemoryInboxStore, SqlInboxStore
from autonomy.inbox.watchdog import (
    DataWatchdog,
    InMemoryWatchdogStore,
    SqlWatchdogStore,
    WatchdogPolicy,
)
from autonomy.mandate.models import Mandate, MandateStatus
from autonomy.runtime.promotion import (
    REASON_KEPT,
    REASON_RESUMED,
    AutonomyLedger,
    AutopilotGuard,
    GuardReport,
    InMemoryAutonomyLedger,
    PromotionRule,
    SqlAutonomyLedger,
)

__all__ = ["ActResult", "TickResult", "ZeroTouch", "install", "watchdog_gate", "zero_touch_for_runtime"]


@dataclass(frozen=True)
class ActResult:
    item: InboxItem
    done: bool
    words: str
    target: str | None = None


@dataclass
class TickResult:
    guard: GuardReport | None = None
    sync: SyncReport = field(default_factory=SyncReport)
    errors: list[str] = field(default_factory=list)


@dataclass
class ZeroTouch:
    inbox: Inbox
    sources: InboxSources
    guard: AutopilotGuard | None = None
    watchdog: DataWatchdog | None = None
    sync_now: Callable[[str, datetime], object] | None = None
    """connection_id, now -> ask the connector engine for a sync now."""
    resume_channel: Callable[[Channel], object] | None = None
    close_privacy: Callable[[str, str, datetime], object] | None = None
    """connection_id, incident code, now -> close the privacy incident a person completed."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def tick(self, now: datetime) -> TickResult:
        """Run the guard, collect the conditions, reconcile the inbox. Safe to call twice."""
        with self._lock:
            result = TickResult()
            if self.guard is not None:
                result.guard = self.guard.run(now)
                result.errors.extend(result.guard.errors)
            collected: Collected = collect(self.sources, now)
            result.errors.extend(collected.errors)
            result.sync = self.inbox.sync(collected.conditions, now, cleared=collected.cleared)
            return result

    def __call__(self, now: datetime) -> TickResult:
        return self.tick(now)

    def act(self, item_id: str, now: datetime, *, by: str) -> ActResult:
        """Press the item's one button. Raises KeyError (no such item) or ValueError (cannot now)."""
        item = self.inbox.store.get(item_id)
        if item is None or item.business_id != self.inbox.business_id:
            raise KeyError(item_id)
        if not item.open:
            return ActResult(item, True, "Already done.")
        button = item.button
        workspace = self.sources.workspace
        if button.action == "open":
            return ActResult(item, False, "Opens the page where this is done.", button.target)
        if button.action == "turn_on_autopilot":
            if self.guard is None or button.goal_id is None:
                raise ValueError("Autopilot is not available here.")
            change = self.guard.promote(button.goal_id, now, by=by)
            return self._done(item, now, by, change.words or "Autopilot is on.")
        if button.action in ("pause_goal", "resume_in_approve"):
            goal = next((g for g in workspace.goals() if g.goal_id == button.goal_id), None)
            if goal is None:
                raise ValueError("That goal no longer exists.")
            if button.action == "pause_goal":
                workspace.put_goal(goal.model_copy(update={"status": GoalStatus.PAUSED}))
                return self._done(item, now, by, "Paused.")
            briefs = workspace.briefs(goal.goal_id)
            if not briefs or max(briefs, key=lambda b: b.version).approved_at is None:
                raise ValueError("Approve the plan before it can run again.")
            workspace.put_goal(
                goal.model_copy(update={"status": GoalStatus.ACTIVE, "autonomy": AutonomyLevel.APPROVE})
            )
            # Logged whenever the level or the pause changes: a goal Autopilot already dropped to
            # Approve and its breaker paused is resumed by this person, and the ledger must say so (else
            # the guard's step back would still read as waiting for a person, and page them again).
            if goal.autonomy is not AutonomyLevel.APPROVE or goal.status is not GoalStatus.ACTIVE:
                self._log_person(
                    goal.goal_id, goal.autonomy, now, by, REASON_RESUMED, "Resumed in Approve from the inbox."
                )
            return self._done(item, now, by, "Running again in Approve: you see each batch first.")
        if button.action == "resume_channel":
            if self.resume_channel is None or button.channel is None:
                raise ValueError("Channels are resumed by the worker here.")
            self.resume_channel(Channel(button.channel))
            return self._done(item, now, by, f"{button.channel.title()} resumed.")
        if button.action == "sync_now":
            if self.sync_now is None or button.connection_id is None:
                raise ValueError("Syncing is not available here.")
            self.sync_now(button.connection_id, now)
            return ActResult(item, False, "Sync asked for; this clears by itself once the data is fresh.")
        if button.action == "acknowledge":
            if self.watchdog is None:
                raise ValueError("The data watchdog is not running here.")
            self.watchdog.acknowledge(item.fingerprint, now, by=by)
            return self._done(item, now, by, "Noted; decisions go on.")
        if button.action == "mark_handled":
            if item.cause.startswith("privacy:") and self.close_privacy is not None and button.connection_id:
                self.close_privacy(button.connection_id, item.cause.removeprefix("privacy:"), now)
            if (
                item.goal_id is not None
                and self.guard is not None
                and self.guard.demotion(item.goal_id) is not None
            ):
                # "Keep in Approve" on Autopilot's step back: the person has seen it and chose Approve.
                self._log_person(
                    item.goal_id,
                    AutonomyLevel.APPROVE,
                    now,
                    by,
                    REASON_KEPT,
                    "Kept in Approve from the inbox.",
                )
                return self._done(item, now, by, "Kept in Approve: you see each batch first.")
            return self._done(item, now, by, "Marked as done.")
        raise ValueError(f"unknown action {button.action!r}")

    def _log_person(
        self, goal_id: str, from_level: AutonomyLevel, now: datetime, by: str, reason: str, words: str
    ) -> None:
        if self.guard is not None:
            self.guard.record(
                goal_id,
                now,
                from_level=from_level,
                to_level=AutonomyLevel.APPROVE,
                actor=f"person:{by}",
                reason=reason,
                words=words,
            )

    def _done(self, item: InboxItem, now: datetime, by: str, words: str) -> ActResult:
        resolved = self.inbox.resolve(item.item_id, now, by=by) or item
        return ActResult(resolved, True, words)


def watchdog_gate(watchdog: DataWatchdog) -> Callable[[datetime], Any]:
    """A runtime `DataGate` from the watchdog alone (a runtime with no connector engine)."""
    from autonomy.runtime.service import DataHold

    def gate(now: datetime) -> DataHold | None:
        reasons = watchdog.holds(watchdog.business_id, now)
        return DataHold(reasons=tuple(reasons)) if reasons else None

    return gate


def install(runtime: Any, zero_touch: ZeroTouch, *, monitor: Any = None) -> None:
    """Hook `zero_touch.tick` into the runtime's ticks and put the watchdog on its data gate."""
    if zero_touch.tick not in runtime.before_tick:
        runtime.before_tick.append(zero_touch.tick)
    watchdog = zero_touch.watchdog
    if watchdog is None:
        return
    if monitor is not None:
        monitor.watchdog = watchdog
        return
    existing = getattr(runtime, "data_gate", None)
    if existing is not None and getattr(existing, "watchdog", None) is watchdog:
        return  # installed already
    # Without its monitor at hand, the watchdog's holds join the gate the runtime has (never unheard).
    own = watchdog_gate(watchdog)
    gate = own if existing is None else _joined_gate(existing, own)
    setattr(gate, "watchdog", watchdog)  # noqa: B010
    runtime.data_gate = gate


def _joined_gate(
    first: Callable[[datetime], Any], second: Callable[[datetime], Any]
) -> Callable[[datetime], Any]:
    from autonomy.runtime.service import DataHold

    def gate(now: datetime) -> DataHold | None:
        a, b = first(now), second(now)
        if a is None and b is None:
            return None
        reasons = tuple(dict.fromkeys((*(a.reasons if a else ()), *(b.reasons if b else ()))))
        return DataHold(reasons=reasons, alerts=tuple(a.alerts) if a else ())

    return gate


def zero_touch_for_runtime(
    runtime: Any,
    *,
    engine: Engine | None = None,
    wiring: Any = None,
    business_id: str | None = None,
    rule: PromotionRule | None = None,
    policy: WatchdogPolicy | None = None,
    mandate_for: Callable[[str], Mandate | None] | None = None,
    mandate_ledger_head: Callable[[], str] | None = None,
    margin_rate: float | None = None,
    channel_costs: Mapping[Any, float] | None = None,
    store: InboxStore | None = None,
    ledger: AutonomyLedger | None = None,
    app_url: str = "/v2/app/",
) -> ZeroTouch:
    """The three over an `AutonomyRuntime`'s own stores; SQL tables in `engine` when given.

    `wiring` (a `ConnectorWiring`) adds the connections, incidents, identity links, `sync now` and
    the privacy desk. `mandate_for` (goal id -> its mandate) turns on the mandate checks and
    automatic promotion where the mandate pre-authorises it."""
    business = (
        business_id
        or getattr(runtime, "business_id", None)
        or getattr(wiring, "business_id", None)
        or "default"
    )
    timezone = getattr(getattr(runtime, "config", None), "timezone", "UTC")
    inbox_store: InboxStore = (
        store
        if store is not None
        else (SqlInboxStore(engine) if engine is not None else InMemoryInboxStore())
    )
    autonomy_ledger: AutonomyLedger = (
        ledger
        if ledger is not None
        else (SqlAutonomyLedger(engine) if engine is not None else InMemoryAutonomyLedger())
    )
    connector_engine = getattr(wiring, "engine", None)
    connector_store = getattr(connector_engine, "store", None)
    identity_store = getattr(wiring, "identity_store", None)
    watch_policy = policy or WatchdogPolicy(timezone=timezone)
    watchdog = DataWatchdog(
        business,
        runtime.events,
        states=runtime.states,
        identity=identity_store,
        store=SqlWatchdogStore(engine) if engine is not None else InMemoryWatchdogStore(),
        policy=watch_policy,
    )

    source = getattr(runtime, "mandates", None)
    verified: Callable[[Mandate, datetime], bool] | None = None
    if source is not None:
        if mandate_for is None:

            def mandate_for(goal_id: str) -> Mandate | None:
                found: Mandate | None = source.lookup(goal_id) if source.governs(goal_id) else None
                return found

        def verified(mandate: Mandate, now: datetime) -> bool:
            return not source.problems(mandate, now)

    def mandate_active(goal_id: str, now: datetime) -> bool | None:
        if mandate_for is None:
            return None
        m = mandate_for(goal_id)
        return (
            m is not None
            and m.effective_status(now) is MandateStatus.ACTIVE
            and m.is_valid_at(now)
            and (verified is None or verified(m, now))
        )

    guard = AutopilotGuard(
        business_id=business,
        workspace=runtime.workspace,
        decisions=runtime.decisions,
        events=runtime.events,
        ledger=autonomy_ledger,
        alerts=getattr(runtime, "alert_store", None),
        rule=rule,
        mandate_for=mandate_for,
        mandate_verified=verified,
        mandate_ledger_head=mandate_ledger_head,
        governed=source is not None,
        data_trusted=watchdog.trusted,
        channel_costs=channel_costs if channel_costs is not None else getattr(runtime, "channel_costs", None),
        margin_rate=margin_rate if margin_rate is not None else getattr(runtime, "margin_rate", None),
        timezone=timezone,
    )
    inbox = Inbox(inbox_store, business)
    paused: Callable[[], Iterable[Channel]] | None = None
    if hasattr(runtime, "paused_channels"):

        def paused() -> Iterable[Channel]:
            channels: Iterable[Channel] = runtime.paused_channels
            return channels

    sources = InboxSources(
        business_id=business,
        workspace=runtime.workspace,
        alerts=getattr(runtime, "alert_store", None),
        decisions=runtime.decisions,
        events=runtime.events,
        connectors=connector_store,
        guard=guard,
        watchdog=watchdog,
        paused_channels=paused,
        mandate_active=mandate_active if mandate_for is not None else None,
        handled_at=inbox.handled_at,
        app_url=app_url,
    )
    sync_now = None
    close_privacy = None
    if connector_engine is not None:

        queue = getattr(runtime, "queue", None)

        def sync_now(connection_id: str, now: datetime) -> object:
            """Queue a live sync of every stream on the minute grid (as the Connect screen's button)."""
            from autonomy.connectors import Lane, enqueue_once, sync_job

            if connector_store is None:
                raise ValueError("Syncing is not available here.")
            connection = connector_store.get_connection(connection_id)
            connector = connector_engine.registry.find(connection.kind) if connection is not None else None
            if queue is None or connector is None:
                raise ValueError("Syncing is not available here.")
            run_at = now.replace(second=0, microsecond=0)
            for spec in connector.capabilities.streams:
                enqueue_once(queue, sync_job(connection_id, spec.stream, Lane.LIVE, run_at, tag="requested"))
            return run_at

        desk = getattr(getattr(connector_engine, "monitor", None), "desk", None)
        if desk is not None:

            def close_privacy(connection_id: str, code: str, now: datetime) -> object:
                return desk.resolve(connection_id, code, now)

    resume = getattr(runtime, "resume_channels", None)
    return ZeroTouch(
        inbox=inbox,
        sources=sources,
        guard=guard,
        watchdog=watchdog,
        sync_now=sync_now,
        resume_channel=(lambda c: resume([c])) if callable(resume) else None,
        close_privacy=close_privacy,
    )
