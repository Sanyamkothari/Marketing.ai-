"""Alerts: the table, the log sink, the SNS sink on moto, and the no-data-values rule (DEC-770)."""

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

from engine.platform_db import sqlite_engine
from engine.scheduling.alerts import (
    AlertError,
    AlertKind,
    AlertQuery,
    AlertStore,
    LogAlertSink,
    SnsAlertSink,
    build_alert_sink,
    new_alert,
    sns_message,
    sns_subject,
)
from engine.settings import Settings

REGION = "ap-south-1"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> AlertStore:
    return AlertStore(sqlite_engine(tmp_path / "platform.db"))


def failed_job(**changes: Any) -> Any:
    values: dict[str, Any] = {
        "use_case_id": "telco-churn",
        "client_id": "c_demo_1",
        "schedule_id": "sch_1",
        "run_id": "r_20260901_abcd0123",
        "message": "The scheduled scoring for telco-churn did not finish (reason: CHAMPION_NOT_FOUND).",
        "now": T0,
    }
    values.update(changes)
    return new_alert(AlertKind.SCHEDULED_JOB_FAILED, **values)


def test_the_severity_follows_the_kind() -> None:
    assert failed_job().severity == "critical"
    assert new_alert(AlertKind.DRIFT_ABOVE_THRESHOLD, use_case_id="u", message="m").severity == "warning"


def test_the_log_sink_persists_and_logs_ids_only(store: AlertStore, caplog: pytest.LogCaptureFixture) -> None:
    alert = failed_job()
    with caplog.at_level(logging.WARNING, logger="engine.scheduling.alerts"):
        stored = LogAlertSink(store).raise_alert(alert)
    assert store.get(stored.alert_id) == alert
    (record,) = [line for line in caplog.records if "alert.raised" in line.getMessage()]
    line = record.getMessage()
    assert alert.alert_id in line and "scheduled_job_failed" in line
    assert alert.message not in line, "the log carries ids, not the message"


def test_queries_filter_and_order_newest_first(store: AlertStore) -> None:
    sink = LogAlertSink(store)
    older = sink.raise_alert(failed_job(now=T0))
    newer = sink.raise_alert(failed_job(now=T0 + timedelta(hours=1), client_id="c_other_1"))
    drift = sink.raise_alert(
        new_alert(AlertKind.DRIFT_ABOVE_THRESHOLD, use_case_id="fault-prediction", message="m")
    )
    assert [a.alert_id for a in store.query(AlertQuery(use_case_id="telco-churn"))] == [
        newer.alert_id,
        older.alert_id,
    ]
    assert store.query(AlertQuery(client_id="c_other_1")) == (newer,)
    assert store.query(AlertQuery(kind=AlertKind.DRIFT_ABOVE_THRESHOLD)) == (drift,)


def test_acknowledge_is_once_and_idempotent(store: AlertStore) -> None:
    alert = LogAlertSink(store).raise_alert(failed_job())
    first = store.acknowledge(alert.alert_id, by="u_ops", now=T0 + timedelta(hours=2))
    again = store.acknowledge(alert.alert_id, by="u_other", now=T0 + timedelta(hours=3))
    assert first.acknowledged_by == "u_ops" == again.acknowledged_by
    assert again.acknowledged_at == T0 + timedelta(hours=2)
    assert store.query(AlertQuery(unacknowledged_only=True)) == ()
    with pytest.raises(AlertError) as caught:
        store.acknowledge("al_nope", by="u")
    assert caught.value.code == "ALERT_NOT_FOUND"


def test_the_email_names_ids_and_fits_the_sns_subject_limit() -> None:
    alert = failed_job(use_case_id="u" * 150)
    assert len(sns_subject(alert)) <= 100
    body = sns_message(failed_job())
    assert body.startswith("The scheduled scoring")
    assert "Run: r_20260901_abcd0123" in body
    assert "Schedule: sch_1" in body


@pytest.fixture
def sns() -> Iterator[tuple[Any, str, Any, str]]:
    """A topic with an SQS queue subscribed, so a test can read what was published."""
    with mock_aws():
        client = boto3.client("sns", region_name=REGION)
        topic = client.create_topic(Name="marketing-ai-dev-alerts")["TopicArn"]
        sqs = boto3.client("sqs", region_name=REGION)
        queue_url = sqs.create_queue(QueueName="alerts-inbox")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        client.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn)
        yield client, topic, sqs, queue_url


def test_the_sns_sink_persists_then_publishes(store: AlertStore, sns: tuple[Any, str, Any, str]) -> None:
    client, topic, sqs, queue_url = sns
    alert = SnsAlertSink(store, topic_arn=topic, region_name=REGION, client=client).raise_alert(failed_job())
    assert store.get(alert.alert_id) == alert
    (message,) = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)["Messages"]
    envelope = json.loads(message["Body"])
    assert envelope["Subject"] == "[critical] Scheduled job failed - telco-churn"
    assert envelope["Message"] == sns_message(alert)
    assert envelope["MessageAttributes"]["kind"]["Value"] == "scheduled_job_failed"


def test_a_failed_publish_keeps_the_alert_and_does_not_raise(
    store: AlertStore, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        def publish(self, **kwargs: Any) -> None:
            raise RuntimeError("relay down: secret detail")

    with caplog.at_level(logging.WARNING, logger="engine.scheduling.alerts"):
        alert = SnsAlertSink(store, topic_arn="arn:x", region_name=REGION, client=Broken()).raise_alert(
            failed_job()
        )
    assert store.get(alert.alert_id) == alert
    assert any(
        "alert.publish" in line.getMessage() and "RuntimeError" in line.getMessage()
        for line in caplog.records
    )
    assert not any("secret detail" in line.getMessage() for line in caplog.records)


def test_build_alert_sink_picks_the_backend(tmp_path: Path, sns: tuple[Any, str, Any, str]) -> None:
    client, topic, _, _ = sns
    engine = sqlite_engine(tmp_path / "platform.db")
    assert isinstance(build_alert_sink(Settings(), engine=engine), LogAlertSink)
    built = build_alert_sink(
        Settings(alert_backend="sns", alert_sns_topic_arn=topic, aws_region=REGION),
        engine=engine,
        sns_client=client,
    )
    assert isinstance(built, SnsAlertSink)
