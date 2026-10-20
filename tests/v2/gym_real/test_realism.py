"""The Gym's realism (wave 3a): unsubscribes cost money, sleeping dogs leave clues, the history holds
past marketing with logged probabilities, and none of it costs determinism or speed."""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from autonomy.contracts import (
    Action,
    Channel,
    Decision,
    DecisionKind,
    Event,
    EventType,
    stable_id,
)
from autonomy.gym import GYM_OFFERS, SCENARIOS, ScenarioSpec, default_content, get_scenario
from autonomy.gym.truth import DEFAULT_START, PlantedTruth
from autonomy.gym.world import (
    HISTORY_LAPSED_FACTOR,
    HISTORY_MAX_INCLUSION,
    HISTORY_NO_OFFER_SHARE,
    HISTORY_RECENT_DAYS,
    HISTORY_RECENT_FACTOR,
    HISTORY_WHATSAPP_SHARE,
    LOGGING_POLICY,
    GymWorld,
)

START = DEFAULT_START
NOON = START + timedelta(hours=12)


def _spec(**kw: object) -> ScenarioSpec:
    base: dict[str, object] = {"name": "test", "n_customers": 300, "history_days": 30, "days": 10}
    base.update(kw)
    return ScenarioSpec.model_validate(base)


def _email(at: object = NOON, offer: str = "none") -> Action:
    return Action(goal_id="g", channel=Channel.EMAIL, offer_id=offer, send_at=at)


def _decision(cid: str, action: Action, tag: object = 0) -> Decision:
    return Decision(
        decision_id=stable_id("dec", cid, action.send_at.isoformat(), action.channel, tag),
        customer_id=cid,
        made_at=action.send_at,
        kind=DecisionKind.ACT,
        action=action,
        probability=1.0,
        reason="test",
        policy_version="test",
    )


def _world(spec: ScenarioSpec) -> GymWorld:
    return GymWorld(spec, offers=GYM_OFFERS, content=default_content())


def _run(world: GymWorld, seed: int, days: int, every: int = 3) -> list[Event]:
    """Reset, then each day contact every `every`-th customer by email at noon."""
    events = list(world.reset(seed))
    ids = world.customer_ids
    for day in range(days):
        events.extend(world.advance(START + timedelta(days=day, hours=10)))
        at = START + timedelta(days=day, hours=12)
        world.act([_decision(ids[i], _email(at), day) for i in range(day % every, len(ids), every)])
    events.extend(world.advance(START + timedelta(days=days)))
    return events


def _auc(score: np.ndarray, label: np.ndarray) -> float:
    """Area under the ROC curve (ties count half): the chance a random dog outscores a random non-dog."""
    ranks = pd.Series(score).rank().to_numpy()
    pos, neg = int(label.sum()), int((~label).sum())
    return float((ranks[label].sum() - pos * (pos + 1) / 2) / (pos * neg))


# --- unsubscribes and complaints cost money -------------------------------------------------------


def test_lost_contact_value_is_charged_in_expected_net() -> None:
    free = _spec(lost_contact_value=0.0, complaint_cost=0.0, sleeping_dog_share=0.2)
    priced = _spec(complaint_cost=5.0, sleeping_dog_share=0.2)
    t_free, t_priced = PlantedTruth(free, seed=3), PlantedTruth(priced, seed=3)
    margin = priced.aov_mean * priced.margin_rate
    sends = priced.lost_contact_sends_per_week * priced.lost_contact_horizon_days / 7.0
    p_unsub = priced.unsubscribe_base  # no contacts yet this week
    share = priced.complaint_share_of_unsubscribes
    checked = {"dog": 0, "customer": 0}
    for cid in t_priced.customer_ids:
        i = t_priced.index(cid)
        if not t_priced.consent[i, 0]:
            continue
        gap = t_free.expected_net(cid, _email(), NOON) - t_priced.expected_net(cid, _email(), NOON)
        lost = t_priced.lost_contact_value(cid, Channel.EMAIL)
        if t_priced.is_sleeping_dog(cid):
            assert lost == 0.0  # messaging a dog never made money, so losing them costs nothing
            p_dog = p_unsub * priced.sleeping_dog_unsubscribe_multiplier
            assert gap == pytest.approx(p_dog * share * priced.complaint_cost)
            checked["dog"] += 1
        else:
            uplift = float(t_priced.uplift[i, 0])
            per_send = uplift * priced.effect_days * margin - priced.channel_cost[Channel.EMAIL]
            assert lost == pytest.approx(max(per_send, 0.0) * sends)
            assert gap == pytest.approx(p_unsub * (lost + share * priced.complaint_cost))
            checked["customer"] += 1
    assert checked["dog"] > 10 and checked["customer"] > 100

    fixed = PlantedTruth(_spec(lost_contact_value=25.0), seed=3)
    assert np.all(fixed.lost_value == 25.0)


