"""Plan J M100: scoring several offers and choosing the offer are linear in the customers.

Timed on 200,000 customers: `MultiArmUpliftModel.predict_arms` (an X-learner of two offers on LightGBM,
the scoring run's per-arm prediction), `arm_net_values` and `choose_offers` with a budget. The 1M-row
figure is five times the 200k measurement: every step is a vectorised column operation, a tree
ensemble's prediction, or (the budget walk) one sort and one pass.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest

from engine.decide.offer_choice import NO_OFFER, arm_net_values, choose_offers
from engine.measurement.simulate import MULTI_ARM_LEVELS, multi_arm_population
from engine.pilot.roi import ValueCosts
from engine.uplift.config import UpliftBaseModel, UpliftLearner, UpliftPolicyConfig
from engine.uplift.data import coerce_arms
from engine.uplift.learners import MultiArmUpliftModel, make_multi_arm_learner

ROWS = 200_000
FAST_ROWS = 20_000
BUDGET_SECONDS = 3.0
"""The review's line between a fast test and a slow one; the measurement is reported either way."""
FEATURES = ["offer_affinity", "tenure_months", "noise_1", "noise_2"]
COSTS = [ValueCosts(contact_cost=1.0, offer_cost=10.0), ValueCosts(contact_cost=1.0, offer_cost=20.0)]
POLICY = UpliftPolicyConfig(value_per_conversion=1000.0)


def _model() -> MultiArmUpliftModel:
    planted = multi_arm_population(6_000, seed=41)
    codes, _ = coerce_arms(planted.frame["offer"], MULTI_ARM_LEVELS)
    assert codes is not None
    model = make_multi_arm_learner(
        UpliftLearner.X_LEARNER, UpliftBaseModel.LIGHTGBM, levels=MULTI_ARM_LEVELS, seed=1
    )
    model.fit_arms(planted.frame[FEATURES].astype("float64"), codes, planted.frame["converted"].to_numpy())
    return model


def _customers(rows: int) -> pd.DataFrame:
    return multi_arm_population(rows, seed=42).frame[FEATURES].astype("float64")


def _choose(model: MultiArmUpliftModel, customers: pd.DataFrame) -> float:
    started = time.perf_counter()
    predicted = model.predict_arms(customers)
    money = arm_net_values(predicted.uplift, POLICY, arm_costs=COSTS, p_treated=predicted.p_treated)
    choice = choose_offers(
        money.net_value,
        money.cost,
        sleeping_dog=predicted.uplift <= -0.01,
        budget=float(len(customers)) * 3.0,
    )
    elapsed = time.perf_counter() - started
    assert len(choice.arm) == len(customers) and (choice.arm != NO_OFFER).any()
    assert choice.spent <= float(len(customers)) * 3.0
    return elapsed


def _best_of_three(run: Callable[[], float]) -> float:
    return min(run() for _ in range(3))


@pytest.fixture(scope="module")
def model() -> MultiArmUpliftModel:
    return _model()


def _offer_choice(rows: int, seed: int) -> float:
    """`arm_net_values` and `choose_offers` with a budget, on `rows` customers' predictions of 3 offers."""
    rng = np.random.default_rng(seed)
    uplift = rng.normal(0.0, 0.08, size=(rows, 3))
    p_treated = rng.random((rows, 3))
    eligible = rng.random((rows, 3)) < 0.8
    started = time.perf_counter()
    money = arm_net_values(uplift, POLICY, arm_costs=[*COSTS, COSTS[0]], p_treated=p_treated)
    choice = choose_offers(
        money.net_value, money.cost, sleeping_dog=uplift <= -0.01, eligible=eligible, budget=rows * 5.0
    )
    elapsed = time.perf_counter() - started
    assert choice.spent <= rows * 5.0 and (choice.arm != NO_OFFER).any()
    return elapsed


def test_choosing_offers_for_200k_customers_is_fast_and_linear() -> None:
    small = _best_of_three(lambda: _offer_choice(FAST_ROWS, 5))
    large = _offer_choice(ROWS, 6)
    print(
        f"\n[Perf] arm_net_values + choose_offers, {ROWS:,} rows x 3 offers with a budget: {large:.2f}s; "
        f"1M estimate {large * 5:.1f}s; {FAST_ROWS:,} rows {small:.3f}s"
    )
    assert large < BUDGET_SECONDS / 2, f"{ROWS:,} rows took {large:.2f}s"
    assert large < 30 * max(small, 0.02), "ten times the rows took far more than ten times as long"


@pytest.mark.slow
def test_scoring_and_choosing_for_200k_customers_is_linear(model: MultiArmUpliftModel) -> None:
    """Slow because predicting two offers' X-learners (eight tree ensembles) on 200,000 customers takes
    about 16 s on one shared core: about twice a one-offer model's prediction, which it is made of."""
    small_customers, large_customers = _customers(FAST_ROWS), _customers(ROWS)
    small = _best_of_three(lambda: _choose(model, small_customers))
    large = _choose(model, large_customers)
    print(
        f"\n[Perf] predict_arms + arm_net_values + choose_offers, {ROWS:,} rows: {large:.2f}s; "
        f"1M estimate {large * 5:.1f}s; {FAST_ROWS:,} rows {small:.2f}s"
    )
    # Linear work gives about 10x for ten times the rows and quadratic work about 100x; 30x leaves room
    # for contention while still catching anything worse than linear.
    assert large < 30 * max(small, 0.05), "ten times the rows took far more than ten times as long"
