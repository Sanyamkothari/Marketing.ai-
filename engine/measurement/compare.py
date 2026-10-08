"""Does ranking by uplift beat ranking by risk at the same budget? (Plan J M96)

The research's strongest warning about uplift models is that they are worth using only where they
beat plain risk targeting *at the same budget*, and that this has to be measured out of sample, not
assumed. This module measures it on randomised rows - the rows whose treatment was drawn at random
with a known probability: an uplift training file, or a cycle's holdout plus explore slice (M92) -
in two steps.

**1. Cross-fitting** (:func:`cross_fit`). The rows are split into `folds` folds (stratified on
treatment and outcome; by customer when `groups` is given, so a customer's rows share a fold). For
each fold, an uplift learner (and, for the comparison, a plain risk model) is fitted on *other* folds
and scores this fold. Every row therefore gets an uplift, `p_treated`, `p_control` and a risk score
from models that never saw it. The function checks that on the way out (a row scored by a model
trained on it raises `RuntimeError`), and `tests/unit/measurement/test_compare.py` proves it with
fitters that record what they were trained on and what they scored.

*Which other folds.* `scheme="k_fold"` (the fold AUUC) trains each fold's model on all the other
folds, the usual K-fold. `scheme="ring"` (the comparison) trains fold `k`'s models on the next
`L = (folds - 1) // 2` folds only, `k+1 .. k+L` round the ring (2 of 5 folds). The reason is the
interval. With K-fold training, fold `k`'s outcomes help choose who is contacted in every other fold
and the other folds' outcomes choose who is contacted in fold `k`, so the fold sums are positively
correlated: on a null-effect population the per-row interval covered 92% instead of 95% at every
size tried (DEC-1305's band rejects that). On the ring, for any two folds one of them never trained
the other's models, and since each fold's error has mean zero given every row outside it, the fold
errors are uncorrelated and the per-row interval is honest. The price is that each comparison model
learns from 40% of the rows rather than 80%, which is said wherever the result is shown.

**2. Equal-budget comparison** (:func:`equal_budget_comparison`). In every fold, both rankings
contact the same number of customers, `top_rows(top_share, fold size)` (stable sort on the score,
ties in input order): the same budget, spent two ways. Each top-N's value is estimated off-policy
with `engine.uplift.ope.evaluate_policy`, with the rows' recorded treatment probabilities as the
logging propensities and the cross-fitted `p_treated`/`p_control` as its outcome model. The extra
conversions over contacting nobody are `Σ π·Γ` with `Γ` the per-row doubly robust effect score
(`engine.uplift.ope.dr_effect_terms`): exactly `n × (DR(π) − DR(treat none))` from the report, but
per row, so the uplift-minus-risk difference `Σ (π_u − π_r)·Γ` is a *paired* estimate whose interval
is `n·mean ± z·√n·sd` of its per-row terms. Per rupee divides by `contacts × cost_per_contact`, a
constant, so its interval is the same interval scaled; with no cost per contact it is null, never a
made-up rupee.

**What the interval targets.** The extra conversions the two cross-fitted policies would cause on
these customers. Its nightly coverage is tested on simulated heterogeneous-effect and null-effect
populations (`tests/statistical/test_risk_comparison_coverage.py`, DEC-1305's band).

**Fold AUUC** (:func:`fold_auuc_report`). A K-fold cross-fit gives each fold's AUUC of the uplift
learner refitted without it: how much the model's ranking quality moves with the rows it learns from.
`UPLIFT_UNSTABLE_ACROSS_FOLDS` is raised (by `engine.model_gates`) when any fold's AUUC is at or
below zero.

`numpy` and `pandas` are imported inside function bodies, as everywhere in the engine.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from engine.uplift.contracts import (
    ConfidenceValue,
    FoldAuuc,
    FoldAuucValue,
    PolicyComparisonValue,
    RiskComparison,
)
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.uplift.config import UpliftLearner

    FloatArray = npt.NDArray[np.float64]
    IntArray = npt.NDArray[np.int_]

__all__ = [
    "LIGHTGBM_ONLY_REASON",
    "CrossFit",
    "FoldScores",
    "RiskFitter",
    "UpliftFitter",
    "cross_fit",
    "equal_budget_comparison",
    "estimate_refit_seconds",
    "fold_assignment",
    "fold_auuc_not_computed",
    "fold_auuc_report",
    "lightgbm_risk_fitter",
    "lightgbm_uplift_fitter",
    "top_share_policy",
    "training_folds",
]

_LOGGER = get_logger(__name__)

LIGHTGBM_ONLY_REASON: Final[str] = (
    "Refitting folds is offered for the LightGBM base model only: an AutoGluon model would take its "
    "whole training time again for every fold."
)
OFF_REASON: Final[str] = "Off by default: turn on uplift.evidence.fold_auuc to refit the model on each fold."


@dataclass(frozen=True)
class FoldScores:
    """One fold model's predictions for the rows it scored."""

    uplift: FloatArray
    p_treated: FloatArray
    p_control: FloatArray


