"""Scale fixes in autonomy/store: the SQL keyset scan walks its index once, and the compact in-memory
event store holds the same events as the fake at a fraction of the memory.

Inline data only; each test runs in well under a second.
"""

from __future__ import annotations

import random
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex

from autonomy.contracts import Channel, Decision, DecisionKind, Event, EventType
from autonomy.data.folder import StateFolder
from autonomy.data.rebuild import rebuild_states
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore
from autonomy.interfaces import EventStore
from autonomy.store import create_all, memory_engine
from autonomy.store.memory import _FAST, COMPACT, CompactEventStore
from autonomy.store.schema import metadata
from autonomy.store.sql import SqlDecisionLog, SqlEventStore

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
BERLIN = ZoneInfo("Europe/Berlin")
IST = timezone(timedelta(hours=5, minutes=30))
ZULU = timezone(timedelta(0), "Z")  # equal to UTC, but prints differently


# --- SQL: the keyset scan ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A fresh in-memory SQLite database with the v2 tables."""
    db = memory_engine()
    create_all(db)
    yield db
    db.dispose()


def _captured(engine: Engine, marker: str) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []

    @sa_event.listens_for(engine, "before_cursor_execute")
    def _grab(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if marker in statement and not statement.startswith("EXPLAIN"):
            seen.append((statement, parameters))

    return seen


def _plan(engine: Engine, statement: str, params: Any) -> str:
    with engine.connect() as conn:
        return " | ".join(str(r[3]) for r in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params))


