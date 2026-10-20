"""Failure isolation, row by row: when one engine fails, the others degrade gracefully.

Each row of the founder's failure-isolation table (docs/v2/FAILURE_ISOLATION.md) has its tests
here, named `test_p<engine>_...`. Each one makes that engine fail with fakes (a store that raises,
an LLM that is down, a carrier that answers 503) and checks the fallback the table promises.

Where today's code does not degrade the way the table says, the test is still written, against
the promised behaviour, and marked xfail with the gap and where to fix it. Nothing here is made to
pass by weakening it. A few tests use optional runtime hooks wired in this round (`gateway=`,
`mandates=`, `state_enricher=` on `AutonomyRuntime`); `_require_hook` xfails them at once if the
hook is missing, so a branch without the wiring reports the gap instead of a TypeError.

Everything runs on in-memory fakes and passed-in time, so the whole file takes about a second.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from autonomy.agents.analyst import Analyst
from autonomy.agents.creative import EMAIL_FOOTER, Creative
from autonomy.agents.critic import BrandRules, Critic
from autonomy.agents.strategist import TEMPLATES, BusinessFacts, Strategist
from autonomy.channels.dispatcher import Dispatcher, DispatchStatus, SkipReason
from autonomy.channels.gateway import GATEWAY_DECISION_JOB, DispatchGateway, Provider, TelemetryEvent
from autonomy.contracts import (
    Action,
    AutonomyLevel,
    Brief,
    Briefing,
    Channel,
    ChannelStats,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Estimate,
    Event,
    EventType,
    Goal,
    GoalKind,
    GoalStatus,
    Group,
    HealthStatus,
    Job,
    Message,
    ProofSummary,
    Reward,
    SendReceipt,
)
from autonomy.fakes import (
    ContactEveryonePolicy,
    FakeChannelAdapter,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryStateStore,
    InMemoryWorkspace,
    NothingPolicy,
    SimpleStateFolder,
)
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_brief, sample_content, sample_goal
from autonomy.interfaces import EventStore, Policy
from autonomy.mandate import (
    ActionCosts,
    FrequencyCap,
    Mandate,
    MandateStatus,
    QuietHours,
    SpendToDate,
    check_action,
    mandate_to_envelope,
)
from autonomy.proof.split import assign_group, group_assigner
from autonomy.runtime.service import AutonomyRuntime, PolicyFactory, RuntimeConfig, SpendFn
from engine.llm import LLMCompletion, LLMError

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # a Monday, before the 10:00 sweep
SWEEP = T0.replace(hour=10)
DAY = timedelta(days=1)
GOAL = "goal_winback"
BROKEN = "goal_broken"  # sorts before GOAL, so it is decided first in a tick
CUSTOMERS = ("c1", "c2", "c3", "c4")
GAP = "docs/v2/FAILURE_ISOLATION.md"


# --- shared fakes ----------------------------------------------------------------------------------


def history(customers: Sequence[str] = CUSTOMERS, *, seen_at: datetime | None = None) -> list[Event]:
    """Each customer consented to email long ago and last bought 90 days before T0 (a lapsed buyer).

    `seen_at` adds a visit per customer then, so the feed looks live up to that moment."""
    out: list[Event] = []
    for cid in customers:
        out.append(
            Event(
                event_id=f"consent_{cid}",
                customer_id=cid,
                type=EventType.CONSENT_GRANTED,
                ts=T0 - timedelta(days=200),
                source="test",
                channel=Channel.EMAIL,
            )
        )
        out.append(
            Event(
                event_id=f"buy_{cid}",
                customer_id=cid,
                type=EventType.PURCHASE,
                ts=T0 - timedelta(days=90),
                source="test",
                value=40.0,
                category="shoes",
            )
        )
        if seen_at is not None:
            out.append(
                Event(
                    event_id=f"visit_{cid}", customer_id=cid, type=EventType.VISIT, ts=seen_at, source="test"
                )
            )
    return out


class FlakyFeed:
    """An `EventStore` whose read path (the feed the runtime folds from) can go down.

    Only `scan` fails: appends and per-decision lookups still work, so the dispatcher can record
    and de-duplicate sends. This is the Data Doctor / ingest side being unreachable."""

    def __init__(self, inner: InMemoryEventStore) -> None:
        self.inner = inner
        self.down = False

    def append(self, events: Any) -> int:
        return self.inner.append(events)

    def for_customer(
        self, customer_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Event]:
        return self.inner.for_customer(customer_id, since=since, until=until)

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        if self.down:
            raise ConnectionError("the event feed is unreachable")
        return self.inner.scan(since=since, until=until)

    def for_decision(self, decision_id: str) -> list[Event]:
        return self.inner.for_decision(decision_id)


class RaisingPolicy:
    """A decision policy that crashes for every customer (or only for those in `only`)."""

    version = "raising-v0"

    def __init__(self, only: Sequence[str] | None = None) -> None:
        self.only = set(only) if only is not None else None
        self.fallback = ContactEveryonePolicy()

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        if self.only is None or state.customer_id in self.only:
            raise RuntimeError("the decision policy crashed")
        return self.fallback.decide(state, ctx)

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


@dataclass
class BudgetedRogue:
    """Contacts everyone it can afford, and labels every customer agent: it ignores the proof split.

    It keeps to `ctx.budget_left`, counting what it committed earlier in the same tick, as decide()
    does. It never looks at the customer's group, so only the runtime and the dispatcher keep the
    holdout uncontacted. `seen` records the budget the runtime handed it at each tick."""

    cost: float
    version: str = "rogue-v0"
    seen: dict[datetime, float] = field(default_factory=dict)
    committed: dict[datetime, float] = field(default_factory=dict)

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        left = ctx.budget_left.get(GOAL, math.inf)
        self.seen.setdefault(ctx.now, left)
        spent = self.committed.get(ctx.now, 0.0)
        agent = state.model_copy(update={"group": Group.AGENT})
        if left - spent < self.cost:
            return NothingPolicy().decide(agent, ctx)
        decision = ContactEveryonePolicy().decide(agent, ctx)
        if decision.kind is DecisionKind.ACT:
            self.committed[ctx.now] = spent + self.cost
        return decision

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


class DownLLM:
    """An `LLMClient` whose provider is unreachable: every call raises `LLMError(LLM_UNAVAILABLE)`."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        self.calls += 1
        raise LLMError("LLM_UNAVAILABLE", "the model provider is unreachable")

    def embed(self, texts: Sequence[str], *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        raise LLMError("LLM_UNAVAILABLE", "the model provider is unreachable")

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        return max(1, len(text) // 4)


class Carrier:
    """A channel provider that is down (HTTP 503, or a read timeout) until `up` is set.

    Like a real provider that honours idempotency keys, it accepts one message id once: a second
    send of the same id returns the first receipt and delivers nothing new. That idempotency would
    hide a gateway that re-sends an accepted message, so `accepted` counts every call answered
    while up, and the tests check it against `delivered`."""

    def __init__(self, channel: Channel = Channel.EMAIL, *, timeout: bool = False) -> None:
        self.channel = channel
        self.timeout = timeout
        self.up = False
        self.attempts: list[Message] = []
        self.delivered: list[Message] = []
        self.accepted: list[Message] = []

    def send(self, message: Message) -> SendReceipt:
        self.attempts.append(message)
        if not self.up:
            if self.timeout:
                raise TimeoutError("read timed out")
            return SendReceipt(accepted=False, error="HTTP 503: service unavailable")
        self.accepted.append(message)
        if all(m.message_id != message.message_id for m in self.delivered):
            self.delivered.append(message)
        return SendReceipt(accepted=True, provider_message_id=f"pm_{message.message_id}")

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        return []


def _require_hook(name: str, why: str) -> None:
    """xfail now when `AutonomyRuntime` has no `name` keyword yet; run the test for real once it has."""
    if name not in inspect.signature(AutonomyRuntime).parameters:
        pytest.xfail(f"AutonomyRuntime has no `{name}=` hook yet: {why} See {GAP}.")


# --- a runtime on fakes ------------------------------------------------------------------------------


@dataclass
class World:
    runtime: AutonomyRuntime
    log: InMemoryEventStore
    events: EventStore
    workspace: InMemoryWorkspace
    decisions: InMemoryDecisionLog
    states: InMemoryStateStore
    adapter: FakeChannelAdapter
    queue: InMemoryJobQueue

    def sends(self) -> list[Event]:
        return [e for e in self.log.scan() if e.type is EventType.SEND]


def _put_goal(ws: InMemoryWorkspace, goal_id: str, autonomy: AutonomyLevel, budget: float | None) -> None:
    start = T0 - DAY
    goal = sample_goal(start, goal_id).model_copy(update={"autonomy": autonomy, "status": GoalStatus.ACTIVE})
    brief = sample_brief(start, goal_id)
    if budget is not None:
        goal = goal.model_copy(update={"budget": budget})
        brief = brief.model_copy(update={"envelope": brief.envelope.model_copy(update={"budget": budget})})
    ws.put_goal(goal)
    ws.put_brief(brief)
    # Content ids are per goal, or a second goal's library would overwrite the first one's.
    ws.put_content(
        c.model_copy(update={"content_id": f"{goal_id}_{c.content_id}"})
        for c in sample_content(start, goal_id)
    )


def build(
    *,
    autonomy: AutonomyLevel = AutonomyLevel.AUTOPILOT,
    policy: Policy | None = None,
    policy_factory: PolicyFactory | None = None,
    customers: Sequence[str] = CUSTOMERS,
    seed_events: Sequence[Event] | None = None,
    wrap: Callable[[InMemoryEventStore], EventStore] | None = None,
    budget: float | None = None,
    extra_goals: Sequence[str] = (),
    **runtime_kwargs: Any,
) -> World:
    log = InMemoryEventStore()
    log.append(history(customers) if seed_events is None else seed_events)
    events: EventStore = wrap(log) if wrap is not None else log
    workspace = InMemoryWorkspace()
    for goal_id in (GOAL, *extra_goals):
        _put_goal(workspace, goal_id, autonomy, budget)
    states, decisions, queue = InMemoryStateStore(), InMemoryDecisionLog(), InMemoryJobQueue()
    adapter = FakeChannelAdapter(Channel.EMAIL)
    dispatcher = Dispatcher(
        {Channel.EMAIL: adapter}, lambda cid, ch: f"{cid}@example.com", workspace, events, queue
    )
    chosen: Policy = policy if policy is not None else ContactEveryonePolicy()
    runtime = AutonomyRuntime(
        events=events,
        states=states,
        decisions=decisions,
        workspace=workspace,
        folder=SimpleStateFolder(),
        policy_factory=policy_factory or (lambda briefs, content: chosen),
        queue=queue,
        dispatcher=dispatcher,
        **runtime_kwargs,
    )
    return World(runtime, log, events, workspace, decisions, states, adapter, queue)


# --- P1 Data Doctor / ingest down ------------------------------------------------------------------


def test_p1_ingest_stalled_runtime_keeps_deciding_on_cached_states() -> None:
    """No new events for two days: the sweep still decides (and acts) on the states it holds."""
    w = build(seed_events=history(seen_at=T0 - timedelta(hours=1)))
    warm = w.runtime.tick(T0)
    assert warm.rebuilt and warm.decisions == []
    later = SWEEP + 2 * DAY  # the feed has been silent since T0 - 1h
    report = w.runtime.tick(later)
    assert report.events_folded == 0
    assert {d.customer_id for d in report.decisions} == set(CUSTOMERS)
    assert len(report.sent) == len(CUSTOMERS)


@pytest.mark.xfail(
    strict=False,
    reason=(
        "The runtime never notices that the event feed has gone quiet: no tick raises an alert when "
        "nothing has been appended for a long time. Fix in autonomy/runtime/service.py (_fold: track "
        "the newest event's arrival and alert 'ingest.stale' past a configurable age). " + GAP
    ),
)
def test_p1_ingest_stalled_raises_a_staleness_alert() -> None:
    w = build(seed_events=history(seen_at=T0 - timedelta(hours=1)))
    w.runtime.tick(T0)
    report = w.runtime.tick(SWEEP + 2 * DAY)
    assert any(a.code.startswith("ingest.") and a.severity != "info" for a in report.alerts)


@pytest.mark.xfail(
    strict=False,
    raises=ConnectionError,
    reason=(
        "An unreachable event feed crashes the whole tick: AutonomyRuntime._fold calls "
        "events.scan() with no guard, so nothing is decided, not even on cached states, and no alert "
        "is stored. Fix in autonomy/runtime/service.py (tick/_fold: catch, keep the watermark, alert "
        "'ingest.unavailable', go on to decide). " + GAP
    ),
)
def test_p1_event_feed_unreachable_keeps_deciding_on_cached_states_and_alerts() -> None:
    feed: dict[str, FlakyFeed] = {}

    def wrap(log: InMemoryEventStore) -> EventStore:
        feed["store"] = FlakyFeed(log)
        return feed["store"]

    w = build(autonomy=AutonomyLevel.SHADOW, wrap=wrap)
    w.runtime.tick(T0)  # warm: every state is built and cached
    feed["store"].down = True
    report = w.runtime.tick(SWEEP)
    assert report.events_folded == 0
    assert {d.customer_id for d in report.decisions} == set(CUSTOMERS)
    assert any(a.code.startswith("ingest.") for a in report.alerts)
    assert {a.alert_id for a in report.alerts} <= {a.alert_id for a in w.runtime.alert_store.list_alerts()}


def test_p1_a_failing_state_enricher_decides_on_plain_states_and_alerts() -> None:
    """Signals derived from the data (another engine's scores) fail: decide on the stored states."""
    _require_hook("state_enricher", "derived signals cannot be injected into the runtime yet.")
    calls: list[int] = []

    def enricher(
        states: list[CustomerState], events_by_customer: Mapping[str, list[Event]], now: datetime
    ) -> list[CustomerState]:
        calls.append(len(states))
        raise TimeoutError("the scoring service timed out")

    w = build(state_enricher=enricher)
    report = w.runtime.tick(SWEEP)
    assert calls, "the enricher was really asked (and failed), not skipped"
    assert {d.customer_id for d in report.decisions} == set(CUSTOMERS)
    assert len(report.sent) == len(CUSTOMERS)
    assert any(
        a.code.startswith("enrich.") and a.goal_id == GOAL and a.severity != "info" for a in report.alerts
    )


# --- P2 mandate store down -------------------------------------------------------------------------


def _mandate(ceiling: float, *, until: datetime) -> Mandate:
    """An active mandate for the win-back goal: email only, every sample template, no quiet hours."""
    return Mandate(
        mandate_id="man_1",
        business_id="demo",
        goal_ids=(GOAL,),
        budget_ceiling=ceiling,
        quiet_hours=QuietHours(start=0, end=0),
        frequency_cap=FrequencyCap(max_touches=5, window_days=7),
        channels=(Channel.EMAIL,),
        approved_template_ids=tuple(f"{GOAL}_ci_{i}" for i in range(5)),
        approved_offer_ids=(TEN_OFF.offer_id,),
        valid_from=T0 - DAY,
        valid_until=until,
        status=MandateStatus.ACTIVE,
    )


def test_p2_the_mandate_already_held_still_governs_and_refuses_budget_expansion() -> None:
    """The enforcement maths needs no store: the mandate in hand keeps capping a bigger brief."""
    mandate = _mandate(100.0, until=SWEEP + 5 * DAY)
    brief = sample_brief(T0, GOAL)
    expanded = brief.envelope.model_copy(update={"budget": 50_000.0})
    envelope = mandate_to_envelope(mandate, (TEN_OFF, FREE_SHIP), base=expanded, spent=40.0)
    assert envelope.budget == pytest.approx(60.0)  # the ceiling less spend, not the brief's 50,000
    assert envelope.channels == (Channel.EMAIL,)

    state = CustomerState(
        customer_id="c1",
        as_of=SWEEP,
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )
    action = Action(goal_id=GOAL, channel=Channel.EMAIL, content_id=f"{GOAL}_ci_0", send_at=SWEEP)
    fits = check_action(
        mandate, action, ActionCosts(channel_cost=1.0), state, SWEEP, spend=SpendToDate(total=40.0)
    )
    assert fits == []
    over = check_action(
        mandate, action, ActionCosts(channel_cost=1.0), state, SWEEP, spend=SpendToDate(total=99.5)
    )
    assert "mandate_budget_ceiling" in over
    # It governs only while unexpired: past valid_until nothing it covers may go.
    after = SWEEP + 6 * DAY
    late = action.model_copy(update={"send_at": after})
    assert "mandate_not_active" in check_action(mandate, late, ActionCosts(channel_cost=1.0), state, after)


class FlakyMandates:
    """The P2 mandate store as the runtime reads it (a `MandateSource`), able to go down.

    It holds mandates by (id, version) and answers with the latest version covering a goal; while
    `down`, every read raises, as an unreachable database or registry would."""

    def __init__(self, *mandates: Mandate) -> None:
        self.mandates = list(mandates)
        self.down = False
        self.reads = 0

    def _read(self) -> None:
        self.reads += 1
        if self.down:
            raise ConnectionError("the mandate store is unreachable")

    def governs(self, goal_id: str) -> bool:
        self._read()
        return any(goal_id in m.goal_ids for m in self.mandates)

    def lookup(self, goal_id: str) -> Mandate | None:
        self._read()
        covering = [m for m in self.mandates if goal_id in m.goal_ids]
        return max(covering, key=lambda m: m.version) if covering else None

    def problems(self, mandate: Mandate, now: datetime) -> list[str]:
        self._read()
        return []


def _expand_budget(w: World, budget: float) -> None:
    """Someone raises the goal's and the brief's budget far past the mandate's ceiling."""
    goal = next(g for g in w.workspace.goals() if g.goal_id == GOAL)
    w.workspace.put_goal(goal.model_copy(update={"budget": budget}))
    brief = max(w.workspace.briefs(GOAL), key=lambda b: b.version)
    w.workspace.put_brief(
        brief.model_copy(
            update={
                "brief_id": f"{brief.brief_id}_v{brief.version + 1}",
                "version": brief.version + 1,
                "envelope": brief.envelope.model_copy(update={"budget": budget}),
            }
        )
    )


MANDATE_HOOK = "the P2 mandate is not wired into the runtime, so there is no mandate store to lose."


def test_p2_the_runtime_mandate_refuses_a_budget_expansion() -> None:
    """With the store up: a goal and brief raised to 50,000 still spend at most the 3.0 ceiling."""
    _require_hook("mandates", MANDATE_HOOK)
    store = FlakyMandates(_mandate(3.0, until=SWEEP + 5 * DAY))
    w = build(channel_costs={Channel.EMAIL: 1.0}, mandates=store)
    _expand_budget(w, 50_000.0)
    first = w.runtime.tick(SWEEP)
    w.runtime.tick(SWEEP + DAY)
    assert store.reads > 0
    assert 0 < len(w.sends()) <= 3
    # It is the mandate that held the fourth customer back (without one, all four go on day one).
    assert first.mandate_blocked and any(a.code == "mandate.violation" for a in first.alerts)


@pytest.mark.xfail(
    strict=False,
    reason=(
        "The runtime's mandate wiring fails closed on a store outage: AutonomyRuntime._mandate_for "
        "catches the error and halts the goal ('mandate.error'), instead of keeping the last "
        "unexpired mandate it read and refusing only what that mandate refuses. Fix in "
        "autonomy/runtime/service.py (_mandate_for/_mandate_state: cache the last good mandate per "
        "goal and use it, with a warning, while it is unexpired). " + GAP
    ),
)
def test_p2_mandate_store_down_keeps_the_last_unexpired_mandate_and_refuses_budget_expansion() -> None:
    _require_hook("mandates", MANDATE_HOOK)
    store = FlakyMandates(_mandate(3.0, until=SWEEP + 5 * DAY))
    w = build(channel_costs={Channel.EMAIL: 1.0}, mandates=store)
    w.runtime.tick(SWEEP)
    sent_first = len(w.sends())
    assert 0 < sent_first <= 3

    store.down = True
    _expand_budget(w, 50_000.0)
    day2 = SWEEP + DAY
    second = w.runtime.tick(day2)  # must not raise
    assert any(a.code.startswith("mandate.") for a in second.alerts), "a person is told the store is down"
    assert GOAL not in second.halted_goals and second.decisions, "the goal keeps running"
    assert len(w.sends()) <= 3  # the expansion is refused: the held mandate's ceiling still binds

    before = len(w.sends())
    w.runtime.tick(SWEEP + 6 * DAY)
    assert len(w.sends()) == before  # past valid_until the held mandate governs nothing


# --- P3 decision policy raising --------------------------------------------------------------------


def test_p3_policy_raising_sends_nothing_unvetted_alerts_and_spares_other_goals() -> None:
    broken, working = RaisingPolicy(), ContactEveryonePolicy()

    def factory(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        return broken if briefs[0].goal_id == BROKEN else working

    w = build(policy_factory=factory, extra_goals=(BROKEN,))
    report = w.runtime.tick(SWEEP)  # must not raise
    assert not any(d.action is not None and d.action.goal_id == BROKEN for d in report.decisions)
    assert not any(e.attrs.get("goal_id") == BROKEN for e in w.sends())
    assert any(a.code == "decide.error" and a.goal_id == BROKEN for a in report.alerts)
    # The other goal is not held hostage by the broken one.
    assert {e.customer_id for e in w.sends() if e.attrs.get("goal_id") == GOAL} == set(CUSTOMERS)


def test_p3_policy_raising_for_one_customer_spares_the_rest() -> None:
    w = build(policy=RaisingPolicy(only=("c2",)))
    report = w.runtime.tick(SWEEP)
    assert {e.customer_id for e in w.sends()} == set(CUSTOMERS) - {"c2"}
    assert any(a.code == "decide.error" for a in report.alerts)


@pytest.mark.xfail(
    strict=False,
    reason=(
        "A customer whose decision raised is skipped with no decision logged, so the proof and the "
        "off-policy log lose that moment, and a static-rule ranking is never tried. Expected: a "
        "DO_NOTHING decision (probability 1, reason naming the failure) per affected customer. Fix "
        "in autonomy/runtime/service.py (_decide_goal's except branch). " + GAP
    ),
)
def test_p3_policy_raising_logs_do_nothing_for_that_tick() -> None:
    w = build(policy=RaisingPolicy())
    report = w.runtime.tick(SWEEP)
    logged = list(w.decisions.scan())
    assert {d.customer_id for d in logged} == set(CUSTOMERS)
    assert all(d.kind is DecisionKind.NOTHING and d.probability == 1.0 for d in logged)
    assert {d.decision_id for d in report.decisions} == {d.decision_id for d in logged}
    assert w.adapter.sent == []


@pytest.mark.xfail(
    strict=False,
    raises=RuntimeError,
    reason=(
        "A policy that cannot be built crashes the whole tick: AutonomyRuntime._running calls "
        "policy_factory() with no guard, so every other goal stops too. Expected: that goal is "
        "skipped for the tick with a 'decide.error' alert. Fix in autonomy/runtime/service.py "
        "(_running). " + GAP
    ),
)
def test_p3_policy_that_cannot_be_built_is_isolated_to_its_goal() -> None:
    def factory(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        if briefs[0].goal_id == BROKEN:
            raise RuntimeError("the model bundle is corrupt")
        return ContactEveryonePolicy()

    w = build(policy_factory=factory, extra_goals=(BROKEN,))
    report = w.runtime.tick(SWEEP)
    assert {e.customer_id for e in w.sends()} == set(CUSTOMERS)
    assert any(a.goal_id == BROKEN and a.severity != "info" for a in report.alerts)


# --- P4 LLM unavailable ----------------------------------------------------------------------------

FACTS = BusinessFacts(
    business_id="demo",
    name="Demo Shop",
    channels=(Channel.EMAIL, Channel.WHATSAPP),
    offers=(TEN_OFF, FREE_SHIP),
    max_contacts_per_week=2,
)


def _proof() -> ProofSummary:
    return ProofSummary(
        goal_id=GOAL,
        as_of=SWEEP,
        agent_n=800,
        baseline_n=100,
        holdout_n=100,
        incremental_revenue=Estimate(point=410.0, low=150.0, high=680.0),
        incremental_profit=None,
        lift_vs_baseline=None,
        spend=82.5,
    )


def test_p4_strategist_falls_back_to_the_template_brief_and_keeps_the_plan() -> None:
    llm = DownLLM()
    goal = sample_goal(T0, GOAL)
    drafted = Strategist(llm).draft_brief(goal, business_facts=FACTS, now=T0)
    assert llm.calls >= 1
    assert not drafted.approved  # a person still approves it
    assert drafted.audience.filters == TEMPLATES[GoalKind.WIN_BACK].filters
    assert "could not be reached" in drafted.rationale

    approved = sample_brief(T0, GOAL)
    report = Briefing(briefing_id="brf_1", goal_id=GOAL, as_of=SWEEP, period="weekly", headline="Quiet week")
    adjusted = Strategist(llm).adjust_brief(approved, report, SWEEP)
    assert adjusted.version == approved.version + 1
    assert adjusted.envelope == approved.envelope and adjusted.audience == approved.audience
    assert "No change this week" in adjusted.rationale


@pytest.mark.xfail(
    strict=False,
    raises=LLMError,
    reason=(
        "Analyst.write_briefing falls back to its code-written template only for unreadable answers "
        "(AgentOutputError); an unreachable model (LLMError) propagates. Fix in "
        "autonomy/agents/analyst.py (catch LLMError beside AgentOutputError). " + GAP
    ),
)
def test_p4_analyst_falls_back_to_the_template_briefing() -> None:
    result = Analyst(DownLLM()).write_briefing(
        sample_goal(T0, GOAL), _proof(), HealthStatus(as_of=SWEEP), {}, "weekly", SWEEP
    )
    assert result.source == "template"
    assert result.briefing.sections  # every number still comes from code


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Creative.build_library has no template copy: an unreachable model (LLMError) propagates, "
        "and an unreadable answer leaves its cell empty. Expected: fixed, pre-approved template copy "
        "per (channel, offer, theme) cell, still DRAFT so the Critic gates it. Fix in "
        "autonomy/agents/creative.py (_cell). " + GAP
    ),
)
def test_p4_creative_falls_back_to_template_copy() -> None:
    creative = Creative(DownLLM())
    items = creative.build_library(sample_brief(T0, GOAL), brand_voice="warm and plain", now=T0)
    assert items, "a library must still come out"
    assert all(i.status is ContentStatus.DRAFT for i in items)  # nothing skips the Critic
    assert {i.channel for i in items} == set(sample_brief(T0, GOAL).envelope.channels)


def _draft(item: ContentItem, body: str) -> ContentItem:
    return item.model_copy(update={"status": ContentStatus.DRAFT, "body": body, "critic_notes": ()})


def test_p4_critic_rules_still_gate_while_its_judge_is_down() -> None:
    """The Critic's rules run before any model call, so rule-breaking copy is rejected regardless."""
    llm = DownLLM()
    item = _draft(sample_content(T0, GOAL)[0], "New in {{top_category}}. Buy now!")  # no unsubscribe hint
    [reviewed] = Critic(llm).review([item], BrandRules.from_brief(sample_brief(T0, GOAL), brand_voice="warm"))
    assert reviewed.status is ContentStatus.REJECTED
    assert any(n.startswith("rule:") for n in reviewed.critic_notes)
    assert llm.calls == 0


@pytest.mark.xfail(
    strict=False,
    raises=LLMError,
    reason=(
        "Critic.review catches only AgentOutputError from its judge; an unreachable judge "
        "(LLMError) propagates out of review(), which fails closed but takes the whole review with "
        "it. Expected: the item is held (REJECTED, or left DRAFT) with a note, never APPROVED. Fix "
        "in autonomy/agents/critic.py (_review_one). " + GAP
    ),
)
def test_p4_critic_holds_rule_passing_copy_while_its_judge_is_down() -> None:
    item = _draft(
        sample_content(T0, GOAL)[0], f"New in {{{{top_category}}}} since your visit.\n\n{EMAIL_FOOTER}"
    )
    rules = BrandRules.from_brief(sample_brief(T0, GOAL), brand_voice="warm")
    [reviewed] = Critic(DownLLM()).review([item], rules)
    assert reviewed.status is not ContentStatus.APPROVED


def test_p4_runtime_weekly_review_survives_an_llm_outage_and_keeps_sending() -> None:
    """Analyst and Strategist both on a dead model: the plan stays, a person is told, sends go on.

    Today the Analyst raises, so the review is refused (`weekly.refused`). Once it falls back to
    its template briefing (see the Analyst xfail), the review completes and the Strategist's
    fallback stores an unchanged next version. Both keep the approved plan, so both pass here."""
    llm = DownLLM()

    def analyst(goal: Goal, brief: Brief, now: datetime) -> Briefing:
        return Analyst(llm).briefing(goal, _proof(), HealthStatus(as_of=now), {}, "weekly", now)

    w = build(analyst=analyst, strategist=Strategist(llm).adjust_brief)
    [approved] = w.workspace.briefs(GOAL)
    report = w.runtime.tick(SWEEP)  # Monday after 07:00: the weekly review is due, and the sweep
    assert llm.calls >= 1
    latest = max(w.workspace.briefs(GOAL), key=lambda b: b.version)
    assert latest.envelope == approved.envelope and latest.audience == approved.audience
    assert latest.approved_by == approved.approved_by
    if report.weekly:
        [outcome] = report.weekly
        assert "No change this week" in outcome.brief.rationale
    else:
        assert any(a.code == "weekly.refused" and a.goal_id == GOAL for a in report.alerts)
        assert latest.version == approved.version
    assert len(report.sent) == len(CUSTOMERS)


# --- P5 provider down ------------------------------------------------------------------------------


@dataclass
class GatewayWorld:
    gateway: DispatchGateway
    queue: InMemoryJobQueue
    events: InMemoryEventStore
    decisions: InMemoryDecisionLog
    states: InMemoryStateStore
    decision: Decision
    state: CustomerState

    def send_events(self) -> list[Event]:
        return [e for e in self.events.scan() if e.type is EventType.SEND]

    def retry(self, now: datetime) -> list[Any]:
        """Claim and run the one due decision job, as a worker would; return its results."""
        job = self.queue.claim([GATEWAY_DECISION_JOB], worker="test", now=now)
        assert job is not None, f"no retry was due at {now.isoformat()}"
        report = self.gateway.run_decision_job(job, self.decisions, self.states, now)
        self.queue.complete(job.job_id)
        return list(report.results)


def gateway_world(
    *carriers: Carrier, at: datetime, telemetry: Callable[[TelemetryEvent], None] | None = None
) -> GatewayWorld:
    ws = InMemoryWorkspace()
    _put_goal(ws, GOAL, AutonomyLevel.AUTOPILOT, None)
    events, queue = InMemoryEventStore(), InMemoryJobQueue()
    providers = [Provider(f"carrier{i}", c) for i, c in enumerate(carriers)]
    gw = DispatchGateway({Channel.EMAIL: providers}, queue=queue, events=events, telemetry=telemetry)
    gw.attach_dispatcher(lambda cid, _ch: f"{cid}@example.com", ws, events)
    state = CustomerState(
        customer_id="c1",
        as_of=at,
        timezone="UTC",  # the fallback rules: promotional quiet hours 21:00-09:00 in the recipient's zone
        top_categories=("shoes",),
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )
    decision = Decision(
        decision_id="dec_c1",
        customer_id="c1",
        made_at=at,
        kind=DecisionKind.ACT,
        action=Action(goal_id=GOAL, channel=Channel.EMAIL, content_id=f"{GOAL}_ci_0", send_at=at),
        probability=0.9,
        reason="test",
        policy_version="t",
    )
    decisions, states = InMemoryDecisionLog(), InMemoryStateStore()
    decisions.record([decision])
    states.put([state])
    return GatewayWorld(gw, queue, events, decisions, states, decision, state)


def _wait(result: Any, queue: InMemoryJobQueue, now: datetime) -> timedelta:
    job: Job | None = queue.get(result.job_id)
    assert job is not None
    return job.run_at - now


def test_p5_provider_down_buffers_with_backoff_and_sends_exactly_once() -> None:
    now = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    carrier = Carrier()
    g = gateway_world(carrier, at=now)

    [first] = g.gateway.dispatch([g.decision], {"c1": g.state}, now).results
    assert first.status is DispatchStatus.QUEUED and first.reason == "provider_error"
    wait1 = _wait(first, g.queue, now)
    assert timedelta(0) < wait1 <= timedelta(minutes=1)
    assert g.gateway.run_due(now + wait1 / 2, decisions=g.decisions, states=g.states) == 0  # not due yet

    t1 = now + wait1
    [second] = g.retry(t1)  # still down: buffered again, waiting longer
    assert second.status is DispatchStatus.QUEUED and second.reason == "provider_error"
    wait2 = _wait(second, g.queue, t1)
    assert wait2 > wait1

    carrier.up = True
    t2 = t1 + wait2
    [third] = g.retry(t2)
    assert third.status is DispatchStatus.SENT
    assert len(carrier.attempts) == 3 and len(carrier.delivered) == 1
    assert [e.decision_id for e in g.send_events()] == ["dec_c1"]

    # Nothing is double-sent: a replayed dispatch and later worker passes deliver nothing new.
    [again] = g.gateway.dispatch([g.decision], {"c1": g.state}, t2 + timedelta(minutes=5)).results
    assert again.status is DispatchStatus.SKIPPED and again.reason == SkipReason.ALREADY_SENT.value
    g.gateway.run_due(t2 + DAY, decisions=g.decisions, states=g.states)
    assert len(carrier.attempts) == 3, "the carrier was not called again"
    assert len(carrier.accepted) == 1 and len(g.send_events()) == 1


def test_p5_quiet_hours_are_rechecked_when_a_buffered_send_retries() -> None:
    """Down at 20:59; the retry lands after 21:00, so it waits for 09:00 instead of sending."""
    now = datetime(2026, 3, 2, 20, 59, tzinfo=UTC)
    carrier = Carrier()
    g = gateway_world(carrier, at=now)
    [first] = g.gateway.dispatch([g.decision], {"c1": g.state}, now).results
    assert first.status is DispatchStatus.QUEUED and first.reason == "provider_error"
    assert _wait(first, g.queue, now) < timedelta(minutes=1)

    carrier.up = True
    quiet = datetime(2026, 3, 2, 21, 0, 30, tzinfo=UTC)
    [held] = g.retry(quiet)
    assert held.status is DispatchStatus.QUEUED and held.reason == "quiet_hours"
    morning = datetime(2026, 3, 3, 9, 0, tzinfo=UTC)
    assert _wait(held, g.queue, quiet) == morning - quiet
    assert len(carrier.attempts) == 1 and carrier.delivered == []  # nothing went out in quiet hours

    [sent] = g.retry(morning)
    assert sent.status is DispatchStatus.SENT
    assert len(carrier.accepted) == 1 and len(g.send_events()) == 1


def test_p5_an_ambiguous_timeout_is_retried_on_the_same_carrier_never_failed_over() -> None:
    """A read timeout may have delivered: the backup carrier is not tried, so no second text lands."""
    now = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    primary, backup = Carrier(timeout=True), Carrier()
    backup.up = True
    g = gateway_world(primary, backup, at=now)
    [first] = g.gateway.dispatch([g.decision], {"c1": g.state}, now).results
    assert first.status is DispatchStatus.QUEUED
    assert backup.attempts == []

    primary.up = True
    [sent] = g.retry(now + _wait(first, g.queue, now))
    assert sent.status is DispatchStatus.SENT
    assert backup.attempts == [] and len(primary.accepted) == 1
    assert len(g.send_events()) == 1


def test_p5_a_definite_rejection_fails_over_to_the_backup_carrier_at_once() -> None:
    now = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    primary, backup = Carrier(), Carrier()
    backup.up = True
    g = gateway_world(primary, backup, at=now)
    [result] = g.gateway.dispatch([g.decision], {"c1": g.state}, now).results
    assert result.status is DispatchStatus.SENT
    assert len(primary.attempts) == 1 and primary.delivered == [] and len(backup.accepted) == 1
    assert g.queue.claim([GATEWAY_DECISION_JOB], worker="t", now=now + DAY) is None


GATEWAY_HOOK = (
    "the runtime dispatches through the plain Dispatcher, so a provider outage on an immediate send "
    "becomes a FAIL event nothing retries, and a retried queued send is not re-gated for quiet hours."
)


def _gateway_runtime(
    carrier: Carrier, config: RuntimeConfig | None = None
) -> tuple[AutonomyRuntime, InMemoryEventStore]:
    log, ws, queue = InMemoryEventStore(), InMemoryWorkspace(), InMemoryJobQueue()
    log.append(history())
    _put_goal(ws, GOAL, AutonomyLevel.AUTOPILOT, None)
    gw = DispatchGateway({Channel.EMAIL: [Provider("ses", carrier)]}, queue=queue, events=log)
    gw.attach_dispatcher(lambda cid, _ch: f"{cid}@example.com", ws, log)
    runtime = AutonomyRuntime(
        events=log,
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        workspace=ws,
        folder=SimpleStateFolder(),
        policy_factory=lambda briefs, content: ContactEveryonePolicy(),
        queue=queue,
        config=config,
        gateway=gw,
    )
    return runtime, log


def test_p5_runtime_buffers_a_provider_outage_through_the_gateway() -> None:
    _require_hook("gateway", GATEWAY_HOOK)
    carrier = Carrier()
    runtime, log = _gateway_runtime(carrier)
    down = runtime.tick(SWEEP)
    assert down.sent == [] and carrier.delivered == [] and len(carrier.attempts) == len(CUSTOMERS)
    carrier.up = True
    runtime.tick(SWEEP + timedelta(minutes=5))  # past the first backoff
    runtime.tick(SWEEP + timedelta(minutes=10))
    assert sorted(m.customer_id for m in carrier.accepted) == sorted(CUSTOMERS)
    assert len([e for e in log.scan() if e.type is EventType.SEND]) == len(CUSTOMERS)


def test_p5_runtime_rechecks_quiet_hours_when_a_buffered_send_retries() -> None:
    """The sweep at 20:59 hits a dead carrier; the retry at 21:00 waits for 09:00, then sends once."""
    _require_hook("gateway", GATEWAY_HOOK)
    carrier = Carrier()
    runtime, log = _gateway_runtime(carrier, RuntimeConfig(sweep_hour=20))
    runtime.tick(T0.replace(hour=20, minute=59))
    assert carrier.delivered == []
    carrier.up = True
    runtime.tick(T0.replace(hour=21, minute=0, second=30))
    runtime.tick(T0.replace(hour=23))
    assert carrier.delivered == [], "nothing goes out in quiet hours, retry or not"
    runtime.tick(T0.replace(hour=9) + DAY)
    runtime.tick(T0.replace(hour=9, minute=30) + DAY)
    assert sorted(m.customer_id for m in carrier.accepted) == sorted(CUSTOMERS)
    assert len([e for e in log.scan() if e.type is EventType.SEND]) == len(CUSTOMERS)


# --- P6 proof / telemetry lagging ------------------------------------------------------------------

SPLIT_SALT = "fail-iso"
EIGHT = tuple(f"c{i}" for i in range(1, 9))


def test_p6_without_outcome_telemetry_dispatch_stays_within_budget_and_respects_holdout() -> None:
    """No delivery, click or purchase event ever arrives. Spend is counted from our own SENDs, so the
    budget still binds; the holdout is kept by the runtime and dispatcher, not by the policy."""
    holdout = {c for c in EIGHT if assign_group(c, SPLIT_SALT, 0.5, 0.0) is Group.HOLDOUT}
    assert holdout and len(holdout) < len(EIGHT)
    policy = BudgetedRogue(cost=1.0)
    w = build(
        policy=policy,
        customers=EIGHT,
        budget=3.0,
        channel_costs={Channel.EMAIL: 1.0},
        assign_group=group_assigner(SPLIT_SALT, holdout_share=0.5, baseline_share=0.0),
    )
    sent_before = 0
    for day in range(3):
        now = SWEEP + day * DAY
        report = w.runtime.tick(now)
        # The budget the policy is handed is the budget less our own sends: no telemetry needed.
        assert policy.seen[now] == pytest.approx(3.0 - sent_before)
        sent_before = len(w.sends())
        assert sent_before <= 3
        assert not any(e.customer_id in holdout for e in w.sends())
        if day == 0:
            assert report.sent, "dispatch continues without telemetry"
            # The policy did choose to contact holdout customers; the dispatcher refused each one.
            assert holdout & {d.customer_id for d in report.acts}
            assert report.dispatch.skip_reasons().get(SkipReason.HOLDOUT.value, 0) >= 1
    assert not any(
        e.type in (EventType.DELIVER, EventType.CLICK, EventType.PURCHASE) for e in w.log.scan() if e.ts > T0
    )


def test_p6_a_lagging_proof_report_does_not_stop_dispatch() -> None:
    def analyst(goal: Goal, brief: Brief, now: datetime) -> Briefing:
        raise TimeoutError("the incrementality ledger has not caught up")

    w = build(analyst=analyst, strategist=Strategist(DownLLM()).adjust_brief)
    report = w.runtime.tick(SWEEP)
    assert any(a.code == "weekly.refused" for a in report.alerts)
    assert len(report.sent) == len(CUSTOMERS)


@pytest.mark.xfail(
    strict=False,
    reason=(
        "A telemetry sink that raises breaks sending: DispatchGateway._emit calls the sink with no "
        "guard inside the FailoverAdapter, so a message the carrier accepted is logged as a FAIL "
        "(and retried), and gateway.dispatch() then raises. Telemetry must never stop or distort a "
        "send. Fix in autonomy/channels/gateway.py (_emit: catch and count sink errors). " + GAP
    ),
)
def test_p6_a_failing_telemetry_sink_does_not_stop_or_distort_a_send() -> None:
    def sink(event: TelemetryEvent) -> None:
        raise ConnectionError("the telemetry collector is unreachable")

    now = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    carrier = Carrier()
    carrier.up = True
    g = gateway_world(carrier, at=now, telemetry=sink)
    report = g.gateway.dispatch([g.decision], {"c1": g.state}, now)
    assert [r.status for r in report.results] == [DispatchStatus.SENT]
    assert len(carrier.accepted) == 1 and len(g.send_events()) == 1
    assert not any(e.type is EventType.FAIL for e in g.events.scan())


@pytest.mark.xfail(
    strict=False,
    raises=ConnectionError,
    reason=(
        "A spend source that raises crashes the tick: AutonomyRuntime._budget_left calls spend_fn "
        "with no guard. Expected: the goal keeps deciding with the last budget it knew (never more), "
        "and a 'spend.unavailable' alert is raised. Fix in autonomy/runtime/service.py "
        "(_budget_left). " + GAP
    ),
)
def test_p6_spend_source_down_keeps_the_last_known_budget() -> None:
    feed = {"down": False}

    def spend(goal_id: str, now: datetime) -> float:
        if feed["down"]:
            raise ConnectionError("the spend ledger is unreachable")
        return 1.0

    spend_fn: SpendFn = spend
    policy = BudgetedRogue(cost=1.0)
    w = build(policy=policy, budget=3.0, spend_fn=spend_fn)
    w.runtime.tick(SWEEP)
    assert policy.seen[SWEEP] == pytest.approx(2.0)
    feed["down"] = True
    day2 = SWEEP + DAY
    report = w.runtime.tick(day2)
    assert day2 in policy.seen and policy.seen[day2] <= 2.0
    assert any("spend" in a.code for a in report.alerts)
