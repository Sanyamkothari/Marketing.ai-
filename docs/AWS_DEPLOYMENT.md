# AWS deployment — Phase 4 notes

Phase 1 runs entirely locally. **Nothing in this document is built.** It records what Phase 4
replaces, which seam each replacement goes behind, and what has to stay true meanwhile so that the
move is a set of new *implementations* rather than a rewrite (plan §12).

Every section names the class a future implementer replaces and the file it lives in. Where a
number would be needed — an instance type, a cost, a latency — none is given: nothing here has been
measured, and a plausible-looking figure in a deployment note is the same mistake as a fabricated
metric in the product (plan §13.3).

The deployment model assumed throughout is **an accelerator in the customer's own AWS account**,
with the product running locally until then (plan §14). That assumption is what makes per-tenant
identity an account boundary rather than a row filter.

---

## 1. What Phase 4 changes

| Phase 1 | Phase 4 |
|---|---|
| Local filesystem artefact store | S3 |
| In-process thread pool | SageMaker Training and Processing jobs |
| SQLite model registry | SageMaker Model Registry, with Postgres for run metadata |
| In-process scoring | Batch Transform |
| No identity | IAM per tenant, in the customer's account |
| Drift measured when a scoring run happens | Drift measured on a schedule |
| Retraining by hand | Retraining triggers |
| No audit trail beyond the run records | Audit log, retention, consent and deletion (DPDP) |

---

## 2. The three protocols that carry the swap

These are the only places that know where anything physically lives. Keeping them clean **is** the
Phase 4 plan.

| Protocol | Phase 1 implementation | Phase 4 implementation |
|---|---|---|
| `Storage` (`engine/storage.py`) | `LocalStorage` over a directory | `S3Storage` |
| `JobRunner` (`engine/jobs.py`) | `ThreadJobRunner` | a SageMaker-backed runner |
| `ModelRegistry` (`engine/registry.py`) | `LocalModelRegistry` over SQLite | SageMaker Model Registry |

All three are constructed in exactly one place: `api/deps.py`, where `get_storage`, `get_registry`
and `get_jobs` build them on first use and cache them on `app.state`. A Phase 4 deployment changes
those three functions and nothing else in the API; `create_app(data_dir=…, config_root=…)` and
`app.dependency_overrides` already let a test or a different environment substitute any of them,
which is the same lever the cloud implementations will use.

### 2.1 `Storage` → S3

Keys are opaque posix strings validated by one gate, writes are atomic, and `local_path()` is the
only escape hatch (DEC-016). Today the layout is **two** prefixes, from `upload_key` and `run_key`:

```
uploads/{upload_id}/{filename}   the customer's file, as uploaded
runs/{run_id}/{filename}         run.json, status.json, run_config.json, profile.json,
                                 validation.json, prepare.json, split.json, leaderboard.json,
                                 best_model.json, evaluation.json, confusion_matrix.json,
                                 decile_lift.json, baseline.json, fairness.json,
                                 feature_importance.json, drift_baseline.json, schema.json,
                                 run_manifest.json, row_explanations.parquet, scores.csv,
                                 model/  (the AutoGluon predictor directory, with scorer.json)
```

`engine/contracts.py` enumerates both sets — `TRAIN_ARTEFACTS` and `SCORE_ARTEFACTS` — so there is
one list to read rather than a convention to infer.

That maps onto a bucket unchanged: the prefixes become S3 prefixes, `write_bytes`/`write_model`
become `PutObject`, `list_keys` becomes a paginated `ListObjectsV2`. Two details decide how much
work is left:

- **Atomic writes.** `LocalStorage` writes a temp file and `os.replace`s it, so a reader sees the
  old object or the new one. A single `PutObject` is already atomic, so `S3Storage` gets this for
  free — but a multipart upload is not, and the completion step is what must be treated as the
  commit point.
