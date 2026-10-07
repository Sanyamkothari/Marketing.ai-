"""Run many simulated campaigns through the real measurement (Plan J M95).

Each simulation draws a campaign with `engine.measurement.simulate.population` from its own fixed seed
and measures it with `engine.uplift.incrementality.measure_incrementality` - the function the product
uses, called the way the product calls it, not a re-implementation of its statistics.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from engine.measurement.simulate import population
from engine.uplift.contracts import IncrementalityReport
from engine.uplift.incrementality import measure_incrementality
from tests.statistical.bands import seeds


@dataclass(frozen=True)
class Draw:
    """One simulated campaign's measurement and the truth it was drawn with."""

    report: IncrementalityReport
    true_itt: float
    immature_rows: int


@contextmanager
def _quiet() -> Iterator[None]:
    """Thousands of INFO lines from the measurement would bury the test report and slow it down."""
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def measure_many(
    base_seed: int, sims: int, *, n: int, base_rate: float, effect: float, **options: Any
) -> list[Draw]:
    """`sims` campaigns of `n` customers, simulation i from the i-th seed of `base_seed`, each measured.

    `options` go to `population` (`compliance`, `contamination`, `immature_share`, `control_share`).
    """
    draws: list[Draw] = []
    with _quiet():
        for seed in seeds(base_seed, sims):
            campaign = population(n, base_rate, effect, seed=seed, **options)
            report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
            draws.append(Draw(report, campaign.true_itt, int(campaign.immature.sum())))
    return draws


def covers(draw: Draw) -> bool:
    """Whether the 95% interval of the absolute lift holds the true ITT."""
    lift = draw.report.absolute_lift
    assert lift is not None and lift.ci_low is not None and lift.ci_high is not None
    return lift.ci_low <= draw.true_itt <= lift.ci_high
