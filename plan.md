# Marketing AI — Implementation Plan

**Reference prototype (the target experience):** https://claude.ai/artifact/8ARwky4HnEPQyEfRKa5Svi
**Owner:** Minfy — AI/ML team
**Status:** Phase 1 build

> **How to use this document.** This is the single source of truth for building the product. Read it fully before writing code. Open the reference prototype and click through it: Overview → a use case → Setup → Run → Results → Data / Model / Output pages. The prototype defines *what the user sees*; this document defines *how it works underneath*. Where the two disagree, this document wins. Where this document is silent, match the prototype. Where both are silent, choose the simplest option that keeps the system config-driven, and record the decision in `docs/DECISIONS.md`.

---

## 1. What we are building

A **reusable marketing AI engine**. A business user selects a use case (e.g. Targeted Advertisement), uploads a CSV, picks the primary key and target column, and clicks Run. The engine validates the data, trains the best model with AutoML, evaluates it, explains it, and can later score new data. Results are shown as a pipeline: **Data → Model → Output**.

The engine is generic. Each use case is a **configuration file**, not code. Adding a new use case, or a new industry, means adding config, not features.

### 1.1 The product in one paragraph

Instead of building a separate model per client, we build one engine. Each industry gets a template of lifecycle stages and use cases. Each client maps their data to a standard format. AutoML trains the best model on their data. Users can train on historical data or score new data with the approved (champion) model, and every prediction comes with a reason and a recommended action.

### 1.2 Use cases in scope (from the prototype)

| ID | Name | Lifecycle stage | Type | Target column | Problem type |
|---|---|---|---|---|---|
| `targeted-advertisement` | Targeted Advertisement | Awareness | Predictive | `converted_30d` | Binary classification |
| `ai-onboarding-assistant` | AI Onboarding Assistant | Onboarding | Generative | `reference_answer` | RAG assistant |
| `order-fulfillment` | Order Fulfillment | Service / Payments | Predictive | `delayed_beyond_sla` | Binary classification, time-based split |
| `fault-prediction` | Fault Prediction | Service / Payments | Predictive | `fault_next_72h` | Binary classification, time-based split |
| `payment-propensity` | Payment Propensity | Service / Payments | Predictive | `late_or_missed_payment` | Binary classification |
| `rca` | RCA (Root Cause Analysis) | Churn | Hybrid | `churn_next_60d` | Binary classification + LLM summary |
| `win-back-campaign` | Win-back Campaign | Win-back | Hybrid | `reactivated_90d` | Binary classification (uplift later) + LLM copy |

**Phase 1 builds the predictive engine end to end for `targeted-advertisement` and `payment-propensity`.** The other predictive use cases must work by adding config only. Generative and hybrid parts are Phase 3.

### 1.3 Non-goals for Phase 1

- No authentication, multi-tenancy, or billing.
- No raw-table aggregation; the upload must already be one row per entity (see the data contract).
- No LLM features (RAG assistant, root-cause summaries, generated copy).
- No uplift modelling; Win-back trains a plain propensity model.
- No AWS deployment; the system runs locally with storage and compute behind interfaces so AWS is a swap, not a rewrite.

---

## 2. Architecture

```
┌──────────────┐   HTTP/JSON   ┌────────────────────┐          ┌─────────────────────┐
│  Web UI      │ ────────────▶ │  API (FastAPI)     │ ───────▶ │  Engine (Python)    │
│  (prototype  │ ◀──────────── │  runs, uploads,    │ ◀─────── │  validate, prepare, │
│   HTML/JS)   │   polling     │  artefacts         │          │  train, eval, score │
└──────────────┘               └────────┬───────────┘          └──────────┬──────────┘
                                        │                                 │
                               ┌────────▼───────────┐          ┌──────────▼──────────┐
                               │  Metadata store    │          │  Artefact store     │
                               │  SQLite (Phase 1)  │          │  local FS (Phase 1) │
                               │  Postgres (later)  │          │  S3 (later)         │
                               └────────────────────┘          └─────────────────────┘
```

### 2.1 Principles

