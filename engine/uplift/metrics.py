"""How well an uplift model ranks customers by what the action changes (plan B §3, Stage A).

A propensity model is judged by whether it finds the customers who convert; an uplift model is
judged by whether the customers it ranks first are the ones whose behaviour the action *changes*.
No single row shows that - a customer is either treated or not, never both - so every metric here is
a statement about groups: rank the hold-out by predicted uplift, walk down the ranking, and compare
the treated and control customers seen so far. The definitions below are the ones the docs quote,
implemented exactly and nowhere else.

**Ranking.** Rows are ranked by predicted uplift, highest first, with a *stable* sort on `-pred`
(`kind="mergesort"`), so rows with equal predictions keep their input order. That makes every
number a deterministic function of the arrays passed in: no random tie-breaking, and a model that
predicts the same value for everyone is scored as "the input order", not as a lucky shuffle.

**Cumulative counts.** For the top `k` rows (`k = 0..n`): `N_t(k)`, `N_c(k)` are the treated and
control counts and `Y_t(k)`, `Y_c(k)` their outcome sums. All curves are functions of these four.

- Qini: `Qini(k) = Y_t(k) − Y_c(k)·N_t(k)/N_c(k)`, the scaled term 0 when `N_c(k) = 0`. The chart
  point at `f = k/n` is `Qini(k)/n` (incremental conversions per hold-out customer) and the random
  line is `f · Qini(n)/n`.
- Uplift curve: `U(k) = (Y_t(k)/N_t(k) − Y_c(k)/N_c(k)) · k/n`, 0 while either arm is empty. Its
  random line is `f · ATE`, where `ATE` is the overall treated rate minus the control rate.
- `AUUC` is the trapezoid integral over `f ∈ [0, 1]` - every `k`, including `k = 0` - of
  `U(k) − f·ATE`; `qini_coefficient` the same integral of `Qini(k)/n − f·Qini(n)/n`. Both are 0 for
  a ranking no better than random, positive when the top of the ranking holds the customers the
  action moves, negative when it holds the ones it puts off.
- `uplift@fraction`: among the top `ceil(fraction·n)` rows, treated rate minus control rate; `None`
  when either arm is empty there, because a rate of nobody is not zero.
- Deciles: `np.array_split` of the ranked rows into ten groups, decile 1 the highest predicted.

**Bootstrap (DEC-605).** Intervals are 95 % percentile intervals over `samples` resamples of the
hold-out. Each resample draws WITH replacement WITHIN each arm, so its treated and control counts
equal the hold-out's: the experiment's design is fixed, only which customers happened to land in
each arm varies. Predictions are held fixed - the model is not refitted - so the interval measures
the noise of the *evaluation*, which is what the champion rule's "lower bound > 0" needs. The draws
come from `np.random.default_rng(seed)`, one resample after the other, treated arm first then
control arm, each as `rng.integers(0, arm_size, arm_size)` into the arm's rows in input order. A
resample is ranked like any data set; equal predictions are ordered by their input position, as a
stable sort of the resample listed in input order would do.

The bootstrap is vectorised, because 200 resamples of a 30 000-row hold-out must take seconds, not
minutes: each resample becomes a row of ranked positions (one `np.sort` per chunk of resamples),
its treatment and outcome columns are gathered from the ranked hold-out, and the four cumulative
sums - and from them every curve - are taken along the rows at once. Chunks cap the memory at a few
tens of megabytes whatever the sample count.

**Beats risk, and calibration (Plan J M96).** `compare_with_baselines` scores plain risk rankings
(the model's own `p_control` and `p_treated`, and the use case's approved propensity model) with the
same AUUC on the same hold-out, and the uplift model's AUUC minus each with a *paired* bootstrap: the
resample draws depend only on the seed and the arm sizes, so with the evaluation's own seed resample
`r` holds the same customers for every ranking (`paired_auuc_resamples`). `calibration_by_decile`
compares each decile's mean predicted uplift with its observed uplift and interval, read from the
same draws through `_bootstrap`'s `windows`.

*Ties in the comparison.* The ranking rule above orders equal predictions by input position, and a
hold-out stays in file order. A plain risk score is often a step function (an isotonic-calibrated
propensity model gives whole blocks of customers the same score), and a campaign export often lists
the treated customers before the control customers: then every tie block puts its treated rows first
and the baseline's AUUC is biased (on a homogeneous effect a useless uplift model "beat" a five-level
risk score with 95% CI 0.014 to 0.033). So every ranking the comparison and the calibration read is
first made strict by `tie_broken_ranks`: equal scores are ordered by a key drawn from the seed alone,
fixed per row - it depends on neither treatment nor outcome, and it is the same for every ranking, so
the pairing holds. A ranking without ties is left exactly as it is, so a continuous uplift model's
`uplift_auuc` is the evaluation's own `auuc`. The champion rule's `evaluate_uplift` is unchanged.

An interval is `None` (the page shows "—") when any resample leaves the statistic undefined, for
example a top 10 % with no control customer in it: dropping those resamples would quietly bias the
band, and a band made of the rest would claim a precision nobody measured.

`numpy` is imported inside the function bodies, never at module level, so `import engine` stays
fast.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from engine.config import Metric
from engine.uplift.contracts import (
    NOT_CAUSAL_NOTE,
    BaselineAuuc,
    BaselineComparison,
    CalibrationDecile,
    ConfidenceValue,
    QiniCurve,
    QiniPoint,
    UpliftAtK,
    UpliftCalibration,
    UpliftDecile,
    UpliftEvaluation,
)
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    import numpy as np
    import numpy.typing as npt

    from engine.uplift.config import UpliftBaseModel, UpliftLearner
    from engine.uplift.contracts import BaselineKind

    FloatArray = npt.NDArray[np.float64]
    IntArray = npt.NDArray[np.int64]

__all__ = [
    "BEATS_RISK_LABELS",
    "CONFIDENCE_LEVEL",
    "DECILES",
    "UPLIFT_AT_FRACTIONS",
    "BaselineInput",
    "HoldoutUplift",
    "auuc_score",
    "bootstrap_uplift_at",
    "bootstrap_uplift_at_many",
    "calibration_by_decile",
    "compare_with_baselines",
    "decile_table",
    "evaluate_uplift",
    "holdout_digest",
    "paired_auuc_difference",
    "paired_auuc_resamples",
    "percentile_interval",
    "qini_coefficient",
    "qini_points",
    "tie_broken_ranks",
    "tie_key",
    "top_rows",
    "uplift_at_fraction",
]

_LOGGER = get_logger(__name__)

CONFIDENCE_LEVEL: Final[float] = 0.95
"""Coverage of every bootstrap interval: the 2.5th and 97.5th percentiles of the resamples."""

UPLIFT_AT_FRACTIONS: Final[tuple[float, ...]] = (0.1, 0.2, 0.3)
"""Top shares `uplift_evaluation.json` reports the observed uplift of."""

DECILES: Final[int] = 10

_CHUNK_CELLS: Final[int] = 2_000_000
"""Resample-by-row cells processed at once; about 16 MB per float64 working array."""

_FRACTION_DIGITS: Final[int] = 9
"""`fraction·n` is rounded to this many decimals before `ceil`, so 0.1·30 is 3 rows, not 4."""


# ---------------------------------------------------------------------------
# Inputs and the ranked hold-out
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Ranked:
    """The hold-out in ranked order (highest predicted uplift first) and how rows map to ranks."""

    pred: FloatArray
    t: IntArray
    y: IntArray
    rank_of: IntArray
    """`rank_of[i]` is the ranked position of input row `i`."""
    value: FloatArray | None = None

    @property
    def n(self) -> int:
        return int(self.t.shape[0])

    @property
    def treated_rows(self) -> int:
        return int(self.t.sum())

    @property
    def control_rows(self) -> int:
        return self.n - self.treated_rows


def _binary(values: npt.ArrayLike, name: str) -> IntArray:
    import numpy as np

    array = np.asarray(values)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got {array.ndim} dimensions.")
    if array.dtype == np.bool_:
        return array.astype(np.int64)
    if not np.issubdtype(array.dtype, np.number):
        raise ValueError(f"{name} must hold 0/1 values, got dtype {array.dtype}.")
    bad = int(np.count_nonzero((array != 0) & (array != 1)))
    if bad:
        raise ValueError(f"{name} must hold only 0 and 1; {bad} of {array.shape[0]} values do not.")
    return array.astype(np.int64)


def _rank(
    pred: npt.ArrayLike,
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    value: npt.ArrayLike | None = None,
) -> _Ranked:
    """Validate the arrays and rank them; see the module docstring for the tie rule."""
    import numpy as np

    scores = np.asarray(pred, dtype=np.float64)
    if scores.ndim != 1:
        raise ValueError(f"pred must be one-dimensional, got {scores.ndim} dimensions.")
    treatment = _binary(t, "t")
    outcome = _binary(y, "y")
    if not scores.shape[0] == treatment.shape[0] == outcome.shape[0]:
        raise ValueError(
            f"pred, t and y must have the same length, got {scores.shape[0]}, {treatment.shape[0]} "
            f"and {outcome.shape[0]}."
        )
    if scores.shape[0] == 0:
        raise ValueError("Uplift metrics need at least one row.")
    not_finite = int(np.count_nonzero(~np.isfinite(scores)))
    if not_finite:
        raise ValueError(f"{not_finite} predicted uplift values are missing or infinite.")
    val_ordered: FloatArray | None = None
    if value is not None:
        val_arr = np.asarray(value, dtype=np.float64)
        if val_arr.ndim != 1 or val_arr.shape[0] != scores.shape[0]:
            raise ValueError(
                f"value must be one-dimensional with same length as pred ({scores.shape[0]}), got shape {val_arr.shape}."
            )
        if not np.all(np.isfinite(val_arr)):
            raise ValueError("value must contain finite numbers.")
    order = np.argsort(-scores, kind="mergesort")
    rank_of = np.empty(order.shape[0], dtype=np.int64)
    rank_of[order] = np.arange(order.shape[0], dtype=np.int64)
    if value is not None:
        val_ordered = val_arr[order]
    return _Ranked(
        pred=scores[order],
        t=treatment[order],
        y=outcome[order],
        rank_of=rank_of,
        value=val_ordered,
    )


def _require_both_arms(ranked: _Ranked) -> None:
    if ranked.treated_rows == 0 or ranked.control_rows == 0:
        raise ValueError(
            f"Uplift needs treated and control customers; the data has {ranked.treated_rows} "
            f"treated and {ranked.control_rows} control rows."
        )


def top_rows(fraction: float, n: int) -> int:
    """How many rows "the top `fraction`" of `n` is: `ceil(fraction·n)`, at least 1.

    Immune to `0.1·30 = 3.0000000000000004`. Public so that every "top X%" in the uplift package
    (uplift@k here, the OPE rule in `engine.uplift.ope`) counts the same rows.
    """
    return _top_rows(fraction, n)


def _top_rows(fraction: float, n: int) -> int:
    """`ceil(fraction·n)`, immune to `0.1·30 = 3.0000000000000004`."""
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}.")
    return max(1, math.ceil(round(fraction * n, _FRACTION_DIGITS)))


# ---------------------------------------------------------------------------
# Curves: vectorised over a (resamples × rows) matrix of ranked t and y
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Cumulative:
    """`N_t, N_c, Y_t, Y_c` for `k = 0..n` along the last axis (so `n + 1` columns)."""

    n_t: FloatArray
    n_c: FloatArray
    y_t: FloatArray
    y_c: FloatArray

    @property
    def n(self) -> int:
        return int(self.n_t.shape[-1]) - 1


def _cumulative(t: IntArray, y: IntArray, value: FloatArray | None = None) -> _Cumulative:
    """Cumulative arm counts and outcome sums of ranked 2-D arrays, with the `k = 0` column."""
    import numpy as np

    rows = t.shape[0]
    zero = np.zeros((rows, 1), dtype=np.float64)
    n_t = np.concatenate([zero, np.cumsum(t, axis=1, dtype=np.float64)], axis=1)
    y_weighted = y * value if value is not None else y
    y_t = np.concatenate([zero, np.cumsum(t * y_weighted, axis=1, dtype=np.float64)], axis=1)
    y_all = np.concatenate([zero, np.cumsum(y_weighted, axis=1, dtype=np.float64)], axis=1)
    k = np.arange(t.shape[1] + 1, dtype=np.float64)
    return _Cumulative(n_t=n_t, n_c=k - n_t, y_t=y_t, y_c=y_all - y_t)


def _qini(c: _Cumulative) -> FloatArray:
    """`Qini(k)` for every `k`, the scaled control term 0 where `N_c(k) = 0`."""
    import numpy as np

    scaled = np.divide(c.y_c * c.n_t, c.n_c, out=np.zeros_like(c.y_c), where=c.n_c > 0)
    qini: FloatArray = c.y_t - scaled
    return qini


def _rate_difference(c: _Cumulative) -> FloatArray:
    """`Y_t/N_t − Y_c/N_c` for every `k`, NaN where either arm is empty."""
    import numpy as np

    both = (c.n_t > 0) & (c.n_c > 0)
    treated = np.divide(c.y_t, c.n_t, out=np.full_like(c.y_t, np.nan), where=both)
    control = np.divide(c.y_c, c.n_c, out=np.full_like(c.y_c, np.nan), where=both)
    difference: FloatArray = treated - control
    return difference


def _uplift_curve(c: _Cumulative) -> FloatArray:
    """`U(k)`: the rate difference inside the top `k`, times `k/n`; 0 while either arm is empty."""
    import numpy as np

    fraction = np.arange(c.n + 1, dtype=np.float64) / c.n
    curve: FloatArray = np.nan_to_num(_rate_difference(c), nan=0.0) * fraction
    return curve


def _ate(c: _Cumulative) -> FloatArray:
    """Overall treated rate minus control rate, per row of the matrix (arms are never empty here)."""
    ate: FloatArray = c.y_t[:, -1] / c.n_t[:, -1] - c.y_c[:, -1] / c.n_c[:, -1]
    return ate


def _auuc(c: _Cumulative) -> FloatArray:
    """Trapezoid area of `U(k) − f·ATE` over `f = k/n`, `k = 0..n`."""
    import numpy as np

    fraction = np.arange(c.n + 1, dtype=np.float64) / c.n
    gap = _uplift_curve(c) - fraction[None, :] * _ate(c)[:, None]
    area: FloatArray = np.asarray(np.trapezoid(gap, dx=1.0 / c.n, axis=1), dtype=np.float64)
    return area


def _qini_area(c: _Cumulative) -> FloatArray:
    """Trapezoid area of `Qini(k)/n − f·Qini(n)/n` over `f = k/n`, `k = 0..n`."""
    import numpy as np

    fraction = np.arange(c.n + 1, dtype=np.float64) / c.n
    per_customer = _qini(c) / c.n
    gap = per_customer - fraction[None, :] * per_customer[:, -1:]
    area: FloatArray = np.asarray(np.trapezoid(gap, dx=1.0 / c.n, axis=1), dtype=np.float64)
    return area


def _single(ranked: _Ranked) -> _Cumulative:
    val_matrix = ranked.value[None, :] if ranked.value is not None else None
    return _cumulative(ranked.t[None, :], ranked.y[None, :], val_matrix)


# ---------------------------------------------------------------------------
# Point estimates
# ---------------------------------------------------------------------------
def qini_points(
    pred: npt.ArrayLike, t: npt.ArrayLike, y: npt.ArrayLike, *, points: int = 101
) -> tuple[QiniPoint, ...]:
    """The Qini chart: `points` evenly spaced shares from 0 to 1, each ON the exact curve.

    Share `i` is `k_i = round(i·n / (points − 1))` rows (half up) and is reported as the exact
    `k_i / n`, so no point is interpolated. When the hold-out has fewer than `points − 1` rows,
    repeated `k` are dropped and every `k = 0..n` is returned once. Needs both arms.
    """
    if points < 2:
        raise ValueError(f"A Qini chart needs at least 2 points, got {points}.")
    ranked = _rank(pred, t, y)
    _require_both_arms(ranked)
    c = _single(ranked)
    n = ranked.n
    qini = _qini(c)[0] / n
    curve = _uplift_curve(c)[0]
    ks = sorted({(i * n + (points - 1) // 2) // (points - 1) for i in range(points)})
    return tuple(
        QiniPoint(
            fraction=k / n,
            qini=float(qini[k]),
            random=float(k / n * qini[n]),
            uplift_curve=float(curve[k]),
        )
        for k in ks
    )


def auuc_score(pred: npt.ArrayLike, t: npt.ArrayLike, y: npt.ArrayLike) -> float:
    """Area between the uplift curve and the random line `f·ATE`; 0 is random. Needs both arms."""
    ranked = _rank(pred, t, y)
    _require_both_arms(ranked)
    return float(_auuc(_single(ranked))[0])


def qini_coefficient(pred: npt.ArrayLike, t: npt.ArrayLike, y: npt.ArrayLike) -> float:
    """Area between the Qini curve (per customer) and its random line; 0 is random. Needs both arms."""
    ranked = _rank(pred, t, y)
    _require_both_arms(ranked)
    return float(_qini_area(_single(ranked))[0])


def _uplift_at_k(c: _Cumulative, k: int) -> FloatArray:
    """Rate difference inside the top `k` rows, per matrix row; NaN where an arm is empty."""
    import numpy as np

    n_t, n_c = c.n_t[:, k], c.n_c[:, k]
    both = (n_t > 0) & (n_c > 0)
    treated = np.divide(c.y_t[:, k], n_t, out=np.full_like(n_t, np.nan), where=both)
    control = np.divide(c.y_c[:, k], n_c, out=np.full_like(n_c, np.nan), where=both)
    difference: FloatArray = treated - control
    return difference


def uplift_at_fraction(
    pred: npt.ArrayLike, t: npt.ArrayLike, y: npt.ArrayLike, fraction: float
) -> float | None:
    """Treated rate minus control rate among the top `ceil(fraction·n)` rows; `None` if an arm is empty."""
    import numpy as np

    ranked = _rank(pred, t, y)
    value = float(_uplift_at_k(_single(ranked), _top_rows(fraction, ranked.n))[0])
    return None if np.isnan(value) else value


def decile_table(pred: npt.ArrayLike, t: npt.ArrayLike, y: npt.ArrayLike) -> tuple[UpliftDecile, ...]:
    """Ten groups of the ranked rows (`np.array_split`), decile 1 the highest predicted uplift.

    A group can only be empty when the hold-out has fewer than ten rows; empty groups are left out
    rather than reported with a made-up mean. An arm missing inside a decile gives a `None` rate.
    """
    import numpy as np

    ranked = _rank(pred, t, y)
    deciles: list[UpliftDecile] = []
    for number, rows in enumerate(np.array_split(np.arange(ranked.n), DECILES), start=1):
        if rows.shape[0] == 0:
            continue
        arm_t, arm_y = ranked.t[rows], ranked.y[rows]
        treated = int(arm_t.sum())
        control = int(rows.shape[0]) - treated
        treated_rate = float(arm_y[arm_t == 1].mean()) if treated else None
        control_rate = float(arm_y[arm_t == 0].mean()) if control else None
        observed = (
            treated_rate - control_rate if treated_rate is not None and control_rate is not None else None
        )
        deciles.append(
            UpliftDecile(
                decile=number,
                rows=int(rows.shape[0]),
                treated_rows=treated,
                control_rows=control,
                treated_rate=treated_rate,
                control_rate=control_rate,
                observed_uplift=observed,
                predicted_uplift=float(ranked.pred[rows].mean()),
            )
        )
    return tuple(deciles)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Resamples:
    """One value per resample for each statistic; NaN where a resample leaves it undefined."""

    auuc: FloatArray | None
    qini: FloatArray | None
    ate: FloatArray | None
    uplift_at: dict[int, FloatArray]
    """Keyed by the number of top rows `k`, not the fraction, so equal `k` are computed once."""
    windows: dict[tuple[int, int], FloatArray] = field(default_factory=dict)
    """Plan J M96: the rate difference among ranked rows `a+1..b`, keyed by `(a, b)` (the deciles)."""


def _bootstrap(
    ranked: _Ranked,
    top_rows: Sequence[int],
    *,
    samples: int,
    seed: int,
    curves: bool,
    windows: Sequence[tuple[int, int]] = (),
) -> _Resamples:
    """Resample within arms, re-rank, and evaluate every statistic on every resample at once.

    `windows` (Plan J M96) adds the rate difference among ranked rows `a+1..b` of each resample; it
    reads the same cumulative sums, so asking for it changes no draw and no other statistic.
    """
    import numpy as np

    if samples < 1:
        raise ValueError(f"bootstrap samples must be at least 1, got {samples}.")
    n = ranked.n
    # Ranked positions of each arm's rows, listed in INPUT order: resample draws index into these.
    treated_positions = ranked.rank_of[ranked.t[ranked.rank_of] == 1]
    control_positions = ranked.rank_of[ranked.t[ranked.rank_of] == 0]
    n_t, n_c = treated_positions.shape[0], control_positions.shape[0]
    rng = np.random.default_rng(seed)
    chunk = max(1, _CHUNK_CELLS // max(n, 1))

    auuc: list[FloatArray] = []
    qini: list[FloatArray] = []
    ate: list[FloatArray] = []
    at: dict[int, list[FloatArray]] = {k: [] for k in top_rows}
    spans: dict[tuple[int, int], list[FloatArray]] = {span: [] for span in windows}
    for start in range(0, samples, chunk):
        size = min(chunk, samples - start)
        positions = np.empty((size, n), dtype=np.int64)
        for row in range(size):  # one resample after the other: the draw order the docstring fixes
            positions[row, :n_t] = treated_positions[rng.integers(0, n_t, n_t)] if n_t else 0
            positions[row, n_t:] = control_positions[rng.integers(0, n_c, n_c)] if n_c else 0
        positions.sort(axis=1)  # re-rank: ranked position order = predicted uplift, ties by input order
        val_matrix = ranked.value[positions] if ranked.value is not None else None
        c = _cumulative(ranked.t[positions], ranked.y[positions], val_matrix)
        if curves:
            auuc.append(_auuc(c))
            qini.append(_qini_area(c))
            ate.append(_ate(c))
        for k in top_rows:
            at[k].append(_uplift_at_k(c, k))
        for span in windows:
            spans[span].append(_window_difference(c, *span))

    return _Resamples(
        auuc=np.concatenate(auuc) if curves else None,
        qini=np.concatenate(qini) if curves else None,
        ate=np.concatenate(ate) if curves else None,
        uplift_at={k: np.concatenate(values) for k, values in at.items()},
        windows={span: np.concatenate(values) for span, values in spans.items()},
    )


def _window_difference(c: _Cumulative, start: int, stop: int) -> FloatArray:
    """Treated rate minus control rate among ranked rows `start+1..stop`, per matrix row; NaN where an
    arm is empty there (Plan J M96: one decile of a resample)."""
    import numpy as np

    n_t = c.n_t[:, stop] - c.n_t[:, start]
    n_c = c.n_c[:, stop] - c.n_c[:, start]
    both = (n_t > 0) & (n_c > 0)
    treated = np.divide(c.y_t[:, stop] - c.y_t[:, start], n_t, out=np.full_like(n_t, np.nan), where=both)
    control = np.divide(c.y_c[:, stop] - c.y_c[:, start], n_c, out=np.full_like(n_c, np.nan), where=both)
    difference: FloatArray = treated - control
    return difference


def percentile_interval(point: float, resamples: FloatArray) -> ConfidenceValue:
    """The 95% percentile interval of `resamples` around `point` (`None` bounds when any is NaN)."""
    return _interval(point, resamples)


def _interval(point: float, resamples: FloatArray) -> ConfidenceValue:
    """Percentile interval; no interval at all if any resample left the statistic undefined."""
    import numpy as np

    if np.isnan(resamples).any():
        return ConfidenceValue(value=point, confidence_level=CONFIDENCE_LEVEL)
    tail = (1.0 - CONFIDENCE_LEVEL) / 2.0 * 100.0
    low, high = np.percentile(resamples, [tail, 100.0 - tail])
    return ConfidenceValue(
        value=point, ci_low=float(low), ci_high=float(high), confidence_level=CONFIDENCE_LEVEL
    )


def bootstrap_uplift_at(
    pred: npt.ArrayLike,
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    fraction: float,
    *,
    samples: int,
    seed: int,
    value: npt.ArrayLike | None = None,
) -> ConfidenceValue | None:
    """`uplift_at_fraction` with its 95 % bootstrap interval; `None` when the point is undefined.

    Uses the same resamples as :func:`evaluate_uplift` for the same `seed`, so the interval here and
    the one in `uplift_evaluation.json` agree for the same share.
    """
    import numpy as np

    ranked = _rank(pred, t, y, value=value)
    k = _top_rows(fraction, ranked.n)
    point = float(_uplift_at_k(_single(ranked), k)[0])
    if np.isnan(point):  # an arm is empty in the top rows of the hold-out itself
        return None
    resamples = _bootstrap(ranked, (k,), samples=samples, seed=seed, curves=False)
    return _interval(point, resamples.uplift_at[k])


def bootstrap_uplift_at_many(
    pred: npt.ArrayLike,
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    fractions: Sequence[float],
    *,
    samples: int,
    seed: int,
    value: npt.ArrayLike | None = None,
) -> tuple[ConfidenceValue | None, ...]:
    """:func:`bootstrap_uplift_at` for several shares at once, from ONE set of resamples.

    Entry `i` equals `bootstrap_uplift_at(pred, t, y, fractions[i], samples=samples, seed=seed)`
    exactly: the resample draws do not depend on which top-`k` counts are read off them, so reading
    many from one pass changes the cost, not the numbers. The budget curve
    (`engine.uplift.policy.profit_curve`) relies on that to put the recommendation's own interval
    on its configured point.
    """
    import numpy as np

    ranked = _rank(pred, t, y, value=value)
    single = _single(ranked)
    ks = [_top_rows(fraction, ranked.n) for fraction in fractions]
    points = [float(_uplift_at_k(single, k)[0]) for k in ks]
    wanted = tuple(sorted({k for k, point in zip(ks, points, strict=True) if not np.isnan(point)}))
    if not wanted:
        return tuple(None for _ in ks)
    resamples = _bootstrap(ranked, wanted, samples=samples, seed=seed, curves=False)
    return tuple(
        None if np.isnan(point) else _interval(point, resamples.uplift_at[k])
        for k, point in zip(ks, points, strict=True)
    )


@dataclass(frozen=True)
class HoldoutUplift:
    """A hold-out's observed uplift among its top share, asked about many shares cheaply.

    `at` is exactly :func:`bootstrap_uplift_at` - what `recommend_policy`'s `observed_top_share`
    callable is - `intervals` the same for several shares from one bootstrap, and `points` the bare
    point estimate for any number of shares with no bootstrap at all (NaN where an arm of the top
    rows is empty), which is what lets the budget curve search every contact count for its optimum.
    """

    pred: FloatArray
    t: IntArray
    y: IntArray
    samples: int
    seed: int
    value: FloatArray | None = None

    @property
    def is_value_weighted(self) -> bool:
        return self.value is not None

    def at(self, fraction: float) -> ConfidenceValue | None:
        return bootstrap_uplift_at(
            self.pred, self.t, self.y, fraction, samples=self.samples, seed=self.seed, value=self.value
        )

    def intervals(self, fractions: Sequence[float]) -> tuple[ConfidenceValue | None, ...]:
        return bootstrap_uplift_at_many(
            self.pred, self.t, self.y, fractions, samples=self.samples, seed=self.seed, value=self.value
        )

    def points(self, fractions: FloatArray) -> FloatArray:
        import numpy as np

        ranked = _rank(self.pred, self.t, self.y, value=self.value)
        # `_top_rows` itself, one share at a time: numpy's rounding is not Python's in every last
        # digit, and a share must count exactly the rows `at` counts for it.
        ks = np.asarray(
            [_top_rows(float(share), ranked.n) for share in np.asarray(fractions).tolist()], dtype=np.int64
        )
        difference: FloatArray = _rate_difference(_single(ranked))[0][ks]
        return difference


# ---------------------------------------------------------------------------
# uplift_evaluation.json + qini_curve.json
# ---------------------------------------------------------------------------
def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    text = f"{value:.4f}"
    return "0.0000" if text == "-0.0000" else text


def _summary(auuc: ConfidenceValue, *, causal: bool) -> str:
    level = f"{auuc.confidence_level * 100:.0f}%"
    if auuc.ci_low is None or auuc.ci_high is None:
        sentence = (
            f"No measurable uplift: the AUUC ({_fmt(auuc.value)}) has no {level} interval, so this "
            f"model cannot be shown to target better than random."
        )
    elif auuc.ci_low > 0.0:
        sentence = (
            f"Targeting by predicted uplift beats random targeting: AUUC {_fmt(auuc.value)} "
            f"({level} CI {_fmt(auuc.ci_low)} to {_fmt(auuc.ci_high)})."
        )
    elif auuc.ci_high < 0.0:
        sentence = (
            f"No measurable uplift: the AUUC interval ({_fmt(auuc.ci_low)} to {_fmt(auuc.ci_high)}) "
            f"lies below zero, so this model targets worse than random."
        )
    else:
        sentence = (
            f"No measurable uplift: the AUUC interval ({_fmt(auuc.ci_low)} to {_fmt(auuc.ci_high)}) "
            f"includes zero, so this model cannot be shown to target better than random."
        )
    return f"{NOT_CAUSAL_NOTE} {sentence}" if not causal else sentence


def holdout_digest(keys: Sequence[object]) -> str:
    """sha256 of the hold-out's primary keys, as text, sorted and newline-joined (DEC-670).

    Order-free, so the same customers give the same fingerprint however the caller listed them;
    two evaluations with different fingerprints were measured on different customers.
    """
    digest = hashlib.sha256()
    for key in sorted(str(key) for key in keys):
        digest.update(key.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def evaluate_uplift(
    pred: npt.ArrayLike,
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    *,
    run_id: str,
    learner: UpliftLearner,
    base_model: UpliftBaseModel,
    bootstrap_samples: int,
    seed: int,
    causal: bool,
    now: datetime | None = None,
    holdout_keys: Sequence[object] | None = None,
) -> tuple[UpliftEvaluation, QiniCurve]:
    """Every hold-out metric of an uplift model, with bootstrap intervals, and its Qini chart.

    This is plan B §5's "evaluate_uplift.py" (DEC-669): the uplift evaluator lives here, and
    `engine/stages/evaluate.py` is untouched. `pred` must come from a model that never saw these
    rows. Needs both arms (`ValueError` otherwise: without a control group there is no uplift to
    measure). A top share whose treated or control arm is empty is left out of `uplift_at` rather
    than reported as a number. `holdout_keys`, the rows' primary keys in any order, sets
    `holdout_fingerprint` so the champion rule can prove two evaluations share a hold-out (DEC-670);
    it must list one key per row.
    """
    import numpy as np

    from engine.utils.time import utc_now

    started = time.perf_counter()
    ranked = _rank(pred, t, y)
    _require_both_arms(ranked)
    if holdout_keys is not None and len(holdout_keys) != ranked.n:
        raise ValueError(
            f"holdout_keys lists {len(holdout_keys)} keys for {ranked.n} hold-out rows; it needs one per row."
        )
    c = _single(ranked)
    n = ranked.n
    top = {fraction: _top_rows(fraction, n) for fraction in UPLIFT_AT_FRACTIONS}
    resamples = _bootstrap(
        ranked, sorted(set(top.values())), samples=bootstrap_samples, seed=seed, curves=True
    )
    assert resamples.auuc is not None and resamples.qini is not None and resamples.ate is not None

    treated_rate = float(ranked.y[ranked.t == 1].mean())
    control_rate = float(ranked.y[ranked.t == 0].mean())
    auuc = _interval(float(_auuc(c)[0]), resamples.auuc)
    uplift_at: list[UpliftAtK] = []
    for fraction, k in top.items():
        point = float(_uplift_at_k(c, k)[0])
        if not np.isnan(point):
            uplift_at.append(UpliftAtK(fraction=fraction, uplift=_interval(point, resamples.uplift_at[k])))

    evaluation = UpliftEvaluation(
        run_id=run_id,
        learner=learner,
        base_model=base_model,
        primary_metric=Metric.AUUC,
        rows_evaluated=n,
        treated_rows=ranked.treated_rows,
        control_rows=ranked.control_rows,
        treated_rate=treated_rate,
        control_rate=control_rate,
        average_treatment_effect=_interval(treated_rate - control_rate, resamples.ate),
        auuc=auuc,
        qini_coefficient=_interval(float(_qini_area(c)[0]), resamples.qini),
        uplift_at=tuple(uplift_at),
        deciles=decile_table(pred, t, y),
        bootstrap_samples=bootstrap_samples,
        measurable_uplift=auuc.ci_low is not None and auuc.ci_low > 0.0,
        causal=causal,
        summary=_summary(auuc, causal=causal),
        evaluated_at=now if now is not None else utc_now(),
        holdout_fingerprint=None if holdout_keys is None else holdout_digest(holdout_keys),
    )
    curve = QiniCurve(run_id=run_id, rows_evaluated=n, points=qini_points(pred, t, y), causal=causal)
    log_stage(_LOGGER, "uplift_evaluate", rows=n, seconds=time.perf_counter() - started)
    return evaluation, curve


# ---------------------------------------------------------------------------
# Plan J M96: does ranking by uplift beat ranking by risk? Is the predicted uplift calibrated?
# ---------------------------------------------------------------------------
BEATS_RISK_LABELS: Final[dict[str, str]] = {
    "p_control": "the model's own chance of the outcome without contact",
    "p_treated": "the model's own chance of the outcome with contact",
    "propensity_model": "the approved propensity model's score",
}
"""How the Approver's screen names each plain ranking."""

