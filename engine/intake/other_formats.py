"""JSON, JSON Lines and a single-table `.gz` or `.zip`, read as the CSV ingest reads (Plan O M240, DEC-1810).

Decision O15: these formats are read; `.xls`, `.xlsb` and password-protected workbooks stay refused until a
dependency review (`engine.intake.excel.LEGACY_EXCEL_SUFFIXES`, untouched). The entry point is
:func:`read_other_format`. It turns the stored file into one UTF-8 CSV at `csv_key`, and says what it did in an
`IntakeReport`, so everything after it (ingest, profile, recipe) reads it as it reads any other CSV:

* **JSON** is a list of records, or one object holding exactly one list of records
  (`{"customers": [...]}`), or an object of equal-length columns. **JSON Lines** (`.jsonl`, `.ndjson`) is one
  record per line. A nested record is flattened into columns named `address.city`; a list is kept as JSON text.
  Strings stay strings, so `"0000123"` is still `0000123`. A record that cannot be read is never dropped: the
  file is refused and the first lines are named.
* **`.gz`** holds one file (`customers.csv.gz`, `orders.json.gz`); what it holds is told by the name inside, and
  when the name says nothing, by the first characters.
* **`.zip`** must hold exactly one table. A zip that holds two tables is refused, with their names, because
  choosing one would be a guess; so is a zip with a password, or one that unpacks to more than the limit.

Nothing is unpacked to disk by name (no path from inside an archive is ever used), and every stream is counted
as it is read, so an archive that lies about its size cannot fill the disk. A `.tar.gz` is refused.
"""

from __future__ import annotations

import codecs
import csv
import gzip
import io
import json
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, Final, Literal

from engine.intake.contracts import IntakeReport
from engine.intake.run import notes_for
from engine.stages.ingest import IngestError
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "DEFAULT_MAX_UNCOMPRESSED_BYTES",
    "OTHER_FORMAT_SUFFIXES",
    "OtherFormatResult",
    "is_other_format",
    "read_other_format",
]

_LOG = get_logger(__name__)

JSON_SUFFIXES: Final[frozenset[str]] = frozenset({".json"})
JSONL_SUFFIXES: Final[frozenset[str]] = frozenset({".jsonl", ".ndjson"})
CSV_LIKE_SUFFIXES: Final[frozenset[str]] = frozenset({".csv", ".tsv", ".txt"})
OTHER_FORMAT_SUFFIXES: Final[frozenset[str]] = frozenset({*JSON_SUFFIXES, *JSONL_SUFFIXES, ".gz", ".zip"})
"""The suffixes this module reads (Plan O O15). `.xls`, `.xlsb` and `.tar.gz` are not among them."""

_TABLE_SUFFIXES: Final[frozenset[str]] = frozenset({*JSON_SUFFIXES, *JSONL_SUFFIXES, *CSV_LIKE_SUFFIXES})
_UNREADABLE_TABLE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".xlsx", ".xlsm", ".xls", ".xlsb", ".parquet", ".pq", ".zip", ".gz", ".tar", ".tgz", ".7z", ".rar"}
)
"""Files inside a zip that hold data this reader cannot open from there: they count as the zip's table."""

DEFAULT_MAX_UNCOMPRESSED_BYTES: Final[int] = 2 * 1024**3
"""The most any container may unpack to (2 GB, the upload size limit's default); a stream is stopped at it."""
MAX_JSON_BYTES: Final[int] = 256 * 1024**2
"""A JSON document is held whole while it is parsed: larger ones are refused with what to do (JSON Lines)."""
MAX_COLUMNS: Final[int] = 1_000
MAX_FLATTEN_DEPTH: Final[int] = 5
MAX_LINES_NAMED: Final[int] = 5
SNIFF_BYTES: Final[int] = 4_096
COPY_CHUNK: Final[int] = 1 << 20
_UNIT_MB: Final[int] = 1024 * 1024

InnerFormat = Literal["csv", "json", "jsonl"]


@dataclass(frozen=True)
class OtherFormatResult:
    """The CSV that was written, what it was made from, and what was done on the way."""

    csv_key: str
    inner_format: InnerFormat
    container: Literal["none", "gz", "zip"]
    inner_name: str | None
    """The file inside a `.gz` or `.zip`, as the archive names it; never used as a path."""
    rows: int | None
    """Records written; `None` when the CSV was copied out of an archive without being parsed."""
    columns: tuple[str, ...]
    """The columns of a converted JSON file; empty for a CSV copied as it was."""
    report: IntakeReport


