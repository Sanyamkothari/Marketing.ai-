# `infra/` - the AWS deployment

One CDK application, seven stacks, one direction. Everything an operator has to decide is a context
key; everything they must not have to decide has a default in `infra/context.py` and nowhere else.

```
network -> storage -> observability -> database -> sagemaker -> compute -> budgets
```

Nothing points backwards. Two of those arrows exist for reasons the resources do not show, and
`infra/app.py` explains both: observability is deployed before the database so that it, rather than
RDS, creates `/aws/rds/instance/<id>/postgresql` and decides how long those logs are kept; sagemaker
is deployed before compute because the task role's `iam:PassRole` has to name an execution role that
already exists.

## Running it

`infra/` has its own virtualenv. `aws-cdk-lib` pulls jsii and a node bridge that `engine/` must
never depend on, and `make lint` has to keep working in a checkout that never installed it.

```
make infra-setup    # .venv-infra: pip install -e ".[deploy]" -r infra/requirements.txt
make infra-lint     # ruff + black (from .venv) + mypy --strict (from .venv-infra)
make infra-test     # the offline assertions; no AWS account, no node beyond jsii
make infra-synth    # a real `cdk synth` of all seven stacks
make infra-nag      # the same synthesis with the AwsSolutions checks switched on
```

`ENV=prod make infra-synth` synthesises production, but `AppContext.validate` will refuse it until
you also pass a certificate and a domain - see **What `env_name` changes**.

None of these needs credentials. `make aws-deploy` and `make aws-bootstrap` do.

## Context keys

Every key is readable as `-c <name>=<value>` or `-c marketing-ai:<name>=<value>`, and **a key this
application does not know is an error rather than a silent default**. That is the whole reason
`AppContext` exists: `-c db_storage_bg=50` is as acceptable to the CDK toolkit as `db_storage_gb`
is, and the only symptom of the typo would be a database of the wrong size three weeks later.

| key | default | notes |
|---|---|---|
| `env_name` | `dev` | `dev` or `prod`. Changes real things; see below. |
| `region` | `ap-south-1` | |
| `client_id` | none | Becomes the `client` tag and the `client_id` setting. |
| `bucket_name_prefix` | `marketing-ai` | Bucket is `<prefix>-<env>-<account>`. |
| `kms_key_alias` | `alias/marketing-ai-<env>` | |
| `db_instance_class` | `db.t4g.small` | Spelt as the RDS console spells it, with the `db.` prefix. |
| `db_storage_gb` | `50` | Autoscales to 4x. |
| `db_multi_az` | `false` in dev, `true` in prod | The one `env_name` default that can be overridden. |
| `db_backup_retention_days` | `7` | Must be at least 1; zero would disable backups silently. |
| `api_cpu` / `api_memory` | `2048` / `8192` | Checked against AWS's Fargate combination table at synth time. |
| `api_min_tasks` / `api_max_tasks` | `1` / `3` | |
| `container_insights` | `false` | A per-task charge, so opt-in at every `env_name`. |
| `sagemaker_instance_train` | `ml.m5.2xlarge` | |
| `sagemaker_instance_process` | `ml.m5.xlarge` | |
| `max_concurrent_jobs` | `3` | |
| `bedrock_enabled` | `false` | |
| `bedrock_model_ids` | empty | **Empty synthesises an explicit `Deny`, never an absent `Allow`.** |
| `nat_gateways` | `1` | See **NAT gateway or interface endpoints**. |
| `vpc_endpoints` | empty | Interface endpoint service names, e.g. `ecr.api,ecr.dkr,logs,secretsmanager,ssm`. |
| `alert_email` | none | Without it, alarms fire into a topic nobody is subscribed to, and synthesis says so. |
| `monthly_budget_usd` | none | **No default.** No budget at all, rather than a guessed one. Requires `alert_email`. |
| `alarm_thresholds` | empty | `Name=value,Name=value`; see **Alarms without thresholds**. |
| `domain_name` / `certificate_arn` | none | Supply both or neither. |
| `image_digest` | none | `<registry>/<repository>@sha256:...`. A tag can be moved after it was tested. |
| `cdk_nag` | `false` | What `make infra-nag` passes. |

## What `env_name` changes

`env_name` is not a label. This is the complete list, and every row has an assertion in
`tests/infra/test_env_name.py` made against the CloudFormation that would actually be deployed.

| | dev | prod |
|---|---|---|
| `db_multi_az` (default) | false | true |
| `db_deletion_protection` | false | true |
| removal policy (bucket, key, database) | `Delete` | `Retain` |
| load balancer | HTTP unless a certificate is given | HTTPS always; no certificate is refused |
| log retention | 30 days | 365 days |
| `cors_origins` written to SSM | `*` unless `domain_name` is given | `https://<domain_name>` |

