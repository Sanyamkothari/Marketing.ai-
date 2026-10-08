"""Plan J M97: rank customers by expected net money, not by uplift alone (DEC-1307).

The milestone's acceptance tests first (the partner agent's, on the explicit API the review asked
for), then one regression test per review finding:

* `min_roi` holds through `recommend_policy` and `profit_curve` - the public entry points - with the
  contact cost from `configs/pilot/value.yaml` and with per-row `offer_cost × p_treated` costs;
* the budget curve is linear in the rows (cumulative sums, checked against a brute-force reference);
* nothing is fabricated: a missing value counts at zero and is counted, a hold-out without values
  quotes no money, and the reason is said;
* `expected_incremental_conversions` is conversions; the money is the value-weighted uplift;
* margin and horizon apply to the ranking, the cut, the reported value and the curve alike;
* a configuration without the new settings computes exactly what `main` computed before M97.

The identity "the curve's point at the budget IS the recommendation" in every money configuration is
in `test_profit_curve.py`.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from engine.pilot.roi import ValueCosts, lookup_value_costs
from engine.uplift.config import UpliftPolicyConfig
from engine.uplift.contracts import ConfidenceValue, PolicyStopReason, Segment
from engine.uplift.metrics import HoldoutUplift, bootstrap_uplift_at
from engine.uplift.policy import (
    MAX_MISSING_VALUE_SHARE,
    NO_MONEY_NOTE,
    _best_contacts,
    choose_contacts,
    customer_net_values,
    holdout_lookups,
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


def synthetic_holdout(seed: int, rows: int = 500) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    pred = rng.normal(0.03, 0.04, rows)
    t = rng.integers(0, 2, rows).astype(np.int64)
    y = (rng.random(rows) < 0.2 + t * np.clip(pred, 0.0, None) * 3).astype(np.int64)
    return pred, t, y


def lookup_of(
    seed: int,
    policy: UpliftPolicyConfig,
    *,
    rows: int = 500,
    values: np.ndarray | None = None,
    samples: int = 30,
) -> tuple[HoldoutUplift | None, HoldoutUplift | None, str | None]:
    pred, t, y = synthetic_holdout(seed, rows)
    found = holdout_lookups(
        pred,
        t,
        y,
        policy=policy,
        samples=samples,
        seed=seed,
        ranked_by_value=values is not None,
        values=values,
        p_treated=np.full(rows, 0.3),
    )
    return found.conversions, found.value, found.note


def at(lookup: HoldoutUplift | None) -> object:
    return None if lookup is None else lookup.at


# ---------------------------------------------------------------------------
# The milestone's acceptance tests
# ---------------------------------------------------------------------------
def test_constant_value_column_reproduces_scalar_path_exactly() -> None:
    """A constant value column reproduces today's scalar path: same customers, same cost, same money."""
    rng = np.random.default_rng(42)
    rows = 200
    uplift = np.round(rng.normal(0.04, 0.05, rows), 4)
    segments = np.where(uplift >= 0.02, P, np.where(uplift <= -0.01, D, L)).astype(object)
    eligible = rng.random(rows) < 0.85
    tiebreak = rng.integers(0, 2**60, rows).astype(np.uint64)
    scalar_value, cost, budget = 50.0, 1.2, 40
    scalar_policy = UpliftPolicyConfig(
        budget_contacts=budget, cost_per_contact=cost, value_per_conversion=scalar_value
    )
    vector_policy = UpliftPolicyConfig(
        budget_contacts=budget, cost_per_contact=cost, value_column="order_value", margin_pct=100.0
    )
    constant = np.full(rows, scalar_value, dtype=np.float64)

    sel_scalar, reason_scalar = choose_contacts(
        uplift, segments, scalar_policy, eligible=eligible, tiebreak=tiebreak
    )
    sel_vector, reason_vector = choose_contacts(
        uplift, segments, vector_policy, eligible=eligible, tiebreak=tiebreak, values=constant
    )
    assert reason_scalar == reason_vector
    assert np.array_equal(sel_scalar, sel_vector)

    conversions, _, _ = lookup_of(42, scalar_policy, rows=rows)
    vector_conversions, vector_value, note = lookup_of(42, vector_policy, rows=rows, values=constant)
    assert note is None and vector_value is not None
    rec_scalar, _ = recommend_policy(
        uplift,
        segments,
        scalar_policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=at(conversions),  # type: ignore[arg-type]
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
        observed_top_share=at(vector_conversions),  # type: ignore[arg-type]
        observed_top_value=vector_value.at,
        eligible=eligible,
        tiebreak=tiebreak,
        values=constant,
    )
    assert rec_scalar.contacts_recommended == rec_vector.contacts_recommended
    assert rec_scalar.stop_reason == rec_vector.stop_reason
    assert rec_scalar.expected_cost == rec_vector.expected_cost
    assert rec_scalar.expected_incremental_conversions == rec_vector.expected_incremental_conversions
    assert rec_scalar.expected_value == pytest.approx(rec_vector.expected_value)
    assert rec_scalar.expected_net_value == pytest.approx(rec_vector.expected_net_value)
    assert rec_vector.values_missing == 0 and rec_vector.money_note is None


