"""Is the held-back control group big enough to see a real lift? Statistical power of the holdout.

A scoring run holds a random control group back from the eligible customers
(`actions.control_group_fraction`, `engine.stages.actions`). After the campaign,
`engine.uplift.incrementality` compares the treated and control conversion rates. This module answers
the question *before* the campaign: with `n` eligible customers, a control fraction `f` and a
baseline conversion rate `p0`, how small a lift can that comparison still reliably detect - and, the
other way round, how big must the control group be to detect a lift you care about. Pure functions of
numbers: no storage, no network, no pandas.

**The test it sizes.** The same one `incrementality.two_proportion_p_value` runs: the pooled
two-proportion z-test, two-sided, with `z = (p1 − p0) / sqrt(p̂(1 − p̂)(1/n_t + 1/n_c))`, `p̂` the pooled
rate. "Detect" means that test gives `p < alpha` (default 0.05, two-sided) with probability at least
`power` (default 0.80) when the true treated rate is `p0 + lift`.

**Formula.** With control rows `n_c`, treated rows `n_t`, `delta` the absolute lift, `p1 = p0 + delta`,
`p̄ = (n_t·p1 + n_c·p0)/(n_t + n_c)` (the pooled rate the test will see), `z_a = Φ⁻¹(1 − alpha/2)`:

    se0 = sqrt(p̄(1 − p̄)(1/n_t + 1/n_c))             spread of the statistic if there is no lift
    se1 = sqrt(p0(1 − p0)/n_c + p1(1 − p1)/n_t)       spread if the lift is real
    power(delta) = Φ((delta − z_a·se0)/se1) + Φ((−delta − z_a·se0)/se1)

The minimum detectable lift is the smallest `delta` with `power(delta) ≥ power`, found by bisection
(`power(delta)` rises with `delta`). The familiar closed form `(z_a + z_b)·sqrt(p0(1−p0)(1/n_t+1/n_c))`
is the same thing with `p1 ≈ p0` in both spreads; it understates the lift when it is large against
`p0`, so it is not used.

**Approximations, stated.** The normal approximation to the binomial (good when every arm expects at
least about 5 conversions and 5 non-conversions - below that the number is a guide, not a guarantee);
the baseline `p0` is taken as known, though in practice it is an estimate; the arms are the rows the
engine really draws (`round_half_up(n·f)` control rows, the rest treated), so the numbers match what
`actions` does; and only an *increase* in conversion is sized (a lift cannot exceed `1 − p0`). The
post-campaign Newcombe interval and this z-test agree closely but not exactly, so a lift sized here is
"about" detectable by the interval too. Customers are treated as independent rows: with several
snapshot rows per customer the real number of independent units is smaller, and the lift needed is a
little larger.

`MAX_CONTROL_FRACTION` mirrors `ActionsConfig.control_group_fraction`'s upper bound (0.50); a unit
test pins the two together. The browser has the same maths in `ui/modules/measure/power.js`; the two
are checked against the same cases (`tests/fixtures/power_cases.json`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Final

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_POWER",
    "MAX_CONTROL_FRACTION",
    "ControlSizing",
    "MinDetectableLift",
    "arm_rows",
    "min_control_fraction",
    "min_detectable_lift",
    "power_of_lift",
]

DEFAULT_ALPHA: Final[float] = 0.05
DEFAULT_POWER: Final[float] = 0.80
MAX_CONTROL_FRACTION: Final[float] = 0.50
"""The largest control fraction the configuration accepts (`actions.control_group_fraction`)."""

_NORMAL: Final[NormalDist] = NormalDist()
_BISECTION_STEPS: Final[int] = 80


@dataclass(frozen=True, slots=True)
class MinDetectableLift:
    """The smallest lift the holdout can reliably detect."""

    absolute: float
    """In rate points as a fraction: 0.012 is 1.2 percentage points."""
    relative: float | None
    """`absolute / p0`; `None` when the baseline is 0 (a relative lift of nothing is undefined)."""
    control_rows: int
    treated_rows: int


@dataclass(frozen=True, slots=True)
class ControlSizing:
    """The smallest control group that detects a target lift."""

    fraction: float
    """`control_rows / n`: giving this to `actions.control_group_fraction` draws exactly `control_rows`."""
    control_rows: int
    treated_rows: int


def arm_rows(n: int, fraction: float) -> tuple[int, int]:
    """`(control, treated)` rows as `actions` draws them: `round_half_up(n·f)` held out, the rest treated."""
    if n <= 0 or fraction <= 0.0:
        return 0, max(n, 0)
    control = min(n, math.floor(n * fraction + 0.5))
    return control, n - control


def _check_rates(p0: float, alpha: float, power: float) -> None:
    if not (math.isfinite(p0) and 0.0 <= p0 <= 1.0):
        raise ValueError("The baseline rate p0 must be between 0 and 1.")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between 0 and 1.")
    if not 0.0 < power < 1.0:
        raise ValueError("power must be between 0 and 1.")


def power_of_lift(
    n_treated: int, n_control: int, p0: float, lift: float, *, alpha: float = DEFAULT_ALPHA
) -> float:
    """Probability that the two-sided pooled z-test finds `p0 → p0 + lift` with arms of these sizes.

    `lift` is absolute and must keep `p0 + lift` inside `[0, 1]`. Returns 0.0 when an arm is empty.
    """
    if n_treated <= 0 or n_control <= 0:
        return 0.0
    p1 = p0 + lift
    if not 0.0 <= p1 <= 1.0:
        raise ValueError("p0 + lift must stay between 0 and 1.")
    z_alpha = _NORMAL.inv_cdf(1.0 - alpha / 2.0)
    pooled = (n_treated * p1 + n_control * p0) / (n_treated + n_control)
    se_null = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_treated + 1.0 / n_control))
    se_alt = math.sqrt(p0 * (1.0 - p0) / n_control + p1 * (1.0 - p1) / n_treated)
    threshold = z_alpha * se_null
    shift = abs(lift)
    if se_alt <= 0.0:
        return 1.0 if shift > threshold else 0.0
    return _NORMAL.cdf((shift - threshold) / se_alt) + _NORMAL.cdf((-shift - threshold) / se_alt)


def _smallest_detectable(
    n_treated: int, n_control: int, p0: float, alpha: float, power: float
) -> float | None:
    """Bisect for the smallest absolute lift with enough power; `None` when even `1 − p0` is too small."""
    room = 1.0 - p0
    if room <= 0.0 or n_treated <= 0 or n_control <= 0:
        return None
    if power_of_lift(n_treated, n_control, p0, room, alpha=alpha) < power:
        return None
    low, high = 0.0, room
    for _ in range(_BISECTION_STEPS):
        middle = (low + high) / 2.0
        if power_of_lift(n_treated, n_control, p0, middle, alpha=alpha) >= power:
            high = middle
        else:
            low = middle
    return high


def min_detectable_lift(
    n: int,
    fraction: float,
    p0: float,
    *,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> MinDetectableLift | None:
    """The smallest lift a holdout of `fraction` of `n` eligible customers detects with `power`.

    `None` when it cannot be said: an arm would be empty (tiny `n` or a fraction that rounds to no
    control rows), the baseline leaves no room to rise (`p0 = 1`), or even a jump to 100 % would not
    be detected. Raises `ValueError` for a `p0`, `alpha` or `power` outside their ranges, or a
    `fraction` outside `[0, 1]`.
    """
    _check_rates(p0, alpha, power)
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("The control fraction must be between 0 and 1.")
    control, treated = arm_rows(n, fraction)
    lift = _smallest_detectable(treated, control, p0, alpha, power)
    if lift is None:
        return None
    return MinDetectableLift(
        absolute=lift,
        relative=lift / p0 if p0 > 0.0 else None,
        control_rows=control,
        treated_rows=treated,
    )


def min_control_fraction(
    n: int,
    p0: float,
    lift: float,
    *,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> ControlSizing | None:
    """The smallest control group, as a fraction of `n`, that detects the absolute `lift` with `power`.

    Searches whole control rows from 1 up to `floor(n × MAX_CONTROL_FRACTION)` (power only grows as
    the arms balance, so a binary search is exact). `None` is "not possible even at the maximum
    fraction": the lift is larger than the baseline leaves room for, or even half the audience held
    back is not enough. Raises `ValueError` for `lift <= 0` or a rate outside its range.
    """
    _check_rates(p0, alpha, power)
    if not (math.isfinite(lift) and lift > 0.0):
        raise ValueError("The target lift must be greater than 0.")
    if p0 + lift > 1.0:
        return None
    largest = math.floor(n * MAX_CONTROL_FRACTION)
    if largest < 1 or n - largest < 1:
        return None

    def enough(control: int) -> bool:
        return power_of_lift(n - control, control, p0, lift, alpha=alpha) >= power

    if not enough(largest):
        return None
    low, high = 1, largest
    while low < high:
        middle = (low + high) // 2
        if enough(middle):
            high = middle
        else:
            low = middle + 1
    return ControlSizing(fraction=low / n, control_rows=low, treated_rows=n - low)
