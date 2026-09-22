# AWS deployment

This document answers one question: how does an engineer with a fresh AWS account get this product
running in `ap-south-1`, and what does it cost. It is written to be followed rather than read. The
Phase 4a acceptance test is that somebody who has never opened this repository reaches a scored file
in under an hour without editing a line of code, so almost every paragraph below is a command, the
output it prints when it worked, or the reason a step exists at all.

One deployment serves one customer. Single tenant: one AWS account, one region, one bucket, one
database, one load balancer. That is the model plan section 14 assumed for Phase 4 and it is what
the stacks in `infra/` build. A shared multi-tenant deployment is a different product and nothing
here is a step towards one.

Section 6 contains no cost figure. That is deliberate, it is explained where it happens, and it is
enforced by `tests/unit/test_docs_honesty.py`.

> **What is in the tree.** Everything this document tells you to run is in the repository, with two
> exceptions, both named where they come up: `scripts/smoke_deployment.py`, which
> `.github/workflows/deploy-dev.yml` invokes as its last step (§4.4), and `infra/README.md`, which
> `infra/network.py` and `infra/database.py` point at (§10.6). Where this document would otherwise
> describe something that is not here, it says so instead.

---

## 1. What this deploys

```
                         internet
                             |
                      (443, or 80 in dev)
                             |
         +-------------------v------------------+     public subnets
         |     Application Load Balancer        |     infra/compute.py
         +-------------------+------------------+
                             | port 8000, security group to security group
         +-------------------v------------------+     private subnets, no public IP
         |  ECS Fargate service                 |     infra/compute.py
         |  one image, `entrypoint.sh serve`    |     Dockerfile target `api`
         +--+--------+-----------+-----------+--+
            |        |           |           |
            |        |           |           +--> SSM Parameter Store /marketing-ai/<env>/*
            |        |           |                written by infra/compute.py
            |        |           |                Secrets Manager     marketing-ai/<env>/app
            |        |           |                written by infra/database.py
            |        |           |
            |        |           +--> SageMaker training / processing job
            |        |                the SAME image, `train` / `job`
            |        |                execution role from infra/sagemaker.py
            |        |
            |        +--> RDS for PostgreSQL 16, private, TLS-only
            |             run index and model registry, infra/database.py
            |
            +--> S3 artefact bucket, SSE-KMS, versioned, public access blocked
                 uploads/  runs/  models/  _bootstrap/      infra/storage.py

   KMS customer-managed key      ECR repository, immutable tags, scan on push
   CloudWatch log groups, metric filters, dashboard, alarms, SNS topic, optional budget
```

The API and the training job run **the same image**; only the first argument differs.
`scripts/entrypoint.sh` understands `serve`, `train`, `job` and `migrate`, and anything it does not
recognise it execs as a command. So what runs inside a SageMaker job is what runs on a laptop under
`docker compose`, and there is no second build to keep in step.

Seven CloudFormation stacks deploy in one direction, declared in `infra/app.py`:

```
network -> storage -> observability -> database -> sagemaker -> compute -> budgets
```

Two of those edges are invisible in the resources. **Observability before database**, because RDS
creates `/aws/rds/instance/<id>/postgresql` itself with no retention the first time it exports a
log, and whoever creates a log group first decides how long its contents are kept. **SageMaker
before compute**, because the task role's `iam:PassRole` has to name an execution role that already
exists. Nothing points backwards: a cycle between stacks is not a slow deployment, it is a
deployment that cannot be performed.

---

## 2. Prerequisites

| | What | Why |
|---|---|---|
| Account | One AWS account for this deployment, region `ap-south-1` | The tenancy boundary is the account, not a row filter |
| Toolchain | Python 3.11, Node 22, Docker with buildx, GNU make | `infra/` drives the CDK CLI through `npx`; the image is built with `docker buildx` |
| CDK | CLI `aws-cdk@2.1142.0` (pinned in the Makefile), `aws-cdk-lib==2.270.0` and `cdk-nag==2.38.2` (pinned in `pyproject.toml`) | §10.1 is why cdk-nag is held back |
| Bootstrap | `cdk bootstrap aws://<account>/ap-south-1`, once per account and region | CDK needs its own staging bucket and roles before any stack can deploy |
| ECR | One repository named `marketing-ai` | `infra/storage.py` creates it; `scripts/build_push_image.sh` pushes into it. §2.2 is the ordering problem that creates |
| Bedrock | Nothing to do. Model access is **reserved, not used** | §3.4 |
| TLS | For `env_name=prod`: an ACM certificate in `ap-south-1` and a domain name | `AppContext.validate` refuses a prod synthesis without both |

Versions matter in one direction only: the CDK CLI, `aws-cdk-lib` and `cdk-nag` are pinned together
and a newer one of any of them is not an upgrade you get for free (§10.1). Python 3.11 is the
version the image is built on and the one `mypy --strict` is configured for.

### 2.1 What the deploying principal needs

The deploying principal is a human or a CI role, and it is **not** the identity the product runs as.
It needs enough to create everything in §1 and to assume the CDK bootstrap roles: CloudFormation,
S3, KMS, ECR, EC2 and VPC, RDS, Secrets Manager, SSM Parameter Store, ECS, ELB, IAM role and policy
creation, CloudWatch Logs, SNS, Budgets, and `sts:AssumeRole` on
`arn:aws:iam::<account>:role/cdk-*`.

In an account the engineer owns outright, the account administrator policy is the honest answer for
a first deployment. A narrower deploy role is real work and nothing in this repository claims to
have derived one; it is Phase 4b.

`.github/workflows/deploy-dev.yml` assumes its role by OIDC and holds no long-lived key. It reads
`vars.DEPLOY_ROLE_ARN` and `vars.ECR_REPOSITORY` from the GitHub environment named after the
deployment, and it is `workflow_dispatch` only — a push to a branch has no business changing
somebody's infrastructure.

### 2.2 The one ordering problem, and the way round it

`scripts/build_push_image.sh` pushes to an ECR repository, and `infra/storage.py` is what creates
that repository. On a genuinely empty account, deploy the storage stack on its own first:

```bash
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 \
  deploy marketing-ai-dev-storage -c env_name=dev
```

Its `RepositoryUri` output is the value of `ECR_REGISTRY/ECR_REPOSITORY`. After that one command,
`make aws-deploy ENV=dev` is the whole loop forever, because the repository already exists — which
is also why the repository is `RETAIN` even in a dev deployment (§8.2).

---

## 3. Configuration

`engine/settings.py` holds one frozen `Settings` object and it is the operator's entire
configuration surface. There is no config file to edit on a server and no code to change: every
field below is filled from an SSM parameter, the application secret, or an environment variable.

### 3.1 Precedence

Lowest first (DEC-301):

1. **SSM Parameter Store**, `/marketing-ai/<env>/<field>` — what `cdk deploy` writes.
2. **Secrets Manager**, the JSON document `marketing-ai/<env>/app`, whose keys are field names.
3. **The process environment**, `MARKETING_AI_*`.
4. **Explicit overrides**, passed in code or by a test.

The environment beating AWS is deliberate: it is what lets an operator override one value on one
task without editing a parameter. It is also the trap in the other direction, which is why the task
definition sets exactly three variables — `MARKETING_AI_SETTINGS_SOURCE=aws`, `MARKETING_AI_ENV` and
`MARKETING_AI_AWS_REGION`. A task definition that also set `MARKETING_AI_S3_BUCKET` would not disagree
with the parameter of the same name; it would silently win over it, for ever.

