"""The margin question is required and the proof prices in the shop's money (docs/v4/ONBOARDING_MARGIN.md)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.deps import (
    DEMO_SEND_COST,
    Services,
    build_demo_services,
    build_services_from_runtime,
)
from api.v2.engines import measurement
from api.v2.insights import goal_spend
from api.v2.onboarding import MARGIN_WARN_ABOVE_PCT, MARGIN_WARN_BELOW_PCT
from autonomy.contracts import (
    Action,
    Channel,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Group,
)
from autonomy.fakes.samples import sample_goal
from autonomy.onboarding.limits import MARGIN_QUESTION
from autonomy.runtime.prices import ShopPrices
from autonomy.runtime.settings import RuntimeSettings, build_runtime
from autonomy.store import memory_engine
from tests.v3.currency.test_currency import onboarding_client

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
QUESTION = "Out of every 100 of sales, how much is left after product cost, shipping and payment fees?"
INR_PRICES = ShopPrices(
    currency="INR",
    channel_costs={Channel.EMAIL: 0.12, Channel.SMS: 0.2, Channel.WHATSAPP: 0.85},
    typical_order_value=1800,
)


def day(n: float) -> datetime:
    return T0 + timedelta(days=n)


# --- the onboarding question ----------------------------------------------------------------------


def test_the_question_is_the_one_in_plain_words() -> None:
    assert MARGIN_QUESTION == QUESTION


def test_the_question_is_unanswered_until_the_shop_answers() -> None:
    client, services, desk = onboarding_client(INR_PRICES)
    with client:
        got = client.get("/v2/onboarding/margin").json()
        assert got["answered"] is False and got["margin_pct"] is None and got["source"] == "not set"
        assert got["question"] == QUESTION and "contribution margin" in got["explanation"]
        assert got["min_pct"] == 0 and got["max_pct"] == 100
        assert services.margin_rate is None and desk.margin_rate is None


def test_an_answer_is_stored_as_a_rate_for_the_proof_and_the_runtime() -> None:
    client, services, desk = onboarding_client(INR_PRICES)
    with client:
        out = client.post("/v2/onboarding/margin", json={"margin_pct": 35}).json()
        assert out["answered"] and out["margin_pct"] == 35 and out["margin_rate"] == 0.35
        assert out["source"] == "your answer" and out["warning"] is None
        assert services.margin_rate == 0.35 and desk.margin_rate == 0.35
        again = client.get("/v2/onboarding/margin").json()
        assert again["margin_rate"] == 0.35


@pytest.mark.parametrize("bad", [0, -5, 100, 140, 1e-9])
def test_an_answer_outside_zero_to_one_is_refused_and_nothing_is_kept(bad: float) -> None:
    client, services, desk = onboarding_client(INR_PRICES)
    with client:
        assert client.post("/v2/onboarding/margin", json={"margin_pct": bad}).status_code == 422
        assert client.post("/v2/onboarding/answers", json={"margin_pct": bad}).status_code == 422
        assert services.margin_rate is None and desk.margin_rate is None


@pytest.mark.parametrize(
    ("pct", "warns"),
    [
        (MARGIN_WARN_BELOW_PCT - 0.5, "very low"),
        (MARGIN_WARN_BELOW_PCT, None),
        (40, None),
        (MARGIN_WARN_ABOVE_PCT, None),
        (MARGIN_WARN_ABOVE_PCT + 1, "very high"),
        (0.4, "very low"),
    ],
)
def test_an_unusual_answer_is_kept_with_a_warning(pct: float, warns: str | None) -> None:
    client, services, _ = onboarding_client(INR_PRICES)
    with client:
        out = client.post("/v2/onboarding/margin", json={"margin_pct": pct}).json()
        assert services.margin_rate == pytest.approx(pct / 100, abs=1e-4)
        if warns is None:
            assert out["warning"] is None
        else:
            assert warns in out["warning"]


def test_the_older_answers_route_stores_the_margin_too() -> None:
    client, services, _ = onboarding_client(INR_PRICES)
    with client:
        assert client.post("/v2/onboarding/answers", json={"margin_pct": 30}).status_code == 200
        assert services.margin_rate == 0.3


def test_the_margin_setting_counts_as_answered() -> None:
    client, services, _ = onboarding_client(INR_PRICES)
    services.margin_rate = 0.42  # AUTONOMY_MARGIN_RATE
    with client:
        got = client.get("/v2/onboarding/margin").json()
        assert got["answered"] and got["source"] == "settings" and got["margin_pct"] == 42


def test_accept_is_refused_until_the_margin_is_answered_and_writes_nothing() -> None:
    client, services, desk = onboarding_client(INR_PRICES)
    before = {g.goal_id for g in services.workspace.goals()}
    head = desk.ledger.head
    with client:
        r = client.post("/v2/onboarding/accept", json={"opportunity_id": "opp_win_back"})
        assert r.status_code == 409 and QUESTION in r.json()["detail"]
        assert {g.goal_id for g in services.workspace.goals()} == before
        assert desk.ledger.head == head and services.margin_rate is None
        ok = client.post("/v2/onboarding/accept", json={"opportunity_id": "opp_win_back", "margin_pct": 45})
        assert ok.status_code == 200, ok.text
        assert services.margin_rate == 0.45


def test_accept_after_an_earlier_answer_needs_no_repeat() -> None:
    client, _, _ = onboarding_client(INR_PRICES)
    with client:
        assert client.post("/v2/onboarding/margin", json={"margin_pct": 45}).status_code == 200
        ok = client.post("/v2/onboarding/accept", json={"opportunity_id": "opp_win_back"})
        assert ok.status_code == 200, ok.text


def test_the_limits_step_asks_the_question_until_it_is_answered() -> None:
    client, _, _ = onboarding_client(INR_PRICES)
    with client:
        first = client.get("/v2/onboarding/limits").json()
        margin = next(limit for limit in first["limits"] if limit["key"] == "margin")
        assert margin["question"] == QUESTION
        client.post("/v2/onboarding/margin", json={"margin_pct": 45})
        second = client.get("/v2/onboarding/limits").json()
        assert next(limit for limit in second["limits"] if limit["key"] == "margin")["question"] is None


# --- the proof ---------------------------------------------------------------------------------


def _decision(did: str, cid: str, group: Group, channel: Channel | None) -> Decision:
    if channel is None:
        return Decision(
            decision_id=did,
            customer_id=cid,
            made_at=T0,
            kind=DecisionKind.NOTHING,
            group=group,
            probability=1.0,
            reason="test",
            policy_version="t",
        )
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=T0,
        kind=DecisionKind.ACT,
        group=group,
        action=Action(goal_id="g1", channel=channel, send_at=T0),
        probability=0.5,
        reason="test",
        policy_version="t",
    )


def _proof_services(env: dict[str, str], channel: Channel | None) -> Services:
    """30 agent and 30 holdout customers; every agent customer bought 100 two days later, none has a margin."""
    runtime = build_runtime(RuntimeSettings.from_env(env), engine=memory_engine())
    runtime.decisions.record(
        [
            _decision(f"d-{g.value}-{i}", f"{g.value}-{i}", g, channel if g is Group.AGENT else None)
            for g in (Group.AGENT, Group.HOLDOUT)
            for i in range(30)
        ]
    )
    if channel is not None:
        runtime.events.append(
            Event(
                event_id=f"s-{i}",
                customer_id=f"agent-{i}",
                type=EventType.SEND,
                ts=T0 + timedelta(minutes=1),
                source="dispatcher",
                channel=channel,
                decision_id=f"d-agent-{i}",
            )
            for i in range(30)
        )
    runtime.events.append(
        Event(
            event_id=f"p-{i}",
            customer_id=f"agent-{i}",
            type=EventType.PURCHASE,
            ts=day(2),
            source="shopify",
            value=100.0,
            attrs={"order_id": f"o-{i}"},
        )
        for i in range(30)
    )
    return build_services_from_runtime(runtime, clock=lambda: day(60))


def test_without_the_margin_the_proof_shows_revenue_only_and_says_why() -> None:
    proof = _proof_services({}, None).proof(None)
    assert proof is not None
    assert proof.incremental_revenue is not None and proof.incremental_revenue.point == pytest.approx(3000)
    assert proof.incremental_profit is None and proof.lift_vs_baseline is None and proof.roi is None
    assert proof.detectable is None
    assert proof.margin_note is not None and proof.margin_note.startswith("Profit needs your margin")
    assert QUESTION in proof.margin_note


def test_answering_the_question_brings_the_profit_back_without_a_restart() -> None:
    services = _proof_services({}, None)
    assert services.proof(None).incremental_profit is None  # type: ignore[union-attr]
    services.set_margin_rate(0.35)
    proof = services.proof(None)
    assert proof is not None and proof.incremental_profit is not None
    assert proof.incremental_profit.point == pytest.approx(30 * 35.0)
    assert proof.margin_rate == 0.35
    assert not proof.margin_note or not proof.margin_note.startswith("Profit needs")


def test_a_channel_with_no_price_is_named_instead_of_costed_at_a_default() -> None:
    services = _proof_services({"AUTONOMY_MARGIN_RATE": "0.35"}, Channel.WHATSAPP)
    proof = services.proof(None)
    assert proof is not None and proof.incremental_revenue is not None
    assert proof.incremental_profit is None and proof.roi is None
    assert proof.margin_note is not None
    assert "No price is set for whatsapp" in proof.margin_note
    assert "AUTONOMY_CHANNEL_COSTS" in proof.margin_note


def test_the_proof_costs_sends_at_the_shops_price_in_its_currency() -> None:
    env = {
        "AUTONOMY_MARGIN_RATE": "0.35",
        "AUTONOMY_CURRENCY": "INR",
        "AUTONOMY_CHANNEL_COSTS": "whatsapp=0.85,email=0.12",
        "AUTONOMY_TYPICAL_ORDER_VALUE": "1800",
    }
    proof = _proof_services(env, Channel.WHATSAPP).proof(None)
    assert proof is not None and proof.incremental_profit is not None
    assert proof.incremental_profit.point == pytest.approx(30 * (35.0 - 0.85))  # the old default said 0.04
    assert proof.spend == pytest.approx(30 * 0.85)
    assert proof.margin_note is None or "No price" not in proof.margin_note


def test_a_free_channel_needs_no_price() -> None:
    proof = _proof_services({"AUTONOMY_MARGIN_RATE": "0.35"}, Channel.PUSH).proof(None)
    assert proof is not None and proof.incremental_profit is not None
    assert proof.incremental_profit.point == pytest.approx(30 * 35.0)


def test_nothing_in_the_api_reads_the_old_dollar_constant() -> None:
    import api.v2.deps as deps

    assert not hasattr(deps, "SEND_COST")
    assert deps.DEMO_SEND_COST is DEMO_SEND_COST


# --- the measurement ledger and the spend --------------------------------------------------------


def _measurement_app(services: Services) -> TestClient:
    from api.v2.deps import attach_services

    app = FastAPI()
    app.include_router(measurement.router)
    attach_services(app, services)
    return TestClient(app)


def test_the_ledger_summary_withholds_profit_for_an_unpriced_channel_and_prices_a_priced_one() -> None:
    services = _proof_services({"AUTONOMY_MARGIN_RATE": "0.35"}, Channel.WHATSAPP)
    services.workspace.put_goal(sample_goal(T0, "g1"))
    out = _measurement_app(services).get("/measurement/goals/g1/summary").json()
    money = out["financial_impact"]
    assert money["net_incremental_profit"] is None and money["roi_ratio"] is None
    assert "No price is set for whatsapp" in money["margin_note"]
    assert money["gross_incremental_revenue"] is not None

    priced = dataclasses.replace(services, runtime=type("R", (), {"prices": INR_PRICES})())
    out = _measurement_app(priced).get("/measurement/goals/g1/summary").json()
    money = out["financial_impact"]
    assert money["net_incremental_profit"] is not None
    assert money["dispatch_costs"] == pytest.approx(30 * 0.85)


def test_the_ledger_summary_without_the_margin_shows_revenue_only() -> None:
    services = _proof_services({}, None)
    services.workspace.put_goal(sample_goal(T0, "g1"))
    out = _measurement_app(services).get("/measurement/goals/g1/summary").json()
    money = out["financial_impact"]
    assert money["net_incremental_profit"] is None
    assert money["margin_note"].startswith("Profit needs your margin")
    assert money["gross_incremental_revenue"] is not None


def test_the_demo_ledger_still_uses_its_named_demo_prices() -> None:
    services = build_demo_services(T0 + timedelta(days=30), n_customers=60, days=10)
    assert services.channel_costs == DEMO_SEND_COST
    assert services.send_prices()[Channel.WHATSAPP] == DEMO_SEND_COST[Channel.WHATSAPP]


def test_goal_spend_from_the_log_uses_the_shops_prices_or_says_unknown() -> None:
    base = _proof_services(
        {}, Channel.WHATSAPP
    )  # 30 WhatsApp sends of goal g1, logged without a goal_id attr
    logged: Any = dataclasses.replace(base, runtime=None)  # no runtime ledger: the log is the source
    now = day(60)
    assert goal_spend(logged, "g1", now) is None  # no price for WhatsApp: unknown, not a dollar default
    priced: Any = dataclasses.replace(logged, channel_costs={Channel.WHATSAPP: 0.85})
    assert goal_spend(priced, "g1", now) == pytest.approx(round(30 * 0.85, 2))
