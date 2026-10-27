"""The hidden truth: its shape, its averages, and that it is not the agent's model."""

from __future__ import annotations

import numpy as np

from autonomy.contracts import Action, Channel
from autonomy.gym.waves.config import CHANNEL_COST, EFFECT_DAYS, WORLDS, get_world
from autonomy.gym.waves.data import FEATURE_COLUMNS
from autonomy.gym.waves.truth import WavesTruth, plant_effect, rank_features
from tests.v4.waves.helpers import make_world

SEEDS = range(5)


def test_the_average_lift_is_the_worlds_whatever_the_seed(data) -> None:
    for name, world in WORLDS.items():
        for seed in SEEDS:
            plant = plant_effect(world, data.features, seed)
            assert abs(plant.rel.mean() - world.mean_lift) < 1e-9, (name, seed)
    assert (plant_effect(get_world("null"), data.features, 0).rel == 0).all()


def test_the_planted_effect_is_fixed_by_the_seed(data) -> None:
    a = plant_effect(get_world("heterogeneous"), data.features, 2).rel
    assert np.array_equal(a, plant_effect(get_world("heterogeneous"), data.features, 2).rel)
    assert not np.array_equal(a, plant_effect(get_world("heterogeneous"), data.features, 3).rel)


def test_a_homogeneous_world_gives_everyone_the_same_lift(data) -> None:
    rel = plant_effect(get_world("homogeneous_small"), data.features, 0).rel
    assert rel.std() < 1e-12 and abs(rel[0] - 0.04) < 1e-12


def test_the_heterogeneous_lift_is_a_tree_not_a_line(data) -> None:
    ranks = rank_features(data.features)
    for seed in SEEDS:
        plant = plant_effect(get_world("heterogeneous"), data.features, seed)
        assert plant.n_leaves > 4 and (plant.rel > 0).all()
        # no single feature explains it linearly, and a straight line through all of them leaves most of it
        corr = [abs(np.corrcoef(ranks[:, j], plant.rel)[0, 1]) for j in range(ranks.shape[1])]
        assert max(corr) < 0.9, (seed, dict(zip(FEATURE_COLUMNS, np.round(corr, 2), strict=True)))
        design = np.column_stack([np.ones(len(ranks)), ranks])
        fit = design @ np.linalg.lstsq(design, plant.rel, rcond=None)[0]
        r2 = 1 - ((plant.rel - fit) ** 2).sum() / ((plant.rel - plant.rel.mean()) ** 2).sum()
        # a tree with splits at random features is rarely well explained by a linear model of all of them
        assert r2 < 0.85, (seed, r2)
        # there are real differences between customers
        assert plant.rel.max() > 1.5 * plant.rel.min()


def test_sleeping_dogs_are_about_the_asked_share_with_a_positive_average(data) -> None:
    world = get_world("sleeping_dogs")
    shares = []
    for seed in SEEDS:
        rel = plant_effect(world, data.features, seed).rel
        shares.append(float((rel < 0).mean()))
        assert rel.mean() > 0
    # leaves are whole blocks of customers, so the share is only roughly the target
    assert 0.03 < float(np.mean(shares)) < 0.35
    assert all(s > 0 for s in shares)


def test_fatigue_shrinks_a_positive_effect_and_not_a_negative_one(data) -> None:
    w = make_world(data, "fatigue_heavy", days=28)
    w.reset(0)
    truth = w.truth
    i = np.array([int(np.flatnonzero(truth.rel > 0)[0])], dtype=np.int64)
    fresh = truth.effects(i, Channel.SMS, 5)
    truth.counts[i[0], 3] = 2  # two messages three days ago
    tired = truth.effects(i, Channel.SMS, 5)
    assert abs(tired.rel_eff[0] - fresh.rel_eff[0] * 0.5**2) < 1e-12
    assert tired.p_unsubscribe[0] > fresh.p_unsubscribe[0]
    assert tired.net[0] < fresh.net[0]


