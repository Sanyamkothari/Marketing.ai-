# Marketing AI

> **New here? Start with [docs/START_HERE.md](docs/START_HERE.md)**: the four pages and the flow in
> plain words, with screenshots. Developers running it on a laptop: [docs/QUICKSTART.md](docs/QUICKSTART.md)
> (Python 3.11, about 20 minutes, no AWS account, AI service or client data needed). How ready v1
> is: [docs/V1_READINESS.md](docs/V1_READINESS.md).

**Marketing AI helps a marketer decide who to contact, and then shows what the campaign changed.**
Pick a stage of the customer lifecycle (Awareness, Onboarding, Service, Churn, Win-back), bring your data,
and the product trains a model, scores every customer with a reason and a next step, and after the
campaign measures the extra conversions it caused.

It works in four pages: **Home** (the customer lifecycle and its use cases), **Connections** (S3, PostgreSQL /
Redshift, MySQL and more, set up and tested in one place), **Results** (every run, newest first) and
**Settings**. Each use case is one flow: **1 Choose data** (upload a file or pick one from a
connection) → **2 Guided setup** (a helper checks and fixes the data, asks when it is unsure,
recommends settings, and changes nothing until you approve) → **3 Run & results** → **4 Measure the
campaign**. It is a single-company tool that starts empty; nothing is pre-loaded.

Under the hood it is one reusable engine: each use case is a *configuration file*, not code, AutoML
trains the best model on your data, a new model is used only after someone approves it, and every
prediction comes with a reason and a recommended action.

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
- **Guided setup and Connections.** Every predictive use case opens on a helper that prepares the
  file and recommends settings for your approval, with the fixes saved and replayed on next month's
  file (Plan G, `docs/AGENTS.md`); data can come from a file or from a tested connection (Plan H,
  `docs/CONNECTIONS.md`).
- **One journey for every business.** Home shows one generic journey (`configs/industries/generic.yaml`,
  DEC-1110 …). The industry templates of Plan A M38 (telecom, banking, insurance, e-commerce, ad
  tech) remain as config and open by URL (`#/industry/<id>`), but are no longer offered.
- **Generative features** on the AI service a person connects under Connections - Amazon Bedrock, OpenAI,
  Claude, OpenRouter, Hugging Face or any OpenAI-compatible server (Deliverable AI; the Guided-setup chat
  uses a separate Product AI): the onboarding assistant, root-cause summaries per risk segment and
  win-back copy with a judge (Phase 3a). With none connected they say so (`409 AI_NOT_CONNECTED`); the
  deterministic test model answers only under `MARKETING_AI_ALLOW_FAKE_AI=1` (tests, DEC-1140).
- **Uplift modelling.** Step 4 of a use case, *Measure the campaign*, learns who to contact next
  time; by hand, from *Settings → Advanced → Uplift workbench* (`#/uplift`): train S-, T- or
  X-learners on a past randomised campaign, read the Qini curve and AUUC, get a budgeted treat list
  that leaves the sleeping dogs alone, and measure a campaign's incremental conversions once its outcomes mature
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
│   │   ├── explain.py            # feature importance + per-row SHAP reasons + the Details beeswarm
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
make setup EXTRAS=local-embeddings   # adds sentence-transformers: the document assistant's local BAAI/bge-small-en-v1.5 embeddings (DEC-1263)
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

`scripts/seed_demo.py` seeds a synthetic "Demo Company" through the platform's own API: raw tables
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
make demo-seed                 # seed Demo Company into MARKETING_AI_DATA_DIR (or data/); trains once
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
<!-- ---- PLAN-G (agents) — append only below this line ---- -->
## Plan G — Use-case agents, Guided setup (M70–M77)

[`docs/plans/MARKETING_AI_PLAN_G_AGENTS.md`](docs/plans/MARKETING_AI_PLAN_G_AGENTS.md) adds a
helper to every use case that trains a model. On the use-case page a *Guided setup* tab sits beside
today's *Manual setup*: the helper checks the uploaded file, suggests fixes (hide ID, empty and
personal-data columns, turn `"₹1,200"` into 1200, one date format, `Y/N` into 1/0) and recommended
settings, each with a plain reason, and asks when it is unsure. Nothing is applied until the user
approves. The fixes are a *recipe* our code runs on a copy, stored with the model and replayed on
every later file. The first screen and Manual setup do not change.

**Done so far.**

- **M70 — decisions, contracts, config.** Every use case carries an `agent:` block (defaults in
  `configs/engine.yaml`; a goal, facts and outcome-column synonyms in each trainable use case),
  validated by `engine/agent/config.py` and never overridable per run. `engine/agent/contracts.py`
  defines the session, proposals, questions, the recipe and its receipt. Tests:
  `tests/unit/agent/`.
- **M71 — read and check tools, dry-run validation.** The helper's first tools wrap what exists:
  the profile, one column in detail (with how the outcome rate moves across its values), the roles
  the use case's hints point at, and the Run button's own checks. `engine/agent/formats.py` finds
  numbers, dates and yes/no values stored as text, and one category spelled several ways, with the
  parsers the recipe will run. `POST /uploads/{id}/checks` answers what Run would say without
  starting a run (`tests/integration/agent/`).
- **M72 — the recipe engine and scoring replay.** `engine/agent/recipe.py` checks and runs approved
  steps on a copy, in a fixed order, counting what each changed and could not read; a recipe never
  touches the ID or the outcome. The prepared copy is stored as a new upload and trains like any
  other; its recipe is saved with the run, and `POST /runs` prepares every later scoring file the
  same way, or answers 409 with `RECIPE_COLUMN_MISSING`, `RECIPE_VALUES_UNCONVERTED` or
  `RECIPE_STEP_INVALID`.
- **M73 — the advisor.** `engine/agent/advisor.py` turns what the tools found into proposals,
  questions and stops by fixed rules, with no AI service: the ID and outcome columns, one fix per
  formatting problem, a question for a possible leak or a date that could be read two ways, and a
  plain stop when the data cannot work. `engine/agent/recommend.py` suggests settings from one
  measured fact each and never loosens a safeguard. `tests/fixtures/agent_bench/` pins the advice
  for sixteen files, including a deliberately messy one.
- **M74 — sessions, chat and Approve.** `/uploads/{id}/agent-session` starts Guided setup (rules
  only), records decisions and answers, answers questions in a chat that can only read the data
  and suggest settings, previews the prepared rows, and on Approve prepares the copy and returns
  the ID, outcome and settings for the ordinary Run. Every number in a chat reply must come from
  the data the helper looked at, or the reply is replaced (`engine/agent/loop.py`,
  `engine/agent/grounding.py`).
- **M75 — Guided setup on screen.** Predictive and hybrid use cases get a "Guided setup
  (recommended)" tab beside Manual setup, which is unchanged and still opens first. Upload a file,
  answer the helper's questions, tick or untick its grouped suggestions, preview the changed rows,
  Approve, then Run with the usual Run button. Chat replies are plain text and show the data they
  came from; without Product AI the chat says so and the suggestions still work (`ui/modules/agent/`,
  `tests/integration/agent/test_guided_setup_ui.py`).
- **M76 — order logs are combined.** Where a use case allows it (Retail Win-back today), a file
  with several rows per customer is no longer refused: the helper asks to combine them into one
  row per customer, built only from rows on or before each customer's snapshot date and checked for
  future-data leaks, and next month's file is combined the same way before scoring
  (`engine/agent/reshape.py`).
- **M77 — hardened and measured.** The chat only states numbers it measured; file content is cleaned
  and treated as data in the prompt; chat messages are masked; sessions are budgeted, bounded and
  safe under concurrent requests; seventeen review findings are fixed with tests. A 1,000,000-row
  file is prepared in seconds and advised in about two minutes. How it all works:
  [`docs/AGENTS.md`](docs/AGENTS.md); timings: `reports/plan_g_performance.md`.
- **Adversarial review.** A second review had each finding reproduced by independent reviewers
  before it counted; 37 were confirmed and fixed with tests, in five areas: number and date
  parsing (no silent 1000x misreads, zoned dates in UTC), combining rows (no guessed day/month
  order), the helper's advice (settings that only work together are decided together), the chat
  (no 500s, no numbers laundered through tool calls) and scoring (one rule for which file a model
  reads, the same answer from the dry run and Run). DEC-1034 … DEC-1045.

Decisions are DEC-1000 … DEC-1055 in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Plan H — One simple product (M80–M84)

[`docs/plans/MARKETING_AI_PLAN_H_SIMPLE.md`](docs/plans/MARKETING_AI_PLAN_H_SIMPLE.md) keeps the engine
and hides the complexity: one generic journey, four pages (Home, Connections, Results, Settings) and a
three-step flow, plus a fourth step after a campaign.

> **Start here if you are a marketer: [`docs/START_HERE.md`](docs/START_HERE.md)**, the four pages
> and the flow in plain words, with screenshots.

- **M84 — Checks and docs.** `docs/START_HERE.md` walks a marketer through the product with
  screenshots of the new flow (`docs/screenshots/start_here/`, re-taken with
  `python -m scripts.capture_start_here`). `docs/UI_AUDIT.md` §8 lists every screen after Plan H,
  the older screens still reachable by URL, and the rough edges still open. Two fixes came out of
  it: the AI service screen highlights Connections in the top bar, and two first requests on an
  empty data folder no longer race to create the database tables (a 500 on a fresh install).
  Every other rough edge the audit listed is now fixed too: the "Also: target with uplift" link is
  gone from use cases (uplift is step 4; the workbench is under Settings → Advanced), breadcrumbs
  start with the page the top bar marks, a run opened from Results stays under Results, the Results
  badge updates without a reload, Manual setup can also pick data from a connection, and the
  helper quotes values one way (DEC-1117 … DEC-1119).

- **Configurable AI service (DEC-1140 … DEC-1149).** Connections → AI service has two settings: **Product AI**
  (the Guided-setup chat, your team's tool) and **Deliverable AI** (what the customer receives: document
  assistant, summaries, campaign copy and their checks), each with its own provider, model, encrypted key
  and test. There is no practice mode: with nothing connected the features that need a model answer
  `409 AI_NOT_CONNECTED` naming the setting, and Guided setup's rules keep working. `engine/ai_service.py`
  (registry, storage, resolution), `engine/llm_http.py` (OpenAI-compatible and Anthropic clients),
  `api/routes/ai_service.py` (`/ai-service`), `docs/CONNECTIONS.md`.

- **M83 — Measure a campaign (step 4).** On a scoring run's Results, upload who responded (customer
  ID and a 1/0 column) to see how many extra conversions the campaign caused compared with the
  customers held back, in one plain sentence. "Learn who to contact next time" then trains an uplift
  model from it and gives its contact list. Offered for every use case that contacts customers
  (`actions.contacts_customers`); see `docs/UPLIFT.md` §9.

- **M81–M82 — one journey, four pages.** Home shows one journey for every business (the customer lifecycle:
  Awareness, Onboarding, Service, Churn, Win-back) with no industry or client
  picker, and nothing is pre-loaded unless a developer runs `make demo-seed`. The top bar is Home ·
  Connections · Results · Settings. Results lists every run, with a badge when models wait for
  approval; Settings holds the AI service, privacy, schedules and the advanced tools. Setup opens on
  Guided setup. Every older screen still opens by its URL.

- **M80 — Connections.** `#/connections` connects Amazon S3 or S3-compatible storage (Google Cloud
  Storage, Cloudflare R2, MinIO), PostgreSQL / Redshift and MySQL / MariaDB, with Snowflake, BigQuery
  and Azure Blob as optional add-ons, plus the AI service. Each connection is tested step by step
  with a plain fix for every failure; Guided setup can then pick a table or file, imported as an
  ordinary upload. Passwords and keys are encrypted with `MARKETING_AI_CONNECTIONS_KEY` (generated
  on a laptop, required in production) and never returned. See `docs/CONNECTIONS.md`.

- **The helper looks at your data.** In Guided setup the chat can look at the actual data with eight
  read-only tools (sample rows, value counts, searches, number and date profiles, comparing two columns,
  empty-data patterns, repeats) instead of guessing from a few examples. Everything it is shown passes one
  default-deny gate: personal details our checks recognise are masked, columns you list in
  `agent.always_hide_columns` are never shown, and `agent.ai_data_access: summaries_only` shows only the
  shape of values (`Aaaaa 9999`). Under each reply, "What the AI looked at" lists what was sent. A separate
  review attacked the masking and its ~34 findings are fixed with tests; what pattern masking cannot catch
  (a name inside a sentence in a column nobody marked) is documented in `docs/AGENTS.md` §7.7 and §11
  (DEC-1046 … DEC-1055).

Decisions are DEC-1100 … DEC-1121 and DEC-1130 … DEC-1135.

- **Ask your data (Plan I).** At the end of a run's Data page, ask the data helper about the file the run
  used ("Churn rate by state", "Who are the top 10% spenders?"). It is Guided setup's helper in a
  read-only session: the same tools, masking and number checks, and it cannot suggest or change anything.
  A new tool, `rate_by`, groups the rows and measures each group (rows, rate of an outcome, or a mean),
  showing no figure for a group of fewer than 10 rows; when the helper used it, the reply carries a bar
  chart drawn from the tool's own numbers. Routes `GET`/`DELETE /uploads/{id}/ask` and
  `POST /uploads/{id}/ask/messages`; see `docs/AGENTS.md` §12 (DEC-1250 … DEC-1258).

<!-- ---- END PLAN-G ---- -->

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

<!-- ---- PLAN-J (product) — append only below this line ---- -->

## Plan J — Product value first (M90–M111)

[`docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md`](docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md) makes the product itself
better at deciding and proving before it is connected to anyone's tools: a control group that stays fixed per
customer, a test planned and registered before it runs, one record and one measurement per campaign, 95% ranges
that are tested to cover 95%, net-value ranking, the choice of offer and channel, one action per customer, and a
finance-ready Value Proof Pack, all validated on real public randomised data. Until integration (after the manager
review) the hand-off is a downloaded treat list. Its decisions are DEC-1300 … DEC-1399 in
[`docs/DECISIONS.md`](docs/DECISIONS.md), one entry per milestone with lettered sub-decisions; its working notes are
in [`docs/DECIDE.md`](docs/DECIDE.md).