UpliftFitter = Callable[["pd.DataFrame", "IntArray", "IntArray"], Callable[["pd.DataFrame"], FoldScores]]
"""`fit(X_train, t_train, y_train) -> predict(X) -> FoldScores`. The frames keep the input index."""

RiskFitter = Callable[["pd.DataFrame", "IntArray"], Callable[["pd.DataFrame"], "FloatArray"]]
"""`fit(X_train, y_train) -> score(X) -> risk`: a plain model of the outcome, treatment ignored."""


@dataclass(frozen=True)
class CrossFit:
    """Out-of-fold predictions for every row, and which fold each row was in."""

    fold_of: IntArray
    uplift: FloatArray
    p_treated: FloatArray
    p_control: FloatArray
    risk: FloatArray
    folds: int
    scheme: Literal["k_fold", "ring"]
    seconds: float
    trained_on: tuple[IntArray, ...] = field(repr=False, default=())
    """Positions each fold's models were fitted on, in fold order (kept for the leak check)."""


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------
def fold_assignment(
    t: npt.ArrayLike, y: npt.ArrayLike, *, folds: int, seed: int, groups: npt.ArrayLike | None = None
) -> IntArray:
    """A fold number `0..folds-1` per row: stratified on (treatment, outcome), or per group.

    Without groups each of the four (t, y) strata is shuffled with `seed` and dealt round-robin, so
    every fold holds about the same share of each. With `groups` (a customer key under a two-column
    key) the distinct groups are shuffled and dealt, so a customer's rows always share a fold.
    """
    import numpy as np

    if folds < 2:
        raise ValueError(f"Cross-fitting needs at least 2 folds, got {folds}.")
    treatment = np.asarray(t).astype(np.int_)
    outcome = np.asarray(y).astype(np.int_)
    rows = treatment.shape[0]
    rng = np.random.default_rng(seed)
    fold_of = np.empty(rows, dtype=np.int_)
    if groups is not None:
        keys = np.asarray(groups, dtype=object)
        unique, inverse = np.unique(keys.astype(str), return_inverse=True)
        order = rng.permutation(unique.shape[0])
        group_fold = np.empty(unique.shape[0], dtype=np.int_)
        group_fold[order] = np.arange(unique.shape[0]) % folds
        fold_of[:] = group_fold[inverse]
        return fold_of
    strata = treatment * 2 + outcome
    for stratum in range(4):
        members = np.flatnonzero(strata == stratum)
        shuffled = members[rng.permutation(members.shape[0])]
        fold_of[shuffled] = np.arange(shuffled.shape[0]) % folds
    return fold_of


