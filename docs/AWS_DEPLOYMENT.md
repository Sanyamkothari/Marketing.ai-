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

> **What is in the tree.** Everything this document tells you to run is in the repository. Where it
> would otherwise describe something that is not here, it says so instead.
>
> **How it was checked.** M50 (Phase 4b) walked this document step by step as a first-time operator
> would, before any AWS account existed: every command that can run offline was run, and every
> command that cannot was checked against the code it names. §12 lists what that walk found and
> where each finding was fixed. What still needs the account is in `docs/M50_CHECKLIST.md`, the
> ordered list for the day it exists. §11 covers what Phase 4b adds to a deployment: sign-in,
> the audit trail, privacy controls and schedules.

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

Eight CloudFormation stacks deploy in one direction, declared in `infra/app.py`:

```
network -> storage -> observability -> database -> sagemaker -> compute -> operations -> budgets
```

`operations` is Phase 4b's (`infra/operations.py`, §11): the audit-export bucket with Object Lock,
the EventBridge Scheduler group, role and job task, the application alert topic, the API task
role's Phase 4b grants and the Parameter Store values that switch those features on. It adds
nothing to a Phase 4a stack.

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
| Toolchain | Python 3.11, Node 22, Docker with buildx, GNU make, AWS CLI v2 | `infra/` drives the CDK CLI through `npx`; the image is built with `docker buildx`; every verification command below is `aws ...` |
| Two virtualenvs | `make setup` (`.venv`: the product, for the helper scripts and the tests) and `make infra-setup` (`.venv-infra`: the CDK app, §4.1) | `scripts/run_in_deployment.py`, `make aws-bootstrap`, `make migrate` and the paid tests run from `.venv`; nothing in §4 works with only one of them |
| CDK | CLI `aws-cdk@2.1142.0` (pinned in the Makefile), `aws-cdk-lib==2.270.0` and `cdk-nag==2.38.2` (pinned in `pyproject.toml`) | §10.1 is why cdk-nag is held back |
| Bootstrap | Once per account and region, from `infra/`: `PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 bootstrap aws://<account>/ap-south-1` | CDK needs its own staging bucket and roles before any stack can deploy. Run it from `infra/` with the pinned CLI, so the bootstrap template version is the one this CLI expects |
| ECR | One repository named `marketing-ai` | `infra/storage.py` creates it; `scripts/build_push_image.sh` pushes into it. §2.2 is the ordering problem that creates |
| Bedrock | Only if `-c bedrock_enabled=true`: request model access in `ap-south-1` for every model in `-c bedrock_model_ids` (plan prerequisite P2). The request has a lead time | §3.4; the paid smoke test is §6.5 |
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

**The first deployment of an account is made from a terminal, not from CI.** `make aws-deploy`
passes `--require-approval broadening`, and the CDK CLI cannot ask for that approval without a
terminal: in CI it stops with an error instead of deploying. Every stack's first deployment creates
IAM roles and security groups, so every first deployment is "broadening". That is the review
working, not a defect: a person reads the IAM prompt once, and CI then deploys only changes that do
not widen access. The same applies later to any change that adds a permission.

### 2.2 The one ordering problem, and the way round it

`scripts/build_push_image.sh` pushes to an ECR repository, and `infra/storage.py` is what creates
that repository. On a genuinely empty account, deploy the storage stack on its own first:

```bash
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 \
  deploy marketing-ai-dev-storage -c env_name=dev
```

Its `RepositoryUri` output is the value of `ECR_REGISTRY/ECR_REPOSITORY`:
`<account>.dkr.ecr.ap-south-1.amazonaws.com/marketing-ai`, where everything before the `/` is
`ECR_REGISTRY` and `marketing-ai` is `ECR_REPOSITORY`. The storage stack references no other
stack, so this deploys it alone. After that one command,
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
`infra/compute.py` publishes for you — and, for the twelve Phase 4b rows at the foot of the table,
`infra/operations.py` (§11) — and everything else is a default you may override.

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
| `auth_mode` | `MARKETING_AI_AUTH_MODE` | `auth_mode` | `off` | — but a prod deployment with `off` answers 503, §11.3 | yes, `local` unless `-c auth_mode=off` (refused for prod) |
| `auth_session_ttl_seconds` | `MARKETING_AI_AUTH_SESSION_TTL_SECONDS` | `auth_session_ttl_seconds` | `28800`, between 300 and 86400 | `auth_mode=local` | no |
| `audit_export_bucket` | `MARKETING_AI_AUDIT_EXPORT_BUCKET` | `audit_export_bucket` | none, meaning an export is written to local disk | an S3 audit export | yes, the Object Lock bucket |
| `audit_export_prefix` | `MARKETING_AI_AUDIT_EXPORT_PREFIX` | `audit_export_prefix` | `audit` | — it is also the IAM boundary | yes, `audit` |
| `audit_retention_days` | `MARKETING_AI_AUDIT_RETENTION_DAYS` | `audit_retention_days` | `2555`, between 1 and 3650 | an S3 audit export | yes: 1 in dev, 2555 in prod, or `-c audit_retention_days` |
| `scheduler_backend` | `MARKETING_AI_SCHEDULER_BACKEND` | `scheduler_backend` | `none` | always | yes, `eventbridge` |
| `scheduler_tick_seconds` | `MARKETING_AI_SCHEDULER_TICK_SECONDS` | `scheduler_tick_seconds` | `60`, between 1 and 3600 | `scheduler_backend=local` only | no |
| `scheduler_group_name` | `MARKETING_AI_SCHEDULER_GROUP_NAME` | `scheduler_group_name` | `marketing-ai` | eventbridge — it is also the IAM boundary | yes, `marketing-ai-<env>` |
| `scheduler_target_arn` | `MARKETING_AI_SCHEDULER_TARGET_ARN` | `scheduler_target_arn` | none | eventbridge | yes, the ECS cluster |
| `scheduler_role_arn` | `MARKETING_AI_SCHEDULER_ROLE_ARN` | `scheduler_role_arn` | none | eventbridge | yes, `marketing-ai-<env>-scheduler` |
| `alert_backend` | `MARKETING_AI_ALERT_BACKEND` | `alert_backend` | `log` | always | yes, `sns` |
| `alert_sns_topic_arn` | `MARKETING_AI_ALERT_SNS_TOPIC_ARN` | `alert_sns_topic_arn` | none | `alert_backend=sns` | yes, `marketing-ai-<env>-alerts` |

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

