"""Writes the trap bench's cases (`scripts/doctor_trap_bench.py`): small CSV files and a human-written `case.json`.

Each function below plants one trap from `SCENARIOS.md` (the scenario ID is on it) in a few dozen rows, with a fixed
seed, and says in plain words what a correct result looks like. The checks use the true numbers of the data this
file made (the true total, the true customer count), never the product's output. Standard library only: this file
imports nothing from the product, so the answer key cannot be tuned to it.

    python -m tests.fixtures.doctor_traps.generate           # write the cases next to this file
    python -m tests.fixtures.doctor_traps.generate --check   # fail if the files on disk differ from a fresh write

Besides the input files, a case may carry `ideal.csv`: the table a correct Doctor would hand over. The tests grade
it with the same checks, so a check that no correct answer could pass is found here, not in a report.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
Row = dict[str, str]

AS_OF = date(2026, 9, 30)
PLANS = ("basic", "standard", "premium")
THEMES = {
    1: "SQL steps slip past the safety limits",
    2: "False, merged or missed joins",
    3: "Double counting and wrong grain",
    4: "Time travel and leaks the checks miss",
    5: "Outcome: who is eligible, and when the clock runs",
    6: "Dates that are read wrong without any error",
    7: "Money and units whose meaning is not in the cells",
    8: "Who one row is, and one model or several",
    9: "Consent, sensitive data and who may be contacted",
    10: "Spreadsheet and export shapes the reader misses",
    11: "Test, staff and extreme accounts",
    12: "Campaign history: selection read as cause",
}


@dataclass
class Trap:
    case_id: str
    scenario: str
    theme: int
    description: str
    expected: str
    tables: dict[str, list[Row]]
    primary: list[str]
    key: str
    checks: list[dict[str, Any]]
    goal: str = "telco-churn"
    answers: dict[str, str] = field(default_factory=dict)
    rows_may_drop: list[str] = field(default_factory=list)
    ideal_rows: list[Row] | None = None
    ideal: dict[str, Any] = field(default_factory=dict)


def rng_for(case_id: str) -> random.Random:
    return random.Random(zlib.crc32(case_id.encode("utf-8")))


def day(offset: int, start: date = date(2026, 1, 1)) -> str:
    return (start + timedelta(days=offset)).isoformat()


def csv_text(rows: Sequence[Row]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def customers(
    rng: random.Random, ids: Sequence[str], churn_rate: float = 0.25, **extra: Callable[[int], str]
) -> list[Row]:
    """A plain customer list: ID, plan, tenure, spend and the 0/1 outcome `churn`."""
    rows: list[Row] = []
    for i, cid in enumerate(ids):
        row: Row = {
            "customer_id": cid,
            "plan": rng.choice(PLANS),
            "tenure_months": str(rng.randint(2, 60)),
            "monthly_spend": str(rng.randint(200, 2000)),
            "churn": "1" if rng.random() < churn_rate else "0",
        }
        for name, make in extra.items():
            row[name] = make(i)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 2. False, merged or missed joins
# ---------------------------------------------------------------------------
def k1_merged_prefixes() -> Trap:
    rng = rng_for("k1_merged_prefixes")
    retail = [f"C-{n}" for n in range(101, 131)]
    business = [f"S-{n}" for n in range(101, 131)]
    rows = customers(rng, retail + business)
    for row in rows:
        row["segment"] = "retail" if row["customer_id"].startswith("C-") else "business"
    orders = [
        {
            "order_id": f"O{n}{k}",
            "customer_id": str(n),
            "order_date": day(n + k * 7),
            "amount": str(rng.randint(100, 900)),
        }
        for n in range(101, 131)
        for k in range(2)
    ]
    return Trap(
        case_id="k1_merged_prefixes",
        scenario="K1",
        theme=2,
        description="Customer IDs C-101 and S-101 are two different people; the orders file writes both as 101.",
        expected="60 customers stay 60: a rule that strips the prefix must not merge C-101 and S-101 into one row.",
        tables={"customers": rows, "orders": orders},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "join",
                "kind": "one_row_per_key",
                "expected_rows": 60,
                "label": "60 customers stay 60 rows, none merged",
            },
            {
                "category": "join",
                "kind": "cells",
                "label": "C-101 and S-101 each keep their own segment",
                "cells": [
                    {"key": "C-101", "column": "segment", "equals": "retail"},
                    {"key": "S-101", "column": "segment", "equals": "business"},
                ],
            },
        ],
        ideal_rows=rows,
    )


def k3_shared_phone() -> Trap:
    rng = rng_for("k3_shared_phone")
    ids = [f"C{n:04d}" for n in range(1, 51)]
    rows = customers(rng, ids)
    for i, row in enumerate(rows):
        row["phone"] = (
            "9999999999" if i < 10 else ("9876543210" if i < 18 else f"98{rng.randint(10000000, 99999999)}")
        )
    calls = [
        {"call_id": f"K{i}", "phone": row["phone"], "call_date": day(i), "minutes": str(rng.randint(1, 30))}
        for i, row in enumerate(rows)
    ]
    return Trap(
        case_id="k3_shared_phone",
        scenario="K3",
        theme=2,
        description="Ten customers share the dummy phone 9999999999 and eight share a family phone; the calls file is keyed by phone.",
        expected="50 customers stay 50 rows: calls must not be handed to every customer who shares a number.",
        tables={"customers": rows, "calls": calls},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "join",
                "kind": "one_row_per_key",
                "expected_rows": 50,
                "label": "50 customers stay 50 rows when calls are keyed by a shared phone",
            }
        ],
        ideal_rows=rows,
    )


def f01_leading_zeros() -> Trap:
    rng = rng_for("f01_leading_zeros")
    ids = [f"{n:06d}" for n in range(101, 161)]
    rows = customers(rng, ids)
    for row in rows:
        row["pin_code"] = f"0{rng.randint(1000, 9999)}1"
    orders = [
        {"order_id": f"O{i}", "customer_id": cid, "amount": str(rng.randint(100, 900))}
        for i, cid in enumerate(ids)
    ]
    return Trap(
        case_id="f01_leading_zeros",
        scenario="F01",
        theme=2,
        description="Customer IDs like 000101 and PIN codes like 012341 start with zeros that a number reader drops.",
        expected="IDs and PIN codes come out exactly as written, with their leading zeros.",
        tables={"customers": rows, "orders": orders},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "unapproved_change",
                "kind": "values_unchanged",
                "columns": ["customer_id", "pin_code"],
                "label": "IDs and PIN codes keep their leading zeros",
            }
        ],
        ideal_rows=rows,
    )


def f02_excel_e_plus() -> Trap:
    rng = rng_for("f02_excel_e_plus")
    rows: list[Row] = []
    for n in range(40):
        true_id = 123456000000 + n * 310_000
        rows.append(
            {
                "customer_id": f"{true_id / 1e11:.5f}E+11",
                "plan": rng.choice(PLANS),
                "churn": "1" if rng.random() < 0.25 else "0",
            }
        )
    return Trap(
        case_id="f02_excel_e_plus",
        scenario="F02",
        theme=2,
        description="Excel saved the 12-digit customer IDs as 1.23457E+11, so 40 customers now share a handful of IDs.",
        expected="The Doctor says the IDs were rounded and asks for a re-export as text; all 40 rows are kept and none is joined.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "asked",
                "kind": "asked",
                "label": "says the IDs were rounded by Excel and asks for a re-export",
                "text": ["rounded", "E\\+", "scientific", "re-?export.*as text", "as text"],
            },
            {"category": "row_loss", "kind": "row_count", "rows": 40, "label": "all 40 rows are kept"},
        ],
        ideal={
            "asks": [
                "These IDs were rounded when the file was saved in Excel. Please re-export with the column as text."
            ],
            "no_universal": ["unapproved_change"],
        },
        ideal_rows=rows,
    )


def crit03_dup_and_drop() -> Trap:
    rng = rng_for("crit03_dup_and_drop")
    ids = [f"C{n:03d}" for n in range(1, 101)]
    rows = customers(rng, ids)
    tags: list[Row] = []
    for n in range(1, 81):
        for k in range(2 if n <= 20 else 1):
            tags.append(
                {
                    "tag_id": f"T{n}-{k}",
                    "customer_id": f"C{n:03d}",
                    "tag": rng.choice(["vip", "promo", "new"]),
                    "tag_date": day(n + k),
                }
            )
    tags += [
        {"tag_id": f"X{n}", "customer_id": f"C9{n:02d}", "tag": "promo", "tag_date": day(n)}
        for n in range(10)
    ]
    return Trap(
        case_id="crit03_dup_and_drop",
        scenario="crit-03",
        theme=1,
        description="Twenty customers have two tag rows and twenty have none, so a join adds 20 and drops 20 and the row count stays 100.",
        expected="Exactly the 100 customers, each once: neither the duplicates nor the dropped customers may cancel out.",
        tables={"accounts": rows, "tags": tags},
        primary=["accounts"],
        key="customer_id",
        checks=[
            {
                "category": "join",
                "kind": "one_row_per_key",
                "expected_rows": 100,
                "label": "100 customers once each (duplicates and drops are not netted)",
            }
        ],
        ideal_rows=rows,
    )


# ---------------------------------------------------------------------------
# 3. Double counting and wrong grain
# ---------------------------------------------------------------------------
def v19_order_total_repeated() -> Trap:
    rng = rng_for("v19_order_total_repeated")
    lines: list[Row] = []
    true_total = 0
    line_no = 0
    for o in range(1, 41):
        total = rng.randint(500, 5000)
        true_total += total
        for _ in range(rng.randint(2, 4)):
            line_no += 1
            lines.append(
                {
                    "line_id": f"L{line_no}",
                    "order_id": f"O{o}",
                    "customer_id": f"C{(o % 25) + 1:03d}",
                    "sku": f"SKU{rng.randint(1, 30)}",
                    "order_total": str(total),
                    "churn": "0",
                }
            )
    ideal = [next(x for x in lines if x["order_id"] == f"O{o}") for o in range(1, 41)]
    kept = {r["line_id"] for r in ideal}
    dropped = [r["line_id"] for r in lines if r["line_id"] not in kept]
    return Trap(
        case_id="v19_order_total_repeated",
        scenario="V19",
        theme=3,
        description="The order total is written on every line item of the order, so adding the column up counts each order 2 to 4 times.",
        expected=f"The order total is counted once per order ({true_total:,}), or the Doctor asks which level the total is on.",
        tables={"order_lines": lines},
        primary=["order_lines"],
        key="line_id",
        rows_may_drop=dropped,
        checks=[
            {
                "category": "double_counting",
                "kind": "any_of",
                "label": "order totals add up to the true spend, not a repeated one",
                "checks": [
                    {
                        "kind": "asked",
                        "text": [
                            "order total",
                            "(every|each) line item",
                            "once per order",
                            "repeated on (every|each) line",
                        ],
                    },
                    {"kind": "column_total", "columns": ["order_total"], "expected_total": true_total},
                ],
            }
        ],
        ideal_rows=ideal,
        ideal={"asks": ["The order total is repeated on every line item. Count it once per order?"]},
    )


def f21_overlapping_exports() -> Trap:
    rng = rng_for("f21_overlapping_exports")
    jan = [
        {
            "call_id": f"K{i}",
            "customer_id": f"C{rng.randint(1, 60):03d}",
            "call_date": day(i % 31),
            "minutes": str(rng.randint(1, 30)),
        }
        for i in range(80)
    ]
    overlap = jan[55:80]
    feb = [dict(r) for r in overlap] + [
        {
            "call_id": f"K{100 + i}",
            "customer_id": f"C{rng.randint(1, 60):03d}",
            "call_date": day(31 + i % 28),
            "minutes": str(rng.randint(1, 30)),
        }
        for i in range(70)
    ]
    unique = jan + feb[len(overlap) :]
    return Trap(
        case_id="f21_overlapping_exports",
        scenario="F21",
        theme=3,
        description="The February export repeats the last 25 calls of January, so stacking the two files counts them twice.",
        expected="January and February are stacked into one table of 150 calls, each call once.",
        tables={"calls_jan": jan, "calls_feb": feb},
        primary=["calls_jan", "calls_feb"],
        key="call_id",
        checks=[
            {
                "category": "double_counting",
                "kind": "one_row_per_key",
                "expected_rows": 150,
                "label": "150 calls once each after stacking the two exports",
            }
        ],
        ideal_rows=unique,
    )


def k15_failed_payments() -> Trap:
    rng = rng_for("k15_failed_payments")
    pays: list[Row] = []
    real = 0
    for i in range(60):
        status = "failed" if i % 5 == 0 else "success"
        amount = rng.randint(200, 3000)
        real += amount if status == "success" else 0
        pays.append(
            {
                "payment_id": f"P{i:03d}",
                "customer_id": f"C{(i % 30) + 1:03d}",
                "amount": str(amount),
                "status": status,
                "churn": "0",
            }
        )
    failed = [r["payment_id"] for r in pays if r["status"] == "failed"]
    return Trap(
        case_id="k15_failed_payments",
        scenario="K15",
        theme=3,
        description="One payment in five is a failed attempt; adding the amount column up counts money that never arrived.",
        expected=f"Only successful payments are counted ({real:,}), or the Doctor asks which statuses are real money.",
        tables={"payments": pays},
        primary=["payments"],
        key="payment_id",
        rows_may_drop=failed,
        checks=[
            {
                "category": "double_counting",
                "kind": "any_of",
                "label": "spend counts successful payments only",
                "checks": [
                    {"kind": "asked", "text": ["failed (payments?|attempts?)", "real money", "successful"]},
                    {"kind": "column_total", "columns": ["amount"], "expected_total": real},
                ],
            }
        ],
        ideal_rows=[r for r in pays if r["status"] == "success"],
        ideal={"asks": ["Which payment statuses are real money? Failed attempts are not."]},
    )


def k16_ticket_status_log() -> Trap:
    rng = rng_for("k16_ticket_status_log")
    log: list[Row] = []
    latest: list[Row] = []
    n = 0
    for t in range(1, 31):
        steps = rng.randint(2, 4)
        for s, status in enumerate(["opened", "assigned", "reopened", "closed"][:steps]):
            n += 1
            row = {
                "log_id": f"G{n}",
                "ticket_id": f"TK{t:03d}",
                "customer_id": f"C{(t % 20) + 1:03d}",
                "status": status,
                "status_date": day(t + s),
                "churn": "0",
            }
            log.append(row)
        latest.append(log[-1])
    return Trap(
        case_id="k16_ticket_status_log",
        scenario="K16",
        theme=3,
        description="Each complaint ticket has two to four status rows (opened, assigned, reopened, closed); counting rows counts a complaint many times.",
        expected="30 tickets, each once with its latest status, or the Doctor asks how to count them.",
        tables={"tickets_log": log},
        primary=["tickets_log"],
        key="ticket_id",
        checks=[
            {
                "category": "double_counting",
                "kind": "any_of",
                "label": "30 tickets counted once each",
                "checks": [
                    {"kind": "asked", "text": ["latest (status|one)", "status (history|rows)", "per ticket"]},
                    {"kind": "one_row_per_key", "expected_rows": 30},
                ],
            }
        ],
        ideal_rows=latest,
        ideal={"asks": ["Each ticket has several status rows. Count the latest one per ticket?"]},
    )


# ---------------------------------------------------------------------------
# 4. Time travel and leaks
# ---------------------------------------------------------------------------
def t06_status_exported_today() -> Trap:
    rng = rng_for("t06_status_exported_today")
    rows = customers(rng, [f"C{n:04d}" for n in range(1, 121)])
    for row in rows:
        row["is_active"] = "0" if row["churn"] == "1" else "1"
    ideal = [{k: v for k, v in r.items() if k != "is_active"} for r in rows]
    return Trap(
        case_id="t06_status_exported_today",
        scenario="T06",
        theme=4,
        description="The customer master was exported today, so is_active already says who left: it is 0 for every churner.",
        expected="is_active is not left among the columns the model learns from (it is known only after the outcome).",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "leak",
                "kind": "any_of",
                "label": "is_active (today's status) is out of the features",
                "checks": [
                    {"kind": "columns_absent", "columns": ["is_active"]},
                    {
                        "kind": "asked",
                        "text": [
                            "is_active.*(today|export|after)",
                            "after the outcome",
                            "known only after",
                            "leak",
                        ],
                    },
                ],
            }
        ],
        ideal_rows=ideal,
        ideal={"touched": ["is_active"]},
    )


def s05_cancellation_reason() -> Trap:
    rng = rng_for("s05_cancellation_reason")
    rows = customers(rng, [f"C{n:04d}" for n in range(1, 121)])
    for row in rows:
        row["cancellation_reason"] = (
            rng.choice(["price", "service", "moved", "competitor"]) if row["churn"] == "1" else ""
        )
    ideal = [{k: v for k, v in r.items() if k != "cancellation_reason"} for r in rows]
    return Trap(
        case_id="s05_cancellation_reason",
        scenario="S05",
        theme=4,
        description="cancellation_reason is filled in only for customers who left, so 'is it filled in' gives the answer away.",
        expected="cancellation_reason is dropped from the features (or the Doctor asks), even though it is blank for most rows.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "leak",
                "kind": "any_of",
                "label": "cancellation_reason is out of the features",
                "checks": [
                    {"kind": "columns_absent", "columns": ["cancellation_reason"]},
                    {
                        "kind": "asked",
                        "text": [
                            "cancellation_reason.*(left|churn|leak|only)",
                            "only for customers who left",
                            "filled in only",
                            "leak",
                        ],
                    },
                ],
            }
        ],
        ideal_rows=ideal,
        ideal={"touched": ["cancellation_reason"]},
    )


def t13_outcome_from_column() -> Trap:
    rng = rng_for("t13_outcome_from_column")
    rows = customers(rng, [f"C{n:04d}" for n in range(1, 121)])
    for row in rows:
        row["account_status"] = "Cancelled" if row["churn"] == "1" else "Active"
    ideal = [{k: v for k, v in r.items() if k != "account_status"} for r in rows]
    return Trap(
        case_id="t13_outcome_from_column",
        scenario="T13",
        theme=4,
        description="The churn flag was built from account_status, which stays in the file as an ordinary column.",
        expected="account_status, the column the outcome came from, is dropped from the features.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "leak",
                "kind": "any_of",
                "label": "account_status (the outcome's source) is out of the features",
                "checks": [
                    {"kind": "columns_absent", "columns": ["account_status"]},
                    {
                        "kind": "asked",
                        "text": [
                            "account_status.*(outcome|churn|built|same)",
                            "built from",
                            "same as the outcome",
                            "leak",
                        ],
                    },
                ],
            }
        ],
        ideal_rows=ideal,
        ideal={"touched": ["account_status"]},
    )


# ---------------------------------------------------------------------------
# 5. Outcome: who is eligible, and when the clock runs
# ---------------------------------------------------------------------------
def s01_never_active() -> Trap:
    rng = rng_for("s01_never_active")
    rows: list[Row] = []
    never: list[str] = []
    for n in range(1, 101):
        cid = f"C{n:04d}"
        if n <= 30:
            rows.append(
                {
                    "customer_id": cid,
                    "signup_date": day(n),
                    "last_order_date": "",
                    "orders_count": "0",
                    "churn": "1",
                }
            )
            never.append(cid)
        else:
            lapsed = rng.random() < 0.2
            last = AS_OF - timedelta(days=rng.randint(100, 200) if lapsed else rng.randint(1, 80))
            rows.append(
                {
                    "customer_id": cid,
                    "signup_date": day(n),
                    "last_order_date": last.isoformat(),
                    "orders_count": str(rng.randint(1, 12)),
                    "churn": "1" if lapsed else "0",
                }
            )
    active = [r for r in rows if r["customer_id"] not in never]
    rate = sum(r["churn"] == "1" for r in active) / len(active)
    return Trap(
        case_id="s01_never_active",
        scenario="S01",
        theme=5,
        description="Churn is 'no order in 90 days', so 30 customers who never ordered at all are labelled churners.",
        expected=f"The 30 never-active customers are not counted in the outcome; the true churn rate is {rate:.1%}, not 44%.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        rows_may_drop=never,
        checks=[
            {
                "category": "outcome_population",
                "kind": "any_of",
                "label": "never-active customers are not in the outcome population",
                "checks": [
                    {
                        "kind": "asked",
                        "text": [
                            "never (active|ordered|bought|used)",
                            "no orders",
                            "who counts",
                            "not active",
                        ],
                    },
                    {
                        "kind": "outcome_population",
                        "exclude_keys": never,
                        "positive": ["1"],
                        "expected_rate": rate,
                        "tolerance": 0.02,
                    },
                ],
            }
        ],
        ideal_rows=active,
        ideal={"asks": ["30 customers never ordered. Leave them out of churn?"], "outcome_column": "churn"},
    )


def t07_immature_last_month() -> Trap:
    rng = rng_for("t07_immature_last_month")
    rows: list[Row] = []
    n = 0
    for month in ("2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"):
        for _ in range(30):
            n += 1
            rate = 0.03 if month == "2026-09" else 0.25
            rows.append(
                {
                    "record_id": f"R{n:04d}",
                    "customer_id": f"C{rng.randint(1, 80):03d}",
                    "snapshot_month": month,
                    "plan": rng.choice(PLANS),
                    "churn": "1" if rng.random() < rate else "0",
                }
            )
    mature = [r for r in rows if r["snapshot_month"] != "2026-09"]
    rate_true = sum(r["churn"] == "1" for r in mature) / len(mature)
    return Trap(
        case_id="t07_immature_last_month",
        scenario="T07",
        theme=5,
        description="The newest month's outcome window has not finished, so its churn rate is 3% against about 25% before.",
        expected=f"The unfinished month (2026-09) is left out of the outcome, or the Doctor asks; the true rate is {rate_true:.1%}.",
        tables={"snapshots": rows},
        primary=["snapshots"],
        key="record_id",
        rows_may_drop=[r["record_id"] for r in rows if r["snapshot_month"] == "2026-09"],
        checks=[
            {
                "category": "outcome_population",
                "kind": "any_of",
                "label": "the unfinished last month is not counted as 'did not leave'",
                "checks": [
                    {
                        "kind": "asked",
                        "text": [
                            "not finished",
                            "window (has not|is not|isn't) (finished|over|closed)",
                            "latest month",
                            "most recent month",
                            "mature",
                            "incomplete month",
                            "still running",
                        ],
                    },
                    {
                        "kind": "outcome_population",
                        "exclude_where": [{"column": "snapshot_month", "values": ["2026-09"]}],
                        "positive": ["1"],
                        "expected_rate": rate_true,
                        "tolerance": 0.03,
                    },
                ],
            }
        ],
        ideal_rows=mature,
        ideal={"asks": ["The newest month is not finished yet. Leave it out?"], "outcome_column": "churn"},
    )


def c09_already_hold_product() -> Trap:
    rng = rng_for("c09_already_hold_product")
    rows: list[Row] = []
    for n in range(1, 121):
        holder = n % 5 in (0, 1)
        rows.append(
            {
                "id": f"P{n:04d}",
                "age": str(rng.randint(21, 70)),
                "vehicle_age": rng.choice(["new", "1-2 years", "old"]),
                "has_vehicle_policy": "Y" if holder else "N",
                "Response": "0" if holder else ("1" if rng.random() < 0.3 else "0"),
            }
        )
    open_rows = [r for r in rows if r["has_vehicle_policy"] == "N"]
    rate = sum(r["Response"] == "1" for r in open_rows) / len(open_rows)
    return Trap(
        case_id="c09_already_hold_product",
        scenario="C09",
        theme=5,
        goal="insurance-cross-sell",
        description="Two in five policyholders already hold the vehicle policy being cross-sold, so they can never 'buy' it.",
        expected=f"Existing holders are not counted in the outcome; the true response rate among the others is {rate:.1%}.",
        tables={"policyholders": rows},
        primary=["policyholders"],
        key="id",
        rows_may_drop=[r["id"] for r in rows if r["has_vehicle_policy"] == "Y"],
        checks=[
            {
                "category": "outcome_population",
                "kind": "any_of",
                "label": "customers who already hold the product are not in the outcome",
                "checks": [
                    {
                        "kind": "asked",
                        "text": [
                            "already hold",
                            "already has",
                            "already own",
                            "existing holder",
                            "has_vehicle_policy.*(outcome|leave|exclude|already)",
                        ],
                    },
                    {
                        "kind": "outcome_population",
                        "exclude_where": [{"column": "has_vehicle_policy", "values": ["Y"]}],
                        "positive": ["1"],
                        "expected_rate": rate,
                        "tolerance": 0.02,
                    },
                ],
            }
        ],
        ideal_rows=open_rows,
        ideal={
            "asks": ["Some customers already hold this policy. Leave them out?"],
            "outcome_column": "Response",
        },
    )


# ---------------------------------------------------------------------------
# 6. Dates read wrong without an error
# ---------------------------------------------------------------------------
def t01_mixed_date_order() -> Trap:
    rng = rng_for("t01_mixed_date_order")
    rows = customers(rng, [f"C{n:04d}" for n in range(1, 81)])
    for i, row in enumerate(rows):
        d = date(2025, 1, 1) + timedelta(days=rng.randint(0, 600))
        if i % 2:
            row["source_system"], row["signup_date"] = "B", f"{d.month:02d}/{d.day:02d}/{d.year}"
        else:
            row["source_system"], row["signup_date"] = "A", f"{d.day:02d}/{d.month:02d}/{d.year}"
    return Trap(
        case_id="t01_mixed_date_order",
        scenario="T01",
        theme=6,
        description="signup_date holds day/month in system A rows and month/day in system B rows, so 03/04/2026 can be two dates.",
        expected="The Doctor stops and asks the date order for each system, or leaves the dates as written; it never reads them all one way.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "unapproved_change",
                "kind": "any_of",
                "label": "dates are not silently read in one order",
                "checks": [
                    {
                        "kind": "asked",
                        "text": ["both orders", "both day", "mixed", "two date orders", "some rows are day"],
                    },
                    {"kind": "values_unchanged", "columns": ["signup_date"]},
                ],
            }
        ],
        ideal_rows=rows,
        ideal={
            "asks": ["Dates are written day first in some rows and month first in others. Which is which?"]
        },
    )


def t03_placeholder_dates() -> Trap:
    rng = rng_for("t03_placeholder_dates")
    rows = customers(rng, [f"C{n:04d}" for n in range(1, 81)])
    for i, row in enumerate(rows):
        row["last_payment_date"] = "1970-01-01" if i % 4 == 0 else day(rng.randint(0, 270))
    ideal = [
        dict(r, last_payment_date="" if r["last_payment_date"] == "1970-01-01" else r["last_payment_date"])
        for r in rows
    ]
    return Trap(
        case_id="t03_placeholder_dates",
        scenario="T03",
        theme=6,
        description="A quarter of the customers have last_payment_date 1970-01-01, the system's stand-in for 'never paid'.",
        expected="1970-01-01 is not kept as a real date: it becomes blank, or the Doctor asks what it means.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "unapproved_change",
                "kind": "any_of",
                "label": "the 1970-01-01 stand-in is not treated as a real date",
                "checks": [
                    {
                        "kind": "asked",
                        "text": ["1970", "placeholder", "never paid", "not a real date", "stand-in"],
                    },
                    {"kind": "no_value", "column": "last_payment_date", "values": ["1970-01-01"]},
                ],
            }
        ],
        ideal_rows=ideal,
        ideal={"touched": ["last_payment_date"]},
    )


def t15_partial_last_month() -> Trap:
    rng = rng_for("t15_partial_last_month")
    rows: list[Row] = []
    n = 0
    for month, days, scale in (
        ("2026-07", "31", 1.0),
        ("2026-08", "31", 1.0),
        ("2026-09", "30", 1.0),
        ("2026-10", "12", 0.4),
    ):
        for c in range(1, 31):
            n += 1
            rows.append(
                {
                    "record_id": f"U{n:04d}",
                    "customer_id": f"C{c:03d}",
                    "month": month,
                    "days_covered": days,
                    "minutes": str(int(rng.randint(200, 600) * scale)),
                    "churn": "1" if c % 5 == 0 else "0",
                }
            )
    full = [r for r in rows if r["month"] != "2026-10"]
    return Trap(
        case_id="t15_partial_last_month",
        scenario="T15",
        theme=5,
        description="The newest month, 2026-10, covers only 12 days, so its usage looks like a collapse for every customer.",
        expected="The partial month is left out, or the Doctor asks; it is never treated as a full month.",
        tables={"monthly_usage": rows},
        primary=["monthly_usage"],
        key="record_id",
        rows_may_drop=[r["record_id"] for r in rows if r["month"] == "2026-10"],
        checks=[
            {
                "category": "outcome_population",
                "kind": "any_of",
                "label": "the 12-day month is not used as a full month",
                "checks": [
                    {
                        "kind": "asked",
                        "text": [
                            "partial",
                            "12 days",
                            "incomplete month",
                            "not finished",
                            "full month",
                        ],
                    },
                    {"kind": "no_value", "column": "month", "values": ["2026-10"]},
                ],
            }
        ],
        ideal_rows=full,
        ideal={"asks": ["The last month has only 12 days. Leave it out?"]},
    )


# ---------------------------------------------------------------------------
# 7. Money and units
# ---------------------------------------------------------------------------
def v01_paise_vs_rupees() -> Trap:
    rng = rng_for("v01_paise_vs_rupees")
    a = [
        {
            "order_id": f"A{i:03d}",
            "customer_id": f"C{rng.randint(1, 40):03d}",
            "amount": str(rng.randint(100, 5000)),
            "churn": "0",
        }
        for i in range(40)
    ]
    b_rupees = [rng.randint(100, 5000) for _ in range(40)]
    b = [
        {
            "order_id": f"B{i:03d}",
            "customer_id": f"C{rng.randint(1, 40):03d}",
            "amount": str(v * 100),
            "churn": "0",
        }
        for i, v in enumerate(b_rupees)
    ]
    total = sum(int(r["amount"]) for r in a) + sum(b_rupees)
    ideal = a + [dict(r, amount=str(int(r["amount"]) // 100)) for r in b]
    return Trap(
        case_id="v01_paise_vs_rupees",
        scenario="V01",
        theme=7,
        description="The second file's amounts are in paise (100 times larger) and the first file's are in rupees.",
        expected=f"The two files are stacked in one unit and the total is {total:,} rupees, or the Doctor asks which unit each file uses.",
        tables={"sales_a": a, "sales_b": b},
        primary=["sales_a", "sales_b"],
        key="order_id",
        checks=[
            {
                "category": "double_counting",
                "kind": "any_of",
                "label": "amounts from both files add up in one unit (rupees)",
                "checks": [
                    {"kind": "asked", "text": ["paise", "rupee", "different (units?|scales?)", "100 times"]},
                    {"kind": "column_total", "columns": ["amount"], "expected_total": total},
                ],
            }
        ],
        ideal_rows=ideal,
        ideal={"touched": ["amount"], "asks": ["The second file looks like paise. Convert to rupees?"]},
    )


def v03_refunds_negative() -> Trap:
    rng = rng_for("v03_refunds_negative")
    rows = [
        {
            "txn_id": f"X{i:03d}",
            "customer_id": f"C{(i % 30) + 1:03d}",
            "amount": str(-rng.randint(100, 800) if i % 6 == 0 else rng.randint(200, 3000)),
            "churn": "0",
        }
        for i in range(60)
    ]
    return Trap(
        case_id="v03_refunds_negative",
        scenario="V03",
        theme=7,
        description="One transaction in six is negative: a refund, which is a strong signal that must not be clipped to zero.",
        expected="The negative amounts stay as they are (refunds), or the Doctor asks about them.",
        tables={"transactions": rows},
        primary=["transactions"],
        key="txn_id",
        checks=[
            {
                "category": "unapproved_change",
                "kind": "any_of",
                "label": "negative amounts (refunds) are not clipped or dropped",
                "checks": [
                    {"kind": "asked", "text": ["refund", "negative"]},
                    {"kind": "values_unchanged", "columns": ["amount"]},
                ],
            }
        ],
        ideal_rows=rows,
    )


def v04_blank_vs_zero() -> Trap:
    rng = rng_for("v04_blank_vs_zero")
    rows = customers(rng, [f"C{n:04d}" for n in range(1, 81)])
    for i, row in enumerate(rows):
        row["last_purchase_amount"] = "" if i % 3 == 0 else str(rng.randint(100, 2000))
    return Trap(
        case_id="v04_blank_vs_zero",
        scenario="V04",
        theme=7,
        description="last_purchase_amount is blank for customers who never bought; filling it with 0 makes them look like small buyers.",
        expected="Blank stays blank (never bought), or the Doctor asks what blank means.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "unapproved_change",
                "kind": "any_of",
                "label": "blank 'never bought' is not turned into 0",
                "checks": [
                    {
                        "kind": "asked",
                        "text": [
                            "blank (means?|for)",
                            "never bought",
                            "means zero",
                            "last_purchase_amount.*(blank|missing|zero)",
                        ],
                    },
                    {"kind": "values_unchanged", "columns": ["last_purchase_amount"]},
                ],
            }
        ],
        ideal_rows=rows,
    )


# ---------------------------------------------------------------------------
# 9. Consent, sensitive data and who may be contacted
# ---------------------------------------------------------------------------
AD = "targeted-advertisement"


def _ad_rows(ids: Sequence[str], rng: random.Random) -> list[Row]:
    return [
        {
            "customer_id": cid,
            "city": rng.choice(["Pune", "Delhi", "Chennai", "Kolkata"]),
            "monthly_spend": str(rng.randint(200, 2000)),
            "converted": "1" if rng.random() < 0.2 else "0",
        }
        for cid in ids
    ]


def c06_dnd_direction() -> Trap:
    rng = rng_for("c06_dnd_direction")
    rows = _ad_rows([f"C{n:04d}" for n in range(1, 81)], rng)
    for i, row in enumerate(rows):
        row["do_not_contact"] = "Y" if i % 3 == 0 else "N"
    no = [r["customer_id"] for r in rows if r["do_not_contact"] == "Y"]
    yes = [r["customer_id"] for r in rows if r["do_not_contact"] == "N"]
    ideal = [dict(r, contactable="0" if r["do_not_contact"] == "Y" else "1") for r in rows]
    return Trap(
        case_id="c06_dnd_direction",
        scenario="C06",
        theme=9,
        goal=AD,
        description="do_not_contact = Y means the customer must NOT be contacted; read as a consent flag, Y means 'may contact'.",
        expected="Everyone with do_not_contact = Y is off the contact list; everyone with N is on it.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "contact_list",
                "kind": "contact_list",
                "must_exclude": no,
                "must_include": yes,
                "label": "do-not-contact customers are off the list (the flag points the right way)",
            }
        ],
        ideal_rows=ideal,
        ideal={"consent_column": "contactable", "touched": ["contactable"]},
    )


def v13_consent_spellings() -> Trap:
    rng = rng_for("v13_consent_spellings")
    rows = _ad_rows([f"C{n:04d}" for n in range(1, 91)], rng)
    forms = ["Y", "N", "", "Haan", "nahi", "✓", "yes", "No", "Y", "N"]
    yes_forms = {"Y", "Haan", "✓", "yes"}
    for i, row in enumerate(rows):
        row["marketing_opt_in"] = forms[i % len(forms)]
    yes = [r["customer_id"] for r in rows if r["marketing_opt_in"] in yes_forms]
    no = [r["customer_id"] for r in rows if r["marketing_opt_in"] not in yes_forms]
    ideal = [dict(r, contactable="1" if r["marketing_opt_in"] in yes_forms else "0") for r in rows]
    return Trap(
        case_id="v13_consent_spellings",
        scenario="V13",
        theme=9,
        goal=AD,
        description="Consent is written Y, yes, Haan or a tick for agreed, N, nahi or No for refused, and left blank when unknown.",
        expected="Y/yes/Haan/tick customers are contactable; N/No/nahi and blank (unknown) customers are not.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "contact_list",
                "kind": "contact_list",
                "must_exclude": no,
                "must_include": yes,
                "label": "every spelling of yes is on the list; refused and unknown are off it",
            }
        ],
        ideal_rows=ideal,
        ideal={"consent_column": "contactable", "touched": ["contactable"]},
    )


def p7_minors() -> Trap:
    rng = rng_for("p7_minors")
    rows = _ad_rows([f"C{n:04d}" for n in range(1, 81)], rng)
    minors: list[str] = []
    for i, row in enumerate(rows):
        if i % 8 == 0:
            born = AS_OF - timedelta(days=365 * rng.randint(9, 16) + 40)
            minors.append(row["customer_id"])
        else:
            born = AS_OF - timedelta(days=365 * rng.randint(20, 65))
        row["date_of_birth"], row["marketing_opt_in"] = born.isoformat(), "Y"
    adults = [r["customer_id"] for r in rows if r["customer_id"] not in minors]
    ideal = [dict(r, contactable="0" if r["customer_id"] in minors else "1") for r in rows]
    return Trap(
        case_id="p7_minors",
        scenario="P7",
        theme=9,
        goal=AD,
        description="Ten customers are under 18 on the as-of date, and every customer has ticked marketing consent.",
        expected="Minors are off the contact list even though they have consent; adults with consent are on it.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "contact_list",
                "kind": "contact_list",
                "must_exclude": minors,
                "must_include": adults,
                "label": "customers under 18 are off the contact list",
            }
        ],
        ideal_rows=ideal,
        ideal={"consent_column": "contactable", "touched": ["contactable"]},
    )


def p8_deceased_fraud() -> Trap:
    rng = rng_for("p8_deceased_fraud")
    rows = _ad_rows([f"C{n:04d}" for n in range(1, 81)], rng)
    bad: list[str] = []
    for i, row in enumerate(rows):
        status = {0: "Deceased", 5: "Fraud", 10: "Legal hold", 15: "Closed"}.get(i % 20, "Active")
        row["account_status"], row["marketing_opt_in"] = status, "Y"
        if status != "Active":
            bad.append(row["customer_id"])
    good = [r["customer_id"] for r in rows if r["customer_id"] not in bad]
    ideal = [dict(r, contactable="0" if r["customer_id"] in bad else "1") for r in rows]
    return Trap(
        case_id="p8_deceased_fraud",
        scenario="P8",
        theme=9,
        goal=AD,
        description="Some customers are deceased, flagged for fraud, under legal hold or closed, yet all show marketing consent.",
        expected="Deceased, fraud, legal-hold and closed customers are off the contact list.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "contact_list",
                "kind": "contact_list",
                "must_exclude": bad,
                "must_include": good,
                "label": "deceased, fraud, legal-hold and closed customers are off the list",
            }
        ],
        ideal_rows=ideal,
        ideal={"consent_column": "contactable", "touched": ["contactable"]},
    )


# ---------------------------------------------------------------------------
# 11. Test, staff and extreme accounts
# ---------------------------------------------------------------------------
def v06_test_accounts() -> Trap:
    rng = rng_for("v06_test_accounts")
    ids = [f"TEST{n:03d}" for n in range(1, 9)] + [f"C{n:04d}" for n in range(1, 73)]
    rows = _ad_rows(ids, rng)
    test: list[str] = []
    for row in rows:
        is_test = row["customer_id"].startswith("TEST")
        row["email"] = (
            f"qa+{row['customer_id'].lower()}@company.example"
            if is_test
            else f"{row['customer_id'].lower()}@mail.example"
        )
        row["marketing_opt_in"] = "Y"
        if is_test:
            test.append(row["customer_id"])
    real = [r["customer_id"] for r in rows if r["customer_id"] not in test]
    ideal = [dict(r, contactable="0" if r["customer_id"] in test else "1") for r in rows]
    return Trap(
        case_id="v06_test_accounts",
        scenario="V06",
        theme=11,
        goal=AD,
        description="Eight QA accounts (TEST001..., qa+...@company.example) sit in the customer list with marketing consent.",
        expected="The test accounts are off the contact list; the real customers are on it.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "contact_list",
                "kind": "contact_list",
                "must_exclude": test,
                "must_include": real,
                "label": "test accounts are off the contact list",
            }
        ],
        ideal_rows=ideal,
        ideal={"consent_column": "contactable", "touched": ["contactable"]},
    )


# ---------------------------------------------------------------------------
# 8. Who one row is
# ---------------------------------------------------------------------------
def k13_household() -> Trap:
    rng = rng_for("k13_household")
    rows: list[Row] = []
    for h in range(1, 21):
        for m in range(3):
            rows.append(
                {
                    "customer_id": f"C{h:03d}{m}",
                    "household_id": f"H{h:03d}",
                    "city": rng.choice(["Pune", "Delhi"]),
                    "monthly_spend": str(rng.randint(200, 2000)),
                    "converted": "1" if rng.random() < 0.2 else "0",
                    "marketing_opt_in": "Y",
                }
            )
    first = {f"C{h:03d}0" for h in range(1, 21)}
    ideal = [dict(r, contactable="1" if r["customer_id"] in first else "0") for r in rows]
    return Trap(
        case_id="k13_household",
        scenario="K13",
        theme=8,
        goal=AD,
        description="Twenty households have three members each with consent; texting everyone sends three messages to one household.",
        expected="At most one person per household is on the contact list.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "contact_list",
                "kind": "contact_list",
                "max_one_per": "household_id",
                "label": "no household gets more than one message",
            }
        ],
        ideal_rows=ideal,
        ideal={"consent_column": "contactable", "touched": ["contactable"]},
    )


# ---------------------------------------------------------------------------
# 10. Spreadsheet and export shapes
# ---------------------------------------------------------------------------
def f04_merged_cells_region() -> Trap:
    rng = rng_for("f04_merged_cells_region")
    rows: list[Row] = []
    full: list[Row] = []
    for r_i, region in enumerate(("North", "South", "East", "West")):
        for k in range(15):
            cid = f"C{r_i * 15 + k + 1:04d}"
            base = {
                "customer_id": cid,
                "plan": rng.choice(PLANS),
                "churn": "1" if rng.random() < 0.25 else "0",
            }
            rows.append(
                {
                    "customer_id": cid,
                    "region": region if k == 0 else "",
                    "plan": base["plan"],
                    "churn": base["churn"],
                }
            )
            full.append({"customer_id": cid, "region": region, "plan": base["plan"], "churn": base["churn"]})
    return Trap(
        case_id="f04_merged_cells_region",
        scenario="F04",
        theme=10,
        description="Region is written once per block (a merged cell) and left blank on the 14 rows below it.",
        expected="Region is filled down to every row of its block, or the Doctor asks whether a blank means 'same as above'.",
        tables={"customers": rows},
        primary=["customers"],
        key="customer_id",
        checks=[
            {
                "category": "unapproved_change",
                "kind": "any_of",
                "label": "the 56 blank regions are filled or asked about",
                "checks": [
                    {
                        "kind": "asked",
                        "text": [
                            "same as (the row )?above",
                            "merged cells?",
                            "fill(ed)? down",
                            "blank region",
                        ],
                    },
                    {"kind": "no_value", "column": "region", "values": [""]},
                ],
            }
        ],
        ideal_rows=full,
        ideal={"touched": ["region"], "asks": ["Does a blank region mean 'same as the row above'?"]},
    )


def f05_pivot_summary() -> Trap:
    rng = rng_for("f05_pivot_summary")
    rows: list[Row] = []
    for region in ("North", "South", "East"):
        for plan in PLANS:
            rows.append(
                {
                    "region": region,
                    "plan": plan,
                    "customers": str(rng.randint(100, 900)),
                    "total_spend": str(rng.randint(10000, 90000)),
                }
            )
    rows.append(
        {
            "region": "Grand Total",
            "plan": "",
            "customers": str(sum(int(r["customers"]) for r in rows)),
            "total_spend": str(sum(int(r["total_spend"]) for r in rows)),
        }
    )
    return Trap(
        case_id="f05_pivot_summary",
        scenario="F05",
        theme=10,
        description="The file is a pivot table pasted as values: counts per region and plan, a Grand Total row and no customer ID.",
        expected="The Doctor says this is a summary, not customers, and asks for the row-level export; it invents no rows.",
        tables={"report": rows},
        primary=["report"],
        key="region",
        checks=[
            {
                "category": "asked",
                "kind": "asked",
                "label": "says it is a summary and asks for one row per customer",
                "text": ["summary", "pivot", "row-level", "grand total", "not customers", "totals, not"],
            }
        ],
        ideal={
            "asks": ["This is a summary report, not one row per customer. Please send the row-level export."],
            "no_universal": ["row_loss", "unapproved_change"],
        },
        ideal_rows=rows,
    )


# ---------------------------------------------------------------------------
# 12. Campaign history
# ---------------------------------------------------------------------------
def c01_selection_not_random() -> Trap:
    rng = rng_for("c01_selection_not_random")
    rows: list[Row] = []
    for n in range(1, 121):
        balance = rng.randint(1000, 100000)
        contacted = balance > 60000
        converted = rng.random() < (0.12 if contacted else 0.03)
        rows.append(
            {
                "customer_id": f"C{n:04d}",
                "balance": str(balance),
                "contacted": "Y" if contacted else "N",
                "converted": "1" if converted else "0",
            }
        )
    return Trap(
        case_id="c01_selection_not_random",
        scenario="C01",
        theme=12,
        goal=AD,
        description="Staff texted only high-balance customers (12% bought against 3% of the rest); there is no random control group.",
        expected="The Doctor asks whether anyone was held out at random; without that it never calls the gap an effect of the text.",
        tables={"campaign_history": rows},
        primary=["campaign_history"],
        key="customer_id",
        checks=[
            {
                "category": "asked",
                "kind": "asked",
                "label": "asks whether a random group was left uncontacted",
                "text": [
                    "random",
                    "holdout",
                    "hold-out",
                    "control group",
                    "chosen by",
                    "who was contacted",
                    "selected (at random|for contact)",
                ],
            }
        ],
        ideal={"asks": ["Was a random group deliberately not contacted?"]},
        ideal_rows=rows,
    )


CASES: tuple[Callable[[], Trap], ...] = (
    crit03_dup_and_drop,
    k1_merged_prefixes,
    k3_shared_phone,
    f01_leading_zeros,
    f02_excel_e_plus,
    v19_order_total_repeated,
    f21_overlapping_exports,
    k15_failed_payments,
    k16_ticket_status_log,
    t06_status_exported_today,
    s05_cancellation_reason,
    t13_outcome_from_column,
    s01_never_active,
    t07_immature_last_month,
    c09_already_hold_product,
    t01_mixed_date_order,
    t03_placeholder_dates,
    t15_partial_last_month,
    v01_paise_vs_rupees,
    v03_refunds_negative,
    v04_blank_vs_zero,
    c06_dnd_direction,
    v13_consent_spellings,
    p7_minors,
    p8_deceased_fraud,
    k13_household,
    v06_test_accounts,
    f04_merged_cells_region,
    f05_pivot_summary,
    c01_selection_not_random,
)


def render(trap: Trap) -> dict[str, str]:
    """The files of one case: file name -> text."""
    files = {f"{name}.csv": csv_text(rows) for name, rows in trap.tables.items()}
    doc: dict[str, Any] = {
        "id": trap.case_id,
        "scenario": trap.scenario,
        "theme": THEMES[trap.theme],
        "description": trap.description,
        "expected": trap.expected,
        "goal": trap.goal,
        "files": {name: f"{name}.csv" for name in trap.tables},
        "primary": trap.primary,
        "key": trap.key,
        "answers": trap.answers,
        "rows_may_drop": trap.rows_may_drop,
        "checks": trap.checks,
        "ideal": dict(trap.ideal, **({"file": "ideal.csv"} if trap.ideal_rows is not None else {})),
    }
    files["case.json"] = json.dumps(doc, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    if trap.ideal_rows is not None:
        files["ideal.csv"] = csv_text(trap.ideal_rows)
    return files


def all_files() -> dict[str, dict[str, str]]:
    return {t.case_id: render(t) for t in (make() for make in CASES)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check", action="store_true", help="compare with the files on disk instead of writing"
    )
    args = parser.parse_args(argv)
    stale: list[str] = []
    for case_id, files in all_files().items():
        folder = HERE / case_id
        for name, text in files.items():
            path = folder / name
            if args.check:
                if not path.exists() or path.read_text(encoding="utf-8") != text:
                    stale.append(f"{case_id}/{name}")
            else:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
    if stale:
        print("out of date:", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
