"""Plan J M100 part B: the scoring run's choice of offer is linear in the customers.

Timed on 200,000 customers of two offers: `engine.decide.offer_run.decide_offers` (each offer's net value
with its own costs, per-channel eligibility, sleeping dogs, the greedy budget walk, the channel of each
offer given) and the treat list's reading of the choice (`_apply_offer_choice`). The model's prediction
is timed by `test_offer_choice_scale.py`. The 1M-row figure is five times the 200k measurement: every
step is a vectorised column operation or (the budget walk) one sort and one pass.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from engine.decide.catalogue import CatalogueStamp, StampedAction
from engine.decide.offer_choice import NO_OFFER
from engine.decide.offer_run import decide_offers, plan_arms
from engine.decide.treat_list import _apply_offer_choice, _OfferChoice
from engine.uplift.config import UpliftPolicyConfig

ROWS = 200_000
FAST_ROWS = 20_000
BUDGET_SECONDS = 3.0
"""The review's line between a fast test and a slow one; the measurement is reported either way."""
LEVELS = ("none", "offer_a", "offer_b")
STAMP = CatalogueStamp(
    run_id="r_20261008_0e500001",
    catalogue_sha256="0" * 64,
    planned_channels={"a": ("sms",), "b": ("email", "sms")},
    created_at=datetime(2026, 10, 8, tzinfo=UTC),
    actions={
        "a": StampedAction(label="A", channels=("sms",), offer_cost=10.0, contact_cost=0.5),
        "b": StampedAction(label="B", channels=("email", "sms"), offer_cost=50.0, contact_cost=0.2),
    },
)


def _choose(rows: int, seed: int) -> float:
    rng = np.random.default_rng(seed)
    uplift = rng.normal(0.02, 0.08, size=(rows, 2))
    taken = rng.random((rows, 2))
    contactable = {"sms": rng.random(rows) < 0.7, "email": rng.random(rows) < 0.6}
    policy = UpliftPolicyConfig.model_validate(
        {
            "value_per_conversion": 1000.0,
            "arm_action_ids": {"offer_a": "a", "offer_b": "b"},
            "total_budget": rows * 4.0,
        }
    )
    arms, _ = plan_arms(LEVELS, policy, stamp=STAMP, configured_channels=("sms", "email"), value_costs=None)
    explore = rng.random(rows) < 0.05
    started = time.perf_counter()
    decided = decide_offers(
        uplift,
        taken,
        arms=arms,
        policy=policy,
        suppressed=rng.random(rows) < 0.05,
        control=rng.random(rows) < 0.1,
        sleeping_dog_max=-0.01,
        contactable=contactable,
    )
    choice = decided.choice
    applied = _apply_offer_choice(
        _OfferChoice(
            covered=np.ones(rows, dtype=bool),
            arm=choice.arm,
            label=np.array([None, "A", "B"], dtype=object)[choice.arm],
            channel=decided.channel,
            net_value=choice.net_value,
            runner_up_arm=choice.runner_up_arm,
            runner_up_label=np.array([None, "A", "B"], dtype=object)[choice.runner_up_arm],
            runner_up_channel=decided.runner_up_channel,
            runner_up_value=choice.runner_up_net_value,
            reason=np.array(choice.reason, dtype=object),
            explore_arm=decided.explore_arm,
            explore_label=np.array([None, "A", "B"], dtype=object)[decided.explore_arm],
            explore_channel=decided.explore_channel,
            explore_value=decided.explore_net_value,
            explore_cost=decided.explore_cost,
        ),
        suppressed=np.zeros(rows, dtype=bool),
        held_out=np.zeros(rows, dtype=bool),
        explore=explore,
        fallback_treat=np.zeros(rows, dtype=bool),
        fallback_offer=pd.Series([None] * rows, dtype="object"),
        fallback_channel=pd.Series([None] * rows, dtype="object"),
        fallback_net_value=pd.Series(np.full(rows, np.nan)),
        contactability_written=True,
    )
    elapsed = time.perf_counter() - started
    assert choice.spent <= rows * 4.0 and (choice.arm != NO_OFFER).any()
    extra = explore & (choice.arm == NO_OFFER) & (decided.explore_arm != NO_OFFER)
    assert int(applied.treat.sum()) == int((choice.arm != NO_OFFER).sum()) + int(extra.sum())
    assert extra.any() and applied.explore_cost is not None and applied.explore_cost > 0
    given = choice.arm == 1
    assert contactable["sms"][given].all(), "offer A is sent only by SMS"
    return elapsed


def _best_of_three(run: Callable[[], float]) -> float:
    return min(run() for _ in range(3))


def test_choosing_offers_in_the_run_for_200k_customers_is_fast_and_linear() -> None:
    small = _best_of_three(lambda: _choose(FAST_ROWS, 5))
    large = _choose(ROWS, 6)
    print(
        f"\n[Perf] decide_offers + the treat list's reading, {ROWS:,} rows x 2 offers with a budget: "
        f"{large:.2f}s; 1M estimate {large * 5:.1f}s; {FAST_ROWS:,} rows {small:.3f}s"
    )
    assert large < BUDGET_SECONDS, f"{ROWS:,} rows took {large:.2f}s"
    # Linear work gives about 10x for ten times the rows and quadratic work about 100x.
    assert large < 30 * max(small, 0.02), "ten times the rows took far more than ten times as long"
