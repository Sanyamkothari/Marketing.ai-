"""Amounts, not only yes/no: the difference in means and the adjusted estimate (Plan J M102, DEC-1312).

A campaign owner is judged on revenue, and a binary effect of one to three points is hard to see. This
module is the statistics `engine.uplift.incrementality.measure_incrementality(outcome_kind="continuous")`
calls: the difference between the contacted and held-back customers' average amount, with a Welch
interval, and the CUPED adjustment by an amount measured before the campaign, which narrows the
interval without moving what is estimated. It is pure: `numpy` arrays in, numbers out, no `pandas`,
no storage, no clock.

**Welch's interval.** With `n1` contacted customers whose amounts have mean `m1` and variance `s1²`
(the unbiased sample variance) and `n0` held-back ones (`m0`, `s0²`):

    d  = m1 - m0                       se = sqrt(s1²/n1 + s0²/n0)
    df = se⁴ / ((s1²/n1)²/(n1-1) + (s0²/n0)²/(n0-1))           (Welch-Satterthwaite)
    d ± t(0.975, df) · se              p = P(|T_df| ≥ |d| / se)

The arms' variances are not assumed equal: a campaign that changes who buys changes the spread too.
The t distribution is computed here (`t_two_sided`, `t_quantile`, through the regularised incomplete
beta function), so no statistics library is needed; with thousands of customers per arm it is the
normal distribution to four decimals. Each arm needs at least two customers with an amount, or there
is no variance and no interval.

**The adjusted estimate (CUPED: Deng, Xu, Kohavi and Walker, 2013).** `X` is an amount each customer
had *before* the campaign (last quarter's revenue). Because the campaign cannot have changed it, its
difference between the arms is chance alone, and subtracting the part of `Y` it predicts removes noise
without bias:

    theta = sum over arms of sum (x - x̄_arm)(y - ȳ_arm) / sum over arms of sum (x - x̄_arm)²
    y'    = y - theta · (x - x̄)                  (x̄: the mean of X over both arms)
    adjusted difference = mean(y'_1) - mean(y'_0) = d - theta · (x̄1 - x̄0)

`theta` is the within-arm regression slope, so a real campaign effect on `Y` does not leak into it.
The interval is Welch's on `y'`. `variance_reduction` is `1 - se'² / se²`: the share of the
difference's variance the adjustment removed. With a covariate correlated `rho` with the amount, it
is about `rho²` (0.36 at `rho = 0.6`), which is why a smaller control group sees the same effect. A
customer with no amount before (`X` unknown, for example a new customer) keeps their row: `X` is set
to the mean of the known values, which keeps the estimate unbiased because it does not depend on the
arm; they are counted in `rows_covariate_missing`. When `X` does not vary, there is nothing to adjust
with and the adjusted estimate is not given (the reason says so).

**Skewed amounts.** Revenue is mostly zeros with a long tail: a few customers spend a hundred times the
median. The interval rests on the average of many customers being close to normal, which a long tail
delays. Kohavi, Deng, Longbotham and Xu (2014, rule 7) give the size that suffices: each arm needs
more than `355 × g²` customers, where `g` is the skewness of the amounts in it. Below that, the report
keeps its numbers and carries `OUTCOME_SKEWED` with a plain sentence: the range may be too narrow. The
nightly suite (`tests/statistical/test_continuous_coverage.py`) checks the 95% coverage of both
intervals on a zero-inflated lognormal (80% of customers spend nothing) at the size the rule asks for.
Amounts are never capped or trimmed: that would change what is measured.

**Point in time.** The adjustment is honest only if `X` was fixed before the campaign. A covariate dated
on or after the day of a customer's treatment (compared by day: a value dated a day was measured up to
its end, so it may follow a contact earlier that day) could contain the campaign's own effect, and a
column with no date cannot be shown not to. Either way the measurement is refused (`COVARIATE_NOT_BEFORE_CAMPAIGN`,
`CovariateNotBeforeCampaignError`), never quietly run. `measure_incrementality` applies the rule;
`engine.measurement.measure.measure_campaign` adds that only a covariate the registered test plan
named in advance is used (`TEST_PLAN_CHANGED` otherwise).

Every sentence this module writes is plain language (`engine.pilot.plain.jargon_in` finds nothing in it).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

__all__ = [
    "CONTINUOUS_CODES",
    "COVARIATE_NOT_BEFORE_CAMPAIGN",
    "OUTCOME_SKEWED",
    "SKEW_RULE",
    "AdjustedDifference",
    "CovariateNotBeforeCampaignError",
    "MeanDifference",
    "OutcomeKind",
    "adjusted_difference",
    "mean_difference",
    "skewed_arms",
    "skewness",
    "t_quantile",
    "t_two_sided",
]

OutcomeKind = Literal["binary", "continuous"]
"""How an outcome is read: yes/no (a rate) or an amount (a mean)."""

COVARIATE_NOT_BEFORE_CAMPAIGN: Final[str] = "COVARIATE_NOT_BEFORE_CAMPAIGN"
"""The amount used to adjust is dated on or after the campaign, or not dated at all: refused."""
OUTCOME_SKEWED: Final[str] = "OUTCOME_SKEWED"
"""A warning on a report: a few very large amounts dominate an arm's average, so its range may be too narrow."""
CONTINUOUS_CODES: Final[frozenset[str]] = frozenset({COVARIATE_NOT_BEFORE_CAMPAIGN, OUTCOME_SKEWED})
"""M102's user-facing codes; `engine.decide.codes.PLAN_J_CODES` joins this set (one definition)."""