CALIBRATION_MIN_DECILES: Final[int] = 5
"""Fewer deciles with an observed interval than this, and calibration is not judged."""

CALIBRATION_MIN_COVERED_SHARE: Final[float] = 0.8
"""Share of those deciles whose interval must contain the prediction (8 of 10)."""


_TIE_STREAM: Final[int] = 0x7469_6573
"""Second word of the tie-break key's seed (`[seed, _TIE_STREAM]`): a stream of its own, never the
bootstrap's draws."""


def tie_key(rows: int, *, seed: int) -> FloatArray:
    """One uniform key per row from the seed alone: the order equal scores take (M96)."""
    import numpy as np

    key: FloatArray = np.random.default_rng([seed, _TIE_STREAM]).random(rows)
    return key


def tie_broken_ranks(scores: npt.ArrayLike, *, seed: int) -> FloatArray:
    """`scores` made strict: `n` for the highest, `1` for the lowest, equal scores in `tie_key` order.

    Returned unchanged (as float64) when no two scores are equal, or when a score is missing or
    infinite (so `_rank` still names the bad values). The key depends only on the seed and the row
    count, never on treatment or outcome, so a tied block no longer follows the file's order.
    """
    import numpy as np

    values = np.asarray(scores, dtype=np.float64)
    if values.ndim != 1 or not np.isfinite(values).all() or np.unique(values).shape[0] == values.shape[0]:
        return values
    rows = values.shape[0]
    order = np.lexsort((tie_key(rows, seed=seed), -values))
    ranks = np.empty(rows, dtype=np.float64)
    ranks[order] = rows - np.arange(rows, dtype=np.float64)
    return ranks


