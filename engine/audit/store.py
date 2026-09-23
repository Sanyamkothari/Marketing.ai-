"""Where audit events are kept: the append-only `audit_events` table (Phase 4b M47).

`SqlAuditLog` implements `engine.audit.events.AuditLog` on the platform database - `platform.db`
beside the artefacts locally, the registry's Postgres on a deployment (DEC-706). Three properties
are the reason this module exists rather than a log file:

**Append-only, enforced below the application** (DEC-714). The protocol has no update or delete, and
this class adds none; that stops a careless caller, not a determined one. So the table also refuses
`UPDATE` and `DELETE` in the database itself: SQLite triggers created here beside the table, and on
Postgres the trigger function Alembic's `0002_access_audit` installs. Changing history then needs
somebody to drop a trigger first - a deliberate, visible act rather than an accident of code.

**Queryable** by the audit viewer: actor, exact action or an action *family* (`models.` matches
`models.approve` and `models.promote`), object, outcome and a time window, newest first, with a
total count for paging.

**Nothing in it is a data value** (DEC-705). The columns are identifiers, hashes and short tokens;
`details` is the closed `DETAIL_KEYS` mapping, stored as JSON.

`record_event` is the one way to write an event that is not tied to an API request - a login
failure, the retention job, a scheduler firing, the bootstrap script. Everything an API request does
is written once, by the audit middleware in `api/access.py`, never by the route itself.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import Column, DateTime, Text, func, text
from sqlalchemy.engine import Engine
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select
from sqlmodel.sql.expression import SelectOfScalar

from engine.access.roles import Principal
from engine.audit.events import AuditEvent, AuditLog, AuditQuery
from engine.platform_db import create_tables
from engine.utils.time import utc_now

__all__ = [
    "ANONYMOUS_ACTOR_ID",
    "ANONYMOUS_ACTOR_KIND",
    "AUDIT_TABLE",
    "AuditEventRow",
    "MemoryAuditLog",
    "SqlAuditLog",
    "new_event_id",
    "record_event",
]

AUDIT_TABLE: Final[str] = "audit_events"

ANONYMOUS_ACTOR_ID: Final[str] = "anonymous"
"""The actor of a request that proved no identity: a refused request, a failed sign-in."""

ANONYMOUS_ACTOR_KIND: Final[str] = "anonymous"

_SQLITE_GUARDS: Final[tuple[str, ...]] = (
    f"CREATE TRIGGER IF NOT EXISTS {AUDIT_TABLE}_no_update BEFORE UPDATE ON {AUDIT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{AUDIT_TABLE} is append-only'); END",
    f"CREATE TRIGGER IF NOT EXISTS {AUDIT_TABLE}_no_delete BEFORE DELETE ON {AUDIT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{AUDIT_TABLE} is append-only'); END",
)
"""SQLite's half of DEC-714. Postgres's half is in `alembic/versions/0002_access_audit.py`."""


class AuditEventRow(SQLModel, table=True):
    """One row per `AuditEvent`. Indexed on what the viewer filters by."""

    __tablename__ = AUDIT_TABLE

    event_id: str = SQLField(primary_key=True)
    occurred_at: datetime = SQLField(
        sa_column=Column("occurred_at", DateTime(timezone=True), nullable=False, index=True)
    )
    actor_id: str = SQLField(index=True)
    actor_kind: str
    action: str = SQLField(index=True)
    object_type: str | None = None
    object_id: str | None = SQLField(default=None, index=True)
    before_hash: str | None = None
    after_hash: str | None = None
    request_id: str | None = SQLField(default=None, index=True)
    outcome: str
    details_json: str = SQLField(sa_column=Column("details_json", Text(), nullable=False))


def new_event_id() -> str:
    """A fresh, unguessable event id."""
    return uuid.uuid4().hex


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _row(event: AuditEvent) -> AuditEventRow:
    return AuditEventRow(
        event_id=event.event_id,
        occurred_at=_utc(event.occurred_at),
        actor_id=event.actor_id,
        actor_kind=event.actor_kind,
        action=event.action,
        object_type=event.object_type,
        object_id=event.object_id,
        before_hash=event.before_hash,
        after_hash=event.after_hash,
        request_id=event.request_id,
        outcome=event.outcome,
        details_json=json.dumps(event.details, sort_keys=True, separators=(",", ":")),
    )


def _event(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        event_id=row.event_id,
        occurred_at=_utc(row.occurred_at),
        actor_id=row.actor_id,
        actor_kind=row.actor_kind,
        action=row.action,
        object_type=row.object_type,
        object_id=row.object_id,
        before_hash=row.before_hash,
        after_hash=row.after_hash,
        request_id=row.request_id,
        outcome=row.outcome,
        details=json.loads(row.details_json),
    )


def _action_matches(pattern: str | None, action: str) -> bool:
    """`pattern` is an exact action, or a family when it ends in `.`; None matches everything."""
    if pattern is None:
        return True
    return action.startswith(pattern) if pattern.endswith(".") else action == pattern


def _filtered(statement: SelectOfScalar[AuditEventRow], query: AuditQuery) -> SelectOfScalar[AuditEventRow]:
    if query.actor_id is not None:
        statement = statement.where(col(AuditEventRow.actor_id) == query.actor_id)
    if query.action is not None:
        if query.action.endswith("."):
            # A family: escape LIKE's wildcards so `runs_` cannot match `runsX`.
            escaped = query.action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(col(AuditEventRow.action).like(f"{escaped}%", escape="\\"))
        else:
            statement = statement.where(col(AuditEventRow.action) == query.action)
    if query.object_type is not None:
        statement = statement.where(col(AuditEventRow.object_type) == query.object_type)
    if query.object_id is not None:
        statement = statement.where(col(AuditEventRow.object_id) == query.object_id)
    if query.outcome is not None:
        statement = statement.where(col(AuditEventRow.outcome) == query.outcome)
    if query.since is not None:
        statement = statement.where(col(AuditEventRow.occurred_at) >= _utc(query.since))
    if query.until is not None:
        statement = statement.where(col(AuditEventRow.occurred_at) < _utc(query.until))
    return statement


class SqlAuditLog:
    """`AuditLog` on the platform database. `append`, `query`, `count`; never an update or a delete."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        create_tables(engine, (AUDIT_TABLE,))
        if engine.dialect.name == "sqlite":
            with engine.begin() as connection:
                for statement in _SQLITE_GUARDS:
                    connection.execute(text(statement))

    def append(self, event: AuditEvent) -> None:
        """Write one event. An event id that already exists is an error, never an overwrite."""
        with Session(self._engine) as session:
            session.add(_row(event))
            session.commit()

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        """Events matching `query`, newest first, `query.limit` from `query.offset`."""
        statement = _filtered(select(AuditEventRow), query)
        statement = (
            statement.order_by(col(AuditEventRow.occurred_at).desc(), col(AuditEventRow.event_id).desc())
            .offset(query.offset)
            .limit(query.limit)
        )
        with Session(self._engine) as session:
            return tuple(_event(row) for row in session.exec(statement).all())

    def count(self, query: AuditQuery) -> int:
        """How many events match `query`, ignoring its limit and offset."""
        inner = _filtered(select(AuditEventRow), query).subquery()
        with Session(self._engine) as session:
            return int(session.exec(select(func.count()).select_from(inner)).one())


