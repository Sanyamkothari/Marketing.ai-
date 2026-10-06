"""The control-group power calculator (`engine/uplift/power.py`) against hand-derived values.

The expected numbers do not come from the function under test: the equal-arm sample size is the
textbook closed form written out below, and a simulation draws arms and runs the very test
`engine.uplift.incrementality.two_proportion_p_value` runs after a campaign.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any, Final

import numpy as np
import pytest

from engine.config import ActionsConfig
from engine.uplift.incrementality import two_proportion_p_value
from engine.uplift.power import (
    MAX_CONTROL_FRACTION,
    arm_rows,
    min_control_fraction,
    min_detectable_lift,
    power_of_lift,
)

CASES: Final[Path] = Path(__file__).resolve().parents[2] / "fixtures" / "power_cases.json"
Z: Final[NormalDist] = NormalDist()


def equal_arm_sample_size(p0: float, p1: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Textbook per-arm size for two proportions, pooled variance under the null (Fleiss, no correction)."""
    z_a, z_b = Z.inv_cdf(1 - alpha / 2), Z.inv_cdf(power)
    pbar = (p0 + p1) / 2
    top = z_a * math.sqrt(2 * pbar * (1 - pbar)) + z_b * math.sqrt(p0 * (1 - p0) + p1 * (1 - p1))
    return math.ceil(top**2 / (p1 - p0) ** 2)


def test_power_flips_exactly_at_the_textbook_sample_size() -> None:
    n = equal_arm_sample_size(0.10, 0.12)
    assert 3800 <= n <= 3900  # the familiar "about 3,840 per arm" for 10% against 12%
    assert power_of_lift(n, n, 0.10, 0.02) >= 0.80
    assert power_of_lift(n - 1, n - 1, 0.10, 0.02) < 0.80


def test_min_detectable_lift_is_the_textbook_lift_for_equal_arms() -> None:
    n = equal_arm_sample_size(0.10, 0.12)
    found = min_detectable_lift(2 * n, 0.5, 0.10)
    assert found is not None
    assert found.control_rows == found.treated_rows == n
    assert found.absolute == pytest.approx(0.02, abs=2e-4)
    assert found.relative == pytest.approx(found.absolute / 0.10)


def test_the_closed_form_is_close_but_never_above() -> None:
    # (z_a + z_b) * sqrt(p0 q0 (1/nt + 1/nc)) assumes p1 = p0; the exact lift is a little larger.
    found = min_detectable_lift(10000, 0.1, 0.1)
    assert found is not None
    closed = (Z.inv_cdf(0.975) + Z.inv_cdf(0.8)) * math.sqrt(0.1 * 0.9 * (1 / 9000 + 1 / 1000))
    assert closed < found.absolute < closed * 1.15
    assert (found.control_rows, found.treated_rows) == (1000, 9000)


def test_a_simulation_with_the_incrementality_test_finds_the_lift_about_eight_times_in_ten() -> None:
    found = min_detectable_lift(10000, 0.1, 0.1)
    assert found is not None
    rng = np.random.default_rng(20261006)
    runs = 3000
    treated = rng.binomial(found.treated_rows, 0.1 + found.absolute, runs)
    control = rng.binomial(found.control_rows, 0.1, runs)
    hits = 0
    for x1, x2 in zip(treated.tolist(), control.tolist(), strict=True):
        p_value = two_proportion_p_value(x1, found.treated_rows, x2, found.control_rows)
        up = x1 / found.treated_rows > x2 / found.control_rows
        hits += 1 if p_value is not None and p_value < 0.05 and up else 0
    assert hits / runs == pytest.approx(0.80, abs=0.03)


def test_more_control_or_more_audience_detects_smaller_lifts() -> None:
    lifts = [min_detectable_lift(10000, f, 0.1) for f in (0.05, 0.1, 0.2, 0.3, 0.5)]
    absolute = [x.absolute for x in lifts if x is not None]
    assert len(absolute) == 5 and absolute == sorted(absolute, reverse=True)
    small, big = min_detectable_lift(2000, 0.1, 0.1), min_detectable_lift(20000, 0.1, 0.1)
    assert small is not None and big is not None and big.absolute < small.absolute


def test_stricter_alpha_or_power_needs_a_bigger_lift() -> None:
    base = min_detectable_lift(10000, 0.1, 0.1)
    strict_alpha = min_detectable_lift(10000, 0.1, 0.1, alpha=0.01)
    strict_power = min_detectable_lift(10000, 0.1, 0.1, power=0.9)
    assert base is not None and strict_alpha is not None and strict_power is not None
    assert strict_alpha.absolute > base.absolute
    assert strict_power.absolute > base.absolute


def test_arms_are_drawn_the_way_actions_draws_them() -> None:
    assert arm_rows(10, 0.25) == (3, 7)  # 2.5 rounds half up
    assert arm_rows(10, 0.04) == (0, 10)  # rounds to no control group
    assert arm_rows(0, 0.1) == (0, 0)
    assert arm_rows(5, 0.5) == (3, 2)