def test_contacts_older_than_the_window_are_forgotten(data) -> None:
    w = make_world(data, "fatigue_heavy", days=60)
    w.reset(0)
    truth = w.truth
    i = np.array([int(np.flatnonzero(truth.rel > 0)[0])], dtype=np.int64)
    truth.counts[i[0], 0] = 3
    assert (
        truth.effects(i, Channel.SMS, EFFECT_DAYS - 1).rel_eff[0]
        < truth.effects(i, Channel.SMS, EFFECT_DAYS).rel_eff[0]
    )
    assert abs(truth.effects(i, Channel.SMS, EFFECT_DAYS + 5).rel_eff[0] - truth.rel[i[0]]) < 1e-12


def test_a_negative_lift_costs_margin_and_is_not_helped_by_the_channel_price(data) -> None:
    w = make_world(data, "sleeping_dogs", days=28)
    w.reset(1)
    dogs = np.flatnonzero(w.truth.rel < 0)
    assert dogs.size
    eff = w.truth.effects(dogs.astype(np.int64), Channel.SMS, 3)
    # a dog loses what it would have bought in the window: nothing if it buys nothing then
    assert (eff.gross <= 0).all() and (eff.gross < 0).any() and (eff.net < 0).all()
    assert (
        eff.p_unsubscribe
        >= w.truth.effects(
            np.flatnonzero(w.truth.rel > 0)[:1].astype(np.int64), Channel.SMS, 3
        ).p_unsubscribe[0]
    ).all()


def test_the_value_of_a_contact_is_margin_times_own_receipt_less_costs(data) -> None:
    w = make_world(data, "heterogeneous", days=28)
    w.reset(0)
    t: WavesTruth = w.truth
    i = np.array([int(np.argmax(t.rel * data.base_rate))], dtype=np.int64)
    eff = t.effects(i, Channel.SMS, 0)
    purchases = t.rel[i[0]] * data.base_rate[i[0]] * EFFECT_DAYS
    assert abs(eff.purchases[0] - purchases) < 1e-12
    # the extra purchases at the customer's own mean receipt, less the payback taken from their real receipts on
    # days 15..44 after the send (day 0), each removed with chance 0.4 x rel x 14 / 30
    gain = purchases * 0.25 * data.value[i[0]]
    payback = 0.4 * t.rel[i[0]] * EFFECT_DAYS / 30 * t.at_stake[i[0], 15:45].sum()
    assert abs(eff.gain[0] - gain) < 1e-9 and abs(eff.loss[0] - payback) < 1e-9
    assert abs(eff.gross[0] - (gain - payback)) < 1e-9
    assert eff.cost[0] == CHANNEL_COST[Channel.SMS]
    assert abs(eff.net[0] - (eff.gross[0] - eff.cost[0] - eff.unsubscribe_cost[0])) < 1e-12
    # the Gym's GroundTruth surface agrees with the batch one
    cid = data.customer_ids[int(i[0])]
    at = w.start.replace(hour=11)
    action = Action(goal_id="g", channel=Channel.SMS, send_at=at)
    assert abs(t.expected_net(cid, action, w.start) - eff.net[0]) < 1e-12
    assert t.best_action(cid, [action], w.start) == (action if eff.net[0] > 0 else None)


def test_the_margin_at_stake_is_the_real_receipts_then_the_base_rate(data) -> None:
    w = make_world(data, "heterogeneous", days=84)
    stake = w.truth.at_stake
    day = np.floor(data.post_s / 86_400).astype(np.int64)
    by_customer = np.bincount(data.post_idx, weights=0.25 * data.post_value, minlength=data.n)
    assert np.allclose(stake[:, : data.horizon_days].sum(axis=1), by_customer)
    assert (stake[:, data.horizon_days :] == (0.25 * data.base_rate * data.value)[:, None]).all()
    assert day.max() < data.horizon_days


def test_a_second_contact_on_the_same_days_cannot_remove_more_than_every_receipt(data) -> None:
    w = make_world(data, "sleeping_dogs", days=28, mean_lift=0.07)
    w.reset(0)
    t = w.truth
    dog = np.array([int(np.flatnonzero(t.rel < 0)[0])], dtype=np.int64)
    t.removal[dog[0], :] = 1.0  # every receipt is already gone
    eff = t.effects(dog, Channel.SMS, 0)
    assert eff.loss[0] == 0.0 and eff.gross[0] == 0.0
