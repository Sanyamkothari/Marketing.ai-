"""The audit store (M47): append-only in the database, queryable, and never a place for a value."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import DatabaseError, IntegrityError

from engine.access.roles import SYSTEM_SCHEDULER, Principal, Role
from engine.audit.events import AuditEvent, AuditLog, AuditQuery
from engine.audit.store import (
    ANONYMOUS_ACTOR_ID,
    MemoryAuditLog,
    SqlAuditLog,
    new_event_id,
    record_event,
)
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine

T0 = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
ASHA = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.APPROVER}))


def event(minutes: int, action: str = "runs.create", **fields: Any) -> AuditEvent:
    values: dict[str, Any] = {
        "event_id": new_event_id(),
        "occurred_at": T0 + timedelta(minutes=minutes),
        "actor_id": "u-asha",
        "actor_kind": "user",
        "action": action,
    }
    values.update(fields)
    return AuditEvent(**values)


@pytest.fixture(params=["sql", "memory"])
def log(request: pytest.FixtureRequest, tmp_path: Path) -> AuditLog:
    if request.param == "memory":
        return MemoryAuditLog()
    return SqlAuditLog(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME))


def test_both_logs_are_audit_logs(log: AuditLog) -> None:
    assert isinstance(log, AuditLog)


def test_an_event_round_trips_exactly(log: AuditLog) -> None:
    written = event(
        0,
        "models.approve",
        object_type="model",
        object_id="m-1",
        before_hash="a" * 64,
        after_hash="b" * 64,
        request_id="r-1",
        outcome="success",
        details={"method": "POST", "status_code": 200, "dry_run": False, "count": 1.5, "role": None},
    )
    log.append(written)
    (read,) = log.query(AuditQuery())
    assert read == written
    assert read.occurred_at.tzinfo is not None


def test_query_is_newest_first_and_pages(log: AuditLog) -> None:
    for minute in range(5):
        log.append(event(minute, object_id=f"r-{minute}"))
    assert [e.object_id for e in log.query(AuditQuery())] == ["r-4", "r-3", "r-2", "r-1", "r-0"]
    assert [e.object_id for e in log.query(AuditQuery(limit=2, offset=1))] == ["r-3", "r-2"]
    assert log.count(AuditQuery(limit=1)) == 5


def test_every_filter_narrows(log: AuditLog) -> None:
    log.append(event(0, "models.approve", object_type="model", object_id="m-1"))
    log.append(event(1, "models.promote", object_type="model", object_id="m-2", outcome="denied"))
    log.append(event(2, "runs.create", object_type="run", object_id="r-1", actor_id="u-bo"))
    log.append(event(3, "runs_x.create"))

    def actions(**filters: Any) -> list[str]:
        return [e.action for e in log.query(AuditQuery(**filters))]

    assert actions(action="models.") == ["models.promote", "models.approve"]
    assert actions(action="models.approve") == ["models.approve"]
    assert actions(action="models") == []  # a family needs its trailing dot
    assert actions(action="runs.") == ["runs.create"]  # `runs.` never matches `runs_x.` (LIKE is escaped)
    assert actions(action="runs_.") == []
    assert actions(actor_id="u-bo") == ["runs.create"]
    assert actions(object_type="model", outcome="denied") == ["models.promote"]
    assert actions(object_id="m-1") == ["models.approve"]
    assert actions(since=T0 + timedelta(minutes=1), until=T0 + timedelta(minutes=3)) == [
        "runs.create",
        "models.promote",
    ]
    assert log.count(AuditQuery(action="models.")) == 2


def test_a_window_in_another_time_zone_means_the_same_instant(log: AuditLog) -> None:
    log.append(event(0))
    ist = T0.astimezone(timezone(timedelta(hours=5, minutes=30)))  # 15:30 in Kolkata is 10:00Z
    assert log.count(AuditQuery(since=ist)) == 1
    assert log.count(AuditQuery(until=ist)) == 0


def test_a_duplicate_event_id_is_refused_not_overwritten(log: AuditLog) -> None:
    first = event(0)
    log.append(first)
    with pytest.raises((IntegrityError, ValueError)):
        log.append(first.model_copy(update={"action": "runs.cancel"}))
    assert [e.action for e in log.query(AuditQuery())] == ["runs.create"]


def test_the_sqlite_table_refuses_update_and_delete(tmp_path: Path) -> None:
    """DEC-714: append-only below the application, not only by the protocol's shape."""
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    log = SqlAuditLog(engine)
    log.append(event(0))
    for statement in ("UPDATE audit_events SET action = 'forged'", "DELETE FROM audit_events"):
        with pytest.raises(DatabaseError, match="append-only"), engine.begin() as connection:
            connection.exec_driver_sql(statement)
    assert [e.action for e in log.query(AuditQuery())] == ["runs.create"]


def test_the_protocol_has_no_way_to_change_an_event() -> None:
    for name in ("update", "delete", "remove", "clear"):
        assert not hasattr(SqlAuditLog, name)
        assert not hasattr(MemoryAuditLog, name)


def test_a_second_store_on_the_same_file_reads_the_same_trail(tmp_path: Path) -> None:
    path = tmp_path / PLATFORM_DB_FILENAME
    SqlAuditLog(sqlite_engine(path)).append(event(0))
    assert SqlAuditLog(sqlite_engine(path)).count(AuditQuery()) == 1


def test_record_event_writes_one_event_for_a_principal_or_anonymous(log: AuditLog) -> None:
    written = record_event(log, SYSTEM_SCHEDULER, "schedules.fire", object_type="schedule", object_id="s-1")
    anonymous = record_event(
        log, None, "auth.login", outcome="failed", details={"reason_code": "BAD_CREDENTIALS"}
    )
    assert (written.actor_id, written.actor_kind) == ("system:scheduler", "system")
    assert (anonymous.actor_id, anonymous.actor_kind) == (ANONYMOUS_ACTOR_ID, "anonymous")
    assert log.count(AuditQuery()) == 2
    assert {e.event_id for e in log.query(AuditQuery())} == {written.event_id, anonymous.event_id}


def test_record_event_refuses_a_detail_key_outside_the_allow_list(log: AuditLog) -> None:
    with pytest.raises(ValidationError):
        record_event(log, ASHA, "uploads.create", details={"customer_email": "someone@example.com"})
    assert log.count(AuditQuery()) == 0
