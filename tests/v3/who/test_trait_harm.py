"""Sleeping dogs by traits: the harm reading pooled across customers who share a few profile traits.

Pure maths on synthetic records (purchases around messages); no Gym, each test well under a second.
"""

from __future__ import annotations

import numpy as np
import pytest

from autonomy.decide.bidders import LEAVE_ALONE_CREDIBLE, _own_loglik
from autonomy.decide.who import TraitHarm, TraitKeys, WhoConfig

SHARE = 0.5
"""A message's weeks are half of everyone's time in these records."""

Records = dict[str, tuple[float, float, float]]


def record(rng: np.random.Generator, ratio: float, trials: int) -> tuple[float, float, float]:
    """Of `trials` purchases, how many fell in a message's week when buying there is `ratio` times
    buying without one."""
    q = ratio * SHARE / (ratio * SHARE + 1.0 - SHARE)
    return float(rng.binomial(trials, q)), float(trials), SHARE


def population(
    groups: dict[str, tuple[int, float]], *, seed: int = 0, thin: int = 4
) -> tuple[Records, dict[str, str]]:
    """key -> (customers, the group's true ratio); every customer has a thin record (about `thin`
    purchases, at least one)."""
    rng = np.random.default_rng(seed)
    records: Records = {}
    keys: dict[str, str] = {}
    for key, (n, ratio) in groups.items():
        for i in range(n):
            cid = f"{key}-{i:03d}"
            records[cid] = record(rng, ratio, max(int(rng.poisson(thin)), 1))
            keys[cid] = key
    return records, keys


BACKGROUND = {"loyal|light": (150, 1.3), "deals|light": (120, 1.2), "answers|full": (100, 1.1)}


def own_alone(records: Records, cid: str) -> float:
    """Chance a customer's ratio is below 1 from their own record alone, on a flat prior."""
    hits, trials, share = records[cid]
    ll = _own_loglik(np.array([hits]), np.array([trials]), np.array([share]), np.array([True]))[0]
    post = np.exp(ll - ll.max())
    post /= post.sum()
    grid = np.exp(np.linspace(np.log(0.05), np.log(5.0), 241))
    return float(post[grid < 1.0].sum())


def test_a_planted_group_of_sleeping_dogs_with_thin_records_is_left_alone():
    records, keys = population({**BACKGROUND, "dogs|heavy": (60, 0.4), "neutral|heavy": (60, 1.0)})
    harm = TraitHarm()
    harm.fit(records, keys)
    assert harm.fitted
    dogs = [c for c in records if keys[c] == "dogs|heavy"]
    neutral = [c for c in records if keys[c] == "neutral|heavy"]
    left_dogs = np.mean([harm.leave_alone(c, 1.0) for c in dogs])
    left_neutral = np.mean([harm.leave_alone(c, 1.0) for c in neutral])
    assert left_dogs >= 0.7
    assert left_neutral <= 0.05
    # the same customers on their own thin records: almost none can be told apart
    alone = np.mean([own_alone(records, c) >= LEAVE_ALONE_CREDIBLE for c in dogs])
    assert alone <= 0.35
    # the groups are read as planted
    groups = harm.groups()
    # shrunk towards the business's ratio (about 1.2), so not all the way down to the planted 0.4
    assert groups["dogs|heavy"][1] < 0.7
    assert groups["neutral|heavy"][1] == pytest.approx(1.0, abs=0.2)
    assert groups["dogs|heavy"][2] >= LEAVE_ALONE_CREDIBLE > groups["neutral|heavy"][2]
    assert "dogs|heavy" in harm.describe()


def test_a_customer_with_no_record_is_read_by_their_group():
    records, keys = population({**BACKGROUND, "dogs|heavy": (60, 0.4), "neutral|heavy": (60, 1.0)})
    harm = TraitHarm()
    harm.fit(records, keys)
    assert harm.leave_alone("stranger", 1.0, "dogs|heavy")
    assert not harm.leave_alone("stranger", 1.0, "neutral|heavy")
    assert not harm.leave_alone("stranger", 1.0, "no such group")
    assert not harm.leave_alone("stranger", 1.0)
    # nothing at stake, nobody is left alone
    assert not harm.leave_alone("stranger", 0.0, "dogs|heavy")