- **`local_path()`.** AutoGluon and pyarrow both insist on a real path: `AutoGluonScorer.save`
  writes a predictor *directory*, `load_scorer` reads one back through
  `storage.local_path(predictor_key)`, and `_open_parquet` opens the upload by path. `S3Storage`
  materialises the object or prefix into a temp directory and returns that path. This is the one
  place with real work in it, and it is deliberately the only one: everything else in the engine
  goes through the byte and model methods.

The per-run prefix is also the unit an audit record points at — but **it is not yet a safe unit of
retention**, and that is the one thing in the key layout Phase 4 should change before a bucket
exists. `model_key(model_id, …)` is defined in `engine/storage.py` and is currently unused:
`predictor_key_for(run_id)` puts the fitted predictor at `runs/{run_id}/model`, and
`ModelVersion.predictor_key` points there. So the champion's model lives inside a training run's
directory, and deleting that run under `governance.retention_days` would delete the model that is
scoring the customer's base. Separating the two prefixes is a small change now (one key helper, one
call site, a migration of existing runs) and a large one once objects are in a customer's account.

### 2.2 `JobRunner` → SageMaker Processing and Training

`ThreadJobRunner` gives each job a `CancelToken`; stages poll it between steps and a cancel is
observed at the next checkpoint (DEC-017). That is the same contract a remote job gives — a stop
request lands between stages, not inside one — so the state machine does not change:
`submit / status / cancel / shutdown`, with `JobInfo` carrying `pending | running | done |
cancelled | failed`.

The mapping is one SageMaker job per pipeline run, not one per stage: `Pipeline._run_stage`
already brackets every stage with a cancel checkpoint and writes `status.json` after each, so the
Running screen keeps working if `status()` reads the remote job's state and the pipeline keeps
writing the same two documents to the same keys.

Two things must be dealt with when the runner stops being in-process:

- **`JobInfo.error` currently carries `str(exc)`** (`engine/jobs.py`). Whatever surfaces that field
  to a user has to carry a coded message from `ENGINE_ERRORS`/`SCORE_ERRORS` instead, the way
  `run_error` already does for `status.json` — an exception's own text is not a user-facing message
  and can quote a cell value.
- **Cancellation of a queued remote job** has no local thread to interrupt; `cancel` becomes a stop
  request on the job, and the "pending jobs are cancelled outright" branch becomes "the job is
  stopped before it starts".

### 2.3 `ModelRegistry` → SageMaker Model Registry

`LocalModelRegistry` stores `ModelVersion` rows in SQLite behind
`register / get / list_versions / get_champion / next_version / approve / promote / archive`.

The policy is **not** in the registry. `should_promote` is a pure function in `engine/registry.py`
(DEC-028), and the caller must hand it a champion score re-measured on the challenger's own test
split (`ChampionScore`, DEC-044). That separation is what makes the managed-service swap cheap: a
Phase 4 registry stores facts — model package groups, approval status, the artefact URI — and the
champion rule stays here, tested without a database.

Mapping:

| Engine concept | SageMaker |
|---|---|
| `ModelVersion` | a model package in a model package group, one group per use case |
| `next_version` | the package group's version counter |
| `approve` (`pending_approval → champion`) | `ModelApprovalStatus` = `Approved` |
| `promote` / demotion in the same transaction | approval status plus a champion tag on the group |
| `archive` | `Rejected`, or a lifecycle tag |
| `test_score`, `metric`, `recipe_hash` | model package metadata |

`ModelStatus` has states SageMaker does not: the demote-the-incumbent-in-the-same-transaction rule
has no single API call behind it, so a Phase 4 registry needs its own small consistency step, and
the `UNIQUE(use_case_id, version)` guarantee has to be re-established against the package group.

---

## 3. Postgres for run metadata

SQLite holds the registry today; the run records live as JSON under `runs/`. Phase 4 moves both the
registry rows and the run metadata into Postgres, because a scheduled drift monitor and a
retraining trigger need to *query* runs, and `list_keys("runs/")` plus a read of each `run.json` —
which is what `GET /runs` does now — does not scale past a laptop.

`run_manifest.json` is already shaped for this. It is one flat record per run, written for every
outcome including failures (DEC-042), carrying the recipe hash, the dataset fingerprint, the
metrics, the model version and the duration. It is a table, not a document, and it is the natural
first Postgres table. The artefacts themselves stay in S3 and the row points at the prefix.

