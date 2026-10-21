"""Bridges to v1's uplift engine: cross-checks of the proof numbers, and v1's AutoML uplift learners as
an optional, stronger learner for `autonomy.uplift`.

**Cross-checks.** Run the same numbers through `engine.uplift.ope` and `engine.measurement.continuous`
so we can see that v2's proof engine agrees with v1's.

**A stronger learner** (`V1UpliftLearner`). `autonomy.uplift` fits its per-customer uplift with a
light numpy learner by default. `V1UpliftLearner` plugs v1's meta-learners (`engine.uplift.learners`:
the S-, T- or X-learner over LightGBM or AutoGluon) in behind the same `UpliftLearner` interface: one
v1 model per arm (that arm's customers against the control group), on the same one-hot customer rows.
Everything around it stays v2's: cross-fitting, the out-of-sample calibration slope and its shrinkage,
the balance check and the blend in the bidder, so a stronger learner moves bids only as far as it
predicts lift on customers it never saw. Use it as

    warm_start_uplift(model, states, history, content, now, learner=V1UpliftLearner())

v1's learners assume random assignment (the X-learner blends with a constant treated share), so this
learner refuses a campaign whose rows carry unequal weights (a propensity-logged one); `fit_uplift`
then fits nothing and says why.

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
from typing import Any

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Estimate
from autonomy.proof.estimate import welch_difference
from autonomy.proof.ope import OpeReport, evaluate

__all__ = [
    "MeanDifferenceCheck",
    "OpeCheck",
    "V1ArmModel",
    "V1UpliftLearner",
    "compare_mean_difference",
    "compare_ope",
    "v2_binary_ope",
]

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


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


# --- v1's AutoML uplift learners behind autonomy.uplift's UpliftLearner ---------------------------


class V1ArmModel:
    """One fitted v1 uplift model per arm, read as `autonomy.uplift.learner.ArmModel`."""

    def __init__(self, models: list[Any], columns: list[str]) -> None:
        self.models = models
        self.columns = columns

    def _frame(self, x: npt.NDArray[np.float32]) -> Any:
        import pandas as pd

        return pd.DataFrame(np.asarray(x, dtype=np.float32), columns=self.columns)

    def predict(self, x: npt.NDArray[np.float32]) -> tuple[FloatArray, FloatArray]:
        frame = self._frame(x)
        preds = [m.predict(frame) for m in self.models]
        # Each arm's model has its own control-group model; their average is the no-message chance.
        mu0 = np.mean([np.asarray(p.p_control, dtype=np.float64) for p in preds], axis=0)
        mu = np.column_stack([np.clip(mu0 + np.asarray(p.uplift, dtype=np.float64), 0.0, 1.0) for p in preds])
        return mu0, mu

    def predict_row(self, cols: list[int]) -> tuple[float, list[float]]:
        row = np.zeros((1, len(self.columns)), dtype=np.float32)
        row[0, cols] = 1.0
        mu0, mu = self.predict(row)
        return float(mu0[0]), [float(v) for v in mu[0]]


@dataclass(frozen=True)
class V1UpliftLearner:
    """v1's S-, T- or X-learner (`learner`: "s_learner", "t_learner", "x_learner") over "lightgbm" or
    "autogluon_fast" (`base_model`), as an `autonomy.uplift.learner.UpliftLearner`. Needs v1's heavy
    dependencies (LightGBM, or AutoGluon); imported only when fitting."""

    learner: str = "x_learner"
    base_model: str = "lightgbm"
    seed: int = 0
    time_limit_s: float = 120.0

    def fit(
        self,
        x: npt.NDArray[np.float32],
        arm: IntArray,
        y: FloatArray,
        weight: FloatArray,
        n_arms: int,
        blocks: int,  # noqa: ARG002 - the interface's; v1's learners pick their own regularisation
    ) -> V1ArmModel:
        if weight.size and float(np.ptp(weight)) > 1e-9 * float(np.abs(weight).max()):
            raise ValueError(
                "v1's uplift learners assume random assignment; these rows carry unequal weights"
            )
        import pandas as pd

        from engine.uplift.config import UpliftBaseModel, UpliftLearner
        from engine.uplift.learners import SLearner, TLearner, XLearner

        kinds: dict[UpliftLearner, type[Any]] = {
            UpliftLearner.S_LEARNER: SLearner,
            UpliftLearner.T_LEARNER: TLearner,
            UpliftLearner.X_LEARNER: XLearner,
        }
        cls = kinds[UpliftLearner(self.learner)]
        base = UpliftBaseModel(self.base_model)
        columns = [f"c{j}" for j in range(x.shape[1])]
        models: list[Any] = []
        for k in range(1, n_arms + 1):
            rows = (arm == 0) | (arm == k)
            frame = pd.DataFrame(np.asarray(x[rows], dtype=np.float32), columns=columns)
            t = (arm[rows] == k).astype(np.int64)
            outcome = (y[rows] > 0.5).astype(np.int64)
            model = cls(base, seed=self.seed, time_limit_s=self.time_limit_s)
            models.append(model.fit(frame, t, outcome))
        return V1ArmModel(models, columns)