`GetParametersByPath` is called with `Recursive=False`, so a parameter must sit exactly one level
under `/marketing-ai/<env>/`. A parameter written deeper is stored, looks right in the console, and
is never read. `infra/naming.ssm_parameter_name` refuses to write one, which is the same check from
the other side.

### 3.2 The full field table

`<env>` is the deployment name: `local`, `dev`, `staging` or `prod`. Every SSM path is
`/marketing-ai/<env>/<field>`, and the leaf may also be spelled as the environment-variable name
(`S3_BUCKET` or `MARKETING_AI_S3_BUCKET` both resolve to `s3_bucket`). "Needed by" names the backend
that refuses to start without it. "Written by `cdk deploy`" marks the parameters
`infra/compute.py` publishes for you — everything else is a default you may override.

| Field | Environment variable | SSM parameter | Default | Needed by | Written by `cdk deploy` |
|---|---|---|---|---|---|
| `env` | `MARKETING_AI_ENV` | — it selects the path | `local` | always | task definition |
| `aws_region` | `MARKETING_AI_AWS_REGION` | `aws_region` | none | s3, sagemaker, bedrock | task definition |
| `config_dir` | `MARKETING_AI_CONFIG_DIR` | `config_dir` | the checkout's `configs/` | — | no |
| `storage_backend` | `MARKETING_AI_STORAGE_BACKEND` | `storage_backend` | `local` | always | yes, `s3` |
| `data_dir` | `MARKETING_AI_DATA_DIR` | `data_dir` | `data` | local storage | no |
| `s3_bucket` | `MARKETING_AI_S3_BUCKET` | `s3_bucket` | none | s3 storage | yes |
| `s3_prefix` | `MARKETING_AI_S3_PREFIX` | `s3_prefix` | `""` | — | no |
| `s3_kms_key_id` | `MARKETING_AI_S3_KMS_KEY_ID` | `s3_kms_key_id` | none | — (an identifier, not a secret) | yes |
| `download_url_ttl_seconds` | `MARKETING_AI_DOWNLOAD_URL_TTL_SECONDS` | `download_url_ttl_seconds` | `900`, between 60 and 3600 | s3 storage | no |
| `local_cache_dir` | `MARKETING_AI_LOCAL_CACHE_DIR` | `local_cache_dir` | a temp directory | s3 storage | no |
| `metadata_backend` | `MARKETING_AI_METADATA_BACKEND` | `metadata_backend` | `sqlite` | always | yes, `postgres` |
| `postgres_dsn` | `MARKETING_AI_POSTGRES_DSN` | **the secret only**, §3.3 | none | postgres | yes, into the secret |
| `postgres_schema` | `MARKETING_AI_POSTGRES_SCHEMA` | `postgres_schema` | the connection's search path | — | yes |
| `job_backend` | `MARKETING_AI_JOB_BACKEND` | `job_backend` | `thread` | always | yes, `sagemaker` |
| `job_max_workers` | `MARKETING_AI_JOB_MAX_WORKERS` | `job_max_workers` | `2`, at least 1 | thread jobs | no |
| `sagemaker_role_arn` | `MARKETING_AI_SAGEMAKER_ROLE_ARN` | `sagemaker_role_arn` | none | sagemaker | yes |
| `sagemaker_image_uri` | `MARKETING_AI_SAGEMAKER_IMAGE_URI` | `sagemaker_image_uri` | none | sagemaker | yes, the digest |
| `sagemaker_instance_type` | `MARKETING_AI_SAGEMAKER_INSTANCE_TYPE` | `sagemaker_instance_type` | none, on purpose | sagemaker | yes, from `-c sagemaker_instance_train` |
| `sagemaker_processing_instance_type` | `MARKETING_AI_SAGEMAKER_PROCESSING_INSTANCE_TYPE` | `sagemaker_processing_instance_type` | none, on purpose | the score job | yes, from `-c sagemaker_instance_process` |
| `sagemaker_instance_count` | `MARKETING_AI_SAGEMAKER_INSTANCE_COUNT` | `sagemaker_instance_count` | `1` | sagemaker | no |
| `sagemaker_volume_size_gb` | `MARKETING_AI_SAGEMAKER_VOLUME_SIZE_GB` | `sagemaker_volume_size_gb` | unset, so the service default applies | — | no |
| `sagemaker_max_runtime_seconds` | `MARKETING_AI_SAGEMAKER_MAX_RUNTIME_SECONDS` | `sagemaker_max_runtime_seconds` | unset, so the service default applies | — | no |
| `sagemaker_max_concurrent_jobs` | `MARKETING_AI_SAGEMAKER_MAX_CONCURRENT_JOBS` | `sagemaker_max_concurrent_jobs` | `2`, at least 1 | sagemaker | yes, from `-c max_concurrent_jobs` |
| `sagemaker_subnet_ids` | `MARKETING_AI_SAGEMAKER_SUBNET_IDS` | `sagemaker_subnet_ids` | empty, meaning no VPC configuration | — | yes, the app subnets |
| `sagemaker_security_group_ids` | `MARKETING_AI_SAGEMAKER_SECURITY_GROUP_IDS` | `sagemaker_security_group_ids` | empty | — | yes |
| `sagemaker_job_name_prefix` | `MARKETING_AI_SAGEMAKER_JOB_NAME_PREFIX` | `sagemaker_job_name_prefix` | `marketing-ai` | sagemaker — it is also the IAM boundary | yes |
| `llm_backend` | `MARKETING_AI_LLM_BACKEND` | `llm_backend` | `fake` | always | yes, from `-c bedrock_enabled` |
| `bedrock_model_id` | `MARKETING_AI_BEDROCK_MODEL_ID` | `bedrock_model_id` | none | `llm_backend=bedrock` | only when `-c bedrock_model_ids` is given |
| `log_level` | `MARKETING_AI_LOG_LEVEL` | `log_level` | `INFO` | — | yes |
| `log_format` | `MARKETING_AI_LOG_FORMAT` | `log_format` | `text` | the metric filters need `json` | yes, `json` |
| `metrics_backend` | `MARKETING_AI_METRICS_BACKEND` | `metrics_backend` | `none` | CloudWatch metrics need `emf` | yes, `emf` |
| `client_id` | `MARKETING_AI_CLIENT_ID` | `client_id` | none | cost-allocation tags and metric dimensions | only when given |
| `cors_origins` | `MARKETING_AI_CORS_ORIGINS` | `cors_origins` | `*` | refused on prod, §3.5 | yes |

Three fields are tuples filled from one comma-separated value: `sagemaker_subnet_ids`,
`sagemaker_security_group_ids` and `cors_origins`.

`aws_region` is read from `MARKETING_AI_AWS_REGION` and from nowhere else. It is **not** filled from
`AWS_REGION` or `AWS_DEFAULT_REGION`: those are whatever the host happens to export, and a
deployment that silently followed one of them would read a different account's Parameter Store
because of an environment variable nobody set deliberately.

Five `MARKETING_AI_*` names are deliberately **not** settings and are listed in `NON_FIELD_ENV_VARS`:
`MARKETING_AI_SETTINGS_SOURCE`, `MARKETING_AI_JOB_SPEC_KEY`, `MARKETING_AI_FAILURE_PATH`,
`MARKETING_AI_REQUIRE_POSTGRES` and `MARKETING_AI_TEST_DATABASE_URL`. The application ignores a
`MARKETING_AI_*` name it does not recognise; what that list is for is the deployment, where
`tests/infra/test_task_definition.py` fails the build if a task definition sets a `MARKETING_AI_*`
variable that is in neither that list nor the table above (DEC-304). Of those five, a task
definition sets exactly one.

