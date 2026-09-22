"""Ingest stage (M2): read the uploaded table once and profile it.

What this module guarantees
---------------------------
*One pass.* :func:`read_upload` opens the upload once, decodes it once and iterates it in chunks.
Rows above the profiling cap are parsed, counted, folded into the fingerprint and discarded, so
``DatasetProfile.row_count`` is **exact** on the upload path while memory stays bounded by one
chunk. The only path that reads a file twice is the encoding retry of step 8 below, bounded by the
four candidate codecs.

*Nothing fabricated* (plan §13.3). A number in the profile is measured or it is ``None``. The one
estimated quantity in the module - the row count of a caller-bounded preview read - is flagged by
``ReadResult.row_count_estimated`` and is never taken by ``POST /uploads``.

*No data value ever leaves the column it came from* (plan §13.7). ``log_stage`` is the only logging
call that carries numbers, PII detectors return detector *names* and never matched values, and every
surface that would otherwise show a PII column's content - ``sample_values``, ``top_categories`` and
``preview_rows`` - carries :data:`REDACTED` instead.

*Bad data is never an exception.* Only a genuinely unreadable *file* raises :class:`IngestError`;
everything about the data's fitness for training is a ``ValidationCheck`` raised by the validate
stage.

Type inference is an ordered table (:func:`infer_column_type`) whose order *is* the mixed-column
resolution rule; in particular the numeric branch is tried before the datetime branch, so a column of
digit strings can never be read as a date.

Heavy libraries (pandas, pyarrow) are imported inside the function bodies, never at module level:
``import engine`` stays fast and ``tests/integration/test_engine_imports.py`` enforces it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

from engine.config import ColumnType, ProblemType
from engine.contracts import CategoryCount, ColumnProfile, DatasetFingerprint, DatasetProfile
from engine.utils.logging import get_logger, log_stage
from engine.utils.text import humanise_count
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.storage import Storage

__all__ = [
    "CHUNK_ROWS",
    "DEFAULT_ID_LIKE_PATTERN",
    "DEFAULT_PROFILE_ROW_CAP",
    "ENCODINGS",
    "FINGERPRINT_ALGORITHM",
    "FINGERPRINT_PREFIX",
    "NAME_MIN_DISTINCT_RATIO",
    "PII_DETECTORS",
    "PREVIEW_ROWS",
    "REDACTED",
    "SAMPLE_VALUES",
    "TOP_CATEGORIES",
    "TOP_CATEGORIES_MAX_DISTINCT",
    "ColumnStats",
    "IngestError",
    "PiiDetector",
    "ReadResult",
    "canonical_chunk_bytes",
    "dataset_fingerprint",
    "detect_encoding",
    "detect_pii",
    "detect_problem_type",
    "exact_column_stats",
    "file_format_for",
    "fingerprint_columns",
    "infer_column_type",
    "ingest_detail",
    "primary_key_candidates",
    "profile_column",
    "profile_dataset",
    "profile_row_cap",
    "read_table",
    "read_upload",
    "schema_digest_line",
    "sniff_delimiter",
    "target_candidate",
    "time_column_candidates",
]

_LOG = get_logger(__name__)


# ---------------------------------------------------------------------------
# 1.1 Module constants
# ---------------------------------------------------------------------------
CHUNK_ROWS: Final[int] = 100_000
"""Rows per streaming chunk; one pass over the file, constant memory above the cap."""

DEFAULT_PROFILE_ROW_CAP: Final[int] = 2_000_000
"""Fallback when `config.validation.profile_row_cap` is absent (DEC-049)."""

SNIFF_BYTES: Final[int] = 65_536
"""Bytes of the head used for BOM, encoding and delimiter detection."""

CANDIDATE_DELIMITERS: Final[str] = ",;\t|"
ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
"""Fallback order (DEC-050). A UTF-16/32 BOM prepends the matching codec instead."""

PREVIEW_ROWS: Final[int] = 5
SAMPLE_VALUES: Final[int] = 5
TOP_CATEGORIES: Final[int] = 10
TOP_CATEGORIES_MAX_DISTINCT: Final[int] = 10
"""Any column with at most this many distinct values gets exact counts, whatever its type (DEC-051)."""

ID_DISTINCT_RATIO: Final[float] = 0.99
ID_MIN_ROWS: Final[int] = 20
NUMERIC_PARSE_RATE: Final[float] = 0.99
DATETIME_PARSE_RATE: Final[float] = 0.95
TEXT_MEAN_LENGTH: Final[float] = 50.0
PII_SAMPLE_VALUES: Final[int] = 1_000
REDACTED: Final[str] = "[REDACTED]"
MAX_CELL_CHARS: Final[int] = 200
"""Longest stringified cell any profile surface carries; longer values end in an ellipsis."""

BOOL_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"true", "t", "yes", "y", "1"})
BOOL_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"false", "f", "no", "n", "0"})
BOOL_TOKENS: Final[frozenset[str]] = BOOL_TRUE_TOKENS | BOOL_FALSE_TOKENS

CSV_SUFFIXES: Final[frozenset[str]] = frozenset({".csv", ".tsv", ".txt"})
PARQUET_SUFFIXES: Final[frozenset[str]] = frozenset({".parquet", ".pq"})

DEFAULT_ID_LIKE_PATTERN: Final[str] = r"(^id$|_id$|^id_|_key$|customer|cust)"
"""Used when the catalog carries no `column_name_patterns.id_like` (DEC-053; prototype `bindDetail`)."""

_NUMERIC_TYPES: Final[frozenset[ColumnType]] = frozenset(
    {ColumnType.INTEGER, ColumnType.FLOAT, ColumnType.BOOLEAN}
)
_CATEGORICAL_TYPES: Final[frozenset[ColumnType]] = frozenset(
    {ColumnType.STRING, ColumnType.TEXT, ColumnType.BOOLEAN}
)
_TEMPORAL_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.DATE, ColumnType.DATETIME})
_ID_LIKE_TYPES: Final[frozenset[ColumnType]] = frozenset(
    {ColumnType.STRING, ColumnType.TEXT, ColumnType.INTEGER}
)

# --- fingerprint (§1.12) ---------------------------------------------------
FINGERPRINT_VERSION: Final[str] = "v1"
FINGERPRINT_PREFIX: Final[str] = "sha256:v1:"
FINGERPRINT_ALGORITHM: Final[str] = "sha256:v1"
"""`DatasetFingerprint.algorithm`: the digest *and* the scheme version, so v2 is never ambiguous."""

FINGERPRINT_HEADER: Final[bytes] = b"marketing-ai/fingerprint/v1\n"
"""Domain separator: a fingerprint can never collide with a bare file hash."""

FINGERPRINT_FLOAT_FORMAT: Final[str] = "%.17g"
FINGERPRINT_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S.%f"


# ---------------------------------------------------------------------------
# 1.2 IngestError
# ---------------------------------------------------------------------------
class IngestError(Exception):
    """The file cannot be read at all. Bad *data* is never this - it is a ValidationCheck."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_UNREADABLE_MESSAGE: Final[str] = (
    "The file could not be read as {format}. Check that it is a valid {format} file."
)


