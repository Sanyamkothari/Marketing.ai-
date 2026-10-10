"""Intake on a stored file (Plan L M150, DEC-1501): decide from its head and tail, rewrite only when needed.

The entry point is :func:`intake_file`. It is called on every file that becomes an upload or a
client source (`POST /uploads`, `POST /connections/{id}/import`, `POST /clients/{id}/sources`, a
connection-backed source's pull), training and scoring alike:

* **A CSV that needs no rule is not touched.** Its bytes stay where they landed and ingest reads them
  as it always did. Deciding costs a read of its first 64 KB, its first `sample_rows` records and its
  last 256 KB.
* **A CSV that needs a rule** is copied to `original.csv` and rewritten in one streaming pass into the
  key ingest reads, as UTF-8 with commas; every cell the rules leave alone is written as it was read.
* **A workbook** is always rewritten (ingest reads CSV and Parquet only), from the chosen sheet, and
  kept as it arrived under `original.xlsx`.
* **Parquet** is typed already; it is read as it is.

The decision is the rules' alone and they run again on next month's file, so the same export is read
the same way every month (the "replay" of the plan's M150). A person who disagrees uploads the file
again with intake turned off (`intake=false`): the file is then read exactly as it was before Plan L.
"""

from __future__ import annotations

import csv
import io
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, Final, Literal

from engine import pii
from engine.intake.config import IntakeConfig
from engine.intake.contracts import IntakeReport, MissingWordsColumn, RenamedColumn, SheetInfo
from engine.intake.excel import (
    EXCEL_SUFFIXES,
    LEGACY_EXCEL_SUFFIXES,
    SHEET_SCAN_ROWS,
    WorkbookError,
    check_workbook,
    looks_like_data,
    open_workbook,
    sheet_bytes,
    sheet_rows,
)
from engine.intake.grid import (
    HeaderDecision,
    find_footer,
    find_header,
    is_empty_record,
    keep_columns,
    missing_word_columns,
    name_columns,
    nonempty,
)
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "INTAKE_REPORT_FILENAME",
    "IntakeResult",
    "ReceivedFormat",
    "companion_keys",
    "intake_file",
    "notes_for",
    "received_format_for",
    "workbook_csv",
]

_LOG = get_logger(__name__)

INTAKE_REPORT_FILENAME: Final[str] = "intake.json"

ReceivedFormat = Literal["csv", "parquet", "xlsx"]

COMPANION_FILENAMES: Final[tuple[str, ...]] = ("original.csv", "original.xlsx", INTAKE_REPORT_FILENAME)
"""What intake may leave beside the file ingest reads; deleted with it."""

TAIL_BYTES: Final[int] = 262_144
"""Bytes at the end of a CSV read to find total and note rows."""

LABEL_CHARS: Final[int] = 40
MAX_WORDS_SHOWN: Final[int] = 10
COPY_CHUNK: Final[int] = 1 << 20


def received_format_for(file_name: str) -> ReceivedFormat:
    """How a received file is read, from its name; an old Excel format is refused with what to do."""
    from engine.stages.ingest import IngestError, file_format_for

    suffix = PurePosixPath(file_name).suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return "xlsx"
    if suffix in LEGACY_EXCEL_SUFFIXES:
        raise IngestError(
            "UPLOAD_UNSUPPORTED_FORMAT",
            f"{suffix} workbooks cannot be read. Open the file in Excel, save it as .xlsx or CSV, and upload it again.",
        )
    try:
        return file_format_for(file_name)
    except IngestError as exc:
        raise IngestError(
            "UPLOAD_UNSUPPORTED_FORMAT", "Only CSV, Excel (.xlsx) and Parquet files can be uploaded."
        ) from exc


def companion_keys(storage: Storage, read_key: str) -> tuple[str, ...]:
    """The keys intake left beside `read_key` that exist: the file as received and `intake.json`."""
    folder = str(PurePosixPath(read_key).parent)
    keys = (f"{folder}/{name}" for name in COMPANION_FILENAMES)
    return tuple(key for key in keys if key != read_key and storage.exists(key))


@dataclass(frozen=True)
class IntakeResult:
    """Where ingest reads the file from now, in which format, and what intake did."""

    source_key: str
    file_format: Literal["csv", "parquet"]
    report: IntakeReport

    @property
    def worth_keeping(self) -> bool:
        """True when the report says something: the file was rewritten, or intake was turned off.

        Only then is `intake.json` written, so a file intake left alone is stored exactly as before.
        """
        return self.report.applied or self.report.turned_off


