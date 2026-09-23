"""Finding a principal's id inside one file, and rewriting that file in its own format without it.

Erasure (and the access export, which reads what erasure would remove) has to work on every kind
of file the product writes, and it has to leave each one readable by the code that wrote it: a
`scores.csv` is still a CSV with the same header and delimiter, a `row_explanations.parquet` still
has its schema, a `profile.json` still validates as a `DatasetProfile`. So each format is handled by
the library that reads it, never by a byte-level substitution that would corrupt a compressed page
or a quoted cell (DEC-741).

**How a match is decided.**

* *Tabular files (CSV, Parquet)* - a row belongs to the principal when one of its **primary-key
  columns** equals the id. The caller passes the key columns, read from the file's own record (the
  run's `run.json`, the dataset's manifest, the artefact's contract); when no record names them,
  any cell equal to the id marks the row, which is the right reading for a client's raw event table
  whose rows are about the customer they name. A cell equal to the id in a *non-key* column of
  someone else's row (a "referred by" column) does not take that row with it: the cell is masked.
* *JSON* - every **string** equal to the id is a hit. An element of a list that carries the id
  directly (a sample row, a preview row, a top-category entry) is the principal's row and is removed
  whole; when the file's key columns are known and the element names one, only that column decides.
  Any other occurrence is masked. JSON *numbers* are never matched outside a key column: every
  artefact that holds a data value stringifies it, and matching numbers would erase counts that
  happen to equal a numeric id.
* *A numeric id* (`5`, `00104`, `2.5`) is matched **only in key columns** (DEC-737). A number is
  also a visit count, a price or the `5` in `2.5`, so the rules above - any cell of a keyless file,
  a non-key cell, a token inside text - would delete and mask other people's data. A file whose key
  is not recorded (an upload no run has read, a client's raw source) cannot be decided for a
  numeric id: its equal cells are counted as `Hits.ambiguous`, and erasure reports the file as
  needing review instead of rewriting it.
* *Text, and text inside a longer cell or string* - an **exact token occurrence**: the id not
  preceded or followed by a letter, digit, underscore or hyphen, so erasing `C-104` never touches
  `C-1042`.
* *Anything else* (a pickled model) is searched byte for byte and reported, never rewritten.

`delete` mode removes the principal's rows; `tombstone` keeps them with the identifying cells
replaced by the tombstone marker and every other text cell of the row emptied (numbers are kept so
row counts and totals still reconcile, DEC-742). In both modes stray occurrences are replaced by the
marker.

`pandas` is not used: the csv module keeps every cell as the text it was, and pyarrow keeps the
Parquet schema and its metadata exactly.
"""

from __future__ import annotations

import codecs
import csv
import importlib
import io
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from engine.privacy.config import ErasureMode
from engine.privacy.consent import principal_key

if TYPE_CHECKING:
    import pyarrow as pa

__all__ = [
    "Extract",
    "FileKind",
    "Hits",
    "Matcher",
    "extract_bytes",
    "file_kind",
    "rewrite_bytes",
    "scan_bytes",
]

TEXT_SUFFIXES: Final[tuple[str, ...]] = (
    ".sql",
    ".txt",
    ".md",
    ".jsonl",
    ".log",
    ".yaml",
    ".yml",
    ".html",
    ".tsv",
)
_DELIMITERS: Final[tuple[str, ...]] = (",", ";", "\t", "|")
_NUMERIC_ID: Final[re.Pattern[str]] = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
"""An id that is also a number, and is therefore matched only in key columns (DEC-737)."""


class FileKind(StrEnum):
    """How a file is read for a principal search."""

    CSV = "csv"
    PARQUET = "parquet"
    JSON = "json"
    TEXT = "text"
    BINARY = "binary"


