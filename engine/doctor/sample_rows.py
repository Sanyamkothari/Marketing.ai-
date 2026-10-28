"""The rows Data Doctor's checks read: the whole file when it fits, else a random sample of the whole file.

A file sorted by date or by outcome read from the top shows one end of itself: the first months, only the churners
or only the customers whose ID starts with A. The checks (profile, joins, dates, outcome) therefore read a
**random sample of the whole file** when the file has more rows than the cap, never its first rows. The sample is
the same every time for the same file (its seed is the upload id), and IDs are read as text, so `0000123` stays
`0000123`.

The key, consent and outcome columns are read over **every row** of the file in the same pass: their full value
lists and exact counts (`LoadedFile.exact`), because one rare spelling of "no consent" is exactly what a sample
can miss. A sample is kept beside the upload so the next step of the visit does not read the file again.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Callable, Iterator, Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal

from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pandas as pd

    from engine.stages.ingest import ColumnStats
    from engine.storage import Storage

__all__ = ["KEY_NAME", "column_chunks", "doctor_rows", "exact_value_columns", "sample_seed"]

_LOG = get_logger(__name__)

KEY_NAME: Final[re.Pattern[str]] = re.compile(
    r"(^id$|_id$|^id_|_key$|customer|cust|phone|mobile|email|pan$)", re.I
)
_CONSENT_NAME: Final[re.Pattern[str]] = re.compile(
    r"consent|opt[_ ]?(in|out)|dnd|do_?not|unsubscrib|permission|whatsapp_?ok|marketing_?ok", re.I
)
_OUTCOME_NAME: Final[re.Pattern[str]] = re.compile(
    r"churn|convert|outcome|target|label|default|fraud|retain|response|cancel|attrition|lapsed", re.I
)
MAX_EXACT_COLUMNS: Final[int] = 8


def sample_seed(upload_id: str) -> int:
    """The same seed for the same upload, in every process."""
    return int(hashlib.sha256(upload_id.encode()).hexdigest()[:8], 16)


def exact_value_columns(names: Sequence[str]) -> tuple[str, ...]:
    """The columns read over every row: those named like a key, a consent flag or the outcome (at most eight)."""
    picked = [str(n) for n in names if _CONSENT_NAME.search(str(n)) or _OUTCOME_NAME.search(str(n))]
    picked += [str(n) for n in names if KEY_NAME.search(str(n)) and str(n) not in picked]
    return tuple(picked[:MAX_EXACT_COLUMNS])


def _stats_json(stats: dict[str, ColumnStats]) -> bytes:
    return json.dumps({name: vars(s) for name, s in stats.items()}, sort_keys=True).encode()


def _stats_from(raw: bytes) -> dict[str, ColumnStats]:
    from engine.stages.ingest import ColumnStats

    return {name: ColumnStats(**fields) for name, fields in json.loads(raw).items()}


def doctor_rows(
    storage: Storage,
    *,
    source_key: str,
    file_format: Literal["csv", "parquet"],
    upload_id: str,
    row_count: int,
    columns: Sequence[str],
    cap: int,
) -> tuple[pd.DataFrame, dict[str, ColumnStats], int | None]:
    """`(rows, exact facts by column, the file's row count when the rows are a sample)`."""
    from engine.stages import ingest

    if row_count <= cap:
        frame = ingest.read_upload(
            storage, source_key, file_format=file_format, max_rows=cap, row_cap=cap, keep_leading_zeros=True
        ).frame
        return frame, {}, None
    wanted = exact_value_columns(columns)
    stem = PurePosixPath(source_key).parent / f"doctor_sample_{cap}"
    rows_key, facts_key = f"{stem}.parquet", f"{stem}.json"
    try:
        if storage.exists(rows_key) and storage.exists(facts_key):
            import pandas as pd

            held = json.loads(storage.read_bytes(facts_key))
            if held.get("rows") == row_count and held.get("columns") == list(wanted):
                frame = pd.read_parquet(io.BytesIO(storage.read_bytes(rows_key)))
                return frame, _stats_from(json.dumps(held["exact"]).encode()), row_count
    except Exception as exc:  # a sample that cannot be read back is made again
        _LOG.info("doctor.sample_cache_unreadable upload=%s error=%s", upload_id, type(exc).__name__)
    sample = ingest.read_sample(
        storage,
        source_key,
        rows=cap,
        seed=sample_seed(upload_id),
        file_format=file_format,
        exact_columns=wanted,
    )
    try:
        buffer = io.BytesIO()
        sample.frame.to_parquet(buffer)
        storage.write_bytes(rows_key, buffer.getvalue())
        facts: dict[str, Any] = {
            "rows": sample.row_count,
            "columns": list(wanted),
            "exact": json.loads(_stats_json(sample.exact)),
        }
        storage.write_bytes(facts_key, json.dumps(facts, sort_keys=True).encode())
    except Exception as exc:  # no cache is not a reason to stop the visit
        _LOG.info("doctor.sample_cache_unwritten upload=%s error=%s", upload_id, type(exc).__name__)
    return sample.frame, sample.exact, sample.row_count


def column_chunks(
    storage: Storage,
    source_key: str,
    *,
    file_format: Literal["csv", "parquet"],
    delimiter: str | None,
    encoding: str,
    chunk_rows: int = 200_000,
) -> Callable[[Sequence[str]], Iterator[pd.DataFrame]]:
    """A reader of every row of some columns of the stored file, chunk by chunk, as text (an ID keeps its leading
    zeros): the counts a sample cannot make exactly (`LoadedFile.column_chunks`). Only the named columns are read, so
    an ID and a date over a large file is cheap. A column the file does not have raises `KeyError`."""

    def read(columns: Sequence[str]) -> Iterator[pd.DataFrame]:
        import pandas as pd

        from engine.stages.ingest import _open_parquet

        wanted = list(dict.fromkeys(str(c) for c in columns))
        if file_format == "parquet":
            parquet = _open_parquet(storage, source_key)
            missing = [c for c in wanted if c not in set(parquet.schema_arrow.names)]
            if missing:
                raise KeyError(missing[0])
            for batch in parquet.iter_batches(batch_size=chunk_rows, columns=wanted):
                yield batch.to_pandas()
            return
        with storage.open_read(source_key) as raw:
            text = io.TextIOWrapper(raw, encoding=encoding, newline="")
            header = pd.read_csv(text, sep="," if delimiter is None else delimiter, nrows=0, engine="c")
            missing = [c for c in wanted if c not in {str(h) for h in header.columns}]
            if missing:
                raise KeyError(missing[0])
        with storage.open_read(source_key) as raw:
            text = io.TextIOWrapper(raw, encoding=encoding, newline="")
            yield from pd.read_csv(
                text,
                sep="," if delimiter is None else delimiter,
                chunksize=chunk_rows,
                header=0,
                usecols=wanted,
                dtype=str,
                keep_default_na=False,
                na_values=[""],
                skipinitialspace=True,
                skip_blank_lines=True,
                engine="c",
                on_bad_lines="skip",  # the run's reader drops them too
            )

    return read
