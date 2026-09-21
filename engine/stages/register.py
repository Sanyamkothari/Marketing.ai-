"""Register stage (M3): build the registry record and the drift baseline for a trained model."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import BestModel, DriftBaseline, FeatureSchema, ModelVersion
    from engine.pipeline import StageContext


def build_model_version(ctx: StageContext, best: BestModel, schema: FeatureSchema) -> ModelVersion:
    """Assemble the `ModelVersion` record for the model this run produced."""
    raise NotImplementedError("M3")


def drift_baseline(
    train: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    model_version_id: str,
) -> DriftBaseline:
    """Summarise the training distribution per feature, so scoring runs can measure drift."""
    raise NotImplementedError("M3")