def is_other_format(file_name: str) -> bool:
    """Whether `file_name` ends in a suffix this module reads."""
    return PurePosixPath(file_name.lower()).suffix in OTHER_FORMAT_SUFFIXES


def _refuse(code: str, message: str) -> IngestError:
    return IngestError(code, message)


# ---------------------------------------------------------------------------
# Bounded streams
# ---------------------------------------------------------------------------
class _CappedReader(io.RawIOBase):
    """A read-only wrapper that fails once more than `limit` bytes have been read through it."""

    def __init__(self, inner: Any, limit: int, what: str) -> None:
        super().__init__()
        self._inner = inner
        self._left = limit
        self._limit = limit
        self._what = what

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self._inner.read(len(buffer))
        if len(data) > self._left:
            raise _refuse(
                "UPLOAD_ARCHIVE_TOO_LARGE",
                f"{self._what} unpacks to more than {self._limit // _UNIT_MB:,} MB. "
                "Save the table you need as CSV and upload that instead.",
            )
        self._left -= len(data)
        buffer[: len(data)] = data
        return len(data)


def _copy_stream(source: Any, sink: BinaryIO) -> int:
    total = 0
    while chunk := source.read(COPY_CHUNK):
        sink.write(chunk)
        total += len(chunk)
    return total


