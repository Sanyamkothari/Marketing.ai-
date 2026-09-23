# Client isolation

**Plan milestone:** M51 (`docs/PHASE4B_PLAN.md`, Part 2). **Waits on:** prerequisite P4, the
deployment-model decision. **State on 2026-09-23:** the part that does not wait on P4 is done: the
audit of every route and storage key, one small gap closed, the open items written as tests that
fail, and the runbook for deploying one client per account. Nothing multi-tenant is built; section 6
sketches what it would take if P4 chooses a Minfy-hosted SaaS.

The tests are `tests/integration/production/test_client_isolation_routes.py`. Every row of the audit
table below names the test that is its evidence. A path that is still open is an
`xfail(strict=True)` test, so the day someone closes it the suite fails until the marker comes off,
and this document cannot quietly go out of date.

---

## 1. The model: one deployment per client, in the client's own AWS account

The current default, and the only model this repository deploys, is **single-tenant**. Each client
of Minfy gets its own AWS account and its own copy of the whole stack: VPC, artefact bucket, KMS
key, RDS instance, ECS service, SageMaker jobs, log groups, alarms and budget
(`docs/AWS_DEPLOYMENT.md` sections 1 and 2). Nothing in `infra/` creates a trust across accounts: no
stack names another account's principal, and no role can be assumed from outside its own account.

### 1.1 Two meanings of "client"

The word names two different things in this code, and most of the questions below come down to
which one is meant.

| | The deployment's client | A client in the client store |
|---|---|---|
| What it is | The customer a whole deployment serves | An onboarding workspace: a name, an industry, its sources, mappings, recipes and datasets |
| Where it lives | `Settings.client_id` (`MARKETING_AI_CLIENT_ID`), set from the CDK context key `client_id` through Parameter Store | `engine/clients.py`, the `clients` table of `clients.db`; the id is minted by `POST /clients` as `c_<slug>_<n>` |
| What reads it | The `client` cost-allocation tag on every stack and every job; the `ClientId` metric dimension; the salt of every data-principal hash (`engine/privacy/config.py::privacy_salt`); the consent gate's fallback for a run with no client of its own | The onboarding API (`/clients/{id}/...`), `POST /datasets`, `POST /runs` with a `dataset_id`, schedules |
| How many per deployment | Exactly one, or none | Any number. Under single-tenant this should be one (section 5.4) |

### 1.2 What the account boundary guarantees

Between two clients deployed in two accounts, these hold because AWS enforces them, not because the
application does:

- **Storage.** Separate buckets, and each bucket's name carries its account id. The task role of one
  account has no grant on another account's bucket, and no bucket policy grants one.
- **Encryption keys.** One customer-managed KMS key per deployment. Destroying one client's key makes
  only that client's objects unreadable.
- **Database.** A separate RDS instance in isolated subnets, reachable only from that deployment's
  security groups.
- **Compute.** SageMaker jobs, Fargate tasks and their roles belong to one account. A job name prefix
  scopes the SageMaker grants inside it (`infra/naming.py::JOB_NAME_PREFIX`).
- **Model calls.** Bedrock is invoked with the account's own credentials, under the account's own
  quotas. What one client's prompts contain never passes through another client's account.
- **Cost.** One bill per account. A per-client cost report is the account's bill, and the `client`
  tag is only a label on top of it.
- **Audit and operations.** CloudTrail, the audit-export bucket with Object Lock, alarms and alert
  topics are all per account.
- **Blast radius.** A leaked task-role credential, a runaway job or a deleted stack affects one
  client.

### 1.3 What it does not guarantee

- **Nothing inside one deployment.** Several client-store clients in one deployment (a Minfy laptop
  onboarding two prospects, a demo deployment, one client with two business units) are separated
  only by the route checks in section 2. They are not separated by storage, keys or roles, and the
  open items in section 4 are real paths between them.
- **Users are deployment-wide.** A role (Viewer, Analyst, Approver, Admin) applies to every client in
  the deployment. There is no way to give a user access to one client and not another, because under
  single-tenant every user works for the deployment's one client.