---

## 4. Batch Transform for scoring

The score flow is `ingest → validate_against_schema → prepare(replay) → predict → explain_rows →
actions → export`. Three properties make it a batch job rather than an endpoint:

- **`FeatureSchema` (`schema.json`) travels with the model version.** `validate_against_schema`
  compares the scoring file against the columns the model was fitted with and reports every
  missing, extra or type-changed column by name. A Batch Transform job runs the same check on the
  same schema object.
- **`prepare.replay` re-applies the recorded transforms and measures nothing.** Whatever was fitted
  was fitted on the training rows only (DEC-046), so a scoring job needs no state beyond
  `prepare.json`.
- **The output is a file, not a response.** `export` writes `scores.csv`; nothing in the flow wants
  a request/response round trip.

A real-time endpoint is a different product decision and is not implied by any of this. If one is
ever wanted, the seam is `AutoGluonScorer`: it already loads a predictor and scores a frame, with no
knowledge of where the frame came from.

---

## 5. Per-tenant identity

Phase 1 has no authentication, no tenancy and no authorisation. The deployment assumption makes the
first cut of tenancy an **account boundary**: one customer, one AWS account, one deployment, one
bucket. Nothing in the engine has to learn what a tenant is for that to hold.

What must exist before the API is reachable in an account:

- **Authentication on every route.** There is none today. `POST /models/{id}/approve` and
  `POST /models/{id}/promote` require `approved_by` and `promoted_by`, and `api/routes/models.py`
  is explicit that these are strings the caller typed and nothing verifies — recorded as a claim
  rather than invented, which is the right Phase 1 answer and not a Phase 4 one. Approval is the
  one action with a real authorisation question behind it: whoever can call it decides which model
  scores the customer's base, so it is the first route that needs a verified identity behind the
  name (plan §14 leaves the approval role open).
- **An IAM role per deployment**, scoped to that customer's bucket prefix, their model package
  group and their job queue. Storage keys carry no tenant, so the prefix is the boundary — which is
  precisely why `Storage` must not grow a "read any key" method.
- **CORS tightened.** See §8.

If a shared multi-tenant deployment is ever wanted instead, the tenant becomes part of the storage
key and every `Storage` call site changes. That is the most expensive thing in this document, and
it is the reason the key layout is worth settling now rather than later.

---

## 6. Drift monitoring and retraining triggers

The measurement exists and is already stored per model version:

- `register` writes `drift_baseline.json` (`DriftBaseline`) from the training rows: per-feature
  bins for numerics, levels for categoricals, null rates.
- `compute_drift` (`engine/stages/score.py`) computes PSI per feature **in the baseline's own bins**
  — the scored file is never re-binned, because two independently binned files are not comparable.
- The verdict comes from `monitoring.drift_psi_threshold` (default `0.20`).
- **Drift never blocks a run** (DEC-051), and a comparison that cannot be made is reported as *not
  measured* rather than as a number. A file with no rows, a baseline with no features and a model
  with no stored baseline all produce `None`, not a zero.

Phase 4 adds only the *schedule*: a periodic job that reads recent scoring runs, or re-scores a
sample, and records the drift report where the monitor can read it. The same rule holds on a
schedule as in a run — a drift report is a signal, never a gate — and "not measured" has to stay
distinguishable from "stable" in whatever dashboard it lands in.

`monitoring.retraining` already enumerates the triggers the product intends: `manual`, `on_drift`,
`weekly`, `monthly`, with `performance_alert_drop_pct` for the fourth case (the champion's measured
score falling). A trigger fires the ordinary train flow through `JobRunner`, and the result meets
the ordinary champion rule: `should_promote` against the incumbent re-scored on the new test split,
and `governance.approval_required` deciding whether the winner waits for
`POST /models/{id}/approve`. **A retraining trigger must not be allowed to promote anything the
champion rule would not**, which is why the rule is a pure function rather than a registry
behaviour.

