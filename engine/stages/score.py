"""Predict stage (M4): batch scoring with a stored predictor and the drift report."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import DriftBaseline, DriftReport
    from engine.storage import Storage


def predict(
    predictor_key: str,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    storage: Storage,
) -> pd.Series:
    """Calibrated scores for every row of `frame`, from the stored predictor."""
    raise NotImplementedError("M4")


def compute_drift(
    baseline: DriftBaseline,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
) -> DriftReport:
    """Population stability index per feature against the training baseline."""
    raise NotImplementedError("M4")
