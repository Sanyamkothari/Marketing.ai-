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
   loop). A policy built in this process (the first tick after a start, or a materially new brief)
   first replays `replay_lookback` of the decision log and events through the same hook, so what
   it learned survives restarts. When `lift_guards` is set, the negative-lift breaker
   (`autonomy.channels.breakers.negative_lift`) then reads each acting goal's proof over closed
   outcome windows and pauses a goal that is confidently losing money against the holdout.
7. **Weekly**, once a week, when an analyst and a strategist are configured: the Orchestrator's
   weekly cycle (Analyst report, then the Strategist's narrowing of the brief).

**Optional wiring**, all off by default so a runtime built without it behaves as before:

- `state_enricher(states, events_by_customer, now) -> states` runs on the states of each goal's
  candidates right before deciding (signals, scores from another engine). It never changes the
  stored state, and if it raises, the goal decides on the plain states and an alert says so.
- `mandates` (a `MandateSource`) puts goals under signed mandates (`autonomy.mandate`). A goal the
  source governs acts only under an active, verified mandate; with none, or an expired, revoked
  or unsigned one, it does not decide (shadow goals still do, as they never act) and a critical
  alert says why. Every ACT decision is checked again with `check_action` right before it is
  batched or sent, with the mandate's spend so far; a violation blocks that send, is noted on the
  decision (`blocked_by`, reason) and raises an alert. Spend is paced by the mandate's
  `TokenBucket` (from `max_daily_spend`): a send the bucket cannot cover waits for another day.
- `gateway` (an `autonomy.channels.gateway.DispatchGateway` with a dispatcher attached) sends
  every decision through its send-time compliance gate: consent proof, do-not-disturb registers,
  country quiet hours, rate limits and carrier failover. A refusal is a skipped send and an
  alert; held decisions come back as `gateway_send_decision` jobs.
- `business_id` and `data_gate` tie the runtime to one shop's connected data
  (`autonomy.runtime.settings.build_runtime` with connectors). Each tick asks
  `data_gate(now)` first; a `DataHold` (the shop's Shopify or Klaviyo data is stale, still loading
  its history, or needs reconnecting) holds every decision and approved batch of the business for
  that tick, and queued sends wait too (`data_send_gate`). The connector's incident desk raises the
  one alert for the problem; the runtime adds none of its own. `request_rebuild` (the connectors'
  `history_hook`, and identity links that changed) makes the next tick rebuild every state from the
  log, at most once per `RuntimeConfig.rebuild_every`. `before_tick` hooks run first in every tick
  (the connector send routing re-reads which tools are connected there, so a connection made, lost
  or reconnected changes the providers without a restart).

**When an engine fails**, the tick goes on with what is still safe (the founder's table,
`docs/v2/FAILURE_ISOLATION.md`). An unreadable event feed folds nothing, keeps the watermark and
decides on the states held (`ingest.unavailable`); a feed with no new customer event for
`ingest_stale_after` raises `ingest.stale`. A mandate store that cannot be read leaves the goal under
the last active, certified mandate it read, until that one expires (`mandate.store_unavailable`). A
policy that raises logs a DO_NOTHING decision for that customer (`LEFT_ALONE`); one that cannot be
built skips only its goal (`decide.error`). A spend source that raises leaves the last budget known,
less the goal's own sends since, and never more (`spend.unavailable`).

Every alert and briefing a tick produces is written to the runtime's `AlertStore` and
`BriefingStore` (`autonomy.runtime.stores`), so a worker's ticks reach the API. Spend per goal
(budget left and the spend-pace breaker) comes from a `SpendLedger` over the event log: channel
cost per SEND plus offer cost per redemption; planned spend paces the goal's budget evenly over its
period (`EvenPacing`). Queued sends belong to the worker (`autonomy.runtime.worker`); see
`RuntimeConfig.run_send_jobs`. The full guide is `docs/v2/RUNTIME.md`.

Nothing here reads the clock: every step takes `now`, so the same ticks over the same events give
the same decisions. Decision ids are derived from (goal, customer, trigger), which is what makes a
repeated tick a no-op. No LLM runs per customer: the policy is maths, and the Conversation agent
only runs on replies.

Each goal gets its own policy, built by `policy_factory([brief], content)` and rebuilt only when
the brief changes materially (audience, envelope, horizon, objective, follow-ups) or its approved
content changes; a new version that only restates the same plan keeps the policy and what it
learned. One policy per goal keeps autonomy levels apart: a goal in shadow can never win a
customer's attention away from a goal on autopilot. Customers are still protected across goals:
a customer contacted (or held for approval) by one goal in a tick is not contacted by another in
the same tick, and the contact caps in `decide()` read the SENDs every goal made.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, Field

from autonomy.agents.conversation import Conversation, ReplyOutcome
from autonomy.agents.orchestrator import AdjustFn, AnalystFn, Orchestrator, WeeklyOutcome
from autonomy.channels.breakers import (
    DEFAULT_GUARDS,
    BreakerResult,
    Guards,
    LiftGuards,
    evaluate_goals,
    negative_lift,
)
from autonomy.channels.dispatcher import (
    Dispatcher,
    DispatchReport,
    DispatchResult,
    SkipReason,
)
from autonomy.contracts import (
    NO_OFFER,
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
from autonomy.decide.bidders import DEFAULT_CHANNEL_COSTS
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
from autonomy.mandate.enforce import ActionCosts, SpendToDate, TokenBucket, check_action, check_bid
from autonomy.mandate.models import Mandate, MandateStatus
from autonomy.proof.rewards import Costs
from autonomy.runtime.batches import (
    BatchStore,
    BatchTransitionError,
    InMemoryBatchStore,
    UnknownBatchError,
    approve,
    mark_sent,
    reject,
)
from autonomy.runtime.stores import (
    AlertStore,
    BriefingStore,
    InMemoryAlertStore,
    InMemoryBriefingStore,
)
from autonomy.runtime.worker import (
    GATEWAY_KIND,
    SEND_KIND,
    JobHandler,
    Worker,
    gateway_handler,
    send_handler,
    worker_owns_sends,
)

if TYPE_CHECKING:
    from autonomy.channels.gateway import DispatchGateway

__all__ = [
    "DATA_HOLD_PREFIX",
    "INGEST_TYPES",
    "LEFT_ALONE",
    "MANDATE_PACED",
    "MATERIAL_BRIEF_FIELDS",
    "TRIGGER_TYPES",
    "AutonomyRuntime",
    "BreakersFn",
    "DataGate",
    "DataHold",
    "EvenPacing",
    "MandateBook",
    "MandateSource",
    "PersonReadsReplies",
    "PolicyFactory",
    "RuntimeConfig",
    "RuntimeCursor",
    "SpendFn",
    "SpendLedger",
    "SplitMismatchError",
    "StandardBreakers",
    "StateEnricher",
    "TickReport",
    "Trigger",
    "build_worker",
    "check_split",
    "make_batch",
    "material_brief",
    "standard_breakers",
    "tick_slot",
]

PolicyFactory = Callable[[Sequence[Brief], Sequence[ContentItem]], Policy]
"""(briefs, approved content) -> a policy. The runtime passes one goal's brief and content."""

BreakersFn = Callable[[Sequence[Brief], Sequence[Event], datetime], BreakerResult]
"""(briefs of running goals, recent events, now) -> alerts and what to pause."""

SpendFn = Callable[[str, datetime], float]
"""(goal_id, now) -> money spent on the goal so far, in its currency."""

GroupFn = Callable[[CustomerState], CustomerState]
"""state -> the same state with its proof group set (see `autonomy.proof.split.group_assigner`)."""

StateEnricher = Callable[[list[CustomerState], Mapping[str, list[Event]], datetime], list[CustomerState]]
"""(states of one goal's candidates, their recent events by customer, now) -> the states to decide
on. Runs right before deciding; the result is matched back by customer id, and a customer it leaves
out is decided on its plain state."""

Severity = Literal["info", "warning", "critical"]

DATA_HOLD_PREFIX = "data_not_fresh: "
"""The reason a queued send is held while the business's connected data cannot be trusted."""


@dataclass(frozen=True)
class DataHold:
    """Why the business must not act right now, in plain words, and alerts the check raised (if any)."""

    reasons: tuple[str, ...]
    alerts: tuple[Alert, ...] = ()


DataGate = Callable[[datetime], DataHold | None]
"""now -> a `DataHold` while the connected data cannot be trusted, else None (see `build_runtime`)."""
TickHook = Callable[[datetime], object]
"""Run at the start of every tick (e.g. re-reading which tools are connected)."""

MANDATE_PACED = "mandate_paced"
"""Noted on a decision the mandate's spend pacer (`TokenBucket`) held back: allowed, but not today."""

MANDATE_MISSING = "mandate_missing"
MANDATE_INACTIVE = "mandate_not_active"
MANDATE_UNVERIFIED = "mandate_unverified"
MANDATE_ERROR = "mandate_error"

ENRICH_PER_CUSTOMER_MAX = 200
"""Up to this many candidates, the enricher's events are read customer by customer; above, one scan."""

_DISPATCHER_SKIPS: frozenset[str] = frozenset(r.value for r in SkipReason)
"""The dispatcher's own skip codes. Any other skip reason on the gateway path is the gateway's refusal."""

INGEST_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.SIGNUP,
        EventType.VISIT,
        EventType.VIEW,
        EventType.CART,
        EventType.PURCHASE,
        EventType.REFUND,
    }
)
"""What customers do on their own, which only the shop's event feed brings in. The staleness check
(`RuntimeConfig.ingest_stale_after`) watches these, so the runtime's own sends never make a dead
feed look alive."""

LEFT_ALONE = "Left alone: the decision engine is unavailable"
"""The reason on the DO_NOTHING decision logged for a customer whose policy call raised."""

TRIGGER_TYPES: frozenset[EventType] = frozenset({EventType.REPLY, EventType.CART})
"""Events that get a customer a decision within one tick, not at the next daily sweep."""

TIME_OPS: frozenset[str] = frozenset({"days_since_gt", "days_since_lt"})
"""Audience filters that change with time alone; crossing one is the lapse trigger."""

MATERIAL_BRIEF_FIELDS: frozenset[str] = frozenset(
    {"goal_id", "objective", "audience", "horizon_days", "envelope", "follow_ups"}
)
"""The parts of a brief a policy is built from. A new version that changes none of them (only its
id, version, rationale, author or approval) keeps the goal's policy and what it has learned."""