# ---------------------------------------------------------------------------
# The pyarrow boundary
# ---------------------------------------------------------------------------
# `pyarrow` ships `py.typed` but `pyarrow.parquet.ParquetFile` is itself unannotated, so every call
# into it is a `no-untyped-call` under `mypy --strict`. Rather than silence that at each call site,
# the surface this module uses is declared once as a Protocol and acquired through one boundary
# function; everything downstream of `_open_parquet` is fully typed. (Adding `pyarrow.*` to the
# pyproject mypy overrides would be the other fix; that file belongs to another owner.)
class _ParquetBatch(Protocol):
    def to_pandas(self) -> pd.DataFrame: ...


class _ParquetMetadata(Protocol):
    @property
    def num_rows(self) -> int: ...


class _ParquetSchema(Protocol):
    @property
    def names(self) -> list[str]: ...

    def empty_table(self) -> _ParquetBatch: ...


class _ParquetReader(Protocol):
    @property
    def metadata(self) -> _ParquetMetadata: ...

    @property
    def schema_arrow(self) -> _ParquetSchema: ...

    def iter_batches(
        self, *, batch_size: int, columns: Sequence[str] | None = None
    ) -> Iterator[_ParquetBatch]: ...


def _open_parquet(storage: Storage, key: str) -> _ParquetReader:
    """`pq.ParquetFile` over the key's real path - `local_path` is the documented escape hatch."""
    import pyarrow.parquet

    factory: Any = pyarrow.parquet.ParquetFile
    return cast("_ParquetReader", factory(storage.local_path(key)))


# ---------------------------------------------------------------------------
# 1.3 Reading
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReadResult:
    """One read of an upload: the profiled rows, how they were read, and how many the file holds."""

    frame: pd.DataFrame
    file_format: Literal["csv", "parquet"]
    delimiter: str | None
    encoding: str
    row_count: int
    row_count_estimated: bool
    truncated: bool
    fingerprint: DatasetFingerprint | None = None
    """Covers every row of the file, including rows above the cap; `None` on a bounded preview read."""


def file_format_for(key: str) -> Literal["csv", "parquet"]:
    """Map a storage key's suffix to the format it is read as, case-insensitively."""
    suffix = PurePosixPath(key).suffix.lower()
    if suffix in CSV_SUFFIXES:
        return "csv"
    if suffix in PARQUET_SUFFIXES:
        return "parquet"
    raise IngestError("UPLOAD_UNSUPPORTED_FORMAT", "Only CSV and Parquet files can be uploaded.")


def _decode_head(head: bytes, codec: str) -> str | None:
    """`head` decoded strictly by `codec`, or `None`.

    A head cut at `SNIFF_BYTES` can end inside a multi-byte character, which is not evidence that
    the codec is wrong, so a full-size head is retried without its last four bytes.
    """
    payloads = (head, head[:-4]) if len(head) >= SNIFF_BYTES else (head,)
    for payload in payloads:
        try:
            return payload.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def detect_encoding(head: bytes) -> tuple[str, ...]:
    """The encodings to try for this head, best first; the last one never fails to decode."""
    if head.startswith(b"\xef\xbb\xbf"):
        candidates = ("utf-8-sig", *ENCODINGS[1:])
    elif head.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        candidates = ("utf-32", *ENCODINGS)
    elif head.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ("utf-16", *ENCODINGS)
    else:
        candidates = ENCODINGS
    kept = tuple(codec for codec in candidates if _decode_head(head, codec) is not None)
    fallback = ENCODINGS[-1]
    if not kept:
        return (fallback,)
    return kept if kept[-1] == fallback else (*kept, fallback)


def _complete_lines(text: str) -> str:
    """`text` with a BOM stripped and any partial trailing line dropped."""
    body = text.lstrip("﻿")
    cut = body.rfind("\n")
    return body if cut < 0 else body[: cut + 1]


def sniff_delimiter(head_text: str) -> str:
    """The CSV delimiter of `head_text`, falling back to a field-count vote and then to a comma."""
    try:
        return csv.Sniffer().sniff(head_text, delimiters=CANDIDATE_DELIMITERS).delimiter
    except csv.Error:
        pass
    lines = [line for line in head_text.splitlines() if line.strip()][:10]
    best = ","
    best_score = 0
    for candidate in CANDIDATE_DELIMITERS:
        counts = [len(record) for record in csv.reader(lines, delimiter=candidate)]
        if not counts or len(set(counts)) != 1 or counts[0] <= 1:
            continue
        if counts[0] > best_score:
            best, best_score = candidate, counts[0]
    return best


