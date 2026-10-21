"""Tiny checks of the real-data scoreboard (scripts/v2_realbench).

Inline fixtures only: no download, no dataset folder, a few seconds in all. The end-to-end test runs
the real agent (the product path) on a few hundred synthetic customers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from autonomy.contracts import Channel, Event, EventType, Theme
from scripts.v2_realbench import adapters as A  # noqa: N812
from scripts.v2_realbench import common as C  # noqa: N812
from scripts.v2_realbench.run import render

T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def test_split_by_hash_is_stable_disjoint_and_salted() -> None:
    ids = np.array([f"c{i}" for i in range(4000)], dtype=object)
    tr, te = C.split_by_hash(ids)
    tr2, te2 = C.split_by_hash(ids)
    assert np.array_equal(tr, tr2) and np.array_equal(te, te2)
    assert not set(tr.tolist()) & set(te.tolist())
    assert len(tr) + len(te) == len(ids)
    assert abs(len(te) / len(ids) - 0.5) < 0.04
    # a customer's side depends on its id only, not on who else is in the file
    _, te_sub = C.split_by_hash(ids[:1000])
    assert set(ids[:1000][te_sub]) == set(ids[te]) & set(ids[:1000])
    _, te_other = C.split_by_hash(ids, salt="another salt")
    assert not np.array_equal(te, te_other)


def test_pick_by_hash_is_a_fixed_exact_sample() -> None:
    ids = np.array([f"r{i}" for i in range(500)], dtype=object)
    a = C.pick_by_hash(ids, 120, salt="s")
    b = C.pick_by_hash(ids[::-1], 120, salt="s")
    assert len(a) == 120 and np.all(np.diff(a) > 0)
    assert set(ids[a]) == set(ids[::-1][b])
    assert len(C.pick_by_hash(ids, 900, salt="s")) == 500
    h = C.splitmix64(np.arange(10, dtype=np.uint64) ^ C.salt_key("x"))
    assert len(set(h.tolist())) == 10


def test_logistic_recovers_a_known_effect() -> None:
    rng = np.random.default_rng(0)
    x1 = rng.integers(0, 2, 20_000).astype(float)
    p = 1 / (1 + np.exp(-(-2.0 + 1.5 * x1)))
    y = (rng.random(20_000) < p).astype(float)
    x = np.column_stack([np.ones_like(x1), x1])
    beta = C.fit_logistic(x, y, lam=0.0)
    assert abs(beta[0] + 2.0) < 0.1 and abs(beta[1] - 1.5) < 0.12


def test_feature_preferences_match_folding_preference_events() -> None:
    feats = pd.DataFrame({"X_1": [0.5, np.nan], "seg": ["a", "b"], "n": [3, 4]})
    prefs = C.feature_preferences(feats)
    events = {
        cid: [
            Event(
                event_id=f"{cid}_{col}",
                customer_id=cid,
                type=EventType.PREFERENCE,
                ts=T0,
                source="t",
                attrs={"key": col, "value": val},
            )
            for col, val in p.items()
        ]
        + [Event(event_id=f"{cid}_su", customer_id=cid, type=EventType.SIGNUP, ts=T0, source="t")]
        for cid, p in zip(["a", "b"], prefs, strict=True)
    }
    states = C.fold_states(events, T0 + timedelta(days=1))
    assert states["a"].preferences == {"X_1": "0.5", "seg": "a", "n": "3"}
    assert states["b"].preferences == prefs[1] == {"seg": "b", "n": "4"}


def test_calibration_ratio_and_no_contact() -> None:
    c = C.calibration(4.0, {"point": 0.2, "low": 0.1, "high": 0.3}, 0.5)
    assert c["measured_per_contact"]["point"] == pytest.approx(0.4)
    assert c["ratio"] == pytest.approx(10.0) and c["within_2x"] is False
    assert C.calibration(0.5, {"point": 0.2, "low": 0.1, "high": 0.3}, 0.5)["within_2x"] is True
    assert C.calibration(None, {"point": 0.0, "low": 0.0, "high": 0.0}, 0.0)["ratio"] is None


def _synthetic(n: int = 600, seed: int = 4) -> tuple[C.BenchData, list[list[str]]]:
    """A 50/50 e-mail trial where the e-mail adds conversions only for x == 1, as an adapter builds it."""
    rng = np.random.default_rng(seed)
    ids = np.array([f"s{i}" for i in range(n)], dtype=object)
    x = rng.integers(0, 2, n)
    arm = rng.integers(0, 2, n).astype(np.int64)
    conv = (rng.random(n) < 0.05 + 0.3 * arm * x).astype(float)
    feats = pd.DataFrame({"x": x.astype(float), "z": rng.normal(size=n)})
    built = A._feature_rct(
        "synthetic",
        ids,
        arm,
        0.5,
        feats,
        conv,
        None,
        (("x",), ("z",)),
        ("inline fixture",),
        {"rows": n},
    )
    calls: list[list[str]] = []
    inner = built.agent.campaign_events

    def recording(sel: Sequence[str]) -> dict[str, list[Event]]:
        calls.append(list(sel))
        return inner(sel)

    built.agent.campaign_events = recording
    return built, calls


def test_end_to_end_on_a_synthetic_trial_uses_train_only() -> None:
    data, calls = _synthetic()
    out = C.evaluate_dataset(data, seeds=1, timings=C.Timings(quiet=True))
    train, test = C.split_by_hash(data.customer_ids)
    assert calls and set(calls[0]) == set(data.customer_ids[train].tolist())
    assert not set(calls[0]) & set(data.customer_ids[test].tolist())
    assert out["train"] == len(train) and out["test"] == len(test)
    pol = out["policies"]
    for name in ("send to none", "send to all: treated", "T-learner (logistic)", "AGENT"):
        assert name in pol
        assert pol[name]["dr"]["low"] <= pol[name]["dr"]["point"] <= pol[name]["dr"]["high"]
    assert any(k.startswith("segment rule (") for k in pol)
    assert pol["send to none"]["contact_rate"] == 0.0
    assert 0.0 <= out["agent"]["contact_rate"] <= 1.0
    assert out["agent"]["history_customers"] == len(train)
    assert out["best_simple"]["name"] in pol
    assert set(out["verdict"]) >= {
        "ties_or_beats_best_simple",
        "beats_best_simple",
        "not_worse_than_send_to_all",
    }
    assert "suggestive" in out["verdict"]["intervals"]
    # the conversion table scores the same policies
    assert set(out["conversion"]) == set(pol)
    md = render({"datasets": {"synthetic": out}})
    assert "| synthetic |" in md and "### synthetic" in md


def test_break_even_cost_is_measured_on_train_only() -> None:
    data, _ = _synthetic()
    train, test = C.split_by_hash(data.customer_ids)
    c, a = data.conversion, data.arm
    ate = c[train][a[train] == 1].mean() - c[train][a[train] == 0].mean()
    assert data.arm_cost[1] == pytest.approx(20.0 * ate)
    ate_test = c[test][a[test] == 1].mean() - c[test][a[test] == 0].mean()
    assert data.arm_cost[1] != pytest.approx(20.0 * ate_test)


def _hillstrom_rows(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    seg = np.array(A.HILLSTROM_ARMS)[np.arange(n) % 3]
    visit = (rng.random(n) < 0.3).astype(int)
    conv = ((rng.random(n) < 0.2) & (visit == 1)).astype(int)
    frame = pd.DataFrame(
        {
            "recency": rng.integers(1, 13, n),
            "history_segment": ["2) $100 - $200"] * n,
            "history": rng.uniform(30, 400, n).round(2),
            "mens": rng.integers(0, 2, n),
            "womens": rng.integers(0, 2, n),
            "zip_code": ["Urban", "Rural", "Surburban"] * (n // 3),
            "newbie": rng.integers(0, 2, n),
            "channel": ["Web", "Phone", "Multichannel"] * (n // 3),
            "segment": seg,
            "visit": visit,
            "conversion": conv,
            "spend": conv * 90.0,
        }
    )
    frame["customer_id"] = [f"h{i:05d}" for i in range(n)]
    return frame


def test_hillstrom_adapter_writes_the_campaign_as_a_connector_would() -> None:
    frame = _hillstrom_rows()
    data = A.hillstrom(Path("/nonexistent"), frame=frame)
    assert data.n == 60 and data.n_arms == 3
    assert np.allclose(data.propensity, 1 / 3)
    assert data.arm_cost == (0.0, A.HILLSTROM_EMAIL_COST, A.HILLSTROM_EMAIL_COST)
    ids = [str(c) for c in data.customer_ids]
    pre = data.agent.pre_events(ids)
    assert all(
        e.type is not EventType.SEND and not e.attrs.get("outcome") for evs in pre.values() for e in evs
    )
    camp = data.agent.campaign_events(ids)
    sends = [e for evs in camp.values() for e in evs if e.type is EventType.SEND]
    assert len(sends) == int((data.arm != 0).sum())
    for e in sends:
        assert e.attrs["content_id"] in ("hill_mens", "hill_womens")
        assert e.attrs["logged_probability"] == pytest.approx(1 / 3)
    for evs in camp.values():
        did = {e.decision_id for e in evs if e.type is EventType.SEND}
        for e in evs:
            if e.type is EventType.CLICK:
                assert e.decision_id in did and e.channel is Channel.EMAIL
    control = [c for c, a in zip(ids, data.arm, strict=True) if a == 0]
    assert all(e.type is not EventType.CLICK for c in control for e in camp[c])
    themes = {c.content_id: c.theme for c in data.agent.content}
    assert themes == {"hill_mens": Theme.QUALITY, "hill_womens": Theme.NOVELTY}
    assert data.profit()[data.arm != 0].min() >= -A.HILLSTROM_EMAIL_COST - 1e-12


def test_arm_of_maps_decisions_and_refuses_unknown_content() -> None:
    data, _ = _synthetic(n=30)
    from autonomy.contracts import Action, Decision, DecisionKind

    act = Action(goal_id="goal_synthetic", channel=Channel.EMAIL, content_id="synthetic_msg", send_at=T0)
    d = Decision(
        decision_id="d",
        customer_id="s1",
        made_at=T0,
        kind=DecisionKind.ACT,
        action=act,
        probability=1.0,
        reason="t",
        policy_version="t",
    )
    assert data.agent.arm_of(d) == 1
    nothing = d.model_copy(update={"decision_id": "n", "kind": DecisionKind.NOTHING, "action": None})
    assert data.agent.arm_of(nothing) == 0
    bad = d.model_copy(update={"action": act.model_copy(update={"content_id": "other"})})
    with pytest.raises(ValueError, match="not an arm"):
        data.agent.arm_of(bad)


def test_fetch_verifies_checksums(tmp_path: Path) -> None:
    import hashlib

    from scripts.v2_realbench import fetch as F  # noqa: N812

    src = tmp_path / "src.csv.gz"
    src.write_bytes(b"customer,arm\n1,0\n")
    good = F.Source("t", "t", "f.csv.gz", src.as_uri(), 17, hashlib.sha256(src.read_bytes()).hexdigest())
    out = F.fetch(good, tmp_path / "data")
    assert out.read_bytes() == src.read_bytes()
    assert F.fetch(good, tmp_path / "data") == out  # present and correct: not fetched again
    bad = F.Source("t", "t", "g.csv.gz", src.as_uri(), 17, "0" * 64)
    with pytest.raises(ValueError, match="checksum"):
        F.fetch(bad, tmp_path / "data")
    assert not (tmp_path / "data" / "t" / "g.csv.gz").exists()
    assert {s.dataset for s in F.SOURCES} == {"hillstrom", "x5", "criteo", "megafon"}
