"""The offer the policy would choose if the hold-out did not exist (`engine.decide.offer_choice.policy_offers`,
DEC-1311 (af)-(ag)), in isolation.

A campaign compares the customers a list contacted with those it held back, and must cut both by one rule.
`choose_offers` never saw a held-back customer, so it cannot say what they would have been given. These
tests fail on the commit before DEC-1311 (af) (the function does not exist there).
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.decide.offer_choice import NO_OFFER, choose_offers, policy_offers


def _both(
    net: list[list[float]],
    cost: list[list[float]] | None = None,
    *,
    held: list[bool] | None = None,
    suppressed: list[bool] | None = None,
    reachable: list[list[bool]] | None = None,
    dogs: list[list[bool]] | None = None,
    budget: float | None = None,
    max_offers: int | None = None,
    min_roi: float = 0.0,
):  # type: ignore[no-untyped-def]
    """The run's choice (hold-out excluded) and the policy's offer (hold-out ignored), on the same arrays."""
    values = np.asarray(net, dtype=np.float64)
    costs = np.ones_like(values) if cost is None else np.asarray(cost, dtype=np.float64)
    rows = values.shape[0]
    shape = values.shape
    out = np.zeros(rows, bool) if held is None else np.asarray(held, dtype=bool)
    gone = np.zeros(rows, bool) if suppressed is None else np.asarray(suppressed, dtype=bool)
    reach = np.ones(shape, bool) if reachable is None else np.asarray(reachable, dtype=bool)
    dog = np.zeros(shape, bool) if dogs is None else np.asarray(dogs, dtype=bool)
    choice = choose_offers(
        values,
        costs,
        sleeping_dog=dog,
        eligible=reach & ~(out | gone)[:, None],
        budget=budget,
        min_roi=min_roi,
        max_offers=max_offers,
    )
    policy = policy_offers(
        values,
        costs,
        sleeping_dog=dog,
        eligible=reach & ~gone[:, None],
        offered=choice.arm != NO_OFFER,
        budget=budget,
        min_roi=min_roi,
        max_offers=max_offers,
    )
    return choice, policy


def test_a_held_back_customer_has_the_offer_the_rule_would_have_chosen() -> None:
    choice, policy = _both([[5.0, 1.0], [1.0, 7.0], [2.0, 9.0]], held=[False, False, True])
    assert choice.arm.tolist() == [1, 2, NO_OFFER]  # the run never offered the held-back customer anything
    assert policy.arm.tolist() == [1, 2, 2]
    assert policy.net_value.tolist() == [5.0, 7.0, 9.0]
    assert policy.intended.tolist() == [True, True, True]


def test_without_a_hold_out_the_policy_offer_is_the_runs_own_choice() -> None:
    choice, policy = _both([[5.0, 1.0], [1.0, 7.0], [-1.0, -2.0], [3.0, 3.0]], budget=8.0)
    given = choice.arm != NO_OFFER
    assert (policy.arm[given] == choice.arm[given]).all()
    assert (policy.net_value[given] == choice.net_value[given]).all()


def test_suppression_contactability_sleeping_dogs_and_min_roi_still_count() -> None:
    reach = [[True, True], [False, False], [True, True], [True, True], [True, True]]
    dogs = [[False, False], [False, False], [True, True], [False, False], [False, False]]
    choice, policy = _both(
        [[4.0, 2.0], [9.0, 9.0], [9.0, 9.0], [9.0, 9.0], [1.0, 1.0]],
        [[1.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0], [4.0, 4.0]],
        suppressed=[False, False, False, True, False],
        reachable=reach,
        dogs=dogs,
        held=[True, True, True, True, True],
        min_roi=1.0,
    )
    assert choice.arm.tolist() == [NO_OFFER] * 5
    # Offer 1 for the reachable one; none for: unreachable, a sleeping dog for every offer, suppressed, and a
    # customer whose best offer returns less than its cost.
    assert policy.arm.tolist() == [1, NO_OFFER, NO_OFFER, NO_OFFER, NO_OFFER]
    assert np.isnan(policy.net_value[1:]).all()


def test_the_budget_cuts_at_the_last_customer_the_run_gave_an_offer_in_the_walks_own_order() -> None:
    # Net value per rupee, highest first: rows 0, 1, 2, 3, 4. Rows 1 and 3 are held back; the budget of 3
    # lets the run give offers to rows 0, 2 and 4 (one rupee each); nobody after row 4 is reached.
    net = [[9.0], [8.0], [7.0], [6.0], [5.0], [4.0], [3.0]]
    choice, policy = _both(net, held=[False, True, False, True, False, False, False], budget=3.0)
    assert choice.arm.tolist() == [1, NO_OFFER, 1, NO_OFFER, 1, NO_OFFER, NO_OFFER]
    # The cut is row 4, the last customer given an offer: the held-back rows 1 and 3 are before it, so they
    # are intended; rows 5 and 6 are beyond it, so they are not.
    assert policy.intended.tolist() == [True, True, True, True, True, False, False]
    assert policy.arm.tolist() == [1, 1, 1, 1, 1, NO_OFFER, NO_OFFER]
    assert np.isnan(policy.net_value[5:]).all()


def test_every_customer_the_run_contacted_is_intended_and_held_back_customers_are_cut_at_the_same_place() -> (
    None
):
    rng = np.random.default_rng(7)
    rows = 4000
    net = rng.normal(2.0, 4.0, size=(rows, 3))
    cost = rng.uniform(0.5, 3.0, size=(rows, 3))
    held = rng.random(rows) < 0.15
    choice, policy = _both(net.tolist(), cost.tolist(), held=held.tolist(), budget=2500.0, max_offers=900)
    contacted = choice.arm != NO_OFFER
    assert contacted.sum() == 900
    assert policy.intended[contacted].all()
    assert (policy.arm[contacted] == choice.arm[contacted]).all()
    # One ranking: every customer beyond the cut ranks below the lowest-ranked customer the run contacted,
    # and every held-back customer ranking above that point is intended.
    preferred = choose_offers(net, cost, sleeping_dog=np.zeros(net.shape, bool)).preferred_arm
    column = np.maximum(preferred - 1, 0)[:, None]
    value = np.take_along_axis(net, column, 1)[:, 0]
    price = np.take_along_axis(cost, column, 1)[:, 0]
    ratio = np.where(preferred > 0, value / price, -np.inf)
    beyond = (preferred > 0) & ~policy.intended
    assert beyond.sum() > 100
    assert ratio[beyond].max() <= ratio[contacted].min() + 1e-12
    above = held & (preferred > 0) & (ratio > ratio[contacted].min())
    assert above.sum() > 20 and policy.intended[above].all()


def test_a_budget_the_run_did_not_spend_on_anyone_makes_nobody_intended() -> None:
    choice, policy = _both([[5.0], [4.0]], [[3.0], [3.0]], held=[False, True], budget=1.0)
    assert (choice.arm == NO_OFFER).all()
    assert not policy.intended.any()


def test_a_max_offers_cap_cuts_in_the_same_order() -> None:
    choice, policy = _both([[9.0], [8.0], [7.0], [6.0]], held=[False, True, False, False], max_offers=2)
    assert choice.arm.tolist() == [1, NO_OFFER, 1, NO_OFFER]
    assert policy.intended.tolist() == [True, True, True, False]


def test_the_policy_offer_is_never_an_offer_the_customer_is_a_sleeping_dog_for() -> None:
    dogs = [[True, False], [False, True]]
    _, policy = _both([[9.0, 1.0], [1.0, 9.0]], held=[True, True], dogs=dogs)
    assert policy.arm.tolist() == [2, 1]


def test_the_shape_of_offered_is_checked() -> None:
    with pytest.raises(ValueError, match="offered"):
        policy_offers(
            np.ones((3, 2)),
            np.ones((3, 2)),
            sleeping_dog=np.zeros((3, 2), bool),
            eligible=np.ones((3, 2), bool),
            offered=np.ones(2, bool),
        )
