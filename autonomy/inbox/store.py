"""Where inbox items live, so the worker that syncs them and the API that shows them agree.

Two tables, in their own `MetaData` (like the runtime's alert table; `create_inbox_tables` makes
them): `inbox_item` holds the latest version of every item (one row per item id), and
`inbox_appearance` an append-only line each time an item opens, reopens or resolves. The second is
what "items a person saw this week" is counted from (`Inbox.appearances`).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from sqlalchemy import JSON, Column, Index, Integer, MetaData, String, Table, select
from sqlalchemy.engine import Engine

from autonomy.contracts import stable_id
from autonomy.inbox.items import InboxItem
from autonomy.store.schema import UtcDateTime
from autonomy.store.sql import insert_ignore, upsert

__all__ = [
    "Appearance",
    "InMemoryInboxStore",
    "InboxStore",
    "SqlInboxStore",
    "create_inbox_tables",
    "inbox_metadata",
]

What = Literal["opened", "reopened", "resolved"]


@dataclass(frozen=True)
class Appearance:
    item_id: str
    business_id: str
    cause: str
    kind: str
    at: datetime
    what: What
    by: str = ""


@runtime_checkable
class InboxStore(Protocol):
    def put(self, item: InboxItem) -> None:
        """Insert or replace the item (by `item_id`)."""
        ...

    def get(self, item_id: str) -> InboxItem | None: ...

    def latest(self, business_id: str, cause: str) -> InboxItem | None:
        """The newest item for this cause (highest episode), open or resolved."""
        ...

    def items(self, business_id: str, *, open_only: bool = False) -> list[InboxItem]:
        """Newest first (by `opened_at`, then id)."""
        ...

    def record(self, appearance: Appearance) -> None:
        """Append one open / reopen / resolve line (a repeat of the same line is ignored)."""
        ...

    def appearances(self, business_id: str, *, since: datetime, until: datetime) -> list[Appearance]:
        """The lines in `[since, until)`, oldest first."""
        ...


def _order(item: InboxItem) -> tuple[datetime, str]:
    return (item.opened_at, item.item_id)


class InMemoryInboxStore:
    def __init__(self) -> None:
        self._items: dict[str, InboxItem] = {}
        self._lines: dict[str, Appearance] = {}
        self._lock = threading.Lock()

    def put(self, item: InboxItem) -> None:
        with self._lock:
            self._items[item.item_id] = item

    def get(self, item_id: str) -> InboxItem | None:
        return self._items.get(item_id)

    def latest(self, business_id: str, cause: str) -> InboxItem | None:
        mine = [i for i in self._items.values() if i.business_id == business_id and i.cause == cause]
        return max(mine, key=lambda i: (i.episode, i.opened_at)) if mine else None

    def items(self, business_id: str, *, open_only: bool = False) -> list[InboxItem]:
        rows = [i for i in self._items.values() if i.business_id == business_id and (i.open or not open_only)]
        return sorted(rows, key=_order, reverse=True)

    def record(self, appearance: Appearance) -> None:
        with self._lock:
            self._lines.setdefault(_line_id(appearance), appearance)

    def appearances(self, business_id: str, *, since: datetime, until: datetime) -> list[Appearance]:
        rows = [a for a in self._lines.values() if a.business_id == business_id and since <= a.at < until]
        return sorted(rows, key=lambda a: (a.at, a.item_id, a.what))


def _line_id(a: Appearance) -> str:
    return stable_id("inboxline", a.item_id, a.what, a.at.isoformat())


# --- SQL ------------------------------------------------------------------------------------------

inbox_metadata = MetaData()

item_table = Table(
    "inbox_item",
    inbox_metadata,
    Column("item_id", String(200), primary_key=True),
    Column("business_id", String(200), nullable=False),
    Column("cause", String(400), nullable=False),
    Column("episode", Integer, nullable=False),
    Column("opened_at", UtcDateTime(), nullable=False),
    Column("open", Integer, nullable=False, doc="1 while unresolved"),
    Column("body", JSON, nullable=False, doc="The whole item"),
    Index("ix_inbox_item_cause", "business_id", "cause", "episode"),
    Index("ix_inbox_item_open", "business_id", "open"),
)

appearance_table = Table(
    "inbox_appearance",
    inbox_metadata,
    Column("line_id", String(200), primary_key=True),
    Column("item_id", String(200), nullable=False),
    Column("business_id", String(200), nullable=False),
    Column("cause", String(400), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("at", UtcDateTime(), nullable=False),
    Column("what", String(20), nullable=False),
    Column("by", String(200), nullable=False),
    Index("ix_inbox_appearance_at", "business_id", "at"),
)


def create_inbox_tables(engine: Engine) -> None:
    """Create the inbox tables if they do not exist. Safe to call twice."""
    inbox_metadata.create_all(engine)


class SqlInboxStore:
    """An `InboxStore` on Postgres or SQLite."""

    def __init__(self, engine: Engine, *, create: bool = True) -> None:
        self._engine = engine
        if create:
            create_inbox_tables(engine)

    def put(self, item: InboxItem) -> None:
        row: dict[str, Any] = {
            "item_id": item.item_id,
            "business_id": item.business_id,
            "cause": item.cause,
            "episode": item.episode,
            "opened_at": item.opened_at,
            "open": 1 if item.open else 0,
            "body": item.model_dump(mode="json"),
        }
        with self._engine.begin() as conn:
            upsert(conn, item_table, [row], keep=())

    def get(self, item_id: str) -> InboxItem | None:
        t = item_table.c
        with self._engine.connect() as conn:
            body = conn.execute(select(t.body).where(t.item_id == item_id)).scalar()
        return None if body is None else InboxItem.model_validate(body)

    def latest(self, business_id: str, cause: str) -> InboxItem | None:
        t = item_table.c
        query = (
            select(t.body)
            .where(t.business_id == business_id, t.cause == cause)
            .order_by(t.episode.desc(), t.opened_at.desc())
            .limit(1)
        )
        with self._engine.connect() as conn:
            body = conn.execute(query).scalar()
        return None if body is None else InboxItem.model_validate(body)

    def items(self, business_id: str, *, open_only: bool = False) -> list[InboxItem]:
        t = item_table.c
        query = select(t.body).where(t.business_id == business_id)
        if open_only:
            query = query.where(t.open == 1)
        with self._engine.connect() as conn:
            rows = [InboxItem.model_validate(b) for b in conn.execute(query).scalars()]
        return sorted(rows, key=_order, reverse=True)

    def record(self, appearance: Appearance) -> None:
        row = {
            "line_id": _line_id(appearance),
            "item_id": appearance.item_id,
            "business_id": appearance.business_id,
            "cause": appearance.cause,
            "kind": appearance.kind,
            "at": appearance.at,
            "what": appearance.what,
            "by": appearance.by,
        }
        with self._engine.begin() as conn:
            insert_ignore(conn, appearance_table, [row])

    def appearances(self, business_id: str, *, since: datetime, until: datetime) -> list[Appearance]:
        t = appearance_table.c
        query = (
            select(t.item_id, t.business_id, t.cause, t.kind, t.at, t.what, t.by)
            .where(t.business_id == business_id, t.at >= since, t.at < until)
            .order_by(t.at, t.item_id, t.what)
        )
        with self._engine.connect() as conn:
            return [
                Appearance(
                    item_id=r.item_id,
                    business_id=r.business_id,
                    cause=r.cause,
                    kind=r.kind,
                    at=r.at,
                    what=_what(r.what),
                    by=r.by,
                )
                for r in conn.execute(query)
            ]


def _what(raw: str) -> What:
    if raw == "opened":
        return "opened"
    if raw == "reopened":
        return "reopened"
    return "resolved"