@dataclass
class _Plan:
    """Everything the rewrite needs, decided before a byte is written."""

    decision: HeaderDecision
    kept: list[int]
    names: list[str]
    renamed: list[RenamedColumn]
    typed: list[int]
    columns_dropped: int
    needs_rewrite: bool
    words: frozenset[str] = field(default_factory=frozenset)
    footer_rows: int = 0
    footer_width: int = 1
    """The width the footer rule judges a one-cell note by: the header's filled cells, in planning and writing alike."""
    drop_empty: bool = True


@dataclass
class _Counts:
    rows_written: int = 0
    empty_rows: int = 0
    cells_left_out: int = 0
    footer_labels: list[str] = field(default_factory=list)
    footer_rows: int = 0
    emptied: dict[int, int] = field(default_factory=dict)
    words: dict[int, set[str]] = field(default_factory=dict)


def intake_file(
    storage: Storage,
    *,
    received_key: str,
    received_format: ReceivedFormat,
    csv_key: str,
    original_key: str,
    config: IntakeConfig,
    sheet: str | None = None,
    enabled: bool = True,
) -> IntakeResult:
    """Run the intake rules on the file at `received_key` and say where ingest should read it.

    `csv_key` is where a rewritten file goes (for a CSV it is `received_key` itself); `original_key`
    is where the file as received is kept when it is rewritten. Raises `engine.stages.ingest.IngestError`
    for a workbook that cannot be read or a sheet that does not exist, as ingest does for a bad CSV.
    """
    if received_format == "parquet":
        report = IntakeReport(source_format="parquet", applied=False, turned_off=not enabled)
        return IntakeResult(received_key, "parquet", _with_notes(report))
    if received_format == "xlsx":
        return _intake_workbook(
            storage,
            received_key=received_key,
            csv_key=csv_key,
            config=config,
            sheet=sheet,
            enabled=enabled,
        )
    if not enabled:
        report = IntakeReport(source_format="csv", applied=False, turned_off=True)
        return IntakeResult(received_key, "csv", _with_notes(report))
    return _intake_csv(storage, received_key=received_key, original_key=original_key, config=config)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _intake_csv(
    storage: Storage, *, received_key: str, original_key: str, config: IntakeConfig
) -> IntakeResult:
    from engine.stages.ingest import SNIFF_BYTES, detect_encoding

    if storage.size_bytes(received_key) == 0:
        return IntakeResult(
            received_key, "csv", _with_notes(IntakeReport(source_format="csv", applied=False))
        )
    with storage.open_read(received_key) as handle:
        head = handle.read(SNIFF_BYTES)
    encodings = detect_encoding(head)
    try:
        encoding, delimiter, records, complete = _csv_head(storage, received_key, head, encodings, config)
        tail = records if complete else _csv_tail(storage, received_key, encoding, delimiter)
    except (csv.Error, UnicodeError, ValueError) as exc:
        # A file the csv module cannot even sample is left to ingest, which says what is wrong with it.
        _LOG.info("intake.csv_unsampled reason=%s", type(exc).__name__)
        return IntakeResult(
            received_key, "csv", _with_notes(IntakeReport(source_format="csv", applied=False))
        )
    plan = _plan(records, tail, complete, config)
    if not plan.needs_rewrite:
        report = IntakeReport(source_format="csv", applied=False, delimiter=delimiter, encoding=encoding)
        return IntakeResult(received_key, "csv", _with_notes(report))

    _copy(storage, received_key, original_key)
    counts = _rewrite_csv(storage, original_key, received_key, encodings, encoding, delimiter, plan, config)
    report = _report(
        plan,
        counts,
        source_format="csv",
        original_key=original_key,
        delimiter=delimiter,
        encoding=encoding,
    )
    _LOG.info(
        "intake.csv_rewritten rows=%d rows_above=%d footer=%d empty=%d renamed=%d missing_columns=%d",
        counts.rows_written,
        plan.decision.rows_above,
        counts.footer_rows,
        counts.empty_rows,
        len(plan.renamed),
        len(report.missing_words),
    )
    return IntakeResult(received_key, "csv", report)


