"""Read a client's file the way v1's Data Doctor does, then map it to events.

A real export is messier than a sample: a title row above the column names, a "Total" row at the end,
a semicolon delimiter, Windows-1252 text, a workbook instead of a CSV, "N/A" where a cell should be
empty. v1 already handles all of that, in two read-only steps the upload route runs on every file:

1. `engine.intake.run.intake_file` decides from the file's head and tail whether a rule is needed and,
   when one is, rewrites the table as plain UTF-8 CSV (keeping the original beside it);
2. `engine.stages.ingest.read_upload` reads the result into a DataFrame, keeping leading zeros in
   key-like columns ("007" stays "007").

This bridge runs exactly those two steps in a private scratch folder, then hands the frame to
`autonomy.data.mapping.map_frame`. It changes nothing in v1 and nothing on disk outside the scratch
folder. The v1 imports sit inside the functions, because `engine.stages.ingest` is heavy (DuckDB);
importing this module costs nothing.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from autonomy.contracts import Event
from autonomy.data.mapping import EventMapping, MappingProblem, map_frame

__all__ = ["DoctorMapped", "DoctorRead", "map_file", "read_with_intake"]

_FOLDER = "intake"


@dataclass(frozen=True)
class DoctorRead:
    """The table v1 read, and what its intake did to get there."""

    frame: pd.DataFrame
    notes: tuple[str, ...]
    """The plain sentences v1's upload screen shows, e.g. "2 rows above the column names were left out"."""
    rewritten: bool
    """True when intake had to rewrite the file (a rule applied, or it was a workbook)."""
    renamed: dict[str, str]
    """Columns intake named or renamed: what the header said -> the name the mapping must use."""


@dataclass(frozen=True)
class DoctorMapped:
    """Events from a file read through v1's intake, the rows that failed, and intake's notes."""

    events: list[Event]
    problems: list[MappingProblem]
    notes: tuple[str, ...]


def read_with_intake(
    path: Path,
    *,
    text_columns: tuple[str, ...] = (),
    sheet: str | None = None,
    work_dir: Path | None = None,
) -> DoctorRead:
    """Read the CSV, workbook or Parquet file at `path` through v1's intake and ingest.

    `text_columns` are read as text whatever they look like (pass the id columns). `work_dir` is
    where intake may write its copies; a temporary folder, removed afterwards, when not given.
    Raises `engine.stages.ingest.IngestError` for a file v1 cannot read at all, with v1's message.
    """
    if work_dir is not None:
        return _read(path, text_columns, sheet, work_dir)
    with tempfile.TemporaryDirectory(prefix="doctor-bridge-") as scratch:
        return _read(path, text_columns, sheet, Path(scratch))


def map_file(
    path: Path,
    mapping: EventMapping,
    source: str,
    *,
    sheet: str | None = None,
    work_dir: Path | None = None,
) -> DoctorMapped:
    """Read `path` through v1's intake, then map it to events with `mapping`.

    The mapping names columns as they are *after* intake; `DoctorRead.renamed` says which changed.
    Id-like columns the mapping names are read as text, so a leading zero is never lost.
    """
    text = tuple(
        c
        for c in (mapping.customer_id, mapping.decision_id_column, mapping.item_column, *mapping.row_key)
        if c is not None
    )
    read = read_with_intake(path, text_columns=text, sheet=sheet, work_dir=work_dir)
    events, problems = map_frame(read.frame, mapping, source)
    return DoctorMapped(events, problems, read.notes)


def _read(path: Path, text_columns: tuple[str, ...], sheet: str | None, root: Path) -> DoctorRead:
    from engine.intake.config import IntakeConfig
    from engine.intake.run import intake_file, received_format_for
    from engine.stages.ingest import read_upload
    from engine.storage import LocalStorage

    received_format = received_format_for(path.name)
    suffix = path.suffix.lower()
    storage = LocalStorage(root)
    received_key = f"{_FOLDER}/received{suffix}"
    storage.write_bytes(received_key, path.read_bytes())
    result = intake_file(
        storage,
        received_key=received_key,
        received_format=received_format,
        # A CSV is rewritten in place (the original is kept aside); a workbook becomes a new CSV.
        csv_key=received_key if received_format == "csv" else f"{_FOLDER}/table.csv",
        original_key=f"{_FOLDER}/original{suffix}",
        config=IntakeConfig(),
        sheet=sheet,
    )
    read = read_upload(
        storage,
        result.source_key,
        file_format=result.file_format,
        keep_all_rows=True,
        keep_leading_zeros=True,
        text_columns=text_columns,
    )
    frame = read.all_rows if read.all_rows is not None else read.frame
    report = result.report
    renamed = {r.original: r.name for r in report.renamed if r.original}
    return DoctorRead(frame=frame, notes=tuple(report.notes), rewritten=report.applied, renamed=renamed)
