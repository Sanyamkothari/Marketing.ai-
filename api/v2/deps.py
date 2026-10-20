"""What the v2 routes run on: a `Services` container, the operations behind each endpoint, and a demo.

The routes in `api.v2.routes` stay thin: they parse the request, call one `Services` method and
wrap the result. Everything that decides something (what an approval changes, which decisions a
filter keeps, how a webhook is verified and stored) lives here, so it can be tested without an
HTTP server and Wave 2 can swap the in-memory stores for Postgres without touching the routes.

`build_demo_services(now)` wires the fakes from `autonomy.fakes` (stores, a sample goal, an
approved brief, a content library) and runs a week of a simulated shop through the reference loop,
so every endpoint returns real-looking data with no external services. With `simulation=True` it
instead seeds from `autonomy.runtime.demo.run_demo` (a Gym shop run by the whole system, with
agents, batches and briefings) and keeps a runtime attached, so `POST /v2/runtime/tick` works.

`build_services_from_runtime(runtime, clock=...)` is the production wiring: the routes read the
runtime's SQL stores, batches are the runtime's `BatchStore`, and proof, health and opportunities
are computed from the stores on request (`StoreReadModels`). Alerts and briefings come from an
alert store and a briefing store (`autonomy.runtime.stores`) when the runtime has them, merged with
what the API's own ticks reported.

Time is passed in: `Services.clock` is the only place "now" comes from, and the demo fixes it.
"""

from __future__ import annotations

import inspect
import math
import statistics
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias

from starlette.applications import Starlette
from starlette.requests import Request

