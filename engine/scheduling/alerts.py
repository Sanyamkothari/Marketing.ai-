"""Alerts: the record, the `alert` table, and where an alert is sent (M49).

Four things raise an alert, and only four: the latest scored data has drifted beyond
`monitoring.drift_psi_threshold` (`drift_above_threshold`), real-world performance has dropped by
more than `monitoring.performance_alert_drop_pct` against the model's test score (`performance_drop`),
a scheduled job failed (`scheduled_job_failed`), and a due slot passed with nothing there to fire it
(`schedule_missed`).

**An alert is written for a person reading an email (DEC-770).** `message` is business language -
"The scheduled scoring for <use case> did not finish" - and it carries identifiers (the schedule, the
run, the model version) and counts, never a data value: no customer id, no feature value, no score.
The ids are enough for someone with access to open the run in the product, and an email is the
least controlled place this system ever writes to.

**Every alert is persisted before it is sent anywhere.** `LogAlertSink` logs one line (kind and ids,
at WARNING) and writes the `alert` row; `SnsAlertSink` does exactly that and *then* publishes to
`Settings.alert_sns_topic_arn`, whose email subscription is plan prerequisite P3. A publish that fails
is logged by its exception class and does not fail the caller: the alert is already in the table,
where the product's monitoring page shows it, and a scheduled job must not be reported as failed
because the mail relay was. `acknowledged_at/by` are set by a person, through the API, once.

Only the SNS sink touches AWS, and boto3 is imported inside it (DEC-306): a laptop never loads it.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime, Index, Text
from sqlalchemy.engine import Engine
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from engine.platform_db import create_tables
from engine.registry import to_utc
from engine.settings import Settings, SettingsError
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

__all__ = [
    "ALERT_TABLE",
    "SEVERITY_FOR",
    "Alert",
    "AlertError",
    "AlertKind",
    "AlertQuery",
    "AlertRow",
    "AlertSink",
    "AlertStore",
    "LogAlertSink",
    "Severity",
    "SnsAlertSink",
    "build_alert_sink",
    "create_alert_table",
    "new_alert",
    "new_alert_id",
    "sns_message",
    "sns_subject",
]

_LOGGER = get_logger(__name__)

ALERT_TABLE: Final[str] = "alert"

SNS_SUBJECT_LIMIT: Final[int] = 100
"""SNS refuses a `Subject` longer than 100 characters."""


class AlertKind(StrEnum):
    DRIFT_ABOVE_THRESHOLD = "drift_above_threshold"
    PERFORMANCE_DROP = "performance_drop"
    SCHEDULED_JOB_FAILED = "scheduled_job_failed"
    SCHEDULE_MISSED = "schedule_missed"


Severity = Literal["info", "warning", "critical"]

SEVERITY_FOR: Final[dict[AlertKind, Severity]] = {
    AlertKind.DRIFT_ABOVE_THRESHOLD: "warning",
    AlertKind.PERFORMANCE_DROP: "warning",
    AlertKind.SCHEDULED_JOB_FAILED: "critical",
    AlertKind.SCHEDULE_MISSED: "warning",
}
"""A failed job is critical - something the client expected did not happen; the rest are warnings -
something to look at, and the product kept working."""

_TITLES: Final[dict[AlertKind, str]] = {
    AlertKind.DRIFT_ABOVE_THRESHOLD: "Data drift above threshold",
    AlertKind.PERFORMANCE_DROP: "Model performance dropped",
    AlertKind.SCHEDULED_JOB_FAILED: "Scheduled job failed",
    AlertKind.SCHEDULE_MISSED: "Scheduled run missed",
}


class AlertError(Exception):
    """`code` is ALERT_NOT_FOUND."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Alert(BaseModel):
    """One alert. Identifiers and business language only - never a data value (DEC-770)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: str = Field(description="Unique id of the alert.")
    kind: AlertKind = Field(description="What happened.")
    severity: Severity = Field(description="`info`, `warning` or `critical`.")
    client_id: str | None = Field(default=None, description="Client it concerns, if any.")
    use_case_id: str = Field(description="Use case it concerns.")
    schedule_id: str | None = Field(default=None, description="Schedule it concerns, if any.")
    run_id: str | None = Field(default=None, description="Run it concerns, if any.")
    model_id: str | None = Field(default=None, description="Model version it concerns, if any.")
    message: str = Field(description="What happened and what to do, in business language.")
    created_at: datetime = Field(description="UTC time it was raised.")
    acknowledged_at: datetime | None = Field(default=None, description="UTC time a person acknowledged it.")
    acknowledged_by: str | None = Field(default=None, description="`Principal.user_id` who acknowledged it.")


class AlertQuery(BaseModel):
    """Filters for the alert history. Every field is optional; all given ones must match."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str | None = None
    use_case_id: str | None = None
    kind: AlertKind | None = None
    unacknowledged_only: bool = False
    limit: int = Field(default=100, ge=1, le=1000)


