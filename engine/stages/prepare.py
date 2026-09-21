"""Prepare and split stages (M3): cleaning, exclusions, PII handling and the train/validation/test split."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import PrepareReport, SplitReport


def prepare(
    df: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    primary_key: str,
    target: str | None,
) -> tuple[pd.DataFrame, PrepareReport]:
    """Clean the table as the config asks and record every transform, drop and removal."""
    raise NotImplementedError("M3")


def replay(df: pd.DataFrame, report: PrepareReport) -> pd.DataFrame:
    """Re-apply a recorded `PrepareReport` to a scoring file, so training and scoring agree."""
    raise NotImplementedError("M3")


def split_dataset(
    df: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    target: str,
) -> tuple[dict[str, pd.DataFrame], SplitReport]:
    """Split into train/validation/test by the configured strategy, seeded from the run id."""
    raise NotImplementedError("M3")
