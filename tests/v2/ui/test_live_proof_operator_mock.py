"""The mock JSON behind the Live, Proof and Operator screens is exactly in the contract shapes.

The UI runs on these files when /v2 is not there (`?mock=1`), so a field that drifts from the
contract would hide a real bug until the API is wired in. Each file is parsed by the contract model
and dumped back: the round trip must give the same JSON, so no key is missing, extra or misspelt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from autonomy.contracts import (
    Alert,
    Briefing,
    CustomerState,
    Decision,
    DecisionKind,
    Event,
    HealthStatus,
    ProofSummary,
)

MOCK = Path(__file__).resolve().parents[3] / "ui" / "v2" / "mock"
GOAL_ID = "goal_winback"


def load(name: str) -> Any:
    return json.loads((MOCK / name).read_text())


def roundtrip(model: type[BaseModel], data: Any) -> Any:
    return model.model_validate(data).model_dump(mode="json")


def roundtrip_list(model: type[BaseModel], data: Any) -> Any:
    adapter = TypeAdapter(list[model])  # type: ignore[valid-type]
    return adapter.dump_python(adapter.validate_python(data), mode="json")


@pytest.mark.parametrize(
    ("name", "model", "many"),
    [
        ("decisions.json", Decision, True),
        ("alerts.json", Alert, True),
        ("briefing.json", Briefing, False),
        ("proof.json", ProofSummary, False),
        ("health.json", HealthStatus, False),
    ],
)
def test_file_is_exactly_the_contract_shape(name: str, model: type[BaseModel], many: bool) -> None:
    data = load(name)
    again = roundtrip_list(model, data) if many else roundtrip(model, data)
    assert again == data


def test_customer_bundle_is_state_decisions_and_events() -> None:
    bundle = load("customer.json")
    assert set(bundle) == {"state", "decisions", "events"}
    assert roundtrip(CustomerState, bundle["state"]) == bundle["state"]
    assert roundtrip_list(Decision, bundle["decisions"]) == bundle["decisions"]
    assert roundtrip_list(Event, bundle["events"]) == bundle["events"]
    cid = bundle["state"]["customer_id"]
    assert {d["customer_id"] for d in bundle["decisions"]} == {cid}
    assert {e["customer_id"] for e in bundle["events"]} == {cid}
    # Every outbound event traces back to one of the customer's decisions.
    ids = {d["decision_id"] for d in bundle["decisions"]}
    assert {e["decision_id"] for e in bundle["events"] if e["decision_id"]} <= ids


def test_customer_state_agrees_with_its_events() -> None:
    """The state is a fold over the events, so the panel's summary and timeline must not disagree."""
    bundle = load("customer.json")
    state = CustomerState.model_validate(bundle["state"])
    events = [Event.model_validate(e) for e in bundle["events"]]
    assert state.version == len(events)
    purchases = [e for e in events if e.type.value == "purchase"]
    assert state.purchases == len(purchases)
    assert state.revenue == pytest.approx(sum(e.value or 0.0 for e in purchases))
    assert state.margin == pytest.approx(sum(e.margin or 0.0 for e in purchases))
    for channel, stats in state.channels.items():
        on = [e for e in events if e.channel == channel]
        assert stats.sends == sum(e.type.value == "send" for e in on), channel
        assert stats.delivers == sum(e.type.value == "deliver" for e in on), channel
        assert stats.clicks == sum(e.type.value == "click" for e in on), channel
    assert state.last_seen_at == max(e.ts for e in events)
    # A stated "no discounts" preference is a hard rule, so no discount is ever bid for her.
    if state.preferences.get("no_discounts") == "true":
        for d in bundle["decisions"]:
            assert all(b["action"]["offer_id"] == "none" for b in d["bids"]), d["decision_id"]


def test_customer_bundle_matches_the_feed() -> None:
    """The customer the feed shows first can be opened in the side panel with the same decision."""
    bundle = load("customer.json")
    feed = {d["decision_id"]: d for d in load("decisions.json")}
    shared = [d for d in bundle["decisions"] if d["decision_id"] in feed]
    assert shared, "the sample customer should appear in the activity feed"
    for d in shared:
        assert feed[d["decision_id"]] == d


def test_decisions_tell_a_believable_story() -> None:
    decisions = [Decision.model_validate(d) for d in load("decisions.json")]
    kinds = {d.kind for d in decisions}
    assert kinds == {DecisionKind.ACT, DecisionKind.NOTHING}
    assert {d.group.value for d in decisions} == {"agent", "baseline", "holdout"}
    assert len({d.decision_id for d in decisions}) == len(decisions)
    made = [d.made_at for d in decisions]
    assert made == sorted(made, reverse=True), "newest first, as the API lists them"
    for d in decisions:
        assert d.reason.endswith("."), d.decision_id
        if d.action is not None:
            assert d.action.goal_id == GOAL_ID
        if d.group.value == "holdout":
            assert d.kind is DecisionKind.NOTHING
        # The bids are best first, and an ACT's expected net is one of its bids'.
        nets = [b.expected_net for b in d.bids]
        assert nets == sorted(nets, reverse=True), d.decision_id
        if d.kind is DecisionKind.ACT and d.bids:
            assert d.expected_net in nets
        for b in d.bids:
            want = (b.uplift * b.value_if_converted - b.offer_cost - b.channel_cost) * b.urgency
            assert b.expected_net == pytest.approx(want, abs=0.01)


def test_mock_goal_ids_match_the_sample_goal() -> None:
    goals = {g["goal_id"]: g for g in load("goals.json")}
    assert GOAL_ID in goals
    assert load("briefing.json")["goal_id"] == GOAL_ID
    assert load("proof.json")["goal_id"] == GOAL_ID
    assert {a["goal_id"] for a in load("alerts.json")} <= {GOAL_ID, None}


def test_proof_file_agrees_with_the_goal_detail() -> None:
    """Live and Proof read /proof; Plan reads the goal bundle. They must not disagree."""
    assert load("proof.json") == load("goal_detail.json")["proof"]
    proof = ProofSummary.model_validate(load("proof.json"))
    assert proof.incremental_profit is not None
    assert proof.roi is not None
    assert proof.spend > 0
    # ROI is extra profit per unit spent, so its range follows the profit range.
    assert proof.roi.point == pytest.approx(proof.incremental_profit.point / proof.spend, abs=0.1)


def _numbers_in(text: str) -> set[float]:
    return {float(m.replace(",", "")) for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def test_briefing_numbers_all_come_from_the_numbers_field() -> None:
    """The contract: every number in a section's body appears in its `numbers` (computed by code)."""
    for section in Briefing.model_validate(load("briefing.json")).sections:
        allowed = set(section.numbers.values())
        allowed |= {round(v * 100, 1) for v in section.numbers.values() if 0 < v < 1}
        assert _numbers_in(section.body) <= allowed, section.title


def test_health_alerts_are_in_the_alerts_list() -> None:
    alerts = {a["alert_id"]: a for a in load("alerts.json")}
    for a in load("health.json")["alerts"]:
        assert alerts[a["alert_id"]] == a
    assert any(
        a["auto_paused"] for a in alerts.values()
    ), "the inbox should show something that paused itself"
