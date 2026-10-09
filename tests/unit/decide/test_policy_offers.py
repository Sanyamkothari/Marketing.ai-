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


def test_the_budget_walk_is_replayed_and_a_held_back_customer_is_intended_when_it_had_room() -> None:
    # Net value per rupee, highest first: rows 0, 1, 2, 3, 4. Rows 1 and 3 are held back; the budget of 3
    # lets the run give offers to rows 0, 2 and 4 (one rupee each); nobody after row 4 is reached.
    net = [[9.0], [8.0], [7.0], [6.0], [5.0], [4.0], [3.0]]
    choice, policy = _both(net, held=[False, True, False, True, False, False, False], budget=3.0)
    assert choice.arm.tolist() == [1, NO_OFFER, 1, NO_OFFER, 1, NO_OFFER, NO_OFFER]
    # The held-back rows 1 and 3 had room when the walk reached them; rows 5 and 6 did not.
    assert policy.intended.tolist() == [True, True, True, True, True, False, False]
    assert policy.arm.tolist() == [1, 1, 1, 1, 1, NO_OFFER, NO_OFFER]
    assert np.isnan(policy.net_value[5:]).all()


def test_a_customer_the_walk_skipped_is_intended_in_neither_arm_and_a_held_back_one_at_a_full_place_is_not() -> (
    None
):
    # Net value per rupee: rows 0 (9), 1 (3), 6 (2.8), 2 (2), 3 (1.5), 4 (1), 5 (0.5). Budget 3. Rows 3, 5 and 6
    # are held back. The run gives 0 (spent 1), skips 1 (a 5-rupee offer does not fit), gives 2 and 4.
    net = [[9.0], [15.0], [2.0], [1.5], [1.0], [0.5], [14.0]]
    cost = [[1.0], [5.0], [1.0], [1.0], [1.0], [1.0], [5.0]]
    held = [False, False, False, True, False, True, True]
    choice, policy = _both(net, cost, held=held, budget=3.0)
    assert choice.arm.tolist() == [1, NO_OFFER, 1, NO_OFFER, 1, NO_OFFER, NO_OFFER]
    assert choice.reason[1] == "over_budget"
    # The skipped row 1 is not intended, so it is in neither arm; row 6 is held back and its 5-rupee offer did
    # not fit at its place; row 3 had room (spent 2 of 3); row 5 came after the budget was spent.
    assert policy.intended.tolist() == [True, False, True, True, True, False, False]
    # Not held back: intended exactly when contacted.
    not_held = ~np.asarray(held)
    assert (policy.intended[not_held] == (choice.arm[not_held] != NO_OFFER)).all()


def test_a_budget_that_does_not_bind_behaves_like_no_budget() -> None:
    net = [[9.0], [8.0], [7.0], [6.0]]
    _, bound = _both(net, held=[False, False, False, True], budget=100.0)
    _, free = _both(net, held=[False, False, False, True])
    assert bound.intended.tolist() == free.intended.tolist() == [True] * 4


def test_the_treated_arm_is_exactly_the_contacted_list_and_a_held_back_customer_is_intended_when_it_fit() -> (
    None
):
    rng = np.random.default_rng(7)
    rows = 4000
    net = rng.normal(2.0, 4.0, size=(rows, 3))
    cost = rng.uniform(0.5, 3.0, size=(rows, 3))
    held = rng.random(rows) < 0.15
    budget, cap = 2500.0, 900
    choice, policy = _both(net.tolist(), cost.tolist(), held=held.tolist(), budget=budget, max_offers=cap)
    contacted = choice.arm != NO_OFFER
    assert contacted.sum() == cap
    assert (policy.intended & ~held == contacted).all()
    assert (policy.arm[contacted] == choice.arm[contacted]).all()
    # An independent check of the held-back customers, vectorised: in the walk's order, what the run had
    # spent and given before each customer's place decides whether their offer fit.
    preferred = choose_offers(net, cost, sleeping_dog=np.zeros(net.shape, bool)).preferred_arm
    column = np.maximum(preferred - 1, 0)[:, None]
    value = np.take_along_axis(net, column, 1)[:, 0]
    price = np.take_along_axis(cost, column, 1)[:, 0]
    has = preferred > 0
    candidates = np.flatnonzero(has)
    order = candidates[np.lexsort((candidates, -value[candidates], -value[candidates] / price[candidates]))]
    spent_before = np.cumsum(np.where(contacted[order], price[order], 0.0)) - np.where(
        contacted[order], price[order], 0.0
    )
    given_before = np.cumsum(contacted[order]) - contacted[order]
    fits = (spent_before + price[order] <= budget + 1e-9) & (given_before < cap)
    assert (policy.intended[order] == fits).all()
    skipped = has & ~held & ~contacted
    assert skipped.sum() > 20 and not policy.intended[skipped].any()
    assert (held & has & policy.intended).sum() > 20 and (held & has & ~policy.intended).sum() > 20


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