def cross_fit(
    X: pd.DataFrame,  # noqa: N803 - the usual name
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    *,
    folds: int,
    seed: int,
    fit_uplift: UpliftFitter,
    fit_risk: RiskFitter | None,
    scheme: Literal["k_fold", "ring"] = "k_fold",
    groups: npt.ArrayLike | None = None,
    fold_of: npt.ArrayLike | None = None,
) -> CrossFit:
    """Score every row with an uplift learner (and a risk model) fitted on other folds only.

    `scheme` picks which other folds (module docstring); `fit_risk=None` fits no risk model and leaves
    `risk` NaN. Raises `RuntimeError` if any row would be scored by a model whose training rows
    include it (the fold-leak guard), and `ValueError` when a fold's training part lacks a treated or
    a control row, or `scheme="ring"` has fewer than 3 folds.
    """
    import numpy as np

    started = time.perf_counter()
    treatment = np.asarray(t).astype(np.int_)
    outcome = np.asarray(y).astype(np.int_)
    rows = len(X.index)
    if treatment.shape[0] != rows or outcome.shape[0] != rows:
        raise ValueError("X, t and y must describe the same rows.")
    assigned = (
        fold_assignment(treatment, outcome, folds=folds, seed=seed, groups=groups)
        if fold_of is None
        else np.asarray(fold_of).astype(np.int_)
    )
    uplift = np.full(rows, np.nan)
    p_treated = np.full(rows, np.nan)
    p_control = np.full(rows, np.nan)
    risk = np.full(rows, np.nan)
    trained: list[IntArray] = []
    for fold in range(folds):
        scored = np.flatnonzero(assigned == fold)
        train = np.flatnonzero(np.isin(assigned, training_folds(fold, folds=folds, scheme=scheme)))
        if np.intersect1d(scored, train).shape[0]:
            raise RuntimeError("A fold's rows are inside its own training rows; refusing to score them.")
        if scored.shape[0] == 0:
            trained.append(train)
            continue
        if not (treatment[train] == 1).any() or not (treatment[train] == 0).any():
            raise ValueError(f"Fold {fold + 1}'s training rows lack a treated or a control customer.")
        predict = fit_uplift(X.iloc[train], treatment[train], outcome[train])
        part = X.iloc[scored]
        predicted = predict(part)
        uplift[scored] = np.asarray(predicted.uplift, dtype=np.float64)
        p_treated[scored] = np.asarray(predicted.p_treated, dtype=np.float64)
        p_control[scored] = np.asarray(predicted.p_control, dtype=np.float64)
        if fit_risk is not None:
            risk[scored] = np.asarray(fit_risk(X.iloc[train], outcome[train])(part), dtype=np.float64)
        trained.append(train)
    if not (np.isfinite(uplift).all() and (fit_risk is None or np.isfinite(risk).all())):
        raise ValueError("A fold model returned a missing or infinite prediction.")
    seconds = time.perf_counter() - started
    log_stage(_LOGGER, "cross_fit", rows=rows, seconds=seconds)
    return CrossFit(
        fold_of=assigned,
        uplift=uplift,
        p_treated=np.clip(p_treated, 0.0, 1.0),
        p_control=np.clip(p_control, 0.0, 1.0),
        risk=risk,
        folds=folds,
        scheme=scheme,
        seconds=seconds,
        trained_on=tuple(trained),
    )


def training_folds(fold: int, *, folds: int, scheme: Literal["k_fold", "ring"]) -> tuple[int, ...]:
    """The folds whose rows fit the models that score `fold`: every other fold, or the next L on the ring."""
    if scheme == "k_fold":
        return tuple(other for other in range(folds) if other != fold)
    reach = (folds - 1) // 2
    if reach < 1:
        raise ValueError(f"Ring cross-fitting needs at least 3 folds, got {folds}.")
    return tuple((fold + step) % folds for step in range(1, reach + 1))


def lightgbm_uplift_fitter(learner: UpliftLearner, *, seed: int) -> UpliftFitter:
    """The configured meta-learner on the LightGBM base model, refitted per fold."""
    from engine.uplift.config import UpliftBaseModel
    from engine.uplift.learners import make_learner

    def fit(features: pd.DataFrame, t: IntArray, y: IntArray) -> Callable[[pd.DataFrame], FoldScores]:
        model = make_learner(learner, UpliftBaseModel.LIGHTGBM, seed=seed).fit(features, t, y)

        def predict(frame: pd.DataFrame) -> FoldScores:
            prediction = model.predict(frame)
            return FoldScores(
                uplift=prediction.uplift, p_treated=prediction.p_treated, p_control=prediction.p_control
            )

        return predict

    return fit


def lightgbm_risk_fitter(*, seed: int) -> RiskFitter:
    """A plain LightGBM classifier of the outcome on the features: risk ranking, treatment ignored."""

    def fit(features: pd.DataFrame, y: IntArray) -> Callable[[pd.DataFrame], FloatArray]:
        import numpy as np
        from lightgbm import LGBMClassifier

        from engine.uplift.learners import lightgbm_params

        if np.unique(y).shape[0] < 2:
            constant = float(np.asarray(y).mean())
            return lambda frame: np.full(len(frame.index), constant)
        model: Any = LGBMClassifier(**lightgbm_params(seed)).fit(features, np.asarray(y).astype(int))

        def score(frame: pd.DataFrame) -> FloatArray:
            probabilities: FloatArray = np.asarray(
                np.asarray(model.predict_proba(frame))[:, 1], dtype=np.float64
            )
            return probabilities

        return score

    return fit


# ---------------------------------------------------------------------------
# The equal-budget comparison
# ---------------------------------------------------------------------------
def top_share_policy(scores: npt.ArrayLike, fold_of: npt.ArrayLike, *, top_share: float) -> FloatArray:
    """`π(1|x)`: 1 for the top `top_rows(top_share, fold size)` rows of each fold by `scores`."""
    import numpy as np

    from engine.uplift.metrics import top_rows

    values = np.asarray(scores, dtype=np.float64)
    folds = np.asarray(fold_of)
    policy = np.zeros(values.shape[0], dtype=np.float64)
    for fold in np.unique(folds).tolist():
        members = np.flatnonzero(folds == fold)
        count = top_rows(top_share, members.shape[0])
        order = members[np.argsort(-values[members], kind="mergesort")]
        policy[order[:count]] = 1.0
    return policy