def file_kind(key: str) -> FileKind:
    """The kind of file a storage key names, by its suffix."""
    lowered = key.lower()
    if lowered.endswith(".csv"):
        return FileKind.CSV
    if lowered.endswith(".parquet"):
        return FileKind.PARQUET
    if lowered.endswith(".json"):
        return FileKind.JSON
    if lowered.endswith(TEXT_SUFFIXES):
        return FileKind.TEXT
    return FileKind.BINARY


@dataclass(frozen=True)
class Matcher:
    """How one principal id is recognised: equality for a value, a token pattern inside text."""

    principal_id: str
    pattern: re.Pattern[str]
    numeric: bool = False

    @classmethod
    def for_id(cls, principal_id: str) -> Matcher:
        """The matcher of a principal id, in its `principal_key` form (`00104` is `104`)."""
        cleaned = principal_key(principal_id.strip())
        return cls(
            cleaned,
            re.compile(rf"(?<![\w-]){re.escape(cleaned)}(?![\w-])"),
            numeric=_NUMERIC_ID.fullmatch(cleaned) is not None,
        )

    def is_text(self, value: object) -> bool:
        """A string equal to the id (surrounding whitespace ignored). Used for JSON."""
        return isinstance(value, str) and principal_key(value) == self.principal_id

    def is_cell(self, value: object) -> bool:
        """A tabular cell equal to the id; a number counts when it prints as the id."""
        if isinstance(value, bool) or value is None:
            return False
        if isinstance(value, int | float | str):
            return principal_key(value) == self.principal_id
        return False

    def count(self, text: str) -> int:
        """Token occurrences of the id inside `text`; always 0 for a numeric id (DEC-737)."""
        if self.numeric or self.principal_id not in text:
            return 0
        return len(self.pattern.findall(text))

    def replace(self, text: str, tombstone: str) -> tuple[str, int]:
        """`text` with every token occurrence replaced by `tombstone`, and how many there were."""
        if self.numeric or self.principal_id not in text:
            return text, 0
        return self.pattern.subn(tombstone, text)

    def masks_mentions(self) -> bool:
        """Whether a cell equal to the id outside a key column is the principal (False for a number)."""
        return not self.numeric

    def maybe_in(self, data: bytes) -> bool:
        """False only when the id certainly does not occur in `data` as text; True means "parse it".

        A byte search is conclusive for an ASCII id that JSON would not escape, in a file that is not
        UTF-16. Anything else is parsed.
        """
        if not self.principal_id.isascii() or '"' in self.principal_id or "\\" in self.principal_id:
            return True
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return True
        return self.principal_id.encode("ascii") in data


@dataclass
class Hits:
    """How much of one file belongs to, or mentions, the principal."""

    rows: int = 0
    cells: int = 0
    occurrences: int = 0
    ambiguous: int = 0
    """Cells equal to a numeric id in a file whose key is unknown: not decidable, never rewritten."""

    @property
    def any(self) -> bool:
        """Whether anything was found that a rewrite would change (`ambiguous` is not)."""
        return bool(self.rows or self.cells or self.occurrences)


@dataclass(frozen=True)
class Extract:
    """What a file holds about a principal, for the access export."""

    kind: FileKind
    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    fragments: tuple[tuple[str, Any], ...] = ()
    lines: tuple[str, ...] = ()
    hits: Hits = field(default_factory=Hits)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def scan_bytes(kind: FileKind, data: bytes, matcher: Matcher, key_columns: tuple[str, ...] | None) -> Hits:
    """Count what `data` holds about the principal, changing nothing."""
    if kind is not FileKind.PARQUET and not matcher.maybe_in(data):
        return Hits()
    if kind is FileKind.CSV:
        return _csv(data, matcher, key_columns, mode=None, tombstone="")[1]
    if kind is FileKind.PARQUET:
        return _parquet(data, matcher, key_columns, mode=None, tombstone="")[1]
    if kind is FileKind.JSON:
        parsed = _json_or_none(data)
        if parsed is not None:
            return _json(parsed[0], matcher, key_columns, mode=None, tombstone="")[1]
    if kind in (FileKind.JSON, FileKind.TEXT):
        return Hits(occurrences=matcher.count(_decode(data)[0]))
    return Hits(occurrences=data.count(matcher.principal_id.encode("utf-8")))


