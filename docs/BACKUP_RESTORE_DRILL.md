# Backup and restore drill (Phase 4b, M52)

Plan M52: *"Backup and restore drill (RDS snapshots, S3 versioning) with a recorded recovery time."*

A backup nobody has restored is a hope, not a control. This is the procedure for the **first drill
on the dev account (account day, prerequisite P1)** and for every drill after it. The API calls are
in `scripts/restore_drill.py`, which is **dry-run by default**, times every step and writes a record;
this page is what a person does around it and where the numbers go.

`docs/RUNBOOK.md` section 8 is the *incident* procedure (something is gone, restore it). This page is
the *rehearsal*: it restores into a copy, never over the running deployment, and it measures.

**Nothing below has been run against AWS yet.** The S3 steps and the RDS call sequence are tested
against moto (`tests/unit/test_restore_drill.py`); every recovery time on this page is blank until
the drill log at the bottom has a row.

## 1. What is backed up, and what is not

| Store | Holds | Backup mechanism (from `infra/`) | Window | Not protected against |
|---|---|---|---|---|
| RDS PostgreSQL | model registry (champions, approvals), run index, and the Phase 4b platform tables: users, sessions, audit events, consent ledger, erasure requests, schedules | automated backups + PITR, `db_backup_retention_days` (7, both envs); manual snapshots (this drill) | PITR to ~5 min ago, back 7 days | the account, the region, the KMS key (below) |
| Artefact bucket | `uploads/`, `runs/`, `models/`, `_bootstrap/` | versioning on; the M48 lifecycle backstop expires **noncurrent** versions `grace_days` (7) after they become noncurrent | an overwritten or deleted object is recoverable for `grace_days` | the bucket's destruction; **a caller with `s3:DeleteObjectVersion`** - which the API task role has on `uploads/`/`runs/` (`docs/SECURITY_REVIEW.md` F-3) |
| Audit export bucket | M47 audit exports | S3 Object Lock, compliance mode, `audit_retention_days` | cannot be deleted by anyone, root included, until retention ends | nothing to restore: it is the evidence |
| Access-log bucket | S3 and ALB access logs | none (not versioned) | - | accepted: operational logs, expire on their own |
| ECR | images | immutable tags; 20 untagged kept | rollback by digest (RUNBOOK section 6) | - |
| KMS customer key | encrypts the bucket, the database, its snapshots, the secrets | rotation on; deletion has a 30-day pending window | cancel a deletion within 30 days | **a completed key deletion makes every backup above unreadable at once** - alarm on `ScheduleKeyDeletion` (account day) |

Two consequences worth reading before any restore:

* **The S3 recovery window for row-level data is `grace_days`, not "forever".** Versioning only
  helps until the lifecycle rule expires the noncurrent version, which is deliberate (DPDP erasure
  and retention must eventually remove bytes, M48). A restore request that arrives after
  `grace_days` cannot be met from S3.
* **A restore can resurrect data that was erased (DPDP).** An RDS restore to a point before an
  erasure brings the consent-ledger rows back; so can an S3 version restore. After any *real*
  restore, erasures completed after the restore point must be applied again. The erasure record
  keeps only `principal_hash` (by design), so re-applying it means matching restored principals by
  hash. The audit exports in the Object Lock bucket are the durable list of those erasures, because
  the `erasure_request` rows themselves are in the database being restored. There is no "re-apply
  erasures by hash" command yet (gap G-3).

## 2. Before the drill (account day)

* The dev deployment is up and has data: `docs/AWS_DEPLOYMENT.md` done, the Phase 1 acceptance run
  on AWS (M50) has produced at least one trained model and one scoring run.
* Operator credentials that can, in `ap-south-1`: `rds:DescribeDBInstances`, `CreateDBSnapshot`,
  `DescribeDBSnapshots`, `RestoreDBInstanceFromDBSnapshot`, `DeleteDBInstance`, `DeleteDBSnapshot`,
  `AddTagsToResource`; `s3:GetBucketVersioning`, `PutObject`, `GetObject`, `GetObjectVersion`,
  `DeleteObject`, `DeleteObjectVersion`, `ListBucketVersions` on the artefact bucket; `kms:Decrypt`,
  `GenerateDataKey` and `CreateGrant` on the deployment key (a restore creates a grant); and the ECS
  permissions `scripts/run_in_deployment.py` documents.
* Bucket name and instance id: the `BucketName` output of `marketing-ai-dev-storage`, and
  `marketing-ai-dev` (`infra/naming.db_instance_identifier`).
* A clock and this page open. Budget about two hours; the restored `db.t4g.small` runs for most of
  it. Its cost is **not yet measured** - read it off Cost Explorer the next day and add it to the log.

## 3. Drill A - S3 versioning (canary, about 5 minutes)

Never touches customer data: a canary under `_drill/` is written, overwritten, deleted, restored
from its first version by `CopyObject`, read back and hash-checked, and then every version of it is
deleted.

```bash
BUCKET=$(aws cloudformation describe-stacks --stack-name marketing-ai-dev-storage \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text)
.venv/bin/python -m scripts.restore_drill s3 --bucket "$BUCKET"             # dry run: read the plan
.venv/bin/python -m scripts.restore_drill s3 --bucket "$BUCKET" --execute   # the drill
```

Pass: every step `ok`, `verified: true`, and `aws s3api list-object-versions --bucket "$BUCKET"
--prefix _drill/` empty afterwards. `rto_seconds` is find + copy + read for one object.

Then rehearse the incident path on a real key without changing it, to prove a version can be found:

