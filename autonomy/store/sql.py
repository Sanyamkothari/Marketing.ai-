"""SQL versions of the event store, state store, decision log and workspace (SQLAlchemy Core).

They behave exactly like the in-memory fakes in `autonomy.fakes.stores`; the parametrised tests in
`tests/v2/backbone/test_sql_stores.py` run the same assertions against both. Three details make that
true and are easy to break:

* **Ties.** The fakes sort with Python's stable sort over insertion order, so rows with equal
  timestamps come back in the order they were first written. Tables carry a `seq` column for this.
* **String order.** The fakes sort ids by code point. Postgres sorts text by the database's locale
  unless told otherwise, so every id comparison goes through `COLLATE "C"` there (byte order of
  UTF-8 is code-point order). SQLite's default BINARY collation already does this.
* **Duplicates.** A repeated `event_id` is ignored (first one wins); a repeated decision, goal,
  content item or customer state replaces the old one but keeps its original position.

Only Postgres and SQLite get the native upsert / insert-ignore statements; any other dialect falls
back to a row-by-row version that is correct but slow.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, and_, create_engine, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.elements import KeyedColumnElement
from sqlalchemy.sql.schema import Table

from autonomy.contracts import Brief, ContentItem, ContentStatus, CustomerState, Decision, Event, Goal
from autonomy.store.schema import (
    brief_table,
    content_item_table,
    customer_state_table,
    decision_table,
    event_table,
    goal_table,
)

__all__ = [
    "SqlDecisionLog",
    "SqlEventStore",
    "SqlStateStore",
    "SqlWorkspace",
    "insert_ignore",
    "memory_engine",
    "next_seq",
    "sortable",
    "upsert",
]

T = TypeVar("T")
Row = dict[str, Any]

DEFAULT_BATCH = 1000
"""Rows fetched per round trip when streaming a scan."""


def memory_engine() -> Engine:
    """An in-memory SQLite engine whose one connection is shared, so every store sees the same data."""
    return create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})


# --- shared helpers -------------------------------------------------------------------------------


def sortable(column: KeyedColumnElement[Any], dialect: str) -> ColumnElement[Any]:
    """`column` compared in code-point order, as Python sorts strings (see the module docstring)."""
    return column.collate("C") if dialect == "postgresql" else column


def next_seq(conn: Connection, table: Table) -> int:
    """The next free write-order number for `table`.

    Two writers in parallel can draw the same number. That only matters for rows with equal
    timestamps written at the same moment by different processes, whose order is arbitrary anyway;
    the id then breaks the tie so the order is still stable.
    """
    current = conn.execute(select(func.max(table.c.seq))).scalar()
    return int(current or 0) + 1


def _chunk_size(conn: Connection, table: Table) -> int:
    """Rows per multi-row statement, kept under each driver's bound-parameter limit."""
    budget = 900 if conn.dialect.name == "sqlite" else 30_000
    return max(1, budget // len(table.columns))


def _chunks(rows: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _key_clause(table: Table, keys: Sequence[str], row: Mapping[str, Any]) -> ColumnElement[bool]:
    return and_(*(table.c[k] == row[k] for k in keys))


def insert_ignore(conn: Connection, table: Table, rows: Sequence[Row]) -> int:
    """Insert rows, silently skipping any whose primary key exists; return how many were new.

    One multi-row statement per chunk rather than `executemany`. On Postgres the rows actually
    inserted are counted from `RETURNING` (a conflict returns nothing): SQLAlchemy 2 with psycopg 3
    reports a core INSERT's `rowcount` as -1, so it cannot be read there.
    """
    if not rows:
        return 0
    dialect = conn.dialect.name
    new = 0
    returned = list(table.primary_key.columns) or list(table.columns)[:1]
    for chunk in _chunks(rows, _chunk_size(conn, table)):
        if dialect == "postgresql":
            statement = pg_insert(table).values(list(chunk)).on_conflict_do_nothing().returning(*returned)
            new += len(conn.execute(statement).fetchall())
        elif dialect == "sqlite":
            new += conn.execute(insert(table).values(list(chunk)).prefix_with("OR IGNORE")).rowcount
        else:
            keys = [c.name for c in table.primary_key.columns]
            for row in chunk:
                exists = conn.execute(select(table.c[keys[0]]).where(_key_clause(table, keys, row))).first()
                if exists is None:
                    conn.execute(insert(table).values(row))
                    new += 1
    return new


def upsert(conn: Connection, table: Table, rows: Sequence[Row], *, keep: Sequence[str] = ("seq",)) -> None:
    """Insert rows, replacing existing ones by primary key but leaving the `keep` columns as they were.

    Rows must have distinct keys: Postgres refuses to update one row twice in one statement.
    """
    if not rows:
        return
    keys = [c.name for c in table.primary_key.columns]
    changing = [c.name for c in table.columns if c.name not in keys and c.name not in keep]
    dialect = conn.dialect.name
    for chunk in _chunks(rows, _chunk_size(conn, table)):
        if dialect == "postgresql":
            pg = pg_insert(table).values(list(chunk))
            conn.execute(
                pg.on_conflict_do_update(index_elements=keys, set_={c: pg.excluded[c] for c in changing})
            )
        elif dialect == "sqlite":
            lite = sqlite_insert(table).values(list(chunk))
            conn.execute(
                lite.on_conflict_do_update(index_elements=keys, set_={c: lite.excluded[c] for c in changing})
            )
        else:
            for row in chunk:
                result = conn.execute(
                    update(table).where(_key_clause(table, keys, row)).values({c: row[c] for c in changing})
                )
                if result.rowcount == 0:
                    conn.execute(insert(table).values(row))


def _window(
    column: KeyedColumnElement[Any], since: datetime | None, until: datetime | None
) -> list[ColumnElement[bool]]:
    """`since <= column < until`, the window the fakes use, as SQL conditions."""
    clauses: list[ColumnElement[bool]] = []
    if since is not None:
        clauses.append(column >= since)
    if until is not None:
        clauses.append(column < until)
    return clauses


def _after(
    ts: KeyedColumnElement[Any],
    seq: KeyedColumnElement[Any],
    key: ColumnElement[Any],
    last: tuple[datetime, int, str],
) -> ColumnElement[bool]:
    """Rows after `last` in (ts, seq, key) order: the keyset condition of a time-ordered scan.

    The leading `ts >= last_ts` is implied by the rest but must be there. Without it the condition
    is a bare `OR`, which SQLite (with bound parameters) plans as a walk of the whole index from the
    start, filtering every row, and Postgres as an index scan from the start with a filter: every
    page then costs as much as everything before it and a full scan is quadratic (709 s for 2.76M
    events on SQLite, docs/v2/real_data/C-scale.md section 4). With it both start the index range
    at `last_ts` (SQLite: `SEARCH ... (ts>?)`, Postgres: an `Index Cond` on ts).
    """
    last_ts, last_seq, last_key = last
    return and_(
        ts >= last_ts,
        or_(ts > last_ts, and_(ts == last_ts, or_(seq > last_seq, and_(seq == last_seq, key > last_key)))),
    )


# --- events ---------------------------------------------------------------------------------------


class SqlEventStore:
    """The append-only event store. A repeated `event_id` is ignored, within a batch or across them."""

    def __init__(self, engine: Engine, *, batch_size: int = DEFAULT_BATCH) -> None:
        self._engine = engine
        self._batch = batch_size
        self._dialect = engine.dialect.name

    def append(self, events: Iterable[Event]) -> int:
        unique: dict[str, Event] = {}
        for e in events:
            unique.setdefault(e.event_id, e)  # the first copy wins, as in the fake
        if not unique:
            return 0
        with self._engine.begin() as conn:
            base = next_seq(conn, event_table)
            rows = [
                {
                    "event_id": e.event_id,
                    "customer_id": e.customer_id,
                    "type": e.type.value,
                    "ts": e.ts,
                    # The fake indexes only a truthy decision_id, so "" is stored as no decision at
                    # all and `for_decision("")` finds nothing on both. `body` keeps the "".
                    "decision_id": e.decision_id or None,
                    "seq": base + i,
                    "body": e.model_dump(mode="json"),
                }
                for i, e in enumerate(unique.values())
            ]
            return insert_ignore(conn, event_table, rows)

    def _ordered(self) -> list[ColumnElement[Any]]:
        t = event_table.c
        return [t.ts, t.seq, sortable(t.event_id, self._dialect)]

    def for_customer(
        self, customer_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Event]:
        t = event_table.c
        query = (
            select(t.body)
            .where(t.customer_id == customer_id, *_window(t.ts, since, until))
            .order_by(*self._ordered())
        )
        with self._engine.connect() as conn:
            return [Event.model_validate(body) for body in conn.execute(query).scalars()]

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        """Every event in time order, fetched `batch_size` at a time.

        Keyset paging on (ts, seq, event_id): no connection is held between batches, so a slow
        consumer never pins a transaction open. Each page starts where the last one ended
        (`_after`), so a full scan costs one pass over the order index, not one per page.
        """
        t = event_table.c
        event_id = sortable(t.event_id, self._dialect)
        after: tuple[datetime, int, str] | None = None
        while True:
            # After the first page `since` is implied by the keyset bound and left out: given two
            # lower bounds on ts, SQLite may start the index range at `since` on every page.
            start = since if after is None else None
            query = select(t.ts, t.seq, t.event_id, t.body).where(*_window(t.ts, start, until))
            if after is not None:
                query = query.where(_after(t.ts, t.seq, event_id, after))
            query = query.order_by(*self._ordered()).limit(self._batch)
            with self._engine.connect() as conn:
                rows = conn.execute(query).all()
            for row in rows:
                yield Event.model_validate(row.body)
            if len(rows) < self._batch:
                return
            last = rows[-1]
            after = (last.ts, last.seq, last.event_id)

    def for_decision(self, decision_id: str) -> list[Event]:
        t = event_table.c
        query = select(t.body).where(t.decision_id == decision_id).order_by(*self._ordered())
        with self._engine.connect() as conn:
            return [Event.model_validate(body) for body in conn.execute(query).scalars()]

    def count(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(event_table)).scalar_one())

    def __len__(self) -> int:
        return self.count()


# --- customer state -------------------------------------------------------------------------------


class SqlStateStore:
    """One row per customer; `put` replaces. `scan` goes in customer_id order."""

    def __init__(self, engine: Engine, *, batch_size: int = DEFAULT_BATCH) -> None:
        self._engine = engine
        self._batch = batch_size
        self._dialect = engine.dialect.name

    def get(self, customer_id: str) -> CustomerState | None:
        t = customer_state_table.c
        with self._engine.connect() as conn:
            body = conn.execute(select(t.body).where(t.customer_id == customer_id)).scalar()
        return None if body is None else CustomerState.model_validate(body)

    def put(self, states: Iterable[CustomerState]) -> None:
        latest: dict[str, CustomerState] = {}
        for s in states:
            latest[s.customer_id] = s  # the last copy wins, as in the fake
        rows = [
            {
                "customer_id": s.customer_id,
                "as_of": s.as_of,
                "version": s.version,
                "body": s.model_dump(mode="json"),
            }
            for s in latest.values()
        ]
        with self._engine.begin() as conn:
            upsert(conn, customer_state_table, rows, keep=())

    def scan(self) -> Iterator[CustomerState]:
        t = customer_state_table.c
        key = sortable(t.customer_id, self._dialect)
        after: str | None = None
        while True:
            query = select(t.customer_id, t.body)
            if after is not None:
                query = query.where(key > after)
            query = query.order_by(key).limit(self._batch)
            with self._engine.connect() as conn:
                rows = conn.execute(query).all()
            for row in rows:
                yield CustomerState.model_validate(row.body)
            if len(rows) < self._batch:
                return
            after = rows[-1].customer_id

    def count(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(customer_state_table)).scalar_one())


