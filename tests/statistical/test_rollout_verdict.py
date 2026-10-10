"""Safe to roll out: `safe` is said wrongly at most 5% of the time, and the lower bound holds (Plan K M137).

`engine.measurement.compare.rollout_verdict` reads the equal-budget comparison's paired per-row terms
(M96) as a one-sided verdict at 95%: `safe` when `n·mean − z·√n·sd > 0`. On the populations of
`tests/statistical/test_risk_comparison_coverage.py` (2,000 simulated populations of 2,000 randomised
customers each, effect known per customer, the same fast linear learners, `tests/statistical/
risk_comparison.py`), and with DEC-1305's band (`tests/statistical/bands.py`: four Monte Carlo standard
errors of the nominal rate):

a. **No better** (`effect="null"`: no one is affected, so the two rankings tie at zero, and
   `effect="risk"`: the effect grows with risk, so risk ranking is the right one): `safe` comes out in at
   most `1 − confidence` of the populations, within the band (5% + 1.95 points at 2,000).
b. **Truly better** (`effect="heterogeneous"`: an effect unrelated to risk): the share of `safe` is the
   verdict's power at this size. It is reported (`-s` prints it), not gated.
c. **The lower bound holds**: the truth, `sum((π_u − π_r) × tau)` for the two cross-fitted policies, is
   at or above the one-sided lower bound in at least `confidence` of the populations, within the band
   (95% − 1.95 points), in every population.
"""

from __future__ import annotations

import pytest

from tests.statistical.bands import band, seeds
from tests.statistical.risk_comparison import Draw, compare_once

pytestmark = pytest.mark.statistical

SIMS = 2_000
ROWS = 2_000
CONFIDENCE = 0.95


def _draws(effect: str, base_seed: int) -> list[Draw]:
    return [
        compare_once(seed, n=ROWS, effect=effect, rollout_confidence=CONFIDENCE)
        for seed in seeds(base_seed, SIMS)
    ]


def _shares(draws: list[Draw]) -> tuple[float, float]:
    safe = 0
    held = 0
    for draw in draws:
        verdict = draw.report.rollout
        assert verdict is not None and verdict.confidence == CONFIDENCE
        safe += verdict.outcome == "safe"
        held += draw.true_difference >= verdict.lower_bound_conversions
    return safe / len(draws), held / len(draws)


@pytest.mark.parametrize(
    ("effect", "base_seed"),
    [("null", 137_001), ("risk", 137_002)],
    ids=["null-effect", "risk-driven-effect"],
)
def test_safe_is_said_wrongly_at_most_one_minus_confidence_of_the_time(effect: str, base_seed: int) -> None:
    safe, held = _shares(_draws(effect, base_seed))
    print(f"\n[Rollout] {effect}: safe in {safe:.2%} of {SIMS:,}; lower bound held in {held:.2%}")
    _, high = band(1.0 - CONFIDENCE, SIMS)
    assert safe <= high, (
        f"safe on a population where uplift ranking is no better: {safe:.4f}, above the band's "
        f"{high:.4f} ({1.0 - CONFIDENCE:.2f} + 4 Monte Carlo standard errors, {SIMS} simulations)"
    )
    low, _ = band(CONFIDENCE, SIMS)
    assert held >= low, f"one-sided lower bound held in {held:.4f}, below the band's {low:.4f} ({effect})"


def test_where_uplift_ranking_is_truly_better_the_power_is_reported_and_the_bound_holds() -> None:
    safe, held = _shares(_draws("heterogeneous", 137_003))
    # (b) The power at 2,000 rows: reported, not gated.
    print(
        f"\n[Rollout] heterogeneous: power (share safe) {safe:.2%} of {SIMS:,}; lower bound held in {held:.2%}"
    )
    low, _ = band(CONFIDENCE, SIMS)
    assert (
        held >= low
    ), f"one-sided lower bound held in {held:.4f}, below the band's {low:.4f} (heterogeneous)"