from autonomy.contracts import (
    NO_OFFER,
    Alert,
    Audience,
    AudienceFilter,
    AutonomyLevel,
    Batch,
    BatchStatus,
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
from autonomy.interfaces import (
    ChannelAdapter,
    DecisionLog,
    Environment,
    EventStore,
    StateFolder,
    StateStore,
    Workspace,
)
from autonomy.loop import run_days

if TYPE_CHECKING:  # imported lazily below, so `import api.v2.deps` stays light
    from api.v2 import insights
    from autonomy.proof import Costs
    from autonomy.runtime.batches import BatchStore
    from autonomy.runtime.service import AutonomyRuntime

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


class TickOutcome(Protocol):
    """What one runtime tick reports. `autonomy.runtime.service.TickReport` is one."""

    @property
    def alerts(self) -> Sequence[Alert]: ...

    @property
    def briefings(self) -> Sequence[Briefing]: ...

    def summary(self) -> dict[str, Any]:
        """Counts plus the new batches and alerts in full, JSON-ready (see `TickResult` in routes)."""
        ...


class Runtime(Protocol):
    """The part of `AutonomyRuntime` the API drives: one tick at a given time."""

    def tick(self, now: datetime) -> TickOutcome: ...


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


class ForbiddenError(ServiceError):
    status_code = 403


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
    batch_store: BatchStore | None = None
    """Decisions held for a person (goals at APPROVE). None: there are no batches to show."""
    runtime: Runtime | None = None
    """The runtime `POST /v2/runtime/tick` drives. None: the endpoint answers 404."""
    allow_tick: bool = False
    """Whether the operator tick endpoint may run. Only the demo and operator setups turn it on;
    in production the worker ticks on its own schedule."""
    catalogue: Mapping[str, str] = field(default_factory=dict)
    """item_id -> product name, for the words "Due for a refill of ..."; empty: names come from events."""
    data_doctor: Callable[[], Mapping[str, Any] | None] | None = None
    """The Data Doctor's latest report (`fixes`, `problems`, `questions`), when one is wired in."""
    _tick_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

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
        budget: float | None,
        currency: str = "USD",
        kind: GoalKind | None = None,
        objective: Objective | None = None,
        ends_at: datetime | None = None,
    ) -> tuple[Goal, Brief]:
        """Store the marketer's goal as a draft and the Strategist's (unapproved) brief for it.

        With no `budget`, the amount (and the period, unless `ends_at` is given) are read from the
        words, e.g. "$5,000 this month"; words that name no amount are refused. A given budget is
        used as it is and the words are not read.
        """
        text = text.strip()
        if not text:
            raise BadRequestError("a goal needs some words")
        now = self.clock()
        if budget is None:
            from api.v2.insights import read_goal_text

            reading = read_goal_text(text, now)
            if reading.budget is None:
                raise BadRequestError("say how much you want to spend, e.g. '$2,000 this month'")
            budget = reading.budget
            if reading.currency is not None and currency.upper() == "USD":
                currency = reading.currency
            if ends_at is None:
                ends_at = reading.ends_at
        # Python's JSON parser reads 1e999 as infinity, and a stored infinite budget would break
        # every later response that includes the goal, so refuse it here.
        if not math.isfinite(budget) or budget < 0:
            raise BadRequestError("the budget must be a finite amount of zero or more")
        if ends_at is not None and (ends_at.tzinfo is None or ends_at <= now):
            raise BadRequestError("the end date must include a time zone and be in the future")
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
            ends_at=ends_at,
            created_at=now,
        )
        brief = self.strategist(goal)
        if brief.goal_id != goal.goal_id:
            raise ValueError(f"the strategist answered goal {goal.goal_id} with a brief for {brief.goal_id}")
        self.workspace.put_goal(goal)
        self.workspace.put_brief(brief)
        return goal, brief

    def set_budget(self, goal_id: str, budget: float) -> tuple[Goal, Brief | None]:
        """Change a goal's budget: "Approve mode for one goal, with a small budget".

        Lowering is always allowed. An approved plan was approved with its budget, so raising the
        goal above it needs a new plan; a draft plan simply takes the new budget.
        """
        if not math.isfinite(budget) or budget < 0:
            raise BadRequestError("the budget must be a finite amount of zero or more")
        goal = self.get_goal(goal_id)
        if goal.status is GoalStatus.DONE:
            raise ConflictError("this goal is finished; start a new one instead")
        brief = self.latest_brief(goal_id)
        if brief is not None and not brief.approved:
            envelope = brief.envelope.model_copy(update={"budget": budget})
            brief = brief.model_copy(update={"envelope": envelope})
            self.workspace.put_brief(brief)
        elif brief is not None and budget > brief.envelope.budget:
            raise ConflictError(
                f"the approved plan's budget is {brief.envelope.budget:g}; raising it needs a new plan"
            )
        updated: Goal = goal.model_copy(update={"budget": budget})
        self.workspace.put_goal(updated)
        return updated, brief

    def pause_all(self) -> list[Goal]:
        """The one big Pause button: pause every active goal; returns the goals it paused."""
        return [self.pause(g.goal_id) for g in self.list_goals() if g.status is GoalStatus.ACTIVE]

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
        # "Nothing goes out without passing" (docs/v2/PITCH.md): a person may reject or retire any
        # message, but may approve only one the strict editor (the Critic) has passed.
        if status is ContentStatus.APPROVED and not editor_passed(item):
            raise ConflictError(
                "the editor has not passed this message; edit it and run it through the editor again"
            )
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

    # batches ------------------------------------------------------------------------------------

    def list_batches(self, goal_id: str | None = None, status: BatchStatus | None = None) -> list[Batch]:
        """Newest first, like every other list here. An unknown goal is a 404, not an empty list."""
        if goal_id is not None:
            self.get_goal(goal_id)
        if self.batch_store is None:
            return []
        statuses = (status,) if status is not None else None
        rows = self.batch_store.list_batches(goal_id=goal_id, statuses=statuses)
        return sorted(rows, key=lambda b: (b.created_at, b.batch_id), reverse=True)

    def approve_batch(self, batch_id: str) -> Batch:
        """A person signs off a pending batch; the runtime sends it on its next tick."""
        from autonomy.runtime.batches import approve

        return self._move_batch(batch_id, approve)

    def reject_batch(self, batch_id: str) -> Batch:
        """A person turns a pending batch down; nothing in it is ever sent."""
        from autonomy.runtime.batches import reject

        return self._move_batch(batch_id, reject)

    def _move_batch(self, batch_id: str, move: Callable[..., Batch]) -> Batch:
        """404 names the batch nobody has (so a client can tell it from a missing route); 409 says
        what already happened to a batch that is no longer pending."""
        from autonomy.runtime.batches import BatchTransitionError, UnknownBatchError

        if self.batch_store is None:
            raise NotFoundError(f"no batch {batch_id!r}")
        try:
            return move(self.batch_store, batch_id, by=APPROVER, now=self.clock())
        except UnknownBatchError as exc:
            raise NotFoundError(f"no batch {batch_id!r}") from exc
        except BatchTransitionError as exc:  # already decided, sent or expired
            if exc.current is BatchStatus.PENDING:  # another writer moved it as this one saved
                raise ConflictError(f"batch {batch_id!r} changed while this was saved; try again") from exc
            raise ConflictError(
                f"batch {batch_id!r} is already {exc.current.value}; only a pending batch can be "
                "approved or rejected"
            ) from exc

    # the operator tick --------------------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """Run one runtime tick at `now` (default: the clock) and return its summary.

        Ticks are serialised: the runtime keeps a cursor, and two ticks at once would race on it.
        """
        if self.runtime is None:
            raise NotFoundError("this API has no runtime to tick")
        if not self.allow_tick:
            raise ForbiddenError("ticking by hand is turned off here; the worker ticks on its schedule")
        at = self.clock() if now is None else now
        if at.tzinfo is None or at.utcoffset() is None:
            raise BadRequestError("now must include a time zone")
        with self._tick_lock:
            return self.runtime.tick(at).summary()

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

    # what the promises need (the counting lives in api.v2.insights) ------------------------------

    def today(self, goal_id: str | None = None, day: date | None = None) -> insights.Today:
        """One day of decisions and events for a goal, counted over every decision that day."""
        from api.v2 import insights

        if goal_id is not None:
            self.get_goal(goal_id)
        return insights.today(self, goal_id, day)

    def activity(
        self, *, goal_id: str | None = None, customer_id: str | None = None, limit: int = 50
    ) -> list[insights.ActivityRow]:
        from api.v2 import insights

        return insights.activity(self, goal_id=goal_id, customer_id=customer_id, limit=limit)

    def customer_insight(self, customer_id: str) -> insights.CustomerInsight:
        from api.v2 import insights

        return insights.customer_insight(self, customer_id)

    def plan_facts(self, goal_id: str) -> insights.PlanFacts:
        from api.v2 import insights

        return insights.plan_facts(self, goal_id)

    def expected(self, goal_id: str, since: datetime | None = None) -> insights.Expected:
        from api.v2 import insights

        return insights.expected(self, goal_id, since)

    def health_limits(self, goal_id: str | None = None) -> insights.HealthLimits:
        from api.v2 import insights

        if goal_id is not None:
            self.get_goal(goal_id)
        return insights.health_limits(self, goal_id)

    def data_status(self) -> insights.DataStatus:
        from api.v2 import insights

        return insights.data_status(self)

    def channels(self) -> list[insights.ChannelStatus]:
        from api.v2 import insights

        return insights.channels(self)

    def sized_opportunities(self) -> list[insights.SizedOpportunity]:
        from api.v2 import insights

        return insights.sized_opportunities(self)

    def read_goal(self, text: str) -> insights.GoalReading:
        from api.v2 import insights

        return insights.read_goal_text(text, self.clock())

    def batch_breakdown(self, batch_id: str) -> insights.BatchBreakdown:
        from api.v2 import insights

        batch = self.batch_store.get(batch_id) if self.batch_store is not None else None
        if batch is None:
            raise NotFoundError(f"no batch {batch_id!r}")
        return insights.batch_breakdown(self, batch)


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

SEND_COST: dict[Channel, float] = {Channel.EMAIL: 0.001, Channel.WHATSAPP: 0.04, Channel.SMS: 0.0075}
"""Rough per-message costs, so the demo's proof has a spend to set against profit: about $1 per
thousand emails, a WhatsApp marketing-template message (Meta prices them by country, mostly
$0.01-0.10), and a US text message. The simulated demo uses `autonomy.runtime.demo.DEMO_CHANNEL_COST`."""


def _demo_group(customer_id: str) -> Group:
    """A stable 80 / 10 / 10 split: agent / baseline / holdout."""
    bucket = int(stable_id("grp", customer_id).split("_", 1)[1], 16) % 10
    if bucket == 0:
        return Group.HOLDOUT
    if bucket == 1:
        return Group.BASELINE
    return Group.AGENT


class _NamedEnvironment:
    """The fake world with a first name on each signup (`autonomy.runtime.demo.with_first_names`), so
    the demo's people read as people. The API only ever shows a name the data has."""

    def __init__(self, inner: Environment) -> None:
        self.inner = inner

    def reset(self, seed: int) -> Sequence[Event]:
        history = self.inner.reset(seed)
        try:
            from autonomy.runtime.demo import with_first_names
        except ImportError:  # the runtime's demo layer is not installed: ids only, as before
            return history
        return with_first_names(history)

    def now(self) -> datetime:
        return self.inner.now()

    def act(self, decisions: Sequence[Decision]) -> None:
        self.inner.act(decisions)

    def advance(self, until: datetime) -> Sequence[Event]:
        return self.inner.advance(until)


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
        return suggest_opportunities(self.states.scan(), self.business_id, self.now)


