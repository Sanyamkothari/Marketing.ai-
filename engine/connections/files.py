"""What the two file stores (S3 and Azure Blob) share: which objects can be picked, and their preview."""

from __future__ import annotations

import csv
import io
from pathlib import PurePosixPath

import pandas as pd

from engine.connections.base import (
    PREVIEW_MAX_PARQUET_BYTES,
    ConnectorError,
    FileFormat,
    Preview,
    frame_preview,
)
from engine.stages.ingest import CSV_SUFFIXES, PARQUET_SUFFIXES

__all__ = ["csv_preview", "file_format_of", "importable", "parent_of", "parquet_preview", "preview_bytes"]


def importable(key: str) -> bool:
    suffix = PurePosixPath(key).suffix.lower()
    return suffix in CSV_SUFFIXES or suffix in PARQUET_SUFFIXES


def file_format_of(key: str) -> FileFormat:
    suffix = PurePosixPath(key).suffix.lower()
    if suffix in PARQUET_SUFFIXES:
        return "parquet"
    if suffix in CSV_SUFFIXES:
        return "csv"
    raise ConnectorError(
        "CONNECTION_NOT_A_TABLE_FILE",
        "Only CSV and Parquet files can be used.",
        "Pick a file ending in .csv or .parquet.",
    )


def parent_of(prefix: str) -> str | None:
    """The prefix one folder up, or None at the top (`a/b/` → `a/`, `a/` → ``)."""
    if not prefix:
        return None
    trimmed = prefix.rstrip("/")
    return trimmed.rsplit("/", 1)[0] + "/" if "/" in trimmed else ""


def csv_preview(head: bytes, truncated: bool) -> Preview:
    """The first rows of a CSV from its first bytes; a cut-off last line is dropped, never shown."""
    if truncated and b"\n" in head:
        head = head[: head.rindex(b"\n") + 1]
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = head.decode("latin-1")
    try:
        frame = pd.read_csv(io.StringIO(text), sep=None, engine="python", nrows=20, dtype=str)
    except (ValueError, pd.errors.ParserError, csv.Error):
        raise ConnectorError(
            "CONNECTION_PREVIEW_UNREADABLE",
            "This file could not be read as a table.",
            "Check it is a CSV file with a header row, or pick another file.",
        ) from None
    return frame_preview(frame)


def parquet_preview(data: bytes) -> Preview:
    try:
        frame = pd.read_parquet(io.BytesIO(data))
    except (ValueError, OSError):
        raise ConnectorError(
            "CONNECTION_PREVIEW_UNREADABLE",
            "This file could not be read as a Parquet table.",
            "Pick another file, or export it again as Parquet or CSV.",
        ) from None
    return frame_preview(frame)


def preview_bytes(data: bytes, file_format: FileFormat, *, truncated: bool) -> Preview:
    return csv_preview(data, truncated) if file_format == "csv" else parquet_preview(data)


def parquet_too_big_to_preview() -> ConnectorError:
    megabytes = PREVIEW_MAX_PARQUET_BYTES // (1024 * 1024)
    return ConnectorError(
        "CONNECTION_PREVIEW_TOO_LARGE",
        f"This Parquet file is larger than {megabytes} MB, so it is not previewed.",
        "You can still import it: the checks after import show its columns and rows.",
        status=413,
    )
