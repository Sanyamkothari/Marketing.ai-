"""JSON, JSON Lines and a one-table `.gz` or `.zip` (Plan O M240, DEC-1810 (a); decision O15)."""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from engine.intake.excel import LEGACY_EXCEL_SUFFIXES
from engine.intake.other_formats import (
    OTHER_FORMAT_SUFFIXES,
    is_other_format,
    read_other_format,
)
from engine.intake.run import received_format_for
from engine.stages.ingest import IngestError, read_upload
from engine.storage import LocalStorage

RECORDS = [
    {"customer_id": "0000123", "plan": "Gold", "spend": 10.50, "active": True, "tags": ["a", "b"]},
    {"customer_id": "0000124", "plan": "Silver", "spend": 7, "active": False, "address": {"city": "Pune"}},
    {"customer_id": "0000125", "plan": None, "spend": 3.25, "active": True, "address": {"city": "Delhi"}},
]


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "store")


def convert(storage: LocalStorage, name: str, data: bytes, **kwargs: int) -> tuple[pd.DataFrame, object]:
    storage.write_bytes(f"u/{name}", data)
    result = read_other_format(
        storage, received_key=f"u/{name}", csv_key="u/source.csv", file_name=name, **kwargs
    )
    frame = read_upload(storage, "u/source.csv", keep_leading_zeros=True).frame
    return frame, result


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


CSV = b"id,plan,spend\n0007,Gold,10\n0008,Silver,20\n"


# ---------------------------------------------------------------------------
# What is read, and what stays refused (O15)
# ---------------------------------------------------------------------------
def test_the_suffixes_read_are_exactly_json_jsonl_gz_and_zip() -> None:
    assert {".json", ".jsonl", ".ndjson", ".gz", ".zip"} == OTHER_FORMAT_SUFFIXES
    assert not OTHER_FORMAT_SUFFIXES & LEGACY_EXCEL_SUFFIXES
    assert is_other_format("A.JSON") and is_other_format("x.csv.gz") and not is_other_format("x.xls")


@pytest.mark.parametrize("name", ["old.xls", "binary.xlsb"])
def test_old_excel_files_stay_refused_whatever_reads_other_formats(storage: LocalStorage, name: str) -> None:
    storage.write_bytes(f"u/{name}", b"x")
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key=f"u/{name}", csv_key="u/source.csv", file_name=name)
    assert caught.value.code == "UPLOAD_UNSUPPORTED_FORMAT"
    with pytest.raises(IngestError) as refused:
        received_format_for(name)
    assert "save it as .xlsx or CSV" in refused.value.message


def test_the_upload_path_is_unchanged_until_the_integrator_wires_it() -> None:
    with pytest.raises(IngestError):
        received_format_for("records.json")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def test_a_list_of_records_becomes_a_table_with_flattened_fields(storage: LocalStorage) -> None:
    frame, result = convert(storage, "customers.json", json.dumps(RECORDS).encode())
    assert list(frame.columns) == ["customer_id", "plan", "spend", "active", "tags", "address.city"]
    assert frame["customer_id"].tolist() == ["0000123", "0000124", "0000125"]
    assert frame["spend"].tolist() == [10.5, 7.0, 3.25]
    assert frame["active"].tolist() == [True, False, True]
    assert frame["tags"].tolist()[0] == '["a","b"]'
    assert frame["address.city"].tolist()[1:] == ["Pune", "Delhi"]
    assert pd.isna(frame["plan"].iloc[2])
    assert (result.rows, result.inner_format, result.container) == (3, "json", "none")  # type: ignore[attr-defined]
    assert result.report.original_key == "u/customers.json"  # type: ignore[attr-defined]
    assert any("Nested fields were flattened" in note for note in result.report.notes)  # type: ignore[attr-defined]


def test_a_number_is_kept_as_it_was_written(storage: LocalStorage) -> None:
    frame, _ = convert(storage, "n.json", b'[{"price": 1.50, "id": "x"}, {"price": 2.00, "id": "y"}]')
    assert storage.read_text("u/source.csv").splitlines()[1:] == ["1.50,x", "2.00,y"]
    assert frame["price"].tolist() == [1.5, 2.0]


