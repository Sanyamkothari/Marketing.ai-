# M50 checklist: the day the dev account exists

Plan M50 is "first deployment and real measurements", and every part of it needs an AWS account that
does not exist yet. This is the ordered list for the day it does: what to have in hand, what to run,
in what order, what a pass looks like, and **where each result is written down**. It is the
companion of `docs/AWS_DEPLOYMENT.md`, which explains every step; this file only sequences them.
Section numbers such as "guide §4.3" refer to that document.

Everything that could be done without an account was done on 2026-09-23 (guide §12): the guide was
walked end to end, `make infra-synth ENV=dev` synthesised all eight stacks, and the gaps found were
fixed. Nothing below has been run against AWS.

**How to use it.** Tick a box only when the step passed. Every "Record" line names the table at the
foot of this file (§9) or the file where the result belongs; fill it in as you go, with the date and
the command, because a number without its source is not a measurement (plan section 13.3). Stop at
the first step that fails and fix it before moving on: later steps assume every earlier one.

Throughout: `ENV=dev`, region `ap-south-1`, and `<account>` is the dev account id. Never commit an
account id, an ARN, a bucket name or an email address (PARALLEL_WORK_PROTOCOL.md §5 rule 5):
they go in `infra/cdk.context.json` and your shell, both outside git.

---

## 1. Prerequisites (plan §0), before anyone opens a terminal

| Done | # | What | From whom | Record |
|---|---|---|---|---|
| [ ] | P1 | A dev AWS account, and a person with administrator access to it for the first deployment | Minfy | §9.1: account alias (not the id), who holds admin, date |
| [ ] | P2 | Bedrock model access in `ap-south-1` for the generation and embedding models chosen | Minfy, via the Bedrock console | §9.1: model ids, date access was granted |
| [ ] | P3 | A monthly budget figure for the dev account, and the alert email address | Minfy | §9.1: the figure and who set it; the address goes only in `infra/cdk.context.json` |
| [ ] | P4 | Single-tenant (the current build) or multi-tenant | Minfy | §9.1. M50 proceeds single-tenant either way; M51 acts on the answer |
| [ ] | P5 | Identity provider: Cognito or the client's SSO | Minfy | §9.1. M50 uses `auth_mode=local` until this exists (guide §9.2) |
| [ ] | P6 | Who approves champions (role name) | Minfy | §9.1; used in §5 |
| [ ] | — | Legal's retention period for prod audit exports | Minfy legal/compliance | §9.1. Not needed for dev (1 day); needed before any prod deployment (guide §11.4) |

---

## 2. The workstation

- [ ] **Toolchain** (guide §2): Python 3.11, Node 22, Docker with buildx, GNU make, AWS CLI v2.
- [ ] **Both virtualenvs.**

  ```bash
  make setup
  make infra-setup
  .venv-infra/bin/pip show cdk-nag | grep '^Version: 2.38.2$'
  ```

- [ ] **Credentials point at the dev account** and nowhere else.

  ```bash
  aws sts get-caller-identity --query '[Account,Arn]' --output text
  aws configure get region    # ap-south-1, or export AWS_REGION=ap-south-1
  ```

  Record: §9.2, the principal used (role name, not the ARN).

- [ ] **Offline checks pass on this machine** before anything is created.

  ```bash
  make infra-test
  make infra-synth ENV=dev
  make infra-nag ENV=dev
  .venv/bin/python -m scripts.aws_bootstrap --env dev --dry-run
  ```

  The synth warnings listed in guide §4.1 are expected; anything else is a finding. Record: §9.2.

- [ ] **CDK bootstrap**, once per account and region (guide §2):

  ```bash
  cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 \
    bootstrap aws://<account>/ap-south-1
  ```

---

## 3. Deploy

- [ ] **Persist this deployment's context** (guide §4.2), so no later deploy drops it. For the paid
  tests, cap concurrency at one job:

  ```bash
  cat > infra/cdk.context.json <<'EOF'
  {
    "marketing-ai:alert_email": "<address from P3>",
    "marketing-ai:monthly_budget_usd": "<figure from P3>",
    "marketing-ai:max_concurrent_jobs": "1"
  }
  EOF
  make infra-synth ENV=dev    # the "No alert_email" warnings are gone
  ```

- [ ] **Storage stack first**, so the image has a repository to go to (guide §2.2):

  ```bash
  cd infra && PATH="$PWD/../.venv-infra/bin:$PATH" npx --yes aws-cdk@2.1142.0 \
    deploy marketing-ai-dev-storage -c env_name=dev
  ```

  Record: §9.2, the time it took.

- [ ] **Log Docker into ECR** with the `RepositoryUri` output split at the `/` (guide §2.2, §4.2):

  ```bash
  export ECR_REGISTRY=<account>.dkr.ecr.ap-south-1.amazonaws.com
  export ECR_REPOSITORY=marketing-ai
  aws ecr get-login-password --region ap-south-1 \
    | docker login --username AWS --password-stdin "$ECR_REGISTRY"
  ```

