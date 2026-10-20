"""Batches: the decisions a person signs off before anything is sent (autonomy level APPROVE).

When a goal runs at `AutonomyLevel.APPROVE`, each sweep's ACT decisions for that goal are held in one
`Batch`. A person approves or rejects the whole batch; the runtime sends an approved batch on its
next tick and then marks it sent. The life of a batch is a small state machine:

    PENDING -> APPROVED -> SENT
    PENDING -> REJECTED
    APPROVED -> REJECTED   (only when it expires unsent: the runtime held it while its goal was paused)

Every move is a compare-and-set (`BatchStore.transition`): it only happens if the batch is still in
the state the caller saw. Two people clicking approve and reject at the same moment cannot both win,
and a batch can never be sent twice.

Two stores share the same behaviour: `InMemoryBatchStore` for tests and the demo, and
`SqlBatchStore` (its own `batch` table, Postgres or SQLite) for production.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import JSON, Column, Index, MetaData, String, Table, select, update
from sqlalchemy.engine import Engine

from autonomy.contracts import Batch, BatchStatus
from autonomy.store.schema import UtcDateTime
from autonomy.store.sql import insert_ignore

__all__ = [
    "ALLOWED_MOVES",
    "BatchStore",
    "BatchTransitionError",
    "InMemoryBatchStore",
    "SqlBatchStore",
    "UnknownBatchError",
    "approve",
    "batch_metadata",
    "batch_table",
    "create_batch_tables",
    "mark_sent",
    "reject",
]

ALLOWED_MOVES: dict[BatchStatus, frozenset[BatchStatus]] = {
    BatchStatus.PENDING: frozenset({BatchStatus.APPROVED, BatchStatus.REJECTED}),
    BatchStatus.APPROVED: frozenset({BatchStatus.SENT, BatchStatus.REJECTED}),
    BatchStatus.REJECTED: frozenset(),
    BatchStatus.SENT: frozenset(),
}
"""Which status may follow which. Rejected and sent are final."""


class UnknownBatchError(LookupError):
    """No batch with this id."""


class BatchTransitionError(ValueError):
    """A move the lifecycle does not allow, or the batch changed since the caller looked at it."""

    def __init__(self, batch_id: str, current: BatchStatus, wanted: BatchStatus) -> None:
        self.batch_id = batch_id
        self.current = current
        self.wanted = wanted
        super().__init__(f"batch {batch_id} is {current.value}; it cannot become {wanted.value}")


@runtime_checkable
class BatchStore(Protocol):
    def put(self, batch: Batch) -> bool:
        """Store a new batch; return False (and change nothing) if one with this id exists."""
        ...

    def get(self, batch_id: str) -> Batch | None: ...

    def list_batches(
        self, *, goal_id: str | None = None, statuses: Sequence[BatchStatus] | None = None
    ) -> list[Batch]:
        """Batches oldest first (by `created_at`, then id), optionally for one goal and some statuses."""
        ...

    def transition(
        self,
        batch_id: str,
        expected: BatchStatus,
        to: BatchStatus,
        *,
        by: str | None,
        at: datetime,
    ) -> Batch:
        """Move a batch from `expected` to `to` if it is still `expected`; return the new batch.

        Raises `UnknownBatchError` for an unknown id and `BatchTransitionError` when the move is not
        allowed or the batch is no longer in `expected`. `by` and `at` are recorded as `decided_by`
        and `decided_at` for approve and reject; sending keeps the person who approved.
        """
        ...


def _moved(batch: Batch, to: BatchStatus, by: str | None, at: datetime) -> Batch:
    if to is BatchStatus.SENT:
        return batch.model_copy(update={"status": to})
    return batch.model_copy(update={"status": to, "decided_by": by, "decided_at": at})


def _check_move(batch_id: str, current: BatchStatus, expected: BatchStatus, to: BatchStatus) -> None:
    if current is not expected or to not in ALLOWED_MOVES[current]:
        raise BatchTransitionError(batch_id, current, to)


def approve(store: BatchStore, batch_id: str, *, by: str, now: datetime) -> Batch:
    """PENDING -> APPROVED. The runtime sends it on its next tick."""
    if not by.strip():
        raise ValueError("an approval needs the name of the person approving")
    return store.transition(batch_id, BatchStatus.PENDING, BatchStatus.APPROVED, by=by, at=now)


def reject(store: BatchStore, batch_id: str, *, by: str, now: datetime) -> Batch:
    """PENDING -> REJECTED. Nothing in the batch is ever sent."""
    if not by.strip():
        raise ValueError("a rejection needs the name of the person rejecting")
    return store.transition(batch_id, BatchStatus.PENDING, BatchStatus.REJECTED, by=by, at=now)


def mark_sent(store: BatchStore, batch_id: str, *, now: datetime) -> Batch:
    """APPROVED -> SENT, once the runtime has handed the batch to the dispatcher."""
    return store.transition(batch_id, BatchStatus.APPROVED, BatchStatus.SENT, by=None, at=now)


# --- in memory --------------------------------------------------------------------------------------


class InMemoryBatchStore:
    def __init__(self, batches: Iterable[Batch] = ()) -> None:
        self._batches: dict[str, Batch] = {}
        for b in batches:
            self.put(b)

    def put(self, batch: Batch) -> bool:
        if batch.batch_id in self._batches:
            return False
        self._batches[batch.batch_id] = batch
        return True

    def get(self, batch_id: str) -> Batch | None:
        return self._batches.get(batch_id)

    def list_batches(
        self, *, goal_id: str | None = None, statuses: Sequence[BatchStatus] | None = None
    ) -> list[Batch]:
        wanted = set(statuses) if statuses is not None else None
        rows = [
            b
            for b in self._batches.values()
            if (goal_id is None or b.goal_id == goal_id) and (wanted is None or b.status in wanted)
        ]
        return sorted(rows, key=lambda b: (b.created_at, b.batch_id))

    def transition(
        self,
        batch_id: str,
        expected: BatchStatus,
        to: BatchStatus,
        *,
        by: str | None,
        at: datetime,
    ) -> Batch:
        current = self._batches.get(batch_id)
        if current is None:
            raise UnknownBatchError(batch_id)
        _check_move(batch_id, current.status, expected, to)
        moved = _moved(current, to, by, at)
        self._batches[batch_id] = moved
        return moved


# --- SQL --------------------------------------------------------------------------------------------

batch_metadata = MetaData()
"""Its own metadata, like the prior table: `autonomy.store.create_all` does not create it."""

batch_table = Table(
    "batch",
    batch_metadata,
    Column("batch_id", String(200), primary_key=True),
    Column("goal_id", String(200), nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("body", JSON, nullable=False, doc="The whole batch, rewritten with the columns"),
    Index("ix_batch_goal_status", "goal_id", "status"),
)


def create_batch_tables(engine: Engine) -> None:
    """Create the batch table if it does not exist. Safe to call twice."""
    batch_metadata.create_all(engine)


class SqlBatchStore:
    """A `BatchStore` on Postgres or SQLite. Transitions are a conditional UPDATE on the status."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, batch: Batch) -> bool:
        row: dict[str, Any] = {
            "batch_id": batch.batch_id,
            "goal_id": batch.goal_id,
            "status": batch.status.value,
            "created_at": batch.created_at,
            "body": batch.model_dump(mode="json"),
        }
        with self._engine.begin() as conn:
            return insert_ignore(conn, batch_table, [row]) > 0

    def get(self, batch_id: str) -> Batch | None:
        query = select(batch_table.c.body).where(batch_table.c.batch_id == batch_id)
        with self._engine.connect() as conn:
            body = conn.execute(query).scalar_one_or_none()
        return Batch.model_validate(body) if body is not None else None

    def list_batches(
        self, *, goal_id: str | None = None, statuses: Sequence[BatchStatus] | None = None
    ) -> list[Batch]:
        t = batch_table.c
        query = select(t.body)
        if goal_id is not None:
            query = query.where(t.goal_id == goal_id)
        if statuses is not None:
            query = query.where(t.status.in_([s.value for s in statuses]))
        with self._engine.connect() as conn:
            rows = [Batch.model_validate(body) for body in conn.execute(query).scalars()]
        # Sorted here, not in SQL, so the order is code-point order on every database (as in memory).
        return sorted(rows, key=lambda b: (b.created_at, b.batch_id))

    def transition(
        self,
        batch_id: str,
        expected: BatchStatus,
        to: BatchStatus,
        *,
        by: str | None,
        at: datetime,
    ) -> Batch:
        current = self.get(batch_id)
        if current is None:
            raise UnknownBatchError(batch_id)
        _check_move(batch_id, current.status, expected, to)
        moved = _moved(current, to, by, at)
        t = batch_table.c
        with self._engine.begin() as conn:
            changed = conn.execute(
                update(batch_table)
                .where(t.batch_id == batch_id, t.status == expected.value)
                .values(status=to.value, body=moved.model_dump(mode="json"))
            ).rowcount
        if changed != 1:
            # Someone else moved it between our read and our write.
            latest = self.get(batch_id)
            raise BatchTransitionError(batch_id, latest.status if latest else expected, to)
        return moved
