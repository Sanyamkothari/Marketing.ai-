"""Fetch the IBM/Kaggle Telco Customer Churn file and write the sample the tests use.

The source file is **already** in the data contract's shape: one row per subscriber, a unique
`customerID`, and a `Churn` column holding the outcome. Nothing is derived, nothing is renamed and
no column is dropped, so `prepared.csv` is a byte-for-byte copy of the download apart from the line
terminator pandas writes. That is the point of this dataset in the library: it is the one that
needed no preparation at all.

    python library/telco-customer-churn/fetch.py            # download + prepare + sample
    python library/telco-customer-churn/fetch.py --no-download

Licence: see LICENSE.txt next to this file.
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "Telco-Customer-Churn.csv"
PREPARED = DATA / "prepared.csv"
SAMPLE = HERE / "sample.csv"

# The IBM repository that publishes the file behind the Kaggle "Telco Customer Churn" dataset.
# Kaggle itself needs an account, so the library fetches IBM's own copy of the same 7,043 rows.
SOURCE_URL = (
    "https://raw.githubusercontent.com/IBM/telco-customer-churn-on-icp4d/master/data/"
    "Telco-Customer-Churn.csv"
)

PRIMARY_KEY = "customerID"
TARGET = "Churn"
SAMPLE_ROWS = 5_000
SAMPLE_SEED = 20260922


def download() -> None:
    """Put the raw CSV in `data/`. The file is ~0.9 MB, so it is fetched whole."""
    DATA.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SOURCE_URL) as response, RAW.open("wb") as handle:
        handle.write(response.read())


def prepare() -> pd.DataFrame:
    """Read the raw file and return it unchanged.

    `TotalCharges` is read as text, not as a float, because eleven rows carry a single space
    instead of a number (the subscribers whose `tenure` is 0 and who have therefore never been
    billed). Those spaces are **left alone**: converting them here would be the engine's job, and
    the run report records what the engine actually did with the column.
    """
    frame = pd.read_csv(RAW)
    assert frame[PRIMARY_KEY].is_unique, "the source file is one row per subscriber"
    return frame


def write_sample(frame: pd.DataFrame) -> pd.DataFrame:
    """A seeded random sample small enough to commit, still carrying both outcomes."""
    sample = frame.sample(n=min(SAMPLE_ROWS, len(frame)), random_state=SAMPLE_SEED)
    sample = sample.sort_index()
    sample.to_csv(SAMPLE, index=False, lineterminator="\n")
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-download", action="store_true", help="use the file already in data/")
    args = parser.parse_args()

    if not args.no_download:
        download()
    frame = prepare()
    frame.to_csv(PREPARED, index=False, lineterminator="\n")
    sample = write_sample(frame)

    print(f"rows          {len(frame):,}")
    print(f"columns       {len(frame.columns)}")
    print(f"target        {TARGET}: {frame[TARGET].value_counts().to_dict()}")
    print(f"prepared      {PREPARED}")
    print(
        f"sample        {SAMPLE} ({len(sample):,} rows, "
        f"{int((sample[TARGET] == 'Yes').sum()):,} positive)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
