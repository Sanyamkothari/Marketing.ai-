"""The Gym world and its planted truth: repeatable by seed, honest about value, strict about rules."""

from __future__ import annotations

import time
from datetime import timedelta

import numpy as np

from autonomy import interfaces as ifc
from autonomy.contracts import (
    Action,
    Channel,
    ContentItem,
    ContentStatus,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Theme,
    stable_id,
)
from autonomy.gym.api import PERSONAS, GroundTruth, GymEnvironment, ScenarioSpec
from autonomy.gym.truth import DEFAULT_START, PlantedTruth
from autonomy.gym.world import GymWorld

START = DEFAULT_START
NOON = START + timedelta(hours=12)


def _spec(**kw: object) -> ScenarioSpec:
    base: dict[str, object] = {"name": "test", "n_customers": 300, "history_days": 30, "days": 10}
    base.update(kw)
    return ScenarioSpec.model_validate(base)


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


def _email(at: object = NOON, offer: str = "none", content: str | None = None) -> Action:
    return Action(goal_id="g", channel=Channel.EMAIL, offer_id=offer, content_id=content, send_at=at)


def _run(world: GymWorld, seed: int, days: int, every: int = 3) -> list[Event]:
    """Reset, then each day contact every `every`-th customer by email at noon."""
    events = list(world.reset(seed))
    ids = world.customer_ids
    for day in range(days):
        now = START + timedelta(days=day, hours=10)
        events.extend(world.advance(now))
        at = START + timedelta(days=day, hours=12)
        world.act([_decision(ids[i], _email(at), day) for i in range(day % every, len(ids), every)])
    events.extend(world.advance(START + timedelta(days=days)))
    return events


def _margin(events: list[Event]) -> float:
    return sum(e.margin or 0.0 for e in events if e.type is EventType.PURCHASE)


# --- shape and protocols -------------------------------------------------------------------------


def test_world_satisfies_the_protocols() -> None:
    world = GymWorld(_spec())
    assert isinstance(world, ifc.Environment)
    assert isinstance(world, GymEnvironment)
    assert isinstance(world.truth, GroundTruth)


def test_history_has_signups_consents_purchases_and_visits_before_day_zero() -> None:
    world = GymWorld(_spec(n_customers=500, history_days=90))
    history = world.reset(1)
    kinds = {e.type for e in history}
    assert {EventType.SIGNUP, EventType.CONSENT_GRANTED, EventType.PURCHASE, EventType.VISIT} <= kinds
    assert all(e.ts < START for e in history)
    assert [e.ts for e in history] == sorted(e.ts for e in history)
    assert len({e.event_id for e in history}) == len(history)
    signups = {e.customer_id for e in history if e.type is EventType.SIGNUP}
    assert signups == set(world.customer_ids)
    first_seen = {}
    for e in history:
        first_seen.setdefault(e.customer_id, e)
    assert all(e.type is EventType.SIGNUP for e in first_seen.values())
    assert world.now() == START


# --- determinism ---------------------------------------------------------------------------------


def test_same_seed_same_world_and_different_seed_different_world() -> None:
    a = _run(GymWorld(_spec()), seed=7, days=5)
    b = _run(GymWorld(_spec()), seed=7, days=5)
    c = _run(GymWorld(_spec()), seed=8, days=5)
    assert [e.model_dump() for e in a] == [e.model_dump() for e in b]
    assert [e.event_id for e in a] != [e.event_id for e in c]


def test_reset_replants_the_same_truth_object() -> None:
    """The scoreboard hands `world.truth` to the oracle before it calls reset(seed); the oracle must
    then read the reset world's customers and contacts, not a stale copy."""
    world = GymWorld(_spec(seed=0, sleeping_dog_share=0.3))
    held = world.truth
    world.reset(9)
    assert world.truth is held and world.planted.seed == 9
    fresh = PlantedTruth(_spec(sleeping_dog_share=0.3), seed=9)
    assert np.array_equal(world.planted.sleeping_dog, fresh.sleeping_dog)
    assert np.array_equal(world.planted.daily_rate, fresh.daily_rate)
    cid = world.customer_ids[0]
    before = held.expected_net(cid, _email(NOON), NOON)
    world.act([_decision(cid, _email())])
    world.reset(9)
    assert held.expected_net(cid, _email(NOON), NOON) == before  # contacts forgotten on reset