def _header_fields(head_text: str, delimiter: str) -> tuple[str, ...]:
    """The header record of `head_text`, quote-aware, each field stripped."""
    for record in csv.reader(io.StringIO(head_text), delimiter=delimiter):
        return tuple(field.strip() for field in record)
    return ()


def _check_header(fields: Sequence[str]) -> None:
    """Raise when the header is unusable: no names at all, or a repeated name."""
    if not any(field for field in fields):
        raise IngestError("UPLOAD_NO_COLUMNS", "The first row of the file does not contain column names.")
    seen: set[str] = set()
    for field in fields:
        if field and field in seen:
            raise IngestError(
                "UPLOAD_DUPLICATE_COLUMNS",
                f"The header repeats the column name '{field}'. Every column needs a distinct name.",
            )
        seen.add(field)


def _estimate_rows(head_text: str, delimiter: str, *, size: int, encoding: str) -> int:
    """Rows in the whole file, extrapolated from the complete data rows of the head."""
    body = _complete_lines(head_text)
    records = list(csv.reader(io.StringIO(body), delimiter=delimiter))
    if len(records) <= 1:
        return max(len(records) - 1, 0)
    header_line = body[: body.find("\n") + 1]
    head_bytes = len(body.encode(encoding, errors="replace"))
    header_bytes = len(header_line.encode(encoding, errors="replace"))
    data_bytes = head_bytes - header_bytes
    if data_bytes <= 0:
        return 0
    return round((size - header_bytes) * (len(records) - 1) / data_bytes)


def profile_row_cap(config: UseCaseConfig) -> int:
    """`config.validation.profile_row_cap` when the config carries one, else the module default."""
    configured: object = getattr(config.validation, "profile_row_cap", None)
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        return configured
    return DEFAULT_PROFILE_ROW_CAP


def read_upload(
    storage: Storage,
    key: str,
    *,
    file_format: Literal["csv", "parquet"] | None = None,
    max_rows: int | None = None,
    row_cap: int = DEFAULT_PROFILE_ROW_CAP,
    chunk_rows: int = CHUNK_ROWS,
) -> ReadResult:
    """Read `key` once: the profiled rows, the exact row count and the dataset fingerprint.

    `max_rows` asks for a cheap bounded preview instead: the tail is not read, so the row count is
    extrapolated (`row_count_estimated`) and no fingerprint is computed. `POST /uploads` never uses
    that path.
    """
    resolved = file_format if file_format is not None else file_format_for(key)
    if storage.size_bytes(key) == 0:
        raise IngestError("UPLOAD_EMPTY", "The file is empty.")
    if resolved == "parquet":
        return _read_parquet(storage, key, max_rows=max_rows, row_cap=row_cap, chunk_rows=chunk_rows)
    return _read_csv(storage, key, max_rows=max_rows, row_cap=row_cap, chunk_rows=chunk_rows)


def _read_csv(
    storage: Storage,
    key: str,
    *,
    max_rows: int | None,
    row_cap: int,
    chunk_rows: int,
) -> ReadResult:
    size = storage.size_bytes(key)
    with storage.open_read(key) as handle:
        head = handle.read(SNIFF_BYTES)
    for encoding in detect_encoding(head):
        decoded = _decode_head(head, encoding)
        if decoded is None:
            continue
        head_text = _complete_lines(decoded)
        delimiter = sniff_delimiter(head_text)
        _check_header(_header_fields(head_text, delimiter))
        try:
            return _read_csv_once(
                storage,
                key,
                encoding=encoding,
                delimiter=delimiter,
                head_text=head_text,
                size=size,
                max_rows=max_rows,
                row_cap=row_cap,
                chunk_rows=chunk_rows,
            )
        except UnicodeDecodeError:
            _LOG.info("ingest.encoding_retry encoding=%s", encoding)
            continue
    raise IngestError(
        "UPLOAD_ENCODING_UNSUPPORTED",
        "The file is not text the engine can read. Save it as UTF-8 CSV and upload it again.",
    )


def _read_csv_once(
    storage: Storage,
    key: str,
    *,
    encoding: str,
    delimiter: str,
    head_text: str,
    size: int,
    max_rows: int | None,
    row_cap: int,
    chunk_rows: int,
) -> ReadResult:
    """One decoded pass. Raises `UnicodeDecodeError` so the caller can try the next codec."""
    import pandas as pd

    bounded = max_rows is not None
    cap = max_rows if max_rows is not None else row_cap
    kept: list[pd.DataFrame] = []
    empty: pd.DataFrame | None = None
    rows_kept = 0
    row_count = 0
    digest = _ContentDigest()

    with storage.open_read(key) as raw:
        text = io.TextIOWrapper(raw, encoding=encoding, newline="")
        try:
            reader = pd.read_csv(
                text,
                sep=delimiter,
                chunksize=max(1, min(chunk_rows, cap) if bounded else chunk_rows),
                header=0,
                quotechar='"',
                doublequote=True,
                escapechar=None,
                skipinitialspace=True,
                skip_blank_lines=True,
                keep_default_na=True,
                na_values=[""],
                dtype=None,
                engine="c",
                on_bad_lines="warn",
                encoding=None,
            )
            for chunk in reader:
                if empty is None:
                    empty = chunk.iloc[0:0]
                row_count += len(chunk)
                if not bounded:
                    digest.update(chunk)
                if rows_kept < cap:
                    take = chunk if rows_kept + len(chunk) <= cap else chunk.iloc[: cap - rows_kept]
                    kept.append(take)
                    rows_kept += len(take)
                elif bounded:
                    break
        except pd.errors.EmptyDataError as exc:
            raise IngestError(
                "UPLOAD_NO_COLUMNS", "The first row of the file does not contain column names."
            ) from exc
        except pd.errors.ParserError as exc:
            raise IngestError("UPLOAD_UNREADABLE", _UNREADABLE_MESSAGE.format(format="CSV")) from exc

    if empty is None:
        raise IngestError("UPLOAD_NO_ROWS", "The file has a header row but no data rows.")
    frame = kept[0] if len(kept) == 1 else pd.concat(kept, ignore_index=True) if kept else empty
    frame = frame.reset_index(drop=True)
    if bounded:
        row_count = max(_estimate_rows(head_text, delimiter, size=size, encoding=encoding), rows_kept)
    if row_count == 0:
        raise IngestError("UPLOAD_NO_ROWS", "The file has a header row but no data rows.")
    return ReadResult(
        frame=frame,
        file_format="csv",
        delimiter=delimiter,
        encoding=encoding,
        row_count=row_count,
        row_count_estimated=bounded,
        truncated=row_count > len(frame),
        fingerprint=None if bounded else digest.finish(frame, n_rows=row_count),
    )


