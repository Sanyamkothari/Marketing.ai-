"""The "effect fading" card raises a false alarm at most one time in forty (Plan J M105, DEC-1315).

A use case's cycles are read as fading when the weighted trend of their measured effects, `engine.measurement
.summary.fading_verdict`, has its whole 95% range below zero: one tail, so with an effect that never moves
the chance of an alarm is 2.5%. This simulates 2,000 series of four cycles whose true effect is the same in
every cycle (`engine.measurement.simulate.population`), measures each cycle with the production chain
(`measure_campaign`), reads the noise of each from its own stored range and counts the series flagged. The
share must not exceed 2.5% by more than four Monte Carlo standard errors (`tests/statistical/bands.py`).

It also checks that the rule has power, on series whose effect really falls by three points a cycle.
"""

from __future__ import annotations

import logging

import pytest

from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import population
from engine.measurement.summary import CycleEffect, fading_verdict, interval_se
from tests.statistical.bands import band, seeds

pytestmark = pytest.mark.statistical

SIMS = 2_000
CYCLES = 4
ROWS = 5_000


def _cycle_effects(truth: list[float], base_seed: int, sim: int) -> list[CycleEffect]:
    effects: list[CycleEffect] = []
    cycle_seeds = seeds(base_seed + sim, len(truth))
    for effect, seed in zip(truth, cycle_seeds, strict=True):
        campaign = population(ROWS, 0.10, effect, seed=seed, control_share=0.2)
        report = measure_campaign(
            campaign.scores, campaign.outcomes, intended_column="intended_treatment", **campaign.measure_kwargs
        )
        lift = report.absolute_lift
        assert lift is not None and lift.ci_low is not None and lift.ci_high is not None
        se = interval_se(lift.ci_low, lift.ci_high, lift.confidence_level)
        assert se is not None
        effects.append(CycleEffect(value=lift.value, se=se))
    return effects


def test_a_use_case_whose_effect_never_moves_is_flagged_at_most_one_time_in_forty() -> None:
    flagged = 0
    logging.disable(logging.CRITICAL)
    try:
        for sim in range(SIMS):
            flagged += fading_verdict(_cycle_effects([0.05] * CYCLES, 105_001, sim)).fading
    finally:
        logging.disable(logging.NOTSET)
    _, high = band(0.025, SIMS)
    assert flagged / SIMS <= high, f"{flagged / SIMS:.4f} of flat series flagged, above {high:.4f}"


def test_a_use_case_whose_effect_falls_three_points_a_cycle_is_found() -> None:
    found = 0
    sims = 500
    logging.disable(logging.CRITICAL)
    try:
        for sim in range(sims):
            truth = [0.12 - 0.03 * index for index in range(CYCLES)]
            found += fading_verdict(_cycle_effects(truth, 105_002, sim)).fading
    finally:
        logging.disable(logging.NOTSET)
    assert found / sims >= 0.9, f"only {found / sims:.3f} of falling series found"
