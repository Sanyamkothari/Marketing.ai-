"""The whole system end to end, in simulation: a Gym shop run by Autonomy for `days` days.

`run_demo()` is the one place where every v2 piece meets, with nothing real on the other end:

- **The environment is a Gym world.** A `GymBridge` puts it behind the channel seam: the dispatcher
  sends through `GymChannelAdapter`s exactly as it would through SES or WhatsApp, the bridge hands
  those messages to the world, and the world's events (deliveries, clicks, purchases, unsubscribes)
  flow back into the event store. The world's own SEND events are dropped, because the dispatcher
  has already recorded the one SEND each message gets.
- **The agents run on a scripted model** (`demo_llm()`), so the outer loop is deterministic and
  offline: the Strategist falls back to its template briefs, the Creative writes from a fixed copy
  book that passes the Critic's rules, the Critic's judge approves, and the Analyst writes its
  template briefing. Every number still comes from code.
- **Two goals, two autonomy levels.** "Win back lapsed customers" runs on AUTOPILOT; "turn one-time
  buyers into repeat buyers" runs at APPROVE, so its ACT decisions are held in a pending `Batch`.
  The demo plays the marketer and approves each batch, and the runtime sends it.
- **Every day**, a `tick` job runs through the worker: `AutonomyRuntime.tick` folds new events,
  checks the circuit breakers, decides for every customer in each goal's audience (with a
  probability on every decision), batches or dispatches, learns nightly and runs the weekly
  Analyst/Strategist cycle. The demo then writes the daily briefing. Sends are queued for their
  `send_at`, and the worker finishes each one on time through the dispatcher, which re-checks every
  rule first.

At the end it returns a `DemoResult`: decisions, events, batches, briefings, alerts, the proof
summary (agent vs holdout and baseline, with intervals), health, and the Gym's own list of rule
breaks, which should be empty. Pass `DemoStores` to persist into stores the API serves.

Time comes from the Gym world's start date, never the clock, and every random draw is seeded, so a
run repeats exactly. It takes a few seconds for a few hundred customers over ten days.
"""

from __future__ import annotations

import importlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from autonomy.agents import (
    Analyst,
    BrandRules,
    BusinessFacts,
    Creative,
    Critic,
    Orchestrator,
    ScriptedLLM,
    Strategist,
)
from autonomy.channels import Dispatcher
from autonomy.contracts import (
    Alert,
    AutonomyLevel,
    Batch,
    BatchStatus,
    Brief,
    Briefing,
    Channel,
    ContentItem,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Goal,
    GoalKind,
    HealthStatus,
    Message,
    Objective,
    ProofSummary,
    SendReceipt,
    stable_id,
)
from autonomy.data import StateFolder as ProductionFolder
from autonomy.fakes import (
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryStateStore,
    InMemoryWorkspace,
)
from autonomy.gym import GYM_OFFERS, GymWorld, RuleBaseline, ScenarioSpec, get_scenario
from autonomy.gym.truth import DEFAULT_START
from autonomy.interfaces import (
    ChannelAdapter,
    DecisionLog,
    EventStore,
    JobQueue,
    Policy,
    StateFolder,
    StateStore,
    Workspace,
)
from autonomy.proof import Costs, group_assigner, proof_rewards, proof_summary
from autonomy.runtime.batches import BatchStore, InMemoryBatchStore
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig, TickReport, standard_breakers
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

__all__ = [
    "BUSINESS_ID",
    "DEMO_GOALS",
    "DemoDay",
    "DemoGoal",
    "DemoResult",
    "DemoStores",
    "DemoSystem",
    "GymBridge",
    "GymChannelAdapter",
    "SpendTrackingEventStore",
    "build_system",
    "demo_llm",
    "health_at",
    "live_demo_worker",
    "proof_at",
    "run_demo",
]