- **Minfy's own access.** Whoever operates the deployment with administrator rights in the account
  can read everything in it. That is governed by the client's account access policy (who gets
  IAM Identity Center access to the account, and for how long), not by this code.
- **The supply chain.** Every client runs the same image, built by Minfy. A compromised build or
  dependency reaches every client at its next deployment. The controls are the pinned lockfile,
  image digests (`AppContext.validate` refuses a tag) and the M52 dependency and secrets scans.
- **The deploying side.** One engineer's shell or one CI runner holding credentials for several
  client accounts is a path between them. Section 5 keeps them apart: one AWS profile and one
  context file per client, and a check of the account before every deploy.
- **The right account.** `infra/app.py` deploys into whatever account the shell's credentials
  resolve to (`CDK_DEFAULT_ACCOUNT`), and nothing pins a context file to one account. Deploying
  client A's context into client B's account is prevented only by the check in section 5.3.

---

## 2. Audit results

The audit read every route the served app mounts (every method and path in the OpenAPI schema of
`create_app()` on 2026-09-23) and every storage prefix listed in `engine/privacy/layout.py`, and
asked one question of each: can a request naming client A reach client B's data? "Isolated" means
the request is refused, or answers only A's records. All tests are in
`tests/integration/production/test_client_isolation_routes.py`, abbreviated here to their function
names.

### 2.1 Routes

