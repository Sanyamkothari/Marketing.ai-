"""Replay a target policy on a public log and compare it with the policy that wrote the log.

`evaluate(policy_fn, logged)` answers: had this policy chosen the actions instead, what would the
average reward per row have been, and is that better than what the logging policy actually got?
The logging policy's value needs no estimate (it is the mean logged reward); the target's is
estimated off-policy with three estimators:

* **IPS** `mean(w·r)`, with `w = π(a|x) / p(a|x)` for the logged action `a`: unbiased when the
  logged propensities are right, which they are in all three public datasets by construction.
* **SNIPS** `Σ w·r / Σ w`: a little biased, usually steadier. Undefined (None) when the target never
  agrees with a logged action.
* **DR** `mean(q̂π(x) + w·(r − q̂(x, a)))`: a reward model's prediction of the policy's value,
  corrected by the weighted residuals. Unbiased if either the propensities or the model are right;
  a decent model shrinks the variance. The default model is a per-action mean, cross-fitted on two
  halves so no row grades its own fit.

**Target vs logging.** The difference is estimated row by row (the target's per-row term minus the
logged reward), so its interval accounts for both coming from the same rows. A policy "beats the
logging policy" when the DR difference's 95% interval is entirely above zero (plan-v2 section 8).

**Which OPE code runs.** The values come from `autonomy.proof.ope` (`ips`, `snips`,
`doubly_robust`), the same estimators that grade the live loop. Only the paired differences against
the logged reward are computed here.

**The uplift-style policy.** `fit_segment_policy` learns, on a train split, the best action per
segment (by the inverse-propensity-weighted mean reward of each action in that segment, shrunk
toward the action's overall mean so small segments don't chase noise). `replay` fits it on train
and evaluates it, and every fixed "always do action k" policy, on the test split.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.contracts import Estimate
from autonomy.proof import ope
from autonomy.proof.estimate import Z_95
from autonomy.proof.replay.datasets import FloatArray, LoggedBandit

__all__ = [
    "PolicyEstimates",
    "PolicyFn",
    "ReplayReport",
    "RewardModel",
    "SegmentPolicy",
    "evaluate",
    "fit_segment_policy",
    "fixed_action_policy",
    "per_action_mean_model",
    "replay",
]

PolicyFn = Callable[[LoggedBandit], FloatArray]
"""Given a log, the target policy's probability of every action on every row: shape (rows, actions)."""
RewardModel = Callable[[LoggedBandit], FloatArray]
"""Given a log, a predicted reward for every action on every row: shape (rows, actions)."""

_TOLERANCE: Final[float] = 1e-6


# --- estimates -----------------------------------------------------------------------------------


def _interval(terms: FloatArray) -> Estimate:
    """Mean of per-row terms with a normal 95% interval (sample sd, ddof=1)."""
    if len(terms) < 2:
        raise ValueError("An interval needs at least two rows.")
    return ope.mean_interval(terms)


def _paired_differences(
    reward: FloatArray, weight: FloatArray, q_logged: FloatArray, q_target: FloatArray
) -> tuple[Estimate, Estimate | None, Estimate]:
    """IPS, SNIPS and DR minus the logged reward, estimated row by row with 95% intervals.

    `autonomy.proof.ope` gives each value on its own; the gain over the logging policy needs the
    per-row terms side by side, so its interval reflects that both come from the same rows.
    """
    ips_terms = weight * reward
    dr_terms = q_target + weight * (reward - q_logged)
    snips_diff: Estimate | None = None
    mean_w = float(weight.mean())
    if mean_w > 0.0:
        value = float(ips_terms.sum() / weight.sum())
        # Influence function of SNIPS minus that of the logged mean (delta method).
        influence = (ips_terms - value * weight) / mean_w - (reward - reward.mean())
        point = value - float(reward.mean())
        half = Z_95 * float(influence.std(ddof=1)) / math.sqrt(len(reward))
        snips_diff = Estimate(point=point, low=point - half, high=point + half)
    return _interval(ips_terms - reward), snips_diff, _interval(dr_terms - reward)


# --- policies and reward models ------------------------------------------------------------------


def _check_policy(probs: FloatArray, logged: LoggedBandit, *, what: str) -> FloatArray:
    out = np.asarray(probs, dtype=np.float64)
    if out.shape != (logged.n_rows, logged.n_actions):
        raise ValueError(f"{what} must have shape {(logged.n_rows, logged.n_actions)}, got {out.shape}.")
    if not np.all(np.isfinite(out)):
        raise ValueError(f"{what} has missing or infinite values.")
    return out