BUSINESS_ID = "demo_shop"
BUSINESS_NAME = "Demo Shop"
BRAND_VOICE = "Warm, plain and helpful. No pressure, no hype."
APPROVER = "demo-marketer"
AUTO_APPROVER = "demo-autoapprove"
DECIDE_HOUR = 9
"""The daily sweep runs at 09:00 UTC, when quiet hours (21:00-09:00) have just ended."""
HEALTH_WINDOW = timedelta(days=7)
PLAN_DAYS = 30
"""Spend pace is judged against the budget spread evenly over this many days."""
FALLBACK_MC_DRAWS = 200
"""Monte Carlo draws for the fallback policy's logged probabilities: enough for a demo, and fast."""


@dataclass(frozen=True)
class DemoGoal:
    text: str
    kind: GoalKind
    budget: float
    autonomy: AutonomyLevel


DEMO_GOALS: tuple[DemoGoal, ...] = (
    DemoGoal(
        "Win back customers who have not bought in two months",
        GoalKind.WIN_BACK,
        5000.0,
        AutonomyLevel.AUTOPILOT,
    ),
    DemoGoal(
        "Turn one-time buyers into repeat buyers", GoalKind.REPEAT_PURCHASE, 3000.0, AutonomyLevel.APPROVE
    ),
)
"""One goal on autopilot, one whose batches a person approves (the demo approves them itself)."""


# --- the scripted model ----------------------------------------------------------------------------

_NO_OFFER_COPY: dict[str, tuple[str, str]] = {
    "reminder": (
        "Something new in {{top_category}}",
        "Hi {{first_name}}, we have added new pieces to {{top_category}} since your last visit. Have a look when you have a minute.",
    ),
    "quality": (
        "Made to last",
        "Hi {{first_name}}, our {{top_category}} range is chosen for quality and made to last. Here is what is new.",
    ),
    "education": (
        "Getting more from {{top_category}}",
        "Hi {{first_name}}, a few simple tips for choosing and caring for {{top_category}}.",
    ),
    "personal": (
        "Picked for you",
        "Hi {{first_name}}, we picked a few {{top_category}} items we think you will like.",
    ),
    "loyalty": (
        "Thank you for shopping with us",
        "Hi {{first_name}}, thank you for being a regular. We are glad to have you.",
    ),
}
_OFFER_COPY: dict[str, tuple[str, str]] = {
    "savings": (
        "{{offer_label}} on {{top_category}}",
        "Hi {{first_name}}, enjoy {{offer_label}} on {{top_category}} this week.",
    ),
    "urgency": (
        "A good week for {{top_category}}",
        "Hi {{first_name}}, {{offer_label}} is yours on your next {{top_category}} order.",
    ),
    "convenience": (
        "{{offer_label}} on your next order",
        "Hi {{first_name}}, your next {{top_category}} order comes with {{offer_label}}.",
    ),
    "loyalty": (
        "A thank-you from us",
        "Hi {{first_name}}, as a thank-you for shopping with us: {{offer_label}}.",
    ),
    "novelty": (
        "Something new, with {{offer_label}}",
        "Hi {{first_name}}, try something new in {{top_category}} with {{offer_label}}.",
    ),
    "personal": (
        "Picked for you",
        "Hi {{first_name}}, we picked a few {{top_category}} items for you, with {{offer_label}}.",
    ),
}
_JUDGE_ANSWER = json.dumps(
    {
        "scores": [
            {"criterion": "brand_voice", "score": 5, "reason": "Plain, warm and on brand."},
            {"criterion": "honesty", "score": 5, "reason": "Mentions only the approved offer."},
        ]
    }
)
_CHANNEL = re.compile(r"^Channel: (\w+)\.", re.MULTILINE)
_THEME = re.compile(r"^Theme: (\w+)\.", re.MULTILINE)
_COUNT = re.compile(r"^Write (\d+) distinct variants\.", re.MULTILINE)


def _creative_answer(prompt: str) -> str:
    channel = _CHANNEL.search(prompt)
    theme = _THEME.search(prompt)
    count = _COUNT.search(prompt)
    with_offer = "\nOffer: " in prompt
    theme_name = theme.group(1) if theme else "reminder"
    book = _OFFER_COPY if with_offer else _NO_OFFER_COPY
    subject, body = book.get(theme_name) or (
        ("{{offer_label}} for you", "Hi {{first_name}}, {{offer_label}} on {{top_category}}.")
        if with_offer
        else ("News from us", "Hi {{first_name}}, here is what is new in {{top_category}}.")
    )
    is_email = channel is not None and channel.group(1) == Channel.EMAIL.value
    n = int(count.group(1)) if count else 1
    variants = []
    for i in range(n):
        text = body if i == 0 else f"{body} Variant {i + 1}."
        variants.append({"subject": subject if is_email else None, "body": text})
    return json.dumps({"variants": variants})


