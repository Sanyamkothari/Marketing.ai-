"""Always-valid intervals: reading an experiment as often as you like without fooling yourself.

A normal 95% interval is only honest when you look once, at a time fixed in advance. Look every week
and stop the first time it excludes zero, and the chance of a false alarm is far above 5%. An
experiment that stops early when an arm is "clearly losing" looks all the time, so it needs an
interval that stays valid at every look.

We use the normal-mixture sequential probability ratio test (mSPRT; Robbins 1970, Johari, Koomen,
Pekelis and Walsh 2017 "Peeking at A/B tests"). For a difference of means `d` with variance `V` of its
estimate and a N(0, tau^2) mixture over the true difference `theta`, the likelihood ratio against
`theta = 0` is

    L = sqrt(V / (V + tau^2)) * exp( tau^2 d^2 / (2 V (V + tau^2)) )

Under no difference `L` is a martingale with mean 1, so by Ville's inequality `P(L >= 1/alpha at ANY
look) <= alpha`. Inverting the test gives the interval `d +/- h` with

    h = sqrt( (2 V (V + tau^2) / tau^2) * ( ln(1/alpha) + 0.5 ln((V + tau^2) / V) ) )

which is valid at every look at once. The price is width: at the same sample it is wider than the
single-look 95% interval, which is exactly the cost of being allowed to look. Measured with `half_width`
on unit-variance data with n = 200 to 20,000 customers per arm: 1.55 to 1.9 times as wide when `tau`
is within a few times the standard error of the sample, and up to 6.4 times (n = 200, tau = 0.02) when
`tau` is far too small for the sample.

How exact is it? The guarantee is exact when the estimate behaves like a Gaussian random walk with
known variance. Here the variance is estimated (Welch) and customer profit is skewed (most customers
buy nothing), so it holds asymptotically, by the central limit theorem, the same way every A/B-testing
platform that uses mSPRT does. That is why the pick and stopping rules also demand a minimum number of
measured customers per arm (`PickRule.min_per_arm`) before any call, and why each look splits `alpha`
across all its comparisons (Bonferroni, which is valid whatever their dependence). `tau` only sets
where the interval is tightest; a wrong `tau` costs width, never validity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

__all__ = ["Interval", "half_width", "sequential_difference"]

FloatArray = npt.NDArray[np.float64]

_TINY = 1e-18


@dataclass(frozen=True)
class Interval:
    """A difference of means with its always-valid interval."""

    point: float
    low: float
    high: float


def half_width(variance: float, prior_sd: float, alpha: float) -> float:
    """`h` of the module docstring: half the width of the always-valid interval at level `alpha`.

    `variance` is the variance of the estimated difference; `prior_sd` is tau.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha is a probability strictly between 0 and 1")
    if prior_sd <= 0.0:
        raise ValueError("the mixture scale must be above zero")
    v = max(variance, _TINY)
    t2 = prior_sd * prior_sd
    return math.sqrt((2.0 * v * (v + t2) / t2) * (math.log(1.0 / alpha) + 0.5 * math.log((v + t2) / v)))


def sequential_difference(a: FloatArray, b: FloatArray, *, prior_sd: float, alpha: float) -> Interval | None:
    """`mean(a) - mean(b)` with its always-valid interval at level `alpha`.

    None when either side has fewer than two values (no variance can be estimated).
    """
    x, y = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if len(x) < 2 or len(y) < 2:
        return None
    diff = float(x.mean() - y.mean())
    variance = float(x.var(ddof=1)) / len(x) + float(y.var(ddof=1)) / len(y)
    h = half_width(variance, prior_sd, alpha)
    return Interval(point=diff, low=diff - h, high=diff + h)
