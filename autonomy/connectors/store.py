"""Where the connectors keep their state: SQL tables, Postgres in production, SQLite in tests.

Same patterns as `autonomy.store` (SQLAlchemy Core, a JSON `body` holding the whole model, only
the queried columns lifted out, `insert_ignore`/`upsert` for idempotent writes) and the same
`UtcDateTime` column type. The tables have their own `MetaData`, like `autonomy.runtime.stores`,
so `autonomy.store.create_all` does not create them: `create_connector_tables(engine)` does.

| Table | One row per | Why |
|---|---|---|
| `connector_connection` | connection | status (`needs_reconnect` …), lookup by platform account |
| `connector_position` | (connection, stream, lane) | the resumable sync position; advanced only after the batch it covers is stored |
| `connector_run` | sync run | rows per run (row-count drop alarm), error streaks, freshness |
| `connector_lease` | named lock | one token refresher and one syncer per stream, across workers |
| `connector_delivery` | webhook delivery | at-least-once deliveries are processed once |
| `connector_record` | (connection, stream, record) | the newest `updated_at` seen: an older copy arriving late is skipped |
| `connector_oauth_state` | pending connect | single-use OAuth `state` with its PKCE verifier |
| `connector_incident` | (connection, problem) | one alert per real problem, not one per check |
| `connector_secret` | connection | the token set, encrypted (`tokens.SqlTokenVault`) |
| `connector_privacy` | privacy request | GDPR/CCPA webhooks recorded for the data layer to act on |
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    and_,
    delete,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine

from autonomy.connectors.base import Connection, ConnectionStatus, Lane, PrivacyKind, RecordKey, Stream
from autonomy.connectors.oauth import PendingAuthorization
from autonomy.store.schema import UtcDateTime
from autonomy.store.sql import insert_ignore, sortable, upsert

__all__ = [
    "ConnectorStore",
    "Incident",
    "PrivacyRequest",
    "SyncPosition",
    "SyncRun",
    "connector_metadata",
    "create_connector_tables",
]

connector_metadata = MetaData()

connection_table = Table(
    "connector_connection",
    connector_metadata,
    Column("connection_id", String(100), primary_key=True),
    Column("kind", String(60), nullable=False),
    Column("business_id", String(200), nullable=False),
    Column("external_id", String(300), nullable=True),
    Column("status", String(30), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    Column("body", JSON, nullable=False),
    Index("ix_connector_connection_account", "kind", "external_id"),
    Index("ix_connector_connection_business", "business_id"),
)

position_table = Table(
    "connector_position",
    connector_metadata,
    Column("connection_id", String(100), nullable=False),
    Column("stream", String(40), nullable=False),
    Column("lane", String(20), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    Column("body", JSON, nullable=False),
    PrimaryKeyConstraint("connection_id", "stream", "lane", name="pk_connector_position"),
)

run_table = Table(
    "connector_run",
    connector_metadata,
    Column("run_id", String(100), primary_key=True),
    Column("connection_id", String(100), nullable=False),
    Column("stream", String(40), nullable=False),
    Column("lane", String(20), nullable=False),
    Column("finished_at", UtcDateTime(), nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("rows", Integer, nullable=False),
    Column("body", JSON, nullable=False),
    Index("ix_connector_run_stream", "connection_id", "stream", "lane", "finished_at"),
)

lease_table = Table(
    "connector_lease",
    connector_metadata,
    Column("name", String(300), primary_key=True),
    Column("holder", String(200), nullable=False),
    Column("expires_at", UtcDateTime(), nullable=False),
)

delivery_table = Table(
    "connector_delivery",
    connector_metadata,
    Column("account", String(300), nullable=False),
    Column("delivery_id", String(300), nullable=False),
    Column("received_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("account", "delivery_id", name="pk_connector_delivery"),
    Index("ix_connector_delivery_received", "received_at"),
)

record_table = Table(
    "connector_record",
    connector_metadata,
    Column("connection_id", String(100), nullable=False),
    Column("stream", String(40), nullable=False),
    Column("record_id", String(300), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=True),
    PrimaryKeyConstraint("connection_id", "stream", "record_id", name="pk_connector_record"),
)

oauth_state_table = Table(
    "connector_oauth_state",
    connector_metadata,
    Column("state", String(200), primary_key=True),
    Column("expires_at", UtcDateTime(), nullable=False),
    Column("body", JSON, nullable=False),
)

incident_table = Table(
    "connector_incident",
    connector_metadata,
    Column("connection_id", String(100), nullable=False),
    Column("code", String(100), nullable=False),
    Column("open", Boolean, nullable=False),
    Column("opened_at", UtcDateTime(), nullable=False),
    Column("body", JSON, nullable=False),
    PrimaryKeyConstraint("connection_id", "code", name="pk_connector_incident"),
)

secret_table = Table(
    "connector_secret",
    connector_metadata,
    Column("connection_id", String(100), primary_key=True),
    Column("token", Text, nullable=False),
    Column("key_fingerprint", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
)

privacy_table = Table(
    "connector_privacy",
    connector_metadata,
    Column("request_id", String(200), primary_key=True),
    Column("connection_id", String(100), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("received_at", UtcDateTime(), nullable=False),
    Column("status", String(20), nullable=False),
    Column("body", JSON, nullable=False),
)


def create_connector_tables(engine: Engine) -> None:
    """Create every connector table that does not exist yet. Safe to call twice."""
    connector_metadata.create_all(engine)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SyncPosition(_Model):
    """Where one lane of one stream has got to. Saved after every stored page, so a crash resumes."""

    connection_id: str
    stream: Stream
    lane: Lane
    window_start: datetime | None = Field(default=None, description="The window being read now")
    window_end: datetime | None = None
    cursor: str | None = Field(default=None, description="The page cursor inside that window")
    high_watermark: datetime | None = Field(
        default=None, description="Live lane: everything changed before this has been read"
    )
    reached: datetime | None = Field(
        default=None, description="History lane: everything from here to the anchor has been read"
    )
    done: bool = Field(default=False, description="History lane: the backfill depth was reached")
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_rows: int = 0
    error_streak: int = 0
    last_error_code: str | None = None
    last_error: str | None = None
    waiting_until: datetime | None = Field(
        default=None, description="An async export (bulk operation) is running; come back then"
    )

    @property
    def in_window(self) -> bool:
        return self.window_end is not None


class SyncRun(_Model):
    run_id: str
    connection_id: str
    stream: Stream
    lane: Lane
    started_at: datetime
    finished_at: datetime
    ok: bool
    complete: bool = Field(description="The window finished (False: more pages, or waiting)")
    rows: int = 0
    new_records: int = 0
    events: int = 0
    problems: int = 0
    missing_required: int = Field(default=0, description="Records lacking a required field (schema drift)")
    error_code: str | None = None
    error: str | None = None


class Incident(_Model):
    connection_id: str
    code: str
    open: bool
    severity: str
    message: str
    opened_at: datetime
    alert_id: str
    last_seen_at: datetime
    resolved_at: datetime | None = None


class PrivacyRequest(_Model):
    request_id: str
    connection_id: str
    kind: PrivacyKind
    received_at: datetime
    status: str = "pending"
    subject: dict[str, Any] = Field(default_factory=dict)


class ConnectorStore:
    """All connector state over one SQLAlchemy engine. Thread- and process-safe through the database."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._dialect = engine.dialect.name

    # -- connections ------------------------------------------------------------------------------

    def put_connection(self, connection: Connection) -> None:
        row = {
            "connection_id": connection.connection_id,
            "kind": connection.kind,
            "business_id": connection.business_id,
            "external_id": connection.external_id,
            "status": connection.status.value,
            "updated_at": connection.updated_at,
            "body": connection.model_dump(mode="json"),
        }
        with self.engine.begin() as conn:
            upsert(conn, connection_table, [row], keep=())

    def get_connection(self, connection_id: str) -> Connection | None:
        t = connection_table.c
        with self.engine.connect() as conn:
            body = conn.execute(select(t.body).where(t.connection_id == connection_id)).scalar()
        return None if body is None else Connection.model_validate(body)

    def find_connection(
        self, kind: str, external_id: str, *, include_disconnected: bool = False
    ) -> Connection | None:
        """The live connection for a platform account (the newest one, if it was connected twice).

        `include_disconnected` also finds one that was uninstalled (privacy webhooks arrive after)."""
        t = connection_table.c
        query = select(t.body).where(t.kind == kind, t.external_id == external_id)
        if not include_disconnected:
            query = query.where(t.status != ConnectionStatus.DISCONNECTED.value)
        query = query.order_by(t.updated_at.desc(), sortable(t.connection_id, self._dialect))
        with self.engine.connect() as conn:
            body = conn.execute(query).scalars().first()
        return None if body is None else Connection.model_validate(body)

    def connections(self, *, business_id: str | None = None, kind: str | None = None) -> list[Connection]:
        t = connection_table.c
        query = select(t.body).order_by(sortable(t.connection_id, self._dialect))
        if business_id is not None:
            query = query.where(t.business_id == business_id)
        if kind is not None:
            query = query.where(t.kind == kind)
        with self.engine.connect() as conn:
            return [Connection.model_validate(b) for b in conn.execute(query).scalars()]

    def set_status(
        self, connection_id: str, status: ConnectionStatus, reason: str | None, now: datetime
    ) -> Connection | None:
        current = self.get_connection(connection_id)
        if current is None:
            return None
        if current.status is status and current.status_reason == reason:
            return current
        updated = current.model_copy(update={"status": status, "status_reason": reason, "updated_at": now})
        self.put_connection(updated)
        return updated

    # -- positions --------------------------------------------------------------------------------

    def position(self, connection_id: str, stream: Stream, lane: Lane) -> SyncPosition:
        t = position_table.c
        query = select(t.body).where(
            t.connection_id == connection_id, t.stream == stream.value, t.lane == lane.value
        )
        with self.engine.connect() as conn:
            body = conn.execute(query).scalar()
        if body is None:
            return SyncPosition(connection_id=connection_id, stream=stream, lane=lane)
        return SyncPosition.model_validate(body)

    def put_position(self, position: SyncPosition, now: datetime) -> None:
        row = {
            "connection_id": position.connection_id,
            "stream": position.stream.value,
            "lane": position.lane.value,
            "updated_at": now,
            "body": position.model_dump(mode="json"),
        }
        with self.engine.begin() as conn:
            upsert(conn, position_table, [row], keep=())

    def positions(self, connection_id: str) -> list[SyncPosition]:
        t = position_table.c
        query = select(t.body).where(t.connection_id == connection_id).order_by(t.stream, t.lane)
        with self.engine.connect() as conn:
            return [SyncPosition.model_validate(b) for b in conn.execute(query).scalars()]

    # -- runs -------------------------------------------------------------------------------------

    def record_run(self, run: SyncRun) -> None:
        row = {
            "run_id": run.run_id,
            "connection_id": run.connection_id,
            "stream": run.stream.value,
            "lane": run.lane.value,
            "finished_at": run.finished_at,
            "ok": run.ok,
            "rows": run.rows,
            "body": run.model_dump(mode="json"),
        }
        with self.engine.begin() as conn:
            upsert(conn, run_table, [row], keep=())

    def recent_runs(
        self, connection_id: str, stream: Stream, lane: Lane, *, limit: int = 20
    ) -> list[SyncRun]:
        """Newest first."""
        t = run_table.c
        query = (
            select(t.body)
            .where(t.connection_id == connection_id, t.stream == stream.value, t.lane == lane.value)
            .order_by(t.finished_at.desc(), sortable(t.run_id, self._dialect).desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return [SyncRun.model_validate(b) for b in conn.execute(query).scalars()]

    def prune_runs(self, before: datetime) -> int:
        with self.engine.begin() as conn:
            return int(conn.execute(delete(run_table).where(run_table.c.finished_at < before)).rowcount)

    # -- leases -----------------------------------------------------------------------------------

    def acquire_lease(self, name: str, holder: str, now: datetime, ttl: timedelta) -> bool:
        """Take the named lock until `now + ttl`, or renew it if `holder` has it. False if someone else does.

        One conditional UPDATE (free or expired or ours), else one INSERT that ignores a conflict:
        on Postgres and SQLite exactly one of two racing workers wins."""
        t = lease_table.c
        expires = now + ttl
        with self.engine.begin() as conn:
            won = conn.execute(
                update(lease_table)
                .where(t.name == name, or_(t.expires_at <= now, t.holder == holder))
                .values(holder=holder, expires_at=expires)
            ).rowcount
            if won == 1:
                return True
            return (
                insert_ignore(conn, lease_table, [{"name": name, "holder": holder, "expires_at": expires}])
                == 1
            )

    def release_lease(self, name: str, holder: str) -> None:
        t = lease_table.c
        with self.engine.begin() as conn:
            conn.execute(delete(lease_table).where(t.name == name, t.holder == holder))

    # -- webhook deliveries -----------------------------------------------------------------------

    def delivery_seen(self, account: str, delivery_id: str) -> bool:
        t = delivery_table.c
        with self.engine.connect() as conn:
            found = conn.execute(
                select(t.delivery_id).where(t.account == account, t.delivery_id == delivery_id)
            ).first()
        return found is not None

    def remember_delivery(self, account: str, delivery_id: str, now: datetime) -> bool:
        """Record a delivery; True when it was not seen before."""
        row = {"account": account, "delivery_id": delivery_id, "received_at": now}
        with self.engine.begin() as conn:
            return insert_ignore(conn, delivery_table, [row]) == 1

    def prune_deliveries(self, before: datetime) -> int:
        """Forget deliveries older than any platform retries (Shopify ~4 h, Stripe ~3 days)."""
        with self.engine.begin() as conn:
            return int(
                conn.execute(delete(delivery_table).where(delivery_table.c.received_at < before)).rowcount
            )

    # -- record versions --------------------------------------------------------------------------

    def record_versions(
        self, connection_id: str, stream: Stream, record_ids: Sequence[str]
    ) -> dict[str, datetime | None]:
        """The newest `updated_at` stored for each of `record_ids` that has been seen."""
        t = record_table.c
        found: dict[str, datetime | None] = {}
        with self.engine.connect() as conn:
            for start in range(0, len(record_ids), 500):
                chunk = list(record_ids[start : start + 500])
                rows = conn.execute(
                    select(t.record_id, t.updated_at).where(
                        t.connection_id == connection_id, t.stream == stream.value, t.record_id.in_(chunk)
                    )
                ).all()
                found.update({str(r.record_id): r.updated_at for r in rows})
        return found

    def claim_newer(self, connection_id: str, stream: Stream, keys: Sequence[RecordKey]) -> set[str]:
        """Record the newest `updated_at` per record; return the ids whose copy here is newer than stored.

        A copy with no `updated_at`, or one never seen, always counts as new (the stable event ids
        still dedupe it). A copy older than or equal to what is stored is skipped: a webhook that
        arrives out of order never overwrites a later version. Each row is one conditional UPDATE
        or one INSERT, so a webhook and a poll racing on the same record cannot both win."""
        t = record_table.c
        newest: dict[str, RecordKey] = {}
        for key in keys:
            held = newest.get(key.record_id)
            if held is None or _later(key.updated_at, held.updated_at):
                newest[key.record_id] = key
        fresh: set[str] = set()
        with self.engine.begin() as conn:
            for key in newest.values():
                where = and_(
                    t.connection_id == connection_id, t.stream == stream.value, t.record_id == key.record_id
                )
                if key.updated_at is None:
                    fresh.add(key.record_id)
                    insert_ignore(conn, record_table, [_record_row(connection_id, stream, key)])
                    continue
                changed = conn.execute(
                    update(record_table)
                    .where(where, or_(t.updated_at.is_(None), t.updated_at < key.updated_at))
                    .values(updated_at=key.updated_at)
                ).rowcount
                if (
                    changed == 1
                    or insert_ignore(conn, record_table, [_record_row(connection_id, stream, key)]) == 1
                ):
                    fresh.add(key.record_id)
        return fresh

    # -- OAuth states (AuthStateStore) ------------------------------------------------------------

    def put_pending(self, pending: PendingAuthorization) -> None:
        row = {
            "state": pending.state,
            "expires_at": pending.expires_at,
            "body": pending.model_dump(mode="json"),
        }
        with self.engine.begin() as conn:
            conn.execute(insert(oauth_state_table).values(row))

    def take_pending(self, state: str, now: datetime) -> PendingAuthorization | None:
        t = oauth_state_table.c
        with self.engine.begin() as conn:
            body = conn.execute(select(t.body).where(t.state == state)).scalar()
            if body is None:
                return None
            gone = conn.execute(delete(oauth_state_table).where(t.state == state)).rowcount
            conn.execute(delete(oauth_state_table).where(t.expires_at <= now))
        pending = PendingAuthorization.model_validate(body)
        if gone != 1 or pending.expires_at <= now:
            return None
        return pending

    # -- incidents --------------------------------------------------------------------------------

    def incident(self, connection_id: str, code: str) -> Incident | None:
        t = incident_table.c
        with self.engine.connect() as conn:
            body = conn.execute(
                select(t.body).where(t.connection_id == connection_id, t.code == code)
            ).scalar()
        return None if body is None else Incident.model_validate(body)

    def open_incidents(self, connection_id: str | None = None) -> list[Incident]:
        t = incident_table.c
        query = select(t.body).where(t.open.is_(True)).order_by(t.opened_at)
        if connection_id is not None:
            query = query.where(t.connection_id == connection_id)
        with self.engine.connect() as conn:
            return [Incident.model_validate(b) for b in conn.execute(query).scalars()]

    def put_incident(self, incident: Incident) -> None:
        row = {
            "connection_id": incident.connection_id,
            "code": incident.code,
            "open": incident.open,
            "opened_at": incident.opened_at,
            "body": incident.model_dump(mode="json"),
        }
        with self.engine.begin() as conn:
            upsert(conn, incident_table, [row], keep=())

    def open_incident_once(self, incident: Incident) -> bool:
        """Open `incident` unless the same problem is already open; True when this call opened it.

        Atomic: of two workers noticing the same problem at once, exactly one gets True (and so
        exactly one alert is raised)."""
        t = incident_table.c
        row = {
            "connection_id": incident.connection_id,
            "code": incident.code,
            "open": True,
            "opened_at": incident.opened_at,
            "body": incident.model_dump(mode="json"),
        }
        with self.engine.begin() as conn:
            reopened = conn.execute(
                update(incident_table)
                .where(t.connection_id == incident.connection_id, t.code == incident.code, t.open.is_(False))
                .values(row)
            ).rowcount
            if reopened == 1:
                return True
            return insert_ignore(conn, incident_table, [row]) == 1

    # -- encrypted secrets (used by tokens.SqlTokenVault) -----------------------------------------

    def read_secret(self, connection_id: str) -> tuple[str, str, int] | None:
        t = secret_table.c
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.token, t.key_fingerprint, t.version).where(t.connection_id == connection_id)
            ).first()
        return None if row is None else (str(row.token), str(row.key_fingerprint), int(row.version))

    def write_secret(self, connection_id: str, token: str, fingerprint: str, now: datetime) -> None:
        t = secret_table.c
        with self.engine.begin() as conn:
            current = conn.execute(select(t.version).where(t.connection_id == connection_id)).scalar()
            row = {
                "connection_id": connection_id,
                "token": token,
                "key_fingerprint": fingerprint,
                "version": int(current or 0) + 1,
                "updated_at": now,
            }
            upsert(conn, secret_table, [row], keep=())

    def delete_secret(self, connection_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(secret_table).where(secret_table.c.connection_id == connection_id))

    # -- privacy requests -------------------------------------------------------------------------

    def put_privacy_request(self, request: PrivacyRequest) -> bool:
        row = {
            "request_id": request.request_id,
            "connection_id": request.connection_id,
            "kind": request.kind.value,
            "received_at": request.received_at,
            "status": request.status,
            "body": request.model_dump(mode="json"),
        }
        with self.engine.begin() as conn:
            return insert_ignore(conn, privacy_table, [row]) == 1

    def privacy_requests(self, *, status: str | None = "pending") -> list[PrivacyRequest]:
        t = privacy_table.c
        query = select(t.body).order_by(t.received_at)
        if status is not None:
            query = query.where(t.status == status)
        with self.engine.connect() as conn:
            return [PrivacyRequest.model_validate(b) for b in conn.execute(query).scalars()]


def _later(a: datetime | None, b: datetime | None) -> bool:
    if a is None:
        return False
    return b is None or a > b


def _record_row(connection_id: str, stream: Stream, key: RecordKey) -> dict[str, Any]:
    return {
        "connection_id": connection_id,
        "stream": stream.value,
        "record_id": key.record_id,
        "updated_at": key.updated_at,
    }
