"""What the v2 routes run on: a `Services` container, the operations behind each endpoint, and a demo.

The routes in `api.v2.routes` stay thin: they parse the request, call one `Services` method and
wrap the result. Everything that decides something (what an approval changes, which decisions a
filter keeps, how a webhook is verified and stored) lives here, so it can be tested without an
HTTP server and Wave 2 can swap the in-memory stores for Postgres without touching the routes.

`build_demo_services(now)` wires the fakes from `autonomy.fakes` (stores, a sample goal, an
approved brief, a content library) and runs a week of a simulated shop through the reference loop,
so every endpoint returns real-looking data with no external services.

Time is passed in: `Services.clock` is the only place "now" comes from, and the demo fixes it.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeAlias

from starlette.applications import Starlette
from starlette.requests import Request

from autonomy.contracts import (
    NO_OFFER,
    Alert,
    Audience,
    AudienceFilter,
    AutonomyLevel,
    Brief,
    Briefing,
    BriefingSection,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionContext,
    Envelope,
    Estimate,
    Event,
    EventType,
    FollowUpStep,
    Goal,
    GoalKind,
    GoalStatus,
    Group,
    HealthStatus,
    Objective,
    ProofSummary,
    Reward,
    Theme,
    stable_id,
)
from autonomy.fakes import (
    ContactEveryonePolicy,
    FakeChannelAdapter,
    FakeEnvironment,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryStateStore,
    InMemoryWorkspace,
    NothingPolicy,
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.interfaces import ChannelAdapter, DecisionLog, EventStore, StateFolder, StateStore, Workspace
from autonomy.loop import run_days

ProofProvider: TypeAlias = Callable[[str | None], ProofSummary | None]
"""goal_id (None: the whole programme) -> the proof so far, or None when there is none yet."""
BriefingProvider: TypeAlias = Callable[[str | None], Briefing | None]
HealthProvider: TypeAlias = Callable[[], HealthStatus]
AlertsProvider: TypeAlias = Callable[[], list[Alert]]
OpportunitiesProvider: TypeAlias = Callable[[], list[Goal]]
Strategist: TypeAlias = Callable[[Goal], Brief]
"""A goal in, an unapproved brief for it out. Wave 2 plugs the real Strategist agent in here."""
Ingest: TypeAlias = Callable[[Sequence[Event]], int]
"""Store events (and fold them into state); return how many were new."""
Clock: TypeAlias = Callable[[], datetime]

SERVICES_STATE_KEY = "v2_services"
"""Where `attach_services()` puts the container on `app.state`, and `get_services()` finds it."""

TIMELINE_LIMIT = 200
"""The most decisions, and the most events, the customer page returns (newest first)."""

APPROVER = "marketer"
"""Who `approve` records as `Brief.approved_by` until the API has real user accounts."""


# --- errors the routes turn into HTTP status codes ------------------------------------------------


class ServiceError(Exception):
    """A request the services refused. `status_code` is the HTTP status the routes answer with."""

    status_code = 400


class BadRequestError(ServiceError):
    status_code = 400


class UnauthorizedError(ServiceError):
    status_code = 401


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    status_code = 409


# --- the container --------------------------------------------------------------------------------


@dataclass
class Services:
    """Everything the v2 routes need, passed in so tests, the demo and production differ only here."""

    workspace: Workspace
    events: EventStore
    states: StateStore
    decisions: DecisionLog
    proof: ProofProvider
    briefing: BriefingProvider
    health: HealthProvider
    alerts: AlertsProvider
    opportunities: OpportunitiesProvider
    strategist: Strategist
    adapters: Mapping[Channel, ChannelAdapter]
    ingest: Ingest
    clock: Clock
    business_id: str = "demo"

    # goals and briefs ---------------------------------------------------------------------------

    def list_goals(self) -> list[Goal]:
        """Newest first, so the marketer sees what they just asked for at the top."""
        return sorted(self.workspace.goals(), key=lambda g: (g.created_at, g.goal_id), reverse=True)

    def get_goal(self, goal_id: str) -> Goal:
        goal = next((g for g in self.workspace.goals() if g.goal_id == goal_id), None)
        if goal is None:
            raise NotFoundError(f"no goal {goal_id!r}")
        return goal

    def latest_brief(self, goal_id: str) -> Brief | None:
        briefs = self.workspace.briefs(goal_id)
        return max(briefs, key=lambda b: b.version) if briefs else None

    def goal_detail(self, goal_id: str) -> tuple[Goal, Brief | None, list[ContentItem], ProofSummary | None]:
        """The goal, its latest brief, all its content (drafts too, so they can be reviewed) and proof."""
        goal = self.get_goal(goal_id)
        content = self.workspace.content(goal_id=goal_id, approved_only=False)
        return goal, self.latest_brief(goal_id), content, self.proof(goal_id)

    def create_goal(
        self,
        text: str,
        budget: float,
        currency: str = "USD",
        kind: GoalKind | None = None,
        objective: Objective | None = None,
    ) -> tuple[Goal, Brief]:
        """Store the marketer's goal as a draft and the Strategist's (unapproved) brief for it."""
        text = text.strip()
        if not text:
            raise BadRequestError("a goal needs some words")
        # Python's JSON parser reads 1e999 as infinity, and a stored infinite budget would break
        # every later response that includes the goal, so refuse it here.
        if not math.isfinite(budget) or budget < 0:
            raise BadRequestError("the budget must be a finite amount of zero or more")
        now = self.clock()
        taken = {g.goal_id for g in self.workspace.goals()}
        n = len(taken)
        goal_id = stable_id("goal", self.business_id, text, now.isoformat(), n)
        while goal_id in taken:  # two identical requests at the same instant
            n += 1
            goal_id = stable_id("goal", self.business_id, text, now.isoformat(), n)
        goal = Goal(
            goal_id=goal_id,
            business_id=self.business_id,
            text=text,
            kind=kind or infer_goal_kind(text),
            objective=objective or Objective.GROSS_PROFIT,
            budget=budget,
            currency=currency.upper(),
            starts_at=now,
            created_at=now,
        )
        brief = self.strategist(goal)
        if brief.goal_id != goal.goal_id:
            raise ValueError(f"the strategist answered goal {goal.goal_id} with a brief for {brief.goal_id}")
        self.workspace.put_goal(goal)
        self.workspace.put_brief(brief)
        return goal, brief

    def approve(self, goal_id: str, autonomy: AutonomyLevel) -> tuple[Goal, Brief]:
        """The marketer signs off the plan once and picks how much the agent may do without asking."""
        goal = self.get_goal(goal_id)
        if goal.status is GoalStatus.DONE:
            raise ConflictError("this goal is finished; start a new one instead")
        brief = self.latest_brief(goal_id)
        if brief is None:
            raise ConflictError("this goal has no plan to approve yet")
        now = self.clock()
        approved: Brief = brief.model_copy(update={"approved_by": APPROVER, "approved_at": now})
        active: Goal = goal.model_copy(update={"autonomy": autonomy, "status": GoalStatus.ACTIVE})
        self.workspace.put_brief(approved)
        self.workspace.put_goal(active)
        return active, approved

    def pause(self, goal_id: str) -> Goal:
        """Stop the agent acting for this goal. Pausing a paused goal is harmless."""
        goal = self.get_goal(goal_id)
        if goal.status is GoalStatus.DONE:
            raise ConflictError("this goal is finished; there is nothing to pause")
        paused: Goal = goal.model_copy(update={"status": GoalStatus.PAUSED})
        self.workspace.put_goal(paused)
        return paused

    def resume(self, goal_id: str) -> Goal:
        """Let the agent act again. Only an approved plan can run, so a draft has to be approved first."""
        goal = self.get_goal(goal_id)
        if goal.status is GoalStatus.DONE:
            raise ConflictError("this goal is finished; start a new one instead")
        brief = self.latest_brief(goal_id)
        if brief is None or not brief.approved:
            raise ConflictError("approve the plan before the agent can run it")
        resumed: Goal = goal.model_copy(update={"status": GoalStatus.ACTIVE})
        self.workspace.put_goal(resumed)
        return resumed

    # content ------------------------------------------------------------------------------------

    def goal_content(self, goal_id: str) -> list[ContentItem]:
        self.get_goal(goal_id)
        return self.workspace.content(goal_id=goal_id, approved_only=False)

    def set_content_status(self, content_id: str, status: ContentStatus) -> ContentItem:
        item = next(
            (c for c in self.workspace.content(approved_only=False) if c.content_id == content_id), None
        )
        if item is None:
            raise NotFoundError(f"no content item {content_id!r}")
        updated: ContentItem = item.model_copy(update={"status": status})
        self.workspace.put_content([updated])
        return updated

    # decisions and customers --------------------------------------------------------------------

    def recent_decisions(
        self, *, goal_id: str | None = None, customer_id: str | None = None, limit: int = 50
    ) -> list[Decision]:
        """Newest first. A goal filter keeps decisions that acted for the goal or weighed a bid for it."""
        # `is not None`: an empty customer_id is a customer nobody has, not "no filter". And `sorted`,
        # not `.sort()`, because a store may hand back a list it keeps.
        rows: Sequence[Decision] = (
            self.decisions.for_customer(customer_id)
            if customer_id is not None
            else list(self.decisions.scan())
        )
        if goal_id is not None:
            rows = [d for d in rows if _touches_goal(d, goal_id)]
        newest = sorted(rows, key=lambda d: (d.made_at, d.decision_id), reverse=True)
        return newest[: max(limit, 0)]

    def customer(self, customer_id: str) -> tuple[CustomerState, list[Decision], list[Event]]:
        """One customer's state, with their latest decisions and events, newest first."""
        state = self.states.get(customer_id)
        if state is None:
            raise NotFoundError(f"no customer {customer_id!r}")
        decisions = sorted(
            self.decisions.for_customer(customer_id), key=lambda d: (d.made_at, d.decision_id), reverse=True
        )
        events = sorted(self.events.for_customer(customer_id), key=lambda e: (e.ts, e.event_id), reverse=True)
        return state, decisions[:TIMELINE_LIMIT], events[:TIMELINE_LIMIT]

    # webhooks -----------------------------------------------------------------------------------

    def receive_webhook(self, channel: str, headers: Mapping[str, str], body: bytes) -> int:
        """Verify a provider callback, turn it into events and store them; return how many were new.

        Header names are lowercased before the adapter sees them (HTTP header names are
        case-insensitive, and HTTP/2 sends them lowercase anyway), so an adapter can look them up
        in lowercase whatever the provider or proxy did to them.
        """
        try:
            adapter = self.adapters.get(Channel(channel))
        except ValueError:
            adapter = None
        if adapter is None:
            raise NotFoundError(f"no webhook for channel {channel!r}")
        lowered = {k.lower(): v for k, v in headers.items()}
        if not adapter.verify_webhook(lowered, body):
            raise UnauthorizedError("the webhook signature does not match")
        try:
            events = adapter.parse_webhook(lowered, body)
        except (ValueError, KeyError, TypeError) as exc:  # bad JSON, missing fields, invalid events
            raise BadRequestError(f"could not read the {channel} webhook: {exc}") from exc
        return self.ingest(events)


