"""The evaluator: fit on one part, score on customers it never saw, no leakage either way."""

from __future__ import annotations

import numpy as np
import pytest

from autonomy.scientist import (
    BanditAgent,
    BlanketPolicy,
    Campaign,
    SegmentRule,
    UpliftPolicy,
    evaluate_candidates,
    split_campaign,
)
from autonomy.scientist.evaluate import HELD_OUT_SHARE, se_of
from tests.v2.scientist.test_menu_campaign import make_campaign


def replace(c: Campaign, **changes: object) -> Campaign:
    fields = {**c.__dict__, **changes}
    fields.pop("attributes", None)  # cached views
    fields.pop("behaviour_matrix", None)
    return Campaign(**fields)


def poison_outcomes(c: Campaign, rows: np.ndarray) -> Campaign:
    """Flip every outcome and every arm on `rows` (as an adversary who wants a fit to cheat would)."""
    converted = c.converted.copy()
    arm = c.arm.copy()
    converted[rows] = 1.0 - converted[rows]
    arm[rows] = (arm[rows] + 1) % c.n_arms
    margin = 10.0 * converted
    return replace(c, converted=converted, margin=margin, arm=arm)


CANDIDATES = (SegmentRule(), UpliftPolicy())


# --- the split --------------------------------------------------------------------------------------


def test_customer_split_is_stable_and_about_half() -> None:
    c = make_campaign(4000)
    a, b = split_campaign(c), split_campaign(c)
    assert np.array_equal(a.held_rows, b.held_rows) and a.method == "customer"
    assert abs(len(a.held_rows) / c.n - HELD_OUT_SHARE) < 0.03
    assert set(a.fit_rows).isdisjoint(a.held_rows) and len(a.fit_rows) + len(a.held_rows) == c.n
    # the split depends on the customer id only, not on the order of the rows
    order = np.random.default_rng(0).permutation(c.n)
    shuffled = c.take(order)
    held_ids = {shuffled.customer_ids[i] for i in split_campaign(shuffled).held_rows}
    assert held_ids == {c.customer_ids[i] for i in a.held_rows}


def test_a_campaign_that_was_not_randomised_is_split_by_time() -> None:
    c = make_campaign(2000)
    times = np.random.default_rng(1).permutation(c.n).astype(np.float64)
    c = replace(c, randomised=False, times=times)
    sp = split_campaign(c)
    assert sp.method == "time"
    assert times[sp.fit_rows].max() < times[sp.held_rows].min()  # the held-out customers are all later
    no_clock = split_campaign(replace(c, times=None))
    assert no_clock.method == "customer" and "not randomised" in no_clock.note
    # one reference time for every row (as a history-built campaign has) orders nobody: no "time" split by row position
    flat = split_campaign(replace(c, times=np.full(c.n, 1.7e9)))
    assert flat.method == "customer" and "not randomised" in flat.note


# --- no leakage (check C4) -----------------------------------------------------------------------------


@pytest.mark.parametrize("cand", [*CANDIDATES, BlanketPolicy(1)], ids=lambda c: c.kind)
def test_nothing_of_the_held_out_outcomes_reaches_the_fit(cand: object) -> None:
    c = make_campaign(5000, seed=5)
    sp = split_campaign(c)
    fit, held = c.take(sp.fit_rows), c.take(sp.held_rows)
    poisoned_held = poison_outcomes(held, np.arange(held.n))  # every held-out outcome and arm flipped
    clean = cand.held_out_probabilities(fit, held)  # type: ignore[attr-defined]
    dirty = cand.held_out_probabilities(fit, poisoned_held)  # type: ignore[attr-defined]
    assert np.array_equal(clean, dirty)
    # and the fit is the fit on the fit rows alone: the same whether or not the rest exists
    alone = cand.held_out_probabilities(c.take(sp.fit_rows), held)  # type: ignore[attr-defined]
    assert np.array_equal(clean, alone)


def test_held_out_scores_do_not_depend_on_the_fit_outcomes_for_a_fixed_policy() -> None:
    c = make_campaign(5000, seed=6)
    sp = split_campaign(c)
    ev = evaluate_candidates(c, [BlanketPolicy(1)], default=BlanketPolicy(1), none=BlanketPolicy(0), split=sp)
    dirty = poison_outcomes(c, sp.fit_rows)
    ev2 = evaluate_candidates(
        dirty, [BlanketPolicy(1)], default=BlanketPolicy(1), none=BlanketPolicy(0), split=sp
    )
    assert (
        ev.candidates[0].value == ev2.candidates[0].value
    )  # a blanket policy fits nothing: only held-out rows count
    assert ev.none.value == ev2.none.value


