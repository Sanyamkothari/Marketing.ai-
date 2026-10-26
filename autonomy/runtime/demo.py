"""The whole system end to end, in simulation: a Gym shop run by Autonomy for `days` days.

`run_demo()` is the one place where every v2 piece meets, with nothing real on the other end:

- **The environment is a Gym world.** A `GymBridge` puts it behind the channel seam: the dispatcher
  sends through `GymChannelAdapter`s exactly as it would through SES or WhatsApp, the bridge hands
  those messages to the world, and the world's events (deliveries, clicks, purchases, unsubscribes)
  flow back into the event store. The world's own SEND events are dropped, because the dispatcher
  has already recorded the one SEND each message gets.
- **The agents run on a scripted model** (`demo_llm()`), so the outer loop is deterministic and
  offline: the Strategist falls back to its template briefs and keeps the plan as it is each week,
  the Creative writes from a fixed copy book that passes the Critic's rules, the Critic's judge
  approves, and the Analyst writes its template briefing. Every number still comes from code.
- **Two goals, two autonomy levels.** "Win back lapsed customers" runs on AUTOPILOT; "remind
  customers to reorder before they run out" (the refill goal) runs at APPROVE, so its ACT decisions
  are held in a pending `Batch`. The demo plays the marketer and approves each batch, and the
  runtime sends it. For the API's demo, `approve_goal_days` starts the APPROVE goal late (a second
  goal the marketer added recently) and `hold_last_batches` leaves the last two days' batches
  pending, so the app has batches to show.
- **The hero example** (`hero=True`, the default; docs/v2/HERO_EXAMPLE.md): "bought a serum, viewed
  moisturisers, nears the refill: refill reminder vs education vs complementary product vs free
  shipping vs nothing, chosen per person by incremental profit". The Gym simulates categories, not
  products, so a **demo product layer** (`ProductLayer`) gives every Gym purchase and visit a product
  from a small skincare-led catalogue (`HERO_PRODUCTS`), deterministically by customer and category:
  each customer has a usual product per category, with its complements now and then. From that
  history the `HeroLayer` builds the `Catalogue`, the refill `CycleModel` and the `CoPurchaseModel`
  (refitted weekly, never on purchases after `now`), and enriches every customer's state with the
  refill scores before each sweep (through the runtime's `state_enricher` hook when it has one,
  otherwise through the state store the runtime reads). The Creative gets an approved asset library
  (`hero_assets`: four images with alt text, placeholder URLs on a reserved domain), so image and
  carousel variants exist where a channel carries them. A `Mandate` signed by two people (a
  marketing lead and an approver; 20% margin floor) covers both goals: the briefs' envelopes come
  from `mandate_to_envelope`, options the mandate rules out for a customer are removed before the
  auction (so logged probabilities stay exact), and every decision is checked with `check_action`.
  The Gym still answers a message by its channel, offer and theme only: it does not know products,
  so a refill reminder works in the Gym as well as a reminder does, whatever the product.
- **Costs are close to real ones** (`DEMO_CHANNEL_COST`): a tenth of a cent per email, four cents
  per WhatsApp marketing message, and the offers' own cost per redemption, so spend and return on
  spend are numbers a marketer would believe.
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

import argparse
import hashlib
import importlib
import inspect
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast

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
from autonomy.assets import Asset, AssetKind, AssetStatus
from autonomy.catalog import REFILL_KEYS, Catalogue, CoPurchaseModel, CycleModel, RefillPick, enrich_state
from autonomy.channels import Dispatcher
from autonomy.contracts import (
    Action,
    Alert,
    Audience,
    AudienceFilter,
    AutonomyLevel,
    Batch,
    BatchStatus,
    Bid,
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
    Format,
    Goal,
    GoalKind,
    Group,
    HealthStatus,
    Message,
    Objective,
    OfferOption,
    ProofSummary,
    Reward,
    SendReceipt,
    Theme,
    stable_id,
)
from autonomy.data import StateFolder as ProductionFolder
from autonomy.decide.bidders import needs_item
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
from autonomy.mandate import (
    ActionCosts,
    FrequencyCap,
    HmacSigner,
    KeyRing,
    Mandate,
    MandateLedger,
    MandateStatus,
    QuietHours,
    SignaturePolicy,
    SignerRole,
    SpendToDate,
    check_action,
    mandate_to_envelope,
    sign_mandate,
    verify_mandate,
    with_signature,
)
from autonomy.proof import Costs, group_assigner, proof_rewards, proof_summary
from autonomy.runtime.batches import BatchStore, InMemoryBatchStore
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig, SpendFn, TickReport, standard_breakers
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
    "DEMO_FIRST_NAMES",
    "DEMO_GOALS",
    "HERO_GOALS",
    "HERO_PRODUCTS",
    "DemoDay",
    "DemoGoal",
    "DemoProduct",
    "DemoResult",
    "DemoStores",
    "DemoSystem",
    "GymBridge",
    "GymChannelAdapter",
    "HeroLayer",
    "HeroNote",
    "HeroPolicy",
    "MandateCheck",
    "MandateSetup",
    "ProductLayer",
    "SpendTrackingEventStore",
    "build_system",
    "certify_mandate",
    "demo_catalogue",
    "demo_first_name",
    "demo_llm",
    "goal_pacing",
    "health_at",
    "hero_assets",
    "hero_story",
    "live_demo_worker",
    "main",
    "product_name",
    "proof_at",
    "run_demo",
    "with_first_names",
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
DEMO_CHANNEL_COST: dict[Channel, float] = {Channel.EMAIL: 0.001, Channel.WHATSAPP: 0.04}
"""What one message costs. Email: about $1 per thousand through an email service. WhatsApp: Meta
charges per marketing-template message, by country, from about $0.01 (India) to over $0.10
(Germany); $0.04 sits between the US and UK rates. Offers cost what each `OfferOption` says per
redemption (free shipping $4, 10% off $6)."""


@dataclass(frozen=True)
class DemoGoal:
    text: str
    kind: GoalKind
    budget: float
    autonomy: AutonomyLevel


_WIN_BACK = DemoGoal(
    "Win back customers who have not bought in two months",
    GoalKind.WIN_BACK,
    5000.0,
    AutonomyLevel.AUTOPILOT,
)
DEMO_GOALS: tuple[DemoGoal, ...] = (
    _WIN_BACK,
    DemoGoal(
        "Turn one-time buyers into repeat buyers", GoalKind.REPEAT_PURCHASE, 3000.0, AutonomyLevel.APPROVE
    ),
)
"""One goal on autopilot, one whose batches a person approves (the demo approves them itself).
The goals of a run with `hero=False`."""

HERO_GOALS: tuple[DemoGoal, ...] = (
    _WIN_BACK,
    DemoGoal(
        "Remind customers to reorder before they run out",
        GoalKind.REPEAT_PURCHASE,
        3000.0,
        AutonomyLevel.APPROVE,
    ),
)
"""The hero run's goals: win-back on autopilot and the refill goal at APPROVE."""

REFILL_AUDIENCE = Audience(
    description=(
        "Due to refill something they buy again: refill due within a week, or up to four weeks overdue"
    ),
    filters=(
        AudienceFilter(field="scores.refill_due_days", op="lte", value=7),
        AudienceFilter(field="scores.refill_due_days", op="gte", value=-28),
    ),
)
"""The refill goal's audience, written on the refill scores the `HeroLayer` puts on each state. The
marketer's edit of the Strategist's template audience (one-time buyers) before approving the brief.
Someone more than four weeks overdue has most likely stopped using the product: win-back's job."""


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
_REFILL_COPY: dict[str, tuple[str, str]] = {
    "reminder": (
        "Running low on your {{recommended_product}}?",
        "Hi {{first_name}}, your {{recommended_product}} is probably nearly finished by now. Reorder it whenever suits you.",
    ),
    "education": (
        "Getting the most from your {{recommended_product}}",
        "Hi {{first_name}}, a few simple tips for getting the most from your {{recommended_product}}.",
    ),
    "quality": (
        "Made to last",
        "Hi {{first_name}}, your {{recommended_product}} is one of our most reordered products. Here is why people stay with it.",
    ),
    "personal": (
        "Goes well with your {{recommended_product}}",
        "Hi {{first_name}}, we picked a few things that go well with your {{recommended_product}}.",
    ),
    "convenience": (
        "{{offer_label}} on your next {{recommended_product}}",
        "Hi {{first_name}}, your next {{recommended_product}} comes with {{offer_label}} this week.",
    ),
}
"""The refill goal's copy (its audience says "refill"). `{{recommended_product}}` is filled at send time
with the product the decision is about (`Action.item_id`), here the customer's refill pick."""
_PERSONAL_COPY: tuple[str, str] = (
    "Picked for you",
    "Hi {{first_name}}, people who bought what you did often like {{recommended_product}}. Have a look when you have a minute.",
)
"""Win-back's complementary-product message: `{{recommended_product}}` is the complement."""
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
_AUDIENCE = re.compile(r"^Audience: (.*)$", re.MULTILINE)
_KEEP_CHANNELS = re.compile(r"^- channels: (.*)$", re.MULTILINE)
_KEEP_OFFER = re.compile(r"^  - (\w+): ", re.MULTILINE)
_KEEP_CAP = re.compile(r"^- weekly cap: (\d+)$", re.MULTILINE)
_KEEP_REASON = (
    "The agent is still learning who responds to what, and the plan's limits are holding, "
    "so the plan stays as the marketer approved it."
)


def _adjust_answer(prompt: str) -> str:
    """The Strategist's weekly review: keep every channel, offer, follow-up and the weekly cap.

    Without this the scripted model would answer in prose, and the brief's rationale (shown on the
    Plan screen) would carry the parse error instead of a reason."""
    channels = _KEEP_CHANNELS.search(prompt)
    cap = _KEEP_CAP.search(prompt)
    return json.dumps(
        {
            "keep_channels": [c.strip() for c in channels.group(1).split(",")] if channels else [],
            "keep_offer_ids": _KEEP_OFFER.findall(prompt),
            "max_contacts_per_week": int(cap.group(1)) if cap else 0,
            "keep_follow_up_days": None,
            "rationale": _KEEP_REASON,
        }
    )