1. **Config over code.** Use-case behaviour lives in YAML under `configs/use_cases/`. The engine never branches on a use-case ID.
2. **Every stage produces an artefact.** Each pipeline stage writes a JSON (and files) to the run directory. The UI renders from these artefacts; it never computes anything itself.
3. **Interfaces at the edges.** Storage, compute and model registry are Python protocols with a local implementation now and an AWS implementation later.
4. **Plain-language failures.** Every validation error has a machine code and a message a business user can act on.
5. **Nothing fabricated.** The UI shows real numbers from real runs. If a value doesn't exist yet, show "—", never a placeholder number.

### 2.2 Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11 | AutoML ecosystem |
| AutoML | **AutoGluon Tabular** (`autogluon.tabular`) | Open source, strong on tabular data, runs locally and on SageMaker, gives a leaderboard, feature importance and supports classification/regression |
| Explainability | SHAP (`shap`) on the best single model; AutoGluon feature importance for the ensemble | Per-row reasons |
| Data | pandas, pyarrow | CSV and Parquet |
| API | FastAPI + uvicorn | Simple, typed, async job endpoints |
| Jobs | Background thread/process pool in Phase 1 (`concurrent.futures`), abstracted behind `JobRunner` | Swap for SageMaker Processing/Training jobs later |
| Metadata | SQLite via SQLModel | Zero setup; swap to Postgres later |
| Config | YAML + pydantic models | Validated configs |
| UI | The prototype's HTML/JS, adapted to call the API | Already designed; no framework needed |
| Tests | pytest, hypothesis for validators | |
| Lint/format | ruff, black, mypy (strict on `engine/`) | |

Pin versions in `pyproject.toml`. If AutoGluon fails to install in the environment, stop and report; do not silently fall back to another library.

---

## 3. Repository layout

```
marketing-ai/
├── README.md
├── pyproject.toml
├── Makefile                      # make setup / test / run / lint
├── configs/
│   ├── engine.yaml               # global defaults (advanced settings defaults)
│   ├── industries/
│   │   └── telecom.yaml          # lifecycle stages + which use cases appear
│   └── use_cases/
│       ├── targeted_advertisement.yaml
│       ├── payment_propensity.yaml
│       ├── order_fulfillment.yaml
│       ├── fault_prediction.yaml
│       ├── rca.yaml
│       └── win_back_campaign.yaml
├── templates/                    # downloadable CSV templates per use case
│   └── targeted_advertisement_template.csv
├── engine/
│   ├── __init__.py
│   ├── config.py                 # pydantic models for YAML configs + advanced settings
│   ├── contracts.py              # pydantic models for every artefact JSON (section 7)
│   ├── storage.py                # Storage protocol + LocalStorage
│   ├── registry.py               # ModelRegistry protocol + local implementation
│   ├── jobs.py                   # JobRunner protocol + ThreadJobRunner
│   ├── pipeline.py               # orchestrates stages for train and score
│   ├── stages/
│   │   ├── ingest.py             # read CSV/Parquet, infer schema, profile
│   │   ├── validate.py           # all checks in section 6
│   │   ├── prepare.py            # cleaning, exclusions, PII, split
│   │   ├── train.py              # AutoGluon training + leaderboard
│   │   ├── evaluate.py           # metrics, confusion matrix, calibration, decile lift, fairness
│   │   ├── explain.py            # feature importance + per-row SHAP reasons
│   │   ├── score.py              # batch scoring with champion model
│   │   └── actions.py            # risk bands, suppression, control group, action mapping
│   └── utils/
├── api/
│   ├── main.py                   # FastAPI app
│   ├── routes/                   # use_cases, uploads, runs, models, artefacts
│   └── schemas.py
├── ui/
│   ├── index.html                # adapted prototype
│   └── static/
├── data/                         # local artefact store (gitignored)
│   └── runs/<run_id>/...
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/                 # small synthetic CSVs incl. deliberately broken ones
└── docs/
    ├── DECISIONS.md              # architecture decision log
    ├── DATA_CONTRACT.md
    ├── API.md
    └── AWS_DEPLOYMENT.md         # Phase 4 notes
```

---

## 4. Data contract

The upload must be a **flat table with exactly one row per entity** (customer, order, asset). Full details in `docs/DATA_CONTRACT.md`.

### 4.1 Required