def test_reset_starts_over() -> None:
    world = GymWorld(_spec())
    first = _run(world, seed=3, days=4)
    again = _run(world, seed=3, days=4)
    assert [e.event_id for e in first] == [e.event_id for e in again]
    assert len(world.contacts) == len({e.decision_id for e in again if e.type is EventType.SEND})


def test_organic_purchases_do_not_depend_on_the_policy() -> None:
    """Common random numbers: with no contact effects, contacting changes nothing about purchases."""
    spec = _spec(channel_uplift={p: {Channel.EMAIL: 0.0} for p in PERSONAS})
    quiet = GymWorld(spec)
    quiet.reset(5)
    organic = list(quiet.advance(START + timedelta(days=5)))
    busy = _run(GymWorld(spec), seed=5, days=5, every=1)
    bought = [e.event_id for e in busy if e.type is EventType.PURCHASE and e.ts >= START]
    assert bought == [e.event_id for e in organic if e.type is EventType.PURCHASE]


# --- the truth ------------------------------------------------------------------------------------


def test_sleeping_dogs_have_negative_value_and_no_best_action() -> None:
    truth = PlantedTruth(_spec(sleeping_dog_share=0.3), seed=1)
    dogs = [c for c in truth.customer_ids if truth.is_sleeping_dog(c)]
    assert dogs
    options = [_email(), _email(offer="pct10"), _email(offer="freeship")]
    for cid in dogs:
        for a in options:
            assert truth.expected_net(cid, a, NOON) < 0
        assert truth.best_action(cid, options, NOON) is None


def test_discounts_to_sure_things_are_wasted() -> None:
    """Loyal customers buy anyway; with cannibalisation the discount costs more than it brings."""
    spec = _spec(persona_mix={"loyal": 1.0}, channel_consent={Channel.EMAIL: 1.0})
    truth = PlantedTruth(spec, seed=2)
    plain = truth.expected_net_all(_email(), NOON)
    discount = truth.expected_net_all(_email(offer="pct10"), NOON)
    assert np.mean(discount) < 0 < np.mean(plain)
    assert np.mean(discount < plain) > 0.95

    fair = PlantedTruth(spec.model_copy(update={"discount_redeemed_by_sure_things": False}), seed=2)
    assert np.mean(fair.expected_net_all(_email(offer="pct10"), NOON)) > np.mean(discount)


def test_offer_and_theme_defaults_follow_the_personas() -> None:
    edu = ContentItem(
        content_id="edu",
        channel=Channel.EMAIL,
        theme=Theme.EDUCATION,
        body="Three tips",
        status=ContentStatus.APPROVED,
        created_at=START,
    )
    spec = _spec(n_customers=2000, channel_consent={Channel.EMAIL: 1.0})
    truth = PlantedTruth(spec, seed=4, content=[edu])
    ps = truth.persona_idx == truth.personas.index("price_sensitive")
    rs = truth.persona_idx == truth.personas.index("researcher")
    alive = ~truth.sleeping_dog
    plain = truth.expected_net_all(_email(), NOON)
    discount = truth.expected_net_all(_email(offer="pct10"), NOON)
    education = truth.expected_net_all(_email(content="edu"), NOON)
    assert np.mean((discount - plain)[ps & alive]) > 0
    assert np.mean((education - plain)[rs & alive]) > 0


def test_no_consent_means_no_value_and_best_action_is_the_max() -> None:
    spec = _spec(channel_consent={Channel.EMAIL: 1.0, Channel.WHATSAPP: 0.0})
    truth = PlantedTruth(spec, seed=6)
    wa = Action(goal_id="g", channel=Channel.WHATSAPP, send_at=NOON)
    assert np.all(truth.expected_net_all(wa, NOON) == -spec.channel_cost[Channel.WHATSAPP])
    cid = next(c for c in truth.customer_ids if not truth.is_sleeping_dog(c))
    options = [wa, _email(), _email(offer="pct10")]
    best = truth.best_action(cid, options, NOON)
    values = [truth.expected_net(cid, a, NOON) for a in options]
    assert best is options[int(np.argmax(values))]