def test_events_realise_the_lost_value_the_truth_charges() -> None:
    """Every UNSUBSCRIBE carries the customer's lost contact value and every COMPLAINT the
    deliverability cost; summed, they match what `expected_net` charged for the same contacts."""
    common: dict[str, object] = {
        "n_customers": 3000,
        "history_days": 0,
        "channel_consent": {Channel.EMAIL: 1.0},
        "unsubscribe_base": 0.15,
        "complaint_share_of_unsubscribes": 0.4,
        "sleeping_dog_share": 0.15,
    }
    spec = _spec(**common, complaint_cost=5.0)
    free = PlantedTruth(_spec(**common, lost_contact_value=0.0, complaint_cost=0.0), seed=4)
    world = _world(spec)
    world.reset(4)
    charged = sum(
        free.expected_net(c, _email(), NOON) - world.truth.expected_net(c, _email(), NOON)
        for c in world.customer_ids
    )
    world.act([_decision(c, _email()) for c in world.customer_ids])
    events = list(world.advance(START + timedelta(days=2)))
    unsubs = [e for e in events if e.type is EventType.UNSUBSCRIBE]
    complaints = [e for e in events if e.type is EventType.COMPLAINT]
    assert len(unsubs) > 300 and len(complaints) > 100
    for e in unsubs:
        assert e.attrs["lost_value"] == pytest.approx(
            world.planted.lost_contact_value(e.customer_id, Channel.EMAIL), abs=1e-4
        )
    assert all(e.attrs["lost_value"] == spec.complaint_cost for e in complaints)
    realised = sum(float(e.attrs["lost_value"]) for e in unsubs + complaints)
    assert realised == pytest.approx(charged, rel=0.15)
    # Annoyed dogs unsubscribe more often than everyone else.
    dogs = {c for c in world.customer_ids if world.truth.is_sleeping_dog(c)}
    dog_rate = sum(e.customer_id in dogs for e in unsubs) / len(dogs)
    other_rate = sum(e.customer_id not in dogs for e in unsubs) / (len(world.customer_ids) - len(dogs))
    assert dog_rate > 2 * other_rate


# --- sleeping dogs leave faint clues --------------------------------------------------------------


def _clue_auc(signal: float, seeds: Sequence[int] = (0, 1)) -> float:
    """AUC of a simple observable score (visits, few clicks on past messages, a past unsubscribe)
    for telling planted sleeping dogs from everyone else, pooled over seeds."""
    scores: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    for seed in seeds:
        world = _world(get_scenario("sleeping_dogs_15", n_customers=1000, sleeping_dog_signal=signal))
        history = world.reset(seed)
        where = {c: i for i, c in enumerate(world.customer_ids)}
        n = len(where)
        visits, clicks, unsubs = np.zeros(n), np.zeros(n), np.zeros(n)
        for e in history:
            if e.type is EventType.VISIT:
                visits[where[e.customer_id]] += 1
            elif e.type is EventType.CLICK:
                clicks[where[e.customer_id]] += 1
            elif e.type in (EventType.UNSUBSCRIBE, EventType.COMPLAINT):
                unsubs[where[e.customer_id]] += 1
        scores.append(visits - 3.0 * np.minimum(clicks, 3) + 3.0 * (unsubs > 0))
        labels.append(world.planted.sleeping_dog.copy())
    # One AUC per seed, then their mean: each compares dogs with non-dogs of the same world.
    per_seed = [_auc(s, lab) for s, lab in zip(scores, labels, strict=True)]
    return float(np.mean(per_seed))