def _sum_interval(terms: FloatArray, scale: float = 1.0) -> ConfidenceValue:
    """`Σ terms` with `n·mean ± z·√n·sd` (sd with ddof 1), all divided by `scale`."""
    from engine.uplift.incrementality import CONFIDENCE_LEVEL, Z_95

    rows = terms.shape[0]
    total = float(terms.sum()) / scale
    if rows < 2:
        return ConfidenceValue(value=total, confidence_level=CONFIDENCE_LEVEL)
    half = Z_95 * math.sqrt(rows) * float(terms.std(ddof=1)) / scale
    return ConfidenceValue(
        value=total, ci_low=total - half, ci_high=total + half, confidence_level=CONFIDENCE_LEVEL
    )


def equal_budget_comparison(
    *,
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    propensity: float | npt.ArrayLike,
    cross: CrossFit,
    top_share: float,
    cost_per_contact: float | None,
    run_id: str,
    causal: bool,
    propensity_source: Literal["recorded", "treated_share"],
    rows_excluded: int = 0,
    now: datetime | None = None,
) -> RiskComparison:
    """Uplift top-N against risk top-N at equal budget, by extra conversions (and per rupee)."""
    import numpy as np

    from engine.uplift.ope import dr_effect_terms, evaluate_policy

    if cross.scheme != "ring" or not np.isfinite(cross.risk).all():
        raise ValueError(
            "The equal-budget comparison needs a ring cross-fit with a risk model (see cross_fit)."
        )
    treatment = np.asarray(t).astype(np.int_)
    outcome = np.asarray(y).astype(np.int_)
    logged = np.broadcast_to(np.asarray(propensity, dtype=np.float64), treatment.shape).astype(np.float64)
    gamma = dr_effect_terms(
        treatment, outcome, p_treated=cross.p_treated, p_control=cross.p_control, propensity=logged
    )
    share = f"{top_share * 100:g}%"
    policies = {
        "uplift": top_share_policy(cross.uplift, cross.fold_of, top_share=top_share),
        "risk": top_share_policy(cross.risk, cross.fold_of, top_share=top_share),
    }
    contacts = int(policies["uplift"].sum())
    if contacts != int(policies["risk"].sum()):
        raise RuntimeError("The two rankings were given different budgets; refusing to compare them.")
    spend = None if cost_per_contact is None or cost_per_contact <= 0.0 else contacts * cost_per_contact
    descriptions = {
        "uplift": f"Treat the top {share} of each fold by cross-fitted predicted uplift",
        "risk": f"Treat the top {share} of each fold by cross-fitted risk (chance of the outcome)",
    }
    values: dict[str, PolicyComparisonValue] = {}
    for name, policy in policies.items():
        report = evaluate_policy(
            treatment,
            outcome,
            policy,
            p_treated=cross.p_treated,
            p_control=cross.p_control,
            propensity=logged,
            run_id=run_id,
            description=descriptions[name],
            causal=causal,
            now=now,
        )
        terms = policy * gamma
        values[name] = PolicyComparisonValue(
            ranking="uplift" if name == "uplift" else "risk",
            contacts=contacts,
            incremental_conversions=_sum_interval(terms),
            per_rupee=None if spend is None else _sum_interval(terms, spend),
            ope=report,
        )
    paired = (policies["uplift"] - policies["risk"]) * gamma
    difference = _sum_interval(paired)
    better = difference.ci_low is not None and difference.ci_low > 0.0
    from engine.utils.time import utc_now

    return RiskComparison(
        run_id=run_id,
        rows=int(treatment.shape[0]),
        rows_excluded=rows_excluded,
        folds=cross.folds,
        top_share=top_share,
        propensity_source=propensity_source,
        cost_per_contact=cost_per_contact,
        uplift=values["uplift"],
        risk=values["risk"],
        difference=difference,
        difference_per_rupee=None if spend is None else _sum_interval(paired, spend),
        uplift_better=better,
        causal=causal,
        summary=_comparison_summary(difference, better, contacts=contacts, share=share),
        computed_at=now if now is not None else utc_now(),
    )


