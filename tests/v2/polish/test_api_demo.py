"""The API's loose ends from Wave 2: batch error codes, alerts and briefings served from stores, and
the demo's costs, late APPROVE goal and pending batches.

The HTTP tests use FastAPI's test client (skipped where FastAPI or httpx is missing; the v2 CI job
installs both). The demo tests run a small Gym shop, a few seconds each.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from api.v2.deps import (
    Services,
    StoreReadModels,
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
    EventType,
)
from autonomy.fakes import InMemoryDecisionLog, InMemoryEventStore, InMemoryStateStore, InMemoryWorkspace
from autonomy.runtime.batches import InMemoryBatchStore

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _client(services: Services) -> Any:
    pytest.importorskip("fastapi", reason="FastAPI is not installed here; the v2 CI job runs these")
    pytest.importorskip("httpx", reason="the FastAPI test client needs httpx")
    from fastapi.testclient import TestClient

    from api.v2.app import create_app

    return TestClient(create_app(services))


@pytest.fixture(scope="module")
def demo() -> Services:
    return build_demo_services(NOW, n_customers=30, days=2)


def _batch(batch_id: str, goal_id: str, status: BatchStatus = BatchStatus.PENDING) -> Batch:
    return Batch(
        batch_id=batch_id,
        goal_id=goal_id,
        created_at=NOW - timedelta(hours=1),
        decision_ids=(f"dec_{batch_id}",),
        n_contacts=1,
        status=status,
    )


@pytest.fixture
def client(demo: Services) -> Iterator[Any]:
    gid = demo.list_goals()[0].goal_id
    store = InMemoryBatchStore(
        [_batch("batch_a", gid), _batch("batch_b", gid), _batch("batch_sent", gid, BatchStatus.SENT)]
    )
    with _client(replace(demo, batch_store=store)) as c:
        yield c


# --- batch routes -----------------------------------------------------------------------------------


def test_unknown_batch_is_a_404_that_names_it(client: Any) -> None:
    for action in ("approve", "reject"):
        r = client.post(f"/v2/batches/batch_nobody/{action}", json={})
        assert r.status_code == 404
        # Named, so the app can tell it from a bare "Not Found" for a route the server lacks.
        assert "batch_nobody" in r.json()["detail"] and r.json()["detail"] != "Not Found"


def test_a_decided_batch_is_a_409_that_says_what_happened(client: Any) -> None:
    assert client.post("/v2/batches/batch_a/approve", json={}).status_code == 200
    for action in ("approve", "reject"):
        r = client.post(f"/v2/batches/batch_a/{action}", json={})
        assert r.status_code == 409
        assert "already approved" in r.json()["detail"]
    r = client.post("/v2/batches/batch_sent/approve", json={})
    assert r.status_code == 409 and "already sent" in r.json()["detail"]


def test_batch_decisions_take_an_empty_body_or_none(client: Any) -> None:
    r = client.post("/v2/batches/batch_a/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = client.post("/v2/batches/batch_b/reject")  # no body at all
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    # A body is still checked: an unknown key is a typo, not something to ignore.
    assert client.post("/v2/batches/batch_sent/approve", json={"note": "x"}).status_code == 422


def test_openapi_documents_the_batch_errors(client: Any) -> None:
    op = client.get("/openapi.json").json()["paths"]["/v2/batches/{batch_id}/approve"]["post"]
    assert {"404", "409"} <= set(op["responses"])
    assert "requestBody" in op and not op["requestBody"].get("required", False)


# --- alerts and briefings from stores -----------------------------------------------------------------


def _alert(alert_id: str, hours_ago: int, severity: str = "warning") -> Alert:
    return Alert(
        alert_id=alert_id,
        at=NOW - timedelta(hours=hours_ago),
        severity=severity,
        code=f"code_{alert_id}",
        message=f"Alert {alert_id}.",
    )


def _briefing(briefing_id: str, goal_id: str | None, hours_ago: int) -> Briefing:
    return Briefing(
        briefing_id=briefing_id,
        goal_id=goal_id,
        as_of=NOW - timedelta(hours=hours_ago),
        period="daily",
        headline=f"Briefing {briefing_id}.",
    )


@dataclass
class AlertStoreStub:
    """Shaped like `autonomy.runtime.stores`' alert store: `list` with optional filters."""

    rows: list[Alert]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def list(self, *, goal_id: str | None = None, limit: int = 100) -> list[Alert]:
        self.calls.append({"goal_id": goal_id, "limit": limit})
        return sorted(self.rows, key=lambda a: a.at, reverse=True)[:limit]


