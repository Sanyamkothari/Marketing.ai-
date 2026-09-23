# Security review checklist (Phase 4b, M52)

**Status:** offline half done on 2026-09-23, against `main` with Part 1 (M46–M49) in progress in the
same tree. **No AWS account exists**, so every check below was run against the code, the synthesised
CloudFormation and the dependency pins, never against a deployment. The account-day half is the
last section.

Everything here can be re-run; each section starts with the command.

## Summary

| # | Check | Result | Open items |
|---|---|---|---|
| 1 | cdk-nag (`AwsSolutions`) on dev and prod | **Clean**: 0 non-compliant; 32 prod / 34 dev suppressions, each with a written reason | Makefile's `synth --all` flag is ignored by CDK CLI 2.1142.0 (harmless) |
| 2 | Dependency vulnerability scan (pip-audit) | **No known vulnerabilities** in 148 app pins and 98 infra-venv packages | npm (CDK CLI), container OS packages and the prototype's node deps are not covered offline |
| 3 | Secrets scan, tree and full history | **Clean**: ~590 files, every commit on every ref; 5 reviewed test fakes listed by fingerprint | Add gitleaks to CI on account day |
| 4 | TLS everywhere | **Met on prod** for every public and AWS-API hop | F-1 `sslmode=require` does not verify the server; F-2 dev is HTTP-only without a certificate; ALB→task hop is plain HTTP inside the VPC (accepted) |
| 5 | Least privilege, every IAM role | **6 roles in the templates reviewed** (plus the rotation function's, which is not in them); no `*` action, every `*` resource is either a key/name prefix or an API with no resource-level permissions | F-3 the API task role can rewrite the bucket lifecycle and delete object versions; F-4 scheduled jobs run as the API task role; F-5 ECS execution role holds `kms:Encrypt`/`GenerateDataKey` it does not need |
| 6 | Found along the way | — | **F-6 (high, privacy): RDS `log_statement=mod` logs bind parameters**, so principal ids and password hashes reach CloudWatch Logs and survive an erasure |

Findings F-1…F-6 are in files this workstream does not own (`infra/**`, Phase 4a). They are listed
with a proposed fix in [Findings](#findings); none was changed here.

---

## 1. cdk-nag

```bash
make infra-setup                  # once; .venv-infra already existed here and was not reinstalled
make infra-nag ENV=dev            # = cdk synth --all -c env_name=dev -c cdk_nag=true
# prod refuses to synthesise without a certificate (AppContext.validate), so pass placeholders:
cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 synth -c env_name=prod \
  -c cdk_nag=true -c certificate_arn=arn:aws:acm:ap-south-1:123456789012:certificate/<uuid> \
  -c domain_name=example.invalid -c alert_email=ops@example.invalid
```

Run on 2026-09-23 with CDK CLI 2.1142.0 (the Makefile's pin), output to a scratch directory so the
parallel workstream's `infra/cdk.out` was not overwritten. Both environments synthesised; cdk-nag
fails the synth on any unsuppressed `Error`, and the reports (`AwsSolutions-*-NagReport.csv`) have
**no `Non-Compliant` row** in any of the eight stacks.

Suppressions, all written in `infra/nag_suppressions.py` with a reason that names the trade-off:

| Rule | What it flags | Where | Count (prod) | Review |
|---|---|---|---|---|
| AwsSolutions-IAM5 | `*` in a resource | task, SageMaker, execution, operations, scheduler roles | 26 | Agree for all but the ones in F-3/F-5: every `*` is the last segment of a key prefix (`uploads/*`), a name prefix (`training-job/marketing-ai-*`), a log-stream suffix, or an API without resource-level permissions (`ecr:GetAuthorizationToken`, `cloudwatch:PutMetricData` - the latter narrowed by a namespace condition) |
| AwsSolutions-EC23 | `0.0.0.0/0` ingress | ALB security group (443 only); VPC-endpoint group | 2 | Agree: public API. The endpoint group is a false positive (its rule is the VPC CIDR) |
| AwsSolutions-ECS2 | plain environment variables | API and job task definitions | 2 | Agree: three non-secret selectors (`MARKETING_AI_SETTINGS_SOURCE`, `MARKETING_AI_ENV`, `MARKETING_AI_AWS_REGION`); every secret is read from Secrets Manager/SSM at start |
| AwsSolutions-ECS4 | Container Insights off | cluster | 1 | Agree as a cost choice; turn it on before tuning `api_cpu`/`api_memory` |
| AwsSolutions-SMG4 | secret without rotation | `marketing-ai/<env>/app` | 1 | Agree with the stated seam (DEC-376): the credential secret rotates, the composed DSN does not until a redeploy |
| AwsSolutions-RDS3 / RDS10 | no Multi-AZ / no deletion protection | database, **dev only** | 2 (dev) | Agree: both are on for prod |

Also noticed: CDK CLI 2.1142.0 prints `Unknown option(s): --all. These will be ignored.` for
`cdk synth --all` (Makefile targets `infra-synth`/`infra-nag`). Synth still covers every stack, so
this is cosmetic; drop `--all` from those two targets when the Makefile is next edited.

## 2. Dependency vulnerability scan

```bash
python3.11 -m venv /tmp/pip-audit-venv && /tmp/pip-audit-venv/bin/pip install pip-audit
/tmp/pip-audit-venv/bin/pip-audit -r requirements-freeze.txt --no-deps --disable-pip
.venv-infra/bin/python -m pip freeze --exclude-editable > /tmp/infra-freeze.txt
/tmp/pip-audit-venv/bin/pip-audit -r /tmp/infra-freeze.txt --no-deps --disable-pip
```

pip-audit 2.10.1 in a throwaway venv (never the project's), against the PyPI/OSV advisory data on
2026-09-23:

| Input | Packages | Known vulnerabilities |
|---|---|---|
| `requirements-freeze.txt` (the pins the app image installs) | 148 | **0** |
| `.venv-infra` (aws-cdk-lib, constructs, cdk-nag, jsii and the infra tools) | 98 | **0** |

Not covered offline, and why:

* **The container's OS packages** (`python:3.11-slim` + `RUNTIME_APT_PACKAGES`). ECR scans on push
  (`image_scan_on_push=True` in `infra/storage.py`); read the first push's findings on account day.
* **The CDK CLI** is an npm package fetched by `npx` at a pinned version; `npm audit` needs a
  lockfile this repository does not keep. It runs on the operator's machine, never in the product.
* **The prototype's node test dependencies** (jsdom, playwright) are dev-only and never shipped.
* A clean result is "no *known* advisory today". Re-run it in CI (see account day).

## 3. Secrets scan

```bash
.venv/bin/python -m scripts.scan_secrets --history     # exit 1 on any finding
.venv/bin/python -m pytest tests/unit/test_scan_secrets.py
```

`scripts/scan_secrets.py` is new in M52: thirteen rules (AWS key id and secret, PEM private keys,
GitHub/Slack/Google/Anthropic/OpenAI/Stripe tokens, JWTs, a URL carrying a password, a quoted
literal assigned to `password`/`secret`/`api_key`/`token`, and an unquoted `.env`/shell line such as
`DB_PASSWORD=...`, the shape a leaked `.env` file has), placeholders skipped, findings printed
redacted (first four characters and the length). The unit test plants one sample per rule and
fails if any rule stops matching, and asserts the tree is clean on every `make test` (skipped, with
the reason printed, inside the test image, which has no `.git`).

Result on 2026-09-23: **tree (~590 text files, tracked plus untracked-not-ignored) and history (every
added line on every ref) clean.** Five values were reviewed and judged fake before that; they are
listed in the code (`REVIEWED_FAKES`) by SHA-256 prefix, never by value; they are public test
fixtures, so this table names them:

| Fingerprint | What it is | Where |
|---|---|---|
| `e2a530e251d36750` | `marketing`, the local docker-compose / CI service-container Postgres password | `.github/workflows/ci.yml`, `tests/fixtures/postgres.py` |
| `148de9c5a7a44d19` | `p`, in `u:p@h` test DSNs | settings, metadata and infra-parameter tests |
| `30c952fab122c3f9` | `pw`, a test DSN password | `tests/unit/test_settings.py` |
| `545c4652e05586b5` | `hunter2-do-not-log`, the value the redaction tests assert never reaches a log | `tests/unit/test_aws_secrets.py`, `tests/unit/test_settings.py` |
| `4f21cdb9cad54751` | `a-wrong-password`, the failed-login case | `tests/integration/production/test_auth_routes.py` (Part 1) |

A new fake credential goes on its own line with the marker `secret-scan: allow`, not in this list.
The scanner is a floor, not a replacement for gitleaks or trufflehog: it has no entropy detection
and knows only the formats above. Known blind spots, from probing it: a password containing a
literal `/` inside a URL (real DSNs percent-encode it), a quoted assignment shorter than 12
characters, and any value containing `example` in any case (treated as a placeholder).

## 4. TLS everywhere

Read from `infra/` and the synthesised prod templates.

| Hop | Control | Where | Status |
|---|---|---|---|
| Browser → ALB (prod) | HTTPS listener on 443 only, ACM certificate, `ELBSecurityPolicy-TLS13-1-2-Res-2021-06` (TLS 1.2/1.3); no port-80 listener at all; `drop_invalid_header_fields` | `infra/compute.py` `_listener` | ✅ `AppContext.validate` refuses prod without `certificate_arn` |
| Browser → ALB (dev) | HTTP on 80 when no certificate is given, with a synth-time warning | same | ⚠️ **F-2** |
| ALB → Fargate task | HTTP on 8000; task security group admits the ALB's group only | `infra/network.py` | Accepted: TLS terminates at the ALB inside a private VPC. End-to-end TLS would need a certificate in the container; revisit only if a client's policy requires it |
| App → RDS | `rds.force_ssl=1` in the parameter group; the composed DSN says `sslmode=require` | `infra/database.py` | ✅ encrypted; ⚠️ **F-1** the server's certificate is not verified |
| App/jobs → S3 | Bucket policies deny `aws:SecureTransport=false` and TLS < 1.2 (`enforce_ssl`, `minimum_tls_version=1.2`) on the artefact, access-log and audit buckets; pre-signed URLs are HTTPS | `infra/storage.py`, `infra/operations.py` | ✅ |
| App → SNS | Both topics `enforce_ssl=True` | `infra/observability.py`, `infra/operations.py` | ✅ |
| App → Secrets Manager, SSM, SageMaker, ECS, KMS, CloudWatch | boto3 over HTTPS (no `use_ssl=False`, no `verify=False` anywhere in `engine/`, `api/`, `scripts/`); interface endpoints admit 443 from the VPC CIDR only | `infra/network.py` | ✅ |
| SageMaker job volumes and output | `VolumeKmsKeyId` / `KmsKeyId` set from the deployment key | `engine/aws/sagemaker_jobs.py` | ✅ (at rest, not transit) |

## 5. Least privilege: every IAM role

Six `AWS::IAM::Role` resources in the prod synth - two in compute (TaskRole, ExecutionRole), one each
in sagemaker, operations and two in network - and the same six in dev. The API's TaskRole has two
rows below because the operations stack attaches a second inline policy to it. The seventh role,
the secrets-rotation function's, is created at deploy time by AWS's Serverless Application
Repository application (`AWS::Serverless::Application` in the database stack) and is not in these
templates. "Justified" means every action is used by a code path that exists today and every
resource is the narrowest the service allows. No policy in any template has a `*` action.

| Role (stack) | Assumed by | Actions | Resources | Justified? |
|---|---|---|---|---|
| **TaskRole** (compute) - the API | `ecs-tasks` | S3 object R/W/delete + multipart; `ListBucket`; KMS encrypt/decrypt; SSM `GetParameter*`; Secrets `GetSecretValue`; SageMaker create/describe/stop training & processing jobs + `AddTags`; `iam:PassRole`; logs; `cloudwatch:PutMetricData`; **explicit Deny** `bedrock:InvokeModel*` | objects under `uploads/`, `runs/`, `models/`, `_bootstrap/` only; the artefact key; `/marketing-ai/<env>/*`; the app secret only; `training-job/marketing-ai-*`; the SageMaker execution role only, `PassedToService=sagemaker`; the API log group; namespace `MarketingAI` | Yes for this block |
| **TaskRole** + `ApiOperationsPolicy` (operations) | same role | `s3:PutObject`, `PutObjectRetention` on audit exports; Scheduler create/update/delete/get; `iam:PassRole` scheduler role; `sns:Publish`; **`s3:PutLifecycleConfiguration`**, `GetLifecycleConfiguration`, `ListBucketVersions`; **`s3:DeleteObjectVersion`** | `audit/*` of the audit bucket; `schedule/marketing-ai-<env>/*`; the scheduler role (`PassedToService=scheduler`); the alert topic; the artefact bucket; `uploads/*`, `runs/*` | Functionally yes (M47–M49); ⚠️ **F-3**, **F-4** |
| **ExecutionRole** (compute) - the ECS agent | `ecs-tasks` | `ecr:GetAuthorizationToken`; image pull; **`kms:Decrypt`, `Encrypt`, `GenerateDataKey`**, `DescribeKey`; log stream + events | `*` (no resource-level permission exists); the ECR repository; the key; the API log group | Mostly; ⚠️ **F-5** |
| **ExecutionRole** (sagemaker) - training/processing jobs | `sagemaker` | S3 object R/W/delete; `ListBucket`; KMS; SSM; Secrets; ECR pull; logs; metrics; EC2 ENI create/delete/describe; Deny Bedrock | same prefixes as the task role; `/aws/sagemaker/*` log groups and the product's job log group; ENI actions on `*` | Yes. ENI on `*` is what SageMaker's VPC mode documents; a `ec2:Vpc`/`ec2:Subnet` condition on `CreateNetworkInterface` would narrow it (low priority). `s3:DeleteObject` on `models/` is broader than a job needs - worth checking on account day which job path deletes |
| **SchedulerRole** (operations) | `scheduler` | `ecs:RunTask`, `ecs:TagResource`, `iam:PassRole` | the job task-definition family only, on this cluster only (`ecs:cluster` condition); tagging only as part of `RunTask`; the task and execution roles (`PassedToService=ecs-tasks`) | Yes |
| **VpcFlowLogIAMRole** (network) | `vpc-flow-logs` | `logs:CreateLogStream`, `PutLogEvents`, `DescribeLogStreams` | the flow-log group | Yes |
| **CustomVpcRestrictDefaultSG…Role** (network) - CDK's custom resource | `lambda` | managed `AWSLambdaBasicExecutionRole`; `ec2:Authorize/RevokeSecurityGroup{Ingress,Egress}` | the VPC's *default* security group only | Yes (CDK-authored; it strips the default group's rules) |
| **Secrets rotation function role** (database) | `lambda` (via the AWS Serverless Application Repository) | not in this repository's templates: `add_rotation_single_user` deploys AWS's `SecretsManagerRDSPostgreSQLRotationSingleUser` application | the credential secret and the instance | **Not reviewable offline** - read its policy on account day |
| (no IAM users, no access keys, no `AdministratorAccess`, no role with a `*` action anywhere) | | | | ✅ |

---

## Findings

Severity is this review's judgement. None is fixed here: each is in `infra/**` (Phase 4a) or needs a
decision.

**F-6 (high, privacy) - RDS logs every write's bind parameters.** `infra/database.py` sets
`log_statement=mod` and exports `postgresql` logs to CloudWatch. psycopg 3 (through SQLAlchemy)
binds parameters server-side, and PostgreSQL logs them with the statement (`DETAIL: parameters:
$1 = '…'`, untruncated by default). So every INSERT/UPDATE writes its values to CloudWatch Logs:
consent-ledger principal ids (M48), user records including password hashes (M46), audit rows, run
metadata. That contradicts plan §13.7 ("never log data values"), and an erasure request (M48)
cannot remove those lines - they live for `log_retention_days` (365 in prod). *Fix:* add
`"log_parameter_max_length": "0"` and `"log_parameter_max_length_on_error": "0"` to the parameter
group (PostgreSQL 13+; the statement text is still logged, the values are not), or drop to
`log_statement=ddl` and use `pgaudit` for write auditing. Add a CDK assertion for it next to the
`rds.force_ssl` one.

**F-3 (medium) - the API task role can defeat S3 versioning for row-level data.** The operations
policy gives the *API's* role `s3:PutLifecycleConfiguration` on the whole artefact bucket and
`s3:DeleteObjectVersion` on `uploads/` and `runs/` (for M48 retention and erasure). A compromised API
task can therefore delete every version of every upload, or install an expiry rule for the whole
bucket - versioning is not a backup against the one identity most exposed to the internet. *Fix
options:* run retention/erasure as the scheduled job task with its **own** role (see F-4) and take
both grants off the API role; or add a bucket-policy Deny of `PutLifecycleConfiguration` for the task
role outside a change window; and, for real backup independence, S3 replication or AWS Backup for S3
into a separate account (a P1/P4 decision).

**F-4 (medium) - scheduled jobs run as the API task role.** `JobTaskDefinition` (operations) uses the
compute stack's `TaskRole`, so a scoring or retention job can create schedules, write audit exports
and pass the SageMaker role, and the API can do everything a retention job can. A `JobTaskRole` with
only the job's grants would separate them; it is also the natural home for F-3's grants.

**F-1 (medium) - `sslmode=require` does not verify the database's certificate.** It encrypts but
would accept any certificate, so a host that could intercept VPC traffic could present its own.
*Fix:* `sslmode=verify-full` with `sslrootcert` pointing at the RDS global CA bundle baked into the
image (`https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`, pinned by hash). Needs
`infra/database.py` (the composed URL) and the Dockerfile.

**F-2 (medium) - dev is HTTP-only unless a certificate is given.** Plan M50 runs the Phase 1
acceptance test on dev with real-looking data and the first Admin signs in over it (M46 bearer
sessions). Give dev a certificate on account day (ACM is free) and treat HTTP-only dev as for
synthetic data only.

**F-5 (low) - the ECS execution role holds `kms:Encrypt`/`GenerateDataKey`.** Pulling a
KMS-encrypted image and writing logs needs `kms:Decrypt` (and `DescribeKey`). Drop the other two in
`infra/policies.py`'s execution-role statement.

**Also noted (informational):** no AWS WAF in front of the ALB (a cost/benefit call for P3; the ALB
drops invalid headers and every route requires a session when `auth_mode=local`); the first-run
admin password path (`scripts/create_user.py`) is handled outside `run_in_deployment` by design.

---

## Account day: the half that needs the AWS account (P1)

| Check | How | Record in |
|---|---|---|
| IAM Access Analyzer policy validation and **unused access** after a week of real traffic | Console or `aws accessanalyzer validate-policy`; `create-analyzer --type ACCOUNT_UNUSED_ACCESS` | this file, table 5 |
| The rotation function's role (SAR) | `aws iam get-role-policy` on the role the nested stack created | table 5 |
| TLS as deployed: protocol/cipher scan of the ALB | `testssl.sh https://<domain>` from outside | table 4 |
| `rds.force_ssl` really refuses plaintext | from inside the VPC: `scripts.run_in_deployment -- psql "…sslmode=disable"` must fail | table 4 |
| F-6 before and after: search the `postgresql` log group for `parameters:` | CloudWatch Logs Insights | Findings |
| ECR image scan findings of the first pushed digest | `aws ecr describe-image-scan-findings` | section 2 |
| Security Hub (AWS Foundational Security Best Practices) and GuardDuty on, CloudTrail multi-region trail present | console | new row in Summary |
| gitleaks + pip-audit in CI on every PR | `.github/workflows/ci.yml` (Phase 4a owns it) | Summary |

Recorded by: _____ on _____ (dev account) · _____ on _____ (prod account)
