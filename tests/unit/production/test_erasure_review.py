"""Erasure review fixes: the S3 mirror, old object versions, lifecycle ownership, models older than
retention, and runs still in progress (DEC-707, DEC-708, DEC-709, DEC-728). S3 is moto, never real AWS."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from engine.access.roles import LOCAL_OPERATOR
from engine.aws.s3_storage import S3Storage
from engine.config import RunMode
from engine.contracts import RunState
from engine.platform_db import sqlite_engine
from engine.privacy.config import load_privacy_config
from engine.privacy.erasure import erase, erasure_request, find_principal
from engine.privacy.errors import PrivacyError
from engine.privacy.lifecycle import apply_lifecycle, build_lifecycle_rules
from engine.privacy.retention import RETENTION_JOB, apply_retention, plan_retention
from engine.storage import LocalStorage
from tests.unit.production.privacy_support import (
    customers,
    write_row_explanations,
    write_run,
    write_scores,
    write_upload,
)

BUCKET = "test-artefacts"
REGION = "ap-south-1"
SENTINEL = "ZQX-SENT-9"
SALT = "local"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        yield client


def _parquet(keys: list[str]) -> bytes:
    buffer = io.BytesIO()
    pd.DataFrame({"customer_id": keys, "v": list(range(len(keys)))}).to_parquet(buffer, index=False)
    return buffer.getvalue()


# --- DEC-707: the S3Storage mirror -----------------------------------------------------------------
def test_an_erased_object_is_not_served_again_from_this_processs_mirror(s3: Any, tmp_path: Path) -> None:
    storage = S3Storage(BUCKET, client=s3, workspace=tmp_path / "mirror")
    storage.write_bytes("datasets/ds_1/dataset.parquet", _parquet(["C-1", SENTINEL, "C-3"]))
    storage.write_text(
        "datasets/ds_1/dataset_manifest.json",
        '{"dataset_id":"ds_1","primary_key":["customer_id"],"built_at":"2026-09-01T00:00:00+00:00"}',
    )
    before = pd.read_parquet(storage.local_path("datasets/ds_1/dataset.parquet"))
    assert SENTINEL in before["customer_id"].tolist()

    outcome = erase(
        storage, SENTINEL, engine=sqlite_engine(tmp_path / "platform.db"), principal=LOCAL_OPERATOR, salt=SALT
    )
    assert outcome.status == "completed"
    after = pd.read_parquet(storage.local_path("datasets/ds_1/dataset.parquet"))
    assert after["customer_id"].tolist() == ["C-1", "C-3"]

    storage.delete("datasets/ds_1/dataset.parquet")
    assert not storage.local_path("datasets/ds_1/dataset.parquet").exists()


# --- DEC-708: old versions and lifecycle ownership ------------------------------------------------
def test_erasure_leaves_no_old_version_of_a_row_level_file(s3: Any, tmp_path: Path) -> None:
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    storage = S3Storage(BUCKET, client=s3, workspace=tmp_path / "mirror")
    write_run(storage, "r_1", mode=RunMode.SCORE, created_at=datetime(2026, 9, 1, tzinfo=UTC))  # type: ignore[arg-type]
    write_scores(storage, "r_1", ["C-1", SENTINEL])  # type: ignore[arg-type]

    erase(
        storage, SENTINEL, engine=sqlite_engine(tmp_path / "platform.db"), principal=LOCAL_OPERATOR, salt=SALT
    )
    for name in ("scores.csv", "scores.parquet"):
        versions = s3.list_object_versions(Bucket=BUCKET, Prefix=f"runs/r_1/{name}").get("Versions", [])
        assert len(versions) == 1, "only the rewritten, current version is left"
        body = s3.get_object(Bucket=BUCKET, Key=f"runs/r_1/{name}")["Body"].read()
        assert SENTINEL.encode() not in body


def test_one_client_prefixs_rules_leave_anothers_alone(s3: Any, config_root: Path) -> None:
    prefix = load_privacy_config(config_root).retention.lifecycle.rule_id_prefix
    first = build_lifecycle_rules(config_root, s3_prefix="client-a")
    apply_lifecycle(s3, BUCKET, first, rule_id_prefix=prefix)
    second = build_lifecycle_rules(config_root, s3_prefix="client-b")
    result = apply_lifecycle(s3, BUCKET, second, rule_id_prefix=prefix)
    assert result.replaced == ()
    assert set(result.preserved) == {rule.rule_id for rule in first}
    current = {rule["ID"] for rule in s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]}
    assert current == {rule.rule_id for rule in (*first, *second)}
    again = apply_lifecycle(s3, BUCKET, second, rule_id_prefix=prefix)
    assert set(again.replaced) == {rule.rule_id for rule in second}


# --- DEC-709: models older than retention -----------------------------------------------------------
def test_a_model_whose_training_data_retention_deleted_is_still_flagged(
    tmp_path: Path, config_root: Path
) -> None:
    storage = LocalStorage(tmp_path / "data")
    trained = datetime(2026, 1, 1, tzinfo=UTC)
    keys = ["C-1", SENTINEL, "C-3"]
    write_upload(storage, "u_1", customers(keys), created_at=trained)
    write_run(
        storage, "r_train", mode=RunMode.TRAIN, created_at=trained, upload_id="u_1", model_version_id="m_1"
    )
    write_row_explanations(storage, "r_train", keys)
    assert find_principal(storage, SENTINEL).models == ("m_1",)
    apply_retention(
        plan_retention(storage, config_root, trained + timedelta(days=100)), storage, None, RETENTION_JOB
    )

    engine = sqlite_engine(tmp_path / "platform.db")
    outcome = erase(storage, SENTINEL, engine=engine, principal=LOCAL_OPERATOR, salt=SALT)
    assert outcome.models_flagged == ("m_1",)
    assert outcome.models_exposure_unknown == ("m_1",)
    assert outcome.status == "completed_with_exceptions", "nobody could check what the model learned from"


# --- DEC-728: runs still in progress ------------------------------------------------------------------
def test_erasure_waits_for_a_run_still_reading_the_persons_data(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    now = datetime(2026, 9, 1, tzinfo=UTC)
    write_upload(storage, "u_1", customers(["C-1", SENTINEL]), created_at=now)
    write_run(storage, "r_busy", mode=RunMode.SCORE, created_at=now, upload_id="u_1", state=RunState.RUNNING)
    before = storage.read_bytes("uploads/u_1/source.csv")
    engine = sqlite_engine(tmp_path / "platform.db")
    with pytest.raises(PrivacyError) as caught:
        erase(storage, SENTINEL, engine=engine, principal=LOCAL_OPERATOR, salt=SALT, request_id="er_busy")
    assert caught.value.code == "ERASURE_RUNS_IN_PROGRESS"
    assert storage.read_bytes("uploads/u_1/source.csv") == before, "nothing was changed"
    stored = erasure_request(engine, "er_busy")
    assert stored is not None and (stored.status, stored.error_code) == ("failed", "ERASURE_RUNS_IN_PROGRESS")
