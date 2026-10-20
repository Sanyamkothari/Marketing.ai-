"""Cross-checks against v1: run the same numbers through `engine.uplift.ope` and
`engine.measurement.continuous` so we can see that v2's proof engine agrees with v1's.

v1's modules pull in its logging, config and contract layers, so they are imported inside the
functions, never at module level: importing this bridge stays cheap, and the light v2 CI never needs
v1. Tests of this module carry `@pytest.mark.v1bridge`.

v1's OPE is for a yes/no treatment and a yes/no outcome; ours is for any option and any reward. The
binary case maps across exactly: the logged option is "treat" or "don't", its logging probability is
`e` or `1 - e`, the target's probability of it is `π1` or `1 - π1`, the reward model's prediction is
`μ1` or `μ0`, and its prediction under the target is `π1·μ1 + (1 - π1)·μ0`.

v1's mean difference uses Welch's t quantile; ours uses the normal one. For a few dozen customers
per group and more they agree to the second or third significant figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Estimate
from autonomy.proof.estimate import welch_difference
from autonomy.proof.ope import OpeReport, evaluate

__all__ = ["MeanDifferenceCheck", "OpeCheck", "compare_mean_difference", "compare_ope", "v2_binary_ope"]

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class OpeCheck:
    """v1's and v2's estimates of the same policy on the same rows, by method."""

    v1: dict[str, Estimate]
    v2: dict[str, Estimate]

    def max_gap(self) -> float:
        """The largest point-estimate gap over the methods both sides report."""
        shared = set(self.v1) & set(self.v2)
        return max((abs(self.v1[m].point - self.v2[m].point) for m in shared), default=0.0)


@dataclass(frozen=True)
class MeanDifferenceCheck:
    v1: Estimate | None
    v2: Estimate | None


def v2_binary_ope(
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    policy_treat: npt.ArrayLike,
    *,
    p_treated: npt.ArrayLike,
    p_control: npt.ArrayLike,
    propensity: float | npt.ArrayLike,
) -> OpeReport:
    """v2's IPS / SNIPS / DR for a yes/no treatment, in v1's terms."""
    treat = np.asarray(t, dtype=np.float64)
    pi1 = np.asarray(policy_treat, dtype=np.float64)
    mu1 = np.asarray(p_treated, dtype=np.float64)
    mu0 = np.asarray(p_control, dtype=np.float64)
    e = np.broadcast_to(np.asarray(propensity, dtype=np.float64), treat.shape)
    logged = np.where(treat == 1, e, 1.0 - e)
    target = np.where(treat == 1, pi1, 1.0 - pi1)
    q_logged = np.where(treat == 1, mu1, mu0)
    q_target = pi1 * mu1 + (1.0 - pi1) * mu0
    return evaluate(np.asarray(y, dtype=np.float64), logged, target, q_logged=q_logged, q_target=q_target)


def compare_ope(
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    policy_treat: npt.ArrayLike,
    *,
    p_treated: npt.ArrayLike,
    p_control: npt.ArrayLike,
    propensity: float | npt.ArrayLike,
    now: datetime,
) -> OpeCheck:
    """The same evaluation through v1's `evaluate_policy` and through v2's estimators."""
    from engine.uplift.ope import evaluate_policy

    t_arr = np.asarray(t, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    pi_arr = np.asarray(policy_treat, dtype=np.float64)
    mu1 = np.asarray(p_treated, dtype=np.float64)
    mu0 = np.asarray(p_control, dtype=np.float64)
    e: float | FloatArray = (
        float(propensity) if isinstance(propensity, int | float) else np.asarray(propensity, dtype=np.float64)
    )
    report = evaluate_policy(
        t_arr,
        y_arr,
        pi_arr,
        p_treated=mu1,
        p_control=mu0,
        propensity=e,
        run_id="v2-crosscheck",
        description="v2 cross-check",
        causal=True,
        now=now,
    )
    v1: dict[str, Estimate] = {}
    for est in report.estimates:
        cv = est.value
        low = cv.ci_low if cv.ci_low is not None else cv.value
        high = cv.ci_high if cv.ci_high is not None else cv.value
        v1[est.method] = Estimate(point=cv.value, low=min(low, cv.value), high=max(high, cv.value))

    ours = v2_binary_ope(t_arr, y_arr, pi_arr, p_treated=mu1, p_control=mu0, propensity=e)
    v2: dict[str, Estimate] = {"ips": ours.ips.value}
    if ours.snips is not None:
        v2["snips"] = ours.snips.value
    if ours.dr is not None:
        v2["dr"] = ours.dr.value
    return OpeCheck(v1=v1, v2=v2)


def compare_mean_difference(treated: npt.ArrayLike, control: npt.ArrayLike) -> MeanDifferenceCheck:
    """v1's Welch mean difference (t quantile) next to v2's (normal quantile)."""
    from engine.measurement.continuous import mean_difference

    a = np.asarray(treated, dtype=np.float64)
    b = np.asarray(control, dtype=np.float64)
    md = mean_difference(a, b)
    v1 = (
        None
        if md is None
        else Estimate(point=md.difference, low=min(md.low, md.difference), high=max(md.high, md.difference))
    )
    return MeanDifferenceCheck(v1=v1, v2=welch_difference(a, b))
