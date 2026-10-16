# Marketing AI

**You connect all your data; the Data Doctor, an AI agent, studies it, asks you when it is unsure, and
fixes it only after you say yes; it tells you which campaigns this data can run; you pick one and press
Start; we do the rest in the background and show you the result, with every detail there if you want it.**

The product is one flow of five steps:

1. **Connect.** Upload files (CSV, Excel, zip) or pick a table from a connection (S3, PostgreSQL / Redshift, MySQL and more).
2. **Data Doctor.** It finds problems, asks a few questions (each with a recommended answer), and changes nothing until you approve.
3. **Suggestions.** "Your data can run" lists the campaigns this data supports, best first.
4. **Start.** One button. Training, scoring and the contact list are built on the server; you can close the page.
5. **Results.** The contact list best first, how well the model did on customers it had not seen, and what is not proven. Approve and download.

It is a single-company tool that starts empty. Under the hood it is one reusable engine: a use case is a
configuration file, AutoML trains the model, and every prediction comes with a reason and a next step.

## Run it

Python 3.11 (about 20 minutes the first time, no AWS account, AI service or client data needed).

```bash
make setup      # create .venv and install the project (AutoGluon included)
make run        # serve the app on http://localhost:8000 (reload on)
make demo       # seed Demo Company once (a few minutes), then serve it with demo mode on
make help       # every target
```

Docker: `make image` builds the API image, `make compose-up` runs it with both Postgres servers on
localhost (`make compose-down` stops it). Step-by-step: [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Where things are

* `engine/`: the engine (data checks and fixes in `engine/doctor/`, who-to-contact rules in `engine/decide/`, training, scoring, measurement).
* `api/`: the FastAPI app and its routes; the contract is [docs/API.md](docs/API.md) (generated, do not hand-edit).
* `ui/`: the browser app (plain JavaScript modules, no build step).
* `configs/`: use cases, industries and settings as YAML; a new use case is a new file, not new code.
* `docs/`: guides, decisions (`docs/DECISIONS.md`), plans, and `docs/history/` for old milestone notes.

## The Data Doctor in 5 lines

* Our rules look at every file first (numbers stored as text, dates, yes/no words, placeholder values, Indian formats, mixed currencies, leaking columns, test accounts, one row per household).
* If a Product AI is connected, an AI review then adds questions, doubts and proposed fixes; it never changes data itself.
* Each change is shown before and after with counts and needs your yes; the AI's suggestions are never pre-ticked.
* After approval our own code applies the changes, saves them as a recipe for next month's file, and a readiness check says whether training will fail.
* It still works end to end with no AI connected. Scenarios: [docs/DATA_DOCTOR_SCENARIOS.md](docs/DATA_DOCTOR_SCENARIOS.md). Safety research: [docs/DATA_DOCTOR_SAFETY_RESEARCH.md](docs/DATA_DOCTOR_SAFETY_RESEARCH.md).

## Testing

```bash
make lint       # ruff + black --check + mypy --strict + generated-file drift check
make test       # the fast suite (no @slow, no @frozen, nothing that bills an account)
make test-all   # also the @slow AutoGluon and browser journeys and the @frozen tests
```

* The nightly workflow (`.github/workflows/nightly.yml`) runs `make test-all`, which adds the tests of features v1 froze.
* The Data Doctor trap bench runs about 30 messy-data traps through the rules path in about 40 seconds: `python -m scripts.doctor_trap_bench -k 2` (report in `reports/doctor_trap_bench.md`; its own self-test is `tests/unit/doctor/test_trap_bench.py`).
* A test is never skipped to get green; a skip is a failure to explain.

## Read next

* [docs/START_HERE.md](docs/START_HERE.md): the pages and the flow in plain words, with screenshots.
* [docs/QUICKSTART.md](docs/QUICKSTART.md): run it on a laptop.
* [docs/API.md](docs/API.md): every endpoint.
* [docs/V1_READINESS.md](docs/V1_READINESS.md): how ready v1 is.
* [docs/history/README_MILESTONES.md](docs/history/README_MILESTONES.md): the old README, a milestone-by-milestone history.
