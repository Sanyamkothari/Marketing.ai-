"""Unit tests for `engine.stages.ingest` (design §1, plan §6.3 `ingest`).

The centre of the file is one known twelve-row CSV whose every profile field is asserted by hand,
so a bug in the profiler cannot agree with a bug in the test. Around it sit the reading tests
(delimiters, BOM, encodings, quoting, Parquet), the ordered type-inference table, the PII detectors
with their redaction guarantee, the fingerprint's stability and its deliberate order sensitivity,
and a realistic sweep driven off `tests/fixtures/make_data.py`'s clean and broken variants.

No use-case id is hard-coded: the known CSV is built from the first id `predictive_use_case_ids()`
reports and from that config's own hints and target column.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO

import pandas as pd
import pytest

from engine.config import ColumnRole, ColumnType, ProblemType, UseCaseConfig, load_use_case
from engine.contracts import ColumnProfile, DatasetProfile, dump_artefact
from engine.stages.ingest import (
    CHUNK_ROWS,
    ENCODINGS,
    PII_DETECTORS,
    REDACTED,
    SAMPLE_VALUES,
    TOP_CATEGORIES,
    TOP_CATEGORIES_MAX_DISTINCT,
    ColumnStats,
    IngestError,
    ReadResult,
    canonical_chunk_bytes,
    dataset_fingerprint,
    detect_encoding,
    detect_pii,
    detect_problem_type,
    exact_column_stats,
    file_format_for,
    fingerprint_columns,
    infer_column_type,
    ingest_detail,
    primary_key_candidates,
    profile_column,
    profile_dataset,
    profile_row_cap,
    read_table,
    read_upload,
    schema_digest_line,
    sniff_delimiter,
    target_candidate,
    time_column_candidates,
)
from engine.storage import LocalStorage
from tests.fixtures.make_data import (
    CONSTANT_COLUMN,
    HIGH_NULL_COLUMN,
    ID_LIKE_COLUMN,
    PII_EMAIL_COLUMN,
    PII_PHONE_COLUMN,
    GenerationSpec,
    generate,
    predictive_use_case_ids,
)

FIXTURE_ROWS = 2_000


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def use_case() -> UseCaseConfig:
    """The first use case the generator serves; nothing below names one."""
    return load_use_case(predictive_use_case_ids()[0])


def store_bytes(tmp_path: Path, name: str, payload: bytes) -> tuple[LocalStorage, str]:
    """A `LocalStorage` holding exactly `payload` under an upload key, and that key."""
    storage = LocalStorage(tmp_path / "store")
    key = f"uploads/u1/{name}"
    storage.write_bytes(key, payload)
    return storage, key


def store_frame(tmp_path: Path, frame: pd.DataFrame, name: str = "data.csv") -> tuple[LocalStorage, str]:
    return store_bytes(tmp_path, name, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))


def store_parquet(
    tmp_path: Path, frame: pd.DataFrame, name: str = "data.parquet"
) -> tuple[LocalStorage, str]:
    storage = LocalStorage(tmp_path / "store")
    key = f"uploads/u1/{name}"
    path = storage.local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return storage, key


def profile_of(frame: pd.DataFrame, config: UseCaseConfig, *, row_count: int | None = None) -> DatasetProfile:
    """`profile_dataset` with sane defaults, so a test names only what it cares about."""
    return profile_dataset(
        frame,
        config,
        upload_id="upl_test",
        file_name="data.csv",
        file_format="csv",
        file_size_bytes=1024,
        delimiter=",",
        encoding="utf-8-sig",
        row_count=row_count,
    )


def profile_upload(result: ReadResult, config: UseCaseConfig, *, size: int = 1024) -> DatasetProfile:
    """Profile a `ReadResult` the way `POST /uploads` does: one read, its exact count, its digest."""
    return profile_dataset(
        result.frame,
        config,
        upload_id="upl_test",
        file_name="data.csv",
        file_format=result.file_format,
        file_size_bytes=size,
        delimiter=result.delimiter,
        encoding=result.encoding,
        row_count=result.row_count,
        fingerprint=result.fingerprint,
    )


def column(profile: DatasetProfile, name: str) -> ColumnProfile:
    return next(c for c in profile.columns if c.name == name)


# --- the known twelve-row CSV ----------------------------------------------
KNOWN_ROWS: tuple[tuple[str, ...], ...] = (
    ("C-1", "2026-08-01", "12", "0.041", "premium", "true", "", "1"),
    ("C-2", "2026-08-01", "3", "0.010", "basic", "false", "", "0"),
    ("C-3", "2026-08-02", "7", "", "standard", "true", "", "1"),
    ("C-4", "2026-08-02", "0", "0.250", "basic", "false", "", "0"),
    ("C-5", "2026-08-03", "21", "0.180", "premium", "true", "", "1"),
    ("C-6", "2026-08-03", "5", "0.060", "", "false", "", "0"),
    ("C-7", "2026-08-04", "9", "0.075", "basic", "true", "", "0"),
    ("C-8", "2026-08-04", "14", "", "premium", "false", "", "1"),
    ("C-9", "2026-08-05", "2", "0.005", "standard", "true", "", "0"),
    ("C-10", "2026-08-05", "30", "0.400", "premium", "false", "", "1"),
    ("C-11", "2026-08-06", "8", "0.090", "basic", "true", "", "0"),
    ("C-12", "2026-08-06", "11", "0.120", "standard", "false", "", "0"),
)


def known_names(config: UseCaseConfig) -> tuple[str, str, str]:
    """The three names the known CSV borrows from the config, so no id is hard-coded."""
    return (config.primary_key_hints[0], config.time_column_hints[0], config.target.column or "target")


def known_csv(config: UseCaseConfig, *, delimiter: str = ",") -> bytes:
    key_name, time_name, target_name = known_names(config)
    header = (
        key_name,
        time_name,
        "visits_last_7d",
        "ad_ctr_90d",
        "plan_tier",
        "marketing_opt_in",
        "notes",
        target_name,
    )
    lines = [delimiter.join(header), *(delimiter.join(row) for row in KNOWN_ROWS)]
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Reading: delimiters, BOM, encodings, quoting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_sniffs_each_delimiter(tmp_path: Path, delimiter: str) -> None:
    payload = (f"a{delimiter}b\n1{delimiter}2\n3{delimiter}4\n5{delimiter}6\n").encode()
    storage, key = store_bytes(tmp_path, "data.csv", payload)
    result = read_upload(storage, key)
    assert result.delimiter == delimiter
    assert list(result.frame.columns) == ["a", "b"]
    assert result.frame.shape == (3, 2)
    assert result.row_count == 3


def test_single_column_file_falls_back_to_comma(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", b"only\n1\n2\n3\n")
    result = read_upload(storage, key)
    assert result.delimiter == ","
    assert list(result.frame.columns) == ["only"]
    assert result.row_count == 3


def test_delimiter_not_confused_by_quoted_commas(tmp_path: Path) -> None:
    payload = b'a,b\n"x,1",2\n"y,2",3\n"z,3",4\n'
    storage, key = store_bytes(tmp_path, "data.csv", payload)
    result = read_upload(storage, key)
    assert result.delimiter == ","
    assert result.frame.iat[0, 0] == "x,1"
    assert result.row_count == 3


def test_semicolon_file_with_commas_inside_fields(tmp_path: Path) -> None:
    payload = b"a;b\n1,5;2,5\n3,5;4,5\n5,5;6,5\n"
    storage, key = store_bytes(tmp_path, "data.csv", payload)
    result = read_upload(storage, key)
    assert result.delimiter == ";"
    assert result.frame.iat[0, 0] == "1,5"


def test_quoted_newline_inside_field_stays_one_row(tmp_path: Path) -> None:
    payload = b'a,b\n"line one\nline two",2\n"plain",3\n'
    storage, key = store_bytes(tmp_path, "data.csv", payload)
    result = read_upload(storage, key)
    assert result.row_count == 2
    assert result.frame.iat[0, 0] == "line one\nline two"


def test_utf8_bom_is_stripped(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", b"\xef\xbb\xbfid,v\n1,2\n")
    result = read_upload(storage, key)
    assert list(result.frame.columns) == ["id", "v"]
    assert result.encoding == "utf-8-sig"


def test_utf16_bom_selects_utf16_first(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", "id,name\n1,café\n".encode("utf-16"))
    result = read_upload(storage, key)
    assert result.encoding == "utf-16"
    assert list(result.frame.columns) == ["id", "name"]
    assert result.frame.iat[0, 1] == "café"


def test_latin1_text_reads_on_a_later_candidate(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", "id,name\n1,café\n".encode("latin-1"))
    result = read_upload(storage, key)
    assert result.encoding in {"cp1252", "latin-1"}
    assert result.frame.iat[0, 1] == "café"


def test_cp1252_smart_quote_survives(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", b"id,note\n1,it\x92s fine\n")
    result = read_upload(storage, key)
    assert result.encoding == "cp1252"
    assert result.frame.iat[0, 1] == "it’s fine"


class CountingStorage:
    """A `Storage` that records how often the upload was opened for reading."""

    def __init__(self, inner: LocalStorage) -> None:
        self.inner = inner
        self.opens = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def open_read(self, key: str) -> AbstractContextManager[BinaryIO]:
        self.opens += 1
        return self.inner.open_read(key)


def test_happy_path_opens_the_file_twice_once_to_sniff_and_once_to_read(tmp_path: Path) -> None:
    """Design §1.3 steps 2 and 4: the head is sniffed, then the file is decoded exactly once."""
    inner, key = store_bytes(tmp_path, "data.csv", numbered_csv(200))
    storage = CountingStorage(inner)
    result = read_upload(storage, key)  # type: ignore[arg-type]
    assert result.row_count == 200
    assert storage.opens == 2


def test_a_bad_byte_past_the_head_retries_with_the_next_candidate_encoding(tmp_path: Path) -> None:
    """Step 8: the only path that reads the file more than once, bounded by the candidate list."""
    prefix = "id,note\n" + "".join(f"{index},plain text row\n" for index in range(6_000))
    payload = prefix.encode("utf-8") + "6000,caf\xe9\n".encode("latin-1")
    assert len(prefix.encode("utf-8")) > 65_536  # the bad byte is past the sniffed head
    inner, key = store_bytes(tmp_path, "data.csv", payload)
    storage = CountingStorage(inner)
    result = read_upload(storage, key)  # type: ignore[arg-type]
    assert result.encoding in {"cp1252", "latin-1"}
    assert result.row_count == 6_001
    assert result.frame.iat[6_000, 1] == "café"
    assert 2 < storage.opens <= 1 + 2 * len(ENCODINGS)


def test_detect_encoding_orders_candidates_and_always_ends_in_a_safe_codec() -> None:
    assert detect_encoding(b"\xef\xbb\xbfid,v\n")[0] == "utf-8-sig"
    assert detect_encoding("id\n".encode("utf-16"))[0] == "utf-16"
    assert detect_encoding(b"id,v\n1,2\n")[0] == "utf-8-sig"
    assert detect_encoding(b"id,v\n1,\x92\n")[0] == "cp1252"
    for head in (b"\xef\xbb\xbfid,v\n", b"id,v\n1,\x92\n", b"\xff\xfe\x00\x00x"):
        assert detect_encoding(head)[-1] == ENCODINGS[-1]


def test_sniff_delimiter_prefers_the_widest_consistent_candidate() -> None:
    assert sniff_delimiter("a|b|c\n1|2|3\n") == "|"
    assert sniff_delimiter("single\n1\n2\n") == ","


def test_crlf_and_trailing_blank_lines(tmp_path: Path) -> None:
    payload = b"a,b\r\n1,2\r\n3,4\r\n\r\n\r\n"
    storage, key = store_bytes(tmp_path, "data.csv", payload)
    result = read_upload(storage, key)
    assert result.row_count == 2
    assert result.frame.iat[1, 1] == 4


def test_read_table_returns_the_frame_of_read_upload(tmp_path: Path, use_case: UseCaseConfig) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", known_csv(use_case))
    assert read_table(storage, key).equals(read_upload(storage, key).frame)


def test_file_format_for_suffixes() -> None:
    assert file_format_for("uploads/a/data.CSV") == "csv"
    assert file_format_for("uploads/a/data.tsv") == "csv"
    assert file_format_for("uploads/a/data.parquet") == "parquet"
    with pytest.raises(IngestError) as caught:
        file_format_for("uploads/a/report.docx")
    assert caught.value.code == "UPLOAD_UNSUPPORTED_FORMAT"


# ---------------------------------------------------------------------------
# Row and column counts, the profile row cap, degenerate files
# ---------------------------------------------------------------------------
def numbered_csv(rows: int) -> bytes:
    body = "".join(f"K-{index},{index},{index % 7}\n" for index in range(rows))
    return f"row_key,value,bucket\n{body}".encode()


def test_row_and_column_counts_are_exact(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", numbered_csv(5_000))
    result = read_upload(storage, key)
    assert result.row_count == 5_000
    assert len(result.frame) == 5_000
    assert len(result.frame.columns) == 3
    assert result.row_count_estimated is False
    assert result.truncated is False


def test_row_count_is_exact_above_the_profile_row_cap(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", numbered_csv(5_000))
    result = read_upload(storage, key, row_cap=1_000)
    assert len(result.frame) == 1_000
    assert result.row_count == 5_000
    assert result.row_count_estimated is False
    assert result.truncated is True


def test_profile_row_cap_keeps_rates_over_the_profiled_rows(tmp_path: Path, use_case: UseCaseConfig) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", numbered_csv(5_000))
    result = read_upload(storage, key, row_cap=100)
    profile = profile_upload(result, use_case)
    assert profile.row_count == 5_000
    assert len(profile.preview_rows) == 5
    assert column(profile, "value").distinct_count == 100
    assert profile.fingerprint.n_rows == 5_000


def test_chunked_read_matches_a_single_chunk_read(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", numbered_csv(1_000))
    small = read_upload(storage, key, chunk_rows=7)
    large = read_upload(storage, key, chunk_rows=CHUNK_ROWS)
    assert small.frame.equals(large.frame)
    assert small.row_count == large.row_count == 1_000


def test_max_rows_reads_a_bounded_preview_and_says_the_count_is_estimated(tmp_path: Path) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", numbered_csv(5_000))
    result = read_upload(storage, key, max_rows=100)
    assert len(result.frame) == 100
    assert result.row_count_estimated is True
    assert result.fingerprint is None
    assert 4_000 <= result.row_count <= 6_000


def test_single_row_file(tmp_path: Path, use_case: UseCaseConfig) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", b"row_key,value\nK-1,7\n")
    result = read_upload(storage, key)
    assert result.row_count == 1
    profile = profile_upload(result, use_case)
    assert profile.row_count == 1
    assert profile.column_count == 2
    assert len(profile.preview_rows) == 1
    key_column = column(profile, "row_key")
    assert key_column.is_unique is True
    assert key_column.is_constant is True
    assert key_column.looks_like_id is False


@pytest.mark.parametrize(
    ("name", "payload", "code"),
    [
        ("data.csv", b"", "UPLOAD_EMPTY"),
        ("data.csv", b"\n", "UPLOAD_NO_COLUMNS"),
        ("data.csv", b",,\n1,2,3\n", "UPLOAD_NO_COLUMNS"),
        ("data.csv", b"a,b\n", "UPLOAD_NO_ROWS"),
        ("data.csv", b"a,a\n1,2\n", "UPLOAD_DUPLICATE_COLUMNS"),
        ("report.docx", b"anything", "UPLOAD_UNSUPPORTED_FORMAT"),
        ("data.parquet", b"PAR1 not really a parquet file", "UPLOAD_UNREADABLE"),
    ],
)
def test_ingest_errors(tmp_path: Path, name: str, payload: bytes, code: str) -> None:
    storage, key = store_bytes(tmp_path, name, payload)
    with pytest.raises(IngestError) as caught:
        read_upload(storage, key)
    assert caught.value.code == code
    assert caught.value.message


def test_profile_row_cap_reads_the_config_when_it_carries_one(use_case: UseCaseConfig) -> None:
    assert profile_row_cap(use_case) > 0


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------
def test_parquet_round_trip(tmp_path: Path) -> None:
    frame = pd.DataFrame({"row_key": ["a", "b", "c"], "value": [1, 2, 3], "ratio": [0.5, 1.5, 2.5]})
    storage, key = store_parquet(tmp_path, frame)
    result = read_upload(storage, key)
    assert result.file_format == "parquet"
    assert result.delimiter is None
    assert result.encoding == "binary"
    assert result.row_count == 3
    assert result.frame["value"].tolist() == [1, 2, 3]


def test_parquet_row_count_comes_from_the_footer_not_the_data(tmp_path: Path) -> None:
    frame = pd.DataFrame({"row_key": [f"K-{i}" for i in range(500)], "value": range(500)})
    storage, key = store_parquet(tmp_path, frame)
    result = read_upload(storage, key, row_cap=0)
    assert result.row_count == 500
    assert result.row_count_estimated is False
    assert len(result.frame) == 0
    assert list(result.frame.columns) == ["row_key", "value"]
    assert result.truncated is True


# ---------------------------------------------------------------------------
# Type inference - the ordered table of §1.4
# ---------------------------------------------------------------------------
LONG_SENTENCE = "The quick brown fox jumps over the lazy dog while the sleepy cat watches on {n}."


@pytest.mark.parametrize(
    ("series", "expected"),
    [
        pytest.param(pd.Series([True, False, True]), ColumnType.BOOLEAN, id="1-bool-dtype"),
        pytest.param(
            pd.to_datetime(pd.Series(["2026-01-01", "2026-01-02", "2026-01-03"])),
            ColumnType.DATE,
            id="2-datetime-dtype-midnight",
        ),
        pytest.param(
            pd.to_datetime(pd.Series(["2026-01-01 10:00", "2026-01-02 11:30"])),
            ColumnType.DATETIME,
            id="2-datetime-dtype-with-time",
        ),
        pytest.param(pd.Series([0, 1, 1, 0], dtype="int64"), ColumnType.BOOLEAN, id="3-int-0-1"),
        pytest.param(pd.Series([0, 1, 2], dtype="int64"), ColumnType.INTEGER, id="4-int-three-levels"),
        pytest.param(pd.Series([1, 1, 1], dtype="int64"), ColumnType.INTEGER, id="4-int-constant"),
        pytest.param(pd.Series([0.0, 1.0, 1.0]), ColumnType.BOOLEAN, id="5-float-0-1"),
        pytest.param(pd.Series([2.0, 3.0, 4.0]), ColumnType.INTEGER, id="6-float-integral"),
        pytest.param(pd.Series([2.5, 3.5, 4.5]), ColumnType.FLOAT, id="6-float-fractional"),
        pytest.param(pd.Series(["true", "false", "true"]), ColumnType.BOOLEAN, id="7-true-false"),
        pytest.param(pd.Series(["yes", "no", "yes"]), ColumnType.BOOLEAN, id="7-yes-no"),
        pytest.param(pd.Series(["1", "0", "1"]), ColumnType.BOOLEAN, id="7-one-zero-text"),
        pytest.param(pd.Series(["1", "2", "3"], dtype=object), ColumnType.INTEGER, id="8-digit-text"),
        pytest.param(
            pd.Series(["20260101", "20260102", "20260103"], dtype=object),
            ColumnType.INTEGER,
            id="8-digit-text-that-would-parse-as-a-date",
        ),
        pytest.param(pd.Series(["1.5", "2.5", "3.5"], dtype=object), ColumnType.FLOAT, id="8-float-text"),
        pytest.param(
            pd.Series(["2026-08-01", "2026-08-02", "2026-08-03"]), ColumnType.DATE, id="9-iso-date-text"
        ),
        pytest.param(
            pd.Series(["2026-08-01T10:00", "2026-08-02T11:00", "2026-08-03T12:00"]),
            ColumnType.DATETIME,
            id="9-iso-datetime-text",
        ),
        pytest.param(
            pd.Series(["01/05/2026", "02/05/2026", "03/05/2026"]), ColumnType.DATE, id="9-slash-date-text"
        ),
        pytest.param(
            pd.Series([LONG_SENTENCE.format(n=i) for i in range(4)]), ColumnType.TEXT, id="10-long-text"
        ),
        pytest.param(pd.Series(["C-10482", "C-22917", "C-33333"]), ColumnType.STRING, id="11-id-like-text"),
        pytest.param(pd.Series(["premium", "basic", "standard"]), ColumnType.STRING, id="11-categories"),
        pytest.param(pd.Series([None, None], dtype=object), ColumnType.STRING, id="empty-column"),
    ],
)
def test_type_inference_table(series: pd.Series, expected: ColumnType) -> None:
    assert infer_column_type(series) is expected


def test_numeric_text_below_the_gate_is_a_string() -> None:
    values = [str(index) for index in range(98)] + ["n/a", "unknown"]
    assert infer_column_type(pd.Series(values, dtype=object)) is ColumnType.STRING


def test_numeric_text_above_the_gate_is_numeric() -> None:
    values = [f"{index}.5" for index in range(199)] + ["n/a"]
    assert infer_column_type(pd.Series(values, dtype=object)) is ColumnType.FLOAT


def test_digit_strings_never_become_a_date_although_they_would_parse() -> None:
    """Branch 8 precedes branch 9: the date parse would succeed, and must not be reached."""
    values = pd.Series(["20260101", "20260102", "20260103", "20260104"], dtype=object)
    assert pd.to_datetime(values, errors="coerce", format="mixed").notna().mean() == 1.0
    assert infer_column_type(values) is ColumnType.INTEGER


def test_mixed_column_resolution_is_the_first_matching_branch() -> None:
    """A column that is both numeric and date-parseable resolves numeric, by the table's order."""
    values = pd.Series([f"2026080{index % 9 + 1}" for index in range(100)], dtype=object)
    assert pd.to_datetime(values, errors="coerce", format="mixed").notna().mean() >= 0.95
    assert infer_column_type(values) is ColumnType.INTEGER