def _touches_goal(decision: Decision, goal_id: str) -> bool:
    if decision.action is not None and decision.action.goal_id == goal_id:
        return True
    return any(b.goal_id == goal_id for b in decision.bids)


# --- wiring into an app ---------------------------------------------------------------------------


def attach_services(app: Starlette, services: Services) -> None:
    """Make `services` what every v2 route gets. Wave 2 calls this when it mounts the router."""
    setattr(app.state, SERVICES_STATE_KEY, services)


def get_services(request: Request) -> Services:
    """The FastAPI dependency the v2 routes declare."""
    services = getattr(request.app.state, SERVICES_STATE_KEY, None)
    if not isinstance(services, Services):
        raise RuntimeError("the v2 router is mounted without services; call attach_services(app, ...)")
    return services


def make_ingest(events: EventStore, states: StateStore, folder: StateFolder) -> Ingest:
    """Store events and fold only the new ones into state, so a retried webhook never counts twice."""

    def ingest(batch: Sequence[Event]) -> int:
        new = [e for e in batch if events.append([e])]
        cache: dict[str, CustomerState | None] = {}
        for e in sorted(new, key=lambda e: e.ts):
            if e.customer_id not in cache:
                cache[e.customer_id] = states.get(e.customer_id)
            cache[e.customer_id] = folder.fold(cache[e.customer_id], e)
        states.put(s for s in cache.values() if s is not None)
        return len(new)

    return ingest


