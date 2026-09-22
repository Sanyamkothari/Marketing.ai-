"""Fetch UCI "Default of Credit Card Clients" and put it in the data contract's shape.

The file is already one row per card account with a unique `ID`, so only one thing has to change:
the target column is published as `default.payment.next.month`, and a template column name has to
match `^[A-Za-z_][A-Za-z0-9_]*$`. It is renamed to `default_payment_next_month`; `mapping.yaml`
records the rename. Nothing else is touched - the coded categories keep their published codes,
including the ones the UCI documentation does not explain (see README.md).

    python library/uci-credit-default/fetch.py
    python library/uci-credit-default/fetch.py --no-download

Licence: see LICENSE.txt next to this file.
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "UCI_Credit_Card.csv"
PREPARED = DATA / "prepared.csv"
SAMPLE = HERE / "sample.csv"

# UCI's own host is not reachable from every network; this GitHub copy is the identical 30,000-row
# CSV that Kaggle publishes as `UCI_Credit_Card.csv` (the UCI .xls converted to CSV).
SOURCE_URL = (
    "https://raw.githubusercontent.com/YuChenAmberLu/Data-Science--Credit-Card-Default/master/"
    "UCI_Credit_Card.csv"
)
UPSTREAM_URL = "https://archive.ics.uci.edu/dataset/350/default+of+credit+card+clients"

PRIMARY_KEY = "ID"
SOURCE_TARGET = "default.payment.next.month"
TARGET = "default_payment_next_month"
SAMPLE_ROWS = 5_000
SAMPLE_SEED = 20260922


def download() -> None:
    """Put the raw CSV in `data/` (~2.9 MB)."""
    DATA.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SOURCE_URL) as response, RAW.open("wb") as handle:
        handle.write(response.read())


def prepare() -> pd.DataFrame:
    """Read the raw file and rename the one column whose published name has dots in it."""
    frame = pd.read_csv(RAW)
    frame = frame.rename(columns={SOURCE_TARGET: TARGET})

    # `ID` is already 1..30000 and unique; it is the primary key and never a feature.
    assert frame[PRIMARY_KEY].is_unique
    assert not any("." in name for name in frame.columns), "no dotted column names survive"
    return frame


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
    frame = prepare()
    frame.to_csv(PREPARED, index=False, lineterminator="\n")
    sample = write_sample(frame)

    print(f"upstream      {UPSTREAM_URL}")
    print(f"rows          {len(frame):,}")
    print(f"columns       {len(frame.columns)}")
    print(f"target        {TARGET}: {frame[TARGET].value_counts().to_dict()}")
    print(f"prepared      {PREPARED}")
    print(f"sample        {SAMPLE} ({len(sample):,} rows, " f"{int(sample[TARGET].sum()):,} positive)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
