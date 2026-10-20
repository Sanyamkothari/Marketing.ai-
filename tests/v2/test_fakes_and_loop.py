"""The fakes satisfy their protocols, and the reference loop runs end to end on the fake world."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autonomy import interfaces as ifc
from autonomy.contracts import Channel, DecisionKind, EventType, Job, JobStatus
from autonomy.fakes import (
    ContactEveryonePolicy,
    FakeChannelAdapter,
    FakeEnvironment,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryPriorStore,
    InMemoryStateStore,
    InMemoryWorkspace,
    KeywordTextSignals,
    NothingPolicy,
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.loop import run_days

START = datetime(2026, 10, 1, tzinfo=UTC)


def test_fakes_satisfy_the_protocols() -> None:
    assert isinstance(InMemoryEventStore(), ifc.EventStore)
    assert isinstance(InMemoryStateStore(), ifc.StateStore)
    assert isinstance(InMemoryDecisionLog(), ifc.DecisionLog)
    assert isinstance(InMemoryWorkspace(), ifc.Workspace)
    assert isinstance(InMemoryJobQueue(), ifc.JobQueue)
    assert isinstance(InMemoryPriorStore(), ifc.PriorStore)
    assert isinstance(SimpleStateFolder(), ifc.StateFolder)
    assert isinstance(NothingPolicy(), ifc.Policy)
    assert isinstance(FakeEnvironment(start=START), ifc.Environment)
    assert isinstance(FakeChannelAdapter(Channel.EMAIL), ifc.ChannelAdapter)
    assert isinstance(KeywordTextSignals(), ifc.TextSignals)


def _run(policy: ifc.Policy, seed: int = 1) -> tuple[InMemoryEventStore, InMemoryStateStore, list]:
    events, states, log = InMemoryEventStore(), InMemoryStateStore(), InMemoryDecisionLog()
    result = run_days(
        env=FakeEnvironment(n_customers=60, start=START, history_days=20),
        policy=policy,
        events=events,
        states=states,
        decisions=log,
        folder=SimpleStateFolder(),
        briefs=[sample_brief(START)],
        content=sample_content(START),
        days=7,
        seed=seed,
    )
    return events, states, result.decisions


def test_loop_runs_and_repeats_exactly() -> None:
    e1, s1, d1 = _run(ContactEveryonePolicy())
    e2, _, d2 = _run(ContactEveryonePolicy())
    assert len(d1) == 60 * 7 and [d.decision_id for d in d1] == [d.decision_id for d in d2]
    assert len(e1) == len(e2)
    sends = [e for e in e1.scan() if e.type is EventType.SEND]
    assert sends and all(e.decision_id for e in sends)
    assert any(s.contacts_7d > 0 for s in s1.scan())


def test_nothing_policy_sends_nothing() -> None:
    events, _, decisions = _run(NothingPolicy())
    assert all(d.kind is DecisionKind.NOTHING for d in decisions)
    assert not [e for e in events.scan() if e.type is EventType.SEND]


def test_event_store_ignores_repeats() -> None:
    env = FakeEnvironment(n_customers=5, start=START, history_days=3)
    history = env.reset(0)
    store = InMemoryEventStore()
    assert store.append(history) == len(history)
    assert store.append(history) == 0


def test_job_queue_claims_due_jobs_once() -> None:
    q = InMemoryJobQueue()
    q.enqueue(Job(job_id="j1", kind="decide_sweep", run_at=START))
    q.enqueue(Job(job_id="j2", kind="decide_sweep", run_at=START + timedelta(days=1)))
    job = q.claim(["decide_sweep"], worker="w", now=START)
    assert job is not None and job.job_id == "j1" and job.status is JobStatus.RUNNING
    assert q.claim(["decide_sweep"], worker="w", now=START) is None
    q.fail("j1", "boom", retry_at=START + timedelta(hours=1))
    assert q.claim(["decide_sweep"], worker="w", now=START + timedelta(hours=1)) is not None
