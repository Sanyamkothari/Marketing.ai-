"""The M52 backup-and-restore drill (`scripts/restore_drill.py`), against moto and never against AWS.

What is pinned: a dry run calls nothing at all (no client is even built); the S3 canary drill
really restores the *first* version after an overwrite and a delete, verifies it by hash and leaves
no version of the canary behind; the version chooser answers "undo the last change" and "as it
was at time T" correctly, including the delete-marker cases; an unversioned bucket is reported as
the finding it is; and the RDS sequence snapshots, restores into a new instance with the source's
network and parameter group, and tears both down unless told to keep them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import restore_drill
from scripts.restore_drill import (
    DRILL_PREFIX,
    DrillError,
    ObjectVersion,
    choose_version,
    object_versions,
    rds_drill,
    s3_canary_drill,
    s3_restore,
)

moto = pytest.importorskip("moto", reason="the drill's AWS paths are tested against moto")

REGION = "ap-south-1"
BUCKET = "drill-test-bucket"


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials, so nothing here could reach a real account even if moto were bypassed."""
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
    import boto3

    with moto.mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield client


class _Exploding:
    """A client that fails the test on any use: a dry run must not touch it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"a dry run called {name}")


def _version(version_id: str, minute: int, *, latest: bool = False, marker: bool = False) -> ObjectVersion:
    return ObjectVersion(version_id, datetime(2026, 10, 1, 9, minute, tzinfo=UTC), latest, marker)


# ---------------------------------------------------------------------------
# Dry runs
# ---------------------------------------------------------------------------
def test_a_dry_run_calls_nothing_and_says_so() -> None:
    for record in (
        s3_canary_drill(_Exploding(), bucket=BUCKET),
        rds_drill(_Exploding(), instance_id="marketing-ai-dev"),
        s3_restore(_Exploding(), bucket=BUCKET, key="runs/r_1/run.json"),
    ):
        assert record.executed is False
        assert record.rto_seconds is None
        assert record.steps and all(
            step.outcome == "planned" and step.seconds is None for step in record.steps
        )


def test_the_command_line_is_a_dry_run_unless_told_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_client(*_args: Any) -> Any:
        raise AssertionError("a dry run built a boto3 client")

    monkeypatch.setattr(restore_drill, "_client", no_client)
    output = tmp_path / "record.json"
    assert restore_drill.main(["rds", "--instance-id", "marketing-ai-dev", "--output", str(output)]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["executed"] is False
    assert [step["call"] for step in written["steps"]][:2] == [
        "rds:DescribeDBInstances",
        "rds:CreateDBSnapshot",
    ]


def test_the_default_record_path_marks_a_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore_drill, "REPORT_DIR", tmp_path)
    path = restore_drill.write_record(s3_canary_drill(None, bucket=BUCKET))
    assert path.parent == tmp_path
    assert path.name.endswith(f"-s3-s3___{BUCKET}-dry-run.json")


# ---------------------------------------------------------------------------
# Choosing the version
# ---------------------------------------------------------------------------
def test_undo_the_last_change_restores_the_previous_version() -> None:
    versions = [_version("v2", 2, latest=True), _version("v1", 1)]
    assert choose_version(versions).version_id == "v1"


def test_undo_a_delete_restores_the_newest_real_version() -> None:
    versions = [_version("dm", 3, latest=True, marker=True), _version("v2", 2), _version("v1", 1)]
    assert choose_version(versions).version_id == "v2"


def test_as_it_was_at_a_time_picks_the_newest_version_at_or_before_it() -> None:
    versions = [_version("dm", 30, latest=True, marker=True), _version("v2", 20), _version("v1", 10)]
    assert choose_version(versions, before=datetime(2026, 10, 1, 9, 15, tzinfo=UTC)).version_id == "v1"
    assert choose_version(versions, before=datetime(2026, 10, 1, 9, 20, tzinfo=UTC)).version_id == "v2"
    with pytest.raises(DrillError, match="at or before"):
        choose_version(versions, before=datetime(2026, 10, 1, 9, 5, tzinfo=UTC))


@pytest.mark.parametrize(
    ("versions", "message"),
    [
        ([], "no versions"),
        ([_version("v1", 1, latest=True)], "one version only"),
        ([_version("dm", 1, latest=True, marker=True)], "only delete markers"),
    ],
)
def test_nothing_to_restore_is_an_error_not_a_guess(versions: list[ObjectVersion], message: str) -> None:
    with pytest.raises(DrillError, match=message):
        choose_version(versions)


# ---------------------------------------------------------------------------
# S3 against moto
# ---------------------------------------------------------------------------
def test_the_canary_drill_restores_the_good_version_verifies_it_and_cleans_up(s3: Any) -> None:
    record = s3_canary_drill(s3, bucket=BUCKET, execute=True, token="known-token")

    assert record.executed is True
    assert record.verified is True
    assert record.rto_seconds is not None and record.rto_seconds >= 0
    assert [step.outcome for step in record.steps] == ["ok"] * len(record.steps)
    assert [step.call for step in record.steps] == [
        "s3:GetBucketVersioning",
        "s3:PutObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListObjectVersions",
        "s3:CopyObject",
        "s3:GetObject",
        "s3:DeleteObjectVersion",
    ]
    # Nothing left behind, and nothing outside the drill prefix was ever written.
    listed = s3.list_object_versions(Bucket=BUCKET)
    assert not listed.get("Versions") and not listed.get("DeleteMarkers")
    assert all(step.params["Key"].startswith(DRILL_PREFIX) for step in record.steps if "Key" in step.params)


def test_the_canary_drill_on_an_unversioned_bucket_is_a_finding(aws_credentials: None) -> None:
    import boto3

    with moto.mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket="unversioned", CreateBucketConfiguration={"LocationConstraint": REGION})
        with pytest.raises(DrillError, match="Versioning is never enabled"):
            s3_canary_drill(client, bucket="unversioned", execute=True)
        assert "Contents" not in client.list_objects_v2(Bucket="unversioned")  # it stopped before writing


def test_an_incident_restore_by_time_copies_that_version_back_and_keeps_history(s3: Any) -> None:
    key = "runs/r_1/run.json"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"good")
    good_at = object_versions(s3, BUCKET, key)[0].last_modified
    # S3 (and moto) stamp LastModified to the second, so "as it was at T" can only tell two writes
    # apart when they are in different seconds - which is also true on the real account.
    time.sleep(1.1)
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"bad")
    s3.delete_object(Bucket=BUCKET, Key=key)
    versions_before = len(object_versions(s3, BUCKET, key))

    record = s3_restore(s3, bucket=BUCKET, key=key, before=good_at, execute=True)

    assert record.executed and record.rto_seconds is not None
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"good"
    assert len(object_versions(s3, BUCKET, key)) == versions_before + 1  # a restore adds, never deletes


def test_an_incident_restore_by_version_id_is_exact(s3: Any) -> None:
    key = "models/m_1/schema.json"
    first = s3.put_object(Bucket=BUCKET, Key=key, Body=b"v1")["VersionId"]
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"v2")

    s3_restore(s3, bucket=BUCKET, key=key, version_id=first, execute=True)

    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"v1"


def test_undo_the_last_change_against_moto(s3: Any) -> None:
    key = "uploads/u_1/profile.json"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"v1")
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"v2")

    s3_restore(s3, bucket=BUCKET, key=key, execute=True)

    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"v1"


# ---------------------------------------------------------------------------
# RDS: the call sequence, against a fake (moto's RDS does not model restore timing or networking)
# ---------------------------------------------------------------------------
class _FakeWaiter:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], name: str) -> None:
        self._calls, self._name = calls, name

    def wait(self, **kwargs: Any) -> None:
        self._calls.append((f"wait:{self._name}", kwargs))


class _FakeRds:
    """Records every call and answers with the shape of the real `DescribeDBInstances`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def describe_db_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_db_instances", kwargs))
        return {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": kwargs["DBInstanceIdentifier"],
                    "DBInstanceClass": "db.t4g.small",
                    "DBSubnetGroup": {"DBSubnetGroupName": "isolated-subnets"},
                    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-database"}],
                    "DBParameterGroups": [{"DBParameterGroupName": "force-ssl-params"}],
                    "LatestRestorableTime": datetime.now(UTC) - timedelta(minutes=4),
                    "Endpoint": {"Address": "restored.example.invalid"},
                }
            ]
        }

    def get_waiter(self, name: str) -> _FakeWaiter:
        return _FakeWaiter(self.calls, name)

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, kwargs))
            return {}

        return call


