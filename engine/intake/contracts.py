"""What intake decided about one file, stored as `intake.json` beside the upload (Plan L M150, DEC-1501).

Every figure is counted while the file is read; `notes` are the plain sentences the upload screen shows,
built from those figures by `engine.intake.run.notes_for` and nothing else. No cell value is stored
except a left-out row's first cell (`footer_labels`), masked by `engine.pii.redact_text` and cut to 40
characters first, so a person can see what was left out.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from engine.config import StrictBase
from engine.contracts import Artefact

__all__ = ["IntakeReport", "MissingWordsColumn", "RenamedColumn", "SheetInfo"]


class SheetInfo(StrictBase):
    """One sheet of a workbook, as intake saw it."""

    name: str = Field(description="The sheet's name in the workbook.")
    rows: int | None = Field(description="Rows the workbook says the sheet has; null when it does not say.")
    looks_like_data: bool = Field(description="True when a row of column names with data under it was found.")
    hidden: bool = Field(
        description="True when the sheet is hidden in Excel; a hidden sheet is read only when named."
    )
    used: bool = Field(description="True for the one sheet that was read.")


class RenamedColumn(StrictBase):
    """A column whose name intake gave or changed."""

    position: int = Field(description="1-based position of the column in the file as read.")
    original: str = Field(description="What the header row said (empty when it said nothing).")
    name: str = Field(description="The name the column has now.")
    reason: Literal["repeated", "blank", "joined"] = Field(
        description="`repeated`: the name was already used; `blank`: the header said nothing; `joined`: two header rows joined."
    )


class MissingWordsColumn(StrictBase):
    """A column where words meaning "no value" were read as empty."""

    column: str = Field(description="The column, by its name after intake.")
    words: tuple[str, ...] = Field(description="The words found, trimmed and lower-cased, at most 10.")
    cells: int = Field(description="Cells emptied in the whole file.")


class IntakeReport(Artefact):
    """`uploads/<id>/intake.json` (and a source's): how the file was read, and what was changed on the way."""

    source_format: Literal["csv", "xlsx", "parquet", "json", "jsonl"] = Field(
        description="The kind of table the file holds (the file inside a `.gz` or `.zip`, when it is one)."
    )
    container: Literal["none", "gz", "zip"] | None = Field(
        default=None,
        description="`gz` or `zip` when the table arrived inside an archive (Plan O M240); null otherwise.",
    )
    applied: bool = Field(
        description="True when the file was rewritten; the original is then kept under `original_key`."
    )
    turned_off: bool = Field(
        default=False, description="True when the person asked for the file to be read as it is."
    )
    original_key: str | None = Field(
        default=None, description="Storage key of the file as received, when rewritten."
    )
    sheet: str | None = Field(default=None, description="The sheet read, for a workbook.")
    sheets: tuple[SheetInfo, ...] = Field(default=(), description="Every sheet of a workbook.")
    delimiter: str | None = Field(default=None, description="The delimiter the CSV was read with.")
    encoding: str | None = Field(default=None, description="The encoding the CSV was read with.")
    header_row: int = Field(default=1, description="1-based row of the file holding the column names.")
    header_rows: int = Field(default=1, description="2 when two header rows were joined into one name.")
    rows_above_header: int = Field(default=0, description="Rows above the column names that were left out.")
    footer_rows: int = Field(default=0, description="Total or note rows left out at the end.")
    footer_labels: tuple[str, ...] = Field(
        default=(), description="The first cell of each left-out row, masked and cut to 40 characters."
    )
    empty_rows: int = Field(default=0, description="Rows with every cell empty that were left out.")
    columns_dropped: int = Field(
        default=0, description="Columns with no name and no value in the rows looked at, left out."
    )
    cells_left_out: int = Field(
        default=0,
        description="Filled cells in a left-out column or to the right of the table (counted, never silent).",
    )
    renamed: tuple[RenamedColumn, ...] = Field(
        default=(), description="Columns whose name intake gave or changed."
    )
    missing_words: tuple[MissingWordsColumn, ...] = Field(
        default=(), description="Columns where words meaning 'no value' were read as empty."
    )
    rows_written: int | None = Field(
        default=None, description="Data rows in the file as read, when rewritten."
    )
    notes: tuple[str, ...] = Field(
        default=(), description="What intake did, in plain sentences, for the upload screen."
    )