def new_alert_id(now: datetime | None = None) -> str:
    """`al_<yyyymmdd>_<12 hex>`."""
    moment = now or utc_now()
    return f"al_{moment.strftime('%Y%m%d')}_{secrets.token_hex(6)}"


def new_alert(
    kind: AlertKind,
    *,
    use_case_id: str,
    message: str,
    client_id: str | None = None,
    schedule_id: str | None = None,
    run_id: str | None = None,
    model_id: str | None = None,
    now: datetime | None = None,
) -> Alert:
    """An `Alert` with a fresh id, the kind's severity and the current time."""
    moment = now or utc_now()
    return Alert(
        alert_id=new_alert_id(moment),
        kind=kind,
        severity=SEVERITY_FOR[kind],
        client_id=client_id,
        use_case_id=use_case_id,
        schedule_id=schedule_id,
        run_id=run_id,
        model_id=model_id,
        message=message,
        created_at=moment,
    )


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
class AlertRow(SQLModel, table=True):
    """`alert`: one row per alert, newest read first."""

    __tablename__ = ALERT_TABLE
    __table_args__: ClassVar[Any] = (
        Index("ix_alert_scope", "client_id", "use_case_id"),
        Index("ix_alert_created_at", "created_at"),
    )

    alert_id: str = SQLField(primary_key=True)
    kind: str
    severity: str
    client_id: str | None = None
    use_case_id: str
    schedule_id: str | None = None
    run_id: str | None = None
    model_id: str | None = None
    message: str = SQLField(sa_column=Column("message", Text(), nullable=False))
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    acknowledged_at: datetime | None = SQLField(
        default=None, sa_column=Column("acknowledged_at", DateTime(timezone=True), nullable=True)
    )
    acknowledged_by: str | None = None


def create_alert_table(engine: Engine) -> None:
    """Create `alert` if missing (SQLite only; Alembic owns Postgres)."""
    create_tables(engine, (ALERT_TABLE,))


def _row(alert: Alert) -> AlertRow:
    return AlertRow(
        alert_id=alert.alert_id,
        kind=alert.kind.value,
        severity=alert.severity,
        client_id=alert.client_id,
        use_case_id=alert.use_case_id,
        schedule_id=alert.schedule_id,
        run_id=alert.run_id,
        model_id=alert.model_id,
        message=alert.message,
        created_at=to_utc(alert.created_at),
        acknowledged_at=None if alert.acknowledged_at is None else to_utc(alert.acknowledged_at),
        acknowledged_by=alert.acknowledged_by,
    )


def _alert(row: AlertRow) -> Alert:
    severity: Severity = (
        "critical" if row.severity == "critical" else "info" if row.severity == "info" else "warning"
    )
    return Alert(
        alert_id=row.alert_id,
        kind=AlertKind(row.kind),
        severity=severity,
        client_id=row.client_id,
        use_case_id=row.use_case_id,
        schedule_id=row.schedule_id,
        run_id=row.run_id,
        model_id=row.model_id,
        message=row.message,
        created_at=to_utc(row.created_at),
        acknowledged_at=None if row.acknowledged_at is None else to_utc(row.acknowledged_at),
        acknowledged_by=row.acknowledged_by,
    )


class AlertStore:
    """The alert history on the platform database."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        create_alert_table(engine)

    def add(self, alert: Alert) -> Alert:
        with Session(self._engine) as session:
            session.add(_row(alert))
            session.commit()
        return alert

    def get(self, alert_id: str) -> Alert:
        with Session(self._engine) as session:
            row = session.get(AlertRow, alert_id)
            if row is None:
                raise AlertError("ALERT_NOT_FOUND", f"No alert with id {alert_id!r}.")
            return _alert(row)

    def query(self, query: AlertQuery) -> tuple[Alert, ...]:
        """Matching alerts, newest first."""
        statement = select(AlertRow)
        if query.client_id is not None:
            statement = statement.where(col(AlertRow.client_id) == query.client_id)
        if query.use_case_id is not None:
            statement = statement.where(col(AlertRow.use_case_id) == query.use_case_id)
        if query.kind is not None:
            statement = statement.where(col(AlertRow.kind) == query.kind.value)
        if query.unacknowledged_only:
            statement = statement.where(col(AlertRow.acknowledged_at).is_(None))
        statement = statement.order_by(col(AlertRow.created_at).desc(), col(AlertRow.alert_id).desc())
        with Session(self._engine) as session:
            return tuple(_alert(row) for row in session.exec(statement.limit(query.limit)).all())

    def acknowledge(self, alert_id: str, *, by: str, now: datetime | None = None) -> Alert:
        """Mark an alert acknowledged. Idempotent: a second acknowledgement keeps the first's time and name."""
        with Session(self._engine) as session:
            row = session.get(AlertRow, alert_id)
            if row is None:
                raise AlertError("ALERT_NOT_FOUND", f"No alert with id {alert_id!r}.")
            if row.acknowledged_at is None:
                row.acknowledged_at = to_utc(now or utc_now())
                row.acknowledged_by = by
                session.add(row)
                session.commit()
                session.refresh(row)
            return _alert(row)