def test_the_rds_drill_restores_into_a_new_instance_like_the_source_and_tears_down() -> None:
    fake = _FakeRds()
    record = rds_drill(fake, instance_id="marketing-ai-dev", execute=True)

    names = [name for name, _ in fake.calls]
    assert names == [
        "describe_db_instances",
        "create_db_snapshot",
        "wait:db_snapshot_available",
        "restore_db_instance_from_db_snapshot",
        "wait:db_instance_available",
        "describe_db_instances",
        "delete_db_instance",
        "delete_db_snapshot",
    ]
    restore = dict(fake.calls)["restore_db_instance_from_db_snapshot"]
    assert restore["DBInstanceIdentifier"].startswith("marketing-ai-dev-restored-")
    assert restore["DBInstanceIdentifier"] != "marketing-ai-dev"  # never over the source
    assert restore["DBSubnetGroupName"] == "isolated-subnets"
    assert restore["VpcSecurityGroupIds"] == ["sg-database"]
    assert restore["DBParameterGroupName"] == "force-ssl-params"  # rds.force_ssl holds on the copy
    assert restore["PubliclyAccessible"] is False
    assert dict(fake.calls)["delete_db_instance"]["DBInstanceIdentifier"] == restore["DBInstanceIdentifier"]
    assert record.rpo_seconds is not None and 200 < record.rpo_seconds < 400
    assert record.rto_seconds is not None