def test_tiny_audience_or_no_control_rows_is_not_estimable() -> None:
    assert min_detectable_lift(10, 0.04, 0.1) is None  # rounds to zero control rows
    assert min_detectable_lift(1, 0.5, 0.1) is None
    assert min_detectable_lift(0, 0.1, 0.1) is None
    assert min_detectable_lift(30, 0.1, 0.9) is None  # 3 vs 27 customers, 10 points of room: not enough


def test_baseline_edges() -> None:
    zero = min_detectable_lift(10000, 0.1, 0.0)
    assert zero is not None and zero.relative is None and 0 < zero.absolute < 0.01
    assert min_detectable_lift(10000, 0.1, 1.0) is None  # nowhere to rise
    nearly = min_detectable_lift(10000, 0.1, 0.99)
    assert nearly is not None and nearly.absolute <= 0.01 + 1e-12
    assert power_of_lift(10, 10, 0.0, 1.0) == 1.0  # 0 -> 100%: the spread under the alternative is zero


def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        min_detectable_lift(100, 0.1, 1.5)
    with pytest.raises(ValueError):
        min_detectable_lift(100, 0.1, float("nan"))
    with pytest.raises(ValueError):
        min_detectable_lift(100, 1.5, 0.1)
    with pytest.raises(ValueError):
        min_detectable_lift(100, 0.1, 0.1, alpha=0.0)
    with pytest.raises(ValueError):
        min_detectable_lift(100, 0.1, 0.1, power=1.0)
    with pytest.raises(ValueError):
        min_control_fraction(100, 0.1, 0.0)
    with pytest.raises(ValueError):
        power_of_lift(10, 10, 0.9, 0.5)


def test_smallest_control_fraction_is_the_inverse() -> None:
    sizing = min_control_fraction(10000, 0.1, 0.03)
    assert sizing is not None
    assert sizing.control_rows == 1021  # a 10% holdout detects 3.03 points, so 3.00 needs a little more
    assert sizing.fraction == pytest.approx(0.1021)
    assert sizing.treated_rows == 10000 - 1021
    assert power_of_lift(sizing.treated_rows, sizing.control_rows, 0.1, 0.03) >= 0.8
    assert power_of_lift(sizing.treated_rows + 1, sizing.control_rows - 1, 0.1, 0.03) < 0.8
    # the engine draws exactly that many rows from the fraction
    assert arm_rows(10000, sizing.fraction) == (sizing.control_rows, sizing.treated_rows)
    # round trip: the lift a holdout of that size detects is at most the target
    again = min_detectable_lift(10000, sizing.fraction, 0.1)
    assert again is not None and again.absolute <= 0.03 + 1e-9


def test_not_possible_even_at_the_maximum_fraction() -> None:
    assert min_control_fraction(10000, 0.1, 0.001) is None  # 0.1 point needs far more than half of 10,000
    assert min_control_fraction(10000, 0.1, 0.95) is None  # more than the baseline leaves room for
    assert min_control_fraction(1, 0.1, 0.5) is None
    at_max = min_detectable_lift(10000, MAX_CONTROL_FRACTION, 0.1)
    assert at_max is not None
    just_over = min_control_fraction(10000, 0.1, at_max.absolute * 1.0001)
    assert just_over is not None and just_over.fraction <= MAX_CONTROL_FRACTION
    assert min_control_fraction(10000, 0.1, at_max.absolute * 0.99) is None


def test_the_max_fraction_is_the_configuration_limit() -> None:
    field = ActionsConfig.model_fields["control_group_fraction"]
    limits = {getattr(m, "le", None) for m in field.metadata}
    assert MAX_CONTROL_FRACTION in limits


def test_the_shared_case_file_still_holds_what_python_computes() -> None:
    """`power.test.mjs` checks the browser against these; this keeps them honest against the engine."""
    cases: dict[str, list[dict[str, Any]]] = json.loads(CASES.read_text(encoding="utf-8"))
    for c in cases["min_detectable_lift"]:
        got = min_detectable_lift(c["n"], c["fraction"], c["p0"], alpha=c["alpha"], power=c["power"])
        if c["expected"] is None:
            assert got is None
            continue
        assert got is not None
        assert got.absolute == pytest.approx(c["expected"]["absolute"], rel=1e-12)
        assert (got.control_rows, got.treated_rows) == (
            c["expected"]["control_rows"],
            c["expected"]["treated_rows"],
        )
    for c in cases["min_control_fraction"]:
        sized = min_control_fraction(c["n"], c["p0"], c["lift"], alpha=c["alpha"], power=c["power"])
        if c["expected"] is None:
            assert sized is None
        else:
            assert sized is not None and sized.control_rows == c["expected"]["control_rows"]
    for c in cases["power_of_lift"]:
        got_power = power_of_lift(c["n_treated"], c["n_control"], c["p0"], c["lift"], alpha=c["alpha"])
        assert got_power == pytest.approx(c["expected"], rel=1e-12)
