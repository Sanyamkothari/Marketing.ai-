# Marketing AI

> **New here? Run the demo on your laptop: [docs/QUICKSTART.md](docs/QUICKSTART.md).** It needs Python 3.11 and
> about 20 minutes, and no AWS account, AI service or client data. How ready v1 is, and what to
> say out loud when you demo it: [docs/V1_READINESS.md](docs/V1_READINESS.md).

A **reusable marketing AI engine**. A business user selects a use case (e.g. Targeted Advertisement),
uploads a CSV, picks the primary key and target column, and clicks Run. The engine validates the data,
trains the best model with AutoML, evaluates it, explains it, and can later score new data. Results are
shown as a pipeline: **Data → Model → Output**. The engine is generic: each use case is a *configuration
file*, not code. Adding a new use case, or a new industry, means adding config, not features.

Instead of building a separate model per client, we build one engine. Each industry gets a template of
lifecycle stages and use cases. Each client maps their data to a standard format. AutoML trains the best
model on their data. Users can train on historical data or score new data with the approved (champion)
model, and every prediction comes with a reason and a recommended action.

---

## What works today

Everything below is merged on `main` and covered by tests that run in CI (`make lint test`) or
nightly (`make test-all`, which adds the `@slow` AutoGluon and browser journeys).

- **Prepared file → trained model → scored file, in the browser.** Upload a CSV or Parquet file with
  one row per customer, keep every default, click Run: validation, AutoML training, evaluation,
  per-row reasons, the champion rule, then scoring of a second file into `scores.csv` with a band,
  an action and a reason on every row (Phase 1; `tests/integration/test_acceptance.py`).
- **Raw tables → dataset → trained model → next month scored, in the browser.** Setup offers *Build
  from raw tables*: upload a customer master and event tables (bills, complaints, activity), accept
  the suggested roles, mappings and features, keep the default churn definition, and build one row
  per customer per snapshot date. "Use this dataset" fills the run, the run trains on the two-column
  key, and next month's tables are scored through the same saved recipe, with the mapping step
  reopened only for a table whose columns changed (Phase 2 and Plan A M34-M35;
  `tests/integration/test_onboarding_acceptance.py`).
- **Several industries.** Telecom (the default) plus banking, insurance, e-commerce and ad tech, each a
  YAML file under `configs/industries/`, chosen on the overview (Plan A M38).
- **Generative features** on the fake LLM by default and on Bedrock when configured: the onboarding
  assistant, root-cause summaries per risk segment and win-back copy with a judge (Phase 3a).
- **Uplift modelling.** From *Uplift modelling ›* on the overview: train S-, T- or X-learners on a
  past randomised campaign, read the Qini curve and AUUC, get a budgeted treat list that leaves the
  sleeping dogs alone, and measure a campaign's incremental conversions once its outcomes mature
  (Phase 3b; `tests/integration/uplift/`). Since M53 an uplift file may hold each customer at
  several snapshot dates, keyed by customer + snapshot date: treatment must be the same for a
  customer across a campaign's snapshots (`TREATMENT_VARIES_WITHIN_ENTITY`), the hold-out keeps each
  customer on one side, and scoring and campaign results read both key columns
  (`tests/integration/uplift/test_uplift_two_column_keys.py`).
- **Uplift runs on Phase 1's pages, with drift.** An uplift run opened in the Data, Model and Output
  pages shows treatment and control with the randomness check, the Qini curve and AUUC, and the
  segments and treat list — never propensity wording, never a request for an artefact the run does
  not write. Every uplift scoring run writes `uplift_drift.json`: Phase 1's feature PSI against a
  baseline the uplift training run stores, and whether the file's treated share is within
  `uplift.drift_treated_share_tolerance` (0.05) of training's, or honestly `not_applicable` when the
  file has no treatment column. `POST /runs` refuses an uplift *training* run with
  `UPLIFT_REQUIRES_UPLIFT_ROUTE` (M53; `tests/unit/uplift/test_phase1_pages_uplift.py`,
  `tests/unit/uplift/test_uplift_drift.py`).
- **Production controls, off by default.** Sign-in with four roles, an append-only audit trail, DPDP
  consent, retention, erasure and access requests, and schedules with alerts and outcome ingestion,
  all on local backends; with every setting at its default nothing changes (Phase 4b Part 1,
  `docs/PRODUCTION.md`). Since Plan D M54: sign-in is rate limited per account and per address, the
  principal-hash salt is a secret with no default, an Approver decides on a challenger from a
  head-to-head screen and can never approve a model they trained, and erasure runs as a background
  job with per-store progress and retry. The first AWS deployment (Part 2) waits on an account.
- **Leak checking follows ruling R1.** A dataset build runs the narrowed future-data leak check by
  default (DEC-096), the **full** check on a client's first build of a recipe or when
  `full_leak_check: true` is sent, and the build report says which ran (Plan D M55;
  `tests/unit/onboarding/test_leak_check.py`).
- **AWS as a set of implementations:** S3 storage, SageMaker jobs, Postgres metadata, the container
  and the CDK infrastructure, all tested offline (Phase 4a).
- **Guard rails the library found missing:** one PII detector for profiling and preparation,
  contacts inside free text masked wherever a cell is shown, dotted and non-ASCII column names that
  train on every model family, and an automatic threshold that can no longer flag every row
  (Plan A M36).

What is not done yet is listed where it belongs: the Phase 1 items under *What is left*, the build's
full-size timing under *Build performance, measured*, the laptop measurement and the Criteo run
under *Plan D*, and each open request in `docs/CROSS_BRANCH_REQUESTS.md`.

---

## Repository layout

```
marketing-ai/
├── README.md
├── plan.md                       # the Phase 1 plan; normative for everything below
├── PARALLEL_WORK_PROTOCOL.md     # the contract between the phase 2 / 3a / 4a branches
├── pyproject.toml
├── requirements-freeze.txt       # resolver output; the pin test reads it (DEC-018)
├── Makefile                      # make setup / test / lint / run / generate
├── .github/
│   └── workflows/
│       ├── ci.yml                # lint + the fast suite, on every push and pull request
│       └── nightly.yml           # the full suite including @slow, nightly and on demand
├── configs/
│   ├── engine.yaml               # catalog (engine constants) + defaults (advanced-settings defaults)
│   ├── industries/               # one file per industry (DEC-085): lifecycle stages + which use cases appear
│   │   ├── telecom.yaml          #   the default journey the overview opens on
│   │   └── ...                   #   banking, insurance, ecommerce, ad_tech (moved from library/, M38)
│   └── use_cases/
│       ├── targeted_advertisement.yaml
│       ├── payment_propensity.yaml
│       ├── order_fulfillment.yaml
│       ├── fault_prediction.yaml
│       ├── rca.yaml
│       ├── win_back_campaign.yaml
│       ├── telco_churn.yaml      # the public-dataset mapping (M6); YAML only, no engine change
│       └── ...                   # the library's four use cases, moved here in M38
├── templates/                    # generated and committed (DEC-014): <use_case>_template.csv and
│   └── ...                       #   <use_case>_template_README.md for every use case
├── engine/
│   ├── __init__.py               # __version__
│   ├── config.py                 # pydantic models for YAML configs, merge, overrides, advanced settings
│   ├── contracts.py              # pydantic models for every artefact JSON
│   ├── settings.py               # the one env-driven Settings: which backends this process talks to
│   ├── llm.py                    # LLMClient protocol + the one deterministic FakeLLMClient (modes, DEC-098)
│   ├── keys.py                   # the primary key, one column or several: key columns and the row key (DEC-083)
│   ├── pii.py                    # the one PII detector, header and value rules, free-text search (DEC-092, DEC-095)
│   ├── column_names.py           # safe internal names for odd headers, used only inside the model (DEC-093)
│   ├── errors.py                 # the pipeline's exception and the RunError any stage failure becomes
│   ├── templates.py              # renders the CSV/README templates from a use-case config
│   ├── storage.py                # Storage protocol + LocalStorage
│   ├── registry.py               # ModelRegistry protocol + SQLite implementation + champion rule
│   ├── jobs.py                   # JobRunner protocol + ThreadJobRunner + the cancel token
│   ├── pipeline.py               # orchestrates the train and score flows; owns stage order and status.json
│   ├── stages/
│   │   ├── ingest.py             # streaming CSV/Parquet read, schema inference, profile, fingerprint
│   │   ├── validate.py           # every check in plan §6
│   │   ├── prepare.py            # cleaning, exclusions, PII, split; fitted on training rows only (DEC-046)
│   │   ├── train.py              # AutoGluon training + leaderboard
│   │   ├── scorer.py             # the fitted model: calibration and the decision threshold (DEC-043)
│   │   ├── evaluate.py           # metrics, confusion matrix, calibration, decile lift, fairness
│   │   ├── explain.py            # feature importance + per-row SHAP reasons
│   │   ├── register.py           # model versions, champion rule and head-to-head promotion
│   │   ├── score.py              # batch scoring with the champion, schema check and drift
│   │   ├── actions.py            # risk bands, suppression, control group, action mapping
│   │   └── export.py             # scores.csv and the output summary
│   ├── onboarding/               # Phase 2: raw tables -> mappings -> features/labels -> a built dataset
│   ├── generative/               # Phase 3a: the assistant, root-cause summaries and win-back copy
│   └── utils/                    # ids, time, text, logging
├── api/
│   ├── main.py                   # create_app, the module-level app, and the /ui mount
│   ├── deps.py                   # config root, storage, registry and job-runner dependencies
│   ├── routes/                   # industries, use_cases, uploads, runs, models, generative, connection,
│   │                             #   and Phase 2's clients, sources, mappings, datasets
│   └── schemas.py                # response models
├── ui/                           # plain HTML + ES modules, no build step, served at /ui
│   ├── index.html                # the adapted prototype: markup and stylesheet
│   ├── app.js                    # the router
│   ├── api.js                    # every call the screens make
│   ├── dom.js                    # rendering helpers; a value nobody measured renders as an em dash
│   ├── overview.js               # the Overview screen, from GET /industries
│   ├── usecase.js                # the use-case screen: Setup → Running → Results
│   ├── pages.js                  # the Data / Model / Output pages, from a run's artefacts
│   ├── settings.js               # advanced settings, generated from the config schema
│   └── modules/
│       └── router.js             # the phase-module registry; app.js asks it before its own routing
├── scripts/
│   ├── gen_templates.py          # regenerates templates/ (make generate; --check in make lint)
│   ├── gen_api_docs.py           # regenerates docs/API.md (make generate; --check in make lint)
│   └── bench_large_file.py       # times ingest and score on a generated large file
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/                 # make_data.py (synthetic CSVs) + configs/ (deliberately broken YAMLs)
├── docs/
│   ├── DECISIONS.md              # architecture decision log
│   ├── DATA_CONTRACT.md
│   ├── API.md                    # generated from the contracts, routes and configs
│   ├── CROSS_BRANCH_REQUESTS.md  # what a branch needs from outside its own files
│   ├── PERFORMANCE.md            # the dataset build, profiled and measured (Plan A M37)
│   ├── plans/                    # the plans this repository holds, and the ones it does not (M39)
│   └── AWS_DEPLOYMENT.md         # Phase 4 notes
└── data/                         # local artefact store, gitignored, created on first run (runs/<run_id>/...)
```

---

## Getting started

A clean machine needs only **Python 3.11** and **make**. `uv` is used when it is on `PATH`, and
`python -m venv` + `pip` otherwise.

```bash
make setup     # create .venv, install the project with its dev extra, verify AutoGluon imports
make lint      # ruff + black --check + mypy --strict + the generated-file drift check
make test      # the fast test suite (everything not marked @slow)
make run       # serve the API on http://localhost:8000
make generate  # regenerate templates/ and docs/API.md from the configs and contracts
```

`make setup` ends by importing AutoGluon and printing `setup ok: autogluon.tabular 1.6.3`. If AutoGluon
fails to install, setup stops with a non-zero exit rather than falling back to another library.

