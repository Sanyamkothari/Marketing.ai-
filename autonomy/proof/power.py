"""What the holdout comparison can see at all: the minimum detectable effect (MDE).

A proof number with a 95% range answers "what did we measure". The MDE answers the question next to
it: "how big would a real gain have to be before this comparison could tell it from nothing". A shop
whose holdout is small cannot see a 5% lift however long it waits for one window, and saying so beside
the number stops a flat result being read as "the agent does nothing".

**The arithmetic** (two groups, two-sided 5% test, 80% power). The smallest true difference a test can
reliably pick out is `(z_{0.975} + z_{0.80}) x SE = 2.8016 x SE`, where `SE` is the standard error of
the difference of the two group means:

* **From the data** (`minimum_detectable_difference`): `SE` is Welch's, `sqrt(s1^2/n1 + s0^2/n0)`, the
  same one the proof's 95% range uses, so the MDE is in the unit of the number it sits next to (money
  per customer; multiply by the agent customers for the incremental figures).
* **Before the data** (`minimum_detectable_lift`): for "did the customer buy", the variance is
  `p(1 - p)` at the holdout's buy rate `p`, so the smallest relative lift is
  `2.8016 x sqrt(p(1 - p)(1/n_agent + 1/n_holdout)) / p`. With a 10% holdout against an 80% agent group
  this gives about 46% for 8,000 customers and a 5% buy rate, and about 7% for 300,000
  (docs/v3/ARCHITECTURE_REVIEW.md section 5c). `smallest_visible_lift` takes the shop size and the
  group shares instead of the group sizes.

**Pooling.** The proof sums every closed window of a customer into one figure
(`autonomy.proof.rewards.proof_rewards`), so its standard error already falls as months are added: the
MDE above is for the whole period the proof covers, not for one window. For planning, a longer period
means a higher buy rate per customer, which is what `minimum_detectable_lift` takes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist
from typing import Final

import numpy as np
import numpy.typing as npt

__all__ = [
    "MDE_MULTIPLIER",
    "POWER",
    "minimum_detectable_difference",
    "minimum_detectable_lift",
    "smallest_visible_lift",
]

FloatArray = npt.NDArray[np.float64]

POWER: Final[float] = 0.8
"""The chance a real effect of the minimum size is seen."""
Z_ALPHA: Final[float] = NormalDist().inv_cdf(0.975)
"""Two-sided 5% (the proof's 95% ranges)."""
Z_POWER: Final[float] = NormalDist().inv_cdf(POWER)
MDE_MULTIPLIER: Final[float] = Z_ALPHA + Z_POWER
"""2.8016: the smallest detectable difference in standard errors."""


def minimum_detectable_difference(
    agent: Sequence[float] | FloatArray, holdout: Sequence[float] | FloatArray
) -> float | None:
    """The smallest true difference of the means that this data could reliably tell from zero.

    `MDE_MULTIPLIER x` Welch's standard error of `mean(agent) - mean(holdout)`; None when either side
    has fewer than two values (no variance can be estimated) or neither side varies at all (nobody
    bought in either group, say): a standard error of 0 would claim any gain could be seen."""
    a, b = np.asarray(agent, dtype=np.float64), np.asarray(holdout, dtype=np.float64)
    if len(a) < 2 or len(b) < 2:
        return None
    se = math.sqrt(float(a.var(ddof=1)) / len(a) + float(b.var(ddof=1)) / len(b))
    if se <= 0.0:
        return None
    return MDE_MULTIPLIER * se


def minimum_detectable_lift(n_agent: int, n_holdout: int, base_rate: float) -> float | None:
    """The smallest relative lift in the buy rate that a comparison of two groups could see.

    `base_rate` is the share of the holdout that buys over the period measured. Returns a fraction
    (0.46 is 46%); None when a group is empty or the rate is not strictly between 0 and 1."""
    if n_agent < 1 or n_holdout < 1 or not 0.0 < base_rate < 1.0:
        return None
    se = math.sqrt(base_rate * (1.0 - base_rate) * (1.0 / n_agent + 1.0 / n_holdout))
    return MDE_MULTIPLIER * se / base_rate


def smallest_visible_lift(
    customers: int, base_rate: float, *, holdout_share: float = 0.1, baseline_share: float = 0.1
) -> float | None:
    """`minimum_detectable_lift` for a shop of `customers` split as the runtime splits them.

    The defaults are the runtime's (10% holdout, 10% baseline, so 80% in the agent group). A fraction."""
    if customers < 1 or holdout_share <= 0 or holdout_share + baseline_share >= 1:
        return None
    n_holdout = round(customers * holdout_share)
    n_agent = round(customers * (1.0 - holdout_share - baseline_share))
    return minimum_detectable_lift(n_agent, n_holdout, base_rate)