def _read_parquet(
    storage: Storage,
    key: str,
    *,
    max_rows: int | None,
    row_cap: int,
    chunk_rows: int,
) -> ReadResult:
    import pyarrow as pa

    cap = row_cap if max_rows is None else min(max_rows, row_cap)
    try:
        parquet_file = _open_parquet(storage, key)
        row_count = int(parquet_file.metadata.num_rows)
        names = tuple(str(name) for name in parquet_file.schema_arrow.names)
        _check_header(names)
        if row_count == 0:
            raise IngestError("UPLOAD_NO_ROWS", "The file has a header row but no data rows.")
        frame, digest = _parquet_frame(parquet_file, cap=cap, chunk_rows=chunk_rows, bounded=max_rows)
    except pa.ArrowInvalid as exc:
        raise IngestError("UPLOAD_UNREADABLE", _UNREADABLE_MESSAGE.format(format="Parquet")) from exc
    return ReadResult(
        frame=frame,
        file_format="parquet",
        delimiter=None,
        encoding="binary",
        row_count=row_count,
        row_count_estimated=False,
        truncated=row_count > len(frame),
        fingerprint=None if max_rows is not None else digest.finish(frame, n_rows=row_count),
    )


def _parquet_frame(
    parquet_file: _ParquetReader, *, cap: int, chunk_rows: int, bounded: int | None
) -> tuple[pd.DataFrame, _ContentDigest]:
    """Row groups up to `cap`, folding every row of the file into the digest on the unbounded path."""
    import pandas as pd

    digest = _ContentDigest()
    kept: list[pd.DataFrame] = []
    empty: pd.DataFrame | None = None
    rows_kept = 0
    for batch in parquet_file.iter_batches(batch_size=max(1, min(chunk_rows, cap) if cap else 1)):
        chunk = batch.to_pandas()
        if empty is None:
            empty = chunk.iloc[0:0]
        if bounded is None:
            digest.update(chunk)
        if rows_kept < cap:
            take = chunk if rows_kept + len(chunk) <= cap else chunk.iloc[: cap - rows_kept]
            kept.append(take)
            rows_kept += len(take)
        elif bounded is not None:
            break
    if empty is None:
        empty = parquet_file.schema_arrow.empty_table().to_pandas()
    frame = kept[0] if len(kept) == 1 else pd.concat(kept, ignore_index=True) if kept else empty
    return frame.reset_index(drop=True), digest


def read_table(storage: Storage, key: str, *, max_rows: int | None = None) -> pd.DataFrame:
    """Read a CSV (sniffed delimiter, BOM tolerated) or Parquet upload into a data frame."""
    return read_upload(storage, key, max_rows=max_rows).frame


# ---------------------------------------------------------------------------
# 1.12 Fingerprint
# ---------------------------------------------------------------------------
def fingerprint_columns(frame: pd.DataFrame) -> tuple[str, ...]:
    """Column names in FILE ORDER - the `columns` field, verbatim, never sorted."""
    return tuple(str(name) for name in frame.columns)


def schema_digest_line(frame: pd.DataFrame, types: Mapping[str, ColumnType]) -> str:
    """One tab-separated line per column, newline-joined. Includes the position, so it is order-aware."""
    lines = [
        f"{position}\t{name}\t{types[name].value}\t{frame[frame.columns[position]].dtype!s}"
        for position, name in enumerate(fingerprint_columns(frame))
    ]
    return "\n".join(lines)


def canonical_chunk_bytes(chunk: pd.DataFrame) -> bytes:
    """The one canonicaliser: stable across pandas dtypes, CSV/Parquet and chunk boundaries."""
    rendered = chunk.to_csv(
        index=False,
        header=False,
        lineterminator="\n",
        date_format=FINGERPRINT_DATE_FORMAT,
        float_format=FINGERPRINT_FLOAT_FORMAT,
        na_rep="",
    )
    return rendered.encode("utf-8")


class _ContentDigest:
    """Streams `canonical_chunk_bytes` into one sha256 and seals it with the schema at the end."""

    def __init__(self) -> None:
        self._content = hashlib.sha256()
        self._rows = 0

    def update(self, chunk: pd.DataFrame) -> None:
        self._content.update(canonical_chunk_bytes(chunk))
        self._rows += len(chunk)

    def finish(
        self,
        frame: pd.DataFrame,
        *,
        types: Mapping[str, ColumnType] | None = None,
        n_rows: int | None = None,
    ) -> DatasetFingerprint:
        resolved = types if types is not None else _inferred_types(frame)
        outer = hashlib.sha256()
        outer.update(FINGERPRINT_HEADER)
        outer.update(schema_digest_line(frame, resolved).encode("utf-8"))
        outer.update(b"\n")
        outer.update(self._content.digest())
        return DatasetFingerprint(
            hash=FINGERPRINT_PREFIX + outer.hexdigest(),
            algorithm=FINGERPRINT_ALGORITHM,
            n_rows=self._rows if n_rows is None else n_rows,
            columns=fingerprint_columns(frame),
        )


