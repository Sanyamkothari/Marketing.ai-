# The public dataset library

Six public datasets, five industries, six use cases, **one engine and no engine code**.

One industry and one use case already shipped (Telecom / `telco-churn`). The library adds four
industries and four use cases, drafts a fifth, and changes no engine code to do it.

Everything in [`library/`](../library/) exists to answer one question: how much of "one engine,
many industries" is true today, by configuration alone? The answer is in the two tables below —
the first is what the engine did, the second is everything it could not do without a change
somebody else owns.

Every number here came from a real run. Where a run failed, or lost to its own baseline, or never
happened, the table says so.

---

## 1. The library

| # | Dataset | Industry | Use case | Rows | Test metric | D1 lift | Story |
|---|---|---|---|---|---|---|---|
| 1 | [Telco Customer Churn](../library/telco-customer-churn/) | Telecom | `telco-churn` | 7,043 | ROC-AUC **0.8573** (baseline 0.8535) | **3.02×** | Call the top 10 % and three in four are leaving; tenure and contract type are 76 % of the answer. |
| 2 | [UCI Bank Marketing](../library/uci-bank-marketing/) | Banking | `bank-term-deposit` | 41,188 | ROC-AUC **0.7825** (baseline 0.7724) *[0.9503 before the leaking column was excluded]* | **4.35×** *[5.83× before]* | The 0.95 model is worthless and the 0.78 model works — and no validation check could tell them apart. |
| 3 | [UCI Online Retail](../library/online-retail/) | E-commerce | `retail-win-back` | 1,463 | PR-AUC **0.4982** (baseline 0.5027) — **loses** | 1.44× | An invoice log alone cannot predict who comes back; the engine ran the whole path and returned an honest no. |
| 4 | [Health Insurance Cross-Sell](../library/health-insurance-cross-sell/) | Insurance | `insurance-cross-sell` | 381,109 | ROC-AUC **0.8555** (baseline 0.8371) | **3.18×** | Half the book is not worth a call; "already insured" is 57 % of the model on its own. |
| 5 | [UCI Credit Default](../library/uci-credit-default/) | Banking | `card-default-propensity` | 30,000 | ROC-AUC **0.7869** (baseline 0.7245) | **3.26×** | The widest win over a linear baseline in the library: last month's payment status is half the answer. |
| 6 | [Criteo Uplift](../library/criteo-uplift/) | Ad-tech | `criteo-uplift` — **planned** | 13,979,592 | **not run** | — | Reserved for Phase 3b. The data is also unreachable from this environment, and both facts are recorded rather than papered over. |

Every run used **engine defaults** — `strategy: balanced`, `time_limit_minutes: 30`, the default
candidate pool — with no overrides, except the second bank run, which adds exactly one:
`prepare.exclude_columns: ["duration"]`.

### The same table, with the parts a demo cares about

| Dataset | Wall clock | Models trained | Winner | Validation findings |
|---|---|---|---|---|
| Telco Customer Churn | 21.0 s | 5 | Ensemble (Logistic Regression + LightGBM + XGBoost) | none |
| UCI Bank Marketing (defaults) | 68.4 s | 5 | Ensemble (LightGBM + XGBoost + Random Forest) | none |
| UCI Bank Marketing (`duration` excluded) | 57.5 s | 5 | Ensemble (LightGBM + XGBoost + Logistic Regression) | none |
| UCI Online Retail | 269.8 s | 5 | "Ensemble (Logistic Regression)" — of one model | 1 warning: `CONSTANT_COLUMN` on `snapshot_date` |
| Health Insurance Cross-Sell | 167.0 s | 5 | Ensemble (XGBoost + LightGBM + Random Forest + 1 more) | none |
| UCI Credit Default | 49.7 s | 5 | Ensemble (LightGBM + XGBoost + Random Forest) | none |

Six runs, none longer than four and a half minutes, the largest on 381,109 rows.

---

## 2. What needed code changes

This is the evidence for how config-only the engine really is, so it is the section to read
sceptically. Four requests were filed in [`CROSS_BRANCH_REQUESTS.md`](../CROSS_BRANCH_REQUESTS.md).
**One of them blocked the work; three did not.**

