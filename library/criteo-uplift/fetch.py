"""Fetch the Criteo Uplift Prediction Dataset v2.1 and write a mapped sample of at most 1M rows.

**This script has never been run to completion.** `go.criteo.net` and `huggingface.co` are both
blocked by the egress policy of the environment this library was built in, so no byte of this
dataset was ever fetched here. It was started again on 2026-09-23, once Phase 3b made the use case
runnable: the direct link was refused with HTTP 403 and the `--mirror` link with a 403 on the proxy
tunnel, before any row was read. Everything below is written against the published file's documented schema; the
`verify()` step exists so that the first person who runs it on a network that allows the download
finds out immediately if the schema has moved, rather than discovering it three stages later.

The dataset is CC BY-NC-SA 4.0 - **non-commercial**. See LICENSE.txt before running this.

WHAT IT DOES
------------
1. Streams the 311 MB gzip in chunks; 13,979,592 rows do not need to be held in memory.
2. Takes a seeded sample of at most `SAMPLE_ROWS` rows (1,000,000, the brief's ceiling) by keeping
   the same proportion of every chunk, so the treated/control ratio is preserved and no chunk is
   ever held in full.
3. Adds `impression_id`, the 1-based row number in the published order: the file ships no key and
   the data contract needs one.
4. Writes `data/prepared.csv` and a 5,000-row `sample.csv`.

It does **no** feature engineering: `f0`..`f11` are anonymised dense floats and there is nothing
meaningful to do to them without knowing what they are. It does not touch `exposure`, which is an
outcome and not a treatment - see README.md.

    python library/criteo-uplift/fetch.py
    python library/criteo-uplift/fetch.py --no-download --sample-rows 200000
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "criteo-research-uplift-v2.1.csv.gz"
PREPARED = DATA / "prepared.csv"
SAMPLE = HERE / "sample.csv"

# Criteo's own direct link, from https://ailab.criteo.com/criteo-uplift-prediction-dataset/ .
# MIRROR_URL is Criteo's Hugging Face copy of the same file, for networks where one is reachable
# and the other is not. Both were blocked where this library was built.
SOURCE_URL = "http://go.criteo.net/criteo-research-uplift-v2.1.csv.gz"
MIRROR_URL = (
    "https://huggingface.co/datasets/criteo/criteo-uplift/resolve/main/" "criteo-research-uplift-v2.1.csv.gz"
)

PRIMARY_KEY = "impression_id"
TARGET = "conversion"
TREATMENT = "treatment"

# The published schema, in published order. `verify()` checks the download against it.
FEATURES = tuple(f"f{i}" for i in range(12))
EXPECTED_COLUMNS = (*FEATURES, "treatment", "conversion", "visit", "exposure")
EXPECTED_ROWS = 13_979_592

SAMPLE_ROWS = 1_000_000  # the brief's ceiling for this dataset
SAMPLE_SEED = 20260922
CHUNK_ROWS = 500_000
COMMITTED_SAMPLE_ROWS = 5_000


def download(url: str = SOURCE_URL) -> None:
    """Put the 311 MB gzip in `data/`. Try MIRROR_URL if this one is refused."""
    DATA.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, RAW.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)


def verify(frame: pd.DataFrame) -> None:
    """Fail loudly if the published schema is not what this script was written against."""
    missing = [name for name in EXPECTED_COLUMNS if name not in frame.columns]
    if missing:
        raise SystemExit(
            f"The download does not carry {missing}. The published schema has changed; "
            f"re-read https://ailab.criteo.com/criteo-uplift-prediction-dataset/ before "
            f"trusting mapping.yaml or use_case.yaml."
        )


def prepare(sample_rows: int) -> pd.DataFrame:
    """Stream the gzip, keep a seeded proportional sample, and add the row-number key.

    Sampling is per chunk and proportional, not global-uniform: every chunk contributes the same
    share of its rows, so the treated/control ratio and the conversion rate of the sample match the
    file's, and the whole 13.98 M rows are never resident at once. The row number is assigned over
    the **whole** file before any row is dropped, so a sampled row keeps the identity it has in the
    published order.
    """
    kept: list[pd.DataFrame] = []
    offset = 0
    total = 0
    # The share of each chunk to keep, so the sample is proportional and the treated/control ratio
    # survives. EXPECTED_ROWS is the published row count; verify() has already refused a file whose
    # schema moved, and a file whose LENGTH moved only makes this share slightly off, which the
    # final trim below corrects.
    share = min(1.0, sample_rows / EXPECTED_ROWS)

    reader = pd.read_csv(RAW, compression="gzip", chunksize=CHUNK_ROWS)
    for index, chunk in enumerate(reader):
        if index == 0:
            verify(chunk)
        chunk = chunk.copy()
        # The key is the row number in the PUBLISHED order, so it is assigned before any row is
        # dropped: a sampled row keeps the identity it has in the full file.
        chunk.insert(0, PRIMARY_KEY, range(offset + 1, offset + len(chunk) + 1))
        offset += len(chunk)
        total += len(chunk)
        if share < 1.0:
            # Sample HERE, not after concatenating: 13,979,592 rows never have to be held at once.
            # The seed varies with the chunk so two chunks do not draw the same row positions.
            keep = min(len(chunk), max(1, round(len(chunk) * share)))
            chunk = chunk.sample(n=keep, random_state=SAMPLE_SEED + index)
        kept.append(chunk)

    frame = pd.concat(kept, ignore_index=True)
    # Rounding per chunk can overshoot by a few rows; trim uniformly from what is now a small frame.
    if len(frame) > sample_rows:
        frame = frame.sample(n=sample_rows, random_state=SAMPLE_SEED)
    frame = frame.sort_values(PRIMARY_KEY).reset_index(drop=True)

    # The published column order, with the key first.
    frame = frame[[PRIMARY_KEY, *EXPECTED_COLUMNS]]
    assert frame[PRIMARY_KEY].is_unique
    print(f"read {total:,} rows from the download; kept {len(frame):,}")
    return frame


def write_sample(frame: pd.DataFrame) -> pd.DataFrame:
    sample = frame.sample(n=min(COMMITTED_SAMPLE_ROWS, len(frame)), random_state=SAMPLE_SEED).sort_values(
        PRIMARY_KEY
    )
    sample.to_csv(SAMPLE, index=False, lineterminator="\n")
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-download", action="store_true", help="use the file already in data/")
    parser.add_argument("--mirror", action="store_true", help="download from MIRROR_URL instead")
    parser.add_argument("--sample-rows", type=int, default=SAMPLE_ROWS)
    args = parser.parse_args()

    if not args.no_download:
        download(MIRROR_URL if args.mirror else SOURCE_URL)
    frame = prepare(args.sample_rows)
    frame.to_csv(PREPARED, index=False, lineterminator="\n")
    sample = write_sample(frame)

    print("upstream      https://ailab.criteo.com/criteo-uplift-prediction-dataset/")
    print("licence       CC BY-NC-SA 4.0 - non-commercial; see LICENSE.txt")
    print(f"rows          {len(frame):,}")
    print(f"columns       {len(frame.columns)}")
    print(f"treatment     {frame[TREATMENT].mean():.3f} treated")
    print(
        f"target        {TARGET}: {frame[TARGET].value_counts().to_dict()} "
        f"({frame[TARGET].mean():.4%} positive)"
    )
    print(f"visit rate    {frame['visit'].mean():.4%}")
    print(f"prepared      {PREPARED}")
    print(f"sample        {SAMPLE} ({len(sample):,} rows)")
    print()
    print("NOTE: do not train a propensity model on `conversion` and call it uplift. See README.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
