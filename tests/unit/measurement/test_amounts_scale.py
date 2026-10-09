"""Plan J M102: measuring an amount, adjusted, is linear in the customers (DEC-1312).

Timed through `measure_incrementality(outcome_kind="continuous", covariate_column=...)` on simulated
revenue: the join, the maturity and point-in-time rules, Welch's interval and the adjustment are column
operations and one regression slope, so ten times the customers take about ten times as long. The fast
test compares 20,000 with 200,000 customers; the slow one measures 1,000,000 (about 9 s on one shared
core; a yes/no outcome of the same size takes about 7 s, and the two share the date handling).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest

from engine.measurement.simulate import revenue_campaign
from engine.uplift.incrementality import measure_incrementality

FAST_ROWS = 20_000
ROWS = 200_000
MILLION = 1_000_000


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _timed(rows: int, seed: int) -> float:
    campaign = revenue_campaign(rows, 2.0, seed=seed, rho=0.6)
    started = time.perf_counter()
    report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.adjusted_kwargs)
    elapsed = time.perf_counter() - started
    assert report.treated_rows + report.control_rows == rows and report.adjusted_interval is not None
    return elapsed


def test_measuring_an_adjusted_amount_is_linear() -> None:
    small = min(_timed(FAST_ROWS, seed) for seed in (1, 2, 3))
    large = _timed(ROWS, 4)
    print(f"\n[Perf] adjusted amount: {FAST_ROWS:,} rows {small:.3f}s; {ROWS:,} rows {large:.2f}s")
    # Linear work gives about 10x for ten times the rows and quadratic work about 100x; 30x leaves room
    # for contention while still catching anything worse than linear.
    assert large < 30 * max(small, 0.02), "ten times the rows took far more than ten times as long"


@pytest.mark.slow
def test_a_million_customers_are_measured_in_linear_time() -> None:
    small = min(_timed(ROWS // 2, seed) for seed in (5, 6))
    large = _timed(MILLION, 7)
    print(f"\n[Perf] adjusted amount: {ROWS // 2:,} rows {small:.2f}s; {MILLION:,} rows {large:.2f}s")
    assert large < 30 * max(small, 0.05), "ten times the rows took far more than ten times as long"
    assert large < 120.0, f"{MILLION:,} rows took {large:.1f}s"