def test_sleeping_dog_clues_rise_with_the_signal() -> None:
    hidden, default, strong = _clue_auc(0.0), _clue_auc(0.5), _clue_auc(1.0)
    assert abs(hidden - 0.5) < 0.07, hidden
    assert default > 0.65, default
    assert strong > default


def test_the_history_never_names_a_sleeping_dog() -> None:
    world = _world(get_scenario("sleeping_dogs_15", n_customers=300, sleeping_dog_signal=1.0))
    events = _run(world, seed=2, days=3)
    for e in events:
        for key, value in e.attrs.items():
            text = f"{key}={value}".lower()
            assert "dog" not in text and "persona" not in text and "annoy" not in text


def test_the_history_carries_no_scoring_attrs() -> None:
    """`lost_value` comes from the planted truth (0 for every sleeping dog), so a past unsubscribe
    carrying it would name the dog even in `sleeping_dogs_hidden`. Only `act` events carry it."""
    world = _world(get_scenario("sleeping_dogs_hidden", n_customers=1000))
    history = world.reset(0)
    gone = [e for e in history if e.type in (EventType.UNSUBSCRIBE, EventType.COMPLAINT)]
    assert gone
    assert all("lost_value" not in e.attrs for e in gone)


def test_a_bare_spec_charges_dogs_only_their_dent() -> None:
    """With no complaint cost (the bare default) a dog's lost contact value is zero, so the old
    'a second contact only costs the extra hours of the dent' identity still holds exactly."""
    spec = _spec(sleeping_dog_share=1.0, channel_consent={Channel.EMAIL: 1.0})
    world = GymWorld(spec)
    world.reset(1)
    cid = world.customer_ids[0]
    world.act([_decision(cid, _email(START))])
    world.advance(START + timedelta(days=1))
    second = _email(START + timedelta(days=1))
    harm = world.truth.expected_net(cid, second, second.send_at)
    full = PlantedTruth(spec, seed=1).expected_net(cid, second, second.send_at)
    cost = -spec.channel_cost[Channel.EMAIL]
    assert abs((harm - cost) - (full - cost) / 7) < 1e-9


def test_scenarios_keep_the_hard_case_visible() -> None:
    assert SCENARIOS["sleeping_dogs_15"].sleeping_dog_signal == 0.5
    hidden = SCENARIOS["sleeping_dogs_hidden"]
    assert hidden.sleeping_dog_signal == 0.0 and hidden.sleeping_dog_share == 0.15
    assert all(s.history_sends_per_week > 0 and s.complaint_cost > 0 for s in SCENARIOS.values())


# --- the history holds past marketing --------------------------------------------------------------