def test_high_value_persuadables_with_lower_raw_uplift_rank_higher() -> None:
    """On a fixture where high-value persuadables have lower raw uplift, they rank higher."""
    # Customer 0: uplift 0.03 × ₹1,000 − ₹1 = ₹29; customer 1: uplift 0.10 × ₹100 − ₹1 = ₹9.
    uplift = np.array([0.03, 0.10])
    values = np.array([1000.0, 100.0])
    policy = UpliftPolicyConfig(
        budget_contacts=1, cost_per_contact=1.0, value_column="balance", margin_pct=100.0
    )
    assert ranking(uplift)[0] == 1
    assert ranking(uplift, net_value=customer_net_values(uplift, policy, values=values).net_value)[0] == 0
    selected, reason = choose_contacts(uplift, segs(P, P), policy, values=values)
    assert selected.tolist() == [True, False]
    assert reason is PolicyStopReason.BUDGET


def test_min_roi_is_respected_and_no_sleeping_dog_is_ever_treated() -> None:
    """min_roi is respected through every entry point; no sleeping dog is ever treated."""
    # Net values 5.0, 1.0 and 0.2 at a cost of 5.0: ROI 100 %, 20 % and 4 %; the sleeping dog's is huge.
    uplift = np.array([0.10, 0.06, 0.052, 0.50])
    values = np.array([100.0, 100.0, 100.0, 10000.0])
    segments = segs(P, P, P, D)
    policy = UpliftPolicyConfig(cost_per_contact=5.0, value_column="order_value", min_roi=0.15)

    selected, reason = choose_contacts(uplift, segments, policy, values=values)
    assert selected.tolist() == [True, True, False, False]
    assert reason is PolicyStopReason.VALUE_BELOW_COST
    recommendation, chosen = recommend_policy(
        uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True, values=values
    )
    assert chosen.tolist() == [True, True, False, False]
    assert recommendation.contacts_recommended == 2 and recommendation.expected_cost == 10.0
    curve = profit_curve(
        uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True, values=values
    )
    assert curve.max_contacts == 2 and curve.max_contacts_reason is PolicyStopReason.VALUE_BELOW_COST


def test_money_fields_are_null_with_plain_reason_when_value_inputs_are_missing() -> None:
    """Money fields are null with a plain reason when value inputs are missing."""
    uplift = np.array([0.05, 0.04, 0.02])
    segments = segs(P, P, P)
    policy = UpliftPolicyConfig(budget_contacts=2)
    conversions, _, _ = lookup_of(3, policy)
    rec, _ = recommend_policy(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed_top_share=at(conversions),  # type: ignore[arg-type]
    )
    curve = profit_curve(
        uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True, observed=conversions
    )
    assert rec.expected_incremental_conversions is not None
    assert (rec.expected_cost, rec.expected_value, rec.expected_net_value) == (None, None, None)
    assert (rec.net_value_low, rec.net_value_high) == (None, None)
    assert curve.configured.expected_cost is None
    assert curve.configured.expected_value is None
    assert curve.configured.expected_net_value is None
    assert curve.configured.roi is None
    assert curve.optimum is None
    assert curve.optimum_note == NO_MONEY_NOTE
    assert not curve.bands_available


