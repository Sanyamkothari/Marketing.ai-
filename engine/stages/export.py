"""Export stage (M4): the downloadable score files and the scoring summary."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import DriftReport, ScoringSummary
    from engine.storage import Storage


def write_scores(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    primary_key: str,
    storage: Storage,
) -> dict[str, str]:
    """Write `scores.csv` and `scores.parquet` and return the artefact filename to storage key map."""
    raise NotImplementedError("M4")


def summarise(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    model_version_id: str,
    drift: DriftReport | None,
) -> ScoringSummary:
    """Counts per band, per action and per suppression reason, plus the configured KPI."""
    raise NotImplementedError("M4")