# --- decisions ------------------------------------------------------------------------------------


class SqlDecisionLog:
    """Every decision with its probability. Recording one again replaces it in place."""

    def __init__(self, engine: Engine, *, batch_size: int = DEFAULT_BATCH) -> None:
        self._engine = engine
        self._batch = batch_size
        self._dialect = engine.dialect.name

    def record(self, decisions: Iterable[Decision]) -> None:
        latest: dict[str, Decision] = {}
        for d in decisions:
            latest[d.decision_id] = d  # last copy wins, first position kept, as in a dict
        if not latest:
            return
        with self._engine.begin() as conn:
            base = next_seq(conn, decision_table)
            rows = [
                {
                    "decision_id": d.decision_id,
                    "customer_id": d.customer_id,
                    "made_at": d.made_at,
                    "kind": d.kind.value,
                    "goal_id": d.action.goal_id if d.action is not None else None,
                    "probability": d.probability,
                    "seq": base + i,
                    "body": d.model_dump(mode="json"),
                }
                for i, d in enumerate(latest.values())
            ]
            upsert(conn, decision_table, rows)

    def get(self, decision_id: str) -> Decision | None:
        t = decision_table.c
        with self._engine.connect() as conn:
            body = conn.execute(select(t.body).where(t.decision_id == decision_id)).scalar()
        return None if body is None else Decision.model_validate(body)

    def _ordered(self) -> list[ColumnElement[Any]]:
        t = decision_table.c
        return [t.made_at, t.seq, sortable(t.decision_id, self._dialect)]

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Decision]:
        t = decision_table.c
        decision_id = sortable(t.decision_id, self._dialect)
        after: tuple[datetime, int, str] | None = None
        while True:
            start = since if after is None else None  # implied by the keyset bound (see SqlEventStore)
            query = select(t.made_at, t.seq, t.decision_id, t.body).where(*_window(t.made_at, start, until))
            if after is not None:
                query = query.where(_after(t.made_at, t.seq, decision_id, after))
            query = query.order_by(*self._ordered()).limit(self._batch)
            with self._engine.connect() as conn:
                rows = conn.execute(query).all()
            for row in rows:
                yield Decision.model_validate(row.body)
            if len(rows) < self._batch:
                return
            last = rows[-1]
            after = (last.made_at, last.seq, last.decision_id)

    def for_customer(self, customer_id: str) -> list[Decision]:
        t = decision_table.c
        query = select(t.body).where(t.customer_id == customer_id).order_by(*self._ordered())
        with self._engine.connect() as conn:
            return [Decision.model_validate(body) for body in conn.execute(query).scalars()]


