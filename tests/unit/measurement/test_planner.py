"""The test planner (Plan J M93): sizes, detectable effects, power and costs.

The acceptance numbers are the textbook ones: at 80% power and two-sided 95%, a fall from 4% to 3%
needs about 5,300 customers per group and a fall from 10% to 8% about 3,200. The reference below is
the closed-form two-proportion formula written out independently with `math.erf` (no
`statistics.NormalDist`), so the planner is checked against arithmetic it does not share.
"""

from __future__ import annotations

import math

import pytest

from engine.measurement.planner import (
    MoneyRange,
    PowerPreviewRequest,
    achieved_power,
    arm_sizes,
    cost_of_explore,
    cost_of_holdout,
    holdout_for_mde,
    mde_two_proportions,
    n_for_mde,
    power_preview,
)
from engine.pilot.plain import jargon_in
from engine.uplift.power import power_of_lift

Z_975 = 1.959963984540054
Z_80 = 0.8416212335729143


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def closed_form_n(p0: float, p1: float) -> float:
    """Equal groups, two-sided 95%, 80% power: (z_a·√(2p̄q̄) + z_b·√(p0q0 + p1q1))² / (p1 − p0)²."""
    pooled = (p0 + p1) / 2.0
    root = Z_975 * math.sqrt(2.0 * pooled * (1.0 - pooled)) + Z_80 * math.sqrt(p0 * (1 - p0) + p1 * (1 - p1))
    return (root / (p1 - p0)) ** 2


@pytest.mark.parametrize(
    ("p0", "p1", "about"),
    [(0.04, 0.03, 5_300), (0.10, 0.08, 3_200)],
)
def test_the_planner_matches_the_closed_form_within_one_percent(p0: float, p1: float, about: int) -> None:
    sizes = n_for_mde(p0, p1 - p0, alpha=0.05, power=0.80)
    assert sizes.reason is None and sizes.n_treat is not None and sizes.n_control == sizes.n_treat
    reference = closed_form_n(p0, p1)
    assert abs(sizes.n_treat - reference) / reference < 0.01
    assert abs(sizes.n_treat - about) / about < 0.01
    # And the other way round: those groups detect that change, and the detectable change is that one.
    assert achieved_power(sizes.n_treat, sizes.n_control, p0, p1 - p0).power == pytest.approx(0.80, abs=0.002)
    down = mde_two_proportions(sizes.n_treat, sizes.n_control, p0, direction="down")
    assert down.absolute is not None and abs(down.absolute - abs(p1 - p0)) / abs(p1 - p0) < 0.01


def test_the_exact_sizes_are_the_textbook_ones() -> None:
    assert n_for_mde(0.04, -0.01).n_treat == 5_301
    assert n_for_mde(0.10, -0.02).n_treat == 3_213


def test_achieved_power_agrees_with_the_holdout_power_of_plan_i() -> None:
    """The far tail aside, `engine.uplift.power` sizes the same test (it keeps the far tail)."""
    for n_t, n_c, p0, lift in ((5301, 5301, 0.04, -0.01), (45_000, 5_000, 0.10, 0.012), (900, 100, 0.3, 0.1)):
        ours = achieved_power(n_t, n_c, p0, lift).power
        assert ours == pytest.approx(power_of_lift(n_t, n_c, p0, lift), abs=1e-4)


def test_either_direction_is_the_larger_of_a_rise_and_a_fall() -> None:
    up = mde_two_proportions(10_000, 1_000, 0.05, direction="up").absolute
    down = mde_two_proportions(10_000, 1_000, 0.05, direction="down").absolute
    either = mde_two_proportions(10_000, 1_000, 0.05)
    assert up is not None and down is not None and either.absolute == max(up, down)
    assert either.relative == pytest.approx(either.absolute / 0.05)
    assert either.points == pytest.approx(either.absolute * 100)