def _views(n: int, *, step: timedelta = timedelta(minutes=1), ties: int = 1) -> list[Event]:
    """`n` VIEW events; `ties` consecutive events share each timestamp."""
    return [
        Event(
            event_id=f"e{(i * 7919) % 1009:04d}-{i}",  # ids not in time order
            customer_id=f"c{i % 5}",
            type=EventType.VIEW,
            ts=T0 + step * (i // ties),
            source="t",
        )
        for i in range(n)
    ]


@pytest.mark.parametrize("since", [None, T0 + timedelta(minutes=5)])
def test_sqlite_event_scan_pages_search_the_order_index(engine: Engine, since: datetime | None) -> None:
    store = SqlEventStore(engine, batch_size=3)
    store.append(_views(20))
    seen = _captured(engine, "event.ts >")
    got = list(store.scan(since=since))
    assert len(got) == (20 if since is None else 15)
    assert seen, "later pages carry the keyset condition"
    for statement, params in seen:
        plan = _plan(engine, statement, params)
        # Before the fix: "SCAN event USING INDEX ix_event_ts_seq | USE TEMP B-TREE ...", a walk of
        # the whole index on every page (quadratic). Now each page seeks to where the last ended.
        assert "SEARCH event USING INDEX ix_event_order (ts>?)" in plan, plan
        assert "TEMP B-TREE" not in plan, plan


def test_sqlite_decision_scan_pages_search_the_order_index(engine: Engine) -> None:
    log = SqlDecisionLog(engine, batch_size=2)
    log.record(
        Decision(
            decision_id=f"d{i}",
            customer_id="c",
            made_at=T0 + timedelta(minutes=i // 2),
            kind=DecisionKind.NOTHING,
            probability=1.0,
            reason="test",
            policy_version="v0",
        )
        for i in range(7)
    )
    seen = _captured(engine, "decision.made_at >")
    assert [d.decision_id for d in log.scan()] == [f"d{i}" for i in range(7)]
    assert seen
    for statement, params in seen:
        plan = _plan(engine, statement, params)
        assert "SEARCH decision USING INDEX ix_decision_order (made_at>?)" in plan, plan
        assert "TEMP B-TREE" not in plan, plan


@pytest.mark.parametrize("batch", [1, 2, 3, 1000])
def test_sql_scan_order_and_windows_match_the_fake_with_ties(engine: Engine, batch: int) -> None:
    # Equal timestamps, ids out of time order, appended in two calls: the order is (ts, insertion).
    events = _views(24, ties=4)
    sql = SqlEventStore(engine, batch_size=batch)
    fake = InMemoryEventStore()
    for part in (events[:13], events[13:]):
        sql.append(reversed(part[:3]))
        sql.append(part[3:])
        fake.append(reversed(part[:3]))
        fake.append(part[3:])
    windows = [(None, None), (T0 + timedelta(minutes=2), None), (None, T0 + timedelta(minutes=4))]
    windows.append((T0 + timedelta(minutes=1), T0 + timedelta(minutes=5)))
    for since, until in windows:
        want = [e.event_id for e in fake.scan(since=since, until=until)]
        assert [e.event_id for e in sql.scan(since=since, until=until)] == want


def test_create_all_adds_new_indexes_to_an_existing_database(engine: Engine) -> None:
    with engine.begin() as conn:  # a database made before the order indexes existed
        conn.execute(text("DROP INDEX ix_event_order"))
        conn.execute(text("DROP INDEX ix_decision_order"))
        conn.execute(text("CREATE INDEX ix_event_ts_seq ON event (ts, seq)"))
    create_all(engine)
    names = {i["name"] for i in inspect(engine).get_indexes("event")}
    assert {"ix_event_order", "ix_event_ts_seq"} <= names
    assert "ix_decision_order" in {i["name"] for i in inspect(engine).get_indexes("decision")}
    assert not any(n and n.endswith("_c") for n in names), "the Postgres-only copies stay off SQLite"


def test_postgres_gets_order_indexes_in_code_point_collation() -> None:
    ddl = {
        str(i.name): str(CreateIndex(i).compile(dialect=PGDialect()))  # type: ignore[no-untyped-call]
        for t in metadata.sorted_tables
        for i in t.indexes
    }
    assert (
        ddl["ix_event_order_c"] == 'CREATE INDEX ix_event_order_c ON event (ts, seq, (event_id COLLATE "C"))'
    )
    assert ddl["ix_decision_order_c"].endswith('(made_at, seq, (decision_id COLLATE "C"))')
    assert ddl["ix_customer_state_id_c"].endswith('((customer_id COLLATE "C"))')


# --- the compact in-memory event store --------------------------------------------------------------


def _varied(n: int, seed: int) -> list[Event]:
    """Events using every field: channels, decisions, None and float amounts, attrs of each scalar
    type, reply text, several time zones (one wall time in a daylight-saving gap), duplicates."""
    rng = random.Random(seed)
    out: list[Event] = []
    for _ in range(n):
        kind = rng.choice([EventType.VIEW, EventType.PURCHASE, EventType.SEND, EventType.REPLY])
        zone = rng.choice([UTC, IST, BERLIN, ZULU])
        ts = (T0 + timedelta(minutes=rng.randrange(0, 40), microseconds=rng.randrange(3))).astimezone(zone)
        fields: dict[str, Any] = {
            "event_id": f"ev{rng.randrange(n * 3)}",
            "customer_id": f"cust{rng.randrange(6)}",
            "type": kind,
            "ts": ts,
            "source": rng.choice(["csv", "gym"]),
        }
        if kind in (EventType.SEND, EventType.REPLY):
            fields["channel"] = rng.choice(list(Channel))
            fields["decision_id"] = rng.choice(["dA", "dB", "", None])
        if kind is EventType.PURCHASE:
            fields["value"] = rng.choice([10, 12.5, 0.0, -0.0])
            fields["margin"] = rng.choice([None, 3.25])
            fields["category"] = rng.choice(["shoes", None])
            fields["item_id"] = f"i{rng.randrange(4)}"
        if kind is EventType.REPLY:
            fields["text"] = rng.choice(["stop", "yes please"])
        if rng.random() < 0.4:
            fields["attrs"] = rng.choice([{"k": True}, {"k": 1}, {"k": 1.0}, {"k": "v", "n": 2}, {}])
        out.append(Event(**fields))
    # a wall time that does not exist in Berlin (clocks jump 02:00 -> 03:00 on 29 Mar 2026), and a
    # repeated one (fold=1 on 25 Oct 2026); neither survives a plain trip through UTC unchanged
    gap = datetime(2026, 3, 29, 2, 30, tzinfo=BERLIN)
    fold = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=BERLIN)
    for k, ts in enumerate((gap, fold)):
        out.append(Event(event_id=f"tz{k}", customer_id="cust0", type=EventType.VIEW, ts=ts, source="csv"))
    return out


def _same(a: list[Event], b: list[Event]) -> None:
    assert [e.event_id for e in a] == [e.event_id for e in b]
    for x, y in zip(a, b, strict=True):
        assert x == y
        assert x.model_fields_set == y.model_fields_set
        assert x.model_dump(mode="json") == y.model_dump(mode="json")
        assert repr(x) == repr(y)


def _both(events: list[Event]) -> tuple[InMemoryEventStore, CompactEventStore]:
    fake, compact = InMemoryEventStore(), CompactEventStore()
    for start in range(0, len(events), 37):  # several appends, not in time order
        part = events[start : start + 37]
        assert compact.append(part) == fake.append(part)
    return fake, compact


def test_layout_covers_every_event_field() -> None:
    # If this fails, Event gained or lost a field: update `_LAYOUT` and `_event` in memory.py. Until
    # then the store keeps every event whole (correct, no memory saving).
    assert COMPACT
    # Events are rebuilt without model_construct's per-field work; if a pydantic upgrade breaks the
    # preconditions the store falls back to model_construct (slower, still correct).
    assert _FAST


@pytest.mark.parametrize("seed", [1, 2])
def test_compact_store_reads_back_what_the_fake_does(seed: int) -> None:
    events = _varied(300, seed)
    fake, compact = _both(events)
    assert isinstance(compact, EventStore)
    assert len(compact) == len(fake) == compact.count()
    assert compact.append(events[:50]) == 0, "repeated ids are ignored"
    windows = [(None, None), (T0 + timedelta(minutes=10), None), (None, T0 + timedelta(minutes=30))]
    windows.append((T0 + timedelta(minutes=5, microseconds=1), T0 + timedelta(minutes=25)))
    for since, until in windows:
        _same(list(compact.scan(since=since, until=until)), list(fake.scan(since=since, until=until)))
        for c in [f"cust{i}" for i in range(7)]:
            got = compact.for_customer(c, since=since, until=until)
            _same(got, fake.for_customer(c, since=since, until=until))
    for d in ["dA", "dB", "", "nope"]:
        _same(compact.for_decision(d), fake.for_decision(d))


def test_compact_store_returns_fresh_attrs() -> None:
    store = CompactEventStore()
    store.append(
        [Event(event_id="a", customer_id="c", type=EventType.VIEW, ts=T0, source="s", attrs={"k": 1})]
    )
    first = store.for_customer("c")[0]
    first.attrs["k"] = 2  # a frozen model's dict is still mutable; the store must not share it
    assert store.for_customer("c")[0].attrs == {"k": 1}


def test_compact_scan_sees_late_and_out_of_order_appends() -> None:
    store, fake = CompactEventStore(), InMemoryEventStore()
    events = _views(30)
    for s in (store, fake):
        s.append(events[10:20])
    assert [e.event_id for e in store.scan()] == [e.event_id for e in fake.scan()]
    for s in (store, fake):
        s.append(events[20:])  # later times: the cached order is extended
    _same(list(store.scan()), list(fake.scan()))
    for s in (store, fake):
        s.append(events[:10])  # earlier times: re-sorted
    _same(list(store.scan()), list(fake.scan()))
    running = store.scan()
    first = next(running)
    store.append([Event(event_id="late", customer_id="c0", type=EventType.VIEW, ts=T0, source="t")])
    assert first.event_id == events[0].event_id
    assert "late" not in {e.event_id for e in running}, "a scan is a snapshot from its first read"
    assert len(list(store.scan())) == 31


def test_compact_store_refuses_naive_windows() -> None:
    store = CompactEventStore()
    store.append(_views(3))
    with pytest.raises(TypeError):
        list(store.scan(since=datetime(2026, 1, 1)))  # naive on purpose


def test_rebuild_from_compact_store_matches_the_fake() -> None:
    events = _varied(200, 3)
    fake, compact = _both(events)
    now = T0 + timedelta(days=1)
    a, b = InMemoryStateStore(), InMemoryStateStore()
    assert rebuild_states(fake, a, StateFolder(), now) == rebuild_states(compact, b, StateFolder(), now)
    assert [s.model_dump() for s in a.scan()] == [s.model_dump() for s in b.scan()]


def _traced_bytes(build: Any) -> tuple[int, Any]:
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        kept = build()
        return tracemalloc.get_traced_memory()[0] - before, kept
    finally:
        tracemalloc.stop()


def _stream(n: int) -> Iterator[Event]:
    for i in range(n):
        yield Event(
            event_id=f"retail_{i:012d}",
            customer_id=f"{100000 + i % 3000}",
            type=EventType.VIEW if i % 10 else EventType.PURCHASE,
            ts=T0 + timedelta(seconds=17 * i),
            source="retailrocket",
            item_id=f"{i % 4000}",
            value=None if i % 10 else 19.5,
        )


def test_compact_store_holds_a_fraction_of_the_fakes_memory() -> None:
    n = 20_000

    def fill(store: Any) -> Any:
        store.append(_stream(n))
        return store

    fake_bytes, _ = _traced_bytes(lambda: fill(InMemoryEventStore()))
    compact_bytes, kept = _traced_bytes(lambda: fill(CompactEventStore()))
    assert len(kept) == n
    # Measured here: about 1,800 bytes per event in the fake and about 200 in the compact store.
    assert fake_bytes / n > 1000
    assert compact_bytes / n < 400
    assert compact_bytes * 5 < fake_bytes


class _MyTime(datetime):
    """A datetime subclass (as pandas' Timestamp is): the store must hand back the same object."""


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")  # the unvalidated list attrs
def test_compact_store_keeps_events_it_cannot_pack_whole() -> None:
    odd = [
        Event.model_construct(
            event_id="sub",
            customer_id="c",
            type=EventType.VIEW,
            ts=_MyTime(2026, 3, 1, 9, tzinfo=UTC),
            source="s",
        ),
        Event.model_construct(
            event_id="list", customer_id="c", type=EventType.VIEW, ts=T0, source="s", attrs={"k": [1, 2]}
        ),
    ]
    plain = Event(event_id="plain", customer_id="c", type=EventType.VIEW, ts=T0, source="s")
    fake, compact = _both([*odd, plain])
    _same(compact.for_customer("c"), fake.for_customer("c"))
    got = compact.for_customer("c")
    assert got[0] is odd[0] and got[1] is odd[1] and got[2] is not plain
    assert type(got[0].ts) is _MyTime


def test_compact_store_keeps_the_sign_of_a_zero_attr() -> None:
    # 0.0 == -0.0 and they hash alike: pooling attrs by value alone read {"x": -0.0} back as 0.0.
    events = [
        Event(event_id=f"z{i}", customer_id="c", type=EventType.VIEW, ts=T0, source="s", attrs={"x": v})
        for i, v in enumerate([0.0, -0.0, 0.0])
    ]
    fake, compact = _both(events)
    _same(compact.for_customer("c"), fake.for_customer("c"))
    assert [repr(e.attrs["x"]) for e in compact.for_customer("c")] == ["0.0", "-0.0", "0.0"]


def test_compact_store_takes_times_whose_utc_instant_is_out_of_range() -> None:
    # 0001-01-01 00:00 at +05:00 is valid, but its UTC instant is before year 1: rebuilding it from
    # UTC microseconds overflows, so it must be kept as given rather than fail the append.
    early = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5)))
    events = [
        Event(event_id="early", customer_id="c", type=EventType.VIEW, ts=early, source="s"),
        Event(event_id="now", customer_id="c", type=EventType.VIEW, ts=T0, source="s"),
    ]
    fake, compact = _both(events)
    _same(compact.for_customer("c"), fake.for_customer("c"))
    _same(list(compact.scan()), list(fake.scan()))