def _script(system: str, prompt: str) -> str:  # noqa: ARG001
    """Deterministic answers for every agent. Anything not scripted gets prose, which no agent can
    parse, so the Strategist and the Analyst fall back to their templates, as designed."""
    if "Score these criteria" in prompt:
        return _JUDGE_ANSWER
    if "distinct variants." in prompt:
        return _creative_answer(prompt)
    return "The demo model answers in prose here, so the agent uses its template."


def demo_llm() -> ScriptedLLM:
    """The scripted model every agent in the demo talks to. See `_script`."""
    return ScriptedLLM(_script, model_id="demo-scripted")


# --- stores ----------------------------------------------------------------------------------------


@dataclass
class DemoStores:
    """Where the demo writes. In memory by default; pass SQL stores to keep the run for the API."""

    workspace: Workspace = field(default_factory=InMemoryWorkspace)
    events: EventStore = field(default_factory=InMemoryEventStore)
    states: StateStore = field(default_factory=InMemoryStateStore)
    decisions: DecisionLog = field(default_factory=InMemoryDecisionLog)
    queue: JobQueue = field(default_factory=InMemoryJobQueue)
    batches: BatchStore = field(default_factory=InMemoryBatchStore)
    briefings: list[Briefing] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)


class SpendTrackingEventStore:
    """An `EventStore` that keeps a running total of message spend per goal as SENDs are stored.

    The dispatcher writes `goal_id` on every SEND it records, so the budget left and the spend-pace
    breaker can read spend without scanning the whole log on every tick."""

    def __init__(self, inner: EventStore, channel_costs: Mapping[Channel, float]) -> None:
        self.inner = inner
        self.channel_costs = dict(channel_costs)
        self.spend: dict[str, float] = defaultdict(float)
        self.sends = 0

    def append(self, events: Iterable[Event]) -> int:
        """Store the events; count the cost of each SEND the store did not already hold.

        Other events go to the store in one call (a Gym history is thousands of rows); SENDs go one
        at a time, because only the store knows whether it has seen one before."""
        rest: list[Event] = []
        sends: list[Event] = []
        for e in events:
            (sends if e.type is EventType.SEND else rest).append(e)
        new = self.inner.append(rest) if rest else 0
        for e in sends:
            if not self.inner.append([e]):
                continue
            new += 1
            self.sends += 1
            goal = e.attrs.get("goal_id")
            if isinstance(goal, str) and e.channel is not None:
                self.spend[goal] += float(self.channel_costs.get(e.channel, 0.0))
        return new

    def for_customer(
        self, customer_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Event]:
        return self.inner.for_customer(customer_id, since=since, until=until)

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        return self.inner.scan(since=since, until=until)

    def for_decision(self, decision_id: str) -> list[Event]:
        return self.inner.for_decision(decision_id)


# --- the Gym behind the channel seam ---------------------------------------------------------------


