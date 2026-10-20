"""The always-on runtime: one `tick(now)` ties events, state, decisions, people and channels together.

`autonomy.loop.run_days` is the reference loop for the Gym. This is the same idea grown up for the
real world, where events arrive at any time, goals sit at different autonomy levels, and a person
can pause or approve things between ticks. A worker calls `tick(now)` every few minutes; the demo
and the operator endpoint call it by hand. Each tick does, in order:

1. **Fold** newly appended events into customer state. The runtime keeps a watermark and the ids
   it has already folded near it, so a late event (up to `late_window` old) is still picked up and
   none is folded twice. The first tick (and, by default, each nightly run) rebuilds every state
   from the event log instead, which also catches anything later than the late window.
2. **Replies** go to the Conversation agent. Its events (an UNSUBSCRIBE for a STOP, a snooze, a
   complaint) are appended and folded before anything is decided, so a customer who said stop is
   never contacted by this tick. Replies a person should read become alerts.
3. **Circuit breakers** (`autonomy.channels.breakers`) run over the last week of events. A tripped
   rule pauses its goal (status PAUSED in the workspace, so a person sees it and resumes it) or a
   channel (on the dispatcher).
4. **Batches.** Batches nobody acted on for `batch_max_age` expire. Batches a person approved
   since the last tick are sent now, if their goal is still active.
5. **Decide.** For each ACTIVE goal with an approved brief: right away for customers with a new
   trigger event (a REPLY or a CART), at the daily sweep hour for everyone in the goal's audience,
   and after the sweep for customers who have just crossed a time threshold of the audience (a
   lapse). Customers already decided for the goal today are skipped. What happens next depends on
   the goal's autonomy level: SHADOW logs only; APPROVE logs and holds the ACT decisions in one
   pending `Batch`; AUTOPILOT logs and dispatches.
6. **Nightly**, once a day at `nightly_hour`: each goal's policy gets `nightly(decisions,
   events_by_customer, now)` with its own goal's decisions, if it has the hook (the fast learning
   loop).
7. **Weekly**, once a week, when an analyst and a strategist are configured: the Orchestrator's
   weekly cycle (Analyst report, then the Strategist's narrowing of the brief).

Nothing here reads the clock: every step takes `now`, so the same ticks over the same events give
the same decisions. Decision ids are derived from (goal, customer, trigger), which is what makes a
repeated tick a no-op. No LLM runs per customer: the policy is maths, and the Conversation agent
only runs on replies.

Each goal gets its own policy, built by `policy_factory([brief], content)` and rebuilt only when
the brief or its approved content changes. One policy per goal keeps autonomy levels apart: a goal
in shadow can never win a customer's attention away from a goal on autopilot. Customers are still
protected across goals: a customer contacted (or held for approval) by one goal in a tick is not
contacted by another in the same tick, and the contact caps in `decide()` read the SENDs every goal
made.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, Field

from autonomy.agents.conversation import Conversation, ReplyOutcome
from autonomy.agents.orchestrator import AdjustFn, AnalystFn, Orchestrator, WeeklyOutcome
from autonomy.channels.breakers import DEFAULT_GUARDS, BreakerResult, Guards, evaluate_goals
from autonomy.channels.dispatcher import (
    SEND_JOB_KIND,
    Dispatcher,
    DispatchReport,
    DispatchResult,
)
from autonomy.contracts import (
    Alert,
    AutonomyLevel,
    Batch,
    BatchStatus,
    Brief,
    Briefing,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    Goal,
    GoalStatus,
    Group,
    stable_id,
)
from autonomy.data.rebuild import apply_events, rebuild_states
from autonomy.decide.audience import matches
from autonomy.interfaces import (
    DecisionLog,
    EventStore,
    JobQueue,
    Policy,
    StateFolder,
    StateStore,
    TextSignals,
    Workspace,
)
from autonomy.runtime.batches import (
    BatchStore,
    BatchTransitionError,
    InMemoryBatchStore,
    UnknownBatchError,
    approve,
    mark_sent,
    reject,
)

if TYPE_CHECKING:
    from autonomy.runtime.worker import Worker

__all__ = [
    "TRIGGER_TYPES",
    "AutonomyRuntime",
    "BreakersFn",
    "PersonReadsReplies",
    "PolicyFactory",
    "RuntimeConfig",
    "RuntimeCursor",
    "SpendFn",
    "TickReport",
    "Trigger",
    "build_worker",
    "make_batch",
    "standard_breakers",
]

PolicyFactory = Callable[[Sequence[Brief], Sequence[ContentItem]], Policy]
"""(briefs, approved content) -> a policy. The runtime passes one goal's brief and content."""

BreakersFn = Callable[[Sequence[Brief], Sequence[Event], datetime], BreakerResult]
"""(briefs of running goals, recent events, now) -> alerts and what to pause."""

SpendFn = Callable[[str, datetime], float]
"""(goal_id, now) -> money spent on the goal so far, in its currency."""

GroupFn = Callable[[CustomerState], CustomerState]
"""state -> the same state with its proof group set (see `autonomy.proof.split.group_assigner`)."""

Severity = Literal["info", "warning", "critical"]

TRIGGER_TYPES: frozenset[EventType] = frozenset({EventType.REPLY, EventType.CART})
"""Events that get a customer a decision within one tick, not at the next daily sweep."""

TIME_OPS: frozenset[str] = frozenset({"days_since_gt", "days_since_lt"})
"""Audience filters that change with time alone; crossing one is the lapse trigger."""

MAX_SEND_JOBS_PER_TICK = 10_000
SOURCE = "runtime"


class Trigger(StrEnum):
    SWEEP = "sweep"
    EVENT = "event"
    LAPSE = "lapse"


