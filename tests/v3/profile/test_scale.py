"""The nightly build must not grow with the shop. A quick bounded-memory check, and the 100k bench (slow)."""

from __future__ import annotations

import json
import subprocess
import sys
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from autonomy.data import StateFolder, rebuild_states
from autonomy.profile import build_profiles
from autonomy.store import SqlEventStore, SqlStateStore, create_all, memory_engine
from tests.v3.profile.synthetic import customer_id, events

AS_OF = datetime(2026, 1, 31, tzinfo=UTC)
BATCH = 100


def _peak_python_bytes(customers: int) -> int:
    """Peak Python-heap bytes allocated by one `build_profiles`, with both stores in SQLite (C memory)."""
    engine = memory_engine()
    create_all(engine)
    store, states = SqlEventStore(engine, batch_size=BATCH), SqlStateStore(engine, batch_size=BATCH)
    store.append(events(customers))
    rebuild_states(store, states, StateFolder(), AS_OF)
    tracemalloc.start()
    try:
        assert build_profiles(store, states, AS_OF, batch_size=BATCH) == customers
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    first = states.get(customer_id(0))
    assert first is not None and first.features_as_of == AS_OF
    return peak


def test_memory_does_not_grow_with_the_number_of_customers() -> None:
    small, large = _peak_python_bytes(200), _peak_python_bytes(1_000)
    # five times the customers, the same batch and page sizes: one batch, one page, a few hundred counters
    assert large < small * 1.5 + 2_000_000, (small, large)
    assert large < 25_000_000


@pytest.mark.slow
def test_a_hundred_thousand_customers(record_property: Callable[[str, object], None]) -> None:
    """Runs `python -m tests.v3.profile.bench --customers 100000` and keeps its numbers.

    The bench fills a SQLite file with ~1.5M events and one folded state per customer (a few minutes),
    then times `build_profiles` in a fresh process. Its figures go into docs/v3/PROFILE.md.
    """
    out = subprocess.run(
        [sys.executable, "-m", "tests.v3.profile.bench", "--customers", "100000"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    for key, value in result.items():
        record_property(key, value)
    print(json.dumps(result))
    assert result["customers"] == 100_000
    assert result["rss_growth_mb"] < 600, result
    assert result["customers_per_s"] > 300, result
