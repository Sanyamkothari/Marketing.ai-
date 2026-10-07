"""The acceptance band every statistical test uses, in one place (Plan J M95).

**The rule.** A statistical test simulates `sims` independent campaigns and counts how often something
happens (the 95% interval holds the truth, a test at 5% rejects). If the true probability of that event
is `p`, the observed share has the Monte Carlo standard error

    se = sqrt(p * (1 - p) / sims)

and the test passes when the observed share is within **four** of them: `|observed - p| <= 4 * se`.
Four, not two, because a check that goes red one run in twenty for no reason gets loosened by whoever
is on call, and a loosened test proves nothing. The seeds are fixed, so a run is deterministic: it gives
the same answer every night. For a correct measurement, the chance that a given seed set lands outside
one four-SE band is about 6e-5 (1 in 16,000) per check, and about 1 in 2,000 across the suite's eight
or so checks, so a check that turns red after a change to the code very probably points at the change,
not at noise. The band is *derived from the number of simulations*, never
chosen by hand: a test that needs a wider band must run more simulations, not accept more error.

For the 95% interval and 2,000 simulations that is 95% +/- 1.95 points; for the 5% false-positive rate
and 10,000 simulations it is 5% +/- 0.87 points; for a mean (the bias test) the standard error is the
standard deviation of the simulated estimates over the square root of the simulations.

**What a band can detect.** The band is what keeps a *change to the code* that makes an interval too
narrow from passing unnoticed, and what stops a pass from depending on a lucky seed. Each 2,000-simulation
coverage case sees coverage that is off by more than about 1.95 points: an interval built at 90% nominal
fails every case, but one at 92.8% still passes the 20% case and is caught only by the 2%, 5% and
intention-to-treat cases and the false-positive check. The false-positive check, at +/- 0.87 points over
10,000 simulations, is the most sensitive guard against an interval that is too narrow; a narrowing of
less than about a point is below what this suite can see.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final

import numpy as np

K: Final[float] = 4.0
"""Monte Carlo standard errors either side of the nominal value that the observed value may differ by."""


def binomial_se(p: float, sims: int) -> float:
    """The Monte Carlo standard error of an observed share of `sims` independent trials with true rate `p`."""
    return math.sqrt(p * (1.0 - p) / sims)


def band(p: float, sims: int, *, k: float = K) -> tuple[float, float]:
    """`(low, high)`: `p` plus and minus `k` Monte Carlo standard errors."""
    half = k * binomial_se(p, sims)
    return p - half, p + half


def seeds(base: int, sims: int) -> Sequence[int]:
    """`sims` reproducible, well-spread seeds from the fixed `base`: simulation i always gets the same one."""
    return [int(s) for s in np.random.default_rng(base).integers(0, 2**32, size=sims)]


def assert_share_in_band(observed: float, p: float, sims: int, *, what: str) -> None:
    """Fail, naming the band and the standard error, unless `observed` is within four of them of `p`."""
    low, high = band(p, sims)
    assert low <= observed <= high, (
        f"{what}: observed {observed:.4f} is outside the band [{low:.4f}, {high:.4f}] "
        f"(nominal {p:.4f} +/- {K:g} x {binomial_se(p, sims):.4f}, {sims} simulations)"
    )


def assert_mean_in_band(estimates: Sequence[float], expected: float, *, what: str) -> None:
    """Fail unless the mean of `estimates` is within four standard errors of the mean of `expected`."""
    values = np.asarray(estimates, dtype=float)
    se = float(values.std(ddof=1) / math.sqrt(len(values)))
    mean = float(values.mean())
    assert abs(mean - expected) <= K * se, (
        f"{what}: mean {mean:.5f} differs from the expected {expected:.5f} by {abs(mean - expected):.5f}, "
        f"more than {K:g} x {se:.5f} ({len(values)} simulations)"
    )
