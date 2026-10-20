"""The runtime's integration wiring: state enricher, mandates, the send-time gateway, negative lift.

Each test drives a real `AutonomyRuntime` tick on in-memory fakes and checks one promise: the
enricher runs before deciding, a mandate violation blocks the send (and says why), a goal with no
standing mandate does not act, the mandate's pacer throttles spend, a gateway refusal or crash is an
alert and never a crash, and the nightly negative-lift breaker pauses a goal that loses money.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.channels.breakers import (
    NEGATIVE_LIFT_CODE,
    Guards,
    LiftGuards,
    evaluate,
    negative_lift,
)
from autonomy.channels.consent import ConsentProof, InMemoryConsentRegistry, evidence_hash
from autonomy.channels.dispatcher import Dispatcher, DispatchReport, DispatchStatus
from autonomy.channels.gateway import DispatchGateway, Provider
from autonomy.contracts import (
    NO_OFFER,
    Action,
    Alert,
    AutonomyLevel,
    Bid,
    Channel,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    GoalStatus,
    Group,
    Job,
    StopMetric,
    StopRule,
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
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.mandate import Mandate, MandateStatus
from autonomy.runtime.service import (
    AutonomyRuntime,
    MandateBook,
    RuntimeConfig,
    StateEnricher,
)
from autonomy.runtime.settings import RuntimeSettings, build_runtime
from autonomy.runtime.worker import SEND_KIND, send_handler
from autonomy.store import memory_engine

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # a Monday, before the 10:00 sweep
SWEEP = T0.replace(hour=10)
GOAL = "goal_winback"
CUSTOMERS = ("c1", "c2", "c3", "c4")
START = T0 - timedelta(days=1)


def history(customers: Sequence[str] = CUSTOMERS) -> list[Event]:
    """Each customer consented to email long ago and last bought 90 days before T0 (lapsed)."""
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
    return out


class RecordingPolicy(ContactEveryonePolicy):
    """Contacts everyone and remembers the states it was asked about."""

    def __init__(self) -> None:
        self.seen: list[CustomerState] = []

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        self.seen.append(state)
        return super().decide(state, ctx)


@dataclass
class World:
    runtime: AutonomyRuntime
    events: InMemoryEventStore
    workspace: InMemoryWorkspace
    decisions: InMemoryDecisionLog
    adapter: FakeChannelAdapter
    policy: RecordingPolicy


def world(
    autonomy: AutonomyLevel = AutonomyLevel.AUTOPILOT,
    *,
    seed_events: Sequence[Event] | None = None,
    goal_start: datetime = START,
    gateway_for: Callable[[InMemoryWorkspace, InMemoryEventStore], DispatchGateway] | None = None,
    **runtime_kw: object,
) -> World:
    events = InMemoryEventStore()
    events.append(history() if seed_events is None else seed_events)
    workspace = InMemoryWorkspace()
    workspace.put_goal(
        sample_goal(goal_start).model_copy(update={"autonomy": autonomy, "status": GoalStatus.ACTIVE})
    )
    workspace.put_brief(sample_brief(goal_start))
    workspace.put_content(sample_content(goal_start))
    decisions = InMemoryDecisionLog()
    adapter = FakeChannelAdapter(Channel.EMAIL)
    queue = InMemoryJobQueue()
    if gateway_for is not None:
        runtime_kw["gateway"] = gateway_for(workspace, events)
    dispatcher: Dispatcher | None = None
    if runtime_kw.get("gateway") is None:
        dispatcher = Dispatcher(
            {Channel.EMAIL: adapter}, lambda cid, ch: f"{cid}@example.com", workspace, events, queue
        )
    policy = RecordingPolicy()
    runtime = AutonomyRuntime(
        events=events,
        states=InMemoryStateStore(),
        decisions=decisions,
        workspace=workspace,
        folder=SimpleStateFolder(),
        policy_factory=lambda briefs, content: policy,
        queue=queue,
        dispatcher=dispatcher,
        **runtime_kw,  # type: ignore[arg-type]
    )
    return World(runtime, events, workspace, decisions, adapter, policy)


def sends(w: World) -> list[Event]:
    return [e for e in w.events.scan() if e.type is EventType.SEND]


def codes(alerts: Sequence[Alert]) -> list[str]:
    return [a.code for a in alerts]


def mandate(**kw: object) -> Mandate:
    base: dict[str, object] = {
        "mandate_id": "m1",
        "business_id": "demo",
        "goal_ids": (GOAL,),
        "budget_ceiling": 1000.0,
        "channels": (Channel.EMAIL, Channel.WHATSAPP),
        "approved_template_ids": tuple(c.content_id for c in sample_content(START)),
        "valid_from": T0 - timedelta(days=10),
        "valid_until": T0 + timedelta(days=30),
        "status": MandateStatus.ACTIVE,
    }
    base.update(kw)
    return Mandate.model_validate(base)


# --- (1) the state enricher -----------------------------------------------------------------------


def test_the_enricher_runs_before_decide_and_sees_each_customers_events() -> None:
    calls: list[tuple[list[str], dict[str, int]]] = []

    def enrich(
        states: list[CustomerState], events: Mapping[str, list[Event]], now: datetime
    ) -> list[CustomerState]:
        assert now == SWEEP
        calls.append(([s.customer_id for s in states], {c: len(e) for c, e in events.items()}))
        return [s.model_copy(update={"scores": {**s.scores, "enriched": 1.0}}) for s in states]

    hook: StateEnricher = enrich
    w = world(state_enricher=hook, config=RuntimeConfig(enrich_lookback=timedelta(days=365)))
    report = w.runtime.tick(SWEEP)
    assert len(calls) == 1
    ids, counts = calls[0]
    assert sorted(ids) == sorted(CUSTOMERS)
    assert all(counts[c] == 2 for c in CUSTOMERS)  # consent + purchase, within the lookback
    assert len(w.policy.seen) == len(CUSTOMERS)
    assert all(s.scores.get("enriched") == 1.0 for s in w.policy.seen)  # enriched before deciding
    assert len(report.sent) == len(CUSTOMERS)
    stored = w.runtime.states.get("c1")
    assert stored is not None and "enriched" not in stored.scores  # the stored state is untouched


def test_a_failing_enricher_is_an_alert_and_the_goal_decides_on_plain_states() -> None:
    def broken(
        states: list[CustomerState], events: Mapping[str, list[Event]], now: datetime
    ) -> list[CustomerState]:
        raise RuntimeError("signals engine down")

    w = world(state_enricher=broken)
    report = w.runtime.tick(SWEEP)
    assert "enrich.error" in codes(report.alerts)
    assert len(report.sent) == len(CUSTOMERS)


# --- (2) mandates ---------------------------------------------------------------------------------


def test_a_mandate_violation_blocks_the_send_with_an_alert_and_a_decision_note() -> None:
    book = MandateBook([mandate(channels=(Channel.WHATSAPP,))])
    w = world(mandates=book)
    report = w.runtime.tick(SWEEP)
    assert report.sent == [] and sends(w) == [] and w.adapter.sent == []
    assert "mandate.violation" in codes(report.alerts)
    assert sorted(report.mandate_blocked) == sorted(d.decision_id for d in report.acts)
    for decision_id in report.mandate_blocked:
        logged = w.decisions.get(decision_id)
        assert logged is not None
        assert "mandate_channel_not_allowed" in logged.blocked_by
        assert "Not sent" in logged.reason
    assert all("mandate_channel_not_allowed" in d.blocked_by for d in report.acts)


def test_a_standing_mandate_lets_the_sends_through() -> None:
    w = world(mandates=MandateBook([mandate()]))
    report = w.runtime.tick(SWEEP)
    assert len(report.sent) == len(CUSTOMERS)
    assert not [c for c in codes(report.alerts) if c.startswith("mandate.")]


@pytest.mark.parametrize(
    ("book", "code"),
    [
        (MandateBook(required_goals=[GOAL]), "mandate.missing"),
        (MandateBook([mandate(status=MandateStatus.REVOKED)]), "mandate.not_active"),
        (
            MandateBook([mandate(valid_from=T0 - timedelta(days=40), valid_until=T0 - timedelta(days=1))]),
            "mandate.not_active",
        ),
        (MandateBook([mandate()], verify=lambda m: ["two_person_rule"]), "mandate.unverified"),
    ],
)
def test_a_goal_without_a_standing_mandate_does_not_act(book: MandateBook, code: str) -> None:
    w = world(mandates=book)
    report = w.runtime.tick(SWEEP)
    assert report.decisions == [] and report.sent == [] and w.adapter.sent == []
    assert report.halted_goals == [GOAL]
    [alert] = [a for a in report.alerts if a.code == code]
    assert alert.severity == "critical" and alert.goal_id == GOAL
    again = w.runtime.tick(SWEEP + timedelta(minutes=5))
    assert code not in codes(again.alerts)  # once a day, not every tick


def test_a_mandate_store_that_raises_halts_the_goal_not_the_tick() -> None:
    class Broken(MandateBook):
        def lookup(self, goal_id: str) -> Mandate | None:
            raise ConnectionError("mandate store down")

    w = world(mandates=Broken(required_goals=[GOAL]))
    report = w.runtime.tick(SWEEP)
    assert "mandate.error" in codes(report.alerts) and report.sent == []


def test_a_shadow_goal_is_not_held_back_by_a_missing_mandate() -> None:
    w = world(AutonomyLevel.SHADOW, mandates=MandateBook(required_goals=[GOAL]))
    report = w.runtime.tick(SWEEP)
    assert len(report.decisions) == len(CUSTOMERS) and report.sent == []
    assert report.halted_goals == []


def test_the_mandate_pacer_throttles_dispatch() -> None:
    # A daily cap of 8 at 1.00 a message: the pacer lets a quarter (2.00) go at once.
    w = world(
        mandates=MandateBook([mandate(max_daily_spend=8.0)]),
        channel_costs={Channel.EMAIL: 1.0},
    )
    report = w.runtime.tick(SWEEP)
    assert len(report.sent) == 2
    assert len(report.mandate_blocked) == 2
    assert "mandate.paced" in codes(report.alerts)
    for decision_id in report.mandate_blocked:
        logged = w.decisions.get(decision_id)
        assert logged is not None and "mandate_paced" in logged.blocked_by


class FreeChannelBidPolicy(RecordingPolicy):
    """Contacts everyone and attaches a bid that prices the channel at nothing."""

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        d = super().decide(state, ctx)
        assert d.action is not None
        bid = Bid(
            goal_id=GOAL,
            action=d.action,
            uplift=0.1,
            value_if_converted=10.0,
            channel_cost=0.0,
            expected_net=1.0,
        )
        return d.model_copy(update={"bids": (bid,)})


def test_the_pacer_prices_a_bid_at_the_runtimes_channel_cost() -> None:
    # The SpendLedger charges 1.00 an email whatever the bid said, so the mandate does too.
    w = world(
        mandates=MandateBook([mandate(max_daily_spend=8.0)]),
        channel_costs={Channel.EMAIL: 1.0},
    )
    w.runtime.policy_factory = lambda briefs, content: FreeChannelBidPolicy()
    report = w.runtime.tick(SWEEP)
    assert len(report.sent) == 2 and len(report.mandate_blocked) == 2


def test_a_queued_send_the_gate_refuses_is_skipped_without_sending() -> None:
    w = world(mandates=MandateBook([mandate(status=MandateStatus.REVOKED)]))
    assert w.runtime.dispatcher is not None
    action = Action(goal_id=GOAL, channel=Channel.EMAIL, content_id="x", send_at=SWEEP)
    w.decisions.record(
        [
            Decision(
                decision_id="d1",
                customer_id="c1",
                made_at=SWEEP,
                kind=DecisionKind.ACT,
                action=action,
                probability=1.0,
                reason="test",
                policy_version="t",
            )
        ]
    )
    w.runtime.tick(T0)  # folds the history, so c1 has a state
    reports: list[DispatchReport] = []
    handler = send_handler(
        w.runtime.dispatcher,
        w.decisions,
        w.runtime.states,
        on_report=reports.append,
        gate=w.runtime.send_gate,
    )
    handler(Job(job_id="j1", kind=SEND_KIND, payload={"decision_id": "d1"}, run_at=SWEEP), SWEEP)
    [result] = reports[0].results
    assert result.status is DispatchStatus.SKIPPED and result.reason == "mandate_not_active"
    assert w.adapter.sent == [] and sends(w) == []


def test_an_approved_batch_is_checked_against_the_mandate_when_sent() -> None:
    book = MandateBook([mandate()])
    w = world(AutonomyLevel.APPROVE, mandates=book)
    report = w.runtime.tick(SWEEP)
    [batch] = report.batches_created
    w.runtime.approve_batch(batch.batch_id, by="founder", now=SWEEP + timedelta(minutes=1))
    book.put(mandate(status=MandateStatus.REVOKED))  # revoked between approval and sending
    held = w.runtime.tick(SWEEP + timedelta(minutes=5))
    assert held.sent == [] and held.batches_sent == []
    assert "mandate.not_active" in codes(held.alerts)


def test_the_send_gate_stops_a_queued_send_under_a_revoked_mandate() -> None:
    book = MandateBook([mandate()])
    w = world(mandates=book)
    action = Action(goal_id=GOAL, channel=Channel.EMAIL, content_id="x", send_at=SWEEP)
    decision = Decision(
        decision_id="d1",
        customer_id="c1",
        made_at=SWEEP,
        kind=DecisionKind.ACT,
        action=action,
        probability=1.0,
        reason="test",
        policy_version="t",
    )
    assert w.runtime.send_gate(decision, SWEEP) is None  # no state yet: the dispatcher reports it
    book.put(mandate(status=MandateStatus.REVOKED))
    assert w.runtime.send_gate(decision, SWEEP) == "mandate_not_active"


# --- (3) the gateway ------------------------------------------------------------------------------


def gateway_world(registry: InMemoryConsentRegistry) -> tuple[World, DispatchGateway, FakeChannelAdapter]:
    adapter = FakeChannelAdapter(Channel.EMAIL)
    gateway = DispatchGateway({Channel.EMAIL: [Provider("email", adapter)]}, consent=registry)

    def attach(workspace: InMemoryWorkspace, events: InMemoryEventStore) -> DispatchGateway:
        gateway.attach_dispatcher(lambda cid, ch: f"{cid}@example.com", workspace, events)
        return gateway

    w = world(gateway_for=attach)
    assert w.runtime.dispatcher is gateway.dispatcher
    return w, gateway, adapter


def test_a_gateway_refusal_for_no_consent_is_an_alert_not_a_crash() -> None:
    registry = InMemoryConsentRegistry(
        [
            ConsentProof(
                consent_id="consent-c1",
                purpose="promotional",
                channel=Channel.EMAIL,
                captured_at=T0 - timedelta(days=30),
                source="web_form",
                evidence_hash=evidence_hash("c1 ticked the box"),
                recipient="c1@example.com",
                customer_id="c1",
            )
        ]
    )
    w, _, adapter = gateway_world(registry)
    report = w.runtime.tick(SWEEP)
    assert [r.customer_id for r in report.sent] == ["c1"]  # the one customer with a consent proof
    assert len(adapter.sent) == 1
    [alert] = [a for a in report.alerts if a.code == "dispatch.refused"]
    assert "consent_missing (3)" in alert.message and alert.goal_id == GOAL


def test_a_gateway_that_raises_is_an_alert_and_the_tick_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    w, gateway, _ = gateway_world(InMemoryConsentRegistry())

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("carrier SDK exploded")

    monkeypatch.setattr(gateway, "dispatch", boom)
    report = w.runtime.tick(SWEEP)
    assert "dispatch.error" in codes(report.alerts)
    assert len(report.decisions) == len(CUSTOMERS) and report.sent == []


def test_a_gateway_needs_an_attached_dispatcher() -> None:
    gateway = DispatchGateway({Channel.EMAIL: [Provider("email", FakeChannelAdapter(Channel.EMAIL))]})
    with pytest.raises(ValueError, match="attach"):
        world(gateway=gateway)


def test_build_runtime_wraps_the_dispatcher_in_the_gateway_only_when_asked() -> None:
    adapters = {Channel.WEBHOOK: FakeChannelAdapter(Channel.WEBHOOK)}
    plain = build_runtime(RuntimeSettings(), engine=memory_engine(), adapters=adapters)
    assert plain.gateway is None and plain.dispatcher is not None

    settings = RuntimeSettings.from_env({"AUTONOMY_GATEWAY_ENABLED": "true", "AUTONOMY_COUNTRIES": "in, us"})
    assert settings.countries == ("IN", "US")
    wired = build_runtime(settings, engine=memory_engine(), adapters=adapters)
    assert wired.gateway is not None
    assert wired.dispatcher is wired.gateway.dispatcher
    assert wired.gateway.consent is not None  # the SQL consent registry
    assert wired.gateway.queue is wired.queue
    assert wired.gateway.default_country == "IN"
    assert wired.lift_guards is not None


def test_dnd_settings_must_name_a_register_and_a_path() -> None:
    with pytest.raises(ValueError, match="REGISTER=path"):
        RuntimeSettings(dnd_registries=("ncpr.txt",))


# --- (4) breakers ---------------------------------------------------------------------------------


def outbound(n: int, etype: EventType, at: datetime, tag: str) -> list[Event]:
    return [
        Event(
            event_id=f"{tag}_{etype.value}_{i}",
            customer_id=f"x{i}",
            type=etype,
            ts=at,
            source="test",
            channel=Channel.EMAIL,
            decision_id=f"{tag}_d{i}",
        )
        for i in range(n)
    ]


def test_complaints_in_the_last_hour_trip_even_when_the_day_dilutes_them() -> None:
    now = SWEEP
    rule = StopRule(metric=StopMetric.COMPLAINT_RATE, threshold=0.005, action="pause_goal")
    earlier = now - timedelta(hours=6)
    recent = now - timedelta(minutes=20)
    events = (
        outbound(2000, EventType.SEND, earlier, "a")
        + outbound(2000, EventType.DELIVER, earlier, "a")
        + outbound(150, EventType.SEND, recent, "b")
        + outbound(150, EventType.DELIVER, recent, "b")
        + outbound(3, EventType.COMPLAINT, recent, "b")
    )
    res = evaluate([rule], events, {}, 0, 0, now, goal_id=GOAL)
    [alert] = res.alerts
    assert res.paused_goals == {GOAL} and "the last hour" in alert.message
    off = evaluate([rule], events, {}, 0, 0, now, goal_id=GOAL, guards=Guards(fast_window=None))
    assert off.alerts == []  # 3 of 2150 over the day is under the limit


def test_the_last_hour_counts_only_complaints_about_that_hours_messages() -> None:
    # Yesterday's big send is complained about late; the last hour's small send drew none. Those
    # late complaints over the hour's 150 deliveries must not read as a 2% burst.
    now = SWEEP
    rule = StopRule(metric=StopMetric.COMPLAINT_RATE, threshold=0.005, action="pause_goal")
    yesterday = now - timedelta(hours=20)
    recent = now - timedelta(minutes=20)
    events = (
        outbound(2000, EventType.SEND, yesterday, "a")
        + outbound(2000, EventType.DELIVER, yesterday, "a")
        + outbound(150, EventType.SEND, recent, "b")
        + outbound(150, EventType.DELIVER, recent, "b")
        + [e.model_copy(update={"ts": recent}) for e in outbound(3, EventType.COMPLAINT, yesterday, "a")]
    )
    res = evaluate([rule], events, {}, 0, 0, now, goal_id=GOAL)
    assert res.alerts == [] and res.paused_goals == set()


def _holdout_id(cid: str, at: datetime) -> str:
    """The id the runtime gives a sweep decision (so the nightly check can find its goal)."""
    return stable_id("dec", GOAL, cid, "sweep", at.date().isoformat())


def proof_log(
    made_at: datetime, *, agent_buys: float, holdout_buys: float, n: int = 40
) -> tuple[list[Decision], list[Event]]:
    """`n` agent customers contacted and `n` holdout customers left alone, with purchases after."""
    decisions: list[Decision] = []
    events: list[Event] = []
    action = Action(goal_id=GOAL, channel=Channel.EMAIL, offer_id=NO_OFFER.offer_id, send_at=made_at)
    for i in range(n):
        a, h = f"a{i}", f"h{i}"
        did = _holdout_id(a, made_at)
        decisions.append(
            Decision(
                decision_id=did,
                customer_id=a,
                made_at=made_at,
                kind=DecisionKind.ACT,
                action=action,
                group=Group.AGENT,
                probability=1.0,
                reason="contact",
                policy_version="t",
            )
        )
        decisions.append(
            Decision(
                decision_id=_holdout_id(h, made_at),
                customer_id=h,
                made_at=made_at,
                kind=DecisionKind.NOTHING,
                group=Group.HOLDOUT,
                probability=1.0,
                reason="holdout",
                policy_version="t",
            )
        )
        events.append(
            Event(
                event_id=f"send_{a}",
                customer_id=a,
                type=EventType.SEND,
                ts=made_at,
                source="test",
                channel=Channel.EMAIL,
                decision_id=did,
                attrs={"goal_id": GOAL},
            )
        )
        for cid, base in ((a, agent_buys), (h, holdout_buys)):
            events.append(
                Event(
                    event_id=f"buy2_{cid}",
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=made_at + timedelta(days=2),
                    source="test",
                    value=base + (i % 7),
                )
            )
    return decisions, events


LIFT = LiftGuards(min_group=30, min_closed=60)


def test_negative_lift_pauses_only_a_goal_confidently_losing_money() -> None:
    now = T0
    made = now - timedelta(days=40)
    brief = sample_brief(made)
    losing, events = proof_log(made, agent_buys=1.0, holdout_buys=30.0)
    by_customer: dict[str, list[Event]] = {}
    for e in events:
        by_customer.setdefault(e.customer_id, []).append(e)
    tripped = negative_lift(brief, losing, by_customer, now, guards=LIFT)
    assert tripped.paused_goals == {GOAL}
    [alert] = tripped.alerts
    assert alert.code == NEGATIVE_LIFT_CODE and alert.auto_paused and alert.severity == "critical"

    # The same losing goal, below the minimum sample: nothing is judged.
    small = negative_lift(brief, losing, by_customer, now, guards=LiftGuards(min_closed=1000))
    assert small.alerts == [] and small.paused_goals == set()
    few = negative_lift(brief, losing, by_customer, now, guards=LiftGuards(min_group=50, min_closed=60))
    assert few.alerts == []  # 40 customers a group is under a floor of 50

    winning, events = proof_log(made, agent_buys=30.0, holdout_buys=1.0)
    by_customer = {}
    for e in events:
        by_customer.setdefault(e.customer_id, []).append(e)
    quiet = negative_lift(brief, winning, by_customer, now, guards=LIFT)
    assert quiet.alerts == [] and quiet.paused_goals == set()  # one-sided: never declares a win


def test_the_nightly_negative_lift_check_pauses_the_goal() -> None:
    nightly = T0.replace(hour=2, minute=30)
    made = nightly - timedelta(days=40)
    decisions, events = proof_log(made, agent_buys=1.0, holdout_buys=30.0)
    w = world(seed_events=history() + events, goal_start=made - timedelta(days=1), lift_guards=LIFT)
    w.decisions.record(decisions)
    report = w.runtime.tick(nightly)
    assert report.nightly_ran
    assert report.paused_goals == [GOAL]
    assert NEGATIVE_LIFT_CODE in codes(report.alerts)
    goal = next(g for g in w.workspace.goals() if g.goal_id == GOAL)
    assert goal.status is GoalStatus.PAUSED


def test_the_negative_lift_check_is_off_by_default() -> None:
    nightly = T0.replace(hour=2, minute=30)
    made = nightly - timedelta(days=40)
    decisions, events = proof_log(made, agent_buys=1.0, holdout_buys=30.0)
    w = world(seed_events=history() + events, goal_start=made - timedelta(days=1))
    w.decisions.record(decisions)
    report = w.runtime.tick(nightly)
    assert report.nightly_ran and report.paused_goals == []