def fixed_action_policy(action: int) -> PolicyFn:
    """A policy that always takes `action` (e.g. 0 = 'no e-mail' on Hillstrom)."""

    def policy(logged: LoggedBandit) -> FloatArray:
        if not 0 <= action < logged.n_actions:
            raise ValueError(f"action {action} is outside 0..{logged.n_actions - 1}.")
        probs = np.zeros((logged.n_rows, logged.n_actions), dtype=np.float64)
        probs[:, action] = 1.0
        return probs

    return policy


def _action_means(logged: LoggedBandit, rows: npt.NDArray[np.bool_]) -> FloatArray:
    """Inverse-propensity-weighted mean reward of each action over `rows` (overall mean if unseen)."""
    a, r, p = logged.actions[rows], logged.reward[rows], logged.propensity[rows]
    fallback = float(r.mean()) if len(r) else 0.0
    weights = np.bincount(a, weights=1.0 / p, minlength=logged.n_actions)
    totals = np.bincount(a, weights=r / p, minlength=logged.n_actions)
    seen = weights > 0
    means = np.full(logged.n_actions, fallback, dtype=np.float64)
    means[seen] = totals[seen] / weights[seen]
    return means


def per_action_mean_model(logged: LoggedBandit) -> FloatArray:
    """A deliberately plain reward model: each action's mean reward, cross-fitted on two halves.

    Rows in the even half get the means learned on the odd half and vice versa, so a row's own
    reward never feeds its own prediction (which would bias the DR correction toward zero).
    """
    even = np.arange(logged.n_rows) % 2 == 0
    out = np.empty((logged.n_rows, logged.n_actions), dtype=np.float64)
    if logged.n_rows < 2:
        out[:] = _action_means(logged, np.ones(logged.n_rows, dtype=bool))
        return out
    out[even] = _action_means(logged, ~even)
    out[~even] = _action_means(logged, even)
    return out


# --- evaluate ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyEstimates:
    """A target policy's estimated value on a log, and how it compares with the logging policy."""

    policy_name: str
    source: str
    reward_name: str
    n_rows: int
    logging_value: Estimate
    ips: Estimate
    snips: Estimate | None
    dr: Estimate
    ips_vs_logging: Estimate
    snips_vs_logging: Estimate | None
    dr_vs_logging: Estimate
    effective_sample_size: float
    """(Σw)² / Σw²: how many logged rows the estimate is really resting on."""
    max_weight: float

    @property
    def beats_logging(self) -> bool:
        """True when the DR difference's whole 95% interval is above zero."""
        return self.dr_vs_logging.low > 0.0

    @property
    def sentence(self) -> str:
        """One plain-English line for the replay report."""
        d = self.dr_vs_logging
        verdict = (
            "beats the logging policy"
            if self.beats_logging
            else (
                "does worse than the logging policy"
                if d.high < 0.0
                else "cannot be told apart from the logging policy"
            )
        )
        return (
            f"On {self.source} ({self.n_rows} rows), '{self.policy_name}' {verdict}: "
            f"{self.reward_name} per row {self.dr.point:.4g} vs {self.logging_value.point:.4g} logged "
            f"(difference {d.point:+.4g}, 95% interval {d.low:+.4g} to {d.high:+.4g})."
        )


def evaluate(
    policy_fn: PolicyFn,
    logged: LoggedBandit,
    *,
    reward_model: RewardModel | None = None,
    policy_name: str = "target",
) -> PolicyEstimates:
    """Estimate `policy_fn`'s value on `logged` (IPS, SNIPS, DR) and its gain over the logging policy.

    `policy_fn` returns the target's action probabilities per row; rows must sum to 1. The reward
    model defaults to `per_action_mean_model`. Raises ValueError on fewer than two rows or a
    malformed policy or model output.
    """
    if logged.n_rows < 2:
        raise ValueError("Off-policy evaluation needs at least two logged rows.")
    pi = _check_policy(policy_fn(logged), logged, what="The policy's probabilities")
    if np.any(pi < -_TOLERANCE) or not np.allclose(pi.sum(axis=1), 1.0, atol=_TOLERANCE):
        raise ValueError("The policy's probabilities must be non-negative and sum to 1 on every row.")
    pi = np.clip(pi, 0.0, 1.0)
    q = _check_policy((reward_model or per_action_mean_model)(logged), logged, what="The reward model")

    rows = np.arange(logged.n_rows)
    pi_logged = pi[rows, logged.actions]
    q_logged = q[rows, logged.actions]
    q_target = (pi * q).sum(axis=1)
    r, p = logged.reward, logged.propensity

    ips = ope.ips(r, p, pi_logged)
    snips = ope.snips(r, p, pi_logged)
    dr = ope.doubly_robust(r, p, pi_logged, q_logged, q_target)
    ips_diff, snips_diff, dr_diff = _paired_differences(r, pi_logged / p, q_logged, q_target)
    return PolicyEstimates(
        policy_name=policy_name,
        source=logged.source,
        reward_name=logged.reward_name,
        n_rows=logged.n_rows,
        logging_value=_interval(r),
        ips=ips.value,
        snips=None if snips is None else snips.value,
        dr=dr.value,
        ips_vs_logging=ips_diff,
        snips_vs_logging=snips_diff,
        dr_vs_logging=dr_diff,
        effective_sample_size=ips.effective_n,
        max_weight=ips.max_weight,
    )