# --- the template strategist ----------------------------------------------------------------------

_KIND_WORDS: tuple[tuple[GoalKind, tuple[str, ...]], ...] = (
    (GoalKind.CART_RECOVERY, ("cart", "checkout", "abandon")),
    (GoalKind.WIN_BACK, ("win back", "winback", "lapsed", "haven't bought", "inactive", "come back")),
    (GoalKind.ONBOARDING, ("onboard", "welcome", "new customer", "new signup", "first purchase")),
    (GoalKind.CROSS_SELL, ("cross-sell", "cross sell", "upsell", "other categor")),
    (GoalKind.REPEAT_PURCHASE, ("repeat", "second purchase", "buy again", "reorder")),
    (GoalKind.RETENTION, ("retain", "retention", "churn")),
)


def infer_goal_kind(text: str) -> GoalKind:
    """A keyword guess at the goal's kind; CUSTOM when nothing matches. The Strategist may refine it."""
    low = text.lower().replace("’", "'")
    for kind, words in _KIND_WORDS:
        if any(w in low for w in words):
            return kind
    return GoalKind.CUSTOM


_AUDIENCES: dict[GoalKind, Audience] = {
    GoalKind.WIN_BACK: Audience(
        description="Bought before, nothing in the last 60 days",
        filters=(
            AudienceFilter(field="purchases", op="gte", value=1),
            AudienceFilter(field="last_purchase_at", op="days_since_gt", value=60),
        ),
    ),
    GoalKind.REPEAT_PURCHASE: Audience(
        description="Bought exactly once",
        filters=(AudienceFilter(field="purchases", op="eq", value=1),),
    ),
    GoalKind.CART_RECOVERY: Audience(
        description="Visited in the last 3 days",
        filters=(AudienceFilter(field="last_seen_at", op="days_since_lt", value=3),),
    ),
    GoalKind.CROSS_SELL: Audience(
        description="Bought at least twice",
        filters=(AudienceFilter(field="purchases", op="gte", value=2),),
    ),
    GoalKind.ONBOARDING: Audience(
        description="Signed up in the last 30 days and not bought yet",
        filters=(
            AudienceFilter(field="purchases", op="eq", value=0),
            AudienceFilter(field="first_seen_at", op="days_since_lt", value=30),
        ),
    ),
    GoalKind.RETENTION: Audience(
        description="Regular buyers who have gone quiet for a month",
        filters=(
            AudienceFilter(field="purchases", op="gte", value=2),
            AudienceFilter(field="last_purchase_at", op="days_since_gt", value=30),
        ),
    ),
    GoalKind.CUSTOM: Audience(description="Everyone we can reach"),
}