def rewrite_bytes(
    kind: FileKind,
    data: bytes,
    matcher: Matcher,
    key_columns: tuple[str, ...] | None,
    *,
    mode: ErasureMode,
    tombstone: str,
) -> tuple[bytes, Hits]:
    """`data` rewritten in its own format without the principal, and what was removed or masked.

    `FileKind.BINARY` is returned unchanged: the caller reports it as unrewritable.
    """
    if kind is FileKind.CSV:
        return _csv(data, matcher, key_columns, mode=mode, tombstone=tombstone)
    if kind is FileKind.PARQUET:
        return _parquet(data, matcher, key_columns, mode=mode, tombstone=tombstone)
    if kind is FileKind.JSON:
        parsed = _json_or_none(data)
        if parsed is not None:
            document, trailing_newline = parsed
            rewritten, hits = _json(document, matcher, key_columns, mode=mode, tombstone=tombstone)
            text = json.dumps(rewritten, indent=2, ensure_ascii=False) + ("\n" if trailing_newline else "")
            return text.encode("utf-8"), hits
    if kind in (FileKind.JSON, FileKind.TEXT):
        text, encoding, bom = _decode(data)
        replaced, count = matcher.replace(text, tombstone)
        return bom + replaced.encode(encoding), Hits(occurrences=count)
    return data, Hits(occurrences=data.count(matcher.principal_id.encode("utf-8")))


def extract_bytes(
    kind: FileKind, data: bytes, matcher: Matcher, key_columns: tuple[str, ...] | None
) -> Extract:
    """Everything `data` holds about the principal: its rows, its JSON values, its lines of text."""
    hits = scan_bytes(kind, data, matcher, key_columns)
    if not hits.any:
        return Extract(kind=kind)
    if kind is FileKind.CSV:
        header, rows = _csv_rows(data)
        return Extract(
            kind=kind, header=header, rows=_matching_rows(header, rows, matcher, key_columns), hits=hits
        )
    if kind is FileKind.PARQUET:
        header, rows = _parquet_rows(data)
        return Extract(
            kind=kind, header=header, rows=_matching_rows(header, rows, matcher, key_columns), hits=hits
        )
    if kind is FileKind.JSON:
        parsed = _json_or_none(data)
        if parsed is not None:
            fragments: list[tuple[str, Any]] = []
            _json_fragments(parsed[0], matcher, "$", fragments, key_columns)
            return Extract(kind=kind, fragments=tuple(fragments), hits=hits)
    if kind in (FileKind.JSON, FileKind.TEXT):
        text = _decode(data)[0]
        return Extract(
            kind=kind, lines=tuple(line for line in text.splitlines() if matcher.count(line)), hits=hits
        )
    return Extract(kind=kind, hits=hits)