The two instance-type fields have no default **on purpose**: an instance type is a cost decision and
this repository does not take cost decisions on a customer's behalf. The deployment supplies its
choice through CDK context (`-c sagemaker_instance_train`, `-c sagemaker_instance_process`), whose
own defaults live in one place, `infra/context.py`, next to every other knob.

### 3.3 The one secret

`postgres_dsn` is the only `SecretStr` in the model and the only value that is not in Parameter
Store. It lives in the Secrets Manager document `marketing-ai/<env>/app`, whose keys are `Settings`
field names, and the task role may read that one ARN and nothing else.

The database's own credential is a **separate** secret, `marketing-ai/<env>/db`. The task can never
read it. That is the secret RDS knows about and the one the AWS single-user rotation function
rewrites.

`engine.settings.summary()` and `redacted()` are built from an allow-list of non-secret fields, and
`SettingsError` names the variable to export and never its value, so a wrong URL cannot escape
through a log line or a traceback.

The URL in `marketing-ai/<env>/app` is a **snapshot composed at deploy time** (DEC-376). The rotation
function changes the password in the credential secret and knows nothing about the application
document, so after a rotation the URL carries the previous password until the database stack is
deployed again. §10.5 is what that looks like from the outside.

### 3.4 Bedrock: two knobs, and which one is IAM

`llm_backend` is the application's switch: `fake` (the default) or `bedrock`. `bedrock_model_id` is
the one model completions go to, and `Settings` refuses `llm_backend=bedrock` without it and without
`aws_region`.

The CDK has a *third* knob that is not a setting: `-c bedrock_model_ids`, a comma-separated list.
That list is an IAM question rather than a configuration one — a role may legitimately be allowed to
invoke several models (a generation model, an embedding model, a judge) while the engine sends
completions to one — so it is what `infra/policies.py` scopes `bedrock:InvokeModel` to, and the
first entry is what the deployment writes into `bedrock_model_id`.

A deployment calls Bedrock as its task role and nothing else. The AWS connection screen
(`#/generative/connection`) is read-only on every deployment: it can confirm that the role reaches
Bedrock and which configured models it may use - masked, and without invoking one - but it cannot
point the deployment at another identity, and it never stores a key. Choosing a profile is a laptop
feature; see docs/GENERATIVE.md, section 9.

An empty list with `-c bedrock_enabled=true`, or `bedrock_enabled=false`, synthesises an explicit
`Deny` rather than an absent `Allow` (DEC-371). Requesting model access is a per-account, per-model
request with a lead time; it is in §2 so an operator who plans ahead knows that.

### 3.5 The three refusals

A deployment that is described wrongly fails at startup, loudly, rather than at the first request.

| Refusal | Code | What it means |
|---|---|---|
| An incomplete backend | `SETTING_REQUIRED` | `storage_backend=s3` with no `s3_bucket` or no `aws_region`; `metadata_backend=postgres` with no `postgres_dsn`; `job_backend=sagemaker` without the role, the image, the instance type and the region; `llm_backend=bedrock` without `bedrock_model_id` |
| `job_backend=sagemaker` without `storage_backend=s3` | `SETTING_REQUIRED` | A remote job cannot read a local disk, so this combination is a deployment that would fail at its first run (DEC-302) |
| `cors_origins` containing `*` when `env=prod` | `SETTING_REQUIRED` | DEC-024 left CORS open for a UI opened as a local file. That is a statement about a laptop, not about an internet-facing load balancer, so a prod deployment that never mentions `cors_origins` is refused rather than inheriting the laptop's answer (DEC-307) |
| A value the field cannot hold | `SETTING_INVALID` | `MARKETING_AI_JOB_MAX_WORKERS=nine`, a `download_url_ttl_seconds` outside 60…3600, an `s3_prefix` containing a `..` segment |

The message always names the environment variable to export and never its value (DEC-303).

A `MARKETING_AI_*` variable that matches no field is **ignored** by the application rather than
refused: a deployment must not fail to start because some other tool on the host exported a name in
that space. The check that a typo is caught is one layer out, at the deployment — see the note under
the field table.

`infra/context.py` refuses the same class of mistake one layer up: an unrecognised `-c` key is a
`ContextError` with a "did you mean" hint, because `-c db_storage_bg=50` otherwise synthesises
happily and quietly uses the default (DEC-365).

---

## 4. Deploy

Three commands, in this order. `ENV` is `dev` or `prod`.

### 4.1 `make infra-setup`

```bash
make infra-setup
```

Creates `.venv-infra` and installs the `deploy` extra plus `infra/requirements.txt`. The CDK app has
a virtualenv of its own because `aws-cdk-lib` pulls jsii and a Node bridge that the engine must
never depend on, and `make lint` has to keep working in a checkout that never installed it
(DEC-364).

It prints pip's progress and nothing else. When it worked,
`.venv-infra/bin/python -c "import aws_cdk"` succeeds.

Before you deploy anything, synthesise offline. None of this needs an account:

```bash
make infra-test          # the CloudFormation assertions and the snapshots
make infra-synth ENV=dev # really runs `cdk synth`; needs node, not credentials
make infra-nag  ENV=dev  # the same synthesis with the AwsSolutions checks switched on
```

Both `make infra-synth` and `make infra-nag` print a warning that no `image_digest` was given and
the synthesis is therefore naming the repository's `latest` tag. **That warning is normal during a
synth and is not normal during a deployment**: `make aws-deploy` always passes a digest. A dev
synthesis also warns, in capitals, that the load balancer is HTTP-only when no certificate was
given. Both warnings are there to be read rather than suppressed.

### 4.2 `make aws-deploy ENV=dev`

Point the shell at the registry first, and log Docker into it:

```bash
export ECR_REGISTRY=<account>.dkr.ecr.ap-south-1.amazonaws.com
export ECR_REPOSITORY=marketing-ai
aws ecr get-login-password --region ap-south-1 \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"
```

```bash
make aws-deploy ENV=dev
```

Two steps in one target. `scripts/build_push_image.sh` builds the `api` target for `linux/amd64`,
pushes it, asks the **registry** which digest it stored — a manifest list and a single-platform
manifest have different digests, and only the registry knows which one the repository now points at
— and writes `<registry>/<repository>@sha256:...` to `.image-digest`. Then `cdk deploy --all` runs
with that digest in `-c image_digest=` and `--require-approval broadening`.

The digest is the point. A tag can be moved after it has been tested, by a later build or by a
person, so a deployment that names a tag cannot say what it is running. `AppContext.validate`
refuses an `image_digest` without `@sha256:` in it for exactly that reason.

The push prints:

```
building <registry>/marketing-ai:<short sha> for linux/amd64
pushed <registry>/marketing-ai@sha256:<64 hex>
wrote .image-digest
```

and the deployment prints one block per stack, in the order of §1, ending with the outputs:

