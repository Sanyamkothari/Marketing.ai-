"""The job queue in SQL: scheduled work (decide sweeps, nightly learning, briefings, webhook events).

Several workers may claim at once. On Postgres the claim takes the next due row with
`SELECT ... FOR UPDATE SKIP LOCKED`, so two workers never get the same job and neither waits for the
other. SQLite has no row locks; there the claim is a plain transaction whose UPDATE only succeeds if
the job is still queued, and a worker that loses the race simply looks again.

Behaviour matches `autonomy.fakes.InMemoryJobQueue`: the next job is the earliest `run_at`, ties
broken by `job_id` in code-point order; claiming marks it running and counts an attempt; a failure
with `retry_at` puts it back in the queue for later.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from autonomy.contracts import Job, JobStatus
from autonomy.store.schema import job_table
from autonomy.store.sql import sortable, upsert

__all__ = ["SqlJobQueue"]


def _row(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "kind": job.kind,
        "status": job.status.value,
        "run_at": job.run_at,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "payload": dict(job.payload),
        "body": job.model_dump(mode="json"),
    }


class SqlJobQueue:
    """A `JobQueue` over any SQLAlchemy engine; built for Postgres, tested on SQLite too."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._dialect = engine.dialect.name

    def enqueue(self, job: Job) -> None:
        """Add a job, or replace one with the same id (and forget who claimed it)."""
        row = _row(job) | {"claimed_by": None, "claimed_at": None}
        with self._engine.begin() as conn:
            upsert(conn, job_table, [row], keep=())

    def claim(self, kinds: Sequence[str], *, worker: str, now: datetime) -> Job | None:
        if not kinds:
            return None
        t = job_table.c
        query = (
            select(t.job_id, t.body)
            .where(t.status == JobStatus.QUEUED.value, t.kind.in_(list(kinds)), t.run_at <= now)
            .order_by(t.run_at, sortable(t.job_id, self._dialect))
            .limit(1)
        )
        if self._dialect == "postgresql":
            query = query.with_for_update(skip_locked=True)
        with self._engine.begin() as conn:
            while True:
                found = conn.execute(query).first()
                if found is None:
                    return None
                job = Job.model_validate(found.body)
                claimed = job.model_copy(update={"status": JobStatus.RUNNING, "attempts": job.attempts + 1})
                values = _row(claimed) | {"claimed_by": worker, "claimed_at": now}
                won = conn.execute(
                    update(job_table)
                    .where(t.job_id == found.job_id, t.status == JobStatus.QUEUED.value)
                    .values(values)
                ).rowcount
                if won == 1:
                    return claimed
                # SQLite only: another worker took it between our read and our write. Look again.

    def complete(self, job_id: str) -> None:
        with self._engine.begin() as conn:
            job = self._locked(conn, job_id)
            self._save(conn, job.model_copy(update={"status": JobStatus.DONE}))

    def fail(self, job_id: str, error: str, *, retry_at: datetime | None = None) -> None:
        """Record the error; with `retry_at` the job goes back in the queue for then, else it stops."""
        with self._engine.begin() as conn:
            job = self._locked(conn, job_id)
            if retry_at is None:
                changes: dict[str, Any] = {"status": JobStatus.FAILED, "last_error": error}
            else:
                changes = {"status": JobStatus.QUEUED, "run_at": retry_at, "last_error": error}
            self._save(conn, job.model_copy(update=changes))

    def get(self, job_id: str) -> Job | None:
        with self._engine.connect() as conn:
            body = conn.execute(select(job_table.c.body).where(job_table.c.job_id == job_id)).scalar()
        return None if body is None else Job.model_validate(body)

    def _locked(self, conn: Connection, job_id: str) -> Job:
        """The job, row-locked on Postgres for the rest of the transaction. KeyError if unknown."""
        query = select(job_table.c.body).where(job_table.c.job_id == job_id)
        if self._dialect == "postgresql":
            query = query.with_for_update()
        body = conn.execute(query).scalar()
        if body is None:
            raise KeyError(job_id)
        return Job.model_validate(body)

    def _save(self, conn: Connection, job: Job) -> None:
        conn.execute(update(job_table).where(job_table.c.job_id == job.job_id).values(_row(job)))