def template_strategist(goal: Goal) -> Brief:
    """A deterministic brief from the goal's kind: a stand-in for the Strategist agent.

    It starts cautious: email and WhatsApp, no discount, two contacts a week at most and one
    follow-up. The marketer reviews it before anything runs.
    """
    horizon = 3 if goal.kind is GoalKind.CART_RECOVERY else 14
    return Brief(
        brief_id=f"brief_{goal.goal_id}_1",
        goal_id=goal.goal_id,
        objective=goal.objective,
        audience=_AUDIENCES[goal.kind],
        horizon_days=horizon,
        envelope=Envelope(
            channels=(Channel.EMAIL, Channel.WHATSAPP),
            offers=(NO_OFFER,),
            budget=goal.budget,
            max_contacts_per_week=2,
        ),
        follow_ups=(FollowUpStep(after_days=3, when="no_purchase"),),
        rationale=(
            f"A starting plan for a {goal.kind.value.replace('_', ' ')} goal: reach "
            f"{_AUDIENCES[goal.kind].description.lower()} by email or WhatsApp, without discounts "
            "until the agent learns who needs one, and follow up once if they have not bought."
        ),
        created_by="template-strategist",
        created_at=goal.created_at,
    )


# --- the demo -------------------------------------------------------------------------------------