# ---------------------------------------------------------------------------
# The known twelve-row CSV
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def known_profile(tmp_path_factory: pytest.TempPathFactory, use_case: UseCaseConfig) -> DatasetProfile:
    payload = known_csv(use_case)
    storage, key = store_bytes(tmp_path_factory.mktemp("known"), "data.csv", payload)
    result = read_upload(storage, key)
    return profile_upload(result, use_case, size=len(payload))


def test_known_csv_shape(known_profile: DatasetProfile) -> None:
    assert known_profile.row_count == 12
    assert known_profile.column_count == 8
    assert [c.position for c in known_profile.columns] == list(range(8))
    assert known_profile.delimiter == ","
    assert known_profile.file_format == "csv"


def test_known_csv_key_column(known_profile: DatasetProfile, use_case: UseCaseConfig) -> None:
    key_name, _, _ = known_names(use_case)
    key_column = column(known_profile, key_name)
    assert key_column.inferred_type is ColumnType.STRING
    assert key_column.null_count == 0
    assert key_column.null_rate == 0.0
    assert key_column.distinct_count == 12
    assert key_column.is_unique is True
    assert key_column.is_constant is False
    assert key_column.looks_like_id is False  # 12 rows < ID_MIN_ROWS
    assert key_column.sample_values == ("C-1", "C-2", "C-3", "C-4", "C-5")
    assert key_column.minimum is None