@dataclass(frozen=True)
class BaselineInput:
    """One plain ranking to compare with the uplift model: its scores, or why there are none."""

    kind: BaselineKind
    scores: FloatArray | None
    reason: str | None = None
    model_id: str | None = None


def paired_auuc_resamples(
    pred: npt.ArrayLike, t: npt.ArrayLike, y: npt.ArrayLike, *, samples: int, seed: int
) -> tuple[float, FloatArray]:
    """The AUUC of ranking by `pred` and its bootstrap resamples, as `evaluate_uplift` draws them.

    **Why two calls with the same `seed` are paired.** `_bootstrap` draws `rng.integers(0, n_arm,
    n_arm)` into each arm's rows listed in INPUT order, treated arm first; the draws depend only on
    the seed and the two arm sizes, never on the ranking. So for the same `t`, `y` and seed, resample
    `r` of one ranking holds exactly the same customers (with the same multiplicities) as resample
    `r` of any other ranking: the difference of the two AUUCs per resample is a paired bootstrap of
    the difference, and the uplift model's resamples are the very ones behind its `auuc` interval.

    Equal scores are first ordered by `tie_broken_ranks` (module docstring, "Ties in the comparison"),
    with the same key for every ranking; scores without ties are ranked exactly as `evaluate_uplift`
    ranks them.
    """
    ranked = _rank(tie_broken_ranks(pred, seed=seed), t, y)
    _require_both_arms(ranked)
    resamples = _bootstrap(ranked, (), samples=samples, seed=seed, curves=True)
    assert resamples.auuc is not None
    return float(_auuc(_single(ranked))[0]), resamples.auuc


