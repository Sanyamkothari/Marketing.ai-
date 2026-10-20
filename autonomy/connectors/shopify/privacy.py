"""Carrying out Shopify's three mandatory privacy requests on our copy of the shop's data.

The base records each compliance webhook as a `PrivacyRequest` (`connector_privacy`, status
`pending`) and raises one alert. `ShopifyPrivacyDesk.run_pending(now)` then does the work, from a
worker job that repeats every hour (`privacy_handlers`):

| Topic | What happens to our copy | Request ends as |
|---|---|---|
| `customers/redact` | the customer's events (every source), customer state and decisions are deleted, and the connector's record versions for the customer and the orders named | `done`, its alert closed |
| `shop/redact` (48 h after uninstall) | every Shopify-sourced event of the shop's customers, their customer states and decisions, and the connection's record versions and sync positions are deleted | `done`, its alert closed |
| `customers/data_request` | everything we hold about the customer (events, state, decisions) is exported as one JSON document, kept in `shopify_privacy_result` for the API to hand to the merchant | `exported`; its alert stays open until a person has sent it (Shopify expects the merchant to receive it) |

The event store is otherwise append-only; erasure is the one deliberate exception, and the
runtime's state for an erased customer is deleted with it rather than rebuilt. Other stores that
hold customer data (a message ledger, a channel's own tables) plug in through `erasers`, each
called with the customer ids and returning how many rows it removed. A request that fails stays
`pending` and is tried again on the next run; Shopify allows 30 days.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import JSON, Column, MetaData, String, Table, delete, select, update
from sqlalchemy.engine import Engine

from autonomy.connectors.base import Connection, PrivacyKind, Stream
from autonomy.connectors.health import ALERT_PRIVACY, IncidentDesk
from autonomy.connectors.store import (
    ConnectorStore,
    PrivacyRequest,
    position_table,
    privacy_table,
    record_table,
)
from autonomy.connectors.sync import enqueue_once, slot_after
from autonomy.contracts import Event, Job, stable_id
from autonomy.store.schema import UtcDateTime, customer_state_table, decision_table, event_table
from autonomy.store.sql import upsert

if TYPE_CHECKING:
    from autonomy.interfaces import JobQueue

__all__ = [
    "PRIVACY_JOB_KIND",
    "PrivacyOutcome",
    "ShopifyPrivacyDesk",
    "create_privacy_tables",
    "privacy_handlers",
    "privacy_job",
]

KIND: Final[str] = "shopify"
SOURCE: Final[str] = "shopify"
PRIVACY_JOB_KIND: Final[str] = "shopify_privacy"
EVERY: Final[timedelta] = timedelta(hours=1)
CHUNK: Final[int] = 500

Eraser = Callable[[Sequence[str]], int]

privacy_metadata = MetaData()
result_table = Table(
    "shopify_privacy_result",
    privacy_metadata,
    Column("request_id", String(200), primary_key=True),
    Column("status", String(20), nullable=False),
    Column("completed_at", UtcDateTime(), nullable=False),
    Column("body", JSON, nullable=False),
)


def create_privacy_tables(engine: Engine) -> None:
    """Create `shopify_privacy_result` if it is missing. Safe to call twice."""
    privacy_metadata.create_all(engine)


@dataclass(frozen=True)
class PrivacyOutcome:
    request_id: str
    kind: PrivacyKind
    status: str
    """'done', 'exported', or 'failed' (left pending, retried next run)."""
    removed: Mapping[str, int] = field(default_factory=dict)
    detail: str | None = None


def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]


class ShopifyPrivacyDesk:
    """Executes pending Shopify privacy requests. `db` holds the event, state and decision tables."""

    def __init__(
        self,
        store: ConnectorStore,
        db: Engine,
        *,
        desk: IncidentDesk | None = None,
        erasers: Sequence[Eraser] = (),
    ) -> None:
        self.store = store
        self.db = db
        self.desk = desk
        self.erasers = tuple(erasers)
        create_privacy_tables(store.engine)

    # -- running ---------------------------------------------------------------------------------

    def run_pending(self, now: datetime) -> list[PrivacyOutcome]:
        outcomes: list[PrivacyOutcome] = []
        for request in self.store.privacy_requests(status="pending"):
            connection = self.store.get_connection(request.connection_id)
            if connection is None or connection.kind != KIND:
                continue
            try:
                outcomes.append(self.execute(connection, request, now))
            except Exception as exc:  # stays pending: tried again next run, well inside 30 days
                outcomes.append(
                    PrivacyOutcome(request.request_id, request.kind, "failed", detail=type(exc).__name__)
                )
        return outcomes

    def execute(self, connection: Connection, request: PrivacyRequest, now: datetime) -> PrivacyOutcome:
        subject = request.subject
        customer = str(subject.get("customer_id") or "")
        orders = [str(o) for o in subject.get("order_ids") or () if o]
        if request.kind is PrivacyKind.DATA_REQUEST:
            export = self.export_customer(customer) if customer else {"customer_id": None, "events": []}
            self._finish(request, "exported", now, {"export": export})
            return PrivacyOutcome(request.request_id, request.kind, "exported")
        if request.kind is PrivacyKind.REDACT_CUSTOMER:
            removed = self.erase_customers(connection, [customer] if customer else [], orders)
        else:
            removed = self.erase_shop(connection)
        self._finish(request, "done", now, {"removed": removed})
        if self.desk is not None:
            self.desk.resolve(connection.connection_id, f"{ALERT_PRIVACY}:{request.request_id}", now)
        return PrivacyOutcome(request.request_id, request.kind, "done", removed=removed)

    def result(self, request_id: str) -> Mapping[str, Any] | None:
        """What a finished request produced (the export of a data request), for the API."""
        with self.store.engine.connect() as conn:
            body = conn.execute(
                select(result_table.c.body).where(result_table.c.request_id == request_id)
            ).scalar()
        return body if isinstance(body, Mapping) else None

    # -- the work --------------------------------------------------------------------------------

    def export_customer(self, customer_id: str) -> dict[str, Any]:
        t, s, d = event_table.c, customer_state_table.c, decision_table.c
        with self.db.connect() as conn:
            events = list(
                conn.execute(
                    select(t.body).where(t.customer_id == customer_id).order_by(t.ts, t.seq)
                ).scalars()
            )
            state = conn.execute(select(s.body).where(s.customer_id == customer_id)).scalar()
            decisions = list(
                conn.execute(select(d.body).where(d.customer_id == customer_id).order_by(d.made_at)).scalars()
            )
        return {"customer_id": customer_id, "events": events, "state": state, "decisions": decisions}

    def erase_customers(
        self, connection: Connection, customer_ids: Sequence[str], order_ids: Sequence[str] = ()
    ) -> dict[str, int]:
        """Everything about these customers, from every source."""
        removed = {"events": 0, "states": 0, "decisions": 0, "records": 0, "other": 0}
        ids = [c for c in dict.fromkeys(customer_ids) if c]
        with self.db.begin() as conn:
            for chunk in _chunks(ids):
                removed["events"] += conn.execute(
                    delete(event_table).where(event_table.c.customer_id.in_(chunk))
                ).rowcount
                removed.update(self._states_and_decisions(conn, chunk, removed))
        removed["records"] = self._forget_records(connection, ids, order_ids)
        removed["other"] = sum(eraser(ids) for eraser in self.erasers) if ids else 0
        return removed

    def erase_shop(self, connection: Connection) -> dict[str, int]:
        """The shop's data: its customers' Shopify events, states and decisions, and the sync state."""
        rt = record_table.c
        with self.store.engine.connect() as conn:
            ids = [
                str(r)
                for r in conn.execute(
                    select(rt.record_id).where(
                        rt.connection_id == connection.connection_id, rt.stream == Stream.CUSTOMERS.value
                    )
                ).scalars()
            ]
        removed = {"events": 0, "states": 0, "decisions": 0, "records": 0, "other": 0}
        t = event_table.c
        with self.db.begin() as conn:
            for chunk in _chunks(ids):
                rows = conn.execute(select(t.event_id, t.body).where(t.customer_id.in_(chunk))).all()
                doomed = [str(r.event_id) for r in rows if _source(r.body) == SOURCE]
                for part in _chunks(doomed):
                    removed["events"] += conn.execute(
                        delete(event_table).where(t.event_id.in_(part))
                    ).rowcount
                removed.update(self._states_and_decisions(conn, chunk, removed))
        with self.store.engine.begin() as conn:
            removed["records"] = conn.execute(
                delete(record_table).where(rt.connection_id == connection.connection_id)
            ).rowcount
            conn.execute(
                delete(position_table).where(position_table.c.connection_id == connection.connection_id)
            )
        removed["other"] = sum(eraser(ids) for eraser in self.erasers) if ids else 0
        return removed

    # -- internals -------------------------------------------------------------------------------

    @staticmethod
    def _states_and_decisions(conn: Any, chunk: Sequence[str], removed: Mapping[str, int]) -> dict[str, int]:
        states = conn.execute(
            delete(customer_state_table).where(customer_state_table.c.customer_id.in_(chunk))
        ).rowcount
        decisions = conn.execute(
            delete(decision_table).where(decision_table.c.customer_id.in_(chunk))
        ).rowcount
        return {"states": removed["states"] + states, "decisions": removed["decisions"] + decisions}

    def _forget_records(
        self, connection: Connection, customer_ids: Sequence[str], order_ids: Sequence[str]
    ) -> int:
        rt = record_table.c
        count = 0
        with self.store.engine.begin() as conn:
            for stream, ids in ((Stream.CUSTOMERS, customer_ids), (Stream.ORDERS, order_ids)):
                for chunk in _chunks(list(ids)):
                    count += conn.execute(
                        delete(record_table).where(
                            rt.connection_id == connection.connection_id,
                            rt.stream == stream.value,
                            rt.record_id.in_(chunk),
                        )
                    ).rowcount
        return count

    def _finish(self, request: PrivacyRequest, status: str, now: datetime, body: Mapping[str, Any]) -> None:
        with self.store.engine.begin() as conn:
            upsert(
                conn,
                result_table,
                [
                    {
                        "request_id": request.request_id,
                        "status": status,
                        "completed_at": now,
                        "body": dict(body),
                    }
                ],
                keep=(),
            )
            conn.execute(
                update(privacy_table)
                .where(privacy_table.c.request_id == request.request_id)
                .values(
                    status=status, body=request.model_copy(update={"status": status}).model_dump(mode="json")
                )
            )


def _source(body: Any) -> str | None:
    if isinstance(body, Mapping):
        return str(body.get("source") or "") or None
    try:
        return Event.model_validate(body).source
    except Exception:
        return None


def privacy_job(run_at: datetime) -> Job:
    return Job(
        job_id=stable_id("job", PRIVACY_JOB_KIND, run_at.isoformat()),
        kind=PRIVACY_JOB_KIND,
        payload={},
        run_at=run_at,
    )


def privacy_handlers(desk: ShopifyPrivacyDesk, queue: JobQueue) -> dict[str, Callable[[Job, datetime], None]]:
    """The worker handler for `shopify_privacy`: run pending requests, then queue the next hourly run.

    Start the chain once at deploy time with `enqueue_once(queue, privacy_job(now))`; the job id is
    per slot, so starting it twice is harmless."""

    def run(job: Job, now: datetime) -> None:  # noqa: ARG001
        enqueue_once(queue, privacy_job(slot_after(now, EVERY)))
        desk.run_pending(now)

    return {PRIVACY_JOB_KIND: run}