- A **primary key** column: unique, non-null. Never used as a feature.
- For training: a **target** column. Binary (two distinct values, any labels such as 0/1, yes/no, true/false, churned/active) or numeric for regression.
- At least `min_rows` rows (default 1,000) and `min_positive` positive examples (default 200) for classification.
- CSV (UTF-8, comma separated, header row) or Parquet. Max 2 GB in Phase 1.

### 4.2 Strongly recommended

- A **snapshot/date column** (any column whose name matches `date|time|month|week|day|_ts$|_at$`, or picked by the user). Required when the use-case config sets `split.type: time_based`.
- Features that describe the entity **as of the snapshot date**, i.e. before the outcome happened.

### 4.3 Templates

For every use case, `templates/<use_case>_template.csv` contains the header row, five example rows, and a second file `<use_case>_template_README.md` describing each column in one line. The UI exposes these as "Download template" next to the upload control. Generate them from the use-case config, do not hand-write them.

### 4.4 Schema memory

When a model is trained, the exact feature schema (column names, inferred types, category levels) is saved with the model (`schema.json`). Scoring validates the new file against it and reports every missing, extra or type-mismatched column by name.

---

## 5. Use-case configuration

Each use case is one YAML file, validated by `engine/config.py`. Example, `configs/use_cases/targeted_advertisement.yaml`:

```yaml
id: targeted-advertisement
name: Targeted Advertisement
description: Scores every customer on how likely they are to convert, so ads go only to the audiences most likely to respond.
lifecycle_stage: Awareness
ai_type: predictive            # predictive | generative | hybrid
problem_type: binary_classification   # binary_classification | regression | forecasting (later) | clustering (later)
entity: customer

target:
  column: converted_30d
  positive_label: 1            # optional; auto-detected if omitted
  definition: Purchased within 30 days of ad exposure
  label_source: Campaign conversion logs

primary_key_hints: [customer_id, cust_id, id]
time_column_hints: [snapshot_date, as_of_date, date]

split:
  type: random_stratified      # random_stratified | time_based
  validation_fraction: 0.15
  test_fraction: 0.15

model_search:
  metric: roc_auc              # roc_auc | pr_auc | f1 | recall | precision | rmse | mae
  strategy: balanced           # fast | balanced | exhaustive  -> maps to AutoGluon presets
  candidates: [XGBoost, LightGBM, RandomForest, LogisticRegression]   # subset of engine-supported names
  time_limit_minutes: 30
  ensemble: true

evaluation:
  calibration: isotonic        # isotonic | platt | none
  reasons_per_row: 3
  champion_min_improvement_pct: 1.0

actions:
  score_field: propensity
  bands:
    - {name: High, min_score: 0.80, action: Serve ad}
    - {name: Medium, min_score: 0.50, action: Retarget}
    - {name: Low, min_score: 0.00, action: Suppress}
  suppression:
    opt_out_column: marketing_opt_in   # rows where false are suppressed; optional
    recently_contacted_column: last_contacted_at
    recently_contacted_days: 14
  control_group_fraction: 0.10

output:
  kpi:
    label: Target audience
    formula: count_where_band_in(["High","Medium"])
```

Rules:

- Every field has a default in `configs/engine.yaml`; a use-case file only overrides what differs.
- **Advanced settings in the UI are exactly the fields of this schema.** The UI sends overrides per run; the engine merges `engine.yaml` → use-case YAML → run overrides and saves the merged result as `run_config.json`.
- Unknown fields fail validation loudly.
- Industry files (`configs/industries/telecom.yaml`) list lifecycle stages in order and the use-case IDs under each. The overview screen is rendered from this file.

---

## 6. The pipeline

Two flows: **train** and **score**. Each is a sequence of stages. Each stage writes its artefact and a `status.json` entry (`pending | running | done | failed`, with start/end time and a human-readable `detail` line, which is what the Running screen shows).

### 6.1 Train flow

```
ingest → validate → prepare → split → train → evaluate → explain → register
```

### 6.2 Score flow

```
ingest → validate_against_schema → prepare(same transforms) → predict → explain_rows → actions → export
```

### 6.3 Stage specifications

#### ingest
- Read CSV (sniff delimiter, handle BOM) or Parquet with pyarrow. Stream, do not load twice.
- Infer per-column: dtype, null rate, distinct count, sample values, min/max for numerics, top categories.
- Detect candidate primary key (unique, non-null, name matches hints), candidate time columns, candidate target (name matches config).
- Write `profile.json` (schema in section 7).