---

## 7. Data-protection controls

The configuration for these already exists; Phase 4 is where they acquire teeth.

| Control | Config | Phase 1 | Phase 4 |
|---|---|---|---|
| Retention | `governance.retention_days` (0–730, `0` = delete the upload right after the run) | recorded, not enforced | an S3 lifecycle rule on `uploads/`, plus a deletion path for `runs/` — which needs the model moved out of the run prefix first (§2.1) |
| Consent | `governance.consent_column` | `prepare` keeps only rows where it is truthy and records how many were removed; `CONSENT_COLUMN_MISSING` blocks a run whose consent column is absent | unchanged, and the removed count becomes an audit record |
| PII | `prepare.pii_handling` (`redact` \| `drop_columns` \| `keep`) | detected at ingest, redacted or dropped at prepare; `PII_DETECTED` warns per column and never records a matched value | unchanged, plus encryption at rest and in transit |
| Deletion | — | not implemented | a per-entity deletion path across the upload, the run artefacts and the metadata row |
| Audit | — | the run records and `run_manifest.json` | an append-only log of who uploaded, ran, approved and exported what |

Two things already hold and must keep holding:

- **No data value ever reaches a log**, on any path, including failure paths (plan §13.7).
  `engine/utils/logging.py` carries this: `log_stage` admits a stage name, a row count and a
  duration, `log_failure` names an exception's class and not its message, and
  `RedactingFormatter` — installed by `configure_logging` — renders a traceback's frames while
  withholding every exception message, because a library builds those out of the value that upset
  it. `tests/unit/test_logging_audit.py` enforces this mechanically by driving the stages over a
  frame of sentinel values. A Phase 4 handler that ships logs to CloudWatch must be attached
  through `configure_logging`, or it will format records its own way and undo this.
- **The template rows are illustrative, not real.** Nothing generated from them is ever presented
  as a measurement (plan §13.3).

Deletion is the one control with no seam yet. The reason it is cheap to add is that everything
belonging to a run lives under one prefix and everything belonging to a model under another — but
a deletion request is *per entity*, and an entity's rows are inside files under both. That
requirement should be settled before the key layout is frozen in a customer account.

---

## 8. CORS must be tightened before Phase 4

Phase 1 serves the API with **open CORS** (DEC-024). The UI is the prototype opened as a local file,
with no origin and no auth, so a permissive policy is the only thing that works and nothing is at
risk: there is no authentication, no multi-tenancy and no customer data behind the API on a laptop
(plan §1.3).

That stops being true the moment the API is reachable in an AWS account. Before Phase 4:

- replace the open policy with an explicit allow-list of the UI's origin(s);
- add authentication and per-tenant IAM ahead of, or together with, that change — an allow-list is
  not an authorisation mechanism;
- re-check the two routes DEC-024 added beyond plan §8 (`GET /healthz` and
  `GET /use-cases/{id}/template_README.md`): `/healthz` returns the version and must stay free of
  any other detail.

---

## 9. The short list

If only one thing is carried out of this document into every Phase 1 review, it is that these five
must stay true, because each one is a seam that a single shortcut would close:

1. Nothing outside `engine/storage.py` builds a path; everything uses a key.
2. Nothing outside `api/deps.py` constructs a `Storage`, a `JobRunner` or a `ModelRegistry`.
3. The champion rule stays a pure function, and its caller keeps re-scoring the incumbent.
4. Cancellation stays cooperative and checked between stages.
5. No data value reaches a log, and no unmeasured number is ever rendered as a measurement.

---

## 10. Running the Bedrock smoke tests

Everything above this line is a Phase 4 plan for a system that is not built yet. This section is
the one exception: `BedrockLLMClient` (`engine/llm.py`) is already built, in Phase 3a, and
`tests/integration/test_bedrock_smoke.py` is an opt-in suite that calls the real service with it.
It is documented here rather than in `docs/GENERATIVE.md` because it is an AWS account operation -
credentials, a region, IAM permissions - and this is where those already live.

