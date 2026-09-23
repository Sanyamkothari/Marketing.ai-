"""`engine.stages.prepare` on pandas' nullable integers (`Int64`), the dtype a built dataset's count
features read back as (Plan A M35, DEC-091).

A dataset built from raw tables stores a count as a nullable integer, because a customer with no rows
in a window has no count at all, and Parquet hands that back as `Int64`. The default `outliers: clip`
then wrote a float bound into it, `Int64` refused (`TypeError: Invalid value '455.96' for dtype
'Int64'`), and the first training run on the first built dataset failed at the split stage. The fill
median, a float too, had the same problem. These tests hold both, on both the fit path and replay,
and pin that a plain NumPy integer column from a CSV upload is handled exactly as before.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.config import UseCaseConfig, load_use_case_document
from engine.stages.prepare import prepare, replay

RUN_ID = "r_20260923_abcdef01"
ROWS = 100


def config_for(**patch: dict[str, Any]) -> UseCaseConfig:
    """`tests/unit/test_prepare.py`'s helper: one block differs from the defaults, nothing else."""
    document = load_use_case_document("targeted-advertisement")
    document["template"] = {"columns": []}
    document["target"] = {"column": "converted", "positive_label": 1}
    for block, value in patch.items():
        current = document.get(block)
        document[block] = {**current, **value} if isinstance(current, dict) else value
    return UseCaseConfig.model_validate(document)


def frame_with(counts: pd.Series[Any]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C{index:04d}" for index in range(ROWS)],
            "complaints_90d": counts,
            "converted": [index % 2 for index in range(ROWS)],
        }
    )


def nullable_counts() -> pd.Series[Any]:
    """Counts 0..98 plus one outlier, one of them missing - quantiles and median land between integers."""
    values: list[int | None] = [*range(ROWS - 1), 10_000]
    values[3] = None
    return pd.Series(values, dtype="Int64")


def test_a_nullable_integer_column_is_clipped_and_filled_instead_of_failing() -> None:
    frame = frame_with(nullable_counts())
    config = config_for(prepare={"outliers": "clip", "missing_values": "fill", "deduplicate": False})

    prepared, report = prepare(frame, config, run_id=RUN_ID, primary_key="customer_id", target="converted")

    clip = next(t for t in report.transforms if t.kind == "clip_percentile")
    fill = next(t for t in report.transforms if t.kind == "fill_median")
    assert clip.parameters["applied"] is True
    assert prepared["complaints_90d"].max() == pytest.approx(clip.parameters["upper"])
    assert prepared["complaints_90d"].isna().sum() == 0
    assert prepared["complaints_90d"].iloc[3] == pytest.approx(fill.parameters["value"])


def test_replay_applies_the_recorded_bounds_and_fill_to_a_nullable_integer_column() -> None:
    config = config_for(prepare={"outliers": "clip", "missing_values": "fill", "deduplicate": False})
    _, report = prepare(
        frame_with(nullable_counts()), config, run_id=RUN_ID, primary_key="customer_id", target="converted"
    )
    scoring = frame_with(nullable_counts()).drop(columns=["converted"])

    replayed = replay(scoring, report)

    clip = next(t for t in report.transforms if t.kind == "clip_percentile")
    fill = next(t for t in report.transforms if t.kind == "fill_median")
    assert replayed["complaints_90d"].max() == pytest.approx(clip.parameters["upper"])
    assert replayed["complaints_90d"].iloc[3] == pytest.approx(fill.parameters["value"])


def test_a_numpy_integer_column_keeps_the_behaviour_it_always_had() -> None:
    """The widening is for extension dtypes only: a CSV's `int64` column goes through exactly the
    same `clip` it always did, so a Phase 1 run's recorded transforms are unchanged."""
    counts = pd.Series([*range(ROWS - 1), 10_000], dtype="int64")
    config = config_for(prepare={"outliers": "clip", "missing_values": "auto", "deduplicate": False})

    prepared, report = prepare(
        frame_with(counts), config, run_id=RUN_ID, primary_key="customer_id", target="converted"
    )

    clip = next(t for t in report.transforms if t.kind == "clip_percentile")
    expected = counts.clip(lower=clip.parameters["lower"], upper=clip.parameters["upper"])
    pd.testing.assert_series_equal(prepared["complaints_90d"], expected, check_names=False)