def test_history_contains_logged_past_sends_that_follow_the_rules() -> None:
    world = _world(get_scenario("baseline_shop", n_customers=500))
    history = world.reset(1)
    sends = [e for e in history if e.type is EventType.SEND]
    assert len(sends) > 500 * 3  # a few campaigns per customer over 180 days
    assert all(e.ts < START for e in history)
    by_decision: dict[str, set[EventType]] = {}
    for e in history:
        if e.decision_id is not None:
            by_decision.setdefault(e.decision_id, set()).add(e.type)
    for e in sends:
        p = e.attrs["logged_probability"]
        assert isinstance(p, float) and 0 < p <= HISTORY_MAX_INCLUSION
        assert e.attrs["logging_policy"] == LOGGING_POLICY
        assert e.attrs["offer_id"] in {o.offer_id for o in GYM_OFFERS}
        assert e.decision_id is not None and EventType.DELIVER in by_decision[e.decision_id]
        local = e.ts.astimezone(world.planted.local_time(world.planted.index(e.customer_id), e.ts).tzinfo)
        assert local.hour == 10 and local.weekday() == 1  # Tuesdays at 10:00 local
    kinds = {e.type for e in history}
    assert {EventType.CLICK, EventType.UNSUBSCRIBE} <= kinds
    assert any("offer_id" in e.attrs for e in history if e.type is EventType.PURCHASE)

    # Sends only go to people who consented on that channel and had not unsubscribed from it.
    consented = {(e.customer_id, e.channel) for e in history if e.type is EventType.CONSENT_GRANTED}
    gone: dict[tuple[str, Channel | None], object] = {}
    for e in history:
        key = (e.customer_id, e.channel)
        if e.type is EventType.SEND:
            assert key in consented and key not in gone
        elif e.type is EventType.UNSUBSCRIBE:
            gone[key] = e.ts