**Why it exists at all.** `generative.llm.backend` defaults to `fake`, and every generative test
that is not this one runs against `GroundedFakeLLMClient`, which hashes words into buckets so
retrieval is exercisable offline (DEC-214). That fake has one documented blind spot: DEC-219 found
a question that shares the corpus's *vocabulary* without sharing its *meaning* and scores it ABOVE
several genuine questions, because a bag of words cannot separate the two. Nothing offline can
prove a real embedding model gets that right. This suite is what does, and it is the only place in
the repository allowed to call Bedrock for real.

**Credentials.** Ordinary boto3 credential resolution - `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` (plus `AWS_SESSION_TOKEN` for temporary ones), `AWS_PROFILE` against a
shared credentials file, or an execution role's own instance profile. Nothing test-specific is
invented for this. The identity needs `bedrock:Converse`, `bedrock:InvokeModel` and, if the chosen
generation model supports it, `bedrock:CountTokens` on whichever model ids you configure below -
and, separately from IAM, that account must have requested and been granted *model access* for
those model ids in the Bedrock console for the region you run in, which is a one-time, per-account,
per-region step AWS gates independently of any IAM policy.

**Region and model ids.** Three environment variables, read directly by the test module rather than
through `engine.settings.Settings` - that module speaks for the whole application's single
`MARKETING_AI_BEDROCK_MODEL_ID`, where this suite needs a generation id and a separate embedding id
to run independent proofs, and is deliberately a smaller, louder, test-only vocabulary instead:

| Variable | Required | Meaning |
|---|---|---|
| `BEDROCK_SMOKE_REGION` | yes | The region to call, e.g. `ap-south-1` - must be one where the chosen models have been granted access. |
| `BEDROCK_SMOKE_GENERATION_MODEL_ID` | yes | The Bedrock model id `complete` is proved against. |
| `BEDROCK_SMOKE_EMBEDDING_MODEL_ID` | yes | The Bedrock model id `embed` is proved against. |
| `BEDROCK_SMOKE_EMBEDDING_DIMENSIONS` | no | Asserts the embedding model's exact vector width; omitted, the tests only check that the width is positive and stays the same across calls. |

No default or example id is given here for the same reason `configs/llm_prices.yaml` ships empty
and no model id appears anywhere in `engine/` (DEC-204, DEC-208): a model id is deployment data,
it differs by account and region, and a plausible-looking one printed in a document gets copied
into a real run by someone who never meant to pick it. Read the model ids your own account has been
granted access to off the Bedrock console's model catalogue instead.

**All three variables unset is the default, and is silent and instant.** Every test in the module
self-skips, loudly, before importing `boto3` at all when any of the three is missing, so a checkout
with no AWS access, no `-m bedrock`, and no export of any of the above runs the rest of the suite
exactly as it always has. Setting the three variables without real credentials behind them still
skips, one level later, once `boto3.Session().get_credentials()` comes back empty - bounded by
botocore's own short timeouts, never an open-ended wait.

**Running it.**

```sh
export BEDROCK_SMOKE_REGION=ap-south-1
export BEDROCK_SMOKE_GENERATION_MODEL_ID=<a generation model id your account can invoke there>
export BEDROCK_SMOKE_EMBEDDING_MODEL_ID=<an embedding model id your account can invoke there>
.venv/bin/python -m pytest tests/integration/test_bedrock_smoke.py -m bedrock -v
```

The `bedrock` marker (registered in `pyproject.toml`) is what `-m bedrock` selects; it does not by
itself exclude the suite from an unfiltered run; the environment-variable check does that.

**Rough cost.** One run makes one short `Converse` call, one `CountTokens` call (which Bedrock does
not bill as generation), and four short `embed` calls - a few hundred tokens moved in total, with
the single completion call dominating whatever it costs. `tests/integration/test_bedrock_smoke.py`'s
own module docstring carries the up-to-date estimate rather than this file repeating one that could
drift out of step with it; as a shape rather than a figure, expect it to sit well under what a
single page of this document costs to review, and to scale with the price of the generation model
you chose far more than with anything else the suite does.
