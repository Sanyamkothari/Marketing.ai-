"""The review's paths at scale (Plan J M101, DEC-1311): two kinds of value, explore rows, hold-outs and a
channel cap, over 200,000 customers.

There is no Python loop over the rows, so one million customers take about five times as long. Two tests:
the fast one measures how the time grows (200,000 rows against 50,000 in the same process, so a busy
machine slows both), and the `slow` one holds the 200,000-row run to a wall-clock budget, which only means
something on a machine that is not shared.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from engine.decide.arbitrate import ArbitrationConfig, arbitrate_treat_lists

N = 200_000
SMALL = 50_000
BUDGET_SECONDS = 3.0
GROWTH_LIMIT = 6.0
"""Four times the rows may take at most this many times as long (linear is 4; the sorts add a little)."""


def _frame(
    ids: list[str], rng: np.random.Generator, use_case: str, channel: str, *, gross: bool
) -> pd.DataFrame:
    m = len(ids)
    values = rng.uniform(10.0, 100.0, m)
    return pd.DataFrame(
        {
            "customer_id": ids,
            "use_case": use_case,
            "model_version": "m",
            "segment": "persuadable",
            "treat": rng.random(m) > 0.4,
            "holdout": rng.random(m) > 0.9,
            "explore": rng.random(m) > 0.95,
            "suppression_reason": None,
            "offer": "Offer",
            "channel": channel,
            "net_value": None if gross else values,
            "expected_gross_value": values if gross else None,
            "reason_1": None,
            "reason_2": None,
            "reason_3": None,
        }
    )


def _lists(n: int, seed: int) -> list[pd.DataFrame]:
    """Three overlapping lists over `n` customers: two kinds of value, explore rows and hold-outs."""
    rng = np.random.default_rng(seed)
    ids = [f"C-{i:07d}" for i in range(n)]
    a, b, c = (n * 3) // 4, n // 4, n // 2
    return [
        _frame(ids[:a], rng, "uc-1", "email", gross=False),
        _frame(ids[b:], rng, "uc-2", "push", gross=True),
        _frame(ids[c:], rng, "uc-3", "sms", gross=False),
    ]


def _timed(n: int, config: ArbitrationConfig) -> float:
    """The best of two runs (the lists are built once): a busy machine slows a run, rarely every run."""
    lists = _lists(n, seed=7)
    best = float("inf")
    for _ in range(2):
        start = time.perf_counter()
        arbitrate_treat_lists(lists, config=config)
        best = min(best, time.perf_counter() - start)
    return best


def test_arbitration_time_grows_linearly_with_the_rows() -> None:
    config = ArbitrationConfig(use_cases={"uc-2": {"priority": 2.0}}, channel_caps={"sms": 5_000})
    small = _timed(SMALL, config)
    large = _timed(N, config)
    assert large < GROWTH_LIMIT * small, (
        f"{N} rows took {large:.2f}s and {SMALL} rows took {small:.2f}s: "
        f"{large / small:.1f} times as long for 4 times the rows (limit {GROWTH_LIMIT})"
    )


@pytest.mark.slow
def test_arbitrate_200k_rows_with_mixed_values_explore_and_caps() -> None:
    lists = _lists(N, seed=7)
    config = ArbitrationConfig(use_cases={"uc-2": {"priority": 2.0}}, channel_caps={"sms": 20_000})

    start = time.perf_counter()
    arbitrated, summary = arbitrate_treat_lists(lists, config=config)
    elapsed = time.perf_counter() - start

    assert (
        elapsed < BUDGET_SECONDS
    ), f"Arbitration of 200k rows took {elapsed:.2f}s (budget: {BUDGET_SECONDS}s)"
    treated = arbitrated[arbitrated["treat"]]
    assert treated["customer_id"].is_unique
    assert int((treated["channel"] == "sms").sum()) <= 20_000
    assert summary.customers_decided_by_priority > 0
    assert summary.customers_decided_by_value > 0