def test_logged_probabilities_can_be_rebuilt_from_the_history() -> None:
    """The logging policy only uses what the history shows before each campaign day (recent
    purchases, open channels), so an analyst can recompute every logged probability: the condition
    for valid off-policy estimates. A policy that peeked at the same day's later purchases would not."""
    world = _world(get_scenario("baseline_shop", n_customers=400))
    history = world.reset(2)
    day = timedelta(days=1)
    bought: dict[str, list[int]] = {}
    consent: set[tuple[str, Channel | None]] = set()
    gone: set[tuple[str, Channel | None]] = set()
    offers = len({o.offer_id for o in GYM_OFFERS} | {"none"}) - 1
    checked = 0
    for e in history:  # time order, so `bought` and `gone` hold only what came before the send
        key = (e.customer_id, e.channel)
        if e.type is EventType.PURCHASE:
            bought.setdefault(e.customer_id, []).append((e.ts - START) // day)
        elif e.type is EventType.CONSENT_GRANTED:
            consent.add(key)
        elif e.type is EventType.UNSUBSCRIBE:
            gone.add(key)
        elif e.type is EventType.SEND:
            local_date = world.planted.local_time(world.planted.index(e.customer_id), e.ts).date()
            campaign = (local_date - START.date()).days
            recent = any(
                campaign - HISTORY_RECENT_DAYS <= d < campaign for d in bought.get(e.customer_id, [])
            )
            factor = HISTORY_RECENT_FACTOR if recent else HISTORY_LAPSED_FACTOR
            include = min(world.spec.history_sends_per_week * factor, HISTORY_MAX_INCLUSION)
            open_ = [(e.customer_id, c) in consent and (e.customer_id, c) not in gone for c in Channel]
            both = open_[list(Channel).index(Channel.EMAIL)] and open_[list(Channel).index(Channel.WHATSAPP)]
            share = HISTORY_WHATSAPP_SHARE if e.channel is Channel.WHATSAPP else 1 - HISTORY_WHATSAPP_SHARE
            channel = share if both else 1.0
            offer = (
                HISTORY_NO_OFFER_SHARE
                if e.attrs["offer_id"] == "none"
                else (1 - HISTORY_NO_OFFER_SHARE) / offers
            )
            assert e.attrs["logged_probability"] == pytest.approx(include * channel * offer, abs=1e-6)
            checked += 1
    assert checked > 1000


def test_past_unsubscribes_stay_in_force_and_past_sends_still_count() -> None:
    world = _world(get_scenario("baseline_shop", n_customers=600))
    history = world.reset(3)
    gone = [e for e in history if e.type is EventType.UNSUBSCRIBE and e.channel is Channel.EMAIL]
    assert gone
    cid = gone[0].customer_id
    assert world.truth.expected_net(cid, _email(), NOON) == -world.spec.channel_cost[Channel.EMAIL]
    world.act([_decision(cid, _email())])
    assert any("after they unsubscribed" in v for v in world.violations())

    # A send in the last week of the history counts towards this week's contacts (fatigue).
    last_week = [e for e in history if e.type is EventType.SEND and e.ts > START - timedelta(days=7)]
    assert last_week
    i = world.planted.index(last_week[0].customer_id)
    prior = world.planted.contacts_7d(np.array([i]), np.array([world.planted.seconds(NOON)]))
    assert int(prior[0]) >= 1


def test_past_sends_cause_purchases_on_common_random_numbers() -> None:
    """With no dogs, turning past marketing on only adds purchases: the organic draws are shared."""
    off = _world(get_scenario("baseline_shop", n_customers=500, history_sends_per_week=0.0))
    on = _world(get_scenario("baseline_shop", n_customers=500))
    bought_off = {e.event_id for e in off.reset(5) if e.type is EventType.PURCHASE}
    bought_on = {e.event_id for e in on.reset(5) if e.type is EventType.PURCHASE}
    assert bought_off < bought_on


def test_a_bare_spec_keeps_the_old_history() -> None:
    """Raw specs (and the tests written against them) see no past marketing unless they ask."""
    world = GymWorld(_spec())
    first = _run(world, seed=3, days=4)
    assert not any(e.type is EventType.SEND and e.ts < START for e in first)
    assert len(world.contacts) == len({e.decision_id for e in first if e.type is EventType.SEND})


# --- determinism and speed ------------------------------------------------------------------------


def test_same_seed_same_realistic_world() -> None:
    spec = get_scenario("sleeping_dogs_15", n_customers=300, history_days=60)
    world = _world(spec)
    a = _run(world, seed=7, days=4)
    b = _run(_world(spec), seed=7, days=4)
    again = _run(world, seed=7, days=4)  # a reset forgets the past campaigns' unsubscribes and contacts
    c = _run(_world(spec), seed=8, days=4)
    assert [e.model_dump() for e in a] == [e.model_dump() for e in b] == [e.model_dump() for e in again]
    assert [e.event_id for e in a] != [e.event_id for e in c]


def _timed(spec: ScenarioSpec, days: int) -> tuple[float, float]:
    """Seconds for reset (the history) and for `days` decision days of contacts and purchases."""
    world = GymWorld(spec)
    ids = world.customer_ids
    batches = [
        [_decision(ids[i], _email(START + timedelta(days=d, hours=12)), d) for i in range(d % 4, len(ids), 4)]
        for d in range(days)
    ]
    t0 = time.perf_counter()
    world.reset(1)
    t1 = time.perf_counter()
    for d in range(days):
        world.advance(START + timedelta(days=d, hours=10))
        world.act(batches[d])
    world.advance(START + timedelta(days=days))
    return t1 - t0, time.perf_counter() - t1


def test_realism_leaves_the_simulation_speed_unchanged() -> None:
    plain = get_scenario(
        "sleeping_dogs_15", n_customers=1000, history_sends_per_week=0.0, sleeping_dog_signal=0.0
    )
    real = get_scenario("sleeping_dogs_15", n_customers=1000)
    runs: dict[str, list[tuple[float, float]]] = {"plain": [], "real": []}
    for _ in range(3):  # interleaved, best of three: timing on a shared machine is noisy
        runs["plain"].append(_timed(plain, 20))
        runs["real"].append(_timed(real, 20))
    forward_plain = min(r[1] for r in runs["plain"])
    forward_real = min(r[1] for r in runs["real"])
    history_real = min(r[0] for r in runs["real"])
    # A loose bound: shared CI runners and busy laptops jitter by tens of percent; this catches a real
    # slowdown (realism doubling the cost of a simulated day), not scheduler noise.
    assert forward_real < 2.0 * forward_plain + 0.2, (forward_real, forward_plain)
    assert history_real < 2.0, history_real  # 1,000 customers, 180 days of history with campaigns