| Route | Isolated between clients of one deployment? | Evidence | Test |
|---|---|---|---|
| `GET /clients`, `POST /clients`, `GET /clients/{id}` | Not scoped, by design: lists every client of the deployment | Clients are the deployment's workspaces, and any role can see them all (section 1.3) | none |
| `POST /clients/{A}/sources` | **Yes.** Stored under `clients/{A}/`, registered under A; an unknown client is 404 | `add_source` refuses a source built for another client (`CLIENT_MISMATCH`) | `test_a_source_upload_for_an_unknown_client_is_refused`, `test_a_clients_raw_sources_live_under_its_own_prefix` |
| `GET /clients/{A}/sources` | **Yes** | `list_sources(client_id)` filters in SQL | `test_a_clients_source_list_holds_only_its_own_sources` |
| `PATCH`, `DELETE /clients/{A}/sources/{B's source}` | **Yes.** 404 `SOURCE_NOT_FOUND`, the same as for an unknown id; B's source is unchanged | `api.routes.sources.load_source` compares the source's owner | `test_another_clients_source_cannot_be_changed_or_deleted_through_this_client` |
| `POST /clients/{A}/mappings/suggest` with B's source | **Yes.** 404 `SOURCE_NOT_FOUND` | `load_source` | `test_a_mapping_cannot_be_suggested_from_another_clients_source` |
| `PUT /clients/{A}/mappings/{B's mapping}` | **Yes.** 404 `MAPPING_NOT_FOUND`; B's mapping stays B's. A body naming another client is 409 `CLIENT_MISMATCH` | The store upserts, so the route checks the stored owner first | `test_another_clients_mapping_cannot_be_overwritten_through_this_client`; `test_api_datasets.py::test_save_mapping_naming_another_client_is_409` |
| `GET /clients/{A}/mappings`, `GET /clients/{A}/onboarding-specs` | **Yes** | Filtered in SQL by client | `test_a_clients_mapping_and_recipe_lists_hold_only_its_own` |
| `POST /clients/{A}/onboarding-specs` naming B's source or mapping | **Yes.** 404 on the borrowed id | `load_source_specs`, `load_mappings` | `test_a_recipe_cannot_name_another_clients_source_or_mapping` |
| `POST /clients/{A}/onboarding-specs/{B's spec}/preview`; `POST /datasets` with A and B's spec | **Yes.** 404 `ONBOARDING_SPEC_NOT_FOUND` | `api.routes.datasets.load_spec` | `test_another_clients_recipe_cannot_be_previewed_or_built_through_this_client` |
| `GET /datasets?client_id=A` | **Yes** | `list_datasets(client_id=...)` | `test_a_dataset_list_narrowed_to_a_client_holds_only_its_datasets` |
| `GET /datasets` with no client | Not scoped, by design: lists every client's datasets | An opt-in filter, like `GET /runs` | none |
| `POST /runs` with B's `dataset_id` and `client_id: A` | **Yes.** 409 `DATASET_CLIENT_MISMATCH`, and no run directory is written | `api.routes.runs._dataset_source` | `test_a_run_for_one_client_cannot_read_another_clients_dataset` |
| `POST /runs` with B's `dataset_id` and no `client_id` | Accepted, and recorded as B's run (`run.json` names B). Nothing is mixed: the request named no client | `RunRecord.client_id` comes from the dataset's manifest | none |
| `GET /runs?client_id=A` | **Yes, since M51.** Before M51 the parameter was ignored and B's runs were listed under A | `api.routes.runs.list_runs`; see section 3 | `test_run_history_narrowed_to_a_client_holds_only_its_runs`; `test_run_history_without_a_client_still_lists_every_run` |
| `GET /datasets/{id}`, `/report`, `/sample`, `/features.sql` | **No.** A deployment-wide id, and the routes take no client | Section 4, item 2 | `test_a_global_id_read_naming_another_client_is_refused` (xfail) |
| `GET /runs/{id}`, `/artefacts/{name}`, `/scores.csv`, `POST /runs/{id}/cancel`, the run-scoped generative routes (`/root-cause`, `/campaign-copy`, its approve and regenerate routes, `/copy_messages.csv`), and the run-scoped outcome routes mounted since the audit (`GET`/`POST /runs/{id}/outcomes`, `GET /runs/{id}/incrementality-input`) | **No**, for the same reason | Section 4, item 2 | `test_a_global_id_read_naming_another_client_is_refused` (xfail; the three run reads stand for the family, and the copy and outcome routes are not exercised by a test of their own) |
| `POST /runs` in `score` mode | **No.** The champion is one per use case, so a champion trained on B's data scores A's dataset | Section 4, item 1 | `test_scoring_a_clients_dataset_never_uses_a_champion_trained_on_another_clients_data` (xfail) |
| `GET /models`, `POST /models/{id}/approve`, `/promote` | **No.** A model version records no client | Section 4, item 1 | `test_model_versions_listed_for_a_client_exclude_another_clients` (xfail) |
| `POST /use-cases/{id}/indexes`, `GET /use-cases/{id}/indexes`, `/indexes/{id}`, `/evaluate`, `/ask`, `POST /use-cases/{id}/reference-sets` | **No.** An index belongs to a use case. `DocIndexManifest.client_id` exists, but no route sets it, and the champion index is one per use case | Section 4, item 4 | `test_a_knowledge_index_can_be_built_for_one_client` (xfail) |
| `POST /uploads`, `GET /uploads/{id}/profile` | Not client-scoped, by design. The upload path is Phase 1's single-client path, and a run of an upload records no client. The consent gate falls back to `Settings.client_id` | Section 5.4 keeps the two identifiers equal | none |
| `/auth/*`, `/users*`, `/audit/*` | Deployment-wide by design. A user is not bound to a client, and the audit log has no client column | Section 1.3 | none |
| `/connection/aws*` | Deployment-wide by design: one AWS identity per deployment | Phase 3a | none |
| `/industries`, `/use-cases/*` (read-only config), `/healthz` | Hold no client data | | none |

Phase 4b routes still being written when this was audited (`/privacy/*`, `/schedules/*`,
`/monitoring/*`) are covered by the requirements in section 3, not by a test in this module. They were
mounted while it was written: `GET /schedules` takes a `client_id` filter, and `POST /schedules`
fills a missing client from its recipe's client (`api.routes.schedules._client_for`).

### 2.2 Storage keys