def test_fatigue_shrinks_the_value_of_the_next_contact() -> None:
    world = GymWorld(_spec(sleeping_dog_share=0.0, channel_consent={Channel.EMAIL: 1.0}))
    world.reset(1)
    cid = world.customer_ids[0]
    later = NOON + timedelta(days=1)
    before = world.truth.expected_net(cid, _email(later), later)
    world.act([_decision(cid, _email())])
    assert world.truth.expected_net(cid, _email(later), later) < before


def test_expected_net_matches_what_the_world_simulates() -> None:
    """Same seed, contact everyone vs no one: the realised margin gap is the truth's sum."""
    spec = _spec(
        n_customers=3000,
        history_days=0,
        sleeping_dog_share=0.0,
        channel_consent={Channel.EMAIL: 1.0},
        channel_uplift={p: {Channel.EMAIL: 0.02} for p in PERSONAS},
    )
    control = GymWorld(spec)
    control.reset(11)
    base = _margin(list(control.advance(START + timedelta(days=9))))
    treated = GymWorld(spec)
    treated.reset(11)
    treated.act([_decision(c, _email()) for c in treated.customer_ids])
    lifted = _margin(list(treated.advance(START + timedelta(days=9))))
    predicted = sum(c.expected_net for c in treated.contacts) + len(treated.contacts) * 0.001
    assert predicted > 0
    assert abs((lifted - base) - predicted) < 0.15 * predicted


# --- contacts and rules --------------------------------------------------------------------------


def test_violations_are_recorded_and_caps_are_counted_not_violations() -> None:
    spec = _spec(channel_consent={Channel.EMAIL: 1.0, Channel.WHATSAPP: 0.0}, unsubscribe_base=0.0)
    world = GymWorld(spec)
    world.reset(1)
    a, b, c = world.customer_ids[:3]
    world.act([_decision(a, _email())])  # fine
    assert world.violations() == []
    world.act([_decision(a, Action(goal_id="g", channel=Channel.WHATSAPP, send_at=NOON))])
    world.act([_decision(b, _email(START + timedelta(hours=23)))])  # 23:00 UTC, quiet hours
    world.planted.note_unsubscribe(world.planted.index(c), Channel.EMAIL, START)
    world.act([_decision(c, _email())])
    found = world.violations()
    assert len(found) == 3
    assert any("without consent" in v and a in v for v in found)
    assert any("quiet hours" in v and b in v for v in found)
    assert any("unsubscribed" in v and c in v for v in found)

    for k in range(3):
        world.act([_decision(a, _email(NOON + timedelta(hours=1 + k)), tag=k)])
    assert len(world.violations()) == 3
    assert world.over_cap_contacts() >= 2
    assert max(r.contacts_7d for r in world.contacts if r.customer_id == a) == 5

    events = world.advance(START + timedelta(days=1))
    fails = [e for e in events if e.type is EventType.FAIL]
    assert {e.customer_id for e in fails} == {a, c}
    assert all(e.decision_id for e in events if e.type in (EventType.SEND, EventType.DELIVER))


def test_two_contacts_at_the_same_moment_count_as_two() -> None:
    world = GymWorld(_spec(channel_consent={Channel.EMAIL: 1.0}, sleeping_dog_share=0.0))
    world.reset(1)
    cid = world.customer_ids[0]
    world.act([_decision(cid, _email(), tag=0), _decision(cid, _email(), tag=1)])
    first, second = world.contacts
    assert (first.contacts_7d, second.contacts_7d) == (1, 2)
    assert second.expected_net < first.expected_net  # the second is fatigued by the first


def test_quiet_hours_use_local_time() -> None:
    spec = _spec(timezones={"Asia/Kolkata": 1.0}, channel_consent={Channel.EMAIL: 1.0})
    world = GymWorld(spec)
    world.reset(1)
    cid = world.customer_ids[0]
    world.act([_decision(cid, _email(START + timedelta(hours=6)))])  # 11:30 in India
    world.act([_decision(cid, _email(START + timedelta(hours=17)), tag=1)])  # 22:30 in India
    assert len(world.violations()) == 1 and "22:30 local" in world.violations()[0]