MAX_SEND_JOBS_PER_TICK = 10_000
SOURCE = "runtime"
DEFAULT_HORIZON_DAYS = 14


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
    run_send_jobs: bool | None = None
    """Whether the tick runs queued sends that are due. Send jobs have one owner, a `Worker` with a
    send handler: None (the default) runs them through a hosted worker only while no worker in this
    process owns this runtime's dispatcher's sends; False never does (production, where the worker
    process owns them); True always does."""
    replay_lookback: timedelta = timedelta(days=30)
    """How much of the decision log a newly built policy replays through `nightly()`."""
    plan_days: int = 30
    """A goal with no end date has its budget paced evenly over this many days from its start."""
    holdout_share: float | None = None
    baseline_share: float | None = None
    """The business's one proof split. When set, a brief whose envelope asks for other shares is
    refused (its goal does not run, and a critical alert says why). None: not checked."""
    seed: int = 0
    worker: str = "runtime"
    enrich_lookback: timedelta = timedelta(days=30)
    """How far back the events handed to the `state_enricher` reach."""
    proof_lookback: timedelta = timedelta(days=120)
    """How much of a goal's decision log the nightly negative-lift check reads (from its start at most)."""
    mandate_burst_share: float = 0.25
    """The share of a mandate's daily cap its spend pacer lets go at once (see `TokenBucket`)."""
    rebuild_every: timedelta = timedelta(minutes=15)
    """A requested rebuild (`request_rebuild`: connector history landed, identity links changed) runs at
    most this often, so a backfill landing chunk after chunk rebuilds a few times, not every page."""
    ingest_stale_after: timedelta | None = timedelta(days=2)
    """Raise an `ingest.stale` alert when the newest customer event (`INGEST_TYPES`) is older than
    this, counted from the process's first fold at most: the feed has probably gone quiet. The
    runtime keeps deciding on the states it holds. None turns the check off."""

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
        if self.replay_lookback < timedelta(0):
            raise ValueError("replay_lookback cannot be negative")
        if self.plan_days < 1:
            raise ValueError("plan_days must be at least 1")
        for name in ("holdout_share", "baseline_share"):
            share = getattr(self, name)
            if share is not None and not 0 <= share <= 0.5:
                raise ValueError(f"{name} is a share from 0 to 0.5")
        if (self.holdout_share is None) != (self.baseline_share is None):
            raise ValueError("set both holdout_share and baseline_share, or neither")
        if self.enrich_lookback < timedelta(0) or self.proof_lookback < timedelta(0):
            raise ValueError("enrich_lookback and proof_lookback cannot be negative")
        if not 0 < self.mandate_burst_share < 1:
            raise ValueError("mandate_burst_share is between 0 and 1, exclusive")
        if self.ingest_stale_after is not None and self.ingest_stale_after <= timedelta(0):
            raise ValueError("ingest_stale_after must be positive, or None")


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
    replayed_goals: list[str] = field(default_factory=list)
    """Goals whose policy was built this tick and replayed the decision log to relearn."""
    halted_goals: list[str] = field(default_factory=list)
    """Goals that did not act this tick because their mandate is missing, inactive or unsigned."""
    mandate_blocked: list[str] = field(default_factory=list)
    """Decisions a mandate check stopped (violations, or the spend pacer) before batching or sending."""
    data_held: list[str] = field(default_factory=list)
    """Why the business's decisions and approved batches were held this tick (stale connected data)."""

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
            "halted_goals": list(self.halted_goals),
            "mandate_blocked": len(self.mandate_blocked),
            "data_held": list(self.data_held),
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


@dataclass(frozen=True)
class StandardBreakers:
    """The circuit breakers of `autonomy.channels.breakers`, per goal, with its own stop rules.

    Without `spend_fn` and `planned_fn` spend pace is never judged; without baselines the
    unsubscribe multiple is never judged. A breaker never guesses. The runtime fills a missing
    `spend_fn` and `planned_fn` with its own `SpendLedger` and `EvenPacing` (see `filled`), so the
    spend-pace rule works without any wiring.
    """

    baselines: Mapping[str, float] = field(default_factory=dict)
    spend_fn: SpendFn | None = None
    planned_fn: SpendFn | None = None
    guards: Guards = DEFAULT_GUARDS

    def filled(self, *, spend_fn: SpendFn | None, planned_fn: SpendFn | None) -> StandardBreakers:
        """These breakers with any missing spend or planned-spend source taken from the arguments."""
        return replace(
            self,
            spend_fn=self.spend_fn if self.spend_fn is not None else spend_fn,
            planned_fn=self.planned_fn if self.planned_fn is not None else planned_fn,
        )

    def __call__(self, briefs: Sequence[Brief], recent: Sequence[Event], now: datetime) -> BreakerResult:
        spend_fn, planned_fn = self.spend_fn, self.planned_fn
        spend = {b.goal_id: spend_fn(b.goal_id, now) for b in briefs} if spend_fn else {}
        planned = {b.goal_id: planned_fn(b.goal_id, now) for b in briefs} if planned_fn else {}
        return evaluate_goals(briefs, recent, dict(self.baselines), spend, planned, now, guards=self.guards)


def standard_breakers(
    *,
    baselines: Mapping[str, float] | None = None,
    spend_fn: SpendFn | None = None,
    planned_fn: SpendFn | None = None,
    guards: Guards = DEFAULT_GUARDS,
) -> StandardBreakers:
    """The standard circuit breakers (see `StandardBreakers`)."""
    return StandardBreakers(
        baselines=dict(baselines or {}), spend_fn=spend_fn, planned_fn=planned_fn, guards=guards
    )


# --- mandates -------------------------------------------------------------------------------------


@runtime_checkable
class MandateSource(Protocol):
    """Where the runtime finds each goal's mandate (`autonomy.mandate.Mandate`).

    `governs(goal_id)` says whether the goal must act under a mandate at all; `lookup(goal_id)` is
    the mandate that currently stands for it (latest version, whatever its status), or None; and
    `problems(mandate, now)` is every reason its signatures do not authorise it (empty when they do,
    e.g. `autonomy.mandate.verify_mandate`). The runtime checks status and validity itself.
    """

    def governs(self, goal_id: str) -> bool: ...

    def lookup(self, goal_id: str) -> Mandate | None: ...

    def problems(self, mandate: Mandate, now: datetime) -> list[str]: ...


class MandateBook:
    """An in-memory `MandateSource`: the mandates you give it, latest version per goal.

    A goal is governed when a mandate covers it, when it is in `required_goals`, or always with
    `require_all` (then a goal with no mandate does not act). `verify` checks signatures, e.g.
    `lambda m: verify_mandate(m, policy, keys)`; without it, a mandate's status is trusted, which
    is right for a store that only ever holds verified mandates.
    """

    def __init__(
        self,
        mandates: Iterable[Mandate] = (),
        *,
        required_goals: Iterable[str] = (),
        require_all: bool = False,
        verify: Callable[[Mandate], list[str]] | None = None,
    ) -> None:
        self._mandates: dict[tuple[str, int], Mandate] = {}
        self.required_goals = frozenset(required_goals)
        self.require_all = require_all
        self.verify = verify
        for m in mandates:
            self.put(m)

    def put(self, mandate: Mandate) -> None:
        """Add a mandate, or replace the same version (a revocation changes only its status)."""
        self._mandates[(mandate.mandate_id, mandate.version)] = mandate

    def governs(self, goal_id: str) -> bool:
        if self.require_all or goal_id in self.required_goals:
            return True
        return any(goal_id in m.goal_ids for m in self._mandates.values())

    def lookup(self, goal_id: str) -> Mandate | None:
        covering = [m for m in self._mandates.values() if goal_id in m.goal_ids]
        if not covering:
            return None
        return max(covering, key=lambda m: (m.version, m.valid_from, m.mandate_id))

    def problems(self, mandate: Mandate, now: datetime) -> list[str]:  # noqa: ARG002
        return [] if self.verify is None else list(self.verify(mandate))


# --- spend ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _OfferSend:
    """A SEND that carried an offer: it is paid for when a purchase in its window uses the offer."""

    ts: datetime
    decision_id: str
    goal_id: str
    offer_id: str