| Prefix (`engine/privacy/layout.py`) | Keyed by client? | Note |
|---|---|---|
| `clients/<c>/sources/<s>/raw.*`, `profile.json` | **Yes** | `test_a_clients_raw_sources_live_under_its_own_prefix` |
| `datasets/<id>/*` | No. The manifest names the client | `test_every_artefact_of_a_client_lives_under_its_prefix` (xfail) |
| `runs/<id>/*` | No. `run.json` names the client of a dataset run | the same test |
| `uploads/<id>/*` | No. The deployment's own | Phase 1 path |
| `models/**`, `runs/<id>/model/**` | No. One registry per deployment | section 4, item 1 |
| `indexes/<id>/*` | No | section 4, item 4 |
| `llm_cache/<xx>/<key>.json` | No, and nothing in the served app writes it | `test_the_served_api_keeps_no_completion_cache_to_share_between_clients` (tripwire) |
| `clients.db`, `registry.db`, `platform.db` | One file per deployment. `clients`, `sources`, `mappings`, `specs`, `datasets`, consent and schedule rows carry `client_id`. Model versions, runs, users, sessions and audit events do not | |

### 2.3 Other shared state

- **LLM completion cache.** `engine/generative/cache.py` keys a completion by prompt content, model
  and temperature, with no client in the key or the directory. Wired into the app it would be one
  cache for every client: a hit would reveal that another client sent the same prompt, and an erasure
  would have to search across clients. Today nothing constructs one outside `cache.py` and `budget.py`,
  and every route's `Meter` keeps `NullCache`. The tripwire test fails the day that changes.
- **Consent ledger.** Keyed by `client_id`. A dataset run is gated by its dataset's client, and an
  upload run by `Settings.client_id` (`engine/privacy/consent.py::consent_gate_for_run`). When the
  two identifiers differ, and consent was imported under the other one, the gate finds no ledger and
  the run is not gated. It fails open, with no message. Section 5.4 keeps the identifiers equal, and
  section 3 reports the finding.
- **Principal-hash salt.** `Settings.client_id` salts every data-principal hash in the consent
  ledger, the erasure register and the audit log. Two deployments therefore never produce a hash
  that can be joined. One deployment that changes its `client_id` can no longer find its own earlier
  hashes (section 5.4).
- **Cost tags.** Every job carries `client` = `Settings.client_id`, not the dataset's client
  (`engine/runs.py::job_spec_for`, `engine/scheduling/firing.py`). Under single-tenant the account
  is the cost boundary, so this is correct. A tenant tag per job is part of section 6.

---

## 3. What M51 changed, and requirements for code landing now

**Closed (cross-branch, `api/routes/runs.py`, trunk).** `GET /runs` takes an optional
`client_id` query parameter and keeps only runs whose `run.json` names that client. This is the
filter `RunRecord.client_id` was added for (Phase 2 plan section 6.5, change 4) and that
`GET /datasets?client_id=` already had. A run of an upload names no client and is never listed under
one. Without the parameter the answer is exactly what it was. `docs/API.md` is unchanged, because its
endpoint table does not list query parameters. The test failed before the change and passes after it.

**Requirements for code in flight.** These are not tests yet, because the code they apply to is
still being written, or is owned by another workstream:

1. **Schedules** (`engine/scheduling/service.py`, `firing.py`). These already refuse a spec or dataset
   of another client when the schedule names a client, and `POST /schedules` gives a schedule with no
   client its recipe's client. A schedule that names only a `dataset_id` and no client stays
   client-less, and its dataset's client is then never checked (`_check_spec` and the firing's dataset
   check both skip when `client_id` is `None`). On a deployment holding clients it should be refused,
   or given the dataset's client. Scheduled retraining produces a challenger for the one per-use-case
   champion, so it inherits section 4, item 1.
2. **LLM cache.** If `CompletionCache` is wired into a route or job, its root is
   `llm_cache/<client_id>/`, and the tripwire test is extended rather than deleted.
3. **Retention and erasure.** `docs/AWS_DEPLOYMENT.md` section 11.7 describes S3 lifecycle rules
   "per client prefix" as a backstop. Only `clients/<c>/sources/` is a client prefix. Datasets, runs
   and uploads are not (section 2.2), so a per-client lifecycle rule cannot reach them until item 3
   of section 4 is done.
4. **S3 grants on AWS (an M50 gap, Phase 4a).** `infra/naming.py::OBJECT_PREFIXES` grants the task
   role `uploads/`, `runs/`, `models/` and `_bootstrap/` only. `clients/`, `datasets/`, `indexes/` and
   `llm_cache/` are denied, so on AWS onboarding, dataset builds and knowledge indexes would fail
   with AccessDenied. This is not an isolation leak, but it has to be fixed before M50's acceptance
   test.
