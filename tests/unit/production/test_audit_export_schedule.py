"""The scheduled audit export and the housekeeping schedule (DEC-726, DEC-775): nobody has to ask."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from engine.audit.events import AuditEvent, AuditQuery
from engine.audit.export import (
    AUDIT_EXPORT_JOB,
    SCHEDULED_EXPORT_ACTION,
    LocalAuditExportSink,
    export_due,
)
from engine.audit.store import MemoryAuditLog, new_event_id
from engine.scheduling.scheduler import (
    HOUSEKEEPING_ARGUMENTS,
    HOUSEKEEPING_SCHEDULE_NAME,
    EventBridgeScheduler,
)
from tests.unit.production.test_eventbridge_scheduler import GROUP, REGION, settings

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def event(at: datetime) -> AuditEvent:
    return AuditEvent(
        event_id=new_event_id(),
        occurred_at=at,
        actor_id="u-asha",
        actor_kind="user",
        action="runs.create",
        details={"method": "POST", "status_code": 201},
    )


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_consecutive_scheduled_exports_tile_the_log(tmp_path: Path) -> None:
    log = MemoryAuditLog()
    sink = LocalAuditExportSink(tmp_path)
    log.append(event(T0))
    first = export_due(log, sink, now=T0 + timedelta(hours=1))
    assert first is not None and first.event_count == 1
    assert export_due(log, sink, now=T0 + timedelta(hours=5)) is None, "not due for a day"

    log.append(event(T0 + timedelta(hours=3)))
    second = export_due(log, sink, now=T0 + timedelta(days=1, hours=1))
    assert second is not None
    exported = lines(tmp_path / "audit" / "exports" / second.key)
    assert [row["action"] for row in exported] == [
        SCHEDULED_EXPORT_ACTION,
        "runs.create",
    ], "the previous export's own event and what came after it - nothing from the first window"

    recorded = log.query(AuditQuery(action=SCHEDULED_EXPORT_ACTION))
    assert len(recorded) == 2
    newest = recorded[0]
    assert newest.actor_id == AUDIT_EXPORT_JOB.user_id
    assert newest.after_hash == second.sha256
    assert newest.details["window_start"] == (T0 + timedelta(hours=1)).isoformat()


def test_an_overdue_export_is_logged_at_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    log = MemoryAuditLog()
    sink = LocalAuditExportSink(tmp_path)
    export_due(log, sink, now=T0)
    with caplog.at_level(logging.ERROR, logger="engine.audit.export"):
        export_due(log, sink, now=T0 + timedelta(days=3))
    assert any("audit export overdue" in record.getMessage() for record in caplog.records)


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        made = boto3.client("scheduler", region_name=REGION)
        made.create_schedule_group(Name=GROUP)
        yield made


def test_the_housekeeping_schedule_sweeps_and_exports_every_hour(client: Any) -> None:
    scheduler = EventBridgeScheduler.from_settings(settings(), client=client)
    scheduler.ensure_housekeeping()
    scheduler.ensure_housekeeping()  # a restart updates it in place
    stored = client.get_schedule(Name=HOUSEKEEPING_SCHEDULE_NAME, GroupName=GROUP)
    assert stored["ScheduleExpression"] == "rate(1 hour)"
    assert stored["State"] == "ENABLED"
    overrides = json.loads(stored["Target"]["Input"])
    (container,) = overrides["containerOverrides"]
    assert container["command"][-2:] == list(HOUSEKEEPING_ARGUMENTS)