def test_a_long_own_record_outweighs_the_group():
    records, keys = population({**BACKGROUND, "dogs|heavy": (60, 0.4), "neutral|heavy": (60, 1.0)})
    rng = np.random.default_rng(99)
    # a member of the dogs' group who has bought a lot, and shows no harm at all
    records["dogs-fine"] = record(rng, 1.4, 120)
    keys["dogs-fine"] = "dogs|heavy"
    # a member of the neutral group whose long record shows plain harm
    records["neutral-hurt"] = record(rng, 0.25, 120)
    keys["neutral-hurt"] = "neutral|heavy"
    harm = TraitHarm()
    harm.fit(records, keys)
    assert not harm.leave_alone("dogs-fine", 1.0)
    assert harm.leave_alone("neutral-hurt", 1.0)


def test_a_thin_record_stays_near_its_group_and_more_record_moves_it():
    records, keys = population({**BACKGROUND, "dogs|heavy": (60, 0.4)})
    base = dict(records)
    rng = np.random.default_rng(5)
    counts = (1, 4, 16, 64)
    by_n = {}
    for n in counts:
        # the same person (neutral ratio) with a record of n purchases, in the dogs' group
        records = dict(base)
        records["person"] = record(rng, 1.0, n)
        keys_n = {**keys, "person": "dogs|heavy"}
        harm = TraitHarm()
        harm.fit(records, keys_n)
        by_n[n] = harm.p_harm("person")
    assert by_n[1] > by_n[64]
    assert by_n[1] >= LEAVE_ALONE_CREDIBLE - 0.1  # with one purchase they are their group
    assert by_n[64] < LEAVE_ALONE_CREDIBLE


def test_where_groups_differ_by_noise_alone_nobody_is_left_alone():
    for seed in range(4):
        records, keys = population(
            {f"g{k}": (60, 1.3) for k in range(12)},
            seed=seed,
        )
        harm = TraitHarm()
        harm.fit(records, keys)
        assert harm.fitted
        assert harm.left_alone() == 0
        assert not any(p >= LEAVE_ALONE_CREDIBLE for _, _, p in harm.groups().values())
        assert "No group" in harm.describe()


def test_a_group_too_small_to_read_is_not_evidence():
    records, keys = population({**BACKGROUND, "dogs|heavy": (8, 0.3)})
    harm = TraitHarm(min_group=20)
    harm.fit(records, keys)
    assert not any(harm.leave_alone(c, 1.0) for c in records if keys[c] == "dogs|heavy")
    assert not harm.leave_alone("stranger", 1.0, "dogs|heavy")


def test_too_few_customers_reads_nothing():
    records, keys = population({"a": (10, 0.3)})
    harm = TraitHarm()
    harm.fit(records, keys)
    assert not harm.fitted and harm.left_alone() == 0
    assert not harm.leave_alone("a-000", 1.0, "a")
    assert "Not enough" in harm.describe()


def test_the_keys_split_customers_on_three_traits():
    keys = TraitKeys(engage_cut=0.1, offer_cut=0.5, load_cut=5)
    silent = {"ch_engaged": 0.0, "price_offer_sensitivity": 0.9}
    assert keys.key(silent, 7) == "silent|deals|heavy"
    quiet = {"ch_engaged": 1.0, "ch_click_rate": 0.02, "price_offer_sensitivity": 0.2}
    assert keys.key(quiet, 4) == "answers_less|full_price|light"
    keen = {"ch_engaged": 1.0, "ch_click_rate": 0.3, "price_offer_sensitivity": 0.2}
    assert keys.key(keen, 5) == "answers_more|full_price|heavy"


def test_the_config_refuses_nonsense():
    with pytest.raises(ValueError):
        WhoConfig(risk_urgency=-1.0)
    with pytest.raises(ValueError):
        WhoConfig(value_cost_share=0.0)
    with pytest.raises(ValueError):
        TraitHarm(credible=0.2)