def test_an_object_holding_one_list_of_records_is_that_table(storage: LocalStorage) -> None:
    frame, _ = convert(storage, "w.json", json.dumps({"exported": "x", "customers": RECORDS}).encode())
    assert len(frame) == 3


def test_an_object_of_equal_length_columns_is_a_table(storage: LocalStorage) -> None:
    frame, _ = convert(storage, "c.json", b'{"id": ["01", "02"], "n": [1, 2]}')
    assert frame["id"].tolist() == ["01", "02"] and frame["n"].tolist() == [1, 2]


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        (b'{"a": [{"x": 1}], "b": [{"y": 2}]}', "2 lists of records"),
        (b'{"a": [1, 2], "b": [1]}', "different lengths"),
        (b"[1, 2, 3]", "not a table"),
        (b'"text"', "not a table"),
        (b"[]", "not a table"),
    ],
)
def test_json_that_is_not_one_table_is_refused_in_plain_words(
    storage: LocalStorage, document: bytes, fragment: str
) -> None:
    storage.write_bytes("u/x.json", document)
    with pytest.raises(IngestError, match=fragment) as caught:
        read_other_format(storage, received_key="u/x.json", csv_key="u/source.csv", file_name="x.json")
    assert caught.value.code == "UPLOAD_JSON_NOT_A_TABLE"
    assert not storage.exists("u/source.csv")


def test_invalid_json_is_unreadable_not_empty(storage: LocalStorage) -> None:
    storage.write_bytes("u/x.json", b'[{"a": 1},')
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/x.json", csv_key="u/source.csv", file_name="x.json")
    assert caught.value.code == "UPLOAD_UNREADABLE"