# ---------------------------------------------------------------------------
# Text decoding
# ---------------------------------------------------------------------------
def _decode(data: bytes) -> tuple[str, str, bytes]:
    """`(text, encoding, bom)`: UTF-8 (with or without BOM), UTF-16 by BOM, else Latin-1 (lossless)."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8"), "utf-8", codecs.BOM_UTF8
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16-le" if data.startswith(codecs.BOM_UTF16_LE) else "utf-16-be"
        return data[2:].decode(encoding), encoding, data[:2]
    try:
        return data.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1", b""


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _csv_dialect(text: str) -> tuple[str, str]:
    """`(delimiter, line terminator)` of a CSV text, from its first line."""
    first = text.split("\n", 1)[0]
    delimiter = max(_DELIMITERS, key=first.count) if any(d in first for d in _DELIMITERS) else ","
    return delimiter, "\r\n" if "\r\n" in text[:65536] else "\n"


def _csv_rows(data: bytes) -> tuple[tuple[str, ...], list[list[str]]]:
    text = _decode(data)[0]
    delimiter, _ = _csv_dialect(text)
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    if not rows:
        return (), []
    return tuple(rows[0]), rows[1:]


def _csv(
    data: bytes,
    matcher: Matcher,
    key_columns: tuple[str, ...] | None,
    *,
    mode: ErasureMode | None,
    tombstone: str,
) -> tuple[bytes, Hits]:
    text, encoding, bom = _decode(data)
    delimiter, terminator = _csv_dialect(text)
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    hits = Hits()
    if not rows:
        return data, hits
    header, body = rows[0], rows[1:]
    owners = _owner_indexes(header, matcher, key_columns)
    kept: list[list[str]] = [_mask_cells(header, matcher, tombstone, hits, count_cells=False)]
    for row in body:
        indexes = range(len(row)) if owners is None else owners
        if any(index < len(row) and matcher.is_cell(row[index]) for index in indexes):
            hits.rows += 1
            if mode is ErasureMode.TOMBSTONE:
                kept.append(
                    [
                        _tombstone_cell(cell, matcher, tombstone, key=owners is None or index in owners)
                        for index, cell in enumerate(row)
                    ]
                )
            continue
        if owners == ():
            hits.ambiguous += sum(1 for cell in row if matcher.is_cell(cell))
        kept.append(_mask_cells(row, matcher, tombstone, hits, count_cells=True))
    if mode is None or not hits.any:
        return data, hits
    out = io.StringIO()
    csv.writer(out, delimiter=delimiter, lineterminator=terminator).writerows(kept)
    return bom + out.getvalue().encode(encoding), hits


def _owner_indexes(
    header: list[str] | tuple[str, ...], matcher: Matcher, key_columns: tuple[str, ...] | None
) -> tuple[int, ...] | None:
    """The columns that decide whose row it is: the key columns; None for "any cell" (a keyless file,
    text id); `()` for "none" (a keyless file and a numeric id, which cannot be decided, DEC-737)."""
    keys = tuple(index for index, name in enumerate(header) if key_columns and name.strip() in key_columns)
    if keys:
        return keys
    return () if matcher.numeric else None


def _mask_cells(
    row: list[str], matcher: Matcher, tombstone: str, hits: Hits, *, count_cells: bool
) -> list[str]:
    masked: list[str] = []
    for cell in row:
        if count_cells and matcher.masks_mentions() and matcher.is_cell(cell):
            hits.cells += 1
            masked.append(tombstone)
            continue
        replaced, count = matcher.replace(cell, tombstone)
        hits.occurrences += count
        masked.append(replaced)
    return masked


def _tombstone_cell(cell: str, matcher: Matcher, tombstone: str, *, key: bool) -> str:
    """A tombstoned row's cell: the id becomes the marker, a number stays, other text is emptied."""
    if ((key or matcher.masks_mentions()) and matcher.is_cell(cell)) or matcher.count(cell):
        return tombstone
    try:
        float(cell)
    except ValueError:
        return ""
    return cell


def _matching_rows(
    header: tuple[str, ...], rows: list[list[str]], matcher: Matcher, key_columns: tuple[str, ...] | None
) -> tuple[tuple[str, ...], ...]:
    """The principal's own rows, by the rule scan and rewrite use: someone else's row that merely
    mentions the id (a "referred by" cell) is theirs, and is not handed over (DEC-737)."""
    owners = _owner_indexes(header, matcher, key_columns)
    if owners is None:
        return tuple(
            tuple(row) for row in rows if any(matcher.is_cell(cell) or matcher.count(cell) for cell in row)
        )
    return tuple(
        tuple(row)
        for row in rows
        if any(index < len(row) and matcher.is_cell(row[index]) for index in owners)
    )


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------
def _parquet_rows(data: bytes) -> tuple[tuple[str, ...], list[list[str]]]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(pa.BufferReader(data))  # type: ignore[no-untyped-call]
    columns = [table.column(name).to_pylist() for name in table.column_names]
    rows = [[_render(column[index]) for column in columns] for index in range(table.num_rows)]
    return tuple(table.column_names), rows