def suggest_opportunities(
    states: Iterable[CustomerState],
    business_id: str,
    now: datetime,
    *,
    taken: frozenset[GoalKind] = frozenset(),
) -> list[Goal]:
    """Draft goals sized from customer state; kinds in `taken` (already being worked on) are left out."""
    rows = list(states)
    ideas = [
        (
            "opp_repeat_purchase",
            GoalKind.REPEAT_PURCHASE,
            sum(1 for s in rows if s.purchases == 1),
            "Turn {n} one-time buyers into repeat customers",
        ),
        (
            "opp_onboarding",
            GoalKind.ONBOARDING,
            sum(1 for s in rows if s.purchases == 0),
            "Get {n} customers who signed up but never bought to a first purchase",
        ),
    ]
    return [
        Goal(
            goal_id=goal_id,
            business_id=business_id,
            text=text.format(n=n),
            kind=kind,
            budget=1000,
            starts_at=now,
            created_at=now,
        )
        for goal_id, kind, n, text in ideas
        if n > 0 and kind not in taken
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


def editor_passed(item: ContentItem) -> bool:
    """Whether the Critic's last word on this item was a pass (its notes end with "approved...")."""
    return bool(item.critic_notes) and item.critic_notes[-1].startswith("approved")


def _demo_content(start: datetime, goal_id: str) -> list[ContentItem]:
    """The sample library plus one draft waiting for review, so the content screen has work to show."""
    items = sample_content(start, goal_id)
    draft = items[0].model_copy(
        update={
            "content_id": "ci_draft_1",
            "theme": Theme.URGENCY,
            "subject": "Your favourites are going fast",
            "body": "Hi {{first_name}}, a few {{top_category}} picks are almost gone.",
            "status": ContentStatus.DRAFT,  # passed the editor; waiting for a person's yes
            "critic_notes": ("rules: all passed", "judge: brand voice 5, honesty 5", "approved"),
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
    simulation: bool = False,
    scenario: str = "baseline_shop",
    approve_goal_days: int | None = None,
    hold_batches: bool = False,
) -> Services:
    """In-memory services with a goal, an approved brief, content and `days` of simulated history.

    The simulated week ends at `now`. `clock` is what new goals and approvals are stamped with;
    it defaults to `now`, so the demo is the same on every run.

    `simulation=True` seeds from the whole system instead (`simulated_demo_services`): a Gym shop
    with two goals, agents, batches and briefings, and a runtime the tick endpoint drives. When
    that is not installed, it falls back to the simple fake-world demo below. `approve_goal_days`
    and `hold_batches` shape the simulated demo only (see `simulated_demo_services`).
    """
    if now.tzinfo is None:
        raise ValueError("pass a timezone-aware now")
    if simulation:
        simulated = simulated_demo_services(
            now,
            n_customers=n_customers,
            days=days,
            seed=seed,
            clock=clock,
            scenario=scenario,
            approve_goal_days=approve_goal_days,
            hold_batches=hold_batches,
        )
        if simulated is not None:
            return simulated
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
        env=_NamedEnvironment(FakeEnvironment(n_customers=n_customers, start=start)),
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


# --- services over a real runtime ---------------------------------------------------------------------

DEFAULT_HORIZON_DAYS = 14
"""The outcome window the proof uses when no brief says otherwise."""
HEALTH_WINDOW = timedelta(days=7)
ALERTS_LIMIT = 200
"""The most alerts `/v2/alerts` returns (newest first)."""
_TICK = timedelta(microseconds=1)
"""Store scans exclude `until`; adding this makes them include it."""


@dataclass
class StoreReadModels:
    """Proof, briefing, health, alerts and opportunities read from the stores on each request.

    Proof is computed the way the Gym and the demo compute it (`autonomy.proof`): decisions whose
    outcome window has closed by now, last-touch rewards, agent vs holdout and vs baseline.

    Alerts and briefings are read from `alert_store` and `briefing_store` when there are any (the
    runtime's stores, with `list` and `latest` methods), and merged with the ones kept here: seeded
    by the caller, and appended to by every tick the API runs (`record`). Merging means nothing is
    lost whether or not the runtime also writes what a tick reports into its stores; an alert that
    is in both is shown once.
    """

    workspace: Workspace
    events: EventStore
    states: StateStore
    decisions: DecisionLog
    clock: Clock
    business_id: str
    costs: Costs | None = None
    """Channel and offer costs for the proof; by default `SEND_COST` and the briefs' offer costs."""
    alerts_log: list[Alert] = field(default_factory=list)
    briefings_log: list[Briefing] = field(default_factory=list)
    notes: tuple[str, ...] = ()
    """Added to every proof summary, e.g. that the data is simulated."""
    seed: int = 0
    alert_store: Any = None
    """Anything with `list(...) -> Sequence[Alert]`, e.g. `autonomy.runtime.stores`' alert store."""
    briefing_store: Any = None
    """Anything with `latest(goal_id) -> Briefing | None` (or `list(...)`), e.g. the briefing store."""

    def record(self, report: TickOutcome) -> None:
        self.alerts_log.extend(report.alerts)
        self.briefings_log.extend(report.briefings)

    def proof(self, goal_id: str | None) -> ProofSummary | None:
        from autonomy.proof import Costs, proof_rewards, proof_summary

        now = self.clock()
        briefs = self.workspace.briefs()
        objective = Objective.GROSS_PROFIT
        if goal_id is not None:
            goal = next((g for g in self.workspace.goals() if g.goal_id == goal_id), None)
            if goal is None:
                return None
            objective = goal.objective
            briefs = [b for b in briefs if b.goal_id == goal_id]
        if next(self.decisions.scan(until=now + _TICK), None) is None:
            return None
        horizon = max((b.horizon_days for b in briefs), default=DEFAULT_HORIZON_DAYS)
        cutoff = now - timedelta(days=horizon)
        closed = [d for d in self.decisions.scan(until=cutoff + _TICK) if d.made_at <= cutoff]
        by_customer = {
            c: self.events.for_customer(c, until=now) for c in sorted({d.customer_id for d in closed})
        }
        costs = self.costs or Costs(
            channel=SEND_COST,
            offer={o.offer_id: o.cost_per_redemption for b in briefs for o in b.envelope.offers},
        )
        rewards = proof_rewards(closed, by_customer, horizon, objective, costs, now=now)
        summary = proof_summary(rewards, closed, now, goal_id, seed=self.seed)
        if not closed:
            wait = f"Outcomes count once a decision's {horizon}-day window has closed; none has yet."
            summary = summary.model_copy(update={"notes": (*summary.notes, wait)})
        elif goal_id is not None and not any(_touches_goal(d, goal_id) for d in closed):
            # A goal that started recently: say why it has no numbers of its own yet.
            wait = (
                f"Outcomes count once a decision's {horizon}-day window has closed; none of this "
                "goal's decisions has reached that yet."
            )
            summary = summary.model_copy(update={"notes": (*summary.notes, wait)})
        if self.notes:
            summary = summary.model_copy(update={"notes": (*summary.notes, *self.notes)})
        return summary

    def briefing(self, goal_id: str | None) -> Briefing | None:
        """The newest briefing for exactly this goal (None: the programme-wide one)."""
        mine = [b for b in self.briefings_log if b.goal_id == goal_id]
        mine.extend(b for b in _stored_briefings(self.briefing_store, goal_id) if b.goal_id == goal_id)
        return max(mine, key=lambda b: (b.as_of, b.briefing_id)) if mine else None

    def alerts(self) -> list[Alert]:
        """Newest first, each alert once (a tick repeated at the same time raises the same ids)."""
        unique = {a.alert_id: a for a in [*_stored_alerts(self.alert_store), *self.alerts_log]}
        return sorted(unique.values(), key=lambda a: (a.at, a.alert_id), reverse=True)[:ALERTS_LIMIT]

    def health(self) -> HealthStatus:
        now = self.clock()
        # `until` is exclusive; what happened at this very instant (a tick's sends) counts.
        counts = Counter(e.type for e in self.events.scan(since=now - HEALTH_WINDOW, until=now + _TICK))
        sends, delivers = counts[EventType.SEND], counts[EventType.DELIVER]
        return HealthStatus(
            as_of=now,
            delivery_rate=_rate(delivers, sends),
            complaint_rate=_rate(counts[EventType.COMPLAINT], delivers),
            unsubscribe_rate=_rate(counts[EventType.UNSUBSCRIBE], delivers),
            block_rate=_rate(counts[EventType.BLOCK], delivers),
            contacts_per_customer_week=_rate(sends, self.states.count()),
            alerts=tuple(self.alerts()),
        )

    def opportunities(self) -> list[Goal]:
        """Suggestions for kinds of goal nobody is working on yet."""
        taken = frozenset(g.kind for g in self.workspace.goals() if g.status is not GoalStatus.DONE)
        return suggest_opportunities(self.states.scan(), self.business_id, self.clock(), taken=taken)


def _call_with(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Call `fn` with the keyword arguments it declares, and leave out the ones it does not.

    The stores' read methods may take optional filters (`goal_id`, `limit`) or none; this lets the
    API ask for what it wants from either. A single required argument with another name gets the
    first value positionally, so `latest(goal)` works as well as `latest(goal_id=...)`."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # a builtin without a signature: call it plainly
        return fn()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    named = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    accepted = {k: v for k, v in kwargs.items() if k in params and params[k].kind in named}
    required = [
        p
        for p in params.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and p.name not in accepted
    ]
    if len(required) == 1 and kwargs and not accepted:
        return fn(next(iter(kwargs.values())))
    return fn(**accepted)


def _stored_alerts(store: Any) -> list[Alert]:
    """The alerts an alert store holds, newest first; [] when there is no store."""
    read = getattr(store, "list", None)
    if not callable(read):
        return []
    rows = _call_with(read, limit=ALERTS_LIMIT)
    return [a if isinstance(a, Alert) else Alert.model_validate(a) for a in rows or ()]


def _stored_briefings(store: Any, goal_id: str | None) -> list[Briefing]:
    """The briefing store's newest briefing for this goal (from `latest`, else from `list`)."""
    latest = getattr(store, "latest", None)
    if callable(latest):
        found = _call_with(latest, goal_id=goal_id)
        rows: Iterable[Any] = () if found is None else (found,)
    else:
        read = getattr(store, "list", None)
        if not callable(read):
            return []
        rows = _call_with(read, goal_id=goal_id) or ()
    return [b if isinstance(b, Briefing) else Briefing.model_validate(b) for b in rows]


def _runtime_store(runtime: object, names: Sequence[str], method: str) -> Any:
    """The first of the runtime's attributes in `names` that has `method`; None if none has."""
    for name in names:
        store = getattr(runtime, name, None)
        if store is not None and callable(getattr(store, method, None)):
            return store
    return None


@dataclass
class _RecordingRuntime:
    """Ticks the runtime and keeps what each tick said (alerts, briefings) for the read endpoints."""

    inner: Runtime
    reads: StoreReadModels

    def tick(self, now: datetime) -> TickOutcome:
        report = self.inner.tick(now)
        self.reads.record(report)
        return report


def build_services_from_runtime(
    runtime: AutonomyRuntime,
    *,
    clock: Clock,
    business_id: str = "default",
    strategist: Strategist = template_strategist,
    costs: Costs | None = None,
    adapters: Mapping[Channel, ChannelAdapter] | None = None,
    alerts: Sequence[Alert] = (),
    briefings: Sequence[Briefing] = (),
    notes: Sequence[str] = (),
    allow_tick: bool = False,
    seed: int = 0,
    alert_store: Any = None,
    briefing_store: Any = None,
    catalogue: Mapping[str, str] | None = None,
) -> Services:
    """Services over a running `AutonomyRuntime` and its stores (SQL in production).

    The API and the runtime share every store, so a goal approved here is what the next tick
    reads, and a batch approved here is what it sends. Webhooks only append their events: the
    runtime folds them into state on its next tick, as the worker's webhook handler does, so a
    callback is never folded twice. `adapters` defaults to the dispatcher's.

    `alert_store` and `briefing_store` default to the runtime's own (`runtime.alert_store` and
    `runtime.briefing_store`, when it has them); `alerts` and `briefings` are served alongside.
    `catalogue` (item_id -> product name) is what product ids are shown as; none: names come from events.
    """
    if alert_store is None:
        alert_store = _runtime_store(runtime, ("alert_store", "alerts_store"), "list")
    if briefing_store is None:
        briefing_store = _runtime_store(runtime, ("briefing_store", "briefings_store"), "latest")
        if briefing_store is None:
            briefing_store = _runtime_store(runtime, ("briefing_store", "briefings_store"), "list")
    reads = StoreReadModels(
        workspace=runtime.workspace,
        events=runtime.events,
        states=runtime.states,
        decisions=runtime.decisions,
        clock=clock,
        business_id=business_id,
        costs=costs,
        alerts_log=list(alerts),
        briefings_log=list(briefings),
        notes=tuple(notes),
        seed=seed,
        alert_store=alert_store,
        briefing_store=briefing_store,
    )
    if adapters is None:
        adapters = dict(runtime.dispatcher.adapters) if runtime.dispatcher is not None else {}
    events = runtime.events

    def ingest(batch: Sequence[Event]) -> int:
        return events.append(batch)

    return Services(
        workspace=runtime.workspace,
        events=runtime.events,
        states=runtime.states,
        decisions=runtime.decisions,
        proof=reads.proof,
        briefing=reads.briefing,
        health=reads.health,
        alerts=reads.alerts,
        opportunities=reads.opportunities,
        strategist=strategist,
        adapters=adapters,
        ingest=ingest,
        clock=clock,
        business_id=business_id,
        batch_store=runtime.batches,
        runtime=_RecordingRuntime(runtime, reads),
        allow_tick=allow_tick,
        catalogue=dict(catalogue or {}),
    )


# --- the simulation-backed demo -----------------------------------------------------------------------

SIMULATION_NOTE = "Demo data from a simulated shop, not your customers."


def _demo_address(customer_id: str, channel: Channel) -> str:
    return f"{customer_id}.{channel.value}@customers.demo.invalid"


def simulated_demo_services(
    now: datetime,
    *,
    n_customers: int = 200,
    days: int = 7,
    seed: int = 7,
    clock: Clock | None = None,
    scenario: str = "baseline_shop",
    approve_goal_days: int | None = None,
    hold_batches: bool = False,
) -> Services | None:
    """A Gym shop run by the whole system for `days` days (`autonomy.runtime.demo.run_demo`), served.

    `approve_goal_days` starts the APPROVE goal that many days before the end instead of at the
    start, and `hold_batches` leaves the simulation's last batches pending for the marketer (see
    `run_demo`); `python -m api.v2.app` sets both, so the app opens with a batch to approve.

    The simulation ends at the last daily sweep (09:00 UTC) at or before `now`. The API then keeps
    a runtime over the same stores, with the tick endpoint turned on: each tick decides, holds the
    APPROVE goal's contacts in a pending batch for the marketer, and sends the rest through fake
    channels. The Gym world itself is not kept, so messages sent after the simulation get no
    replies or purchases. Returns None when the runtime or the Gym is not installed.
    """
    try:
        from autonomy.channels.dispatcher import Dispatcher
        from autonomy.data import StateFolder as ProductionFolder
        from autonomy.proof import group_assigner
        from autonomy.runtime.demo import (
            BUSINESS_ID,
            DECIDE_HOUR,
            EnrichingStateStore,
            SpendTrackingEventStore,
            demo_catalogue,
            goal_pacing,
            policy_factory,
            product_name,
            run_demo,
        )
        from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig, standard_breakers
    except ImportError:
        return None
    if days < 1:
        raise ValueError("days must be at least 1")
    utc_now = now.astimezone(UTC)
    end = utc_now.replace(hour=DECIDE_HOUR, minute=0, second=0, microsecond=0)
    if end > utc_now:
        end -= timedelta(days=1)
    result = run_demo(
        scenario,
        days=days,
        customers=n_customers,
        seed=seed,
        start=end - timedelta(days=days),
        approve_goal_days=approve_goal_days,
        hold_last_batches=hold_batches,
    )
    st = result.stores
    costs = result.costs
    adapters: dict[Channel, ChannelAdapter] = {
        ch: FakeChannelAdapter(ch, secret=DEMO_SECRET) for ch in (Channel.EMAIL, Channel.WHATSAPP)
    }
    # A goal's budget is a hard limit: carry the simulation's spend over (the tracker only counts
    # SENDs appended through it), so ticks after the simulation see what is left, and keep the
    # circuit breakers (spend pace included) the simulation ran with.
    events = SpendTrackingEventStore(st.events, costs.channel)
    item_ids: set[str] = set()
    for e in st.events.scan():
        goal_of = e.attrs.get("goal_id")
        if e.type is EventType.SEND and e.channel is not None and isinstance(goal_of, str):
            events.spend[goal_of] += float(costs.channel.get(e.channel, 0.0))
        if e.item_id:
            item_ids.add(e.item_id)
    # What product ids are shown as: the demo shop's names, else the id in words (never a bare id).
    catalogue = demo_catalogue()
    catalogue.update({i: product_name(i) for i in item_ids if i not in catalogue})
    # Paced from each goal's own start, as in the simulation: the APPROVE goal may have started late.
    planned = goal_pacing(result.goals, result.start)

    def spend(goal_id: str, now: datetime) -> float:  # noqa: ARG001
        return events.spend.get(goal_id, 0.0)

    # The refill goal selects its audience by refill scores, so later ticks read enriched states too.
    hero = result.hero
    runtime = AutonomyRuntime(
        events=events,
        states=EnrichingStateStore(st.states, hero) if hero is not None else st.states,
        decisions=st.decisions,
        workspace=st.workspace,
        folder=ProductionFolder(),
        policy_factory=policy_factory({g.goal_id: g.kind for g in result.goals}, costs, hero=hero),
        queue=st.queue,
        dispatcher=Dispatcher(adapters, _demo_address, st.workspace, events, st.queue),
        batches=st.batches,
        breakers_fn=standard_breakers(spend_fn=spend, planned_fn=planned),
        spend_fn=spend,
        assign_group=group_assigner(BUSINESS_ID, envelope=result.briefs[0].envelope),
        config=RuntimeConfig(sweep_hour=DECIDE_HOUR, seed=seed, worker="demo-api", nightly_rebuild=False),
    )
    demo_alert = Alert(
        alert_id=stable_id("alert", "demo_data", result.end.isoformat()),
        at=result.end,
        severity="info",
        code="demo_data",
        message="You are looking at demo data from a shop simulated by the Autonomy Gym.",
    )
    return build_services_from_runtime(
        runtime,
        clock=clock if clock is not None else (lambda: now),
        business_id=BUSINESS_ID,
        costs=costs,
        adapters=adapters,
        alerts=[demo_alert, *result.alerts],
        briefings=result.briefings,
        notes=(SIMULATION_NOTE,),
        allow_tick=True,
        seed=seed,
        catalogue=catalogue,
    )