Optional extras:

```bash
make setup EXTRAS=nn   # installs .[dev,nn] — adds torch so the NeuralNet family can be selected (DEC-011)
```

Other targets: `make test-all` (every test, including `@slow`, but not the `@bedrock` and `@aws`
tests that bill a real account — opt in with `pytest -m bedrock`), `make format` (apply `ruff --fix` and
`black`), `make check-generated` (fail if `templates/` or `docs/API.md` are stale), `make clean`, and
`make help` to list them all.

---

## Running the UI

The UI is the adapted prototype: `ui/index.html` plus seven ES modules, with no build step. A browser
cannot fetch an ES module over `file://`, so the same process that answers the API serves the screens:
`api/main.py` mounts `ui/` at `/ui` (plan §9).

```bash
make run                     # uvicorn api.main:app on :8000
# then open http://localhost:8000/ui
```

Every screen renders from a response the API just gave it. The Overview comes from `GET /industries`,
the Setup form and its advanced settings are generated from `GET /use-cases/{id}`, the Running screen
polls `GET /runs/{id}`, and the Data / Model / Output pages read that run's artefacts. Anything the API
did not send renders as an em dash — the UI never fills a gap with a sample number (plan §13.3).

---

## API endpoints

Every route below is mounted today. `docs/API.md` is generated from the same routes and contracts and
carries the request and response shapes; this table is the index.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/healthz` | `{"status": "ok", "version": "0.1.0"}` (DEC-024) |
| `GET` | `/industries` | every industry journey: lifecycle stages in order, their use-case cards, and the legend |
| `GET` | `/use-cases/{id}` | the merged config, the advanced-settings schema and the Setup-screen copy |
| `GET` | `/use-cases/{id}/template.csv` | the downloadable CSV template for that use case |
| `GET` | `/use-cases/{id}/template_README.md` | the one-line-per-column description of that template (DEC-024) |
| `POST` | `/uploads` | stores a CSV or Parquet file, profiles it, and returns everything the Setup screen renders |
| `GET` | `/uploads/{id}/profile` | the stored dataset profile of one upload |
| `POST` | `/runs` | validates the upload synchronously; `409` + the validation report on errors, otherwise starts the job and returns `{run_id}` |
| `GET` | `/runs` | run history, newest first, filterable by use case and by train/score mode |
| `GET` | `/runs/{id}` | the run record plus the status document the Running screen polls |
| `GET` | `/runs/{id}/artefacts/{name}` | one artefact of that run, whitelisted against the artefact registry |
| `GET` | `/runs/{id}/scores.csv` | the scored rows of a scoring run, as CSV |
| `POST` | `/runs/{id}/cancel` | asks a pending or running run to stop |
| `GET` | `/models` | registered model versions, newest first, with the champion flagged |
| `POST` | `/models/{id}/approve` | approves a version waiting for a human, making it champion; `403 SEPARATION_OF_DUTIES` for the person who started its training run (Plan D, DEC-862) |
| `POST` | `/models/{id}/promote` | makes a version champion by hand, recording who did it and why; the same separation-of-duties refusal |
| `POST` | `/models/{id}/reject` | archives a version waiting for approval, with a required reason (Plan D, DEC-862) |

`GET /use-cases/{id}` accepts optional `columns`, `primary_key` and `target` query parameters so the
column widgets in the advanced settings can be populated from the uploaded file.

Validation confirmations ("yes, exclude the leaky column") are sent back as overrides on `POST /runs`:
`overrides.prepare.exclude_columns` and `overrides.validation.acknowledged` (plan §8).

---

## Large-file handling

`scripts/bench_large_file.py` generates a synthetic targeted-advertisement scoring file, reads and
profiles it, then runs the whole score flow (plan §6.2) over the same rows with a trained champion.
Every number below was printed by that script. Nothing here is projected, and plan §13.3 is why:
until this was run, "it streams" was an argument about the code rather than a result.

Two independent runs, so the figures are a measured range rather than one sample:

| stage | rows | seconds | rows/sec |
| --- | --- | --- | --- |
| ingest (read + profile, whole-file fingerprint) | 1,000,000 | 68.5 / 68.7 | 14,590 / 14,564 |
| score (full score flow, a reason for every row) | 1,000,000 | 245.3 / 250.3 | 4,077 / 3,995 |

57.8 MB CSV. Peak resident memory 6,160 MB and 6,116 MB (`resource.getrusage(RUSAGE_SELF).ru_maxrss`,
a whole-process high-water mark). The two runs agree to within 2%. The score flow re-reads the upload
as its own first stage, so about 66 s of the score number is a second ingest and the two rows are
**not** additive. Generating the file (12.0 / 12.3 s) and training the champion on 4,000 rows
(39.6 / 37.6 s, `time_limit_minutes: 1`, `strategy: fast`, no ensemble) are setup and are not in
the table.

Where the score time goes, from the first run's stage log (the second agrees to within 3 s
on every stage):

| stage | seconds |
| --- | --- |
| ingest | 66.1 |
| validate_against_schema | 28.9 |
| prepare (replay) | 0.001 |
| predict | 1.6 |
| **explain_rows** | **132.7** |
| actions | 3.9 |
| export | 10.2 |

**Explaining the rows is the cost, not reading them.** Streaming ingest moves a million rows in
about a minute; producing the per-row reason plan §6.3 requires for every exported row takes twice
that. A deployment that wants a million rows scored faster should look there first — the tier that
runs is reported in `status.json` and on the Running screen, so it is visible per run.

A control point at 250,000 rows on the same machine — ingest 16.1 s (15,531 rows/sec), score 65.8 s
(3,798 rows/sec), peak 1,705 MB — puts both paths at roughly linear scaling up to a million.

### Parquet against CSV, measured

A later run of the same script with `--format both` builds one frame, writes it to both formats and
reads each back, so the two ingests cover identical rows, back to back, in one process on one machine:

| ingest (read + profile, whole-file fingerprint) | rows | seconds | rows/sec | file on disk |
| --- | --- | --- | --- | --- |
| Parquet | 1,000,000 | 67.1 | 14,913 | 9.8 MB |
| CSV | 1,000,000 | 67.7 | 14,774 | 57.8 MB |

Parquet costs **0.99× the seconds** off a file **0.17× the size**. Streaming ingest is not where the
two formats differ. The engine's own stage log says why: `profile_dataset` took 31.6 s on the CSV
frame and 32.6 s on the Parquet frame, which leaves the read half at about 36.1 s CSV against 34.5 s
Parquet — Parquet reads roughly 4% faster, and the format-independent profiling and whole-file
fingerprint swamp that. The Parquet file is the one `pandas.to_parquet` writes with pyarrow defaults
(snappy, default row-group size), not one tuned for fast reads.

Two things from that run are **not** results about the formats, and are recorded here so they are not
read as if they were. The scoring stages took 896.7 s (Parquet) against 930.1 s (CSV) — about 3.8×
the 245 s the table above records for the same stage — because the box was saturated while they ran
(load average 4.03 on 4 CPUs). That is contention, not format. The ingest pair is the trustworthy half
of the run: the two ingests are adjacent, they agree to within 1%, and both land within 2% of the
68.5 / 68.7 s already recorded for CSV on a quiet machine. Peak resident memory was 6,313 MB for a
process that ran both paths, so it is one high-water mark belonging to neither format alone; a
per-format peak needs a run per format.

One property of the formats, not of the benchmark: Parquet carries its own types, while the CSV frame
is inferred from text, so an empty CSV field arrives as null where an empty Parquet string stays an
empty string. Both files scored end to end without error, but the two paths do not produce identical
profiles.

Two honest caveats over everything above. It was measured on 4 CPUs, 15.7 GiB RAM, Python 3.11.15,
Linux — **a container, not the laptop plan §11 names**. And the machine has to be quiet: a third
attempt at a million rows was killed at a 45-minute cap while several other jobs held the same four
cores, and the `--format both` scoring figures are the same effect caught in the act, so on a busy box
expect far worse than the tables.

### Re-measured after Plan A (M37's open item)

Plan A M37 asks for this benchmark to be run again and recorded. On 2026-09-23 it ran on the same
container (4 CPUs, 15.7 GiB, a quiet machine: load average under 1 at the start of each run), first
with Plan A and then with the code before it (`main` @ `d37fc72`), back to back:

| 1,000,000 rows, CSV | ingest | score (full flow) | of which per-row reasons | peak RSS |
| --- | --- | --- | --- | --- |
| before Plan A | 57.0 s | 322.8 s | 210.7 s | 6,174 MB |
| after Plan A | 55.1 s | 275.5 s | 166.6 s | 6,276 MB |
| after Plan A, Parquet | 54.0 s | 267.2 s | 150.1 s | (same process as the CSV row) |

Plan A made neither path slower. The score figures differ from each other, and from the 245 s in the
first table, mostly because of **which model each run trains**: every run fits its own champion on
4,000 rows under a one-minute limit, and the per-row reasons cost what explaining that model costs.
Read the score column as a range, not a constant. The ingest figures agree within 4% of each other and
are faster than the first table's 68.5 s. That comparison is across days, so it is not attributed to
any one change. This is still a container, not the laptop plan §11 names.

Re-run it with:

```bash
.venv/bin/python -m scripts.bench_large_file --rows 1000000                # CSV, the first table
.venv/bin/python -m scripts.bench_large_file --rows 1000000 --format both  # adds the Parquet half
```

---

## Milestone status

| # | Milestone | Definition of done | Status |
|---|---|---|---|
| M1 | Skeleton + configs | Repo layout, `pyproject`, configs load and validate, contracts defined, `make setup` works on a clean machine | **done** |
| M2 | Ingest + validate | All validation checks implemented with tests; `POST /uploads` and `POST /runs` return proper 409 payloads on the broken fixtures | **done** |
| M3 | Train flow | Full train on synthetic data produces every artefact; leaderboard, evaluation, decile lift, SHAP reasons are real; registry with champion rule | **done** — and reached from the product since DEC-081; the browser acceptance test below trains through the screens |
| M4 | Score flow | Champion scores a new file; schema mismatch reported by column name; drift computed; bands, suppression, control group applied; `scores.csv` downloadable | **done** |
| M5 | UI wired | Prototype screens run against the API end to end with no simulated values; Data/Model/Output pages render from artefacts | **done** — every page renders from real artefacts with nothing simulated, and the browser acceptance test below walks the whole journey |
| M6 | Config-only reuse | `payment_propensity` and the Telco churn mapping work by adding YAML only; documented in README | **done** — both are config only and tested; the end-to-end run on the downloaded Kaggle file stays a manual step by design (plan §10) |
| M7 | Hardening | Cancel, error states, large-file handling (streaming, 1M rows in under the time limit on a laptop), logging, `docs/` complete | **partial** — see below |

**Plan §11's overall Phase 1 acceptance test passes.** That criterion is a sentence about a
person rather than an API call — a non-technical user "uploads it, keeps every default, clicks Run,
and receives a scored file with reasons and actions for a second upload" — and until now nothing in
the suite performed it. `tests/integration/test_acceptance.py` now does: it starts the real app under
uvicorn, opens `/ui` in Chromium and walks Overview → Targeted Advertisement → the Setup screen's own
template link → the file input → Run → the Running screen → Results → the Data / Model / Output pages
→ "Score new data" → the `scores.csv` download, clicking only what a user would click; Advanced
settings is asserted closed, so every default really is kept (DEC-075).

**Running it for the first time found that the product could not train a model** — and that is worth
recording, because the suite was green over it. Two defects, neither in `engine/`, stood between the
screens and that sentence, and both are fixed (DEC-081):

- `api/routes/runs.py` chose its job with `if version is None`, and a version is only ever resolved on
  the *score* path — so every **training** run got DEC-060's M2 placeholder and stopped at `prepare`
  with *"Preparing features is not built yet."* `Pipeline.run_train` had worked since M3 and was never
  reached from anything a user could click. The route now dispatches on `body.mode`.
- `ui/api.js`'s `postUpload` sent the file and the use case but never `mode`, so `POST /uploads` took
  its default of `train` and a "Score new data" upload was then refused by `POST /runs` with
  `UPLOAD_MODE_MISMATCH` — advice the user could not act on, because the same screen repeats the same
  upload. It now sends the mode the screen already holds.

The suite had been holding the first one in place: `test_m2_job_runs_two_stages_then_fails_honestly_at
_prepare` asserted the failure, so thousands of green tests said nothing about whether a user could
train. It is replaced by `test_a_training_run_submits_the_train_flow`, which asserts the builder the
route *submits* — the assertion whose absence let this ship.

The 13 cases share one module-scoped journey fixture, so the walk is paid for once. It is marked
`@slow` and skips cleanly where playwright or a browser is missing, so `ci.yml` stays green on a
machine with no browser and `nightly.yml` is where it actually runs.

M7 in detail. Done:

- **Cancel.** `POST /runs/{id}/cancel`; `engine/jobs.py` carries a cancel token the pipeline checks
  between steps, and a job that has not started yet is cancelled outright.
- **Error states.** Every stage raises a coded exception; `engine/errors.py` turns any failure into the
  `RunError` that `status.json` and `run.json` carry, and the UI renders failed and cancelled states
  rather than stalling on a spinner.
- **Streaming ingest.** `engine/stages/ingest.py` pulls a CSV through `read_csv(chunksize=…)` and a
  Parquet file through `iter_batches`, folding every row into the content fingerprint as it goes, so
  memory stays flat in the number of rows.
- **The logging audit.** `tests/unit/test_logging_audit.py` drives every stage that logs over a frame of
  sentinel values and fails if a single one reaches a log record — plan §13.7 enforced mechanically
  rather than by inspection.
- **`docs/` complete.** `DECISIONS.md`, `DATA_CONTRACT.md`, the generated `API.md`, and `AWS_DEPLOYMENT.md`.
- **CI.** `.github/workflows/` (below), which plan §10 asks for.

- **Large-file handling, measured.** `scripts/bench_large_file.py` was run at 1,000,000 rows, in CSV
  and now in Parquet, and every number above was printed by it rather than projected. One qualification
  keeps M7 short of done on this line: it was measured on a 4-CPU container rather than the laptop
  plan §11 names.

### What is left, and whose phase it belongs to

DEC-074 parked nine settings that the form records and no stage reads. Listing them as one block of
"not done" implied they were all Phase 1 debt. Checked against plan §12, which names the later phases,
they are not the same kind of thing at all.

**Phase 1 items still open, and one now settled:**

- **The `features` block** — `auto_feature_engineering`, `categorical_encoding`, `numeric_scaling`,
  `text_columns`, `selection` and `max_features`, six of DEC-074's nine. `plan.md` never asked for them,
  and whether they should exist is now settled by Plan A ruling D2 (DEC-084, DEC-098): they stay in
  the schema, shown disabled as "Coming later — recorded with the run, not yet applied", the screen
  never sends them, and they are left out of the recipe hash, so two runs that differ only in them
  share a recipe. Shipping one means removing its path from `engine.config.ADVISORY_PATHS`.
- **The laptop.** Plan §11 asks for a million rows under the time limit *on a laptop*, and the numbers
  above were measured on a 4-CPU container, most recently after Plan A (*Re-measured after Plan A*,
  below the large-file tables). The format gap is closed; the machine gap is not, because no laptop
  is available to this repository's runs. Since Plan D M57, `python -m scripts.bench_1m --kind laptop
  --label "<laptop>" --record` measures and records it in `docs/PERFORMANCE.md` (DEC-866); running it on a laptop is an
  owner action, and no laptop row has been recorded.

**Parked for Phase 4 by plan §12, not Phase 1 debt** — §12 defers "drift monitoring schedule,
retraining triggers … DPDP controls (retention, consent, deletion)" by name. *Since Phase 4b, two of
these three have shipped:* `governance.retention_days` (with erasure and consent) and
`monitoring.performance_alert_drop_pct` (with schedules and alerts) are live, and only
`monitoring.retraining` stays parked on the form. The Phase 4b section below is current; the two
bullets that follow record the state before it:

- **`governance.retention_days`** and **deletion**. Retention is recorded with the run and not enforced;
  deletion has no seam at all (`docs/AWS_DEPLOYMENT.md` §7). The seam is worth calling out because §12
  closes by asking that Phase 4 be implementations behind the `Storage` / `JobRunner` / `ModelRegistry`
  protocols rather than rewrites, and a per-entity deletion path reaching across the upload, the run
  artefacts and the metadata row is not yet one of those. The consent *filter* plan §6 asks for is built
  and shipped; it is the data-protection control of the same name that §12 defers.
- **`monitoring.retraining`** and **`monitoring.performance_alert_drop_pct`**. Per-run drift is Phase 1
  and shipped — PSI against `drift_baseline.json`, written to `drift.json`, warned on above the
  threshold. What these two settings would need is monitoring *between* runs, a schedule and an alert,
  which is exactly the half §12 names.

Those three settings being disabled and recorded was the correct state for work the plan defers, not a
shortfall against Phase 1, until Phase 4b built two of them. Each is stated where a reader meets it rather than left to be discovered.

---

## Continuous integration

Two workflows, both installing the project exactly the way a developer does (`make setup`) so that CI
cannot pass or fail for reasons the Makefile does not know about:

- **`.github/workflows/ci.yml`** — lint plus the fast suite (`make lint test`) on every push and pull
  request. `make lint` chains the generated-file drift check, ruff, `black --check` and `mypy --strict`,
  so a stale `templates/` or `docs/API.md` fails the pull request. The `@slow` AutoGluon tests are
  deliberately excluded to keep the wait in minutes.
- **`.github/workflows/nightly.yml`** — the full suite including `@slow` (`make test-all`), on a nightly
  schedule and on demand via `workflow_dispatch`. This is what keeps "may be marked slow" (plan §10)
  from becoming "never run": the integration train and score flows and the golden checks run here.
  The scheduled run checks out `main` by name; a manual run tests the branch it was started from.
  **Owner action required:** GitHub fires a `schedule` only on the repository's *default* branch,
  which is currently `claude/gracious-noether-y0njma`, not `main`, so until the default branch is
  switched (Settings → General → Default branch) the nightly runs only when started by hand.
- **The library, on request.** A separate nightly job runs `make library-test` (`library/tests`: five
  small trainings on committed samples, no network) when the repository variable
  `NIGHTLY_LIBRARY_TESTS` is `true`, or when a manual run ticks "library".
- **The README is checked against the tests.** `ci.yml` runs `make check-readme` after `make test`:
  `scripts/check_readme.py` reads that run's JUnit report and fails when a milestone table marks a
  milestone pending (or not started, todo, planned) while every mapped test that ran passed. The
  mapping is `MILESTONE_TESTS` in the script; a milestone adds its own tests there when it merges
  (DEC-099). Locally, `make check-readme` runs only the pending milestones' tests, and
  `make check-readme JUNIT=report.xml` reads an existing report.
- **The jsdom and Postgres tests run, not skip (Plan D M56).** `ci.yml`'s `lint-test` job sets up
  node 22, runs `npm ci` in every `tests/` directory with a lockfile, runs `make prototype-test`, and
  sets `REQUIRE_JSDOM=1`, so a missing node or jsdom fails the job (DEC-877). The Postgres tests run
  in `make test` against the job's Postgres service under `MARKETING_AI_REQUIRE_POSTGRES=1`
  (DEC-879). After `make check-readme`, `scripts/check_no_skips.py` reads the same JUnit report and
  fails if a must-run node, jsdom or Postgres test was skipped or did not run (DEC-878).

---

## Public dataset smoke test

Plan section 10 asks for one public dataset mapped onto the engine **by configuration only**, as the
proof that config-only reuse is real rather than aspirational. That dataset is the **Kaggle Telco
Customer Churn** file, and the mapping is [`configs/use_cases/telco_churn.yaml`](configs/use_cases/telco_churn.yaml).
Nothing under `engine/` or `api/` was added, changed or branched for it.

[`docs/LIBRARY.md`](docs/LIBRARY.md) is the public dataset library: six public datasets across five
industries, run through the engine on configuration alone, with what each run actually scored and
what needed a code change. Its industries and use cases live in `configs/` since Plan A M38.

### Where to get the data

Download **Telco Customer Churn** (BlastChar's copy of the IBM sample) from
<https://www.kaggle.com/datasets/blastchar/telco-customer-churn>. The download is a single file,
`WA_Fn-UseC_-Telco-Customer-Churn.csv`: 7,043 subscribers, 21 columns, one row per subscriber. It is
deliberately **not** committed here and no test downloads it — `tests/unit/test_config_only_reuse.py`
synthesises a frame with the same header, value vocabularies and dtypes instead.

### What to map

Nothing. Do not rename a column, derive a flag or pre-clean the file — that is the point of the
exercise. The YAML names the columns the file already has:

| The file | The config | Why |
|---|---|---|
| `customerID` | `template` role `primary_key`, and `primary_key_hints` | identifies the row, never a feature |
| `Churn`, holding `Yes` / `No` | `target.column`, with `target.positive_label: "Yes"` | plan section 4.1: a binary target may carry any two labels, not only `0`/`1` |
| the other 19 columns | `template` role `feature` | `SeniorCitizen` and `tenure` are whole numbers, `MonthlyCharges` a decimal, the rest categorical text |
| *(no date column)* | `split.type` stays `random_stratified` | there is nothing to order the rows by, so a time-based split is not offered |
| *(no consent or contact column)* | `actions.suppression.opt_out_column` and `recently_contacted_column` set to `null` | the public file carries no marketing consent or contact history; the suppression switches stay on and simply have nothing to act on |

`TotalCharges` is blank for a subscriber whose `tenure` is 0, so a plain `read_csv` reads it as text;
the engine's own ingest parses it to a number and leaves those cells missing.

The template for this mapping is generated from the YAML like every other one — never hand-written:

```bash
make generate     # or: python -m scripts.gen_templates
```

It lands in [`templates/telco_churn_template.csv`](templates/telco_churn_template.csv) and its
`_README.md`. The example rows in it show the file's format; they are not rows of the Kaggle file.

### What to expect

The automated half of the smoke test runs in a second and needs no download:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/unit/test_config_only_reuse.py -q
```

