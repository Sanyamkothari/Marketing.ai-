# Runbook

On-call operations for a deployment that is already serving. `docs/AWS_DEPLOYMENT.md` is the other
half: it is read once per account, to get a deployment up. This one is read at three in the morning
by somebody who did not write the system, so every entry is the same three things in the same
order — the symptom you can see, what it means, and the first three things to do.

Assumptions throughout: one customer, one AWS account, `ap-south-1`, `ENV` is the deployment name
(`dev` or `prod`), and commands run from the repository root with credentials for that account.

```bash
export ENV=dev
export AWS_REGION=ap-south-1
export SERVICE_URL=$(aws cloudformation describe-stacks --stack-name "marketing-ai-$ENV-compute" \
  --query "Stacks[0].Outputs[?OutputKey=='ServiceUrl'].OutputValue" --output text)
export BUCKET=$(aws cloudformation describe-stacks --stack-name "marketing-ai-$ENV-storage" \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text)
```

---

## 1. Orientation

Six moving parts, and that is all of them:

1. An **Application Load Balancer** in the public subnets; it sends traffic only to a task answering
   `/healthz`.
2. One **ECS Fargate service**, `marketing-ai-<env>-api`, in the private subnets. The image it runs
   also trains and migrates; only the first argument to `scripts/entrypoint.sh` differs.
3. The **S3 artefact bucket**: `uploads/`, `runs/<run id>/`, `models/<use case>/<version>/`. This is
   the record of what happened.
4. **RDS for PostgreSQL**: the run index (an index of `run.json`, rebuildable) and the model
   registry rows (champion, approvals — *not* rebuildable).
5. **SageMaker** training and processing jobs, created per run from a `JobSpec`, named after the run.
6. **CloudWatch**: three log groups of ours, SageMaker's two of its own, and one SNS alarm topic.

### Where the logs are

| What writes it | Log group | Stream |
|---|---|---|
| The API task | `/marketing-ai/<env>/api` | `api/api/<ecs task id>` (driver prefix, container name, task id) |
| A SageMaker **training** job container | `/aws/sagemaker/TrainingJobs` | `<job name>/algo-1-<epoch>` |
| A SageMaker **processing** job container | `/aws/sagemaker/ProcessingJobs` | `<job name>/algo-1-<epoch>` |
| Postgres itself | `/aws/rds/instance/marketing-ai-<env>/postgresql` | RDS's own |
| Reserved for job output of ours | `/marketing-ai/<env>/jobs` | — |

The last row is the one to know before you go looking. The group exists and the execution role may
write to it, but a SageMaker job's container output goes to the service's own groups: the engine
logs to stdout and SageMaker collects stdout. Look in `/aws/sagemaker/TrainingJobs` first, and treat
an empty `/marketing-ai/<env>/jobs` as normal rather than as a lost job.

### Finding one run

A run id looks like `r_20261014_ab12cd34` — `r_`, the UTC date, eight hex characters. It is the
directory name, and everything else is derived from it:

