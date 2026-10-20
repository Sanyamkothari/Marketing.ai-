"""Off-policy evaluation: how a different policy would have done, read from decisions we logged.

Every decision is logged with the probability `p_i` that the logging policy had of choosing exactly
the option it chose (`Decision.probability`). For a target policy `π`, let `π_i` be the probability
that `π` would have chosen that same logged option for that same customer and moment. The importance
weight is `w_i = π_i / p_i`, and `r_i` is the logged reward.

* **IPS** (inverse propensity scoring) `V = mean(w·r)`. Unbiased when the assumptions below hold.
* **SNIPS** (self-normalised IPS) `V = Σ w·r / Σ w`. Slightly biased, usually much less variable, and
  never outside the range of the rewards. Undefined (None) when `Σ w = 0`: no logged option is one
  the target policy would ever choose.
* **DR** (doubly robust) `V = mean(q̂π_i + w_i·(r_i − q̂_i))`, where `q̂_i` is a reward model's
  prediction for the logged option and `q̂π_i = Σ_a π(a | x_i)·q̂(x_i, a)` its prediction for the
  target policy. Unbiased if *either* the logged probabilities or the reward model are right, and a
  decent model shrinks the variance well below IPS's.

**Intervals** are 95% normal approximations: `mean ± z·sd/√n` of the per-row terms for IPS and DR,
and the delta-method variance `var(w·r − V·w) / (n · mean(w)²)` for SNIPS. `effective_n` is Kish's
`(Σw)² / Σw²`: when it is a small share of `n`, a few rows carry the estimate and the interval is
optimistic.

**Assumptions (read before trusting a number).**

1. *The logged probabilities are right.* They are, by construction, when `decide()` computed them;
   they are not for decisions copied from a tool that didn't log them.
2. *Positivity.* Every option the target policy might choose had a probability above zero under the
   logging policy. The exploration floor exists to guarantee this. Holdout and baseline decisions
   break it (the holdout could only ever "do nothing"), so `from_decisions` uses agent decisions only.
3. *Same granularity.* `π_i` must be the probability of the same option the log's `p_i` refers to
   (channel, offer, content and send time together, as `decide()` chose them).
4. *No interference.* One customer's reward doesn't depend on what was done to another, and a
   reward is caused by its own decision. Overlapping outcome windows weaken this, so use
   window-attributed rewards (`build_rewards`, the default) and treat results as indicative when
   decisions for the same customer come close together.
5. *Clipping.* With `clip`, weights above it are capped. That lowers variance and adds bias toward
   the logging policy's value; leave it None for an unbiased estimate.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Decision, Estimate, Group, Reward
from autonomy.proof.estimate import Z_95

__all__ = [
    "LoggedBandit",
    "OpeReport",
    "OpeResult",
    "doubly_robust",
    "evaluate",
    "from_decisions",
    "ips",
    "snips",
]

FloatArray = npt.NDArray[np.float64]
OpeMethod = Literal["ips", "snips", "dr"]


@dataclass(frozen=True)
class OpeResult:
    """One estimate of the target policy's mean reward per decision."""

    method: OpeMethod
    value: Estimate
    n: int
    effective_n: float
    max_weight: float


@dataclass(frozen=True)
class OpeReport:
    """IPS, SNIPS (None when undefined) and DR (None without a reward model), plus the logged mean."""

    n: int
    logged_value: float
    ips: OpeResult
    snips: OpeResult | None
    dr: OpeResult | None


def _arrays(*named: tuple[str, object]) -> list[FloatArray]:
    out = [np.asarray(v, dtype=np.float64).reshape(-1) for _, v in named]
    n = len(out[0])
    for (name, _), arr in zip(named, out, strict=True):
        if len(arr) != n:
            raise ValueError(f"{name} has {len(arr)} rows but {named[0][0]} has {n}")
        if not np.all(np.isfinite(arr)):
            raise ValueError(f"{name} has a missing or infinite value")
    if n < 2:
        raise ValueError("off-policy evaluation needs at least two logged decisions")
    return out


def _weights(logged_prob: FloatArray, target_prob: FloatArray, clip: float | None) -> FloatArray:
    if np.any(logged_prob <= 0.0) or np.any(logged_prob > 1.0):
        raise ValueError("every logged probability must be in (0, 1]")
    if np.any(target_prob < 0.0) or np.any(target_prob > 1.0):
        raise ValueError("every target probability must be in [0, 1]")
    w: FloatArray = target_prob / logged_prob
    if clip is not None:
        if clip <= 0:
            raise ValueError("clip must be above zero")
        w = np.minimum(w, clip)
    return w


def _kish(w: FloatArray) -> float:
    sq = float(np.sum(w * w))
    return float(np.sum(w)) ** 2 / sq if sq > 0 else 0.0


def mean_interval(terms: FloatArray) -> Estimate:
    """Mean of per-row terms with a normal 95% interval (sample sd, ddof=1)."""
    point = float(terms.mean())
    half = Z_95 * float(terms.std(ddof=1)) / math.sqrt(len(terms))
    return Estimate(point=point, low=point - half, high=point + half)


def _result(method: OpeMethod, value: Estimate, w: FloatArray) -> OpeResult:
    return OpeResult(
        method=method, value=value, n=len(w), effective_n=_kish(w), max_weight=float(w.max(initial=0.0))
    )