It builds a frame with the dataset's exact columns and dtypes, loads the use case through the real
config loader, and puts it through the real validate stage. Expect:

- **no validation errors**, so `POST /runs` would accept the upload rather than refuse it with a 409;
- **one warning**: `PII_DETECTED` on `gender`. The PII name detector reads `Female` / `Male` as personal
  names. It is a warning, it is acknowledgeable, and `engine/stages/prepare.py` does not redact the
  column, so the run is unaffected. It is a known false positive on this dataset, pinned by the test so
  that any *second* warning has to be explained;
- **19 features**, with no column dropped and none redacted, and a stratified train/validation/test
  split in which all three parts carry both `Yes` and `No`.

The manual half is the end-to-end run: download the file, open **Telecom → Churn → Telco Customer
Churn**, upload the CSV, pick `customerID` and `Churn`, keep every default, and Run. What comes back is
the leaderboard, the evaluation, the decile lift table and the comparison against the logistic baseline
the engine trains alongside the model — all of them computed from that run. **No score from this
dataset is quoted here, and none should be written down anywhere in the product until a real run has
produced it** (plan section 13.3). The bar the smoke test sets is that the run completes on config
alone and beats its own baseline, not that it reaches a particular number.

M6's other half, `payment_propensity`, already works the same way; the test asserts that neither use
case is named anywhere under `engine/` or `api/`.

---

## Decisions