# ---------------------------------------------------------------------------
# Review finding 1: min_roi through the public entry points, with the costs a real run has
# ---------------------------------------------------------------------------
def test_min_roi_holds_through_recommend_policy_with_the_value_yaml_contact_cost() -> None:
    """The review's reproduction: net 9.14, 4.14, 1.14 at the ₹0.86 contact cost of value.yaml, min_roi 5.

    Only the first pays 5 × its cost (4.30): `choose_contacts` chose one; `recommend_policy` chose all
    three because it handed down the net value but not the cost.
    """
    assert lookup_value_costs().contact_cost == 0.86  # the shipped editable default
    uplift = np.array([0.10, 0.05, 0.02])
    values = np.array([100.0, 100.0, 100.0])
    segments = segs(P, P, P)
    policy = UpliftPolicyConfig(value_column="order_value", min_roi=5.0)  # no cost_per_contact

    money = customer_net_values(uplift, policy, values=values)
    assert money.net_value is not None
    assert money.net_value.tolist() == pytest.approx([9.14, 4.14, 1.14])
    selected, _ = choose_contacts(uplift, segments, policy, values=values)
    recommendation, chosen = recommend_policy(
        uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True, values=values
    )
    curve = profit_curve(
        uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True, values=values
    )
    assert selected.tolist() == chosen.tolist() == [True, False, False]
    assert recommendation.contacts_recommended == 1
    assert recommendation.stop_reason is PolicyStopReason.VALUE_BELOW_COST
    assert recommendation.expected_cost == 0.86
    assert curve.max_contacts == 1 and curve.configured.contacts == 1
    assert curve.configured.expected_cost == recommendation.expected_cost


def test_min_roi_holds_with_per_row_offer_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    """With an offer cost, each row's cost is `contact + offer × p_treated`: the cut uses that row's cost."""
    monkeypatch.setattr(
        "engine.pilot.roi.lookup_value_costs", lambda *_a, **_k: ValueCosts(offer_cost=10.0, contact_cost=1.0)
    )
    uplift = np.array([0.20, 0.10, 0.08, 0.06])
    values = np.array([100.0, 100.0, 100.0, 100.0])
    p_treated = np.array([0.9, 0.1, 0.5, 0.1])
    # costs 10.0, 2.0, 6.0, 2.0; net 10.0, 8.0, 2.0, 4.0; ranked 0, 1, 3, 2; min_roi 0.5 needs net ≥ cost / 2.
    segments = segs(P, P, P, P)
    policy = UpliftPolicyConfig(value_column="order_value", min_roi=0.5)
    money = customer_net_values(uplift, policy, values=values, p_treated=p_treated)
    assert money.cost is not None and money.cost.tolist() == pytest.approx([10.0, 2.0, 6.0, 2.0])
    assert money.contact_cost is None  # the costs differ, so totals are summed per row

    recommendation, chosen = recommend_policy(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        values=values,
        p_treated=p_treated,
    )
    curve = profit_curve(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        values=values,
        p_treated=p_treated,
    )
    # 0 (10 ≥ 5), 1 (8 ≥ 1), 3 (4 ≥ 1) pay; 2 (2 < 3) does not, and it is last in the ranking.
    assert chosen.tolist() == [True, True, False, True]
    assert recommendation.expected_cost == pytest.approx(14.0)
    assert curve.max_contacts == 3 and curve.configured.expected_cost == recommendation.expected_cost

    stricter = policy.model_copy(update={"min_roi": 1.5})  # the top row needs 15 and earns 10
    recommendation, chosen = recommend_policy(
        uplift,
        segments,
        stricter,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        values=values,
        p_treated=p_treated,
    )
    assert chosen.tolist() == [False, False, False, False]  # the top row (10 < 15) stops the prefix
    assert recommendation.stop_reason is PolicyStopReason.VALUE_BELOW_COST