def test_known_csv_time_column(known_profile: DatasetProfile, use_case: UseCaseConfig) -> None:
    _, time_name, _ = known_names(use_case)
    time_column = column(known_profile, time_name)
    assert time_column.inferred_type is ColumnType.DATE
    assert time_column.distinct_count == 6
    assert time_column.looks_like_time is True
    assert time_column.is_unique is False


def test_known_csv_integer_column(known_profile: DatasetProfile) -> None:
    visits = column(known_profile, "visits_last_7d")
    assert visits.inferred_type is ColumnType.INTEGER
    assert visits.dtype == "int64"
    assert visits.null_count == 0
    assert visits.distinct_count == 12
    assert visits.is_unique is True
    assert visits.minimum == 0.0
    assert visits.maximum == 30.0
    assert visits.mean == round(122 / 12, 4)
    assert visits.looks_like_time is False


def test_known_csv_float_column_with_nulls(known_profile: DatasetProfile) -> None:
    ctr = column(known_profile, "ad_ctr_90d")
    assert ctr.inferred_type is ColumnType.FLOAT
    assert ctr.null_count == 2
    assert ctr.null_rate == round(2 / 12, 4)
    assert ctr.distinct_count == 10
    assert ctr.minimum == 0.005
    assert ctr.maximum == 0.4
    assert ctr.mean == round(1.231 / 10, 4)