def _creative_answer(prompt: str) -> str:
    channel = _CHANNEL.search(prompt)
    theme = _THEME.search(prompt)
    count = _COUNT.search(prompt)
    audience = _AUDIENCE.search(prompt)
    with_offer = "\nOffer: " in prompt
    theme_name = theme.group(1) if theme else "reminder"
    book = _OFFER_COPY if with_offer else _NO_OFFER_COPY
    refill = audience is not None and "refill" in audience.group(1).lower()
    found = book.get(theme_name)
    if refill and (theme_name in _REFILL_COPY) and (with_offer == (theme_name == "convenience")):
        found = _REFILL_COPY[theme_name]
    elif theme_name == "personal" and not with_offer:
        found = _PERSONAL_COPY
    subject, body = found or (
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
    parse, so the Strategist's first brief and the Analyst fall back to their templates, as
    designed. The Strategist's weekly review is scripted to keep the plan unchanged."""
    if "Score these criteria" in prompt:
        return _JUDGE_ANSWER
    if "distinct variants." in prompt:
        return _creative_answer(prompt)
    if prompt.startswith("Adjust this brief for the coming week"):
        return _adjust_answer(prompt)
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


def goal_pacing(goals: Iterable[Goal], start: datetime, *, plan_days: int = PLAN_DAYS) -> SpendFn:
    """Planned spend to date for the demo's goals: each budget spread evenly over `plan_days`.

    Elapsed time counts from each goal's own `starts_at` (falling back to `start`), so a goal added
    part-way through has had no time to spend yet and the spend-pace breaker can still catch it.
    The simulation and the API demo both pace with this, so they match each other."""
    listed = list(goals)
    budgets = {g.goal_id: g.budget for g in listed}
    goal_start: dict[str, datetime] = {g.goal_id: g.starts_at for g in listed}

    def planned(goal_id: str, now: datetime) -> float:
        elapsed = max(0.0, (now - goal_start.get(goal_id, start)) / timedelta(days=1))
        return budgets.get(goal_id, 0.0) * min(1.0, elapsed / plan_days)

    return planned


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
            goal = e.attrs.get("goal_id")
            if not isinstance(goal, str):
                continue  # a past campaign from the shop's own history: not ours, not our spend
            self.sends += 1
            if e.channel is not None:
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

    def __init__(
        self,
        world: GymWorld,
        decisions: DecisionLog,
        *,
        immediate: bool = False,
        relabel: Callable[[Sequence[Event]], list[Event]] | None = None,
    ) -> None:
        self.world = world
        self.decisions = decisions
        self.immediate = immediate
        """Hand each message to the world at once (live mode, where the world lags the clock)."""
        self.relabel = relabel
        """Applied to the world's events before they are stored (the hero run's product layer)."""
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
        from autonomy.gym.score import policy_view

        seen = policy_view([e for e in self.world.advance(until) if e.type is not EventType.SEND])
        return self.relabel(seen) if self.relabel is not None else seen


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
    kinds: Mapping[str, GoalKind], costs: Costs, *, hero: HeroLayer | None = None
) -> Callable[[Sequence[Brief], Sequence[ContentItem]], Policy]:
    """One goal's policy: the agent stream's `build_agent_policy` when it is installed, else the
    plain auction (`AutonomousPolicy` over one `GoalBidder`) with nightly learning added.

    With `hero`, the agent also gets the products and assets it accepts (`HeroLayer.agent_kwargs`),
    and the policy is wrapped in a `HeroPolicy` (refill scores, the mandate, products, reasons)."""

    def build(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        try:  # looked up by name so the demo still runs (on the plain auction) without the agent
            agent: Any = importlib.import_module("autonomy.runtime.agent")
        except ImportError:
            agent = None
        make = getattr(agent, "build_agent_policy", None)
        horizon = max((b.horizon_days for b in briefs), default=14)
        extra = hero.agent_kwargs(make) if hero is not None and make is not None else {}
        policy: Policy
        if make is not None:
            built = make(
                list(briefs),
                list(content),
                costs=dict(costs.channel),
                kinds=dict(kinds),
                baseline_policy=RuleBaseline(),
                **extra,
            )
            if callable(getattr(built, "nightly", None)):
                policy = cast(Policy, built)
            else:
                policy = cast(Policy, _LearnsNightly(cast(Policy, built), horizon, costs))
        else:
            from autonomy.decide import AutonomousPolicy, FatigueModel, GoalBidder

            # With the hero, the bidders read the (gated) library from the context, not a fixed copy.
            library = None if hero is not None else content
            bidders = [
                GoalBidder(b, library, costs=dict(costs.channel), kind=kinds.get(b.goal_id, GoalKind.CUSTOM))
                for b in briefs
            ]
            auction = AutonomousPolicy(
                bidders,
                FatigueModel().price,
                baseline_policy=RuleBaseline(),
                mc_draws=FALLBACK_MC_DRAWS,
                version="autonomy-demo-v1",
            )
            policy = cast(Policy, _LearnsNightly(auction, horizon, costs))
        if hero is None or not briefs:
            return policy
        return cast(Policy, HeroPolicy(policy, hero, briefs[0].goal_id, fills_items="item_lookup" in extra))

    return build


# --- the hero example: products, refills, formats and a signed mandate ------------------------------


@dataclass(frozen=True)
class DemoProduct:
    """One product of the demo shop (the demo's product layer; the Gym itself has no products).

    `weight` says how often the product is a customer's usual pick in its category, and `goes_with`
    lists the products in the same category bought alongside it now and then (its complements)."""

    item_id: str
    name: str
    category: str
    replenishable: bool
    weight: float = 1.0
    goes_with: tuple[str, ...] = ()


HERO_PRODUCTS: tuple[DemoProduct, ...] = (
    DemoProduct("serum_vitc", "Vitamin C serum", "beauty", True, 4.0, ("moisturiser_daily", "spf50")),
    DemoProduct(
        "moisturiser_daily", "Daily moisturiser", "beauty", True, 3.0, ("serum_vitc", "cleanser_gentle")
    ),
    DemoProduct("cleanser_gentle", "Gentle cleanser", "beauty", True, 2.0, ("moisturiser_daily",)),
    DemoProduct("spf50", "SPF 50 sunscreen", "beauty", True, 1.0, ("moisturiser_daily",)),
    DemoProduct("candle_soy", "Soy candle", "home", True, 2.0, ("diffuser_refill",)),
    DemoProduct("diffuser_refill", "Reed diffuser refill", "home", True, 1.0, ("candle_soy",)),
    DemoProduct("throw_linen", "Linen throw", "home", False, 1.0, ("candle_soy",)),
    DemoProduct("socks_bamboo", "Bamboo socks", "apparel", False, 2.0, ("tee_organic",)),
    DemoProduct("tee_organic", "Organic cotton tee", "apparel", False, 2.0, ("socks_bamboo",)),
    DemoProduct("robe_waffle", "Waffle robe", "apparel", False, 1.0, ("socks_bamboo",)),
    DemoProduct("bottle_steel", "Steel water bottle", "outdoor", False, 2.0, ("bars_trail",)),
    DemoProduct("bars_trail", "Trail energy bars", "outdoor", True, 2.0, ("bottle_steel", "lipbalm_spf")),
    DemoProduct("lipbalm_spf", "SPF lip balm", "outdoor", True, 1.0, ("bars_trail",)),
    DemoProduct("earbuds", "Wireless earbuds", "electronics", False, 2.0, ("case_phone", "cable_usbc")),
    DemoProduct("cable_usbc", "USB-C cable", "electronics", False, 2.0, ("earbuds",)),
    DemoProduct("case_phone", "Phone case", "electronics", False, 1.0, ("cable_usbc",)),
)
"""A small skincare-led catalogue over the Gym's five categories. Replenishable products are the ones
people run out of (serum, moisturiser, candles, energy bars); the rest are bought once in a while.
Prices and typical cycles are not stated: the `Catalogue` infers them from the labelled purchases."""

USUAL_SHARE = 0.7
"""A labelled purchase is the customer's usual product in its category this often, and one of that
product's complements otherwise (so complements turn up on the same shopping trips)."""
VISIT_OWN_SHARE = 0.3
"""A visit after a purchase looks at the product bought last this often, else at one of its complements
(the serum buyer who keeps looking at moisturisers)."""
REFIT_EVERY = timedelta(days=7)
"""The catalogue's models (refill cycles, co-purchase) are refitted weekly, on purchases up to then."""
COMPLEMENT_CANDIDATES = 3
"""Of the recommender's top picks, the one the customer looked at most is the complement (ties keep
the recommender's order)."""
MANDATE_MARGIN_FLOOR = 0.2
"""The mandate's margin floor: an order's margin after the offer's cost must stay at or above 20% of
its revenue. At the Gym's 40% margin, 10% off ($6 per redemption) clears it only on orders of $30 or
more, and free shipping ($4) on orders of $20 or more."""
MANDATE_DAILY_SPEND = 500.0
MANDATE_VALID = timedelta(days=365)
MANDATE_SIGNERS: tuple[tuple[str, SignerRole], ...] = (
    ("asha.marketing-lead", SignerRole.MARKETING_LEAD),
    ("ravi.approver", SignerRole.APPROVER),
)
"""Who signs the demo's mandate: two different people, as the two-person rule asks (no finance
signature: the budget ceiling is under the policy's 10,000 threshold)."""
ASSET_HOST = "https://assets.demo.invalid/hero"
"""Placeholder image URLs on a reserved domain (RFC 2606): nothing is ever fetched from it."""


def _unit(*parts: object) -> float:
    """A deterministic number in [0, 1) from its parts (a stable hash), for the product layer."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:13], 16) / float(16**13)


# --- what people read: first names for the demo's customers, names for its products ---------------

DEMO_FIRST_NAMES: tuple[str, ...] = (
    # Africa
    "Amara", "Kwame", "Zainab", "Tendai", "Chidi", "Nia", "Kofi", "Ayana", "Thabo", "Fatou",
    "Sipho", "Adaeze", "Jabari", "Imani", "Lerato", "Oluwaseun", "Makena", "Tariq", "Nala", "Kagiso",
    "Ama", "Babajide", "Wanjiru", "Moussa", "Folake", "Yaw", "Ifeoma", "Bongani", "Esi", "Abebe",
    # Middle East and North Africa
    "Layla", "Omar", "Yasmin", "Karim", "Noor", "Samir", "Rania", "Hassan", "Leila", "Youssef",
    "Dalia", "Rami", "Salma", "Ziad", "Mariam", "Tamar", "Eitan", "Shira", "Arash", "Darya",
    # South Asia
    "Priya", "Arjun", "Ananya", "Rohan", "Kavya", "Vikram", "Meera", "Aditya", "Isha", "Sanjay",
    "Divya", "Rahul", "Nisha", "Farhan", "Ayesha", "Imran", "Sana", "Nimal", "Tharushi", "Anil",
    "Lakshmi", "Dev", "Pooja", "Ravi", "Shreya",
    # East and South-East Asia
    "Mei", "Wei", "Yuki", "Haruto", "Sakura", "Kenji", "Hana", "Jun", "Min-jun", "Seo-yeon",
    "Ji-woo", "Linh", "Minh", "Thanh", "Anh", "Siti", "Budi", "Putri", "Arif", "Dewi",
    "Malee", "Niran", "Ploy", "Jasmine", "Rizal", "Mai", "Xiu", "Chen", "Hiro", "Aiko",
    # Europe
    "Sofia", "Lukas", "Emma", "Matteo", "Chloé", "Lars", "Ingrid", "Sven", "Freya", "Oskar",
    "Elena", "Mateusz", "Zofia", "Nikos", "Eleni", "Dmitri", "Anya", "Ivan", "Katya", "Bram",
    "Femke", "Joost", "Siobhan", "Liam", "Aoife", "Niamh", "Rhys", "Cerys", "Hamish", "Isla",
    "Pablo", "Lucía", "Javier", "Inés", "Tiago", "Beatriz", "Giulia", "Luca", "Chiara", "Marco",
    "Léa", "Hugo", "Camille", "Élodie", "Jonas", "Leonie", "Felix", "Mila", "Annika", "Henrik",
    # The Americas
    "Valentina", "Santiago", "Camila", "Diego", "Mariana", "Mateo", "Gabriela", "Joaquín", "Lucas",
    "Ximena", "Rafael", "Daniela", "Thiago", "Fernanda", "Andrés", "Paola", "Emilio", "Renata",
    "Ethan", "Olivia", "Noah", "Ava", "Maya", "Caleb", "Harper", "Jordan", "Riley", "Marcus",
    "Keisha", "Darnell", "Tyrone", "Aaliyah", "Dakota", "Sequoia", "Talia", "Grace", "Owen", "Zoe",
    # Oceania
    "Aroha", "Tama", "Mere", "Wiremu", "Leilani", "Kai", "Malia", "Sione", "Ruby", "Jack",
)  # fmt: skip
"""About 200 first names from around the world, for the demo's simulated customers. First names
only: no surnames and no addresses, so nothing here reads as a real person's details."""


def demo_first_name(customer_id: str) -> str:
    """The demo customer's first name: the same for the same id on every run (a stable hash of it)."""
    return DEMO_FIRST_NAMES[int(_unit("first_name", customer_id) * len(DEMO_FIRST_NAMES))]


def with_first_names(events: Iterable[Event]) -> list[Event]:
    """The events with a `first_name` on every signup that has none, as a shop's sign-up data has.

    The Gym simulates customers by id only. The API never makes a name up (it shows the one the
    data has), so the demo puts one in its data here, at the edge, and nowhere else."""
    out: list[Event] = []
    for e in events:
        if e.type is EventType.SIGNUP and not e.attrs.get("first_name"):
            e = e.model_copy(update={"attrs": {**e.attrs, "first_name": demo_first_name(e.customer_id)}})
        out.append(e)
    return out


def product_name(item_id: str, products: Sequence[DemoProduct] = HERO_PRODUCTS) -> str:
    """The product's name in the demo shop ("Organic cotton tee"), else its id in words ("Tee organic")."""
    for p in products:
        if p.item_id == item_id:
            return p.name
    words = " ".join(item_id.replace("-", " ").replace("_", " ").split())
    return words[:1].upper() + words[1:] if words else item_id


def demo_catalogue(
    events: Iterable[Event] = (), products: Sequence[DemoProduct] = HERO_PRODUCTS
) -> dict[str, str]:
    """item_id -> name for every demo product and every other item the events mention, for display."""
    names = {p.item_id: p.name for p in products}
    for e in events:
        if e.item_id and e.item_id not in names:
            names[e.item_id] = product_name(e.item_id, products)
    return names


def hero_assets(now: datetime) -> list[Asset]:
    """The brand's approved images for the hero run: brand pictures that show no single product, so
    a message about one product never shows another (alt text on every one)."""

    def image(asset_id: str, alt: str, themes: tuple[Theme, ...]) -> Asset:
        return Asset(
            asset_id=asset_id,
            kind=AssetKind.IMAGE,
            url=f"{ASSET_HOST}/{asset_id}.jpg",
            alt_text=alt,
            width=1200,
            height=628,
            size_bytes=180_000,
            mime_type="image/jpeg",
            themes=themes,
            status=AssetStatus.APPROVED,
            approved_by=APPROVER,
            created_at=now,
        )

    return [
        image(
            "hero_counter",
            "The Demo Shop counter, with shelves of skincare, candles and everyday goods behind it.",
            (),
        ),
        image(
            "hero_refill_shelf",
            "A wooden shelf of glass bottles and refill pouches, lined up ready to be topped up.",
            (Theme.REMINDER, Theme.EDUCATION, Theme.QUALITY),
        ),
        image(
            "hero_parcel",
            "A brown paper parcel tied with string, waiting on a doorstep.",
            (Theme.CONVENIENCE, Theme.SAVINGS, Theme.URGENCY),
        ),
        image(
            "hero_gift_note",
            "A handwritten thank-you card on tissue paper inside an open gift box.",
            (Theme.PERSONAL, Theme.LOYALTY),
        ),
    ]


class ProductLayer:
    """Gives the Gym's purchases and visits a product, deterministically (a demo layer).

    The Gym simulates customers who buy in categories; it has no products. Each customer gets a
    usual product per category (drawn once from the category's `weight`s by a stable hash of the
    customer, the category and the seed). A purchase is that usual product `USUAL_SHARE` of the time
    and one of its complements otherwise. A visit after a purchase looks at the product bought last,
    or at one of its complements; a visit before any purchase looks at any product. Nothing here
    reads the Gym's truth: only the events' own customer, category, time and id.
    """

    def __init__(self, products: Sequence[DemoProduct] = HERO_PRODUCTS, *, seed: int = 0) -> None:
        self.products: dict[str, DemoProduct] = {p.item_id: p for p in products}
        self.by_category: dict[str, list[DemoProduct]] = defaultdict(list)
        for p in products:
            self.by_category[p.category].append(p)
        self.seed = seed
        self._bought: dict[str, list[tuple[datetime, str]]] = defaultdict(list)

    def usual(self, customer_id: str, category: str) -> str | None:
        """The customer's usual product in a category (None for a category with no products)."""
        items = self.by_category.get(category)
        if not items:
            return None
        u = _unit(self.seed, "usual", customer_id, category) * sum(p.weight for p in items)
        for p in items:
            u -= p.weight
            if u < 0:
                return p.item_id
        return items[-1].item_id

    def label(self, events: Sequence[Event]) -> list[Event]:
        """The events with `item_id` set on purchases and visits; everything else unchanged.

        Purchases are labelled first, in time order, so a visit sees what the customer bought before it.
        """
        out = list(events)
        purchases = [
            i
            for i, e in enumerate(out)
            if e.type is EventType.PURCHASE and e.item_id is None and e.category in self.by_category
        ]
        for i in sorted(purchases, key=lambda i: (out[i].ts, out[i].event_id)):
            e = out[i]
            item = self._purchase_item(e)
            if item is not None:
                out[i] = e.model_copy(update={"item_id": item})
                self._bought[e.customer_id].append((e.ts, item))
        for bought in self._bought.values():
            bought.sort()
        for i, e in enumerate(out):
            if e.type is EventType.VISIT and e.item_id is None:
                item = self._visit_item(e)
                out[i] = e.model_copy(update={"item_id": item, "category": self.products[item].category})
        return out

    def _purchase_item(self, e: Event) -> str | None:
        usual = self.usual(e.customer_id, str(e.category))
        if usual is None:
            return None
        others = self.products[usual].goes_with
        if not others or _unit(self.seed, "item", e.event_id) < USUAL_SHARE:
            return usual
        return others[int(_unit(self.seed, "other", e.event_id) * len(others))]

    def _visit_item(self, e: Event) -> str:
        before = [item for ts, item in self._bought.get(e.customer_id, ()) if ts <= e.ts]
        if before:
            last = before[-1]
            others = self.products[last].goes_with
            if not others or _unit(self.seed, "look", e.event_id) < VISIT_OWN_SHARE:
                return last
            return others[int(_unit(self.seed, "look-at", e.event_id) * len(others))]
        every = sorted(self.products)
        return every[int(_unit(self.seed, "browse", e.event_id) * len(every))]


@dataclass
class MandateSetup:
    """The demo's signed mandates, oldest first, with the keys, signature policy and ledger.

    One version covers the goals that start together; a goal that starts later brings a new version
    (more goals, its templates, a bigger ceiling), signed again when it starts, and the old version is
    recorded as superseded. `at(now)` is the version in force: the newest one signed by then."""

    keys: KeyRing
    policy: SignaturePolicy
    ledger: MandateLedger
    versions: list[Mandate] = field(default_factory=list)

    @staticmethod
    def signed_at(mandate: Mandate) -> datetime:
        return max(s.signed_at for s in mandate.signers)

    def at(self, now: datetime) -> Mandate | None:
        found = [m for m in self.versions if self.signed_at(m) <= now]
        return found[-1] if found else None

    @property
    def mandate(self) -> Mandate:
        """The newest version."""
        return self.versions[-1]

    def authorised(self, mandate: Mandate, now: datetime) -> bool:
        """Properly signed, active and inside its validity window at `now`."""
        return (
            not verify_mandate(mandate, self.policy, self.keys)
            and mandate.effective_status(now) is MandateStatus.ACTIVE
            and mandate.is_valid_at(now)
        )


def _secret(person: str) -> bytes:
    # Demo keys: HMAC (the light v2 install has no `cryptography`), one secret per person. A real
    # deployment keeps each signer's key on their own device; see docs/v2/MANDATE.md.
    return hashlib.sha256(f"demo-mandate-key:{person}".encode()).digest()


def _signer(person: str) -> HmacSigner:
    return HmacSigner(f"key-{person}", _secret(person))


def demo_key_ring() -> KeyRing:
    """The verifier's keys for `MANDATE_SIGNERS`: each key bound to its one person and role."""
    keys = KeyRing()
    for person, role in MANDATE_SIGNERS:
        keys.add_hmac(f"key-{person}", _secret(person), person_id=person, roles=(role,))
    return keys


def _mandate_draft(
    goal_ids: Sequence[str], budget: float, at: datetime, version: int, channels: Sequence[Channel]
) -> Mandate:
    """The limits of one mandate version, before its templates are known and before anyone signs."""
    return Mandate(
        mandate_id=stable_id("mandate", BUSINESS_ID),
        business_id=BUSINESS_ID,
        goal_ids=tuple(goal_ids),
        version=version,
        budget_ceiling=budget,
        currency="USD",
        max_daily_spend=min(MANDATE_DAILY_SPEND, budget),
        margin_floor_pct=MANDATE_MARGIN_FLOOR,
        quiet_hours=QuietHours(start=21, end=9, timezone="UTC"),
        frequency_cap=FrequencyCap(max_touches=2, window_days=7),
        channels=tuple(channels),
        approved_offer_ids=tuple(sorted(o.offer_id for o in GYM_OFFERS if o.offer_id != "none")),
        cohort_exclusions=(AudienceFilter(field="open_complaint", op="eq", value=True),),
        valid_from=at,
        valid_until=at + MANDATE_VALID,
        created_by=APPROVER,
        note="The demo shop's limits on autonomous marketing: what the agent may never do.",
    )


def certify_mandate(
    setup: MandateSetup, draft: Mandate, template_ids: Iterable[str], at: datetime
) -> Mandate:
    """Add the approved templates, collect both signatures, record every step in the ledger.

    Raises if the result is not authorised: the demo must never run on a mandate that is not."""
    mandate = draft.model_copy(
        update={
            "approved_template_ids": tuple(sorted(set(template_ids))),
            "status": MandateStatus.PENDING_SIGNATURES,
        }
    )
    if setup.versions:  # the ledger takes versions in order, so the old one is closed first
        setup.ledger.append("superseded", setup.versions[-1], actor=APPROVER, at=at)
    setup.ledger.append("created", mandate, actor=APPROVER, at=at)
    for person, role in MANDATE_SIGNERS:
        sig = sign_mandate(mandate, _signer(person), person_id=person, role=role, now=at)
        mandate = with_signature(mandate, sig, policy=setup.policy, keys=setup.keys)
        setup.ledger.append("signed", mandate, actor=person, at=at)
    problems = verify_mandate(mandate, setup.policy, setup.keys)
    if problems or mandate.status is not MandateStatus.ACTIVE:
        raise RuntimeError(f"the demo mandate is not authorised: {problems}")
    setup.ledger.append("activated", mandate, actor=APPROVER, at=at)
    setup.versions.append(mandate)
    return mandate


@dataclass(frozen=True)
class MandateCheck:
    """One decision's check against the mandate in force when it was made (`codes` empty = passed).

    A decision with codes was held: it is logged as "do nothing" with the codes in `blocked_by`."""

    goal_id: str
    customer_id: str
    group: str
    made_at: datetime
    content_id: str | None
    mandate_version: int | None
    codes: tuple[str, ...]


@dataclass(frozen=True)
class OptionView:
    """One option the agent weighed (from its `explain_bids`), kept for the walk-through."""

    content_id: str | None
    channel: Channel
    offer_id: str
    format: Format
    item_id: str | None
    expected_net: float
    net: float
    """Expected net minus the attention price: above 0 beats doing nothing on average."""
    blocked_by: str | None
    considered: bool


@dataclass(frozen=True)
class HeroNote:
    """What the hero layer knew and did for one decision of one goal (the decision ids are given by
    the runtime after deciding, so notes are found by goal, customer and moment: `HeroLayer.note`)."""

    goal_id: str
    customer_id: str
    made_at: datetime
    refill_scores: Mapping[str, float]
    refill_item: str | None
    complement: tuple[str, str] | None
    item_id: str | None
    item_kind: Literal["refill", "complement"] | None
    item_reason: str | None
    asset_id: str | None
    allowed_offers: tuple[str, ...]
    mandate_codes: tuple[str, ...]
    held: bool
    """The policy chose an action the mandate refused, so the decision became "do nothing"."""
    bought: tuple[tuple[datetime, str, float], ...]
    """(time, item, value) of every purchase up to the decision."""
    looked_at: tuple[tuple[datetime, str], ...]
    group: str
    segment: str | None
    avg_order_value: float
    contacts_7d: int
    channels: tuple[str, ...]
    """The channels the customer could be reached on."""
    options: tuple[OptionView, ...] = ()
    """Every option the agent weighed, when the run asked for them (`explain=True`)."""


def _days(x: float) -> str:
    n = round(x)
    return f"{n} day" if n == 1 else f"{n} days"


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


class HeroLayer:
    """Products, refill scores, recommendations, approved assets and the mandate, for the hero run.

    * `ingest(events)` labels Gym events with products (`ProductLayer`) and keeps each customer's
      purchases and product visits, so enrichment never has to scan the event store.
    * `enrich(states, events_by_customer, now)` is the runtime's `state_enricher` hook: it refits the
      refill `CycleModel` and the `CoPurchaseModel` weekly and puts the refill scores on each state
      (`autonomy.catalog.enrich_state`). `refreshed(state, now)` does one state, from a cache.
    * `item_lookup` / `recommender` / `asset_of` are what the agent's bidders read (customer -> refill
      pick or complement, content -> approved asset).
    * `gate(...)` and `finish(...)` keep every decision inside the mandate (see `HeroPolicy`).

    Pure bookkeeping and maths, no LLM, no clock: `now` is always passed in.
    """

    def __init__(
        self,
        *,
        products: ProductLayer,
        history: Sequence[Event],
        assets: Sequence[Asset],
        mandates: MandateSetup,
        offers: Sequence[OfferOption],
        costs: Costs,
        kinds: Mapping[str, GoalKind],
    ) -> None:
        self.products = products
        self.assets = list(assets)
        self.mandates = mandates
        self.offers = {o.offer_id: o for o in offers}
        self.costs = costs
        self.kinds = dict(kinds)
        self.clock: datetime | None = None
        """The moment the demo is deciding at, for the state-store fallback (`_EnrichingStateStore`)."""
        self.explain = False
        """Record every option the agent weighs for the refill goal's agent-group decisions (for the
        walk-through). It prices each such decision's options a second time, so it is off by default."""
        self._purchases: dict[str, list[Event]] = defaultdict(list)
        self._visits: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
        self._record(history)
        records = [
            {"item_id": p.item_id, "name": p.name, "category": p.category, "replenishable": p.replenishable}
            for p in products.products.values()
        ]
        everything = [e for evs in self._purchases.values() for e in evs]
        self.catalogue = Catalogue.from_records(records, purchases=everything)
        self.names = {p.item_id: p.name for p in self.catalogue}
        self.cycles: CycleModel | None = None
        self.co: CoPurchaseModel | None = None
        self.fitted_at: datetime | None = None
        self.picks: dict[str, RefillPick | None] = {}
        self._cache: dict[
            str, tuple[tuple[datetime, int, datetime | None], dict[str, float], RefillPick | None]
        ] = {}
        self._complements: dict[str, tuple[tuple[int, int, datetime | None], tuple[str, str] | None]] = {}
        self._seen_at: dict[str, datetime] = {}
        self._by_url = {a.url: a for a in sorted(self.assets, key=lambda a: a.asset_id) if a.is_approved}
        self._gated: dict[
            tuple[int, int | None, frozenset[str]], tuple[tuple[ContentItem, ...], tuple[ContentItem, ...]]
        ] = {}
        self._content_by_id: tuple[object, dict[str, ContentItem]] = ((), {})
        self._scheduled: dict[str, list[datetime]] = defaultdict(list)
        self._committed = 0.0
        self._committed_on: dict[date, float] = defaultdict(float)
        self._authorised: dict[int, bool] = {}
        self.item_of_customer: dict[str, str] = {}
        self.notes: dict[tuple[str, str, datetime], HeroNote] = {}
        self.checks: list[MandateCheck] = []

    # --- events ---------------------------------------------------------------------------------

    def ingest(self, events: Sequence[Event]) -> list[Event]:
        """Label the events with products and remember the purchases and visits; returns the labelled events."""
        labelled = self.products.label(events)
        self._record(labelled)
        return labelled

    def _record(self, events: Iterable[Event]) -> None:
        touched: set[str] = set()
        for e in events:
            if e.type is EventType.PURCHASE and e.item_id is not None:
                self._purchases[e.customer_id].append(e)
                touched.add(e.customer_id)
            elif e.type is EventType.VISIT and e.item_id is not None:
                self._visits[e.customer_id].append((e.ts, e.item_id))
        for cid in touched:
            self._purchases[cid].sort(key=lambda e: (e.ts, e.event_id))

    def bought(self, customer_id: str, now: datetime) -> list[Event]:
        return [e for e in self._purchases.get(customer_id, ()) if e.ts <= now]

    def looked_at(self, customer_id: str, now: datetime) -> list[tuple[datetime, str]]:
        return sorted(v for v in self._visits.get(customer_id, ()) if v[0] <= now)

    # --- models and state -----------------------------------------------------------------------

    def _refit(self, now: datetime) -> None:
        if self.fitted_at is not None and now - self.fitted_at < REFIT_EVERY:
            return
        known = [e for evs in self._purchases.values() for e in evs if e.ts <= now]
        self.cycles = CycleModel.fit(known, now, level="item", catalogue=self.catalogue)
        self.co = CoPurchaseModel.fit(known, as_of=now, names=self.names)
        self.fitted_at = now

    def enrich(
        self,
        states: Sequence[CustomerState],
        events_by_customer: Mapping[str, Sequence[Event]],  # noqa: ARG002
        now: datetime,
    ) -> list[CustomerState]:
        """The runtime's `state_enricher` hook: every state with its refill scores at `now`.

        The demo saw every purchase on its way into the event store (`ingest`), so it reads its own
        copy rather than `events_by_customer`; both hold the same purchases."""
        return [self.refreshed(s, now) for s in states]

    def refreshed(self, state: CustomerState, now: datetime) -> CustomerState:
        """One state with the refill scores at `now` (old `refill_` keys replaced). Cached per moment."""
        self._refit(now)
        cid = state.customer_id
        bought = self.bought(cid, now)
        key = (now, len(bought), self.fitted_at)
        cached = self._cache.get(cid)
        if cached is None or cached[0] != key:
            refill: dict[str, float] = {}
            pick: RefillPick | None = None
            if any(e.item_id is not None and self.catalogue.is_replenishable(e.item_id) for e in bought):
                enriched, pick = enrich_state(state, bought, self.catalogue, now, model=self.cycles)
                refill = {k: v for k, v in enriched.scores.items() if k in REFILL_KEYS}
            cached = (key, refill, pick)
            self._cache[cid] = cached
        self.picks[cid] = cached[2]
        self._seen_at[cid] = now
        scores = {k: v for k, v in state.scores.items() if k not in REFILL_KEYS} | cached[1]
        return state if scores == state.scores else state.model_copy(update={"scores": scores})

    # --- what the bidders read ------------------------------------------------------------------

    def refill_why(self, pick: RefillPick) -> str:
        est = pick.estimate
        due = est.days_until_due
        if due < -0.5:
            when = f"refill about {_days(-due)} overdue"
        elif due <= 0.5:
            when = "refill due about now"
        else:
            when = f"refill due in about {_days(due)}"
        return f"{when}; {est.p14:.0%} chance they reorder it within 14 days"

    def item_lookup(self, customer_id: str) -> tuple[str, str] | None:
        """The customer's refill pick (their most pressing replenishable product) and why."""
        pick = self.picks.get(customer_id)
        if pick is None:
            return None
        return pick.item_id, self.refill_why(pick)

    def recommender(self, customer_id: str) -> tuple[str, str] | None:
        """A complementary product and why: of the recommender's top picks, the one they looked at most."""
        co = self.co
        now = self._seen_at.get(customer_id, self.fitted_at)
        if co is None or now is None:
            return None
        bought = self.bought(customer_id, now)
        looks = self.looked_at(customer_id, now)
        key = (len(bought), len(looks), self.fitted_at)
        cached = self._complements.get(customer_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        recs = co.recommend(bought, k=COMPLEMENT_CANDIDATES, now=now)
        found: tuple[str, str] | None = None
        if recs:
            views = Counter(item for _, item in looks)
            best = max(enumerate(recs), key=lambda r: (views[r[1].item_id], -r[0]))[1]
            reason = best.reason
            if views[best.item_id]:
                reason += f", and they looked at it {_times(views[best.item_id])}"
            found = (best.item_id, reason)
        self._complements[customer_id] = (key, found)
        return found

    def asset_of(self, item: ContentItem) -> Asset | None:
        """The approved asset behind a content item's media (its first URL), or None."""
        for url in (item.asset_url or "").split():
            asset = self._by_url.get(url)
            if asset is not None:
                return asset
        return None

    def agent_kwargs(self, make: Callable[..., Any]) -> dict[str, Any]:
        """What the agent factory accepts of products, assets and the mandate (by its signature)."""
        try:
            params = inspect.signature(make).parameters
        except (TypeError, ValueError):
            return {}
        offered: dict[str, Any] = {
            "item_lookup": self.item_lookup,
            "recommender": self.recommender,
            "asset_of": self.asset_of,
            "assets": tuple(self.assets),
        }
        return {k: v for k, v in offered.items() if k in params}

    def profile(self, customer_id: str) -> dict[str, str]:
        """Slot values for the dispatcher: the product the customer's latest message is about."""
        item = self.item_of_customer.get(customer_id)
        return {"recommended_product": self.names.get(item, item)} if item is not None else {}

    # --- the mandate ----------------------------------------------------------------------------

    def _usable(self, mandate: Mandate | None, now: datetime) -> bool:
        if mandate is None:
            return False
        ok = self._authorised.get(mandate.version)
        if ok is None:
            ok = not verify_mandate(mandate, self.mandates.policy, self.mandates.keys)
            self._authorised[mandate.version] = ok
        return ok and mandate.effective_status(now) is MandateStatus.ACTIVE and mandate.is_valid_at(now)

    def action_costs(
        self, state: CustomerState, offer_id: str, channel: Channel, expected_offer_cost: float | None = None
    ) -> ActionCosts:
        """What an action costs and what the order it hopes for earns, for the mandate check.

        Revenue is the customer's average order; the margin is that at their own margin rate (the
        shop's when they have no revenue yet); the margin floor is checked with the offer's full
        cost per redemption, because an order that converts redeems it."""
        revenue = state.avg_order_value if state.purchases and state.revenue > 0 else None
        rate = state.margin / state.revenue if state.revenue > 0 else self.costs.margin_rate
        offer = self.offers.get(offer_id)
        per = offer.cost_per_redemption if offer is not None else 0.0
        return ActionCosts(
            offer_cost=per if expected_offer_cost is None else expected_offer_cost,
            channel_cost=float(self.costs.channel.get(channel, 0.0)),
            revenue_if_converted=revenue,
            margin_if_converted=revenue * rate if revenue is not None else None,
            offer_cost_if_converted=per,
        )

    def allowed_offers(self, mandate: Mandate, state: CustomerState, now: datetime) -> frozenset[str]:
        """The offers the mandate allows for this customer: approved, and keeping the margin floor."""
        from autonomy.decide.audience import passes

        if any(passes(state, f, now) for f in mandate.cohort_exclusions):
            return frozenset()
        allowed = {"none"}
        for offer_id in mandate.approved_offer_ids:
            costs = self.action_costs(state, offer_id, mandate.channels[0])
            revenue, margin = costs.revenue_if_converted, costs.margin_if_converted
            keeps_floor = (
                revenue is not None
                and revenue > 0
                and margin is not None
                and (margin - costs.conversion_offer_cost) / revenue >= mandate.margin_floor_pct - 1e-9
            )
            if keeps_floor:
                allowed.add(offer_id)
        return frozenset(allowed)

    def gate(
        self, content: tuple[ContentItem, ...], state: CustomerState, now: datetime
    ) -> tuple[tuple[ContentItem, ...], frozenset[str]]:
        """The approved content this customer may get under the mandate in force, and its offers.

        Content outside the mandate (a template it does not list, a channel it does not allow, an
        offer that would break its margin floor for this customer) is removed before the auction,
        so the policy never considers it and its logged probabilities stay exact. A customer in an
        excluded cohort, or a moment with no authorised mandate, gets no content at all."""
        mandate = self.mandates.at(now)
        if mandate is None or not self._usable(mandate, now):
            return (), frozenset()
        offers = self.allowed_offers(mandate, state, now)
        if not offers:
            return (), offers
        key = (id(content), mandate.version, offers)
        cached = self._gated.get(key)
        if cached is not None and cached[0] is content:
            return cached[1], offers
        templates = set(mandate.approved_template_ids)
        channels = set(mandate.channels)
        kept = tuple(
            c for c in content if c.content_id in templates and c.channel in channels and c.offer_id in offers
        )
        if len(self._gated) > 256:
            self._gated.clear()
        self._gated[key] = (content, kept)
        return kept, offers

    def _pending(self, customer_id: str, at: datetime, now: datetime, window: timedelta) -> int:
        """Sends this layer passed that are still to go out (after `now`) and fall in the window to `at`."""
        return sum(1 for s in self._scheduled.get(customer_id, ()) if s > now and at - window < s <= at)

    def _content(self, content: tuple[ContentItem, ...], content_id: str | None) -> ContentItem | None:
        if self._content_by_id[0] is not content:
            self._content_by_id = (content, {c.content_id: c for c in content})
        return self._content_by_id[1].get(content_id or "")

    # --- after the policy has decided -----------------------------------------------------------

    def finish(
        self,
        decision: Decision,
        state: CustomerState,
        ctx: DecisionContext,
        goal_id: str,
        offers: frozenset[str],
        *,
        fills_items: bool,
        options: tuple[OptionView, ...] = (),
    ) -> Decision:
        """Name the product and the asset, check the mandate, explain, and note it all down.

        `fills_items` says the agent already puts products on its actions; otherwise this names the
        product the way the agent's bidder does: a refill goal's actions are about the refill pick,
        and another goal's only when the content names a product (the complement, else the pick)."""
        now = ctx.now
        cid = state.customer_id
        pick = self.picks.get(cid)
        complement = self.recommender(cid)
        mandate = self.mandates.at(now)
        codes: tuple[str, ...] = ()
        held = False
        kind: Literal["refill", "complement"] | None = None
        why: str | None = None
        action = decision.action
        if decision.kind is DecisionKind.ACT and action is not None:
            item = self._content(ctx.content, action.content_id)
            update: dict[str, object] = {}
            # Content that names a product always gets one, even when the agent fills products itself:
            # its baseline rule does not, and the send-time slot must never name a stale product.
            names_one = item is not None and needs_item(item)
            if action.item_id is None and (not fills_items or names_one):
                chosen = self._item_for(goal_id, item, pick, complement)
                if chosen is not None:
                    update["item_id"] = chosen
            if action.asset_id is None and item is not None and item.format is not Format.TEXT:
                asset = self.asset_of(item)
                if asset is not None:
                    update["asset_id"] = asset.asset_id
            if update:
                action = action.model_copy(update=update)
            codes = self._check(mandate, action, decision, state, now)
            if codes:
                held = True
                decision = decision.model_copy(
                    update={
                        "kind": DecisionKind.NOTHING,
                        "action": None,
                        "expected_net": 0.0,
                        "blocked_by": tuple(dict.fromkeys((*decision.blocked_by, *codes))),
                        "reason": "Left alone: the message chosen would break the signed mandate ("
                        + ", ".join(c.removeprefix("mandate_").replace("_", " ") for c in codes)
                        + ").",
                    }
                )
            else:
                if action.item_id is not None:
                    kind, why = self._item_reason(action.item_id, pick, complement)
                    self.item_of_customer[cid] = action.item_id
                    name = self.names.get(action.item_id, action.item_id)
                    reason = decision.reason
                    if name not in reason:
                        reason = f"{reason.rstrip('.')}. It is about their {name}: {why}."
                    decision = decision.model_copy(update={"action": action, "reason": reason})
                else:
                    decision = decision.model_copy(update={"action": action})
        final = decision.action
        self.checks.append(
            MandateCheck(
                goal_id=goal_id,
                customer_id=cid,
                group=decision.group.value,
                made_at=now,
                content_id=final.content_id if final is not None else None,
                mandate_version=mandate.version if mandate is not None else None,
                codes=codes,
            )
        )
        self.notes[(goal_id, cid, now)] = HeroNote(
            goal_id=goal_id,
            customer_id=cid,
            made_at=now,
            refill_scores={k: v for k, v in state.scores.items() if k in REFILL_KEYS},
            refill_item=pick.item_id if pick is not None else None,
            complement=complement,
            item_id=final.item_id if final is not None else None,
            item_kind=kind,
            item_reason=why,
            asset_id=final.asset_id if final is not None else None,
            allowed_offers=tuple(sorted(offers)),
            mandate_codes=codes,
            held=held,
            bought=tuple((e.ts, e.item_id or "", float(e.value or 0.0)) for e in self.bought(cid, now)),
            looked_at=tuple(self.looked_at(cid, now)),
            group=state.group.value,
            segment=state.segment,
            avg_order_value=state.avg_order_value if state.purchases else 0.0,
            contacts_7d=state.contacts_7d,
            channels=tuple(c.value for c in state.contactable_channels()),
            options=options,
        )
        return decision

    def _item_for(
        self,
        goal_id: str,
        item: ContentItem | None,
        pick: RefillPick | None,
        complement: tuple[str, str] | None,
    ) -> str | None:
        if self.kinds.get(goal_id) is GoalKind.REPEAT_PURCHASE:
            return pick.item_id if pick is not None else None
        if item is not None and needs_item(item):
            if complement is not None:
                return complement[0]
            return pick.item_id if pick is not None else None
        return None

    def _item_reason(
        self, item_id: str, pick: RefillPick | None, complement: tuple[str, str] | None
    ) -> tuple[Literal["refill", "complement"], str]:
        if pick is not None and pick.item_id == item_id:
            return "refill", self.refill_why(pick)
        if complement is not None and complement[0] == item_id:
            return "complement", complement[1][:1].lower() + complement[1][1:]
        return "complement", "a product they may like"

    def _check(
        self, mandate: Mandate | None, action: Action, decision: Decision, state: CustomerState, now: datetime
    ) -> tuple[str, ...]:
        """Every way the action breaks the mandate in force (none: it passed and is committed)."""
        if mandate is None:
            return ("mandate_not_active",)
        bid = next(
            (
                b
                for b in decision.bids
                if (b.action.channel, b.action.offer_id, b.action.content_id)
                == (action.channel, action.offer_id, action.content_id)
            ),
            None,
        )
        costs = self.action_costs(state, action.offer_id, action.channel, bid.offer_cost if bid else None)
        day = action.send_at.astimezone(UTC).date()
        spend = SpendToDate(total=self._committed, today=self._committed_on.get(day, 0.0))
        window = timedelta(days=mandate.frequency_cap.window_days)
        pending = self._pending(state.customer_id, action.send_at, now, window)
        codes = tuple(check_action(mandate, action, costs, state, now, spend=spend, pending_touches=pending))
        if not codes:
            self._committed += costs.spend
            self._committed_on[day] += costs.spend
            sends = [s for s in self._scheduled[state.customer_id] if s > now - window]
            self._scheduled[state.customer_id] = [*sends, action.send_at]
        return codes

    def note(self, decision: Decision) -> HeroNote | None:
        """The note for a logged decision.

        An ACT decision names its goal, and a decision with bids names it on its bids (each goal has
        its own policy). Otherwise the note is found by customer and moment, which is exact unless two
        goals decided for the customer at that moment: then the first goal by id is returned."""
        cid, at = decision.customer_id, decision.made_at
        if decision.action is not None:
            return self.notes.get((decision.action.goal_id, cid, at))
        if decision.bids:
            return self.notes.get((decision.bids[0].goal_id, cid, at))
        for goal_id in sorted(self.kinds):
            found = self.notes.get((goal_id, cid, at))
            if found is not None:
                return found
        return None


class HeroPolicy:
    """A goal's policy inside the hero run: enrich, keep to the mandate, decide, explain.

    1. The state gets its refill scores at `now` (a no-op when the runtime's hook already did it).
    2. The content the mandate rules out for this customer is removed from the context, so the
       policy only ever weighs options the mandate allows (`HeroLayer.gate`). Removing options before
       the auction is a hard rule, so the probability the policy logs is still exact.
    3. The policy decides (the agent's own auction: refill reminder, education, complementary
       product, free shipping, ... or nothing, by expected incremental profit).
    4. The action gets its product and asset, is checked with `check_action` against the mandate in
       force, and its reason says what the product is and why (`HeroLayer.finish`). An action that
       still fails the check (it should not: the envelope and the gate cover the same limits) becomes
       "do nothing" with the mandate's codes in `blocked_by`, and is counted in `HeroNote.held`.

    Everything else (`learn`, `nightly`, `version`, ...) is the wrapped policy's own.
    """

    def __init__(self, inner: Policy, hero: HeroLayer, goal_id: str, *, fills_items: bool) -> None:
        self.inner = inner
        self.hero = hero
        self.goal_id = goal_id
        self.fills_items = fills_items
        self.version: str = inner.version

    def decide(self, state: CustomerState, ctx: DecisionContext, *, trigger: str | None = None) -> Decision:
        state = self.hero.refreshed(state, ctx.now)
        content, offers = self.hero.gate(ctx.content, state, ctx.now)
        gated = ctx if content is ctx.content else ctx.model_copy(update={"content": content})
        options = self._options(state, gated, trigger)
        if trigger is None:
            decision = self.inner.decide(state, gated)
        else:
            decision = cast(Any, self.inner).decide(state, gated, trigger=trigger)
        return self.hero.finish(
            decision, state, gated, self.goal_id, offers, fills_items=self.fills_items, options=options
        )

    def _options(
        self, state: CustomerState, ctx: DecisionContext, trigger: str | None
    ) -> tuple[OptionView, ...]:
        """Every option the agent would weigh, asked before deciding (asking changes nothing)."""
        explain = getattr(self.inner, "explain_bids", None)
        wanted = (
            self.hero.explain
            and state.group is Group.AGENT
            and self.hero.kinds.get(self.goal_id) is GoalKind.REPEAT_PURCHASE
            and callable(explain)
        )
        if not wanted or explain is None:
            return ()
        found = explain(state, ctx) if trigger is None else explain(state, ctx, trigger=trigger)
        out: list[OptionView] = []
        for e in found:
            bid = getattr(e, "bid", None)
            if not isinstance(bid, Bid):
                continue
            out.append(
                OptionView(
                    content_id=bid.action.content_id,
                    channel=bid.action.channel,
                    offer_id=bid.action.offer_id,
                    format=bid.action.format,
                    item_id=bid.action.item_id,
                    expected_net=bid.expected_net,
                    net=float(getattr(e, "net", bid.expected_net)),
                    blocked_by=getattr(e, "blocked_by", None),
                    considered=bool(getattr(e, "considered", False)),
                )
            )
        return tuple(out)

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        self.inner.learn(rewards, decisions)

    def __getattr__(self, name: str) -> Any:
        if name == "inner":  # not set yet (copying): never recurse
            raise AttributeError(name)
        return getattr(self.inner, name)


class _EnrichingStateStore:
    """The runtime's state store when the runtime has no `state_enricher` hook: every state the sweep
    scans carries its refill scores as of the moment the demo is deciding at (`HeroLayer.clock`), so
    the refill goal's audience (written on those scores) is picked from current numbers."""

    def __init__(self, inner: StateStore, hero: HeroLayer) -> None:
        self.inner = inner
        self.hero = hero

    def get(self, customer_id: str) -> CustomerState | None:
        # Single reads are for folding events and for deciding; `HeroPolicy` enriches what it decides
        # on, so only the sweep's scan (which picks each goal's audience) needs the scores here.
        return self.inner.get(customer_id)

    def put(self, states: Iterable[CustomerState]) -> None:
        self.inner.put(states)

    def scan(self) -> Iterator[CustomerState]:
        clock = self.hero.clock
        for state in self.inner.scan():
            yield self.hero.refreshed(state, clock) if clock is not None else state

    def count(self) -> int:
        return self.inner.count()


def _runtime_hooks(hero: HeroLayer) -> dict[str, Any]:
    """The hero's hooks the runtime accepts, by its signature (another stream is adding them)."""
    try:
        params = inspect.signature(AutonomyRuntime).parameters
    except (TypeError, ValueError):
        return {}
    out: dict[str, Any] = {}
    # Not passed on purpose: the runtime's `state_enricher` runs on each goal's audience *after* it is
    # chosen, but the refill goal's audience is chosen *by* the refill scores. Enriching every state
    # as it is read (`_EnrichingStateStore`) lets the audience see them. `hero.enrich` stays for
    # runtimes whose goals do not select on enriched fields.
    _ = params, hero
    return out


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
    hero: HeroLayer | None = None

    def tick(self, now: datetime) -> list[TickReport]:
        if self.hero is not None:
            self.hero.clock = now  # what the state-store fallback enriches to
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
    hero: HeroLayer | None = None

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
    stores: DemoStores,
    start: datetime,
    customers: int,
    goal_starts: Mapping[GoalKind, datetime] | None = None,
    *,
    hero: bool = False,
) -> tuple[list[Goal], list[Brief], Strategist, Analyst, MandateSetup | None]:
    """Goal -> Strategist brief -> approval -> Creative library -> Critic -> active, for each demo goal.

    A goal in `goal_starts` is planned, approved and stamped at that later time (the marketer added it
    part-way through), and the runtime leaves it alone until then because of its `starts_at`.

    With `hero`: the goals are `HERO_GOALS`; each brief's envelope is `mandate_to_envelope` of the
    mandate's limits over the Strategist's envelope (never looser than either); the refill goal's
    audience is `REFILL_AUDIENCE`; the Creative and the Critic get the approved `hero_assets`, so
    image and carousel variants exist, plus a no-offer complementary-product ("personal") cell. The
    goals that start together are then covered by one mandate version listing their approved
    templates, signed by two people at that moment (`certify_mandate`)."""
    llm = demo_llm()
    orchestrator = Orchestrator(stores.workspace)
    strategist = Strategist(llm)
    creative = Creative(llm)
    critic = Critic(llm, seed=0)
    channels = (Channel.EMAIL, Channel.WHATSAPP)
    facts = BusinessFacts(
        business_id=BUSINESS_ID,
        name=BUSINESS_NAME,
        channels=channels,
        offers=GYM_OFFERS,
        customer_count=customers,
    )
    specs = HERO_GOALS if hero else DEMO_GOALS
    starts = goal_starts or {}
    assets = hero_assets(start) if hero else []
    setup = (
        MandateSetup(keys=demo_key_ring(), policy=SignaturePolicy(), ledger=MandateLedger()) if hero else None
    )
    made: dict[int, tuple[Goal, Brief]] = {}
    covered: list[str] = []
    budget = 0.0
    templates: list[str] = []
    order = sorted(range(len(specs)), key=lambda i: (max(start, starts.get(specs[i].kind, start)), i))
    by_start: dict[datetime, list[int]] = defaultdict(list)
    for i in order:
        by_start[max(start, starts.get(specs[i].kind, start))].append(i)
    for at, group in by_start.items():
        covered += [stable_id("goal", BUSINESS_ID, specs[i].kind.value) for i in group]
        budget += sum(specs[i].budget for i in group)
        draft: Mandate | None = None
        if setup is not None:
            draft = _mandate_draft(covered, budget, at, len(setup.versions) + 1, channels)
        for i in group:
            spec = specs[i]
            goal = Goal(
                goal_id=stable_id("goal", BUSINESS_ID, spec.kind.value),
                business_id=BUSINESS_ID,
                text=spec.text,
                kind=spec.kind,
                budget=spec.budget,
                starts_at=at,
                created_at=at,
            )
            orchestrator.create(goal)
            brief = strategist.draft_brief(goal, business_facts=facts, now=at)
            if draft is not None:
                update: dict[str, object] = {
                    "envelope": mandate_to_envelope(draft, GYM_OFFERS, base=brief.envelope)
                }
                if spec.kind is GoalKind.REPEAT_PURCHASE:
                    update["audience"] = REFILL_AUDIENCE
                brief = brief.model_copy(update=update)
            orchestrator.attach_brief(goal, brief)
            approved = orchestrator.approve(goal, approved_by=APPROVER, now=at, autonomy=spec.autonomy)
            drafts = creative.build_library(
                approved, brand_voice=BRAND_VOICE, n_per_cell=1, now=at, assets=assets
            )
            if hero:  # a complementary-product message, with no offer
                plain = approved.model_copy(
                    update={
                        "envelope": approved.envelope.model_copy(
                            update={
                                "offers": tuple(o for o in approved.envelope.offers if o.offer_id == "none")
                            }
                        )
                    }
                )
                drafts += creative.build_library(
                    plain,
                    brand_voice=BRAND_VOICE,
                    n_per_cell=1,
                    now=at,
                    themes=(Theme.PERSONAL,),
                    assets=assets,
                )
            rules = BrandRules.from_brief(approved, brand_voice=BRAND_VOICE, assets=assets)
            reviewed = critic.review(drafts, rules)
            orchestrator.add_library(goal, reviewed)
            templates += [c.content_id for c in stores.workspace.content(goal_id=goal.goal_id)]
            made[i] = (orchestrator.activate(goal, now=at), approved)
        if setup is not None and draft is not None:
            certify_mandate(setup, draft, templates, at)
    goals = [made[i][0] for i in range(len(specs))]
    briefs = [made[i][1] for i in range(len(specs))]
    return goals, briefs, strategist, Analyst(llm), setup


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
    channel_cost: Mapping[Channel, float] | None = None,
    goal_starts: Mapping[GoalKind, datetime] | None = None,
    hero: bool = True,
    explain: bool = False,
) -> DemoSystem:
    """Build the agents' plan, the Gym world, the stores, the runtime and the worker.

    `live` hands each send to the world as soon as the worker runs it and lets the world catch up
    to `now` at each tick, for running on a wall-clock worker loop. `channel_cost` replaces the
    scenario's message costs (default `DEMO_CHANNEL_COST`); the world, the policy, the budget and the
    proof all use the same figures. `goal_starts` starts some goals later than `start`. `hero`
    (default) runs the hero example (see the module docstring); False runs the plain two goals.
    `explain` records every option the agent weighs for the refill goal (`HeroNote.options`)."""
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    stores = stores if stores is not None else DemoStores()
    folder = folder if folder is not None else ProductionFolder()
    goals, briefs, strategist, analyst, mandates = _setup_goals(
        stores, start, customers, goal_starts, hero=hero
    )
    content = [c for g in goals for c in stores.workspace.content(goal_id=g.goal_id)]
    cost = dict(channel_cost if channel_cost is not None else DEMO_CHANNEL_COST)
    spec = get_scenario(scenario, n_customers=customers, days=days, seed=seed, channel_cost=cost)
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
    from autonomy.gym.score import policy_view

    history = policy_view(world.reset(seed))  # the Gym's truth bookkeeping never reaches the agent
    history = with_first_names(history)  # the Gym's customers are ids; a shop's have first names
    kinds = {g.goal_id: g.kind for g in goals}
    layer: HeroLayer | None = None
    if mandates is not None:
        products = ProductLayer(seed=seed)
        history = products.label(history)
        layer = HeroLayer(
            products=products,
            history=history,
            assets=hero_assets(start),
            mandates=mandates,
            offers=GYM_OFFERS,
            costs=costs,
            kinds=kinds,
        )
        layer.explain = explain
    events.append(history)
    bridge = GymBridge(
        world, stores.decisions, immediate=live, relabel=layer.ingest if layer is not None else None
    )
    adapters = bridge.adapters()
    dispatcher = Dispatcher(
        adapters,
        _address,
        stores.workspace,
        events,
        stores.queue,
        profiles=layer.profile if layer is not None else None,
    )
    planned = goal_pacing(goals, start)
    horizon = max(b.horizon_days for b in briefs)

    def spend(goal_id: str, now: datetime) -> float:  # noqa: ARG001
        return events.spend[goal_id]

    def weekly_briefing(goal: Goal, brief: Brief, now: datetime) -> Briefing:  # noqa: ARG001
        proof = proof_at(stores.decisions, events, costs, now, horizon, seed=seed)
        return analyst.briefing(goal, proof, health_at(events, now, customers), {}, "weekly", now)

    hooks = _runtime_hooks(layer) if layer is not None else {}
    states: StateStore = stores.states
    if layer is not None and "state_enricher" not in hooks:
        # This runtime has no enrichment hook yet: enrich every state it reads instead.
        states = _EnrichingStateStore(stores.states, layer)
    runtime = AutonomyRuntime(
        events=events,
        states=states,
        decisions=stores.decisions,
        workspace=stores.workspace,
        folder=folder,
        policy_factory=policy_factory(kinds, costs, hero=layer),
        queue=stores.queue,
        dispatcher=dispatcher,
        batches=stores.batches,
        breakers_fn=standard_breakers(spend_fn=spend, planned_fn=planned),
        analyst=weekly_briefing,
        strategist=strategist.adjust_brief,
        spend_fn=spend,
        assign_group=group_assigner(BUSINESS_ID, envelope=envelope),
        # Gym events never arrive late, so the nightly full rebuild of state would only cost time.
        # Nothing in the demo reads the customer profile yet, so it is not built either.
        config=RuntimeConfig(
            sweep_hour=DECIDE_HOUR,
            seed=seed,
            worker="demo-runtime",
            nightly_rebuild=False,
            nightly_profile=False,
        ),
        **hooks,
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
        hero=layer,
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
        hero=layer,
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
    costs: Costs
    """The message and offer costs the run used, so whoever serves it prices spend the same way."""
    sent: int
    """Messages the dispatcher sent (SEND events it recorded)."""
    contacts: int
    """Contacts that landed in the Gym world."""
    sleeping_dogs_contacted: float
    reports: list[TickReport]
    stores: DemoStores
    hero: HeroLayer | None = None
    """The hero run's products, models, assets, mandate, checks and notes (None with `hero=False`)."""

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
        if self.hero is not None:
            products = [d for d in self.decisions if d.action is not None and d.action.item_id is not None]
            media = [d for d in self.decisions if d.action is not None and d.action.format is not Format.TEXT]
            failed = sum(1 for c in self.hero.checks if c.codes)
            lines.append(
                f"Hero: {len(products):,} contacts about a named product, {len(media):,} with an image, "
                f"{failed} held by the mandate (v{self.hero.mandates.mandate.version}, "
                f"{len(self.hero.mandates.ledger)} ledger entries)."
            )
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
    channel_cost: Mapping[Channel, float] | None = None,
    approve_goal_days: int | None = None,
    hold_last_batches: bool = False,
    hero: bool = True,
    explain: bool = False,
) -> DemoResult:
    """Run the whole system on a Gym shop for `days` days; see the module docstring.

    `approve_goal_days` starts the APPROVE goal at the daily sweep that many days before the end
    (None, or a number of at least `days`: with the other goal, at the start). `hold_last_batches`
    stops the demo approving batches on the last day, so they are still pending at the end, as they
    would be for a marketer who has not looked yet. `hero=False` runs the plain two goals without
    products, assets or a mandate; `explain` keeps every option the agent weighed for the refill goal
    (for `hero_story`; it prices those decisions twice)."""
    if days < 1:
        raise ValueError("days must be at least 1")
    if approve_goal_days is not None and approve_goal_days < 1:
        raise ValueError("approve_goal_days must be at least 1 (None starts it with the other goal)")
    # The first daily sweep at or after `start` (an earlier one would come before the goals start).
    first = start.astimezone(UTC).replace(hour=DECIDE_HOUR, minute=0, second=0, microsecond=0)
    if first < start:
        first += timedelta(days=1)
    goal_starts: dict[GoalKind, datetime] = {}
    if approve_goal_days is not None and approve_goal_days < days:
        late = first + timedelta(days=days - approve_goal_days)
        specs = HERO_GOALS if hero else DEMO_GOALS
        goal_starts = {g.kind: late for g in specs if g.autonomy is AutonomyLevel.APPROVE}
    system = build_system(
        scenario,
        days=days,
        customers=customers,
        seed=seed,
        start=start,
        stores=stores,
        folder=folder,
        channel_cost=channel_cost,
        goal_starts=goal_starts,
        hero=hero,
        explain=explain,
    )
    for day in range(days):
        # Hold the last two days: on a small shop the agent may rightly contact nobody new on the very
        # last day (weekly caps), and the app still needs a batch waiting for a person.
        system.cycle.auto_approve = not (hold_last_batches and day >= days - 2)
        system.day(first + timedelta(days=day))
    end = first + timedelta(days=days)
    system.step_to(end)
    st = system.stores
    if system.hero is not None:
        # Keep the refill scores as of the end on the stored states, for whoever serves the run next
        # (the API's runtime over these stores has no hero layer to enrich them).
        st.states.put(system.hero.enrich(list(st.states.scan()), {}, end))
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
        costs=cycle.costs,
        sent=system.events.sends,
        contacts=len(system.world.contacts),
        sleeping_dogs_contacted=system.world.sleeping_dogs_contacted_share(),
        reports=list(cycle.reports),
        stores=st,
        hero=system.hero,
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


