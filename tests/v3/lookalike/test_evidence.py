"""What the model learns from: past campaigns rebuilt point in time, nightly features, and the
weights that keep a targeted or much-decided customer from counting as many strangers."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import Action, Channel, Decision, DecisionKind, Event, EventType, Group
from autonomy.decide.lookalike import (
    BASE_NAMES,
    LookalikeConfig,
    LookalikeResponseModel,
    Outcome,
    OutcomeBook,
    history_profile,
    profile_of,
)
from autonomy.gym import default_brief, default_content
from autonomy.learning import ModelBundle, build_bundle
from autonomy.learning.calibration import HistoryDigest
from autonomy.learning.observations import LoggedSend, Timeline
from autonomy.runtime.agent import ClickResponseModel, LookalikeClickModel, build_agent_policy

from .synth import KEY, T0, WINDOW, fitted, make_state, outcomes

COL = {name: i for i, name in enumerate(BASE_NAMES)}


# --- history_profile ------------------------------------------------------------------------------


def test_a_send_at_the_profile_moment_is_the_treatment_not_its_history() -> None:
    at = T0
    first = at - timedelta(days=200)
    bought = [(at - timedelta(days=50), 20.0)]
    treated = history_profile("t", at, first_seen=first, purchases=bought, sends=[at])
    control = history_profile("c", at, first_seen=first, purchases=bought, sends=[])
    earlier = history_profile("e", at, first_seen=first, purchases=bought, sends=[at - timedelta(days=2)])
    assert treated.base[COL["contacts_7d"]] == control.base[COL["contacts_7d"]] == 0.0
    assert treated.base[COL["contacts_30d"]] == 0.0
    assert earlier.base[COL["contacts_7d"]] == 1.0


# --- the warm start from a past randomised campaign -----------------------------------------------

START = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)


def _event(eid: str, cid: str, kind: EventType, ts: datetime, value: float | None = None) -> Event:
    channel = Channel.EMAIL if kind is EventType.SEND else None
    return Event(event_id=eid, customer_id=cid, type=kind, ts=ts, source="t", value=value, channel=channel)


def _send(cid: str, offer: str) -> LoggedSend:
    return LoggedSend(
        customer_id=cid,
        sent_at=START,
        channel=Channel.EMAIL,
        offer_id=offer,
        theme="reminder",
        propensity=0.4,
        delivered=True,
        clicked=False,
        click_at=None,
        decision_id=None,
        campaign_id="spring",
    )


def test_the_warm_start_rebuilds_each_customer_before_the_campaign() -> None:
    policy = build_agent_policy([default_brief()], default_content(), lookalike=True)
    model = policy.bundle.response
    assert isinstance(model, LookalikeClickModel)
    digest = HistoryDigest()
    treated = [f"t{i}" for i in range(100)]
    control = [f"h{i}" for i in range(50)]
    before: dict[str, int] = {}
    for i, cid in enumerate(treated + control):
        before[cid] = 2 + (i % 3 == 0)  # the same mix of buyers in both groups
        digest.add(_event(f"p1{cid}", cid, EventType.PURCHASE, START - timedelta(days=100), 20.0))
        digest.add(_event(f"p2{cid}", cid, EventType.PURCHASE, START - timedelta(days=60), 20.0))
        if before[cid] == 3:
            digest.add(_event(f"p3{cid}", cid, EventType.PURCHASE, START - timedelta(days=30), 20.0))
    for cid in treated:
        digest.add(_event(f"s{cid}", cid, EventType.SEND, START))
    after = {
        "t0": (EventType.PURCHASE, 30.0),  # bought in the window
        "t1": (EventType.REFUND, 20.0),  # only a refund: not a purchase
        "t2": (EventType.PURCHASE, 0.0),  # a free order still counts, as on the live path
        "h0": (EventType.PURCHASE, 15.0),
    }
    for cid, (kind, value) in after.items():
        digest.add(_event(f"a{cid}", cid, kind, START + timedelta(days=2), value))
    policy.digest = digest
    policy._history_holdout = dict.fromkeys(control, START)
    sends = [_send(c, "none" if i % 2 == 0 else "pct10") for i, c in enumerate(treated)]
    policy._warm_lookalike(START + timedelta(days=30), sends)

    rows = model.lookalike._rows
    keys = {r.customer_id: r.key for r in rows}
    assert len(rows) == 150  # 100 recipients, and the 50 shared controls once, not once per arm
    assert {keys[c] for c in control} == {None} and None not in {keys[c] for c in treated}
    by_id = {r.customer_id: r for r in rows}
    for cid in ("t0", "h0", "t5", "h5"):
        prof = by_id[cid].profile
        assert prof.as_of == START
        assert prof.base[COL["purchases"]] == pytest.approx(math.log1p(before[cid]))  # none after
        assert prof.base[COL["contacts_7d"]] == 0.0  # the campaign's own send is not in
    assert {c for c, r in by_id.items() if r.y == 1.0} == {"t0", "t2", "h0"}
    assert model.lookalike.refused == 0


# --- nightly features -----------------------------------------------------------------------------


def test_features_built_after_the_window_opened_are_refused() -> None:
    state = make_state(0, np.random.default_rng(0)).model_copy(
        update={"features": {"visits_30d": 3.0}, "features_as_of": T0 + timedelta(days=30)}
    )
    model = LookalikeResponseModel()
    assert profile_of(state).as_of == T0 + timedelta(days=30)
    assert model.observe([Outcome("c0", profile_of(state), KEY, True, T0, T0 + WINDOW)]) == 0
    assert model.refused == 1
    on_time = state.model_copy(update={"features_as_of": T0 - timedelta(hours=2)})
    assert profile_of(on_time).as_of == T0
    assert model.observe([Outcome("c0", profile_of(on_time), KEY, True, T0, T0 + WINDOW)]) == 1


def test_nightly_features_are_learned_and_read_as_average_when_missing() -> None:
    states, rows = outcomes(3000, seed=8)
    rng = np.random.default_rng(9)
    with_features = [
        Outcome(
            r.customer_id,
            profile_of(
                st.model_copy(
                    update={"features": {"visits_30d": float(rng.integers(0, 9))}, "features_as_of": T0}
                )
            ),
            r.action_key,
            r.converted,
            r.window_start,
            r.window_end,
        )
        for st, r in zip(states, rows, strict=True)
    ]
    model = LookalikeResponseModel()
    model.observe(with_features)
    assert model.fit(T0 + timedelta(days=8))
    fit = model._fit_state
    assert fit is not None and fit.raw_names == ("visits_30d",)
    plain = states[0]  # no features at all when predicting
    est = model.estimate(plain, KEY)
    assert est is not None and math.isfinite(est.mean) and est.sd > 0
    rich = plain.model_copy(update={"features": {"visits_30d": 8.0}, "features_as_of": T0})
    assert model.estimate(rich, KEY) is not None


def test_no_first_seen_date_reads_as_unknown_tenure_not_a_new_customer() -> None:
    state = make_state(0, np.random.default_rng(0)).model_copy(
        update={"first_seen_at": None, "purchases": 40}
    )
    prof = profile_of(state)
    assert math.isnan(prof.base[COL["tenure"]]) and math.isnan(prof.base[COL["frequency"]])
    model, _states = fitted()
    est = model.estimate(state, KEY)
    assert est is not None and math.isfinite(est.mean)


# --- what a row is worth --------------------------------------------------------------------------


def test_one_customer_decided_on_every_day_does_not_count_as_many_controls() -> None:
    states, rows = outcomes(400, seed=2, holdout=0.0)  # contacted only
    regular = profile_of(states[0])
    daily = [
        Outcome("regular", regular, None, False, T0, T0 + WINDOW + timedelta(days=i % 2)) for i in range(500)
    ]
    model = LookalikeResponseModel(config=LookalikeConfig(min_treated=100.0, min_control=20.0))
    model.observe(rows + daily)
    assert not model.fit(T0 + timedelta(days=9))  # 500 windows of one person are not 20 controls
    assert not model.ready


def test_contacted_rows_are_weighted_by_their_logged_propensity() -> None:
    model = LookalikeResponseModel(config=LookalikeConfig(max_customer_weight=1e9))
    prof = profile_of(make_state(0, np.random.default_rng(0)))
    rows = [
        Outcome("a", prof, KEY, True, T0, T0 + WINDOW, propensity=0.9),
        Outcome("b", prof, KEY, False, T0, T0 + WINDOW, propensity=0.1),
        Outcome("c", prof, None, False, T0, T0 + WINDOW),
    ]
    model.observe(rows)
    w = model._crowd_weights(model._rows)
    assert w[0] + w[1] == pytest.approx(2.0)  # self-normalised: the contacted rows keep their total
    assert w[1] == pytest.approx(9.0 * w[0])  # the unlikely send stands for nine likely ones
    assert w[2] == 1.0


def test_an_option_the_crowd_never_saw_is_neutral_and_uncertain() -> None:
    model, states = fitted()
    st = states[0]
    seen = model.estimate(st, KEY)
    unseen = model.estimate(st, "whatsapp|freeship|loyalty|video")
    other = model.estimate(st, "sms|pct10|savings|carousel")
    assert seen is not None and unseen is not None and other is not None
    assert unseen.option_sd > seen.option_sd > 0.0  # nothing is known about any of its levels
    assert unseen.sd > seen.sd
    # the shared effect of contact carries over: neutral, not a level's worth of logit lost
    assert abs(unseen.mean - seen.mean) < 3 * unseen.sd


# --- the book --------------------------------------------------------------------------------------


def test_the_book_weights_overlapping_sends_and_passes_their_propensity() -> None:
    book = OutcomeBook(WINDOW)
    events: list[Event] = []
    for i, day in enumerate((0, 1, 10)):
        at = T0 + timedelta(days=day)
        did = f"d{i}"
        book.remember(
            Decision(
                decision_id=did,
                customer_id="c",
                made_at=at,
                kind=DecisionKind.ACT,
                action=Action(goal_id="g", channel=Channel.EMAIL, send_at=at, content_id="x"),
                group=Group.AGENT,
                probability=0.25,
                reason="test",
                policy_version="t",
            ),
            profile_of(make_state(0, np.random.default_rng(0)).model_copy(update={"as_of": at})),
            KEY,
        )
        for kind in (EventType.SEND, EventType.DELIVER):
            events.append(
                Event(
                    event_id=f"{kind.value}{i}",
                    customer_id="c",
                    type=kind,
                    ts=at,
                    source="t",
                    channel=Channel.EMAIL,
                    decision_id=did,
                )
            )
    got = book.close(T0 + timedelta(days=30), lambda _cid: Timeline(events))
    assert [round(o.weight, 4) for o in got] == [1.0, round(1 / 7, 4), 1.0]
    assert {o.propensity for o in got} == {0.25}


# --- the switch ------------------------------------------------------------------------------------


def test_a_falsy_switch_is_off_and_a_config_with_a_bundle_is_refused() -> None:
    for off in (None, 0, False):
        policy = build_agent_policy([default_brief()], default_content(), lookalike=off)  # type: ignore[arg-type]
        assert type(policy.bundle.response) is ClickResponseModel
    fresh = build_bundle()
    bundle = ModelBundle(response=LookalikeClickModel(), value=fresh.value, fatigue=fresh.fatigue)
    with pytest.raises(ValueError, match="LookalikeConfig"):
        build_agent_policy([default_brief()], default_content(), bundle=bundle, lookalike=LookalikeConfig())