def paired_auuc_difference(
    pred: npt.ArrayLike,
    baseline: npt.ArrayLike,
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    *,
    samples: int,
    seed: int,
) -> ConfidenceValue:
    """AUUC(rank by `pred`) − AUUC(rank by `baseline`), with a paired percentile bootstrap interval."""
    point_a, draws_a = paired_auuc_resamples(pred, t, y, samples=samples, seed=seed)
    point_b, draws_b = paired_auuc_resamples(baseline, t, y, samples=samples, seed=seed)
    return _interval(point_a - point_b, draws_a - draws_b)


def compare_with_baselines(
    pred: npt.ArrayLike,
    t: npt.ArrayLike,
    y: npt.ArrayLike,
    baselines: Sequence[BaselineInput],
    *,
    samples: int,
    seed: int,
) -> BaselineComparison:
    """`baseline_comparison`: each plain ranking's AUUC on the same hold-out and the paired gap (M96).

    The beats-risk check is decided against the approved propensity model when it was scored, else
    against `p_control` (the uplift model's own estimate of plain risk); it passes only when the
    paired difference's lower bound is above zero. A difference with no interval does not pass.
    """
    uplift_point, uplift_draws = paired_auuc_resamples(pred, t, y, samples=samples, seed=seed)
    uplift_auuc = _interval(uplift_point, uplift_draws)
    rows: list[BaselineAuuc] = []
    for item in baselines:
        label = BEATS_RISK_LABELS[item.kind]
        if item.scores is None:
            rows.append(
                BaselineAuuc(
                    baseline=item.kind,
                    label=label,
                    available=False,
                    reason=item.reason or "This ranking could not be computed.",
                    model_id=item.model_id,
                )
            )
            continue
        point, draws = paired_auuc_resamples(item.scores, t, y, samples=samples, seed=seed)
        difference = _interval(uplift_point - point, uplift_draws - draws)
        rows.append(
            BaselineAuuc(
                baseline=item.kind,
                label=label,
                available=True,
                model_id=item.model_id,
                auuc=_interval(point, draws),
                difference=difference,
                uplift_better=difference.ci_low is not None and difference.ci_low > 0.0,
            )
        )
    by_kind = {row.baseline: row for row in rows if row.available}
    risk: BaselineKind = "propensity_model" if "propensity_model" in by_kind else "p_control"
    decider = by_kind.get(risk)
    beats = bool(decider is not None and decider.uplift_better)
    return BaselineComparison(
        uplift_auuc=uplift_auuc,
        baselines=tuple(rows),
        risk_baseline=risk,
        beats_risk=beats,
        bootstrap_samples=samples,
        summary=_beats_risk_summary(decider, beats, label=BEATS_RISK_LABELS[risk]),
    )


