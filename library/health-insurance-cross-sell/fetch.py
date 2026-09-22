"""Fetch the Health Insurance Cross-Sell training file and write the sample the tests use.

Like the Telco file, this one arrives in the data contract's shape already: 381,109 rows, one per
policyholder, a unique `id`, and a `Response` column holding the outcome. Nothing is derived,
renamed or dropped here.

Two columns look numeric and are not: `Region_Code` and `Policy_Sales_Channel` are float-typed
category codes. They are **left as published** - re-typing them would be a modelling decision, and
what the engine makes of them is recorded in the run report.

    python library/health-insurance-cross-sell/fetch.py
    python library/health-insurance-cross-sell/fetch.py --no-download

Licence: see LICENSE.txt next to this file.
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "train.csv"
PREPARED = DATA / "prepared.csv"
SAMPLE = HERE / "sample.csv"

# Kaggle needs an account and an API token; this GitHub copy is the identical `train.csv`
# (21,432,357 bytes, 381,109 rows) from the Kaggle "Health Insurance Cross Sell Prediction" set.
SOURCE_URL = "https://raw.githubusercontent.com/asyntychaki/Health-Insurance-Cross-Sell/main/data/train.csv"
UPSTREAM_URL = "https://www.kaggle.com/datasets/anmolkumar/health-insurance-cross-sell-prediction"

PRIMARY_KEY = "id"
TARGET = "Response"
SAMPLE_ROWS = 5_000
SAMPLE_SEED = 20260922


def download() -> None:
    """Put the raw CSV in `data/` (~21 MB)."""
    DATA.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SOURCE_URL) as response, RAW.open("wb") as handle:
        handle.write(response.read())


def prepare() -> pd.DataFrame:
    """Read the raw file and return it unchanged.

    The dataset carries no names, addresses, emails or phone numbers - `Gender` and `Age` are the
    only person-level attributes and both are features, not contact details - so there is nothing
    to drop for privacy here.
    """
    frame = pd.read_csv(RAW)
    assert frame[PRIMARY_KEY].is_unique, "the source file is one row per policyholder"
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
