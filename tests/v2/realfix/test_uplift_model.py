"""autonomy.uplift: the campaign table, the out-of-sample calibrated model and its refusals."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from autonomy.contracts import CustomerState, Decision, DecisionKind, Event, EventType, Group
from autonomy.uplift import campaign_table, fit_uplift
from autonomy.uplift.features import FeatureEncoder, behaviour_from_events, behaviour_from_state
from autonomy.uplift.learner import SharedBaselineLogit
from autonomy.uplift.model import FittedUplift
from tests.v2.realfix.test_uplift_support import NOW, T0, campaign, content

KEY = "email|none|quality"


def _library() -> dict[str, object]:
    return {c.content_id: c for c in content()}


def _fit(**kw: object) -> tuple[FittedUplift | None, list[str]]:
    events, states = campaign(**kw)  # type: ignore[arg-type]
    table, notes = campaign_table(events, states, _library(), NOW)  # type: ignore[arg-type]
    if table is None:
        return None, notes
    return fit_uplift(table)


def _who(tier: str) -> CustomerState:
    return CustomerState(
        customer_id=f"new_{tier}", as_of=NOW, preferences={"tier": tier, "age": "30"}, first_seen_at=T0
    )


def test_table_reads_arms_controls_and_outcomes() -> None:
    events, states = campaign(n=1000)
    table, notes = campaign_table(events, states, _library(), NOW)  # type: ignore[arg-type]
    assert table is not None, notes
    assert table.arm_keys == (KEY,)
    assert table.randomised and not table.explicit_control
    sent = sum(1 for evs in events.values() if any(e.type is EventType.SEND for e in evs))
    assert int((table.arm == 1).sum()) == sent
    assert int((table.arm == 0).sum()) == 1000 - sent
    np.testing.assert_allclose(table.propensity, 0.5)
    bought = sum(1 for evs in events.values() if any(e.type is EventType.PURCHASE for e in evs))
    assert table.y.sum() == bought


def test_features_never_see_the_window() -> None:
    """Behaviour is rebuilt from events before the send: the purchase the campaign caused is the
    outcome, never a feature."""
    events, states = campaign(n=800, base=1.0)
    table, _ = campaign_table(events, states, _library(), NOW)  # type: ignore[arg-type]
    assert table is not None
    assert table.y.min() == 1.0
    assert float(np.nansum(table.behaviour[:, 0])) == 0.0  # no purchase counted before t0
    evs = events[table.customer_ids[0]]
    assert behaviour_from_events(evs, T0)[0] == 0.0
    assert behaviour_from_events(evs, NOW)[0] == 1.0


def test_lift_where_features_say_and_only_there() -> None:
    model, notes = _fit(n=4000, lift_a=0.12, lift_b=0.0)
    assert model is not None, notes
    arm = model.report.arms[0]
    assert arm.slope_used > 0.5, model.report.summary()
    assert abs(arm.ate - 0.06) < 3 * arm.ate_se + 0.01
    a, b = model.score(_who("a"), KEY, NOW), model.score(_who("b"), KEY, NOW)
    assert a is not None and b is not None
    assert a.mean > b.mean + 0.04
    assert a.sd > 0 and a.own_sd is not None and a.own_sd <= a.sd
    assert model.score(_who("a"), "sms|none|quality", NOW) is None  # no arm, no score


def test_no_heterogeneity_means_everyone_gets_the_average() -> None:
    model, notes = _fit(n=4000, lift_a=0.06, lift_b=0.06, seed=3)
    assert model is not None, notes
    a, b = model.score(_who("a"), KEY, NOW), model.score(_who("b"), KEY, NOW)
    assert a is not None and b is not None
    assert abs(a.mean - b.mean) < 0.03
    assert abs(a.mean - 0.06) < 0.04


def test_deterministic() -> None:
    m1, _ = _fit(n=1500)
    m2, _ = _fit(n=1500)
    assert m1 is not None and m2 is not None
    assert m1.score(_who("a"), KEY, NOW) == m2.score(_who("a"), KEY, NOW)


def test_two_arms_each_measured() -> None:
    model, notes = _fit(n=6000, two_arms=True, lift_a=0.12, lift_b=0.04)
    assert model is not None, notes
    by_key = {a.key: a for a in model.report.arms}
    assert set(by_key) == {"email|none|quality", "email|none|novelty"}
    assert by_key["email|none|quality"].ate > by_key["email|none|novelty"].ate


def test_targeted_campaign_is_refused() -> None:
    """Only tier a was ever sent: the inferred control group is not comparable, so no model."""
    model, notes = _fit(n=3000, targeted=True)
    assert model is None
    assert any("not a fair comparison" in n for n in notes)


def test_no_logged_probability_no_campaign() -> None:
    model, notes = _fit(n=600, logged=False)
    assert model is None
    assert any("logged its probability" in n for n in notes)


def test_no_control_group_no_model() -> None:
    model, notes = _fit(n=1000, p_send=0.99)
    assert model is None
    assert any("control" in n for n in notes)


def test_window_still_open_no_model() -> None:
    events, states = campaign(n=600)
    table, notes = campaign_table(events, states, _library(), T0 + timedelta(days=3))  # type: ignore[arg-type]
    assert table is None and any("closed" in n for n in notes)


def test_explicit_holdout_decisions_are_the_control() -> None:
    events, states = campaign(n=1200)
    sent = {c for c, evs in events.items() if any(e.type is EventType.SEND for e in evs)}
    held = sorted(c for c in events if c not in sent)[:300]
    decisions = [
        Decision(
            decision_id=f"d_{c}",
            customer_id=c,
            made_at=T0,
            kind=DecisionKind.NOTHING,
            group=Group.HOLDOUT,
            probability=0.5,
            reason="past campaign: control",
            policy_version="t",
        )
        for c in held
    ]
    table, notes = campaign_table(events, states, _library(), NOW, decisions=decisions)  # type: ignore[arg-type]
    assert table is not None, notes
    assert table.explicit_control
    assert int((table.arm == 0).sum()) == 300


def test_propensity_logged_campaign_is_weighted() -> None:
    model, notes = _fit(n=4000, varying=True)
    assert model is not None, notes
    assert not model.report.randomised
    assert any("estimated from the features" in n for n in notes)


def test_propensity_logged_campaign_recovers_the_effect_a_naive_comparison_misses() -> None:
    """Over-40s are sent more often (0.7 vs 0.3) AND buy anyway three times as often: the plain
    sent-minus-not difference is biased upwards; the weighted, doubly robust average is not."""
    events, states = campaign(
        n=12000, varying=True, confounded=True, lift_a=0.10, lift_b=0.10, base=0.04, seed=11
    )
    table, notes = campaign_table(events, states, _library(), NOW)  # type: ignore[arg-type]
    assert table is not None, notes
    naive = float(table.y[table.arm == 1].mean() - table.y[table.arm == 0].mean())
    model, notes = fit_uplift(table)
    assert model is not None, notes
    arm = model.report.arms[0]
    assert naive > 0.10 + 0.025  # the confounding is real in this campaign
    assert abs(arm.ate - 0.10) < 3 * arm.ate_se + 0.01, (arm.ate, arm.ate_se, naive)


def test_state_and_events_give_the_same_columns() -> None:
    """A customer encodes the same at decision time (their state, folded by the production
    `StateFolder`) as at fitting time (rebuilt from their events): purchases, net revenue after a
    refund, recency and tenure, against an encoder whose behaviour bins actually cut."""
    from autonomy.data.folder import StateFolder
    from autonomy.data.rebuild import rebuild_states
    from autonomy.fakes.stores import InMemoryEventStore, InMemoryStateStore

    events: dict[str, list[Event]] = {}
    for i in range(60):
        cid = f"h{i:03d}"
        evs = [
            Event(
                event_id=f"{cid}_s",
                customer_id=cid,
                type=EventType.SIGNUP,
                ts=T0 - timedelta(days=100 + 5 * i),
                source="t",
            )
        ]
        for j in range(i % 4):
            evs.append(
                Event(
                    event_id=f"{cid}_p{j}",
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=T0 - timedelta(days=3 + 7 * j + i % 5),
                    source="t",
                    value=20.0 + 10 * j + i,
                )
            )
        if i % 3 == 0 and i % 4:
            evs.append(
                Event(
                    event_id=f"{cid}_r",
                    customer_id=cid,
                    type=EventType.REFUND,
                    ts=T0 - timedelta(days=1),
                    source="t",
                    value=15.0,
                )
            )
        events[cid] = evs
    store, folded = InMemoryEventStore(), InMemoryStateStore()
    store.append([e for evs in events.values() for e in evs])
    rebuild_states(store, folded, StateFolder(), T0)
    by_id = {s.customer_id: s for s in folded.scan()}
    rows = np.array([behaviour_from_events(events[c], T0) for c in sorted(events)])
    enc = FeatureEncoder.fit([{} for _ in rows], rows)
    assert enc.blocks >= 3  # the behaviour bins cut, so a mismatch would show
    for cid in sorted(events):
        assert behaviour_from_state(by_id[cid], T0) == pytest.approx(
            behaviour_from_events(events[cid], T0), nan_ok=True
        )
        assert enc.active({}, behaviour_from_state(by_id[cid], T0)) == enc.active(
            {}, behaviour_from_events(events[cid], T0)
        )


def test_learner_converges_on_rare_outcomes() -> None:
    """A few buyers in a small control group must not send Newton off to infinity."""
    rng = np.random.default_rng(0)
    n, blocks, bins = 20000, 12, 7
    x = np.zeros((n, 1 + blocks * bins), dtype=np.float32)
    x[:, 0] = 1
    for f in range(blocks):
        x[np.arange(n), 1 + f * bins + rng.integers(0, bins, n)] = 1
    x[:, 1 + 11 * bins] = 1  # a constant block, as an attribute nobody varies on gives
    arm = (rng.random(n) < 0.85).astype(np.int64)
    y = (rng.random(n) < 0.002 * (1 + arm)).astype(np.float64)
    fit = SharedBaselineLogit().fit(x, arm, y, np.ones(n), 1, blocks)
    assert np.abs(fit.beta).max() < 20 and np.abs(fit.gamma).max() < 20
    mu0, mu = fit.predict(x)
    assert 0.0005 < mu0.mean() < 0.005 and mu.mean() > mu0.mean()


@pytest.mark.parametrize("bad", ["", "abc", "nan", "inf"])
def test_bad_attribute_values_are_missing(bad: str) -> None:
    enc = FeatureEncoder.fit([{"age": str(a)} for a in range(100)], np.zeros((100, 4)))
    missing = enc.active({}, (0.0, 0.0, float("nan"), float("nan")))
    assert enc.active({"age": bad}, (0.0, 0.0, float("nan"), float("nan"))) == missing


def test_event_helpers_ignore_later_events() -> None:
    e = Event(event_id="p", customer_id="c", type=EventType.PURCHASE, ts=NOW, source="t", value=10.0)
    assert behaviour_from_events([e], T0)[0] == 0.0