# --- the uplift-style target policy --------------------------------------------------------------


def _segment_labels(
    contexts: pd.DataFrame, columns: Sequence[str], edges: dict[str, FloatArray]
) -> list[str]:
    """One label per row: each column's value, numeric columns cut at the train split's edges."""
    parts: list[list[str]] = []
    for col in columns:
        if col not in contexts.columns:
            raise ValueError(f"Segment column {col!r} is not in the contexts.")
        if col in edges:
            values = contexts[col].to_numpy(dtype=np.float64)
            parts.append([f"{col}#{int(b)}" for b in np.searchsorted(edges[col], values, side="right")])
        else:
            parts.append([f"{col}={v}" for v in contexts[col].astype(str).to_numpy()])
    return ["|".join(row) for row in zip(*parts, strict=True)] if parts else ["all"] * len(contexts)


@dataclass(frozen=True)
class SegmentPolicy:
    """Best action per segment, learned on a train split; unseen segments get the overall best."""

    columns: tuple[str, ...]
    edges: dict[str, FloatArray]
    choice: dict[str, int]
    default_action: int
    n_actions: int
    action_names: tuple[str, ...]
    epsilon: float = 0.0
    table: tuple[tuple[str, int, str], ...] = field(default=())
    """(segment, train rows, chosen action name), for the report."""

    def actions_for(self, logged: LoggedBandit) -> npt.NDArray[np.int64]:
        labels = _segment_labels(logged.contexts, self.columns, self.edges)
        return np.array([self.choice.get(label, self.default_action) for label in labels], dtype=np.int64)

    def __call__(self, logged: LoggedBandit) -> FloatArray:
        if logged.n_actions != self.n_actions:
            raise ValueError("This policy was fitted on a log with a different action space.")
        probs = np.full((logged.n_rows, self.n_actions), self.epsilon / self.n_actions, dtype=np.float64)
        probs[np.arange(logged.n_rows), self.actions_for(logged)] += 1.0 - self.epsilon
        return probs


