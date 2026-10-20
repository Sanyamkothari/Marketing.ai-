"""WF1 persistence: SQL versions of the event store, state store, decision log, workspace and job queue.

Postgres in production and CI, SQLite on a laptop; both through SQLAlchemy Core and one schema
(`autonomy.store.schema`). Each class behaves exactly like its in-memory fake in `autonomy.fakes`.

    engine = create_engine(url)
    create_all(engine)
    events = SqlEventStore(engine)
"""

from autonomy.store.queue import SqlJobQueue
from autonomy.store.schema import create_all, metadata
from autonomy.store.sql import SqlDecisionLog, SqlEventStore, SqlStateStore, SqlWorkspace, memory_engine

__all__ = [
    "SqlDecisionLog",
    "SqlEventStore",
    "SqlJobQueue",
    "SqlStateStore",
    "SqlWorkspace",
    "create_all",
    "memory_engine",
    "metadata",
]