@dataclass(frozen=True)
class RuntimeConfig:
    """When things happen. Hours are local to `timezone` (the business's time zone)."""

    sweep_hour: int = 10
    nightly_hour: int = 2
    weekly_weekday: int = 0
    """Monday is 0."""
    weekly_hour: int = 7
    timezone: str = "UTC"
    late_window: timedelta = timedelta(days=2)
    """How old an event may be when it arrives and still be folded by the incremental fold."""
    trigger_window: timedelta = timedelta(days=1)
    """A REPLY or CART older than this when the runtime first sees it triggers nothing."""
    breaker_window: timedelta = timedelta(days=7)
    lapse_check_every: timedelta = timedelta(hours=1)
    batch_max_age: timedelta = timedelta(days=3)
    """A batch not approved (or not sendable) within this long expires: its decisions are stale."""
    nightly_lookback: timedelta = timedelta(days=30)
    nightly_rebuild: bool = True
    one_contact_per_tick: bool = True
    """A customer contacted (or held for approval) by one goal is not decided by another that tick."""
    run_send_jobs: bool = True
    seed: int = 0
    worker: str = "runtime"

    def __post_init__(self) -> None:
        for name in ("sweep_hour", "nightly_hour", "weekly_hour"):
            if not 0 <= getattr(self, name) <= 23:
                raise ValueError(f"{name} is an hour of the day, 0 to 23")
        if not 0 <= self.weekly_weekday <= 6:
            raise ValueError("weekly_weekday is 0 (Monday) to 6 (Sunday)")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as err:
            raise ValueError(f"unknown time zone {self.timezone!r}") from err
        if self.late_window < timedelta(0) or self.trigger_window < timedelta(0):
            raise ValueError("windows cannot be negative")


class RuntimeCursor(BaseModel):
    """Where the runtime is up to. Small and JSON-able, so a worker can save it between restarts.

    Losing it is safe: the next tick rebuilds state from the log, and decision ids make repeated
    decisions no-ops. It only saves work (and a repeated nightly or weekly run).
    """

    watermark: AwareDatetime | None = None
    last_tick: AwareDatetime | None = None
    nightly_on: date | None = None
    weekly_on: str | None = Field(default=None, description="ISO week, e.g. '2026-W43'")
    swept: dict[str, date] = Field(default_factory=dict, description="goal_id -> local day of its last sweep")
    lapse_checked_at: AwareDatetime | None = None


@dataclass
class TickReport:
    """What one tick did. `summary()` is what the operator endpoint returns."""

    now: datetime
    events_folded: int = 0
    rebuilt: bool = False
    decisions: list[Decision] = field(default_factory=list)
    dispatch: DispatchReport = field(default_factory=DispatchReport)
    batches_created: list[Batch] = field(default_factory=list)
    batches_sent: list[Batch] = field(default_factory=list)
    batches_expired: list[Batch] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    replies: list[ReplyOutcome] = field(default_factory=list)
    paused_goals: list[str] = field(default_factory=list)
    paused_channels: list[Channel] = field(default_factory=list)
    swept_goals: list[str] = field(default_factory=list)
    nightly_ran: bool = False
    weekly: list[WeeklyOutcome] = field(default_factory=list)

    @property
    def sent(self) -> list[DispatchResult]:
        return self.dispatch.sent

    @property
    def acts(self) -> list[Decision]:
        return [d for d in self.decisions if d.kind is DecisionKind.ACT]

    @property
    def briefings(self) -> list[Briefing]:
        return [w.insights for w in self.weekly]

    def summary(self) -> dict[str, Any]:
        """Counts plus the batches and alerts in full, JSON-ready."""
        return {
            "now": self.now.isoformat(),
            "decisions": len(self.decisions),
            "acts": len(self.acts),
            "sent": len(self.sent),
            "queued": len(self.dispatch.queued),
            "failed": len(self.dispatch.failed),
            "skipped": self.dispatch.skip_reasons(),
            "events_folded": self.events_folded,
            "batches": [b.model_dump(mode="json") for b in self.batches_created],
            "batches_sent": [b.batch_id for b in self.batches_sent],
            "batches_expired": [b.batch_id for b in self.batches_expired],
            "alerts": [a.model_dump(mode="json") for a in self.alerts],
            "paused_goals": list(self.paused_goals),
            "paused_channels": [c.value for c in self.paused_channels],
            "swept_goals": list(self.swept_goals),
            "nightly_ran": self.nightly_ran,
            "weekly": [w.brief.goal_id for w in self.weekly],
        }


class PersonReadsReplies:
    """A `TextSignals` that is never sure, so every reply the keyword nets miss goes to a person.

    The default for the Conversation agent when no classifier is configured: STOP and complaint
    words are still honoured at once (they never reach a classifier), and nothing is guessed.
    """

    def classify(
        self, texts: Sequence[str], *, question: str, labels: Sequence[str]  # noqa: ARG002
    ) -> list[tuple[str, float]]:
        fallback = "other" if "other" in labels else labels[-1]
        return [(fallback, 0.0) for _ in texts]


def standard_breakers(
    *,
    baselines: Mapping[str, float] | None = None,
    spend_fn: SpendFn | None = None,
    planned_fn: SpendFn | None = None,
    guards: Guards = DEFAULT_GUARDS,
) -> BreakersFn:
    """The circuit breakers of `autonomy.channels.breakers`, per goal, with its own stop rules.

    Without `spend_fn` and `planned_fn` spend pace is never judged (planned spend 0 is below every
    guard); without baselines the unsubscribe multiple is never judged. Both are deliberate: a
    breaker never guesses.
    """
    base = dict(baselines or {})

    def run(briefs: Sequence[Brief], recent: Sequence[Event], now: datetime) -> BreakerResult:
        spend = {b.goal_id: spend_fn(b.goal_id, now) for b in briefs} if spend_fn else {}
        planned = {b.goal_id: planned_fn(b.goal_id, now) for b in briefs} if planned_fn else {}
        return evaluate_goals(briefs, recent, base, spend, planned, now, guards=guards)

    return run