| # | Milestone | Definition of done | Status | Tests that prove it |
|---|---|---|---|---|
| M90 | Plan J set-up | DEC-1300…1399 claimed; a PLAN-J block in the 12 shared files and in the marker test's `PHASES`; the stage-function amendment of protocol §3 (pending the owner's sign-off); `PLAN_J_CODES` and the `known_codes()` hook; the `statistical` marker and `make test-statistical`, kept out of `make test` and `make test-all`; `docs/DECIDE.md` skeleton | **done** | `tests/unit/test_shared_file_markers.py`, `tests/unit/decide/test_plan_j_codes.py`, `tests/unit/decide/test_statistical_collection.py` |
| M91 | Fix what the research and the code review found | Uplift runs consult the consent ledger; every contacting use case has a consent purpose; a one-way SMS sender gets an opt-out link instead of "Reply STOP"; copy approval records the signed-in person; row-level downloads need Analyst and are audited | **done** | `tests/integration/uplift/test_uplift_consent.py`, `tests/unit/production/test_privacy_purpose_coverage.py`, `tests/unit/generative/test_sms_one_way.py`, `tests/integration/test_api_generative_copy_identity.py`, `tests/integration/decide/test_row_level_downloads.py` |
| M92 | Persistent holdout and explore slice | `actions.holdout.scope` run (default, unchanged) / use_case / universal: a salted hash-threshold control group that never moves between runs and nests across shares; `actions.explore_fraction` (0–10%) marks eligible, non-selected, non-sleeping-dog customers with a re-drawn second hash; `holdout_assignment.parquet` with the logging propensity off-policy evaluation needs (engaged runs only); salt fingerprint and epoch ledger in `platform_setting`; `GET /holdout` (Viewer), `PUT /holdout` (Admin, audited); a generated `marketing-ai/<env>/holdout-salt` secret on AWS (DEC-1302) | **done** | `tests/unit/holdout/test_assign.py`, `tests/unit/holdout/test_spec.py`, `tests/unit/holdout/test_salt.py`, `tests/unit/holdout/test_holdout_flow.py`, `tests/integration/holdout/test_holdout_api.py`, `tests/integration/holdout/test_holdout_uplift_flow.py` |
| M93 | Plan the test, define the outcome well | test planner (MDE, sample size, holdout cost, explore cost; null with a reason, never 0; direction follows the campaign's aim), `POST /measurement/power-preview` (Viewer, counts only), lapse labels with `grace_days` and `exclude_roles` (censoring by the earliest table end, customers-left-out count, leak probe on every grace day), readiness sections "The outcome, checked" (monthly `LABEL_RATE_UNSTABLE`), "Can we measure it?", "Past campaigns" (treatment-history check and sufficiency verdict) | **done** | `tests/unit/measurement/test_planner.py`, `tests/unit/measurement/test_lapse_labels.py`, `tests/unit/measurement/test_readiness_planning.py`, `tests/unit/measurement/test_treatment_history.py`, `tests/unit/measurement/test_neutral_defaults.py`, `tests/unit/measurement/test_codes.py`, `tests/integration/measurement/test_grace_window_leak.py`, `tests/integration/measurement/test_power_preview_route.py`, `tests/integration/measurement/test_readiness_measure.py` |
| M94 | One campaign record, one measurement path, a registered test plan | A campaign recorded once and measured through one path (`measure_campaign`, which `/runs/{id}/campaign-results` and step 4 also use); a registered, hashed test plan whose amendments are kept and which is checked at measurement (`TEST_PLAN_CHANGED`, with erasure allowed for); early looks labelled, with no verdict; no partial number before outcomes mature (`CAMPAIGN_NOT_MATURED`) and no measurement as of a future moment; campaign rows covered by erasure and retention; at integration, the "Plan the test" slider over M93's planner (`GET /campaigns/{id}/plan-preview`, computed points only) and M92's holdout epochs (`CAMPAIGN_EPOCH_MISMATCH`); at M95's integration, the assignment's explore flag and probability from M92's `holdout_assignment.parquet` | **done** | `tests/unit/measurement/test_campaign.py`, `tests/unit/measurement/test_campaign_migration.py`, `tests/unit/measurement/test_campaign_privacy.py`, `tests/unit/measurement/test_measure.py`, `tests/unit/measurement/test_plan.py`, `tests/unit/measurement/test_roi_campaign.py`, `tests/unit/measurement/test_campaign_epochs.py`, `tests/integration/measurement/test_campaigns_api.py`, `tests/integration/measurement/test_campaign_access.py`, `tests/integration/measurement/test_campaign_direction.py`, `tests/integration/measurement/test_campaign_erasure.py`, `tests/integration/measurement/test_campaign_ui.py`, `tests/integration/measurement/test_campaign_epochs.py`, `tests/integration/measurement/test_plan_preview.py`, `tests/integration/measurement/test_campaign_explore.py`, `tests/integration/production/ui/measurement/campaigns.test.mjs` |
| M95 | Validity harness and synthetic quarantine | Nightly tests (`make test-statistical`, never `make test`) that the 95% intervals cover 95% at base rates 2%, 5% and 20% and under partial compliance, that a campaign with no effect is called effective 5% of the time, that leaving immature rows out leaves the lift unbiased, and that the planner's n delivers its 80% power, each within four Monte Carlo standard errors; `engine/measurement/simulate.py` builds the campaigns with a known effect; a planted effect can never be presented as a result: `RunRecord.synthetic` and `UploadRecord.synthetic` follow generated data into runs (uploads, the demo's datasets, scheduled firings, derived uploads, "Learn from this campaign") and every results and value report drawn from one, HTML and PDF, carries "Synthetic data: planted effect, not a forecast" | **done** | `tests/unit/measurement/test_simulate.py`, `tests/unit/pilot/test_synthetic_quarantine.py`, `tests/unit/test_docs_honesty.py`, `tests/unit/production/test_firing.py`, `tests/integration/test_synthetic_runs.py`, `tests/integration/uplift/test_measure_campaign.py`, `tests/integration/pilot/test_pilot_acceptance.py`, `tests/statistical/test_interval_coverage.py`, `tests/statistical/test_false_positive_rate.py`, `tests/statistical/test_immature_bias.py`, `tests/statistical/test_power_at_planner_n.py` |
| M96 | Uplift must earn its place: stability, calibration and the beats-risk check | The uplift evaluation measures whether ranking by uplift beats plain risk ranking: a paired bootstrap of the AUUC difference against the approved propensity model or p_control, with seeded tie-breaking. It also compares predicted with observed uplift by decile, and, opt-in, the fold AUUC with per-fold intervals judged against noise. These appear as advisory checks on the Approvals screen (UPLIFT_NOT_BETTER_THAN_RISK, UPLIFT_UNSTABLE_ACROSS_FOLDS, UPLIFT_MISCALIBRATED); the champion rule is unchanged. A scoring run of a model that failed the check ranks its contact list by the approved propensity model at the same number of contacts and writes ranking_choice.json with the reason (J5). An opt-in equal-budget comparison of uplift top-N against risk top-N (ring cross-fit, OPE with recorded propensities, per rupee) is covered by a nightly coverage test | **done** | `tests/unit/uplift/test_beats_risk.py`, `tests/unit/uplift/test_dr_effect_terms.py`, `tests/unit/measurement/test_compare.py`, `tests/unit/decide/test_model_gates.py`, `tests/unit/decide/test_ranking_choice.py`, `tests/unit/decide/test_ranking_banner_js.py`, `tests/unit/decide/ranking_banner.test.mjs`, `tests/integration/decide/test_uplift_ranking_flow.py`, `tests/integration/production/ui/approvals/checks.test.mjs`, `tests/statistical/test_risk_comparison_coverage.py` |
| M97 | Rank by net value | Customers ranked by expected net money: uplift × value × margin × horizon − offer cost × p_treated − contact cost (opt in with `uplift.policy.value_column`, plus `margin_pct`, `horizon_months` and `min_roi`). Contact and offer costs come from `cost_per_contact` or the editable India defaults in `configs/pilot/value.yaml`, read once per run and recorded. A customer below min_roi × its own cost is left out wherever it ranks. Expected conversions come from the hold-out; money comes from the value-weighted hold-out ranked the same way, with honest ranges. Money fields are null with a plain reason when value inputs are missing, and nothing is filled in. The budget curve replays the recorded costs, and its point at the budget equals the recommendation in every money configuration. Propensity runs write expected gross value labelled "not incremental". Default configurations are unchanged. Built by the partner agent (`plan-j/m97-net-value`, 19e5a71), then fixed by Claude Code after two review rounds | **done** | `tests/unit/uplift/test_net_value.py`, `tests/unit/uplift/test_profit_curve.py`, `tests/unit/decide/test_gross_value.py`, `tests/unit/pilot/test_roi.py`, `tests/unit/uplift/uplift_ui.test.mjs`, `tests/integration/uplift/test_net_value_flow.py` |
| M98 | The treat list: one file with everything, reasons in business words | One downloadable file per scoring run (`engine/decide/treat_list.py`: `treat_list.csv`, `.parquet` and a summary), built on first request from the run's own artefacts and its own `run_config.json`, joined to the scores on every key column, never by position: `treat` is M92's `treated` column, `holdout` and `explore` come from `holdout_assignment.parquet` (blank, with the summary's counts null too, when the run wrote none), `net_value` is M97's incremental value on an uplift run and `expected_gross_value` (not incremental) on a propensity run, and offer follows treat. Reasons in business words (`configs/decide/reasons.yaml`, `engine/decide/reasons.py`): each phrase prints the row's own value and says which way it moved the score, never a trend or a number of its own. Row-level registration, Analyst-only and audited (`GET /runs/{id}/treat_list.csv`); a file retention removed is rebuilt or refused in plain words (`409 RUN_NOT_SCORED`). The treat list card and "Download treat list (CSV)" on the Output page of propensity and uplift scoring runs, apart from the contact list. Default runs byte-identical. Built by the partner agent (`plan-j/m98-treat-list`, 3d8a71a), then fixed by Claude Code after two review rounds | **done** | `tests/unit/decide/test_treat_list.py`, `tests/unit/decide/test_treat_list_scale.py`, `tests/unit/decide/test_treat_list_card_js.py`, `tests/unit/decide/treat_list_card.test.mjs`, `tests/unit/decide/treat_list_page.test.mjs`, `tests/unit/decide/test_m98_handoff.py`, `tests/unit/holdout/test_selection_masks.py`, `tests/unit/agent/test_reason_wording.py`, `tests/integration/decide/test_treat_list_flow.py`, `tests/integration/decide/test_treat_list_golden.py`, `tests/integration/decide/test_treat_list_review.py`, `tests/integration/decide/test_treat_list_real_run.py`, `tests/integration/decide/test_treat_list_real_propensity.py` |
| M99 | Offer and channel catalogue, channel-aware consent | An optional action catalogue, `configs/decide/catalogue.yaml` (none ships; `configs/decide/catalogue.example.yaml` is a labelled example that is never read), declares actions with their channels in order of preference, offer and contact costs and DLT details; bands and campaign policies point at them by `action_id` and `treat_action_id`, and an unknown id fails config load (`CATALOGUE_ACTION_UNKNOWN`). Its costs take over M97's value lookup for its channels. Region rules are data (`configs/regions/in.yaml`: an SMS action needs a registered DLT template, `ACTION_DLT_TEMPLATE_MISSING`). `actions.suppression.channels` sets consent per channel: a customer opted out of SMS stays in for email, and a customer contactable on no planned channel is not treated but never suppressed; the three suppression reasons and their precedence are unchanged. Each run with channels writes `channel_contactability.parquet` (row-level, Analyst-only) and `.json`, plus `catalogue_stamp.json` when a catalogue exists, so the treat list plans channels from the catalogue the run ran under; the treat list fills `channel` and `contactable_channels` and stamps `catalogue_sha256`. The consent ledger accepts a per-channel record (Alembic `0007`; old records apply to every channel) and refuses a channel that is not a channel name (`CONSENT_CHANNEL_INVALID`). Defaults unchanged, except that every treat list gains an empty `contactable_channels` column and `catalogue_sha256: null`. Built by the partner agent (`plan-j/m99-catalogue`, 01e923e), then fixed by Claude Code after two review rounds | **done** | `tests/unit/decide/test_catalogue.py`, `tests/unit/decide/test_contactability.py`, `tests/unit/decide/test_m99_handoff.py`, `tests/unit/production/test_consent_channel_migration.py`, `tests/integration/decide/test_catalogue_consent.py`, `tests/integration/decide/test_channel_consent_real_run.py`, `tests/integration/decide/test_channel_consent_real_propensity.py` |
| M100 | Choose the offer: multi-treatment uplift | Several offers plus no offer: each customer gets the offer they respond to, within budget and contactable channels; sleeping dogs get none. Part A: `uplift.treatment_levels` (control first; empty keeps every default artefact byte-identical) learns, checks, evaluates and measures several offers against one shared control. Each report gains `arms`, and every existing field stays the first offer against the control (DEC-668 (3)). `arm_policy_value.json` compares the best offer per customer with the first offer alone (IPW, paired bootstrap within arms). `scores.parquet` gains `uplift_arm_<k>`/`p_treated_arm_<k>`; `scores.csv` is unchanged. `measure_campaign` measures each configured offer. `engine/decide/offer_choice.py` chooses the offer per customer as a pure function (net value, eligibility, sleeping dogs, budget). A model of several offers is never champion (`MULTI_ARM_PROMOTION_REFUSED`, by the run or by hand; the guard fails closed). Part B: a scoring run of a model of several offers chooses each customer's offer after the actions stage (`engine/decide/offer_run.py`). Each offer is priced with its own catalogue action (`uplift.policy.arm_action_ids`, from the run's `catalogue_stamp.json`) or M97's value settings. Eligibility is per offer: not suppressed, not held back, contactable on one of its channels. A channel the use case does not configure is open only to all-channel consent. Sleeping dogs are excluded per offer, and `uplift.policy.total_budget` (rupees) and `budget_contacts` both hold. The run writes `offer_choice.parquet` (row-level) and `offer_choice.json`. The treat list gains `runner_up_offer`, `runner_up_net_value` and `offer_reason`; its summary gains `offer_counts`, `channel_rows` and `explore_offer_counts`/`explore_cost` (exploration is outside the budget). The M99 gaps are closed: channel-only consent passes the gate when channels are configured; channel consent columns are never model inputs (`CHANNEL_COLUMN_MODEL_INPUT` refuses an older model that has one); every creator of a run, scheduled firings included, stamps the catalogue it checked. With no value set, no offer is chosen (`OFFER_CHOICE_NOT_MADE`). Both parts built by Claude Code | **done** | `tests/unit/decide/test_multi_arm_model.py`, `tests/unit/decide/test_offer_choice.py`, `tests/unit/decide/test_offer_choice_scale.py`, `tests/unit/decide/test_arms_card_js.py`, `tests/unit/decide/arms_card.test.mjs`, `tests/unit/measurement/test_arms.py`, `tests/integration/decide/test_multi_arm_flow.py`, `tests/integration/decide/test_m100_binary_identity.py`, `tests/statistical/test_arm_interval_coverage.py`, `tests/unit/decide/test_offer_run.py`, `tests/unit/decide/test_offer_run_scale.py`, `tests/unit/decide/test_channel_gaps.py`, `tests/unit/decide/test_scheduled_catalogue_stamp.py`, `tests/unit/decide/test_offer_choice_card_js.py`, `tests/unit/decide/offer_choice_card.test.mjs`, `tests/integration/decide/test_offer_choice_run.py`, `tests/integration/decide/test_offer_choice_explore.py`, `tests/integration/decide/test_offer_choice_binary.py`, `tests/integration/decide/test_channel_gate_real_run.py` |
| M101 | One action per customer across use cases | No customer gets two actions in one cycle; the winner has the highest priority × value; contact caps and holdouts respected. `POST /decide/arbitrate` (Analyst) arbitrates the latest finished scoring run of each use case (by finish time, two runs of one use case refused) over the same customer key: hold-out members are never treated by any use case, explore rows keep their action, then priority × value decides when every candidate carries the same kind of value (incremental or gross, never mixed, never filled in), else priority alone, then the request order; `contact_cap_per_customer` (default 1) and `channel_caps` (default none) come from `decide/arbitration.yaml` in the config root (none ships; `configs/decide/arbitration.example.yaml` is never read), and a file that is there but cannot be read is refused (422 `ARBITRATION_CONFIG_INVALID`), not replaced by defaults. Each row says what won and what lost (`winning_use_case`, `arbitration_reason`, `losing_actions`); with one use case selected the output equals its treat list. `arbitrated_treat_list.csv`/`.parquet` are written only into each participating run (row-level, Analyst-only, audited; no copy at the store root) with the aggregate `arbitration_summary.json`; the Results page shows the conflicts card. One campaign per use case whose two arms are cut by the same rule (the arbitration re-run as if no one were held back), with `POST /campaigns`' start and window; posting the same arbitration twice makes one set of campaigns. Built by the partner agent (`plan-j/m101-arbitration`, 98b2959), then fixed by Claude Code after two review rounds. Follow-up: arbitration also protects a use case's own per-run control group (not only M92 hold-out): the treat list carries an additive `control_group` column, no other use case treats a control customer, the row names the use case in `control_use_cases`, and the campaign arms follow DEC-1311 (n). DEC-1311 (al)-(aq) | **done** | `tests/unit/decide/test_arbitrate.py`, `tests/unit/decide/test_arbitrate_real_runs.py`, `tests/unit/decide/test_arbitrate_scale.py`, `tests/unit/decide/test_arbitrate_second_review.py`, `tests/unit/decide/test_arbitrate_control_group.py`, `tests/unit/decide/test_conflicts_card_js.py`, `tests/unit/decide/conflicts_card.test.mjs`, `tests/integration/decide/test_arbitration_flow.py`, `tests/integration/decide/test_arbitration_review.py`, `tests/integration/decide/test_arbitration_second_review.py`, `tests/integration/decide/test_control_group_arbitration.py` |
| M102 | Revenue outcomes and CUPED | Revenue measured with intervals; CUPED shrinks the holdout needed; binary outcomes unchanged. A campaign, or a run's own measurement, can be measured on an amount per customer (`outcome_kind: continuous`): the registered test plan, `POST /campaigns/{id}/measure`, `POST /runs/{id}/measure` and `POST /runs/{id}/campaign-results`. The report gives the two averages, their difference with Welch's interval and p-value, and the relative change. It can be adjusted by a pre-campaign amount the test plan registered (CUPED: `adjusted_lift`, `adjusted_interval`, `variance_reduction` about rho²; the unadjusted estimate is always shown beside it), and that amount must arrive with the outcomes dated before the day of contact (422 `COVARIATE_NOT_BEFORE_CAMPAIGN`, nothing stored). Long-tailed amounts below Kohavi's 355·g² customers per arm keep their numbers and carry `OUTCOME_SKEWED`; nothing is capped. The planner (`mde_continuous`, `n_for_mde_continuous`, `achieved_power_continuous`), the plan slider (`mde_amount`) and the value view handle amounts. Several offers stay yes/no only. Yes/no reports are byte-identical (pinned against reports recorded on bc38024). Built by Claude Code after one review round with two lenses | **done** | `tests/unit/measurement/test_continuous.py`, `tests/unit/measurement/test_planner_continuous.py`, `tests/unit/measurement/test_roi_amount.py`, `tests/unit/measurement/test_amounts_scale.py`, `tests/unit/measurement/test_m102_binary_identity.py`, `tests/integration/measurement/test_campaign_amounts.py`, `tests/statistical/test_continuous_coverage.py` |
| M103 | Audit any campaign, and the programme readout | Upload a past campaign's assignment and outcomes for an audit readout, labelled causal only for random assignment; a programme-level readout. `POST /campaigns/audit` (Analyst): an assignment file and an outcomes file make an `external` campaign measured by the one measurement path, with no run record; an audit of a propensity run's own groups reproduces its report field for field. The result is labelled **Causal** only when the engine could not tell the groups apart from the customer details in the file, **Random by your statement, not verified** when it could not test, and **Descriptive only** otherwise; a random-by-statement result never says the campaign added or caused anything, and a descriptive one has no verdict. A contact file for any campaign (`POST /campaigns/{id}/contacts`, or beside an audit or a programme) gives the contact rate and the contamination of the held-back group exactly, and the effect on the contacted as a labelled secondary result with Fieller's interval (withheld when the contact rates differ by no more than chance). `POST /campaigns/programme` (Analyst) reads everyone not held back against the universal holdout over a period, intent to treat; it is refused when the holdout began or was redrawn after the period began (409 `CAMPAIGN_EPOCH_MISMATCH`) and when it carries a plan or an earlier amount (no adjusted estimate: a finished period cannot pre-register one), with notes for lists scored without the universal holdout and for a held-back share far from the rule. An audited or programme amount stays an amount when measured again. Three codes (`AUDIT_ARM_UNREADABLE`, `CONTACT_FILE_UNREADABLE`, `PROGRAMME_NO_HOLDOUT`). Built by Claude Code after one review round with two lenses. DEC-1313 | **done** | `tests/unit/measurement/test_audit.py`, `tests/unit/measurement/test_reconcile.py`, `tests/unit/measurement/test_programme.py`, `tests/unit/measurement/test_audit_scaling.py`, `tests/unit/measurement/test_audit_view.py`, `tests/unit/measurement/audit_view.test.mjs`, `tests/unit/measurement/test_campaign_privacy.py`, `tests/integration/measurement/test_campaign_audit.py`, `tests/integration/measurement/test_campaign_contacts.py`, `tests/integration/measurement/test_programme_readout.py`, `tests/integration/measurement/test_audit_trail.py`, `tests/integration/measurement/test_campaign_access.py`, `tests/statistical/test_complier_coverage.py` |
| M104 | The Value Proof Pack | A finance-ready pack in which every number traces to a measured artefact; backfiring segments flagged; no row-level data. `GET /pilot/proof/{campaign_id}` (html, pdf or json; Viewer) gives a ten-section pack for finance in which every number is a traced figure, re-verified before it is shown. That covers digits in the pack's own words, on the page and in the PDF. Claims follow the causal basis: proven, random by your statement, or descriptive only. Groups that backfired are flagged under a family-wise guard; an Analyst approves leaving them out next cycle (`POST /pilot/proof/{campaign_id}/suppressions`, audited, never applied). The net value is a rupee range from the measured (or adjusted) interval and the value inputs (`PUT /pilot/proof/{campaign_id}/value`). A programme readout prices no contacts or offers. Generated data, including a generated contact file, is refused (`PROOF_SYNTHETIC_DATA`), and so is an immature result (`PROOF_NOT_MATURE`); a number that does not trace back is `PROOF_NOT_TRACEABLE` and approving a group the pack does not flag is `PROOF_SUPPRESSION_INVALID`. The pack reads no row-level data. Built by Claude Code after one review round with two lenses. Review fix (DEC-1314 (r)-(x)): with several offers the Pack's headline, extra outcomes, value and net value add every offer together against the shared control. They are read from the measurement's own pooled comparison (`offers_combined`, whose interval counts the control once), while the costs remain every offer's. A report measured before that comparison states the first offer's scope and costs only that offer. A Pack of one offer is byte-identical (five stored packs). On Hillstrom the conversion Pack now reads +212 extra conversions, every offer together (150 to 270). The finding raised by M110a (DEC-1322 (g)) is fixed. The fix was built by Claude Code after one review round with two lenses. DEC-1314 | **done** | `tests/unit/pilot/test_proof.py`, `tests/unit/measurement/test_segments.py`, `tests/integration/pilot/test_proof_pack.py`, `tests/integration/pilot/test_proof_pack_programme.py`, `tests/integration/pilot/test_proof_pack_offers.py`, `tests/integration/pilot/test_proof_pack_amounts.py`, `tests/statistical/test_backfire_false_alarm.py`, `tests/integration/production/ui/simple/proof.test.mjs`, `tests/integration/pilot/test_proof_pack_several_offers.py`, `tests/unit/pilot/test_proof_single_offer_golden.py`, `tests/unit/measurement/test_arms.py`, `tests/statistical/test_offers_combined_coverage.py` |
| M105 | Warnings and proven value to date | Cards for campaigns with no control, early looks, backfiring segments and fading effects; the proven value to date. `GET /campaigns/summary` (Viewer) gives the Results page its cards (no control group, early look, underpowered plan, contaminated control group, drift, a model waiting for approval, a group that backfired, uplift not beating risk, effect fading), each shown only while its condition holds, and the value proven to date: the sum of measured lower bounds over engine-random or verified-random final campaigns, with customers counted once (campaigns on the same customers add one lower bound), every campaign read, one total per outcome column and one in rupees. Stated-random, descriptive, programme, generated, unfinished and no-control campaigns are listed apart or excluded with their reason. The effect is fading only when at least three cycles' weighted trend lies wholly below zero (2.5% false alarms, nightly). Additive and read-only; every number is a traced figure (`SUMMARY_NOT_TRACEABLE` otherwise). Built by Claude Code after one review round with two lenses. DEC-1315 | **done** | `tests/unit/measurement/test_summary.py`, `tests/integration/measurement/test_campaign_summary.py`, `tests/integration/measurement/test_campaign_summary_cycles.py`, `tests/integration/measurement/test_campaign_summary_units.py`, `tests/integration/measurement/test_campaign_summary_scale.py`, `tests/statistical/test_fading_false_alarm.py`, `tests/integration/production/ui/simple/summary.test.mjs` |
| M106 | Learn from the last cycle | The next model learns from the last cycle's holdout and explore slice; a calibration block. On a scoring run that engaged the holdout service, step 4's "Learn who to contact next time" learns only from the last cycle's randomised rows (the list plus the explore share), each with a recorded chance of contact strictly between 0 and 1, balanced to an even chance in each group so the frame passes `TREATMENT_NOT_RANDOM`; everyone left out is counted by reason in `learned_from.json`. `LEARN_NO_OVERLAP` (409) refuses a cycle with no explore share and an outcomes file that undoes the randomisation (uneven coverage of contacted and not-contacted customers, or too few outside the list). The learned model always waits for an Approver, who sees predicted against measured change by tenth for the model that chose the last list (`ApprovalItem.live_calibration`). `POST /uplift/runs` takes a `dataset_id` and, with a cost cap set, passes the same cost gate and watcher as `POST /runs`. Every other run learns exactly as before. Built by Claude Code after one review round with two lenses. DEC-1316 | **done** | `tests/unit/measurement/test_learn.py`, `tests/integration/measurement/test_learn_from_cycle.py`, `tests/integration/measurement/test_learn_default_unchanged.py`, `tests/integration/uplift/test_uplift_dataset_run.py`, `tests/integration/decide/test_uplift_run_cost.py`, `tests/integration/production/ui/approvals/live_calibration.test.mjs` |
| M107 | The monthly loop, read-only | The monthly cycle runs on schedule from saved connections (read-only) with only the approval left to a person. Three new schedule kinds after `score` (`treat_list`, `measure`, `learn`), each reading only what the step before wrote, carry a cycle from this month's tables to a challenger that waits for an Approver, with an `info` alert at each step (`treat_list_ready`, `campaign_measured`, `challenger_waiting`). Sources can be bound to a saved connection (`POST /clients/{id}/sources/from-connection`, Analyst): the newest CSV or Parquet file under a folder, or a table, is read again before each scheduled build, but only when the build would read that bound table, so a recipe of uploaded files reads no connection; a table read again meets the per-source row limit. Outcomes are read for the campaign's own date window (`POST /campaigns/{id}/outcomes` takes `upload_id` or `connection_id`; one quoted date-window `WHERE` on a checked column, amending DEC-1105, no free SQL, a read-only session), and consent can be read from a connection (`POST /privacy/consent/imports/from-connection`, Admin, the file route's size limit). The sibling `/from-connection` routes, in place of extending the multipart routes, are an approved deviation from the plan. A refused learn is not retried until the campaign is measured again; the learning run passes M108's cost gate only once a run is about to start. Nothing is written to a client's systems and the treat list stays a download. Built by Claude Code after one review round with two lenses. DEC-1317 | **done** | `tests/unit/measurement/test_cycle_schedules.py`, `tests/unit/measurement/test_cycle_upload.py`, `tests/unit/measurement/test_outcome_pull.py`, `tests/integration/measurement/test_bound_sources.py`, `tests/integration/measurement/test_connection_pulls_api.py`, `tests/integration/measurement/test_monthly_loop.py` |
| M108 | Cost before each run, with a cap | The cost of a run shown before it starts; a cap that refuses or stops a run. `GET /use-cases/{use_case_id}/cost-estimate` (Viewer) gives the most one run could cost at the AWS list price, in US dollars, beside the Run button; it is null with a reason when the price list, the machine's price or the time limit is missing, and rupees appear only beside an Admin-saved exchange rate with its source and date (`PUT /cost/fx-rate`, audited). An optional `governance.max_run_cost_usd` (config-only) makes `POST /runs` and scheduled firings answer `RUN_COST_NEEDS_CONFIRMATION` above it (a person may confirm; a schedule cannot), and a running job is stopped through `cancel_run` with `RUN_COST_CAP_REACHED` when its running cost passes it. `GET /cost/spend` (Viewer, `#/cost`) gives the monthly recorded spend. AI text is shown but not counted: a run never calls it (an approved departure from the plan). Opt-in: with no cap, a default `run_config.json` is byte-identical. Built by Claude Code after one review round with two lenses. DEC-1318 | **done** | `tests/unit/decide/test_run_cost.py`, `tests/unit/decide/test_scheduled_run_cost.py`, `tests/unit/decide/test_cost_card_js.py`, `tests/unit/decide/cost_card.test.mjs`, `tests/integration/decide/test_run_cost_api.py`, `tests/integration/decide/test_run_cost_defaults.py` |
| M109 | Small fixes that make existing things work better | The retail baseline, annotated drift events and reviewed suggestion benchmarks. An Analyst can note an event (a price change, a promotion, a competitor's launch, a change of rules, a season) against a scoring run's drift report: `GET /runs/{run_id}/drift-events` (Viewer), `POST` and `DELETE` (Analyst, audited without the text), kept in a new row-free artefact, `drift_annotations.json`; `drift.json`, the predict stage and the verdict are never written. An event counts as a possible reason only when dated between the model's training run and the drift measurement and only through measures that moved; it never lowers a measured change or silences an alert. The scoring Output page shows the "Events behind the change" card with add and remove, and the root-cause screen shows it read-only. Verdicts, the plan card and the power card say "detectable effect" and "early look" (sentences only, no number or verdict kind). Guided setup sizes the held-back share with the test planner for a 2-point change and proposes `actions.control_group_fraction` as a check suggestion; the agent benchmark gains one case (`mid_size_file`), the other 21 digests unchanged. Retail win-back (UCI Online Retail): no setting was changed; with 20% of the shoppers sealed first, no model family, extra column or label and window setting beats the baseline (`library/online-retail/run_report.md`). Built by Claude Code after one review round with two lenses. DEC-1319 | **done** | `tests/integration/decide/test_drift_events.py`, `tests/unit/agent/test_holdout_advice.py`, `tests/unit/decide/test_verdict_wording.py`, `tests/unit/decide/test_drift_events_card_js.py`, `tests/unit/decide/test_rca_drift_events_js.py`, `tests/unit/decide/test_planner_words_js.py`, `tests/unit/decide/drift_events_card.test.mjs`, `tests/unit/decide/rca_drift_events.test.mjs`, `tests/unit/decide/planner_words.test.mjs` |
| M110 | Validate on real public randomised data | The whole journey run on real randomised public data (Hillstrom; Criteo and others where obtained), each with a reproducible run report. The whole journey runs on Hillstrom (MineThatData, 2008: 64,000 customers randomised to a men's e-mail, a women's e-mail or no e-mail) through `python -m library.run_engine journey --dataset hillstrom-email`, every step through the product's own API, split once (seed 20261010) into 32,000 training rows and 32,000 evaluation rows used only for measurement, with both test plans registered before any evaluation outcome is read; `library/hillstrom-email/run_report.md` is rendered only from the run's results and a re-run reproduces every number (a test checks both). Results, recorded as they fell: the risk model's ROC-AUC is 0.534 and does not beat its baseline; uplift does **not** beat risk ranking, in the engine's check (whose risk model had seen 84.5% of the hold-out, recorded) or repeated out of sample on the evaluation rows for either e-mail, and it is not calibrated; the targeted list beats sending no e-mail (+0.45 pts conversion off-policy, +0.60 pts measured by the replay campaign) but **loses** to sending everyone the men's e-mail (-0.24 pts, -0.43 to -0.04). Both Value Proof Packs are built, provenance-verified and labelled "public dataset, retrospective replay, a third of each group kept". New use case `hillstrom-email` (configs only, no engine code); only a tenth-of-the-file `sample.csv` (6,401 rows) is committed and the full file is git-ignored; whether that sample may stay in git is the product owner's decision (no explicit licence). Criteo and X5 were unreachable and Lenta's licence is not approved, so they were skipped with reasons. Built by Claude Code after one review round with two lenses. DEC-1320 | **done** | `library/tests/test_hillstrom_email.py` (opt-in library test), `tests/unit/test_prototype_parity.py` (its `hillstrom-email` entry) |
| M110a | Hillstrom audit readout (closes the Phase 3 gate's real-past-campaign item) | An audit readout on at least one real past campaign; with none supplied by a client, on Hillstrom (the plan's fallback). `python -m library.run_engine audit --dataset hillstrom-email` reads Hillstrom's own 2008 randomised e-mail test through the public `POST /campaigns/audit`, as a client would: `POST /uploads` of an assignment file with all eight customer details and a separate outcomes file, then the audit with No E-Mail as the control and the two e-mails as offers. The engine itself labels it **Causal** (verified random; guessing scores 0.502 to 0.506 against the 0.60 limit), and a test shows the same route says **Descriptive only** for an assignment chosen from customer history. Effects with intervals: conversion men's e-mail +0.68 pts (145 extra, 107 to 184), women's +0.31 pts (67 extra, 32 to 101); spend +$0.60 any e-mail, +$0.77 men's, +$0.42 women's per customer, skew-flagged, with a seeded bootstrap beside the engine's interval; several offers on an amount is refused (`CAMPAIGN_INVALID`) and recorded. Four Value Proof Packs, labelled "public dataset, retrospective audit", pass provenance; the programme readout does not apply (409 `PROGRAMME_NO_HOLDOUT`, no universal hold-out). Only aggregates are committed (`library/hillstrom-email/audit.results.json`); section 8 of `run_report.md` is rendered from it. A finding for M104 (a Pack with several offers credits the first offer and charges every offer's contacts) was raised, no engine code changed; the M104 fix (DEC-1314 (r)-(x)) has since fixed it, and section 8's conversion Pack, regenerated, reads +212 extra conversions, every offer together (150 to 270). No engine, API, config or UI change. Built by Claude Code after one review round with two lenses. DEC-1322 | **done** | `library/tests/test_hillstrom_audit.py` (opt-in library test), `library/tests/test_hillstrom_email.py` |
| M111 | The manager demo | A demo script that uses only real results, rehearsed once end to end by someone outside the team. [`library/DEMO_SCRIPT.md`](library/DEMO_SCRIPT.md) tells the Hillstrom results as they fell: the list beats sending nothing (+0.45 pts off the file, +0.60 pts in the replay); on conversions it is worse than sending everyone the men's e-mail (-0.24 pts, -0.43 to -0.04), in money it is not shown to differ; uplift does not beat plain risk ranking. `python -m scripts.seed_validated` loads the validated dataset into a clean store through the product's own API (a file is validated by its SHA-256; the committed sample is labelled, and its numbers are not the validated ones). [`library/hillstrom-email/DEMO_SUMMARY.md`](library/hillstrom-email/DEMO_SUMMARY.md) is generated, never typed: every number is traced to a field of the committed aggregate `journey.results.json` and re-verified, and `python -m scripts.demo_summary --check` fails when the page drifts. The rehearsal by a person outside the team is **PENDING** ([`library/DEMO_REHEARSAL.md`](library/DEMO_REHEARSAL.md)); the agent's dry run is Part 2 of that file and is not the rehearsal. No engine, API, config or UI change. Built by Claude Code after one review round with two lenses. DEC-1321 | **done** | `tests/unit/test_demo_script.py`, `tests/unit/test_demo_summary.py`, `tests/unit/test_seed_validated.py` |

M90 changes no product behaviour. It claims the decision range, adds this block to the shared files, and sets up
the statistical suite: `make test-statistical` (also run nightly) runs `tests/statistical/`, which `make test` and
`make test-all` never collect because `tests/statistical/conftest.py` ignores it unless
`MARKETING_AI_STATISTICAL=1` (DEC-1300 (e)).

M91 (DEC-1301) fixes five defects:

- **Uplift runs and consent.** Uplift scoring runs are gated by the consent ledger just like propensity runs: a
  customer whose consent is withdrawn, expired or not recorded is suppressed as `consent_false`, and the run writes
  `consent_report.json`.
- **Consent purposes.** `configs/privacy.yaml` now lists every use case that contacts customers (`retail-win-back`,
  `bank-term-deposit`, `insurance-cross-sell` as `marketing_communication`; `card-default-propensity` as
  `account_servicing`). A use case with `actions.contacts_customers` true and no purpose is refused at config load
  with `CONSENT_PURPOSE_MISSING`; operational use cases (`contacts_customers: false`) need none.
- **One-way SMS.** `generative.campaign_copy.sms_sender: two_way` (default) `| one_way`. With `one_way` the SMS
  opt-out line is the `{{opt_out_link}}` merge field (replaced by the sending system per recipient) instead of
  "Reply STOP to opt out", and the guardrail rule `sms_reply_stop_one_way` blocks any reply-to-STOP wording
  ([`docs/GENERATIVE.md`](docs/GENERATIVE.md) §14).
- **Copy approval.** With sign-in on, approving a template records the signed-in username as `approved_by`, whatever
  the request body says. `GET /runs/{run_id}/copy_messages.csv?approved_only=true` returns only the rows of approved
  templates (status `approved`, refused renderings left out); without the parameter the download is unchanged.
  Regenerating a template re-renders its rows, so the file always matches the template text (§15).
- **Row-level downloads.** `scores.csv`, `scores.parquet`, `row_explanations.parquet` and `copy_messages.csv` need
  the Analyst role when sign-in is on, on every route that serves them, and each download writes one audit event
  (`runs.customer_rows_download` on the generic artefact route). A refusal is `403 ROLE_REQUIRED`, "Only an Analyst
  can download customer-level rows."; reports and charts stay open to Viewers, and with sign-in off nothing changes
  ([`docs/PRODUCTION.md`](docs/PRODUCTION.md) §2). With sign-in on, a Viewer sees "Download contact list (CSV)" and
  "Download messages (CSV)" disabled, with the reason beside them.

M92 (DEC-1302) makes the control group persistent, opt-in per use case:

- **Scopes.** `actions.holdout.scope: run` (the default) is today's control group, redrawn every run. `use_case` holds
  the same customers out of that use case on every run; `universal` holds the same customers out of every use case on
  it. Under a persistent scope `actions.holdout.fraction` sets the share, membership is a salted hash per customer, and
  a larger share always contains a smaller one.
- **Salt and epochs.** The salt is `MARKETING_AI_HOLDOUT_SALT` (at least 16 characters; on AWS generated once as
  `marketing-ai/<env>/holdout-salt`). A missing or changed salt, a lowered share, or universal use cases asking for
  different shares stop the run with a code (`HOLDOUT_SALT_MISSING`, `HOLDOUT_SALT_CHANGED`,
  `HOLDOUT_FRACTION_LOWERED`, `HOLDOUT_FRACTION_MISMATCH`). `GET /holdout` shows the holdouts (Viewer); an Admin
  starts a new epoch, or adopts a new salt, with `PUT /holdout` (audited).
- **Explore slice.** `actions.explore_fraction` (0–0.10, default 0) marks a re-drawn share of eligible customers the
  policy did not select, never predicted sleeping dogs. Engaged runs write `holdout_assignment.parquet` with
  `treated` and `treatment_probability` for off-policy evaluation ([`docs/UPLIFT.md`](docs/UPLIFT.md) §14); like
  the other customer-level files it is served by `GET /runs/{run_id}/artefacts/{name}` to Analysts only.

M95 (DEC-1305) shows that the measurement is honest and keeps planted data out of results:

- **Validity harness.** `engine/measurement/simulate.py` builds campaigns whose true effect is known, and
  `make test-statistical` (nightly, never in `make test` or `make test-all`) measures thousands of them through the
  product's own `measure_incrementality`: the 95% range holds the true effect 95% of the time at base rates of 2%, 5%
  and 20% and when only some contacted customers receive the offer; a campaign with no effect is called effective
  5% of the time; leaving immature customers out leaves the lift unbiased; and a test of the size M93's planner asks
  for finds the planned effect 80% of the time. Each check must land within four Monte Carlo standard errors of its
  target, a band set by the number of simulations ([`docs/DECIDE.md`](docs/DECIDE.md) §9).
- **Synthetic quarantine.** `RunRecord.synthetic` and `UploadRecord.synthetic` (`POST /uploads` takes
  `synthetic=true`) follow generated data into every run made from it: the seeded demo's runs and datasets, scheduled
  firings on them, derived uploads, and "Learn from this campaign". Every results and value report drawn from such a
  run, on screen and as PDF, opens with "Synthetic data: planted effect, not a forecast", and `test_docs_honesty`
  keeps the demo's planted figure out of the documentation except where it is labelled planted.

**Phase 1 (M90–M95), against its exit gate** ([`docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md`](docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md)):

- M90–M95 are merged on `main` and all read **done** above; `make lint` and the fast suite are green at M95's
  integration, and the nightly statistical suite passed (15 tests, `make test-statistical`, 2026-10-07).
  `make test-all` (the slow, training tests) was not re-run in full at this integration.
- Each of M91's five defects has a regression test that fails on the commit before its fix: the M91 builders ran
  each new test against the unfixed code and saw it fail before writing the fix (DEC-1301), namely
  `tests/integration/uplift/test_uplift_consent.py`, `tests/unit/production/test_privacy_purpose_coverage.py`,
  `tests/unit/generative/test_sms_one_way.py`, `tests/integration/test_api_generative_copy_identity.py` and
  `tests/integration/decide/test_row_level_downloads.py`.
- The stable universal holdout is shown on synthetic keys: `tests/unit/holdout/test_assign.py` holds membership
  identical across 12 monthly runs on 10,000 customers under `scope: universal` (M92). A full 12-month replay of
  the scoring pipeline has not been run.

**Phase 2 (M96–M101), against its exit gate** ([`docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md`](docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md)):
the gate asks for M96–M101 merged, `make test-all` green, and one journey end to end on fixtures: approve with
checks → value-ranked, offer-chosen, channel-aware treat list → arbitration across three use cases → campaigns
created and measured. **The gate is met.** Its one known gap, the multi-offer campaign's population (DEC-1311 (ab)),
was closed on 2026-10-09 by DEC-1311 (af)–(ak) (below).

- M96–M101 are merged on `main` and all read **done** above. `make test-all` (every test except the `bedrock` and
  `aws` ones) is green at 2e27bd1 (M101's integration) on 2026-10-09: 11,404 passed, 62 skipped, 11 xfailed and 0
  failed in 30.5 minutes under three workers (`-n 3`). That run predates the journey module below and the Phase 2
  gap fix (DEC-1311 (af)–(ak)), which merged after it; `make test-all` has not been re-run in full since. Both were
  run on `main` after their merges: the journey module on its own, serially (27 passed at the gate, 28 after the
  gap fix, together with `tests/integration/decide/test_policy_offer.py`), and the fast suite in full.
- The Phase 2 journey test (`tests/integration/decide/test_phase2_journey.py`, DEC-1311 (z)–(ae)) chains, through
  the API on fixtures, three use cases trained; the M96 checks shown to an Approver (one fails and the approval
  still goes ahead); the multi-offer model refused as champion and scored by naming its version; value-ranked,
  offer-chosen, consent-aware treat lists; arbitration with one action per customer, the universal hold-out and the
  risk model's own hold-out enforced (customers one use case holds back get no action from any); and one campaign
  per use case measured against a planted truth, each campaign's two arms checked exactly against the cut of
  DEC-1311 (n). The module is marked `slow`, so `make test` does not run it; it builds its journey once (about
  60 s, 28 tests in about 70 s); run it with `--dist loadgroup` under xdist.
- The known gap is closed. A campaign of a use case that chooses the offer per customer is measured within the
  customers the policy meant to contact (`policy_intended` of `offer_choice.parquet`, DEC-1311 (af)–(ak)), not
  within the first offer's `intended_treatment`. Under a total budget the budget walk is replayed with its own fit
  test, so the treated arm is exactly the contacted list, a customer the walk skipped is in neither arm, and a
  held-back customer is in the hold-out arm when the walk had room for their offer at their place. Before, about
  1 in 5 of the customers that use case contacts were in neither arm (413 of 1,945 in the journey run); now the
  journey test asserts the multi-offer arms exactly, `tests/integration/decide/test_policy_offer.py` does so on a
  budgeted run, and `tests/unit/decide/test_policy_offers.py` covers the replay of the budget walk.
- Each step is also tested on its own: the advisory approval checks and the fallback to risk ranking (M96,
  `tests/unit/decide/test_model_gates.py`, `tests/integration/decide/test_uplift_ranking_flow.py`), the net-value
  ranking (M97, `tests/integration/uplift/test_net_value_flow.py`), the offer chosen per customer on a channel the
  customer can be reached on (M99 and M100, `tests/integration/decide/test_channel_consent_real_run.py`,
  `tests/integration/decide/test_offer_choice_run.py`), and arbitration across three use cases with one campaign
  per use case (M101, `tests/integration/decide/test_arbitration_review.py`).

**Phase 3 (M102–M105), against its exit gate** ([`docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md`](docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md)):
the gate asks for M102–M104 merged (with M105 following), the nightly coverage tests (yes/no, amounts, per offer)
within their bands, the Proof Pack provenance test green, and an audit readout on at least one real past campaign.
**The gate is met** (2026-10-10): every item below holds, the last one through the plan's Hillstrom fallback
(M110a, DEC-1322).

- M102, M103, M104 and M105 are merged on `main` and read **done** above (M105 on 2026-10-09, DEC-1315).
- **The Proof Pack provenance test is green:** `tests/integration/pilot/test_proof_pack.py`
  (`test_every_digit_on_the_page_and_in_the_pdf_is_a_traced_figure` over every kind of campaign built, and
  `test_a_changed_artefact_makes_the_build_fail_rather_than_show_an_untraced_number`) and
  `tests/unit/pilot/test_proof.py` pass on `main` after the M104 merge (DEC-1314), again after the M105 merge, and
  again on the merged tree at the M110a integration (2026-10-10).
- **The nightly coverage tests are within their bands,** as actually run on `main` after the M105 merge
  (2026-10-09): yes/no (`tests/statistical/test_interval_coverage.py`), amounts
  (`tests/statistical/test_continuous_coverage.py`) and per offer (`tests/statistical/test_arm_interval_coverage.py`),
  11 passed, with `MARKETING_AI_STATISTICAL=1`. M105's own `tests/statistical/test_fading_false_alarm.py` passed on
  the merged tree too (2 passed). Earlier, M103's `tests/statistical/test_complier_coverage.py` (4 passed) and M104's
  `tests/statistical/test_backfire_false_alarm.py` (1 passed) passed after their merges on 2026-10-09. The whole
  nightly suite (`make test-statistical`) was not re-run in one go at the M103, M104 or M105 integration. The three
  coverage tests were run again on the merged tree at the M110a integration (2026-10-10): 11 passed.
- **The audit readout on a real past randomised campaign is produced (M110a, DEC-1322).** No prospect or Minfy team
  has supplied a past campaign, so the plan's fallback applies: Hillstrom's own e-mail test (a retailer's 2008
  randomised campaign, a public dataset, read retrospectively) goes through the M103 route a client would use,
  `POST /uploads` then `POST /campaigns/audit`, with the customer details in the assignment file and the outcomes in a
  separate file. The engine labels it **Causal** (verified random), not the harness: conversion, men's e-mail
  +0.68 pts (145 extra, 107 to 184) and women's +0.31 pts (67 extra, 32 to 101); spend +$0.60 per customer for any
  e-mail, skew-flagged. Four Value Proof Packs pass provenance. The programme readout does not apply (409
  `PROGRAMME_NO_HOLDOUT`: the file has no universal hold-out). The numbers are in section 8 of
  [`library/hillstrom-email/run_report.md`](library/hillstrom-email/run_report.md), rendered from the committed
  aggregate `audit.results.json` (`library/tests/test_hillstrom_audit.py`). It is a public dataset's campaign, not a
  client's; a client's own past campaign remains the next proof. Before it, M110 (DEC-1320) had replayed Hillstrom's
  random e-mail as two campaigns recorded with `POST /campaigns`, which did not exercise the audit route.

**Phase 4 (M106–M111), against its exit gate** ([`docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md`](docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md)):
the gate asks for M106–M111 merged, the scheduled cycle green on fakes, validation results recorded and the demo
rehearsed; the manager review follows. **The gate is not yet met:** the demo has not been rehearsed by a person
outside the team.

- M106, M107, M108, M109, M110 and M111 are merged on `main` and read **done** above, all on 2026-10-10 (DEC-1316,
  DEC-1317, DEC-1318, DEC-1319, DEC-1320 and DEC-1321).
- **The scheduled cycle is green on fakes:** `tests/integration/measurement/test_monthly_loop.py` (marked `slow`)
  runs SCORE → TREAT LIST → MEASURE → LEARN on schedules under a controlled clock, reading the month's tables and the
  campaign's outcomes from fake connections, read-only, and ends with a challenger waiting for an Approver and the
  champion unchanged; it passed on `main` after the M107 merge (1 passed, run serially, 2026-10-10). The same test
  checks that a capped deployment's learning run is refused with nothing written (DEC-1317 (h)).
- **Validation results are recorded (M110, DEC-1320)** on one dataset, Hillstrom (64,000 customers, randomised to a
  men's e-mail, a women's e-mail or no e-mail), in `library/hillstrom-email/run_report.md`, which is rendered only
  from the run's results and which a fresh run on the full file reproduces (`library/tests/test_hillstrom_email.py`,
  19 passed on `main` after the merge, 2026-10-10). They go against the product as often as for it: uplift does not
  beat risk ranking, in the engine's check or out of sample, and is not calibrated; the risk model's ROC-AUC is 0.534
  and does not beat its baseline; the targeted list beats sending no e-mail (+0.45 pts off-policy, +0.60 pts by
  replay) but loses to sending everyone the men's e-mail (-0.24 pts). Criteo and X5 were unreachable and Lenta's
  licence is not approved, so no second dataset was run. M110 itself did not run the M103 audit route; M110a
  did, on the same file (DEC-1322, see the Phase 3 gate above).
- **The demo is built (M111, DEC-1321):** [`library/DEMO_SCRIPT.md`](library/DEMO_SCRIPT.md) runs on the
  validated Hillstrom results only, loaded by `python -m scripts.seed_validated`, and quotes only
  [`library/hillstrom-email/DEMO_SUMMARY.md`](library/hillstrom-email/DEMO_SUMMARY.md), a page generated from the
  committed aggregate `journey.results.json` with every number traced and re-verified. It tells the results as they
  fell, including those that go against the product, and states its limits first.
- **The rehearsal is PENDING.** No person outside the team has rehearsed the demo end to end. Its checklist is
  [`library/DEMO_REHEARSAL.md`](library/DEMO_REHEARSAL.md); the agent's dry run recorded as Part 2 of that file is not
  the rehearsal. **The Phase 4 gate is not met until a person outside the team rehearses the demo** and signs that
  file; the manager review follows.
<!-- ---- END PLAN-J ---- -->
<!-- ---- PLAN-L (data doctor) — append only below this line ---- -->

## Plan L — Data Doctor, one agent for messy data (M150–M161)

[`docs/plans/MARKETING_AI_PLAN_L_DATA_DOCTOR.md`](docs/plans/MARKETING_AI_PLAN_L_DATA_DOCTOR.md) is for clients
with no data team: files of any shape go in, Data Doctor reads them, fixes what it can with the person's approval,
lets them correct anything by hand, builds the one file a use case needs, and says plainly what is missing. Every
change stays a step our tested code runs and next month's file replays. How it works, in plain words:
[`docs/DATA_DOCTOR.md`](docs/DATA_DOCTOR.md). Decisions: DEC-1500 … DEC-1599 in [`docs/DECISIONS.md`](docs/DECISIONS.md),
ten numbers per milestone.

| # | Milestone | Definition of done | Status | Tests that prove it |
|---|---|---|---|---|
| M150 | Intake widening | Excel workbooks (every sheet listed, the table chosen), the header found under titles, two header rows joined, total and note rows left out, repeated names numbered, missing words read as empty in number/date columns; a clean CSV stored byte for byte as before; `intake=false` reads as before; uploads, imports, sources and pulls alike; the upload screens say what was done | **done** | `tests/unit/intake/`, `tests/integration/test_api_intake.py`, `tests/integration/test_intake_ui.py` |
| M151 | Data Doctor, the place | `#/doctor`, linked from Home: many files, what each one is, what they can power per use case (Guided setup's own advisor), a hand-off that opens the use case's Guided setup on the file | **done** | `tests/unit/doctor/test_diagnose.py`, `tests/integration/test_api_doctor.py` |
| M152 | The AI proposes steps | `propose_step`: the chat suggests a data fix, grounded in a look at the column, checked by the recipe engine, measured on 1,000 rows, never ticked, labelled as the AI's (prompt v2) | **done** | `tests/unit/agent/test_propose_step.py` |
| M153 | New value steps | nine stateless steps (`map_values`, `set_missing_text`, `convert_units`, `correct_cells`, `split_column`, `valid_range`, `fill_missing`, `remove_rows`, `rename_column`), and the rules' range suggestion for ages and percentages | **done** | `tests/unit/agent/test_value_steps.py`, `tests/unit/agent/test_recipe_plan_l.py`, `tests/unit/agent/test_range_advice.py` |
| M154 | Editing by hand | "Edit by hand" in Guided setup: a table view (personal data hidden); a cell corrected for one customer, a value changed everywhere, rows left out; each edit an accepted step labelled "Your edit", undone by unticking, replayed next month | **done** | `tests/unit/agent/test_edits.py`, `tests/integration/agent/test_edit_ui.py` |
| M155 | Several files (level 4) | the diagnosis says how each log meets the customer list (the measured share, the fix in plain words such as taking off `CUST-` and leading zeros, and the share after it) and which files are the same report for different months (with column drift); a built dataset scored by a model with a recipe is prepared by it, through `POST /runs` and scheduled firings (`RECIPE_DATASET_UNSUPPORTED` retired); Data Doctor driving the builder's roles, mappings and stacking is still to do | **done** | `tests/unit/doctor/test_joins.py`, `tests/integration/test_api_doctor_joins.py`, `tests/integration/agent/test_dataset_recipe_refusal.py` |
| M156 | Wide sheets | `unpivot`: a month-per-column sheet into rows (one per customer per month) by a fixed name grammar, the outcome carried, first in the fixed order; the advisor asks when the months have a year, the rows are combined back per customer, and next month's sheet replays the frozen column list | **done** | `tests/unit/agent/test_unpivot.py`, `tests/unit/agent/test_advisor_unpivot.py`, `tests/integration/agent/test_unpivot_api.py` |
| M157 | Outcome from events (level 5) | a use case no file has the outcome of is offered it from an event log as a sentence with choices ("no recharge for 30 / 60 / 90 days after the snapshot"), each counted by onboarding's own label builder, censored month-ends left out; `POST /doctor/outcome` records an explicit yes to one exact sentence and returns its `LabelSpec`; sentences in `configs/outcomes.yaml`; the chat's `propose_label` not yet | **done** | `tests/unit/doctor/test_outcomes.py`, `tests/integration/test_api_doctor_outcome.py` |
| M158 | Enough data | a use case that is not ready says what is short (the Run button's own counts against `validation.min_rows`/`min_positive`), what to do now (a use case the files power; for one that contacts customers, the next campaign with its control group sized by the test planner) and what to collect from whom (the outcome's source and the pilot's data request) | **done** | `tests/unit/doctor/test_sufficiency.py`, `tests/integration/test_api_doctor.py` |
| M159 | Memory per client | a file of a shape treated before (columns in order plus sheet) is offered the treatment on upload and in Guided setup; "Use the same treatment" ticks the saved steps, still approved; per use case (uploads are not client-scoped); names and ids only; covered by retention and erasure | **done** | `tests/unit/doctor/test_memory.py`, `tests/integration/agent/test_api_doctor_memory.py` |
| M160 | The messy-file benchmark | thirty files shaped like real exports, five use cases, driven to Approve with no person; `reports/plan_l_benchmark.md`: 0 fail at upload, 30 of 30 reach an approved run, 0.33 questions per file | **done** | `tests/integration/test_plan_l_benchmark.py` |
| M161 | Hardening and docs | a security review of every new surface, fixed (erasure reaches kept originals, corrections by the ID column only, hostile workbooks refused, a failed upload leaves nothing, uploads off the event loop, a bounded diagnosis); a workbook opens without reading its rows and the biggest sheet is read; the Data Doctor page shows how files fit and asks the outcome question; `docs/DATA_DOCTOR.md` complete; the fast suite green (12,603 passed) | **done** | `tests/integration/test_intake_erasure.py`, `tests/integration/test_doctor_ui.py` |
<!-- ---- END PLAN-L ---- -->
<!-- ---- PLAN-K (agents that check) — append only below this line ---- -->

## Plan K — Agents that check, not agents that send (M130–M145)

[`docs/plans/MARKETING_AI_PLAN_K_PROPOSAL.md`](docs/plans/MARKETING_AI_PLAN_K_PROPOSAL.md) (what and why) and
[`docs/plans/PLAN_K_TECHNICAL_DESIGN.md`](docs/plans/PLAN_K_TECHNICAL_DESIGN.md) (how) let an agent, ours or another
company's, look at the product's own numbers and suggest, never send: every suggestion is a proposal checked by a
default-deny policy, made real only by a person with the Approver role, and written to a hash-chained, signed decision
ledger. Five invariants hold throughout: no tool writes or sends, every number comes from a tool result, nothing
becomes real without a person, no agent changes who is held back, and every proposal, approval, refusal and evidence
label is on the ledger. Everything is behind three switches in `configs/agentic/agentic.yaml` (`agentic.enabled`,
`compliance.gate.enabled`, `mcp.enabled`), all off by default, and with all three off the product is Plan J's. How it
works: [`docs/AGENTIC.md`](docs/AGENTIC.md). Decisions: DEC-1400 … DEC-1499 in [`docs/DECISIONS.md`](docs/DECISIONS.md);
M131, M135 and M137 were built ahead of the rest of Plan K, under Plan J's range (DEC-1323, DEC-1324, DEC-1325).

| # | Milestone | Definition of done | Status | Tests that prove it |
|---|---|---|---|---|
| M130 | Plan K set-up | DEC-1400…1499 claimed in `PARALLEL_WORK_PROTOCOL.md` §4; a `PLAN-K` block after Plan L's in the 12 shared files and in the marker test's `PHASES`; `engine.agentic.codes.PLAN_K_CODES` wired into `engine.pilot.help.known_codes()`, so every Plan K code needs a plain-language entry; the three switches `agentic.enabled`, `compliance.gate.enabled` and `mcp.enabled` declared, documented and off in `configs/agentic/agentic.yaml` (read by `engine/agentic/config.py`); the milestone list in `docs/AGENTIC.md`; the three milestones built early moved into this table. No product behaviour changes. DEC-1400 | **done** | `tests/unit/test_shared_file_markers.py`, `tests/unit/pilot/test_help.py`, `tests/unit/agentic/test_flags.py` |
| M131 | TRAI Third Amendment consent rules and the measurement purpose (Plan K, built early (DEC-1323)) | India's TRAI Third Amendment rules, kept as data in the region file, and a consent purpose for measurement. The consent ledger records an optional `basis` (explicit, inquiry, legacy) and `operator_registered` per consent, through `POST /privacy/consent`, the CSV import and the connection import (a bad cell is `CONSENT_BASIS_INVALID` or `CONSENT_REGISTERED_INVALID` by row and column, the value never quoted; Postgres gets Alembic `0008`, SQLite is upgraded in place). When the client's region file has a `consent_basis` block (India ships one), an unexpired inquiry consent older than 7 days and a legacy consent not recorded as registered count as not contactable on call, ivr and sms only: those customers are not suppressed, keep their score, band and action, and the treat list uses another channel they can be reached on; `channel_contactability.json` counts them per channel and records the rules' sha256. Region files gain `<channel>_requires_true`, and India requires `a2p_declared: true` on catalogue call and ivr actions (a real YAML boolean; otherwise `ACTION_A2P_UNDECLARED`). `configs/privacy.yaml` gains the purpose `measurement_comparison_groups`, which no use case maps and nothing depends on (a legal opinion is pending). Opt-in: with no catalogue, region file or block a run is byte-identical. Known limit: the rules are read from the run process's config root (`docs/DECIDE.md` section 26). Not legal advice. A Plan K milestone built early under Plan J's decision range. Built by Claude Code after one review round with two lenses. DEC-1323 | **done** (built early under Plan J, DEC-1323) | `tests/unit/decide/test_a2p_declaration.py`, `tests/unit/production/test_consent_basis.py`, `tests/integration/decide/test_consent_basis_real_run.py`, `tests/integration/measurement/test_connection_pulls_api.py`, `tests/integration/production/test_privacy_routes.py` |
| M132 | The agent-safe core | A tool registry with read and propose kinds only (no write kind; each tool copies the route policy of the route that serves the same data, and a test holds them equal). Eight tools wrap results the product already stored: seven reads plus `draft_test_plan`; each result carries a content-hash evidence id, aggregates only within 16 KB, null with a plain reason when not computed and `TOOL_ARTEFACT_UNREADABLE` when stored but unreadable. A default-deny policy (`configs/agentic/policy.yaml`) with six rules, `holdout.immutable`, `budget.cap`, `consent.channel` (judged by the plan's reach), `frequency.cap`, `test.registered` and `role.separation`, over facts the server builds from stored artefacts, never from the payload. Proposals are drafted for Analysts only and approved by another Approver through `engine.approvals.decide_proposal`; the stored proposal must match its ledger entry or it is refused as `PROPOSAL_TAMPERED`, and a test plan is registered only if its hash is the one proposed. The decision ledger is append-only, hash-chained and Ed25519-signed, holds ids, digests and codes only (never a row or free text), has indexed proposal and evidence lookups (Alembic `0009`) and `python -m engine.agentic.ledger verify --from-checkpoint`. `/agentic` proposal routes; audited tools write audit events. Scenario evaluation: pass^k at k=10 over 27 scenarios and a policy mutation test. Everything behind `agentic.enabled`, off by default and byte-identical when off (migration `0009` still adds the empty table). Docs: `docs/AGENTIC.md`. Built by Claude Code after one review round with two lenses. DEC-1401 | **done** | `tests/unit/agentic/test_canonical.py`, `tests/unit/agentic/test_ledger.py`, `tests/unit/agentic/test_policy.py`, `tests/unit/agentic/test_registry.py`, `tests/unit/agentic/test_switch.py`, `tests/agentic/scenarios/test_scenarios.py`, `tests/agentic/test_proposal_routes.py`, `tests/agentic/test_read_tools.py`, `tests/unit/measurement/test_campaign_migration.py` |
| M133 | The verifier over MCP, demo data only | An MCP server at `/mcp` (the official SDK, `mcp==2.3.0`; streamable HTTP, stateless, POST only; sign-in required: 503 `AUTH_NOT_CONFIGURED` with sign-in off, 401 `AUTH_REQUIRED` without a session) exposes exactly the agent-safe core's read tools (scope `marketing-ai:read`, every signed-in person) and `draft_test_plan` (`marketing-ai:propose`, Analyst); no scope approves and no tool sends or writes (a test walks the tool list). Results are `structuredContent` validated against the tool's output model, with a short text written by our code in the Value Proof Pack's scope and words; a scripted client gets Hillstrom's numbers exactly as the product shows them. Demo library only (`MCP_DEMO_ONLY`, the same answer for "not there"); no group of 1 to 9 customers is shown (Plan I's complementary suppression; `MCP_GROUP_TOO_SMALL` for a campaign the Pack would print one of); a third-party text gate; per-sign-in rate limits (`MCP_RATE_LIMITED`); long tools time out (`MCP_TIMEOUT`, no Tasks in the pinned SDK); every call audited. Behind `mcp.enabled` in `configs/agentic/agentic.yaml` (with `agentic.enabled`; settings in `configs/agentic/mcp.yaml`), off by default: nothing mounted and the SDK not imported. Docs: `docs/AGENTIC.md` section 8. Built by Claude Code after one review round with two lenses. DEC-1404 | **done** | `tests/unit/mcp/`, `tests/integration/mcp/test_scripted_client.py`, `tests/integration/mcp/test_threats.py`, `tests/integration/mcp/test_small_groups.py`, `tests/integration/mcp/test_injection_canaries.py` |
| M134 | The pre-send compliance gate | Before a treat list leaves the product, every customer it would contact gets one decision per planned channel (`engine/compliance/`): consent on the channel from the run's own `channel_contactability.parquet`, the registered DLT template (five tags, a linear Thompson automaton, no language model), the URL whitelist including links with no scheme, and a WhatsApp opt-in of its own in the consent ledger; a catalogue changed since the run fails closed. Offers are planned by arm, never by label. A customer goes on the first planned channel the gate allows, else is not treated. The decisions go to `compliance_decisions.parquet` (row-level), whose sha256 is signed on the decision ledger; a list stored before the switch was turned on is rebuilt through the gate. Opt-in with `compliance.gate.enabled` in `configs/agentic/agentic.yaml`, the gate's one switch (off by default, and then every treat list is byte-identical); its settings are in `configs/compliance/compliance.yaml`, which refuses a switch of its own (`COMPLIANCE_SWITCH_MISPLACED`). 1M customers × 3 channels: decide about 5 s, the whole hand-off about 23 s. Docs: `docs/AGENTIC.md` section 9a. Built by Claude Code after one review round with two lenses. DEC-1405 | **done** | `tests/unit/compliance/test_compliance_config.py`, `tests/unit/compliance/test_dlt.py`, `tests/unit/compliance/test_urls.py`, `tests/unit/compliance/test_gate_rules.py`, `tests/unit/compliance/test_gate_offer_arms.py`, `tests/unit/compliance/test_gate_scale.py`, `tests/integration/compliance/test_gate_real_run.py`, `tests/integration/compliance/test_gate_offer_choice_run.py`, `tests/integration/decide/test_treat_list_golden.py` |
| M135 | The scoring run's prediction as the adjustment, CUPAC (Plan K, built early (DEC-1324)) | A yes/no outcome's range narrowed by the chance of the outcome the scoring run predicted before the campaign. A test plan may register `covariate_source: run_score` (with `covariate_column` null; naming both is refused), for a yes/no outcome or an amount; it is part of the plan's content and `plan_hash`, left out while unset, so every earlier plan keeps its hash. The prediction is the propensity run's own `actions.score_field` column (read from its `run_config.json`), or `p_control` on an uplift run, from the campaign's one scoring run (none or several runs, or several offers, are refused: `TEST_PLAN_INVALID`). It must exist before the campaign: a run that finished after `treatment_start` or a customer's own treatment day, or never finished, is `COVARIATE_NOT_BEFORE_CAMPAIGN` (422). It is joined on every key column, never by position; a customer with no prediction gets the mean and is counted in `rows_covariate_missing`. The estimator is M102's adjusted difference, unchanged; the report reuses M102's fields and adds `covariate_source: run_score`, always keeping the unadjusted estimate, and a null adjustment always carries one of four plain reasons. Measuring with it when the plan did not register it is `TEST_PLAN_CHANGED` (409). The verdict, the Value Proof Pack (its own method lines, never M102's "earlier amount"), the value view and the summary's fading rule read the adjusted estimate. A yes/no plan may give `expected_rho2` to size the test. Opt-in: without it every report is byte-identical (a SHA-256 recorded on the code before M135). The nightly test checks coverage, the false-positive rate and a useless prediction. No new codes or routes. Docs: `docs/DECIDE.md` section 27. A Plan K milestone built early under Plan J's decision range. Built by Claude Code after one review round with two lenses. DEC-1324 | **done** (built early under Plan J, DEC-1324) | `tests/unit/measurement/test_run_score.py`, `tests/unit/measurement/test_run_score_scale.py`, `tests/integration/measurement/test_campaign_run_score.py`, `tests/statistical/test_run_score_coverage.py` |
| M136 | Pooling across campaigns | Local empirical-Bayes pooling, opt-in with `pooling.enabled` in `configs/measurement.yaml` (off by default, and then every summary, Value Proof Pack and learn record is byte-identical). Each measured campaign gets an Estimated figure drawn towards comparable campaigns only: the same use case, outcome and window, channels and offers, held-back group and epoch, generated-data flag and client; the registered primary estimate is read from the stored report (M135's adjusted estimate when present), never re-measured. REML with a DerSimonian-Laird fallback and Morris's (1983) estimator and range (a recorded deviation from design §9.2: the design's plug-in point fell just below the coverage band at k=8 and 12). The figure appears beside the measured result in the campaign summary and the Value Proof Pack, never on the benefit line and never in the value proven to date, and as information-only `pooled_prior` in the monthly loop's learn record. Fewer than four comparable campaigns: nothing is pooled and the reason is shown (`POOL_TOO_FEW`); campaigns that differ by no more than chance: `POOL_NO_SPREAD` warning. Files stay current with their members' reports and the settings; a bad settings file is a coded 422. The nightly test (`tests/statistical/test_pooling_coverage.py`) checks coverage at k = 4, 5, 6, 8 and 12. Built by Claude Code after one review round with two lenses. DEC-1402 | **done** | `tests/unit/measurement/test_pooling.py`, `tests/unit/measurement/test_pooling_summary.py`, `tests/unit/measurement/test_cycle_schedules.py`, `tests/integration/measurement/test_campaign_pooling.py` |
| M137 | Safe to roll out (Plan K, built early (DEC-1325)) | A one-sided "safe to roll out" verdict (safe / not safe / cannot tell) on M96's equal-budget comparison of uplift against risk ranking, in rupees when value inputs exist. Opt-in with `uplift.evidence.rollout_verdict` (needs `risk_comparison`; not agent-editable) and `uplift.evidence.rollout_confidence` (one-sided, default 0.95, 0.80 to 0.995): `risk_comparison.json` gains `rollout`, the same paired per-row terms M96's ring cross-fit builds read as one-sided bounds `n·mean ∓ z·√n·sd` (no new selection/certification split). Rupees go through M97's own net value (contact cost cancels at equal budget; a missing value counts as zero, as in M97; above 10% missing the rupee bounds are null with a plain reason); with no value inputs there are no rupees. `rows_needed` is given only for cannot tell with a positive gain, "if the gain seen here is real". The Approver's `UPLIFT_NOT_BETTER_THAN_RISK` message appends the verdict's plain sentence; the approval rule is unchanged. Off by default, and the file is then byte-identical (a golden written on the unchanged code). The nightly test checks the false-safe rate under no effect and the lower bound's coverage. No new codes or routes. A Plan K milestone built early under Plan J's decision range. Built by Claude Code after one review round with two lenses. DEC-1325 | **done** (built early under Plan J, DEC-1325) | `tests/unit/measurement/test_compare.py`, `tests/unit/decide/test_model_gates.py`, `tests/unit/uplift/test_uplift_config.py`, `tests/integration/uplift/test_rollout_verdict_flow.py`, `tests/statistical/test_rollout_verdict.py` |
| M138 | An early harm alarm | A one-sided normal-mixture sequential test (design §9.4) checks every interim reading for harm only: in the monthly loop's measure step, including campaigns whose outcome window is still open (read in memory, nothing stored), and on `POST /campaigns/{id}/measure` for an early look or a reading with customers still inside their window. The harmful side is decided as the Value Proof Pack decides it; a benefit never fires; several offers are judged at alpha/m. When it fires: `campaigns/<id>/harm_alarm.json` (aggregate), the `HARM_ALARM` warning card on the campaigns' summary and one `harm_alarm` alert; nothing is stopped, and there is never an early win. Opt-in with `harm_alarm.enabled` in `configs/measurement.yaml` (off by default, and then nothing is read or written); a bad settings file is a coded 422 or a failed firing before anything is measured. Nightly (`tests/statistical/test_harm_alarm.py`): A/A false alarms at or below 1% however often it is read, planted harm of 3 points caught before the planned day in 86.9% of campaigns at 2,712 per group. Docs: `docs/DECIDE.md` section 28. Built by Claude Code after one review round with two lenses. DEC-1403 | **done** | `tests/unit/measurement/test_harm_alarm.py`, `tests/integration/measurement/test_harm_alarm_loop.py` |
| M139 | The campaign planner | "Draft a plan" under a Results warning card, or a goal in a form or in words (`POST /agentic/plans`, Analyst, audited), becomes one approvable campaign plan through a state machine with one interrupt (FRAME, SIZE, CANDIDATES, SELECT, CHECK, DRAFT, INTERRUPT, EXECUTE), each step stored and resumable (`POST /agentic/plans/{id}/resume`). The test is sized on everyone the campaign compares, an amount outcome is planned as an amount, SELECT is rules only (M137's safe first, never not safe), and the M132 policy checks the draft. The language model touches at most two optional steps (goal words onto the form's values; a narrative as structured claims); every claim is checked against the evidence and every number filled in by the renderer, so no sentence with an ungrounded number reaches the screen (`CLAIM_*`; the rules-only summary otherwise and without an AI service). An approval builds the list, opens the campaign and freezes the test plan through the product's own functions, idempotently; an approval too late for the drafted analysis date is `PROPOSAL_STALE`. pass^k at k=10 over the scenario set. No measure schedule is created (an agreed deviation). All of it is off unless `agentic.enabled` is on. Docs: `docs/AGENTIC.md` section 9. Built by Claude Code after one review round with two lenses. DEC-1406 | **done** | `tests/unit/agentic/test_claims.py`, `tests/unit/agentic/test_planner.py`, `tests/agentic/test_planner_routes.py`, `tests/agentic/test_planner_select.py`, `tests/agentic/test_planner_ui.py`, `tests/agentic/scenarios/test_planner_scenarios.py`, `tests/agentic/test_proposal_routes.py` |
| M140 | Message variants as measured arms | With `generative.variant_arms: true` on the use case (off by default, and then every file is byte-identical), at least two approved campaign-copy variants, each registered in the catalogue first (a `copy:` reference bound to the exact words; an SMS variant needs a registered DLT template id, else `VARIANT_TEMPLATE_UNREGISTERED`), become randomised arms of the treat list (`POST /runs/{id}/campaign-copy/variant-arms`), drawn from each customer's own key: the control template, then `variant_a`, `variant_b` and so on, each measured against the no-message held-back group and against each other (Newcombe intervals, Bonferroni). The arms stay in use only while the copy they were drawn from is still approved and unchanged; arbitration refuses a run with arms in force. The Value Proof Pack names the best message only when its interval beats every other. The nightly test (`tests/statistical/test_variant_arms.py`): the planted best message named in 98.3% of 1,000 campaigns, a winner among equal messages in 0%. Docs: `docs/DECIDE.md` section 29. Built by Claude Code after one review round with two lenses. DEC-1407 | **done** | `tests/unit/decide/test_variant_arms.py`, `tests/integration/decide/test_variant_arms_journey.py`, `tests/integration/test_api_config.py`, `tests/statistical/test_variant_arms.py` |
| M141 | The verifier on client data | One deployment serves its clients over MCP (`mcp.workspace: clients`, `mcp.workspaces` in `configs/agentic/mcp.yaml`). Every token is bound to one client workspace, with MCP_WORKSPACE_DENIED outside it (worded the same for "not there"). There is one hash-chained ledger per workspace (`agentic_workspace_log`, Alembic 0010). Every MCP answer and approval receipt carries a receipt (chain, seq, entry hash, holdout salt fingerprint), checked with `python -m engine.agentic.ledger verify --against` (RECEIPT_MISMATCH if the history was rewritten). A proposal is decided on the chain that drafted it, and an edited proposal file gets PROPOSAL_TAMPERED. The MCP tasks extension covers long audits (MCP_TASK_NOT_FOUND, MCP_TASK_CANCELLED; expired records are swept). The `check_before_contact` read tool reports what the compliance gate did, with every count small-group suppressed. Aggregates only; nothing writes, sends or approves. Behind `mcp.enabled` in `configs/agentic/agentic.yaml`, off by default, and the workspace stays `demo`. Docs: `docs/AGENTIC.md` section 8.7. Built by Claude Code after one review round with two lenses. DEC-1408 | **done** (on test client workspaces; no pilot client has used it yet) | `tests/unit/mcp/test_workspaces.py`, `tests/unit/mcp/test_tasks.py`, `tests/unit/mcp/test_suppress.py`, `tests/unit/agentic/test_workspace_chains.py`, `tests/integration/mcp/test_client_workspaces.py`, `tests/integration/compliance/test_check_before_contact.py` |
| M142 | Setup help for integration sources | Guided setup reads Hinglish and abbreviated headers (`cust_mob_no`, `last_rchg_dt`, `grahak_id`) by rules (`configs/agent/header_lexicon.yaml`, name score times a check of the profile's counts), as "please check" suggestions for the ID, outcome, date, consent, opt-out and last-contact columns that are never ticked, whatever `agent.tick_uncertain` says; the use case's own outcome names anchor by whole name. A file with no outcome column but a last-activity date gets lapse drafts (30, 60 and 90 days plus grace, counted from each row's own date) with example customers by row number, never an ID. An optional AI "Second opinion" (`POST /uploads/{id}/agent-session/assist`, through the chat's egress gate, meter and guardrails) can only reorder the rules' candidates and add leak questions. All of it is behind `agentic.enabled` (gated at integration; off, Guided setup's advice, tool results and egress payloads are byte-identical to before). Benchmark of 27 synthetic exports: untuned held-out accuracy of the rules 92.3% (60 of 65), below the 95% target; all cases 95.9%; the 22 earlier digests unchanged. Docs: `docs/AGENTS.md` section 13. Built by Claude Code after one review round with two lenses. DEC-1409 | **done** | `tests/unit/agent/test_headers.py`, `tests/unit/agent/test_header_advice.py`, `tests/unit/agent/test_header_benchmark.py`, `tests/unit/agent/test_header_switch.py`, `tests/unit/agent/test_assist.py`, `tests/unit/agent/test_advisor.py`, `tests/integration/agent/test_assist_api.py` |
| M143 | Tabular foundation model challenger | A model licence allow-list (`configs/agentic/model_licences.yaml`, read by `engine/agentic/model_licences.py`, default deny) refuses any model search whose preset or algorithm is not cleared for commercial use (`MODEL_LICENCE_BLOCKED`: AutoGluon's `noncommercial`, TabPFN 2.5 and later, anything not reviewed); a test reads every allowed preset through the installed AutoGluon. An opt-in `model_search.strategy: foundation` (the `extreme_quality` preset) is never a default and never offered on the form, and **today loads no foundation model**: the frozen `engine/stages/train.py` passes its own algorithms, which AutoGluon prefers, so a run that picks it carries a warning saying so (cross-branch request to the protocol owner). **No benchmark was run** (Hugging Face weights blocked here, no GPU), no gain is shown, and `foundation` is recommended nowhere (`docs/LIBRARY.md` section 9). The default strategy and the 13 shipped use cases resolve exactly as before (pinned). Built by Claude Code after one review round with two lenses. DEC-1410 | **done** (a licence guard; the challenger not benchmarked) | `tests/unit/agentic/test_model_licences.py` |
| M144 | Save-desk card, as a randomised pilot | A read-only per-customer lookup of the stored treat list for call-centre staff (`POST /agentic/save-desk/card`, key in the body, Analyst, one audit event per call with a salted hash of the key, rate limited per person): the top reasons, the recommended offer and channel, the net value in rupees with the list's range, and a likely-to-stay-anyway flag, each line labelled Estimated or Descriptive (never Causal); customers held back or suppressed get no offer, and a customer drawn at random to learn from keeps the offer without "Do not discount". Rules only (no model). Built as a randomised pilot design: `configs/agentic/save_desk_pilot.yaml` registers the test plan, the staff member is randomised (exact split by salted hash), and the power calculation allows for clustering (DEFF = 1 + (m-1) x ICC, staff needed per arm). No pilot result is claimed: the measurement has no clustered standard errors yet, so "ships only on a measured result" stays open. Off unless `agentic.enabled`. Docs: `docs/AGENTIC.md` section 7a. Built by Claude Code after one review round with two lenses. DEC-1411 | **done** (the pilot design; no pilot result is claimed) | `tests/unit/agentic/test_save_desk.py`, `tests/unit/agentic/test_save_desk_pilot.py`, `tests/agentic/test_save_desk.py`, `tests/agentic/test_proposal_routes.py` |
| M145 | Message variants pass the compliance gate | With the pre-send gate on and a run's message arms in force (M140), each customer is checked against the words they will actually receive. A variant's own approved template for their group and channel is checked against its own registered catalogue action: DLT template text on a channel whose region requires one, and every link against the URL whitelist. Each (arm, group, channel) is checked once. Two new fail-closed reasons: `COMPLIANCE_VARIANT_NO_COPY` (no words of the variant on that channel; the customer moves to the next channel where their own variant passes, never to another message) and `COMPLIANCE_VARIANT_UNREGISTERED` (no registered template to check the words against). Messages are tied to the catalogue the arms were drawn under, and only when the arms were drawn over the run's own stamp; a run with no stamp still fails closed. A campaign sets aside, whole, any arm the gate stopped by its words (its customers are in neither campaign arm), and `variant_comparison.json` lists it as not sent and never compares it. Customers withheld for consent stay in their arm (intention to treat). 1,000,000 customers take 5.2 s with arms against 3.6 s without. With the gate off, or no arms, nothing changes. Docs: `docs/AGENTIC.md` §9a, `docs/DECIDE.md` §29. A follow-up found at the integration of M134 and M140. Built by Claude Code after one review round with two lenses. DEC-1412 | **done** | `tests/unit/compliance/test_gate_variants.py`, `tests/unit/compliance/test_gate_variants_scale.py`, `tests/integration/compliance/test_gate_variant_arms.py`, `tests/unit/decide/test_variant_arms.py` |
<!-- ---- END PLAN-K ---- -->
<!-- ---- PLAN-M (simple UI) — append only below this line ---- -->

## Plan M — A product a marketer understands in minutes (M170–M181)

[`docs/plans/MARKETING_AI_PLAN_M_SIMPLE_UI.md`](docs/plans/MARKETING_AI_PLAN_M_SIMPLE_UI.md) re-centres the product on
the thing a marketer already manages in CleverTap, MoEngage or Netcore: the **campaign**. The engine stays exactly as
it is; only navigation, pages and words change, and everything technical goes behind one "Show technical details"
switch. Phase 1 (M170–M173) is quick wins: one contact list, one word for each thing, money in ₹ and lakh, a Help
menu. Phase 2 (M174–M180) is the campaign-centred structure behind `ui.layout`. Phase 3 (M181) proves it is simpler
with a timed five-person test. Decisions: DEC-1600 … DEC-1699 in [`docs/DECISIONS.md`](docs/DECISIONS.md). The
glossary: [`docs/ux/GLOSSARY.md`](docs/ux/GLOSSARY.md), generated from `ui/labels.js`. After the plan, M182 (the brand as
configuration), M183 (the usability session pack) and M184 (safety fixes from the autonomy audit in the research
report [`docs/research/AUTONOMOUS_MARKETING_AGENT_2026_10.md`](docs/research/AUTONOMOUS_MARKETING_AGENT_2026_10.md))
were added; then M185–M188 (the look: a design system, [`docs/ux/VISUAL_DESIGN.md`](docs/ux/VISUAL_DESIGN.md), and
every page redrawn with it), M189 (an honest progress bar) and M190 (the model-approval hold on arbitrated lists).

| # | Milestone | Definition of done | Status | Tests that prove it |
|---|---|---|---|---|
| M170 | Plan M set-up, the glossary and the visible-text check | DEC-1600…1699 claimed in `PARALLEL_WORK_PROTOCOL.md` §4; a `PLAN-M` block after Plan K's in the 12 shared files and in the marker test's `PHASES`. `ui/labels.js` is the glossary as data (one word per thing, the never-shown engine words and why) with a `term()` helper, imported alike by the browser and node; `docs/ux/GLOSSARY.md` is generated from it and a test compares the two word for word. `tests/integration/test_visible_text_ui.py` draws 40 main pages in jsdom (app.js and its modules, in `ui/index.html`'s order) against 118 API answers recorded from the validated Hillstrom demo (`tests/fixtures/capture_ui_demo.py` → `tests/fixtures/ui_demo/api.json`), collects the visible text (leaving out class `tech`, `[data-technical]`, a `<details>` whose summary says only "Technical details", and hidden elements; the summary of a folded section is still read) and fails a page that shows a never-shown word beyond its recorded baseline (`baseline.json`: 405 occurrences on 38 pages, M172's to-do list), or that asks for an answer the recording lacks. A planted "Uplift by decile" on Home fails it. The only page edits are presentation-only `data-technical` marks (Advanced settings, the SHAP beeswarm's folded body). Additive; no engine or API change. Built by Claude Code after one review round with two lenses. DEC-1600 | **done** | `tests/integration/test_visible_text_ui.py`, `tests/integration/production/ui/glossary/labels.test.mjs`, `tests/integration/production/ui/glossary/visible_text.test.mjs`, `tests/integration/test_ui.py`, `tests/unit/test_shared_file_markers.py` |
| M171 | One contact list | One primary "Download contact list" on every scoring page (the Results view, the Output page, the uplift "Who to contact" page): the run's treat list (`GET /runs/{id}/treat_list.csv`, route, Analyst policy and audit unchanged, the file byte-identical); `scores.csv` is "All scores (for analysts)" under Technical details, and a run whose treat list cannot be built (a 409 or 404, nothing else) gets it under the same label with a one-line note. One sentence, built only from the run's own files, says how many are worth contacting, how many will be contacted and why the difference (on an uplift run only what happens after the policy chose; a reached budget is its own clause). Pages draw first and fill in the treat-list answer when the server gives it (`ui/contact_list.js`, `loadContactList`). The Output page keeps tiles, the Priority table, the sentence, the 25-row sample and the one download; the test-size calculator is in a closed section and drift events under Technical details. The visible-text baseline fell from 405 to 369. UI only; no route, config, engine or code change. Built by Claude Code after one review round with two lenses. DEC-1601 | **done** | `tests/integration/decide/test_one_contact_list.py`, `tests/unit/decide/test_contact_list_js.py`, `tests/unit/uplift/test_uplift_ui.py`, `tests/integration/decide/test_treat_list_review.py`, `tests/integration/production/test_production_ui.py`, `tests/integration/test_visible_text_ui.py` |
| M172 | Words and numbers | The glossary applied across every page: the visible-text baseline is empty (`baseline.json` is `{}`), 50 pages on the branch and every Phase 2 page since, with no allow-list. Goal names (Plan M §4.2) are display names from one table, `ui/labels.js` `GOAL_NAMES` / `goalName` / `goalShortName`, applied to every goal the API returns; ids, configs and API answers unchanged. `ui/format.js` is the one number format: rupees in Indian grouping, in lakh and crore with the exact figure in a tooltip, one-decimal percentages, a real minus sign, the em dash for a missing value, and (final integration) `day()` for dates; the Phase 2 pages use it instead of local helpers. Lists show short campaign names (goal and month, at most 40 characters) with the recorded label as a subtitle. A name the person supplied (a file, a campaign label, a column, an offer) is marked `data-user-text` and left out of the check. The uplift workbench reads "Build an effect model" / "Make a contact list"; algorithm names are technical. UI only; no engine, API or config change. Built by Claude Code after one review round with two lenses. DEC-1602 | **done** | `tests/integration/test_visible_text_ui.py`, `tests/unit/ux/test_format_js.py`, `tests/integration/simple/test_simple_ui.py`, `tests/integration/measurement/test_campaign_ui.py`, `tests/integration/test_reports_ui.py`, `tests/unit/uplift/test_uplift_ui.py`, `tests/integration/test_acceptance.py`, `tests/integration/uplift/test_uplift_browser.py` |
| M173 | Help and loose ends | A Help (?) menu top right, keyboard reachable: Take the tour (outside demo mode too, on the real pages, skipping a step whose element never appeared), Glossary (`#/help/glossary`, from `ui/labels.js`), Send feedback, What's new (`ui/modules/simple/whats-new.json`). Settings links Costs, Waiting for approval, Data Doctor, Check a past campaign and Campaigns waiting for results, each by role; `#/industry/<id>` redirects Home, which lists other industries' goals in a closed section. The demo's data-readiness link by goal follows the goal's newest build or says nothing was built (it asked for a report that cannot exist). Pages stop asking for files that cannot exist (the value view first; an outcome window ahead or unknown proves nothing was recorded; the OPE estimate is read when its card opens; no budget curve for a list ranked by the propensity model). A Playwright sweep of every route prefix on a seeded demo store logs zero console errors, and a click crawler from the top bar reaches every route prefix. UI only; no endpoint, config key or code. Built by Claude Code after one review round with two lenses. DEC-1603 | **done** | `tests/integration/test_ui_sweep.py`, `tests/integration/simple/test_simple_ui.py`, `tests/integration/production/test_production_ops_ui_js.py`, `tests/integration/production/test_production_ui_js.py`, `tests/unit/uplift/test_uplift_ui.py`, `tests/integration/test_ui_journey.py` |
| M174 | The new shell | `ui_layout` setting (`MARKETING_AI_UI_LAYOUT`: `classic` by default, or `campaigns`), handed to the browser on `GET /auth/me` (added field). `campaigns` draws `ui/shell.js`: a left sidebar of five places (Overview, Campaigns, Data, Reports, Settings), the approvals bell (a count for an Approver), Help (?) and the profile at top right; it folds behind Menu below 900 px, and the keyboard path has visible focus (the Help menu too). One table in `ui/layout.js` gives every old route a home or a redirect (`#/doctor` is kept, at home in Data; `#/results` goes to the Campaigns list); redirects replace the history entry and only go to a page this build has. With `classic` the app is byte for byte what it was: a differential check removes M174's marked lines from a copy of `ui/app.js` and `ui/chrome.js` and compares the bar on every page of the visible-text check. Presentation only; no route, code or engine change. Built by Claude Code after one review round with two lenses. DEC-1604 | **done** | `tests/integration/test_shell_ui.py`, `tests/integration/test_shell_browser.py`, `tests/integration/production/test_ui_layout_setting.py`, `tests/integration/test_visible_text_ui.py`, `tests/integration/test_ui.py` |
| M175 | Overview | `#/overview` in both layouts: needs your attention (up to five cards, one action each), value proven to date (a floor, in ₹ lakh and crore with its unit), up to five live campaigns with status and next step, + New campaign (the one primary button, in the header) and a first-visit checklist ticked from real state. One additive call, `GET /overview` (Viewer), built from existing summaries; a figure that cannot be traced leaves the cards and headline out. Campaign status is the one rule shared with the Campaigns pages (`campaign_state`, `results_date`; the shared case table `tests/fixtures/campaigns/status_cases.json`). Fits 1440 × 900 on the demo; empty states for a fresh install; usable at 390 px. Built by Claude Code after one review round with two lenses. DEC-1605 | **done** | `tests/integration/overview/test_overview_api.py`, `tests/integration/overview/test_overview_fixture.py`, `tests/integration/test_overview_ui.py`, `tests/unit/measurement/test_campaign_state_cases.py`, `tests/integration/test_visible_text_ui.py` |
| M176 | Campaigns list and the campaign page | `#/campaigns` lists every campaign (the newest 100) with one status chip, Draft, Live, Waiting for results or Measured, by the rule shared with the Overview. Each campaign opens on Overview, Audience and Results tabs, plus Technical details with the switch on; the Overview tab has one primary button that changes with the stage. Results are added on the Results tab only, and the four older places link to it; figures added there equal today's route's. The contact list is read in one pass as it streams. "Refresh the list" re-scores with the current model through the existing route. A campaign another tool sent is marked "Sent by another tool". No API change. Built by Claude Code after one review round with two lenses. DEC-1606 | **done** | `tests/integration/test_campaigns_page_ui.py`, `tests/integration/test_visible_text_ui.py` |
| M177 | "New campaign" wizard | `#/campaigns/new`: Goal, Customers (Guided setup embedded unchanged), Audience, Review and Send on one page, Audience and Review on one screen since M181 part C, whose one button "Approve and download" fixes the test plan and then downloads the list. State lives in the URL and on the server, so a refresh resumes on the right step. Existing routes only: a first campaign learns, then builds its list with the settings the person accepted; a later one scores with the approved model; the wizard creates the campaign record and registers its test plan with the planner's own numbers, frozen before the one download. Figures come only from API answers (customers per channel, the realised comparison group, the engine's value range when it gives one, the cost in rupees once an exchange rate is set). Nothing new in the engine. Built by Claude Code after one review round with two lenses. DEC-1607 | **done** | `tests/integration/test_campaign_wizard_ui.py`, `tests/integration/test_campaign_wizard_journey.py`, `tests/integration/test_visible_text_ui.py` |
| M178 | Reports | `#/reports[/<tab>]` (`ui/modules/reports/`): Value proof (the Value Proof Packs, each with its claim in words and its PDF), Programme summary (the block Results draws, from the same `GET /campaigns/summary`; a failed read shows an error, never "nothing proven"), Check a past campaign (today's audit form in three short steps, sending the same request; checkbox state kept across a redraw) and Model reports (today's `#/pilot` lists, in a closed technical section until M180). Every report reachable from `#/pilot` is reachable here (tested); the audit result through the new form equals the old form's against the real API; `#/results`, `#/audit` and `#/pilot` are unchanged. Four visible-text entries with empty allow-lists; in the classic layout Settings' Advanced tools links it, so the M173 crawler reaches it. UI only; no endpoint, config key or code. Built by Claude Code after one review round with two lenses. DEC-1608 | **done** | `tests/integration/test_reports_ui.py`, `tests/integration/test_visible_text_ui.py`, `tests/integration/test_ui_sweep.py`, `tests/integration/simple/test_simple_ui.py` |
| M179 | Data | `#/data` (Sources) and `#/data/check` (Check my data): saved connections and uploaded files in one list with health and last import; Test, Edit, Delete and "+ Add a source" are the Connections module's own; "Check my data" is one click to Data Doctor. Additive `GET /uploads` (Viewer) with `made_by`, `from_connection_id` and `total`. Old `#/connections` and `#/doctor` routes unchanged. File and source names are the person's (`data-user-text`). Built by Claude Code after one review round with two lenses. DEC-1609 | **done** | `tests/integration/test_api_uploads_list.py`, `tests/integration/data_page/test_data_ui.py`, `tests/integration/test_visible_text_ui.py` |
| M180 | "Show technical details" | One per-person switch, on Settings ("What you see") and in the profile menu. Signed in, the choice is saved on the server (`GET /auth/me` `preferences`, `PUT /auth/me/preferences`, any role, own choice only, audited); with sign-in off, in the browser. Off by default in the campaigns layout and on in the classic one. With it off, the Data, Model and Output links and pages, the uplift workbench's blocks, model reports, Model health, algorithm names, Advanced settings, the campaign page's Technical details tab and every Technical details section are hidden by one CSS rule; the pages still open at their address and permissions do not change. The visible-text check runs in both positions, in both layouts: with it off, no never-shown word appears on any page. Built by Claude Code after one review round with two lenses. DEC-1610 | **done** | `tests/integration/test_api_config.py`, `tests/integration/production/test_preferences_routes.py`, `tests/integration/production/test_production_ui_js.py`, `tests/integration/simple/test_simple_ui.py`, `tests/integration/test_visible_text_ui.py`, `tests/integration/test_technical_switch_browser.py`, `tests/unit/decide/test_contact_list_js.py` |
| M181 | Usability evidence and the switch-over | Part A: the five-person test kit (`docs/ux/USABILITY_TEST.md`, two timed tasks, the observer's sheet, success rules and questions; `docs/ux/usability_results_TEMPLATE.md` for results a person records, never invented), accessibility checks over every page in both layouts (contrast, labels; known defects may only shrink), the keyboard path in Chromium and a screenshot script for both layouts. Part B: Chromium journeys pin the click counts of both tasks from the Overview (Task 2: 1 click by Reports, 3 by the campaign page). Part C: New campaign takes 4 clicks from the Overview, first or later campaign, within Plan M's target of 5 (asserted directly; it was 7 and 6): Audience and Review are one screen, and "Approve and download" fixes the plan and then downloads the list; on a goal's first campaign an Approver approves the new model in that same, explicitly worded click. Until the model is approved no screen offers its list's plan or download, nothing is offered when the approval cannot be checked, and the server refuses both for a campaign's list (`409 MODEL_NOT_APPROVED`; a list no campaign names, as in the Phase 1 acceptance journey, and analyst files such as `scores.csv` stay downloadable, DEC-050). **The switch-over is pending the usability test**: `ui_layout` stays `classic` by default until five people have done the two tasks and the results are recorded. Built by Claude Code after review rounds with two lenses. DEC-1611 | **done** (parts A–C); switch-over pending the usability test | `tests/unit/test_usability_kit_docs.py`, `tests/integration/test_accessibility_ui.py`, `tests/integration/test_keyboard_path_ui.py`, `tests/integration/test_ui_screenshots.py`, `tests/integration/test_usability_journeys.py`, `tests/integration/test_campaign_wizard_journey.py`, `tests/integration/production/test_model_not_approved.py`, `tests/unit/test_getting_started_doc.py` |
| M182 | The brand as configuration | Four optional deployment settings, `brand_name`, `brand_logo_url` (https:// or a path this site serves, such as `/ui/acme-logo.svg`), `brand_logo_alt` and `brand_color` (a `#RRGGBB` that passes WCAG AA on the page, checked at start-up; `MARKETING_AI_BRAND_*`), reach the UI in an added `brand` object on `GET /auth/me` and change the top bar's name and logo, the browser tab's title and the accent colour in both layouts. With none set, every page and `ui/index.html` itself are as before (a differential jsdom test over 16 pages in both layouts). With sign-in on, the sign-in page shows the brand only in a browser that has signed in before. Built by Claude Code after one review round with two lenses. DEC-1612 | **done** | `tests/integration/production/test_brand_setting.py`, `tests/integration/test_brand_ui.py`, `tests/integration/test_brand_browser.py` |
| M183 | The usability session pack | `python -m scripts.usability_session --root DIR --participant P1`: one command per participant seeds the validated Hillstrom store once, gives the participant a fresh copy (never reused silently; `--reset` or `--resume`) and prints the shell-safe command that serves it in the campaigns layout without demo mode; it refuses a root in or around the real data directory. `docs/ux/usability/RECRUITMENT.md` and `docs/ux/usability/CONSENT.md` (not legal advice: it needs review by the client's legal adviser); the kit's §13 describes the pack. Opt-in; no change to the app, API or defaults. Built by Claude Code after one review round with two lenses. DEC-1613 | **done** | `tests/unit/test_usability_session.py`, `tests/unit/test_usability_kit_docs.py` |
| M184 | Safety fixes from the autonomy audit | What EXECUTE carries out is what the policy checked: an approved campaign plan's contact list keeps to its one channel and planned number of contacts, taken from the run's own net value, uplift or score, never by key order (otherwise `409 CAMPAIGN_LIST_NOT_AS_APPROVED`, nothing opened, the plan still pending). Unattended steps (a scheduled scoring firing that names a model, the planner, EXECUTE) use only approved models, under one rule shared with the contact-list hold (`engine/model_approval.py`, `MODEL_NOT_APPROVED`; a candidate a person names on `POST /runs` is still served, DEC-050). `compliance.gate.strict` (default off) withholds a message the gate could not check (`COMPLIANCE_MESSAGE_NOT_CHECKED`). Copy approval refuses whoever asked for or regenerated the copy (`SEPARATION_OF_DUTIES`, sign-in on) and is a signed `copy_template.approved` ledger entry with agentic on. Decisions made with sign-in off carry `identity_verified: false`; `agentic.require_sign_in` (default off) makes EXECUTE refuse them. Defaults unchanged except where a hole is closed. Built by Claude Code after one review round with two lenses. DEC-1614 | **done** | `tests/agentic/test_planner_safety.py`, `tests/unit/decide/test_campaign_scope.py`, `tests/unit/production/test_firing_model_approval.py`, `tests/unit/compliance/test_gate_strict.py`, `tests/integration/test_api_generative_copy_separation.py`, `tests/integration/test_api_generative_copy_identity.py`, `tests/integration/production/test_approvals_identity.py`, `tests/unit/agentic/test_verified_identity.py` |
| M185 | The look's foundation | Inter Variable ships with the app (`ui/fonts/inter/`, OFL-1.1, served as `font/woff2` even in the slim image). One style sheet, `ui/theme.css`, loaded by one `<link>` at the end of `ui/index.html`'s PLAN-M block, restyles both layouts: brand-blue primary buttons and selections, status colours always with an icon and a word, no page card, 12 px cards, underline tabs, a full-height sidebar with the product mark at the top and "by Minfy" at its foot. `ui/components.js` adds the component helpers (pageHeader, button, statusChip, tagChip, statTile, kpiRow, valueRange, emptyState, tabs, stepper, attentionItem) and the one status mapping, `statusOf`. Review fixes: keyboard-reachable stepper links, whole-word stat units, a value-range track that shrinks, 40 px bar targets, chips at their own width. At integration: 40 px small buttons on touch screens, `needs_addon` in the health mapping, a brand-blue bar for a range with no estimate, and the status bridges no page draws any more removed. The design system is in `docs/ux/VISUAL_DESIGN.md`. Built by Claude Code after one review round with two lenses. DEC-1615 | **done** | `tests/integration/test_components_ui.py`, `tests/integration/production/ui/components/components.test.mjs`, `tests/integration/test_shell_ui.py`, `tests/integration/test_shell_browser.py`, `tests/integration/test_brand_browser.py`, `tests/integration/test_accessibility_ui.py`, `tests/integration/production/ui/a11y/contrast.test.mjs`, `tests/integration/test_ui.py` |
| M186 | The look of the Overview, the Campaigns list and the campaign page | Redrawn with M185's theme and helpers. Overview: attention items, a 48 px "value proven to date" hero (no range bar: the API sends a floor), live rows with status chips, a numbered progress-list checklist. Campaigns list: a table with one-line goal tags, status chips, right-aligned counts and the net value (or the extra outcomes) with its likely-range bar, read only from Value Proof Packs that are ready; stacked cards on a phone. Campaign page: the name, the status chip and the view's one primary action in the header, underline tabs, KPI tiles with ranges, "What was sent" as a definition list, a results drop zone. 40 px targets on phones. No API, route or word changes. Built by Claude Code after one review round with two lenses. DEC-1616 | **done** | `tests/integration/test_campaigns_look_browser.py`, `tests/integration/production/ui/campaigns/look.test.mjs`, `tests/integration/test_campaigns_page_ui.py`, `tests/integration/test_overview_ui.py`, `tests/integration/production/ui/overview/overview.test.mjs`, `tests/integration/test_components_ui.py`, `tests/integration/test_brand_browser.py`, `tests/integration/test_shell_browser.py` |
| M187 | The look of the New campaign wizard | The wizard draws the design system: a numbered stepper (circles joined by a line; "Step 2 of 5 · Customers" on a phone); goal cards with a line icon and a quiet stage tag, the chosen goal forgotten when the person leaves; a calm frame around Guided setup; Review opens with three tiles (contacted, comparison group, expected value with its range bar), small tables with share bars, the compliance result as a status chip and one primary button; Send is a success state. Every colour reads a theme token. Same words, routes and 4-click journeys; 40 px targets on touch screens. Built by Claude Code after one review round with two lenses. DEC-1617 | **done** | `tests/integration/test_campaign_wizard_look.py`, `tests/integration/production/ui/campaigns/wizard/wizard_look.test.mjs`, `tests/integration/test_campaign_wizard_ui.py` |
| M188 | The look of Reports, Data, Settings and Help | Reports: underline tabs, the newest pack as the header's one primary action, each campaign's net value with a range bar (lakh and crore figures included) and its evidence as a status chip, a KPI row above the programme summary, a three-step check form with drop zones and labelled fields. Data: sources with type icons and short health chips, the detail on a line below. Settings: cards of rows and a real toggle. Help: a searchable glossary and dated What's new entries. 40 px row actions on touch screens; the selected tab kept in view on a phone. No route, API or word removed. Built by Claude Code after one review round. DEC-1618 | **done** | `tests/integration/test_look_pages_browser.py`, `tests/integration/production/ui/reports/look.test.mjs`, `tests/integration/production/ui/data/look.test.mjs`, `tests/integration/production/ui/simple/look.test.mjs`, `tests/integration/test_reports_ui.py`, `tests/integration/data_page/test_data_ui.py`, `tests/integration/simple/test_simple_ui.py` |
| M189 | Honest progress bar | `status.json` carries a time-weighted estimated_pct, eta_seconds and estimate_basis beside progress_pct (the train stage is weighted by the search's time budget, with a 5 s heartbeat while long stages run that never writes over a cancel or failure written from outside the flow). The goal's Running card, the uplift running screen and the wizard's learning step say "About N minutes left" or "Finishing up…" instead of sitting at 50%. Built by Claude Code after one review round with two lenses. DEC-1619 | **done** | `tests/unit/test_run_progress.py`, `tests/unit/test_run_progress_pipeline.py`, `tests/integration/test_run_progress_api.py`, `tests/integration/test_run_progress_ui.py`, `tests/integration/test_ui.py` |
| M190 | The model-approval hold on arbitrated lists | An arbitrated campaign's test plan and its cross-goal contact list (`/decide/arbitrated-treat-list.*` and the run's `arbitrated_treat_list.*`) are refused with `409 MODEL_NOT_APPROVED` when ANY goal's run the list was built from used a model waiting for approval or turned down, once a campaign names any of those runs; the message names each such run and goal. The source runs are read from the `arbitration_summary.json` kept in each arbitrated run. One-run lists, `scores.csv` and plan.md §11's download with no campaign are unchanged. Built by Claude Code and reviewed before integration. DEC-1620 | **done** | `tests/integration/production/test_model_not_approved.py` |
| M192 | The AI service in one click | Settings › AI service (`#/settings/ai`) shows the two AI services in plain words, "AI that helps your team" and "AI that writes for your customers", each with its state from `GET /ai-service` and one Connect/Change button to its key form; in the campaigns layout the key form uses the same names and goes back to Settings › AI service. The Settings row, the Data page note and the "No AI service is connected" messages link there. No API or engine change. Built by Claude Code after one review round with one lens. DEC-1622 | **done** | `tests/integration/test_ai_service_settings_browser.py`, `tests/integration/simple/test_simple_ui.py`, `tests/integration/test_visible_text_ui.py` |
<!-- ---- END PLAN-M ---- -->
<!-- ---- PLAN-O (Data Doctor) — append only below this line ---- -->

## Plan O — Data Doctor, the one data agent (M230–M250)

[`docs/plans/MARKETING_AI_PLAN_O_DATA_DOCTOR.md`](docs/plans/MARKETING_AI_PLAN_O_DATA_DOCTOR.md) builds **one agent,
Data Doctor**, whose tools are every data capability the product has (the design:
[`docs/plans/DATA_DOCTOR_AGENT_DESIGN.md`](docs/plans/DATA_DOCTOR_AGENT_DESIGN.md)). It keeps one case per piece of
work, writes one prescription that replays at training and at scoring, and issues one readiness report whose `ready`
means the run will not fail for a data reason. Approved by the product owner on 12 October 2026 (O1–O19 as
recommended); the foundation (M230–M235) is built first and the Data Doctor screen (M247) waits for the visual
redesign. Everything is behind `data_doctor.unified` (off), and while it is off nothing changes. Decisions:
DEC-1800 … DEC-1899 in [`docs/DECISIONS.md`](docs/DECISIONS.md). In plain words:
[`docs/DATA_DOCTOR.md`](docs/DATA_DOCTOR.md) §12 (what is coming), §13-§17 (wave 1) and §18-§27 (wave 2), built
behind the switch.

| # | Milestone | Definition of done | Status | Tests that prove it |
|---|---|---|---|---|
| M230 | Plan O set-up | DEC-1800…1899 claimed in `PARALLEL_WORK_PROTOCOL.md` §4; an empty `PLAN-O (Data Doctor)` block after Plan M's in the 12 shared files and in the marker test's `PHASES`. `PLAN_O_CODES` (`engine/doctor/codes.py`, empty) joins `engine.pilot.help.known_codes()`. The switch `data_doctor.unified: false` in `configs/engine.yaml` defaults, validated (`engine/doctor/config.py`, `UseCaseConfig.data_doctor`), not overridable per run, outside the recipe hash and left out of `run_config.json` while off. `docs/DATA_DOCTOR.md` §12 says what is coming. Nothing a run does changes. DEC-1800 | **done** | `tests/unit/test_shared_file_markers.py`, `tests/unit/pilot/test_help.py`, `tests/unit/doctor/test_plan_o_setup.py` |
| M231 | The case | One persistent Data Doctor case per piece of work under `cases/<client>/<case>/` (`engine/doctor/case.py`: `Case`, `CaseFile`, `TimelineEvent`, `CaseStore`): `case.json` with a cross-process version lock (`409 CASE_VERSION_CONFLICT`), an append-only timeline that records user ids and loses no event across a crash, evidence by reference (never a cell). Per-client scope (`404 CASE_NOT_FOUND`, never naming the case). `POST /cases`, `GET /cases/{id}`, `POST /cases/{id}/files`, `GET /cases/{id}/timeline` exist only when `data_doctor.unified` is on (the `_install_cases` hook in `api/main.py`'s PLAN-O block). Guided sessions and Doctor diagnoses open as read-only cases (`CASE_READ_ONLY`); an unreadable case answers `409 CASE_UNREADABLE`, never a 500. Retention and erasure cover the new `cases` store (`case_data`, `case_log`, `forget_upload`, `RetentionResult.forgotten`); `privacy.processing_log_days` defaults to 365. Local store only until `cases/` is registered as an S3 prefix. DEC-1801 | **done** | `tests/unit/doctor/test_case.py`, `tests/unit/production/test_case_privacy.py`, `tests/integration/test_api_cases.py` |
| M232 | One tool registry | `engine/doctor/registry.py`: one registry of READ, PROPOSE and ASK tools (no APPLY; executors refused by name), each call's role checked (`TOOL_ROLE_REQUIRED`; a Viewer calls READ tools only), content-hash evidence ids (`ev_` + 16 letters), `session-only` or `aggregate` exposure and none listed on MCP (O7), budgets of 5,500 characters for row-level tools and 16 KB for aggregates. The design's 80-tool catalogue as data (`engine/doctor/catalogue.py`). Registered: the 22 existing tools, the first 10 wrappers and, at the wave 1 integration, the 16 catalogued tools of M234, M237, M238, M240 and M241 (`engine/doctor/tools_wave_one.py`, rules and screen only; M240's file context over M231's case in `engine/doctor/case_files.py`). Behind `data_doctor.unified` (off) Plan G's advisor and chat call their tools through it, and the 22 + 27 golden digests and whole chat sessions are byte-identical. DEC-1802 | **done** | `tests/unit/doctor/test_registry.py`, `tests/unit/doctor/test_registry_runtime.py`, `tests/unit/doctor/test_registry_canary.py`, `tests/unit/doctor/test_plan_o_wave_one.py` |
| M233 | A policy for every proposal kind | Default-deny rule sets for every Data Doctor proposal kind (`configs/agentic/policy.yaml` `data:`, `engine/doctor/policy.py`, 13 rules, mutation-tested), applied in the chat, the person's edits and the registry (wave-1 tool outputs included, through `OUTPUT_READERS`), behind `data_doctor.unified`: what each kind may suggest, never the ID or the outcome, personal data only hidden, the level, what is never ticked, which settings, decision O10 (warn above 30%, refuse above 60%, counting only what a step rewrites) and the APPROVE-class list with the exact sentence. Every refusal today stays a refusal with the same code; `NA` is never read as yes or no (`POLICY_BOOLEAN_SPELLING`). DEC-1803 | **done** | `tests/unit/doctor/test_policy.py`, `tests/unit/doctor/test_policy_mutation.py`, `tests/unit/doctor/test_policy_runtime.py`, `tests/unit/doctor/test_policy_tools.py` |
| M234 | The readiness report | `check_readiness` (`engine/doctor/readiness.py`, `readiness_contracts.py`, `readiness_probes.py`) gives one verdict (`ready`, `ready_with_warnings`, `needs_answers`, `needs_approval`, `not_ready`) on the prepared copy: POST /runs' own refusals (`RECIPE_ROLES_MISMATCH`, the score-mode recipe replay), its dry run (`check_plan`; the uplift checks for uplift goals) and 15 X-probes that run the engine's own prepare and split. All nine [RDY] §1.7 gaps are `not_ready` naming the probe; a ready report cannot be built over a failing check (property-tested); it agrees with POST /runs on every shared code. 26 of the 30 Plan L benchmark files are ready and 4 `ready_with_warnings`. `store_readiness` and `attach_readiness_to_run` exist; nothing calls them yet (M235). Registered as the aggregate tool #69. DEC-1804 | **done** | `tests/unit/doctor/test_readiness_gaps.py`, `tests/unit/doctor/test_readiness_invariant.py`, `tests/unit/doctor/test_readiness_report.py`, `tests/integration/test_readiness_agrees_with_runs.py`, `tests/integration/test_readiness_benchmark.py` |
| M235 | The visit and the goal brief | The Data Doctor visit (`engine/doctor/visit.py`): a persisted state machine over a case, one atomic `case.json` write per transition, that resumes after a crash at any state with nothing lost or repeated (content ids, `op_id`s, a lazily connected chat port); an optional goal brief taken from the use-case configuration only (`load_goal_brief`, tool #35, `brief_hash`). Without a goal it says what the files can power and what is missing; choosing a goal mid-case asks nothing again, and two goals keep separate sections. Every state completes with no AI service; the chat is Plan G's `chat_turn`. Routes under `/cases/{id}/visit…` exist only with `data_doctor.unified` on (`VISIT_NOT_WAITING`, `VISIT_UNKNOWN_CHOICE`). DEC-1805 | **done** | `tests/unit/doctor/test_visit.py`, `tests/unit/doctor/test_goal_brief.py`, `tests/integration/test_api_case_visit.py` |
| M236 | Wrapper tools and `explain` | Fourteen tools that wrap existing product functions join Data Doctor's registry (`engine/doctor/tools_wrappers.py`: intake, connections, refresh, preflight, the second opinion, roles, key, mapping, personal data, leaks, uplift data, run checks, approval requests), plus #82 and #83 and `explain` (`engine/doctor/explain.py`), which answers only from results the server recorded while code fills every number. Every proposal lands pending; no tool lists the AI as a caller; `second_opinion` keeps its route's `agentic.enabled` switch and refusals; the connection tools share the Connections screen's four-calls-at-once limit (`engine/connections/limits.py`, `CONNECTION_BUSY`). Behind `data_doctor.unified`. DEC-1806 | **done** | `tests/unit/doctor/test_tools_wrappers.py`, `tests/unit/doctor/test_explain.py`, `tests/unit/doctor/test_registry.py`, `tests/unit/doctor/test_registry_canary.py`, `tests/unit/doctor/test_policy_runtime.py`, `tests/unit/doctor/test_plan_o_wave_one.py`, `tests/unit/generative/test_prompts.py` |
| M237 | Several files | The engine core of joining, stacking and building: the `normalise_key` recipe step (prefix, leading zeros, case, spaces, `+91` phones through M241's `to_e164_india`; exact matches only), `join_files` (attribute files and outcome lists onto a customer list, every row counted), `stack_files` (overlapping exports counted once, one question for conflicting rows) and `build_dataset` (`plan_build` proposes; `drive_builder` runs the approved plan through the builder's routes and `replay_build` replays it next month). The list's IDs are kept; joins and stacks that drop more than 2% of rows, and outcome lists, need approval. The M157 outcome offer measures through key rules. Registered as PROPOSE tools; reachable once M233's rule sets exist, behind `data_doctor.unified`. DEC-1807 | **done** | `tests/unit/agent/test_key_steps.py`, `tests/unit/doctor/test_join_files.py`, `tests/unit/doctor/test_stack_files.py`, `tests/unit/doctor/test_build_driver.py`, `tests/unit/doctor/test_tools_files.py`, `tests/unit/doctor/test_outcomes_key_rules.py`, `tests/integration/test_m237_several_files.py` |
| M238 | Row and value tools | The recipe step `keep_latest_per_id` (reshape level, point-in-time probe, never ticked, APPROVE-class above a 2% drop, lost IDs counted) and the tools `synonym_clusters`, `impossible_values`, `detect_units` and `split_file` (`engine/doctor/tools_rows.py`), session-only, each proposal pending and measured by running the step; an AI merge of spellings that changes more than 30% of a column warns and above 60% is refused (O10, `engine/doctor/change_share.py`; M233 applies the same guard to every AI step). `doctor_levels` and `check_reshape_guards` (O3) are defined; wiring them into level resolution and Approve waits for M233. Registered in the registry, behind `data_doctor.unified`. DEC-1808 | **done** | `tests/unit/doctor/test_keep_latest.py`, `tests/unit/doctor/test_tools_rows.py`, `tests/unit/doctor/test_split_file.py`, `tests/unit/doctor/test_detect_units.py`, `tests/unit/doctor/test_synonym_clusters.py`, `tests/unit/doctor/test_impossible_values.py`, `tests/unit/doctor/test_change_share_and_reshape_guard.py` |
| M239 | Outcome tools | `define_outcome` (event presence, absence or a value threshold, with grace, files that leave customers out, each read on a named date, and censoring), `map_outcome_values` (a multi-valued column mapped to yes, no or leave out) and `check_target_type` (`engine/doctor/outcome_definition.py`, `tools_outcomes.py`). Each outcome is an APPROVE-class definition (rule sets `data.define_outcome`, `data.map_outcome_values`) approved by an Analyst with its exact sentence; unread dates and non-number values are counted in that sentence, and anything that changes the meaning is asked, never picked (`OUTCOME_UNDECIDED`). A value the approval never mentioned stops the build (`OUTCOME_VALUE_NOT_MAPPED`). Rules and screen only, never the AI. Behind `data_doctor.unified`. DEC-1809 | **done** | `tests/unit/doctor/test_outcome_tools.py`, `tests/unit/doctor/test_policy.py`, `tests/unit/doctor/test_policy_runtime.py` |
| M240 | Input formats and parser safety | JSON, JSON Lines and a `.gz` or `.zip` holding one table read as tables (`engine/intake/other_formats.py`); each workbook sheet can become its own file of a case (`engine/intake/sheets.py`, through M231's case); `check_parser_loss` names the lines the CSV reader dropped (`X_PARSER_ROW_LOSS`) or a whole-file column shift (`X_PARSER_COLUMNS_SHIFTED`); key-like columns keep leading zeros (`read_upload(keep_leading_zeros=True)`, `DataRecipe.text_columns`, replayed); `parse_date` reads Excel day numbers. Additive and off by default: uploads and ingest are not yet routed through it (wave 2). DEC-1810 | **done** | `tests/unit/doctor/test_parser_loss.py`, `tests/unit/doctor/test_tools_formats.py`, `tests/unit/intake/test_other_formats.py`, `tests/unit/intake/test_leading_zeros.py`, `tests/unit/intake/test_split_workbook.py`, `tests/unit/agent/test_excel_serial_dates.py`, `tests/unit/agent/test_text_columns_replay.py` |
| M241 | The locale profile and Indian formats | Lakh, crore, K, M and `/-` read exactly when proposed (`parse_number`'s opt-in `indian_scale_words`); a bare L, K or M is sure only when the column is clearly money; `haan`/`nahi` and `हाँ`/`नहीं` read only when both are seen, and `NA` is always asked; GSTIN (checksum, PAN or TAN), voter ID, PIN and `+91` phones recognised; GSTIN and voter ID masked before any prompt; a client's number, date and yes/no settings confirmed once and then applied visibly (`engine/doctor/locale.py`), a confirmed currency never overwritten by a later file; Hinglish headers on whenever `data_doctor.unified` is on (27 golden digests change only then). No currency conversion (M242). DEC-1811 | **done** | `tests/unit/doctor/test_india_ids.py`, `tests/unit/doctor/test_indian_values.py`, `tests/unit/doctor/test_locale.py`, `tests/unit/doctor/test_tools_locale.py`, `tests/unit/agent/test_egress_india_ids.py`, `tests/unit/agent/test_header_switch_unified.py` |
| M242 | Currency | Money keeps its unit (`parse_number` `unit`). A foreign currency is asked about (`CURRENCY_FOREIGN`): report it as written, or convert it with the new `convert_currency` step at a dated rate a person gives and an Analyst approves word for word; a rate is never fetched. Mixed currencies stop (`CURRENCY_MIXED`, a 409 that asks which rows are which), and partial conversions guard themselves. The value view and the proof pack list each money column's unit and every rate, its date, source and approver, under "Currency", and keep writing amounts from the rupee value inputs in rupees. The workspace currency is `data_doctor.workspace_currency` (INR) or the client's confirmed profile; `ui/format.js` `setWorkspaceCurrency`. `convert_currency` (#64) is in the registry, the person's only. Switch off: byte-identical. DEC-1812 | **done** | `tests/unit/agent/test_currency_steps.py`, `tests/unit/doctor/test_currency.py`, `tests/unit/doctor/test_currency_reports.py`, `tests/unit/doctor/test_tools_currency.py`, `tests/unit/ux/test_workspace_currency_js.py`, `tests/integration/pilot/test_currency_reports_api.py`, `tests/integration/agent/test_currency_recipe_api.py` |
| M243 | The prescription | Each approval is saved as one prescription with one hash (`engine/doctor/prescription.py`, content-addressed under `cases/<client>/<case>/prescriptions/`): the intake decisions, each file's recipe and column contract, the build, the locale and money basis, the post-build recipe, the outcome, and the goal sections. It is replayed at training (outcome and post-build recipe included, for files and built datasets, scheduled retrains of the same spec, and next month's file) and at scoring (`prescription_replay.py`, `api/routes/run_prescriptions.py`). It stops and asks on any drift (`PRESCRIPTION_DRIFT`, `CURRENCY_FOREIGN`, `CURRENCY_MIXED`; a 409 on `POST /runs`), never re-learns; a missing prescription is `PRESCRIPTION_NOT_FOUND`. Prepared runs keep `prescription_ref.json` and the money basis. With the switch off, runs are byte-identical. DEC-1813 | **done** | `tests/unit/doctor/test_prescription.py`, `tests/integration/test_prescription_runs.py` |
| M244 | Time settings | Training time is offered as Quick, Balanced (recommended) or Thorough, each "about N min", estimated from this deployment's past runs ("from similar public datasets" before any run exists); Guided setup suggests Balanced and keeps the offer it showed, so the progress bar's first ETA equals the promise, and each stage's estimate is corrected by actual-over-expected (`engine/doctor/time_settings.py`, `tools_time.py`). Data Doctor also proposes the date each row describes (a sparse outcome date never is one; `TIME_COLUMN_UNREADABLE`), where a split by date cuts, whether the newest rows' outcomes are final yet and how often to take snapshots; answers are kept as the change itself and remembered per client. `TIME_BUDGET_NOT_USED` for a learner with no time limit. Behind `data_doctor.unified`; the switch-on golden digests pin the training-time suggestion. DEC-1814 | **done** | `tests/unit/doctor/test_time_settings.py`, `tests/unit/doctor/test_time_settings_visit.py`, `tests/unit/agent/test_header_switch_unified.py` |
| M245 | Notice what I don't know | Drift and new values noticed against the prescription, asked once and remembered as standing answers; a new value pattern fixed only by an approved fixed-list step | pending | — |
| M246 | The protocol | One clear question only when it matters (the impact rule, a question budget, always "I don't know"); answers remembered per client and reused visibly and revocably, only while they still mean the same; an explicit yes with the exact sentence and the right role for what changes meaning (`APPROVAL_CONFIRMATION_MISSING`), with separation of duties in cases and Guided setup; grounded three-part doubts (`DOUBT_UNGROUNDED`); requests for more data or a different file; every decision signed into Plan K's ledger (agentic on) or kept on the case timeline (`engine/doctor/protocol.py`, `standing.py`, `plan_g_approval.py`). Behind `data_doctor.unified`. DEC-1816 | **done** | `tests/unit/doctor/test_protocol.py`, `tests/unit/doctor/test_protocol_visit.py`, `tests/unit/doctor/test_plan_g_approval.py`, `tests/unit/agentic/test_ledger.py`, `tests/integration/test_api_data_protocol.py` |
| M247 | One Data Doctor screen, three places | `ui/modules/datadoctor/` opened from Data › Check my data, from the "New campaign" wizard's Customers step and from a goal's setup (through Guided setup's mount), always on the same case; "Choose this goal" continues in the wizard and nothing is asked twice. Questions with "I don't know" and "Change answer"; approvals with the exact sentence, which names the goal by its display name (`engine/doctor/goal_names.py`, kept equal to `ui/labels.js`); "I agree to this sentence" only after "I have read this sentence", and a reader the server would refuse is told whom to ask; changes left out until someone agrees are listed and Approve never starts the run while one is ("Continue with this data" is separate); doubts in three parts, requests for data, remembered answers with their date and Revoke, the chat (Settings › AI service when no AI is connected), the timeline and the verdict ("Ready, with notes" always with a plain note). The server builds the view (`engine/doctor/screen.py`; `GET /cases/{case_id}/screen`, `GET /data-doctor/cases`), and `GET /auth/me` carries `data_doctor` only while the switch is on. Behind `data_doctor.unified`, except the approval sentence's wording. No new codes. DEC-1817 | **done** | `tests/unit/doctor/test_screen.py`, `tests/unit/doctor/test_goal_names.py`, `tests/integration/test_api_case_screen.py`, `tests/integration/test_data_doctor_ui.py`, `tests/integration/test_data_doctor_journey.py` |
| M248 | Evals | `make agent-eval` (`engine/doctor/evals.py`, `scripts/plan_o_benchmark.py`): pass^5 and injection canaries for every registered tool, seven harm metrics with "not measured" shown honestly, the end-to-end "ready" invariant, O10 decided by M233's policy, cost per case; Plan L's 30 files, the extended cases and 20 independent held-out Indian files. It exits non-zero on a failed gate. The cassette is from the scripted reference model until one is recorded from a real model; the gate run (`--train`) waits for M247. DEC-1818 | **done** | `tests/unit/doctor/test_evals.py`, `tests/unit/doctor/test_eval_harness.py` |
| M249 | One Data Doctor | Guided setup and the Doctor page become Data Doctor, old routes become adapters, the seven verdicts become views of one report, and `data_doctor.unified` is turned on once the parity checklist is met | pending | — |
| M250 | Demo and proof | A demo on generated data and `reports/plan_o_proof.md`, with every pitch figure measured or marked "not measured" | pending | — |
<!-- ---- END PLAN-O ---- -->