5. **The client store on AWS (an M50 gap, Phase 2 and 4a).** `api/routes/clients.py::get_client_store`
   always opens a SQLite `clients.db` in the local data directory. On Fargate that is the task's
   ephemeral disk: every client, source, mapping, recipe and dataset index is lost when the task
   stops, and two tasks do not share one. It needs a Postgres `ClientStore` beside `PostgresMetadata`.

---

## 4. Open M51 items (structural; they wait on P4)

Each is a strict xfail in the test module. The reason string of each starts with "M51, waits on
decision P4".

1. **The model registry has no client.** `ModelVersion` records no client, and the champion rule
   (frozen, `engine/registry.py`) keeps one champion per use case. On a deployment with two clients,
   a champion trained on client B's data scores client A's customers, and approving or promoting a
   version for one client replaces the other client's champion. This is the one open item that mixes
   data rather than merely showing it.
   *If P4 is single-tenant:* either refuse a second client in a deployment (section 5.4 makes that
   the operating rule), or refuse, at `POST /runs`, a champion whose training run's `client_id`
   differs from the dataset's. The second is small and needs no change to the frozen rule.
   *If P4 is SaaS:* key the registry by (tenant, client, use case). That unfreezes the champion rule
   and needs a decision record.
2. **Artefact routes are addressed by a deployment-wide id.** Datasets, runs, copy batches and indexes
   are read by id alone. Anyone who holds B's id reads B's artefact, whatever client they work for.
   *Single-tenant:* by design, because every user belongs to the one client. The interim target for a
   deployment that holds several workspaces is to honour the same `client_id` qualifier the list
   routes take (this is what the xfail asks for).
   *SaaS:* the tenant comes from the principal, never from the request, and a foreign id answers 404.
   The cross-tenant suite of section 6.6 replaces this test.
3. **Storage keys carry no client.** Only sources are under `clients/<c>/`. Until every artefact is
   under a client (or tenant) prefix, no S3 prefix policy, per-client lifecycle rule or per-client KMS
   key can be written. Closing it is a key-layout migration of every writer, plus a copy of existing
   data.
4. **Knowledge indexes are per use case.** The index-build form has no client, and the champion index
   answers every client's questions from the same documents.

Two of the findings are configuration hazards rather than paths, and are handled operationally in
section 5.4: the consent gate failing open when `Settings.client_id` and the client-store id differ,
and the principal-hash salt changing with `client_id`.

---

## 5. Runbook: deploying one client

This runbook extends `docs/AWS_DEPLOYMENT.md`. It does not repeat it: every step in that guide
applies, once per client account. Placeholders are in angle brackets. Never commit an account id,
bucket name or ARN (`PARALLEL_WORK_PROTOCOL.md` section 5, rule 5).

### 5.1 A new account

1. **Create the account** under the owning organisation. It is the client's own organisation when the
   client owns the account, or Minfy's when Minfy hosts it for them. Put it in an organisational unit
   whose service control policies deny regions other than `ap-south-1`, deny leaving the
   organisation, and deny disabling CloudTrail. One account per client per environment: `dev` and
   `prod` are two accounts (plan prerequisite P1).
2. **Give access through IAM Identity Center**, with permission sets scoped to that account, never
   long-lived IAM users. Record who from Minfy has administrator access, and for how long. That list
   is the answer to "who at Minfy can see our data".
3. **Create a named AWS CLI profile** for the account on each deploying machine
   (`<client>-<env>`), and never make it the default profile.
4. **Enable Bedrock model access** in `ap-south-1` for the models the client will use (P2). The
   request has a lead time, and it is per account.
5. **CDK-bootstrap** the account and region, as `docs/AWS_DEPLOYMENT.md` section 2 describes.

### 5.2 Context for this client

Keep one context file per client and environment, outside the repository (for example
`~/.marketing-ai/<client>-<env>.cdk.context.json`), and copy it to `infra/cdk.context.json` just
before a deploy. The file is git-ignored, and one checkout holds one deployment's context at a time
(`docs/AWS_DEPLOYMENT.md` section 4.2).

