"""Rows the CSV reader dropped without saying so (Plan O M240, DEC-1810 (b); probe `X_PARSER_ROW_LOSS`).

`engine.stages.ingest` reads a CSV with `on_bad_lines="warn"`: a line with more fields than the header is
left out and a warning goes to a log nobody reads. The file still "reads", the row count is smaller than the
file's, and the person is told nothing (readiness gap [RDY] 1.1). :func:`check_parser_loss` closes it:

* it counts the records of the file by the quote-aware rules the reader uses (a quoted field may hold a line
  break; a blank line is not a record), and compares them with the rows the reader kept;
* it names the physical lines, counted from 1 with the header as line 1, where a record with too many fields
  starts, at most :data:`MAX_LINES_NAMED` of them (`lines_more` counts the rest);
* when any row was dropped the verdict is `not_ready` with code `X_PARSER_ROW_LOSS`, and the message says in
  plain words which lines to look at and what usually causes it.

The report holds counts and line numbers only; no cell value is read into it, so it is safe wherever a
count is (it never reaches a model except through the egress gate like any other tool result).
A Parquet file has no lines to lose and is `ready`.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from engine.config import StrictBase
from engine.stages import ingest

if TYPE_CHECKING:
    from engine.stages.ingest import ReadResult
    from engine.storage import Storage

__all__ = [
    "MAX_LINES_NAMED",
    "PARSER_LOSS_CODE",
    "PARSER_LOSS_CODES",
    "ParserLossReport",
    "check_parser_loss",
]

PARSER_LOSS_CODE: Final[str] = "X_PARSER_ROW_LOSS"
PARSER_LOSS_CODES: Final[frozenset[str]] = frozenset({PARSER_LOSS_CODE})
"""The codes this module can return; the integrator adds them to `PLAN_O_CODES` with their help entries."""

MAX_LINES_NAMED: Final[int] = 20


class ParserLossReport(StrictBase):
    """Whether reading the file lost rows, and which lines were lost."""

    verdict: Literal["ready", "not_ready"] = Field(description="`not_ready` when any row was dropped.")
    code: str | None = Field(description="`X_PARSER_ROW_LOSS` when not ready; otherwise null.")
    records: int = Field(ge=0, description="Data records the file holds, counted by the reader's own rules.")
    rows_read: int = Field(ge=0, description="Rows the reader kept.")
    rows_dropped: int = Field(ge=0, description="Records the reader left out.")
    header_fields: int = Field(ge=0, description="Columns the header names.")
    lines: tuple[int, ...] = Field(
        description="1-based file lines (the header is line 1) where a record with too many fields starts."
    )
    lines_more: int = Field(ge=0, description="Further such lines beyond the ones named.")
    unexplained: int = Field(
        ge=0, description="Dropped records no line could be named for (0 when every one is named)."
    )
    message: str = Field(description="What happened, in plain words; empty when ready.")
    fix: str = Field(description="What to do about it, in plain words; empty when ready.")


def _blank(record: list[str]) -> bool:
    """A record the reader skips: nothing on the line, or one cell of white space."""
    return not record or (len(record) == 1 and not record[0].strip())


def _join(numbers: tuple[int, ...], more: int) -> str:
    shown = ", ".join(str(number) for number in numbers)
    if more:
        return f"{shown} and {more} more"
    if len(numbers) > 1:
        head, last = ", ".join(str(number) for number in numbers[:-1]), numbers[-1]
        return f"{head} and {last}"
    return shown


def check_parser_loss(
    storage: Storage,
    key: str,
    *,
    read: ReadResult | None = None,
    file_format: Literal["csv", "parquet"] | None = None,
) -> ParserLossReport:
    """Compare the records of the CSV at `key` with the rows the reader kept, and name the lines it dropped.

    `read` is the `ReadResult` of a read the caller already made of this file (its row count, delimiter and
    encoding are used as they are); without it the file is read once here, in one streaming pass. Raises
    `engine.stages.ingest.IngestError` for a file that cannot be read at all, as the reader does.
    """
    resolved = file_format if file_format is not None else ingest.file_format_for(key)
    if read is None:
        read = ingest.read_upload(storage, key, file_format=resolved, row_cap=1)
    if resolved == "parquet" or read.file_format == "parquet":
        return _ready(read.row_count, header_fields=len(read.frame.columns))

    lines: list[int] = []
    over = 0
    records = 0
    width = 0
    with storage.open_read(key) as raw:
        text = io.TextIOWrapper(raw, encoding=read.encoding, newline="")
        reader = csv.reader(text, delimiter=read.delimiter or ",", quotechar='"', skipinitialspace=True)
        header = next(reader, None)
        # The header record may start after blank lines; the reader skips them too.
        while header is not None and _blank(header):
            header = next(reader, None)
        width = len(header or [])
        previous_end = reader.line_num
        for record in reader:
            start = previous_end + 1
            previous_end = reader.line_num
            if _blank(record):
                continue
            records += 1
            if len(record) > width:
                over += 1
                if len(lines) < MAX_LINES_NAMED:
                    lines.append(start)
    dropped = max(records - read.row_count, over)
    if dropped == 0:
        return _ready(records, header_fields=width, rows_read=read.row_count)
    named = tuple(lines)
    more = max(over - len(named), 0)
    unexplained = max(dropped - over, 0)
    return ParserLossReport(
        verdict="not_ready",
        code=PARSER_LOSS_CODE,
        records=records,
        rows_read=read.row_count,
        rows_dropped=dropped,
        header_fields=width,
        lines=named,
        lines_more=more,
        unexplained=unexplained,
        message=_message(dropped, named, more, unexplained, width),
        fix=(
            "Open the file and look at those lines. A stray comma, or a quote that is never closed, is the "
            "usual cause. Fix them, or take them out, and upload the file again."
            if named
            else "Open the file and look for lines with a stray comma or a quote that is never closed. "
            "Fix them, or take them out, and upload the file again."
        ),
    )


def _ready(records: int, *, header_fields: int, rows_read: int | None = None) -> ParserLossReport:
    return ParserLossReport(
        verdict="ready",
        code=None,
        records=records,
        rows_read=records if rows_read is None else rows_read,
        rows_dropped=0,
        header_fields=header_fields,
        lines=(),
        lines_more=0,
        unexplained=0,
        message="",
        fix="",
    )


def _message(dropped: int, named: tuple[int, ...], more: int, unexplained: int, width: int) -> str:
    rows = f"{dropped:,} row{'s' if dropped != 1 else ''}"
    columns = f"{width:,} column{'s' if width != 1 else ''}"
    if not named:
        return (
            f"{rows} of this file {'was' if dropped == 1 else 'were'} left out when it was read, and no single "
            "line could be named."
        )
    where = f"line {_join(named, more)}" if len(named) + more == 1 else f"lines {_join(named, more)}"
    text = (
        f"{rows} of this file {'was' if dropped == 1 else 'were'} left out when it was read: "
        f"{where} {'has' if len(named) + more == 1 else 'have'} more values than the {columns} the header names."
    )
    if unexplained:
        text += f" {unexplained:,} more could not be traced to a line."
    return text
