"""Fetch UCI Bank Marketing (`bank-additional-full`) and put it in the data contract's shape.

Three things stand between the published file and an upload the engine will accept, and all three
are format, not modelling:

1. **The file is semicolon-separated and every text field is quoted.** The data contract asks for
   comma-separated UTF-8 CSV, so it is re-written as one.
2. **There is no primary key.** The contract needs a column that identifies each row. The file is
   published in campaign-call order (May 2008 to November 2010), so a 1-based row number in that
   order is a stable identifier for a row; it is added as `client_id` and is never a feature.
3. **Four column names contain dots** (`emp.var.rate`, `cons.price.idx`, `cons.conf.idx`,
   `nr.employed`). Template column names have to match `^[A-Za-z_][A-Za-z0-9_]*$`, so they are
   renamed to the same names with underscores. `mapping.yaml` records every rename.

Nothing else is touched: no imputation, no binning, no encoding, no column dropped. In particular
`duration` is **kept**, even though UCI's own notes say it should be discarded for a realistic
model, because whether the engine notices it on its own is one of the things this dataset is here
to answer. The run report says what the engine said.

    python library/uci-bank-marketing/fetch.py
    python library/uci-bank-marketing/fetch.py --no-download

Licence: see LICENSE.txt next to this file.
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "bank-additional-full.csv"
PREPARED = DATA / "prepared.csv"
SAMPLE = HERE / "sample.csv"

# UCI's own host (archive.ics.uci.edu) is not reachable from every network; this GitHub copy is the
# identical 41,188-row `bank-additional-full.csv` from the UCI `bank-additional.zip` bundle.
SOURCE_URL = (
    "https://raw.githubusercontent.com/llhthinker/MachineLearningLab/master/"
    "UCI%20Bank%20Marketing%20Data%20Set/data/bank-additional/bank-additional-full.csv"
)
UPSTREAM_URL = "https://archive.ics.uci.edu/dataset/222/bank+marketing"

PRIMARY_KEY = "client_id"
TARGET = "y"
SAMPLE_ROWS = 5_000
SAMPLE_SEED = 20260922

# Dots are legal in a pandas column name but not in a template column name, so the four
# macro-economic indicators are renamed. Only the punctuation changes.
RENAMES = {
    "emp.var.rate": "emp_var_rate",
    "cons.price.idx": "cons_price_idx",
    "cons.conf.idx": "cons_conf_idx",
    "nr.employed": "nr_employed",
}


def download() -> None:
    """Put the raw semicolon-separated CSV in `data/` (~5.8 MB)."""
    DATA.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SOURCE_URL) as response, RAW.open("wb") as handle:
        handle.write(response.read())


def prepare() -> pd.DataFrame:
    """Re-read the file as comma CSV, rename the four dotted columns, add the row-number key."""
    frame = pd.read_csv(RAW, sep=";", quotechar='"')
    frame = frame.rename(columns=RENAMES)

    # The published order is the order the calls were made in, so the row number is a stable
    # identifier for a row. It is the primary key and the engine never uses a key as a feature.
    frame.insert(0, PRIMARY_KEY, range(1, len(frame) + 1))

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
    print(
        f"sample        {SAMPLE} ({len(sample):,} rows, "
        f"{int((sample[TARGET] == 'yes').sum()):,} positive)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
