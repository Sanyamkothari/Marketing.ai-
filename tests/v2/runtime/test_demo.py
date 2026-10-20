"""The end-to-end demo (Gym world + agents + runtime + worker) and the worker's job handling."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from autonomy.channels import Dispatcher
from autonomy.channels.dispatcher import DispatchReport, DispatchResult, DispatchStatus
from autonomy.contracts import (
    AutonomyLevel,
    BatchStatus,
    Briefing,
    Channel,
    ContentStatus,
    DecisionKind,
    Event,
    EventType,
    Group,
    Job,
    JobStatus,
    ProofSummary,
)
from autonomy.fakes import (
    FakeChannelAdapter,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryStateStore,
)
from autonomy.runtime.demo import DemoResult, DemoStores, build_system, live_demo_worker, run_demo
from autonomy.runtime.worker import (
    SEND_KIND,
    TICK_KIND,
    WEBHOOK_KIND,
    PermanentJobError,
    Worker,
    next_tick_at,
    retry_delay,
    send_handler,
    tick_handler,
    tick_job,
    webhook_handler,
    webhook_job,
)

T0 = datetime(2026, 3, 2, 9, tzinfo=UTC)


@pytest.fixture(scope="module")
def demo() -> DemoResult:
    return run_demo(scenario="baseline_shop", days=10, customers=300, seed=0)


# --- the demo ---------------------------------------------------------------------------------------


def test_demo_makes_decisions_with_probabilities(demo: DemoResult) -> None:
    assert demo.decisions
    assert all(0 < d.probability <= 1 for d in demo.decisions)
    acts = [d for d in demo.decisions if d.kind is DecisionKind.ACT]
    assert acts and all(d.action is not None for d in acts)
    assert all(d.reason for d in demo.decisions)
    assert all(d.kind is DecisionKind.NOTHING for d in demo.decisions if d.group is Group.HOLDOUT)


def test_demo_produces_briefings_proof_and_no_rule_breaks(demo: DemoResult) -> None:
    daily = [b for b in demo.briefings if b.period == "daily"]
    assert len(daily) >= 1 and all(isinstance(b, Briefing) and b.headline for b in daily)
    assert isinstance(demo.proof, ProofSummary)
    assert demo.violations == []
    assert demo.health.delivery_rate is not None


def test_agents_built_an_approved_library_and_two_active_goals(demo: DemoResult) -> None:
    assert {g.autonomy for g in demo.goals} == {AutonomyLevel.AUTOPILOT, AutonomyLevel.APPROVE}
    assert all(b.approved and b.created_by == "strategist:template" for b in demo.briefs)
    assert demo.content and all(c.status is ContentStatus.APPROVED for c in demo.content)


def test_every_send_traces_to_one_act_decision_and_lands_in_the_gym(demo: DemoResult) -> None:
    by_id = {d.decision_id: d for d in demo.decisions}
    sends = [e for e in demo.events if e.type is EventType.SEND]
    assert sends and len(sends) == demo.sent == demo.contacts
    per_decision = Counter(e.decision_id for e in sends)
    assert max(per_decision.values()) == 1
    for e in sends:
        decision = by_id[str(e.decision_id)]
        assert decision.kind is DecisionKind.ACT and decision.group is not Group.HOLDOUT
    delivered = {e.decision_id for e in demo.events if e.type is EventType.DELIVER}
    assert delivered == set(per_decision)


def test_approve_goal_sends_only_through_approved_batches(demo: DemoResult) -> None:
    approve_goal = next(g for g in demo.goals if g.autonomy is AutonomyLevel.APPROVE)
    batches = [b for b in demo.batches if b.goal_id == approve_goal.goal_id]
    assert batches and all(b.status is BatchStatus.SENT and b.decided_by for b in batches)
    in_batches = {i for b in batches for i in b.decision_ids}
    sent_for_goal = {
        e.decision_id
        for e in demo.events
        if e.type is EventType.SEND and e.attrs.get("goal_id") == approve_goal.goal_id
    }
    assert sent_for_goal and sent_for_goal <= in_batches


def test_demo_repeats_exactly_and_persists_into_given_stores() -> None:
    stores = DemoStores()
    first = run_demo(days=3, customers=120, seed=7, stores=stores)
    second = run_demo(days=3, customers=120, seed=7)
    assert [d.decision_id for d in first.decisions] == [d.decision_id for d in second.decisions]
    assert [d.probability for d in first.decisions] == [d.probability for d in second.decisions]
    assert stores.decisions.get(first.decisions[0].decision_id) is not None
    assert stores.states.count() == 120
    assert first.stores is stores


# --- the worker -------------------------------------------------------------------------------------


def _job(kind: str = "flaky") -> Job:
    return Job(job_id=f"job_{kind}", kind=kind, run_at=T0)


def test_retry_delay_doubles_up_to_the_cap() -> None:
    assert retry_delay(1) == timedelta(minutes=1)
    assert retry_delay(3) == timedelta(minutes=4)
    assert retry_delay(10) == timedelta(hours=1)
    assert retry_delay(500) == timedelta(hours=1)


def test_failing_job_is_retried_with_backoff_then_stops() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue(_job())
    calls: list[datetime] = []

    def boom(job: Job, now: datetime) -> None:
        calls.append(now)
        raise RuntimeError("provider down")

    worker = Worker(queue, {"flaky": boom}, max_attempts=3)
    first = worker.run_once(T0)
    assert first is not None and first.status == "retry" and first.retry_at == T0 + timedelta(minutes=1)
    assert worker.run_once(T0) is None  # not due again yet
    second = worker.run_once(T0 + timedelta(minutes=1))
    assert second is not None and second.retry_at == T0 + timedelta(minutes=3)
    third = worker.run_once(T0 + timedelta(minutes=3))
    assert third is not None and third.status == "failed" and third.attempts == 3
    stored = queue.get("job_flaky")
    assert stored is not None and stored.status is JobStatus.FAILED
    assert stored.last_error == "RuntimeError: provider down"
    assert len(calls) == 3


def test_permanent_error_fails_without_retry_and_success_completes() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue(_job("bad"))
    queue.enqueue(_job("good"))

    def bad(job: Job, now: datetime) -> None:
        raise PermanentJobError("signature does not match")

    worker = Worker(queue, {"bad": bad, "good": lambda job, now: None})
    outcomes = {o.kind: o for o in worker.drain(T0)}
    assert outcomes["bad"].status == "failed" and outcomes["good"].status == "done"
    good = queue.get("job_good")
    assert good is not None and good.status is JobStatus.DONE


def test_repeating_tick_queues_its_successor() -> None:
    queue = InMemoryJobQueue()
    ticks: list[datetime] = []
    worker = Worker(queue, {TICK_KIND: tick_handler(ticks.append, queue)})
    queue.enqueue(tick_job(T0, every_minutes=60))
    worker.drain(T0 + timedelta(minutes=5))  # a late worker keeps the schedule
    worker.drain(T0 + timedelta(minutes=65))
    assert ticks == [T0 + timedelta(minutes=5), T0 + timedelta(minutes=65)]
    nxt = queue.get(tick_job(T0 + timedelta(hours=2)).job_id)
    assert nxt is not None and nxt.status is JobStatus.QUEUED


def test_webhook_job_is_verified_parsed_and_ingested_once() -> None:
    adapter = FakeChannelAdapter(Channel.EMAIL)
    store = InMemoryEventStore()

    def ingest(events: Sequence[Event]) -> int:
        return store.append(events)

    queue = InMemoryJobQueue()
    worker = Worker(queue, {WEBHOOK_KIND: webhook_handler({Channel.EMAIL: adapter}, ingest)})
    body = json.dumps(
        {
            "type": "click",
            "message_id": "m1",
            "decision_id": "dec_1",
            "customer_id": "c1",
            "ts": T0.isoformat(),
        }
    ).encode()
    queue.enqueue(webhook_job(Channel.EMAIL, {"X-Fake-Signature": "test-secret"}, body, received_at=T0))
    queue.enqueue(webhook_job(Channel.EMAIL, {"X-Fake-Signature": "wrong"}, body, received_at=T0))
    outcomes = worker.drain(T0)
    assert sorted(o.status for o in outcomes) == ["done", "failed"]
    assert [e.decision_id for e in store.scan()] == ["dec_1"]


def test_a_failing_tick_keeps_the_schedule_and_retries_do_not_fork_it() -> None:
    queue = InMemoryJobQueue()

    def broken(now: datetime) -> None:
        raise RuntimeError("database down")

    worker = Worker(queue, {TICK_KIND: tick_handler(broken, queue)}, max_attempts=2)
    queue.enqueue(tick_job(T0, every_minutes=60))
    first = worker.run_once(T0)
    assert first is not None and first.status == "retry"
    second = worker.run_once(T0 + timedelta(minutes=1))  # the retry runs off the schedule
    assert second is not None and second.status == "failed"
    successor = queue.get(tick_job(T0 + timedelta(hours=1)).job_id)
    assert successor is not None and successor.status is JobStatus.QUEUED  # on the original schedule
    assert queue.get(tick_job(T0 + timedelta(minutes=61)).job_id) is None  # the retry started no chain


def test_a_worker_far_behind_skips_missed_ticks() -> None:
    every = timedelta(minutes=5)
    assert next_tick_at(T0, every, T0) == T0 + every
    assert next_tick_at(T0, every, T0 + timedelta(minutes=4)) == T0 + every
    assert next_tick_at(T0, every, T0 + timedelta(hours=24, minutes=2)) == T0 + timedelta(hours=24, minutes=5)
    queue = InMemoryJobQueue()
    ticks: list[datetime] = []
    worker = Worker(queue, {TICK_KIND: tick_handler(ticks.append, queue)})
    queue.enqueue(tick_job(T0, every_minutes=5))
    worker.drain(T0 + timedelta(days=1, minutes=2))  # down for a day: one tick, not 289
    assert ticks == [T0 + timedelta(days=1, minutes=2)]


class _FailingDispatcher:
    def run_send_job(self, job: Job, decisions: object, states: object, now: datetime) -> DispatchReport:
        failed = DispatchResult(
            decision_id="dec_1", customer_id="c1", status=DispatchStatus.FAILED, reason="provider 503"
        )
        return DispatchReport(results=[failed])


def test_a_provider_failure_is_retried_by_the_send_handler() -> None:
    queue = InMemoryJobQueue()
    handler = send_handler(
        cast(Dispatcher, _FailingDispatcher()), InMemoryDecisionLog(), InMemoryStateStore()
    )
    worker = Worker(queue, {SEND_KIND: handler})
    queue.enqueue(Job(job_id="job_send", kind=SEND_KIND, payload={"decision_id": "dec_1"}, run_at=T0))
    outcome = worker.run_once(T0)
    assert outcome is not None and outcome.status == "retry"
    assert outcome.error is not None and "provider 503" in outcome.error


def test_live_demo_ticks_hourly_decides_after_the_sweep_hour_and_briefs_once_a_day() -> None:
    start = datetime(2026, 1, 5, tzinfo=UTC)
    seeded = live_demo_worker(start=start, customers=40)
    assert seeded.run_once(start) is not None  # the seed tick job is queued at `start`

    system = build_system(customers=40, start=start, live=True)
    system.stores.queue.enqueue(tick_job(start, every_minutes=60))
    for hour in range(12):
        system.worker.drain(start + timedelta(hours=hour))
    ticks = [o for o in system.worker.outcomes if o.kind == TICK_KIND]
    assert len(ticks) == 12 and all(o.status == "done" for o in ticks)
    assert system.cycle.reports[-1].now == start + timedelta(hours=11)
    made = [d.made_at for d in system.stores.decisions.scan()]
    assert made and min(made) == start + timedelta(hours=9)  # nothing before the daily sweep
    assert [b.period for b in system.stores.briefings].count("daily") == 1
