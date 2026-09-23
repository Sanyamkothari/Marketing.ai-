"""Audit exports (M47): JSON lines locally, S3 Object Lock in COMPLIANCE mode in AWS, CSV for the viewer.

The S3 path runs against moto with a bucket created with `ObjectLockEnabledForBucket=True`, and the
retention is read back from the object itself - the claim under test is "the file cannot be deleted
before this date", and the object's own retention is the only place that claim lives.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.audit.events import AuditEvent, AuditQuery
from engine.audit.export import (
    CSV_COLUMNS,
    EXPORT_DIRECTORY,
    AuditExportSink,
    LocalAuditExportSink,
    S3AuditExportSink,
    collect_events,
    export_events,
    export_sink_for,
    render_csv,
    render_jsonl,
)
from engine.audit.store import MemoryAuditLog, new_event_id
from engine.settings import Settings

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
BUCKET = "audit-lock-unit"
REGION = "us-east-1"


def event(minutes: int, action: str = "runs.create", object_id: str | None = None) -> AuditEvent:
    return AuditEvent(
        event_id=new_event_id(),
        occurred_at=T0 + timedelta(minutes=minutes),
        actor_id="u-asha",
        actor_kind="user",
        action=action,
        object_id=object_id,
        details={"method": "POST", "status_code": 201},
    )


@pytest.fixture
def log() -> MemoryAuditLog:
    subject = MemoryAuditLog()
    for minute in range(3):
        subject.append(event(minute, object_id=f"r-{minute}"))
    return subject


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_SECURITY_TOKEN", "testing"),
        ("AWS_SESSION_TOKEN", "testing"),
        ("AWS_DEFAULT_REGION", REGION),
    ):
        monkeypatch.setenv(name, value)


@pytest.fixture
def s3(aws_credentials: None) -> Iterator[Any]:
    moto = pytest.importorskip("moto", reason="the Object Lock export is tested against moto")
    import boto3

    with moto.mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, ObjectLockEnabledForBucket=True)
        yield client


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_jsonl_is_one_sorted_object_per_line() -> None:
    events = [event(0), event(1)]
    body = render_jsonl(events)
    lines = body.decode().splitlines()
    assert len(lines) == 2 and body.endswith(b"\n")
    assert [AuditEvent.model_validate_json(line) for line in lines] == events
    assert list(json.loads(lines[0])) == sorted(json.loads(lines[0]))
    assert render_jsonl([]) == b""


def test_csv_has_the_viewer_columns_and_neutralises_formulas() -> None:
    hostile = event(0, action="runs.create", object_id="=HYPERLINK(1)")
    signed = event(1, action="-2+3", object_id="@SUM(A1)")
    text = render_csv([hostile, signed])
    rows = list(csv.reader(io.StringIO(text)))
    assert tuple(rows[0]) == CSV_COLUMNS
    by_column = [dict(zip(CSV_COLUMNS, row, strict=True)) for row in rows[1:]]
    assert by_column[0]["object_id"] == "'=HYPERLINK(1)"
    assert by_column[1]["action"] == "'-2+3"
    assert by_column[1]["object_id"] == "'@SUM(A1)"
    assert json.loads(by_column[0]["details"]) == {"method": "POST", "status_code": 201}


def test_collect_events_pages_past_the_query_ceiling_and_returns_oldest_first() -> None:
    subject = MemoryAuditLog()
    for minute in range(10005):
        subject.append(event(minute))
    collected = collect_events(subject, AuditQuery(limit=5))
    assert len(collected) == 10005
    assert collected[0].occurred_at == T0
    assert collected == sorted(collected, key=lambda item: item.occurred_at)


# ---------------------------------------------------------------------------
# Local sink
# ---------------------------------------------------------------------------
def test_a_local_export_writes_json_lines_under_the_data_directory(
    log: MemoryAuditLog, tmp_path: Path
) -> None:
    sink = LocalAuditExportSink(tmp_path)
    assert isinstance(sink, AuditExportSink)
    now = T0 + timedelta(hours=1)
    result = export_events(log, sink, clock=lambda: now)
    target = tmp_path / EXPORT_DIRECTORY / result.key
    body = target.read_bytes()
    assert result.key == "20260901T110000000000Z.jsonl"
    assert result.location == f"audit/exports/{result.key}"
    assert result.event_count == 3
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert result.retain_until is None
    assert [json.loads(line)["object_id"] for line in body.decode().splitlines()] == ["r-0", "r-1", "r-2"]


def test_an_export_window_is_closed_at_its_start(log: MemoryAuditLog, tmp_path: Path) -> None:
    result = export_events(log, LocalAuditExportSink(tmp_path), AuditQuery(since=T0 + timedelta(minutes=1)))
    assert result.event_count == 2
    result = export_events(
        log, LocalAuditExportSink(tmp_path / "b"), clock=lambda: T0 + timedelta(minutes=1, seconds=30)
    )
    assert result.event_count == 2  # `until` defaults to the export's own instant


def test_a_local_export_never_overwrites(log: MemoryAuditLog, tmp_path: Path) -> None:
    sink = LocalAuditExportSink(tmp_path)
    export_events(log, sink, clock=lambda: T0)
    with pytest.raises(FileExistsError):
        export_events(log, sink, clock=lambda: T0)


# ---------------------------------------------------------------------------
# S3 Object Lock (DEC-715)
# ---------------------------------------------------------------------------
def test_an_s3_export_is_locked_in_compliance_mode_until_the_retention_date(
    log: MemoryAuditLog, s3: Any
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    sink = S3AuditExportSink(s3, bucket=BUCKET, prefix="audit/", retention_days=30, clock=lambda: now)
    result = export_events(log, sink, clock=lambda: now)
    assert result.key.startswith("audit/") and result.key.endswith(".jsonl")
    assert result.location == f"s3://{BUCKET}/{result.key}"
    assert result.retain_until == now + timedelta(days=30)

    # Read back from the object itself. `HeadObject` rather than `GetObjectRetention`, which moto
    # 5.2.3 answers with a 500; the two report the same two fields on real S3.
    head = s3.head_object(Bucket=BUCKET, Key=result.key)
    assert head["ObjectLockMode"] == "COMPLIANCE"
    assert head["ObjectLockRetainUntilDate"].astimezone(UTC) == now + timedelta(days=30)
    body = s3.get_object(Bucket=BUCKET, Key=result.key)["Body"].read()
    assert hashlib.sha256(body).hexdigest() == result.sha256
    assert len(body.splitlines()) == result.event_count == 3


def test_a_locked_export_cannot_be_deleted_before_its_date(log: MemoryAuditLog, s3: Any) -> None:
    from botocore.exceptions import ClientError

    sink = S3AuditExportSink(s3, bucket=BUCKET, prefix="audit", retention_days=1)
    result = export_events(log, sink)
    version = s3.head_object(Bucket=BUCKET, Key=result.key)["VersionId"]
    with pytest.raises(ClientError):
        s3.delete_object(Bucket=BUCKET, Key=result.key, VersionId=version)


def test_a_bucket_without_object_lock_fails_the_export_loudly(log: MemoryAuditLog, s3: Any) -> None:
    from botocore.exceptions import ClientError

    s3.create_bucket(Bucket="plain-bucket")
    sink = S3AuditExportSink(s3, bucket="plain-bucket", prefix="audit", retention_days=1)
    with pytest.raises(ClientError):
        export_events(log, sink)
    assert "Contents" not in s3.list_objects_v2(Bucket="plain-bucket")


def test_export_sink_for_picks_s3_only_when_a_bucket_is_configured(tmp_path: Path, s3: Any) -> None:
    local = export_sink_for(Settings(data_dir=tmp_path), None)
    assert isinstance(local, LocalAuditExportSink)
    remote = export_sink_for(
        Settings(audit_export_bucket=BUCKET, audit_retention_days=7, aws_region=REGION),
        tmp_path,
        s3_client=s3,
    )
    assert isinstance(remote, S3AuditExportSink)
    result = remote.write("x.jsonl", b"{}\n")
    assert result.key == "audit/x.jsonl"
    assert s3.head_object(Bucket=BUCKET, Key="audit/x.jsonl")["ObjectLockMode"] == "COMPLIANCE"