def test_fitted_candidates_do_depend_on_the_fit_outcomes() -> None:
    """The reverse check, so the two above cannot pass because nothing is read at all."""
    c = make_campaign(5000, seed=7)
    sp = split_campaign(c)
    dirty = poison_outcomes(c, sp.fit_rows)
    a = SegmentRule().held_out_probabilities(c.take(sp.fit_rows), c.take(sp.held_rows))
    b = SegmentRule().held_out_probabilities(dirty.take(sp.fit_rows), c.take(sp.held_rows))
    assert not np.array_equal(a, b)


# --- the scores --------------------------------------------------------------------------------------


def test_off_policy_value_matches_the_truth_within_its_interval() -> None:
    """Known truth: send A to x1 > 0.5, nothing otherwise. Per customer: profit of no contact is 10 x
    base chance (mean 1.5); sending A where x1 > 0.5 adds +0.5, so the rule is worth 1.5 + 0.25."""
    c = make_campaign(20000, seed=8, arm_probs=(0.4, 0.3, 0.3))
    rule = SegmentRule(columns=("x1",), exploration_floor=0.0)
    ev = evaluate_candidates(c, [rule], default=BlanketPolicy(0), none=BlanketPolicy(0))
    e = ev.candidates[0]
    assert e.status == "ok" and e.value is not None and e.vs_default is not None
    assert e.value.low < 1.75 < e.value.high
    assert abs(e.value.point - 1.75) < 3.0 * se_of(e.value)
    assert ev.none.value is not None and ev.none.value.low < 1.5 < ev.none.value.high
    assert e.vs_default.low > 0.0  # the paired gain over doing nothing is clearly positive
    assert e.vs_none == e.vs_default  # the default here is 'nothing'
    assert e.contact_rate is not None and 0.4 < e.contact_rate < 0.6 and e.effective_n > 1000
    assert ev.default.vs_default is not None and ev.default.vs_default.point == 0.0
    assert ev.n_fit + ev.n_held == c.n and len(ev.arm_counts_fit) == 3


def test_a_blanket_loser_is_scored_as_a_loser() -> None:
    c = make_campaign(20000, seed=9)
    ev = evaluate_candidates(c, [BlanketPolicy(2)], default=BlanketPolicy(0), none=BlanketPolicy(0))
    e = ev.candidates[0]
    assert e.vs_default is not None and e.vs_default.high < 0.0  # arm B costs 1 and earns 0.2


def test_a_campaign_without_logged_probabilities_cannot_be_evaluated() -> None:
    c = replace(make_campaign(2000), propensity=None)
    with pytest.raises(ValueError, match="propensit"):
        evaluate_candidates(c, CANDIDATES, default=BlanketPolicy(0), none=BlanketPolicy(0))


def test_a_candidate_that_fails_is_reported_and_the_others_still_run() -> None:
    c = make_campaign(3000, seed=10)
    broken = BanditAgent(runner=lambda fit, held: np.zeros((held.n, held.n_arms)))  # rows do not sum to 1
    ev = evaluate_candidates(c, [broken, BlanketPolicy(1)], default=BlanketPolicy(0), none=BlanketPolicy(0))
    assert ev.candidates[0].status == "failed" and "sum" in ev.candidates[0].note
    assert ev.candidates[1].status == "ok"


def test_the_agent_runner_sees_the_fit_part_then_the_held_out_part_only() -> None:
    c = make_campaign(3000, seed=11)
    sp = split_campaign(c)
    seen: dict[str, tuple[str, ...]] = {}

    def runner(fit: Campaign, held: Campaign) -> np.ndarray:
        seen["fit"], seen["held"] = fit.customer_ids, held.customer_ids
        return np.tile([1.0, 0.0, 0.0], (held.n, 1))

    ev = evaluate_candidates(
        c, [BanditAgent(runner=runner)], default=BlanketPolicy(0), none=BlanketPolicy(0), split=sp
    )
    assert ev.candidates[0].status == "ok"
    assert set(seen["fit"]).isdisjoint(seen["held"])
    assert len(seen["fit"]) == len(sp.fit_rows) and len(seen["held"]) == len(sp.held_rows)