#### validate
Run **all** checks and collect results; do not stop at the first. Severity: `error` blocks the run, `warning` is shown but allowed. Each check has `code`, `severity`, `message` (business language), `suggestion`, and `details` (machine-readable).

| Code | Check | Severity | Message pattern |
|---|---|---|---|
| `PK_MISSING` | Primary key column not selected/found | error | "Choose the column that identifies each customer." |
| `PK_NOT_UNIQUE` | Duplicate keys | error | "This file has {n} rows per {entity} on average. The model needs one row per {entity}." Suggestion: aggregate (Phase 2) or fix upstream. |
| `PK_NULLS` | Null keys | error | |
| `TARGET_MISSING` | Target column absent (train) | error | |
| `TARGET_NOT_BINARY` | More than 2 distinct values for a classification target | error | Offer switch to regression if numeric. |
| `TARGET_CONSTANT` | One value only | error | |
| `TARGET_TOO_FEW_POSITIVES` | Positives < `min_positive` | error | "Only {n} positive examples. At least {min} are needed for a reliable model." |
| `TARGET_IMBALANCE_SEVERE` | Positive rate < 1% or > 99% | warning | |
| `ROWS_TOO_FEW` | Rows < `min_rows` | error | |
| `LEAKAGE_SUSPECTED` | A single column predicts the target with AUC > 0.98 (single-feature model or perfect correlation), or column name matches leakage patterns (`*_date` after target, `churn*`, `converted*`, `outcome*`) | warning, becomes error unless user confirms | "Column '{col}' almost perfectly predicts the target. It may contain the answer. Exclude it?" |
| `TIME_COLUMN_MISSING` | Time-based split requested, no time column | error | |
| `TIME_COLUMN_UNPARSEABLE` | Cannot parse as date | error | |
| `HIGH_NULL_COLUMN` | Column > 60% null | warning | Will be dropped unless kept. |
| `CONSTANT_COLUMN` | One distinct value | warning | Dropped. |
| `HIGH_CARDINALITY_ID_LIKE` | Distinct ≈ rows and not the PK | warning | Treated as ID and dropped. |
| `PII_DETECTED` | Regex/heuristics for emails, phone numbers, names, PAN/Aadhaar-like patterns | warning | Redacted/dropped per settings. |
| `SCHEMA_MISMATCH` | Scoring file vs saved schema | error | List missing/extra/type-changed columns by name. |
| `CONSENT_COLUMN_MISSING` | Consent column configured but absent | error | |

Write `validation.json`. The UI must render every item; the Run button in the API is refused while any `error` exists (`409` with the validation payload).

#### prepare
- Apply exclusions (user-selected, ID-like, constant, high-null, PII-drop).
- Redact PII text columns (replace with `[REDACTED]`) or drop, per settings.
- Missing values: `auto` leaves it to AutoGluon; `fill` uses median/mode; `drop_rows` drops.
- Outliers: clip numerics to 1st–99th percentile when set.
- Deduplicate exact-duplicate rows when set.
- Consent filter: keep rows where consent column is truthy; record how many were removed.
- Record every transform in `prepare.json` so scoring replays them identically (fit-on-train-only for anything statistical).

#### split
- `random_stratified`: stratify on target; `group_column` keeps all rows of a group in one set (use `GroupShuffleSplit`).
- `time_based`: sort by time column; train = oldest, validation = next, test = most recent, using the configured fractions. Record the cut-off dates; the Running screen shows "Training on data before {date}, testing after".
- Write `split.json` with sizes, positive rates per split, cut-off dates.

#### train
- AutoGluon `TabularPredictor(label=target, eval_metric=metric, problem_type=...)`.
- `strategy` → presets: `fast → medium_quality`, `balanced → good_quality`, `exhaustive → best_quality`.
- `candidates` → `hyperparameters` dict restricted to the selected model families (map engine names to AutoGluon keys: `XGBoost→XGB`, `LightGBM→GBM`, `RandomForest→RF`, `LogisticRegression→LR`, `NeuralNet→NN_TORCH`, `CatBoost→CAT`). `ensemble: false` → `num_bag_folds=0, num_stack_levels=0` and `fit_weighted_ensemble=False`.
- `time_limit_minutes` → `time_limit` seconds. `folds` → `num_bag_folds`. `imbalance`: `class_weights` → `sample_weight` balanced; `oversampling` → apply SMOTE to train split only (imbalanced-learn), `auto` → class weights when positive rate < 10%.
- Explicit validation set passed as `tuning_data` (never let AutoGluon re-split).
- Output: `leaderboard.json` (model name, family, validation score, test score, fit time, predict time), `best_model.json`, and the saved predictor directory.

