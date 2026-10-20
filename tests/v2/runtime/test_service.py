"""The runtime's tick: each autonomy level does exactly what it promises, and repeated ticks are safe."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.agents.orchestrator import AdjustFn, AnalystFn
from autonomy.channels.breakers import BreakerResult, Guards
from autonomy.channels.dispatcher import Dispatcher
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
    Message,
    Reward,
    SendReceipt,
    stable_id,
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
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.interfaces import Policy
from autonomy.runtime.batches import (
    BatchStore,
    BatchTransitionError,
    InMemoryBatchStore,
    SqlBatchStore,
    UnknownBatchError,
    approve,
    create_batch_tables,
    mark_sent,
    reject,
)
from autonomy.runtime.service import (
    AutonomyRuntime,
    BreakersFn,
    PolicyFactory,
    RuntimeConfig,
    RuntimeCursor,
    make_batch,
    standard_breakers,
)
from autonomy.runtime.settings import RuntimeSettings, build_runtime
from autonomy.store import memory_engine

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # a Monday, before the 10:00 sweep
SWEEP = T0.replace(hour=10)
GOAL = "goal_winback"
CUSTOMERS = ("c1", "c2", "c3", "c4")


def history(customers: Sequence[str] = CUSTOMERS, *, bought: datetime | None = None) -> list[Event]:
    """Each customer consented to email long ago and last bought 90 days before T0 (lapsed)."""
    when = bought or T0 - timedelta(days=90)
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
                ts=when,
                source="test",
                value=40.0,
                category="shoes",
            )
        )
    return out


@dataclass
class World:
    runtime: AutonomyRuntime
    events: InMemoryEventStore
    workspace: InMemoryWorkspace
    decisions: InMemoryDecisionLog
    states: InMemoryStateStore
    adapter: FakeChannelAdapter
    batches: InMemoryBatchStore


def world(
    autonomy: AutonomyLevel,
    *,
    status: GoalStatus = GoalStatus.ACTIVE,
    policy: Policy | None = None,
    breakers_fn: BreakersFn | None = None,
    seed_events: Sequence[Event] | None = None,
    analyst: AnalystFn | None = None,
    strategist: AdjustFn | None = None,
    policy_factory: PolicyFactory | None = None,
    config: RuntimeConfig | None = None,
    adapter: FakeChannelAdapter | None = None,
) -> World:
    events = InMemoryEventStore()
    events.append(history() if seed_events is None else seed_events)
    workspace = InMemoryWorkspace()
    start = T0 - timedelta(days=1)
    workspace.put_goal(sample_goal(start).model_copy(update={"autonomy": autonomy, "status": status}))
    workspace.put_brief(sample_brief(start))
    workspace.put_content(sample_content(start))
    states = InMemoryStateStore()
    decisions = InMemoryDecisionLog()
    adapter = adapter or FakeChannelAdapter(Channel.EMAIL)
    queue = InMemoryJobQueue()  # one queue, as in production: the runtime runs the sends it queues
    dispatcher = Dispatcher(
        {Channel.EMAIL: adapter},
        lambda cid, ch: f"{cid}@example.com",
        workspace,
        events,
        queue,
    )
    batches = InMemoryBatchStore()
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
        batches=batches,
        breakers_fn=breakers_fn,
        analyst=analyst,
        strategist=strategist,
        config=config,
    )
    return World(runtime, events, workspace, decisions, states, adapter, batches)


def sends(w: World) -> list[Event]:
    return [e for e in w.events.scan() if e.type is EventType.SEND]


# --- autonomy levels ----------------------------------------------------------------------------


def test_shadow_decides_and_logs_but_sends_nothing() -> None:
    w = world(AutonomyLevel.SHADOW)
    report = w.runtime.tick(SWEEP)
    assert report.swept_goals == [GOAL]
    assert len(report.decisions) == len(CUSTOMERS)
    assert all(d.kind is DecisionKind.ACT for d in report.decisions)
    assert {d.decision_id for d in w.decisions.scan()} == {d.decision_id for d in report.decisions}
    assert w.adapter.sent == [] and sends(w) == []
    assert report.batches_created == [] and w.batches.list_batches() == []


def test_approve_holds_a_batch_and_sends_only_after_approval() -> None:
    w = world(AutonomyLevel.APPROVE)
    first = w.runtime.tick(SWEEP)
    assert len(first.batches_created) == 1
    batch = first.batches_created[0]
    assert batch.status is BatchStatus.PENDING
    assert batch.n_contacts == len(CUSTOMERS) and len(batch.decision_ids) == len(CUSTOMERS)
    assert "4 customers by email (4)" in batch.summary
    assert w.adapter.sent == []

    later = w.runtime.tick(SWEEP + timedelta(minutes=5))  # nobody approved yet
    assert w.adapter.sent == [] and later.batches_created == []

    approved = w.runtime.approve_batch(batch.batch_id, by="founder", now=SWEEP + timedelta(minutes=6))
    assert approved.status is BatchStatus.APPROVED and approved.decided_by == "founder"
    assert w.adapter.sent == []  # approval alone sends nothing; the next tick does

    sent = w.runtime.tick(SWEEP + timedelta(minutes=10))
    assert len(w.adapter.sent) == len(CUSTOMERS)
    assert [b.batch_id for b in sent.batches_sent] == [batch.batch_id]
    stored = w.batches.get(batch.batch_id)
    assert stored is not None and stored.status is BatchStatus.SENT
    assert {e.decision_id for e in sends(w)} == set(batch.decision_ids)

    w.runtime.tick(SWEEP + timedelta(minutes=15))
    assert len(w.adapter.sent) == len(CUSTOMERS)  # never twice


def test_a_rejected_batch_is_never_sent() -> None:
    w = world(AutonomyLevel.APPROVE)
    batch = w.runtime.tick(SWEEP).batches_created[0]
    w.runtime.reject_batch(batch.batch_id, by="founder", now=SWEEP)
    w.runtime.tick(SWEEP + timedelta(minutes=5))
    w.runtime.tick(SWEEP + timedelta(days=1))
    assert batch.batch_id not in {e.decision_id for e in sends(w)}
    assert not any(e.decision_id in batch.decision_ids for e in sends(w))


def test_an_approved_batch_waits_while_its_channel_is_paused() -> None:
    paused = {"email": False}

    def trip(briefs: Sequence[Brief], recent: Sequence[Event], now: datetime) -> BreakerResult:
        channels = {Channel.EMAIL} if paused["email"] else set()
        return BreakerResult(alerts=[], paused_goals=set(), paused_channels=channels)

    w = world(AutonomyLevel.APPROVE, breakers_fn=trip)
    batch = w.runtime.tick(SWEEP).batches_created[0]
    w.runtime.approve_batch(batch.batch_id, by="founder", now=SWEEP)
    paused["email"] = True
    held = w.runtime.tick(SWEEP + timedelta(minutes=5))
    assert held.batches_sent == [] and w.adapter.sent == []
    stored = w.batches.get(batch.batch_id)
    assert stored is not None and stored.status is BatchStatus.APPROVED  # not marked sent unsent

    paused["email"] = False
    w.runtime.resume_channels([Channel.EMAIL])
    sent = w.runtime.tick(SWEEP + timedelta(minutes=10))
    assert [b.batch_id for b in sent.batches_sent] == [batch.batch_id]
    assert len(w.adapter.sent) == len(CUSTOMERS)


class FlakyAdapter(FakeChannelAdapter):
    """Refuses the first `failures` messages, then accepts (and records) the rest."""

    def __init__(self, channel: Channel, failures: int) -> None:
        super().__init__(channel)
        self.failures = failures

    def send(self, message: Message) -> SendReceipt:
        if self.failures > 0:
            self.failures -= 1
            return SendReceipt(accepted=False, error="throttled")
        return super().send(message)


@dataclass
class SendsInAnHour:
    version: str = "later-v0"

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        d = ContactEveryonePolicy().decide(state, ctx)
        if d.action is None:
            return d
        later = d.action.model_copy(update={"send_at": ctx.now + timedelta(hours=1)})
        return d.model_copy(update={"action": later})

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


def test_a_queued_send_that_fails_is_retried() -> None:
    w = world(
        AutonomyLevel.AUTOPILOT, policy=SendsInAnHour(), adapter=FlakyAdapter(Channel.EMAIL, failures=1)
    )
    first = w.runtime.tick(SWEEP)
    assert len(first.dispatch.queued) == len(CUSTOMERS) and w.adapter.sent == []
    due = w.runtime.tick(SWEEP + timedelta(hours=1))
    assert len(due.dispatch.failed) == 1 and len(due.sent) == len(CUSTOMERS) - 1
    retried = w.runtime.tick(SWEEP + timedelta(hours=1, minutes=2))  # the first retry is a minute later
    assert len(retried.sent) == 1
    assert len(w.adapter.sent) == len(CUSTOMERS)


def test_an_unapproved_batch_expires() -> None:
    w = world(AutonomyLevel.APPROVE)
    batch = w.runtime.tick(SWEEP).batches_created[0]
    report = w.runtime.tick(SWEEP + timedelta(days=4))
    assert batch.batch_id in {b.batch_id for b in report.batches_expired}
    stored = w.batches.get(batch.batch_id)
    assert stored is not None and stored.status is BatchStatus.REJECTED
    assert any(a.code == "batch.expired" for a in report.alerts)


def test_autopilot_sends_and_state_sees_the_contact() -> None:
    w = world(AutonomyLevel.AUTOPILOT)
    report = w.runtime.tick(SWEEP)
    assert len(report.sent) == len(CUSTOMERS)
    assert len(w.adapter.sent) == len(CUSTOMERS)
    assert {e.decision_id for e in sends(w)} == {d.decision_id for d in report.decisions}
    state = w.states.get("c1")
    assert state is not None and state.contacts_7d == 1  # folded in the same tick


@pytest.mark.parametrize("status", [GoalStatus.PAUSED, GoalStatus.DRAFT, GoalStatus.DONE])
def test_a_goal_that_is_not_active_does_nothing(status: GoalStatus) -> None:
    w = world(AutonomyLevel.AUTOPILOT, status=status)
    report = w.runtime.tick(SWEEP)
    assert report.decisions == [] and w.adapter.sent == [] and report.batches_created == []


def test_an_unapproved_brief_does_nothing() -> None:
    w = world(AutonomyLevel.AUTOPILOT)
    brief = sample_brief(T0)
    w.workspace.put_brief(brief.model_copy(update={"version": 2, "approved_at": None, "approved_by": None}))
    assert w.runtime.tick(SWEEP).decisions == []


# --- breakers ----------------------------------------------------------------------------------


@dataclass
class SeesContacts:
    """Contacts everyone, and remembers how many recent contacts each customer had when decided."""

    version: str = "sees-v0"
    seen: list[tuple[str, int]] = field(default_factory=list)

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        self.seen.append((ctx.briefs[0].goal_id, state.contacts_7d))
        return ContactEveryonePolicy().decide(state, ctx)

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


@pytest.mark.parametrize("one_contact", [True, False])
def test_two_goals_in_one_tick(one_contact: bool) -> None:
    policy = SeesContacts()
    w = world(
        AutonomyLevel.AUTOPILOT,
        policy=policy,
        config=RuntimeConfig(one_contact_per_tick=one_contact),
    )
    start = T0 - timedelta(days=1)
    other = "goal_other"  # sorts first, so it decides first
    w.workspace.put_goal(
        sample_goal(start).model_copy(
            update={"goal_id": other, "autonomy": AutonomyLevel.AUTOPILOT, "status": GoalStatus.ACTIVE}
        )
    )
    w.workspace.put_brief(
        sample_brief(start).model_copy(update={"goal_id": other, "brief_id": "brief_other"})
    )
    w.workspace.put_content(
        c.model_copy(update={"goal_id": other, "content_id": f"{c.content_id}_other"})
        for c in sample_content(start)
    )
    report = w.runtime.tick(SWEEP)
    n = len(CUSTOMERS)
    assert report.dispatch.skip_reasons() == {}
    if one_contact:
        assert policy.seen == [(other, 0)] * n and len(w.adapter.sent) == n
    else:
        # The second goal sees the first goal's send in the state it decides on.
        assert policy.seen == [(other, 0)] * n + [(GOAL, 1)] * n and len(w.adapter.sent) == 2 * n


def test_a_breaker_trip_pauses_the_goal_and_stops_sending() -> None:
    w = world(
        AutonomyLevel.AUTOPILOT,
        breakers_fn=standard_breakers(guards=Guards(min_deliveries=1, min_count=1)),
    )
    first = w.runtime.tick(SWEEP)
    assert len(first.sent) == len(CUSTOMERS)
    harm: list[Event] = []
    for i, send in enumerate(sends(w)):
        assert send.channel is not None
        harm.append(send.model_copy(update={"event_id": f"dlv_{i}", "type": EventType.DELIVER}))
        harm.append(send.model_copy(update={"event_id": f"cmp_{i}", "type": EventType.COMPLAINT}))
    w.events.append(harm)

    report = w.runtime.tick(SWEEP + timedelta(days=1))
    assert report.paused_goals == [GOAL]
    assert any(a.code == "breaker.complaint_rate" for a in report.alerts)
    goal = next(g for g in w.workspace.goals() if g.goal_id == GOAL)
    assert goal.status is GoalStatus.PAUSED
    assert report.decisions == [] and len(w.adapter.sent) == len(CUSTOMERS)


def test_a_breaker_can_pause_a_channel() -> None:
    def trip(briefs: Sequence[Brief], recent: Sequence[Event], now: datetime) -> BreakerResult:
        alert = Alert(
            alert_id="a1",
            at=now,
            severity="critical",
            code="breaker.delivery_rate_below",
            message="Too many bounces. Paused email.",
            auto_paused=True,
        )
        return BreakerResult(alerts=[alert], paused_goals=set(), paused_channels={Channel.EMAIL})

    w = world(AutonomyLevel.AUTOPILOT, breakers_fn=trip)
    report = w.runtime.tick(SWEEP)
    assert report.paused_channels == [Channel.EMAIL]
    assert w.adapter.sent == []
    assert report.dispatch.skip_reasons() == {"paused_channel": len(CUSTOMERS)}
    again = w.runtime.tick(SWEEP + timedelta(minutes=5))
    assert again.alerts == [] and again.paused_channels == []  # told once, not every tick


# --- idempotency and timing ------------------------------------------------------------------------


def test_ticks_are_idempotent() -> None:
    w = world(AutonomyLevel.AUTOPILOT)
    first = w.runtime.tick(SWEEP)
    again = w.runtime.tick(SWEEP)
    later = w.runtime.tick(SWEEP + timedelta(hours=3))
    assert len(first.decisions) == len(CUSTOMERS)
    assert again.decisions == [] and later.decisions == []
    assert again.sent == [] and later.sent == []
    assert len(w.adapter.sent) == len(CUSTOMERS)
    next_day = w.runtime.tick(SWEEP + timedelta(days=1))
    assert len(next_day.decisions) == len(CUSTOMERS)  # a new day, a new sweep


def test_a_restarted_runtime_does_not_decide_twice() -> None:
    w = world(AutonomyLevel.AUTOPILOT)
    w.runtime.tick(SWEEP)
    fresh = AutonomyRuntime(
        events=w.events,
        states=w.states,
        decisions=w.decisions,
        workspace=w.workspace,
        folder=SimpleStateFolder(),
        policy_factory=lambda briefs, content: ContactEveryonePolicy(),
        dispatcher=w.runtime.dispatcher,
        batches=w.batches,
        cursor=RuntimeCursor.model_validate_json(w.runtime.cursor.model_dump_json()),
    )
    report = fresh.tick(SWEEP + timedelta(minutes=5))
    assert report.rebuilt and report.decisions == []
    state = w.states.get("c1")
    assert state is not None and state.purchases == 1 and state.contacts_7d == 1  # rebuilt, not doubled


def test_a_restart_that_lost_its_cursor_does_not_decide_twice() -> None:
    w = world(AutonomyLevel.AUTOPILOT)
    w.runtime.tick(SWEEP)
    fresh = AutonomyRuntime(
        events=w.events,
        states=w.states,
        decisions=w.decisions,
        workspace=w.workspace,
        folder=SimpleStateFolder(),
        policy_factory=lambda briefs, content: ContactEveryonePolicy(),
        dispatcher=w.runtime.dispatcher,
        batches=w.batches,
    )
    report = fresh.tick(SWEEP + timedelta(minutes=5))
    assert report.swept_goals == [GOAL]  # it does not know it swept, so it sweeps...
    assert report.decisions == [] and len(w.adapter.sent) == len(CUSTOMERS)  # ...and finds it all decided


def test_a_restart_does_not_decide_a_do_nothing_customer_again_for_a_lapse() -> None:
    # c9 lapses at 09:45, between a lapse check at 09:30 and the 10:00 sweep, so the sweep decides
    # it, and the 10:35 lapse check sees it cross too. A NOTHING decision has no action or bids,
    # so only its derived id says which goal it was for.
    c9 = history(["c9"], bought=SWEEP - timedelta(days=60, minutes=15))
    w = world(AutonomyLevel.AUTOPILOT, policy=NothingPolicy(), seed_events=[*history(), *c9])
    w.runtime.tick(T0)
    w.runtime.tick(SWEEP - timedelta(minutes=30))
    assert "c9" in {d.customer_id for d in w.runtime.tick(SWEEP).decisions}
    fresh = AutonomyRuntime(
        events=w.events,
        states=w.states,
        decisions=w.decisions,
        workspace=w.workspace,
        folder=SimpleStateFolder(),
        policy_factory=lambda briefs, content: NothingPolicy(),
        dispatcher=w.runtime.dispatcher,
        batches=w.batches,
        cursor=RuntimeCursor.model_validate_json(w.runtime.cursor.model_dump_json()),
    )
    assert fresh.tick(SWEEP + timedelta(minutes=35)).decisions == []


def test_a_reply_stored_while_the_nightly_tick_runs_still_reaches_the_conversation_agent() -> None:
    night = SWEEP + timedelta(hours=16)  # 02:00 the next day: the nightly rebuild is due
    stop = Event(
        event_id="reply_c1",
        customer_id="c1",
        type=EventType.REPLY,
        ts=night - timedelta(minutes=1),
        source="ses",
        channel=Channel.EMAIL,
        text="STOP",
    )

    def store_a_reply_midway(
        briefs: Sequence[Brief], recent: Sequence[Event], now: datetime
    ) -> BreakerResult:
        if now == night:  # the API stores a webhook while the tick is running
            w.events.append([stop])
        return BreakerResult(alerts=[], paused_goals=set(), paused_channels=set())

    w = world(AutonomyLevel.SHADOW, breakers_fn=store_a_reply_midway)
    w.runtime.tick(SWEEP)
    assert w.runtime.tick(night).rebuilt
    report = w.runtime.tick(night + timedelta(minutes=5))
    assert [r.intent for r in report.replies] == ["stop"]
    state = w.states.get("c1")
    assert state is not None and state.channels[Channel.EMAIL].unsubscribed


def test_nothing_is_decided_before_the_sweep_hour() -> None:
    w = world(AutonomyLevel.AUTOPILOT)
    report = w.runtime.tick(T0)
    assert report.rebuilt and report.decisions == [] and report.swept_goals == []


def test_a_cart_event_triggers_a_decision_at_once() -> None:
    w = world(AutonomyLevel.AUTOPILOT)
    w.runtime.tick(T0)
    w.events.append(
        [
            Event(
                event_id="cart_c2",
                customer_id="c2",
                type=EventType.CART,
                ts=T0 + timedelta(minutes=1),
                source="shop",
            )
        ]
    )
    report = w.runtime.tick(T0 + timedelta(minutes=5))
    assert [d.customer_id for d in report.decisions] == ["c2"]
    assert [m.customer_id for m in w.adapter.sent] == ["c2"]
    sweep = w.runtime.tick(SWEEP)
    assert sorted(d.customer_id for d in sweep.decisions) == ["c1", "c3", "c4"]  # c2 already contacted


def test_a_stop_reply_unsubscribes_before_anyone_is_contacted() -> None:
    w = world(AutonomyLevel.AUTOPILOT)
    w.runtime.tick(T0)
    w.events.append(
        [
            Event(
                event_id="reply_c3",
                customer_id="c3",
                type=EventType.REPLY,
                ts=T0 + timedelta(minutes=2),
                source="ses",
                channel=Channel.EMAIL,
                text="STOP",
            )
        ]
    )
    report = w.runtime.tick(SWEEP)
    assert [r.intent for r in report.replies] == ["stop"]
    state = w.states.get("c3")
    assert state is not None and state.channels[Channel.EMAIL].unsubscribed
    assert "c3" not in {m.customer_id for m in w.adapter.sent}
    assert len(w.adapter.sent) == len(CUSTOMERS) - 1
    assert w.runtime.tick(SWEEP + timedelta(minutes=5)).replies == []  # handled once


def test_crossing_the_lapse_threshold_triggers_after_the_sweep() -> None:
    lapsing = history(["c9"], bought=SWEEP - timedelta(days=60) + timedelta(hours=1))
    w = world(AutonomyLevel.AUTOPILOT, seed_events=[*history(), *lapsing])
    first = w.runtime.tick(SWEEP)
    assert "c9" not in {d.customer_id for d in first.decisions}  # 59 days and 23 hours: not lapsed yet
    report = w.runtime.tick(SWEEP + timedelta(hours=1, minutes=30))
    assert [d.customer_id for d in report.decisions] == ["c9"]


# --- learning loops ----------------------------------------------------------------------------


@dataclass
class LearningPolicy:
    version: str = "learner-v0"
    nightly_calls: list[tuple[int, int, datetime]] = field(default_factory=list)
    inner: Policy = field(default_factory=ContactEveryonePolicy)

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        return self.inner.decide(state, ctx)

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None

    def nightly(
        self, decisions: Sequence[Decision], events_by_customer: Mapping[str, Sequence[Event]], now: datetime
    ) -> None:
        self.nightly_calls.append((len(decisions), len(events_by_customer), now))


def test_nightly_runs_once_a_day() -> None:
    policy = LearningPolicy()
    w = world(AutonomyLevel.AUTOPILOT, policy=policy)
    first = w.runtime.tick(SWEEP)  # past 02:00 already, so today's nightly run catches up after the sweep
    assert first.nightly_ran
    assert not w.runtime.tick(SWEEP + timedelta(hours=1)).nightly_ran
    night = SWEEP + timedelta(hours=16)  # 02:00 the next day
    report = w.runtime.tick(night)
    assert report.nightly_ran and report.rebuilt
    n = len(CUSTOMERS)
    assert policy.nightly_calls == [(n, n, SWEEP), (n, n, night)]
    assert not w.runtime.tick(night + timedelta(minutes=5)).nightly_ran
    assert len(policy.nightly_calls) == 2


def test_nightly_hands_each_goal_only_its_own_decisions() -> None:
    learners: dict[str, LearningPolicy] = {}

    def factory(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        gid = briefs[0].goal_id
        learners.setdefault(gid, LearningPolicy(inner=NothingPolicy()))
        return learners[gid]

    w = world(AutonomyLevel.SHADOW, policy_factory=factory)
    start = T0 - timedelta(days=1)
    other = "goal_other"
    w.workspace.put_goal(
        sample_goal(start).model_copy(
            update={"goal_id": other, "autonomy": AutonomyLevel.SHADOW, "status": GoalStatus.ACTIVE}
        )
    )
    w.workspace.put_brief(
        sample_brief(start).model_copy(update={"goal_id": other, "brief_id": "brief_other"})
    )
    report = w.runtime.tick(SWEEP)
    assert report.nightly_ran and len(report.decisions) == 2 * len(CUSTOMERS)
    n = len(CUSTOMERS)
    assert {gid: p.nightly_calls for gid, p in learners.items()} == {
        GOAL: [(n, 0, SWEEP)],  # shadow sent nothing, and the purchases are older than 30 days
        other: [(n, 0, SWEEP)],
    }


def test_weekly_cycle_adjusts_the_brief_once_a_week() -> None:
    def analyst(goal: Goal, brief: Brief, now: datetime) -> Briefing:
        return Briefing(
            briefing_id=stable_id("br", goal.goal_id, now), as_of=now, period="weekly", headline="ok"
        )

    def strategist(brief: Brief, insights: Briefing, now: datetime) -> Brief:
        return brief.model_copy(
            update={"version": brief.version + 1, "brief_id": f"{brief.brief_id}_next", "created_at": now}
        )

    w = world(AutonomyLevel.SHADOW, analyst=analyst, strategist=strategist)
    w.runtime.tick(SWEEP)  # Monday after 07:00: the goal's brief is from last week, so it is reviewed
    briefs = w.workspace.briefs(GOAL)
    assert [b.version for b in briefs] == [2]
    report = w.runtime.tick(SWEEP + timedelta(days=1))
    assert report.weekly == []
    assert [b.version for b in w.workspace.briefs(GOAL)] == [2]


# --- batches ------------------------------------------------------------------------------------


def _stores() -> list[BatchStore]:
    engine = memory_engine()
    create_batch_tables(engine)
    return [InMemoryBatchStore(), SqlBatchStore(engine)]


@pytest.mark.parametrize("store", _stores(), ids=["memory", "sqlite"])
def test_batch_store_transitions(store: BatchStore) -> None:
    w = world(AutonomyLevel.APPROVE)
    acts = w.runtime.tick(SWEEP).decisions
    a = make_batch("g1", acts[:2], SWEEP)
    b = make_batch("g2", acts[2:], SWEEP + timedelta(minutes=1))
    assert store.put(a) and store.put(b) and not store.put(a)
    assert [x.batch_id for x in store.list_batches()] == [a.batch_id, b.batch_id]
    assert [x.batch_id for x in store.list_batches(goal_id="g2")] == [b.batch_id]

    ok = approve(store, a.batch_id, by="ana", now=SWEEP)
    assert ok.status is BatchStatus.APPROVED and ok.decided_at == SWEEP
    with pytest.raises(BatchTransitionError):
        reject(store, a.batch_id, by="ben", now=SWEEP)
    with pytest.raises(BatchTransitionError):
        approve(store, a.batch_id, by="ana", now=SWEEP)
    sent = mark_sent(store, a.batch_id, now=SWEEP)
    assert sent.status is BatchStatus.SENT and sent.decided_by == "ana"
    with pytest.raises(BatchTransitionError):
        mark_sent(store, b.batch_id, now=SWEEP)  # pending batches are never sent
    assert reject(store, b.batch_id, by="ben", now=SWEEP).status is BatchStatus.REJECTED
    assert [x.batch_id for x in store.list_batches(statuses=[BatchStatus.REJECTED])] == [b.batch_id]
    with pytest.raises(UnknownBatchError):
        approve(store, "nope", by="ana", now=SWEEP)
    with pytest.raises(ValueError):
        approve(store, b.batch_id, by=" ", now=SWEEP)
    got = store.get(a.batch_id)
    assert isinstance(got, Batch) and got == sent


# --- settings and production wiring --------------------------------------------------------------


def test_settings_read_the_environment() -> None:
    s = RuntimeSettings.from_env(
        {
            "AUTONOMY_SWEEP_HOUR": "9",
            "AUTONOMY_TIMEZONE": "Europe/London",
            "AUTONOMY_SES_TOPIC_ARNS": "arn:a, arn:b",
            "AUTONOMY_SOMETHING_ELSE": "ignored",
        }
    )
    assert s.sweep_hour == 9 and s.timezone == "Europe/London" and s.ses_topic_arns == ("arn:a", "arn:b")
    assert s.config() == RuntimeConfig(sweep_hour=9, timezone="Europe/London")
    with pytest.raises(ValueError):
        RuntimeSettings.from_env({"AUTONOMY_TIMEZONE": "Mars/Base"})
    assert RuntimeSettings.secret("TOKEN", {"TOKEN": "abc"}) == "abc"


def test_build_runtime_runs_on_sql_stores() -> None:
    engine = memory_engine()
    adapter = FakeChannelAdapter(Channel.EMAIL)
    runtime = build_runtime(
        RuntimeSettings(database_url="sqlite://"),
        engine=engine,
        policy_factory=lambda briefs, content: ContactEveryonePolicy(),
        adapters={Channel.EMAIL: adapter},
        address_book=lambda cid, ch: f"{cid}@example.com",
    )
    start = T0 - timedelta(days=1)
    runtime.workspace.put_goal(
        sample_goal(start).model_copy(
            update={"autonomy": AutonomyLevel.AUTOPILOT, "status": GoalStatus.ACTIVE}
        )
    )
    runtime.workspace.put_brief(sample_brief(start))
    runtime.workspace.put_content(sample_content(start))
    runtime.events.append(history())

    report = runtime.tick(SWEEP)
    holdout = [d for d in report.decisions if d.group.value == "holdout"]
    assert len(report.decisions) == len(CUSTOMERS)
    assert len(adapter.sent) == len(report.acts) - len([d for d in holdout if d.kind is DecisionKind.ACT])
    assert runtime.tick(SWEEP + timedelta(minutes=5)).decisions == []
    content: list[ContentItem] = runtime.workspace.content(goal_id=GOAL)
    assert content


def test_build_worker_wires_the_runtime_for_the_entry_point() -> None:
    from autonomy.runtime.service import build_worker

    worker = build_worker("w-test", environ={"AUTONOMY_DATABASE_URL": "sqlite://"}, start=T0)
    assert set(worker.kinds) == {"tick", "send_message", "webhook_event"}
    outcome = worker.run_once(T0)  # the seeded tick: no goals yet, so it only folds
    assert outcome is not None and outcome.kind == "tick" and outcome.status == "done"


def test_the_production_policy_factory_passes_each_goal_kind_to_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    from autonomy.fakes import InMemoryWorkspace
    from autonomy.runtime.settings import workspace_policy_factory

    seen: dict[str, object] = {}

    def build_agent_policy(briefs: object, content: object, **kwargs: object) -> Policy:
        seen.update(kwargs)
        return ContactEveryonePolicy()

    agent = types.ModuleType("autonomy.runtime.agent")
    agent.build_agent_policy = build_agent_policy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "autonomy.runtime.agent", agent)
    workspace = InMemoryWorkspace()
    start = T0 - timedelta(days=1)
    goal = sample_goal(start)
    workspace.put_goal(goal)

    workspace_policy_factory(workspace)([sample_brief(start)], sample_content(start))
    assert seen == {"kinds": {goal.goal_id: goal.kind}}
