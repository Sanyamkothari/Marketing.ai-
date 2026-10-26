"""Seeded samples of the real UCI Online Retail log (v3 phase 0, B7): see `make_fixture.py` and `ATTRIBUTION.txt`."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pandas as pd

__all__ = ["SAMPLE_FILE", "SMALL_FILE", "small_log", "uci_sample"]

HERE: Final[Path] = Path(__file__).resolve().parent
SAMPLE_FILE: Final[Path] = HERE / "online_retail_sample.csv.gz"
SMALL_FILE: Final[Path] = HERE / "online_retail_small.csv.gz"


def uci_sample() -> pd.DataFrame:
    """About 1,000 customers of UCI Online Retail, every line of each, and guest checkouts in proportion; read as a
    CSV upload is read (`CustomerID` is a float because some lines have none)."""
    return pd.read_csv(SAMPLE_FILE)


def small_log() -> pd.DataFrame:
    """A few dozen customers of the same file: too small to build a model from."""
    return pd.read_csv(SMALL_FILE)