def test_money_computed_once_is_used_as_given() -> None:
    """The entry points take the caller's `CustomerMoney` and refuse it alongside raw inputs."""
    uplift = np.array([0.10, 0.05])
    policy = UpliftPolicyConfig(value_column="v", cost_per_contact=1.0, min_roi=1.0)
    money = customer_net_values(uplift, policy, values=np.array([100.0, 100.0]))
    selected, _ = choose_contacts(uplift, segs(P, P), policy, money=money)
    assert selected.tolist() == [True, True]  # net 9 ≥ 1 and 4 ≥ 1
    with pytest.raises(ValueError, match="either money or values"):
        choose_contacts(uplift, segs(P, P), policy, money=money, values=np.array([1.0, 1.0]))


# ---------------------------------------------------------------------------
# Review finding 2: linear time
# ---------------------------------------------------------------------------
class Points:
    """A fake hold-out whose observed value per customer falls with the share and is NaN at a few."""

    def points(self, fractions: np.ndarray) -> np.ndarray:
        shares = np.asarray(fractions, dtype=np.float64)
        out = 30.0 - 25.0 * shares
        out[(np.arange(len(shares)) % 7) == 3] = np.nan
        return out

    def intervals(self, fractions: object) -> list[ConfidenceValue | None]:
        raise AssertionError("_best_contacts never asks for intervals")


@pytest.mark.parametrize("seed", range(5))
def test_best_contacts_equals_a_brute_force_reference(seed: int) -> None:
    rng = np.random.default_rng(seed)
    rows = int(rng.integers(20, 80))
    uplift = rng.normal(0.05, 0.05, rows)
    values = rng.uniform(5.0, 300.0, rows)
    p_treated = rng.uniform(0.0, 1.0, rows)
    policy = UpliftPolicyConfig(value_column="v", margin_pct=40.0, horizon_months=3)
    money = customer_net_values(
        uplift,
        policy,
        values=values,
        p_treated=p_treated,
        value_costs=ValueCosts(offer_cost=4.0, contact_cost=0.5),
    )
    assert money.net_value is not None and money.cost is not None and money.unit is not None
    ranked = ranking(uplift, net_value=money.net_value)[: int(rng.integers(1, rows))]
    depths = np.sort(rng.choice(np.arange(1, rows + 1), size=len(ranked), replace=False))
    observed = Points()
    best, note = _best_contacts(depths, rows, money, None, observed, money.cost_totals(ranked), None)

    lift = observed.points(depths / rows)
    reference = [0.0] + [
        c * lift[c - 1] * money.unit - sum(float(money.cost[i]) for i in ranked[:c])
        for c in range(1, len(ranked) + 1)
    ]
    expected = int(np.nanargmax(np.asarray(reference)))
    assert note is None and best == expected


def test_the_budget_curve_is_linear_on_200k_rows_with_values_and_per_row_costs() -> None:
    """Quadratic `_best_contacts` took ~2.3 s at 60k rows (13 minutes at 1M); this must stay fast."""
    rows = 200_000
    rng = np.random.default_rng(7)
    uplift = rng.normal(0.04, 0.03, rows)
    segments = np.where(uplift >= 0.02, P, L).astype(object)
    values = rng.gamma(2.0, 50.0, rows)
    p_treated = rng.uniform(0.0, 0.5, rows)
    t = rng.integers(0, 2, rows).astype(np.int64)
    y = (rng.random(rows) < 0.1 + t * np.clip(uplift, 0.0, None)).astype(np.int64)
    policy = UpliftPolicyConfig(value_column="v", margin_pct=30.0, min_roi=0.1)
    costs = ValueCosts(offer_cost=5.0, contact_cost=0.86)
    started = time.perf_counter()
    money = customer_net_values(uplift, policy, values=values, p_treated=p_treated, value_costs=costs)
    assert money.contact_cost is None  # per-row costs
    lookups = holdout_lookups(
        uplift,
        t,
        y,
        policy=policy,
        samples=10,
        seed=7,
        ranked_by_value=True,
        values=values,
        p_treated=p_treated,
        value_costs=costs,
    )
    curve = profit_curve(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed=lookups.conversions,
        observed_value=lookups.value,
        money=money,
    )
    elapsed = time.perf_counter() - started
    assert curve.optimum is not None and curve.value_weighted
    assert elapsed < 10.0, f"profit_curve took {elapsed:.1f}s on {rows} rows"


