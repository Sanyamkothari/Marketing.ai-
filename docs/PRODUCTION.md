# Production controls: operator guide (Phase 4b Part 1)

Sign-in and roles, the audit trail, DPDP controls, and schedules with alerts and outcomes
(`docs/PHASE4B_PLAN.md`, M46–M49). Everything here runs on a laptop with local backends; the AWS
backends (S3 Object Lock exports, EventBridge Scheduler, SNS) are built and tested offline with moto
and have not yet run against a real account (that is M50). Decisions are cited as DEC-7xx; the
entries are in `docs/DECISIONS.md` under "Phase 4b".

> **Legal note.** The DPDP controls below are engineering controls that support compliance. They are
> not legal advice; Minfy's legal or compliance team must review the final design. The Digital
> Personal Data Protection Rules were notified on 13 Nov 2025, with compliance required by
> 13 May 2027.

With every Phase 4b setting at its default, the product behaves exactly as it did before Phase 4b:
nobody signs in, nothing fires on its own, and an alert is a log line (DEC-701).

---

## 1. Turn on sign-in and create the first Admin

```bash
export MARKETING_AI_AUTH_MODE=local
.venv/bin/python -m scripts.create_user --username admin --role admin     # prompts twice for the password
make run                                                                  # then open http://localhost:8000/ui/
```

- The password is never a command-line argument (DEC-722): the script prompts twice, or reads the
  first line of stdin with `--password-stdin` for automation. At least 12 characters.
- `--role` repeats: `--role analyst --role approver`. It defaults to `admin`.
- `--data-dir` writes to `<dir>/platform.db` instead of the directory the settings name.
- The creation is audited as `system:bootstrap`, so even the first Admin has an entry.
- The same script is the recovery path if every Admin is lost: the API refuses to disable or
  demote the last enabled Admin (409 `LAST_ADMIN`, DEC-712), but it cannot stop a database edit.

Once signed in, an Admin adds people on **Admin → Users** (`#/admin/users`). Sessions last
`MARKETING_AI_AUTH_SESSION_TTL_SECONDS` (default 8 hours). Disabling a user, changing their roles or
changing their password signs them out everywhere at once (DEC-711).

**On a prod deployment, sign-in off fails closed.** With `MARKETING_AI_ENV=prod` and
`MARKETING_AI_AUTH_MODE=off`, every route except `/healthz` answers 503 `AUTH_NOT_CONFIGURED`, and
startup logs an ERROR; the service stays up and says why (DEC-702). Single sign-on (Cognito or the
client's SAML/OIDC) is M50 and waits for decision P5.

### 1.1 Sign-in rate limiting

`POST /auth/login` is rate limited (Plan D M54, DEC-861, DEC-867). Failed sign-ins are counted in a
sliding window, separately **per account** (the username as typed, case-folded, so an unknown
username locks exactly like a real one and a lock-out confirms nothing) and **per client address**.
When either reaches its limit it is locked: every sign-in for that username, or from that address,
is refused with **429 `LOGIN_LOCKED`** and a `Retry-After` header, *without the password being
checked*, until the lock-out ends. The failure that starts a lock-out and every refused attempt are
in the audit trail (`auth.login`, `reason_code: LOGIN_LOCKED`, `lockout: account|address`); the
username and the address are not.

| Setting | Variable | Default | What it does |
|---|---|---|---|
| `login_max_failures_per_account` | `MARKETING_AI_LOGIN_MAX_FAILURES_PER_ACCOUNT` | `5` | failures for one username, within the window, that lock it |
| `login_max_failures_per_address` | `MARKETING_AI_LOGIN_MAX_FAILURES_PER_ADDRESS` | `20` | failures from one client address, within the window, that lock it |
| `login_failure_window_seconds` | `MARKETING_AI_LOGIN_FAILURE_WINDOW_SECONDS` | `900` | how far back failures are counted |
| `login_lockout_seconds` | `MARKETING_AI_LOGIN_LOCKOUT_SECONDS` | `900` | how long a locked username or address is refused |
| `trusted_proxy_hops` | `MARKETING_AI_TRUSTED_PROXY_HOPS` | `0` | proxies in front of the API whose `X-Forwarded-For` entries are trusted |

- **Behind a load balancer, set `trusted_proxy_hops` to the number of proxies.** With `0` the
  address counted is the TCP peer. Behind the AWS deployment's ALB that peer is the ALB's own
  private address for *every* request, so twenty wrong passwords from anyone would lock sign-in for
  everyone. The CDK deployment therefore publishes `trusted_proxy_hops=1` (`infra/compute.py`): the
  ALB is the only way in (the tasks' security group admits the ALB's group only), and the ALB
  appends the real client's address as the last `X-Forwarded-For` entry. With `N` hops the API uses
  the N-th entry from the right, reading every `X-Forwarded-For` header line; entries further left
  are whatever the client wrote and are never used. Do not set it higher than the real number of
  proxies, or a client can choose its own address; leave it `0` when clients connect directly.
- **A successful sign-in clears that username's failures**, not the address's.
- **An Admin can lift an account lock**: resetting the user's password (**Admin → Users**, or
  `POST /users/{id}/password`) or re-enabling a disabled user clears that username's failures and
  lock. An address lock is not lifted that way; it ends when its lock-out ends, and its 429 says so.