class SpendLedger:
    """Money spent per goal, read from the event log: channel cost per SEND, offer cost per redemption.

    A SEND costs its channel's price (`channel_costs`, by default the bidders' own
    `DEFAULT_CHANNEL_COSTS`; a channel missing from the table costs what the decision's chosen bid
    said). An offer costs money only when it is used: a PURCHASE whose `attrs["offer_id"]` names the
    offer of a decision sent to that customer within the goal's outcome window (`horizon_days` of
    its brief) pays that offer once, charged at the purchase. The price is the purchase's
    `attrs["offer_cost"]` if the shop reported one, else the brief's `cost_per_redemption`. The
    latest qualifying send is charged (last touch), and each decision pays its offer at most once.

    The runtime feeds it every event it folds (`observe`) and rebuilds it with state, so it is
    always derived from the log: idempotent by event id, and nothing to persist. Use it as a
    `SpendFn`: `ledger(goal_id, now)`.
    """

    def __init__(
        self,
        decisions: DecisionLog,
        workspace: Workspace,
        *,
        channel_costs: Mapping[Channel, float] | None = None,
    ) -> None:
        self.decisions = decisions
        self.workspace = workspace
        self.channel_costs: dict[Channel, float] = dict(
            DEFAULT_CHANNEL_COSTS if channel_costs is None else channel_costs
        )
        self._seen: set[str] = set()
        self._charges: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        self._offer_sends: dict[str, list[_OfferSend]] = defaultdict(list)
        self._unmatched: dict[str, list[Event]] = defaultdict(list)
        self._paid: set[str] = set()
        self._briefs: dict[str, Brief] | None = None

    def reset(self) -> None:
        """Forget everything; the caller replays the log next (the runtime's full rebuild)."""
        self._seen.clear()
        self._charges.clear()
        self._offer_sends.clear()
        self._unmatched.clear()
        self._paid.clear()
        self._briefs = None

    def refresh(self) -> None:
        """Read the briefs again (outcome windows and offer prices) the next time they are needed."""
        self._briefs = None

    def adopt(self, other: SpendLedger) -> None:
        """Take over what `other` (a ledger just rebuilt from the log) knows, keeping this object.

        The runtime rebuilds into a fresh ledger and adopts it only once the scan has finished, so a
        feed that fails half way leaves the spend it knew in place, never an empty ledger."""
        self._seen = other._seen
        self._charges = other._charges
        self._offer_sends = other._offer_sends
        self._unmatched = other._unmatched
        self._paid = other._paid
        self._briefs = other._briefs

    def observe(self, events: Iterable[Event]) -> None:
        for e in events:
            self.observe_one(e)

    def observe_one(self, e: Event) -> None:
        if e.type is EventType.SEND:
            self._send(e)
        elif e.type is EventType.PURCHASE:
            self._purchase(e)

    def spent(self, goal_id: str, now: datetime) -> float:
        """Channel and offer cost charged to the goal at or before `now`."""
        return math.fsum(amount for ts, amount in self._charges.get(goal_id, ()) if ts <= now)

    def __call__(self, goal_id: str, now: datetime) -> float:
        return self.spent(goal_id, now)

    # --- internals ---

    def _send(self, e: Event) -> None:
        if e.event_id in self._seen:
            return
        self._seen.add(e.event_id)
        goal = e.attrs.get("goal_id")
        offer = e.attrs.get("offer_id")
        priced = e.channel is not None and e.channel in self.channel_costs
        decision: Decision | None = None
        if (not isinstance(goal, str) or not isinstance(offer, str) or not priced) and e.decision_id:
            decision = self.decisions.get(e.decision_id)
        if decision is not None and decision.action is not None:
            goal = goal if isinstance(goal, str) else decision.action.goal_id
            offer = offer if isinstance(offer, str) else decision.action.offer_id
        if not isinstance(goal, str):
            return  # not a send any goal made
        if priced:
            assert e.channel is not None
            cost = self.channel_costs[e.channel]
        else:
            chosen = None
            if decision is not None and decision.action is not None:
                chosen = next((b for b in decision.bids if b.action == decision.action), None)
            cost = max(chosen.channel_cost, 0.0) if chosen is not None else 0.0
        if cost > 0:
            self._charges[goal].append((e.ts, cost))
        if isinstance(offer, str) and offer != NO_OFFER.offer_id and e.decision_id:
            self._offer_sends[e.customer_id].append(_OfferSend(e.ts, e.decision_id, goal, offer))
            waiting = self._unmatched.get(e.customer_id)
            if waiting:
                self._unmatched[e.customer_id] = [p for p in waiting if not self._match(p)]

    def _purchase(self, e: Event) -> None:
        offer = e.attrs.get("offer_id")
        if not isinstance(offer, str) or offer == NO_OFFER.offer_id or e.event_id in self._seen:
            return
        self._seen.add(e.event_id)
        if not self._match(e):
            # Its send may still arrive (a late event); it is matched then.
            self._unmatched[e.customer_id].append(e)

    def _match(self, purchase: Event) -> bool:
        offer = purchase.attrs.get("offer_id")
        candidates = [
            s
            for s in self._offer_sends.get(purchase.customer_id, ())
            if s.offer_id == offer
            and s.decision_id not in self._paid
            and s.ts <= purchase.ts <= s.ts + timedelta(days=self._horizon(s.goal_id))
        ]
        if not candidates:
            return False
        send = max(candidates, key=lambda s: (s.ts, s.decision_id))
        reported = purchase.attrs.get("offer_cost")
        if isinstance(reported, int | float) and not isinstance(reported, bool):
            cost = max(float(reported), 0.0)
        else:
            cost = self._offer_price(send.goal_id, send.offer_id)
        self._paid.add(send.decision_id)
        if cost > 0:
            self._charges[send.goal_id].append((purchase.ts, cost))
        return True

    def _brief(self, goal_id: str) -> Brief | None:
        if self._briefs is None:
            latest: dict[str, Brief] = {}
            for b in self.workspace.briefs():
                known = latest.get(b.goal_id)
                if known is None or b.version > known.version:
                    latest[b.goal_id] = b
            self._briefs = latest
        return self._briefs.get(goal_id)

    def _horizon(self, goal_id: str) -> int:
        brief = self._brief(goal_id)
        return brief.horizon_days if brief is not None else DEFAULT_HORIZON_DAYS

    def _offer_price(self, goal_id: str, offer_id: str) -> float:
        brief = self._brief(goal_id)
        if brief is None:
            return 0.0
        found = next((o for o in brief.envelope.offers if o.offer_id == offer_id), None)
        return found.cost_per_redemption if found is not None else 0.0


class EvenPacing:
    """Planned spend to date: the goal's budget spread evenly from its start to its end.

    The budget is the smaller of the goal's and its latest brief's. A goal with no end date is
    paced over `plan_days` (the "$5k this month" a marketer usually means). Before the start the
    plan is 0; after the end, the whole budget. This is the spend-pace breaker's denominator.
    """

    def __init__(self, workspace: Workspace, *, plan_days: int = 30) -> None:
        if plan_days < 1:
            raise ValueError("plan_days must be at least 1")
        self.workspace = workspace
        self.plan_days = plan_days

    def __call__(self, goal_id: str, now: datetime) -> float:
        goal = next((g for g in self.workspace.goals() if g.goal_id == goal_id), None)
        if goal is None:
            return 0.0
        budget = goal.budget
        briefs = self.workspace.briefs(goal_id)
        if briefs:
            budget = min(budget, max(briefs, key=lambda b: b.version).envelope.budget)
        end = goal.ends_at if goal.ends_at is not None else goal.starts_at + timedelta(days=self.plan_days)
        span = (end - goal.starts_at).total_seconds()
        if span <= 0:
            return budget if now >= goal.starts_at else 0.0
        share = (now - goal.starts_at).total_seconds() / span
        return budget * min(1.0, max(0.0, share))


# --- the business's one split ----------------------------------------------------------------------


class SplitMismatchError(ValueError):
    """A brief asks for holdout or baseline shares other than the business's one split."""


def check_split(brief: Brief, holdout_share: float, baseline_share: float) -> None:
    """Refuse a brief whose envelope asks for a different proof split than the business uses.

    Every customer is split once for the whole business (`autonomy.proof.split`), so proof for one
    goal can be compared with proof for another, and a customer is never holdout for one goal and
    treated by another. A brief cannot change that split; the message says what to do instead.
    """
    env = brief.envelope
    if math.isclose(env.holdout_share, holdout_share, abs_tol=1e-9) and math.isclose(
        env.baseline_share, baseline_share, abs_tol=1e-9
    ):
        return
    raise SplitMismatchError(
        f"Brief {brief.brief_id} (goal {brief.goal_id}, version {brief.version}) asks for a "
        f"{env.holdout_share:.0%} holdout and a {env.baseline_share:.0%} baseline, but this business "
        f"splits every customer once: {holdout_share:.0%} holdout and {baseline_share:.0%} baseline. "
        "Set the brief's envelope to those shares, or change AUTONOMY_HOLDOUT_SHARE and "
        "AUTONOMY_BASELINE_SHARE for the whole business."
    )


