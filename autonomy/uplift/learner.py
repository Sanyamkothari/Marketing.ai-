"""The default uplift learner: one logistic model with a shared baseline and a ridge-shrunk effect per arm.

    logit P(buy | x, arm k) = x . beta + x . gamma_k        (gamma_0 = 0 for "no message")

`x` is the one-hot customer row with an intercept (`FeatureEncoder`). `beta` is who buys anyway, fitted
on every customer of the campaign whatever they got; `gamma_k` is how arm k changes the odds, and the
intercept of `gamma_k` is arm k's average effect. The predicted uplift of arm k is
`sigmoid(x . beta + x . gamma_k) - sigmoid(x . beta)`.

Why this shape rather than two separate models (a T-learner): the control group of a campaign is often
small (15% on Criteo) and buying rare, and a model fitted on the control alone is mostly noise, which a
T-learner hands straight to the uplift. Sharing the baseline uses every customer for who buys anyway;
even with every effect term but the intercept shrunk to zero, uplift still varies with the baseline
(a constant odds ratio lifts likely buyers more in absolute terms). The effect terms then add what the
data show beyond that.

**Penalties, set before looking at any data set.** Ridge on beta with weight `BASE_PENALTY` (a prior sd
of 1 on each bin's log-odds, weak). On each arm's effect intercept `MAIN_PENALTY` (near-flat). On each
arm's effect per bin `EFFECT_SD_TOTAL ** -2 x blocks` (a prior sd of `EFFECT_SD_TOTAL / sqrt(blocks)`
per feature block, so the effect over all features together has a prior sd of about
`EFFECT_SD_TOTAL` in log odds, however many features the business has). How far to trust what is left
is not decided here but out of sample (`autonomy.uplift.model`: the calibration slope).

Fitting is Newton-Raphson (IRLS) with step halving on the penalised likelihood. The Hessian has a block structure (beta with every arm, each arm with
itself only), so it is built from `X' W X` over all rows and over each arm's rows, never from a design
matrix with the interaction columns written out; rows are taken in chunks to keep memory flat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import numpy.typing as npt

__all__ = [
    "ArmModel",
    "SharedBaselineLogit",
    "UpliftLearner",
    "sigmoid",
    "tdot",
]

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
X32 = npt.NDArray[np.float32]

BASE_PENALTY = 1.0
MAIN_PENALTY = 1e-4
EFFECT_SD_TOTAL = 0.5
MAX_ITERS = 30
TOLERANCE = 1e-7
HALVINGS = 30
CHUNK_ROWS = 16_384


def sigmoid(z: FloatArray) -> FloatArray:
    out: FloatArray = 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))
    return out


class ArmModel(Protocol):
    """A fitted outcome model per arm."""

    def predict(self, x: X32) -> tuple[FloatArray, FloatArray]:
        """(P(buy | x, no message) of shape (n,), P(buy | x, arm k) of shape (n, n_arms))."""
        ...

    def predict_row(self, cols: list[int]) -> tuple[float, list[float]]:
        """The same for one customer given as their active columns."""
        ...


class UpliftLearner(Protocol):
    """Fits an `ArmModel` on one campaign's rows: `arm` 0 is no message, 1..n_arms the messages."""

    def fit(
        self, x: X32, arm: IntArray, y: FloatArray, weight: FloatArray, n_arms: int, blocks: int
    ) -> ArmModel: ...


def _gram(x: X32, w: FloatArray) -> FloatArray:
    """X' diag(w) X, in float64, chunked."""
    d = x.shape[1]
    out = np.zeros((d, d), dtype=np.float64)
    for lo in range(0, x.shape[0], CHUNK_ROWS):
        xc = x[lo : lo + CHUNK_ROWS].astype(np.float64)
        out += (xc * w[lo : lo + CHUNK_ROWS, None]).T @ xc
    return out


def _dot(x: X32, v: FloatArray) -> FloatArray:
    """X v in float64, chunked."""
    out = np.empty(x.shape[0], dtype=np.float64)
    for lo in range(0, x.shape[0], CHUNK_ROWS):
        out[lo : lo + CHUNK_ROWS] = x[lo : lo + CHUNK_ROWS].astype(np.float64) @ v
    return out


def tdot(x: X32, v: FloatArray) -> FloatArray:
    """X' v in float64, chunked."""
    out = np.zeros(x.shape[1], dtype=np.float64)
    for lo in range(0, x.shape[0], CHUNK_ROWS):
        out += x[lo : lo + CHUNK_ROWS].astype(np.float64).T @ v[lo : lo + CHUNK_ROWS]
    return out


