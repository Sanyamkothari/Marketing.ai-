"""The gate at scale (Plan K M134; design §11: 1 million customers x 3 channels in under 60 s).

`decide` is timed on 1,000,000 customers, each with three planned channels (sms, email, whatsapp), against a
contactability file of the same customers in another order, opt-in grants for a third of them and the
per-message verdicts of three actions - every input a hand-off gives it. The best of three runs at each size,
the two sizes taking turns, is held under the budget and against a 100,000-customer run: linear work takes
about ten times as long for ten times the customers, quadratic work about a hundred times.
"""

from __future__ import annotations

import time

import numpy as np
import pyarrow as pa
import pytest

from engine.compliance.gate import GateRules, MessageVerdict, decide

ROWS = 1_000_000
SMALL_ROWS = 100_000
BUDGET_SECONDS = 60.0
CHANNELS = ("sms", "email", "whatsapp")
ACTIONS = ("offer_a", "offer_b", "offer_c")


def _inputs(
    rows: int, seed: int
) -> tuple[pa.Table, pa.Table, pa.Table, dict[tuple[str, str], MessageVerdict]]:
    rng = np.random.default_rng(seed)
    ids = np.char.add("C-", np.arange(rows).astype(str)).astype(object)
    customers = pa.table(
        {
            "customer_id": pa.array(ids, pa.string()),
            "action_id": pa.array(np.asarray(ACTIONS, dtype=object)[rng.integers(0, 3, rows)], pa.string()),
        }
    )
    order = rng.permutation(rows)
    contactability = pa.table(
        {
            "customer_id": pa.array(ids[order], pa.string()),
            "contactable_sms": pa.array(rng.random(rows) < 0.8),
            "contactable_email": pa.array(rng.random(rows) < 0.7),
        }
    )
    granted = rng.random(rows) < 0.3
    opt_ins = pa.table(
        {
            "customer_id": pa.array(ids[granted], pa.string()),
            "channel": pa.array(["whatsapp"] * int(granted.sum()), pa.string()),
            "consent_seq": pa.array(np.arange(int(granted.sum()), dtype=np.int64)),
        }
    )
    messages = {
        (action, channel): MessageVerdict(
            template_mismatch=action == "offer_b" and channel == "sms",
            url_not_whitelisted=action == "offer_c" and channel == "sms",
            catalogue_ref=f"{'a' * 64}#{action}",
        )
        for action in ACTIONS
        for channel in CHANNELS
    }
    return customers, contactability, opt_ins, messages


def _decide(rows: int, seed: int) -> float:
    customers, contactability, opt_ins, messages = _inputs(rows, seed)
    start = time.perf_counter()
    table = decide(
        customers,
        key_columns=("customer_id",),
        planned={None: (), **dict.fromkeys(ACTIONS, CHANNELS)},
        rules=GateRules(
            rule_version="r" * 64,
            purpose="marketing_communication",
            contactability_ref="channel_contactability.parquet#sha256=" + "c" * 64,
            opt_in_channels=("whatsapp",),
        ),
        contactability=contactability,
        opt_ins=opt_ins,
        messages=messages,
    )
    elapsed = time.perf_counter() - start
    assert table.num_rows == 3 * rows
    allowed = np.asarray(table["allowed"].to_numpy(zero_copy_only=False), dtype=bool)
    assert 0 < int(allowed.sum()) < 3 * rows
    return elapsed


@pytest.mark.slow
def test_a_million_customers_on_three_channels_are_decided_within_the_budget_and_linearly() -> None:
    small = large = float("inf")
    for _ in range(3):
        small = min(small, _decide(SMALL_ROWS, 5))
        large = min(large, _decide(ROWS, 6))
    print(
        f"\n[Perf] compliance gate decide: {ROWS:,} customers x {len(CHANNELS)} channels {large:.2f}s "
        f"(budget {BUDGET_SECONDS:.0f}s); {SMALL_ROWS:,} customers {small:.2f}s"
    )
    assert large < BUDGET_SECONDS, f"{ROWS:,} customers took {large:.2f}s"
    assert large < 30 * max(small, 0.05), "ten times the customers took far more than ten times as long"


def test_a_hundred_thousand_customers_on_three_channels_take_seconds_not_minutes() -> None:
    """The fast suite's guard: the 100k run alone, well inside a tenth of the 1M budget."""
    elapsed = min(_decide(SMALL_ROWS, 7) for _ in range(2))
    print(f"\n[Perf] compliance gate decide: {SMALL_ROWS:,} customers x 3 channels {elapsed:.2f}s")
    assert elapsed < BUDGET_SECONDS / 10
