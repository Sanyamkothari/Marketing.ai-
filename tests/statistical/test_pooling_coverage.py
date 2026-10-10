"""Pooled 95% ranges cover the true effects, and are narrower than each small campaign's own (Plan K M136).

Each simulation is one cell of comparable campaigns. Every campaign's true effect `τ_j` is drawn from a **mixture**,
not from the normal distribution the model assumes (half a point or two and a half points; or nothing, one point
or three), so the test asks whether the method holds up when its own prior is wrong. Each campaign contacts between
500 and 4,000 customers and holds back as many (fewer than 5,000 rows per arm, where pooling is meant to help), at a
5% base rate. Its measured difference and 95% range are the Newcombe interval of `measure_incrementality`
(`engine.uplift.incrementality.newcombe_interval`, the function every yes/no report's `absolute_lift` comes from),
on binomial counts drawn for the arms; the standard error is read from the range's width exactly as
`engine.measurement.pooling` reads a stored report, and the cell is fitted by `fit_cell`, the function
`refresh_pooled_estimates` uses.

What is required (design §9.2, DEC-1402):

* coverage: the share of campaigns whose pooled 95% range holds their true effect, averaged within each simulation
  and then over 2,000 simulations, is at least the lower edge of the DEC-1305 band (`tests/statistical/bands.py`:
  95% less four Monte Carlo standard errors). Within a cell the campaigns share one fitted average, so the
  simulation, not the campaign, is the independent unit the band counts;
* width: on average the pooled range is narrower than the campaign's own;
* a null world (no campaign has any effect): coverage at least the band's lower edge, and a pooled range that
  excludes zero (a false effect) at most the band's upper edge of 5%.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pytest

from engine.measurement.pooling import CellMember, fit_cell, se_from_interval
from engine.uplift.contracts import ConfidenceValue
from engine.uplift.incrementality import newcombe_interval
from tests.statistical.bands import band, seeds

pytestmark = pytest.mark.statistical

SIMS = 2_000
NOMINAL = 0.95
BASE_RATE = 0.05
ARM_ROWS = (500, 4_000)


@dataclass(frozen=True)
class CellResult:
    coverage: float
    """Share of the cell's campaigns whose pooled range holds the truth."""
    width_ratio: float
    """Mean of pooled width / own width over the cell's campaigns."""
    false_effects: float
    """Share of the cell's campaigns whose pooled range excludes zero."""
    raw_coverage: float


def _cell(seed: int, campaigns: int, effects: Sequence[float]) -> CellResult:
    rng = np.random.default_rng(seed)
    members: list[CellMember] = []
    truths: list[float] = []
    raw: list[tuple[float, float]] = []
    for index in range(campaigns):
        truth = float(rng.choice(effects))
        rows = int(rng.integers(*ARM_ROWS))
        treated = int(rng.binomial(rows, BASE_RATE + truth))
        held = int(rng.binomial(rows, BASE_RATE))
        value, low, high = newcombe_interval(treated, rows, held, rows)
        se = se_from_interval(ConfidenceValue(value=value, ci_low=low, ci_high=high))
        assert se is not None
        members.append(CellMember(f"c{index}", value, se))
        truths.append(truth)
        raw.append((low, high))
    fit = fit_cell(members)
    covered = widths = false = raw_covered = 0.0
    for member, truth, (raw_low, raw_high) in zip(fit.members, truths, raw, strict=True):
        low, high = member.interval()
        covered += low <= truth <= high
        widths += (high - low) / (raw_high - raw_low)
        false += low > 0.0 or high < 0.0
        raw_covered += raw_low <= truth <= raw_high
    return CellResult(covered / campaigns, widths / campaigns, false / campaigns, raw_covered / campaigns)


def _world(base_seed: int, campaigns: int, effects: Sequence[float]) -> list[CellResult]:
    return [_cell(seed, campaigns, effects) for seed in seeds(base_seed, SIMS)]


@pytest.mark.parametrize(
    ("campaigns", "effects", "seed"),
    [
        (8, (0.005, 0.025), 136_001),
        (6, (0.0, 0.01, 0.03), 136_002),
        (12, (0.005, 0.025), 136_003),
    ],
    ids=["eight-campaigns-two-groups", "six-campaigns-three-groups", "twelve-campaigns-two-groups"],
)
def test_pooled_ranges_cover_a_mixture_of_true_effects_and_are_narrower(
    campaigns: int, effects: tuple[float, ...], seed: int
) -> None:
    results = _world(seed, campaigns, effects)
    coverage = float(np.mean([result.coverage for result in results]))
    low, _ = band(NOMINAL, SIMS)
    assert coverage >= low, f"pooled coverage {coverage:.4f} is below the band's lower edge {low:.4f}"
    ratio = float(np.mean([result.width_ratio for result in results]))
    assert ratio < 1.0, f"pooled ranges are not narrower than the campaigns' own (mean ratio {ratio:.3f})"


def test_in_a_null_world_pooling_covers_and_invents_no_effect() -> None:
    results = _world(136_004, 8, (0.0,))
    coverage = float(np.mean([result.coverage for result in results]))
    false = float(np.mean([result.false_effects for result in results]))
    low, _ = band(NOMINAL, SIMS)
    _, high = band(0.05, SIMS)
    assert coverage >= low, f"null-world coverage {coverage:.4f} is below {low:.4f}"
    assert (
        false <= high
    ), f"pooled ranges excluded zero for {false:.4f} of campaigns with no effect (band {high:.4f})"


def test_the_campaigns_own_ranges_cover_as_the_measurement_promises() -> None:
    """A guard on the simulation itself: the unpooled Newcombe ranges hold the truth at their nominal rate."""
    results = _world(136_005, 8, (0.005, 0.025))
    raw = float(np.mean([result.raw_coverage for result in results]))
    low, high = band(NOMINAL, SIMS)
    assert low <= raw <= high