DEMO_SECRET = "test-secret"
"""The webhook secret of the demo's fake adapters (send it as the `x-fake-signature` header)."""

SEND_COST: dict[Channel, float] = {Channel.EMAIL: 0.001, Channel.WHATSAPP: 0.01, Channel.SMS: 0.0075}
"""Rough per-message costs, so the demo's proof has a spend to set against profit."""


def _demo_group(customer_id: str) -> Group:
    """A stable 80 / 10 / 10 split: agent / baseline / holdout."""
    bucket = int(stable_id("grp", customer_id).split("_", 1)[1], 16) % 10
    if bucket == 0:
        return Group.HOLDOUT
    if bucket == 1:
        return Group.BASELINE
    return Group.AGENT


class _DemoPolicy:
    """ContactEveryonePolicy, held to the weekly cap, never touching the holdout.

    The baseline group gets the same rule with a cap of one a week, so the proof page has a
    simple rule to compare the agent with.
    """

    version = "demo-everyone-v0"

    def __init__(self) -> None:
        self._everyone = ContactEveryonePolicy()
        self._nothing = NothingPolicy()

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        cap = min((b.envelope.max_contacts_per_week for b in ctx.briefs), default=0)
        if state.group is Group.BASELINE:
            cap = min(cap, 1)
        reason: str | None = None
        if state.group is Group.HOLDOUT:
            reason = "This customer is in the holdout, which we never contact, so we can measure the effect."
        elif state.contacts_7d >= cap:
            times = "once" if state.contacts_7d == 1 else f"{state.contacts_7d} times"
            reason = f"Already contacted {times} this week; the cap is {cap}."
        if reason is None:
            decision = self._everyone.decide(state, ctx)
        else:
            decision = self._nothing.decide(state, ctx).model_copy(update={"reason": reason})
        stamped: Decision = decision.model_copy(
            update={
                "decision_id": stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version),
                "group": state.group,
                "policy_version": self.version,
            }
        )
        return stamped

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:  # noqa: ARG002
        return None


