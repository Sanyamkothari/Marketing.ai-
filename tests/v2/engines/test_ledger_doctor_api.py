"""P6 (measurement) and P1 (doctor) engine APIs, on a tiny app with only those two routers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.engines import doctor, measurement
from autonomy.contracts import (
    Action,
    Channel,
    Decision,
    DecisionKind,
    Estimate,
    Event,
    EventType,
    Group,
)
from autonomy.proof import Z_95, assign_group

NOW = datetime(2026, 6, 1, tzinfo=UTC)
START = NOW - timedelta(days=40)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(measurement.router)
    app.include_router(doctor.router)
    return TestClient(app)


# --- synthetic ledger data -------------------------------------------------------------------------


def _world(
    n: int, treated_rate: float, holdout_rate: float, *, offer: bool = False
) -> tuple[list[Decision], list[Event]]:
    """`n` treated and `n` holdout customers, one decision each at START, with exact conversion rates.

    Treated customers are emailed (a SEND event names the decision); converters buy 100 (margin 40)
    on day 3, inside a 14-day window that has closed by NOW.
    """
    decisions: list[Decision] = []
    events: list[Event] = []
    for arm, rate in (("t", treated_rate), ("h", holdout_rate)):
        buyers = round(n * rate)
        for i in range(n):
            cid, did = f"{arm}{i}", f"d_{arm}{i}"
            treated = arm == "t"
            action = (
                Action(
                    goal_id="g1",
                    channel=Channel.EMAIL,
                    offer_id="ten_off" if offer else "none",
                    send_at=START,
                )
                if treated
                else None
            )
            decisions.append(
                Decision(
                    decision_id=did,
                    customer_id=cid,
                    made_at=START,
                    kind=DecisionKind.ACT if treated else DecisionKind.NOTHING,
                    action=action,
                    group=Group.AGENT if treated else Group.HOLDOUT,
                    probability=1.0,
                    reason="test",
                    policy_version="t",
                )
            )
            if treated:
                events.append(
                    Event(
                        event_id=f"s_{did}",
                        customer_id=cid,
                        type=EventType.SEND,
                        ts=START,
                        source="test",
                        channel=Channel.EMAIL,
                        decision_id=did,
                    )
                )
            if i < buyers:
                events.append(
                    Event(
                        event_id=f"p_{did}",
                        customer_id=cid,
                        type=EventType.PURCHASE,
                        ts=START + timedelta(days=3),
                        source="test",
                        value=100.0,
                        margin=40.0,
                        attrs={"offer_id": "ten_off"} if offer and treated else {},
                    )
                )
    return decisions, events


def _summary(client: TestClient, decisions: list[Decision], events: list[Event], **extra: Any) -> Any:
    body = {
        "decisions": [d.model_dump(mode="json") for d in decisions],
        "events": [e.model_dump(mode="json") for e in events],
        "horizon_days": 14,
        "objective": "gross_profit",
        "channel_costs": {"email": 0.5},
        "offer_costs": {"ten_off": 10.0},
        "now": NOW.isoformat(),
        **extra,
    }
    res = client.post("/measurement/summary", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_summary_positive_effect(client: TestClient) -> None:
    out = _summary(client, *_world(200, 0.40, 0.10))
    assert out["status"] == "STATISTICALLY_SIGNIFICANT_POSITIVE"
    assert (out["treated_customers"], out["holdout_customers"], out["baseline_customers"]) == (200, 200, 0)
    cm = out["causal_metrics"]
    assert cm["treated_conversion_rate"] == pytest.approx(0.40)
    assert cm["holdout_conversion_rate"] == pytest.approx(0.10)
    assert cm["net_incremental_lift_pct"] == pytest.approx(300.0)
    low, high = cm["confidence_interval_95"]
    assert low < 300.0 < high and low > 0
    assert cm["p_value"] < 1e-6
    fi = out["financial_impact"]
    assert fi["dispatch_costs"] == pytest.approx(200 * 0.5)
    assert fi["offer_cost"] == pytest.approx(0.0)
    # 60 extra buyers x 100 revenue; profit: 60 x 40 margin minus 100 of email.
    assert fi["gross_incremental_revenue"]["point"] == pytest.approx(6000.0)
    assert fi["net_incremental_profit"]["point"] == pytest.approx(2400.0 - 100.0)
    assert fi["roi_ratio"]["point"] == pytest.approx(2300.0 / 100.0)
    assert fi["net_incremental_profit"]["low"] <= fi["net_incremental_profit"]["point"]


def test_summary_negative_null_and_insufficient(client: TestClient) -> None:
    assert _summary(client, *_world(200, 0.05, 0.30))["status"] == "SIGNIFICANT_NEGATIVE"
    null = _summary(client, *_world(200, 0.20, 0.20))
    assert null["status"] == "NOT_YET_SIGNIFICANT"
    assert null["causal_metrics"]["p_value"] == pytest.approx(1.0)
    small = _summary(client, *_world(10, 0.9, 0.0))
    assert small["status"] == "INSUFFICIENT_DATA"
    assert small["causal_metrics"]["p_value"] is None
    assert small["financial_impact"]["net_incremental_profit"] is None
    assert any("Too few customers" in n for n in small["notes"])


def test_summary_offer_costs_and_loss_note(client: TestClient) -> None:
    # Offers cost 10 each but margin is only 4: more buyers, less money.
    decisions, events = _world(200, 0.40, 0.10, offer=True)
    events = [e.model_copy(update={"margin": 4.0}) if e.type is EventType.PURCHASE else e for e in events]
    out = _summary(client, decisions, events)
    assert out["status"] == "STATISTICALLY_SIGNIFICANT_POSITIVE"
    assert out["financial_impact"]["offer_cost"] == pytest.approx(80 * 10.0)
    assert out["financial_impact"]["net_incremental_profit"]["high"] < 0
    assert any("cost more than they made" in n for n in out["notes"])


def test_summary_rejects_unknown_keys(client: TestClient) -> None:
    res = client.post(
        "/measurement/summary", json={"decisions": [], "events": [], "now": NOW.isoformat(), "x": 1}
    )
    assert res.status_code == 422


def test_p_value_sanity() -> None:
    assert measurement.p_value(None) is None
    assert measurement.p_value(Estimate(point=0.0, low=-1.0, high=1.0)) == pytest.approx(1.0)
    # A point exactly 1.96 standard errors from zero: p = 0.05.
    se = 1.0
    at_edge = Estimate(point=Z_95 * se, low=0.0, high=2 * Z_95 * se)
    assert measurement.p_value(at_edge) == pytest.approx(0.05, abs=1e-9)
    assert measurement.p_value(Estimate(point=-5.0, low=-5.0, high=-5.0)) == 0.0
    # Symmetric in sign.
    neg = Estimate(point=-1.0, low=-2.0, high=0.0)
    pos = Estimate(point=1.0, low=0.0, high=2.0)
    assert measurement.p_value(neg) == pytest.approx(measurement.p_value(pos))


def test_goal_summary_needs_services(client: TestClient) -> None:
    assert client.get("/measurement/goals/g1/summary").status_code == 503


def test_goal_summary_from_services() -> None:
    from api.v2.deps import attach_services, build_demo_services

    app = FastAPI()
    app.include_router(measurement.router)
    services = build_demo_services(NOW, n_customers=120, days=20)
    attach_services(app, services)
    c = TestClient(app)
    goal_id = services.workspace.goals()[0].goal_id
    res = c.get(f"/measurement/goals/{goal_id}/summary")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["goal_id"] == goal_id
    assert out["status"] in {s.value for s in measurement.MeasurementStatus}
    assert out["treated_customers"] + out["holdout_customers"] + out["baseline_customers"] > 0
    assert c.get("/measurement/goals/nope/summary").status_code == 404


# --- off-policy evaluation -------------------------------------------------------------------------


def _bandit(n: int = 4000, p_act: float = 0.3, seed: int = 3) -> list[dict[str, Any]]:
    """A logged bandit: act with chance 0.3; acting converts 50% of the time, doing nothing 20%."""
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        acted = bool(rng.random() < p_act)
        reward = float(rng.random() < (0.5 if acted else 0.2))
        rows.append(
            {
                "kind": "act" if acted else "nothing",
                "probability": p_act if acted else 1 - p_act,
                "reward": reward,
                "q_act": 0.5,
                "q_nothing": 0.2,
            }
        )
    return rows


def test_ope_recovers_known_values(client: TestClient) -> None:
    rows = _bandit()
    for target, truth in (("always_act", 0.5), ("never_act", 0.2)):
        res = client.post("/measurement/ope", json={"logged": rows, "target_policy": target})
        assert res.status_code == 200, res.text
        out = res.json()
        for method in ("ips", "snips", "dr"):
            est = out[method]["value"]
            assert est["low"] <= truth <= est["high"], (target, method, est)
        assert (
            out["dr"]["value"]["high"] - out["dr"]["value"]["low"]
            < out["ips"]["value"]["high"] - out["ips"]["value"]["low"]
        )
    logged = client.post("/measurement/ope", json={"logged": rows, "target_policy": "logged"}).json()
    assert logged["ips"]["value"]["point"] == pytest.approx(logged["logged_value"])
    assert logged["acted_share"] == pytest.approx(0.3, abs=0.03)


def test_ope_without_model_and_bad_input(client: TestClient) -> None:
    rows = [{k: v for k, v in r.items() if not k.startswith("q_")} for r in _bandit(200)]
    out = client.post("/measurement/ope", json={"logged": rows, "target_policy": "always_act"}).json()
    assert out["dr"] is None
    assert any("doubly robust" in n for n in out["notes"])
    one = client.post("/measurement/ope", json={"logged": rows[:1], "target_policy": "logged"})
    assert one.status_code == 422


# --- holdout assignment ----------------------------------------------------------------------------


def test_holdout_assign_shares_match(client: TestClient) -> None:
    ids = [f"c{i}" for i in range(10_000)]
    body = {"customer_ids": ids, "salt": "shop-1", "holdout_share": 0.1, "baseline_share": 0.2}
    out = client.post("/measurement/holdout/assign", json=body).json()
    assert out["shares"]["holdout"] == pytest.approx(0.1, abs=0.015)
    assert out["shares"]["baseline"] == pytest.approx(0.2, abs=0.015)
    assert sum(out["counts"].values()) == 10_000
    for cid in ids[:50]:
        assert out["groups"][cid] == assign_group(cid, "shop-1", 0.1, 0.2).value
    again = client.post("/measurement/holdout/assign", json=body).json()
    assert again["groups"] == out["groups"]
    bad = client.post(
        "/measurement/holdout/assign",
        json={"customer_ids": ["a"], "salt": "s", "holdout_share": 0.6, "baseline_share": 0.5},
    )
    assert bad.status_code == 422


# --- doctor: profile -------------------------------------------------------------------------------


def _verhoeff_complete(stem: str) -> str:
    """The stem plus the check digit that makes it pass the Verhoeff check."""
    return next(stem + d for d in "0123456789" if doctor.is_verhoeff_valid(stem + d))


def _planted_csv(n: int = 150) -> bytes:
    lines = ["customer_id,email,mobile,card,aadhaar,pan,signup_date,last_order,spend,churned,refund_flag"]
    for i in range(n):
        cid = f"C{i:04d}" if i != 7 else "C0003"  # one duplicated key
        aadhaar = _verhoeff_complete(f"{2345_6789_000 + i:011d}")
        signup = "2026-01-15" if i % 50 else "2027-03-01"  # three rows dated after the snapshot
        last = "not a date" if i == 9 else f"2026-02-{(i % 27) + 1:02d}"
        churned = "1" if i % 3 == 0 else "0"
        lines.append(
            f"{cid},user{i}@example.com,+91 98{i:08d},4111 1111 1111 1111,{aadhaar},ABCPE{i % 10000:04d}F,"
            f"{signup},{last},{100 + i},{churned},{churned}"
        )
    return ("\n".join(lines) + "\n").encode()


def _profile(client: TestClient, data: bytes, **fields: str) -> Any:
    res = client.post("/doctor/profile", files={"file": ("x.csv", data, "text/csv")}, data=fields)
    assert res.status_code == 200, res.text
    return res.json()


def test_luhn_and_verhoeff() -> None:
    assert doctor.is_luhn_valid("4111111111111111")
    assert not doctor.is_luhn_valid("4111111111111112")
    assert doctor.is_verhoeff_valid("2363")  # the textbook example: 236 has check digit 3
    assert not doctor.is_verhoeff_valid("2364")


def test_profile_finds_planted_problems(client: TestClient) -> None:
    out = _profile(
        client,
        _planted_csv(),
        snapshot="2026-06-01T00:00:00+00:00",
        primary_key="customer_id",
        target="churned",
        use_v1="false",
    )
    assert out["rows"] == 150 and out["columns"] == 11
    pii = {f["column"]: (f["type"], f["action"]) for f in out["pii_detected"]}
    assert pii["email"] == ("email", "hash")
    assert pii["mobile"] == ("phone", "hash")
    assert pii["card"] == ("card_number", "drop")
    assert pii["aadhaar"] == ("aadhaar", "drop")
    assert pii["pan"] == ("pan", "drop")
    assert "spend" not in pii and "customer_id" not in pii

    assert out["primary_key_valid"] is False
    assert out["primary_key"]["duplicate_keys"] == 1
    assert out["primary_key"]["duplicate_rows"] == 1

    dates = {d["column"]: d for d in out["temporal_integrity"]["date_columns"]}
    assert set(dates) == {"signup_date", "last_order"}
    assert dates["signup_date"]["future_dated"] == 3
    assert dates["last_order"]["unparseable"] == 1
    assert out["temporal_integrity"]["ok"] is False

    suspects = {s["column"] for s in out["leak_check"]["suspects"]}
    assert "refund_flag" in suspects  # a copy of the target
    codes = {p["code"] for p in out["problems"]}
    assert {"duplicate_key", "future_dates", "unparseable_dates", "leak_suspected"} <= codes
    assert out["readiness_score"] < 50
    assert out["v1_enrichment"] is None
    # Never echoes a cell value.
    assert "user1@example.com" not in json.dumps(out)


def test_profile_clean_file_scores_high(client: TestClient) -> None:
    lines = ["order_id,customer_id,order_date,amount"]
    lines += [f"O{i},C{i % 40},2026-03-{(i % 28) + 1:02d},{10 + i}" for i in range(200)]
    out = _profile(client, ("\n".join(lines)).encode(), snapshot="2026-06-01T00:00:00Z", use_v1="false")
    assert out["primary_key"]["column"] == "order_id" and out["primary_key"]["guessed"] is True
    assert out["primary_key_valid"] is True
    assert out["pii_detected"] == []
    assert out["temporal_integrity"]["ok"] is True
    assert out["readiness_score"] == 100


def test_profile_date_of_birth_and_categorical_leak() -> None:
    # A date of birth parses as a date, but it is still personal data; a date it is not (created_at).
    # `state` is text whose values alone give the target away (the correlation check can't see it).
    lines = ["customer_id,dob,created_at,state,city,churned"]
    for i in range(120):
        churned = i % 3 == 0
        lines.append(
            f"C{i},1990-01-{i % 28 + 1:02d},2026-01-{i % 28 + 1:02d},{'gone' if churned else 'live'},"
            f"{['Pune', 'Delhi', 'Goa'][i % 4 % 3]},{int(churned)}"
        )
    frame = doctor.read_csv_bytes("\n".join(lines).encode())
    out = doctor.profile_frame(frame, target="churned", use_v1=False)
    pii = {f.column: (f.type, f.action, f.detected_by) for f in out.pii_detected}
    assert pii == {"dob": ("date_of_birth", "drop", "header")}
    assert {d.column for d in out.temporal_integrity.date_columns} == {"dob", "created_at"}
    suspects = {s.column: s.reason for s in out.leak_check.suspects}
    assert suspects == {"state": "its values alone tell the target apart"}


def test_profile_raw_body_and_limits(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"id,phone\n1,(555) 123-4567\n2,(555) 765-4321\n"
    res = client.post("/doctor/profile?use_v1=false", content=raw, headers={"content-type": "text/csv"})
    assert res.status_code == 200
    assert res.json()["pii_detected"][0]["type"] == "phone"
    monkeypatch.setattr(doctor, "MAX_UPLOAD_BYTES", 10)
    too_big = client.post("/doctor/profile", content=raw, headers={"content-type": "text/csv"})
    assert too_big.status_code == 413
    monkeypatch.undo()
    no_file = client.post("/doctor/profile", data={"target": "x"}, files={"other": ("a", b"x")})
    assert no_file.status_code == 422
    unknown = client.post("/doctor/profile", files={"file": ("x.csv", raw)}, data={"primary_key": "nope"})
    assert unknown.status_code == 422


@pytest.mark.v1bridge
def test_profile_v1_enrichment(client: TestClient) -> None:
    lines = ["gst_number,amount"] + [f"27AAPFU0939F1ZV,{i}" for i in range(30)]
    out = _profile(client, ("\n".join(lines)).encode())
    assert out["v1_enrichment"] is not None
    assert out["v1_enrichment"]["source"] == "engine.doctor.india_ids"


# --- doctor: map-events ----------------------------------------------------------------------------


def test_map_events(client: TestClient) -> None:
    csv = b"cust,when,total\nA,2026-03-01T10:00:00,12.50\n,2026-03-02T10:00:00,4\nB,yesterday,3\nC,2026-03-03,7\n"
    mapping = {"customer_id": "cust", "timestamp": "when", "event_type": "purchase", "value_column": "total"}
    res = client.post(
        "/doctor/map-events",
        files={"file": ("orders.csv", csv, "text/csv")},
        data={"mapping": json.dumps(mapping), "source": "shop"},
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["rows"] == 4 and out["mapped"] == 2
    assert [e["customer_id"] for e in out["events"]] == ["A", "C"]
    assert out["events"][0]["value"] == pytest.approx(12.5)
    assert out["problem_counts"] == {"missing_customer": 1, "bad_timestamp": 1}

    missing_col = {**mapping, "value_column": "nope"}
    bad = client.post(
        "/doctor/map-events", files={"file": ("o.csv", csv)}, data={"mapping": json.dumps(missing_col)}
    )
    assert bad.status_code == 422
    assert client.post("/doctor/map-events", files={"file": ("o.csv", csv)}).status_code == 422
