"""Where the runtime keeps the alerts and briefings it produces, so every process can read them.

A worker ticks in its own process; the API answers in another. Before these stores, alerts and
briefings lived only in the `TickReport` of the process that made them, so `/v2/alerts` and
`/v2/briefing` never saw what a worker tick said. Now the runtime writes every alert and briefing
it produces to an `AlertStore` and a `BriefingStore`, and the API reads them from there.

Both stores are append-only and keyed by the contract's id: alert and briefing ids are stable
(derived from what they are about), so a tick repeated at the same moment writes nothing new.

Two implementations share each protocol: in memory for tests and the demo, and SQL (SQLAlchemy
Core, Postgres or SQLite) for production. The SQL tables have their own `MetaData`, like the batch
table, so `autonomy.store.create_all` does not create them; `create_record_tables` does.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from sqlalchemy import JSON, Column, Index, MetaData, String, Table, select
from sqlalchemy.engine import Engine

from autonomy.contracts import Alert, Briefing
from autonomy.store.schema import UtcDateTime
from autonomy.store.sql import insert_ignore, sortable

__all__ = [
    "AlertStore",
    "BriefingStore",
    "InMemoryAlertStore",
    "InMemoryBriefingStore",
    "SqlAlertStore",
    "SqlBriefingStore",
    "alert_table",
    "briefing_table",
    "create_record_tables",
    "record_metadata",
]

Period = Literal["daily", "weekly"]


@runtime_checkable
class AlertStore(Protocol):
    def put(self, alerts: Iterable[Alert]) -> int:
        """Store alerts; return how many were new. A repeated `alert_id` is ignored."""
        ...

    def list_alerts(
        self, *, goal_id: str | None = None, since: datetime | None = None, limit: int | None = None
    ) -> list[Alert]:
        """Newest first (by `at`, then id). `goal_id` keeps that goal's alerts plus account-wide ones
        (no goal); `since` keeps alerts at or after it; `limit` caps how many come back."""
        ...


@runtime_checkable
class BriefingStore(Protocol):
    def put(self, briefings: Iterable[Briefing]) -> int:
        """Store briefings; return how many were new. A repeated `briefing_id` is ignored."""
        ...

    def latest(self, goal_id: str | None, *, period: Period | None = None) -> Briefing | None:
        """The newest briefing for exactly this goal (None: the programme-wide one)."""
        ...

    def list_briefings(self, *, limit: int | None = None) -> list[Briefing]:
        """Every briefing, newest first (by `as_of`, then id)."""
        ...


def _alert_order(a: Alert) -> tuple[datetime, str]:
    return (a.at, a.alert_id)


def _briefing_order(b: Briefing) -> tuple[datetime, str]:
    return (b.as_of, b.briefing_id)


def _check_limit(limit: int | None) -> None:
    if limit is not None and limit < 0:
        raise ValueError("limit cannot be negative")


def _keep_alert(a: Alert, goal_id: str | None, since: datetime | None) -> bool:
    if goal_id is not None and a.goal_id not in (goal_id, None):
        return False
    return since is None or a.at >= since


# --- in memory --------------------------------------------------------------------------------------


class InMemoryAlertStore:
    def __init__(self, alerts: Iterable[Alert] = ()) -> None:
        self._alerts: dict[str, Alert] = {}
        self.put(alerts)

    def put(self, alerts: Iterable[Alert]) -> int:
        new = 0
        for a in alerts:
            if a.alert_id not in self._alerts:
                self._alerts[a.alert_id] = a
                new += 1
        return new

    def list_alerts(
        self, *, goal_id: str | None = None, since: datetime | None = None, limit: int | None = None
    ) -> builtins.list[Alert]:
        _check_limit(limit)
        rows = sorted(
            (a for a in self._alerts.values() if _keep_alert(a, goal_id, since)),
            key=_alert_order,
            reverse=True,
        )
        return rows if limit is None else rows[:limit]

    def list(
        self, *, goal_id: str | None = None, since: datetime | None = None, limit: int | None = None
    ) -> builtins.list[Alert]:
        """The same as `list_alerts`, for readers that look for a store's generic `list`."""
        return self.list_alerts(goal_id=goal_id, since=since, limit=limit)


class InMemoryBriefingStore:
    def __init__(self, briefings: Iterable[Briefing] = ()) -> None:
        self._briefings: dict[str, Briefing] = {}
        self.put(briefings)

    def put(self, briefings: Iterable[Briefing]) -> int:
        new = 0
        for b in briefings:
            if b.briefing_id not in self._briefings:
                self._briefings[b.briefing_id] = b
                new += 1
        return new

    def latest(self, goal_id: str | None, *, period: Period | None = None) -> Briefing | None:
        mine = [
            b
            for b in self._briefings.values()
            if b.goal_id == goal_id and (period is None or b.period == period)
        ]
        return max(mine, key=_briefing_order) if mine else None

    def list_briefings(self, *, limit: int | None = None) -> list[Briefing]:
        _check_limit(limit)
        rows = sorted(self._briefings.values(), key=_briefing_order, reverse=True)
        return rows if limit is None else rows[:limit]


