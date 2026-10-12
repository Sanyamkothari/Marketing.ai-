"""Data Doctor tools for input formats and parser safety (Plan O M240, DEC-1810).

Standalone mode (`plan_o_standalone.md`): M232's registry is not on this branch, so the four tools are declared
here as plain frozen specs shaped like the design's section 5.2, with one `register(registry)` the integrator
wires to M232. Nothing here imports the registry.

| Tool | Kind | Mode | Exposure | Wraps |
|---|---|---|---|---|
| `read_other_format` | READ | rules | session-only | `engine.intake.other_formats.read_other_format` |
| `read_workbook_sheets` | READ | rules | session-only | `engine.intake.sheets.split_workbook` |
| `check_parser_loss` | READ | rules | aggregate | `engine.doctor.parser_loss.check_parser_loss` |
| `find_excel_serial_dates` | READ | rules | session-only | `engine.agent.formats.find_excel_serial_dates` |

No tool takes a storage key: a call names a file by its id in the case, and the context turns the id into a
key inside the case's own client scope, so a tool cannot read another client's file. No tool returns a row:
the results are counts, column names, line numbers, file names and plain sentences from our own rules. The one
exception is `find_excel_serial_dates`, whose notes carry the date range read from a column's cells (it is
`session-only`, like every tool that can show something from the file). A refusal (a zip with two tables, a
JSON file that is not a table, a file that cannot be read) is a normal result with `status: "refused"` and the
reason, not an exception, so the agent can tell the person what to do.

Two things the integrator's context must provide (the case is M231's, not on this branch):

* `readable_key(file_id)`: the CSV or Parquet the engine actually reads for the file (the `source.csv` made
  from a `.gz`, `.zip` or JSON file, or the CSV intake rewrote), not the bytes as they arrived;
* `add_file(parent_file_id, csv_key, label)`: puts a file made from another into the case and returns its
  id, so a workbook's customer sheet and bills sheet become two files of one case.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from engine.agent.formats import find_excel_serial_dates
from engine.config import StrictBase
from engine.doctor.parser_loss import ParserLossReport, check_parser_loss
from engine.intake.config import IntakeConfig
from engine.intake.other_formats import read_other_format
from engine.intake.sheets import split_workbook
from engine.stages.ingest import IngestError

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "FORMAT_TOOLS",
    "CheckParserLossInput",
    "CheckParserLossOutput",
    "FindExcelSerialDatesInput",
    "FindExcelSerialDatesOutput",
    "FormatToolContext",
    "FormatToolSpec",
    "ReadOtherFormatInput",
    "ReadOtherFormatOutput",
    "ReadWorkbookSheetsInput",
    "ReadWorkbookSheetsOutput",
    "register",
]

ToolKind = Literal["read", "propose", "ask"]
Exposure = Literal["session-only", "aggregate"]


class FormatToolContext(Protocol):
    """What these tools need from the case they run in. The integrator implements it over M231's case.

    A file is named by `file_id`, its id in the case. The context resolves it inside the case's client
    scope, so a tool never sees a key, and a key of another client is not reachable.
    """

    @property
    def storage(self) -> Storage: ...

    def received_file(self, file_id: str) -> tuple[str, str]:
        """`(storage key, file name)` of the file as it arrived."""
        ...

    def derived_key(self, file_id: str, name: str) -> str:
        """Where a file made from `file_id` is stored, e.g. `name="source.csv"` or `"sheet-2.csv"`."""
        ...

    def readable_key(self, file_id: str) -> str:
        """Storage key of the CSV or Parquet the engine reads for `file_id` (never a .gz, .zip or JSON file)."""
        ...

    def add_file(self, parent_file_id: str, csv_key: str, label: str) -> str:
        """Add the CSV at `csv_key`, made from `parent_file_id`, to the case as a file of its own; its id."""
        ...

    def frame(self, file_id: str) -> pd.DataFrame:
        """The file's rows as the engine reads them (capped at the profile cap)."""
        ...


class _Model(StrictBase):
    """Every tool model refuses unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class FormatToolSpec:
    """One tool, shaped like the design's registry entry (section 5.2)."""

    name: str
    kind: ToolKind
    mode: Literal["rules", "ai"]
    exposure: Exposure
    role: Literal["viewer", "analyst", "approver", "admin"]
    """The least role that may call it (`RoutePolicy.role`); every READ tool is open to a Viewer."""
    description: str
    wraps: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    run: Callable[[Any, FormatToolContext], BaseModel]


