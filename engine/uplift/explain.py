"""Explanations of predicted uplift: the importance chart and "why this customer is persuadable".

This is the uplift counterpart of `engine.stages.explain`, and it deliberately produces the same
artefacts - `FeatureImportance` and `RowExplanation` from `engine.contracts` - with the same reason
sentences (`"visits_30d ↑ (9)"`, `"plan = premium"`, `"region missing"`), so the Output page and
`scores.csv` render an uplift run exactly as they render a Phase 1 run. The per-row ordering and the
sentence are not re-implemented here: :func:`engine.stages.explain.reasons_for_row` builds them.

What differs is **what is explained**. A Phase 1 reason explains a probability of converting; an
uplift reason explains the *change* in that probability caused by treatment. A feature with an up
arrow here makes the customer more persuadable, not more likely to convert - a loyal big spender may
convert very often and be a sure thing with no uplift at all. The caption therefore names the
target, "predicted uplift", and never just "SHAP".

Both functions consume the matrix `UpliftModel.contributions` returns and compute nothing about the
model: explanation is measurement, never selection. The matrix is either exact TreeSHAP (X-learner on
LightGBM) or TreeSHAP of a surrogate whose fidelity the model records; that choice is the learner's,
and the caller reports it (`engine.uplift.learners`).

A row whose contributions are all zero gets no reasons at all rather than a padded or invented one:
the model does not distinguish that customer from the average, and saying otherwise would be
fabrication. `numpy` and `pandas` are imported inside the functions.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Final

from engine.contracts import FeatureImportance, FeatureImportanceItem, ReasonMethod, RowExplanation
from engine.stages.explain import IMPORTANCE_UNAVAILABLE_CAPTION, TOP_FEATURES, reasons_for_row
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    FloatArray = npt.NDArray[np.float64]

__all__ = [
    "UPLIFT_IMPORTANCE_CAPTION",
    "uplift_importance",
    "uplift_reasons",
]

_LOGGER = get_logger(__name__)

UPLIFT_IMPORTANCE_CAPTION: Final[str] = "Mean |SHAP| on predicted uplift, test split (%)"
"""Names the method and the target: this chart ranks what moves the uplift, not the conversion."""

_IMPORTANCE_DECIMALS: Final[int] = 6
_SHARE_DECIMALS: Final[int] = 1
_SCORE_DECIMALS: Final[int] = 6


def uplift_importance(
    contributions: FloatArray, features: Sequence[str], *, run_id: str
) -> FeatureImportance:
    """The global chart: the top 20 features by mean |contribution| to the predicted uplift.

    `contributions` is `rows x len(features)`, the first element of `UpliftModel.contributions` on
    the test split. Shares are normalised over the returned features and sum to exactly 100.0 (the
    last one absorbs the rounding residue), as on the Phase 1 chart. Ties are broken by feature name
    so the chart does not depend on column order. With no rows there is nothing to average and the
    chart is empty, captioned as unavailable - never filled with zeros.
    """
    import numpy as np

    matrix = _checked_matrix(contributions, len(features))
    if matrix.shape[0] == 0 or matrix.shape[1] == 0:
        return FeatureImportance(
            run_id=run_id, method="shap", top_n=0, items=(), caption=IMPORTANCE_UNAVAILABLE_CAPTION
        )
    means = np.abs(matrix).mean(axis=0)
    ranked = sorted(
        ((str(feature), float(mean)) for feature, mean in zip(features, means, strict=True)),
        key=lambda pair: (-pair[1], pair[0]),
    )[:TOP_FEATURES]
    shares = _shares([mean for _, mean in ranked])
    items = tuple(
        FeatureImportanceItem(
            rank=position + 1,
            feature=feature,
            importance=round(mean, _IMPORTANCE_DECIMALS),
            share_pct=shares[position],
        )
        for position, (feature, mean) in enumerate(ranked)
    )
    return FeatureImportance(
        run_id=run_id, method="shap", top_n=len(items), items=items, caption=UPLIFT_IMPORTANCE_CAPTION
    )


def uplift_reasons(
    contributions: FloatArray,
    frame: pd.DataFrame,
    uplift: FloatArray,
    keys: Sequence[str],
    *,
    top_n: int,
) -> tuple[RowExplanation, ...]:
    """Per row, the `top_n` features that moved its predicted uplift most, strongest first.

    `frame` is the prepared feature frame the contributions were computed on: its columns, in order,
    are the contribution matrix's columns, and its values are what the sentences show. `keys` are the
    rows' primary-key values and `uplift` the predicted uplift each explanation carries as `score`.
    Equal contributions are ordered by the feature's global rank (mean |contribution| over these
    rows), then by name, exactly as `engine.stages.explain.reasons_for_row` orders Phase 1 reasons.
    """
    import numpy as np

    started = time.perf_counter()
    features = [str(column) for column in frame.columns]
    matrix = _checked_matrix(contributions, len(features))
    rows = matrix.shape[0]
    scores = np.asarray(uplift, dtype=np.float64)
    if not len(frame) == rows == len(scores) == len(keys):
        raise ValueError("contributions, frame, uplift and keys must describe the same rows")
    if top_n < 0:
        raise ValueError("top_n must not be negative")
    order = np.argsort(-np.abs(matrix).mean(axis=0), kind="mergesort") if rows else np.arange(0)
    rank = {features[int(position)]: place + 1 for place, position in enumerate(order)}
    values = {feature: frame[feature].tolist() for feature in features}
    explanations = tuple(
        RowExplanation(
            primary_key=str(keys[row]),
            score=round(float(scores[row]), _SCORE_DECIMALS),
            reasons=reasons_for_row(
                dict(zip(features, matrix[row].tolist(), strict=True)),
                {feature: values[feature][row] for feature in features},
                limit=top_n,
                rank=rank,
            ),
            method=ReasonMethod.TREE_SHAP,
        )
        for row in range(rows)
    )
    log_stage(_LOGGER, "uplift_reasons", rows=rows, seconds=time.perf_counter() - started)
    return explanations


def _checked_matrix(contributions: FloatArray, width: int) -> FloatArray:
    """`contributions` as a finite `rows x width` float matrix; ValueError otherwise.

    A non-finite contribution cannot be ranked or rendered honestly, so it is refused rather than
    silently zeroed.
    """
    import numpy as np

    matrix = np.asarray(contributions, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] != width:
        raise ValueError(f"contributions must be a rows x {width} matrix, one column per feature")
    if not np.isfinite(matrix).all():
        raise ValueError("contributions contain non-finite values")
    return matrix


def _shares(importances: Sequence[float]) -> list[float]:
    """Percentages summing to exactly 100.0; all zeros when nothing moved the uplift at all."""
    total = math.fsum(importances)
    if total <= 0.0:
        return [0.0] * len(importances)
    shares = [round(value / total * 100.0, _SHARE_DECIMALS) for value in importances]
    shares[-1] = round(100.0 - math.fsum(shares[:-1]), _SHARE_DECIMALS)
    return shares