- **Parallel attempts on one username are taken one at a time**, so a burst of simultaneous guesses
  gets no more 401s than the account limit before the 429s start. The address limit is not
  serialised: a burst across many usernames can overshoot it by the number of requests in flight.
- **The counts are in memory, per API process.** A restart forgets every lock, and with several API
  processes or tasks each counts on its own, so the effective limit is multiplied by their number -
  weaker, never a lock-out of the wrong person. The container runs one process (`--workers 1`).

## 2. Roles

Roles are a set, not a ladder (DEC-703). Every role includes Viewer; **Admin does not include
Approver or Analyst**, so the person who manages users cannot also approve a champion unless they are
separately granted Approver.

| Role | May | Examples |
|---|---|---|
| Viewer | read every screen and report | runs, results, schedules, alerts, the consent report of a run |
| Analyst | create and change work | upload, onboard, build datasets, train, score, generate copy, create and fire schedules, upload outcomes, acknowledge alerts |
| Approver | approve | approve or promote a champion, approve campaign copy |
| Admin | administer | users, AWS connection settings, the audit log and its exports, every privacy route (consent, retention, erasure, access requests) |

Scheduled work runs as `system:scheduler`, which holds Analyst only: a schedule can train a
challenger but can never approve or promote it. Every route declares its role in
`api/access_policy.py`; a route without one is refused (DEC-704). A refused control in the UI is
shown disabled, never hidden, with the server's own sentence beside it: "Only an Approver can
approve a champion." `GET /auth/me` lists every route with whether the caller may use it.

## 3. The audit trail

Every mutating request (POST, PUT, PATCH, DELETE) writes exactly one event, whether it succeeded,
failed or was refused, and so do the four row-level downloads (scores, artefacts, copy messages,
dataset samples). Each event records who, when, the action, the object, before/after hashes and the
request id. No data values are ever recorded: details are a closed set of identifier keys, and a
data principal appears only as a salted hash (DEC-705).

- **Append only.** Database triggers refuse UPDATE and DELETE on `audit_events` (and TRUNCATE on
  Postgres) (DEC-714).
- **Viewer.** **Admin → Audit log** (`#/admin/audit`) filters by actor, action (a value ending in `.`
  is a family, e.g. `privacy.`), object, outcome and dates, and downloads the same window as CSV.
- **Retained export.** "Write a retained export" (`POST /audit/exports`) writes JSON lines to
  `<data dir>/audit/exports/` locally, or, when `MARKETING_AI_AUDIT_EXPORT_BUCKET` is set, to S3 with
  Object Lock in COMPLIANCE mode for `MARKETING_AI_AUDIT_RETENTION_DAYS` (DEC-715). A bucket without
  Object Lock fails the export with 502 rather than writing an unprotected copy.