class GymBridge:
    """Carries dispatched messages into a Gym world and brings the world's events back.

    Messages are buffered and handed to the world in one batch before it next moves, so a day's
    sends share one random block (as the scoreboard's runs do). Each message is acted at its own
    `send_at`; the caller must not move the world past a send time before that send is flushed."""

    def __init__(self, world: GymWorld, decisions: DecisionLog, *, immediate: bool = False) -> None:
        self.world = world
        self.decisions = decisions
        self.immediate = immediate
        """Hand each message to the world at once (live mode, where the world lags the clock)."""
        self._pending: list[Decision] = []

    def adapters(self) -> dict[Channel, ChannelAdapter]:
        return {ch: GymChannelAdapter(ch, self) for ch in (Channel.EMAIL, Channel.WHATSAPP)}

    def submit(self, message: Message) -> SendReceipt:
        decision = self.decisions.get(message.decision_id)
        if decision is None or decision.action is None:
            return SendReceipt(accepted=False, error=f"no ACT decision {message.decision_id} in the log")
        action = decision.action.model_copy(update={"send_at": message.send_at})
        self._pending.append(decision.model_copy(update={"action": action}))
        if self.immediate:
            self.flush()
        return SendReceipt(accepted=True, provider_message_id=stable_id("gym", message.message_id))

    def flush(self) -> int:
        if not self._pending:
            return 0
        batch, self._pending = self._pending, []
        self.world.act(batch)
        return len(batch)

    def advance(self, until: datetime) -> list[Event]:
        """Flush, move the world to `until`, and return its events without its own SEND copies."""
        self.flush()
        return [e for e in self.world.advance(until) if e.type is not EventType.SEND]


class GymChannelAdapter:
    """A `ChannelAdapter` whose provider is the Gym world. It never receives webhooks."""

    def __init__(self, channel: Channel, bridge: GymBridge) -> None:
        self.channel = channel
        self.bridge = bridge

    def send(self, message: Message) -> SendReceipt:
        return self.bridge.submit(message)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:  # noqa: ARG002
        return []


# --- proof, health and the policy ------------------------------------------------------------------


def proof_at(
    decisions: DecisionLog,
    events: EventStore,
    costs: Costs,
    now: datetime,
    horizon_days: int,
    *,
    seed: int = 0,
) -> ProofSummary:
    """Programme-wide proof as of `now`, from decisions whose outcome window has closed by then."""
    cutoff = now - timedelta(days=horizon_days)
    closed = [d for d in decisions.scan(until=cutoff + timedelta(microseconds=1)) if d.made_at <= cutoff]
    by_customer = {c: events.for_customer(c, until=now) for c in sorted({d.customer_id for d in closed})}
    rewards = proof_rewards(closed, by_customer, horizon_days, Objective.GROSS_PROFIT, costs, now=now)
    return proof_summary(rewards, closed, now, None, seed=seed)


def health_at(
    events: EventStore, now: datetime, customers: int, alerts: Sequence[Alert] = ()
) -> HealthStatus:
    """Sending health over the last week: delivery, complaint, unsubscribe and block rates."""
    counts = Counter(e.type for e in events.scan(since=now - HEALTH_WINDOW, until=now))
    sends, delivers = counts[EventType.SEND], counts[EventType.DELIVER]

    def rate(n: int, d: int) -> float | None:
        return n / d if d else None

    return HealthStatus(
        as_of=now,
        delivery_rate=rate(delivers, sends),
        complaint_rate=rate(counts[EventType.COMPLAINT], delivers),
        unsubscribe_rate=rate(counts[EventType.UNSUBSCRIBE], delivers),
        block_rate=rate(counts[EventType.BLOCK], delivers),
        contacts_per_customer_week=rate(sends, customers),
        alerts=tuple(alerts),
    )


class _LearnsNightly:
    """Gives a plain `Policy` the runtime's `nightly(decisions, events_by_customer, now)` hook.

    Each night it hands the policy the rewards of decisions whose outcome window closed since the
    last night, so no outcome is learned twice."""

    def __init__(self, policy: Policy, horizon_days: int, costs: Costs) -> None:
        self.policy = policy
        self.version = policy.version
        self.horizon_days = horizon_days
        self.costs = costs
        self._last: datetime | None = None

    def decide(self, state: Any, ctx: Any) -> Decision:
        return self.policy.decide(state, ctx)

    def learn(self, rewards: Sequence[Any], decisions: Mapping[str, Decision]) -> None:
        self.policy.learn(rewards, decisions)

    def nightly(
        self, decisions: Sequence[Decision], events_by_customer: Mapping[str, Sequence[Event]], now: datetime
    ) -> None:
        from autonomy.proof import build_rewards

        since, self._last = self._last, now
        rewards = build_rewards(
            decisions, events_by_customer, self.horizon_days, Objective.GROSS_PROFIT, self.costs, now=now
        )
        fresh = [r for r in rewards if since is None or r.window_end > since]
        if fresh:
            self.policy.learn(fresh, {d.decision_id: d for d in decisions})


