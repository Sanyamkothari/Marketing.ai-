"""The v2 API over the demo services: the rules in `Services`, then every endpoint over HTTP.

The first half needs only pydantic and starlette. The HTTP half needs FastAPI (and httpx for the
test client); where they are missing it skips with that reason, and the v2 CI job installs both.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from api.v2.deps import (
    DEMO_SECRET,
    BadRequestError,
    ConflictError,
    NotFoundError,
    Services,
    UnauthorizedError,
    build_demo_services,
    infer_goal_kind,
    template_strategist,
)
from autonomy.contracts import (
    Action,
    Alert,
    AutonomyLevel,
    Bid,
    Brief,
    Briefing,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    Decision,
    DecisionKind,
    Event,
    Goal,
    GoalKind,
    GoalStatus,
    HealthStatus,
    ProofSummary,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
OPENAPI_FILE = Path(__file__).resolve().parents[3] / "docs" / "v2" / "openapi.json"
SIGNED = {"x-fake-signature": DEMO_SECRET}


@pytest.fixture
def services() -> Services:
    return build_demo_services(NOW, n_customers=60, days=5)


def _acted(services: Services) -> Decision:
    return next(d for d in services.decisions.scan() if d.kind is DecisionKind.ACT)


def _click_body(decision: Decision, message_id: str = "m1") -> bytes:
    assert decision.action is not None
    return json.dumps(
        {
            "type": "click",
            "message_id": message_id,
            "decision_id": decision.decision_id,
            "customer_id": decision.customer_id,
            "ts": (NOW + timedelta(minutes=5)).isoformat(),
        }
    ).encode()


# --- the services, without HTTP -------------------------------------------------------------------


def test_demo_has_real_looking_data(services: Services) -> None:
    (goal,) = services.list_goals()
    assert goal.status is GoalStatus.ACTIVE
    brief = services.latest_brief(goal.goal_id)
    assert brief is not None and brief.approved
    statuses = {c.status for c in services.goal_content(goal.goal_id)}
    assert statuses == {ContentStatus.APPROVED, ContentStatus.DRAFT}
    assert services.states.count() == 60
    kinds = {d.kind for d in services.decisions.scan()}
    assert kinds == {DecisionKind.ACT, DecisionKind.NOTHING}
    proof = services.proof(None)
    assert proof is not None and proof.agent_n + proof.baseline_n + proof.holdout_n == 60
    assert services.proof("goal_unknown") is None
    assert services.briefing(goal.goal_id) is not None
    assert services.health().delivery_rate is not None
    assert any(a.code == "demo_data" for a in services.alerts())
    assert all(o.status is GoalStatus.DRAFT for o in services.opportunities())


def test_the_demo_is_deterministic() -> None:
    a = build_demo_services(NOW, n_customers=30, days=3)
    b = build_demo_services(NOW, n_customers=30, days=3)
    assert list(a.decisions.scan()) == list(b.decisions.scan())
    assert a.proof(None) == b.proof(None)


def test_create_goal_stores_a_draft_and_an_unapproved_brief(services: Services) -> None:
    goal, brief = services.create_goal("  Recover abandoned carts this month ", 800, "eur")
    assert goal.text == "Recover abandoned carts this month"
    assert goal.kind is GoalKind.CART_RECOVERY
    assert goal.status is GoalStatus.DRAFT and goal.autonomy is AutonomyLevel.SHADOW
    assert goal.currency == "EUR" and goal.created_at == NOW
    assert brief.goal_id == goal.goal_id and not brief.approved
    assert brief.envelope.budget == 800
    assert services.list_goals()[0] == goal
    again, _ = services.create_goal("Recover abandoned carts this month", 800, "EUR")
    assert again.goal_id != goal.goal_id
    with pytest.raises(BadRequestError):
        services.create_goal("   ", 10)
    with pytest.raises(BadRequestError):  # JSON's 1e999 parses to infinity; it must never be stored
        services.create_goal("Sell more", float("inf"))
    assert all(math.isfinite(g.budget) for g in services.list_goals())


def test_infer_goal_kind_and_template_brief() -> None:
    assert infer_goal_kind("Win back customers who haven’t bought in 60 days") is GoalKind.WIN_BACK
    assert infer_goal_kind("Welcome new signups") is GoalKind.ONBOARDING
    assert infer_goal_kind("Sell more socks") is GoalKind.CUSTOM
    goal = Goal(goal_id="g1", business_id="b", text="t", budget=5, starts_at=NOW, created_at=NOW)
    brief = template_strategist(goal)
    assert brief == template_strategist(goal)
    assert brief.created_at == NOW and brief.audience.filters == ()


def test_approve_then_pause_and_resume(services: Services) -> None:
    goal, _ = services.create_goal("Get one-time buyers to buy again", 1000)
    with pytest.raises(ConflictError):
        services.resume(goal.goal_id)  # not approved yet
    with pytest.raises(
        ConflictError
    ):  # Autopilot needs proof (docs/v4/CALIBRATION.md); tests/v4/calibrate covers the gate
        services.approve(goal.goal_id, AutonomyLevel.AUTOPILOT)
    active, brief = services.approve(goal.goal_id, AutonomyLevel.APPROVE)
    assert active.status is GoalStatus.ACTIVE and active.autonomy is AutonomyLevel.APPROVE
    assert brief.approved_at == NOW and brief.approved_by == "marketer"
    assert services.latest_brief(goal.goal_id) == brief
    assert services.pause(goal.goal_id).status is GoalStatus.PAUSED
    assert services.resume(goal.goal_id).status is GoalStatus.ACTIVE
    with pytest.raises(NotFoundError):
        services.approve("goal_missing", AutonomyLevel.SHADOW)


def test_content_status_and_decision_filters(services: Services) -> None:
    item = services.set_content_status("ci_draft_1", ContentStatus.APPROVED)
    assert item.status is ContentStatus.APPROVED
    with pytest.raises(NotFoundError):
        services.set_content_status("ci_missing", ContentStatus.REJECTED)
    newest = services.recent_decisions(limit=5)
    assert len(newest) == 5
    assert [d.made_at for d in newest] == sorted((d.made_at for d in newest), reverse=True)
    goal_rows = services.recent_decisions(goal_id="goal_winback", limit=1000)
    assert goal_rows and all(d.action is not None for d in goal_rows)
    cid = newest[0].customer_id
    assert {d.customer_id for d in services.recent_decisions(customer_id=cid)} == {cid}
    assert services.recent_decisions(customer_id="") == []  # an empty id filters, it is not "everyone"


def test_goal_filter_keeps_decisions_that_only_weighed_a_bid(services: Services) -> None:
    """A NOTHING decision that considered a goal's bid belongs in that goal's activity feed."""
    action = Action(goal_id="goal_other", channel=Channel.EMAIL, send_at=NOW)
    bid = Bid(goal_id="goal_other", action=action, uplift=0.01, value_if_converted=10, expected_net=0.1)
    weighed = Decision(
        decision_id="dec_weighed",
        customer_id="c_x",
        made_at=NOW + timedelta(hours=1),
        kind=DecisionKind.NOTHING,
        probability=1.0,
        reason="The bid did not beat the attention price.",
        bids=(bid,),
        policy_version="test",
    )
    services.decisions.record([weighed])
    assert [d.decision_id for d in services.recent_decisions(goal_id="goal_other")] == ["dec_weighed"]
    assert "dec_weighed" not in {d.decision_id for d in services.recent_decisions(goal_id="goal_winback")}


def test_webhook_is_verified_stored_once_and_folded(services: Services) -> None:
    decision = _acted(services)
    assert decision.action is not None
    channel = decision.action.channel
    before = services.states.get(decision.customer_id)
    assert before is not None
    body = _click_body(decision)
    assert services.receive_webhook(channel.value, {"X-Fake-Signature": DEMO_SECRET}, body) == 1
    assert services.receive_webhook(channel.value, SIGNED, body) == 0  # a retried callback
    after = services.states.get(decision.customer_id)
    assert after is not None
    assert after.channels[channel].clicks == before.channels[channel].clicks + 1
    with pytest.raises(UnauthorizedError):
        services.receive_webhook(channel.value, {"x-fake-signature": "wrong"}, body)
    with pytest.raises(NotFoundError):
        services.receive_webhook("pigeon", SIGNED, body)
    with pytest.raises(NotFoundError):
        services.receive_webhook(Channel.SMS.value, SIGNED, body)  # a channel with no adapter
    with pytest.raises(BadRequestError):
        services.receive_webhook(channel.value, SIGNED, b"not json")


# --- over HTTP ------------------------------------------------------------------------------------


@pytest.fixture
def client(services: Services) -> Iterator[Any]:
    pytest.importorskip("fastapi", reason="FastAPI is not installed here; the v2 CI job runs these")
    pytest.importorskip("httpx", reason="the FastAPI test client needs httpx")
    from fastapi.testclient import TestClient

    from api.v2.app import create_app

    with TestClient(create_app(services)) as c:
        yield c


def test_goal_endpoints(client: Any) -> None:
    r = client.get("/v2/goals")
    assert r.status_code == 200
    goals = [Goal.model_validate(g) for g in r.json()]
    assert [g.goal_id for g in goals] == ["goal_winback"]

    r = client.get("/v2/goals/goal_winback")
    assert r.status_code == 200
    detail = r.json()
    assert set(detail) == {"goal", "brief", "content", "proof"}
    Goal.model_validate(detail["goal"])
    assert Brief.model_validate(detail["brief"]).approved
    assert len([ContentItem.model_validate(c) for c in detail["content"]]) == 6
    ProofSummary.model_validate(detail["proof"])

    r = client.get("/v2/goals/goal_winback/content")
    assert r.status_code == 200
    assert {ContentItem.model_validate(c).content_id for c in r.json()} >= {"ci_0", "ci_draft_1"}

    assert client.get("/v2/goals/goal_missing").status_code == 404
    assert client.get("/v2/goals/goal_missing/content").status_code == 404


def test_create_approve_pause_resume_flow(client: Any) -> None:
    r = client.post("/v2/goals", json={"text": "Win back lapsed buyers", "budget": 2500, "currency": "USD"})
    assert r.status_code == 200
    created = r.json()
    assert set(created) == {"goal", "brief"}
    goal = Goal.model_validate(created["goal"])
    assert goal.status is GoalStatus.DRAFT and goal.kind is GoalKind.WIN_BACK
    assert not Brief.model_validate(created["brief"]).approved
    gid = goal.goal_id

    detail = client.get(f"/v2/goals/{gid}").json()
    assert detail["content"] == [] and detail["proof"] is None

    assert client.post(f"/v2/goals/{gid}/resume").status_code == 409  # not approved yet
    r = client.post(f"/v2/goals/{gid}/approve", json={"autonomy": "approve"})
    assert r.status_code == 200
    approved = r.json()
    assert approved["goal"]["status"] == "active" and approved["goal"]["autonomy"] == "approve"
    assert Brief.model_validate(approved["brief"]).approved_at == NOW

    r = client.post(f"/v2/goals/{gid}/pause")
    assert r.status_code == 200 and Goal.model_validate(r.json()).status is GoalStatus.PAUSED
    r = client.post(f"/v2/goals/{gid}/resume")
    assert r.status_code == 200 and Goal.model_validate(r.json()).status is GoalStatus.ACTIVE

    assert client.post("/v2/goals", json={"text": "", "budget": 10}).status_code == 422
    assert client.post("/v2/goals", json={"text": "x", "budget": -1}).status_code == 422
    infinite = b'{"text": "x", "budget": 1e999}'  # Python's JSON parser reads this as infinity
    r = client.post("/v2/goals", content=infinite, headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert client.get("/v2/goals").status_code == 200
    assert client.post(f"/v2/goals/{gid}/approve", json={"autonomy": "yolo"}).status_code == 422
    assert client.post("/v2/goals/goal_missing/approve", json={"autonomy": "shadow"}).status_code == 404
    assert client.post("/v2/goals/goal_missing/pause").status_code == 404


def test_content_status_endpoint(client: Any) -> None:
    r = client.post("/v2/content/ci_draft_1/status", json={"status": "approved"})
    assert r.status_code == 200
    assert ContentItem.model_validate(r.json()).status is ContentStatus.APPROVED
    assert client.post("/v2/content/ci_missing/status", json={"status": "approved"}).status_code == 404
    assert client.post("/v2/content/ci_0/status", json={"status": "maybe"}).status_code == 422
    # Nothing goes out without passing the editor: rejected or unchecked copy cannot be approved.
    assert client.post("/v2/content/ci_0/status", json={"status": "rejected"}).status_code == 200
    r = client.post("/v2/content/ci_0/status", json={"status": "approved"})
    assert r.status_code == 409 and "editor" in r.text


def test_decisions_and_customer_endpoints(client: Any) -> None:
    r = client.get("/v2/decisions", params={"limit": 7})
    assert r.status_code == 200
    rows = [Decision.model_validate(d) for d in r.json()]
    assert len(rows) == 7 and all(d.reason for d in rows)
    cid = rows[0].customer_id

    r = client.get("/v2/decisions", params={"goal_id": "goal_winback", "customer_id": cid, "limit": 50})
    assert r.status_code == 200
    assert {Decision.model_validate(d).customer_id for d in r.json()} <= {cid}
    assert client.get("/v2/decisions", params={"limit": 0}).status_code == 422

    r = client.get(f"/v2/customers/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"state", "decisions", "events"}
    assert CustomerState.model_validate(body["state"]).customer_id == cid
    assert [Decision.model_validate(d).customer_id for d in body["decisions"]]
    assert [Event.model_validate(e).customer_id for e in body["events"]]
    assert client.get("/v2/customers/nobody").status_code == 404


def test_reporting_endpoints(client: Any) -> None:
    r = client.get("/v2/briefing", params={"goal_id": "goal_winback"})
    assert r.status_code == 200 and Briefing.model_validate(r.json()).goal_id == "goal_winback"
    r = client.get("/v2/briefing", params={"goal_id": "goal_other"})
    assert r.status_code == 200 and r.json() is None

    r = client.get("/v2/proof")
    assert r.status_code == 200 and ProofSummary.model_validate(r.json()).goal_id is None
    r = client.get("/v2/proof", params={"goal_id": "goal_other"})
    assert r.status_code == 200 and r.json() is None

    r = client.get("/v2/health")
    assert r.status_code == 200 and HealthStatus.model_validate(r.json()).alerts

    r = client.get("/v2/alerts")
    assert r.status_code == 200 and [Alert.model_validate(a) for a in r.json()]

    r = client.get("/v2/opportunities")
    assert r.status_code == 200
    assert all(Goal.model_validate(g).status is GoalStatus.DRAFT for g in r.json())


def test_webhook_endpoint(client: Any, services: Services) -> None:
    decision = _acted(services)
    assert decision.action is not None
    url = f"/v2/webhooks/{decision.action.channel.value}"
    body = _click_body(decision, message_id="m-http")
    r = client.post(url, content=body, headers={"X-Fake-Signature": DEMO_SECRET})
    assert r.status_code == 200 and r.json() == {"accepted": 1}
    r = client.post(url, content=body, headers=SIGNED)
    assert r.status_code == 200 and r.json() == {"accepted": 0}
    assert any(
        e.decision_id == decision.decision_id for e in services.events.for_customer(decision.customer_id)
    )
    assert client.post(url, content=body, headers={"x-fake-signature": "nope"}).status_code == 401
    assert client.post(url, content=body).status_code == 401
    assert client.post("/v2/webhooks/pigeon", content=body, headers=SIGNED).status_code == 404
    assert client.post(url, content=b"{", headers=SIGNED).status_code == 400


def test_cors_allows_localhost_only(client: Any) -> None:
    ok = client.get("/v2/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    other = client.get("/v2/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_ui_is_served_next_to_the_api(client: Any, services: Services, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from api.v2.app import create_app

    (tmp_path / "index.html").write_text("<h1>Autonomy</h1>", encoding="utf-8")
    with TestClient(create_app(services, ui_dir=tmp_path)) as ui:
        r = ui.get("/v2/app/")
        assert r.status_code == 200 and "Autonomy" in r.text
        assert ui.get("/v2/goals").status_code == 200  # the API under the same prefix still answers
    assert client  # the shared fixture only gates this test on FastAPI being installed


def test_published_openapi_matches_the_routes(client: Any, tmp_path: Path) -> None:
    from api.v2.app import export_openapi

    live = export_openapi(tmp_path / "openapi.json", client.app)
    assert json.loads((tmp_path / "openapi.json").read_text()) == live
    published = json.loads(OPENAPI_FILE.read_text())

    def ops(schema: dict[str, Any]) -> set[tuple[str, str]]:
        return {(path, method) for path, item in schema["paths"].items() for method in item}

    expected = {
        ("/v2/goals", "get"),
        ("/v2/goals", "post"),
        ("/v2/goals/{goal_id}", "get"),
        ("/v2/goals/{goal_id}/approve", "post"),
        ("/v2/goals/{goal_id}/pause", "post"),
        ("/v2/goals/{goal_id}/resume", "post"),
        ("/v2/goals/{goal_id}/content", "get"),
        ("/v2/content/{content_id}/status", "post"),
        ("/v2/decisions", "get"),
        ("/v2/customers/{customer_id}", "get"),
        ("/v2/briefing", "get"),
        ("/v2/proof", "get"),
        ("/v2/health", "get"),
        ("/v2/alerts", "get"),
        ("/v2/opportunities", "get"),
        ("/v2/webhooks/{channel}", "post"),
        ("/v2/batches", "get"),
        ("/v2/batches/{batch_id}/approve", "post"),
        ("/v2/batches/{batch_id}/reject", "post"),
        ("/v2/runtime/tick", "post"),
        # What PITCH.md's promises need (api/v2/insights.py; docs/v2/PROMISE_MAP.md "Data for each screen").
        ("/v2/goals/read", "post"),
        ("/v2/goals/{goal_id}", "patch"),
        ("/v2/goals/{goal_id}/plan", "get"),
        ("/v2/pause", "post"),
        ("/v2/activity", "get"),
        ("/v2/customers/{customer_id}/insight", "get"),
        ("/v2/today", "get"),
        ("/v2/expected", "get"),
        ("/v2/health/limits", "get"),
        ("/v2/data/status", "get"),
        ("/v2/channels", "get"),
        ("/v2/opportunities/sized", "get"),
        ("/v2/batches/{batch_id}/breakdown", "get"),
        # The SaaS connectors (api/v2/connectors.py; docs/v2/CONNECTORS.md): connect once, then health in words.
        ("/v2/connectors", "get"),
        ("/v2/connectors/connections", "get"),
        ("/v2/connectors/connections/{connection_id}", "get"),
        ("/v2/connectors/connections/{connection_id}", "delete"),
        ("/v2/connectors/connections/{connection_id}/sync", "post"),
        ("/v2/connectors/connections/{connection_id}/setup", "get"),
        ("/v2/connectors/webhooks/{kind}", "post"),
        ("/v2/connectors/webhooks/{kind}/{connection_id}", "post"),
        ("/v2/connectors/{kind}/connect", "post"),
        ("/v2/connectors/{kind}/callback", "get"),
        ("/v2/connectors/{kind}/install", "get"),
        ("/v2/connectors/{kind}/key", "post"),
        # Zero-touch setup (docs/v2/ZERO_TOUCH.md): sized goals, learned limits, the brand voice, the
        # exceptions-only inbox and Shadow -> Autopilot in one click.
        ("/v2/onboarding/proposals", "get"),
        ("/v2/onboarding/limits", "get"),
        ("/v2/onboarding/answers", "post"),
        ("/v2/onboarding/accept", "post"),
        ("/v2/onboarding/margin", "get"),
        ("/v2/onboarding/margin", "post"),
        ("/v2/onboarding/mandates", "get"),
        ("/v2/onboarding/brand", "get"),
        ("/v2/onboarding/brand/read", "post"),
        ("/v2/onboarding/brand/approve", "post"),
        ("/v2/inbox", "get"),
        ("/v2/inbox/{item_id}/act", "post"),
        ("/v2/inbox/history", "get"),
        ("/v2/inbox/data", "get"),
        ("/v2/autopilot/ledger", "get"),
        ("/v2/autopilot/{goal_id}", "get"),
        ("/v2/autopilot/{goal_id}/turn-on", "post"),
    }
    # The marketer app's routes are all there; the standalone engines (/v2/engines/...) add their own.
    assert expected <= ops(live)
    assert {op for op in ops(live) - expected if not op[0].startswith("/v2/engines")} == set()
    assert ops(published) == ops(live), "docs/v2/openapi.json is stale: run `python -m api.v2.app --openapi`"
    assert set(published["components"]["schemas"]) >= {"Goal", "Brief", "Decision", "ProofSummary"}
