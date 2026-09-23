"""The backup-and-restore drill (M52): restore an S3 object version and an RDS snapshot, and time it.

Why this script exists
----------------------
Phase 4b plan M52 asks for "a backup and restore drill (RDS snapshots, S3 versioning) with a
recorded recovery time". The infrastructure has the backups - `infra/database.py` keeps automated
RDS backups for `db_backup_retention_days` and `infra/storage.py` turns versioning on for the
artefact bucket - but a backup that has never been restored is a hope, not a control. The only
evidence that counts is a restore that was performed, verified and timed, on the real account.
There is no account yet, so this is the drill *written down as code*: `docs/BACKUP_RESTORE_DRILL.md`
is the procedure a person follows on account day, and this module does the parts that are API
calls, times every step, and writes the record.

Dry run by default
------------------
Without `--execute` nothing is called at all: no boto3 client is even built. The plan - every
call, with its parameters - is printed and written to the record with `executed: false`. The
drill creates a billed RDS instance and deletes things afterwards, so the only way to do that is to
say so on the command line.

What the drills do
------------------
**S3 (`s3`)** never touches customer data. It writes a canary object under `_drill/` with a random
token, overwrites it (the "bad deploy") and then deletes it (the "accidental delete"), exactly the
two failures versioning exists for. It then restores the *first* version by copying it over the
key - `CopyObject` with a `VersionId` - which is AWS's recommended restore: history is kept, the
restored object is a new current version, and the step is auditable in CloudTrail. It reads the
object back, checks the token's SHA-256, and deletes every version of the canary so the drill
leaves nothing behind. `s3-restore` is the same restore for a real key during an incident: pick the
version (by id, or the newest one at or before a time) and copy it back.

**RDS (`rds`)** takes a manual snapshot of the deployment's instance, restores it into a **new**
instance (`<instance>-restored-<stamp>`) on the same subnet group, security groups, parameter group
(so `rds.force_ssl=1` and the logging settings hold on the copy too) and class, and waits until it
is available. The restored copy is never pointed at by the application. Unless `--keep` is given,
the drill instance and the manual snapshot are deleted at the end (the drill instance has no final
snapshot: it is a copy) - so a drill that is to be verified runs with `--keep` and is torn down afterwards.

**When a step fails** - a waiter times out, a permission is missing - the drill does not simply
stop: whatever this run created is torn down (each teardown best-effort, so one failing does not
skip the next), anything that could not be deleted is named in the record's notes as still billed,
the record is written anyway, and the command exits 1. The canary drill likewise removes its
canary after a failure. A failed drill is the one whose record matters most.

**Verification (`verify-db`)** needs a connection from inside the VPC - the database is in isolated
subnets - so it is meant to run *as the deployment*, through `scripts/run_in_deployment.py`. It
reads the deployment's own DSN from Secrets Manager, swaps only the host for the restored copy's
endpoint, and compares the two: every table present, the same Alembic revision, and no table with
more rows on the copy than on the source. It prints table names and counts, never a value.

What "recovery time" means here
-------------------------------
Each step is timed with a monotonic clock and the record keeps them separately, because the number
an operator needs is not one number: snapshot creation time depends on how much changed since the
last one, restore time on the volume size, and the time to *repoint* the application (a new
`postgres_dsn` secret and a redeploy) is a human step the script cannot time. `rto_seconds` is the
sum of the steps this script performs; `docs/BACKUP_RESTORE_DRILL.md` says where to add the rest.
For RDS the record also keeps the instance's `LatestRestorableTime` at the start of the drill,
because `now - LatestRestorableTime` is the recovery *point* a point-in-time restore would give.

Run it with::

    .venv/bin/python -m scripts.restore_drill s3 --bucket <artefact bucket>             # dry run
    .venv/bin/python -m scripts.restore_drill s3 --bucket <artefact bucket> --execute
    .venv/bin/python -m scripts.restore_drill rds --instance-id marketing-ai-dev --execute --keep
    .venv/bin/python -m scripts.run_in_deployment --env dev -- \\
        python -m scripts.restore_drill verify-db --restored-host <restored endpoint>
    .venv/bin/python -m scripts.restore_drill s3-restore --bucket <b> --key runs/r_x/run.json \\
        --before 2026-10-01T09:00:00Z --execute

Every AWS call goes through one client object passed in, so the tests drive the S3 drill against
moto and the RDS sequence against moto and a fake; nothing in the suite reaches AWS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import column, func, inspect, select, table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

REPORT_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "reports" / "drills"
"""One JSON record per drill run: `<UTC date>-<drill>-<target>.json`."""

DRILL_PREFIX: Final[str] = "_drill/"
"""The only prefix the S3 drill writes to. No product code reads or lists it."""

DRILL_TAG: Final[dict[str, str]] = {"Key": "marketing-ai:drill", "Value": "restore"}
"""Tagged on everything the RDS drill creates, so a forgotten copy is findable in the console."""

WAITER_DELAY_SECONDS: Final[int] = 30
WAITER_MAX_ATTEMPTS: Final[int] = 120
"""An hour at most for each wait; a restore that has not finished by then is itself the finding."""

REGION: Final[str] = "ap-south-1"

Drill = Literal["s3", "rds", "s3-restore", "rds-verify"]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------
class StepRecord(BaseModel):
    """One step of a drill: what was (or would be) called and how long it took."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="What the step does, in operator language.")
    call: str = Field(description="The AWS API operation, e.g. `s3:CopyObject`.")
    params: dict[str, Any] = Field(description="Its parameters. Never a secret: none of them take one.")
    seconds: float | None = Field(description="Wall time of the step; None in a dry run.")
    outcome: str = Field(description="`planned`, `ok`, or what went wrong.")