def policy_factory(
    kinds: Mapping[str, GoalKind], costs: Costs
) -> Callable[[Sequence[Brief], Sequence[ContentItem]], Policy]:
    """One goal's policy: the agent stream's `build_agent_policy` when it is installed, else the
    plain auction (`AutonomousPolicy` over one `GoalBidder`) with nightly learning added."""

    def build(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        try:  # written by another stream; looked up by name so the demo runs with or without it
            agent: Any = importlib.import_module("autonomy.runtime.agent")
        except ImportError:
            agent = None
        make = getattr(agent, "build_agent_policy", None)
        horizon = max((b.horizon_days for b in briefs), default=14)
        if make is not None:
            built = make(
                list(briefs),
                list(content),
                costs=dict(costs.channel),
                kinds=dict(kinds),
                baseline_policy=RuleBaseline(),
            )
            if callable(getattr(built, "nightly", None)):
                return cast(Policy, built)
            return cast(Policy, _LearnsNightly(cast(Policy, built), horizon, costs))
        from autonomy.decide import AutonomousPolicy, FatigueModel, GoalBidder

        bidders = [
            GoalBidder(b, content, costs=dict(costs.channel), kind=kinds.get(b.goal_id, GoalKind.CUSTOM))
            for b in briefs
        ]
        auction = AutonomousPolicy(
            bidders,
            FatigueModel().price,
            baseline_policy=RuleBaseline(),
            mc_draws=FALLBACK_MC_DRAWS,
            version="autonomy-demo-v1",
        )
        return cast(Policy, _LearnsNightly(auction, horizon, costs))

    return build


# --- the daily cycle -------------------------------------------------------------------------------


@dataclass
class DemoDay:
    """What the worker's tick job runs: the runtime's tick, the marketer's approvals, the briefing."""

    runtime: AutonomyRuntime
    stores: DemoStores
    events: SpendTrackingEventStore
    analyst: Analyst
    costs: Costs
    customers: int
    horizon_days: int
    seed: int = 0
    auto_approve: bool = True
    reports: list[TickReport] = field(default_factory=list)
    briefed_on: date | None = None
    """The day of the last daily briefing: a worker that ticks hourly still writes one a day."""

    def tick(self, now: datetime) -> list[TickReport]:
        reports = [self.runtime.tick(now)]
        if self.auto_approve:  # the demo plays the marketer who approves every batch it is shown
            pending = self.runtime.list_batches(statuses=(BatchStatus.PENDING,))
            for batch in pending:
                self.runtime.approve_batch(batch.batch_id, by=AUTO_APPROVER, now=now)
            if pending:  # a second tick at the same moment sends what was just approved
                reports.append(self.runtime.tick(now))
        for r in reports:
            self.stores.alerts.extend(r.alerts)
            self.stores.briefings.extend(r.briefings)
        self.reports.extend(reports)
        today = now.astimezone(UTC)
        if today.hour >= DECIDE_HOUR and self.briefed_on != today.date():  # after the daily sweep
            self.briefed_on = today.date()
            self.stores.briefings.append(self.daily_briefing(now, reports))
        return reports

    def daily_briefing(self, now: datetime, reports: Sequence[TickReport]) -> Briefing:
        proof = proof_at(
            self.stores.decisions, self.events, self.costs, now, self.horizon_days, seed=self.seed
        )
        alerts = [a for r in reports for a in r.alerts]
        health = health_at(self.events, now, self.customers, alerts)
        decided = sum(len(r.decisions) for r in reports)
        acts = sum(len(r.acts) for r in reports)
        facts = {
            "customers decided for": float(decided),
            "contacts chosen": float(acts),
            "customers left alone": float(decided - acts),
        }
        return self.analyst.briefing(None, proof, health, facts, "daily", now)


@dataclass
class DemoSystem:
    """Everything one demo run is made of, wired together. `run_demo` drives it day by day."""

    spec: ScenarioSpec
    start: datetime
    stores: DemoStores
    events: SpendTrackingEventStore
    world: GymWorld
    bridge: GymBridge
    dispatcher: Dispatcher
    runtime: AutonomyRuntime
    cycle: DemoDay
    worker: Worker
    goals: list[Goal]
    briefs: list[Brief]
    content: list[ContentItem]

    def step_to(self, t: datetime) -> None:
        """Move the world to `t`, store what happened, and finish every job due by then."""
        self.events.append(self.bridge.advance(t))
        self.worker.drain(t)
        self.bridge.flush()

    def day(self, now: datetime) -> list[TickReport]:
        """One simulated day: catch up to `now`, run the tick job, then each queued send on time."""
        self.step_to(now)
        before = len(self.cycle.reports)
        self.stores.queue.enqueue(tick_job(now))
        self.worker.drain(now)
        self.bridge.flush()
        reports = self.cycle.reports[before:]
        queued = [r.decision_id for rep in reports for r in rep.dispatch.queued]
        times = {_send_at(self.stores.decisions, d) for d in queued}
        for t in sorted(t for t in times if t is not None):
            self.step_to(t)
        return reports


def _send_at(decisions: DecisionLog, decision_id: str) -> datetime | None:
    d = decisions.get(decision_id)
    return d.action.send_at if d is not None and d.action is not None else None


def _address(customer_id: str, channel: Channel) -> str:
    """Every Gym customer has an address on every channel; consent decides who may be reached."""
    if channel is Channel.WHATSAPP:
        digits = int(stable_id("tel", customer_id)[4:14], 16) % 10**9
        return f"+1555{digits:09d}"
    return f"{customer_id}@customers.demo.invalid"


def _setup_goals(
    stores: DemoStores, start: datetime, customers: int
) -> tuple[list[Goal], list[Brief], Strategist, Analyst]:
    """Goal -> Strategist brief -> approval -> Creative library -> Critic -> active, for each demo goal."""
    llm = demo_llm()
    orchestrator = Orchestrator(stores.workspace)
    strategist = Strategist(llm)
    creative = Creative(llm)
    critic = Critic(llm, seed=0)
    facts = BusinessFacts(
        business_id=BUSINESS_ID,
        name=BUSINESS_NAME,
        channels=(Channel.EMAIL, Channel.WHATSAPP),
        offers=GYM_OFFERS,
        customer_count=customers,
    )
    goals: list[Goal] = []
    briefs: list[Brief] = []
    for spec in DEMO_GOALS:
        goal = Goal(
            goal_id=stable_id("goal", BUSINESS_ID, spec.kind.value),
            business_id=BUSINESS_ID,
            text=spec.text,
            kind=spec.kind,
            budget=spec.budget,
            starts_at=start,
            created_at=start,
        )
        orchestrator.create(goal)
        orchestrator.attach_brief(goal, strategist.draft_brief(goal, business_facts=facts, now=start))
        approved = orchestrator.approve(goal, approved_by=APPROVER, now=start, autonomy=spec.autonomy)
        drafts = creative.build_library(approved, brand_voice=BRAND_VOICE, n_per_cell=1, now=start)
        reviewed = critic.review(drafts, BrandRules.from_brief(approved, brand_voice=BRAND_VOICE))
        orchestrator.add_library(goal, reviewed)
        goals.append(orchestrator.activate(goal, now=start))
        briefs.append(approved)
    return goals, briefs, strategist, Analyst(llm)


def build_system(
    scenario: str = "baseline_shop",
    *,
    days: int = 30,
    customers: int = 1500,
    seed: int = 0,
    start: datetime = DEFAULT_START,
    stores: DemoStores | None = None,
    folder: StateFolder | None = None,
    live: bool = False,
) -> DemoSystem:
    """Build the agents' plan, the Gym world, the stores, the runtime and the worker.

    `live` hands each send to the world as soon as the worker runs it and lets the world catch up
    to `now` at each tick, for running on a wall-clock worker loop."""
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    stores = stores if stores is not None else DemoStores()
    folder = folder if folder is not None else ProductionFolder()
    goals, briefs, strategist, analyst = _setup_goals(stores, start, customers)
    content = [c for g in goals for c in stores.workspace.content(goal_id=g.goal_id)]
    spec = get_scenario(scenario, n_customers=customers, days=days, seed=seed)
    envelope = briefs[0].envelope
    world = GymWorld(
        spec,
        start=start,
        offers=GYM_OFFERS,
        content=content,
        quiet_start_hour=envelope.quiet_start_hour,
        quiet_end_hour=envelope.quiet_end_hour,
        contact_cap=min(b.envelope.max_contacts_per_week for b in briefs),
    )
    costs = Costs(
        channel=dict(spec.channel_cost),
        offer={o.offer_id: o.cost_per_redemption for o in GYM_OFFERS},
        margin_rate=spec.margin_rate,
    )
    events = SpendTrackingEventStore(stores.events, costs.channel)
    events.append(world.reset(seed))
    bridge = GymBridge(world, stores.decisions, immediate=live)
    adapters = bridge.adapters()
    dispatcher = Dispatcher(adapters, _address, stores.workspace, events, stores.queue)
    budgets = {g.goal_id: g.budget for g in goals}
    horizon = max(b.horizon_days for b in briefs)

    def spend(goal_id: str, now: datetime) -> float:  # noqa: ARG001
        return events.spend[goal_id]

    def planned(goal_id: str, now: datetime) -> float:
        elapsed = max(0.0, (now - start) / timedelta(days=1))
        return budgets.get(goal_id, 0.0) * min(1.0, elapsed / PLAN_DAYS)

    def weekly_briefing(goal: Goal, brief: Brief, now: datetime) -> Briefing:  # noqa: ARG001
        proof = proof_at(stores.decisions, events, costs, now, horizon, seed=seed)
        return analyst.briefing(goal, proof, health_at(events, now, customers), {}, "weekly", now)

    runtime = AutonomyRuntime(
        events=events,
        states=stores.states,
        decisions=stores.decisions,
        workspace=stores.workspace,
        folder=folder,
        policy_factory=policy_factory({g.goal_id: g.kind for g in goals}, costs),
        queue=stores.queue,
        dispatcher=dispatcher,
        batches=stores.batches,
        breakers_fn=standard_breakers(spend_fn=spend, planned_fn=planned),
        analyst=weekly_briefing,
        strategist=strategist.adjust_brief,
        spend_fn=spend,
        assign_group=group_assigner(BUSINESS_ID, envelope=envelope),
        # Gym events never arrive late, so the nightly full rebuild of state would only cost time.
        config=RuntimeConfig(sweep_hour=DECIDE_HOUR, seed=seed, worker="demo-runtime", nightly_rebuild=False),
    )
    cycle = DemoDay(
        runtime=runtime,
        stores=stores,
        events=events,
        analyst=analyst,
        costs=costs,
        customers=customers,
        horizon_days=horizon,
        seed=seed,
    )

    def tick(now: datetime) -> None:
        if live:  # the world catches up to the wall clock before the runtime looks at it
            events.append(bridge.advance(now))
        cycle.tick(now)

    worker = Worker(
        stores.queue,
        {
            TICK_KIND: tick_handler(tick, stores.queue),
            SEND_KIND: send_handler(dispatcher, stores.decisions, stores.states),
            WEBHOOK_KIND: webhook_handler(adapters, events.append),
        },
        name="demo-worker",
    )
    return DemoSystem(
        spec=spec,
        start=start,
        stores=stores,
        events=events,
        world=world,
        bridge=bridge,
        dispatcher=dispatcher,
        runtime=runtime,
        cycle=cycle,
        worker=worker,
        goals=goals,
        briefs=briefs,
        content=content,
    )


# --- running it ----------------------------------------------------------------------------------------


@dataclass
class DemoResult:
    """Everything a demo run produced. `summary()` says it in a few plain lines."""

    scenario: str
    days: int
    customers: int
    seed: int
    start: datetime
    end: datetime
    goals: list[Goal]
    briefs: list[Brief]
    content: list[ContentItem]
    decisions: list[Decision]
    events: list[Event]
    batches: list[Batch]
    briefings: list[Briefing]
    alerts: list[Alert]
    proof: ProofSummary
    health: HealthStatus
    violations: list[str]
    """The Gym's own record of contacts that broke a rule (no consent, unsubscribed, quiet hours)."""
    sent: int
    """Messages the dispatcher sent (SEND events it recorded)."""
    contacts: int
    """Contacts that landed in the Gym world."""
    sleeping_dogs_contacted: float
    reports: list[TickReport]
    stores: DemoStores

    def summary(self) -> str:
        acts = sum(1 for d in self.decisions if d.kind is DecisionKind.ACT)
        lines = [
            f"{self.scenario}: {self.customers:,} customers, {self.days} days from {self.start:%Y-%m-%d}.",
            f"{len(self.decisions):,} decisions ({acts:,} to contact), {self.sent:,} messages sent, "
            f"{self.contacts:,} contacts landed in the Gym.",
            f"{len(self.batches)} batches held for approval, {len(self.briefings)} briefings, "
            f"{len(self.alerts)} alerts, {len(self.violations)} rule breaks.",
        ]
        if self.proof.incremental_profit is not None:
            p = self.proof.incremental_profit
            lines.append(
                f"Incremental profit vs holdout: {p.point:,.2f} (95% {p.low:,.2f} to {p.high:,.2f})."
            )
        else:
            lines.append("No proof claim yet: " + (" ".join(self.proof.notes) or "no closed outcomes."))
        return "\n".join(lines)


def run_demo(
    scenario: str = "baseline_shop",
    days: int = 30,
    customers: int = 1500,
    seed: int = 0,
    *,
    start: datetime = DEFAULT_START,
    stores: DemoStores | None = None,
    folder: StateFolder | None = None,
) -> DemoResult:
    """Run the whole system on a Gym shop for `days` days; see the module docstring."""
    if days < 1:
        raise ValueError("days must be at least 1")
    system = build_system(
        scenario, days=days, customers=customers, seed=seed, start=start, stores=stores, folder=folder
    )
    # The first daily sweep at or after `start` (an earlier one would come before the goals start).
    first = start.astimezone(UTC).replace(hour=DECIDE_HOUR, minute=0, second=0, microsecond=0)
    if first < start:
        first += timedelta(days=1)
    for day in range(days):
        system.day(first + timedelta(days=day))
    end = first + timedelta(days=days)
    system.step_to(end)
    st = system.stores
    cycle = system.cycle
    proof = proof_at(st.decisions, system.events, cycle.costs, end, cycle.horizon_days, seed=seed)
    health = health_at(system.events, end, customers)
    decisions = list(st.decisions.scan())
    acts = sum(1 for d in decisions if d.kind is DecisionKind.ACT)
    facts = {"decisions made": float(len(decisions)), "contacts chosen": float(acts)}
    st.briefings.append(cycle.analyst.briefing(None, proof, health, facts, "weekly", end))
    return DemoResult(
        scenario=scenario,
        days=days,
        customers=customers,
        seed=seed,
        start=start,
        end=end,
        goals=list(st.workspace.goals()),
        briefs=system.briefs,
        content=system.content,
        decisions=decisions,
        events=list(system.events.scan()),
        batches=st.batches.list_batches(),
        briefings=list(st.briefings),
        alerts=list(st.alerts),
        proof=proof,
        health=health,
        violations=system.world.violations(),
        sent=system.events.sends,
        contacts=len(system.world.contacts),
        sleeping_dogs_contacted=system.world.sleeping_dogs_contacted_share(),
        reports=list(cycle.reports),
        stores=st,
    )


def live_demo_worker(
    *, name: str = "demo-worker", start: datetime, customers: int = 300, tick_minutes: int = 60
) -> Worker:
    """A worker over a Gym shop that starts at `start`, for `python -m autonomy.runtime.worker --demo`.

    Sends go into the world as soon as the worker runs them, and the world catches up to the wall
    clock at each tick, so the demo runs in real time on the worker's loop. The repeating tick job
    is seeded at `start`, every `tick_minutes`."""
    system = build_system(customers=customers, start=start, live=True)
    system.worker.name = name
    system.stores.queue.enqueue(tick_job(start, every_minutes=tick_minutes))
    return system.worker