#### evaluate
On the **test split only**:
- Classification: ROC-AUC, PR-AUC, precision, recall, F1 at the chosen threshold; confusion matrix; calibration (fit isotonic/Platt on validation split, apply to test, report Brier score before/after); **decile lift table** (sort by score, 10 bins, actual positive rate and lift vs base rate per bin); a baseline (logistic regression on the same data, trained separately) for the "model vs baseline" table.
- Threshold: `auto` = maximise F1 on validation; `0.5`; or manual.
- Fairness (if a sensitive column is set): positive rate, recall and precision per group, plus max gap. Report only; do not block.
- Regression: RMSE, MAE, R², residual distribution; decile table of actual mean vs predicted mean.
- Write `evaluation.json`, `confusion_matrix.json`, `decile_lift.json`, `baseline.json`, `fairness.json`.

#### explain
- Global: AutoGluon permutation feature importance on test split (`feature_importance.json`, top 20 with normalised share).
- Per-row: TreeSHAP on the best single tree model when available; otherwise KernelSHAP on a 1,000-row sample; otherwise fall back to permutation-based local importance. Store the top `reasons_per_row` reasons as `[{feature, value, contribution, direction}]`. Human-readable form: `"visits_last_7d ↑ (12 visits)"` for positive contributions, `↓` for negative.

#### register
- Create a model version in the registry: `{model_id, use_case, version, created_at, metric, test_score, schema.json, run_config.json, artefact paths, status: candidate}`.
- Champion rule: if no champion exists, promote. Otherwise promote only if `test_score` beats the champion's stored test score by at least `champion_min_improvement_pct` **on the same evaluation metric**. If `approval_required` is set, mark `pending_approval` and expose `POST /models/{id}/approve`.
- Store a drift baseline: per-feature distribution summary of the training data (`drift_baseline.json`, histograms for numerics, frequency tables for categoricals).

#### predict (score flow)
- Load champion (or the model version chosen by the user). Replay `prepare.json` transforms. Predict probabilities (calibrated).
- Compute PSI per feature against `drift_baseline.json`; write `drift.json`; warning if any PSI > threshold.

#### actions
- Assign band by `bands` (first band whose `min_score` ≤ score, bands sorted descending).
- Suppression: opted-out, recently contacted, consent false → `action: Suppressed`, keep the score, record the reason.
- Control group: random `control_group_fraction` of eligible rows → `action: Control (hold out)`, seeded by run ID for reproducibility.
- Output rows: `{primary_key, score, band, action, reasons[], suppressed_reason?}`.

#### export
- Write `scores.csv` and `scores.parquet` plus `scoring_summary.json` (rows scored, per-band counts, per-action counts, suppression counts, drift summary, KPI from config).

---

## 7. Artefact contracts (what the UI renders)

Every run directory `data/runs/<run_id>/` contains, at most:

```
run.json                 # id, use_case, mode, created_at, status, user overrides, links
status.json              # stage list with state + detail lines (drives the Running screen)
profile.json             # ingest output
validation.json          # list of checks
prepare.json             # transforms applied, columns dropped and why, rows removed and why
split.json
leaderboard.json
best_model.json
evaluation.json
confusion_matrix.json
decile_lift.json
baseline.json
fairness.json
feature_importance.json
row_explanations.parquet # per-row reasons (train: test split sample; score: all rows)
drift_baseline.json      # train
drift.json               # score
scoring_summary.json     # score
scores.csv / scores.parquet
model/                   # AutoGluon predictor directory (train)
```

Define every JSON as a pydantic model in `engine/contracts.py` and generate `docs/API.md` from them. The prototype's pages map to these artefacts:

