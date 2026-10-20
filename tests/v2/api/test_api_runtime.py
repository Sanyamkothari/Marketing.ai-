"""The API's runtime endpoints: batches a person signs off, the operator's tick, and real-runtime wiring.

Most tests run over HTTP with FastAPI's test client (skipped where FastAPI or httpx is missing; the
v2 CI job installs both). The batch rules are checked with an in-memory `BatchStore`, the tick
endpoint with a stub runtime, and `build_services_from_runtime` with a real `AutonomyRuntime` on
the in-memory fakes: a goal at APPROVE holds its contacts in a batch, the API approves it, and the
next tick sends it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from api.v2.deps import (
    APPROVER,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    Services,
    build_demo_services,
    build_services_from_runtime,
)
from autonomy.contracts import (
    Alert,
    AutonomyLevel,
    Batch,
    BatchStatus,
    Briefing,
    Channel,
    Event,
    EventType,
    GoalKind,
    GoalStatus,
)
from autonomy.runtime.batches import InMemoryBatchStore

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def demo() -> Services:
    return build_demo_services(NOW, n_customers=30, days=2)


def _goal_id(services: Services) -> str:
    return services.list_goals()[0].goal_id


def _batch(batch_id: str, goal_id: str, hours_ago: int, status: BatchStatus = BatchStatus.PENDING) -> Batch:
    return Batch(
        batch_id=batch_id,
        goal_id=goal_id,
        created_at=NOW - timedelta(hours=hours_ago),
        decision_ids=(f"dec_{batch_id}_1", f"dec_{batch_id}_2"),
        n_contacts=2,
        expected_net=3.5,
        expected_spend=0.02,
        summary="2 customers by email (2), no offer (2).",
        status=status,
    )


@pytest.fixture
def services(demo: Services) -> Services:
    """The demo with three batches: two pending (one older) and one already sent."""
    gid = _goal_id(demo)
    store = InMemoryBatchStore(
        [
            _batch("batch_old", gid, hours_ago=5),
            _batch("batch_new", gid, hours_ago=1),
            _batch("batch_sent", gid, hours_ago=30, status=BatchStatus.SENT),
        ]
    )
    return replace(demo, batch_store=store)


def _client(services: Services) -> Any:
    pytest.importorskip("fastapi", reason="FastAPI is not installed here; the v2 CI job runs these")
    pytest.importorskip("httpx", reason="the FastAPI test client needs httpx")
    from fastapi.testclient import TestClient

    from api.v2.app import create_app

    return TestClient(create_app(services))


@pytest.fixture
def client(services: Services) -> Iterator[Any]:
    with _client(services) as c:
        yield c


# --- batches, without HTTP --------------------------------------------------------------------------


def test_batch_rules(services: Services) -> None:
    gid = _goal_id(services)
    assert [b.batch_id for b in services.list_batches()] == ["batch_new", "batch_old", "batch_sent"]
    assert [b.batch_id for b in services.list_batches(gid, BatchStatus.PENDING)] == ["batch_new", "batch_old"]
    with pytest.raises(NotFoundError):
        services.list_batches("goal_nobody")

    approved = services.approve_batch("batch_old")
    assert approved.status is BatchStatus.APPROVED
    assert approved.decided_by == APPROVER and approved.decided_at == NOW
    with pytest.raises(ConflictError):
        services.approve_batch("batch_old")  # a second click changes nothing
    with pytest.raises(ConflictError):
        services.reject_batch("batch_sent")
    with pytest.raises(NotFoundError):
        services.approve_batch("batch_nobody")


def test_without_a_batch_store_there_are_no_batches(demo: Services) -> None:
    assert demo.batch_store is None
    assert demo.list_batches() == []
    with pytest.raises(NotFoundError):
        demo.reject_batch("batch_old")


# --- batches over HTTP ------------------------------------------------------------------------------


def test_batch_list_approve_reject_flow(client: Any, services: Services) -> None:
    gid = _goal_id(services)
    r = client.get("/v2/batches", params={"goal_id": gid})
    assert r.status_code == 200
    rows = [Batch.model_validate(b) for b in r.json()]
    assert [b.batch_id for b in rows] == ["batch_new", "batch_old", "batch_sent"]

    pending = client.get("/v2/batches", params={"status": "pending"}).json()
    assert [b["batch_id"] for b in pending] == ["batch_new", "batch_old"]

    r = client.post("/v2/batches/batch_new/approve")
    assert r.status_code == 200
    body = Batch.model_validate(r.json())
    assert body.status is BatchStatus.APPROVED and body.decided_by == APPROVER

    r = client.post("/v2/batches/batch_old/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    # Decided batches cannot be decided again, and the store agrees with what was answered.
    assert client.post("/v2/batches/batch_new/reject").status_code == 409
    assert client.post("/v2/batches/batch_old/approve").status_code == 409
    assert services.batch_store is not None
    stored = services.batch_store.get("batch_new")
    assert stored is not None and stored.status is BatchStatus.APPROVED
    assert client.get("/v2/batches", params={"status": "pending"}).json() == []


def test_batch_404s(client: Any) -> None:
    for path in ("/v2/batches/batch_nobody/approve", "/v2/batches/batch_nobody/reject"):
        r = client.post(path)
        assert r.status_code == 404 and "batch_nobody" in r.json()["detail"]
    assert client.get("/v2/batches", params={"goal_id": "goal_nobody"}).status_code == 404
    assert client.get("/v2/batches", params={"status": "maybe"}).status_code == 422


# --- the operator tick ------------------------------------------------------------------------------


@dataclass
class StubReport:
    now: datetime
    batch: Batch
    alerts: list[Alert] = field(default_factory=list)
    briefings: list[Briefing] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "now": self.now.isoformat(),
            "decisions": 3,
            "acts": 2,
            "sent": 1,
            "queued": 0,
            "failed": 0,
            "skipped": {"quiet_hours": 1},  # extra keys a runtime reports are dropped
            "events_folded": 4,
            "batches": [self.batch.model_dump(mode="json")],
            "batches_sent": [],
            "batches_expired": [],
            "alerts": [a.model_dump(mode="json") for a in self.alerts],
            "paused_goals": [],
            "paused_channels": ["sms"],
            "swept_goals": [self.batch.goal_id],
            "nightly_ran": False,
            "weekly": [],
        }


@dataclass
class StubRuntime:
    goal_id: str
    ticks: list[datetime] = field(default_factory=list)

    def tick(self, now: datetime) -> StubReport:
        self.ticks.append(now)
        alert = Alert(alert_id="alert_1", at=now, severity="warning", code="breaker.test", message="Test.")
        return StubReport(now, _batch("batch_tick", self.goal_id, hours_ago=0), alerts=[alert])


def test_tick_endpoint_runs_the_runtime(demo: Services) -> None:
    stub = StubRuntime(_goal_id(demo))
    services = replace(demo, runtime=stub, allow_tick=True)
    with _client(services) as client:
        r = client.post("/v2/runtime/tick", json={"now": "2026-10-21T09:00:00+00:00"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["decisions"] == 3 and body["sent"] == 1 and body["acts"] == 2
        assert [b["batch_id"] for b in body["batches"]] == ["batch_tick"]
        assert [a["code"] for a in body["alerts"]] == ["breaker.test"]
        assert body["paused_channels"] == [Channel.SMS.value]
        assert "skipped" not in body and "weekly" not in body

        assert client.post("/v2/runtime/tick").status_code == 200  # no body: the API's clock
        assert client.post("/v2/runtime/tick", json={}).status_code == 200
        assert stub.ticks == [datetime(2026, 10, 21, 9, 0, tzinfo=UTC), NOW, NOW]

        r = client.post("/v2/runtime/tick", json={"now": "2026-10-21T09:00:00"})  # no time zone
        assert r.status_code == 422
        assert len(stub.ticks) == 3


def test_tick_404_without_runtime_and_403_when_turned_off(demo: Services) -> None:
    with _client(demo) as client:
        r = client.post("/v2/runtime/tick")
        assert r.status_code == 404 and "runtime" in r.json()["detail"]
    stub = StubRuntime(_goal_id(demo))
    with pytest.raises(ForbiddenError):
        replace(demo, runtime=stub).tick()
    with _client(replace(demo, runtime=stub)) as client:
        assert client.post("/v2/runtime/tick").status_code == 403
    assert stub.ticks == []


# --- a real runtime behind the API ------------------------------------------------------------------

T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a Monday, at the runtime's default 10:00 sweep
CUSTOMERS = ("c1", "c2", "c3")


def _history() -> list[Event]:
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
                category="shoes",
            )
        )
    return out


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def _runtime_services(clock: Clock) -> tuple[Services, Any]:
    from autonomy.channels.dispatcher import Dispatcher
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
    from autonomy.runtime.service import AutonomyRuntime

    events = InMemoryEventStore()
    events.append(_history())
    workspace = InMemoryWorkspace()
    start = T0 - timedelta(days=1)
    goal = sample_goal(start).model_copy(
        update={"autonomy": AutonomyLevel.APPROVE, "status": GoalStatus.ACTIVE}
    )
    workspace.put_goal(goal)
    workspace.put_brief(sample_brief(start))
    workspace.put_content(sample_content(start))
    adapter = FakeChannelAdapter(Channel.EMAIL)
    queue = InMemoryJobQueue()
    policy = ContactEveryonePolicy()

    def address(customer_id: str, channel: Channel) -> str:
        return f"{customer_id}@example.com"

    def policies(briefs: Sequence[Any], content: Sequence[Any]) -> ContactEveryonePolicy:
        return policy

    runtime = AutonomyRuntime(
        events=events,
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        workspace=workspace,
        folder=SimpleStateFolder(),
        policy_factory=policies,
        queue=queue,
        dispatcher=Dispatcher({Channel.EMAIL: adapter}, address, workspace, events, queue),
        batches=InMemoryBatchStore(),
    )
    return build_services_from_runtime(runtime, clock=clock, business_id="shop", allow_tick=True), adapter


def test_real_runtime_holds_a_batch_until_the_api_approves_it() -> None:
    clock = Clock(T0)
    services, adapter = _runtime_services(clock)
    assert services.adapters.keys() == {Channel.EMAIL}
    gid = _goal_id(services)
    with _client(services) as client:
        first = client.post("/v2/runtime/tick").json()
        assert first["decisions"] == len(CUSTOMERS) and first["sent"] == 0
        assert len(first["batches"]) == 1
        batch_id = first["batches"][0]["batch_id"]
        assert adapter.sent == []

        listed = client.get("/v2/batches", params={"goal_id": gid, "status": "pending"}).json()
        assert [b["batch_id"] for b in listed] == [batch_id]
        assert len(client.get("/v2/decisions", params={"goal_id": gid}).json()) == len(CUSTOMERS)

        clock.now = T0 + timedelta(minutes=5)
        assert client.post(f"/v2/batches/{batch_id}/approve").json()["status"] == "approved"
        assert adapter.sent == []  # approving sends nothing; the next tick does

        clock.now = T0 + timedelta(minutes=10)
        second = client.post("/v2/runtime/tick").json()
        assert second["batches_sent"] == [batch_id] and second["sent"] == len(CUSTOMERS)
        assert len(adapter.sent) == len(CUSTOMERS)
        assert client.get("/v2/batches").json()[0]["status"] == "sent"

        # The read endpoints answer from the runtime's stores.
        assert client.get("/v2/customers/c1").status_code == 200
        health = client.get("/v2/health").json()
        assert health["contacts_per_customer_week"] == 1.0
        assert client.get("/v2/proof", params={"goal_id": gid}).status_code == 200
        assert client.get("/v2/proof", params={"goal_id": "goal_nobody"}).json() is None
        assert client.get("/v2/briefing").json() is None


def test_runtime_webhooks_append_and_the_tick_folds_them() -> None:
    """A callback is stored at once and folded by the runtime's next tick, so it never counts twice."""
    clock = Clock(T0)
    services, _ = _runtime_services(clock)
    services.tick()
    before = services.states.get("c1")
    assert before is not None
    buy = Event(
        event_id="late_buy_c1",
        customer_id="c1",
        type=EventType.PURCHASE,
        ts=T0 + timedelta(minutes=1),
        source="test",
        value=25.0,
    )
    assert services.ingest([buy]) == 1
    assert services.ingest([buy]) == 0
    unchanged = services.states.get("c1")
    assert unchanged is not None and unchanged.purchases == before.purchases

    clock.now = T0 + timedelta(minutes=5)
    services.tick()
    clock.now = T0 + timedelta(minutes=10)
    services.tick()
    after = services.states.get("c1")
    assert after is not None and after.purchases == before.purchases + 1