def test_known_csv_categorical_column(known_profile: DatasetProfile) -> None:
    plan = column(known_profile, "plan_tier")
    assert plan.inferred_type is ColumnType.STRING
    assert plan.null_count == 1
    assert plan.distinct_count == 3
    assert plan.is_unique is False
    assert [(c.value, c.count) for c in plan.top_categories] == [
        ("basic", 4),
        ("premium", 4),
        ("standard", 3),
    ]
    assert plan.top_categories[0].share == round(4 / 11, 4)


def test_known_csv_boolean_column(known_profile: DatasetProfile) -> None:
    opt_in = column(known_profile, "marketing_opt_in")
    assert opt_in.inferred_type is ColumnType.BOOLEAN
    assert {c.value for c in opt_in.top_categories} == {"true", "false"}
    assert opt_in.sample_values[0] in {"true", "false"}


def test_known_csv_all_null_column(known_profile: DatasetProfile) -> None:
    notes = column(known_profile, "notes")
    assert notes.inferred_type is ColumnType.STRING
    assert notes.null_count == 12
    assert notes.null_rate == 1.0
    assert notes.distinct_count == 0
    assert notes.is_unique is False
    assert notes.is_constant is False
    assert notes.sample_values == ()
    assert notes.top_categories == ()


def test_known_csv_binary_target_carries_exact_counts(
    known_profile: DatasetProfile, use_case: UseCaseConfig
) -> None:
    """The DEC-051 widening: a 0/1 int64 target gets exact positive and negative counts."""
    _, _, target_name = known_names(use_case)
    target = column(known_profile, target_name)
    assert target.inferred_type is ColumnType.BOOLEAN
    assert target.dtype == "int64"
    assert [(c.value, c.count) for c in target.top_categories] == [("0", 7), ("1", 5)]
    assert sum(c.count for c in target.top_categories) == 12
    assert target.minimum == 0.0
    assert target.maximum == 1.0
    assert target.mean == round(5 / 12, 4)