# ---------------------------------------------------------------------------
# The sinks
# ---------------------------------------------------------------------------
@runtime_checkable
class AlertSink(Protocol):
    """Where a raised alert goes. `raise_alert` returns the alert as stored."""

    def raise_alert(self, alert: Alert) -> Alert: ...


class LogAlertSink:
    """Log one line and persist the alert. The whole of `alert_backend=log`."""

    def __init__(self, store: AlertStore) -> None:
        self.store = store

    def raise_alert(self, alert: Alert) -> Alert:
        _LOGGER.warning(
            "alert.raised alert_id=%s kind=%s severity=%s use_case=%s schedule_id=%s run_id=%s model_id=%s",
            alert.alert_id,
            alert.kind.value,
            alert.severity,
            alert.use_case_id,
            alert.schedule_id,
            alert.run_id,
            alert.model_id,
        )
        return self.store.add(alert)


class SnsAlertSink:
    """`LogAlertSink`, then a publish to the alert topic (whose email subscription is P3)."""

    def __init__(
        self, store: AlertStore, *, topic_arn: str, region_name: str | None, client: Any = None
    ) -> None:
        self._log = LogAlertSink(store)
        self._topic_arn = topic_arn
        self._region_name = region_name
        self._client = client

    @property
    def store(self) -> AlertStore:
        return self._log.store

    def _sns(self) -> Any:
        if self._client is None:
            import boto3  # a deliberate local import: a laptop never loads boto3 (DEC-306)

            self._client = boto3.client("sns", region_name=self._region_name)
        return self._client

    def raise_alert(self, alert: Alert) -> Alert:
        stored = self._log.raise_alert(alert)
        try:
            self._sns().publish(
                TopicArn=self._topic_arn,
                Subject=sns_subject(stored),
                Message=sns_message(stored),
                MessageAttributes={
                    "kind": {"DataType": "String", "StringValue": stored.kind.value},
                    "severity": {"DataType": "String", "StringValue": stored.severity},
                },
            )
        except Exception as exc:  # the alert is stored; a mail relay failing must not fail the job
            log_failure(_LOGGER, f"alert.publish alert_id={stored.alert_id}", exc)
        return stored


def sns_subject(alert: Alert) -> str:
    """`[critical] Scheduled job failed - <use case id>`, cut to SNS's 100-character limit."""
    subject = f"[{alert.severity}] {_TITLES[alert.kind]} - {alert.use_case_id}"
    return subject[:SNS_SUBJECT_LIMIT]


def sns_message(alert: Alert) -> str:
    """The email body: the message, then the identifiers someone with access needs to look."""
    lines = [alert.message, ""]
    for label, value in (
        ("Use case", alert.use_case_id),
        ("Client", alert.client_id),
        ("Schedule", alert.schedule_id),
        ("Run", alert.run_id),
        ("Model version", alert.model_id),
        ("Alert", alert.alert_id),
        ("Raised at (UTC)", to_utc(alert.created_at).isoformat()),
    ):
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def build_alert_sink(settings: Settings, *, engine: Engine, sns_client: Any = None) -> AlertSink:
    """The sink `settings.alert_backend` names, persisting to `engine`'s `alert` table."""
    store = AlertStore(engine)
    if settings.alert_backend == "log":
        return LogAlertSink(store)
    if not settings.alert_sns_topic_arn:  # Settings already refuses this; kept for a hand-built object
        raise SettingsError(
            "SETTING_REQUIRED",
            "alert_backend=sns needs alert_sns_topic_arn.",
            env_var="MARKETING_AI_ALERT_SNS_TOPIC_ARN",
        )
    return SnsAlertSink(
        store, topic_arn=settings.alert_sns_topic_arn, region_name=settings.aws_region, client=sns_client
    )