def test_json_with_a_byte_order_mark_and_utf16_are_read(storage: LocalStorage) -> None:
    text = json.dumps([{"name": "Zoë"}], ensure_ascii=False)
    frame, _ = convert(storage, "bom.json", b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert frame["name"].tolist() == ["Zoë"]
    frame16, _ = convert(storage, "u16.json", text.encode("utf-16"))
    assert frame16["name"].tolist() == ["Zoë"]


def test_a_json_file_past_the_size_limit_points_to_json_lines(
    storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    import engine.intake.other_formats as module

    monkeypatch.setattr(module, "MAX_JSON_BYTES", 100)
    storage.write_bytes("u/big.json", json.dumps(RECORDS * 5).encode())
    with pytest.raises(IngestError, match="JSON Lines") as caught:
        read_other_format(storage, received_key="u/big.json", csv_key="u/source.csv", file_name="big.json")
    assert caught.value.code == "UPLOAD_JSON_TOO_LARGE"


# ---------------------------------------------------------------------------
# JSON Lines
# ---------------------------------------------------------------------------
def test_json_lines_are_read_one_record_per_line_with_the_union_of_fields(storage: LocalStorage) -> None:
    lines = b"\n".join(json.dumps(record).encode() for record in RECORDS) + b"\n\n"
    frame, result = convert(storage, "customers.jsonl", lines)
    assert len(frame) == 3 and frame["customer_id"].tolist()[0] == "0000123"
    assert "address.city" in frame.columns
    assert result.inner_format == "jsonl"  # type: ignore[attr-defined]


def test_a_bad_json_lines_line_is_never_dropped_the_file_is_refused_naming_it(storage: LocalStorage) -> None:
    data = b'{"id": "1"}\n{"id": "2"\n{"id": "3"}\n[1, 2]\n'
    storage.write_bytes("u/x.jsonl", data)
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/x.jsonl", csv_key="u/source.csv", file_name="x.jsonl")
    assert caught.value.code == "UPLOAD_UNREADABLE"
    assert "Lines 2, 4 of this JSON Lines file are not a JSON record" in caught.value.message
    assert not storage.exists("u/source.csv")


def test_an_empty_json_lines_file_has_no_rows(storage: LocalStorage) -> None:
    storage.write_bytes("u/x.jsonl", b"\n\n")
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/x.jsonl", csv_key="u/source.csv", file_name="x.jsonl")
    assert caught.value.code == "UPLOAD_NO_ROWS"


# ---------------------------------------------------------------------------
# .gz
# ---------------------------------------------------------------------------
def test_a_gzipped_csv_is_read_as_the_csv_it_holds(storage: LocalStorage) -> None:
    frame, result = convert(storage, "customers.csv.gz", gzip.compress(CSV))
    assert storage.read_bytes("u/source.csv") == CSV
    assert frame["id"].tolist() == ["0007", "0008"]
    assert (result.container, result.inner_name, result.inner_format) == ("gz", "customers.csv", "csv")  # type: ignore[attr-defined]


def test_a_gzipped_json_and_json_lines_file_are_read(storage: LocalStorage) -> None:
    frame, _ = convert(storage, "r.json.gz", gzip.compress(json.dumps(RECORDS).encode()))
    assert len(frame) == 3
    lines = b"\n".join(json.dumps(record).encode() for record in RECORDS)
    frame2, _ = convert(storage, "r.jsonl.gz", gzip.compress(lines))
    assert len(frame2) == 3


@pytest.mark.parametrize(
    ("payload", "rows"),
    [(json.dumps(RECORDS).encode(), 3), (b"\n".join(json.dumps(r).encode() for r in RECORDS), 3), (CSV, 2)],
)
def test_a_gz_with_no_name_inside_is_told_by_its_first_characters(
    storage: LocalStorage, payload: bytes, rows: int
) -> None:
    frame, _ = convert(storage, "export.gz", gzip.compress(payload))
    assert len(frame) == rows


def test_a_tar_gz_is_refused_because_it_can_hold_many_files(storage: LocalStorage) -> None:
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("a.csv")
        info.size = len(CSV)
        archive.addfile(info, io.BytesIO(CSV))
    for name in ("export.tar.gz", "export.gz"):
        storage.write_bytes(f"u/{name}", buffer.getvalue())
        with pytest.raises(IngestError, match=r"\.tar archive") as caught:
            read_other_format(storage, received_key=f"u/{name}", csv_key="u/source.csv", file_name=name)
        assert caught.value.code == "UPLOAD_ARCHIVE_NOT_ONE_TABLE"


def test_a_damaged_gz_is_unreadable_with_what_to_check(storage: LocalStorage) -> None:
    storage.write_bytes("u/x.csv.gz", gzip.compress(CSV)[:-12])
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/x.csv.gz", csv_key="u/source.csv", file_name="x.csv.gz")
    assert caught.value.code == "UPLOAD_UNREADABLE"
    assert not storage.exists("u/source.csv")


def test_a_gz_that_unpacks_past_the_limit_is_stopped_while_it_streams(storage: LocalStorage) -> None:
    big = b"a,b\n" + b"1,2\n" * 100_000
    storage.write_bytes("u/x.csv.gz", gzip.compress(big))
    with pytest.raises(IngestError) as caught:
        read_other_format(
            storage,
            received_key="u/x.csv.gz",
            csv_key="u/source.csv",
            file_name="x.csv.gz",
            max_uncompressed_bytes=10_000,
        )
    assert caught.value.code == "UPLOAD_ARCHIVE_TOO_LARGE"
    assert not storage.exists("u/source.csv")


# ---------------------------------------------------------------------------
# .zip
# ---------------------------------------------------------------------------
def test_a_zip_holding_one_table_is_read_and_ignores_folders_and_mac_copies(storage: LocalStorage) -> None:
    data = zipped({"__MACOSX/._customers.csv": b"junk", "export/": b"", "export/customers.csv": CSV})
    frame, result = convert(storage, "export.zip", data)
    assert frame["id"].tolist() == ["0007", "0008"]
    assert (result.container, result.inner_name) == ("zip", "customers.csv")  # type: ignore[attr-defined]
    assert result.report.notes[0].startswith("Read the file inside the zip file")  # type: ignore[attr-defined]


def test_a_zip_holding_json_lines_is_converted(storage: LocalStorage) -> None:
    lines = b"\n".join(json.dumps(record).encode() for record in RECORDS)
    frame, _ = convert(storage, "x.zip", zipped({"c.jsonl": lines}))
    assert len(frame) == 3


def test_a_zip_holding_two_tables_is_refused_with_a_plain_reason_naming_them(storage: LocalStorage) -> None:
    storage.write_bytes("u/two.zip", zipped({"customers.csv": CSV, "bills.csv": CSV}))
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/two.zip", csv_key="u/source.csv", file_name="two.zip")
    assert caught.value.code == "UPLOAD_ARCHIVE_NOT_ONE_TABLE"
    message = caught.value.message
    assert "holds 2 tables" in message and '"customers.csv"' in message and '"bills.csv"' in message
    assert "one at a time" in message
    assert not storage.exists("u/source.csv")


def test_a_zip_with_a_workbook_inside_says_to_upload_that_file_directly(storage: LocalStorage) -> None:
    storage.write_bytes("u/x.zip", zipped({"book.xlsx": b"PK"}))
    with pytest.raises(IngestError, match="Unzip it and upload that file"):
        read_other_format(storage, received_key="u/x.zip", csv_key="u/source.csv", file_name="x.zip")


def test_a_zip_with_no_table_or_nothing_in_it_is_refused(storage: LocalStorage) -> None:
    for files, fragment in (({"notes.pdf": b"%PDF"}, "no table"), ({}, "empty")):
        storage.write_bytes("u/x.zip", zipped(files))
        with pytest.raises(IngestError, match=fragment):
            read_other_format(storage, received_key="u/x.zip", csv_key="u/source.csv", file_name="x.zip")


def test_a_zip_with_a_password_is_refused(storage: LocalStorage) -> None:
    data = bytearray(zipped({"c.csv": CSV}))
    local, central = data.index(b"PK\x03\x04"), data.index(b"PK\x01\x02")
    data[local + 6] |= 1  # general purpose bit 0: encrypted
    data[central + 8] |= 1
    storage.write_bytes("u/x.zip", bytes(data))
    with pytest.raises(IngestError, match="password") as caught:
        read_other_format(storage, received_key="u/x.zip", csv_key="u/source.csv", file_name="x.zip")
    assert caught.value.code == "UPLOAD_ARCHIVE_PROTECTED"


def test_a_zip_that_unpacks_past_the_limit_is_refused_before_anything_is_written(
    storage: LocalStorage,
) -> None:
    storage.write_bytes("u/x.zip", zipped({"c.csv": b"a,b\n" + b"1,2\n" * 50_000}))
    with pytest.raises(IngestError) as caught:
        read_other_format(
            storage,
            received_key="u/x.zip",
            csv_key="u/source.csv",
            file_name="x.zip",
            max_uncompressed_bytes=10_000,
        )
    assert caught.value.code == "UPLOAD_ARCHIVE_TOO_LARGE"
    assert not storage.exists("u/source.csv")


def test_a_zip_that_lies_about_its_size_is_unreadable_never_silently_cut_short(storage: LocalStorage) -> None:
    data = bytearray(zipped({"c.csv": b"a,b\n" + b"1,2\n" * 50_000}))
    central = data.index(b"PK\x01\x02")
    data[central + 24 : central + 28] = (10).to_bytes(4, "little")  # declared size
    storage.write_bytes("u/x.zip", bytes(data))
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/x.zip", csv_key="u/source.csv", file_name="x.zip")
    assert caught.value.code == "UPLOAD_UNREADABLE"
    assert not storage.exists("u/source.csv")


def test_something_that_is_not_a_zip_is_unreadable(storage: LocalStorage) -> None:
    storage.write_bytes("u/x.zip", b"not a zip")
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/x.zip", csv_key="u/source.csv", file_name="x.zip")
    assert caught.value.code == "UPLOAD_UNREADABLE"


# ---------------------------------------------------------------------------
# Review fixes: nothing in a record is lost; a readme is not a table; the report tells the truth
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "document",
    [
        [{"a": {"b": 1}, "a.b": 2}],
        [{"a.b": 2, "a": {"b": 1}}],
        [{"id": 1}, {"x": {"y": {"z": 1}}, "x.y": {"z": 2}}],
    ],
)
def test_two_fields_that_flatten_to_one_column_are_refused_not_merged(
    storage: LocalStorage, document: list[dict[str, object]]
) -> None:
    storage.write_bytes("u/c.json", json.dumps(document).encode())
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/c.json", csv_key="u/source.csv", file_name="c.json")
    assert caught.value.code == "UPLOAD_JSON_NOT_A_TABLE"
    assert (
        "become the same column" in caught.value.message
        and "One of them would be lost" in caught.value.message
    )
    assert not storage.exists("u/source.csv")


def test_two_json_lines_fields_that_flatten_to_one_column_are_refused(storage: LocalStorage) -> None:
    storage.write_bytes("u/c.jsonl", b'{"a": {"b": 1}, "a.b": 2}\n')
    with pytest.raises(IngestError) as caught:
        read_other_format(storage, received_key="u/c.jsonl", csv_key="u/source.csv", file_name="c.jsonl")
    assert caught.value.code == "UPLOAD_JSON_NOT_A_TABLE"
    assert not storage.exists("u/source.csv")


def test_a_readme_next_to_the_table_in_a_zip_is_not_a_second_table(storage: LocalStorage) -> None:
    data = zipped({"customers.csv": CSV, "README.txt": b"Exported on Monday. Questions? Ask finance."})
    frame, result = convert(storage, "export.zip", data)
    assert frame["id"].tolist() == ["0007", "0008"]
    assert result.inner_name == "customers.csv"  # type: ignore[attr-defined]


def test_a_zip_with_only_text_files_still_counts_them_as_tables(storage: LocalStorage) -> None:
    storage.write_bytes("u/two.zip", zipped({"a.txt": CSV, "b.txt": CSV}))
    with pytest.raises(IngestError, match="holds 2 tables"):
        read_other_format(storage, received_key="u/two.zip", csv_key="u/source.csv", file_name="two.zip")
    storage.write_bytes("u/one.zip", zipped({"a.txt": CSV}))
    frame, _ = convert(storage, "one.zip", zipped({"a.txt": CSV}))
    assert frame["id"].tolist() == ["0007", "0008"]


@pytest.mark.parametrize(
    ("name", "data", "source_format", "container"),
    [
        ("c.json", json.dumps(RECORDS).encode(), "json", "none"),
        ("c.jsonl", b'{"a": 1}\n{"a": 2}\n', "jsonl", "none"),
        ("c.csv.gz", gzip.compress(CSV), "csv", "gz"),
        ("c.json.gz", gzip.compress(json.dumps(RECORDS).encode()), "json", "gz"),
        ("c.zip", zipped({"c.csv": CSV}), "csv", "zip"),
        ("c.zip", zipped({"c.jsonl": b'{"a": 1}\n'}), "jsonl", "zip"),
    ],
    # Named ids: the gzip and zip bytes carry the time they were made, so ids built from them differ
    # between xdist workers that collect a second apart, and xdist then refuses to run at all.
    ids=["json", "jsonl", "csv-gz", "json-gz", "csv-zip", "jsonl-zip"],
)
def test_the_report_names_the_kind_of_table_and_the_container_it_arrived_in(
    storage: LocalStorage, name: str, data: bytes, source_format: str, container: str
) -> None:
    _, result = convert(storage, name, data)
    assert result.report.source_format == source_format  # type: ignore[attr-defined]
    assert result.report.container == container  # type: ignore[attr-defined]
