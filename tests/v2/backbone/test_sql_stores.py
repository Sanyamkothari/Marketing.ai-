"""The SQL stores behave exactly like the in-memory fakes.

Every test here runs three times: against the fakes, against SQLite, and against Postgres. The
Postgres run needs `MARKETING_AI_TEST_DATABASE_URL` (CI sets it) and skips with that reason when it
is absent. Each Postgres run gets a schema of its own, dropped afterwards.

The SQL stores are built with a tiny scan batch so that keyset paging is exercised by small data.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine

from autonomy import interfaces as ifc
from autonomy.contracts import (
    Action,
    Bid,
    Channel,
    ChannelStats,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Group,
    Job,
    JobStatus,
)
from autonomy.fakes import (
    ContactEveryonePolicy,
    FakeEnvironment,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryStateStore,
    InMemoryWorkspace,
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.loop import run_days
from autonomy.store import (
    SqlDecisionLog,
    SqlEventStore,
    SqlJobQueue,
    SqlStateStore,
    SqlWorkspace,
    create_all,
    memory_engine,
)
from autonomy.store.schema import event_table

DB_ENV = "MARKETING_AI_TEST_DATABASE_URL"
REQUIRE_ENV = "MARKETING_AI_REQUIRE_POSTGRES"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Stores:
    name: str
    events: Any
    states: Any
    decisions: Any
    workspace: Any
    jobs: Any
    engine: Engine | None = None


def _sql_stores(name: str, engine: Engine) -> Stores:
    create_all(engine)
    return Stores(
        name=name,
        events=SqlEventStore(engine, batch_size=2),
        states=SqlStateStore(engine, batch_size=2),
        decisions=SqlDecisionLog(engine, batch_size=2),
        workspace=SqlWorkspace(engine),
        jobs=SqlJobQueue(engine),
        engine=engine,
    )


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """An engine whose tables live in a fresh schema, dropped when the test ends."""
    url = os.environ.get(DB_ENV, "").strip()
    if not url:
        reason = f"{DB_ENV} is not set, so the Postgres variant cannot run (CI sets it)"
        # The same switch as tests/fixtures/postgres.py (not imported: it pulls in v1 modules the
        # light v2 job does not install). A pipeline that should have a database goes red.
        if os.environ.get(REQUIRE_ENV, "").strip() in {"1", "true", "yes", "on"}:
            pytest.fail(f"{REQUIRE_ENV} is set: {reason}")
        pytest.skip(reason)
    base = create_engine(url)
    schema = f"v2test_{uuid.uuid4().hex[:12]}"
    with base.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield base.execution_options(schema_translate_map={None: schema})
    finally:
        with base.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base.dispose()


@pytest.fixture(params=["fake", "sqlite", "postgres"])
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Stores]:
    if request.param == "fake":
        yield Stores(
            "fake",
            InMemoryEventStore(),
            InMemoryStateStore(),
            InMemoryDecisionLog(),
            InMemoryWorkspace(),
            InMemoryJobQueue(),
        )
    elif request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'v2.db'}")
        try:
            yield _sql_stores("sqlite", engine)
        finally:
            engine.dispose()
    else:
        yield _sql_stores("postgres", request.getfixturevalue("postgres_engine"))


@pytest.fixture(params=["sqlite", "postgres"])
def sql_engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'v2.db'}")
        create_all(engine)
        try:
            yield engine
        finally:
            engine.dispose()
    else:
        engine = request.getfixturevalue("postgres_engine")
        create_all(engine)
        yield engine


# --- builders ---------------------------------------------------------------------------------


def _event(
    eid: str,
    customer: str,
    ts: datetime,
    kind: EventType = EventType.VISIT,
    decision_id: str | None = None,
    **extra: Any,
) -> Event:
    channel = Channel.EMAIL if kind in (EventType.SEND, EventType.OPEN, EventType.CLICK) else None
    return Event(
        event_id=eid,
        customer_id=customer,
        type=kind,
        ts=ts,
        source="test",
        channel=channel,
        decision_id=decision_id,
        **extra,
    )


def _state(customer: str, version: int = 1, **extra: Any) -> CustomerState:
    return CustomerState(customer_id=customer, as_of=T0, version=version, **extra)


def _decision(did: str, customer: str, made_at: datetime, *, act: bool = True, reason: str = "r") -> Decision:
    action = Action(goal_id="goal_winback", channel=Channel.EMAIL, offer_id="pct10", send_at=made_at)
    bid = Bid(
        goal_id="goal_winback",
        action=action,
        uplift=0.031,
        value_if_converted=42.5,
        offer_cost=6.0,
        expected_net=1.0 / 3.0,
        uncertainty=0.2,
    )
    return Decision(
        decision_id=did,
        customer_id=customer,
        made_at=made_at,
        kind=DecisionKind.ACT if act else DecisionKind.NOTHING,
        action=action if act else None,
        probability=0.7 if act else 0.3,
        reason=reason,
        bids=(bid,) if act else (),
        blocked_by=("quiet_hours",),
        policy_version="v1",
    )


def _job(jid: str, kind: str, run_at: datetime, **payload: str | int | float | bool | None) -> Job:
    return Job(job_id=jid, kind=kind, run_at=run_at, payload=payload)


# --- protocols --------------------------------------------------------------------------------


def test_every_store_satisfies_its_protocol(stores: Stores) -> None:
    assert isinstance(stores.events, ifc.EventStore)
    assert isinstance(stores.states, ifc.StateStore)
    assert isinstance(stores.decisions, ifc.DecisionLog)
    assert isinstance(stores.workspace, ifc.Workspace)
    assert isinstance(stores.jobs, ifc.JobQueue)


# --- events -----------------------------------------------------------------------------------


def test_append_counts_new_events_and_ignores_repeats(stores: Stores) -> None:
    first = _event("e1", "c1", T0, attrs={"page": "home"})
    batch = [first, _event("e2", "c1", T0 + timedelta(hours=1)), _event("e1", "c1", T0, attrs={"page": "x"})]
    assert stores.events.append(batch) == 2
    assert stores.events.append([_event("e2", "c9", T0), _event("e3", "c1", T0)]) == 1
    assert stores.events.append([]) == 0
    assert stores.events.append(e for e in [_event("e4", "c2", T0)]) == 1
    timeline = stores.events.for_customer("c1")
    assert [e.event_id for e in timeline] == ["e1", "e3", "e2"]
    assert timeline[0] == first  # the first copy of a repeated id wins
    assert stores.events.for_customer("c9") == []


def test_events_come_back_in_time_order_with_ties_in_write_order(stores: Stores) -> None:
    later = T0 + timedelta(hours=2)
    # 10:00 in India is 04:30 UTC: earlier than T0 despite the bigger wall-clock digits.
    early_ist = datetime(2026, 10, 1, 10, 0, tzinfo=IST)
    events = [
        _event("z", "c1", later),
        _event("b", "c1", T0),
        _event("a", "c1", T0),  # same instant as "b", written after it
        _event("m", "c2", T0 + timedelta(hours=1)),
        _event("ist", "c1", early_ist),
    ]
    stores.events.append(events[:2])
    stores.events.append(events[2:])
    assert [e.event_id for e in stores.events.for_customer("c1")] == ["ist", "b", "a", "z"]
    assert [e.event_id for e in stores.events.scan()] == ["ist", "b", "a", "m", "z"]
    assert stores.events.for_customer("c1")[0].ts.utcoffset() == timedelta(hours=5, minutes=30)


def test_windows_include_since_and_exclude_until(stores: Stores) -> None:
    stores.events.append(_event(f"e{h}", "c1", T0 + timedelta(hours=h)) for h in range(6))
    since, until = T0 + timedelta(hours=1), T0 + timedelta(hours=4)
    assert [e.event_id for e in stores.events.for_customer("c1", since=since, until=until)] == [
        "e1",
        "e2",
        "e3",
    ]
    assert [e.event_id for e in stores.events.scan(since=since)] == ["e1", "e2", "e3", "e4", "e5"]
    assert [e.event_id for e in stores.events.scan(until=until)] == ["e0", "e1", "e2", "e3"]
    assert [e.event_id for e in stores.events.scan(since=since.astimezone(IST), until=until)] == [
        "e1",
        "e2",
        "e3",
    ]


def test_scan_streams_many_events_in_order(stores: Stores) -> None:
    events = [_event(f"e{i:03d}", f"c{i % 7}", T0 + timedelta(minutes=(i * 37) % 50)) for i in range(60)]
    stores.events.append(events)
    expected = [e.event_id for e in sorted(events, key=lambda e: e.ts)]
    assert [e.event_id for e in stores.events.scan()] == expected
    assert len(stores.events) == 60


def test_events_by_decision(stores: Stores) -> None:
    stores.events.append(
        [
            _event("open", "c1", T0 + timedelta(hours=1), EventType.OPEN, "d1"),
            _event("send", "c1", T0, EventType.SEND, "d1"),
            _event("other", "c1", T0, EventType.SEND, "d2"),
            _event("buy", "c1", T0, EventType.PURCHASE, value=19.99, margin=7.5, category="tea"),
        ]
    )
    assert [e.event_id for e in stores.events.for_decision("d1")] == ["send", "open"]
    assert stores.events.for_decision("nope") == []
    # An empty decision_id is "no decision" in the fake's index; the event itself keeps the "".
    stores.events.append([_event("blank", "c2", T0, decision_id="")])
    assert stores.events.for_decision("") == []
    assert stores.events.for_customer("c2")[0].decision_id == ""
    buy = stores.events.for_customer("c1", until=T0 + timedelta(seconds=1))[-1]
    assert (buy.value, buy.margin, buy.category) == (19.99, 7.5, "tea")


# --- customer state ---------------------------------------------------------------------------


def test_state_round_trips_and_replaces(stores: Stores) -> None:
    rich = _state(
        "c1",
        version=12,
        group=Group.HOLDOUT,
        channels={
            Channel.EMAIL: ChannelStats(address_known=True, consent=True, sends=3, last_send_at=T0),
            Channel.WHATSAPP: ChannelStats(unsubscribed=True),
        },
        active_hours=tuple([1.0 / 24] * 24),
        recent_contacts=(T0, T0.astimezone(IST)),
        category_counts={"tea": 2, "cups": 1},
        top_categories=("tea", "cups"),
        scores={"churn_risk": 0.123456789},
        preferences={"no_discounts": "true"},
        revenue=101.1,
    )
    assert stores.states.get("c1") is None
    stores.states.put([rich])
    assert stores.states.get("c1") == rich
    assert stores.states.get("c1").contactable_channels() == (Channel.EMAIL,)
    stores.states.put([_state("c1", version=13), _state("c1", version=14)])  # the last copy wins
    assert stores.states.get("c1").version == 14
    assert stores.states.count() == 1


def test_state_scan_goes_in_code_point_order(stores: Stores) -> None:
    ids = ["b", "B", "a", "_x", "Z9", "z", "é", "A", "10", "9"]
    stores.states.put(_state(c) for c in ids)
    assert [s.customer_id for s in stores.states.scan()] == sorted(ids)
    assert stores.states.count() == len(ids)
    stores.states.put([])
    assert stores.states.count() == len(ids)


# --- decisions --------------------------------------------------------------------------------


def test_decisions_record_get_and_replace_in_place(stores: Stores) -> None:
    d1 = _decision("d1", "c1", T0)
    d2 = _decision("d2", "c1", T0, act=False)
    d3 = _decision("d3", "c2", T0 - timedelta(hours=1))
    stores.decisions.record([d1, d2, d3])
    assert stores.decisions.get("d1") == d1
    assert stores.decisions.get("d2") == d2 and stores.decisions.get("d2").action is None
    assert stores.decisions.get("zz") is None
    assert [d.decision_id for d in stores.decisions.scan()] == ["d3", "d1", "d2"]
    revised = _decision("d1", "c1", T0, reason="revised")
    stores.decisions.record([revised])  # replaced, but keeps its place among equal times
    assert stores.decisions.get("d1") == revised
    assert [d.decision_id for d in stores.decisions.scan()] == ["d3", "d1", "d2"]
    assert [d.decision_id for d in stores.decisions.for_customer("c1")] == ["d1", "d2"]
    assert stores.decisions.for_customer("nobody") == []


def test_decision_scan_window(stores: Stores) -> None:
    stores.decisions.record(_decision(f"d{h}", f"c{h % 2}", T0 + timedelta(hours=h)) for h in range(7))
    got = stores.decisions.scan(since=T0 + timedelta(hours=2), until=T0 + timedelta(hours=5))
    assert [d.decision_id for d in got] == ["d2", "d3", "d4"]
    assert [d.decision_id for d in stores.decisions.scan()] == [f"d{h}" for h in range(7)]


# --- workspace --------------------------------------------------------------------------------


def test_goals_keep_their_order_when_replaced(stores: Stores) -> None:
    ws = stores.workspace
    assert ws.goals() == []
    ws.put_goal(sample_goal(T0, "g_b"))
    ws.put_goal(sample_goal(T0, "g_a"))
    updated = sample_goal(T0, "g_b").model_copy(update={"budget": 99.0})
    ws.put_goal(updated)
    assert [g.goal_id for g in ws.goals()] == ["g_b", "g_a"]
    assert ws.goals()[0] == updated


def test_briefs_return_the_latest_version_per_goal(stores: Stores) -> None:
    ws = stores.workspace
    v1 = sample_brief(T0, "g1")
    v2 = v1.model_copy(update={"version": 2, "rationale": "second"})
    v2b = v1.model_copy(update={"version": 2, "rationale": "second, edited"})
    other = sample_brief(T0, "g0")
    ws.put_brief(v1)
    ws.put_brief(other)
    ws.put_brief(v2)
    ws.put_brief(v1)  # an older version arriving late never wins
    assert ws.briefs() == [v2, other]
    ws.put_brief(v2b)  # the same version again replaces it
    assert ws.briefs("g1") == [v2b]
    assert ws.briefs("nope") == []


def test_content_filters_by_goal_and_approval(stores: Stores) -> None:
    ws = stores.workspace
    library = sample_content(T0, "g1")
    shared = library[0].model_copy(update={"content_id": "shared", "goal_id": None})
    draft = library[1].model_copy(update={"content_id": "draft", "status": ContentStatus.DRAFT})
    elsewhere = library[2].model_copy(update={"content_id": "g2_item", "goal_id": "g2"})
    ws.put_content([*library, shared, draft, elsewhere])
    approved_ids = [c.content_id for c in library]
    assert [c.content_id for c in ws.content()] == [*approved_ids, "shared", "g2_item"]
    assert [c.content_id for c in ws.content(goal_id="g1")] == [*approved_ids, "shared"]
    assert [c.content_id for c in ws.content(goal_id="g2")] == ["shared", "g2_item"]
    assert "draft" in [c.content_id for c in ws.content(goal_id="g1", approved_only=False)]
    retired = library[0].model_copy(update={"status": ContentStatus.RETIRED})
    ws.put_content([retired])
    assert library[0].content_id not in [c.content_id for c in ws.content()]
    everything: list[ContentItem] = ws.content(approved_only=False)
    assert everything[0] == retired  # replaced in place


# --- job queue --------------------------------------------------------------------------------


def test_claim_takes_the_earliest_due_job_of_the_asked_kinds(stores: Stores) -> None:
    q = stores.jobs
    assert q.claim(["decide_sweep"], worker="w1", now=T0) is None
    q.enqueue(_job("j_b", "decide_sweep", T0, day=1))
    q.enqueue(_job("j_a", "decide_sweep", T0))  # same time: the smaller id goes first
    q.enqueue(_job("j_early", "learn_nightly", T0 - timedelta(hours=1)))
    q.enqueue(_job("j_future", "decide_sweep", T0 + timedelta(hours=1)))
    assert q.claim([], worker="w1", now=T0) is None
    first = q.claim(["decide_sweep"], worker="w1", now=T0)
    assert first is not None and first.job_id == "j_a"
    assert (first.status, first.attempts) == (JobStatus.RUNNING, 1)
    second = q.claim(["decide_sweep"], worker="w2", now=T0)
    assert second is not None and second.job_id == "j_b" and second.payload == {"day": 1}
    assert q.claim(["decide_sweep"], worker="w1", now=T0) is None  # j_future is not due yet
    assert q.claim(["decide_sweep", "learn_nightly"], worker="w1", now=T0).job_id == "j_early"
    assert q.claim(["decide_sweep"], worker="w1", now=T0 + timedelta(hours=1)).job_id == "j_future"
    assert q.get("j_a") == first


def test_complete_and_fail_with_and_without_retry(stores: Stores) -> None:
    q = stores.jobs
    q.enqueue(_job("j1", "briefing_daily", T0))
    q.enqueue(_job("j2", "briefing_daily", T0))
    q.enqueue(_job("j3", "briefing_daily", T0))
    j1 = q.claim(["briefing_daily"], worker="w", now=T0)
    q.complete(j1.job_id)
    assert q.get("j1").status is JobStatus.DONE
    q.claim(["briefing_daily"], worker="w", now=T0)
    retry_at = (T0 + timedelta(minutes=5)).astimezone(IST)
    q.fail("j2", "timeout", retry_at=retry_at)
    j2 = q.get("j2")
    assert (j2.status, j2.run_at, j2.last_error, j2.attempts) == (JobStatus.QUEUED, retry_at, "timeout", 1)
    assert j2.run_at.utcoffset() == timedelta(hours=5, minutes=30)
    q.claim(["briefing_daily"], worker="w", now=T0)
    q.fail("j3", "bad payload")
    assert (q.get("j3").status, q.get("j3").last_error) == (JobStatus.FAILED, "bad payload")
    assert q.claim(["briefing_daily"], worker="w", now=T0) is None
    again = q.claim(["briefing_daily"], worker="w", now=T0 + timedelta(minutes=5))
    assert again is not None and (again.job_id, again.attempts) == ("j2", 2)
    with pytest.raises(KeyError):
        q.complete("missing")
    with pytest.raises(KeyError):
        q.fail("missing", "x")
    assert q.get("missing") is None


def test_enqueue_replaces_a_job(stores: Stores) -> None:
    q = stores.jobs
    q.enqueue(_job("j1", "webhook_event", T0))
    q.claim(["webhook_event"], worker="w", now=T0)
    q.enqueue(_job("j1", "webhook_event", T0, retry=True))
    job = q.get("j1")
    assert (job.status, job.attempts, job.payload) == (JobStatus.QUEUED, 0, {"retry": True})


# --- the reference loop -----------------------------------------------------------------------


def test_the_reference_loop_gives_the_same_answer_on_sql(stores: Stores) -> None:
    """The whole loop on these stores matches the loop on the fakes, decision for decision."""

    def run(events: Any, states: Any, decisions: Any) -> list[Decision]:
        return run_days(
            env=FakeEnvironment(n_customers=12, start=T0, history_days=10),
            policy=ContactEveryonePolicy(),
            events=events,
            states=states,
            decisions=decisions,
            folder=SimpleStateFolder(),
            briefs=[sample_brief(T0)],
            content=sample_content(T0),
            days=3,
            seed=7,
        ).decisions

    fake_events, fake_states, fake_log = InMemoryEventStore(), InMemoryStateStore(), InMemoryDecisionLog()
    expected = run(fake_events, fake_states, fake_log)
    got = run(stores.events, stores.states, stores.decisions)
    assert got == expected
    assert list(stores.events.scan()) == list(fake_events.scan())
    assert list(stores.states.scan()) == list(fake_states.scan())
    assert list(stores.decisions.scan()) == list(fake_log.scan())


# --- SQL only ---------------------------------------------------------------------------------


def test_create_all_is_idempotent_and_makes_every_table(sql_engine: Engine) -> None:
    create_all(sql_engine)
    schema = sql_engine.get_execution_options().get("schema_translate_map", {}).get(None)
    names = set(inspect(sql_engine).get_table_names(schema=schema))
    assert {"event", "customer_state", "decision", "goal", "brief", "content_item", "job"} <= names


def test_data_outlives_the_store_objects(sql_engine: Engine) -> None:
    SqlEventStore(sql_engine).append([_event("e1", "c1", T0)])
    SqlStateStore(sql_engine).put([_state("c1")])
    SqlJobQueue(sql_engine).enqueue(_job("j1", "decide_sweep", T0))
    assert SqlEventStore(sql_engine).for_customer("c1")[0].event_id == "e1"
    assert SqlStateStore(sql_engine).get("c1") == _state("c1")
    assert SqlJobQueue(sql_engine).claim(["decide_sweep"], worker="w", now=T0) is not None
    with sql_engine.connect() as conn:
        row = conn.execute(select(event_table.c.ts, event_table.c.type)).one()
    assert row.ts == T0 and row.type == "visit"


def test_a_naive_datetime_is_refused(sql_engine: Engine) -> None:
    with pytest.raises(Exception, match="naive"):
        list(SqlEventStore(sql_engine).scan(since=datetime(2026, 10, 1)))


def test_a_locked_job_is_skipped_on_postgres(sql_engine: Engine) -> None:
    """While one worker holds a job, another claims the next one instead of waiting."""
    if sql_engine.dialect.name != "postgresql":
        pytest.skip("SKIP LOCKED is Postgres only; SQLite claims inside a plain transaction")
    from autonomy.store.schema import job_table

    q = SqlJobQueue(sql_engine)
    q.enqueue(_job("j1", "decide_sweep", T0))
    q.enqueue(_job("j2", "decide_sweep", T0))
    with sql_engine.begin() as holder:
        holder.execute(select(job_table.c.job_id).where(job_table.c.job_id == "j1").with_for_update())
        claimed = q.claim(["decide_sweep"], worker="w2", now=T0)
        assert claimed is not None and claimed.job_id == "j2"


def test_memory_engine_is_shared_between_stores() -> None:
    engine = memory_engine()
    create_all(engine)
    SqlEventStore(engine).append([_event("e1", "c1", T0)])
    assert len(SqlEventStore(engine)) == 1
