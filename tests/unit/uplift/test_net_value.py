"""Milestone M97 acceptance tests: rank customers by expected net money, not by uplift alone.

Acceptance criteria:
- The identity in tests/unit/uplift/test_profit_curve.py (the profit-curve point at the budget equals
  the recommendation, field for field) holds with and without value_column. Extend that file; never loosen it.
- A constant value column reproduces today's scalar path exactly.
- On a fixture where high-value persuadables have lower raw uplift, they rank higher.
- min_roi is respected; no sleeping dog is ever treated.
- Money fields are null with a plain reason when value inputs are missing.
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.uplift.config import UpliftPolicyConfig
from engine.uplift.contracts import PolicyStopReason, Segment
from engine.uplift.metrics import HoldoutUplift
from engine.uplift.policy import (
    NO_MONEY_NOTE,
    choose_contacts,
    profit_curve,
    ranking,
    recommend_policy,
)

RUN_ID = "r_20261008_m97netval"

P = Segment.PERSUADABLE
S = Segment.SURE_THING
L = Segment.LOST_CAUSE
D = Segment.SLEEPING_DOG


def segs(*items: Segment) -> np.ndarray:
    array = np.empty(len(items), dtype=object)
    array[:] = list(items)
    return array


def synthetic_holdout(
    seed: int, rows: int = 500, samples: int = 40, values: np.ndarray | None = None
) -> HoldoutUplift:
    rng = np.random.default_rng(seed)
    pred = rng.normal(0.03, 0.04, rows)
    t = rng.integers(0, 2, rows).astype(np.int64)
    y = (rng.random(rows) < 0.2 + t * np.clip(pred, 0.0, None) * 3).astype(np.int64)
    return HoldoutUplift(pred=pred, t=t, y=y, samples=samples, seed=seed, value=values)


def test_constant_value_column_reproduces_scalar_path_exactly() -> None:
    """A constant value column reproduces today's scalar path exactly."""
    rng = np.random.default_rng(42)
    rows = 200
    uplift = np.round(rng.normal(0.04, 0.05, rows), 4)
    segments = np.where(uplift >= 0.02, P, np.where(uplift <= -0.01, D, L)).astype(object)
    eligible = rng.random(rows) < 0.85
    tiebreak = rng.integers(0, 2**60, rows).astype(np.uint64)

    scalar_value = 50.0
    cost = 1.2
    budget = 40

    scalar_policy = UpliftPolicyConfig(
        budget_contacts=budget,
        cost_per_contact=cost,
        value_per_conversion=scalar_value,
    )

    vector_policy = UpliftPolicyConfig(
        budget_contacts=budget,
        cost_per_contact=cost,
        value_column="order_value",
        margin_pct=100.0,
    )

    constant_values = np.full(rows, scalar_value, dtype=np.float64)

    sel_scalar, reason_scalar = choose_contacts(
        uplift, segments, scalar_policy, eligible=eligible, tiebreak=tiebreak
    )
    sel_vector, reason_vector = choose_contacts(
        uplift, segments, vector_policy, eligible=eligible, tiebreak=tiebreak, values=constant_values
    )

    assert reason_scalar == reason_vector
    assert np.array_equal(sel_scalar, sel_vector)

    lookup_scalar = synthetic_holdout(42, rows=rows, samples=30)
    lookup_vector = HoldoutUplift(
        pred=lookup_scalar.pred,
        t=lookup_scalar.t,
        y=lookup_scalar.y,
        samples=30,
        seed=42,
        value=np.full(rows, scalar_value, dtype=np.float64),
    )

    rec_scalar, _ = recommend_policy(
        uplift,
        segments,
        scalar_policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=lookup_scalar.at,
        eligible=eligible,
        tiebreak=tiebreak,
    )
    rec_vector, _ = recommend_policy(
        uplift,
        segments,
        vector_policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=lookup_vector.at,
        eligible=eligible,
        tiebreak=tiebreak,
        values=constant_values,
    )

    assert rec_scalar.contacts_recommended == rec_vector.contacts_recommended
    assert rec_scalar.stop_reason == rec_vector.stop_reason
    assert rec_scalar.expected_cost == rec_vector.expected_cost
    assert rec_scalar.expected_value == pytest.approx(rec_vector.expected_value)
    assert rec_scalar.expected_net_value == pytest.approx(rec_vector.expected_net_value)