def material_brief(brief: Brief) -> str:
    """The parts of a brief a policy is built from, as stable JSON (see `MATERIAL_BRIEF_FIELDS`)."""
    return json.dumps(brief.model_dump(mode="json", include=set(MATERIAL_BRIEF_FIELDS)), sort_keys=True)


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
        planned_fn: SpendFn | None = None,
        assign_group: GroupFn | None = None,
        config: RuntimeConfig | None = None,
        cursor: RuntimeCursor | None = None,
        alerts: AlertStore | None = None,
        briefings: BriefingStore | None = None,
        channel_costs: Mapping[Channel, float] | None = None,
        state_enricher: StateEnricher | None = None,
        mandates: MandateSource | None = None,
        gateway: DispatchGateway | None = None,
        lift_guards: LiftGuards | None = None,
        business_id: str | None = None,
        data_gate: DataGate | None = None,
        before_tick: Sequence[TickHook] = (),
    ) -> None:
        """`conversation` defaults to one over `text_signals`, else over `PersonReadsReplies`: STOP
        is always honoured. `strategist` is the weekly adjustment, e.g. `Strategist.adjust_brief`;
        the weekly cycle runs only when both it and `analyst` are given.

        `spend_fn` defaults to a `SpendLedger` over the log (priced with `channel_costs`) and
        `planned_fn` to `EvenPacing`; `StandardBreakers` without their own spend sources get these.
        `alerts` and `briefings` default to in-memory stores; production passes SQL ones.

        The rest is optional and off by default (see the module docstring): `state_enricher` runs
        on each goal's candidate states right before deciding; `mandates` puts goals under signed
        mandates; `gateway` sends through a `DispatchGateway` (its attached dispatcher becomes
        `dispatcher`, so pauses reach it); `lift_guards` turns on the nightly negative-lift
        breaker; `business_id`, `data_gate` and `before_tick` tie it to a connected shop."""
        if gateway is not None:
            if gateway.dispatcher is None:
                raise ValueError("attach a dispatcher to the gateway (attach_dispatcher) before using it")
            if dispatcher is not None and dispatcher is not gateway.dispatcher:
                raise ValueError("pass the gateway's own dispatcher, or none: the gateway sends through it")
            dispatcher = gateway.dispatcher
        self.events = events
        self.states = states
        self.decisions = decisions
        self.workspace = workspace
        self.folder = folder
        self.policy_factory = policy_factory
        self.queue = queue
        self.dispatcher = dispatcher
        self.gateway = gateway
        self.state_enricher = state_enricher
        self.mandates = mandates
        self.lift_guards = lift_guards
        self.business_id = business_id
        self.data_gate = data_gate
        self.before_tick: list[TickHook] = list(before_tick)
        self._rebuild_requested = False
        self._last_rebuild: datetime | None = None
        self._hold_seen: tuple[datetime, DataHold | None] | None = None
        """The data gate's last answer and its minute: queued sends reuse it, not one check each."""
        self.channel_costs: dict[Channel, float] = dict(
            DEFAULT_CHANNEL_COSTS if channel_costs is None else channel_costs
        )
        self.batches: BatchStore = batches if batches is not None else InMemoryBatchStore()
        self.breakers_fn = breakers_fn
        self.conversation = conversation or Conversation(text_signals or PersonReadsReplies())
        self.analyst = analyst
        self.strategist = strategist
        self.assign_group = assign_group
        self.config = config or RuntimeConfig()
        self.cursor = cursor or RuntimeCursor()
        self.alert_store: AlertStore = alerts if alerts is not None else InMemoryAlertStore()
        self.briefing_store: BriefingStore = briefings if briefings is not None else InMemoryBriefingStore()
        self.spend_ledger: SpendLedger | None = None
        if spend_fn is None:
            self.spend_ledger = SpendLedger(decisions, workspace, channel_costs=channel_costs)
            spend_fn = self.spend_ledger
        self.spend_fn: SpendFn = spend_fn
        self.planned_fn: SpendFn = (
            planned_fn if planned_fn is not None else EvenPacing(workspace, plan_days=self.config.plan_days)
        )
        if isinstance(breakers_fn, StandardBreakers):
            self.breakers_fn = breakers_fn.filled(spend_fn=self.spend_fn, planned_fn=self.planned_fn)
        self._tz = ZoneInfo(self.config.timezone)
        self._seen: dict[str, datetime] = {}
        """Ids (and times) of events already folded, near the watermark."""
        self._warm = False
        self._day: _Day | None = None
        self._policies: dict[str, tuple[tuple[object, ...], Policy]] = {}
        self._paused_channels: set[Channel] = set()
        self._buckets: dict[tuple[str, int], TokenBucket] = {}
        """The spend pacer per mandate version. Rebuilt after a restart from the last day's spend."""
        self._newest_ingest: datetime | None = None
        """The time of the newest customer event (`INGEST_TYPES`) folded, for the staleness alert."""
        self._watching_since: datetime | None = None
        """The first successful fold in this process: the staleness clock never starts earlier."""
        self._last_mandate: dict[str, tuple[Mandate, datetime]] = {}
        """goal_id -> the last active, certified mandate the store gave, and when it was read."""
        self._spend_known: dict[str, tuple[float, float, datetime]] = {}
        """goal_id -> (spend, budget left, when) at the last good read of the spend source."""
        self._ledger_read_at: datetime | None = None
        """When a process that has not ticked last read the log into its spend ledger (`send_gate`)."""

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

    def record(self, *, alerts: Iterable[Alert] = (), briefings: Iterable[Briefing] = ()) -> None:
        """Store alerts and briefings made outside a tick (a daily briefing, an operator's note)."""
        self.alert_store.put(alerts)
        self.briefing_store.put(briefings)

    def request_rebuild(self, *_: object) -> None:
        """Rebuild every state from the log on the next tick (at most once per `rebuild_every`).

        The connectors' `history_hook` (history older than the late window landed) and the identity
        linker's `on_change` (a Klaviyo profile was linked to a Shopify customer) both call this;
        it takes and ignores their arguments."""
        self._rebuild_requested = True

    def data_send_gate(self, decision: Decision, now: datetime) -> str | None:  # noqa: ARG002
        """A `SendGate` holding queued sends while the connected data cannot be trusted.

        The answer is kept for the minute it was asked in (the tick's own check counts), so a sweep
        that sends thousands of queued messages checks the shop's data once, not once per message."""
        if self.data_gate is None:
            return None
        minute = now.replace(second=0, microsecond=0)
        seen = self._hold_seen
        if seen is not None and seen[0] == minute:
            hold = seen[1]
        else:
            try:
                hold = self.data_gate(now)
            except Exception as err:  # cannot tell: hold rather than send on data nobody checked
                return f"{DATA_HOLD_PREFIX}the data check failed ({type(err).__name__})"
            self._hold_seen = (minute, hold)
        return None if hold is None else DATA_HOLD_PREFIX + "; ".join(hold.reasons)

    def check_brief(self, brief: Brief) -> None:
        """Raise `SplitMismatchError` if the brief asks for another proof split than the business's.

        The API calls this before it approves a brief; the tick also refuses to run such a goal."""
        if self.config.holdout_share is not None and self.config.baseline_share is not None:
            check_split(brief, self.config.holdout_share, self.config.baseline_share)

    # --- the tick ------------------------------------------------------------------------------

    def tick(self, now: datetime) -> TickReport:
        """One pass of the loop at `now` (timezone-aware). Calling it twice at the same `now` is safe."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        report = TickReport(now=now)
        local = now.astimezone(self._tz)
        self._start_day(local.date())
        self._run_hooks(now, report)
        if self.spend_ledger is not None:
            self.spend_ledger.refresh()  # a weekly brief may have moved a window or an offer price

        # The nightly rebuild is this tick's fold, not a step after deciding: events that arrive
        # while the tick runs (the API stores webhooks at any time) must still reach the replies
        # and triggers below, and a rebuild late in the tick would fold them out of their sight.
        nightly_due = self._nightly_due(local)
        requested = self._rebuild_due(now)
        if requested:
            self._rebuild_requested = False  # a request made while this rebuild runs waits for the next
        new_events = self._fold(
            now, report, rebuild=(nightly_due and self.config.nightly_rebuild) or requested
        )
        if requested and not report.rebuilt:
            self._rebuild_requested = True  # the log could not be read: try again next tick
        self._handle_replies(new_events, now, report)
        self._run_breakers(now, report)
        self._expire_batches(now, report)
        held = self._data_hold(now, report)
        if not held:
            self._send_approved(now, report)
        self._run_send_jobs(now, report)
        if not held:
            self._decide(now, local, new_events, report)
        if nightly_due:
            self._nightly(now, local, report)
        self._weekly(now, local, report)

        self.cursor.last_tick = now if self.cursor.last_tick is None else max(self.cursor.last_tick, now)
        # Written last, once: every process (the API, an operator) reads them from here.
        self.alert_store.put(report.alerts)
        self.briefing_store.put(report.briefings)
        return report

    # --- 0. hooks and the data gate -------------------------------------------------------------

    def _run_hooks(self, now: datetime, report: TickReport) -> None:
        """`before_tick` hooks; one that raises is an alert (once a day each), never a stopped tick."""
        for hook in self.before_tick:
            try:
                hook(now)
            except Exception as err:
                name = getattr(hook, "__qualname__", type(hook).__name__)
                day = self._today()
                if ("runtime.hook_error", name) in day.alerted:
                    continue
                day.alerted.add(("runtime.hook_error", name))
                self._alert(
                    report,
                    code="runtime.hook_error",
                    key=f"{name}:{day.day.isoformat()}",
                    severity="warning",
                    message=(
                        f"A start-of-tick step ({name}) failed ({type(err).__name__}: {err}); the tick "
                        "went on with what it had."
                    )[:1000],
                )

    def _data_hold(self, now: datetime, report: TickReport) -> bool:
        """True when the business's connected data cannot be trusted: nothing is decided or sent.

        The connector side raises the one alert per problem (stale, needs reconnect); a hold while
        history is still loading raises none, because nothing is wrong. A check that fails is a
        hold too, with one alert a day."""
        if self.data_gate is None:
            return False
        try:
            hold = self.data_gate(now)
        except Exception as err:
            report.data_held.append(f"the data check failed ({type(err).__name__})")
            day = self._today()
            if ("data.gate_error", None) not in day.alerted:
                day.alerted.add(("data.gate_error", None))
                self._alert(
                    report,
                    code="data.gate_error",
                    key=day.day.isoformat(),
                    severity="warning",
                    message=(
                        "Autonomy could not check whether the shop's connected data is up to date "
                        f"({type(err).__name__}), so it decided and sent nothing this tick."
                    ),
                )
            return True
        self._hold_seen = (now.replace(second=0, microsecond=0), hold)
        if hold is None:
            return False
        report.data_held.extend(hold.reasons)
        report.alerts.extend(hold.alerts)
        return True

    def _rebuild_due(self, now: datetime) -> bool:
        if not self._rebuild_requested or not self._warm:
            return False  # a process's first tick rebuilds anyway
        last = self._last_rebuild
        return last is None or now - last >= self.config.rebuild_every

    # --- 1. fold ---------------------------------------------------------------------------------

    def _fold(self, now: datetime, report: TickReport, *, rebuild: bool = False) -> list[Event]:
        """Fold events appended since the last tick; return the new ones up to `now`."""
        if rebuild or self.cursor.watermark is None or not self._warm:
            # A new process does not know which events near a saved watermark it folded, so it
            # rebuilds once; folding an event twice would count a purchase twice.
            return self._full_rebuild(now, report)
        since = self.cursor.watermark - self.config.late_window
        try:
            new = [e for e in self.events.scan(since=since) if e.ts <= now and e.event_id not in self._seen]
        except Exception as err:  # P1 ingest: an unreachable feed folds nothing; deciding goes on
            self._feed_down(report, err)
            return []  # the watermark stays, so these events are folded once the feed is back
        self._note_ingest(new, now)
        self._check_stale(now, report)
        if new:
            apply_events(new, self.states, self.folder, now=now)
            self._mark_seen(new)
            self._assign_groups({e.customer_id for e in new})
            if self.spend_ledger is not None:
                self.spend_ledger.observe(new)
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
        ledger = self.spend_ledger
        # Rebuilt from the same scan of the log as state, into a fresh ledger adopted only once the
        # scan has finished (P1 ingest: a failed rebuild keeps the spend already known).
        rebuilt = (
            SpendLedger(ledger.decisions, ledger.workspace, channel_costs=ledger.channel_costs)
            if ledger is not None
            else None
        )
        recording = _RecordingEvents(
            self.events, floor, rebuilt.observe_one if rebuilt is not None else None, until=now
        )
        try:
            rebuild_states(recording, self.states, self.folder, now)
        except Exception as err:  # P1 ingest: decide on the states already stored; retry next tick
            self._feed_down(report, err)
            return []
        if ledger is not None and rebuilt is not None:
            ledger.adopt(rebuilt)
        if recording.newest_ingest is not None:
            self._newest_ingest = recording.newest_ingest
        self._check_stale(now, report)
        # Everything folded, future-dated events included, is marked so it is never folded again.
        fresh = [
            e for e in recording.recent if e.ts <= now and (not self._warm or e.event_id not in self._seen)
        ]
        self._mark_seen(recording.recent)
        self._assign_groups(None)
        self._advance_watermark(now)
        self._warm = True
        self._last_rebuild = now
        report.rebuilt = True
        report.events_folded += len(fresh)
        return fresh

    def _feed_down(self, report: TickReport, err: Exception) -> None:
        """P1 ingest: the feed could not be read. Alert (once a day); the tick decides on cached states."""
        day = self._today()
        if ("ingest.unavailable", None) in day.alerted:
            return
        day.alerted.add(("ingest.unavailable", None))
        self._alert(
            report,
            code="ingest.unavailable",
            key=day.day.isoformat(),
            severity="warning",
            message=(
                f"New events could not be folded ({type(err).__name__}: {err}). The runtime keeps "
                "deciding on the customer states it holds and folds the missed events once the feed "
                "is back; check the event feed."
            )[:1000],
        )

    def _note_ingest(self, events: Iterable[Event], now: datetime) -> None:
        """Remember the newest customer event folded (for the P1 staleness alert)."""
        for e in events:
            fresh = self._newest_ingest is None or e.ts > self._newest_ingest
            if e.type in INGEST_TYPES and e.ts <= now and fresh:
                self._newest_ingest = e.ts

    def _check_stale(self, now: datetime, report: TickReport) -> None:
        """P1 ingest: alert (once a day) when no new customer event has come for `ingest_stale_after`.

        The quiet is counted from the newest customer event, but never from before this process's
        first fold: a log whose history is old is not a dead feed until the runtime has watched it
        stay quiet for the whole window. A log with no customer events at all is a setup problem,
        not a stale feed, so it raises nothing here."""
        if self._watching_since is None:
            self._watching_since = now
        limit, newest = self.config.ingest_stale_after, self._newest_ingest
        if limit is None or newest is None or now - max(newest, self._watching_since) <= limit:
            return
        day = self._today()
        if ("ingest.stale", None) in day.alerted:
            return
        day.alerted.add(("ingest.stale", None))
        self._alert(
            report,
            code="ingest.stale",
            key=day.day.isoformat(),
            severity="warning",
            message=(
                f"No new customer events (visits, carts, purchases) for {_age(now - newest)}: the "
                f"newest is from {newest.isoformat()}. The feed may have gone quiet; the runtime keeps "
                "deciding on the states it holds."
            ),
        )

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
            if self.spend_ledger is not None:
                self.spend_ledger.observe(fresh)

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
        try:
            recent = [e for e in self.events.scan(since=now - self.config.breaker_window) if e.ts <= now]
            result = self.breakers_fn([b for _, b in running], recent, now)
        except Exception as err:  # P1 ingest / P6 spend: breakers that cannot read are an alert, not a stop
            self._alert(
                report,
                code="breakers.error",
                key=now.isoformat(),
                severity="warning",
                message=(
                    f"The circuit breakers could not run ({type(err).__name__}); nothing was paused by "
                    "them this tick."
                ),
            )
            return
        self._apply_breakers(result, {g.goal_id: g for g, _ in running}, report)

    def _apply_breakers(self, result: BreakerResult, goals: Mapping[str, Goal], report: TickReport) -> None:
        """Pause what a breaker verdict names (active goals in `goals`, channels) and keep its alerts.

        A repeated alert (same code and goal) is reported once a day, unless it paused the goal anew."""
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
            may_act, mandate = self._mandate_for(goal, now, report)
            if not may_act:
                continue  # held until the mandate stands again, or until the batch expires
            states = self._fresh_states({d.customer_id for d in decided}, now)
            if mandate is not None:
                brief = self._latest_brief(batch.goal_id)
                decided = self._mandate_filter(mandate, brief, decided, states, now, report, pace=True)
            self._send_now(batch.goal_id, decided, states, now, report)
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
        """Run queued sends that are due, when this runtime hosts the send worker (see the config).

        Send jobs have one owner, a `Worker` with a send handler. When no worker runs them in this
        process (tests, the API's operator tick), the tick hosts that same handler, so the claim,
        the retry rules and the give-up are exactly the worker's."""
        if self.queue is None or self.dispatcher is None or not self._hosts_sends():
            return
        dispatcher = self.dispatcher

        def take(result: DispatchReport) -> None:
            self._take_dispatch(result, now, report)

        gate = self.send_gate if self.mandates is not None else None
        hold = self.data_send_gate if self.data_gate is not None else None
        handlers: dict[str, JobHandler] = {
            SEND_KIND: send_handler(
                dispatcher,
                self.decisions,
                self.states,
                on_report=take,
                gate=gate,
                hold=hold,
                queue=self.queue,
            )
        }
        gateway = self.gateway
        gateway_jobs: JobHandler | None = None
        if gateway is not None and gateway.queue is not None:
            gateway_jobs = gateway_handler(
                gateway,
                self.decisions,
                self.states,
                on_report=take,
                gate=gate,
                hold=hold,
                queue=gateway.queue,
            )
            if gateway.queue is self.queue:
                handlers[GATEWAY_KIND] = gateway_jobs
                gateway_jobs = None
        hosted = Worker(self.queue, handlers, name=self.config.worker, hosted=True)
        hosted.drain(now, limit=MAX_SEND_JOBS_PER_TICK)
        if gateway_jobs is not None and gateway is not None and gateway.queue is not None:
            # The gateway keeps its held decisions on a queue of its own.
            Worker(gateway.queue, {GATEWAY_KIND: gateway_jobs}, name=self.config.worker, hosted=True).drain(
                now, limit=MAX_SEND_JOBS_PER_TICK
            )

    def _hosts_sends(self) -> bool:
        if self.config.run_send_jobs is not None:
            return self.config.run_send_jobs
        return self.dispatcher is not None and not worker_owns_sends(self.dispatcher)

    def _take_dispatch(self, result: DispatchReport, now: datetime, report: TickReport) -> None:
        report.dispatch.merge(result)
        self._fold_written(result.events, now)

    # --- 5. decide -------------------------------------------------------------------------------

    def _decide(
        self, now: datetime, local: datetime, new_events: Sequence[Event], report: TickReport
    ) -> None:
        running = self._running(now, report)
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
            may_act, mandate = self._mandate_for(run.goal, now, report)
            if not may_act:
                report.halted_goals.append(gid)
                continue  # not even decided: a decision today would block deciding again once it stands
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
            self._act(run, made, contacted_this_tick, now, report, mandate=mandate)
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
            budget_left={gid: self._budget_left(run, now, report)},
            seed=_seed(self.config.seed, gid, now),
            policy_version=run.policy.version,
        )
        picked: list[tuple[_Candidate, CustomerState]] = []
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
            picked.append((cand, state))
            decided_now.add(cid)
        picked = self._enrich(gid, picked, now, report)
        made: list[Decision] = []
        failures = 0
        for cand, state in picked:
            try:
                decision = run.policy.decide(state, ctx)
            except Exception:  # P3 Decision Hub: log DO_NOTHING for this customer; the rest still decide
                failures += 1
                made.append(_left_alone(cand.decision_id, state, now, run.policy.version))
                continue
            made.append(decision.model_copy(update={"decision_id": cand.decision_id}))
        if failures:
            self._alert(
                report,
                code="decide.error",
                key=f"{gid}:{now.isoformat()}",
                severity="warning",
                goal_id=gid,
                message=(
                    f"The policy failed for {failures} customers of this goal; each was left alone this "
                    "tick (a do-nothing decision is logged) and nothing was sent to them."
                ),
            )
        if made:
            self.decisions.record(made)
            for d in made:
                day.add(d, gid)
            report.decisions.extend(made)
        return made

    def _enrich(
        self,
        goal_id: str,
        picked: list[tuple[_Candidate, CustomerState]],
        now: datetime,
        report: TickReport,
    ) -> list[tuple[_Candidate, CustomerState]]:
        """Hand the candidates' states to the `state_enricher`, if any; plain states if it fails."""
        enricher = self.state_enricher
        if enricher is None or not picked:
            return picked
        states = [state for _, state in picked]
        try:
            enriched = enricher(states, self._recent_events([s.customer_id for s in states], now), now)
            by_id = {s.customer_id: s for s in enriched}
        except Exception as err:  # an enricher bug must not stop deciding; the plain states still work
            self._alert(
                report,
                code="enrich.error",
                key=f"{goal_id}:{now.isoformat()}",
                severity="warning",
                goal_id=goal_id,
                message=(
                    f"The state enricher failed ({type(err).__name__}); this goal decided on the "
                    "customers' plain state."
                ),
            )
            return picked
        return [(cand, by_id.get(cand.customer_id, state)) for cand, state in picked]

    def _recent_events(self, customer_ids: Sequence[str], now: datetime) -> dict[str, list[Event]]:
        """Each customer's events in the enricher's lookback, oldest first (every id has a list)."""
        since = now - self.config.enrich_lookback
        wanted = sorted(set(customer_ids))
        if len(wanted) <= ENRICH_PER_CUSTOMER_MAX:
            return {
                cid: sorted(self.events.for_customer(cid, since=since, until=now), key=lambda e: e.ts)
                for cid in wanted
            }
        out: dict[str, list[Event]] = {cid: [] for cid in wanted}
        for e in self.events.scan(since=since, until=now):
            bucket = out.get(e.customer_id)
            if bucket is not None:
                bucket.append(e)
        for bucket in out.values():
            bucket.sort(key=lambda e: e.ts)
        return out

    def _act(
        self,
        run: _Running,
        made: Sequence[Decision],
        contacted_this_tick: set[str],
        now: datetime,
        report: TickReport,
        *,
        mandate: Mandate | None = None,
    ) -> None:
        """Do what the goal's autonomy level allows with its ACT decisions.

        Under a mandate, every ACT decision is checked again first; what it blocks is neither
        batched nor sent (`_mandate_filter`), and the pacer draws spend only for what is sent."""
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
        states: dict[str, CustomerState] | None = None
        if mandate is not None:
            states = self._fresh_states({d.customer_id for d in acts}, now)
            # A batch is paced when it is sent, not when it is held for a person; with no channels
            # nothing is sent, so nothing is drawn from the pacer either.
            pace = level is AutonomyLevel.AUTOPILOT and self.dispatcher is not None
            acts = self._mandate_filter(mandate, run.brief, acts, states, now, report, pace=pace)
            if not acts:
                return
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
        if states is None:
            states = self._fresh_states({d.customer_id for d in acts}, now)
        self._send_now(gid, acts, states, now, report)

    def _send_now(
        self,
        goal_id: str,
        acts: Sequence[Decision],
        states: Mapping[str, CustomerState],
        now: datetime,
        report: TickReport,
    ) -> None:
        """Dispatch, through the gateway when there is one. A refusal or a crash is an alert.

        A failing channel or gateway must never stop the tick: the other goals still decide and
        send, and a person reads what went wrong."""
        if not acts:
            return
        assert self.dispatcher is not None  # callers check
        try:
            if self.gateway is not None:
                result = self.gateway.dispatch(acts, states, now)
            else:
                result = self.dispatcher.dispatch(acts, states, now)
        except Exception as err:
            self._alert(
                report,
                code="dispatch.error",
                key=f"{goal_id}:{now.isoformat()}:{_ids_key(d.decision_id for d in acts)}",
                severity="critical",
                goal_id=goal_id,
                message=(
                    f"Sending {len(acts)} messages for this goal failed ({type(err).__name__}: {err}); "
                    "nothing was sent this tick. The decisions are logged."
                )[:1000],
            )
            return
        self._take_dispatch(result, now, report)
        if self.gateway is None:
            return
        refused = Counter(r.reason for r in result.skipped if r.reason not in _DISPATCHER_SKIPS)
        if refused:
            listed = ", ".join(
                f"{k} ({v})" for k, v in sorted(refused.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            n = sum(refused.values())
            self._alert(
                report,
                code="dispatch.refused",
                key=f"{goal_id}:{now.isoformat()}:{_ids_key(d.decision_id for d in acts)}",
                severity="warning",
                goal_id=goal_id,
                message=(
                    f"The send-time gateway refused {n} {'message' if n == 1 else 'messages'} for this "
                    f"goal: {listed}. Nothing was sent to them."
                ),
            )

    # --- mandates -------------------------------------------------------------------------------

    def _mandate_state(self, goal_id: str, now: datetime) -> tuple[Mandate | None, str | None, str]:
        """(the mandate to act under, a problem code, why in words). No source: (None, None, '').

        A goal the source does not govern acts without one. One it governs needs a mandate that is
        active now, inside its validity window and authorised by its signatures."""
        source = self.mandates
        if source is None or not source.governs(goal_id):
            return None, None, ""
        mandate = source.lookup(goal_id)
        if mandate is None:
            return None, MANDATE_MISSING, "it needs a signed mandate and none covers it"
        status = mandate.effective_status(now)
        if status is not MandateStatus.ACTIVE:
            return (
                mandate,
                MANDATE_INACTIVE,
                f"its mandate {mandate.mandate_id} (v{mandate.version}) is {status.value}",
            )
        if not mandate.is_valid_at(now):
            return (
                mandate,
                MANDATE_INACTIVE,
                f"its mandate {mandate.mandate_id} (v{mandate.version}) is valid only from "
                f"{mandate.valid_from.isoformat()} to {mandate.valid_until.isoformat()}",
            )
        problems = source.problems(mandate, now)
        if problems:
            return (
                mandate,
                MANDATE_UNVERIFIED,
                f"the signatures on mandate {mandate.mandate_id} (v{mandate.version}) do not authorise it "
                f"({', '.join(problems)})",
            )
        return mandate, None, ""

    def _mandate_for(self, goal: Goal, now: datetime, report: TickReport) -> tuple[bool, Mandate | None]:
        """(may this goal act, the mandate it acts under). A goal that may not act raises an alert,
        once a day; a shadow goal never acts, so it is never held back."""
        if self.mandates is None or goal.autonomy is AutonomyLevel.SHADOW:
            return True, None
        gid = goal.goal_id
        day = self._today()
        try:
            mandate, problem, why = self._read_mandate(gid, now)
        except Exception as err:  # a mandate store outage stops this goal, never the tick
            held = self._held_mandate(gid, now)
            if held is not None:  # P2 mandate store down: run under the last good, unexpired mandate
                self._store_down_alert(gid, held, err, report)
                return True, held
            mandate, problem, why = (
                None,
                MANDATE_ERROR,
                f"its mandate could not be read ({type(err).__name__}) and no unexpired mandate read "
                "earlier stands in",
            )
        if problem is None:
            return True, mandate
        key = (f"mandate.{problem}", gid)
        if key not in day.alerted:
            day.alerted.add(key)
            self._alert(
                report,
                code=f"mandate.{problem.removeprefix('mandate_')}",
                key=f"{gid}:{problem}:{day.day.isoformat()}",
                severity="critical",
                goal_id=gid,
                message=f"This goal is not acting: {why}. Nothing is batched or sent until a mandate stands.",
            )
        return False, None

    def _read_mandate(self, goal_id: str, now: datetime) -> tuple[Mandate | None, str | None, str]:
        """`_mandate_state`, remembering an active, certified mandate as the goal's last good one.

        Any other answer from the store (no mandate, revoked, unsigned, not governed) clears it: only
        a store that cannot answer at all lets the last good mandate stand in (P2)."""
        mandate, problem, why = self._mandate_state(goal_id, now)
        if problem is None and mandate is not None:
            self._last_mandate[goal_id] = (mandate, now)
        else:
            self._last_mandate.pop(goal_id, None)
        return mandate, problem, why

    def _held_mandate(self, goal_id: str, now: datetime) -> Mandate | None:
        """P2 mandate store down: the last good mandate read for the goal, if it is still unexpired."""
        held = self._last_mandate.get(goal_id)
        if held is None:
            return None
        mandate = held[0]
        if mandate.effective_status(now) is not MandateStatus.ACTIVE or not mandate.is_valid_at(now):
            return None  # expired while the store was down: the goal halts (fail safe)
        return mandate

    def _store_down_alert(self, goal_id: str, mandate: Mandate, err: Exception, report: TickReport) -> None:
        """Say (once a day per goal) that the goal runs under a mandate held from an earlier read."""
        day = self._today()
        key = ("mandate.store_unavailable", goal_id)
        if key in day.alerted:
            return
        day.alerted.add(key)
        read_at = self._last_mandate[goal_id][1]
        self._alert(
            report,
            code="mandate.store_unavailable",
            key=f"{goal_id}:{day.day.isoformat()}",
            severity="warning",
            goal_id=goal_id,
            message=(
                f"The mandate store could not be read ({type(err).__name__}). This goal keeps running "
                f"under mandate {mandate.mandate_id} (v{mandate.version}) as read at "
                f"{read_at.isoformat()}: nothing beyond it is spent, and the goal stops at "
                f"{mandate.valid_until.isoformat()} unless the store is back."
            ),
        )

    def send_gate(self, decision: Decision, now: datetime) -> str | None:
        """The mandate check for a queued send coming due (a worker's `SendGate`): a reason, or None.

        The pacer was drawn when the send was queued; this re-checks the rest with today's spend,
        so a mandate revoked or a cap reached while the message waited stops it."""
        action = decision.action
        if self.mandates is None or action is None:
            return None
        try:
            mandate, problem, _ = self._read_mandate(action.goal_id, now)
        except Exception:
            held = self._held_mandate(action.goal_id, now)
            if held is None:
                raise  # nothing stands in: the job fails and is retried, nothing is sent
            mandate, problem = held, None  # P2 mandate store down: the last good mandate still gates
        if problem is not None:
            return problem
        if mandate is None:
            return None
        state = self.states.get(decision.customer_id)
        if state is None:
            return None  # the dispatcher reports the missing state itself
        state = self.folder.refresh(state, now)
        brief = self._latest_brief(action.goal_id)
        self._catch_up_ledger(now)
        codes, _ = self._check_one(mandate, brief, decision, state, now, self._mandate_spend(mandate, now))
        return codes[0] if codes else None

    def _catch_up_ledger(self, now: datetime) -> None:
        """Bring the spend ledger up from the log in a process that has not ticked (a worker's gate).

        A tick keeps the ledger current through its fold. A worker that claims a queued send before
        any tick ran would otherwise gate it on a ledger never built, which reads as unknown spend
        and refuses the send for good. If the log cannot be read this raises: the job fails and is
        retried, and nothing is sent."""
        ledger = self.spend_ledger
        if ledger is None or self._warm:
            return
        if self._ledger_read_at is None:
            fresh = SpendLedger(ledger.decisions, ledger.workspace, channel_costs=ledger.channel_costs)
            fresh.observe(self.events.scan())
            ledger.adopt(fresh)
        else:
            ledger.refresh()
            ledger.observe(self.events.scan(since=self._ledger_read_at - self.config.late_window))
        self._ledger_read_at = now if self._ledger_read_at is None else max(self._ledger_read_at, now)

    def _check_one(
        self,
        mandate: Mandate,
        brief: Brief | None,
        decision: Decision,
        state: CustomerState,
        now: datetime,
        spend: SpendToDate,
    ) -> tuple[list[str], float]:
        """(every mandate violation of this ACT decision, what it would spend)."""
        action = decision.action
        assert action is not None
        chosen = next((b for b in decision.bids if b.action == action), None)
        if chosen is not None:
            # Priced as the SpendLedger will charge it: the runtime's channel price where it has
            # one, else the bid's. A bid that prices the channel lower would otherwise pass the
            # daily cap and the pacer, and the ledger would then record more than they allowed.
            priced = self.channel_costs.get(action.channel)
            if priced is not None and priced != chosen.channel_cost:
                chosen = chosen.model_copy(update={"channel_cost": max(priced, 0.0)})
            offers = brief.envelope.offers if brief is not None else ()
            codes = check_bid(mandate, chosen, state, now, offers=offers, spend=spend)
            return codes, max(chosen.offer_cost, 0.0) + max(chosen.channel_cost, 0.0)
        costs = ActionCosts(channel_cost=max(self.channel_costs.get(action.channel, 0.0), 0.0))
        return check_action(mandate, action, costs, state, now, spend=spend), costs.spend

    def _mandate_filter(
        self,
        mandate: Mandate,
        brief: Brief | None,
        acts: Sequence[Decision],
        states: Mapping[str, CustomerState],
        now: datetime,
        report: TickReport,
        *,
        pace: bool,
    ) -> list[Decision]:
        """The ACT decisions the mandate lets through now; the rest are noted and alerted.

        Each is checked with `check_action` (via `check_bid` when the decision carries its bid), with
        the mandate's spend so far plus what this call already let through. With `pace`, what passes
        is drawn from the mandate's `TokenBucket`; a send the bucket cannot cover is held
        (`mandate_paced`) and the customer is decided again on another day."""
        spend = self._mandate_spend(mandate, now)
        extra = 0.0
        # P6 spend: with spend unknown nothing passes the ceiling, and a pacer built now (from a
        # ledger a cold start never filled) would be kept with a whole fresh burst in it.
        bucket = self._bucket(mandate, now) if pace and not math.isinf(spend.today) else None
        allowed: list[Decision] = []
        blocked: list[tuple[Decision, list[str]]] = []
        for d in acts:
            state = states.get(d.customer_id)
            if d.action is None or state is None:
                allowed.append(d)  # the dispatcher skips a decision with no state, in its own words
                continue
            so_far = SpendToDate(total=spend.total + extra, today=spend.today + extra)
            codes, amount = self._check_one(mandate, brief, d, state, now, so_far)
            if not codes and bucket is not None:
                ok, bucket = bucket.try_spend(amount, now)
                if not ok:
                    codes = [MANDATE_PACED]
            if codes:
                blocked.append((d, codes))
                continue
            extra += amount
            allowed.append(d)
        if bucket is not None:
            self._buckets[(mandate.mandate_id, mandate.version)] = bucket
        if blocked:
            self._note_blocked(mandate, blocked, now, report)
        return allowed

    def _note_blocked(
        self,
        mandate: Mandate,
        blocked: Sequence[tuple[Decision, list[str]]],
        now: datetime,
        report: TickReport,
    ) -> None:
        """Note on each decision why the mandate stopped it, and raise one alert per goal."""
        noted: dict[str, Decision] = {}
        by_goal: dict[str, Counter[str]] = defaultdict(Counter)
        for d, codes in blocked:
            assert d.action is not None
            paced = codes == [MANDATE_PACED]
            why = (
                "the mandate's daily spend pacing had no room for it today"
                if paced
                else f"it breaks the mandate ({', '.join(codes)})"
            )
            noted[d.decision_id] = d.model_copy(
                update={
                    "blocked_by": tuple(dict.fromkeys((*d.blocked_by, *codes))),
                    "reason": f"{d.reason} Not sent: {why}.",
                }
            )
            by_goal[d.action.goal_id].update(codes)
            report.mandate_blocked.append(d.decision_id)
        self.decisions.record(noted.values())
        report.decisions = [noted.get(d.decision_id, d) for d in report.decisions]
        for gid in sorted(by_goal):
            counts = by_goal[gid]
            n = sum(1 for d, _ in blocked if d.action is not None and d.action.goal_id == gid)
            listed = ", ".join(
                f"{k} ({v})" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            only_paced = set(counts) == {MANDATE_PACED}
            self._alert(
                report,
                code="mandate.paced" if only_paced else "mandate.violation",
                key=f"{gid}:{now.isoformat()}:{_ids_key(d.decision_id for d, _ in blocked)}",
                severity="info" if only_paced else "warning",
                goal_id=gid,
                message=(
                    f"Mandate {mandate.mandate_id} (v{mandate.version}) held back {n} "
                    f"{'send' if n == 1 else 'sends'} for this goal: {listed}. "
                    "Each decision notes why; nothing was sent to those customers."
                ),
            )

    def _mandate_spend(self, mandate: Mandate, now: datetime) -> SpendToDate:
        """Money spent under the mandate: over its validity window, and on `now`'s UTC day."""
        at = now.astimezone(UTC)
        midnight = datetime.combine(at.date(), time(0), tzinfo=UTC)
        total = today = 0.0
        if self._spend_unknown():
            return SpendToDate(total=math.inf, today=math.inf)  # P6 spend: unknown spend sends nothing
        try:
            for gid in mandate.goal_ids:
                spent = self.spend_fn(gid, now)
                total += max(0.0, spent - self.spend_fn(gid, mandate.valid_from))
                today += max(0.0, spent - self.spend_fn(gid, midnight))
        except Exception:  # P6 spend: unknown spend counts as the whole ceiling, so nothing goes
            return SpendToDate(total=math.inf, today=math.inf)
        return SpendToDate(total=total, today=today)

    def _bucket(self, mandate: Mandate, now: datetime) -> TokenBucket | None:
        """The mandate version's spend pacer; None when it has no daily cap.

        A pacer built in this process (after a restart, or for a new version) starts with what
        the last 24 hours already spent taken out, so a restart never frees a fresh burst."""
        key = (mandate.mandate_id, mandate.version)
        known = self._buckets.get(key)
        if known is not None:
            return known
        fresh = TokenBucket.for_mandate(mandate, now, burst_share=self.config.mandate_burst_share)
        if fresh is None:
            return None
        recent: float
        try:
            recent = sum(
                max(0.0, self.spend_fn(gid, now) - self.spend_fn(gid, now - timedelta(days=1)))
                for gid in mandate.goal_ids
            )
        except Exception:  # P6 spend: a pacer built while spend is unknown starts empty and refills
            recent = math.inf
        fresh = fresh.model_copy(update={"tokens": max(0.0, fresh.tokens - recent)})
        self._buckets[key] = fresh
        return fresh

    def _latest_brief(self, goal_id: str) -> Brief | None:
        found = self.workspace.briefs(goal_id)
        return max(found, key=lambda b: b.version) if found else None

    def _lapse_due(self, now: datetime) -> bool:
        if self.cursor.last_tick is None:
            return False
        last = self.cursor.lapse_checked_at
        return last is None or now - last >= self.config.lapse_check_every

    def _budget_left(self, run: _Running, now: datetime, report: TickReport) -> float:
        """The goal's budget less its spend. With the spend unknown, the last budget known, never more."""
        gid = run.goal.goal_id
        cap = min(run.brief.envelope.budget, run.goal.budget)
        try:
            if self._spend_unknown():
                raise RuntimeError("the spend ledger has not been built from the event log yet")
            spent = self.spend_fn(gid, now)
        except Exception as err:  # P6 spend telemetry: keep the last known budget less our own sends
            return self._budget_unknown(gid, cap, now, err, report)
        left = max(0.0, cap - spent)
        self._spend_known[gid] = (spent, left, now)
        return left

    def _spend_unknown(self) -> bool:
        """True while the runtime's own spend ledger has never been built from the log (a cold start
        whose feed is down)."""
        return self.spend_ledger is not None and not self._warm and self._ledger_read_at is None

    def _budget_unknown(
        self, goal_id: str, cap: float, now: datetime, err: Exception, report: TickReport
    ) -> float:
        """P6 spend: the budget left when the spend source cannot be read; alert once a day.

        It is the budget left at the last good read (or the cap less that read's spend, if the cap
        was cut since), less what this goal's own SENDs since then cost. A raised budget is never
        honoured, and with no good read, or no way to count the sends since, nothing is spent."""
        day = self._today()
        key = ("spend.unavailable", goal_id)
        if key not in day.alerted:
            day.alerted.add(key)
            self._alert(
                report,
                code="spend.unavailable",
                key=f"{goal_id}:{day.day.isoformat()}",
                severity="warning",
                goal_id=goal_id,
                message=(
                    f"This goal's spend could not be read ({type(err).__name__}). It keeps deciding "
                    "with the last budget it knew, less its own sends since, and never more."
                ),
            )
        known = self._spend_known.get(goal_id)
        if known is None:
            return 0.0
        spent, left, at = known
        try:
            # Priced as the ledger prices them, which also finds the goal of a gateway SEND (it
            # carries only the decision id). `scan`'s `until` is exclusive and this tick's queued
            # sends land at `now`, so the window is filtered by hand, inclusive at both ends.
            ours = SpendLedger(self.decisions, self.workspace, channel_costs=self.channel_costs)
            ours.observe(e for e in self.events.scan(since=at) if at <= e.ts <= now)
            since = ours.spent(goal_id, now)
        except Exception:  # the sends since the last read cannot be counted either: spend nothing
            return 0.0
        return max(0.0, min(left, cap - spent) - since)

    # --- 6. nightly ------------------------------------------------------------------------------

    def _nightly_due(self, local: datetime) -> bool:
        return local.hour >= self.config.nightly_hour and self.cursor.nightly_on != local.date()

    def _nightly(self, now: datetime, local: datetime, report: TickReport) -> None:
        """The fast learning loop. The tick has already rebuilt state, if configured to."""
        today = local.date()
        self.cursor.nightly_on = today
        report.nightly_ran = True
        # A policy built earlier in this tick replayed the log before deciding; learning again now
        # adds this tick's decisions (nightly() keeps everything by id, so nothing counts twice).
        running = self._running(now, report)
        self._learn(running, now, self.config.nightly_lookback, report, code="nightly.error")
        self._negative_lift(running, now, report)

    def _negative_lift(self, running: Sequence[_Running], now: datetime, report: TickReport) -> None:
        """The nightly negative-lift breaker: pause an acting goal whose proof shows it losing money.

        Each goal is judged on its own decisions (holdout ones included) over closed outcome
        windows; see `autonomy.channels.breakers.negative_lift`. A failure is an alert, never a
        stop: the other goals are still judged."""
        guards = self.lift_guards
        acting = [r for r in running if r.goal.autonomy is not AutonomyLevel.SHADOW]
        if guards is None or not acting:
            return
        since = now - self.config.proof_lookback
        try:
            decided = [d for d in self.decisions.scan(since=since) if d.made_at <= now]
            customers = {d.customer_id for d in decided}
            by_customer: dict[str, list[Event]] = defaultdict(list)
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
        except Exception as err:
            self._alert(
                report,
                code="proof.error",
                key=f"lift:{now.isoformat()}",
                severity="warning",
                message=f"The nightly proof check could not read the logs ({type(err).__name__}).",
            )
            return
        for run in acting:
            gid = run.goal.goal_id
            mine = [d for d in decided if owner[d.decision_id] == gid and d.made_at >= run.goal.starts_at]
            if not mine:
                continue
            events = {d.customer_id: by_customer.get(d.customer_id, []) for d in mine}
            try:
                result = negative_lift(
                    run.brief,
                    mine,
                    events,
                    now,
                    costs=Costs.from_brief(run.brief, channel=self.channel_costs),
                    guards=guards,
                )
            except Exception as err:
                self._alert(
                    report,
                    code="proof.error",
                    key=f"{gid}:{now.isoformat()}",
                    severity="warning",
                    goal_id=gid,
                    message=f"The nightly proof check failed for this goal ({type(err).__name__}).",
                )
                continue
            self._apply_breakers(result, {gid: run.goal}, report)

    def _replay(self, built: Sequence[_Running], now: datetime, report: TickReport) -> None:
        """Relearn what new policies lost: replay the decision log and events through `nightly()`.

        A policy's learning lives in memory. Without this, a restart (or a weekly brief that
        changed the plan) would decide with priors only until the nightly run, and that run would
        relearn only what is still in its window. The replay hands the policy `replay_lookback` of
        its goal's decisions and their events at once; `nightly()` keeps decisions and events by
        id, so a later nightly run does not count them twice.
        """
        learners = [r for r in built if callable(getattr(r.policy, "nightly", None))]
        if not learners or self.config.replay_lookback <= timedelta(0):
            return
        fed = self._learn(
            learners, now, self.config.replay_lookback, report, code="replay.error", skip_empty=True
        )
        report.replayed_goals.extend(fed)

    def _learn(
        self,
        running: Sequence[_Running],
        now: datetime,
        lookback: timedelta,
        report: TickReport,
        *,
        code: str,
        skip_empty: bool = False,
    ) -> list[str]:
        """Hand each goal's policy its own decisions since `now - lookback` and their events.

        Returns the goals whose policy learned (a call that raised is an alert, not a learn). With
        `skip_empty`, a goal with no decisions in the window is not called (a replay of nothing
        teaches nothing)."""
        learners: list[tuple[_Running, Callable[..., object]]] = []
        for run in running:
            nightly = getattr(run.policy, "nightly", None)
            if callable(nightly):
                learners.append((run, nightly))
        if not learners:
            return []
        since = now - lookback
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
        day = now.astimezone(self._tz).date().isoformat()
        fed: list[str] = []
        for run, fn in learners:
            # Each policy learns from its own goal's decisions only. Handing it every goal's would
            # count a holdout customer's "do nothing" once per goal, and as control for goals whose
            # audience they are not in. Decisions no goal can be found for go to every policy.
            gid = run.goal.goal_id
            mine = [d for d in decided if owner[d.decision_id] in (gid, None)]
            if skip_empty and not mine:
                continue
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
                    code=code,
                    key=f"{gid}:{day}",
                    severity="warning",
                    goal_id=gid,
                    message=f"Learning failed ({type(err).__name__}); decisions use what was learned before.",
                )
            else:
                fed.append(gid)
        return fed

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

    def _running(self, now: datetime, report: TickReport) -> list[_Running]:
        """Active goals inside their dates with an approved brief, each with its (cached) policy.

        A goal's policy is rebuilt only when its brief changes materially or its approved content
        changes; a newly built policy replays the log first (`_replay`). A brief that asks for a
        proof split other than the business's is refused: its goal does not run."""
        out: list[_Running] = []
        built: list[_Running] = []
        for goal, brief in self._goals_with_briefs():
            if not _sendable(goal, now, require_level=False):
                continue
            try:
                self.check_brief(brief)
            except SplitMismatchError as err:
                self._refuse(goal, report, str(err))
                continue
            content = sorted(
                self.workspace.content(goal_id=goal.goal_id, approved_only=True), key=lambda c: c.content_id
            )
            key: tuple[object, ...] = (material_brief(brief), tuple(c.content_id for c in content))
            cached = self._policies.get(goal.goal_id)
            fresh = cached is None or cached[0] != key
            if fresh:
                try:
                    built_policy = self.policy_factory([brief], content)
                except Exception as err:  # P3: a policy that cannot be built skips only its goal
                    self._unbuildable(goal, report, err)
                    continue  # tried again next tick; the other goals run
                cached = (key, built_policy)
                self._policies[goal.goal_id] = cached
            assert cached is not None
            run = _Running(goal=goal, brief=brief, content=content, policy=cached[1])
            out.append(run)
            if fresh:
                built.append(run)
        if built:
            self._replay(built, now, report)
        return out

    def _unbuildable(self, goal: Goal, report: TickReport, err: Exception) -> None:
        """Alert (once a day per goal) that a goal's policy could not be built, so it did not run."""
        day = self._today()
        key = ("decide.unbuildable", goal.goal_id)
        if key in day.alerted:
            return
        day.alerted.add(key)
        self._alert(
            report,
            code="decide.error",
            key=f"{goal.goal_id}:build:{day.day.isoformat()}",
            severity="critical",
            goal_id=goal.goal_id,
            message=(
                f"This goal's decision policy could not be built ({type(err).__name__}: {err}), so it "
                "is not deciding or sending; it is tried again every tick. Other goals are not affected."
            )[:1000],
        )

    def _refuse(self, goal: Goal, report: TickReport, why: str) -> None:
        """Alert (once a day per goal) that a goal's brief was refused, so a person fixes it."""
        day = self._today()
        key = ("brief.split_mismatch", goal.goal_id)
        if key in day.alerted:
            return
        day.alerted.add(key)
        self._alert(
            report,
            code="brief.split_mismatch",
            key=f"{goal.goal_id}:{day.day.isoformat()}",
            severity="critical",
            goal_id=goal.goal_id,
            message=f"This goal is not running. {why}",
        )

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


def _left_alone(decision_id: str, state: CustomerState, now: datetime, version: str) -> Decision:
    """P3 Decision Hub: the DO_NOTHING decision logged when the policy raised for this customer.

    Probability 1 (nothing else was possible), so proof and the off-policy log keep the moment."""
    return Decision(
        decision_id=decision_id,
        customer_id=state.customer_id,
        made_at=now,
        kind=DecisionKind.NOTHING,
        group=state.group,
        probability=1.0,
        reason=LEFT_ALONE,
        policy_version=version,
    )


def _seed(seed: int, goal_id: str, now: datetime) -> int:
    """A sampling seed fixed by (seed, goal, moment), so a repeated run samples the same way."""
    return int(stable_id("seed", seed, goal_id, now.isoformat())[5:13], 16)


class _RecordingEvents:
    """An `EventStore` that remembers the events (at or after `floor`) its scans hand out.

    `rebuild_states` folds what one scan of the log returns. Wrapping the store records exactly
    those events, so the runtime marks as folded what was folded, not what a second scan finds.
    """

    def __init__(
        self,
        inner: EventStore,
        floor: datetime,
        observe: Callable[[Event], None] | None = None,
        until: datetime | None = None,
    ) -> None:
        self.inner = inner
        self.floor = floor
        self.observe = observe
        """Also sees every event scanned (the spend ledger rebuilds from the same pass)."""
        self.recent: list[Event] = []
        self.until = until
        self.newest_ingest: datetime | None = None
        """The newest customer event (`INGEST_TYPES`) scanned, at or before `until`."""

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
            if self.observe is not None:
                self.observe(e)
            due = self.until is None or e.ts <= self.until
            if e.type in INGEST_TYPES and due and (self.newest_ingest is None or e.ts > self.newest_ingest):
                self.newest_ingest = e.ts
            yield e

    def for_decision(self, decision_id: str) -> list[Event]:
        return self.inner.for_decision(decision_id)


def _ids_key(decision_ids: Iterable[str]) -> str:
    """A short, order-free key for a set of decisions: two sends for one goal in one tick (two
    approved batches, say) get distinct alert ids, so the alert store keeps both."""
    return stable_id("ids", *sorted(set(decision_ids)))


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


def _age(span: timedelta) -> str:
    """A rough age in words: whole days from a day up, else whole hours."""
    if span >= timedelta(days=1):
        return "1 day" if span.days == 1 else f"{span.days} days"
    hours = int(span.total_seconds() // 3600)
    return "1 hour" if hours == 1 else f"{hours} hours"


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
    the tick interval counted from the Unix epoch. Every tick job sits on that one grid, so a
    restart queues either the same job as the running chain (the id is its time) or one whose
    successor is the chain's next job, and the two chains merge at once instead of running twice.
    The worker owns send jobs: the runtime is built with `run_send_jobs=False`.

    With `AUTONOMY_CONNECTORS=true` the same worker runs the connector jobs too (every stream's sync,
    webhooks, the daily maintenance, Klaviyo's delivery watch and Shopify's privacy desk), over the
    engine `build_runtime` ties to the runtime, and queued sends wait while the shop's data cannot
    be trusted (`AutonomyRuntime.data_send_gate`).
    """
    from autonomy.runtime.settings import (
        RuntimeSettings,
        build_connector_engine,
        build_runtime,
        engine_for,
    )
    from autonomy.runtime.stores import SqlAlertStore, create_record_tables
    from autonomy.runtime.worker import tick_job
    from autonomy.store import SqlEventStore, create_all

    settings = RuntimeSettings.from_env(environ)
    db = engine_for(settings.database_url)
    wiring = None
    if settings.connectors:
        create_all(db)
        create_record_tables(db)
        wiring = build_connector_engine(
            settings,
            db=db,
            events=SqlEventStore(db),
            alerts=SqlAlertStore(db),
            environ=environ,
            holder=name,
        )
    runtime = build_runtime(settings, environ=environ, engine=db, run_send_jobs=False, connectors=wiring)
    every = timedelta(minutes=settings.tick_every_minutes)
    if start is None:
        start = tick_slot(datetime.now(UTC), every)
    worker = worker_for_runtime(runtime, name=name, wiring=wiring, db=db if wiring is not None else None)
    worker.queue.enqueue(tick_job(start, every_minutes=settings.tick_every_minutes))
    return worker


def worker_for_runtime(
    runtime: AutonomyRuntime,
    *,
    name: str = "worker-1",
    wiring: Any = None,
    db: Any = None,
    zero_touch: bool = True,
) -> Worker:
    """The worker's handlers over a built production runtime (`build_worker` without the settings).

    Tick, send, gateway and webhook handlers, plus every connector job when `wiring` (a
    `ConnectorWiring`) is given. With connectors and `zero_touch`, the exceptions-only inbox, the data
    watchdog and the Autopilot guard are installed on the runtime's ticks
    (`autonomy.inbox.install`), their tables in `db`: automatic demotion runs with every tick, not
    only when someone opens the inbox, and the watchdog's holds join the connector data gate. Queued
    sends wait while that gate holds (`AutonomyRuntime.data_send_gate`). It enqueues no tick job."""
    from autonomy.runtime.worker import (
        TICK_KIND,
        WEBHOOK_KIND,
        connector_handlers,
        tick_handler,
        webhook_handler,
    )

    runtime.config = replace(runtime.config, run_send_jobs=False, worker=name)
    if runtime.queue is None or runtime.dispatcher is None:  # build_runtime always sets both
        raise RuntimeError("the runtime has no queue or dispatcher")
    if wiring is not None and zero_touch:
        from autonomy.inbox.install import install, zero_touch_for_runtime

        zt = zero_touch_for_runtime(runtime, engine=db, wiring=wiring)
        install(runtime, zt, monitor=wiring.engine.monitor)
    events = runtime.events
    gate = runtime.send_gate if runtime.mandates is not None else None
    hold = runtime.data_send_gate if runtime.data_gate is not None else None
    queue = runtime.queue
    handlers: dict[str, JobHandler] = {
        TICK_KIND: tick_handler(runtime.tick, queue),
        SEND_KIND: send_handler(
            runtime.dispatcher, runtime.decisions, runtime.states, gate=gate, hold=hold, queue=queue
        ),
        WEBHOOK_KIND: webhook_handler(runtime.dispatcher.adapters, events.append),
    }
    if runtime.gateway is not None:
        # build_runtime gives the gateway the runtime's queue, so this worker claims its held decisions.
        handlers[GATEWAY_KIND] = gateway_handler(
            runtime.gateway, runtime.decisions, runtime.states, gate=gate, hold=hold, queue=queue
        )
    if wiring is not None:
        handlers.update(
            connector_handlers(wiring.engine, queue, events=wiring.events, privacy_desk=wiring.privacy_desk)
        )
    return Worker(queue=queue, handlers=handlers, name=name)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def tick_slot(at: datetime, every: timedelta) -> datetime:
    """The start of the tick interval holding `at`, on a grid of `every` counted from the Unix epoch."""
    if every <= timedelta(0):
        raise ValueError("the tick interval must be positive")
    return _EPOCH + every * ((at - _EPOCH) // every)