def _comparison_summary(difference: ConfidenceValue, better: bool, *, contacts: int, share: str) -> str:
    def fmt(value: float | None) -> str:
        return "—" if value is None else f"{value:+,.1f}"

    gap = f"{fmt(difference.value)} extra conversions (95% CI {fmt(difference.ci_low)} to {fmt(difference.ci_high)})"
    if better:
        return (
            f"At the same budget ({contacts:,} contacts, the top {share} of each fold), ranking by "
            f"uplift beats risk ranking: {gap}."
        )
    return (
        f"At the same budget ({contacts:,} contacts, the top {share} of each fold), ranking by uplift "
        f"does not beat risk ranking: {gap}, a range that does not lie above zero."
    )


# ---------------------------------------------------------------------------
# Fold AUUC
# ---------------------------------------------------------------------------
def estimate_refit_seconds(
    fit_seconds: float | None,
    *,
    folds: int,
    rows_all: int,
    rows_fitted: int,
    schemes: tuple[Literal["k_fold", "ring"], ...] = ("k_fold",),
) -> float | None:
    """What refitting `folds` folds will take, from one fit of `rows_fitted` rows taking `fit_seconds`.

    Fit time is taken as linear in rows: a K-fold refit learns from `(folds - 1) / folds` of
    `rows_all`, a ring refit from `((folds - 1) // 2) / folds`, once per fold and per scheme asked for.
    The ring's plain risk model is cheap beside the meta-learner's sub-models and is not added. None
    when the fit was not timed.
    """
    if fit_seconds is None or rows_fitted <= 0:
        return None
    share = {"k_fold": (folds - 1) / folds, "ring": ((folds - 1) // 2) / folds}
    total = sum(fit_seconds * rows_all * share[scheme] / rows_fitted * folds for scheme in schemes)
    return round(total, 1)


def fold_auuc_not_computed(*, folds: int, reason: str, estimated_refit_seconds: float | None) -> FoldAuuc:
    """`fold_auuc` when the folds were not refitted: the reason, and the cost of turning it on."""
    cost = (
        ""
        if estimated_refit_seconds is None
        else f" Turning it on refits the model {folds} times: about {_duration(estimated_refit_seconds)} on this data."
    )
    return FoldAuuc(
        computed=False,
        reason=reason,
        folds=folds,
        estimated_refit_seconds=estimated_refit_seconds,
        summary=f"Not measured. {reason}{cost}",
    )


def fold_auuc_report(
    cross: CrossFit, t: npt.ArrayLike, y: npt.ArrayLike, *, estimated_refit_seconds: float | None
) -> FoldAuuc:
    """Each fold's AUUC of the uplift learner refitted without it, and whether every one is above 0."""
    import numpy as np

    from engine.uplift.metrics import auuc_score

    treatment = np.asarray(t).astype(np.int_)
    outcome = np.asarray(y).astype(np.int_)
    values: list[FoldAuucValue] = []
    for fold in range(cross.folds):
        rows = np.flatnonzero(cross.fold_of == fold)
        arm = treatment[rows]
        auuc = (
            auuc_score(cross.uplift[rows], arm, outcome[rows])
            if rows.shape[0] and (arm == 1).any() and (arm == 0).any()
            else None
        )
        values.append(FoldAuucValue(fold=fold + 1, rows=int(rows.shape[0]), auuc=auuc))
    measured = [value.auuc for value in values if value.auuc is not None]
    mean = float(np.mean(measured)) if measured else None
    sd = float(np.std(measured, ddof=1)) if len(measured) > 1 else None
    minimum = min(measured) if measured else None
    stable = bool(measured) and len(measured) == len(values) and all(value > 0.0 for value in measured)
    return FoldAuuc(
        computed=True,
        folds=cross.folds,
        values=tuple(values),
        mean=mean,
        sd=sd,
        minimum=minimum,
        stable=stable,
        estimated_refit_seconds=estimated_refit_seconds,
        refit_seconds=round(cross.seconds, 1),
        summary=_fold_summary(stable, measured, len(values), mean, minimum),
    )


def _fold_summary(
    stable: bool, measured: list[float], folds: int, mean: float | None, low: float | None
) -> str:
    def fmt(value: float | None) -> str:
        return "—" if value is None else f"{value:.4f}"

    if stable:
        return f"Stable across folds: all {folds} refitted models beat random (AUUC {fmt(low)} at the lowest, {fmt(mean)} on average)."
    below = sum(1 for value in measured if value <= 0.0)
    missing = folds - len(measured)
    parts = [f"{below} of {folds} refitted models do no better than random"] if below else []
    if missing:
        parts.append(f"{missing} fold{'s' if missing != 1 else ''} could not be measured")
    return f"Unstable across folds: {' and '.join(parts)} (AUUC {fmt(low)} at the lowest, {fmt(mean)} on average)."


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} seconds"
    return f"{round(seconds / 60)} minutes"
