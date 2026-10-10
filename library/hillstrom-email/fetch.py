"""Fetch the MineThatData E-Mail Analytics (Hillstrom) file, verify it, and put it in the contract's shape.

Kevin Hillstrom published this file for his 2008 "E-Mail Analytics and Data Mining Challenge": 64,000
customers who last bought within twelve months, randomly split into three equal groups for two weeks -
one sent an e-mail featuring men's merchandise, one an e-mail featuring women's merchandise, and one
sent nothing - with the visits, conversions and spend of the following two weeks. It is the library's
first dataset in which the treatment was assigned at random, so it can say what a campaign *changed*,
not only who responded.

Two things stand between the published file and an upload the engine accepts, and both are format:

1. **There is no primary key.** A 1-based row number in the published order is added as `customer_id`;
   it identifies a row and is never a feature.
2. **Nothing else.** Every published column is kept with its published name and values, including the
   `Surburban` spelling in `zip_code`. `segment` is the randomised treatment (`Mens E-Mail`, `Womens
   E-Mail`, `No E-Mail`); `visit`, `conversion` and `spend` are outcomes measured after the e-mail.

**Where the file comes from.** The canonical host, www.minethatdata.com, is tried first. It did not
resolve from the environment this library was built in (2026-10-10), so the script falls back to the
mirror the scikit-uplift project's `fetch_hillstrom` reads, a gzip of the same CSV. Whichever answers,
the decompressed CSV must have the SHA-256 below, or nothing is written: a file that differs from the
one this library was validated on is refused, never used. The product owner approved obtaining the file
on 2026-10-09; read `LICENSE.txt` before using it outside internal validation.

    python library/hillstrom-email/fetch.py                 # download, verify, prepare
    python library/hillstrom-email/fetch.py --no-download   # verify and prepare data/hillstrom.csv
    python library/hillstrom-email/fetch.py --from PATH     # verify and prepare a copy you already have

Writes `data/hillstrom.csv` (the verified raw file) and `data/prepared.csv` (with `customer_id`), both
git-ignored. `sample.csv`, the committed sample the tests read, is rewritten only with `--write-sample`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

import pandas as pd

HERE: Final = Path(__file__).resolve().parent
DATA: Final = HERE / "data"
RAW: Final = DATA / "hillstrom.csv"
PREPARED: Final = DATA / "prepared.csv"
SAMPLE: Final = HERE / "sample.csv"

CANONICAL_URL: Final = (
    "http://www.minethatdata.com/Kevin_Hillstrom_MineThatData_E-MailAnalytics_DataMiningChallenge_2008.03.20.csv"
)
MIRROR_URL: Final = "https://hillstorm1.s3.us-east-2.amazonaws.com/hillstorm_no_indices.csv.gz"
"""The scikit-uplift project's mirror (`sklift.datasets.fetch_hillstrom`): a gzip of the same CSV."""
SHA256: Final = "00a6a868e05a9ffe7382da51629f6d6dce88c5acfc945e79d314ebc78fd3a2c0"
"""Of the decompressed CSV this library was validated on: 64,000 rows plus the header."""

ROWS: Final = 64_000
COLUMNS: Final = (
    "recency",
    "history_segment",
    "history",
    "mens",
    "womens",
    "zip_code",
    "newbie",
    "channel",
    "segment",
    "visit",
    "conversion",
    "spend",
)
LEVELS: Final = ("No E-Mail", "Mens E-Mail", "Womens E-Mail")
"""The treatment column's values, the control first (the order `uplift.treatment_levels` uses)."""
PRIMARY_KEY: Final = "customer_id"
TREATMENT: Final = "segment"
TARGET: Final = "conversion"

SAMPLE_ROWS: Final = 6_400
"""A tenth of the file, drawn within each of the three groups, for the tests; never the whole file."""
SAMPLE_SEED: Final = 20261010
TIMEOUT_S: Final = 60


class ChecksumError(RuntimeError):
    """The file read is not the file this library was validated on."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path: Path) -> None:
    """Raise `ChecksumError` unless `path` is byte for byte the validated CSV."""
    found = sha256_of(path)
    if found != SHA256:
        raise ChecksumError(f"{path} has SHA-256 {found}, not the validated {SHA256}; it is not used.")


def _download(url: str, target: Path) -> None:
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def download() -> str:
    """Put the verified CSV at `data/hillstrom.csv`; return the URL it came from.

    The canonical URL first, then the mirror. A source that cannot be reached, or whose file fails the
    checksum, is reported and the next one is tried; when none gives the validated file, nothing is kept.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    partial = DATA / "download.part"
    failures: list[str] = []
    for url, gzipped in ((CANONICAL_URL, False), (MIRROR_URL, True)):
        try:
            _download(url, partial)
            if gzipped:
                unpacked = DATA / "download.csv"
                with gzip.open(partial, "rb") as source, unpacked.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                partial.unlink()
                unpacked.replace(partial)
            verify(partial)
        except (urllib.error.URLError, OSError, ChecksumError) as exc:
            failures.append(f"{url}: {exc}")
            partial.unlink(missing_ok=True)
            print(f"not used  {url}: {exc}", file=sys.stderr)
            continue
        partial.replace(RAW)
        return url
    raise RuntimeError("No source gave the validated file:\n" + "\n".join(failures))


def prepare(raw: Path = RAW) -> pd.DataFrame:
    """The verified file with `customer_id` (the 1-based row number) in front; nothing else changes."""
    verify(raw)
    frame = pd.read_csv(raw)
    if tuple(frame.columns) != COLUMNS:
        raise ValueError(f"unexpected columns {list(frame.columns)}")
    if len(frame) != ROWS:
        raise ValueError(f"expected {ROWS} rows, found {len(frame)}")
    if set(frame[TREATMENT].unique()) != set(LEVELS):
        raise ValueError(f"unexpected treatment values {sorted(frame[TREATMENT].unique())}")
    frame.insert(0, PRIMARY_KEY, range(1, len(frame) + 1))
    return frame


def write_sample(frame: pd.DataFrame, path: Path = SAMPLE) -> pd.DataFrame:
    """A tenth of the rows, the same share of each group, in the published order."""
    share = SAMPLE_ROWS / len(frame)
    parts = [
        group.sample(frac=share, random_state=SAMPLE_SEED)
        for _level, group in frame.groupby(TREATMENT, sort=True)
    ]
    sample = pd.concat(parts).sort_index()
    sample.to_csv(path, index=False, lineterminator="\n")
    return sample


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--no-download", action="store_true", help="use the file already in data/")
    parser.add_argument(
        "--from", dest="source", type=Path, default=None, help="a local copy to verify and use"
    )
    parser.add_argument("--write-sample", action="store_true", help="also rewrite the committed sample.csv")
    args = parser.parse_args(argv)

    if args.source is not None:
        verify(args.source)
        DATA.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.source, RAW)
        origin = str(args.source)
    elif args.no_download:
        origin = str(RAW)
    else:
        origin = download()
    frame = prepare()
    frame.to_csv(PREPARED, index=False, lineterminator="\n")
    print(f"source        {origin}")
    print(f"sha256        {SHA256} (verified)")
    print(f"rows          {len(frame):,}")
    print(f"groups        {frame[TREATMENT].value_counts().sort_index().to_dict()}")
    print(f"conversions   {int(frame[TARGET].sum()):,}")
    print(f"prepared      {PREPARED}")
    if args.write_sample:
        sample = write_sample(frame)
        print(f"sample        {SAMPLE} ({len(sample):,} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