class DrillRecord(BaseModel):
    """What one drill did, written to `reports/drills/` and summarised in the drill log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    drill: Drill = Field(description="Which drill ran.")
    target: str = Field(description="The bucket or the DB instance identifier.")
    executed: bool = Field(description="False for a dry run: nothing was called.")
    started_at: datetime = Field(description="UTC start of the drill.")
    finished_at: datetime = Field(description="UTC end of the drill.")
    steps: tuple[StepRecord, ...] = Field(description="Every step, in order.")
    rto_seconds: float | None = Field(description="Sum of the timed restore steps (not setup/teardown).")
    rpo_seconds: float | None = Field(
        description="RDS: start of drill minus the instance's LatestRestorableTime. None for S3."
    )
    verified: bool | None = Field(description="S3: the restored bytes' hash matched. RDS: filled in by hand.")
    notes: tuple[str, ...] = Field(description="What a reader needs, including the manual steps left.")


class DrillError(Exception):
    """A drill step found something wrong; the message says which step and what.

    `record` is the drill's record up to and including the failure and whatever teardown ran after
    it, so `main` can still write it: a failed drill is the one whose record matters most, and it
    is also the one most likely to have left a billed resource behind (listed in its notes).
    """

    def __init__(self, message: str, record: DrillRecord | None = None) -> None:
        super().__init__(message)
        self.record = record


@dataclass
class _Recorder:
    """Collects steps; runs each step only when executing, and times it."""

    execute: bool
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self.steps: list[StepRecord] = []
        self.restore_seconds = 0.0

    def step(
        self,
        name: str,
        call: str,
        params: dict[str, Any],
        action: Callable[[], Any] | None = None,
        *,
        counts_to_rto: bool = False,
    ) -> Any:
        if not self.execute or action is None:
            self.steps.append(
                StepRecord(name=name, call=call, params=params, seconds=None, outcome="planned")
            )
            return None
        started = self.clock()
        try:
            result = action()
        except Exception as exc:
            seconds = round(self.clock() - started, 3)
            self.steps.append(
                StepRecord(name=name, call=call, params=params, seconds=seconds, outcome=type(exc).__name__)
            )
            raise
        seconds = round(self.clock() - started, 3)
        if counts_to_rto:
            self.restore_seconds += seconds
        self.steps.append(StepRecord(name=name, call=call, params=params, seconds=seconds, outcome="ok"))
        return result

    def cleanup(self, name: str, call: str, params: dict[str, Any], action: Callable[[], Any] | None) -> bool:
        """A teardown step that never raises: False (and the step's outcome) when it failed.

        Teardown runs after a failure too, and one teardown failing must not stop the next one or
        replace the error that caused the failure - the caller lists what was left behind instead.
        """
        try:
            self.step(name, call, params, action)
        except Exception:  # recorded as the step's outcome by `step`
            return False
        return True


def _failure_message(exc: Exception, steps: Sequence[StepRecord]) -> str:
    """What to say when a drill stopped: a `DrillError`'s own words, else the step that failed."""
    if isinstance(exc, DrillError):
        return str(exc)
    failed = next((step for step in reversed(steps) if step.outcome not in ("ok", "planned")), None)
    where = f" at step '{failed.name}'" if failed else ""
    return f"The drill stopped{where}: {type(exc).__name__}."


# ---------------------------------------------------------------------------
# S3: find a version, restore it, and the canary drill
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ObjectVersion:
    """One entry of `ListObjectVersions` for one key: a real version or a delete marker."""

    version_id: str
    last_modified: datetime
    is_latest: bool
    delete_marker: bool
    size: int | None = None


def object_versions(client: Any, bucket: str, key: str) -> list[ObjectVersion]:
    """Every version and delete marker of exactly `key`, newest first."""
    found: list[ObjectVersion] = []
    paginator = client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=key):
        for entry in page.get("Versions", []):
            if entry["Key"] == key:
                found.append(
                    ObjectVersion(
                        entry["VersionId"], entry["LastModified"], entry["IsLatest"], False, entry.get("Size")
                    )
                )
        for entry in page.get("DeleteMarkers", []):
            if entry["Key"] == key:
                found.append(
                    ObjectVersion(entry["VersionId"], entry["LastModified"], entry["IsLatest"], True)
                )
    # LastModified has one-second resolution on S3, so it cannot order two writes in the same
    # second; IsLatest breaks that tie for the current one, and the listing's own order (newest
    # first, which S3 documents) is kept for the rest by the stable sort.
    return sorted(found, key=lambda v: (v.last_modified, v.is_latest), reverse=True)