| Thing | How it is spelled | Example |
|---|---|---|
| Run directory | `runs/<run id>/` in the artefact bucket | `runs/r_20261014_ab12cd34/` |
| SageMaker job name | `<prefix>-<train\|score>-<run id>`, with every underscore replaced by a hyphen (SageMaker's alphabet), truncated to the last 63 characters | `marketing-ai-train-r-20261014-ab12cd34` |
| Job log stream | the job name, then `/algo-1-…` | `marketing-ai-train-r-20261014-ab12cd34/algo-1-…` |

The prefix is `Settings.sagemaker_job_name_prefix`, default `marketing-ai`, and it is also the IAM
boundary: the task role may describe and stop `training-job/marketing-ai-*` and nothing else.

```bash
export RUN_ID=r_20261014_ab12cd34
export JOB="marketing-ai-train-$(echo "$RUN_ID" | tr '_' '-')"

curl -s "$SERVICE_URL/runs/$RUN_ID" | python -m json.tool          # run.json + status.json
aws s3 ls "s3://$BUCKET/runs/$RUN_ID/"
aws logs tail /aws/sagemaker/TrainingJobs --log-stream-name-prefix "$JOB" --since 2h
aws logs tail "/marketing-ai/$ENV/api" --since 30m --filter-pattern "$RUN_ID"
```

**No log line anywhere quotes a data value.** `log_failure` records an exception's class and not its
message, and `RedactingFormatter` renders a traceback's frames while withholding every exception
message, because a library builds those out of the value that upset it. A failure you cannot
diagnose from a log line is that control working; reproduce it against a synthetic file.

---

## 2. A run is stuck at "running"

**Symptom.** The Running screen has not moved for a long time. `GET /runs/{id}` keeps answering
`state: running`, and `status.json`'s `updated_at` is old.

**What it means.** Either the run really is running — training is one long stage — or the compute
carrying it ended without this process hearing about it. A remote job can be killed, its instance
can fail to start, somebody can stop it from the console; in every one of those cases nothing in
the API process was there to write the ending, so the status document says "running" for ever.

`SageMakerJobRunner.reconcile` is the repair, and it runs on its own: `GET /runs/{id}` calls it
before it reads the status document, so *polling the run is what fixes it*. It writes an ending only
when all three of these hold — SageMaker says the job reached a terminal state, `status.json` still
says pending or running, and that document has not been written for `RECONCILE_GRACE_SECONDS`
(120). The grace period is what stops it from beating a container that is at this moment writing the
better answer: the stage that failed, with the detail line that stage earned.

**First three things.**

```bash
# 1. Poll the run. This is not a diagnostic; it is the call that triggers reconciliation.
curl -s "$SERVICE_URL/runs/$RUN_ID" | python -m json.tool

# 2. Ask SageMaker what the job actually did. Stopped/Failed here plus "running" above is the case
#    reconcile exists for; InProgress means the run is simply not finished.
aws sagemaker describe-training-job --training-job-name "$JOB" \
  --query '{status:TrainingJobStatus,secondary:SecondaryStatus,reason:FailureReason}'
aws sagemaker describe-training-job --training-job-name "$JOB" \
  --query '{billable:BillableTimeInSeconds,started:TrainingStartTime,ended:TrainingEndTime}'

# 3. Read the document itself, in case the API cannot.
aws s3 cp "s3://$BUCKET/runs/$RUN_ID/status.json" - | python -m json.tool
```

If step 2 says `InProgress` and step 3's `updated_at` is advancing, nothing is wrong. If step 2 says
`Failed` or `Stopped` and step 1 still shows `running` two minutes later, reconciliation is not
happening: check that `job_backend=sagemaker` on this deployment (a thread-pool deployment has no
remote job to reconcile) and that the task role can call `DescribeTrainingJob` — a control plane the
process cannot reach changes nothing and is logged rather than raised.

The SageMaker console's Training jobs list, filtered by the job name above, is the same information
with a graph attached. Reading it needs your own credentials: the task role holds no `List*` action
at all, deliberately, because `ListTrainingJobs` cannot be scoped to a name prefix.

---

## 3. A run failed

**Symptom.** `state: failed` on the run, a red stage on the Running screen, or a `RunsFailed` alarm.

**What it means.** A stage raised a coded failure and the pipeline wrote it down. The code is the
thing to read: it says which of the failures the engine anticipated this is, and every anticipated
failure has a message written for the person who uploaded the file.

**First three things.**

```bash
# 1. The failing stage and its code. `error` carries {code, message, stage}.
curl -s "$SERVICE_URL/runs/$RUN_ID" \
  | python -c 'import json,sys; s=json.load(sys.stdin)["status"]; \
print(s["state"]); [print(r["key"], r["state"], r.get("detail"), r.get("error")) for r in s["stages"]]'

# 2. The run manifest, which is written for every outcome including this one.
curl -s "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" | python -m json.tool

# 3. The job's own output.
aws logs tail /aws/sagemaker/TrainingJobs --log-stream-name-prefix "$JOB" --since 3h
aws sagemaker describe-training-job --training-job-name "$JOB" --query 'FailureReason'
```

`FailureReason` is the first line of `/opt/ml/output/failure`, which the container writes on its way
out: `scripts/run_job_entrypoint.py` writes the engine's own message there, so the SageMaker console
shows the same sentence the run does rather than a Python traceback. Its path is overridable with
`MARKETING_AI_FAILURE_PATH`, which is what the tests use; leave it alone in a deployment.

### What the codes mean, and where the table is

| Code | What happened |
|---|---|
| `RUN_BLOCKED_BY_VALIDATION` | Validation found an error-severity problem; nothing was trained. Not an incident — see section 10 |
| `STAGE_FAILED` | A stage failed in a way no stage anticipated. This one is a bug; the traceback is in the log, never in the artefact |
| `STAGE_OUT_OF_ORDER` | A stage ran before the stage that feeds it. A wiring fault, not a data problem |
| `JOB_SUBMIT_FAILED` | The compute service refused the job, so no stage ever ran |
| `JOB_FAILED_REMOTELY` | The compute ended before the work finished; this is the ending reconciliation wrote (section 2) |
| `JOB_SPEC_UNREADABLE` | The container could not read the `job_spec.json` it was pointed at, so it ran nothing and changed nothing |

That table is `ENGINE_ERRORS` in `engine/errors.py`, with the suggestion each code carries. It is
only the failures the **pipeline itself** decides on. The stages carry their own: `SCORE_ERRORS` in
`engine/stages/score.py`, and the coded exceptions `IngestError`, `TrainError`, `EvaluationError`,
`RegistryError`, `StorageError` and `ConfigError` in their own modules. Whichever raised it,
`engine.errors.run_error` copies that code and message into `status.json` unchanged, so the code you
read is the code the stage chose.

---

## 4. Cancelling and re-running

**Symptom.** A run has to stop: wrong file, wrong settings, or it is holding compute before a
deployment.

**What it means.** Cancellation is **cooperative** (DEC-017). There is no kill. Every job function
holds a `CancelToken` and stages poll it between steps, so a cancelled run stops at the next clean
boundary; a job that has not started yet is cancelled outright. Inside a long training stage the
stop is bounded by AutoGluon's own `time_limit`, not by the cancel call.

**First three things.**

```bash
# 1. Ask the API. `cancelled` is true only for a job that was still pending or running.
curl -sX POST "$SERVICE_URL/runs/$RUN_ID/cancel" | python -m json.tool

# 2. Confirm both documents are terminal: every unfinished stage stops and the record says cancelled.
curl -s "$SERVICE_URL/runs/$RUN_ID" | python -m json.tool

# 3. Only if the remote job is still burning compute after that, stop it at the service.
aws sagemaker stop-training-job --training-job-name "$JOB"
```

Step 3 is the exception, not the routine: stopping the job at the service skips the cooperative
path, and the ending is then written by reconciliation (section 2) rather than by the run.

**A cancelled run keeps its artefacts.** The run directory is not deleted, and the manifest is
written for every outcome including this one. A cancelled attempt is evidence: it holds the recipe,
the dataset fingerprint and whatever the run reached before it stopped, which is exactly what you
want when the question afterwards is "what were we running when we stopped it". Re-running is a new
run with a new id; nothing is resumed, and an in-flight run whose task is replaced by a deployment
is not resumed either.

---

## 5. The run list is empty or stale

**Symptom.** The history page shows nothing, or is missing runs whose directories are plainly in the
bucket.

**What it means.** The run index is an **index of `run.json`, not the record**. It exists so a list
page does not read every run in the bucket, and it is written best-effort *after* the run document:
a database that was down when a run finished costs that run its row and nothing else. The documents
in S3 are the whole history; the table is a convenience over them.

**First three things.**

```bash
# 1. Confirm the run documents are there. If they are, nothing has been lost.
aws s3 ls "s3://$BUCKET/runs/" | tail -20

# 2. See the size of the repair without writing a row.
.venv/bin/python -m scripts.reconcile_runs --dry-run

# 3. Rebuild.
.venv/bin/python -m scripts.reconcile_runs
```

It walks `runs/*/run.json`, upserts a row for each, and drops rows whose document is gone. **That is
why it is safe:** it only ever copies what the authoritative document already says, so a rebuild
cannot invent a run or change one, and running it when nothing is wrong is a no-op with a cost. The
cost is the reason it is a command somebody runs and never something that happens on its own — it is
a full listing of the runs prefix, which is precisely the expensive operation the index exists to
remove from the request path.

Exit codes: `0` done; `2` this deployment keeps no index (`metadata_backend` is not `postgres`),
which is a shape, not a fault; `3` the scan finished but some documents could not be read or some
rows could not be written — the reasons are in the log, and running it again after fixing them is
safe.

Run it after an outage, after a restore, and the first time a deployment gets a database over a
bucket that already has runs in it.

---

## 6. Rolling back the image

**Symptom.** The new version is worse: tasks cycling, a route failing, a stage failing on files that
worked yesterday.

**What it means.** A deployment is an image **digest** plus a schema revision. Rolling back moves
the digest; it does not move the schema.

**First three things.**

```bash
# 1. Find what is running now, and what ran before.
aws ecs describe-task-definition --task-definition "marketing-ai-$ENV-api" \
  --query 'taskDefinition.containerDefinitions[0].image'
aws ecr describe-images --repository-name marketing-ai --region "$AWS_REGION" \
  --query 'reverse(sort_by(imageDetails,&imagePushedAt))[:5].[imageDigest,imagePushedAt]' --output table

# 2. Deploy the previous digest explicitly. Do not rebuild: a rebuild is a new artefact.
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 deploy --all \
  -c env_name="$ENV" -c image_digest="<registry>/marketing-ai@sha256:<previous>" \
  --require-approval broadening

# 3. Watch the replacement finish, then confirm what answers.
aws ecs describe-services --cluster "marketing-ai-$ENV" --services "marketing-ai-$ENV-api" \
  --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount}'
curl -fsS "$SERVICE_URL/healthz"
```

**Why a digest and not a tag.** A tag can be moved — by a later build, or by a person — after it has
been tested, so a deployment that names a tag cannot say what it is running. A digest is the
content. ECR tags here are immutable and a window of untagged images is retained, so the previous
digest is still there to name. `make aws-deploy` always passes a digest; `make infra-synth` warns
about `latest` because it has none, which is normal during a synth and never normal during a
deployment.

**What a rollback does not undo: a database migration.** The schema stays where it is. This is why
migrations are written additively — add a column, deploy the image that uses it, drop the old column
in a *later* migration once every task is running the new image. A rolled-back image meets a schema
one step ahead of it and must still work; if it does not, the migration was destructive and the
repair is forward, not back. `.venv/bin/alembic current` says where the schema is.

Nothing else is undone either: artefacts written by the bad version stay written, rows it inserted
stay inserted, and a run it failed stays failed.

---

## 7. Scaling

**Symptom.** The API is slow under load, or runs sit at "Waiting for compute" on the Running screen.

**What it means.** Two independent limits, and they are not the same knob.

| Limit | Where it is set | Default | Effect |
|---|---|---|---|
| ECS service size | `api_min_tasks`, `api_max_tasks` (CDK context) | 1 and 3 | The service scales on CPU toward a 60% target, with a 120-second cooldown either way. The range is the knob; the policy is not |
| Task size | `api_cpu`, `api_memory` | 2048 / 8192 | A pair Fargate does not offer is refused at synth time, not at deploy time |
| Concurrent jobs | `sagemaker_max_concurrent_jobs` | 2 | How many remote jobs may run at once before a run waits |
| Job size | `sagemaker_train_instance_type`, `sagemaker_processing_instance_type` | none, deliberately | An instance type is a cost decision; this repository does not take one for a customer |

**"Waiting for compute" is not an error.** It is the detail line on a run held back by
`sagemaker_max_concurrent_jobs`, and the run's state stays `pending` — which is what it is. There is
no `queued` state in this product, on purpose: one state vocabulary covers runs, stages and jobs, and
a backend-specific member would leak into every screen that renders one.

**First three things.**

```bash
# 1. Which limit is the one being hit: tasks, or jobs.
aws ecs describe-services --cluster "marketing-ai-$ENV" --services "marketing-ai-$ENV-api" \
  --query 'services[0].{desired:desiredCount,running:runningCount,events:events[:3].message}'
aws ssm get-parameter --name "/marketing-ai/$ENV/sagemaker_max_concurrent_jobs" \
  --query 'Parameter.Value' --output text

# 2. Raise the one that is. A parameter takes effect for a task that starts AFTERWARDS.
aws ssm put-parameter --name "/marketing-ai/$ENV/sagemaker_max_concurrent_jobs" \
  --value 4 --type String --overwrite
aws ecs update-service --cluster "marketing-ai-$ENV" --service "marketing-ai-$ENV-api" \
  --force-new-deployment

# 3. Task count and task size are stack context, not a parameter: redeploy.
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 deploy \
  "marketing-ai-$ENV-compute" -c env_name="$ENV" -c image_digest="$(cat ../.image-digest)" \
  -c api_max_tasks=6 --require-approval broadening
```

`Settings` is read once at startup, so every parameter change is a change for the *next* task or the
*next* job, never for one already running. Raising the concurrency cap raises spend in exactly the
proportion you raised it; measure the next runs' `compute` and `cost_estimate` and write down what
you measured, with the date, or write down nothing.

---

## 8. Backups and restore

**Symptom.** Something is gone: a table, a run's artefacts, or both.

**What it means.** Two mechanisms, neither of which is a copy of this deployment somewhere else.

| | Mechanism | What it protects against | What it does not |
|---|---|---|---|
| Database | RDS automated backups, `db_backup_retention_days` (7 by default, never 0) | A bad write, a dropped table, a bad migration | The account, the region |
| Artefacts | S3 object versioning on the artefact bucket | An overwrite or a delete of an object | The bucket being destroyed |

A restore of the database creates a **new instance**. It never overwrites the running one.

**First three things.**

```bash
# 1. Find out what you can restore to, before deciding anything.
aws rds describe-db-instances --db-instance-identifier "marketing-ai-$ENV" \
  --query 'DBInstances[0].{earliest:EarliestRestorableTime,latest:LatestRestorableTime}'
aws s3api list-object-versions --bucket "$BUCKET" --prefix "runs/$RUN_ID/" \
  --query 'Versions[].{Key:Key,Id:VersionId,When:LastModified}' --output table

# 2. Restore the OBJECTS first (see below), one version at a time.
aws s3api get-object --bucket "$BUCKET" --key "runs/$RUN_ID/run.json" \
  --version-id "<version>" /tmp/run.json
aws s3api put-object --bucket "$BUCKET" --key "runs/$RUN_ID/run.json" --body /tmp/run.json

# 3. Then the database, into a new identifier.
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier "marketing-ai-$ENV" \
  --target-db-instance-identifier "marketing-ai-$ENV-restore" \
  --restore-time 2026-10-14T09:00:00Z --no-publicly-accessible
```

**The order is bucket first, then database, and the reason is the direction the pointers run.** A
model registry row names files under `models/<use case>/<version>/`; a run index row names a run
directory. Rows point at objects; objects point at nothing. Restore the database to a moment when
the bucket is behind it and you have a champion whose predictor does not exist — which fails at
scoring time, in front of a user. Restore the bucket first, to at least as recent a moment, and the
worst case is an object nothing references yet, which is invisible and harmless.

Then, in this order: point the deployment at the restored instance by recomposing the application
secret (deploy the database stack again — see `docs/AWS_DEPLOYMENT.md`, section "The database will
not connect, or connects with the wrong password"), restart the service so tasks re-read it, and
rebuild the run index (section 5) so the list page agrees with the bucket again.

Two things to know before the restore, not during it. A restored instance comes up with a **default
parameter group**, so `rds.force_ssl` is not set on it until the deployment's parameter group is
applied — connections the deployment refuses today would be accepted by the restored one. And the
**model registry rows are not rebuildable from S3**: `scripts/reconcile_runs.py` rebuilds the run
index because `run.json` is authoritative, but champion status, approvals and promotion notes exist
only in the database. That asymmetry is the whole reason the database restore has to be right.

---

## 9. Alarms

Alarms arrive from the SNS topic `marketing-ai-<env>-alarms`, whose ARN is the `AlarmTopicArn`
output of the observability stack. A deployment that named no `alert_email` raises alarms nobody is
told about, and the synth warns about exactly that.

**Every threshold below is POLICY, CHOSEN BEFORE ANY TRAFFIC EXISTS.** Nothing in this repository
has measured a run, a stage or a bill. Where a threshold could not be chosen honestly at all it is
left as a substitution and the alarm **is not created** — the synth prints which ones, and
`infra/observability/alarms.json` carries the argument for each in its `rationale`, which is copied
into the alarm's own description so the person woken by it reads the reasoning and not just a
number.

```bash
aws sns list-subscriptions-by-topic --topic-arn \
  "$(aws cloudformation describe-stacks --stack-name "marketing-ai-$ENV-observability" \
     --query "Stacks[0].Outputs[?OutputKey=='AlarmTopicArn'].OutputValue" --output text)"
aws cloudwatch describe-alarms --alarm-name-prefix "marketing-ai-$ENV" \
  --query 'MetricAlarms[].[AlarmName,StateValue,Threshold,AlarmDescription]' --output table
```

### 9.1 `marketing-ai-<env>-no-healthy-targets`

**What fired.** `HealthyHostCount` behind the load balancer was below 1 for three one-minute
periods. Missing data is treated as breaching.

**What it means.** Nothing is answering `/healthz`. This is the service being down. It is not a
tuning threshold and there is no opinion in it: zero healthy targets is zero.

**First three things.**

```bash
# 1. Why the tasks stopped. stoppedReason names the image, the log driver, or the process.
aws ecs describe-tasks --cluster "marketing-ai-$ENV" \
  --tasks $(aws ecs list-tasks --cluster "marketing-ai-$ENV" --desired-status STOPPED \
            --query 'taskArns[:3]' --output text) \
  --query 'tasks[].{stopped:stoppedReason,exit:containers[0].exitCode}'
# 2. What the process said before it died. A settings refusal names a field and never a value.
aws logs tail "/marketing-ai/$ENV/api" --since 30m
# 3. If the last deployment caused it, roll the digest back (section 6).
```

No log lines **at all** usually means the task could not start its log driver, which is a network
path, not the application: see `docs/AWS_DEPLOYMENT.md`, section "The Fargate task starts,
stops, and leaves no logs at all".

### 9.2 `marketing-ai-<env>-runs-failed`

**What fired.** One or more `RunsFailed` datapoints in a five-minute period.

**What it means.** A run ended in a coded failure. The threshold is 1, for one period, on one
datapoint — POLICY, CHOSEN BEFORE ANY TRAFFIC EXISTS. A failed run is an event, not a rate: with no
history there is no rate to compare against, and the only defensible statement today is "say
something the first time this happens". Missing data is not breaching, because a deployment with no
runs is idle rather than broken.

**First three things.**

```bash
# 1. Which run. The list route has no state filter; read the records.
curl -s "$SERVICE_URL/runs?limit=20" \
  | python -c 'import json,sys; [print(r["run_id"], r["state"], r.get("use_case_id")) \
for r in json.load(sys.stdin)["runs"]]'
# 2. Its code and stage — section 3.
# 3. Decide whether it is an incident at all: a validation refusal is not (section 10).
```

Expect this alarm to be noisy until the deployment has enough history to say what a normal failure
rate is. When it does, change the threshold *and write down what you measured beside it*.

### 9.3 `marketing-ai-<env>-stage-duration`

**What fired.** Average `StageDurationSeconds` above the threshold for three consecutive five-minute
periods.

**What it means.** Stages are taking longer than this deployment decided they should. **The threshold
itself is not in this repository**: how long a stage should take is a measurement, nobody here has
run one against real customer data, and a number written in would be read as though it had been
measured. The deployment supplies it with
`-c alarm_thresholds=StageDurationSecondsAlarmThresholdSeconds=<seconds>`, and until it does, this
alarm does not exist. Three of three periods is POLICY, CHOSEN BEFORE ANY TRAFFIC EXISTS: one slow
stage is a big dataset, three in a row is a problem.

**First three things.**

```bash
# 1. Which stage, over what window.
aws cloudwatch get-metric-statistics --namespace MarketingAI --metric-name StageDurationSeconds \
  --dimensions Name=Env,Value="$ENV" Name=ClientId,Value=unset \
  --start-time "$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 300 --statistics Average Maximum
# 2. The runs in that window, and the size of the files they were given.
curl -s "$SERVICE_URL/runs?limit=10" | python -m json.tool
# 3. The job's billable time, which says whether the time was compute or waiting.
aws sagemaker describe-training-job --training-job-name "$JOB" \
  --query '{billable:BillableTimeInSeconds,started:TrainingStartTime,ended:TrainingEndTime}'
```

A bigger file than the deployment has seen before is the ordinary cause, and the answer is a bigger
instance type or a longer threshold — both of which are decisions, taken with the measurement in
hand. `ClientId` is `unset` unless this deployment set `client_id`.

### 9.4 `marketing-ai-<env>-job-cost`

**What fired.** `JobCostUsd` summed over one day exceeded the daily figure this deployment was given.

**What it means.** Estimated spend on jobs crossed a line somebody drew. Two things about that line.
It is a **budget**, a business decision nobody stated to this repository, so it is left as the
substitution `JobCostUsdDailyBudgetUsd` and the alarm is not created until a deployment supplies it.
And the metric is derived from AWS **list prices**, never from a bill: it ignores savings plans, free
tier, spot, tax and any negotiated discount. This is a tripwire on estimated spend. The one-day
period is POLICY, CHOSEN BEFORE ANY TRAFFIC EXISTS — a budget is stated per day, so it is evaluated
per day.

**First three things.**

```bash
# 1. What ran. Sum the estimates the runs themselves recorded.
curl -s "$SERVICE_URL/runs?limit=50" \
  | python -c 'import json,sys; [print(r["run_id"], r["state"]) for r in json.load(sys.stdin)["runs"]]'
curl -s "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" \
  | python -c 'import json,sys; m=json.load(sys.stdin); print(m["compute"]); print(m["cost_estimate"])'
# 2. What was actually billed, which is a different question and lags by up to 24 hours.
aws ce get-cost-and-usage --region us-east-1 \
  --time-period Start=2026-10-14,End=2026-10-15 --granularity DAILY --metrics UnblendedCost \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon SageMaker"]}}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE
# 3. If it was one run, find out why it was expensive before changing a limit.
aws sagemaker describe-training-job --training-job-name "$JOB" \
  --query '{instance:ResourceConfig.InstanceType,billable:BillableTimeInSeconds}'
```

`estimated_usd` and a Cost Explorer figure answer different questions and will not agree; when they
diverge, the list price is not wrong. `docs/AWS_DEPLOYMENT.md`, section "Why a list price is not a
bill", is the long version.

### 9.5 The AWS budget notification

**What fired.** The monthly AWS budget crossed one of its two notification thresholds. This is not a
CloudWatch alarm and does not come from the topic above.

**What it means.** The thresholds are percentages of the operator's own number, so they carry no
claim about what anything costs. A budget exists only if somebody passed `-c monthly_budget_usd` and
an `-c alert_email`; a budget with nobody to tell is refused at synth time.

**First three things.** Cost Explorer grouped by service for the month; then by usage type for the
service that grew; then decide whether the budget or the deployment is the thing that should change.
If the budget reports nothing at all, cost-allocation tags have not been activated in Billing and
Cost Management — CloudFormation cannot do it, and it takes up to 24 hours to take effect.

---

## 10. Things that are NOT incidents

| You see | Why it is working as designed |
|---|---|
| A **drift warning** on a scoring run | Drift is a reported signal and never a gate (DEC-051). A run whose data has shifted is still scored, reported and logged at WARNING, because refusing to score would leave the user with no numbers exactly when the data changed |
| `drift not measured` instead of a verdict | There was no comparison to make: no stored baseline, no rows, or no features in the baseline. The arithmetic would have answered anyway — an empty frame produces the largest PSI the metric can produce — so the engine reports the absence instead of a number about data that is not there (DEC-051) |
| A run refused with **409** `VALIDATION_FAILED` | Validation runs synchronously before a run is created, and the whole report comes back with the response so the Setup screen can list the problems inline. No run directory exists, nothing was spent, and the user has something to fix |
| A **409** naming a champion or a model version | `CHAMPION_NOT_FOUND`, `MODEL_USE_CASE_MISMATCH`. The model was resolved *before* the run was accepted, so a scoring run against a foreign or missing model is refused up front instead of failing halfway through |
| **The champion did not change** after a successful training run | The new version did not beat the re-scored champion, so it stays `candidate`. Training succeeded; the comparison decided. A version that *did* beat it stops at `pending_approval` when the use case requires approval |
| `estimated_usd: null` on a finished run | Null is never zero here. It means the figure cannot be stated honestly: no billable time was reported, or there is no published rate for that instance in that region. A fabricated zero would be indistinguishable from a real measurement of free compute; `basis` says which case this is |
| `LlmCostUsd` with no datapoints, ever | The generative phase is not in this repository. The metric is a hook and reads nothing |
| An empty `/marketing-ai/<env>/jobs` log group | Job containers write to SageMaker's own groups. Section 1 |
| `make infra-synth` warning about `latest` | Normal without an image digest. Not normal during a deployment, which always passes one |

---

## 11. What this runbook cannot tell you yet

No AWS account exists behind this repository. Nothing here has been deployed, and so nothing here
has been measured. Everything in this list needs a first real deployment to establish, and until it
is established it is honestly absent rather than quietly guessed:

- **Real alarm thresholds.** `StageDurationSecondsAlarmThresholdSeconds` and
  `JobCostUsdDailyBudgetUsd` have no committed values, so those two alarms do not exist yet. The
  `RunsFailed` threshold of 1 is a policy chosen with no history, and is expected to be wrong in one
  direction or the other.
- **Real cost.** Idle cost per month and cost per run are both unmeasured; the tables and the exact
  commands that fill them are in `docs/AWS_DEPLOYMENT.md`, section "What it costs".
- **Real startup time.** The health-check grace period is 120 seconds, chosen before anyone timed a
  cold start of this image. Whether it is generous or tight is a measurement nobody has taken.
- **Whether the EMF metrics actually appear.** `metrics_backend=emf` writes metric documents to
  stdout and CloudWatch is expected to extract them. That path has been tested against a fake sink,
  never against CloudWatch. Confirm `RunsStarted` appears in the `MarketingAI` namespace on the
  first deployment before trusting any dashboard built on it.
- **Whether the alarm email carries the description.** The `rationale` from
  `infra/observability/alarms.json` is put into each alarm's description precisely so the person
  woken at three in the morning reads the argument. Whether SNS's email body actually includes it
  has not been observed.
- **Whether the container runs end to end.** One layer of the image — the `libgomp1` install
  LightGBM needs — could not be built where the image was authored, because that session's egress
  policy refuses `deb.debian.org`. See `docs/AWS_DEPLOYMENT.md`, section
  "`OSError: libgomp.so.1: cannot open shared object file`".
- **Whether reconciliation fires in anger.** It has been exercised against an in-process fake. The
  first time it matters will be the first time a real job dies.

When one of these becomes known, write the number down **with the command that produced it and the
date it was produced**, or do not write it down at all.