def _reader(text: io.TextIOBase | Iterator[str], delimiter: str) -> Iterator[list[str]]:
    return csv.reader(text, delimiter=delimiter, quotechar='"', doublequote=True, skipinitialspace=True)


def _csv_head(
    storage: Storage, key: str, head: bytes, encodings: Sequence[str], config: IntakeConfig
) -> tuple[str, str, list[list[str]], bool]:
    """The encoding, the delimiter, the first records and whether they are the whole file."""
    from engine.stages.ingest import CANDIDATE_DELIMITERS, sniff_delimiter

    encoding = encodings[0]
    text = head.decode(encoding, errors="replace").lstrip("﻿")
    cut = text.rfind("\n")
    head_text = text if cut < 0 else text[: cut + 1]
    sniffed = sniff_delimiter(head_text)
    delimiter = _choose_delimiter(head_text, sniffed, CANDIDATE_DELIMITERS, config)
    limit = config.max_header_search_rows + config.sample_rows + 2
    records: list[list[str]] = []
    complete = True
    with storage.open_read(key) as raw:
        stream = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline="")
        for record in _reader(stream, delimiter):
            if len(records) >= limit:
                complete = False
                break
            records.append(record)
    if records and records[0]:
        records[0][0] = records[0][0].lstrip("﻿")
    return encoding, delimiter, records, complete


def _choose_delimiter(head_text: str, sniffed: str, candidates: str, config: IntakeConfig) -> str:
    """The sniffed delimiter, unless title rows fooled the sniffer and another one finds a wider header."""
    records = list(_reader(io.StringIO(head_text), sniffed))
    decision = find_header(records, config)
    if not decision.moved and records and len(nonempty(records[0])) >= 2:
        return sniffed
    best, best_width = sniffed, len(nonempty(records[decision.header_index])) if records else 0
    for candidate in candidates:
        if candidate == sniffed:
            continue
        trial = list(_reader(io.StringIO(head_text), candidate))
        if not trial:
            continue
        found = find_header(trial, config)
        width = len(nonempty(trial[found.header_index]))
        if width > best_width and width >= 2:
            best, best_width = candidate, width
    return best


def _csv_tail(storage: Storage, key: str, encoding: str, delimiter: str) -> list[list[str]]:
    """The last records of the file: by seeking when the store allows it, else by one read through."""
    keep = 64
    size = storage.size_bytes(key)
    if not encoding.startswith(("utf-16", "utf-32")):
        try:
            with storage.open_read(key) as raw:
                raw.seek(max(0, size - TAIL_BYTES))
                data = raw.read()
            text = data.decode(encoding, errors="replace")
            if size > TAIL_BYTES:
                text = text[text.find("\n") + 1 :]
            return list(_reader(io.StringIO(text), delimiter))[-keep:]
        except (OSError, AttributeError, io.UnsupportedOperation):
            pass
    last: deque[list[str]] = deque(maxlen=keep)
    with storage.open_read(key) as raw:
        stream = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline="")
        last.extend(_reader(stream, delimiter))
    return list(last)


def _plan(records: list[list[str]], tail: list[list[str]], complete: bool, config: IntakeConfig) -> _Plan:
    decision = find_header(records, config)
    header = records[decision.header_index] if decision.header_index < len(records) else []
    group = records[decision.group_index] if decision.group_index is not None else None
    body = records[decision.data_start :]
    width_guess = max(len(nonempty(header)), 1)
    footer_rows = 0
    if complete:
        footer_span, footer = find_footer(body, width_guess, config)
        footer_rows = len(footer)
        if footer_span:
            body = body[: len(body) - footer_span]
    else:
        _span, footer = find_footer(tail, width_guess, config, keep_at_least=0)
        footer_rows = len(footer)
    sample = [record for record in body if not is_empty_record(record)][: config.sample_rows]
    kept, dropped = keep_columns(header, sample)
    names, renamed = name_columns(header, group, kept)
    projected = [[record[index] if index < len(record) else "" for index in kept] for record in sample]
    words = frozenset(config.missing_words)
    typed, seen = missing_word_columns(projected, len(kept), words)
    end = body if complete else tail
    padded_empty = any(record and is_empty_record(record) for record in [*body, *end])
    needs = (
        decision.moved
        or decision.group_index is not None
        or any(change.reason == "repeated" for change in renamed)
        or footer_rows > 0
        or dropped > 0
        or bool(seen)
        or padded_empty
    )
    return _Plan(
        decision=decision,
        kept=kept,
        names=names,
        renamed=renamed,
        typed=typed,
        columns_dropped=dropped,
        needs_rewrite=needs,
        words=words,
        footer_rows=footer_rows,
        footer_width=width_guess,
    )


