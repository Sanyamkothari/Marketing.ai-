"""Review fixes for the margin question and the proof in the shop's money (docs/v4/ONBOARDING_MARGIN.md)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.deps import Services, attach_services, build_services_from_runtime
from api.v2.engines import measurement
from api.v2.engines.measurement import PROFIT_LOSS_NOTE, MeasurementSummary
from api.v2.honest_proof import goal_decisions, withhold_measurement_profit
from api.v2.onboarding import MARGIN_NOT_SET_NOTE, SETTINGS_SOURCE, MarginStatus
from autonomy.contracts import (
    Action,
    AutonomyLevel,
    Channel,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Group,
)
from autonomy.fakes.samples import sample_goal
from autonomy.onboarding import LearnedLimits
from autonomy.onboarding.limits import MARGIN_QUESTION
from autonomy.onboarding.opportunities import ORDERS_SOURCE
from autonomy.proof import Costs
from autonomy.runtime.prices import ShopPrices
from autonomy.runtime.promotion import AutopilotGuard, InMemoryAutonomyLedger
from autonomy.runtime.settings import RuntimeSettings, build_runtime
from autonomy.store import memory_engine
from tests.v3.currency.test_currency import onboarding_client

ROOT = Path(__file__).resolve().parents[3]
MOCK = ROOT / "ui" / "v2" / "mock"
T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
INR_PRICES = ShopPrices(
    currency="INR",
    channel_costs={Channel.EMAIL: 0.12, Channel.SMS: 0.2, Channel.WHATSAPP: 0.85},
    typical_order_value=1800,
)
ORDERS_MARGIN = 0.3


def _margin_row(limits: dict[str, Any]) -> dict[str, Any]:
    return next(row for row in limits["limits"] if row["key"] == "margin")


def _margin_shop_client() -> tuple[TestClient, Services, Any]:
    """The onboarding client on a shop whose every order carries a 30% margin of its own."""
    from autonomy.fakes.stores import InMemoryEventStore

    client, services, desk = onboarding_client(INR_PRICES)
    events = [
        (
            e.model_copy(update={"margin": round(ORDERS_MARGIN * e.value, 2)})
            if e.type is EventType.PURCHASE and e.value
            else e
        )
        for e in services.events.scan()
    ]
    store = InMemoryEventStore()
    store.append(events)
    services.events = store
    return client, services, desk


# --- the orders' margins keep sizing the goal and the offer floor ----------------------------------


def test_a_typed_margin_never_replaces_the_orders_margins_for_sizing_or_the_floor() -> None:
    client, services, _ = _margin_shop_client()
    with client:
        before = client.get("/v2/onboarding/limits", params={"offers": "true"}).json()
        row = _margin_row(before)
        assert row["source"] == ORDERS_SOURCE and row["question"] == MARGIN_QUESTION
        assert before["terms"]["margin_rate"] == pytest.approx(ORDERS_MARGIN)
        scan_before = client.get("/v2/onboarding/proposals").json()

        assert client.post("/v2/onboarding/margin", json={"margin_pct": 70}).status_code == 200
        after = client.get("/v2/onboarding/limits", params={"offers": "true"}).json()
        assert after["terms"]["margin_rate"] == before["terms"]["margin_rate"]
        assert after["terms"]["margin_floor_pct"] == before["terms"]["margin_floor_pct"]
        row = _margin_row(after)
        assert row["question"] is None and after["questions"] == []
        assert "30%" in row["found"] and "70% from your answer" in row["found"]
        scan_after = client.get("/v2/onboarding/proposals").json()
        assert scan_after["margin"]["source"] == ORDERS_SOURCE
        assert [p["monthly_profit"] for p in scan_after["proposals"]] == [
            p["monthly_profit"] for p in scan_before["proposals"]
        ]
        assert services.margin_rate == 0.7  # the proof's rate for purchases with no margin

        ok = client.post("/v2/onboarding/accept", json={"opportunity_id": "opp_win_back"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["limits"]["terms"]["margin_rate"] == pytest.approx(ORDERS_MARGIN)


def test_the_margin_setting_is_named_as_the_setting_on_the_limits_row() -> None:
    client, services, _ = onboarding_client(INR_PRICES)
    services.margin_rate = 0.42  # AUTONOMY_MARGIN_RATE
    with client:
        limits = client.get("/v2/onboarding/limits").json()
    row = _margin_row(limits)
    assert row["source"] == SETTINGS_SOURCE and row["question"] is None
    assert row["found"] == f"42% gross margin from {SETTINGS_SOURCE}."
    assert "your answer" not in row["found"]
    assert limits["terms"]["margin_rate"] == 0.42


# --- after a mandate is signed ---------------------------------------------------------------------


def test_after_a_mandate_is_signed_the_margin_can_be_lowered_but_not_raised() -> None:
    client, services, desk = onboarding_client(INR_PRICES)
    with client:
        ok = client.post("/v2/onboarding/accept", json={"opportunity_id": "opp_win_back", "margin_pct": 40})
        assert ok.status_code == 200, ok.text
        assert desk.signed and services.margin_rate == 0.4
        for route in ("/v2/onboarding/margin", "/v2/onboarding/answers"):
            r = client.post(route, json={"margin_pct": 55})
            assert r.status_code == 409 and "can only be lowered" in r.json()["detail"]
        again = client.post(
            "/v2/onboarding/accept", json={"opportunity_id": "opp_vip_at_risk", "margin_pct": 60}
        )
        assert again.status_code == 409
        assert services.margin_rate == 0.4 and desk.margin_rate == 0.4
        lower = client.post("/v2/onboarding/margin", json={"margin_pct": 30})
        assert lower.status_code == 200 and lower.json()["margin_rate"] == 0.3
        assert services.margin_rate == 0.3


def test_the_answer_reaches_the_live_runtime() -> None:
    runtime = build_runtime(RuntimeSettings.from_env({}), engine=memory_engine())
    services = build_services_from_runtime(runtime, clock=lambda: T0)
    inner: Any = getattr(services.runtime, "inner", services.runtime)
    before: float | None = inner.margin_rate
    assert before is None
    services.set_margin_rate(0.25)
    after: float | None = inner.margin_rate
    assert after == 0.25 and services.margin_rate == 0.25


def _guard(services: Services) -> AutopilotGuard:
    return AutopilotGuard(
        business_id=services.business_id,
        workspace=services.workspace,
        decisions=services.decisions,
        events=services.events,
        ledger=InMemoryAutonomyLedger(),
    )


def test_the_answer_reaches_the_autopilot_guard_and_drops_its_cached_judgements() -> None:
    client, services, _ = onboarding_client(INR_PRICES)
    guard = _guard(services)
    guard._proof[("g1", AutonomyLevel.AUTOPILOT)] = object()  # type: ignore[assignment]  # judged at the old margin
    client.app.state.inbox_services = SimpleNamespace(zero_touch=SimpleNamespace(guard=guard))
    with client:
        assert client.post("/v2/onboarding/margin", json={"margin_pct": 35}).status_code == 200
    assert guard.margin_rate == 0.35 and guard._proof == {}


def test_a_refused_accept_keeps_no_margin() -> None:
    client, services, desk = onboarding_client(INR_PRICES)
    with client:
        r = client.post("/v2/onboarding/accept", json={"opportunity_id": "opp_nope", "margin_pct": 40})
        assert r.status_code == 404
        assert services.margin_rate is None and desk.margin_rate is None


# --- one goal's proof is not withheld for another goal's channel ------------------------------------


def _act(did: str, cid: str, goal_id: str, channel: Channel, at: datetime) -> Decision:
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.ACT,
        group=Group.AGENT,
        action=Action(goal_id=goal_id, channel=channel, send_at=at),
        probability=0.5,
        reason="test",
        policy_version="t",
    )


def _two_goal_services() -> Services:
    """Goal g1 emailed 30 customers (priced) who bought; goal g2 sent WhatsApp, which has no price, later."""
    env = {
        "AUTONOMY_MARGIN_RATE": "0.35",
        "AUTONOMY_CURRENCY": "INR",
        "AUTONOMY_CHANNEL_COSTS": "email=0.12",
        "AUTONOMY_TYPICAL_ORDER_VALUE": "1800",
    }
    runtime = build_runtime(RuntimeSettings.from_env(env), engine=memory_engine())
    late = T0 + timedelta(days=55)
    acts = [_act(f"d1-{i}", f"a-{i}", "g1", Channel.EMAIL, T0) for i in range(30)]
    acts += [_act(f"d2-{i}", f"b-{i}", "g2", Channel.WHATSAPP, late) for i in range(30)]
    holdout = [
        Decision(
            decision_id=f"h-{i}",
            customer_id=f"h-{i}",
            made_at=T0,
            kind=DecisionKind.NOTHING,
            group=Group.HOLDOUT,
            probability=1.0,
            reason="test",
            policy_version="t",
        )
        for i in range(30)
    ]
    runtime.decisions.record([*acts, *holdout])
    runtime.events.append(
        Event(
            event_id=f"s-{d.decision_id}",
            customer_id=d.customer_id,
            type=EventType.SEND,
            ts=d.made_at + timedelta(minutes=1),
            source="dispatcher",
            channel=d.action.channel if d.action is not None else None,
            decision_id=d.decision_id,
        )
        for d in acts
    )
    runtime.events.append(
        Event(
            event_id=f"p-{i}",
            customer_id=f"a-{i}",
            type=EventType.PURCHASE,
            ts=T0 + timedelta(days=2),
            source="shopify",
            value=100.0,
            attrs={"order_id": f"o-{i}"},
        )
        for i in range(30)
    )
    services = build_services_from_runtime(runtime, clock=lambda: T0 + timedelta(days=60))
    services.workspace.put_goal(sample_goal(T0, "g1"))
    services.workspace.put_goal(sample_goal(T0, "g2"))
    return services


def _ledger(services: Services, goal_id: str) -> dict[str, Any]:
    app = FastAPI()
    app.include_router(measurement.router)
    attach_services(app, services)
    out: dict[str, Any] = TestClient(app).get(f"/measurement/goals/{goal_id}/summary").json()
    return out


def test_a_goals_profit_is_not_withheld_for_a_channel_only_another_goal_used() -> None:
    services = _two_goal_services()
    first = _ledger(services, "g1")["financial_impact"]
    assert first["net_incremental_profit"] is not None
    assert "whatsapp" not in (first["margin_note"] or "")
    second = _ledger(services, "g2")["financial_impact"]
    assert second["net_incremental_profit"] is None
    assert "No price is set for whatsapp" in second["margin_note"]


def test_goal_decisions_keeps_the_goals_actions_and_everything_for_the_programme() -> None:
    a = _act("d1", "c1", "g1", Channel.EMAIL, T0)
    b = _act("d2", "c2", "g2", Channel.WHATSAPP, T0)
    assert goal_decisions([a, b], "g1") == [a]
    assert goal_decisions([a, b], None) == [a, b]


# --- the measurement ledger's notes ------------------------------------------------------------------


def test_withholding_profit_drops_the_note_that_reads_it() -> None:
    services = _two_goal_services()
    summary = MeasurementSummary.model_validate(_ledger(services, "g1"))
    with_loss = summary.model_copy(update={"notes": (*summary.notes, PROFIT_LOSS_NOTE)})
    out = withhold_measurement_profit(with_loss, margin_rate=0.35, unpriced=(Channel.WHATSAPP,))
    assert PROFIT_LOSS_NOTE not in out.notes
    assert out.financial_impact.net_incremental_profit is None
    kept = withhold_measurement_profit(with_loss, margin_rate=0.35)
    assert PROFIT_LOSS_NOTE in kept.notes  # nothing withheld: the note stands


# --- the proof and the ledger count profit alike -----------------------------------------------------


def test_a_demo_with_fixed_costs_counts_profit_at_the_live_margin_on_both_pages() -> None:
    base = _two_goal_services()
    runtime: Any = base.runtime
    inner = getattr(runtime, "inner", runtime)
    demo_costs = Costs(channel={Channel.EMAIL: 9.0}, margin_rate=0.5)
    services = build_services_from_runtime(inner, clock=base.clock, costs=demo_costs, margin_rate=None)
    proof = services.proof("g1")
    assert proof is not None and proof.margin_rate == services.margin_rate
    services.set_margin_rate(0.2)
    proof = services.proof("g1")
    assert proof is not None and proof.margin_rate == 0.2
    ledger = _ledger(services, "g1")["financial_impact"]
    # The runtime has the shop's prices (email 0.12): both pages use them, not the demo's 9.0.
    assert services.send_prices()[Channel.EMAIL] == 0.12
    assert proof.spend == pytest.approx(30 * 0.12)
    assert ledger["dispatch_costs"] == pytest.approx(30 * 0.12)


def test_without_a_runtime_setting_the_demos_own_margin_is_the_services_margin() -> None:
    runtime = build_runtime(RuntimeSettings.from_env({}), engine=memory_engine())
    services = build_services_from_runtime(
        runtime, clock=lambda: T0, costs=Costs(channel={Channel.EMAIL: 0.001}, margin_rate=0.45)
    )
    assert services.margin_rate == 0.45


# --- the mock the app shows without a server ---------------------------------------------------------


def test_the_mock_margin_is_a_margin_status_not_yet_answered() -> None:
    raw = json.loads((MOCK / "onboarding_margin.json").read_text())
    status = MarginStatus.model_validate(raw)
    assert json.loads(status.model_dump_json()) == raw
    assert not status.answered and status.margin_pct is None and status.source == "not set"
    assert status.question == MARGIN_QUESTION and status.note == MARGIN_NOT_SET_NOTE


def test_the_mock_limits_ask_the_margin_as_the_server_does_for_an_unanswered_shop() -> None:
    raw = json.loads((MOCK / "onboarding_limits.json").read_text())
    for body in raw.values():
        learned = LearnedLimits.model_validate(body)
        assert json.loads(learned.model_dump_json()) == body
        margin = next(lim for lim in learned.limits if lim.key == "margin")
        assert margin.source == ORDERS_SOURCE and margin.question == MARGIN_QUESTION
        assert learned.questions == (MARGIN_QUESTION,)
