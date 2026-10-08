"""Plan J M100: the offer choice, in isolation (`engine.decide.offer_choice`, DEC-1310 (f)).

The rule, per customer: the eligible, non-sleeping-dog offer with the highest net value, or no offer
when there is none worth its cost; under a total budget, customers are taken greedily by net value per
rupee and the budget is never exceeded. These tests fail on the commit before M100 (the module does
not exist there).
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.decide.offer_choice import (
    NO_OFFER,
    OFFER_REASONS,
    arm_net_values,
    choose_offers,
)
from engine.pilot.roi import ValueCosts
from engine.uplift.config import UpliftPolicyConfig


def _choice(net: list[list[float]], cost: list[list[float]] | None = None, **kwargs: object):  # type: ignore[no-untyped-def]
    values = np.asarray(net, dtype=np.float64)
    costs = np.ones_like(values) if cost is None else np.asarray(cost, dtype=np.float64)
    dogs = kwargs.pop("sleeping_dog", np.zeros(values.shape, dtype=bool))
    return choose_offers(values, costs, sleeping_dog=dogs, **kwargs)  # type: ignore[arg-type]


def test_each_customer_gets_the_offer_with_the_highest_net_value() -> None:
    choice = _choice([[5.0, 1.0], [1.0, 7.0], [2.0, 2.0]])
    assert choice.arm.tolist() == [1, 2, 1]  # a tie goes to the earlier offer
    assert choice.net_value.tolist() == [5.0, 7.0, 2.0]
    assert choice.reason == ["offer", "offer", "offer"]


def test_no_offer_when_every_offer_is_below_its_cost() -> None:
    choice = _choice([[-1.0, -0.5], [0.0, -2.0]])
    assert choice.arm.tolist() == [NO_OFFER, 1]  # a net value of exactly zero pays for itself
    assert choice.reason[0] == "below_cost"
    assert np.isnan(choice.net_value[0])
    assert choice.cost[0] == 0.0


def test_min_roi_asks_each_offer_to_return_a_multiple_of_its_cost() -> None:
    choice = _choice([[3.0, 5.0]], [[1.0, 4.0]], min_roi=1.0)
    assert choice.arm.tolist() == [2]  # both return their cost (3 >= 1, 5 >= 4): the larger wins
    choice = _choice([[3.0, 3.5]], [[1.0, 4.0]], min_roi=1.0)
    assert choice.arm.tolist() == [1]  # offer 2 (3.5 < 4.0) is not a candidate, offer 1 (3 >= 1) is


def test_a_sleeping_dog_never_gets_the_offer_that_puts_them_off() -> None:
    dogs = np.array([[True, False], [True, True], [False, True]])
    choice = _choice([[9.0, 1.0], [9.0, 9.0], [1.0, 9.0]], sleeping_dog=dogs)
    assert choice.arm.tolist() == [2, NO_OFFER, 1]
    assert choice.reason[1] == "sleeping_dog"
    assert not np.any(dogs[np.arange(3), np.maximum(choice.arm - 1, 0)] & (choice.arm != NO_OFFER))


def test_an_ineligible_offer_is_never_chosen() -> None:
    eligible = np.array([[False, True], [False, False], [True, False]])
    choice = _choice([[9.0, 1.0], [9.0, 9.0], [1.0, 9.0]], eligible=eligible)
    assert choice.arm.tolist() == [2, NO_OFFER, 1]
    assert choice.reason[1] == "no_eligible_offer"


def test_randomised_inputs_never_choose_an_ineligible_offer_or_a_sleeping_dog() -> None:
    rng = np.random.default_rng(100)
    rows, arms = 5_000, 3
    net = rng.normal(size=(rows, arms))
    net[rng.random((rows, arms)) < 0.05] = np.nan
    cost = rng.random((rows, arms)) * 2.0
    dogs = rng.random((rows, arms)) < 0.2
    eligible = rng.random((rows, arms)) < 0.7
    for budget in (None, 50.0):
        choice = choose_offers(net, cost, sleeping_dog=dogs, eligible=eligible, budget=budget)
        given = choice.arm != NO_OFFER
        k = choice.arm[given] - 1
        assert eligible[given, k].all()
        assert not dogs[given, k].any()
        assert np.isfinite(net[given, k]).all() and (net[given, k] >= 0).all()
        assert np.isclose(choice.spent, cost[given, k].sum())
        if budget is not None:
            assert choice.spent <= budget + 1e-9
        # The chosen offer is the best candidate before the budget.
        best = np.where(eligible & ~dogs & np.isfinite(net) & (net >= 0), net, -np.inf).max(axis=1)
        preferred = choice.preferred_arm != NO_OFFER
        assert np.allclose(net[preferred, choice.preferred_arm[preferred] - 1], best[preferred])
        assert (np.isinf(best) == ~preferred).all()


def test_the_runner_up_is_the_next_offer_the_customer_could_be_given() -> None:
    dogs = np.array([[False, False, False], [False, True, False], [False, False, False]])
    choice = _choice([[5.0, 3.0, -1.0], [2.0, 9.0, 1.0], [-1.0, -2.0, -0.5]], sleeping_dog=dogs)
    assert choice.runner_up_arm.tolist() == [2, 3, 3]
    assert choice.runner_up_net_value.tolist() == [3.0, 1.0, -0.5]  # for no offer: the best one there was
    assert choice.arm[2] == NO_OFFER


def test_one_offer_only_has_no_runner_up() -> None:
    choice = _choice([[4.0], [-1.0]])
    assert choice.runner_up_arm.tolist() == [NO_OFFER, 1]
    assert np.isnan(choice.runner_up_net_value[0])


def test_the_budget_is_spent_greedily_by_net_value_per_rupee() -> None:
    # Ratios: 10/5 = 2, 6/2 = 3, 8/8 = 1, 3/1 = 3 (tie with row 1, larger net value first).
    choice = _choice([[10.0], [6.0], [8.0], [3.0]], [[5.0], [2.0], [8.0], [1.0]], budget=8.5)
    assert choice.arm.tolist() == [1, 1, NO_OFFER, 1]
    assert choice.spent == 8.0
    assert choice.reason[2] == "over_budget"
    assert choice.preferred_arm.tolist() == [1, 1, 1, 1]


def test_a_customer_who_does_not_fit_is_skipped_and_cheaper_ones_still_fit() -> None:
    choice = _choice([[50.0], [9.0], [1.0]], [[10.0], [6.0], [1.0]], budget=11.0)
    # By ratio: row 0 (5), row 1 (1.5), row 2 (1). Row 0 fits (10), row 1 does not (16), row 2 does (11).
    assert choice.arm.tolist() == [1, NO_OFFER, 1]
    assert choice.spent == 11.0


def test_an_offer_is_never_switched_to_a_cheaper_one_to_fit_the_budget() -> None:
    choice = _choice([[10.0, 9.0]], [[5.0, 1.0]], budget=2.0)
    assert choice.arm.tolist() == [NO_OFFER]
    assert choice.preferred_arm.tolist() == [1]


def test_a_zero_cost_offer_is_taken_first_and_costs_nothing() -> None:
    choice = _choice([[1.0], [100.0]], [[0.0], [5.0]], budget=0.0)
    assert choice.arm.tolist() == [1, NO_OFFER]
    assert choice.spent == 0.0


def test_no_budget_gives_every_customer_their_best_offer() -> None:
    rng = np.random.default_rng(3)
    net, cost = rng.normal(size=(500, 2)), rng.random((500, 2))
    dogs = np.zeros((500, 2), dtype=bool)
    free = choose_offers(net, cost, sleeping_dog=dogs)
    ample = choose_offers(net, cost, sleeping_dog=dogs, budget=float(cost.sum()) + 1.0)
    assert (free.arm == ample.arm).all() and (free.arm == free.preferred_arm).all()


def test_bad_inputs_are_refused() -> None:
    dogs = np.zeros((2, 2), dtype=bool)
    with pytest.raises(ValueError, match="rows x offers"):
        choose_offers(np.zeros(2), np.zeros(2), sleeping_dog=dogs)
    with pytest.raises(ValueError, match="cost"):
        choose_offers(np.zeros((2, 2)), -np.ones((2, 2)), sleeping_dog=dogs)
    with pytest.raises(ValueError, match="shape"):
        choose_offers(np.zeros((2, 2)), np.zeros((2, 2)), sleeping_dog=np.zeros((2, 3), dtype=bool))
    with pytest.raises(ValueError, match="boolean"):
        choose_offers(np.zeros((2, 2)), np.zeros((2, 2)), sleeping_dog=np.zeros((2, 2)))
    with pytest.raises(ValueError, match="budget"):
        choose_offers(np.zeros((2, 2)), np.zeros((2, 2)), sleeping_dog=dogs, budget=-1.0)


def test_reasons_are_words_from_the_published_list() -> None:
    choice = _choice([[1.0], [-1.0]])
    assert set(choice.reason) <= set(OFFER_REASONS)
    assert choice.offered().tolist() == [True, False]


# ---------------------------------------------------------------------------
# arm_net_values: M97's net value per offer, with each offer's own costs
# ---------------------------------------------------------------------------
def test_net_value_per_offer_uses_that_offers_costs() -> None:
    policy = UpliftPolicyConfig(value_per_conversion=1000.0)
    uplift = np.array([[0.10, 0.02], [0.00, 0.05]])
    p_treated = np.array([[0.5, 0.2], [0.1, 0.4]])
    money = arm_net_values(
        uplift,
        policy,
        arm_costs=[
            ValueCosts(contact_cost=2.0, offer_cost=50.0),
            ValueCosts(contact_cost=1.0, offer_cost=0.0),
        ],
        p_treated=p_treated,
    )
    assert np.allclose(money.cost, [[27.0, 1.0], [7.0, 1.0]])
    assert np.allclose(money.net_value, [[100.0 - 27.0, 20.0 - 1.0], [-7.0, 50.0 - 1.0]])


def test_the_value_path_weights_each_customer_by_their_value() -> None:
    policy = UpliftPolicyConfig(value_column="spend", margin_pct=50.0)
    uplift = np.array([[0.1, 0.2], [0.1, 0.2]])
    money = arm_net_values(
        uplift,
        policy,
        arm_costs=[
            ValueCosts(contact_cost=1.0, offer_cost=0.0),
            ValueCosts(contact_cost=3.0, offer_cost=0.0),
        ],
        values=np.array([1000.0, np.nan]),
    )
    # 0.1 x 1000 x 0.5 - 1 = 49; 0.2 x 1000 x 0.5 - 3 = 97; a missing value counts as zero value.
    assert np.allclose(money.net_value, [[49.0, 97.0], [-1.0, -3.0]])


def test_cost_per_contact_is_every_offers_contact_cost_as_in_m97() -> None:
    policy = UpliftPolicyConfig(value_per_conversion=10.0, cost_per_contact=0.5)
    money = arm_net_values(
        np.array([[0.1, 0.1]]),
        policy,
        arm_costs=[
            ValueCosts(contact_cost=9.0, offer_cost=0.0),
            ValueCosts(contact_cost=9.0, offer_cost=1.0),
        ],
    )
    assert np.allclose(money.cost, [[0.5, 1.5]])


def test_no_stated_value_cannot_choose_by_money() -> None:
    with pytest.raises(ValueError, match="value_per_conversion"):
        arm_net_values(
            np.zeros((1, 2)),
            UpliftPolicyConfig(),
            arm_costs=[ValueCosts(contact_cost=0.0, offer_cost=0.0)] * 2,
        )
    with pytest.raises(ValueError, match="arm_costs"):
        arm_net_values(
            np.zeros((1, 2)),
            UpliftPolicyConfig(value_per_conversion=1.0),
            arm_costs=[ValueCosts(contact_cost=0.0, offer_cost=0.0)],
        )
