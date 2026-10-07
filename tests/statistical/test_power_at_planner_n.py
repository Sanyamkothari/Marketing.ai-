"""Achieved power at the planner's n is the 80% it promises (Plan J M95, needs M93's planner).

`engine/measurement/planner.py` (M93) says how many customers per arm a test needs to detect an effect of
a given size with 80% power at 5%. That promise is checked here the only way it can be: simulate many
campaigns of exactly that size with exactly that effect, decide each with the measurement's own rule (the
Newcombe 95% interval excluding zero, as `measure_incrementality` reports it), and count how often the
effect is found. The share must be within four Monte Carlo standard errors of 0.80
(`tests/statistical/bands.py`: 0.80 +/- 3.6 points at 2,000 simulations) - or the planner is corrected.

**Guarded.** The tests below are defined only when `engine.measurement.planner` can be found: without it
the module defines nothing, so nothing is collected and nothing is reported as skipped. M93 is merged, so
they are collected and run. They call the planner's merged API: `n_for_mde` (signed change, returning
`ArmSizes`), `achieved_power` (signed change, returning `PowerEstimate`) and `power_preview` (the numbers the
"Plan the test" card shows), so the promise checked is the one the product makes.
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

import pytest

from tests.statistical.bands import assert_share_in_band
from tests.statistical.campaigns import measure_many

pytestmark = pytest.mark.statistical

PLANNER_MODULE = "engine.measurement.planner"
PLANNER_PRESENT = importlib.util.find_spec(PLANNER_MODULE) is not None

SIMS = 2_000
TARGET_POWER = 0.80
ALPHA = 0.05
CASES = [
    # (base rate, effect, seed): the plan's two worked examples, 4% -> 3% and 10% -> 8% per arm.
    (0.04, -0.01, 95_030),
    (0.10, -0.02, 95_031),
]

if PLANNER_PRESENT:
    planner: Any = importlib.import_module(PLANNER_MODULE)

    def _n_per_arm(base_rate: float, effect: float) -> int:
        """Customers per arm the planner asks for to see `effect` (signed) with 80% power, equal arms."""
        sizes = planner.n_for_mde(base_rate, effect, alpha=ALPHA, power=TARGET_POWER, control_ratio=1.0)
        assert sizes.reason is None and sizes.n_treat is not None and sizes.n_control == sizes.n_treat
        return int(sizes.n_treat)

    def _planner_power(base_rate: float, effect: float, n: int) -> float:
        """The planner's own power for a change of `effect` (signed) with `n` customers in each arm."""
        estimate = planner.achieved_power(n, n, base_rate, effect, alpha=ALPHA)
        assert estimate.reason is None and estimate.power is not None
        return float(estimate.power)

    @pytest.mark.parametrize(("base_rate", "effect", "seed"), CASES, ids=["4pct-to-3pct", "10pct-to-8pct"])
    def test_the_measurement_finds_the_planned_effect_with_the_planned_power(
        base_rate: float, effect: float, seed: int
    ) -> None:
        n = _n_per_arm(base_rate, effect)
        draws = measure_many(seed, SIMS, n=2 * n, base_rate=base_rate, effect=effect, control_share=0.5)
        found = sum(
            d.report.absolute_lift is not None and d.report.absolute_lift.excludes_zero for d in draws
        )
        assert_share_in_band(found / SIMS, TARGET_POWER, SIMS, what=f"achieved power at n={n} per arm")

    @pytest.mark.parametrize(("base_rate", "effect", "seed"), CASES, ids=["4pct-to-3pct", "10pct-to-8pct"])
    def test_the_planners_own_power_at_its_n_is_the_target(
        base_rate: float, effect: float, seed: int
    ) -> None:
        del seed
        n = _n_per_arm(base_rate, effect)
        assert _planner_power(base_rate, effect, n) == pytest.approx(TARGET_POWER, abs=0.01)

    @pytest.mark.parametrize(("base_rate", "effect", "seed"), CASES, ids=["4pct-to-3pct", "10pct-to-8pct"])
    def test_the_plan_preview_at_the_planners_n_shows_the_planned_effect(
        base_rate: float, effect: float, seed: int
    ) -> None:
        """The card's number agrees with the planner: at 2n customers split in half, the smallest fall
        the test is sure to see is the planned one (no larger; at most a hair smaller, since n is rounded up).
        """
        del seed
        n = _n_per_arm(base_rate, effect)
        preview = planner.power_preview(
            planner.PowerPreviewRequest(
                eligible=2 * n,
                base_rate=base_rate,
                holdout_shares=(0.5,),
                alpha=ALPHA,
                power=TARGET_POWER,
                direction="down" if effect < 0 else "up",
            )
        )
        (point,) = preview.points
        assert (point.n_treat, point.n_control) == (n, n)
        assert point.mde_pp is not None
        assert abs(effect) * 100.0 - 0.01 <= point.mde_pp <= abs(effect) * 100.0 + 1e-6
