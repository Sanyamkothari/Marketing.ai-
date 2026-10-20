"""The WF1 acceptance target, end to end on SQL: load online-retail as events, rebuild state, and
fetch any customer's timeline in under a second.

Runs on an in-memory SQLite engine so it stays fast and needs no server; the per-store Postgres
variants live in test_sql_stores.py.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from autonomy.contracts import EventType
from autonomy.data import StateFolder, load_online_retail_sample, rebuild_states
from autonomy.priors import SqlPriorStore
from autonomy.priors import create_all as create_prior_tables
from autonomy.store import SqlEventStore, SqlStateStore, create_all, memory_engine

NOW = datetime(2011, 9, 10, 12, tzinfo=UTC)


def test_online_retail_loads_rebuilds_and_serves_a_timeline_fast() -> None:
    loaded = load_online_retail_sample()
    assert loaded.problems == []

    engine = memory_engine()
    create_all(engine)
    create_prior_tables(engine)  # the prior table lives beside the others without clashing
    events = SqlEventStore(engine)
    states = SqlStateStore(engine)

    assert events.append(loaded.events) == len(loaded.events)
    assert events.append(loaded.events) == 0  # loading the same file again adds nothing

    written = rebuild_states(events, states, StateFolder(), NOW)
    customers = {e.customer_id for e in loaded.events}
    assert written == len(customers) == states.count()

    customer = sorted(customers)[len(customers) // 2]
    started = time.perf_counter()
    timeline = events.for_customer(customer)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
    assert timeline == sorted(timeline, key=lambda e: e.ts)
    assert {e.customer_id for e in timeline} == {customer}

    state = states.get(customer)
    assert state is not None
    purchases = [e for e in timeline if e.type is EventType.PURCHASE]
    assert state.purchases == len(purchases)
    assert state.country is not None  # country rides on the reconstructed purchases
    assert SqlPriorStore(engine).keys() == []


def test_rebuilt_state_matches_folding_each_timeline_by_hand() -> None:
    loaded = load_online_retail_sample()
    engine = memory_engine()
    create_all(engine)
    events, states = SqlEventStore(engine), SqlStateStore(engine)
    events.append(loaded.events)
    rebuild_states(events, states, StateFolder(), NOW)

    customers = sorted({e.customer_id for e in loaded.events})
    for customer in [*customers[:3], *customers[-3:]]:
        folder = StateFolder()
        state = None
        for event in events.for_customer(customer):
            state = folder.fold(state, event)
        assert state is not None
        assert states.get(customer) == folder.refresh(state, NOW)
