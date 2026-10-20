"""Download the three public replay datasets into a local cache, verifying them where we can.

Nothing in the product or the tests calls this; it is run by hand (or by a nightly job) before a
full replay. The network may be blocked where this runs, so every failure is reported with the URL
that was tried, and nothing half-downloaded is left in the cache.

Checksums: Hillstrom's is known (the same file `library/hillstrom-email/fetch.py` validates). The
Open Bandit and Criteo checksums are placeholders (`None`) until someone downloads them once from a
network that allows it and records the SHA-256 printed by `fetch()`; until then the file is kept
but flagged as unverified.

    python -m autonomy.proof.replay.fetch hillstrom
    python -m autonomy.proof.replay.fetch criteo_uplift --cache-dir /data/replay
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["DATASETS", "DatasetSource", "FetchResult", "default_cache_dir", "fetch", "sha256_file"]

CACHE_ENV: Final = "AUTONOMY_REPLAY_CACHE"


@dataclass(frozen=True)
class DatasetSource:
    """Where one public file lives and what the cached copy must look like."""

    name: str
    urls: tuple[str, ...]
    """Tried in order; the first that answers wins."""
    filename: str
    """Name in the cache after any decompression."""
    sha256: str | None
    """Of the file as cached (decompressed). None: not yet recorded, so not verified."""
    unpack: str = "none"
    """'none', 'gunzip' (one gzip member to `filename`) or 'unzip' (an archive into a folder)."""
    licence: str = ""


DATASETS: Final[dict[str, DatasetSource]] = {
    "open_bandit": DatasetSource(
        name="open_bandit",
        urls=("https://research.zozo.com/data_release/open_bandit_dataset.zip",),
        filename="open_bandit_dataset",
        sha256=None,  # placeholder: record on first verified download (of the zip)
        unpack="unzip",
        licence="CC BY 4.0 (ZOZO Research)",
    ),
    "open_bandit_sample_random": DatasetSource(
        name="open_bandit_sample_random",
        urls=("https://raw.githubusercontent.com/st-tech/zr-obp/master/obd/random/all/all.csv",),
        filename="open_bandit_sample_random_all.csv",
        sha256=None,  # placeholder
        licence="CC BY 4.0 (ZOZO Research); the 10,000-row sample shipped with zr-obp",
    ),
    "open_bandit_sample_bts": DatasetSource(
        name="open_bandit_sample_bts",
        urls=("https://raw.githubusercontent.com/st-tech/zr-obp/master/obd/bts/all/all.csv",),
        filename="open_bandit_sample_bts_all.csv",
        sha256=None,  # placeholder
        licence="CC BY 4.0 (ZOZO Research); the 10,000-row sample shipped with zr-obp",
    ),
    "hillstrom": DatasetSource(
        name="hillstrom",
        urls=(
            "https://hillstorm1.s3.us-east-2.amazonaws.com/hillstorm_no_indices.csv.gz",
            "http://www.minethatdata.com/Kevin_Hillstrom_MineThatData_E-MailAnalytics_DataMiningChallenge_2008.03.20.csv",
        ),
        filename="hillstrom.csv",
        sha256="00a6a868e05a9ffe7382da51629f6d6dce88c5acfc945e79d314ebc78fd3a2c0",
        unpack="gunzip",
        licence="See library/hillstrom-email/LICENSE.txt",
    ),
    "criteo_uplift": DatasetSource(
        name="criteo_uplift",
        urls=("http://go.criteo.net/criteo-research-uplift-v2.1.csv.gz",),
        filename="criteo-research-uplift-v2.1.csv",
        sha256=None,  # placeholder: record on first verified download
        unpack="gunzip",
        licence="CC BY-NC-SA 4.0 (Criteo AI Lab)",
    ),
}


@dataclass(frozen=True)
class FetchResult:
    path: Path
    url: str | None
    """None when the cached copy was reused."""
    sha256: str | None
    verified: bool


Downloader = Callable[[str, Path], None]


def default_cache_dir() -> Path:
    """`$AUTONOMY_REPLAY_CACHE`, else `~/.cache/autonomy/replay`."""
    configured = os.environ.get(CACHE_ENV)
    return Path(configured) if configured else Path.home() / ".cache" / "autonomy" / "replay"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _urllib_download(url: str, target: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)


def _is_gzip(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def _unpack(source: DatasetSource, raw: Path, final: Path) -> None:
    if source.unpack == "unzip":
        with zipfile.ZipFile(raw) as archive:
            archive.extractall(final)
    elif source.unpack == "gunzip" and _is_gzip(raw):
        with gzip.open(raw, "rb") as src, final.open("wb") as out:
            shutil.copyfileobj(src, out)
    else:  # plain file (or a mirror that serves the CSV uncompressed)
        shutil.copyfile(raw, final)


def fetch(
    name: str,
    *,
    cache_dir: Path | None = None,
    force: bool = False,
    download: Downloader = _urllib_download,
) -> FetchResult:
    """Download dataset `name` into the cache (or reuse it) and verify its checksum when known.

    Raises KeyError for an unknown name, RuntimeError when every URL fails, and ValueError when the
    checksum does not match (the bad file is deleted, never used). For the 'unzip' kind the
    checksum is of the archive, which is kept beside the folder.
    """
    source = DATASETS[name]
    cache = cache_dir or default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    final = cache / source.filename
    raw = cache / f"{source.filename}.download"
    checked = raw if source.unpack == "unzip" else final
    if final.exists() and not force:
        digest = sha256_file(checked) if checked.is_file() else None
        return FetchResult(final, None, digest, source.sha256 is not None and digest == source.sha256)

    errors: list[str] = []
    for url in source.urls:
        try:
            download(url, raw)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            errors.append(f"{url}: {exc}")
            raw.unlink(missing_ok=True)
            continue
        if final.is_dir():
            shutil.rmtree(final)
        _unpack(source, raw, final)
        digest = sha256_file(checked)
        if source.sha256 is not None and digest != source.sha256:
            for leftover in (raw, final):
                if leftover.is_dir():
                    shutil.rmtree(leftover)
                else:
                    leftover.unlink(missing_ok=True)
            raise ValueError(f"{name}: checksum {digest} from {url} does not match {source.sha256}.")
        if source.unpack != "unzip":
            raw.unlink(missing_ok=True)
        return FetchResult(final, url, digest, source.sha256 is not None)
    raise RuntimeError(f"{name}: every source failed:\n" + "\n".join(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download a public replay dataset into the cache.")
    parser.add_argument("name", choices=sorted(DATASETS))
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    result = fetch(args.name, cache_dir=args.cache_dir, force=args.force)
    status = "verified" if result.verified else "UNVERIFIED (no checksum recorded yet)"
    print(f"{result.path}  sha256={result.sha256}  {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