def _inferred_types(frame: pd.DataFrame) -> dict[str, ColumnType]:
    return {str(name): infer_column_type(frame[name]) for name in frame.columns}


def dataset_fingerprint(
    frame: pd.DataFrame,
    types: Mapping[str, ColumnType] | None = None,
    *,
    chunk_rows: int = CHUNK_ROWS,
    n_rows: int | None = None,
) -> DatasetFingerprint:
    """Fingerprint a whole in-memory frame; equal to the streaming digest of the same rows.

    Deliberately sensitive to row order and to column order (§1.12): a seeded split sends different
    rows to train and test when the rows arrive in a different order, so row-permuted data is not
    the same input and must not claim the same identity.
    """
    digest = _ContentDigest()
    step = max(1, chunk_rows)
    for start in range(0, len(frame), step):
        digest.update(frame.iloc[start : start + step])
    return digest.finish(frame, types=types, n_rows=n_rows)


# ---------------------------------------------------------------------------
# 1.4 Type inference
# ---------------------------------------------------------------------------
def _numeric_parse_rate(values: pd.Series[Any]) -> tuple[float, pd.Series[Any]]:
    import pandas as pd

    parsed = pd.to_numeric(values, errors="coerce")
    return float(parsed.notna().mean()), parsed


def _datetime_parse_rate(values: pd.Series[Any]) -> tuple[float, pd.Series[Any]]:
    import pandas as pd

    try:
        parsed = pd.to_datetime(values.astype(str), errors="coerce", format="mixed")
    except (ValueError, TypeError):
        return 0.0, pd.Series([], dtype="datetime64[ns]")
    return float(parsed.notna().mean()), parsed


def _all_midnight(values: pd.Series[Any]) -> bool:
    observed = values.dropna()
    if len(observed) == 0:
        return True
    return bool((observed.dt.normalize() == observed).all())


def infer_column_type(series: pd.Series[Any]) -> ColumnType:
    """The engine-level type of `series`, by the ordered table of §1.4.

    The order *is* the mixed-column rule: a column is whatever the earliest branch it satisfies says
    it is. Branch 8 (numeric text) precedes branch 9 (datetime text), so a column of digit strings is
    an integer column and never a date.
    """
    import pandas as pd

    observed = series.dropna()
    if len(observed) == 0:
        return ColumnType.STRING
    distinct = int(observed.nunique(dropna=True))

    # 1 - a real boolean dtype
    if pd.api.types.is_bool_dtype(series):
        return ColumnType.BOOLEAN
    # 2 - a real datetime dtype
    if pd.api.types.is_datetime64_any_dtype(series):
        return ColumnType.DATE if _all_midnight(observed) else ColumnType.DATETIME
    if pd.api.types.is_integer_dtype(series):
        # 3 - a 0/1 integer column is a boolean in disguise
        if distinct == 2 and set(observed.unique()) <= {0, 1}:
            return ColumnType.BOOLEAN
        # 4
        return ColumnType.INTEGER
    if pd.api.types.is_float_dtype(series):
        # 5 - a 0.0/1.0 float column is a boolean in disguise
        if distinct == 2 and set(observed.unique()) <= {0.0, 1.0}:
            return ColumnType.BOOLEAN
        # 6 - whole floats are integers that pandas widened for a null
        whole = bool((observed % 1 == 0).all()) and float(observed.abs().max()) < 2**53
        return ColumnType.INTEGER if whole else ColumnType.FLOAT

    text = observed.astype(str)
    # 7 - two distinct boolean tokens
    if distinct == 2:
        tokens = {str(value).strip().lower() for value in observed.unique()}
        if tokens <= BOOL_TOKENS and tokens & BOOL_TRUE_TOKENS and tokens & BOOL_FALSE_TOKENS:
            return ColumnType.BOOLEAN
    # 8 - numeric text, BEFORE any date parsing
    if distinct > 2:
        rate, parsed = _numeric_parse_rate(observed)
        if rate >= NUMERIC_PARSE_RATE:
            values = parsed.dropna()
            integral = bool((values % 1 == 0).all()) and float(values.abs().max()) < 2**53
            return ColumnType.INTEGER if integral else ColumnType.FLOAT
    # 9 - datetime text
    rate, parsed = _datetime_parse_rate(observed)
    if rate >= DATETIME_PARSE_RATE:
        return ColumnType.DATE if _all_midnight(parsed) else ColumnType.DATETIME
    # 10 - long free text
    if float(text.str.len().mean()) > TEXT_MEAN_LENGTH:
        return ColumnType.TEXT
    # 11
    return ColumnType.STRING


# ---------------------------------------------------------------------------
# 1.5 PII detection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PiiDetector:
    """One PII shape: what its values look like, what its column is usually called, and how sure.

    `min_distinct_ratio` guards the *values alone* branch: the share of the sampled values that
    must be distinct before the value pattern may fire on its own. It stays 0 for a shape no
    ordinary column wears by accident (an e-mail address, an Aadhaar number) and rises above 0 for
    a shape that is also the shape of a perfectly innocent category level. It never touches the
    name-assisted branch, so a column whose *name* says it holds names is judged exactly as before.
    """

    kind: str
    value_pattern: re.Pattern[str] | None
    name_pattern: re.Pattern[str] | None
    min_value_match_rate: float
    name_assisted_rate: float = 0.20
    min_distinct_ratio: float = 0.0


NAME_MIN_DISTINCT_RATIO: Final[float] = 0.40
"""How much of a sampled column must be distinct before its values alone may be read as names.

Well above any ordinary categorical column (a handful of levels over hundreds of rows) and well
below a real roster of people (near one distinct value per row, even when a few names repeat).
"""


