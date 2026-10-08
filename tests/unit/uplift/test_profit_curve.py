"""`engine.uplift.policy.profit_curve`: the targeting recommendation replayed at every budget.

The one property that matters most is pinned first: the point at the configured budget is the
recommendation itself, field for field, on random rankings with and without eligibility masks,
tie-breaks, budgets and money settings. Then the rules the curve shares with it (only eligible
persuadables, never a sleeping dog, nobody below cost), and the optimum on a case worked by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

import numpy as np
import pytest

from engine.uplift.config import UpliftPolicyConfig
from engine.uplift.contracts import ConfidenceValue, PolicyStopReason, Segment
from engine.uplift.metrics import HoldoutUplift, bootstrap_uplift_at, uplift_at_fraction
from engine.uplift.policy import (
    BANDS_NOTE,
    NO_HOLDOUT_NOTE,
    NO_MONEY_NOTE,
    profit_curve,
    recommend_policy,
)

RUN_ID = "r_20260930_bbbbbbbb"

P = Segment.PERSUADABLE
S = Segment.SURE_THING
L = Segment.LOST_CAUSE
D = Segment.SLEEPING_DOG


def segs(*items: Segment) -> np.ndarray:
    array = np.empty(len(items), dtype=object)
    array[:] = list(items)
    return array


def holdout(seed: int, rows: int = 600, samples: int = 40) -> HoldoutUplift:
    """A randomised hold-out whose top rows respond more to treatment: a real, noisy lookup."""
    rng = np.random.default_rng(seed)
    pred = rng.normal(0.02, 0.05, rows)
    t = rng.integers(0, 2, rows).astype(np.int64)
    y = (rng.random(rows) < 0.2 + t * np.clip(pred, 0.0, None) * 3).astype(np.int64)
    return HoldoutUplift(pred=pred, t=t, y=y, samples=samples, seed=seed)


class Linear:
    """A fake hold-out whose observed uplift falls linearly with the share: `0.5 − 0.5·share`.

    Its interval is the point ± 0.1, so every band can be checked by hand.
    """

    def __init__(self) -> None:
        self.asked: list[float] = []

    def points(self, fractions: np.ndarray) -> np.ndarray:
        return 0.5 - 0.5 * np.asarray(fractions, dtype=np.float64)

    def intervals(self, fractions: Sequence[float]) -> list[ConfidenceValue | None]:
        self.asked.extend(fractions)
        return [
            ConfidenceValue(value=v, ci_low=v - 0.1, ci_high=v + 0.1)
            for v in (0.5 - 0.5 * f for f in fractions)
        ]


# ---------------------------------------------------------------------------
# The configured point IS the recommendation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(12))
def test_the_configured_point_equals_the_recommendation_exactly(seed: int) -> None:
    rng = np.random.default_rng(seed)
    rows = int(rng.integers(30, 400))
    uplift = np.round(rng.normal(0.02, 0.06, rows), int(rng.integers(2, 5)))  # rounding makes ties
    segments = np.empty(rows, dtype=object)
    segments[:] = [
        P if u >= 0.02 else D if u <= -0.01 else (S if rng.random() < 0.5 else L) for u in uplift.tolist()
    ]
    eligible = rng.random(rows) < 0.8 if seed % 2 else None
    tiebreak = rng.integers(0, 2**62, rows).astype(np.uint64) if seed % 3 else None
    budget = int(rng.integers(1, rows)) if seed % 4 else None
    money = seed % 5 != 0
    policy = UpliftPolicyConfig(
        budget_contacts=budget,
        cost_per_contact=float(rng.uniform(0.5, 3.0)) if money else None,
        value_per_conversion=float(rng.uniform(20.0, 80.0)) if money else None,
    )
    lookup = holdout(seed)
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
    curve = profit_curve(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed=lookup,
        eligible=eligible,
        tiebreak=tiebreak,
    )
    point = curve.configured
    assert point.contacts == recommendation.contacts_recommended
    assert point.predicted_incremental_conversions == recommendation.predicted_incremental_conversions
    assert point.expected_incremental_conversions == recommendation.expected_incremental_conversions
    assert point.expected_cost == recommendation.expected_cost
    assert point.expected_value == recommendation.expected_value
    assert point.expected_net_value == recommendation.expected_net_value
    assert point.net_value_low == recommendation.net_value_low
    assert point.net_value_high == recommendation.net_value_high
    assert curve.configured_stop_reason is recommendation.stop_reason
    assert curve.eligible_persuadables == recommendation.eligible_persuadables
    assert curve.rows == recommendation.rows
    assert point in curve.points


@pytest.mark.parametrize("seed", range(6))
def test_the_configured_point_equals_the_recommendation_with_value_column(seed: int) -> None:
    rng = np.random.default_rng(seed + 100)
    rows = int(rng.integers(50, 200))
    uplift = np.round(rng.normal(0.02, 0.06, rows), 3)
    segments = np.empty(rows, dtype=object)
    segments[:] = [
        P if u >= 0.02 else D if u <= -0.01 else (S if rng.random() < 0.5 else L) for u in uplift.tolist()
    ]
    eligible = rng.random(rows) < 0.8
    tiebreak = rng.integers(0, 2**62, rows).astype(np.uint64)
    budget = int(rng.integers(1, rows)) if seed % 2 else None
    values = rng.uniform(10.0, 500.0, rows)
    policy = UpliftPolicyConfig(
        budget_contacts=budget,
        cost_per_contact=float(rng.uniform(0.5, 3.0)),
        value_column="customer_value",
        margin_pct=75.0,
        min_roi=0.1,
    )
    lookup = holdout(seed + 100)
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
        values=values,
    )
    curve = profit_curve(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed=lookup,
        eligible=eligible,
        tiebreak=tiebreak,
        values=values,
    )
    point = curve.configured
    assert point.contacts == recommendation.contacts_recommended
    assert point.predicted_incremental_conversions == recommendation.predicted_incremental_conversions
    assert point.expected_incremental_conversions == recommendation.expected_incremental_conversions
    assert point.expected_cost == recommendation.expected_cost
    assert point.expected_value == recommendation.expected_value
    assert point.expected_net_value == recommendation.expected_net_value
    assert point.net_value_low == recommendation.net_value_low
    assert point.net_value_high == recommendation.net_value_high
    assert curve.configured_stop_reason is recommendation.stop_reason
    assert curve.eligible_persuadables == recommendation.eligible_persuadables
    assert curve.rows == recommendation.rows
    assert point in curve.points


# Plan J M97 (DEC-1307): the identity in every money configuration a run can have. `offer` prices each
# contact as `contact + offer_cost × p_treated` (per-row costs); `yaml` takes the contact cost from
# `configs/pilot/value.yaml`; `missing` leaves some values out (counted at zero); `too_many` leaves out more
# than the limit (no money, a reason); `no_holdout_values` is a model whose hold-out has no values.
MONEY_CONFIGURATIONS = (
    "scalar_margin_horizon_min_roi",
    "value_with_cost",
    "value_yaml_cost",
    "value_offer_cost",
    "value_missing",
    "value_too_many_missing",
    "value_no_holdout_values",
)


@pytest.mark.parametrize("seed", range(3))
@pytest.mark.parametrize("configuration", MONEY_CONFIGURATIONS)
def test_the_configured_point_equals_the_recommendation_in_every_money_configuration(
    configuration: str, seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.pilot.roi import ValueCosts
    from engine.uplift.policy import customer_net_values, holdout_lookups

    if configuration == "value_offer_cost":
        monkeypatch.setattr(
            "engine.pilot.roi.lookup_value_costs",
            lambda *_a, **_k: ValueCosts(offer_cost=3.0, contact_cost=0.4),
        )
    rng = np.random.default_rng(seed + 700)
    rows = int(rng.integers(60, 300))
    uplift = np.round(rng.normal(0.03, 0.06, rows), 3)
    segments = np.empty(rows, dtype=object)
    segments[:] = [
        P if u >= 0.02 else D if u <= -0.01 else (S if rng.random() < 0.5 else L) for u in uplift.tolist()
    ]
    eligible = rng.random(rows) < 0.85
    tiebreak = rng.integers(0, 2**62, rows).astype(np.uint64)
    p_treated = rng.uniform(0.05, 0.6, rows)
    budget = int(rng.integers(1, rows)) if seed % 2 else None
    values: np.ndarray | None = rng.uniform(20.0, 400.0, rows)
    assert values is not None
    if configuration == "value_missing":
        values[rng.choice(rows, size=3, replace=False)] = np.nan  # under the 10 % limit
    if configuration == "value_too_many_missing":
        values[rng.random(rows) < 0.4] = np.nan
    if configuration == "scalar_margin_horizon_min_roi":
        values = None
        policy = UpliftPolicyConfig(
            budget_contacts=budget,
            cost_per_contact=float(rng.uniform(0.5, 2.0)),
            value_per_conversion=float(rng.uniform(20.0, 80.0)),
            margin_pct=40.0,
            horizon_months=3,
            min_roi=0.2,
        )
    else:
        policy = UpliftPolicyConfig(
            budget_contacts=budget,
            cost_per_contact=None if configuration in {"value_yaml_cost", "value_offer_cost"} else 1.5,
            value_column="customer_value",
            margin_pct=60.0,
            horizon_months=2,
            min_roi=0.1,
        )
    hold = np.random.default_rng(seed + 900)
    hold_rows = 600
    hold_pred = hold.normal(0.03, 0.05, hold_rows)
    hold_t = hold.integers(0, 2, hold_rows).astype(np.int64)
    hold_y = (hold.random(hold_rows) < 0.2 + hold_t * np.clip(hold_pred, 0.0, None) * 3).astype(np.int64)
    hold_values = hold.uniform(20.0, 400.0, hold_rows)
    if configuration in {"value_missing", "value_too_many_missing"}:
        hold_values[
            hold.choice(hold_rows, size=30 if configuration == "value_missing" else 240, replace=False)
        ] = np.nan
    lookups = holdout_lookups(
        hold_pred,
        hold_t,
        hold_y,
        policy=policy,
        samples=30,
        seed=seed,
        ranked_by_value=values is not None,
        values=None if configuration == "value_no_holdout_values" else hold_values,
        p_treated=hold.uniform(0.05, 0.6, hold_rows),
    )
    money = customer_net_values(uplift, policy, values=values, p_treated=p_treated)
    recommendation, _ = recommend_policy(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=None if lookups.conversions is None else lookups.conversions.at,
        observed_top_value=None if lookups.value is None else lookups.value.at,
        holdout_note=lookups.note,
        eligible=eligible,
        tiebreak=tiebreak,
        money=money,
    )
    curve = profit_curve(
        uplift,
        segments,
        policy,
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed=lookups.conversions,
        observed_value=lookups.value,
        holdout_note=lookups.note,
        eligible=eligible,
        tiebreak=tiebreak,
        values=values,
        p_treated=p_treated,
    )
    point = curve.configured
    assert point.contacts == recommendation.contacts_recommended
    assert point.predicted_incremental_conversions == recommendation.predicted_incremental_conversions
    assert point.expected_incremental_conversions == recommendation.expected_incremental_conversions
    assert point.expected_cost == recommendation.expected_cost
    assert point.expected_value == recommendation.expected_value
    assert point.expected_net_value == recommendation.expected_net_value
    assert point.net_value_low == recommendation.net_value_low
    assert point.net_value_high == recommendation.net_value_high
    assert curve.configured_stop_reason is recommendation.stop_reason
    assert curve.eligible_persuadables == recommendation.eligible_persuadables
    assert curve.rows == recommendation.rows
    assert curve.money_note == recommendation.money_note
    assert curve.values_missing == recommendation.values_missing
    assert curve.value_weighted is (values is not None)
    assert point in curve.points
    # The money is there exactly when every number it is made of is.
    priced = configuration in {
        "scalar_margin_horizon_min_roi",
        "value_with_cost",
        "value_yaml_cost",
        "value_offer_cost",
        "value_missing",
    }
    assert (recommendation.expected_value is not None) is priced
    if not priced:
        assert recommendation.money_note and curve.optimum is None and curve.optimum_note
    # No sleeping dog, at any budget.
    assert curve.optimum is None or curve.optimum in curve.points


def test_without_a_lookup_the_configured_point_has_no_expectation_like_the_recommendation() -> None:
    uplift = np.array([0.3, 0.2, 0.1, -0.2])
    segments = segs(P, P, P, D)
    policy = UpliftPolicyConfig(budget_contacts=2, cost_per_contact=1.0, value_per_conversion=10.0)
    recommendation, _ = recommend_policy(
        uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True
    )
    curve = profit_curve(uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True)
    assert curve.configured.expected_incremental_conversions is None
    assert recommendation.expected_incremental_conversions is None
    assert curve.configured.expected_cost == recommendation.expected_cost == 2.0
    assert curve.configured.expected_net_value is None and curve.configured.roi is None
    assert curve.optimum is None and curve.optimum_note == NO_HOLDOUT_NOTE
    assert not curve.bands_available and curve.bands_note == NO_HOLDOUT_NOTE


# ---------------------------------------------------------------------------
# The shape of the curve and the rules it shares with the recommendation
# ---------------------------------------------------------------------------
def test_contacts_rise_from_zero_to_every_contact_the_rules_allow() -> None:
    rng = np.random.default_rng(3)
    uplift = rng.normal(0.05, 0.05, 500)
    segments = np.where(uplift >= 0.02, P, np.where(uplift <= -0.01, D, L)).astype(object)
    curve = profit_curve(
        uplift,
        segments,
        UpliftPolicyConfig(budget_contacts=100),
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
    )
    contacts = [point.contacts for point in curve.points]
    assert contacts[0] == 0 and contacts[-1] == curve.max_contacts
    assert all(a < b for a, b in pairwise(contacts))
    assert curve.max_contacts == int((segments == P).sum())
    assert curve.max_contacts_reason is PolicyStopReason.ALL_PERSUADABLES
    assert curve.configured.contacts == 100
    assert 100 in contacts
    depths = [point.ranking_depth for point in curve.points]
    assert all(a < b for a, b in pairwise(depths))
    # The model's own sum only grows: every persuadable's predicted uplift is positive.
    predicted = [point.predicted_incremental_conversions for point in curve.points]
    assert all(a < b for a, b in pairwise(predicted))


def test_sleeping_dogs_are_never_contacted_at_any_budget() -> None:
    # The sleeping dog is mislabelled with the highest uplift, so the ranking puts it first.
    uplift = np.array([0.9, 0.3, 0.2, -0.4, 0.1])
    segments = segs(D, P, P, D, P)
    curve = profit_curve(
        uplift, segments, UpliftPolicyConfig(), run_id=RUN_ID, computed_on="test", causal=True
    )
    assert curve.max_contacts == 3
    by_contacts = {point.contacts: point for point in curve.points}
    assert set(by_contacts) == {0, 1, 2, 3}
    # One contact reaches position 2 of the ranking: the dog at position 1 is passed over.
    assert by_contacts[1].ranking_depth == 2
    assert by_contacts[3].predicted_incremental_conversions == pytest.approx(0.6)


def test_ineligible_rows_are_skipped_and_below_cost_rows_end_the_curve() -> None:
    uplift = np.array([0.40, 0.30, 0.20, 0.10, 0.05])
    segments = segs(P, P, P, P, P)
    eligible = np.array([True, False, True, True, True])
    # 100 × uplift against a cost of 8: 0.40, 0.20, 0.10 pay; 0.05 does not.
    policy = UpliftPolicyConfig(cost_per_contact=8.0, value_per_conversion=100.0)
    curve = profit_curve(
        uplift, segments, policy, run_id=RUN_ID, computed_on="scored", causal=True, eligible=eligible
    )
    assert curve.eligible_persuadables == 4
    assert curve.max_contacts == 3
    assert curve.max_contacts_reason is PolicyStopReason.VALUE_BELOW_COST
    assert [point.ranking_depth for point in curve.points] == [0, 1, 3, 4]


def test_no_persuadable_is_a_single_point_at_zero() -> None:
    curve = profit_curve(
        np.array([0.0, -0.3]),
        segs(L, D),
        UpliftPolicyConfig(),
        run_id=RUN_ID,
        computed_on="test",
        causal=False,
    )
    assert [point.contacts for point in curve.points] == [0]
    assert curve.max_contacts_reason is PolicyStopReason.NO_PERSUADABLES
    assert curve.causal is False


# ---------------------------------------------------------------------------
# The optimum, the band and the ROI on a case worked by hand
# ---------------------------------------------------------------------------
def test_the_optimum_on_a_hand_built_case() -> None:
    # Ten persuadables, best first; contacting c of them reaches share c/10, whose observed uplift is
    # 0.5 − 0.05c. Net value = c × (0.5 − 0.05c) × 10 − 0.6c = 4.4c − 0.5c²: 9.6 at c = 4, 9.5 at 5.
    uplift = np.linspace(0.5, 0.05, 10)
    segments = segs(*([P] * 10))
    policy = UpliftPolicyConfig(budget_contacts=7, cost_per_contact=0.6, value_per_conversion=10.0)
    lookup = Linear()
    # Two plotted points only (0 and the last row that pays, 9: 0.05 × 10 < 0.6): the optimum is
    # searched over every count, not the grid, and joins it.
    curve = profit_curve(
        uplift, segments, policy, run_id=RUN_ID, computed_on="test", causal=True, observed=lookup, points=2
    )
    assert curve.max_contacts == 9 and curve.max_contacts_reason is PolicyStopReason.VALUE_BELOW_COST
    assert [point.contacts for point in curve.points] == [0, 4, 7, 9]
    optimum = curve.optimum
    assert optimum is not None and curve.optimum_note is None
    assert optimum.contacts == 4
    assert optimum.expected_net_value == pytest.approx(9.6)
    assert optimum.expected_cost == pytest.approx(2.4)
    assert optimum.roi == pytest.approx(4.0)
    # The band: conversions 4 × (0.3 ± 0.1) = 0.8 to 1.6, so net 8 − 2.4 = 5.6 to 16 − 2.4 = 13.6.
    assert optimum.net_value_low == pytest.approx(5.6)
    assert optimum.net_value_high == pytest.approx(13.6)
    assert curve.bands_available and curve.bands_note == BANDS_NOTE
    # The configured budget of 7: 4.4 × 7 − 0.5 × 49 = 6.3.
    assert curve.configured.contacts == 7
    assert curve.configured.expected_net_value == pytest.approx(6.3)
    # Nobody contacted: exactly nothing, and no ROI on no spend.
    zero = curve.points[0]
    assert zero.expected_incremental_conversions == ConfidenceValue(value=0.0, ci_low=0.0, ci_high=0.0)
    assert zero.expected_net_value == 0.0 and zero.roi is None
    # The hold-out was asked one question per plotted contact count, about the depth each reaches.
    assert lookup.asked == pytest.approx([0.4, 0.7, 0.9])


class Flat(Linear):
    """A fake hold-out that observes the same uplift, `level`, in every share."""

    def __init__(self, level: float) -> None:
        super().__init__()
        self.level = level

    def points(self, fractions: np.ndarray) -> np.ndarray:
        return np.full(len(fractions), self.level)

    def intervals(self, fractions: Sequence[float]) -> list[ConfidenceValue | None]:
        return [ConfidenceValue(value=self.level) for _ in fractions]


def test_when_every_budget_loses_money_the_optimum_is_nobody() -> None:
    # The model says every row pays (0.5 × 10 ≥ 1), but the hold-out observed 0.05: c × 0.5 − c < 0.
    policy = UpliftPolicyConfig(cost_per_contact=1.0, value_per_conversion=10.0)
    curve = profit_curve(
        np.full(10, 0.5),
        segs(*([P] * 10)),
        policy,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed=Flat(0.05),
    )
    assert curve.max_contacts == 10
    assert curve.optimum is not None and curve.optimum.contacts == 0
    assert curve.points[-1].expected_net_value == pytest.approx(-5.0)
    # The flat fake has no interval, so there is no band - and the curve says so.
    assert not curve.bands_available and curve.bands_note != BANDS_NOTE


def test_the_optimum_prefers_fewer_contacts_on_a_tie() -> None:
    # Net value = c × 0.125 × 8 − c × 1 = 0 exactly at every c: contacting nobody is as good and cheaper.
    policy = UpliftPolicyConfig(cost_per_contact=1.0, value_per_conversion=8.0)
    curve = profit_curve(
        np.full(5, 0.125),
        segs(*([P] * 5)),
        policy,
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed=Flat(0.125),
    )
    assert curve.max_contacts == 5
    assert curve.optimum is not None and curve.optimum.contacts == 0


def test_without_both_money_settings_there_is_no_optimum_and_no_band() -> None:
    uplift = np.linspace(0.5, 0.05, 10)
    curve = profit_curve(
        uplift,
        segs(*([P] * 10)),
        UpliftPolicyConfig(value_per_conversion=10.0),
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed=Linear(),
    )
    assert curve.optimum is None and curve.optimum_note == NO_MONEY_NOTE
    assert not curve.bands_available
    assert all(point.expected_net_value is None and point.roi is None for point in curve.points)
    # Conversions and value are still there: they need no cost.
    assert curve.points[-1].expected_value is not None


def test_overridden_is_recorded_as_given() -> None:
    curve = profit_curve(
        np.array([0.1]),
        segs(P),
        UpliftPolicyConfig(),
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        overridden=True,
    )
    assert curve.overridden is True


def test_fewer_than_two_points_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 2 points"):
        profit_curve(
            np.array([0.1]),
            segs(P),
            UpliftPolicyConfig(),
            run_id=RUN_ID,
            computed_on="test",
            causal=True,
            points=1,
        )


# ---------------------------------------------------------------------------
# HoldoutUplift: many shares, the same numbers as one at a time
# ---------------------------------------------------------------------------
def test_holdout_intervals_equal_one_bootstrap_per_share() -> None:
    lookup = holdout(5, rows=300, samples=30)
    fractions = [0.004, 0.05, 0.1, 0.37, 0.5, 1.0]
    many = lookup.intervals(fractions)
    for fraction, interval in zip(fractions, many, strict=True):
        single = bootstrap_uplift_at(lookup.pred, lookup.t, lookup.y, fraction, samples=30, seed=5)
        assert interval == single
        assert lookup.at(fraction) == single
    points = lookup.points(np.asarray(fractions))
    for fraction, point in zip(fractions, points.tolist(), strict=True):
        expected = uplift_at_fraction(lookup.pred, lookup.t, lookup.y, fraction)
        if expected is None:
            assert np.isnan(point)
        else:
            assert point == expected