- **A failed audit write does not fail the request.** It is logged at ERROR and counted; alarm on
  that line (DEC-719).

## 4. DPDP controls

Policy lives in `configs/privacy.yaml` (DEC-730): the purposes a consent may be given for, which use
case serves which purpose, which run artefacts are row-level, and whether erasure deletes rows or
tombstones them. Without the file there are no privacy controls and every privacy route answers 409
`PRIVACY_NOT_CONFIGURED`. All of it is on **Privacy** (`#/privacy/...`, Admin).

- **Consent ledger.** Import a CSV (`principal_id, purpose, status, recorded_at`, optional `source`,
  `expires_at`) per client; the whole file loads or none of it does, and problems are reported by row
  and column, never by value (DEC-734). When a ledger exists for a scoring run's client and purpose,
  principals without valid consent are suppressed through Phase 1's `consent_false` rule and counted
  in `consent_report.json` (DEC-732). Without a ledger a run is Phase 1's, byte for byte.
- **Retention.** `governance.retention_days` is enforced (DEC-736, DEC-795): uploads, datasets and
  row-level run artefacts past their deadline are deleted; models, manifests and aggregate reports
  are kept. The dry run is the default everywhere and applying executes exactly the plan reviewed:
  ```bash
  .venv/bin/python -m scripts.run_retention            # what would be deleted
  .venv/bin/python -m scripts.run_retention --apply    # delete it, audited
  ```
  In the UI, apply sends back the plan's hash and is refused (409 `RETENTION_PLAN_CHANGED`) if
  anything changed since the dry run (DEC-748). S3 lifecycle rules are a backstop only and are not
  yet applied by anything (DEC-740).
- **Erasure.** `POST /privacy/erasure` scans every artefact, rewrites each file without the person,
  re-scans it, deletes LLM cache hits, records the request and outcome in the audit log, and flags
  models trained on data that contained the person (DEC-741, DEC-743). Flagged models are retrained
  into a challenger at the next scheduled retraining cycle, not immediately; the champion rule and
  approval still apply (DEC-768).
- **Access requests.** `POST /privacy/access-requests` returns one zip of everything held about the
  person; it is never stored (DEC-745, DEC-753).
- **Principal ids go in request bodies only**, never in a URL, because URLs reach logs and browser
  history that nothing can erase (DEC-746).
- **Breach runbook.** `docs/RUNBOOK.md` section 12.

## 5. Schedules, alerts and outcomes

Schedules are per client × use case and of three kinds: `score` (on the newest tables through the
saved onboarding recipe, or a dataset), `drift_check` and `retrain`. Cadence is a preset (daily,
weekly, monthly at 02:00) or a five-field cron line, in a timezone (default Asia/Kolkata). All of it
is on **Monitoring** (`#/monitoring/...`).

| `MARKETING_AI_SCHEDULER_BACKEND` | What fires schedules |
|---|---|
| `none` (default) | nothing; "Run now" still works |
| `local` | a thread in the API process, every `MARKETING_AI_SCHEDULER_TICK_SECONDS` |
| `eventbridge` | EventBridge Scheduler, running `scripts/fire_schedule.py` as an ECS task (DEC-765) |

- A slot fires at most once, even across processes; slots that passed while nothing ran are recorded
  as `missed`, raise one alert and trigger one catch-up (DEC-763, DEC-764).
- `monitoring.retraining` in a use case's configuration creates managed schedules: `weekly` or
  `monthly` retrains, `on_drift` checks drift weekly and retrains on drift (DEC-767). After changing
  a recipe, "Sync retraining schedules" (`POST /schedules/retraining/sync`) applies it at once.
- **Outcomes.** Once a scoring run's outcome window has matured, upload the actual outcomes
  (`POST /runs/{run_id}/outcomes`, CSV or Parquet). The file is read in memory and never stored; the
  real-world metric is measured on the control group when there is one, and a drop beyond the run's
  `monitoring.performance_alert_drop_pct` raises an alert (DEC-771, DEC-772). Runs with a control
  group also get `incrementality_input.json` for Plan B (DEC-773).