# --- the hero walk-through -------------------------------------------------------------------------

_THEME_OPTION: dict[Theme, str] = {
    Theme.REMINDER: "refill reminder",
    Theme.EDUCATION: "how-to (education)",
    Theme.QUALITY: "why people reorder it (quality)",
    Theme.PERSONAL: "goes-well-with (complementary)",
    Theme.CONVENIENCE: "convenience",
    Theme.SAVINGS: "savings",
    Theme.URGENCY: "savings, this week (urgency)",
}
"""How the walk-through names each message theme; the offer has its own column."""


def _hero_pick(result: DemoResult, hero: HeroLayer, customer_id: str | None) -> Decision | None:
    """The decision to walk through: the refill goal's message about a serum to someone who looked at
    its complements twice or more, made after the first week (the agent has learned something) with two
    weeks left to watch, where the chosen option was the top estimate (not an exploring draw); else
    the closest there is. Which message won is not part of the choice: that is the agent's call."""
    refill_goals = {g.goal_id for g in result.goals if g.kind is GoalKind.REPEAT_PURCHASE}
    content = {c.content_id: c for c in result.content}
    candidates: list[tuple[tuple[bool, bool, bool, bool, bool, bool, datetime, str], Decision]] = []
    for d in result.decisions:
        if customer_id is not None and d.customer_id != customer_id:
            continue
        a = d.action
        if a is None or a.goal_id not in refill_goals or a.item_id is None:
            continue
        note = hero.note(d)
        if note is None or note.item_kind != "refill":
            continue
        product = hero.products.products.get(a.item_id)
        others = set(product.goes_with) if product is not None else set()
        looked = sum(1 for _, item in note.looked_at if item in others)
        item = content.get(a.content_id or "")
        settled = result.start + timedelta(days=7) <= d.made_at <= result.end - timedelta(days=14)
        chosen = next((b for b in d.bids if b.action.content_id == a.content_id), None)
        top = chosen is not None and chosen.expected_net >= max(b.expected_net for b in d.bids)
        rank = (
            a.item_id != "serum_vitc",
            looked < 2,
            not settled,
            chosen is None,
            not top,
            item is None or item.theme is not Theme.REMINDER,
            d.made_at,
            d.customer_id,
        )
        candidates.append((rank, d))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


