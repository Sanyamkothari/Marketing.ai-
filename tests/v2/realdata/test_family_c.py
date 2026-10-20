"""Family-C real-data runners (event streams and scale): adapters and two findings, on tiny inline data.

No files, no network, a second or two. They pin:

* RetailRocket's epoch-millisecond times are refused by the mapping as published, and the runner's
  adapter turns view / addtocart / transaction rows into VIEW / CART / PURCHASE events;
* views and carts change only a customer's `last_seen_at` (and first-seen / version) in state;
* **a finding, pinned so a fix is noticed**: the SQLite event store's keyset scan, with bound
  parameters, is planned as a full index scan on every page, which makes `scan()` (and so
  `rebuild_states`) quadratic in the log size. When `autonomy/store/sql.py` is fixed this test fails;
  update `docs/v2/real_data/C-scale.md` and flip the assertion.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

RUNNERS = Path(__file__).resolve().parents[3] / "scripts" / "v2_realdata"
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

from c_adapters import (  # noqa: E402
    RR_MAPPING,
    RR_MAPPING_AS_PUBLISHED,
    RR_MAPPING_ORDERS,
    adapt_retailrocket,
)
from sqlalchemy import event as sa_event  # noqa: E402

from autonomy.contracts import Event, EventType  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.mapping import map_frame, problem_counts  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore  # noqa: E402
from autonomy.store import create_all  # noqa: E402
from autonomy.store.sql import SqlEventStore, memory_engine  # noqa: E402


def _rr_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.array([1433221332117, 1433224214164, 1433224300000], dtype="int64"),
            "visitorid": pd.array([257597, 257597, 257597], dtype="int64"),
            "event": pd.array(["view", "addtocart", "transaction"], dtype="string"),
            "itemid": pd.array([355908, 355908, 355908], dtype="int64"),
            "transactionid": [float("nan"), float("nan"), 4000.0],
        }
    )


def test_retailrocket_epoch_ms_is_refused_as_published_and_the_adapter_maps_it() -> None:
    raw = _rr_rows()
    events, problems = map_frame(raw, RR_MAPPING_AS_PUBLISHED, "retailrocket")
    assert events == []
    assert problem_counts(problems) == {"bad_timestamp": 3}

    events, problems = map_frame(adapt_retailrocket(raw), RR_MAPPING, "retailrocket")
    assert problems == []
    assert [e.type for e in events] == [EventType.VIEW, EventType.CART, EventType.PURCHASE]
    assert events[0].ts == datetime(2015, 6, 2, 5, 2, 12, 117000, tzinfo=UTC)
    buy = events[2]
    assert buy.value == 0.0 and buy.attrs["value_placeholder"] is True
    assert buy.attrs["transaction_id"] == "4000"


def test_order_grain_makes_one_purchase_per_transaction_id() -> None:
    # Two item lines of one transaction (logged 2 s apart) and a second transaction later.
    raw = pd.DataFrame(
        {
            "timestamp": pd.array([1433224300000, 1433224302000, 1433310700000], dtype="int64"),
            "visitorid": pd.array([257597, 257597, 257597], dtype="int64"),
            "event": pd.array(["transaction"] * 3, dtype="string"),
            "itemid": pd.array([1, 2, 3], dtype="int64"),
            "transactionid": [4000.0, 4000.0, 4001.0],
        }
    )
    lines, _ = map_frame(adapt_retailrocket(raw), RR_MAPPING, "retailrocket")
    orders, problems = map_frame(adapt_retailrocket(raw, grain="order"), RR_MAPPING_ORDERS, "retailrocket")
    assert problems == []
    assert len(lines) == 3  # line grain: every item line is a purchase
    assert sorted(e.attrs["transaction_id"] for e in orders) == ["4000", "4001"]
    first = min(orders, key=lambda e: e.ts)
    assert first.attrs["items"] == 2 and first.ts == datetime(2015, 6, 2, 5, 51, 40, tzinfo=UTC)


def test_views_and_carts_only_move_last_seen() -> None:
    raw = _rr_rows().iloc[:2]  # a view and a cart, no purchase
    events, _ = map_frame(adapt_retailrocket(raw), RR_MAPPING, "retailrocket")
    store = InMemoryEventStore()
    store.append(events)
    states = InMemoryStateStore()
    now = events[-1].ts + timedelta(hours=1)
    rebuild_states(store, states, StateFolder(), now)
    (s,) = list(states.scan())
    assert s.last_seen_at == events[1].ts
    assert s.purchases == 0 and s.segment == "prospect"
    assert s.top_categories == () and s.channels == {} and s.hour_counts == ()


def test_finding_sqlite_event_scan_pages_are_full_index_scans() -> None:
    engine = memory_engine()
    create_all(engine)
    store = SqlEventStore(engine, batch_size=2)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    store.append(
        Event(
            event_id=f"e{i}", customer_id="c", type=EventType.VIEW, ts=t0 + timedelta(minutes=i), source="t"
        )
        for i in range(5)
    )
    seen: list[tuple[str, object]] = []

    @sa_event.listens_for(engine, "before_cursor_execute")
    def _grab(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        seen.append((statement, parameters))

    assert len(list(store.scan())) == 5
    paged = [(s, p) for s, p in seen if "event.ts >" in s]
    assert paged, "a later page carries the keyset condition"
    statement, params = paged[-1]
    with engine.connect() as conn:
        plan = " ".join(str(r[3]) for r in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params))
    # Today: "SCAN event USING INDEX ix_event_ts_seq" (every page walks the index from the start).
    # Fixed: "SEARCH event USING INDEX ix_event_ts_seq (ts>?)".
    assert "SCAN event" in plan, plan