def ips(
    rewards: npt.ArrayLike,
    logged_prob: npt.ArrayLike,
    target_prob: npt.ArrayLike,
    *,
    clip: float | None = None,
) -> OpeResult:
    """Inverse propensity scoring: `mean(w·r)`."""
    r, p, pi = _arrays(("rewards", rewards), ("logged_prob", logged_prob), ("target_prob", target_prob))
    w = _weights(p, pi, clip)
    return _result("ips", mean_interval(w * r), w)


def snips(
    rewards: npt.ArrayLike,
    logged_prob: npt.ArrayLike,
    target_prob: npt.ArrayLike,
    *,
    clip: float | None = None,
) -> OpeResult | None:
    """Self-normalised IPS: `Σ w·r / Σ w`; None when no logged option has target weight."""
    r, p, pi = _arrays(("rewards", rewards), ("logged_prob", logged_prob), ("target_prob", target_prob))
    w = _weights(p, pi, clip)
    total = float(w.sum())
    if total <= 0.0:
        return None
    point = float((w * r).sum()) / total
    mean_w = total / len(w)
    resid = w * r - point * w
    se = math.sqrt(float(resid.var(ddof=1)) / (len(w) * mean_w**2))
    return _result("snips", Estimate(point=point, low=point - Z_95 * se, high=point + Z_95 * se), w)


def doubly_robust(
    rewards: npt.ArrayLike,
    logged_prob: npt.ArrayLike,
    target_prob: npt.ArrayLike,
    q_logged: npt.ArrayLike,
    q_target: npt.ArrayLike,
    *,
    clip: float | None = None,
) -> OpeResult:
    """Doubly robust: `mean(q̂π + w·(r − q̂))`.

    `q_logged[i]` is the reward model's prediction for the logged option; `q_target[i]` is its
    prediction averaged over the target policy's choices for that row.
    """
    r, p, pi, q, qpi = _arrays(
        ("rewards", rewards),
        ("logged_prob", logged_prob),
        ("target_prob", target_prob),
        ("q_logged", q_logged),
        ("q_target", q_target),
    )
    w = _weights(p, pi, clip)
    return _result("dr", mean_interval(qpi + w * (r - q)), w)


def evaluate(
    rewards: npt.ArrayLike,
    logged_prob: npt.ArrayLike,
    target_prob: npt.ArrayLike,
    *,
    q_logged: npt.ArrayLike | None = None,
    q_target: npt.ArrayLike | None = None,
    clip: float | None = None,
) -> OpeReport:
    """All three estimates at once. DR needs both `q_logged` and `q_target`."""
    if (q_logged is None) != (q_target is None):
        raise ValueError("doubly robust needs both q_logged and q_target, or neither")
    r = np.asarray(rewards, dtype=np.float64).reshape(-1)
    dr = (
        doubly_robust(r, logged_prob, target_prob, q_logged, q_target, clip=clip)
        if q_logged is not None and q_target is not None
        else None
    )
    return OpeReport(
        n=len(r),
        logged_value=float(r.mean()) if len(r) else 0.0,
        ips=ips(r, logged_prob, target_prob, clip=clip),
        snips=snips(r, logged_prob, target_prob, clip=clip),
        dr=dr,
    )


@dataclass(frozen=True)
class LoggedBandit:
    """Logged decisions lined up with their rewards, as arrays for the estimators."""

    decisions: tuple[Decision, ...]
    rewards: FloatArray
    logged_prob: FloatArray

    def evaluate(
        self,
        target: Callable[[Decision], float],
        *,
        q: Callable[[Decision], tuple[float, float]] | None = None,
        clip: float | None = None,
    ) -> OpeReport:
        """Estimate a target policy's value on this log.

        `target(d)` is the probability the target policy gives to the option `d` logged. `q(d)`, if
        given, returns the reward model's `(q̂ for the logged option, q̂ averaged over the target)`.
        """
        pi = np.array([target(d) for d in self.decisions], dtype=np.float64)
        if q is None:
            return evaluate(self.rewards, self.logged_prob, pi, clip=clip)
        pairs = [q(d) for d in self.decisions]
        return evaluate(
            self.rewards,
            self.logged_prob,
            pi,
            q_logged=np.array([a for a, _ in pairs], dtype=np.float64),
            q_target=np.array([b for _, b in pairs], dtype=np.float64),
            clip=clip,
        )


def from_decisions(
    decisions: Iterable[Decision] | Mapping[str, Decision],
    rewards: Sequence[Reward],
    *,
    groups: tuple[Group, ...] = (Group.AGENT,),
    value: Callable[[Reward], float] = lambda r: r.net,
) -> LoggedBandit:
    """Pair each reward with its decision (agent group only, by default), in reward order.

    Rewards whose decision is missing or in another group are dropped. `value` picks what is being
    evaluated; the default is `Reward.net`.
    """
    by_id = dict(decisions) if isinstance(decisions, Mapping) else {d.decision_id: d for d in decisions}
    kept: list[tuple[Decision, float]] = []
    for r in rewards:
        d = by_id.get(r.decision_id)
        if d is not None and d.group in groups:
            kept.append((d, float(value(r))))
    return LoggedBandit(
        decisions=tuple(d for d, _ in kept),
        rewards=np.array([v for _, v in kept], dtype=np.float64),
        logged_prob=np.array([d.probability for d, _ in kept], dtype=np.float64),
    )