@dataclass
class _DemoReadModels:
    """Proof, briefing, health, alerts and opportunities computed from the demo's stores on request."""

    workspace: Workspace
    events: EventStore
    states: StateStore
    goal_id: str
    business_id: str
    start: datetime
    now: datetime

    def _counts(self, since: datetime) -> Counter[EventType]:
        return Counter(e.type for e in self.events.scan(since=since))

    def _covers(self, goal_id: str | None) -> bool:
        return goal_id is None or goal_id == self.goal_id

    def proof(self, goal_id: str | None) -> ProofSummary | None:
        """Agent vs holdout and vs baseline, from purchases and send costs since the demo started."""
        if not self._covers(goal_id):
            return None
        revenue: dict[str, float] = defaultdict(float)
        margin: dict[str, float] = defaultdict(float)
        cost: dict[str, float] = defaultdict(float)
        for e in self.events.scan(since=self.start):
            if e.type is EventType.PURCHASE:
                revenue[e.customer_id] += e.value or 0.0
                margin[e.customer_id] += e.margin or 0.0
            elif e.type is EventType.SEND and e.channel is not None:
                cost[e.customer_id] += SEND_COST.get(e.channel, 0.0)
        rev: dict[Group, list[float]] = defaultdict(list)
        prof: dict[Group, list[float]] = defaultdict(list)
        spend = 0.0
        for s in self.states.scan():
            rev[s.group].append(revenue[s.customer_id])
            prof[s.group].append(margin[s.customer_id] - cost[s.customer_id])
            if s.group is Group.AGENT:
                spend += cost[s.customer_id]
        n_agent = len(rev[Group.AGENT])
        inc_profit = _difference(prof[Group.AGENT], prof[Group.HOLDOUT], scale=n_agent)
        roi = None
        if inc_profit is not None and spend > 0:
            roi = Estimate(
                point=round(inc_profit.point / spend, 2),
                low=round(inc_profit.low / spend, 2),
                high=round(inc_profit.high / spend, 2),
            )
        return ProofSummary(
            goal_id=goal_id,
            as_of=self.now,
            agent_n=n_agent,
            baseline_n=len(rev[Group.BASELINE]),
            holdout_n=len(rev[Group.HOLDOUT]),
            incremental_revenue=_difference(rev[Group.AGENT], rev[Group.HOLDOUT], scale=n_agent),
            incremental_profit=inc_profit,
            lift_vs_baseline=_difference(prof[Group.AGENT], prof[Group.BASELINE], scale=1),
            spend=round(spend, 2),
            roi=roi,
            notes=("Demo data from a simulated shop, not your customers.",),
        )

    def briefing(self, goal_id: str | None) -> Briefing | None:
        """The last seven days in three short sections; every number is counted here, not written."""
        if not self._covers(goal_id):
            return None
        since = self.now - timedelta(days=7)
        counts = self._counts(since)
        buys = [e for e in self.events.scan(since=since) if e.type is EventType.PURCHASE]
        revenue = round(sum(e.value or 0.0 for e in buys), 2)
        buyers = len({e.customer_id for e in buys})
        sends, delivers, clicks = counts[EventType.SEND], counts[EventType.DELIVER], counts[EventType.CLICK]
        unsubs, complaints = counts[EventType.UNSUBSCRIBE], counts[EventType.COMPLAINT]
        return Briefing(
            briefing_id=stable_id("brf", goal_id, self.now.isoformat()),
            goal_id=goal_id,
            as_of=self.now,
            period="weekly",
            headline=f"{buyers} customers bought this week, for {revenue:.2f} in revenue.",
            sections=(
                BriefingSection(
                    title="What the agent did",
                    body=f"Sent {sends} messages; {delivers} were delivered and {clicks} got a click.",
                    numbers={"sends": sends, "delivers": delivers, "clicks": clicks},
                ),
                BriefingSection(
                    title="What happened",
                    body=f"{buyers} customers made {len(buys)} purchases worth {revenue:.2f}.",
                    numbers={"buyers": buyers, "purchases": len(buys), "revenue": revenue},
                ),
                BriefingSection(
                    title="What to watch",
                    body=f"{unsubs} customers unsubscribed and {complaints} complained.",
                    numbers={"unsubscribes": unsubs, "complaints": complaints},
                ),
            ),
        )

    def alerts(self) -> list[Alert]:
        counts = self._counts(self.now - timedelta(days=7))
        out = [
            Alert(
                alert_id=stable_id("alert", "demo_data", self.now.isoformat()),
                at=self.now,
                severity="info",
                code="demo_data",
                message="You are looking at demo data from a simulated shop.",
            )
        ]
        delivers, unsubs = counts[EventType.DELIVER], counts[EventType.UNSUBSCRIBE]
        if delivers and unsubs / delivers > 0.005:
            out.append(
                Alert(
                    alert_id=stable_id("alert", "unsubscribes_up", self.now.isoformat()),
                    at=self.now,
                    goal_id=self.goal_id,
                    severity="warning",
                    code="unsubscribes_up",
                    message=(
                        f"{unsubs} customers unsubscribed in the last 7 days "
                        f"({unsubs / delivers:.1%} of deliveries)."
                    ),
                )
            )
        return out

    def health(self) -> HealthStatus:
        counts = self._counts(self.now - timedelta(days=7))
        sends, delivers = counts[EventType.SEND], counts[EventType.DELIVER]
        customers = self.states.count()
        return HealthStatus(
            as_of=self.now,
            delivery_rate=_rate(delivers, sends),
            complaint_rate=_rate(counts[EventType.COMPLAINT], delivers),
            unsubscribe_rate=_rate(counts[EventType.UNSUBSCRIBE], delivers),
            block_rate=_rate(counts[EventType.BLOCK], delivers),
            contacts_per_customer_week=_rate(sends, customers),
            alerts=tuple(self.alerts()),
        )

    def opportunities(self) -> list[Goal]:
        """Goals the agent would suggest, sized from customer state. Drafts: nothing runs until approved."""
        states = list(self.states.scan())
        ideas = [
            (
                "opp_repeat_purchase",
                GoalKind.REPEAT_PURCHASE,
                sum(1 for s in states if s.purchases == 1),
                "Turn {n} one-time buyers into repeat customers",
            ),
            (
                "opp_onboarding",
                GoalKind.ONBOARDING,
                sum(1 for s in states if s.purchases == 0),
                "Get {n} customers who signed up but never bought to a first purchase",
            ),
        ]
        return [
            Goal(
                goal_id=goal_id,
                business_id=self.business_id,
                text=text.format(n=n),
                kind=kind,
                budget=1000,
                starts_at=self.now,
                created_at=self.now,
            )
            for goal_id, kind, n, text in ideas
            if n > 0
        ]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _difference(a: Sequence[float], b: Sequence[float], *, scale: float) -> Estimate | None:
    """(mean(a) - mean(b)) x scale with a normal 95% interval; None when either side is too small."""
    if len(a) < 2 or len(b) < 2:
        return None
    diff = statistics.fmean(a) - statistics.fmean(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return Estimate(
        point=round(diff * scale, 2),
        low=round((diff - 1.96 * se) * scale, 2),
        high=round((diff + 1.96 * se) * scale, 2),
    )


def _demo_content(start: datetime, goal_id: str) -> list[ContentItem]:
    """The sample library plus one draft waiting for review, so the content screen has work to show."""
    items = sample_content(start, goal_id)
    draft = items[0].model_copy(
        update={
            "content_id": "ci_draft_1",
            "theme": Theme.URGENCY,
            "subject": "Your favourites are going fast",
            "body": "Hi {{first_name}}, a few {{top_category}} picks are almost gone.",
            "status": ContentStatus.DRAFT,
        }
    )
    return [*items, draft]


def build_demo_services(
    now: datetime,
    *,
    n_customers: int = 200,
    days: int = 7,
    seed: int = 7,
    clock: Clock | None = None,
) -> Services:
    """In-memory services with a goal, an approved brief, content and `days` of simulated history.

    The simulated week ends at `now`. `clock` is what new goals and approvals are stamped with;
    it defaults to `now`, so the demo is the same on every run.
    """
    if now.tzinfo is None:
        raise ValueError("pass a timezone-aware now")
    start = now - timedelta(days=days)
    workspace = InMemoryWorkspace()
    events = InMemoryEventStore()
    states = InMemoryStateStore()
    decisions = InMemoryDecisionLog()
    folder = SimpleStateFolder()

    goal = sample_goal(start).model_copy(
        update={"autonomy": AutonomyLevel.AUTOPILOT, "status": GoalStatus.ACTIVE}
    )
    brief = sample_brief(start, goal.goal_id)
    content = _demo_content(start, goal.goal_id)
    workspace.put_goal(goal)
    workspace.put_brief(brief)
    workspace.put_content(content)

    run_days(
        env=FakeEnvironment(n_customers=n_customers, start=start),
        policy=_DemoPolicy(),
        events=events,
        states=states,
        decisions=decisions,
        folder=folder,
        briefs=[brief],
        content=[c for c in content if c.status is ContentStatus.APPROVED],
        days=days,
        seed=seed,
        assign_group=lambda s: s.model_copy(update={"group": _demo_group(s.customer_id)}),
    )

    reads = _DemoReadModels(
        workspace=workspace,
        events=events,
        states=states,
        goal_id=goal.goal_id,
        business_id=goal.business_id,
        start=start,
        now=now,
    )
    adapters: dict[Channel, ChannelAdapter] = {
        ch: FakeChannelAdapter(ch, secret=DEMO_SECRET) for ch in (Channel.EMAIL, Channel.WHATSAPP)
    }
    return Services(
        workspace=workspace,
        events=events,
        states=states,
        decisions=decisions,
        proof=reads.proof,
        briefing=reads.briefing,
        health=reads.health,
        alerts=reads.alerts,
        opportunities=reads.opportunities,
        strategist=template_strategist,
        adapters=adapters,
        ingest=make_ingest(events, states, folder),
        clock=clock if clock is not None else (lambda: now),
        business_id=goal.business_id,
    )
