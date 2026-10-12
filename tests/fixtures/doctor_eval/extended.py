"""The extended messy benchmark's new cases (Plan O M248; the design §12 "Messy-file benchmark").

Plan L's thirty files (`scripts.plan_l_benchmark.cases`) plus the cases the design adds: lakh and crore, `haan/nahi`,
GSTIN, mixed currency, a dollar file, a multi-valued outcome, and two multi-file cases (overlapping monthly exports;
two per-customer files). These are **tuning** files: rules may be changed because of them. The held-out Indian
files are elsewhere (`heldout_indian.py`) and never tuned on.

Each single-file case is one of Targeted Advertisement's own generated files (`tests.fixtures.make_data`, no client
data) with one change. A case that needs a tool not built yet names it (`needs`); the harness reports it as waiting
for that tool and never runs it with a stand-in.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import pandas as pd

from tests.fixtures.make_data import GenerationSpec, generate

__all__ = ["EXTENDED", "MULTI_FILE", "ExtendedCase", "MultiFileCase", "case_bytes", "case_frame"]

USE_CASE: Final[str] = "targeted-advertisement"
ROWS: Final[int] = 2_000


@dataclass(frozen=True)
class ExtendedCase:
    name: str
    change: Callable[[pd.DataFrame], pd.DataFrame]
    needs: tuple[str, ...] = ()
    """Catalogue tools the case needs that a later milestone builds: it waits for them."""
    use_case: str = USE_CASE
    seed: int = 0


def _lakh_crore(frame: pd.DataFrame) -> pd.DataFrame:
    words = ["₹2.5 L", "1.2 Cr", "Rs 3,50,000/-", "12K", "₹ 75,000", "4.75 lakh"]
    return frame.assign(annual_spend=[words[i % len(words)] for i in range(len(frame))])


def _haan_nahi(frame: pd.DataFrame) -> pd.DataFrame:
    yes = frame["marketing_opt_in"].astype(str).str.lower().eq("true")
    return frame.assign(marketing_opt_in=["haan" if v else "nahi" for v in yes])


def _gstin(frame: pd.DataFrame) -> pd.DataFrame:
    values = ["27AAPFU0939F1ZV", "", "29AAGCB7383J1Z4", "", "invalid-gst"]
    return frame.assign(business_gstin=[values[i % len(values)] for i in range(len(frame))])


def _mixed_currency(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(
        order_value=[f"₹{1_200 + i}" if i % 3 else f"${15 + i % 40}" for i in range(len(frame))]
    )


def _dollar_file(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(order_value=[f"${15 + (i * 7) % 400:,.2f}" for i in range(len(frame))])


def _multi_valued_outcome(frame: pd.DataFrame) -> pd.DataFrame:
    target = frame["converted_30d"]
    status = [
        "Churned" if v == 1 else ("Suspended" if i % 9 == 0 else "Active") for i, v in enumerate(target)
    ]
    return frame.drop(columns=["converted_30d"]).assign(status=status)


EXTENDED: Final[tuple[ExtendedCase, ...]] = (
    ExtendedCase("x01_lakh_crore", _lakh_crore, seed=21),
    ExtendedCase("x02_haan_nahi", _haan_nahi, seed=22),
    ExtendedCase("x03_gstin", _gstin, seed=23),
    ExtendedCase("x04_mixed_currency", _mixed_currency, needs=("convert_currency",), seed=24),
    ExtendedCase("x05_dollar_file", _dollar_file, needs=("convert_currency",), seed=25),
    ExtendedCase("x06_multi_valued_outcome", _multi_valued_outcome, needs=("map_outcome_values",), seed=26),
)
"""The single-file extended cases, in report order."""


def case_frame(case: ExtendedCase) -> pd.DataFrame:
    """The case's data, before it is written."""
    frame = generate(GenerationSpec(use_case_id=case.use_case, rows=ROWS, variant="clean", seed=case.seed))
    return case.change(frame)


def case_bytes(case: ExtendedCase) -> tuple[str, bytes, str]:
    """`(file name, bytes, content type)`: the case written as a CSV export."""
    frame = case_frame(case)
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow([str(c) for c in frame.columns])
    for row in frame.itertuples(index=False):
        writer.writerow(["" if pd.isna(v) else v for v in row])
    return f"{case.name}.csv", text.getvalue().encode("utf-8"), "text/csv"


@dataclass(frozen=True)
class MultiFileCase:
    """A case of several files, evaluated through the registry's tools (M237): every row in and out counted."""

    name: str
    tool: str
    frames: Callable[[], tuple[pd.DataFrame, ...]]
    needs: tuple[str, ...] = ()


def _monthly(start: int, rows: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C-{10_000 + (i + start) % 1_500:05d}" for i in range(rows)],
            "bill_date": [
                f"2025-{1 + (i + start) // 300 % 12:02d}-{1 + (i + start) % 27:02d}" for i in range(rows)
            ],
            "amount": [round(100 + ((i + start) * 13) % 700 + 0.5, 2) for i in range(rows)],
        }
    )


def _overlapping_exports() -> tuple[pd.DataFrame, ...]:
    """Three monthly exports, each repeating the previous one's last 300 rows."""
    return (_monthly(0, 900), _monthly(600, 900), _monthly(1_200, 900))


def _two_customer_files() -> tuple[pd.DataFrame, ...]:
    customers = generate(GenerationSpec(use_case_id=USE_CASE, rows=1_500, variant="clean", seed=31))
    extra = pd.DataFrame(
        {
            "customer_id": customers["customer_id"].iloc[::2].reset_index(drop=True),
            "loyalty_points": [i * 10 % 900 for i in range(len(customers.iloc[::2]))],
        }
    )
    return (customers, extra)


MULTI_FILE: Final[tuple[MultiFileCase, ...]] = (
    MultiFileCase("m01_overlapping_monthly_exports", "stack_files", _overlapping_exports),
    MultiFileCase("m02_two_per_customer_files", "join_files", _two_customer_files),
)