# ---------------------------------------------------------------------------
# JSON to rows
# ---------------------------------------------------------------------------
def _cell(value: Any) -> str:
    """A JSON value as the text a CSV cell holds: strings as they are, `null` as nothing, `true` as `true`."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _flatten(record: dict[str, Any], prefix: str = "", depth: int = 0) -> dict[str, str]:
    """One record as `{column: text}`; a nested object becomes `outer.inner`, deeper than five levels stays JSON."""
    out: dict[str, str] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value and depth < MAX_FLATTEN_DEPTH:
            inner = _flatten(value, f"{name}.", depth + 1)
            for column, cell in inner.items():
                _put(out, column, cell)
        else:
            _put(out, name, _cell(value))
    return out


def _put(out: dict[str, str], column: str, cell: str) -> None:
    """Add one cell; two fields that flatten to one column name are refused, never silently merged."""
    if column in out:
        raise _refuse(
            "UPLOAD_JSON_NOT_A_TABLE",
            f'Two fields in this file become the same column, "{column}" (for example a field "a.b" and a '
            'field "b" inside "a"). One of them would be lost. Rename one of them and upload the file again.',
        )
    out[column] = cell


def _json_loads(text: str) -> Any:
    """`json.loads` that keeps a number as it was written (`1.50` stays `1.50`) and reads `NaN` as empty."""
    return json.loads(text, parse_float=str, parse_constant=lambda _name: None)


def _records_of(document: Any) -> list[dict[str, Any]]:
    """The records of a parsed JSON document, or a refusal that says which shapes are read."""
    not_a_table = (
        "This JSON file is not a table. Use a list of records ([{...}, {...}]), one object that holds a "
        "list of records, or an object of equal-length columns."
    )
    if isinstance(document, list):
        if document and all(isinstance(item, dict) for item in document):
            return document
        raise _refuse("UPLOAD_JSON_NOT_A_TABLE", not_a_table)
    if not isinstance(document, dict) or not document:
        raise _refuse("UPLOAD_JSON_NOT_A_TABLE", not_a_table)
    lists = {
        key: value
        for key, value in document.items()
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value)
    }
    if len(lists) > 1:
        names = ", ".join(f'"{key}"' for key in list(lists)[:5])
        raise _refuse(
            "UPLOAD_JSON_NOT_A_TABLE",
            f"This JSON file holds {len(lists)} lists of records ({names}). Upload one table at a time.",
        )
    if len(lists) == 1:
        return next(iter(lists.values()))
    columns = [value for value in document.values() if isinstance(value, list)]
    if columns and len(columns) == len(document):
        if len({len(column) for column in columns}) != 1:
            raise _refuse(
                "UPLOAD_JSON_NOT_A_TABLE",
                "This JSON file is an object of lists of different lengths, so it is not a table.",
            )
        if any(isinstance(item, dict | list) for column in columns for item in column):
            raise _refuse("UPLOAD_JSON_NOT_A_TABLE", not_a_table)
        field_names = list(document)
        return [dict(zip(field_names, row, strict=True)) for row in zip(*columns, strict=True)]
    if all(not isinstance(value, list) for value in document.values()):
        return [document]  # one record
    raise _refuse("UPLOAD_JSON_NOT_A_TABLE", not_a_table)


def _write_rows(rows: Iterator[dict[str, str]], columns: list[str], sink: BinaryIO) -> int:
    out = io.TextIOWrapper(sink, encoding="utf-8", newline="")
    count = 0
    try:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(column, "") for column in columns])
            count += 1
    finally:
        out.flush()
        out.detach()
    return count


def _column_order(rows: list[dict[str, str]]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            if key not in seen:
                seen[key] = None
                if len(seen) > MAX_COLUMNS:
                    raise _refuse(
                        "UPLOAD_JSON_NOT_A_TABLE",
                        f"The records in this file have more than {MAX_COLUMNS:,} different fields, "
                        "so it is not a table.",
                    )
    return list(seen)


def _decode_text(data: bytes) -> str:
    """JSON text from bytes: UTF-8 with or without a mark, or UTF-16/32 when the mark says so."""
    try:
        encoding = json.detect_encoding(data)
        return data.decode(encoding).lstrip("﻿")
    except (UnicodeDecodeError, LookupError) as exc:
        raise _refuse(
            "UPLOAD_ENCODING_UNSUPPORTED",
            "This JSON file is not text the engine can read. JSON is UTF-8; save it that way and upload it again.",
        ) from exc


def _read_all(stream: Any, limit: int, what: str) -> bytes:
    capped = io.BufferedReader(_CappedReader(stream, min(limit, MAX_JSON_BYTES + 1), what))
    try:
        data = capped.read()
    except IngestError as exc:
        if exc.code == "UPLOAD_ARCHIVE_TOO_LARGE":
            raise _refuse(
                "UPLOAD_JSON_TOO_LARGE",
                f"{what} is larger than {MAX_JSON_BYTES // _UNIT_MB:,} MB. Save it as JSON Lines (one record "
                "per line) or as CSV and upload that instead.",
            ) from exc
        raise
    if len(data) > MAX_JSON_BYTES:
        raise _refuse(
            "UPLOAD_JSON_TOO_LARGE",
            f"{what} is larger than {MAX_JSON_BYTES // _UNIT_MB:,} MB. Save it as JSON Lines (one record "
            "per line) or as CSV and upload that instead.",
        )
    return data


def _convert_json(stream: Any, sink: BinaryIO, limit: int, what: str) -> tuple[int, list[str]]:
    text = _decode_text(_read_all(stream, limit, what))
    try:
        document = _json_loads(text)
    except (ValueError, RecursionError) as exc:
        position = f" Line {exc.lineno}." if isinstance(exc, json.JSONDecodeError) else ""
        raise _refuse(
            "UPLOAD_UNREADABLE", f"The file could not be read as JSON. Check that it is valid JSON.{position}"
        ) from exc
    rows = [_flatten(record) for record in _records_of(document)]
    if not rows:
        raise _refuse("UPLOAD_NO_ROWS", "The file holds no records.")
    columns = _column_order(rows)
    return _write_rows(iter(rows), columns, sink), columns


def _jsonl_lines(stream: Any) -> Iterator[tuple[int, str]]:
    """`(line number, text)` of each non-blank line of a UTF-8 stream, decoded incrementally."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    pending = ""
    number = 0
    try:
        while chunk := stream.read(COPY_CHUNK):
            pending += decoder.decode(chunk)
            *lines, pending = pending.split("\n")
            for line in lines:
                number += 1
                if line.strip():
                    yield number, line
        pending += decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise _refuse(
            "UPLOAD_ENCODING_UNSUPPORTED",
            "This JSON Lines file is not UTF-8 text. Save it as UTF-8 and upload it again.",
        ) from exc
    if pending.strip():
        yield number + 1, pending


def _jsonl_rows(stream: Any) -> Iterator[dict[str, str]]:
    bad: list[int] = []
    count = 0
    for number, line in _jsonl_lines(stream):
        try:
            value = _json_loads(line)
        except (ValueError, RecursionError):
            value = None
        if not isinstance(value, dict):
            count += 1
            if len(bad) < MAX_LINES_NAMED:
                bad.append(number)
            continue
        if not bad:
            yield _flatten(value)
    if bad:
        more = f" and {count - len(bad)} more" if count > len(bad) else ""
        listed = ", ".join(str(number) for number in bad)
        noun = "Line" if count == 1 else "Lines"
        verb = "is" if count == 1 else "are"
        raise _refuse(
            "UPLOAD_UNREADABLE",
            f"{noun} {listed}{more} of this JSON Lines file {verb} not a JSON record, so the file was not "
            "read. Each line must be one {...} record.",
        )