def test_known_csv_preview_rows(known_profile: DatasetProfile) -> None:
    assert len(known_profile.preview_rows) == 5
    assert all(len(row) == 8 for row in known_profile.preview_rows)
    assert known_profile.preview_rows[0][0] == "C-1"
    assert known_profile.preview_rows[0][2] == "12"
    assert known_profile.preview_rows[0][6] == ""  # the all-null notes column
    assert known_profile.preview_rows[2][3] == ""  # the missing ad_ctr_90d cell


def test_known_csv_missing_value_rate(known_profile: DatasetProfile) -> None:
    assert known_profile.missing_value_rate_pct == round(100 * 15 / (12 * 8), 2)


def test_known_csv_candidates(known_profile: DatasetProfile, use_case: UseCaseConfig) -> None:
    key_name, time_name, target_name = known_names(use_case)
    assert known_profile.primary_key_candidates[0] == key_name
    assert known_profile.time_column_candidates == (time_name,)
    assert known_profile.target_candidate == target_name


def test_known_csv_detail_line(known_profile: DatasetProfile) -> None:
    assert ingest_detail(known_profile) == "12 rows · 8 columns · CSV"


def test_profile_is_deterministic(tmp_path: Path, use_case: UseCaseConfig) -> None:
    storage, key = store_bytes(tmp_path, "data.csv", known_csv(use_case))
    first = profile_upload(read_upload(storage, key), use_case)
    second = profile_upload(read_upload(storage, key), use_case)
    assert first.model_dump(exclude={"profiled_at"}) == second.model_dump(exclude={"profiled_at"})


def test_row_count_override_keeps_rates_over_the_profiled_rows(use_case: UseCaseConfig) -> None:
    frame = pd.DataFrame({"row_key": range(1_000), "value": [None] * 500 + [1] * 500})
    profile = profile_of(frame, use_case, row_count=5_000)
    assert profile.row_count == 5_000
    assert column(profile, "value").null_rate == 0.5


# ---------------------------------------------------------------------------
# Candidate detection
# ---------------------------------------------------------------------------
def test_primary_key_candidate_ordering(use_case: UseCaseConfig) -> None:
    """Hint order first (against file order), then an id-like name, then position."""
    first_hint, second_hint = use_case.primary_key_hints[0], use_case.primary_key_hints[1]
    frame = pd.DataFrame(
        {
            "zz_unique": [f"Z-{i}" for i in range(30)],
            "external_key": [f"E-{i}" for i in range(30)],
            second_hint: [f"S-{i}" for i in range(30)],
            first_hint: [f"F-{i}" for i in range(30)],
            "not_unique": [i % 3 for i in range(30)],
        }
    )
    profile = profile_of(frame, use_case)
    assert profile.primary_key_candidates == (first_hint, second_hint, "external_key", "zz_unique")
    assert "not_unique" not in profile.primary_key_candidates


def test_primary_key_candidates_exclude_nullable_and_duplicate_columns(use_case: UseCaseConfig) -> None:
    frame = pd.DataFrame(
        {
            "with_nulls": [f"N-{i}" if i else None for i in range(30)],
            "duplicated": [f"D-{i % 5}" for i in range(30)],
        }
    )
    assert primary_key_candidates(profile_of(frame, use_case).columns, use_case) == ()


def test_time_column_candidates_need_a_matching_name_and_a_parsing_type(
    use_case: UseCaseConfig,
) -> None:
    hint = use_case.time_column_hints[0]
    dates = [f"2026-08-{i % 28 + 1:02d}" for i in range(30)]
    frame = pd.DataFrame(
        {
            "event_day": dates,
            "update_day": ["n/a"] * 30,  # name matches, values do not parse
            "created": dates,  # values parse, name does not match
            hint: dates,
        }
    )
    profile = profile_of(frame, use_case)
    assert profile.time_column_candidates == (hint, "event_day")
    assert column(profile, "created").looks_like_time is True
    assert column(profile, "update_day").looks_like_time is True
    assert time_column_candidates(profile.columns, use_case) == (hint, "event_day")


