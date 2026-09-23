"""Off-policy evaluation: how a candidate targeting policy would have done on logged data (plan B §12).

A randomised experiment logs, for every customer, the treatment they got (`t`, drawn with a known
probability `e = P(t = 1 | x)`) and the outcome (`y`). That log answers questions about policies that
were never run: "had we treated only the top 20 % by predicted uplift, what conversion rate would we
have seen?". Phase 5's action-policy agent is judged this way, so the estimators are implemented
here once, exactly, and nowhere else.

**Notation.** `π(1 | x) = policy_treat` is the candidate policy's probability of treating the row
(0/1 for a deterministic rule), `π(0 | x) = 1 − π(1 | x)`; `p(A | x)` is the logging probability of
the action `A = t` actually taken, `e` or `1 − e`. The importance weight is `w = π(A | x) / p(A | x)`.
`μ1(x) = p_treated`, `μ0(x) = p_control` are outcome-model predictions from a model that did NOT see
these rows (the hold-out), so no row grades its own fit.

* **IPS** (inverse propensity scoring) `= mean(w·y)`: unbiased whenever the logging propensities
  are right - which they are by construction under random assignment.
* **SNIPS** (self-normalised IPS) `= Σ w·y / Σ w`: slightly biased, usually less variable, and it
  never leaves `[0, 1]` for a binary outcome. When no logged row took an action the policy would
  take (`Σ w = 0`) the ratio is 0/0 and SNIPS is left out of the report, never reported as 0.
* **DR** (doubly robust) `= mean(q̂π + w·(y − q̂(A)))`, with `q̂(A) = μ_A(x)` and
  `q̂π = π(1|x)·μ1(x) + π(0|x)·μ0(x)`: the outcome model's prediction of the policy's value, corrected
  by the weighted residuals. It stays unbiased if *either* the propensities or the outcome model are
  right, and a good outcome model shrinks its variance well below IPS's.

**Intervals** are normal approximations, 95 %: `mean ± z·sd/√n` (sample sd, `ddof = 1`) of the
per-row terms for IPS and DR; for SNIPS, the ratio's delta-method variance
`var(w·y − R·w) / (n · mean(w)²)` with `R` the SNIPS estimate. `treat_all` and `treat_none` are DR
estimates of the two trivial policies, the yardsticks any targeting policy is read against. With
fewer than two rows an interval cannot be estimated and is `None`, never a made-up band.

`z` is the exact normal quantile (`NormalDist().inv_cdf(0.975) ≈ 1.95996`), shared with the
incrementality report.

`numpy` is imported inside the function bodies, never at module level, so `import engine` stays
fast.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from engine.uplift.contracts import ConfidenceValue, OpeEstimate, OpeReport
from engine.uplift.incrementality import CONFIDENCE_LEVEL, Z_95
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    FloatArray = npt.NDArray[np.float64]

__all__ = ["evaluate_policy", "policy_from_rule"]

_LOGGER = get_logger(__name__)

_PROBABILITY_SLACK: Final[float] = 1e-9
"""Tolerance for probabilities a float computation nudged just outside `[0, 1]`."""


def evaluate_policy(
    t: np.ndarray,
    y: np.ndarray,
    policy_treat: np.ndarray,
    *,
    p_treated: np.ndarray,
    p_control: np.ndarray,
    propensity: float | np.ndarray,
    run_id: str,
    description: str,
    causal: bool,
    now: datetime | None = None,
) -> OpeReport:
    """IPS, SNIPS and DR estimates of `policy_treat`'s outcome rate on the logged rows.

    SNIPS is omitted from `estimates` when it is undefined (`Σ w = 0`); IPS and DR always appear.

    Raises `ValueError` when the arrays differ in length or are empty, `t` or `y` is not 0/1,
    `policy_treat` leaves `[0, 1]`, a logging propensity is not strictly inside `(0, 1)` (a row that
    could only ever get one action carries no information about the other) or an outcome prediction
    is not a finite probability.
    """
    import numpy as np

    started = time.perf_counter()
    treatment = _binary(t, name="t")
    outcome = _binary(y, name="y")
    rows = len(treatment)
    if rows == 0:
        raise ValueError("Off-policy evaluation needs at least one logged row.")
    pi1 = _probabilities(policy_treat, rows=rows, name="policy_treat")
    mu1 = _probabilities(p_treated, rows=rows, name="p_treated")
    mu0 = _probabilities(p_control, rows=rows, name="p_control")
    if len(outcome) != rows:
        raise ValueError(f"t has {rows} rows but y has {len(outcome)}; they must describe the same rows.")
    logged = np.broadcast_to(np.asarray(propensity, dtype=np.float64), (rows,)).astype(np.float64)
    if not np.all(np.isfinite(logged)) or np.any(logged <= 0.0) or np.any(logged >= 1.0):
        raise ValueError(
            "Every logging propensity must be strictly between 0 and 1; a row that could only get one "
            "action says nothing about the other."
        )

    snips = _snips(treatment, outcome, pi1, logged)
    estimates = (
        OpeEstimate(method="ips", value=_ips(treatment, outcome, pi1, logged)),
        *(() if snips is None else (OpeEstimate(method="snips", value=snips),)),
        OpeEstimate(method="dr", value=_dr(treatment, outcome, pi1, logged, mu1, mu0)),
    )
    ones = np.ones(rows, dtype=np.float64)
    report = OpeReport(
        run_id=run_id,
        policy_description=description,
        rows=rows,
        policy_treat_share=float(pi1.mean()),
        propensity=float(logged.mean()),
        estimates=estimates,
        logged_value=float(outcome.mean()),
        treat_all_value=_dr(treatment, outcome, ones, logged, mu1, mu0),
        treat_none_value=_dr(treatment, outcome, ones * 0.0, logged, mu1, mu0),
        causal=causal,
        computed_at=now if now is not None else datetime.now(UTC),
    )
    _LOGGER.info("ope rows=%d causal=%s", rows, causal)
    log_stage(_LOGGER, "ope", rows=rows, seconds=time.perf_counter() - started)
    return report


def policy_from_rule(
    uplift: np.ndarray, *, top_share: float | None = None, min_uplift: float | None = None
) -> tuple[np.ndarray, str]:
    """A deterministic policy (`π(1 | x)` of 0.0 or 1.0 per row) and its description in words.

    `top_share` treats the top `ceil(top_share·n)` rows by predicted uplift, counted by
    `engine.uplift.metrics.top_rows` so that `0.07·100` is 7 rows here exactly as in uplift@7%
    (stable sort on `-uplift`, so ties keep input order, as in `engine.uplift.metrics`); `min_uplift` treats every
    row whose predicted uplift is at least that value. Given both, a row must pass both. At least one
    is required.
    """
    import numpy as np

    from engine.uplift.metrics import top_rows

    if top_share is None and min_uplift is None:
        raise ValueError("A policy rule needs top_share, min_uplift or both.")
    scores = np.asarray(uplift, dtype=np.float64)
    if scores.ndim != 1 or not np.all(np.isfinite(scores)):
        raise ValueError("Predicted uplift must be a one-dimensional array of finite numbers.")
    treat = np.ones(len(scores), dtype=bool)
    parts: list[str] = []
    if top_share is not None:
        if not 0.0 < top_share <= 1.0:
            raise ValueError("top_share must be greater than 0 and at most 1.")
        count = top_rows(top_share, len(scores)) if len(scores) else 0
        order = np.argsort(-scores, kind="mergesort")
        top = np.zeros(len(scores), dtype=bool)
        top[order[:count]] = True
        treat &= top
        parts.append(f"the top {top_share * 100:g}% of customers by predicted uplift")
    if min_uplift is not None:
        if not math.isfinite(min_uplift):
            raise ValueError("min_uplift must be a finite number.")
        treat &= scores >= min_uplift
        rule = f"predicted uplift of at least {min_uplift * 100:+.1f} points"
        parts.append(f"customers with a {rule}" if top_share is None else f"only those with a {rule}")
    return treat.astype(np.float64), "Treat " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Estimators
# ---------------------------------------------------------------------------
def _weights(t: FloatArray, pi1: FloatArray, e: FloatArray) -> FloatArray:
    """`w = π(A | x) / p(A | x)` for the logged action `A = t`."""
    import numpy as np

    weights: FloatArray = np.where(t == 1.0, pi1 / e, (1.0 - pi1) / (1.0 - e))
    return weights


def _mean_interval(terms: FloatArray) -> ConfidenceValue:
    """`mean ± z·sd/√n` of per-row terms; no interval with fewer than two rows."""
    value = float(terms.mean())
    if len(terms) < 2:
        return ConfidenceValue(value=value, confidence_level=CONFIDENCE_LEVEL)
    half = Z_95 * float(terms.std(ddof=1)) / math.sqrt(len(terms))
    return ConfidenceValue(
        value=value, ci_low=value - half, ci_high=value + half, confidence_level=CONFIDENCE_LEVEL
    )


def _ips(t: FloatArray, y: FloatArray, pi1: FloatArray, e: FloatArray) -> ConfidenceValue:
    return _mean_interval(_weights(t, pi1, e) * y)


def _snips(t: FloatArray, y: FloatArray, pi1: FloatArray, e: FloatArray) -> ConfidenceValue | None:
    """`Σ w·y / Σ w` with a delta-method interval; `None` when `Σ w = 0`.

    With `Σ w = 0` no logged row took an action the policy would take, so the ratio is 0/0: there
    is no estimate, and the report leaves SNIPS out rather than print a made-up 0 (an
    `OpeEstimate.value` cannot be null).
    """
    weights = _weights(t, pi1, e)
    mean_weight = float(weights.mean())
    if mean_weight <= 0.0:
        return None
    ratio = float((weights * y).sum() / weights.sum())
    rows = len(weights)
    if rows < 2:
        return ConfidenceValue(value=ratio, confidence_level=CONFIDENCE_LEVEL)
    residual = weights * y - ratio * weights
    half = Z_95 * math.sqrt(float(residual.var(ddof=1)) / rows) / mean_weight
    return ConfidenceValue(
        value=ratio, ci_low=ratio - half, ci_high=ratio + half, confidence_level=CONFIDENCE_LEVEL
    )


def _dr(
    t: FloatArray, y: FloatArray, pi1: FloatArray, e: FloatArray, mu1: FloatArray, mu0: FloatArray
) -> ConfidenceValue:
    import numpy as np

    q_policy = pi1 * mu1 + (1.0 - pi1) * mu0
    q_logged = np.where(t == 1.0, mu1, mu0)
    return _mean_interval(q_policy + _weights(t, pi1, e) * (y - q_logged))


# ---------------------------------------------------------------------------
# Input checks
# ---------------------------------------------------------------------------
def _binary(values: np.ndarray, *, name: str) -> FloatArray:
    import numpy as np

    array = np.asarray(values)
    if array.ndim != 1:
        raise ValueError(f"{name} must be a one-dimensional array.")
    as_float = array.astype(np.float64)
    if not np.all((as_float == 0.0) | (as_float == 1.0)):
        raise ValueError(f"{name} must hold only 0 and 1.")
    return as_float


def _probabilities(values: np.ndarray, *, rows: int, name: str) -> FloatArray:
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    if array.shape != (rows,):
        raise ValueError(f"{name} must have one value per logged row ({rows}).")
    if (
        not np.all(np.isfinite(array))
        or np.any(array < -_PROBABILITY_SLACK)
        or np.any(array > 1.0 + _PROBABILITY_SLACK)
    ):
        raise ValueError(f"{name} must hold probabilities between 0 and 1.")
    clipped: FloatArray = np.clip(array, 0.0, 1.0)
    return clipped