```json
{
  "marketing-ai:client_id": "<client-store id of this client, see 5.4>",
  "marketing-ai:alert_email": "<the client's operations address>",
  "marketing-ai:monthly_budget_usd": "<the client's figure, P3>"
}
```

Add `certificate_arn` and `domain_name` for prod, and the Phase 4b keys the guide lists
(section 11.1).

### 5.3 Naming, and deploying into the right account

- **`env_name` stays `dev` or `prod`.** It is not a client name. `infra/context.py` accepts only those
  two, and every stack, SSM path, secret and log group is named `marketing-ai-<env>-...`. Two clients
  never collide because they are in two accounts, not because their names differ. Do not put a client
  name into `env_name` or `bucket_name_prefix` to "make it unique". Uniqueness comes from the account,
  and the bucket name already carries the account id.
- **The client is identified by the account and the `client` tag.** `client_id` puts `client=<id>` on
  every resource, and on every SageMaker job through `Settings.client_id`.
- **Check the account before every deploy.**

  ```bash
  export AWS_PROFILE=<client>-<env>
  aws sts get-caller-identity --query Account --output text   # must print the client's account
  make aws-deploy ENV=<env>
  ```

  Nothing in the CDK app pins a context file to an account (section 1.3). Pinning one, for example
  with an `expected_account` context key that the synthesis refuses to deploy anywhere else, is
  requested of Phase 4a in the M51 report.
- **CI.** `.github/workflows/deploy-dev.yml` names its GitHub environment after `env_name`, so one
  repository can deploy to one dev account. For several client accounts, deploy from a terminal
  (every first deployment is from a terminal anyway, section 2.1 of the guide), or give the workflow a
  GitHub environment per client and environment, each with its own `DEPLOY_ROLE_ARN` trusted only by
  that account.

### 5.4 The `client_id` setting, set once

1. **Choose the client's name** as it will be registered, for example `Acme Telecom`.
2. **Set `client_id` to the id the client store will mint for it.** On a fresh deployment the first
   client created with that name is `c_<slug>_1`, where the slug is the lowercased name with every
   run of non-alphanumerics replaced by `_` (`engine/clients.py::_slugify`). For `Acme Telecom` the id
   is `c_acme_telecom_1`.
3. **Deploy and bootstrap** (guide sections 4.2 and 4.3), then create the first Admin (guide section
   11.2).
4. **Create exactly one client**, with that name, as the first `POST /clients` on the deployment.
   Check that the answer's `client_id` equals the setting:

   ```bash
   curl -s -X POST "<ServiceUrl>/clients" -H "Authorization: Bearer <token>" \
     -H 'content-type: application/json' -d '{"name": "Acme Telecom", "industry": "<industry>"}'
   # {"client_id":"c_acme_telecom_1"}   must equal the client_id context value
   ```

   If it does not match, fix the context value and redeploy before any data arrives.
5. **Never change `client_id` once the deployment holds data.** It salts every data-principal hash.
   After a change, the consent ledger, the erasure register and the audit log can no longer find
   anyone recorded earlier, and every cost report splits in two.

Why the two identifiers must be equal: an upload run is gated by the consent ledger of
`Settings.client_id`, and a dataset run by the ledger of its dataset's client (section 2.3). If they
differ, consent imported under one is silently not applied to runs that resolve the other. A single
client per deployment also removes the open items of section 4 in practice. There is then no second
client for a champion, an id or an index to leak to.

### 5.5 Budgets and cost reports

- `monthly_budget_usd` and `alert_email` in the context file give the account an AWS Budget with an
  alert (guide section 6.4). The budget is per account, which under this model is per client.
- Activate the `product`, `env` and `client` cost-allocation tags in the account's Billing console
  right after the first deploy (guide section 6.1). Tags activate per account (or at the payer
  account of a consolidated organisation, where the `client` tag is what separates clients on one
  bill).
- The client's cost report is its account's Cost Explorer, grouped by service. Per-run costs follow
  guide section 6.2. Idle cost is paid once per client. That fixed cost per client is the main
  economic argument for SaaS in P4, and it is still `NOT YET MEASURED` (M50).

