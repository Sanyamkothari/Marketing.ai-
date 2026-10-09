"""The review's paths at scale (Plan J M101, DEC-1311): two kinds of value, explore rows, hold-outs and a
channel cap, over 200,000 customers. The same 3 second budget as `test_arbitrate.py`'s plain test; there is
no Python loop over the rows, so one million customers take about five times as long."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from engine.decide.arbitrate import ArbitrationConfig, arbitrate_treat_lists

N = 200_000
BUDGET_SECONDS = 3.0


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


def test_arbitrate_200k_rows_with_mixed_values_explore_and_caps() -> None:
    rng = np.random.default_rng(7)
    ids = [f"C-{i:07d}" for i in range(N)]
    lists = [
        _frame(ids[:150_000], rng, "uc-1", "email", gross=False),
        _frame(ids[50_000:], rng, "uc-2", "sms", gross=True),
        _frame(ids[100_000:], rng, "uc-3", "sms", gross=False),
    ]
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