PII_DETECTORS: Final[tuple[PiiDetector, ...]] = (
    PiiDetector(
        kind="email",
        value_pattern=re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        name_pattern=re.compile(r"(?i)(^|_)(e?mail|email_address)($|_)"),
        min_value_match_rate=0.60,
    ),
    PiiDetector(
        kind="phone",
        # An earlier version of this pattern allowed exactly two digit groups after the country
        # code, so the three-group NANP shape `+1-555-555-0001` - what `tests/fixtures/make_data.py`'s
        # `pii_column` variant writes, and the commonest international format there is - did not
        # match. The trailing group repeats one to two times instead; the guard that matters is
        # unchanged (at least seven digits), so a short integer column still cannot look like a phone.
        value_pattern=re.compile(
            r"(?:\+|00)?\d{1,3}[ \-]?(?:\(\d{2,4}\)[ \-]?)?\d{3,5}(?:[ \-]?\d{3,5}){1,2}"
        ),
        name_pattern=re.compile(r"(?i)(^|_)(phone|mobile|msisdn|contact_number|telephone)($|_)"),
        min_value_match_rate=0.80,
    ),
    PiiDetector(
        kind="pan",
        value_pattern=re.compile(r"(?i)[A-Z]{5}\d{4}[A-Z]"),
        name_pattern=re.compile(r"(?i)(^|_)pan(_no|_number)?($|_)"),
        min_value_match_rate=0.60,
    ),
    PiiDetector(
        kind="aadhaar",
        value_pattern=re.compile(r"[2-9]\d{3}[ \-]?\d{4}[ \-]?\d{4}"),
        name_pattern=re.compile(r"(?i)(^|_)aadhaa?r(_no|_number)?($|_)"),
        min_value_match_rate=0.80,
    ),
    PiiDetector(
        kind="name",
        # The value pattern is "one to four capitalised words", which is what a personal name looks
        # like - and also what a great many category levels look like: `Female`/`Male`, `Yes`/`No`,
        # `Basic`/`Premium` all full-match at a 100 % rate. Redacting such a column would silently
        # drop a model input, and a column like `gender` is exactly the one a fairness report wants.
        # What really separates the two is vocabulary size: names are open-ended and near-unique,
        # a category is a small fixed set repeated over and over. So on values alone the detector
        # also demands an open vocabulary (`min_distinct_ratio`); a column whose name says `name`
        # still fires through the name-assisted branch however few distinct values it carries.
        value_pattern=re.compile(r"[A-Z][a-z]+(?:[ '\-][A-Z][a-z]+){0,3}"),
        name_pattern=re.compile(
            r"(?i)(^|_)(name|first_name|last_name|full_name|given_name|surname|"
            r"customer_name|account_name|contact_name|holder_name)($|_)"
        ),
        min_value_match_rate=0.90,
        min_distinct_ratio=NAME_MIN_DISTINCT_RATIO,
    ),
)

_DIGIT_DETECTOR_KINDS: Final[frozenset[str]] = frozenset({"phone", "pan", "aadhaar"})
"""The only detectors an INTEGER column is examined for: a 10-digit mobile read as `int64`."""

_PII_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.STRING, ColumnType.TEXT, ColumnType.INTEGER})
"""FLOAT, BOOLEAN, DATE and DATETIME columns are never examined for PII."""


def detect_pii(series: pd.Series[Any], name: str, inferred: ColumnType) -> tuple[str, ...]:
    """Detector kinds that fired, in `PII_DETECTORS` order. Never returns, stores or logs a value."""
    if inferred not in _PII_TYPES:
        return ()
    textual = inferred is not ColumnType.INTEGER
    sample = [str(value).strip() for value in series.dropna().head(PII_SAMPLE_VALUES)]
    if not sample:
        return ()
    distinct_ratio = len(set(sample)) / len(sample)
    fired: list[str] = []
    for detector in PII_DETECTORS:
        if not textual and detector.kind not in _DIGIT_DETECTOR_KINDS:
            continue
        pattern = detector.value_pattern
        if pattern is None:
            continue
        matches = sum(1 for value in sample if pattern.fullmatch(value) is not None)
        rate = matches / len(sample)
        named = detector.name_pattern is not None and detector.name_pattern.search(name) is not None
        by_values = rate >= detector.min_value_match_rate and distinct_ratio >= detector.min_distinct_ratio
        by_name = named and rate >= detector.name_assisted_rate
        if by_values or by_name:
            fired.append(detector.kind)
    return tuple(fired)


# ---------------------------------------------------------------------------
# 1.6 Per-column profiling
# ---------------------------------------------------------------------------
def _cell_str(value: object) -> str:
    """One cell as the UI shows it: empty for a null, ISO for a date, no `repr` artefacts."""
    import pandas as pd

    if value is None or value is pd.NaT or value is pd.NA:
        return ""
    if pd.api.types.is_bool(value):
        return "true" if value else "false"
    if isinstance(value, pd.Timestamp):
        rendered = value.date().isoformat() if value == value.normalize() else value.isoformat()
    elif isinstance(value, float):
        if math.isnan(value):
            return ""
        rendered = str(int(value)) if value.is_integer() else str(value)
    elif isinstance(value, (datetime, date)):
        rendered = value.isoformat()
    else:
        rendered = str(value)
    if len(rendered) > MAX_CELL_CHARS:
        return rendered[: MAX_CELL_CHARS - 1] + "…"
    return rendered


def _numeric_summary(series: pd.Series[Any]) -> tuple[float | None, float | None, float | None]:
    import pandas as pd

    try:
        numeric = pd.to_numeric(series, errors="coerce")
    except (TypeError, ValueError):
        return None, None, None
    observed = numeric.dropna()
    if len(observed) == 0:
        return None, None, None
    return (
        round(float(observed.min()), 4),
        round(float(observed.max()), 4),
        round(float(observed.mean()), 4),
    )