def fit_segment_policy(
    train: LoggedBandit,
    *,
    segment_columns: Sequence[str],
    n_bins: int = 4,
    shrinkage: float = 50.0,
    epsilon: float = 0.0,
) -> SegmentPolicy:
    """Pick, per segment, the action with the highest shrunk mean reward on `train`.

    Each action's mean in a segment is the inverse-propensity-weighted mean of its rows, pulled
    toward the action's overall mean with weight `shrinkage` (pseudo-rows), so a segment with a
    handful of rows mostly follows the overall winner. Ties go to the lowest action index, which on
    Hillstrom and Criteo is "do nothing". Numeric columns with more than `n_bins` distinct values
    are cut at train quantiles; others are used as categories. `epsilon` spreads that much
    probability uniformly over all actions (exploration); 0 gives a deterministic policy.
    """
    if not 0.0 <= epsilon <= 1.0:
        raise ValueError("epsilon must be between 0 and 1.")
    if shrinkage < 0 or n_bins < 2:
        raise ValueError("shrinkage must be 0 or more and n_bins at least 2.")
    if train.n_rows == 0:
        raise ValueError("Cannot fit a policy on an empty log.")
    columns = tuple(segment_columns)
    edges: dict[str, FloatArray] = {}
    for col in columns:
        if col not in train.contexts.columns:
            raise ValueError(f"Segment column {col!r} is not in the contexts.")
        series = train.contexts[col]
        if pd.api.types.is_numeric_dtype(series) and series.nunique() > n_bins:
            values = series.to_numpy(dtype=np.float64)
            inner = np.quantile(values, np.linspace(0.0, 1.0, n_bins + 1)[1:-1])
            edges[col] = np.unique(inner).astype(np.float64)

    overall = _action_means(train, np.ones(train.n_rows, dtype=bool))
    # An action never logged in train has no evidence at all (its "mean" is only a fallback), so it
    # can never be chosen; otherwise it could win just because the fallback beats the real means.
    logged_ever = np.bincount(train.actions, minlength=train.n_actions) > 0
    default_action = int(np.argmax(np.where(logged_ever, overall, -np.inf)))
    labels = np.array(_segment_labels(train.contexts, columns, edges), dtype=object)
    choice: dict[str, int] = {}
    table: list[tuple[str, int, str]] = []
    for label in sorted(set(labels.tolist())):
        rows = labels == label
        local = _action_means(train, rows)
        # Effective rows per action (Kish: (Σw)²/Σw² of the 1/p weights). It is the plain row count
        # when an action's propensity is constant, and does not inflate rare, low-propensity actions
        # (Criteo control rows at p=0.15) the way scaling Σ(1/p) by the mean propensity would.
        inv = 1.0 / train.propensity[rows]
        w_sum = np.bincount(train.actions[rows], weights=inv, minlength=train.n_actions)
        w_sq = np.bincount(train.actions[rows], weights=inv * inv, minlength=train.n_actions)
        n_eff = np.where(w_sq > 0, w_sum * w_sum / np.maximum(w_sq, 1e-300), 0.0)
        # With no rows of an action in this segment its value is the overall one (the limit of the
        # shrinkage), also when shrinkage is 0, rather than a meaningless 0.
        shrunk = np.where(
            n_eff > 0, (n_eff * local + shrinkage * overall) / np.maximum(n_eff + shrinkage, 1e-12), overall
        )
        best = int(np.argmax(np.where(logged_ever, shrunk, -np.inf)))
        choice[str(label)] = best
        table.append((str(label), int(rows.sum()), train.action_names[best]))
    return SegmentPolicy(
        columns=columns,
        edges=edges,
        choice=choice,
        default_action=default_action,
        n_actions=train.n_actions,
        action_names=train.action_names,
        epsilon=epsilon,
        table=tuple(table),
    )


# --- replay --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayReport:
    """One dataset's replay: the fitted policy on the test split, beside every fixed action."""

    source: str
    reward_name: str
    n_train: int
    n_test: int
    policy: SegmentPolicy
    target: PolicyEstimates
    fixed_actions: tuple[PolicyEstimates, ...]
    notes: tuple[str, ...]

    @property
    def beats_logging(self) -> bool:
        return self.target.beats_logging

    @property
    def summary(self) -> str:
        """The headline, plus an honest note when the fitted policy does not win."""
        return " ".join((self.target.sentence, *self.notes))


def replay(
    logged: LoggedBandit,
    *,
    segment_columns: Sequence[str],
    test_share: float = 0.5,
    seed: int = 0,
    n_bins: int = 4,
    shrinkage: float = 50.0,
    epsilon: float = 0.0,
    max_fixed_actions: int = 10,
    reward_model: RewardModel | None = None,
) -> ReplayReport:
    """Split `logged`, fit the segment policy on train, and evaluate it on test.

    With at most `max_fixed_actions` actions, each "always do action k" policy is evaluated too, so
    the report can say when one blanket action would have done as well (the honest note the plan
    asks for, after v1 found uplift did not beat simple ranking on Hillstrom).
    """
    train, test = logged.split(test_share, seed=seed)
    policy = fit_segment_policy(
        train, segment_columns=segment_columns, n_bins=n_bins, shrinkage=shrinkage, epsilon=epsilon
    )
    target = evaluate(policy, test, reward_model=reward_model, policy_name="segment policy")
    fixed: tuple[PolicyEstimates, ...] = ()
    if logged.n_actions <= max_fixed_actions:
        fixed = tuple(
            evaluate(fixed_action_policy(a), test, reward_model=reward_model, policy_name=f"always {name}")
            for a, name in enumerate(logged.action_names)
        )
    notes: list[str] = []
    if not target.beats_logging:
        notes.append(
            "Honest note: the segment policy did not clear the logging policy with an interval above zero."
        )
    if fixed:
        best = max(fixed, key=lambda e: e.dr.point)
        if best.dr.point >= target.dr.point:
            notes.append(
                f"A blanket rule, '{best.policy_name}', scored as well or better ({best.dr.point:.4g} per row)."
            )
    return ReplayReport(
        source=logged.source,
        reward_name=logged.reward_name,
        n_train=train.n_rows,
        n_test=test.n_rows,
        policy=policy,
        target=target,
        fixed_actions=fixed,
        notes=tuple(notes),
    )
