"""Evaluate stage (M3): hold-out metrics, confusion matrix, decile lift, baseline and fairness."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import (
        BaselineComparison,
        ConfusionMatrix,
        DecileLift,
        EvaluationReport,
        FairnessReport,
    )


def evaluate(
    y_true: pd.Series,
    y_score: pd.Series,
    config: UseCaseConfig,
    *,
    run_id: str,
    validation_scores: pd.Series,
    validation_true: pd.Series,
    groups: pd.Series | None,
    baseline_scores: pd.Series | None,
) -> tuple[EvaluationReport, ConfusionMatrix, DecileLift, BaselineComparison, FairnessReport]:
    """Score the model on the test split only and produce the five evaluation artefacts."""
    raise NotImplementedError("M3")