def _copy(storage: Storage, source: str, target: str) -> None:
    with storage.open_read(source) as reader, storage.open_write(target) as writer:
        while chunk := reader.read(COPY_CHUNK):
            writer.write(chunk)


def _rewrite_csv(
    storage: Storage,
    source: str,
    target: str,
    encodings: Sequence[str],
    first: str,
    delimiter: str,
    plan: _Plan,
    config: IntakeConfig,
) -> _Counts:
    """One streaming pass; a codec that fails part-way is replaced by the next one and the pass restarts."""
    from engine.stages.ingest import IngestError

    order = [first, *(codec for codec in encodings if codec != first)]
    for encoding in order:
        try:
            with storage.open_read(source) as raw, storage.open_write(target) as sink:
                stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="")
                out = io.TextIOWrapper(sink, encoding="utf-8", newline="")
                try:
                    counts = _write(_strip_bom(_reader(stream, delimiter)), plan, out, config)
                finally:
                    out.flush()
                    out.detach()
                return counts
        except UnicodeDecodeError:
            _LOG.info("intake.encoding_retry encoding=%s", encoding)
            continue
        except csv.Error as exc:
            raise IngestError(
                "UPLOAD_UNREADABLE",
                "The file could not be read as a CSV table. Save it again as CSV and upload it.",
            ) from exc
    raise IngestError(
        "UPLOAD_ENCODING_UNSUPPORTED",
        "The file is not text the engine can read. Save it as UTF-8 CSV and upload it again.",
    )


def _strip_bom(records: Iterator[list[str]]) -> Iterator[list[str]]:
    for index, record in enumerate(records):
        if index == 0 and record:
            record[0] = record[0].lstrip("﻿")
        yield record


def _write(records: Iterator[list[str]], plan: _Plan, sink: io.TextIOBase, config: IntakeConfig) -> _Counts:
    """Write the header and every data row the rules keep; hold back the last rows until the footer is known."""
    writer = csv.writer(sink, lineterminator="\n")
    writer.writerow(plan.names)
    counts = _Counts()
    kept = plan.kept
    kept_set = set(kept)
    typed = plan.typed
    held: deque[list[str]] = deque()
    hold = config.max_footer_rows + 1

    def emit(record: list[str]) -> None:
        row = [record[index] if index < len(record) else "" for index in kept]
        counts.cells_left_out += sum(
            1 for index, cell in enumerate(record) if index not in kept_set and cell.strip()
        )
        for column in typed:
            value = row[column].strip().casefold()
            if value and value in plan.words:
                row[column] = ""
                counts.emptied[column] = counts.emptied.get(column, 0) + 1
                counts.words.setdefault(column, set()).add(value)
        writer.writerow(row)
        counts.rows_written += 1

    for index, record in enumerate(records):
        if index < plan.decision.data_start:
            continue
        if is_empty_record(record) and plan.drop_empty:
            if record:
                counts.empty_rows += 1
            continue
        held.append(record)
        if len(held) > hold:
            emit(held.popleft())
    span, labels = find_footer(
        list(held), plan.footer_width, config, keep_at_least=0 if counts.rows_written else 1
    )
    for record in list(held)[: len(held) - span]:
        emit(record)
    counts.footer_rows = len(labels)
    counts.footer_labels = [_label(label) for label in labels]
    return counts