def test_keep_leaves_the_copy_and_says_it_is_billed() -> None:
    fake = _FakeRds()
    record = rds_drill(fake, instance_id="marketing-ai-dev", execute=True, keep=True)
    assert "delete_db_instance" not in [name for name, _ in fake.calls]
    assert any("billed" in note for note in record.notes)


def test_the_rds_drill_runs_against_moto(aws_credentials: None) -> None:
    """The same calls against moto's RDS: the parameter names and shapes are the real API's."""
    import boto3

    with moto.mock_aws():
        client = boto3.client("rds", region_name=REGION)
        client.create_db_instance(
            DBInstanceIdentifier="marketing-ai-dev",
            DBInstanceClass="db.t4g.small",
            Engine="postgres",
            MasterUsername="marketing_ai_app",
            MasterUserPassword="not-a-real-one",  # secret-scan: allow (moto fixture)
            AllocatedStorage=20,
        )
        record = rds_drill(client, instance_id="marketing-ai-dev", execute=True, waiter_delay=1)
        assert [step.outcome for step in record.steps] == ["ok"] * len(record.steps)
        remaining = [db["DBInstanceIdentifier"] for db in client.describe_db_instances()["DBInstances"]]
        assert remaining == ["marketing-ai-dev"]
        snapshots = client.describe_db_snapshots(SnapshotType="manual")["DBSnapshots"]
        assert snapshots == []


# ---------------------------------------------------------------------------
# verify-db: the restored copy against the source (SQLite stands in for the two Postgres servers)
# ---------------------------------------------------------------------------
def _database(path: Path, *, audit_rows: int, revision: str = "0004", extra_table: bool = True) -> Any:
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision})
        connection.execute(text("CREATE TABLE audit_events (id INTEGER PRIMARY KEY, action TEXT)"))
        for index in range(audit_rows):
            connection.execute(text("INSERT INTO audit_events (action) VALUES (:a)"), {"a": f"a{index}"})
        if extra_table:
            connection.execute(text("CREATE TABLE consent (id INTEGER PRIMARY KEY)"))
    return engine


def test_a_copy_behind_the_source_verifies_and_reports_how_far_behind(tmp_path: Path) -> None:
    source = _database(tmp_path / "source.db", audit_rows=5)
    restored = _database(tmp_path / "restored.db", audit_rows=3)

    record = restore_drill.verify_restored_database(source, restored, schema=None, target="copy")

    assert record.verified is True
    assert any("'audit_events': 2" in note for note in record.notes)
    rendered = json.dumps(record.model_dump(mode="json"))
    assert "a0" not in rendered and "a4" not in rendered  # counts only, never a value