def test_looks_like_time_matches_on_the_name_alone(use_case: UseCaseConfig) -> None:
    frame = pd.DataFrame({"billing_day": list(range(30)), "plain": list(range(30))})
    profile = profile_of(frame, use_case)
    assert column(profile, "billing_day").looks_like_time is True
    assert column(profile, "plain").looks_like_time is False


def test_looks_like_id_needs_enough_rows(use_case: UseCaseConfig) -> None:
    small = pd.DataFrame({"row_key": [f"K-{i}" for i in range(12)]})
    large = pd.DataFrame({"row_key": [f"K-{i}" for i in range(30)]})
    assert column(profile_of(small, use_case), "row_key").looks_like_id is False
    assert column(profile_of(large, use_case), "row_key").looks_like_id is True


def test_target_candidate_matching(use_case: UseCaseConfig) -> None:
    target = use_case.target.column or ""
    frame = pd.DataFrame({"row_key": [1, 2, 3], target: [0, 1, 0]})
    assert target_candidate(profile_of(frame, use_case).columns, use_case) == target

    upper = pd.DataFrame({"row_key": [1, 2, 3], target.upper(): [0, 1, 0]})
    assert target_candidate(profile_of(upper, use_case).columns, use_case) == target.upper()

    absent = pd.DataFrame({"row_key": [1, 2, 3], "something_else": [0, 1, 0]})
    assert target_candidate(profile_of(absent, use_case).columns, use_case) is None


@pytest.mark.parametrize(
    ("values", "has_time", "expected"),
    [
        ([0, 1] * 20, False, ProblemType.BINARY_CLASSIFICATION),
        (["yes", "no"] * 20, False, ProblemType.BINARY_CLASSIFICATION),
        ([float(i) / 3 for i in range(40)], True, ProblemType.FORECASTING),
        ([float(i) / 3 for i in range(40)], False, ProblemType.REGRESSION),
        ([f"level_{i % 5}" for i in range(40)], False, ProblemType.BINARY_CLASSIFICATION),
    ],
)
def test_detect_problem_type(
    use_case: UseCaseConfig, values: list[object], has_time: bool, expected: ProblemType
) -> None:
    data: dict[str, object] = {"outcome": values}
    if has_time:
        data[use_case.time_column_hints[0]] = [f"2026-08-{i % 28 + 1:02d}" for i in range(40)]
    profile = profile_of(pd.DataFrame(data), use_case)
    assert detect_problem_type(profile, "outcome") is expected
    assert detect_problem_type(profile, "not_a_column") is ProblemType.BINARY_CLASSIFICATION


# ---------------------------------------------------------------------------
# PII detection and redaction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "values", "expected"),
    [
        (
            "billing_contact_email",
            [f"person.{i}@example.invalid" for i in range(20)],
            ("email",),
        ),
        ("contact_number", [f"+91 98765 {43210 + i}" for i in range(20)], ("phone",)),
        ("nanp_number", [f"+1-555-555-{i:04d}" for i in range(20)], ("phone",)),
        ("pan_no", [f"ABCDE{1000 + i}F" for i in range(20)], ("pan",)),
        ("uid", [f"2345 6789 {1000 + i}" for i in range(20)], ("phone", "aadhaar")),
        (
            "customer_name",
            ["Priya Sharma", "Arjun Mehta", "Rahul Verma", "Ananya Rao"] * 5,
            ("name",),
        ),
    ],
)
def test_pii_detectors_positive(name: str, values: list[str], expected: tuple[str, ...]) -> None:
    series = pd.Series(values, name=name, dtype=object)
    assert detect_pii(series, name, infer_column_type(series)) == expected


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("plan_tier", ["premium", "basic", "standard"] * 10),
        ("region", ["south", "north", "east", "west"] * 8),
        ("customer_id", [f"C-{10482 + i}" for i in range(30)]),
        ("snapshot_date", [f"2026-08-{i % 28 + 1:02d}" for i in range(30)]),
        ("ad_ctr_90d", [round(0.01 * i, 3) for i in range(30)]),
        ("tenure_months", list(range(1, 31))),
        ("notes", ["short note"] * 30),
    ],
)
def test_pii_detectors_negative(name: str, values: list[object]) -> None:
    series = pd.Series(values, name=name)
    assert detect_pii(series, name, infer_column_type(series)) == ()


def test_every_detector_has_a_positive_case() -> None:
    covered = {"email", "phone", "pan", "aadhaar", "name"}
    assert {detector.kind for detector in PII_DETECTORS} == covered


def test_pii_values_never_reach_any_profile_surface(tmp_path: Path, use_case: UseCaseConfig) -> None:
    emails = [f"person.{index}@example.invalid" for index in range(30)]
    frame = pd.DataFrame({"row_key": [f"K-{i}" for i in range(30)], "billing_email": emails})
    storage, key = store_frame(tmp_path, frame)
    profile = profile_upload(read_upload(storage, key), use_case)

    email_column = column(profile, "billing_email")
    assert email_column.pii_kinds == ("email",)
    assert email_column.sample_values == (REDACTED,) * SAMPLE_VALUES
    assert email_column.top_categories == ()
    assert all(row[1] == REDACTED for row in profile.preview_rows)
    assert profile.preview_rows[0][0] == "K-0"

    serialised = dump_artefact(profile)
    for value in emails:
        assert value not in serialised
    assert "example.invalid" not in serialised
    assert json.loads(serialised)["columns"][1]["pii_kinds"] == ["email"]


def test_pii_redaction_keeps_the_sample_short_for_a_short_column(use_case: UseCaseConfig) -> None:
    frame = pd.DataFrame({"billing_email": [f"p{i}@example.invalid" for i in range(3)]})
    profile = profile_of(frame, use_case)
    assert column(profile, "billing_email").sample_values == (REDACTED,) * 3


def test_non_textual_columns_are_never_examined_for_pii() -> None:
    floats = pd.Series([9876543210.0 + i for i in range(20)], name="amount")
    assert detect_pii(floats, "amount", ColumnType.FLOAT) == ()
    dates = pd.to_datetime(pd.Series([f"2026-08-{i % 28 + 1:02d}" for i in range(20)]))
    assert detect_pii(dates, "snapshot_date", ColumnType.DATE) == ()