@dataclass(frozen=True)
class SharedBaselineFit:
    """Fitted coefficients: `beta` (d,) and `gamma` (n_arms, d)."""

    beta: FloatArray
    gamma: FloatArray

    def predict(self, x: X32) -> tuple[FloatArray, FloatArray]:
        base = _dot(x, self.beta)
        mu0 = sigmoid(base)
        mu = np.empty((x.shape[0], self.gamma.shape[0]), dtype=np.float64)
        for k in range(self.gamma.shape[0]):
            mu[:, k] = sigmoid(base + _dot(x, self.gamma[k]))
        return mu0, mu

    def predict_row(self, cols: list[int]) -> tuple[float, list[float]]:
        base = float(self.beta[cols].sum())
        mu0 = float(sigmoid(np.array([base]))[0])
        effects = self.gamma[:, cols].sum(axis=1) + base
        return mu0, [float(v) for v in sigmoid(effects)]


@dataclass(frozen=True)
class SharedBaselineLogit:
    """The default learner (see the module docstring). The penalties are generic, not per data set."""

    base_penalty: float = BASE_PENALTY
    main_penalty: float = MAIN_PENALTY
    effect_sd_total: float = EFFECT_SD_TOTAL
    max_iters: int = MAX_ITERS

    def fit(
        self, x: X32, arm: IntArray, y: FloatArray, weight: FloatArray, n_arms: int, blocks: int
    ) -> SharedBaselineFit:
        d = x.shape[1]
        k_arms = n_arms
        effect_penalty = max(blocks, 1) / (self.effect_sd_total**2)
        pen_beta = np.full(d, self.base_penalty)
        pen_beta[0] = 0.0
        pen_gamma = np.full(d, effect_penalty)
        pen_gamma[0] = self.main_penalty
        rows = [np.flatnonzero(arm == k + 1) for k in range(k_arms)]
        xs = [x[r] for r in rows]
        ybar = float(np.clip(np.average(y, weights=weight), 1e-6, 1 - 1e-6))
        beta = np.zeros(d)
        beta[0] = float(np.log(ybar / (1 - ybar)))
        gamma = np.zeros((k_arms, d))
        size = d * (k_arms + 1)

        def linear(b: FloatArray, g: FloatArray) -> FloatArray:
            eta = _dot(x, b)
            for k in range(k_arms):
                eta[rows[k]] += _dot(xs[k], g[k])
            return eta

        def objective(eta: FloatArray, b: FloatArray, g: FloatArray) -> float:
            # Penalised log-likelihood, computed stably: y * eta - log(1 + e^eta).
            ll = float(np.sum(weight * (y * eta - np.logaddexp(0.0, eta))))
            return ll - 0.5 * float(pen_beta @ (b * b)) - 0.5 * float(np.sum(pen_gamma * (g * g)))

        eta = linear(beta, gamma)
        current = objective(eta, beta, gamma)
        for _ in range(self.max_iters):
            p = sigmoid(eta)
            w = weight * np.clip(p * (1 - p), 1e-9, None)
            r = weight * (y - p)
            hess = np.zeros((size, size))
            grad = np.zeros(size)
            hess[:d, :d] = _gram(x, w) + np.diag(pen_beta)
            grad[:d] = tdot(x, r) - pen_beta * beta
            for k in range(k_arms):
                lo, hi = d * (k + 1), d * (k + 2)
                gk = _gram(xs[k], w[rows[k]])
                hess[:d, lo:hi] = gk
                hess[lo:hi, :d] = gk
                hess[lo:hi, lo:hi] = gk + np.diag(pen_gamma)
                grad[lo:hi] = tdot(xs[k], r[rows[k]]) - pen_gamma * gamma[k]
            try:
                step = np.linalg.solve(hess, grad)
            except np.linalg.LinAlgError:
                step = np.linalg.lstsq(hess, grad, rcond=None)[0]
            # Newton can overshoot on rare outcomes (a few dozen buyers in a control group): halve
            # the step until the penalised likelihood does not fall, so every step is an improvement.
            scale = 1.0
            for _half in range(HALVINGS):
                b_try = beta + scale * step[:d]
                g_try = gamma + scale * step[d:].reshape(k_arms, d)
                eta_try = linear(b_try, g_try)
                value = objective(eta_try, b_try, g_try)
                if value >= current - 1e-9 * abs(current):
                    break
                scale *= 0.5
            else:
                break  # no step improves: at the optimum to numerical precision
            beta, gamma, eta, current = b_try, g_try, eta_try, value
            if float(np.max(np.abs(scale * step))) < TOLERANCE:
                break
        return SharedBaselineFit(beta=beta, gamma=gamma)