| Stack | Output | What it is |
|---|---|---|
| network | `VpcId`, `EgressModel` | The VPC, and whether the private subnets reach AWS through a NAT gateway or through interface endpoints |
| storage | `BucketName`, `KeyArn`, `RepositoryUri` | The artefact bucket, its customer-managed key, and `ECR_REGISTRY/ECR_REPOSITORY` |
| observability | `AlarmTopicArn`, `GeneratedArtefacts` | Where alarms go, and which alarms were created — including any left uncreated for want of a threshold (§10.6) |
| database | `EndpointAddress`, `ApplicationSecretArn` | The private endpoint, and the one secret the task may read |
| sagemaker | `ExecutionRoleArn`, `JobsSecurityGroupId` | What a job runs as, and what it attaches to |
| compute | `ServiceUrl`, `ImageReference` | **Where the API answers**, and the digest it is running |
| budgets | `Budget` | The budget, or a sentence saying none was asked for — §6.4 |

`--require-approval broadening` means CDK stops and asks before any change that widens an IAM policy
or a security group. Read what it prints. That prompt is the review.

`-c env_name=prod` refuses to synthesise without both `-c certificate_arn=...` and
`-c domain_name=...`. That is not a missing feature: an HTTP-only load balancer puts every upload and
every pre-signed download URL on the public internet in clear text.

### 4.3 `make aws-bootstrap ENV=dev`

```bash
make aws-bootstrap ENV=dev
```

`cdk deploy` creates the database; it does not create the schema. And it cannot tell you whether the
task role can actually do what its policy appears to allow — a policy is a document, not a
demonstration. This command applies the Alembic migrations and probes every permission with one real
call each, reporting the whole list rather than stopping at the first failure.

Working output:

```
python -m scripts.aws_bootstrap: env=dev region=ap-south-1 storage_backend=s3 metadata_backend=postgres job_backend=sagemaker log_format=json metrics_backend=emf

  [     ok] configuration: 7 use cases load
  [     ok] storage: wrote, read and deleted s3://<bucket>/_bootstrap/permission-probe.txt
  [     ok] database: PostgreSQL 16.x
  [     ok] migrations: schema at head
  [     ok] jobs: may describe training jobs

5/5 checks passed; this deployment is ready
```

A failure prints `FAILED` with the exception's class — never its message, which routinely quotes the
resource and the principal — and a `fix:` line naming the IAM actions to grant. The process exits
non-zero. `--dry-run` says what it would check and stops; `--no-migrate` probes without touching the
schema.

The `jobs` probe describes a training job that does not exist. A role allowed to look gets
`ValidationException`, which is a pass; a role that is not gets `AccessDeniedException`, which is a
failure. Describing a job that does not exist is the only way to test the permission without
creating one and paying for it.

The first line is `engine.settings.summary()`, rendered from an allow-list of non-secret fields. It cannot
print the database URL.

### 4.4 The same three steps in CI

`.github/workflows/deploy-dev.yml` runs `make infra-setup`, `make aws-deploy`, `make aws-bootstrap`
and then a smoke test, under `workflow_dispatch` only, with a role assumed by OIDC and no long-lived
key anywhere. Its last step invokes `scripts/smoke_deployment.py`, **which is not in this repository
yet**. Until it is, run §5 by hand after a deployment, or dispatch the workflow with `skip_smoke`.

---

## 5. Verify

The acceptance walk-through. Nothing here is simulated, and every number on every screen was
computed by the run that produced it.

```bash
export SERVICE_URL=$(aws cloudformation describe-stacks --stack-name marketing-ai-dev-compute \
  --region ap-south-1 --query "Stacks[0].Outputs[?OutputKey=='ServiceUrl'].OutputValue" --output text)
curl -fsS "$SERVICE_URL/healthz"
```

1. **Open the URL.** `GET /healthz` answers `{"status": "ok", "version": "..."}` and deliberately
   carries no other detail. The load balancer's target group uses the same route, so a task that
   answers it is a task receiving traffic.
2. **Pick a use case.** Telecom → a lifecycle stage → a use case. The Setup screen is rendered from
   the merged config, so a use case that loads is a use case that works.
3. **Download the template.** The button on the Setup screen, or
   `GET /use-cases/{id}/template.csv`. The example rows show the file's format and are never
   presented as data.
4. **Upload a file.** `POST /uploads`. A validation failure comes back as a `409` naming the column,
   not as a stack trace.
5. **Run.** Choose the primary key and the target, keep every default, Run. The Running screen polls
   `GET /runs/{run_id}`, which reads `run.json` and `status.json`; both exist from the moment the
   `202` is returned.
6. **Read the result.** Data → Model → Output: the leaderboard, the evaluation, the decile lift
   table and the comparison against the baseline the engine trains alongside the model.
7. **Score a new file** with the champion and download `scores.csv` from
   `GET /runs/{run_id}/scores.csv`.

### 5.1 Confirming the training ran as a SageMaker job

In a deployment, `job_backend=sagemaker` and the run's work happens in a container on a training
instance, not in the API task's thread pool. Two things prove it, and they are worth looking at once
per account so the difference is familiar:

```bash
aws sagemaker list-training-jobs --name-contains marketing-ai --max-results 5 \
  --sort-by CreationTime --sort-order Descending \
  --query 'TrainingJobSummaries[].[TrainingJobName,TrainingJobStatus]' --output table --region ap-south-1

curl -s "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["compute"])'
```

`compute.backend` reads `sagemaker-training` rather than `thread`, and it carries the job name, the
instance type and the region. A run that is waiting for compute because
`sagemaker_max_concurrent_jobs` is reached shows as `pending` with "Waiting for compute" on its
first stage — `RunState` has no `queued` member, because a queue is not a state a run is in
(DEC-335).

Note that the role the *task* holds carries no `List*` action at all. The `list-training-jobs` call
above runs with your own credentials.

### 5.2 Where the run's cost is

`run_manifest.json` is one flat record per run, written for **every** outcome including a failure.
It carries `compute` (`ComputeInfo`) and `cost_estimate` (`CostEstimate`):

```bash
curl -s "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" \
  | python -c 'import json,sys; m=json.load(sys.stdin); print(m["compute"]); print(m["cost_estimate"])'
```

`ComputeInfo.billable_seconds` is filled only from `DescribeTrainingJob.BillableTimeInSeconds`, and
`billable_seconds_source` names that field, so a reader never has to guess which number they are
looking at. A processing job reports start and end times, which are wall clock, so its
`billable_seconds` stays null. `CostEstimate.estimated_usd` is null — never zero — whenever the
figure cannot be stated honestly, and `basis` is the sentence that has to be read with it.

**The UI does not show a cost.** Nothing under `ui/` reads `cost_estimate`, and neither
`GET /runs/{run_id}` nor any other route returns it; the artefact above is where the figure is. That
is a gap in the product rather than in this document, and §10.6 repeats it where somebody looking
for it will find it.

### 5.3 Finding the job's logs

Five log groups, and the one you want is not the one named after the product:

| What writes it | Log group |
|---|---|
| The API task | `/marketing-ai/<env>/api` |
| A **training** job's container | `/aws/sagemaker/TrainingJobs` |
| A **processing** job's container | `/aws/sagemaker/ProcessingJobs` |
| Postgres itself | `/aws/rds/instance/marketing-ai-<env>/postgresql` |
| Reserved for job output of ours | `/marketing-ai/<env>/jobs` |

SageMaker writes a job's container output to log groups **it** owns and creates, which is why the
execution role has `logs:CreateLogGroup` scoped to `/aws/sagemaker/*` and the Fargate task role has
none at all. The engine logs to stdout and SageMaker collects stdout, so
`/marketing-ai/<env>/jobs` — which exists, and which the execution role may write to — is normally
empty. That is not a lost job.