def _top_categories(series: pd.Series[Any], *, non_null: int) -> tuple[CategoryCount, ...]:
    counts = series.value_counts(dropna=True)
    ordered = sorted(
        ((_cell_str(value), int(count)) for value, count in counts.items()),
        key=lambda item: (-item[1], item[0]),
    )
    return tuple(
        CategoryCount(value=value, count=count, share=round(count / non_null, 4))
        for value, count in ordered[:TOP_CATEGORIES]
    )


def profile_column(
    series: pd.Series[Any], *, position: int, profiled_rows: int, time_like: re.Pattern[str]
) -> ColumnProfile:
    """Everything the profile records about one column; PII content never reaches a field."""
    name = str(series.name)
    inferred = infer_column_type(series)
    null_count = int(series.isna().sum())
    non_null = profiled_rows - null_count
    distinct_count = int(series.nunique(dropna=True))
    is_unique = distinct_count == non_null and null_count < profiled_rows
    pii_kinds = detect_pii(series, name, inferred)

    if pii_kinds:
        sample_values: tuple[str, ...] = (REDACTED,) * min(SAMPLE_VALUES, max(non_null, 0))
        top_categories: tuple[CategoryCount, ...] = ()
    else:
        sample_values = tuple(_cell_str(value) for value in series.dropna().head(SAMPLE_VALUES))
        wanted = inferred in _CATEGORICAL_TYPES or distinct_count <= TOP_CATEGORIES_MAX_DISTINCT
        top_categories = _top_categories(series, non_null=non_null) if wanted and non_null else ()

    minimum, maximum, mean = _numeric_summary(series) if inferred in _NUMERIC_TYPES else (None, None, None)
    looks_like_id = (
        is_unique
        and null_count == 0
        and profiled_rows >= ID_MIN_ROWS
        and distinct_count >= ID_DISTINCT_RATIO * profiled_rows
        and inferred in _ID_LIKE_TYPES
    )
    return ColumnProfile(
        name=name,
        position=position,
        dtype=str(series.dtype),
        inferred_type=inferred,
        null_count=null_count,
        null_rate=round(null_count / profiled_rows, 4) if profiled_rows else 0.0,
        distinct_count=distinct_count,
        is_unique=is_unique,
        is_constant=distinct_count == 1,
        sample_values=sample_values,
        minimum=minimum,
        maximum=maximum,
        mean=mean,
        top_categories=top_categories,
        looks_like_id=looks_like_id,
        looks_like_time=bool(time_like.search(name)) or inferred in _TEMPORAL_TYPES,
        pii_kinds=pii_kinds,
    )


# ---------------------------------------------------------------------------
# 1.7 Whole-table profiling
# ---------------------------------------------------------------------------
def _preview_rows(df: pd.DataFrame, columns: Sequence[ColumnProfile]) -> tuple[tuple[str, ...], ...]:
    redact = tuple(bool(column.pii_kinds) for column in columns)
    head = df.head(PREVIEW_ROWS)
    return tuple(
        tuple(
            REDACTED if redact[position] else _cell_str(head.iat[row, position])
            for position in range(len(head.columns))
        )
        for row in range(len(head))
    )


def profile_dataset(
    df: pd.DataFrame,
    config: UseCaseConfig,
    *,
    upload_id: str,
    file_name: str,
    file_format: Literal["csv", "parquet"],
    file_size_bytes: int,
    delimiter: str | None,
    encoding: str,
    row_count: int | None = None,
    fingerprint: DatasetFingerprint | None = None,
) -> DatasetProfile:
    """Describe the uploaded table: per-column types, nulls, distincts and the preview rows.

    `row_count` is the file's row count when `df` holds only the capped prefix (DEC-046);
    `fingerprint` is the one `read_upload` computed over *every* row, and is recomputed from `df`
    only when the caller has none - the single-pass caller always passes it.
    """
    started = perf_counter()
    profiled_rows = len(df)
    total_rows = profiled_rows if row_count is None else row_count
    time_like = re.compile(config.catalog.column_name_patterns.time_like, re.IGNORECASE)
    columns = tuple(
        profile_column(df[name], position=position, profiled_rows=profiled_rows, time_like=time_like)
        for position, name in enumerate(df.columns)
    )
    column_count = len(columns)
    cells = profiled_rows * column_count
    nulls = sum(column.null_count for column in columns)
    profile = DatasetProfile(
        upload_id=upload_id,
        file_name=file_name,
        file_size_bytes=file_size_bytes,
        file_format=file_format,
        delimiter=delimiter,
        encoding=encoding,
        row_count=total_rows,
        column_count=column_count,
        columns=columns,
        primary_key_candidates=primary_key_candidates(columns, config),
        time_column_candidates=time_column_candidates(columns, config),
        target_candidate=target_candidate(columns, config),
        preview_rows=_preview_rows(df, columns),
        missing_value_rate_pct=round(100 * nulls / cells, 2) if cells else 0.0,
        fingerprint=(
            fingerprint
            if fingerprint is not None
            else dataset_fingerprint(df, _column_types(columns), n_rows=total_rows)
        ),
        profiled_at=utc_now(),
    )
    log_stage(_LOG, "ingest", rows=profiled_rows, seconds=perf_counter() - started)
    return profile


def _column_types(columns: Sequence[ColumnProfile]) -> dict[str, ColumnType]:
    return {column.name: column.inferred_type for column in columns}


# ---------------------------------------------------------------------------
# 1.8 Candidate detection
# ---------------------------------------------------------------------------
def _id_like_pattern(config: UseCaseConfig) -> re.Pattern[str]:
    configured: object = getattr(config.catalog.column_name_patterns, "id_like", None)
    pattern = configured if isinstance(configured, str) and configured else DEFAULT_ID_LIKE_PATTERN
    return re.compile(pattern, re.IGNORECASE)


def _hint_rank(name: str, hints: Sequence[str]) -> int | None:
    lowered = name.lower()
    for index, hint in enumerate(hints):
        if hint.lower() == lowered:
            return index
    return None