| Prototype page | Artefacts |
|---|---|
| Setup: preview | `profile.json` |
| Running | `status.json` |
| Results summary + pipeline blocks | `run.json`, `best_model.json`, `evaluation.json`, `scoring_summary.json` |
| Data page | `profile.json`, `prepare.json`, `split.json`, use-case `target` block |
| Model page | `run_config.json` (training setup), `evaluation.json` + `baseline.json` (model vs baseline table), `feature_importance.json`, `confusion_matrix.json`, `leaderboard.json` |
| Output page | `decile_lift.json`, `scoring_summary.json`, `drift.json`, `scores.csv` sample rows with reasons |
| Previous runs | list of `run.json` |

---

## 8. API

FastAPI, JSON, all responses typed. Long operations are jobs; the UI polls.

```
GET  /industries                         → industry template (stages + use cases)
GET  /use-cases/{id}                     → merged config (defaults + use case), advanced settings schema
GET  /use-cases/{id}/template.csv        → downloadable template

POST /uploads                            → multipart file; returns {upload_id, profile}   (runs ingest + profile)
GET  /uploads/{id}/profile

POST /runs                               → {use_case, mode: train|score, upload_id, primary_key, target?, model_version_id?, overrides{}}
                                           runs validate synchronously; 409 + validation.json on errors;
                                           otherwise starts the job and returns {run_id}
GET  /runs?use_case=                     → run history
GET  /runs/{id}                          → run.json + status.json
GET  /runs/{id}/artefacts/{name}         → any artefact JSON
GET  /runs/{id}/scores.csv               → download
POST /runs/{id}/cancel

GET  /models?use_case=                   → versions, champion flag
POST /models/{id}/approve                → promote pending_approval → champion
POST /models/{id}/promote                → manual override (records who/why)
```

Validation confirmations (e.g. "yes, exclude the leaky column") are sent as overrides: `overrides.prepare.exclude_columns: [...]` and `overrides.validation.acknowledged: ["LEAKAGE_SUSPECTED:col"]`.

---

## 9. UI

Reuse the prototype (`https://claude.ai/artifact/8ARwky4HnEPQyEfRKa5Svi`) as `ui/index.html`. Keep its look, structure and copy. Replace the simulated behaviour with API calls:

1. **Overview** renders from `GET /industries`.
2. **Use-case page** has three states, exactly as in the prototype: **Setup → Running → Results**.
   - Setup: mode switch; step 1 upload (calls `POST /uploads`, shows profile preview, "Download template" link); step 2 primary key + target (pre-filled from profile detection) + detected problem type with override; step 3 model (AutoML default, or single family); Advanced settings collapsed, eight stages, fields generated from the config schema returned by `GET /use-cases/{id}` so new settings appear without UI changes.
   - Clicking Run calls `POST /runs`. On `409`, render the validation list inline with the confirmation controls (exclude column, acknowledge warning, switch problem type). On success, switch to Running and poll `GET /runs/{id}` every 2 s.
   - Running: render `status.json` stages with their `detail` lines.
   - Results: summary bar, the three pipeline blocks with real values, "Run again / change settings", Previous runs.
3. **Data / Model / Output pages** render from artefacts (table in section 7). Charts (feature importance bars, decile lift, confusion matrix) are inline SVG as in the prototype, fed by real numbers.
4. Show "—" for anything not yet available. Never show sample numbers.

No build tooling required; plain HTML/JS is fine. If the file grows past ~2,000 lines, split into ES modules served statically.

---

## 10. Testing and sample data

- **Synthetic generator** `tests/fixtures/make_data.py` that produces, for each predictive use case, a realistic CSV (10k rows) with a learnable signal, a snapshot date, a marketing opt-in column and a `last_contacted_at` column. Also produce broken variants: duplicate keys, a leaky column, too few positives, constant column, a PII column, a scoring file with a renamed column.
- **Unit tests**: every validation check (positive and negative case), config merging, band assignment, suppression, control group determinism, split correctness (time-based never leaks future rows into train), schema mismatch reporting.
- **Integration tests**: full train flow on the synthetic Targeted Advertisement data with `time_limit_minutes: 1` and `strategy: fast`; assert every artefact exists and validates against its contract; then a score flow on the scoring file; assert `scores.csv` row count equals input rows and every row has a band, action and reasons.
- **Golden checks**: on the synthetic data, test ROC-AUC must exceed the logistic baseline and exceed 0.7 (the generator plants enough signal for this). If this fails, the pipeline is broken, not the data.
- **Public dataset smoke test** (manual, documented in README): Kaggle Telco Customer Churn mapped to a `churn` use case via config only. This is the proof that config-only reuse works.
- CI: `make lint test` must pass; integration tests may be marked slow.

