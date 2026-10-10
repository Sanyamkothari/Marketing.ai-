"""Plan K M135: adjusting by the scoring run's prediction is linear in the customers (DEC-1324).

Timed through `measure_incrementality(run_prediction=...)` on a simulated yes/no campaign: the key join of
the prediction (a hash join, `reindex` on the text keys), the point-in-time rule and the adjusted difference
are column operations and one regression slope, so ten times the customers take about ten times as long.
The fast test compares 20,000 with 200,000 customers; the slow one 100,000 (best of three) with 1,000,000.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest

from engine.measurement.simulate import predicted_campaign
from engine.uplift.incrementality import measure_incrementality

FAST_ROWS = 20_000
ROWS = 200_000
TENTH = 100_000
MILLION = 1_000_000
RATIO = 30.0
"""Linear work gives about 10x for ten times the rows and quadratic about 100x; 30x leaves room for contention."""


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _timed(rows: int, seed: int) -> float:
    campaign = predicted_campaign(rows, 0.15, 0.02, seed=seed)
    started = time.perf_counter()
    report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.adjusted_kwargs)
    elapsed = time.perf_counter() - started
    assert report.treated_rows + report.control_rows == rows and report.adjusted_interval is not None
    return elapsed


def test_adjusting_by_the_runs_prediction_is_linear() -> None:
    small = min(_timed(FAST_ROWS, seed) for seed in (1, 2, 3))
    large = _timed(ROWS, 4)
    print(f"\n[Perf] run-score adjustment: {FAST_ROWS:,} rows {small:.3f}s; {ROWS:,} rows {large:.2f}s")
    assert large < RATIO * max(small, 0.02), "ten times the rows took far more than ten times as long"


@pytest.mark.slow
def test_a_million_customers_are_adjusted_in_linear_time() -> None:
    small = min(_timed(TENTH, seed) for seed in (5, 6, 7))
    large = _timed(MILLION, 8)
    print(f"\n[Perf] run-score adjustment: {TENTH:,} rows {small:.2f}s; {MILLION:,} rows {large:.2f}s")
    assert large < RATIO * max(small, 0.05), "ten times the rows took far more than ten times as long"
    assert large < 120.0, f"{MILLION:,} rows took {large:.1f}s"
