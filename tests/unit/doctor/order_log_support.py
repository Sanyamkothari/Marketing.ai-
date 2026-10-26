"""Shared helpers of the order-log path's tests (v3 phase 0)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from engine.config import load_use_case
from engine.doctor.diagnose import LoadedFile
from engine.stages.ingest import profile_dataset

USE_CASE = "retail-win-back"


def loaded(
    frame: pd.DataFrame,
    name: str,
    config_root: Path,
    upload_id: str = "u_log",
    use_case: str = USE_CASE,
    file_rows: int | None = None,
) -> LoadedFile:
    """`frame` as the diagnosis reads an upload: its profile is the one ingest makes."""
    config = load_use_case(use_case, config_root)
    profile = profile_dataset(
        frame,
        config,
        upload_id=upload_id,
        file_name=name,
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    return LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=profile, file_rows=file_rows)