```bash
aws logs tail /aws/sagemaker/TrainingJobs --log-stream-name-prefix <job name> --since 2h --region ap-south-1
aws logs tail /marketing-ai/dev/api --since 30m --filter-pattern "$RUN_ID" --region ap-south-1
aws sagemaker describe-training-job --training-job-name <job name> --region ap-south-1 \
  --query '{status:TrainingJobStatus,reason:FailureReason,billable:BillableTimeInSeconds,instance:ResourceConfig.InstanceType}'
```

The API's logs are one JSON object per line, which is what `log_format=json` produces and what the metric
filters in the observability stack match on. **No data value ever reaches a log, on any path
including a failure path**: `log_stage` admits a stage name, a row count and a duration; `log_failure`
names an exception's class and not its message; and `RedactingFormatter` renders a traceback's frames
while withholding every exception message, because a library builds those out of the value that upset
it. If a failure cannot be diagnosed from a log line, that is the control working — reproduce it
against a synthetic file rather than loosening the formatter.

---

## 6. What it costs

**Nothing in this section has been measured.** There is no AWS account behind this repository, no
stack has ever been deployed from it, and no bill has been read. Every cell of both tables below
holds the literal marker `NOT YET MEASURED`, and each table is followed by the exact commands that
fill it in.

That is the harder choice and it is the right one. A plausible-looking figure in a deployment note
is the same mistake as a fabricated metric in the product (plan section 13.3): once it is written
down it gets quoted, it ends up in front of a customer, and nobody can say afterwards where it came
from. An empty cell with the command beside it is worth more than a number nobody can source.

### 6.1 Idle cost

What this deployment costs per month with no run at all: the load balancer, the minimum Fargate
task, the RDS instance and its storage, the NAT gateway or the interface endpoints, the KMS key,
CloudWatch ingestion and retention, and whatever has accumulated in S3.

#### Idle cost, per month

| Component | ap-south-1, dev | ap-south-1, prod |
|---|---|---|
| Application Load Balancer | NOT YET MEASURED | NOT YET MEASURED |
| Fargate task at `api_min_tasks` | NOT YET MEASURED | NOT YET MEASURED |
| RDS for PostgreSQL instance | NOT YET MEASURED | NOT YET MEASURED |
| RDS storage and automated backups | NOT YET MEASURED | NOT YET MEASURED |
| NAT gateway or interface endpoints | NOT YET MEASURED | NOT YET MEASURED |
| KMS customer-managed key | NOT YET MEASURED | NOT YET MEASURED |
| CloudWatch logs, metrics and alarms | NOT YET MEASURED | NOT YET MEASURED |
| S3 storage and requests | NOT YET MEASURED | NOT YET MEASURED |
| Secrets Manager and Parameter Store | NOT YET MEASURED | NOT YET MEASURED |
| **Total** | NOT YET MEASURED | NOT YET MEASURED |

To fill it: deploy, leave the deployment alone for a full billing day with no runs, then ask Cost
Explorer what it charged. Cost Explorer lags by up to 24 hours, so read it the day after.

```bash
aws ce get-cost-and-usage \
  --region us-east-1 \
  --time-period Start=2026-10-01,End=2026-11-01 \
  --granularity MONTHLY \
  --metrics UnblendedCost \
  --filter '{"And":[
      {"Dimensions":{"Key":"REGION","Values":["ap-south-1"]}},
      {"Tags":{"Key":"product","Values":["marketing-ai"]}},
      {"Tags":{"Key":"env","Values":["dev"]}}]}' \
  --group-by Type=DIMENSION,Key=SERVICE
```

Two things about that command. `aws ce` lives only in `us-east-1`, whatever region you deployed
into. And the `product` and `env` tags — put on every resource in every stack by `infra/app.py`,
plus `client` when `client_id` is set — **match nothing until they are activated** as cost-allocation
tags in Billing and Cost Management. CloudFormation cannot activate them, and activation takes up to
24 hours to take effect. Activate `product`, `env` and `client` immediately after the first
deployment, or the filter above returns an empty result that reads exactly like a free deployment.
Without the tags, drop the `Tags` clauses and group the whole account by service.

### 6.2 Per-run cost

What one run adds: the training job, the processing job, the S3 writes, and the storage the
artefacts then occupy.

#### Per-run cost

| Run | Billable seconds | Instance type | Estimated at list price | Cost Explorer, same day |
|---|---|---|---|---|
| Train, template-sized file | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Train, Telco Churn (7,043 rows) | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Score, same file | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |

The first three columns come from the run itself, not from a spreadsheet:

```bash
curl -s "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" \
  | python -c 'import json,sys; m=json.load(sys.stdin); print(m["compute"]); print(m["cost_estimate"])'
```

The last column is Cost Explorer narrowed to the day and the service, and it is the one that is a
bill:

```bash
aws ce get-cost-and-usage --region us-east-1 \
  --time-period Start=2026-10-14,End=2026-10-15 \
  --granularity DAILY --metrics UnblendedCost \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon SageMaker"]}}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE
```

CloudWatch is the third source and the only one that answers "how has this changed over time".
`engine/aws/metrics.py` publishes five metrics into the `MarketingAI` namespace when
`metrics_backend=emf`: `RunsStarted`, `RunsFailed`, `StageDurationSeconds`, `JobCostUsd` and
`LlmCostUsd`.

```bash
aws cloudwatch get-metric-statistics --namespace MarketingAI \
  --metric-name StageDurationSeconds --start-time 2026-10-14T00:00:00Z \
  --end-time 2026-10-15T00:00:00Z --period 3600 --statistics Sum Average Maximum \
  --region ap-south-1
```

`JobCostUsd` is emitted only where a real figure exists — never a zero standing in for a null.
`LlmCostUsd` will stay empty: the generative phase is not in this repository (§3.4).

### 6.3 Why a list price is not a bill

`configs/aws_prices.yaml` is generated by `make prices` from the public AWS price list, and it
records what it read and when. For `ap-south-1` it carries the offer file URL, the offer version
`20260921172504` and the publication date `2026-09-21T17:25:04+00:00`. Those are published **list
prices**: a fact about a rate card, not a measurement of anybody's bill. They ignore savings plans,
free tier, spot, taxes and every negotiated discount, and they go stale the moment AWS changes them.

`CostEstimate.basis` is what carries that distinction into the product. When `estimated_usd` is set,
it is billable seconds times instances times the published list rate, and `basis` says so in those
words, naming the rate, the offer version and the date it was published. When there is no rate for
that instance in that region, or the platform reported no billable time, or there is no rate card at
all, `estimated_usd` is **null rather than zero** — a fabricated zero would be indistinguishable
from a real measurement of free compute (DEC-330, DEC-333).

So the "estimated at list price" column and the Cost Explorer column of §6.2 are two different kinds
of thing, deliberately printed side by side. If they diverge, the list price is not wrong; it is
answering a different question.

### 6.4 A budget is the control; this table is not

`infra/budgets.py` creates a monthly AWS budget only when somebody names an amount, because nobody
in this repository knows what this deployment is allowed to cost:

```bash
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 deploy --all \
  -c env_name=dev -c image_digest="$(cat ../.image-digest)" \
  -c monthly_budget_usd=<your amount> -c alert_email=<you@example.com> \
  --require-approval broadening
```

