"""Explain stages: global feature importance (M3) and per-row reasons (M3 train, M4 score).

`shap` is imported inside the function bodies, never at module level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import FeatureImportance, RowExplanation
    from engine.storage import Storage


def global_importance(
    predictor_key: str,
    test: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    storage: Storage,
) -> FeatureImportance:
    """Permutation feature importance on the test split, top 20 with normalised shares."""
    raise NotImplementedError("M3")


def row_reasons(
    predictor_key: str,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    storage: Storage,
) -> tuple[RowExplanation, ...]:
    """The top `reasons_per_row` SHAP reasons for each row of `frame`."""
    raise NotImplementedError("M3")