A prod synthesis without `certificate_arn` and `domain_name` fails at synth time with the fix in the
message. An HTTP-only load balancer serves every upload, every pre-signed download URL and every API
response in clear text over the public internet; it is a dev-only convenience and the synthesis says
so in capitals every time it is used.

## The task definition sets only frozen environment variables

`engine/settings.py` refuses to start when it sees a `MARKETING_AI_*` variable that is neither a
`Settings` field nor in `NON_FIELD_ENV_VARS`. A task definition that invents a name is therefore a
deployment that does not boot, and no amount of care is a substitute for a test: the synthesised
task definition's environment is compared against that frozen table in
`tests/infra/test_task_definition.py`.

The task carries three variables - `MARKETING_AI_SETTINGS_SOURCE=aws`, `MARKETING_AI_ENV` and
`MARKETING_AI_AWS_REGION` - which is *which deployment this is and where to read it from*. Everything
else is read at start-up from `/marketing-ai/<env>/<field_name>` in Parameter Store (flat,
non-recursive, leaf names are field names) and from the JSON secret `marketing-ai/<env>/app`.

## IAM

Policies are written as explicit statement lists in `infra/policies.py`, not generated by
`grant*` helpers, because the policy is the document a reviewer reads. Two properties are enforced
by tests rather than by care:

* **No `*` resource that is not on the list.** `RESOURCE_WILDCARD_ALLOW_LIST` names every action for
  which AWS documents no resource types - `ecr:GetAuthorizationToken`, `cloudwatch:PutMetricData`
  (narrowed by a `cloudwatch:namespace` condition), and the EC2 network-interface actions a
  SageMaker job with a `VpcConfig` requires. `tests/infra/test_iam.py` walks every identity policy
  in all seven synthesised stacks, in both `dev` and `prod`, and fails on any `Allow` with
  `"Resource": "*"` whose actions are not all on that list - including one that arrived inside a
  construct nobody read. A second test fails if the allow-list grows an entry nothing uses.
* **`s3:ListBucket` on the bucket itself.** Without it S3 answers `403 AccessDenied` for a key that
  is simply not there, because it will not confirm an object's absence to a principal that may not
  list. `S3Storage.exists()` would then be unable to tell "no such run" from "your role is wrong",
  and every absent-artefact path in the product would turn into a permissions bug report. The
  statement deliberately carries no `s3:prefix` condition: there is no `s3:prefix` in a `GetObject`
  request, so the condition would fail closed and bring the 403 back.

S3 object access is scoped to the four prefixes the product actually writes - `uploads/`, `runs/`,
`models/`, `_bootstrap/`. SageMaker access is scoped to job names beginning with
`marketing-ai-`, and there is **no `List*` action**: the runner only ever describes a job whose name
it already knows, and `sagemaker:ListTrainingJobs` cannot be scoped to a prefix, so granting it
would mean showing this role every job in the account.

## S3

Block public access, SSE-KMS with a customer-managed key, versioning, TLS-only (and TLS 1.2
minimum), and an `AbortIncompleteMultipartUpload` rule so a failed predictor upload does not leave
paid-for garbage behind.

The encryption deny is deliberately **not** the snippet from every blog post. Denying any
`PutObject` that does not carry `x-amz-server-side-encryption: aws:kms` would break this product,
because `S3Storage` sets that header only when `s3_kms_key_id` is configured. A header-less PUT is
not an unencrypted one - S3 applies the bucket's default encryption, which is this same customer
key. So the bucket policy denies a PUT that names the **wrong** encryption or the **wrong key**, and
lets a silent one fall through to the default. Both denies are conditioned on `Null: false`, which
is what makes "wrong" and "absent" different.

## NAT gateway or interface endpoints

The VPC spans two availability zones with private subnets for the tasks and isolated subnets for the
database. How those private subnets reach AWS is a parameter, not a decision baked into the stack:

* `-c nat_gateways=1` (the default) - the tasks reach AWS over a NAT gateway.
* `-c nat_gateways=0 -c vpc_endpoints=ecr.api,ecr.dkr,logs,secretsmanager,ssm` - no NAT gateway,
  and traffic stays on the AWS network.

`AppContext.validate` **refuses `nat_gateways=0` without those five endpoints**, and this is a guard
worth having rather than a formality. Without `ecr.api` and `ecr.dkr` a Fargate task cannot pull its
image; without `logs` it cannot start its `awslogs` driver, so it stops with a log-driver error and
no log line; without `secretsmanager` and `ssm` it exits on `load_settings()`'s first statement,
before it can say why. Each failure looks like a different bug and none of them looks like "no route
to the internet". The S3 gateway endpoint is always created - it is a route-table entry rather than
an ENI, so it is neither a per-AZ resource nor priced like one - and it carries every artefact and
every image layer blob.

