# Marketing AI

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
│   ├── industries/
│   │   └── telecom.yaml          # lifecycle stages + which use cases appear
│   └── use_cases/
│       ├── targeted_advertisement.yaml
│       ├── payment_propensity.yaml
│       ├── order_fulfillment.yaml
│       ├── fault_prediction.yaml
│       ├── rca.yaml
│       ├── win_back_campaign.yaml
│       └── telco_churn.yaml      # the public-dataset mapping (M6); YAML only, no engine change
├── templates/                    # generated and committed (DEC-014): <use_case>_template.csv and
│   └── ...                       #   <use_case>_template_README.md for each of the seven use cases
├── engine/
│   ├── __init__.py               # __version__
│   ├── config.py                 # pydantic models for YAML configs, merge, overrides, advanced settings
│   ├── contracts.py              # pydantic models for every artefact JSON
│   ├── settings.py               # the one env-driven Settings: which backends this process talks to
│   ├── llm.py                    # LLMClient protocol + the deterministic FakeLLMClient
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
│   ├── onboarding/               # Phase 2's package; specs.py awaits the Phase 2 plan
│   ├── generative/               # Phase 3a's package; contracts.py awaits the Phase 3a plan
│   └── utils/                    # ids, time, text, logging
├── api/
│   ├── main.py                   # create_app, the module-level app, and the /ui mount
│   ├── deps.py                   # config root, storage, registry and job-runner dependencies
│   ├── routes/                   # industries, use_cases, uploads, runs, models
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

Other targets: `make test-all` (every test, including `@slow`), `make format` (apply `ruff --fix` and
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
| `POST` | `/models/{id}/approve` | approves a version waiting for a human, making it champion |
| `POST` | `/models/{id}/promote` | makes a version champion by hand, recording who did it and why |

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

Two honest caveats. This was measured on 4 CPUs, 15.7 GiB RAM, Python 3.11.15, Linux — **a
container, not the laptop plan §11 names**. And the machine has to be quiet: a third attempt at the
same row count was killed at a 45-minute cap while several other jobs held the same four cores, so on
a busy box expect far worse than the table. Only the CSV path was exercised; the Parquet ingest path
has not been measured.

Re-run it with:

```bash
.venv/bin/python -m scripts.bench_large_file --rows 1000000
```

---

## Milestone status

| # | Milestone | Definition of done | Status |
|---|---|---|---|
| M1 | Skeleton + configs | Repo layout, `pyproject`, configs load and validate, contracts defined, `make setup` works on a clean machine | **done** |
| M2 | Ingest + validate | All validation checks implemented with tests; `POST /uploads` and `POST /runs` return proper 409 payloads on the broken fixtures | **done** |
| M3 | Train flow | Full train on synthetic data produces every artefact; leaderboard, evaluation, decile lift, SHAP reasons are real; registry with champion rule | **done** |
| M4 | Score flow | Champion scores a new file; schema mismatch reported by column name; drift computed; bands, suppression, control group applied; `scores.csv` downloadable | **done** |
| M5 | UI wired | Prototype screens run against the API end to end with no simulated values; Data/Model/Output pages render from artefacts | **done** |
| M6 | Config-only reuse | `payment_propensity` and the Telco churn mapping work by adding YAML only; documented in README | **done** — both are config only and tested; the end-to-end run on the downloaded Kaggle file stays a manual step by design (plan §10) |
| M7 | Hardening | Cancel, error states, large-file handling (streaming, 1M rows in under the time limit on a laptop), logging, `docs/` complete | **partial** — see below |

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

- **Large-file handling, measured.** `scripts/bench_large_file.py` was run at 1,000,000 rows and the
  numbers are recorded above, not projected. Two qualifications keep M7 short of done on this line:
  it was measured on a 4-CPU container rather than the laptop plan §11 names, and only the CSV
  ingest path was exercised — the Parquet path is still untimed.

Not done: **deletion**, the one data-protection control with no seam yet (`docs/AWS_DEPLOYMENT.md` §7),
and the nine advisory settings of DEC-074, which the form records and the engine does not act on. Both
are stated where a reader meets them rather than left to be discovered.

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

---

## Public dataset smoke test

Plan section 10 asks for one public dataset mapped onto the engine **by configuration only**, as the
proof that config-only reuse is real rather than aspirational. That dataset is the **Kaggle Telco
Customer Churn** file, and the mapping is [`configs/use_cases/telco_churn.yaml`](configs/use_cases/telco_churn.yaml).
Nothing under `engine/` or `api/` was added, changed or branched for it.

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
`DEC-` entry with its context, decision and consequences. The log runs DEC-001 … DEC-079: M1 opened it
with DEC-001 … DEC-040, each milestone since has appended its own, and DEC-075 … DEC-079 are the shared
surface the phase branches build on. DEC-059, DEC-064 and DEC-071 are unused — no code cites them.
Numbers from DEC-100 up are allocated per phase — 100…199 for Phase 2, 200…299 for Phase 3a, 300…399
for Phase 4a — so three branches cannot claim the same one. Entries are never rewritten in place — a
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

_Nothing merged yet._

<!-- ---- END PHASE-2 ---- -->

<!-- ---- PHASE-3A (generative) — append only below this line ---- -->

### Phase 3a — Generative and hybrid

_Nothing merged yet._

<!-- ---- END PHASE-3A ---- -->

<!-- ---- PHASE-4A (aws) — append only below this line ---- -->

### Phase 4a — AWS and production

_Nothing merged yet._

<!-- ---- END PHASE-4A ---- -->