```bash
.venv/bin/python -m scripts.restore_drill s3-restore --bucket "$BUCKET" \
  --key "runs/<a real run id>/run.json" --before "<UTC time before its last write>"   # dry run only
```

## 4. Drill B - RDS snapshot restore (about 30-60 minutes, mostly waiting)

```bash
.venv/bin/python -m scripts.restore_drill rds --instance-id marketing-ai-dev          # dry run
.venv/bin/python -m scripts.restore_drill rds --instance-id marketing-ai-dev --execute --keep
```

What `--execute` does, each step timed: read the source (its subnet group, security groups,
parameter group, class and `LatestRestorableTime`), `CreateDBSnapshot`
`marketing-ai-dev-drill-<stamp>`, wait for it, `RestoreDBInstanceFromDBSnapshot` into
**`marketing-ai-dev-restored-<stamp>`** with the source's network and **the source's parameter
group** (a restore otherwise gets the default group - RUNBOOK section 8 explains why that matters
for `rds.force_ssl`), not public, not Multi-AZ, tagged `marketing-ai:drill=restore`, wait for it,
print its endpoint. `--keep` leaves it running so it can be verified.

Verify from inside the VPC, as the deployment (the restored copy has the same security groups, so
the service's own network reaches it):

```bash
python -m scripts.run_in_deployment --env dev -- \
  python -m scripts.restore_drill verify-db --restored-host <endpoint printed above>
```

Pass: exit 0 and `verified: true` - every table present on the copy, the same Alembic revision, no
table with more rows on the copy than on the source. The notes list how many rows each table is
behind the source, which is the recovery point in rows. Paste the printed JSON next to the record.

Also check, by hand, while the copy exists:

* `aws rds describe-db-instances --db-instance-identifier marketing-ai-dev-restored-<stamp>
  --query 'DBInstances[0].[DBParameterGroups[0].DBParameterGroupName,StorageEncrypted,KmsKeyId,PubliclyAccessible]'`
  shows the deployment's parameter group, encrypted with the deployment key, not public.

Tear down (the script does this when `--keep` is not given):

```bash
aws rds delete-db-instance --db-instance-identifier marketing-ai-dev-restored-<stamp> \
  --skip-final-snapshot --delete-automated-backups
aws rds delete-db-snapshot --db-snapshot-identifier marketing-ai-dev-drill-<stamp>
```

Optional variant, the one an incident more often needs: point-in-time restore, same verification.

```bash
aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier marketing-ai-dev \
  --target-db-instance-identifier marketing-ai-dev-restored-pitr --use-latest-restorable-time \
  --db-subnet-group-name <from source> --vpc-security-group-ids <from source> \
  --db-parameter-group-name <from source> --no-publicly-accessible --no-multi-az
```

## 5. Measuring the recovery time

The script times what it calls; an incident also has human steps it cannot time. Record all of them:

| Component | Measured by | Where it comes from |
|---|---|---|
| Snapshot creation | script (`take a manual snapshot` + wait) | record `steps[]` |
| Restore to available | script (`restore` + wait), summed into `rto_seconds` | record |
| Verification | `verify-db` step timings | printed JSON |
| Repointing the application (new `postgres_dsn` via a database-stack deploy, service restart; RUNBOOK section 8) | **stopwatch** - not done in a drill against dev's live service unless agreed | drill log |
| Rebuilding the run index (`scripts/reconcile_runs.py`, RUNBOOK section 5) | stopwatch | drill log |
| RPO, database | script: drill start minus `LatestRestorableTime` (`rpo_seconds`) | record |
| RPO, objects | 0 within `grace_days` (versioning); none after | configuration, not measured |

**Recovery time for the log = snapshot-to-available (script) + repoint + reindex (stopwatch).**
The records go to `reports/drills/<date>-<drill>-<target>.json`; commit them with the log row.

## 6. Drill log

One row per drill. The first row is due on account day; the drill is repeated at least before every
production go-live and after any change to `infra/database.py` or `infra/storage.py`.

| Date | Env | Operator | Drill | Snapshot (s) | Restore to available (s) | Verify | Repoint + reindex (min) | Total RTO | RPO | Cost | Issues found |
|---|---|---|---|---|---|---|---|---|---|---|---|
| _not yet run_ | dev | | S3 canary | - | | | - | | - | | |
| _not yet run_ | dev | | RDS snapshot | | | | | | | | |

## 7. Gaps that need the account or a decision

* **G-1 (P1):** every number above. Nothing has been restored on AWS.
* **G-2 (P1/P4):** backups live in the same account and region as the data, encrypted with the same
  key. Cross-account snapshot copy and S3 replication (or AWS Backup with a vault in a separate
  account) protect against losing the account or the key; which one depends on the deployment model
  (P4) and on budget (P3).
* **G-3 (Part 1 / M48):** re-applying erasures after a restore (section 1). Needs a command that
  takes the principal hashes of erasures completed after a point in time (from the audit exports)
  and erases matching principals from the restored stores. Until then, a real restore must be
  followed by re-submitting those erasure requests by hand from the client's own records.
* **G-4 (Phase 4a):** after a real restore the CloudFormation stack still names the old instance.
  Whether to rename-swap instances (keeps the endpoint, drifts the stack) or redeploy with the new
  identifier needs deciding on account day and writing into RUNBOOK section 8.
* **G-5 (Phase 4a):** `docs/SECURITY_REVIEW.md` F-3 - the API task role can delete object versions
  and rewrite lifecycle rules, so S3 versioning does not protect `uploads/`/`runs/` from a
  compromised API task.