SKEW_RULE: Final[float] = 355.0
"""Customers per arm per unit of squared skewness the normal approximation needs (Kohavi et al. 2014, rule 7)."""

_MAX_ITERATIONS: Final[int] = 100_000
_TINY: Final[float] = 1e-300
_EPSILON: Final[float] = 1e-15
_BISECTION_STEPS: Final[int] = 200


class CovariateNotBeforeCampaignError(ValueError):
    """The adjustment's amount cannot be shown to come from before the campaign (`COVARIATE_NOT_BEFORE_CAMPAIGN`)."""

    code: Final[str] = COVARIATE_NOT_BEFORE_CAMPAIGN


# ---------------------------------------------------------------------------
# The t distribution, without a statistics library
# ---------------------------------------------------------------------------
def _beta_fraction(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function (modified Lentz's method)."""
    total, plus, minus = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - total * x / plus
    d = 1.0 / (d if abs(d) >= _TINY else _TINY)
    h = d
    for m in range(1, _MAX_ITERATIONS + 1):
        m2 = 2 * m
        for numerator in (
            m * (b - m) * x / ((minus + m2) * (a + m2)),
            -(a + m) * (total + m) * x / ((a + m2) * (plus + m2)),
        ):
            d = 1.0 + numerator * d
            d = 1.0 / (d if abs(d) >= _TINY else _TINY)
            c = 1.0 + numerator / c
            c = c if abs(c) >= _TINY else _TINY
            step = d * c
            h *= step
        if abs(step - 1.0) < _EPSILON:
            return h
    return h  # pragma: no cover - converges in at most a few hundred steps for these arguments


def _regularised_beta(a: float, b: float, x: float) -> float:
    """`I_x(a, b)`, the regularised incomplete beta function, for `a, b > 0` and `x` in [0, 1]."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_fraction(a, b, x) / a
    return 1.0 - front * _beta_fraction(b, a, 1.0 - x) / b


def t_two_sided(statistic: float, df: float) -> float:
    """`P(|T| >= |statistic|)` for Student's t with `df > 0` degrees of freedom (real `df` allowed)."""
    if not df > 0.0:
        raise ValueError("The degrees of freedom must be above 0.")
    if not math.isfinite(statistic):
        return 0.0
    return min(1.0, max(0.0, _regularised_beta(df / 2.0, 0.5, df / (df + statistic * statistic))))


def t_quantile(probability: float, df: float) -> float:
    """The `probability` quantile of Student's t with `df` degrees of freedom, for `probability` in (0.5, 1).

    Found by bisection on :func:`t_two_sided`: the `t > 0` whose two-sided tail is `2 (1 - probability)`.
    """
    if not 0.5 < probability < 1.0:
        raise ValueError("The probability must be between 0.5 and 1.")
    tail = 2.0 * (1.0 - probability)
    high = 2.0
    while t_two_sided(high, df) > tail:
        high *= 2.0
    low = 0.0
    for _ in range(_BISECTION_STEPS):
        middle = (low + high) / 2.0
        if t_two_sided(middle, df) > tail:
            low = middle
        else:
            high = middle
        if high - low <= 1e-12 * high:
            break
    return (low + high) / 2.0


# ---------------------------------------------------------------------------
# The difference in means and the adjusted estimate
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MeanDifference:
    """The contacted arm's mean minus the held-back arm's, with Welch's interval."""

    treated_mean: float
    control_mean: float
    difference: float
    low: float
    high: float
    standard_error: float
    df: float
    p_value: float | None


@dataclass(frozen=True, slots=True)
class AdjustedDifference:
    """The CUPED-adjusted difference, its Welch interval and the share of the variance it removed."""

    theta: float
    difference: float
    low: float
    high: float
    standard_error: float
    variance_reduction: float


def _welch(
    treated: NDArray[np.float64], control: NDArray[np.float64], confidence: float
) -> tuple[float, float, float, float, float, float | None]:
    """`(difference, low, high, se, df, p)` of two samples of at least two values each."""
    n1, n0 = len(treated), len(control)
    v1 = float(np.var(treated, ddof=1)) / n1
    v0 = float(np.var(control, ddof=1)) / n0
    difference = float(np.mean(treated)) - float(np.mean(control))
    variance = v1 + v0
    if variance <= 0.0:  # both arms constant: the difference is exact, and no test is meaningful
        return difference, difference, difference, 0.0, float(n1 + n0 - 2), None
    se = math.sqrt(variance)
    df = variance**2 / (v1**2 / (n1 - 1) + v0**2 / (n0 - 1))
    half = t_quantile(0.5 + confidence / 2.0, df) * se
    return difference, difference - half, difference + half, se, df, t_two_sided(difference / se, df)


def mean_difference(
    treated: NDArray[np.float64], control: NDArray[np.float64], *, confidence: float = 0.95
) -> MeanDifference | None:
    """Welch's comparison of the two arms' amounts; None when an arm has fewer than two amounts."""
    y1 = np.asarray(treated, dtype=np.float64)
    y0 = np.asarray(control, dtype=np.float64)
    if len(y1) < 2 or len(y0) < 2:
        return None
    difference, low, high, se, df, p_value = _welch(y1, y0, confidence)
    return MeanDifference(
        treated_mean=float(np.mean(y1)),
        control_mean=float(np.mean(y0)),
        difference=difference,
        low=low,
        high=high,
        standard_error=se,
        df=df,
        p_value=p_value,
    )


def adjusted_difference(
    treated: NDArray[np.float64],
    treated_covariate: NDArray[np.float64],
    control: NDArray[np.float64],
    control_covariate: NDArray[np.float64],
    *,
    confidence: float = 0.95,
) -> AdjustedDifference | None:
    """The CUPED estimate of the module docstring; None when an arm is too small or `X` does not vary.

    The covariates hold no missing value here: the caller fills an unknown one with the mean of the known.
    """
    y1, x1 = np.asarray(treated, dtype=np.float64), np.asarray(treated_covariate, dtype=np.float64)
    y0, x0 = np.asarray(control, dtype=np.float64), np.asarray(control_covariate, dtype=np.float64)
    if len(y1) < 2 or len(y0) < 2:
        return None
    dx1, dx0 = x1 - x1.mean(), x0 - x0.mean()
    spread = float(dx1 @ dx1 + dx0 @ dx0)
    if not spread > 0.0:
        return None
    theta = float(dx1 @ (y1 - y1.mean()) + dx0 @ (y0 - y0.mean())) / spread
    centre = float(np.concatenate([x1, x0]).mean())
    a1, a0 = y1 - theta * (x1 - centre), y0 - theta * (x0 - centre)
    difference, low, high, se, _, _ = _welch(a1, a0, confidence)
    _, _, _, raw_se, _, _ = _welch(y1, y0, confidence)
    reduction = 1.0 - (se * se) / (raw_se * raw_se) if raw_se > 0.0 else 0.0
    return AdjustedDifference(
        theta=theta,
        difference=difference,
        low=low,
        high=high,
        standard_error=se,
        variance_reduction=reduction,
    )


def skewness(values: NDArray[np.float64]) -> float | None:
    """The sample skewness `g = m3 / m2^1.5` (biased moments); None for fewer than three values or none varying."""
    y = np.asarray(values, dtype=np.float64)
    if len(y) < 3:
        return None
    centred = y - y.mean()
    m2 = float(np.mean(centred * centred))
    if not m2 > 0.0:
        return None
    return float(np.mean(centred**3)) / float(m2**1.5)


def skewed_arms(*arms: NDArray[np.float64]) -> bool:
    """True when some arm has fewer than `SKEW_RULE × g²` customers: its average is not yet close to normal."""
    for values in arms:
        g = skewness(values)
        if g is not None and len(values) < SKEW_RULE * g * g:
            return True
    return False