`make aws-deploy` passes `env_name` and `image_digest` and nothing else, so any other context key is
supplied by calling `cdk deploy` directly, as above. The budget stack always writes
`/marketing-ai/cost/<env>/monthly-budget-usd`, holding either the amount or the word `none`, so the
answer to "did anyone set a budget here" does not depend on reading a console.

A budget given without `alert_email` is refused at synth time: a budget nobody is told about is a
number in a console, not a control. Its two notification thresholds are percentages of the
operator's own amount, so they carry no claim about what anything costs.

---

## 7. Upgrading

The unit of a deployment is an image digest and a schema revision. Roll them one at a time.

### 7.1 Migrate the database

```bash
MARKETING_AI_POSTGRES_DSN="postgresql://...?sslmode=require" make migrate
```

That is `alembic upgrade head`. From inside the deployment, the same thing runs as a one-off task
with the image's `migrate` entrypoint (`alembic -c /app/alembic.ini upgrade head`).
`make aws-bootstrap` also applies migrations, which is why it runs after `cdk deploy` and before the
smoke test.

Write migrations so that the **old** code can run against the **new** schema: add a column, deploy
the image that uses it, drop the old column in a later migration. ECS replaces tasks one at a time,
so a window in which both images are live is the normal case, not the exception.

### 7.2 Roll the image

```bash
make aws-deploy ENV=dev
```

The image is always named by digest. To roll back, deploy the previous digest explicitly rather than
rebuilding:

```bash
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 deploy --all \
  -c env_name=dev -c image_digest="<registry>/marketing-ai@sha256:<previous>" \
  --require-approval broadening
```

ECR tags are immutable and a rollback window of twenty untagged images is kept, so the previous
digest is still there. The ECS deployment is a rolling replacement behind the load balancer's health
check, and the target group only shifts traffic to a task that answers `/healthz`.

### 7.3 What is safe to do while serving

| Change | Safe while serving | Why |
|---|---|---|
| A new image digest | yes | Rolling replacement behind the health check |
| An additive migration | yes | Old and new code both read the old columns |
| A destructive migration | no | Do it once every task is running the image that stopped using the column |
| An SSM parameter value | yes, from the next task start | `Settings` is read once at startup; a running task keeps what it loaded |
| `cors_origins`, `log_level`, the instance types | yes, same caveat | They take effect for a task or a job that starts afterwards |
| The database instance class, or Multi-AZ | no | RDS restarts the instance and every task sees connection errors |
| Rotating the credential secret | see §10.5 | The application document holds a snapshot until the database stack is deployed again |

A run that is in flight when its task is replaced is **not** resumed. Cancellation is cooperative and
is observed between stages, so before a deployment either let running jobs finish or cancel them
with `POST /runs/{run_id}/cancel`.

---

## 8. Backups and teardown

### 8.1 What is backed up

| | Mechanism | Default | Where it is set |
|---|---|---|---|
| Database | RDS automated backups | `db_backup_retention_days`, 7 days | `-c db_backup_retention_days=` |
| Database | Snapshots carry the resource tags | always | `copy_tags_to_snapshot=True` |
| Artefacts | S3 object versioning on the artefact bucket | on | `infra/storage.py` |
| Artefacts | Incomplete multipart uploads abandoned after 7 days | on | `ABORT_INCOMPLETE_UPLOAD_DAYS` |
| Access logs | Expired after `log_retention_days` — 30 in dev, 365 in prod | on | `infra/context.py` |

`db_backup_retention_days` may not be set to zero. Zero disables automated backups, and a deployment
holding a customer's data has no business doing that silently.

Versioning is not a backup of the bucket to somewhere else. It protects against an overwrite or a
delete, not against the bucket being destroyed. A copy in another account is a Phase 4b question and
this repository does not create one.

### 8.2 What `cdk destroy` removes, and what it keeps

```bash
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 \
  destroy --all -c env_name=dev
```

On a **dev** deployment `removal_policy_destroy` is true, so the bucket, the key, the database and
the parameters are marked `DESTROY`. On **prod** they are `RETAIN` and survive the stack.

| Resource | dev | prod |
|---|---|---|
| Artefact bucket | Deleted **only if empty** — nothing empties it automatically | Retained |
| Access-log bucket | Same | Retained |
| KMS key | Scheduled for deletion after 30 days | Retained |
| RDS instance | Deleted, and its automated backups with it | Retained; `deletion_protection` refuses the delete |
| SSM parameters | Deleted | Retained |
| Secrets | Deleted after the recovery window | Retained |
| ECR repository | **Retained always**, dev included | Retained |
| CloudWatch log groups | Deleted with the observability stack | Retained |

Three of those are deliberate and are worth knowing before the command is typed.

Neither bucket has `auto_delete_objects`, and the ECR repository does not have `empty_on_delete`.
Both of those are custom-resource Lambdas with standing permission to empty a store, living in the
account for the lifetime of the deployment so that one manual teardown needs one fewer command. A
non-empty bucket refusing to delete is the correct outcome for a store holding a customer's data.

The ECR repository is retained **even in dev**, because CI pushes an image into it before
`cdk deploy` runs; a destroy that took it away would break the next deployment of the same
environment.

And the KMS key's pending window is the maximum AWS offers rather than the minimum, in dev too: a
deleted key is an unreadable bucket, and the mistake this protects against is noticing late.

### 8.3 Really deleting the data

Destroying the stacks is not deleting the data.

```bash
# 1. every version and every delete marker in the artefact bucket
aws s3api list-object-versions --bucket <bucket> --region ap-south-1 \
  --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' > versions.json
aws s3api delete-objects --bucket <bucket> --region ap-south-1 --delete file://versions.json
# repeat for DeleteMarkers[], then:
aws s3 rb "s3://<bucket>" --region ap-south-1

# 2. the database, keeping no final snapshot - only when that is what you mean
aws rds delete-db-instance --db-instance-identifier marketing-ai-dev --skip-final-snapshot \
  --delete-automated-backups --region ap-south-1

# 3. the secrets, with no recovery window
aws secretsmanager delete-secret --secret-id marketing-ai/dev/app \
  --force-delete-without-recovery --region ap-south-1
aws secretsmanager delete-secret --secret-id marketing-ai/dev/db \
  --force-delete-without-recovery --region ap-south-1

# 4. the key, which makes every remaining ciphertext unreadable
aws kms schedule-key-deletion --key-id <key arn> --pending-window-in-days 7 --region ap-south-1
```

A versioned bucket is **not** emptied by `aws s3 rm --recursive`: that writes delete markers and
leaves every version in place, still stored and still billed.

`governance.retention_days` is recorded per run and is **not enforced** by anything here. There is
no S3 lifecycle rule on `uploads/` and no per-entity deletion path. A DPDP deletion request today is
the sequence above, narrowed by hand. That is Phase 4b work and §9.2 says so again.

---

## 9. Security

### 9.1 What is enforced