def test_high_value_persuadables_with_lower_raw_uplift_rank_higher() -> None:
    """On a fixture where high-value persuadables have lower raw uplift, they rank higher."""
    # Customer 0: raw uplift 0.03, value 1,000 INR -> expected gross margin = 30.0 INR
    # Customer 1: raw uplift 0.10, value 100 INR   -> expected gross margin = 10.0 INR
    # Cost per contact = 1.0 INR
    # By raw uplift alone: Customer 1 (0.10) > Customer 0 (0.03).
    # By net value: Customer 0 (30 - 1 = 29 INR) > Customer 1 (10 - 1 = 9 INR).
    uplift = np.array([0.03, 0.10])
    values = np.array([1000.0, 100.0])
    segments = segs(P, P)

    policy = UpliftPolicyConfig(
        budget_contacts=1,
        cost_per_contact=1.0,
        value_column="balance",
        margin_pct=100.0,
    )

    # Raw ranking orders index 1 first
    raw_order = ranking(uplift)
    assert raw_order[0] == 1

    # Net value ranking orders index 0 first
    net_order = ranking(uplift, net_value=uplift * values - 1.0)
    assert net_order[0] == 0

    selected, reason = choose_contacts(uplift, segments, policy, values=values)
    # With budget=1, only Customer 0 (the high-value one) is selected
    assert bool(selected[0]) is True
    assert bool(selected[1]) is False
    assert reason is PolicyStopReason.BUDGET


def test_min_roi_is_respected_and_no_sleeping_dog_is_ever_treated() -> None:
    """min_roi is respected; no sleeping dog is ever treated."""
    # Persuadable 0: uplift 0.10, value 100, cost 5.0 -> net 5.0, roi = 5.0 / 5.0 = 1.0 (100% ROI)
    # Persuadable 1: uplift 0.06, value 100, cost 5.0 -> net 1.0, roi = 1.0 / 5.0 = 0.2 (20% ROI)
    # Persuadable 2: uplift 0.052, value 100, cost 5.0 -> net 0.2, roi = 0.2 / 5.0 = 0.04 (4% ROI)
    # Sleeping Dog 3: uplift 0.50 (very high raw uplift!), value 10,000, cost 5.0
    uplift = np.array([0.10, 0.06, 0.052, 0.50])
    values = np.array([100.0, 100.0, 100.0, 10000.0])
    segments = segs(P, P, P, D)

    # Require at least 15% ROI (min_roi = 0.15)
    policy = UpliftPolicyConfig(
        cost_per_contact=5.0,
        value_column="order_value",
        min_roi=0.15,
    )

    selected, reason = choose_contacts(uplift, segments, policy, values=values)

    # Persuadable 0 (roi=1.0) and Persuadable 1 (roi=0.2) pass min_roi (0.15).
    # Persuadable 2 (roi=0.04) fails min_roi.
    # Sleeping dog 3 is NEVER selected, even though raw uplift and value are huge.
    assert bool(selected[0]) is True
    assert bool(selected[1]) is True
    assert bool(selected[2]) is False
    assert bool(selected[3]) is False
    assert reason is PolicyStopReason.VALUE_BELOW_COST


def test_money_fields_are_null_with_plain_reason_when_value_inputs_are_missing() -> None:
    """Money fields are null with a plain reason when value inputs are missing."""
    uplift = np.array([0.05, 0.04, 0.02])
    segments = segs(P, P, P)

    # No cost and no value configured
    policy = UpliftPolicyConfig(budget_contacts=2)

    rec, _ = recommend_policy(uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True)
    curve = profit_curve(uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True)

    assert rec.expected_cost is None
    assert rec.expected_value is None
    assert rec.expected_net_value is None
    assert rec.net_value_low is None
    assert rec.net_value_high is None

    assert curve.configured.expected_cost is None
    assert curve.configured.expected_value is None
    assert curve.configured.expected_net_value is None
    assert curve.configured.roi is None
    assert curve.optimum is None
    assert curve.optimum_note == NO_MONEY_NOTE
    assert not curve.bands_available