# ---------------------------------------------------------------------------
# Review findings 4 and 5: nothing fabricated; conversions are conversions
# ---------------------------------------------------------------------------
def test_conversions_stay_conversions_and_the_value_comes_from_the_value_weighted_uplift() -> None:
    rng = np.random.default_rng(11)
    rows = 400
    uplift = np.round(rng.normal(0.05, 0.04, rows), 4)
    segments = np.where(uplift >= 0.02, P, L).astype(object)
    values = rng.uniform(100.0, 1000.0, rows)
    policy = UpliftPolicyConfig(value_column="spend", cost_per_contact=1.0, margin_pct=25.0, horizon_months=2)
    conversions, value, note = lookup_of(11, policy, rows=rows, values=values)
    assert conversions is not None and value is not None and note is None
    recommendation, _ = recommend_policy(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed_top_share=conversions.at,
        observed_top_value=value.at,
        values=values,
    )
    contacts = recommendation.contacts_recommended
    money = customer_net_values(uplift, policy, values=values)
    order = ranking(uplift, net_value=money.net_value)
    depth = int(np.flatnonzero(segments[order] == P)[contacts - 1]) + 1
    unweighted = conversions.at(depth / rows)
    weighted = value.at(depth / rows)
    assert unweighted is not None and weighted is not None
    expected = recommendation.expected_incremental_conversions
    assert expected is not None and expected.value == contacts * unweighted.value
    assert expected.value <= contacts  # a count of extra conversions, never rupees
    assert recommendation.expected_value == contacts * weighted.value * (0.25 * 2)
    assert recommendation.expected_net_value == recommendation.expected_value - contacts * 1.0


def test_a_hold_out_without_values_quotes_no_money_and_says_why() -> None:
    uplift = np.array([0.10, 0.05, 0.03])
    values = np.array([500.0, 200.0, 900.0])
    policy = UpliftPolicyConfig(value_column="balance", cost_per_contact=1.0)
    found = holdout_lookups(
        *synthetic_holdout(5), policy=policy, samples=10, seed=5, ranked_by_value=True, values=None
    )
    assert found.conversions is None and found.value is None
    assert found.note is not None and "training hold-out has no 'balance' values" in found.note
    recommendation, chosen = recommend_policy(
        uplift,
        segs(P, P, P),
        policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=None,
        observed_top_value=None,
        holdout_note=found.note,
        values=values,
    )
    assert chosen.all()  # still ranked by value and cut on cost
    assert recommendation.expected_incremental_conversions is None
    assert recommendation.expected_value is None and recommendation.expected_net_value is None
    assert recommendation.expected_cost == 3.0
    assert recommendation.money_note == found.note


