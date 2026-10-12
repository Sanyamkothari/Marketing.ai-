"""Each sheet of a workbook as its own file (Plan O M240, DEC-1810 (e)).

Intake reads one sheet of a workbook, the biggest visible table (`engine.intake.run`, DEC-1502). A workbook
that holds a customer sheet and a bills sheet is two files of one case, not one file and a note saying the
other sheet exists ([INV] 1.2, Plan L's request). :func:`split_workbook` writes every visible sheet that
holds a table as its own CSV, by the same rules a single sheet is read with, and says which sheets it left
out and why. It writes files and nothing else: where they are filed (an upload each, a file of a case) is the
caller's, through `csv_key_for`.

A sheet is left out when it is hidden (unless it is named), holds no table (a cover page, a note), or comes
after the limit of :data:`MAX_SHEETS`. A named sheet that does not exist is refused, as it is for a single
sheet.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, Literal

from engine.intake.config import IntakeConfig
from engine.intake.contracts import IntakeReport, SheetInfo
from engine.intake.excel import WorkbookError, check_workbook, open_workbook
from engine.intake.run import _choose_sheet, _convert_workbook
from engine.stages.ingest import IngestError
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = ["MAX_SHEETS", "LeftOutSheet", "SheetFile", "WorkbookSplit", "split_workbook"]

_LOG = get_logger(__name__)

MAX_SHEETS: Final[int] = 20
"""The most sheets one workbook is split into, the same cap the Doctor route puts on files."""


@dataclass(frozen=True)
class SheetFile:
    """One sheet written as a CSV."""

    sheet: str
    csv_key: str
    label: str
    """What the file is called on screen: `<workbook> - <sheet>`."""
    rows: int
    report: IntakeReport


@dataclass(frozen=True)
class LeftOutSheet:
    """A sheet that was not written, and why."""

    sheet: str
    reason: Literal["hidden", "no_table", "over_limit"]


@dataclass(frozen=True)
class WorkbookSplit:
    """The files a workbook became, and the sheets it did not."""

    workbook_key: str
    files: tuple[SheetFile, ...]
    left_out: tuple[LeftOutSheet, ...]
    sheets: tuple[SheetInfo, ...]
    """Every sheet of the workbook as intake saw it."""


def split_workbook(
    storage: Storage,
    *,
    received_key: str,
    csv_key_for: Callable[[int, str], str],
    config: IntakeConfig | None = None,
    sheets: Sequence[str] | None = None,
    file_name: str | None = None,
) -> WorkbookSplit:
    """Write each table sheet of the workbook at `received_key` as a CSV at `csv_key_for(position, sheet)`.

    `sheets` names the ones to write (compared without case or edge spaces; a hidden sheet is written when it
    is named); without it every visible sheet that holds a table is written. `position` counts from 1 in the
    workbook's own order. Raises `engine.stages.ingest.IngestError` for a workbook that cannot be read, has no
    table at all, or has no sheet of a name that was asked for.
    """
    rules = config or IntakeConfig()
    path = storage.local_path(received_key)
    try:
        check_workbook(path, rules)
        book = open_workbook(path)
    except WorkbookError as exc:
        raise IngestError(exc.code, exc.message) from exc
    try:
        try:
            infos, _chosen = _choose_sheet(book, None, rules)
        except WorkbookError as exc:
            raise IngestError(exc.code, exc.message) from exc
    finally:
        book.close()
    wanted = _wanted(infos, sheets)
    stem = PurePosixPath(file_name or received_key).stem
    files: list[SheetFile] = []
    left_out: list[LeftOutSheet] = []
    for position, info in enumerate(infos, start=1):
        if info.name not in wanted:
            if sheets is None:
                left_out.append(LeftOutSheet(info.name, "hidden" if info.hidden else "no_table"))
            continue
        if len(files) >= MAX_SHEETS:
            left_out.append(LeftOutSheet(info.name, "over_limit"))
            continue
        key = csv_key_for(position, info.name)
        try:
            with storage.open_write(key) as sink:
                report = _convert_workbook(
                    path, sink, rules, sheet=info.name, enabled=True, original_key=received_key
                )
        except IngestError:
            if storage.exists(key):
                storage.delete(key)
            raise
        files.append(
            SheetFile(
                sheet=info.name,
                csv_key=key,
                label=f"{stem} - {info.name}",
                rows=report.rows_written or 0,
                report=report,
            )
        )
    if not files:
        raise IngestError("UPLOAD_NO_ROWS", "None of this workbook's sheets holds a table.")
    _LOG.info("intake.workbook_split sheets=%d written=%d left_out=%d", len(infos), len(files), len(left_out))
    return WorkbookSplit(
        workbook_key=received_key,
        files=tuple(files),
        left_out=tuple(left_out),
        sheets=tuple(infos),
    )


def _wanted(infos: list[SheetInfo], sheets: Sequence[str] | None) -> set[str]:
    """The names of the sheets to write: the ones asked for, else every visible sheet with a table."""
    if sheets is None:
        return {info.name for info in infos if info.looks_like_data and not info.hidden}
    names = [info.name for info in infos]
    chosen: set[str] = set()
    for asked in sheets:
        exact = [name for name in names if name == asked]
        folded = [name for name in names if name.strip().casefold() == asked.strip().casefold()]
        if not (exact or folded):
            listed = ", ".join(f'"{name}"' for name in names)
            raise IngestError(
                "UPLOAD_SHEET_NOT_FOUND",
                f'This workbook has no sheet called "{asked}". Its sheets are {listed}.',
            )
        chosen.add((exact or folded)[0])
    return chosen