### 5.6 Teardown when a client leaves

1. **Export first.** Run the audit export (guide section 11.4). Run DPDP access exports if the
   contract requires them. Hand over whatever the contract says the client keeps (models, reports),
   and record what was handed over in a ticket.
2. **Stop the work.** Disable every schedule, and scale the service to zero.
3. **Destroy the stacks** (guide section 8.2). On prod the bucket, key, database and parameters are
   `RETAIN`. That is deliberate, so delete them explicitly, as section 8.3 shows. The audit-export
   bucket cannot be emptied until its Object Lock dates pass, and that is also deliberate.
4. **Schedule the KMS key for deletion last.** Once the key is gone, anything still encrypted with it
   is unreadable, including backups nobody remembered.
5. **Close the account**, or return it to the client, and remove the Identity Center assignments and
   the local AWS profile and context file on every deploying machine.
6. **Record it.** Keep the date, who did it, and the audit-export object key in Minfy's client register.

---

## 6. If P4 chooses a multi-tenant SaaS: design sketch

Not built. This is what "tenant isolation" would have to mean for one Minfy-hosted deployment
serving several client organisations. The bar is the one the account gives today: a defect in one
layer must not by itself expose another tenant's data. So every layer below enforces the boundary on
its own, and no layer trusts the one above it.

### 6.1 A tenant on every request, every row, every key

- **Tenant from the principal, never from the request.** Sign-in (P5) resolves a user to exactly one
  tenant. A `TenantContext` dependency, installed through the same `PHASE_APP_HOOKS` mechanism access
  control uses, puts it on the request. No route takes a tenant parameter. A foreign id answers the
  same 404 as an unknown one, as Phase 2's `load_source` already does.
- **Jobs carry it.** The job spec gains `tenant_id`. The SageMaker and scheduler paths run with it,
  and a job refuses to start without it.
- **Rows.** A non-null `tenant_id` goes on every table: the client store's five tables (once they
  move to Postgres, section 3 item 5), `model_versions`, `runs`, users and sessions, `audit_events`,
  the consent, erasure and retrain tables, and schedules, firings and alerts. It leads every primary
  key and index. The registry's champion uniqueness becomes (tenant, client, use case).

### 6.2 Postgres row-level security

- `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` on every table, with
  one policy per table:
  `USING (tenant_id = current_setting('app.tenant_id')::text) WITH CHECK (same)`.
- The application connects as a role that owns nothing and lacks `BYPASSRLS`. Migrations run as a
  separate owner role.
- Every transaction starts with `SET LOCAL app.tenant_id = :tenant`, from the request's context, in
  the session factory. A pooled connection therefore cannot carry one tenant's setting into the next
  request. A missing setting makes `current_setting` raise, so the query fails closed instead of
  matching nothing silently.
- SQLite (laptop mode) has no RLS. There, the tenant filter in the repository layer is the only layer,
  and SaaS is a Postgres-only mode.

### 6.3 S3: a prefix per tenant, enforced by IAM session policies

- Every key moves under `tenants/<tenant_id>/...`. This is section 4 item 3, done once for all
  prefixes, with a migration that copies existing objects.
- **Attribute-based access.** The task role no longer reads the bucket directly. For each request or
  job it calls `sts:AssumeRole` on a tenant-access role, with the session tag `tenant=<id>` (and
  `sts:TagSession` allowed only to the task role). The tenant-access role's policy grants
  `arn:aws:s3:::<bucket>/tenants/${aws:PrincipalTag/tenant}/*` and a `ListBucket` condition on
  `s3:prefix` for the same path. A bug that builds the wrong key then gets AccessDenied from S3
  instead of another tenant's object.
- Credentials are cached per tenant for their lifetime (15 minutes to 1 hour), which keeps the extra
  STS calls well below STS rate limits. SageMaker jobs get a tenant-tagged execution-role session the
  same way.

### 6.4 A KMS key per tenant

- A customer-managed key per tenant, created when the tenant is onboarded (so by a provisioning
  service, not a static CDK stack), and tagged `tenant=<id>`. Its key policy allows use only to
  sessions whose `aws:PrincipalTag/tenant` matches the key's tag.