class MemoryAuditLog:
    """`AuditLog` in a list: for tests of code that writes events, and for a process with no database."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> None:
        """Keep one event; a duplicate id is refused as the table would refuse it."""
        with self._lock:
            if any(existing.event_id == event.event_id for existing in self._events):
                raise ValueError("an audit event with this id already exists")
            self._events.append(event)

    def _matching(self, query: AuditQuery) -> list[AuditEvent]:
        found = [
            event
            for event in self._events
            if (query.actor_id is None or event.actor_id == query.actor_id)
            and _action_matches(query.action, event.action)
            and (query.object_type is None or event.object_type == query.object_type)
            and (query.object_id is None or event.object_id == query.object_id)
            and (query.outcome is None or event.outcome == query.outcome)
            and (query.since is None or event.occurred_at >= _utc(query.since))
            and (query.until is None or event.occurred_at < _utc(query.until))
        ]
        return sorted(found, key=lambda event: (event.occurred_at, event.event_id), reverse=True)

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        """Matching events, newest first."""
        with self._lock:
            return tuple(self._matching(query)[query.offset : query.offset + query.limit])

    def count(self, query: AuditQuery) -> int:
        """How many events match."""
        with self._lock:
            return len(self._matching(query))


def record_event(
    audit_log: AuditLog,
    principal: Principal | None,
    action: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    outcome: str = "success",
    before_hash: str | None = None,
    after_hash: str | None = None,
    details: Mapping[str, str | int | float | bool | None] | None = None,
    request_id: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> AuditEvent:
    """Build one `AuditEvent` and append it; returns what was written.

    For events that are not an API request: the retention job, a scheduler firing, the bootstrap
    script. `principal=None` records the anonymous actor. `details` is checked against
    `DETAIL_KEYS` by `AuditEvent` itself, so an unexpected key is an error here and not a leak later.
    An API route never calls this for its own request: the middleware writes that event, and a route
    enriches it with `api.access.set_audit_context` (exactly one event per request).
    """
    event = AuditEvent(
        event_id=new_event_id(),
        occurred_at=clock(),
        actor_id=principal.user_id if principal is not None else ANONYMOUS_ACTOR_ID,
        actor_kind=principal.kind if principal is not None else ANONYMOUS_ACTOR_KIND,
        action=action,
        object_type=object_type,
        object_id=object_id,
        before_hash=before_hash,
        after_hash=after_hash,
        request_id=request_id,
        outcome=outcome,
        details=dict(details or {}),
    )
    audit_log.append(event)
    return event
