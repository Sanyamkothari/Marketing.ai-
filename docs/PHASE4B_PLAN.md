# Marketing AI — Plan C: Phase 4b First Deployment, Access Control, Privacy and Scheduling

**Companion to:** `MARKETING_AI_PHASE4A_PLAN.md`, `plan.md`, `PARALLEL_WORK_PROTOCOL.md`
**Owner:** Minfy — AI/ML team
**Branch:** `phase-4b-production` — Part 1 (local-first work) can start now in parallel with Plan A; Part 2 needs an AWS account
**Decision range:** DEC-700…799

> **What this phase does.** Phase 4a built the AWS version of the product and tested it offline. Phase 4b takes it to a real, safe, multi-user deployment: first deployment and cost measurement, sign-in and roles, separation between clients, an audit trail, India DPDP controls (consent, retention, deletion), and scheduled scoring, monitoring and retraining. It also turns on the monitoring settings that are currently inactive.
>
> **Legal note.** The DPDP items below are engineering controls that support compliance. They are not legal advice; Minfy's legal or compliance team must review the final design. Known dates: the Digital Personal Data Protection Rules were notified on 13 Nov 2025, with compliance required by 13 May 2027 and penalties up to ₹250 crore.

---

## 0. Prerequisites (owner actions, before Part 2)

| # | Needed from Minfy | Why |
|---|---|---|
| P1 | A dev AWS account (and later a separate prod account), CDK-bootstrapped in `ap-south-1` | Nothing has been deployed yet |
| P2 | Bedrock model access enabled in `ap-south-1` for the chosen models | Phase 3a's real LLM path has never run |
| P3 | A monthly budget figure and an alert email | AWS Budgets and alarms |
| P4 | **Deployment-model decision**: single-tenant in each client's account (current default) or a Minfy-hosted multi-tenant SaaS | Changes how isolation is built (section 3) |
| P5 | Identity provider: Amazon Cognito, or the client's SSO (SAML/OIDC) | Sign-in design |
| P6 | Who approves champions per client (role name) | RBAC |

## Part 1 — can start now (local backends, no AWS needed)

### M46 — Roles and access control

- Roles: **Viewer** (see results), **Analyst** (upload, build, train, score), **Approver** (approve champions, approve campaign copy), **Admin** (users, settings, retention, autonomy level for Phase 5).
- Every API route declares its required role; a test fails if a route has none.
- Local mode: a simple built-in user store for development; production mode delegates to the identity provider (M50).
- The UI hides actions the user cannot take and explains why ("Only an Approver can approve a champion").

### M47 — Audit log

- Append-only `audit_events` table: who, when, action, object, before/after hashes, request ID. Written for uploads, builds, runs, approvals, copy approvals, setting changes, deletions, logins, role changes.
- No customer data values in the audit log (reuse the logging-audit test approach from Phase 1).
- Export to S3 with Object Lock (compliance mode) in AWS; local mode writes JSON lines.
- Audit viewer page for Admins with filters and CSV export.

### M48 — DPDP controls (engineering side)

1. **Consent and purpose:** a consent ledger per client: data principal ID, purpose (e.g. "marketing communication"), status, source, timestamp. The Phase 1 consent/opt-out column becomes a lookup against this ledger when present. Scoring and actions for a purpose exclude principals without valid consent for it; excluded counts are reported.
2. **Retention:** the currently inactive `governance.retention_days` becomes live. A retention job deletes uploads, datasets and row-level artefacts older than the setting (models and aggregate reports kept); S3 lifecycle rules per client prefix in AWS; a dry-run mode shows what would be deleted.
3. **Erasure requests:** `POST /privacy/erasure` with a data principal ID: finds every place the ID appears (uploads, datasets, scores, row explanations, copy messages, LLM cache), deletes or tombstones it, records the request and outcome in the audit log, and flags models trained on data that included the person (retraining at the next scheduled cycle, not immediately; document this).
4. **Access requests:** export everything held about a data principal as a file for the client to share.
5. **Breach runbook:** a section in `docs/RUNBOOK.md` covering detection signals, who to notify, and the evidence the audit log provides.