| Id | What | Owner | Blocked the library? |
|---|---|---|---|
| [CBR-401](../CROSS_BRANCH_REQUESTS.md#cbr-401) | Two assertions in `tests/unit/test_config_loading.py` pin the config directory to one industry and to telecom's seven use cases, so no second industry or seventh use case can be added to `configs/`. | `tests/` | **Yes** — see DEC-400 |
| [CBR-402](../CROSS_BRANCH_REQUESTS.md#cbr-402) | `prepare` has a second, looser PII detector than `ingest`; its phone-number regex matches ISO dates, so `snapshot_date` was redacted with nothing in the validation report to say so. | `engine/` | No |
| [CBR-403](../CROSS_BRANCH_REQUESTS.md#cbr-403) | Template column names cannot contain dots, so `emp.var.rate` and `default.payment.next.month` must be renamed before a use case can carry a template. | `engine/` | No |
| [CBR-404](../CROSS_BRANCH_REQUESTS.md#cbr-404) | `threshold.mode: auto` can settle on a threshold that calls every row positive (recall 1.0, specificity 0.0), which reads as a triumph and is no decision at all. | `engine/` | No |

### What this means, said plainly

**No modelling change was needed for any dataset.** Nothing in `engine/stages/` had to move for
five public files from four industries — telecom, banking, e-commerce and insurance — to be
ingested, validated, prepared, split, trained, evaluated, explained, banded and exported. All five
ran end to end on the first attempt with no overrides at all. That is the claim, and it held. The
sixth, Criteo Uplift, was not run for reasons that have nothing to do with the engine (DEC-406).

**The one blocker is a test, not the engine.** `load_industry`, `list_industries` and every loader
already take a config root and already validate each file on its own (DEC-038). The engine is
perfectly happy with four industries; two test assertions are not. Until they are relaxed, the
library's configs sit in `library/configs/` — a second root the engine already supports, with
`engine.yaml` symlinked so there is nothing to drift — and moving them afterwards is `git mv` plus
`make generate`. See DEC-400.

**Three preparation steps were needed, and none of them was modelling.** Re-writing a
semicolon-separated file as comma CSV; adding a primary key to a file that ships none; renaming
four dotted column names. All three are format, all three are recorded in the dataset's
`mapping.yaml`, and all three are exactly what Phase 2's column-mapping UI is designed to absorb.

**One dataset needed real work, and the brief predicted it.** UCI Online Retail is a transaction
log, and the engine cannot aggregate (plan §12). `library/online-retail/fetch.py` builds the
one-row-per-customer file in the open, every derived column is explained in that directory's
README, and `mapping.yaml` states the aggregation for each one so Phase 2 has a specification to
reproduce.

### What the engine got right that is worth saying

* **It found the leaking column's absence honestly.** On the bank file it did *not* flag
  `duration`, and it was right not to: none of `LEAKAGE_SUSPECTED`'s three branches describes a
  column that does not contain the answer but simply does not exist yet. The library says so
  rather than claiming a catch it did not make — and shows that the fix is one line of
  configuration.
* **It returned a negative result rather than a flattering one.** On the win-back file the search
  lost to its own logistic-regression baseline, and `baseline.json` says `model_beats_baseline:
  false`. A system that could not do that would be worse than useless.
* **It refused nothing it should have accepted.** Five files trained, zero validation *errors*,
  one warning in total across every run.

### What the library could not demonstrate

* **Suppression and consent.** No public dataset carries an opt-out flag or a contact log, so all
  four library use cases set `opt_out_column` and `recently_contacted_column` to `null`
  (DEC-407, following `telco_churn.yaml`'s own precedent). `SUPPRESSION_COLUMN_MISSING` and
  `CONSENT_COLUMN_MISSING` are therefore never exercised here. That is a gap in the demo, not in
  the engine.
* **Time-based splits.** Not one of the six files carries a usable date. The bank file has a month
  with no year; the credit-card file has an ordered six-month series with no dates; the win-back
  file is a single snapshot. Every run used `random_stratified`, and on the bank file in particular
  a time-based split would be the more honest evaluation.
* **Scoring and drift.** The library trains; it does not exercise the score flow, `SCHEMA_MISMATCH`
  or the drift report. A scoring pass over a held-back slice of each file would be the obvious next
  addition.
* **Uplift.** See dataset 6 and DEC-406.

---

## 3. Running it

```bash
# one dataset, end to end
python library/uci-bank-marketing/fetch.py
python -m library.run_engine \
  --dataset uci-bank-marketing --use-case bank-term-deposit --config-root library/configs \
  --csv library/uci-bank-marketing/data/prepared.csv --primary-key client_id --target y

# the library's own tests: validate + a one-minute train on each committed sample
python -m pytest library/tests
```

The tests are **not** in pytest's default `testpaths`, so `make test` does not run them
(DEC-409). Downloads and run directories are git-ignored; each dataset's `sample.csv` is committed
so the tests work with no network.

## 4. The demo script

Which dataset to show to whom, and what to click:
**[`library/DEMO_SCRIPT.md`](../library/DEMO_SCRIPT.md)**.

## 5. Decisions

DEC-400 … DEC-411 in [`docs/DECISIONS.md`](DECISIONS.md), covering the config root, the win-back
label definition, the `duration` call, the derived keys, the Criteo position, and what the library's
tests assert.

**On that last one, briefly, because it is the least comfortable decision here.** The brief asks
each test to assert that test AUC beats the baseline. On a one-minute search over a few thousand
rows, that comparison is a coin flip for three of the five datasets — measured over repeated runs,
`telco-customer-churn` won 2 of 3, `health-insurance-cross-sell` 2 of 3, and `online-retail` 1 of
6, with its ROC-AUC ranging from 0.5307 to 0.6434. So: `uci-credit-default`, whose margin is
+0.045 across five runs, asserts the strict comparison; three more assert a per-dataset ROC-AUC
floor and that the search is never *materially* worse than the baseline; and `online-retail`
asserts no score at all, checking instead that the aggregation produced the right shape, that the
derived target is sound, and that the baseline comparison exists and is readable. Lowering a floor
until a test stops failing would have been quicker and would have meant nothing. DEC-410 has the
measurements.

## 6. Licences at a glance

| Dataset | Licence | Commercial use |
|---|---|---|
| Telco Customer Churn | Apache-2.0 (repository fetched); IBM sample data | yes |
| UCI Bank Marketing | CC BY 4.0 | yes, with attribution |
| UCI Online Retail | CC BY 4.0 | yes, with attribution |
| UCI Credit Default | CC BY 4.0 | yes, with attribution |
| Health Insurance Cross-Sell | reported **GNU GPL v2**, **unverified** — kaggle.com was unreachable | **check first** |
| Criteo Uplift | **CC BY-NC-SA 4.0 — non-commercial** | **no** |

Each dataset's `LICENSE.txt` carries the citation, the attribution requirement and, where a page
could not be reached to confirm the licence, says exactly that.