def test_integer_columns_are_examined_for_digit_shaped_pii() -> None:
    mobiles = pd.Series([9876543210 + index for index in range(20)], name="mobile", dtype="int64")
    assert "phone" in detect_pii(mobiles, "mobile", ColumnType.INTEGER)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------
def fingerprint_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_key": [f"K-{index}" for index in range(200)],
            "value": list(range(200)),
            "ratio": [round(index / 7, 6) for index in range(200)],
        }
    )


def test_fingerprint_prefix_and_shape(tmp_path: Path) -> None:
    storage, key = store_frame(tmp_path, fingerprint_frame())
    result = read_upload(storage, key)
    assert result.fingerprint is not None
    assert result.fingerprint.hash.startswith("sha256:v1:")
    digest = result.fingerprint.hash.removeprefix("sha256:v1:")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert result.fingerprint.algorithm == "sha256:v1"
    assert result.fingerprint.n_rows == 200
    assert result.fingerprint.columns == ("row_key", "value", "ratio")


def test_fingerprint_is_stable_across_two_runs(tmp_path: Path) -> None:
    storage, key = store_frame(tmp_path, fingerprint_frame())
    first, second = read_upload(storage, key), read_upload(storage, key)
    assert first.fingerprint is not None and second.fingerprint is not None
    assert first.fingerprint.hash == second.fingerprint.hash


def test_fingerprint_is_chunk_size_invariant(tmp_path: Path) -> None:
    storage, key = store_frame(tmp_path, fingerprint_frame())
    small = read_upload(storage, key, chunk_rows=7)
    large = read_upload(storage, key, chunk_rows=CHUNK_ROWS)
    assert small.fingerprint is not None and large.fingerprint is not None
    assert small.fingerprint.hash == large.fingerprint.hash
    assert dataset_fingerprint(large.frame, chunk_rows=3).hash == large.fingerprint.hash
    assert dataset_fingerprint(large.frame).hash == large.fingerprint.hash


def test_fingerprint_covers_rows_above_the_profile_row_cap(tmp_path: Path) -> None:
    storage, key = store_frame(tmp_path, fingerprint_frame())
    full = read_upload(storage, key)
    capped = read_upload(storage, key, row_cap=10)
    assert full.fingerprint is not None and capped.fingerprint is not None
    assert capped.fingerprint.hash == full.fingerprint.hash
    assert capped.fingerprint.n_rows == 200
    assert len(capped.frame) == 10


def test_fingerprint_is_content_sensitive(tmp_path: Path) -> None:
    frame = fingerprint_frame()
    changed = frame.copy()
    changed.iat[57, 1] = int(changed.iat[57, 1]) + 1
    assert dataset_fingerprint(frame).hash != dataset_fingerprint(changed).hash


def test_fingerprint_is_column_order_sensitive(tmp_path: Path) -> None:
    frame = fingerprint_frame()
    swapped = frame[["value", "row_key", "ratio"]]
    assert dataset_fingerprint(frame).hash != dataset_fingerprint(swapped).hash
    assert dataset_fingerprint(swapped).columns == ("value", "row_key", "ratio")
    assert fingerprint_columns(frame) == ("row_key", "value", "ratio")


def test_fingerprint_is_row_order_sensitive() -> None:
    frame = fingerprint_frame()
    reversed_rows = frame.iloc[::-1].reset_index(drop=True)
    assert sorted(frame["value"]) == sorted(reversed_rows["value"])
    assert dataset_fingerprint(frame).hash != dataset_fingerprint(reversed_rows).hash


def test_fingerprint_is_format_agnostic_for_a_table_that_reads_back_identically(
    tmp_path: Path,
) -> None:
    frame = fingerprint_frame()
    csv_storage, csv_key = store_frame(tmp_path / "csv", frame)
    parquet_storage, parquet_key = store_parquet(tmp_path / "parquet", frame)
    from_csv = read_upload(csv_storage, csv_key)
    from_parquet = read_upload(parquet_storage, parquet_key)
    assert from_csv.frame.equals(from_parquet.frame)
    assert from_csv.fingerprint is not None and from_parquet.fingerprint is not None
    assert from_csv.fingerprint.hash == from_parquet.fingerprint.hash


