"""Fast fitters and one simulated comparison, for the equal-budget comparison's tests (Plan J M96).

The interval of `engine.measurement.compare.equal_budget_comparison` is a property of the estimator
(cross-fitted, doubly robust, the recorded propensities), not of the learner that ranks the rows, so
its coverage is tested with learners that fit in microseconds: a T-learner of two linear probability
models and a linear risk model, by least squares. The product uses LightGBM
(`compare.lightgbm_uplift_fitter`, `compare.lightgbm_risk_fitter`); thousands of simulations with it
would take hours.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from engine.measurement.compare import FoldScores, cross_fit, equal_budget_comparison, top_share_policy
from engine.measurement.simulate import SimulatedUpliftPopulation, uplift_population
from engine.uplift.contracts import ConfidenceValue, RiskComparison

TOP_SHARE = 0.3
FOLDS = 5


def _design(frame: pd.DataFrame) -> np.ndarray:
    return np.column_stack([np.ones(len(frame.index)), frame.to_numpy(dtype=np.float64)])


def _least_squares(frame: pd.DataFrame, target: np.ndarray) -> np.ndarray:
    coefficients, *_ = np.linalg.lstsq(_design(frame), np.asarray(target, dtype=np.float64), rcond=None)
    return np.asarray(coefficients)


def linear_uplift_fitter(
    features: pd.DataFrame, t: np.ndarray, y: np.ndarray
) -> Callable[[pd.DataFrame], FoldScores]:
    """A T-learner of two linear probability models, clipped to [0.01, 0.99]."""
    treated = _least_squares(features[t == 1], y[t == 1])
    control = _least_squares(features[t == 0], y[t == 0])

    def predict(frame: pd.DataFrame) -> FoldScores:
        design = _design(frame)
        p_treated = np.clip(design @ treated, 0.01, 0.99)
        p_control = np.clip(design @ control, 0.01, 0.99)
        return FoldScores(uplift=p_treated - p_control, p_treated=p_treated, p_control=p_control)

    return predict


def linear_risk_fitter(features: pd.DataFrame, y: np.ndarray) -> Callable[[pd.DataFrame], np.ndarray]:
    """A linear probability model of the outcome, treatment ignored: plain risk."""
    coefficients = _least_squares(features, y)
    return lambda frame: np.asarray(_design(frame) @ coefficients, dtype=np.float64)


@dataclass(frozen=True)
class Draw:
    """One simulated comparison and the truths its intervals should hold."""

    report: RiskComparison
    true_uplift: float
    true_risk: float

    @property
    def true_difference(self) -> float:
        return self.true_uplift - self.true_risk


def compare_once(seed: int, *, n: int, effect: str) -> Draw:
    population: SimulatedUpliftPopulation = uplift_population(n, seed=seed, effect=effect)  # type: ignore[arg-type]
    cross = cross_fit(
        population.features,
        population.t,
        population.y,
        folds=FOLDS,
        seed=seed,
        fit_uplift=linear_uplift_fitter,
        fit_risk=linear_risk_fitter,
        scheme="ring",
    )
    report = equal_budget_comparison(
        t=population.t,
        y=population.y,
        propensity=population.propensity,
        cross=cross,
        top_share=TOP_SHARE,
        cost_per_contact=10.0,
        run_id="simulated",
        causal=True,
        propensity_source="recorded",
        seed=seed,
    )
    # The same tie key the comparison used, so the truths are of the very policies it evaluated.
    uplift_policy = top_share_policy(cross.uplift, cross.fold_of, top_share=TOP_SHARE, seed=seed)
    risk_policy = top_share_policy(cross.risk, cross.fold_of, top_share=TOP_SHARE, seed=seed)
    return Draw(
        report=report,
        true_uplift=population.true_incremental(uplift_policy),
        true_risk=population.true_incremental(risk_policy),
    )


def covers(interval: ConfidenceValue, truth: float) -> bool:
    assert interval.ci_low is not None and interval.ci_high is not None
    return interval.ci_low <= truth <= interval.ci_high