def make_batch(goal_id: str, acts: Sequence[Decision], now: datetime) -> Batch:
    """One pending batch holding a goal's ACT decisions. Its id depends only on the decisions."""
    ids = tuple(sorted(d.decision_id for d in acts))
    channels: Counter[str] = Counter()
    offers: Counter[str] = Counter()
    spend = 0.0
    for d in acts:
        if d.action is None:
            continue
        channels[d.action.channel.value] += 1
        offers[d.action.offer_id] += 1
        chosen = next((b for b in d.bids if b.action == d.action), None)
        if chosen is not None:
            spend += max(chosen.offer_cost, 0.0) + max(chosen.channel_cost, 0.0)
    n = len(ids)

    def listed(counts: Counter[str]) -> str:
        return ", ".join(f"{k} ({v})" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    who = "1 customer" if n == 1 else f"{n} customers"
    return Batch(
        batch_id=stable_id("batch", goal_id, *ids),
        goal_id=goal_id,
        created_at=now,
        decision_ids=ids,
        n_contacts=n,
        expected_net=round(sum(d.expected_net for d in acts), 2),
        expected_spend=round(spend, 2),
        summary=f"{who} by {listed(channels)}; offers: {listed(offers)}.",
    )


@dataclass
class _Day:
    """What has been decided today (local day), so a customer is decided once per goal per day."""

    day: date
    ids: set[str] = field(default_factory=set)
    decided: set[tuple[str, str]] = field(default_factory=set)
    """(goal_id, customer_id) pairs with any decision today we can attribute to the goal."""
    acted: set[tuple[str, str]] = field(default_factory=set)
    """(goal_id, customer_id) pairs with an ACT decision today."""
    alerted: set[tuple[str, str | None]] = field(default_factory=set)

    def add(self, decision: Decision, goal_id: str | None) -> None:
        self.ids.add(decision.decision_id)
        if decision.action is not None:
            goal_id = decision.action.goal_id
            self.acted.add((goal_id, decision.customer_id))
        elif goal_id is None and decision.bids:
            goal_id = decision.bids[0].goal_id
        if goal_id is not None:
            self.decided.add((goal_id, decision.customer_id))


@dataclass
class _Running:
    """An active goal with an approved brief, ready to decide for."""

    goal: Goal
    brief: Brief
    content: list[ContentItem]
    policy: Policy


@dataclass
class _Candidate:
    customer_id: str
    trigger: Trigger
    decision_id: str
    state: CustomerState | None = None
    """Already read and refreshed this tick (sweeps and lapses); None means read it when needed."""


class AutonomyRuntime:
    """Runs the whole loop, one `tick(now)` at a time. See the module docstring for the steps."""

    def __init__(
        self,
        *,
        events: EventStore,
        states: StateStore,
        decisions: DecisionLog,
        workspace: Workspace,
        folder: StateFolder,
        policy_factory: PolicyFactory,
        queue: JobQueue | None = None,
        dispatcher: Dispatcher | None = None,
        batches: BatchStore | None = None,
        breakers_fn: BreakersFn | None = None,
        conversation: Conversation | None = None,
        text_signals: TextSignals | None = None,
        analyst: AnalystFn | None = None,
        strategist: AdjustFn | None = None,
        spend_fn: SpendFn | None = None,
        assign_group: GroupFn | None = None,
        config: RuntimeConfig | None = None,
        cursor: RuntimeCursor | None = None,
    ) -> None:
        """`conversation` defaults to one over `text_signals`, else over `PersonReadsReplies`: STOP
        is always honoured. `strategist` is the weekly adjustment, e.g. `Strategist.adjust_brief`;
        the weekly cycle runs only when both it and `analyst` are given."""
        self.events = events
        self.states = states
        self.decisions = decisions
        self.workspace = workspace
        self.folder = folder
        self.policy_factory = policy_factory
        self.queue = queue
        self.dispatcher = dispatcher
        self.batches: BatchStore = batches if batches is not None else InMemoryBatchStore()
        self.breakers_fn = breakers_fn
        self.conversation = conversation or Conversation(text_signals or PersonReadsReplies())
        self.analyst = analyst
        self.strategist = strategist
        self.spend_fn = spend_fn
        self.assign_group = assign_group
        self.config = config or RuntimeConfig()
        self.cursor = cursor or RuntimeCursor()
        self._tz = ZoneInfo(self.config.timezone)
        self._seen: dict[str, datetime] = {}
        """Ids (and times) of events already folded, near the watermark."""
        self._warm = False
        self._day: _Day | None = None
        self._policies: dict[str, tuple[tuple[object, ...], Policy]] = {}
        self._paused_channels: set[Channel] = set()

    # --- the API's helpers ---------------------------------------------------------------------

    def list_batches(
        self, goal_id: str | None = None, statuses: Sequence[BatchStatus] | None = None
    ) -> list[Batch]:
        return self.batches.list_batches(goal_id=goal_id, statuses=statuses)

    def approve_batch(self, batch_id: str, *, by: str, now: datetime) -> Batch:
        """A person approves a pending batch; the next tick sends it."""
        return approve(self.batches, batch_id, by=by, now=now)

    def reject_batch(self, batch_id: str, *, by: str, now: datetime) -> Batch:
        return reject(self.batches, batch_id, by=by, now=now)

    def resume_channels(self, channels: Iterable[Channel]) -> None:
        """A person lifts a breaker's channel pause. It trips again if the harm is still in the data."""
        chosen = set(channels)
        self._paused_channels -= chosen
        if self.dispatcher is not None:
            self.dispatcher.resume(channels=chosen)

    @property
    def paused_channels(self) -> frozenset[Channel]:
        return frozenset(self._paused_channels)

    # --- the tick ------------------------------------------------------------------------------

    def tick(self, now: datetime) -> TickReport:
        """One pass of the loop at `now` (timezone-aware). Calling it twice at the same `now` is safe."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        report = TickReport(now=now)
        local = now.astimezone(self._tz)
        self._start_day(local.date())

        # The nightly rebuild is this tick's fold, not a step after deciding: events that arrive
        # while the tick runs (the API stores webhooks at any time) must still reach the replies
        # and triggers below, and a rebuild late in the tick would fold them out of their sight.
        nightly_due = self._nightly_due(local)
        new_events = self._fold(now, report, rebuild=nightly_due and self.config.nightly_rebuild)
        self._handle_replies(new_events, now, report)
        self._run_breakers(now, report)
        self._expire_batches(now, report)
        self._send_approved(now, report)
        self._run_send_jobs(now, report)
        self._decide(now, local, new_events, report)
        if nightly_due:
            self._nightly(now, local, report)
        self._weekly(now, local, report)

        self.cursor.last_tick = now if self.cursor.last_tick is None else max(self.cursor.last_tick, now)
        return report

    # --- 1. fold ---------------------------------------------------------------------------------

    def _fold(self, now: datetime, report: TickReport, *, rebuild: bool = False) -> list[Event]:
        """Fold events appended since the last tick; return the new ones up to `now`."""
        if rebuild or self.cursor.watermark is None or not self._warm:
            # A new process does not know which events near a saved watermark it folded, so it
            # rebuilds once; folding an event twice would count a purchase twice.
            return self._full_rebuild(now, report)
        since = self.cursor.watermark - self.config.late_window
        new = [e for e in self.events.scan(since=since) if e.ts <= now and e.event_id not in self._seen]
        if new:
            apply_events(new, self.states, self.folder, now=now)
            self._mark_seen(new)
            self._assign_groups({e.customer_id for e in new})
        report.events_folded += len(new)
        self._advance_watermark(now)
        return new

    def _full_rebuild(self, now: datetime, report: TickReport) -> list[Event]:
        """Rebuild every state from the log; return the events this process had not seen before.

        The rebuild's own scan records the recent events it folds, so what is marked as folded is
        exactly what was folded, even while other processes append. A new process has seen
        nothing, so it returns every recent event (replies and triggers skip what was handled).
        """
        floor = now - max(self.config.late_window, self.config.trigger_window)
        recording = _RecordingEvents(self.events, floor)
        rebuild_states(recording, self.states, self.folder, now)
        # Everything folded, future-dated events included, is marked so it is never folded again.
        fresh = [
            e for e in recording.recent if e.ts <= now and (not self._warm or e.event_id not in self._seen)
        ]
        self._mark_seen(recording.recent)
        self._assign_groups(None)
        self._advance_watermark(now)
        self._warm = True
        report.rebuilt = True
        report.events_folded += len(fresh)
        return fresh

    def _mark_seen(self, events: Iterable[Event]) -> None:
        for e in events:
            self._seen[e.event_id] = e.ts

    def _advance_watermark(self, now: datetime) -> None:
        wm = self.cursor.watermark
        self.cursor.watermark = now if wm is None else max(wm, now)
        floor = self.cursor.watermark - self.config.late_window
        self._seen = {k: ts for k, ts in self._seen.items() if ts >= floor}

    def _assign_groups(self, customer_ids: set[str] | None) -> None:
        if self.assign_group is None:
            return
        rows: Iterable[CustomerState]
        if customer_ids is None:
            rows = list(self.states.scan())
        else:
            rows = [s for s in (self.states.get(c) for c in sorted(customer_ids)) if s is not None]
        changed = [g for s in rows if (g := self.assign_group(s)) is not s and g != s]
        if changed:
            self.states.put(changed)

    def _fold_written(self, events: Sequence[Event], now: datetime) -> None:
        """Fold events this tick wrote itself (sends, reply outcomes) right away."""
        fresh = [e for e in events if e.event_id not in self._seen and e.ts <= now]
        if fresh:
            apply_events(fresh, self.states, self.folder, now=now)
            self._mark_seen(fresh)

    # --- 2. replies ------------------------------------------------------------------------------

    def _handle_replies(self, new_events: Sequence[Event], now: datetime, report: TickReport) -> None:
        for reply in (e for e in new_events if e.type is EventType.REPLY):
            if self._reply_handled(reply):
                continue
            state = self.states.get(reply.customer_id)
            try:
                outcome = self.conversation.handle_reply(reply, state, now)
            except Exception as err:  # one bad reply must not stop the loop; a person reads it
                self._alert(
                    report,
                    code="reply.error",
                    key=reply.event_id,
                    severity="warning",
                    message=(
                        f"A reply from customer {reply.customer_id} could not be read "
                        f"({type(err).__name__}); a person should read it."
                    ),
                )
                continue
            if outcome.events_to_record:
                self.events.append(outcome.events_to_record)
                self._fold_written(outcome.events_to_record, now)
            report.replies.append(outcome)
            if outcome.needs_human:
                self._alert(
                    report,
                    code="reply.needs_human",
                    key=reply.event_id,
                    severity="warning",
                    message=(
                        f"Customer {reply.customer_id} replied ({outcome.intent}); a person should "
                        f"read it. {outcome.reason}"
                    ),
                )

    def _reply_handled(self, reply: Event) -> bool:
        """True if the Conversation agent's events for this reply are already in the log."""
        return any(
            e.attrs.get("reply_event_id") == reply.event_id
            for e in self.events.for_customer(reply.customer_id, since=reply.ts)
        )

    # --- 3. breakers -----------------------------------------------------------------------------

    def _run_breakers(self, now: datetime, report: TickReport) -> None:
        if self.breakers_fn is None:
            return
        running = [(g, b) for g, b in self._goals_with_briefs() if g.status is GoalStatus.ACTIVE]
        if not running:
            return
        recent = [e for e in self.events.scan(since=now - self.config.breaker_window) if e.ts <= now]
        result = self.breakers_fn([b for _, b in running], recent, now)
        goals = {g.goal_id: g for g, _ in running}
        newly_paused: set[str] = set()
        for goal_id in sorted(result.paused_goals):
            goal = goals.get(goal_id)
            if goal is not None and goal.status is GoalStatus.ACTIVE:
                self.workspace.put_goal(goal.model_copy(update={"status": GoalStatus.PAUSED}))
                newly_paused.add(goal_id)
                report.paused_goals.append(goal_id)
        new_channels = sorted(set(result.paused_channels) - self._paused_channels, key=lambda c: c.value)
        if new_channels:
            self._paused_channels.update(new_channels)
            if self.dispatcher is not None:
                self.dispatcher.pause(channels=new_channels)
            report.paused_channels.extend(new_channels)
        day = self._today()
        for alert in result.alerts:
            key = (alert.code, alert.goal_id)
            if key in day.alerted and alert.goal_id not in newly_paused:
                continue
            day.alerted.add(key)
            report.alerts.append(alert)

    # --- 4. batches ------------------------------------------------------------------------------

    def _expire_batches(self, now: datetime, report: TickReport) -> None:
        for batch in self.batches.list_batches(statuses=(BatchStatus.PENDING, BatchStatus.APPROVED)):
            if now - batch.created_at <= self.config.batch_max_age:
                continue
            try:
                expired = self.batches.transition(
                    batch.batch_id, batch.status, BatchStatus.REJECTED, by=f"{SOURCE}:expired", at=now
                )
            except (BatchTransitionError, UnknownBatchError):
                continue  # a person got there first
            report.batches_expired.append(expired)
            self._alert(
                report,
                code="batch.expired",
                key=batch.batch_id,
                severity="info",
                goal_id=batch.goal_id,
                message=(
                    f"A batch of {batch.n_contacts} contacts was {batch.status.value} but not sent within "
                    f"{_days(self.config.batch_max_age)}; it expired because its decisions are out of date."
                ),
            )

    def _send_approved(self, now: datetime, report: TickReport) -> None:
        if self.dispatcher is None:
            return
        goals = {g.goal_id: g for g in self.workspace.goals()}
        for batch in self.batches.list_batches(statuses=(BatchStatus.APPROVED,)):
            goal = goals.get(batch.goal_id)
            if goal is None or not _sendable(goal, now):
                continue  # held until the goal runs again, or until it expires
            decided = [d for d in (self.decisions.get(i) for i in batch.decision_ids) if d is not None]
            if self._held_by_pause(batch.goal_id, decided):
                # Sending now would skip every message and still mark the batch sent, so what the
                # person approved would be lost. Hold it until the pause lifts, or until it expires.
                continue
            states = self._fresh_states({d.customer_id for d in decided}, now)
            result = self.dispatcher.dispatch(decided, states, now)
            self._take_dispatch(result, now, report)
            try:
                report.batches_sent.append(mark_sent(self.batches, batch.batch_id, now=now))
            except (BatchTransitionError, UnknownBatchError):
                continue

    def _held_by_pause(self, goal_id: str, decided: Sequence[Decision]) -> bool:
        """True if the dispatcher would skip this batch because its goal or a channel is paused."""
        assert self.dispatcher is not None  # only called when sending
        if goal_id in self.dispatcher.paused_goals:
            return True
        paused = self._paused_channels | self.dispatcher.paused_channels
        return any(d.action is not None and d.action.channel in paused for d in decided)

    def _run_send_jobs(self, now: datetime, report: TickReport) -> None:
        """Send queued messages that are due (a decision's send_at was in the future)."""
        if self.queue is None or self.dispatcher is None or not self.config.run_send_jobs:
            return
        # The worker's retry rules, so a send failed here is retried exactly as the worker would.
        from autonomy.runtime.worker import DEFAULT_MAX_ATTEMPTS, retry_delay

        for _ in range(MAX_SEND_JOBS_PER_TICK):
            job = self.queue.claim([SEND_JOB_KIND], worker=self.config.worker, now=now)
            if job is None:
                return
            retry_at = now + retry_delay(job.attempts) if job.attempts < DEFAULT_MAX_ATTEMPTS else None
            try:
                result = self.dispatcher.run_send_job(job, self.decisions, self.states, now)
            except Exception as err:  # retried later, then left failed; the tick carries on
                self.queue.fail(job.job_id, f"{type(err).__name__}: {err}"[:500], retry_at=retry_at)
                continue
            if result.failed:
                # The provider refused or was down. Completing the job would drop the message.
                reasons = "; ".join(r.reason or "provider error" for r in result.failed)
                self.queue.fail(job.job_id, f"send failed: {reasons}"[:500], retry_at=retry_at)
            else:
                self.queue.complete(job.job_id)
            self._take_dispatch(result, now, report)

    def _take_dispatch(self, result: DispatchReport, now: datetime, report: TickReport) -> None:
        report.dispatch.merge(result)
        self._fold_written(result.events, now)

    # --- 5. decide -------------------------------------------------------------------------------

    def _decide(
        self, now: datetime, local: datetime, new_events: Sequence[Event], report: TickReport
    ) -> None:
        running = self._running(now)
        if not running:
            return
        day = self._today()
        triggers = sorted(
            (
                e
                for e in new_events
                if e.type in TRIGGER_TYPES and now - self.config.trigger_window <= e.ts <= now
            ),
            key=lambda e: (e.ts, e.event_id),
        )
        lapse_due = self._lapse_due(now)
        everyone: list[CustomerState] | None = None
        contacted_this_tick: set[str] = set()

        for run in running:
            gid = run.goal.goal_id
            candidates: list[_Candidate] = []
            for e in triggers:
                did = _decision_id(gid, e.customer_id, Trigger.EVENT, e.event_id)
                candidates.append(_Candidate(e.customer_id, Trigger.EVENT, did))
            sweep_due = local.hour >= self.config.sweep_hour and self.cursor.swept.get(gid) != day.day
            lapse = (
                lapse_due
                and not sweep_due
                and self.cursor.swept.get(gid) == day.day
                and any(f.op in TIME_OPS for f in run.brief.audience.filters)
            )
            if sweep_due or lapse:
                if everyone is None:
                    everyone = [self.folder.refresh(s, now) for s in self.states.scan()]
                if sweep_due:
                    trig, key = Trigger.SWEEP, day.day.isoformat()
                    members = [s for s in everyone if matches(s, run.brief.audience, now)]
                else:
                    trig, key = Trigger.LAPSE, day.day.isoformat()
                    before = self.cursor.lapse_checked_at or self.cursor.last_tick or now
                    members = [
                        s
                        for s in everyone
                        if matches(s, run.brief.audience, now) and not matches(s, run.brief.audience, before)
                    ]
                for s in members:
                    did = _decision_id(gid, s.customer_id, trig, key)
                    candidates.append(_Candidate(s.customer_id, trig, did, s))

            made = self._decide_goal(run, candidates, contacted_this_tick, now, report)
            if sweep_due:
                self.cursor.swept[gid] = day.day
                report.swept_goals.append(gid)
            self._act(run, made, contacted_this_tick, now, report)
        if lapse_due:
            self.cursor.lapse_checked_at = now

    def _decide_goal(
        self,
        run: _Running,
        candidates: Sequence[_Candidate],
        contacted_this_tick: set[str],
        now: datetime,
        report: TickReport,
    ) -> list[Decision]:
        gid = run.goal.goal_id
        day = self._today()
        ctx = DecisionContext(
            now=now,
            briefs=(run.brief,),
            content=tuple(run.content),
            budget_left={gid: self._budget_left(run, now)},
            seed=_seed(self.config.seed, gid, now),
            policy_version=run.policy.version,
        )
        made: list[Decision] = []
        failures = 0
        decided_now: set[str] = set()
        for cand in candidates:
            cid = cand.customer_id
            if cand.decision_id in day.ids or cid in decided_now:
                continue  # already decided: a repeated tick, or one decision per goal per customer per tick
            if cand.trigger is not Trigger.EVENT and (gid, cid) in day.decided:
                continue  # sweeps and lapses decide a customer once a day per goal
            if (gid, cid) in day.acted:
                continue  # never two contacts for one goal in a day
            if cid in contacted_this_tick and self.config.one_contact_per_tick:
                continue  # nor two goals in one tick
            if cand.trigger is Trigger.EVENT and self.decisions.get(cand.decision_id) is not None:
                continue  # decided in an earlier process for the same event
            # A snapshot from this tick is current: only replies (folded before deciding) and
            # this tick's sends change state. A customer another goal reached this tick is read
            # afresh, so the contact caps in decide() see that send.
            state = None if cid in contacted_this_tick else cand.state
            if state is None:
                stored = self.states.get(cid)
                if stored is None:
                    continue
                state = self.folder.refresh(stored, now)
            if cand.trigger is Trigger.EVENT and not matches(state, run.brief.audience, now):
                continue
            try:
                decision = run.policy.decide(state, ctx)
            except Exception:  # a policy bug for one customer must not stop everyone else's
                failures += 1
                continue
            decision = decision.model_copy(update={"decision_id": cand.decision_id})
            made.append(decision)
            decided_now.add(cid)
        if failures:
            self._alert(
                report,
                code="decide.error",
                key=f"{gid}:{now.isoformat()}",
                severity="warning",
                goal_id=gid,
                message=f"The policy failed for {failures} customers of this goal; they were skipped.",
            )
        if made:
            self.decisions.record(made)
            for d in made:
                day.add(d, gid)
            report.decisions.extend(made)
        return made

    def _act(
        self,
        run: _Running,
        made: Sequence[Decision],
        contacted_this_tick: set[str],
        now: datetime,
        report: TickReport,
    ) -> None:
        """Do what the goal's autonomy level allows with its ACT decisions."""
        gid = run.goal.goal_id
        acts = [
            d
            for d in made
            if d.kind is DecisionKind.ACT
            and d.action is not None
            and d.action.goal_id == gid
            and d.group is not Group.HOLDOUT
        ]
        stray = sum(1 for d in made if d.action is not None and d.action.goal_id != gid)
        if stray:
            self._alert(
                report,
                code="decide.wrong_goal",
                key=f"{gid}:{now.isoformat()}",
                severity="warning",
                goal_id=gid,
                message=f"The policy chose another goal for {stray} customers; those were logged, not sent.",
            )
        if not acts:
            return
        level = run.goal.autonomy
        if level is AutonomyLevel.SHADOW:
            return
        contacted_this_tick.update(d.customer_id for d in acts)
        if level is AutonomyLevel.APPROVE:
            batch = make_batch(gid, acts, now)
            if self.batches.put(batch):
                report.batches_created.append(batch)
            return
        if self.dispatcher is None:
            self._alert(
                report,
                code="dispatch.missing",
                key=gid,
                severity="critical",
                goal_id=gid,
                message="This goal is on autopilot but no channels are set up; nothing was sent.",
            )
            return
        states = self._fresh_states({d.customer_id for d in acts}, now)
        self._take_dispatch(self.dispatcher.dispatch(acts, states, now), now, report)

    def _lapse_due(self, now: datetime) -> bool:
        if self.cursor.last_tick is None:
            return False
        last = self.cursor.lapse_checked_at
        return last is None or now - last >= self.config.lapse_check_every

    def _budget_left(self, run: _Running, now: datetime) -> float:
        spent = self.spend_fn(run.goal.goal_id, now) if self.spend_fn is not None else 0.0
        return max(0.0, min(run.brief.envelope.budget, run.goal.budget) - spent)

    # --- 6. nightly ------------------------------------------------------------------------------

    def _nightly_due(self, local: datetime) -> bool:
        return local.hour >= self.config.nightly_hour and self.cursor.nightly_on != local.date()

    def _nightly(self, now: datetime, local: datetime, report: TickReport) -> None:
        """The fast learning loop. The tick has already rebuilt state, if configured to."""
        today = local.date()
        self.cursor.nightly_on = today
        report.nightly_ran = True
        running = self._running(now)
        learners: list[tuple[_Running, Callable[..., object]]] = []
        for run in running:
            nightly = getattr(run.policy, "nightly", None)
            if callable(nightly):
                learners.append((run, nightly))
        if not learners:
            return
        since = now - self.config.nightly_lookback
        decided = [d for d in self.decisions.scan(since=since) if d.made_at <= now]
        customers = {d.customer_id for d in decided}
        by_customer: dict[str, list[Event]] = defaultdict(list)
        # Trigger events from one trigger window earlier, so an event decision near `since` is
        # still attributed to its goal.
        for e in self.events.scan(since=since - self.config.trigger_window):
            if e.customer_id in customers and e.ts <= now:
                by_customer[e.customer_id].append(e)
        goal_ids = sorted(g.goal_id for g in self.workspace.goals())
        owner = {
            d.decision_id: _goal_of(
                d,
                goal_ids,
                self._tz,
                [e.event_id for e in by_customer.get(d.customer_id, ()) if e.type in TRIGGER_TYPES],
            )
            for d in decided
        }
        for run, fn in learners:
            # Each policy learns from its own goal's decisions only. Handing it every goal's would
            # count a holdout customer's "do nothing" once per goal, and as control for goals whose
            # audience they are not in. Decisions no goal can be found for go to every policy.
            gid = run.goal.goal_id
            mine = [d for d in decided if owner[d.decision_id] in (gid, None)]
            mine_customers = {d.customer_id for d in mine}
            outcomes: dict[str, list[Event]] = {}
            for c, evs in by_customer.items():
                kept = [e for e in evs if e.ts >= since] if c in mine_customers else []
                if kept:
                    outcomes[c] = kept
            try:
                fn(mine, outcomes, now)
            except Exception as err:  # learning failing must never stop deciding
                self._alert(
                    report,
                    code="nightly.error",
                    key=f"{run.goal.goal_id}:{today.isoformat()}",
                    severity="warning",
                    goal_id=run.goal.goal_id,
                    message=f"Nightly learning failed ({type(err).__name__}); decisions use what was learned before.",
                )

    # --- 7. weekly -------------------------------------------------------------------------------

    def _weekly(self, now: datetime, local: datetime, report: TickReport) -> None:
        if self.analyst is None or self.strategist is None:
            return
        iso = local.isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
        due = local.weekday() > self.config.weekly_weekday or (
            local.weekday() == self.config.weekly_weekday and local.hour >= self.config.weekly_hour
        )
        if not due or self.cursor.weekly_on == week:
            return
        self.cursor.weekly_on = week
        monday = local.date() - timedelta(days=local.weekday())
        week_start = datetime.combine(monday, time(0), tzinfo=self._tz)
        orchestrator = Orchestrator(self.workspace, analyst=self.analyst, adjust=self.strategist)
        for goal, brief in self._goals_with_briefs():
            if goal.status not in (GoalStatus.ACTIVE, GoalStatus.PAUSED) or brief.created_at >= week_start:
                continue  # not running, or its brief is new this week (or this week's cycle already ran)
            try:
                report.weekly.append(orchestrator.weekly_cycle(goal, now=now))
            except Exception as err:  # a refused adjustment keeps the current brief; a person is told
                self._alert(
                    report,
                    code="weekly.refused",
                    key=f"{goal.goal_id}:{week}",
                    severity="warning",
                    goal_id=goal.goal_id,
                    message=f"The weekly review did not change the brief: {err}",
                )

    # --- helpers ---------------------------------------------------------------------------------

    def _start_day(self, today: date) -> None:
        """Load today's decisions once per local day, so repeated ticks skip what is decided."""
        if self._day is not None and self._day.day == today:
            return
        day = _Day(day=today)
        start = datetime.combine(today, time(0), tzinfo=self._tz)
        end = start + timedelta(days=1)
        goal_ids = sorted(g.goal_id for g in self.workspace.goals())
        for d in self.decisions.scan(since=start, until=end):
            # A NOTHING decision (every holdout customer's, for one) has no action and often no
            # bids; its derived id still names the goal, so a restart does not decide it again.
            day.add(d, _goal_of(d, goal_ids, self._tz))
        self._day = day

    def _today(self) -> _Day:
        assert self._day is not None  # set at the start of every tick
        return self._day

    def _goals_with_briefs(self) -> list[tuple[Goal, Brief]]:
        """Every goal whose latest brief is approved, with that brief."""
        out: list[tuple[Goal, Brief]] = []
        for goal in sorted(self.workspace.goals(), key=lambda g: g.goal_id):
            found = self.workspace.briefs(goal.goal_id)
            if not found:
                continue
            brief = max(found, key=lambda b: b.version)
            if brief.approved:
                out.append((goal, brief))
        return out

    def _running(self, now: datetime) -> list[_Running]:
        """Active goals inside their dates with an approved brief, each with its (cached) policy."""
        out: list[_Running] = []
        for goal, brief in self._goals_with_briefs():
            if not _sendable(goal, now, require_level=False):
                continue
            content = sorted(
                self.workspace.content(goal_id=goal.goal_id, approved_only=True), key=lambda c: c.content_id
            )
            key: tuple[object, ...] = (
                brief.brief_id,
                brief.version,
                brief.envelope,
                tuple(c.content_id for c in content),
            )
            cached = self._policies.get(goal.goal_id)
            if cached is None or cached[0] != key:
                cached = (key, self.policy_factory([brief], content))
                self._policies[goal.goal_id] = cached
            out.append(_Running(goal=goal, brief=brief, content=content, policy=cached[1]))
        return out

    def _fresh_states(self, customer_ids: Iterable[str], now: datetime) -> dict[str, CustomerState]:
        out: dict[str, CustomerState] = {}
        for cid in sorted(set(customer_ids)):
            s = self.states.get(cid)
            if s is not None:
                out[cid] = self.folder.refresh(s, now)
        return out

    def _alert(
        self,
        report: TickReport,
        *,
        code: str,
        key: str,
        severity: Severity,
        message: str,
        goal_id: str | None = None,
    ) -> None:
        report.alerts.append(
            Alert(
                alert_id=stable_id("alert", SOURCE, code, key),
                at=report.now,
                goal_id=goal_id,
                severity=severity,
                code=code,
                message=message,
            )
        )


def _sendable(goal: Goal, now: datetime, *, require_level: bool = True) -> bool:
    """An active goal inside its dates (and, for sending, out of shadow mode)."""
    if goal.status is not GoalStatus.ACTIVE:
        return False
    if now < goal.starts_at or (goal.ends_at is not None and now >= goal.ends_at):
        return False
    return not (require_level and goal.autonomy is AutonomyLevel.SHADOW)


def _seed(seed: int, goal_id: str, now: datetime) -> int:
    """A sampling seed fixed by (seed, goal, moment), so a repeated run samples the same way."""
    return int(stable_id("seed", seed, goal_id, now.isoformat())[5:13], 16)


class _RecordingEvents:
    """An `EventStore` that remembers the events (at or after `floor`) its scans hand out.

    `rebuild_states` folds what one scan of the log returns. Wrapping the store records exactly
    those events, so the runtime marks as folded what was folded, not what a second scan finds.
    """

    def __init__(self, inner: EventStore, floor: datetime) -> None:
        self.inner = inner
        self.floor = floor
        self.recent: list[Event] = []

    def append(self, events: Iterable[Event]) -> int:
        return self.inner.append(events)

    def for_customer(
        self, customer_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Event]:
        return self.inner.for_customer(customer_id, since=since, until=until)

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        for e in self.inner.scan(since=since, until=until):
            if e.ts >= self.floor:
                self.recent.append(e)
            yield e

    def for_decision(self, decision_id: str) -> list[Event]:
        return self.inner.for_decision(decision_id)


def _decision_id(goal_id: str, customer_id: str, trigger: Trigger, key: str) -> str:
    """The id the runtime gives a decision: one per (goal, customer, trigger, local day or event)."""
    return stable_id("dec", goal_id, customer_id, trigger.value, key)


def _derived_goal(
    decision: Decision, goal_ids: Sequence[str], tz: ZoneInfo, trigger_event_ids: Sequence[str] = ()
) -> str | None:
    """The goal whose derived id this decision carries, or None if it was not made by this runtime.

    Sweep and lapse ids are keyed by the local day the decision was made (`made_at` is the tick's
    `now`); event ids by the triggering event, so those need the customer's trigger event ids.
    """
    day = decision.made_at.astimezone(tz).date().isoformat()
    cid = decision.customer_id
    for gid in goal_ids:
        if decision.decision_id in (
            _decision_id(gid, cid, Trigger.SWEEP, day),
            _decision_id(gid, cid, Trigger.LAPSE, day),
        ):
            return gid
        if any(decision.decision_id == _decision_id(gid, cid, Trigger.EVENT, e) for e in trigger_event_ids):
            return gid
    return None


def _goal_of(
    decision: Decision, goal_ids: Sequence[str], tz: ZoneInfo, trigger_event_ids: Sequence[str] = ()
) -> str | None:
    """The goal a decision belongs to: its action's, else its best bid's, else its derived id's.

    Each goal has its own policy, so a bid names the decision's goal; the derived id (a few hashes
    per goal) is only worked out for decisions with neither, such as every holdout customer's.
    """
    if decision.action is not None:
        return decision.action.goal_id
    if decision.bids:
        return decision.bids[0].goal_id
    return _derived_goal(decision, goal_ids, tz, trigger_event_ids)


def _days(span: timedelta) -> str:
    hours = span.total_seconds() / 3600
    if hours % 24 == 0:
        n = int(hours // 24)
        return "1 day" if n == 1 else f"{n} days"
    return f"{hours:g} hours"


def build_worker(
    name: str = "worker-1",
    *,
    environ: Mapping[str, str] | None = None,
    start: datetime | None = None,
) -> Worker:
    """The production worker (`python -m autonomy.runtime.worker`): the runtime from the settings,
    with tick, send and webhook handlers, and a repeating tick job seeded at `start`.

    This is wiring for the entry point, so with no `start` it takes the wall clock, rounded down to
    the tick interval: a restart within one interval queues the same seed job (the id is its time),
    not a second chain of ticks. The tick leaves queued sends to the worker's send handler.
    """
    from dataclasses import replace
    from datetime import UTC

    from autonomy.runtime.settings import RuntimeSettings, build_runtime
    from autonomy.runtime.worker import (
        SEND_KIND,
        TICK_KIND,
        WEBHOOK_KIND,
        Worker,
        send_handler,
        tick_handler,
        tick_job,
        webhook_handler,
    )

    settings = RuntimeSettings.from_env(environ)
    runtime = build_runtime(settings, environ=environ)
    runtime.config = replace(runtime.config, run_send_jobs=False, worker=name)
    if runtime.queue is None or runtime.dispatcher is None:  # build_runtime always sets both
        raise RuntimeError("the runtime has no queue or dispatcher")
    every = settings.tick_every_minutes
    if start is None:
        wall = datetime.now(UTC)
        start = wall.replace(second=0, microsecond=0) - timedelta(minutes=wall.minute % every)
    runtime.queue.enqueue(tick_job(start, every_minutes=every))
    events = runtime.events
    return Worker(
        queue=runtime.queue,
        handlers={
            TICK_KIND: tick_handler(runtime.tick, runtime.queue),
            SEND_KIND: send_handler(runtime.dispatcher, runtime.decisions, runtime.states),
            WEBHOOK_KIND: webhook_handler(runtime.dispatcher.adapters, events.append),
        },
        name=name,
    )