### M49 — Scheduling, monitoring and outcomes (local scheduler first)

- A scheduler abstraction: local (simple in-process scheduler for dev) and AWS (EventBridge Scheduler calling the API).
- Schedules per client × use case: scoring (e.g. monthly on new tables via the saved onboarding spec), drift checks, retraining. The inactive `monitoring.retraining` and `monitoring.performance_alert_drop_pct` settings become live.
- **Outcome ingestion:** once outcome windows mature, join actual outcomes to past scores to compute real-world performance and, for runs with control groups, feed Plan B's incrementality report. This is also what Phase 5's Monitor will read.
- Alerts: drift above threshold, performance drop beyond the setting, failed scheduled job, via email (SNS in AWS).
- Retraining produces a challenger; the champion rule and human approval still apply.

## Part 2 — needs the AWS account

### M50 — First deployment and real measurements

- Follow `docs/AWS_DEPLOYMENT.md` exactly on the dev account; fix every gap found in the guide as part of this milestone.
- Wire the identity provider (P5) and roles.
- Run the Phase 1 acceptance test on AWS: training as a SageMaker job, artefacts in S3, run visible in the UI.
- Run the paid tests once (real Bedrock, real AWS) with a small budget cap.
- **Measure costs** and replace every "NOT YET MEASURED" cell: idle monthly cost, cost per Fast/Balanced/Exhaustive training run, per 100k scored rows, per 1,000 assistant questions. This answers the open "cost tiers" question.

### M51 — Client isolation (depends on P4)

- **Single-tenant (default):** one deployment per client account; isolation is the account boundary. Verify no cross-client paths exist, and document the deployment runbook per client.
- **Multi-tenant SaaS (if chosen):** tenant ID on every row with Postgres row-level security; S3 prefixes per tenant with IAM session policies based on tenant tags; per-tenant KMS keys; per-tenant budgets and cost reports; tests that prove one tenant cannot read another's data through any API route.

### M52 — Hardening

- Backup and restore drill (RDS snapshots, S3 versioning) with a recorded recovery time.
- Load test: concurrent users, concurrent jobs, queueing behaviour.
- Security review checklist: cdk-nag clean, dependency scan, secrets scan, TLS everywhere, least-privilege review of every role.
- Runbook updated with everything learnt in M50–M52.

## 4. Testing

- Every route has a role test (allowed and denied).
- Audit: every mutating route writes exactly one audit event; no data values present.
- DPDP: consent exclusion counts correct; retention dry-run equals real run; erasure removes the ID from every store (search test across all artefacts); access export is complete.
- Scheduler: schedules fire; missed runs are reported; retraining yields a challenger, never an automatic champion.
- AWS (opt-in, paid): deployment smoke, acceptance test on AWS, cost capture, alarm fires on a forced failure.

## 5. Milestones summary

| # | Milestone | Needs AWS? |
|---|---|---|
| M46 | Roles and access control | No |
| M47 | Audit log | No (S3 export tested with moto) |
| M48 | DPDP controls | No (S3 lifecycle tested offline) |
| M49 | Scheduling, monitoring, outcomes | No (EventBridge tested offline) |
| M50 | First deployment and cost measurement | **Yes** |
| M51 | Client isolation | **Yes**, and decision P4 |
| M52 | Hardening | **Yes** |

## 6. Acceptance test

On the dev AWS account: an Admin signs in, creates an Analyst and an Approver; the Analyst uploads data and trains; only the Approver can approve the champion; a monthly scoring schedule runs on its own; an erasure request removes a customer from every store and appears in the audit log; the deployment guide contains real, dated cost figures; and nothing in the process required editing code.