---

## 11. Milestones and definition of done

| # | Milestone | Definition of done |
|---|---|---|
| M1 | Skeleton + configs | Repo layout, `pyproject`, configs load and validate, contracts defined, `make setup` works on a clean machine |
| M2 | Ingest + validate | All checks in section 6 implemented with tests; `POST /uploads` and `POST /runs` return proper 409 payloads on the broken fixtures |
| M3 | Train flow | Full train on synthetic data produces every artefact; leaderboard, evaluation, decile lift, SHAP reasons are real; registry with champion rule |
| M4 | Score flow | Champion scores a new file; schema mismatch reported by column name; drift computed; bands, suppression, control group applied; `scores.csv` downloadable |
| M5 | UI wired | Prototype screens run against the API end to end with no simulated values; Data/Model/Output pages render from artefacts |
| M6 | Config-only reuse | `payment_propensity` and the Telco churn mapping work by adding YAML only; documented in README |
| M7 | Hardening | Cancel, error states, large-file handling (streaming, 1M rows in under the time limit on a laptop), logging, `docs/` complete |

**Overall Phase 1 acceptance test:** a non-technical user downloads the template, fills it with real historical data, uploads it, keeps every default, clicks Run, and receives a scored file with reasons and actions for a second upload, without anyone from the ML team touching it.

---

## 12. Later phases (design for, do not build now)

- **Phase 2, data onboarding:** column mapping UI (their names → standard names), raw-table aggregation (sum/mean/latest per key over windows), saved mappings per client.
- **Phase 3, generative and hybrid:** RAG assistant (Bedrock + OpenSearch), root-cause summaries from top SHAP reasons + complaint text, generated win-back copy with guardrails, evaluation sets; uplift modelling for Win-back using the control group data.
- **Phase 4, AWS and production:** S3 storage, SageMaker Training/Processing for jobs, SageMaker Model Registry, Batch Transform for scoring, Postgres metadata, IAM per tenant, deployment as an accelerator in the customer's account, drift monitoring schedule, retraining triggers, audit log, DPDP controls (retention, consent, deletion).
- **Problem types:** regression and forecasting via AutoGluon; clustering as a separate "segmentation" service.

Keep the `Storage`, `JobRunner`, `ModelRegistry` protocols clean so Phase 4 is implementations, not rewrites.

---

## 13. Working rules for the implementing agent

1. Work milestone by milestone in the order above. Do not start the UI before M4 passes its tests.
2. After each milestone, run `make lint test`, update `README.md` with how to run it, and write a short entry in `docs/DECISIONS.md` for any choice not covered here.
3. Never invent metrics, sample rows or placeholder numbers in code paths that reach the UI.
4. Every user-facing error must come from the validation table or be added to it with a code, message and suggestion.
5. Prefer boring, well-known libraries. Do not add a dependency for something 30 lines of pandas can do.
6. Type-annotate everything in `engine/`. `mypy --strict` must pass there.
7. Log stage timings and row counts at INFO; never log data values.
8. If AutoGluon behaviour differs from what this plan assumes (API names, presets), follow the installed version's documentation and note the difference in `docs/DECISIONS.md`.
9. If a requirement here is impossible or clearly wrong, stop, explain, and propose the smallest change, rather than silently doing something else.

---

## 14. Open decisions (defaults assumed for Phase 1)

| Decision | Assumed default | Revisit when |
|---|---|---|
| Deployment model (SaaS vs. in-customer AWS account) | Design for in-customer account; run locally | Before Phase 4 |
| AutoML engine | AutoGluon | If licensing or SageMaker-native requirements appear |
| Data shape | Prepared one-row-per-entity table + template | Phase 2 |
| First industry | Telecom | When a second client industry is confirmed |
| Champion approval role | Manual `POST /models/{id}/approve`, no auth | Phase 4 |
| Cost tiers for Fast / Balanced / Exhaustive | Not shown to users | Product decision |