def test_runtime_read_models_dedupe_alerts_and_skip_goals_in_progress() -> None:
    clock = Clock(T0)
    services, _ = _runtime_services(clock)
    services.tick()  # folds the history into customer state
    assert GoalKind.REPEAT_PURCHASE in {o.kind for o in services.opportunities()}
    assert GoalKind.WIN_BACK not in {o.kind for o in services.opportunities()}  # already a goal
    repeat = services.list_goals()[0].model_copy(
        update={"goal_id": "goal_repeat", "kind": GoalKind.REPEAT_PURCHASE, "status": GoalStatus.DRAFT}
    )
    services.workspace.put_goal(repeat)
    assert GoalKind.REPEAT_PURCHASE not in {o.kind for o in services.opportunities()}

    # A tick repeated at the same moment raises the same alert ids; /v2/alerts shows each once.
    recording: Any = services.runtime
    recording.inner = StubRuntime(_goal_id(services))
    services.tick()
    services.tick()
    assert [a.alert_id for a in services.alerts()] == ["alert_1"]
    assert [a.alert_id for a in services.health().alerts] == ["alert_1"]


# --- the simulation-backed demo ---------------------------------------------------------------------


def test_simulated_demo_is_served_and_keeps_ticking() -> None:
    services = build_demo_services(NOW, n_customers=120, days=3, simulation=True)
    assert services.runtime is not None and services.allow_tick and services.batch_store is not None
    goals = services.list_goals()
    assert {g.autonomy for g in goals} == {AutonomyLevel.AUTOPILOT, AutonomyLevel.APPROVE}
    assert next(services.decisions.scan(), None) is not None
    assert any(a.code == "demo_data" for a in services.alerts())
    assert services.briefing(None) is not None
    # The simulation ends at the last 09:00 UTC sweep before now; nothing is stamped after now.
    assert max(e.ts for e in services.events.scan()) <= NOW

    report = services.tick()
    assert report["decisions"] > 0
    approve_goal = next(g.goal_id for g in goals if g.autonomy is AutonomyLevel.APPROVE)
    pending = services.list_batches(approve_goal, BatchStatus.PENDING)
    assert pending, "the APPROVE goal's contacts wait in a batch for the marketer"
    assert [b["batch_id"] for b in report["batches"]] == [b.batch_id for b in pending]

    # Budgets are hard limits: the API's runtime starts from what the simulation already spent.
    runtime: Any = services.runtime
    spent = {g.goal_id: runtime.inner.spend_fn(g.goal_id, NOW) for g in goals}
    assert all(v > 0 for v in spent.values()), spent
    assert runtime.inner.breakers_fn is not None


def test_simulated_demo_falls_back_to_the_fake_world(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "autonomy.runtime.demo", None)  # importing it now fails
    services = build_demo_services(NOW, n_customers=30, days=2, simulation=True)
    assert services.runtime is None and services.batch_store is None
    assert [g.status for g in services.list_goals()] == [GoalStatus.ACTIVE]