def test_sleeping_dogs_buy_less_after_a_contact() -> None:
    spec = _spec(
        n_customers=2000,
        history_days=0,
        sleeping_dog_share=1.0,
        channel_consent={Channel.EMAIL: 1.0},
        daily_purchase_rate=dict.fromkeys(PERSONAS, 0.05),
    )
    control = GymWorld(spec)
    control.reset(2)
    base = [e for e in control.advance(START + timedelta(days=7)) if e.type is EventType.PURCHASE]
    treated = GymWorld(spec)
    treated.reset(2)
    treated.act([_decision(c, _email(START)) for c in treated.customer_ids])
    after = [e for e in treated.advance(START + timedelta(days=7)) if e.type is EventType.PURCHASE]
    assert len(after) < 0.7 * len(base)
    assert treated.sleeping_dogs_contacted_share() == 1.0


def test_a_second_contact_to_a_dented_dog_only_costs_the_extra_hours() -> None:
    world = GymWorld(_spec(sleeping_dog_share=1.0, channel_consent={Channel.EMAIL: 1.0}))
    world.reset(1)
    cid = world.customer_ids[0]
    world.act([_decision(cid, _email(START))])
    world.advance(START + timedelta(days=1))
    second = _email(START + timedelta(days=1))
    harm = world.truth.expected_net(cid, second, second.send_at)
    fresh = PlantedTruth(_spec(sleeping_dog_share=1.0, channel_consent={Channel.EMAIL: 1.0}), seed=1)
    full = fresh.expected_net(cid, second, second.send_at)
    cost = -_spec().channel_cost[Channel.EMAIL]
    assert full < harm < cost + 1e-12
    # One extra day of a seven-day dent.
    assert abs((harm - cost) - (full - cost) / 7) < 1e-9


def test_advancing_to_a_time_inside_an_hour_never_returns_late_events() -> None:
    spec = _spec(history_days=0, daily_purchase_rate=dict.fromkeys(PERSONAS, 0.2))
    aligned = GymWorld(spec)
    aligned.reset(4)
    whole = [e.event_id for e in aligned.advance(START + timedelta(days=2))]
    odd = GymWorld(spec)
    odd.reset(4)
    seen: list[Event] = []
    until = START
    while until < START + timedelta(days=2):
        prev, until = until, min(until + timedelta(minutes=97), START + timedelta(days=2))
        batch = list(odd.advance(until))
        assert all(prev <= e.ts < until for e in batch)
        seen.extend(batch)
    assert [e.event_id for e in seen] == whole


def test_discount_is_redeemed_by_buyers_in_the_window() -> None:
    spec = _spec(
        n_customers=1000,
        history_days=0,
        channel_consent={Channel.EMAIL: 1.0},
        daily_purchase_rate=dict.fromkeys(PERSONAS, 0.05),
    )
    world = GymWorld(spec)
    world.reset(3)
    ids = world.customer_ids
    world.act([_decision(c, _email(START, offer="pct10")) for c in ids[:500]])
    events = world.advance(START + timedelta(days=spec.effect_days + 3))
    buys = [e for e in events if e.type is EventType.PURCHASE]
    in_window = [e for e in buys if e.customer_id in set(ids[:500]) and e.ts < START + timedelta(days=7)]
    assert in_window and all(e.attrs.get("offer_id") == "pct10" for e in in_window)
    assert all("offer_id" not in e.attrs for e in buys if e.customer_id in set(ids[500:]))
    assert all("offer_id" not in e.attrs for e in buys if e.ts >= START + timedelta(days=7, hours=1))


def test_drift_changes_purchase_rates_from_its_day() -> None:
    spec = _spec(history_days=0, drift_day=3, drift_purchase_multiplier=0.0)
    world = GymWorld(spec)
    world.reset(1)
    buys = [e for e in world.advance(START + timedelta(days=8)) if e.type is EventType.PURCHASE]
    assert buys and all(e.ts < START + timedelta(days=3) for e in buys)


def test_two_thousand_customers_for_thirty_days_is_fast() -> None:
    world = GymWorld(_spec(n_customers=2000, history_days=180, days=30))
    t0 = time.perf_counter()
    events = _run(world, seed=1, days=30, every=4)
    elapsed = time.perf_counter() - t0
    assert elapsed < 10, f"took {elapsed:.1f}s"
    assert any(e.type is EventType.PURCHASE and e.ts >= START for e in events)
    assert len(world.contacts) > 10_000