**The shape of the charge** (no figures here; nobody in this repository has an account to measure
one, and a number nobody measured does not belong in a file people plan budgets from):

* An **interface endpoint** is billed **per endpoint, per availability zone, per hour**, for as long
  as it exists, plus a charge per GB of data processed. Five endpoints across two AZs is ten
  endpoint-AZ-hours for every hour the VPC exists, whether or not anything uses them.
* A **NAT gateway** is billed **per gateway per hour** plus a charge per GB processed. One gateway
  shared by both AZs is one gateway-hour per hour - and a single point of failure; one per AZ is two.

Which is cheaper depends on how many endpoints you need and how much data moves, so it is a
deployment decision rather than a default. Current prices are on AWS's own pages and only there:

* PrivateLink / interface endpoints - <https://aws.amazon.com/privatelink/pricing/>
* NAT gateway - <https://aws.amazon.com/vpc/pricing/>

## The database, and the one seam in it

RDS for PostgreSQL 16: private, encrypted with the customer key, `rds.force_ssl=1` so a client that
forgets `sslmode=require` gets a connection error on day one rather than an audit finding, automated
backups, and a port that is deliberately not 5432.

There are **two secrets**, and the difference matters:

* `marketing-ai/<env>/db` is the **credential**. It is generated by the stack, it is what RDS knows
  about, and it is what AWS's single-user rotation function rewrites every 90 days. Nothing but the
  database and the rotation function reads it. The application is never given it.
* `marketing-ai/<env>/app` is the **application document** that `engine/aws/secrets.py` fetches,
  whose keys are `Settings` field names. It holds one key, `postgres_dsn`, composed at deploy time
  from the credential through a CloudFormation dynamic reference.

> **Operator note - what to do when the credential rotates.** That composed URL is a *snapshot*
> (DEC-376). AWS's rotation function changes `password` in the credential secret and knows nothing
> about the application document, so **after a rotation the application's URL carries the previous
> password until this stack is redeployed**. Redeploy the database stack to recompose it. This is
> why the rotation interval is set to the longest one that is still a rotation rather than to a
> number that sounds diligent: every rotation currently costs a deployment. The fix is not in
> `infra/` - it is for `engine.settings` to accept the standard RDS credential document
> (`username`, `password`, `host`, `port`, `dbname`) and build the URL itself, at which point this
> stack keeps one secret, rotation is end-to-end, and the `AwsSolutions-SMG4` suppression below
> goes away with it.

## The service

ECS Fargate behind an Application Load Balancer, health-checked on `/healthz`, autoscaling between
`api_min_tasks` and `api_max_tasks` on CPU.

The health check's **start period is 120 seconds, and that number is chosen, not measured.** The
reason is that AutoGluon's import is heavy - which is a reason, not a measurement. Nobody here has
timed a cold start on Fargate, because doing so needs an account. If you have one, time it and
change the number.

## Observability

`infra/observability.py` contains **no widget list and no alarm threshold of its own**. It reads two
files that another owner generates with `python -m scripts.gen_dashboard`:

* `infra/observability/dashboard.json` - a CloudWatch dashboard body: `{"widgets": [...]}`.
* `infra/observability/alarms.json` - `{"alarms": [...]}`, each entry with `name`, `namespace`,
  `metric_name`, `evaluation_periods` and `threshold` required, and `dimensions`, `statistic`,
  `period_seconds`, `datapoints_to_alarm`, `comparison_operator`, `treat_missing_data` and
  `rationale` optional. `${Env}` and `${ClientId}` are substituted by the stack.

If neither file exists the stack still deploys and you get the log groups, the SNS topic and the
metric filters, with a warning saying what is missing.

### Alarms without thresholds

An alarm whose `threshold` is a substitution such as `"${StageDurationSecondsAlarmThresholdSeconds}"`
is **not created** unless the deployment supplies the value:

```
-c alarm_thresholds=StageDurationSecondsAlarmThresholdSeconds=900,JobCostUsdDailyBudgetUsd=25
```

Synthesis names every alarm it skipped and the exact flag that would create it. This is deliberate:
the honest answers to "what should this threshold be" are "the operator tells us" and "there is no
alarm". Inventing a plausible number would be neither, and it would read to the next person as
though somebody had measured it. The alarm's `rationale` is carried into its CloudWatch description,
so whoever is woken at 3am reads the argument and not just the number.

## cdk-nag

`make infra-nag` runs the `AwsSolutions` pack and **passes with no findings**, for `dev` and for
`prod`. The same checks also run offline in `tests/infra/test_nag.py`, so a suppression that stops
matching fails `make infra-test` a minute after it is written rather than at the end of a pipeline.

