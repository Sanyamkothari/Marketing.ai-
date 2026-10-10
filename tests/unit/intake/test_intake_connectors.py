"""Excel workbooks reach intake from connections too (Plan L M150, DEC-1504)."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from engine.connections.base import ConnectorError
from engine.connections.files import file_format_of, importable, preview_bytes
from engine.measurement import pull


def report_book() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Outcomes"
    for row in (
        ["Campaign outcomes"],
        ["customer_id", "converted"],
        ["C1", 1],
        ["C2", "-"],
        ["C3", 0],
        ["Total", 1],
    ):
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("key", ["exports/customers.xlsx", "exports/CUSTOMERS.XLSM"])
def test_a_workbook_in_a_bucket_can_be_picked(key: str) -> None:
    assert importable(key)
    assert file_format_of(key) == "xlsx"


def test_an_old_workbook_in_a_bucket_cannot_be_picked() -> None:
    assert not importable("exports/customers.xls")
    with pytest.raises(ConnectorError, match=r"CSV, Excel \(\.xlsx\) and Parquet"):
        file_format_of("exports/customers.xls")


def test_a_workbooks_preview_is_the_table_intake_reads() -> None:
    preview = preview_bytes(report_book(), "xlsx", truncated=False)
    assert list(preview.columns) == ["customer_id", "converted"]
    assert len(preview.rows) == 3


def test_a_damaged_workbook_preview_says_so() -> None:
    with pytest.raises(ConnectorError) as caught:
        preview_bytes(b"not a workbook", "xlsx", truncated=False)
    assert caught.value.code == "CONNECTION_PREVIEW_UNREADABLE"


def test_a_pulled_workbook_is_read_through_intake() -> None:
    frame = pull._read(report_book(), "xlsx")
    assert list(frame.columns) == ["customer_id", "converted"]
    assert frame["customer_id"].tolist() == ["C1", "C2", "C3"]