| Control | Where |
|---|---|
| Public access blocked on both buckets, object ownership `BUCKET_OWNER_ENFORCED` | `infra/storage.py` |
| Artefact bucket: SSE-KMS with a customer-managed key, key rotation on, bucket key on | `infra/storage.py` |
| Access-log bucket: SSE-S3 deliberately, so an AWS service principal never gets a grant on the product's key | `infra/storage.py` |
| A bucket policy denying a PUT that names the **wrong** encryption or the wrong key | `_deny_wrong_encryption_headers`, DEC-373 |
| TLS-only on both buckets (`enforce_ssl`), with a TLS 1.2 floor | `infra/storage.py` |
| ECR: immutable tags, scan on push, KMS-encrypted with the same key | `infra/storage.py` |
| The task and the database in private subnets, no public IP, `publicly_accessible=False` | `infra/network.py`, `infra/database.py` |
| `rds.force_ssl=1` in the parameter group, and a composed URL that says `sslmode=require` | `infra/database.py` |
| RDS storage encrypted with the same customer-managed key | `infra/database.py` |
| The database password never rendered into a template, a synth output or a console | `infra/database.py` |
| HTTPS with `TLS13_RES` when a certificate is given; prod cannot be deployed without one | `infra/compute.py`, `AppContext.validate` |
| Security-group-to-security-group rules rather than CIDRs | `infra/network.py` |
| IAM statements written out by hand, not generated by `grant*` helpers | `infra/policies.py`, DEC-369 |
| S3 access scoped to four prefixes — `uploads/`, `runs/`, `models/`, `_bootstrap/` | `infra/naming.py` |
| SageMaker access scoped to `training-job/marketing-ai-*` and `processing-job/marketing-ai-*`, with no `List*` action anywhere | `infra/policies.py` |
| `secretsmanager:GetSecretValue` on exactly one ARN; the task can never read the database credential | `infra/compute.py` |
| Parameter Store reads scoped to `/marketing-ai/<env>` and its children | `infra/policies.py` |
| The container runs as uid 10001, not root | `Dockerfile` |

**The wildcard grants, enumerated.** `RESOURCE_WILDCARD_ALLOW_LIST` in `infra/policies.py` is the
complete set of actions allowed to appear in an `Allow` whose `Resource` is `"*"`, and
`tests/infra/test_iam.py` walks every identity policy in every synthesised stack and fails on any
other.

| Action | Why it cannot be scoped |
|---|---|
| `ecr:GetAuthorizationToken` | Returns a token for the caller's registry, not for a named repository; the IAM reference lists it with no resource types. Every other ECR action here is scoped to one repository ARN |
| `cloudwatch:PutMetricData` | No resource types either. Narrowed the only way AWS offers, a `cloudwatch:namespace` condition pinned to `MarketingAI`, so the task cannot forge `AWS/RDS` datapoints |
| `ec2:CreateNetworkInterface`, `ec2:CreateNetworkInterfacePermission`, `ec2:DeleteNetworkInterface`, `ec2:DeleteNetworkInterfacePermission`, `ec2:DescribeNetworkInterfaces`, `ec2:DescribeDhcpOptions`, `ec2:DescribeSecurityGroups`, `ec2:DescribeSubnets`, `ec2:DescribeVpcs` | A VPC-attached SageMaker job makes the *service* create ENIs in our subnets. They do not exist when the policy is written and their ids are not predictable, which is why AWS's own documented SageMaker execution policy uses `*` for exactly this set |

An ARN containing a wildcard **path** — `.../uploads/*`, `.../training-job/marketing-ai-*` — is a
different thing, and it is the point: it names a prefix.

One further grant is deliberate rather than accidental. `s3:ListBucket` carries no `s3:prefix`
condition, because during a `GetObject` there is no `s3:prefix` in the request and a condition on it
would fail closed — turning every absent artefact into a 403 that looks like a permissions bug
(§10.6).

### 9.2 What is not enforced

- **There is no authentication.** Every route is open to anyone who can reach the load balancer. In
  a dev deployment that is the internet, over HTTP, unless a certificate was supplied. Put the
  deployment behind something — a VPN, an IP allow-list on the ALB, an authenticating proxy — or
  treat the URL as public. This is the single largest gap in Phase 4a.
- **`approved_by` is a caller-supplied claim, not an identity** (DEC-055).
  `POST /models/{id}/approve` and `POST /models/{id}/promote` require a non-blank string and store
  it verbatim; nothing verifies it, and the API never substitutes one of its own. Approval decides
  which model scores the customer's base, so it is the first route that needs a verified identity
  behind the name. Until there is one, the audit trail records what the caller *claimed* — which is
  the honest version of an unauthenticated deployment — and it must not be presented anywhere as
  evidence of who acted.
- **There is no per-tenant isolation inside a deployment.** The tenancy boundary is the AWS account.
  Storage keys carry no tenant, which is exactly why `Storage` must not grow a "read any key"
  method.
- **DPDP controls are recorded, not enforced.** `governance.retention_days` is stored and nothing
  acts on it; there is no consent audit record and no per-entity deletion path. `consent_column` and
  `pii_handling` do work today, in the prepare stage, and are the exception.
- **`cors_origins` defaults to `*` in dev**, and is refused only on prod. An allow-list is not an
  authorisation mechanism either way.

All of the above is Phase 4b. None of it is a configuration mistake to be corrected by changing a
setting.

---

## 10. Troubleshooting

The five failures a first deployment actually hits, in roughly the order they arrive.

### 10.1 `cdk synth` dies with `TypeError: aspectApplication.aspect.visit is not a function`

**Symptom.** `make infra-nag`, or any synthesis with `-c cdk_nag=true`, fails inside the Node bridge
before a single stack is rendered.

**Cause.** cdk-nag 3.0.2 is incompatible with `aws-cdk-lib==2.270.0` — the aspect interface changed.
This was measured, not guessed: that exact `TypeError` is what `npx cdk synth` produces with the two
of them installed together.

**Fix.** Nothing to do; `pyproject.toml` pins `cdk-nag==2.38.2` for this reason (DEC-366). If you
are seeing this error, something upgraded cdk-nag. Rebuild the infra venv with `make infra-setup`
and check that `.venv-infra/bin/pip show cdk-nag` reports 2.38.2.

### 10.2 The Fargate task starts, stops, and leaves no logs at all

**Symptom.** The ECS service cycles tasks. `aws ecs describe-tasks` shows a `stoppedReason` about
the log driver, an image pull, or a timeout. `/marketing-ai/<env>/api` has no entries whatsoever.

**Cause.** The task is in a private subnet with no route to the AWS APIs it needs *before* it can
run. With `nat_gateways=0` and no interface endpoints it cannot pull its image (`ecr.api`,
`ecr.dkr`), cannot start `awslogs` (`logs`), and cannot read its settings (`ssm`,
`secretsmanager`). The failure is not "slower"; it is a task that stops before it writes a line.

**Fix.** `AppContext.validate` refuses that combination at synth time and lists the missing
endpoints in the message, so it normally cannot reach a deployment at all — the default is
`nat_gateways=1`. If it did reach one (a VPC changed outside CDK, an endpoint deleted by hand),
redeploy with either:

```bash
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 deploy --all \
  -c env_name=dev -c image_digest="$(cat ../.image-digest)" -c nat_gateways=1 \
  --require-approval broadening
# or, keeping the subnets closed:
#   -c vpc_endpoints=ecr.api,ecr.dkr,logs,secretsmanager,ssm
```

The S3 gateway endpoint carries the image layers and is always created.

### 10.3 `OSError: libgomp.so.1: cannot open shared object file`

**Symptom.** The container starts and dies on `import lightgbm`, in the API or in a job.

**Cause.** LightGBM links against `libgomp1`, which the slim Python base image does not carry. The
copies that scikit-learn and xgboost bundle have mangled SONAMEs and do not satisfy the lookup.

**Fix.** The `Dockerfile` already installs it, in the `base` stage, through the
`RUNTIME_APT_PACKAGES` build argument. Do not pass `--build-arg RUNTIME_APT_PACKAGES=` unless the
base image you are naming already carries the library.