def _label(text: str) -> str:
    masked, _kinds = pii.redact_text(text)
    return masked if len(masked) <= LABEL_CHARS else masked[: LABEL_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# Workbooks
# ---------------------------------------------------------------------------
def _intake_workbook(
    storage: Storage,
    *,
    received_key: str,
    csv_key: str,
    config: IntakeConfig,
    sheet: str | None,
    enabled: bool,
) -> IntakeResult:
    with storage.open_write(csv_key) as sink:
        report = _convert_workbook(
            storage.local_path(received_key),
            sink,
            config,
            sheet=sheet,
            enabled=enabled,
            original_key=received_key,
        )
    return IntakeResult(csv_key, "csv", report)


def workbook_csv(
    data: bytes, config: IntakeConfig, *, sheet: str | None = None
) -> tuple[bytes, IntakeReport]:
    """A workbook held in memory as the CSV intake reads from it, and the report: for a pulled file (DEC-1504)."""
    sink = io.BytesIO()
    report = _convert_workbook(io.BytesIO(data), sink, config, sheet=sheet, enabled=True, original_key=None)
    return sink.getvalue(), report


def _convert_workbook(
    source: Path | BinaryIO,
    sink: BinaryIO,
    config: IntakeConfig,
    *,
    sheet: str | None,
    enabled: bool,
    original_key: str | None,
) -> IntakeReport:
    """Read one sheet of the workbook at `source` through the rules and write it to `sink` as UTF-8 CSV."""
    from engine.stages.ingest import IngestError

    try:
        check_workbook(source, config)
        if not isinstance(source, Path):
            source.seek(0)
        book = open_workbook(source)
    except WorkbookError as exc:
        raise IngestError(exc.code, exc.message) from exc
    try:
        infos, chosen = _choose_sheet(book, sheet, config)
        worksheet = book[chosen]
        limit = config.max_header_search_rows + config.sample_rows + 2
        head = list(sheet_rows(worksheet, max_rows=limit))
        if enabled:
            plan = _plan(head, [], len(head) < limit, config)
        else:
            width = max((len(record) for record in head), default=0)
            header = head[0] if head else []
            plan = _Plan(
                decision=HeaderDecision(),
                kept=list(range(width)),
                names=[header[index] if index < len(header) else "" for index in range(width)],
                renamed=[],
                typed=[],
                columns_dropped=0,
                needs_rewrite=True,
                drop_empty=False,
            )
        plain = config.model_copy(update={"max_footer_rows": 0}) if not enabled else config
        out = io.TextIOWrapper(sink, encoding="utf-8", newline="")
        try:
            counts = _write(sheet_rows(worksheet), plan, out, plain)
        finally:
            out.flush()
            out.detach()
    except WorkbookError as exc:
        raise IngestError(exc.code, exc.message) from exc
    finally:
        book.close()
    report = _report(
        plan,
        counts,
        source_format="xlsx",
        original_key=original_key,
        delimiter=",",
        encoding="utf-8",
        sheet=chosen,
        sheets=tuple(info.model_copy(update={"used": info.name == chosen}) for info in infos),
        turned_off=not enabled,
    )
    _LOG.info(
        "intake.workbook_read sheets=%d rows=%d rows_above=%d footer=%d",
        len(infos),
        counts.rows_written,
        plan.decision.rows_above,
        counts.footer_rows,
    )
    return report


def _choose_sheet(book: Any, wanted: str | None, config: IntakeConfig) -> tuple[list[SheetInfo], str]:
    """Every sheet described, and the one to read: the one named, else the visible table with the most rows."""
    worksheets = list(book.worksheets)
    infos: list[SheetInfo] = []
    for worksheet in worksheets:
        rows = list(sheet_rows(worksheet, max_rows=SHEET_SCAN_ROWS))
        declared = getattr(worksheet, "max_row", None)
        infos.append(
            SheetInfo(
                name=str(worksheet.title),
                rows=int(declared) if isinstance(declared, int) else None,
                looks_like_data=looks_like_data(rows, config),
                hidden=getattr(worksheet, "sheet_state", "visible") != "visible",
                used=False,
            )
        )
    names = [info.name for info in infos]
    if wanted is not None:
        exact = [name for name in names if name == wanted]
        folded = [name for name in names if name.strip().casefold() == wanted.strip().casefold()]
        if exact or folded:
            return infos, (exact or folded)[0]
        listed = ", ".join(f'"{name}"' for name in names)
        raise WorkbookError(
            "UPLOAD_SHEET_NOT_FOUND",
            f'This workbook has no sheet called "{wanted}". Its sheets are {listed}.',
        )
    tables = [(index, info) for index, info in enumerate(infos) if info.looks_like_data and not info.hidden]
    if tables:
        # The biggest by what the sheet holds: the row count a workbook declares can be missing, or "A1".
        sizes = [sheet_bytes(book, worksheet) for worksheet in worksheets]
        _index, info = min(tables, key=lambda pair: (-(sizes[pair[0]] or pair[1].rows or 0), pair[0]))
        return infos, info.name
    for info, worksheet in zip(infos, worksheets, strict=True):
        if not info.hidden and any(nonempty(row) for row in sheet_rows(worksheet, max_rows=SHEET_SCAN_ROWS)):
            return infos, info.name
    raise WorkbookError("UPLOAD_NO_ROWS", "None of this workbook's sheets holds any data.")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def _report(
    plan: _Plan,
    counts: _Counts,
    *,
    source_format: Literal["csv", "xlsx"],
    original_key: str | None,
    delimiter: str,
    encoding: str,
    sheet: str | None = None,
    sheets: tuple[SheetInfo, ...] = (),
    turned_off: bool = False,
) -> IntakeReport:
    missing = tuple(
        MissingWordsColumn(
            column=plan.names[column],
            words=tuple(sorted(counts.words.get(column, set())))[:MAX_WORDS_SHOWN],
            cells=cells,
        )
        for column, cells in sorted(counts.emptied.items())
    )
    report = IntakeReport(
        source_format=source_format,
        applied=True,
        turned_off=turned_off,
        original_key=original_key,
        sheet=sheet,
        sheets=sheets,
        delimiter=delimiter,
        encoding=encoding,
        header_row=plan.decision.header_index + 1,
        header_rows=1 if plan.decision.group_index is None else 2,
        rows_above_header=plan.decision.rows_above,
        footer_rows=counts.footer_rows,
        footer_labels=tuple(counts.footer_labels),
        empty_rows=counts.empty_rows,
        columns_dropped=plan.columns_dropped,
        cells_left_out=counts.cells_left_out,
        renamed=tuple(plan.renamed),
        missing_words=missing,
        rows_written=counts.rows_written,
    )
    return _with_notes(report)


def _with_notes(report: IntakeReport) -> IntakeReport:
    return report.model_copy(update={"notes": notes_for(report)})


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _quoted(items: Sequence[str], limit: int = 3) -> str:
    shown = ", ".join(f'"{item}"' for item in items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def notes_for(report: IntakeReport) -> tuple[str, ...]:
    """What intake did, in plain sentences, from the report's own figures."""
    notes: list[str] = []
    if report.turned_off:
        notes.append("Read as it is: the intake rules were turned off for this file.")
    if report.source_format == "xlsx" and report.sheet is not None:
        notes.append(f'Read the sheet "{report.sheet}" of this Excel workbook.')
        others = [
            info.name for info in report.sheets if not info.used and info.looks_like_data and not info.hidden
        ]
        if others:
            notes.append(
                f"The workbook has {_plural(len(others), 'other sheet')} with a table: {_quoted(others)}. "
                "Choose it to read it instead."
            )
    if report.rows_above_header:
        notes.append(
            f"Skipped {_plural(report.rows_above_header, 'row')} above the column names (a title or notes); "
            + (
                f"the column names are on row {report.header_row}."
                if report.header_rows == 1
                else f"the column names are on rows {report.header_row - 1} and {report.header_row}."
            )
        )
    joined = [change.name for change in report.renamed if change.reason == "joined"]
    if joined:
        notes.append(f"Joined two header rows into one name, for example {_quoted(joined, 2)}.")
    if report.footer_rows:
        notes.append(
            f"Left out {_plural(report.footer_rows, 'row')} at the end that looked like a total or a note: "
            f"{_quoted(list(report.footer_labels))}."
        )
    if report.empty_rows:
        notes.append(f"Left out {_plural(report.empty_rows, 'empty row')}.")
    if report.columns_dropped:
        notes.append(f"Left out {_plural(report.columns_dropped, 'column')} with no name and no values.")
    if report.cells_left_out:
        notes.append(
            f"{_plural(report.cells_left_out, 'filled cell')} sat outside the table (in a column with no name, "
            "or to the right of the last column) and were left out."
        )
    repeated = [change.name for change in report.renamed if change.reason == "repeated"]
    if repeated:
        notes.append(
            f"Gave {_plural(len(repeated), 'repeated column name')} a number to tell them apart: {_quoted(repeated)}."
        )
    if report.missing_words:
        words = sorted({word for column in report.missing_words for word in column.words})
        cells = sum(column.cells for column in report.missing_words)
        columns = [column.column for column in report.missing_words]
        notes.append(
            f'Read words that mean "no value" ({_quoted(words, 4)}) as empty in '
            f"{_plural(len(columns), 'column')} of numbers, dates or yes/no: {_quoted(columns)} "
            f"({_plural(cells, 'cell')})."
        )
    return tuple(notes)