@dataclass
class BriefingStoreStub:
    rows: list[Briefing]

    def latest(self, goal_id: str | None = None) -> Briefing | None:
        mine = [b for b in self.rows if b.goal_id == goal_id]
        return max(mine, key=lambda b: b.as_of) if mine else None


@dataclass
class BriefingListOnly:
    """A store with `list(goal)` but no `latest`, and a differently named argument."""

    rows: list[Briefing]

    def list(self, goal: str | None) -> Sequence[Briefing]:
        return [b for b in self.rows if b.goal_id == goal]


def _reads(**stores: Any) -> StoreReadModels:
    return StoreReadModels(
        workspace=InMemoryWorkspace(),
        events=InMemoryEventStore(),
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        clock=lambda: NOW,
        business_id="demo",
        alerts_log=[_alert("logged", 5), _alert("both", 3)],
        briefings_log=[_briefing("brf_logged", None, 10)],
        **stores,
    )


def test_alerts_merge_the_store_with_what_ticks_reported() -> None:
    store = AlertStoreStub([_alert("stored", 1), _alert("both", 3)])
    reads = _reads(alert_store=store)
    assert [a.alert_id for a in reads.alerts()] == ["stored", "both", "logged"]
    assert store.calls and store.calls[0]["limit"] >= 100


def test_briefing_is_the_newest_from_the_store_or_the_log() -> None:
    reads = _reads(briefing_store=BriefingStoreStub([_briefing("brf_new", None, 1)]))
    briefing = reads.briefing(None)
    assert briefing is not None and briefing.briefing_id == "brf_new"
    assert reads.briefing("goal_other") is None
    # An older stored briefing does not hide a newer one a tick reported.
    reads = _reads(briefing_store=BriefingStoreStub([_briefing("brf_old", None, 20)]))
    briefing = reads.briefing(None)
    assert briefing is not None and briefing.briefing_id == "brf_logged"


def test_a_store_with_only_list_and_its_own_argument_names_still_works() -> None:
    reads = _reads(briefing_store=BriefingListOnly([_briefing("brf_g", "goal_1", 0)]))
    briefing = reads.briefing("goal_1")
    assert briefing is not None and briefing.briefing_id == "brf_g"


def test_without_stores_the_logs_are_served_as_before() -> None:
    reads = _reads()
    assert [a.alert_id for a in reads.alerts()] == ["both", "logged"]
    briefing = reads.briefing(None)
    assert briefing is not None and briefing.briefing_id == "brf_logged"


@dataclass
class RuntimeStub:
    """The attributes `build_services_from_runtime` reads, plus the stores a runtime may carry."""

    alert_store: Any
    briefing_store: Any
    workspace: InMemoryWorkspace = field(default_factory=InMemoryWorkspace)
    events: InMemoryEventStore = field(default_factory=InMemoryEventStore)
    states: InMemoryStateStore = field(default_factory=InMemoryStateStore)
    decisions: InMemoryDecisionLog = field(default_factory=InMemoryDecisionLog)
    batches: InMemoryBatchStore = field(default_factory=InMemoryBatchStore)
    dispatcher: None = None

    def tick(self, now: datetime) -> Any:  # pragma: no cover - not ticked here
        raise AssertionError("not ticked in this test")


def test_the_api_serves_the_runtime_stores_over_http() -> None:
    runtime: Any = RuntimeStub(
        alert_store=AlertStoreStub([_alert("stored", 1, "critical")]),
        briefing_store=BriefingStoreStub([_briefing("brf_goal", "goal_x", 2), _briefing("brf_all", None, 1)]),
    )
    services = build_services_from_runtime(runtime, clock=lambda: NOW, adapters={})
    with _client(services) as c:
        assert [a["alert_id"] for a in c.get("/v2/alerts").json()] == ["stored"]
        assert c.get("/v2/briefing").json()["briefing_id"] == "brf_all"
        assert c.get("/v2/briefing", params={"goal_id": "goal_x"}).json()["briefing_id"] == "brf_goal"
        assert c.get("/v2/briefing", params={"goal_id": "goal_y"}).json() is None
        assert [a["alert_id"] for a in c.get("/v2/health").json()["alerts"]] == ["stored"]


