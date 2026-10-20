"""P3 Decision Hub API: evaluate and evaluate-batch on a tiny app that mounts only the engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from api.v2.deps import attach_services, build_demo_services
from api.v2.engines import decisions
from api.v2.engines.decisions import MAX_BATCH, evaluate
from autonomy.contracts import Channel, ChannelStats, CustomerState, Group
from autonomy.fakes.samples import sample_brief, sample_content

NOW = datetime(2026, 3, 2, 9, tzinfo=UTC)


def _client(services: Any = None) -> TestClient:
    engines = APIRouter(prefix="/v2/engines")
    engines.include_router(decisions.router)
    app = FastAPI()
    app.include_router(engines)
    if services is not None:
        attach_services(app, services)
    return TestClient(app)


def _lapsed(customer_id: str = "c_lapsed", group: Group = Group.AGENT) -> CustomerState:
    """Bought three times, last 100 days ago; email reachable, WhatsApp without consent."""
    return CustomerState(
        customer_id=customer_id,
        as_of=NOW,
        group=group,
        purchases=3,
        revenue=150.0,
        margin=60.0,
        first_seen_at=NOW - timedelta(days=400),
        last_purchase_at=NOW - timedelta(days=100),
        channels={
            Channel.EMAIL: ChannelStats(address_known=True, consent=True),
            Channel.WHATSAPP: ChannelStats(address_known=True, consent=False),
        },
    )


def _body(state: CustomerState, **extra: Any) -> dict[str, Any]:
    return {
        "customer_state": state.model_dump(mode="json"),
        "briefs": [sample_brief(NOW).model_dump(mode="json")],
        "content": [c.model_dump(mode="json") for c in sample_content(NOW)],
        "now": NOW.isoformat(),
        "seed": 1,
        **extra,
    }


def test_holdout_is_left_alone_with_certainty() -> None:
    r = _client().post("/v2/engines/decisions/evaluate", json=_body(_lapsed(group=Group.HOLDOUT)))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["decision"] == "DO_NOTHING"
    assert out["winning_action"] is None
    assert out["probability"] == 1.0
    assert out["blocked_by"] == ["holdout"]
    assert [s["reason_code"] for s in out["suppressed_actions"]] == ["holdout"]
    assert out["clearing"]["winning_bid"] is None


def test_lapsed_contactable_customer_gets_an_action_with_probability() -> None:
    r = _client().post("/v2/engines/decisions/evaluate", json=_body(_lapsed()))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["decision"] == "TAKE_ACTION"
    win = out["winning_action"]
    assert win["goal_id"] == "goal_winback"
    assert win["channel"] == "email"
    assert 0 < win["assigned_probability"] <= 1
    assert win["assigned_probability"] == out["probability"]
    assert win["reason"]
    assert datetime.fromisoformat(win["scheduled_time"]) >= NOW
    assert out["policy_version"]

    suppressed = out["suppressed_actions"]
    codes = {s["reason_code"] for s in suppressed}
    # WhatsApp is in the envelope but the customer never consented to it.
    assert {"goal_id": "goal_winback", "channel": "whatsapp"}.items() <= next(
        s for s in suppressed if s["reason_code"] == "no_consent"
    ).items()
    # 10% off costs more than it adds, so it cannot beat the price of another message.
    assert "below_attention_price" in codes
    # The winner is never listed as suppressed.
    assert all(
        (s["channel"], s["offer_id"], s["content_id"]) != (win["channel"], win["offer_id"], win["content_id"])
        for s in suppressed
    )

    clearing = out["clearing"]
    assert clearing["winning_bid"] == win["expected_net_value"]
    runner = clearing["runner_up"]
    assert runner is not None
    assert clearing["clearing_price"] == max(clearing["attention_price"], runner["bid"])
    assert clearing["winner_surplus"] == pytest.approx(clearing["winning_bid"] - clearing["clearing_price"])


def test_same_request_repeats_exactly() -> None:
    client = _client()
    a = client.post("/v2/engines/decisions/evaluate", json=_body(_lapsed())).json()
    b = client.post("/v2/engines/decisions/evaluate", json=_body(_lapsed())).json()
    assert a == b


def test_customer_out_of_reach_does_nothing_and_says_why() -> None:
    state = _lapsed().model_copy(update={"channels": {}})
    out = _client().post("/v2/engines/decisions/evaluate", json=_body(state)).json()
    assert out["decision"] == "DO_NOTHING"
    assert 0 < out["probability"] <= 1
    assert {s["reason_code"] for s in out["suppressed_actions"]} == {"no_consent"}


def test_hard_rule_blocks_are_listed_with_their_bids() -> None:
    state = _lapsed().model_copy(update={"contacts_7d": 2})  # the sample envelope allows 2 a week
    out = _client().post("/v2/engines/decisions/evaluate", json=_body(state)).json()
    assert out["decision"] == "DO_NOTHING"
    assert "weekly_cap" in out["blocked_by"]
    capped = [s for s in out["suppressed_actions"] if s["reason_code"] == "weekly_cap"]
    assert capped and all(s["channel"] == "email" and s["bid"] is not None for s in capped)
    assert out["clearing"]["runner_up"] is None


def test_batch_evaluates_each_customer_like_a_single_call() -> None:
    states = [_lapsed("c1"), _lapsed("c2", Group.HOLDOUT), _lapsed("c3")]
    body = _body(states[0])
    del body["customer_state"]
    body["customer_states"] = [s.model_dump(mode="json") for s in states]
    r = _client().post("/v2/engines/decisions/evaluate-batch", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["n_customers"] == 3
    assert out["n_take_action"] + out["n_do_nothing"] == 3
    assert [x["customer_id"] for x in out["results"]] == ["c1", "c2", "c3"]
    assert out["results"][1]["decision"] == "DO_NOTHING"
    single = evaluate(states[0], [sample_brief(NOW)], sample_content(NOW), now=NOW, seed=1)
    assert out["results"][0] == single.model_dump(mode="json")


def test_batch_cap_is_enforced() -> None:
    state = _lapsed().model_dump(mode="json")
    body = _body(_lapsed())
    del body["customer_state"]
    body["customer_states"] = [state] * (MAX_BATCH + 1)
    r = _client().post("/v2/engines/decisions/evaluate-batch", json=body)
    assert r.status_code == 422


def test_brief_ids_need_a_workspace() -> None:
    body = _body(_lapsed(), briefs=[], brief_ids=["brief_goal_winback_1"])
    r = _client().post("/v2/engines/decisions/evaluate", json=body)
    assert r.status_code == 400
    assert "workspace" in r.json()["detail"]


def test_brief_ids_and_content_resolve_from_services() -> None:
    services = build_demo_services(NOW, n_customers=20, days=1)
    client = _client(services)
    body = _body(_lapsed(), briefs=[], brief_ids=["brief_goal_winback_1"])
    del body["content"]
    r = client.post("/v2/engines/decisions/evaluate", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["decision"] in {"TAKE_ACTION", "DO_NOTHING"}
    missing = client.post(
        "/v2/engines/decisions/evaluate", json=_body(_lapsed(), briefs=[], brief_ids=["nope"])
    )
    assert missing.status_code == 404


def test_unapproved_brief_is_refused() -> None:
    brief = sample_brief(NOW).model_copy(update={"approved_at": None, "approved_by": None})
    body = _body(_lapsed(), briefs=[brief.model_dump(mode="json")])
    r = _client().post("/v2/engines/decisions/evaluate", json=body)
    assert r.status_code == 400


def test_aggregator_lists_engines_and_mounts_decisions() -> None:
    from api.v2.engines.router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    index = client.get("/v2/engines").json()
    ids = [e["id"] for e in index["engines"]]
    assert ids == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert next(e for e in index["engines"] if e["id"] == "P3")["mounted"] is True
    r = client.post("/v2/engines/decisions/evaluate", json=_body(_lapsed()))
    assert r.status_code == 200, r.text


def test_snooze_block_is_reported_as_the_block_not_a_lost_auction() -> None:
    # The customer reads at 20:00, but the agent aims for its default hour, which falls inside the
    # snooze. Every email bid is snoozed, and the suppressed list must say so, not "lost_draw".
    state = _lapsed().model_copy(
        update={
            "active_hours": tuple(1.0 if h == 20 else 0.0 for h in range(24)),
            "preferences": {"snooze_until": "2026-03-02T15:00:00+00:00"},
        }
    )
    out = _client().post("/v2/engines/decisions/evaluate", json=_body(state)).json()
    assert out["decision"] == "DO_NOTHING"
    assert out["blocked_by"] == ["snoozed"]
    with_bids = [s for s in out["suppressed_actions"] if s["bid"] is not None]
    assert with_bids and {s["reason_code"] for s in with_bids} == {"snoozed"}


def test_rule_codes_on_suppressed_bids_match_the_decisions_blocked_by() -> None:
    for state in (
        _lapsed(),
        _lapsed().model_copy(update={"contacts_7d": 2}),
        _lapsed().model_copy(update={"last_contact_at": NOW - timedelta(hours=1)}),
    ):
        out = _client().post("/v2/engines/decisions/evaluate", json=_body(state)).json()
        auction = {"below_attention_price", "outbid", "lost_draw"}
        rule_codes = {
            s["reason_code"]
            for s in out["suppressed_actions"]
            if s["bid"] is not None and s["reason_code"] not in auction
        }
        assert rule_codes == set(out["blocked_by"])
