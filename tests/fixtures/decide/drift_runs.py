"""A scoring run whose customers have moved, for the drift-event tests (Plan J M109).

Nothing here writes a drift verdict by hand. The training run and the scoring run are written by
`tests.fixtures.make_run.write_run`; the baseline is built from the training rows by the engine's own
`register.drift_baseline`, the scored rows are moved (a feature multiplied, so it really leaves the
baseline's bins) and `score.compute_drift` measures the PSI. Only the result is stored in the scoring
run as `drift.json`, which is the one thing the predict stage would have done with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import pandas as pd

from engine.config import RunMode, load_use_case
from engine.contracts import DriftReport, RunRecord
from engine.stages import register, score
from engine.storage import Storage, run_key
from tests.fixtures.make_run import RunSpec, feature_columns, primary_key_of, source_key, write_run

__all__ = ["TRAINED_ON", "USE_CASE", "DriftedRun", "write_drifted_run"]

USE_CASE: Final[str] = "targeted-advertisement"
TRAINED_ON: Final[datetime] = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
"""The day the training run is dated; events before it are already in what the model learned."""
SCORED_ON: Final[datetime] = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)

#: Columns moved when a run is made to drift, so a test can name a measure that really moved.
MOVED: Final[tuple[str, ...]] = ("visits_last_7d", "tenure_months")


@dataclass(frozen=True)
class DriftedRun:
    run_id: str
    training_run_id: str
    report: DriftReport


def write_drifted_run(storage: Storage, *, move: bool = True, rows: int = 400) -> DriftedRun:
    """A scoring run with a `drift.json` measured by the engine; `move=False` leaves the customers as they were."""
    config = load_use_case(USE_CASE)
    training = write_run(
        storage, RunSpec(USE_CASE, mode=RunMode.TRAIN, rows=rows, seed=11, created_at=TRAINED_ON)
    )
    scoring = write_run(
        storage, RunSpec(USE_CASE, mode=RunMode.SCORE, rows=rows, seed=12, created_at=SCORED_ON)
    )
    features = [name for name in feature_columns(config) if name]
    trained_frame = _rows(storage, training)
    scored_frame = _rows(storage, scoring)
    if move:
        for name in MOVED:
            scored_frame[name] = scored_frame[name] * 6 + 40
    baseline = register.drift_baseline(
        trained_frame[features],
        config,
        run_id=training,
        model_version_id="m_drift_fixture",
        primary_key=primary_key_of(config),
    )
    report = score.compute_drift(baseline, scored_frame[features], config, run_id=scoring)
    assert report is not None
    report = report.model_copy(update={"computed_at": SCORED_ON})
    storage.write_model(run_key(scoring, "drift.json"), report)
    return DriftedRun(run_id=scoring, training_run_id=training, report=report)


def _rows(storage: Storage, run_id: str) -> pd.DataFrame:
    record = storage.read_model(run_key(run_id, "run.json"), RunRecord)
    import io

    return pd.read_csv(io.BytesIO(storage.read_bytes(source_key(record))))