def _render(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _parquet(
    data: bytes,
    matcher: Matcher,
    key_columns: tuple[str, ...] | None,
    *,
    mode: ErasureMode | None,
    tombstone: str,
) -> tuple[bytes, Hits]:
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(pa.BufferReader(data))  # type: ignore[no-untyped-call]
    names = table.column_names
    exact = {name: _exact_mask(table.column(name), matcher) for name in names}
    keys = [name for name in names if key_columns and name in key_columns]
    owner_names = keys or ([] if matcher.numeric else names)
    owned = np.zeros(table.num_rows, dtype=bool)
    for name in owner_names:
        owned |= exact[name]
    hits = Hits(rows=int(owned.sum()))
    if not keys and matcher.numeric:
        hits.ambiguous = int(sum(int(mask.sum()) for mask in exact.values()))
    other_rows = ~owned
    changes: dict[str, list[Any]] = {}
    for name in names:
        column = table.column(name)
        values: list[Any] | None = None
        if pa.types.is_dictionary(column.type) and _unused_dictionary_entry(
            column, matcher, used=bool(exact[name].any()), decides=name in owner_names or not matcher.numeric
        ):
            hits.occurrences += 1  # an unused dictionary entry is still the id in the file's bytes
            values = column.to_pylist()
        if not matcher.masks_mentions():
            if values is not None:
                changes[name] = values
            continue  # a numeric id is never masked outside the key columns (DEC-737)
        nested = pa.types.is_nested(column.type) or pa.types.is_dictionary(column.type)
        if not nested:  # a nested cell is rewritten value by value below, never replaced whole
            for index in np.flatnonzero(exact[name] & other_rows).tolist():
                values = values if values is not None else column.to_pylist()
                hits.cells += 1
                values[index] = _masked_value(column.type, tombstone)
        candidates = other_rows if nested else other_rows & ~exact[name]
        for index in _text_candidates(column, matcher, candidates):
            values = values if values is not None else column.to_pylist()
            values[index], count = _replace_deep(values[index], matcher, tombstone)
            hits.occurrences += count
        if values is not None:
            changes[name] = values
    if mode is None or not hits.any:
        return data, hits
    if mode is ErasureMode.TOMBSTONE and hits.rows:
        for name in names:
            values = changes.get(name) or table.column(name).to_pylist()
            column_type = table.column(name).type
            key = name in owner_names or matcher.masks_mentions()
            for index in np.flatnonzero(owned).tolist():
                values[index] = _tombstoned_value(values[index], column_type, matcher, tombstone, key=key)
            changes[name] = values
    for name, values in changes.items():
        position = names.index(name)
        schema_field = table.schema.field(position)
        table = table.set_column(position, schema_field, pa.array(values, type=schema_field.type))
    if mode is ErasureMode.DELETE and hits.rows:
        table = table.filter(pa.array(other_rows))
    for position, schema_field in enumerate(table.schema):
        if pa.types.is_dictionary(schema_field.type):
            # `filter` keeps a dictionary whole, so the deleted id would stay in the dictionary page:
            # rebuild each dictionary from the values that remain (DEC-737).
            rebuilt = pa.array(table.column(position).to_pylist(), type=schema_field.type)
            table = table.set_column(position, schema_field, rebuilt)
    sink = io.BytesIO()
    pq.write_table(table, sink)  # type: ignore[no-untyped-call]
    return sink.getvalue(), hits


def _exact_mask(column: pa.ChunkedArray, matcher: Matcher) -> Any:
    """A numpy bool array: which cells of `column` equal the id (nested values included)."""
    import numpy as np
    import pyarrow as pa

    pc: Any = importlib.import_module("pyarrow.compute")  # no stubs for the compute kernels

    kind = column.type
    if pa.types.is_string(kind) or pa.types.is_large_string(kind):
        if matcher.numeric:  # `00104` is `104`: compare in `principal_key` form (DEC-737)
            return np.array([matcher.is_cell(value) for value in column.to_pylist()], dtype=bool)
        mask = pc.equal(pc.utf8_trim_whitespace(column), matcher.principal_id)
        return np.asarray(pc.fill_null(mask, False).to_numpy(zero_copy_only=False), dtype=bool)
    if pa.types.is_dictionary(kind):
        return np.array([matcher.is_cell(value) for value in column.to_pylist()], dtype=bool)
    if pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_decimal(kind):
        try:
            number = float(matcher.principal_id)
        except ValueError:
            return np.zeros(len(column), dtype=bool)
        if principal_key(number) != matcher.principal_id:
            return np.zeros(len(column), dtype=bool)
        values = column.to_pylist()
        return np.array([matcher.is_cell(value) for value in values], dtype=bool)
    return np.array([_deep_equals(value, matcher) for value in column.to_pylist()], dtype=bool)


def _unused_dictionary_entry(column: pa.ChunkedArray, matcher: Matcher, *, used: bool, decides: bool) -> bool:
    """Whether a dictionary column's dictionary holds the id although no row uses it."""
    if used or not decides:
        return False
    return any(
        matcher.is_cell(value) or (isinstance(value, str) and matcher.count(value) > 0)
        for chunk in column.chunks
        for value in chunk.dictionary.to_pylist()
    )


def _deep_equals(value: object, matcher: Matcher) -> bool:
    if isinstance(value, dict):
        return any(_deep_equals(item, matcher) for item in value.values())
    if isinstance(value, list):
        return any(_deep_equals(item, matcher) for item in value)
    return matcher.is_text(value)


def _text_candidates(column: pa.ChunkedArray, matcher: Matcher, rows: Any) -> list[int]:
    """Rows (among `rows`) whose cell holds the id inside longer text, nested values included."""
    import numpy as np
    import pyarrow as pa

    pc: Any = importlib.import_module("pyarrow.compute")  # no stubs for the compute kernels

    kind = column.type
    if pa.types.is_string(kind) or pa.types.is_large_string(kind):
        found = pc.fill_null(pc.match_substring(column, matcher.principal_id), False)
        mask = np.asarray(found.to_numpy(zero_copy_only=False), dtype=bool) & rows
        values = column.to_pylist()
        return [index for index in np.flatnonzero(mask).tolist() if matcher.count(str(values[index]))]
    if pa.types.is_nested(kind) or pa.types.is_dictionary(kind):
        values = column.to_pylist()
        return [index for index in np.flatnonzero(rows).tolist() if _deep_count(values[index], matcher)]
    return []


def _deep_count(value: object, matcher: Matcher) -> int:
    if isinstance(value, dict):
        return sum(_deep_count(item, matcher) for item in value.values())
    if isinstance(value, list):
        return sum(_deep_count(item, matcher) for item in value)
    if isinstance(value, str) and not matcher.is_text(value):
        return matcher.count(value)
    return 1 if matcher.is_text(value) else 0


def _replace_deep(value: object, matcher: Matcher, tombstone: str) -> tuple[Any, int]:
    if isinstance(value, dict):
        total = 0
        replaced: dict[str, Any] = {}
        for key, item in value.items():
            replaced[key], count = _replace_deep(item, matcher, tombstone)
            total += count
        return replaced, total
    if isinstance(value, list):
        items: list[Any] = []
        total = 0
        for item in value:
            new, count = _replace_deep(item, matcher, tombstone)
            items.append(new)
            total += count
        return items, total
    if matcher.is_text(value):
        return tombstone, 1
    if isinstance(value, str):
        return matcher.replace(value, tombstone)
    return value, 0


def _masked_value(kind: pa.DataType, tombstone: str) -> Any:
    import pyarrow as pa

    return tombstone if pa.types.is_string(kind) or pa.types.is_large_string(kind) else None


def _tombstoned_value(
    value: object, kind: pa.DataType, matcher: Matcher, tombstone: str, *, key: bool
) -> Any:
    """A tombstoned row's cell: the id becomes the marker, numbers and booleans stay, text is emptied."""
    import pyarrow as pa

    is_text = pa.types.is_string(kind) or pa.types.is_large_string(kind)
    if (key and matcher.is_cell(value)) or (isinstance(value, str) and matcher.count(value)):
        return tombstone if is_text else None
    if is_text:
        return None
    if pa.types.is_nested(kind):
        return _replace_deep(value, matcher, tombstone)[0]
    return value


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def _json_or_none(data: bytes) -> tuple[Any, bool] | None:
    """The parsed document and whether the file ended in a newline; None when it is not JSON."""
    text = _decode(data)[0]
    try:
        return json.loads(text), text.endswith("\n")
    except ValueError:
        return None


_RECORD_KEY_FIELDS: Final[tuple[str, ...]] = ("primary_key", "entity_key")
"""Fields that name whose record a JSON object is, whatever the file's key column is called:
`ScoreRow.primary_key` (a scoring summary's sample rows), `RowExplanation.primary_key`,
`CopyMessage.entity_key`."""


def _carries(
    element: object, matcher: Matcher, keys: tuple[str, ...] | None, *, key_values: bool = False
) -> bool:
    """Whether a list element *is* the principal's row: the id, or a row/record holding it directly.

    A record that names a key column (the file's own, or a `_RECORD_KEY_FIELDS` field) is decided by
    that field alone, as a table row is; a numeric id is decided only that way, or inside a key
    column's own profile (`key_values`, DEC-737).
    """
    mentions = matcher.masks_mentions() or key_values
    if isinstance(element, dict):
        named = [name for name in (*(keys or ()), *_RECORD_KEY_FIELDS) if name in element]
        if named:
            return any(matcher.is_cell(element[name]) for name in named)
        return mentions and any(matcher.is_text(item) for item in element.values())
    if not mentions:
        return False
    if matcher.is_text(element):
        return True
    if isinstance(element, list):
        return any(matcher.is_text(item) for item in element)
    return False


def _json(
    node: Any, matcher: Matcher, keys: tuple[str, ...] | None, *, mode: ErasureMode | None, tombstone: str
) -> tuple[Any, Hits]:
    hits = Hits()
    return _walk(node, matcher, mode, tombstone, hits, keys), hits


def _key_positions(node: dict[str, Any], keys: tuple[str, ...] | None) -> tuple[int, ...]:
    """In a `DatasetProfile`, the positions of the key columns - what decides a `preview_rows` row."""
    columns = node.get("columns")
    if not keys or not isinstance(columns, list) or not isinstance(node.get("preview_rows"), list):
        return ()
    return tuple(
        int(column.get("position", index))
        for index, column in enumerate(columns)
        if isinstance(column, dict) and column.get("name") in keys
    )


def _walk(
    node: Any,
    matcher: Matcher,
    mode: ErasureMode | None,
    tombstone: str,
    hits: Hits,
    keys: tuple[str, ...] | None,
    *,
    key_values: bool = False,
) -> Any:
    """`node` without the principal. `key_values` is set inside a key column's own profile, whose
    sample values and categories are key values, so even a numeric id is decided there."""
    mentions = matcher.masks_mentions() or key_values
    if isinstance(node, dict):
        positions = _key_positions(node, keys)
        is_key_profile = isinstance(node.get("name"), str) and node.get("name") in (keys or ())
        result: dict[str, Any] = {}
        for key, value in node.items():
            if mentions and matcher.is_text(key):
                hits.cells += 1
                if mode is ErasureMode.TOMBSTONE:
                    result[tombstone] = _walk(value, matcher, mode, tombstone, Hits(), keys)
                continue
            if key == "preview_rows" and positions and isinstance(value, list):
                result[key] = _positional_rows(value, positions, matcher, mode, tombstone, hits)
                continue
            result[key] = _walk(
                value, matcher, mode, tombstone, hits, keys, key_values=key_values or is_key_profile
            )
        return result
    if isinstance(node, list):
        items: list[Any] = []
        for element in node:
            if _carries(element, matcher, keys, key_values=key_values):
                hits.rows += 1
                if mode is ErasureMode.TOMBSTONE:
                    items.append(_tombstone_element(element, matcher, tombstone))
                continue
            items.append(_walk(element, matcher, mode, tombstone, hits, keys, key_values=key_values))
        return items
    if matcher.is_text(node):
        if not mentions:
            if keys is None:
                hits.ambiguous += 1
            return node
        hits.cells += 1
        return tombstone
    if isinstance(node, str):
        replaced, count = matcher.replace(node, tombstone)
        hits.occurrences += count
        return replaced
    return node


def _positional_rows(
    rows: list[Any],
    positions: tuple[int, ...],
    matcher: Matcher,
    mode: ErasureMode | None,
    tombstone: str,
    hits: Hits,
) -> list[Any]:
    """A profile's `preview_rows`: a row is the principal's when a key position holds the id."""
    kept: list[Any] = []
    for row in rows:
        owned = isinstance(row, list) and any(
            position < len(row) and matcher.is_cell(row[position]) for position in positions
        )
        if owned:
            hits.rows += 1
            if mode is ErasureMode.TOMBSTONE:
                kept.append(_tombstone_element(row, matcher, tombstone))
            continue
        kept.append(_walk(row, matcher, mode, tombstone, hits, ()))
    return kept


def _tombstone_element(element: Any, matcher: Matcher, tombstone: str) -> Any:
    """A tombstoned JSON row, by the rule a tombstoned table row follows (DEC-742): the id becomes
    the marker, numbers, booleans and nulls stay, and every other text value is emptied."""
    if isinstance(element, dict):
        return {key: _tombstone_element(value, matcher, tombstone) for key, value in element.items()}
    if isinstance(element, list):
        return [_tombstone_element(item, matcher, tombstone) for item in element]
    if isinstance(element, str):
        return tombstone if matcher.is_text(element) or matcher.count(element) else ""
    if isinstance(element, int | float) and not isinstance(element, bool) and matcher.is_cell(element):
        return tombstone
    return element


def _json_fragments(
    node: Any,
    matcher: Matcher,
    path: str,
    out: list[tuple[str, Any]],
    keys: tuple[str, ...] | None = None,
    *,
    key_values: bool = False,
) -> None:
    """Every JSON value that is, or directly carries, the principal - with its JSONPath.

    Decided by the rules `_walk` applies, so the export hands over exactly what an erasure removes.
    """
    mentions = matcher.masks_mentions() or key_values
    if isinstance(node, dict):
        positions = _key_positions(node, keys)
        is_key_profile = isinstance(node.get("name"), str) and node.get("name") in (keys or ())
        for key, value in node.items():
            child = f"{path}.{key}"
            if mentions and matcher.is_text(key):
                out.append((f"{path}.{{key}}", value))
            elif mentions and (matcher.is_text(value) or (isinstance(value, str) and matcher.count(value))):
                out.append((child, value))
            elif key == "preview_rows" and positions and isinstance(value, list):
                out.extend(
                    (f"{child}[{index}]", row)
                    for index, row in enumerate(value)
                    if isinstance(row, list)
                    and any(position < len(row) and matcher.is_cell(row[position]) for position in positions)
                )
            else:
                _json_fragments(value, matcher, child, out, keys, key_values=key_values or is_key_profile)
        return
    if isinstance(node, list):
        for index, element in enumerate(node):
            if _carries(element, matcher, keys, key_values=key_values) or (
                isinstance(element, str) and matcher.count(element)
            ):
                out.append((f"{path}[{index}]", element))
            else:
                _json_fragments(element, matcher, f"{path}[{index}]", out, keys, key_values=key_values)