### 3.5 The refusals

A deployment that is described wrongly fails at startup, loudly, rather than at the first request.

| Refusal | Code | What it means |
|---|---|---|
| An incomplete backend | `SETTING_REQUIRED` | `storage_backend=s3` with no `s3_bucket` or no `aws_region`; `metadata_backend=postgres` with no `postgres_dsn`; `job_backend=sagemaker` without the role, the image, the instance type and the region; `llm_backend=bedrock` without `bedrock_model_id` |
| `job_backend=sagemaker` without `storage_backend=s3` | `SETTING_REQUIRED` | A remote job cannot read a local disk, so this combination is a deployment that would fail at its first run (DEC-302) |
| `scheduler_backend=eventbridge` without `scheduler_target_arn`, `scheduler_role_arn` and `aws_region`; `alert_backend=sns` without `alert_sns_topic_arn` and `aws_region` | `SETTING_REQUIRED` | Phase 4b's two AWS backends, refused the same way as Phase 4a's (DEC-701) |
| `cors_origins` containing `*` when `env=prod` | `SETTING_REQUIRED` | DEC-024 left CORS open for a UI opened as a local file. That is a statement about a laptop, not about an internet-facing load balancer, so a prod deployment that never mentions `cors_origins` is refused rather than inheriting the laptop's answer (DEC-307) |
| A value the field cannot hold | `SETTING_INVALID` | `MARKETING_AI_JOB_MAX_WORKERS=nine`, a `download_url_ttl_seconds` outside 60…3600, an `s3_prefix` containing a `..` segment |

The message always names the environment variable to export and never its value (DEC-303).

`auth_mode=off` on `env=prod` is deliberately **not** a fifth refusal. The application starts, logs
an error, and answers every route except the health probe and sign-in with 503
`AUTH_NOT_CONFIGURED` (DEC-702, §11.3): it fails closed per request rather than failing to boot.
`infra/context.py` refuses the same combination one layer up, at synth time.

A `MARKETING_AI_*` variable that matches no field is **ignored** by the application rather than
refused: a deployment must not fail to start because some other tool on the host exported a name in
that space. The check that a typo is caught is one layer out, at the deployment — see the note under
the field table.

`infra/context.py` refuses the same class of mistake one layer up: an unrecognised `-c` key is a
`ContextError` with a "did you mean" hint, because `-c db_storage_bg=50` otherwise synthesises
happily and quietly uses the default (DEC-365).

---

## 4. Deploy

Three steps, in this order — set up, deploy, bootstrap from inside — and, after the very first
deployment, one service restart (§4.2). `ENV` is `dev` or `prod`.

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

The complete list a bare `make infra-synth ENV=dev` printed on the M50 walk, every one of them
expected until you supply what it asks for:

| Warning | Goes away when |
|---|---|
| `Unknown option(s): --all. These will be ignored.` | Never; `cdk synth` synthesises every stack anyway, and the flag is harmless |
| `No alert_email: this deployment raises alarms that nobody is told about` (and the operations stack's equivalent for application alerts) | `-c alert_email=...` is set (§4.2, persisted) |
| `Alarm 'marketing-ai-${Env}-stage-duration' is NOT created` and the same for `job-cost` | `-c alarm_thresholds=...` names a threshold from this deployment's own history (§10.6) |
| `No cross-stack-reference strength configured, defaulting to "strong"` | Never; a CDK feature-flag notice, and strong references are what the stacks rely on |
| `No image_digest: this synthesis names the repository's latest tag` | `make aws-deploy` passes the digest |
| `HTTP-ONLY LOAD BALANCER` | `-c certificate_arn=... -c domain_name=...` |
| `81 feature flags are not configured` | Never; informational |

It ends with `Successfully synthesized to .../infra/cdk.out`. If the first synthesis in a fresh
`.venv-infra` fails with `ModuleNotFoundError: No module named 'cdk_nag'` while
`.venv-infra/bin/pip show cdk-nag` reports it installed, run it again: the M50 walk saw that once,
while another process was writing to the same venv, and not on any rerun.

### 4.2 `make aws-deploy ENV=dev`

Point the shell at the registry first, and log Docker into it:

```bash
export ECR_REGISTRY=<account>.dkr.ecr.ap-south-1.amazonaws.com
export ECR_REPOSITORY=marketing-ai
aws ecr get-login-password --region ap-south-1 \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"
```

**Persist the context keys that describe this deployment before the first deploy.** `make aws-deploy`
passes `env_name` and `image_digest` and nothing else, and CDK context given with `-c` on one
command line is not remembered by the next. A key given once by hand — `alert_email`,
`monthly_budget_usd`, `certificate_arn`, `alarm_thresholds` — therefore **disappears on the next
`make aws-deploy`**, and CloudFormation removes what it created: the email subscriptions, the budget,
the HTTPS listener. The CDK CLI reads `infra/cdk.context.json` on every command, `make aws-deploy`
included, so put them there once:

```bash
cat > infra/cdk.context.json <<'EOF'
{
  "marketing-ai:alert_email": "<you@example.com>",
  "marketing-ai:monthly_budget_usd": "<your amount>"
}
EOF
```

The `marketing-ai:` spelling is the namespaced form `infra/context.py` accepts, so a key this
application does not know is still an error rather than a silent default. The file names an
account's resources, so it is in `.gitignore` and is never committed; keep one per checkout per
deployment. The M50 walk confirmed offline that `make infra-synth` picks it up: with the file above,
the synthesised observability template carries the email subscription and the budgets stack a budget.

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
| operations | `AuditBucketName`, `AlertTopicArn`, `ScheduleGroupName`, `SchedulerRoleArn`, `JobTaskDefinitionArn` | Phase 4b's resources, §11 |
| budgets | `Budget` | The budget, or a sentence saying none was asked for — §6.4 |

`--require-approval broadening` means CDK stops and asks before any change that widens an IAM policy
or a security group. Read what it prints. That prompt is the review.

`-c env_name=prod` refuses to synthesise without both `-c certificate_arn=...` and
`-c domain_name=...`. That is not a missing feature: an HTTP-only load balancer puts every upload and
every pre-signed download URL on the public internet in clear text.

**After the first deployment, restart the service once.** The compute stack starts the API's tasks
*before* the operations stack writes the Phase 4b parameters (`auth_mode`, `scheduler_backend`,
`alert_backend` and the rest), and `Settings` is read once, when a task starts (§7.3). So the tasks
of a first deployment run with every Phase 4b default: sign-in off, no scheduler, alerts to the log
only. One forced deployment picks the parameters up:

```bash
aws ecs update-service --cluster marketing-ai-dev --service marketing-ai-dev-api \
  --force-new-deployment --region ap-south-1
aws ecs wait services-stable --cluster marketing-ai-dev --services marketing-ai-dev-api --region ap-south-1
```

The same applies after any deployment that changes a value the operations stack writes. A one-off
task (§4.3) always starts fresh, so it never needs this.

### 4.3 Bootstrap the deployment, from inside it

```bash
.venv/bin/python -m scripts.run_in_deployment --env dev -- python -m scripts.aws_bootstrap
```

`cdk deploy` creates the database; it does not create the schema. And it cannot tell you whether the
task role can actually do what its policy appears to allow — a policy is a document, not a
demonstration. `scripts/aws_bootstrap.py` applies the Alembic migrations and probes every permission
with one real call each, reporting the whole list rather than stopping at the first failure.

**It has to run inside the deployment**, and that is what `scripts/run_in_deployment.py` is for. The
database is in isolated subnets that admit the service's and the jobs' security groups and nothing
else, so from a laptop or a CI runner the database probe and the migrations cannot connect at all.
And the probes run as whoever runs them: from a laptop that is your own, usually administrator,
credentials, and a storage probe that passes as an administrator says nothing about the task role.
`run_in_deployment` reads the running service's task definition, subnets and security groups, starts
**that** task definition once with its command replaced, waits for it to stop, prints what it
logged, and exits with its exit code. It passes no environment variables: the task reads its
settings from Parameter Store like the service does. It needs your credentials for
`ecs:DescribeServices`, `ecs:RunTask`, `ecs:DescribeTasks`, `iam:PassRole` on the two task roles and
`logs:GetLogEvents` — nothing inside the task uses them.

Working output (the task's own lines, then one line from `run_in_deployment` on stderr):

```
python -m scripts.aws_bootstrap: env=dev aws_region=ap-south-1 storage_backend=s3 metadata_backend=postgres job_backend=sagemaker llm_backend=fake log_format=json metrics_backend=emf

  [     ok] configuration: <n> use cases load
  [     ok] storage: wrote, read and deleted s3://<bucket>/_bootstrap/permission-probe.txt
  [     ok] database: PostgreSQL 16.x
  [     ok] migrations: schema at head
  [     ok] jobs: may describe training jobs

5/5 checks passed; this deployment is ready
python -m scripts.run_in_deployment: task stopped (Essential container in task exited), exit code 0
```

`<n>` is the number of use-case configs in the image (eight in this tree). Around the report you may also
see the migration's own log lines. A failure prints `FAILED` with the exception's
class — never its message, which routinely quotes the resource and the principal — and a `fix:` line
naming the IAM actions to grant; the task, and so `run_in_deployment`, exits non-zero. A task that
never started (an image it could not pull, a subnet with no route) has no log and exits 125, with
ECS's stop reason on the last line. `--no-migrate` probes without touching the schema; the image's
own `migrate` entrypoint applies the schema without probing:

```bash
.venv/bin/python -m scripts.run_in_deployment --env dev -- migrate
```

The `jobs` probe describes a training job that does not exist. A role allowed to look gets
`ValidationException`, which is a pass; a role that is not gets `AccessDeniedException`, which is a
failure. Describing a job that does not exist is the only way to test the permission without
creating one and paying for it.

The first line is `engine.settings.summary()`, rendered from an allow-list of non-secret fields. It
cannot print the database URL. Migrations go into `postgres_schema` (`marketing_ai`, written by the
compute stack): the bootstrap hands Alembic the URL and the schema together.

**`make aws-bootstrap ENV=dev`** runs the same script where you are, as you. That is useful for
`--dry-run` and on a machine that really sits inside the VPC; anywhere else it is the wrong tool. It
reads the deployment only when `MARKETING_AI_SETTINGS_SOURCE=aws` and `MARKETING_AI_AWS_REGION` are
exported; without them the settings come from your shell, every backend is at its laptop default,
and the script now says so and fails (`deployment: FAILED`), where it used to report "ready" having
checked nothing.

### 4.4 The same steps in CI

`.github/workflows/deploy-dev.yml` runs `make infra-setup`, then `make aws-deploy` (which builds,
pushes and deploys, with the ECR variables from `aws-actions/amazon-ecr-login`), then the bootstrap
of §4.3 through `scripts/run_in_deployment.py`, then `scripts/smoke_deployment.py`, under
`workflow_dispatch` only, with a role assumed by OIDC and no long-lived key anywhere.

Three things about it, all found by the M50 walk:

- **It cannot make a first deployment**, nor any deployment that widens IAM or a security group:
  `--require-approval broadening` needs a terminal to ask in, and CI has none, so the deploy step
  stops with an error. Make those deployments from a terminal (§2.1); CI then deploys the rest.
- **The deploying role needs more than CloudFormation.** Besides everything in §2.1, the bootstrap
  step starts a task: `ecs:DescribeServices`, `ecs:RunTask`, `ecs:DescribeTasks`, `iam:PassRole` on
  `marketing-ai-<env>-task` and `marketing-ai-<env>-task-execution`, and `logs:GetLogEvents` on the
  API log group. The smoke step reads the compute stack's outputs (`cloudformation:DescribeStacks`).
- **The smoke test is deliberately small.** It reads `ServiceUrl` from the compute stack, checks
  `GET /healthz`, and asks `GET /auth/me` with no token which sign-in state the deployment is in:
  `401` is `auth_mode=local` and passes; `200` is sign-in off, a warning on dev and a failure
  anywhere else; `503 AUTH_NOT_CONFIGURED` is a prod deployment failing closed, and fails. It sends
  no credential and starts no run, so it is safe against any deployment; §5 is still a person's job.

---

## 5. Verify

The acceptance walk-through. Nothing here is simulated, and every number on every screen was
computed by the run that produced it.

```bash
export SERVICE_URL=$(aws cloudformation describe-stacks --stack-name marketing-ai-dev-compute \
  --region ap-south-1 --query "Stacks[0].Outputs[?OutputKey=='ServiceUrl'].OutputValue" --output text)
curl -fsS "$SERVICE_URL/healthz"
```

**Sign in first.** A deployment built from this tree has `auth_mode=local` (§11.1), so every route
except the health probe and sign-in answers `401` until you sign in, and nobody can sign in until
the first Admin exists: create it now (§11.2). In the UI, sign in on the first screen. From a shell,
fetch a token once — the password is read without echo and never lands in the shell history — and
send it with every request:

```bash
read -r -p "username: " MA_USER; read -r -s -p "password: " MA_PASSWORD; echo
export TOKEN=$(MA_USER="$MA_USER" MA_PASSWORD="$MA_PASSWORD" python3 - "$SERVICE_URL" <<'EOF'
import json, os, sys, urllib.request
body = json.dumps({"username": os.environ["MA_USER"], "password": os.environ["MA_PASSWORD"]}).encode()
request = urllib.request.Request(sys.argv[1] + "/auth/login", data=body, headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(request))["token"])
EOF
)
unset MA_PASSWORD
```

Each `curl` below carries `-H "Authorization: Bearer $TOKEN"`. The token lasts
`auth_session_ttl_seconds` (eight hours by default).

1. **Open the URL.** `GET /healthz` answers `{"status": "ok", "version": "..."}` and deliberately
   carries no other detail. The load balancer's target group uses the same route, so a task that
   answers it is a task receiving traffic. It is public; everything else is not.
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

curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["compute"])'
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
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" \
  | python3 -c 'import json,sys; m=json.load(sys.stdin); print(m["compute"]); print(m["cost_estimate"])'
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
artefacts then occupy. The last five rows are the ones plan M50 asks for to settle the "cost tiers"
question: one training run per strategy on the same file, scoring normalised to 100,000 rows, and
the assistant normalised to 1,000 questions. `docs/M50_CHECKLIST.md` says how each is produced and
where the figure is recorded; for the assistant row the "billable seconds" and "instance type"
cells are not meaningful and are recorded as the model id and the token counts instead.

#### Per-run cost

| Run | Billable seconds | Instance type | Estimated at list price | Cost Explorer, same day |
|---|---|---|---|---|
| Train, template-sized file | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Train, Telco Churn (7,043 rows) | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Score, same file | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Train, Telco Churn, strategy `fast` | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Train, Telco Churn, strategy `balanced` | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Train, Telco Churn, strategy `exhaustive` | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Score, per 100,000 rows | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |
| Assistant, per 1,000 questions (Bedrock) | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED | NOT YET MEASURED |

The first three columns come from the run itself, not from a spreadsheet:

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" \
  | python3 -c 'import json,sys; m=json.load(sys.stdin); print(m["compute"]); print(m["cost_estimate"])'
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
`LlmCostUsd` will stay empty: the generative phase is in this repository now (§3.4), but nothing
calls `engine.aws.metrics.record_llm_cost` yet and `configs/llm_prices.yaml` ships empty on purpose,
so the assistant's cost comes from Cost Explorer, grouped by the Bedrock usage type, and from the
token counts the generative screens report — not from this metric.

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

`make aws-deploy` passes `env_name` and `image_digest` and nothing else. A key given only on one
`cdk deploy` command line, as above, is gone at the next `make aws-deploy`, which then deletes the
budget: keep `monthly_budget_usd` and `alert_email` in `infra/cdk.context.json` (§4.2) so every
deployment carries them. The budget stack always writes
`/marketing-ai/cost/<env>/monthly-budget-usd`, holding either the amount or the word `none`, so the
answer to "did anyone set a budget here" does not depend on reading a console.

A budget given without `alert_email` is refused at synth time: a budget nobody is told about is a
number in a console, not a control. Its two notification thresholds are percentages of the
operator's own amount, so they carry no claim about what anything costs.

### 6.5 The paid tests

Two pytest markers bill a real account, and neither runs unless it is selected by name: `make test`
and `make test-all` both exclude them (DEC-358).

| Marker | What it calls | Selected with |
|---|---|---|
| `bedrock` | Amazon Bedrock, from `tests/integration/test_bedrock_smoke.py`: one `Converse` call capped at 32 output tokens, one `CountTokens` call and four short embeddings | `.venv/bin/python -m pytest -m bedrock tests/integration/test_bedrock_smoke.py` |
| `aws` | Reserved for tests against a real deployment. **No test in this tree carries it yet**; the deployment is exercised by §4.3, §4.4 and §5 instead | `.venv/bin/python -m pytest -m aws` (collects nothing today) |

The Bedrock test self-skips unless three variables are exported — `BEDROCK_SMOKE_REGION`,
`BEDROCK_SMOKE_GENERATION_MODEL_ID` and `BEDROCK_SMOKE_EMBEDDING_MODEL_ID`, plus the optional
`BEDROCK_SMOKE_EMBEDDING_DIMENSIONS` — and boto3 finds credentials. Those are test variables read
straight from the environment, not `Settings` fields, which is why they do not start with the
product's prefix. The models must have Bedrock model access in that region (§2). Run it with your
own credentials, from `.venv`, after the budget of §6.4 exists: a budget is the cap, and the test's
own design (one completion, nothing repeated) is what keeps it small.

---

## 7. Upgrading

The unit of a deployment is an image digest and a schema revision. Roll them one at a time.

### 7.1 Migrate the database

The deployment's database is reachable only from inside its VPC, so a migration runs as a one-off
task with the image's `migrate` entrypoint (`alembic -c /app/alembic.ini upgrade head`), which reads
the URL and the schema from the deployment's own settings:

```bash
.venv/bin/python -m scripts.run_in_deployment --env dev -- migrate
```

The bootstrap of §4.3 applies migrations too, which is why it runs after `cdk deploy` and before
the smoke test. To read the DDL before it is applied, render it offline — no connection, nothing
executed; the URL only chooses the dialect, so use a placeholder, never the real one:

```bash
.venv/bin/alembic -x url=postgresql://placeholder@localhost/marketing -x schema=marketing_ai upgrade head --sql
```

With a schema, the script begins by creating it and putting it on the `search_path`, exactly as an
online migration does. `make migrate` is `alembic upgrade head` against whatever `Settings` describes
in your shell; it is for a database you can reach, such as the local stack's. Pointed at a Postgres
by hand, export both `MARKETING_AI_POSTGRES_DSN` and `MARKETING_AI_POSTGRES_SCHEMA=marketing_ai`:
with the URL alone the tables land in `public`, and a deployment reads `marketing_ai` first.

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
| Audit exports | A copy of the audit table, Object Lock COMPLIANCE for `audit_retention_days` | 1 day in dev, 2555 in prod | `-c audit_retention_days=`, §11.4 |

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
| Audit-export bucket (Phase 4b) | **Retained always**; a locked version cannot be deleted by anyone until its date passes | Retained |
| Scheduler group, alert topic, job task definition (Phase 4b) | Deleted | Group retained; the rest deleted |
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

`governance.retention_days` is enforced by Phase 4b's retention job, and a DPDP erasure request has
its own route; §11.7 and §11.8 are how to run them on a deployment. The commands above are for
deleting a whole deployment's data, not one person's.

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
| Sign-in on every route but the probe and sign-in itself; a route with no declared role is refused, not open | `api/access.py`, `api/access_policy.py`; `auth_mode=local` written by `infra/operations.py` |
| Audit exports locked in COMPLIANCE mode, and a bucket policy denying every principal `DeleteObject` and `DeleteObjectVersion` | `infra/operations.py`, `engine/audit/export.py` |
| The task may only put objects under `audit/` in the audit bucket: no read, no list, no delete | `infra/policies.py` `audit_export_write_statement` |
| EventBridge Scheduler's role can start one task family in one cluster, and only a schedule in this deployment's group may assume it | `infra/operations.py`, `infra/policies.py` `run_job_task_statements` |
| The task publishes to the application alert topic only, never to the CloudWatch alarm topic | `infra/policies.py` `alert_publish_statement` |

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

- **Sign-in is the built-in user store, not an identity provider.** `auth_mode` is `off` or `local`;
  the client's SSO or Cognito (plan prerequisite P5) is a third value that does not exist yet. The
  local store hashes passwords with PBKDF2 and stores only a digest of each session token, but it
  has no multi-factor authentication, no lockout or rate limit on failed sign-ins, and no
  federation. Until P5 is decided and built, keep a dev deployment's URL to the people testing it,
  and treat HTTP-only dev (no certificate) as sending passwords in clear text — because it does.
- **The first Admin's password crosses the ECS API** on a deployment (§11.2). That is acceptable
  for dev with an immediate password change and not for prod.
- **With `auth_mode=off`, nothing is enforced**: every request is the all-roles local operator, and
  `approved_by` is again a caller-supplied claim rather than an identity (DEC-055). A dev deployment
  can be switched to `off` with `-c auth_mode=off`; `infra/context.py` refuses it for prod, and a prod
  task that nevertheless reads `off` answers 503 (§11.3).
- **There is no per-tenant isolation inside a deployment.** The tenancy boundary is the AWS account.
  Storage keys carry no tenant, which is exactly why `Storage` must not grow a "read any key"
  method. Plan M51 decides whether that stays so (prerequisite P4).
- **DPDP controls support compliance; they are not a legal opinion.** Retention, erasure, consent
  and access exports exist (§11.7, §11.8), and Minfy's legal or compliance team must review them.
  Erasure flags models trained on the person's data for retraining at the next cycle rather than
  retraining at once.
- **`cors_origins` defaults to `*` in dev**, and is refused only on prod. An allow-list is not an
  authorisation mechanism either way.

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
# (a) ask the deployment, from inside it, what it can actually reach; the `database` check prints a `fix:` line
.venv/bin/python -m scripts.run_in_deployment --env dev -- python -m scripts.aws_bootstrap --no-migrate

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
- **`infra/README.md`** is the reference for every context key and every cdk-nag suppression; this
  document is the walk-through. Where the two describe the same thing, the code is the tiebreaker.
- **`make infra-synth` warns about `latest`.** Normal without `-c image_digest`; not normal during a
  deployment.

---

## 11. Phase 4b: sign-in, audit, privacy and schedules

Phase 4b (plan M46–M49) adds sign-in and roles, an append-only audit trail, the DPDP retention,
erasure and consent controls, and scheduled scoring, monitoring and retraining with alerts. Every one
of them was built to run on a laptop first, and every `Settings` default reproduces the behaviour
before Phase 4b. On a deployment, the `operations` stack (`infra/operations.py`) supplies the AWS
half and writes the parameters that switch each feature on, so a deployment made from this tree has
all of them without a hand-edited parameter.

### 11.1 The settings, and where a deployment gets them

The twelve Phase 4b rows of §3.2 are the whole configuration surface. On a deployment:

| Setting | Value on a deployment | Chosen with |
|---|---|---|
| `auth_mode` | `local` | `-c auth_mode=off` is accepted for dev only; there is no identity-provider value until plan P5 is decided (§11.9) |
| `auth_session_ttl_seconds` | `28800` (eight hours), the default | Not written by any stack; §11.1 below |
| `audit_export_bucket`, `audit_export_prefix` | the Object Lock bucket, `audit` | Fixed; §11.4 |
| `audit_retention_days` | `1` in dev, `2555` in prod | `-c audit_retention_days=` (1 to 3650), **before** the first export — §11.4 |
| `scheduler_backend`, `scheduler_group_name`, `scheduler_target_arn`, `scheduler_role_arn` | `eventbridge`, `marketing-ai-<env>`, the ECS cluster, `marketing-ai-<env>-scheduler` | Fixed; §11.5 |
| `scheduler_tick_seconds` | unused | Only `scheduler_backend=local` reads it |
| `alert_backend`, `alert_sns_topic_arn` | `sns`, `marketing-ai-<env>-alerts` | Fixed; the recipient is `-c alert_email=`, §11.6 |

Two context keys size the scheduled-job task: `-c job_cpu=` and `-c job_memory=`, 1024 and 4096 by
default — chosen, not measured, and checked against Fargate's size table at synth time.

To see what a deployment actually reads:

```bash
aws ssm get-parameters-by-path --path /marketing-ai/dev/ --region ap-south-1 \
  --query 'Parameters[].[Name,Value]' --output table
```

A value the stacks do not write — `auth_session_ttl_seconds`, say — is set by writing the parameter
by hand and restarting the service (§4.2):

```bash
aws ssm put-parameter --name /marketing-ai/dev/auth_session_ttl_seconds --type String \
  --value 3600 --overwrite --region ap-south-1
```

**Never hand-write a parameter a stack writes.** The next deployment either overwrites your value
without a word, or, if you created it before the stack did, fails with "already exists". The
`put-parameter` above is safe because no stack owns that name; the list of names a stack owns is the
right-hand column of §3.2.

### 11.2 The first Admin

With `auth_mode=local` every route except `GET /healthz` and `POST /auth/login` needs a signed-in
user, and the API cannot create the first one: an Admin creates users, and there is no Admin yet. The
first Admin comes from `scripts/create_user.py`, run inside the deployment because the users table is
in the deployment's database.

1. **The schema is at head** — §4.3. The user, session and audit tables are migrations `0002` on.
2. **Create the Admin.** The script never takes a password on its command line (DEC-722): it prompts,
   or reads the first line of standard input with `--password-stdin`. A one-off ECS task has no
   terminal and no standard input, and the service has ECS Exec switched off, so on a deployment the
   password has to be written into the command that feeds it to standard input:

   ```bash
   .venv/bin/python -m scripts.run_in_deployment --env dev -- \
     sh -c 'printf "%s\n" "<one-time password, at least 12 characters>" | python -m scripts.create_user --username <admin> --role admin --password-stdin'
   ```

   It prints `created user <user id> with roles admin` and writes a `users.create` audit event by
   `system:bootstrap`. Exit code 2 is a refusal the message explains (a username already taken, a
   password under twelve characters); 3 is a settings problem, and nothing was written.

   **That password is not a secret after this command.** The command line is part of the `RunTask`
   request: `aws ecs describe-tasks` shows it for as long as ECS lists the stopped task, and
   CloudTrail records the request. So it is a one-time password. Sign in with it at once and change
   it — in the UI, or `POST /users/{user_id}/password` with `current_password` — which also revokes
   every session it opened. This is acceptable for dev; **do not use it for prod.** The real fix is
   for `create_user.py` to read the password from a Secrets Manager secret the task role may read,
   and §12 lists it as open.
3. **Create the other users** from the Admin screen or with `POST /users`. Roles are a set, and
   Admin does not include Approver or Analyst (DEC-703): an Admin who is also the person who approves
   champions is given both roles, deliberately. Plan prerequisite P6 names who approves per client.
4. **Recovery.** If the last Admin is locked out, create another Admin the same way; that Admin can
   reset the first one's password. The API refuses to disable or demote the last active Admin
   (DEC-712), so this is the only way to reach a deployment with none.

### 11.3 A prod deployment answers 503 until sign-in is configured

If a task on `env=prod` reads `auth_mode=off`, it starts, logs an error, and answers every route
except `GET /healthz` and `POST /auth/login` with **503 `AUTH_NOT_CONFIGURED`** (DEC-702). The
health probe still passes, so ECS does not roll the deployment back and the load balancer keeps
routing to it: the symptom is a deployment that looks healthy and refuses everybody. That is
deliberate — `off` means every request acts as the all-roles local operator, which on a public load
balancer is no access control at all, so the product fails closed rather than open.

A deployment built from this tree cannot get there by accident: the operations stack writes
`auth_mode=local`, and `infra/context.py` refuses `-c auth_mode=off` for prod. It happens when a
task reads its settings before the operations stack has written them — the first deployment, until
the restart of §4.2 — or when somebody edits the parameter by hand. The fix is the parameter at
`local`, a restart, and an Admin (§11.2). `scripts/smoke_deployment.py` reports the state in one
line either way; on dev, `off` is a warning in the log and a warning from the smoke test.

### 11.4 The audit-export bucket, with Object Lock

The audit table in the database is the record, append-only (DEC-714). An export is a copy of a
window of it, as JSON lines, written where a compromise of the database cannot reach it: the bucket
`<bucket_name_prefix>-<env>-<account>-audit` (output `AuditBucketName`), created with **S3 Object Lock
in COMPLIANCE mode**. Each export is written with a retain-until date `audit_retention_days` ahead,
and the bucket's default retention is the same number of days. In COMPLIANCE mode *nobody*, the root
user included, can delete a locked version or shorten its lock until the date passes, and the bucket
policy additionally denies every principal `DeleteObject` and `DeleteObjectVersion`, so a delete
marker cannot hide an export either. The task role may put objects under `audit/` and do nothing
else there: it cannot read, list or delete an export.

**Decide `audit_retention_days` before the first prod export**, with Minfy's legal or compliance
team: it is permanent for every object written under it. The prod default, 2555 days, is the
setting's own default rather than a legal answer. Dev defaults to one day, the minimum S3 accepts,
because a dev account is torn down and rebuilt.

An Admin exports with `POST /audit/exports`; the export is itself an audited event carrying the
file's SHA-256. Check the lock with your own credentials:

```bash
aws s3api get-object-lock-configuration --bucket <audit bucket> --region ap-south-1
aws s3api list-objects-v2 --bucket <audit bucket> --prefix audit/ --region ap-south-1 --query 'Contents[].Key'
aws s3api get-object-retention --bucket <audit bucket> --key <key> --region ap-south-1
```

The first reports `ObjectLockEnabled: Enabled` with a `COMPLIANCE` default rule; the last reports
`Mode: COMPLIANCE` and a `RetainUntilDate`. The bucket is retained by `cdk destroy` at every
`env_name` (§8.2): removing a dev one means waiting out the lock, deleting the bucket policy that
denies deletion, deleting every version, then the bucket.

### 11.5 Schedules: EventBridge Scheduler and the job task

With `scheduler_backend=eventbridge`, saving a schedule in the product creates an EventBridge
Scheduler schedule in the group `marketing-ai-<env>`. When it fires, the scheduler assumes
`marketing-ai-<env>-scheduler` and runs one ECS task in the deployment's cluster from the task
family `marketing-ai-<env>-job` — the product's own image and the API's own task role, container
`job`, command `python -m scripts.fire_schedule` with the schedule's arguments. The schedule names
the family, never a revision, so it keeps firing across deployments. The scheduler's role can start
that one family in that one cluster and nothing else, and its trust policy admits only schedules in
this deployment's group.

Scheduled work runs as the system scheduler principal, which holds the Analyst role only: a
scheduled retraining produces a challenger, and the champion rule and a person's approval still
decide what is promoted.

Check what exists and what fired, with your own credentials (the task role has no `List` action):

```bash
aws scheduler list-schedules --group-name marketing-ai-dev --region ap-south-1 \
  --query 'Schedules[].[Name,State]' --output table
aws scheduler get-schedule --group-name marketing-ai-dev --name <schedule> --region ap-south-1 \
  --query '{expression:ScheduleExpression,timezone:ScheduleExpressionTimezone,cluster:Target.Arn,role:Target.RoleArn,family:Target.EcsParameters.TaskDefinitionArn}'
aws logs tail /marketing-ai/dev/api --log-stream-name-prefix job/ --since 2h --region ap-south-1
```

A firing logs to the API log group under the `job/` stream prefix, so a failed firing is counted by
the same error metric filter as the API. `scheduler_group_name` must be `marketing-ai-<env>`: it is
the IAM boundary, and a schedule created in any other group is refused.

### 11.6 Alerts, and confirming the email subscription

There are two SNS topics, deliberately. `marketing-ai-<env>-alarms` carries CloudWatch alarms and
only CloudWatch publishes to it. `marketing-ai-<env>-alerts` carries the application's own alerts —
drift above its threshold, a performance drop beyond the setting, a failed scheduled job — and only
the task role publishes to it, so the application can never forge an alarm's "OK". `-c alert_email=`
subscribes the same address to both (plan prerequisite P3).

**Each subscription sends a confirmation email, and nothing is delivered until its link is
clicked.** A publish to a topic whose only subscription is pending still succeeds, so an
unconfirmed subscription looks exactly like a quiet week. Check both after the first deployment:

```bash
for topic in alarms alerts; do
  aws sns list-subscriptions-by-topic --region ap-south-1 \
    --topic-arn "arn:aws:sns:ap-south-1:<account>:marketing-ai-dev-$topic" \
    --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
done
```

A confirmed subscription shows an ARN; an unconfirmed one shows `PendingConfirmation`. Confirmation
links expire. A new `make aws-deploy` does not resend one, because nothing changed; removing
`alert_email` from `infra/cdk.context.json`, deploying, restoring it and deploying again does. To see the path work end to end,
publish once with your own credentials:

```bash
aws sns publish --region ap-south-1 --topic-arn "<AlertTopicArn>" \
  --subject "marketing-ai dev: delivery test" --message "Test of the alert path. No action needed."
```

### 11.7 The retention job

`governance.retention_days` is enforced by `scripts/run_retention.py`: it deletes uploads, datasets
and row-level artefacts older than each use case's setting, and keeps models and aggregate reports.
A dry run is the default, and it is the plan the real run executes, key for key (DEC-736). Run it
inside the deployment, dry first:

```bash
.venv/bin/python -m scripts.run_in_deployment --env dev -- python -m scripts.run_retention
.venv/bin/python -m scripts.run_in_deployment --env dev -- python -m scripts.run_retention --apply
```

`--json` prints the plan as JSON for a ticket. `--apply` writes one `privacy.retention.apply` audit
event carrying counts, never a key or a value. Retention is measured in days, so daily is often
enough; read the dry run before the first `--apply` on any deployment holding a customer's data.

**A delete on the artefact bucket is not yet the end of the bytes.** The bucket is versioned (§8.1),
so the job's delete — an ordinary `DeleteObject` through the storage layer — lays a delete marker and
leaves the object behind as a noncurrent version. Two mechanisms exist to remove it: the task role
may delete object *versions* under `uploads/` and `runs/` and nowhere else (`infra/policies.py`,
`EraseObjectVersions`), and `engine/privacy/lifecycle.py` builds per-client-prefix lifecycle rules
that expire noncurrent versions `grace_days` (`configs/privacy.yaml`) after the job's own deadline, as
a backstop. On the M50 walk (2026-09-23) nothing in the tree yet called either one: no code deleted
a version, and nothing applied the lifecycle rules. Until that changes, after an `--apply` check
what is left and treat every noncurrent version under those prefixes as data still held:

```bash
aws s3api list-object-versions --bucket <BucketName> --prefix uploads/ --region ap-south-1 \
  --query '{versions:length(Versions[?IsLatest==`false`] || `[]`),markers:length(DeleteMarkers || `[]`)}'
aws s3api get-bucket-lifecycle-configuration --bucket <BucketName> --region ap-south-1 \
  --query 'Rules[].ID'
```

### 11.8 Erasure, access requests and consent

A DPDP erasure request (`POST /privacy/erasure`, plan M48), an access request and the consent ledger need
nothing on a deployment beyond what §11.1 switches on, and erasure records the request and its
outcome in the audit log without the data principal's id in the clear. On the M50 walk the engine
side (`engine/privacy/erasure.py`, `access_export.py`, `consent.py`) was in the tree and the HTTP
routes were not yet; before the acceptance step of `docs/M50_CHECKLIST.md` §6.2, confirm the route is
in the deployment's `/openapi.json`. What §11.7 says about noncurrent versions applies to an erasure
too: until a version delete or the lifecycle backstop runs, the erased bytes survive as old versions. Models trained on the person's data are flagged for retraining at the
next scheduled cycle, not retrained at once. The breach runbook is in `docs/RUNBOOK.md`.

### 11.9 What still needs a decision

| Prerequisite | What waits on it |
|---|---|
| P1, a dev account | Everything in `docs/M50_CHECKLIST.md` |
| P2, Bedrock model access | `-c bedrock_enabled=true` and the assistant's cost row (§6.2) |
| P3, a budget and an alert email | `infra/cdk.context.json` (§4.2), §6.4 and §11.6 |
| P4, single-tenant or multi-tenant | Plan M51. This document builds one deployment per client account |
| P5, the identity provider | A third `auth_mode` value. Until then sign-in is the built-in store |
| P6, who approves champions | Which users §11.2 gives the Approver role |
| Legal review | `audit_retention_days` for prod (§11.4) and the DPDP controls (§9.2) |

---

## 12. What the M50 walk found

M50 walked this document from §1 to §11 as a first-time operator would, on 2026-09-23, with no AWS
account: every command that runs offline was run (`make infra-synth`, `make infra-test`, every
script's `--help` and `--dry-run`, settings loading with the documented variables, Alembic's offline
`--sql` mode) and every command that needs an account was checked against the code it names. What it
found, and where each finding was fixed:

| # | Where | What a first-time operator would have hit | Fixed in |
|---|---|---|---|
| 1 | Top of the document | It said `scripts/smoke_deployment.py` and `infra/README.md` did not exist; the README did, and the smoke test did not | This document; `scripts/smoke_deployment.py` written |
| 2 | §2 | The AWS CLI, the `.venv` that `make aws-bootstrap` and the helper scripts run from, and the exact `cdk bootstrap` command were not listed | §2 |
| 3 | §2, §6.2 | Bedrock was "reserved, not used" and `LlmCostUsd` "not in this repository"; the generative phase is here, and the paid Bedrock test pointed at this document for variables it did not mention | §2, §6.2, §6.5 |
| 4 | §4.1 | A bare synthesis prints five warnings the document did not list | §4.1 |
| 5 | §4.2, §6.4 | Context given with `-c` by hand (`alert_email`, the budget, the certificate) is dropped by the next `make aws-deploy`, which then deletes the subscriptions, the budget and the HTTPS listener | §4.2 (`infra/cdk.context.json`, confirmed with a synthesis) and `.gitignore` |
| 6 | §4.2 | The first deployment's tasks start before the operations stack writes the Phase 4b parameters, so they run with sign-in off until restarted | §4.2 (restart step) |
| 7 | §4.3 | `make aws-bootstrap ENV=dev`, as written, read the laptop's settings, skipped every probe, and printed "4/4 checks passed; this deployment is ready" | `scripts/aws_bootstrap.py`: skipped is not passed, and a named deployment with nothing probed fails |
| 8 | §4.3 | With the AWS loader and no credentials, it died with a traceback | `scripts/aws_bootstrap.py` |
| 9 | §4.3 | Its database probe used the composed `postgresql://` URL directly, which asks for psycopg 2 — not in the image — so it could never pass | `scripts/aws_bootstrap.py` uses the application's engine factory |
| 10 | §4.3, §7.1 | Its migrations dropped `postgres_schema` and would have created the tables in `public`, while the image's `migrate` reads `marketing_ai` | `scripts/aws_bootstrap.py` passes URL and schema together |
| 11 | §4.3, §7.1, §10.5 | The database is in isolated subnets, so the bootstrap, `make migrate` and the probes cannot run from a laptop or a CI runner, and from there the probes test the operator's credentials rather than the task role | `scripts/run_in_deployment.py` (one-off task with the service's own definition and network); §4.3, §7.1, §10.5 |
| 12 | §4.3 | The sample output named `region=` (it is `aws_region=`), omitted `llm_backend`, and counted seven use cases (there are eight) | §4.3 |
| 13 | §4.4 | CI built and pushed the image, then `make aws-deploy` built it again without the ECR variables and failed; the bootstrap ran from a `.venv` CI never creates; the smoke module did not exist; and `--require-approval broadening` cannot prompt in CI, so a first deployment there cannot succeed | `.github/workflows/deploy-dev.yml`; §2.1, §4.4 |
| 14 | §5 | With sign-in on, every `curl` needs a token and the walk-through needs a user | §5, §11.2 |
| 15 | §6.2 | Plan M50's cost questions — per strategy, per 100,000 scored rows, per 1,000 assistant questions — had no row to record into | §6.2 (rows added, unmeasured) |
| 16 | §7.1 | `alembic upgrade --sql` with a schema produced DDL that referenced a schema it never created | `alembic/env.py` |
| 17 | §8.3, §9.2 | Retention, erasure and authentication were described as absent | §8.3, §9.2, §11 |

Still open, because each is a change to code this milestone does not own, or needs the account:

- `scripts/create_user.py` cannot receive a password on a deployment except through the `RunTask`
  command line (§11.2). It should read one from a Secrets Manager secret the task role may read.
- The `migrate` target's help text in the Makefile names a prefixed `DATABASE_URL` variable that is
  not a setting; the variable it means is `MARKETING_AI_POSTGRES_DSN`.
- `infra/README.md` says the application refuses to start on an unknown `MARKETING_AI_*` variable;
  it ignores one (§3.5), and the refusal is the deployment test's.
- `make aws-bootstrap` still runs the probes where it is invoked; running it through
  `scripts/run_in_deployment.py` would make the target mean what §4.3 needs.
- Retention and erasure delete objects on a versioned bucket without removing the noncurrent
  version, and the lifecycle backstop that would expire it is built but applied by nothing (§11.7,
  §11.8). The IAM for both is deployed; the code that uses it is Phase 4b M48's to write.
- The erasure and access-request HTTP routes were not in `api/routes/` on the walk (§11.8). §11 was
  written against Phase 4b's code as it stood that day; re-read it against the final code before the
  checklist's §6.2.
- Everything that needs the account: `docs/M50_CHECKLIST.md`.

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