def test_the_runtime_stores_module_is_read_as_it_is_written() -> None:
    """The real in-memory stores (`autonomy.runtime.stores`), found on a runtime by attribute name."""
    stores = pytest.importorskip("autonomy.runtime.stores")
    alert_store = stores.InMemoryAlertStore([_alert("stored", 1), _alert("older", 9)])
    briefing_store = stores.InMemoryBriefingStore([_briefing("brf_all", None, 1), _briefing("brf_g", "g", 2)])
    runtime: Any = RuntimeStub(alert_store=alert_store, briefing_store=briefing_store)
    services = build_services_from_runtime(runtime, clock=lambda: NOW, adapters={})
    assert [a.alert_id for a in services.alerts()] == ["stored", "older"]
    all_goals, one_goal = services.briefing(None), services.briefing("g")
    assert all_goals is not None and all_goals.briefing_id == "brf_all"
    assert one_goal is not None and one_goal.briefing_id == "brf_g"


# --- the demo --------------------------------------------------------------------------------------


def test_the_demo_prices_messages_like_real_providers() -> None:
    from autonomy.runtime.demo import DEMO_CHANNEL_COST

    assert DEMO_CHANNEL_COST[Channel.EMAIL] == pytest.approx(0.001)
    assert 0.03 <= DEMO_CHANNEL_COST[Channel.WHATSAPP] <= 0.05


@pytest.fixture(scope="module")
def story() -> Services:
    """The API's demo, small: the APPROVE goal starts late and its last batch waits for a person."""
    return build_demo_services(
        NOW, n_customers=150, days=6, simulation=True, approve_goal_days=3, hold_batches=True
    )


def test_the_demo_starts_the_approve_goal_late_and_leaves_a_batch_pending(story: Services) -> None:
    goals = {g.autonomy: g for g in story.list_goals()}
    autopilot, approve = goals[AutonomyLevel.AUTOPILOT], goals[AutonomyLevel.APPROVE]
    assert approve.starts_at - autopilot.starts_at == timedelta(days=3)
    assert story.list_goals()[0] == approve  # the newer goal is listed first

    pending = story.list_batches(approve.goal_id, BatchStatus.PENDING)
    assert pending and all(b.created_at <= NOW for b in pending)
    assert all(b.created_at >= approve.starts_at for b in story.list_batches(approve.goal_id))
    # Earlier batches were approved and sent; nothing from the pending one was sent.
    sent = {
        e.decision_id
        for e in story.events.scan()
        if e.type is EventType.SEND and e.attrs.get("goal_id") == approve.goal_id
    }
    assert not sent & {i for b in pending for i in b.decision_ids}

    with _client(story) as c:
        r = c.post(f"/v2/batches/{pending[0].batch_id}/approve", json={})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        assert c.post(f"/v2/batches/{pending[0].batch_id}/reject", json={}).status_code == 409


def test_the_demo_proof_prices_spend_with_the_demo_costs(story: Services) -> None:
    from autonomy.runtime.demo import DEMO_CHANNEL_COST

    sends = [e for e in story.events.scan() if e.type is EventType.SEND and e.channel is not None]
    assert sends
    reads: Any = story.runtime
    costs = reads.reads.costs
    assert costs is not None and dict(costs.channel) == DEMO_CHANNEL_COST


def test_a_new_goal_says_its_outcomes_are_not_in_yet() -> None:
    """With outcome windows closed for the first goal only, the late goal's proof says why it is empty."""
    services = build_demo_services(
        NOW, n_customers=120, days=17, simulation=True, approve_goal_days=2, hold_batches=True
    )
    approve = next(g for g in services.list_goals() if g.autonomy is AutonomyLevel.APPROVE)
    proof = services.proof(approve.goal_id)
    assert proof is not None and proof.incremental_profit is None
    assert any("none of this goal's decisions" in n for n in proof.notes)


def test_the_late_goal_is_paced_from_its_own_start(story: Services) -> None:
    """Planned spend for spend pace counts from each goal's start: a goal added three days ago has
    three days of budget planned, not the whole run's, so the breaker can still catch it overspending."""
    goals = {g.autonomy: g for g in story.list_goals()}
    autopilot, approve = goals[AutonomyLevel.AUTOPILOT], goals[AutonomyLevel.APPROVE]
    recording: Any = story.runtime
    planned = recording.inner.breakers_fn.planned_fn
    at = approve.starts_at + timedelta(days=1)
    assert planned(approve.goal_id, approve.starts_at) == pytest.approx(0.0)
    assert planned(approve.goal_id, at) == pytest.approx(approve.budget / 30)
    assert planned(autopilot.goal_id, at) == pytest.approx(autopilot.budget * 4 / 30)


def test_the_demo_refuses_an_approve_goal_that_never_starts() -> None:
    from autonomy.runtime.demo import run_demo

    with pytest.raises(ValueError, match="approve_goal_days"):
        run_demo(days=3, customers=20, approve_goal_days=0)