Every choice that `plan.md` does not make is recorded in [`docs/DECISIONS.md`](docs/DECISIONS.md) as a
`DEC-` entry with its context, decision and consequences. The trunk's own entries run DEC-001 … DEC-082:
M1 opened the log with DEC-001 … DEC-040, each milestone since has appended its own, and DEC-075 …
DEC-079 are the shared surface the phase branches build on. DEC-059, DEC-064 and DEC-071 are unused — no
code cites them. From DEC-100 up, numbers are allocated a hundred per workstream — Phase 2 100…, Phase 3a
200…, Phase 4a 300… (exhausted), the dataset library 400… — and the table in `PARALLEL_WORK_PROTOCOL.md`
§4 is where the next phase claims DEC-500 up before its first entry. Entries are never rewritten in place — a
decision that is reversed gets a new entry naming the one it supersedes (plan §13.2). See also
[`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) for the shape of the upload and
[`docs/AWS_DEPLOYMENT.md`](docs/AWS_DEPLOYMENT.md) for the Phase 4 notes.

---

## Working in parallel: phases 2, 3a and 4a

Three branches build on this one at the same time. `PARALLEL_WORK_PROTOCOL.md` is the contract
between them — branch order, who owns which file, and what a shared file's marker blocks mean —
and `docs/CROSS_BRANCH_REQUESTS.md` is where a branch writes down what it needs from outside its
own files.

**Shared surface added before the branches started.** `engine/settings.py` is the one object that
says what this process talks to (DEC-075); `engine/llm.py` is the `LLMClient` protocol and its
deterministic fake (DEC-076); `primary_key` accepts several columns on every contract it travels
through (DEC-077); `RunRequest` and `RunManifest` carry `dataset_id` and `client_id` (DEC-078);
`RunManifest` carries `llm_usage` and `compute`. Twelve shared files have marker blocks and
`tests/unit/test_shared_file_markers.py` fails when one goes missing (DEC-079).

**If you are working on a phase branch:** read the protocol first, work only inside your own
block in a shared file, and run `make lint test` before every commit and `make test-all` before
every rebase.

<!-- =======================================================================
     Shared file (PARALLEL_WORK_PROTOCOL.md §4). Append a section per
     milestone under your own phase heading, inside your own block.
     ======================================================================= -->

<!-- ---- PHASE-2 (onboarding) — append only below this line ---- -->

### Phase 2 — Data onboarding

Phase 2 turns a client's *raw* tables — a customer master, a billing table with one row per invoice,
a complaints table with one row per ticket, and usually no target column at all — into the
one-row-per-entity dataset Phase 1 already knows how to train and score on.

| # | Milestone | Definition of done | Status |
|---|---|---|---|
| — | Onboarding contracts | `configs/roles.yaml`, the spec vocabulary, `engine/onboarding/specs.py`, the `onboarding` defaults block; Phase 1 suites green and unedited | **done** |
| M8 | Clients, sources, roles, standard schema | `ClientStore`; source upload with profiling and role detection; `standard_schema` for the predictive use cases; `GET /use-cases/{id}/standard-schema` | **done** |
| M9 | Mapping | Heuristic suggester, transforms, value maps, mapping checks; an entity-only source maps to a Phase 1-shaped table | **done** |
| M10 | Aggregation engine | `FeatureSpec` → DuckDB; the function library; the golden and point-in-time tests; `features.sql` written | **done** |
| M11 | Labels, snapshots, composite keys | All four label types; censoring; periodic snapshots; the stages taught composite keys | **done** — the stages carry the composite key since Plan A M34 (DEC-083) |
| M12 | Datasets, lineage, run integration | `DatasetRegistry`, the build job, `POST /runs` with `dataset_id`, build-then-score | **done** |
| M13 | UI | The four-step onboarding panel inside Setup, the client selector, preview, lineage | **done** — mounted in Setup by Plan A M35 (DEC-090) |
| M14 | Hardening and docs | Build benchmark, cancel and error states, `docs/ONBOARDING.md` | **done** — benchmark recorded below; the build was sped up in Plan A M37 |

#### Build performance, measured

`scripts/bench_onboarding.py` generates the raw tables and times a real `build_dataset` — no stage
stubbed, every onboarding and Phase 1 check run, `dataset.parquet` on disk at the end. At the plan's
own target, on **4 CPUs · 15.7 GiB RAM · Python 3.11.15 · Linux** (a container, not a laptop), three
builds of the same tables, back to back:

| | |
|---|---|
| Input | 200,000 customers · 5,000,000 usage rows · 6 CSVs · 1,377 MB (37.7M event rows) |
| Output | 2,400,000 rows · 200,000 entities · 12 snapshots · 60 features (3 dropped, all-null) |
| Before Plan A M37 | 763.5 s · peak RSS 10,863 MB (the M14 run, on other tables: 828.9 s) |
| After M37 | **275.9 s** · peak RSS 8,300 MB, plus about 1,000 MB in three render workers |
| Target | the same shape in under 300 s |
| Verdict | **met** — 2.8× faster, the same dataset (identical cells; floats within 4 × 10⁻¹⁶) |

M37 profiled first (`docs/PERFORMANCE.md`). The plan expected Parquet writing and column renaming
to dominate; they did not (writing was under 1% of the build). The build did work twice: the dataset
fingerprinted twice, every source read and fingerprinted twice, and the leak probe re-ran a full
aggregation. Removing that brought it to 369.1 s (DEC-096, DEC-097). The rest was the fingerprint
canonicaliser rendering every cell on one core, which now renders chunks in worker processes and
hashes them in order, so every fingerprint is byte-identical (DEC-097's addendum). The before/after
pairs at one tenth of the size, and the profiles, are in `docs/PERFORMANCE.md` §1–§5.

Two defects were found by running this and could not have been found any other way. The build was
reading only the first 2,000,000 rows of each source, having inherited the *profiling* row cap; and
the leak probe compared floats exactly, so DuckDB's parallel summation order made every float
feature look like it had moved and raised `FUTURE_EVENTS_LEAKED` — the one finding a user may never
acknowledge. Both are fixed, both have regression tests, and both are described in
`docs/DECISIONS.md`.

The contracts landed first, as `PARALLEL_WORK_PROTOCOL.md` §2 asks: `configs/roles.yaml` (what an
uploaded table *is*), the spec vocabulary in the PHASE-2 block of `engine/config.py`, the artefact
models in `engine/onboarding/specs.py`, and the `onboarding` defaults in `engine.yaml`. They fill
the blocker the contracts-first task recorded against `engine/onboarding/specs.py`, which was empty
because the Phase 2 plan was not in the repository when that task ran. Nothing executes them yet.

Two rules in this package are absolute and are worth knowing before reading it. **Point-in-time
correctness:** a feature for snapshot date *T* may read only events at or before *T*, a label only
events after it; `FUTURE_EVENTS_LEAKED` is a bug, and `OnboardingCheck` refuses to mark it
acknowledgeable. **Suggest, never decide silently:** roles, mappings and features are proposed with
a confidence and confirmed by the user, and an auto-accepted item is still shown and still
reversible.

#### The reference prototype

**The reference prototype is in the repository.** `marketing-ai-prototype.html` is the design
source of truth named in `plan.md` §9 — one HTML file, hash routing, dark mode, mobile layout,
no build step. It now carries the Phase 2 §10 screens the build agents copy: a client selector
in the header, Setup step 1 as a choice between a prepared file and raw tables, the four-step
onboarding panel behind it (sources → mapping → features & label → build & review), score mode
with a saved recipe, and the lineage block on the Data page. It also carries the Phase 3a §9
screens (see below). `CHANGELOG-prototype.md` lists every screen with the plan section it
implements and the illustrative numbers they share.

```bash
open marketing-ai-prototype.html   # no build step, no server needed
make prototype-test                # 89 jsdom tests in tests/prototype/ (npm ci; in CI since Plan D M56)
make prototype-screenshots         # docs/prototype/*.png, desktop and mobile
```

`tests/prototype/existing.test.mjs` pins the screens that must not move: the lifecycle
overview, the seven use-case definitions, the eight advanced-settings stages, the three run
states and the colour tokens. Screenshots of every new state, desktop (1440px) and mobile
(390px), are in `docs/prototype/`.

### Plan A — Phase 2 completion and integration hardening

[`docs/plans/MARKETING_AI_PLAN_A_PHASE2B.md`](docs/plans/MARKETING_AI_PLAN_A_PHASE2B.md) finishes what
the 23 September status report left open, so the product works end to end *through the UI* on raw
tables, and closes the engine issues the dataset library found. Its seven rulings are DEC-083…089;
what building them found is DEC-090…099.

| # | Milestone | Definition of done | Status |
|---|---|---|---|
| M34 | Two-column keys through every stage | A periodic dataset trains and scores on `(entity_key, snapshot_date)`; no entity in two splits; control and suppression per entity; `scores.csv` round-trips the client's ids | **done** — DEC-083 |
| M35 | Onboarding wired into Setup | Setup step 1 builds from raw tables; client picker; "Use this dataset"; next month's tables replayed through the saved recipe; lineage on the Data page; the browser journey | **done** — DEC-090, DEC-091 |
| M36 | Engine issues the library found | One PII detector; odd column names; `threshold.mode: auto` under a flagged-rate ceiling; free-text PII | **done** — DEC-092…095 |
| M37 | Dataset build speed | 200k customers, 5M events, 12 snapshots, 60 features in ≤ 300 s; profile recorded | **done** — 275.9 s at the target size, from 763.5 s on the same tables and machine; DEC-096, DEC-097 |
| M38 | Configuration and settings cleanup | Several industries; inactive settings "Coming later"; the `train.py` line; one fake LLM client, one check type | **done** — DEC-098 |
| M39 | Docs, CI and housekeeping | README current; plans under `docs/plans/`; nightly on `main`; the library job; the README check | **done** — DEC-099; the nightly waits on the default-branch switch above |

**What changed for a user.** Setup step 1 offers *Upload a prepared file* or *Build from raw tables*.
The second mounts the four-step onboarding panel inline; the header carries a client picker that
starts on "Demo". "Use this dataset" fills step 2 with both key columns, the label, the problem type
and a time-based split on `snapshot_date`, and Run trains on the dataset. In score mode the card
reads "Upload this month's tables": the new files are matched to the recipe's, each mapping is
replayed, and only a table that lost a column reopens its mapping step. The Data page of a dataset
run shows where it came from: sources → mapping → recipe → dataset → run.

**Things a reader should know.**

- *Keys.* A single-column key is byte-identical to before: `run.json`, the recipe and its hash do
  not change. A composite key splits by entity, or on the snapshot date for a time-based use case.
  The generative win-back and root-cause modules still take a single key and refuse a composite run
  by name.
- *PII.* A header that names personal data (email, phone, name, pan, aadhaar, address, ssn,
  passport) is now enough on its own to redact a column and report `PII_DETECTED`, as preparation
  already did; contacts typed inside free text are masked in every sample, preview, reason and LLM
  input, and reported as the warning `PII_IN_FREE_TEXT`. A date column named like a time hint is kept
  with the rows and never trained on (`carried_columns` in `prepare.json`).
- *Thresholds.* `threshold.mode: auto` maximises the use case's metric on validation but refuses an
  optimum that flags more than `evaluation.threshold.max_flagged_rate` (0.30) of the rows, falling
  back to the top decile and saying so on the Model page.
- *Monthly scoring.* `onboarding.limits.max_sources` counts the tables no saved recipe reads, so a
  client can keep scoring month after month.
- *The acceptance journey* uploads four tables, not the plan's three: the default churn label reads
  the activity table.

<!-- ---- END PHASE-2 ---- -->

<!-- ---- PHASE-3A (generative) — append only below this line ---- -->

### Phase 3a — Generative and hybrid

**The prototype carries the Phase 3a §9 screens.** In `marketing-ai-prototype.html`: the AI
Onboarding Assistant's document upload, optional reference questions, refusal message,
index-build running screen and results (index summary, pass rate against its threshold, the ten
weakest answers, a Try it panel whose citations expand to the quoted chunk, a cost line); RCA's
root causes per risk segment; and win-back's campaign copy with judge scores, block reasons and
approve / regenerate. `CHANGELOG-prototype.md` maps each to its plan section; the file and its
tests are listed under Phase 2 above.

<!-- ---- END PHASE-3A ---- -->

<!-- ---- PHASE-4A (aws) — append only below this line ---- -->

### Phase 4a — AWS and production

**What it is.** Phase 1 put storage, jobs and the model registry behind protocols so that AWS would
be a set of new *implementations* rather than a rewrite (plan §12). Phase 4a writes those
implementations, packages the product in a container, and describes the infrastructure that runs it
in a customer's own AWS account in `ap-south-1`.

Three rules shaped every line of it, and each is enforced by something mechanical rather than by
anybody remembering:

* **No protocol changed.** `Storage`, `JobRunner` and `ModelRegistry` have the same members they had
  in Phase 1, and the same four test fakes still satisfy them. What a remote backend needs that a
  local one does not is an *additive capability protocol* — `SupportsLocalMirror`,
  `SupportsPresignedDownload`, `ReconcilingJobRunner` — reached through a free function that no-ops
  for a store or runner that does not implement it.
* **Local behaviour does not change.** Every `Settings` default reproduces Phase 1 exactly: the
  local filesystem, SQLite, a two-worker thread pool, the same directory, the same two environment
  variable names. `tests/unit/test_settings.py` asserts each default, so a later edit cannot drift
  them.
* **The same suite runs against AWS with only `Settings` changed.** `tests/unit/test_storage_contract.py`
  is one conformance suite parametrised over `LocalStorage` and `S3Storage`-on-moto; the registry and
  metadata suites work the same way. A backend that passes is a backend that behaves like the other
  one.

**Where to read about it.**

* [`docs/AWS_DEPLOYMENT.md`](docs/AWS_DEPLOYMENT.md) — the deployment walk-through, read once per
  account: what to create, in what order, what each stack costs in *shape* and what it is for. Its
  cost tables ship with every cell holding the literal marker `NOT YET MEASURED`, beside the exact
  commands that fill them — no AWS account exists behind this repository, and plan §13.3 forbids
  writing a number nobody measured (DEC-397).
* [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — day-two operations, organised by symptom, for the moment an
  alarm fires rather than the moment the account is created (DEC-398).
* [`infra/README.md`](infra/README.md) — the CDK app: seven stacks, what each owns, and the
  first-deployment checklist.
* [`docs/DECISIONS.md`](docs/DECISIONS.md) — DEC-300 … DEC-399, allocated by area so entries written
  in parallel could not collide.

**What is in the tree** (none of it reachable from the paths listed higher up this file, which
predate the branch):

```
engine/
├── settings.py                   # one frozen model describing a deployment; the only place os.environ is read
├── runs.py                       # create_run, the job bodies, and JobSpec <-> closure
└── aws/                          # nothing here imports boto3 at module scope (DEC-306)
    ├── s3_storage.py             # Storage over S3: ranged GETs, multipart, SSE-KMS, content-diff
    ├── postgres.py, run_index.py # the model registry and the run index over Postgres
    ├── s3_registry.py            # published models as objects, for a reader with no database
    ├── sagemaker_jobs.py         # JobRunner as a SageMaker job, plus reconcile and a queue
    ├── sagemaker_registry.py     # the optional model-package mirror
    ├── prices.py                 # a published list price, never a bill
    ├── secrets.py, metrics.py    # SSM + Secrets Manager; CloudWatch EMF
alembic/                          # the migrations; `make migrate`
infra/                            # the CDK app, in its own venv (DEC-364)
scripts/run_job_entrypoint.py     # what the container runs
Dockerfile, docker-compose.yml    # the image, and a local Postgres to test against
```

**Make targets.** `make aws-test` runs every Phase 4a suite offline — moto for S3, fakes for
SageMaker, a docker-compose Postgres for the metadata tests, which skip *with a reason* when no
server is there. `make infra-setup`, `infra-lint`, `infra-test`, `infra-synth` and `infra-nag` are
the infrastructure's own chain; `make image` and `image-test` build the container and run the fast
suite *inside* it, which is the only thing that catches a missing system library before a
deployment does. `make aws-deploy` and `aws-bootstrap` are the two that need credentials.

**Milestones.** M21 settings and `S3Storage`; M22 the container and its entrypoint; M23
`SageMakerJobRunner`; M24 Postgres, Alembic and `S3ModelRegistry`; M25 the CDK infrastructure; M26
observability and the two documents. The milestone table higher up this file stops at M7 because it
is Phase 1's table; this paragraph is Phase 4a's.

<!-- ---- END PHASE-4A ---- -->

<!-- ---- PHASE-4B (production) — append only below this line ---- -->

### Phase 4b — Production (Part 1: M46–M49)

**What it is.** Part 1 of [`docs/PHASE4B_PLAN.md`](docs/PHASE4B_PLAN.md): sign-in and roles (M46),
an append-only audit trail (M47), India DPDP engineering controls (M48), and schedules, alerts and
outcome ingestion (M49). All of it runs with local backends and no AWS account; the AWS backends
(S3 Object Lock exports, EventBridge Scheduler, SNS) are built behind protocols and tested with moto.
With every new setting at its default, the product behaves exactly as it did before Phase 4b:
nobody signs in, nothing fires on its own, an alert is a log line (DEC-701). The operator guide is
[`docs/PRODUCTION.md`](docs/PRODUCTION.md); decisions are DEC-700 … DEC-798 in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

> **Legal note.** The DPDP items are engineering controls that support compliance, not legal advice;
> Minfy's legal or compliance team must review the final design. The DPDP Rules were notified on
> 13 Nov 2025, with compliance required by 13 May 2027.

**What exists.**

| Area | Code | Screens |
|---|---|---|
| Users, sessions, roles, enforcement | `engine/access/`, `api/access.py`, `api/access_policy.py`, `api/routes/auth.py` | sign-in, user bar, Admin → Users, role-aware controls on every screen |
| Audit log and retained export | `engine/audit/`, `api/routes/audit.py` | Admin → Audit log (filters, CSV, retained export) |
| Consent, retention, erasure, access export | `engine/privacy/`, `configs/privacy.yaml`, `api/routes/privacy.py`, `scripts/run_retention.py` | Privacy (Admin) |
| Schedules, alerts, outcomes | `engine/scheduling/`, `api/routes/schedules.py`, `api/routes/monitoring.py`, `scripts/fire_schedule.py` | Monitoring |
| Schema | `alembic/versions/0002_access_audit.py` → `0003_privacy.py` → `0004_scheduling.py`, one linear chain after `0001` | — |

The screens are one module, `ui/modules/production/`, registered from the PHASE-4B blocks of
`ui/index.html` and `ui/modules/router.js`; no other phase's screen was edited.

**Turn on sign-in and bootstrap the first Admin.**

```bash
export MARKETING_AI_AUTH_MODE=local
.venv/bin/python -m scripts.create_user --username admin --role admin   # prompts twice; never an argument
make run                                                                # http://localhost:8000/ui
```

`--password-stdin` reads the password from stdin for automation, and the creation is audited as
`system:bootstrap` (DEC-722). The same script recovers a deployment that has lost every Admin; the
API itself refuses to disable or demote the last one (DEC-712). On a prod deployment with sign-in off,
every route but `/healthz` answers 503 `AUTH_NOT_CONFIGURED` rather than serving anyone (DEC-702).

**Roles** are a set, not a ladder; every role includes Viewer, and Admin does **not** include
Approver or Analyst (DEC-703).

| Role | May |
|---|---|
| Viewer | read every screen and report |
| Analyst | upload, onboard, build, train, score, generate copy, create and fire schedules, upload outcomes, acknowledge alerts |
| Approver | approve, reject or promote a challenger (never one from a training run they started, since Plan D, DEC-862), approve campaign copy |
| Admin | users, AWS connection settings, the audit log and exports, every privacy route |

Every route declares its role in `api/access_policy.py`, a route without one is refused, and a test
fails if one is missing (DEC-704, DEC-716). Scheduled work runs as `system:scheduler`, which is
Analyst only, so a schedule can train a challenger and can never approve it. The UI shows a refused
action disabled with the server's reason — "Only an Approver can approve a champion." — never hidden
(DEC-792).

**Audit trail and export.** Every mutating request, and every row-level download, writes exactly one
event — succeeded, failed or refused — with who, when, the action, the object, before/after hashes
and the request id, and no data values: details are a closed set of identifier keys and a data
principal appears only as a salted hash (DEC-705, DEC-718; since Plan D the salt is a deployment
secret with no default in code, DEC-860). Database triggers refuse UPDATE and
DELETE on the table (DEC-714). Admins export a window as JSON lines, locally or to S3 with Object
Lock in COMPLIANCE mode (DEC-715).

**DPDP controls.** Policy is `configs/privacy.yaml` (DEC-730).

- *Consent:* a per-client ledger (CSV import, all or nothing); scoring for a use case mapped to a
  purpose suppresses principals without valid consent through Phase 1's `consent_false` rule and
  reports the counts in `consent_report.json`. No ledger, no change: the run is Phase 1's byte for
  byte (DEC-731, DEC-732).
- *Retention:* `governance.retention_days` is live. `python -m scripts.run_retention` is a dry run by
  default, `--apply` executes exactly that plan; models and aggregate reports are kept (DEC-736).
  `governance.retention_days` and `monitoring.performance_alert_drop_pct` are no longer shown as
  "Coming later" on the Setup form (DEC-795), which supersedes the "parked for Phase 4" note under
  *What is left* above for those two; `monitoring.retraining` is live through managed schedules and
  stays parked on the per-run form because it is read from the use case, not the run.
- *Erasure:* `POST /privacy/erasure` removes a person from every artefact (scanned, rewritten,
  re-scanned), records the outcome in the audit log, and flags models trained on their data for
  retraining at the next scheduled cycle — not immediately, and never straight to champion (DEC-741,
  DEC-743, DEC-768). Since Plan D it answers `202` and a background job does the work store by
  store, with progress and retry (DEC-863; see *Plan D* below).
- *Access requests:* `POST /privacy/access-requests` returns one zip of everything held (DEC-745).
- *Breach runbook:* `docs/RUNBOOK.md` §12.

**Schedules, alerts and outcomes.** Scoring, drift-check and retraining schedules per client × use
case, fired by nothing (default), a local thread, or EventBridge Scheduler running
`scripts/fire_schedule.py` as an ECS task (DEC-765). A slot fires at most once; missed slots are
recorded and alerted (DEC-763, DEC-764). `monitoring.retraining` creates managed schedules
(DEC-767). Uploading actual outcomes for a matured scoring run measures real-world performance —
on the control group when there is one — alerts when the drop exceeds
`monitoring.performance_alert_drop_pct`, and writes Plan B's `incrementality_input.json` (DEC-771 …
DEC-773). Alerts go to the alert history and the log, and also to SNS (email) when configured.

**New settings** (all optional; see `docs/PRODUCTION.md` §6 and `docs/AWS_DEPLOYMENT.md`'s
settings table): `MARKETING_AI_AUTH_MODE`, `MARKETING_AI_AUTH_SESSION_TTL_SECONDS`,
`MARKETING_AI_AUDIT_EXPORT_BUCKET`, `MARKETING_AI_AUDIT_EXPORT_PREFIX`,
`MARKETING_AI_AUDIT_RETENTION_DAYS`, `MARKETING_AI_SCHEDULER_BACKEND`,
`MARKETING_AI_SCHEDULER_TICK_SECONDS`, `MARKETING_AI_SCHEDULER_GROUP_NAME`,
`MARKETING_AI_SCHEDULER_TARGET_ARN`, `MARKETING_AI_SCHEDULER_ROLE_ARN`,
`MARKETING_AI_ALERT_BACKEND`, `MARKETING_AI_ALERT_SNS_TOPIC_ARN`.

**Running the tests.**

```bash
make production-test        # every Phase 4b suite, including the two that train a real model
make production-test-fast   # the same without those two
```

The same tests also run in the main gates: the fast ones in `make test`, all of them in
`make test-all`. The screen tests run in jsdom through pytest. Locally they skip, saying so, until
`npm ci` has been run once in `tests/integration/production/ui`; in CI (`REQUIRE_JSDOM=1`, Plan D
M56, DEC-877) node and `npm ci` are set up and a missing jsdom fails the run instead of skipping.

**What is left for Part 2 (M50–M52).** Each needs an AWS account and owner decisions P1–P6
(`docs/PHASE4B_PLAN.md` §0):

- **M50 — first deployment and cost measurement** (P1–P3, P5): wire the identity provider — a third
  `IdentityProvider` beside `DisabledIdentity` and `LocalIdentity` (DEC-713) — and run the first real
  EventBridge firing, SNS delivery and Object Lock export, none of which has run against AWS yet (the
  migrations and the Postgres append-only trigger pass `make test-postgres` on a local server).
  `docs/M50_CHECKLIST.md` sequences the day.
- **M51 — client isolation** (P4: single-tenant per account, or multi-tenant SaaS).
- **M52 — hardening:** backup and restore drill, load test, security review.
- Also open from Part 1: "Run now" as a background job rather than inside the request, and alert
  deduplication. Login rate limiting, the secret consent salt, erasure as a background job and the
  approve/reject screen were done by Plan D M54 (DEC-860 … DEC-864; see *Plan D* below).

<!-- ---- END PHASE-4B ---- -->
<!-- ---- PHASE-3B (uplift) — append only below this line ---- -->

### Phase 3b — Uplift modelling

Phase 1 predicts **who will convert**. Uplift predicts **who converts *because* of the action**: the
difference between a customer's chance of converting if contacted and if not, learnt from a past
campaign whose treatment was **randomly assigned**. It is a problem type of its own (`uplift`,
metric `auuc`), configured by the `uplift:` block of a use case (`configs/engine.yaml` has the
defaults, each commented) and read only when the problem type is uplift (DEC-601). The
plain-language guide, with every metric's exact definition, is [`docs/UPLIFT.md`](docs/UPLIFT.md).

**Where it is in the product.** The uplift screens are a UI module (`ui/modules/uplift/`) at
`#/uplift`, reached from an **Uplift modelling ›** link the module adds to the Overview and an
**Uplift for this use case ›** link on every use-case screen (DEC-639). A Phase 1 scoring run's
screen also links to its **Campaign results**. Uplift is deliberately *not* a choice in Phase 1's
Setup (DEC-608). An uplift run starts from its own Setup, which picks the treatment column and runs
the experiment checks before the run exists.

| # | Milestone | Definition of done | Status |
|---|---|---|---|
| M40 | Contracts, checks, problem type | Data contract, the seven checks, `uplift` registered, config schema, artefact models | **done** |
| M41 | Learners and evaluation | S/T/X learners; Qini, AUUC, deciles with bootstrap intervals; uplift champion rule; planted-effect and null tests green | **done** |
| M42 | Segments, policy, explanations | Four segments, budgeted policy, SHAP on the uplift, actions use segments | **done** |
| M43 | Incrementality and OPE | Campaign results with maturity, for uplift and Phase 1 scoring runs; IPS, SNIPS (when defined) and DR with tests | **done** |
| M44 | UI | Setup, Model, Output and Campaign results screens; prototype updated | **done**; walked end to end in Chromium (DEC-660); the prototype follows DEC-608 and labels an uplift run on Win-back a candidate (DEC-667), and leaves out the Overview link pending a ruling (DEC-666) |
| M45 | Criteo, docs, hardening | Criteo run report; `docs/UPLIFT.md`; README and DECISIONS current | docs **done**; Criteo **not run**: the data cannot be fetched here (re-tested 2026-09-23, DEC-656) |

#### M40 — Contracts, checks, problem type

`engine/uplift/config.py` (the `uplift:` block and which leaves a Phase 5 agent may edit),
`engine/uplift/contracts.py` (every artefact model, served from their own registry, DEC-602),
`engine/uplift/data.py` (what may be a feature, DEC-633 to DEC-635) and `engine/uplift/checks.py`:
treatment column missing or not 0/1, an arm too small, treatment predictable from the features, the
outcome window not yet elapsed, a feature dated after the treatment, and (since Plan D M53) a customer
treated at one snapshot and held out at another. `POST /uplift/runs` runs
Phase 1's validation and these seven synchronously and answers `409` with both reports (DEC-654).
`TREATMENT_NOT_RANDOM` can be acknowledged, never loosened per run (DEC-607). An acknowledged run is
marked not causal in every artefact.

```bash
.venv/bin/python -m pytest tests/unit/uplift/test_uplift_checks.py tests/unit/uplift/test_uplift_data.py \
    tests/unit/uplift/test_uplift_config.py -q
```

#### M41 — Learners, evaluation and the champion rule

`engine/uplift/learners.py` fits an S-, T- or X-learner on LightGBM or AutoGluon.
`engine/uplift/metrics.py` measures Qini, AUUC, uplift@10/20/30% and deciles on the hold-out, with
percentile bootstrap intervals drawn within each arm and the model held fixed (DEC-605, DEC-610).
`engine/uplift/champion.py` keeps Phase 1's shape: beat the champion on the same hold-out by
`champion_min_improvement_pct`. It also requires the model to be causal with an AUUC lower bound
above zero (DEC-614). An uplift model fills an empty champion slot only on a use case configured as
uplift (DEC-609), and never replaces a champion of another metric (DEC-649).

```bash
.venv/bin/python -m pytest tests/unit/uplift/test_metrics.py tests/unit/uplift/test_champion.py \
    tests/unit/uplift/test_learners.py -q
```

#### M42 — Segments, targeting, explanations and actions

`engine/uplift/segments.py` splits customers into persuadables, sure things, lost causes and
sleeping dogs. Sleeping dogs are never treated (DEC-623). `engine/uplift/policy.py` recommends how
many to contact within the budget, with the expected incremental conversions the hold-out measured
at that ranking depth (DEC-604). `engine/uplift/explain.py` gives SHAP of the predicted uplift, exact
for the X-learner on LightGBM and a stated-fidelity surrogate otherwise (DEC-615, DEC-616).
`engine/uplift/actions.py` replaces bands with segments on top of Phase 1's unchanged suppression and
control group (DEC-621). It marks `intended_treatment` so campaign results compare like with like
(DEC-606, DEC-624).

```bash
.venv/bin/python -m pytest tests/unit/uplift/test_segments.py tests/unit/uplift/test_policy.py \
    tests/unit/uplift/test_uplift_actions.py tests/unit/uplift/test_uplift_explain.py -q
```

#### M43 — Incrementality and off-policy evaluation

`engine/uplift/incrementality.py` measures a finished campaign from an uploaded outcomes file:
treated rate minus control rate with a Newcombe interval, relative lift, incremental conversions and
a p-value. Rows whose outcome window has not elapsed are excluded and counted, and "Results available
on <date>" is shown until one has. It works for any scoring run with Phase 1's control group, not
only an uplift one. `engine/uplift/ope.py` estimates a targeting rule off-policy on the randomised
hold-out (IPS, doubly robust, and SNIPS when it is defined, DEC-661). `engine/uplift/flow.py` runs
uplift training and scoring as subclasses of Phase 1's own flows, dispatched by one lookup in each
`Pipeline` entry point (DEC-646).

```bash
.venv/bin/python -m pytest tests/unit/uplift/test_incrementality.py tests/unit/uplift/test_ope.py \
    tests/integration/uplift -q
```

#### M44 — The uplift screens

`ui/modules/uplift/`: Setup (upload, treatment picker, train or score, inline refusals with the
acknowledge button), Running, Model (Qini curve with the random line, AUUC with its interval,
uplift by decile, an OPE form), Output (four-segment chart, recommended contacts, expected
incremental conversions, treat list download) and Campaign results. A banner marks every screen
whose artefacts say `causal: false`, and a missing value is always "—" (DEC-644).

The screens have been walked end to end in Chromium: `tests/integration/uplift/test_uplift_browser.py`
starts the real app, clicks through upload, training, the Model and Output pages (checked number for
number against the artefacts), scoring, Campaign results before and after maturity, the not-random
refusal and acknowledgement, and a 390 px width, with no console error beyond the `404`s the contract
defines (DEC-660). It is marked slow and needs `playwright` with a Chromium build; without them it
skips and says why. The reference
prototype (`marketing-ai-prototype.html`) starts uplift on its own screen as the product does
(DEC-608, DEC-666) and shows an uplift run on Win-back as a candidate to promote (DEC-667);
`make prototype-test` checks it.

```bash
make run                       # then open http://localhost:8000/ui/#/uplift
.venv/bin/python -m pytest tests/unit/uplift/test_uplift_ui.py -q    # the node suites; skipped without node
.venv/bin/python -m pytest tests/integration/uplift/test_uplift_browser.py -m slow   # needs playwright + chromium
```

#### M45 — Documentation, Criteo and hardening

[`docs/UPLIFT.md`](docs/UPLIFT.md) is the guide for marketers and reviewers: uplift against
propensity, the data contract, the seven checks, the segments, how to read the Qini chart and AUUC,
targeting, campaign results, OPE, the champion rule, what "not causal" means, how to run it, and the
limits. These include binary treatment and outcome only; the single-column-key limit listed here
before is gone since Plan D M53, which keys uplift by customer + snapshot date (DEC-853). The Criteo Uplift use case in
[`library/criteo-uplift/`](library/criteo-uplift/) is now written for this problem type, but the
dataset could not be downloaded from this environment, so it stays uninstalled and has no run report
numbers (DEC-656).

```bash
make uplift-test               # every Phase 3b suite, slow ones included
make lint test                 # the gate, as for every phase
```

```
engine/uplift/
├── config.py, contracts.py       # the `uplift:` block and every artefact model (DEC-601 … DEC-603)
├── data.py, checks.py            # data preparation and the seven checks
├── learners.py, explain.py       # S/T/X meta-learners; TreeSHAP of the predicted uplift
├── metrics.py, champion.py       # Qini, AUUC, deciles, bootstrap; the uplift champion rule
├── segments.py, policy.py        # the four segments; the budgeted targeting recommendation
├── actions.py                    # segments instead of bands, on Phase 1's suppression and control group
├── incrementality.py, ope.py     # campaign results; off-policy evaluation
└── flow.py                       # UpliftTrainFlow / UpliftScoreFlow, dispatched by engine.pipeline.uplift_flow_for
api/routes/uplift.py              # treatment candidates, POST /uplift/runs, artefacts, campaign results, OPE
ui/modules/uplift/                # Setup, Running, Model, Output and Campaign results screens
```

Decisions are DEC-600 … DEC-680 in [`docs/DECISIONS.md`](docs/DECISIONS.md). What Phase 3b changed
above its blocks is announced in [`docs/CROSS_BRANCH_REQUESTS.md`](docs/CROSS_BRANCH_REQUESTS.md).

<!-- ---- END PHASE-3B ---- -->
<!-- ---- PLAN-E (pilot) — append only below this line ---- -->

### Plan E — Pilot readiness

A pilot fails for reasons tests never catch: a data request nobody could follow, results a business
user cannot read, no way to show value to the client's management. Plan E builds what goes around
the engine for a first client pilot. It **reads artefacts only**: nothing in `engine/pilot/` trains,
scores, checks or promotes anything, and no check, threshold or champion rule changed. Every report
is one page model (`engine/pilot/document.py`, DEC-903) drawn twice, as a self-contained HTML page
and as a PDF (`fpdf2`, pinned in the PLAN-E block of `pyproject.toml`), so the two cannot disagree.
Every client-facing sentence goes through one plain-language catalogue,
[`configs/pilot/help.yaml`](configs/pilot/help.yaml) (`engine/pilot/help.py`, DEC-902), and a
jargon check (`engine/pilot/plain.py`, DEC-904) that the tests run over the catalogue, the data
request and every report. The plan text itself is not in the repository (see
[`docs/plans/README.md`](docs/plans/README.md)).

| # | Milestone | Definition of done | Status |
|---|---|---|---|
| M59 | Data request kit and pre-flight check | Data request and templates generated from the configs and drift-checked; the pre-flight checker runs on a client laptop without AutoGluon; `dist/pilot-kit.zip` | **done** |
| M60 | Data readiness report and help catalogue | One verdict per dataset build (Ready, Ready with warnings, Not ready) with every problem's meaning and fix; every check code and advanced setting explained in plain words | **done** |
| M61 | Business results report | The champion's headline, reasons, actions and limits, in business terms, as HTML and PDF | **done**; the Minfy and client logos are placeholder boxes |
| M62 | Value and ROI view | A measured campaign effect times the client's own values, always a range, in rupees | **done** |
| M63 | Demo mode, tour and tooltips | "Demo Telecom" seeded through the platform's own API; a six-step tour; "What does this mean?" beside every warning and setting | **done** |
| M64 | Playbook, feedback and usability test | The pilot playbook; in-app feedback stored and exported; the internal usability session run | **partial** — playbook and feedback built; the usability session has not been run and the playbook awaits review |

#### M59 — The data request kit and the pre-flight check

`engine/pilot/data_request.py` builds the client-facing request from the use-case configs,
`configs/roles.yaml` and the wording in `configs/pilot/data_request.yaml`: which tables (required,
recommended), which columns (required, needed, useful), how much history (the same sum the build
refuses below), and what never to send (DEC-905). `scripts/gen_data_request.py` writes the committed
[`docs/pilot/DATA_REQUEST.md`](docs/pilot/DATA_REQUEST.md) and `docs/pilot/templates/*_template.csv`;
`--check` fails on drift. `engine/pilot/preflight.py` and `scripts/preflight.py` check a folder of
the client's files on the client's own laptop with the platform's own profiling and onboarding checks,
upload nothing, and write one HTML page; the exit code is 0 (nothing blocks), 1 (a problem blocks) or
2 (the command was misused) (DEC-906). `scripts/build_pilot_kit.py` zips the request, the templates,
the checker and what it runs on into `dist/pilot-kit.zip`, with a `requirements-preflight.txt` of
four libraries.

```bash
make pilot-generate                                   # regenerate the request and templates
make pilot-check                                      # fail if they are stale
make pilot-kit                                        # dist/pilot-kit.zip
.venv/bin/python -m scripts.preflight <folder> [--use-case telco-churn] [--out report.html]
```

#### M60 — The data readiness report and the help catalogue

`engine/pilot/readiness.py` reads a build back for a client analyst from
`datasets/<id>/build_status.json`, `build_report.json` and `dataset_manifest.json`, the client
store's recipe, mappings and sources, and each source's `profile.json`: what arrived, how well it
links up, whether there is enough history and enough examples of the outcome, which personal
details were masked, and what blocks the pilot, with the fix. The verdict is the build's own; the
report explains it and never overrides it (DEC-907). `engine/pilot/help.py` serves
`configs/pilot/help.yaml`: a title, a meaning and a fix for every check code (Phase 1, Phase 2,
the Phase 3b uplift checks, the two drift verdicts, the threshold fallback and the five `PREFLIGHT_*`
codes), a meaning for every advanced setting path, a glossary and plain names for the metrics.

#### M61 — The business results report

`engine/pilot/results.py` answers five questions about a use case's champion (or one model) from
its training run's `run_config`, `evaluation`, `decile_lift`, `baseline`, `feature_importance` and
split, and the latest scoring run's `scoring_summary` and `row_explanations`; for an uplift
champion, `uplift_evaluation` and `segments`. What does it do, how good is it ("the 10% flagged
first contain X% of everyone who left", beside a random 10%), why, what to do, and what are its
limits. Example customers are "Customer A/B/C", never an ID; a section whose artefact is missing
says so rather than showing a figure (DEC-909).

#### M62 — The value and ROI view

`engine/pilot/roi.py` reads a campaign's measured effect from `incrementality_report.json` (Phase
3b campaign results) or `incrementality_input.json` (Phase 4b outcome ingestion, with the same
Newcombe interval), multiplies it by the values the client enters (stored with the run as
`runs/<id>/pilot_roi_inputs.json`), and shows every amount as a range in rupees with Indian digit
grouping (`format_inr`). Before the outcome window has passed it shows the date results will be
ready, not a number (DEC-908).

#### M63 — Demo mode, the tour and the tooltips

`scripts/seed_demo.py` seeds a synthetic "Demo Telecom" through the platform's own API: raw tables
for 4,000 subscribers uploaded and mapped, a dataset built, a churn model trained and made champion,
next month scored with a control group of 400, a win-back uplift model trained, and both campaigns
measured on simulated outcomes (about two and a half minutes; DEC-960). It writes `pilot/demo/demo.json` (`engine/pilot/demo.py`) and keeps the raw
tables, clean and with one planted problem, under `pilot/demo/raw/<variant>/` for the pre-flight
check. No public or non-commercial dataset is used (DEC-910). With `MARKETING_AI_DEMO_MODE=true`
(`Settings.demo_mode`, off by default, DEC-901) the screens open on the demo. The UI module
`ui/modules/pilot/` (`index.js`, `api.js`, `screen.js`, `help.js`, `feedback.js`, `tour.js`,
`styles.js`) adds the Pilot screen at `#/pilot`, `#/pilot/view/readiness/<id>`,
`#/pilot/view/results/<uc>` and `#/pilot/value/<run>`, a demo badge, a six-step tour, and a "?"
beside every warning code and advanced setting, without editing any other workstream's screen
(DEC-912).

```bash
make demo-seed                 # seed Demo Telecom into MARKETING_AI_DATA_DIR (or data/); trains once
make demo                      # seed if needed, then serve on :8000 with MARKETING_AI_DEMO_MODE=true
```

#### M64 — The playbook and the feedback loop

[`docs/pilot/PLAYBOOK.md`](docs/pilot/PLAYBOOK.md) is the ten-week pilot plan with its success
criteria, and [`docs/pilot/USABILITY_TEST.md`](docs/pilot/USABILITY_TEST.md) the script for the
internal usability session. `engine/pilot/feedback.py` stores what a person types into the feedback
button on every screen as one JSON file under `pilot/feedback/<id>.json`, with the screen's route
(ids, never cell values) and the words passed through the platform's redaction, and exports them as
`pilot_feedback.csv` or `.jsonl` (DEC-911).

**Routes** (`api/routes/pilot.py`; every one reads artefacts). Reading is Viewer, saving value
inputs is Analyst, and the feedback export is Admin and audited on read.

| Method | Path | What |
|---|---|---|
| GET | `/pilot/help` | the plain-language catalogue the tooltips read |
| GET | `/pilot/data-request` | the data request, `format=md` or `json` |
| GET | `/pilot/templates/{role}` | one table's header-only CSV template |
| GET | `/pilot/readiness/{dataset_id}` | the data readiness report, `format=html`, `pdf` or `json` |
| GET | `/pilot/results?use_case=…` or `?model_id=…` | the business results report of a champion, or of one model |
| GET, PUT | `/pilot/roi/{run_id}` | a campaign's value view; the client's value inputs |
| GET | `/pilot/demo` | whether demo mode is on, and what the seeded demo holds |
| GET | `/pilot/demo/raw/{variant}` | the demo's raw tables as a zip (clean or broken) |
| POST | `/pilot/feedback` | record feedback on a screen |
| GET | `/pilot/feedback/export` | every feedback entry, for the pilot team |

**Running the tests.**

```bash
make pilot-test                # every Plan E suite except the slow acceptance test
.venv/bin/python -m pytest tests/integration/pilot/test_pilot_acceptance.py -m slow   # seeds the demo; browser journey
```

`tests/unit/pilot/` covers the kit, the pre-flight check, the catalogue, the page model, the value
view and the feedback store; `tests/integration/pilot/test_pilot_api.py` the routes and their
refusals; `test_readiness_report.py` the readiness report on real builds; and the slow
`test_pilot_acceptance.py` seeds the demo and walks the journey Plan E's acceptance names, including
the results report, which has no fast test of its own.

```
engine/pilot/
├── document.py                   # ReportDocument blocks; render_html and render_pdf
├── help.py, plain.py             # the plain-language catalogue; the jargon check
├── data_request.py, preflight.py # the data request kit; the pre-flight checker
├── readiness.py, results.py      # the data readiness and business results reports
├── roi.py                        # the value view, as rupee ranges
├── demo.py                       # the demo manifest and where its raw tables live
└── feedback.py                   # feedback entries, redacted; CSV and JSON lines export
api/routes/pilot.py               # the /pilot/* routes and their access policies
ui/modules/pilot/                 # the Pilot screen, tour, tooltips and feedback button
configs/pilot/                    # help.yaml and data_request.yaml
scripts/                          # preflight.py, gen_data_request.py, seed_demo.py, build_pilot_kit.py
docs/pilot/                       # DATA_REQUEST.md, templates/, PLAYBOOK.md, USABILITY_TEST.md
```

**What is left.**

1. **The internal usability session has not been run.** It needs a colleague outside the team and a
   facilitator; [`docs/pilot/USABILITY_TEST.md`](docs/pilot/USABILITY_TEST.md) is the script, and
   its session log is empty.
2. **The playbook awaits Mukund's review.** [`docs/pilot/PLAYBOOK.md`](docs/pilot/PLAYBOOK.md) is a
   draft and has not been agreed with a client.
3. **The client and Minfy logos are placeholders.** The reports draw named boxes; the logos are the
   client's and Minfy's to supply.
4. **Nothing is scheduled.** The `pilot_feedback` export and the reports are produced on request
   only; no schedule sends them.
5. ~~The churn demo campaign is measured through outcome ingestion.~~ **Done (DEC-951).** Since
   Plan D M53 campaign results join on both key columns, so the seed measures the churn campaign on
   its Campaign results page (`POST /runs/{id}/campaign-results`), like the win-back one.

Decisions are DEC-900 … DEC-912 in [`docs/DECISIONS.md`](docs/DECISIONS.md). What Plan E changed
above its blocks is announced in [`docs/CROSS_BRANCH_REQUESTS.md`](docs/CROSS_BRANCH_REQUESTS.md).

<!-- ---- END PLAN-E ---- -->

<!-- ---- PLAN-D (hardening) — append only below this line ---- -->

## Plan D — leftovers and hardening (M53–M58)

[`docs/plans/MARKETING_AI_PLAN_D_HARDENING.md`](docs/plans/MARKETING_AI_PLAN_D_HARDENING.md) closes
the open items of the 23 Sep 2026 status report that need no AWS account. It adds no new capability:
every item was already named in an earlier plan or in `docs/CROSS_BRANCH_REQUESTS.md`. Its three
rulings are recorded as DEC-850 (R1, the narrowed leak check), DEC-851 (R3, the secret salt) and
DEC-852 (R2, Criteo for internal validation only); its decisions are DEC-850 … DEC-899 in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

| # | Milestone | Definition of done | Status | Tests that prove it |
|---|---|---|---|---|
| M53 | Uplift on two-column keys, and uplift across the product | Uplift keyed by customer + snapshot date, one arm per customer per campaign, hold-out grouped by customer; Phase 1's pages render uplift runs; uplift drift; the uplift codes in `docs/DATA_CONTRACT.md` | **done** | `tests/unit/uplift/test_uplift_two_column_keys.py`, `tests/integration/uplift/test_uplift_two_column_keys.py`, `tests/unit/uplift/test_phase1_pages_uplift.py`, `tests/unit/uplift/test_uplift_drift.py` |
| M54 | Phase 4b leftovers | Sign-in rate limiting; a secret privacy salt (R3); the approve/reject screen with separation of duties; erasure as a background job; migration `0005` | **done** | `tests/integration/production/test_login_rate_limit.py`, `tests/unit/production/test_login_throttle.py`, `tests/unit/production/test_privacy_config.py`, `tests/integration/production/test_approvals.py`, `tests/integration/production/test_approvals_ui_js.py`, `tests/integration/production/test_privacy_erasure_api.py`, `tests/unit/production/test_plan_d_migration.py` |
| M55 | Leak-check conditions (R1) | `full_leak_check`; the full check forced on a recipe's first build and shown in the build report; nightly and golden full checks; a leak the narrowed check misses | **done**; the full check at the full M14 size is a manual measurement not yet run (DEC-873) | `tests/unit/onboarding/test_leak_check.py`, `tests/integration/test_onboarding_leak_check.py` |
| M56 | Test reliability and CI coverage | The two timing-sensitive uplift prototype tests fixed and passing 50 times under load; jsdom and Postgres tests run, not skip, in CI | **done** | `tests/unit/test_check_no_skips.py`, `tests/unit/test_container_files.py` |
| M57 | Measurements that need a laptop or another network | 1M rows on a stated laptop recorded in `docs/PERFORMANCE.md`; the Criteo run, or a record that the network stayed blocked | **partial** — tooling done; laptop measurement is an owner action; Criteo network blocked, skipped | `tests/unit/test_bench_1m.py` (the tooling only; no test can prove a measurement) |
| M58 | Documentation and repository hygiene | Missing plans committed; every cross-branch request closed or re-filed; README current and covered by the honesty check; merged branches deleted | **partial** — README, decisions and the cross-branch log are current; the missing plans (DEC-885) and deleting merged branches are owner actions | documentation; `tests/unit/test_check_readme.py` covers the table's mapping |

`scripts/check_readme.py` maps M53 … M56 to the tests above, so a "pending" in this table with those
tests green fails CI. M57 and M58 are left unmapped: what is left of them is a measurement and an
owner's action, which no test can prove.

### M53 — Uplift on two-column keys, and uplift across the product

What changed is in *What works today* at the top of this README (the uplift bullets) and in DEC-853
… DEC-859. In short: an uplift file may hold each customer at several snapshot dates; treatment is
per customer per campaign (`TREATMENT_VARIES_WITHIN_ENTITY`, error, not acknowledgeable); the uplift
hold-out is grouped by customer; `GET /runs/{id}/artefacts/{name}` serves the uplift artefacts;
Phase 1's Data, Model and Output pages render an uplift run; every uplift scoring run writes
`uplift_drift.json`; and `POST /runs` refuses an uplift training run with `422
UPLIFT_REQUIRES_UPLIFT_ROUTE`. The uplift codes are in `docs/DATA_CONTRACT.md` §11, kept in step with
`UPLIFT_VALIDATION_CODES` by a test. Remaining limit (`docs/UPLIFT.md` §15): `POST /uplift/runs`
takes an upload, not a `dataset_id`, so a multi-snapshot scoring file goes through a dataset.

### M54 — Phase 4b leftovers

M54 closes four items Phase 4b Part 1 left open: login rate limiting, a secret privacy salt (ruling
R3), the approve/reject screen, and erasure as a background job. It also adds migration `0005` and
M57's measurement tooling. None of it needs an AWS account. Decisions are DEC-860 … DEC-866.

| Area | Code | Routes | Screens |
|---|---|---|---|
| Sign-in rate limiting | `engine/access/throttle.py`, `api/access.py` (`client_address`), `api/routes/auth.py` | `POST /auth/login` → 429 `LOGIN_LOCKED` + `Retry-After` | sign-in shows the refusal |
| Secret privacy salt | `engine/privacy/config.py` (`privacy_salt`, `check_privacy_salt`), `engine/settings.py`, `infra/database.py` (`PrivacySalt`) | — (a prod API does not start without it) | — |
| Approvals and separation of duties | `engine/approvals.py`, `api/routes/approvals.py`, `api/routes/models.py` | `GET /approvals`, `POST /models/{id}/approve`, `/promote`, `/reject` | Approvals (`#/approvals`) |
| Background erasure | `engine/privacy/erasure.py`, `engine/privacy/erasure_jobs.py`, `api/routes/privacy.py` | `POST /privacy/erasure` → 202, `GET /privacy/erasure/{id}/progress`, `POST /privacy/erasure/{id}/retry`, `GET /privacy/erasure/{id}` | Privacy (Admin) |
| Schema | `alembic/versions/0005_plan_d.py`: `model_decision`, `erasure_progress`; autogenerate covers the platform tables (DEC-865, DEC-888) | — | — |
| 1M-row measurement | `scripts/bench_1m.py`, `scripts/bench_large_file.py` | — | — |

- *Sign-in limits (DEC-861):* failed sign-ins are counted per username (case-folded, known or not)
  and per client address in a sliding window. Either one locks when it reaches its limit, and while
  it is locked the password is not checked at all. A successful sign-in clears only the account's
  count. The failure that locks, and every refused attempt, is audited with `lockout`. Counts are
  in memory in each process and a restart clears them. Behind a load balancer, set
  `MARKETING_AI_TRUSTED_PROXY_HOPS` so the address is read from `X-Forwarded-For`.
- *Privacy salt (DEC-860):* `MARKETING_AI_PRIVACY_SALT` must be at least 16 characters and has no
  default in code. On a laptop (SQLite platform database) one is generated once into
  `privacy_salt` (0600) beside `platform.db`. On `env=prod` the API refuses to start without it.
  With Postgres on dev or staging, a privacy route or gated scoring run fails with `SETTING_REQUIRED`
  until it is set. The database stack generates `marketing-ai/<env>/privacy-salt` once, keeps it
  through `cdk destroy`, never rotates it, and passes it in through the application secret.
  Changing a salt makes every existing hash unmatchable.
- *Approvals (DEC-862, DEC-864):* each waiting challenger is shown against the champion it was
  measured against, on the challenger's own test split, with every metric the training run recorded
  for both models, the difference, and which model each favours. Nothing is re-scored or estimated.
  Runs trained before Plan D show the headline metric only and say so, and a comparison whose
  champion has since changed is flagged. Approve and reject need a reason on the screen and are
  recorded in `model_decision`. The person who started the training run (`run.json` `requested_by`)
  gets 403 `SEPARATION_OF_DUTIES`, and the screen shows the button disabled with that reason. With
  sign-in on, `approved_by`/`promoted_by` is the signed-in username. With sign-in off the rule
  cannot be enforced, and the screen says so.
- *Erasure (DEC-863):* the request is queued and answered 202. One background worker, holding the
  store rewrite lock, rewrites store by store and retries a failing store up to three times with
  backoff. The Privacy screen shows each store's progress, then the completion report. A request
  that ends `failed` can be retried by giving the id again, which is checked against the stored
  hash. The audit trail records the start (`privacy.erasure`) and the end
  (`privacy.erasure.complete`).

**New settings** (all optional except the salt on prod): `MARKETING_AI_PRIVACY_SALT`,
`MARKETING_AI_LOGIN_MAX_FAILURES_PER_ACCOUNT` (5), `MARKETING_AI_LOGIN_MAX_FAILURES_PER_ADDRESS` (20),
`MARKETING_AI_LOGIN_FAILURE_WINDOW_SECONDS` (900), `MARKETING_AI_LOGIN_LOCKOUT_SECONDS` (900),
`MARKETING_AI_TRUSTED_PROXY_HOPS` (0).

**Upgrading.** Run `alembic upgrade head` (`0004` → `0005`) on Postgres. Set
`MARKETING_AI_PRIVACY_SALT` everywhere except a laptop, before starting the API. A local ledger or
erasure register written before M54 was hashed with the client id or `local`, and its hashes do not
match the new salt; nothing re-hashes them.

### M55 — Leak-check conditions (ruling R1)

- Leak checking follows ruling R1. A dataset build runs the narrowed future-data leak check by default (DEC-096). The **full** check rebuilds every snapshot row. It runs when `POST /datasets` is sent `full_leak_check: true`, and always on a client's first build of a recipe. "The same recipe" is `recipe_hash`: the recipe's logic without ids or times, so monthly replays and scheduled rebuilds of an unchanged recipe count as the same one (DEC-870, DEC-871).
- The build report (`GET /datasets/{id}/report`, and a "Future-data check" card on the Build step) records which check ran, why, and how many snapshot rows it rebuilt. The manifest records the `recipe_hash` (DEC-874).
- A test builds a join-key leak that the SQL guard and the narrowed check both miss: the build passes and writes the dataset. The full check catches it with FUTURE_EVENTS_LEAKED (DEC-872). The full check runs nightly (`make test-all`) on the 20,000-customer benchmark tables and in the golden end-to-end tests (DEC-873). `python -m scripts.bench_onboarding --full-leak-check` measures it at any size.

The measured cost is in `docs/PERFORMANCE.md` §8.

### M56 — Test reliability and CI coverage

- The two uplift prototype tests that failed about one run in three on a busy machine are fixed at
  the cause: `tests/prototype/harness.mjs`'s `settle(dom)` waits until the page has no file read or
  timer left, instead of the first re-render or a fixed sleep, and every fixed wait in the prototype
  suites was replaced by it. No assertion changed (DEC-875).
- `scripts/loop_prototype_tests.sh COUNT TEST_FILE` runs a suite COUNT times, with `LOOP_LOAD=N`
  busy loops beside it (DEC-876).
- CI runs the node and jsdom suites and the prototype suite, and fails, rather than skips, when node
  or jsdom is missing; `scripts/check_no_skips.py` fails CI when a must-run node, jsdom or Postgres
  test was skipped or never ran (DEC-877, DEC-878). The Postgres tests already ran in CI's
  `make test`; that was verified, not changed (DEC-879).

Measured on 4 vCPUs shared with other test runs (load average 9–25), with
`LOOP_LOAD=N scripts/loop_prototype_tests.sh COUNT tests/prototype/uplift.test.mjs` (38 tests in
the file):

| Harness | Load | Runs | Passed | Failing tests |
|---|---|---|---|---|
| before (first-swap `upload()`, fixed sleeps), snapshot of `2248cd0` | 4 busy loops | 6 | **0** | the two reported tests, in all 6 runs |
| intermediate (read tracking only, fixed sleeps kept) | 2 × (2 busy loops + a loop) | 6 (stopped) | 4 | "0% control group…" (`wait(1300)` too short), "DEC-608…" (`wait(30)` after a link click) |
| after (DEC-875 `settle()`) | 2 × (2 busy loops + a loop), concurrent | 50 | **50** | none (25/25 and 25/25) |

The "after" row is two copies of the script running at once, 25 runs each, each with `LOOP_LOAD=2`:
four busy loops and two node suites on four cores, which is harsher than the single-loop "before".

### M57 — Measurements that need a laptop or another network

- **The one-million-row laptop measurement is an owner action and has not been made.** On an idle
  laptop, from a checkout that has run `make setup`, run
  `.venv/bin/python -m scripts.bench_1m --kind laptop --label "<make and model, RAM>" --record`.
  It appends a row to `docs/PERFORMANCE.md`. No laptop number has been measured, so plan §11's
  laptop criterion is not met; the container figures under *Large-file handling* are the only
  one-million-row measurements. `bench_large_file.py` now reports peak memory correctly on macOS
  (DEC-866).
- **The Criteo uplift run was skipped.** From this environment the Criteo and mirror hosts answered
  403, so no row was read (`library/criteo-uplift/run_report.md`, `docs/LIBRARY.md`). It needs a
  machine with open internet, and its report is internal validation only, under the non-commercial
  licence (R2, DEC-852).

### M58 — Documentation and repository hygiene

- `docs/plans/` holds Plan D's text; the Phase 2, 3a and 4a plans, Plan B, the Phase 5 design and
  Plans E and F were not available to the branch and are listed as missing in
  [`docs/plans/README.md`](docs/plans/README.md) (DEC-885).
- Every open entry in `docs/CROSS_BRANCH_REQUESTS.md` was either resolved, with the date and the
  decision that closed it, or re-filed to the person who must act.
- Deleting the merged branches is an owner action.

<!-- ---- END PLAN-D ---- -->
