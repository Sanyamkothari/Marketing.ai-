"""Audit exports: a window of the audit log as JSON lines, and the viewer's CSV (Phase 4b M47).

Plan M47 asks for two things that pull in different directions: an audit trail a production
deployment cannot quietly rewrite, and one a laptop can read without an AWS account. So an export
is one byte string - JSON lines, one `AuditEvent` per line, oldest first - written to one of two
places behind `AuditExportSink`:

* `LocalAuditExportSink`: `<data_dir>/audit/exports/<timestamp>.jsonl`. A copy for a person to
  read, with no immutability claim at all.
* `S3AuditExportSink`, when `settings.audit_export_bucket` is set: `PutObject` with **Object Lock in
  COMPLIANCE mode** and `RetainUntilDate = now + settings.audit_retention_days` (DEC-715).
  Compliance mode, not governance: in governance mode an account holding
  `s3:BypassGovernanceRetention` can delete the object, which is exactly the person an audit trail
  must be safe from; in compliance mode nobody can, the root user included, until the date passes.
  The bucket must have been created with Object Lock enabled (it cannot be switched on for most
  existing buckets), so a bucket without it fails the export loudly rather than storing a copy that
  only looks protected. `ContentMD5` is sent because S3 requires an integrity header on every
  Object Lock write, and the SHA-256 of the body is returned so the caller can record it in the
  audit log - the export is itself an audited event, and its hash ties the event to the file.

The export is a *copy*. The table stays the record, append-only in the database (DEC-714); the
export is what survives a compromise of the database.

**Nobody has to remember to export (DEC-726).** `export_due` is what the hourly housekeeping
firing runs (`scripts/fire_schedule.py --sweep --export-audit`, an EventBridge schedule the API
creates at startup; the local scheduler's tick on a laptop): when the last *scheduled* export is a
day old it writes the window from that export's end up to now, so consecutive files tile the log
with no gap and no overlap, and appends one `audit.export.scheduled` event as `system:audit-export`
carrying the window and the file's SHA-256 - the next export starts where that event says the last
one ended. An export more than two periods late is logged at ERROR, which the deployment's error
alarm counts. A person with the database owner's rights can still rewrite rows that no export has
covered yet; with this schedule that window is at most a day, not "until an Admin thinks of it".

`render_csv` is the viewer's download. Every cell that a spreadsheet would read as a formula
(`=`, `+`, `-`, `@`, tab, carriage return) is prefixed with `'`: an object id is whatever a path
parameter said, and a CSV that runs a formula when an Admin opens it would turn the audit viewer
into an attack on its reader.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from engine.access.roles import Principal, Role
from engine.audit.events import AuditEvent, AuditLog, AuditQuery
from engine.settings import Settings
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "AUDIT_EXPORT_JOB",
    "CSV_COLUMNS",
    "EXPORT_DIRECTORY",
    "SCHEDULED_EXPORT_ACTION",
    "SCHEDULED_EXPORT_EVERY",
    "AuditExportResult",
    "AuditExportSink",
    "LocalAuditExportSink",
    "S3AuditExportSink",
    "collect_events",
    "export_due",
    "export_events",
    "export_sink_for",
    "render_csv",
    "render_jsonl",
]

_LOGGER = get_logger(__name__)

SCHEDULED_EXPORT_ACTION: Final[str] = "audit.export.scheduled"
"""The action of the event each scheduled export appends; its `window_end` is where the next starts."""

SCHEDULED_EXPORT_EVERY: Final[timedelta] = timedelta(days=1)
"""How often the scheduled export writes a window; twice this without one is logged at ERROR."""

AUDIT_EXPORT_JOB: Final[Principal] = Principal(
    user_id="system:audit-export",
    username="audit export job",
    roles=frozenset({Role.ADMIN}),
    kind="system",
)
"""Who the scheduled export acts as in the trail: an Admin task (plan M47), run by the system."""

EXPORT_DIRECTORY: Final[Path] = Path("audit") / "exports"
"""Where local exports go, relative to the data directory."""

CSV_COLUMNS: Final[tuple[str, ...]] = (
    "occurred_at",
    "actor_id",
    "actor_kind",
    "action",
    "object_type",
    "object_id",
    "outcome",
    "request_id",
    "before_hash",
    "after_hash",
    "details",
    "event_id",
)
"""The viewer's CSV header, in the order an auditor reads a row."""