def primary_key_candidates(columns: Sequence[ColumnProfile], config: UseCaseConfig) -> tuple[str, ...]:
    """Unique, non-null columns: configured hints first, then id-like names, then file position."""
    id_like = _id_like_pattern(config)
    ranked: list[tuple[int, int, int, str]] = []
    for column in columns:
        if not (column.is_unique and column.null_count == 0):
            continue
        hint = _hint_rank(column.name, config.primary_key_hints)
        if hint is not None:
            ranked.append((0, hint, column.position, column.name))
        elif id_like.search(column.name):
            ranked.append((1, 0, column.position, column.name))
        else:
            ranked.append((2, 0, column.position, column.name))
    return tuple(name for _, _, _, name in sorted(ranked))


def time_column_candidates(columns: Sequence[ColumnProfile], config: UseCaseConfig) -> tuple[str, ...]:
    """Columns whose name looks time-like AND whose values parse as dates (plan §6.3)."""
    time_like = re.compile(config.catalog.column_name_patterns.time_like, re.IGNORECASE)
    ranked: list[tuple[int, int, int, str]] = []
    for column in columns:
        if not (time_like.search(column.name) and column.inferred_type in _TEMPORAL_TYPES):
            continue
        hint = _hint_rank(column.name, config.time_column_hints)
        if hint is not None:
            ranked.append((0, hint, column.position, column.name))
        else:
            ranked.append((1, 0, column.position, column.name))
    return tuple(name for _, _, _, name in sorted(ranked))


def target_candidate(columns: Sequence[ColumnProfile], config: UseCaseConfig) -> str | None:
    """The configured target column when the file carries it. Never guessed from the data."""
    configured = config.target.column
    if not configured:
        return None
    names = [column.name for column in columns]
    if configured in names:
        return configured
    matches = [name for name in names if name.lower() == configured.lower()]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# 1.9 Problem-type detection
# ---------------------------------------------------------------------------
def detect_problem_type(profile: DatasetProfile, target: str) -> ProblemType:
    """The prototype's `detectType`, driven by the profile's measured numbers rather than five rows."""
    column = next((c for c in profile.columns if c.name == target), None)
    if column is None:
        return ProblemType.BINARY_CLASSIFICATION
    if column.distinct_count <= 2:
        return ProblemType.BINARY_CLASSIFICATION
    if column.inferred_type in {ColumnType.INTEGER, ColumnType.FLOAT}:
        return ProblemType.FORECASTING if profile.time_column_candidates else ProblemType.REGRESSION
    return ProblemType.BINARY_CLASSIFICATION


# ---------------------------------------------------------------------------
# 1.10 The exact second pass for above-cap files
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ColumnStats:
    """Exact per-column facts for a file larger than the profiling cap (DEC-054)."""

    name: str
    row_count: int
    null_count: int
    distinct_count: int
    is_unique: bool
    value_counts: dict[str, int]


class _StatsAccumulator:
    """Row count, null count and the distinct set of one column, accumulated chunk by chunk."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.rows = 0
        self.nulls = 0
        self.values: dict[str, int] = {}
        self.distinct = 0

    def update(self, series: pd.Series[Any]) -> None:
        self.rows += len(series)
        self.nulls += int(series.isna().sum())
        for value, count in series.value_counts(dropna=True).items():
            key = _cell_str(value)
            self.values[key] = self.values.get(key, 0) + int(count)
        self.distinct = len(self.values)

    def finish(self) -> ColumnStats:
        non_null = self.rows - self.nulls
        return ColumnStats(
            name=self.name,
            row_count=self.rows,
            null_count=self.nulls,
            distinct_count=self.distinct,
            is_unique=self.distinct == non_null and self.nulls < self.rows,
            value_counts=(
                dict(sorted(self.values.items())) if self.distinct <= TOP_CATEGORIES_MAX_DISTINCT else {}
            ),
        )


def exact_column_stats(
    storage: Storage,
    key: str,
    columns: Sequence[str],
    *,
    file_format: Literal["csv", "parquet"],
    delimiter: str | None,
    encoding: str,
    chunk_rows: int = CHUNK_ROWS,
) -> dict[str, ColumnStats]:
    """A streaming pass restricted to `columns`, so `PK_*`, `TARGET_*` and the time check are exact.

    Used only when the file is larger than the profiling cap, and only for the primary key, target,
    time and consent columns - at most four, so the memory is one distinct-value set per column.
    """
    import pandas as pd

    wanted = list(dict.fromkeys(columns))
    accumulators = {name: _StatsAccumulator(name) for name in wanted}
    if not wanted:
        return {}
    if file_format == "parquet":
        parquet_file = _open_parquet(storage, key)
        for batch in parquet_file.iter_batches(batch_size=max(1, chunk_rows), columns=wanted):
            chunk = batch.to_pandas()
            for name in wanted:
                accumulators[name].update(chunk[name])
    else:
        with storage.open_read(key) as raw:
            text = io.TextIOWrapper(raw, encoding=encoding, newline="")
            reader = pd.read_csv(
                text,
                sep="," if delimiter is None else delimiter,
                chunksize=max(1, chunk_rows),
                header=0,
                usecols=wanted,
                skipinitialspace=True,
                skip_blank_lines=True,
                na_values=[""],
                engine="c",
                on_bad_lines="warn",
                encoding=None,
            )
            for chunk in reader:
                for name in wanted:
                    accumulators[name].update(chunk[name])
    return {name: accumulators[name].finish() for name in wanted}


# ---------------------------------------------------------------------------
# 1.11 Stage detail line
# ---------------------------------------------------------------------------
def ingest_detail(profile: DatasetProfile) -> str:
    """The `StageStatus.detail` of `StageKey.INGEST`: `"12K rows · 10 columns · CSV"`."""
    return (
        f"{humanise_count(profile.row_count)} rows · {profile.column_count} columns"
        f" · {profile.file_format.upper()}"
    )
