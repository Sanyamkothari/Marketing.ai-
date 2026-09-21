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
│   ├── contracts.py              # pydantic models for every artefact JSON
│   ├── storage.py                # Storage protocol + LocalStorage
│   ├── registry.py               # ModelRegistry protocol + local implementation
│   ├── jobs.py                   # JobRunner protocol + ThreadJobRunner
│   ├── pipeline.py               # orchestrates stages for train and score
│   ├── stages/
│   │   ├── ingest.py             # read CSV/Parquet, infer schema, profile
│   │   ├── validate.py           # all validation checks
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

Milestone 1 creates the skeleton: `configs/`, `engine/`, `api/`, `scripts/`, `tests/` and `docs/`.
`ui/` and `data/` arrive with later milestones.

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

## API endpoints available in M1

M1 ships the config-only part of the API. Everything else (uploads, runs, models, artefacts) arrives in
M2–M4.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/healthz` | `{"status": "ok", "version": "0.1.0"}` (DEC-024) |
| `GET` | `/industries` | the industry template: lifecycle stages in order, their use cases, and the legend |
| `GET` | `/use-cases/{id}` | the merged config, the advanced-settings schema and the Setup-screen copy |
| `GET` | `/use-cases/{id}/template.csv` | the downloadable CSV template for that use case |
| `GET` | `/use-cases/{id}/template_README.md` | the one-line-per-column description of that template (DEC-024) |

`GET /use-cases/{id}` accepts optional `columns`, `primary_key` and `target` query parameters so the
column widgets in the advanced settings can be populated from the uploaded file.

---

## Milestone status

| # | Milestone | Definition of done | Status |
|---|---|---|---|
| M1 | Skeleton + configs | Repo layout, `pyproject`, configs load and validate, contracts defined, `make setup` works on a clean machine | **done** |
| M2 | Ingest + validate | All validation checks implemented with tests; `POST /uploads` and `POST /runs` return proper 409 payloads on the broken fixtures | pending |
| M3 | Train flow | Full train on synthetic data produces every artefact; leaderboard, evaluation, decile lift, SHAP reasons are real; registry with champion rule | pending |
| M4 | Score flow | Champion scores a new file; schema mismatch reported by column name; drift computed; bands, suppression, control group applied; `scores.csv` downloadable | pending |
| M5 | UI wired | Prototype screens run against the API end to end with no simulated values; Data/Model/Output pages render from artefacts | pending |
| M6 | Config-only reuse | `payment_propensity` and the Telco churn mapping work by adding YAML only; documented in README | pending |
| M7 | Hardening | Cancel, error states, large-file handling, logging, `docs/` complete | pending |

---

## Public dataset smoke test

The public **Telco customer churn** dataset is the config-only reuse proof: mapping its columns onto a
use-case template and running the engine end to end with YAML alone lands in **M6**, together with
`payment_propensity`. No part of that mapping exists yet, and no numbers from it appear anywhere in the
product until the run is real.

---

## Decisions

Every choice that `plan.md` does not make is recorded in [`docs/DECISIONS.md`](docs/DECISIONS.md) as a
`DEC-` entry with its context, decision and consequences. M1 opens the log with DEC-001 … DEC-030; later
milestones append. See also [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) for the shape of the upload
and [`docs/AWS_DEPLOYMENT.md`](docs/AWS_DEPLOYMENT.md) for the Phase 4 notes.