def test_fingerprint_is_reproducible_in_another_process(tmp_path: Path, repo_root: Path) -> None:
    storage, key = store_frame(tmp_path, fingerprint_frame())
    expected = read_upload(storage, key).fingerprint
    assert expected is not None
    source = (
        "from engine.stages.ingest import read_upload\n"
        "from engine.storage import LocalStorage\n"
        f"storage = LocalStorage({str(storage.root)!r})\n"
        f"result = read_upload(storage, {key!r})\n"
        "print(result.fingerprint.hash)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(repo_root), "PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected.hash


def test_schema_digest_is_position_aware() -> None:
    frame = fingerprint_frame()
    types = {name: infer_column_type(frame[name]) for name in frame.columns}
    lines = schema_digest_line(frame, types).splitlines()
    assert len(lines) == 3
    assert lines[0].split("\t")[:3] == ["0", "row_key", "string"]
    assert lines[1].split("\t")[:3] == ["1", "value", "integer"]


def test_float_canonicalisation_round_trips() -> None:
    values = [5e-324, -0.0, 0.1, 1 / 3, 1e308, 2.2250738585072014e-308]
    rendered = canonical_chunk_bytes(pd.DataFrame({"f": values})).decode("utf-8").splitlines()
    assert [float(text) for text in rendered] == values


def test_canonicalisation_agrees_across_dtypes() -> None:
    """Chunk-size invariance rests on this: int64, float64 and object render a whole number alike."""
    as_int = canonical_chunk_bytes(pd.DataFrame({"x": pd.Series([1, 2], dtype="int64")}))
    as_float = canonical_chunk_bytes(pd.DataFrame({"x": pd.Series([1.0, 2.0], dtype="float64")}))
    as_text = canonical_chunk_bytes(pd.DataFrame({"x": pd.Series(["1", "2"], dtype=object)}))
    assert as_int == as_float == as_text


# ---------------------------------------------------------------------------
# The exact second pass, for files above the cap
# ---------------------------------------------------------------------------
def test_exact_column_stats_are_exact_above_the_cap(tmp_path: Path) -> None:
    payload = numbered_csv(500)
    storage, key = store_bytes(tmp_path, "data.csv", payload)
    capped = read_upload(storage, key, row_cap=5)
    assert len(capped.frame) == 5

    stats = exact_column_stats(
        storage,
        key,
        ["row_key", "bucket"],
        file_format="csv",
        delimiter=",",
        encoding=capped.encoding,
        chunk_rows=37,
    )
    assert isinstance(stats["row_key"], ColumnStats)
    assert stats["row_key"].row_count == 500
    assert stats["row_key"].distinct_count == 500
    assert stats["row_key"].is_unique is True
    assert stats["row_key"].null_count == 0
    assert stats["row_key"].value_counts == {}  # too many distinct values to keep
    assert stats["bucket"].distinct_count == 7
    assert sum(stats["bucket"].value_counts.values()) == 500


def test_exact_column_stats_over_parquet(tmp_path: Path) -> None:
    frame = pd.DataFrame({"row_key": [f"K-{i}" for i in range(100)], "bucket": [i % 4 for i in range(100)]})
    storage, key = store_parquet(tmp_path, frame)
    stats = exact_column_stats(
        storage, key, ["bucket"], file_format="parquet", delimiter=None, encoding="binary", chunk_rows=11
    )
    assert stats["bucket"].row_count == 100
    assert stats["bucket"].distinct_count == 4
    assert stats["bucket"].value_counts == {"0": 25, "1": 25, "2": 25, "3": 25}


# ---------------------------------------------------------------------------
# Realistic sweep, driven off the synthetic generator
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", predictive_use_case_ids())
def test_clean_fixture_profiles_cleanly(tmp_path: Path, use_case_id: str) -> None:
    config = load_use_case(use_case_id)
    frame = generate(GenerationSpec(use_case_id, rows=FIXTURE_ROWS))
    storage, key = store_frame(tmp_path, frame)
    result = read_upload(storage, key)
    profile = profile_upload(result, config)

    assert profile.row_count == FIXTURE_ROWS
    assert profile.column_count == len(config.template.columns)
    assert [c.name for c in profile.columns] == [c.name for c in config.template.columns]
    assert profile.target_candidate == config.target.column

    template_key = config.template.by_role(ColumnRole.PRIMARY_KEY)[0].name
    assert profile.primary_key_candidates[0] == template_key
    assert column(profile, template_key).is_unique is True
    assert column(profile, template_key).looks_like_id is True

    template_time = config.template.by_role(ColumnRole.TIME)
    if template_time:
        assert template_time[0].name in profile.time_column_candidates

    assert all(c.pii_kinds == () for c in profile.columns), "no clean column may look like PII"
    assert 0.0 <= profile.missing_value_rate_pct <= 100.0
    assert len(profile.preview_rows) == 5
    assert profile.fingerprint.n_rows == FIXTURE_ROWS


@pytest.mark.parametrize("use_case_id", predictive_use_case_ids()[:1])
@pytest.mark.parametrize(
    ("variant", "check"),
    [
        ("constant_column", "constant"),
        ("high_null_column", "high_null"),
        ("id_like_column", "id_like"),
        ("pii_column", "pii"),
        ("duplicate_keys", "duplicate_keys"),
        ("null_keys", "null_keys"),
    ],
)
def test_broken_fixture_variants_show_up_in_the_profile(
    tmp_path: Path, use_case_id: str, variant: str, check: str
) -> None:
    config = load_use_case(use_case_id)
    frame = generate(GenerationSpec(use_case_id, rows=FIXTURE_ROWS, variant=variant))
    storage, key = store_frame(tmp_path, frame)
    profile = profile_upload(read_upload(storage, key), config)
    template_key = config.template.by_role(ColumnRole.PRIMARY_KEY)[0].name

    if check == "constant":
        assert column(profile, CONSTANT_COLUMN).is_constant is True
        assert column(profile, CONSTANT_COLUMN).distinct_count == 1
    elif check == "high_null":
        assert column(profile, HIGH_NULL_COLUMN).null_rate > 0.6
    elif check == "id_like":
        assert column(profile, ID_LIKE_COLUMN).looks_like_id is True
        assert ID_LIKE_COLUMN in profile.primary_key_candidates
    elif check == "pii":
        assert column(profile, PII_EMAIL_COLUMN).pii_kinds == ("email",)
        assert column(profile, PII_PHONE_COLUMN).pii_kinds == ("phone",)
        serialised = dump_artefact(profile)
        assert "example.invalid" not in serialised
        assert "+1-555-555" not in serialised
    elif check == "duplicate_keys":
        assert column(profile, template_key).is_unique is False
        assert template_key not in profile.primary_key_candidates
    elif check == "null_keys":
        assert column(profile, template_key).null_count > 0
        assert template_key not in profile.primary_key_candidates


def test_top_categories_are_capped_and_totally_ordered(use_case: UseCaseConfig) -> None:
    values = ["a"] * 12 + ["b"] * 12 + [f"v{index:02d}" for index in range(30)]
    frame = pd.DataFrame({"category": values})
    categories = profile_column(
        frame["category"],
        position=0,
        profiled_rows=len(values),
        time_like=re.compile("never-matches"),
    ).top_categories
    assert len(categories) == TOP_CATEGORIES
    assert [c.count for c in categories] == sorted((c.count for c in categories), reverse=True)
    assert [c.value for c in categories][:2] == ["a", "b"]
    tail = [c.value for c in categories[2:]]
    assert tail == sorted(tail)


def test_top_categories_widening_applies_to_any_low_cardinality_column(use_case: UseCaseConfig) -> None:
    frame = pd.DataFrame({"score": [index % TOP_CATEGORIES_MAX_DISTINCT for index in range(100)]})
    profile = profile_of(frame, use_case)
    score = column(profile, "score")
    assert score.inferred_type is ColumnType.INTEGER
    assert len(score.top_categories) == TOP_CATEGORIES_MAX_DISTINCT
    assert sum(c.count for c in score.top_categories) == 100
