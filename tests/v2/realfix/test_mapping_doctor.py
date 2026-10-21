"""The Data Doctor on messy files: refusals in plain words, profile checks, and the asking route.

Inline bytes only. The route tests need FastAPI (the CI image has it; the bare 3.11 venv does not).
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.engines import doctor


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(doctor.router)
    return TestClient(app)


def _rows(n: int = 120) -> list[str]:
    return [f"C{i % 30},2024-03-{i % 28 + 1:02d}T10:00:00,{10 + i}.50" for i in range(n)]


def _codes(report: doctor.ProfileReport) -> dict[str, str | None]:
    return {p.code: p.column for p in report.problems}


# --- reading ------------------------------------------------------------------------------------------
def test_reading_refuses_what_it_would_get_wrong_and_names_it() -> None:
    body = "cust,when,amount\n" + "\n".join(_rows(5)) + "\n"
    with pytest.raises(ValueError, match="same header: 'cust'"):
        doctor.read_csv_bytes(body.replace("amount", "cust", 1).encode())
    with pytest.raises(ValueError, match="Excel workbook"):
        doctor.read_csv_bytes(b"PK\x03\x04" + b"\x00" * 20)
    with pytest.raises(ValueError, match="JSON"):
        doctor.read_csv_bytes(b'{"person": "a", "time": 0}\n{"person": "b", "time": 1}\n')
    with pytest.raises(ValueError, match="delimiter=';'"):
        doctor.read_csv_bytes(body.replace(",", ";").encode())
    # A quote opened in one row's text and closed three rows later swallows the rows in between.
    lines = body.splitlines()
    lines[1] = lines[1].replace("C0,", 'C0,"', 1).replace("T10:00:00,", "T10:00:00 note,", 1)
    lines[4] = lines[4] + '"'
    with pytest.raises(ValueError, match="stray quote"):
        doctor.read_csv_bytes(("\n".join(lines) + "\n").encode())


def test_gzip_and_utf16_are_read_and_latin1_says_so() -> None:
    body = "cust,when,amount\n" + "\n".join(_rows(5)) + "\n"
    assert len(doctor.read_csv_bytes(gzip.compress(body.encode()))) == 5
    assert len(doctor.read_csv_bytes(body.encode("utf-16"))) == 5
    frame = doctor.read_csv_bytes("Счёт,x\n1,2\n".encode("cp1251"))
    assert frame.attrs["decoded_as"] == "latin-1"
    assert "not_utf8" in _codes(doctor.profile_frame(frame, use_v1=False))


# --- profile checks ----------------------------------------------------------------------------------
def test_profile_flags_ids_that_would_merge_split_or_lose_people() -> None:
    cids = ["0", "unknown"] + [f"00{i}" for i in range(100, 160)] + ["4.56789E+11"] * 3
    cids += ["Ann@x.com", "ann@x.com"] + [f"c{i}" for i in range(40)] + ["TOTAL"]
    lines = ["customer_id,amount"] + [f"{c},{i + 1}" for i, c in enumerate(cids)]
    report = doctor.profile_frame(doctor.read_csv_bytes("\n".join(lines).encode()), use_v1=False)
    codes = _codes(report)
    for code in ("placeholder_ids", "scientific_ids", "leading_zero_ids", "mixed_case_ids", "totals_row"):
        assert codes.get(code) == "customer_id", code
    assert "4.56789E+11" not in json.dumps(report.model_dump(mode="json"))  # never echoes a cell


def test_profile_flags_dates_numbers_currencies_and_sentinels() -> None:
    lines = ["order_date,price,currency,pdays,qty"]
    for i in range(120):
        price = f"{(i % 9) + 1}.{i % 10}50"  # 1.050, 2.150 ...: decimal point or thousands?
        lines.append(
            f"0{i % 9 + 1}/12/2010,{price},{'GBP' if i % 2 else 'BRL'},{999 if i % 10 else i % 7},{i % 5}"
        )
    lines.append("01/01/1900,1.000,GBP,3,1")
    report = doctor.profile_frame(doctor.read_csv_bytes("\n".join(lines).encode()), use_v1=False)
    codes = _codes(report)
    assert codes["ambiguous_day_month"] == "order_date"
    assert codes["placeholder_dates"] == "order_date"
    assert codes["ambiguous_separators"] == "price"
    assert codes["mixed_currency"] == "currency"
    assert codes["sentinel_value"] == "pdays"
    assert "sentinel_value" not in [p.code for p in report.problems if p.column == "qty"]
    (dates,) = report.temporal_integrity.date_columns
    assert dates.ambiguous_day_month == 120 and dates.placeholder_dates == 1


def test_leak_check_names_columns_measured_after_the_contact() -> None:
    lines = ["client,duration,status_code,y"] + [f"c{i},{(i * 37) % 600},{i % 3},{i % 2}" for i in range(80)]
    frame = doctor.read_csv_bytes("\n".join(lines).encode())
    suspects = {
        s.column: s for s in doctor.profile_frame(frame, target="y", use_v1=False).leak_check.suspects
    }
    assert "measured during or after the contact" in suspects["duration"].reason
    told = doctor.profile_frame(frame, target="y", use_v1=False, known_after=["status_code"])
    assert {s.column: s.score for s in told.leak_check.suspects}["status_code"] == 1.0


def test_clean_file_still_scores_full_marks() -> None:
    lines = ["order_id,customer_id,order_date,amount"]
    lines += [f"O{i},C{i % 40},2026-03-{(i % 28) + 1:02d},{10 + i}" for i in range(200)]
    frame = doctor.read_csv_bytes("\n".join(lines).encode())
    report = doctor.profile_frame(frame, snapshot=None, use_v1=False)
    assert report.readiness_score == 100, report.problems


# --- the route asks ---------------------------------------------------------------------------------
def _post(client: TestClient, csv: bytes, mapping: dict[str, Any], **fields: str) -> Any:
    return client.post(
        "/doctor/map-events",
        files={"file": ("orders.csv", csv, "text/csv")},
        data={"mapping": json.dumps(mapping), **fields},
    )


def test_map_events_asks_plain_questions_then_maps_with_the_answers(client: TestClient) -> None:
    csv = b"cust,when,total\nA,01/12/2010 08:26,2.550\nB,01/12/2010 09:00,350\n"
    mapping = {
        "customer_id": "cust",
        "timestamp": "when",
        "timestamp_format": "%m/%d/%Y %H:%M",
        "event_type": "purchase",
        "value_column": "total",
    }
    asked = _post(client, csv, mapping)
    assert asked.status_code == 422
    detail = asked.json()["detail"]
    assert {q["option"] for q in detail["questions"]} == {"timezone", "date_order", "decimal"}
    assert "day first" in detail["message"]
    answered = {**mapping, "timestamp_format": "%d/%m/%Y %H:%M", "date_order": "day_first"}
    answered |= {"timezone": "Europe/London", "decimal": ","}
    ok = _post(client, csv, answered)
    assert ok.status_code == 200, ok.text
    assert [e["value"] for e in ok.json()["events"]] == [2550.0, 350.0]
    assert ok.json()["events"][0]["ts"].startswith("2010-12-01")
    # ask=false keeps the library defaults (UTC, repeats kept) and reports what it cannot read.
    lib = _post(client, csv, mapping, ask="false")
    assert lib.status_code == 200 and lib.json()["problem_counts"] == {"ambiguous_number": 1}


def test_map_events_reports_rows_after_the_snapshot_and_says_what_it_inferred(client: TestClient) -> None:
    csv = b'cust,when,total\nA,2024-01-01T10:00:00,"2,50"\nB,2068-01-01T10:00:00,4\n'
    mapping = {
        "customer_id": "cust",
        "timestamp": "when",
        "timezone": "UTC",
        "event_type": "purchase",
        "value_column": "total",
    }
    out = _post(client, csv, mapping, snapshot="2026-10-01T00:00:00+00:00")
    assert out.status_code == 200, out.text
    body = out.json()
    assert [e["value"] for e in body["events"]] == [2.5]
    assert body["problem_counts"] == {"bad_timestamp": 1}
    assert any("decimal comma" in note for note in body["inferred"])


def test_a_multi_line_comment_with_commas_is_not_a_stray_quote() -> None:
    # A review whose text runs over lines with a few commas (Olist's reviews file has thousands).
    body = (
        "review_id,order_id,score,comment,created\n"
        'r1,o1,5,"Happy with it.\nOn time, well packed, with the invoice, box and manual.\nGreat.",2018-01-01\n'
        "r2,o2,4,fine,2018-01-02\n"
    )
    frame = doctor.read_csv_bytes(body.encode())
    assert len(frame) == 2 and frame["comment"].iloc[0].count("\n") == 2