- **Alerts** (drift above threshold, performance drop, failed scheduled job, missed schedule) are
  stored and logged; with `MARKETING_AI_ALERT_BACKEND=sns` they are also published to
  `MARKETING_AI_ALERT_SNS_TOPIC_ARN` (email through SNS). Messages carry ids and counts only (DEC-770).

## 6. Settings

| Setting | Variable | Default |
|---|---|---|
| `auth_mode` | `MARKETING_AI_AUTH_MODE` | `off` |
| `auth_session_ttl_seconds` | `MARKETING_AI_AUTH_SESSION_TTL_SECONDS` | `28800` |
| `audit_export_bucket` | `MARKETING_AI_AUDIT_EXPORT_BUCKET` | none (local export) |
| `audit_export_prefix` | `MARKETING_AI_AUDIT_EXPORT_PREFIX` | `audit` |
| `audit_retention_days` | `MARKETING_AI_AUDIT_RETENTION_DAYS` | `2555` |
| `scheduler_backend` | `MARKETING_AI_SCHEDULER_BACKEND` | `none` |
| `scheduler_tick_seconds` | `MARKETING_AI_SCHEDULER_TICK_SECONDS` | `60` |
| `scheduler_group_name` | `MARKETING_AI_SCHEDULER_GROUP_NAME` | `marketing-ai` |
| `scheduler_target_arn` | `MARKETING_AI_SCHEDULER_TARGET_ARN` | none (required for `eventbridge`) |
| `scheduler_role_arn` | `MARKETING_AI_SCHEDULER_ROLE_ARN` | none (required for `eventbridge`) |
| `alert_backend` | `MARKETING_AI_ALERT_BACKEND` | `log` |
| `alert_sns_topic_arn` | `MARKETING_AI_ALERT_SNS_TOPIC_ARN` | none (required for `sns`) |
| `login_max_failures_per_account` | `MARKETING_AI_LOGIN_MAX_FAILURES_PER_ACCOUNT` | `5` (§1.1) |
| `login_max_failures_per_address` | `MARKETING_AI_LOGIN_MAX_FAILURES_PER_ADDRESS` | `20` (§1.1) |
| `login_failure_window_seconds` | `MARKETING_AI_LOGIN_FAILURE_WINDOW_SECONDS` | `900` (§1.1) |
| `login_lockout_seconds` | `MARKETING_AI_LOGIN_LOCKOUT_SECONDS` | `900` (§1.1) |
| `trusted_proxy_hops` | `MARKETING_AI_TRUSTED_PROXY_HOPS` | `0`; the AWS deployment writes `1` (§1.1) |

The deployment view of the same settings (what the CDK stacks set per environment) is in
`docs/AWS_DEPLOYMENT.md`'s settings table.

## 7. Tests

```bash
make production-test        # every Phase 4b suite, including the two that train a model
make production-test-fast   # the same without them
```

The screen tests run in jsdom through pytest, which writes their fixtures from the real app; they
skip, saying so, until `npm install` has been run once in `tests/integration/production/ui`.

## 8. Known limits (Part 1)

- Sign-in rate limiting (§1.1) is in memory, per API process: a restart forgets every lock, and
  several processes or tasks each count on their own (DEC-861).
- The principal-hash salt is a secret, `MARKETING_AI_PRIVACY_SALT` (DEC-860): a production API does not start without it, the database stack generates and retains it, and a changed salt is refused rather than silently orphaning every stored hash (DEC-883). Hashes written before Plan D used the client id (DEC-733) and must be re-imported once.
- Erasure and access exports run inside the request; a very large store makes a long request.
  Erasure cannot reach downloaded copies, database backups or S3 noncurrent versions.
- "Run now" builds the scheduled dataset inside the request (DEC-781).
- No alert deduplication: a weekly drift check re-alerts while drift persists.
- EventBridge's input substitution, SNS delivery and the Object Lock export have not run against
  real AWS, and the Postgres tables not against RDS; that is M50. (The Postgres append-only trigger
  and every Phase 4b migration pass `make test-postgres` against a local PostgreSQL 16.)