def choose_version(versions: Sequence[ObjectVersion], *, before: datetime | None = None) -> ObjectVersion:
    """The version to restore.

    With `before`: the newest real version written at or before that time - "as it was at 09:00".
    Without: undo the last change - the newest real version that is not the current one, or, when
    the current entry is a delete marker, the newest real version (undo a delete).
    """
    real = [v for v in versions if not v.delete_marker]
    if before is not None:
        candidates = [v for v in real if v.last_modified <= before]
        if not candidates:
            raise DrillError(f"No version was written at or before {before.isoformat()}.")
        return candidates[0]
    if not versions:
        raise DrillError(
            "The key has no versions: it never existed, or versioning was off when it was written."
        )
    current = versions[0]
    if current.delete_marker:
        if not real:
            raise DrillError("The key has only delete markers; there is nothing to restore.")
        return real[0]
    older = [v for v in real if v.version_id != current.version_id]
    if not older:
        raise DrillError("The key has one version only; there is no earlier state to restore.")
    return older[0]


def copy_source(bucket: str, key: str, version_id: str) -> dict[str, str]:
    """The `CopySource` of a restore: this exact version of this key."""
    return {"Bucket": bucket, "Key": key, "VersionId": version_id}


def s3_restore(
    client: Any | None,
    *,
    bucket: str,
    key: str,
    version_id: str | None = None,
    before: datetime | None = None,
    execute: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> DrillRecord:
    """Restore one real key to an earlier version by copying that version over it (incident use)."""
    started_at = datetime.now(UTC)
    recorder = _Recorder(execute=execute and client is not None, clock=clock)
    chosen_id = version_id
    if chosen_id is None:
        chosen = recorder.step(
            "find the version to restore",
            "s3:ListObjectVersions",
            {"Bucket": bucket, "Prefix": key, "before": before.isoformat() if before else None},
            (lambda: choose_version(object_versions(client, bucket, key), before=before)) if client else None,
            counts_to_rto=True,
        )
        chosen_id = chosen.version_id if chosen is not None else "<chosen at run time>"
    recorder.step(
        "copy that version over the key",
        "s3:CopyObject",
        {"Bucket": bucket, "Key": key, "CopySource": copy_source(bucket, key, chosen_id)},
        (
            (
                lambda: client.copy_object(
                    Bucket=bucket, Key=key, CopySource=copy_source(bucket, key, chosen_id)
                )
            )
            if client
            else None
        ),
        counts_to_rto=True,
    )
    return DrillRecord(
        drill="s3-restore",
        target=f"s3://{bucket}/{key}",
        executed=recorder.execute,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        steps=tuple(recorder.steps),
        rto_seconds=round(recorder.restore_seconds, 3) if recorder.execute else None,
        rpo_seconds=None,
        verified=None,
        notes=("The previous current version is kept as a noncurrent version; nothing was deleted.",),
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def s3_canary_drill(
    client: Any | None,
    *,
    bucket: str,
    execute: bool = False,
    clock: Callable[[], float] = time.monotonic,
    token: str | None = None,
) -> DrillRecord:
    """Write, corrupt, delete and restore a canary under `_drill/`; verify it; clean up every version."""
    started_at = datetime.now(UTC)
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    key = f"{DRILL_PREFIX}restore-canary-{stamp}.json"
    good = json.dumps({"drill": "restore", "token": token or secrets.token_hex(16)}).encode()
    recorder = _Recorder(execute=execute and client is not None, clock=clock)
    live = client if recorder.execute else None

    def put(body: bytes) -> str:
        assert live is not None
        return str(live.put_object(Bucket=bucket, Key=key, Body=body)["VersionId"])

    def versioning_is_on() -> str:
        assert live is not None
        status = str(live.get_bucket_versioning(Bucket=bucket).get("Status", "never enabled"))
        if status != "Enabled":
            raise DrillError(f"Versioning is {status} on {bucket}: a deleted object cannot be restored.")
        return status

    failure: Exception | None = None
    wrote_canary = False
    verified: bool | None = None
    try:
        recorder.step(
            "check that versioning is on",
            "s3:GetBucketVersioning",
            {"Bucket": bucket},
            versioning_is_on if live else None,
        )
        wrote_canary = recorder.execute
        verified = _canary_restore_steps(recorder, live, bucket=bucket, key=key, good=good, put=put)
    except Exception as exc:  # re-raised below, after the canary is cleaned up
        failure = exc

    def remove_every_version() -> int:
        assert live is not None
        versions = object_versions(live, bucket, key)
        for version in versions:
            live.delete_object(Bucket=bucket, Key=key, VersionId=version.version_id)
        return len(versions)

    cleaned = True
    if wrote_canary or not recorder.execute:
        cleaned = recorder.cleanup(
            "clean up: delete every version of the canary",
            "s3:DeleteObjectVersion",
            {"Bucket": bucket, "Key": key},
            remove_every_version if live else None,
        )
    notes = [
        f"Canary key {key}; no customer object was read or written.",
        "RTO here is find + copy + verify for one object. A prefix-wide restore is one copy per key.",
    ]
    if not cleaned:
        notes.append(f"LEFT BEHIND: versions of {key}; delete them by hand (list-object-versions).")
    record = DrillRecord(
        drill="s3",
        target=f"s3://{bucket}",
        executed=recorder.execute,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        steps=tuple(recorder.steps),
        rto_seconds=round(recorder.restore_seconds, 3) if recorder.execute else None,
        rpo_seconds=None,
        verified=verified,
        notes=tuple(notes),
    )
    if failure is not None:
        raise DrillError(_failure_message(failure, recorder.steps), record) from failure
    if verified is False:
        raise DrillError("The restored canary's hash does not match the version that was written.", record)
    return record


def _canary_restore_steps(
    recorder: _Recorder, live: Any | None, *, bucket: str, key: str, good: bytes, put: Callable[[bytes], str]
) -> bool | None:
    """Write, overwrite, delete, find, restore and read back the canary; True when the bytes match."""
    bad = b'{"drill": "restore", "token": "corrupted by the drill"}'
    first_id = recorder.step(
        "write the canary (the good version)",
        "s3:PutObject",
        {"Bucket": bucket, "Key": key, "sha256": _sha256(good)},
        (lambda: put(good)) if live else None,
    )
    recorder.step(
        "overwrite it (simulated bad write)",
        "s3:PutObject",
        {"Bucket": bucket, "Key": key},
        (lambda: put(bad)) if live else None,
    )
    recorder.step(
        "delete it (simulated accidental delete)",
        "s3:DeleteObject",
        {"Bucket": bucket, "Key": key},
        (lambda: live.delete_object(Bucket=bucket, Key=key)) if live else None,
    )

    def find_good() -> ObjectVersion:
        versions = object_versions(live, bucket, key)
        if not versions[0].delete_marker:
            raise DrillError("The delete did not leave a delete marker: is versioning on for this bucket?")
        return next(v for v in versions if v.version_id == first_id)

    recorder.step(
        "find the good version",
        "s3:ListObjectVersions",
        {"Bucket": bucket, "Prefix": key},
        find_good if live else None,
        counts_to_rto=True,
    )
    recorder.step(
        "restore it by copying that version over the key",
        "s3:CopyObject",
        {"Bucket": bucket, "Key": key, "CopySource": copy_source(bucket, key, first_id or "<first version>")},
        (
            (
                lambda: live.copy_object(
                    Bucket=bucket, Key=key, CopySource=copy_source(bucket, key, str(first_id))
                )
            )
            if live
            else None
        ),
        counts_to_rto=True,
    )
    restored = recorder.step(
        "read it back and compare hashes",
        "s3:GetObject",
        {"Bucket": bucket, "Key": key},
        (lambda: live.get_object(Bucket=bucket, Key=key)["Body"].read()) if live else None,
        counts_to_rto=True,
    )
    return None if restored is None else _sha256(restored) == _sha256(good)


# ---------------------------------------------------------------------------
# RDS: snapshot, restore into a new instance, tear down
# ---------------------------------------------------------------------------
def drill_identifier(instance_id: str, kind: Literal["drill", "restored"], stamp: str) -> str:
    """`<instance>-<kind>-<stamp>`, cut to RDS's 63-character limit and never ending in a hyphen."""
    return f"{instance_id}-{kind}-{stamp}"[:63].rstrip("-")


def restore_parameters(
    source: dict[str, Any] | None, *, restored_id: str, snapshot_id: str
) -> dict[str, Any]:
    """`RestoreDBInstanceFromDBSnapshot`'s parameters: the source's network, parameters and class.

    A restore does **not** inherit the source's security groups or parameter group - it gets the
    VPC's default security group and the engine family's default parameter group, which lacks this
    deployment's statement logging and, on PostgreSQL before 15, has `rds.force_ssl=0`. So they
    are copied from the source explicitly, and a field the source does
    not report is left out rather than guessed (RDS then applies its default, and the record shows
    the omission). In a dry run there is no source and the plan says where each value will come from.
    """
    params: dict[str, Any] = {
        "DBInstanceIdentifier": restored_id,
        "DBSnapshotIdentifier": snapshot_id,
        "PubliclyAccessible": False,
        "MultiAZ": False,
        "DeletionProtection": False,
        "Tags": [DRILL_TAG],
    }
    if source is None:
        params.update(
            DBSubnetGroupName="<from source>",
            VpcSecurityGroupIds=["<from source>"],
            DBParameterGroupName="<from source>",
            DBInstanceClass="<from source>",
        )
        return params
    subnet_group = (source.get("DBSubnetGroup") or {}).get("DBSubnetGroupName")
    if subnet_group:
        params["DBSubnetGroupName"] = subnet_group
    groups = [group["VpcSecurityGroupId"] for group in source.get("VpcSecurityGroups", [])]
    if groups:
        params["VpcSecurityGroupIds"] = groups
    parameter_groups = source.get("DBParameterGroups", [])
    if parameter_groups:
        params["DBParameterGroupName"] = parameter_groups[0]["DBParameterGroupName"]
    if source.get("DBInstanceClass"):
        params["DBInstanceClass"] = source["DBInstanceClass"]
    return params


def rds_drill(
    client: Any | None,
    *,
    instance_id: str,
    execute: bool = False,
    keep: bool = False,
    clock: Callable[[], float] = time.monotonic,
    waiter_delay: int = WAITER_DELAY_SECONDS,
) -> DrillRecord:
    """Snapshot `instance_id`, restore it into a new instance, wait, and (unless `keep`) delete both."""
    started_at = datetime.now(UTC)
    stamp = started_at.strftime("%Y%m%d%H%M%S").lower()
    snapshot_id = drill_identifier(instance_id, "drill", stamp)
    restored_id = drill_identifier(instance_id, "restored", stamp)
    recorder = _Recorder(execute=execute and client is not None, clock=clock)
    live = client if recorder.execute else None
    waiter_config = {"Delay": waiter_delay, "MaxAttempts": WAITER_MAX_ATTEMPTS}

    source: dict[str, Any] | None = None
    endpoint: str | None = None
    snapshot_created = restore_started = False
    failure: Exception | None = None
    try:
        source = recorder.step(
            "read the source instance (network, parameter group, class, restorable time)",
            "rds:DescribeDBInstances",
            {"DBInstanceIdentifier": instance_id},
            (
                (lambda: live.describe_db_instances(DBInstanceIdentifier=instance_id)["DBInstances"][0])
                if live
                else None
            ),
        )
        recorder.step(
            "take a manual snapshot",
            "rds:CreateDBSnapshot",
            {"DBInstanceIdentifier": instance_id, "DBSnapshotIdentifier": snapshot_id, "Tags": [DRILL_TAG]},
            (
                (
                    lambda: live.create_db_snapshot(
                        DBInstanceIdentifier=instance_id, DBSnapshotIdentifier=snapshot_id, Tags=[DRILL_TAG]
                    )
                )
                if live
                else None
            ),
        )
        snapshot_created = recorder.execute
        recorder.step(
            "wait for the snapshot to be available",
            "rds:DescribeDBSnapshots (waiter db_snapshot_available)",
            {"DBSnapshotIdentifier": snapshot_id},
            (
                (
                    lambda: live.get_waiter("db_snapshot_available").wait(
                        DBSnapshotIdentifier=snapshot_id, WaiterConfig=waiter_config
                    )
                )
                if live
                else None
            ),
        )
        restore_params = restore_parameters(source, restored_id=restored_id, snapshot_id=snapshot_id)
        recorder.step(
            "restore the snapshot into a new instance",
            "rds:RestoreDBInstanceFromDBSnapshot",
            restore_params,
            (lambda: live.restore_db_instance_from_db_snapshot(**restore_params)) if live else None,
            counts_to_rto=True,
        )
        restore_started = recorder.execute
        recorder.step(
            "wait for the restored instance to be available",
            "rds:DescribeDBInstances (waiter db_instance_available)",
            {"DBInstanceIdentifier": restored_id},
            (
                (
                    lambda: live.get_waiter("db_instance_available").wait(
                        DBInstanceIdentifier=restored_id, WaiterConfig=waiter_config
                    )
                )
                if live
                else None
            ),
            counts_to_rto=True,
        )
        endpoint = recorder.step(
            "read the restored endpoint",
            "rds:DescribeDBInstances",
            {"DBInstanceIdentifier": restored_id},
            (
                (
                    lambda: live.describe_db_instances(DBInstanceIdentifier=restored_id)["DBInstances"][0]
                    .get("Endpoint", {})
                    .get("Address")
                )
                if live
                else None
            ),
        )
    except Exception as exc:  # re-raised below, after whatever this run created is torn down
        failure = exc

    rpo_seconds: float | None = None
    if source is not None and source.get("LatestRestorableTime") is not None:
        rpo_seconds = round((started_at - source["LatestRestorableTime"]).total_seconds(), 3)

    # Teardown runs after a failure as well - a restore whose waiter timed out is still a billed
    # instance - and only for what this run created. `--keep` keeps both unless the drill failed
    # before there was anything worth verifying.
    left_behind: list[str] = []
    dry = not recorder.execute
    delete_copy = (dry or restore_started) and not keep
    delete_snapshot = (dry or snapshot_created) and (
        not keep or (failure is not None and not restore_started)
    )
    if delete_copy and not recorder.cleanup(
        "tear down: delete the restored copy (no final snapshot: it is a copy)",
        "rds:DeleteDBInstance",
        {"DBInstanceIdentifier": restored_id, "SkipFinalSnapshot": True, "DeleteAutomatedBackups": True},
        (
            (
                lambda: live.delete_db_instance(
                    DBInstanceIdentifier=restored_id, SkipFinalSnapshot=True, DeleteAutomatedBackups=True
                )
            )
            if live
            else None
        ),
    ):
        left_behind.append(f"instance {restored_id}")
    if delete_snapshot and not recorder.cleanup(
        "tear down: delete the manual snapshot",
        "rds:DeleteDBSnapshot",
        {"DBSnapshotIdentifier": snapshot_id},
        (lambda: live.delete_db_snapshot(DBSnapshotIdentifier=snapshot_id)) if live else None,
    ):
        left_behind.append(f"snapshot {snapshot_id}")
    notes = [
        (
            f"Restored copy: {restored_id} at {endpoint or '<endpoint>'}. The application was not repointed."
            if dry or restore_started
            else "No copy was restored: the drill stopped before the restore step."
        ),
        "Verify with --keep, from inside the VPC: python -m scripts.run_in_deployment --env <env> -- "
        f"python -m scripts.restore_drill verify-db --restored-host {endpoint or '<endpoint>'}",
        "RTO excludes repointing the application (new postgres_dsn secret + redeploy); time it by hand.",
    ]
    if keep and restore_started:
        notes.append(f"--keep: {restored_id} and snapshot {snapshot_id} are still running and billed.")
    if left_behind:
        notes.append(
            f"LEFT BEHIND, still billed: {', '.join(left_behind)}. The teardown step's outcome says why; "
            "delete by hand once it is available (docs/BACKUP_RESTORE_DRILL.md section 4)."
        )
    record = DrillRecord(
        drill="rds",
        target=instance_id,
        executed=recorder.execute,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        steps=tuple(recorder.steps),
        rto_seconds=round(recorder.restore_seconds, 3) if recorder.execute and failure is None else None,
        rpo_seconds=rpo_seconds,
        verified=None,
        notes=tuple(notes),
    )
    if failure is not None:
        raise DrillError(_failure_message(failure, recorder.steps), record) from failure
    return record


# ---------------------------------------------------------------------------
# RDS: verify the restored copy against the source, from inside the VPC
# ---------------------------------------------------------------------------
ALEMBIC_TABLE: Final[str] = "alembic_version"


def table_row_counts(engine: Engine, schema: str | None) -> dict[str, int]:
    """`{table: row count}` for every table in `schema`. Names and counts only - never a value."""
    names = sorted(inspect(engine).get_table_names(schema=schema))
    with engine.connect() as connection:
        return {
            name: int(
                connection.execute(select(func.count()).select_from(table(name, schema=schema))).scalar_one()
            )
            for name in names
        }


def schema_revision(engine: Engine, schema: str | None) -> str | None:
    """The Alembic revision the database is at, or None when it has no `alembic_version` table."""
    if ALEMBIC_TABLE not in inspect(engine).get_table_names(schema=schema):
        return None
    with engine.connect() as connection:
        version = connection.execute(
            select(column("version_num")).select_from(table(ALEMBIC_TABLE, schema=schema))
        ).scalar()
    return None if version is None else str(version)


def verify_restored_database(
    source: Engine,
    restored: Engine,
    *,
    schema: str | None,
    target: str,
    clock: Callable[[], float] = time.monotonic,
) -> DrillRecord:
    """Compare the restored copy with the running database: same tables, same revision, sane counts.

    The source keeps taking writes after the snapshot, so equal counts are not the test. What must
    hold is that every table exists on the copy, the Alembic revision is the same, and no table has
    *more* rows on the copy than on the source (which would mean the copy is not of this database).
    A table with fewer rows is expected and listed, so the reader can see how much the copy is
    behind - that difference is the drill's measured recovery point, in rows.
    """
    started_at = datetime.now(UTC)
    recorder = _Recorder(execute=True, clock=clock)
    source_counts: dict[str, int] = recorder.step(
        "count rows on the source",
        "SELECT count(*) per table",
        {"schema": schema},
        lambda: table_row_counts(source, schema),
    )
    restored_counts: dict[str, int] = recorder.step(
        "count rows on the restored copy",
        "SELECT count(*) per table",
        {"schema": schema},
        lambda: table_row_counts(restored, schema),
        counts_to_rto=True,
    )
    source_revision = schema_revision(source, schema)
    restored_revision = schema_revision(restored, schema)
    missing = sorted(set(source_counts) - set(restored_counts))
    ahead = sorted(name for name, count in restored_counts.items() if count > source_counts.get(name, count))
    behind = {
        name: source_counts[name] - count
        for name, count in sorted(restored_counts.items())
        if name in source_counts and count < source_counts[name]
    }
    verified = not missing and not ahead and source_revision == restored_revision
    notes = [
        f"tables: {len(source_counts)} on the source, {len(restored_counts)} on the copy",
        f"alembic revision: source {source_revision}, copy {restored_revision}",
        f"rows the copy is behind, per table: {behind or 'none'}",
    ]
    if missing:
        notes.append(f"FINDING: tables missing on the copy: {missing}")
    if ahead:
        notes.append(f"FINDING: tables with more rows on the copy than the source: {ahead}")
    return DrillRecord(
        drill="rds-verify",
        target=target,
        executed=True,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        steps=tuple(recorder.steps),
        rto_seconds=None,
        rpo_seconds=None,
        verified=verified,
        notes=tuple(notes),
    )


def _engines_for(restored_host: str) -> tuple[Engine, Engine, str | None]:
    """The running database and the restored copy, from this deployment's own settings.

    Run inside the deployment (`scripts/run_in_deployment.py`), so the DSN comes from Secrets
    Manager exactly as the service reads it; only the host is swapped. The copy was restored from a
    snapshot of this database, so the same credentials open it. Neither URL is ever printed.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from engine.settings import load_settings

    settings = load_settings()
    if not settings.postgres_dsn:
        raise DrillError("This deployment has no postgres_dsn; run this inside the deployment.")
    url = make_url(settings.postgres_dsn.get_secret_value())
    return create_engine(url), create_engine(url.set(host=restored_host)), settings.postgres_schema


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------
def write_record(record: DrillRecord, output: Path | None = None) -> Path:
    """Write `record` as JSON to `output`, or to `reports/drills/<date>-<drill>-<target>.json`."""
    safe_target = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in record.target)[:80]
    dry = "" if record.executed else "-dry-run"
    path = (
        output
        or REPORT_DIR / f"{record.started_at.date().isoformat()}-{record.drill}-{safe_target}{dry}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _client(service: Literal["s3", "rds"], region: str) -> Any:
    """A boto3 client; imported here so a dry run needs neither boto3 nor credentials."""
    import boto3

    if service == "s3":
        return boto3.client("s3", region_name=region)
    return boto3.client("rds", region_name=region)


def _parse_time(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--before needs a timezone, e.g. 2026-10-01T09:00:00Z.")
    return parsed


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.restore_drill", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="drill", required=True)
    s3 = sub.add_parser("s3", help="the S3 versioning drill on a _drill/ canary")
    s3.add_argument("--bucket", required=True)
    rds = sub.add_parser("rds", help="snapshot the instance and restore it into a new one")
    rds.add_argument("--instance-id", required=True)
    rds.add_argument("--keep", action="store_true", help="leave the restored copy and snapshot running")
    verify = sub.add_parser(
        "verify-db", help="inside the deployment: compare the restored copy's tables with the source"
    )
    verify.add_argument("--restored-host", required=True, help="the restored instance's endpoint address")
    restore = sub.add_parser("s3-restore", help="restore one real key to an earlier version")
    restore.add_argument("--bucket", required=True)
    restore.add_argument("--key", required=True)
    choose = restore.add_mutually_exclusive_group()
    choose.add_argument("--version-id")
    choose.add_argument("--before", type=_parse_time, help="newest version at or before this UTC time")
    for command in (s3, rds, restore):
        command.add_argument("--execute", action="store_true", help="really call AWS (default: dry run)")
        command.add_argument("--region", default=REGION)
        command.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one drill (dry by default), write its record, print its steps."""
    args = parse_args(argv)
    if args.drill == "verify-db":
        source, restored, schema = _engines_for(args.restored_host)
        verified = verify_restored_database(source, restored, schema=schema, target=args.restored_host)
        # Printed whole: inside a one-off task the file below is lost with the container, and
        # `run_in_deployment` echoes this output to the operator. It holds names and counts only.
        print(json.dumps(verified.model_dump(mode="json"), indent=2))
        return 0 if verified.verified else 1
    try:
        record = _run_drill(args)
    except DrillError as exc:
        if exc.record is None:
            raise
        # A failed drill still writes its record: which step failed, and what it left behind.
        _print_record(exc.record, write_record(exc.record, args.output))
        print(f"FAILED: {exc}")
        return 1
    path = write_record(record, args.output)
    _print_record(record, path)
    return 0


def _run_drill(args: argparse.Namespace) -> DrillRecord:
    """The one drill `args` names, dry unless `--execute`."""
    if args.drill == "s3":
        client = _client("s3", args.region) if args.execute else None
        record = s3_canary_drill(client, bucket=args.bucket, execute=args.execute)
    elif args.drill == "rds":
        client = _client("rds", args.region) if args.execute else None
        record = rds_drill(client, instance_id=args.instance_id, execute=args.execute, keep=args.keep)
    else:
        client = _client("s3", args.region) if args.execute else None
        record = s3_restore(
            client,
            bucket=args.bucket,
            key=args.key,
            version_id=args.version_id,
            before=args.before,
            execute=args.execute,
        )
    return record


def _print_record(record: DrillRecord, path: Path) -> None:
    print(f"{record.drill} drill on {record.target}: {'EXECUTED' if record.executed else 'DRY RUN'}")
    for step in record.steps:
        timing = f"{step.seconds:.1f}s" if step.seconds is not None else "-"
        print(f"  [{step.outcome:>7}] {timing:>8}  {step.call:<45} {step.name}")
    if record.rto_seconds is not None:
        print(f"  restore steps took {record.rto_seconds:.1f}s (rto_seconds)")
    for note in record.notes:
        print(f"  note: {note}")
    print(f"record: {path}")


if __name__ == "__main__":
    sys.exit(main())
