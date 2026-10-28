"""The results report bands its examples as the export does: a regression run by the share of rows scoring below."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from engine.config import load_use_case
from engine.pilot.results import _example_rows
from engine.stages.actions import assign_bands
from engine.storage import LocalStorage

CONFIGS = Path(__file__).resolve().parents[3] / "configs"


def _write(storage: LocalStorage, key: str, scores: np.ndarray) -> None:
    rows = [
        {"primary_key": f"C{i}", "score": float(s), "reasons": [], "method": "permutation"}
        for i, s in enumerate(scores)
    ]
    path = storage.local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_a_regression_runs_examples_carry_the_exports_bands(tmp_path: Path) -> None:
    config = load_use_case("customer-spend", CONFIGS)
    storage = LocalStorage(tmp_path)
    rng = np.random.default_rng(1)
    amounts = np.round(rng.gamma(2.0, 300.0, 400), 2)  # predicted spend, far above every probability edge
    _write(storage, "runs/r1/row_explanations.parquet", amounts)
    examples, bands = _example_rows(storage, "runs/r1/row_explanations.parquet", config, uplift=False)
    exported = assign_bands(pd.Series(amounts), config)
    by_key = dict(zip((f"C{i}" for i in range(len(amounts))), exported, strict=True))
    assert examples and [by_key[e.primary_key] for e in examples] == list(bands)
    # on raw amounts every customer would sit in the top band; the export spreads them over the bands
    assert len(Counter(exported)) > 1 and len(set(bands)) > 1
    for name in set(bands):
        top = max(e.score for e, b in zip(examples, bands, strict=True) if b == name)
        assert top == max(a for a, b in zip(amounts, exported, strict=True) if b == name)
