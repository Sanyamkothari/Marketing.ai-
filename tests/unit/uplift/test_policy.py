"""`engine.uplift.policy`: who is chosen, how many, why no more, and what it is expected to earn.

Small hand-written rankings pin the rules; a hypothesis property checks, over random predictions,
segments, masks and settings, that a sleeping dog is never chosen and the choice is always the top
of the eligible persuadables within budget.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.uplift.config import UpliftPolicyConfig
from engine.uplift.contracts import ConfidenceValue, PolicyStopReason, Segment, SegmentThresholds
from engine.uplift.policy import below_cost, choose_contacts, rank_positions, ranking, recommend_policy

RUN_ID = "r_20260921_aaaaaaaa"

P = Segment.PERSUADABLE
S = Segment.SURE_THING
L = Segment.LOST_CAUSE
D = Segment.SLEEPING_DOG


def segs(*items: Segment) -> np.ndarray:
    array = np.empty(len(items), dtype=object)
    array[:] = list(items)
    return array


# Rows 0..7; persuadables are rows 1, 2, 4, 6 with uplifts 0.10, 0.30, 0.05, 0.20.
UPLIFT = np.array([0.0, 0.10, 0.30, -0.20, 0.05, 0.01, 0.20, -0.50])
SEGMENTS = segs(L, P, P, D, P, S, P, D)


def chosen(mask: np.ndarray) -> list[int]:
    return np.flatnonzero(mask).tolist()


# ---------------------------------------------------------------------------
# choose_contacts
# ---------------------------------------------------------------------------
def test_without_limits_every_persuadable_and_nothing_else_is_chosen() -> None:
    selected, reason = choose_contacts(UPLIFT, SEGMENTS, UpliftPolicyConfig())
    assert chosen(selected) == [1, 2, 4, 6]
    assert reason is PolicyStopReason.ALL_PERSUADABLES
    assert selected.dtype == np.bool_


def test_the_budget_takes_the_highest_uplift_first() -> None:
    selected, reason = choose_contacts(UPLIFT, SEGMENTS, UpliftPolicyConfig(budget_contacts=2))
    assert chosen(selected) == [2, 6]  # uplift 0.30 and 0.20
    assert reason is PolicyStopReason.BUDGET


def test_a_budget_at_least_the_candidate_count_takes_them_all() -> None:
    selected, reason = choose_contacts(UPLIFT, SEGMENTS, UpliftPolicyConfig(budget_contacts=4))
    assert chosen(selected) == [1, 2, 4, 6]
    assert reason is PolicyStopReason.ALL_PERSUADABLES


def test_the_value_cost_rule_stops_before_the_first_row_that_does_not_pay() -> None:
    # 100 × uplift against a cost of 8: 30, 20, 10 pay; 5 does not.
    policy = UpliftPolicyConfig(value_per_conversion=100.0, cost_per_contact=8.0)
    selected, reason = choose_contacts(UPLIFT, SEGMENTS, policy)
    assert chosen(selected) == [1, 2, 6]
    assert reason is PolicyStopReason.VALUE_BELOW_COST


def test_a_row_that_exactly_pays_for_itself_is_kept() -> None:
    policy = UpliftPolicyConfig(value_per_conversion=8.0, cost_per_contact=1.0)
    selected, reason = choose_contacts(np.array([0.25, 0.125, 0.0625]), segs(P, P, P), policy)
    assert chosen(selected) == [0, 1]  # 0.125 × 8 = 1.0: not below a cost of 1; 0.0625 × 8 is
    assert reason is PolicyStopReason.VALUE_BELOW_COST


def test_value_or_cost_alone_does_not_cut() -> None:
    for policy in (UpliftPolicyConfig(value_per_conversion=1.0), UpliftPolicyConfig(cost_per_contact=99.0)):
        selected, reason = choose_contacts(UPLIFT, SEGMENTS, policy)
        assert chosen(selected) == [1, 2, 4, 6]
        assert reason is PolicyStopReason.ALL_PERSUADABLES


def test_when_budget_and_cost_end_at_the_same_row_the_cost_is_the_reason() -> None:
    policy = UpliftPolicyConfig(budget_contacts=3, value_per_conversion=100.0, cost_per_contact=8.0)
    selected, reason = choose_contacts(UPLIFT, SEGMENTS, policy)
    assert chosen(selected) == [1, 2, 6]
    assert reason is PolicyStopReason.VALUE_BELOW_COST


def test_a_tighter_budget_than_the_cost_cut_is_the_reason() -> None:
    policy = UpliftPolicyConfig(budget_contacts=1, value_per_conversion=100.0, cost_per_contact=8.0)
    selected, reason = choose_contacts(UPLIFT, SEGMENTS, policy)
    assert chosen(selected) == [2]
    assert reason is PolicyStopReason.BUDGET


def test_when_even_the_best_persuadable_does_not_pay_nobody_is_chosen() -> None:
    policy = UpliftPolicyConfig(value_per_conversion=10.0, cost_per_contact=50.0)
    selected, reason = choose_contacts(UPLIFT, SEGMENTS, policy)
    assert chosen(selected) == []
    assert reason is PolicyStopReason.VALUE_BELOW_COST


def test_no_persuadable_means_no_contact() -> None:
    selected, reason = choose_contacts(np.array([0.0, -0.3]), segs(S, D), UpliftPolicyConfig())
    assert chosen(selected) == []
    assert reason is PolicyStopReason.NO_PERSUADABLES


def test_ineligible_rows_are_never_chosen() -> None:
    eligible = np.array([True, True, False, True, True, True, True, True])
    selected, reason = choose_contacts(
        UPLIFT, SEGMENTS, UpliftPolicyConfig(budget_contacts=2), eligible=eligible
    )
    assert chosen(selected) == [1, 6]  # row 2 (the best) is not eligible
    assert reason is PolicyStopReason.BUDGET


def test_persuadables_that_are_all_ineligible_count_as_none() -> None:
    eligible = np.array([True, False, False, True, False, True, False, True])
    selected, reason = choose_contacts(UPLIFT, SEGMENTS, UpliftPolicyConfig(), eligible=eligible)
    assert chosen(selected) == []
    assert reason is PolicyStopReason.NO_PERSUADABLES


def test_ties_keep_input_order() -> None:
    selected, _ = choose_contacts(
        np.array([0.1, 0.1, 0.1, 0.1]), segs(P, P, P, P), UpliftPolicyConfig(budget_contacts=2)
    )
    assert chosen(selected) == [0, 1]


def test_sleeping_dogs_are_never_chosen_even_with_a_huge_budget_and_free_contacts() -> None:
    policy = UpliftPolicyConfig(budget_contacts=10**9, cost_per_contact=0.0, value_per_conversion=10**6)
    uplift = np.array([-0.5, -0.02, -0.01, 0.3])
    selected, _ = choose_contacts(uplift, segs(D, D, D, P), policy)
    assert chosen(selected) == [3]


def test_a_sleeping_dog_is_not_chosen_even_if_a_caller_mislabels_its_uplift() -> None:
    # Only the segment decides eligibility for treatment: a high uplift on a row segmented as a
    # sleeping dog (say, by an older model card's cuts) still never buys it a contact.
    selected, _ = choose_contacts(np.array([0.9, 0.1]), segs(D, P), UpliftPolicyConfig(budget_contacts=5))
    assert chosen(selected) == [1]


@pytest.mark.parametrize(
    ("eligible", "message"),
    [
        (np.array([True, False]), "eligible must be a one-dimensional mask"),
        (np.array([1, 0, 1, 1, 1, 1, 1, 1]), "boolean"),
    ],
)
def test_a_malformed_eligibility_mask_is_refused(eligible: np.ndarray, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        choose_contacts(UPLIFT, SEGMENTS, UpliftPolicyConfig(), eligible=eligible)


def test_a_missing_uplift_is_refused() -> None:
    with pytest.raises(ValueError, match="1 of 2 rows"):
        choose_contacts(np.array([np.nan, 0.1]), segs(P, P), UpliftPolicyConfig())


def test_below_cost_needs_both_value_and_cost() -> None:
    assert below_cost(UPLIFT, UpliftPolicyConfig(cost_per_contact=5.0)).tolist() == [False] * 8
    cut = below_cost(
        np.array([0.1, 0.05]), UpliftPolicyConfig(cost_per_contact=6.0, value_per_conversion=100.0)
    )
    assert cut.tolist() == [False, True]


segment_strategy = st.sampled_from([P, S, L, D])


@settings(max_examples=200, deadline=None)
@given(
    rows=st.lists(
        st.tuples(st.floats(-1.0, 1.0, allow_nan=False), segment_strategy, st.booleans()),
        min_size=0,
        max_size=40,
    ),
    budget=st.one_of(st.none(), st.integers(1, 50)),
    cost=st.one_of(st.none(), st.floats(0.0, 100.0, allow_nan=False)),
    value=st.one_of(st.none(), st.floats(0.0, 1000.0, allow_nan=False)),
)
def test_the_choice_is_always_the_top_eligible_persuadables_and_never_a_sleeping_dog(
    rows: list[tuple[float, Segment, bool]],
    budget: int | None,
    cost: float | None,
    value: float | None,
) -> None:
    uplift = np.array([row[0] for row in rows], dtype=np.float64)
    segments = segs(*(row[1] for row in rows))
    eligible = np.array([row[2] for row in rows], dtype=bool)
    policy = UpliftPolicyConfig(budget_contacts=budget, cost_per_contact=cost, value_per_conversion=value)
    selected, reason = choose_contacts(uplift, segments, policy, eligible=eligible)

    is_persuadable = np.array([segment is P for segment in segments.tolist()], dtype=bool)
    is_sleeping = np.array([segment is D for segment in segments.tolist()], dtype=bool)
    assert not (selected & is_sleeping).any()
    assert not (selected & ~(is_persuadable & eligible)).any()
    if budget is not None:
        assert int(selected.sum()) <= budget
    if cost is not None and value is not None:
        assert (uplift[selected] * value >= cost).all()
    passed_over = is_persuadable & eligible & ~selected
    if selected.any() and passed_over.any():
        assert uplift[selected].min() >= uplift[passed_over].max()
    if not (is_persuadable & eligible).any():
        assert reason is PolicyStopReason.NO_PERSUADABLES
    elif not passed_over.any():
        assert reason is PolicyStopReason.ALL_PERSUADABLES


# ---------------------------------------------------------------------------
# recommend_policy
# ---------------------------------------------------------------------------
def test_the_recommendation_reports_n_and_the_models_own_sum() -> None:
    recommendation, selected = recommend_policy(
        UPLIFT,
        SEGMENTS,
        UpliftPolicyConfig(budget_contacts=2),
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
    )
    assert chosen(selected) == [2, 6]
    assert recommendation.run_id == RUN_ID
    assert recommendation.computed_on == "test"
    assert recommendation.rows == 8
    assert recommendation.eligible_persuadables == 4
    assert recommendation.contacts_recommended == 2
    assert recommendation.stop_reason is PolicyStopReason.BUDGET
    assert recommendation.budget_contacts == 2
    assert recommendation.predicted_incremental_conversions == pytest.approx(0.50)
    assert recommendation.causal is True


def test_without_a_measured_hold_out_nothing_is_invented() -> None:
    policy = UpliftPolicyConfig(budget_contacts=2, cost_per_contact=3.0, value_per_conversion=50.0)
    recommendation, _ = recommend_policy(
        UPLIFT, SEGMENTS, policy, run_id=RUN_ID, computed_on="scored", causal=False
    )
    assert recommendation.expected_incremental_conversions is None
    assert recommendation.expected_cost == pytest.approx(6.0)
    assert recommendation.expected_value is None
    assert recommendation.expected_net_value is None
    assert recommendation.causal is False


def test_expected_conversions_are_n_times_the_observed_top_share_uplift() -> None:
    calls: list[float] = []

    def observed(fraction: float) -> ConfidenceValue | None:
        calls.append(fraction)
        return ConfidenceValue(value=0.15, ci_low=0.05, ci_high=0.25)

    policy = UpliftPolicyConfig(budget_contacts=2, cost_per_contact=3.0, value_per_conversion=50.0)
    recommendation, _ = recommend_policy(
        UPLIFT, SEGMENTS, policy, run_id=RUN_ID, computed_on="test", causal=True, observed_top_share=observed
    )
    assert calls == [pytest.approx(2 / 8)]
    expected = recommendation.expected_incremental_conversions
    assert expected is not None
    assert expected.value == pytest.approx(0.30)
    assert expected.ci_low == pytest.approx(0.10)
    assert expected.ci_high == pytest.approx(0.50)
    assert expected.confidence_level == 0.95
    assert recommendation.expected_cost == pytest.approx(6.0)
    assert recommendation.expected_value == pytest.approx(15.0)
    assert recommendation.expected_net_value == pytest.approx(9.0)


def test_an_unmeasurable_top_share_leaves_the_expectation_null() -> None:
    recommendation, _ = recommend_policy(
        UPLIFT,
        SEGMENTS,
        UpliftPolicyConfig(value_per_conversion=10.0),
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed_top_share=lambda fraction: None,
    )
    assert recommendation.expected_incremental_conversions is None
    assert recommendation.expected_value is None
    assert recommendation.expected_cost is None


def test_a_missing_interval_bound_stays_missing() -> None:
    recommendation, _ = recommend_policy(
        UPLIFT,
        SEGMENTS,
        UpliftPolicyConfig(),
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed_top_share=lambda fraction: ConfidenceValue(value=0.1),
    )
    expected = recommendation.expected_incremental_conversions
    assert expected is not None
    assert expected.value == pytest.approx(0.4)
    assert expected.ci_low is None
    assert expected.ci_high is None


def test_choosing_nobody_expects_exactly_zero_without_asking_the_hold_out() -> None:
    def observed(fraction: float) -> ConfidenceValue | None:
        raise AssertionError("an empty selection must not be looked up")

    recommendation, selected = recommend_policy(
        np.array([0.0, -0.3]),
        segs(S, D),
        UpliftPolicyConfig(cost_per_contact=2.0, value_per_conversion=10.0),
        run_id=RUN_ID,
        computed_on="test",
        causal=True,
        observed_top_share=observed,
    )
    assert not selected.any()
    assert recommendation.stop_reason is PolicyStopReason.NO_PERSUADABLES
    assert recommendation.contacts_recommended == 0
    assert recommendation.predicted_incremental_conversions == 0.0
    assert recommendation.expected_incremental_conversions == ConfidenceValue(
        value=0.0, ci_low=0.0, ci_high=0.0
    )
    assert recommendation.expected_cost == 0.0
    assert recommendation.expected_value == 0.0
    assert recommendation.expected_net_value == 0.0


def test_eligibility_is_counted_in_the_recommendation() -> None:
    eligible = np.array([True, True, False, True, True, True, False, True])
    recommendation, selected = recommend_policy(
        UPLIFT,
        SEGMENTS,
        UpliftPolicyConfig(),
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        eligible=eligible,
    )
    assert chosen(selected) == [1, 4]
    assert recommendation.eligible_persuadables == 2
    assert recommendation.rows == 8
    assert recommendation.stop_reason is PolicyStopReason.ALL_PERSUADABLES


# ---------------------------------------------------------------------------
# Ties and the ranking depth the selection reaches
# ---------------------------------------------------------------------------
def test_a_tiebreak_orders_rows_of_equal_uplift() -> None:
    uplift = np.array([0.1, 0.1, 0.1, 0.1, 0.3])
    selected, reason = choose_contacts(
        uplift,
        segs(P, P, P, P, P),
        UpliftPolicyConfig(budget_contacts=3),
        tiebreak=np.array([30, 10, 20, 0, 99], dtype=np.uint64),
    )
    # Row 4 first (highest uplift, whatever its tie-break), then the tied rows by tie-break: 3, 1.
    assert chosen(selected) == [1, 3, 4]
    assert reason is PolicyStopReason.BUDGET


def test_ranking_positions_follow_uplift_then_tiebreak_then_input_order() -> None:
    uplift = np.array([0.1, 0.2, 0.1, 0.1])
    assert rank_positions(uplift).tolist() == [1, 0, 2, 3]
    assert rank_positions(uplift, np.array([5.0, 0.0, 1.0, 5.0])).tolist() == [2, 0, 1, 3]
    assert ranking(uplift, np.array([5.0, 0.0, 1.0, 5.0])).tolist() == [1, 2, 0, 3]


@pytest.mark.parametrize(
    "tiebreak",
    [np.array([1, 2]), np.array([[1, 2, 3, 4, 5, 6, 7, 8]]), np.array(["a"] * 8), np.full(8, np.nan)],
)
def test_a_malformed_tiebreak_is_refused(tiebreak: np.ndarray) -> None:
    with pytest.raises(ValueError, match="tiebreak"):
        choose_contacts(UPLIFT, SEGMENTS, UpliftPolicyConfig(), tiebreak=tiebreak)


def test_the_hold_out_is_asked_about_the_depth_the_selection_reaches_not_n_over_rows() -> None:
    # Row 2 (the best) is not eligible, so the two chosen rows (6 and 1) reach position 3 of the
    # ranking of all 8 rows: the hold-out must be asked about its top 3/8, not its top 2/8.
    calls: list[float] = []

    def observed(fraction: float) -> ConfidenceValue | None:
        calls.append(fraction)
        return ConfidenceValue(value=0.1, ci_low=0.05, ci_high=0.15)

    eligible = np.array([True, True, False, True, True, True, True, True])
    recommendation, selected = recommend_policy(
        UPLIFT,
        SEGMENTS,
        UpliftPolicyConfig(budget_contacts=2),
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=observed,
        eligible=eligible,
    )
    assert chosen(selected) == [1, 6]
    assert calls == [pytest.approx(3 / 8)]
    expected = recommendation.expected_incremental_conversions
    assert expected is not None and expected.value == pytest.approx(0.2)


def test_the_depth_counts_tied_rows_only_up_to_the_tiebreak_cut() -> None:
    calls: list[float] = []

    def observed(fraction: float) -> ConfidenceValue | None:
        calls.append(fraction)
        return ConfidenceValue(value=0.1)

    uplift = np.full(10, 0.1)
    eligible = np.array([True, False] * 5)
    recommend_policy(
        uplift,
        segs(*([P] * 10)),
        UpliftPolicyConfig(budget_contacts=2),
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=observed,
        eligible=eligible,
        tiebreak=np.arange(10, dtype=np.uint64)[::-1].copy(),
    )
    # Tie-break ranks row 9 first; the eligible (even) rows by it are 8, 6, ...; row 6 is 4th.
    assert calls == [pytest.approx(4 / 10)]


def test_expected_conversions_are_not_overstated_when_control_and_suppressed_rows_are_ranked() -> None:
    """A steep uplift curve, a 10% control group and 40% suppressed at random (the review's case).

    The hold-out predictions equal the true uplift, so the expected incremental conversions of the
    chosen rows are known: the sum of their true uplift. Asking the hold-out about `N / rows` (the
    old rule) reported about 346 against a truth of about 272, outside its own interval.
    """
    from engine.uplift.metrics import bootstrap_uplift_at
    from engine.uplift.segments import assign_segments

    rng = np.random.default_rng(0)
    holdout_tau = rng.exponential(0.05, 40_000)
    t = rng.integers(0, 2, 40_000)
    y = (rng.random(40_000) < np.clip(0.05 + holdout_tau * t, 0.0, 1.0)).astype(int)

    def observed(fraction: float) -> ConfidenceValue | None:
        return bootstrap_uplift_at(holdout_tau, t, y, fraction, samples=50, seed=1)

    rows = 20_000
    tau = rng.exponential(0.05, rows)
    thresholds = SegmentThresholds(
        persuadable_min_uplift=0.02,
        sleeping_dog_max_uplift=-0.01,
        sure_thing_min_probability=0.5,
        sure_thing_from_base_rate=False,
    )
    segments = assign_segments(tau, np.full(rows, 0.05), thresholds)
    draw = rng.random(rows)
    eligible = draw >= 0.5  # 10% control and 40% suppressed, neither depending on uplift
    recommendation, selected = recommend_policy(
        tau,
        segments,
        UpliftPolicyConfig(budget_contacts=2_000),
        run_id=RUN_ID,
        computed_on="scored",
        causal=True,
        observed_top_share=observed,
        eligible=eligible,
    )
    expected = recommendation.expected_incremental_conversions
    assert recommendation.contacts_recommended == 2_000
    assert expected is not None and expected.ci_low is not None and expected.ci_high is not None
    truth = float(tau[selected].sum())
    assert expected.ci_low <= truth <= expected.ci_high
    assert abs(expected.value - truth) / truth < 0.1