def _convert_jsonl(open_stream: Any, sink: BinaryIO) -> tuple[int, list[str]]:
    """Two streaming passes: the first finds the columns (and refuses a bad line), the second writes."""
    seen: dict[str, None] = {}
    with open_stream() as first:
        for row in _jsonl_rows(first):
            for key in row:
                if key not in seen:
                    seen[key] = None
                    if len(seen) > MAX_COLUMNS:
                        raise _refuse(
                            "UPLOAD_JSON_NOT_A_TABLE",
                            f"The records in this file have more than {MAX_COLUMNS:,} different fields, "
                            "so it is not a table.",
                        )
    if not seen:
        raise _refuse("UPLOAD_NO_ROWS", "The file holds no records.")
    columns = list(seen)
    with open_stream() as second:
        return _write_rows(_jsonl_rows(second), columns, sink), columns


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------
def _inner_format_of(name: str) -> InnerFormat | None:
    suffix = PurePosixPath(name.lower()).suffix
    if suffix in JSON_SUFFIXES:
        return "json"
    if suffix in JSONL_SUFFIXES:
        return "jsonl"
    if suffix in CSV_LIKE_SUFFIXES:
        return "csv"
    return None


def _sniff_inner_format(head: bytes) -> InnerFormat:
    """What an unnamed `.gz` holds, from its first characters: `[` is JSON, a `{...}` line then more is JSON Lines."""
    text = head.decode("utf-8-sig", errors="ignore").lstrip()
    if text.startswith("["):
        return "json"
    if text.startswith("{"):
        first, _sep, rest = text.partition("\n")
        try:
            _json_loads(first)
        except ValueError:
            return "json"
        return "jsonl" if rest.strip() else "json"
    return "csv"


def _gz_inner_name(file_name: str) -> str:
    base = PurePosixPath(file_name).name
    return base[: -len(".gz")] if base.lower().endswith(".gz") else base


def _is_tar(head: bytes) -> bool:
    return head[257:262] == b"ustar"