# ---------------------------------------------------------------------------
# read_other_format
# ---------------------------------------------------------------------------
class ReadOtherFormatInput(_Model):
    file_id: str = Field(min_length=1, max_length=128, description="The file's id in the case.")


class ReadOtherFormatOutput(_Model):
    status: Literal["read", "refused"]
    code: str | None = Field(default=None, description="The refusal's code when refused.")
    message: str = Field(default="", description="The refusal in plain words when refused; empty otherwise.")
    inner_format: Literal["csv", "json", "jsonl"] | None = None
    container: Literal["none", "gz", "zip"] | None = None
    file_name: str | None = Field(default=None, description="The file inside a .gz or .zip.")
    rows: int | None = Field(default=None, ge=0, description="Records written; null when copied as a CSV.")
    columns: tuple[str, ...] = Field(default=(), description="Columns of a converted JSON file.")
    notes: tuple[str, ...] = Field(default=(), description="What was done, in plain sentences.")


def _run_read_other_format(given: ReadOtherFormatInput, ctx: FormatToolContext) -> ReadOtherFormatOutput:
    key, name = ctx.received_file(given.file_id)
    try:
        result = read_other_format(
            ctx.storage,
            received_key=key,
            csv_key=ctx.derived_key(given.file_id, "source.csv"),
            file_name=name,
        )
    except IngestError as exc:
        return ReadOtherFormatOutput(status="refused", code=exc.code, message=exc.message)
    return ReadOtherFormatOutput(
        status="read",
        inner_format=result.inner_format,
        container=result.container,
        file_name=result.inner_name,
        rows=result.rows,
        columns=result.columns,
        notes=result.report.notes,
    )


# ---------------------------------------------------------------------------
# read_workbook_sheets
# ---------------------------------------------------------------------------
class ReadWorkbookSheetsInput(_Model):
    file_id: str = Field(min_length=1, max_length=128, description="The workbook's id in the case.")
    sheets: tuple[str, ...] | None = Field(
        default=None, max_length=20, description="Sheets to read; all visible sheets with a table when null."
    )


class SheetOut(_Model):
    sheet: str
    label: str
    rows: int = Field(ge=0)
    file_position: int = Field(ge=1, description="Position of the sheet in the workbook, from 1.")
    file_id: str = Field(description="The sheet's id as a file of the case.")


class LeftOutOut(_Model):
    sheet: str
    reason: Literal["hidden", "no_table", "over_limit"]


class ReadWorkbookSheetsOutput(_Model):
    status: Literal["read", "refused"]
    code: str | None = None
    message: str = ""
    files: tuple[SheetOut, ...] = ()
    left_out: tuple[LeftOutOut, ...] = ()


def _run_read_workbook_sheets(
    given: ReadWorkbookSheetsInput, ctx: FormatToolContext
) -> ReadWorkbookSheetsOutput:
    key, name = ctx.received_file(given.file_id)
    positions: dict[str, int] = {}

    def csv_key_for(position: int, sheet: str) -> str:
        positions[sheet] = position
        return ctx.derived_key(given.file_id, f"sheet-{position}.csv")

    try:
        split = split_workbook(
            ctx.storage,
            received_key=key,
            csv_key_for=csv_key_for,
            config=IntakeConfig(),
            sheets=given.sheets,
            file_name=name,
        )
    except IngestError as exc:
        return ReadWorkbookSheetsOutput(status="refused", code=exc.code, message=exc.message)
    # Each sheet becomes a file of the case, in workbook order, only after the whole split succeeded.
    return ReadWorkbookSheetsOutput(
        status="read",
        files=tuple(
            SheetOut(
                sheet=f.sheet,
                label=f.label,
                rows=f.rows,
                file_position=positions[f.sheet],
                file_id=ctx.add_file(given.file_id, f.csv_key, f.label),
            )
            for f in split.files
        ),
        left_out=tuple(LeftOutOut(sheet=item.sheet, reason=item.reason) for item in split.left_out),
    )


# ---------------------------------------------------------------------------
# check_parser_loss
# ---------------------------------------------------------------------------
class CheckParserLossInput(_Model):
    file_id: str = Field(min_length=1, max_length=128, description="The file's id in the case.")