def _beats_risk_summary(row: BaselineAuuc | None, beats: bool, *, label: str) -> str:
    if row is None or row.difference is None:
        return f"Not checked: ranking by {label} could not be computed on this hold-out."
    gap = row.difference
    if beats:
        return (
            f"Ranking by predicted uplift beats risk ranking (by {label}): its AUUC is higher by "
            f"{_fmt(gap.value)} (95% CI {_fmt(gap.ci_low)} to {_fmt(gap.ci_high)})."
        )
    if gap.ci_low is None or gap.ci_high is None:
        where = "has no interval"
    elif gap.ci_high < 0.0:
        where = "lies below zero"
    else:
        where = "includes zero"
    return (
        f"This model does not beat risk ranking (by {label}): the AUUC difference is {_fmt(gap.value)} "
        f"(95% CI {_fmt(gap.ci_low)} to {_fmt(gap.ci_high)}), a range that {where}."
    )


def calibration_by_decile(
    pred: npt.ArrayLike, t: npt.ArrayLike, y: npt.ArrayLike, *, samples: int, seed: int
) -> UpliftCalibration:
    """Predicted against observed uplift in each decile, with bootstrap intervals (M96).

    The deciles are `decile_table`'s (ranked rows split by `np.array_split`); each resample is
    re-ranked and cut at the same row counts, from the same draws as the evaluation's own intervals.
    Equal predictions are ordered by `tie_broken_ranks`, as in the comparison, so a tied block that
    straddles a cut does not send its treated rows to the higher decile because of the file's order.
    """
    import numpy as np

    scores = np.asarray(pred, dtype=np.float64)
    ranked = _rank(tie_broken_ranks(scores, seed=seed), t, y)
    _require_both_arms(ranked)
    predicted_by_rank = np.empty(ranked.n, dtype=np.float64)
    predicted_by_rank[ranked.rank_of] = scores
    groups = [rows for rows in np.array_split(np.arange(ranked.n), DECILES) if rows.shape[0]]
    spans = [(int(rows[0]), int(rows[-1]) + 1) for rows in groups]
    resamples = _bootstrap(ranked, (), samples=samples, seed=seed, curves=False, windows=spans)
    single = _single(ranked)
    deciles: list[CalibrationDecile] = []
    gaps: list[tuple[int, float]] = []
    for number, (span, rows) in enumerate(zip(spans, groups, strict=True), start=1):
        predicted = float(predicted_by_rank[rows].mean())
        value = float(_window_difference(single, *span)[0])
        observed: ConfidenceValue | None = None
        inside: bool | None = None
        if not np.isnan(value):
            observed = _interval(value, resamples.windows[span])
            gaps.append((int(rows.shape[0]), abs(value - predicted)))
            if observed.ci_low is not None and observed.ci_high is not None:
                inside = observed.ci_low <= predicted <= observed.ci_high
        deciles.append(
            CalibrationDecile(
                decile=number,
                rows=int(rows.shape[0]),
                predicted_uplift=predicted,
                observed_uplift=observed,
                within_interval=inside,
            )
        )
    weight = sum(rows for rows, _ in gaps)
    weighted_gap = None if weight == 0 else sum(rows * gap for rows, gap in gaps) / weight
    judged = [d.within_interval for d in deciles if d.within_interval is not None]
    covered = sum(1 for inside in judged if inside)
    calibrated = (
        None
        if len(judged) < CALIBRATION_MIN_DECILES
        else covered >= math.ceil(CALIBRATION_MIN_COVERED_SHARE * len(judged) - 1e-9)
    )
    return UpliftCalibration(
        deciles=tuple(deciles),
        weighted_abs_gap=weighted_gap,
        deciles_with_interval=len(judged),
        deciles_covered=covered,
        well_calibrated=calibrated,
        summary=_calibration_summary(calibrated, covered, len(judged), weighted_gap),
    )


def _calibration_summary(calibrated: bool | None, covered: int, judged: int, gap: float | None) -> str:
    points = "—" if gap is None else f"{gap * 100:.1f} points"
    if calibrated is None:
        return (
            f"Not judged: only {judged} of the ten groups had enough treated and control customers "
            f"to measure their uplift (average gap {points})."
        )
    verdict = "matches" if calibrated else "does not match"
    return (
        f"Predicted uplift {verdict} what was measured: {covered} of {judged} groups contain the "
        f"prediction in their 95% range; the average gap is {points}."
    )
