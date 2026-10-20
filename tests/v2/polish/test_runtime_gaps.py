"""The runtime's Wave 2 loose ends, closed: spend with offers, stored alerts and briefings, learning
that survives restarts, one proof split per business, one owner for send jobs, and the worker CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.channels.dispatcher import Dispatcher
from autonomy.contracts import (
    Action,
    Alert,
    AutonomyLevel,
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
    Reward,
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
from autonomy.interfaces import Policy
from autonomy.runtime.service import (
    AutonomyRuntime,
    EvenPacing,
    PolicyFactory,
    RuntimeConfig,
    RuntimeCursor,
    SpendLedger,
    SplitMismatchError,
    StandardBreakers,
    check_split,
    standard_breakers,
    tick_slot,
)
from autonomy.runtime.settings import RuntimeSettings, build_runtime
from autonomy.runtime.stores import (
    AlertStore,
    BriefingStore,
    InMemoryAlertStore,
    InMemoryBriefingStore,
    SqlAlertStore,
    SqlBriefingStore,
    create_record_tables,
)
from autonomy.runtime.worker import (
    SEND_KIND,
    TICK_KIND,
    WEBHOOK_KIND,
    SendHandler,
    Worker,
    send_handler,
    worker_owns_sends,
)
from autonomy.store import memory_engine

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # a Monday, before the 10:00 sweep
SWEEP = T0.replace(hour=10)
START = T0 - timedelta(days=1)
GOAL = "goal_winback"
CUSTOMERS = ("c1", "c2", "c3", "c4")
EMAIL_COST = 0.001  # DEFAULT_CHANNEL_COSTS[EMAIL]
PCT10_COST = 6.0  # the sample brief's 10% off, per redemption


def history() -> list[Event]:
    """Each customer consented to email long ago and last bought 90 days before T0 (lapsed)."""
    out: list[Event] = []
    for cid in CUSTOMERS:
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
            )
        )
    return out


@dataclass
class OfferPolicy:
    """Emails everyone with 10% off (content ci_1), sending `delay` after deciding; records budget_left."""

    delay: timedelta = timedelta(0)
    version: str = "offer-v0"
    budgets: list[float] = field(default_factory=list)

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        brief = ctx.briefs[0]
        self.budgets.append(ctx.budget_left[brief.goal_id])
        action = Action(
            goal_id=brief.goal_id,
            channel=Channel.EMAIL,
            offer_id="pct10",
            content_id="ci_1",
            send_at=ctx.now + self.delay,
        )
        return Decision(
            decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat()),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.ACT,
            action=action,
            group=state.group,
            probability=1.0,
            reason="Test: 10% off for everyone.",
            policy_version=self.version,
        )

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


@dataclass
class LearningPolicy:
    version: str = "learner-v0"
    nightly_calls: list[tuple[int, int, datetime]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    """Every call in order: "decide", or "nightly:<number of decisions>"."""

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        self.calls.append("decide")
        return ContactEveryonePolicy().decide(state, ctx)

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None

    def nightly(
        self, decisions: Sequence[Decision], events_by_customer: Mapping[str, Sequence[Event]], now: datetime
    ) -> None:
        self.calls.append(f"nightly:{len(decisions)}")
        self.nightly_calls.append((len(decisions), len(events_by_customer), now))


@dataclass
class BrokenLearner(LearningPolicy):
    def nightly(
        self, decisions: Sequence[Decision], events_by_customer: Mapping[str, Sequence[Event]], now: datetime
    ) -> None:
        raise RuntimeError("the learner is broken")


@dataclass
class Stores:
    events: InMemoryEventStore = field(default_factory=InMemoryEventStore)
    workspace: InMemoryWorkspace = field(default_factory=InMemoryWorkspace)
    states: InMemoryStateStore = field(default_factory=InMemoryStateStore)
    decisions: InMemoryDecisionLog = field(default_factory=InMemoryDecisionLog)
    queue: InMemoryJobQueue = field(default_factory=InMemoryJobQueue)
    adapter: FakeChannelAdapter = field(default_factory=lambda: FakeChannelAdapter(Channel.EMAIL))
    alerts: InMemoryAlertStore = field(default_factory=InMemoryAlertStore)
    briefings: InMemoryBriefingStore = field(default_factory=InMemoryBriefingStore)


def stores(*, brief: Brief | None = None, autonomy: AutonomyLevel = AutonomyLevel.AUTOPILOT) -> Stores:
    st = Stores()
    st.events.append(history())
    st.workspace.put_goal(
        sample_goal(START).model_copy(update={"autonomy": autonomy, "status": GoalStatus.ACTIVE})
    )
    st.workspace.put_brief(brief or sample_brief(START))
    st.workspace.put_content(sample_content(START))
    return st


def runtime(
    st: Stores,
    policy: Policy | None = None,
    *,
    factory: PolicyFactory | None = None,
    config: RuntimeConfig | None = None,
    cursor: RuntimeCursor | None = None,
    **kwargs: object,
) -> AutonomyRuntime:
    chosen: Policy = policy if policy is not None else ContactEveryonePolicy()
    dispatcher = Dispatcher(
        {Channel.EMAIL: st.adapter}, lambda cid, ch: f"{cid}@example.com", st.workspace, st.events, st.queue
    )
    return AutonomyRuntime(
        events=st.events,
        states=st.states,
        decisions=st.decisions,
        workspace=st.workspace,
        folder=SimpleStateFolder(),
        policy_factory=factory or (lambda briefs, content: chosen),
        queue=st.queue,
        dispatcher=dispatcher,
        config=config,
        cursor=cursor,
        alerts=st.alerts,
        briefings=st.briefings,
        **kwargs,  # type: ignore[arg-type]
    )


def purchase(cid: str, ts: datetime, *, offer: str | None = "pct10", **attrs: float | str) -> Event:
    extra: dict[str, float | str] = dict(attrs)
    if offer is not None:
        extra["offer_id"] = offer
    return Event(
        event_id=stable_id("buy", cid, ts.isoformat(), offer or ""),
        customer_id=cid,
        type=EventType.PURCHASE,
        ts=ts,
        source="test",
        value=60.0,
        attrs=extra,
    )


def send_event(cid: str, ts: datetime, decision_id: str, *, offer: str = "pct10") -> Event:
    return Event(
        event_id=stable_id("send", decision_id),
        customer_id=cid,
        type=EventType.SEND,
        ts=ts,
        source="test",
        channel=Channel.EMAIL,
        decision_id=decision_id,
        attrs={"goal_id": GOAL, "offer_id": offer},
    )


# --- 1. spend: channel and offer costs, and a planned-spend source ---------------------------------


def test_the_ledger_charges_a_used_offer_once_inside_the_outcome_window() -> None:
    st = stores()
    ledger = SpendLedger(st.decisions, st.workspace)
    ledger.observe([send_event("c1", SWEEP, "d1"), send_event("c2", SWEEP, "d2")])
    assert ledger(GOAL, SWEEP) == pytest.approx(2 * EMAIL_COST)

    ledger.observe(
        [
            purchase("c1", SWEEP + timedelta(days=2)),  # used the offer in the window: paid
            purchase("c1", SWEEP + timedelta(days=3)),  # the same decision pays its offer once
            purchase("c2", SWEEP + timedelta(days=20)),  # after the 14-day window: not ours
            purchase("c3", SWEEP + timedelta(days=1)),  # nobody sent c3 an offer
            purchase("c1", SWEEP + timedelta(days=1), offer=None),  # bought without the offer
        ]
    )
    assert ledger(GOAL, SWEEP + timedelta(days=30)) == pytest.approx(2 * EMAIL_COST + PCT10_COST)
    assert ledger(GOAL, SWEEP + timedelta(days=1)) == pytest.approx(2 * EMAIL_COST)  # charged when used

    # Idempotent by event id, and a shop-reported offer cost wins over the brief's price.
    ledger.observe([send_event("c1", SWEEP, "d1")])
    ledger.observe([purchase("c4", SWEEP + timedelta(hours=5), offer_cost=2.5)])  # its send is late
    ledger.observe([send_event("c4", SWEEP, "d4")])
    assert ledger(GOAL, SWEEP + timedelta(days=30)) == pytest.approx(3 * EMAIL_COST + PCT10_COST + 2.5)
    ledger.reset()
    assert ledger(GOAL, SWEEP + timedelta(days=30)) == 0.0


def test_budget_left_includes_redeemed_offers() -> None:
    st = stores()
    policy = OfferPolicy()
    rt = runtime(st, policy)
    first = rt.tick(SWEEP)
    assert len(first.sent) == len(CUSTOMERS)
    assert policy.budgets == [5000.0] * len(CUSTOMERS)
    st.events.append([purchase("c1", SWEEP + timedelta(hours=1))])

    rt.tick(SWEEP + timedelta(days=1))
    spent = len(CUSTOMERS) * EMAIL_COST + PCT10_COST
    assert policy.budgets[len(CUSTOMERS)] == pytest.approx(5000.0 - spent)
    assert rt.spend_fn(GOAL, SWEEP + timedelta(hours=2)) == pytest.approx(spent)


def test_spend_pace_trips_on_offer_spend_against_even_pacing() -> None:
    st = stores()
    rt = runtime(st, OfferPolicy(), breakers_fn=standard_breakers())
    assert isinstance(rt.breakers_fn, StandardBreakers)
    assert rt.breakers_fn.spend_fn is rt.spend_fn and rt.breakers_fn.planned_fn is rt.planned_fn
    assert rt.tick(SWEEP).paused_goals == []
    # 1,000 of offers redeemed on day two, against about 194 planned so far (5,000 over 30 days).
    st.events.append([purchase("c1", SWEEP + timedelta(hours=1), offer_cost=1000.0)])
    report = rt.tick(SWEEP + timedelta(hours=2))
    assert report.paused_goals == [GOAL]
    assert any(a.code == "breaker.spend_pace" for a in report.alerts)
    assert any(a.code == "breaker.spend_pace" for a in st.alerts.list_alerts(goal_id=GOAL))
    goal = next(g for g in st.workspace.goals() if g.goal_id == GOAL)
    assert goal.status is GoalStatus.PAUSED


def test_even_pacing_spreads_the_budget_over_the_goal_period() -> None:
    ws = InMemoryWorkspace()
    ws.put_goal(sample_goal(START).model_copy(update={"ends_at": START + timedelta(days=10)}))
    ws.put_brief(sample_brief(START))
    ws.put_goal(sample_goal(START, goal_id="open_ended"))
    pacing = EvenPacing(ws, plan_days=20)
    assert pacing(GOAL, START - timedelta(days=1)) == 0.0
    assert pacing(GOAL, START + timedelta(days=5)) == pytest.approx(2500.0)
    assert pacing(GOAL, START + timedelta(days=40)) == pytest.approx(5000.0)
    assert pacing("open_ended", START + timedelta(days=5)) == pytest.approx(1250.0)  # 5 of 20 days
    assert pacing("nobody", START) == 0.0


# --- 2. alert and briefing stores --------------------------------------------------------------------


def _alert(i: int, at: datetime, goal_id: str | None) -> Alert:
    return Alert(alert_id=f"a{i}", at=at, goal_id=goal_id, severity="info", code="x", message="m")


def _briefing(i: int, at: datetime, goal_id: str | None, period: str = "daily") -> Briefing:
    return Briefing(briefing_id=f"b{i}", goal_id=goal_id, as_of=at, period=period, headline="h")  # type: ignore[arg-type]


def _record_stores() -> list[tuple[AlertStore, BriefingStore]]:
    engine = memory_engine()
    create_record_tables(engine)
    create_record_tables(engine)  # safe twice
    return [
        (InMemoryAlertStore(), InMemoryBriefingStore()),
        (SqlAlertStore(engine), SqlBriefingStore(engine)),
    ]


@pytest.mark.parametrize("pair", _record_stores(), ids=["memory", "sqlite"])
def test_alert_and_briefing_stores(pair: tuple[AlertStore, BriefingStore]) -> None:
    alerts, briefings = pair
    rows = [_alert(1, T0, GOAL), _alert(2, T0 + timedelta(hours=1), None), _alert(3, T0, "other")]
    assert alerts.put(rows) == 3 and alerts.put(rows[:1]) == 0
    assert [a.alert_id for a in alerts.list_alerts()] == ["a2", "a3", "a1"]
    assert [a.alert_id for a in alerts.list_alerts(goal_id=GOAL)] == ["a2", "a1"]
    assert [a.alert_id for a in alerts.list_alerts(since=T0 + timedelta(minutes=1))] == ["a2"]
    assert [a.alert_id for a in alerts.list_alerts(limit=1)] == ["a2"]
    assert alerts.list_alerts()[0] == rows[1]
    assert isinstance(alerts, InMemoryAlertStore | SqlAlertStore)
    assert alerts.list(limit=1) == alerts.list_alerts(limit=1)  # the generic name readers look for

    b = [
        _briefing(1, T0, None),
        _briefing(2, T0 + timedelta(days=1), None, "weekly"),
        _briefing(3, T0 + timedelta(days=2), GOAL),
    ]
    assert briefings.put(b) == 3 and briefings.put(b) == 0
    assert briefings.latest(None) == b[1]
    assert briefings.latest(None, period="daily") == b[0]
    assert briefings.latest(GOAL) == b[2] and briefings.latest("other") is None
    assert [x.briefing_id for x in briefings.list_briefings(limit=2)] == ["b3", "b2"]


def test_the_runtime_stores_its_alerts_and_weekly_briefings() -> None:
    def analyst(goal: Goal, brief: Brief, now: datetime) -> Briefing:
        return Briefing(
            briefing_id=stable_id("br", goal.goal_id, now),
            goal_id=goal.goal_id,
            as_of=now,
            period="weekly",
            headline="ok",
        )

    def strategist(brief: Brief, insights: Briefing, now: datetime) -> Brief:
        return brief.model_copy(update={"version": brief.version + 1, "created_at": now})

    st = stores(autonomy=AutonomyLevel.APPROVE)
    rt = runtime(st, analyst=analyst, strategist=strategist)
    rt.tick(SWEEP)
    weekly = st.briefings.latest(GOAL, period="weekly")
    assert weekly is not None and weekly.as_of == SWEEP
    expired = rt.tick(SWEEP + timedelta(days=4))  # nobody approved the batch
    assert any(a.code == "batch.expired" for a in expired.alerts)
    assert {a.alert_id for a in expired.alerts} <= {a.alert_id for a in st.alerts.list_alerts()}
    rt.record(alerts=[_alert(9, SWEEP, None)], briefings=[_briefing(9, SWEEP, None)])
    assert "a9" in {a.alert_id for a in st.alerts.list_alerts()}
    assert st.briefings.latest(None) is not None


# --- 3. learning survives restarts; identical brief versions keep the policy ------------------------


def test_a_restarted_runtime_replays_the_log_through_nightly_before_deciding() -> None:
    st = stores(autonomy=AutonomyLevel.SHADOW)
    before = LearningPolicy()
    first = runtime(st, before)
    assert first.tick(SWEEP).replayed_goals == []  # nothing in the log yet: nothing to replay
    decided = len(CUSTOMERS)

    built: list[LearningPolicy] = []

    def factory(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        built.append(LearningPolicy())
        return built[-1]

    later = SWEEP + timedelta(hours=3)
    restarted = runtime(st, factory=factory, cursor=first.cursor.model_copy(deep=True))
    report = restarted.tick(later)
    assert report.replayed_goals == [GOAL] and not report.nightly_ran
    assert len(built) == 1 and built[0].nightly_calls == [(decided, 0, later)]

    # A new version that only restates the plan keeps the policy (and what it learned).
    v1 = sample_brief(START)
    st.workspace.put_brief(v1.model_copy(update={"version": 2, "rationale": "Same plan, reworded."}))
    assert restarted.tick(later + timedelta(hours=1)).replayed_goals == [] and len(built) == 1
    # A material change rebuilds it, and the new policy relearns from the log.
    wider = v1.envelope.model_copy(update={"max_contacts_per_week": 3})
    st.workspace.put_brief(v1.model_copy(update={"version": 3, "envelope": wider}))
    report = restarted.tick(later + timedelta(hours=2))
    assert report.replayed_goals == [GOAL] and len(built) == 2
    assert built[1].nightly_calls == [(decided, 0, later + timedelta(hours=2))]


def test_a_policy_built_on_restart_learns_before_its_first_decision() -> None:
    st = stores(autonomy=AutonomyLevel.SHADOW)
    first = runtime(st, LearningPolicy())
    first.tick(SWEEP)
    n = len(CUSTOMERS)

    fresh = LearningPolicy()
    restarted = runtime(st, fresh, cursor=first.cursor.model_copy(deep=True))
    report = restarted.tick(SWEEP + timedelta(days=1))  # the next day's sweep and its nightly run
    assert report.replayed_goals == [GOAL] and report.nightly_ran and len(report.decisions) == n
    # Yesterday's decisions are relearned first; then today's sweep; then the nightly run over both.
    assert fresh.calls == [f"nightly:{n}", *["decide"] * n, f"nightly:{2 * n}"]


def test_a_failed_replay_is_an_alert_and_not_reported_as_replayed() -> None:
    st = stores(autonomy=AutonomyLevel.SHADOW)
    first = runtime(st, LearningPolicy())
    first.tick(SWEEP)
    restarted = runtime(st, BrokenLearner(), cursor=first.cursor.model_copy(deep=True))
    report = restarted.tick(SWEEP + timedelta(hours=3))
    assert report.replayed_goals == []
    assert [a.code for a in report.alerts] == ["replay.error"]


# --- 4. one proof split per business ------------------------------------------------------------------


def test_a_brief_asking_for_another_split_is_refused_with_a_clear_error() -> None:
    v1 = sample_brief(START)
    odd = v1.model_copy(update={"envelope": v1.envelope.model_copy(update={"holdout_share": 0.2})})
    st = stores(brief=odd)
    rt = runtime(st, config=RuntimeConfig(holdout_share=0.1, baseline_share=0.1))
    with pytest.raises(SplitMismatchError, match="20% holdout") as err:
        rt.check_brief(odd)
    assert "10% holdout and 10% baseline" in str(err.value)
    rt.check_brief(v1)  # the business's split passes

    report = rt.tick(SWEEP)
    assert report.decisions == [] and st.adapter.sent == []
    refused = [a for a in report.alerts if a.code == "brief.split_mismatch"]
    assert len(refused) == 1 and refused[0].severity == "critical" and refused[0].goal_id == GOAL
    assert rt.tick(SWEEP + timedelta(minutes=5)).alerts == []  # said once a day
    assert any(a.code == "brief.split_mismatch" for a in st.alerts.list_alerts())

    st.workspace.put_brief(v1.model_copy(update={"version": 2}))
    assert len(rt.tick(SWEEP + timedelta(minutes=10)).decisions) == len(CUSTOMERS)
    check_split(v1, 0.10, 0.10)
    with pytest.raises(ValueError, match="both"):
        RuntimeConfig(holdout_share=0.1)


def test_build_runtime_enforces_the_settings_split_and_stores_alerts_in_sql() -> None:
    engine = memory_engine()
    rt = build_runtime(
        RuntimeSettings(database_url="sqlite://", holdout_share=0.05, baseline_share=0.05),
        engine=engine,
        policy_factory=lambda briefs, content: ContactEveryonePolicy(),
        adapters={Channel.EMAIL: FakeChannelAdapter(Channel.EMAIL)},
        address_book=lambda cid, ch: f"{cid}@example.com",
    )
    assert rt.config.run_send_jobs is False  # the worker process owns send jobs
    rt.workspace.put_goal(
        sample_goal(START).model_copy(
            update={"autonomy": AutonomyLevel.AUTOPILOT, "status": GoalStatus.ACTIVE}
        )
    )
    rt.workspace.put_brief(sample_brief(START))  # 10% / 10%: not this business's split
    rt.workspace.put_content(sample_content(START))
    rt.events.append(history())
    assert rt.tick(SWEEP).decisions == []
    elsewhere = SqlAlertStore(engine)  # what another process (the API) reads
    assert [a.code for a in elsewhere.list_alerts(goal_id=GOAL)] == ["brief.split_mismatch"]


# --- 5. send jobs have one owner -------------------------------------------------------------------


def test_a_worker_with_a_send_handler_owns_send_jobs() -> None:
    st = stores()
    rt = runtime(st, OfferPolicy(delay=timedelta(hours=1)))
    assert rt.dispatcher is not None and not worker_owns_sends(rt.dispatcher)
    worker = Worker(st.queue, {SEND_KIND: send_handler(rt.dispatcher, st.decisions, st.states)})
    assert worker_owns_sends(rt.dispatcher)
    assert len(rt.tick(SWEEP).dispatch.queued) == len(CUSTOMERS)
    due = SWEEP + timedelta(hours=1)
    assert rt.tick(due).sent == [] and st.adapter.sent == []  # the tick leaves them to the worker
    outcomes = worker.drain(due)
    assert [o.status for o in outcomes] == ["done"] * len(CUSTOMERS)
    assert len(st.adapter.sent) == len(CUSTOMERS)


def test_a_runtime_without_a_worker_hosts_the_send_handler_and_false_turns_it_off() -> None:
    st = stores()
    rt = runtime(st, OfferPolicy(delay=timedelta(hours=1)))
    rt.tick(SWEEP)
    hosted = rt.tick(SWEEP + timedelta(hours=1))
    assert len(hosted.sent) == len(CUSTOMERS)
    assert rt.dispatcher is not None and not worker_owns_sends(rt.dispatcher)  # hosted: no takeover
    assert all(st.states.get(c) is not None and st.states.get(c).contacts_7d == 1 for c in CUSTOMERS)  # type: ignore[union-attr]

    off = stores()
    quiet = runtime(off, OfferPolicy(delay=timedelta(hours=1)), config=RuntimeConfig(run_send_jobs=False))
    quiet.tick(SWEEP)
    assert quiet.tick(SWEEP + timedelta(hours=1)).sent == [] and off.adapter.sent == []
    assert off.queue.claim([SEND_KIND], worker="w", now=SWEEP + timedelta(hours=1)) is not None


# --- 6. build_worker and the CLI without --demo ------------------------------------------------------


def test_build_worker_owns_sends_and_seeds_ticks_on_one_grid() -> None:
    from autonomy.runtime.service import build_worker

    worker = build_worker("w-gaps", environ={"AUTONOMY_DATABASE_URL": "sqlite://"}, start=T0)
    sender = worker.handlers[SEND_KIND]
    assert isinstance(sender, SendHandler) and worker_owns_sends(sender.dispatcher)
    assert set(worker.kinds) == {SEND_KIND, TICK_KIND, WEBHOOK_KIND}
    # The repeating tick is seeded at `start`, and not due before it.
    assert worker.queue.claim([TICK_KIND], worker="probe", now=T0 - timedelta(seconds=1)) is None
    seeded = worker.queue.claim([TICK_KIND], worker="probe", now=T0)
    assert seeded is not None and seeded.run_at == T0 and seeded.payload["every_minutes"] == 5
    every = timedelta(minutes=7)
    a = tick_slot(datetime(2026, 3, 2, 10, 3, 30, tzinfo=UTC), every)
    b = tick_slot(a + timedelta(minutes=6, seconds=59), every)
    assert a == b and (a - datetime(1970, 1, 1, tzinfo=UTC)) % every == timedelta(0)
    assert tick_slot(a + every, every) == a + every


def test_the_worker_cli_runs_without_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    from autonomy.runtime.worker import main

    monkeypatch.setenv("AUTONOMY_DATABASE_URL", "sqlite://")
    assert main(["--once", "--name", "cli-test"]) == 0
    monkeypatch.setenv("AUTONOMY_TIMEZONE", "Mars/Base")
    assert main(["--once"]) == 2  # bad settings: a clear message, not a traceback