class CheckParserLossOutput(_Model):
    status: Literal["checked", "refused"]
    code: str | None = Field(default=None, description="Why the file could not be read, when refused.")
    message: str = Field(default="", description="The refusal in plain words when refused; empty otherwise.")
    report: ParserLossReport | None = Field(default=None, description="The check's result when checked.")


def _run_check_parser_loss(given: CheckParserLossInput, ctx: FormatToolContext) -> CheckParserLossOutput:
    # The CSV the engine reads, not the bytes as they arrived: a .csv.gz or .zip holds its table inside.
    try:
        return CheckParserLossOutput(
            status="checked", report=check_parser_loss(ctx.storage, ctx.readable_key(given.file_id))
        )
    except IngestError as exc:
        return CheckParserLossOutput(status="refused", code=exc.code, message=exc.message)


# ---------------------------------------------------------------------------
# find_excel_serial_dates
# ---------------------------------------------------------------------------
class FindExcelSerialDatesInput(_Model):
    file_id: str = Field(min_length=1, max_length=128, description="The file's id in the case.")


class SerialDateColumn(_Model):
    column: str
    non_empty: int = Field(ge=0)
    convertible: int = Field(ge=0)
    failed: int = Field(ge=0)
    notes: tuple[str, ...]
    excel_serial: bool = True
    date_1904: bool = False


class FindExcelSerialDatesOutput(_Model):
    columns: tuple[SerialDateColumn, ...]


def _run_find_excel_serial_dates(
    given: FindExcelSerialDatesInput, ctx: FormatToolContext
) -> FindExcelSerialDatesOutput:
    issues = find_excel_serial_dates(ctx.frame(given.file_id))
    return FindExcelSerialDatesOutput(
        columns=tuple(
            SerialDateColumn(
                column=issue.column,
                non_empty=issue.non_empty,
                convertible=issue.convertible,
                failed=issue.failed,
                notes=issue.notes,
            )
            for issue in issues
        )
    )


FORMAT_TOOLS: Final[tuple[FormatToolSpec, ...]] = (
    FormatToolSpec(
        name="read_other_format",
        kind="read",
        mode="rules",
        exposure="session-only",
        role="viewer",
        description=(
            "Reads a JSON, JSON Lines, .gz or .zip file as a table. A zip with more than one table, a protected "
            "or oversized archive, or JSON that is not a table is refused with the reason."
        ),
        wraps="engine.intake.other_formats.read_other_format",
        input_model=ReadOtherFormatInput,
        output_model=ReadOtherFormatOutput,
        run=_run_read_other_format,
    ),
    FormatToolSpec(
        name="read_workbook_sheets",
        kind="read",
        mode="rules",
        exposure="session-only",
        role="viewer",
        description="Reads each sheet of a workbook that holds a table as its own file.",
        wraps="engine.intake.sheets.split_workbook",
        input_model=ReadWorkbookSheetsInput,
        output_model=ReadWorkbookSheetsOutput,
        run=_run_read_workbook_sheets,
    ),
    FormatToolSpec(
        name="check_parser_loss",
        kind="read",
        mode="rules",
        exposure="aggregate",
        role="viewer",
        description=(
            "Compares the records of the CSV the engine reads with the rows the reader kept, and names the "
            "lines it dropped (X_PARSER_ROW_LOSS) or the columns every row was shifted by "
            "(X_PARSER_COLUMNS_SHIFTED)."
        ),
        wraps="engine.doctor.parser_loss.check_parser_loss",
        input_model=CheckParserLossInput,
        output_model=CheckParserLossOutput,
        run=_run_check_parser_loss,
    ),
    FormatToolSpec(
        name="find_excel_serial_dates",
        kind="read",
        mode="rules",
        exposure="session-only",
        role="viewer",
        description=(
            "Finds columns that hold dates as Excel's day numbers (44927 for 1 January 2023); the fix is a "
            "parse_date step with excel_serial."
        ),
        wraps="engine.agent.formats.find_excel_serial_dates",
        input_model=FindExcelSerialDatesInput,
        output_model=FindExcelSerialDatesOutput,
        run=_run_find_excel_serial_dates,
    ),
)


class _Registry(Protocol):
    def register(self, tool: FormatToolSpec) -> None: ...


def register(registry: _Registry) -> None:
    """Add this module's tools to `registry`, one `registry.register(spec)` each.

    The integrator passes M232's registry (or an adapter that turns a `FormatToolSpec` into its entry).
    """
    for spec in FORMAT_TOOLS:
        registry.register(spec)
