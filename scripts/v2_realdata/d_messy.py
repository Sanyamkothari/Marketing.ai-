"""Family D, stage 2: deliberately messy files through the Data Doctor, the mapping and the loaders.

Every case starts from **real rows** (Online Retail I unless stated; bank marketing, Starbucks
rewards, Olist, KDD Cup 98, hotel bookings and health insurance where the mess is native to them),
damages them the way real exports are damaged, and feeds them to what a customer can use without
code: the Data Doctor's two routes (`POST /v2/engines/doctor/profile` and `/map-events`, through
FastAPI's TestClient), plus `map_frame` and the built-in loader where a customer would write a line
of pandas first. Each result is compared row by row with the clean truth (the same rows, cleanly
mapped), so a wrong answer that raises no problem is caught:

- `ok`: every row came out as in the clean truth.
- `reported`: some rows were left out and each one is in `problems`; nothing mapped is wrong.
- `refused`: the route or the mapping refused the whole file with a message (good or misleading:
  the notes say which).
- `silent_wrong`: rows came out with a different customer, time or amount than the truth (or extra
  rows appeared) and **no problem said so**. The most important outcome.
- `cannot`: the shape cannot be expressed without code (wide tables, split dates, nested JSON).
- `crash`: an unexpected exception or a 500.

Run (from the worktree root):

    python3 -P scripts/v2_realdata/d_messy.py            # every case
    python3 -P scripts/v2_realdata/d_messy.py --only T1,I2

Results: /home/user/datasets/_results/d/messy/cases.json (and one line per case on stdout).
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import time
import traceback
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from d_common import (  # noqa: E402
    DATASETS,
    command_line,
    doctor_client,
    env_info,
    peak_rss_mb,
    post_file,
    write_result,
)

from api.v2.engines.doctor import profile_frame, read_csv_bytes  # noqa: E402
from autonomy.contracts import Channel, EventType  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.loaders import online_retail_log_events  # noqa: E402
from autonomy.data.mapping import EventMapping, MappingError, map_frame, problem_counts  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore  # noqa: E402

N_ROWS = 2000
CLIENT = doctor_client()

BASE_MAPPING = EventMapping(
    customer_id="CustomerID",
    timestamp="InvoiceDate",
    timestamp_format="%m/%d/%Y %H:%M",
    timezone="Europe/London",
    event_type=EventType.PURCHASE,
    quantity_column="Quantity",
    unit_price_column="UnitPrice",
    item_column="StockCode",
)


# --------------------------------------------------------------------------- the clean truth


def base_frame() -> pd.DataFrame:
    """The first `N_ROWS` sale lines of Online Retail I with a customer id, read as text."""
    path = DATASETS / "online_retail_i" / "online-retail-dataset.csv"
    raw = pd.read_csv(path, dtype=str, nrows=20000, encoding="latin-1")
    ok = raw.CustomerID.notna() & (raw.Quantity.astype(float) > 0) & (raw.UnitPrice.astype(float) > 0)
    return raw[ok].head(N_ROWS).reset_index(drop=True)


BASE = base_frame()


def to_csv(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8")


@dataclass
class Truth:
    customer: dict[str, str]
    ts: dict[str, datetime]
    value: dict[str, float]

    @property
    def total(self) -> float:
        return float(sum(self.value.values()))


def truth_of(frame: pd.DataFrame, mapping: EventMapping, source: str = "csv") -> Truth:
    events, problems = map_frame(frame, mapping, source)
    assert not problems, problem_counts(problems)
    return Truth(
        {e.event_id: e.customer_id for e in events},
        {e.event_id: e.ts for e in events},
        {e.event_id: float(e.value or 0.0) for e in events},
    )


TRUTH = truth_of(BASE, BASE_MAPPING)


def compare(events: list[dict[str, Any]], problems: dict[str, int], truth: Truth) -> dict[str, Any]:
    """Row-by-row against the clean truth. `events` are dicts (route JSON or model_dump)."""
    wrong = {"customer": 0, "ts": 0, "value": 0}
    unknown = 0
    for e in events:
        eid = e["event_id"]
        if eid not in truth.customer:
            unknown += 1
            continue
        if e["customer_id"] != truth.customer[eid]:
            wrong["customer"] += 1
        ts = e["ts"] if isinstance(e["ts"], datetime) else datetime.fromisoformat(e["ts"])
        if ts != truth.ts[eid]:
            wrong["ts"] += 1
        if abs(float(e.get("value") or 0.0) - truth.value[eid]) > 1e-6:
            wrong["value"] += 1
    total = float(sum(float(e.get("value") or 0.0) for e in events))
    n_wrong = sum(wrong.values()) + unknown
    left_out = sum(problems.values())
    if n_wrong:
        verdict = "silent_wrong"
    elif left_out:
        verdict = "reported"
    elif len(events) == len(truth.customer):
        verdict = "ok"
    else:
        verdict = "silent_wrong"  # rows vanished with no problem
    return {
        "verdict": verdict,
        "rows_truth": len(truth.customer),
        "mapped": len(events),
        "problems": problems,
        "wrong_fields": wrong,
        "rows_not_in_truth": unknown,
        "customers": len({e["customer_id"] for e in events}),
        "customers_truth": len(set(truth.customer.values())),
        "value_total": round(total, 2),
        "value_truth": round(truth.total, 2),
        "value_error_pct": round(100 * (total - truth.total) / truth.total, 3) if truth.total else None,
    }


def compare_content(events: list[dict[str, Any]], problems: dict[str, int], truth: Truth) -> dict[str, Any]:
    """Compare by what the rows say, not by event id: for files whose row positions moved (a
    repeated header, merged rows), where positional ids no longer line up with the truth."""
    from collections import Counter

    def key(cid: str, ts: Any, v: float) -> tuple[str, str, float]:
        t = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
        return (cid, t.isoformat(), round(float(v), 6))

    want = Counter(key(truth.customer[k], truth.ts[k], truth.value[k]) for k in truth.customer)
    got = Counter(key(e["customer_id"], e["ts"], float(e.get("value") or 0.0)) for e in events)
    matched = sum((want & got).values())
    extra = sum((got - want).values())
    missing = sum((want - got).values())
    left_out = sum(problems.values())
    verdict = (
        "ok"
        if not extra and not missing
        else ("reported" if not extra and missing <= left_out else "silent_wrong")
    )
    total = float(sum(float(e.get("value") or 0.0) for e in events))
    return {
        "verdict": verdict,
        "compared_by": "content",
        "rows_truth": len(truth.customer),
        "mapped": len(events),
        "matched": matched,
        "extra_rows": extra,
        "missing_rows": missing,
        "problems": problems,
        "value_error_pct": round(100 * (total - truth.total) / truth.total, 3),
    }


def route_map(
    data: bytes, mapping: EventMapping, fields: dict[str, str] | None = None, filename: str = "upload.csv"
) -> tuple[int, Any]:
    form = {"mapping": mapping.model_dump_json(), **(fields or {})}
    resp = CLIENT.post(
        "/v2/engines/doctor/map-events", files={"file": (filename, data, "text/csv")}, data=form
    )
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:300]
    return resp.status_code, body


def route_profile(
    data: bytes, fields: dict[str, str] | None = None, filename: str = "upload.csv"
) -> dict[str, Any]:
    f = {"use_v1": "false", **(fields or {})}
    return post_file(CLIENT, "profile", data, filename, f)


def route_summary(data: bytes, mapping: EventMapping, filename: str = "upload.csv") -> dict[str, Any]:
    status, body = route_map(data, mapping, filename=filename)
    if status != 200:
        return {"verdict": "refused", "status": status, "detail": str(body.get("detail"))[:400]}
    return {
        "status": status,
        "rows": body["rows"],
        "mapped": body["mapped"],
        "problem_counts": body["problem_counts"],
        "first_problems": [
            f"{p['code']}:{p.get('column')}:{p['message'][:70]}" for p in body["problems"][:3]
        ],
    }


def via_route(
    data: bytes,
    mapping: EventMapping,
    truth: Truth,
    fields: dict[str, str] | None = None,
    filename: str = "upload.csv",
    by_content: bool = False,
) -> dict[str, Any]:
    status, body = route_map(data, mapping, fields, filename)
    if status != 200:
        return {
            "verdict": "crash" if status >= 500 else "refused",
            "status": status,
            "detail": str(body.get("detail") if isinstance(body, dict) else body)[:500],
        }
    out = (compare_content if by_content else compare)(body["events"], body["problem_counts"], truth)
    out["status"] = status
    return out


def via_frame(
    frame: pd.DataFrame, mapping: EventMapping, truth: Truth, source: str = "csv"
) -> dict[str, Any]:
    try:
        events, problems = map_frame(frame, mapping, source)
    except MappingError as exc:
        return {"verdict": "refused", "detail": str(exc)[:500]}
    return compare([e.model_dump() for e in events], problem_counts(problems), truth)


def sample_problems(
    data: bytes, mapping: EventMapping, n: int = 3, fields: dict[str, str] | None = None
) -> list[str]:
    status, body = route_map(data, mapping, fields)
    if status != 200:
        return []
    return [f"{p['code']}:{p.get('column')}:{p['message'][:80]}" for p in body["problems"][:n]]


# --------------------------------------------------------------------------- the cases

CASES: dict[str, tuple[str, Callable[[], dict[str, Any]]]] = {}


def case(code: str, title: str) -> Callable[[Callable[[], dict[str, Any]]], Callable[[], dict[str, Any]]]:
    def wrap(fn: Callable[[], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
        CASES[code] = (title, fn)
        return fn

    return wrap


# ---- F: file formats and encodings


@case("F0", "Clean control: the base rows as a plain UTF-8 CSV")
def f0() -> dict[str, Any]:
    data = to_csv(BASE)
    return {"profile": route_profile(data), "map_events": via_route(data, BASE_MAPPING, TRUTH)}


@case("F1", "Semicolon-separated CSV (bank marketing, as published), delimiter not given / given")
def f1() -> dict[str, Any]:
    data = (DATASETS / "bank_marketing" / "bank-additional-full.csv").read_bytes()
    return {
        "profile_no_delimiter": route_profile(data),
        "profile_delimiter_semicolon": route_profile(data, {"delimiter": ";"}),
        "map_events_no_delimiter": route_map(
            data[:200000], EventMapping(customer_id="age", timestamp="month", event_type=EventType.VISIT)
        )[1],
    }


@case("F2", "Excel workbook (.xlsx, two sheets) built from the base rows")
def f2() -> dict[str, Any]:
    bio = io.BytesIO()
    with pd.ExcelWriter(bio) as w:
        BASE.iloc[:1000].to_excel(w, sheet_name="Year 2010", index=False)
        BASE.iloc[1000:].to_excel(w, sheet_name="Year 2011", index=False)
    data = bio.getvalue()
    customer = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str)  # what a customer's pandas sees
    first_only = next(iter(customer.values()))
    return {
        "profile": route_profile(data, filename="online_retail.xlsx"),
        "map_events": via_route(data, BASE_MAPPING, TRUTH, filename="online_retail.xlsx"),
        "customer_read_excel_default_first_sheet": via_frame(first_only, BASE_MAPPING, TRUTH),
        "sheets": list(customer),
        "note": "pd.read_excel without sheet_name reads only the first sheet: half the rows, no warning",
    }


@case("F3", "JSON lines (Starbucks rewards transcript, nested `value` dict)")
def f3() -> dict[str, Any]:
    path = DATASETS / "starbucks_rewards"
    files = sorted(p.name for p in path.iterdir())
    src = next(p for p in path.iterdir() if "transcript" in p.name)
    raw = src.read_bytes()
    if src.suffix == ".gz":
        raw = gzip.decompress(raw)
    elif src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            raw = z.read(z.namelist()[0])
    head = b"\n".join(raw.splitlines()[:2000]) + b"\n"
    frame = pd.read_json(io.BytesIO(head), lines=True)
    mapping = EventMapping(
        customer_id="person",
        timestamp="time",
        event_type_column="event",
        event_type_values={"transaction": EventType.PURCHASE, "offer received": EventType.SEND},
        value_column="value",
    )
    try:
        events, problems = map_frame(frame, mapping, "jsonl")
        customer_path = {
            "mapped": len(events),
            "problems": problem_counts(problems),
            "first_problems": [f"{p.code}:{p.column}:{p.message[:70]}" for p in problems[:3]],
        }
    except MappingError as exc:
        customer_path = {"refused": str(exc)[:300]}
    return {
        "files": files,
        "profile": route_profile(head, filename=src.name),
        "map_events": route_summary(head, mapping, filename=src.name),
        "customer_pd_read_json_then_map_frame": customer_path,
        "value_column_cells": [str(v)[:40] for v in frame["value"].head(3)],
        "time_column": "hours since the start of the test, not a date",
    }


@case("F4", "Gzipped CSV uploaded as is")
def f4() -> dict[str, Any]:
    data = gzip.compress(to_csv(BASE))
    return {
        "profile": route_profile(data, filename="orders.csv.gz"),
        "map_events": via_route(data, BASE_MAPPING, TRUTH, filename="orders.csv.gz"),
    }


@case("F5", "UTF-16 CSV (Excel's 'Unicode text' export)")
def f5() -> dict[str, Any]:
    data = BASE.to_csv(index=False).encode("utf-16")
    return {"profile": route_profile(data), "map_events": via_route(data, BASE_MAPPING, TRUTH)}


RU = {
    "InvoiceNo": "Счёт",
    "StockCode": "Артикул",
    "Description": "Описание",
    "Quantity": "Количество",
    "InvoiceDate": "Дата",
    "UnitPrice": "Цена",
    "CustomerID": "Клиент",
    "Country": "Страна",
}
RU_MAPPING = BASE_MAPPING.model_copy(
    update={
        "customer_id": "Клиент",
        "timestamp": "Дата",
        "quantity_column": "Количество",
        "unit_price_column": "Цена",
        "item_column": "Артикул",
    }
)


@case("F6", "Russian headers, UTF-8 and Windows-1251 (cp1251) encodings")
def f6() -> dict[str, Any]:
    ru = BASE.rename(columns=RU)
    ru["Описание"] = "Подарочный набор"  # Cyrillic values too
    utf8 = ru.to_csv(index=False).encode("utf-8")
    cp = ru.to_csv(index=False).encode("cp1251")
    return {
        "utf8_map_events": via_route(utf8, RU_MAPPING, TRUTH),
        "cp1251_map_events": via_route(cp, RU_MAPPING, TRUTH),
        "cp1251_profile_columns": route_profile(cp).get("problems"),
        "cp1251_headers_as_read": list(read_csv_bytes(cp).columns)[:4],
        "category_mapping_cp1251": _cp1251_category(cp),
    }


def _cp1251_category(cp: bytes) -> dict[str, Any]:
    """Map the cp1251 file with its headers as the Doctor decodes them, Description as category."""
    frame = read_csv_bytes(cp)
    cols = list(frame.columns)
    m = BASE_MAPPING.model_copy(
        update={
            "customer_id": cols[6],
            "timestamp": cols[4],
            "quantity_column": cols[3],
            "unit_price_column": cols[5],
            "item_column": cols[1],
            "category_column": cols[2],
        }
    )
    events, problems = map_frame(frame, m, "csv")
    return {
        "mapped": len(events),
        "problems": problem_counts(problems),
        "category_stored": events[0].category if events else None,
        "category_written": "Подарочный набор",
    }


@case("F7", "Chinese headers (UTF-8 with BOM, CRLF line ends)")
def f7() -> dict[str, Any]:
    zh = {
        "CustomerID": "客户编号",
        "InvoiceDate": "日期",
        "Quantity": "数量",
        "UnitPrice": "单价",
        "StockCode": "商品",
    }
    frame = BASE.rename(columns=zh)
    data = frame.to_csv(index=False, lineterminator="\r\n").encode("utf-8-sig")
    m = BASE_MAPPING.model_copy(
        update={
            "customer_id": "客户编号",
            "timestamp": "日期",
            "quantity_column": "数量",
            "unit_price_column": "单价",
            "item_column": "商品",
        }
    )
    return {"map_events": via_route(data, m, TRUTH)}


# ---- I: identities


@case("I1", "Blank customer ids on 10% of rows")
def i1() -> dict[str, Any]:
    f = BASE.copy()
    f.loc[f.index % 10 == 0, "CustomerID"] = ""
    data = to_csv(f)
    return {"map_events": via_route(data, BASE_MAPPING, TRUTH), "profile": route_profile(data)}


@case("I2", "Placeholder ids for guests: '0', 'unknown', '-', 'guest', 'N/A', 'NULL', '#N/A' on 14% of rows")
def i2() -> dict[str, Any]:
    f = BASE.copy()
    words = ["0", "unknown", "-", "guest", "N/A", "NULL", "#N/A"]
    idx = f.index[f.index % 50 < 7]
    f.loc[idx, "CustomerID"] = [words[i % 50] for i in idx]
    data = to_csv(f)
    _status, body = route_map(data, BASE_MAPPING)
    sizes: dict[str, int] = {}
    for e in body["events"]:
        if e["customer_id"] in words:
            sizes[e["customer_id"]] = sizes.get(e["customer_id"], 0) + 1
    out = compare(body["events"], body["problem_counts"], TRUTH)
    out["placeholder_customers_created"] = sizes
    return {"map_events": out, "profile": route_profile(data, {"primary_key": "CustomerID"})}


@case("I3", "Leading-zero ids: orders read as text by the Doctor, consent list read by the customer's pandas")
def i3() -> dict[str, Any]:
    f = BASE.copy()
    f["CustomerID"] = "00" + f.CustomerID.str.replace(".0", "", regex=False)
    orders = to_csv(f)
    _status, body = route_map(orders, BASE_MAPPING)
    order_ids = {e["customer_id"] for e in body["events"]}
    consent = pd.DataFrame({"CustomerID": sorted(order_ids), "ts": "2010-12-01T00:00:00", "channel": "email"})
    consent_frame = pd.read_csv(io.BytesIO(to_csv(consent)))  # pandas' default: ids become integers
    cm = EventMapping(
        customer_id="CustomerID",
        timestamp="ts",
        event_type=EventType.CONSENT_GRANTED,
        channel_column="channel",
    )
    cev, _ = map_frame(consent_frame, cm, "consent.csv")
    consent_ids = {e.customer_id for e in cev}
    return {
        "order_customers": len(order_ids),
        "consent_customers": len(consent_ids),
        "same_ids": len(order_ids & consent_ids),
        "example": [sorted(order_ids)[0], sorted(consent_ids)[0]],
        "verdict": "silent_wrong" if not (order_ids & consent_ids) else "ok",
        "note": "nobody with an order has consent and nobody with consent has an order; no problem raised",
    }


@case("I4", "Ids saved by Excel in scientific notation (12-digit loyalty numbers -> 1.23457E+11)")
def i4() -> dict[str, Any]:
    f = BASE.copy()
    base_ids = sorted(f.CustomerID.unique())
    big = {cid: str(123456700000 + i * 37) for i, cid in enumerate(base_ids)}
    f["CustomerID"] = f.CustomerID.map(big)
    t = truth_of(f, BASE_MAPPING)
    f["CustomerID"] = f.CustomerID.map(lambda s: f"{float(s):.5E}")
    out = via_route(to_csv(f), BASE_MAPPING, t)
    out["distinct_ids_written"] = len(base_ids)
    return {"map_events": out}


@case("I5", "Email as the id with different letter case for the same person")
def i5() -> dict[str, Any]:
    f = BASE.copy()
    ids = {cid: f"shopper{i}@example.com" for i, cid in enumerate(sorted(f.CustomerID.unique()))}
    f["CustomerID"] = f.CustomerID.map(ids)
    t = truth_of(f, BASE_MAPPING)
    f.loc[f.index % 3 == 0, "CustomerID"] = f.loc[f.index % 3 == 0, "CustomerID"].str.upper()
    data = to_csv(f)
    return {
        "map_events": via_route(data, BASE_MAPPING, t),
        "profile_pii": route_profile(data).get("problems"),
    }


# ---- D: duplicates and re-imports


@case("D1", "Exact duplicate rows (every 20th row twice), no row_key")
def d1() -> dict[str, Any]:
    f = pd.concat([BASE, BASE.iloc[::20]]).sort_index(kind="stable").reset_index(drop=True)
    data = to_csv(f)
    _status, body = route_map(data, BASE_MAPPING)
    total = sum(e["value"] for e in body["events"])
    return {
        "verdict": "silent_wrong" if body["mapped"] > len(TRUTH.customer) else "reported",
        "mapped": body["mapped"],
        "rows_truth": len(TRUTH.customer),
        "problems": body["problem_counts"],
        "value_error_pct": round(100 * (total - TRUTH.total) / TRUTH.total, 3),
        "profile": route_profile(data).get("problems"),
    }


@case(
    "D2", "Re-export with new orders at the top (newest first), loaded on top of the first export, no row_key"
)
def d2() -> dict[str, Any]:
    old = BASE.iloc[100:].reset_index(drop=True)  # last week's export
    new = BASE.iloc[:100]  # this week's orders
    reexport = pd.concat([new, old], ignore_index=True)  # newest first, as many tools export
    store = InMemoryEventStore()
    e1, _ = map_frame(read_csv_bytes(to_csv(old)), BASE_MAPPING, "orders.csv")
    store.append(e1)
    e2, _ = map_frame(read_csv_bytes(to_csv(reexport)), BASE_MAPPING, "orders.csv")
    store.append(e2)
    kept = [e.model_dump() for e in store.scan()]
    out = compare_content(kept, {}, TRUTH)
    out.update(
        events_first_load=len(e1),
        events_in_reexport=len(e2),
        events_in_store=len(kept),
        note="ids come from row position: the 100 new orders take ids the store already has and are "
        "dropped as repeats, while the 100 old rows pushed past the end are added a second time",
    )
    return out


@case("D3", "row_key = (InvoiceNo, StockCode): legitimate repeated lines in one invoice")
def d3() -> dict[str, Any]:
    m = BASE_MAPPING.model_copy(update={"row_key": ("InvoiceNo", "StockCode")})
    events, problems = map_frame(BASE, m, "csv")
    lost = len(BASE) - len(events)
    total = sum(float(e.value or 0) for e in events)
    return {
        "verdict": "reported" if lost and problem_counts(problems) else "ok",
        "rows": len(BASE),
        "mapped": len(events),
        "problems": problem_counts(problems),
        "value_error_pct": round(100 * (total - TRUTH.total) / TRUTH.total, 3),
        "note": "reported as duplicate_row, but the lines are real (same item scanned twice); the money is lost",
    }


@case("D4", "Built-in Online Retail loader on a file with exact duplicate lines")
def d4() -> dict[str, Any]:
    f = pd.concat([BASE, BASE.iloc[::20]]).reset_index(drop=True)
    clean = online_retail_log_events(BASE, grain="invoice")
    dup = online_retail_log_events(f, grain="invoice")
    tc = sum(float(e.value or 0) for e in clean.events)
    td = sum(float(e.value or 0) for e in dup.events)
    return {
        "verdict": "silent_wrong" if abs(td - tc) > 1e-6 else "ok",
        "invoice_events_clean": len(clean.events),
        "invoice_events_dup": len(dup.events),
        "problems_dup": problem_counts(dup.problems),
        "value_error_pct": round(100 * (td - tc) / tc, 3),
    }


# ---- N: numbers and money


def _price_variant(
    fmt: Callable[[str], str], rows: Callable[[pd.DataFrame], pd.Index] | None = None
) -> dict[str, Any]:
    f = BASE.copy()
    idx = f.index if rows is None else rows(f)
    f.loc[idx, "UnitPrice"] = f.loc[idx, "UnitPrice"].map(fmt)
    data = to_csv(f)
    return {
        "map_events": via_route(data, BASE_MAPPING, TRUTH),
        "first_problems": sample_problems(data, BASE_MAPPING),
    }


@case("N1", "Decimal comma ('2,55') on every price")
def n1() -> dict[str, Any]:
    return _price_variant(lambda s: s.replace(".", ","))


@case("N2", "European thousands and decimals ('1.234,50'): prices x 1000 written the European way")
def n2() -> dict[str, Any]:
    f = BASE.copy()
    f["UnitPrice"] = (f.UnitPrice.astype(float) * 1000).map(
        lambda v: f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    )
    t = BASE.copy()
    t["UnitPrice"] = (t.UnitPrice.astype(float) * 1000).map(lambda v: f"{v:.2f}")
    truth = truth_of(t, BASE_MAPPING)
    data = to_csv(f)
    return {
        "map_events": via_route(data, BASE_MAPPING, truth),
        "first_problems": sample_problems(data, BASE_MAPPING),
        "examples": f.UnitPrice.head(3).tolist(),
    }


@case("N3", "European thousands without decimals ('1.234' meaning 1234) on whole-number amounts")
def n3() -> dict[str, Any]:
    f = BASE.copy()
    f["Quantity"] = "1"
    whole = (f.UnitPrice.astype(float) * 1000).round().astype(int)
    f["UnitPrice"] = whole.map(lambda v: f"{v:,}".replace(",", "."))
    t = f.copy()
    t["UnitPrice"] = whole.astype(str)
    truth = truth_of(t, BASE_MAPPING)
    return {"map_events": via_route(to_csv(f), BASE_MAPPING, truth), "examples": f.UnitPrice.head(3).tolist()}


@case("N4", "Currency symbols and codes in the amount ('£2.55', '2.55 GBP') on 10% of rows")
def n4() -> dict[str, Any]:
    out = _price_variant(lambda s: "£" + s, lambda f: f.index[f.index % 20 == 0])
    out2 = _price_variant(lambda s: s + " GBP", lambda f: f.index[f.index % 20 == 1])
    return {"pound_sign": out, "code_suffix": out2}


@case(
    "N5",
    "Two currencies in one file: UK orders in GBP + Brazilian (Olist) orders in BRL, with a currency column",
)
def n5() -> dict[str, Any]:
    uk = BASE[["CustomerID", "InvoiceDate", "Quantity", "UnitPrice"]].copy()
    uk["ts"] = pd.to_datetime(uk.InvoiceDate, format="%m/%d/%Y %H:%M").dt.strftime("%Y-%m-%dT%H:%M:%S")
    uk["value"] = uk.Quantity.astype(float) * uk.UnitPrice.astype(float)
    uk["currency"] = "GBP"
    olist_dir = DATASETS / "olist"
    pay_path = next(p for p in olist_dir.iterdir() if "payments" in p.name)
    pay = pd.read_csv(pay_path, nrows=2000)
    br = pd.DataFrame(
        {
            "CustomerID": "br_" + pay.order_id.str[:8],
            "ts": "2017-10-02T10:56:33",
            "value": pay.payment_value,
            "currency": "BRL",
        }
    )
    both = pd.concat([uk[["CustomerID", "ts", "value", "currency"]], br], ignore_index=True)
    m = EventMapping(
        customer_id="CustomerID",
        timestamp="ts",
        event_type=EventType.PURCHASE,
        value_column="value",
        attrs={"currency": "currency"},
    )
    _status, body = route_map(to_csv(both), m)
    events = body["events"]
    by_cur: dict[str, float] = {}
    for e in events:
        by_cur[e["attrs"]["currency"]] = by_cur.get(e["attrs"]["currency"], 0.0) + e["value"]
    from autonomy.contracts import Event

    store = InMemoryEventStore()
    store.append([Event.model_validate(e) for e in events])
    states = InMemoryStateStore()
    rebuild_states(store, states, StateFolder(), datetime.fromisoformat("2018-01-01T00:00:00+00:00"))
    rev = sum(s.revenue for s in states.scan())
    return {
        "verdict": "silent_wrong",
        "mapped": body["mapped"],
        "problems": body["problem_counts"],
        "sum_by_currency_attr": {k: round(v, 2) for k, v in by_cur.items()},
        "state_revenue_total_one_number": round(rev, 2),
        "note": "the currency survives only as an attr; state adds GBP and BRL into one revenue",
    }


@case("N6", "Negative prices (bad-debt adjustments) without negative_as")
def n6() -> dict[str, Any]:
    return _price_variant(lambda s: "-" + s, lambda f: f.index[f.index % 50 == 0])


@case("N7", "'N/A' and '-' in the price on 2% of rows")
def n7() -> dict[str, Any]:
    out = _price_variant(lambda _s: "N/A", lambda f: f.index[f.index % 100 == 0])
    out2 = _price_variant(lambda _s: "-", lambda f: f.index[f.index % 100 == 1])
    return {"na": out, "dash": out2}


# ---- T: dates


def _date_variant(
    series_fn: Callable[[pd.Series], pd.Series],
    mapping: EventMapping = BASE_MAPPING,
    rows: Callable[[pd.DataFrame], pd.Index] | None = None,
    fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    f = BASE.copy()
    idx = f.index if rows is None else rows(f)
    parsed = pd.to_datetime(f.InvoiceDate, format="%m/%d/%Y %H:%M")
    new = series_fn(parsed)
    f.loc[idx, "InvoiceDate"] = new[idx]
    data = to_csv(f)
    return {
        "map_events": via_route(data, mapping, TRUTH),
        "first_problems": sample_problems(data, mapping),
        "examples": f.InvoiceDate.head(2).tolist(),
        "profile": route_profile(data, fields or {"snapshot": "2012-01-01T00:00:00+00:00"}),
    }


@case("T1", "UK day-first dates (01/12/2010) read with the US format the loader uses (%m/%d/%Y)")
def t1() -> dict[str, Any]:
    out = _date_variant(lambda p: p.dt.strftime("%d/%m/%Y %H:%M"))
    # A wider date range, so some days are <= 12 and some are not.
    f = pd.read_csv(DATASETS / "online_retail_i" / "online-retail-dataset.csv", dtype=str, encoding="latin-1")
    f = f[f.CustomerID.notna() & (f.Quantity.astype(float) > 0) & (f.UnitPrice.astype(float) > 0)]
    f = f.iloc[:: max(1, len(f) // 5000)].head(5000).reset_index(drop=True)
    truth = truth_of(f, BASE_MAPPING)
    g = f.copy()
    g["InvoiceDate"] = pd.to_datetime(f.InvoiceDate, format="%m/%d/%Y %H:%M").dt.strftime("%d/%m/%Y %H:%M")
    year = via_route(to_csv(g), BASE_MAPPING, truth)
    loader = online_retail_log_events(g, grain="line")
    lt = online_retail_log_events(f, grain="line")
    tmap = {e.event_id: e.ts for e in lt.events}
    swapped = sum(1 for e in loader.events if e.event_id in tmap and e.ts != tmap[e.event_id])
    return {
        "first_2000_rows_all_in_december": out,
        "a_year_of_rows": year,
        "built_in_loader_year": {
            "events": len(loader.events),
            "wrong_dates": swapped,
            "problems": problem_counts(loader.problems),
        },
    }


@case("T2", "Dates not in ISO 8601 with no format given (the Doctor's default)")
def t2() -> dict[str, Any]:
    m = BASE_MAPPING.model_copy(update={"timestamp_format": None})
    return _date_variant(lambda p: p.dt.strftime("%m/%d/%Y %H:%M"), mapping=m)


@case("T3", "Excel serial dates (40513.35) on 5% of rows")
def t3() -> dict[str, Any]:
    return _date_variant(
        lambda p: ((p - pd.Timestamp("1899-12-30")) / pd.Timedelta(days=1)).round(5).astype(str),
        rows=lambda f: f.index[f.index % 20 == 0],
    )


@case("T4", "Two-digit years (%m/%d/%y) where the data are from 1968-1969")
def t4() -> dict[str, Any]:
    m = BASE_MAPPING.model_copy(update={"timestamp_format": "%m/%d/%y %H:%M"})
    f = BASE.copy()
    p = pd.to_datetime(f.InvoiceDate, format="%m/%d/%Y %H:%M") - pd.DateOffset(years=42)
    truth = Truth(TRUTH.customer, {k: v.replace(year=v.year - 42) for k, v in TRUTH.ts.items()}, TRUTH.value)
    f["InvoiceDate"] = p.dt.strftime("%m/%d/%y %H:%M")
    data = to_csv(f)
    return {
        "map_events": via_route(data, m, truth),
        "examples": f.InvoiceDate.head(2).tolist(),
        "profile_with_snapshot_2026": route_profile(data, {"snapshot": "2026-10-20T00:00:00+00:00"}).get(
            "problems"
        ),
    }


@case("T5", "Placeholder dates ('01/01/1900 00:00', '01/01/1970 00:00') on 5% of rows")
def t5() -> dict[str, Any]:
    out = _date_variant(
        lambda p: pd.Series(
            np.where(np.arange(len(p)) % 2, "01/01/1900 00:00", "01/01/1970 00:00"), index=p.index
        ),
        rows=lambda f: f.index[f.index % 20 == 0],
    )
    f = BASE.copy()
    idx = f.index[f.index % 20 == 0]
    f.loc[idx, "InvoiceDate"] = "01/01/1900 00:00"
    events, _ = map_frame(f, BASE_MAPPING, "csv")
    store = InMemoryEventStore()
    store.append(events)
    st = InMemoryStateStore()
    rebuild_states(store, st, StateFolder(), datetime.fromisoformat("2010-12-10T00:00:00+00:00"))
    out["first_seen_1900_customers"] = sum(
        1 for s in st.scan() if s.first_seen_at and s.first_seen_at.year == 1900
    )
    return out


@case("T6", "Times with a zone word (' GMT', ' BST') and with numeric offsets mixed in one column")
def t6() -> dict[str, Any]:
    m = BASE_MAPPING.model_copy(update={"timestamp_format": None})
    zone_word = _date_variant(lambda p: p.dt.strftime("%Y-%m-%d %H:%M:%S") + " GMT", mapping=m)
    f = BASE.copy()
    p = pd.to_datetime(f.InvoiceDate, format="%m/%d/%Y %H:%M").dt.tz_localize("Europe/London")
    offs = ["+00:00", "+05:30", "-05:00"]
    f["InvoiceDate"] = [
        ts.tz_convert(
            {"+00:00": "UTC", "+05:30": "Asia/Kolkata", "-05:00": "America/New_York"}[offs[i % 3]]
        ).isoformat()
        for i, ts in enumerate(p)
    ]
    mixed = via_route(to_csv(f), m, TRUTH)
    return {"zone_word": zone_word, "mixed_numeric_offsets": mixed}


@case(
    "T7", "YYMM month codes (KDD Cup 98): '9606' as text vs '9606.0' after pandas read a column with blanks"
)
def t7() -> dict[str, Any]:
    with zipfile.ZipFile(DATASETS / "kdd98_full" / "cup98LRN.zip") as z:
        raw = pd.read_csv(z.open("cup98LRN.txt"), nrows=3000, low_memory=False)
    frame_pd = raw[["CONTROLN", "ADATE_3"]].copy()  # what pandas gives a customer: floats, NaN
    m = EventMapping(
        customer_id="CONTROLN",
        timestamp="ADATE_3",
        timestamp_format="%y%m",
        event_type=EventType.SEND,
        channel=Channel.WEBHOOK,
    )
    e1, p1 = map_frame(frame_pd, m, "kdd")
    with zipfile.ZipFile(DATASETS / "kdd98_full" / "cup98LRN.zip") as z:
        head = b"".join(z.open("cup98LRN.txt").readlines()[:3001])
    text = read_csv_bytes(head)[["CONTROLN", "ADATE_3"]]  # what the Doctor reads: text, "9606"
    e2, p2 = map_frame(text, m, "kdd")
    return {
        "verdict": "reported",
        "pandas_floats": {
            "mapped": len(e1),
            "problems": problem_counts(p1),
            "first": [f"{p.code}:{p.message}" for p in p1[:1]],
        },
        "doctor_text": {
            "mapped": len(e2),
            "problems": problem_counts(p2),
            "first": [f"{p.code}:{p.message}" for p in p2[:1]],
        },
        "cells_pandas": frame_pd.ADATE_3.astype(str).head(2).tolist(),
        "cells_text": text.ADATE_3.head(2).tolist(),
        "blank_cells": int(text.ADATE_3.isna().sum()),
    }


# ---- Z: time zones


@case("Z1", "Naive New York shop times loaded with the default time zone (UTC)")
def z1() -> dict[str, Any]:
    m_ny = BASE_MAPPING.model_copy(update={"timezone": "America/New_York"})
    m_def = BASE_MAPPING.model_copy(update={"timezone": "UTC"})
    t_ny = truth_of(BASE, m_ny)
    out = via_frame(BASE, m_def, t_ny)
    e_ny, _ = map_frame(BASE, m_ny, "csv")
    e_def, _ = map_frame(BASE, m_def, "csv")
    shift = {(b.ts - a.ts).total_seconds() / 3600 for a, b in zip(e_ny, e_def, strict=True)}
    out["stored_minus_true_hours"] = sorted(shift)
    store = InMemoryEventStore()
    store.append(e_ny)
    st = InMemoryStateStore()
    rebuild_states(store, st, StateFolder(), datetime.fromisoformat("2010-12-10T00:00:00+00:00"))
    out["state_timezone_even_with_the_right_mapping_zone"] = sorted({s.timezone for s in st.scan()})
    out["note"] = "every purchase is stored 5 hours early; the mapping's zone never reaches state either"
    return out


@case(
    "Z2",
    "DST edge: local times that do not exist (spring forward) or happen twice (fall back), Europe/London",
)
def z2() -> dict[str, Any]:
    f = pd.DataFrame(
        {
            "CustomerID": ["a", "b", "c"],
            "InvoiceDate": ["2011-03-27 01:30:00", "2011-10-30 01:30:00", "2011-10-30 00:30:00"],
            "Quantity": "1",
            "UnitPrice": "1",
            "StockCode": "x",
        }
    )
    m = BASE_MAPPING.model_copy(update={"timestamp_format": None})
    events, problems = map_frame(f, m, "csv")
    return {
        "verdict": "ok" if not problems else "reported",
        "written": f.InvoiceDate.tolist(),
        "stored_utc": [e.ts.astimezone(UTC).isoformat() for e in events],
        "problems": problem_counts(problems),
        "note": "01:30 on 27 Mar 2011 never happened in London; it is stored without a warning",
    }


# ---- X: text, structure, size


@case("X1", "Huge text column: a 200 KB product review on 50 rows, mapped as an attr")
def x1() -> dict[str, Any]:
    f = BASE.copy()
    rng = np.random.default_rng(0)
    words = np.array(["great", "gift", "box", "arrived", "late", "lovely", "colour", "would", "buy", "again"])
    review = " ".join(rng.choice(words, size=33000))
    f["Review"] = ""
    f.loc[f.index % 40 == 0, "Review"] = review
    m = BASE_MAPPING.model_copy(update={"attrs": {"review": "Review"}})
    data = to_csv(f)
    t0 = time.perf_counter()
    prof = route_profile(data)
    tp = time.perf_counter() - t0
    t0 = time.perf_counter()
    status, body = route_map(data, m)
    tm = time.perf_counter() - t0
    resp_bytes = len(json.dumps(body)) if isinstance(body, dict) else 0
    return {
        "verdict": "ok" if status == 200 else "refused",
        "upload_mb": round(len(data) / 2**20, 2),
        "profile_seconds": round(tp, 2),
        "profile_score": prof.get("readiness_score"),
        "map_seconds": round(tm, 2),
        "map_status": status,
        "response_mb": round(resp_bytes / 2**20, 2),
        "review_chars_kept_in_attrs": (
            len(body["events"][0]["attrs"].get("review", "")) if status == 200 else None
        ),
    }


@case("X2", "A stray quote in a text field (unclosed until a later quote) and one never closed")
def x2() -> dict[str, Any]:
    f = BASE.copy()
    csv_text = f.to_csv(index=False)
    lines = csv_text.splitlines()

    # Row 10's description gets an opening quote; row 14's description a closing one.
    def damage(line: str, before: str = "", after: str = "") -> str:
        parts = line.split(",")
        parts[2] = before + parts[2] + after
        return ",".join(parts)

    lines[11] = damage(lines[11], before='"')  # row 10's description opens a quote...
    lines[15] = damage(lines[15], after='"')  # ...and row 14's closes it
    merged = ("\n".join(lines) + "\n").encode()
    unclosed_lines = csv_text.splitlines()
    unclosed_lines[11] = damage(unclosed_lines[11], '"')
    unclosed = ("\n".join(unclosed_lines) + "\n").encode()
    return {
        "later_quote_closes_it": via_route(merged, BASE_MAPPING, TRUTH, by_content=True),
        "never_closed": via_route(unclosed, BASE_MAPPING, TRUTH),
    }


@case("X3", "Two exports concatenated: the header row repeated in the middle")
def x3() -> dict[str, Any]:
    text = BASE.iloc[:1000].to_csv(index=False) + BASE.iloc[1000:].to_csv(index=False)
    data = text.encode()
    return {
        "map_events": via_route(data, BASE_MAPPING, TRUTH, by_content=True),
        "first_problems": sample_problems(data, BASE_MAPPING),
    }


@case("X4", "A totals row at the bottom ('TOTAL' in the id column, the sum in the price)")
def x4() -> dict[str, Any]:
    f = BASE.copy()
    total = pd.DataFrame(
        [
            {
                "InvoiceNo": "",
                "StockCode": "",
                "Description": "",
                "Quantity": "1",
                "InvoiceDate": f.InvoiceDate.iloc[-1],
                "UnitPrice": f"{TRUTH.total:.2f}",
                "CustomerID": "TOTAL",
                "Country": "",
            }
        ]
    )
    data = to_csv(pd.concat([f, total], ignore_index=True))
    return {"map_events": via_route(data, BASE_MAPPING, TRUTH)}


@case("X5", "Header with a stray space (' CustomerID') and a duplicated column name")
def x5() -> dict[str, Any]:
    f = BASE.rename(columns={"CustomerID": " CustomerID"})
    space = route_map(to_csv(f), BASE_MAPPING)
    g = BASE.copy()
    g.insert(0, "dup", "store_" + g.Country.str[:2])  # a store id that an export also called CustomerID
    text = g.to_csv(index=False).replace("dup,", "CustomerID,", 1)
    dup = via_route(text.encode(), BASE_MAPPING, TRUTH)
    return {
        "space_in_header": {"status": space[0], "detail": str(space[1].get("detail"))[:300]},
        "duplicated_header": dup,
    }


@case("X6", "Upload size: health insurance train.csv (21.4 MB) and the same file cut to 19 MB")
def x6() -> dict[str, Any]:
    data = (DATASETS / "health_insurance_xsell" / "train.csv").read_bytes()
    cut = data[: 19 * 2**20]
    cut = cut[: cut.rfind(b"\n") + 1]
    t0 = time.perf_counter()
    small = route_profile(cut, {"primary_key": "id", "target": "Response"})
    return {"full": route_profile(data), "cut_19mb": small, "cut_seconds": round(time.perf_counter() - t0, 2)}


# ---- W: shapes and meaning the mapping cannot express


@case("W1", "Wide table: six monthly payments in columns (credit default PAY_AMT1..6)")
def w1() -> dict[str, Any]:
    cols = list(pd.read_csv(DATASETS / "uci_credit_default" / "UCI_Credit_Card.csv", nrows=1).columns)
    return {
        "verdict": "cannot",
        "columns": [c for c in cols if c.startswith("PAY_AMT")],
        "why": "EventMapping has one timestamp and one value column per table; six (month, amount) pairs per row need a melt in code (d_adapters.adapt_credit)",
    }


@case("W2", "Date split over three columns with the month as a name (hotel bookings)")
def w2() -> dict[str, Any]:
    f = pd.read_csv(DATASETS / "hotel_bookings" / "hotels.csv", nrows=200)
    m = EventMapping(
        customer_id="agent",
        timestamp="arrival_date_year",
        timestamp_format="%Y",
        event_type=EventType.PURCHASE,
        value_column="adr",
    )
    events, problems = map_frame(f, m, "hotel")
    return {
        "verdict": "cannot",
        "best_without_code": {
            "mapped": len(events),
            "problems": problem_counts(problems),
            "note": "timestamp_format='%Y' puts every stay on 1 January",
            "first_ts": events[0].ts.isoformat() if events else None,
        },
        "why": "one timestamp column only; year + month name + day need joining in code (d_adapters.hotel_arrival)",
    }


@case("W3", "Sentinel numbers: bank pdays=999 ('never contacted'), Starbucks age=118 ('unknown')")
def w3() -> dict[str, Any]:
    bank = (DATASETS / "bank_marketing" / "bank-additional-full.csv").read_bytes()
    prof = route_profile(bank, {"delimiter": ";"})
    frame = read_csv_bytes(bank, delimiter=";")
    share999 = float((frame.pdays == "999").mean())
    sb = pd.read_json(DATASETS / "starbucks_rewards" / "profile.json", lines=True)
    sb_prof = route_profile(sb.to_csv(index=False).encode(), {"primary_key": "id"})
    return {
        "verdict": "silent_wrong" if not any("pdays" in p for p in prof.get("problems", [])) else "reported",
        "pdays_999_share": round(share999, 4),
        "profile_problems": prof.get("problems"),
        "starbucks_age_118_rows": int((sb.age == 118).sum()),
        "starbucks_rows": len(sb),
        "starbucks_profile_problems": sb_prof.get("problems"),
        "note": "no problem names pdays; a model or filter reading it as days would see 96% of clients 999 days out",
    }


@case(
    "W4", "Columns recorded after the outcome: bank `duration` (call length) and hotel `reservation_status`"
)
def w4() -> dict[str, Any]:
    bank = read_csv_bytes(
        (DATASETS / "bank_marketing" / "bank-additional-full.csv").read_bytes(), delimiter=";"
    )
    rb = profile_frame(bank, target="y", use_v1=False)
    hotel = read_csv_bytes((DATASETS / "hotel_bookings" / "hotels.csv").read_bytes())
    rh = profile_frame(hotel, target="is_canceled", use_v1=False)
    y = (bank.y == "yes").astype(float)
    corr = float(np.corrcoef(bank.duration.astype(float), y)[0, 1])
    return {
        "bank_leak_suspects": [f"{s.column}:{s.reason}:{s.score}" for s in rb.leak_check.suspects],
        "bank_duration_corr_with_y": round(corr, 4),
        "hotel_leak_suspects": [f"{s.column}:{s.reason}:{s.score}" for s in rh.leak_check.suspects],
        "verdict": (
            "silent_wrong" if not any(s.column == "duration" for s in rb.leak_check.suspects) else "reported"
        ),
    }


# --------------------------------------------------------------------------- runner


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    only = [c.strip() for c in args.only.split(",") if c.strip()]
    results: dict[str, Any] = {}
    for code, (title, fn) in CASES.items():
        if only and code not in only:
            continue
        t0 = time.perf_counter()
        try:
            res = fn()
        except Exception as exc:  # a crash of the product (or of this runner) is itself a finding
            res = {
                "verdict": "crash",
                "error": f"{type(exc).__name__}: {exc}"[:600],
                "trace": traceback.format_exc()[-1500:],
            }
        res["seconds"] = round(time.perf_counter() - t0, 2)
        results[code] = {"title": title, **res}
        print(f"{code} {title[:70]:<70} -> {_verdicts(res)}", flush=True)
    payload = {
        "command": command_line(),
        "env": env_info(),
        "base_rows": N_ROWS,
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "cases": results,
    }
    name = "cases" if not only else "cases_" + "_".join(only)
    print(write_result("messy", name, payload))
    return 0


def _verdicts(res: Any) -> str:
    found: list[str] = []

    def walk(x: Any, path: str) -> None:
        if isinstance(x, dict):
            if "verdict" in x:
                found.append(f"{path or 'case'}={x['verdict']}")
            for k, v in x.items():
                if k != "verdict":
                    walk(v, f"{path}.{k}" if path else k)

    walk(res, "")
    return ", ".join(found)


if __name__ == "__main__":
    raise SystemExit(main())