def test_a_bigger_control_group_sees_a_smaller_change() -> None:
    points = [
        mde_two_proportions(*arm_sizes(50_000, share), 0.04).absolute for share in (0.03, 0.05, 0.10, 0.15)
    ]
    assert all(value is not None for value in points)
    assert points == sorted(points, reverse=True)


def test_holdout_for_mde_is_the_smallest_share_that_is_enough() -> None:
    plan = holdout_for_mde(100_000, 0.04, -0.01)
    assert plan.share is not None and plan.n_control is not None and plan.n_treat is not None
    assert achieved_power(plan.n_treat, plan.n_control, 0.04, -0.01).power >= 0.80  # type: ignore[operator]
    one_fewer = achieved_power(plan.n_treat + 1, plan.n_control - 1, 0.04, -0.01).power
    assert one_fewer is not None and one_fewer < 0.80
    assert holdout_for_mde(2_000, 0.04, -0.01).share is None
    assert holdout_for_mde(2_000, 0.04, -0.01).reason is not None


@pytest.mark.parametrize(
    ("result", "value"),
    [
        (lambda: mde_two_proportions(1000, 1000, None), "absolute"),
        (lambda: n_for_mde(None, -0.01), "n_treat"),
        (lambda: holdout_for_mde(10_000, None, -0.01), "share"),
        (lambda: achieved_power(1000, 1000, None, -0.01), "power"),
    ],
)
def test_an_unknown_base_rate_gives_null_with_a_reason(result, value: str) -> None:  # type: ignore[no-untyped-def]
    found = result()
    assert getattr(found, value) is None
    assert found.reason and "base rate is not known" in found.reason


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        (lambda: mde_two_proportions(1000, 0, 0.04), "no customers"),
        (lambda: mde_two_proportions(10, 10, 0.04), "Too few customers"),
        (lambda: mde_two_proportions(1000, 1000, 0.0), "0% or 100%"),
        (lambda: n_for_mde(0.04, 0.0), "zero"),
        (lambda: n_for_mde(0.04, -0.05), "below 0%"),
        (lambda: achieved_power(1000, 1000, 0.04, 0.97), "above 100%"),
    ],
)
def test_what_cannot_be_computed_is_null_with_its_reason_never_zero(call, reason: str) -> None:  # type: ignore[no-untyped-def]
    found = call()
    assert found.reason is not None and reason in found.reason