# --- workspace ------------------------------------------------------------------------------------


class SqlWorkspace:
    """Goals, briefs (every version kept; reads return the latest) and the content library."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put_goal(self, goal: Goal) -> None:
        with self._engine.begin() as conn:
            row = {
                "goal_id": goal.goal_id,
                "seq": next_seq(conn, goal_table),
                "body": goal.model_dump(mode="json"),
            }
            upsert(conn, goal_table, [row])

    def goals(self) -> list[Goal]:
        with self._engine.connect() as conn:
            bodies = conn.execute(select(goal_table.c.body).order_by(goal_table.c.seq)).scalars().all()
        return [Goal.model_validate(b) for b in bodies]

    def put_brief(self, brief: Brief) -> None:
        """Store this version. A re-put of the same (goal, version) replaces it, as in the fake."""
        with self._engine.begin() as conn:
            row = {
                "goal_id": brief.goal_id,
                "version": brief.version,
                "brief_id": brief.brief_id,
                "seq": next_seq(conn, brief_table),
                "body": brief.model_dump(mode="json"),
            }
            upsert(conn, brief_table, [row])

    def briefs(self, goal_id: str | None = None) -> list[Brief]:
        """The latest version per goal, goals in the order their first brief arrived.

        Every version stays in the table (an older version put late is stored but never wins), so
        the pick happens here. There are a handful of briefs per goal, so Python does it.
        """
        t = brief_table.c
        query = select(t.goal_id, t.version, t.seq, t.body).order_by(t.seq)
        if goal_id is not None:
            query = query.where(t.goal_id == goal_id)
        with self._engine.connect() as conn:
            rows = conn.execute(query).all()
        first_seen: dict[str, int] = {}
        best: dict[str, tuple[int, Any]] = {}
        for row in rows:
            first_seen.setdefault(row.goal_id, row.seq)
            if row.goal_id not in best or row.version > best[row.goal_id][0]:
                best[row.goal_id] = (row.version, row.body)
        order = sorted(first_seen, key=first_seen.__getitem__)
        return [Brief.model_validate(best[g][1]) for g in order]

    def put_content(self, items: Iterable[ContentItem]) -> None:
        latest: dict[str, ContentItem] = {}
        for item in items:
            latest[item.content_id] = item
        if not latest:
            return
        with self._engine.begin() as conn:
            base = next_seq(conn, content_item_table)
            rows = [
                {
                    "content_id": c.content_id,
                    "goal_id": c.goal_id,
                    "status": c.status.value,
                    "seq": base + i,
                    "body": c.model_dump(mode="json"),
                }
                for i, c in enumerate(latest.values())
            ]
            upsert(conn, content_item_table, rows)

    def content(self, *, goal_id: str | None = None, approved_only: bool = True) -> list[ContentItem]:
        """Items for this goal plus the shared ones (no goal), in the order they were first added."""
        t = content_item_table.c
        query = select(t.body).order_by(t.seq)
        if goal_id is not None:
            query = query.where(or_(t.goal_id == goal_id, t.goal_id.is_(None)))
        if approved_only:
            query = query.where(t.status == ContentStatus.APPROVED.value)
        with self._engine.connect() as conn:
            bodies = conn.execute(query).scalars().all()
        return [ContentItem.model_validate(b) for b in bodies]