def test_missing_values_count_at_zero_are_counted_and_never_filled_in() -> None:
    uplift = np.array([0.10, 0.09, 0.08, 0.07, 0.06, 0.05, 0.04, 0.03, 0.03, 0.03, 0.03])
    values = np.array([100.0, np.nan, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    policy = UpliftPolicyConfig(value_column="balance", cost_per_contact=0.5)
    money = customer_net_values(uplift, policy, values=values)
    assert money.values_missing == 1 and money.unit == 1.0  # 1 of 11 is under the 10 % limit
    assert money.net_value is not None and money.net_value[1] == -0.5  # zero value, still its cost
    selected, _ = choose_contacts(uplift, segs(*([P] * 11)), policy, values=values)
    assert not selected[1] and selected.sum() == 10
    recommendation, _ = recommend_policy(
        uplift, segs(*([P] * 11)), policy, run_id=RUN_ID, computed_on="scored", causal=True, values=values
    )
    assert recommendation.values_missing == 1
    assert (
        recommendation.money_note is not None
        and "1 of 11 customers have no 'balance'" in recommendation.money_note
    )


def test_too_many_missing_values_null_the_money_with_a_reason() -> None:
    uplift = np.array([0.10, 0.09, 0.08, 0.07])
    values = np.array([100.0, np.nan, 100.0, 100.0])  # 25 % missing, above the limit
    assert MAX_MISSING_VALUE_SHARE < 0.25
    policy = UpliftPolicyConfig(value_column="balance", cost_per_contact=0.5)
    conversions, value, _ = lookup_of(9, policy, rows=4 * 125, values=np.tile(values, 125))
    assert conversions is not None and value is None  # the hold-out is 25 % missing too
    recommendation, _ = recommend_policy(
        uplift,
        segs(P, P, P, P),
        policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=conversions.at,
        values=values,
    )
    assert recommendation.expected_value is None and recommendation.expected_net_value is None
    assert recommendation.expected_cost == 1.5
    assert recommendation.expected_incremental_conversions is not None
    assert recommendation.money_note is not None and "(25%) have no 'balance'" in recommendation.money_note


def test_the_uplift_actions_stage_does_not_fail_on_a_missing_value() -> None:
    """One customer without a value must not fail the scoring run; the value is not invented."""
    from engine.uplift.actions import (
        CUSTOMER_VALUE_COLUMN,
        NET_VALUE_COLUMN,
        TREAT_ACTION,
        apply_uplift_actions,
    )
    from tests.unit.uplift.test_uplift_actions import THRESHOLDS, use_case

    rows = 60
    rng = np.random.default_rng(1)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C-{index:04d}" for index in range(rows)],
            "uplift": np.round(rng.uniform(0.03, 0.2, rows), 4),
            "p_treated": np.round(rng.uniform(0.3, 0.5, rows), 4),
            "p_control": np.round(rng.uniform(0.1, 0.2, rows), 4),
            "opted_out": [False] * rows,
            "order_value": [None if index == 4 else 100.0 + index for index in range(rows)],
        }
    )
    config = use_case(control_fraction=0.0, policy={"value_column": "order_value", "cost_per_contact": 1.0})
    result, recommendation = apply_uplift_actions(
        frame, config, run_id=RUN_ID, primary_key="customer_id", causal=True, thresholds=THRESHOLDS
    )
    assert recommendation.values_missing == 1
    assert np.isnan(result[CUSTOMER_VALUE_COLUMN].iat[4])
    assert result[NET_VALUE_COLUMN].iat[4] == -1.0
    assert result["action"].iat[4] != TREAT_ACTION


# ---------------------------------------------------------------------------
# Review finding 6: margin and horizon, the same multiplier everywhere
# ---------------------------------------------------------------------------
def test_margin_and_horizon_apply_to_the_cut_and_to_the_reported_value_alike() -> None:
    uplift = np.array([0.10, 0.03])
    policy = UpliftPolicyConfig(
        cost_per_contact=1.0, value_per_conversion=50.0, margin_pct=30.0, horizon_months=2
    )
    # unit = 50 × 0.30 × 2 = 30: row 0 earns 3.0 ≥ 1; row 1 earns 0.9 < 1 (it would pay at 100 % margin).
    observed = HoldoutUplift(*synthetic_holdout(4), samples=20, seed=4)
    recommendation, chosen = recommend_policy(
        uplift,
        segs(P, P),
        policy,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed_top_share=observed.at,
    )
    assert chosen.tolist() == [True, False]
    expected = recommendation.expected_incremental_conversions
    assert expected is not None and recommendation.expected_value == expected.value * 30.0
    curve = profit_curve(
        uplift, segs(P, P), policy, run_id=RUN_ID, computed_on="test", causal=True, observed=observed
    )
    assert curve.max_contacts == 1
    assert curve.configured.expected_value == recommendation.expected_value
    assert curve.value_basis == "₹50 per conversion × 30% margin × 2 months"


def test_value_basis_uses_the_repository_inr_format() -> None:
    curve = profit_curve(
        np.array([0.1]),
        segs(P),
        UpliftPolicyConfig(cost_per_contact=1.0, value_per_conversion=150000.0),
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
    )
    assert curve.value_basis == "₹1,50,000 (1.50 lakh) per conversion"
    assert not curve.value_weighted


