# `library/` — the public dataset library

Six public datasets, five industries, six use cases, run through the engine with **no engine
code** — five trained end to end, one deliberately not.

The summary table, the "what needed code changes" section and the licence overview are in
[`docs/LIBRARY.md`](../docs/LIBRARY.md); the demo script is [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md).
This file is the map of the directory.

## The datasets

| Directory | Industry | Use case | Run? |
|---|---|---|---|
| [`telco-customer-churn/`](telco-customer-churn/) | Telecom | `telco-churn` (ships in `configs/`) | yes |
| [`uci-bank-marketing/`](uci-bank-marketing/) | Banking | `bank-term-deposit` | yes, twice |
| [`online-retail/`](online-retail/) | E-commerce | `retail-win-back` | yes — and it loses to its baseline |
| [`health-insurance-cross-sell/`](health-insurance-cross-sell/) | Insurance | `insurance-cross-sell` | yes |
| [`uci-credit-default/`](uci-credit-default/) | Banking | `card-default-propensity` | yes |
| [`criteo-uplift/`](criteo-uplift/) | Ad-tech | `criteo-uplift` — **planned** (configured for Phase 3b uplift; data unreachable here) | **no**, and the report says why |

Each directory holds the same six files: `README.md` (source, licence, rows, columns, what the
target means, known quirks), `LICENSE.txt`, `fetch.py`, `mapping.yaml`, `run_report.md` and
`sample.csv`. `criteo-uplift/` has a seventh, `use_case.yaml`, and no `sample.csv` — see its
README.

## The rest of the directory

| Path | What it is |
|---|---|
| [`configs/`](configs/) | a second config root: four industry files, four use-case files, and their generated templates. `engine.yaml` is a **symlink** to `configs/engine.yaml`, so there is one copy of the defaults. See DEC-400 and the *two assertions forbid a second industry* entry in [`docs/CROSS_BRANCH_REQUESTS.md`](../docs/CROSS_BRANCH_REQUESTS.md) for why they are not in the repository's own `configs/`. |
| [`run_engine.py`](run_engine.py) | the harness behind every `run_report.md`. It uploads a CSV, calls `Pipeline.run_train` exactly the way `POST /runs` does, and writes a `results.json` holding the validation findings, leaderboard, test metrics, baseline comparison, decile lift, top features and wall clock. Every number in every report comes out of it. |
| [`tests/`](tests/) | one pytest module per dataset, opt-in (DEC-409) |
| [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) | one page: which dataset to show to which audience, and what to click |
| `.runs/`, `*/data/` | git-ignored. `fetch.py` rebuilds the data; the runs are scratch. |

## Running it

```bash
# 1. get a dataset (downloads, prepares, rewrites sample.csv)
python library/uci-bank-marketing/fetch.py

# 2. run the engine on it with defaults
python -m library.run_engine \
  --dataset uci-bank-marketing --use-case bank-term-deposit --config-root library/configs \
  --csv library/uci-bank-marketing/data/prepared.csv --primary-key client_id --target y

# 3. the library's own tests: validate + a one-minute train on each committed sample
python -m pytest library/tests            # about two minutes for all six
python -m pytest library/tests -k telco
```

`--config-root library/configs` is needed for every dataset except `telco-customer-churn`, whose
use case ships in the repository's own `configs/`.

The tests are **not** in pytest's default `testpaths`, so `make test` does not run them. Every
`sample.csv` is committed, so they work with no network.

## The rules this directory was built under

* **Configuration and data only.** Nothing in `engine/`, `api/`, `ui/`, `tests/` or any existing
  YAML was edited. Where the engine needed a change, a
  [cross-branch request](../docs/CROSS_BRANCH_REQUESTS.md) was filed and the work went on without it.
* **No fabricated numbers.** Every figure in a `run_report.md` came from a run whose `results.json`
  is named at the bottom of that report. Where a run failed, lost or never happened, the report
  says so in those words.
* **Every derived column is explained.** Only `online-retail/` has any, and
  [its README](online-retail/README.md) gives the arithmetic for each one, with the leakage
  argument and the assertion that enforces it.
* **PII is flagged and dropped in `fetch.py`.** Only one column anywhere came close — the
  free-text `Description` in the retail invoice log — and it is dropped before aggregation. Each
  `mapping.yaml` carries a `pii:` list; all six are empty, and each README says why.
* **Decisions are recorded.** DEC-400 … DEC-411 in [`docs/DECISIONS.md`](../docs/DECISIONS.md).
