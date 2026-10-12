"""Rows the CSV reader drops without saying so (Plan O M240, DEC-1810 (b); probe `X_PARSER_ROW_LOSS`)."""

from __future__ import annotations

import random
from pathlib import Path

import pandas as pd
import pytest

from engine.doctor.parser_loss import MAX_LINES_NAMED, PARSER_LOSS_CODE, check_parser_loss
from engine.stages.ingest import read_upload
from engine.storage import LocalStorage

pytestmark = pytest.mark.filterwarnings("ignore::pandas.errors.ParserWarning")
"""The reader's own warning for the lines it drops is what these tests are about; they do not need to see it."""


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "store")


def lines_file(rows: list[str], header: str = "id,plan,spend") -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode()


GOOD = [f"C{index},Gold,{index}" for index in range(1, 13)]


def with_bad(bad_at: dict[int, str]) -> list[str]:
    """`GOOD` with the given 0-based positions replaced by malformed lines."""
    rows = list(GOOD)
    for position, text in bad_at.items():
        rows[position] = text
    return rows


# ---------------------------------------------------------------------------
# The gap: the reader keeps going and the row count is simply smaller
# ---------------------------------------------------------------------------
def test_today_the_reader_drops_malformed_lines_and_says_nothing(storage: LocalStorage) -> None:
    rows = with_bad({2: "C3,Gold,3,EXTRA", 6: "C7,Gold,7,x,y", 9: "C10,Gold,10,z"})
    storage.write_bytes("u/f.csv", lines_file(rows))
    read = read_upload(storage, "u/f.csv")
    assert read.row_count == 9 and len(read.frame) == 9  # three rows are gone, with no error and no flag


# ---------------------------------------------------------------------------
# Acceptance: three malformed lines are not_ready with X_PARSER_ROW_LOSS, naming the lines
# ---------------------------------------------------------------------------
def test_three_malformed_lines_are_not_ready_and_named(storage: LocalStorage) -> None:
    rows = with_bad({2: "C3,Gold,3,EXTRA", 6: "C7,Gold,7,x,y", 9: "C10,Gold,10,z"})
    storage.write_bytes("u/f.csv", lines_file(rows))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.verdict == "not_ready" and report.code == PARSER_LOSS_CODE == "X_PARSER_ROW_LOSS"
    assert report.lines == (4, 8, 11)  # the header is line 1; the first data row is line 2
    assert (report.records, report.rows_read, report.rows_dropped) == (12, 9, 3)
    assert (report.lines_more, report.unexplained, report.header_fields) == (0, 0, 3)
    assert "lines 4, 8 and 11 have more values than the 3 columns" in report.message
    assert report.message.startswith("3 rows of this file were left out")
    assert "stray comma" in report.fix


def test_a_clean_file_is_ready(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", lines_file(GOOD))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.verdict == "ready" and report.code is None
    assert (report.records, report.rows_read, report.rows_dropped, report.lines) == (12, 12, 0, ())
    assert report.message == report.fix == ""


def test_one_lost_row_is_worded_in_the_singular(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", lines_file(with_bad({4: "C5,Gold,5,EXTRA"})))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.lines == (6,)
    assert report.message.startswith("1 row of this file was left out")
    assert "line 6 has more values" in report.message


def test_the_lines_named_are_physical_lines_even_after_a_quoted_line_break(storage: LocalStorage) -> None:
    rows = ['C1,"Gold\nPlus",1', "C2,Silver,2", "C3,Gold,3,EXTRA", "C4,Gold,4"]
    storage.write_bytes("u/f.csv", lines_file(rows))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.lines == (5,)  # header 1, row 1 spans lines 2-3, row 2 is line 4, the bad row is line 5
    assert report.rows_dropped == 1


def test_short_lines_are_filled_not_dropped_so_they_are_not_loss(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", lines_file(with_bad({3: "C4,Gold", 5: "C6"})))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.verdict == "ready"
    assert read_upload(storage, "u/f.csv").row_count == 12


def test_blank_and_white_space_lines_are_not_records(storage: LocalStorage) -> None:
    rows = [GOOD[0], "", GOOD[1], "   ", GOOD[2], "\t"]
    storage.write_bytes("u/f.csv", lines_file(rows))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.verdict == "ready" and report.records == report.rows_read == 3


def test_a_semicolon_file_with_a_bad_line_is_checked_with_its_own_delimiter(storage: LocalStorage) -> None:
    rows = [row.replace(",", ";") for row in GOOD]
    rows[11] = "C12;Gold;12;5"
    storage.write_bytes("u/f.csv", lines_file(rows, header="id;plan;spend"))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.lines == (13,) and report.verdict == "not_ready"


def test_many_bad_lines_name_the_first_twenty_and_count_the_rest(storage: LocalStorage) -> None:
    rows = [f"C{i},Gold,{i},EXTRA" if i % 2 == 0 else f"C{i},Gold,{i}" for i in range(1, 121)]
    storage.write_bytes("u/f.csv", lines_file(rows))
    report = check_parser_loss(storage, "u/f.csv")
    assert len(report.lines) == MAX_LINES_NAMED == 20
    assert report.lines[:3] == (3, 5, 7)
    assert report.lines_more == 60 - 20 and report.rows_dropped == 60
    assert "and 40 more" in report.message


def test_the_report_holds_counts_and_line_numbers_never_a_cell(storage: LocalStorage) -> None:
    secret = "jane.roe@example.test"
    rows = with_bad({3: f"C4,{secret},4,{secret}"})
    storage.write_bytes("u/f.csv", lines_file(rows))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.rows_dropped == 1
    assert secret not in report.model_dump_json() and "C4" not in report.model_dump_json()


def test_a_read_the_caller_already_made_is_not_repeated(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", lines_file(with_bad({1: "C2,Gold,2,EXTRA"})))
    read = read_upload(storage, "u/f.csv")
    storage.write_bytes("u/f.csv", lines_file(GOOD))  # would read clean; the caller's read decides rows_read
    report = check_parser_loss(storage, "u/f.csv", read=read)
    assert report.rows_read == 11 and report.rows_dropped == 1


def test_a_parquet_file_has_no_lines_to_lose(storage: LocalStorage) -> None:
    path = storage.local_path("u/f.parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"id": ["a", "b"], "n": [1, 2]}).to_parquet(path)
    report = check_parser_loss(storage, "u/f.parquet")
    assert report.verdict == "ready" and report.rows_read == 2


@pytest.mark.parametrize("seed", range(8))
def test_the_dropped_count_and_the_lines_match_what_was_injected(storage: LocalStorage, seed: int) -> None:
    rng = random.Random(seed)
    total = rng.randint(30, 200)
    bad = sorted(rng.sample(range(total), rng.randint(0, 12)))
    rows = [f"C{i},Gold,{i},EXTRA" if i in bad else f"C{i},Gold,{i}" for i in range(total)]
    storage.write_bytes("u/f.csv", lines_file(rows))
    report = check_parser_loss(storage, "u/f.csv")
    assert report.rows_dropped == len(bad)
    assert report.lines == tuple(position + 2 for position in bad)[:MAX_LINES_NAMED]
    assert (report.verdict == "ready") == (not bad)
    assert read_upload(storage, "u/f.csv").row_count == total - len(bad)
