"""Key-like columns keep their leading zeros (Plan O M240, DEC-1810 (c)): `0000123` stays `0000123`.

A CSV is read in chunks, each typed on its own, so an ID that looks like a number was a number by the time it was
a cell. `read_upload(keep_leading_zeros=True)` and `text_columns=` read such columns as text; the default read is
what it always was.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.dataset_recipe import consistent_types
from engine.agent.recipe import run_recipe
from engine.intake.config import IntakeConfig
from engine.intake.run import intake_file
from engine.stages.ingest import leading_zero_columns, read_upload
from engine.storage import LocalStorage


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "store")


MONTH_1 = (
    "Customer report,,\n"
    "customer_id,plan,spend,pin\n"
    '0000123,Gold,"₹1,200",110001\n'
    "0000124,Silver,300,400001\n"
    "0000125,Gold,45,560001\n"
    "0000126,Gold,10,600001\n"
)
MONTH_2 = (
    "customer_id,plan,spend,pin\n"
    '0000125,Gold,"₹2,000",560001\n'
    "0000199,Silver,15,110001\n"
    "0000123,Gold,99,110001\n"
)
STEPS = (
    RecipeStep(
        order=1,
        kind=RecipeStepKind.PARSE_NUMBER,
        column="spend",
        params={"decimal": ".", "percent_to_fraction": True},
    ),
)


def prepare(frame: pd.DataFrame) -> pd.DataFrame:
    return run_recipe(
        frame,
        STEPS,
        upload_id="u1",
        primary_key="customer_id",
        target=None,
        levels=(AgentLevel.CLEAN,),
        max_failure_pct=5.0,
    ).frame


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------
def test_today_a_chunked_read_turns_an_id_into_a_number_and_the_zeros_are_gone(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", MONTH_2.encode())
    frame = read_upload(storage, "u/f.csv").frame
    assert frame["customer_id"].tolist() == [125, 199, 123]  # the default read is unchanged (switch off)


def test_keep_leading_zeros_reads_the_id_as_text(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", MONTH_2.encode())
    result = read_upload(storage, "u/f.csv", keep_leading_zeros=True)
    assert result.frame["customer_id"].tolist() == ["0000125", "0000199", "0000123"]
    assert result.row_count == 3 and result.fingerprint is not None


def test_only_columns_that_show_a_leading_zero_become_text(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", b"id,n,x,flag\n007,10,0.5,0\n008,20,0.25,1\n")
    frame = read_upload(storage, "u/f.csv", keep_leading_zeros=True).frame
    assert frame["id"].tolist() == ["007", "008"]
    assert frame["n"].dtype.kind == "i" and frame["x"].dtype.kind == "f" and frame["flag"].dtype.kind == "i"


def test_zero_and_a_decimal_that_starts_with_zero_are_not_leading_zeros() -> None:
    assert leading_zero_columns("a,b,c\n0,0.5,10\n0.25,0,5\n", ",") == ()
    assert leading_zero_columns("a,b,c\n00,0.5,010\n", ",") == ("a", "c")


def test_a_named_column_is_text_even_when_the_first_rows_show_no_zero(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", b"account,n\n123456,1\n234567,2\n")
    assert read_upload(storage, "u/f.csv").frame["account"].tolist() == [123456, 234567]
    frame = read_upload(storage, "u/f.csv", text_columns=("account",)).frame
    assert frame["account"].tolist() == ["123456", "234567"] and frame["n"].tolist() == [1, 2]


def test_a_text_column_that_is_not_in_the_file_is_ignored(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", b"a,b\n1,2\n")
    assert read_upload(storage, "u/f.csv", text_columns=("nope",)).frame["a"].tolist() == [1]


def test_a_semicolon_file_and_a_bounded_preview_keep_zeros_too(storage: LocalStorage) -> None:
    rows = "\n".join(f"{index:07d};{index}" for index in range(1, 40))
    storage.write_bytes("u/f.csv", f"id;n\n{rows}\n".encode())
    preview = read_upload(storage, "u/f.csv", max_rows=5, keep_leading_zeros=True)
    assert preview.frame["id"].tolist()[:2] == ["0000001", "0000002"]


def test_empty_cells_in_a_text_id_stay_empty(storage: LocalStorage) -> None:
    storage.write_bytes("u/f.csv", b"id,n\n007,1\n,2\n009,3\n")
    frame = read_upload(storage, "u/f.csv", keep_leading_zeros=True).frame
    assert frame["id"].tolist()[0] == "007" and pd.isna(frame["id"].iloc[1])


def test_a_leading_zero_read_of_parquet_is_a_no_op(storage: LocalStorage) -> None:
    path = storage.local_path("u/f.parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"id": ["007", "008"]}).to_parquet(path)
    frame = read_upload(storage, "u/f.parquet", keep_leading_zeros=True, text_columns=("id",)).frame
    assert frame["id"].tolist() == ["007", "008"]


# ---------------------------------------------------------------------------
# Acceptance: through intake, recipe and replay
# ---------------------------------------------------------------------------
def test_0000123_stays_0000123_through_intake_recipe_and_replay(storage: LocalStorage) -> None:
    # Month 1: a title row above the header, so intake rewrites the file.
    storage.write_bytes("u1/source.csv", MONTH_1.encode())
    result = intake_file(
        storage,
        received_key="u1/source.csv",
        received_format="csv",
        csv_key="u1/source.csv",
        original_key="u1/original.csv",
        config=IntakeConfig(),
    )
    assert result.report.applied and "0000123" in storage.read_text("u1/source.csv")

    first = read_upload(storage, result.source_key, keep_leading_zeros=True).frame
    assert first["customer_id"].tolist() == ["0000123", "0000124", "0000125", "0000126"]
    assert first["pin"].tolist() == [110001, 400001, 560001, 600001]  # no zero shown, so still a number

    # The recipe: the ID is untouched, and a saved prepared file reads back with its zeros.
    prepared = prepare(first)
    assert prepared["customer_id"].tolist() == ["0000123", "0000124", "0000125", "0000126"]
    assert prepared["spend"].tolist() == pytest.approx([1200.0, 300.0, 45.0, 10.0])
    path = storage.local_path("u1/prepared.parquet")
    consistent_types(prepared).to_parquet(path, index=False)
    assert read_upload(storage, "u1/prepared.parquet").frame["customer_id"].tolist()[0] == "0000123"

    # Replay: next month's file, the same steps, the same IDs, so the two months still join.
    storage.write_bytes("u2/source.csv", MONTH_2.encode())
    second = prepare(read_upload(storage, "u2/source.csv", keep_leading_zeros=True).frame)
    assert second["customer_id"].tolist() == ["0000125", "0000199", "0000123"]
    joined = prepared.merge(second, on="customer_id", suffixes=("_m1", "_m2"))
    assert sorted(joined["customer_id"]) == ["0000123", "0000125"]
    again = prepare(read_upload(storage, "u2/source.csv", keep_leading_zeros=True).frame)
    pd.testing.assert_frame_equal(second, again)  # a replay gives the same rows


def test_a_workbook_text_id_keeps_its_zeros_through_intake(storage: LocalStorage) -> None:
    book = Workbook()
    sheet = book.active
    sheet.append(["customer_id", "spend"])
    sheet.append(["0000123", 10])
    sheet.append(["0000124", 20])
    buffer = io.BytesIO()
    book.save(buffer)
    storage.write_bytes("u/original.xlsx", buffer.getvalue())
    result = intake_file(
        storage,
        received_key="u/original.xlsx",
        received_format="xlsx",
        csv_key="u/source.csv",
        original_key="u/original.xlsx",
        config=IntakeConfig(),
    )
    frame = read_upload(storage, result.source_key, keep_leading_zeros=True).frame
    assert frame["customer_id"].tolist() == ["0000123", "0000124"]