_PAGE: Final[int] = 10000
"""`AuditQuery.limit`'s ceiling; an export pages through the window at this size."""

_FORMULA_PREFIXES: Final[tuple[str, ...]] = ("=", "+", "-", "@", "\t", "\r")


class AuditExportResult(BaseModel):
    """What an export wrote, and the facts an auditor needs to verify it later."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location: str = Field(
        description="`audit/exports/<name>` inside the data directory, or `s3://bucket/key`."
    )
    key: str = Field(description="The file name or object key, relative to its sink.")
    event_count: int = Field(description="How many events the file holds.")
    sha256: str = Field(description="SHA-256 of the file's bytes.")
    retain_until: datetime | None = Field(
        default=None, description="Object Lock retention date; null for a local copy, which has none."
    )


@runtime_checkable
class AuditExportSink(Protocol):
    """Where an export's bytes go."""

    def write(self, name: str, body: bytes) -> AuditExportResult: ...


def render_jsonl(events: Iterable[AuditEvent]) -> bytes:
    """One JSON object per line, keys sorted, newline-terminated; empty bytes for no events."""
    lines = [
        json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) for event in events
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def _safe_cell(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def render_csv(events: Iterable[AuditEvent]) -> str:
    """The viewer's CSV: `CSV_COLUMNS`, formula-looking cells neutralised."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for event in events:
        record = event.model_dump(mode="json")
        record["details"] = json.dumps(event.details, sort_keys=True, separators=(",", ":"))
        writer.writerow([_safe_cell(record[column]) for column in CSV_COLUMNS])
    return buffer.getvalue()


def collect_events(audit_log: AuditLog, window: AuditQuery) -> list[AuditEvent]:
    """Every event matching `window`'s filters, oldest first, paging past `AuditQuery.limit`.

    `window.limit` and `window.offset` are ignored: an export is the whole window or it is not an
    export. Paging is by offset over a newest-first order, so an event appended *during* the export
    shifts later pages by one; `until` fixed at the start removes that, which is why `export_events`
    always sets it.
    """
    collected: list[AuditEvent] = []
    offset = 0
    while True:
        page = audit_log.query(window.model_copy(update={"limit": _PAGE, "offset": offset}))
        collected.extend(page)
        if len(page) < _PAGE:
            break
        offset += _PAGE
    collected.reverse()
    return collected


def export_events(
    audit_log: AuditLog,
    sink: AuditExportSink,
    window: AuditQuery | None = None,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> AuditExportResult:
    """Write the events of `window` (all of them when None) to `sink` as JSON lines.

    `until` defaults to now, so the file describes a closed interval even while requests keep
    being audited, and the name is that instant, so two exports never overwrite each other.
    """
    now = clock()
    query = window or AuditQuery()
    if query.until is None:
        query = query.model_copy(update={"until": now})
    body = render_jsonl(collect_events(audit_log, query))
    name = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}.jsonl"
    return sink.write(name, body)


def _last_window_end(audit_log: AuditLog) -> datetime | None:
    """Where the newest successful scheduled export ended, or None when there has been none."""
    events = audit_log.query(AuditQuery(action=SCHEDULED_EXPORT_ACTION, outcome="success", limit=1))
    if not events:
        return None
    value = events[0].details.get("window_end")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def export_due(
    audit_log: AuditLog,
    sink: AuditExportSink,
    *,
    now: datetime | None = None,
    every: timedelta = SCHEDULED_EXPORT_EVERY,
) -> AuditExportResult | None:
    """Write the next window of the log when the last scheduled export is `every` old (DEC-726).

    The window is `[end of the last scheduled export, now)` - the whole log before the first one -
    and one `audit.export.scheduled` event records it. None when nothing is due yet. A write that
    fails is recorded as a `failed` event and raised, so the caller's exit status shows it.
    """
    moment = now or utc_now()
    since = _last_window_end(audit_log)
    if since is not None and moment - since < every:
        return None
    if since is not None and moment - since >= 2 * every:
        _LOGGER.error("audit export overdue: the last scheduled export ended %s ago", moment - since)
    details: dict[str, str | int | float | bool | None] = {
        "window_start": None if since is None else since.isoformat(),
        "window_end": moment.isoformat(),
    }
    try:
        result = export_events(audit_log, sink, AuditQuery(since=since, until=moment), clock=lambda: moment)
    except Exception as exc:
        details["reason_code"] = type(exc).__name__[:64]
        audit_log.append(_export_event(moment, outcome="failed", details=details))
        raise
    details.update({"export_key": result.key, "count": result.event_count})
    audit_log.append(_export_event(moment, outcome="success", details=details, result=result))
    _LOGGER.info("audit export written key=%s events=%d", result.key, result.event_count)
    return result


def _export_event(
    moment: datetime,
    *,
    outcome: str,
    details: dict[str, str | int | float | bool | None],
    result: AuditExportResult | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=uuid.uuid4().hex,
        occurred_at=moment,
        actor_id=AUDIT_EXPORT_JOB.user_id,
        actor_kind=AUDIT_EXPORT_JOB.kind,
        action=SCHEDULED_EXPORT_ACTION,
        object_type="audit_export",
        object_id=None if result is None else result.key,
        after_hash=None if result is None else result.sha256,
        outcome=outcome,
        details=details,
    )


class LocalAuditExportSink:
    """`<root>/audit/exports/<name>`: a readable copy on the local disk, with no retention claim."""

    def __init__(self, root: Path) -> None:
        self._directory = Path(root) / EXPORT_DIRECTORY

    def write(self, name: str, body: bytes) -> AuditExportResult:
        """Write the file; refuses to overwrite one that exists."""
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / Path(name).name
        with target.open("xb") as handle:
            handle.write(body)
        return AuditExportResult(
            location=f"{EXPORT_DIRECTORY.as_posix()}/{target.name}",
            key=target.name,
            event_count=body.count(b"\n"),
            sha256=hashlib.sha256(body).hexdigest(),
        )


class S3AuditExportSink:
    """An Object Lock (COMPLIANCE) write to the audit bucket (DEC-715)."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        prefix: str,
        retention_days: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._retention = timedelta(days=retention_days)
        self._clock = clock

    def write(self, name: str, body: bytes) -> AuditExportResult:
        """`PutObject` with `ObjectLockMode=COMPLIANCE`, a retain-until date and `ContentMD5`."""
        key = f"{self._prefix}/{name}" if self._prefix else name
        retain_until = self._clock() + self._retention
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
            ContentMD5=base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode("ascii"),
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
        )
        return AuditExportResult(
            location=f"s3://{self._bucket}/{key}",
            key=key,
            event_count=body.count(b"\n"),
            sha256=hashlib.sha256(body).hexdigest(),
            retain_until=retain_until,
        )


def export_sink_for(settings: Settings, data_dir: Path | None, *, s3_client: Any = None) -> AuditExportSink:
    """S3 with Object Lock when `audit_export_bucket` is set, otherwise the local data directory.

    `s3_client` lets a test hand in a moto client; otherwise one is built for `aws_region`, with the
    import local so a laptop never loads boto3 (DEC-306).
    """
    if settings.audit_export_bucket:
        client = s3_client
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=settings.aws_region)
        return S3AuditExportSink(
            client,
            bucket=settings.audit_export_bucket,
            prefix=settings.audit_export_prefix,
            retention_days=settings.audit_retention_days,
        )
    return LocalAuditExportSink(data_dir if data_dir is not None else settings.data_dir)