# --- SQL --------------------------------------------------------------------------------------------

record_metadata = MetaData()
"""Its own metadata: `autonomy.store.create_all` does not create these tables."""

alert_table = Table(
    "runtime_alert",
    record_metadata,
    Column("alert_id", String(200), primary_key=True),
    Column("at", UtcDateTime(), nullable=False),
    Column("goal_id", String(200), nullable=True),
    Column("code", String(100), nullable=False),
    Column("body", JSON, nullable=False, doc="The whole alert"),
    Index("ix_runtime_alert_at", "at"),
    Index("ix_runtime_alert_goal", "goal_id"),
)

briefing_table = Table(
    "runtime_briefing",
    record_metadata,
    Column("briefing_id", String(200), primary_key=True),
    Column("as_of", UtcDateTime(), nullable=False),
    Column("goal_id", String(200), nullable=True),
    Column("period", String(20), nullable=False),
    Column("body", JSON, nullable=False, doc="The whole briefing"),
    Index("ix_runtime_briefing_goal", "goal_id", "as_of"),
)


def create_record_tables(engine: Engine) -> None:
    """Create the alert and briefing tables if they do not exist. Safe to call twice."""
    record_metadata.create_all(engine)


class SqlAlertStore:
    """An `AlertStore` on Postgres or SQLite."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, alerts: Iterable[Alert]) -> int:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for a in alerts:
            if a.alert_id in seen:
                continue
            seen.add(a.alert_id)
            rows.append(
                {
                    "alert_id": a.alert_id,
                    "at": a.at,
                    "goal_id": a.goal_id,
                    "code": a.code,
                    "body": a.model_dump(mode="json"),
                }
            )
        if not rows:
            return 0
        with self._engine.begin() as conn:
            return insert_ignore(conn, alert_table, rows)

    def list_alerts(
        self, *, goal_id: str | None = None, since: datetime | None = None, limit: int | None = None
    ) -> builtins.list[Alert]:
        _check_limit(limit)
        t = alert_table.c
        # Ordered and limited in SQL, so a read costs `limit` rows, not the whole history. The id
        # is compared by code point on every database, so ties order as they do in memory.
        query = select(t.body).order_by(t.at.desc(), sortable(t.alert_id, self._engine.dialect.name).desc())
        if goal_id is not None:
            query = query.where((t.goal_id == goal_id) | t.goal_id.is_(None))
        if since is not None:
            query = query.where(t.at >= since)
        if limit is not None:
            query = query.limit(limit)
        with self._engine.connect() as conn:
            return [Alert.model_validate(body) for body in conn.execute(query).scalars()]

    def list(
        self, *, goal_id: str | None = None, since: datetime | None = None, limit: int | None = None
    ) -> builtins.list[Alert]:
        """The same as `list_alerts`, for readers that look for a store's generic `list`."""
        return self.list_alerts(goal_id=goal_id, since=since, limit=limit)


class SqlBriefingStore:
    """A `BriefingStore` on Postgres or SQLite."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, briefings: Iterable[Briefing]) -> int:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for b in briefings:
            if b.briefing_id in seen:
                continue
            seen.add(b.briefing_id)
            rows.append(
                {
                    "briefing_id": b.briefing_id,
                    "as_of": b.as_of,
                    "goal_id": b.goal_id,
                    "period": b.period,
                    "body": b.model_dump(mode="json"),
                }
            )
        if not rows:
            return 0
        with self._engine.begin() as conn:
            return insert_ignore(conn, briefing_table, rows)

    def latest(self, goal_id: str | None, *, period: Period | None = None) -> Briefing | None:
        t = briefing_table.c
        query = self._newest_first().where(t.goal_id.is_(None) if goal_id is None else t.goal_id == goal_id)
        if period is not None:
            query = query.where(t.period == period)
        rows = self._fetch(query.limit(1))
        return rows[0] if rows else None

    def list_briefings(self, *, limit: int | None = None) -> list[Briefing]:
        _check_limit(limit)
        query = self._newest_first()
        return self._fetch(query if limit is None else query.limit(limit))

    def _newest_first(self) -> Any:
        """Every briefing body, newest first, ties by id in code-point order (as in memory)."""
        t = briefing_table.c
        return select(t.body).order_by(
            t.as_of.desc(), sortable(t.briefing_id, self._engine.dialect.name).desc()
        )

    def _fetch(self, query: Any) -> list[Briefing]:
        with self._engine.connect() as conn:
            return [Briefing.model_validate(body) for body in conn.execute(query).scalars()]
