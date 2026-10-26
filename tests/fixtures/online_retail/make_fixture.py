"""Regenerate the UCI Online Retail fixtures from the full log (v3 phase 0, B7).

The two files next to this script are samples of the real UCI Online Retail log (541,909 invoice lines of a UK gift
retailer, 2010-12-01 to 2011-12-09), CC BY 4.0, credited in `ATTRIBUTION.txt` and `library/online-retail/LICENSE.txt`.
The full file is 45 MB and is never committed. To regenerate, download it once and run this script on it:

    curl -L -o /tmp/online-retail-dataset.csv \\
        https://raw.githubusercontent.com/databricks/Spark-The-Definitive-Guide/master/data/retail-data/all/online-retail-dataset.csv
    python -m tests.fixtures.online_retail.make_fixture /tmp/online-retail-dataset.csv

(`--check` regenerates in memory and says whether the committed files are byte-for-byte what the script gives.)

* `online_retail_sample.csv.gz` - sampled **by customer**: `CUSTOMERS` of the 4,372 customers, seeded, each with every
  one of their lines, so a shopper's history is whole (a sample by line would cut orders and histories in pieces and
  say nothing true about recency or spend). The guest checkouts (whole orders with no `CustomerID`, 135,080 lines of
  the full file) are sampled in proportion, by order, so about the same share of lines has no customer as in the full
  file. On the lapsed audience this sample gives 1,190 rows and 413 positives over six month-ends (measured by
  `tests/integration/test_order_log_workflow.py`), which clears the engine's minimums (1,000 rows, 200 positives)
  without touching them.
* `online_retail_small.csv.gz` - a slice cut from the sample, again by customer: shoppers' lines up to `SMALL_LINES`
  (3,000; a shopper whose history does not fit is skipped, never cut), plus the guest checkouts in proportion. It cannot
  reach the minimums and must come out `ROWS_TOO_FEW`.

The rows are written as the log has them (every cell text, `CustomerID` as `17850`, the date as `12/1/2010 8:26`),
gzipped with a fixed timestamp so the bytes are the same every run.
"""

from __future__ import annotations

import argparse
import gzip
import io
import sys
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

HERE: Final[Path] = Path(__file__).resolve().parent
SAMPLE: Final[Path] = HERE / "online_retail_sample.csv.gz"
SMALL: Final[Path] = HERE / "online_retail_small.csv.gz"

FULL_ROWS: Final[int] = 541_909
FULL_CUSTOMERS: Final[int] = 4_372
COLUMNS: Final[tuple[str, ...]] = (
    "InvoiceNo",
    "StockCode",
    "Description",
    "Quantity",
    "InvoiceDate",
    "UnitPrice",
    "CustomerID",
    "Country",
)
SEED: Final[int] = 20261026
CUSTOMERS: Final[int] = 1_000
SMALL_LINES: Final[int] = 3_000


def read_full(path: Path) -> pd.DataFrame:
    """The full log, every cell as the file has it (text, blanks kept as empty strings); refuses anything else."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    if tuple(frame.columns) != COLUMNS:
        raise SystemExit(f"{path} has columns {list(frame.columns)}, not {list(COLUMNS)}.")
    if len(frame) != FULL_ROWS:
        raise SystemExit(f"{path} has {len(frame):,} data rows, not the {FULL_ROWS:,} of UCI Online Retail.")
    if frame.loc[frame["CustomerID"] != "", "CustomerID"].nunique() != FULL_CUSTOMERS:
        raise SystemExit(f"{path} does not have the {FULL_CUSTOMERS:,} customers of UCI Online Retail.")
    return frame


def sample_by_customer(frame: pd.DataFrame, customers: list[str], guest_orders: list[str]) -> pd.DataFrame:
    """The lines of `customers`, and of the guest-checkout orders `guest_orders`, in the file's own order."""
    keep = frame["CustomerID"].isin(customers) | (
        (frame["CustomerID"] == "") & frame["InvoiceNo"].isin(guest_orders)
    )
    return frame.loc[keep]


def guest_order_numbers(frame: pd.DataFrame) -> list[str]:
    """Order numbers whose lines all have no customer. UCI has no order with lines both with and without one."""
    named = frame.loc[frame["CustomerID"] != "", "InvoiceNo"]
    blank = frame.loc[frame["CustomerID"] == "", "InvoiceNo"]
    if set(named) & set(blank):
        raise SystemExit("An order has lines with and without a CustomerID; the fixture assumes none does.")
    return sorted(set(blank))


def build(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`(sample, small)`: the by-customer sample and the small slice cut from it, both seeded."""
    rng = np.random.default_rng(SEED)
    everyone = sorted(set(frame.loc[frame["CustomerID"] != "", "CustomerID"]))
    guests = guest_order_numbers(frame)
    share = CUSTOMERS / len(everyone)
    chosen = sorted(rng.choice(everyone, size=CUSTOMERS, replace=False).tolist())
    chosen_guests = sorted(rng.choice(guests, size=round(len(guests) * share), replace=False).tolist())
    sample = sample_by_customer(frame, chosen, chosen_guests)

    # The small slice: customers in a seeded order, each taken if their lines still fit in SMALL_LINES (a shopper with
    # a long history is skipped, not cut), guests in proportion.
    lines = sample.loc[sample["CustomerID"] != ""].groupby("CustomerID").size()
    order = rng.permutation(np.asarray(chosen, dtype=object)).tolist()
    small_customers: list[str] = []
    total = 0
    for customer in order:
        if total + int(lines[customer]) <= SMALL_LINES:
            small_customers.append(customer)
            total += int(lines[customer])
    small_share = len(small_customers) / len(chosen)
    small_guests = sorted(
        rng.choice(chosen_guests, size=round(len(chosen_guests) * small_share), replace=False).tolist()
    )
    return sample, sample_by_customer(frame, small_customers, small_guests)


def gzipped_csv(frame: pd.DataFrame) -> bytes:
    """The frame as CSV text, gzipped with no name and a zero timestamp: the same bytes every run."""
    text = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as handle:
        handle.write(text)
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("full", type=Path, help="the full online-retail-dataset.csv (541,909 data rows)")
    parser.add_argument(
        "--check", action="store_true", help="compare with the committed files instead of writing"
    )
    args = parser.parse_args(argv)
    sample, small = build(read_full(args.full))
    made = {SAMPLE: gzipped_csv(sample), SMALL: gzipped_csv(small)}
    for path, data in made.items():
        if args.check:
            same = path.exists() and path.read_bytes() == data
            print(f"{path.name}: {'identical' if same else 'DIFFERENT'}")
            if not same:
                return 1
        else:
            path.write_bytes(data)
            print(f"{path.name}: {len(data):,} bytes gzipped")
    named = sample.loc[sample["CustomerID"] != ""]
    print(
        f"sample: {len(sample):,} lines, {named['CustomerID'].nunique():,} customers, "
        f"{(sample['CustomerID'] == '').sum():,} lines with no CustomerID"
    )
    print(
        f"small: {len(small):,} lines, {small.loc[small['CustomerID'] != '', 'CustomerID'].nunique():,} customers"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
