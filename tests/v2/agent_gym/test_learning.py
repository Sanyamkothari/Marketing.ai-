"""The fast learning loop: closed windows, control from the holdout only, themes, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import (
    Action,
    Channel,
    ContentItem,
    ContentStatus,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Group,
    Prior,
    PriorSource,
    Reward,
    Theme,
)
from autonomy.decide.models import ANY, DEFAULT_CONTROL_PRIOR, ValueModel
from autonomy.learning import (
    LearnedValueModel,
    ModelBundle,
    build_bundle,
    holdout_anchors,
    learn_from_rewards,
    prior_values,
    update_models,
)

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
H = 14
KEY_SAVINGS = "email|none|savings"
KEY_QUALITY = "email|none|quality"


def _content(cid: str, theme: Theme) -> ContentItem:
    return ContentItem(
        content_id=cid,
        channel=Channel.EMAIL,
        theme=theme,
        body="Hi {{first_name}}",
        status=ContentStatus.APPROVED,
        created_at=T0,
    )


LIBRARY = {"c_sav": _content("c_sav", Theme.SAVINGS), "c_qual": _content("c_qual", Theme.QUALITY)}


def _act(did: str, cid: str, at: datetime, content_id: str = "c_sav", group: Group = Group.AGENT) -> Decision:
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.ACT,
        action=Action(goal_id="g", channel=Channel.EMAIL, content_id=content_id, send_at=at),
        group=group,
        probability=0.5,
        reason="test",
        policy_version="t",
    )


def _nothing(did: str, cid: str, at: datetime, group: Group, p: float = 1.0) -> Decision:
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=at,
        kind=DecisionKind.NOTHING,
        group=group,
        probability=p,
        reason="test",
        policy_version="t",
    )


def _ev(eid: str, cid: str, typ: EventType, at: datetime, **kw: object) -> Event:
    if typ in (EventType.SEND, EventType.DELIVER, EventType.UNSUBSCRIBE, EventType.COMPLAINT):
        kw.setdefault("channel", Channel.EMAIL)
    if typ is EventType.PURCHASE:
        kw.setdefault("value", 40.0)
    return Event(event_id=eid, customer_id=cid, type=typ, ts=at, source="test", **kw)  # type: ignore[arg-type]


def _send(did: str, cid: str, at: datetime) -> Event:
    return _ev(f"s_{did}", cid, EventType.SEND, at, decision_id=did)


def _counts(bundle: ModelBundle, key: str | None) -> tuple[float, float]:
    """Pooled (successes, failures) learned for an action key, or the control when key is None."""
    if key is None:
        post = bundle.response.posterior_control(ANY)
        return post.alpha - DEFAULT_CONTROL_PRIOR.alpha, post.beta - DEFAULT_CONTROL_PRIOR.beta
    base = build_bundle().response.posterior_treated(ANY, key)
    post = bundle.response.posterior_treated(ANY, key)
    return post.alpha - base.alpha, post.beta - base.beta


# --- closed windows -------------------------------------------------------------------------------


def test_treated_window_closes_after_horizon_from_send() -> None:
    d = _act("d1", "c1", T0)
    events = {
        "c1": [
            _send("d1", "c1", T0 + timedelta(hours=3)),
            _ev("p1", "c1", EventType.PURCHASE, T0 + timedelta(days=5)),
        ]
    }
    bundle = build_bundle()

    early = update_models(bundle, [d], events, LIBRARY, T0 + timedelta(days=H), H)
    assert early.still_open == 1 and early.treated == 0  # send was 3 h after the decision
    assert _counts(bundle, KEY_SAVINGS) == (0.0, 0.0)

    late = update_models(bundle, [d], events, LIBRARY, T0 + timedelta(days=H, hours=3), H)
    assert late.treated == 1 and late.windows_closed == 1 and late.fatigue == 1
    assert _counts(bundle, KEY_SAVINGS) == pytest.approx((1.0, 0.0))


def test_purchase_after_window_is_not_a_conversion() -> None:
    d = _act("d1", "c1", T0)
    events = {
        "c1": [_send("d1", "c1", T0), _ev("p1", "c1", EventType.PURCHASE, T0 + timedelta(days=H, hours=1))]
    }
    bundle = build_bundle()
    update_models(bundle, [d], events, LIBRARY, T0 + timedelta(days=30), H, learn_value=False)
    assert _counts(bundle, KEY_SAVINGS) == pytest.approx((0.0, 1.0))


def test_unsent_act_is_closed_but_not_learned_as_treated() -> None:
    d = _act("d1", "c1", T0)
    bundle = build_bundle()
    report = update_models(bundle, [d], {"c1": []}, LIBRARY, T0 + timedelta(days=30), H)
    assert report.not_sent == 1 and report.treated == 0 and report.fatigue == 0
    assert "d1" in bundle.learned_decisions


def test_undelivered_send_is_not_learned_as_treated_or_fatigue() -> None:
    decisions = [_act("d1", "c1", T0), _act("d2", "c2", T0)]
    events = {
        # the world records the SEND, then reports it failed (no consent, already unsubscribed)
        "c1": [
            _send("d1", "c1", T0),
            _ev("f1", "c1", EventType.FAIL, T0, decision_id="d1", channel=Channel.EMAIL),
        ],
        "c2": [
            _send("d2", "c2", T0),
            _ev("dl2", "c2", EventType.DELIVER, T0 + timedelta(minutes=1), decision_id="d2"),
        ],
    }
    bundle = build_bundle()
    report = update_models(bundle, decisions, events, LIBRARY, T0 + timedelta(days=30), H)
    assert report.not_delivered == 1 and report.treated == 1 and report.fatigue == 1
    assert _counts(bundle, KEY_SAVINGS) == pytest.approx((0.0, 1.0))
    assert {"d1", "d2"} <= bundle.learned_decisions


def test_unsubscribe_naming_the_decision_is_attributed_to_it() -> None:
    decisions = [_act("d1", "c1", T0), _act("d2", "c1", T0 + timedelta(days=1))]
    events = {
        "c1": [
            _send("d1", "c1", T0),
            _send("d2", "c1", T0 + timedelta(days=1)),
            # after d2 was sent, but it names d1: d1 caused it, d2 did not
            _ev("u1", "c1", EventType.UNSUBSCRIBE, T0 + timedelta(days=1, hours=2), decision_id="d1"),
        ]
    }
    bundle = build_bundle()
    fresh = build_bundle()
    update_models(bundle, decisions, events, LIBRARY, T0 + timedelta(days=30), H)
    assert bundle.fatigue.p_unsub(0) > fresh.fatigue.p_unsub(0)  # d1 had no earlier sends


def test_customer_missing_from_events_is_left_for_later() -> None:
    bundle = build_bundle()
    report = update_models(bundle, [_act("d1", "c1", T0)], {}, LIBRARY, T0 + timedelta(days=30), H)
    assert report.no_events == 1 and "d1" not in bundle.learned_decisions


# --- theme mapping --------------------------------------------------------------------------------


def test_action_key_uses_the_content_theme() -> None:
    decisions = [
        _act("d1", "c1", T0, "c_sav"),
        _act("d2", "c2", T0, "c_qual"),
        _act("d3", "c3", T0, "c_gone"),
    ]
    events = {
        "c1": [_send("d1", "c1", T0), _ev("p1", "c1", EventType.PURCHASE, T0 + timedelta(days=1))],
        "c2": [_send("d2", "c2", T0)],
        "c3": [_send("d3", "c3", T0)],
    }
    bundle = build_bundle()
    report = update_models(bundle, decisions, events, LIBRARY, T0 + timedelta(days=20), H)
    assert _counts(bundle, KEY_SAVINGS) == pytest.approx((1.0, 0.0))
    assert _counts(bundle, KEY_QUALITY) == pytest.approx((0.0, 1.0))
    assert report.treated == 2 and report.unknown_content == 1
    assert report.fatigue == 3  # fatigue does not need the theme


def test_segment_of_keys_observations_by_segment() -> None:
    n = 40
    decisions = [_act(f"d{k}", f"c{k}", T0) for k in range(n)]
    events = {
        f"c{k}": [
            _send(f"d{k}", f"c{k}", T0),
            _ev(f"p{k}", f"c{k}", EventType.PURCHASE, T0 + timedelta(days=1)),
        ]
        for k in range(n)
    }
    bundle = build_bundle()
    segments = {f"c{k}": "vip" for k in range(n)}
    update_models(bundle, decisions, events, LIBRARY, T0 + timedelta(days=20), H, segment_of=segments)
    vip = bundle.response.posterior_treated("vip", KEY_SAVINGS)
    other = bundle.response.posterior_treated("new", KEY_SAVINGS)  # borrows vip's data, shrunk
    assert vip.mean > other.mean > build_bundle().response.posterior_treated("new", KEY_SAVINGS).mean


# --- control only from the holdout ----------------------------------------------------------------


def test_control_comes_only_from_holdout_by_default() -> None:
    decisions = [
        _nothing("h1", "ch", T0, Group.HOLDOUT),
        _nothing("a1", "ca", T0, Group.AGENT, p=0.4),
        _nothing("b1", "cb", T0, Group.BASELINE),
    ]
    buy = T0 + timedelta(days=2)
    events = {cid: [_ev(f"p_{cid}", cid, EventType.PURCHASE, buy)] for cid in ("ch", "ca", "cb")}
    bundle = build_bundle()
    report = update_models(bundle, decisions, events, LIBRARY, T0 + timedelta(days=20), H)
    assert report.control_holdout == 1 and report.control_agent_ipw == 0
    assert _counts(bundle, None) == pytest.approx((1.0, 0.0))
    assert "a1" not in bundle.learned_decisions  # unused, so a later IPW pass could still use it


def test_agent_nothing_counts_only_with_ipw_and_weights_sum_to_n() -> None:
    decisions = [
        _nothing("a1", "c1", T0, Group.AGENT, p=0.25),  # raw weight 4, bought
        _nothing("a2", "c2", T0, Group.AGENT, p=1.0),  # raw weight 1, did not buy
    ]
    events = {"c1": [_ev("p1", "c1", EventType.PURCHASE, T0 + timedelta(days=1))], "c2": []}
    bundle = build_bundle()
    report = update_models(
        bundle, decisions, events, LIBRARY, T0 + timedelta(days=20), H, agent_nothing_ipw=True
    )
    assert report.control_agent_ipw == 2
    hits, misses = _counts(bundle, None)
    assert hits + misses == pytest.approx(2.0)
    assert hits == pytest.approx(2 * 4 / 5)


def test_control_window_with_a_send_is_dropped() -> None:
    d = _nothing("h1", "ch", T0, Group.HOLDOUT)
    events = {"ch": [_ev("s_x", "ch", EventType.SEND, T0 + timedelta(days=1))]}
    bundle = build_bundle()
    report = update_models(bundle, [d], events, LIBRARY, T0 + timedelta(days=20), H)
    assert report.contaminated == 1 and report.control_holdout == 0


def test_daily_holdout_decisions_are_discounted_for_overlap() -> None:
    decisions = [_nothing(f"h{k}", "ch", T0 + timedelta(days=k), Group.HOLDOUT) for k in range(H + 1)]
    bundle = build_bundle()
    update_models(bundle, decisions, {"ch": []}, LIBRARY, T0 + timedelta(days=60), H, learn_value=False)
    hits, misses = _counts(bundle, None)
    # the first window counts 1, each next daily one 1/14: 15 windows ~ 2 independent ones
    assert hits == 0 and misses == pytest.approx(1 + H / H)


def test_virtual_holdout_windows_one_per_night() -> None:
    events = {
        "h1": [
            _ev("v1", "h1", EventType.VISIT, T0 - timedelta(days=30)),
            _ev("p1", "h1", EventType.PURCHASE, T0 + timedelta(days=3)),
        ],
        "h2": [_ev("v2", "h2", EventType.VISIT, T0 + timedelta(days=10))],  # arrived later
    }
    bundle = build_bundle()
    night1 = T0 + timedelta(days=H)
    r1 = update_models(bundle, [], events, LIBRARY, night1, H, holdout_customers=["h1", "h2"])
    assert r1.control_holdout == 1  # h2 did not exist at the window start
    assert _counts(bundle, None) == pytest.approx((1.0, 0.0))
    again = update_models(bundle, [], events, LIBRARY, night1, H, holdout_customers=["h1", "h2"])
    assert again.control_holdout == 0
    r3 = update_models(bundle, [], events, LIBRARY, night1 + timedelta(days=2), H, holdout_customers=["h1"])
    assert r3.control_holdout == 2
    assert holdout_anchors(night1, H, None) == [T0]


# --- idempotency ----------------------------------------------------------------------------------


def test_update_is_idempotent() -> None:
    decisions = [_act("d1", "c1", T0), _nothing("h1", "ch", T0, Group.HOLDOUT)]
    events = {
        "c1": [
            _send("d1", "c1", T0),
            _ev("p1", "c1", EventType.PURCHASE, T0 + timedelta(days=2), margin=10.0),
        ],
        "ch": [],
    }
    bundle = build_bundle()
    now = T0 + timedelta(days=20)
    first = update_models(bundle, decisions, events, LIBRARY, now, H)
    snapshot = (_counts(bundle, KEY_SAVINGS), _counts(bundle, None), bundle.fatigue.p_unsub(0))
    second = update_models(bundle, decisions, events, LIBRARY, now + timedelta(days=5), H)
    assert first.response == 2 and first.value == 1
    assert second.response == 0 and second.value == 0 and second.fatigue == 0
    assert second.already_learned == 2
    assert (_counts(bundle, KEY_SAVINGS), _counts(bundle, None), bundle.fatigue.p_unsub(0)) == snapshot
    assert isinstance(bundle.value, LearnedValueModel) and bundle.value.stats.orders == 1


def test_learn_from_rewards_is_idempotent_and_learns_fatigue() -> None:
    d = _act("d1", "c1", T0)
    reward = Reward(
        decision_id="d1",
        customer_id="c1",
        goal_id="g",
        window_end=T0 + timedelta(days=H),
        converted=False,
        net=0.0,
        unsubscribed=True,
    )
    bundle = build_bundle()
    before = bundle.fatigue.p_unsub(3)
    r1 = learn_from_rewards(bundle, [reward], {"d1": d}, LIBRARY, horizon_days=H, contacts_7d_at={"d1": 3})
    r2 = learn_from_rewards(bundle, [reward], {"d1": d}, LIBRARY, horizon_days=H, contacts_7d_at={"d1": 3})
    assert (r1.treated, r1.fatigue) == (1, 1) and (r2.treated, r2.fatigue) == (0, 0)
    assert bundle.fatigue.p_unsub(3) > before


# --- fatigue and value ----------------------------------------------------------------------------


def test_fatigue_counts_prior_sends_and_unsubscribes() -> None:
    decisions = [_act(f"d{k}", "c1", T0 + timedelta(days=k)) for k in range(3)]
    events = {"c1": [_send(f"d{k}", "c1", T0 + timedelta(days=k)) for k in range(3)]}
    events["c1"].append(_ev("u1", "c1", EventType.UNSUBSCRIBE, T0 + timedelta(days=2, hours=1)))
    bundle = build_bundle()
    fresh = build_bundle()
    update_models(bundle, decisions, events, LIBRARY, T0 + timedelta(days=30), H)
    # the third send (two sends earlier that week) carried the unsubscribe
    assert bundle.fatigue.p_unsub(2) > fresh.fatigue.p_unsub(2)
    assert bundle.fatigue.p_unsub(0) < fresh.fatigue.p_unsub(0)


def test_value_model_learns_order_value_and_margin() -> None:
    events = {"c1": [_ev(f"p{k}", "c1", EventType.PURCHASE, T0, value=100.0, margin=60.0) for k in range(40)]}
    bundle = build_bundle()
    update_models(bundle, [], events, LIBRARY, T0 + timedelta(days=1), H)
    assert isinstance(bundle.value, LearnedValueModel)
    assert bundle.value.learned_order_value == pytest.approx((50 * 20 + 4000) / 60)
    assert 0.4 < bundle.value.learned_margin_rate < 0.6
    assert bundle.fatigue.value_model is bundle.value  # shared, so the attention price moves too


# --- priors ---------------------------------------------------------------------------------------


def test_build_bundle_seeds_from_prior_values() -> None:
    prior = Prior(
        key="gym/defaults",
        source=PriorSource.GYM,
        values={
            "treated/*/email|*|savings": 0.2,
            "control/*": 0.02,
            "value/order_value": 80.0,
            "fatigue/base_rate": 0.01,
            "unrelated": 3.0,
        },
        updated_at=T0,
    )
    bundle = build_bundle(prior_values([prior]))
    assert bundle.response.posterior_treated(ANY, KEY_SAVINGS).mean == pytest.approx(0.2)
    assert bundle.response.posterior_control(ANY).mean == pytest.approx(0.02)
    assert isinstance(bundle.value, ValueModel) and bundle.value.default_order_value == 80.0
    assert bundle.fatigue.p_unsub(0) == pytest.approx(0.01)
    plain = build_bundle(None)
    assert plain.fatigue.p_unsub(0) == pytest.approx(0.002)