**Be aware:** that apt layer **has never been built in the environment where the image was
authored**. The egress policy of that session refuses `deb.debian.org`, so `apt-get update` could
not run there and the layer is unproven rather than proven. The Dockerfile is ordinary and
`tests/unit/test_container_files.py` asserts the layer is present — but the first person to build
this image on a machine with normal network access is the first person to execute it. If the build
fails at that step, the network is the suspect before the Dockerfile is.

### 10.4 A setting has no effect — or the application refuses to start naming one

**Symptom.** A parameter you edited changes nothing, and there is no error at all. Or the task exits
immediately with `SETTING_REQUIRED` naming a `MARKETING_AI_*` variable you thought you had set.

**Cause.** Three different mistakes with the same shape.

- **A misspelt name.** The application *ignores* a `MARKETING_AI_*` name it does not recognise, so a
  typo is silent here — which is why the check is at the deployment instead:
  `tests/infra/test_task_definition.py` fails the build on any `MARKETING_AI_*` variable that is
  neither a field nor in `NON_FIELD_ENV_VARS` (DEC-304). A typo in an *SSM leaf* is silent
  everywhere, so the field table in §3.2 is the list to check it against. The parameter is also
  accepted under either spelling — `s3_bucket` or `MARKETING_AI_S3_BUCKET` — and a leaf matching
  neither is ignored.
- **A parameter written one level too deep.** `GetParametersByPath` runs with `Recursive=False`, so
  `/marketing-ai/dev/sagemaker/role_arn` is stored, looks correct in the console, and is never read.
  It has to be `/marketing-ai/dev/sagemaker_role_arn`.
- **The environment beating SSM.** If the task definition sets the variable, editing the parameter
  cannot win: environment beats SSM by design (DEC-301). Only three variables belong in the task
  definition, and anything else there is the bug.

**Fix.**

```bash
aws ssm get-parameters-by-path --path /marketing-ai/dev/ --region ap-south-1 \
  --query 'Parameters[].Name' --output text
aws ecs describe-task-definition --task-definition marketing-ai-dev-api \
  --region ap-south-1 --query 'taskDefinition.containerDefinitions[0].environment'
```

### 10.5 The database will not connect, or connects with the wrong password

**Symptom (a).** `SSL connection is required`, or a connection that hangs and times out.
**Symptom (b).** `password authentication failed`, from a deployment that worked last month.

**Cause (a).** `rds.force_ssl=1` is set, so the server refuses a plaintext connection, and the
security group admits only the app and job security groups. A URL without `sslmode=require`, or a
client outside those groups, cannot connect. That is the control working.

**Cause (b).** The credential rotated. The AWS single-user rotation function rewrites `password` in
`marketing-ai/<env>/db` and knows nothing about `marketing-ai/<env>/app`, whose `postgres_dsn` was
composed at deploy time, so the application document now holds the previous password (DEC-376).

**Fix.**

```bash
# (a) ask the deployment what it can actually reach; the `database` check prints a `fix:` line
.venv/bin/python -m scripts.aws_bootstrap --env dev --no-migrate

# (b) recompose the application document by deploying the database stack again
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 \
  deploy marketing-ai-dev-database -c env_name=dev
```

Then force a new ECS deployment so the tasks re-read the document.

The real repair is for `engine/settings.py` to read the standard RDS credential document
(`username`, `password`, `host`, `port`, `dbname`) and compose the URL itself, at which point the
deployment keeps one secret and rotation is end to end. Until then the rotation interval is set
deliberately long, rather than to a number that sounds diligent and pages somebody monthly.

### 10.6 Also worth knowing

- **`403 AccessDenied` on an artefact that simply is not there.** S3 will not confirm the absence of
  an object to a principal that may not list, so a missing `s3:ListBucket` turns every absent
  artefact into what looks like a permissions failure. That statement is granted for this reason.
- **`AccessDenied` on a write after somebody adds a bucket policy.** The product's `PutObject`
  carries no encryption header when `s3_kms_key_id` is unset, and that is a valid deployment. The
  policy here denies a PUT that names the *wrong* encryption, not one that declines to answer; a
  policy copied from a blog post that denies header-less PUTs will break every write (DEC-373).
- **An alarm you expected does not exist.** `infra/observability/alarms.json` commits no threshold
  for an alarm whose threshold would be a measurement nobody has taken. An alarm whose substitution
  is not supplied is *not created*, and the `GeneratedArtefacts` output names it. Supply one with
  `-c alarm_thresholds=Name=value` from this deployment's own history (DEC-379).
- **The `Jobs*` metric filters never fire.** The observability stack puts the same metric filters on
  `/marketing-ai/<env>/api` and `/marketing-ai/<env>/jobs`, and a SageMaker job's container output
  goes to SageMaker's own log groups (§5.3), so the ones on the jobs group have nothing to match.
  The `Api*` metrics are the ones with data behind them.
- **The budget reports nothing.** Cost-allocation tags have not been activated yet. §6.1.
- **There is no cost in the UI.** Nothing under `ui/` reads `cost_estimate` and no route returns it;
  §5.2 is where the number is.
- **`infra/README.md` does not exist yet**, although `infra/network.py` and `infra/database.py` name
  it. The egress shape it would describe is §10.2 and the rotation shape is §10.5.
- **`make infra-synth` warns about `latest`.** Normal without `-c image_digest`; not normal during a
  deployment.

---

## Decisions this document records

These belong in `docs/DECISIONS.md`. They are stated here so the document and the log cannot
disagree about what was decided.

- **DEC-397 — The Phase 4a cost tables ship unmeasured, with the commands that fill them.**
  *Context:* the Phase 4a plan asks for a measured idle-cost table and a per-run cost table, and no
  AWS account exists behind this repository. *Decision:* both tables ship with every cell holding
  the literal marker `NOT YET MEASURED`, beside the exact Cost Explorer, run-manifest and CloudWatch
  commands that fill them, and §6 says once, near its top, that nothing in it has been measured.
  *Consequences:* the document is honest about what is not known and still useful to the person who
  will find out; a half-filled table is caught by `tests/unit/test_docs_honesty.py`; §6.3 keeps list
  price and bill distinguishable in the reader's mind, the way `CostEstimate.basis` does in the
  product (plan section 13.3).
- **DEC-398 — `docs/RUNBOOK.md` is separate from `docs/AWS_DEPLOYMENT.md`.** *Context:* one document
  was carrying both "get this running for the first time" and "it is 2 a.m. and an alarm fired".
  *Decision:* this document is the deployment walk-through, read once per account; `docs/RUNBOOK.md`
  is day-two operations, read under time pressure, and is organised by symptom. *Consequences:* two
  shorter documents with one question each; §10 here stays the five failures of a *first*
  deployment, and everything that happens to a deployment already serving lives in the runbook.
- **DEC-399 — Documentation honesty is enforced by a test, not by review.** *Context:* the rule that
  no unmeasured number is written down holds only as long as every future editor remembers it.
  *Decision:* `tests/unit/test_docs_honesty.py` fails the build when a currency figure appears
  outside a command block without a source citation, when a cost table loses its marker, when either
  document tells the reader to run a `make` target the Makefile does not define, or names a
  `MARKETING_AI_*` variable the application would refuse to start with. *Consequences:* the cheapest
  mistakes are caught mechanically; the test gates the documents and not the product, so a real
  measurement is added by adding it with its source beside it.
