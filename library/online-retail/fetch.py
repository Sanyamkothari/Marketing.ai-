"""Fetch the UCI Online Retail transactions and aggregate them to one row per customer.

This is the only dataset in the library that does **not** arrive in the data contract's shape. The
published file is a transaction log - 541,909 invoice lines - and the contract wants one row per
customer with a primary key and a target. Building that row is Phase 2's job inside the engine
(plan section 12, "automated aggregation"); until Phase 2 lands, this script does it here, in the
open, and `README.md` explains every column it produces.

WHAT THIS SCRIPT DECIDES, AND WHY
---------------------------------
A win-back campaign asks one question: *of the customers who have stopped buying, which ones will
come back?* Answering it from a transaction log needs three dates and one rule:

    SNAPSHOT       2011-09-10   the day the campaign would be planned
    LAPSE WINDOW   90 days      a customer is "lapsed" at the snapshot if their last purchase was
                                at least 90 whole days earlier, i.e. at or before
                                2011-06-12 00:00 (a purchase later that day counts as 89 days)
    OUTCOME WINDOW 90 days      `reactivated_90d` is 1 when they bought again in
                                (2011-09-10, 2011-12-09]

The snapshot is the last day in the file minus 90, so the outcome window is complete for every
customer: nobody is scored as "did not come back" only because the file ends.

**Every feature is computed from invoice lines dated on or before the snapshot.** Nothing after it
is read except the target itself. That is the whole leakage argument for this file, and it is
enforced by the `assert` at the end of `build_rows`.

The aggregates are plain counts, sums, means and date differences over the customer's own
invoices - the arithmetic the engine cannot do because it never sees more than one row per entity.
No ratio, no binning, no encoding, no scaling: those the engine does itself.

    python library/online-retail/fetch.py
    python library/online-retail/fetch.py --no-download

Licence: see LICENSE.txt next to this file.
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "online-retail-dataset.csv"
PREPARED = DATA / "prepared.csv"
SAMPLE = HERE / "sample.csv"

# UCI's own host is not reachable from every network; this copy, shipped with Databricks'
# "Spark: The Definitive Guide", is the identical 541,909-line UCI Online Retail CSV.
SOURCE_URL = (
    "https://raw.githubusercontent.com/databricks/Spark-The-Definitive-Guide/master/"
    "data/retail-data/all/online-retail-dataset.csv"
)
UPSTREAM_URL = "https://archive.ics.uci.edu/dataset/352/online+retail"

PRIMARY_KEY = "customer_id"
TARGET = "reactivated_90d"

SNAPSHOT = pd.Timestamp("2011-09-10")
LAPSE_DAYS = 90
OUTCOME_DAYS = 90
OUTCOME_END = SNAPSHOT + pd.Timedelta(days=OUTCOME_DAYS)

SAMPLE_ROWS = 5_000
SAMPLE_SEED = 20260922


def download() -> None:
    """Put the raw transaction log in `data/` (~45 MB)."""
    DATA.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SOURCE_URL) as response, RAW.open("wb") as handle:
        handle.write(response.read())


def read_transactions() -> pd.DataFrame:
    """The raw log with dates parsed and the rows that cannot belong to a customer removed."""
    frame = pd.read_csv(RAW)
    frame["InvoiceDate"] = pd.to_datetime(frame["InvoiceDate"], format="%m/%d/%Y %H:%M")

    # 135,080 lines have no CustomerID (guest checkout). They cannot be attributed to a customer,
    # so they cannot contribute to a customer's row. They are dropped, not imputed.
    frame = frame[frame["CustomerID"].notna()].copy()
    frame["CustomerID"] = frame["CustomerID"].astype(int).astype(str)

    # `Description` is free-text product copy. It is not needed by any aggregate below and is the
    # only column that could carry stray personal data, so it is dropped here rather than later.
    frame = frame.drop(columns=["Description"])

    frame["line_value"] = frame["Quantity"] * frame["UnitPrice"]
    # An invoice number beginning with "C" is a cancellation; its Quantity is negative.
    frame["is_cancellation"] = frame["InvoiceNo"].astype(str).str.startswith("C")
    return frame


def build_rows(transactions: pd.DataFrame) -> pd.DataFrame:
    """One row per lapsed customer: aggregates up to the snapshot, plus the outcome after it."""
    before = transactions[transactions["InvoiceDate"] <= SNAPSHOT]
    after = transactions[
        (transactions["InvoiceDate"] > SNAPSHOT) & (transactions["InvoiceDate"] <= OUTCOME_END)
    ]

    # Purchases only, for the aggregates that describe buying. Cancellations and the handful of
    # zero/negative-price lines (postage adjustments, manual corrections) are counted separately.
    purchases = before[(~before["is_cancellation"]) & (before["Quantity"] > 0) & (before["UnitPrice"] > 0)]
    returns = before[before["is_cancellation"]]

    # --- per-invoice totals, so "order value" means the value of a whole order -----------------
    orders = (
        purchases.groupby(["CustomerID", "InvoiceNo"])
        .agg(order_value=("line_value", "sum"), order_date=("InvoiceDate", "min"))
        .reset_index()
    )

    per_customer = orders.groupby("CustomerID").agg(
        orders_total=("InvoiceNo", "nunique"),
        first_order_at=("order_date", "min"),
        last_order_at=("order_date", "max"),
        spend_total=("order_value", "sum"),
        order_value_mean=("order_value", "mean"),
        order_value_max=("order_value", "max"),
    )

    per_customer["items_total"] = purchases.groupby("CustomerID")["Quantity"].sum()
    per_customer["distinct_products"] = purchases.groupby("CustomerID")["StockCode"].nunique()
    per_customer["active_months"] = purchases.groupby("CustomerID")["InvoiceDate"].apply(
        lambda s: s.dt.to_period("M").nunique()
    )
    per_customer["country"] = purchases.groupby("CustomerID")["Country"].agg(
        lambda s: s.mode().iat[0]  # a customer can appear under more than one country; take the usual one
    )

    # Returns: how often this customer sent something back before the snapshot.
    per_customer["returned_orders"] = (
        returns.groupby("CustomerID")["InvoiceNo"].nunique().reindex(per_customer.index).fillna(0).astype(int)
    )
    per_customer["returned_items"] = (
        returns.groupby("CustomerID")["Quantity"]
        .sum()
        .abs()
        .reindex(per_customer.index)
        .fillna(0)
        .astype(int)
    )

    # --- dates turned into day counts, which is what a model can read ---------------------------
    per_customer["recency_days"] = (SNAPSHOT - per_customer["last_order_at"]).dt.days
    per_customer["tenure_days"] = (SNAPSHOT - per_customer["first_order_at"]).dt.days
    # Mean gap between consecutive orders; 0 when the customer only ever placed one order.
    span = (per_customer["last_order_at"] - per_customer["first_order_at"]).dt.days
    gaps = per_customer["orders_total"] - 1
    per_customer["days_between_orders_mean"] = (span / gaps.where(gaps > 0)).fillna(0).round(2)

    # --- the win-back audience: lapsed at the snapshot ------------------------------------------
    lapsed = per_customer[per_customer["recency_days"] >= LAPSE_DAYS].copy()

    # --- the outcome, and the ONLY thing read from after the snapshot ---------------------------
    reactivated = set(after[(~after["is_cancellation"]) & (after["Quantity"] > 0)]["CustomerID"].unique())
    lapsed[TARGET] = lapsed.index.isin(reactivated).astype(int)

    rows = lapsed.reset_index().rename(columns={"CustomerID": PRIMARY_KEY})
    rows["snapshot_date"] = SNAPSHOT.date().isoformat()

    columns = [
        PRIMARY_KEY,
        "snapshot_date",
        "country",
        "recency_days",
        "tenure_days",
        "orders_total",
        "items_total",
        "distinct_products",
        "active_months",
        "spend_total",
        "order_value_mean",
        "order_value_max",
        "days_between_orders_mean",
        "returned_orders",
        "returned_items",
        TARGET,
    ]
    rows = rows[columns]
    for name in ("spend_total", "order_value_mean", "order_value_max"):
        rows[name] = rows[name].round(2)

    # The leakage argument, checked rather than asserted in prose: every row's last purchase is at
    # least LAPSE_DAYS before the snapshot, so no feature can have seen the outcome window.
    assert (rows["recency_days"] >= LAPSE_DAYS).all()
    assert rows[PRIMARY_KEY].is_unique
    return rows


def write_sample(frame: pd.DataFrame) -> pd.DataFrame:
    sample = frame.sample(n=min(SAMPLE_ROWS, len(frame)), random_state=SAMPLE_SEED).sort_index()
    sample.to_csv(SAMPLE, index=False, lineterminator="\n")
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-download", action="store_true", help="use the file already in data/")
    args = parser.parse_args()

    if not args.no_download:
        download()
    transactions = read_transactions()
    rows = build_rows(transactions)
    rows.to_csv(PREPARED, index=False, lineterminator="\n")
    sample = write_sample(rows)

    print(f"upstream        {UPSTREAM_URL}")
    print(f"invoice lines   {len(transactions):,} (after dropping guest-checkout lines)")
    print(f"snapshot        {SNAPSHOT.date()}  lapse {LAPSE_DAYS}d  outcome to {OUTCOME_END.date()}")
    print(f"rows            {len(rows):,} lapsed customers")
    print(f"columns         {len(rows.columns)}")
    print(
        f"target          {TARGET}: {rows[TARGET].value_counts().to_dict()} "
        f"({rows[TARGET].mean():.1%} positive)"
    )
    print(f"prepared        {PREPARED}")
    print(f"sample          {SAMPLE} ({len(sample):,} rows, {int(sample[TARGET].sum()):,} positive)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
