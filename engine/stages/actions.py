"""Actions stage (M4): risk bands, suppression, the control group and the action per row."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig


def assign_bands(scores: pd.Series, config: UseCaseConfig) -> pd.Series:
    """The band name of each score: the first band whose `min_score` the score reaches."""
    raise NotImplementedError("M4")


def apply_actions(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    primary_key: str,
) -> pd.DataFrame:
    """Add band, action and suppression reason to every scored row, seeded from the run id."""
    raise NotImplementedError("M4")