- Every `PutObject` names the tenant's key (`SSEKMSKeyId`), because a bucket default cannot vary by
  prefix. A bucket policy denies a `PutObject` under `tenants/<t>/` that names any other key.
- Offboarding a tenant means scheduling its key for deletion (crypto-shredding), plus deleting the
  prefix.
- RDS cannot encrypt rows with per-tenant keys. There, RLS is the control. Per-tenant keys for
  database fields that hold personal data (application-level envelope encryption) would be a further
  step.

### 6.5 Per-tenant budgets and cost reports

- `tenant` becomes a cost-allocation tag on every SageMaker job, and a dimension on every EMF metric
  (next to `ClientId`).
- Shared costs (ALB, Fargate, RDS, NAT or endpoints) are not taggable per tenant. They are
  apportioned monthly from measured usage: request counts, job-seconds, and S3 bytes per prefix from
  S3 Storage Lens. The monthly report states the apportioning rule it used.
- Per-tenant AWS Budgets filter on the `tenant` tag, for the taggable part. A per-tenant spend cap
  (Bedrock tokens, training minutes) is enforced in the application, because AWS Budgets alerts but
  does not stop anything.
- Noisy neighbours: per-tenant Bedrock token quotas, a limit on concurrent jobs per tenant, and API
  rate limits.

### 6.6 The cross-tenant test suite that would prove it

- **Every route, generated from the app.** In the style of
  `tests/integration/production/access_support.py::live_routes`: two tenants, each with one of every
  object; for every route and every foreign id, 404 and no write. The route list is read from the
  app, so a new route is covered the day it lands, and a route with no tenant rule fails the suite.
- **Lists.** Every list route, as each tenant, answers only that tenant's rows.
- **Database** (`-m postgres`). As the application role, with `app.tenant_id` set to A: a `SELECT`
  of B's rows returns none, and an `INSERT` or `UPDATE` of a row with B's tenant raises. With no
  setting, every query raises. The application role cannot `SET ROLE` to the owner.
- **Storage.** A property test: every key any writer produces for tenant A starts with
  `tenants/A/`, and a moto-backed S3 with the session policy refuses a read of `tenants/B/`. moto
  does not evaluate IAM by default, so the policy documents are also checked with offline assertions,
  and once against a real account with `iam simulate-principal-policy` (`-m aws`).
- **KMS.** The key policy refuses A's session on B's key (a CDK assertion offline, and a paid test
  once).
- **Jobs.** A job spec without a tenant is refused. A job for A given B's dataset key fails at S3.
- **Caches and indexes.** An LLM cache hit and a vector-index retrieval never cross tenants.
- **The strict xfails in this module** become passing tests or are replaced by the suite above.

### 6.7 Effort, honestly

An estimate from reading the code, not a measurement. It assumes one engineer who already knows
this codebase:

| Work | Engineer-weeks |
|---|---|
| Tenant context, the job spec, every route taking the tenant from the principal | 2–3 |
| `tenant_id` on every table, Alembic migrations, RLS policies, session wiring, a Postgres client store | 3–4 |
| Key-layout migration for every writer and reader, the ABAC role, credential vending, SageMaker sessions | 3–4 |
| Per-tenant KMS keys, provisioning and offboarding, bucket-policy guards | 2 |
| Registry keyed by tenant and client (unfreezing the champion rule, with a decision record) | 1–2 |
| Per-tenant cost tags, metrics, apportioning report, quotas and rate limits | 1–2 |
| The cross-tenant suite (section 6.6), including the Postgres and paid AWS parts | 2–3 |
| Hardening, an external penetration test, fixes | 2–3 |
| **Total** | **16–23 engineer-weeks**, about 4–6 months for one engineer or 3 months for two |

Not in the estimate: legal work (under DPDP, Minfy becomes a processor hosting several data
fiduciaries in one system, which changes the contracts and the breach runbook); data residency
requests from individual tenants; and moving existing single-tenant deployments into the SaaS. The
single-tenant alternative costs no engineering beyond this runbook. It costs one idle stack per
client, a figure M50 still has to measure. P4 is the comparison of that fixed cost against this
estimate.