def _usable_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The files of a zip that could be a table: no folders, no `__MACOSX/` copies, no hidden or lock files."""
    members: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        name = info.filename
        base = PurePosixPath(name).name
        if info.is_dir() or name.startswith("__MACOSX/") or base.startswith((".", "~$")):
            continue
        members.append(info)
    return members


def _table_members(members: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    """The members that are tables. A `.txt` file is one only when nothing else in the zip is: next to a
    `customers.csv`, a `README.txt` is a note, not a second table."""

    def suffix(info: zipfile.ZipInfo) -> str:
        return PurePosixPath(info.filename.lower()).suffix

    tables = [
        info for info in members if suffix(info) in (_TABLE_SUFFIXES - {".txt"}) | _UNREADABLE_TABLE_SUFFIXES
    ]
    return tables or [info for info in members if suffix(info) == ".txt"]


def _quoted_names(infos: list[zipfile.ZipInfo], limit: int = 4) -> str:
    names = [PurePosixPath(info.filename).name for info in infos]
    shown = ", ".join(f'"{name}"' for name in names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def read_other_format(
    storage: Storage,
    *,
    received_key: str,
    csv_key: str,
    file_name: str,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> OtherFormatResult:
    """Read the JSON, JSON Lines, `.gz` or `.zip` file at `received_key` and write it as CSV to `csv_key`.

    The file as received stays where it is (`IntakeReport.original_key` names it). Raises
    `engine.stages.ingest.IngestError` with a plain message for a file that is not a table, a zip with more
    than one table, a protected or oversized archive, a `.tar.gz`, or a JSON Lines file with a bad line.
    `.xls`, `.xlsb` and every other suffix are refused as they always were.
    """
    suffix = PurePosixPath(file_name.lower()).suffix
    if suffix not in OTHER_FORMAT_SUFFIXES:
        raise _refuse(
            "UPLOAD_UNSUPPORTED_FORMAT",
            "Only CSV, Excel (.xlsx), Parquet, JSON, JSON Lines, and a .gz or .zip holding one table can be uploaded.",
        )
    try:
        if suffix == ".zip":
            result = _read_zip(storage, received_key, csv_key, max_uncompressed_bytes)
        elif suffix == ".gz":
            result = _read_gz(storage, received_key, csv_key, file_name, max_uncompressed_bytes)
        else:
            result = _read_plain(storage, received_key, csv_key, suffix, max_uncompressed_bytes)
    except (OSError, zlib.error, EOFError, zipfile.BadZipFile) as exc:
        if storage.exists(csv_key):
            storage.delete(csv_key)
        if isinstance(exc, OSError) and not isinstance(exc, gzip.BadGzipFile):
            raise
        raise _refuse(
            "UPLOAD_UNREADABLE",
            "The file could not be unpacked. Check that it is a complete, valid .gz or .zip file.",
        ) from exc
    except IngestError:
        if storage.exists(csv_key):
            storage.delete(csv_key)
        raise
    _LOG.info(
        "intake.other_format_read container=%s inner=%s rows=%s",
        result.container,
        result.inner_format,
        result.rows,
    )
    return result


def _report(
    received_key: str,
    *,
    container: Literal["none", "gz", "zip"],
    inner_format: InnerFormat,
    inner_name: str | None,
    rows: int | None,
    columns: tuple[str, ...],
) -> IntakeReport:
    base = IntakeReport(
        source_format=inner_format,
        container=container,
        applied=True,
        original_key=received_key,
        delimiter="," if inner_format != "csv" else None,
        encoding="utf-8" if inner_format != "csv" else None,
        rows_written=rows,
    )
    notes: list[str] = []
    if container != "none":
        label = {"gz": "gzip file", "zip": "zip file"}[container]
        named = f' ("{inner_name}")' if inner_name else ""
        notes.append(f"Read the file inside the {label}{named} as a table.")
    if inner_format in {"json", "jsonl"}:
        kind = "JSON Lines" if inner_format == "jsonl" else "JSON"
        notes.append(f"Read the {kind} records as a table: {rows:,} rows and {len(columns):,} columns.")
        if any("." in column for column in columns):
            notes.append(
                "Nested fields were flattened into columns named like address.city; lists were kept as text."
            )
    return base.model_copy(update={"notes": (*notes, *notes_for(base))})


def _read_plain(
    storage: Storage, received_key: str, csv_key: str, suffix: str, limit: int
) -> OtherFormatResult:
    inner: InnerFormat = "jsonl" if suffix in JSONL_SUFFIXES else "json"
    with storage.open_write(csv_key) as sink:
        if inner == "jsonl":
            rows, columns = _convert_jsonl(lambda: storage.open_read(received_key), sink)
        else:
            with storage.open_read(received_key) as stream:
                rows, columns = _convert_json(stream, sink, limit, "This file")
    return OtherFormatResult(
        csv_key=csv_key,
        inner_format=inner,
        container="none",
        inner_name=None,
        rows=rows,
        columns=tuple(columns),
        report=_report(
            received_key,
            container="none",
            inner_format=inner,
            inner_name=None,
            rows=rows,
            columns=tuple(columns),
        ),
    )


def _read_gz(
    storage: Storage, received_key: str, csv_key: str, file_name: str, limit: int
) -> OtherFormatResult:
    inner_name = _gz_inner_name(file_name)
    lowered = inner_name.lower()
    if lowered.endswith(".tar") or file_name.lower().endswith(".tgz"):
        raise _refuse(
            "UPLOAD_ARCHIVE_NOT_ONE_TABLE",
            "This is a .tar archive, which can hold many files. Unpack it and upload the table you need.",
        )

    def opened() -> Any:
        return _GzStream(storage, received_key, limit, "This gzip file")

    with opened() as probe:
        head = probe.read(SNIFF_BYTES)
    if _is_tar(head):
        raise _refuse(
            "UPLOAD_ARCHIVE_NOT_ONE_TABLE",
            "This is a .tar archive, which can hold many files. Unpack it and upload the table you need.",
        )
    inner = _inner_format_of(inner_name) or _sniff_inner_format(head)
    rows: int | None = None
    columns: list[str] = []
    with storage.open_write(csv_key) as sink:
        if inner == "csv":
            with opened() as stream:
                _copy_stream(stream, sink)
        elif inner == "jsonl":
            rows, columns = _convert_jsonl(opened, sink)
        else:
            with opened() as stream:
                rows, columns = _convert_json(stream, sink, limit, "This gzip file")
    return OtherFormatResult(
        csv_key=csv_key,
        inner_format=inner,
        container="gz",
        inner_name=inner_name,
        rows=rows,
        columns=tuple(columns),
        report=_report(
            received_key,
            container="gz",
            inner_format=inner,
            inner_name=inner_name,
            rows=rows,
            columns=tuple(columns),
        ),
    )


class _GzStream:
    """A context manager over the decompressed bytes of a stored `.gz` file, counted against a limit."""

    def __init__(self, storage: Storage, key: str, limit: int, what: str) -> None:
        self._storage = storage
        self._key = key
        self._limit = limit
        self._what = what
        self._source: Any = None
        self._stream: Any = None

    def __enter__(self) -> Any:
        self._source = self._storage.open_read(self._key)
        raw = self._source.__enter__()
        self._stream = io.BufferedReader(_CappedReader(gzip.GzipFile(fileobj=raw), self._limit, self._what))
        return self._stream

    def __exit__(self, *exc: object) -> None:
        if self._stream is not None:
            self._stream.close()
        if self._source is not None:
            self._source.__exit__(None, None, None)


def _read_zip(storage: Storage, received_key: str, csv_key: str, limit: int) -> OtherFormatResult:
    path = storage.local_path(received_key)
    if not zipfile.is_zipfile(path):
        raise _refuse(
            "UPLOAD_UNREADABLE",
            "This is not a readable .zip file. Check that it is complete, or unzip it and upload the table.",
        )
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise _refuse("UPLOAD_UNREADABLE", "This .zip file is damaged and cannot be opened.") from exc
    with archive:
        members = _usable_members(archive)
        if any(info.flag_bits & 0x1 for info in members):
            raise _refuse(
                "UPLOAD_ARCHIVE_PROTECTED",
                "This .zip file has a password. Remove the password, or unzip it and upload the table.",
            )
        if sum(info.file_size for info in members) > limit:
            raise _refuse(
                "UPLOAD_ARCHIVE_TOO_LARGE",
                f"This .zip file unpacks to more than {limit // _UNIT_MB:,} MB. "
                "Save the table you need as CSV and upload that instead.",
            )
        tables = _table_members(members)
        if not tables:
            raise _refuse(
                "UPLOAD_ARCHIVE_NOT_ONE_TABLE",
                "This .zip file holds no table the engine can read (CSV, JSON or JSON Lines)."
                + (f" It holds {_quoted_names(members)}." if members else " It is empty."),
            )
        if len(tables) > 1:
            raise _refuse(
                "UPLOAD_ARCHIVE_NOT_ONE_TABLE",
                f"This .zip file holds {len(tables)} tables ({_quoted_names(tables)}). "
                "Upload them one at a time, or unzip it and upload each file.",
            )
        member = tables[0]
        inner = _inner_format_of(member.filename)
        if inner is None:
            suffix = PurePosixPath(member.filename.lower()).suffix
            raise _refuse(
                "UPLOAD_ARCHIVE_NOT_ONE_TABLE",
                f'The file inside this .zip, "{PurePosixPath(member.filename).name}", is a {suffix} file, '
                "which cannot be read from inside an archive. Unzip it and upload that file.",
            )
        inner_name = PurePosixPath(member.filename).name
        rows: int | None = None
        columns: list[str] = []

        def opened() -> Any:
            return _MemberStream(archive, member, limit)

        with storage.open_write(csv_key) as sink:
            if inner == "csv":
                with opened() as stream:
                    _copy_stream(stream, sink)
            elif inner == "jsonl":
                rows, columns = _convert_jsonl(opened, sink)
            else:
                with opened() as stream:
                    rows, columns = _convert_json(stream, sink, limit, "The file inside this .zip")
    return OtherFormatResult(
        csv_key=csv_key,
        inner_format=inner,
        container="zip",
        inner_name=inner_name,
        rows=rows,
        columns=tuple(columns),
        report=_report(
            received_key,
            container="zip",
            inner_format=inner,
            inner_name=inner_name,
            rows=rows,
            columns=tuple(columns),
        ),
    )


class _MemberStream:
    """A context manager over one zip member's bytes, counted against a limit whatever the zip declares."""

    def __init__(self, archive: zipfile.ZipFile, member: zipfile.ZipInfo, limit: int) -> None:
        self._archive = archive
        self._member = member
        self._limit = limit
        self._raw: Any = None
        self._stream: Any = None

    def __enter__(self) -> Any:
        self._raw = self._archive.open(self._member)
        self._stream = io.BufferedReader(_CappedReader(self._raw, self._limit, "This .zip file"))
        return self._stream

    def __exit__(self, *exc: object) -> None:
        if self._stream is not None:
            self._stream.close()
        if self._raw is not None:
            self._raw.close()