# ---------------------------------------------------------------------------
# Review finding 6 (defaults): a configuration without M97's settings is main's arithmetic exactly
# ---------------------------------------------------------------------------
def _main_reference(
    uplift: np.ndarray,
    segments: np.ndarray,
    policy: UpliftPolicyConfig,
    eligible: np.ndarray,
    tiebreak: np.ndarray,
    lookup: HoldoutUplift,
) -> dict[str, object]:
    """`engine/uplift/policy.py` as it was on `main` before M97, transcribed: uplift ranking, the cut
    `uplift × value < cost`, `contacts × cost`, `conversions × value`."""
    value, cost = policy.value_per_conversion, policy.cost_per_contact
    order = np.asarray(np.lexsort((tiebreak, -uplift)), dtype=np.int64)
    is_candidate = (segments == P.value) & eligible
    positions = np.flatnonzero(is_candidate[order])
    ranked = order[positions]
    cut = (
        np.zeros(len(ranked), dtype=bool) if value is None or cost is None else uplift[ranked] * value < cost
    )
    paying = int(np.argmax(cut)) if cut.any() else len(ranked)
    budget = policy.budget_contacts
    take = min(len(ranked), paying, len(ranked) if budget is None else budget)
    depth = int(positions[take - 1]) + 1 if take else 0
    observed = (
        None
        if not take
        else bootstrap_uplift_at(
            lookup.pred, lookup.t, lookup.y, depth / len(uplift), samples=lookup.samples, seed=lookup.seed
        )
    )
    expected = (
        ConfidenceValue(value=0.0, ci_low=0.0, ci_high=0.0)
        if not take
        else (
            None
            if observed is None
            else ConfidenceValue(
                value=take * observed.value,
                ci_low=None if observed.ci_low is None else take * observed.ci_low,
                ci_high=None if observed.ci_high is None else take * observed.ci_high,
                confidence_level=observed.confidence_level,
            )
        )
    )
    expected_cost = None if cost is None else take * cost
    expected_value = None if expected is None or value is None else expected.value * value
    net = None if expected_value is None or expected_cost is None else expected_value - expected_cost
    low = None
    if (
        expected is not None
        and expected.ci_low is not None
        and value is not None
        and expected_cost is not None
    ):
        low = expected.ci_low * value - expected_cost
    return {
        "contacts": take,
        "expected": expected,
        "cost": expected_cost,
        "value": expected_value,
        "net": net,
        "low": low,
    }


@pytest.mark.parametrize("seed", range(10))
def test_a_configuration_without_m97_settings_is_mains_arithmetic_exactly(seed: int) -> None:
    rng = np.random.default_rng(seed + 500)
    rows = int(rng.integers(40, 300))
    uplift = np.round(rng.normal(0.02, 0.06, rows), int(rng.integers(2, 5)))
    segments = np.where(uplift >= 0.02, P, np.where(uplift <= -0.01, D, L)).astype(object)
    eligible = rng.random(rows) < 0.8
    tiebreak = rng.integers(0, 2**62, rows).astype(np.uint64)
    money = seed % 3 != 0
    policy = UpliftPolicyConfig(
        budget_contacts=int(rng.integers(1, rows)) if seed % 2 else None,
        cost_per_contact=float(rng.uniform(0.5, 3.0)) if money else None,
        value_per_conversion=float(rng.uniform(20.0, 80.0)) if money else None,
    )
    lookup = HoldoutUplift(*synthetic_holdout(seed, 600), samples=30, seed=seed)
    recommendation, _ = recommend_policy(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=lookup.at,
        eligible=eligible,
        tiebreak=tiebreak,
    )
    reference = _main_reference(uplift, segments, policy, eligible, tiebreak, lookup)
    assert recommendation.contacts_recommended == reference["contacts"]
    assert recommendation.expected_incremental_conversions == reference["expected"]
    assert recommendation.expected_cost == reference["cost"]  # `==`: the same floats, not approx
    assert recommendation.expected_value == reference["value"]
    assert recommendation.expected_net_value == reference["net"]
    assert recommendation.net_value_low == reference["low"]
    assert recommendation.money_note is None and recommendation.values_missing is None
