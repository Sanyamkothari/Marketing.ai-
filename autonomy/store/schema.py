"""The v2 tables, written once in SQLAlchemy Core so Postgres (production, CI) and SQLite (laptops,
tests) share one definition.

Every table keeps the full contract in `body` (a JSON copy of `model_dump(mode="json")`) and lifts
out only the columns we filter, sort or join on. Reading back is `model_validate(body)`, so a field
added to a contract later needs no migration here; a new column is only needed to query by it.

Several tables carry a `seq` column. It records the order rows were first written, which is how the
in-memory fakes break ties (Python's sort is stable and dicts keep insertion order). Without it two
events with the same timestamp could come back in a different order from SQL than from the fake, and
folding them into state could give a different answer.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.types import TypeDecorator

__all__ = [
    "UtcDateTime",
    "brief_table",
    "content_item_table",
    "create_all",
    "customer_state_table",
    "decision_table",
    "event_table",
    "goal_table",
    "job_table",
    "metadata",
]


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp stored in UTC, refusing naive datetimes.

    Postgres keeps `timestamptz` natively. SQLite has no time zones: SQLAlchemy writes the wall-clock
    digits and drops the offset, so two moments with different offsets would compare wrongly. We
    therefore convert to UTC before writing and mark values UTC again when reading, which makes the
    stored text sort in true time order on both databases.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"a naive datetime cannot be stored; give it a time zone: {value!r}")
        utc = value.astimezone(UTC)
        return utc if dialect.name == "postgresql" else utc.replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,  # noqa: ARG002 - TypeDecorator fixes the signature
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


metadata = MetaData()
"""Every v2 table. Pass `schema_translate_map` on the engine to place them in a schema of your own."""

event_table = Table(
    "event",
    metadata,
    Column("event_id", String(200), primary_key=True),
    Column("customer_id", String(200), nullable=False),
    Column("type", String(40), nullable=False),
    Column("ts", UtcDateTime(), nullable=False),
    Column("decision_id", String(200), nullable=True),
    Column("seq", BigInteger, nullable=False, doc="Write order; breaks ties between equal timestamps"),
    Column("body", JSON, nullable=False),
    Index("ix_event_customer_ts", "customer_id", "ts"),
    Index("ix_event_decision", "decision_id"),
    Index("ix_event_ts_seq", "ts", "seq"),
    Index("ix_event_seq", "seq"),
)

customer_state_table = Table(
    "customer_state",
    metadata,
    Column("customer_id", String(200), primary_key=True),
    Column("as_of", UtcDateTime(), nullable=False),
    Column("version", Integer, nullable=False),
    Column("body", JSON, nullable=False),
)

decision_table = Table(
    "decision",
    metadata,
    Column("decision_id", String(200), primary_key=True),
    Column("customer_id", String(200), nullable=False),
    Column("made_at", UtcDateTime(), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("goal_id", String(200), nullable=True),
    Column("probability", Float, nullable=False),
    Column("seq", BigInteger, nullable=False, doc="First-write order; kept when a decision is re-recorded"),
    Column("body", JSON, nullable=False),
    Index("ix_decision_customer_made_at", "customer_id", "made_at"),
    Index("ix_decision_made_at_seq", "made_at", "seq"),
    Index("ix_decision_seq", "seq"),
)

goal_table = Table(
    "goal",
    metadata,
    Column("goal_id", String(200), primary_key=True),
    Column("seq", BigInteger, nullable=False),
    Column("body", JSON, nullable=False),
)

brief_table = Table(
    "brief",
    metadata,
    Column("goal_id", String(200), nullable=False),
    Column("version", Integer, nullable=False),
    Column("brief_id", String(200), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("body", JSON, nullable=False),
    PrimaryKeyConstraint("goal_id", "version", name="pk_brief"),
)

content_item_table = Table(
    "content_item",
    metadata,
    Column("content_id", String(200), primary_key=True),
    Column("goal_id", String(200), nullable=True),
    Column("status", String(20), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("body", JSON, nullable=False),
    Index("ix_content_item_goal", "goal_id"),
)

job_table = Table(
    "job",
    metadata,
    Column("job_id", String(200), primary_key=True),
    Column("kind", String(100), nullable=False),
    Column("status", String(20), nullable=False),
    Column("run_at", UtcDateTime(), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("last_error", Text, nullable=True),
    Column("payload", JSON, nullable=False),
    Column("claimed_by", String(200), nullable=True, doc="The worker that last claimed it"),
    Column("claimed_at", UtcDateTime(), nullable=True),
    Column("body", JSON, nullable=False, doc="The whole job, rewritten with the columns"),
    Index("ix_job_due", "status", "kind", "run_at"),
)


def create_all(engine: Engine) -> None:
    """Create every v2 table that does not exist yet. Safe to call twice."""
    metadata.create_all(engine)