- [ ] **Build, push and deploy, from this terminal** (not CI: the first deployment widens IAM and
  needs the approval prompt, guide §2.1). Read every IAM change the prompt shows before answering.

  ```bash
  make aws-deploy ENV=dev
  ```

  The first build is the first time the Dockerfile's apt layer runs anywhere (guide §10.3). Record:
  §9.2, build time, the digest in `.image-digest`, total deploy time, and the image size as ECR
  stored it:

  ```bash
  aws ecr describe-images --repository-name marketing-ai --region ap-south-1 \
    --image-ids imageDigest="$(cut -d@ -f2 .image-digest)" --query 'imageDetails[0].imageSizeInBytes'
  ```

- [ ] **Restart the service once** so it reads the Phase 4b parameters (guide §4.2):

  ```bash
  aws ecs update-service --cluster marketing-ai-dev --service marketing-ai-dev-api \
    --force-new-deployment --region ap-south-1
  aws ecs wait services-stable --cluster marketing-ai-dev --services marketing-ai-dev-api --region ap-south-1
  ```

  Record: §9.2, the time from the restart to `services-stable` — the first real cold start, which the
  120-second health-check start period in `infra/compute.py` was chosen without.

- [ ] **Confirm both email subscriptions** (guide §11.6): click both links, then check neither
  reads `PendingConfirmation`.
- [ ] **Activate the cost-allocation tags** `product`, `env` and `client` in Billing and Cost
  Management (guide §6.1). They take up to 24 hours; the idle-cost day (§8) cannot start until
  they are active. Record: §9.2, the time they were activated.

---

## 4. Bootstrap and smoke test

- [ ] **Schema and permissions, from inside the deployment** (guide §4.3):

  ```bash
  .venv/bin/python -m scripts.run_in_deployment --env dev -- python -m scripts.aws_bootstrap
  ```

  Pass: `5/5 checks passed; this deployment is ready`, exit code 0. Record: §9.2, the report's last
  line and any `fix:` lines you had to act on.

- [ ] **Smoke test**:

  ```bash
  .venv/bin/python -m scripts.smoke_deployment --env dev
  ```

  Pass: `healthz` ok and `sign-in` ok with `401 without a token: auth_mode=local is live`. Record: §9.2.

- [ ] **CI can deploy a no-op**: dispatch `deploy-dev` from GitHub with the environment's
  `DEPLOY_ROLE_ARN` and `ECR_REPOSITORY` variables set and the extra permissions of guide §4.4 on
  that role. Pass: every step green, including the bootstrap and the smoke test. Record: §9.2, the
  run URL.

---

## 5. Sign-in and roles (guide §11.2)

- [ ] **The first Admin**, with a one-time password (dev only; guide §11.2 says why):

  ```bash
  .venv/bin/python -m scripts.run_in_deployment --env dev -- \
    sh -c 'printf "%s\n" "<one-time password>" | python -m scripts.create_user --username <admin> --role admin --password-stdin'
  ```