@pytest.mark.parametrize(
    ("restored_kwargs", "finding"),
    [
        ({"audit_rows": 3, "extra_table": False}, "tables missing on the copy: ['consent']"),
        ({"audit_rows": 9}, "more rows on the copy than the source: ['audit_events']"),
        ({"audit_rows": 3, "revision": "0003"}, None),
    ],
)
def test_a_copy_that_is_not_this_database_does_not_verify(
    tmp_path: Path, restored_kwargs: dict[str, Any], finding: str | None
) -> None:
    source = _database(tmp_path / "source.db", audit_rows=5)
    restored = _database(tmp_path / "restored.db", **restored_kwargs)

    record = restore_drill.verify_restored_database(source, restored, schema=None, target="copy")

    assert record.verified is False
    if finding is not None:
        assert any(finding in note for note in record.notes)
    else:
        assert any("source 0004, copy 0003" in note for note in record.notes)


# ---------------------------------------------------------------------------
# A drill that fails part-way tears down what it created and still leaves a record
# ---------------------------------------------------------------------------
class _WaiterTimesOut(_FakeRds):
    """The restored instance never becomes available: the waiter gives up, as botocore's does."""

    def get_waiter(self, name: str) -> Any:
        if name != "db_instance_available":
            return super().get_waiter(name)

        class _GivesUp:
            def wait(self, **_kwargs: Any) -> None:
                raise TimeoutError("Waiter db_instance_available failed: Max attempts exceeded")

        return _GivesUp()


def test_a_failed_rds_restore_still_tears_down_and_carries_its_record() -> None:
    fake = _WaiterTimesOut()
    with pytest.raises(DrillError, match="wait for the restored instance") as raised:
        rds_drill(fake, instance_id="marketing-ai-dev", execute=True)

    names = [name for name, _ in fake.calls]
    assert names[-2:] == ["delete_db_instance", "delete_db_snapshot"]  # nothing billed is left running
    record = raised.value.record
    assert record is not None and record.executed and record.rto_seconds is None
    outcomes = {step.name: step.outcome for step in record.steps}
    assert outcomes["wait for the restored instance to be available"] == "TimeoutError"
    assert "read the restored endpoint" not in outcomes  # it stopped at the failure


class _SnapshotFails(_FakeRds):
    def create_db_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_db_snapshot", kwargs))
        raise PermissionError("AccessDenied")


def test_a_drill_that_created_nothing_deletes_nothing() -> None:
    fake = _SnapshotFails()
    with pytest.raises(DrillError, match="take a manual snapshot"):
        rds_drill(fake, instance_id="marketing-ai-dev", execute=True)
    assert [name for name, _ in fake.calls] == ["describe_db_instances", "create_db_snapshot"]


class _DeleteRefused(_WaiterTimesOut):
    def delete_db_instance(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_db_instance", kwargs))
        raise RuntimeError("InvalidDBInstanceState: the instance is still creating")


def test_what_could_not_be_torn_down_is_named_as_still_billed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restore_drill, "_client", lambda *_args: _DeleteRefused())
    output = tmp_path / "record.json"

    exit_code = restore_drill.main(
        ["rds", "--instance-id", "marketing-ai-dev", "--execute", "--output", str(output)]
    )

    assert exit_code == 1
    written = json.loads(output.read_text(encoding="utf-8"))  # written although the drill failed
    assert any(
        "LEFT BEHIND, still billed: instance marketing-ai-dev-restored-" in n for n in written["notes"]
    )
    assert not any("snapshot marketing-ai-dev-drill" in n for n in written["notes"])  # that one went


def test_a_failed_canary_drill_still_removes_the_canary(s3: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def copy_refused(**_kwargs: Any) -> None:
        raise PermissionError("AccessDenied on CopyObject")

    monkeypatch.setattr(s3, "copy_object", copy_refused)
    with pytest.raises(DrillError, match="restore it by copying") as raised:
        s3_canary_drill(s3, bucket=BUCKET, execute=True)

    listed = s3.list_object_versions(Bucket=BUCKET)
    assert not listed.get("Versions") and not listed.get("DeleteMarkers")
    record = raised.value.record
    assert record is not None and record.steps[-1].call == "s3:DeleteObjectVersion"
    assert record.steps[-1].outcome == "ok"
