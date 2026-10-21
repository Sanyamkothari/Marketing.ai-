"""Download the scoreboard's four datasets, checked against fixed SHA-256 checksums.

    python -m scripts.v2_realbench.fetch --data-dir /tmp/datasets                 # all four
    python -m scripts.v2_realbench.fetch --data-dir /tmp/datasets --dataset hillstrom

Every URL is a public host a GitHub Actions runner can reach (two public S3 buckets and a GitHub
release); the sizes and checksums are those recorded when the files were first downloaded
(`/home/user/datasets/*/SOURCE.txt`, docs/v2/real_data/CATALOGUE.md). A file already present with
the right checksum is not downloaded again; a wrong checksum is an error and the file is removed.
Nothing is ever written inside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    dataset: str
    folder: str
    file: str
    url: str
    bytes: int
    sha256: str


SOURCES: tuple[Source, ...] = (
    Source(
        "hillstrom",
        "hillstrom",
        "hillstorm_no_indices.csv.gz",
        "https://hillstorm1.s3.us-east-2.amazonaws.com/hillstorm_no_indices.csv.gz",
        443_070,
        "bab6578f60db5d792f1c2372c502f029152a5249cf5ea84390f3b7f885d7234f",
    ),
    Source(
        "x5",
        "x5_retailhero",
        "uplift_train.csv.gz",
        "https://sklift.s3.eu-west-2.amazonaws.com/uplift_train.csv.gz",
        1_182_423,
        "0a218e864a1ec1dfe26e980a082b81d5f46d199c4a6ae32abbaa95265af1c2c7",
    ),
    Source(
        "x5",
        "x5_retailhero",
        "clients.csv.gz",
        "https://sklift.s3.eu-west-2.amazonaws.com/clients.csv.gz",
        7_637_755,
        "bfca1719b862735ae9816ed9bbba6924931c6a4dff761c481efe9addff458429",
    ),
    Source(
        "x5",
        "x5_retailhero",
        "purchases.csv.gz",
        "https://sklift.s3.eu-west-2.amazonaws.com/purchases.csv.gz",
        669_979_268,
        "3d57519fa92b553fd1e947c120add475cc42e0c64c323a52cef5aebe8645c8e2",
    ),
    Source(
        "criteo",
        "criteo_uplift",
        "criteo-research-uplift-v2.1.csv.gz",
        "https://github.com/jszymon/uplift_sklearn_data/releases/download/Criteo/criteo-research-uplift-v2.1.csv.gz",
        311_422_618,
        "2716e1bf0fd157a93b5bf86924d9088419dfbac2022c6cd90030220634f616dc",
    ),
    Source(
        "megafon",
        "megafon",
        "megafon_dataset.csv.gz",
        "https://sklift.s3.eu-west-2.amazonaws.com/megafon_dataset.csv.gz",
        275_944_146,
        "cdcb2d052b90f8eefa75937d5540f114d0748ea231d95e2778dd6760478e4a00",
    ),
)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(source: Source, data_dir: Path, *, timeout: float = 120.0) -> Path:
    """Download one file (unless present and correct) and verify its checksum."""
    target = data_dir / source.folder / source.file
    if target.exists() and sha256_of(target) == source.sha256:
        print(f"ok (already there): {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print(f"downloading {source.url} ({source.bytes:,} bytes)", flush=True)
    with urllib.request.urlopen(source.url, timeout=timeout) as resp, partial.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1 << 20)
    got = sha256_of(partial)
    if got != source.sha256:
        partial.unlink(missing_ok=True)
        raise ValueError(f"{source.file}: checksum {got} does not match {source.sha256}")
    partial.replace(target)
    print(f"ok: {target}")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download the real-data scoreboard's datasets.")
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--dataset", choices=["hillstrom", "x5", "criteo", "megafon", "all"], default="all")
    args = ap.parse_args(argv)
    for s in SOURCES:
        if args.dataset in ("all", s.dataset):
            fetch(s, args.data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