@pytest.mark.parametrize(
    "call",
    [
        lambda: mde_two_proportions(-1, 10, 0.1),
        lambda: mde_two_proportions(10, 10, 1.5),
        lambda: mde_two_proportions(10, 10, 0.1, alpha=0.0),
        lambda: n_for_mde(0.1, 0.01, power=1.0),
        lambda: cost_of_holdout(10, 0.01, -1.0),
    ],
)
def test_a_callers_mistake_raises(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        call()


def test_the_cost_of_a_holdout_is_the_conversions_it_forgoes() -> None:
    assert cost_of_holdout(2_000, 0.01, 500.0).amount == MoneyRange(low=10_000.0, high=10_000.0)
    assert cost_of_holdout(2_000, (0.005, 0.02), 500.0).amount == MoneyRange(low=5_000.0, high=20_000.0)
    assert cost_of_holdout(2_000, -0.01, 500.0).amount == MoneyRange(low=10_000.0, high=10_000.0)
    no_value = cost_of_holdout(2_000, 0.01, None)
    assert no_value.amount is None and no_value.reason and "value per conversion" in no_value.reason
    no_effect = cost_of_holdout(2_000, None, 500.0)
    assert no_effect.amount is None and no_effect.reason


def test_the_cost_of_exploring_runs_from_contacts_only_to_every_offer_taken() -> None:
    assert cost_of_explore(1_000, 2.0, 100.0).amount == MoneyRange(low=2_000.0, high=102_000.0)
    assert cost_of_explore(0, None, None).amount == MoneyRange(low=0.0, high=0.0)
    missing = cost_of_explore(1_000, 2.0, None)
    assert missing.amount is None and missing.reason


def test_the_preview_gives_one_point_per_share_from_counts_only() -> None:
    request = PowerPreviewRequest(
        eligible=50_000,
        base_rate=0.04,
        holdout_shares=(0.03, 0.05, 0.10, 0.15),
        explore_share=0.02,
        value_per_conversion=500.0,
        contact_cost=1.0,
        offer_cost=50.0,
    )
    preview = power_preview(request)
    assert [point.holdout_share for point in preview.points] == [0.03, 0.05, 0.10, 0.15]
    first = preview.points[0]
    assert (first.n_treat, first.n_control) == (48_500, 1_500)
    expected = mde_two_proportions(48_500, 1_500, 0.04)
    assert first.mde_pp == pytest.approx(expected.points, abs=1e-4)
    assert first.cost_of_holdout == cost_of_holdout(1_500, expected.absolute, 500.0).amount
    assert first.cost_of_explore == MoneyRange(low=1_000.0, high=51_000.0)
    assert first.reason is None
    assert "95% confidence" in preview.basis and "80%" in preview.basis


def test_the_preview_with_an_unknown_base_rate_says_why() -> None:
    preview = power_preview(PowerPreviewRequest(eligible=10_000, base_rate=None, holdout_shares=(0.05,)))
    (point,) = preview.points
    assert point.mde_pp is None and point.cost_of_holdout is None
    assert point.reason and "base rate is not known" in point.reason
    assert point.cost_of_explore == MoneyRange(low=0.0, high=0.0)


@pytest.mark.parametrize("shares", [(0.0,), (0.6,), ()])
def test_the_preview_refuses_a_share_outside_its_range(shares: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        PowerPreviewRequest(eligible=10_000, base_rate=0.04, holdout_shares=shares)


def test_a_fall_is_easier_to_see_than_a_rise_with_a_small_control_group() -> None:
    """Review M93: the pooled test is asymmetric with an unbalanced holdout (20,000 eligible, 5% held
    back, 4% base rate: a rise of about 2.04 points, a fall of about 1.53), so the direction matters."""
    n_treat, n_control = arm_sizes(20_000, 0.05)
    up = mde_two_proportions(n_treat, n_control, 0.04, direction="up")
    down = mde_two_proportions(n_treat, n_control, 0.04, direction="down")
    assert up.points == pytest.approx(2.04, abs=0.01) and down.points == pytest.approx(1.53, abs=0.01)
    assert mde_two_proportions(n_treat, n_control, 0.04).points == up.points


@pytest.mark.parametrize("direction", ["up", "down", "either"])
def test_the_preview_plans_for_the_direction_asked(direction: str) -> None:
    request = PowerPreviewRequest(
        eligible=20_000, base_rate=0.04, holdout_shares=(0.05,), direction=direction  # type: ignore[arg-type]
    )
    (point,) = power_preview(request).points
    expected = mde_two_proportions(point.n_treat, point.n_control, 0.04, direction=direction)  # type: ignore[arg-type]
    assert point.mde_pp == pytest.approx(expected.points, abs=1e-4)
    assert PowerPreviewRequest(eligible=1, base_rate=0.1, holdout_shares=(0.1,)).direction == "either"
    assert not jargon_in(power_preview(request).basis)


def test_no_planner_message_uses_jargon() -> None:
    from engine.measurement import planner

    reasons = [value for name, value in vars(planner).items() if name.startswith("REASON_")]
    assert len(reasons) >= 10
    preview = power_preview(
        PowerPreviewRequest(eligible=100, base_rate=0.5, holdout_shares=(0.01, 0.5), explore_share=0.1)
    )
    texts = [*reasons, preview.basis, *(point.reason or "" for point in preview.points)]
    assert [text for text in texts if jargon_in(text)] == []