> `cdk-nag 3.0.2 is incompatible with aws-cdk-lib 2.270.0` - `cdk synth` dies inside the aspect
> visitor with `TypeError: aspectApplication.aspect.visit is not a function`. `pyproject.toml` pins
> `cdk-nag==2.38.2`, which works (DEC-366).

### Fixed rather than suppressed

* **`AwsSolutions-RDS11`** (default endpoint port) - the database was moved off 5432.
  `infra/network.py::POSTGRES_PORT` is read both by the instance and by its ingress rule, so the two
  cannot drift, and nobody ever types the port because the application is handed a URL composed from
  the instance's own endpoint attributes. It is worth little on its own - the instance is in an
  isolated subnet reachable from two named security groups - and it costs nothing.
* **`AwsSolutions-ECS4`** (Container Insights) - now a context key, `-c container_insights=true`,
  rather than a hard-coded `DISABLED`. Off by default because it is a per-task charge; the
  suppression below exists only for a deployment that actually leaves it off.

### Suppressed, and why

Every suppression lives in `infra/nag_suppressions.py` with its full reason. In short:

| rule | where | why |
|---|---|---|
| `AwsSolutions-S1` | the access-log bucket | It *is* the server access log bucket. S3 will not deliver a bucket's access logs into itself, and a third bucket moves the question one hop. It holds log records only, blocks public access, enforces TLS 1.2 and expires on the deployment's retention. |
| `AwsSolutions-ECS2` | task definition | The rule wants no plain environment variables. The three present are which deployment this is and where to read it from; they are already public in the stack name. Every secret is fetched by the task from one Secrets Manager ARN and none is rendered into the template. |
| `AwsSolutions-EC23` | ALB security group | A public API's load balancer, so `0.0.0.0/0` is the requirement. What the rule guards against - a wide open port range - is not what this does: exactly one port, 443 with a certificate and 80 only in dev, with egress restricted to the tasks. |
| `CdkNagValidationFailure` | endpoint security group | `AwsSolutions-EC23` cannot evaluate a rule whose source is the VPC's own CIDR, because that synthesises to an `Fn::GetAtt`. It would have passed. Suppressed so a *real* validation failure is visible instead of being one line of known noise. |
| `AwsSolutions-SMG4` | `marketing-ai/<env>/app` | The credential does rotate; this second secret is not a credential of anything, so there is no service for a rotation function to rotate against. See the operator note above - it goes away when `engine.settings` reads the credential document directly. |
| `AwsSolutions-IAM5` | three role policies | Two different things, split in the module. ARNs with a wildcard **path** (`.../uploads/*`, `.../training-job/marketing-ai-*`, a log group's `:*` streams) are the boundary, not a widening, and the individual ARNs cannot be named because they contain run ids that do not exist at synth time. Bare `Resource::*` is accepted only for actions AWS documents with no resource types - and `tests/infra/test_iam.py` is stricter than the rule, so it is the test, not the suppression, that holds that line. |
| `AwsSolutions-RDS3` | database, dev only | Multi-AZ is off because this deployment did not ask for it. Overridable with `-c db_multi_az=true`; a prod synthesis never reaches the suppression. |
| `AwsSolutions-RDS10` | database, dev only | Deletion protection is off because `removal_policy_destroy` is on, which is what `env_name=dev` means. |
| `AwsSolutions-ECS4` | cluster, when insights are off | A recurring per-task charge left to an explicit opt-in. What is genuinely lost is per-task CPU and memory utilisation - turn it on before tuning `api_cpu` or `api_memory`. |

The last three are **conditional**, and each is keyed on the property the rule actually checks
rather than on `env_name`. That distinction has teeth: `db_multi_az` is overridable, so
`-c env_name=dev -c db_multi_az=true` raises no `RDS3` finding while its deletion protection is
still off and `RDS10` still fires. Keying both on `env_name` would have attached one suppression to
a finding that no longer exists and left the other uncovered.

## Tags

Every resource in every stack carries `product=marketing-ai`, `env=<env_name>`, and `client` when
`client_id` is set. `use_case` is deliberately absent at this level: one deployment serves every use
case, so it is carried instead on the things that do belong to one - `JobSpec.tags` puts it on a
SageMaker job, and the object prefixes carry it in S3 (DEC-375).

## What has not been verified

There is no AWS account and no credentials in the environment this was built in. `cdk synth`,
`cdk-nag` and every assertion in `tests/infra/` really run, and they are what the claims above rest
on. **Nothing has been deployed.** No cost, no latency, no cold-start time and no failover behaviour
here has been measured, and where a number is chosen rather than measured it says so.