- [ ] **Sign in and change that password at once** (UI, or `POST /users/{user_id}/password`).
- [ ] **Create an Analyst and an Approver** as the Admin (the P6 role name decides who is Approver).
- [ ] **Separation of duties holds**: signed in as the Analyst, approving a champion is refused.
  Fetch the Analyst's token with the sign-in snippet of guide §5 and keep it as `ANALYST_TOKEN`; a
  model id comes from the run of §6.1, so this step can be done right after it.

  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $ANALYST_TOKEN" \
    -H 'Content-Type: application/json' -d '{"approved_by": "analyst"}' \
    "$SERVICE_URL/models/<model id>/approve"
  ```

  Pass: `403`, and the Audit screen shows the attempt with outcome `denied`. Record: §9.3.

---

## 6. Acceptance on AWS

### 6.1 Phase 1's acceptance test, on AWS (plan M50)

Signed in as the Analyst, follow guide §5 steps 1–7 with the public Telco Churn file (7,043 rows) —
never a customer's file. `python library/telco-customer-churn/fetch.py` downloads it into
`library/telco-customer-churn/data/`; `library/telco-customer-churn/README.md` names its source and
licence.

- [ ] **Training ran as a SageMaker job**, not in the API task (guide §5.1):

  ```bash
  aws sagemaker list-training-jobs --name-contains marketing-ai --max-results 5 \
    --sort-by CreationTime --sort-order Descending \
    --query 'TrainingJobSummaries[].[TrainingJobName,TrainingJobStatus]' --output table --region ap-south-1
  curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/runs/$RUN_ID/artefacts/run_manifest.json" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["compute"]["backend"])'
  ```

  Pass: the job is `Completed` and the backend reads `sagemaker-training`.
- [ ] **Artefacts are in S3**:

  ```bash
  aws s3 ls "s3://<BucketName>/runs/$RUN_ID/" --recursive --region ap-south-1 | head -20
  ```

- [ ] **The run is visible in the UI**, leaderboard, evaluation, decile lift and baseline comparison
  on screen, every figure from this run.
- [ ] **Only the Approver can approve the champion**; the Approver does. Scoring a new file with
  the champion produces `scores.csv`.

Record: §9.3, the run id, the job name, pass/fail per line, date.

### 6.2 Phase 4b's acceptance test (plan §6)

- [ ] **A monthly scoring schedule** is created by the Analyst and appears in
  `aws scheduler list-schedules --group-name marketing-ai-dev --region ap-south-1` (guide §11.5).
- [ ] **It runs on its own.** A monthly schedule cannot be waited for, so also create a schedule
  whose first firing is within the next hour, then watch it fire:

  ```bash
  aws logs tail /marketing-ai/dev/api --log-stream-name-prefix job/ --since 2h --follow --region ap-south-1
  ```

  Pass: the firing's run appears in the UI, started by the scheduler principal. If a firing fails
  on its own, its alert email must arrive; if none does, publish the test message of guide §11.6 and
  record the failed-firing alert as unproven rather than passed.
- [ ] **An erasure request** for one synthetic customer id removes it from every store and appears
  in the audit log (guide §11.8). Check with an access request for the same id afterwards (empty),
  never by printing artefacts to a terminal.
- [ ] **An audit export is locked**: `POST /audit/exports` as the Admin, then
  `aws s3api get-object-retention` on the new key shows `COMPLIANCE` (guide §11.4).
- [ ] **The retention job's dry run and apply agree** (guide §11.7):

  ```bash
  .venv/bin/python -m scripts.run_in_deployment --env dev -- python -m scripts.run_retention --json
  .venv/bin/python -m scripts.run_in_deployment --env dev -- python -m scripts.run_retention --apply --json
  ```

  Then run the noncurrent-version check of guide §11.7. Pass: the apply deleted exactly the keys
  the dry run listed. Noncurrent versions left under `uploads/` or `runs/` are not a pass of the
  DPDP control: record their count in §9.3 as a finding, with the erasure check above.

- [ ] **An alarm fires on a forced failure** (plan §4, "alarm fires on a forced failure"): scale the
  service to zero for five minutes and confirm the `no-healthy-targets` alarm email arrives, then
  scale back.

  ```bash
  aws ecs update-service --cluster marketing-ai-dev --service marketing-ai-dev-api --desired-count 0 --region ap-south-1
  # wait for the email, then:
  aws ecs update-service --cluster marketing-ai-dev --service marketing-ai-dev-api --desired-count 1 --region ap-south-1
  ```

- [ ] **Nothing in the process required editing code.** If anything did, that is an M50 finding:
  fix the guide or the script, and add it to guide §12.

Record: §9.3.

---

## 7. The paid tests, under a cap

A budget is an alert, not a brake: AWS Budgets emails at its thresholds and stops nothing. The caps
that actually bound spend are the ones below, set **before** the paid runs.

- [ ] **The budget exists** and reports the P3 figure:

  ```bash
  aws ssm get-parameter --name /marketing-ai/cost/dev/monthly-budget-usd --region ap-south-1 \
    --query Parameter.Value --output text
  ```

- [ ] **Every SageMaker job has a ceiling**: no stack writes this setting, so set it by hand
  (guide §11.1) and restart the service. One hour is a chosen ceiling, not a measurement:

  ```bash
  aws ssm put-parameter --name /marketing-ai/dev/sagemaker_max_runtime_seconds --type String \
    --value 3600 --overwrite --region ap-south-1
  ```

- [ ] **One job at a time**: `max_concurrent_jobs` is `1` in `infra/cdk.context.json` (step 3).
- [ ] **The Bedrock smoke test** (guide §6.5), with your own credentials:

  ```bash
  export BEDROCK_SMOKE_REGION=ap-south-1
  export BEDROCK_SMOKE_GENERATION_MODEL_ID=<generation model from P2>
  export BEDROCK_SMOKE_EMBEDDING_MODEL_ID=<embedding model from P2>
  .venv/bin/python -m pytest -m bedrock tests/integration/test_bedrock_smoke.py
  ```

  Pass: seven tests pass, none skipped. Record: §9.4, the token counts it reports.
- [ ] **The real LLM path in the product**: redeploy with `"marketing-ai:bedrock_enabled": "true"`
  and `"marketing-ai:bedrock_model_ids": "<ids>"` in `infra/cdk.context.json`, restart, and ask the
  assistant 20 questions from the Telco documents. Record: §9.4, the date and the count, for step 8.

---

## 8. Cost capture (guide §6)

Every figure goes first into §9.5 below, with the date and the command beside it. The guide's two
cost tables are guarded by `tests/unit/test_docs_honesty.py`, which fails if any cell stops reading
`NOT YET MEASURED`, because a half-filled table reads as a measured one (DEC-399). So the move into
the guide is **one change** that fills every cell of a table, cites the date and the source on each
line, and replaces `test_the_cost_tables_are_entirely_unmeasured` with a check that every cell is
either the marker or a figure with a date — recorded as a decision in `docs/DECISIONS.md`.

- [ ] **Idle cost**: once the tags are active, leave the deployment with no run for one full UTC
  day, and read Cost Explorer the day after (it lags by up to 24 hours) — the command in guide §6.1,
  with that day's dates. Record: §9.5, by service.
- [ ] **Per training run, per strategy**: one Telco Churn training run each with `fast`, `balanced`
  and `exhaustive`, on separate days so Cost Explorer can tell them apart. For each: the manifest's
  `compute` and `cost_estimate` (guide §6.2) and Cost Explorer's SageMaker line for that day.
- [ ] **Per 100,000 scored rows**: score one file, take its processing job's cost from Cost
  Explorer, and divide by the row count times 100,000. Record the row count used.
- [ ] **Per 1,000 assistant questions**: Cost Explorer's Amazon Bedrock line for the day of step 7's
  questions, divided by the number asked, times 1,000. Record the model id and total tokens too.
- [ ] **Answer the "cost tiers" question** in one paragraph, with the figures above as its only
  numbers. Record: `docs/DECISIONS.md`, Phase 4b range.

---

## 9. Results

Every cell starts as "not yet run". Replace it with the result, the date and the command's source;
leave it as it is if the step was not run.

### 9.1 Prerequisites

| # | Answer | Date | Decided by |
|---|---|---|---|
| P1 | not yet run | | |
| P2 | not yet run | | |
| P3 | not yet run | | |
| P4 | not yet run | | |
| P5 | not yet run | | |
| P6 | not yet run | | |
| Prod audit retention | not yet run | | |

### 9.2 Deployment

| Step | Result | Date | Source |
|---|---|---|---|
| Deploying principal | not yet run | | `aws sts get-caller-identity` |
| Offline checks on the day | not yet run | | `make infra-test`, `make infra-synth`, `make infra-nag` |
| Storage stack deploy time | not yet run | | wall clock |
| First image build time and size | not yet run | | `make aws-deploy`, `aws ecr describe-images` |
| Image digest | not yet run | | `.image-digest` |
| Full deploy time | not yet run | | wall clock |
| Cold start to `services-stable` | not yet run | | `aws ecs wait services-stable` |
| Cost-allocation tags activated | not yet run | | Billing console |
| Bootstrap report | not yet run | | `scripts.run_in_deployment -- python -m scripts.aws_bootstrap` |
| Smoke test | not yet run | | `scripts.smoke_deployment` |
| CI deploy of a no-op | not yet run | | the workflow run |

### 9.3 Acceptance

| Check | Result | Date | Evidence |
|---|---|---|---|
| Analyst cannot approve (403, audited as denied) | not yet run | | |
| Training ran as a SageMaker job | not yet run | | job name |
| Artefacts in S3 | not yet run | | run id |
| Run visible in the UI | not yet run | | |
| Only the Approver approved; scores produced | not yet run | | |
| Monthly schedule created | not yet run | | schedule name |
| A schedule fired on its own | not yet run | | log stream |
| Failed schedule alerted by email | not yet run | | |
| Erasure removed the id everywhere; audited | not yet run | | audit event id |
| Audit export locked in COMPLIANCE mode | not yet run | | key |
| Retention dry run equals apply | not yet run | | |
| Noncurrent versions left under `uploads/` and `runs/` after apply and erasure | not yet run | | `aws s3api list-object-versions`, guide §11.7 |
| Alarm fired on a forced failure | not yet run | | |
| No code edited during the process | not yet run | | |

### 9.4 Paid tests

| Test | Result | Date | Tokens / notes |
|---|---|---|---|
| Bedrock smoke test | not yet run | | |
| Assistant questions asked in the product | not yet run | | |

### 9.5 Costs, before they move to the guide

| Figure | Value | Date measured | Source command |
|---|---|---|---|
| Idle, per day, by service | not yet run | | Cost Explorer, guide §6.1 |
| Training run, `fast` | not yet run | | manifest + Cost Explorer |
| Training run, `balanced` | not yet run | | manifest + Cost Explorer |
| Training run, `exhaustive` | not yet run | | manifest + Cost Explorer |
| Per 100,000 scored rows | not yet run | | Cost Explorer, row count |
| Per 1,000 assistant questions | not yet run | | Cost Explorer, question count |
