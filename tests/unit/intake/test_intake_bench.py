"""Plan L M150's done-when: thirty messy files reach a profile with the right columns, and next month replays.

Each case goes through `intake_file` and then the unchanged ingest (`read_upload` and
`profile_dataset`), exactly as `POST /uploads` runs them, and must come out as the clean customer
table: the expected column names, 25 rows, no total or note row among the customers, and the
missing words read as empty so the number columns are numbers.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from engine.intake.config import IntakeConfig
from engine.intake.run import IntakeResult, intake_file
from engine.stages.ingest import read_upload
from engine.storage import LocalStorage
from tests.unit.intake.bench import CASES, ROWS, Case, write

CONFIG = IntakeConfig()


def run_case(case: Case, root: Path) -> tuple[IntakeResult, pd.DataFrame, LocalStorage]:
    storage = LocalStorage(root / "store")
    path = write(case, root)
    received = f"uploads/{case.name}/" + ("source.csv" if case.fmt == "csv" else "original.xlsx")
    storage.write_bytes(received, path.read_bytes())
    result = intake_file(
        storage,
        received_key=received,
        received_format=case.fmt,
        csv_key=f"uploads/{case.name}/source.csv",
        original_key=f"uploads/{case.name}/original.{case.fmt}",
        config=CONFIG,
    )
    frame = read_upload(storage, result.source_key, file_format=result.file_format).frame
    return result, frame, storage


def test_the_bench_has_thirty_cases() -> None:
    assert len(CASES) == 30
    assert len({case.name for case in CASES}) == 30


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_messy_file_reads_as_the_clean_table(case: Case, tmp_path: Path) -> None:
    result, frame, storage = run_case(case, tmp_path)
    assert list(frame.columns) == case.expected_columns
    assert len(frame) == ROWS
    assert (
        frame["id"].astype(str).str.startswith("C").all()
    ), "a title, total or note row was read as a customer"
    number_columns = ["Q1 spend", "Q2 spend"] if case.grouped else ["tenure", "spend"]
    for column in number_columns:
        assert pd.api.types.is_numeric_dtype(frame[column]), f"{column} did not read as numbers"
    report = result.report
    assert report.footer_rows == {"": 0, "total": 1, "total_note": 2}[case.footer]
    if case.fmt == "xlsx":
        assert report.sheet == "Customers"
        assert report.applied and storage.exists(f"uploads/{case.name}/original.xlsx")
    if case.missing:
        assert report.missing_words, "missing words were not read as empty"
        assert frame["active"].isna().any()


@pytest.mark.parametrize("case", [c for c in CASES if c.name == "01_clean_table"], ids=["01_clean_table"])
def test_a_clean_csv_is_not_rewritten(case: Case, tmp_path: Path) -> None:
    path = write(case, tmp_path)
    result, _frame, storage = run_case(case, tmp_path)
    assert not result.report.applied
    assert not storage.exists(f"uploads/{case.name}/original.csv")
    assert storage.read_bytes(f"uploads/{case.name}/source.csv") == path.read_bytes()
    assert result.report.notes == ()


@pytest.mark.parametrize(
    ("this_month", "next_month"),
    [("11_report_export", "29_next_month_csv"), ("26_xlsx_report", "30_next_month_xlsx")],
)
def test_next_months_export_is_read_like_this_months(
    this_month: str, next_month: str, tmp_path: Path
) -> None:
    """The same export with other values and other title text reads to the same columns and types."""
    by_name = {case.name: case for case in CASES}
    _, first, _ = run_case(by_name[this_month], tmp_path / "a")
    _, second, _ = run_case(by_name[next_month], tmp_path / "b")
    assert list(first.columns) == list(second.columns)
    assert [str(dtype) for dtype in first.dtypes] == [str(dtype) for dtype in second.dtypes]
    assert not set(first["id"]) & set(second["id"]), "the second month is a different set of customers"
