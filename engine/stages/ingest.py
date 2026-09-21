"""Ingest stage (M2): read the uploaded table and profile it.

Every body lands in M2; the signatures are fixed here so the pipeline and the API can be written
against them. Heavy libraries are imported inside the function bodies, never at module level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Literal

    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import DatasetProfile
    from engine.storage import Storage


def read_table(storage: Storage, key: str, *, max_rows: int | None = None) -> pd.DataFrame:
    """Read a CSV (sniffed delimiter, BOM tolerated) or Parquet upload into a data frame."""
    raise NotImplementedError("M2")


def profile_dataset(
    df: pd.DataFrame,
    config: UseCaseConfig,
    *,
    upload_id: str,
    file_name: str,
    file_format: Literal["csv", "parquet"],
    file_size_bytes: int,
    delimiter: str | None,
    encoding: str,
) -> DatasetProfile:
    """Describe the uploaded table: per-column types, nulls, distincts and the preview rows."""
    raise NotImplementedError("M2")