def _window(scores: Mapping[str, float]) -> str:
    if scores.get("refill_window_open"):
        return "open"
    return "already past" if scores.get("refill_due_days", 0.0) < 0 else "not open yet"


def _money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def hero_story(result: DemoResult, customer_id: str | None = None, *, name: str = "Priya") -> str:
    """One simulated customer through the hero example, as Markdown, from a finished run.

    Everything comes from the run itself: the products she bought and looked at (the demo's product
    layer), her refill scores and next best items at the moment of the decision, the options the
    agent weighed (`Decision.bids`, the top ones by expected net), the decision and its reason, the
    mandate check, and what happened in the following two weeks. The Gym has no names, so `name` is
    what the walk-through calls her."""
    hero = result.hero
    if hero is None:
        return "This run has no hero layer (it ran with hero=False)."
    decision = _hero_pick(result, hero, customer_id)
    if decision is None or decision.action is None:
        return "No refill decision about a product in this run; try more customers or days."
    note = hero.note(decision)
    assert note is not None
    action = decision.action
    names = hero.names
    content = {c.content_id: c for c in result.content}
    goals = {g.goal_id: g for g in result.goals}
    by_id = {d.decision_id: d for d in result.decisions if d.customer_id == decision.customer_id}
    cid = decision.customer_id
    when = decision.made_at
    mandate = hero.mandates.at(when)
    lines: list[str] = []
    add = lines.append

    add(f"### {name} (`{cid}`), {when:%A %d %B %Y, %H:%M} UTC")
    add("")
    add("**What the agent knew about her**")
    add("")
    for ts, item, value in note.bought:
        add(f"- Bought {names.get(item, item)} on {ts:%d %b %Y} ({_money(value)}).")
    looks = Counter(item for _, item in note.looked_at)
    for item, n in sorted(looks.items(), key=lambda kv: (-kv[1], kv[0])):
        add(f"- Looked at {names.get(item, item)} {_times(n)}.")
    add(
        f"- {len(note.bought)} purchases, {_money(note.avg_order_value)} an order on average; segment "
        f"`{note.segment}`; group `{note.group}`; {note.contacts_7d} message"
        f"{'' if note.contacts_7d == 1 else 's'} in the last 7 days; "
        f"reachable by {', '.join(note.channels) or 'nothing'}."
    )
    s = note.refill_scores
    if note.refill_item is not None and s:
        add(
            f"- Refill pick: **{names.get(note.refill_item, note.refill_item)}**. Her typical gap for it "
            f"is about {s.get('refill_cycle_days', math.nan):.0f} days; the refill is due in "
            f"{s.get('refill_due_days', math.nan):.1f} days (negative = overdue); chance she reorders it "
            f"within 14 days {s.get('refill_p14', math.nan):.0%}; refill window "
            f"{_window(s)}; chance she is still on her "
            f"cycle {s.get('refill_still_likely', math.nan):.0%}."
        )
    if note.complement is not None:
        add(
            f"- Complementary product: **{names.get(note.complement[0], note.complement[0])}** "
            f"({note.complement[1]})."
        )
    if mandate is not None:
        ruled_out = sorted(set(mandate.approved_offer_ids) - set(note.allowed_offers))
        why = (
            f" ({', '.join(hero.offers[o].label for o in ruled_out)} would take her order below the "
            f"{mandate.margin_floor_pct:.0%} margin floor, so it was never an option)"
            if ruled_out
            else ""
        )
        add(
            f"- Mandate v{mandate.version} (signed by {', '.join(sig.person_id for sig in mandate.signers)}): "
            f"offers allowed for her: {', '.join(hero.offers[o].label for o in note.allowed_offers)}{why}."
        )
    add("")
    goal = goals.get(action.goal_id)
    add(f'**The options the agent weighed for the goal "{goal.text if goal else action.goal_id}"**')
    add("")

    def label(content_id: str | None, offer_id: str) -> tuple[str, str]:
        item = content.get(content_id or "")
        theme = _THEME_OPTION.get(item.theme, item.theme.value) if item is not None else "?"
        offer = hero.offers[offer_id].label if offer_id in hero.offers else offer_id
        return theme, offer

    if note.options:
        add(
            "Every option, as the agent priced it a moment before deciding (`explain_bids`). Net is the "
            "expected incremental profit after costs, minus the attention price; only options above "
            "doing nothing (0) and near the best are sampled."
        )
        add("")
        add("| option | channel | offer | format | expected net | net | status |")
        add("|---|---|---|---|---:|---:|---|")
        ranked = sorted(note.options, key=lambda o: (o.blocked_by is not None, -o.net))
        for o in ranked:
            theme, offer = label(o.content_id, o.offer_id)
            mark = (
                " **(chosen)**" if o.content_id == action.content_id and o.channel is action.channel else ""
            )
            status = (
                f"blocked: {o.blocked_by}" if o.blocked_by else ("sampled" if o.considered else "not sampled")
            )
            add(
                f"| {theme}{mark} | {o.channel.value} | {offer} | {o.format.value} | "
                f"{_money(o.expected_net)} | {_money(o.net)} | {status} |"
            )
        add("| do nothing | | | | $0.00 | $0.00 | the bar |")
    else:
        add("The top options the decision logged (`Decision.bids`):")
        add("")
        add("| option | channel | offer | format | product | expected net |")
        add("|---|---|---|---|---|---:|")
        for b in decision.bids:
            theme, offer = label(b.action.content_id, b.action.offer_id)
            product = names.get(b.action.item_id, "-") if b.action.item_id else "-"
            chosen = " **(chosen)**" if b.action.content_id == action.content_id else ""
            add(
                f"| {theme}{chosen} | {b.action.channel.value} | {offer} | {b.action.format.value} | "
                f"{product} | {_money(b.expected_net)} |"
            )
        if not any(b.action.content_id == action.content_id for b in decision.bids):
            theme, offer = label(action.content_id, action.offer_id)
            product = names.get(action.item_id, "-") if action.item_id else "-"
            add(
                f"| {theme} **(chosen; not among the top five logged)** | {action.channel.value} | {offer} | "
                f"{action.format.value} | {product} | {_money(decision.expected_net)} |"
            )
        add(f"| do nothing | | | | | {_money(0.0)} |")
    add("")
    add(
        f"Every option first paid the attention price, {_money(decision.attention_price)} (the learned "
        "risk that one more message costs her as a customer), and had to beat doing nothing. "
        f"Blocked along the way: {', '.join(decision.blocked_by) or 'nothing'}."
    )
    add("")
    add("**The decision**")
    add("")
    add(f"> {decision.reason}")
    add("")
    add(
        f"Chosen with probability {decision.probability:.2f} (logged for proof); sent at "
        f"{action.send_at:%H:%M} UTC; format `{action.format.value}`"
        + (f" with the approved image `{action.asset_id}`" if action.asset_id else "")
        + f"; mandate check: {'passed' if not note.mandate_codes else ', '.join(note.mandate_codes)}."
    )
    others = [
        d
        for d in result.decisions
        if d.customer_id == cid and d.made_at == when and d.decision_id != decision.decision_id
    ]
    for d in others:
        add(f'The same morning, the other goal decided: "{d.reason}"')
    add("")
    add("**What happened next (14 days)**")
    add("")
    after = sorted(
        (
            e
            for e in result.events
            if e.customer_id == cid
            and when < e.ts <= when + timedelta(days=14)
            and e.type is not EventType.DELIVER
        ),
        key=lambda e: (e.ts, e.event_id),
    )
    if not after:
        add("- Nothing: no message landed and she did not buy.")
    for e in after:
        what = e.type.value
        if e.type is EventType.PURCHASE:
            what = f"bought {names.get(e.item_id or '', e.item_id or 'something')} ({_money(float(e.value or 0.0))})"
        elif e.type is EventType.SEND:
            goal_of = goals.get(str(e.attrs.get("goal_id", "")))
            what = f"message sent by {e.channel.value if e.channel else '?'}"
            if e.decision_id == decision.decision_id:
                what += " (this decision)"
            elif goal_of is not None:
                sent_for = by_id.get(e.decision_id or "")
                what += f' for "{goal_of.text}"'
                if sent_for is not None and sent_for.action is not None and sent_for.action.item_id:
                    what += f", about {names.get(sent_for.action.item_id, sent_for.action.item_id)}"
        elif e.type is EventType.CLICK:
            what = "clicked the message"
        add(f"- {e.ts:%d %b %H:%M}: {what}.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m autonomy.runtime.demo`: run the hero demo, print its summary and one walk-through."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--scenario", default="baseline_shop")
    parser.add_argument("--customers", type=int, default=600)
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--customer", default=None, help="walk through this customer id")
    args = parser.parse_args(argv)
    result = run_demo(args.scenario, days=args.days, customers=args.customers, seed=args.seed, explain=True)
    print(result.summary())
    print()
    print(hero_story(result, args.customer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# Public name for callers outside the demo (the API keeps ticking the simulated shop after it).
EnrichingStateStore = _EnrichingStateStore
